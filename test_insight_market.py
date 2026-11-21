"""
Insight Engine — Wave 3: Market Intel + Talent Pulse  (modules/insight_market.py)

  * city normalisation
  * salary grid maths on fixture data; n >= 3 rule; companies grouped by company key
  * the period counts REAL activity only (a record merely touched does not count)
  * period / domain / client filters; previous-period comparison
  * market movement per company; top reasons; market signals
  * Talent Pulse: only aggregated numbers with n, no names / individual CTCs
  * the newsletter draft is a draft: nothing queued or sent; admins only
  * recruiters can open it (owner's decision) but see only their own jobs
  * tenant isolation; the per-agency switch; CSV export

Run:   pytest test_insight_market.py -q
"""
import os
import sys
import csv
import io
import tempfile
import datetime

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CO, OTHER = 94301, 94302
ADMIN, OADMIN, RIYA = 9430101, 9430201, 9430102
M1, M_SCH, M_R, M_O = 9430301, 9430302, 9430303, 9430304
P = {k: 9430400 + i for i, k in enumerate(['P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7', 'P8', 'P10', 'R1', 'F1'])}


def ago(days):
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
            - datetime.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S')


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='insight3_'))
    import io as _io, contextlib
    with contextlib.redirect_stdout(_io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db(); c = conn.cursor(); now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", list(kw.values()))
    ins('companies', id=CO, name='Market Co', status='active', created_at=now)
    ins('companies', id=OTHER, name='Other Agency', status='active', created_at=now)
    for uid, un, co, adm in ((ADMIN, 'mi_admin', CO, 1), (OADMIN, 'mi_other', OTHER, 1), (RIYA, 'mi_riya', CO, 0)):
        ins('users', id=uid, username=un, password_hash='x', display_name=un.title(), role='user', status='approved',
            company_id=co, is_company_admin=adm, created_at=now)
    ins('mandates', id=M1, role='LV Sales', client='Lauritz Knudsen', status='active', owner_id=CO, assigned_user_id=ADMIN, created_at=ago(500))
    ins('mandates', id=M_SCH, role='MV Sales', client='Schneider Electric India', status='active', owner_id=CO, assigned_user_id=ADMIN, created_at=ago(500))
    ins('mandates', id=M_R, role='Service', client='Havells', status='active', owner_id=CO, assigned_user_id=RIYA, created_at=ago(500))
    ins('mandates', id=M_O, role='X', client='Y', status='active', owner_id=OTHER, assigned_user_id=OADMIN, created_at=ago(500))
    c.execute('INSERT OR IGNORE INTO mandate_assignees (company_id, mandate_id, user_id, is_primary, is_active) VALUES (?,?,?,1,1)',
              (CO, M_R, RIYA))
    cs, lv, mv = 'Channel Sales', '["LV"]', '["MV"]'
    rows = [
        # key, mandate, owner, name, company, location, family, band, domains, ctc, ectc, history [(stage, days ago)]
        ('P1', M1, CO, 'Asha Verma', 'Siemens Ltd', 'Bangalore', cs, 'Manager', lv, 10, 13, [('Called', 20), ('Not Interested', 10)]),
        ('P2', M1, CO, 'Ravi Kumar', 'Siemens', 'Bengaluru, Karnataka', cs, 'Manager', lv, 12, 15, [('Not Interested', 12)]),
        ('P3', M1, CO, 'Neha Gupta', 'ABB India', 'Whitefield, Bangalore', cs, 'Manager', lv, 14, 0, [('Interested', 15)]),
        ('P4', M1, CO, 'Vikram Rao', 'Siemens Limited', 'BANGALORE', cs, 'Manager', lv, 20, 24, [('Interested', 9)]),
        ('P5', M_SCH, CO, 'Kiran Shah', 'Schneider Electric', 'Pune', 'Key Account / Project Sales', 'Sr Manager', mv, 30, 36, [('Called', 8)]),
        # previous period (days 180–360 ago)
        ('P6', M1, CO, 'Prev One', 'Siemens', 'Mumbai', cs, 'Manager', lv, 9, 11, [('Not Interested', 250)]),
        ('P7', M1, CO, 'Prev Two', 'Siemens', 'Navi Mumbai', cs, 'Manager', lv, 9, 12, [('Interested', 250)]),
        ('P8', M1, CO, 'Prev Three', 'Siemens', 'Thane', cs, 'Manager', lv, 10, 12, [('Interested', 240)]),
        # touched yesterday by a background job, no real activity: must NOT count
        ('P10', M1, CO, 'Stale Person', 'Siemens Ltd', 'Bangalore', cs, 'Manager', lv, 50, 60, []),
        ('R1', M_R, CO, 'Riya Cand', 'Polycab', 'Chennai', 'Service / Field', 'Manager', lv, 8, 10, [('Called', 5)]),
        ('F1', M_O, OTHER, 'Foreign Cand', 'Siemens Ltd', 'Bangalore', cs, 'Manager', lv, 99, 120, [('Interested', 5)]),
    ]
    for key, mid, own, nm, comp, loc, fam, band, dom, ctc, ectc, hist in rows:
        ins('candidates', id=P[key], mandate_id=mid, name=nm, company=comp, location=loc, role_family=fam, level_band=band,
            domain_tags=dom, ctc_current=ctc, ctc_expected=ectc, stage=(hist[-1][0] if hist else 'Screening'),
            owner_id=own, phone='97%08d' % P[key], created_at=ago(500), updated_at=ago(1))
        for st, d in hist:
            c.execute('INSERT INTO stage_history (candidate_id, from_stage, to_stage, note, created_at) VALUES (?,?,?,?,?)',
                      (P[key], 'Screening', st, '', ago(d)))
    for key, code, d in (('P1', 'ctc_gap', 10), ('P2', 'ctc_gap', 12), ('P6', 'location', 250)):
        c.execute('INSERT INTO candidate_reasons (owner_id, candidate_id, mandate_id, stage, reason_code, detail, client_feedback, '
                  'source, user_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (CO, P[key], M1, 'Not Interested', code, 'ZZDETAIL secret', '', 'manual', ADMIN, ago(d)))
    for comp, sig, d in (('Siemens', 'freeze', 5), ('Siemens Ltd', 'freeze', 6), ('ABB', 'hiring', 7), ('L&T', 'exit', 260)):
        c.execute('INSERT INTO market_notes (owner_id, candidate_id, company_name, company_key, signal_type, note, source, '
                  'user_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)', (CO, 0, comp, comp.lower().replace(' ltd', ''), sig,
                                                                       'ZZNOTE text', 'manual', ADMIN, ago(d)))
    conn.commit(); conn.close()
    from modules import insight_market
    insight_market.invalidate(CO); insight_market.invalidate(OTHER)
    yield server


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


def data(c, **q):
    q.setdefault('refresh', '1')
    return c.get('/api/market-intel/data?' + '&'.join('%s=%s' % kv for kv in q.items()))


def test_city_normalisation(srv):
    from modules.insight_market import normalise_city as n
    assert n('Bangalore') == n('Bengaluru, Karnataka') == n('Whitefield, Bangalore') == n('BANGALORE') == 'Bengaluru'
    assert n('Gurgaon, Haryana') == n('Noida') == n('New Delhi') == 'Delhi NCR'
    assert n('Navi Mumbai') == n('Thane West') == 'Mumbai'
    assert n('Secunderabad') == 'Hyderabad'
    assert n('Pune / Bangalore') == 'Pune'                        # the first city named wins
    assert n('Mysore') == 'Other (South)'
    assert n('') == 'Not known'


def test_grid_maths_and_real_activity_only(srv):
    j = data(cl(srv, ADMIN), period=6).get_json()
    assert j['ok']
    g = {(c['family'], c['band'], c['city']): c for c in j['grid']}
    c = g[('Channel Sales', 'Manager', 'Bengaluru')]
    assert c['n'] == 4                                           # P10 (touched, no activity) is NOT in it
    assert c['median'] == 13 and c['median_expected'] == 15 and c['n_expected'] == 3
    assert c['median_hike_pct'] == 25.0 and c['n_hike'] == 3
    assert c['companies'][0]['n'] == 3 and c['companies'][1] == {'name': 'ABB India', 'n': 1}   # Siemens spellings merged
    small = g[('Key Account / Project Sales', 'Sr Manager', 'Pune')]
    assert small['n'] == 1 and small.get('small') and 'median' not in small
    assert ('Service / Field', 'Manager', 'Chennai') in g             # admin sees every job
    # previous period: P6–P8 in Mumbai
    gp = {(x['family'], x['band'], x['city']): x for x in j['grid_prev']}
    assert gp[('Channel Sales', 'Manager', 'Mumbai')]['n'] == 3


def test_filters_period_domain_clients(srv):
    a = cl(srv, ADMIN)
    six = data(a, period=6).get_json()
    assert six['market']['contacted'] == 6 and six['market']['interested_or_later'] == 2 and six['market']['said_no'] == 2
    assert six['market']['prev']['contacted'] == 3
    twelve = data(a, period=12).get_json()
    assert twelve['market']['contacted'] == 9 and twelve['market']['prev']['contacted'] == 0
    mv = data(a, period=6, domain='MV').get_json()
    assert mv['people'] == 1 and mv['grid'][0]['city'] == 'Pune'
    clients = data(a, period=6, companies='clients').get_json()
    assert clients['people'] == 1 and clients['movement'][0]['company'] == 'Schneider Electric' and clients['movement'][0]['client']
    others = data(a, period=6, companies='others').get_json()
    assert others['people'] == 5
    assert data(a, period=99).get_json()['filters']['period'] == 6       # unknown period falls back


def test_movement_reasons_signals(srv):
    j = data(cl(srv, ADMIN), period=6).get_json()
    sie = next(r for r in j['movement'] if r['key'] == 'siemens')
    assert sie['contacted'] == 3 and sie['said_no'] == 2 and sie['no_pct'] == 66.7
    assert sie['prev']['contacted'] == 3 and sie['prev']['said_no'] == 1 and sie['no_change_pts'] == 33.4
    assert sie['segments'][0]['city'] == 'Bengaluru' and sie['segments'][0]['contacted'] == 3
    assert j['reasons']['top'] == [{'code': 'ctc_gap', 'label': 'CTC gap', 'n': 2}]
    assert j['reasons_prev']['top'][0]['code'] == 'location'
    sig = {s['signal_type']: s for s in j['signals']}
    assert sig['freeze']['n'] == 2 and 'exit' not in sig


def test_talent_pulse_is_aggregated_with_n(srv):
    a = cl(srv, ADMIN)
    r = a.get('/api/market-intel/pulse?period=6').get_json()
    pts = r['points']
    assert 3 <= len(pts) <= 5
    kinds = [p['kind'] for p in pts]
    assert kinds[0] == 'pay' and 'openness' in kinds and 'shift' in kinds
    assert 'Bengaluru' in pts[0]['text'] and '25%' in pts[0]['text'] and '(n = 3)' in pts[0]['text']
    # P1, P2, P4 have both CTCs: current 10, 12, 20 -> 12; expected 13, 15, 24 -> 15
    assert '₹12 L' in pts[0]['text'] and '₹15 L' in pts[0]['text']
    for p in pts:
        assert p['n'] >= 3 and ('n = ' in p['text'] or ' of ' in p['text'])
    raw = str(r)
    for secret in ('Asha', 'Ravi', 'Neha', 'Vikram', 'Kiran', 'Stale', 'ZZDETAIL', 'ZZNOTE', '97%08d' % P['P1'],
                   '₹10 L', '₹20 L', '₹50 L'):
        assert secret not in raw
    shift = next(p for p in pts if p['kind'] == 'shift')
    assert shift['text'].startswith('Siemens') and '2 of 3' in shift['text'] and '1 of 3' in shift['text']


def test_pulse_draft_is_only_a_draft(srv):
    assert cl(srv, RIYA).post('/api/market-intel/pulse/draft', json={'period': 6}).status_code == 403
    r = cl(srv, ADMIN).post('/api/market-intel/pulse/draft', json={'period': 6}).get_json()
    assert r['ok'] and r['url'] == '/newsletter?i=%d' % r['issue_id']
    conn = srv.get_db()
    i = conn.execute('SELECT * FROM newsletter_issues WHERE id=?', (r['issue_id'],)).fetchone()
    assert i['company_id'] == CO and i['status'] == 'draft' and i['approved_at'] in ('', None) and not i['queued']
    assert i['title'].startswith('Talent Pulse') and '(n = 3)' in i['points']
    assert conn.execute('SELECT COUNT(*) FROM newsletter_recipients WHERE issue_id=?', (r['issue_id'],)).fetchone()[0] == 0
    conn.close()
    # the existing newsletter flow opens it as a normal draft for this agency only
    assert cl(srv, ADMIN).get('/api/newsletter/issue/%d' % r['issue_id']).get_json()['issue']['status'] == 'draft'
    assert cl(srv, OADMIN).get('/api/newsletter/issue/%d' % r['issue_id']).status_code in (403, 404)


def test_recruiter_sees_only_own_jobs(srv):
    j = data(cl(srv, RIYA), period=6).get_json()
    assert j['ok'] and j['people'] == 1 and j['can_draft'] is False
    assert [c['city'] for c in j['grid']] == ['Chennai']
    assert not any(r['key'] == 'siemens' for r in j['movement']) and j['reasons']['n'] == 0


def test_tenant_isolation(srv):
    o = data(cl(srv, OADMIN), period=6).get_json()
    assert o['people'] == 1 and o['market']['contacted'] == 1 and o['signals'] == [] and o['reasons']['n'] == 0
    assert all(c.get('small') for c in o['grid'])                    # their one person, never a band
    a = data(cl(srv, ADMIN), period=6).get_json()
    g = {(c['family'], c['band'], c['city']): c for c in a['grid']}
    assert g[('Channel Sales', 'Manager', 'Bengaluru')]['n'] == 4     # the other agency's person is not in it


def test_csv_export(srv):
    r = cl(srv, ADMIN).get('/api/market-intel/grid.csv?period=6')
    assert r.status_code == 200 and r.mimetype == 'text/csv' and 'attachment' in r.headers['Content-Disposition']
    rows = list(csv.reader(io.StringIO(r.get_data(as_text=True).lstrip('﻿'))))
    assert rows[0][:4] == ['Role family', 'Level', 'City', 'n']
    beng = next(x for x in rows if x[2] == 'Bengaluru')
    assert beng[3] == '4' and beng[4] == '13.0' and beng[7] == '25.0'
    assert any(x[4] == 'n too small' for x in rows[1:])
    assert 'Asha' not in r.get_data(as_text=True)


def test_switch_per_agency_and_page(srv):
    a = cl(srv, ADMIN)
    assert cl(srv, RIYA).post('/api/market-intel/config', json={'on': False}).status_code == 403
    assert a.post('/api/market-intel/config', json={'on': False}).get_json()['on'] is False
    for path in ('/api/market-intel/data', '/api/market-intel/grid.csv', '/api/market-intel/pulse'):
        assert a.get(path).status_code == 404
    assert a.post('/api/market-intel/pulse/draft', json={}).status_code == 404
    assert data(cl(srv, OADMIN)).status_code == 200                  # the other agency is unaffected
    assert a.post('/api/market-intel/config', json={'on': True}).get_json()['on'] is True
    r = a.get('/market-intel')
    assert r.status_code == 200 and b'Talent Pulse' in r.data and b'/app-nav.js' in r.data
