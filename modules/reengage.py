"""
Candidate re-engagement — automatic "please update your profile" e-mails.

Six months after a candidate entered HireLab (and every six months after
that), the candidate gets one e-mail from the company mailbox:

  * job candidates: "On <date> HireLab considered your profile for <role> with
    <client>. It did not move forward ... we specialise in Data Center, Solar,
    Electrical and Automation ... we already have most of your details, please
    refresh them" + a personal update-profile link;
  * Central DB candidates: a shorter note about the talent network and the
    domains we hire for, with the same link.

A person is one e-mail address (they may sit in several jobs); the LATEST job
is the one mentioned. Nobody is mailed who:
  * has no e-mail, unsubscribed (candidates.do_not_email), or was switched OFF
    by the recruiter (candidates.reengage_off);
  * is live in a pipeline: Shared with Client, Interview, Offer, Placed, Joined.

The six months restart from the latest of: entering HireLab, our last e-mail,
or the candidate updating their profile.

Sending: a background thread wakes every 5 minutes and sends a few mails
through the normal ATS mail pipeline (company mailbox + signature, saved in the
candidate's e-mail thread), Monday–Saturday between the configured hours, up to
the daily cap. Paused until the admin switches it on.

Additive only: two tables (reengage_settings, reengage_log) and one column
(candidates.reengage_off). Unsubscribe reuses the ATS /u/<token> page.
"""

import datetime
import hashlib
import hmac
import html
import math
import os
import re
import sys
import threading
import time

from flask import Blueprint, request, jsonify, redirect, Response

from modules.shared import get_db, ts, effective_company_id, real_user_id, is_company_admin, login_required, _core
from modules import register_migration

bp = Blueprint('reengage', __name__, url_prefix='/api/reengage')
pages = Blueprint('reengage_pages', __name__)

IST = datetime.timedelta(hours=5, minutes=30)
ACTIVE_STAGES = {'shared with client', 'interview inprocess', 'interview in process', 'interview', 'offered',
                 'offer', 'offer accepted', 'placed', 'joined'}
DOMAINS_LINE = 'Data Center, Solar & Renewable Energy, Electrical and Industrial Automation'
DOMAINS_DETAIL = ('Data Center (critical facilities, MEP, electrical and project roles), Solar & Renewable Energy '
                  '(EPC, O&M, design and sales), Electrical (switchgear, power distribution, substations) and '
                  'Industrial Automation (PLC, SCADA, DCS, BMS)')
DEFAULTS = {'enabled': 0, 'daily_cap': 50, 'interval_days': 180, 'start_hour': 10, 'end_hour': 19, 'reminder_days': 7}
REMINDER_WINDOW_DAYS = 14   # a reminder is only sent within this many days after it falls due
TICK_SECONDS = 300
MAX_PER_TICK = 5


