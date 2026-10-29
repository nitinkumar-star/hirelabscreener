"""
Talent page CV upload (modules/cv_upload.py) + Market Pulse designed e-mail
(modules/newsletter.py: clean_html, hosted images, compose).

Run:   pytest test_talent_uploads.py -q
Boots the real server.py against a throw-away DATA_DIR. Only the AI resume
reader is replaced by a fake.
"""
import io
import os
import sys
import json
import base64
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PNG = base64.b64encode(bytes.fromhex('89504e470d0a1a0a0000000d4948445200000001000000010806000000'
                                     '1f15c4890000000d49444154789c6360000000000500010d0a2db40000'
                                     '000049454e44ae426082')).decode()


@pytest.fixture(scope='module')
def srv():
    os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='tupl_')
    import server
    server.app.config['TESTING'] = True
    conn = server.get_db()
    c = conn.cursor()

    def company(name):
        c.execute("INSERT INTO companies (name, status, created_at) VALUES (?, 'active', '2026-01-01T00:00:00')", (name,))
        return c.lastrowid

    def user(uname, cid, admin=1):
        c.execute("INSERT INTO users (username, password_hash, display_name, role, status, company_id, "
                  "is_company_admin, created_at) VALUES (?, 'x', ?, 'user', 'approved', ?, ?, '2026-01-01T00:00:00')",
                  (uname, uname.title(), cid, admin))
        return c.lastrowid

    A, B = company('Upl Agency A'), company('Upl Agency B')
    ids = {'A': A, 'B': B, 'a': user('upla', A), 'b': user('uplb', B)}
    c.execute("INSERT INTO mandates (client, role, status, owner_id, created_at) VALUES ('Infosys','Java Lead','active',?,'2026-10-01')", (A,))
    ids['mid_a'] = c.lastrowid
    c.execute("INSERT INTO mandates (client, role, status, owner_id, created_at) VALUES ('Wipro','PM','active',?,'2026-10-01')", (B,))
    ids['mid_b'] = c.lastrowid
    conn.commit(); conn.close()

    orig = (server.extract_text_from_file, server._ai_parse_resume_text, server.get_setting)
    people = {}

    def fake_extract(data, name):
        return data.decode('utf-8', 'ignore') * 3, None

    def fake_parse(text, key):
        return people.get(text.split()[0], {}), None, 200

    server.extract_text_from_file = fake_extract
    server._ai_parse_resume_text = fake_parse
    server.get_setting = lambda k, d='': 'test-key' if k == 'deepseek_api_key' else orig[2](k, d)
    yield server, ids, people
    server.extract_text_from_file, server._ai_parse_resume_text, server.get_setting = orig


def cl_for(srv, uid):
    cl = srv[0].app.test_client()
    with cl.session_transaction() as s:
        s['user_id'] = uid
    return cl


def up(cl, content, fname='cv.pdf', **form):
    data = {'cv': (io.BytesIO(content.encode()), fname)}
    data.update(form)
    return cl.post('/api/talent/upload-cv', data=data, content_type='multipart/form-data')


# ── CV upload ─────────────────────────────────────────────────────────────
def test_upload_creates_candidate_with_cv_file(srv):
    server, ids, people = srv
    people['RAHUL-CV'] = {'name': 'Rahul Sharma', 'company': 'TCS', 'email': 'rahul@x.com', 'phone': '98765 43210',
                          'designation': 'Developer', 'experience': 6, 'key_skills': ['Java', 'Spring']}
    cl = cl_for(srv, ids['a'])
    r = up(cl, 'RAHUL-CV ' + 'x' * 40, 'Rahul_CV.pdf')
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d['central'] and d['name'] == 'Rahul Sharma'
    conn = server.get_db()
    row = conn.execute('SELECT * FROM candidates WHERE id=?', (d['id'],)).fetchone()
    conn.close()
    assert row['owner_id'] == ids['A'] and row['cv_original_name'] == 'Rahul_CV.pdf'
    assert json.loads(row['key_skills']) == ['Java', 'Spring'] and row['stage'] == server.CENTRAL_STAGE
    assert os.path.isfile(os.path.join(server.CV_DIR, row['cv_path']))


