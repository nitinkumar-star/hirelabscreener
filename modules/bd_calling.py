"""
BD Calling Mode — the BD workspace as a cold-calling sheet  (Nov 2026)

One screen for the whole loop:  call -> understand the business -> note -> email -> follow up.

  SHEET        Prospects of the open BD workspace as rows (company, contact, phone, e-mail,
               status, last outcome, next step). Cells edit in place. "Today" puts the
               callbacks that are due first, then people who replied or opened your e-mail,
               then prospects never called.
  OUTCOMES     One click: No answer / Callback / Interested / Send details / Meeting /
               Not interested / Wrong number. Each logs a call on the company timeline and
               sets the next step: a callback becomes a BD follow-up task at that time,
               "No answer" retries later, "Send details" opens an e-mail drafted from the
               call, "Not interested" asks why and brings the company back in 90 days.
  UNDERSTANDING  A short form per prospect: what they make, locations, size, roles they hire,
               hiring pain, who decides, agency used today, why now, notes.
               * AI research: reads the company website (+ the agency's Learning notes) and
                 fills the empty fields.
               * Voice / free text: say what you heard on the call; AI puts it in the fields.
  OPENER       A 30-second call opener and answers to 3 common objections, from the form.
  E-MAIL       AI writes 3 versions (short / value / candidate-led) with 3 subject lines each,
               from the form, the call notes, the agency's real placements (counted from the
               ATS, never invented) and the sender's own past e-mails (their style).
  BULK         Pick many rows -> one personalised draft each (background) -> review one by one
               (Enter = approve, S = skip) -> sent from the sender's own mailbox, spaced out,
               inside working hours, up to a daily cap.
  FOLLOW-UP    No reply after N days (default 3 and 7): a short follow-up in the same thread.
               A reply (seen by the Gmail sync) stops the sequence and rings the bell.
  TRACKING     Open pixel per e-mail: opened prospects move up in "Today".
  TARGETS      Calls and e-mails today against the user's daily target.
  IMPORT       Paste rows from Excel / CSV; duplicates (existing prospects or clients) are
               flagged; prospects + contacts are created in the open workspace.
  E-MAIL GUESS From the contact's name and the company's domain (or a colleague's address).

Additive only: new tables bd_call, bd_outreach, bd_call_settings. Calls, follow-ups, meetings
and e-mails are also written to crm_activities, so the company timeline, BD Home and Tasks see
them. API: /api/bd/calling/... (admin-only for recruiter accounts, like the rest of BD).
Public: /bdm/o/<token>.gif (open pixel). Script: /bd-calling.js.
"""

import csv
import datetime
import hashlib
import hmac
import io
import ipaddress
import json
import os
import random
import re
import socket
import sys
import threading
import time
from urllib.parse import urlparse

from flask import Blueprint, request, jsonify, Response

from modules import register_migration
from modules.shared import get_db, ts, effective_company_id, real_user_id, login_required, _core

