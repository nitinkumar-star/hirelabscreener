"""
RecruitOS — Skill Engine, Phase 1: dictionary + job skill spec + candidate skill depth  (Nov 2026)

Phase 1 builds and SHOWS the skill data. It does not change any score yet
(owner's decision 2-A: the new skill score comes in Phase 2, side by side with
the old one first).

1. DICTIONARY   skill_taxonomy.py (shipped base: standard names, aliases incl.
                brands, category, domain, nearby skills with partial credit)
                + each agency's own additions in skill_custom (owner's 3-A).
                AI suggestions are used at once for the job they came from and
                become agency-wide after an admin approves them (owner's 1-A).
2. JOB SKILL SPEC  per mandate: for every skill the job needs — importance,
                minimum years, involvement (led / did / supported / any), how
                recent, and which nearby skills count. Made by DeepSeek from the
                job text + the Job Breakdown, editable on the Job Breakdown tab;
                rebuilt when the job text changes unless a person edited it.
3. CANDIDATE SKILL DEPTH  per candidate: for every skill — years (from the
                dated work history, overlaps counted once), last used, current
                role or not, involvement (led / did / supported / exposure) with
                an exact quote from the CV, and the source (CV / profile).
                Years are arithmetic, never guessed; the AI only reads the CV
                for involvement and which role a skill belongs to.

Routes (all @login_required, agency + recruiter scope; freelancers are not allowed)
  GET  /skill-engine.js
  GET  /api/candidates/<cid>/skill-depth          stored result (+ stale flag)
  POST /api/candidates/<cid>/skill-depth          analyse now
  GET  /api/mandates/<mid>/skill-spec?ensure=1    the job's skill spec (made on first open)
  POST /api/mandates/<mid>/skill-spec/generate    rebuild with AI
  PUT  /api/mandates/<mid>/skill-spec             save edits
  GET  /api/skills/dictionary?q=&mid=             search the dictionary (typeahead)
  GET  /api/skills/config  ·  POST (admin) {on}
  GET  /api/skills/suggestions                    AI suggestions waiting for approval (admin)
  POST /api/skills/suggestions/<id>   {decision: approve|reject}   (admin)
  POST /api/skills/backfill/start|stop · GET /api/skills/backfill/status   (admin)

Tables (new): skill_custom, mandate_skill_spec, candidate_skill_depth, skill_backfill.
Switch: insight_settings 'skill_engine_on' (default on).
ROLLBACK: remove 'skill_engine' from modules/__init__.py. Nothing else reads these tables.
"""

import os
import re
import sys
import json
import time
import hashlib
import datetime
import threading

from flask import Blueprint, request, jsonify, Response

from modules.shared import get_db, ts, effective_company_id, real_user_id, is_company_admin, login_required, _core
from modules import register_migration

