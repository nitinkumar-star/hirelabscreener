"""
Talent Pool — segmentation + CV freshness (Wave 2 of candidate engagement).

ONE row per PERSON (talent_profiles): a person is one e-mail address, or —
when there is no e-mail — one candidate record chain (copied_from root).

Segments (fixed lists, edit INDUSTRIES / FUNCTIONS below to change them):
  * industries   multi   Data Center · Solar & Renewables · Electrical & Power ·
                         Industrial Automation · Testing, Inspection & Certification
  * functions    multi   Projects & Execution · Design & Engineering · O&M / Facility ·
                         Sales, BD & Tendering · QA/QC & Commissioning · HSE ·
                         Procurement & SCM · Leadership
  * seniority    one     Junior (<4 yrs) · Mid (4–10) · Senior (10–15) · Leadership (15+)
  * region       one     NCR · North · West · South · East · Central · Overseas · Unknown

Who sets them — nobody has to do it by hand:
  1. rules      keyword scoring over designation, skills, AI tags, summary and
                the job the person came from (free, instant, background thread);
  2. ai         DeepSeek, only for people the rules could not place, capped per
                day, switched on by an admin;
  3. candidate  what the candidate ticks on the update-profile page ("what would
                you like to hear about") — wins over rules/AI for industries/functions;
  4. manual     a recruiter edits the chip on the profile — wins over everything.

CV freshness: cv_date = latest of (a record with a CV was created, the person
submitted the update-profile form). Fresh = within N days (default 180).
Every update-profile submission is logged durably in talent_updates with a
"what changed" summary (company, designation, CTC, notice, location, CV) —
candidates.update_submitted_at is cleared when the task is dismissed, so it
cannot be used for history.

Additive only: new tables talent_profiles, talent_updates, talent_settings.
"""

import datetime
import hashlib
import json
import os
import re
import sys
import threading
import time

from flask import Blueprint, request, jsonify, redirect, Response, g

from modules.shared import get_db, ts, effective_company_id, real_user_id, is_company_admin, login_required, _core
from modules import register_migration

bp = Blueprint('segments', __name__, url_prefix='/api/segments')
pages = Blueprint('segments_pages', __name__)
cand_bp = Blueprint('segments_cand', __name__)

IST = datetime.timedelta(hours=5, minutes=30)
TICK_SECONDS = 600
AI_BATCH = 15

# ── taxonomy ───────────────────────────────────────────────────────────────
# (key, label, [(pattern, weight)])  — patterns are regex on lower-case text;
# \b is added around short tokens so "pv" does not match "pvt".
INDUSTRIES = [
    ('data_center', 'Data Center', [
        ('data ?cent(er|re)s?', 3), ('datacent(er|re)', 3), ('hyperscale', 3), ('colocation', 3), ('critical facilit', 3),
        ('mission critical', 3), ('dcim', 3), ('tier ?(iii|iv|3|4)', 2), ('precision cooling', 2), ('crah', 2), ('crac', 2),
        ('chiller', 1), (r'\bups\b', 1), ('uptime', 1), ('white space', 2)]),
    ('solar_re', 'Solar & Renewables', [
        ('solar', 3), (r'\bpv\b', 3), ('photovoltaic', 3), ('renewable', 3), (r'\bwind\b', 2), ('wind (farm|turbine|power)', 3),
        (r'\bbess\b', 3), ('battery energy storage', 3), ('green hydrogen', 3), ('rooftop', 2), ('pvsyst', 3), (r'\bipp\b', 2),
        ('inverter', 1), (r'\bmwp\b', 2), (r'\bkwp\b', 2), ('module mounting', 2), ('string (inverter|combiner)', 2)]),
    ('electrical', 'Electrical & Power', [
        ('switchgear', 3), ('substation', 3), ('transformer', 2), (r'\behv\b', 3), ('transmission line', 3),
        ('power distribution', 3), (r'\b(lt|ht|lv|mv|hv)\b ?panel', 2), (r'\b(gis|ais)\b', 2), ('protection relay', 2),
        ('busbar|bus duct|busway', 2), (r'\b\d{2,3} ?kv\b', 2), ('power system', 2), (r'\b(mcc|pcc)\b', 2), ('cable laying', 1),
        ('electrical', 1), ('genset|dg set', 1), ('earthing|lightning protection', 1), ('energy meter|metering', 1)]),
    ('automation', 'Industrial Automation', [
        (r'\bplc\b', 3), (r'\bscada\b', 3), (r'\bdcs\b', 3), (r'\bhmi\b', 2), ('industrial automation', 3), ('automation', 2),
        ('instrumentation', 2), (r'\bvfd\b|variable frequency drive', 2), ('drives', 1), ('robotic', 2), ('process control', 2),
        ('control system', 2), ('allen.?bradley|rockwell|siemens s7|tia portal|wincc', 3), (r'\bbms\b|building management', 2),
        (r'\biiot\b', 2), ('motion control', 2)]),
    ('tic', 'Testing, Inspection & Certification', [
        ('certification', 2), ('inspection', 2), ('third party inspection', 3), (r'\btpi\b', 3), ('testing laborator', 3),
        (r'\bnabl\b', 3), ('calibration', 2), (r't(u|ü)v', 3), ('bureau veritas', 3), (r'\bsgs\b', 3), ('intertek', 3),
        (r'\bdekra\b', 3), ('lloyd', 2), (r'\bul solutions\b|underwriters laborator', 3), ('conformity assessment', 3),
        ('type testing', 2), ('audit', 1)]),
]
FUNCTIONS = [
    ('projects', 'Projects & Execution', [
        ('project (manager|engineer|lead|head|in.?charge|execution|management)', 3), ('execution', 2), ('site (engineer|in.?charge|manager)', 3),
        ('construction', 2), ('installation', 2), ('erection', 2), ('planning engineer|project planning', 2), (r'\bepc\b', 1)]),
    ('design', 'Design & Engineering', [
        ('design', 3), ('detail(ed)? engineering', 3), ('basic engineering', 3), ('drafts?man|drafting', 3), ('autocad', 2),
        ('etap|dialux|pvsyst|revit|e3d|smartplant', 2), (r'\bsld\b|single line diagram', 2), ('layout', 1), ('engineering manager', 2)]),
    ('om', 'O&M / Facility', [
        (r'o ?& ?m\b|o and m\b', 3), ('operation(s)? (and|&) maintenance', 3), ('maintenance', 2), ('facility|facilities', 2),
        ('plant operation', 3), ('asset management', 2), ('field service|service engineer', 2), ('shift (engineer|in.?charge)', 2),
        ('critical environment', 2)]),
    ('sales', 'Sales, BD & Tendering', [
        ('sales', 3), ('business development', 3), (r'\bbd\b', 2), ('key account', 3), ('pre.?sales', 3), ('tender', 2),
        ('bid(ding)?\b', 2), ('proposal', 2), ('estimation', 1), ('channel (partner|sales)', 3), ('marketing', 2), ('commercial', 1)]),
    ('qaqc', 'QA/QC & Commissioning', [
        (r'\bqa\b|\bqc\b|qa/?qc', 3), ('quality', 2), ('commissioning', 3), ('testing (and|&) commissioning|t ?& ?c\b', 3),
        ('inspector', 3), ('inspection', 1), ('audit', 1)]),
    ('hse', 'HSE', [
        (r'\bhse\b|\behs\b|\bshe\b', 3), ('safety', 3), ('environment,? health', 3)]),
    ('procurement', 'Procurement & SCM', [
        ('procurement', 3), ('purchase', 3), ('supply chain', 3), (r'\bscm\b', 3), ('vendor development', 3),
        ('contracts? (manager|management)', 2), ('buyer', 2), ('logistics', 2)]),
    ('leadership', 'Leadership', [
        (r'\b(head|vice president|vp|avp|director|general manager|gm|ceo|coo|cto|cfo|president|chief|country manager|business head)\b', 3)]),
]
SENIORITY = [('junior', 'Junior (0–4 yrs)'), ('mid', 'Mid (4–10 yrs)'), ('senior', 'Senior (10–15 yrs)'),
             ('leadership', 'Leadership (15+ yrs)'), ('unknown', 'Unknown')]
