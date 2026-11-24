"""
"Connect with Google" for a user's own mailbox — no App Password (Nov 2026).

Before, every new user had to turn on 2-Step Verification, make a Gmail App
Password and paste it into the ATS. Now the user clicks "Connect with Google",
signs in, presses Allow, and the mailbox is connected.

HOW IT WORKS
  * Google OAuth (authorization code flow, offline access) with the scope
    https://mail.google.com/ — the one Gmail scope that allows SMTP and IMAP.
  * The refresh token is stored sealed (seal_secret) in user_mail_identity.
    smtp_app_password holds the marker "oauth2:google" instead of a password,
    so every place that already reads the user's mailbox settings keeps
    working; at login time server.py swaps the marker for a fresh access
    token (SMTP / IMAP AUTH XOAUTH2 — see _smtp_login_once / imap_login).

SETUP (once, by the Workspace admin) — see GOOGLE_CONNECT_SETUP.md
  Render environment:
    GOOGLE_OAUTH_CLIENT_ID      the OAuth client's Client ID
    GOOGLE_OAUTH_CLIENT_SECRET  its Client secret
    GOOGLE_OAUTH_DOMAIN         optional, e.g. hirelabtalent.com: only accounts
                                of that Workspace domain may connect
    PUBLIC_BASE_URL             optional; the callback is <base>/google-mail/callback
  Without the two keys the button is not shown and nothing changes.

Routes
  GET  /api/google-mail/status      is it set up? is my mailbox connected with Google?
  GET  /google-mail/connect         -> Google sign-in
  GET  /google-mail/callback        <- Google; saves the connection
  POST /api/google-mail/disconnect  removes the connection (and revokes the token)

ROLLBACK: remove 'google_mail' from modules/__init__.py. Users connected with
Google then have no working mailbox until they add an App Password again.
"""

import os
import json
import time
import base64
import secrets
import threading
from urllib.parse import urlencode

import requests
from flask import Blueprint, request, jsonify, redirect, session

from modules.shared import get_db, ts, _core, current_user, login_required
from modules import register_migration

bp = Blueprint('google_mail', __name__)

MARK = 'oauth2:google'
SCOPES = 'openid email https://mail.google.com/'
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
REVOKE_URL = 'https://oauth2.googleapis.com/revoke'

_tok_cache = {}            # uid -> (access_token, expires_at)
_tok_lock = threading.Lock()


class GoogleAuthError(Exception):
    """The Google connection cannot give an access token (revoked, expired, not set up)."""


@register_migration
def migrate(conn):
    for col, defn in (('oauth_provider', "TEXT DEFAULT ''"), ('oauth_refresh', "TEXT DEFAULT ''"),
                      ('oauth_connected_at', "TEXT DEFAULT ''")):
        try:
            conn.execute('ALTER TABLE user_mail_identity ADD COLUMN %s %s' % (col, defn))
        except Exception:
            pass


