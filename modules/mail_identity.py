"""
RecruitOS — Per-recruiter mail identity  (Sep 2026)

PROBLEM
-------
Email settings (Gmail + App Password + signature) existed only at COMPANY
level and only an admin can change company settings. So a recruiter sub-account
could neither send from their own Gmail nor fix their name / email id: every
email left from the admin's mailbox, signed with the admin's name.

WHAT THIS MODULE ADDS (strictly additive)
-----------------------------------------
* table user_mail_identity — one row per user: own Gmail, App Password,
  sender display name.
* /api/me/mail           GET / PUT / DELETE  — the logged-in user's own mailbox
* /api/me/mail/test      POST                — SMTP login test (+ test mail to self)
* /api/team/<uid>        GET / PUT           — admin edits a team member
                                                (name, login id, profile, mailbox)

The sending side lives in server.py (get_setting → _mail_personal_value): the
sender is the logged-in user, the Email-Agent drafts go out as the mandate's
primary recruiter, and the company inbox sync always uses the company mailbox.
Nothing configured = exactly the old company behaviour.

ROLLBACK: remove 'mail_identity' from modules/__init__.py. The table can stay;
nothing reads it once the module and the server.py hook are gone.
"""

import re
import smtplib
from email.mime.text import MIMEText

from flask import Blueprint, request, jsonify, session

from modules.shared import (get_db, ts, _core, current_user, effective_company_id,
                            is_company_admin, login_required, log_activity)
from modules import register_migration

bp = Blueprint('mail_identity', __name__)

_EMAIL_RX = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_PROFILE_FIELDS = ('display_name', 'profile_phone', 'profile_designation', 'profile_email')


