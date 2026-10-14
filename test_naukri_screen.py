"""
Naukri search screening — modules/naukri_screen.py

Run:   pytest test_naukri_screen.py -q
Real server.py, real migrations, real access guards. DeepSeek is replaced by a
fake that records what was sent, so we can check redaction and the prompt.
"""
import os
import sys
import json
import tempfile

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CO, OTHER = 971, 972
ADMIN, RIYA, OADMIN, FL = 9701, 9702, 9703, 9704
M_A, M_R, M_O = 97101, 97102, 97103


class FakeResp:
    def __init__(self, obj, status=200):
        self.status_code = status
        self._obj = obj

    def json(self):
        return {'choices': [{'message': {'content': json.dumps(self._obj)}}], 'usage': {}}


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='screen_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db(); c = conn.cursor(); now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", list(kw.values()))

    ins('companies', id=CO, name='Screen Co', status='active', created_at=now)
    ins('companies', id=OTHER, name='Other Co', status='active', created_at=now)
    for uid, un, co, adm, role in ((ADMIN, 'sc_admin', CO, 1, 'user'), (RIYA, 'sc_riya', CO, 0, 'user'),
                                   (OADMIN, 'sc_other', OTHER, 1, 'user'), (FL, 'sc_fl', CO, 0, 'freelancer_sourcer')):
        ins('users', id=uid, username=un, password_hash='x', display_name=un, role=role, status='approved',
            company_id=co, is_company_admin=adm, created_at=now)
    for mid, role, who, co in ((M_A, 'Solar O&M Manager', ADMIN, CO), (M_R, 'Riya Job', RIYA, CO),
                               (M_O, 'Other Job', OADMIN, OTHER)):
        ins('mandates', id=mid, role=role, client='Client ' + role, status='active', owner_id=co,
            assigned_user_id=who, created_at=now, jd=role + ' — runs solar plant O&M', ctc_min=8, ctc_max=14,
            exp_min=6, exp_max=10)
    c.execute("INSERT OR IGNORE INTO mandate_assignees (company_id, mandate_id, user_id, is_primary, added_by, added_at, is_active) "
              "VALUES (?,?,?,1,?,?,1)", (CO, M_R, RIYA, RIYA, now))
    conn.commit(); conn.close()
    yield server


@pytest.fixture
def ds(srv, monkeypatch):
    """Fake DeepSeek. Set .answer to what it should reply; .calls records payloads."""
    class D:
        calls = []
        answer = None

        def __call__(self, key, payload, timeout=60, endpoint='deepseek'):
            self.calls.append({'key': key, 'payload': payload, 'endpoint': endpoint})
            a = self.answer(payload) if callable(self.answer) else self.answer
            return FakeResp(a)
    d = D()
    d.calls = []
    monkeypatch.setattr(srv, 'call_deepseek', d)
    real = srv.get_setting
    monkeypatch.setattr(srv, 'get_setting', lambda k, default='': 'test-key' if k == 'deepseek_api_key' else real(k, default))
    return d


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


def echo_answer(payload):
    """Verdict per card, derived from its text, so the mapping can be checked."""
    user = payload['messages'][-1]['content']
    out = []
    for block in user.split('[key: ')[1:]:
        key = block.split(']')[0]
        v = 'open' if 'solar' in block.lower() else 'skip'
        out.append({'key': key, 'verdict': v, 'score': 80 if v == 'open' else 20, 'reason': 'r ' + key})
    return {'results': out}


# ── pure helpers ─────────────────────────────────────────────────────────
def test_redact_strips_contacts():
    from modules.naukri_screen import redact
    t = redact('Call me on +91 98765 43210 or 9876543210, mail a.b@x.co.in. Exp 8 yrs, CTC 12.5 Lacs')
    assert '98765' not in t and '9876543210' not in t and 'a.b@x' not in t
    assert '8 yrs' in t and '12.5 Lacs' in t


def test_clean_rules_dedupes_and_strips_bullets():
    from modules.naukri_screen import clean_rules
    assert clean_rules(['- Skip EPC', '1. Skip EPC', '', '• Min 6 yrs']) == ['Skip EPC', 'Min 6 yrs']
    assert clean_rules('a\n\nb') == ['a', 'b']


