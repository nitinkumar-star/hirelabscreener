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
  "location_fit": "<one short line: candidate location vs job location, relocation needed or not, or unknown>"
}

RULES
- Every strength/gap must name the concrete skill, tool, domain or responsibility. No generic lines.
- Only use facts present in the inputs. If something is unknown, put it in gaps or to_verify, never assume.
- Must-have skills weigh far more than good-to-have.
- 3-6 items in strengths, 2-6 in gaps, 2-5 in to_verify.
- Never mention salary, CTC or budget."""


def _pitch_prompt(lang):
    if lang == 'hinglish':
        lang_rule = ("LANGUAGE: Hinglish — the natural Hindi-English mix Indian recruiters speak on calls, "
                     "written in ROMAN script only (no Devanagari). Keep technical terms, the job title and "
                     "skill names in English. Example tone: \"Aapka current location kya hai, aur Pune "
                     "shift hone mein aap comfortable rahenge?\"")
    else:
        lang_rule = ("LANGUAGE: Simple, natural spoken Indian English — short sentences, polite, "
                     "conversational. Not formal written English.")
    return f"""You write the PHONE CALL SCRIPT an Indian agency recruiter reads when calling a candidate
about one job. It must sound like a real person talking — warm, direct, unhurried — never like a
sales brochure or a robot.

{lang_rule}

You receive: the recruiter and agency names, the job details, the candidate's profile, and a
screening EVALUATION of this candidate against the job (strengths, gaps, claims to verify).

FOLLOW THIS FLOW EXACTLY (each step short — 1 to 3 spoken sentences):
1. intro     — Greet the candidate by first name, introduce the recruiter by name and the agency,
               ask if it is a good time for a few minutes.
2. location  — Say the role is based in <job location> (and work mode if known) and ASK the
               candidate about their comfort with that location / relocation / commute. If the
               evaluation says they are elsewhere, ask about relocation naturally.
3. client    — Describe the client company in ONE or TWO short lines. Use only well-known, true facts
               for recognisable companies; for anything you are not sure about, stay neutral
               (e.g. "an established company in the solar EPC space"). Never invent size, funding,
               awards or parent companies.
4. title     — State the position title clearly.
5. role      — The role in brief: 2-3 lines on what the person will actually own and work on,
               taken from the JD. No hype.
6. questions — 4 to 6 skill questions the recruiter asks to judge fit. Build them from the
               evaluation: probe every important GAP and every claim TO VERIFY, plus the must-have
               skills. Questions must be specific and answerable on a phone call (projects, tools,
               scale, responsibilities, numbers), not "tell me about yourself". For each question
               also write what a GOOD answer sounds like, so a junior recruiter can judge it.

HARD RULES
- NEVER state or hint at CTC, salary, budget, package or any money figure — you do not know it and
  must not guess. Instead write "ctc_response": the diplomatic line the recruiter uses IF the
  candidate asks about budget (e.g. the client decides based on the candidate's current package,
  experience and interview performance; first understand their current and expected, then discuss).
  It must contain NO number.
- NO flattery. Do not say the profile is impressive/great/strong, do not say "I came across your
  profile", no "perfect fit", no "exciting/amazing opportunity". Just be clear and respectful.
- Do not tell the candidate their score, gaps or weaknesses.
- Use the candidate's real first name. Do not use placeholders except the recruiter/agency names
  given to you.

Return ONLY a JSON object, no prose:
{{
  "intro": "...", "location": "...", "client": "...", "title": "...", "role": "...",
  "questions": [{{"q": "...", "checks": "<which gap/claim/skill this tests>", "good_answer": "..."}}],
  "ctc_response": "...",
  "closing": "<one line to close: next step, e.g. ask for updated CV / best time for a detailed discussion>"
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
_TEXT_KEYS = ('intro', 'location', 'client', 'title', 'role', 'ctc_response', 'closing')


def _all_texts(p):
    out = [(k, str(p.get(k) or '')) for k in _TEXT_KEYS]
    for i, q in enumerate(p.get('questions') or []):
        if isinstance(q, dict):
            out.append((f'q{i}', str(q.get('q') or '')))
    return out


def _violations(p):
    bad = []
    for k, t in _all_texts(p):
        if _MONEY_FIGURE.search(t):
            bad.append(f'"{k}" contains a money figure')
        if _FLATTERY.search(t):
            bad.append(f'"{k}" contains flattery ("{_FLATTERY.search(t).group(0)}")')
    return bad


def _scrub(p):
    """Last line of defence: drop any sentence still carrying money / flattery."""
    def clean(t):
        parts = re.split(r'(?<=[.!?])\s+', str(t or ''))
        return ' '.join(s for s in parts if not _MONEY_FIGURE.search(s) and not _FLATTERY.search(s)).strip()
    for k in _TEXT_KEYS:
        if k in p:
            p[k] = clean(p[k])
    qs = []
    for q in p.get('questions') or []:
        if isinstance(q, dict) and clean(q.get('q')):
            q['q'] = clean(q.get('q'))
            qs.append(q)
    p['questions'] = qs
    if not p.get('ctc_response'):
        p['ctc_response'] = ('The final number is decided by the client based on your current package, '
                             'experience and how the interviews go. Could you share your current and '
                             'expected so I can position your profile correctly?')
    return p


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
    return e


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
    return {'evaluation': ev, 'pitch': pt, 'lang': r['lang'], 'at': r['created_at'],
            'by': r['created_by_name'], 'stale': stale}


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
            conn.commit()
        finally:
            conn.close()
        have = sorted({r['lang'] for r in rows})
        for r in rows:
            if r['lang'] == lang:
                out = _row_out(r, fp_now)
                if out:
                    return jsonify({'ok': True, 'cached': True, 'available': have, **out})
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
                              0.2, 1000, 'pitch-evaluate')
            ev = _normalise_eval(ev)

            step = 'ai-write-pitch'
            pitch_user = (f"Recruiter name: {recruiter}\nAgency name: {agency}\n\n"
                          f"JOB\n{role_txt}\n\nCANDIDATE\n{cand_txt}\n\n"
                          f"EVALUATION (for you only — never read it out)\n{json.dumps(ev, ensure_ascii=False)}")
            system = _pitch_prompt(lang)
            pt, raw = _ask_json(ds_key, system, pitch_user, 0.6, 1600, 'pitch-write')
            step = 'check-rules'
            bad = _violations(pt)
            if bad:
                step = 'ai-fix-pitch'
                pt2, _ = _ask_json(ds_key, system, pitch_user, 0.4, 1600, 'pitch-write-fix', extra_messages=[
                    {'role': 'assistant', 'content': raw},
                    {'role': 'user', 'content': 'Rewrite it. These rules were broken: ' + '; '.join(bad)
                     + '. Remove every money figure and every flattering phrase. Same JSON shape.'}])
                if isinstance(pt2, dict) and pt2.get('intro'):
                    pt = pt2
            step = 'scrub'
            pt = _scrub(pt)
            if not pt.get('intro') or not pt.get('questions'):
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
        return jsonify({'ok': True, 'cached': True, 'evaluation': ev, 'pitch': pt, 'lang': lang,
                        'at': at, 'by': recruiter, 'stale': False})
    except Exception as e:
        return _internal(step, e)


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
