"""
BD Workspace — Wave 1 tests (records engine, opportunities, views).

Run:   pytest tests/test_bd_records.py -q
Boots the real server.py against a throw-away DATA_DIR, so it exercises the
real migrations, services, audit and tenancy code — nothing is mocked.
"""
import os
import sys
import json
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture(scope='module')
def srv():
    os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='bdtest_')
    import server
    server.app.config['TESTING'] = True
    conn = server.get_db()
    c = conn.cursor()

    def company(name):
        c.execute("INSERT INTO companies (name, status, created_at) VALUES (?, 'active', '2026-01-01T00:00:00')", (name,))
        return c.lastrowid

    def user(uname, cid, admin=0, role='user'):
        c.execute("INSERT INTO users (username, password_hash, display_name, role, status, company_id, "
                  "is_company_admin, created_at) VALUES (?, 'x', ?, ?, 'approved', ?, ?, '2026-01-01T00:00:00')",
                  (uname, uname.title(), role, cid, admin))
        return c.lastrowid

    A = company('Agency A')
    B = company('Agency B')
    ids = {
        'A': A, 'B': B,
        'a_admin': user('aadmin', A, admin=1),
        'a_rec': user('arec', A),
        'a_mgr': user('amgr', A, admin=1),     # a second admin: per-user BD behaviour
        'b_admin': user('badmin', B, admin=1),
        'a_free': user('afree', A, role='freelancer_sourcer'),
    }
    conn.commit(); conn.close()
    return server, ids


def client_for(srv, uid):
    server, _ = srv
    cl = server.app.test_client()
    with cl.session_transaction() as s:
        s['user_id'] = uid
    return cl


def post(cl, url, body):
    return cl.post(url, data=json.dumps(body), content_type='application/json')


def patch(cl, url, body):
    return cl.patch(url, data=json.dumps(body), content_type='application/json')


def q(cl, obj, **spec):
    r = post(cl, f'/api/bd/records/{obj}/query', spec)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


# ── schema ────────────────────────────────────────────────────────────────
def test_migration_is_additive(srv):
    server, _ = srv
    conn = server.get_db()
    cols = {r['name'] for r in conn.execute('PRAGMA table_info(crm_clients)')}
    assert {'domain', 'linkedin', 'employees', 'annual_revenue', 'name', 'status', 'gstin'} <= cols
    acols = {r['name'] for r in conn.execute('PRAGMA table_info(crm_activities)')}
    assert 'opportunity_id' in acols
    tables = {r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'bd_opportunities', 'bd_views'} <= tables
    conn.close()


def test_migration_is_idempotent(srv):
    server, _ = srv
    import modules
    conn = server.get_db()
    modules.run_migrations(conn)      # second boot must be a clean no-op
    modules.run_migrations(conn)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) n FROM pragma_table_info('crm_clients') WHERE name='domain'").fetchone()['n']
    conn.close()
    assert n == 1


def test_auth_and_guards(srv):
    server, ids = srv
    anon = server.app.test_client()
    assert anon.get('/api/bd/objects').status_code == 401
    fl = client_for(srv, ids['a_free'])
    assert fl.get('/api/bd/records/companies').status_code == 403


def test_objects_meta(srv):
    _, ids = srv
    cl = client_for(srv, ids['a_admin'])
    d = cl.get('/api/bd/objects').get_json()
    assert set(d['objects']) == {'companies', 'people', 'opportunities', 'tasks', 'notes'}
    assert [s['value'] for s in d['stages']][:2] == ['lead', 'contacted']
    assert any(m['id'] == ids['a_rec'] for m in d['members'])
    assert not any(m['id'] == ids['b_admin'] for m in d['members'])


# ── companies ─────────────────────────────────────────────────────────────
def test_company_create_and_extra_fields(srv):
    _, ids = srv
    cl = client_for(srv, ids['a_admin'])
    r = post(cl, '/api/bd/records/companies', {
        'name': 'Insight Cosmetics', 'domain': 'https://www.InsightCosmetics.in/about',
        'employees': 250, 'annual_revenue': '12,50,00,000', 'city': 'Mumbai'})
    assert r.status_code == 200, r.get_json()
    rec = r.get_json()['record']
    assert rec['domain'] == 'insightcosmetics.in'
    assert rec['employees'] == 250 and rec['annual_revenue'] == 125000000
    assert rec['status'] == 'prospect'          # BD-created company starts as prospect
    assert rec['created_by_label'] == 'Aadmin'


