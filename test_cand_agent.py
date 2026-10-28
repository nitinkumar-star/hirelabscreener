"""
Candidate AI Agent (Phase 1) — instruction -> approved e-mail / WhatsApp draft / reminder.

Run:   pytest test_cand_agent.py -q
Boots the real server.py against a throw-away DATA_DIR. The AI call and the
SMTP send are replaced by fakes; everything else (tables, guards, scheduling,
notifications, reminders) is the real code.
"""
import os
import sys
import json
import datetime
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

NOW = datetime.datetime(2026, 10, 28, 14, 5)


@pytest.fixture(scope='module')
def srv():
    os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='cagent_')
    import server
    from modules import cand_agent as ca
    server.app.config['TESTING'] = True
    conn = server.get_db()
    c = conn.cursor()

    def company(name):
        c.execute("INSERT INTO companies (name, status, created_at) VALUES (?, 'active', '2026-01-01T00:00:00')", (name,))
        return c.lastrowid

    def user(uname, cid, admin=0):
        c.execute("INSERT INTO users (username, password_hash, display_name, role, status, company_id, "
                  "is_company_admin, created_at) VALUES (?, 'x', ?, 'user', 'approved', ?, ?, '2026-01-01T00:00:00')",
                  (uname, uname.title(), cid, admin))
        return c.lastrowid

    A, B = company('Agent Agency A'), company('Agent Agency B')
    ids = {'A': A, 'B': B, 'a_admin': user('agadmin', A, admin=1), 'a_rec': user('agrec', A),
           'b_admin': user('agbadmin', B, admin=1)}
    c.execute("INSERT INTO mandates (client, role, location, jd, status, owner_id, created_at) "
              "VALUES ('Infosys', 'Java Lead', 'Pune', 'Java 11, Spring Boot, 8+ years', 'active', ?, '2026-10-01')", (A,))
    ids['mid'] = c.lastrowid

    def cand(name, email, phone, **extra):
        cols = {'mandate_id': ids['mid'], 'name': name, 'email': email, 'phone': phone, 'owner_id': A,
                'stage': 'sourced', 'company': 'TCS', 'created_at': '2026-10-01T10:00:00'}
        cols.update(extra)
        c.execute('INSERT INTO candidates (%s) VALUES (%s)' % (','.join(cols), ','.join('?' * len(cols))),
                  list(cols.values()))
        return c.lastrowid

    ids['rahul'] = cand('Rahul Sharma', 'rahul@example.com', '9876543210')
    ids['unsub'] = cand('Unsub Person', 'unsub@example.com', '9876500000', do_not_email=1)
    conn.commit(); conn.close()

    sent = []

    def fake_send(to, subject, body, **kw):
        sent.append({'to': to, 'subject': subject, 'body': body, **kw})
        return True, '<msg-%d@x>' % len(sent), '<msg-%d@x>' % len(sent), ''
    orig = (server.email_service_send, ca._now, ca.ai_plan)
    server.email_service_send = fake_send
    ca._now = lambda: NOW
    ca._rl.clear()
    yield server, ca, ids, sent
    server.email_service_send, ca._now, ca.ai_plan = orig   # other test modules share this server


def cl_for(srv, uid):
    server = srv[0]
    cl = server.app.test_client()
    with cl.session_transaction() as s:
        s['user_id'] = uid
    return cl


def post(cl, url, body=None):
    return cl.post(url, data=json.dumps(body or {}), content_type='application/json')


def fake_plan(ca, plan, seen=None):
    def _p(context, instruction):
        if seen is not None:
            seen.append((context, instruction))
        return plan
    ca.ai_plan = _p


def db(srv):
    return srv[0].get_db()


# ── planning ──────────────────────────────────────────────────────────────
def test_plan_reads_context_and_masks_contacts(srv):
    server, ca, ids, _ = srv
    seen = []
    fake_plan(ca, {'understood': 'Follow-up e-mail tomorrow 11 AM, plus a WhatsApp nudge.',
                   'actions': [{'type': 'email', 'when': '2026-10-29T11:00', 'time_stated': True,
                                'subject': 'Java Lead – Infosys', 'body': 'Hi Rahul, following up on the JD…'},
                               {'type': 'whatsapp', 'when': '2026-10-29T11:30', 'time_stated': True,
                                'body': 'Hi Rahul, sent you the JD by mail. – Agadmin'}],
                   'question': None}, seen)
    cl = cl_for(srv, ids['a_admin'])
    r = post(cl, f'/api/candidates/{ids["rahul"]}/agent/plan',
             {'instruction': 'Please drop a follow up email for the JD at 11 AM tomorrow'})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d['understood'].startswith('Follow-up')
    tasks = {t['action']: t for t in d['tasks']}
    assert tasks['email']['status'] == 'draft' and tasks['email']['run_at'] == '2026-10-29T11:00:00'
    assert tasks['email']['to_addr'] == 'rahul@example.com'
    assert tasks['whatsapp']['wa_number'] == '919876543210'
    ctx = seen[0][0]
    assert 'Java Lead' in ctx and 'Spring Boot' in ctx and 'Rahul Sharma' in ctx
    assert 'rahul@example.com' not in ctx and '9876543210' not in ctx


