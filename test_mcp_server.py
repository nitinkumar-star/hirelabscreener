"""
Claude connector (mcp_server.py): "ask anything" over the ATS.

ats_catalog lists the read-only routes the ATS publishes at /api/mcp/catalog;
ats_read may call only those. The connector's ATS client is replaced by the
Flask test client, so the real routes and the real catalog are used.

Run:   pytest test_mcp_server.py -q
"""
import os
import sys
import json
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CO, ADMIN = 981, 9801

httpx = pytest.importorskip('httpx')
pytest.importorskip('starlette')


class _FakeATS(object):
    def __init__(self, client):
        self.c, self.calls = client, []

    def get(self, path, params=None):
        self.calls.append(('GET', path, params))
        r = self.c.get(path, query_string=params or {})
        if r.status_code >= 400:
            raise httpx.HTTPStatusError('x', request=httpx.Request('GET', 'http://ats' + path),
                                        response=httpx.Response(r.status_code))
        return r.get_json()

    def post(self, path, payload=None):
        raise AssertionError('read tools must not POST')

    put = post


@pytest.fixture(scope='module')
def mcp():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='mcp_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    c = server.get_db(); now = server.ts()
    c.execute("INSERT OR REPLACE INTO companies (id, name, status, created_at) VALUES (?,?,?,?)", (CO, 'MCP Co', 'active', now))
    c.execute("INSERT OR REPLACE INTO users (id, username, password_hash, display_name, role, status, company_id, "
              "is_company_admin, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
              (ADMIN, 'mcp_admin', 'x', 'Admin', 'user', 'approved', CO, 1, now))
    c.execute("INSERT INTO invoices (owner_id, invoice_no, invoice_date, buyer_name, amount, gst_rate, status, doc_type) "
              "VALUES (?,?,?,?,?,?,?,?)", (CO, 'MCP/001', '2026-11-01', 'Tata Power', 100000, 18, 'Sent', 'tax'))
    c.execute("INSERT INTO mandates (owner_id, client, role, status, created_at) VALUES (?,?,?,?,?)",
              (CO, 'Acme', 'Open Role', 'active', now))
    c.execute("INSERT INTO mandates (owner_id, client, role, status, created_at) VALUES (?,?,?,?,?)",
              (CO, 'Acme', 'Old Role', 'closed', now))
    c.commit(); c.close()
    cl = server.app.test_client()
    with cl.session_transaction() as s:
        s['user_id'] = ADMIN
    os.environ.setdefault('ATS_BASE_URL', 'http://ats.invalid')
    import mcp_server
    mcp_server.ats = _FakeATS(cl)
    mcp_server._CATALOG.update(at=0.0, routes=[])
    return mcp_server


def call(mcp, name, args=None):
    out = mcp._handle_rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args or {}}})
    res = out['result']
    return json.loads(res['content'][0]['text']) if not res.get('isError') else res


def test_tools_and_instructions_are_listed(mcp):
    init = mcp._handle_rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})['result']
    assert 'ats_catalog' in init['instructions']
    names = {t['name'] for t in mcp._handle_rpc({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})['result']['tools']}
    assert {'ats_catalog', 'ats_read'} <= names


def test_catalog_finds_routes_but_not_secrets(mcp):
    cat = call(mcp, 'ats_catalog', {'search': 'invoice'})
    paths = {r['path'] for r in cat['routes']}
    assert '/api/invoices' in paths
    allp = set(call(mcp, 'ats_catalog')['paths'])
    for bad in ('/api/settings', '/api/admin/backup/db', '/api/cv/<path:filename>', '/api/export',
                '/api/reminders/<int:rid>/done', '/api/emailbox/attachment/<int:aid>'):
        assert bad not in allp, bad
    assert len(allp) > 150


def test_read_any_catalog_route(mcp):
    inv = call(mcp, 'ats_read', {'path': '/api/invoices'})
    assert any(i.get('invoice_no') == 'MCP/001' for i in inv['invoices'])
    s = call(mcp, 'ats_read', {'path': 'api/invoices/summary'})
    assert s['invoiced_taxable'] == 100000 and s['invoiced_gst'] == 18000
    # ids and query strings
    m = call(mcp, 'list_mandates')
    mid = m['mandates'][0]['id']
    assert call(mcp, 'ats_read', {'path': '/api/mandates/%d/candidates' % mid}) is not None


def test_read_refuses_paths_outside_the_catalog(mcp):
    for p in ('/api/settings', '/api/admin/backup/db', '/api/reminders/1/done', '/api/../etc/passwd', '/team-chat'):
        r = call(mcp, 'ats_read', {'path': p})
        assert 'error' in r, p
    assert not any(c[1] in ('/api/settings', '/api/admin/backup/db', '/api/reminders/1/done') for c in mcp.ats.calls)


def test_list_mandates_defaults_to_active(mcp):
    act = call(mcp, 'list_mandates')
    assert act['status'] == 'active' and all(m['status'] == 'active' for m in act['mandates'])
    allm = call(mcp, 'list_mandates', {'status': 'all'})
    assert any(m['status'] == 'closed' for m in allm['mandates'])


def test_overview_drops_old_board_reports(mcp):
    ov = call(mcp, 'get_overview')
    assert 'last_plan' not in ov and 'last_review' not in ov


def test_big_results_are_cut(mcp, monkeypatch):
    monkeypatch.setattr(mcp, 'MAX_READ_CHARS', 50)
    r = call(mcp, 'ats_read', {'path': '/api/invoices'})
    assert r['truncated'] and len(r['partial_json']) == 50