def _cfg():
    return ((os.environ.get('GOOGLE_OAUTH_CLIENT_ID') or '').strip(),
            (os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET') or '').strip())


def configured():
    cid, sec = _cfg()
    return bool(cid and sec)


def _domain():
    return (os.environ.get('GOOGLE_OAUTH_DOMAIN') or '').strip().lower().lstrip('@')


def _redirect_uri():
    base = (os.environ.get('PUBLIC_BASE_URL') or '').strip().rstrip('/')
    if not base:
        base = request.host_url.rstrip('/')
        if base.startswith('http://') and not base.startswith(('http://localhost', 'http://127.')):
            base = 'https://' + base[len('http://'):]        # behind Render's proxy
    return base + '/google-mail/callback'


def is_marker(pw):
    return str(pw or '').strip() == MARK


def access_token(uid):
    """A fresh access token for user `uid` (cached until a minute before it expires)."""
    uid = int(uid or 0)
    now = time.time()
    with _tok_lock:
        hit = _tok_cache.get(uid)
        if hit and hit[1] - 60 > now:
            return hit[0]
    cid, sec = _cfg()
    if not (cid and sec):
        raise GoogleAuthError('Connect with Google is not set up on this server (GOOGLE_OAUTH_CLIENT_ID / SECRET).')
    conn = get_db()
    try:
        row = conn.execute('SELECT oauth_refresh FROM user_mail_identity WHERE user_id=?', (uid,)).fetchone()
    finally:
        conn.close()
    refresh = _core().unseal_secret(row['oauth_refresh'] or '') if row else ''
    if not refresh:
        raise GoogleAuthError('This mailbox is not connected with Google. Settings -> My Account -> Email -> Connect with Google.')
    try:
        r = requests.post(TOKEN_URL, data={'client_id': cid, 'client_secret': sec, 'refresh_token': refresh,
                                           'grant_type': 'refresh_token'}, timeout=15)
        data = r.json()
    except Exception as e:
        raise GoogleAuthError('Could not reach Google: %s' % e)
    if r.status_code != 200 or not data.get('access_token'):
        err = data.get('error') or ('HTTP %s' % r.status_code)
        if err == 'invalid_grant':
            raise GoogleAuthError('The Google connection was removed or has expired. Connect with Google again in Settings.')
        raise GoogleAuthError('Google refused the connection: %s' % err)
    tok = data['access_token']
    with _tok_lock:
        _tok_cache[uid] = (tok, now + int(data.get('expires_in') or 3000))
    return tok


def xoauth2(email, uid):
    """The SASL XOAUTH2 string for SMTP / IMAP."""
    return 'user=%s\x01auth=Bearer %s\x01\x01' % (email, access_token(uid))


def _uid_for(email):
    """The user whose Google connection belongs to this mailbox address."""
    conn = get_db()
    try:
        r = conn.execute("SELECT user_id FROM user_mail_identity WHERE LOWER(smtp_email)=? AND oauth_provider='google' "
                         "AND COALESCE(oauth_refresh,'')!='' ORDER BY oauth_connected_at DESC LIMIT 1",
                         ((email or '').strip().lower(),)).fetchone()
        return int(r['user_id']) if r else 0
    finally:
        conn.close()


def xoauth2_for_email(email):
    uid = _uid_for(email)
    if not uid:
        raise GoogleAuthError('No Google connection found for %s. Connect with Google again in Settings.' % email)
    return xoauth2(email, uid)


# ══════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════
def _me():
    u = current_user() or {}
    return int(u.get('id') or 0), int(u.get('company_id') or 0)


@bp.route('/api/google-mail/status')
@login_required
def status():
    uid, _co = _me()
    out = {'ok': True, 'configured': configured(), 'domain': _domain(), 'connected': False, 'email': ''}
    conn = get_db()
    try:
        r = conn.execute('SELECT smtp_email, smtp_app_password, oauth_provider, oauth_refresh, oauth_connected_at '
                         'FROM user_mail_identity WHERE user_id=?', (uid,)).fetchone()
    except Exception:
        r = None
    finally:
        conn.close()
    if r and r['oauth_provider'] == 'google' and r['oauth_refresh'] \
            and is_marker(_core().unseal_secret(r['smtp_app_password'] or '')):
        out.update(connected=True, email=r['smtp_email'] or '', connected_at=r['oauth_connected_at'] or '')
    return jsonify(out)


@bp.route('/google-mail/connect')
@login_required
def connect():
    if not configured():
        return _done(False, 'Connect with Google is not set up yet. Ask your admin (GOOGLE_OAUTH_CLIENT_ID / SECRET on the server).')
    uid, _co = _me()
    state = secrets.token_urlsafe(24)
    session['gm_oauth'] = {'state': state, 'uid': uid, 'at': int(time.time())}
    u = current_user() or {}
    params = {'client_id': _cfg()[0], 'redirect_uri': _redirect_uri(), 'response_type': 'code',
              'scope': SCOPES, 'access_type': 'offline', 'prompt': 'consent', 'include_granted_scopes': 'true',
              'state': state}
    hint = (u.get('profile_email') or '').strip()
    if hint and '@' in hint:
        params['login_hint'] = hint
    if _domain():
        params['hd'] = _domain()
    return redirect(AUTH_URL + '?' + urlencode(params))


def _id_claims(id_token):
    """Claims of the id_token we just got from Google's token endpoint over TLS."""
    try:
        part = id_token.split('.')[1]
        part += '=' * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode()).decode())
    except Exception:
        return {}


