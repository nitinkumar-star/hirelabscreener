"""
BD Workspace — multiple workspaces + Airtable-style custom columns (Oct 2026).

Run:   pytest test_bd_workspaces.py -q
Boots the real server.py against a throw-away DATA_DIR (nothing mocked).
"""
import os
import sys
import json
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope='module')
def srv():
    os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='bdwstest_')
    import server
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

    A, B = company('WS Agency A'), company('WS Agency B')
    ids = {'A': A, 'B': B, 'a_admin': user('wsaadmin', A, admin=1), 'a_rec': user('wsarec', A),
           'b_admin': user('wsbadmin', B, admin=1)}
    conn.commit(); conn.close()
    return server, ids


def cl_for(srv, uid):
    server, _ = srv
    cl = server.app.test_client()
    with cl.session_transaction() as s:
        s['user_id'] = uid
    return cl


def call(cl, method, url, body=None):
    return getattr(cl, method)(url, data=json.dumps(body or {}), content_type='application/json')


def names(cl, obj, ws=0):
    r = call(cl, 'post', f'/api/bd/records/{obj}/query?ws={ws}', {'per_page': 200})
    assert r.status_code == 200, r.get_json()
    return {x.get('name') or x.get('subject') for x in r.get_json()['records']}


# ── workspaces ────────────────────────────────────────────────────────────
def test_workspace_crud_and_isolation(srv):
    _, ids = srv
    adm, rec = cl_for(srv, ids['a_admin']), cl_for(srv, ids['a_rec'])
    assert call(rec, 'post', '/api/bd/workspaces', {'name': 'IT'}).status_code == 403
    r = call(adm, 'post', '/api/bd/workspaces', {'name': 'IT Staffing', 'color': '#3E63DD'})
    assert r.status_code == 200, r.get_json()
    it = r.get_json()['workspace']['id']
    assert call(adm, 'post', '/api/bd/workspaces', {'name': 'it staffing'}).status_code == 409
    assert call(adm, 'post', '/api/bd/workspaces', {'name': 'Main'}).status_code == 400

    assert call(adm, 'post', '/api/bd/records/companies?ws=0', {'name': 'Main Co'}).status_code == 200
    r = call(adm, 'post', f'/api/bd/records/companies?ws={it}', {'name': 'IT Co'})
    assert r.status_code == 200, r.get_json()
    it_co = r.get_json()['record']['id']
    assert names(adm, 'companies', 0) >= {'Main Co'} and 'IT Co' not in names(adm, 'companies', 0)
    assert names(adm, 'companies', it) == {'IT Co'}

    # people / deals follow their company's workspace
    r = call(adm, 'post', f'/api/bd/records/people?ws={it}', {'name': 'Asha', 'client_id': it_co})
    assert r.status_code == 200, r.get_json()
    assert 'Asha' in names(adm, 'people', it) and 'Asha' not in names(adm, 'people', 0)
    # ...and cannot be attached to a company of another workspace
    r = call(adm, 'post', '/api/bd/records/people?ws=0', {'name': 'Ravi', 'client_id': it_co})
    assert r.status_code == 400 and 'another BD workspace' in r.get_json()['error']

    # the listing counts companies per workspace
    ws = {w['id']: w for w in adm.get('/api/bd/workspaces').get_json()['workspaces']}
    assert ws[0]['is_main'] and ws[it]['companies'] == 1

    # another tenant cannot use (or even see) this workspace
    other = cl_for(srv, ids['b_admin'])
    assert call(other, 'post', f'/api/bd/records/companies/query?ws={it}', {}).status_code == 404
    assert it not in {w['id'] for w in call(other, 'get', '/api/bd/workspaces').get_json()['workspaces']}

    # rename, refuse delete while not empty, move the company out, then delete
    assert call(adm, 'patch', f'/api/bd/workspaces/{it}', {'name': 'IT BD'}).get_json()['workspace']['name'] == 'IT BD'
    assert call(adm, 'delete', f'/api/bd/workspaces/{it}').status_code == 409
    r = call(adm, 'post', f'/api/bd/records/companies/{it_co}/workspace', {'workspace_id': 0})
    assert r.status_code == 200 and 'IT Co' in names(adm, 'companies', 0)
    assert 'Asha' in names(adm, 'people', 0)
    assert call(adm, 'delete', f'/api/bd/workspaces/{it}').status_code == 200
    assert call(adm, 'post', f'/api/bd/records/companies/query?ws={it}', {}).status_code == 404


