"""
Gmail sync that looks after itself (modules/mail_sync.py) + the intro tour
(modules/intro_tour.py).

Run:   pytest test_mail_sync_tour.py -q
"""
import os
import sys
import time
import tempfile
import datetime
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='mailsync_'))
    import server
    server.app.config['TESTING'] = True
    conn = server.get_db()
    c = conn.cursor()
    c.execute("INSERT INTO companies (name, status, plan, billing_status, created_at) "
              "VALUES ('MS Agency', 'active', 'standard', 'active', '2026-01-01T00:00:00')")
    co = c.lastrowid
    ids = {'co': co}
    for key, un, adm in (('boss', 'ms_boss', 1), ('rec', 'ms_rec', 0)):
        c.execute("INSERT INTO users (username, password_hash, display_name, role, status, company_id, is_company_admin, "
                  "created_at, tour_done_at) VALUES (?, 'x', ?, 'user', 'approved', ?, ?, '2026-01-01T00:00:00', '')",
                  (un, un, co, adm))
        ids[key] = c.lastrowid
    conn.commit(); conn.close()
    yield server, ids


def as_user(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


def ms():
    from modules import mail_sync
    return mail_sync


def test_plain_errors(srv):
    m = ms()
    assert m.classify('IMAP login failed: b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)". Enable IMAP in Gmail '
                      'and check the app password.')[0] == 'auth'
    assert m.classify("b'[ALERT] Your account is not enabled for IMAP use.'")[0] == 'imap_off'
    assert m.classify('Could not connect (imap.gmail.com): timed out')[0] == 'network'
    assert m.classify('Email not configured. Settings -> Email Configuration.')[0] == 'not_configured'
    assert m.classify('[WEBLOGINREQUIRED] Web login required')[0] == 'blocked'
    k, msg = m.classify('something odd')
    assert k == 'other' and 'something odd' in msg


def test_one_pass_per_mailbox_status_and_alert(srv):
    server, ids = srv
    m = ms()
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set(); release.wait(5)
        return 3, None
    t = threading.Thread(target=lambda: m.guarded(ids['co'], ids['rec'], slow))
    t.start(); started.wait(5)
    assert m.guarded(ids['co'], ids['rec'], lambda: (99, None)) == (0, None)        # second pass waits its turn
    release.set(); t.join(5)
    st = m.status_row(ids['co'], ids['rec'])
    assert st['last_added'] == 3 and st['fails'] == 0 and st['last_ok_at']

    bad = lambda: (0, 'IMAP login failed: [AUTHENTICATIONFAILED] Invalid credentials')
    added, err = m.guarded(ids['co'], ids['rec'], bad)
    assert added == 0 and 'App Password' in err
    m.guarded(ids['co'], ids['rec'], bad)
    st = m.status_row(ids['co'], ids['rec'])
    assert st['fails'] == 2 and st['error_kind'] == 'auth' and st['last_ok_at']
    conn = server.get_db()
    n = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND kind='mail_sync'", (ids['rec'],)).fetchone()[0]
    conn.close()
    assert n == 1                                            # told once, not on every failed pass
    # company mailbox failure -> the agency admin is told
    m.guarded(ids['co'], 0, lambda: (0, "[ALERT] Your account is not enabled for IMAP use"))
    conn = server.get_db()
    n = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND kind='mail_sync'", (ids['boss'],)).fetchone()[0]
    conn.close()
    assert n == 1
    m.guarded(ids['co'], ids['rec'], lambda: (1, None))     # works again -> clean slate
    st = m.status_row(ids['co'], ids['rec'])
    assert st['fails'] == 0 and st['error_kind'] == '' and st['alerted_at'] == '' and st['total_added'] == 4


def test_background_schedule_backs_off(srv):
    m = ms()
    now = datetime.datetime(2026, 11, 3, 12, 0)
    ago = lambda mins: (now - datetime.timedelta(minutes=mins)).isoformat()
    assert m.due({}, now, 5)
    assert not m.due({'last_try_at': ago(2)}, now, 5) and m.due({'last_try_at': ago(5)}, now, 5)
    assert not m.due({'last_try_at': ago(20), 'fails': 3, 'error_kind': 'network'}, now, 5)
    assert m.due({'last_try_at': ago(31), 'fails': 3, 'error_kind': 'network'}, now, 5)
    assert not m.due({'last_try_at': ago(120), 'fails': 3, 'error_kind': 'auth'}, now, 5)


def test_tick_syncs_every_connected_mailbox(srv, monkeypatch):
    server, ids = srv
    m = ms()
    conn = server.get_db()
    conn.execute("INSERT OR REPLACE INTO user_mail_identity (user_id, company_id, smtp_email, smtp_app_password) "
                 "VALUES (?, ?, 'boss@gmail.com', 'abcdefghijklmnop')", (ids['boss'], ids['co']))
    conn.execute("DELETE FROM mail_sync_status WHERE user_id=?", (ids['boss'],))
    conn.commit(); conn.close()
    ran = []
    monkeypatch.setattr(m, 'run_box', lambda oid, uid: ran.append((oid, uid)))
    m.tick()
    assert (ids['co'], ids['boss']) in ran
    monkeypatch.setenv('MAIL_AUTOSYNC', 'off')
    ran.clear(); m.tick()
    assert ran == []


def test_guided_connect_tests_before_saving(srv, monkeypatch):
    server, ids = srv
    m = ms()
    from modules import mail_identity as mi
    calls = []
    monkeypatch.setattr(m, '_imap_try', lambda e, p: (calls.append(('imap', e, p)) or
                                                      ((False, 'IMAP login failed: [AUTHENTICATIONFAILED] Invalid credentials')
                                                       if p.startswith('wrong') else (True, ''))))
    monkeypatch.setattr(mi, '_smtp_try', lambda e, p, **k: (True, ''))
    monkeypatch.setattr(m, '_start_bg', lambda oid, uid: calls.append(('sync', oid, uid)))
    rec = as_user(server, ids['rec'])
    r = rec.post('/api/mail/connect', json={'email': 'rec@gmail.com', 'app_password': 'my gmail pass'})
    assert r.status_code == 400 and r.get_json()['step'] == 'password'                 # not an App Password
    r = rec.post('/api/mail/connect', json={'email': 'rec@gmail.com', 'app_password': 'wrong pass word xyzw'})
    j = r.get_json()
    assert r.status_code == 400 and j['kind'] == 'auth' and not j['checks'][0]['ok']
    conn = server.get_db()
    assert not conn.execute('SELECT 1 FROM user_mail_identity WHERE user_id=?', (ids['rec'],)).fetchone()  # nothing saved
    conn.close()
    r = rec.post('/api/mail/connect', json={'email': 'rec@gmail.com', 'app_password': 'abcd efgh ijkl mnop',
                                            'display_name': 'Rec One'})
    j = r.get_json()
    assert j['ok'] and [c['ok'] for c in j['checks']] == [True, True, True]
    assert ('imap', 'rec@gmail.com', 'abcdefghijklmnop') in calls and ('sync', ids['co'], ids['rec']) in calls
    conn = server.get_db()
    row = conn.execute('SELECT * FROM user_mail_identity WHERE user_id=?', (ids['rec'],)).fetchone()
    conn.close()
    assert row['smtp_app_password'] == 'abcdefghijklmnop' and row['verified_at'] and row['smtp_display_name'] == 'Rec One'
    # only the agency admin connects the company mailbox
    assert rec.post('/api/mail/connect', json={'box': 'company', 'email': 'co@gmail.com',
                                               'app_password': 'abcdefghijklmnop'}).status_code == 403
    st = rec.get('/api/mail/status').get_json()
    assert [b['box'] for b in st['boxes']] == ['me'] and st['boxes'][0]['email'] == 'rec@gmail.com'
    boss = as_user(server, ids['boss']).get('/api/mail/status').get_json()
    assert [b['box'] for b in boss['boxes']] == ['me', 'company'] and boss['boxes'][1]['error_kind'] == 'imap_off'
    assert as_user(server, ids['rec']).post('/api/mail/sync-now', json={'box': 'company'}).status_code == 403


def test_intro_tour_flag(srv):
    server, ids = srv
    c = as_user(server, ids['rec'])
    assert c.get('/api/auth/status').get_json()['user']['tour_done'] is False
    assert c.post('/api/me/tour', json={'done': True}).get_json()['ok']
    assert c.get('/api/auth/status').get_json()['user']['tour_done'] is True
    c.post('/api/me/tour', json={'done': False})
    assert c.get('/api/auth/status').get_json()['user']['tour_done'] is False
