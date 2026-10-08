"""
Vector matching tabs — modules/matching.py

Run:   pytest test_matching.py -q
Real server.py, real migrations, real access guard. Vectors are tiny (3-d)
so the expected ranking is obvious: cosine similarity only, no keywords.
"""
import os
import sys
import json
import tempfile

import pytest
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CO, OTHER = 961, 962
ADMIN, RIYA, OADMIN = 9601, 9602, 9603
M_A, M_B, M_R, M_POOL, M_CLOSED, M_NOJD, M_O = 96101, 96102, 96103, 96104, 96105, 96106, 96107
C1, C2, C3, C4, C5, C6, C_R, C_O = 96201, 96202, 96203, 96204, 96205, 96206, 96207, 96208


def blob(v):
    return np.asarray(v, dtype=np.float32).tobytes()


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='match_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db(); c = conn.cursor(); now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", list(kw.values()))

    ins('companies', id=CO, name='Match Co', status='active', created_at=now)
    ins('companies', id=OTHER, name='Other Co', status='active', created_at=now)
    for uid, un, co, adm in ((ADMIN, 'mt_admin', CO, 1), (RIYA, 'mt_riya', CO, 0), (OADMIN, 'mt_other', OTHER, 1)):
        ins('users', id=uid, username=un, password_hash='x', display_name=un, role='user', status='approved',
            company_id=co, is_company_admin=adm, created_at=now)
    for mid, role, client, status, who, co in ((M_A, 'Solar Design Engineer', 'L&T', 'active', ADMIN, CO),
                                               (M_B, 'Sales Manager', 'Resolven', 'active', ADMIN, CO),
                                               (M_R, 'Riya Job', 'RiyaClient', 'active', RIYA, CO),
                                               (M_POOL, 'Central Database', 'save for later', 'central', ADMIN, CO),
                                               (M_CLOSED, 'Old Job', 'OldCo', 'closed', ADMIN, CO),
                                               (M_NOJD, 'No JD Job', 'X', 'active', ADMIN, CO),
                                               (M_O, 'Other Job', 'Other', 'active', OADMIN, OTHER)):
        ins('mandates', id=mid, role=role, client=client, status=status, owner_id=co, assigned_user_id=who,
            created_at=now, jd='' if mid == M_NOJD else role + ' JD')
    c.execute("INSERT OR REPLACE INTO tenant_settings (company_id, key, value) VALUES (?,?,?)",
              (CO, 'central_mandate_id', str(M_POOL)))
    for mid, v in ((M_A, [1, 0, 0]), (M_B, [0, 1, 0]), (M_R, [0, 0, 1]), (M_CLOSED, [1, 0, 0]), (M_O, [1, 0, 0]),
                   (M_NOJD, [0.1, 0.9, 0])):
        c.execute("INSERT OR REPLACE INTO mandate_vectors (mandate_id, embedding_vec, status) VALUES (?,?,'completed')",
                  (mid, blob(v)))

    def cand(cid, mid, name, stage, vec, phone, co=CO, cv=''):
        kw = dict(id=cid, mandate_id=mid, name=name, stage=stage, phone=phone, owner_id=co, created_at=now,
                  company='Co ' + name, designation='Engineer', key_skills='["solar"]', cv_path=cv,
                  ctc_current=10, recruiter_feedback='old feedback', placement_fee=50000)
        if vec:
            kw.update(embedding=json.dumps(vec), embedding_vec=blob(vec), embedding_status='completed',
                      embedding_text_version='candidate-template-v2')
        ins('candidates', **kw)

    cand(C1, M_B, 'Asha Active', 'Interested', [0.9, 0.1, 0], '9811100001', cv='asha.pdf')
    cand(C2, M_POOL, 'Pooja Pool', 'Central DB', [0.8, 0.2, 0], '9811100002')
    cand(C3, M_A, 'Already Here', 'Screening', [1, 0, 0], '9811100003')
    cand(C4, M_B, 'Nitesh NotSuitable', 'Not Suitable', [0.1, 0.9, 0], '9811100004')
    cand(C5, M_B, 'No Vector', 'Screening', None, '9811100005')
    cand(C6, M_CLOSED, 'Asha Duplicate', 'Placed', [0.9, 0.1, 0], '+91 98111 00001')   # same phone as C1
    cand(C_R, M_R, 'Riya Cand', 'Screening', [0.2, 0, 0.9], '9811100007')
    cand(C_O, M_O, 'Other Agency', 'Screening', [1, 0, 0], '9811100008', co=OTHER)
    ins('work_history', candidate_id=C1, company='Tata Power', designation='Design Engineer', is_current=1, sort_order=1)
    c.execute("INSERT OR IGNORE INTO mandate_assignees (company_id, mandate_id, user_id, is_primary, added_by, added_at, is_active) "
              "VALUES (?,?,?,1,?,?,1)", (CO, M_R, RIYA, RIYA, now))
    conn.commit(); conn.close()
    yield server


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


