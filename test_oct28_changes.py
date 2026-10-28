"""
Oct 28 2026 changes:
  * per-job "automated e-mails OFF" switch (modules/mail_guard.py) and its use by
    Re-engage, mass campaigns and the Talent Pool "emailable" flag
  * Final Result: resume + call evidence → one decision (modules/final_result.py)
  * Pitch tab: checklist only (no call script)
  * BD: quick-create a company while adding a person
  * LinkedIn capture → Central Database / BD (modules/linkedin_capture.py)
  * Talent Pool segmentation rules

Run:   pytest test_oct28_changes.py -q
"""
import os
import sys
import json
import tempfile
import datetime

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CO, OTHER = 9281, 9282
ADMIN, OADMIN = 92801, 92802
M_ON, M_OFF = 928101, 928102


def ago(days):
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
            - datetime.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S')


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='oct28_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db(); c = conn.cursor(); now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", list(kw.values()))
    ins('companies', id=CO, name='Oct Co', status='active', created_at=now)
    ins('companies', id=OTHER, name='Other Co', status='active', created_at=now)
    for uid, un, co in ((ADMIN, 'oct_admin', CO), (OADMIN, 'oct_other', OTHER)):
        ins('users', id=uid, username=un, password_hash='x', display_name=un.title(), role='user', status='approved',
            company_id=co, is_company_admin=1, created_at=now)
    ins('mandates', id=M_ON, role='Solar O&M Engineer', client='Azure Power', status='active', owner_id=CO,
        assigned_user_id=ADMIN, created_at=ago(400))
    ins('mandates', id=M_OFF, role='DC Electrical Lead', client='Equinix', status='active', owner_id=CO,
        assigned_user_id=ADMIN, created_at=ago(400))
    for cid, mid, name, em in ((928201, M_ON, 'Asha Verma', 'asha@ex.com'),
                               (928202, M_OFF, 'Ravi Kumar', 'ravi@ex.com'),
                               (928203, M_ON, 'Ravi Kumar', 'RAVI@ex.com'),      # same person, other job
                               (928204, M_ON, 'Neha Gupta', 'neha@ex.com')):
        ins('candidates', id=cid, mandate_id=mid, name=name, email=em, stage='Screening', owner_id=CO, created_at=ago(300))
    conn.commit(); conn.close()
    yield server


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


# ── per-job automated e-mail switch ────────────────────────────────────────
def test_switch_toggles_and_is_tenant_scoped(srv):
    a = cl(srv, ADMIN)
    r = a.get(f'/api/mandates/{M_OFF}/auto-mail').get_json()
    assert r['ok'] and r['off'] is False and r['candidates'] == 1
    r = a.post(f'/api/mandates/{M_OFF}/auto-mail', json={'off': True}).get_json()
    assert r['off'] is True
    # another company cannot read or flip it
    o = cl(srv, OADMIN)
    assert o.post(f'/api/mandates/{M_OFF}/auto-mail', json={'off': False}).status_code == 404


def test_blocked_covers_the_person_not_only_the_record(srv):
    from modules import mail_guard as mg
    conn = srv.get_db()
    try:
        ids, ems, _ = mg.blocked(conn, CO)
        assert 928202 in ids and 'ravi@ex.com' in ems
        assert mg.is_blocked(conn, CO, 928203, 'RAVI@ex.com')      # same e-mail in a job that is ON
        assert not mg.is_blocked(conn, CO, 928201, 'asha@ex.com')
        assert not mg.is_blocked(conn, OTHER, 928202, 'ravi@ex.com')
    finally:
        conn.close()


def test_reengage_excludes_people_of_a_switched_off_job(srv):
    import modules.reengage as re_
    conn = srv.get_db()
    try:
        ppl = {p['email']: p for p in re_.build_people(conn, CO)}
    finally:
        conn.close()
    assert ppl['ravi@ex.com']['state'] == 'excluded' and 'Job e-mails off' in ppl['ravi@ex.com']['reason']
    assert ppl['asha@ex.com']['state'] != 'excluded' or 'Job e-mails off' not in ppl['asha@ex.com']['reason']


