"""
RecruitOS — Candidate + Position specific Call Pitch  (DeepSeek)

Flow when the recruiter clicks "Generate Pitch" on a candidate profile:

  STEP 1  EVALUATE   candidate (profile + work history + CV text)  vs  mandate
                     (title, JD, must-have / good-to-have skills, SOP)
                     -> match score, verdict, strengths, gaps, claims to verify
  STEP 2  PITCH      a human phone script in Nitin's fixed 7-step flow:
                       1 intro recruiter + agency
                       2 hiring location + ask candidate's comfort
                       3 client in very short
                       4 position title
                       5 (rule) never disclose CTC / budget — diplomatic line ready
                       6 position in brief
                       7 skill questions built from Step 1's gaps / claims to verify

BUDGET NEVER LEAKS — enforced at the root, not by asking nicely:
  * mandates.ctc_min / ctc_max are never sent to the model
  * any JD / SOP line mentioning CTC, salary, budget, LPA, lakh, ₹, package,
    compensation is removed before the text reaches the model
  * candidate CTC fields are not sent either
  * the finished pitch is scanned; if a money figure or a banned flattery
    phrase slips through, it is regenerated once with a correction, and if it
    still fails the offending sentence is dropped.

Stored per (candidate, mandate, language) in `candidate_pitches` (additive).
"""

import re
import json
from flask import Blueprint, request, jsonify

from modules.shared import (
    get_db, ts, current_user, effective_company_id, real_user_id,
    login_required, _core,
)
from modules import register_migration

bp = Blueprint('pitch', __name__, url_prefix='/api')

LANGS = {'en': 'English', 'hinglish': 'Hinglish'}
MODEL = 'deepseek-chat'
CV_MAX = 6000
JD_MAX = 6000


# ══════════════════════════════════════════════════════════════════════════
#  MIGRATION
# ══════════════════════════════════════════════════════════════════════════
# Dedicated table name. The generic name "candidate_pitches" (used by the first
# release of this module) can collide with a table an older ATS build left in a
# production database with different columns — CREATE TABLE IF NOT EXISTS then
# silently keeps the OLD shape and every save fails. A unique name cannot
# collide. The old table is left untouched (additive only).
PITCH_TABLE = 'hl_candidate_call_pitch'

_PITCH_COLUMNS = [
    ('owner_id', 'INTEGER DEFAULT 0'),
    ('candidate_id', 'INTEGER DEFAULT 0'),
    ('mandate_id', 'INTEGER DEFAULT 0'),
    ('lang', "TEXT DEFAULT 'en'"),
    ('evaluation', "TEXT DEFAULT ''"),
    ('pitch', "TEXT DEFAULT ''"),
    ('model', "TEXT DEFAULT ''"),
    ('created_by', 'INTEGER DEFAULT 0'),
    ('created_by_name', "TEXT DEFAULT ''"),
    ('created_at', "TEXT DEFAULT ''"),
    ('fingerprint', "TEXT DEFAULT ''"),
]


def _ensure_schema(conn):
    """Create the table if missing and add any missing column. Idempotent,
    additive, safe to call on every request path that writes."""
    conn.execute(f'CREATE TABLE IF NOT EXISTS {PITCH_TABLE} (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                 + ', '.join(f'{n} {t}' for n, t in _PITCH_COLUMNS) + ')')
    have = {r[1] for r in conn.execute(f'PRAGMA table_info({PITCH_TABLE})').fetchall()}
    for n, t in _PITCH_COLUMNS:
        if n not in have:
            conn.execute(f'ALTER TABLE {PITCH_TABLE} ADD COLUMN {n} {t}')
    try:
        conn.execute(f'CREATE INDEX IF NOT EXISTS idx_hlpitch_cand ON {PITCH_TABLE}(candidate_id, mandate_id, lang)')
    except Exception:
        pass


@register_migration
def migrate(conn):
    _ensure_schema(conn)
    _ensure_override_schema(conn)


# ══════════════════════════════════════════════════════════════════════════
#  SANITISERS — keep money out of everything the model sees
# ══════════════════════════════════════════════════════════════════════════
_MONEY_WORDS = re.compile(
    r'\b(ctc|salary|salaries|budget|lpa|lakh|lakhs|lac|lacs|crore|package|compensation|'
    r'remuneration|pay\s*scale|pay\s*range|stipend|per\s+annum|p\.a\.|inr)\b|₹|rs\.?\s*\d',
    re.I)
# A money FIGURE in generated text: "12 LPA", "₹15L", "15 lakh", "10-12 L", "Rs 8,00,000"
_MONEY_FIGURE = re.compile(
    r'(₹\s*\d|rs\.?\s*\d|inr\s*\d|\d[\d,.]*\s*(lpa|lakhs?|lacs?|crores?|l\b|cr\b)|'
    r'\d[\d,.]*\s*-\s*\d[\d,.]*\s*(lpa|lakhs?|l\b))', re.I)
