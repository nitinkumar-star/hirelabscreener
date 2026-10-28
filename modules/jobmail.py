"""
Job-Match Mail — e-mail a job to the right people already in the database.

Flow (one "campaign" per job send):

  1. BUILD    Rank the whole database against the job's JD vector (the same
              embedding search as the "Matching Candidates" tab — no keywords),
              then apply hard filters:
                * has a valid e-mail, not unsubscribed, not switched off;
                * not already in this job, not live in another pipeline
                  (Shared with Client / Interview / Offer / Placed / Joined);
                * never mailed about THIS job before;
                * frequency guard: no HireLab e-mail (job mail or re-engage)
                  in the last N days (default 7);
                * experience inside the job's range (± tolerance), current CTC
                  not above the job's budget, optional location check;
                * similarity at or above the chosen cut-off.
              One person = one e-mail address (best-scoring record is used).
  2. AI CHECK DeepSeek reads the JD and each shortlisted profile/resume and
              says fit / maybe / not fit with a one-line reason. "Not fit" is
              un-ticked automatically. Runs in the background; the page polls.
  3. APPROVE  The recruiter reviews the list, edits the note, sends a test,
              and approves. Nothing is ever sent without this step.
  4. SEND     A background thread sends through the normal ATS mail pipeline
              (company mailbox + signature, saved in the candidate's thread),
              Mon–Sat inside the sending window, up to the daily cap.
  5. RESPOND  The mail has two buttons. "Yes, I'm interested" (confirmed on a
              page, so link scanners cannot fake it) puts the person on the job
              at stage "Interested" and creates a call task for the recruiter.
              "Not right now" asks for a reason; "contact me later" creates a
              90-day follow-up task. Opens and clicks are tracked.

Additive only: new tables jobmail_settings, jobmail_campaigns,
jobmail_recipients. Nothing existing is altered. Public pages: /jm/...
"""

import datetime
import hashlib
import hmac
import html
import json
import math
import os
import re
import sys
import threading
import time

from flask import Blueprint, request, jsonify, redirect, Response

from modules.shared import get_db, ts, effective_company_id, real_user_id, is_company_admin, login_required, _core
from modules import register_migration

bp = Blueprint('jobmail', __name__, url_prefix='/api/jobmail')
pages = Blueprint('jobmail_pages', __name__)

IST = datetime.timedelta(hours=5, minutes=30)
LIVE_STAGES = {'shared with client', 'interview inprocess', 'interview in process', 'interview', 'offered',
               'offer', 'offer accepted', 'placed', 'joined'}
DEFAULTS = {'daily_cap': 40, 'gap_days': 7, 'start_hour': 10, 'end_hour': 19, 'default_min': 70, 'default_limit': 30}
DEFAULT_FILTERS = {'min_score': None, 'limit': None, 'exp_tolerance': 2, 'use_exp': True, 'use_ctc': True,
                   'use_location': False}
NOT_NOW_REASONS = [
    ('happy', 'I am happy in my current role'),
    ('later', 'Open to it later — contact me in 3 months'),
    ('location', 'The location does not suit me'),
    ('ctc', 'Compensation would not work for me'),
    ('notice', 'My notice period is too long right now'),
    ('role', 'This role is not the right fit for my experience'),
]
TICK_SECONDS = 120
MAX_PER_TICK = 5
AI_BATCH = 6
RANK_POOL = 3000


