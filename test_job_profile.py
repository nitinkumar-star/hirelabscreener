"""
Job Breakdown + extension explanation — modules/job_profile.py

Run:   pytest test_job_profile.py -q
Real server.py, real migrations and access guards; DeepSeek is faked.
"""
import os
import sys
import json
import tempfile

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CO, OTHER = 981, 982
ADMIN, RIYA, OADMIN, FL = 9801, 9802, 9803, 9804
M_A, M_R, M_O, M_EMPTY, M_FL = 98101, 98102, 98103, 98104, 98105

BREAKDOWN = {
    'summary': 'Runs O&M for utility-scale solar plants.',
    'core': [{'skill': 'Utility-scale solar plant O&M', 'why': 'the job', 'equivalents': ['solar O&M']},
             {'skill': 'SCADA monitoring', 'why': 'daily work', 'equivalents': []}],
    'important': [{'skill': 'HT switchyard maintenance', 'why': 'plant has 33kV yard'}],
    'nice': [{'skill': 'Module cleaning robots'}],
    'limits': {'locations': ['Rajasthan'], 'exp_min': 1, 'ctc_max': 99},
    'not_this': ['Rooftop solar sales is not plant O&M'],
}

RESUME = ('Asha Verma. Managing O&M of 150 MW solar plants for Azure Power since 2018. '
          'SCADA monitoring and inverter troubleshooting every shift. Module cleaning robots rollout.')
PROFILE = 'Candidate: Asha Verma\nCurrent Role: Solar O&M Engineer\nExperience: 8 years\nKey Skills: Solar O&M, SCADA'

EXPLAIN = {
    'summary': 'Eight years running utility-scale solar O&M with SCADA.',
    'requirements': [{'id': 'C1', 'status': 'cv', 'evidence': 'Managing O&M of 150 MW solar plants'},
                     {'id': 'C2', 'status': 'cv', 'evidence': 'SCADA monitoring and inverter troubleshooting'},
                     {'id': 'I1', 'status': 'missing', 'evidence': ''}],
    'nice_found': ['Module cleaning robots', 'Made-up thing'],
    'checks': {'experience': {'value': '8 yrs', 'status': 'ok'}, 'ctc': {'value': '12.5 LPA', 'status': 'ok'},
               'location': {'value': 'Jaipur', 'status': 'ok'}},
    'fit': 'strong', 'confidence': 85,
    'risks': ['No HT yard exposure'], 'questions': ['Have you handled 33kV switchyard?'],
}


class FakeResp:
    status_code = 200

    def __init__(self, obj):
        self._obj = obj

    def json(self):
        c = self._obj if isinstance(self._obj, str) else json.dumps(self._obj)
        return {'choices': [{'message': {'content': c}}], 'usage': {}}


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='jobprof_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db(); c = conn.cursor(); now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", list(kw.values()))

    ins('companies', id=CO, name='JP Co', status='active', created_at=now)
    ins('companies', id=OTHER, name='Other Co', status='active', created_at=now)
    for uid, un, co, adm, role in ((ADMIN, 'jp_admin', CO, 1, 'user'), (RIYA, 'jp_riya', CO, 0, 'user'),
                                   (OADMIN, 'jp_other', OTHER, 1, 'user'), (FL, 'jp_fl', CO, 0, 'freelancer_sourcer')):
        ins('users', id=uid, username=un, password_hash='x', display_name=un, role=role, status='approved',
            company_id=co, is_company_admin=adm, created_at=now)
    for mid, role, who, co, jd in ((M_A, 'Solar O&M Manager', ADMIN, CO, 'Run O&M of 200 MW solar plants.'),
                                   (M_R, 'Riya Job', RIYA, CO, 'Riya JD'),
                                   (M_O, 'Other Job', OADMIN, OTHER, 'Other JD'),
                                   (M_EMPTY, 'No JD', ADMIN, CO, ''),
                                   (M_FL, 'FL Job', ADMIN, CO, 'FL JD')):
        ins('mandates', id=mid, role=role, client='Client', status='active', owner_id=co, assigned_user_id=who,
            created_at=now, jd=jd, ctc_min=8, ctc_max=14, exp_min=6, exp_max=10, location='Jaipur')
    c.execute("INSERT OR IGNORE INTO mandate_assignees (company_id, mandate_id, user_id, is_primary, added_by, added_at, is_active) "
              "VALUES (?,?,?,1,?,?,1)", (CO, M_R, RIYA, RIYA, now))
    try:
        c.execute("INSERT INTO mandate_freelancers (company_id, mandate_id, freelancer_user_id, is_active) VALUES (?,?,?,1)",
                  (CO, M_FL, FL))
    except Exception:
        pass
    conn.commit(); conn.close()
    yield server