_FLATTERY = re.compile(
    r'(impressive|impressed|came across your profile|saw your profile|seen your profile|'
    r'your profile (is|looks) (great|strong|excellent|amazing|very good|perfect)|'
    r'perfect (fit|match)|great fit|exciting opportunity|amazing opportunity|'
    r'golden opportunity|dream (job|role))', re.I)


def _strip_money(text):
    """Drop every line/sentence of a JD or SOP that talks about pay."""
    if not text:
        return ''
    out = []
    for line in re.split(r'\n+', text):
        parts = re.split(r'(?<=[.!?;])\s+', line)
        keep = [p for p in parts if not _MONEY_WORDS.search(p)]
        if keep:
            out.append(' '.join(keep))
    return '\n'.join(out).strip()


def _clip(s, n):
    s = (s or '').strip()
    return s[:n] + (' …' if len(s) > n else '')


# ══════════════════════════════════════════════════════════════════════════
#  INPUT BUILDERS
# ══════════════════════════════════════════════════════════════════════════
def _skills(v):
    """must_have_skills is stored as a JSON list string — show it as plain text."""
    if not v:
        return ''
    try:
        x = json.loads(v) if isinstance(v, str) else v
        if isinstance(x, list):
            return ', '.join(str(i).strip() for i in x if str(i).strip())
    except Exception:
        pass
    return str(v).strip()


def _role_block(m):
    core = _core()
    jd = _strip_money(core.html_to_text(m['jd']) if m['jd'] else '')
    sop = _strip_money(core.html_to_text(m['sop_text']) if m['sop_text'] else '')
    exp = ''
    if m['exp_min'] or m['exp_max']:
        exp = f"{m['exp_min'] or 0}-{m['exp_max'] or ''} years"
    elif m['experience']:
        exp = str(m['experience'])
    lines = [
        f"Position title: {m['role'] or ''}",
        f"Client company: {m['client'] or ''}",
        f"Job location: {m['location'] or ''}",
        f"Work mode: {m['work_mode'] or ''}" if m['work_mode'] else '',
        f"Experience required: {exp}" if exp else '',
        f"Must-have skills: {_skills(m['must_have_skills'])}" if _skills(m['must_have_skills']) else '',
        f"Good-to-have skills: {_skills(m['good_to_have_skills'])}" if _skills(m['good_to_have_skills']) else '',
        '',
        'JOB DESCRIPTION:',
        _clip(jd, JD_MAX) or '(No JD text on file — use the title, client and skills above.)',
    ]
    if sop:
        lines += ['', "CLIENT'S SCREENING NOTES (what the client wants checked):", _clip(sop, 2500)]
    return '\n'.join(l for l in lines if l is not None)


def _candidate_block(conn, c):
    core = _core()
    wh = conn.execute('SELECT company, designation, start_date, end_date, is_current, description '
                      'FROM work_history WHERE candidate_id=? ORDER BY sort_order, id', (c['id'],)).fetchall()
    hist = []
    for w in wh[:8]:
        span = f"{w['start_date'] or '?'} to {'present' if w['is_current'] else (w['end_date'] or '?')}"
        hist.append(f"- {w['designation'] or ''} at {w['company'] or ''} ({span})"
                    + (f": {_clip(w['description'], 300)}" if w['description'] else ''))
    try:
        cv = core._candidate_cv_text(c, max_chars=CV_MAX)
    except Exception:
        cv = ''
    cv = _strip_money(cv)   # a CV can state current / expected CTC — not needed, keep it out
    lines = [
        f"Name: {c['name'] or ''}",
        f"Current designation: {c['designation'] or ''}",
        f"Current company: {c['company'] or ''}",
        f"Total experience: {c['experience'] or ''} years" if c['experience'] else '',
        f"Current location: {c['location'] or ''}" if c['location'] else '',
        f"Preferred location: {c['preferred_location'] or ''}" if c['preferred_location'] else '',
        f"Notice period: {c['notice_period']} days" if c['notice_period'] else '',
        f"Qualification: {c['qualification'] or ''} {c['specialization'] or ''}".strip() if (c['qualification'] or c['specialization']) else '',
        f"Key skills: {c['key_skills']}" if c['key_skills'] else '',
        f"Secondary skills: {c['secondary_skills']}" if c['secondary_skills'] else '',
        f"Industry background: {c['industry_background']}" if c['industry_background'] else '',
        f"Profile summary: {_clip(c['career_summary'], 800)}" if c['career_summary'] else '',
    ]
    if hist:
        lines += ['Work history:'] + hist
    lines += ['', 'RESUME TEXT:', cv or '(No resume text available — rely on the profile fields above.)']
    return '\n'.join(l for l in lines if l)