def test_views_are_per_workspace(srv):
    _, ids = srv
    adm = cl_for(srv, ids['a_admin'])
    w2 = call(adm, 'post', '/api/bd/workspaces', {'name': 'Non-IT'}).get_json()['workspace']['id']
    r = call(adm, 'post', f'/api/bd/views?ws={w2}', {'object': 'companies', 'name': 'Only here'})
    assert r.status_code == 200, r.get_json()
    main = {v['name'] for v in adm.get('/api/bd/views?object=companies&ws=0').get_json()['views']}
    here = {v['name'] for v in adm.get(f'/api/bd/views?object=companies&ws={w2}').get_json()['views']}
    assert 'Only here' in here and 'Only here' not in main
    assert 'Prospects' in here and 'Prospects' in main     # built-in views seeded per workspace


# ── custom fields ─────────────────────────────────────────────────────────
def test_custom_fields_end_to_end(srv):
    _, ids = srv
    adm, rec = cl_for(srv, ids['a_admin']), cl_for(srv, ids['a_rec'])
    assert call(rec, 'post', '/api/bd/fields', {'object': 'companies', 'label': 'Tier'}).status_code == 403
    assert call(adm, 'post', '/api/bd/fields', {'object': 'companies', 'label': 'Industry'}).status_code == 409
    r = call(adm, 'post', '/api/bd/fields', {'object': 'companies', 'label': 'Tier', 'type': 'select',
                                             'options': ['Gold', 'Silver', 'gold']})
    assert r.status_code == 200, r.get_json()
    tier = r.get_json()['field']
    assert [o['label'] for o in tier['options']] == ['Gold', 'Silver']
    gold, silver = tier['options'][0]['value'], tier['options'][1]['value']
    fee = call(adm, 'post', '/api/bd/fields', {'object': 'companies', 'label': 'Fee %', 'type': 'percent'}).get_json()['field']
    nda = call(adm, 'post', '/api/bd/fields', {'object': 'companies', 'label': 'NDA', 'type': 'bool'}).get_json()['field']
    note = call(adm, 'post', '/api/bd/fields', {'object': 'companies', 'label': 'Hiring for', 'type': 'text'}).get_json()['field']

    meta = adm.get('/api/bd/objects?ws=0').get_json()['objects']['companies']
    keys = {f['key']: f for f in meta['fields']}
    assert keys[tier['key']]['custom'] and keys[tier['key']]['editable']
    assert tier['key'] in [c['field'] for c in meta['default_view']['columns']]

    ids_ = {}
    for nm, t, f, n in [('Cf One', gold, 8.33, True), ('Cf Two', silver, 12, False), ('Cf Three', None, None, False)]:
        body = {'name': nm, tier['key']: t, fee['key']: f, nda['key']: n, note['key']: 'Java devs' if t else ''}
        r = call(adm, 'post', '/api/bd/records/companies', body)
        assert r.status_code == 200, r.get_json()
        ids_[nm] = r.get_json()['record']['id']
        assert r.get_json()['record'][tier['key']] == t

    # inline edit + validation
    r = call(adm, 'patch', f'/api/bd/records/companies/{ids_["Cf Three"]}', {fee['key']: '5'})
    assert r.status_code == 200 and r.get_json()['record'][fee['key']] == 5
    # validation
    assert call(adm, 'patch', f'/api/bd/records/companies/{ids_["Cf Three"]}', {tier['key']: 'nope'}).status_code == 400
    assert call(adm, 'patch', f'/api/bd/records/companies/{ids_["Cf Three"]}', {fee['key']: 'abc'}).status_code == 400
    assert call(adm, 'patch', f'/api/bd/records/companies/{ids_["Cf Three"]}', {'cf_999999': 'x'}).status_code == 400

    # filter / sort / search / calculate / group on custom columns
    def q(**spec):
        r = call(adm, 'post', '/api/bd/records/companies/query', spec)
        assert r.status_code == 200, r.get_json()
        return r.get_json()
    d = q(filter=[{'field': tier['key'], 'op': 'in', 'value': [gold]}])
    assert {x['name'] for x in d['records']} == {'Cf One'}
    d = q(filter=[{'field': fee['key'], 'op': 'gte', 'value': 6}], sort=[{'field': fee['key'], 'dir': 'desc'}])
    assert [x['name'] for x in d['records']] == ['Cf Two', 'Cf One']
    assert {x['name'] for x in q(filter=[{'field': nda['key'], 'op': 'is_true'}])['records']} == {'Cf One'}
    assert {x['name'] for x in q(q='java devs')['records']} == {'Cf One', 'Cf Two'}
    d = q(filter=[{'field': 'name', 'op': 'starts_with', 'value': 'cf '}], calc={fee['key']: 'sum'})
    assert d['calc'][fee['key']]['value'] == pytest.approx(25.33)
    g = call(adm, 'post', '/api/bd/records/companies/groups', {'group_by': tier['key']}).get_json()
    assert {x['label']: x['count'] for x in g['groups']}['Gold'] == 1

    # a kanban view grouped by the custom select saves fine
    r = call(adm, 'post', '/api/bd/views', {'object': 'companies', 'name': 'By tier',
             'config': {'view_type': 'kanban', 'kanban_field': tier['key'],
                        'filter': [{'field': fee['key'], 'op': 'is_not_empty'}], 'calc': {fee['key']: 'avg'}}})
    assert r.status_code == 200, r.get_json()
    vid = r.get_json()['view']['id']

    # rename an option keeps the values; removing one clears them
    r = call(adm, 'patch', f'/api/bd/fields/{tier["field_id"]}',
             {'label': 'Account tier', 'options': [{'value': gold, 'label': 'Platinum'}]})
    assert r.status_code == 200, r.get_json()
    rec1 = adm.get(f'/api/bd/records/companies/{ids_["Cf One"]}').get_json()['record']
    rec2 = adm.get(f'/api/bd/records/companies/{ids_["Cf Two"]}').get_json()['record']
    assert rec1[tier['key']] == gold and rec2[tier['key']] is None
    assert call(adm, 'patch', f'/api/bd/fields/{tier["field_id"]}', {'type': 'text'}).status_code == 400

    # delete: gone from meta and from the saved view, which still works
    assert call(adm, 'delete', f'/api/bd/fields/{fee["field_id"]}').status_code == 200
    assert call(adm, 'delete', f'/api/bd/fields/{tier["field_id"]}').status_code == 200
    meta = adm.get('/api/bd/objects').get_json()['objects']['companies']
    assert tier['key'] not in {f['key'] for f in meta['fields']}
    v = [x for x in adm.get('/api/bd/views?object=companies').get_json()['views'] if x['id'] == vid][0]
    assert v['config']['view_type'] == 'table' and not v['config'].get('filter') and not v['config']['calc']
    assert call(adm, 'put', f'/api/bd/views/{vid}', {'config': v['config']}).status_code == 200