def test_company_create_admin_only_and_dedup(srv):
    _, ids = srv
    rec = client_for(srv, ids['a_rec'])
    assert post(rec, '/api/bd/records/companies', {'name': 'X Corp'}).status_code == 403
    adm = client_for(srv, ids['a_admin'])
    r = post(adm, '/api/bd/records/companies', {'name': 'insight cosmetics pvt ltd'})
    assert r.status_code == 409 and r.get_json().get('existing_id')


def test_inline_edit_validates_before_writing(srv):
    _, ids = srv
    cl = client_for(srv, ids['a_admin'])
    cid = q(cl, 'companies', q='insight')['records'][0]['id']
    r = patch(cl, f'/api/bd/records/companies/{cid}', {'city': 'Pune', 'domain': 'not a domain'})
    assert r.status_code == 400
    assert q(cl, 'companies', q='insight')['records'][0]['city'] == 'Mumbai'   # nothing half-saved
    r = patch(cl, f'/api/bd/records/companies/{cid}', {'city': 'Pune', 'owner_user_id': ids['a_rec']})
    assert r.status_code == 200
    assert r.get_json()['record']['owner_user_id_label'] == 'Arec'
    assert patch(cl, f'/api/bd/records/companies/{cid}', {'owner_user_id': ids['b_admin']}).status_code == 400
    assert patch(cl, f'/api/bd/records/companies/{cid}', {'people_count': 5}).status_code == 400


# ── tenancy ───────────────────────────────────────────────────────────────
def test_tenant_isolation(srv):
    _, ids = srv
    a = client_for(srv, ids['a_admin'])
    b = client_for(srv, ids['b_admin'])
    cid = q(a, 'companies', q='insight')['records'][0]['id']
    assert q(b, 'companies')['total'] == 0
    assert b.get(f'/api/bd/records/companies/{cid}').status_code == 404
    assert patch(b, f'/api/bd/records/companies/{cid}', {'city': 'Hacked'}).status_code == 404
    assert post(b, '/api/bd/records/opportunities', {'client_id': cid}).status_code == 404
    assert post(b, '/api/bd/records/people', {'client_id': cid, 'name': 'X'}).status_code == 404


# ── people ────────────────────────────────────────────────────────────────
def test_people_create_move_and_relation_label(srv):
    _, ids = srv
    cl = client_for(srv, ids['a_admin'])
    c1 = q(cl, 'companies', q='insight')['records'][0]['id']
    c2 = post(cl, '/api/bd/records/companies', {'name': 'Resolven'}).get_json()['record']['id']
    r = post(cl, '/api/bd/records/people', {'client_id': c1, 'name': 'Komal Bhanushali',
                                            'email': 'komal@insight.in', 'designation': 'HR Head'})
    assert r.status_code == 200, r.get_json()
    p = r.get_json()['record']
    assert p['client_id_label'] == 'Insight Cosmetics'
    r = patch(cl, f'/api/bd/records/people/{p["id"]}', {'client_id': c2})
    assert r.get_json()['record']['client_id_label'] == 'Resolven'
    patch(cl, f'/api/bd/records/people/{p["id"]}', {'client_id': c1})
    assert q(cl, 'companies', q='insight')['records'][0]['people_count'] == 1


# ── opportunities ─────────────────────────────────────────────────────────
def test_opportunity_lifecycle_and_won_activates_client(srv):
    server, ids = srv
    cl = client_for(srv, ids['a_admin'])
    cid = q(cl, 'companies', q='insight')['records'][0]['id']
    pid = q(cl, 'people', q='komal')['records'][0]['id']
    r = post(cl, '/api/bd/records/opportunities', {'client_id': cid, 'contact_id': pid, 'amount': 500000})
    assert r.status_code == 200, r.get_json()
    o = r.get_json()['record']
    assert o['name'] == 'Insight Cosmetics deal' and o['stage'] == 'lead' and o['probability'] == 10
    assert o['owner_user_id'] == ids['a_admin'] and o['contact_id_label'] == 'Komal Bhanushali'

    o = patch(cl, f'/api/bd/records/opportunities/{o["id"]}', {'stage': 'proposal_sent'}).get_json()['record']
    assert o['probability'] == 60 and o['stage_changed_at']
    o = patch(cl, f'/api/bd/records/opportunities/{o["id"]}', {'stage': 'meeting_done', 'probability': 55}).get_json()['record']
    assert o['probability'] == 55            # explicit probability wins over stage default

    assert q(cl, 'companies', q='insight')['records'][0]['status'] == 'prospect'
    o = patch(cl, f'/api/bd/records/opportunities/{o["id"]}', {'stage': 'won'}).get_json()['record']
    assert o['won_at'] and o['probability'] == 100
    assert q(cl, 'companies', q='insight')['records'][0]['status'] == 'active'

    conn = server.get_db()
    acts = [r['action'] for r in conn.execute("SELECT action FROM activity_log WHERE action LIKE 'opportunity.%'")]
    conn.close()
    assert 'opportunity.created' in acts and 'opportunity.stage_changed' in acts


