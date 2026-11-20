"""
Team Chat — modules/team_chat.py (/team-chat)

Run:   pytest test_team_chat.py -q
"""
import io
import json
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CO, OTHER = 9810, 9811
ADMIN, RIYA, AMIT, OADMIN, FL = 98101, 98102, 98103, 98104, 98105
M_A, M_R, M_O, POOL = 98201, 98202, 98203, 98204
C_A, C_R, C_O = 98301, 98302, 98303


class FakeResp:
    def __init__(self, obj, status=200):
        self.status_code = status
        self._obj = obj

    def json(self):
        return {'choices': [{'message': {'content': json.dumps(self._obj)}}], 'usage': {}}


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='chat_'))
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db(); c = conn.cursor(); now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", list(kw.values()))

    ins('companies', id=CO, name='Chat Co', status='active', created_at=now)
    ins('companies', id=OTHER, name='Chat Other', status='active', created_at=now)
    for uid, un, dn, co, adm, role in ((ADMIN, 'tc_admin', 'Nitin Admin', CO, 1, 'user'), (RIYA, 'tc_riya', 'Riya Sharma', CO, 0, 'user'),
                                       (AMIT, 'tc_amit', 'Amit', CO, 0, 'user'), (OADMIN, 'tc_other', 'Other Boss', OTHER, 1, 'user'),
                                       (FL, 'tc_fl', 'Free Lancer', CO, 0, 'freelancer_sourcer')):
        ins('users', id=uid, username=un, password_hash='x', display_name=dn, role=role, status='approved',
            company_id=co, is_company_admin=adm, created_at=now)
    for mid, role, who, co, st in ((M_A, 'Admin Job', ADMIN, CO, 'active'), (M_R, 'Riya Job', RIYA, CO, 'active'),
                                   (M_O, 'Other Job', OADMIN, OTHER, 'active'), (POOL, 'Central', ADMIN, CO, 'central')):
        ins('mandates', id=mid, role=role, client='Client', status=st, owner_id=co, assigned_user_id=who, created_at=now)
    c.execute("INSERT OR IGNORE INTO mandate_assignees (company_id, mandate_id, user_id, is_primary, added_by, added_at, is_active) "
              "VALUES (?,?,?,1,?,?,1)", (CO, M_R, RIYA, RIYA, now))
    for cid, mid, name, co in ((C_A, M_A, 'Secret Sharma', CO), (C_R, M_R, 'Asha Verma', CO), (C_O, M_O, 'Other Person', OTHER)):
        ins('candidates', id=cid, mandate_id=mid, name=name, company='ABB', designation='Sales Engineer', stage='Screening',
            owner_id=co, created_at=now)
    conn.commit(); conn.close()
    yield server


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


@pytest.fixture(autouse=True)
def _away():
    from modules import team_chat
    team_chat._seen.clear()                 # nobody "has the chat open" unless a test polls
    yield


def general(srv, uid=ADMIN):
    st = cl(srv, uid).get('/api/chat/state').get_json()
    return [c for c in st['channels'] if c['name'] == 'general'][0]['id'], st


def test_page_needs_sign_in_and_not_freelancer(srv):
    assert srv.app.test_client().get('/team-chat').status_code == 302
    assert cl(srv, FL).get('/team-chat').status_code == 302
    r = cl(srv, RIYA).get('/team-chat')
    assert r.status_code == 200 and b'Team Chat' in r.data and b'/app-nav.js' in r.data
    assert cl(srv, FL).get('/api/chat/state').status_code == 403
    assert srv.app.test_client().get('/api/chat/state').status_code == 401


def test_general_for_everyone_and_people_list(srv):
    gid, st = general(srv)
    names = [u['name'] for u in st['users']]
    assert 'Riya Sharma' in names and 'Free Lancer' not in names and 'Other Boss' not in names
    gid2, _ = general(srv, RIYA)
    assert gid == gid2
    # another agency gets its own #general
    gid3, st3 = general(srv, OADMIN)
    assert gid3 != gid and [u['name'] for u in st3['users']] == ['Other Boss']


