"""
Gender on the candidate (Oct 2026): stored only when the CV / Naukri page states it.

Run:   pytest test_gender.py -q
"""
import io
import os
import sys
import json
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope='module')
def srv():
    os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='gender_')
    import server
    server.app.config['TESTING'] = True
    conn = server.get_db()
    c = conn.cursor()
    c.execute("INSERT INTO companies (name, status, created_at) VALUES ('G Agency', 'active', '2026-01-01T00:00:00')")
    co = c.lastrowid
    c.execute("INSERT INTO users (username, password_hash, display_name, role, status, company_id, is_company_admin, "
              "created_at) VALUES ('gadmin', 'x', 'G Admin', 'user', 'approved', ?, 1, '2026-01-01T00:00:00')", (co,))
    uid = c.lastrowid
    c.execute("INSERT INTO mandates (client, role, status, owner_id, created_at) VALUES ('Acme','Sales','active',?,'2026-10-01')", (co,))
    mid = c.lastrowid
    conn.commit(); conn.close()
    yield server, {'co': co, 'uid': uid, 'mid': mid}


def cl(srv):
    c = srv[0].app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = srv[1]['uid']
    return c


def test_detect_gender_only_when_stated(srv):
    server = srv[0]
    d = server.detect_gender
    assert d('Personal details\nGender: Female\nDOB 1994') == 'Female'
    assert d('SEX : M') == 'Male'
    assert d('Male, Single/unmarried  Date of birth') == 'Male'
    assert d('Female, Married') == 'Female'
    assert d('Managed a team of 12 including female technicians') == ''
    assert d('Priya Sharma, Sales Manager') == ''          # never from the name
    assert server.norm_gender('f') == 'Female' and server.norm_gender('banana') == ''


def test_push_profile_edit_filter_and_export(srv):
    server, ids = srv
    c = cl(srv)
    r = c.post('/api/extension/push', json={'mandate_id': ids['mid'], 'name': 'Kundan Patil', 'phone': '9876500011',
                                            'email': 'kundan@x.com', 'gender': 'male'})
    assert r.status_code == 200, r.get_json()
    cid = r.get_json()['candidate_id']
    assert c.get(f'/api/candidates/{cid}').get_json()['gender'] == 'Male'
    # pushing again without a gender keeps it
    c.post('/api/extension/push', json={'mandate_id': ids['mid'], 'name': 'Kundan Patil', 'phone': '9876500011',
                                        'email': 'kundan@x.com', 'gender': ''})
    assert c.get(f'/api/candidates/{cid}').get_json()['gender'] == 'Male'
    # edited on the profile (normalised), logged in the journey
    assert c.put(f'/api/candidates/{cid}', json={'gender': 'F'}).status_code == 200
    assert c.get(f'/api/candidates/{cid}').get_json()['gender'] == 'Female'
    r2 = c.post('/api/extension/push', json={'mandate_id': ids['mid'], 'name': 'Amit Rao', 'phone': '9876500022',
                                             'email': 'amit@x.com'})
    other = r2.get_json()['candidate_id']
    names = lambda q: {x['id'] for x in c.get('/api/central-db/search?' + q).get_json()['candidates']}
    assert cid in names('gender=Female') and other not in names('gender=Female')
    assert other in names('gender=unknown') and cid not in names('gender=unknown')
    csv = c.get('/api/central-db/export').get_data(as_text=True)
    assert 'Gender' in csv.splitlines()[0] and 'Female' in csv


def test_extension_parse_reads_stated_gender(srv, monkeypatch):
    server, ids = srv
    from modules import naukri_screen as ns
    monkeypatch.setattr(ns, '_deepseek_key', lambda core: 'k')
    monkeypatch.setattr(ns, '_ask_deepseek', lambda *a, **k: {'name': 'Neha Joshi', 'company': 'L&T', 'gender': ''})
    c = cl(srv)
    page = 'Neha Joshi\nSales Manager at L&T\nPersonal details\nFemale, Single/unmarried\nPune' + ' ' * 40
    d = c.post('/api/assistant/parse', data={'page_text': page}).get_json()
    assert d['ok'] and d['fields']['gender'] == 'Female'
    monkeypatch.setattr(ns, '_ask_deepseek', lambda *a, **k: {'name': 'Ravi', 'gender': 'Male'})
    d = c.post('/api/assistant/parse', data={'page_text': 'Ravi Kumar profile text for parsing ' * 3}).get_json()
    assert d['fields']['gender'] == 'Male'