def test_no_time_defaults_to_next_working_morning_and_answer_needs_no_approval(srv):
    server, ca, ids, _ = srv
    fake_plan(ca, {'understood': 'x', 'actions': [
        {'type': 'reminder', 'when': None, 'time_stated': False, 'body': 'Call Rahul about notice period'},
        {'type': 'answer', 'body': 'Notice period is not recorded.'}]})
    cl = cl_for(srv, ids['a_admin'])
    d = post(cl, f'/api/candidates/{ids["rahul"]}/agent/plan', {'instruction': 'remind me to call him'}).get_json()
    rem = [t for t in d['tasks'] if t['action'] == 'reminder'][0]
    ans = [t for t in d['tasks'] if t['action'] == 'answer'][0]
    assert rem['run_at'] == '2026-10-29T10:00:00' and rem['time_stated'] == 0
    assert ans['status'] == 'done'
    # approving the reminder creates a real task for the recruiter
    r = post(cl, f'/api/candidates/{ids["rahul"]}/agent/tasks/{rem["id"]}/approve', {})
    assert r.status_code == 200 and r.get_json()['task']['status'] == 'done'
    conn = db(srv)
    row = conn.execute('SELECT * FROM reminders WHERE id=?', (r.get_json()['task']['reminder_id'],)).fetchone()
    conn.close()
    assert row['note'] == 'Call Rahul about notice period' and row['due_at'] == '2026-10-29T10:00'
    assert row['assigned_to'] == ids['a_admin'] and row['candidate_id'] == ids['rahul']


def test_default_time_skips_sunday_and_past_hours():
    from modules.cand_agent import default_time
    sat_eve = datetime.datetime(2026, 10, 31, 18, 0)          # Saturday
    assert default_time(sat_eve) == datetime.datetime(2026, 11, 2, 10, 0)   # Monday
    assert default_time(datetime.datetime(2026, 10, 28, 9, 0)) == datetime.datetime(2026, 10, 28, 10, 0)


# ── e-mail: scheduled, sent by the worker, held on a reply ────────────────
def _email_task(srv, cl, when='2026-10-29T11:00'):
    server, ca, ids, _ = srv
    fake_plan(ca, {'understood': 'x', 'actions': [{'type': 'email', 'when': when, 'time_stated': True,
                                                  'subject': 'Java Lead', 'body': 'Hi Rahul'}]})
    d = post(cl, f'/api/candidates/{ids["rahul"]}/agent/plan', {'instruction': 'mail him tomorrow 11'}).get_json()
    return [t for t in d['tasks'] if t['action'] == 'email' and t['status'] == 'draft'][0]


def test_email_scheduled_then_sent_at_time(srv):
    server, ca, ids, sent = srv
    cl = cl_for(srv, ids['a_admin'])
    t = _email_task(srv, cl)
    r = post(cl, f'/api/candidates/{ids["rahul"]}/agent/tasks/{t["id"]}/approve',
             {'subject': 'Java Lead – follow-up', 'body': 'Hi Rahul, edited'})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['task']['status'] == 'scheduled'
    n0 = len(sent)
    assert ca.run_due(datetime.datetime(2026, 10, 29, 10, 59)) == 0 and len(sent) == n0
    assert ca.run_due(datetime.datetime(2026, 10, 29, 11, 0)) == 1
    assert sent[-1]['to'] == 'rahul@example.com' and sent[-1]['body'] == 'Hi Rahul, edited'
    conn = db(srv)
    st = conn.execute('SELECT status FROM cand_agent_tasks WHERE id=?', (t['id'],)).fetchone()['status']
    ev = conn.execute("SELECT detail FROM candidate_events WHERE candidate_id=? AND event_type='email' "
                      "ORDER BY id DESC LIMIT 1", (ids['rahul'],)).fetchone()['detail']
    nt = conn.execute("SELECT * FROM notifications WHERE user_id=? AND kind='agent_sent'", (ids['a_admin'],)).fetchall()
    conn.close()
    assert st == 'sent' and 'Java Lead – follow-up' in ev and len(nt) == 1