REGIONS = [
    ('ncr', 'Delhi NCR', r'delhi|noida|gurgaon|gurugram|ghaziabad|faridabad|\bncr\b|greater noida|manesar|sonipat|bahadurgarh'),
    ('north', 'North', r'chandigarh|mohali|panchkula|jaipur|lucknow|kanpur|ludhiana|amritsar|jalandhar|dehradun|haridwar|'
                       r'punjab|haryana|rajasthan|uttarakhand|uttar pradesh|himachal|shimla|jammu|srinagar|varanasi|agra|'
                       r'jodhpur|udaipur|bikaner|meerut|panipat|karnal|hisar|rohtak|baddi'),
    ('west', 'West', r'mumbai|navi mumbai|thane|pune|ahmedabad|gandhinagar|vadodara|baroda|surat|rajkot|nashik|nagpur|'
                     r'aurangabad|gujarat|maharashtra|goa|kutch|bhuj|mundra|jamnagar|vapi|silvassa|kolhapur'),
    ('south', 'South', r'bangalore|bengaluru|chennai|hyderabad|secunderabad|kochi|cochin|trivandrum|thiruvananthapuram|'
                       r'coimbatore|madurai|mysore|mysuru|mangalore|vizag|visakhapatnam|vijayawada|tirupati|hosur|'
                       r'kerala|tamil ?nadu|karnataka|telangana|andhra|pondicherry|puducherry|anantapur|kurnool'),
    ('east', 'East', r'kolkata|calcutta|bhubaneswar|odisha|orissa|patna|bihar|jharkhand|ranchi|jamshedpur|guwahati|assam|'
                     r'west bengal|durgapur|siliguri|cuttack|rourkela|dhanbad|bokaro|northeast|meghalaya|tripura'),
    ('central', 'Central', r'indore|bhopal|raipur|bilaspur|jabalpur|gwalior|madhya pradesh|chhattisgarh|ujjain|korba'),
    ('overseas', 'Overseas', r'dubai|abu dhabi|\buae\b|saudi|riyadh|jeddah|qatar|doha|oman|muscat|kuwait|bahrain|'
                             r'singapore|malaysia|\busa\b|united states|\buk\b|united kingdom|london|germany|australia|canada|'
                             r'africa|nigeria|kenya|abroad|gulf|middle east'),
]
REGION_KEYS = [r[0] for r in REGIONS] + ['unknown']
REGION_LABELS = dict([(r[0], r[1]) for r in REGIONS] + [('unknown', 'Unknown')])
IND_LABELS = {k: l for k, l, _ in INDUSTRIES}
FUN_LABELS = {k: l for k, l, _ in FUNCTIONS}
SEN_LABELS = dict(SENIORITY)
_COMPILED = {}
DEFAULTS = {'fresh_days': 180, 'ai_enabled': 0, 'ai_daily': 300}
LIVE_STAGES = {'shared with client', 'interview inprocess', 'interview in process', 'interview', 'offered',
               'offer', 'offer accepted', 'placed', 'joined'}
WATCH = [('company', 'Company'), ('designation', 'Designation'), ('ctc_current', 'Current CTC'),
         ('ctc_expected', 'Expected CTC'), ('notice_period', 'Notice period'), ('location', 'Location'),
         ('preferred_location', 'Preferred location'), ('experience', 'Experience'), ('phone', 'Phone'), ('email', 'E-mail')]