# ══════════════════════════════════════════════════════════════════════════
#  MIGRATION  (additive)
# ══════════════════════════════════════════════════════════════════════════
@register_migration
def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS user_mail_identity (
        user_id INTEGER PRIMARY KEY,
        company_id INTEGER DEFAULT 0,
        smtp_email TEXT DEFAULT '',
        smtp_app_password TEXT DEFAULT '',
        smtp_display_name TEXT DEFAULT '',
        verified_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        updated_by INTEGER DEFAULT 0
    )''')
    # users.profile_* are added by the core; be safe on very old databases.
    for col in ('profile_phone', 'profile_designation', 'profile_email'):
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT ''")
        except Exception:
            pass
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════
_INVISIBLE = re.compile(r'[\s\u00a0\u2000-\u200f\u2028\u2029\u202f\u205f\u2060\u3000\ufeff]+')


def _clean_pw(v):
    """Google shows App Passwords as 'abcd efgh ijkl mnop'; copying it often
    brings NON-BREAKING or zero-width spaces, which a plain replace(' ', '')
    misses — Gmail then drops the login. Remove every kind of whitespace."""
    return _INVISIBLE.sub('', str(v or ''))


def _clean_email(v):
    return _INVISIBLE.sub('', str(v or '')).strip()


def _smtp_host(addr):
    e = (addr or '').lower()
    if '@outlook' in e or '@hotmail' in e or '@live' in e:
        return 'smtp-mail.outlook.com', 587
    if '@yahoo' in e:
        return 'smtp.mail.yahoo.com', 587
    return 'smtp.gmail.com', 587       # Gmail + Google Workspace domains


def _row(conn, uid):
    r = conn.execute('SELECT * FROM user_mail_identity WHERE user_id=?', (uid,)).fetchone()
    return dict(r) if r else {}


def _public(conn, uid):
    """Identity as the UI sees it — the password is NEVER returned."""
    r = _row(conn, uid)
    u = conn.execute('SELECT id, username, display_name, profile_phone, profile_designation, '
                     'profile_email, role, is_company_admin FROM users WHERE id=?', (uid,)).fetchone()
    u = dict(u) if u else {}
    return {
        'smtp_email': r.get('smtp_email', '') or '',
        'has_password': bool((r.get('smtp_app_password') or '').strip()),
        'smtp_display_name': r.get('smtp_display_name', '') or '',
        'verified_at': r.get('verified_at', '') or '',
        'updated_at': r.get('updated_at', '') or '',
        'profile': {k: (u.get(k) or '') for k in _PROFILE_FIELDS},
        'username': u.get('username', ''),
    }


def _company_sender():
    """Address the company mailbox sends from (shown as the fallback)."""
    core = _core()
    try:
        with core.mail_sender(0):
            return core.get_setting('smtp_email', '') or ''
    except Exception:
        return ''


def _save_identity(conn, uid, company_id, d, actor):
    """Apply smtp_* fields from `d` to user `uid`. Blank password = keep.
    Returns error text or None."""
    cur = _row(conn, uid)
    em = _clean_email(d.get('smtp_email', cur.get('smtp_email', '')))
    if em and not _EMAIL_RX.match(em):
        return 'That email address does not look right.'
    pw_in = d.get('smtp_app_password')
    pw = cur.get('smtp_app_password', '') or ''
    if pw_in is not None and str(pw_in).strip():
        pw = _clean_pw(pw_in)
    if em.lower() != (cur.get('smtp_email', '') or '').lower() and (pw_in is None or not str(pw_in).strip()):
        # a new address needs its own app password — never reuse the old one
        pw = ''
    dn = (d.get('smtp_display_name', cur.get('smtp_display_name', '')) or '').strip()
    if not em:
        pw = ''
    verified = cur.get('verified_at', '') if (em == cur.get('smtp_email') and pw == cur.get('smtp_app_password')) else ''
    conn.execute('INSERT INTO user_mail_identity (user_id, company_id, smtp_email, smtp_app_password, '
                 'smtp_display_name, verified_at, updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?) '
                 'ON CONFLICT(user_id) DO UPDATE SET company_id=excluded.company_id, '
                 'smtp_email=excluded.smtp_email, smtp_app_password=excluded.smtp_app_password, '
                 'smtp_display_name=excluded.smtp_display_name, verified_at=excluded.verified_at, '
                 'updated_at=excluded.updated_at, updated_by=excluded.updated_by',
                 (uid, company_id or 0, em, pw, dn, verified or '', ts(), actor or 0))
    return None


def _save_profile(conn, uid, d):
    for f in _PROFILE_FIELDS:
        if f in d:
            v = (d.get(f) or '').strip()
            if f == 'display_name' and not v:
                continue            # never blank a person's name
            if f == 'profile_email' and v and not _EMAIL_RX.match(v):
                return 'Profile email does not look right.'
            conn.execute(f'UPDATE users SET {f}=? WHERE id=?', (v, uid))
    return None


def _smtp_try(email_addr, password, send_to=None, display_name=''):
    """Step-by-step check so the message says exactly WHAT failed."""
    core = _core()
    host, port = _smtp_host(email_addr)
    try:
        s, used = core.smtp_open(host, port, email_addr, password, timeout=20)
    except smtplib.SMTPAuthenticationError as e:
        raw = (e.smtp_error or b'').decode('utf-8', 'replace') if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
        return False, ('Gmail rejected the login (' + (raw[:90] or 'wrong password') + '). Use a 16-letter App Password '
                       '(Google Account → Security → 2-Step Verification → App passwords), not the normal Gmail password.')
    except core.SmtpStepError as e:
        if e.step.startswith('login'):
            return False, ('Gmail closed the connection during login (' + e.detail[:80] + '). This almost always means '
                           'the password is not an App Password, or 2-Step Verification is off on this Gmail. '
                           'Create a new App Password and paste it again.')
        return False, ('Could not reach Gmail from the server (' + e.step + ' — ' + e.detail[:80] + '). '
                       'The server network is blocking email ports 587/465.')
    except Exception as e:
        return False, 'Could not connect: ' + str(e)[:160]
    try:
        if send_to:
            msg = MIMEText('This is a test from HireLab ATS. Your emails to candidates '
                           'will now go out from this mailbox, and replies will land here.', 'plain', 'utf-8')
            msg['Subject'] = 'HireLab ATS — your email is connected'
            msg['From'] = f'{display_name} <{email_addr}>' if display_name else email_addr
            msg['To'] = send_to
            s.sendmail(email_addr, [send_to], msg.as_string())
        s.quit()
        return True, ''
    except Exception as e:
        return False, 'Logged in, but Gmail refused to send the test mail: ' + str(e)[:140]


# ══════════════════════════════════════════════════════════════════════════
#  MY MAILBOX (any logged-in user)
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/api/me/mail', methods=['GET'])
@login_required
def my_mail_get():
    uid = session.get('user_id')
    conn = get_db()
    out = _public(conn, uid)
    conn.close()
    out['company_sender'] = _company_sender()
    out['status'] = _core().mail_identity_status(uid)
    return jsonify({'ok': True, 'mail': out})


@bp.route('/api/me/mail', methods=['PUT', 'POST'])
@login_required
def my_mail_put():
    uid = session.get('user_id')
    if session.get('view_as_company'):
        return jsonify({'error': 'Switch back to your own account to change your mailbox.'}), 400
    d = request.json or {}
    u = current_user() or {}
    conn = get_db()
    err = _save_profile(conn, uid, d) if any(k in d for k in _PROFILE_FIELDS) else None
    if not err and any(k in d for k in ('smtp_email', 'smtp_app_password', 'smtp_display_name')):
        err = _save_identity(conn, uid, u.get('company_id'), d, uid)
    if err:
        conn.close()
        return jsonify({'error': err}), 400
    conn.commit()
    out = _public(conn, uid)
    conn.close()
    try:
        log_activity('mail_identity_update', 'Updated own email settings', 'user', uid)
    except Exception:
        pass
    return jsonify({'ok': True, 'mail': out})


@bp.route('/api/me/mail', methods=['DELETE'])
@login_required
def my_mail_delete():
    uid = session.get('user_id')
    conn = get_db()
    conn.execute('DELETE FROM user_mail_identity WHERE user_id=?', (uid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@bp.route('/api/me/mail/test', methods=['POST'])
@login_required
def my_mail_test():
    """Log in to SMTP with the typed (or saved) credentials and send a test
    mail to that same address. Marks the saved identity verified on success."""
    uid = session.get('user_id')
    d = request.json or {}
    conn = get_db()
    cur = _row(conn, uid)
    em = _clean_email(d.get('smtp_email') or cur.get('smtp_email'))
    pw = _clean_pw(d.get('smtp_app_password'))
    if not pw and em.lower() == (cur.get('smtp_email') or '').lower():
        pw = cur.get('smtp_app_password') or ''
    dn = (d.get('smtp_display_name') or cur.get('smtp_display_name') or '').strip()
    conn.close()      # never hold the database while talking to Gmail
    if not em or not pw:
        return jsonify({'error': 'Enter your Gmail address and App Password first.'}), 400
    ok, err = _smtp_try(em, pw, send_to=em, display_name=dn)
    if ok and em == cur.get('smtp_email') and pw == cur.get('smtp_app_password'):
        conn = get_db()
        conn.execute('UPDATE user_mail_identity SET verified_at=? WHERE user_id=?', (ts(), uid))
        conn.commit(); conn.close()
    if not ok:
        return jsonify({'ok': False, 'error': err}), 400
    return jsonify({'ok': True, 'message': 'Connected. A test email was sent to ' + em + '.'})


# ══════════════════════════════════════════════════════════════════════════
#  ADMIN: EDIT A TEAM MEMBER
# ══════════════════════════════════════════════════════════════════════════
def _can_manage(conn, uid):
    """Platform owner: anyone. Company admin: members of their own company."""
    me = current_user() or {}
    u = conn.execute('SELECT id, company_id, role FROM users WHERE id=?', (uid,)).fetchone()
    if not u:
        return None, ('User not found', 404)
    if me.get('role') == 'admin':
        return dict(u), None
    if is_company_admin() and u['company_id'] == effective_company_id():
        if u['role'] == 'admin':
            return None, ('Only the platform owner can edit this account', 403)
        return dict(u), None
    return None, ('Admin access required', 403)


@bp.route('/api/team/<int:uid>', methods=['GET'])
@login_required
def team_member_get(uid):
    conn = get_db()
    u, err = _can_manage(conn, uid)
    if err:
        conn.close(); return jsonify({'error': err[0]}), err[1]
    out = _public(conn, uid)
    conn.close()
    return jsonify({'ok': True, 'user': out})


@bp.route('/api/team/<int:uid>', methods=['PUT', 'POST'])
@login_required
def team_member_put(uid):
    d = request.json or {}
    conn = get_db()
    u, err = _can_manage(conn, uid)
    if err:
        conn.close(); return jsonify({'error': err[0]}), err[1]
    if 'username' in d:
        un = (d.get('username') or '').strip()
        if not un:
            conn.close(); return jsonify({'error': 'Login id cannot be empty'}), 400
        clash = conn.execute('SELECT id FROM users WHERE LOWER(username)=LOWER(?) AND id!=?', (un, uid)).fetchone()
        if clash:
            conn.close(); return jsonify({'error': 'That login id is already taken'}), 400
        conn.execute('UPDATE users SET username=? WHERE id=?', (un, uid))
    e2 = _save_profile(conn, uid, d)
    if not e2 and any(k in d for k in ('smtp_email', 'smtp_app_password', 'smtp_display_name')):
        e2 = _save_identity(conn, uid, u.get('company_id'), d, session.get('user_id'))
    if e2:
        conn.close(); return jsonify({'error': e2}), 400
    conn.commit()
    out = _public(conn, uid)
    conn.close()
    try:
        log_activity('edit_user', 'Edited team member #%d' % uid, 'user', uid)
    except Exception:
        pass
    return jsonify({'ok': True, 'user': out})
