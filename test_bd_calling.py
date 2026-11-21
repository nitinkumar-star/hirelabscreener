"""
BD workspace -> Calling mode (cold-calling sheet)  — modules/bd_calling.py

Run:   pytest test_bd_calling.py -q
"""
import json
import time

import pytest

from test_naukri_screen import srv, cl, CO, ADMIN, RIYA, OADMIN  # noqa: F401  (fixtures)


class FakeResp:
    status_code = 200

    def __init__(self, obj):
        self._obj = obj

    def json(self):
        return {'choices': [{'message': {'content': json.dumps(self._obj)}}], 'usage': {}}


@pytest.fixture()
def ai(srv, monkeypatch):
    seen = []

    def fake(key, payload, timeout=60, endpoint=''):
        sysmsg = payload['messages'][0]['content']
        user = payload['messages'][1]['content']
        seen.append({'endpoint': endpoint, 'user': user})
        if 'website text' in user.lower() or 'fill what you can' in sysmsg:
            return FakeResp({'what_they_do': 'LV switchgear and panels', 'locations': 'Pune plant, Delhi office',
                             'roles_hired': 'Sales engineers, design engineers', 'why_now': 'New plant in Pune',
                             'industry': 'Electrical', 'city': 'Pune', 'size': ''})
        if 'wrote or said' in sysmsg:
            return FakeResp({'hiring_pain': 'Sales engineers leave in 1 year', 'decision_maker': 'Priya, HR head',
                             'current_agency': 'Naukri only', 'contact_name': 'Priya'})
        if 'prepare a recruiter for a cold call' in sysmsg:
            return FakeResp({'openers': [{'style': 'trigger', 'label': 'Reason-for-calling', 'script': 'Hi Priya, Nitin here - saw the new Pune plant. Is this a bad time?'},
                                         {'style': 'insight', 'label': 'Insight-led', 'script': 'We are a leading recruitment agency with a huge database.'}],
                             'credibility': 'We fill sales engineer roles in switchgear.', 'discovery': ['Which role is hardest?', 'What happens when it stays empty?'],
                             'objections': [{'q': 'We do not use agencies', 'a': 'Fair.'}], 'next_step': '15-minute intake call',
                             'avoid': ['any vacancies?']})
        if 'cold-call opener' in sysmsg:
            return FakeResp({'opener': 'Hi Priya, this is Nitin...', 'questions': ['How many sales engineers?'],
                             'objections': [{'q': 'We do not use agencies', 'a': 'Fair - one profile, no commitment.'}]})
        if 'follow-up' in sysmsg:
            return FakeResp({'body': 'Hi Priya, a quick follow-up. Nitin'})
        return FakeResp({'variants': [
            {'style': 'short', 'subjects': ['Sales engineers for your Pune plant', 'B', 'C'], 'body': 'Hi Priya, short. Nitin'},
            {'style': 'value', 'subjects': ['V1'], 'body': 'Hi Priya, value. Nitin'},
            {'style': 'candidate', 'subjects': ['C1'], 'body': 'Hi Priya, candidate. Nitin'}]})
    monkeypatch.setattr(srv, 'call_deepseek', fake)
    real = srv.get_setting
    monkeypatch.setattr(srv, 'get_setting', lambda k, default='': 'test-key' if k == 'deepseek_api_key' else real(k, default))
    return seen


@pytest.fixture()
def mail(srv, monkeypatch):
    sent = []

    def fake_send(to, subject, body_text, body_html=None, in_reply_to='', **kw):
        sent.append({'to': to, 'subject': subject, 'body': body_text, 'html': body_html or '', 'in_reply_to': in_reply_to,
                     'uid': srv._mail_sender_uid()})
        return True, None, '<msg%d@test>' % len(sent), 't%d' % len(sent)
    monkeypatch.setattr(srv, 'email_service_send', fake_send)
    return sent