@pytest.fixture
def ds(srv, monkeypatch):
    class D:
        def __init__(self):
            self.calls = []

        def __call__(self, key, payload, timeout=60, endpoint='deepseek'):
            self.calls.append({'payload': payload, 'endpoint': endpoint})
            if endpoint == 'job-breakdown':
                return FakeResp(BREAKDOWN)
            if endpoint == 'profile-explain':
                return FakeResp(self.explain)
            return FakeResp({})
    d = D()
    d.explain = EXPLAIN
    monkeypatch.setattr(srv, 'call_deepseek', d)
    real = srv.get_setting
    monkeypatch.setattr(srv, 'get_setting', lambda k, default='': 'k' if k == 'deepseek_api_key' else real(k, default))
    return d


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


# ── pure ─────────────────────────────────────────────────────────────────
def test_clean_profile_mandate_limits_win_and_tiers_are_exclusive(srv):
    from modules.job_profile import clean_profile
    p = clean_profile({'core': ['A', 'B'], 'important': ['a', 'C'], 'nice': ['C', 'D'],
                       'limits': {'exp_min': 2, 'ctc_max': 99}},
                      {'exp_min': 6, 'exp_max': 10, 'ctc_max': 14, 'location': 'Jaipur, Pune'})
    assert [i['skill'] for i in p['core']] == ['A', 'B']
    assert [i['skill'] for i in p['important']] == ['C']
    assert [i['skill'] for i in p['nice']] == ['D']
    assert p['limits']['exp_min'] == 6 and p['limits']['ctc_max'] == 14
    assert p['limits']['locations'] == ['Jaipur', 'Pune']


def test_blend_matches_server_judge_and_caps_core_missing(srv):
    from modules.job_profile import blend
    r = blend(90, 'strong', 85, [])
    assert r['verdict'] == 'Strong Match' and not r['caps']
    r = blend(90, 'strong', 85, ['SCADA'])
    assert r['score'] == 74.0 and r['verdict'] == 'Good Fit' and 'SCADA' in r['caps'][0]
    r = blend(90, 'mismatch', 80, [])
    assert r['score'] == 34.0 and r['verdict'] == 'Not Suitable'


def test_quote_check(srv):
    from modules.job_profile import quote_found
    assert quote_found('Managing O&M of 150 MW solar plants', RESUME)
    assert not quote_found('Led a 2 GW wind farm commissioning', RESUME)


# ── breakdown routes ─────────────────────────────────────────────────────
def test_breakdown_generates_once_and_is_cached(srv, ds):
    c = cl(srv, ADMIN)
    j = c.get('/api/job-profile/%d' % M_A).get_json()
    assert j['ok'] and j['profile'] is None and j['status'] == 'none'          # no generation without ensure
    j = c.get('/api/job-profile/%d?ensure=1' % M_A).get_json()
    assert j['status'] == 'ai' and j['profile']['core'][0]['skill'] == 'Utility-scale solar plant O&M'
    assert j['profile']['limits']['ctc_max'] == 14                              # mandate field beat the AI's 99
    n = len(ds.calls)
    c.get('/api/job-profile/%d?ensure=1' % M_A)
    assert len(ds.calls) == n                                                   # cached
    sysmsg = ds.calls[0]['payload']['messages'][1]['content']
    assert 'Run O&M of 200 MW solar plants.' in sysmsg


def test_jd_change_regenerates_ai_but_keeps_edited(srv, ds):
    c = cl(srv, ADMIN)
    c.get('/api/job-profile/%d?ensure=1' % M_A)
    conn = srv.get_db(); conn.execute("UPDATE mandates SET jd='Run O&M of 300 MW solar plants.' WHERE id=?", (M_A,)); conn.commit(); conn.close()
    n = len(ds.calls)
    c.get('/api/job-profile/%d?ensure=1' % M_A)
    assert len(ds.calls) == n + 1                                               # AI one refreshed
    edited = dict(BREAKDOWN, core=[{'skill': 'My own core item'}])
    j = c.put('/api/job-profile/%d' % M_A, json={'profile': edited}).get_json()
    assert j['status'] == 'edited' and j['profile']['core'][0]['skill'] == 'My own core item'
    conn = srv.get_db(); conn.execute("UPDATE mandates SET jd='Totally new JD for O&M.' WHERE id=?", (M_A,)); conn.commit(); conn.close()
    n = len(ds.calls)
    j = c.get('/api/job-profile/%d?ensure=1' % M_A).get_json()
    assert len(ds.calls) == n and j['status'] == 'edited' and j['stale'] is True
    j = c.post('/api/job-profile/%d/generate' % M_A).get_json()
    assert j['status'] == 'ai' and j['stale'] is False


