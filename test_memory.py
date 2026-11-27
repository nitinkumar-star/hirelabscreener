"""
Memory (modules/memory.py) — Wave 1 backend tests.

Run:   pytest test_memory.py -q
Boots the real server.py (real migrations, real tenant + recruiter guards) —
nothing is mocked. Covers: CRUD, validation, history rows, archive/unarchive,
expiry filtering, search + sort, dedupe, the context briefing (candidate ->
mandate -> client, incl. client-side people and the candidate's feedback
fields), company isolation and recruiter scoping.
"""
import os
import sys
import datetime
import tempfile

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CO, OTHER_CO = 961, 962
ADMIN, RIYA, RAJ, OTHER_ADMIN = 9601, 9602, 9603, 9604
M_RIYA, M_ADMIN, M_OTHER = 96101, 96102, 96103
C_RIYA, C_ADMIN, C_OTHER = 96201, 96202, 96203
CRM_LK, CRM_ADMIN = 96301, 96302
LK = 'Lauritz & Knudsen'


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='memory_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db()
    c = conn.cursor()
    now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})",
                  list(kw.values()))

    ins('companies', id=CO, name='Memory Co', status='active', created_at=now)
    ins('companies', id=OTHER_CO, name='Memory Other', status='active', created_at=now)
    for uid, un, co, adm in ((ADMIN, 'mem_admin', CO, 1), (RIYA, 'mem_riya', CO, 0),
                             (RAJ, 'mem_raj', CO, 0), (OTHER_ADMIN, 'mem_other', OTHER_CO, 1)):
        ins('users', id=uid, username=un, password_hash='x', display_name=un.split('_')[1].title(),
            role='user', status='approved', company_id=co, is_company_admin=adm, created_at=now)
    ins('crm_clients', id=CRM_LK, company_id=CO, name=LK)
    ins('crm_clients', id=CRM_ADMIN, company_id=CO, name='AdminClient')
    ins('mandates', id=M_RIYA, client=LK, role='Distribution Manager', location='Haridwar', owner_id=CO,
        assigned_user_id=RIYA, status='active', crm_client_id=CRM_LK, created_at=now)
    ins('mandates', id=M_ADMIN, client='AdminClient', role='AdminRole', owner_id=CO,
        assigned_user_id=ADMIN, status='active', crm_client_id=CRM_ADMIN, created_at=now)
    ins('mandates', id=M_OTHER, client='OtherClient', role='OtherRole', owner_id=OTHER_CO,
        assigned_user_id=OTHER_ADMIN, status='active', created_at=now)
    ins('candidates', id=C_RIYA, mandate_id=M_RIYA, name='Vipin Kumar Tomar', phone='9100000001',
        owner_id=CO, stage='Screening', created_at=now,
        client_feedback='Ref check not positive', recruiter_feedback='Good on channel sales',
        general_comments='Notice 30 days')
    ins('candidates', id=C_ADMIN, mandate_id=M_ADMIN, name='Admin Cand', phone='9100000002',
        owner_id=CO, stage='Screening', created_at=now)
    ins('candidates', id=C_OTHER, mandate_id=M_OTHER, name='Other Cand', phone='9100000003',
        owner_id=OTHER_CO, stage='Screening', created_at=now)
    ins('mandate_client_notes', mandate_id=M_RIYA, owner_id=CO, note='Hire will handle the Ghaziabad branch',
        created_by=ADMIN, created_at=now, is_active=1)
    conn.commit()
    conn.close()
    yield server


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