bp = Blueprint('bd_calling', __name__, url_prefix='/api/bd/calling')
pages = Blueprint('bd_calling_pages', __name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IST = datetime.timedelta(hours=5, minutes=30)

STATUSES = [
    ('new', 'New'), ('no_answer', 'No answer'), ('callback', 'Callback'), ('interested', 'Interested'),
    ('send_details', 'Send details'), ('meeting', 'Meeting fixed'), ('not_interested', 'Not interested'),
    ('wrong_number', 'Wrong number'),
]
STATUS_KEYS = [k for k, _ in STATUSES]
NOT_INTERESTED_REASONS = [
    ('no_hiring', 'No hiring right now'), ('own_team', 'Hire on their own / in-house team'),
    ('other_agency', 'Already work with another agency'), ('fee', 'Fee / budget'),
    ('wrong_person', 'Not the right person'), ('other', 'Other'),
]
U_FIELDS = [
    ('what_they_do', 'What they make / do'), ('locations', 'Plants / offices'), ('size', 'Size'),
    ('roles_hired', 'Roles they hire'), ('hiring_pain', 'Hiring pain'), ('decision_maker', 'Who decides'),
    ('current_agency', 'Agency they use today'), ('why_now', 'Why now'), ('notes', 'Other notes'),
]
U_KEYS = [k for k, _ in U_FIELDS]
DEFAULT_SETTINGS = {'calls_target': 50, 'emails_target': 20, 'gap_seconds': 150, 'daily_cap': 60,
                    'fu_days': '3,7', 'fu_on': 1, 'start_hour': 9, 'end_hour': 19}
TICK_SECONDS = 60
PLACED_STAGES = ('placed', 'joined', 'offer accepted')


# ══════════════════════════════════════════════════════════════════════════
#  MIGRATION (additive)
# ══════════════════════════════════════════════════════════════════════════
@register_migration
def _migrate_bd_calling(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS bd_call (
        client_id INTEGER PRIMARY KEY,
        company_id INTEGER NOT NULL,
        contact_id INTEGER DEFAULT 0,
        status TEXT DEFAULT 'new',
        last_outcome TEXT DEFAULT '',
        last_called_at TEXT DEFAULT '',
        last_called_by INTEGER DEFAULT 0,
        attempts INTEGER DEFAULT 0,
        next_at TEXT DEFAULT '',
        next_note TEXT DEFAULT '',
        next_task_id INTEGER DEFAULT 0,
        reason TEXT DEFAULT '',
        understanding TEXT DEFAULT '',
        understanding_at TEXT DEFAULT '',
        research_at TEXT DEFAULT '',
        opener TEXT DEFAULT '',
        email_state TEXT DEFAULT '',
        last_email_at TEXT DEFAULT '',
        opens INTEGER DEFAULT 0,
        last_open_at TEXT DEFAULT '',
        replied_at TEXT DEFAULT '',
        seq_active INTEGER DEFAULT 0,
        seq_step INTEGER DEFAULT 0,
        seq_next_at TEXT DEFAULT '',
        seq_root_id INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT ''
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS bd_outreach (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        client_id INTEGER NOT NULL,
        contact_id INTEGER DEFAULT 0,
        user_id INTEGER DEFAULT 0,
        step INTEGER DEFAULT 0,
        root_id INTEGER DEFAULT 0,
        style TEXT DEFAULT '',
        to_addr TEXT DEFAULT '',
        subject TEXT DEFAULT '',
        body TEXT DEFAULT '',
        status TEXT DEFAULT 'draft',
        followups INTEGER DEFAULT 1,
        message_id TEXT DEFAULT '',
        scheduled_at TEXT DEFAULT '',
        sent_at TEXT DEFAULT '',
        sent_ts REAL DEFAULT 0,
        opened_at TEXT DEFAULT '',
        opens INTEGER DEFAULT 0,
        replied_at TEXT DEFAULT '',
        error TEXT DEFAULT '',
        batch TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS bd_playbook (
        company_id INTEGER PRIMARY KEY,
        text TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        updated_by INTEGER DEFAULT 0
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS bd_call_settings (
        user_id INTEGER PRIMARY KEY,
        company_id INTEGER NOT NULL,
        settings TEXT DEFAULT ''
    )''')
    for sql in ('CREATE INDEX IF NOT EXISTS idx_bdcall_co ON bd_call(company_id, status)',
                'CREATE INDEX IF NOT EXISTS idx_bdout_client ON bd_outreach(client_id, status)',
                'CREATE INDEX IF NOT EXISTS idx_bdout_queue ON bd_outreach(status, scheduled_at)',
                'CREATE INDEX IF NOT EXISTS idx_bdout_user ON bd_outreach(user_id, status, sent_at)'):
        try:
            conn.execute(sql)
        except Exception:
            pass
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  SMALL HELPERS
# ══════════════════════════════════════════════════════════════════════════
class BDError(Exception):
    def __init__(self, msg, code=400):
        Exception.__init__(self, msg)
        self.msg, self.code = msg, code


def _err(msg, code=400):
    return jsonify({'ok': False, 'error': msg}), code


def now_ist():
    return datetime.datetime.utcnow() + IST


def _iso(d):
    return d.strftime('%Y-%m-%dT%H:%M:%S')


def _parse(s):
    s = (s or '').strip().replace(' ', 'T')
    for fmt, n in (('%Y-%m-%dT%H:%M:%S', 19), ('%Y-%m-%dT%H:%M', 16), ('%Y-%m-%d', 10)):
        try:
            return datetime.datetime.strptime(s[:n], fmt)
        except ValueError:
            continue
    return None


def _norm_dt(s):
    d = _parse(s)
    return _iso(d) if d else ''


def _next_morning(d=None, hour=11):
    d = (d or now_ist()) + datetime.timedelta(days=1)
    while d.weekday() == 6:
        d += datetime.timedelta(days=1)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0)


def _guard():
    """BD rules: freelancers never; corporate workspaces have no BD."""
    try:
        from modules.freelancer import _is_freelancer
        if _is_freelancer():
            raise BDError('Freelancers cannot access Business Development', 403)
    except BDError:
        raise
    except Exception:
        pass
    try:
        if _core().workspace_mode() == 'corporate':
            raise BDError('Not found', 404)
    except BDError:
        raise
    except Exception:
        pass


def _api(fn):
    """login + BD guard + BDError -> JSON error."""
    from functools import wraps

    @wraps(fn)
    def inner(*a, **kw):
        try:
            _guard()
            return fn(*a, **kw)
        except BDError as e:
            return _err(e.msg, e.code)
        except Exception as e:
            if type(e).__name__ == 'TokenCapError':
                return _err('Your AI usage cap for this period is reached. Ask the admin to raise it.', 429)
            if type(e).__name__ in ('ValidationError', 'DuplicateError'):
                code = getattr(e, 'code', 400)
                return _err(getattr(e, 'message', str(e)), code if isinstance(code, int) else 400)
            raise
    return login_required(inner)


def _ws():
    try:
        wid = max(0, int(request.args.get('ws') or 0))
    except ValueError:
        raise BDError('Invalid workspace.')
    if wid:
        conn = get_db()
        try:
            ok = conn.execute('SELECT id FROM bd_workspaces WHERE id=? AND company_id=? AND is_active=1',
                              (wid, effective_company_id())).fetchone()
        finally:
            conn.close()
        if not ok:
            raise BDError('Workspace not found.', 404)
    return wid


def _company(conn, oid, cid):
    r = conn.execute("SELECT * FROM crm_clients WHERE id=? AND company_id=? AND is_active=1 "
                     "AND COALESCE(is_internal,0)=0", (cid, oid)).fetchone()
    if not r:
        raise BDError('Company not found.', 404)
    return dict(r)


def _call_row(conn, oid, cid):
    conn.execute('INSERT OR IGNORE INTO bd_call (client_id, company_id, updated_at) VALUES (?,?,?)', (cid, oid, ts()))
    return dict(conn.execute('SELECT * FROM bd_call WHERE client_id=?', (cid,)).fetchone())


def _contacts(conn, oid, cid):
    return [dict(r) for r in conn.execute(
        'SELECT id, name, designation, email, phone, linkedin, is_primary, is_decision_maker FROM crm_contacts '
        'WHERE client_id=? AND company_id=? AND is_active=1 ORDER BY is_primary DESC, is_decision_maker DESC, id',
        (cid, oid)).fetchall()]


def _pick_contact(contacts, want=0):
    for c in contacts:
        if c['id'] == want:
            return c
    return contacts[0] if contacts else None


def _understanding(row):
    try:
        u = json.loads(row.get('understanding') or '{}')
    except Exception:
        u = {}
    return {k: str(u.get(k) or '') for k in U_KEYS}


def _u_score(u):
    return sum(1 for k in U_KEYS if k != 'notes' and (u.get(k) or '').strip())


def _settings(conn, uid, oid):
    r = conn.execute('SELECT settings FROM bd_call_settings WHERE user_id=?', (uid,)).fetchone()
    s = dict(DEFAULT_SETTINGS)
    if r:
        try:
            s.update({k: v for k, v in json.loads(r['settings'] or '{}').items() if k in DEFAULT_SETTINGS})
        except Exception:
            pass
    return s


def _fu_days(s):
    out = []
    for x in str(s.get('fu_days') or '').split(','):
        try:
            n = int(x.strip())
        except ValueError:
            continue
        if 1 <= n <= 60 and n not in out:
            out.append(n)
    return sorted(out)[:3]


def _users(conn, oid):
    return {r['id']: (r['display_name'] or r['username'] or '') for r in conn.execute(
        'SELECT id, display_name, username FROM users WHERE company_id=?', (oid,)).fetchall()}


def _ai_key():
    try:
        return _core().get_setting('deepseek_api_key', '') or ''
    except Exception:
        return ''


def _ai_json(system, user, max_tokens=900, temperature=0.4, endpoint='bd-calling'):
    key = _ai_key()
    if not key:
        raise BDError('AI is not set up. Add the DeepSeek API key in Settings.', 400)
    core = _core()
    resp = core.call_deepseek(key, {'model': 'deepseek-chat', 'temperature': temperature, 'max_tokens': max_tokens,
                                    'response_format': {'type': 'json_object'},
                                    'messages': [{'role': 'system', 'content': system},
                                                 {'role': 'user', 'content': user[:14000]}]},
                              timeout=90, endpoint=endpoint)
    if resp.status_code != 200:
        raise BDError('The AI could not answer right now (HTTP %s). Try again.' % resp.status_code, 502)
    raw = resp.json()['choices'][0]['message']['content']
    try:
        j = json.loads(raw)
    except Exception:
        j = core.parse_json(raw)
    if not isinstance(j, dict):
        raise BDError('The AI answer could not be read. Try again.', 502)
    return j


def _sign(raw):
    key = _core().app.secret_key
    key = key if isinstance(key, bytes) else str(key or 'hirelab').encode()
    return hmac.new(key, ('bdm:' + raw).encode(), hashlib.sha256).hexdigest()[:16]


def token_for(oid):
    return '%d-%s' % (oid, _sign(str(oid)))


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


def _log_activity(conn, oid, cid, contact_id, atype, subject, body='', outcome='', due='', meta=None, uid=None,
                  status=None):
    uid = real_user_id() if uid is None else uid
    now = ts()
    st = status or ('open' if (atype in ('followup', 'task') or due) else 'done')
    cur = conn.execute(
        'INSERT INTO crm_activities (company_id,client_id,contact_id,activity_type,subject,body,outcome,due_at,'
        'completed_at,status,owner_user_id,meta,is_active,created_by,updated_by,created_at,updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)',
        (oid, cid, contact_id or 0, atype, subject[:300], body, outcome[:300], due, '' if st == 'open' else now, st,
         uid, json.dumps(meta or {}), uid, uid, now, now))
    return cur.lastrowid


def _close_task(conn, tid):
    if tid:
        conn.execute("UPDATE crm_activities SET status='done', completed_at=?, updated_at=? WHERE id=? AND status='open'",
                     (ts(), ts(), tid))


def _notify(conn, oid, uid, title, body, kind='bd_reply'):
    try:
        conn.execute('INSERT INTO notifications (company_id,user_id,kind,title,body,candidate_id,mandate_id,'
                     "actor_id,actor_name,created_at,updated_at,read_at,acked_at) VALUES (?,?,?,?,?,0,0,0,'',?,?,'','')",
                     (oid, uid, kind, title[:200], body[:500], ts(), ts()))
    except Exception as e:
        print('[bd-calling] notify failed: %s' % e)


# ══════════════════════════════════════════════════════════════════════════
#  THE SHEET
# ══════════════════════════════════════════════════════════════════════════
def _email_info(conn, cid):
    rows = conn.execute("SELECT id, step, status, sent_at, opens, opened_at, replied_at, scheduled_at FROM bd_outreach "
                        "WHERE client_id=? AND status IN ('sent','queued','sending','draft','drafting') ORDER BY id",
                        (cid,)).fetchall()
    sent = [r for r in rows if r['status'] == 'sent']
    return {'sent': len(sent), 'queued': sum(1 for r in rows if r['status'] in ('queued', 'sending')),
            'drafts': sum(1 for r in rows if r['status'] in ('draft', 'drafting')),
            'opens': sum(r['opens'] or 0 for r in sent), 'last_sent_at': sent[-1]['sent_at'] if sent else ''}


def _bucket(r, today_end):
    """Where a row sits in Today: 1 due, 2 replied, 3 opened, 4 new, 9 not today."""
    st = r['status'] or 'new'
    if st in ('not_interested', 'wrong_number'):
        if r['next_at'] and r['next_at'] <= today_end:
            return 1, 'Try again (said no on %s)' % (r['last_called_at'] or '')[:10]
        return 9, ''
    if r['next_at'] and r['next_at'] <= today_end:
        return 1, ('Overdue · ' if r['next_at'] < _iso(now_ist()) else 'Due · ') + r['next_at'][11:16]
    if r['replied_at'] and (not r['last_called_at'] or r['last_called_at'] < r['replied_at']):
        return 2, 'Replied to your e-mail'
    if r['opens'] and (not r['last_called_at'] or r['last_called_at'] < (r['last_open_at'] or '')):
        return 3, 'Opened your e-mail %d×' % r['opens']
    if st == 'new' and not r['last_called_at']:
        return 4, 'Not called yet'
    return 9, ''


def _sheet_rows(conn, oid, ws, view='today', q='', mine=False, uid=0, ids=None):
    sql = ("SELECT c.id, c.name, c.website, c.industry, c.city, c.owner_user_id, c.created_at, "
           "b.contact_id, b.status, b.last_outcome, b.last_called_at, b.last_called_by, b.attempts, b.next_at, "
           "b.next_note, b.reason, b.understanding, b.email_state, b.opens, b.last_open_at, b.replied_at, "
           "b.seq_active, b.seq_next_at "
           "FROM crm_clients c LEFT JOIN bd_call b ON b.client_id=c.id "
           "WHERE c.company_id=? AND c.is_active=1 AND COALESCE(c.is_internal,0)=0 "
           "AND COALESCE(c.record_type,'client')='prospect' AND COALESCE(c.bd_workspace_id,0)=?")
    args = [oid, ws]
    if ids:
        sql += ' AND c.id IN (%s)' % ','.join('?' * len(ids))
        args += list(ids)
    if mine:
        sql += ' AND (c.owner_user_id=? OR COALESCE(c.owner_user_id,0)=0)'
        args.append(uid)
    if q:
        like = '%' + q.replace('%', '') + '%'
        sql += (' AND (c.name LIKE ? OR c.industry LIKE ? OR c.city LIKE ? OR EXISTS (SELECT 1 FROM crm_contacts k '
                'WHERE k.client_id=c.id AND k.is_active=1 AND (k.name LIKE ? OR k.email LIKE ? OR k.phone LIKE ?)))')
        args += [like] * 6
    rows = [dict(r) for r in conn.execute(sql + ' ORDER BY c.id DESC LIMIT 2000', args).fetchall()]
    today_end = _iso(now_ist().replace(hour=23, minute=59, second=59))
    out = []
    for r in rows:
        for k in ('status', 'last_outcome', 'last_called_at', 'next_at', 'next_note', 'reason', 'email_state',
                  'last_open_at', 'replied_at', 'seq_next_at'):
            r[k] = r.get(k) or ''
        r['status'] = r['status'] or 'new'
        r['opens'] = r.get('opens') or 0
        r['attempts'] = r.get('attempts') or 0
        b, why = _bucket(r, today_end)
        r['bucket'], r['why'] = b, why
        out.append(r)
    if view == 'today':
        out = [r for r in out if r['bucket'] < 9]
        out.sort(key=lambda r: (r['bucket'], r['next_at'] or '9', -(r['opens'] or 0), -r['id']))
    elif view == 'callbacks':
        out = [r for r in out if r['next_at'] and r['status'] not in ('not_interested', 'wrong_number')]
        out.sort(key=lambda r: r['next_at'])
    elif view == 'hot':
        out = [r for r in out if r['opens'] or r['replied_at']]
        out.sort(key=lambda r: (not r['replied_at'], -(r['opens'] or 0)))
    elif view in STATUS_KEYS:
        out = [r for r in out if r['status'] == view]
    elif view == 'new':
        out = [r for r in out if not r['last_called_at']]
    return out


def _decorate(conn, oid, rows):
    """Contact + e-mail summary for the rows actually shown."""
    for r in rows:
        cs = _contacts(conn, oid, r['id'])
        c = _pick_contact(cs, r.get('contact_id') or 0)
        r['contact'] = c or {}
        r['contacts'] = len(cs)
        try:
            u = json.loads(r.pop('understanding', '') or '{}')
        except Exception:
            u = {}
        r['u_score'] = _u_score(u)
        r['email'] = _email_info(conn, r['id'])
    return rows


def _today_stats(conn, oid, uid):
    day = now_ist().strftime('%Y-%m-%d')
    calls = conn.execute("SELECT COUNT(*) FROM crm_activities WHERE company_id=? AND created_by=? AND activity_type='call' "
                         "AND created_at LIKE ? AND meta LIKE '%bd_call%'", (oid, uid, day + '%')).fetchone()[0]
    emails = conn.execute("SELECT COUNT(*) FROM bd_outreach WHERE company_id=? AND user_id=? AND status='sent' "
                          "AND sent_at LIKE ?", (oid, uid, day + '%')).fetchone()[0]
    return {'calls': calls, 'emails': emails}


@bp.route('/meta', methods=['GET'])
@_api
def meta():
    conn = get_db()
    try:
        oid, uid = effective_company_id(), real_user_id()
        s = _settings(conn, uid, oid)
        mail = {}
        try:
            mail = _core().mail_identity_status(uid)
        except Exception:
            pass
        return jsonify({'ok': True, 'statuses': STATUSES, 'reasons': NOT_INTERESTED_REASONS, 'u_fields': U_FIELDS,
                        'phrases': [{'phrase': p, 'tip': t} for p, t in AGENCY_SPEAK],
                        'settings': s, 'today': _today_stats(conn, oid, uid), 'ai': bool(_ai_key()),
                        'mail': mail, 'me': uid})
    finally:
        conn.close()


@bp.route('/list', methods=['GET'])
@_api
def sheet():
    ws = _ws()
    view = request.args.get('view') or 'today'
    q = (request.args.get('q') or '').strip()
    mine = request.args.get('mine') in ('1', 'true')
    conn = get_db()
    try:
        oid, uid = effective_company_id(), real_user_id()
        rows = _sheet_rows(conn, oid, ws, view, q, mine, uid)
        counts = {}
        allrows = _sheet_rows(conn, oid, ws, 'all', '', mine, uid)
        counts['all'] = len(allrows)
        counts['today'] = sum(1 for r in allrows if r['bucket'] < 9)
        counts['callbacks'] = sum(1 for r in allrows if r['next_at'] and r['status'] not in ('not_interested', 'wrong_number'))
        counts['hot'] = sum(1 for r in allrows if r['opens'] or r['replied_at'])
        counts['new'] = sum(1 for r in allrows if not r['last_called_at'])
        for k in STATUS_KEYS:
            counts[k] = sum(1 for r in allrows if r['status'] == k)
        total = len(rows)
        rows = _decorate(conn, oid, rows[:300])
        users = _users(conn, oid)
        for r in rows:
            r['owner_name'] = users.get(r.get('owner_user_id') or 0, '')
            r['caller_name'] = users.get(r.get('last_called_by') or 0, '')
        return jsonify({'ok': True, 'rows': rows, 'total': total, 'counts': counts,
                        'today': _today_stats(conn, oid, uid), 'settings': _settings(conn, uid, oid)})
    finally:
        conn.close()


_CO_EDIT = {'name', 'website', 'industry', 'city'}
_CT_EDIT = {'contact_name': 'name', 'designation': 'designation', 'phone': 'phone', 'email': 'email',
            'linkedin': 'linkedin'}


@bp.route('/companies/<int:cid>', methods=['PATCH'])
@_api
def edit_cell(cid):
    """Inline edit of one cell: company fields, or the calling contact's fields (creates the contact if none)."""
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        oid = effective_company_id()
        co = _company(conn, oid, cid)
        row = _call_row(conn, oid, cid)
        from modules import crm
        for k, v in d.items():
            v = str(v if v is not None else '').strip()
            if k in _CO_EDIT:
                if k == 'name':
                    if not v:
                        raise BDError('Company name cannot be empty.')
                    key = crm._norm_name(v)
                    dup = conn.execute('SELECT id, name FROM crm_clients WHERE company_id=? AND name_key=? AND id!=? AND is_active=1',
                                       (oid, key, cid)).fetchone()
                    if dup:
                        raise BDError('"%s" already exists.' % dup['name'])
                    conn.execute('UPDATE crm_clients SET name=?, name_key=?, updated_at=?, updated_by=? WHERE id=?',
                                 (v, key, ts(), real_user_id(), cid))
                else:
                    conn.execute('UPDATE crm_clients SET %s=?, updated_at=?, updated_by=? WHERE id=?' % k,
                                 (v, ts(), real_user_id(), cid))
            elif k == 'contact_id':
                cs = _contacts(conn, oid, cid)
                if int(v or 0) not in [c['id'] for c in cs]:
                    raise BDError('Contact not found.')
                conn.execute('UPDATE bd_call SET contact_id=?, updated_at=? WHERE client_id=?', (int(v), ts(), cid))
            elif k in _CT_EDIT:
                col = _CT_EDIT[k]
                if col == 'email' and v and not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', v):
                    raise BDError('That e-mail address does not look right.')
                cs = _contacts(conn, oid, cid)
                c = _pick_contact(cs, row.get('contact_id') or 0)
                if not c:
                    if not v:
                        continue
                    payload = {'name': v if col == 'name' else 'Contact', col: v, 'is_primary': True}
                    if col == 'name':
                        payload = {'name': v, 'is_primary': True}
                    new_id = crm.ContactService.create(conn, cid, payload)
                    conn.execute('UPDATE bd_call SET contact_id=? WHERE client_id=?', (new_id, cid))
                    continue
                if col == 'email' and v:
                    ek = crm._norm_email(v)
                    dup = conn.execute('SELECT id FROM crm_contacts WHERE company_id=? AND email_key=? AND id!=? AND is_active=1',
                                       (oid, ek, c['id'])).fetchone()
                    if dup:
                        raise BDError('Another contact already has %s.' % v)
                    conn.execute('UPDATE crm_contacts SET email=?, email_key=?, updated_at=? WHERE id=?', (v, ek, ts(), c['id']))
                elif col == 'name' and not v:
                    raise BDError('Contact name cannot be empty.')
                else:
                    conn.execute('UPDATE crm_contacts SET %s=?, updated_at=? WHERE id=?' % col, (v, ts(), c['id']))
        conn.commit()
        r = _decorate(conn, oid, _sheet_rows(conn, oid, co.get('bd_workspace_id') or 0, 'all', ids=[cid]))
        return jsonify({'ok': True, 'row': r[0] if r else None})
    finally:
        conn.close()


@bp.route('/companies/<int:cid>/contacts', methods=['POST'])
@_api
def add_contact(cid):
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        oid = effective_company_id()
        _company(conn, oid, cid)
        from modules import crm
        new_id = crm.ContactService.create(conn, cid, {k: d.get(k) for k in ('name', 'designation', 'email', 'phone', 'linkedin')})
        _call_row(conn, oid, cid)
        if d.get('make_caller', True):
            conn.execute('UPDATE bd_call SET contact_id=? WHERE client_id=?', (new_id, cid))
        conn.commit()
        return jsonify({'ok': True, 'id': new_id, 'contacts': _contacts(conn, oid, cid)})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  OUTCOMES -> NEXT STEP
# ══════════════════════════════════════════════════════════════════════════
def _stop_sequence(conn, cid, why=''):
    conn.execute("UPDATE bd_call SET seq_active=0, seq_next_at='' WHERE client_id=?", (cid,))
    conn.execute("UPDATE bd_outreach SET status='cancelled', error=? WHERE client_id=? AND step>0 AND status IN ('queued','draft')",
                 (why or 'Sequence stopped', cid))


@bp.route('/companies/<int:cid>/outcome', methods=['POST'])
@_api
def outcome(cid):
    d = request.get_json(silent=True) or {}
    st = (d.get('status') or '').strip()
    if st not in STATUS_KEYS or st == 'new':
        raise BDError('Pick an outcome.')
    note = (d.get('note') or '').strip()[:2000]
    conn = get_db()
    try:
        oid, uid = effective_company_id(), real_user_id()
        co = _company(conn, oid, cid)
        row = _call_row(conn, oid, cid)
        contacts = _contacts(conn, oid, cid)
        contact = _pick_contact(contacts, int(d.get('contact_id') or row.get('contact_id') or 0))
        ctid = contact['id'] if contact else 0
        now = now_ist()
        label = dict(STATUSES)[st]
        next_at, next_note, reason, task_id = '', '', '', 0
        compose = False
        _close_task(conn, row.get('next_task_id'))
        if st == 'no_answer':
            attempts = (row.get('attempts') or 0) + 1
            nxt = now + datetime.timedelta(hours=2) if attempts % 3 else _next_morning(now)
            if nxt.hour >= 19:
                nxt = _next_morning(now)
            next_at = _norm_dt(d.get('next_at')) or _iso(nxt)
            next_note = 'Try again (attempt %d)' % (attempts + 1)
        elif st in ('callback', 'interested', 'meeting'):
            next_at = _norm_dt(d.get('next_at'))
            if st == 'callback' and not next_at:
                next_at = _iso(_next_morning(now))
            if st == 'meeting' and not next_at:
                raise BDError('Pick the meeting date and time.')
            if next_at:
                kind = 'meeting' if st == 'meeting' else 'followup'
                title = ('Meeting with ' if st == 'meeting' else 'Call back ') + (contact['name'] if contact else co['name'])
                next_note = (d.get('next_note') or '').strip() or title
                task_id = _log_activity(conn, oid, cid, ctid, kind, next_note + ' · ' + co['name'], note, '', next_at,
                                        {'bd_call': True}, uid, status='open')
        elif st == 'send_details':
            compose = True
            next_at = _iso(now + datetime.timedelta(days=2)) if not d.get('next_at') else _norm_dt(d.get('next_at'))
            next_note = 'Follow up on the details e-mail'
        elif st == 'not_interested':
            reason = (d.get('reason') or '').strip()
            if reason not in dict(NOT_INTERESTED_REASONS):
                raise BDError('Pick a reason.')
            try:
                days = max(7, min(365, int(d.get('retry_days') or 90)))
            except ValueError:
                days = 90
            next_at = _iso((now + datetime.timedelta(days=days)).replace(hour=11, minute=0, second=0, microsecond=0))
            next_note = 'Try again (%s)' % dict(NOT_INTERESTED_REASONS)[reason]
        if st in ('interested', 'meeting', 'not_interested', 'wrong_number'):
            _stop_sequence(conn, cid, 'Call outcome: ' + label)
        attempts_sql = 'attempts=attempts+1' if st == 'no_answer' else 'attempts=0'
        conn.execute('UPDATE bd_call SET status=?, last_outcome=?, last_called_at=?, last_called_by=?, %s, next_at=?, '
                     'next_note=?, next_task_id=?, reason=?, contact_id=?, updated_at=? WHERE client_id=?' % attempts_sql,
                     (st, (label + (': ' + note if note else ''))[:300], _iso(now), uid, next_at, next_note, task_id,
                      reason, ctid, ts(), cid))
        _log_activity(conn, oid, cid, ctid, 'call', 'Call: ' + label + (' — ' + contact['name'] if contact else ''),
                      note, label + (' (%s)' % dict(NOT_INTERESTED_REASONS)[reason] if reason else ''), '',
                      {'bd_call': True, 'status': st, 'next_at': next_at}, uid)
        if st == 'meeting':
            conn.execute("UPDATE crm_clients SET status='prospect' WHERE id=?", (cid,))
        conn.commit()
        r = _decorate(conn, oid, _sheet_rows(conn, oid, co.get('bd_workspace_id') or 0, 'all', ids=[cid]))
        return jsonify({'ok': True, 'row': r[0] if r else None, 'compose': compose, 'today': _today_stats(conn, oid, uid)})
    finally:
        conn.close()


@bp.route('/companies/<int:cid>/next', methods=['POST'])
@_api
def set_next(cid):
    """Move / clear the next step without logging a call."""
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        oid = effective_company_id()
        co = _company(conn, oid, cid)
        row = _call_row(conn, oid, cid)
        nxt = _norm_dt(d.get('next_at'))
        if row.get('next_task_id'):
            if nxt:
                conn.execute('UPDATE crm_activities SET due_at=?, updated_at=? WHERE id=?', (nxt, ts(), row['next_task_id']))
            else:
                _close_task(conn, row['next_task_id'])
        conn.execute('UPDATE bd_call SET next_at=?, next_note=?, next_task_id=? WHERE client_id=?',
                     (nxt, (d.get('next_note') or row.get('next_note') or '')[:200] if nxt else '',
                      row.get('next_task_id') if nxt else 0, cid))
        conn.commit()
        r = _decorate(conn, oid, _sheet_rows(conn, oid, co.get('bd_workspace_id') or 0, 'all', ids=[cid]))
        return jsonify({'ok': True, 'row': r[0] if r else None})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  DETAIL PANEL + UNDERSTANDING
# ══════════════════════════════════════════════════════════════════════════
def _outreach_out(r, users):
    d = dict(r)
    d['user_name'] = users.get(d.get('user_id') or 0, '')
    return d


@bp.route('/companies/<int:cid>', methods=['GET'])
@_api
def detail(cid):
    conn = get_db()
    try:
        oid = effective_company_id()
        co = _company(conn, oid, cid)
        row = _call_row(conn, oid, cid)
        conn.commit()
        users = _users(conn, oid)
        calls = [dict(r) for r in conn.execute(
            "SELECT id, activity_type, subject, body, outcome, due_at, status, created_at, created_by FROM crm_activities "
            "WHERE client_id=? AND company_id=? AND is_active=1 AND activity_type IN ('call','followup','meeting','note') "
            "ORDER BY id DESC LIMIT 30", (cid, oid)).fetchall()]
        for c in calls:
            c['by'] = users.get(c['created_by'] or 0, '')
        emails = [_outreach_out(r, users) for r in conn.execute(
            "SELECT * FROM bd_outreach WHERE client_id=? AND company_id=? AND status!='cancelled' ORDER BY id DESC LIMIT 30",
            (cid, oid)).fetchall()]
        try:
            opener = json.loads(row.get('opener') or '{}')
        except Exception:
            opener = {}
        return jsonify({'ok': True, 'company': {k: co.get(k) for k in ('id', 'name', 'website', 'industry', 'city', 'state',
                                                                        'linkedin', 'employees', 'domain', 'notes')},
                        'call': {k: row.get(k) for k in ('status', 'last_outcome', 'last_called_at', 'attempts', 'next_at',
                                                         'next_note', 'reason', 'contact_id', 'email_state', 'opens',
                                                         'replied_at', 'seq_active', 'seq_step', 'seq_next_at',
                                                         'understanding_at', 'research_at')},
                        'understanding': _understanding(row), 'u_score': _u_score(_understanding(row)),
                        'opener': opener, 'contacts': _contacts(conn, oid, cid), 'history': calls, 'emails': emails})
    finally:
        conn.close()


def _save_u(conn, cid, u):
    conn.execute('UPDATE bd_call SET understanding=?, understanding_at=?, updated_at=? WHERE client_id=?',
                 (json.dumps({k: (u.get(k) or '')[:2000] for k in U_KEYS}), ts(), ts(), cid))


@bp.route('/companies/<int:cid>/understanding', methods=['PUT', 'POST'])
@_api
def save_understanding(cid):
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        oid = effective_company_id()
        _company(conn, oid, cid)
        row = _call_row(conn, oid, cid)
        u = _understanding(row)
        for k in U_KEYS:
            if k in d:
                u[k] = str(d.get(k) or '').strip()
        _save_u(conn, cid, u)
        conn.commit()
        return jsonify({'ok': True, 'understanding': u, 'u_score': _u_score(u)})
    finally:
        conn.close()


# ── AI research: the company website + the agency's Learning notes ─────────
def _public_host(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


def _html_text(html):
    html = re.sub(r'(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>', ' ', html or '')
    html = re.sub(r'(?is)<br\s*/?>|</p>|</li>|</h\d>|</div>', '\n', html)
    txt = re.sub(r'(?s)<[^>]+>', ' ', html)
    txt = re.sub(r'&nbsp;', ' ', txt)
    txt = re.sub(r'&amp;', '&', txt)
    txt = re.sub(r'[ \t\r\f\v]+', ' ', txt)
    txt = re.sub(r'\n\s*\n+', '\n', txt)
    return txt.strip()


def fetch_site(url, max_pages=3):
    """Text of a public website (home + about pages). Private / internal addresses are refused."""
    import requests
    url = (url or '').strip()
    if not url:
        return ''
    if not re.match(r'^https?://', url, re.I):
        url = 'https://' + url
    p = urlparse(url)
    if p.scheme not in ('http', 'https') or not p.hostname or not _public_host(p.hostname):
        return ''
    base = '%s://%s' % (p.scheme, p.netloc)
    out, seen = [], set()
    for path in [p.path or '/', '/about', '/about-us', '/company', '/careers'][:max_pages + 2]:
        u = base + (path if path.startswith('/') else '/' + path)
        if u in seen:
            continue
        seen.add(u)
        try:
            r = requests.get(u, timeout=8, allow_redirects=True, stream=True,
                             headers={'User-Agent': 'Mozilla/5.0 (compatible; HireLabResearch/1.0)'})
            final = urlparse(r.url)
            if final.hostname and not _public_host(final.hostname):
                continue
            if r.status_code != 200 or 'html' not in (r.headers.get('Content-Type') or 'text/html'):
                continue
            raw = r.raw.read(400000, decode_content=True)
            txt = _html_text(raw.decode(r.encoding or 'utf-8', 'ignore'))
            if txt:
                out.append('[%s]\n%s' % (path, txt[:5000]))
        except Exception:
            continue
        if len(out) >= max_pages:
            break
    return '\n\n'.join(out)[:12000]


def learning_notes(conn, oid, name):
    """What the agency's Learning page (Electrical Industry Explorer) says about this company."""
    name = (name or '').strip()
    if len(name) < 3:
        return ''
    key = re.escape(name.split()[0] if len(name.split()[0]) >= 4 else name)
    texts = []
    try:
        r = conn.execute('SELECT state FROM learning_state WHERE company_id=?', (oid,)).fetchone()
        if r and r['state']:
            texts.append(r['state'])
    except Exception:
        pass
    try:
        with open(os.path.join(ROOT, 'learning.html'), encoding='utf-8') as f:
            texts.append(f.read())
    except Exception:
        pass
    out = []
    for t in texts:
        for m in re.finditer(key, t, re.I):
            seg = t[max(0, m.start() - 300): m.end() + 500]
            seg = re.sub(r'[{}\[\]"\\]+', ' ', _html_text(seg))
            seg = re.sub(r'\s+', ' ', seg).strip()
            if seg and seg not in out:
                out.append(seg)
            if len(out) >= 4:
                break
        if len(out) >= 4:
            break
    return '\n- '.join([''] + out).strip() if out else ''


RESEARCH_PROMPT = """You help an Indian recruitment agency prepare a cold call to a company.
From the website text and notes, fill what you can of this JSON (short, plain, factual; empty string when unknown,
never guess numbers or names that are not in the text):
{"what_they_do": "what they make / sell, 1-2 lines", "locations": "plants, offices, cities",
 "size": "employees / turnover / scale if stated", "roles_hired": "roles they likely hire (from careers page or products)",
 "hiring_pain": "", "decision_maker": "", "current_agency": "",
 "why_now": "expansion, new plant, new product, funding, careers openings - only if in the text",
 "industry": "one or two words", "city": "head office city if clear"}"""


@bp.route('/companies/<int:cid>/research', methods=['POST'])
@_api
def research(cid):
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        oid = effective_company_id()
        co = _company(conn, oid, cid)
        row = _call_row(conn, oid, cid)
        site = (d.get('website') or co.get('website') or co.get('domain') or '').strip()
        if site and not co.get('website'):
            conn.execute('UPDATE crm_clients SET website=? WHERE id=?', (site, cid))
        notes = learning_notes(conn, oid, co['name'])
        conn.commit()
    finally:
        conn.close()
    text = fetch_site(site) if site else ''
    if not text and not notes:
        raise BDError('Nothing to read: add the company website first (the site could not be opened).' if site
                      else 'Add the company website first, then AI can read it.')
    j = _ai_json(RESEARCH_PROMPT, 'COMPANY: %s\nWEBSITE: %s\n\nWEBSITE TEXT:\n%s\n\nAGENCY NOTES (Learning page):\n%s'
                 % (co['name'], site, text or '(could not open)', notes or '(none)'), 800, 0.2, 'bd-research')
    conn = get_db()
    try:
        row = _call_row(conn, oid, cid)
        u = _understanding(row)
        filled = []
        for k in U_KEYS:
            v = str(j.get(k) or '').strip()
            if v and not u.get(k):
                u[k] = v[:1500]
                filled.append(k)
        _save_u(conn, cid, u)
        conn.execute('UPDATE bd_call SET research_at=? WHERE client_id=?', (ts(), cid))
        for col in ('industry', 'city'):
            v = str(j.get(col) or '').strip()
            if v and not co.get(col):
                conn.execute('UPDATE crm_clients SET %s=? WHERE id=?' % col, (v[:80], cid))
        conn.commit()
        return jsonify({'ok': True, 'understanding': u, 'filled': filled, 'u_score': _u_score(u),
                        'read_site': bool(text), 'used_learning': bool(notes)})
    finally:
        conn.close()


PARSE_PROMPT = """A recruiter wrote or said (often Hinglish) what they learned on a call with a company.
Put it into this JSON. Only what was said; empty string when not mentioned. Write in clear English.
{"what_they_do": "", "locations": "", "size": "", "roles_hired": "", "hiring_pain": "", "decision_maker": "",
 "current_agency": "", "why_now": "", "notes": "anything else useful, e.g. next step promised",
 "contact_name": "", "contact_designation": "", "contact_phone": "", "contact_email": ""}"""


@bp.route('/companies/<int:cid>/understanding/parse', methods=['POST'])
@_api
def parse_notes(cid):
    d = request.get_json(silent=True) or {}
    text = (d.get('text') or '').strip()
    if len(text) < 5:
        raise BDError('Say or type a bit more first.')
    conn = get_db()
    try:
        oid = effective_company_id()
        co = _company(conn, oid, cid)
        row = _call_row(conn, oid, cid)
        conn.commit()
    finally:
        conn.close()
    j = _ai_json(PARSE_PROMPT, 'COMPANY: %s\n\nWHAT THE RECRUITER SAID:\n%s' % (co['name'], text[:6000]), 700, 0.1, 'bd-parse')
    conn = get_db()
    try:
        row = _call_row(conn, oid, cid)
        u = _understanding(row)
        changed = []
        for k in U_KEYS:
            v = str(j.get(k) or '').strip()
            if not v:
                continue
            if u.get(k) and v.lower() not in u[k].lower():
                u[k] = (u[k] + '; ' + v)[:2000]
            elif not u.get(k):
                u[k] = v[:2000]
            else:
                continue
            changed.append(k)
        _save_u(conn, cid, u)
        conn.commit()
        contact = {k[8:]: str(j.get(k) or '').strip() for k in ('contact_name', 'contact_designation', 'contact_phone',
                                                               'contact_email') if str(j.get(k) or '').strip()}
        return jsonify({'ok': True, 'understanding': u, 'changed': changed, 'u_score': _u_score(u),
                        'contact_hint': contact})
    finally:
        conn.close()


# ── facts the AI may use: real placements from the ATS ────────────────────
def proof_facts(conn, oid, u, co):
    """Counted from the ATS — the e-mail may only use these numbers."""
    rows = conn.execute(
        "SELECT m.role, m.client, COALESCE(k.industry,'') AS industry FROM candidates c JOIN mandates m ON m.id=c.mandate_id "
        "LEFT JOIN crm_clients k ON k.id=m.crm_client_id "
        "WHERE c.owner_id=? AND LOWER(TRIM(COALESCE(c.stage,''))) IN ('placed','joined','offer accepted')",
        (oid,)).fetchall()
    total = len(rows)
    jobs = conn.execute("SELECT COUNT(*) FROM mandates WHERE owner_id=? AND COALESCE(status,'')!='central'", (oid,)).fetchone()[0]
    clients = conn.execute("SELECT COUNT(DISTINCT client) FROM mandates WHERE owner_id=? AND COALESCE(client,'')!='' "
                           "AND COALESCE(status,'')!='central'", (oid,)).fetchone()[0]
    words = set(w for w in re.findall(r'[a-z]{4,}', ((u.get('roles_hired') or '') + ' ' + (co.get('industry') or '') + ' '
                                                    + (u.get('what_they_do') or '')).lower())
                if w not in ('engineer', 'manager', 'executive', 'their', 'with', 'from', 'that', 'they', 'company',
                             'products', 'india', 'limited', 'private'))
    ind = (co.get('industry') or '').lower().strip()
    same_ind = sum(1 for r in rows if ind and ind in (r['industry'] or '').lower())
    role_hits = {}
    for r in rows:
        role = (r['role'] or '').strip()
        if role and any(w in role.lower() for w in words):
            role_hits[role] = role_hits.get(role, 0) + 1
    top = sorted(role_hits.items(), key=lambda x: -x[1])[:6]
    lines = ['Total placements recorded in our ATS: %d' % total, 'Jobs worked on: %d for %d companies' % (jobs, clients)]
    if ind and same_ind:
        lines.append('Placements with %s companies: %d' % (co.get('industry'), same_ind))
    if top:
        lines.append('Similar roles we placed: ' + ', '.join('%s (%d)' % (k, v) for k, v in top))
    return {'lines': lines, 'total': total, 'similar': top, 'same_industry': same_ind}


def style_samples(conn, oid, uid):
    rows = conn.execute("SELECT subject, body FROM bd_outreach WHERE company_id=? AND user_id=? AND step=0 AND status='sent' "
                        "ORDER BY id DESC LIMIT 3", (oid, uid)).fetchall()
    if len(rows) < 2:
        rows = list(rows) + list(conn.execute(
            "SELECT subject, body FROM crm_activities WHERE company_id=? AND created_by=? AND activity_type='email' "
            "AND is_active=1 ORDER BY id DESC LIMIT 3", (oid, uid)).fetchall())
    return ['Subject: %s\n%s' % (r['subject'] or '', (r['body'] or '')[:900]) for r in rows[:3]]


def _sender_name(conn, uid):
    r = conn.execute('SELECT display_name, username FROM users WHERE id=?', (uid,)).fetchone()
    return (r['display_name'] or r['username'] or '') if r else ''


def _agency(conn, oid):
    r = conn.execute('SELECT name FROM companies WHERE id=?', (oid,)).fetchone()
    return r['name'] if r else ''


def _call_notes(conn, cid):
    rows = conn.execute("SELECT outcome, body, created_at FROM crm_activities WHERE client_id=? AND activity_type='call' "
                        "AND is_active=1 ORDER BY id DESC LIMIT 4", (cid,)).fetchall()
    return ['%s %s: %s' % ((r['created_at'] or '')[:10], r['outcome'] or '', (r['body'] or '')[:400]) for r in rows]


# ══════════════════════════════════════════════════════════════════════════
#  CALL PLAYBOOK — how this agency opens calls and writes e-mails
#  Default: consultative BD (discovery -> credibility -> consultative conversation,
#  the three pillars of "Business Development for Recruiters (Who Hate Selling)",
#  Jay Tyler) plus well-known methods: insight-led selling, SPIN-style discovery,
#  permission-based openers, "no"-oriented questions, MPC (candidate-led) calls.
#  Written in our own words; each agency can edit it (Calling -> Settings -> Playbook).
# ══════════════════════════════════════════════════════════════════════════
DEFAULT_PLAYBOOK = """GOAL OF THE FIRST CALL
- We are not selling a service. We are finding out if there is a real hiring problem we can help with, and earning a second conversation.
- Success = they talk more than we do (70/30), we learn something real about their hiring, and they agree to one small next step.

OPENING (first 15 seconds)
- Name + agency in one short line. No "How are you today?", no "We are a leading recruitment agency".
- Give ONE specific, researched reason for calling them, today: a new plant, expansion, roles they are advertising, a leadership change, something from the notes. If there is no trigger, say honestly that we specialise in their space and ask one sharp question.
- Ask permission with a no-oriented question: "Is this a bad time for two minutes?" / "Would it be a terrible idea if I asked you one question about how you hire sales engineers?"

CREDIBILITY (one line, never a list)
- One specific proof that we know their world: the roles we fill, where that talent sits, a real placement type. Numbers ONLY from the agency facts. Never "huge database", "best talent", "end-to-end solutions".
- Give before we ask: one useful market observation (where engineers are moving, notice periods, counter-offers, salary movement) - only if it is true from our notes.

DISCOVERY (questions, then listen)
- Which roles are hardest to fill right now, and why?
- What happens to the business when that seat stays empty for 2-3 months?
- How do you hire today - job boards, referrals, in-house team, agencies? What has worked and what has not?
- Who is involved in the decision, and how fast do you usually move once you see the right profile?
- What would a good agency do differently from the ones you have tried?
- Summarise what you heard in one line and label it ("Sounds like the real issue is people leaving within a year...").

OBJECTIONS = information, not rejection
- "We don't use agencies": respect it; ask how they fill the hard roles when job boards don't work; offer something useful with no commitment (a market map, one anonymised profile).
- "Fee is too high": don't discount on the first call; ask what an empty seat costs them per month; we are paid only on success.
- "No hiring right now": ask about the next 6 months, attrition, replacements, new projects; ask permission to share a market update once a quarter.
- "Send me your details": agree, but ask one question first so what we send is useful to them.

CLOSE FOR A SMALL NEXT STEP (never "any vacancies?")
- A 15-minute call to understand one role properly, OR we send 2 anonymised profiles for their hardest role, OR we share a short market map of where that talent sits.
- Fix the date and time on the call.

CANDIDATE-LED CALL (MPC)
- When we have a strong, available candidate in their space: lead with the profile type (years, domain, achievement - no name, no CTC) and ask if that kind of person would interest them now or later.

LANGUAGE
- Short sentences, plain words, confident and curious. Hinglish is fine on the phone.
- Never: "kindly", "do the needful", "revert", "touch base", "one-stop", "manpower", "pool of candidates", "quality candidates", "any requirements?", "hope this email finds you well".
- Never badmouth other agencies. Never promise what our facts do not show.

E-MAIL
- Subject: specific to them (their plant, role or market), not "Recruitment services".
- First line about THEM. One insight or one proof. One small ask. Under 120 words. No attachments, no CV dumps."""

# phrases that make us sound like every other agency -> what to do instead
AGENCY_SPEAK = [
    ('leading recruitment', 'Say what you actually do: the roles and industry you fill.'),
    ('leading staffing', 'Say what you actually do: the roles and industry you fill.'),
    ('one-stop', 'Name the specific roles you fill instead.'),
    ('one stop', 'Name the specific roles you fill instead.'),
    ('end-to-end', 'Name the specific roles you fill instead.'),
    ('end to end recruitment', 'Name the specific roles you fill instead.'),
    ('manpower', 'Say "engineers", "sales team" - the real roles.'),
    ('pool of candidates', 'Mention one relevant profile type instead.'),
    ('huge database', 'Mention one relevant profile type instead.'),
    ('vast database', 'Mention one relevant profile type instead.'),
    ('quality candidates', 'Describe the candidate (years, domain, achievement).'),
    ('best talent', 'Describe the candidate (years, domain, achievement).'),
    ('top talent', 'Describe the candidate (years, domain, achievement).'),
    ('any vacancies', 'Ask which role is hardest to fill right now.'),
    ('any requirements', 'Ask which role is hardest to fill right now.'),
    ('any requirement', 'Ask which role is hardest to fill right now.'),
    ('any openings', 'Ask which role is hardest to fill right now.'),
    ('introduce ourselves', 'Start with a reason about THEM, not about us.'),
    ('introduce our company', 'Start with a reason about THEM, not about us.'),
    ('our services', 'Talk about their problem, not our services.'),
    ('hope this email finds you', 'Start with something about them.'),
    ('hope you are doing well', 'Start with something about them.'),
    ('i hope you are well', 'Start with something about them.'),
    ('kindly', 'Use "please" or just ask.'),
    ('do the needful', 'Say exactly what you want them to do.'),
    ('revert', 'Use "reply".'),
    ('touch base', 'Say what the call is for.'),
    ('reach out', 'Say what you want to talk about.'),
    ('synergy', 'Use plain words.'),
    ('win-win', 'Use plain words.'),
    ('cost-effective', 'Talk about the cost of an empty seat instead.'),
    ('dedicated team of', 'Name one specific thing you do.'),
    ('years of experience in recruitment', 'Show one specific proof instead.'),
    ('partner with you', 'Offer one small concrete next step instead.'),
    ('at the earliest', 'Give a day: "by Thursday?"'),
    ('please find attached', 'No attachments in a first e-mail.'),
]


def lint_text(text):
    """Agency clichés found in a text: [{phrase, tip}] (case-insensitive, each once)."""
    low = ' ' + re.sub(r'\s+', ' ', (text or '').lower()) + ' '
    out, seen = [], set()
    for ph, tip in AGENCY_SPEAK:
        if ph in low and tip not in seen:
            out.append({'phrase': ph, 'tip': tip})
            seen.add(tip)
    return out


def playbook(conn, oid):
    r = conn.execute('SELECT text FROM bd_playbook WHERE company_id=?', (oid,)).fetchone()
    return (r['text'] if r and (r['text'] or '').strip() else DEFAULT_PLAYBOOK)


@bp.route('/playbook', methods=['GET'])
@_api
def playbook_get():
    conn = get_db()
    try:
        oid = effective_company_id()
        r = conn.execute('SELECT text, updated_at, updated_by FROM bd_playbook WHERE company_id=?', (oid,)).fetchone()
        custom = bool(r and (r['text'] or '').strip())
        return jsonify({'ok': True, 'text': playbook(conn, oid), 'custom': custom, 'default': DEFAULT_PLAYBOOK,
                        'updated_at': r['updated_at'] if custom else '',
                        'phrases': [{'phrase': p, 'tip': t} for p, t in AGENCY_SPEAK]})
    finally:
        conn.close()


@bp.route('/playbook', methods=['PUT', 'POST'])
@_api
def playbook_put():
    d = request.get_json(silent=True) or {}
    text = '' if d.get('reset') else str(d.get('text') or '').strip()
    if len(text) > 20000:
        raise BDError('The playbook is too long (max 20,000 characters).')
    conn = get_db()
    try:
        oid = effective_company_id()
        conn.execute('INSERT OR REPLACE INTO bd_playbook (company_id, text, updated_at, updated_by) VALUES (?,?,?,?)',
                     (oid, text, ts(), real_user_id()))
        conn.commit()
        return jsonify({'ok': True, 'text': playbook(conn, oid), 'custom': bool(text)})
    finally:
        conn.close()


@bp.route('/lint', methods=['POST'])
@_api
def lint():
    d = request.get_json(silent=True) or {}
    return jsonify({'ok': True, 'issues': lint_text((d.get('subject') or '') + '\n' + (d.get('text') or ''))})


DRAFT_PROMPT = """You write B2B cold e-mails for an Indian recruitment agency to a prospect company's hiring person.
Rules:
- Plain, warm, specific English. No buzzwords, no "I hope this email finds you well", no exaggeration.
- Use ONLY the facts given. Numbers about the agency may come ONLY from AGENCY FACTS; if a fact is not there,
  do not claim it. Never invent client names, candidate names or numbers.
- First line must be personal to this company (from UNDERSTANDING / CALL NOTES).
- Short: {words} words max in the body. One clear ask (a 10-minute call, or "shall I share profiles?").
- Sign off with the sender's first name only; no signature block (it is added automatically).
- If WRITING STYLE samples are given, match their tone and length.
- Follow the agency's PLAYBOOK (given below the facts): it decides tone, what to avoid and the kind of ask.
- Must NOT sound like a typical agency: no "leading recruitment agency", "manpower", "pool of candidates",
  "any requirements", "kindly", "revert", "touch base", "our services". Talk about THEIR situation.
Return JSON: {{"variants": [
  {{"style": "short", "subjects": ["", "", ""], "body": ""}},
  {{"style": "value", "subjects": ["", "", ""], "body": ""}},
  {{"style": "candidate", "subjects": ["", "", ""], "body": ""}}]}}
- short: 3-5 sentences, straight to the point.
- value: lead with what the agency has done that matters to them (only AGENCY FACTS).
- candidate: offer a kind of ready candidate for a role they hire (describe the profile type, no names, no CTC)."""

FOLLOWUP_PROMPT = """Write a short follow-up (2-4 sentences, max 70 words) to a cold e-mail that got no reply, from an Indian
recruitment agency. Add one new useful angle from the facts (never invent): an insight, a candidate type, or a question
about their hardest role. Polite, no guilt-tripping, no "just following up", no agency clichés. Follow the PLAYBOOK.
Sign off with the sender's first name. Return JSON {"body": ""}."""


def _context(conn, oid, uid, cid, contact, kind='cold', note=''):
    co = _company(conn, oid, cid)
    row = _call_row(conn, oid, cid)
    u = _understanding(row)
    facts = proof_facts(conn, oid, u, co)
    ctx = ['COMPANY: %s (%s%s)' % (co['name'], co.get('industry') or 'industry unknown',
                                    (', ' + co['city']) if co.get('city') else ''),
           'TO: %s%s' % ((contact or {}).get('name') or 'the hiring team',
                         (', ' + contact['designation']) if contact and contact.get('designation') else ''),
           'SENDER: %s at %s' % (_sender_name(conn, uid) or 'the recruiter', _agency(conn, oid) or 'our agency'),
           '\nUNDERSTANDING:'] + ['- %s: %s' % (lbl, u[k]) for k, lbl in U_FIELDS if u.get(k)]
    notes = _call_notes(conn, cid)
    if notes:
        ctx += ['\nCALL NOTES:'] + ['- ' + n for n in notes]
    ctx += ['\nAGENCY FACTS (the only numbers allowed):'] + ['- ' + x for x in facts['lines']]
    if kind == 'details':
        ctx.append('\nPURPOSE: they asked on the call to send details - this is NOT cold; refer to the call.')
    if note:
        ctx.append('\nRECRUITER WANTS TO SAY: ' + note[:800])
    st = style_samples(conn, oid, uid)
    if st:
        ctx += ['\nWRITING STYLE samples (the sender\'s past e-mails):'] + ['---\n' + x for x in st]
    ctx += ['\nPLAYBOOK (how this agency sells - follow it):', playbook(conn, oid)[:9000]]
    return '\n'.join(ctx), facts


def ai_variants(conn, oid, uid, cid, contact, kind='cold', note=''):
    ctx, facts = _context(conn, oid, uid, cid, contact, kind, note)
    prompt = DRAFT_PROMPT.format(words=130 if kind == 'cold' else 170)
    j = _ai_json(prompt, ctx, 1400, 0.6, 'bd-email')
    bad = sorted({i['phrase'] for v in (j.get('variants') or []) if isinstance(v, dict)
                  for i in lint_text(' '.join([str(v.get('body') or '')] + [str(x) for x in (v.get('subjects') or [])]))})
    if bad:
        # one correction pass: the draft used agency clichés
        j2 = _ai_json(prompt, ctx + '\n\nYOUR LAST DRAFT USED THESE AGENCY CLICHES - rewrite all 3 without them: '
                      + ', '.join(bad), 1400, 0.5, 'bd-email')
        if j2.get('variants'):
            j = j2
    out = []
    for v in (j.get('variants') or [])[:3]:
        if not isinstance(v, dict) or not str(v.get('body') or '').strip():
            continue
        subs = [str(s).strip() for s in (v.get('subjects') or []) if str(s).strip()][:3]
        body = str(v['body']).strip()
        out.append({'style': str(v.get('style') or ''), 'subjects': subs or ['Hiring at %s' % _company(conn, oid, cid)['name']],
                    'body': body, 'flags': lint_text(body + ' ' + ' '.join(subs))})
    if not out:
        raise BDError('The AI did not return an e-mail. Try again.', 502)
    return out, facts


@bp.route('/companies/<int:cid>/draft', methods=['POST'])
@_api
def draft(cid):
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        oid, uid = effective_company_id(), real_user_id()
        _company(conn, oid, cid)
        cs = _contacts(conn, oid, cid)
        row = _call_row(conn, oid, cid)
        contact = _pick_contact(cs, int(d.get('contact_id') or row.get('contact_id') or 0))
        variants, facts = ai_variants(conn, oid, uid, cid, contact, d.get('kind') or 'cold', d.get('note') or '')
        return jsonify({'ok': True, 'variants': variants, 'facts': facts['lines'], 'to': (contact or {}).get('email') or '',
                        'contact': contact})
    finally:
        conn.close()


OPENER_PROMPT = """You prepare a recruiter for a cold call to this company. The recruiter must NOT sound like a typical
recruitment agency. Follow the PLAYBOOK exactly (it is the agency's own method). Use the UNDERSTANDING and CALL NOTES
for specifics; numbers only from AGENCY FACTS; never invent facts, names or achievements. Natural spoken English that
also works in Hinglish, short sentences.
Return JSON:
{"openers": [
  {"style": "trigger", "label": "Reason-for-calling", "script": "15-20 seconds: name + agency, a specific researched reason (from the notes), permission question"},
  {"style": "insight", "label": "Insight-led", "script": "open with one useful market observation for their world, then a permission question"},
  {"style": "candidate", "label": "Candidate-led", "script": "a ready profile type for a role they hire (no name, no CTC), then a question"}],
 "credibility": "one line that shows we know their world (only true facts)",
 "insight": "one market observation to give before asking (only if supported by notes; else empty)",
 "discovery": ["5 open questions in the order to ask"],
 "listen_for": ["3 signals of a real hiring pain"],
 "objections": [{"q": "We don't use agencies", "a": ""}, {"q": "Your fee is too high", "a": ""},
                {"q": "No hiring right now", "a": ""}, {"q": "Send me your details", "a": ""}],
 "next_step": "the small next step to ask for at the end",
 "avoid": ["3 things not to say on this call"]}"""


def _strs(v, n):
    return [str(x).strip() for x in (v or []) if str(x).strip()][:n]


@bp.route('/companies/<int:cid>/opener', methods=['POST'])
@_api
def opener(cid):
    conn = get_db()
    try:
        oid, uid = effective_company_id(), real_user_id()
        row = _call_row(conn, oid, cid)
        contact = _pick_contact(_contacts(conn, oid, cid), row.get('contact_id') or 0)
        ctx, _f = _context(conn, oid, uid, cid, contact)
        j = _ai_json(OPENER_PROMPT, ctx, 1600, 0.5, 'bd-opener')
        ops = [{'style': str(o.get('style') or ''), 'label': str(o.get('label') or o.get('style') or 'Opener'),
                'script': str(o.get('script') or '').strip()} for o in (j.get('openers') or [])
               if isinstance(o, dict) and str(o.get('script') or '').strip()][:3]
        if not ops and str(j.get('opener') or '').strip():
            ops = [{'style': 'trigger', 'label': 'Opener', 'script': str(j['opener']).strip()}]
        for o in ops:
            o['flags'] = lint_text(o['script'])
        op = {'openers': ops, 'opener': ops[0]['script'] if ops else '',
              'credibility': str(j.get('credibility') or '').strip(), 'insight': str(j.get('insight') or '').strip(),
              'questions': _strs(j.get('discovery') or j.get('questions'), 6), 'listen_for': _strs(j.get('listen_for'), 4),
              'objections': [{'q': str(o.get('q') or ''), 'a': str(o.get('a') or '')} for o in (j.get('objections') or [])
                             if isinstance(o, dict)][:5],
              'next_step': str(j.get('next_step') or '').strip(), 'avoid': _strs(j.get('avoid'), 4), 'at': ts()}
        conn.execute('UPDATE bd_call SET opener=? WHERE client_id=?', (json.dumps(op), cid))
        conn.commit()
        return jsonify({'ok': True, 'opener': op})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  SENDING
# ══════════════════════════════════════════════════════════════════════════
def _valid_email(e):
    return bool(re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', (e or '').strip()))


def _new_outreach(conn, oid, uid, cid, contact_id, to, subject, body, status, step=0, root_id=0, style='',
                  followups=1, scheduled_at='', batch=''):
    cur = conn.execute(
        'INSERT INTO bd_outreach (company_id, client_id, contact_id, user_id, step, root_id, style, to_addr, subject, body, '
        'status, followups, scheduled_at, batch, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (oid, cid, contact_id or 0, uid, step, root_id, style, (to or '').strip(), subject, body, status,
         1 if followups else 0, scheduled_at, batch, ts(), ts()))
    return cur.lastrowid


def send_outreach(conn, rid):
    """Send one bd_outreach row now, as its user, from their mailbox. Returns (ok, error)."""
    cur = conn.execute("UPDATE bd_outreach SET status='sending', updated_at=? WHERE id=? AND status IN ('queued','draft')",
                       (ts(), rid))
    conn.commit()
    if cur.rowcount != 1:
        return False, 'already handled'
    r = dict(conn.execute('SELECT * FROM bd_outreach WHERE id=?', (rid,)).fetchone())
    oid, uid, cid = r['company_id'], r['user_id'], r['client_id']
    call = _call_row(conn, oid, cid)
    skip = ''
    if not _valid_email(r['to_addr']):
        skip = 'No valid e-mail address'
    elif r['step'] > 0 and (call.get('replied_at') or not call.get('seq_active')):
        skip = 'They replied / sequence stopped'
    if skip:
        conn.execute("UPDATE bd_outreach SET status='cancelled', error=? WHERE id=?", (skip, rid))
        conn.commit()
        return False, skip
    core = _core()
    in_reply_to = ''
    if r['step'] > 0 and r['root_id']:
        prev = conn.execute("SELECT message_id FROM bd_outreach WHERE (id=? OR root_id=?) AND status='sent' "
                            "AND message_id!='' ORDER BY id DESC LIMIT 1", (r['root_id'], r['root_id'])).fetchone()
        in_reply_to = prev['message_id'] if prev else ''
    pixel = '%s/bdm/o/%s.gif' % (_base(), token_for(rid))
    html = core._text_to_html(r['body']) + '<img src="%s" width="1" height="1" alt="" style="display:block;border:0">' % pixel
    try:
        with core.tenant_pin(oid), core.mail_sender(uid):
            ok, err, mid, _tid = core.email_service_send(r['to_addr'], r['subject'], r['body'], body_html=html,
                                                         in_reply_to=in_reply_to)
    except Exception as e:
        ok, err, mid = False, '%s: %s' % (type(e).__name__, e), ''
    if not ok:
        conn.execute("UPDATE bd_outreach SET status='failed', error=?, updated_at=? WHERE id=?", (str(err or '')[:300], ts(), rid))
        conn.commit()
        return False, err
    now = ts()
    conn.execute("UPDATE bd_outreach SET status='sent', sent_at=?, sent_ts=?, message_id=?, error='', updated_at=? WHERE id=?",
                 (now, time.time(), mid or '', now, rid))
    st = _settings(conn, uid, oid)
    days = _fu_days(st)
    root = r['root_id'] or rid
    if r['step'] == 0:
        on = bool(r['followups']) and bool(days)
        conn.execute("UPDATE bd_call SET email_state='sent', last_email_at=?, seq_active=?, seq_step=0, seq_next_at=?, "
                     "seq_root_id=?, replied_at='' WHERE client_id=?",
                     (now, 1 if on else 0, _iso(_fu_time(now_ist(), days[0], st)) if on else '', rid, cid))
    else:
        nxt = days[r['step']] - days[r['step'] - 1] if r['step'] < len(days) else 0
        conn.execute("UPDATE bd_call SET last_email_at=?, seq_step=?, seq_next_at=?, seq_active=? WHERE client_id=?",
                     (now, r['step'], _iso(_fu_time(now_ist(), nxt, st)) if nxt else '', 1 if nxt else 0, cid))
    who = conn.execute('SELECT name FROM crm_contacts WHERE id=?', (r['contact_id'],)).fetchone()
    with core.tenant_pin(oid):
        _log_activity(conn, oid, cid, r['contact_id'], 'email', r['subject'], r['body'], '', '',
                      {'direction': 'outgoing', 'to': r['to_addr'], 'sent': True, 'bd_outreach': rid, 'step': r['step'],
                       'root': root}, uid)
    conn.commit()
    return True, None


def _fu_time(base, days, st):
    d = base + datetime.timedelta(days=days)
    while d.weekday() == 6:
        d += datetime.timedelta(days=1)
    h = max(int(st.get('start_hour') or 9), 10)
    return d.replace(hour=min(h + 1, 17), minute=random.randint(0, 50), second=0, microsecond=0)


@bp.route('/companies/<int:cid>/send', methods=['POST'])
@_api
def send_one(cid):
    """Send (now) or queue one e-mail written / picked in the composer."""
    d = request.get_json(silent=True) or {}
    to = (d.get('to') or '').strip()
    subject = (d.get('subject') or '').strip()
    body = (d.get('body') or '').strip()
    if not _valid_email(to):
        raise BDError('Add a valid e-mail address.')
    if not subject or not body:
        raise BDError('Subject and message are required.')
    conn = get_db()
    try:
        oid, uid = effective_company_id(), real_user_id()
        _company(conn, oid, cid)
        row = _call_row(conn, oid, cid)
        cs = _contacts(conn, oid, cid)
        contact = _pick_contact(cs, int(d.get('contact_id') or row.get('contact_id') or 0))
        if contact and not contact.get('email') and d.get('save_email', True):
            from modules import crm
            ek = crm._norm_email(to)
            if not conn.execute('SELECT 1 FROM crm_contacts WHERE company_id=? AND email_key=? AND is_active=1', (oid, ek)).fetchone():
                conn.execute('UPDATE crm_contacts SET email=?, email_key=? WHERE id=?', (to, ek, contact['id']))
        rid = _new_outreach(conn, oid, uid, cid, contact['id'] if contact else 0, to, subject, body,
                            'queued' if d.get('queue') else 'draft', style=d.get('style') or '',
                            followups=d.get('followups', True), scheduled_at=ts())
        conn.commit()
        if d.get('queue'):
            return jsonify({'ok': True, 'queued': True, 'id': rid})
        ok, err = send_outreach(conn, rid)
        if not ok:
            raise BDError(err or 'Could not send', 400)
        return jsonify({'ok': True, 'sent': True, 'id': rid, 'today': _today_stats(conn, oid, uid)})
    finally:
        conn.close()


# ── bulk: drafts in the background, then review ────────────────────────────
_BULK = {}


def _bulk_worker(app, oid, uid, ids, style, note, batch):
    with app.app_context():
        conn = get_db()
        core = _core()
        try:
            with core.tenant_pin(oid), core.mail_sender(uid):
                for rid in ids:
                    r = conn.execute("SELECT * FROM bd_outreach WHERE id=? AND status='drafting'", (rid,)).fetchone()
                    if not r:
                        continue
                    try:
                        contact = conn.execute('SELECT id, name, designation, email FROM crm_contacts WHERE id=?',
                                               (r['contact_id'],)).fetchone()
                        variants, _f = _variants_bg(conn, oid, uid, r['client_id'], dict(contact) if contact else None, note)
                        v = next((x for x in variants if x['style'] == style), variants[0])
                        conn.execute("UPDATE bd_outreach SET status='draft', subject=?, body=?, style=?, error='', updated_at=? "
                                     "WHERE id=? AND status='drafting'", (v['subjects'][0], v['body'], v['style'], ts(), rid))
                    except Exception as e:
                        msg = 'AI cap reached' if type(e).__name__ == 'TokenCapError' else (getattr(e, 'msg', '') or str(e))
                        conn.execute("UPDATE bd_outreach SET status='draft_failed', error=?, updated_at=? WHERE id=?",
                                     (msg[:300], ts(), rid))
                    conn.commit()
        finally:
            conn.close()
            _BULK.pop(batch, None)


def _variants_bg(conn, oid, uid, cid, contact, note):
    """ai_variants outside a request: effective_company_id comes from the tenant pin."""
    return ai_variants(conn, oid, uid, cid, contact, 'cold', note)


@bp.route('/bulk-draft', methods=['POST'])
@_api
def bulk_draft():
    d = request.get_json(silent=True) or {}
    ids = [int(x) for x in (d.get('client_ids') or []) if str(x).isdigit()][:100]
    style = d.get('style') if d.get('style') in ('short', 'value', 'candidate') else 'short'
    if not ids:
        raise BDError('Select prospects first.')
    if not _ai_key():
        raise BDError('AI is not set up. Add the DeepSeek API key in Settings.')
    conn = get_db()
    try:
        oid, uid = effective_company_id(), real_user_id()
        batch = 'b%d%04d' % (int(time.time()), random.randint(0, 9999))
        made, skipped = [], []
        for cid in ids:
            co = conn.execute("SELECT id, name FROM crm_clients WHERE id=? AND company_id=? AND is_active=1", (cid, oid)).fetchone()
            if not co:
                continue
            row = _call_row(conn, oid, cid)
            if conn.execute("SELECT 1 FROM bd_outreach WHERE client_id=? AND status IN ('drafting','draft','queued','sending')",
                            (cid,)).fetchone():
                skipped.append({'id': cid, 'name': co['name'], 'why': 'Already has an e-mail waiting'})
                continue
            contact = _pick_contact(_contacts(conn, oid, cid), row.get('contact_id') or 0)
            if not contact or not _valid_email(contact.get('email')):
                skipped.append({'id': cid, 'name': co['name'], 'why': 'No contact e-mail'})
                continue
            made.append(_new_outreach(conn, oid, uid, cid, contact['id'], contact['email'], '', '', 'drafting',
                                      style=style, followups=d.get('followups', True), batch=batch))
        conn.commit()
    finally:
        conn.close()
    if made:
        app = _core().app
        _BULK[batch] = len(made)
        if d.get('_sync'):
            _bulk_worker(app, oid, uid, made, style, (d.get('note') or '')[:800], batch)
        else:
            threading.Thread(target=_bulk_worker, args=(app, oid, uid, made, style, (d.get('note') or '')[:800], batch),
                             daemon=True).start()
    return jsonify({'ok': True, 'batch': batch, 'drafting': len(made), 'skipped': skipped})


@bp.route('/review', methods=['GET'])
@_api
def review():
    conn = get_db()
    try:
        oid, uid = effective_company_id(), real_user_id()
        rows = [dict(r) for r in conn.execute(
            "SELECT o.*, c.name AS company, k.name AS contact_name, k.designation FROM bd_outreach o "
            "JOIN crm_clients c ON c.id=o.client_id LEFT JOIN crm_contacts k ON k.id=o.contact_id "
            "WHERE o.company_id=? AND o.user_id=? AND o.step=0 AND o.status IN ('drafting','draft','draft_failed') "
            "ORDER BY o.id", (oid, uid)).fetchall()]
        queued = conn.execute("SELECT COUNT(*) FROM bd_outreach WHERE company_id=? AND user_id=? AND status IN ('queued','sending')",
                              (oid, uid)).fetchone()[0]
        return jsonify({'ok': True, 'drafts': rows, 'queued': queued,
                        'working': sum(1 for r in rows if r['status'] == 'drafting')})
    finally:
        conn.close()


def _own_outreach(conn, rid):
    r = conn.execute('SELECT * FROM bd_outreach WHERE id=? AND company_id=?', (rid, effective_company_id())).fetchone()
    if not r or r['user_id'] != real_user_id():
        raise BDError('E-mail not found.', 404)
    return dict(r)


@bp.route('/emails/<int:rid>', methods=['PATCH'])
@_api
def edit_email(rid):
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        r = _own_outreach(conn, rid)
        if r['status'] not in ('draft', 'queued', 'draft_failed'):
            raise BDError('This e-mail can no longer be changed.')
        to = (d.get('to') if 'to' in d else r['to_addr']).strip()
        conn.execute("UPDATE bd_outreach SET subject=?, body=?, to_addr=?, followups=?, "
                     "status=CASE WHEN status='draft_failed' THEN 'draft' ELSE status END, updated_at=? WHERE id=?",
                     (str(d.get('subject', r['subject'])).strip(), str(d.get('body', r['body'])).strip(), to,
                      1 if d.get('followups', r['followups']) else 0, ts(), rid))
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


@bp.route('/emails/<int:rid>/approve', methods=['POST'])
@_api
def approve(rid):
    """Approve a reviewed draft: queue it after the user's last queued e-mail (spaced by the gap)."""
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        oid, uid = effective_company_id(), real_user_id()
        r = _own_outreach(conn, rid)
        if r['status'] != 'draft':
            raise BDError('Only a ready draft can be approved.')
        for k in ('subject', 'body', 'to'):
            if k in d:
                conn.execute('UPDATE bd_outreach SET %s=? WHERE id=?' % ('to_addr' if k == 'to' else k),
                             (str(d[k]).strip(), rid))
        r = dict(conn.execute('SELECT * FROM bd_outreach WHERE id=?', (rid,)).fetchone())
        if not _valid_email(r['to_addr']) or not r['subject'] or not r['body']:
            raise BDError('Fill the e-mail address, subject and message first.')
        st = _settings(conn, uid, oid)
        last = conn.execute("SELECT MAX(scheduled_at) FROM bd_outreach WHERE user_id=? AND status IN ('queued','sending')",
                            (uid,)).fetchone()[0]
        base = max(_parse(last) or now_ist(), now_ist())
        gap = max(60, int(st.get('gap_seconds') or 150))
        when = base + datetime.timedelta(seconds=gap + random.randint(0, gap // 2)) if last else now_ist()
        conn.execute("UPDATE bd_outreach SET status='queued', scheduled_at=?, updated_at=? WHERE id=?", (_iso(when), ts(), rid))
        conn.commit()
        return jsonify({'ok': True, 'scheduled_at': _iso(when)})
    finally:
        conn.close()


@bp.route('/emails/<int:rid>/skip', methods=['POST'])
@_api
def skip(rid):
    conn = get_db()
    try:
        r = _own_outreach(conn, rid)
        if r['status'] in ('sent', 'sending'):
            raise BDError('Already sent.')
        conn.execute("UPDATE bd_outreach SET status='cancelled', error='Skipped', updated_at=? WHERE id=?", (ts(), rid))
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


@bp.route('/companies/<int:cid>/sequence', methods=['POST'])
@_api
def sequence(cid):
    """Stop (or restart) the automatic follow-ups for a prospect."""
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        oid = effective_company_id()
        _company(conn, oid, cid)
        row = _call_row(conn, oid, cid)
        if d.get('active'):
            st = _settings(conn, real_user_id(), oid)
            days = _fu_days(st)
            step = row.get('seq_step') or 0
            if not row.get('seq_root_id') or step >= len(days) or row.get('replied_at'):
                raise BDError('Nothing to follow up (no first e-mail, all follow-ups sent, or they replied).')
            conn.execute('UPDATE bd_call SET seq_active=1, seq_next_at=? WHERE client_id=?',
                         (_iso(_fu_time(now_ist(), 1, st)), cid))
        else:
            _stop_sequence(conn, cid, 'Stopped by user')
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  BACKGROUND: queue, follow-ups, replies
# ══════════════════════════════════════════════════════════════════════════
def _in_hours(st, now=None):
    now = now or now_ist()
    return now.weekday() != 6 and int(st.get('start_hour') or 9) <= now.hour < int(st.get('end_hour') or 19)


def _sent_today(conn, uid):
    return conn.execute("SELECT COUNT(*) FROM bd_outreach WHERE user_id=? AND status='sent' AND sent_at LIKE ?",
                        (uid, now_ist().strftime('%Y-%m-%d') + '%')).fetchone()[0]


def run_queue(conn, force=False):
    """Send due queued e-mails, one per user per tick (spacing), inside working hours and the daily cap."""
    sent = 0
    now = _iso(now_ist())
    users = [r[0] for r in conn.execute("SELECT DISTINCT user_id FROM bd_outreach WHERE status='queued' AND scheduled_at<=?",
                                        (now,)).fetchall()]
    for uid in users:
        r = conn.execute("SELECT id, company_id FROM bd_outreach WHERE user_id=? AND status='queued' AND scheduled_at<=? "
                         "ORDER BY scheduled_at, id LIMIT 1", (uid, now)).fetchone()
        if not r:
            continue
        st = _settings(conn, uid, r['company_id'])
        if not force and (not _in_hours(st) or _sent_today(conn, uid) >= int(st.get('daily_cap') or 60)):
            continue
        ok, err = send_outreach(conn, r['id'])
        if ok:
            sent += 1
        elif err and any(w in str(err).lower() for w in ('not configured', 'auth', 'connect', 'timed out', 'timeout',
                                                          'network', 'temporarily')):
            conn.execute("UPDATE bd_outreach SET status='queued', scheduled_at=?, error=? WHERE id=?",
                         (_iso(now_ist() + datetime.timedelta(minutes=15)), str(err)[:300], r['id']))
            conn.commit()
    return sent


def _fallback_followup(step, first_name):
    if step == 1:
        return ('Hi %s,\n\nJust bringing my note below back to the top of your inbox. If hiring support is useful in the '
                'coming weeks, I would be glad to share a few relevant profiles.\n\nShall we take 10 minutes this week?'
                % (first_name or 'there'))
    return ('Hi %s,\n\nI will not keep filling your inbox - if a requirement opens up later, just reply to this e-mail '
            'and I will share profiles within a day.' % (first_name or 'there'))


def run_followups(conn, force=False):
    """Draft + send the next follow-up for prospects whose sequence is due."""
    made = 0
    now = _iso(now_ist())
    rows = [dict(r) for r in conn.execute("SELECT * FROM bd_call WHERE seq_active=1 AND seq_next_at!='' AND seq_next_at<=? "
                                          "AND COALESCE(replied_at,'')=''", (now,)).fetchall()]
    core = _core()
    for b in rows:
        root = conn.execute('SELECT * FROM bd_outreach WHERE id=?', (b['seq_root_id'],)).fetchone()
        if not root or root['status'] != 'sent':
            conn.execute('UPDATE bd_call SET seq_active=0 WHERE client_id=?', (b['client_id'],))
            conn.commit()
            continue
        root = dict(root)
        st = _settings(conn, root['user_id'], root['company_id'])
        days = _fu_days(st)
        step = (b['seq_step'] or 0) + 1
        if step > len(days):
            conn.execute('UPDATE bd_call SET seq_active=0 WHERE client_id=?', (b['client_id'],))
            conn.commit()
            continue
        if not force and not _in_hours(st):
            continue
        if conn.execute("SELECT 1 FROM bd_outreach WHERE root_id=? AND step=? AND status IN ('queued','sending','sent')",
                        (root['id'], step)).fetchone():
            continue
        contact = conn.execute('SELECT name, designation FROM crm_contacts WHERE id=?', (root['contact_id'],)).fetchone()
        first = ((contact['name'] if contact else '') or '').split(' ')[0]
        body = ''
        try:
            with core.tenant_pin(root['company_id']), core.mail_sender(root['user_id']):
                ctx, _f = _context(conn, root['company_id'], root['user_id'], root['client_id'],
                                   dict(contact) if contact else None)
                j = _ai_json(FOLLOWUP_PROMPT, ctx + '\n\nFIRST E-MAIL:\n' + root['body'][:1500] +
                             '\n\nThis is follow-up number %d.' % step, 300, 0.5, 'bd-followup')
                body = str(j.get('body') or '').strip()
        except Exception as e:
            print('[bd-calling] follow-up AI failed, using the short template: %s' % e)
        body = body or _fallback_followup(step, first)
        subj = root['subject'] if root['subject'].lower().startswith('re:') else 'Re: ' + root['subject']
        rid = _new_outreach(conn, root['company_id'], root['user_id'], root['client_id'], root['contact_id'], root['to_addr'],
                            subj, body, 'queued', step=step, root_id=root['id'], style='followup', followups=1,
                            scheduled_at=now)
        conn.commit()
        ok, _err2 = send_outreach(conn, rid)
        made += 1 if ok else 0
    return made


def _addr(s):
    m = re.search(r'[\w.+\-]+@[\w\-]+(\.[\w\-]+)+', s or '')
    return m.group(0).lower() if m else ''


def check_replies(conn):
    """A synced inbox message from the prospect after our e-mail = a reply: stop follow-ups, ring the bell."""
    found = 0
    since = _iso(now_ist() - datetime.timedelta(days=45))
    rows = [dict(r) for r in conn.execute(
        "SELECT o.id, o.company_id, o.client_id, o.user_id, o.to_addr, o.sent_ts, o.subject FROM bd_outreach o "
        "JOIN bd_call b ON b.client_id=o.client_id WHERE o.status='sent' AND o.step=0 AND o.sent_at>=? "
        "AND COALESCE(b.replied_at,'')='' ", (since,)).fetchall()]
    for r in rows:
        to = _addr(r['to_addr'])
        if not to:
            continue
        hit = conn.execute("SELECT id, subject, snippet, date_ts FROM emails WHERE owner_id=? AND COALESCE(folder,'')!='Sent' "
                           "AND LOWER(from_addr) LIKE ? AND COALESCE(date_ts,0)>=? ORDER BY date_ts LIMIT 1",
                           (r['company_id'], '%' + to + '%', (r['sent_ts'] or 0) - 60)).fetchone()
        if not hit:
            continue
        found += 1
        now = ts()
        conn.execute("UPDATE bd_call SET replied_at=?, email_state='replied', seq_active=0, seq_next_at='' WHERE client_id=?",
                     (now, r['client_id']))
        conn.execute("UPDATE bd_outreach SET replied_at=? WHERE id=? OR root_id=?", (now, r['id'], r['id']))
        conn.execute("UPDATE bd_outreach SET status='cancelled', error='They replied' WHERE root_id=? AND status IN ('queued','draft')",
                     (r['id'],))
        co = conn.execute('SELECT name FROM crm_clients WHERE id=?', (r['client_id'],)).fetchone()
        name = co['name'] if co else 'A prospect'
        _notify(conn, r['company_id'], r['user_id'], '%s replied to your e-mail' % name,
                (hit['subject'] or '') + ' — ' + (hit['snippet'] or '')[:200])
        try:
            with _core().tenant_pin(r['company_id']):
                _log_activity(conn, r['company_id'], r['client_id'], 0, 'note', 'Replied to the cold e-mail',
                              (hit['snippet'] or '')[:500], 'Replied', '', {'bd_reply': r['id']}, r['user_id'])
        except Exception:
            pass
        conn.commit()
    return found


def tick(force=False):
    conn = get_db()
    try:
        check_replies(conn)
        run_followups(conn, force)
        run_queue(conn, force)
    finally:
        conn.close()


def _loop():
    time.sleep(90)
    while True:
        try:
            tick()
        except Exception as e:
            print('[bd-calling] loop error: %s' % e)
        time.sleep(TICK_SECONDS)


_started = False


def start_worker():
    global _started
    if _started or 'pytest' in sys.modules or os.environ.get('HIRELAB_NO_WORKERS'):
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
    print('[bd-calling] background thread started')


start_worker()


# ══════════════════════════════════════════════════════════════════════════
#  SETTINGS / TARGETS
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/settings', methods=['PUT', 'POST'])
@_api
def save_settings():
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        oid, uid = effective_company_id(), real_user_id()
        s = _settings(conn, uid, oid)
        lim = {'calls_target': (0, 500), 'emails_target': (0, 300), 'gap_seconds': (60, 3600), 'daily_cap': (1, 300),
               'fu_on': (0, 1), 'start_hour': (0, 23), 'end_hour': (1, 24)}
        for k, (lo, hi) in lim.items():
            if k in d:
                try:
                    s[k] = max(lo, min(hi, int(d[k])))
                except (TypeError, ValueError):
                    raise BDError('%s must be a number.' % k)
        if 'fu_days' in d:
            s['fu_days'] = ','.join(str(x) for x in _fu_days({'fu_days': d['fu_days']}))
        if s['end_hour'] <= s['start_hour']:
            raise BDError('End hour must be after the start hour.')
        conn.execute('INSERT OR REPLACE INTO bd_call_settings (user_id, company_id, settings) VALUES (?,?,?)',
                     (uid, oid, json.dumps(s)))
        conn.commit()
        return jsonify({'ok': True, 'settings': s})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  IMPORT (paste from Excel / CSV)
# ══════════════════════════════════════════════════════════════════════════
_ALIASES = {
    'company': ('company', 'company name', 'organisation', 'organization', 'account', 'client', 'firm'),
    'contact': ('contact', 'contact name', 'name', 'person', 'hr', 'full name'),
    'designation': ('designation', 'title', 'job title', 'role', 'position'),
    'phone': ('phone', 'mobile', 'phone number', 'contact number', 'number', 'mobile number'),
    'email': ('email', 'e-mail', 'email id', 'mail', 'email address'),
    'website': ('website', 'site', 'url', 'domain', 'web'),
    'city': ('city', 'location', 'place'),
    'industry': ('industry', 'sector', 'segment'),
    'linkedin': ('linkedin', 'linkedin url', 'profile'),
    'notes': ('notes', 'note', 'remarks', 'comment', 'comments'),
}


def parse_paste(text):
    text = (text or '').strip('\ufeff\n ')
    if not text:
        return [], []
    first = text.split('\n', 1)[0]
    delim = '\t' if '\t' in first else (';' if first.count(';') > first.count(',') else ',')
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any((c or '').strip() for c in r)]
    if not rows:
        return [], []
    head = [h.strip().lower() for h in rows[0]]
    cols = {}
    for i, h in enumerate(head):
        for k, al in _ALIASES.items():
            if h in al and k not in cols:
                cols[k] = i
    body = rows[1:]
    if 'company' not in cols:          # no header row: company, contact, designation, phone, email, website, city
        order = ['company', 'contact', 'designation', 'phone', 'email', 'website', 'city', 'industry']
        cols = {k: i for i, k in enumerate(order)}
        body = rows
    out = []
    for r in body[:1000]:
        g = lambda k: (r[cols[k]].strip() if k in cols and cols[k] < len(r) else '')
        item = {k: g(k) for k in _ALIASES}
        if item['company']:
            out.append(item)
    return out, sorted(cols, key=lambda k: cols[k])


@bp.route('/import', methods=['POST'])
@_api
def import_rows():
    d = request.get_json(silent=True) or {}
    ws = _ws()
    items, cols = parse_paste(d.get('text') or '')
    if not items:
        raise BDError('Nothing to import: paste rows with at least a company name (first row can be the headings).')
    conn = get_db()
    try:
        oid = effective_company_id()
        from modules import crm
        seen = {}
        preview = []
        for it in items:
            key = crm._norm_name(it['company'])
            dup = conn.execute("SELECT id, name, COALESCE(record_type,'client') AS rt FROM crm_clients WHERE company_id=? "
                               "AND name_key=? AND is_active=1", (oid, key)).fetchone()
            em = crm._norm_email(it['email']) if it['email'] else ''
            edup = conn.execute('SELECT id FROM crm_contacts WHERE company_id=? AND email_key=? AND is_active=1',
                                (oid, em)).fetchone() if em else None
            state = 'new'
            if dup:
                state = 'exists_client' if dup['rt'] == 'client' else 'exists_prospect'
            elif key in seen:
                state = 'same_company'
            seen.setdefault(key, 1)
            if it['email'] and not _valid_email(it['email']):
                it['email_bad'] = True
            preview.append(dict(it, state=state, existing_id=dup['id'] if dup else 0, contact_dup=bool(edup)))
        if not d.get('commit'):
            return jsonify({'ok': True, 'preview': preview[:300], 'columns': cols, 'total': len(preview),
                            'new': sum(1 for p in preview if p['state'] == 'new'),
                            'exists': sum(1 for p in preview if p['state'].startswith('exists'))})
        created, contacts, skipped = 0, 0, 0
        ids = {}
        for p in preview:
            key = crm._norm_name(p['company'])
            if p['state'] == 'exists_client':
                skipped += 1
                continue
            cid = p['existing_id'] or ids.get(key)
            if not cid:
                cid = crm.ClientService.create(conn, {'name': p['company'], 'record_type': 'prospect', 'website': p['website'],
                                                      'city': p['city'], 'industry': p['industry'], 'notes': p['notes']})
                conn.execute('UPDATE crm_clients SET bd_workspace_id=?, owner_user_id=? WHERE id=?', (ws, real_user_id(), cid))
                created += 1
            elif p['state'] == 'exists_prospect' and not d.get('add_contacts_to_existing', True):
                skipped += 1
                continue
            ids[key] = cid
            _call_row(conn, oid, cid)
            if p['contact'] and not p['contact_dup']:
                try:
                    ctid = crm.ContactService.create(conn, cid, {'name': p['contact'], 'designation': p['designation'],
                                                                 'phone': p['phone'], 'linkedin': p['linkedin'],
                                                                 'email': p['email'] if not p.get('email_bad') else ''})
                    contacts += 1
                    if not conn.execute('SELECT contact_id FROM bd_call WHERE client_id=?', (cid,)).fetchone()['contact_id']:
                        conn.execute('UPDATE bd_call SET contact_id=? WHERE client_id=?', (ctid, cid))
                except Exception as e:
                    if type(e).__name__ not in ('DuplicateError', 'ValidationError'):
                        raise
        conn.commit()
        return jsonify({'ok': True, 'created': created, 'contacts': contacts, 'skipped': skipped})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  E-MAIL GUESS
# ══════════════════════════════════════════════════════════════════════════
_FREE = {'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'rediffmail.com', 'yahoo.co.in', 'live.com', 'icloud.com'}


def _domain(website):
    w = (website or '').strip().lower()
    if not w:
        return ''
    if '://' not in w:
        w = 'http://' + w
    h = urlparse(w).hostname or ''
    h = re.sub(r'^www\d?\.', '', h)
    return h if '.' in h and h not in _FREE else ''


def _name_parts(name):
    parts = [re.sub(r'[^a-z]', '', p) for p in (name or '').lower().split()]
    parts = [p for p in parts if p and p not in ('mr', 'ms', 'mrs', 'dr')]
    return (parts[0], parts[-1] if len(parts) > 1 else '') if parts else ('', '')


PATTERNS = [('{f}.{l}', 'first.last'), ('{f}', 'first'), ('{f}{l}', 'firstlast'), ('{fi}{l}', 'flast'),
            ('{f}_{l}', 'first_last'), ('{f}{li}', 'firstl'), ('{l}.{f}', 'last.first')]


def _apply(pat, f, l):
    if ('{l}' in pat or '{li}' in pat) and not l:
        return ''
    return pat.format(f=f, l=l, fi=f[:1], li=l[:1])


def guess_emails(name, website, colleagues=()):
    """Most likely addresses: a colleague's real address shows the company pattern; else common patterns."""
    f, l = _name_parts(name)
    if not f:
        return []
    dom = ''
    learned = None
    for cn, ce in colleagues:
        ce = (ce or '').lower().strip()
        if '@' not in ce:
            continue
        local, d = ce.split('@', 1)
        if d in _FREE:
            continue
        dom = dom or d
        cf, cl = _name_parts(cn)
        for pat, label in PATTERNS:
            if cf and _apply(pat, cf, cl) == local:
                learned = (pat, label, d)
                break
        if learned:
            break
    dom = (learned[2] if learned else '') or _domain(website) or dom
    if not dom:
        return []
    out = []
    if learned:
        e = _apply(learned[0], f, l)
        if e:
            out.append({'email': '%s@%s' % (e, dom), 'why': 'Same pattern as a colleague (%s)' % learned[1], 'level': 'high'})
    for pat, label in PATTERNS:
        e = _apply(pat, f, l)
        if e and not any(o['email'] == '%s@%s' % (e, dom) for o in out):
            out.append({'email': '%s@%s' % (e, dom), 'why': 'Common pattern (%s)' % label, 'level': 'guess'})
    return out[:6]


@bp.route('/companies/<int:cid>/email-guess', methods=['GET'])
@_api
def email_guess(cid):
    conn = get_db()
    try:
        oid = effective_company_id()
        co = _company(conn, oid, cid)
        row = _call_row(conn, oid, cid)
        cs = _contacts(conn, oid, cid)
        want = int(request.args.get('contact_id') or row.get('contact_id') or 0)
        c = _pick_contact(cs, want)
        name = (request.args.get('name') or (c or {}).get('name') or '').strip()
        if not name:
            raise BDError('Add the contact name first.')
        colleagues = [(x['name'], x['email']) for x in cs if x['email'] and (not c or x['id'] != c['id'])]
        out = guess_emails(name, co.get('website') or co.get('domain') or '', colleagues)
        if not out:
            raise BDError('Add the company website first (the e-mail domain comes from it).')
        return jsonify({'ok': True, 'guesses': out})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  PUBLIC: open pixel  +  the page script
# ══════════════════════════════════════════════════════════════════════════
_GIF = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,'
        b'\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')


@pages.route('/bdm/o/<tok>.gif')
def track_open(tok):
    rid = parse_token(tok)
    if rid:
        conn = get_db()
        try:
            r = conn.execute("SELECT id, client_id, root_id, sent_ts FROM bd_outreach WHERE id=? AND status='sent'", (rid,)).fetchone()
            # ignore the first seconds after sending (our own mail client / Gmail's image check)
            if r and time.time() - (r['sent_ts'] or 0) > 20:
                now = ts()
                conn.execute("UPDATE bd_outreach SET opens=opens+1, opened_at=CASE WHEN COALESCE(opened_at,'')='' THEN ? "
                             "ELSE opened_at END WHERE id=?", (now, rid))
                conn.execute("UPDATE bd_call SET opens=opens+1, last_open_at=?, email_state=CASE WHEN email_state='replied' "
                             "THEN email_state ELSE 'opened' END WHERE client_id=?", (now, r['client_id']))
                conn.commit()
        finally:
            conn.close()
    return Response(_GIF, mimetype='image/gif', headers={'Cache-Control': 'no-store, max-age=0'})


@pages.route('/bd-calling.js')
def page_js():
    with open(os.path.join(ROOT, 'bd-calling.js'), encoding='utf-8') as f:
        resp = Response(f.read(), mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp
