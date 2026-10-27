"""
BD workspace separated from Client CRM (Oct 2026).

Run:   pytest test_bd_split.py -q

BD workspace = prospects (companies, people, deals, tasks, notes, e-mail).
Client CRM   = clients. "Convert to client" moves a prospect across with all
its contacts, e-mails, notes and follow-ups; a won deal converts too.
"""
import sqlite3

from test_naukri_screen import srv, cl, CO, ADMIN, RIYA, OADMIN  # noqa: F401  (fixtures)


def _bd_companies(c, q=''):
    r = c.post('/api/bd/records/companies/query', json={'q': q, 'per_page': 100})
    assert r.status_code == 200, r.get_json()
    return {x['name']: x for x in r.get_json()['records']}


def _crm(c, q='', t=''):
    j = c.get('/api/crm/clients?per_page=100&q=' + q + ('&type=' + t if t else '')).get_json()
    return {x['name']: x for x in j['clients']}


def test_add_from_bd_is_a_prospect_and_from_crm_a_client(srv):
    a = cl(srv, ADMIN)
    r = a.post('/api/bd/records/companies', json={'name': 'Split Prospect One'})
    assert r.status_code == 200, r.get_json()
    r = a.post('/api/crm/clients', json={'name': 'Split Client One', 'city': 'Pune'})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['client']['record_type'] == 'client'
    bd, crm = _bd_companies(a, 'split'), _crm(a, 'split')
    assert 'Split Prospect One' in bd and 'Split Client One' not in bd
    assert 'Split Client One' in crm and 'Split Prospect One' not in crm
    assert 'Split Prospect One' in _crm(a, 'split', 'prospect')            # explicit prospect list
    assert bd['Split Prospect One']['status'] == 'prospect'


def test_duplicate_names_are_caught_across_both_lists(srv):
    a = cl(srv, ADMIN)
    r = a.post('/api/crm/clients', json={'name': 'Split Prospect One Pvt Ltd'})
    assert r.status_code == 409 and 'BD workspace' in r.get_json()['error']
    r = a.post('/api/bd/records/companies', json={'name': 'Split Client One'})
    assert r.status_code == 409 and 'in Clients' in r.get_json()['error']


def test_convert_keeps_everything_and_closes_open_deals(srv):
    a = cl(srv, ADMIN)
    cid = _bd_companies(a, 'split prospect one')['Split Prospect One']['id']
    p = a.post('/api/bd/records/people', json={'client_id': cid, 'name': 'Asha Rao', 'email': 'asha@splitone.in'})
    assert p.status_code == 200, p.get_json()
    a.post('/api/bd/records/tasks', json={'client_id': cid, 'subject': 'Split follow-up', 'due_at': '2099-01-01T10:00:00'})
    a.post('/api/bd/records/notes', json={'client_id': cid, 'subject': 'Split note', 'body': 'met HR'})
    deal = a.post('/api/bd/records/opportunities', json={'client_id': cid, 'name': 'Split deal', 'stage': 'proposal_sent'})
    assert deal.status_code == 200, deal.get_json()

    r = a.post(f'/api/crm/clients/{cid}/convert')
    assert r.status_code == 200 and r.get_json()['deals_won'] == 1
    assert a.post(f'/api/crm/clients/{cid}/convert').status_code == 400       # already a client

    crm = _crm(a, 'split prospect one')['Split Prospect One']
    assert crm['record_type'] == 'client' and crm['status'] == 'active' and crm['converted_at']
    assert 'Split Prospect One' not in _bd_companies(a, 'split')
    full = a.get(f'/api/crm/clients/{cid}').get_json()
    assert [c['name'] for c in full['contacts']] == ['Asha Rao']             # contacts came along
    tl = [x['subject'] for x in a.get(f'/api/bd/clients/{cid}/timeline').get_json()['activities']]
    assert 'Converted to client' in tl and 'Split follow-up' in tl and 'Split note' in tl
    # BD lists no longer carry the client's follow-ups / notes / open deals; the won deal stays as history
    tasks = a.post('/api/bd/records/tasks/query', json={'q': 'split', 'per_page': 100}).get_json()['records']
    assert not any(t['subject'] == 'Split follow-up' for t in tasks)
    opps = a.post('/api/bd/records/opportunities/query', json={'q': 'split deal'}).get_json()['records']
    assert opps and opps[0]['stage'] == 'won'