@register_migration
def _migrate_reengage(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS reengage_settings (
        company_id INTEGER PRIMARY KEY,
        enabled INTEGER DEFAULT 0,
        daily_cap INTEGER DEFAULT 50,
        interval_days INTEGER DEFAULT 180,
        start_hour INTEGER DEFAULT 10,
        end_hour INTEGER DEFAULT 19,
        tick_at TEXT DEFAULT '',
        last_error TEXT DEFAULT '',
        updated_by INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT ''
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS reengage_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        candidate_id INTEGER NOT NULL,
        email TEXT NOT NULL,
        cycle_key TEXT NOT NULL,
        kind TEXT DEFAULT 'job',
        mandate_id INTEGER DEFAULT 0,
        role TEXT DEFAULT '',
        client TEXT DEFAULT '',
        considered_on TEXT DEFAULT '',
        status TEXT DEFAULT 'sending',
        error TEXT DEFAULT '',
        sent_at TEXT DEFAULT '',
        opened_at TEXT DEFAULT '',
        clicked_at TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        UNIQUE(company_id, email, cycle_key)
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_reengage_log_co ON reengage_log(company_id, sent_at)')
    try:
        conn.execute('ALTER TABLE candidates ADD COLUMN reengage_off INTEGER DEFAULT 0')
    except Exception:
        pass
    conn.execute('''CREATE TABLE IF NOT EXISTS reengage_text (
        company_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        subject TEXT DEFAULT '',
        body TEXT DEFAULT '',
        updated_by INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT '',
        PRIMARY KEY (company_id, kind)
    )''')
    try:      # Wave 2: one soft reminder N days after the e-mail (0 = off)
        conn.execute('ALTER TABLE reengage_settings ADD COLUMN reminder_days INTEGER DEFAULT 7')
    except Exception:
        pass
    conn.commit()


# ── small helpers ──────────────────────────────────────────────────────────
def now_ist():
    return datetime.datetime.utcnow() + IST


def _dt(s):
    s = str(s or '').strip().replace(' ', 'T')
    if len(s) < 10:
        return None
    for fmt, n in (('%Y-%m-%dT%H:%M:%S', 19), ('%Y-%m-%dT%H:%M', 16), ('%Y-%m-%d', 10)):
        try:
            return datetime.datetime.strptime(s[:n], fmt)
        except ValueError:
            continue
    return None


def nice_date(s):
    d = _dt(s)
    return '%d %s %d' % (d.day, d.strftime('%B'), d.year) if d else ''


def first_name(name):
    n = str(name or '').strip()
    for t in ('mr.', 'mrs.', 'ms.', 'dr.', 'er.', 'mr', 'mrs', 'ms', 'dr', 'er'):
        if n.lower().startswith(t + ' '):
            n = n[len(t) + 1:].strip()
            break
    return (n.split() or ['there'])[0].strip(',').title()


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_settings(conn, oid):
    r = conn.execute('SELECT * FROM reengage_settings WHERE company_id=?', (oid,)).fetchone()
    s = dict(DEFAULTS)
    if r:
        s.update({k: r[k] for k in r.keys() if k in s or k in ('last_error', 'updated_at')})
    return s


def _sign(raw):
    key = _core().app.secret_key
    key = key if isinstance(key, bytes) else str(key or 'hirelab').encode()
    return hmac.new(key, ('re:' + raw).encode(), hashlib.sha256).hexdigest()[:16]


def track_token(log_id):
    return '%d-%s' % (log_id, _sign(str(log_id)))


def parse_track(tok):
    try:
        lid, sig = str(tok).split('-', 1)
        lid = int(lid)
    except Exception:
        return None
    return lid if hmac.compare_digest(_sign(str(lid)), sig) else None


# ── who gets mailed ────────────────────────────────────────────────────────
def build_people(conn, oid, settings=None, now=None, only_email=None):
    """One entry per e-mail address with its state:
       due | upcoming | excluded (with reason). Latest job first.

    only_email (Speed, Oct 2026): build just that one person. The candidate
    profile asked for one person's status but built the whole database
    (~1.5 s on 30k candidates). The SQL below only narrows what is read; the
    grouping and every rule stay exactly the same, so the entry is identical."""
    settings = settings or get_settings(conn, oid)
    now = now or now_ist()
    interval = datetime.timedelta(days=int(settings.get('interval_days') or 180))
    try:
        pool = int(_core().get_or_create_central_mandate() or 0)
    except Exception:
        pool = 0
    mandates = {m['id']: m for m in _rows(conn, 'SELECT id, role, client, status FROM mandates WHERE owner_id=?', (oid,))}
    one = (only_email or '').strip().lower()
    cands = _rows(conn, "SELECT id, name, email, mandate_id, stage, created_at, update_submitted_at, "
                        "COALESCE(do_not_email,0) AS dne, COALESCE(reengage_off,0) AS off "
                        "FROM candidates WHERE owner_id=? AND TRIM(COALESCE(email,''))<>''"
                        + (" AND instr(LOWER(email), ?) > 0" if one else ""), (oid, one) if one else (oid,))
    sent = {}
    for r in _rows(conn, "SELECT email, MAX(sent_at) AS last FROM reengage_log WHERE company_id=? AND status='sent' "
                         + ("AND email=? " if one else "") + "GROUP BY email", (oid, one) if one else (oid,)):
        sent[r['email']] = r['last']
    upd_log = {}      # Wave 2: durable update-profile log (update_submitted_at is cleared when its task is dismissed)
    try:
        for r in _rows(conn, "SELECT LOWER(TRIM(email)) AS em, MAX(created_at) AS last FROM talent_updates "
                             "WHERE company_id=? AND email<>'' " + ("AND LOWER(TRIM(email))=? " if one else "")
                             + "GROUP BY em", (oid, one) if one else (oid,)):
            upd_log[r['em']] = r['last']
    except Exception:
        pass
    mg_ids, mg_ems, mg_lab = set(), set(), {}       # per-job "automated e-mails OFF" (modules/mail_guard.py)
    try:
        from modules import mail_guard as _mg
        mg_ids, mg_ems, mg_lab = _mg.blocked(conn, oid)
    except Exception:
        pass
    groups = {}
    for c in cands:
        em = c['email'].strip().lower()
        if '@' not in em or '.' not in em.split('@')[-1]:
            continue
        if one and em != one:
            continue        # the SQL narrowing above is a superset; keep exact matches only
        groups.setdefault(em, []).append(c)
    out = []
    for em, rows in groups.items():
        rows.sort(key=lambda c: (str(c['created_at'] or ''), c['id']), reverse=True)
        latest = rows[0]
        m = mandates.get(int(latest['mandate_id'] or 0))
        is_pool = (not m) or int(latest['mandate_id'] or 0) == pool or (m and (m['status'] or '') == 'central')
        p = {'email': em, 'candidate_id': latest['id'], 'name': latest['name'] or '', 'ids': [c['id'] for c in rows],
             'kind': 'pool' if is_pool else 'job', 'mandate_id': 0 if is_pool else m['id'],
             'role': '' if is_pool else (m['role'] or ''), 'client': '' if is_pool else (m['client'] or ''),
             'considered_on': str(latest['created_at'] or ''), 'stage': latest['stage'] or '',
             'last_sent': sent.get(em, ''), 'off': any(c['off'] for c in rows)}
        anchors = [d for d in (_dt(latest['created_at']), _dt(sent.get(em)), _dt(upd_log.get(em)),
                               max([_dt(c['update_submitted_at']) or datetime.datetime.min for c in rows])) if d]
        anchor = max(anchors) if anchors else None
        p['anchor'] = anchor.strftime('%Y-%m-%d') if anchor else ''
        p['due_on'] = (anchor + interval).strftime('%Y-%m-%d') if anchor else ''
        if any(c['dne'] for c in rows):
            p['state'], p['reason'] = 'excluded', 'Unsubscribed'
        elif p['off']:
            p['state'], p['reason'] = 'excluded', 'Switched off by you'
        elif em in mg_ems or any(c['id'] in mg_ids for c in rows):
            p['state'], p['reason'] = 'excluded', 'Job e-mails off (%s)' % mg_lab.get(em, 'job')
        elif any((c['stage'] or '').strip().lower() in ACTIVE_STAGES for c in rows):
            live = [c['stage'] for c in rows if (c['stage'] or '').strip().lower() in ACTIVE_STAGES][0]
            p['state'], p['reason'] = 'excluded', 'Live in a pipeline (%s)' % live
        elif not anchor:
            p['state'], p['reason'] = 'excluded', 'No date on record'
        elif anchor + interval <= now:
            p['state'], p['reason'] = 'due', ''
        else:
            p['state'], p['reason'] = 'upcoming', ''
        out.append(p)
    out.sort(key=lambda p: p['due_on'] or '9999')
    return out


# ── the e-mail ─────────────────────────────────────────────────────────────
# ── editable e-mail text (empty = built-in default) ────────────────────────
_ASK = ('We already have most of your details with us — we only need a quick refresh with your latest '
        'information (current role and company, CTC, notice period, location and an updated CV). '
        'It takes about 2 minutes, and it helps us put the right opportunity in front of you.')
DEFAULT_TEXT = {
    'job': {'subject': '{first_name}, a quick update on your profile with HireLab',
            'body': 'On {date}, HireLab considered your profile for the {role} position{with_client}. Unfortunately, '
                    'due to some reasons, things did not move forward at that time.\n\n'
                    'HireLab is a recruitment agency specialising in {domains} hiring, and we regularly work on new '
                    'openings in these areas.\n\n' + _ASK},
    'pool': {'subject': '{first_name}, is your HireLab profile up to date?',
             'body': 'Your profile has been part of the HireLab talent network{since_date}.\n\n'
                     'HireLab is a recruitment agency that hires for {domains_detail} companies — engineering, project, '
                     'O&M, sales and leadership roles.\n\n' + _ASK},
    'reminder': {'subject': '{first_name}, 2 minutes to refresh your HireLab profile',
                 'body': 'A quick reminder about our note last week — we are keeping your profile ready for new '
                         '{domains} openings, and a refreshed CV helps us put the right role in front of you.\n\n'
                         'Just confirm your current role, CTC, notice period and location, and upload your latest CV.'},
}
TEXT_KINDS = ('job', 'pool', 'reminder')
PLACEHOLDERS = [('{first_name}', 'Asha'), ('{role}', 'job they were considered for'), ('{client}', 'that client'),
                ('{with_client}', '" with Azure Power" or nothing'), ('{date}', '12 April 2026'),
                ('{since_date}', '" since 12 April 2026" or nothing'), ('{domains}', 'your four sectors, one line'),
                ('{domains_detail}', 'your sectors with example roles')]
_PH_RX = re.compile(r'\{([a-z_]+)\}')


def fill(text, values):
    return _PH_RX.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), str(text or ''))