def test_mandate_matching_ranks_by_vector_and_dedupes(srv):
    a = cl(srv, ADMIN)
    r = a.get(f'/api/match/mandate/{M_A}/candidates').get_json()
    ids = [x['id'] for x in r['results']]
    assert C3 not in ids                                   # already in this job
    assert C_O not in ids                                  # other agency never
    assert C5 not in ids                                   # no vector -> cannot be ranked
    assert len({C1, C6} & set(ids)) == 1                   # same person (same phone) shown once
    assert ids.index(C2) > ids.index(C1 if C1 in ids else C6) and ids[-1] == C4   # cosine order
    by = {x['id']: x for x in r['results']}
    assert by[C2]['where'] == 'pool' and by[C2]['action'] == 'move'
    top = by.get(C1) or by.get(C6)
    assert top['action'] == 'copy' and top['score'] > by[C2]['score'] > by[C4]['score']
    assert by[C4]['where'] == 'dead'
    assert r['not_embedded'] >= 1
    # minimum match filter
    hi = a.get(f'/api/match/mandate/{M_A}/candidates?min=95').get_json()['results']
    assert C4 not in [x['id'] for x in hi]


def test_add_copies_or_moves(srv):
    a = cl(srv, ADMIN)
    r = a.post(f'/api/match/mandate/{M_A}/add', json={'candidate_ids': [C1, C2], 'scores': {str(C1): 99.4}}).get_json()
    st = {x['id']: x for x in r['results']}
    assert r['added'] == 2 and st[C1]['status'] == 'copied' and st[C2]['status'] == 'moved'
    conn = srv.get_db()
    new = conn.execute('SELECT * FROM candidates WHERE id=?', (st[C1]['new_id'],)).fetchone()
    orig = conn.execute('SELECT * FROM candidates WHERE id=?', (C1,)).fetchone()
    # the copy: same person, fresh pipeline state, vector + resume + history carried over
    assert new['mandate_id'] == M_A and new['stage'] == 'Screening' and new['copied_from'] == C1
    assert (new['name'], new['phone'], new['cv_path']) == (orig['name'], orig['phone'], 'asha.pdf')
    assert new['embedding_vec'] == orig['embedding_vec'] and new['embedding_status'] == 'completed'
    assert not new['recruiter_feedback'] and not new['placement_fee']
    assert conn.execute('SELECT company FROM work_history WHERE candidate_id=?', (new['id'],)).fetchone()[0] == 'Tata Power'
    note = conn.execute('SELECT note FROM stage_history WHERE candidate_id=?', (new['id'],)).fetchone()[0]
    assert '99.4% match' in note and 'Sales Manager' in note and 'Interested' in note
    # the original pipeline is untouched
    assert (orig['mandate_id'], orig['stage']) == (M_B, 'Interested')
    # pool candidate moved, starts at Screening
    p = conn.execute('SELECT mandate_id, stage FROM candidates WHERE id=?', (C2,)).fetchone()
    assert (p['mandate_id'], p['stage']) == (M_A, 'Screening')
    conn.close()
    # adding again (original, its copy, or the same-phone duplicate) does nothing
    again = a.post(f'/api/match/mandate/{M_A}/add', json={'candidate_ids': [C1, C6, st[C1]['new_id']]}).get_json()
    assert again['added'] == 0 and {x['status'] for x in again['results']} == {'already_in_job'}
    ids = [x['id'] for x in a.get(f'/api/match/mandate/{M_A}/candidates').get_json()['results']]
    assert not ({C1, C2, C6, st[C1]['new_id']} & set(ids))
    # the person is not offered back to job B either (their original lives there)
    ids_b = [x['id'] for x in a.get(f'/api/match/mandate/{M_B}/candidates').get_json()['results']]
    assert not ({C1, C6, st[C1]['new_id']} & set(ids_b))


def test_candidate_job_matching(srv):
    a = cl(srv, ADMIN)
    r = a.get(f'/api/match/candidate/{C4}/jobs').get_json()
    jobs = [x['mandate_id'] for x in r['results']]
    assert jobs[0] == M_B                                  # [0.1,0.9,0] fits the Sales JD best
    assert M_CLOSED not in jobs and M_POOL not in jobs and M_O not in jobs   # active, own agency only
    assert M_NOJD not in jobs                              # a job with no JD is never 'matched'
    assert {x['mandate_id']: x['in_this_job'] for x in r['results']}[M_B] is True
    r1 = a.get(f'/api/match/candidate/{C1}/jobs').get_json()
    flags = {x['mandate_id']: x['in_this_job'] for x in r1['results']}
    assert flags[M_A] is True and flags[M_B] is True       # copy in A, original in B
    assert r1['resume_in_vector'] is True
    # not embedded yet -> queued, friendly message
    p = a.get(f'/api/match/candidate/{C5}/jobs').get_json()
    assert p['pending'] and not p['results']
    conn = srv.get_db()
    assert conn.execute("SELECT COUNT(*) FROM embedding_jobs WHERE candidate_id=?", (C5,)).fetchone()[0] >= 1
    conn.close()