bp = Blueprint('skill_engine', __name__)
pages = Blueprint('skill_engine_pages', __name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEVELS = ('led', 'did', 'supported', 'exposure')
INVOLVEMENT = ('led', 'did', 'supported', 'any')
IMPORTANCE = ('core', 'important', 'nice')
NEW_WINDOW_HOURS = 48
THROTTLE_SECONDS = 3
IDLE_SECONDS = 60
CV_CHARS = 7000


def taxonomy():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import skill_taxonomy
    return skill_taxonomy


@register_migration
def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS skill_custom (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        kind TEXT NOT NULL,                 -- alias | skill | near
        skill_key TEXT NOT NULL,            -- the skill this is about (new skills: c_<slug>)
        value TEXT DEFAULT '',              -- alias text | new skill name | nearby skill key
        credit REAL,                        -- near: share of credit
        domain TEXT DEFAULT '',
        category TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',      -- pending | approved | rejected
        source TEXT DEFAULT 'ai',           -- ai | admin
        mandate_id INTEGER DEFAULT 0,
        created_by INTEGER DEFAULT 0,
        created_at TEXT DEFAULT '',
        decided_by INTEGER DEFAULT 0,
        decided_at TEXT DEFAULT ''
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_skc_owner ON skill_custom(owner_id, status)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_skc_mand ON skill_custom(owner_id, mandate_id)')
    conn.execute('''CREATE TABLE IF NOT EXISTS mandate_skill_spec (
        owner_id INTEGER NOT NULL,
        mandate_id INTEGER NOT NULL,
        spec TEXT DEFAULT '{}',
        status TEXT DEFAULT 'ai',           -- ai | edited
        source_hash TEXT DEFAULT '',
        generated_at TEXT DEFAULT '',
        edited_by INTEGER DEFAULT 0,
        edited_at TEXT DEFAULT '',
        PRIMARY KEY (owner_id, mandate_id)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS candidate_skill_depth (
        candidate_id INTEGER PRIMARY KEY,
        owner_id INTEGER DEFAULT 0,
        data TEXT DEFAULT '{}',
        input_hash TEXT DEFAULT '',
        ai_used INTEGER DEFAULT 0,
        status TEXT DEFAULT 'done',         -- done | error | skipped
        error TEXT DEFAULT '',
        computed_at TEXT DEFAULT ''
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_csd_owner ON candidate_skill_depth(owner_id, status)')
    conn.execute('''CREATE TABLE IF NOT EXISTS skill_backfill (
        owner_id INTEGER PRIMARY KEY,
        status TEXT DEFAULT 'idle',
        total INTEGER DEFAULT 0,
        done INTEGER DEFAULT 0,
        failed INTEGER DEFAULT 0,
        message TEXT DEFAULT '',
        started_by INTEGER DEFAULT 0,
        started_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        finished_at TEXT DEFAULT ''
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS insight_settings (
        owner_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT DEFAULT '', updated_at TEXT DEFAULT '',
        PRIMARY KEY (owner_id, key))''')
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════
def _uid():
    try:
        return int(real_user_id() or 0)
    except Exception:
        return 0


def _clean(v, n=200):
    return ' '.join(str(v or '').split())[:n]


def _norm(text):
    """Lower case; '-' and '_' count as spaces; one space between words."""
    t = str(text or '').lower().replace('–', ' ').replace('—', ' ')
    t = re.sub(r'[\-_]+', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def _slug(name):
    return 'c_' + (re.sub(r'[^a-z0-9]+', '_', str(name or '').lower()).strip('_')[:40] or 'skill')


def _jl(v):
    try:
        x = json.loads(v or '[]')
        return [str(i) for i in x if str(i).strip()] if isinstance(x, list) else []
    except Exception:
        return [s.strip() for s in str(v or '').split(',') if s.strip()]


def engine_on(conn, oid):
    r = conn.execute("SELECT value FROM insight_settings WHERE owner_id=? AND key='skill_engine_on'", (oid,)).fetchone()
    return True if r is None else str(r['value'] or '').strip().lower() not in ('0', 'false', 'off', 'no', '')


def _fl_block():
    try:
        from modules.freelancer import block_if_freelancer
        return block_if_freelancer()
    except Exception:
        return None


def _scope():
    try:
        return _core()._cs('c.mandate_id')
    except Exception:
        return '', []


def _own_cand(conn, cid):
    sc, sp = _scope()
    return conn.execute('SELECT c.* FROM candidates c WHERE c.id=? AND c.owner_id=?' + sc,
                        [cid, effective_company_id()] + sp).fetchone()


def _own_mandate(conn, mid):
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


# ══════════════════════════════════════════════════════════════════════════
#  1. Dictionary (base + the agency's own, + a job's own AI suggestions)
# ══════════════════════════════════════════════════════════════════════════
def lexicon(conn, oid, mandate_id=0):
    """{key: {key, name, category, domain, aliases[], near{}, custom, pending}} for this
    agency: the shipped base + approved additions + (only for this mandate) its own
    pending AI suggestions (owner's decision 1-A)."""
    lex = {k: dict(v, aliases=list(v['aliases']), near=dict(v['near']), custom=False, pending=False)
           for k, v in taxonomy().BASE.items()}
    rows = conn.execute("SELECT * FROM skill_custom WHERE owner_id=? AND (status='approved' OR (status='pending' AND "
                        "mandate_id=? AND ?>0)) ORDER BY kind DESC, id", (oid, mandate_id or 0, mandate_id or 0)).fetchall()
    for r in sorted(rows, key=lambda r: {'skill': 0, 'alias': 1, 'near': 2}.get(r['kind'], 3)):
        pend = r['status'] == 'pending'
        if r['kind'] == 'skill':
            if r['skill_key'] not in lex:
                lex[r['skill_key']] = {'key': r['skill_key'], 'name': r['value'], 'category': r['category'] or 'process',
                                       'domain': r['domain'] or 'general', 'aliases': [_norm(r['value'])], 'near': {},
                                       'custom': True, 'pending': pend}
        elif r['kind'] == 'alias' and r['skill_key'] in lex:
            a = _norm(r['value'])
            if a and a not in lex[r['skill_key']]['aliases']:
                lex[r['skill_key']]['aliases'].append(a)
        elif r['kind'] == 'near' and r['skill_key'] in lex and r['value'] in lex:
            lex[r['skill_key']]['near'][r['value']] = float(r['credit'] or 0.5)
    return lex


_RX_CACHE = {}


def _matcher(lex):
    """One regex over every alias (longest first) -> {alias: key}."""
    sig = hashlib.sha1(json.dumps(sorted((k, sorted(v['aliases'])) for k, v in lex.items())).encode()).hexdigest()
    hit = _RX_CACHE.get(sig)
    if hit:
        return hit
    amap = {}
    for k, v in lex.items():
        for a in v['aliases']:
            a = _norm(a)
            if a and a not in amap:
                amap[a] = k
    alts = sorted(amap, key=len, reverse=True)
    rx = re.compile(r'(?<![a-z0-9])(' + '|'.join(re.escape(a) for a in alts) + r')(?:s|es)?(?![a-z0-9])') if alts else None
    if len(_RX_CACHE) > 50:
        _RX_CACHE.clear()
    _RX_CACHE[sig] = (rx, amap)
    return rx, amap


def exact_key(name, lex):
    """The skill whose name or one of whose aliases IS this whole name (plural
    tolerated); '' when none. Used for names the AI writes, so that "Busway
    retrofit selling" never maps to a skill just because it contains "retrofit"."""
    n = _norm(name)
    if not n:
        return ''
    _rx, amap = _matcher(lex)
    for cand in (n, n[:-1] if n.endswith('s') else n, n[:-2] if n.endswith('es') else n):
        if cand in amap:
            return amap[cand]
    for k, v in lex.items():
        if _norm(v['name']) == n:
            return k
    return ''


def find_skills(text, lex):
    """{key: first matched alias} for every dictionary skill named in the text."""
    rx, amap = _matcher(lex)
    out = {}
    if not rx:
        return out
    for m in rx.finditer(_norm(text)):
        k = amap.get(m.group(1))
        if k and k not in out:
            out[k] = m.group(1)
    return out


# ══════════════════════════════════════════════════════════════════════════
#  2. Candidate skill depth
# ══════════════════════════════════════════════════════════════════════════
def _xe():
    from modules.xp_engine import experience_engine as xe
    return xe


def _spans(conn, cid):
    xe = _xe()
    now_m = xe.now_month_index()
    out = []
    rows = conn.execute('SELECT company, designation, start_date, end_date, COALESCE(is_current,0) AS is_current, '
                        "COALESCE(description,'') AS description FROM work_history WHERE candidate_id=? "
                        'ORDER BY COALESCE(sort_order,0), id', (cid,)).fetchall()
    for i, w in enumerate(rows):
        s = xe._start_index(w['start_date'])
        e = xe._end_exclusive(w['end_date'], bool(w['is_current']), now_m)
        ok = s is not None and e is not None and e > s
        out.append({'i': i, 'company': w['company'] or '', 'designation': w['designation'] or '',
                    'current': bool(w['is_current']) or xe.is_current_text(w['end_date']),
                    'start': s if ok else None, 'end': e if ok else None,
                    'dates': ' – '.join(x for x in (w['start_date'] or '', w['end_date'] or ('present' if w['is_current'] else '')) if x),
                    'text': '%s. %s' % (w['designation'] or '', w['description'] or '')})
    return out, now_m


def _months(spans, idxs):
    xe = _xe()
    iv = [(s['start'], s['end']) for s in spans if s['i'] in idxs and s['start'] is not None]
    if not iv:
        return None, None
    total, _lo, hi = xe.merge_intervals(iv)
    return total, hi


def _input_hash(c, spans, cv_text):
    raw = json.dumps([c['designation'], c['key_skills'], c['secondary_skills'] if 'secondary_skills' in c.keys() else '',
                      c['career_summary'], c['cv_path'], [(s['designation'], s['dates'], s['text']) for s in spans],
                      len(cv_text or '')], default=str)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


_DEPTH_SYSTEM = (
    'You read ONE candidate\'s CV for an Indian electrical / power / automation / solar / data-center recruitment agency. '
    'For each skill the person really has, say how deeply they were involved and in which of their roles. Use the '
    'dictionary key when the skill is in the dictionary; otherwise give a short standard name (max 5 new skills). '
    'Levels: "led" = owned / headed / managed it or a team for it; "did" = did the work hands-on as their own job; '
    '"supported" = assisted / coordinated / was part of a team; "exposure" = trained, familiar, mentioned only. '
    'Judge from what the CV says the person DID, not from keyword lists. "roles" = the numbers of the work-history roles '
    'where they used it (from the list given), [] if unclear. "quote" = an EXACT short quote (max 20 words) copied from '
    'the CV that shows it, or "". Never invent. Return ONLY JSON: {"skills": [{"key": "dictionary key or \\"\\"", '
    '"name": "only when key is empty", "level": "led|did|supported|exposure", "roles": [0], "quote": "..."}]} '
    'with at most 25 skills, the most important first.')


def _classify_depth(core, key, user):
    resp = core.call_deepseek(key, {
        'model': 'deepseek-chat', 'temperature': 0.1, 'max_tokens': 1800, 'response_format': {'type': 'json_object'},
        'messages': [{'role': 'system', 'content': _DEPTH_SYSTEM}, {'role': 'user', 'content': user}]},
        timeout=60, endpoint='skill-depth')
    if resp.status_code != 200:
        raise RuntimeError('DeepSeek HTTP %s' % resp.status_code)
    return core.parse_json(resp.json()['choices'][0]['message']['content'] or '') or {}


def compute_depth(conn, oid, cid, use_ai=True, classify=None):
    """Work out one person's skill depth and store it on every record of that
    person. Returns the data dict. Raises the core TokenCapError when the
    agency is over its AI limit (nothing stored)."""
    core = _core()
    c = conn.execute('SELECT * FROM candidates WHERE id=? AND owner_id=?', (cid, oid)).fetchone()
    if not c:
        return None
    lex = lexicon(conn, oid)
    spans, now_m = _spans(conn, cid)
    cv = ''
    try:
        cv = core._candidate_cv_text(c, CV_CHARS) or ''
    except Exception:
        cv = ''
    profile = ' '.join([c['designation'] or '', ' '.join(_jl(c['key_skills'])),
                        ' '.join(_jl(c['secondary_skills'])) if 'secondary_skills' in c.keys() else '',
                        c['career_summary'] or '', c['industry_background'] or ''])
    found_cv = find_skills(cv, lex)
    found_profile = find_skills(profile, lex)
    per_span = {s['i']: set(find_skills(s['text'], lex)) for s in spans}
    skills = {}

    def entry(k):
        if k not in skills:
            skills[k] = {'key': k, 'name': lex[k]['name'] if k in lex else k, 'domain': lex.get(k, {}).get('domain', ''),
                         'category': lex.get(k, {}).get('category', ''), 'roles': set(), 'level': None, 'quote': '',
                         'quote_ok': False, 'source': None, 'new': k not in lex}
        return skills[k]
    for i, ks in per_span.items():
        for k in ks:
            entry(k)['roles'].add(i)
    for k in found_cv:
        entry(k)['source'] = 'cv'
    for k in found_profile:
        e = entry(k)
        e['source'] = e['source'] or 'profile'
    for k, e in skills.items():
        if e['source'] is None:
            e['source'] = 'cv' if e['roles'] else 'profile'     # found only in a work-history description

    ai_used = False
    key = core.get_setting('deepseek_api_key') if use_ai else ''
    if use_ai and (key or classify) and (cv.strip() or any(s['text'].strip(' .') for s in spans)):
        roles_txt = '\n'.join('%d. %s @ %s (%s): %s' % (s['i'], s['designation'], s['company'], s['dates'],
                                                       _clean(s['text'].split('. ', 1)[-1], 500)) for s in spans) or '(none)'
        dict_txt = ', '.join('%s=%s' % (k, v['name']) for k, v in lex.items())
        user = ('DICTIONARY (key=name): %s\n\nWORK HISTORY ROLES:\n%s\n\nCURRENT: %s at %s\nSKILLS ALREADY SEEN: %s\n\nCV TEXT:\n%s'
                % (dict_txt, roles_txt, c['designation'] or '', c['company'] or '', ', '.join(sorted(skills)) or '-',
                   (cv or profile)[:CV_CHARS]))
        try:
            got = (classify or (lambda u: _classify_depth(core, key, u)))(user)
            ai_used = True
        except Exception as e:
            if type(e).__name__ == 'TokenCapError':
                raise
            got = {}
        evidence_text = cv + '\n' + profile + '\n' + '\n'.join(s['text'] for s in spans)
        new_n = 0
        for it in (got.get('skills') or [])[:25] if isinstance(got, dict) else []:
            if not isinstance(it, dict):
                continue
            k = str(it.get('key') or '').strip()
            if k not in lex:
                nm = _clean(it.get('name'), 60)
                if not nm or new_n >= 5:
                    continue
                k = exact_key(nm, lex) or _slug(nm)  # the "new" name may be a known alias
                if k not in lex:
                    new_n += 1
                    e = entry(k)
                    e['name'], e['new'] = nm, True
            e = entry(k)
            lvl = str(it.get('level') or '').lower()
            if lvl in LEVELS:
                e['level'] = lvl
            for r in it.get('roles') or []:
                try:
                    r = int(r)
                except (TypeError, ValueError):
                    continue
                if any(s['i'] == r for s in spans):
                    e['roles'].add(r)
            q = _clean(it.get('quote'), 200)
            if q:
                from modules.job_profile import quote_found
                if quote_found(q, evidence_text):
                    e['quote'], e['quote_ok'] = q, True
            if e['source'] is None:
                e['source'] = 'cv' if cv else 'profile'

    out = []
    for k, e in skills.items():
        months, last = _months(spans, e['roles'])
        cur = any(s['current'] for s in spans if s['i'] in e['roles'])
        xe = _xe()
        out.append({'key': k, 'name': e['name'], 'domain': e['domain'], 'category': e['category'],
                    'years': round(months / 12.0, 1) if months else None,
                    'last_used': (None if last is None else ('current' if cur or last > now_m else xe.index_to_iso(last - 1))),
                    'current': cur, 'level': e['level'], 'quote': e['quote'], 'quote_ok': e['quote_ok'],
                    'source': e['source'], 'new': e['new'],
                    'roles': [{'company': s['company'], 'designation': s['designation'], 'dates': s['dates']}
                              for s in spans if s['i'] in e['roles']][:6]})
    rank = {'led': 0, 'did': 1, 'supported': 2, 'exposure': 3, None: 4}
    out.sort(key=lambda x: (-(x['years'] or 0), rank[x['level']], x['name'].lower()))
    data = {'skills': out, 'ai': ai_used, 'cv': bool(cv.strip()), 'roles': len(spans),
            'dated_roles': sum(1 for s in spans if s['start'] is not None), 'computed_at': ts()}
    h = _input_hash(c, spans, cv)
    root = int(c['copied_from'] or 0) or int(c['id'])
    ids = [r[0] for r in conn.execute('SELECT id FROM candidates WHERE owner_id=? AND (id=? OR copied_from=?)', (oid, root, root))] or [cid]
    for i in ids:
        conn.execute('INSERT OR REPLACE INTO candidate_skill_depth (candidate_id, owner_id, data, input_hash, ai_used, status, '
                     'error, computed_at) VALUES (?,?,?,?,?,?,?,?)',
                     (i, oid, json.dumps(data), h, 1 if ai_used else 0, 'done', '', ts()))
    return data


# ══════════════════════════════════════════════════════════════════════════
#  3. Job skill spec
# ══════════════════════════════════════════════════════════════════════════
_SPEC_SYSTEM = (
    'You turn ONE job (mandate) of an Indian electrical / power / automation / solar / data-center recruitment agency into '
    'a precise SKILL SPEC for matching candidates. Use the dictionary keys whenever a skill is in the dictionary. Rules:\n'
    '- 5 to 12 skills. importance: "core" (cannot do the job without it, max 5), "important", "nice".\n'
    '- min_years: minimum years with THIS skill the job needs, only if the job text implies it, else null.\n'
    '- involvement: what the person must have done with it: "led", "did", "supported" or "any".\n'
    '- recent_years: used within the last N years, only if it matters (e.g. 3), else null.\n'
    '- aliases: other names / brands / abbreviations for this skill that the dictionary does NOT already list (max 5).\n'
    '- near: nearby skills that partly count, as {"key" (or "name"), "credit": 0.2-0.9}, max 4.\n'
    '- why: one short line. Do not invent requirements that are not in the job text or the breakdown.\n'
    'Return ONLY JSON: {"skills": [{"key": "dictionary key or \\"\\"", "name": "only when key is empty", "importance": '
    '"core|important|nice", "min_years": null, "involvement": "any", "recent_years": null, "why": "...", "aliases": [], '
    '"near": [{"key": "", "name": "", "credit": 0.5}]}], "notes": "one line"}')


def _job_text(conn, core, m):
    from modules.naukri_screen import job_brief
    txt = job_brief(core, m)
    try:
        from modules import job_profile as jp
        got = jp.ensure(conn, core, m, create=False)
        if got and got.get('profile'):
            txt += '\n\nJOB BREAKDOWN (agreed with the recruiter):\n' + jp.profile_text(got['profile'])
    except Exception:
        pass
    return txt


def _spec_row(conn, oid, mid):
    return conn.execute('SELECT * FROM mandate_skill_spec WHERE owner_id=? AND mandate_id=?', (oid, mid)).fetchone()


def _add_custom(conn, oid, mid, kind, skill_key, value, credit=None, domain='', category=''):
    dup = conn.execute('SELECT 1 FROM skill_custom WHERE owner_id=? AND kind=? AND skill_key=? AND LOWER(value)=LOWER(?) '
                       "AND status!='rejected'", (oid, kind, skill_key, value)).fetchone()
    if dup:
        return
    conn.execute('INSERT INTO skill_custom (owner_id, kind, skill_key, value, credit, domain, category, status, source, '
                 'mandate_id, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                 (oid, kind, skill_key, value, credit, domain, category, 'pending', 'ai', mid, _uid(), ts()))


def clean_spec(conn, oid, mid, raw, from_ai=True):
    """Validate a spec. AI-new skills / aliases / nearby links become pending
    suggestions tied to this mandate (usable here at once, agency-wide after approval)."""
    items = (raw.get('skills') if isinstance(raw, dict) else raw) or []
    lex = lexicon(conn, oid, mid)
    out, seen = [], set()
    for it in items[:15] if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        k = str(it.get('key') or '').strip()
        if k not in lex:
            nm = _clean(it.get('name') or it.get('key'), 60)
            if not nm:
                continue
            hit = exact_key(nm, lex)
            if hit:
                k = hit
            elif from_ai:
                k = _slug(nm)
                _add_custom(conn, oid, mid, 'skill', k, nm, domain='general', category='process')
                lex = lexicon(conn, oid, mid)
            else:
                continue
        if k in seen:
            continue
        seen.add(k)
        imp = str(it.get('importance') or 'important').lower()
        inv = str(it.get('involvement') or 'any').lower()

        def num(v, lo, hi):
            try:
                n = float(v)
            except (TypeError, ValueError):
                return None
            return n if lo <= n <= hi else None
        if from_ai:
            for a in (it.get('aliases') or [])[:5]:
                a = _norm(_clean(a, 60))
                if a and len(a) >= 3 and not exact_key(a, lex) and a not in lex[k]['aliases']:
                    _add_custom(conn, oid, mid, 'alias', k, a)
        near = []
        for n in (it.get('near') or [])[:4]:
            if not isinstance(n, dict):
                continue
            nk = str(n.get('key') or '').strip()
            if nk not in lex:
                nk = exact_key(_clean(n.get('name') or nk, 60), lex)
            cr = num(n.get('credit'), 0.1, 0.95)
            if nk and nk != k and cr is not None:
                near.append({'key': nk, 'name': lex[nk]['name'], 'credit': round(cr, 2)})
                if from_ai and nk not in lex[k]['near']:
                    _add_custom(conn, oid, mid, 'near', k, nk, credit=round(cr, 2))
        for nk, cr in lex[k]['near'].items():               # the dictionary's own nearby skills
            if nk in lex and not any(x['key'] == nk for x in near):
                near.append({'key': nk, 'name': lex[nk]['name'], 'credit': cr})
        out.append({'key': k, 'name': lex[k]['name'], 'importance': imp if imp in IMPORTANCE else 'important',
                    'min_years': num(it.get('min_years'), 0, 40), 'involvement': inv if inv in INVOLVEMENT else 'any',
                    'recent_years': num(it.get('recent_years'), 1, 20), 'why': _clean(it.get('why'), 160),
                    'near': near[:6], 'pending': bool(lex[k].get('pending')), 'domain': lex[k]['domain']})
    order = {'core': 0, 'important': 1, 'nice': 2}
    out.sort(key=lambda x: order[x['importance']])
    return {'skills': out, 'notes': _clean(raw.get('notes') if isinstance(raw, dict) else '', 300)}


def generate_spec(conn, oid, m, ask=None):
    core = _core()
    key = core.get_setting('deepseek_api_key')
    if not key and not ask:
        raise RuntimeError('DeepSeek is not set up. Add the key in Settings → Admin → AI API Keys.')
    txt = _job_text(conn, core, m)
    lex = lexicon(conn, oid, m['id'])
    user = 'DICTIONARY (key=name): %s\n\nTHE JOB:\n%s' % (', '.join('%s=%s' % (k, v['name']) for k, v in lex.items()), txt[:9000])
    if ask:
        raw = ask(user)
    else:
        resp = core.call_deepseek(key, {
            'model': 'deepseek-chat', 'temperature': 0.2, 'max_tokens': 2200, 'response_format': {'type': 'json_object'},
            'messages': [{'role': 'system', 'content': _SPEC_SYSTEM}, {'role': 'user', 'content': user}]},
            timeout=75, endpoint='skill-spec')
        if resp.status_code != 200:
            raise RuntimeError('DeepSeek HTTP %s' % resp.status_code)
        raw = core.parse_json(resp.json()['choices'][0]['message']['content'] or '') or {}
    spec = clean_spec(conn, oid, m['id'], raw, from_ai=True)
    if not spec['skills']:
        raise ValueError('The AI returned no skills for this job.')
    h = hashlib.sha1(_job_text(conn, core, m).encode('utf-8')).hexdigest()
    conn.execute('INSERT OR REPLACE INTO mandate_skill_spec (owner_id, mandate_id, spec, status, source_hash, generated_at, '
                 'edited_by, edited_at) VALUES (?,?,?,?,?,?,?,?)', (oid, m['id'], json.dumps(spec), 'ai', h, ts(), 0, ''))
    conn.commit()
    return spec


def _pack_spec(conn, oid, m, r):
    try:
        spec = json.loads(r['spec'] or '{}')
    except Exception:
        spec = {}
    lex = lexicon(conn, oid, m['id'])
    for s in spec.get('skills', []):                       # pending flags change when an admin approves
        s['pending'] = bool(lex.get(s['key'], {}).get('pending'))
    h = hashlib.sha1(_job_text(conn, _core(), m).encode('utf-8')).hexdigest()
    return {'spec': spec, 'status': r['status'], 'generated_at': r['generated_at'] or '', 'edited_at': r['edited_at'] or '',
            'stale': bool(r['source_hash'] and r['source_hash'] != h)}


def ensure_spec(conn, oid, m, create=True, ask=None):
    r = _spec_row(conn, oid, m['id'])
    if r:
        packed = _pack_spec(conn, oid, m, r)
        if not packed['stale'] or r['status'] == 'edited' or not create:
            return packed
    elif not create:
        return None
    from modules import job_profile as jp
    if not jp.has_job_content(_core(), m):
        return None
    generate_spec(conn, oid, m, ask)
    return _pack_spec(conn, oid, m, _spec_row(conn, oid, m['id']))


# ══════════════════════════════════════════════════════════════════════════
#  Background: new candidates + the admin backfill (same pattern as tagging)
# ══════════════════════════════════════════════════════════════════════════
def _since(hours):
    try:
        now = _core()._ist_now()
    except Exception:
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    return (now - datetime.timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%S')


def _capped(oid):
    try:
        return bool(_core().over_token_cap(oid))
    except Exception:
        return False


_TODO = ("FROM candidates c LEFT JOIN candidate_skill_depth d ON d.candidate_id=c.id WHERE d.candidate_id IS NULL "
         "AND COALESCE(c.owner_id,0)>0")


def _remaining(conn, oid):
    return conn.execute('SELECT COUNT(*) ' + _TODO + ' AND c.owner_id=?', (oid,)).fetchone()[0]


def _next_job(conn):
    seen = {}
    for r in conn.execute('SELECT c.id, c.owner_id ' + _TODO + ' AND c.created_at>=? ORDER BY c.id DESC LIMIT 50',
                          (_since(NEW_WINDOW_HOURS),)).fetchall():
        oid = r['owner_id']
        if oid not in seen:
            seen[oid] = engine_on(conn, oid) and not _capped(oid)
        if seen[oid]:
            return 'new', oid, r['id']
    for b in conn.execute("SELECT owner_id FROM skill_backfill WHERE status='running' ORDER BY updated_at").fetchall():
        oid = b['owner_id']
        if _capped(oid):
            conn.execute("UPDATE skill_backfill SET status='capped', message=?, updated_at=? WHERE owner_id=?",
                         ('Stopped: the monthly AI limit is reached.', ts(), oid))
            conn.commit()
            continue
        r = conn.execute('SELECT c.id ' + _TODO + ' AND c.owner_id=? ORDER BY COALESCE(c.updated_at, c.created_at) DESC, '
                         'c.id DESC LIMIT 1', (oid,)).fetchone()
        if not r:
            conn.execute("UPDATE skill_backfill SET status='done', message='', finished_at=?, updated_at=? WHERE owner_id=?",
                         (ts(), ts(), oid))
            conn.commit()
            continue
        return 'backfill', oid, r['id']
    return None


_tick_lock = threading.Lock()


def tick(classify=None):
    """Analyse one person. True when an AI call was made (the loop then waits)."""
    with _tick_lock:
        core = _core()
        conn = get_db()
        try:
            job = _next_job(conn)
            if not job:
                return False
            kind, oid, cid = job
            core._bg_tenant_set(oid)
            try:
                try:
                    data = compute_depth(conn, oid, cid, use_ai=True, classify=classify)
                    failed = 0
                except Exception as e:
                    if type(e).__name__ == 'TokenCapError':
                        raise
                    conn.rollback()
                    conn.execute('INSERT OR REPLACE INTO candidate_skill_depth (candidate_id, owner_id, status, error, '
                                 'computed_at) VALUES (?,?,?,?,?)', (cid, oid, 'error', str(e)[:200], ts()))
                    data, failed = None, 1
                if kind == 'backfill':
                    conn.execute('UPDATE skill_backfill SET done=done+1, failed=failed+?, updated_at=? WHERE owner_id=?',
                                 (failed, ts(), oid))
                conn.commit()
                return bool(data and data.get('ai'))
            except Exception as e:
                conn.rollback()
                if type(e).__name__ == 'TokenCapError':
                    conn.execute("UPDATE skill_backfill SET status='capped', message=?, updated_at=? WHERE owner_id=?",
                                 ('Stopped: the monthly AI limit is reached.', ts(), oid))
                    conn.commit()
                    return False
                raise
            finally:
                core._bg_tenant_clear()
        finally:
            conn.close()


def _has_more():
    try:
        conn = get_db()
        try:
            return _next_job(conn) is not None
        finally:
            conn.close()
    except Exception:
        return False


def _loop():
    time.sleep(50)
    while True:
        try:
            r = tick()
        except Exception as e:
            print('[skill-engine] worker error: %s' % e)
            r = None
        time.sleep(THROTTLE_SECONDS if r is True else (0.2 if r is False and _has_more() else IDLE_SECONDS))


_started = False


def start_worker():
    global _started
    if _started or 'pytest' in sys.modules or os.environ.get('HIRELAB_NO_WORKERS'):
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name='skill-engine').start()
    print('[skill-engine] background thread started')


start_worker()


# ══════════════════════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════════════════════
@pages.route('/skill-engine.js')
def skill_js():
    with open(os.path.join(ROOT, 'skill-engine.js'), encoding='utf-8') as fh:
        return Response(fh.read(), mimetype='application/javascript', headers={'Cache-Control': 'no-cache'})


def _off(conn):
    if not engine_on(conn, effective_company_id()):
        return jsonify({'error': 'The Skill Engine is switched off for your agency.', 'off': True}), 404
    return None


@bp.route('/api/candidates/<int:cid>/skill-depth', methods=['GET'])
@login_required
def get_depth(cid):
    blocked = _fl_block()
    if blocked:
        return blocked
    conn = get_db()
    try:
        off = _off(conn)
        if off:
            return off
        c = _own_cand(conn, cid)
        if not c:
            return jsonify({'error': 'Not found'}), 404
        r = conn.execute('SELECT * FROM candidate_skill_depth WHERE candidate_id=? AND owner_id=?',
                         (cid, effective_company_id())).fetchone()
        if not r or r['status'] != 'done':
            return jsonify({'ok': True, 'data': None, 'error': (r['error'] if r else '')})
        spans, _n = _spans(conn, cid)
        cv = ''
        try:
            cv = _core()._candidate_cv_text(c, CV_CHARS) or ''
        except Exception:
            pass
        stale = r['input_hash'] != _input_hash(c, spans, cv)
        return jsonify({'ok': True, 'data': json.loads(r['data'] or '{}'), 'stale': stale, 'computed_at': r['computed_at']})
    finally:
        conn.close()


@bp.route('/api/candidates/<int:cid>/skill-depth', methods=['POST'])
@login_required
def post_depth(cid):
    blocked = _fl_block()
    if blocked:
        return blocked
    oid = effective_company_id()
    conn = get_db()
    try:
        off = _off(conn)
        if off:
            return off
        if not _own_cand(conn, cid):
            return jsonify({'error': 'Not found'}), 404
        try:
            data = compute_depth(conn, oid, cid, use_ai=True)
        except Exception as e:
            if type(e).__name__ == 'TokenCapError':
                return jsonify({'error': 'The agency\'s monthly AI limit is reached.'}), 429
            raise
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True, 'data': data, 'stale': False})


@bp.route('/api/mandates/<int:mid>/skill-spec', methods=['GET'])
@login_required
def get_spec(mid):
    blocked = _fl_block()
    if blocked:
        return blocked
    conn = get_db()
    try:
        off = _off(conn)
        if off:
            return off
        m = _own_mandate(conn, mid)
        if not m:
            return jsonify({'error': 'Not found'}), 404
        try:
            got = ensure_spec(conn, effective_company_id(), m, create=request.args.get('ensure') == '1')
        except Exception as e:
            if type(e).__name__ == 'TokenCapError':
                return jsonify({'error': 'The agency\'s monthly AI limit is reached.'}), 429
            return jsonify({'ok': True, 'spec': None, 'error': str(e)[:200]})
        conn.commit()
    finally:
        conn.close()
    return jsonify(dict(got or {'spec': None}, ok=True))


@bp.route('/api/mandates/<int:mid>/skill-spec/generate', methods=['POST'])
@login_required
def regen_spec(mid):
    blocked = _fl_block()
    if blocked:
        return blocked
    oid = effective_company_id()
    conn = get_db()
    try:
        off = _off(conn)
        if off:
            return off
        m = _own_mandate(conn, mid)
        if not m:
            return jsonify({'error': 'Not found'}), 404
        try:
            generate_spec(conn, oid, m)
        except Exception as e:
            if type(e).__name__ == 'TokenCapError':
                return jsonify({'error': 'The agency\'s monthly AI limit is reached.'}), 429
            return jsonify({'error': str(e)[:200]}), 400
        got = _pack_spec(conn, oid, m, _spec_row(conn, oid, mid))
    finally:
        conn.close()
    return jsonify(dict(got, ok=True))


@bp.route('/api/mandates/<int:mid>/skill-spec', methods=['PUT'])
@login_required
def save_spec(mid):
    blocked = _fl_block()
    if blocked:
        return blocked
    oid = effective_company_id()
    body = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        off = _off(conn)
        if off:
            return off
        m = _own_mandate(conn, mid)
        if not m:
            return jsonify({'error': 'Not found'}), 404
        spec = clean_spec(conn, oid, mid, {'skills': body.get('skills') or [], 'notes': body.get('notes', '')}, from_ai=False)
        if not spec['skills']:
            return jsonify({'error': 'Keep at least one skill.'}), 400
        prev = _spec_row(conn, oid, mid)
        conn.execute('INSERT OR REPLACE INTO mandate_skill_spec (owner_id, mandate_id, spec, status, source_hash, '
                     'generated_at, edited_by, edited_at) VALUES (?,?,?,?,?,?,?,?)',
                     (oid, mid, json.dumps(spec), 'edited', prev['source_hash'] if prev else '',
                      prev['generated_at'] if prev else '', _uid(), ts()))
        conn.commit()
        got = _pack_spec(conn, oid, m, _spec_row(conn, oid, mid))
    finally:
        conn.close()
    return jsonify(dict(got, ok=True))


@bp.route('/api/skills/dictionary', methods=['GET'])
@login_required
def dictionary():
    blocked = _fl_block()
    if blocked:
        return blocked
    q = _norm(request.args.get('q'))
    try:
        mid = int(request.args.get('mid') or 0)
    except ValueError:
        mid = 0
    conn = get_db()
    try:
        if mid and not _own_mandate(conn, mid):
            mid = 0
        lex = lexicon(conn, effective_company_id(), mid)
    finally:
        conn.close()
    t = taxonomy()
    rows = [v for v in lex.values() if not q or q in _norm(v['name']) or any(q in a for a in v['aliases'])]
    rows.sort(key=lambda v: (not _norm(v['name']).startswith(q), v['name'].lower()))
    return jsonify({'ok': True, 'total': len(lex), 'domains': t.DOMAINS, 'categories': t.CATEGORIES,
                    'skills': [{'key': v['key'], 'name': v['name'], 'domain': v['domain'], 'category': v['category'],
                                'aliases': v['aliases'][:12], 'custom': v['custom'], 'pending': v['pending']} for v in rows[:40]]})


@bp.route('/api/skills/config', methods=['GET'])
@login_required
def config():
    conn = get_db()
    try:
        oid = effective_company_id()
        on = engine_on(conn, oid)
        pending = conn.execute("SELECT COUNT(*) FROM skill_custom WHERE owner_id=? AND status='pending'", (oid,)).fetchone()[0]
    finally:
        conn.close()
    return jsonify({'ok': True, 'on': on, 'is_admin': bool(is_company_admin()), 'pending': pending,
                    'base_skills': len(taxonomy().BASE)})


@bp.route('/api/skills/config', methods=['POST'])
@login_required
def set_config():
    if not is_company_admin():
        return jsonify({'error': 'Only an admin can change this.'}), 403
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        conn.execute('INSERT OR REPLACE INTO insight_settings (owner_id, key, value, updated_at) VALUES (?,?,?,?)',
                     (effective_company_id(), 'skill_engine_on', '1' if d.get('on') else '0', ts()))
        conn.commit()
        on = engine_on(conn, effective_company_id())
    finally:
        conn.close()
    return jsonify({'ok': True, 'on': on})


@bp.route('/api/skills/suggestions', methods=['GET'])
@login_required
def suggestions():
    if not is_company_admin():
        return jsonify({'error': 'Only an admin can see this.'}), 403
    oid = effective_company_id()
    conn = get_db()
    try:
        lex = lexicon(conn, oid)
        rows = conn.execute("SELECT s.*, m.role AS m_role, m.client AS m_client FROM skill_custom s LEFT JOIN mandates m "
                            "ON m.id=s.mandate_id AND m.owner_id=s.owner_id WHERE s.owner_id=? AND s.status='pending' "
                            'ORDER BY s.id DESC LIMIT 300', (oid,)).fetchall()
        names = {r['skill_key']: r['value'] for r in rows if r['kind'] == 'skill'}
    finally:
        conn.close()

    def nm(k):
        return lex[k]['name'] if k in lex else names.get(k, k)
    return jsonify({'ok': True, 'suggestions': [
        {'id': r['id'], 'kind': r['kind'], 'skill': nm(r['skill_key']), 'skill_key': r['skill_key'],
         'value': nm(r['value']) if r['kind'] == 'near' else r['value'], 'credit': r['credit'],
         'job': ' · '.join(x for x in (r['m_role'], r['m_client']) if x), 'date': (r['created_at'] or '')[:10]}
        for r in rows]})


@bp.route('/api/skills/suggestions/<int:sid>', methods=['POST'])
@login_required
def decide(sid):
    if not is_company_admin():
        return jsonify({'error': 'Only an admin can do this.'}), 403
    dec = str((request.get_json(silent=True) or {}).get('decision') or '')
    if dec not in ('approve', 'reject'):
        return jsonify({'error': 'decision must be approve or reject'}), 400
    conn = get_db()
    try:
        cur = conn.execute("UPDATE skill_custom SET status=?, decided_by=?, decided_at=? WHERE id=? AND owner_id=? "
                           "AND status='pending'", ('approved' if dec == 'approve' else 'rejected', _uid(), ts(), sid,
                                                    effective_company_id()))
        conn.commit()
        if not cur.rowcount:
            return jsonify({'error': 'Not found'}), 404
    finally:
        conn.close()
    return jsonify({'ok': True})


def _bf_status(conn, oid):
    b = conn.execute('SELECT * FROM skill_backfill WHERE owner_id=?', (oid,)).fetchone()
    d = dict(b) if b else {'status': 'idle', 'total': 0, 'done': 0, 'failed': 0, 'message': ''}
    d.pop('owner_id', None)
    d['remaining'] = _remaining(conn, oid)
    d['analysed'] = conn.execute("SELECT COUNT(*) FROM candidate_skill_depth WHERE owner_id=? AND status='done'",
                                 (oid,)).fetchone()[0]
    return d


@bp.route('/api/skills/backfill/status', methods=['GET'])
@login_required
def bf_status():
    if not is_company_admin():
        return jsonify({'error': 'Only an admin can see this.'}), 403
    conn = get_db()
    try:
        return jsonify({'ok': True, 'backfill': _bf_status(conn, effective_company_id())})
    finally:
        conn.close()


@bp.route('/api/skills/backfill/start', methods=['POST'])
@login_required
def bf_start():
    if not is_company_admin():
        return jsonify({'error': 'Only an admin can start this.'}), 403
    oid = effective_company_id()
    conn = get_db()
    try:
        conn.execute("DELETE FROM candidate_skill_depth WHERE owner_id=? AND status='error'", (oid,))
        total = _remaining(conn, oid)
        b = conn.execute('SELECT status FROM skill_backfill WHERE owner_id=?', (oid,)).fetchone()
        if b and b['status'] == 'running':
            conn.execute('UPDATE skill_backfill SET total=done+?, updated_at=? WHERE owner_id=?', (total, ts(), oid))
        else:
            conn.execute('INSERT OR REPLACE INTO skill_backfill (owner_id, status, total, done, failed, message, started_by, '
                         'started_at, updated_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (oid, 'running' if total else 'done', total, 0, 0, '', _uid(), ts(), ts(), '' if total else ts()))
        conn.commit()
        return jsonify({'ok': True, 'backfill': _bf_status(conn, oid)})
    finally:
        conn.close()


@bp.route('/api/skills/backfill/stop', methods=['POST'])
@login_required
def bf_stop():
    if not is_company_admin():
        return jsonify({'error': 'Only an admin can stop this.'}), 403
    oid = effective_company_id()
    conn = get_db()
    try:
        conn.execute("UPDATE skill_backfill SET status='stopped', message='Stopped by an admin.', updated_at=? "
                     "WHERE owner_id=? AND status='running'", (ts(), oid))
        conn.commit()
        return jsonify({'ok': True, 'backfill': _bf_status(conn, oid)})
    finally:
        conn.close()
