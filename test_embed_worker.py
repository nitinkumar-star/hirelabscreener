"""
Nov 2026: "candidates are not being indexed".

The Jina key is saved PER COMPANY (tenant_settings), but the background
embedding worker has no logged-in company, so it read only the global row and,
with that empty, never indexed anything. Each job now runs pinned to the
candidate's company. Failed jobs are re-queued after a few hours.

Run:   pytest test_embed_worker.py -q
"""
import os
import sys
import tempfile
import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class _Stop(BaseException):
    pass


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='embw_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    return server


def _cands(srv, oid, n):
    c = srv.get_db()
    ids = [c.execute('INSERT INTO candidates (mandate_id, name, designation, key_skills, owner_id, created_at) '
                     'VALUES (0,?,?,?,?,?)', ('C%d' % i, 'Engineer', '["python"]', oid, srv.ts())).lastrowid
           for i in range(n)]
    c.commit(); c.close()
    return ids


def _run_worker(srv, monkeypatch, cycles=8):
    import time
    calls = {'n': 0}

    def fake_sleep(_x):
        calls['n'] += 1
        if calls['n'] > cycles:
            raise _Stop()
    monkeypatch.setattr(time, 'sleep', fake_sleep)
    with pytest.raises(_Stop):
        srv._embedding_worker_loop()


def test_worker_uses_the_companys_own_key(srv, monkeypatch):
    c = srv.get_db()
    saved = c.execute("SELECT value FROM settings WHERE key='embedding_api_key'").fetchone()
    c.execute("DELETE FROM settings WHERE key='embedding_api_key'")
    c.execute("INSERT OR REPLACE INTO tenant_settings (company_id, key, value) VALUES (7, 'embedding_api_key', 'co7key')")
    c.commit(); c.close()
    monkeypatch.delenv('JINA_API_KEY', raising=False)
    try:
        _check_worker(srv, monkeypatch)
    finally:                               # leave the shared test DB as it was
        c = srv.get_db()
        c.execute("DELETE FROM tenant_settings WHERE company_id=7 AND key='embedding_api_key'")
        if saved is not None:
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('embedding_api_key', ?)", (saved[0],))
        c.commit(); c.close()


def _check_worker(srv, monkeypatch):
    ids = _cands(srv, 7, 3)
    c = srv.get_db()                       # shared test DB: only these jobs are waiting
    c.execute("UPDATE embedding_jobs SET status='cancelled' WHERE status IN ('pending','retrying','processing')")
    c.commit(); c.close()
    for i in ids:
        srv.queue_embedding_job(i)
    used = []

    def fake_embed(_txt):
        used.append(srv.get_setting('embedding_api_key'))
        return [0.1] * 8
    monkeypatch.setattr(srv, 'embed_one', fake_embed)
    monkeypatch.setattr(srv, '_embed_cfg', lambda k: {'embed_interval_ms': 0}.get(k, srv._EMBED_CFG_DEFAULTS[k]))
    monkeypatch.setattr(srv, '_reconcile_missing_embeddings', lambda *a: 0)
    _run_worker(srv, monkeypatch, cycles=40)
    c = srv.get_db()
    done = [r[0] for r in c.execute('SELECT embedding_status FROM candidates WHERE id IN (%s)' % ','.join('?' * len(ids)), ids)]
    c.close()
    assert done == ['completed'] * 3
    assert set(used) == {'co7key'}


def test_failed_jobs_are_requeued_after_a_while(srv):
    cid = _cands(srv, 7, 1)[0]
    c = srv.get_db()
    old = (srv._ist_now() - datetime.timedelta(hours=srv.EMBED_FAILED_RETRY_H + 1)).isoformat(timespec='seconds')
    c.execute("INSERT INTO embedding_jobs (candidate_id, status, retry_count, max_retries, created_at, completed_at) "
              "VALUES (?, 'failed', 5, 5, ?, ?)", (cid, old, srv.ts()))      # failed just now: left alone
    c.commit()
    srv._reconcile_missing_embeddings(c, 500)
    assert not c.execute("SELECT 1 FROM embedding_jobs WHERE candidate_id=? AND status='pending'", (cid,)).fetchone()
    c.execute("UPDATE embedding_jobs SET completed_at=? WHERE candidate_id=?", (old, cid))   # failed hours ago
    c.commit()
    srv._reconcile_missing_embeddings(c, 500)
    assert c.execute("SELECT 1 FROM embedding_jobs WHERE candidate_id=? AND status='pending'", (cid,)).fetchone()
    c.close()