def new(c, **body):
    r = c.post('/api/memory', json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()['id']


def ids(resp):
    return {n['id'] for n in resp.get_json()['notes']}


def day(server, delta):
    return (server._ist_now().date() + datetime.timedelta(days=delta)).isoformat()


# ── tables ────────────────────────────────────────────────────────────────
def test_tables_and_indexes_exist(srv):
    conn = srv.get_db()
    try:
        cols = {r['name'] for r in conn.execute('PRAGMA table_info(memory_notes)')}
        for col in ('company_id', 'owner_id', 'scope', 'entity_id', 'entity_name', 'client_name', 'title',
                    'content', 'tags', 'importance', 'status', 'source', 'valid_until', 'created_at',
                    'updated_at', 'created_by', 'updated_by'):
            assert col in cols
        hcols = {r['name'] for r in conn.execute('PRAGMA table_info(memory_note_history)')}
        assert {'note_id', 'old_content', 'new_content', 'changed_by', 'changed_at'} <= hcols
        idx = {r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                               "AND tbl_name='memory_notes'")}
        assert {'idx_memn_scope_entity', 'idx_memn_entity_name', 'idx_memn_status'} <= idx
        # the older RME tables are untouched and still there
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='rme_memories'").fetchone()
    finally:
        conn.close()


def test_requires_login(srv):
    c = srv.app.test_client()
    assert c.get('/api/memory').status_code == 401
    assert c.post('/api/memory', json={'scope': 'general', 'content': 'x'}).status_code == 401


# ── CRUD + history ───────────────────────────────────────────────────────
def test_crud_and_history(srv):
    a = cl(srv, ADMIN)
    nid = new(a, scope='team', entity_id=RIYA, title='Riya — coordination',
              content='Riya handles candidate coordination and interview scheduling.',
              tags=['team', 'scheduling'], importance='high')
    r = a.get(f'/api/memory/{nid}').get_json()['note']
    assert r['entity_name'] == 'Riya'                       # filled from the linked user
    assert r['tags'] == ['team', 'scheduling'] and r['importance'] == 'high'
    assert r['source'] == 'user' and r['created_by'] == ADMIN and r['owner_id'] == CO
    assert r['can_edit'] is True
    assert [h['action'] for h in r['history']] == ['create']

    u = a.put(f'/api/memory/{nid}', json={'content': 'Riya handles coordination, scheduling and ref checks.',
                                         'importance': 'normal'})
    assert u.status_code == 200 and set(u.get_json()['changed']) == {'content', 'importance'}
    r = a.get(f'/api/memory/{nid}').get_json()['note']
    assert r['content'].endswith('ref checks.') and r['updated_by'] == ADMIN
    assert r['title'] == 'Riya — coordination'               # untouched field kept
    h = r['history'][0]
    assert h['action'] == 'update' and h['old_content'].startswith('Riya handles candidate')
    assert h['new_content'].endswith('ref checks.') and h['changed_by'] == ADMIN

    # an update that changes nothing writes no history row
    a.put(f'/api/memory/{nid}', json={'importance': 'normal'})
    assert len(a.get(f'/api/memory/{nid}').get_json()['note']['history']) == 2
    assert a.get('/api/memory/999999999').status_code == 404


def test_validation(srv):
    a = cl(srv, ADMIN)
    bad = [({'scope': 'nope', 'content': 'x'}, 400),
           ({'scope': 'general', 'content': '  '}, 400),
           ({'scope': 'general', 'entity_id': M_RIYA, 'content': 'x'}, 400),
           ({'scope': 'client', 'content': 'x'}, 400),               # needs entity_name or entity_id
           ({'scope': 'general', 'content': 'x', 'importance': 'urgent'}, 400),
           ({'scope': 'general', 'content': 'x', 'valid_until': 'next week'}, 400),
           ({'scope': 'general', 'content': 'x', 'valid_until': '2026-02-30'}, 400),
           ({'scope': 'general', 'content': 'x', 'source': 'robot'}, 400),
           ({'scope': 'mandate', 'entity_id': M_OTHER, 'content': 'x'}, 404),   # other company's job
           ({'scope': 'candidate', 'entity_id': C_OTHER, 'content': 'x'}, 404),
           ({'scope': 'mandate', 'entity_id': 'abc', 'content': 'x'}, 400)]
    for body, code in bad:
        assert a.post('/api/memory', json=body).status_code == code, body
    # title defaults to the first line of content
    nid = new(a, scope='general', content='Always share CTC in LPA\nsecond line')
    assert a.get(f'/api/memory/{nid}').get_json()['note']['title'] == 'Always share CTC in LPA'


