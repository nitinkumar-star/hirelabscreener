"""
Job Breakdown + candidate explanation.

STEP A — Job Breakdown (one per mandate, saved)
  DeepSeek reads the mandate (JD, boolean, must/good skills, hiring intent,
  SOP, experience, CTC) and splits what the job needs into:
    core       deal-breakers: without these the person cannot do the job
    important  strongly preferred; a gap can be trained or offset
    nice       desirable; the job can be done without them
    limits     experience range, CTC ceiling, locations (mandate fields win)
    not_this   look-alike profiles to reject ("rooftop sales is not plant O&M")
  Each item carries "equivalents" (other names/tools that count the same).
  The recruiter sees it on the mandate's "Job Breakdown" tab and can edit it.
  It is regenerated automatically when the job text changes — unless the
  recruiter edited it, in which case it is kept and flagged "JD changed".

STEP B — Candidate explanation (Chrome extension, detailed profile page)
  POST /api/extension/explain compares one candidate with the breakdown:
  per core/important item: found in CV (with quote) / profile only / missing;
  nice-to-haves found; experience/CTC/location checks; risks; questions for
  the call; and a fit verdict. This call REPLACES the old intent-judge call
  (the extension asks score-match to skip it), so the blend below mirrors
  server.py's judge formula exactly, plus one new rule:
    any core requirement missing  ->  the score cannot reach "Strong Match".

The same breakdown also feeds the Naukri search Screener (naukri_screen.py).

Additive only: one new table. Nothing about a candidate is stored.
"""

import re
import json
import time
import hashlib
import threading
from functools import wraps
from flask import Blueprint, request, jsonify, session

from modules.shared import get_db, ts, effective_company_id, real_user_id, login_required, _core
from modules import register_migration

bp = Blueprint('job_profile', __name__)

LIMITS = {'core': 6, 'important': 8, 'nice': 10, 'not_this': 6}
EXPLAIN_LIMIT, WINDOW = 150, 600
_rl, _rl_lock = {}, threading.Lock()
_gen_locks, _gen_guard = {}, threading.Lock()


