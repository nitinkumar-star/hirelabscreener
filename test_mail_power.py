"""
Email Box, Gmail-grade (modules/mail_power.py):
  delete reaches Gmail Trash, Gmail-style search, DeepSeek compose,
  and every mailbox syncing at the same time (modules/mail_sync.py).

Run:   pytest test_mail_power.py -q
"""
import os
import sys
import json
import time
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='mailpower_'))
    import server
    server.app.config['TESTING'] = True
    conn = server.get_db()
    c = conn.cursor()
    c.execute("INSERT INTO companies (name, status, plan, billing_status, created_at) "
              "VALUES ('MP Agency', 'active', 'standard', 'active', '2026-01-01T00:00:00')")
    co = c.lastrowid
    c.execute("INSERT INTO users (username, password_hash, display_name, role, status, company_id, is_company_admin, "
              "created_at, tour_done_at) VALUES ('mp_boss', 'x', 'Boss', 'user', 'approved', ?, 1, "
              "'2026-01-01T00:00:00', '')", (co,))
    boss = c.lastrowid
    c.execute("INSERT INTO candidates (owner_id, mandate_id, name, email, designation, company) "
              "VALUES (?, 0, 'Rahul Verma', 'rahul@example.com', 'Design Engineer', 'Tata Power')", (co,))
    cand = c.lastrowid
    now = time.time()
    rows = [
        # (msg_id, folder, from, from_name, to, subject, body, age_days, read, starred, archived, cand)
        ('<a1@mail>', 'Inbox', 'priya@client.com', 'Priya Shah', 'hr@agency.com', 'Offer letter for Rahul',
         'Please find the offer letter attached', 1, 0, 1, 0, cand),
        ('<a2@mail>', 'Inbox', 'news@weekly.com', 'Weekly News', 'hr@agency.com', 'Market newsletter',
         'This week in solar hiring', 3, 1, 0, 0, None),
        ('<a3@mail>', 'Sent', 'hr@agency.com', 'HR', 'rahul@example.com', 'Interview on Friday',
         'Hi Rahul, your interview is on Friday', 10, 1, 0, 0, cand),
        ('<a4@mail>', 'Inbox', 'old@vendor.com', 'Old Vendor', 'hr@agency.com', 'Invoice from vendor',
         'Invoice for September', 60, 1, 0, 1, None),
    ]
    ids = {'co': co, 'boss': boss, 'cand': cand}
    for r in rows:
        c.execute("INSERT INTO emails (owner_id,user_id,msg_id,folder,from_addr,from_name,to_addr,subject,body,snippet,"
                  "date_ts,is_read,is_starred,is_archived,candidate_id,created_at) VALUES (?,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (co, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[6][:80], now - r[7] * 86400, r[8], r[9], r[10],
                   r[11], '2026-11-01'))
        ids[r[0]] = c.lastrowid
    c.execute("INSERT INTO email_attachments (owner_id,email_id,direction,filename,size) VALUES (?,?,'in','Offer_Rahul.pdf',100)",
              (co, ids['<a1@mail>']))
    conn.commit(); conn.close()
    yield server, ids


