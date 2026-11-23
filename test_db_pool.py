"""
Speed fix (Nov 2026): get_db() reuses connections per thread instead of
opening a fresh one (which re-parses the large schema every time).

Run:   pytest test_db_pool.py -q
"""
import os
import sys
import sqlite3
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='pool_'))
    import server
    c = server.get_db()
    c.execute('CREATE TABLE IF NOT EXISTS pool_t (v INT)')
    c.execute('DELETE FROM pool_t')
    c.commit(); c.close()
    return server


def count(srv):
    c = srv.get_db()
    try:
        return c.execute('SELECT COUNT(*) FROM pool_t').fetchone()[0]
    finally:
        c.close()


def test_connection_is_reused_on_the_same_thread(srv):
    a = srv.get_db(); a.close()
    b = srv.get_db(); b.close()
    assert a is b


def test_uncommitted_write_is_discarded_on_close(srv):
    n = count(srv)
    c = srv.get_db(); c.execute('INSERT INTO pool_t VALUES (1)'); c.close()
    assert count(srv) == n


def test_nested_get_db_gets_its_own_connection(srv):
    n = count(srv)
    outer = srv.get_db()
    outer.execute('INSERT INTO pool_t VALUES (2)')
    inner = srv.get_db()
    assert inner is not outer
    inner.close()                      # must not roll back the outer write
    assert outer.in_transaction
    outer.commit(); outer.close()
    assert count(srv) == n + 1


def test_writes_from_other_connections_are_seen(srv):
    n = count(srv)
    p = srv.get_db(); p.execute('SELECT * FROM pool_t').fetchall(); p.close()

    def writer():
        w = srv.get_db(); w.execute('INSERT INTO pool_t VALUES (3)'); w.commit(); w.close()
    t = threading.Thread(target=writer); t.start(); t.join()
    raw = sqlite3.connect(srv.DB_PATH); raw.execute('INSERT INTO pool_t VALUES (4)'); raw.commit(); raw.close()
    assert count(srv) == n + 2


def test_reused_connection_is_reset(srv):
    c = srv.get_db(); c.row_factory = None; c.close(); c.close()   # double close is harmless
    d = srv.get_db()
    assert d.row_factory is sqlite3.Row
    d.close()
