"""
Naukri search screening — "which of these profiles is worth opening?"

Used by the HireLab Chrome extension on a Naukri Resdex search-results page.
The recruiter presses "Analyze this page"; the extension reads the result cards
that are on screen and sends them here in small batches. DeepSeek judges each
card against ONE mandate (its JD, skills, CTC budget and the recruiter's own
screening rules for that mandate) and returns open / maybe / skip, a score and
a one-line reason.

The recruiter can also chat with the screener ("ignore EPC contractors",
"10+ yrs only for this one"). The chat turns into a short list of rules that
is saved PER MANDATE and used on every later analysis of that mandate.

Design rules
  - Additive only: two new tables, no change to any existing table.
  - Nothing a candidate wrote is stored. Profile text lives only for the length
    of the request; phone numbers and e-mail addresses are removed before the
    text leaves for DeepSeek.
  - Same access rules as the rest of the extension: logged-in session, mandate
    of the caller's own company, recruiter scope enforced by modules/access.py
    (mandate_id in the query/body) and re-checked here.
  - The DeepSeek key never leaves the server; usage is logged by call_deepseek.

Routes (all JSON, all under /api/extension/screen)
  GET  /state?mandate_id=            rules, chat history, mandate summary
  POST /                {mandate_id, profiles:[{key,name,text}]}  -> results
  POST /chat            {mandate_id, message, context?}           -> reply + rules
  POST /rules           {mandate_id, rules:[...]}                  manual edit / clear
"""

import re
import json
import time
import threading
from functools import wraps
from flask import Blueprint, request, jsonify, session

from modules.shared import get_db, ts, effective_company_id, real_user_id, _core
from modules import register_migration

bp = Blueprint('naukri_screen', __name__, url_prefix='/api/extension/screen')

MAX_PROFILES_PER_CALL = 12
MAX_PROFILE_CHARS = 1800
MAX_RULES = 25
MAX_RULE_CHARS = 240
MAX_CHAT_KEEP = 40
SCREEN_LIMIT, CHAT_LIMIT, WINDOW = 150, 40, 600   # per user per 10 minutes

_rl = {}
_rl_lock = threading.Lock()


