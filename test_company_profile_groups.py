"""
Company Talent Map: every profile group's fields (Sales, Production, Tender …)

  * each person is shown with their own profile group; sales data without a group counts as Sales
  * the group's custom values come from the person's newest record (copies on other jobs included)
  * per group: numbers -> n, median, min-max (n >= 3) and total (internal only); tags / choice -> counts;
    text -> how many filled
  * client view: no per-person numbers or text, no totals; tags / choice and bands only
  * the Intelligence tab gets the same per-group aggregates
  * another agency's groups and values never show

Run:   pytest test_company_profile_groups.py -q
"""
import os
import sys
import json
import tempfile
import datetime

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CO, OTHER = 94501, 94502
ADMIN, OADMIN = 9450101, 9450201
M1, M2, MO = 9450301, 9450302, 9450303
G_SALES, G_PROD, G_TENDER, G_OTHER = 9450401, 9450402, 9450403, 9450404
S1, P1, P2, P3, P1C, T1, N1, F1, X1 = range(9450501, 9450510)


def ago(days):
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
            - datetime.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S')


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='cpg_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db(); c = conn.cursor(); now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", list(kw.values()))
    ins('companies', id=CO, name='Groups Co', status='active', created_at=now)
    ins('companies', id=OTHER, name='Other Agency', status='active', created_at=now)
    for uid, un, co in ((ADMIN, 'cpg_admin', CO), (OADMIN, 'cpg_other', OTHER)):
        ins('users', id=uid, username=un, password_hash='x', display_name=un.title(), role='user', status='approved',
            company_id=co, is_company_admin=1, created_at=now)
    for mid, own in ((M1, CO), (M2, CO), (MO, OTHER)):
        ins('mandates', id=mid, role='Role', client='Client', status='active', owner_id=own,
            assigned_user_id=ADMIN if own == CO else OADMIN, created_at=ago(90))
    sales_fields = [{'key': 'sys_' + k, 'type': 'system', 'sys': k, 'label': k} for k in
                    ('yearly_target', 'team_direct', 'product_handles')]
    prod_fields = [{'key': 'plant_capacity', 'type': 'number', 'label': 'Plant capacity', 'unit': 'MW'},
                   {'key': 'software', 'type': 'tags', 'label': 'Software'},
                   {'key': 'shift', 'type': 'choice', 'label': 'Shift', 'options': ['A', 'B']},
                   {'key': 'notes', 'type': 'text', 'label': 'Notes'}]
    tender_fields = [{'key': 'tender_value', 'type': 'number', 'label': 'Tender value', 'unit': '₹ Cr'}]
    for gid, co, name, fields, sales in ((G_SALES, CO, 'Sales', sales_fields, 1), (G_PROD, CO, 'Production', prod_fields, 0),
                                         (G_TENDER, CO, 'Tender', tender_fields, 0),
                                         (G_OTHER, OTHER, 'Production', prod_fields, 0)):
        ins('profile_groups', id=gid, company_id=co, name=name, fields=json.dumps(fields), is_sales=sales, is_active=1,
            created_at=now, updated_at=now)
    people = [(S1, M1, CO, 'Sam Sales', 0, 10), (P1, M1, CO, 'Priya Prod', G_PROD, None), (P2, M1, CO, 'Pankaj Prod', G_PROD, None),
              (P3, M1, CO, 'Pooja Prod', G_PROD, None), (T1, M1, CO, 'Tarun Tender', G_TENDER, None),
              (N1, M1, CO, 'Nina None', 0, None), (X1, MO, OTHER, 'Xavier Other', G_OTHER, None)]
    for cid, mid, own, nm, gid, tgt in people:
        ins('candidates', id=cid, mandate_id=mid, name=nm, company='Polycab India Ltd', designation='Engineer', stage='Screening',
            owner_id=own, profile_group_id=gid, yearly_target=tgt, created_at=ago(30), updated_at=ago(3))
    # P1 also on a second job; its newer value must win
    ins('candidates', id=P1C, mandate_id=M2, name='Priya Prod', company='Polycab India Ltd', designation='Engineer',
        stage='Screening', owner_id=CO, profile_group_id=G_PROD, copied_from=P1, created_at=ago(20), updated_at=ago(1))
    # a former employee (now elsewhere) in the Production group
    ins('candidates', id=F1, mandate_id=M1, name='Farah Former', company='Havells', designation='Engineer', stage='Screening',
        owner_id=CO, profile_group_id=G_PROD, created_at=ago(30), updated_at=ago(3))
    c.execute("INSERT INTO work_history (candidate_id, company, designation, start_date, end_date, is_current) VALUES (?,?,?,?,?,0)",
              (F1, 'Polycab', 'Engineer', '2018', '2021'))

    def val(cid, gid, key, co, text='', num=None, js='', when=None):
        ins('candidate_profile_values', candidate_id=cid, group_id=gid, field_key=key, company_id=co, value_text=text,
            value_num=num, value_json=js, updated_at=when or ago(5))
    for cid, cap, sw, sh in ((P1, 10, ['AutoCAD', 'ETAP'], 'A'), (P2, 20, ['AutoCAD'], 'B'), (P3, 30, ['SAP'], 'A')):
        val(cid, G_PROD, 'plant_capacity', CO, '%g' % cap, cap)
        val(cid, G_PROD, 'software', CO, ' | '.join(sw), None, json.dumps(sw))
        val(cid, G_PROD, 'shift', CO, sh)
        val(cid, G_PROD, 'notes', CO, 'ZZTEXT private note')
    val(P1C, G_PROD, 'plant_capacity', CO, '12', 12, when=ago(1))         # newer than P1's 10
    val(T1, G_TENDER, 'tender_value', CO, '50', 50)
    val(F1, G_PROD, 'plant_capacity', CO, '40', 40)
    val(X1, G_OTHER, 'plant_capacity', OTHER, '999', 999)
    val(X1, G_OTHER, 'notes', OTHER, 'ZZFOREIGN note')
    conn.commit(); conn.close()
    yield server


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


