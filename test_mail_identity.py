"""
Per-recruiter mail identity.

Run:   pytest tests/test_mail_identity.py -q

A recruiter's emails must go out from THEIR Gmail (when they set one up) and
be signed with THEIR name; Email-Agent drafts go out as the mandate's primary
recruiter; the company inbox sync always uses the company mailbox; nothing
configured = the old company behaviour. No real SMTP/IMAP is touched.
"""
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CO, OTHER_CO = 931, 932
ADMIN, RIYA, RAJ, OTHER_ADMIN = 9301, 9302, 9303, 9304
M_RIYA, C_RIYA, M_ADMIN, C_ADMIN, ITEM = 93101, 93201, 93102, 93202, 93301


def _decoded(raw):
    """Headers + every decoded text part, so assertions read plain text."""
    import email
    m = email.message_from_string(raw)
    out = ['From: ' + str(m['From'])]
    for part in m.walk():
        if part.get_content_maintype() == 'text':
            out.append(part.get_payload(decode=True).decode('utf-8', 'replace'))
    return '\n'.join(out)


class FakeSMTP(object):
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def starttls(self):
        pass

    def login(self, user, pw):
        self.user, self.pw = user, pw
        import smtplib
        if pw == 'badpass':
            raise smtplib.SMTPAuthenticationError(535, b'bad')
        if pw == 'dropme':
            raise smtplib.SMTPServerDisconnected('Connection unexpectedly closed')

    def sendmail(self, frm, to, msg):
        FakeSMTP.sent.append({'login': self.user, 'pw': self.pw, 'from': frm, 'to': to, 'msg': _decoded(msg)})
        return {}

    def send_message(self, msg, *a, **k):
        FakeSMTP.sent.append({'login': self.user, 'pw': self.pw, 'from': msg['From'], 'to': msg['To'],
                              'msg': _decoded(msg.as_string())})
        return {}

    def quit(self):
        pass

    def close(self):
        pass


@pytest.fixture(scope='module')
def env():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='mail_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    import smtplib
    orig = smtplib.SMTP
    smtplib.SMTP = FakeSMTP
    conn = server.get_db(); c = conn.cursor(); now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})",
                  list(kw.values()))

    ins('companies', id=CO, name='HireLab Mail', status='active', created_at=now)
    ins('companies', id=OTHER_CO, name='Other Mail', status='active', created_at=now)
    for uid, un, co, adm, dn in ((ADMIN, 'ml_admin', CO, 1, 'Nitin Kumar'), (RIYA, 'ml_riya', CO, 0, 'Riya Sharma'),
                                 (RAJ, 'ml_raj', CO, 0, 'Raj Verma'), (OTHER_ADMIN, 'ml_other', OTHER_CO, 1, 'Other')):
        ins('users', id=uid, username=un, password_hash='x', display_name=dn, role='user',
            status='approved', company_id=co, is_company_admin=adm, created_at=now)
    c.execute("UPDATE users SET profile_designation='Senior Recruiter', profile_phone='98100 00002' WHERE id=?", (RIYA,))
    for k, v in (('smtp_email', 'nitin@hirelabtalent.com'), ('smtp_app_password', 'companypass'),
                 ('smtp_display_name', 'HireLab Talent'), ('sig_name', 'Nitin Kumar'),
                 ('sig_company', 'HireLab Talent'), ('company_name', 'HireLab Talent'),
                 ('recruiter_name', 'Nitin Kumar')):
        c.execute("INSERT OR REPLACE INTO tenant_settings (company_id,key,value) VALUES (?,?,?)", (CO, k, v))
    ins('mandates', id=M_RIYA, client='Acme', role='Java Dev', owner_id=CO, assigned_user_id=RIYA,
        status='active', created_at=now)
    ins('mandates', id=M_ADMIN, client='Beta', role='PM', owner_id=CO, assigned_user_id=ADMIN,
        status='active', created_at=now)
    ins('candidates', id=C_RIYA, mandate_id=M_RIYA, name='Asha Cand', email='asha@x.com', owner_id=CO,
        stage='Screening', created_at=now)
    ins('candidates', id=C_ADMIN, mandate_id=M_ADMIN, name='Bala Cand', email='bala@x.com', owner_id=CO,
        stage='Screening', created_at=now)
    ins('agent_items', id=ITEM, owner_id=CO, candidate_id=C_RIYA, mandate_id=M_RIYA, kind='followup',
        subject='Re: Java Dev', body='Hi Asha', status='pending', created_at=now)
    conn.commit(); conn.close()
    yield server
    smtplib.SMTP = orig


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


def setting_as(server, uid, key):
    with server.app.test_request_context('/'):
        from flask import session
        session['user_id'] = uid
        return server.get_setting(key, '')