def test_entity_defaults_from_links(srv):
    a = cl(srv, ADMIN)
    m = a.get(f"/api/memory/{new(a, scope='mandate', entity_id=M_RIYA, content='x1')}").get_json()['note']
    assert m['entity_name'] == 'Distribution Manager — Haridwar' and m['client_name'] == LK
    k = a.get(f"/api/memory/{new(a, scope='candidate', entity_id=C_RIYA, content='x2')}").get_json()['note']
    assert k['entity_name'] == 'Vipin Kumar Tomar' and k['client_name'] == LK
    cnote = a.get(f"/api/memory/{new(a, scope='client', entity_id=CRM_LK, content='x3')}").get_json()['note']
    assert cnote['entity_name'] == LK and cnote['client_name'] == LK
    # free-text client (no CRM id): client_name follows entity_name
    f = a.get(f"/api/memory/{new(a, scope='client', entity_name='Hitachi Energy', content='x4')}").get_json()['note']
    assert f['client_name'] == 'Hitachi Energy' and f['entity_id'] is None
    for n in (m, k, cnote, f):
        a.post(f"/api/memory/{n['id']}/archive")


# ── archive / unarchive ──────────────────────────────────────────────────
def test_archive_unarchive(srv):
    a = cl(srv, ADMIN)
    nid = new(a, scope='general', title='Archive me', content='temporary fact')
    assert a.post(f'/api/memory/{nid}/archive').get_json()['status'] == 'archived'
    assert nid not in ids(a.get('/api/memory?q=temporary'))
    assert nid in ids(a.get('/api/memory?q=temporary&include_archived=true'))
    assert nid in ids(a.get('/api/memory?status=archived'))
    assert a.get(f'/api/memory/{nid}').status_code == 200    # still readable directly
    assert a.post(f'/api/memory/{nid}/unarchive').get_json()['status'] == 'active'
    assert nid in ids(a.get('/api/memory?q=temporary'))
    acts = [h['action'] for h in a.get(f'/api/memory/{nid}').get_json()['note']['history']]
    assert acts == ['unarchive', 'archive', 'create']
    conn = srv.get_db()
    try:                                                     # soft delete only: the row is still there
        assert conn.execute('SELECT COUNT(*) n FROM memory_notes WHERE id=?', (nid,)).fetchone()['n'] == 1
    finally:
        conn.close()


# ── expiry ───────────────────────────────────────────────────────────────
def test_expiry_filtering(srv):
    a = cl(srv, ADMIN)
    old = new(a, scope='general', title='Slot gone', content='expirytest slot yesterday',
              valid_until=day(srv, -1))
    today = new(a, scope='general', title='Slot today', content='expirytest slot today', valid_until=day(srv, 0))
    later = new(a, scope='general', title='Slot later', content='expirytest slot later',
                valid_until=day(srv, 5) + 'T17:00:00')
    got = ids(a.get('/api/memory?q=expirytest'))
    assert old not in got and {today, later} <= got
    r = a.get('/api/memory?q=expirytest&include_expired=1').get_json()['notes']
    assert {n['id'] for n in r} >= {old, today, later}
    assert {n['id']: n['expired'] for n in r}[old] is True
    assert {n['id']: n['expired'] for n in r}[today] is False
    # clearing valid_until brings it back
    a.put(f'/api/memory/{old}', json={'valid_until': ''})
    assert old in ids(a.get('/api/memory?q=expirytest'))


