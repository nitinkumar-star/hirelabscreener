"""
RecruitOS — Gmail sync that looks after itself  (Nov 2026)

Before: the company inbox only synced in the background for agencies that had
the Email Agent switched on for some candidate, a recruiter's own Gmail only
when they pressed Sync, two syncs of the same mailbox could run at once, and a
revoked App Password failed silently with a raw IMAP error.

Now:
  * ONE pass per mailbox at a time (company box and each person's own Gmail);
    a second request while one runs simply waits for the next pass.
  * Background auto-sync of EVERY connected mailbox every few minutes
    (setting mail_autosync_minutes, default 5), with back-off when Gmail keeps
    failing, so a broken mailbox does not hammer Google.
  * Status per mailbox (last synced, last error, failures in a row) and plain
    messages that say what to do ("IMAP is off in Gmail → …").
  * When a mailbox stops working because of the password, the owner of that
    mailbox gets a bell notification once — "Gmail sync stopped, reconnect".
  * Guided connect: the e-mail + App Password are tested against Gmail (IMAP
    reading AND SMTP sending) BEFORE they are saved, then the first sync starts.

  POST /api/mail/connect    {box: 'me'|'company', email, app_password, display_name?}
  GET  /api/mail/status     the caller's mailboxes and how their sync is doing
  POST /api/mail/sync-now   {box}   start a sync in the background

Switch off the background part without a redeploy: env MAIL_AUTOSYNC=off.
"""

import os
import sys
import time
import datetime
import threading

from flask import Blueprint, request, jsonify, session

from modules.shared import get_db, ts, effective_company_id, is_company_admin, login_required, _core
from modules import register_migration

bp = Blueprint('mail_sync', __name__, url_prefix='/api/mail')

TICK = 60                      # seconds between scheduler ticks
BACKOFF_MIN = 30               # after 3 failures in a row
AUTH_BACKOFF_MIN = 360         # wrong / revoked password: wait for a reconnect (or 6 h)

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_RUNNING = set()