def test_email_held_when_candidate_replied_then_send_anyway(srv):
    server, ca, ids, sent = srv
    cl = cl_for(srv, ids['a_admin'])
    t = _email_task(srv, cl)
    post(cl, f'/api/candidates/{ids["rahul"]}/agent/tasks/{t["id"]}/approve', {})
    conn = db(srv)
    conn.execute("INSERT INTO email_messages (company_id,candidate_id,direction,from_addr,to_addr,subject,body,"
                 "message_id,sent_at,created_at) VALUES (?,?,'received','rahul@example.com','me','Re','Interested!',"
                 "'<in-1>','2099-01-01T00:00:00','2099-01-01T00:00:00')", (ids['A'], ids['rahul']))
    conn.commit(); conn.close()
    n0 = len(sent)
    ca.run_due(datetime.datetime(2026, 10, 29, 11, 1))
    conn = db(srv)
    row = conn.execute('SELECT status, reason FROM cand_agent_tasks WHERE id=?', (t['id'],)).fetchone()
    conn.close()
    assert row['status'] == 'held' and 'replied' in row['reason'] and len(sent) == n0
    r = post(cl, f'/api/candidates/{ids["rahul"]}/agent/tasks/{t["id"]}/approve', {})
    assert r.status_code == 200 and r.get_json()['task']['status'] == 'sent' and len(sent) == n0 + 1


def test_approve_rules(srv):
    server, ca, ids, _ = srv
    cl = cl_for(srv, ids['a_admin'])
    t = _email_task(srv, cl)
    url = f'/api/candidates/{ids["rahul"]}/agent/tasks/{t["id"]}'
    assert post(cl, url + '/approve', {'run_at': '2026-10-27T09:00'}).status_code == 400   # time passed
    assert post(cl, url + '/approve', {'to': 'not-an-email'}).status_code == 400
    assert post(cl, url + '/cancel').get_json()['task']['status'] == 'cancelled'
    assert post(cl, url + '/approve', {}).status_code == 409
    # unsubscribed candidate: the draft carries the warning and cannot be approved
    fake_plan(ca, {'understood': 'x', 'actions': [{'type': 'email', 'when': None, 'subject': 's', 'body': 'b'}]})
    d = post(cl, f'/api/candidates/{ids["unsub"]}/agent/plan', {'instruction': 'mail'}).get_json()
    u = d['tasks'][0]
    assert 'unsubscribed' in u['error']
    assert post(cl, f'/api/candidates/{ids["unsub"]}/agent/tasks/{u["id"]}/approve', {}).status_code == 400


# ── WhatsApp: draft -> notification at the time -> recruiter sends ────────
def test_whatsapp_draft_ready_and_marked_sent(srv):
    server, ca, ids, _ = srv
    cl = cl_for(srv, ids['a_admin'])
    fake_plan(ca, {'understood': 'x', 'actions': [{'type': 'whatsapp', 'when': '2026-10-29T18:00',
                                                  'time_stated': True, 'body': 'Hi Rahul!'}]})
    d = post(cl, f'/api/candidates/{ids["rahul"]}/agent/plan', {'instruction': 'whatsapp him at 6 pm tomorrow'}).get_json()
    t = [x for x in d['tasks'] if x['action'] == 'whatsapp' and x['status'] == 'draft'][0]
    url = f'/api/candidates/{ids["rahul"]}/agent/tasks/{t["id"]}'
    assert post(cl, url + '/approve', {}).get_json()['task']['status'] == 'scheduled'
    ca.run_due(datetime.datetime(2026, 10, 29, 18, 0))
    conn = db(srv)
    assert conn.execute('SELECT status FROM cand_agent_tasks WHERE id=?', (t['id'],)).fetchone()['status'] == 'ready'
    assert conn.execute("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND kind='agent_wa'",
                        (ids['a_admin'],)).fetchone()['n'] == 1
    conn.close()
    # no Business API configured -> API send refused, manual send recorded
    assert post(cl, url + '/wa-send', {'body': 'Hi Rahul!!'}).status_code == 400
    r = post(cl, url + '/wa-done', {'body': 'Hi Rahul!!'})
    assert r.status_code == 200 and r.get_json()['task']['status'] == 'sent'
    conn = db(srv)
    ev = conn.execute("SELECT detail FROM candidate_events WHERE candidate_id=? AND event_type='whatsapp' "
                      "ORDER BY id DESC LIMIT 1", (ids['rahul'],)).fetchone()['detail']
    conn.close()
    assert 'Hi Rahul!!' in ev


# ── access ────────────────────────────────────────────────────────────────
def test_other_company_and_unassigned_recruiter_are_refused(srv):
    server, ca, ids, _ = srv
    fake_plan(ca, {'understood': 'x', 'actions': []})
    other = cl_for(srv, ids['b_admin'])
    assert other.get(f'/api/candidates/{ids["rahul"]}/agent').status_code == 404
    assert post(other, f'/api/candidates/{ids["rahul"]}/agent/plan', {'instruction': 'mail him'}).status_code == 404
    rec = cl_for(srv, ids['a_rec'])           # recruiter not assigned to the job
    assert post(rec, f'/api/candidates/{ids["rahul"]}/agent/plan', {'instruction': 'mail him'}).status_code in (403, 404)
    assert cl_for(srv, ids['a_admin']).get(f'/api/candidates/{ids["rahul"]}/agent').get_json()['ok']
