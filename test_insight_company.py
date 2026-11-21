"""
Insight Engine — Wave 2: Company Intelligence  (modules/insight_company.py)

  * pay bands: medians and hike % right on fixture data, the n >= 3 rule
  * people grid (role family x level), copies of one person counted once, former employees
  * openness index over the people we actually contacted
  * top reasons for "no"; the client branch (client rejections + why candidates said no to the client)
  * client view: no names, no individual CTCs, no free text
  * data quality, cache + invalidation, the per-agency switch
  * recruiter scope and tenant isolation

Run:   pytest test_insight_company.py -q
"""
import os
import sys
import json
import tempfile
import datetime

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CO, OTHER = 94201, 94202
ADMIN, OADMIN, RIYA = 9420101, 9420201, 9420102
M_LK, M_SIE, M_RIYA, M_OTHER = 9420301, 9420302, 9420303, 9420304
P1, P2, P3, P4, K1, K2, UN, P1_COPY, ABB, FORMER, RIYA_C, FOREIGN = range(9420401, 9420413)


def ago(days):
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
            - datetime.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S')


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='insight2_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db(); c = conn.cursor(); now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", list(kw.values()))
    ins('companies', id=CO, name='Intel Co', status='active', created_at=now)
    ins('companies', id=OTHER, name='Other Agency', status='active', created_at=now)
    for uid, un, co, adm in ((ADMIN, 'ci_admin', CO, 1), (OADMIN, 'ci_other', OTHER, 1), (RIYA, 'ci_riya', CO, 0)):
        ins('users', id=uid, username=un, password_hash='x', display_name=un.title(), role='user', status='approved',
            company_id=co, is_company_admin=adm, created_at=now)
    ins('mandates', id=M_LK, role='LV Sales', client='Lauritz Knudsen', status='active', owner_id=CO, assigned_user_id=ADMIN, created_at=ago(90))
    ins('mandates', id=M_SIE, role='MV Sales', client='Siemens Limited', status='active', owner_id=CO, assigned_user_id=ADMIN, created_at=ago(90))
    ins('mandates', id=M_RIYA, role='Service', client='Havells', status='active', owner_id=CO, assigned_user_id=RIYA, created_at=ago(90))
    ins('mandates', id=M_OTHER, role='X', client='Y', status='active', owner_id=OTHER, assigned_user_id=OADMIN, created_at=ago(90))
    c.execute('INSERT OR IGNORE INTO mandate_assignees (company_id, mandate_id, user_id, is_primary, is_active) VALUES (?,?,?,1,1)',
              (CO, M_RIYA, RIYA))
    cs = '["Channel Sales"]'
    people = [
        # id, mandate, name, company, role_family, function_tags, level, ctc, ectc, notice, stage
        (P1, M_LK, 'Asha Verma', 'Siemens Ltd', 'Channel Sales', cs, 'Manager', 10, 13, 30, 'Interested'),
        (P2, M_LK, 'Ravi Kumar', 'Siemens Limited', 'Channel Sales', cs, 'Manager', 12, 15, 45, 'Not Interested'),
        (P3, M_LK, 'Neha Gupta', 'SIEMENS', 'Channel Sales', cs, 'Manager', 14, 0, 90, 'Called'),
        (P4, M_LK, 'Vikram Rao', 'Siemens Pvt. Ltd.', 'Channel Sales', cs, 'Manager', 20, 24, 120, 'Screening'),
        (K1, M_LK, 'Kiran Shah', 'Siemens Ltd', 'Key Account / Project Sales', '', 'Sr Manager', 30, 0, 0, 'Shared with Client'),
        (K2, M_LK, 'Kunal Jain', 'Siemens Ltd', '', '["Key Account Management"]', 'Sr Manager', 32, 0, 0, 'Not Interested'),
        (UN, M_LK, 'Uma Nair', 'Siemens Ltd', '', '', '', 0, 0, 0, 'Not Suitable'),
        (ABB, M_SIE, 'Arjun ABB', 'ABB India', 'Channel Sales', cs, 'Manager', 11, 14, 60, 'Not Interested'),
        (FORMER, M_LK, 'Farhan Former', 'Polycab', 'Channel Sales', cs, 'Manager', 18, 21, 60, 'Screening'),
        (RIYA_C, M_RIYA, 'Riya Seen', 'Siemens Ltd', 'Service / Field', '', 'Manager', 9, 11, 30, 'Called'),
    ]
    for pid, mid, nm, comp, fam, ft, lv, ctc, ectc, npd, st in people:
        ins('candidates', id=pid, mandate_id=mid, name=nm, company=comp, role_family=fam, function_tags=ft, level_band=lv,
            ctc_current=ctc, ctc_expected=ectc, notice_period=npd, stage=st, owner_id=CO, phone='98%08d' % pid,
            created_at=ago(60), updated_at=ago(5))
    # the same person as P1 on the Siemens job (a linked copy), rejected by the client there
    ins('candidates', id=P1_COPY, mandate_id=M_SIE, name='Asha Verma', company='Siemens Ltd', role_family='Channel Sales',
        level_band='Manager', ctc_current=10, ctc_expected=13, notice_period=30, stage='Client Rejected on Paper', owner_id=CO,
        copied_from=P1, created_at=ago(60), updated_at=ago(10))
    ins('candidates', id=FOREIGN, mandate_id=M_OTHER, name='Foreign Person', company='Siemens Ltd', role_family='Channel Sales',
        level_band='Manager', ctc_current=99, ctc_expected=120, stage='Interested', owner_id=OTHER, created_at=ago(60), updated_at=ago(1))
    c.execute("INSERT INTO work_history (candidate_id, company, designation, start_date, end_date, is_current) VALUES (?,?,?,?,?,0)",
              (FORMER, 'Siemens Pvt Ltd', 'Sales Engineer', '2015', '2019'))
    for cid, mid, st, code, det, fb, when in (
            (P2, M_LK, 'Not Interested', 'ctc_gap', 'Wants 30 percent hike', '', ago(3)),
            (K2, M_LK, 'Not Interested', 'client_brand', 'ZZDETAIL brand concern', '', ago(2)),
            (P1_COPY, M_SIE, 'Client Rejected on Paper', 'profile_mismatch', '', 'ZZFEEDBACK needs MV exposure', ago(4)),
            (ABB, M_SIE, 'Not Interested', 'client_brand', 'ZZABB does not like the client', '', ago(1))):
        c.execute('INSERT INTO candidate_reasons (owner_id, candidate_id, mandate_id, stage, reason_code, detail, client_feedback, '
                  'source, user_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)', (CO, cid, mid, st, code, det, fb, 'manual', ADMIN, when))
    for comp, key, sig, note, owner, cid in (('Siemens', 'siemens', 'hiring', 'ZZNOTE 3 openings in Pune', CO, P1),
                                             ('Siemens Ltd', 'siemens', 'freeze', 'ZZNOTE freeze till March', CO, 0),
                                             ('Siemens', 'siemens', 'exit', 'ZZFOREIGN note', OTHER, FOREIGN)):
        c.execute('INSERT INTO market_notes (owner_id, candidate_id, company_name, company_key, signal_type, note, source, '
                  'user_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)', (owner, cid, comp, key, sig, note, 'manual', ADMIN, ago(2)))
    conn.commit(); conn.close()
    from modules import insight_company
    insight_company.invalidate(CO); insight_company.invalidate(OTHER)
    yield server


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