def test_post_poll_unread_and_read(srv):
    gid, st = general(srv)
    general(srv, RIYA)
    rev = st['rev']
    r = cl(srv, ADMIN).post('/api/chat/channels/%d/messages' % gid, json={'body': 'Hello **team**'})
    assert r.get_json()['ok']
    p = cl(srv, RIYA).get('/api/chat/poll?since=%d' % rev).get_json()
    assert any(m['body'] == 'Hello **team**' for m in p['messages'])
    g = [c for c in p['channels'] if c['id'] == gid][0]
    assert g['unread'] >= 1
    cl(srv, RIYA).post('/api/chat/channels/%d/read' % gid, json={})
    p = cl(srv, RIYA).get('/api/chat/poll?since=%d' % rev).get_json()
    assert [c for c in p['channels'] if c['id'] == gid][0]['unread'] == 0
    # other agency never sees it
    o = cl(srv, OADMIN)
    assert o.get('/api/chat/channels/%d/messages' % gid).status_code == 404
    assert not any(m['body'] == 'Hello **team**' for m in o.get('/api/chat/poll?since=%d' % rev).get_json()['messages'])


def test_private_channel_and_dm_are_members_only(srv):
    a = cl(srv, ADMIN)
    r = a.post('/api/chat/channels', json={'name': 'Leadership Team', 'private': True, 'members': [AMIT, OADMIN, FL]}).get_json()
    assert r['ok']
    chid = r['id']
    mem = a.get('/api/chat/channels/%d/members' % chid).get_json()['members']
    assert sorted(mem) == sorted([ADMIN, AMIT])                 # other agency / freelancer are not added
    a.post('/api/chat/channels/%d/messages' % chid, json={'body': 'budget talk'})
    assert cl(srv, RIYA).get('/api/chat/channels/%d/messages' % chid).status_code == 404
    assert cl(srv, RIYA).post('/api/chat/channels/%d/messages' % chid, json={'body': 'x'}).status_code == 404
    assert 'leadership-team' in [c['name'] for c in cl(srv, AMIT).get('/api/chat/state').get_json()['channels']]
    assert cl(srv, RIYA).get('/api/chat/search?q=budget').get_json()['messages'] == []
    # DM
    d = cl(srv, RIYA).post('/api/chat/dm', json={'user': AMIT}).get_json()
    assert d['ok'] and cl(srv, AMIT).post('/api/chat/dm', json={'user': RIYA}).get_json()['id'] == d['id']
    cl(srv, RIYA).post('/api/chat/channels/%d/messages' % d['id'], json={'body': 'psst'})
    assert cl(srv, ADMIN).get('/api/chat/channels/%d/messages' % d['id']).status_code == 404
    assert cl(srv, RIYA).post('/api/chat/dm', json={'user': OADMIN}).status_code == 404


def test_public_channel_read_then_join(srv):
    a = cl(srv, ADMIN)
    chid = a.post('/api/chat/channels', json={'name': 'sales'}).get_json()['id']
    assert a.post('/api/chat/channels', json={'name': 'Sales'}).status_code == 400   # same name
    r = cl(srv, AMIT)
    st = r.get('/api/chat/state').get_json()
    assert chid in [c['id'] for c in st['browse']]
    assert r.get('/api/chat/channels/%d/messages' % chid).get_json()['member'] is False
    assert r.post('/api/chat/channels/%d/messages' % chid, json={'body': 'hi'}).status_code == 403
    assert r.post('/api/chat/channels/%d/join' % chid).get_json()['ok']
    assert r.post('/api/chat/channels/%d/messages' % chid, json={'body': 'hi'}).get_json()['ok']
    assert r.post('/api/chat/channels/%d/leave' % chid).get_json()['ok']
    gid, _ = general(srv)
    assert r.post('/api/chat/channels/%d/leave' % gid).status_code == 400


