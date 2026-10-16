"""
Google Meet invites (permanent Meet link + calendar invite e-mail)
— modules/google_meet.py + scheduler hooks.

Run:   pytest test_google_meet.py -q
Real server and scheduler routes; the SMTP send is captured, not sent.
"""
import os
import sys
import tempfile
import email

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CO = 991
HOST, RIYA, PLAIN = 9901, 9902, 9903
LINK = 'https://meet.google.com/abc-defg-hij'


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='gmeet_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db(); c = conn.cursor(); now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", list(kw.values()))
    ins('companies', id=CO, name='Meet Co', status='active', created_at=now)
    for uid, un, adm in ((HOST, 'gm_host', 1), (RIYA, 'gm_riya', 0), (PLAIN, 'gm_plain', 0)):
        ins('users', id=uid, username=un, password_hash='x', display_name=un.title(), role='user', status='approved',
            company_id=CO, is_company_admin=adm, created_at=now)
    for k, v in (('smtp_email', 'nitin@hirelabtalent.com'), ('smtp_app_password', 'abcd efgh ijkl mnop')):
        c.execute('INSERT OR REPLACE INTO tenant_settings (company_id, key, value) VALUES (?,?,?)', (CO, k, v))
    conn.commit(); conn.close()
    yield server


@pytest.fixture
def mail(srv, monkeypatch):
    import modules.google_meet as gm
    sent = []

    def fake(msg, addr, pw, recipients):
        sent.append({'msg': msg, 'from': addr, 'to': recipients, 'raw': msg.as_string()})
    monkeypatch.setattr(gm, 'send_message', fake)
    plat = []
    monkeypatch.setattr(srv, '_platform_smtp_send', lambda to, s, p, h=None: plat.append(to) or (True, ''))
    sent_obj = type('M', (), {})()
    sent_obj.sent, sent_obj.platform = sent, plat
    return sent_obj


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


