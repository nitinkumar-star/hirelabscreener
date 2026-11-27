"""
Claude connector (mcp_server.py): Memory tools — Wave 2 tests.

recall_memory, get_memory_context, save_memory, archive_memory run against the
REAL ATS routes (modules/memory.py) through the Flask test client, exactly as
test_mcp_server.py does for the read tools. Also checks the write switches:
memory writes follow MCP_ALLOW_MEMORY_WRITES, independently of MCP_ALLOW_WRITES.

Run:   pytest test_mcp_memory.py -q
"""
import os
import sys
import json
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CO, ADMIN = 971, 9701
M1, C1 = 97101, 97201

httpx = pytest.importorskip('httpx')
pytest.importorskip('starlette')


class _FlaskATS(object):
    """Stands in for mcp_server.ATS: same get/post/put, real Flask routes."""

    def __init__(self, client):
        self.c, self.calls = client, []

    def _do(self, method, path, params=None, payload=None):
        self.calls.append((method, path, params, payload))
        if method == 'GET':
            r = self.c.get(path, query_string=params or {})
        else:
            r = self.c.open(path, method=method, json=payload or {})
        if r.status_code >= 400:
            req = httpx.Request(method, 'http://ats' + path)
            raise httpx.HTTPStatusError('x', request=req,
                                        response=httpx.Response(r.status_code, content=r.data,
                                                                headers={'content-type': r.content_type},
                                                                request=req))
        return r.get_json()

    def get(self, path, params=None):
        return self._do('GET', path, params=params)

    def post(self, path, payload=None):
        return self._do('POST', path, payload=payload)

    def put(self, path, payload=None):
        return self._do('PUT', path, payload=payload)


@pytest.fixture(scope='module')
def mcp():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='mcpmem_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    c = server.get_db(); now = server.ts()
    c.execute("INSERT OR REPLACE INTO companies (id, name, status, created_at) VALUES (?,?,?,?)",
              (CO, 'MCP Mem Co', 'active', now))
    c.execute("INSERT OR REPLACE INTO users (id, username, password_hash, display_name, role, status, company_id, "
              "is_company_admin, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
              (ADMIN, 'mcpmem_admin', 'x', 'Admin', 'user', 'approved', CO, 1, now))
    c.execute("INSERT OR REPLACE INTO mandates (id, owner_id, client, role, location, status, created_at) "
              "VALUES (?,?,?,?,?,?,?)", (M1, CO, 'Lauritz & Knudsen', 'Distribution Manager', 'Haridwar', 'active', now))
    c.execute("INSERT OR REPLACE INTO candidates (id, mandate_id, name, phone, owner_id, stage, created_at, "
              "client_feedback) VALUES (?,?,?,?,?,?,?,?)",
              (C1, M1, 'Vipin Kumar Tomar', '9300000001', CO, 'Screening', now, 'Ref check not positive'))
    c.commit(); c.close()
    cl = server.app.test_client()
    with cl.session_transaction() as s:
        s['user_id'] = ADMIN
    os.environ.setdefault('ATS_BASE_URL', 'http://ats.invalid')
    import mcp_server
    mcp_server.ats = _FlaskATS(cl)
    return mcp_server


@pytest.fixture
def mem_on(mcp, monkeypatch):
    monkeypatch.setattr(mcp, 'ALLOW_MEMORY_WRITES', True)
    monkeypatch.setattr(mcp, 'ALLOW_WRITES', False)
    return mcp


def rpc(mcp, method, params=None):
    return mcp._handle_rpc({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})['result']


def call(mcp, name, args=None):
    res = rpc(mcp, 'tools/call', {'name': name, 'arguments': args or {}})
    if res.get('isError'):
        return {'_error': res['content'][0]['text']}
    return json.loads(res['content'][0]['text'])


def names(mcp):
    return {t['name']: t for t in rpc(mcp, 'tools/list')['tools']}


# ── switches + listing ───────────────────────────────────────────────────
def test_memory_writes_have_their_own_switch(mem_on):
    t = names(mem_on)
    assert {'recall_memory', 'get_memory_context', 'save_memory', 'archive_memory'} <= set(t)
    assert 'send_candidate_email' not in t and 'update_candidate' not in t   # other writes stay off
    assert t['archive_memory']['annotations']['destructiveHint'] is True
    assert t['save_memory']['annotations']['destructiveHint'] is False
    assert t['recall_memory']['annotations']['readOnlyHint'] is True
    assert 'Call this FIRST' in t['get_memory_context']['description']
    assert 'valid_until' in t['save_memory']['description']
    ins = rpc(mem_on, 'initialize')['instructions']
    assert 'get_memory_context' in ins and 'save_memory' in ins and 'ats_catalog' in ins