def test_fallback_is_company_mailbox_but_recruiter_signature(env):
    s = env
    assert setting_as(s, RIYA, 'smtp_email') == 'nitin@hirelabtalent.com'
    assert setting_as(s, RIYA, 'smtp_app_password') == 'companypass'
    assert setting_as(s, RIYA, 'sig_name') == 'Riya Sharma'
    assert setting_as(s, RIYA, 'sig_designation') == 'Senior Recruiter'
    assert setting_as(s, RIYA, 'recruiter_name') == 'Riya Sharma'
    assert setting_as(s, RIYA, 'sig_company') == 'HireLab Talent'      # company part stays
    # the admin is untouched
    assert setting_as(s, ADMIN, 'sig_name') == 'Nitin Kumar'
    assert setting_as(s, ADMIN, 'smtp_email') == 'nitin@hirelabtalent.com'


def test_recruiter_saves_own_gmail_password_never_returned(env):
    s = env
    c = cl(s, RIYA)
    r = c.put('/api/me/mail', json={'smtp_email': 'riya.hirelab@gmail.com',
                                    'smtp_app_password': 'abcd efgh ijkl mnop',
                                    'smtp_display_name': 'Riya | HireLab'})
    assert r.status_code == 200, r.get_json()
    j = c.get('/api/me/mail').get_json()
    assert j['mail']['smtp_email'] == 'riya.hirelab@gmail.com'
    assert j['mail']['has_password'] is True
    assert 'abcdefghijklmnop' not in str(j)
    assert j['mail']['company_sender'] == 'nitin@hirelabtalent.com'
    # resolution
    assert setting_as(s, RIYA, 'smtp_email') == 'riya.hirelab@gmail.com'
    assert setting_as(s, RIYA, 'smtp_app_password') == 'abcdefghijklmnop'
    assert setting_as(s, RIYA, 'smtp_display_name') == 'Riya | HireLab'
    assert setting_as(s, RIYA, 'sig_email') == 'riya.hirelab@gmail.com'
    assert setting_as(s, ADMIN, 'smtp_email') == 'nitin@hirelabtalent.com'
    assert setting_as(s, RAJ, 'smtp_email') == 'nitin@hirelabtalent.com'
    # a recruiter's settings view shows THEIR sender, secret still masked
    st = c.get('/api/settings').get_json()
    assert st['smtp_email'] == 'riya.hirelab@gmail.com'
    assert st['smtp_app_password'] == '__set__'
    # blank password keeps the saved one
    c.put('/api/me/mail', json={'smtp_email': 'riya.hirelab@gmail.com', 'smtp_display_name': 'Riya S'})
    assert setting_as(s, RIYA, 'smtp_app_password') == 'abcdefghijklmnop'
    # a NEW address never inherits the old password
    c.put('/api/me/mail', json={'smtp_email': 'riya2@gmail.com'})
    assert setting_as(s, RIYA, 'smtp_email') == 'nitin@hirelabtalent.com'   # incomplete -> company
    c.put('/api/me/mail', json={'smtp_email': 'riya.hirelab@gmail.com', 'smtp_app_password': 'abcdefghijklmnop',
                                'smtp_display_name': 'Riya | HireLab'})


def test_candidate_email_goes_from_recruiter_gmail(env):
    s = env
    FakeSMTP.sent.clear()
    r = cl(s, RIYA).post(f'/api/candidates/{C_RIYA}/send-email',
                         json={'to': 'asha@x.com', 'subject': 'Java role', 'body': 'Hi Asha, quick chat?'})
    assert r.status_code == 200, r.get_json()
    m = FakeSMTP.sent[-1]
    assert m['login'] == 'riya.hirelab@gmail.com' and m['pw'] == 'abcdefghijklmnop'
    assert 'riya.hirelab@gmail.com' in m['msg'] and 'Riya Sharma' in m['msg']
    assert 'Nitin Kumar' not in m['msg']


def test_admin_email_still_company(env):
    s = env
    FakeSMTP.sent.clear()
    r = cl(s, ADMIN).post(f'/api/candidates/{C_ADMIN}/send-email',
                          json={'to': 'bala@x.com', 'subject': 'PM role', 'body': 'Hello Bala'})
    assert r.status_code == 200, r.get_json()
    m = FakeSMTP.sent[-1]
    assert m['login'] == 'nitin@hirelabtalent.com'
    assert 'Riya' not in m['msg']


def test_agent_followup_goes_as_primary_recruiter_even_if_admin_clicks(env):
    s = env
    FakeSMTP.sent.clear()
    r = cl(s, ADMIN).post(f'/api/candidates/{C_RIYA}/send-email',
                          json={'to': 'asha@x.com', 'subject': 'Re: Java Dev', 'body': 'Just following up',
                                'agent_item_id': ITEM})
    assert r.status_code == 200, r.get_json()
    m = FakeSMTP.sent[-1]
    assert m['login'] == 'riya.hirelab@gmail.com'
    assert 'Riya Sharma' in m['msg']
    # an item id from a different candidate is ignored (sender = the clicker)
    FakeSMTP.sent.clear()
    cl(s, ADMIN).post(f'/api/candidates/{C_ADMIN}/send-email',
                      json={'to': 'bala@x.com', 'subject': 'x', 'body': 'y', 'agent_item_id': ITEM})
    assert FakeSMTP.sent[-1]['login'] == 'nitin@hirelabtalent.com'