def test_campaign_list_skips_switched_off_people(srv):
    conn = srv.get_db()
    try:
        cur = conn.execute("INSERT INTO campaigns (owner_id,created_by,name,mandate_id,subject,body,filter_stages,candidate_ids,"
                           "status,daily_cap,dedupe_days,sent_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'draft',100,0,0,?,?)",
                           (CO, ADMIN, 'T', None, 'Hi', 'Hello {first_name}', '[]',
                            json.dumps([928201, 928202, 928203, 928204]), srv.ts(), srv.ts()))
        conn.commit()
        camp = srv._camp_row(conn, cur.lastrowid, CO)
        built = {cid: (st, why) for cid, _em, st, why in srv._camp_build_list(conn, CO, camp)}
    finally:
        conn.close()
    assert built[928202][0] == 'skipped' and 'switched off' in built[928202][1]
    assert built[928203][0] == 'skipped'                     # same person, other job
    assert built[928201][0] == 'pending'


def test_switch_on_restores(srv):
    from modules import mail_guard as mg
    a = cl(srv, ADMIN)
    a.post(f'/api/mandates/{M_OFF}/auto-mail', json={'off': False})
    conn = srv.get_db()
    try:
        assert not mg.is_blocked(conn, CO, 928202, 'ravi@ex.com')
    finally:
        conn.close()


# ── Final Result: combine + fixed decision rule ────────────────────────────
def test_final_result_rule_uses_combined_statuses():
    from modules.final_result import normalise
    rows_in = [{'requirement': 'SCADA', 'status': 'UNCLEAR', 'evidence': 'not in CV', 'kind': 'skill'},
               {'requirement': 'Pune location', 'status': 'MET', 'evidence': 'lives in Pune', 'kind': 'location'}]
    out = {'rows': [{'requirement': 'SCADA', 'resume_evidence': 'not in CV', 'call_evidence': '4 yrs on WinCC SCADA at ABB',
                     'final_status': 'MET', 'why': 'call gave specific evidence'},
                    {'requirement': 'Pune location', 'final_status': 'MET'}],
           'final_score': 82, 'summary': 'Good', 'client_summary': 'A perfect fit engineer at ₹12 LPA.'}
    res = normalise(out, rows_in, {'interest_level': 'HIGH'})
    assert [r['status'] for r in res['rows']] == ['MET', 'MET']
    assert res['rows'][0]['resume_status'] == 'UNCLEAR'
    assert res['decision']['status'] == 'shortlisted'
    assert 'LPA' not in res['client_summary'] and 'perfect fit' not in res['client_summary'].lower()


def test_final_result_low_interest_never_shortlisted():
    from modules.final_result import normalise
    rows_in = [{'requirement': 'PLC', 'status': 'MET', 'evidence': 'yes'}]
    res = normalise({'rows': [{'requirement': 'PLC', 'final_status': 'MET'}], 'final_score': 90}, rows_in,
                    {'interest_level': 'LOW'})
    assert res['decision']['status'] == 'hold' and 'interest' in res['decision']['rule'].lower()


def test_final_result_keeps_must_have_list_even_if_model_drops_one():
    from modules.final_result import normalise
    rows_in = [{'requirement': 'A', 'status': 'MET'}, {'requirement': 'B', 'status': 'NOT_MET'}]
    res = normalise({'rows': [{'requirement': 'A', 'final_status': 'MET'}], 'final_score': 80}, rows_in, {})
    assert [r['requirement'] for r in res['rows']] == ['A', 'B']
    assert res['rows'][1]['status'] == 'NOT_MET' and res['decision']['status'] == 'rejected'


def test_final_result_get_without_inputs(srv):
    a = cl(srv, ADMIN)
    r = a.get('/api/candidates/928201/final-result').get_json()
    assert r['ok'] and r['result'] is None and r['have'] == {'pitch': False, 'call': False}
    assert cl(srv, OADMIN).get('/api/candidates/928201/final-result').status_code == 404