# ══════════════════════════════════════════════════════════════════════════
#  PROMPTS
# ══════════════════════════════════════════════════════════════════════════
EVAL_PROMPT = """You are a senior technical recruiter in India screening ONE candidate for ONE job
before a phone call. Compare the candidate against the job honestly and specifically.

Return ONLY a JSON object, no prose, in exactly this shape:
{
  "match_score": <integer 0-100>,
  "verdict": "Strong fit" | "Possible fit" | "Weak fit",
  "summary": "<one plain sentence: why this score>",
  "strengths": ["<specific evidence from the profile that matches a job need>", ...],
  "gaps": ["<a job requirement the profile does not show, or shows weakly>", ...],
  "to_verify": ["<a claim in the profile/resume that matters for this job and must be confirmed on the call>", ...],
  "location_fit": "<one short line: candidate location vs job location, relocation needed or not, or unknown>",
  "must_have_check": [
    {"requirement": "<one must-have of the job>", "kind": "skill" | "experience" | "location" | "other",
     "status": "MET" | "NOT_MET" | "UNCLEAR",
     "evidence": "<the profile/resume fact behind the status, quoted or paraphrased; or what is missing>"}
  ],
  "shortlist_reasons": ["<why this profile CAN go to the client: a job need + the profile fact that meets it>", ...],
  "reject_reasons": ["<why it should NOT go: a job need + what the profile lacks or contradicts>", ...],
  "client_summary": "<3-4 sentences in professional English for the CLIENT: who the candidate is and why they fit THIS role, using only facts from the profile>"
}

RULES
- Every strength/gap must name the concrete skill, tool, domain or responsibility. No generic lines.
- Only use facts present in the inputs. If something is unknown, put it in gaps or to_verify, never assume.
- Must-have skills weigh far more than good-to-have.
- 3-6 items in strengths, 2-6 in gaps, 2-5 in to_verify.
- must_have_check: ONE row for EVERY must-have skill listed in the job, plus one row for the required
  experience range (if given) and one for the job location (if given). If the job lists no must-have
  skills, use the 3-5 most essential requirements of the JD instead.
  MET = the profile/resume clearly shows it. NOT_MET = the profile clearly lacks it or contradicts it
  (e.g. 3 years when 6-10 are required, a different domain, a firm "not willing to relocate").
  UNCLEAR = the inputs simply do not say — never guess MET or NOT_MET.
- shortlist_reasons: 2-4 items when the profile meets the job's core needs, else []. reject_reasons:
  one item per NOT_MET must-have (plus any other serious mismatch), else [].
- client_summary: factual and neutral — no gaps, no weaknesses, no score, no flattery words
  (impressive, perfect, great fit), no contact details.
- Never mention salary, CTC or budget anywhere."""


PAUSE = '(PAUSE)'   # marker the model puts wherever the caller must stop and listen


def _pitch_prompt(lang):
    if lang == 'hinglish':
        lang_rule = ("LANGUAGE: Hinglish — the natural Hindi-English mix Indian recruiters speak on calls, "
                     "written in ROMAN script only (no Devanagari). Keep technical terms, the job title, "
                     "product and skill names in English. Example tone: \"Aapka current location kya hai, "
                     "aur Pune shift hone mein aap comfortable rahenge?\"")
    else:
        lang_rule = ("LANGUAGE: Simple, natural spoken Indian English — short sentences, polite, "
                     "conversational. Not formal written English.")
    return f"""You write the PHONE CALL SCRIPT that a VERY JUNIOR Indian recruiter reads out, word for
word, when calling a candidate about one job. The caller has no technical knowledge, so the script
must be complete, easy to read aloud, and must sound like a real person talking — warm, direct,
unhurried — never like a brochure or a robot.

{lang_rule}

You receive: the recruiter and agency names, the job details, the candidate's profile, and a
screening EVALUATION of this candidate (strengths, gaps, claims to verify).

WRITE "script" AS FLOWING PARAGRAPHS (plain text, paragraphs separated by a blank line, no headings,
no bullets, no numbering), in exactly this order:

Paragraph 1 — Introduction: greet the candidate by first name, introduce the recruiter by name and
  the agency, ask if it is a good time to talk for a few minutes. Then write {PAUSE}.
Paragraph 2 — Location: say where the job is based (and the work mode if known), then ASK about
  their comfort with that location / relocation / commute. If the evaluation shows they live
  elsewhere, ask about relocation naturally. Then write {PAUSE}.
Paragraph 3 — Client: describe the client company in one or two short lines. Use only well-known,
  true facts for recognisable companies; if unsure, stay neutral (e.g. "an established company in
  the solar EPC space"). Never invent size, funding, awards or parent companies.
Paragraph 4 — Position and role (THE MOST IMPORTANT PART — make it DETAILED): state the position
  title clearly, then explain the role in 6 to 9 spoken sentences so the candidate gets a real
  picture: what they will own, the day-to-day work, the products / systems / domain, who they will
  deal with (customers, dealers, site teams, OEMs — whatever the JD says), territory or sites and
  travel if mentioned, the team or reporting line if mentioned, and what success looks like in the
  role. Use ONLY what the JD and role details say — never invent facts; if the JD is thin, say less
  rather than make things up. End by asking if the role sounds interesting to them, then {PAUSE}.
Paragraph 5 — Exposure questions: one short line to transition ("I have a few quick questions
  about your experience"), then ask the exposure questions ONE BY ONE inside the paragraph, each
  followed by {PAUSE}.
Paragraph 6 — Closing: thank them and give the next step (e.g. ask for the updated CV on WhatsApp
  / email and the best time for a detailed discussion).

EXPOSURE QUESTIONS (5 to 7) — the caller CANNOT judge skill level, so the questions only find out
WHETHER the candidate has exposure to each important skill, and roughly how much:
- Form: "Have you worked on / handled <skill or area>? If yes, for about how many years, and in
  which company?" (or the natural Hinglish equivalent).
- Cover every must-have skill, plus every important GAP and every claim TO VERIFY from the
  evaluation. One skill per question.
- NEVER ask "how would you…", "explain…", "what is the difference…", scenario, technical or
  test-style questions. No follow-up drilling. Keep each question to one simple sentence.
Also return the same questions in "exposure_questions" so the caller can tick answers on screen.

HARD RULES
- NEVER state or hint at CTC, salary, budget, package or any money figure — you do not know it and
  must not guess. Instead write "ctc_response": the diplomatic line the caller uses IF the
  candidate asks about budget (the client decides based on the candidate's current package,
  experience and interviews; first understand their current and expected). It must contain NO number.
- NO flattery: do not call the profile impressive/great/strong, do not say "I came across your
  profile", no "perfect fit", no "exciting/amazing opportunity". Be clear and respectful.
- Never tell the candidate their score, gaps or weaknesses.
- Use the candidate's real first name. No placeholders except the recruiter/agency names given.

Return ONLY a JSON object, no prose:
{{
  "script": "<paragraph 1>\\n\\n<paragraph 2>\\n\\n...\\n\\n<paragraph 6>",
  "exposure_questions": [{{"skill": "<short skill name>", "q": "<the exact question>",
                          "note": "<what the caller writes down, e.g. Yes/No + years + company>"}}],
  "ctc_response": "..."
}}"""