def client(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


def search(srv, q, folder=''):
    server, ids = srv
    r = client(server, ids['boss']).get('/api/emailbox/list', query_string={'q': q, 'folder': folder})
    assert r.status_code == 200, r.get_json()
    return {e['subject'] for e in r.get_json()['emails']}


# ── search ────────────────────────────────────────────────────────────────
def test_search_operators(srv):
    assert search(srv, 'from:priya') == {'Offer letter for Rahul'}
    assert search(srv, 'to:rahul') == {'Interview on Friday'}
    assert search(srv, 'subject:"offer letter"') == {'Offer letter for Rahul'}
    assert search(srv, 'has:attachment') == {'Offer letter for Rahul'}
    assert search(srv, 'filename:pdf') == {'Offer letter for Rahul'}
    assert search(srv, 'offer_rahul') == {'Offer letter for Rahul'}          # attachment name, as a plain word
    assert search(srv, 'is:unread') == {'Offer letter for Rahul'}
    assert search(srv, 'is:starred') == {'Offer letter for Rahul'}
    assert search(srv, 'in:sent') == {'Interview on Friday'}
    assert search(srv, 'candidate:rahul') == {'Offer letter for Rahul', 'Interview on Friday'}
    assert search(srv, 'newsletter OR invoice') == {'Market newsletter', 'Invoice from vendor'}
    assert 'Market newsletter' not in search(srv, 'agency -newsletter')
    assert search(srv, 'newer_than:5d') == {'Offer letter for Rahul', 'Market newsletter'}
    assert search(srv, 'older_than:30d') == {'Invoice from vendor'}
    assert search(srv, 'rahul friday') == {'Interview on Friday'}            # every word, any order


def test_search_from_inbox_covers_all_mail_and_returns_terms(srv):
    server, ids = srv
    # archived mail is found by a search started in the Inbox (Gmail behaviour)
    assert search(srv, 'invoice', folder='Inbox') == {'Invoice from vendor'}
    # but a folder chip still narrows the search
    assert search(srv, 'rahul', folder='Sent') == {'Interview on Friday'}
    r = client(server, ids['boss']).get('/api/emailbox/list', query_string={'q': 'from:priya offer'}).get_json()
    assert r['terms'] == ['priya', 'offer'] and r['scope'] == 'anywhere'


def test_bad_query_does_not_break(srv):
    assert search(srv, '"unbalanced quote') == set()
    assert search(srv, 'is:whatever') >= {'Offer letter for Rahul'}       # unknown operator ignored
    assert search(srv, '10:30') == set()                                   # treated as text


# ── delete reaches Gmail ──────────────────────────────────────────────────
def ops(server, co):
    conn = server.get_db()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM mail_gmail_ops WHERE company_id=? AND "
                                              "COALESCE(done_at,'')='' ORDER BY id", (co,)).fetchall()]
    finally:
        conn.close()


def test_delete_queues_gmail_trash_and_undo_cancels(srv):
    server, ids = srv
    c = client(server, ids['boss'])
    eid = ids['<a2@mail>']
    r = c.post('/api/emailbox/bulk', json={'ids': [eid], 'action': 'delete'}).get_json()
    assert r['ok'] and r['gmail'] == 1
    q = ops(server, ids['co'])
    assert [(o['msg_id'], o['action'], o['folder']) for o in q] == [('<a2@mail>', 'trash', 'Inbox')]
    # shows in Trash, not in the inbox
    assert 'Market newsletter' in search(srv, '', folder='Trash')
    assert 'Market newsletter' not in search(srv, '', folder='Inbox')
    assert c.get('/api/emailbox/counts').get_json()['trash'] >= 1
    # Undo before it reached Gmail: the waiting trash op is simply cancelled
    r = c.post('/api/emailbox/bulk', json={'ids': [eid], 'action': 'restore'}).get_json()
    assert r['ok'] and r['gmail'] == 0
    assert ops(server, ids['co']) == []
    assert 'Market newsletter' in search(srv, '', folder='Inbox')


def test_trashed_conversation_opens_from_trash(srv):
    server, ids = srv
    c = client(server, ids['boss'])
    eid = ids['<a4@mail>']
    c.post('/api/emailbox/bulk', json={'ids': [eid], 'action': 'delete'})
    t = c.get('/api/emailbox/thread', query_string={'eid': eid}).get_json()
    assert [m['id'] for m in t['thread']] == [eid]
    c.post('/api/emailbox/bulk', json={'ids': [eid], 'action': 'restore'})


def test_drafts_and_fake_ids_never_go_to_gmail(srv):
    from modules import mail_power as mp
    server, ids = srv
    conn = server.get_db()
    n = mp.enqueue(conn, ids['co'], 0, [{'msg_id': 'Inbox:12', 'folder': 'Inbox'},
                                        {'msg_id': '<d@x>', 'folder': 'Drafts', 'is_draft': 1},
                                        {'msg_id': '', 'folder': 'Inbox'}], 'trash')
    conn.close()
    assert n == 0