def test_agent_draft_signoff_is_primary_recruiter(env):
    s = env
    conn = s.get_db()
    with s.tenant_pin(CO), s.mail_sender(0):
        sig, uid = s._agent_signoff_for(conn, M_RIYA, CO, 'HireLab Talent')
        sig2, uid2 = s._agent_signoff_for(conn, M_ADMIN, CO, 'HireLab Talent')
    conn.close()
    assert uid == RIYA and sig.startswith('Riya Sharma')
    assert uid2 == 0 and sig2 == 'HireLab Talent'


def test_company_inbox_sync_never_uses_recruiter_gmail(env):
    s = env
    import imaplib
    seen = {}

    class FakeIMAP(object):
        def __init__(self, host, *a, **k):
            pass

        def login(self, u, p):
            seen['user'] = u
            raise imaplib.IMAP4.error('stop here')

    orig = imaplib.IMAP4_SSL
    imaplib.IMAP4_SSL = FakeIMAP
    try:
        with s.app.test_request_context('/'):
            from flask import session
            session['user_id'] = RIYA
            s._sync_mailbox(CO)
        s._sync_imap_inbox(CO)          # worker thread path: no session at all
    finally:
        imaplib.IMAP4_SSL = orig
    assert seen['user'] == 'nitin@hirelabtalent.com'


def test_worker_thread_without_pin_is_company(env):
    s = env
    import threading
    out = {}
    t = threading.Thread(target=lambda: out.setdefault('v', s._mail_sender_uid()))
    t.start(); t.join()
    assert out['v'] == 0


def test_test_endpoint(env):
    s = env
    c = cl(s, RIYA)
    r = c.post('/api/me/mail/test', json={})
    assert r.status_code == 200, r.get_json()
    assert c.get('/api/me/mail').get_json()['mail']['verified_at']
    r = c.post('/api/me/mail/test', json={'smtp_email': 'riya.hirelab@gmail.com', 'smtp_app_password': 'badpass'})
    assert r.status_code == 400 and 'App Password' in r.get_json()['error']
    # Gmail dropping the line at login -> clear hint, after also trying port 465
    import smtplib
    _ssl = smtplib.SMTP_SSL
    smtplib.SMTP_SSL = FakeSMTP
    try:
        r = c.post('/api/me/mail/test', json={'smtp_email': 'riya.hirelab@gmail.com', 'smtp_app_password': 'dropme'})
    finally:
        smtplib.SMTP_SSL = _ssl
    e = r.get_json()['error']
    assert r.status_code == 400 and 'during login' in e and 'App Password' in e, e


def test_admin_edits_team_member(env):
    s = env
    a = cl(s, ADMIN)
    r = a.put(f'/api/team/{RAJ}', json={'display_name': 'Raj K Verma', 'username': 'raj.verma',
                                        'profile_email': 'raj@hirelabtalent.com',
                                        'smtp_email': 'raj.hirelab@gmail.com', 'smtp_app_password': 'rajpass1234'})
    assert r.status_code == 200, r.get_json()
    u = a.get(f'/api/team/{RAJ}').get_json()['user']
    assert u['username'] == 'raj.verma' and u['profile']['display_name'] == 'Raj K Verma'
    assert u['has_password'] and 'rajpass1234' not in str(u)
    assert setting_as(s, RAJ, 'smtp_email') == 'raj.hirelab@gmail.com'
    # duplicate login id refused
    assert a.put(f'/api/team/{RAJ}', json={'username': 'ml_riya'}).status_code == 400


def test_recruiter_and_other_company_cannot_edit(env):
    s = env
    assert cl(s, RAJ).put(f'/api/team/{RIYA}', json={'display_name': 'hacked'}).status_code == 403
    assert cl(s, RAJ).get(f'/api/team/{RIYA}').status_code == 403
    assert cl(s, OTHER_ADMIN).put(f'/api/team/{RIYA}', json={'display_name': 'hacked'}).status_code == 403
    assert cl(s, RIYA).put(f'/api/team/{RIYA}', json={'username': 'x'}).status_code == 403
    conn = s.get_db()
    assert conn.execute('SELECT display_name FROM users WHERE id=?', (RIYA,)).fetchone()[0] == 'Riya Sharma'
    conn.close()


def test_remove_reverts_to_company(env):
    s = env
    c = cl(s, RIYA)
    assert c.delete('/api/me/mail').status_code == 200
    assert setting_as(s, RIYA, 'smtp_email') == 'nitin@hirelabtalent.com'
    assert setting_as(s, RIYA, 'sig_name') == 'Riya Sharma'
