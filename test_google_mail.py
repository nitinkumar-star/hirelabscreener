"""
"Connect with Google" for a user's own Gmail (no App Password).

Run:   pytest test_google_mail.py -q     (Google and SMTP are faked)
"""
import os
import sys
import json
import base64
import tempfile
from urllib.parse import urlparse, parse_qs

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CO, RIYA = 971, 9701


class _Resp(object):
    def __init__(self, code, data):
        self.status_code, self._d = code, data

    def json(self):
        return self._d


def _idtok(claims):
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'x.' + part + '.y'


@pytest.fixture(scope='module')
def env():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='gmail_oauth_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    c = server.get_db(); now = server.ts()
    c.execute("INSERT OR REPLACE INTO companies (id, name, status, created_at) VALUES (?,?,?,?)", (CO, 'G Co', 'active', now))
    c.execute("INSERT OR REPLACE INTO users (id, username, password_hash, display_name, role, status, company_id, "
              "is_company_admin, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
              (RIYA, 'g_riya', 'x', 'Riya Sharma', 'user', 'approved', CO, 0, now))
    c.commit(); c.close()
    return server


def _client(server):
    cl = server.app.test_client()
    with cl.session_transaction() as s:
        s['user_id'] = RIYA
    return cl


def test_not_set_up_means_no_button(env, monkeypatch):
    monkeypatch.delenv('GOOGLE_OAUTH_CLIENT_ID', raising=False)
    monkeypatch.delenv('GOOGLE_OAUTH_CLIENT_SECRET', raising=False)
    cl = _client(env)
    assert cl.get('/api/google-mail/status').get_json()['configured'] is False
    r = cl.get('/google-mail/connect')
    assert r.status_code == 302 and 'gmail=error' in r.headers['Location']


def test_connect_send_and_disconnect(env, monkeypatch):
    from modules import google_mail as gm
    monkeypatch.setenv('GOOGLE_OAUTH_CLIENT_ID', 'cid.apps.googleusercontent.com')
    monkeypatch.setenv('GOOGLE_OAUTH_CLIENT_SECRET', 'sec')
    monkeypatch.setenv('GOOGLE_OAUTH_DOMAIN', 'hirelabtalent.com')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://ats.example.com')
    cl = _client(env)

    r = cl.get('/google-mail/connect')
    loc = urlparse(r.headers['Location'])
    q = parse_qs(loc.query)
    assert loc.netloc == 'accounts.google.com'
    assert q['redirect_uri'] == ['https://ats.example.com/google-mail/callback']
    assert 'https://mail.google.com/' in q['scope'][0] and q['access_type'] == ['offline'] and q['hd'] == ['hirelabtalent.com']
    state = q['state'][0]

    calls = []

    def fake_post(url, data=None, timeout=None):
        calls.append((url, data))
        if data.get('grant_type') == 'authorization_code':
            return _Resp(200, {'access_token': 'AT1', 'expires_in': 3600, 'refresh_token': 'RT1',
                               'scope': 'openid https://mail.google.com/ email',
                               'id_token': _idtok({'email': 'riya@hirelabtalent.com', 'email_verified': True,
                                                   'hd': 'hirelabtalent.com'})})
        if data.get('grant_type') == 'refresh_token':
            return _Resp(200, {'access_token': 'AT2', 'expires_in': 3600})
        return _Resp(200, {})
    monkeypatch.setattr(gm.requests, 'post', fake_post)

    # a wrong state is refused
    r = cl.get('/google-mail/callback?code=abc&state=nope')
    assert 'gmail=error' in r.headers['Location']
    cl.get('/google-mail/connect')
    with cl.session_transaction() as s:
        state = s['gm_oauth']['state']
    r = cl.get('/google-mail/callback?code=abc&state=' + state)
    assert 'gmail=connected' in r.headers['Location'], r.headers['Location']

    st = cl.get('/api/google-mail/status').get_json()
    assert st['connected'] and st['email'] == 'riya@hirelabtalent.com'
    mail = cl.get('/api/me/mail').get_json()['mail']
    assert mail['google'] and mail['status']['own_mailbox'] and mail['smtp_email'] == 'riya@hirelabtalent.com'
    c = env.get_db()
    row = c.execute('SELECT * FROM user_mail_identity WHERE user_id=?', (RIYA,)).fetchone()
    c.close()
    assert 'RT1' not in row['oauth_refresh']                       # stored sealed
    assert env.unseal_secret(row['oauth_refresh']) == 'RT1'

    # SMTP logs in with XOAUTH2 (no App Password)
    class FakeSrv(object):
        def ehlo_or_helo_if_needed(self):
            pass

        def auth(self, mech, obj):
            self.mech, self.first = mech, obj()
    gm._tok_cache.clear()
    srv = FakeSrv()
    env._smtp_login_once(srv, 'riya@hirelabtalent.com', 'oauth2:google')
    assert srv.mech == 'XOAUTH2' and srv.first == 'user=riya@hirelabtalent.com\x01auth=Bearer AT2\x01\x01'

    class FakeImap(object):
        def authenticate(self, mech, fn):
            self.mech, self.blob = mech, fn(b'')
    M = FakeImap()
    env.imap_login(M, 'riya@hirelabtalent.com', 'oauth2:google')
    assert M.mech == 'XOAUTH2' and b'auth=Bearer AT2' in M.blob

    # a revoked connection says so clearly
    gm._tok_cache.clear()
    monkeypatch.setattr(gm.requests, 'post', lambda url, data=None, timeout=None: _Resp(400, {'error': 'invalid_grant'}))
    import smtplib
    with pytest.raises(smtplib.SMTPAuthenticationError) as ei:
        env._smtp_login_once(FakeSrv(), 'riya@hirelabtalent.com', 'oauth2:google')
    assert b'Connect with Google again' in ei.value.smtp_error

    monkeypatch.setattr(gm.requests, 'post', fake_post)
    assert cl.post('/api/google-mail/disconnect', json={}).get_json()['ok']
    assert cl.get('/api/google-mail/status').get_json()['connected'] is False
    assert cl.get('/api/me/mail').get_json()['mail']['status']['own_mailbox'] is False


def test_other_domain_is_refused(env, monkeypatch):
    from modules import google_mail as gm
    monkeypatch.setenv('GOOGLE_OAUTH_CLIENT_ID', 'cid')
    monkeypatch.setenv('GOOGLE_OAUTH_CLIENT_SECRET', 'sec')
    monkeypatch.setenv('GOOGLE_OAUTH_DOMAIN', 'hirelabtalent.com')
    monkeypatch.setattr(gm.requests, 'post', lambda url, data=None, timeout=None: _Resp(200, {
        'access_token': 'A', 'refresh_token': 'R', 'scope': 'https://mail.google.com/',
        'id_token': _idtok({'email': 'someone@gmail.com', 'email_verified': True})}))
    cl = _client(env)
    cl.get('/google-mail/connect')
    with cl.session_transaction() as s:
        state = s['gm_oauth']['state']
    r = cl.get('/google-mail/callback?code=abc&state=' + state)
    assert 'gmail=error' in r.headers['Location'] and 'hirelabtalent.com' in r.headers['Location']