def test_job_without_jd(srv):
    r = cl(srv, ADMIN).get(f'/api/match/mandate/{M_NOJD}/candidates').get_json()
    assert r['pending'] and 'no JD' in r['message']


def test_scope_and_isolation(srv):
    riya, other = cl(srv, RIYA), cl(srv, OADMIN)
    assert riya.get(f'/api/match/mandate/{M_A}/candidates').status_code == 404     # not her job
    assert riya.post(f'/api/match/mandate/{M_A}/add', json={'candidate_ids': [C4]}).status_code == 404
    mine = [x['id'] for x in riya.get(f'/api/match/mandate/{M_R}/candidates').get_json()['results']]
    assert C4 not in mine and C_O not in mine            # admin's pipeline + other agency hidden
    r = riya.post(f'/api/match/mandate/{M_R}/add', json={'candidate_ids': [C4]})
    assert r.status_code == 404 or r.get_json()['added'] == 0
    assert riya.get(f'/api/match/candidate/{C4}/jobs').status_code == 404
    jobs = [x['mandate_id'] for x in riya.get(f'/api/match/candidate/{C_R}/jobs').get_json()['results']]
    assert jobs == [M_R]                                  # only jobs she is assigned to
    for path in (f'/api/match/mandate/{M_A}/candidates', f'/api/match/candidate/{C1}/jobs'):
        assert other.get(path).status_code == 404
    assert other.post(f'/api/match/mandate/{M_O}/add', json={'candidate_ids': [C1]}).get_json()['added'] == 0


# ── read-only diagnosis ──────────────────────────────────────────────────
def test_diag_overview_and_job(srv):
    a = cl(srv, ADMIN)
    conn = srv.get_db()
    conn.execute("UPDATE candidates SET embedding_text='Engineer\nResume:\nPV design' WHERE id=?", (C3,))
    conn.commit(); conn.close()
    o = a.get('/api/match/diag/overview').get_json()
    c = o['candidates']
    assert c['with_vector'] >= 6 and c['without_vector'] >= 1 and c['resume_text_in_vector'] == 1
    assert c['cv_attached_but_not_in_vector'] >= 1               # C1 has asha.pdf, text has no Resume:
    assert o['mixed_dimensions'] is False and o['embedding_call']['task_mode_used'] is False
    assert o['jobs']['active_without_jd'] == 1                   # M_NOJD
    j = a.get(f'/api/match/diag/job/{M_A}').get_json()
    d = j['distribution']
    assert d['count'] >= 6 and d['max'] >= d['p90'] >= d['median'] >= d['p10'] >= d['min']
    assert j['top15'][0]['score'] == d['max'] and sum(h['n'] for h in j['histogram']) == d['count']
    assert C_O not in [x['id'] for x in j['top15'] + j['bottom5']]  # own agency only


def test_diag_compare_with_mocked_jina(srv, monkeypatch):
    import requests
    a = cl(srv, ADMIN)
    calls = []

    class R:
        status_code = 200
        def __init__(self, body): self._b = body; self.text = json.dumps(body)
        def json(self): return self._b

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, json))
        if url.endswith('/rerank'):
            n = len(json['documents'])
            return R({'results': [{'index': i, 'relevance_score': (n - i) / n} for i in range(n)]})
        k = len(json['input'])
        return R({'data': [{'embedding': [1.0, float(i), 0.0]} for i in range(k)]})
    monkeypatch.setattr(requests, 'post', fake_post)
    orig = srv.get_setting
    monkeypatch.setattr(srv, 'get_setting', lambda k, d='': 'jina-test' if k == 'embedding_api_key' else orig(k, d))
    r = a.post(f'/api/match/diag/job/{M_A}/compare').get_json()
    assert r['ok'], r
    tasks = [c[1].get('task') for c in calls if c[0].endswith('/embeddings')]
    assert tasks == ['retrieval.query', 'retrieval.passage']       # the fix being evaluated
    rr = [c[1] for c in calls if c[0].endswith('/rerank')][0]
    assert not any(doc.split('\n')[0].strip() in ('Asha Active', 'Pooja Pool') for doc in rr['documents'])  # name line removed
    it = r['items'][0]
    assert {'score', 'task_mode', 'rerank', 'score_rank', 'rerank_rank'} <= set(it)
    assert set(r['spread']) == {'current', 'task_mode', 'rerank'}
    # nothing was written
    conn = srv.get_db()
    assert conn.execute("SELECT COUNT(*) FROM candidates WHERE embedding_text LIKE '%retrieval%'").fetchone()[0] == 0
    conn.close()


def test_diag_admin_only(srv):
    for path in ('/api/match/diag/overview', f'/api/match/diag/job/{M_R}'):
        assert cl(srv, RIYA).get(path).status_code in (403, 404)
    assert cl(srv, RIYA).post(f'/api/match/diag/job/{M_R}/compare').status_code in (403, 404)
    assert cl(srv, OADMIN).get(f'/api/match/diag/job/{M_A}').status_code == 404