def test_jobs_and_invoices_pick_clients_only(srv):
    a = cl(srv, ADMIN)
    pid = a.post('/api/bd/records/companies', json={'name': 'Split Prospect Two'}).get_json()['record']['id']
    conn = srv.get_db()
    mid = conn.execute("INSERT INTO mandates (client, role, owner_id, status, created_at) VALUES "
                       "('x','Split Role',?, 'active', '2026-10-27T10:00:00')", (CO,)).lastrowid
    conn.commit(); conn.close()
    r = a.post(f'/api/mandates/{mid}/link-client', json={'crm_client_id': pid})
    assert r.status_code == 404 and 'converted' in r.get_json()['error']
    billing = a.get('/api/crm/clients-billing').get_json().get('clients', [])
    assert not any(c['name'] == 'Split Prospect Two' for c in billing)
    # after conversion the job can be linked, and then the client cannot be moved back
    assert a.post(f'/api/crm/clients/{pid}/convert').status_code == 200
    assert a.post(f'/api/mandates/{mid}/link-client', json={'crm_client_id': pid}).status_code == 200
    r = a.post(f'/api/crm/clients/{pid}/move-to-bd')
    assert r.status_code == 400 and 'job' in r.get_json()['error']


def test_move_back_to_bd(srv):
    a = cl(srv, ADMIN)
    cid = _crm(a, 'split client one')['Split Client One']['id']
    assert a.post(f'/api/crm/clients/{cid}/move-to-bd').status_code == 200
    assert 'Split Client One' in _bd_companies(a, 'split client')
    assert 'Split Client One' not in _crm(a, 'split client')
    assert a.post(f'/api/crm/clients/{cid}/move-to-bd').status_code == 400   # already a prospect


def test_only_admins_of_the_same_company_can_convert(srv):
    a = cl(srv, ADMIN)
    cid = a.post('/api/bd/records/companies', json={'name': 'Split Prospect Three'}).get_json()['record']['id']
    assert cl(srv, RIYA).post(f'/api/crm/clients/{cid}/convert').status_code == 403
    assert cl(srv, OADMIN).post(f'/api/crm/clients/{cid}/convert').status_code == 404
    assert 'Split Prospect Three' in _bd_companies(a, 'split prospect three')


def test_migration_moves_existing_prospects_once(srv):
    """Old database: companies marked 'Prospect' move to BD, unless a job or
    invoice already hangs off them; everything else stays a client."""
    from modules import crm
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE crm_clients (id INTEGER PRIMARY KEY, company_id INTEGER, name TEXT, status TEXT, "
               "is_active INTEGER DEFAULT 1, is_internal INTEGER DEFAULT 0, name_key TEXT DEFAULT '')")
    db.execute('CREATE TABLE mandates (id INTEGER PRIMARY KEY, crm_client_id INTEGER)')
    db.execute('CREATE TABLE invoices (id INTEGER PRIMARY KEY, client_id INTEGER)')
    db.executemany('INSERT INTO crm_clients (id, company_id, name, status) VALUES (?,?,?,?)',
                   [(1, 1, 'Plain Prospect', 'prospect'), (2, 1, 'Active Client', 'active'),
                    (3, 1, 'Prospect With Job', 'prospect'), (4, 1, 'Prospect With Invoice', 'Prospect')])
    db.execute('INSERT INTO mandates (crm_client_id) VALUES (3)')
    db.execute('INSERT INTO invoices (client_id) VALUES (4)')
    crm.migrate(db)
    got = {r['name']: r['record_type'] for r in db.execute('SELECT name, record_type FROM crm_clients')}
    assert got == {'Plain Prospect': 'prospect', 'Active Client': 'client',
                   'Prospect With Job': 'client', 'Prospect With Invoice': 'client'}
    # a later boot never moves anything again
    db.execute("UPDATE crm_clients SET record_type='client', status='prospect' WHERE id=1")
    crm.migrate(db)
    assert db.execute('SELECT record_type FROM crm_clients WHERE id=1').fetchone()[0] == 'client'