def test_opportunity_validation(srv):
    _, ids = srv
    cl = client_for(srv, ids['a_admin'])
    cid = q(cl, 'companies', q='insight')['records'][0]['id']
    other = q(cl, 'companies', q='resolven')['records'][0]['id']
    pid = q(cl, 'people', q='komal')['records'][0]['id']
    assert post(cl, '/api/bd/records/opportunities', {}).status_code == 400
    assert post(cl, '/api/bd/records/opportunities', {'client_id': cid, 'stage': 'bogus'}).status_code == 400
    assert post(cl, '/api/bd/records/opportunities', {'client_id': cid, 'amount': -5}).status_code == 400
    assert post(cl, '/api/bd/records/opportunities', {'client_id': cid, 'close_date': '2026-02-30'}).status_code == 400
    assert post(cl, '/api/bd/records/opportunities', {'client_id': other, 'contact_id': pid}).status_code == 400
    # moving a deal to another company drops the now-invalid point of contact
    o = post(cl, '/api/bd/records/opportunities', {'client_id': cid, 'contact_id': pid, 'name': 'Move me'}).get_json()['record']
    o = patch(cl, f'/api/bd/records/opportunities/{o["id"]}', {'client_id': other}).get_json()['record']
    assert o['contact_id'] == 0
    assert cl.delete(f'/api/bd/records/opportunities/{o["id"]}').status_code == 200
    assert cl.get(f'/api/bd/records/opportunities/{o["id"]}').status_code == 404


# ── filters / sort / calculate / groups ───────────────────────────────────
def test_filters_sort_calc(srv):
    _, ids = srv
    cl = client_for(srv, ids['a_admin'])
    cid = q(cl, 'companies', q='resolven')['records'][0]['id']
    for n, amt, st in (('Big', 900000, 'lead'), ('Small', 100000, 'contacted'), ('NoAmt', None, 'lead')):
        post(cl, '/api/bd/records/opportunities', {'client_id': cid, 'name': n, 'amount': amt, 'stage': st})

    d = q(cl, 'opportunities', filter={'logic': 'and', 'conditions': [
        {'field': 'stage', 'op': 'in', 'value': ['lead', 'contacted']},
        {'field': 'amount', 'op': 'gte', 'value': 100000}]},
        sort=[{'field': 'amount', 'dir': 'desc'}],
        calc={'amount': 'sum', 'close_date': 'pct_empty'})
    assert [r['name'] for r in d['records']] == ['Big', 'Small']
    assert d['calc']['amount']['value'] == 1000000
    assert d['calc']['close_date']['value'] == 100.0

    d = q(cl, 'opportunities', filter={'logic': 'or', 'conditions': [
        {'field': 'name', 'op': 'contains', 'value': 'big'},
        {'field': 'amount', 'op': 'is_empty'}]})
    assert {r['name'] for r in d['records']} == {'Big', 'NoAmt'}

    # empties sort last in both directions
    d = q(cl, 'opportunities', filter=[{'field': 'client_id', 'op': 'in', 'value': [cid]}],
          sort=[{'field': 'amount', 'dir': 'asc'}])
    assert d['records'][-1]['name'] == 'NoAmt'

    d = q(cl, 'opportunities', filter=[{'field': 'owner_user_id', 'op': 'is_me'}], per_page=1)
    assert d['per_page'] == 1 and d['total'] >= 3 and len(d['records']) == 1