def test_threads_reactions_pin_edit_delete(srv):
    gid, _ = general(srv)
    general(srv, RIYA)
    a, r = cl(srv, ADMIN), cl(srv, RIYA)
    root = a.post('/api/chat/channels/%d/messages' % gid, json={'body': 'Who can call Asha?'}).get_json()['message']
    rep = r.post('/api/chat/channels/%d/messages' % gid, json={'body': 'Me', 'parent_id': root['id']}).get_json()['message']
    rep2 = a.post('/api/chat/channels/%d/messages' % gid, json={'body': 'Thanks', 'parent_id': rep['id']}).get_json()['message']
    assert rep2['parent_id'] == root['id']                         # reply to a reply stays in the thread
    th = r.get('/api/chat/messages/%d/thread' % root['id']).get_json()
    assert [m['body'] for m in th['messages']] == ['Who can call Asha?', 'Me', 'Thanks'] and th['messages'][0]['reply_count'] == 2
    top = [m['id'] for m in a.get('/api/chat/channels/%d/messages' % gid).get_json()['messages']]
    assert rep['id'] not in top                                    # replies stay out of the channel list
    x = r.post('/api/chat/messages/%d/react' % root['id'], json={'emoji': '👍'}).get_json()['message']
    assert x['reactions'] == [{'emoji': '👍', 'users': [RIYA]}]
    x = r.post('/api/chat/messages/%d/react' % root['id'], json={'emoji': '👍'}).get_json()['message']
    assert x['reactions'] == []
    assert r.post('/api/chat/messages/%d/pin' % root['id']).get_json()['pinned']
    assert [p['id'] for p in a.get('/api/chat/channels/%d/messages' % gid).get_json()['pins']] == [root['id']]
    assert r.patch('/api/chat/messages/%d' % root['id'], json={'body': 'hacked'}).status_code == 403
    e = a.patch('/api/chat/messages/%d' % root['id'], json={'body': 'Who can call Asha today?'}).get_json()['message']
    assert e['body'].endswith('today?') and e['edited_at']
    assert r.delete('/api/chat/messages/%d' % rep2['id']).status_code == 403
    assert a.delete('/api/chat/messages/%d' % rep['id']).get_json()['message']['deleted'] == 1   # admin may delete
    th = a.get('/api/chat/messages/%d/thread' % root['id']).get_json()
    assert th['messages'][0]['reply_count'] == 1 and th['messages'][1]['body'] == ''


def test_mentions_notify_and_mentions_view(srv):
    gid, _ = general(srv)
    general(srv, RIYA)
    srv_conn = srv.get_db(); srv_conn.execute("DELETE FROM notifications WHERE user_id=?", (RIYA,)); srv_conn.commit(); srv_conn.close()
    from modules import team_chat
    team_chat._seen.clear()                 # Riya closed the chat
    cl(srv, ADMIN).post('/api/chat/channels/%d/messages' % gid, json={'body': 'Can you call <@%d> ?' % RIYA})
    cl(srv, ADMIN).post('/api/chat/channels/%d/messages' % gid, json={'body': 'and also this <@%d>' % RIYA})
    n = cl(srv, RIYA).get('/api/notifications').get_json()['notifications']
    chat = [x for x in n if x['kind'] == 'chat_mention']
    assert len(chat) == 1 and 'Nitin Admin mentioned you in #general' == chat[0]['title']
    assert 'Can you call @Riya Sharma ?' in chat[0]['body'] and 'and also this' in chat[0]['body']   # merged, readable
    m = cl(srv, RIYA).get('/api/chat/mentions').get_json()['messages']
    assert any('<@%d>' % RIYA in x['body'] for x in m)
    # someone who has the chat open gets no bell
    cl(srv, AMIT).get('/api/chat/poll?since=1')
    dm = cl(srv, ADMIN).post('/api/chat/dm', json={'user': AMIT}).get_json()['id']
    cl(srv, ADMIN).post('/api/chat/channels/%d/messages' % dm, json={'body': 'quick one'})
    assert not [x for x in cl(srv, AMIT).get('/api/notifications').get_json()['notifications'] if x['kind'] == 'chat_dm']