# ══════════════════════════════════════════════════════════════════════════
#  DEEPSEEK CALL
# ══════════════════════════════════════════════════════════════════════════
class PitchError(Exception):
    def __init__(self, msg, code=502):
        super().__init__(msg)
        self.code = code


def _ask_json(ds_key, system, user, temperature, max_tokens, endpoint, extra_messages=None):
    core = _core()
    import requests
    msgs = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
    if extra_messages:
        msgs += extra_messages
    try:
        rr = core.call_deepseek(ds_key, {
            'model': MODEL, 'temperature': temperature, 'max_tokens': max_tokens,
            'response_format': {'type': 'json_object'}, 'messages': msgs,
        }, timeout=90, endpoint=endpoint)
    except core.TokenCapError:
        raise PitchError('Monthly AI token cap reached.', 429)
    except requests.exceptions.Timeout:
        raise PitchError('DeepSeek timed out. Please try again.', 504)
    except Exception as e:
        raise PitchError(f'Could not reach DeepSeek — {type(e).__name__}: {e}')
    if rr.status_code != 200:
        try:
            err = rr.json().get('error', {}).get('message', rr.text[:300])
        except Exception:
            err = rr.text[:300]
        raise PitchError(f'DeepSeek returned {rr.status_code}: {err}')
    try:
        content = rr.json()['choices'][0]['message']['content']
    except Exception:
        raise PitchError('Unexpected DeepSeek response.')
    try:
        data = json.loads(content)
    except Exception:
        try:
            data = core.parse_json(content)
        except Exception:
            data = None
    if not isinstance(data, dict):
        raise PitchError('AI returned an unreadable answer. Please try again.')
    return data, content


# ══════════════════════════════════════════════════════════════════════════
#  VALIDATION of the finished pitch
# ══════════════════════════════════════════════════════════════════════════
# Question openers that test ability instead of asking about exposure.
_TEST_Q = re.compile(r'\b(how would you|how do you|how will you|explain|describe how|what is the difference|'
                     r'what are the steps|walk me through|kaise karenge|kaise karte|samjhaiye|difference kya)\b', re.I)


def _all_texts(p):
    out = [('script', str(p.get('script') or '')), ('ctc_response', str(p.get('ctc_response') or ''))]
    for i, q in enumerate(p.get('exposure_questions') or []):
        if isinstance(q, dict):
            out.append((f'question {i + 1}', str(q.get('q') or '')))
    return out


def _violations(p):
    bad = []
    for k, t in _all_texts(p):
        if _MONEY_FIGURE.search(t):
            bad.append(f'"{k}" contains a money figure')
        if _FLATTERY.search(t):
            bad.append(f'"{k}" contains flattery ("{_FLATTERY.search(t).group(0)}")')
    script = str(p.get('script') or '')
    paras = [x for x in re.split(r'\n\s*\n', script) if x.strip()]
    if len(paras) < 5:
        bad.append(f'"script" must be 6 paragraphs separated by blank lines (got {len(paras)})')
    for i, q in enumerate(p.get('exposure_questions') or []):
        if isinstance(q, dict) and _TEST_Q.search(str(q.get('q') or '')):
            bad.append(f'question {i + 1} tests ability ("{_TEST_Q.search(str(q.get("q"))).group(0)}") — ask only about exposure')
    if len(p.get('exposure_questions') or []) < 4:
        bad.append('"exposure_questions" needs 5 to 7 questions')
    return bad