def test_filter_rejects_bad_input(srv):
    _, ids = srv
    cl = client_for(srv, ids['a_admin'])
    bad = [
        {'filter': [{'field': 'name); DROP TABLE crm_clients;--', 'op': 'eq', 'value': 1}]},
        {'filter': [{'field': 'amount', 'op': 'contains', 'value': 'x'}]},
        {'filter': [{'field': 'amount', 'op': 'gt', 'value': 'abc'}]},
        {'sort': [{'field': 'nope'}]},
        {'calc': {'name': 'sum'}},
    ]
    for spec in bad:
        r = post(cl, '/api/bd/records/opportunities/query', spec)
        assert r.status_code == 400, spec
    assert post(cl, '/api/bd/records/unicorns/query', {}).status_code == 404
    # LIKE wildcards are literal
    assert q(cl, 'companies', filter=[{'field': 'name', 'op': 'contains', 'value': '%'}])['total'] == 0


def test_groups_for_kanban(srv):
    _, ids = srv
    cl = client_for(srv, ids['a_admin'])
    d = post(cl, '/api/bd/records/opportunities/groups', {'group_by': 'stage', 'sum_field': 'amount'}).get_json()
    vals = [g['value'] for g in d['groups']]
    assert vals[:6] == ['lead', 'contacted', 'meeting_done', 'proposal_sent', 'won', 'lost']
    lead = next(g for g in d['groups'] if g['value'] == 'lead')
    assert lead['count'] >= 2 and lead['sum'] >= 900000
    assert post(cl, '/api/bd/records/opportunities/groups', {'group_by': 'amount'}).status_code == 400


# ── tasks / notes ─────────────────────────────────────────────────────────
def test_tasks_and_notes(srv):
    _, ids = srv
    cl = client_for(srv, ids['a_admin'])
    opp = q(cl, 'opportunities', q='big')['records'][0]
    r = post(cl, '/api/bd/records/tasks', {'opportunity_id': opp['id'], 'subject': 'Send terms',
                                           'due_at': '2020-01-01T10:00:00', 'owner_user_id': ids['a_rec']})
    assert r.status_code == 200, r.get_json()
    t = r.get_json()['record']
    assert t['client_id'] == opp['client_id']            # company implied by the deal
    assert t['opportunity_id_label'] == 'Big' and t['is_overdue'] is True
    t = patch(cl, f'/api/bd/records/tasks/{t["id"]}', {'status': 'done'}).get_json()['record']
    assert t['status'] == 'done' and t['completed_at'] and t['is_overdue'] is False

    n = post(cl, '/api/bd/records/notes', {'client_id': opp['client_id'], 'subject': 'Call recap',
                                          'body': 'Budget approved'}).get_json()['record']
    assert n['subject'] == 'Call recap'
    assert post(cl, '/api/bd/records/notes', {'client_id': opp['client_id'], 'activity_type': 'task'}).status_code == 400
    # a note id is not reachable through /tasks
    assert cl.get(f'/api/bd/records/tasks/{n["id"]}').status_code == 404
    other = q(cl, 'companies', q='insight')['records'][0]['id']
    assert patch(cl, f'/api/bd/records/notes/{n["id"]}', {'client_id': other}).status_code == 200
    assert q(cl, 'notes', q='recap')['records'][0]['client_id'] == other