def _prospect(a, name, **kw):
    r = a.post('/api/bd/records/companies', json=dict({'name': name}, **kw))
    assert r.status_code == 200, r.get_json()
    return r.get_json()['record']['id']


def _rows(a, view='all', q=''):
    j = a.get('/api/bd/calling/list?view=%s&q=%s' % (view, q)).get_json()
    assert j['ok'], j
    return j


def test_sheet_lists_prospects_only_and_edits_cells(srv):
    a = cl(srv, ADMIN)
    pid = _prospect(a, 'Calltest Switchgear', website='calltest-sg.in')
    a.post('/api/crm/clients', json={'name': 'Calltest Real Client'})
    names = [r['name'] for r in _rows(a, 'all', 'calltest')['rows']]
    assert 'Calltest Switchgear' in names and 'Calltest Real Client' not in names
    # typing a phone on a row with no contact creates the contact
    r = a.patch('/api/bd/calling/companies/%d' % pid, json={'contact_name': 'Priya Nair'}).get_json()
    assert r['ok'] and r['row']['contact']['name'] == 'Priya Nair'
    r = a.patch('/api/bd/calling/companies/%d' % pid, json={'phone': '98100 00001', 'designation': 'HR Head',
                                                             'city': 'Pune'}).get_json()
    assert r['row']['contact']['phone'] == '98100 00001' and r['row']['city'] == 'Pune'
    assert a.patch('/api/bd/calling/companies/%d' % pid, json={'email': 'not-an-email'}).status_code == 400
    assert a.patch('/api/bd/calling/companies/%d' % pid, json={'name': 'Calltest Real Client'}).status_code == 400


def test_outcomes_set_the_next_step_and_today_order(srv):
    a = cl(srv, ADMIN)
    cb = _prospect(a, 'Calltest Callback Co')
    na = _prospect(a, 'Calltest NoAnswer Co')
    ni = _prospect(a, 'Calltest NotInterested Co')
    _prospect(a, 'Calltest Fresh Co')
    today = srv.ts()[:10]
    r = a.post('/api/bd/calling/companies/%d/outcome' % cb, json={'status': 'callback', 'next_at': today + 'T00:05',
                                                                  'note': 'call after lunch'}).get_json()
    assert r['ok'] and r['row']['next_at'].startswith(today) and r['today']['calls'] >= 1
    conn = srv.get_db()
    task = conn.execute("SELECT * FROM crm_activities WHERE client_id=? AND activity_type='followup' AND status='open'",
                        (cb,)).fetchone()
    assert task and task['due_at'].startswith(today)
    r = a.post('/api/bd/calling/companies/%d/outcome' % na, json={'status': 'no_answer'}).get_json()
    assert r['row']['attempts'] == 1 and r['row']['next_at']
    assert a.post('/api/bd/calling/companies/%d/outcome' % ni, json={'status': 'not_interested'}).status_code == 400
    r = a.post('/api/bd/calling/companies/%d/outcome' % ni, json={'status': 'not_interested', 'reason': 'fee'}).get_json()
    assert r['row']['next_at'] > srv.ts() and r['row']['status'] == 'not_interested'
    assert a.post('/api/bd/calling/companies/%d/outcome' % ni, json={'status': 'meeting'}).status_code == 400
    # a second outcome closes the earlier callback task
    a.post('/api/bd/calling/companies/%d/outcome' % cb, json={'status': 'interested'})
    assert conn.execute('SELECT status FROM crm_activities WHERE id=?', (task['id'],)).fetchone()['status'] == 'done'
    calls = conn.execute("SELECT COUNT(*) FROM crm_activities WHERE client_id=? AND activity_type='call'", (cb,)).fetchone()[0]
    assert calls == 2
    conn.close()
    a.post('/api/bd/calling/companies/%d/next' % cb, json={'next_at': today + 'T00:01'})
    order = [r['name'] for r in _rows(a, 'today', 'calltest')['rows']]
    assert order.index('Calltest Callback Co') < order.index('Calltest Fresh Co')
    assert 'Calltest NotInterested Co' not in order                   # back in 90 days, not today
    assert 'send_details' in [k for k, _ in a.get('/api/bd/calling/meta').get_json()['statuses']]