@register_migration
def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS mail_sync_status (
        company_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL DEFAULT 0,
        last_try_at TEXT DEFAULT '',
        last_ok_at TEXT DEFAULT '',
        last_error TEXT DEFAULT '',
        error_kind TEXT DEFAULT '',
        fails INTEGER DEFAULT 0,
        last_added INTEGER DEFAULT 0,
        total_added INTEGER DEFAULT 0,
        alerted_at TEXT DEFAULT '',
        PRIMARY KEY (company_id, user_id)
    )''')
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  Plain-language errors
# ══════════════════════════════════════════════════════════════════════════
MESSAGES = {
    'not_configured': 'Gmail is not connected yet.',
    'auth': ('Gmail did not accept the App Password (it may have been removed, or the Gmail password was '
             'changed). Create a new App Password and reconnect.'),
    'imap_off': ('IMAP is switched off in this Gmail. Open Gmail → Settings → See all settings → Forwarding and '
                 'POP/IMAP → Enable IMAP → Save changes, then reconnect.'),
    'blocked': ('Google blocked the sign-in as unusual. Open accounts.google.com on your computer, confirm it was '
                'you, then reconnect.'),
    'network': 'Could not reach Gmail just now. HireLab will try again automatically.',
}


def classify(err):
    """(kind, friendly message) for a raw sync / login error."""
    e = str(err or '').lower()
    if not e:
        return '', ''
    if 'not configured' in e or 'connect your gmail first' in e:
        kind = 'not_configured'
    elif 'imap' in e and ('disabled' in e or 'not enabled' in e or 'enable imap' in e and 'login failed' not in e):
        kind = 'imap_off'
    elif ('application-specific password' in e or 'authenticationfailed' in e or 'invalid credentials' in e
          or 'login failed' in e or 'username and password not accepted' in e or 'rejected the login' in e
          or 'not an app password' in e):
        kind = 'auth'
    elif 'web login required' in e or 'log in via your web browser' in e or 'webloginrequired' in e:
        kind = 'blocked'
    elif ('timed out' in e or 'timeout' in e or 'could not connect' in e or 'could not reach' in e
          or 'temporar' in e or 'eof' in e or 'connection' in e or 'network' in e):
        kind = 'network'
    else:
        return 'other', 'Sync problem: ' + str(err)[:180]
    return kind, MESSAGES[kind]


# ══════════════════════════════════════════════════════════════════════════
#  One pass per mailbox + status
# ══════════════════════════════════════════════════════════════════════════
def _lock(key):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def guarded(oid, uid, fn):
    """Run one sync pass `fn() -> (added, error)` for mailbox (oid, uid).
    Returns (added, friendly_error_or_None). A pass already running for the
    same mailbox -> (0, None) at once: that pass covers this request."""
    key = (int(oid or 0), int(uid or 0))
    lk = _lock(key)
    if not lk.acquire(blocking=False):
        return 0, None
    _RUNNING.add(key)
    try:
        try:
            added, err = fn()
        except Exception as e:
            added, err = 0, 'Sync error: %s' % str(e)[:160]
        kind, msg = classify(err) if err else ('', '')
        try:
            record(key[0], key[1], added or 0, kind, msg)
        except Exception as e:
            print('[mail-sync] status write failed: %s' % e)
        return added, (msg if err else None)
    finally:
        _RUNNING.discard(key)
        lk.release()


def record(oid, uid, added, kind, msg):
    now = ts()
    conn = get_db()
    try:
        cur = conn.execute('SELECT * FROM mail_sync_status WHERE company_id=? AND user_id=?', (oid, uid)).fetchone()
        cur = dict(cur) if cur else {'fails': 0, 'total_added': 0, 'alerted_at': ''}
        if not kind:
            conn.execute('INSERT INTO mail_sync_status (company_id,user_id,last_try_at,last_ok_at,last_error,error_kind,'
                         'fails,last_added,total_added,alerted_at) VALUES (?,?,?,?,"","",0,?,?,"") '
                         'ON CONFLICT(company_id,user_id) DO UPDATE SET last_try_at=excluded.last_try_at, '
                         'last_ok_at=excluded.last_ok_at, last_error="", error_kind="", fails=0, '
                         'last_added=excluded.last_added, total_added=mail_sync_status.total_added+excluded.last_added, '
                         'alerted_at=""', (oid, uid, now, now, added, added))
        else:
            fails = 0 if kind == 'not_configured' else int(cur.get('fails') or 0) + 1
            alert = kind in ('auth', 'imap_off', 'blocked') and not cur.get('alerted_at')
            conn.execute('INSERT INTO mail_sync_status (company_id,user_id,last_try_at,last_error,error_kind,fails,alerted_at) '
                         'VALUES (?,?,?,?,?,?,?) ON CONFLICT(company_id,user_id) DO UPDATE SET '
                         'last_try_at=excluded.last_try_at, last_error=excluded.last_error, '
                         'error_kind=excluded.error_kind, fails=excluded.fails, alerted_at=excluded.alerted_at',
                         (oid, uid, now, msg, kind, fails, now if alert else (cur.get('alerted_at') or '')))
            if alert:
                _alert(conn, oid, uid, msg)
        conn.commit()
    finally:
        conn.close()


def _alert(conn, oid, uid, msg):
    """Bell notification to whoever can fix the mailbox (its owner, or the agency admins)."""
    if uid:
        to = [uid]
        where = 'Settings → My Account → My Email → Reconnect Gmail'
    else:
        to = [r['id'] for r in conn.execute(
            "SELECT id FROM users WHERE company_id=? AND COALESCE(status,'approved')='approved' "
            "AND (COALESCE(is_company_admin,0)=1 OR role='admin')", (oid,)).fetchall()]
        where = 'Settings → Email Configuration → Reconnect Gmail'
    try:
        from modules.notifications import _create
    except Exception:
        return
    for u in to:
        try:
            _create(conn, oid, u, 'mail_sync', 'Gmail sync stopped', [msg, 'Fix: ' + where])
        except Exception:
            pass


def status_row(oid, uid):
    conn = get_db()
    try:
        r = conn.execute('SELECT * FROM mail_sync_status WHERE company_id=? AND user_id=?', (oid, uid)).fetchone()
        return dict(r) if r else {}
    finally:
        conn.close()


def reset(oid, uid):
    conn = get_db()
    try:
        conn.execute("UPDATE mail_sync_status SET fails=0, last_error='', error_kind='', alerted_at='' "
                     "WHERE company_id=? AND user_id=?", (oid, uid))
        conn.commit()
    finally:
        conn.close()


def _minutes():
    try:
        return max(2, min(int(_core().get_setting('mail_autosync_minutes', '5') or 5), 120))
    except Exception:
        return 5


def due(st, now=None, every=None):
    """Should the background sync run this mailbox now?"""
    now = now or datetime.datetime.fromisoformat(ts())
    every = every or _minutes()
    try:
        last = datetime.datetime.fromisoformat(st.get('last_try_at')) if st.get('last_try_at') else None
    except Exception:
        last = None
    if not last:
        return True
    fails = int(st.get('fails') or 0)
    wait = every
    if fails >= 3:
        wait = AUTH_BACKOFF_MIN if st.get('error_kind') in ('auth', 'imap_off', 'blocked') else BACKOFF_MIN
    return (now - last).total_seconds() >= wait * 60 - 5


# ══════════════════════════════════════════════════════════════════════════
#  Background auto-sync of every connected mailbox
# ══════════════════════════════════════════════════════════════════════════
def mailboxes():
    """[(company_id, user_id)] of every connected mailbox (0 = the company box)."""
    core = _core()
    out = []
    conn = get_db()
    try:
        cos = [r['id'] for r in conn.execute("SELECT id FROM companies WHERE COALESCE(status,'active')='active'")]
        users = [(r['company_id'], r['user_id']) for r in conn.execute(
            "SELECT m.company_id, m.user_id FROM user_mail_identity m JOIN users u ON u.id=m.user_id "
            "WHERE COALESCE(m.smtp_email,'')!='' AND COALESCE(m.smtp_app_password,'')!='' "
            "AND COALESCE(u.status,'approved')='approved'")]
    finally:
        conn.close()
    for oid in cos:
        try:
            with core.tenant_pin(oid), core.mail_sender(0):
                if core.get_setting('smtp_email', '') and core.get_setting('smtp_app_password', ''):
                    out.append((oid, 0))
        except Exception:
            pass
    out += [(int(c or 0), int(u)) for c, u in users if c]
    return out


def run_box(oid, uid):
    core = _core()
    if uid:
        return core._sync_user_mailbox(oid, uid)
    return core._sync_mailbox(oid)


def tick():
    core = _core()
    if (os.environ.get('MAIL_AUTOSYNC', '') or '').strip().lower() in ('off', '0', 'false', 'no'):
        return 0
    if (os.environ.get('EMAIL_AGENT_BG', '') or '').strip().lower() in ('off', '0', 'false', 'no'):
        return 0
    every = _minutes()
    n = 0
    for oid, uid in mailboxes():
        if not due(status_row(oid, uid), every=every):
            continue
        try:
            run_box(oid, uid)
            n += 1
        except Exception as e:
            print('[mail-sync] %s/%s: %s' % (oid, uid, e))
    return n


def _loop():
    time.sleep(45)
    while True:
        try:
            tick()
        except Exception as e:
            print('[mail-sync] loop error: %s' % e)
        time.sleep(TICK)


_started = False


def start_worker():
    global _started
    if _started or 'pytest' in sys.modules or os.environ.get('HIRELAB_NO_WORKERS'):
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
    print('[mail-sync] background auto-sync started')


start_worker()


# ══════════════════════════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════════════════════════
def _box(d=None):
    b = str((d or {}).get('box') or request.args.get('box') or 'me')
    return 'company' if b == 'company' else 'me'


def _imap_try(email_addr, pw):
    import imaplib
    host = _core()._imap_host_for(email_addr)
    try:
        M = imaplib.IMAP4_SSL(host, 993, timeout=15)
    except Exception as e:
        return False, 'Could not connect to %s: %s' % (host, e)
    try:
        M.login(email_addr, pw)
        try:
            M.select('INBOX', readonly=True)
        except Exception:
            pass
        return True, ''
    except Exception as e:
        return False, 'IMAP login failed: %s' % e
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _start_bg(oid, uid):
    def go():
        try:
            run_box(oid, uid)
        except Exception as e:
            print('[mail-sync] first sync: %s' % e)
    threading.Thread(target=go, daemon=True).start()


@bp.route('/connect', methods=['POST'])
@login_required
def connect():
    from modules import mail_identity as mi
    core = _core()
    d = request.get_json(silent=True) or {}
    box = _box(d)
    uid = int(session.get('user_id') or 0)
    oid = int(effective_company_id() or 0)
    if session.get('view_as_company'):
        return jsonify({'error': 'Switch back to your own account to connect a mailbox.'}), 400
    if box == 'company' and not is_company_admin():
        return jsonify({'error': 'Only the agency admin can connect the company mailbox.'}), 403
    em = mi._clean_email(d.get('email'))
    pw = mi._clean_pw(d.get('app_password'))
    if not mi._EMAIL_RX.match(em or ''):
        return jsonify({'error': 'Enter your full Gmail address.', 'step': 'email'}), 400
    if len(pw) < 16 and '@gmail' in em.lower():
        return jsonify({'error': 'An App Password has 16 letters (like abcd efgh ijkl mnop). Your normal Gmail password will not work.',
                        'step': 'password'}), 400
    checks = []
    ok, err = _imap_try(em, pw)
    if not ok:
        kind, msg = classify(err)
        return jsonify({'ok': False, 'step': 'imap', 'kind': kind, 'error': msg, 'detail': str(err)[:200],
                        'checks': [{'name': 'Read mail (IMAP)', 'ok': False, 'detail': msg}]}), 400
    checks.append({'name': 'Read mail (IMAP)', 'ok': True, 'detail': 'Connected to the inbox'})
    ok, err = mi._smtp_try(em, pw)
    if not ok:
        checks.append({'name': 'Send mail (SMTP)', 'ok': False, 'detail': err})
        return jsonify({'ok': False, 'step': 'smtp', 'error': err, 'checks': checks}), 400
    checks.append({'name': 'Send mail (SMTP)', 'ok': True, 'detail': 'Gmail accepted the login'})
    dn = (d.get('display_name') or '').strip()
    if box == 'me':
        conn = get_db()
        try:
            u = core.current_user() or {}
            e2 = mi._save_identity(conn, uid, u.get('company_id'),
                                   {'smtp_email': em, 'smtp_app_password': pw,
                                    'smtp_display_name': dn or (u.get('display_name') or '')}, uid)
            if e2:
                return jsonify({'error': e2}), 400
            conn.execute('UPDATE user_mail_identity SET verified_at=? WHERE user_id=?', (ts(), uid))
            conn.commit()
        finally:
            conn.close()
        sync_uid = uid
    else:
        if core.set_setting('smtp_email', em) is False or core.set_setting('smtp_app_password', pw) is False:
            return jsonify({'error': 'Could not save the company mailbox.'}), 400
        if dn:
            core.set_setting('smtp_display_name', dn)
        sync_uid = 0
    try:
        core.forget_current_user()
    except Exception:
        pass
    reset(oid, sync_uid)
    _start_bg(oid, sync_uid)
    checks.append({'name': 'First sync', 'ok': True, 'detail': 'Started — mail appears in the Email Box in a minute or two'})
    try:
        core.log_activity('mail_connect', 'Connected %s mailbox %s' % (box, em))
    except Exception:
        pass
    return jsonify({'ok': True, 'email': em, 'box': box, 'checks': checks, 'auto_minutes': _minutes()})


def _box_status(oid, uid, email):
    st = status_row(oid, uid)
    return {'box': 'me' if uid else 'company', 'email': email, 'connected': bool(email),
            'syncing': (oid, uid) in _RUNNING, 'last_ok_at': st.get('last_ok_at') or '',
            'last_try_at': st.get('last_try_at') or '', 'error': st.get('last_error') or '',
            'error_kind': st.get('error_kind') or '', 'fails': int(st.get('fails') or 0),
            'last_added': int(st.get('last_added') or 0)}


@bp.route('/status', methods=['GET'])
@login_required
def status():
    core = _core()
    uid = int(session.get('user_id') or 0)
    oid = int(effective_company_id() or 0)
    out = []
    me = core.mail_identity_status(uid) if uid else {}
    out.append(_box_status(oid, uid, me.get('email', '') if me.get('own_mailbox') else ''))
    if is_company_admin():
        try:
            with core.mail_sender(0):
                em = core.get_setting('smtp_email', '') if core.get_setting('smtp_app_password', '') else ''
        except Exception:
            em = ''
        out.append(_box_status(oid, 0, em))
    return jsonify({'ok': True, 'boxes': out, 'auto_minutes': _minutes(),
                    'auto_on': (os.environ.get('MAIL_AUTOSYNC', '') or '').strip().lower() not in ('off', '0', 'false', 'no')})


@bp.route('/sync-now', methods=['POST'])
@login_required
def sync_now():
    d = request.get_json(silent=True) or {}
    box = _box(d)
    uid = int(session.get('user_id') or 0)
    oid = int(effective_company_id() or 0)
    if box == 'company' and not is_company_admin():
        return jsonify({'error': 'Only the agency admin can sync the company mailbox.'}), 403
    key_uid = uid if box == 'me' else 0
    if (oid, key_uid) in _RUNNING:
        return jsonify({'ok': True, 'started': False, 'running': True})
    _start_bg(oid, key_uid)
    return jsonify({'ok': True, 'started': True})
