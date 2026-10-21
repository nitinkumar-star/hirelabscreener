"""
Candidate re-engagement e-mails — modules/reengage.py

Run:   pytest test_reengage.py -q
Real server, real tables; the e-mail pipeline is captured, not sent.
"""
import os
import sys
import tempfile
import datetime

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CO, OTHER = 981, 982
ADMIN, RIYA, OADMIN = 9801, 9802, 9803
M_JOB, M_OLD = 98101, 98102


def ago(days):
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30) - datetime.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S')


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='reengage_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db(); c = conn.cursor(); now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", list(kw.values()))
    ins('companies', id=CO, name='Re Co', status='active', created_at=now)
    ins('companies', id=OTHER, name='Other', status='active', created_at=now)
    for uid, un, co, adm in ((ADMIN, 're_admin', CO, 1), (RIYA, 're_riya', CO, 0), (OADMIN, 're_other', OTHER, 1)):
        ins('users', id=uid, username=un, password_hash='x', display_name=un.title(), role='user', status='approved',
            company_id=co, is_company_admin=adm, created_at=now)
    ins('mandates', id=M_JOB, role='Solar O&M Engineer', client='Azure Power', status='active', owner_id=CO,
        assigned_user_id=ADMIN, created_at=ago(400))
    ins('mandates', id=M_OLD, role='Wind Site Lead', client='Suzlon', status='closed', owner_id=CO,
        assigned_user_id=ADMIN, created_at=ago(500))
    rows = [  # id, mandate, name, email, stage, created days ago
        (98201, M_JOB, 'Asha Verma', 'asha@example.com', 'Not Suitable', 200),      # due, job
        (98202, M_OLD, 'Asha Verma', 'ASHA@example.com', 'Screening', 300),         # older row, same person
        (98203, M_JOB, 'Ravi Kumar', 'ravi@example.com', 'Placed', 300),            # placed -> excluded
        (98204, M_JOB, 'Neha Gupta', 'neha@example.com', 'Screening', 30),          # upcoming
        (98205, M_JOB, 'No Mail', '', 'Screening', 300),                            # no e-mail
        (98206, M_JOB, 'Off Person', 'off@example.com', 'Screening', 300),          # switched off
        (98207, M_JOB, 'Unsub Person', 'unsub@example.com', 'Screening', 300),      # unsubscribed
        (98208, M_JOB, 'Interview Guy', 'iv@example.com', 'Screening', 250),        # one row live in interview
        (98209, M_OLD, 'Interview Guy', 'iv@example.com', 'Interview Inprocess', 260),
        (98210, M_JOB, 'Updated Lady', 'upd@example.com', 'Screening', 300),        # updated profile 20 days ago
    ]
    for cid, mid, name, em, st, d in rows:
        ins('candidates', id=cid, mandate_id=mid, name=name, email=em, stage=st, owner_id=CO, created_at=ago(d))
    c.execute("UPDATE candidates SET reengage_off=1 WHERE id=98206")
    c.execute("UPDATE candidates SET do_not_email=1 WHERE id=98207")
    c.execute("UPDATE candidates SET update_submitted_at=? WHERE id=98210", (ago(20),))
    ins('candidates', id=98299, mandate_id=0, name='Other Co Person', email='x@example.com', stage='Screening',
        owner_id=OTHER, created_at=ago(400))
    conn.commit(); conn.close()
    yield server


@pytest.fixture
def mail(srv, monkeypatch):
    sent = []

    def fake(to, subject, body, body_html=None, candidate_id=None, extra_headers=None, **kw):
        sent.append({'to': to, 'subject': subject, 'body': body, 'html': body_html or '', 'cid': candidate_id,
                     'headers': extra_headers or {}})
        return True, '', '<m>', 't'
    monkeypatch.setattr(srv, 'email_service_send', fake)
    monkeypatch.setattr(srv, '_smtp_params', lambda: ('team@hirelabtalent.com', 'pw', 'smtp.gmail.com', 587, 'HireLab'))
    return sent


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


def people(srv):
    import modules.reengage as re_
    conn = srv.get_db()
    with srv.app.test_request_context():
        from flask import session
        session['user_id'] = ADMIN
        out = {p['email']: p for p in re_.build_people(conn, CO)}
    conn.close()
    return out


def test_who_is_due(srv):
    p = people(srv)
    a = p['asha@example.com']
    assert a['state'] == 'due' and a['candidate_id'] == 98201 and a['role'] == 'Solar O&M Engineer' and a['client'] == 'Azure Power'
    assert p['ravi@example.com']['state'] == 'excluded' and 'Placed' in p['ravi@example.com']['reason']
    assert p['neha@example.com']['state'] == 'upcoming'
    assert p['off@example.com']['reason'] == 'Switched off by you'
    assert p['unsub@example.com']['reason'] == 'Unsubscribed'
    assert p['iv@example.com']['state'] == 'excluded'             # live in another job
    assert p['upd@example.com']['state'] == 'upcoming'            # profile updated recently restarts the clock
    assert 'x@example.com' not in p and len([e for e in p if not e]) == 0