def get_text(conn, oid, kind):
    t = dict(DEFAULT_TEXT[kind])
    try:
        r = conn.execute('SELECT subject, body FROM reengage_text WHERE company_id=? AND kind=?', (oid, kind)).fetchone()
        if r:
            if (r['subject'] or '').strip():
                t['subject'] = r['subject']
            if (r['body'] or '').strip():
                t['body'] = r['body']
    except Exception:
        pass
    return t


def _values(p):
    when = nice_date(p.get('considered_on'))
    return {'first_name': first_name(p.get('name')), 'role': p.get('role') or '', 'client': p.get('client') or '',
            'with_client': (' with %s' % p['client']) if p.get('client') else '', 'date': when or 'an earlier date',
            'since_date': (' since %s' % when) if when else '', 'domains': DOMAINS_LINE, 'domains_detail': DOMAINS_DETAIL}


def _paras(text):
    return [x.strip() for x in re.split(r'\n\s*\n', str(text or '')) if x.strip()]


def kind_of(p):
    return 'job' if (p.get('kind') == 'job' and p.get('role')) else 'pool'


def _render(fn, subject, paras, link, unsub, pixel, has_signature, validity=True):
    sign_off = 'Warm regards,' if has_signature else 'Warm regards,\nTeam HireLab'
    note = '\n\nThis link is personal to you and stays valid for 14 days.' if validity else ''
    plain = ('Hi %s,\n\n%s\n\nUpdate your profile: %s%s\n\n%s\n\n---\nYou are receiving this because your profile is '
             'with HireLab. To stop these e-mails: %s' % (fn, '\n\n'.join(paras), link, note, sign_off, unsub))
    e = html.escape
    body = ('<p>Hi %s,</p>%s'
            '<p><a href="%s" style="display:inline-block;background:#0F6E56;color:#fff;padding:11px 22px;'
            'border-radius:8px;text-decoration:none;font-weight:600">Update my profile</a></p>'
            '%s<p>%s</p>'
            '<p style="font-size:11px;color:#888;margin-top:22px">You are receiving this because your profile is '
            'with HireLab. <a href="%s" style="color:#888">Unsubscribe</a></p>%s'
            % (e(fn), ''.join('<p>%s</p>' % e(x).replace('\n', '<br>') for x in paras), e(link),
               '<p style="font-size:12px;color:#666">This link is personal to you and stays valid for 14 days.</p>' if validity else '',
               e(sign_off).replace('\n', '<br>'), e(unsub),
               ('<img src="%s" width="1" height="1" alt="" style="display:block">' % e(pixel)) if pixel else ''))
    return subject, plain, body


def compose(p, link, unsub, pixel='', has_signature=True, text=None):
    """(subject, plain, html) for one person. `text` = {'subject','body'} (editable); default if None."""
    t = text or DEFAULT_TEXT[kind_of(p)]
    v = _values(p)
    subject = re.sub(r'\s+', ' ', fill(t['subject'], v)).strip()[:180]
    return _render(v['first_name'], subject, _paras(fill(t['body'], v)), link, unsub, pixel, has_signature)


def _base():
    core = _core()
    try:
        return core._camp_base_url().rstrip('/')
    except Exception:
        return (os.environ.get('PUBLIC_BASE_URL', '') or 'https://hirelabscreener.onrender.com').rstrip('/')