@register_migration
def _migrate_segments(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS talent_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        person_key TEXT NOT NULL,
        email TEXT DEFAULT '',
        candidate_id INTEGER DEFAULT 0,
        record_ids TEXT DEFAULT '[]',
        name TEXT DEFAULT '',
        designation TEXT DEFAULT '',
        company TEXT DEFAULT '',
        experience REAL DEFAULT 0,
        location TEXT DEFAULT '',
        industries TEXT DEFAULT '[]',
        functions TEXT DEFAULT '[]',
        seniority TEXT DEFAULT 'unknown',
        region TEXT DEFAULT 'unknown',
        seg_source TEXT DEFAULT '',
        seg_scores TEXT DEFAULT '{}',
        manual INTEGER DEFAULT 0,
        ai_tried INTEGER DEFAULT 0,
        text_hash TEXT DEFAULT '',
        interest_industries TEXT DEFAULT '[]',
        interest_functions TEXT DEFAULT '[]',
        interests_at TEXT DEFAULT '',
        cv_date TEXT DEFAULT '',
        last_update_at TEXT DEFAULT '',
        emailable INTEGER DEFAULT 0,
        live INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT '',
        UNIQUE(company_id, person_key)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS talent_updates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        candidate_id INTEGER NOT NULL,
        email TEXT DEFAULT '',
        name TEXT DEFAULT '',
        changes TEXT DEFAULT '[]',
        summary TEXT DEFAULT '',
        with_cv INTEGER DEFAULT 0,
        created_at TEXT DEFAULT ''
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS talent_settings (
        company_id INTEGER PRIMARY KEY,
        fresh_days INTEGER DEFAULT 180,
        ai_enabled INTEGER DEFAULT 0,
        ai_daily INTEGER DEFAULT 300,
        ai_used_day TEXT DEFAULT '',
        ai_used INTEGER DEFAULT 0,
        tick_at TEXT DEFAULT '',
        last_scan_at TEXT DEFAULT '',
        last_error TEXT DEFAULT '',
        updated_by INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT ''
    )''')
    for sql in ('CREATE INDEX IF NOT EXISTS idx_tp_email ON talent_profiles(company_id, email)',
                'CREATE INDEX IF NOT EXISTS idx_tp_cand ON talent_profiles(candidate_id)',
                'CREATE INDEX IF NOT EXISTS idx_tu_co ON talent_updates(company_id, created_at)',
                'CREATE INDEX IF NOT EXISTS idx_tu_email ON talent_updates(company_id, email)',
                'CREATE INDEX IF NOT EXISTS idx_tu_cand ON talent_updates(candidate_id)'):
        conn.execute(sql)
    conn.commit()


# ── helpers ────────────────────────────────────────────────────────────────
def now_ist():
    return datetime.datetime.utcnow() + IST


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _err(msg, code=400):
    return jsonify({'ok': False, 'error': msg}), code


def _oid():
    return int(effective_company_id() or 0)


def _valid_email(em):
    em = str(em or '').strip().lower()
    return em if ('@' in em and '.' in em.split('@')[-1] and ' ' not in em) else ''


def _jl(v):
    try:
        x = json.loads(v or '[]')
        return x if isinstance(x, list) else []
    except Exception:
        return []


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def get_settings(conn, oid):
    r = conn.execute('SELECT * FROM talent_settings WHERE company_id=?', (oid,)).fetchone()
    s = dict(DEFAULTS, last_scan_at='', last_error='', ai_used=0, ai_used_day='')
    if r:
        for k in list(s):
            if k in r.keys() and r[k] is not None:
                s[k] = r[k]
    return s


def _pat(p):
    c = _COMPILED.get(p)
    if c is None:
        c = _COMPILED[p] = re.compile(p)
    return c


def _score(table, weighted_texts):
    """{key: score} for a taxonomy table over [(text, field_weight)]."""
    out = {}
    for key, _label, pats in table:
        s = 0
        for txt, fw in weighted_texts:
            if not txt:
                continue
            for p, w in pats:
                if _pat(p).search(txt):
                    s += w * fw
        if s:
            out[key] = s
    return out


def _pick(scores, floor):
    if not scores:
        return []
    top = max(scores.values())
    return [k for k, v in sorted(scores.items(), key=lambda x: -x[1]) if v >= floor and v >= top * 0.45][:3]


def seniority_of(exp, title=''):
    e = _num(exp)
    if re.search(r'\b(head|vice president|vp|avp|director|general manager|ceo|coo|cto|president|chief|country manager)\b',
                 str(title or '').lower()) and e >= 10:
        return 'leadership'
    if e <= 0:
        return 'unknown'
    return 'junior' if e < 4 else 'mid' if e < 10 else 'senior' if e < 15 else 'leadership'


def region_of(location, preferred=''):
    for txt in (str(location or '').lower(), str(preferred or '').lower()):
        if not txt.strip():
            continue
        for key, _l, p in REGIONS:
            if _pat(p).search(txt):
                return key
    return 'unknown'


def classify(person):
    """Rules: (industries, functions, scores) for one person dict of texts."""
    title = person.get('title', '')
    texts = [(title, 3), (person.get('job', ''), 2), (person.get('skills', ''), 2), (person.get('tags', ''), 2),
             (person.get('company', ''), 1), (person.get('summary', ''), 1)]
    ind = _score(INDUSTRIES, texts)
    fun = _score(FUNCTIONS, [(title, 3), (person.get('tags', ''), 1), (person.get('skills', ''), 1), (person.get('summary', ''), 1)])
    industries = _pick(ind, 4)
    functions = [f for f in _pick(fun, 3) if f != 'leadership']
    if 'leadership' in fun and fun['leadership'] >= 9:
        functions = (functions + ['leadership'])[:3]
    return industries, functions, {'industries': ind, 'functions': fun}


# ══════════════════════════════════════════════════════════════════════════
#  The scan — builds / refreshes talent_profiles for one company
# ══════════════════════════════════════════════════════════════════════════
def _person_key(row):
    em = _valid_email(row['email'])
    return em if em else 'id:%d' % (int(row['copied_from'] or 0) or int(row['id']))


def _cand_cols(conn):
    return {r[1] for r in conn.execute('PRAGMA table_info(candidates)')}


def scan_company(conn, oid):
    """Rules pass over every person of a company. Returns counts."""
    have = _cand_cols(conn)
    want = ['id', 'email', 'copied_from', 'name', 'designation', 'company', 'experience', 'location', 'preferred_location',
            'key_skills', 'key_skill_tags', 'domain_tags', 'function_tags', 'industry_background', 'career_summary',
            'cv_path', 'created_at', 'stage', 'do_not_email', 'reengage_off', 'mandate_id']
    cols = ', '.join('c.' + c for c in want if c in have)
    rows = _rows(conn, 'SELECT %s, m.role AS m_role, m.status AS m_status FROM candidates c '
                       'LEFT JOIN mandates m ON m.id=c.mandate_id WHERE c.owner_id=?' % cols, (oid,))
    upd = {}
    for r in conn.execute('SELECT LOWER(TRIM(email)) AS em, candidate_id, MAX(created_at) AS last FROM talent_updates '
                          'WHERE company_id=? GROUP BY em, candidate_id', (oid,)):
        for k in (r['em'], 'cid:%d' % r['candidate_id']):
            if k and (k not in upd or str(r['last']) > upd[k]):
                upd[k] = str(r['last'])
    groups = {}
    for r in rows:
        groups.setdefault(_person_key(r), []).append(r)
    existing = {r['person_key']: r for r in _rows(conn, 'SELECT person_key, text_hash, manual, industries, functions, '
                                                        'interest_industries, interest_functions, seg_source, ai_tried '
                                                        'FROM talent_profiles WHERE company_id=?', (oid,))}
    now = ts()
    n_new = n_changed = 0
    for key, recs in groups.items():
        recs.sort(key=lambda r: (str(r.get('created_at') or ''), r['id']), reverse=True)
        lt = recs[0]
        tags = ' '.join(' '.join(str(x) for x in _jl(r.get(c))) for r in recs for c in
                        ('key_skill_tags', 'domain_tags', 'function_tags'))
        skills = ' '.join(' '.join(str(x) for x in _jl(r.get('key_skills'))) for r in recs[:3])
        person = {
            'title': ' '.join(str(r.get('designation') or '') for r in recs[:3]).lower(),
            'job': ' '.join(str(r.get('m_role') or '') for r in recs if (r.get('m_status') or '') != 'central').lower(),
            'skills': skills.lower(), 'tags': tags.lower(),
            'company': ' '.join(str(r.get('company') or '') + ' ' + str(r.get('industry_background') or '') for r in recs[:3]).lower(),
            'summary': str(lt.get('career_summary') or '')[:800].lower(),
        }
        h = hashlib.sha1(json.dumps([person, lt.get('experience'), lt.get('location'), lt.get('preferred_location')],
                                    sort_keys=True).encode()).hexdigest()[:16]
        em = _valid_email(lt.get('email'))
        cv_dates = [str(r.get('created_at') or '') for r in recs if r.get('cv_path')]
        lu = max([upd.get(em, '')] + [upd.get('cid:%d' % r['id'], '') for r in recs])
        cv_date = max(cv_dates + [lu]) if (cv_dates or lu) else ''
        emailable = 1 if (em and not any(r.get('do_not_email') for r in recs) and not any(r.get('reengage_off') for r in recs)) else 0
        live = 1 if any(str(r.get('stage') or '').strip().lower() in LIVE_STAGES for r in recs) else 0
        base = {'email': em, 'candidate_id': lt['id'], 'record_ids': json.dumps([r['id'] for r in recs][:50]),
                'name': lt.get('name') or '', 'designation': lt.get('designation') or '', 'company': lt.get('company') or '',
                'experience': _num(lt.get('experience')), 'location': lt.get('location') or '',
                'seniority': seniority_of(lt.get('experience'), lt.get('designation')),
                'region': region_of(lt.get('location'), lt.get('preferred_location')),
                'cv_date': cv_date, 'last_update_at': lu, 'emailable': emailable, 'live': live, 'updated_at': now}
        ex = existing.get(key)
        if ex and ex['text_hash'] == h:
            sets = base
        else:
            sets = dict(base, text_hash=h)
            if not (ex and ex['manual']):
                ind, fun, sc = classify(person)
                sets.update(industries=json.dumps(ind), functions=json.dumps(fun), seg_scores=json.dumps(sc),
                            seg_source='rules' if (ind or fun) else '', ai_tried=0)
            n_changed += 1
        if ex:
            conn.execute('UPDATE talent_profiles SET %s WHERE company_id=? AND person_key=?' % ','.join(k + '=?' for k in sets),
                         list(sets.values()) + [oid, key])
        else:
            sets.setdefault('industries', '[]')
            sets.setdefault('functions', '[]')
            conn.execute('INSERT INTO talent_profiles (company_id, person_key, %s) VALUES (?,?,%s)'
                         % (','.join(sets), ','.join('?' * len(sets))), [oid, key] + list(sets.values()))
            n_new += 1
    gone = set(existing) - set(groups)
    for key in gone:
        conn.execute('DELETE FROM talent_profiles WHERE company_id=? AND person_key=?', (oid, key))
    conn.execute('INSERT OR IGNORE INTO talent_settings (company_id) VALUES (?)', (oid,))
    conn.execute('UPDATE talent_settings SET last_scan_at=? WHERE company_id=?', (now, oid))
    conn.commit()
    return {'people': len(groups), 'new': n_new, 'reclassified': n_changed, 'removed': len(gone)}


# ── AI fallback for people the rules could not place ─────────────────────
AI_SYSTEM = (
    'You classify engineering candidates in India for a recruitment agency. For each candidate choose '
    'industries (0-2) from: %s; and functions (0-2) from: %s. Use ONLY these keys. If the person clearly does '
    'not belong to any industry, return an empty list. Reply ONLY with JSON: '
    '{"results":[{"id":<id>,"industries":[...],"functions":[...]}]}'
    % (', '.join('%s=%s' % (k, l) for k, l, _ in INDUSTRIES), ', '.join('%s=%s' % (k, l) for k, l, _ in FUNCTIONS)))


def _ask_ai(core, key, people):
    lines = []
    for p in people:
        lines.append('id: %s | title: %s | company: %s | exp: %s | skills: %s | summary: %s'
                     % (p['id'], p['designation'], p['company'], p['experience'], p['skills'][:300], p['summary'][:500]))
    payload = {'model': 'deepseek-chat', 'temperature': 0, 'max_tokens': 1500, 'response_format': {'type': 'json_object'},
               'messages': [{'role': 'system', 'content': AI_SYSTEM}, {'role': 'user', 'content': '\n'.join(lines)}]}
    resp = core.call_deepseek(key, payload, timeout=90, endpoint='talent-segments')
    if resp.status_code != 200:
        raise RuntimeError('DeepSeek returned HTTP %s' % resp.status_code)
    raw = resp.json()['choices'][0]['message']['content']
    try:
        j = json.loads(raw)
    except Exception:
        s, e = raw.find('{'), raw.rfind('}')
        j = json.loads(raw[s:e + 1]) if 0 <= s < e else {}
    out = {}
    for x in (j.get('results') or []) if isinstance(j, dict) else []:
        try:
            ind = [k for k in (x.get('industries') or []) if k in IND_LABELS][:2]
            fun = [k for k in (x.get('functions') or []) if k in FUN_LABELS][:2]
            out[int(x.get('id'))] = (ind, fun)
        except Exception:
            continue
    return out


def ai_fill(conn, oid, limit=60):
    """Classify unplaced people with DeepSeek, inside the daily cap. Returns how many were tried."""
    core = _core()
    s = get_settings(conn, oid)
    if not s['ai_enabled']:
        return 0
    day = now_ist().strftime('%Y-%m-%d')
    used = int(s['ai_used'] or 0) if s['ai_used_day'] == day else 0
    room = max(0, int(s['ai_daily']) - used)
    if not room:
        return 0
    key = (core.get_setting('deepseek_api_key') or '').strip()
    if not key:
        return 0
    todo = _rows(conn, "SELECT p.id AS pid, p.candidate_id FROM talent_profiles p WHERE p.company_id=? AND p.manual=0 "
                       "AND p.ai_tried=0 AND (p.industries='[]' OR p.functions='[]') ORDER BY p.emailable DESC, p.id LIMIT ?",
                 (oid, min(room, limit)))
    if not todo:
        return 0
    have = _cand_cols(conn)
    tried = 0
    for i in range(0, len(todo), AI_BATCH):
        batch = todo[i:i + AI_BATCH]
        people = []
        for t in batch:
            c = conn.execute('SELECT * FROM candidates WHERE id=?', (t['candidate_id'],)).fetchone()
            if not c:
                continue
            summary = str(c['career_summary'] or '') if 'career_summary' in have else ''
            if not summary.strip() and 'embedding_text' in have:
                summary = str(c['embedding_text'] or '')
            people.append({'id': t['pid'], 'designation': c['designation'] or '', 'company': c['company'] or '',
                           'experience': c['experience'] or '', 'skills': ', '.join(str(x) for x in _jl(c['key_skills'])),
                           'summary': re.sub(r'\s+', ' ', summary)})
        try:
            res = _ask_ai(core, key, people)
        except Exception as exc:
            conn.execute('UPDATE talent_settings SET last_error=? WHERE company_id=?', ('AI: %s' % str(exc)[:200], oid))
            conn.commit()
            break
        for p in people:
            r = res.get(int(p['id']))
            cur = conn.execute('SELECT industries, functions FROM talent_profiles WHERE id=?', (p['id'],)).fetchone()
            ind, fun = _jl(cur['industries']), _jl(cur['functions'])
            if r:
                ind = ind or r[0]
                fun = fun or r[1]
            conn.execute("UPDATE talent_profiles SET industries=?, functions=?, ai_tried=1, "
                         "seg_source=CASE WHEN ? THEN 'ai' ELSE seg_source END WHERE id=?",
                         (json.dumps(ind), json.dumps(fun), 1 if r and (r[0] or r[1]) else 0, p['id']))
            tried += 1
        conn.execute('UPDATE talent_settings SET ai_used_day=?, ai_used=? WHERE company_id=?', (day, used + tried, oid))
        conn.commit()
    return tried


def tick():
    core = _core()
    conn = get_db()
    try:
        cos = [r[0] for r in conn.execute("SELECT DISTINCT owner_id FROM candidates WHERE COALESCE(owner_id,0)<>0").fetchall()]
        stamp = ts()
        for oid in cos:
            conn.execute('INSERT OR IGNORE INTO talent_settings (company_id) VALUES (?)', (oid,))
            cur = conn.execute('UPDATE talent_settings SET tick_at=? WHERE company_id=? AND (tick_at IS NULL OR tick_at<?)',
                               (stamp, oid, (now_ist() - datetime.timedelta(seconds=TICK_SECONDS - 30)).strftime('%Y-%m-%dT%H:%M:%S')))
            conn.commit()
            if cur.rowcount != 1:
                continue
            core._bg_tenant_set(oid)
            try:
                scan_company(conn, oid)
                ai_fill(conn, oid)
            except Exception as e:
                print('[segments] company %s failed: %s' % (oid, e))
            finally:
                core._bg_tenant_clear()
    finally:
        conn.close()


def _loop():
    time.sleep(90)
    while True:
        try:
            tick()
        except Exception as e:
            print('[segments] loop error: %s' % e)
        time.sleep(TICK_SECONDS)


_started = False


def start_worker():
    global _started
    if _started or 'pytest' in sys.modules or os.environ.get('HIRELAB_NO_WORKERS'):
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
    print('[segments] background thread started')


start_worker()


# ══════════════════════════════════════════════════════════════════════════
#  Update-profile capture: "what changed" + candidate's own interests
# ══════════════════════════════════════════════════════════════════════════
_UPD_RX = re.compile(r'^/api/public/candidate-update/([^/]+)/?$')


def _fmt(k, v):
    if v in (None, '') or (k in ('ctc_current', 'ctc_expected', 'notice_period', 'experience') and not _num(v)):
        return '—'
    if k in ('ctc_current', 'ctc_expected'):
        return '%g LPA' % _num(v)
    if k == 'notice_period':
        return '%g days' % _num(v)
    if k == 'experience':
        return '%g yrs' % _num(v)
    return str(v).strip()


def _changed(k, a, b):
    if k in ('ctc_current', 'ctc_expected', 'notice_period', 'experience'):
        return abs(_num(a) - _num(b)) > 0.01
    return str(a or '').strip().lower() != str(b or '').strip().lower()


@bp.before_app_request
def _capture_before():
    if request.method != 'POST':
        return None
    m = _UPD_RX.match(request.path or '')
    if not m:
        return None
    try:
        conn = get_db()
        r = conn.execute('SELECT * FROM candidates WHERE update_token=?', (m.group(1),)).fetchone()
        conn.close()
        if r:
            g._tp_pre = dict(r)
    except Exception as e:
        print('[segments] capture-before: %s' % e)
    return None


@bp.after_app_request
def _capture_after(response):
    pre = getattr(g, '_tp_pre', None)
    if not pre or not (200 <= response.status_code < 300):
        return response
    g._tp_pre = None
    try:
        d = request.form if request.form else (request.get_json(silent=True) or {})
        conn = get_db()
        try:
            post = conn.execute('SELECT * FROM candidates WHERE id=?', (pre['id'],)).fetchone()
            if post:
                record_update(conn, pre, dict(post), d)
        finally:
            conn.close()
    except Exception as e:
        print('[segments] capture-after: %s' % e)
    return response


def record_update(conn, pre, post, form):
    oid = int(post.get('owner_id') or 0)
    changes = []
    for k, label in WATCH:
        if k in post and _changed(k, pre.get(k), post.get(k)):
            changes.append({'field': k, 'label': label, 'from': _fmt(k, pre.get(k)), 'to': _fmt(k, post.get(k))})
    with_cv = 1 if str(post.get('cv_path') or '') != str(pre.get('cv_path') or '') and post.get('cv_path') else 0
    parts = ['%s %s → %s' % (c['label'], c['from'], c['to']) for c in changes if c['field'] not in ('phone', 'email')]
    summary = '; '.join(parts[:6]) or 'No change in key details'
    if with_cv:
        summary += ' · new CV'
    now = ts()
    em = _valid_email(post.get('email')) or _valid_email(pre.get('email'))
    conn.execute('INSERT INTO talent_updates (company_id, candidate_id, email, name, changes, summary, with_cv, created_at) '
                 'VALUES (?,?,?,?,?,?,?,?)', (oid, post['id'], em, post.get('name') or '', json.dumps(changes), summary,
                                               with_cv, now))
    try:
        conn.execute('INSERT INTO candidate_events (candidate_id,event_type,detail,created_at) VALUES (?,?,?,?)',
                     (post['id'], 'update', 'What changed: ' + summary, now))
    except Exception:
        pass
    # candidate's own interests ("what would you like to hear about")
    ii = _parse_list(form.get('interest_industries'), IND_LABELS)
    ff = _parse_list(form.get('interest_functions'), FUN_LABELS)
    key = em or 'id:%d' % (int(post.get('copied_from') or 0) or int(post['id']))
    row = conn.execute('SELECT id FROM talent_profiles WHERE company_id=? AND person_key=?', (oid, key)).fetchone()
    sets = {'last_update_at': now, 'cv_date': now}     # details confirmed today = fresh (same rule as the scan)
    if form.get('interest_industries') is not None or form.get('interest_functions') is not None:
        sets.update(interest_industries=json.dumps(ii), interest_functions=json.dumps(ff), interests_at=now)
    sets = {k: v for k, v in sets.items() if v is not None}
    if row:
        conn.execute('UPDATE talent_profiles SET %s WHERE id=?' % ','.join(k + '=?' for k in sets),
                     list(sets.values()) + [row['id']])
    else:
        sets.update(email=em, candidate_id=post['id'], name=post.get('name') or '', updated_at=now)
        conn.execute('INSERT INTO talent_profiles (company_id, person_key, %s) VALUES (?,?,%s)'
                     % (','.join(sets), ','.join('?' * len(sets))), [oid, key] + list(sets.values()))
    conn.commit()
    return summary


def _parse_list(v, allowed):
    if v is None:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            v = [x.strip() for x in v.split(',')]
    return [x for x in (v or []) if x in allowed][:6]


@pages.route('/api/public/talent-interests/<token>', methods=['GET'])
def public_interests(token):
    """Options + current choices for the update-profile page."""
    conn = get_db()
    try:
        c = conn.execute('SELECT id, email, owner_id, copied_from FROM candidates WHERE update_token=?', (token,)).fetchone() \
            if token and len(token) >= 10 else None
        if not c:
            return jsonify({'ok': False}), 404
        em = _valid_email(c['email'])
        key = em or 'id:%d' % (int(c['copied_from'] or 0) or int(c['id']))
        p = conn.execute('SELECT industries, functions, interest_industries, interest_functions, interests_at '
                         'FROM talent_profiles WHERE company_id=? AND person_key=?', (c['owner_id'], key)).fetchone()
        ii = _jl(p['interest_industries']) if p and p['interests_at'] else (_jl(p['industries']) if p else [])
        ff = _jl(p['interest_functions']) if p and p['interests_at'] else (_jl(p['functions']) if p else [])
        return jsonify({'ok': True, 'industries': [{'key': k, 'label': l} for k, l, _ in INDUSTRIES],
                        'functions': [{'key': k, 'label': l} for k, l, _ in FUNCTIONS if k != 'leadership'],
                        'chosen_industries': ii, 'chosen_functions': ff})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  Effective segments + queries (used by the dashboard and later waves)
# ══════════════════════════════════════════════════════════════════════════
def effective(p):
    """Industries/functions that count: manual > candidate's own > rules/AI."""
    ind, fun = _jl(p['industries']), _jl(p['functions'])
    if not p.get('manual') and p.get('interests_at'):
        ii, ff = _jl(p['interest_industries']), _jl(p['interest_functions'])
        ind = ii or ind
        fun = ff or fun
    return ind, fun


def is_fresh(p, fresh_days, now=None):
    d = str(p.get('cv_date') or '')[:10]
    if len(d) < 10:
        return False
    now = now or now_ist()
    try:
        return (now - datetime.datetime.strptime(d, '%Y-%m-%d')).days <= int(fresh_days)
    except ValueError:
        return False


def load_people(conn, oid):
    return _rows(conn, 'SELECT * FROM talent_profiles WHERE company_id=?', (oid,))


def match(p, f, fresh_days, now=None):
    """Does person p pass filter f? f keys (all optional, lists = OR inside, AND across):
       industries, functions, seniority, regions, freshness ('fresh'|'stale'|''), emailable_only, exclude_live,
       unclassified_only."""
    ind, fun = effective(p)
    if f.get('unclassified_only') and ind:
        return False
    if f.get('industries') and not (set(f['industries']) & set(ind)):
        return False
    if f.get('functions') and not (set(f['functions']) & set(fun)):
        return False
    if f.get('seniority') and p.get('seniority') not in f['seniority']:
        return False
    if f.get('regions') and p.get('region') not in f['regions']:
        return False
    if f.get('emailable_only') and not p.get('emailable'):
        return False
    if f.get('exclude_live') and p.get('live'):
        return False
    fr = f.get('freshness') or ''
    if fr:
        fresh = is_fresh(p, fresh_days, now)
        if (fr == 'fresh') != fresh:
            return False
    return True


def segment_people(conn, oid, f):
    """Public helper for later waves (newsletter): people matching filter f."""
    s = get_settings(conn, oid)
    now = now_ist()
    return [p for p in load_people(conn, oid) if match(p, f, s['fresh_days'], now)]


def _clean_filter(d):
    def lst(k, allowed):
        v = d.get(k) or []
        return [x for x in v if x in allowed] if isinstance(v, list) else []
    return {'industries': lst('industries', IND_LABELS), 'functions': lst('functions', FUN_LABELS),
            'seniority': lst('seniority', SEN_LABELS), 'regions': lst('regions', REGION_LABELS),
            'freshness': d.get('freshness') if d.get('freshness') in ('fresh', 'stale') else '',
            'emailable_only': bool(d.get('emailable_only')), 'exclude_live': bool(d.get('exclude_live')),
            'unclassified_only': bool(d.get('unclassified_only'))}


def _person_out(p, fresh_days, now):
    ind, fun = effective(p)
    d = str(p.get('cv_date') or '')[:10]
    age = None
    if len(d) == 10:
        try:
            age = (now - datetime.datetime.strptime(d, '%Y-%m-%d')).days
        except ValueError:
            pass
    return {'candidate_id': p['candidate_id'], 'name': p['name'], 'email': p['email'], 'designation': p['designation'],
            'company': p['company'], 'experience': p['experience'], 'location': p['location'],
            'industries': ind, 'functions': fun, 'seniority': p['seniority'], 'region': p['region'],
            'source': 'manual' if p.get('manual') else ('candidate' if p.get('interests_at') else p.get('seg_source') or ''),
            'cv_date': p.get('cv_date') or '', 'cv_age_days': age, 'fresh': age is not None and age <= int(fresh_days),
            'emailable': bool(p.get('emailable')), 'live': bool(p.get('live'))}


def _admin():
    if not is_company_admin():
        return _err('Only an admin can open the Talent Pool dashboard.', 403)
    return None


@bp.route('/meta', methods=['GET'])
@login_required
def meta():
    return jsonify({'ok': True,
                    'industries': [{'key': k, 'label': l} for k, l, _ in INDUSTRIES],
                    'functions': [{'key': k, 'label': l} for k, l, _ in FUNCTIONS],
                    'seniority': [{'key': k, 'label': l} for k, l in SENIORITY],
                    'regions': [{'key': k, 'label': REGION_LABELS[k]} for k in REGION_KEYS]})


@bp.route('/summary', methods=['GET'])
@login_required
def summary():
    e = _admin()
    if e:
        return e
    oid = _oid()
    conn = get_db()
    try:
        s = get_settings(conn, oid)
        if not s['last_scan_at']:
            scan_company(conn, oid)
            s = get_settings(conn, oid)
        now = now_ist()
        people = load_people(conn, oid)
        fd = int(s['fresh_days'])
        tot = len(people)
        k = {'people': tot, 'emailable': 0, 'fresh': 0, 'fresh_emailable': 0, 'classified': 0, 'by_candidate': 0,
             'manual': 0, 'ai': 0, 'updated_30': 0, 'no_cv': 0}
        mat = {ik: {sk: 0 for sk, _ in SENIORITY} for ik, _, _ in INDUSTRIES}
        mat['_none'] = {sk: 0 for sk, _ in SENIORITY}
        fun_c = {fk: 0 for fk, _, _ in FUNCTIONS}
        reg_c = {rk: 0 for rk in REGION_KEYS}
        fresh_by_ind = {ik: [0, 0] for ik, _, _ in INDUSTRIES}
        fresh_by_ind['_none'] = [0, 0]
        since30 = (now - datetime.timedelta(days=30)).strftime('%Y-%m-%d')
        for p in people:
            ind, fun = effective(p)
            fr = is_fresh(p, fd, now)
            k['emailable'] += 1 if p['emailable'] else 0
            k['fresh'] += 1 if fr else 0
            k['fresh_emailable'] += 1 if (fr and p['emailable']) else 0
            k['classified'] += 1 if ind else 0
            k['by_candidate'] += 1 if p['interests_at'] else 0
            k['manual'] += 1 if p['manual'] else 0
            k['ai'] += 1 if p['seg_source'] == 'ai' else 0
            k['updated_30'] += 1 if str(p['last_update_at'] or '') >= since30 else 0
            k['no_cv'] += 0 if p['cv_date'] else 1
            for ik in (ind or ['_none']):
                if ik in mat:
                    mat[ik][p['seniority'] if p['seniority'] in SEN_LABELS else 'unknown'] += 1
                    fresh_by_ind[ik][0 if fr else 1] += 1
            for fk in fun:
                if fk in fun_c:
                    fun_c[fk] += 1
            reg_c[p['region'] if p['region'] in reg_c else 'unknown'] += 1
        recent = _rows(conn, 'SELECT u.*, c.mandate_id FROM talent_updates u LEFT JOIN candidates c ON c.id=u.candidate_id '
                             'WHERE u.company_id=? ORDER BY u.id DESC LIMIT 30', (oid,))
        for r in recent:
            r['changes'] = _jl(r['changes'])
        return jsonify({'ok': True, 'settings': s, 'kpis': k, 'matrix': mat, 'functions': fun_c, 'regions': reg_c,
                        'fresh_by_industry': fresh_by_ind, 'recent_updates': recent})
    finally:
        conn.close()


@bp.route('/query', methods=['POST'])
@login_required
def query():
    """Segment builder: count + first people for a filter."""
    e = _admin()
    if e:
        return e
    d = request.get_json(silent=True) or {}
    f = _clean_filter(d.get('filter') or {})
    oid = _oid()
    conn = get_db()
    try:
        s = get_settings(conn, oid)
        now = now_ist()
        hits = [p for p in load_people(conn, oid) if match(p, f, s['fresh_days'], now)]
        hits.sort(key=lambda p: (str(p.get('cv_date') or '')), reverse=(f.get('freshness') != 'stale'))
        lim = max(1, min(int(d.get('limit') or 50), 500))
        out = [_person_out(p, s['fresh_days'], now) for p in hits[:lim]]
        ids = [o['candidate_id'] for o in out if o['candidate_id']]
        mids = {}
        if ids:
            q = ','.join('?' * len(ids))
            mids = {r[0]: r[1] for r in conn.execute('SELECT id, mandate_id FROM candidates WHERE id IN (%s)' % q, ids)}
        for o in out:
            o['mandate_id'] = mids.get(o['candidate_id'], 0)
        return jsonify({'ok': True, 'total': len(hits), 'emailable': sum(1 for p in hits if p['emailable']),
                        'fresh': sum(1 for p in hits if is_fresh(p, s['fresh_days'], now)),
                        'people': out, 'filter': f})
    finally:
        conn.close()


@bp.route('/settings', methods=['PUT', 'POST'])
@login_required
def save_settings():
    e = _admin()
    if e:
        return e
    d = request.get_json(silent=True) or {}
    oid = _oid()
    conn = get_db()
    try:
        s = get_settings(conn, oid)

        def num(k, lo, hi):
            try:
                return max(lo, min(hi, int(d.get(k, s[k]))))
            except (TypeError, ValueError):
                return s[k]
        ai = 1 if d.get('ai_enabled', s['ai_enabled']) in (1, True, '1', 'true') else 0
        conn.execute('INSERT OR IGNORE INTO talent_settings (company_id) VALUES (?)', (oid,))
        conn.execute('UPDATE talent_settings SET fresh_days=?, ai_enabled=?, ai_daily=?, updated_by=?, updated_at=? '
                     'WHERE company_id=?', (num('fresh_days', 30, 730), ai, num('ai_daily', 0, 5000),
                                            real_user_id(), ts(), oid))
        conn.commit()
        return jsonify({'ok': True, 'settings': get_settings(conn, oid)})
    finally:
        conn.close()


@bp.route('/rescan', methods=['POST'])
@login_required
def rescan():
    e = _admin()
    if e:
        return e
    oid = _oid()
    conn = get_db()
    try:
        r = scan_company(conn, oid)
        n = 0
        if (request.get_json(silent=True) or {}).get('with_ai'):
            n = ai_fill(conn, oid, limit=60)
        return jsonify({'ok': True, 'scan': r, 'ai_classified': n, 'settings': get_settings(conn, oid)})
    finally:
        conn.close()


# ── per-candidate chip (under /api/candidates/<id>/ so the access guard applies) ──
def _profile_for_cand(conn, oid, cid):
    c = conn.execute('SELECT id, email, copied_from FROM candidates WHERE id=? AND owner_id=?', (cid, oid)).fetchone()
    if not c:
        return None, None
    em = _valid_email(c['email'])
    key = em or 'id:%d' % (int(c['copied_from'] or 0) or int(c['id']))
    p = conn.execute('SELECT * FROM talent_profiles WHERE company_id=? AND person_key=?', (oid, key)).fetchone()
    return c, (dict(p) if p else None)


@cand_bp.route('/api/candidates/<int:cid>/segments', methods=['GET', 'PUT', 'POST'])
@login_required
def candidate_segments(cid):
    oid = _oid()
    conn = get_db()
    try:
        c, p = _profile_for_cand(conn, oid, cid)
        if not c:
            return _err('Not found', 404)
        if not p:
            scan_company(conn, oid)
            c, p = _profile_for_cand(conn, oid, cid)
        if not p:
            return _err('Not found', 404)
        s = get_settings(conn, oid)
        if request.method in ('PUT', 'POST'):
            d = request.get_json(silent=True) or {}
            if d.get('reset'):
                conn.execute("UPDATE talent_profiles SET manual=0, text_hash='' WHERE id=?", (p['id'],))
                conn.commit()
                scan_company(conn, oid)
            else:
                ind = [x for x in (d.get('industries') or []) if x in IND_LABELS][:4]
                fun = [x for x in (d.get('functions') or []) if x in FUN_LABELS][:4]
                sets = {'industries': json.dumps(ind), 'functions': json.dumps(fun), 'manual': 1, 'seg_source': 'manual'}
                if d.get('seniority') in SEN_LABELS:
                    sets['seniority'] = d['seniority']
                if d.get('region') in REGION_LABELS:
                    sets['region'] = d['region']
                conn.execute('UPDATE talent_profiles SET %s WHERE id=?' % ','.join(k + '=?' for k in sets),
                             list(sets.values()) + [p['id']])
                conn.commit()
            c, p = _profile_for_cand(conn, oid, cid)
        out = _person_out(p, s['fresh_days'], now_ist())
        out['labels'] = {'industries': [IND_LABELS[x] for x in out['industries'] if x in IND_LABELS],
                         'functions': [FUN_LABELS[x] for x in out['functions'] if x in FUN_LABELS],
                         'seniority': SEN_LABELS.get(out['seniority'], ''), 'region': REGION_LABELS.get(out['region'], '')}
        last = conn.execute('SELECT summary, created_at FROM talent_updates WHERE company_id=? AND '
                            '(candidate_id=? OR (email<>\'\' AND email=?)) ORDER BY id DESC LIMIT 1',
                            (oid, cid, p['email'] or '#')).fetchone()
        out['last_update'] = dict(last) if last else None
        out['fresh_days'] = s['fresh_days']
        out['manual'] = bool(p['manual'])
        return jsonify(dict(out, ok=True))
    finally:
        conn.close()


@pages.route('/talent-pool')
def talent_page():
    core = _core()
    if not core.current_user():
        return redirect('/')
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'talent-pool.html')
    with open(path, encoding='utf-8') as f:
        return Response(f.read(), mimetype='text/html')