# ── Pitch: checklist only ──────────────────────────────────────────────────
def test_pitch_checklist_mode_rules():
    from modules import pitch as P
    assert P.SCRIPT_IN_PITCH is False
    p = {'script': '', 'exposure_questions': [{'skill': s, 'q': 'Have you worked on %s?' % s} for s in 'abcde'],
         'ctc_response': 'The client decides.'}
    assert P._violations(p) == []
    assert 'ANSWER CHECKLIST' in P._checklist_prompt('en')


# ── BD: quick company ──────────────────────────────────────────────────────
def test_quick_company_creates_once_and_reuses(srv):
    a = cl(srv, ADMIN)
    r1 = a.post('/api/bd/companies/quick', json={'name': 'Waaree Energies'}).get_json()
    assert r1['ok'] and r1['created'] is True
    r2 = a.post('/api/bd/companies/quick', json={'name': '  waaree   energies '}).get_json()
    assert r2['id'] == r1['id'] and r2['created'] is False
    p = a.post('/api/bd/records/people', json={'name': 'Kiran', 'client_id': r1['id'], 'designation': 'HR Head'}).get_json()
    assert p.get('ok'), p
    assert a.post('/api/bd/companies/quick', json={'name': 'x'}).status_code == 400


# ── LinkedIn capture ───────────────────────────────────────────────────────
F = {'name': 'Priya Sharma', 'headline': 'Electrical Lead – Data Centers', 'designation': 'Electrical Lead',
     'company': 'Yotta Infrastructure', 'location': 'Navi Mumbai', 'experience': 9.5,
     'work_history': [{'company': 'Yotta Infrastructure', 'designation': 'Electrical Lead', 'start_date': 'Jan 2021',
                       'end_date': 'Present', 'is_current': True}],
     'education': [{'school': 'VJTI', 'degree': 'B.E. Electrical'}], 'key_skills': ['UPS', 'LT panels']}


def test_public_url_normalised():
    from modules.linkedin_capture import public_url
    assert public_url('https://in.linkedin.com/in/Priya-Sharma-12ab/?trk=x') == 'https://www.linkedin.com/in/priya-sharma-12ab/'
    assert public_url('https://www.naukri.com/x') == ''


def test_linkedin_to_central_db_then_update_not_duplicate(srv):
    a = cl(srv, ADMIN)
    url = 'https://www.linkedin.com/in/priya-sharma-12ab/'
    r = a.post('/api/assistant/linkedin/save', json={'target': 'candidate', 'url': url, 'fields': F}).get_json()
    assert r['ok'] and r['action'] == 'added' and r['central'] is True
    conn = srv.get_db()
    try:
        c = conn.execute('SELECT * FROM candidates WHERE id=?', (r['candidate_id'],)).fetchone()
        assert c['linkedin_url'] == url and c['owner_id'] == CO and c['company'] == 'Yotta Infrastructure'
        assert conn.execute('SELECT COUNT(*) FROM work_history WHERE candidate_id=?', (c['id'],)).fetchone()[0] == 1
    finally:
        conn.close()
    r2 = a.post('/api/assistant/linkedin/save', json={'target': 'candidate', 'url': url + '?x=1',
                                                      'fields': dict(F, designation='Senior Electrical Lead')}).get_json()
    assert r2['action'] == 'updated' and r2['candidate_id'] == r['candidate_id']


def test_linkedin_to_bd_creates_company_and_person(srv):
    a = cl(srv, ADMIN)
    url = 'https://www.linkedin.com/in/amit-hr-99/'
    r = a.post('/api/assistant/linkedin/save', json={'target': 'bd', 'url': url,
                                                     'fields': dict(F, name='Amit Rao', company='Avaada Energy')}).get_json()
    assert r['ok'] and r['action'] == 'added' and r['company_created'] is True and r['company'] == 'Avaada Energy'
    r2 = a.post('/api/assistant/linkedin/save', json={'target': 'bd', 'url': url,
                                                      'fields': dict(F, name='Amit Rao', company='Avaada Energy')}).get_json()
    assert r2['action'] == 'updated' and r2['person_id'] == r['person_id'] and r2['company_created'] is False
    # visible in the BD People table (prospect company)
    q = a.post('/api/bd/records/people/query', json={'q': 'Amit Rao'}).get_json()
    assert any(x['id'] == r['person_id'] for x in q['records'])