def test_understanding_research_voice_and_opener(srv, ai, monkeypatch):
    from modules import bd_calling
    a = cl(srv, ADMIN)
    pid = _prospect(a, 'Calltest Research Co', website='research-co.example')
    a.put('/api/bd/calling/companies/%d/understanding' % pid, json={'size': '800 people'})
    monkeypatch.setattr(bd_calling, 'fetch_site', lambda url, max_pages=3: 'We make LV switchgear. Plant in Pune.')
    r = a.post('/api/bd/calling/companies/%d/research' % pid).get_json()
    assert r['ok'] and r['read_site'] and 'what_they_do' in r['filled']
    assert r['understanding']['size'] == '800 people'                  # a field you wrote is never overwritten
    detail = a.get('/api/bd/calling/companies/%d' % pid).get_json()
    assert detail['company']['industry'] == 'Electrical' and detail['company']['city'] == 'Pune'
    r = a.post('/api/bd/calling/companies/%d/understanding/parse' % pid,
               json={'text': 'Priya HR head bol rahi thi sales engineers 1 saal mein chhod dete hain, Naukri use karte hain'}).get_json()
    assert r['ok'] and 'hiring_pain' in r['changed'] and r['contact_hint']['name'] == 'Priya'
    op = a.post('/api/bd/calling/companies/%d/opener' % pid).get_json()
    assert op['opener']['opener'].startswith('Hi Priya') and len(op['opener']['openers']) == 2
    assert op['opener']['openers'][1]['flags'] and not op['opener']['openers'][0]['flags']     # agency-speak flagged
    assert op['opener']['next_step'] == '15-minute intake call' and op['opener']['questions'][0] == 'Which role is hardest?'
    assert 'PLAYBOOK' in ai[-1]['user'] and 'DISCOVERY' in ai[-1]['user']                   # the call plan follows the playbook
    assert a.get('/api/bd/calling/companies/%d' % pid).get_json()['opener']['objections']


def test_private_addresses_are_never_fetched():
    from modules import bd_calling
    assert bd_calling.fetch_site('http://127.0.0.1:5000/') == ''
    assert bd_calling.fetch_site('http://localhost/') == ''
    assert bd_calling.fetch_site('file:///etc/passwd') == ''


def test_ai_email_uses_only_real_placements(srv, ai):
    a = cl(srv, ADMIN)
    pid = _prospect(a, 'Calltest Mail Co', industry='Electrical')
    a.patch('/api/bd/calling/companies/%d' % pid, json={'contact_name': 'Priya Mail', 'email': 'priya@calltest-mail.in'})
    a.put('/api/bd/calling/companies/%d/understanding' % pid, json={'roles_hired': 'Sales engineers'})
    conn = srv.get_db()
    mid = conn.execute("INSERT INTO mandates (client, role, owner_id, status, created_at) VALUES "
                       "('Calltest Old Client','Area Sales Engineer',?, 'active', ?)", (CO, srv.ts())).lastrowid
    conn.execute("INSERT INTO candidates (name, mandate_id, owner_id, stage, created_at) VALUES ('Placed Guy',?,?,'Joined',?)",
                 (mid, CO, srv.ts()))
    conn.commit(); conn.close()
    j = a.post('/api/bd/calling/companies/%d/draft' % pid, json={}).get_json()
    assert j['ok'] and len(j['variants']) == 3 and j['to'] == 'priya@calltest-mail.in'
    assert any('Area Sales Engineer (1)' in f for f in j['facts'])
    prompt = ai[-1]['user']
    assert 'AGENCY FACTS' in prompt and 'Area Sales Engineer (1)' in prompt and 'Sales engineers' in prompt