def test_normalize_results_marks_missing_unknown():
    from modules.naukri_screen import normalize_results
    r = normalize_results({'results': [{'key': 'a', 'verdict': 'OPEN', 'score': 150, 'reason': 'x'},
                                       {'key': 'zzz', 'verdict': 'open'},
                                       {'key': 'c', 'score': 30}]}, ['a', 'b', 'c'])
    assert [x['verdict'] for x in r] == ['open', 'unknown', 'skip']
    assert r[0]['score'] == 100


# ── routes ───────────────────────────────────────────────────────────────
def test_requires_login(srv):
    c = srv.app.test_client()
    assert c.get('/api/extension/screen/state?mandate_id=%d' % M_A).status_code == 401
    assert c.post('/api/extension/screen', json={'mandate_id': M_A, 'profiles': []}).status_code == 401
    assert c.open('/api/extension/screen', method='OPTIONS').status_code == 204


def test_screen_maps_results_and_redacts(srv, ds):
    ds.answer = echo_answer
    c = cl(srv, ADMIN)
    profiles = [{'key': 'k1', 'name': 'Asha', 'text': 'Asha — Solar O&M lead, 8 yrs, 12 Lacs. Ph 9876543210'},
                {'key': 'k2', 'name': 'Ravi', 'text': 'Ravi — Civil site engineer for highways, 7 yrs'}]
    r = c.post('/api/extension/screen', json={'mandate_id': M_A, 'profiles': profiles})
    j = r.get_json()
    assert r.status_code == 200 and j['ok'], j
    assert [(x['key'], x['verdict']) for x in j['results']] == [('k1', 'open'), ('k2', 'skip')]
    sent = json.dumps(ds.calls[-1]['payload'])
    assert '9876543210' not in sent
    sysmsg = ds.calls[-1]['payload']['messages'][0]['content']
    assert '14 LPA' in sysmsg and 'Solar O&M Manager' in sysmsg and '6-10 years' in sysmsg
    assert ds.calls[-1]['endpoint'] == 'naukri-screen'


def test_screen_limits_batch_size(srv, ds):
    ds.answer = echo_answer
    c = cl(srv, ADMIN)
    many = [{'key': 'k%d' % i, 'text': 'solar engineer profile text %d' % i} for i in range(13)]
    assert c.post('/api/extension/screen', json={'mandate_id': M_A, 'profiles': many}).status_code == 400
    assert not ds.calls


def test_chat_saves_rules_per_mandate_and_next_screen_uses_them(srv, ds):
    c = cl(srv, ADMIN)
    ds.answer = {'reply': 'Theek hai, EPC wale skip karunga.', 'rules': ['Skip candidates from EPC contractors'],
                 'changed': True}
    r = c.post('/api/extension/screen/chat', json={'mandate_id': M_A, 'message': 'EPC wale mat dikhao'})
    j = r.get_json()
    assert j['ok'] and j['changed'] and j['rules'] == ['Skip candidates from EPC contractors']
    st = c.get('/api/extension/screen/state?mandate_id=%d' % M_A).get_json()
    assert st['rules'] == ['Skip candidates from EPC contractors']
    assert [m['role'] for m in st['chat']][-2:] == ['me', 'ai']
    # the rule reaches the next analysis
    ds.answer = echo_answer
    c.post('/api/extension/screen', json={'mandate_id': M_A, 'profiles': [{'key': 'x', 'text': 'solar person with 9 yrs O&M'}]})
    assert 'Skip candidates from EPC contractors' in ds.calls[-1]['payload']['messages'][0]['content']
    # ...but not another mandate's
    cl(srv, RIYA).post('/api/extension/screen', json={'mandate_id': M_R, 'profiles': [{'key': 'x', 'text': 'solar person with 9 yrs O&M'}]})
    assert 'Skip candidates from EPC contractors' not in ds.calls[-1]['payload']['messages'][0]['content']