def intel(c, name='Siemens', view='internal', refresh=True):
    r = c.get('/api/talent-map/intel?name=%s&view=%s%s' % (name, view, '&refresh=1' if refresh else ''))
    return r


def test_people_grid_copies_once_and_former(srv):
    j = intel(cl(srv, ADMIN)).get_json()
    assert j['ok'] and j['company'] in ('Siemens Ltd',)
    P = j['people']
    assert P['current'] == 8                    # P1..P4, K1, K2, UN, RIYA_C (P1's copy counted once)
    assert P['former'] == 1
    g = P['current_grid']
    assert g['cells']['Channel Sales']['Manager'] == 4
    assert g['cells']['Key Account / Project Sales']['Sr Manager'] == 2      # K2 derived from its Function tag
    assert g['cells']['Not tagged']['Not tagged'] == 1
    assert g['families'][0] == 'Channel Sales' and g['families'][-1] == 'Not tagged'


def test_pay_medians_hike_and_min_n(srv):
    j = intel(cl(srv, ADMIN)).get_json()
    pay = {(p['family'], p['band']): p for p in j['pay']}
    cs = pay[('Channel Sales', 'Manager')]
    assert cs['n'] == 4 and cs['min'] == 10 and cs['max'] == 20 and cs['median'] == 13
    assert cs['median_expected'] == 15 and cs['n_expected'] == 3          # 13, 15, 24 (0 ignored)
    assert cs['median_hike_pct'] == 25.0 and cs['n_hike'] == 3            # 30 %, 25 %, 20 %
    k = pay[('Key Account / Project Sales', 'Sr Manager')]
    assert k['n'] == 2 and k.get('small') and 'median' not in k and 'min' not in k
    nb = {b['bucket']: b['n'] for b in j['notice']['buckets']}
    assert nb == {'0–30 days': 2, '31–60 days': 1, '61–90 days': 1, '90+ days': 1}