def _ensure_link(conn, cid):
    """Personal update-profile token on this candidate row (kept if one exists),
    re-armed for 14 days from now."""
    import secrets
    r = conn.execute('SELECT update_token FROM candidates WHERE id=?', (cid,)).fetchone()
    tok = (r['update_token'] if r else '') or secrets.token_urlsafe(24)
    conn.execute('UPDATE candidates SET update_token=?, update_requested_at=? WHERE id=?', (tok, ts(), cid))
    conn.commit()
    return tok


def _has_signature():
    try:
        return bool((_core()._build_signature() or ('', ''))[0])
    except Exception:
        return False


def send_one(conn, oid, p):
    """Send the e-mail to one due person. Returns (ok, error). Never raises."""
    core = _core()
    cur = conn.execute('INSERT OR IGNORE INTO reengage_log (company_id, candidate_id, email, cycle_key, kind, mandate_id, '
                       'role, client, considered_on, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (oid, p['candidate_id'], p['email'], p['anchor'], p['kind'], p['mandate_id'], p['role'],
                        p['client'], p['considered_on'], 'sending', ts()))
    conn.commit()
    if cur.rowcount != 1:
        return False, 'already handled'
    lid = cur.lastrowid
    try:
        tok = _ensure_link(conn, p['candidate_id'])
        base = _base()
        tt = track_token(lid)
        link = '%s/re/c/%s' % (base, tt)
        unsub = '%s/u/%s' % (base, core._unsub_token(oid, p['candidate_id']))
        subject, plain, body = compose(p, link, unsub, '%s/re/o/%s.gif' % (base, tt), _has_signature(),
                                       get_text(conn, oid, kind_of(p)))
        ok, err, _mid, _tid = core.email_service_send(
            p['email'], subject, plain, body_html=body, candidate_id=p['candidate_id'],
            extra_headers={'List-Unsubscribe': '<%s>' % unsub, 'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click'})
    except Exception as exc:
        ok, err = False, '%s: %s' % (type(exc).__name__, exc)
    if ok:
        conn.execute("UPDATE reengage_log SET status='sent', sent_at=? WHERE id=?", (ts(), lid))
        try:      # journey entry on the same connection (a second writer would wait on this one)
            conn.execute('INSERT INTO candidate_events (candidate_id,event_type,detail,created_at) VALUES (?,?,?,?)',
                         (p['candidate_id'], 'note', 'Re-engagement e-mail sent to %s' % p['email'], ts()))
        except Exception:
            pass
    else:
        conn.execute("UPDATE reengage_log SET status='failed', error=? WHERE id=?", (str(err or '')[:300], lid))
    conn.commit()
    return ok, err


def sent_today(conn, oid):
    day = now_ist().strftime('%Y-%m-%d')
    return conn.execute("SELECT COUNT(*) FROM reengage_log WHERE company_id=? AND status='sent' AND sent_at LIKE ?",
                        (oid, day + '%')).fetchone()[0]