# ── search + sort ────────────────────────────────────────────────────────
def test_search_filters_and_sort(srv):
    a = cl(srv, ADMIN)
    low = new(a, scope='person', entity_name='Nishtha Bajaj', client_name=LK, importance='low',
              title='Nishtha low', content='sorttest low')
    high = new(a, scope='person', entity_name='Nishtha Bajaj', client_name=LK, importance='high',
               title='Nishtha — DM grade', content='sorttest Rejects 10-yr profiles for DM roles',
               tags=['rejection-pattern'])
    norm = new(a, scope='person', entity_name='Nishtha Bajaj', client_name=LK, title='Nishtha normal',
               content='sorttest normal')
    r = a.get('/api/memory?q=sorttest').get_json()['notes']
    assert [n['id'] for n in r][:3] == [high, norm, low]
    assert ids(a.get('/api/memory?entity_name=nishtha&q=sorttest')) == {low, high, norm}
    assert ids(a.get('/api/memory?q=sorttest+10-yr')) == {high}          # every word must match
    assert ids(a.get('/api/memory?q=rejection-pattern')) >= {high}      # tags are searched
    assert ids(a.get('/api/memory?q=sorttest&importance=high')) == {high}
    assert ids(a.get('/api/memory?q=sorttest&scope=client')) == set()
    assert len(a.get('/api/memory?q=sorttest&limit=2').get_json()['notes']) == 2
    assert a.get('/api/memory?scope=bad').status_code == 400
    # LIKE wildcards in the query are literal
    assert a.get('/api/memory?q=%25').status_code == 200


# ── dedupe ───────────────────────────────────────────────────────────────
def test_dedupe_updates_instead_of_duplicating(srv):
    a = cl(srv, ADMIN)
    first = a.post('/api/memory', json={'scope': 'client', 'entity_id': CRM_LK, 'title': 'Prefers DM-level profiles',
                                        'content': 'Grade 4, 6-8 LPA', 'dedupe': True, 'source': 'claude'})
    assert first.status_code == 201 and first.get_json()['action'] == 'created'
    nid = first.get_json()['id']
    again = a.post('/api/memory', json={'scope': 'client', 'entity_id': CRM_LK, 'title': 'prefers DM level profiles',
                                        'content': 'Grade 4, 6-8 LPA; no 10-yr profiles', 'dedupe': True,
                                        'source': 'claude'})
    j = again.get_json()
    assert again.status_code == 200 and j['action'] == 'updated' and j['deduped'] and j['id'] == nid
    assert j['note']['content'].endswith('no 10-yr profiles')
    assert a.get(f'/api/memory/{nid}').get_json()['note']['history'][0]['action'] == 'update'
    # a different title is a different note
    other = a.post('/api/memory', json={'scope': 'client', 'entity_id': CRM_LK, 'title': 'Interview panel on Fridays',
                                        'content': 'x', 'dedupe': True})
    assert other.get_json()['action'] == 'created' and other.get_json()['id'] != nid
    # without dedupe a second identical note is allowed (explicit choice)
    plain = a.post('/api/memory', json={'scope': 'client', 'entity_id': CRM_LK, 'title': 'Prefers DM-level profiles',
                                        'content': 'dup'})
    assert plain.status_code == 201 and plain.get_json()['id'] != nid
    for x in (nid, other.get_json()['id'], plain.get_json()['id']):
        a.post(f'/api/memory/{x}/archive')


# ── company isolation ────────────────────────────────────────────────────
def test_company_isolation(srv):
    a, o = cl(srv, ADMIN), cl(srv, OTHER_ADMIN)
    nid = new(a, scope='general', title='Secret of Memory Co', content='isolationtest secret')
    assert o.get(f'/api/memory/{nid}').status_code == 404
    assert o.put(f'/api/memory/{nid}', json={'content': 'hacked'}).status_code == 404
    assert o.post(f'/api/memory/{nid}/archive').status_code == 404
    assert o.post(f'/api/memory/{nid}/unarchive').status_code == 404
    assert nid not in ids(o.get('/api/memory?q=isolationtest&include_archived=1&include_expired=1'))
    assert o.get(f'/api/memory/context?candidate_id={C_RIYA}').status_code == 404
    assert o.get(f'/api/memory/context?mandate_id={M_RIYA}').status_code == 404
    ctx = o.get('/api/memory/context?client=lauritz').get_json()
    assert ctx['client']['notes'] == [] and ctx['mandates'] == []
    assert o.post('/api/memory', json={'scope': 'candidate', 'entity_id': C_RIYA, 'content': 'x'}).status_code == 404
    assert a.get(f'/api/memory/{nid}').get_json()['note']['content'] == 'isolationtest secret'


