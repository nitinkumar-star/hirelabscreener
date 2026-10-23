"""
HireLab Assistant — Central Database push + Find job (Oct 2026)

Run:   pytest test_assistant_jobs.py -q
Uses the same real server / fixtures as test_assistant.py.
"""
import array

from test_naukri_screen import srv, ds, cl, CO, ADMIN, RIYA, FL, M_A, M_R  # noqa: F401  (fixtures)


def _blob(v):
    return array.array('f', v).tobytes()


def _set_vec(srv, mid, vec):
    conn = srv.get_db()
    conn.execute('DELETE FROM mandate_vectors WHERE mandate_id=?', (mid,))
    conn.execute("INSERT INTO mandate_vectors (mandate_id, embedding_vec, status) VALUES (?,?, 'completed')", (mid, _blob(vec)))
    conn.commit(); conn.close()


# ── Central Database ─────────────────────────────────────────────────────
def test_push_to_central_and_dupes(srv):
    c = cl(srv, ADMIN)
    r = c.post('/api/extension/push', json={'mandate_id': 'central', 'name': 'Pool Person', 'phone': '9876500011',
                                            'email': 'pool.person@example.com', 'company': 'Tata Power'}).get_json()
    assert r['ok'] and r['action'] == 'added' and r['central'] is True
    conn = srv.get_db()
    row = conn.execute('SELECT c.mandate_id, c.stage, m.status FROM candidates c JOIN mandates m ON m.id=c.mandate_id '
                       'WHERE c.id=?', (r['candidate_id'],)).fetchone()
    conn.close()
    assert row['status'] == 'central' and row['mandate_id'] == r['mandate_id'] and row['stage'] == srv.CENTRAL_STAGE

    # the extension's duplicate check accepts "central" and reports the pool copy as "this job"
    j = c.post('/api/assistant/dupes', json={'mandate_id': 'central', 'name': 'P. Person', 'phone': '9876500011'}).get_json()
    assert j['ok'] and j['mandate_id'] == r['mandate_id']
    assert j['hits'][0]['candidate_id'] == r['candidate_id'] and j['hits'][0]['where'] == 'this_job'

    # same person checked against a real job -> shown as Central DB ("pool")
    j = c.post('/api/assistant/dupes', json={'mandate_id': M_A, 'name': 'P. Person', 'phone': '9876500011'}).get_json()
    assert j['hits'][0]['where'] == 'pool'

    # pushing again with the same e-mail updates, and still says where it lives
    r2 = c.post('/api/extension/push', json={'mandate_id': 'central', 'name': 'Pool Person', 'phone': '9876500011',
                                             'email': 'pool.person@example.com', 'company': 'Adani Green'}).get_json()
    assert r2['action'] == 'updated' and r2['candidate_id'] == r['candidate_id'] and r2['mandate_id'] == r['mandate_id']


def test_freelancer_cannot_push_to_central(srv):
    r = cl(srv, FL).post('/api/extension/push', json={'mandate_id': 'central', 'name': 'X', 'phone': '9876500012'})
    assert r.status_code == 403


# ── Find job ─────────────────────────────────────────────────────────────
def test_jobs_list_without_text(srv):
    j = cl(srv, ADMIN).post('/api/assistant/jobs', json={}).get_json()
    assert j['ok'] and j['ai'] is False
    ids = [x['id'] for x in j['jobs']]
    assert M_A in ids and M_R in ids
    a = next(x for x in j['jobs'] if x['id'] == M_A)
    assert a['role'] == 'Solar O&M Manager' and a['exp_min'] == 6 and a['ctc_max'] == 14 and 'O&M' in a['jd']
    assert a['score'] is None and isinstance(a['pipeline'], int)
    # central pool never appears as a job
    assert all(x['role'] != 'Central Database' for x in j['jobs'])


def test_jobs_scoped_recruiter_sees_only_assigned(srv):
    j = cl(srv, RIYA).post('/api/assistant/jobs', json={}).get_json()
    assert [x['id'] for x in j['jobs']] == [M_R]


def test_jobs_ai_ranking(srv, monkeypatch):
    _set_vec(srv, M_A, [1.0, 0.0, 0.0])
    _set_vec(srv, M_R, [0.0, 1.0, 0.0])
    real = srv.get_setting
    monkeypatch.setattr(srv, 'get_setting', lambda k, default='': 'emb-key' if k == 'embedding_api_key' else real(k, default))
    seen = {}

    def fake_embed(text):
        seen['text'] = text
        return [0.2, 0.98, 0.0]
    monkeypatch.setattr(srv, 'embed_long', fake_embed)
    j = cl(srv, ADMIN).post('/api/assistant/jobs', json={'text': 'Riya-type candidate, call 9811122334'}).get_json()
    assert j['ok'] and j['ai'] is True
    top = j['jobs'][0]
    assert top['id'] == M_R and top['score'] > 90
    assert next(x for x in j['jobs'] if x['id'] == M_A)['score'] < 30
    assert '9811122334' not in seen['text']      # phone numbers are redacted before embedding


def test_jobs_ai_unavailable_falls_back(srv, monkeypatch):
    _set_vec(srv, M_A, [1.0, 0.0, 0.0])
    real = srv.get_setting
    monkeypatch.setattr(srv, 'get_setting', lambda k, default='': '' if k == 'embedding_api_key' else real(k, default))
    j = cl(srv, ADMIN).post('/api/assistant/jobs', json={'text': 'some profile text'}).get_json()
    assert j['ok'] and j['ai'] is False and 'Embedding API key' in j['ai_note'] and j['jobs']


def test_jobs_requires_login_and_blocks_freelancer(srv):
    assert srv.app.test_client().post('/api/assistant/jobs', json={}).status_code == 401
    assert cl(srv, FL).post('/api/assistant/jobs', json={}).status_code in (401, 403, 404)