def test_upload_to_job_and_duplicate_and_missing_name(srv):
    server, ids, people = srv
    cl = cl_for(srv, ids['a'])
    people['PRIYA-CV'] = {'name': 'Priya N', 'company': 'L&T', 'email': 'priya@x.com'}
    r = up(cl, 'PRIYA-CV ' + 'x' * 40, mandate_id=str(ids['mid_a']))
    assert r.status_code == 200 and r.get_json()['mandate_id'] == ids['mid_a'] and not r.get_json()['central']
    # same person again (same phone as Rahul, different e-mail) -> not added twice
    people['RAHUL-2'] = {'name': 'Rahul S', 'company': 'TCS', 'email': 'other@x.com', 'phone': '+91 9876543210'}
    r = up(cl, 'RAHUL-2 ' + 'x' * 40)
    assert r.status_code == 409 and r.get_json()['duplicate']['name'] == 'Rahul Sharma'
    # name not readable -> 422 with a guess from the file name; then saved with the typed name
    r = up(cl, 'UNKNOWN ' + 'x' * 40, 'Amit_Verma_Resume (1).docx')
    assert r.status_code == 422 and r.get_json()['guess']['name'] == 'Amit Verma'
    r = up(cl, 'UNKNOWN ' + 'x' * 40, 'Amit_Verma_Resume (1).docx', name='Amit Verma', company='Siemens')
    assert r.status_code == 200 and r.get_json()['name'] == 'Amit Verma'


def test_upload_rules(srv):
    server, ids, _ = srv
    cl = cl_for(srv, ids['a'])
    assert up(cl, 'x' * 60, 'photo.png').status_code == 400
    assert up(cl, 'x' * 60, mandate_id=str(ids['mid_b'])).status_code == 404     # another company's job
    anon = server.app.test_client()
    assert anon.post('/api/talent/upload-cv').status_code in (401, 302)


# ── Market Pulse: designed e-mail ─────────────────────────────────────────
def test_clean_html_keeps_design_and_strips_danger(srv):
    from modules import newsletter as nl
    raw = ('<div style="font-family:Arial"><table cellpadding="0" style="width:100%"><tr>'
           '<td style="background:#0F6E56;width:40%;height:8px"></td><td bgcolor="#eee">6–16</td></tr></table>'
           '<script>alert(1)</script><style>body{}</style><p onclick="x()">Hi <b>{first_name}</b></p>'
           '<a href="javascript:alert(1)">bad</a><a href="https://hirelab.in" target="x">ok</a>'
           '<img src="data:image/png;base64,' + PNG + '" alt="logo"><img src="file:///c:/x.png">'
           '<p style="background:url(javascript:x)">css</p><iframe src="https://evil"></iframe></div>')
    out = nl.clean_html(raw, 77)
    assert 'background:#0F6E56' in out and 'bgcolor="#eee"' in out and '<table' in out and '<td' in out
    assert '<script' not in out and 'alert(1)' not in out and 'onclick' not in out and '<style' not in out
    assert 'javascript:' not in out and '<iframe' not in out and 'file:' not in out
    assert 'href="https://hirelab.in"' in out and 'rel="noopener"' in out
    assert '/nl/img/77_' in out and 'data:image' not in out
    name = out.split('/nl/img/')[1].split('"')[0]
    r = srv[0].app.test_client().get('/nl/img/' + name)
    assert r.status_code == 200 and r.data[:4] == b'\x89PNG'
    assert srv[0].app.test_client().get('/nl/img/../../etc/passwd').status_code == 404


def test_designed_issue_saved_and_composed(srv):
    server, ids, _ = srv
    from modules import newsletter as nl
    cl = cl_for(srv, ids['a'])
    iid = cl.post('/api/newsletter/issues', json={}).get_json()['issue']['id']
    html_in = '<h1 style="color:#0E2A47">Renewable Talent Pulse</h1><p>Dear {first_name},</p><script>x</script>'
    r = cl.put(f'/api/newsletter/issue/{iid}', json={'subject': 'Who is hiring', 'body_html': html_in, 'cv_box': False})
    assert r.status_code == 200, r.get_json()
    i = r.get_json()['issue']
    assert '<script' not in i['body_html'] and 'color:#0E2A47' in i['body_html'] and i['cv_box'] is False
    conn = server.get_db()
    issue = nl._issue(conn, ids['A'], iid)
    conn.close()
    subj, plain, body = nl.compose(issue, {'name': 'Rahul Sharma', 'cv_date': ''}, 'https://x/cv', 'https://x/u')
    assert subj == 'Who is hiring' and 'Dear Rahul,' in body and 'Renewable Talent Pulse' in plain
    assert 'Unsubscribe' in body and 'Add my CV' not in body          # profile box switched off
    issue['cv_box'] = 1
    assert 'Add my CV' in nl.compose(issue, {'name': 'R', 'cv_date': ''}, 'https://x/cv', 'https://x/u')[2]
    # duplicating the issue keeps the design
    d = cl.post('/api/newsletter/issues', json={'copy_of': iid}).get_json()['issue']
    assert 'Renewable Talent Pulse' in d['body_html']
