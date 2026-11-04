"""
Profile groups (modules/profile_groups.py): agencies build their own candidate
profile sections in Settings; a candidate gets one group; all of it is
searchable in Talent.

Run:   pytest test_profile_groups.py -q
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='pgroups_'))
    import server
    server.app.config['TESTING'] = True
    conn = server.get_db()
    c = conn.cursor()
    ids = {}
    for tag in ('A', 'B'):
        c.execute("INSERT INTO companies (name, status, plan, billing_status, created_at) "
                  "VALUES (?, 'active', 'standard', 'active', '2026-01-01T00:00:00')", ('PG ' + tag,))
        ids['co' + tag] = co = c.lastrowid
        for key, adm in (('boss', 1), ('rec', 0)):
            c.execute("INSERT INTO users (username, password_hash, display_name, role, status, company_id, is_company_admin, "
                      "created_at) VALUES (?, 'x', ?, 'user', 'approved', ?, ?, '2026-01-01T00:00:00')",
                      ('pg_%s_%s' % (key, tag), key, co, adm))
            ids[key + tag] = c.lastrowid
        c.execute("INSERT INTO mandates (client, role, status, owner_id, created_at) VALUES ('X','Plant Head','active',?,'2026-10-01')", (co,))
        ids['m' + tag] = c.lastrowid
    for name in ('Asha Plant', 'Bala Sales', 'Chetan Plant'):
        c.execute("INSERT INTO candidates (mandate_id, name, stage, owner_id, created_at, updated_at) "
                  "VALUES (?, ?, 'Screening', ?, '2026-10-01', '2026-10-01')", (ids['mA'], name, ids['coA']))
        ids[name.split()[0]] = c.lastrowid
    c.execute("UPDATE candidates SET yearly_target=12, product_handles='[\"Cables\"]' WHERE id=?", (ids['Bala'],))
    conn.commit(); conn.close()
    yield server, ids


def cl(srv, who):
    server, ids = srv
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = ids[who]
    return c


PRODUCTION = {'name': 'Production', 'fields': [
    {'label': 'Plant', 'type': 'text'},
    {'label': 'Capacity', 'type': 'number', 'unit': 'MW'},
    {'label': 'Processes', 'type': 'tags'},
    {'label': 'Shift', 'type': 'choice', 'options': ['General', 'Rotational']},
    {'type': 'system', 'sys': 'gender'}]}


def test_sales_group_is_there_and_old_sales_data_shows_in_it(srv):
    server, ids = srv
    r = cl(srv, 'recA').get('/api/profile-groups').get_json()
    assert [g['name'] for g in r['groups']] == ['Sales'] and r['groups'][0]['is_sales']
    assert [f['sys'] for f in r['groups'][0]['fields']] == ['gender', 'yearly_target', 'team_direct', 'team_indirect',
                                                             'product_handles', 'function_tags', 'geography_tags']
    assert len(r['system']) == 7
    sales = r['groups'][0]['id']
    assert cl(srv, 'bossA').get('/api/candidates/%d/profile' % ids['Bala']).get_json()['group_id'] == sales
    assert cl(srv, 'bossA').get('/api/candidates/%d/profile' % ids['Asha']).get_json()['group_id'] == 0
    # a recruiter only reaches candidates of jobs they work on
    assert cl(srv, 'recA').get('/api/candidates/%d/profile' % ids['Asha']).status_code in (403, 404)


def test_any_user_builds_a_group_and_fields_are_checked(srv):
    server, ids = srv
    rec = cl(srv, 'recA')
    assert rec.post('/api/profile-groups', json={'name': '', 'fields': PRODUCTION['fields']}).status_code == 400
    assert rec.post('/api/profile-groups', json={'name': 'X', 'fields': [{'label': 'Shift', 'type': 'choice', 'options': ['A']}]}).status_code == 400
    assert rec.post('/api/profile-groups', json={'name': 'X', 'fields': [{'label': 'a', 'type': 'magic'}]}).status_code == 400
    g = rec.post('/api/profile-groups', json=PRODUCTION).get_json()['group']
    assert [f['key'] for f in g['fields']] == ['plant', 'capacity', 'processes', 'shift', 'sys_gender']
    assert rec.post('/api/profile-groups', json=PRODUCTION).status_code == 400            # same name twice
    # renaming a field keeps its key (values stay); a new field with the old name gets a new key
    f = g['fields']
    f[0]['label'] = 'Plant / site'
    g2 = rec.put('/api/profile-groups/%d' % g['id'], json={'fields': f + [{'label': 'Plant', 'type': 'text'}]}).get_json()['group']
    assert [x['key'] for x in g2['fields']] == ['plant', 'capacity', 'processes', 'shift', 'sys_gender', 'plant_2']
    rec.put('/api/profile-groups/%d' % g['id'], json={'fields': f})
    # another agency neither sees nor changes it
    other = cl(srv, 'bossB')
    assert [x['name'] for x in other.get('/api/profile-groups').get_json()['groups']] == ['Sales']
    assert other.put('/api/profile-groups/%d' % g['id'], json={'name': 'Hacked'}).status_code == 404


def _gid(srv, name):
    return next(g['id'] for g in cl(srv, 'recA').get('/api/profile-groups').get_json()['groups'] if g['name'] == name)


def test_fill_a_candidate_and_search_by_it(srv):
    server, ids = srv
    gid = _gid(srv, 'Production')
    rec = cl(srv, 'bossA')
    bad = rec.put('/api/candidates/%d/profile' % ids['Asha'], json={'group_id': gid, 'values': {'capacity': 'lots'}})
    assert bad.status_code == 400 and 'number' in bad.get_json()['error']
    assert rec.put('/api/candidates/%d/profile' % ids['Asha'], json={'group_id': gid, 'values': {'shift': 'Night'}}).status_code == 400
    r = rec.put('/api/candidates/%d/profile' % ids['Asha'], json={'group_id': gid, 'values': {
        'plant': 'Halol', 'capacity': '250', 'processes': ['Extrusion', 'extrusion', 'Moulding'], 'shift': 'rotational'}}).get_json()
    assert r['values'] == {'plant': 'Halol', 'capacity': 250, 'processes': ['Extrusion', 'Moulding'], 'shift': 'Rotational'}
    rec.put('/api/candidates/%d/profile' % ids['Chetan'], json={'group_id': gid, 'values': {'plant': 'Pune', 'capacity': 80,
                                                                                          'processes': 'Assembly'}})
    assert rec.get('/api/candidates/%d/profile' % ids['Asha']).get_json()['group_id'] == gid
    # clearing a value removes it
    rec.put('/api/candidates/%d/profile' % ids['Chetan'], json={'group_id': gid, 'values': {'plant': 'Pune', 'capacity': 80, 'processes': ''}})
    assert 'processes' not in rec.get('/api/candidates/%d/profile' % ids['Chetan']).get_json()['values']

    find = lambda qs: sorted(x['id'] for x in rec.get('/api/central-db/search?' + qs).get_json()['candidates'])
    assert find('pgroup=%d' % gid) == sorted([ids['Asha'], ids['Chetan']])
    assert find('pgroup=%d&pfmin_capacity=100' % gid) == [ids['Asha']]
    assert find('pgroup=%d&pfmax_capacity=100' % gid) == [ids['Chetan']]
    assert find('pgroup=%d&pf_processes=moulding' % gid) == [ids['Asha']]
    assert find('pgroup=%d&pf_shift=Rotational' % gid) == [ids['Asha']]
    assert find('pgroup=%d&pf_plant=pune,halol' % gid) == sorted([ids['Asha'], ids['Chetan']])
    assert find('q=halol') == [ids['Asha']]                                                # keyword reads custom values
    sales = _gid(srv, 'Sales')
    assert find('pgroup=%d' % sales) == [ids['Bala']]                                     # old sales data counts as Sales
    rows = rec.get('/api/central-db/search?pgroup=%d&pfmin_capacity=100' % gid).get_json()['candidates']
    assert rows[0]['profile'] == {'group': 'Production', 'items': [['Plant / site', 'Halol'], ['Capacity', '250 MW'],
                                                                   ['Processes', 'Extrusion | Moulding'], ['Shift', 'Rotational']]}
    assert rec.get('/api/profile-groups/%d/suggest?key=processes' % gid).get_json()['values'] == ['Extrusion', 'Moulding']
    # another agency cannot read or write this candidate's profile, nor filter by the group
    other = cl(srv, 'bossB')
    assert other.get('/api/candidates/%d/profile' % ids['Asha']).status_code == 404
    assert other.get('/api/central-db/search?pgroup=%d' % gid).get_json()['candidates'] == []


def test_delete_needs_owner_or_admin(srv):
    server, ids = srv
    gid = cl(srv, 'bossA').post('/api/profile-groups', json={'name': 'IT', 'fields': [{'label': 'Stack', 'type': 'tags'}]}).get_json()['group']['id']
    assert cl(srv, 'recA').delete('/api/profile-groups/%d' % gid).status_code == 403
    assert cl(srv, 'bossA').delete('/api/profile-groups/%d' % gid).get_json()['ok']
    assert 'IT' not in [g['name'] for g in cl(srv, 'recA').get('/api/profile-groups').get_json()['groups']]