def test_compose_mentions_job_domains_and_details(srv):
    from modules.reengage import compose
    s, plain, html = compose({'name': 'Mr. asha verma', 'kind': 'job', 'role': 'Solar O&M Engineer', 'client': 'Azure Power',
                              'considered_on': '2026-04-12T10:00:00'}, 'https://x/re/c/1', 'https://x/u/1')
    assert s.startswith('Asha,') and 'On 12 April 2026, HireLab considered your profile for the Solar O&M Engineer position with Azure Power' in plain
    assert 'Data Center' in plain and 'Solar' in plain and 'Automation' in plain
    assert 'already have most of your details' in plain and 'https://x/re/c/1' in html and 'Unsubscribe' in html
    s2, p2, _ = compose({'name': 'Neha', 'kind': 'pool', 'considered_on': '2026-01-05'}, 'L', 'U')
    assert 'talent network since 5 January 2026' in p2 and 'PLC, SCADA' in p2


def test_dashboard_settings_and_access(srv, mail):
    a = cl(srv, ADMIN)
    j = a.get('/api/reengage/summary').get_json()
    assert j['ok'] and j['settings']['enabled'] == 0 and j['settings']['daily_cap'] == 50 and j['kpis']['due'] == 1
    assert a.put('/api/reengage/settings', json={'enabled': True, 'daily_cap': 50, 'start_hour': 10, 'end_hour': 19}).get_json()['settings']['enabled'] == 1
    assert a.put('/api/reengage/settings', json={'start_hour': 19, 'end_hour': 10}).status_code == 400
    assert cl(srv, RIYA).get('/api/reengage/summary').status_code == 403
    assert srv.app.test_client().get('/api/reengage/summary').status_code in (302, 401)
    lst = a.get('/api/reengage/people?tab=excluded').get_json()['items']
    assert {i['email'] for i in lst} >= {'ravi@example.com', 'off@example.com', 'unsub@example.com', 'iv@example.com'}
    pv = a.get('/api/reengage/preview?kind=job').get_json()
    assert 'Azure Power' in pv['html'] or 'considered your profile' in pv['html']


def test_send_once_track_and_profile_switch(srv, mail):
    import modules.reengage as re_
    a = cl(srv, ADMIN)
    r = a.post('/api/reengage/run-now', json={'count': 5}).get_json()
    assert r['ok'] and r['sent'] == 1 and len(mail) == 1
    m = mail[0]
    assert m['to'] == 'asha@example.com' and m['cid'] == 98201 and 'List-Unsubscribe' in m['headers']
    assert 'Solar O&M Engineer' in m['body'] and '/re/c/' in m['html']
    # never twice in the same cycle
    assert a.post('/api/reengage/run-now', json={'count': 5}).get_json()['sent'] == 0 and len(mail) == 1
    assert people(srv)['asha@example.com']['state'] == 'upcoming'
    # click -> update-profile link with the personal token; open pixel
    tok = m['html'].split('/re/c/')[1].split('"')[0]
    c = srv.app.test_client()
    rr = c.get('/re/c/' + tok)
    assert rr.status_code == 302 and '/update-profile?token=' in rr.headers['Location']
    assert c.get('/re/o/%s.gif' % tok).status_code == 200
    assert c.get('/re/c/1-bad').status_code == 302
    k = a.get('/api/reengage/summary').get_json()['kpis']
    assert k['sent_total'] == 1 and k['clicked'] == 1 and k['opened'] == 1
    # the candidate updates their profile -> counted
    conn = srv.get_db()
    conn.execute('UPDATE candidates SET update_submitted_at=? WHERE id=98201', ('2999-01-01T00:00:00',))
    conn.commit(); conn.close()
    assert a.get('/api/reengage/summary').get_json()['kpis']['updated'] == 1
    # per-candidate switch (works for a recruiter on a candidate they can see? admin here) — all rows of the person
    assert a.post('/api/candidates/98201/reengage', json={'off': True}).get_json()['off'] is True
    conn = srv.get_db()
    offs = [r[0] for r in conn.execute("SELECT reengage_off FROM candidates WHERE id IN (98201, 98202)")]
    conn.close()
    assert offs == [1, 1]
    j = a.get('/api/candidates/98201/reengage').get_json()
    assert j['off'] and 'Switched off' in j['line']
    a.post('/api/reengage/toggle', json={'candidate_id': 98201, 'off': False})
    assert not a.get('/api/candidates/98201/reengage').get_json()['off']
    # another company cannot touch it
    assert cl(srv, OADMIN).post('/api/reengage/toggle', json={'candidate_id': 98201, 'off': True}).status_code == 404


def test_daily_cap_and_window(srv, mail, monkeypatch):
    import modules.reengage as re_
    conn = srv.get_db()
    for i in range(6):
        conn.execute("INSERT OR REPLACE INTO candidates (id, mandate_id, name, email, stage, owner_id, created_at) "
                     "VALUES (?,?,?,?,?,?,?)", (98300 + i, M_JOB, 'Bulk %d' % i, 'bulk%d@example.com' % i, 'Screening', CO, ago(300)))
    conn.execute('UPDATE reengage_settings SET daily_cap=3, enabled=1 WHERE company_id=?', (CO,))
    conn.commit()
    # outside the window nothing goes
    monkeypatch.setattr(re_, 'now_ist', lambda: datetime.datetime(2026, 10, 21, 7, 0))
    with srv.app.test_request_context():
        from flask import session
        session['user_id'] = ADMIN
        assert re_.run_company(conn, CO) == 0
        monkeypatch.setattr(re_, 'now_ist', lambda: datetime.datetime.utcnow() + re_.IST)
        n = re_.run_company(conn, CO, force=True, limit=10)
    conn.close()
    assert n <= 3 and re_.sent_today(srv.get_db(), CO) <= 3


def test_page_needs_login(srv):
    assert srv.app.test_client().get('/reengage').status_code == 302
    assert b'Candidate re-engagement' in cl(srv, ADMIN).get('/reengage').data