@register_migration
def _migrate_naukri_screen(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS ext_screen_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL DEFAULT 0,
        mandate_id INTEGER NOT NULL,
        rules TEXT DEFAULT '[]',
        chat TEXT DEFAULT '[]',
        updated_by INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT '',
        UNIQUE(company_id, mandate_id)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS ext_screen_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER DEFAULT 0,
        user_id INTEGER DEFAULT 0,
        mandate_id INTEGER DEFAULT 0,
        kind TEXT DEFAULT '',
        n_profiles INTEGER DEFAULT 0,
        n_open INTEGER DEFAULT 0,
        n_maybe INTEGER DEFAULT 0,
        n_skip INTEGER DEFAULT 0,
        created_at TEXT DEFAULT ''
    )''')
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  PURE HELPERS  (unit tested without a database)
# ══════════════════════════════════════════════════════════════════════════
_EMAIL = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
# 10-digit Indian mobiles with optional +91/0 prefix and separators, and any
# other long digit run that looks like a phone number.
_PHONE = re.compile(r'(?<!\d)(?:\+?91[\s\-]?|0)?[6-9]\d{2}[\s\-]?\d{3}[\s\-]?\d{4}(?!\d)')
_LONGNUM = re.compile(r'(?<!\d)\d[\d\s\-]{9,}\d(?!\d)')


def redact(text):
    """Remove e-mail addresses and phone numbers; collapse whitespace."""
    t = str(text or '')
    t = _EMAIL.sub('[email]', t)
    t = _PHONE.sub('[phone]', t)
    t = _LONGNUM.sub('[number]', t)
    t = re.sub(r'[ \t\r\f\v]+', ' ', t)
    t = re.sub(r'\n\s*\n+', '\n', t)
    return t.strip()


def clean_rules(rules):
    """A list of short, unique, non-empty rule strings."""
    out, seen = [], set()
    if isinstance(rules, str):
        rules = [r for r in re.split(r'\n+', rules)]
    for r in rules or []:
        s = re.sub(r'^\s*(?:[-*•]|\d+[.)])\s*', '', str(r or '')).strip()
        s = re.sub(r'\s+', ' ', s)[:MAX_RULE_CHARS]
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
        if len(out) >= MAX_RULES:
            break
    return out


VERDICTS = ('open', 'maybe', 'skip')


def normalize_results(raw, keys):
    """Map DeepSeek's answer back onto the keys we sent. Anything missing or
    malformed becomes an explicit 'unknown' — never a silent skip."""
    items = []
    if isinstance(raw, dict):
        items = raw.get('results') or raw.get('profiles') or []
    elif isinstance(raw, list):
        items = raw
    by_key = {}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        k = str(it.get('key') or it.get('id') or '').strip()
        if k not in keys or k in by_key:
            continue
        v = str(it.get('verdict') or '').strip().lower()
        if v not in VERDICTS:
            v = {'yes': 'open', 'strong': 'open', 'no': 'skip', 'reject': 'skip'}.get(v, '')
        try:
            sc = int(round(float(it.get('score'))))
        except (TypeError, ValueError):
            sc = None
        if sc is not None:
            sc = max(0, min(100, sc))
        if not v and sc is not None:
            v = 'open' if sc >= 70 else 'maybe' if sc >= 45 else 'skip'
        if not v:
            continue
        if sc is None:
            sc = {'open': 75, 'maybe': 55, 'skip': 20}[v]
        by_key[k] = {'key': k, 'verdict': v, 'score': sc,
                     'reason': re.sub(r'\s+', ' ', str(it.get('reason') or '')).strip()[:200],
                     'flags': [str(f)[:40] for f in (it.get('flags') or []) if f][:4]
                     if isinstance(it.get('flags'), list) else []}
    out = []
    for k in keys:
        out.append(by_key.get(k) or {'key': k, 'verdict': 'unknown', 'score': None,
                                     'reason': 'The AI did not return a verdict for this profile.',
                                     'flags': []})
    return out


def _num(v):
    try:
        f = float(v or 0)
        return f if f > 0 else 0
    except (TypeError, ValueError):
        return 0


def job_brief(core, m):
    """Everything the screener needs to know about the job, as plain text."""
    def get(k, d=''):
        try:
            v = m[k]
        except (IndexError, KeyError):
            return d
        return d if v is None else v
    try:
        base = core.mandate_jd_text(m)
    except Exception:
        base = 'Role: %s\nClient: %s\nJob Description:\n%s' % (get('role'), get('client'), get('jd'))
    extra = []
    emin, emax = _num(get('exp_min', 0)), _num(get('exp_max', 0))
    if emin or emax:
        extra.append('Experience required: %s-%s years' % (('%g' % emin) if emin else '?', ('%g' % emax) if emax else '?'))
    elif str(get('experience', '') or '').strip():
        extra.append('Experience required: %s' % str(get('experience')).strip())
    intent = str(get('search_intent', '') or '').strip()
    if intent:
        extra.append('Hiring intent (from the recruiter): ' + intent)
    sop = str(get('sop_text', '') or '').strip()
    if sop:
        extra.append('Screening SOP:\n' + sop[:1500])
    text = base + ('\n' + '\n'.join(extra) if extra else '')
    return text[:9000]


def budget_line(m, core=None):
    try:
        cmin, cmax = _num(m['ctc_min']), _num(m['ctc_max'])
    except Exception:
        cmin = cmax = 0
    if cmax:
        return ('CTC budget for this job: %s-%g LPA. HARD FILTER: if the card shows a CURRENT CTC above %g LPA, '
                'the verdict is "skip" with reason starting "CTC above budget" — unless a recruiter rule below '
                'relaxes the budget.' % (('%g' % cmin) if cmin else '0', cmax, cmax))
    return 'No CTC budget is set for this job, so do not filter on CTC.'


SCREEN_PROMPT = """You are a senior recruiter at HireLab, an agency hiring for Solar, Electrical and Renewable-energy companies in India.
You are looking at a page of Naukri Resdex SEARCH RESULTS (short candidate cards, not full CVs) for ONE job.
Opening a full profile costs the recruiter time, so for every card decide whether it is worth opening.

verdict:
  "open"  - clearly matches the core of the job (right kind of role/domain, experience in range, budget fits). Open it.
  "maybe" - plausible but something important is unclear or slightly off. Open only if time permits.
  "skip"  - clear mismatch on something that matters (wrong domain/function, far outside experience, over budget, breaks a recruiter rule).
score: 0-100, how strong a fit the card looks (open is usually >= 70, maybe 45-69, skip < 45).
reason: ONE short line, max 18 words, naming the decisive fact (e.g. "8 yrs solar EPC site execution, Gujarat, 9 LPA - fits").
flags: up to 3 very short tags such as "over budget", "exp low", "relocation", "rule: no EPC".

Be strict and realistic: a page of search results usually has only a few genuinely strong profiles. Do not reward keyword stuffing;
judge the actual roles, industry and seniority shown. Judge ONLY from the card text; never invent facts. If the card is too thin to judge, use "maybe" and say what is missing.
The recruiter's rules OVERRIDE your general judgement whenever they apply.

{budget}

RECRUITER'S RULES FOR THIS JOB:
{rules}

THE JOB:
{job}

Reply with ONLY a JSON object: {{"results": [{{"key": "<key exactly as given>", "verdict": "open|maybe|skip", "score": 0-100, "reason": "...", "flags": ["..."]}}]}} with one entry per card, in the same order."""


CHAT_PROMPT = """You maintain the screening rules a recruiter uses to judge Naukri search-result cards for ONE job.
The recruiter writes in English, Hindi or Hinglish. They may (a) add, change or remove a rule, (b) ask why a profile got a verdict, or (c) ask something else.

Current rules (numbered):
{rules}

{budget}

The job:
{job}
{context}
Reply with ONLY a JSON object:
{{"reply": "<short answer to the recruiter, in the same language style they used, max 60 words>",
  "rules": ["<the COMPLETE updated rule list>"],
  "changed": true|false}}
Rules must be short, specific and checkable from a search-result card, written in English, one idea per rule
(e.g. "Skip candidates whose current company is an EPC contractor", "Minimum 6 years in solar O&M", "Budget may stretch to 18 LPA for strong profiles").
Keep every existing rule the recruiter did not ask to change. If the message only asks a question, return the rules unchanged with "changed": false.
Never invent requirements the recruiter did not state."""


# ══════════════════════════════════════════════════════════════════════════
#  DB helpers
# ══════════════════════════════════════════════════════════════════════════
def _rate_ok(kind, uid, limit):
    now = time.time()
    k = (kind, uid)
    with _rl_lock:
        q = [t for t in _rl.get(k, []) if now - t < WINDOW]
        if len(q) >= limit:
            _rl[k] = q
            return False
        q.append(now)
        _rl[k] = q
        return True


def _load_mandate(conn, mid_raw):
    """(mandate_row, error_response) for the caller's company and scope."""
    if mid_raw in (None, '', 'central'):
        return None, (jsonify({'ok': False, 'error': 'Pick a mandate in the HireLab extension first.'}), 400)
    try:
        mid = int(mid_raw)
    except (TypeError, ValueError):
        return None, (jsonify({'ok': False, 'error': 'Invalid mandate_id'}), 400)
    m = conn.execute('SELECT * FROM mandates WHERE id=? AND owner_id=?', (mid, effective_company_id())).fetchone()
    if not m:
        return None, (jsonify({'ok': False, 'error': 'Mandate not found, or it is not yours.'}), 404)
    try:
        from modules.access import can_see_mandate
        if not can_see_mandate(conn, mid):
            return None, (jsonify({'ok': False, 'error': 'This mandate is not assigned to you.'}), 403)
    except ImportError:
        pass
    return m, None


def _get_state(conn, mid):
    r = conn.execute('SELECT rules, chat, updated_at, updated_by FROM ext_screen_rules WHERE company_id=? AND mandate_id=?',
                     (effective_company_id(), mid)).fetchone()
    if not r:
        return [], [], '', 0
    try:
        rules = json.loads(r['rules'] or '[]')
    except Exception:
        rules = []
    try:
        chat = json.loads(r['chat'] or '[]')
    except Exception:
        chat = []
    return clean_rules(rules), chat if isinstance(chat, list) else [], r['updated_at'] or '', r['updated_by'] or 0


def _save_state(conn, mid, rules, chat):
    conn.execute('''INSERT INTO ext_screen_rules (company_id, mandate_id, rules, chat, updated_by, updated_at)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(company_id, mandate_id) DO UPDATE SET
                      rules=excluded.rules, chat=excluded.chat,
                      updated_by=excluded.updated_by, updated_at=excluded.updated_at''',
                 (effective_company_id(), mid, json.dumps(clean_rules(rules)),
                  json.dumps((chat or [])[-MAX_CHAT_KEEP:]), int(real_user_id() or 0), ts()))
    conn.commit()


def _deepseek_key(core):
    try:
        return (core.get_setting('deepseek_api_key') or '').strip()
    except Exception:
        return ''


def _ask_deepseek(core, key, system, user, max_tokens, endpoint, timeout=60):
    payload = {'model': 'deepseek-chat', 'temperature': 0, 'max_tokens': max_tokens,
               'response_format': {'type': 'json_object'},
               'messages': [{'role': 'system', 'content': system},
                            {'role': 'user', 'content': user}]}
    resp = core.call_deepseek(key, payload, timeout=timeout, endpoint=endpoint)
    if resp.status_code != 200:
        raise RuntimeError('DeepSeek returned HTTP %s' % resp.status_code)
    raw = resp.json()['choices'][0]['message']['content']
    j = _parse(core, raw)
    if j is None:
        raise ValueError('DeepSeek reply was not JSON')
    return j


def _parse(core, raw):
    """json.loads first: the core parse_json tries '[' before '{', so an object
    that contains a list (our {"results": [...]}) would come back as the list."""
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
    return core.parse_json(raw) if hasattr(core, 'parse_json') else None


def _rules_text(rules):
    return '\n'.join('%d. %s' % (i + 1, r) for i, r in enumerate(rules)) if rules else '(none yet)'


def _err(msg, code):
    return jsonify({'ok': False, 'error': msg}), code


def _ai_error(core, exc):
    if type(exc).__name__ == 'TokenCapError':
        return _err('Your AI usage cap for this period is reached. Ask the admin to raise it.', 429)
    print('[naukri-screen] DeepSeek failed: %s: %s' % (type(exc).__name__, exc))
    return _err('The AI could not analyse this right now (%s). Try again in a moment.' % type(exc).__name__, 502)


# ══════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════
def _ext_route(fn):
    """CORS preflight answered before any auth; then the same 401 JSON the
    other extension routes give, which background.js turns into 'log in'."""
    @wraps(fn)
    def inner(*a, **kw):
        if request.method == 'OPTIONS':
            return ('', 204)
        if not session.get('user_id'):
            return jsonify({'ok': False, 'error': 'auth_required',
                            'message': 'Please log into HireLab in this browser first.'}), 401
        try:
            return fn(*a, **kw)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            return jsonify({'ok': False, 'error': 'Screening failed on the server: %s: %s'
                            % (type(exc).__name__, exc)}), 500
    return inner


@bp.route('/state', methods=['GET', 'OPTIONS'])
@_ext_route
def screen_state():
    core = _core()
    conn = get_db()
    try:
        m, err = _load_mandate(conn, request.args.get('mandate_id'))
        if err:
            return err
        rules, chat, upd_at, upd_by = _get_state(conn, m['id'])
        who = ''
        if upd_by:
            u = conn.execute('SELECT display_name, username FROM users WHERE id=?', (upd_by,)).fetchone()
            who = ((u['display_name'] or u['username']) if u else '') or ''
        has_jd = any(str(core._row_get(m, k, '') or '').strip()
                     for k in ('jd', 'boolean_query', 'must_have_skills', 'search_intent'))
        return jsonify({'ok': True,
                        'mandate': {'id': m['id'], 'role': m['role'], 'client': m['client'],
                                    'ctc_min': _num(m['ctc_min']), 'ctc_max': _num(m['ctc_max']),
                                    'has_jd': has_jd},
                        'rules': rules, 'chat': chat[-MAX_CHAT_KEEP:],
                        'updated_at': upd_at, 'updated_by': who,
                        'ai_available': bool(_deepseek_key(core))})
    finally:
        conn.close()


@bp.route('', methods=['POST', 'OPTIONS'], strict_slashes=False)
@_ext_route
def screen_profiles():
    core = _core()
    d = request.get_json(silent=True) or {}
    profiles = d.get('profiles') or []
    if not isinstance(profiles, list) or not profiles:
        return _err('No profiles were read from the page.', 400)
    if len(profiles) > MAX_PROFILES_PER_CALL:
        return _err('Send at most %d profiles per call.' % MAX_PROFILES_PER_CALL, 400)

    clean, keys = [], []
    for p in profiles:
        if not isinstance(p, dict):
            continue
        k = str(p.get('key') or '').strip()[:80]
        txt = redact(p.get('text'))[:MAX_PROFILE_CHARS]
        if not k or k in keys or len(txt) < 20:
            continue
        keys.append(k)
        clean.append({'key': k, 'text': txt})
    if not clean:
        return _err('The profile cards on this page had no readable text.', 400)

    conn = get_db()
    try:
        m, err = _load_mandate(conn, d.get('mandate_id'))
        if err:
            return err
        key = _deepseek_key(core)
        if not key:
            return _err('DeepSeek is not set up. Add the DeepSeek API key in ATS Settings.', 400)
        uid = int(real_user_id() or 0)
        if not _rate_ok('screen', uid, SCREEN_LIMIT):
            return _err('Too many analyses in the last 10 minutes. Wait a little and retry.', 429)
        rules, _chat, _a, _b = _get_state(conn, m['id'])
        system = SCREEN_PROMPT.format(budget=budget_line(m), rules=_rules_text(rules), job=job_brief(core, m))
        user = 'CARDS:\n' + '\n\n'.join('[key: %s]\n%s' % (c['key'], c['text']) for c in clean)
        try:
            raw = _ask_deepseek(core, key, system, user, max_tokens=150 + 110 * len(clean),
                                endpoint='naukri-screen', timeout=75)
        except Exception as exc:
            return _ai_error(core, exc)
        results = normalize_results(raw, keys)
        cnt = {v: sum(1 for r in results if r['verdict'] == v) for v in VERDICTS}
        try:
            conn.execute('INSERT INTO ext_screen_log (company_id, user_id, mandate_id, kind, n_profiles, n_open, n_maybe, n_skip, created_at) '
                         'VALUES (?,?,?,?,?,?,?,?,?)',
                         (effective_company_id(), uid, m['id'], 'screen', len(clean), cnt['open'], cnt['maybe'], cnt['skip'], ts()))
            conn.commit()
        except Exception as e:
            print('[naukri-screen] log failed: %s' % e)
        return jsonify({'ok': True, 'results': results, 'rules_used': len(rules),
                        'mandate_role': m['role'], 'mandate_client': m['client']})
    finally:
        conn.close()


@bp.route('/chat', methods=['POST', 'OPTIONS'])
@_ext_route
def screen_chat():
    core = _core()
    d = request.get_json(silent=True) or {}
    msg = str(d.get('message') or '').strip()[:1200]
    if not msg:
        return _err('Type a message first.', 400)
    conn = get_db()
    try:
        m, err = _load_mandate(conn, d.get('mandate_id'))
        if err:
            return err
        key = _deepseek_key(core)
        if not key:
            return _err('DeepSeek is not set up. Add the DeepSeek API key in ATS Settings.', 400)
        uid = int(real_user_id() or 0)
        if not _rate_ok('chat', uid, CHAT_LIMIT):
            return _err('Too many chat messages in the last 10 minutes. Wait a little and retry.', 429)
        rules, chat, _a, _b = _get_state(conn, m['id'])

        ctx = ''
        items = d.get('context') if isinstance(d.get('context'), list) else []
        if items:
            lines = []
            for it in items[:40]:
                if not isinstance(it, dict):
                    continue
                lines.append('- %s: %s (%s) %s' % (redact(it.get('name'))[:60], str(it.get('verdict') or '')[:8],
                                                   str(it.get('score') or ''), redact(it.get('reason'))[:160]))
            if lines:
                ctx = '\nLatest verdicts on the recruiter\'s screen:\n' + '\n'.join(lines) + '\n'

        history = []
        for h in chat[-10:]:
            role = 'assistant' if h.get('role') == 'ai' else 'user'
            history.append({'role': role, 'content': str(h.get('text') or '')[:600]})

        system = CHAT_PROMPT.format(rules=_rules_text(rules), budget=budget_line(m),
                                    job=job_brief(core, m)[:5000], context=ctx)
        payload_msgs = [{'role': 'system', 'content': system}] + history + [{'role': 'user', 'content': msg}]
        try:
            payload = {'model': 'deepseek-chat', 'temperature': 0, 'max_tokens': 900,
                       'response_format': {'type': 'json_object'}, 'messages': payload_msgs}
            resp = core.call_deepseek(key, payload, timeout=45, endpoint='naukri-screen-chat')
            if resp.status_code != 200:
                raise RuntimeError('DeepSeek returned HTTP %s' % resp.status_code)
            raw = resp.json()['choices'][0]['message']['content']
            j = _parse(core, raw)
            if not isinstance(j, dict):
                raise ValueError('DeepSeek reply was not a JSON object')
        except Exception as exc:
            return _ai_error(core, exc)

        reply = re.sub(r'\s+', ' ', str(j.get('reply') or '')).strip()[:600] or 'Done.'
        new_rules = clean_rules(j.get('rules')) if isinstance(j.get('rules'), list) else rules
        changed = new_rules != rules
        # A question must never wipe the list: an empty answer with no explicit
        # change keeps what was there.
        if not new_rules and rules and not j.get('changed'):
            new_rules, changed = rules, False
        now = ts()
        chat = chat + [{'role': 'me', 'text': msg, 'at': now, 'by': uid},
                       {'role': 'ai', 'text': reply, 'at': now, 'changed': changed}]
        _save_state(conn, m['id'], new_rules, chat)
        return jsonify({'ok': True, 'reply': reply, 'rules': new_rules, 'changed': changed,
                        'chat': chat[-MAX_CHAT_KEEP:]})
    finally:
        conn.close()


@bp.route('/rules', methods=['POST', 'OPTIONS'])
@_ext_route
def screen_rules():
    """Manual edit of the saved rules (also used for 'clear')."""
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        m, err = _load_mandate(conn, d.get('mandate_id'))
        if err:
            return err
        rules, chat, _a, _b = _get_state(conn, m['id'])
        new_rules = clean_rules(d.get('rules') or [])
        if d.get('clear_chat'):
            chat = []
        _save_state(conn, m['id'], new_rules, chat)
        return jsonify({'ok': True, 'rules': new_rules, 'chat': chat[-MAX_CHAT_KEEP:]})
    finally:
        conn.close()