# ── context briefing ─────────────────────────────────────────────────────
@pytest.fixture(scope='module')
def ctx_notes(srv):
    a = cl(srv, ADMIN)
    n = {
        'cand': new(a, scope='candidate', entity_id=C_RIYA, title='Ref check', content='Ref check not positive (client, 26 Nov 2026)'),
        'mand': new(a, scope='mandate', entity_id=M_RIYA, title='Priority', content='Top priority role'),
        'client': new(a, scope='client', entity_id=CRM_LK, title='Grade', content='Wants DM-level (grade 4)'),
        'client_free': new(a, scope='client', entity_name=LK, title='Payment', content='Pays in 45 days'),
        'person': new(a, scope='person', entity_name='Nishtha Bajaj', client_name=LK, title='Nishtha',
                      content='Talent Attraction; priority Haridwar, Amritsar'),
        'other_client': new(a, scope='client', entity_id=CRM_ADMIN, title='Admin client', content='x'),
        'admin_mand': new(a, scope='mandate', entity_id=M_ADMIN, title='Admin job', content='x'),
        'admin_cand': new(a, scope='candidate', entity_id=C_ADMIN, title='Admin cand', content='x'),
        'gen_high': new(a, scope='general', title='Always LPA', content='Quote CTC in LPA', importance='high'),
        'team': new(a, scope='team', entity_id=RAJ, title='Raj', content='Raj handles solar mandates'),
        'expired_cand': new(a, scope='candidate', entity_id=C_RIYA, title='Slot', content='Interview slot',
                            valid_until=day(srv, -2)),
    }
    return n


def test_context_candidate(srv, ctx_notes):
    a = cl(srv, ADMIN)
    j = a.get(f'/api/memory/context?candidate_id={C_RIYA}').get_json()
    assert j['type'] == 'candidate' and j['candidate']['name'] == 'Vipin Kumar Tomar'
    assert j['candidate_fields'] == {'client_feedback': 'Ref check not positive',
                                     'recruiter_feedback': 'Good on channel sales',
                                     'general_comments': 'Notice 30 days'}
    assert ctx_notes['cand'] in {n['id'] for n in j['notes']}
    assert ctx_notes['expired_cand'] not in {n['id'] for n in j['notes']}
    assert j['mandate']['id'] == M_RIYA and ctx_notes['mand'] in {n['id'] for n in j['mandate']['notes']}
    assert j['mandate']['client_notes_on_job'][0]['note'] == 'Hire will handle the Ghaziabad branch'
    cn = {n['id'] for n in j['client']['notes']}
    assert {ctx_notes['client'], ctx_notes['client_free']} <= cn and ctx_notes['other_client'] not in cn
    assert ctx_notes['person'] in {n['id'] for n in j['client']['people_notes']}
    assert ctx_notes['gen_high'] in {n['id'] for n in j['general_high']}
    j2 = a.get(f'/api/memory/context?candidate_id={C_RIYA}&include_expired=true').get_json()
    assert ctx_notes['expired_cand'] in {n['id'] for n in j2['notes']}
    # the candidate's own fields are read, never changed
    conn = srv.get_db()
    try:
        assert conn.execute('SELECT client_feedback FROM candidates WHERE id=?', (C_RIYA,)).fetchone()[0] == \
            'Ref check not positive'
    finally:
        conn.close()


def test_context_mandate_and_client(srv, ctx_notes):
    a = cl(srv, ADMIN)
    j = a.get(f'/api/memory/context?mandate_id={M_RIYA}').get_json()
    assert j['type'] == 'mandate' and ctx_notes['mand'] in {n['id'] for n in j['mandate']['notes']}
    assert ctx_notes['cand'] in {n['id'] for n in j['mandate']['candidate_notes']}
    assert ctx_notes['client'] in {n['id'] for n in j['client']['notes']}

    c = a.get('/api/memory/context?client=lauritz').get_json()
    assert c['type'] == 'client'
    assert {ctx_notes['client'], ctx_notes['client_free']} <= {n['id'] for n in c['client']['notes']}
    assert ctx_notes['person'] in {n['id'] for n in c['client']['people_notes']}
    assert [m['id'] for m in c['mandates']] == [M_RIYA]
    assert ctx_notes['mand'] in {n['id'] for n in c['mandates'][0]['notes']}

    for bad in ('', 'client=x&mandate_id=1', 'mandate_id=abc'):
        assert a.get('/api/memory/context?' + bad).status_code == 400
    assert a.get('/api/memory/context?candidate_id=999999999').status_code == 404


