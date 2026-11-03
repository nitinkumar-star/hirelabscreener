"""
Commercial launch readiness (Nov 2026): trial lifecycle, payment instructions,
abuse limits on public auth endpoints, security headers.

Run:   pytest test_launch_ready.py -q
"""
import os
import sys
import tempfile
import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='launch_'))
    import server
    server.app.config['TESTING'] = True
    conn = server.get_db()
    c = conn.cursor()
    past = (datetime.datetime.now() - datetime.timedelta(days=2)).isoformat()
    c.execute("INSERT INTO companies (name, status, plan, billing_status, created_at) "
              "VALUES ('LR Owner', 'active', 'owner', 'owner', '2026-01-01T00:00:00')")
    owner_co = c.lastrowid
    c.execute("INSERT INTO companies (name, status, plan, billing_status, trial_ends_at, created_at) "
              "VALUES ('LR Agency', 'active', 'standard', 'trial', ?, '2026-01-01T00:00:00')", (past,))
    co = c.lastrowid
    ids = {'co': co}
    for key, un, role, cid in (('owner', 'lr_owner', 'admin', owner_co), ('agent', 'lr_agent', 'user', co)):
        c.execute("INSERT INTO users (username, password_hash, display_name, role, status, company_id, is_company_admin, "
                  "created_at) VALUES (?, ?, ?, ?, 'approved', ?, 1, '2026-01-01T00:00:00')",
                  (un, server.hash_password('secret-pass-1'), un, role, cid))
        ids[key] = c.lastrowid
    conn.commit(); conn.close()
    server.set_setting('billing_pay_instructions', 'UPI: hirelab@okhdfc')
    yield server, ids


def as_user(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


def login(server, un):
    return server.app.test_client().post('/api/auth/login', json={'username': un, 'password': 'secret-pass-1'})


def test_trial_over_blocks_every_sign_in_and_session(srv):
    server, ids = srv
    for _ in range(2):                                   # used to let the 2nd sign-in through (past_due)
        r = login(server, 'lr_agent')
        j = r.get_json()
        assert r.status_code == 402 and j['code'] == 'trial_ended' and 'hirelab@okhdfc' in j['pay_instructions']
    c = as_user(server, ids['agent'])
    r = c.get('/api/mandates')
    assert r.status_code == 402 and r.get_json()['code'] == 'trial_ended'
    b = c.get('/api/billing/me').get_json()              # billing stays open so they can pay
    assert b['ok'] and b['bill']['pay_instructions'] == 'UPI: hirelab@okhdfc' and b['bill']['support_email']


def test_extend_trial_then_payment_unlocks(srv):
    server, ids = srv
    owner = as_user(server, ids['owner'])
    assert as_user(server, ids['agent']).post(f"/api/admin/billing/{ids['co']}/extend-trial", json={'days': 7}).status_code in (402, 403)
    assert owner.post(f"/api/admin/billing/{ids['co']}/extend-trial", json={'days': 0}).status_code == 400
    r = owner.post(f"/api/admin/billing/{ids['co']}/extend-trial", json={'days': 7})
    assert r.status_code == 200 and r.get_json()['trial_ends_at'][:10] == (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
    assert login(server, 'lr_agent').status_code == 200
    assert as_user(server, ids['agent']).get('/api/mandates').status_code == 200

    # trial runs out again, then the agency pays: a later 'past_due' badge must not lock a paying agency
    conn = server.get_db()
    conn.execute("UPDATE companies SET trial_ends_at=?, billing_status='trial' WHERE id=?",
                 ((datetime.datetime.now() - datetime.timedelta(days=1)).isoformat(), ids['co']))
    conn.commit(); conn.close()
    assert login(server, 'lr_agent').status_code == 402
    assert owner.post(f"/api/admin/billing/{ids['co']}/pay", json={'amount': 700, 'method': 'upi'}).status_code == 200
    assert login(server, 'lr_agent').status_code == 200
    assert owner.post(f"/api/admin/billing/{ids['co']}/status", json={'status': 'past_due'}).status_code == 200
    assert login(server, 'lr_agent').status_code == 200
    assert as_user(server, ids['agent']).get('/api/mandates').status_code == 200


def test_public_auth_endpoints_are_rate_limited(srv):
    server, ids = srv
    server._RL_BUCKETS.clear()
    c = server.app.test_client()
    codes = [c.post('/api/auth/signup', json={'username': 'lr_new%d' % i, 'password': 'secret-pass-1',
                                             'email': 'n%d@x.com' % i, 'accept_terms': True}).status_code
             for i in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429
    codes = [c.post('/api/auth/forgot', json={'identifier': 'nobody'}).status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429
    server._RL_BUCKETS.clear()


def test_security_headers_and_health(srv):
    server, ids = srv
    r = server.app.test_client().get('/api/health')
    assert r.get_json() == {'ok': True}                  # no server file paths
    assert r.headers['X-Content-Type-Options'] == 'nosniff' and 'Referrer-Policy' in r.headers
    assert 'X-Frame-Options' not in r.headers            # public, signed-out pages stay embeddable
    assert as_user(server, ids['owner']).get('/api/auth/status').headers['X-Frame-Options'] == 'SAMEORIGIN'