def test_custom_fields_are_per_workspace_and_tenant(srv):
    _, ids = srv
    adm = cl_for(srv, ids['a_admin'])
    w = call(adm, 'post', '/api/bd/workspaces', {'name': 'Leadership hiring'}).get_json()['workspace']['id']
    f = call(adm, 'post', f'/api/bd/fields?ws={w}', {'object': 'opportunities', 'label': 'Retainer',
                                                    'type': 'currency'}).get_json()['field']
    main = {x['key'] for x in adm.get('/api/bd/objects?ws=0').get_json()['objects']['opportunities']['fields']}
    here = {x['key'] for x in adm.get(f'/api/bd/objects?ws={w}').get_json()['objects']['opportunities']['fields']}
    assert f['key'] in here and f['key'] not in main
    # a field of another workspace cannot be written from Main
    co = call(adm, 'post', '/api/bd/records/companies', {'name': 'Ws Field Co'}).get_json()['record']['id']
    r = call(adm, 'post', '/api/bd/records/opportunities', {'name': 'Deal', 'client_id': co, f['key']: 100})
    assert r.status_code == 400
    # another tenant cannot touch it
    other = cl_for(srv, ids['b_admin'])
    assert call(other, 'patch', f'/api/bd/fields/{f["field_id"]}', {'label': 'x'}).status_code == 404
    assert call(other, 'delete', f'/api/bd/fields/{f["field_id"]}').status_code == 404