# ── recruiter scoping ────────────────────────────────────────────────────
def test_recruiter_read_scope(srv, ctx_notes):
    riya, raj = cl(srv, RIYA), cl(srv, RAJ)
    seen = ids(riya.get('/api/memory?limit=200'))
    for k in ('cand', 'mand', 'client', 'client_free', 'person', 'gen_high', 'team'):
        assert ctx_notes[k] in seen, k
    for k in ('other_client', 'admin_mand', 'admin_cand'):
        assert ctx_notes[k] not in seen, k
        assert riya.get(f"/api/memory/{ctx_notes[k]}").status_code == 404
    # Raj has no mandates: general + team only
    raj_seen = ids(raj.get('/api/memory?limit=200'))
    assert ctx_notes['gen_high'] in raj_seen and ctx_notes['team'] in raj_seen
    assert not raj_seen & {ctx_notes[k] for k in ('cand', 'mand', 'client', 'person')}
    assert raj.get(f'/api/memory/context?candidate_id={C_RIYA}').status_code == 404
    assert raj.get(f'/api/memory/context?mandate_id={M_RIYA}').status_code == 404
    assert raj.get('/api/memory/context?client=lauritz').get_json()['mandates'] == []
    # Riya's briefing works and leaves out the admin's client
    j = riya.get(f'/api/memory/context?candidate_id={C_RIYA}').get_json()
    assert ctx_notes['person'] in {n['id'] for n in j['client']['people_notes']}
    assert riya.get(f'/api/memory/context?candidate_id={C_ADMIN}').status_code == 404
    assert riya.get('/api/memory/context?client=adminclient').get_json()['client']['notes'] == []


def test_recruiter_write_scope(srv, ctx_notes):
    riya = cl(srv, RIYA)
    r = riya.post('/api/memory', json={'scope': 'mandate', 'entity_id': M_RIYA, 'content': 'Riya mandate note'})
    assert r.status_code == 201 and r.get_json()['note']['source'] == 'recruiter'
    own = r.get_json()['id']
    assert riya.post('/api/memory', json={'scope': 'candidate', 'entity_id': C_RIYA, 'content': 'ok'}).status_code == 201
    assert riya.post('/api/memory', json={'scope': 'general', 'content': 'Riya general'}).status_code == 201
    for body in ({'scope': 'mandate', 'entity_id': M_ADMIN, 'content': 'x'},
                 {'scope': 'candidate', 'entity_id': C_ADMIN, 'content': 'x'},
                 {'scope': 'client', 'entity_name': LK, 'content': 'x'},
                 {'scope': 'person', 'entity_name': 'Someone', 'client_name': LK, 'content': 'x'},
                 {'scope': 'team', 'entity_name': 'Riya', 'content': 'x'}):
        assert riya.post('/api/memory', json=body).status_code == 403, body
    # can read the client / person / team notes but not change them
    for k in ('client', 'person', 'team'):
        assert riya.get(f"/api/memory/{ctx_notes[k]}").get_json()['note']['can_edit'] is False
        assert riya.put(f"/api/memory/{ctx_notes[k]}", json={'content': 'x'}).status_code == 403
        assert riya.post(f"/api/memory/{ctx_notes[k]}/archive").status_code == 403
    # admin notes on her own mandate: editable
    assert riya.put(f"/api/memory/{ctx_notes['mand']}", json={'importance': 'high'}).status_code == 200
    # cannot move her note onto another recruiter's job
    assert riya.put(f'/api/memory/{own}', json={'entity_id': M_ADMIN}).status_code == 403
    assert riya.post(f'/api/memory/{own}/archive').status_code == 200
    assert riya.post(f'/api/memory/{own}/unarchive').status_code == 200