def _clean_text(t):
    """Drop any sentence carrying money / flattery, keep paragraph breaks."""
    paras = re.split(r'\n\s*\n', str(t or ''))
    keep = []
    for para in paras:
        parts = re.split(r'(?<=[.!?])\s+', para.strip())
        para2 = ' '.join(x for x in parts if not _MONEY_FIGURE.search(x) and not _FLATTERY.search(x)).strip()
        if para2:
            keep.append(para2)
    return '\n\n'.join(keep)


def _scrub(p):
    """Last line of defence after the corrective retry."""
    p['script'] = _clean_text(p.get('script'))
    qs = []
    for q in p.get('exposure_questions') or []:
        if isinstance(q, dict):
            qq = _clean_text(q.get('q'))
            if qq:
                qs.append({'skill': str(q.get('skill') or '').strip()[:60], 'q': qq,
                           'note': str(q.get('note') or '').strip()})
    p['exposure_questions'] = qs
    p['ctc_response'] = _clean_text(p.get('ctc_response'))
    if not p['ctc_response']:
        p['ctc_response'] = ('The final number is decided by the client based on your current package, '
                             'experience and how the interviews go. Could you share your current and '
                             'expected so I can position your profile correctly?')
    p['format'] = 2
    return {k: p[k] for k in ('script', 'exposure_questions', 'ctc_response', 'format')}


def _normalise_eval(e):
    try:
        s = int(round(float(e.get('match_score', 0))))
    except Exception:
        s = 0
    e['match_score'] = max(0, min(100, s))
    if e.get('verdict') not in ('Strong fit', 'Possible fit', 'Weak fit'):
        e['verdict'] = 'Strong fit' if s >= 75 else ('Possible fit' if s >= 50 else 'Weak fit')
    for k in ('strengths', 'gaps', 'to_verify'):
        v = e.get(k)
        e[k] = [str(x) for x in v if str(x).strip()][:8] if isinstance(v, list) else []
    e['summary'] = str(e.get('summary') or '')
    e['location_fit'] = str(e.get('location_fit') or '')
    rows = []
    for r in (e.get('must_have_check') if isinstance(e.get('must_have_check'), list) else [])[:14]:
        if not isinstance(r, dict) or not str(r.get('requirement') or '').strip():
            continue
        st = str(r.get('status') or '').strip().upper().replace(' ', '_')
        st = st if st in ('MET', 'NOT_MET', 'UNCLEAR') else 'UNCLEAR'
        kind = str(r.get('kind') or 'other').strip().lower()
        rows.append({'requirement': str(r['requirement']).strip()[:160],
                     'kind': kind if kind in ('skill', 'experience', 'location', 'other') else 'other',
                     'status': st, 'evidence': str(r.get('evidence') or '').strip()[:300]})
    e['must_have_check'] = rows
    for k in ('shortlist_reasons', 'reject_reasons'):
        v = e.get(k)
        e[k] = [str(x).strip() for x in v if str(x).strip()][:6] if isinstance(v, list) else []
    cs = _clean_text(e.get('client_summary'))          # no money figure / flattery for the client
    e['client_summary'] = cs[:1200]
    e['decision'] = decide(e)
    return e


# ══════════════════════════════════════════════════════════════════════════
#  DECISION — "can this profile go to the client?"
#  A fixed rule over the must-have check, not the model's mood, so two
#  similar profiles always get the same answer and the reason is visible.
# ══════════════════════════════════════════════════════════════════════════
SHORTLIST_MIN_SCORE = 70      # all must-haves met AND at least this score -> shortlisted
NO_CHECK_REJECT_BELOW = 45    # job without a usable must-have list: score decides


def decide(e):
    """{'status': shortlisted|rejected|hold, 'shareable': bool, 'rule': why, 'verify': [...]}"""
    rows = e.get('must_have_check') or []
    score = int(e.get('match_score') or 0)
    missing = [r['requirement'] for r in rows if r['status'] == 'NOT_MET']
    unclear = [r['requirement'] for r in rows if r['status'] == 'UNCLEAR']
    if missing:
        return {'status': 'rejected', 'shareable': False, 'verify': [],
                'rule': 'Must-have not met: ' + '; '.join(missing[:4])}
    if unclear:
        return {'status': 'hold', 'shareable': False, 'verify': unclear,
                'rule': 'Confirm on the call first: ' + '; '.join(unclear[:4])}
    if rows:
        if score >= SHORTLIST_MIN_SCORE:
            return {'status': 'shortlisted', 'shareable': True, 'verify': [],
                    'rule': f'All {len(rows)} must-haves met and match score {score}/100'}
        return {'status': 'hold', 'shareable': False, 'verify': [],
                'rule': f'All must-haves met, but the overall match is only {score}/100 '
                        f'(needs {SHORTLIST_MIN_SCORE}) — review the gaps before sharing'}
    # no must-have list could be built (empty job): fall back to the score
    if score >= SHORTLIST_MIN_SCORE:
        return {'status': 'shortlisted', 'shareable': True, 'verify': [],
                'rule': f'Match score {score}/100 (the job has no must-have list to check)'}
    if score < NO_CHECK_REJECT_BELOW:
        return {'status': 'rejected', 'shareable': False, 'verify': [],
                'rule': f'Match score {score}/100 (the job has no must-have list to check)'}
    return {'status': 'hold', 'shareable': False, 'verify': [],
            'rule': f'Match score {score}/100 — add must-have skills to the job for a clear decision'}