# ── views ─────────────────────────────────────────────────────────────────
def test_views(srv):
    _, ids = srv
    adm = client_for(srv, ids['a_admin'])
    mgr = client_for(srv, ids['a_mgr'])
    rec = client_for(srv, ids['a_rec'])
    cfg = {'columns': ['name', {'field': 'amount', 'width': 140}],
           'filter': [{'field': 'stage', 'op': 'in', 'value': ['lead']}],
           'sort': [{'field': 'amount', 'dir': 'desc'}], 'calc': {'amount': 'sum'}}
    r = post(mgr, '/api/bd/views', {'object': 'opportunities', 'name': 'My leads', 'config': cfg})
    assert r.status_code == 200, r.get_json()
    mine = r.get_json()['view']
    assert mine['config']['columns'][1]['width'] == 140
    # recruiter sub-accounts have no BD workspace at all
    assert post(rec, '/api/bd/views', {'object': 'opportunities', 'name': 'T'}).status_code == 403
    assert rec.get('/api/bd/views?object=opportunities').status_code == 403
    team = post(adm, '/api/bd/views', {'object': 'opportunities', 'name': 'Pipeline', 'visibility': 'team',
                                       'config': {'view_type': 'kanban'}}).get_json()['view']
    assert team['config']['kanban_field'] == 'stage'
    assert post(adm, '/api/bd/views', {'object': 'opportunities', 'name': 'x',
                                       'config': {'columns': ['bogus']}}).status_code == 400
    private = post(adm, '/api/bd/views', {'object': 'opportunities', 'name': 'Admin only'}).get_json()['view']

    names = [v['name'] for v in mgr.get('/api/bd/views?object=opportunities').get_json()['views']]
    assert 'My leads' in names and 'Pipeline' in names and 'Admin only' not in names
    names_admin = [v['name'] for v in adm.get('/api/bd/views?object=opportunities').get_json()['views']]
    assert 'My leads' not in names_admin               # private stays private
    assert patch(mgr, f'/api/bd/views/{private["id"]}', {'name': 'hijack'}).status_code == 404
    assert mgr.delete(f'/api/bd/views/{mine["id"]}').status_code == 200
    b = client_for(srv, ids['b_admin'])
    assert b.delete(f'/api/bd/views/{team["id"]}').status_code == 404

def test_legacy_endpoints_still_work(srv):
    _, ids = srv
    cl = client_for(srv, ids['a_admin'])
    assert cl.get('/api/bd/command-center').status_code == 200
    assert cl.get('/api/crm/clients').status_code == 200
    assert cl.get('/api/bd/meta').status_code == 200


# ── Wave 3: record panel timeline + calendar ──────────────────────────────
def test_timeline_scoping(srv):
    _, ids = srv
    cl = client_for(srv, ids['a_admin'])
    cid = post(cl, '/api/bd/records/companies', {'name': 'Timeline Co'}).get_json()['record']['id']
    p1 = post(cl, '/api/bd/records/people', {'client_id': cid, 'name': 'Tina One'}).get_json()['record']['id']
    p2 = post(cl, '/api/bd/records/people', {'client_id': cid, 'name': 'Tom Two'}).get_json()['record']['id']
    opp = post(cl, '/api/bd/records/opportunities', {'client_id': cid, 'contact_id': p1, 'name': 'TL deal'}).get_json()['record']['id']
    patch(cl, f'/api/bd/records/opportunities/{opp}', {'stage': 'contacted'})
    post(cl, '/api/bd/records/notes', {'client_id': cid, 'contact_id': p2, 'subject': 'Tom note'})
    post(cl, '/api/bd/records/tasks', {'opportunity_id': opp, 'subject': 'Deal task'})

    co = cl.get(f'/api/bd/records/companies/{cid}/timeline').get_json()['items']
    acts = [i['action'] for i in co]
    assert 'client.created' in acts and 'opportunity.stage_changed' in acts and 'contact.created' in acts

    tl = cl.get(f'/api/bd/records/opportunities/{opp}/timeline').get_json()['items']
    assert {i['action'] for i in tl} >= {'opportunity.created', 'opportunity.stage_changed', 'bd.task'}
    assert not any('Tom note' in i['detail'] for i in tl)
    sc = next(i for i in tl if i['action'] == 'opportunity.stage_changed')
    assert sc['from'] == 'lead' and sc['to'] == 'contacted'
    assert sc['actor'] == 'Aadmin'          # display name, not the login username

    tom = cl.get(f'/api/bd/records/people/{p2}/timeline').get_json()['items']
    assert any('Tom note' in i['detail'] for i in tom)
    assert not any(i['action'].startswith('opportunity.') for i in tom)

    b = client_for(srv, ids['b_admin'])
    assert b.get(f'/api/bd/records/companies/{cid}/timeline').status_code == 404