def test_memory_writes_off(mcp, monkeypatch):
    monkeypatch.setattr(mcp, 'ALLOW_MEMORY_WRITES', False)
    monkeypatch.setattr(mcp, 'ALLOW_WRITES', True)
    t = names(mcp)
    assert 'recall_memory' in t and 'get_memory_context' in t
    assert 'save_memory' not in t and 'archive_memory' not in t
    assert 'send_candidate_email' in t                                     # unaffected
    r = call(mcp, 'save_memory', {'scope': 'general', 'content': 'x'})
    assert 'MCP_ALLOW_MEMORY_WRITES' in r['_error']
    assert 'save_memory' not in rpc(mcp, 'initialize')['instructions']


def test_all_writes_off_reads_still_work(mcp, monkeypatch):
    monkeypatch.setattr(mcp, 'ALLOW_MEMORY_WRITES', False)
    monkeypatch.setattr(mcp, 'ALLOW_WRITES', False)
    t = names(mcp)
    assert 'recall_memory' in t and 'save_memory' not in t and 'add_candidate' not in t
    assert 'count' in call(mcp, 'recall_memory')


# ── save / recall / context / archive ────────────────────────────────────
def test_save_dedupe_update_and_recall(mem_on):
    a = call(mem_on, 'save_memory', {'scope': 'person', 'entity_name': 'Nishtha Bajaj',
                                     'client_name': 'Lauritz & Knudsen', 'title': 'Nishtha wants DM-level profiles',
                                     'content': 'Rejects 10-yr profiles for DM roles; wants grade 4, 6-8 LPA.',
                                     'tags': ['rejection-pattern'], 'importance': 'high'})
    assert a['action'] == 'created' and a['note']['source'] == 'claude'
    assert a['tell_user'].startswith('Saved to ATS memory (created')
    nid = a['id']
    b = call(mem_on, 'save_memory', {'scope': 'person', 'entity_name': 'Nishtha Bajaj',
                                     'title': 'Nishtha wants DM level profiles',
                                     'content': 'Rejects 10-yr profiles for DM roles; wants grade 4, 6-8 LPA (Nov 2026).'})
    assert b['action'] == 'updated' and b['id'] == nid and b['merged_into_existing'] is True
    c = call(mem_on, 'save_memory', {'note_id': nid, 'importance': 'normal'})
    assert c['action'] == 'updated' and c['note']['importance'] == 'normal'
    assert call(mem_on, 'save_memory', {'note_id': nid, 'importance': 'normal'})['action'] == 'unchanged'

    r = call(mem_on, 'recall_memory', {'query': '10-yr', 'entity_name': 'nishtha'})
    assert r['count'] == 1
    n = r['notes'][0]
    assert n['id'] == nid and n['content'].endswith('(Nov 2026).') and n['tags'] == ['rejection-pattern']
    assert 'status' not in n and 'created_by' not in n                     # compact output
    assert call(mem_on, 'recall_memory', {'query': 'zzznothing'})['hint']


def test_save_errors_are_readable(mem_on):
    r = call(mem_on, 'save_memory', {'scope': 'candidate', 'content': 'x'})
    assert 'needs entity_name or entity_id' in r['_error']
    assert 'required' in call(mem_on, 'save_memory', {'content': 'no scope'})['_error']
    assert 'scope must be' in call(mem_on, 'save_memory', {'scope': 'gossip', 'content': 'x'})['_error']
    assert 'valid_until' in call(mem_on, 'save_memory', {'scope': 'general', 'content': 'x',
                                                         'valid_until': 'next friday'})['_error']
    assert 'Not found' in call(mem_on, 'save_memory', {'note_id': 999999999, 'content': 'x'})['_error']
    assert 'Not found' in call(mem_on, 'save_memory', {'scope': 'mandate', 'entity_id': 999999999,
                                                       'content': 'x'})['_error']