def slots_for(c, link=LINK):
    av = c.get('/api/scheduler/availability').get_json()['availability']
    c.put('/api/scheduler/availability', json={'modes': ['Phone', 'Video'], 'min_notice_hours': 0,
                                              'weekly_hours': {k: [['00:00', '23:59']] for k in
                                                               ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')}})
    if link is not None:
        assert c.put('/api/me/meet-link', json={'meet_link': link}).get_json()['ok']
    s = c.get('/api/scheduler/public/%s/slots' % av['token']).get_json()['slots']
    return av['token'], [d + 'T' + h for d in sorted(s)[1:] for h in s[d]]


def ics_of(m):
    msg = email.message_from_string(m['raw'])
    for part in msg.walk():
        if part.get_content_type() == 'text/calendar':
            return part.get_payload(decode=True).decode(), part.get_param('method')
    return None, None


def meeting(srv, mid):
    conn = srv.get_db()
    r = dict(conn.execute('SELECT * FROM meetings WHERE id=?', (mid,)).fetchone())
    conn.close()
    return r


# ── pure ─────────────────────────────────────────────────────────────────
def test_clean_meet_link(srv):
    from modules.google_meet import clean_meet_link
    assert clean_meet_link('meet.google.com/ABC-defg-hij?authuser=0') == LINK
    assert clean_meet_link('https://meet.google.com/abc-defg-hij') == LINK
    assert clean_meet_link('https://zoom.us/j/123') == ''


def test_ics_is_wellformed(srv):
    from modules.google_meet import build_ics
    m = {'id': 7, 'start_at': '2026-10-20T11:00', 'end_at': '2026-10-20T11:30', 'guest_name': 'Asha, Verma',
         'guest_email': 'a@x.com', 'purpose': 'Solar; O&M'}
    ics = build_ics(m, 'REQUEST', 1, 'Nitin', 'n@h.com', LINK, 'https://h/book/manage/t', 'Meeting: Asha <> Nitin ' * 4)
    assert 'DTSTART:20261020T053000Z' in ics and 'DTEND:20261020T060000Z' in ics      # 11:00 IST = 05:30 UTC
    assert 'METHOD:REQUEST' in ics and 'UID:hirelab-meeting-7@hirelab' in ics and 'LOCATION:' + LINK in ics
    assert 'CN=Asha\\, Verma' in ics
    assert all(len(l.encode()) <= 75 for l in ics.split('\r\n'))


# ── routes ───────────────────────────────────────────────────────────────
def test_save_link_validation(srv, mail):
    c = cl(srv, HOST)
    assert c.put('/api/me/meet-link', json={'meet_link': 'https://zoom.us/j/1'}).status_code == 400
    j = c.put('/api/me/meet-link', json={'meet_link': 'meet.google.com/abc-defg-hij'}).get_json()
    assert j['meet_link'] == LINK
    g = c.get('/api/me/meet-link').get_json()
    assert g['meet_link'] == LINK and g['sender'] == 'nitin@hirelabtalent.com'


def test_video_booking_emails_invite_from_host(srv, mail):
    c = cl(srv, HOST)
    token, slots = slots_for(c)
    pub = srv.app.test_client()
    j = pub.post('/api/scheduler/public/%s/book' % token, json={'start_at': slots[0], 'guest_name': 'Asha Verma',
                                                              'guest_email': 'asha@example.com', 'mode': 'Video'}).get_json()
    assert j['ok'] and j['meet_link'] == LINK and j['google_invite_sent']
    s = mail.sent[-1]
    assert s['from'] == 'nitin@hirelabtalent.com' and s['to'] == ['asha@example.com', 'nitin@hirelabtalent.com']
    ics, method = ics_of(s)
    assert method == 'REQUEST' and 'SEQUENCE:1' in ics and 'mailto:asha@example.com' in ics and LINK in ics
    assert '/book/manage/' in ics.replace('\r\n ', '')
    m = meeting(srv, j['meeting_id'])
    assert m['location'] == LINK and m['invite_seq'] == 1
    assert 'asha@example.com' not in mail.platform           # no second, duplicate confirmation


def test_reschedule_and_cancel_update_the_same_invite(srv, mail):
    c = cl(srv, HOST)
    token, slots = slots_for(c)
    pub = srv.app.test_client()
    j = pub.post('/api/scheduler/public/%s/book' % token, json={'start_at': slots[1], 'guest_name': 'Ravi',
                                                              'guest_email': 'ravi@example.com', 'mode': 'Video'}).get_json()
    pub.post('/api/scheduler/public/manage/%s/reschedule' % j['manage_token'], json={'start_at': slots[5]})
    ics, method = ics_of(mail.sent[-1])
    uid = 'UID:hirelab-meeting-%s@hirelab' % j['meeting_id']
    assert method == 'REQUEST' and 'SEQUENCE:2' in ics and uid in ics
    assert 'Updated invitation' in mail.sent[-1]['msg']['Subject']
    pub.post('/api/scheduler/public/manage/%s/cancel' % j['manage_token'])
    ics, method = ics_of(mail.sent[-1])
    assert method == 'CANCEL' and 'SEQUENCE:3' in ics and uid in ics and 'STATUS:CANCELLED' in ics


def test_phone_booking_and_no_link_unchanged(srv, mail):
    c = cl(srv, HOST)
    token, slots = slots_for(c)
    pub = srv.app.test_client()
    n = len(mail.sent)
    j = pub.post('/api/scheduler/public/%s/book' % token, json={'start_at': slots[2], 'guest_name': 'P',
                                                              'guest_email': 'p@example.com', 'mode': 'Phone'}).get_json()
    assert j['ok'] and not j['meet_link'] and len(mail.sent) == n and 'p@example.com' in mail.platform
    cp = cl(srv, PLAIN)
    token2, slots2 = slots_for(cp, link=None)
    j2 = pub.post('/api/scheduler/public/%s/book' % token2, json={'start_at': slots2[0], 'guest_name': 'V',
                                                                'guest_email': 'v@example.com', 'mode': 'Video'}).get_json()
    assert j2['ok'] and not j2['meet_link'] and len(mail.sent) == n and 'v@example.com' in mail.platform


def test_no_guest_email_still_shows_link(srv, mail):
    c = cl(srv, HOST)
    token, slots = slots_for(c)
    n = len(mail.sent)
    j = srv.app.test_client().post('/api/scheduler/public/%s/book' % token, json={
        'start_at': slots[3], 'guest_name': 'No Mail', 'mode': 'Video'}).get_json()
    assert j['ok'] and j['meet_link'] == LINK and len(mail.sent) == n


def test_send_failure_does_not_break_booking(srv, mail, monkeypatch):
    import modules.google_meet as gm
    def boom(*a, **k):
        raise RuntimeError('smtp down')
    monkeypatch.setattr(gm, 'send_message', boom)
    c = cl(srv, HOST)
    token, slots = slots_for(c)
    j = srv.app.test_client().post('/api/scheduler/public/%s/book' % token, json={
        'start_at': slots[4], 'guest_name': 'G', 'guest_email': 'g@example.com', 'mode': 'Video'}).get_json()
    assert j['ok'] and 'g@example.com' in mail.platform          # falls back to the usual confirmation
    assert 'smtp down' in meeting(srv, j['meeting_id'])['google_error']


def test_manual_booking_status_cancel_and_test_invite(srv, mail):
    c = cl(srv, HOST)
    _, slots = slots_for(c)
    j = c.post('/api/scheduler/meetings', json={'start_at': slots[8], 'guest_name': 'Client X', 'mode': 'Video',
                                                'guest_email': 'x@client.com'}).get_json()
    assert j['google']['state'] == 'created'
    c.post('/api/scheduler/meetings/%d/status' % j['meeting_id'], json={'status': 'cancelled'})
    assert ics_of(mail.sent[-1])[1] == 'CANCEL'
    t = c.post('/api/me/meet-link/test').get_json()
    assert t['ok'] and mail.sent[-1]['to'] == ['nitin@hirelabtalent.com']


def test_access(srv, mail):
    c = cl(srv, HOST)
    _, slots = slots_for(c)
    mid = c.post('/api/scheduler/meetings', json={'start_at': slots[9], 'guest_name': 'Z', 'mode': 'Video'}).get_json()['meeting_id']
    assert cl(srv, RIYA).post('/api/google/meetings/%d/retry' % mid).status_code in (403, 404)
    assert srv.app.test_client().get('/api/me/meet-link').status_code == 401


def test_send_message_uses_core_smtp(srv, monkeypatch):
    import modules.google_meet as gm
    got = {}

    class FakeSrv:
        def sendmail(self, frm, to, body):
            got.update(frm=frm, to=to, body=body)

        def quit(self):
            got['quit'] = True
    monkeypatch.setattr(srv, 'smtp_open', lambda host, port, u, p: (got.update(host=host, user=u) or FakeSrv(), port))
    m = {'id': 1, 'start_at': '2026-10-20T11:00', 'end_at': '2026-10-20T11:30', 'guest_name': 'A',
         'guest_email': 'a@x.com', 'purpose': '', 'manage_token': ''}
    gm.send_message(gm.build_message(m, 'REQUEST', 1, 'Nitin', 'n@hirelabtalent.com', LINK, '', 'new'),
                    'n@hirelabtalent.com', 'pw', ['a@x.com'])
    assert got['host'] == 'smtp.gmail.com' and got['to'] == ['a@x.com'] and 'text/calendar' in got['body'] and got['quit']