def test_linkedin_save_needs_login_and_valid_url(srv):
    assert srv.app.test_client().post('/api/assistant/linkedin/save', json={}).status_code == 401
    a = cl(srv, ADMIN)
    assert a.post('/api/assistant/linkedin/save', json={'url': 'https://naukri.com/x', 'fields': F}).status_code == 400


# ── Talent Pool segmentation ───────────────────────────────────────────────
def test_segments_rules(srv):
    import modules.segments as sg
    it = sg.classify(dict(title='automation test engineer', job='', skills='selenium java jira sql', tags='',
                          company='accenture', summary='test automation'))
    assert it[0] == []                                           # software tester is not Industrial Automation
    dc = sg.classify(dict(title='electrical engineer', job='', skills='ups, lt panels', tags='', company='equinix', summary=''))
    assert 'data_center' in dc[0] and 'electrical' in dc[0]
    qa = sg.classify(dict(title='qa/qc engineer', job='solar qa qc', skills='inspection, audit, certification', tags='',
                          company='sterling and wilson solar', summary='inspection of pv modules'))
    assert qa[0] == ['solar_re']                                 # generic "inspection" no longer means TIC
    plc = sg.classify(dict(title='automation engineer', job='', skills='plc scada', tags='', company='', summary=''))
    assert plc[0] == ['automation']


def test_segments_scan_marks_switched_off_people_not_emailable(srv):
    import modules.segments as sg
    a = cl(srv, ADMIN)
    a.post(f'/api/mandates/{M_OFF}/auto-mail', json={'off': True})
    conn = srv.get_db()
    try:
        sg.scan_company(conn, CO)
        r = conn.execute("SELECT emailable FROM talent_profiles WHERE company_id=? AND person_key='ravi@ex.com'", (CO,)).fetchone()
        assert r['emailable'] == 0
        r = conn.execute("SELECT emailable FROM talent_profiles WHERE company_id=? AND person_key='asha@ex.com'", (CO,)).fetchone()
        assert r['emailable'] == 1
    finally:
        conn.close()
    a.post(f'/api/mandates/{M_OFF}/auto-mail', json={'off': False})


def test_final_result_build_end_to_end(srv, monkeypatch):
    from modules import pitch as P
    conn = srv.get_db()
    try:
        P._ensure_schema(conn)
        ev = {'match_score': 60, 'must_have_check': [{'requirement': 'O&M of solar plants', 'status': 'UNCLEAR', 'evidence': ''}]}
        conn.execute(f'INSERT INTO {P.PITCH_TABLE} (owner_id,candidate_id,mandate_id,lang,evaluation,pitch,created_at) '
                     'VALUES (?,?,?,?,?,?,?)', (CO, 928204, M_ON, 'en', json.dumps(ev), '{}', srv.ts()))
        conn.execute("INSERT INTO candidate_call_analysis (owner_id,candidate_id,mandate_id,analysis,created_at) VALUES (?,?,?,?,?)",
                     (CO, 928204, M_ON, json.dumps({'interest_level': 'HIGH', 'recommendation': 'PROCEED'}), srv.ts()))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(srv, 'get_setting', lambda k, d='': 'key' if k == 'deepseek_api_key' else d)
    monkeypatch.setattr(P, '_ask_json', lambda *a, **k: ({'rows': [{'requirement': 'O&M of solar plants', 'final_status': 'MET',
                                                                     'call_evidence': 'Ran 50 MW plant O&M for 3 yrs'}],
                                                           'final_score': 78, 'summary': 's'}, ''))
    a = cl(srv, ADMIN)
    g = a.get('/api/candidates/928204/final-result').get_json()
    assert g['have'] == {'pitch': True, 'call': True} and g['result'] is None
    r = a.post('/api/candidates/928204/final-result').get_json()
    assert r['ok'] and r['result']['decision']['status'] == 'shortlisted'
    g = a.get('/api/candidates/928204/final-result').get_json()
    assert g['result']['rows'][0]['status'] == 'MET' and g['stale'] is False