@register_migration
def _migrate_job_profile(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS mandate_job_profile (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL DEFAULT 0,
        mandate_id INTEGER NOT NULL,
        profile TEXT DEFAULT '{}',
        source_hash TEXT DEFAULT '',
        status TEXT DEFAULT 'ai',
        generated_at TEXT DEFAULT '',
        edited_by INTEGER DEFAULT 0,
        edited_at TEXT DEFAULT '',
        UNIQUE(company_id, mandate_id)
    )''')
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  PURE HELPERS (unit tested)
# ══════════════════════════════════════════════════════════════════════════
def _s(v, n):
    return re.sub(r'\s+', ' ', str(v or '')).strip()[:n]


def _num(v):
    try:
        f = float(v or 0)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _items(raw, n, with_why=True):
    out, seen = [], set()
    for it in raw if isinstance(raw, list) else []:
        if isinstance(it, str):
            it = {'skill': it}
        if not isinstance(it, dict):
            continue
        skill = _s(it.get('skill') or it.get('name'), 90)
        if not skill or skill.lower() in seen:
            continue
        seen.add(skill.lower())
        eq = it.get('equivalents') or []
        if isinstance(eq, str):
            eq = [e for e in re.split(r'[,;/]', eq)]
        d = {'skill': skill,
             'equivalents': [x for x in (_s(e, 60) for e in eq) if x][:8]}
        if with_why:
            d['why'] = _s(it.get('why'), 220)
        out.append(d)
        if len(out) >= n:
            break
    return out


def clean_profile(p, mandate=None):
    """A well-formed breakdown. Mandate fields (experience, CTC) override the
    AI's limits, because the recruiter typed those numbers deliberately."""
    p = p if isinstance(p, dict) else {}
    lim = p.get('limits') if isinstance(p.get('limits'), dict) else {}
    locs = lim.get('locations') or []
    if isinstance(locs, str):
        locs = re.split(r'[,;/]', locs)
    out = {
        'summary': _s(p.get('summary'), 700),
        'core': _items(p.get('core'), LIMITS['core']),
        'important': _items(p.get('important'), LIMITS['important']),
        'nice': _items(p.get('nice'), LIMITS['nice'], with_why=False),
        'limits': {'exp_min': _num(lim.get('exp_min')), 'exp_max': _num(lim.get('exp_max')),
                   'ctc_max': _num(lim.get('ctc_max')),
                   'locations': [x for x in (_s(l, 60) for l in locs) if x][:8],
                   'notes': _s(lim.get('notes'), 300)},
        'not_this': [x for x in (_s(t, 180) for t in (p.get('not_this') or [])) if x][:LIMITS['not_this']],
    }
    # an item may live in one tier only — the stricter one wins
    seen = set()
    for tier in ('core', 'important', 'nice'):
        keep = []
        for it in out[tier]:
            if it['skill'].lower() not in seen:
                seen.add(it['skill'].lower())
                keep.append(it)
        out[tier] = keep
    if mandate is not None:
        def g(k):
            try:
                return mandate[k]
            except (IndexError, KeyError):
                return None
        if _num(g('exp_min')):
            out['limits']['exp_min'] = _num(g('exp_min'))
        if _num(g('exp_max')):
            out['limits']['exp_max'] = _num(g('exp_max'))
        if _num(g('ctc_max')):
            out['limits']['ctc_max'] = _num(g('ctc_max'))
        loc = _s(g('location'), 120)
        if loc and not out['limits']['locations']:
            out['limits']['locations'] = [x for x in (_s(l, 60) for l in re.split(r'[,;/]', loc)) if x][:8]
    return out


def profile_text(p):
    """The breakdown as compact text for other prompts (Screener, explain)."""
    if not p:
        return ''
    lines = []
    if p.get('summary'):
        lines.append('What the job really is: ' + p['summary'])

    def tier(name, items):
        if items:
            lines.append(name + ':')
            for it in items:
                eq = (' (also counts: ' + ', '.join(it['equivalents']) + ')') if it.get('equivalents') else ''
                lines.append('  - ' + it['skill'] + eq)
    tier('CORE (deal-breakers)', p.get('core'))
    tier('IMPORTANT (strongly preferred)', p.get('important'))
    tier('NICE-TO-HAVE (can do without)', p.get('nice'))
    lim = p.get('limits') or {}
    bits = []
    if lim.get('exp_min') or lim.get('exp_max'):
        bits.append('experience %s-%s yrs' % (('%g' % lim['exp_min']) if lim.get('exp_min') else '?',
                                              ('%g' % lim['exp_max']) if lim.get('exp_max') else '?'))
    if lim.get('ctc_max'):
        bits.append('CTC up to %g LPA' % lim['ctc_max'])
    if lim.get('locations'):
        bits.append('location ' + ', '.join(lim['locations']))
    if bits:
        lines.append('LIMITS: ' + '; '.join(bits) + (('. ' + lim['notes']) if lim.get('notes') else ''))
    if p.get('not_this'):
        lines.append('NOT THIS (reject look-alikes): ' + ' | '.join(p['not_this']))
    return '\n'.join(lines)


def _tokens(t):
    return [w for w in re.findall(r'[a-z0-9&+#.]+', str(t or '').lower()) if len(w) > 1]


def quote_found(quote, text):
    """Is the AI's evidence quote really in this text? (60% of its words)"""
    q = _tokens(quote)
    if len(q) < 2:
        return False
    hay = set(_tokens(text))
    return sum(1 for w in q if w in hay) / len(q) >= 0.6


STATUSES = ('cv', 'profile', 'missing')
FITS = {'strong': 90, 'partial': 60, 'weak': 35, 'mismatch': 10}


def normalize_explanation(raw, prof, resume_text, profile_text_):
    raw = raw if isinstance(raw, dict) else {}
    by_id = {}
    for it in (raw.get('requirements') or raw.get('items') or []):
        if isinstance(it, dict) and it.get('id'):
            by_id[str(it['id']).strip().upper()] = it

    def rows(prefix, items):
        out = []
        for i, it in enumerate(items):
            r = by_id.get('%s%d' % (prefix, i + 1), {})
            st = str(r.get('status') or '').lower().strip()
            st = st if st in STATUSES else 'unknown'
            ev = _s(r.get('evidence'), 220)
            verified = None
            if st == 'cv':
                if ev and quote_found(ev, resume_text):
                    verified = True
                elif ev and quote_found(ev, profile_text_):
                    st, verified = 'profile', True       # it is real, but not in the CV
                else:
                    verified = False                      # AI said CV; the quote is not there
            elif st == 'profile' and ev:
                verified = quote_found(ev, profile_text_) or quote_found(ev, resume_text)
            out.append({'skill': it['skill'], 'status': st, 'evidence': ev, 'verified': verified})
        return out

    core = rows('C', prof.get('core') or [])
    imp = rows('I', prof.get('important') or [])
    nice_names = {n['skill'].lower(): n['skill'] for n in prof.get('nice') or []}
    nice_found = []
    for n in raw.get('nice_found') or []:
        k = _s(n, 90).lower()
        if k in nice_names and nice_names[k] not in nice_found:
            nice_found.append(nice_names[k])

    def check(key, ok_vals):
        c = (raw.get('checks') or {}).get(key) if isinstance(raw.get('checks'), dict) else None
        c = c if isinstance(c, dict) else {}
        st = str(c.get('status') or 'unknown').lower()
        return {'value': _s(c.get('value'), 60), 'status': st if st in ok_vals else 'unknown',
                'note': _s(c.get('note'), 160)}

    fit = str(raw.get('fit') or '').lower().strip()
    fit = fit if fit in FITS else 'partial'
    try:
        conf = max(0, min(100, int(raw.get('confidence', 70))))
    except (TypeError, ValueError):
        conf = 70
    return {
        'summary': _s(raw.get('summary'), 500),
        'core': core, 'important': imp, 'nice_found': nice_found,
        'checks': {'experience': check('experience', ('ok', 'low', 'high', 'unknown')),
                   'ctc': check('ctc', ('ok', 'over', 'unknown')),
                   'location': check('location', ('ok', 'relocation', 'unknown'))},
        'risks': [x for x in (_s(r, 200) for r in (raw.get('risks') or [])) if x][:3],
        'questions': [x for x in (_s(q, 200) for q in (raw.get('questions') or [])) if x][:3],
        'fit': fit, 'confidence': conf,
        'core_missing': [r['skill'] for r in core if r['status'] == 'missing'],
    }


def verdict_for(score):
    if score >= 75:
        return 'Strong Match', '#0F8A6B'
    if score >= 55:
        return 'Good Fit', '#2A73C5'
    if score >= 35:
        return 'Partial Match', '#D97706'
    return 'Not Suitable', '#A32D2D'


def blend(base, fit, conf, core_missing):
    """server.py's judge blend (65/35, confident mismatch capped at 34), plus:
    a missing core requirement caps the score below Strong Match (74)."""
    b = FITS.get(fit, 60)
    judge = round(b * 0.7 + conf * 0.3 * (b / 90.0), 1)
    final = round(float(base) * 0.65 + judge * 0.35, 1)
    caps = []
    if fit == 'mismatch' and conf >= 60 and final > 34.0:
        final = 34.0
        caps.append('AI judged this a mismatch for the role')
    if core_missing and final > 74.0:
        final = 74.0
        caps.append('Core requirement missing: ' + ', '.join(core_missing[:3]))
    v, c = verdict_for(final)
    return {'score': final, 'judge_score': judge, 'verdict': v, 'verdict_color': c, 'caps': caps}


# ══════════════════════════════════════════════════════════════════════════
#  DeepSeek
# ══════════════════════════════════════════════════════════════════════════
GEN_PROMPT = """You are a senior technical recruiter for Solar, Electrical, Automation and Renewable-energy roles in India.
Read the job below and decide what REALLY matters for doing it. Reply with ONLY a JSON object:
{"summary": "2-3 sentences: what this person will actually do day to day and at what level",
 "core": [{"skill": "...", "why": "one line", "equivalents": ["other names/tools that count the same"]}],
 "important": [{"skill": "...", "why": "...", "equivalents": [...]}],
 "nice": [{"skill": "...", "equivalents": [...]}],
 "limits": {"locations": ["..."], "notes": "anything else that limits who fits (shift, travel, licence)"},
 "not_this": ["look-alike profiles that should be rejected, e.g. 'Rooftop solar sales is not utility-scale plant O&M'"]}
Rules:
- core = without it the person cannot do this job from day one. Usually 3-5 items, never more than 6.
- Skills the recruiter entered as MUST-HAVE and every AND-line of the boolean are core, unless obviously generic (e.g. MS Office).
- important = strongly preferred; a gap can be trained or offset by other strengths.
- nice = desirable; the job can be done without them.
- Write each item as a capability a CV would show ("Utility-scale solar plant O&M", "Siemens S7 PLC programming"), not a single vague word.
- Do NOT invent requirements that are not in the job text. If the job text is thin, keep the lists short.
- English only."""

EXPLAIN_PROMPT = """You are a senior recruiter checking ONE candidate against an agreed job breakdown.
For every requirement id (C1.. = core, I1.. = important) decide:
  "cv"      the RESUME clearly shows it (accept the listed equivalents)
  "profile" only the Naukri profile fields / skill list claim it
  "missing" not shown anywhere
and give "evidence": a short EXACT quote (max 20 words) copied from the resume or profile, or "" when missing.
Reply with ONLY a JSON object:
{"summary": "max 2 sentences, plain English, the decisive facts",
 "requirements": [{"id": "C1", "status": "cv|profile|missing", "evidence": "..."}],
 "nice_found": ["exact nice-to-have names that are shown"],
 "checks": {"experience": {"value": "e.g. 8 yrs", "status": "ok|low|high|unknown", "note": "..."},
            "ctc": {"value": "e.g. 12.5 LPA", "status": "ok|over|unknown", "note": "..."},
            "location": {"value": "...", "status": "ok|relocation|unknown", "note": "..."}},
 "fit": "strong|partial|weak|mismatch", "confidence": 0-100,
 "risks": ["up to 3 short risks"], "questions": ["up to 3 questions to ask on the screening call to close the gaps"]}
"mismatch" = the person is one of the NOT THIS look-alikes, or works in the wrong domain/function altogether.
Judge what the person actually DID (built, ran, maintained) — not just keywords. Never invent facts. English only."""


def _key(core):
    try:
        return (core.get_setting('deepseek_api_key') or '').strip()
    except Exception:
        return ''


def _parse(core, raw):
    try:
        return json.loads(raw)
    except Exception:
        pass
    s, e = raw.find('{'), raw.rfind('}')
    if 0 <= s < e:
        try:
            return json.loads(raw[s:e + 1])
        except Exception:
            pass
    return None


def _ask(core, key, system, user, max_tokens, endpoint, timeout):
    payload = {'model': 'deepseek-chat', 'temperature': 0, 'max_tokens': max_tokens,
               'response_format': {'type': 'json_object'},
               'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
    resp = core.call_deepseek(key, payload, timeout=timeout, endpoint=endpoint)
    if resp.status_code != 200:
        raise RuntimeError('DeepSeek returned HTTP %s' % resp.status_code)
    j = _parse(core, resp.json()['choices'][0]['message']['content'])
    if not isinstance(j, dict):
        raise ValueError('DeepSeek reply was not a JSON object')
    return j


# ══════════════════════════════════════════════════════════════════════════
#  Breakdown storage
# ══════════════════════════════════════════════════════════════════════════
def source_text(core, m):
    from modules.naukri_screen import job_brief
    return job_brief(core, m)


def has_job_content(core, m):
    def g(k):
        try:
            return str(m[k] or '').strip()
        except (IndexError, KeyError):
            return ''
    return any(g(k) not in ('', '[]') for k in ('jd', 'boolean_query', 'must_have_skills', 'search_intent'))


def _row(conn, mid):
    return conn.execute('SELECT * FROM mandate_job_profile WHERE company_id=? AND mandate_id=?',
                        (effective_company_id(), mid)).fetchone()


def _pack(r, cur_hash):
    try:
        p = json.loads(r['profile'] or '{}')
    except Exception:
        p = {}
    return {'profile': p, 'status': r['status'] or 'ai',
            'stale': bool(cur_hash and r['source_hash'] and r['source_hash'] != cur_hash),
            'generated_at': r['generated_at'] or '', 'edited_at': r['edited_at'] or ''}


def _save(conn, mid, prof, h, status):
    now = ts()
    conn.execute('''INSERT INTO mandate_job_profile (company_id, mandate_id, profile, source_hash, status,
                        generated_at, edited_by, edited_at) VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(company_id, mandate_id) DO UPDATE SET profile=excluded.profile,
                        source_hash=excluded.source_hash, status=excluded.status,
                        generated_at=CASE WHEN excluded.status='ai' THEN excluded.generated_at ELSE mandate_job_profile.generated_at END,
                        edited_by=excluded.edited_by, edited_at=excluded.edited_at''',
                 (effective_company_id(), mid, json.dumps(prof), h, status,
                  now if status == 'ai' else '', int(real_user_id() or 0) if status == 'edited' else 0,
                  now if status == 'edited' else ''))
    conn.commit()


def generate(conn, core, m):
    key = _key(core)
    if not key:
        raise RuntimeError('DeepSeek is not set up. Add the DeepSeek API key in ATS Settings.')
    src = source_text(core, m)
    raw = _ask(core, key, GEN_PROMPT, 'THE JOB:\n' + src, 1400, 'job-breakdown', 60)
    prof = clean_profile(raw, m)
    if not prof['core'] and not prof['important']:
        raise ValueError('The AI returned an empty breakdown.')
    _save(conn, m['id'], prof, hashlib.sha1(src.encode('utf-8')).hexdigest(), 'ai')
    return prof


def ensure(conn, core, m, create=True):
    """The current breakdown for a mandate, regenerating an AI one whose job
    text changed. An edited breakdown is never overwritten automatically.
    Returns the packed dict, or None when there is nothing (and create=False
    or the mandate has no job content)."""
    h = hashlib.sha1(source_text(core, m).encode('utf-8')).hexdigest()
    r = _row(conn, m['id'])
    if r and (r['status'] == 'edited' or r['source_hash'] == h):
        return _pack(r, h)
    if not create or not has_job_content(core, m):
        return _pack(r, h) if r else None
    # one generation per mandate at a time; a second caller waits and reuses it
    lk_key = (effective_company_id(), m['id'])
    with _gen_guard:
        lk = _gen_locks.setdefault(lk_key, threading.Lock())
    with lk:
        r = _row(conn, m['id'])
        if r and (r['status'] == 'edited' or r['source_hash'] == h):
            return _pack(r, h)
        generate(conn, core, m)
        return _pack(_row(conn, m['id']), h)


def load_for_prompt(conn, core, m):
    """Breakdown text for another prompt; '' when unavailable. Never raises."""
    try:
        got = ensure(conn, core, m, create=True)
        return profile_text(got['profile']) if got else ''
    except Exception as e:
        print('[job-profile] breakdown unavailable for mandate %s: %s' % (m['id'], e))
        return ''


# ══════════════════════════════════════════════════════════════════════════
#  Routes — ATS
# ══════════════════════════════════════════════════════════════════════════
def _mandate(conn, mid):
    m = conn.execute('SELECT * FROM mandates WHERE id=? AND owner_id=?', (mid, effective_company_id())).fetchone()
    if not m:
        return None
    try:
        from modules.access import can_see_mandate
        if not can_see_mandate(conn, mid):
            return None
    except ImportError:
        pass
    return m


def _err(msg, code):
    return jsonify({'ok': False, 'error': msg}), code


def _ai_err(exc):
    if type(exc).__name__ == 'TokenCapError':
        return _err('Your AI usage cap for this period is reached. Ask the admin to raise it.', 429)
    print('[job-profile] AI failed: %s: %s' % (type(exc).__name__, exc))
    msg = str(exc) if 'DeepSeek is not set up' in str(exc) else \
        'The AI could not build the breakdown right now (%s). Try again.' % type(exc).__name__
    return _err(msg, 502)


@bp.route('/api/job-profile/<int:mid>', methods=['GET'])
@login_required
def get_profile(mid):
    core = _core()
    conn = get_db()
    try:
        m = _mandate(conn, mid)
        if not m:
            return _err('Not found', 404)
        content = has_job_content(core, m)
        try:
            got = ensure(conn, core, m, create=request.args.get('ensure') == '1' and content)
        except Exception as exc:
            return _ai_err(exc)
        return jsonify({'ok': True, 'has_job_content': content, 'ai_available': bool(_key(core)),
                        **(got or {'profile': None, 'status': 'none', 'stale': False,
                                   'generated_at': '', 'edited_at': ''})})
    finally:
        conn.close()


@bp.route('/api/job-profile/<int:mid>/generate', methods=['POST'])
@login_required
def regen_profile(mid):
    core = _core()
    conn = get_db()
    try:
        m = _mandate(conn, mid)
        if not m:
            return _err('Not found', 404)
        if not has_job_content(core, m):
            return _err('Add a JD, boolean or must-have skills to this mandate first.', 400)
        try:
            prof = generate(conn, core, m)
        except Exception as exc:
            return _ai_err(exc)
        h = hashlib.sha1(source_text(core, m).encode('utf-8')).hexdigest()
        return jsonify({'ok': True, **_pack(_row(conn, mid), h), 'profile': prof})
    finally:
        conn.close()


@bp.route('/api/job-profile/<int:mid>', methods=['PUT', 'POST'])
@login_required
def save_profile(mid):
    core = _core()
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        m = _mandate(conn, mid)
        if not m:
            return _err('Not found', 404)
        prof = clean_profile(d.get('profile'), m)
        if not prof['core'] and not prof['important'] and not prof['nice']:
            return _err('The breakdown needs at least one requirement.', 400)
        h = hashlib.sha1(source_text(core, m).encode('utf-8')).hexdigest()
        _save(conn, mid, prof, h, 'edited')
        return jsonify({'ok': True, **_pack(_row(conn, mid), h)})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  Route — Chrome extension explanation
# ══════════════════════════════════════════════════════════════════════════
def _rate_ok(uid):
    now = time.time()
    with _rl_lock:
        q = [t for t in _rl.get(uid, []) if now - t < WINDOW]
        if len(q) >= EXPLAIN_LIMIT:
            _rl[uid] = q
            return False
        q.append(now)
        _rl[uid] = q
        return True


@bp.route('/api/extension/explain', methods=['POST', 'OPTIONS'])
def explain():
    if request.method == 'OPTIONS':
        return ('', 204)
    if not session.get('user_id'):
        return jsonify({'ok': False, 'error': 'auth_required',
                        'message': 'Please log into HireLab in this browser first.'}), 401
    try:
        return _explain()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return _err('Explanation failed on the server: %s: %s' % (type(exc).__name__, exc), 500)


def _explain():
    core = _core()
    d = request.get_json(silent=True) or {}
    try:
        mid = int(d.get('mandate_id'))
    except (TypeError, ValueError):
        return _err('Pick a mandate first.', 400)
    resume = str(d.get('resume_text') or '').strip()
    prof_txt = str(d.get('candidate_text') or '').strip()
    if len(resume) + len(prof_txt) < 30:
        return _err('No candidate text was read from the page.', 400)
    conn = get_db()
    try:
        m = _mandate(conn, mid)
        if not m:
            return _err('Mandate not found, or it is not yours.', 404)
        key = _key(core)
        if not key:
            return jsonify({'ok': False, 'kind': 'nokey',
                            'error': 'DeepSeek is not set up in ATS Settings, so there is no AI review.'})
        if not has_job_content(core, m):
            return jsonify({'ok': False, 'kind': 'nojd',
                            'error': 'This mandate has no JD, boolean or must-have skills, so there is nothing to explain against.'})
        if not _rate_ok(int(real_user_id() or 0)):
            return _err('Too many AI reviews in the last 10 minutes. Wait a little.', 429)
        try:
            got = ensure(conn, core, m, create=True)
        except Exception as exc:
            return _ai_err(exc)
        prof = (got or {}).get('profile') or {}

        req_lines = []
        for i, it in enumerate(prof.get('core') or []):
            req_lines.append('C%d (core): %s%s' % (i + 1, it['skill'],
                             (' [equivalents: ' + ', '.join(it['equivalents']) + ']') if it.get('equivalents') else ''))
        for i, it in enumerate(prof.get('important') or []):
            req_lines.append('I%d (important): %s%s' % (i + 1, it['skill'],
                             (' [equivalents: ' + ', '.join(it['equivalents']) + ']') if it.get('equivalents') else ''))
        nice = ', '.join(n['skill'] for n in prof.get('nice') or []) or '(none)'
        intent = ''
        try:
            intent = str(m['search_intent'] or '').strip()
        except (IndexError, KeyError):
            pass
        user = ('JOB: %s at %s\n%s\n\nREQUIREMENT IDS:\n%s\nNICE-TO-HAVE: %s\n%s\n\n---\nCANDIDATE\n%s\nNAUKRI PROFILE:\n%s' % (
            m['role'], m['client'], profile_text(prof), '\n'.join(req_lines), nice,
            ('HIRING INTENT: ' + intent[:1500]) if intent else '',
            ('RESUME:\n' + resume[:5500] + '\n\n') if resume else '(no resume text available)\n\n',
            prof_txt[:3500]))
        try:
            raw = _ask(core, key, EXPLAIN_PROMPT, user, 1500, 'profile-explain', 45)
        except Exception as exc:
            return _ai_err(exc)
        ex = normalize_explanation(raw, prof, resume, prof_txt)
        out = {'ok': True, 'explanation': ex,
               'breakdown': prof, 'breakdown_status': got.get('status'), 'breakdown_stale': got.get('stale'),
               'mandate_id': mid}
        if d.get('base_score') is not None:
            try:
                out['final'] = blend(float(d['base_score']), ex['fit'], ex['confidence'], ex['core_missing'])
            except (TypeError, ValueError):
                pass
        return jsonify(out)
    finally:
        conn.close()
