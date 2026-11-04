"""
Chase client for feedback (modules/job_pulse.py): goes to everyone the profiles
were shared with (To + CC), replies inside the share e-mail's thread with that
e-mail quoted below, and attaches the CVs again.

Run:   pytest test_client_chase.py -q
"""
import os
import sys
import email
import time
import tempfile
import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SENT = []


class FakeSMTP:
    def __init__(self, *a, **k):
        pass

    def starttls(self, *a, **k):
        pass

    def ehlo(self, *a, **k):
        pass

    def login(self, *a, **k):
        pass

    def sendmail(self, frm, rcpts, raw):
        SENT.append({'from': frm, 'rcpts': list(rcpts), 'msg': email.message_from_string(raw)})

    def quit(self):
        pass


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='chase_'))
    import server
    server.app.config['TESTING'] = True
    conn = server.get_db()
    c = conn.cursor()
    c.execute("INSERT INTO companies (name, status, plan, billing_status, created_at) "
              "VALUES ('CH Agency', 'active', 'standard', 'active', '2026-01-01T00:00:00')")
    co = c.lastrowid
    c.execute("INSERT INTO users (username, password_hash, display_name, role, status, company_id, is_company_admin, created_at) "
              "VALUES ('ch_boss', 'x', 'Boss', 'user', 'approved', ?, 1, '2026-01-01T00:00:00')", (co,))
    uid = c.lastrowid
    for k, v in (('smtp_email', 'agency@gmail.com'), ('smtp_app_password', 'abcdefghijklmnop'),
                 ('smtp_display_name', 'CH Agency')):
        c.execute('INSERT OR REPLACE INTO tenant_settings (company_id, key, value) VALUES (?,?,?)', (co, k, v))
    c.execute("INSERT INTO crm_clients (company_id, name, created_at) VALUES (?, 'Polycab', '2026-01-01')", (co,))
    client_id = c.lastrowid
    c.execute("INSERT INTO crm_contacts (company_id, client_id, name, email, is_active) VALUES (?,?,?,?,1)",
              (co, client_id, 'Anita Rao', 'anita@polycab.com'))
    spoc = c.lastrowid
    c.execute("INSERT INTO mandates (client, role, status, owner_id, created_at, spoc_contact_id, crm_client_id) "
              "VALUES ('Polycab', 'Area Sales Manager', 'active', ?, '2026-10-01', ?, ?)", (co, spoc, client_id))
    mid = c.lastrowid
    old = (datetime.datetime.now() - datetime.timedelta(days=6)).isoformat(timespec='seconds')
    cids = []
    for i, name in enumerate(('Ravi Kumar', 'Sana Shaikh')):
        c.execute("INSERT INTO candidates (mandate_id, name, stage, owner_id, email, created_at, updated_at) "
                  "VALUES (?, ?, 'Screening', ?, ?, ?, ?)", (mid, name, co, 'c%d@x.com' % i, old, old))
        cid = c.lastrowid
        cids.append(cid)
        fn = 'chase_%d.pdf' % cid
        with open(os.path.join(server.CV_DIR, fn), 'wb') as f:
            f.write(b'%PDF-1.4 cv of ' + name.encode())
        c.execute("UPDATE candidates SET cv_path=?, cv_original_name='resume.pdf' WHERE id=?", (fn, cid))
    conn.commit(); conn.close()
    yield server, {'co': co, 'uid': uid, 'mid': mid, 'cids': cids}


@pytest.fixture
def smtp(srv, monkeypatch):
    server = srv[0]
    SENT.clear()
    monkeypatch.setattr(server.smtplib, 'SMTP', FakeSMTP)
    monkeypatch.setattr(server, 'smtp_open', lambda *a, **k: (FakeSMTP(), 'starttls'))
    return SENT