def test_openness_counts_only_contacted(srv):
    o = intel(cl(srv, ADMIN)).get_json()['openness']
    # contacted: P1 (Interested), P2 (NI), P3 (Called), K1 (Shared), K2 (NI), RIYA_C (Called)
    # not contacted: P4 (Screening), UN (Not Suitable)
    assert o['contacted'] == 6 and o['interested_or_later'] == 2 and o['said_no'] == 2 and o['undecided'] == 2
    assert o['openness_pct'] == 33.3 and o['no_pct'] == 33.3


def test_reasons_and_client_branch(srv):
    j = intel(cl(srv, ADMIN)).get_json()
    top = {t['code']: t['n'] for t in j['reasons']['top']}
    assert top == {'ctc_gap': 1, 'client_brand': 1} and j['reasons']['n'] == 2
    assert any('Wants 30 percent hike' == x['detail'] for x in j['reasons']['recent'])
    cb = j['client']
    assert cb['is_client'] and cb['jobs'] == 1
    assert [t['code'] for t in cb['client_rejections']['top']] == ['profile_mismatch']
    assert cb['client_rejections']['recent'][0]['feedback'] == 'ZZFEEDBACK needs MV exposure'
    assert [t['code'] for t in cb['candidate_no_to_client']['top']] == ['client_brand']
    assert cb['candidate_no_to_client']['brand_signals'][0]['code'] == 'client_brand'
    # a company that is not a client has no client section
    assert intel(cl(srv, ADMIN), name='ABB India').get_json()['client'] is None


def test_notes_timeline_and_quality(srv):
    j = intel(cl(srv, ADMIN)).get_json()
    assert j['notes']['n'] == 2 and {x['signal_type'] for x in j['notes']['by_signal']} == {'hiring', 'freeze'}
    assert 'ZZFOREIGN' not in json.dumps(j)
    assert j['notes']['timeline'][0]['note'].startswith('ZZNOTE')
    q = j['quality']
    assert q['records'] == 8
    assert q['expected_ctc_pct'] == 50.0                  # P1, P2, P4, RIYA_C: 4 of 8
    assert q['role_family_pct'] == 87.5                   # all but UN
    # negative outcomes on any job of these people: P2, K2 (NI), UN (Not Suitable), P1's copy (Client Rejected)
    assert q['negatives'] == 4 and q['negatives_with_reason_pct'] == 75.0