def test_calendar(srv):
    server, ids = srv
    cl = client_for(srv, ids['a_admin'])
    cid = q(cl, 'companies', q='timeline co')['records'][0]['id']
    pid = q(cl, 'people', q='tina')['records'][0]['id']
    post(cl, '/api/bd/records/tasks', {'client_id': cid, 'contact_id': pid, 'activity_type': 'meeting',
                                       'subject': 'Kickoff', 'due_at': '2099-01-01T10:00:00'})
    post(cl, '/api/bd/records/tasks', {'client_id': cid, 'activity_type': 'call',
                                       'subject': 'Old call', 'due_at': '2020-01-01T10:00:00'})
    conn = server.get_db()
    conn.execute("INSERT INTO meetings (company_id, host_user_id, crm_client_id, crm_contact_id, guest_name, "
                 "purpose, start_at, status) VALUES (?,?,?,?,?,?,?,?)",
                 (ids['A'], ids['a_admin'], cid, pid, 'Tina', 'Intro call', '2099-02-01T11:00', 'confirmed'))
    conn.commit(); conn.close()
    d = cl.get(f'/api/bd/records/companies/{cid}/calendar').get_json()
    assert [i['title'] for i in d['upcoming']] == ['Kickoff', 'Intro call']
    assert [i['title'] for i in d['past']] == ['Old call']
    p = cl.get(f'/api/bd/records/people/{pid}/calendar').get_json()
    assert {i['title'] for i in p['upcoming']} == {'Kickoff', 'Intro call'} and not p['past']
    assert cl.get(f'/api/bd/records/notes/1/calendar').status_code in (400, 404)
    b = client_for(srv, ids['b_admin'])
    assert b.get(f'/api/bd/records/companies/{cid}/calendar').status_code == 404


# ── Wave 4: built-in views, personal default, deal -> job, new operator ───
def test_builtin_views_seeded_once(srv):
    _, ids = srv
    adm = client_for(srv, ids['a_admin'])
    rec = client_for(srv, ids['a_rec'])
    mgr = client_for(srv, ids['a_mgr'])
    v1 = mgr.get('/api/bd/views?object=tasks').get_json()
    names = [v['name'] for v in v1['views']]
    assert names[:3] == ['My open tasks', 'Overdue', 'Due this week']
    assert all(v['is_system'] and v['visibility'] == 'team' for v in v1['views'][:3])
    # admin deletes one; it must not come back on the next load
    od = next(v for v in v1['views'] if v['name'] == 'Overdue')
    assert rec.delete(f'/api/bd/views/{od["id"]}').status_code == 403
    assert adm.delete(f'/api/bd/views/{od["id"]}').status_code == 200
    names2 = [v['name'] for v in adm.get('/api/bd/views?object=tasks').get_json()['views']]
    assert 'Overdue' not in names2 and names2.count('My open tasks') == 1
    # other tenant gets its own copy
    b = client_for(srv, ids['b_admin'])
    assert [v['name'] for v in b.get('/api/bd/views?object=tasks').get_json()['views']][:1] == ['My open tasks']
    # every built-in config actually runs
    for obj in ('opportunities', 'tasks', 'companies', 'people', 'notes'):
        for v in adm.get(f'/api/bd/views?object={obj}').get_json()['views']:
            if not v['is_system']:
                continue
            cfg = v['config']
            if cfg.get('view_type') == 'kanban':
                r = post(adm, f'/api/bd/records/{obj}/groups', {'group_by': cfg['kanban_field'], 'filter': cfg.get('filter')})
            else:
                r = post(adm, f'/api/bd/records/{obj}/query', {'filter': cfg.get('filter'), 'sort': cfg.get('sort'), 'calc': cfg.get('calc')})
            assert r.status_code == 200, (obj, v['name'], r.get_json())


def test_personal_default_view(srv):
    _, ids = srv
    rec = client_for(srv, ids['a_mgr'])     # per-user: a second admin
    adm = client_for(srv, ids['a_admin'])
    vs = rec.get('/api/bd/views?object=opportunities').get_json()
    assert vs['default_view_id'] == 0
    pipe = next(v for v in vs['views'] if v['name'] == 'Pipeline' and v['is_system'])
    assert post(rec, '/api/bd/views/default', {'object': 'opportunities', 'view_id': pipe['id']}).status_code == 200
    assert rec.get('/api/bd/views?object=opportunities').get_json()['default_view_id'] == pipe['id']
    assert adm.get('/api/bd/views?object=opportunities').get_json()['default_view_id'] != pipe['id']  # per user
    assert post(rec, '/api/bd/views/default', {'object': 'tasks', 'view_id': pipe['id']}).status_code == 404
    b = client_for(srv, ids['b_admin'])
    assert post(b, '/api/bd/views/default', {'object': 'opportunities', 'view_id': pipe['id']}).status_code == 404
    assert post(rec, '/api/bd/views/default', {'object': 'opportunities', 'view_id': 0}).status_code == 200
    assert rec.get('/api/bd/views?object=opportunities').get_json()['default_view_id'] == 0


