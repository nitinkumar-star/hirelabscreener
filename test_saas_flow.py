"""
Self-serve SaaS flow (Nov 2026): AI cost meter in rupees, automatic sign-up
with a 30-day trial, online payment (Razorpay) that opens the ATS again.

Run:   pytest test_saas_flow.py -q
"""
import os
import sys
import json
import hmac
import hashlib
import tempfile
import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='saas_'))
    import server
    server.app.config['TESTING'] = True
    conn = server.get_db()
    c = conn.cursor()
    c.execute("INSERT INTO companies (name, status, plan, billing_status, created_at) "
              "VALUES ('SF Owner', 'active', 'owner', 'owner', '2026-01-01T00:00:00')")
    owner_co = c.lastrowid
    c.execute("INSERT INTO companies (name, status, plan, billing_status, trial_ends_at, created_at) "
              "VALUES ('SF Agency', 'active', 'standard', 'trial', ?, '2026-01-01T00:00:00')",
              ((datetime.datetime.now() + datetime.timedelta(days=10)).isoformat(),))
    co = c.lastrowid
    ids = {'co': co, 'owner_co': owner_co}
    for key, un, role, cid, adm in (('owner', 'sf_owner', 'admin', owner_co, 1), ('boss', 'sf_boss', 'user', co, 1),
                                    ('rec', 'sf_rec', 'user', co, 0)):
        c.execute("INSERT INTO users (username, password_hash, display_name, role, status, company_id, is_company_admin, "
                  "created_at) VALUES (?, ?, ?, ?, 'approved', ?, ?, '2026-01-01T00:00:00')",
                  (un, server.hash_password('secret-pass-1'), un.upper(), role, cid, adm))
        ids[key] = c.lastrowid
    conn.commit(); conn.close()
    server.set_setting('billing_usd_inr', '88')
    server.set_setting('billing_token_markup', '1.0')
    yield server, ids