def company(c, view='internal'):
    return c.get('/api/talent-map/company?former=1&view=%s&name=Polycab' % view)


def test_people_carry_their_group_and_values(srv):
    j = company(cl(srv, ADMIN)).get_json()
    by = {p['id']: p for p in j['people']}
    assert len(j['people']) == 6                                  # P1 and its copy are one person
    assert by[S1]['pgroup'] == G_SALES                            # sales data, no group -> Sales
    assert by[N1]['pgroup'] == 0
    assert by[T1]['pgroup'] == G_TENDER
    p1 = next(p for p in j['people'] if p['name'] == 'Priya Prod')
    assert p1['pgroup'] == G_PROD and p1['pvals']['plant_capacity'] == 12          # newest record wins
    assert p1['pvals']['software'] == ['AutoCAD', 'ETAP'] and p1['pvals']['notes'] == 'ZZTEXT private note'
    names = {g['name'] for g in j['groups']}
    assert names == {'Sales', 'Production', 'Tender'}
    prod = next(g for g in j['groups'] if g['name'] == 'Production')
    assert [f['kind'] for f in prod['fields']] == ['number', 'tags', 'choice', 'text']
    sales = next(g for g in j['groups'] if g['name'] == 'Sales')
    assert sales['fields'][0]['kind'] == 'number' and sales['fields'][2]['kind'] == 'tags'
    assert j['former'][0]['name'] == 'Farah Former' and j['former'][0]['pgroup'] == G_PROD


def test_group_summary_numbers_tags_text(srv):
    j = company(cl(srv, ADMIN)).get_json()
    gs = {g['name']: g for g in j['group_summary']}
    prod = {f['key']: f for f in gs['Production']['fields']}
    assert gs['Production']['people'] == 3                        # current employees only
    cap = prod['plant_capacity']
    assert cap['n'] == 3 and cap['median'] == 20 and cap['min'] == 12 and cap['max'] == 30 and cap['total'] == 62
    assert prod['software']['top'] == [['AutoCAD', 2], ['ETAP', 1], ['SAP', 1]]
    assert prod['shift']['top'] == [['A', 2], ['B', 1]]
    assert prod['notes'] == {'key': 'notes', 'label': 'Notes', 'kind': 'text', 'unit': '', 'n': 3}
    tv = gs['Tender']['fields'][0]
    assert tv['n'] == 1 and tv.get('small') and 'median' not in tv


def test_client_view_strips_numbers_and_text(srv):
    r = company(cl(srv, ADMIN), view='client')
    raw = r.get_data(as_text=True)
    j = r.get_json()
    assert 'ZZTEXT' not in raw
    p1 = next(p for p in j['people'] if p['name'] == 'Priya Prod')
    assert set(p1['pvals']) == {'software', 'shift'}
    cap = next(f for g in j['group_summary'] if g['name'] == 'Production' for f in g['fields'] if f['key'] == 'plant_capacity')
    assert cap['median'] == 20 and 'total' not in cap                # bands only
    # the internal call keeps returning everything (existing behaviour unchanged)
    assert 'notes' in next(p for p in company(cl(srv, ADMIN)).get_json()['people'] if p['name'] == 'Priya Prod')['pvals']


def test_intelligence_has_the_same_group_fields(srv):
    a = cl(srv, ADMIN)
    j = a.get('/api/talent-map/intel?refresh=1&name=Polycab').get_json()
    pf = {g['name']: g for g in j['profile_fields']}
    cap = next(f for f in pf['Production']['fields'] if f['key'] == 'plant_capacity')
    assert cap['median'] == 20 and cap['total'] == 62
    c = a.get('/api/talent-map/intel?refresh=1&view=client&name=Polycab')
    raw = c.get_data(as_text=True)
    assert 'ZZTEXT' not in raw and 'Priya' not in raw and '"total"' not in raw
    pf = {g['name']: g for g in c.get_json()['profile_fields']}
    assert next(f for f in pf['Production']['fields'] if f['key'] == 'software')['top'][0] == ['AutoCAD', 2]


def test_other_agency_sees_only_its_own(srv):
    o = company(cl(srv, OADMIN)).get_json()
    raw = json.dumps(o)
    assert len(o['people']) == 1 and o['people'][0]['pvals']['plant_capacity'] == 999
    assert 'Priya' not in raw and 'ZZTEXT' not in raw and [g['name'] for g in o['groups']] == ['Production']
    a = json.dumps(company(cl(srv, ADMIN)).get_json())
    assert '999' not in a and 'ZZFOREIGN' not in a and 'Xavier' not in a


def test_page_has_the_group_view(srv):
    r = cl(srv, ADMIN).get('/talent-map')
    assert r.status_code == 200 and b'data-grp' in r.data and b'profile fields' in r.data