def test_not_in_last_days_operator(srv):
    server, ids = srv
    cl = client_for(srv, ids['a_admin'])
    fresh = post(cl, '/api/bd/records/companies', {'name': 'Fresh Activity Co'}).get_json()['record']['id']
    quiet = post(cl, '/api/bd/records/companies', {'name': 'Silent Co'}).get_json()['record']['id']
    post(cl, '/api/bd/records/notes', {'client_id': fresh, 'subject': 'just talked'})
    old = post(cl, '/api/bd/records/companies', {'name': 'Old Talk Co'}).get_json()['record']['id']
    conn = server.get_db()
    conn.execute("INSERT INTO crm_activities (company_id, client_id, activity_type, subject, status, is_active, created_at) "
                 "VALUES (?,?,?,?,?,1,?)", (ids['A'], old, 'note', 'ancient', 'done', '2020-01-01T10:00:00'))
    conn.commit(); conn.close()
    d = q(cl, 'companies', q=' co', filter=[{'field': 'last_activity', 'op': 'not_in_last_days', 'value': 30}])
    names = {r['name'] for r in d['records']}
    assert 'Silent Co' in names and 'Old Talk Co' in names and 'Fresh Activity Co' not in names


def test_link_mandate(srv):
    server, ids = srv
    cl = client_for(srv, ids['a_admin'])
    cid = q(cl, 'companies', q='resolven')['records'][0]['id']
    other = q(cl, 'companies', q='silent co')['records'][0]['id']
    opp = post(cl, '/api/bd/records/opportunities', {'client_id': cid, 'name': 'Job deal', 'stage': 'won'}).get_json()['record']
    conn = server.get_db()
    c = conn.cursor()
    c.execute("INSERT INTO mandates (client, role, owner_id, crm_client_id, status, created_at) VALUES (?,?,?,?,?,?)",
              ('Resolven', 'Site Engineer', ids['A'], cid, 'active', '2026-09-01T00:00:00'))
    good = c.lastrowid
    c.execute("INSERT INTO mandates (client, role, owner_id, crm_client_id, status, created_at) VALUES (?,?,?,?,?,?)",
              ('Silent', 'Wrong co', ids['A'], other, 'active', '2026-09-01T00:00:00'))
    wrong = c.lastrowid
    c.execute("INSERT INTO mandates (client, role, owner_id, crm_client_id, status, created_at) VALUES (?,?,?,?,?,?)",
              ('X', 'Other tenant', ids['B'], cid, 'active', '2026-09-01T00:00:00'))
    foreign = c.lastrowid
    conn.commit(); conn.close()
    u = f'/api/bd/records/opportunities/{opp["id"]}/link-mandate'
    assert post(cl, u, {'mandate_id': wrong}).status_code == 400
    assert post(cl, u, {'mandate_id': foreign}).status_code == 404
    r = post(cl, u, {'mandate_id': good})
    assert r.status_code == 200 and r.get_json()['record']['mandate_id'] == good
    assert r.get_json()['record']['mandate_id_label'] == 'Site Engineer'
    b = client_for(srv, ids['b_admin'])
    assert post(b, u, {'mandate_id': foreign}).status_code == 404