def test_candidate_and_job_cards_follow_access(srv):
    gid, _ = general(srv)
    general(srv, RIYA)
    body = 'Look at <c:%d> for <m:%d>, and <c:%d> for <m:%d>, not <c:%d>' % (C_R, M_R, C_A, M_A, C_O)
    cl(srv, ADMIN).post('/api/chat/channels/%d/messages' % gid, json={'body': body})
    adm = cl(srv, ADMIN).get('/api/chat/channels/%d/messages' % gid).get_json()['refs']
    assert adm['c:%d' % C_A]['name'] == 'Secret Sharma' and adm['m:%d' % M_A]['role'] == 'Admin Job'
    assert adm['c:%d' % C_O].get('missing')                        # another agency's candidate is never shown
    riya = cl(srv, RIYA).get('/api/chat/channels/%d/messages' % gid).get_json()['refs']
    assert riya['c:%d' % C_R]['name'] == 'Asha Verma' and riya['m:%d' % M_R]['role'] == 'Riya Job'
    assert riya['c:%d' % C_A] == {'kind': 'c', 'id': C_A, 'restricted': True}
    assert riya['m:%d' % M_A] == {'kind': 'm', 'id': M_A, 'restricted': True}
    lk = cl(srv, RIYA).get('/api/chat/lookup?q=Sharma').get_json()
    assert [c['name'] for c in lk['candidates']] == []             # Secret Sharma is not hers
    lk = cl(srv, RIYA).get('/api/chat/lookup?q=job').get_json()
    assert [j['role'] for j in lk['jobs']] == ['Riya Job']


def test_job_channel(srv):
    a = cl(srv, ADMIN)
    assert a.post('/api/chat/channels', json={'name': 'x-other', 'job': M_O}).status_code == 400
    assert cl(srv, RIYA).post('/api/chat/channels', json={'name': 'x-admin', 'job': M_A}).status_code == 400
    chid = cl(srv, RIYA).post('/api/chat/channels', json={'name': 'riya-job', 'job': M_R}).get_json()['id']
    j = cl(srv, RIYA).get('/api/chat/channels/%d/messages' % chid).get_json()
    assert j['refs']['m:%d' % M_R]['role'] == 'Riya Job'
    assert [c for c in cl(srv, RIYA).get('/api/chat/state').get_json()['channels'] if c['id'] == chid][0]['mandate_id'] == M_R


def test_files_upload_and_download(srv):
    gid, _ = general(srv)
    general(srv, RIYA)
    r = cl(srv, RIYA).post('/api/chat/channels/%d/upload' % gid, data={'file': (io.BytesIO(b'%PDF-1.4 cv'), 'Asha CV.pdf'), 'body': 'CV attached'},
                           content_type='multipart/form-data').get_json()
    assert r['ok'] and r['message']['file']['name'] == 'Asha CV.pdf'
    mid = r['message']['id']
    d = cl(srv, ADMIN).get('/api/chat/files/%d' % mid)
    assert d.status_code == 200 and d.data == b'%PDF-1.4 cv'
    assert cl(srv, OADMIN).get('/api/chat/files/%d' % mid).status_code == 404
    assert cl(srv, RIYA).get('/api/chat/search?q=Asha CV').get_json()['messages'][0]['id'] == mid
    cl(srv, RIYA).delete('/api/chat/messages/%d' % mid)
    assert cl(srv, ADMIN).get('/api/chat/files/%d' % mid).status_code == 404


def test_search_words_and_from(srv):
    gid, _ = general(srv)
    general(srv, AMIT)
    cl(srv, AMIT).post('/api/chat/channels/%d/messages' % gid, json={'body': 'Offer released to Kunal at Siemens'})
    hits = cl(srv, ADMIN).get('/api/chat/search?q=siemens kunal').get_json()['messages']
    assert hits and hits[0]['body'].startswith('Offer released')
    assert cl(srv, ADMIN).get('/api/chat/search?q=siemens from:amit').get_json()['messages']
    assert not cl(srv, ADMIN).get('/api/chat/search?q=siemens from:riya').get_json()['messages']