def call_view(ev):
    """The parts of an evaluation a call needs (script writer, call analysis):
    no client summary / reasons, so the prompt stays short."""
    keep = ('match_score', 'verdict', 'summary', 'strengths', 'gaps', 'to_verify', 'location_fit', 'must_have_check')
    return {k: ev[k] for k in keep if k in ev}


# ══════════════════════════════════════════════════════════════════════════
#  RECRUITER OVERRIDE of the decision (with a reason) — per candidate + job,
#  kept across re-generations. Additive table; the latest active row wins.
# ══════════════════════════════════════════════════════════════════════════
OVERRIDE_TABLE = 'hl_pitch_decision_override'
DECISIONS = ('shortlisted', 'rejected', 'hold')


def _ensure_override_schema(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {OVERRIDE_TABLE} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER DEFAULT 0, candidate_id INTEGER DEFAULT 0, mandate_id INTEGER DEFAULT 0,
        status TEXT DEFAULT '', reason TEXT DEFAULT '', ai_status TEXT DEFAULT '',
        by_id INTEGER DEFAULT 0, by_name TEXT DEFAULT '', created_at TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1)''')
    conn.execute(f'CREATE INDEX IF NOT EXISTS idx_pdo_cand ON {OVERRIDE_TABLE}(candidate_id, mandate_id, is_active)')


def _override(conn, cid, mid):
    try:
        r = conn.execute(f'SELECT status, reason, ai_status, by_name, created_at FROM {OVERRIDE_TABLE} '
                         'WHERE candidate_id=? AND mandate_id=? AND company_id=? AND is_active=1 '
                         'ORDER BY id DESC LIMIT 1', (cid, mid or 0, effective_company_id())).fetchone()
    except Exception:
        return None
    return dict(r) if r else None


# ══════════════════════════════════════════════════════════════════════════
#  STALENESS — only inputs that change the pitch count (not stage moves etc.)
# ══════════════════════════════════════════════════════════════════════════
_FP_CAND = ('name', 'designation', 'company', 'experience', 'location', 'preferred_location',
            'notice_period', 'qualification', 'specialization', 'key_skills', 'secondary_skills',
            'industry_background', 'career_summary', 'cv_path', 'mandate_id')
_FP_MAND = ('role', 'client', 'location', 'work_mode', 'exp_min', 'exp_max', 'experience',
            'must_have_skills', 'good_to_have_skills', 'jd', 'sop_text')


def _fingerprint(conn, c, m):
    import hashlib
    parts = [str(c[k] if k in c.keys() else '') for k in _FP_CAND]
    if m is not None:
        parts += [str(m[k] if k in m.keys() else '') for k in _FP_MAND]
    for w in conn.execute('SELECT company, designation, start_date, end_date, is_current, description '
                          'FROM work_history WHERE candidate_id=? ORDER BY sort_order, id', (c['id'],)).fetchall():
        parts.append('|'.join(str(x or '') for x in w))
    return hashlib.sha1('\x1f'.join(parts).encode('utf-8', 'ignore')).hexdigest()


# ══════════════════════════════════════════════════════════════════════════
#  ACCESS
# ══════════════════════════════════════════════════════════════════════════
def _load(conn, cid):
    c = conn.execute('SELECT * FROM candidates WHERE id=? AND owner_id=?',
                     (cid, effective_company_id())).fetchone()
    if not c:
        return None, None
    m = conn.execute('SELECT * FROM mandates WHERE id=? AND owner_id=?',
                     (c['mandate_id'], effective_company_id())).fetchone()
    return c, m


def _row_out(r, fp_now):
    try:
        ev = json.loads(r['evaluation'] or '{}')
        pt = json.loads(r['pitch'] or '{}')
    except Exception:
        return None
    stale = bool(r['fingerprint']) and r['fingerprint'] != fp_now
    # 'decision' exists from Oct 2026 on; an older evaluation shows a re-generate nudge instead
    return {'evaluation': ev, 'pitch': pt, 'lang': r['lang'], 'at': r['created_at'],
            'by': r['created_by_name'], 'stale': stale, 'has_decision': bool(ev.get('decision'))}


# ══════════════════════════════════════════════════════════════════════════
#  ROUTES
#  Every failure answers JSON naming the STEP that failed — never Flask's HTML
#  500 page — so a production problem can be read straight off the screen.
# ══════════════════════════════════════════════════════════════════════════
def _internal(step, e):
    import traceback
    traceback.print_exc()
    print(f'[pitch] failed at step={step}: {type(e).__name__}: {e}')
    return jsonify({'error': f'Internal error at step "{step}": {type(e).__name__}: {e}',
                    'step': step}), 500


@bp.route('/candidates/<int:cid>/pitch', methods=['GET'])
@login_required
def get_pitch(cid):
    step = 'load'
    try:
        lang = request.args.get('lang', 'en')
        lang = lang if lang in LANGS else 'en'
        conn = get_db()
        try:
            c, m = _load(conn, cid)
            if not c:
                return jsonify({'error': 'Candidate not found'}), 404
            step = 'read-saved'
            _ensure_schema(conn)
            rows = conn.execute(f'SELECT * FROM {PITCH_TABLE} WHERE candidate_id=? AND mandate_id=? '
                                'AND owner_id=? ORDER BY id DESC',
                                (cid, c['mandate_id'], effective_company_id())).fetchall()
            step = 'fingerprint'
            fp_now = _fingerprint(conn, c, m) if rows else ''
            step = 'override'
            ovr = _override(conn, cid, c['mandate_id'])
            conn.commit()
        finally:
            conn.close()
        have = sorted({r['lang'] for r in rows})
        for r in rows:
            if r['lang'] == lang:
                out = _row_out(r, fp_now)
                if out:
                    return jsonify({'ok': True, 'cached': True, 'available': have, 'override': ovr, **out})
        return jsonify({'ok': True, 'cached': False, 'available': have,
                        'has_mandate': bool(m), 'has_jd': bool(m and (m['jd'] or '').strip())})
    except Exception as e:
        return _internal(step, e)


@bp.route('/candidates/<int:cid>/pitch', methods=['POST'])
@login_required
def generate_pitch(cid):
    step = 'setup'
    try:
        core = _core()
        d = request.get_json(silent=True) or {}
        lang = d.get('lang', 'en')
        lang = lang if lang in LANGS else 'en'
        ds_key = core.get_setting('deepseek_api_key')
        if not ds_key:
            return jsonify({'error': 'DeepSeek API key not set. Add it in Settings.'}), 400

        step = 'load-candidate'
        conn = get_db()
        try:
            c, m = _load(conn, cid)
            if not c:
                return jsonify({'error': 'Candidate not found'}), 404
            if not m or (m['status'] or '') == 'central':
                return jsonify({'error': 'This candidate is not on a job mandate. Move them to a mandate first — the pitch needs a JD.'}), 400
            step = 'build-job-text'
            role_txt = _role_block(m)
            step = 'build-candidate-text'
            cand_txt = _candidate_block(conn, c)
            step = 'fingerprint'
            fp = _fingerprint(conn, c, m)
            mandate_id = c['mandate_id']
        finally:
            conn.close()

        u = current_user() or {}
        recruiter = (u.get('display_name') or u.get('username') or '').strip() or 'the recruiter'
        agency = (core.get_setting('company_name', '') or u.get('company_name') or '').strip() or 'our agency'

        try:
            step = 'ai-evaluate'
            ev, _ = _ask_json(ds_key, EVAL_PROMPT,
                              'JOB\n' + role_txt + '\n\nCANDIDATE\n' + cand_txt,
                              0.2, 1800, 'pitch-evaluate')
            ev = _normalise_eval(ev)

            step = 'ai-write-pitch'
            pitch_user = (f"Recruiter name: {recruiter}\nAgency name: {agency}\n\n"
                          f"JOB\n{role_txt}\n\nCANDIDATE\n{cand_txt}\n\n"
                          f"EVALUATION (for you only — never read it out)\n{json.dumps(call_view(ev), ensure_ascii=False)}")
            system = _pitch_prompt(lang)
            pt, raw = _ask_json(ds_key, system, pitch_user, 0.6, 2600, 'pitch-write')
            step = 'check-rules'
            bad = _violations(pt)
            if bad:
                step = 'ai-fix-pitch'
                pt2, _ = _ask_json(ds_key, system, pitch_user, 0.4, 2600, 'pitch-write-fix', extra_messages=[
                    {'role': 'assistant', 'content': raw},
                    {'role': 'user', 'content': 'Rewrite it. These rules were broken: ' + '; '.join(bad)
                     + '. Fix all of them. Same JSON shape.'}])
                if isinstance(pt2, dict) and pt2.get('script'):
                    pt = pt2
            step = 'scrub'
            pt = _scrub(pt)
            if len(pt.get('script') or '') < 300 or not pt.get('exposure_questions'):
                raise PitchError('AI returned an incomplete pitch. Please try again.')
        except PitchError as e:
            print(f'[pitch] step={step}: {e}')
            return jsonify({'error': str(e), 'step': step}), e.code

        step = 'save'
        at = ts()
        conn = get_db()
        try:
            _ensure_schema(conn)
            conn.execute(f'INSERT INTO {PITCH_TABLE} (owner_id,candidate_id,mandate_id,lang,evaluation,pitch,'
                         'model,created_by,created_by_name,created_at,fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                         (effective_company_id(), cid, mandate_id, lang,
                          json.dumps(ev, ensure_ascii=False), json.dumps(pt, ensure_ascii=False),
                          MODEL, real_user_id() or 0, recruiter, at, fp))
            # keep only the latest per (candidate, mandate, language)
            conn.execute(f'DELETE FROM {PITCH_TABLE} WHERE candidate_id=? AND mandate_id=? AND lang=? '
                         f'AND id NOT IN (SELECT MAX(id) FROM {PITCH_TABLE} WHERE candidate_id=? '
                         'AND mandate_id=? AND lang=?)', (cid, mandate_id, lang, cid, mandate_id, lang))
            conn.commit()
        finally:
            conn.close()
        conn = get_db()
        try:
            ovr = _override(conn, cid, mandate_id)
        finally:
            conn.close()
        return jsonify({'ok': True, 'cached': True, 'evaluation': ev, 'pitch': pt, 'lang': lang,
                        'at': at, 'by': recruiter, 'stale': False, 'has_decision': True, 'override': ovr})
    except Exception as e:
        return _internal(step, e)


@bp.route('/candidates/<int:cid>/pitch/decision', methods=['POST', 'DELETE'])
@login_required
def set_pitch_decision(cid):
    """Recruiter overrides the AI decision (with a reason), or DELETE goes back to the AI's."""
    conn = get_db()
    try:
        c, m = _load(conn, cid)
        if not c:
            return jsonify({'error': 'Candidate not found'}), 404
        _ensure_override_schema(conn)
        u = current_user() or {}
        who = (u.get('display_name') or u.get('username') or '').strip()
        now = ts()
        mid = c['mandate_id'] or 0
        conn.execute(f'UPDATE {OVERRIDE_TABLE} SET is_active=0 WHERE candidate_id=? AND mandate_id=? '
                     'AND company_id=? AND is_active=1', (cid, mid, effective_company_id()))
        if request.method == 'DELETE':
            note = 'Pitch decision: back to the AI decision'
            ovr = None
        else:
            d = request.get_json(silent=True) or {}
            status = str(d.get('status') or '').strip().lower()
            reason = str(d.get('reason') or '').strip()
            if status not in DECISIONS:
                return jsonify({'error': 'Choose Shortlisted, Rejected or Hold.'}), 400
            if len(reason) < 5:
                return jsonify({'error': 'Write a short reason for changing the decision.'}), 400
            ai = ''
            try:
                r = conn.execute(f'SELECT evaluation FROM {PITCH_TABLE} WHERE candidate_id=? AND mandate_id=? '
                                 'AND owner_id=? ORDER BY id DESC LIMIT 1', (cid, mid, effective_company_id())).fetchone()
                ai = ((json.loads(r['evaluation']) or {}).get('decision') or {}).get('status', '') if r else ''
            except Exception:
                ai = ''
            conn.execute(f'INSERT INTO {OVERRIDE_TABLE} (company_id, candidate_id, mandate_id, status, reason, ai_status, '
                         'by_id, by_name, created_at, is_active) VALUES (?,?,?,?,?,?,?,?,?,1)',
                         (effective_company_id(), cid, mid, status, reason[:500], ai, real_user_id() or 0, who, now))
            note = f'Pitch decision set to {status.title()} by {who}: {reason[:300]}'
            ovr = {'status': status, 'reason': reason[:500], 'ai_status': ai, 'by_name': who, 'created_at': now}
        try:
            conn.execute('INSERT INTO candidate_events (candidate_id, event_type, detail, created_at) VALUES (?,?,?,?)',
                         (cid, 'note', note, now))
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()
    try:
        _core().log_activity('pitch.decision', note, entity_type='candidate', entity_id=cid)
    except Exception:
        pass
    return jsonify({'ok': True, 'override': ovr})



@bp.route('/pitch/diagnose', methods=['GET'])
@login_required
def pitch_diagnose():
    """Admin-only health check: shows the live DB schema of the tables this
    feature touches and dry-runs the input build for one candidate (?cid=),
    WITHOUT calling the AI. Open in the browser while logged in as admin."""
    from modules.shared import is_company_admin
    if not is_company_admin():
        return jsonify({'error': 'Admin only'}), 403
    out = {'ok': True}
    conn = get_db()
    try:
        for t in ('candidate_pitches', PITCH_TABLE, 'notifications'):
            cols = conn.execute(f'PRAGMA table_info({t})').fetchall()
            out[f'table:{t}'] = [f"{r[1]} {r[2]}{' NOT NULL' if r[3] else ''}" for r in cols] or 'does not exist'
        out['deepseek_key_set'] = bool(_core().get_setting('deepseek_api_key'))
        cid = request.args.get('cid', type=int)
        if cid:
            try:
                c, m = _load(conn, cid)
                if not c:
                    out['dry_run'] = 'candidate not found in this workspace'
                else:
                    rt = _role_block(m) if m else ''
                    ct = _candidate_block(conn, c)
                    out['dry_run'] = {'mandate_found': bool(m), 'job_text_chars': len(rt),
                                      'candidate_text_chars': len(ct),
                                      'fingerprint': _fingerprint(conn, c, m)[:12]}
            except Exception as e:
                import traceback
                out['dry_run'] = f'FAILED: {type(e).__name__}: {e}'
                out['trace'] = traceback.format_exc()[-1500:]
    finally:
        conn.close()
    return jsonify(out)