def test_question_does_not_wipe_rules(srv, ds):
    c = cl(srv, ADMIN)
    before = c.get('/api/extension/screen/state?mandate_id=%d' % M_A).get_json()['rules']
    assert before
    ds.answer = {'reply': 'Because he is in civil.', 'rules': [], 'changed': False}
    j = c.post('/api/extension/screen/chat', json={'mandate_id': M_A, 'message': 'Ravi ko skip kyun kiya?',
                                                    'context': [{'name': 'Ravi', 'verdict': 'skip', 'reason': 'civil'}]}).get_json()
    assert j['rules'] == before and j['changed'] is False
    assert 'Ravi' in ds.calls[-1]['payload']['messages'][0]['content']


def test_manual_rules_edit_and_clear(srv):
    c = cl(srv, ADMIN)
    j = c.post('/api/extension/screen/rules', json={'mandate_id': M_A, 'rules': ['Min 6 yrs', 'Min 6 yrs', '']}).get_json()
    assert j['rules'] == ['Min 6 yrs']
    j = c.post('/api/extension/screen/rules', json={'mandate_id': M_A, 'rules': [], 'clear_chat': True}).get_json()
    assert j['rules'] == [] and j['chat'] == []


def test_recruiter_cannot_use_unassigned_mandate(srv, ds):
    ds.answer = echo_answer
    c = cl(srv, RIYA)
    assert c.get('/api/extension/screen/state?mandate_id=%d' % M_A).status_code in (403, 404)
    assert c.post('/api/extension/screen', json={'mandate_id': M_A, 'profiles': [{'key': 'a', 'text': 'solar O&M engineer 9 yrs ok'}]}).status_code in (403, 404)
    assert c.post('/api/extension/screen/chat', json={'mandate_id': M_A, 'message': 'hi'}).status_code in (403, 404)
    assert c.post('/api/extension/screen/rules', json={'mandate_id': M_A, 'rules': ['x']}).status_code in (403, 404)
    assert not ds.calls
    assert c.get('/api/extension/screen/state?mandate_id=%d' % M_R).status_code == 200


def test_other_company_mandate_is_invisible(srv, ds):
    c = cl(srv, ADMIN)
    assert c.get('/api/extension/screen/state?mandate_id=%d' % M_O).status_code in (403, 404)
    assert c.post('/api/extension/screen/rules', json={'mandate_id': M_O, 'rules': ['x']}).status_code in (403, 404)


def test_freelancer_blocked(srv):
    c = cl(srv, FL)
    assert c.get('/api/extension/screen/state?mandate_id=%d' % M_A).status_code == 403


def test_ai_failure_is_reported_not_crashed(srv, ds):
    ds.answer = 'not json at all'
    c = cl(srv, ADMIN)
    r = c.post('/api/extension/screen', json={'mandate_id': M_A, 'profiles': [{'key': 'a', 'text': 'solar O&M engineer 9 yrs okay'}]})
    j = r.get_json()
    # a non-object answer maps every key to "unknown" rather than inventing a verdict
    assert r.status_code == 200 and j['results'][0]['verdict'] == 'unknown'


# ── duplicates on a search page ──────────────────────────────────────────
def _seed_dupes(srv):
    conn = srv.get_db(); now = srv.ts()
    conn.execute("INSERT OR REPLACE INTO mandates (id, role, client, status, owner_id, assigned_user_id, created_at) "
                 "VALUES (97109, 'Old Wind Job', 'Suzlon', 'closed', ?, ?, ?)", (CO, ADMIN, now))
    rows = [(97201, M_A, 'Asha Verma', 'Azure Power', 'Solar O&M Engineer', 'Interested', CO),
            (97202, 97109, 'Ravi Kumar', 'L&T Construction', 'Site Engineer', 'Rejected', CO),
            (97203, M_A, 'Rahul Mehta', 'Tata Power', 'Engineer', 'Screening', CO),
            (97204, M_R, 'Neha Gupta', 'Mahindra Susten', 'O&M Engineer', 'Screening', CO),
            (97205, M_O, 'Meena Joshi', 'Sterling Wilson', 'Senior Engineer', 'Screening', OTHER)]
    for cid, mid, name, comp, des, stage, co in rows:
        conn.execute("INSERT OR REPLACE INTO candidates (id, mandate_id, name, company, designation, stage, owner_id, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?)", (cid, mid, name, comp, des, stage, co, now))
    conn.execute("UPDATE candidates SET naukri_ref='ref-xyz' WHERE id=97203")
    conn.commit(); conn.close()