def _done(ok, msg):
    """Back to the app (Settings -> My Account -> Email) with a message."""
    from urllib.parse import quote
    return redirect('/?gmail=%s&msg=%s' % ('connected' if ok else 'error', quote(msg[:300])))


@bp.route('/google-mail/callback')
@login_required
def callback():
    st = session.pop('gm_oauth', None) or {}
    uid, co = _me()
    if request.args.get('error'):
        return _done(False, 'Google sign-in was cancelled (%s).' % request.args.get('error'))
    if not st or st.get('state') != request.args.get('state') or st.get('uid') != uid \
            or time.time() - st.get('at', 0) > 900:
        return _done(False, 'The sign-in link expired. Please press Connect with Google again.')
    cid, sec = _cfg()
    try:
        r = requests.post(TOKEN_URL, data={'code': request.args.get('code', ''), 'client_id': cid, 'client_secret': sec,
                                           'redirect_uri': _redirect_uri(), 'grant_type': 'authorization_code'},
                          timeout=15)
        data = r.json()
    except Exception as e:
        return _done(False, 'Could not reach Google: %s' % e)
    if r.status_code != 200 or not data.get('access_token'):
        return _done(False, 'Google did not accept the sign-in (%s).' % (data.get('error_description') or data.get('error') or r.status_code))
    if 'https://mail.google.com/' not in (data.get('scope') or ''):
        return _done(False, 'Please tick "Read, compose, send and permanently delete all your email" on the Google screen.')
    refresh = data.get('refresh_token') or ''
    claims = _id_claims(data.get('id_token') or '')
    email = (claims.get('email') or '').strip().lower()
    if not email or not claims.get('email_verified', True):
        return _done(False, 'Google did not tell us the e-mail address of this account.')
    dom = _domain()
    if dom and email.split('@')[-1] != dom and (claims.get('hd') or '').lower() != dom:
        return _done(False, 'Only %s accounts can be connected here.' % dom)
    if not refresh:
        return _done(False, 'Google did not give a lasting connection. Remove the app at myaccount.google.com/permissions and connect again.')
    core = _core()
    u = current_user() or {}
    conn = get_db()
    try:
        old = conn.execute('SELECT smtp_display_name FROM user_mail_identity WHERE user_id=?', (uid,)).fetchone()
        name = (old['smtp_display_name'] if old else '') or (u.get('display_name') or u.get('username') or '')
        conn.execute('INSERT OR IGNORE INTO user_mail_identity (user_id, company_id) VALUES (?, ?)', (uid, co))
        conn.execute('UPDATE user_mail_identity SET company_id=?, smtp_email=?, smtp_app_password=?, smtp_display_name=?, '
                     "verified_at=?, updated_at=?, updated_by=?, oauth_provider='google', oauth_refresh=?, "
                     'oauth_connected_at=? WHERE user_id=?',
                     (co, email, core.seal_secret(MARK), name, ts(), ts(), uid, core.seal_secret(refresh), ts(), uid))
        conn.commit()
    finally:
        conn.close()
    with _tok_lock:
        _tok_cache[uid] = (data['access_token'], time.time() + int(data.get('expires_in') or 3000))
    return _done(True, 'Connected %s with Google. Your e-mails now go out from this mailbox.' % email)


@bp.route('/api/google-mail/disconnect', methods=['POST'])
@login_required
def disconnect():
    uid, _co = _me()
    conn = get_db()
    try:
        r = conn.execute('SELECT oauth_refresh FROM user_mail_identity WHERE user_id=?', (uid,)).fetchone()
        refresh = _core().unseal_secret(r['oauth_refresh'] or '') if r else ''
        conn.execute("UPDATE user_mail_identity SET smtp_app_password='', oauth_provider='', oauth_refresh='', "
                     "oauth_connected_at='', updated_at=? WHERE user_id=? AND oauth_provider='google'", (ts(), uid))
        conn.commit()
    finally:
        conn.close()
    with _tok_lock:
        _tok_cache.pop(uid, None)
    if refresh:
        try:
            requests.post(REVOKE_URL, data={'token': refresh}, timeout=10)
        except Exception:
            pass
    return jsonify({'ok': True})
