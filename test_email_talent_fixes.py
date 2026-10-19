"""
Email Box + Talent fixes (audit Phase A).

Run:   pytest test_email_talent_fixes.py -q

1. Inbox list returns thread_id, so conversations group.
2. Gmail \\Seen flags are parsed; read-in-Gmail mails become read (never the reverse).
3. Talent keyword search matches all words in any order; columns sort server-side.
4. AI search is gated on the embedding key it actually uses (not Gemini).
5. Re-index and index status stay inside the caller's company; reset is admin-only.
"""
from test_naukri_screen import srv, cl, _seed_dupes, CO, OTHER, ADMIN, RIYA, M_A  # noqa: F401  (fixtures)


def test_parse_seen_flags():
    import server
    lines = [b'1 (FLAGS (\\Seen))', b'2 (FLAGS ())', (b'3 (FLAGS (\\Answered \\Seen) UID 9)', b''),
             b'4 (UID 5 FLAGS (\\Flagged))', b'garbage']
    assert server._parse_seen_flags(lines) == {b'1', b'3'}


def test_seen_flags_only_mark_read(srv):
    conn = srv.get_db()
    conn.execute("DELETE FROM emails WHERE owner_id=?", (CO,))
    for mid, rd in (('<s1@x>', 0), ('<s2@x>', 0), ('<s3@x>', 1)):
        conn.execute("INSERT INTO emails (owner_id,msg_id,folder,subject,is_read,date_ts) VALUES (?,?,?,?,?,1)",
                     (CO, mid, 'Inbox', 'x', rd))
    conn.commit()

    class FakeM:
        def fetch(self, ids, what):
            # message 1 read in Gmail, 2 unread, 3 unread in Gmail but read in the ATS
            return 'OK', [b'1 (FLAGS (\\Seen))', b'2 (FLAGS ())', b'3 (FLAGS ())']
    n = srv._sync_seen_flags(FakeM(), conn, CO, [b'1', b'2', b'3'], 'Inbox',
                             {b'1': '<s1@x>', b'2': '<s2@x>', b'3': '<s3@x>'})
    got = dict(conn.execute("SELECT msg_id,is_read FROM emails WHERE owner_id=?", (CO,)).fetchall())
    conn.close()
    assert n == 1
    assert got == {'<s1@x>': 1, '<s2@x>': 0, '<s3@x>': 1}


def test_inbox_list_has_thread_id(srv):
    conn = srv.get_db()
    conn.execute("DELETE FROM emails WHERE owner_id=?", (CO,))
    conn.execute("INSERT INTO emails (owner_id,msg_id,folder,subject,thread_id,date_ts) VALUES (?,?,?,?,?,2)",
                 (CO, '<t1@x>', 'Inbox', 'Re: job', 'th-1'))
    conn.execute("INSERT INTO emails (owner_id,msg_id,folder,subject,thread_id,date_ts) VALUES (?,?,?,?,?,1)",
                 (CO, '<t0@x>', 'Sent', 'job', 'th-1'))
    conn.commit(); conn.close()
    r = cl(srv, ADMIN).get('/api/emailbox/list').get_json()
    assert r['ok'] and {e['thread_id'] for e in r['emails']} == {'th-1'}


def test_keyword_search_all_words_any_order(srv):
    _seed_dupes(srv)
    c = cl(srv, ADMIN)
    r = c.get('/api/central-db/search?q=power%20solar').get_json()
    names = [x['name'] for x in r['candidates']]
    assert 'Asha Verma' in names                 # "Azure Power" + "Solar O&M Engineer"
    r = c.get('/api/central-db/search?q=power%20wind').get_json()
    assert r['total'] == 0


def test_search_sort(srv):
    _seed_dupes(srv)
    c = cl(srv, ADMIN)
    up = [x['name'] for x in c.get('/api/central-db/search?sort=name&dir=asc').get_json()['candidates']]
    down = [x['name'] for x in c.get('/api/central-db/search?sort=name&dir=desc').get_json()['candidates']]
    assert up == sorted(up, key=str.lower) and down == list(reversed(up))
    # other tenants never appear
    assert 'Meena Joshi' not in up


def test_ai_search_gated_on_embedding_key(srv, monkeypatch):
    real = srv.get_setting
    monkeypatch.setattr(srv, 'get_setting',
                        lambda k, d='': '' if k == 'embedding_api_key' else ('g-key' if k == 'gemini_api_key' else real(k, d)))
    r = cl(srv, ADMIN).post('/api/ai/search', json={'query': 'solar'})
    assert r.status_code == 400 and r.get_json().get('code') == 'no_embedding_key'


def test_reindex_scoped_and_reset_admin_only(srv, monkeypatch):
    _seed_dupes(srv)
    conn = srv.get_db()
    conn.execute("UPDATE candidates SET embedding='[1]' WHERE owner_id IN (?,?)", (CO, OTHER))
    conn.commit(); conn.close()
    real = srv.get_setting
    monkeypatch.setattr(srv, 'get_setting', lambda k, d='': 'jina-key' if k == 'embedding_api_key' else real(k, d))
    monkeypatch.setattr(srv, 'embed_candidate_row', lambda conn, c, key: {'status': 'missing'})
    # a recruiter cannot wipe the index
    r = cl(srv, RIYA).post('/api/ai/reindex', json={'force': True, 'reset': True})
    assert r.status_code == 403
    # an admin reset touches only their own company
    r = cl(srv, ADMIN).post('/api/ai/reindex', json={'force': True, 'reset': True, 'batch': 1}).get_json()
    assert r['ok']
    conn = srv.get_db()
    other = conn.execute("SELECT COUNT(*) n FROM candidates WHERE owner_id=? AND embedding='[1]'", (OTHER,)).fetchone()['n']
    mine = conn.execute("SELECT COUNT(*) n FROM candidates WHERE owner_id=? AND embedding='[1]'", (CO,)).fetchone()['n']
    conn.close()
    assert other >= 1 and mine == 0
    st = cl(srv, ADMIN).get('/api/ai/index-status').get_json()
    conn = srv.get_db()
    mine_total = conn.execute("SELECT COUNT(*) n FROM candidates WHERE owner_id=?", (CO,)).fetchone()['n']
    conn.close()
    assert st['total'] == mine_total and 'has_embedding_key' in st