def client(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


def _share(server, ids, c):
    r = c.post('/api/mandates/%d/submit-to-client' % ids['mid'], json={
        'to': ['anita@polycab.com', 'hr@polycab.com'], 'cc': ['vp.sales@polycab.com', 'team@agency.in'],
        'subject': 'Area Sales Manager — 2 profiles | CH Agency', 'greeting': 'Dear Anita,',
        'intro': 'Please find attached 2 profiles:', 'candidate_ids': ids['cids']})
    assert r.status_code == 200, r.get_json()
    # move both to "Shared with Client" 6 days ago, as the pipeline does
    conn = server.get_db()
    old = (datetime.datetime.now() - datetime.timedelta(days=6)).isoformat(timespec='seconds')
    for cid in ids['cids']:
        conn.execute("UPDATE candidates SET stage='Shared with Client', updated_at=? WHERE id=?", (old, cid))
        conn.execute("INSERT INTO stage_history (candidate_id, from_stage, to_stage, note, created_at) "
                     "VALUES (?, 'Screening', 'Shared with Client', '', ?)", (cid, old))
    conn.commit(); conn.close()


def test_chase_replies_in_share_thread_to_all_with_cvs(srv, smtp):
    server, ids = srv
    c = client(server, ids['uid'])
    _share(server, ids, c)
    share = smtp[-1]['msg']
    share_id = share['Message-ID']
    assert share_id and len([p for p in share.walk() if p.get_filename()]) == 2

    d = c.get('/api/job-pulse/chase?mandate_id=%d' % ids['mid']).get_json()
    assert d['ok'] and d['to'] == ['anita@polycab.com', 'hr@polycab.com']
    assert d['cc'] == ['vp.sales@polycab.com', 'team@agency.in']
    assert d['reply_to']['message_id'] == share_id and d['subject'] == 'Re: Area Sales Manager — 2 profiles | CH Agency'
    assert [x['has_cv'] for x in d['candidates']] == [True, True] and 'attached again' in d['body']

    smtp.clear()
    r = c.post('/api/job-pulse/chase', json={
        'mandate_id': ids['mid'], 'to': ', '.join(d['to']), 'cc': ', '.join(d['cc']), 'subject': d['subject'],
        'body': d['body'], 'candidate_ids': [x['id'] for x in d['candidates']],
        'attach_ids': [d['candidates'][0]['id']], 'in_reply_to': share_id}).get_json()
    assert r['ok'] and r['attached'] == 1 and r['threaded']
    sent = smtp[-1]
    m = sent['msg']
    assert set(sent['rcpts']) == {'anita@polycab.com', 'hr@polycab.com', 'vp.sales@polycab.com', 'team@agency.in'}
    assert m['In-Reply-To'] == share_id and share_id in m['References'] and m['Cc'] == 'vp.sales@polycab.com, team@agency.in'
    files = [p.get_filename() for p in m.walk() if p.get_filename()]
    assert files == [d['candidates'][0]['cv_name']]
    html = next(p for p in m.walk() if p.get_content_type() == 'text/html').get_payload(decode=True).decode()
    assert 'wrote:' in html and 'Please find attached 2 profiles' in html          # the share e-mail, quoted
    assert html.index('Following up') < html.index('wrote:')


def test_chase_falls_back_to_job_cc_and_ignores_foreign_thread(srv, smtp):
    server, ids = srv
    conn = server.get_db()
    conn.execute('DELETE FROM client_submissions WHERE mandate_id=?', (ids['mid'],))
    conn.execute("DELETE FROM emails WHERE owner_id=? AND folder='Sent'", (ids['co'],))
    conn.execute("UPDATE mandates SET cc_internal_emails='[\"lead@agency.in\"]' WHERE id=?", (ids['mid'],))
    conn.commit(); conn.close()
    c = client(server, ids['uid'])
    d = c.get('/api/job-pulse/chase?mandate_id=%d' % ids['mid']).get_json()
    assert d['to'] == ['anita@polycab.com'] and d['cc'] == ['lead@agency.in'] and d['reply_to'] is None
    assert d['subject'] == 'Feedback on profiles — Area Sales Manager'
    r = c.post('/api/job-pulse/chase', json={'mandate_id': ids['mid'], 'to': 'anita@polycab.com', 'cc': 'not-an-email',
                                             'subject': 'x', 'body': 'y'})
    assert r.status_code == 400
    r = c.post('/api/job-pulse/chase', json={'mandate_id': ids['mid'], 'to': 'anita@polycab.com', 'subject': 'x',
                                             'body': 'y', 'in_reply_to': '<someone-else@x>'}).get_json()
    assert r['ok'] and not r['threaded'] and 'In-Reply-To' not in smtp[-1]['msg']


def test_old_share_found_in_synced_sent_mail(srv, smtp):
    server, ids = srv
    conn = server.get_db()
    conn.execute("INSERT INTO emails (owner_id, msg_id, folder, from_addr, to_addr, cc, subject, body, body_html, date_ts, "
                 "created_at, user_id) VALUES (?, '<old-share@gmail.com>', 'Sent', 'agency@gmail.com', "
                 "'Anita Rao <anita@polycab.com>', 'ceo@polycab.com', 'Profiles: Area Sales Manager', 'old body', "
                 "'<p>old body</p>', ?, '2026-10-20T10:00:00', 0)", (ids['co'], time.time() - 5 * 86400))
    conn.commit(); conn.close()
    d = client(server, ids['uid']).get('/api/job-pulse/chase?mandate_id=%d' % ids['mid']).get_json()
    assert d['reply_to']['message_id'] == '<old-share@gmail.com>' and d['reply_to']['from_mailbox']
    assert d['to'] == ['anita@polycab.com'] and d['cc'] == ['ceo@polycab.com', 'lead@agency.in']   # + the job's CC list
    assert d['subject'] == 'Re: Profiles: Area Sales Manager'


def test_gmail_share_found_by_candidate_names_and_reply_goes_to_latest_in_thread(srv, smtp):
    """CVs sent straight from Gmail with a free-text subject, and the client already replied in that thread."""
    server, ids = srv
    now = time.time()
    conn = server.get_db()
    conn.execute("DELETE FROM emails WHERE owner_id=?", (ids['co'],))
    conn.execute("INSERT INTO emails (owner_id, msg_id, folder, from_addr, to_addr, cc, subject, body, body_html, date_ts, "
                 "created_at, user_id, thread_id) VALUES (?, '<gm-share@gmail.com>', 'Sent', 'agency@gmail.com', "
                 "'hr@polycab.com', 'Deepak <deepak@polycab.com>', 'CVs for your review', "
                 "'Hi, sharing Ravi Kumar and Sana Shaikh for your opening.', '<p>sharing Ravi Kumar and Sana Shaikh</p>', ?, "
                 "'2026-10-28T10:00:00', 0, 'conv-x')", (ids['co'], now - 6 * 86400))
    conn.execute("INSERT INTO emails (owner_id, msg_id, folder, from_addr, to_addr, subject, body, body_html, date_ts, "
                 "created_at, user_id, thread_id) VALUES (?, '<client-reply@polycab.com>', 'Inbox', 'hr@polycab.com', "
                 "'agency@gmail.com', 'Re: CVs for your review', 'Thanks, will revert by Friday.', "
                 "'<p>Thanks, will revert by Friday.</p>', ?, '2026-10-29T10:00:00', 0, 'conv-x')", (ids['co'], now - 5 * 86400))
    # unrelated mail to someone else, same job title, must not be picked
    conn.execute("INSERT INTO emails (owner_id, msg_id, folder, from_addr, to_addr, subject, body, date_ts, created_at, user_id) "
                 "VALUES (?, '<other@gmail.com>', 'Sent', 'agency@gmail.com', 'someone@other.com', 'Area Sales Manager JD', "
                 "'jd', ?, '2026-10-30T10:00:00', 0)", (ids['co'], now - 4 * 86400))
    # Mandate Setup: a client-side CC contact (from CRM) + an internal one
    cl = conn.execute('SELECT client_id FROM crm_contacts WHERE email=?', ('anita@polycab.com',)).fetchone()['client_id']
    cur = conn.execute("INSERT INTO crm_contacts (company_id, client_id, name, email, is_active) VALUES (?,?,?,?,1)",
                       (ids['co'], cl, 'Vikas (Head HR)', 'vikas@polycab.com'))
    conn.execute("UPDATE mandates SET cc_external_ids=?, cc_internal_emails='[\"lead@agency.in\"]' WHERE id=?",
                 ('[%d]' % cur.lastrowid, ids['mid']))
    conn.commit(); conn.close()
    c = client(server, ids['uid'])
    d = c.get('/api/job-pulse/chase?mandate_id=%d' % ids['mid']).get_json()
    assert [o['share_id'] for o in d['options']] == ['<gm-share@gmail.com>']
    o = d['options'][0]
    assert o['message_id'] == '<client-reply@polycab.com>' and set(o['matched']) == {'Ravi Kumar', 'Sana Shaikh'}
    # the SPOC was not on that e-mail -> added to To; the job's CC contacts join the e-mail's CC
    assert d['to'] == ['anita@polycab.com', 'hr@polycab.com']
    assert d['cc'] == ['deepak@polycab.com', 'vikas@polycab.com', 'lead@agency.in'] and d['subject'] == 'Re: CVs for your review'
    assert o['to'] == d['to'] and o['cc'] == d['cc']
    assert d['new_to'] == ['anita@polycab.com'] and d['new_cc'] == ['vikas@polycab.com', 'lead@agency.in']
    smtp.clear()
    r = c.post('/api/job-pulse/chase', json={'mandate_id': ids['mid'], 'to': 'hr@polycab.com', 'cc': 'deepak@polycab.com',
                                             'subject': d['subject'], 'body': 'Gentle reminder', 'in_reply_to': o['message_id'],
                                             'attach_ids': [x['id'] for x in d['candidates']]}).get_json()
    assert r['ok'] and r['threaded'] and r['attached'] == 2
    m = smtp[-1]['msg']
    html = next(p for p in m.walk() if p.get_content_type() == 'text/html').get_payload(decode=True).decode()
    assert m['In-Reply-To'] == '<client-reply@polycab.com>' and 'will revert by Friday' in html