def test_client_view_leaks_nothing_personal(srv):
    r = intel(cl(srv, ADMIN), view='client')
    raw = r.get_data(as_text=True)
    j = r.get_json()
    assert j['view'] == 'client'
    for secret in ('Asha', 'Ravi', 'Kiran', 'Kunal', 'Uma', 'Arjun', 'Farhan', 'Riya Seen', '98%08d' % P1,
                   'Wants 30 percent', 'ZZDETAIL', 'ZZFEEDBACK', 'ZZABB', 'ZZNOTE'):
        assert secret not in raw, secret
    for p in j['pay']:                                   # small groups carry no CTC values at all
        if p['n'] < 3:
            assert not ({'min', 'max', 'median', 'median_expected'} & set(p))
    assert 'recent' not in j['reasons'] and 'timeline' not in j['notes']
    assert 'recent' not in j['client']['client_rejections']
    assert '"id"' not in raw and 'candidate_id' not in raw
    # the aggregates are still there
    assert j['people']['current'] == 8 and j['openness']['contacted'] == 6 and j['notes']['n'] == 2


def test_cache_and_invalidation_on_wave1_writes(srv):
    a = cl(srv, ADMIN)
    assert intel(a).get_json()['cached'] is False
    assert intel(a, refresh=False).get_json()['cached'] is True
    # a market note through Wave 1 clears this company's cache
    r = a.post(f'/api/candidates/{P3}/market-notes', json={'signal_type': 'pay', 'note': 'ZZNOTE increments 10%'})
    assert r.status_code == 200
    j = intel(a, refresh=False).get_json()
    assert j['cached'] is False and j['notes']['n'] == 3
    # so does a reason
    intel(a, refresh=False)
    a.post(f'/api/candidates/{P3}/stage', json={'stage': 'Not Interested', 'reason_code': 'location'})
    j = intel(a, refresh=False).get_json()
    assert j['cached'] is False and j['openness']['said_no'] == 3


def test_recruiter_scope(srv):
    j = intel(cl(srv, RIYA)).get_json()
    assert j['people']['current'] == 1                # only the Siemens person on her own job
    raw = json.dumps(j)
    assert 'ZZFEEDBACK' not in raw and 'Wants 30 percent' not in raw
    assert j['client']['jobs'] == 0 and j['client']['client_rejections']['n'] == 0
    assert j['notes']['n'] == 1                       # the note without a candidate; not the one tied to P1
    assert cl(srv, RIYA).post('/api/talent-map/intel/config', json={'on': False}).status_code == 403


def test_tenant_isolation(srv):
    o = intel(cl(srv, OADMIN)).get_json()
    raw = json.dumps(o)
    assert o['people']['current'] == 1 and o['notes']['n'] == 1
    assert o['client'] is None and o['reasons']['n'] == 0
    for secret in ('Asha', 'ZZNOTE', 'ZZFEEDBACK', 'Wants 30 percent'):
        assert secret not in raw
    pay = o['pay'][0]
    assert pay['n'] == 1 and pay.get('small')
    # and CO never sees the other agency's person or note
    a = intel(cl(srv, ADMIN)).get_json()
    assert 'ZZFOREIGN' not in json.dumps(a) and a['people']['current'] == 8


def test_switch_per_agency(srv):
    a = cl(srv, ADMIN)
    assert a.get('/api/talent-map/intel/config').get_json()['on'] is True
    assert a.post('/api/talent-map/intel/config', json={'on': False}).get_json()['on'] is False
    r = intel(a)
    assert r.status_code == 404 and r.get_json()['off'] is True
    assert intel(cl(srv, OADMIN)).status_code == 200            # the other agency is not affected
    assert a.post('/api/talent-map/intel/config', json={'on': True}).get_json()['on'] is True
    assert intel(a).status_code == 200
    assert a.get('/api/talent-map/intel?name=').status_code == 400


def test_page_has_the_tab(srv):
    r = cl(srv, ADMIN).get('/talent-map')
    assert r.status_code == 200 and b'Intelligence' in r.data and b'/intel?view=' in r.data