def test_send_track_open_follow_up_and_reply(srv, ai, mail):
    from modules import bd_calling
    a = cl(srv, ADMIN)
    pid = _prospect(a, 'Calltest Seq Co')
    a.patch('/api/bd/calling/companies/%d' % pid, json={'contact_name': 'Ravi Seq', 'email': 'ravi@calltest-seq.in'})
    r = a.post('/api/bd/calling/companies/%d/send' % pid, json={'to': 'ravi@calltest-seq.in', 'subject': 'Hello Ravi',
                                                                'body': 'Hi Ravi, short note. Nitin'}).get_json()
    assert r['ok'] and r['sent'] and r['today']['emails'] >= 1
    assert mail[-1]['uid'] == ADMIN and '/bdm/o/' in mail[-1]['html']
    rid = r['id']
    conn = srv.get_db()
    b = conn.execute('SELECT * FROM bd_call WHERE client_id=?', (pid,)).fetchone()
    assert b['seq_active'] == 1 and b['seq_next_at'] > srv.ts()
    act = conn.execute("SELECT * FROM crm_activities WHERE client_id=? AND activity_type='email'", (pid,)).fetchone()
    assert act and json.loads(act['meta'])['sent'] is True
    # open pixel (after the first seconds)
    conn.execute('UPDATE bd_outreach SET sent_ts=? WHERE id=?', (time.time() - 120, rid)); conn.commit()
    tok = bd_calling.token_for(rid)
    assert srv.app.test_client().get('/bdm/o/%s.gif' % tok).status_code == 200
    assert srv.app.test_client().get('/bdm/o/%d-bad.gif' % rid).status_code == 200      # bad token: counted nowhere
    assert conn.execute('SELECT opens FROM bd_call WHERE client_id=?', (pid,)).fetchone()['opens'] == 1
    hot = [x['name'] for x in _rows(a, 'hot', 'calltest seq')['rows']]
    assert hot == ['Calltest Seq Co']
    # follow-up 1 is due -> sent in the same thread, "Re:" subject
    conn.execute("UPDATE bd_call SET seq_next_at='2000-01-01T10:00:00' WHERE client_id=?", (pid,)); conn.commit()
    assert bd_calling.run_followups(conn, force=True) == 1
    assert mail[-1]['subject'] == 'Re: Hello Ravi' and mail[-1]['in_reply_to'] == '<msg%d@test>' % (len(mail) - 1)
    b = conn.execute('SELECT * FROM bd_call WHERE client_id=?', (pid,)).fetchone()
    assert b['seq_step'] == 1 and b['seq_active'] == 1
    # the prospect replies (synced inbox) -> sequence stops, bell rings
    conn.execute("INSERT INTO emails (owner_id, msg_id, folder, from_addr, from_name, subject, snippet, date_ts, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (CO, '<r1@x>', 'INBOX', 'Ravi <ravi@calltest-seq.in>', 'Ravi',
                                                 'Re: Hello Ravi', 'Sure, call me Monday', time.time(), srv.ts()))
    conn.commit()
    assert bd_calling.check_replies(conn) == 1
    b = conn.execute('SELECT * FROM bd_call WHERE client_id=?', (pid,)).fetchone()
    assert b['replied_at'] and b['seq_active'] == 0 and b['email_state'] == 'replied'
    n = conn.execute("SELECT * FROM notifications WHERE user_id=? AND kind='bd_reply' ORDER BY id DESC", (ADMIN,)).fetchone()
    assert n and 'Calltest Seq Co replied' in n['title']
    conn.execute("UPDATE bd_call SET seq_next_at='2000-01-01T10:00:00', seq_active=1 WHERE client_id=?", (pid,)); conn.commit()
    assert bd_calling.run_followups(conn, force=True) == 0                     # replied: never again
    today = [x for x in _rows(a, 'today', 'calltest seq')['rows']]
    assert today and today[0]['why'] == 'Replied to your e-mail'
    conn.close()


