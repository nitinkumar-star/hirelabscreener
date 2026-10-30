"""
Product handling, geography handling, yearly target + Company Talent Map (Oct 2026).

Run:   pytest test_sales_profile.py -q
"""
import os
import sys
import json
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope='module')
def srv():
    os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='salesp_')
    import server
    server.app.config['TESTING'] = True
    conn = server.get_db()
    c = conn.cursor()
    ids = {}
    for tag in ('A', 'B'):
        c.execute("INSERT INTO companies (name, status, created_at) VALUES (?, 'active', '2026-01-01T00:00:00')", ('SP ' + tag,))
        ids[tag] = c.lastrowid
        c.execute("INSERT INTO users (username, password_hash, display_name, role, status, company_id, is_company_admin, "
                  "created_at) VALUES (?, 'x', ?, 'user', 'approved', ?, 1, '2026-01-01T00:00:00')", ('sp' + tag, 'SP ' + tag, ids[tag]))
        ids['u' + tag] = c.lastrowid
        c.execute("INSERT INTO mandates (client, role, status, owner_id, created_at) VALUES ('X','Sales','active',?,'2026-10-01')", (ids[tag],))
        ids['m' + tag] = c.lastrowid
    conn.commit(); conn.close()
    yield server, ids


def cl(srv, who='A'):
    c = srv[0].app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = srv[1]['u' + who]
    return c


def push(c, mid, name, phone, company, **extra):
    body = {'mandate_id': mid, 'name': name, 'phone': phone, 'email': phone + '@x.com', 'company': company}
    body.update(extra)
    r = c.post('/api/extension/push', json=body)
    assert r.status_code == 200, r.get_json()
    return r.get_json()['candidate_id']


def test_fields_saved_searched_and_exported(srv):
    server, ids = srv
    c = cl(srv)
    a = push(c, ids['mA'], 'Vipin Tomar', '9000000001', 'KEI Wires & Cables Ltd', designation='Assistant Manager',
             product_handling=['Cables', 'LV Switchgear'], geography='North India, Uttarakhand', yearly_target_cr='12.5')
    d = c.get(f'/api/candidates/{a}').get_json()
    assert json.loads(d['product_handles']) == ['Cables', 'LV Switchgear']
    assert json.loads(d['geography_tags']) == ['North India', 'Uttarakhand'] and d['yearly_target'] == 12.5
    # a blank re-push keeps what is known
    push(c, ids['mA'], 'Vipin Tomar', '9000000001', 'KEI Wires & Cables Ltd')
    d = c.get(f'/api/candidates/{a}').get_json()
    assert d['yearly_target'] == 12.5 and 'Cables' in d['product_handles']
    # profile edit + geography tag box
    assert c.put(f'/api/candidates/{a}', json={'yearly_target': '15'}).status_code == 200
    assert c.post(f'/api/candidates/{a}/tags', json={'tag_type': 'geography', 'tags': ['North India', 'Delhi NCR']}).get_json()['ok']
    assert 'Uttar Pradesh' in c.get('/api/tags/geography').get_json()['tags']

    b = push(c, ids['mA'], 'Neeraj Kumar', '9000000002', 'kei wires and cables', product_handling=['Wires'],
             geography=['South India'], yearly_target_cr=4)
    ids_of = lambda q: [x['id'] for x in c.get('/api/central-db/search?' + q).get_json()['candidates']]
    assert ids_of('product=cables') == [a] and ids_of('geography=delhi') == [a]
    assert ids_of('target_min=10') == [a] and ids_of('target_max=5') == [b]
    assert ids_of('q=uttarakhand') == [] and a in ids_of('q=%22delhi%20ncr%22')     # geography is in keyword search
    assert ids_of('sort=target&dir=desc')[:2] == [a, b]
    csv = c.get('/api/central-db/export').get_data(as_text=True)
    assert 'Yearly target (Rs Cr)' in csv.splitlines()[0] and 'Delhi NCR' in csv


def test_talent_map(srv):
    server, ids = srv
    c = cl(srv)
    comp = c.get('/api/talent-map/companies?q=kei').get_json()['companies']
    assert len(comp) == 1 and comp[0]['count'] == 2                      # both spellings, one company
    d = c.get('/api/talent-map/company?former=1&name=KEI Wires and Cables').get_json()
    assert {p['name'] for p in d['people']} == {'Vipin Tomar', 'Neeraj Kumar'}
    vip = [p for p in d['people'] if p['name'] == 'Vipin Tomar'][0]
    assert vip['yearly_target'] == 15 and vip['geography'] == ['North India', 'Delhi NCR'] and vip['products'] == ['Cables', 'LV Switchgear']
    # a former employee (work history)
    x = push(c, ids['mA'], 'Mukesh Rao', '9000000003', 'Havells',
             work_history=[{'company': 'Havells', 'designation': 'ASM', 'is_current': 1},
                           {'company': 'KEI Wires & Cables', 'designation': 'Sales Executive'}])
    d = c.get('/api/talent-map/company?former=1&name=KEI Wires and Cables').get_json()
    assert [p['name'] for p in d['former']] == ['Mukesh Rao'] and d['former'][0]['designation'] == 'Sales Executive'
    assert x not in [p['id'] for p in d['people']]
    # another agency sees nothing
    o = cl(srv, 'B').get('/api/talent-map/company?former=1&name=KEI Wires and Cables').get_json()
    assert o['people'] == [] and o['former'] == []
    assert c.get('/talent-map').status_code == 200