@register_migration
def _migrate_jobmail(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS jobmail_settings (
        company_id INTEGER PRIMARY KEY,
        daily_cap INTEGER DEFAULT 40,
        gap_days INTEGER DEFAULT 7,
        start_hour INTEGER DEFAULT 10,
        end_hour INTEGER DEFAULT 19,
        default_min INTEGER DEFAULT 70,
        default_limit INTEGER DEFAULT 30,
        tick_at TEXT DEFAULT '',
        last_error TEXT DEFAULT '',
        updated_by INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT ''
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS jobmail_campaigns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        mandate_id INTEGER NOT NULL,
        status TEXT DEFAULT 'draft',
        filters TEXT DEFAULT '{}',
        show_client INTEGER DEFAULT 0,
        client_label TEXT DEFAULT '',
        show_ctc INTEGER DEFAULT 0,
        note TEXT DEFAULT '',
        ai_status TEXT DEFAULT '',
        ai_done INTEGER DEFAULT 0,
        ai_total INTEGER DEFAULT 0,
        ai_error TEXT DEFAULT '',
        scanned INTEGER DEFAULT 0,
        created_by INTEGER DEFAULT 0,
        created_at TEXT DEFAULT '',
        built_at TEXT DEFAULT '',
        approved_by INTEGER DEFAULT 0,
        approved_at TEXT DEFAULT '',
        finished_at TEXT DEFAULT ''
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS jobmail_recipients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER NOT NULL,
        company_id INTEGER NOT NULL,
        mandate_id INTEGER NOT NULL,
        candidate_id INTEGER NOT NULL,
        email TEXT NOT NULL,
        name TEXT DEFAULT '',
        designation TEXT DEFAULT '',
        company TEXT DEFAULT '',
        experience REAL DEFAULT 0,
        location TEXT DEFAULT '',
        ctc_current REAL DEFAULT 0,
        score REAL DEFAULT 0,
        eligible INTEGER DEFAULT 1,
        skip_reason TEXT DEFAULT '',
        selected INTEGER DEFAULT 1,
        ai_verdict TEXT DEFAULT '',
        ai_reason TEXT DEFAULT '',
        status TEXT DEFAULT 'draft',
        error TEXT DEFAULT '',
        sent_at TEXT DEFAULT '',
        opened_at TEXT DEFAULT '',
        clicked_at TEXT DEFAULT '',
        response TEXT DEFAULT '',
        response_reason TEXT DEFAULT '',
        responded_at TEXT DEFAULT '',
        job_candidate_id INTEGER DEFAULT 0,
        task_id INTEGER DEFAULT 0,
        created_at TEXT DEFAULT ''
    )''')
    for tbl in ('jobmail_campaigns', 'jobmail_settings'):          # editable e-mail text (empty = built-in default)
        for col in TPL_KEYS:
            try:
                conn.execute("ALTER TABLE %s ADD COLUMN %s TEXT DEFAULT ''" % (tbl, col))
            except Exception:
                pass
    for sql in ('CREATE INDEX IF NOT EXISTS idx_jm_rec_campaign ON jobmail_recipients(campaign_id)',
                'CREATE INDEX IF NOT EXISTS idx_jm_rec_email ON jobmail_recipients(company_id, email, status)',
                'CREATE INDEX IF NOT EXISTS idx_jm_rec_status ON jobmail_recipients(company_id, status)',
                'CREATE INDEX IF NOT EXISTS idx_jm_camp_co ON jobmail_campaigns(company_id, mandate_id)'):
        conn.execute(sql)
    conn.commit()


# ── small helpers ──────────────────────────────────────────────────────────
def now_ist():
    return datetime.datetime.utcnow() + IST


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _err(msg, code=400):
    return jsonify({'ok': False, 'error': msg}), code


def _num(v, default=0.0):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return default


def _valid_email(em):
    em = str(em or '').strip().lower()
    return em if ('@' in em and '.' in em.split('@')[-1] and ' ' not in em) else ''


def first_name(name):
    n = str(name or '').strip()
    for t in ('mr.', 'mrs.', 'ms.', 'dr.', 'er.', 'mr', 'mrs', 'ms', 'dr', 'er'):
        if n.lower().startswith(t + ' '):
            n = n[len(t) + 1:].strip()
            break
    return (n.split() or ['there'])[0].strip(',').title()


def get_settings(conn, oid):
    r = conn.execute('SELECT * FROM jobmail_settings WHERE company_id=?', (oid,)).fetchone()
    s = dict(DEFAULTS, last_error='')
    if r:
        for k in list(DEFAULTS) + ['last_error']:
            if r[k] is not None:
                s[k] = r[k]
    return s


def _sign(raw):
    key = _core().app.secret_key
    key = key if isinstance(key, bytes) else str(key or 'hirelab').encode()
    return hmac.new(key, ('jm:' + raw).encode(), hashlib.sha256).hexdigest()[:16]


def token_for(rid):
    return '%d-%s' % (rid, _sign(str(rid)))


def parse_token(tok):
    try:
        rid, sig = str(tok).split('-', 1)
        rid = int(rid)
    except Exception:
        return None
    return rid if hmac.compare_digest(_sign(str(rid)), sig) else None


def _base():
    try:
        return _core()._camp_base_url().rstrip('/')
    except Exception:
        return (os.environ.get('PUBLIC_BASE_URL', '') or 'https://hirelabscreener.onrender.com').rstrip('/')


def _has_signature():
    try:
        return bool((_core()._build_signature() or ('', ''))[0])
    except Exception:
        return False


def _filters(c):
    try:
        f = json.loads(c['filters'] or '{}')
    except Exception:
        f = {}
    out = dict(DEFAULT_FILTERS)
    out.update({k: v for k, v in (f or {}).items() if k in DEFAULT_FILTERS})
    return out


def _campaign(conn, oid, cid):
    r = conn.execute('SELECT * FROM jobmail_campaigns WHERE id=? AND company_id=?', (cid, oid)).fetchone()
    return dict(r) if r else None


def _mandate(conn, oid, mid):
    r = conn.execute('SELECT * FROM mandates WHERE id=? AND owner_id=?', (mid, oid)).fetchone()
    return r


def _can_write_mandate(conn, mid):
    try:
        from modules.access import can_see_mandate
        return can_see_mandate(conn, mid, write=True)
    except Exception:
        return True


def _skills(m, col):
    try:
        return [str(x).strip() for x in (_core().mandate_skills(m, col) or []) if str(x).strip()]
    except Exception:
        return []


def _jd_text(m):
    raw = str(m['jd'] if 'jd' in m.keys() else '') or ''
    try:
        raw = _core().html_to_text(raw)
    except Exception:
        raw = re.sub(r'<[^>]+>', ' ', raw)
    return re.sub(r'\s+', ' ', raw).strip()


def _mval(m, key, default=None):
    try:
        return m[key] if key in m.keys() else default
    except Exception:
        return default


def recent_mail_map(conn, oid, gap_days, exclude_campaign=0):
    """{email: last_at} for every address that got (or is queued to get) a HireLab
    e-mail — job mail or re-engagement — inside the last `gap_days` days."""
    since = (now_ist() - datetime.timedelta(days=int(gap_days or 0))).strftime('%Y-%m-%dT%H:%M:%S')
    out = {}
    for r in conn.execute("SELECT LOWER(TRIM(email)) AS em, MAX(sent_at) AS last FROM jobmail_recipients "
                          "WHERE company_id=? AND status='sent' AND sent_at>=? AND campaign_id<>? GROUP BY em",
                          (oid, since, exclude_campaign)):
        out[r['em']] = r['last']
    for r in conn.execute("SELECT LOWER(TRIM(email)) AS em FROM jobmail_recipients WHERE company_id=? "
                          "AND status IN ('queued','sending') AND campaign_id<>?", (oid, exclude_campaign)):
        out.setdefault(r['em'], 'queued')
    try:
        for r in conn.execute("SELECT LOWER(TRIM(email)) AS em, MAX(sent_at) AS last FROM reengage_log "
                              "WHERE company_id=? AND status='sent' AND sent_at>=? GROUP BY em", (oid, since)):
            if r['em'] not in out or str(r['last']) > str(out[r['em']]):
                out[r['em']] = r['last']
    except Exception:
        pass
    return out


def mailed_recently(conn, oid):
    """Addresses that had a job mail (sent or queued) inside the guard window.
    Used by the re-engagement sender so the two never pile up on one person."""
    s = get_settings(conn, oid)
    since = (now_ist() - datetime.timedelta(days=int(s['gap_days']))).strftime('%Y-%m-%dT%H:%M:%S')
    out = set()
    for r in conn.execute("SELECT LOWER(TRIM(email)) AS em FROM jobmail_recipients WHERE company_id=? AND "
                          "((status='sent' AND sent_at>=?) OR status IN ('queued','sending'))", (oid, since)):
        out.add(r['em'])
    return out


# ══════════════════════════════════════════════════════════════════════════
#  1. BUILD — vector ranking + hard filters
# ══════════════════════════════════════════════════════════════════════════
def _location_ok(m, cand):
    want = str(_mval(m, 'location', '') or '').lower()
    toks = [t.strip() for t in re.split(r'[,/|;]+| or | and ', want) if len(t.strip()) >= 3]
    if not toks:
        return True
    have = (str(cand['location'] or '') + ' ' + str(cand['preferred_location'] or '')).lower()
    if any(w in have for w in ('anywhere', 'pan india', 'pan-india', 'any location', 'open to relocat')):
        return True
    return any(t in have for t in toks)


def build_campaign(conn, oid, camp):
    """(Re)build the recipient list of a DRAFT campaign. Runs inside a request
    (the recruiter scope of the vector search comes from the session)."""
    from modules.matching import _jd_vector, _people_in_mandate, _person_key, _digits
    core = _core()
    mid = int(camp['mandate_id'])
    m = _mandate(conn, oid, mid)
    if not m:
        return False, 'Job not found'
    if (m['status'] or '') != 'active':
        return False, 'This job is not active.'
    s = get_settings(conn, oid)
    f = _filters(camp)
    min_score = float(f['min_score'] if f['min_score'] not in (None, '') else s['default_min'])
    limit = max(1, min(int(f['limit'] or s['default_limit']), 150))
    jd, msg = _jd_vector(conn, mid)
    if jd is None:
        return False, msg or 'This job has no AI profile yet.'
    ranked, scanned = core._search_rank(conn, list(jd), -1.0, int(core._search_cfg('search_max_candidates')),
                                        RANK_POOL, oid)
    ids = [cid for _s, cid in ranked]
    sim_of = {cid: s_ for s_, cid in ranked}

    # facts about each e-mail address across ALL its records
    flags = {}
    for r in conn.execute("SELECT LOWER(TRIM(email)) AS em, MAX(COALESCE(do_not_email,0)) AS dne, "
                          "MAX(COALESCE(reengage_off,0)) AS off, GROUP_CONCAT(LOWER(TRIM(COALESCE(stage,''))), '|') AS stages "
                          "FROM candidates WHERE owner_id=? AND TRIM(COALESCE(email,''))<>'' GROUP BY em", (oid,)):
        flags[r['em']] = r
    in_keys, in_phones = _people_in_mandate(conn, mid, oid)
    in_emails = {r[0] for r in conn.execute("SELECT LOWER(TRIM(email)) FROM candidates WHERE mandate_id=? AND owner_id=? "
                                            "AND TRIM(COALESCE(email,''))<>''", (mid, oid))}
    done_for_job = {r[0] for r in conn.execute("SELECT LOWER(TRIM(email)) FROM jobmail_recipients WHERE company_id=? "
                                               "AND mandate_id=? AND campaign_id<>? AND status IN ('queued','sending','sent')",
                                               (oid, mid, camp['id']))}
    recent = recent_mail_map(conn, oid, s['gap_days'], exclude_campaign=camp['id'])
    mg_ids, mg_ems = set(), set()          # per-job "automated e-mails OFF" switch (modules/mail_guard.py)
    try:
        from modules import mail_guard as _mg
        mg_ids, mg_ems, _ = _mg.blocked(conn, oid)
    except Exception:
        pass

    exp_min, exp_max = _num(_mval(m, 'exp_min')), _num(_mval(m, 'exp_max'))
    ctc_max = _num(_mval(m, 'ctc_max'))
    tol = _num(f.get('exp_tolerance'), 2)

    rows = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ','.join('?' * len(chunk))
        for r in conn.execute(f"SELECT id, name, email, phone, designation, company, experience, location, "
                              f"preferred_location, ctc_current, copied_from, stage FROM candidates WHERE id IN ({q})", chunk):
            rows[r['id']] = r

    seen, eligible, skipped = set(), [], []
    for cid in ids:                                          # best score first
        r = rows.get(cid)
        if not r:
            continue
        em = _valid_email(r['email'])
        score = round(max(0.0, float(sim_of[cid])) * 100, 1)
        if score < min_score:
            break                                            # ranked: everything after is lower
        if not em:
            continue                                         # nobody to write to — not worth listing
        if em in seen:
            continue
        seen.add(em)
        fl = flags.get(em)
        stages = set(str(fl['stages'] or '').split('|')) if fl else set()
        reason = ''
        if fl and fl['dne']:
            reason = 'Unsubscribed'
        elif fl and fl['off']:
            reason = 'E-mails switched off by you'
        elif em in mg_ems or cid in mg_ids:
            reason = 'Automated e-mails switched off for their job'
        elif _person_key(r) in in_keys or cid in in_keys or em in in_emails or (_digits(r['phone']) and _digits(r['phone']) in in_phones):
            reason = 'Already in this job'
        elif stages & LIVE_STAGES:
            reason = 'Live in another pipeline (%s)' % sorted(stages & LIVE_STAGES)[0].title()
        elif em in done_for_job:
            reason = 'Already mailed about this job'
        elif em in recent:
            reason = 'Got a HireLab e-mail in the last %d days' % int(s['gap_days'])
        elif f.get('use_exp') and (exp_min or exp_max) and _num(r['experience']) > 0 and (
                (exp_min and _num(r['experience']) < exp_min - tol) or (exp_max and _num(r['experience']) > exp_max + tol)):
            reason = 'Experience %g yrs is outside %g–%g yrs' % (_num(r['experience']), exp_min, exp_max or exp_min)
        elif f.get('use_ctc') and ctc_max and _num(r['ctc_current']) > ctc_max:
            reason = 'Current CTC %g LPA is above the %g LPA budget' % (_num(r['ctc_current']), ctc_max)
        elif f.get('use_location') and not _location_ok(m, r):
            reason = 'Location does not match'
        item = (r, em, score, reason)
        if reason:
            if len(skipped) < 300:
                skipped.append(item)
        else:
            eligible.append(item)
            if len(eligible) >= limit:
                break

    now = ts()
    conn.execute('DELETE FROM jobmail_recipients WHERE campaign_id=? AND company_id=?', (camp['id'], oid))
    for (r, em, score, reason) in eligible + skipped:
        conn.execute('INSERT INTO jobmail_recipients (campaign_id, company_id, mandate_id, candidate_id, email, name, '
                     'designation, company, experience, location, ctc_current, score, eligible, skip_reason, selected, '
                     'status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (camp['id'], oid, mid, r['id'], em, r['name'] or '', r['designation'] or '', r['company'] or '',
                      _num(r['experience']), r['location'] or '', _num(r['ctc_current']), score,
                      0 if reason else 1, reason, 0 if reason else 1, 'excluded' if reason else 'draft', now))
    conn.execute("UPDATE jobmail_campaigns SET scanned=?, built_at=?, ai_status='', ai_done=0, ai_total=0, ai_error='' "
                 "WHERE id=?", (scanned, now, camp['id']))
    conn.commit()
    return True, ''


# ══════════════════════════════════════════════════════════════════════════
#  2. AI CHECK — DeepSeek reads JD + profile, says fit / maybe / not fit
# ══════════════════════════════════════════════════════════════════════════
AI_SYSTEM = (
    'You are a senior recruiter in India for engineering roles in Data Center, Solar & Renewable Energy, '
    'Electrical (switchgear, power distribution, substations) and Industrial Automation. You decide whether it '
    'is worth e-mailing a job opening to each candidate. Be strict: a wrong function (e.g. sales vs site execution '
    'vs design), wrong domain, or clearly wrong seniority is "not_fit". "maybe" means partly relevant and worth a '
    'call. "fit" means a recruiter would happily call this person for this job. '
    'Reply ONLY with JSON: {"results":[{"id":<candidate id>,"verdict":"fit"|"maybe"|"not_fit",'
    '"reason":"<max 18 words, concrete>"}]} with one entry per candidate.')


def _job_brief(m):
    must, good = _skills(m, 'must_have_skills'), _skills(m, 'good_to_have_skills')
    parts = ['Role: %s' % (m['role'] or ''), 'Client: %s' % (m['client'] or ''),
             'Location: %s' % (_mval(m, 'location', '') or '-')]
    if _num(_mval(m, 'exp_min')) or _num(_mval(m, 'exp_max')):
        parts.append('Experience: %g-%g years' % (_num(_mval(m, 'exp_min')), _num(_mval(m, 'exp_max'))))
    if _num(_mval(m, 'ctc_max')):
        parts.append('CTC budget: %g-%g LPA' % (_num(_mval(m, 'ctc_min')), _num(_mval(m, 'ctc_max'))))
    if must:
        parts.append('Must have: ' + ', '.join(must[:15]))
    if good:
        parts.append('Good to have: ' + ', '.join(good[:10]))
    jd = _jd_text(m)
    if jd:
        parts.append('JD: ' + jd[:2500])
    return '\n'.join(parts)


def _cand_brief(c):
    def lst(v):
        try:
            x = json.loads(v or '[]')
            return ', '.join(str(i) for i in x[:15]) if isinstance(x, list) else str(v)
        except Exception:
            return str(v or '')
    lines = ['id: %s' % c['id'],
             'Current: %s at %s' % (c['designation'] or '-', c['company'] or '-'),
             'Experience: %s years; Location: %s; Current CTC: %s LPA' % (c['experience'] or '-', c['location'] or '-',
                                                                      c['ctc_current'] or '-'),
             'Skills: ' + lst(c['key_skills'])]
    extra = str(c['career_summary'] or '').strip() or str(c['embedding_text'] or '').strip()
    if extra:
        lines.append('Profile/resume: ' + re.sub(r'\s+', ' ', extra)[:1300])
    return '\n'.join(lines)


def _ask_ai(core, key, job, cands):
    user = 'JOB\n' + job + '\n\nCANDIDATES\n' + '\n---\n'.join(_cand_brief(c) for c in cands)
    payload = {'model': 'deepseek-chat', 'temperature': 0, 'max_tokens': 1200,
               'response_format': {'type': 'json_object'},
               'messages': [{'role': 'system', 'content': AI_SYSTEM}, {'role': 'user', 'content': user}]}
    resp = core.call_deepseek(key, payload, timeout=90, endpoint='jobmail-verify')
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
            v = str(x.get('verdict') or '').strip().lower().replace(' ', '_').replace('-', '_')
            if v not in ('fit', 'maybe', 'not_fit'):
                continue
            out[int(x.get('id'))] = (v, re.sub(r'\s+', ' ', str(x.get('reason') or '')).strip()[:200])
        except Exception:
            continue
    return out


def run_ai_check(oid, camp_id):
    """Background: verify every eligible recipient of a draft campaign."""
    core = _core()
    conn = get_db()
    core._bg_tenant_set(oid)
    try:
        camp = _campaign(conn, oid, camp_id)
        m = _mandate(conn, oid, camp['mandate_id']) if camp else None
        if not camp or not m:
            return
        key = (core.get_setting('deepseek_api_key') or '').strip()
        if not key:
            conn.execute("UPDATE jobmail_campaigns SET ai_status='failed', ai_error=? WHERE id=?",
                         ('DeepSeek API key is not set (Settings).', camp_id))
            conn.commit()
            return
        recs = _rows(conn, "SELECT r.id AS rid, c.id, c.designation, c.company, c.experience, c.location, c.ctc_current, "
                           "c.key_skills, c.career_summary, c.embedding_text FROM jobmail_recipients r "
                           "JOIN candidates c ON c.id=r.candidate_id WHERE r.campaign_id=? AND r.eligible=1 "
                           "ORDER BY r.score DESC", (camp_id,))
        job = _job_brief(m)
        done = 0
        for i in range(0, len(recs), AI_BATCH):
            batch = recs[i:i + AI_BATCH]
            try:
                res = _ask_ai(core, key, job, batch)
            except Exception as exc:
                conn.execute("UPDATE jobmail_campaigns SET ai_error=? WHERE id=?", (str(exc)[:200], camp_id))
                conn.commit()
                res = {}
            for c in batch:
                v = res.get(int(c['id']))
                if v:
                    conn.execute('UPDATE jobmail_recipients SET ai_verdict=?, ai_reason=?, '
                                 'selected=CASE WHEN ?=\'not_fit\' THEN 0 ELSE selected END WHERE id=?',
                                 (v[0], v[1], v[0], c['rid']))
            done += len(batch)
            conn.execute('UPDATE jobmail_campaigns SET ai_done=? WHERE id=?', (done, camp_id))
            conn.commit()
        conn.execute("UPDATE jobmail_campaigns SET ai_status='done' WHERE id=?", (camp_id,))
        conn.commit()
    except Exception as exc:
        try:
            conn.execute("UPDATE jobmail_campaigns SET ai_status='failed', ai_error=? WHERE id=?", (str(exc)[:200], camp_id))
            conn.commit()
        except Exception:
            pass
    finally:
        core._bg_tenant_clear()
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  3. THE E-MAIL
# ══════════════════════════════════════════════════════════════════════════
def job_facts(m, camp):
    loc = str(_mval(m, 'location', '') or '').strip()
    who = (m['client'] or '').strip() if camp.get('show_client') else ''
    who = who or (camp.get('client_label') or '').strip() or 'one of our clients'
    exp = ''
    lo, hi = _num(_mval(m, 'exp_min')), _num(_mval(m, 'exp_max'))
    if lo or hi:
        exp = ('%g–%g years' % (lo, hi)) if (lo and hi) else ('%g+ years' % (lo or hi))
    ctc = ''
    if camp.get('show_ctc'):
        cmin, cmax = _num(_mval(m, 'ctc_min')), _num(_mval(m, 'ctc_max'))
        if cmax:
            ctc = ('%g–%g LPA' % (cmin, cmax)) if cmin else ('Up to %g LPA' % cmax)
    return {'role': (m['role'] or '').strip(), 'who': who, 'location': loc, 'exp': exp, 'ctc': ctc,
            'skills': _skills(m, 'must_have_skills')[:6]}


# Editable e-mail text. Placeholders are filled per person; an empty field uses the default.
TPL_KEYS = ('subject_tpl', 'intro_tpl', 'ask_tpl', 'after_tpl')
DEFAULT_TPL = {
    'subject_tpl': '{first_name}, {role} opening{in_location} — matches your profile',
    'intro_tpl': 'We are hiring for a {role} position with {client}{in_location}. Your background{as_current_role} '
                 'looks like a close match, so we wanted you to hear about it first.',
    'ask_tpl': 'Are you open to exploring this?',
    'after_tpl': 'If you say yes, a HireLab recruiter will call you within one working day to share the full details.',
}
PLACEHOLDERS = [('{first_name}', 'Asha'), ('{role}', 'job title'), ('{client}', 'client name or your description'),
                ('{location}', 'job location'), ('{in_location}', '" in Noida" or nothing'),
                ('{as_current_role}', '" as Project Engineer at Tata Power" or nothing'),
                ('{designation}', 'their designation'), ('{company}', 'their company'), ('{experience}', 'job experience range')]
_PH_RX = re.compile(r'\{([a-z_]+)\}')


def fill(text, values):
    """Replace known {placeholders}; unknown ones are left as typed."""
    return _PH_RX.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), str(text or ''))


def tpl_of(camp):
    """The four text pieces for a campaign — its own text, else the built-in default."""
    camp = camp or {}
    return {k: (str(camp.get(k) or '').strip() or DEFAULT_TPL[k]) for k in TPL_KEYS}


def _paras(text):
    return [x.strip() for x in re.split(r'\n\s*\n', str(text or '')) if x.strip()]


def compose(p, facts, yes_link, no_link, unsub, note='', pixel='', has_signature=True, tpl=None):
    """(subject, plain, html) for one person."""
    fn = first_name(p.get('name'))
    role = facts['role'] or 'a new role'
    t = tpl or tpl_of(None)
    bg = ''
    if p.get('designation') and p.get('company'):
        bg = ' as %s at %s' % (p['designation'], p['company'])
    elif p.get('designation'):
        bg = ' as %s' % p['designation']
    values = {'first_name': fn, 'role': role, 'client': facts['who'], 'location': facts['location'] or '',
              'in_location': (' in %s' % facts['location']) if facts['location'] else '', 'as_current_role': bg,
              'designation': p.get('designation') or '', 'company': p.get('company') or '', 'experience': facts['exp'] or ''}
    subject = re.sub(r'\s+', ' ', fill(t['subject_tpl'], values)).strip()[:180]
    opening = fill(t['intro_tpl'], values).strip()
    rows = [('Role', role)]
    if facts['location']:
        rows.append(('Location', facts['location']))
    if facts['exp']:
        rows.append(('Experience', facts['exp']))
    if facts['skills']:
        rows.append(('Key skills', ', '.join(facts['skills'])))
    if facts['ctc']:
        rows.append(('Compensation', facts['ctc']))
    ask = fill(t['ask_tpl'], values).strip()
    after = fill(t['after_tpl'], values).strip()
    sign_off = 'Warm regards,' if has_signature else 'Warm regards,\nTeam HireLab'
    note = (note or '').strip()
    plain = 'Hi %s,\n\n%s\n\n%s\n\n%s%s\n\nYes, I am interested: %s\nNot right now: %s\n\n%s\n\n%s\n\n---\n' \
            'You are receiving this because your profile is with HireLab, a recruitment agency for Data Center, ' \
            'Solar, Electrical and Automation roles. To stop these e-mails: %s' % (
                fn, opening, '\n'.join('%s: %s' % r for r in rows), (note + '\n\n') if note else '', ask,
                yes_link, no_link, after, sign_off, unsub)
    e = html.escape
    table = ''.join('<tr><td style="padding:6px 14px 6px 0;color:#6B6F76;white-space:nowrap;vertical-align:top">%s</td>'
                    '<td style="padding:6px 0;font-weight:600">%s</td></tr>' % (e(k), e(v)) for k, v in rows)
    para = lambda txt: ''.join('<p>%s</p>' % e(x).replace('\n', '<br>') for x in _paras(txt))
    body = ('<p>Hi %s,</p>%s'
            '<table style="border-collapse:collapse;margin:6px 0 14px;font-size:14px">%s</table>'
            '%s<p><b>%s</b></p>'
            '<p><a href="%s" style="display:inline-block;background:#0F6E56;color:#fff;padding:11px 22px;border-radius:8px;'
            'text-decoration:none;font-weight:600;margin:0 8px 8px 0">Yes, I\'m interested</a>'
            '<a href="%s" style="display:inline-block;background:#fff;color:#0E2A47;padding:10px 20px;border-radius:8px;'
            'text-decoration:none;font-weight:600;border:1px solid #C9C6BC;margin:0 0 8px">Not right now</a></p>'
            '<div style="font-size:13px;color:#555">%s</div><p>%s</p>'
            '<p style="font-size:11px;color:#888;margin-top:22px">You are receiving this because your profile is with '
            'HireLab, a recruitment agency for Data Center, Solar, Electrical and Automation roles. '
            '<a href="%s" style="color:#888">Unsubscribe</a></p>%s'
            % (e(fn), para(opening), table, ('<p>%s</p>' % e(note).replace('\n', '<br>')) if note else '', e(ask),
               e(yes_link), e(no_link), para(after), e(sign_off).replace('\n', '<br>'), e(unsub),
               ('<img src="%s" width="1" height="1" alt="" style="display:block">' % e(pixel)) if pixel else ''))
    return subject, plain, body


def _links(rid, oid, cid):
    base, t = _base(), token_for(rid)
    return ('%s/jm/r/%s?a=yes' % (base, t), '%s/jm/r/%s?a=no' % (base, t),
            '%s/u/%s' % (base, _core()._unsub_token(oid, cid)), '%s/jm/o/%s.gif' % (base, t))


# ══════════════════════════════════════════════════════════════════════════
#  4. SEND — background thread
# ══════════════════════════════════════════════════════════════════════════
def _mail_off(conn, oid, rec):
    try:
        from modules import mail_guard as _mg
        return _mg.is_blocked(conn, oid, rec['candidate_id'], rec['email'])
    except Exception:
        return False


def send_one(conn, oid, rec, m, camp):
    """Claim + send one queued recipient. Returns (sent, err)."""
    core = _core()
    cur = conn.execute("UPDATE jobmail_recipients SET status='sending' WHERE id=? AND status='queued'", (rec['id'],))
    conn.commit()
    if cur.rowcount != 1:
        return False, 'already handled'
    s = get_settings(conn, oid)
    # last-moment checks: unsubscribed since approval / mailed by something else / job closed
    dne = conn.execute("SELECT MAX(COALESCE(do_not_email,0)) FROM candidates WHERE owner_id=? AND LOWER(TRIM(email))=?",
                       (oid, rec['email'])).fetchone()[0]
    skip = ''
    if (m['status'] or '') != 'active':
        skip = 'Job closed before sending'
    elif dne:
        skip = 'Unsubscribed before sending'
    elif _mail_off(conn, oid, rec):
        skip = 'Automated e-mails switched off for their job'
    else:
        recent = recent_mail_map(conn, oid, s['gap_days'], exclude_campaign=camp['id'])
        if rec['email'] in recent and recent[rec['email']] != 'queued':
            skip = 'Got another HireLab e-mail in the last %d days' % int(s['gap_days'])
    if skip:
        conn.execute("UPDATE jobmail_recipients SET status='skipped', skip_reason=? WHERE id=?", (skip, rec['id']))
        conn.commit()
        return False, 'skipped'
    try:
        yes, no, unsub, pixel = _links(rec['id'], oid, rec['candidate_id'])
        subject, plain, body = compose(rec, job_facts(m, camp), yes, no, unsub, camp.get('note') or '', pixel,
                                       _has_signature(), tpl_of(camp))
        ok, err, _mid, _tid = core.email_service_send(
            rec['email'], subject, plain, body_html=body, candidate_id=rec['candidate_id'],
            extra_headers={'List-Unsubscribe': '<%s>' % unsub, 'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click'})
    except Exception as exc:
        ok, err = False, '%s: %s' % (type(exc).__name__, exc)
    if ok:
        conn.execute("UPDATE jobmail_recipients SET status='sent', sent_at=? WHERE id=?", (ts(), rec['id']))
        try:
            conn.execute('INSERT INTO candidate_events (candidate_id,event_type,detail,created_at) VALUES (?,?,?,?)',
                         (rec['candidate_id'], 'note', 'Job-match e-mail sent: %s (%s%% match)'
                          % (m['role'] or 'job', rec['score']), ts()))
        except Exception:
            pass
    else:
        conn.execute("UPDATE jobmail_recipients SET status='failed', error=? WHERE id=?", (str(err or '')[:300], rec['id']))
    conn.commit()
    return bool(ok), err


def sent_today(conn, oid):
    day = now_ist().strftime('%Y-%m-%d')
    return conn.execute("SELECT COUNT(*) FROM jobmail_recipients WHERE company_id=? AND status='sent' AND sent_at LIKE ?",
                        (oid, day + '%')).fetchone()[0]


def _finish_campaigns(conn, oid):
    for c in conn.execute("SELECT id FROM jobmail_campaigns WHERE company_id=? AND status='sending'", (oid,)).fetchall():
        left = conn.execute("SELECT COUNT(*) FROM jobmail_recipients WHERE campaign_id=? AND status IN ('queued','sending')",
                            (c['id'],)).fetchone()[0]
        if not left:
            conn.execute("UPDATE jobmail_campaigns SET status='done', finished_at=? WHERE id=?", (ts(), c['id']))
    conn.commit()


def run_company(conn, oid, force=False, limit=None):
    """One sending pass for one company. Returns how many were sent."""
    s = get_settings(conn, oid)
    now = now_ist()
    if not force and (now.weekday() == 6 or not (int(s['start_hour']) <= now.hour < int(s['end_hour']))):
        return 0
    left = int(s['daily_cap']) - sent_today(conn, oid)
    if left <= 0:
        return 0
    if limit is None:
        end = now.replace(hour=min(23, int(s['end_hour'])), minute=0, second=0, microsecond=0)
        ticks = max(1, int((end - now).total_seconds() // TICK_SECONDS))
        limit = min(MAX_PER_TICK, max(1, math.ceil(left / ticks)))
    limit = min(limit, left)
    recs = _rows(conn, "SELECT r.* FROM jobmail_recipients r JOIN jobmail_campaigns c ON c.id=r.campaign_id "
                       "WHERE r.company_id=? AND r.status='queued' AND c.status='sending' "
                       "ORDER BY c.approved_at, r.score DESC LIMIT ?", (oid, max(limit * 4, 20)))
    camps, mands, n = {}, {}, 0
    for rec in recs:
        if n >= limit:
            break
        cid = rec['campaign_id']
        if cid not in camps:
            camps[cid] = _campaign(conn, oid, cid)
            mands[cid] = _mandate(conn, oid, camps[cid]['mandate_id']) if camps[cid] else None
        if not camps[cid] or not mands[cid]:
            continue
        ok, err = send_one(conn, oid, rec, mands[cid], camps[cid])
        if ok:
            n += 1
        elif err and err not in ('already handled', 'skipped') and any(
                w in err.lower() for w in ('not configured', 'auth', 'username and password', 'connect', 'timed out',
                                           'timeout', 'name or service', 'network', 'temporarily', 'try again later')):
            # mailbox / network down: put this one back and stop until the next tick
            conn.execute("UPDATE jobmail_recipients SET status='queued', error='' WHERE id=?", (rec['id'],))
            conn.execute('UPDATE jobmail_settings SET last_error=? WHERE company_id=?', (err[:300], oid))
            conn.commit()
            break
    if n:
        conn.execute("UPDATE jobmail_settings SET last_error='' WHERE company_id=?", (oid,))
        conn.commit()
    _finish_campaigns(conn, oid)
    return n


def tick():
    core = _core()
    conn = get_db()
    try:
        cos = [r[0] for r in conn.execute("SELECT DISTINCT r.company_id FROM jobmail_recipients r JOIN jobmail_campaigns c "
                                          "ON c.id=r.campaign_id WHERE r.status='queued' AND c.status='sending'").fetchall()]
        stamp = ts()
        for oid in cos:
            conn.execute('INSERT OR IGNORE INTO jobmail_settings (company_id) VALUES (?)', (oid,))
            cur = conn.execute('UPDATE jobmail_settings SET tick_at=? WHERE company_id=? AND (tick_at IS NULL OR tick_at<?)',
                               (stamp, oid, (now_ist() - datetime.timedelta(seconds=TICK_SECONDS - 20)).strftime('%Y-%m-%dT%H:%M:%S')))
            conn.commit()
            if cur.rowcount != 1:
                continue
            core._bg_tenant_set(oid)
            try:
                run_company(conn, oid)
            except Exception as e:
                print('[jobmail] company %s failed: %s' % (oid, e))
            finally:
                core._bg_tenant_clear()
    finally:
        conn.close()


def _loop():
    time.sleep(75)
    while True:
        try:
            tick()
        except Exception as e:
            print('[jobmail] loop error: %s' % e)
        time.sleep(TICK_SECONDS)


_started = False


def start_worker():
    global _started
    if _started or 'pytest' in sys.modules or os.environ.get('HIRELAB_NO_WORKERS'):
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
    print('[jobmail] background thread started')


start_worker()


# ══════════════════════════════════════════════════════════════════════════
#  5. RESPONSES — Interested → pipeline + call task;  Not now → reason
# ══════════════════════════════════════════════════════════════════════════
def _create_task(conn, oid, user_id, cand_id, mid, title, notes, due):
    m = conn.execute('SELECT role, client FROM mandates WHERE id=?', (mid,)).fetchone()
    c = conn.execute('SELECT name FROM candidates WHERE id=?', (cand_id,)).fetchone()
    row = {'candidate_id': cand_id, 'mandate_id': mid, 'candidate_name': (c['name'] if c else '') or '',
           'mandate_label': ((m['role'] or '') + (' — ' + m['client'] if m and m['client'] else '')) if m else '',
           'note': title, 'notes': notes, 'due_at': due, 'done': 0, 'stage': 'todo', 'created_at': ts(),
           'owner_id': oid, 'created_by': user_id, 'assigned_to': user_id, 'updated_at': ts(), 'priority': 'high'}
    cols = {r[1] for r in conn.execute('PRAGMA table_info(reminders)')}
    row = {k: v for k, v in row.items() if k in cols}
    cur = conn.execute('INSERT INTO reminders (%s) VALUES (%s)' % (','.join(row), ','.join('?' * len(row))),
                       list(row.values()))
    return cur.lastrowid


def _next_call_slot():
    """Within the next working hours (IST), as 'YYYY-MM-DDTHH:MM'."""
    n = now_ist() + datetime.timedelta(hours=1)
    if n.hour >= 19 or n.weekday() == 6:
        n = (n + datetime.timedelta(days=1)).replace(hour=10, minute=30)
        if n.weekday() == 6:
            n += datetime.timedelta(days=1)
    elif n.hour < 10:
        n = n.replace(hour=10, minute=30)
    return n.strftime('%Y-%m-%dT%H:%M')


def record_interest(conn, rec):
    """Put the person on the job at 'Interested' and give the recruiter a call task.
    Returns a short status: placed | already | closed."""
    from modules.matching import load_src, place_in_job
    core = _core()
    oid, mid = rec['company_id'], rec['mandate_id']
    camp = _campaign(conn, oid, rec['campaign_id']) or {}
    m = _mandate(conn, oid, mid)
    now = ts()
    conn.execute("UPDATE jobmail_recipients SET response='interested', responded_at=?, "
                 "clicked_at=CASE WHEN COALESCE(clicked_at,'')='' THEN ? ELSE clicked_at END WHERE id=?", (now, now, rec['id']))
    conn.commit()
    if not m or (m['status'] or '') != 'active':
        conn.execute('INSERT INTO candidate_events (candidate_id,event_type,detail,created_at) VALUES (?,?,?,?)',
                     (rec['candidate_id'], 'note', 'Said INTERESTED to job e-mail (%s) — job is no longer active'
                      % ((m['role'] if m else '') or 'job'), now))
        conn.commit()
        return 'closed'
    src = load_src(conn, oid, rec['candidate_id'])
    if not src:
        return 'closed'
    how, new_id = place_in_job(conn, src, mid, 'Replied "Interested" to job-match e-mail (%s%% match)' % rec['score'])
    new_id = new_id or rec['candidate_id']
    if how in ('moved', 'copied'):
        conn.execute("UPDATE candidates SET stage='Interested', updated_at=? WHERE id=?", (now, new_id))
        conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                     (new_id, 'Screening', 'Interested', 'Candidate confirmed interest by e-mail', now))
    conn.execute('INSERT INTO candidate_events (candidate_id,event_type,detail,created_at) VALUES (?,?,?,?)',
                 (new_id, 'note', 'Said INTERESTED to job e-mail: %s' % (m['role'] or ''), now))
    uid = int(camp.get('approved_by') or camp.get('created_by') or 0)
    task = 0
    try:
        task = _create_task(conn, oid, uid, new_id, mid, 'Call %s — interested in %s (job e-mail)' % (rec['name'] or 'candidate', m['role'] or 'job'),
                            'Replied "Yes, I\'m interested" to the job-match e-mail. Match %s%%.%s' % (
                                rec['score'], (' AI: ' + rec['ai_reason']) if rec.get('ai_reason') else ''),
                            _next_call_slot())
    except Exception as exc:
        print('[jobmail] task create failed: %s' % exc)
    conn.execute('UPDATE jobmail_recipients SET job_candidate_id=?, task_id=? WHERE id=?', (new_id, task, rec['id']))
    conn.commit()
    return 'placed' if how in ('moved', 'copied') else 'already'


def record_not_now(conn, rec, reason_key):
    labels = dict(NOT_NOW_REASONS)
    reason = labels.get(reason_key, '')
    now = ts()
    conn.execute("UPDATE jobmail_recipients SET response='not_now', response_reason=?, responded_at=?, "
                 "clicked_at=CASE WHEN COALESCE(clicked_at,'')='' THEN ? ELSE clicked_at END WHERE id=?",
                 (reason, now, now, rec['id']))
    m = _mandate(conn, rec['company_id'], rec['mandate_id'])
    conn.execute('INSERT INTO candidate_events (candidate_id,event_type,detail,created_at) VALUES (?,?,?,?)',
                 (rec['candidate_id'], 'note', 'Replied "Not right now" to job e-mail (%s)%s'
                  % ((m['role'] if m else '') or 'job', (': ' + reason) if reason else ''), now))
    if reason_key == 'later':
        camp = _campaign(conn, rec['company_id'], rec['campaign_id']) or {}
        uid = int(camp.get('approved_by') or camp.get('created_by') or 0)
        due = (now_ist() + datetime.timedelta(days=90)).replace(hour=11, minute=0).strftime('%Y-%m-%dT%H:%M')
        try:
            tid = _create_task(conn, rec['company_id'], uid, rec['candidate_id'], 0,
                               'Reconnect with %s — asked to be contacted in 3 months' % (rec['name'] or 'candidate'),
                               'Said "open to it later" to the %s job e-mail.' % ((m['role'] if m else '') or ''), due)
            conn.execute('UPDATE jobmail_recipients SET task_id=? WHERE id=?', (tid, rec['id']))
        except Exception as exc:
            print('[jobmail] later-task failed: %s' % exc)
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  API (logged-in users; recruiters only on jobs they can work on)
# ══════════════════════════════════════════════════════════════════════════
def _oid():
    return int(effective_company_id() or 0)


def _load_for_write(conn, cid):
    """(campaign, error_response)."""
    oid = _oid()
    camp = _campaign(conn, oid, cid)
    if not camp or not _can_write_mandate(conn, camp['mandate_id']):
        return None, _err('Not found', 404)
    return camp, None


def _counts(conn, cid):
    r = conn.execute("SELECT COUNT(*) AS total, SUM(eligible) AS eligible, "
                     "SUM(CASE WHEN eligible=1 AND selected=1 THEN 1 ELSE 0 END) AS selected, "
                     "SUM(CASE WHEN status='queued' OR status='sending' THEN 1 ELSE 0 END) AS queued, "
                     "SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent, "
                     "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
                     "SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped, "
                     "SUM(CASE WHEN COALESCE(opened_at,'')<>'' THEN 1 ELSE 0 END) AS opened, "
                     "SUM(CASE WHEN COALESCE(clicked_at,'')<>'' THEN 1 ELSE 0 END) AS clicked, "
                     "SUM(CASE WHEN response='interested' THEN 1 ELSE 0 END) AS interested, "
                     "SUM(CASE WHEN response='not_now' THEN 1 ELSE 0 END) AS not_now, "
                     "SUM(CASE WHEN ai_verdict='fit' THEN 1 ELSE 0 END) AS ai_fit, "
                     "SUM(CASE WHEN ai_verdict='maybe' THEN 1 ELSE 0 END) AS ai_maybe, "
                     "SUM(CASE WHEN ai_verdict='not_fit' THEN 1 ELSE 0 END) AS ai_not_fit "
                     "FROM jobmail_recipients WHERE campaign_id=?", (cid,)).fetchone()
    return {k: int(r[k] or 0) for k in r.keys()}


def _camp_out(conn, camp, m=None):
    oid = camp['company_id']
    m = m or _mandate(conn, oid, camp['mandate_id'])
    out = {k: camp[k] for k in ('id', 'mandate_id', 'status', 'show_client', 'client_label', 'show_ctc', 'note',
                                'ai_status', 'ai_done', 'ai_total', 'ai_error', 'scanned', 'created_at', 'built_at',
                                'approved_at', 'finished_at')}
    out['filters'] = _filters(camp)
    out['counts'] = _counts(conn, camp['id'])
    out['text'] = {k: str(camp.get(k) or '') for k in TPL_KEYS}
    out['text_defaults'] = DEFAULT_TPL
    out['placeholders'] = [{'tag': a, 'means': b} for a, b in PLACEHOLDERS]
    if m:
        out['job'] = {'id': m['id'], 'role': m['role'] or '', 'client': m['client'] or '',
                      'location': _mval(m, 'location', '') or '', 'status': m['status'] or '',
                      'exp_min': _num(_mval(m, 'exp_min')), 'exp_max': _num(_mval(m, 'exp_max')),
                      'ctc_min': _num(_mval(m, 'ctc_min')), 'ctc_max': _num(_mval(m, 'ctc_max'))}
    return out


@bp.route('/settings', methods=['GET'])
@login_required
def settings_get():
    conn = get_db()
    try:
        s = get_settings(conn, _oid())
        try:
            sender = _core()._smtp_params()[0]
        except Exception:
            sender = ''
        return jsonify({'ok': True, 'settings': s, 'sender': sender, 'is_admin': bool(is_company_admin()),
                        'sent_today': sent_today(conn, _oid())})
    finally:
        conn.close()


@bp.route('/settings', methods=['PUT', 'POST'])
@login_required
def settings_save():
    if not is_company_admin():
        return _err('Only an admin can change job-mail settings.', 403)
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
        vals = {'daily_cap': num('daily_cap', 1, 500), 'gap_days': num('gap_days', 0, 60),
                'start_hour': num('start_hour', 0, 23), 'end_hour': num('end_hour', 1, 24),
                'default_min': num('default_min', 0, 100), 'default_limit': num('default_limit', 1, 150)}
        if vals['end_hour'] <= vals['start_hour']:
            return _err('End hour must be after start hour.')
        conn.execute('INSERT OR IGNORE INTO jobmail_settings (company_id) VALUES (?)', (oid,))
        conn.execute('UPDATE jobmail_settings SET daily_cap=?, gap_days=?, start_hour=?, end_hour=?, default_min=?, '
                     'default_limit=?, updated_by=?, updated_at=? WHERE company_id=?',
                     (vals['daily_cap'], vals['gap_days'], vals['start_hour'], vals['end_hour'], vals['default_min'],
                      vals['default_limit'], real_user_id(), ts(), oid))
        conn.commit()
        return jsonify({'ok': True, 'settings': get_settings(conn, oid)})
    finally:
        conn.close()


@bp.route('/jobs', methods=['GET'])
@login_required
def jobs_list():
    """Active jobs the caller can work on (for the 'new campaign' picker)."""
    from modules.access import mandate_scope_sql
    oid = _oid()
    ms, mp = mandate_scope_sql('m.id')
    conn = get_db()
    try:
        rows = _rows(conn, "SELECT m.id, m.role, m.client, m.location, "
                           "(SELECT COUNT(*) FROM jobmail_campaigns c WHERE c.mandate_id=m.id AND c.company_id=m.owner_id) AS campaigns "
                           "FROM mandates m WHERE m.owner_id=? AND m.status='active'" + ms + " ORDER BY m.id DESC", [oid] + mp)
        return jsonify({'ok': True, 'jobs': rows})
    finally:
        conn.close()


@bp.route('/campaigns', methods=['GET'])
@login_required
def campaigns_list():
    from modules.access import mandate_scope_sql
    oid = _oid()
    ms, mp = mandate_scope_sql('c.mandate_id')
    conn = get_db()
    try:
        camps = _rows(conn, "SELECT c.* FROM jobmail_campaigns c WHERE c.company_id=?" + ms +
                      " ORDER BY c.id DESC LIMIT 200", [oid] + mp)
        out = [_camp_out(conn, c) for c in camps]
        return jsonify({'ok': True, 'campaigns': out})
    finally:
        conn.close()


@bp.route('/campaigns', methods=['POST'])
@login_required
def campaign_create():
    """Open the draft for a job (one draft per job at a time) and build it."""
    d = request.get_json(silent=True) or {}
    oid = _oid()
    try:
        mid = int(d.get('job_id') or 0)
    except (TypeError, ValueError):
        mid = 0
    conn = get_db()
    try:
        m = _mandate(conn, oid, mid)
        if not m or (m['status'] or '') == 'central' or not _can_write_mandate(conn, mid):
            return _err('Job not found', 404)
        r = conn.execute("SELECT id FROM jobmail_campaigns WHERE company_id=? AND mandate_id=? AND status='draft' "
                         "ORDER BY id DESC LIMIT 1", (oid, mid)).fetchone()
        if r:
            camp = _campaign(conn, oid, r['id'])
            fresh = False
        else:
            dflt = conn.execute('SELECT * FROM jobmail_settings WHERE company_id=?', (oid,)).fetchone()
            dt = {k: (str(dflt[k] or '') if dflt and k in dflt.keys() else '') for k in TPL_KEYS}
            cur = conn.execute('INSERT INTO jobmail_campaigns (company_id, mandate_id, status, filters, created_by, created_at, '
                               'subject_tpl, intro_tpl, ask_tpl, after_tpl) VALUES (?,?,?,?,?,?,?,?,?,?)',
                               (oid, mid, 'draft', '{}', int(real_user_id() or 0), ts(),
                                dt['subject_tpl'], dt['intro_tpl'], dt['ask_tpl'], dt['after_tpl']))
            conn.commit()
            camp = _campaign(conn, oid, cur.lastrowid)
            fresh = True
        msg = ''
        if fresh or not camp['built_at']:
            ok, msg = build_campaign(conn, oid, camp)
            camp = _campaign(conn, oid, camp['id'])
        return jsonify({'ok': True, 'campaign': _camp_out(conn, camp, m), 'message': msg})
    finally:
        conn.close()


@bp.route('/campaign/<int:cid>', methods=['GET'])
@login_required
def campaign_get(cid):
    conn = get_db()
    try:
        camp, e = _load_for_write(conn, cid)
        if e:
            return e
        recs = _rows(conn, 'SELECT r.*, COALESCE(c.mandate_id,0) AS cand_mandate_id FROM jobmail_recipients r '
                           'LEFT JOIN candidates c ON c.id=r.candidate_id WHERE r.campaign_id=? '
                           'ORDER BY r.eligible DESC, r.score DESC', (cid,))
        for r in recs:
            r.pop('company_id', None)
        return jsonify({'ok': True, 'campaign': _camp_out(conn, camp), 'recipients': recs})
    finally:
        conn.close()


@bp.route('/campaign/<int:cid>', methods=['PUT', 'POST'])
@login_required
def campaign_update(cid):
    """Draft only: filters, mail options and the tick-box selection."""
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        camp, e = _load_for_write(conn, cid)
        if e:
            return e
        if camp['status'] != 'draft':
            return _err('This campaign was already approved.')
        f = _filters(camp)
        for k in DEFAULT_FILTERS:
            if k in (d.get('filters') or {}):
                v = d['filters'][k]
                if k in ('use_exp', 'use_ctc', 'use_location'):
                    f[k] = bool(v)
                elif v in (None, ''):
                    f[k] = None
                else:
                    try:
                        f[k] = max(0, min(150 if k == 'limit' else 100, float(v)))
                    except (TypeError, ValueError):
                        pass
        sets = {'filters': json.dumps(f)}
        for k in ('show_client', 'show_ctc'):
            if k in d:
                sets[k] = 1 if d[k] in (1, True, '1', 'true') else 0
        for k, n in (('client_label', 120), ('note', 1200)):
            if k in d:
                sets[k] = str(d[k] or '').strip()[:n]
        txt = d.get('text') if isinstance(d.get('text'), dict) else {}
        for k in TPL_KEYS:
            if k in txt:
                v = str(txt[k] or '').strip()
                sets[k] = '' if v == DEFAULT_TPL[k] else v[:200 if k == 'subject_tpl' else 2500]
        if d.get('reset_text'):
            for k in TPL_KEYS:
                sets[k] = ''
        conn.execute('UPDATE jobmail_campaigns SET %s WHERE id=?' % ','.join(k + '=?' for k in sets),
                     list(sets.values()) + [cid])
        if d.get('save_text_as_default'):
            if not is_company_admin():
                return _err('Only an admin can change the default text for everyone.', 403)
            cur_c = _campaign(conn, _oid(), cid)
            conn.execute('INSERT OR IGNORE INTO jobmail_settings (company_id) VALUES (?)', (_oid(),))
            conn.execute('UPDATE jobmail_settings SET subject_tpl=?, intro_tpl=?, ask_tpl=?, after_tpl=? WHERE company_id=?',
                         tuple(cur_c[k] or '' for k in TPL_KEYS) + (_oid(),))
        sel = d.get('selection')
        if isinstance(sel, dict):
            for rid, on in list(sel.items())[:1000]:
                try:
                    conn.execute('UPDATE jobmail_recipients SET selected=? WHERE id=? AND campaign_id=? AND eligible=1',
                                 (1 if on else 0, int(rid), cid))
                except (TypeError, ValueError):
                    continue
        conn.commit()
        camp = _campaign(conn, _oid(), cid)
        msg = ''
        if d.get('rebuild'):
            ok, msg = build_campaign(conn, _oid(), camp)
            if not ok:
                return _err(msg)
            camp = _campaign(conn, _oid(), cid)
        return jsonify({'ok': True, 'campaign': _camp_out(conn, camp), 'message': msg})
    finally:
        conn.close()


@bp.route('/campaign/<int:cid>/ai-check', methods=['POST'])
@login_required
def campaign_ai_check(cid):
    conn = get_db()
    try:
        camp, e = _load_for_write(conn, cid)
        if e:
            return e
        if camp['status'] != 'draft':
            return _err('This campaign was already approved.')
        if camp['ai_status'] == 'running':
            return jsonify({'ok': True, 'campaign': _camp_out(conn, camp)})
        n = conn.execute('SELECT COUNT(*) FROM jobmail_recipients WHERE campaign_id=? AND eligible=1', (cid,)).fetchone()[0]
        if not n:
            return _err('Nobody on the list to check.')
        conn.execute("UPDATE jobmail_campaigns SET ai_status='running', ai_done=0, ai_total=?, ai_error='' WHERE id=?", (n, cid))
        conn.execute("UPDATE jobmail_recipients SET ai_verdict='', ai_reason='' WHERE campaign_id=?", (cid,))
        conn.commit()
        oid = _oid()
        if os.environ.get('JOBMAIL_AI_SYNC') or 'pytest' in sys.modules:
            run_ai_check(oid, cid)
        else:
            threading.Thread(target=run_ai_check, args=(oid, cid), daemon=True).start()
        return jsonify({'ok': True, 'campaign': _camp_out(conn, _campaign(conn, oid, cid))})
    finally:
        conn.close()


def _sample_person(conn, cid):
    r = conn.execute('SELECT * FROM jobmail_recipients WHERE campaign_id=? AND eligible=1 ORDER BY score DESC LIMIT 1',
                     (cid,)).fetchone()
    return dict(r) if r else {'name': 'Asha Verma', 'designation': 'Project Engineer', 'company': 'Tata Power', 'score': 80}


@bp.route('/campaign/<int:cid>/preview', methods=['GET'])
@login_required
def campaign_preview(cid):
    conn = get_db()
    try:
        camp, e = _load_for_write(conn, cid)
        if e:
            return e
        m = _mandate(conn, _oid(), camp['mandate_id'])
        p = _sample_person(conn, cid)
        rid = request.args.get('recipient_id', type=int)
        if rid:
            r = conn.execute('SELECT * FROM jobmail_recipients WHERE id=? AND campaign_id=?', (rid, cid)).fetchone()
            p = dict(r) if r else p
        b = _base()
        subject, plain, body = compose(p, job_facts(m, camp), b + '/jm/r/…?a=yes', b + '/jm/r/…?a=no', b + '/u/…',
                                       camp['note'] or '', '', _has_signature(), tpl_of(camp))
        return jsonify({'ok': True, 'subject': subject, 'html': body, 'text': plain, 'to': p.get('email', ''),
                        'name': p.get('name', '')})
    finally:
        conn.close()


@bp.route('/campaign/<int:cid>/test', methods=['POST'])
@login_required
def campaign_test(cid):
    core = _core()
    conn = get_db()
    try:
        camp, e = _load_for_write(conn, cid)
        if e:
            return e
        sender = core._smtp_params()[0]
        if not sender:
            return _err('Company mailbox is not set. Settings → Email Configuration.')
        m = _mandate(conn, _oid(), camp['mandate_id'])
        p = _sample_person(conn, cid)
        b = _base()
        subject, plain, body = compose(p, job_facts(m, camp), b + '/jm/test?a=yes', b + '/jm/test?a=no', b + '/u/TEST',
                                       camp['note'] or '', '', _has_signature(), tpl_of(camp))
        ok, err, _m, _t = core.email_service_send(sender, '[TEST] ' + subject, plain, body_html=body)
        return jsonify({'ok': bool(ok), 'error': err or '', 'to': sender})
    finally:
        conn.close()


@bp.route('/campaign/<int:cid>/approve', methods=['POST'])
@login_required
def campaign_approve(cid):
    core = _core()
    conn = get_db()
    try:
        camp, e = _load_for_write(conn, cid)
        if e:
            return e
        if camp['status'] != 'draft':
            return _err('This campaign was already approved.')
        if camp['ai_status'] == 'running':
            return _err('Wait for the AI check to finish.')
        m = _mandate(conn, _oid(), camp['mandate_id'])
        if not m or (m['status'] or '') != 'active':
            return _err('This job is not active any more.')
        try:
            if not core._smtp_params()[0]:
                return _err('Company mailbox is not set. Settings → Email Configuration.')
        except Exception:
            pass
        n = conn.execute('SELECT COUNT(*) FROM jobmail_recipients WHERE campaign_id=? AND eligible=1 AND selected=1',
                         (cid,)).fetchone()[0]
        if not n:
            return _err('Tick at least one candidate.')
        conn.execute("UPDATE jobmail_recipients SET status='queued' WHERE campaign_id=? AND eligible=1 AND selected=1", (cid,))
        conn.execute("UPDATE jobmail_recipients SET status='not_selected' WHERE campaign_id=? AND eligible=1 AND selected=0", (cid,))
        conn.execute("UPDATE jobmail_campaigns SET status='sending', approved_by=?, approved_at=? WHERE id=?",
                     (int(real_user_id() or 0), ts(), cid))
        conn.execute('INSERT OR IGNORE INTO jobmail_settings (company_id) VALUES (?)', (_oid(),))
        conn.commit()
        s = get_settings(conn, _oid())
        left = max(0, int(s['daily_cap']) - sent_today(conn, _oid()))
        return jsonify({'ok': True, 'queued': n, 'campaign': _camp_out(conn, _campaign(conn, _oid(), cid)),
                        'days': math.ceil(n / max(1, int(s['daily_cap']))) if n > left else 1})
    finally:
        conn.close()


@bp.route('/campaign/<int:cid>/send-now', methods=['POST'])
@login_required
def campaign_send_now(cid):
    """Push up to 5 queued mails out right now (still inside today's cap)."""
    conn = get_db()
    try:
        camp, e = _load_for_write(conn, cid)
        if e:
            return e
        n = run_company(conn, _oid(), force=True, limit=5)
        return jsonify({'ok': True, 'sent': n, 'last_error': get_settings(conn, _oid()).get('last_error', ''),
                        'campaign': _camp_out(conn, _campaign(conn, _oid(), cid))})
    finally:
        conn.close()


@bp.route('/campaign/<int:cid>/cancel', methods=['POST'])
@login_required
def campaign_cancel(cid):
    conn = get_db()
    try:
        camp, e = _load_for_write(conn, cid)
        if e:
            return e
        if camp['status'] == 'draft':
            conn.execute('DELETE FROM jobmail_recipients WHERE campaign_id=?', (cid,))
            conn.execute("UPDATE jobmail_campaigns SET status='cancelled', finished_at=? WHERE id=?", (ts(), cid))
        else:
            conn.execute("UPDATE jobmail_recipients SET status='cancelled' WHERE campaign_id=? AND status='queued'", (cid,))
            conn.execute("UPDATE jobmail_campaigns SET status=CASE WHEN status='sending' THEN 'stopped' ELSE status END, "
                         "finished_at=? WHERE id=?", (ts(), cid))
        conn.commit()
        return jsonify({'ok': True, 'campaign': _camp_out(conn, _campaign(conn, _oid(), cid))})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  Public pages: open pixel, Interested / Not now
# ══════════════════════════════════════════════════════════════════════════
_GIF = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,'
        b'\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')

_PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HireLab</title><style>
body{margin:0;font:15px/1.55 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:#1F2328;background:#F7F6F2}
.c{max-width:480px;margin:9vh auto;background:#fff;border:1px solid #E3E1DA;border-radius:16px;padding:26px 24px}
.logo{width:34px;height:34px;border-radius:9px;background:#0F6E56;color:#fff;font-weight:700;display:flex;align-items:center;justify-content:center;margin-bottom:14px}
h1{font-size:19px;margin:0 0 8px;color:#0E2A47}p{margin:0 0 12px}.m{color:#6B6F76;font-size:13.5px}
.b{display:inline-block;border:none;border-radius:10px;padding:12px 20px;font:inherit;font-weight:700;cursor:pointer;text-decoration:none}
.g{background:#0F6E56;color:#fff}.o{background:#fff;color:#0E2A47;border:1px solid #C9C6BC}
label{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border:1px solid #E3E1DA;border-radius:10px;margin-bottom:8px;cursor:pointer}
label input{margin-top:4px}
</style></head><body><div class="c"><div class="logo">H</div>%s</div></body></html>'''


def _page(inner, code=200):
    return Response(_PAGE % inner, status=code, mimetype='text/html')


def _rec_for(tok):
    rid = parse_token(tok)
    if not rid:
        return None, None
    conn = get_db()
    r = conn.execute('SELECT * FROM jobmail_recipients WHERE id=?', (rid,)).fetchone()
    if not r:
        conn.close()
        return None, None
    return conn, dict(r)


@pages.route('/jm/o/<tok>.gif')
def track_open(tok):
    rid = parse_token(tok)
    if rid:
        conn = get_db()
        try:
            conn.execute("UPDATE jobmail_recipients SET opened_at=? WHERE id=? AND COALESCE(opened_at,'')=''", (ts(), rid))
            conn.commit()
        finally:
            conn.close()
    return Response(_GIF, mimetype='image/gif', headers={'Cache-Control': 'no-store, max-age=0'})


@pages.route('/jm/test')
def test_link():
    return _page('<h1>This is a test e-mail</h1><p class="m">In the real e-mail this button records the candidate\'s answer.</p>')


def _cv_link(conn, cand_id):
    try:
        from modules.reengage import _ensure_link
        return '/update-profile?token=' + _ensure_link(conn, cand_id)
    except Exception:
        return ''


@pages.route('/jm/r/<tok>', methods=['GET', 'POST'])
def respond(tok):
    conn, rec = _rec_for(tok)
    if not rec:
        return _page('<h1>This link is not valid</h1><p class="m">Please reply to our e-mail and we will get back to you.</p>', 404)
    e = html.escape
    try:
        m = _mandate(conn, rec['company_id'], rec['mandate_id'])
        role = e((m['role'] if m else '') or 'this role')
        fn = e(first_name(rec['name']))
        now = ts()
        conn.execute("UPDATE jobmail_recipients SET opened_at=? WHERE id=? AND COALESCE(opened_at,'')=''", (now, rec['id']))
        conn.commit()
        action = (request.form.get('a') if request.method == 'POST' else request.args.get('a')) or ''
        if request.method == 'POST' and action == 'yes':
            if rec['response'] == 'interested':
                st = 'already'
            else:
                st = record_interest(conn, rec)
            if st == 'closed':
                return _page('<h1>Thank you, %s</h1><p>This %s position has just closed, but we have noted your interest '
                             'and will reach out for similar roles.</p>' % (fn, role))
            rec2 = conn.execute('SELECT job_candidate_id FROM jobmail_recipients WHERE id=?', (rec['id'],)).fetchone()
            cv = _cv_link(conn, (rec2['job_candidate_id'] if rec2 else 0) or rec['candidate_id'])
            return _page('<h1>Thank you, %s!</h1><p>We have noted your interest in the <b>%s</b> position. A HireLab recruiter '
                         'will call you within one working day.</p>%s'
                         % (fn, role, ('<p class="m">To speed things up, share your latest CV and details (takes 2 minutes):</p>'
                                       '<p><a class="b g" href="%s">Update my CV &amp; details</a></p>' % e(cv)) if cv else ''))
        if request.method == 'POST' and action == 'no':
            key = request.form.get('reason') or ''
            record_not_now(conn, rec, key)
            later = key == 'later'
            return _page('<h1>Thank you for letting us know, %s</h1><p>%s</p>' % (
                fn, 'We will get back to you in about three months.' if later else
                'We will keep you in mind for roles that suit you better.'))
        # GET: never act on a GET (mail scanners open links) — ask to confirm
        conn.execute("UPDATE jobmail_recipients SET clicked_at=? WHERE id=? AND COALESCE(clicked_at,'')=''", (now, rec['id']))
        conn.commit()
        if rec['response'] == 'interested':
            return _page('<h1>You are all set, %s</h1><p>We already have your interest in the <b>%s</b> position. '
                         'A recruiter will call you shortly.</p>' % (fn, role))
        if action == 'no':
            opts = ''.join('<label><input type="radio" name="reason" value="%s"%s> %s</label>'
                           % (k, ' checked' if i == 0 else '', e(v)) for i, (k, v) in enumerate(NOT_NOW_REASONS))
            return _page('<h1>No problem, %s</h1><p class="m">Could you tell us why? It helps us send you better roles.</p>'
                         '<form method="post"><input type="hidden" name="a" value="no">%s'
                         '<p style="margin-top:14px"><button class="b g" type="submit">Submit</button></p></form>' % (fn, opts))
        return _page('<h1>Hi %s</h1><p>Please confirm that you are interested in the <b>%s</b> position. '
                     'A HireLab recruiter will call you within one working day.</p>'
                     '<form method="post"><input type="hidden" name="a" value="yes">'
                     '<button class="b g" type="submit">Yes, I\'m interested</button></form>'
                     '<p class="m" style="margin-top:14px">Not for you? <a href="?a=no">Tell us why</a></p>' % (fn, role))
    finally:
        conn.close()


@pages.route('/jobmail')
def jobmail_page():
    core = _core()
    if not core.current_user():
        return redirect('/')
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'jobmail.html')
    with open(p, encoding='utf-8') as f:
        return Response(f.read(), mimetype='text/html')