def run_company(conn, oid, force=False, limit=None):
    """One sending pass for one company. Returns how many were sent."""
    s = get_settings(conn, oid)
    if not s['enabled'] and not force:
        return 0
    now = now_ist()
    if not force and (now.weekday() == 6 or not (int(s['start_hour']) <= now.hour < int(s['end_hour']))):
        return 0
    left = int(s['daily_cap']) - sent_today(conn, oid)
    if left <= 0:
        return 0
    if limit is None:
        end = now.replace(hour=int(s['end_hour']), minute=0, second=0, microsecond=0)
        ticks = max(1, int((end - now).total_seconds() // TICK_SECONDS))
        limit = min(MAX_PER_TICK, max(1, math.ceil(left / ticks)))
    limit = min(limit, left)
    due = [p for p in build_people(conn, oid, s, now) if p['state'] == 'due']
    try:      # frequency guard: someone who just got (or is about to get) a job-match mail waits
        from modules.jobmail import mailed_recently
        recent = mailed_recently(conn, oid)
        try:      # Wave 3: the newsletter already carries the CV ask — wait for its recipients too
            from modules.newsletter import mailed_recently as nl_recent
            recent |= nl_recent(conn, oid)
        except Exception:
            pass
        due = [p for p in due if p['email'] not in recent]
    except Exception:
        pass
    n = 0
    for p in due:
        if n >= limit:
            break
        ok, err = send_one(conn, oid, p)
        if ok:
            n += 1
        elif err and err != 'already handled' and ('not configured' in err.lower() or 'auth' in err.lower()
                                                     or 'username and password' in err.lower()):
            conn.execute('UPDATE reengage_settings SET last_error=? WHERE company_id=?', (err[:300], oid))
            conn.commit()
            break
    if n < limit and int(s.get('reminder_days') or 0) > 0:
        try:
            n += run_reminders(conn, oid, s, limit - n, now)
        except Exception as exc:
            print('[reengage] reminders failed for %s: %s' % (oid, exc))
    if n:
        conn.execute("UPDATE reengage_settings SET last_error='' WHERE company_id=?", (oid,))
        conn.commit()
    return n


# ── Wave 2: one soft reminder when the profile was not updated ─────────────
def reminders_due(conn, oid, s=None, now=None):
    """First e-mails sent `reminder_days`..+14 days ago whose person has not updated
    since, is still mailable, and has had no reminder for that cycle."""
    s = s or get_settings(conn, oid)
    now = now or now_ist()
    rd = int(s.get('reminder_days') or 0)
    if rd <= 0:
        return []
    hi = (now - datetime.timedelta(days=rd)).strftime('%Y-%m-%dT%H:%M:%S')
    lo = (now - datetime.timedelta(days=rd + REMINDER_WINDOW_DAYS)).strftime('%Y-%m-%dT%H:%M:%S')
    logs = _rows(conn, "SELECT * FROM reengage_log WHERE company_id=? AND status='sent' AND kind IN ('job','pool') "
                       "AND sent_at<=? AND sent_at>=? ORDER BY sent_at", (oid, hi, lo))
    if not logs:
        return []
    people = {p['email']: p for p in build_people(conn, oid, s, now)}
    done = {r['cycle_key'] for r in _rows(conn, "SELECT cycle_key FROM reengage_log WHERE company_id=? AND kind='reminder'", (oid,))}
    later = {}
    for r in _rows(conn, "SELECT email, MAX(sent_at) AS last FROM reengage_log WHERE company_id=? AND status='sent' "
                         "GROUP BY email", (oid,)):
        later[r['email']] = r['last']
    try:
        from modules.jobmail import mailed_recently
        recent = mailed_recently(conn, oid)
    except Exception:
        recent = set()
    try:
        from modules.newsletter import mailed_recently as nl_recent
        recent |= nl_recent(conn, oid)
    except Exception:
        pass
    out = []
    for l in logs:
        em = l['email']
        p = people.get(em)
        if not p or p['state'] == 'excluded' or em in recent:
            continue
        if (l['cycle_key'] + ':r') in done or str(later.get(em) or '') > str(l['sent_at']):
            continue                                         # reminded already / a newer mail went out
        # updated since the mail?  (anchor moves past the mail date when they update)
        if p['anchor'] and p['anchor'] > str(l['sent_at'])[:10]:
            continue
        rows = conn.execute("SELECT MAX(COALESCE(update_submitted_at,'')) FROM candidates WHERE owner_id=? AND LOWER(TRIM(email))=?",
                            (oid, em)).fetchone()[0]
        if rows and str(rows) > str(l['sent_at']):
            continue
        try:
            tu = conn.execute("SELECT MAX(created_at) FROM talent_updates WHERE company_id=? AND email=?", (oid, em)).fetchone()[0]
        except Exception:
            tu = None
        if tu and str(tu) > str(l['sent_at']):
            continue
        out.append(dict(p, base_log=l))
    return out


def compose_reminder(p, link, unsub, pixel='', has_signature=True, text=None):
    t = text or DEFAULT_TEXT['reminder']
    v = _values(p)
    subject = re.sub(r'\s+', ' ', fill(t['subject'], v)).strip()[:180]
    return _render(v['first_name'], subject, _paras(fill(t['body'], v)), link, unsub, pixel, has_signature, validity=False)


def run_reminders(conn, oid, s, limit, now=None):
    core = _core()
    n = 0
    for p in reminders_due(conn, oid, s, now):
        if n >= limit:
            break
        l = p['base_log']
        cur = conn.execute('INSERT OR IGNORE INTO reengage_log (company_id, candidate_id, email, cycle_key, kind, mandate_id, '
                           'role, client, considered_on, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (oid, p['candidate_id'], p['email'], l['cycle_key'] + ':r', 'reminder', p['mandate_id'],
                            p['role'], p['client'], p['considered_on'], 'sending', ts()))
        conn.commit()
        if cur.rowcount != 1:
            continue
        lid = cur.lastrowid
        try:
            _ensure_link(conn, p['candidate_id'])
            base = _base()
            tt = track_token(lid)
            unsub = '%s/u/%s' % (base, core._unsub_token(oid, p['candidate_id']))
            subject, plain, body = compose_reminder(p, '%s/re/c/%s' % (base, tt), unsub, '%s/re/o/%s.gif' % (base, tt),
                                                    _has_signature(), get_text(conn, oid, 'reminder'))
            ok, err, _m, _t = core.email_service_send(
                p['email'], subject, plain, body_html=body, candidate_id=p['candidate_id'],
                extra_headers={'List-Unsubscribe': '<%s>' % unsub, 'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click'})
        except Exception as exc:
            ok, err = False, '%s: %s' % (type(exc).__name__, exc)
        if ok:
            conn.execute("UPDATE reengage_log SET status='sent', sent_at=? WHERE id=?", (ts(), lid))
            try:
                conn.execute('INSERT INTO candidate_events (candidate_id,event_type,detail,created_at) VALUES (?,?,?,?)',
                             (p['candidate_id'], 'note', 'Re-engagement reminder sent to %s' % p['email'], ts()))
            except Exception:
                pass
            n += 1
        else:
            conn.execute("UPDATE reengage_log SET status='failed', error=? WHERE id=?", (str(err or '')[:300], lid))
        conn.commit()
    return n


def tick():
    """One pass over every company that has the campaign switched on."""
    core = _core()
    conn = get_db()
    try:
        cos = [r[0] for r in conn.execute('SELECT company_id FROM reengage_settings WHERE enabled=1').fetchall()]
        stamp = ts()
        for oid in cos:
            # claim this company's slot so two workers never send in the same window
            cur = conn.execute('UPDATE reengage_settings SET tick_at=? WHERE company_id=? AND (tick_at IS NULL OR tick_at<?)',
                               (stamp, oid, (now_ist() - datetime.timedelta(seconds=TICK_SECONDS - 30)).strftime('%Y-%m-%dT%H:%M:%S')))
            conn.commit()
            if cur.rowcount != 1:
                continue
            core._bg_tenant_set(oid)
            try:
                run_company(conn, oid)
            except Exception as e:
                print('[reengage] company %s failed: %s' % (oid, e))
            finally:
                core._bg_tenant_clear()
    finally:
        conn.close()


def _loop():
    time.sleep(60)
    while True:
        try:
            tick()
        except Exception as e:
            print('[reengage] loop error: %s' % e)
        time.sleep(TICK_SECONDS)


_started = False


def start_worker():
    global _started
    if _started or 'pytest' in sys.modules or os.environ.get('HIRELAB_NO_WORKERS'):
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
    print('[reengage] background thread started')


start_worker()


# ── API (company admins) ───────────────────────────────────────────────────
def _admin():
    if not is_company_admin():
        return jsonify({'ok': False, 'error': 'Only an admin can manage the re-engagement campaign.'}), 403
    return None


def _person_stats(conn, oid):
    logs = _rows(conn, 'SELECT l.*, c.update_submitted_at FROM reengage_log l LEFT JOIN candidates c ON c.id=l.candidate_id '
                       'WHERE l.company_id=?', (oid,))
    sent = [l for l in logs if l['status'] == 'sent']
    updated = [l for l in sent if l['update_submitted_at'] and str(l['update_submitted_at']) > str(l['sent_at'])]
    unsub = conn.execute("SELECT COUNT(*) FROM candidates c WHERE c.owner_id=? AND COALESCE(c.do_not_email,0)=1 AND "
                         "EXISTS (SELECT 1 FROM reengage_log l WHERE l.company_id=c.owner_id AND l.candidate_id=c.id "
                         "AND l.status='sent' AND COALESCE(c.unsub_at,'')>=l.sent_at)", (oid,)).fetchone()[0]
    return logs, sent, updated, unsub


@bp.route('/summary', methods=['GET'])
@login_required
def summary():
    e = _admin()
    if e:
        return e
    oid = effective_company_id()
    conn = get_db()
    try:
        s = get_settings(conn, oid)
        people = build_people(conn, oid, s)
        logs, sent, updated, unsub = _person_stats(conn, oid)
        today = now_ist().date()
        soon = (today + datetime.timedelta(days=30)).isoformat()
        days = [(today - datetime.timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
        per_day = {d: 0 for d in days}
        for l in sent:
            d = str(l['sent_at'])[:10]
            if d in per_day:
                per_day[d] += 1
        core = _core()
        try:
            sender = core._smtp_params()[0]
        except Exception:
            sender = ''
        k = {
            'due': sum(1 for p in people if p['state'] == 'due'),
            'upcoming_30': sum(1 for p in people if p['state'] == 'upcoming' and p['due_on'] <= soon),
            'upcoming': sum(1 for p in people if p['state'] == 'upcoming'),
            'excluded': sum(1 for p in people if p['state'] == 'excluded'),
            'switched_off': sum(1 for p in people if p.get('reason') == 'Switched off by you'),
            'sent_today': per_day.get(today.isoformat(), 0),
            'sent_total': len(sent),
            'opened': sum(1 for l in sent if l['opened_at']),
            'clicked': sum(1 for l in sent if l['clicked_at']),
            'updated': len(updated),
            'unsubscribed': unsub,
            'failed': sum(1 for l in logs if l['status'] == 'failed'),
            'people': len(people),
            'reminders': sum(1 for l in sent if l.get('kind') == 'reminder'),
            'reminders_due': len(reminders_due(conn, oid, s)) if s.get('reminder_days') else 0,
        }
        days_to_clear = math.ceil(k['due'] / max(1, int(s['daily_cap']))) if k['due'] else 0
        return jsonify({'ok': True, 'settings': s, 'kpis': k, 'per_day': [{'day': d, 'sent': per_day[d]} for d in days],
                        'sender': sender, 'days_to_clear': days_to_clear})
    finally:
        conn.close()


@bp.route('/people', methods=['GET'])
@login_required
def people_list():
    e = _admin()
    if e:
        return e
    oid = effective_company_id()
    tab = request.args.get('tab', 'due')
    q = (request.args.get('q') or '').strip().lower()
    conn = get_db()
    try:
        if tab in ('sent', 'failed'):
            rows = _rows(conn, "SELECT l.*, c.name, c.update_submitted_at FROM reengage_log l LEFT JOIN candidates c "
                               "ON c.id=l.candidate_id WHERE l.company_id=? AND l.status=? ORDER BY l.id DESC LIMIT 1000",
                         (oid, tab))
            items = [{'candidate_id': r['candidate_id'], 'name': r['name'] or '', 'email': r['email'],
                      'role': r['role'], 'client': r['client'], 'kind': r['kind'], 'mandate_id': r['mandate_id'],
                      'sent_at': r['sent_at'], 'opened': bool(r['opened_at']), 'clicked': bool(r['clicked_at']),
                      'updated': bool(r['update_submitted_at'] and str(r['update_submitted_at']) > str(r['sent_at'] or 'z')),
                      'error': r['error']} for r in rows]
        else:
            items = [p for p in build_people(conn, oid) if p['state'] == tab]
            if tab == 'excluded':
                items.sort(key=lambda p: p['reason'])
            items = [{k: p[k] for k in ('candidate_id', 'name', 'email', 'role', 'client', 'kind', 'mandate_id',
                                        'considered_on', 'due_on', 'last_sent', 'reason', 'off', 'stage')} for p in items]
        if q:
            items = [i for i in items if q in (' '.join(str(v) for v in i.values())).lower()]
        return jsonify({'ok': True, 'items': items[:500], 'total': len(items)})
    finally:
        conn.close()


@bp.route('/settings', methods=['PUT', 'POST'])
@login_required
def save_settings():
    e = _admin()
    if e:
        return e
    d = request.get_json(silent=True) or {}
    oid = effective_company_id()
    conn = get_db()
    try:
        s = get_settings(conn, oid)

        def num(k, lo, hi):
            try:
                return max(lo, min(hi, int(d.get(k, s[k]))))
            except (TypeError, ValueError):
                return s[k]
        enabled = 1 if d.get('enabled', s['enabled']) in (1, True, '1', 'true') else 0
        cap, interval = num('daily_cap', 1, 500), num('interval_days', 30, 730)
        reminder = num('reminder_days', 0, 30)
        sh, eh = num('start_hour', 0, 23), num('end_hour', 1, 24)
        if eh <= sh:
            return jsonify({'ok': False, 'error': 'End hour must be after start hour.'}), 400
        conn.execute('''INSERT INTO reengage_settings (company_id, enabled, daily_cap, interval_days, start_hour, end_hour,
                        updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?)
                        ON CONFLICT(company_id) DO UPDATE SET enabled=excluded.enabled, daily_cap=excluded.daily_cap,
                        interval_days=excluded.interval_days, start_hour=excluded.start_hour, end_hour=excluded.end_hour,
                        updated_by=excluded.updated_by, updated_at=excluded.updated_at''',
                     (oid, enabled, cap, interval, sh, eh, real_user_id(), ts()))
        conn.execute('UPDATE reengage_settings SET reminder_days=? WHERE company_id=?', (reminder, oid))
        conn.commit()
        return jsonify({'ok': True, 'settings': get_settings(conn, oid)})
    finally:
        conn.close()


def _person_for(conn, oid, cid):
    r = conn.execute('SELECT email FROM candidates WHERE id=? AND owner_id=?', (cid, oid)).fetchone()
    if not r or not (r['email'] or '').strip():
        return None
    em = r['email'].strip().lower()
    for p in build_people(conn, oid, only_email=em):
        if p['email'] == em:
            return p
    return None


def _sample(conn, oid, want, cid=None):
    p = _person_for(conn, oid, cid) if cid else None
    base_kind = 'job' if want in ('job', 'reminder') else 'pool'
    if not p:
        ppl = build_people(conn, oid)
        p = next((x for x in ppl if x['state'] == 'due' and x['kind'] == base_kind), None) \
            or next((x for x in ppl if x['kind'] == base_kind), None)
    return p or {'name': 'Asha Verma', 'kind': base_kind, 'role': 'Solar O&M Engineer', 'client': 'Azure Power',
                 'considered_on': '2026-04-12'}


def _compose_kind(conn, oid, want, p, link, unsub):
    if want == 'reminder':
        return compose_reminder(p, link, unsub, '', _has_signature(), get_text(conn, oid, 'reminder'))
    return compose(p, link, unsub, '', _has_signature(), get_text(conn, oid, kind_of(p)))


@bp.route('/text', methods=['GET'])
@login_required
def text_get():
    e = _admin()
    if e:
        return e
    oid = effective_company_id()
    conn = get_db()
    try:
        out = {}
        for k in TEXT_KINDS:
            t = get_text(conn, oid, k)
            out[k] = {'subject': t['subject'], 'body': t['body'], 'default_subject': DEFAULT_TEXT[k]['subject'],
                      'default_body': DEFAULT_TEXT[k]['body'],
                      'edited': t['subject'] != DEFAULT_TEXT[k]['subject'] or t['body'] != DEFAULT_TEXT[k]['body']}
        return jsonify({'ok': True, 'text': out, 'placeholders': [{'tag': a, 'means': b} for a, b in PLACEHOLDERS]})
    finally:
        conn.close()


@bp.route('/text', methods=['PUT', 'POST'])
@login_required
def text_save():
    e = _admin()
    if e:
        return e
    d = request.get_json(silent=True) or {}
    k = d.get('kind')
    if k not in TEXT_KINDS:
        return jsonify({'ok': False, 'error': 'Unknown e-mail'}), 400
    oid = effective_company_id()
    conn = get_db()
    try:
        if d.get('reset'):
            conn.execute('DELETE FROM reengage_text WHERE company_id=? AND kind=?', (oid, k))
        else:
            subj = str(d.get('subject') or '').strip()[:200]
            body = str(d.get('body') or '').strip()[:4000]
            if not subj or not body:
                return jsonify({'ok': False, 'error': 'Subject and text cannot be empty (use Reset for the original).'}), 400
            subj = '' if subj == DEFAULT_TEXT[k]['subject'] else subj
            body = '' if body == DEFAULT_TEXT[k]['body'] else body
            conn.execute('INSERT INTO reengage_text (company_id, kind, subject, body, updated_by, updated_at) VALUES (?,?,?,?,?,?) '
                         'ON CONFLICT(company_id, kind) DO UPDATE SET subject=excluded.subject, body=excluded.body, '
                         'updated_by=excluded.updated_by, updated_at=excluded.updated_at',
                         (oid, k, subj, body, real_user_id(), ts()))
        conn.commit()
        t = get_text(conn, oid, k)
        return jsonify({'ok': True, 'subject': t['subject'], 'body': t['body'],
                        'edited': t['subject'] != DEFAULT_TEXT[k]['subject'] or t['body'] != DEFAULT_TEXT[k]['body']})
    finally:
        conn.close()


@bp.route('/preview', methods=['GET'])
@login_required
def preview():
    e = _admin()
    if e:
        return e
    oid = effective_company_id()
    conn = get_db()
    try:
        cid = request.args.get('candidate_id', type=int)
        want = request.args.get('kind', 'job')
        p = _sample(conn, oid, want, cid)
        subject, plain, body = _compose_kind(conn, oid, want, p, _base() + '/update-profile?token=…', _base() + '/u/…')
        return jsonify({'ok': True, 'subject': subject, 'html': body, 'text': plain, 'to': p.get('email', ''),
                        'name': p.get('name', '')})
    finally:
        conn.close()


@bp.route('/test', methods=['POST'])
@login_required
def send_test():
    """Send the preview to the company mailbox itself (nothing is logged)."""
    e = _admin()
    if e:
        return e
    core = _core()
    oid = effective_company_id()
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        sender = core._smtp_params()[0]
        if not sender:
            return jsonify({'ok': False, 'error': 'Company mailbox is not set. Settings → Email Configuration.'}), 400
        want = d.get('kind', 'job')
        p = _sample(conn, oid, want)
        subject, plain, body = _compose_kind(conn, oid, want, p, _base() + '/update-profile?token=TEST', _base() + '/u/TEST')
        ok, err, _m, _t = core.email_service_send(sender, '[TEST] ' + subject, plain, body_html=body)
        return jsonify({'ok': bool(ok), 'error': err or '', 'to': sender})
    finally:
        conn.close()


@bp.route('/run-now', methods=['POST'])
@login_required
def run_now():
    """Send up to N due mails right now (still within today's cap)."""
    e = _admin()
    if e:
        return e
    oid = effective_company_id()
    n = max(1, min(20, int((request.get_json(silent=True) or {}).get('count', 5))))
    conn = get_db()
    try:
        sent = run_company(conn, oid, force=True, limit=n)
        s = get_settings(conn, oid)
        return jsonify({'ok': True, 'sent': sent, 'last_error': s.get('last_error', '')})
    finally:
        conn.close()


@bp.route('/toggle', methods=['POST'])
@login_required
def toggle():
    d = request.get_json(silent=True) or {}
    return _set_off(int(d.get('candidate_id') or 0), bool(d.get('off')))


def _set_off(cid, off):
    oid = effective_company_id()
    conn = get_db()
    try:
        r = conn.execute('SELECT email FROM candidates WHERE id=? AND owner_id=?', (cid, oid)).fetchone()
        if not r:
            return jsonify({'ok': False, 'error': 'Candidate not found'}), 404
        em = (r['email'] or '').strip().lower()
        if em:     # one person = one e-mail: switch every row of that person
            conn.execute("UPDATE candidates SET reengage_off=? WHERE owner_id=? AND LOWER(TRIM(email))=?", (1 if off else 0, oid, em))
        else:
            conn.execute('UPDATE candidates SET reengage_off=? WHERE id=?', (1 if off else 0, cid))
        conn.commit()
        return jsonify({'ok': True, 'off': off})
    finally:
        conn.close()


# Per-candidate status + switch, shown on the candidate profile.
# (Under /api/candidates/<id>/ so the recruiter access guard applies.)
cand_bp = Blueprint('reengage_cand', __name__)


@cand_bp.route('/api/candidates/<int:cid>/reengage', methods=['GET', 'POST'])
@login_required
def candidate_reengage(cid):
    if request.method == 'POST':
        return _set_off(cid, bool((request.get_json(silent=True) or {}).get('off')))
    oid = effective_company_id()
    conn = get_db()
    try:
        r = conn.execute('SELECT email, COALESCE(reengage_off,0) AS off FROM candidates WHERE id=? AND owner_id=?',
                         (cid, oid)).fetchone()
        if not r:
            return jsonify({'ok': False, 'error': 'Not found'}), 404
        p = _person_for(conn, oid, cid)
        s = get_settings(conn, oid)
        if not p:
            line = 'No e-mail address — cannot be mailed'
        elif p['state'] == 'excluded':
            line = p['reason']
        elif p['state'] == 'due':
            line = 'Due now — goes out in the next sending window' if s['enabled'] else 'Due now (campaign paused)'
        else:
            line = 'Next e-mail around %s' % nice_date(p['due_on'])
        if p and p['last_sent']:
            line += ' · last sent %s' % nice_date(p['last_sent'])
        return jsonify({'ok': True, 'off': bool(r['off']), 'line': line, 'has_email': bool(p),
                        'unsubscribed': bool(p and p['reason'] == 'Unsubscribed')})
    finally:
        conn.close()


# ── public: click + open tracking, and the dashboard page ──────────────────
_GIF = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,'
        b'\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')


@pages.route('/re/c/<tok>')
def track_click(tok):
    lid = parse_track(tok)
    base = _base()
    if not lid:
        return redirect(base + '/')
    conn = get_db()
    try:
        r = conn.execute('SELECT l.candidate_id, c.update_token FROM reengage_log l LEFT JOIN candidates c '
                         'ON c.id=l.candidate_id WHERE l.id=?', (lid,)).fetchone()
        if not r or not r['update_token']:
            return Response('<p style="font-family:sans-serif;margin:15vh auto;max-width:420px">This link is no longer '
                            'valid. Please reply to our e-mail and we will send you a new one.</p>', mimetype='text/html')
        conn.execute("UPDATE reengage_log SET clicked_at=? WHERE id=? AND COALESCE(clicked_at,'')=''", (ts(), lid))
        conn.execute("UPDATE reengage_log SET opened_at=? WHERE id=? AND COALESCE(opened_at,'')=''", (ts(), lid))
        # the update link is valid for 14 days from a click too
        conn.execute('UPDATE candidates SET update_requested_at=? WHERE id=?', (ts(), r['candidate_id']))
        conn.commit()
        return redirect('/update-profile?token=' + r['update_token'])
    finally:
        conn.close()


@pages.route('/re/o/<tok>.gif')
def track_open(tok):
    lid = parse_track(tok)
    if lid:
        conn = get_db()
        try:
            conn.execute("UPDATE reengage_log SET opened_at=? WHERE id=? AND COALESCE(opened_at,'')=''", (ts(), lid))
            conn.commit()
        finally:
            conn.close()
    return Response(_GIF, mimetype='image/gif', headers={'Cache-Control': 'no-store, max-age=0'})


@pages.route('/reengage')
def dashboard_page():
    core = _core()
    if not core.current_user():
        return redirect('/')
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reengage.html')
    with open(p, encoding='utf-8') as f:
        return Response(f.read(), mimetype='text/html')