def test_catch_me_up(srv, monkeypatch):
    gid, _ = general(srv)
    sent = {}

    def fake(key, payload, timeout=60, endpoint='deepseek'):
        sent['p'] = payload
        return FakeResp({'summary': 'Offer to Kunal is out.', 'decisions': ['Go with Kunal'], 'actions': ['Amit: send offer'], 'for_you': []})
    monkeypatch.setattr(srv, 'call_deepseek', fake)
    real = srv.get_setting
    monkeypatch.setattr(srv, 'get_setting', lambda k, default='': 'test-key' if k == 'deepseek_api_key' else real(k, default))
    j = cl(srv, ADMIN).post('/api/chat/channels/%d/catchup' % gid).get_json()
    assert j['ok'] and j['summary'] == 'Offer to Kunal is out.' and j['actions'] == ['Amit: send offer']
    assert 'Amit: Offer released to Kunal at Siemens' in sent['p']['messages'][-1]['content']
    assert cl(srv, OADMIN).post('/api/chat/channels/%d/catchup' % gid).status_code == 404


def test_recruiter_guard_allows_chat_routes(srv):
    # numbers in /api/chat/... URLs must not trip the recruiter "unmapped object route" guard
    gid, _ = general(srv, RIYA)
    assert cl(srv, RIYA).get('/api/chat/channels/%d/messages' % gid).status_code == 200


def test_chat_windows_on_every_page_but_the_full_chat(srv):
    js = srv.app.test_client().get('/chat-dock.js')
    assert js.status_code == 200 and js.mimetype == 'application/javascript' and b'attachShadow' in js.data
    c = cl(srv, RIYA)
    assert b'<script src="/chat-dock.js"' in c.get('/').data                  # main app
    for page in ('/learning', '/talent-pool', '/hotlist'):
        assert b'/chat-dock.js' in c.get(page).data, page                     # separate pages (app menu)
    assert b'/chat-dock.js' not in c.get('/team-chat').data                    # the full page has it all


# ══════════════════════════════════════════════════════════════════════════
#  Nov 2026: HireLab AI, morning summary, scheduled messages, status, card actions
# ══════════════════════════════════════════════════════════════════════════
@pytest.fixture
def ai(srv, monkeypatch):
    from modules import team_chat
    monkeypatch.setattr(team_chat, 'AI_SYNC', True)
    team_chat._ai_calls.clear()
    seen = []

    def fake(key, payload, timeout=60, endpoint='deepseek'):
        seen.append(payload)
        return _Txt('Aaj interviews: see data')
    monkeypatch.setattr(srv, 'call_deepseek', fake)
    real = srv.get_setting
    monkeypatch.setattr(srv, 'get_setting', lambda k, default='': 'test-key' if k == 'deepseek_api_key' else real(k, default))
    return seen


class _Txt:
    status_code = 200

    def __init__(self, text):
        self.text = text

    def json(self):
        return {'choices': [{'message': {'content': self.text}}], 'usage': {}}