def as_user(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


# ── 1. AI cost meter ───────────────────────────────────────────────────────
def test_ai_meter_per_recruiter_in_rupees(srv):
    server, ids = srv
    with server.app.test_request_context('/'):
        from flask import session
        session['user_id'] = ids['rec']
        server.log_api_usage('deepseek', 'deepseek-chat', input_tokens=1_000_000, output_tokens=0, endpoint='resume-parse')
    conn = server.get_db()
    row = conn.execute('SELECT company_id, user_id, cost_usd FROM api_usage ORDER BY id DESC LIMIT 1').fetchone()
    conn.execute('INSERT INTO api_usage (company_id, provider, endpoint, input_tokens, cost_usd, created_at, user_id) '
                 "VALUES (?, 'deepseek', 'email-agent', 10, 0.5, ?, 0)", (ids['co'], server.ts()))
    conn.commit(); conn.close()
    assert row['company_id'] == ids['co'] and row['user_id'] == ids['rec'] and round(row['cost_usd'], 2) == 0.27

    assert as_user(server, ids['rec']).get('/api/ai-meter').status_code == 403      # recruiters do not see costs
    d = as_user(server, ids['boss']).get('/api/ai-meter?days=30').get_json()
    people = {p['name']: p['rupees'] for p in d['people']}
    assert people == {'SF_REC': 23.76, 'Automatic (background)': 44.0} and d['total_rupees'] == 67.76
    assert {f['feature'] for f in d['features']} == {'CV reading', 'Email agent'} and len(d['daily']) == 30

    a = as_user(server, ids['owner']).get('/api/ai-meter/agencies').get_json()
    mine = next(x for x in a['agencies'] if x['company_id'] == ids['co'])
    assert mine['cost_rupees'] == 67.76 and mine['people'] == 1
    assert as_user(server, ids['boss']).get('/api/ai-meter/agencies').status_code == 403
    drill = as_user(server, ids['owner']).get('/api/ai-meter?company=%d' % ids['co']).get_json()
    assert drill['total_rupees'] == 67.76


# ── 2. automatic sign-up ───────────────────────────────────────────────────
def test_signup_starts_30_day_trial_and_signs_in(srv):
    server, ids = srv
    server._RL_BUCKETS.clear()
    st = server.app.test_client().get('/api/auth/status').get_json()
    assert st['state'] == 'login' and st['signup_auto'] is True and st['trial_days'] == 30
    c = server.app.test_client()
    r = c.post('/api/auth/signup', json={'username': 'sf_new', 'password': 'secret-pass-1', 'email': 'new@agency.in',
                                         'company_name': 'New Agency', 'accept_terms': True})
    j = r.get_json()
    assert r.status_code == 200 and j['pending'] is False and j['trial_days'] == 30
    assert c.get('/api/auth/status').get_json()['state'] == 'app'                    # signed in straight away
    conn = server.get_db()
    co = conn.execute("SELECT c.* FROM companies c JOIN users u ON u.company_id=c.id WHERE u.username='sf_new'").fetchone()
    conn.close()
    assert co['status'] == 'active' and co['billing_status'] == 'trial' and co['token_cap'] == 2000000
    assert (datetime.datetime.fromisoformat(co['trial_ends_at']) - datetime.datetime.now()).days == 29
    # same e-mail twice -> refused
    r = server.app.test_client().post('/api/auth/signup', json={'username': 'sf_new2', 'password': 'secret-pass-1',
                                                                 'email': 'NEW@agency.in', 'accept_terms': True})
    assert r.status_code == 400
    # owner switches automatic sign-up off -> pending, as before
    server.set_setting('signup_auto_approve', '0')
    r = server.app.test_client().post('/api/auth/signup', json={'username': 'sf_wait', 'password': 'secret-pass-1',
                                                                 'email': 'wait@agency.in', 'accept_terms': True})
    assert r.get_json()['pending'] is True
    server.set_setting('signup_auto_approve', '1')
    server._RL_BUCKETS.clear()


# ── 3. online payment ──────────────────────────────────────────────────────
@pytest.fixture
def razorpay(srv, monkeypatch):
    from modules import payments_online as po
    monkeypatch.setenv('RAZORPAY_KEY_ID', 'rzp_test_key')
    monkeypatch.setenv('RAZORPAY_KEY_SECRET', 'shh')
    monkeypatch.setenv('RAZORPAY_WEBHOOK_SECRET', 'hook')
    made = []

    class R:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def fake_post(url, auth=None, json=None, timeout=None):
        made.append({'url': url, 'auth': auth, 'json': json})
        return R({'id': 'order_%d' % len(made), 'amount': json['amount']})
    monkeypatch.setattr(po.requests, 'post', fake_post)
    return made


def _expire(server, cid, **cols):
    conn = server.get_db()
    for k, v in cols.items():
        conn.execute(f'UPDATE companies SET {k}=? WHERE id=?', (v, cid))
    conn.commit(); conn.close()


def test_pay_online_after_trial_opens_the_ats(srv, razorpay):
    server, ids = srv
    _expire(server, ids['co'], trial_ends_at=(datetime.datetime.now() - datetime.timedelta(days=1)).isoformat(),
            billing_status='trial')
    boss = as_user(server, ids['boss'])
    assert boss.get('/api/mandates').get_json()['code'] == 'trial_ended'
    cfg = boss.get('/api/billing/pay/config').get_json()
    assert cfg['enabled'] and cfg['can_pay'] and cfg['key_id'] == 'rzp_test_key' and cfg['amount_inr'] > 0
    assert as_user(server, ids['rec']).post('/api/billing/pay/order').status_code == 403   # only the agency admin pays
    o = boss.post('/api/billing/pay/order').get_json()
    assert o['ok'] and razorpay[-1]['auth'] == ('rzp_test_key', 'shh') and razorpay[-1]['json']['currency'] == 'INR'
    assert razorpay[-1]['json']['amount'] == o['amount_paise'] == int(round(cfg['amount_inr'] * 100))

    bad = boss.post('/api/billing/pay/verify', json={'razorpay_order_id': o['order_id'], 'razorpay_payment_id': 'pay_1',
                                                     'razorpay_signature': 'nope'})
    assert bad.status_code == 400
    sig = hmac.new(b'shh', (o['order_id'] + '|pay_1').encode(), hashlib.sha256).hexdigest()
    body = {'razorpay_order_id': o['order_id'], 'razorpay_payment_id': 'pay_1', 'razorpay_signature': sig}
    v = boss.post('/api/billing/pay/verify', json=body).get_json()
    assert v['ok'] and v['invoice_no'].startswith('HL-')
    assert (datetime.datetime.fromisoformat(v['paid_until']) - datetime.datetime.now()).days == 29
    assert boss.get('/api/mandates').status_code == 200                            # open again, automatically
    assert boss.post('/api/billing/pay/verify', json=body).get_json().get('already')  # no double payment

    # the webhook for the same payment is recorded once
    ev = json.dumps({'event': 'payment.captured', 'payload': {'payment': {'entity': {
        'id': 'pay_1', 'order_id': o['order_id'], 'amount': o['amount_paise']}}}}).encode()
    c = server.app.test_client()
    assert c.post('/api/billing/razorpay/webhook', data=ev, headers={'X-Razorpay-Signature': 'x'}).status_code == 400
    hs = hmac.new(b'hook', ev, hashlib.sha256).hexdigest()
    assert c.post('/api/billing/razorpay/webhook', data=ev, headers={'X-Razorpay-Signature': hs}).get_json()['ok']
    conn = server.get_db()
    n = conn.execute("SELECT COUNT(*) FROM payments WHERE gateway_ref='pay_1'").fetchone()[0]
    conn.close()
    assert n == 1


def test_renewal_due_then_webhook_payment_reopens(srv, razorpay):
    server, ids = srv
    _expire(server, ids['co'], paid_until=(datetime.datetime.now() - datetime.timedelta(days=5)).isoformat())
    boss = as_user(server, ids['boss'])
    r = boss.get('/api/mandates')
    assert r.status_code == 402 and r.get_json()['code'] == 'renewal_due'
    lr = server.app.test_client().post('/api/auth/login', json={'username': 'sf_boss', 'password': 'secret-pass-1'})
    assert lr.status_code == 402 and lr.get_json()['code'] == 'renewal_due'
    o = boss.post('/api/billing/pay/order').get_json()
    ev = json.dumps({'event': 'order.paid', 'payload': {'payment': {'entity': {
        'id': 'pay_2', 'order_id': o['order_id'], 'amount': o['amount_paise']}}}}).encode()
    hs = hmac.new(b'hook', ev, hashlib.sha256).hexdigest()
    assert server.app.test_client().post('/api/billing/razorpay/webhook', data=ev,
                                         headers={'X-Razorpay-Signature': hs}).get_json()['ok']
    assert boss.get('/api/mandates').status_code == 200
    # within the grace days a lapsed month still works; the owner's "Activate" lifts a lapse too
    _expire(server, ids['co'], paid_until=(datetime.datetime.now() - datetime.timedelta(days=1)).isoformat())
    assert boss.get('/api/mandates').status_code == 200
    _expire(server, ids['co'], paid_until=(datetime.datetime.now() - datetime.timedelta(days=9)).isoformat())
    assert boss.get('/api/mandates').status_code == 402
    assert as_user(server, ids['owner']).post(f"/api/admin/billing/{ids['co']}/status", json={'status': 'active'}).status_code == 200
    assert boss.get('/api/mandates').status_code == 200


def test_online_payment_off_without_keys(srv, monkeypatch):
    server, ids = srv
    for k in ('RAZORPAY_KEY_ID', 'RAZORPAY_KEY_SECRET'):
        monkeypatch.delenv(k, raising=False)
    boss = as_user(server, ids['boss'])
    assert boss.get('/api/billing/pay/config').get_json()['enabled'] is False
    assert boss.post('/api/billing/pay/order').status_code == 400