def test_bulk_drafts_review_approve_and_spaced_sending(srv, ai, mail):
    from modules import bd_calling
    a = cl(srv, ADMIN)
    ids = []
    for i in range(3):
        pid = _prospect(a, 'Calltest Bulk %d' % i)
        if i < 2:
            a.patch('/api/bd/calling/companies/%d' % pid, json={'contact_name': 'Person %d' % i,
                                                                 'email': 'p%d@calltest-bulk.in' % i})
        ids.append(pid)
    r = a.post('/api/bd/calling/bulk-draft', json={'client_ids': ids, 'style': 'value', '_sync': True}).get_json()
    assert r['drafting'] == 2 and r['skipped'][0]['why'] == 'No contact e-mail'
    rv = a.get('/api/bd/calling/review').get_json()
    mine = [d for d in rv['drafts'] if d['company'].startswith('Calltest Bulk')]
    assert len(mine) == 2 and all(d['status'] == 'draft' and d['body'] == 'Hi Priya, value. Nitin' for d in mine)
    assert a.patch('/api/bd/calling/emails/%d' % mine[0]['id'], json={'subject': 'Edited subject'}).get_json()['ok']
    w1 = a.post('/api/bd/calling/emails/%d/approve' % mine[0]['id']).get_json()['scheduled_at']
    w2 = a.post('/api/bd/calling/emails/%d/approve' % mine[1]['id']).get_json()['scheduled_at']
    assert w2 > w1                                                            # spaced out
    conn = srv.get_db()
    conn.execute("UPDATE bd_outreach SET scheduled_at='2000-01-01T00:00:00' WHERE id IN (?,?)", (mine[0]['id'], mine[1]['id']))
    conn.commit()
    n0 = len(mail)
    assert bd_calling.run_queue(conn, force=True) == 1                         # one per user per tick
    assert mail[n0]['subject'] == 'Edited subject'
    assert bd_calling.run_queue(conn, force=True) == 1
    conn.close()
    # someone else cannot touch my drafts
    assert cl(srv, OADMIN).post('/api/bd/calling/emails/%d/skip' % mine[0]['id']).status_code == 404


def test_import_paste_with_duplicates(srv):
    a = cl(srv, ADMIN)
    a.post('/api/crm/clients', json={'name': 'Calltest Existing Client'})
    text = ('Company\tContact\tDesignation\tPhone\tEmail\tWebsite\n'
            'Calltest Import One\tAnil Kumar\tHR Manager\t9810000011\tanil@import-one.in\timport-one.in\n'
            'Calltest Import One\tSunita Rao\tPlant Head\t9810000012\t\t\n'
            'Calltest Existing Client\tX Y\t\t\t\t\n'
            'Calltest Import Two\t\t\t\tbad-email\t\n')
    p = a.post('/api/bd/calling/import', json={'text': text}).get_json()
    assert p['ok'] and p['total'] == 4 and p['new'] == 2 and p['exists'] == 1
    assert [x['state'] for x in p['preview']] == ['new', 'same_company', 'exists_client', 'new']
    c = a.post('/api/bd/calling/import', json={'text': text, 'commit': True}).get_json()
    assert c['created'] == 2 and c['contacts'] == 2 and c['skipped'] == 1
    rows = {r['name']: r for r in _rows(a, 'all', 'calltest import')['rows']}
    assert rows['Calltest Import One']['contacts'] == 2 and rows['Calltest Import One']['contact']['name'] == 'Anil Kumar'


def test_email_guess():
    from modules.bd_calling import guess_emails
    g = guess_emails('Priya Nair', 'https://www.acme-switch.in/about')
    assert g[0]['email'] == 'priya.nair@acme-switch.in' and g[0]['level'] == 'guess'
    g = guess_emails('Priya Nair', '', [('Rahul Verma', 'rverma@acme.co.in')])
    assert g[0]['email'] == 'pnair@acme.co.in' and g[0]['level'] == 'high'
    assert guess_emails('Priya', 'gmail.com') == []