class FakeIMAP:
    """Just enough of imaplib.IMAP4 for apply_ops / mirror_gmail_trash."""
    capabilities = ('IMAP4REV1', 'MOVE')

    def __init__(self, boxes):
        self.boxes = boxes          # folder name -> {uid: message-id}
        self.sel = None
        self.moves = []

    def list(self):
        return 'OK', [b'(\\HasNoChildren) "/" "INBOX"', b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"',
                      b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"', b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Bin"']

    def select(self, name, readonly=False):
        name = name.strip('"')
        if name not in self.boxes:
            return 'NO', [b'']
        self.sel = name
        return 'OK', [str(len(self.boxes[name])).encode()]

    def uid(self, cmd, *args):
        box = self.boxes[self.sel]
        if cmd == 'SEARCH':
            mid = args[-1].strip('"')
            return 'OK', [b' '.join(str(u).encode() for u, m in box.items() if m == mid)]
        if cmd == 'MOVE':
            dest = args[1].strip('"')
            for u in args[0].split(','):
                m = box.pop(int(u))
                self.boxes[dest][max(self.boxes[dest] or {0: 0}) + 1] = m
                self.moves.append((m, self.sel, dest))
            return 'OK', [b'']
        raise AssertionError(cmd)

    def search(self, charset, crit):
        return 'OK', [b' '.join(str(u).encode() for u in self.boxes[self.sel])]

    def fetch(self, nums, what):
        box = self.boxes[self.sel]
        return 'OK', [(b'%d (BODY[HEADER])' % int(n), ('Message-ID: %s\r\n\r\n' % box[int(n)]).encode())
                      for n in nums.split(',')]


def test_apply_ops_moves_to_gmail_trash_and_back():
    from modules import mail_power as mp
    M = FakeIMAP({'INBOX': {1: '<x1@m>'}, '[Gmail]/All Mail': {1: '<x1@m>', 2: '<x2@m>'},
                  '[Gmail]/Sent Mail': {}, '[Gmail]/Bin': {}})
    assert mp.list_folders(M)['trash'] == '[Gmail]/Bin'       # localised name found by \Trash flag
    res = mp.apply_ops(M, [{'id': 1, 'msg_id': '<x2@m>', 'action': 'trash', 'folder': 'Inbox'},
                           {'id': 2, 'msg_id': '<gone@m>', 'action': 'trash', 'folder': 'Inbox'}])
    assert res[1] == (True, '') and res[2][0] is True and 'Not found' in res[2][1]
    assert ('<x2@m>', '[Gmail]/All Mail', '[Gmail]/Bin') in M.moves
    res = mp.apply_ops(M, [{'id': 3, 'msg_id': '<x2@m>', 'action': 'restore', 'folder': 'Sent'}])
    assert res[3] == (True, '') and ('<x2@m>', '[Gmail]/Bin', '[Gmail]/Sent Mail') in M.moves


def test_process_sends_queue_to_gmail(srv, monkeypatch):
    from modules import mail_power as mp
    server, ids = srv
    M = FakeIMAP({'INBOX': {}, '[Gmail]/All Mail': {5: '<a3@mail>'}, '[Gmail]/Sent Mail': {}, '[Gmail]/Bin': {}})
    M.logout = lambda: None
    monkeypatch.setattr(mp, '_imap_login', lambda: (M, ''))
    c = client(server, ids['boss'])
    c.post('/api/emailbox/bulk', json={'ids': [ids['<a3@mail>']], 'action': 'delete'})
    assert mp.process(ids['co'], 0) == 1
    assert ops(server, ids['co']) == [] and M.boxes['[Gmail]/Bin'] == {1: '<a3@mail>'}
    c.post('/api/emailbox/bulk', json={'ids': [ids['<a3@mail>']], 'action': 'restore'})
    assert mp.process(ids['co'], 0) == 1
    assert M.boxes['[Gmail]/Sent Mail'] and not M.boxes['[Gmail]/Bin']


def test_process_failure_keeps_op_for_retry(srv, monkeypatch):
    from modules import mail_power as mp
    server, ids = srv
    def boom():
        raise OSError('timed out')
    monkeypatch.setattr(mp, '_imap_login', boom)
    c = client(server, ids['boss'])
    c.post('/api/emailbox/bulk', json={'ids': [ids['<a1@mail>']], 'action': 'delete'})
    assert mp.process(ids['co'], 0) == 0
    q = ops(server, ids['co'])
    assert q and q[0]['tries'] == 1 and 'timed out' in q[0]['last_error']
    st = c.get('/api/mail/gmail-ops').get_json()
    assert st['pending'] == 1
    c.post('/api/emailbox/bulk', json={'ids': [ids['<a1@mail>']], 'action': 'restore'})
    assert ops(server, ids['co']) == []


def test_mail_deleted_in_gmail_is_deleted_here(srv):
    from modules import mail_power as mp
    server, ids = srv
    M = FakeIMAP({'INBOX': {}, '[Gmail]/All Mail': {}, '[Gmail]/Sent Mail': {}, '[Gmail]/Bin': {7: '<a2@mail>'}})
    conn = server.get_db()
    n = mp.mirror_gmail_trash(M, conn, ids['co'], 0, '01-Oct-2026')
    conn.close()
    assert n == 1
    assert 'Market newsletter' in search(srv, '', folder='Trash')
    client(server, ids['boss']).post('/api/emailbox/bulk', json={'ids': [ids['<a2@mail>']], 'action': 'restore'})


# ── DeepSeek compose ──────────────────────────────────────────────────────
class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def test_ai_write_uses_context_and_cleans_html(srv, monkeypatch):
    server, ids = srv
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-key')
    seen = {}

    def fake(key, payload, timeout=60, endpoint=''):
        seen['payload'] = payload
        out = {'subject': 'Re: Offer letter for Rahul',
               'body_html': '<p onclick="x()">Thanks Priya,</p><script>alert(1)</script><a href="javascript:x">x</a><p>Best regards,</p>'}
        return _Resp({'choices': [{'message': {'content': json.dumps(out)}}]})
    monkeypatch.setattr(server, 'call_deepseek', fake)
    c = client(server, ids['boss'])
    r = c.post('/api/mail/ai-write', json={'mode': 'reply', 'prompt': 'confirm we got it', 'tone': 'friendly',
                                          'to': 'priya@client.com', 'reply_to_email_id': ids['<a1@mail>']}).get_json()
    assert r['ok'] and r['subject'] == 'Re: Offer letter for Rahul'
    assert '<script' not in r['body_html'] and 'onclick' not in r['body_html'] and 'javascript' not in r['body_html']
    assert r['body_html'].startswith('<p>Thanks Priya,</p>')
    user_msg = seen['payload']['messages'][1]['content']
    assert 'confirm we got it' in user_msg and 'offer letter attached' in user_msg and 'friendly' in user_msg

    r = c.post('/api/mail/ai-write', json={'mode': 'write', 'prompt': 'invite for interview', 'to': 'rahul@example.com'}).get_json()
    assert 'Rahul Verma' in seen['payload']['messages'][1]['content']      # candidate context from the To address


def test_ai_write_validation(srv, monkeypatch):
    server, ids = srv
    c = client(server, ids['boss'])
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-key')
    assert c.post('/api/mail/ai-write', json={'mode': 'write'}).status_code == 400
    assert c.post('/api/mail/ai-write', json={'mode': 'shorten'}).status_code == 400
    assert c.post('/api/mail/ai-write', json={'mode': 'hack'}).status_code == 400


def test_clean_html_plain_text():
    from modules.mail_power import clean_html
    assert clean_html('Hi Rahul,\n\nSee you <soon>') == '<p>Hi Rahul,</p><p>See you &lt;soon&gt;</p>'


# ── all mailboxes at the same time ───────────────────────────────────────
def test_tick_syncs_mailboxes_in_parallel(srv, monkeypatch):
    from modules import mail_sync as m
    boxes = [(1, 0), (1, 5), (2, 0), (3, 7)]
    monkeypatch.setattr(m, 'mailboxes', lambda: boxes)
    monkeypatch.setattr(m, 'status_row', lambda oid, uid: {})
    monkeypatch.delenv('MAIL_AUTOSYNC', raising=False)
    live, peak, lock = [0], [0], threading.Lock()

    def slow(oid, uid):
        with lock:
            live[0] += 1; peak[0] = max(peak[0], live[0])
        time.sleep(0.2)
        with lock:
            live[0] -= 1
    monkeypatch.setattr(m, 'run_box', slow)
    t0 = time.time()
    assert m.tick() == 4
    assert peak[0] == 4 and time.time() - t0 < 0.6


def test_sync_now_all_starts_every_mailbox(srv, monkeypatch):
    from modules import mail_sync as m
    server, ids = srv
    started = []
    monkeypatch.setattr(m, 'user_boxes', lambda oid, uid, admin: [(oid, uid), (oid, 0)])
    monkeypatch.setattr(m, '_start_bg', lambda oid, uid: started.append((oid, uid)))
    r = client(server, ids['boss']).post('/api/mail/sync-now', json={'box': 'all'}).get_json()
    assert r['ok'] and r['boxes'] == 2
    assert sorted(started) == sorted([(ids['co'], ids['boss']), (ids['co'], 0)])
