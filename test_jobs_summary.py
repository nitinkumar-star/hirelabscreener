"""
Jobs page pipeline summary — GET /api/mandates/pipeline-summary

Run:   pytest test_jobs_summary.py -q

Checks the stage → bucket mapping (including tenant-custom stages) and that
the endpoint follows the same visibility rule as GET /api/mandates:
  * company admin sees every job in their company
  * recruiter sees only jobs assigned to them
  * another company sees none of it
  * the Central Database pool is never included
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CO, OTHER_CO = 931, 932
ADMIN, REC, OTHER_ADMIN = 9311, 9312, 9321
M_A, M_B, M_POOL, M_FOREIGN = 931001, 931002, 931003, 932001


@pytest.fixture(scope='module')
def server_mod():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='jobsum_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db(); c = conn.cursor(); now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})",
                  list(kw.values()))

    ins('companies', id=CO, name='Jobs Sum Co', status='active', created_at=now)
    ins('companies', id=OTHER_CO, name='Jobs Sum Other', status='active', created_at=now)
    for uid, un, co, adm in ((ADMIN, 'js_admin', CO, 1), (REC, 'js_rec', CO, 0),
                             (OTHER_ADMIN, 'js_other', OTHER_CO, 1)):
        ins('users', id=uid, username=un, password_hash='x', display_name=un, role='user',
            status='approved', company_id=co, is_company_admin=adm, created_at=now)
    ins('mandates', id=M_A, client='Acme', role='Job A', owner_id=CO, assigned_user_id=ADMIN,
        status='active', created_at=now)
    ins('mandates', id=M_B, client='Acme', role='Job B', owner_id=CO, assigned_user_id=REC,
        status='active', created_at=now)
    ins('mandates', id=M_POOL, client='pool', role='Central Database', owner_id=CO,
        assigned_user_id=ADMIN, status='central', created_at=now)
    ins('mandates', id=M_FOREIGN, client='Other', role='Foreign', owner_id=OTHER_CO,
        assigned_user_id=OTHER_ADMIN, status='active', created_at=now)
    stages_a = ['Screening', 'Screening', 'Called', 'Follow Up 1', 'Interested',
                'Shared with Client', 'Interview Inprocess', 'Placed',
                'Not Suitable', 'Client Rejected on Paper', 'Interview Backout', 'My Custom Stage']
    for i, stg in enumerate(stages_a):
        ins('candidates', id=931100 + i, mandate_id=M_A, name=f'A{i}', owner_id=CO, stage=stg,
            created_at=now, updated_at=now)
    ins('candidates', id=931200, mandate_id=M_B, name='B0', owner_id=CO, stage='Shared with Client',
        created_at=now, updated_at=now)
    ins('candidates', id=931300, mandate_id=M_POOL, name='P0', owner_id=CO, stage='Central DB',
        created_at=now, updated_at=now)
    ins('candidates', id=931400, mandate_id=M_FOREIGN, name='F0', owner_id=OTHER_CO, stage='Screening',
        created_at=now, updated_at=now)
    conn.commit(); conn.close()
    # Recruiter assignment rows are kept in sync by triggers in modules/access.py;
    # make sure the recruiter is actually on M_B.
    conn = server.get_db()
    try:
        conn.execute('INSERT OR IGNORE INTO mandate_assignees (mandate_id, user_id, is_primary, is_active, created_at) '
                     'VALUES (?,?,?,?,?)', (M_B, REC, 1, 1, now))
        conn.commit()
    except Exception:
        pass
    conn.close()
    yield server
    conn = server.get_db()
    conn.execute('DELETE FROM candidates WHERE id BETWEEN 931000 AND 932999')
    conn.execute('DELETE FROM mandates WHERE id IN (?,?,?,?)', (M_A, M_B, M_POOL, M_FOREIGN))
    conn.commit(); conn.close()


def _as(server, uid):
    cl = server.app.test_client()
    with cl.session_transaction() as s:
        s['user_id'] = uid
    return cl


def test_bucket_mapping(server_mod):
    b = server_mod.job_summary_bucket
    assert b('Screening') == 'sourced'
    assert b('Follow Up 2') == 'engaged'
    assert b('Updated CV awaited') == 'engaged'
    assert b('Shared with Client') == 'client'
    assert b('Interview Inprocess') == 'interview'
    assert b('Joined') == 'placed'
    assert b('Client Rejected After Interview') == 'dropped'
    # tenant-custom stages
    assert b('Interview Backout') == 'dropped'
    assert b('Offer Declined') == 'dropped'
    assert b('HR Interview') == 'interview'
    assert b('Submitted to HM') == 'client'
    assert b('Some Brand New Stage') == 'engaged'
    assert b('') == 'sourced' and b(None) == 'sourced'


def test_admin_sees_company_jobs_with_counts(server_mod):
    r = _as(server_mod, ADMIN).get('/api/mandates/pipeline-summary')
    assert r.status_code == 200
    data = r.get_json()
    assert data['ok'] is True
    s = data['summary']
    a = s[str(M_A)]
    assert a == {**a, 'sourced': 2, 'engaged': 4, 'client': 1, 'interview': 1, 'placed': 1,
                 'dropped': 3, 'total': 12}
    assert a['last_activity']
    assert s[str(M_B)]['client'] == 1
    assert str(M_POOL) not in s          # Central DB pool is not a job
    assert str(M_FOREIGN) not in s       # other company's job


def test_recruiter_sees_only_assigned(server_mod):
    s = _as(server_mod, REC).get('/api/mandates/pipeline-summary').get_json()['summary']
    assert str(M_B) in s
    assert str(M_A) not in s
    assert str(M_FOREIGN) not in s


def test_other_company_isolated(server_mod):
    s = _as(server_mod, OTHER_ADMIN).get('/api/mandates/pipeline-summary').get_json()['summary']
    assert str(M_A) not in s and str(M_B) not in s
    assert s[str(M_FOREIGN)]['total'] == 1


def test_requires_login(server_mod):
    r = server_mod.app.test_client().get('/api/mandates/pipeline-summary')
    assert r.status_code in (401, 302, 403)