def _seed_interviews(srv):
    conn = srv.get_db(); today = srv.ts()[:10]
    for iid, cid, mid in ((98401, C_A, M_A), (98402, C_R, M_R)):
        conn.execute("INSERT OR REPLACE INTO interviews (id, candidate_id, mandate_id, owner_id, round_name, mode, scheduled_at, status, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (iid, cid, mid, CO, 'Round 1', 'Video', today + 'T16:00', 'scheduled', srv.ts()))
    conn.commit(); conn.close()


def test_at_ai_answers_in_a_thread_with_the_askers_access(srv, ai):
    _seed_interviews(srv)
    gid, _ = general(srv)
    general(srv, RIYA)
    q = cl(srv, RIYA).post('/api/chat/channels/%d/messages' % gid, json={'body': '@AI aaj kitne interviews hain?'}).get_json()['message']
    th = cl(srv, RIYA).get('/api/chat/messages/%d/thread' % q['id']).get_json()['messages']
    assert th[-1]['user_id'] == 0 and th[-1]['body'] == 'Aaj interviews: see data'
    data = ai[-1]['messages'][0]['content']
    assert 'Asha Verma' in data and '<c:%d>' % C_R in data                  # Riya's own job
    assert 'Secret Sharma' not in data and 'Admin Job' not in data           # not the admin's job
    assert ai[-1]['messages'][-1]['content'] == 'aaj kitne interviews hain?'
    # the admin sees the whole agency
    cl(srv, ADMIN).post('/api/chat/channels/%d/messages' % gid, json={'body': '@ai interviews today?'})
    assert 'Secret Sharma' in ai[-1]['messages'][0]['content']
    # a normal message does not wake the AI
    n = len(ai)
    cl(srv, ADMIN).post('/api/chat/channels/%d/messages' % gid, json={'body': 'email me at said@x.com'})
    assert len(ai) == n


def test_private_ai_chat(srv, ai):
    st = cl(srv, AMIT).get('/api/chat/state').get_json()
    aic = [c for c in st['channels'] if c['kind'] == 'ai']
    assert len(aic) == 1
    msgs = cl(srv, AMIT).get('/api/chat/channels/%d/messages' % aic[0]['id']).get_json()['messages']
    assert msgs[0]['user_id'] == 0 and 'HireLab AI' in msgs[0]['body']            # welcome
    cl(srv, AMIT).post('/api/chat/channels/%d/messages' % aic[0]['id'], json={'body': 'pending offers?'})
    msgs = cl(srv, AMIT).get('/api/chat/channels/%d/messages' % aic[0]['id']).get_json()['messages']
    assert msgs[-1]['user_id'] == 0 and msgs[-1]['parent_id'] == 0
    assert cl(srv, RIYA).get('/api/chat/channels/%d/messages' % aic[0]['id']).status_code == 404   # private


def test_morning_summary_once_a_day_with_access(srv, monkeypatch):
    import datetime
    from modules import team_chat
    _seed_interviews(srv)
    monkeypatch.setattr(team_chat, '_ist', lambda: datetime.datetime.fromisoformat(srv.ts()[:10] + 'T09:00:00'))
    conn = srv.get_db()
    conn.execute("UPDATE chat_prefs SET digest_date='', digest_off=0 WHERE user_id IN (?,?)", (RIYA, ADMIN))
    conn.commit()
    assert team_chat.maybe_digest(conn, CO, RIYA) is True
    assert team_chat.maybe_digest(conn, CO, RIYA) is False                     # once a day
    conn.close()
    aic = [c for c in cl(srv, RIYA).get('/api/chat/state').get_json()['channels'] if c['kind'] == 'ai'][0]['id']
    body = cl(srv, RIYA).get('/api/chat/channels/%d/messages' % aic).get_json()['messages'][-1]['body']
    assert 'Good morning, Riya' in body and '<c:%d>' % C_R in body and '<c:%d>' % C_A not in body
    # turned off -> nothing
    cl(srv, ADMIN).post('/api/chat/prefs', json={'digest_off': True})
    conn = srv.get_db()
    assert team_chat.maybe_digest(conn, CO, ADMIN) is False
    conn.close()
    cl(srv, ADMIN).post('/api/chat/prefs', json={'digest_off': False})
    # before 08:30 -> nothing
    monkeypatch.setattr(team_chat, '_ist', lambda: datetime.datetime.fromisoformat(srv.ts()[:10] + 'T07:00:00'))
    conn = srv.get_db(); conn.execute("UPDATE chat_prefs SET digest_date='' WHERE user_id=?", (AMIT,)); conn.commit()
    assert team_chat.maybe_digest(conn, CO, AMIT) is False
    conn.close()


def test_scheduled_messages(srv):
    from modules import team_chat
    gid, _ = general(srv, RIYA)
    r = cl(srv, RIYA)
    assert r.post('/api/chat/channels/%d/schedule' % gid, json={'body': 'too late', 'send_at': '2020-01-01T09:00'}).status_code == 400
    j = r.post('/api/chat/channels/%d/schedule' % gid, json={'body': 'Good morning team <@%d>' % AMIT, 'send_at': '2099-01-01T09:00'})
    assert j.status_code == 400                                                  # > 60 days
    import datetime
    later = (datetime.datetime.fromisoformat(srv.ts()) + datetime.timedelta(hours=2)).isoformat()[:16]
    j = r.post('/api/chat/channels/%d/schedule' % gid, json={'body': 'Standup in 5', 'send_at': later}).get_json()
    assert j['ok'] and [x['body'] for x in j['scheduled']] == ['Standup in 5']
    sid = j['id']
    assert cl(srv, ADMIN).delete('/api/chat/scheduled/%d' % sid).status_code == 404    # only the author
    conn = srv.get_db(); conn.execute("UPDATE chat_scheduled SET send_at='2020-01-01T00:00:00' WHERE id=?", (sid,)); conn.commit()
    assert team_chat.run_due(conn) >= 1
    row = conn.execute('SELECT status, message_id FROM chat_scheduled WHERE id=?', (sid,)).fetchone()
    m = conn.execute('SELECT user_id, body, channel_id FROM chat_messages WHERE id=?', (row['message_id'],)).fetchone()
    conn.close()
    assert row['status'] == 'sent' and m['user_id'] == RIYA and m['body'] == 'Standup in 5' and m['channel_id'] == gid
    # cancel
    j = r.post('/api/chat/channels/%d/schedule' % gid, json={'body': 'never', 'send_at': later}).get_json()
    assert r.delete('/api/chat/scheduled/%d' % j['id']).get_json()['scheduled'] == []


def test_status(srv):
    a = cl(srv, ADMIN)
    st = a.post('/api/chat/status', json={'emoji': '📞', 'text': 'On calls', 'until': '2099-01-01T10:00'}).get_json()['statuses']
    assert st[str(ADMIN)]['text'] == 'On calls'
    s2 = cl(srv, RIYA).get('/api/chat/state').get_json()
    assert s2['statuses'][str(ADMIN)]['emoji'] == '📞' and any(p[1] == 'On leave' for p in s2['presets'])
    assert 'Screening' in s2['stages'] and s2['stages'][-2:] == ['Placed', 'Joined']
    a.post('/api/chat/status', json={'emoji': '🍽️', 'text': 'Lunch', 'until': '2020-01-01T10:00'})   # already over
    assert str(ADMIN) not in cl(srv, RIYA).get('/api/chat/state').get_json()['statuses']
    a.post('/api/chat/status', json={})
    assert cl(srv, OADMIN).get('/api/chat/state').get_json()['statuses'] == {}        # other agency


def test_bell_text_never_names_a_candidate(srv):
    gid, _ = general(srv)
    general(srv, RIYA)
    from modules import team_chat
    team_chat._seen.clear()
    conn = srv.get_db(); conn.execute("DELETE FROM notifications WHERE user_id=?", (RIYA,)); conn.commit(); conn.close()
    cl(srv, ADMIN).post('/api/chat/channels/%d/messages' % gid, json={'body': '<@%d> look at <c:%d>' % (RIYA, C_A)})
    n = [x for x in cl(srv, RIYA).get('/api/notifications').get_json()['notifications'] if x['kind'] == 'chat_mention']
    assert n and 'Secret Sharma' not in n[0]['body'] and '[candidate]' in n[0]['body']


def test_card_action_routes_respect_recruiter_access(srv):
    # the card buttons call the ATS's own routes; a recruiter cannot act on another job's candidate
    r = cl(srv, RIYA)
    assert r.post('/api/candidates/%d/stage' % C_A, json={'stage': 'Interested'}).status_code in (403, 404)
    assert r.post('/api/candidates/%d/stage' % C_R, json={'stage': 'Interested'}).get_json()['ok']


def test_browser_times_are_turned_into_ist():
    from modules.team_chat import to_ist
    assert to_ist('2026-11-21T09:00', -330) == '2026-11-21T09:00'      # a browser in India
    assert to_ist('2026-11-21T09:00', 0) == '2026-11-21T14:30'         # a browser on UTC
    assert to_ist('2026-11-21T09:00', None) == '2026-11-21T09:00'      # no zone sent
    assert to_ist('tomorrow', -330) == ''