def test_time_bound_note(mem_on, mcp):
    import server
    past = (server._ist_now().date().replace(day=1)).isoformat()
    a = call(mem_on, 'save_memory', {'scope': 'candidate', 'entity_id': C1, 'title': 'Interview slot',
                                     'content': 'Client slot offered for 1st', 'valid_until': past})
    if past < server._ist_now().date().isoformat():
        assert a['note'].get('expired') is True
        assert a['id'] not in {n['id'] for n in call(mem_on, 'recall_memory', {'query': 'slot'})['notes']}
        got = call(mem_on, 'recall_memory', {'query': 'slot', 'include_expired': True})['notes']
        assert a['id'] in {n['id'] for n in got}
    cleared = call(mem_on, 'save_memory', {'note_id': a['id'], 'valid_until': 'none'})
    assert 'valid_until' not in cleared['note']


def test_context_tool(mem_on):
    call(mem_on, 'save_memory', {'scope': 'mandate', 'entity_id': M1, 'title': 'Ghaziabad',
                                 'content': 'Hire will handle the Ghaziabad branch'})
    call(mem_on, 'save_memory', {'scope': 'client', 'entity_name': 'Lauritz & Knudsen', 'title': 'Priority roles',
                                 'content': 'Top priority: Haridwar, Amritsar, Chandigarh DM'})
    j = call(mem_on, 'get_memory_context', {'candidate_id': C1})
    assert j['type'] == 'candidate' and 'ok' not in j
    assert j['candidate_fields']['client_feedback'] == 'Ref check not positive'
    assert any('Ghaziabad' in n['content'] for n in j['mandate']['notes'])
    assert any(n['title'] == 'Priority roles' for n in j['client']['notes'])
    assert any(n['entity_name'] == 'Nishtha Bajaj' for n in j['client']['people_notes'])
    assert all('created_by' not in n for n in j['client']['notes'])        # slimmed
    m = call(mem_on, 'get_memory_context', {'mandate_id': M1})
    assert m['type'] == 'mandate'
    c = call(mem_on, 'get_memory_context', {'client': 'lauritz'})
    assert c['type'] == 'client' and c['mandates'][0]['id'] == M1
    assert 'exactly one' in call(mem_on, 'get_memory_context', {})['_error']
    assert 'exactly one' in call(mem_on, 'get_memory_context', {'client': 'x', 'mandate_id': M1})['_error']
    assert 'Not found' in call(mem_on, 'get_memory_context', {'candidate_id': 999999999})['_error']


def test_archive_tool(mem_on):
    a = call(mem_on, 'save_memory', {'scope': 'general', 'title': 'Wrong fact', 'content': 'archivetest wrong'})
    r = call(mem_on, 'archive_memory', {'note_id': a['id']})
    assert r['status'] == 'archived'
    assert call(mem_on, 'recall_memory', {'query': 'archivetest'})['count'] == 0
    assert call(mem_on, 'archive_memory', {'note_id': a['id']})['status'] == 'archived'   # idempotent
    assert 'required' in call(mem_on, 'archive_memory', {'note_id': 0})['_error']


def test_ats_without_memory_says_so(mem_on, monkeypatch):
    class _Old(object):
        def get(self, path, params=None):
            req = httpx.Request('GET', 'http://ats' + path)
            raise httpx.HTTPStatusError('x', request=req, response=httpx.Response(
                404, content=b'<html>Not Found</html>', headers={'content-type': 'text/html'}, request=req))
        post = put = lambda self, path, payload=None: self.get(path)
    monkeypatch.setattr(mem_on, 'ats', _Old())
    assert 'Deploy the ATS Memory Wave 1' in call(mem_on, 'recall_memory')['_error']
    assert 'Deploy the ATS Memory Wave 1' in call(mem_on, 'save_memory', {'scope': 'general', 'content': 'x'})['_error']


def test_switch_default_follows_allow_writes():
    """MCP_ALLOW_MEMORY_WRITES not set -> same as MCP_ALLOW_WRITES; set -> wins."""
    import subprocess
    code = 'import mcp_server as m; print(m.ALLOW_WRITES, m.ALLOW_MEMORY_WRITES)'
    root = os.path.dirname(os.path.abspath(__file__))

    def run(env):
        e = {k: v for k, v in os.environ.items() if not k.startswith('MCP_ALLOW')}
        e.update(env)
        return subprocess.run([sys.executable, '-c', code], cwd=root, env=e, capture_output=True,
                              text=True, timeout=60).stdout.split()
    assert run({}) == ['False', 'False']
    assert run({'MCP_ALLOW_WRITES': 'true'}) == ['True', 'True']
    assert run({'MCP_ALLOW_MEMORY_WRITES': 'true'}) == ['False', 'True']
    assert run({'MCP_ALLOW_WRITES': 'true', 'MCP_ALLOW_MEMORY_WRITES': 'false'}) == ['True', 'False']