# ── Wave 5: BD Home dashboard ─────────────────────────────────────────────
def test_home_dashboard(srv):
    server, ids = srv
    import datetime as _dt
    adm = client_for(srv, ids['a_admin'])
    rec = client_for(srv, ids['a_mgr'])
    today = _dt.date.fromisoformat(server.ts()[:10])
    d = lambda n: (today + _dt.timedelta(days=n)).isoformat()
    cid = post(adm, '/api/bd/records/companies', {'name': 'Home Dash Co', 'status': 'active'}).get_json()['record']['id']
    # open deals across stages, one owned by the recruiter
    o1 = post(adm, '/api/bd/records/opportunities', {'client_id': cid, 'name': 'HD lead', 'amount': 100000, 'close_date': d(-3)}).get_json()['record']
    o2 = post(adm, '/api/bd/records/opportunities', {'client_id': cid, 'name': 'HD proposal', 'stage': 'proposal_sent',
                                                      'amount': 300000, 'close_date': d(5), 'owner_user_id': ids['a_mgr']}).get_json()['record']
    post(adm, '/api/bd/records/opportunities', {'client_id': cid, 'name': 'HD won', 'stage': 'won', 'amount': 250000})
    post(adm, '/api/bd/records/opportunities', {'client_id': cid, 'name': 'HD lost', 'stage': 'lost', 'amount': 50000})
    # tasks: overdue (mine=admin), today (later today), meeting in 3 days
    post(adm, '/api/bd/records/tasks', {'client_id': cid, 'subject': 'HD overdue', 'due_at': d(-1) + 'T10:00:00'})
    post(adm, '/api/bd/records/tasks', {'client_id': cid, 'subject': 'HD today', 'due_at': d(0) + 'T23:59:00'})
    post(adm, '/api/bd/records/tasks', {'opportunity_id': o2['id'], 'subject': 'HD meeting', 'activity_type': 'meeting',
                                        'due_at': d(3) + 'T11:00:00', 'owner_user_id': ids['a_mgr']})
    conn = server.get_db()
    conn.execute("INSERT INTO mandates (client, role, owner_id, crm_client_id, status, assigned_user_id, created_at) "
                 "VALUES (?,?,?,?,?,?,?)", ('Home Dash Co', 'HD Engineer', ids['A'], cid, 'active', ids['a_mgr'], '2026-09-01T00:00:00'))
    conn.execute("INSERT INTO mandates (client, role, owner_id, crm_client_id, status, created_at) "
                 "VALUES (?,?,?,?,?,?)", ('Home Dash Co', 'HD Closed', ids['A'], cid, 'closed', '2026-09-01T00:00:00'))
    conn.commit(); conn.close()

    h = adm.get('/api/bd/home').get_json()
    assert h['ok']
    stages = {f['stage']: f for f in h['funnel']}
    assert list(stages) == ['lead', 'contacted', 'meeting_done', 'proposal_sent']
    assert stages['proposal_sent']['amount'] >= 300000
    assert h['pipeline']['count'] == sum(f['count'] for f in h['funnel'])
    assert h['wins']['this_month']['amount'] >= 250000 and len(h['wins']['trend']) == 6
    assert h['wins']['win_rate_90d'] is not None
    assert any(t['subject'] == 'HD overdue' for t in h['overdue'])
    assert any(t['subject'] == 'HD today' for t in h['today'])
    assert not any(t['subject'] == 'HD today' for t in h['overdue'])
    assert any(a['title'] == 'HD meeting' and a['opportunity'] == 'HD proposal' for a in h['agenda'])
    att = {a['name']: a['reason_kind'] for a in h['attention']}
    assert att.get('HD lead') == 'slipped' and att.get('HD proposal') == 'soon'
    grp = next(g for g in h['requirements'] if g['client_id'] == cid)
    assert [j['role'] for j in grp['jobs']] == ['HD Engineer']          # closed job excluded
    assert any(w['name'] == 'HD won' for w in h['recent_wins'])

    # "Mine" narrows to the recruiter's own deals, tasks and jobs
    m = rec.get('/api/bd/home?scope=me').get_json()
    assert m['scope'] == 'me'
    assert {a['name'] for a in m['attention']} == {'HD proposal'}
    assert not any(t['subject'] in ('HD overdue', 'HD today') for t in m['overdue'] + m['today'])
    assert any(a['title'] == 'HD meeting' for a in m['agenda'])
    assert [g['client_name'] for g in m['requirements']] == ['Home Dash Co']
    assert m['pipeline']['amount'] == 300000

    # another tenant sees none of it
    b = client_for(srv, ids['b_admin'])
    hb = b.get('/api/bd/home').get_json()
    assert not any(a['name'].startswith('HD ') for a in hb['attention'])
    assert not any(g['client_name'] == 'Home Dash Co' for g in hb['requirements'])
    assert client_for(srv, ids['a_rec']).get('/api/bd/home').status_code == 403   # recruiters: no BD
    fl = client_for(srv, ids['a_free'])
    assert fl.get('/api/bd/home').status_code == 403


def test_legacy_command_center_uses_ist_clock(srv):
    server, ids = srv
    import modules.bd as bd
    with server.app.test_request_context():
        assert bd._now().isoformat()[:16] == server.ts()[:16]