def test_empty_breakdown_rejected_and_no_jd_mandate(srv, ds):
    c = cl(srv, ADMIN)
    assert c.put('/api/job-profile/%d' % M_A, json={'profile': {'core': []}}).status_code == 400
    j = c.get('/api/job-profile/%d?ensure=1' % M_EMPTY).get_json()
    assert j['has_job_content'] is False and j['profile'] is None
    assert c.post('/api/job-profile/%d/generate' % M_EMPTY).status_code == 400


def test_breakdown_access(srv, ds):
    assert cl(srv, RIYA).get('/api/job-profile/%d' % M_A).status_code in (403, 404)
    assert cl(srv, RIYA).put('/api/job-profile/%d' % M_A, json={'profile': BREAKDOWN}).status_code in (403, 404)
    assert cl(srv, RIYA).get('/api/job-profile/%d' % M_R).status_code == 200
    assert cl(srv, ADMIN).get('/api/job-profile/%d' % M_O).status_code == 404
    assert srv.app.test_client().get('/api/job-profile/%d' % M_A).status_code == 401


# ── explanation ──────────────────────────────────────────────────────────
def test_explain_verifies_quotes_and_blends(srv, ds):
    c = cl(srv, ADMIN)
    c.post('/api/job-profile/%d/generate' % M_A)
    r = c.post('/api/extension/explain', json={'mandate_id': M_A, 'resume_text': RESUME,
                                               'candidate_text': PROFILE, 'base_score': 88})
    j = r.get_json()
    assert r.status_code == 200 and j['ok'], j
    ex = j['explanation']
    assert [x['status'] for x in ex['core']] == ['cv', 'cv'] and all(x['verified'] for x in ex['core'])
    assert ex['important'][0]['status'] == 'missing'
    assert ex['nice_found'] == ['Module cleaning robots']                    # invented item dropped
    assert j['final']['verdict'] == 'Strong Match' and j['breakdown']['core']
    assert ds.calls[-1]['endpoint'] == 'profile-explain'


def test_explain_fake_cv_quote_is_not_trusted_and_core_missing_caps(srv, ds):
    ds.explain = dict(EXPLAIN, requirements=[
        {'id': 'C1', 'status': 'cv', 'evidence': 'Led a 2 GW wind farm commissioning programme'},
        {'id': 'C2', 'status': 'missing', 'evidence': ''}])
    j = cl(srv, ADMIN).post('/api/extension/explain', json={'mandate_id': M_A, 'resume_text': RESUME,
                                                            'candidate_text': PROFILE, 'base_score': 95}).get_json()
    assert j['explanation']['core'][0]['verified'] is False
    assert j['explanation']['core_missing'] == ['SCADA monitoring']
    assert j['final']['score'] <= 74 and j['final']['caps']


def test_explain_guards(srv, ds):
    assert srv.app.test_client().post('/api/extension/explain', json={'mandate_id': M_A}).status_code == 401
    j = cl(srv, ADMIN).post('/api/extension/explain', json={'mandate_id': M_EMPTY, 'resume_text': RESUME}).get_json()
    assert j['ok'] is False and j['kind'] == 'nojd'
    assert cl(srv, RIYA).post('/api/extension/explain', json={'mandate_id': M_A, 'resume_text': RESUME}).status_code in (403, 404)
    assert cl(srv, ADMIN).post('/api/extension/explain', json={'mandate_id': M_O, 'resume_text': RESUME}).status_code == 404


def test_freelancer_explain_only_on_assigned_mandate(srv, ds):
    c = cl(srv, FL)
    assert c.post('/api/extension/explain', json={'mandate_id': M_A, 'resume_text': RESUME}).status_code == 403
    r = c.post('/api/extension/explain', json={'mandate_id': M_FL, 'resume_text': RESUME, 'candidate_text': PROFILE})
    assert r.status_code == 200, r.get_json()
    assert c.get('/api/job-profile/%d' % M_FL).status_code == 403             # ATS page not for freelancers


def test_screener_prompt_carries_breakdown(srv, ds):
    c = cl(srv, ADMIN)
    c.post('/api/job-profile/%d/generate' % M_A)
    c.post('/api/extension/screen', json={'mandate_id': M_A, 'profiles': [{'key': 'a', 'text': 'Solar O&M engineer 9 yrs Jaipur'}]})
    sysmsg = [x for x in ds.calls if x['endpoint'] == 'naukri-screen'][-1]['payload']['messages'][0]['content']
    assert 'CORE (deal-breakers)' in sysmsg and 'Utility-scale solar plant O&M' in sysmsg
    assert 'Rooftop solar sales is not plant O&M' in sysmsg