def test_dupes_found_by_name_and_company_and_ref(srv):
    _seed_dupes(srv)
    c = cl(srv, ADMIN)
    cards = [{'key': 'a', 'name': 'Asha  Verma', 'text': 'Solar O&M Engineer at Azure Power 8 yrs 12 Lacs'},
             {'key': 'b', 'name': 'Ravi Kumar', 'text': 'Site Engineer at L&T Construction'},
             {'key': 'c', 'name': 'R. Mehta', 'text': 'something', 'ref': 'ref-xyz'},
             {'key': 'd', 'name': 'Asha Verma', 'text': 'Accountant at Infosys'},
             {'key': 'e', 'name': 'Meena Joshi', 'text': 'Senior Engineer at Sterling Wilson'},
             {'key': 'f', 'name': 'Nobody Here', 'text': 'x'}]
    j = c.post('/api/extension/screen/dupes', json={'mandate_id': M_A, 'cards': cards}).get_json()
    got = {x['key']: x['best'] for x in j['dupes']}
    assert got['a']['where'] == 'this_job' and got['a']['level'] == 'strong' and got['a']['stage'] == 'Interested'
    assert got['b']['where'] == 'other_job' and got['b']['job'] == 'Old Wind Job'
    assert got['c']['level'] == 'exact' and got['c']['candidate_id'] == 97203
    assert got['d']['level'] == 'name'                      # same name, different company: only "check"
    assert 'e' not in got and 'f' not in got                # other company's candidate is invisible


def test_dupes_respect_recruiter_scope(srv):
    _seed_dupes(srv)
    j = cl(srv, RIYA).post('/api/extension/screen/dupes', json={'mandate_id': M_R, 'cards': [
        {'key': 'a', 'name': 'Asha Verma', 'text': 'Azure Power'},
        {'key': 'n', 'name': 'Neha Gupta', 'text': 'O&M Engineer at Mahindra Susten'}]}).get_json()
    got = {x['key']: x['best'] for x in j['dupes']}
    assert 'a' not in got                                   # Riya is not on M_A
    assert got['n']['where'] == 'this_job'


def test_push_saves_naukri_ref(srv):
    c = cl(srv, ADMIN)
    r = c.post('/api/extension/push', json={'mandate_id': M_A, 'name': 'Pushed Person', 'phone': '9811177777',
                                            'company': 'X', 'naukri_ref': 'abc123'}).get_json()
    assert r.get('ok'), r
    conn = srv.get_db()
    assert conn.execute('SELECT naukri_ref FROM candidates WHERE id=?', (r['candidate_id'],)).fetchone()[0] == 'abc123'
    conn.close()


def test_dupes_no_false_positives(srv):
    """A different person must not be called "already in this job":
    same name + a role word, scattered company words, or a shared/reused profile ref."""
    _seed_dupes(srv)
    c = cl(srv, ADMIN)
    cards = [{'key': 'role', 'name': 'Asha Verma', 'text': 'Solar O&M Engineer at Adani Green Energy 5 yrs'},
             {'key': 'scat', 'name': 'Rahul Mehta', 'text': 'Engineer at Adani Power, earlier with Tata Projects'},
             {'key': 'ref', 'name': 'Kavita Rao', 'text': 'Sales Manager at Waaree', 'ref': 'ref-xyz'},
             {'key': 'ok', 'name': 'Rahul Mehta', 'text': 'Engineer at Tata Power Delhi', 'ref': ''},
             {'key': 'ok2', 'name': 'R Mehta', 'text': 'x', 'ref': 'ref-xyz'}]
    j = c.post('/api/extension/screen/dupes', json={'mandate_id': M_A, 'cards': cards}).get_json()
    got = {x['key']: x['best'] for x in j['dupes']}
    assert got['role']['level'] == 'name'                   # same name + same role word is NOT proof
    assert got['scat']['level'] == 'name'                   # "Tata" and "Power" in different places
    assert 'ref' not in got                                 # a ref on a different name is not a match
    assert got['ok']['level'] == 'strong'
    assert got['ok2']['level'] == 'exact'