def test_settings_and_access(srv):
    a = cl(srv, ADMIN)
    s = a.put('/api/bd/calling/settings', json={'calls_target': 80, 'fu_days': '2, 5, 99', 'gap_seconds': 5}).get_json()
    assert s['settings']['calls_target'] == 80 and s['settings']['fu_days'] == '2,5' and s['settings']['gap_seconds'] == 60
    pid = _prospect(a, 'Calltest Private Co')
    o = cl(srv, OADMIN)
    assert o.get('/api/bd/calling/companies/%d' % pid).status_code == 404
    assert o.post('/api/bd/calling/companies/%d/outcome' % pid, json={'status': 'no_answer'}).status_code == 404
    assert 'Calltest Private Co' not in [r['name'] for r in _rows(o, 'all', 'calltest')['rows']]
    assert cl(srv, RIYA).get('/api/bd/calling/list').status_code == 403      # BD stays admin-only for recruiters
    assert srv.app.test_client().get('/bd-calling.js').status_code == 200


def test_playbook_edit_reset_and_used_by_the_ai(srv, ai):
    a = cl(srv, ADMIN)
    j = a.get('/api/bd/calling/playbook').get_json()
    assert j['ok'] and not j['custom'] and 'DISCOVERY' in j['text'] and j['phrases']
    a.put('/api/bd/calling/playbook', json={'text': 'OUR WAY: always open with the plant expansion. Never say agency.'})
    j = a.get('/api/bd/calling/playbook').get_json()
    assert j['custom'] and j['text'].startswith('OUR WAY')
    assert cl(srv, OADMIN).get('/api/bd/calling/playbook').get_json()['custom'] is False      # per agency
    pid = _prospect(a, 'Calltest Playbook Co')
    a.post('/api/bd/calling/companies/%d/draft' % pid, json={})
    assert 'OUR WAY: always open with the plant expansion' in ai[-1]['user']
    j = a.put('/api/bd/calling/playbook', json={'reset': True}).get_json()
    assert not j['custom'] and 'DISCOVERY' in j['text']


def test_agency_speak_is_flagged_and_rewritten(srv, monkeypatch):
    from modules.bd_calling import lint_text
    assert {i['phrase'] for i in lint_text('Kindly revert if you have any requirements. We are a leading recruitment firm.')} >= \
        {'kindly', 'revert', 'any requirements', 'leading recruitment'}
    assert lint_text('Saw the new Pune plant - which sales role is hardest to fill?') == []
    a = cl(srv, ADMIN)
    assert a.post('/api/bd/calling/lint', json={'text': 'Hope this email finds you well'}).get_json()['issues']
    calls = []

    def fake(key, payload, timeout=60, endpoint=''):
        calls.append(payload['messages'][1]['content'])
        bad = len(calls) == 1
        body = 'Kindly revert with any requirements.' if bad else 'Saw the Pune plant news - which role is hardest to fill?'
        return FakeResp({'variants': [{'style': 'short', 'subjects': ['Pune plant hiring'], 'body': body}]})
    monkeypatch.setattr(srv, 'call_deepseek', fake)
    real = srv.get_setting
    monkeypatch.setattr(srv, 'get_setting', lambda k, default='': 'test-key' if k == 'deepseek_api_key' else real(k, default))
    pid = _prospect(a, 'Calltest Cliche Co')
    j = a.post('/api/bd/calling/companies/%d/draft' % pid, json={}).get_json()
    assert len(calls) == 2 and 'AGENCY CLICHES' in calls[1]
    assert j['variants'][0]['body'].startswith('Saw the Pune plant') and j['variants'][0]['flags'] == []
