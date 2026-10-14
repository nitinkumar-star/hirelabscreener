"""
HireLab Assistant (paste / CV side panel) — modules/assistant.py

Run:   pytest test_assistant.py -q
Real server and access guards; DeepSeek is the fake from test_naukri_screen.
"""
import io
import json

from test_naukri_screen import srv, ds, cl, _seed_dupes, CO, ADMIN, RIYA, FL, M_A, M_R  # noqa: F401  (fixtures)


def _docx(text):
    from docx import Document
    d = Document()
    for line in text.split('\n'):
        d.add_paragraph(line)
    b = io.BytesIO(); d.save(b); b.seek(0)
    return b


CV = ('Asha Verma\nPhone: +91 98111 22334  |  asha.verma@example.com\n'
      'Senior O&M Engineer, Azure Power (2019 - present)\nManaged 300 MW solar plants, SCADA, inverter maintenance.\n'
      'B.Tech Electrical. Skills: Solar O&M, SCADA, Preventive maintenance.')
PAGE = ('Naukri Resdex  Jobs & Responses  Search\nAsha Verma\nSenior O&M Engineer at Azure Power\n'
        '8y  ₹ 12 Lacs  Gurugram\nNotice period: 1 month\nPhone 9811122334\nKey skills: Solar O&M, SCADA')


# ── pure ─────────────────────────────────────────────────────────────────
def test_find_contacts_and_clean_fields(srv):
    from modules.assistant import find_contacts, clean_fields
    assert find_contacts('call +91-98111 22334 or x@y.co') == ('9811122334', 'x@y.co')
    assert find_contacts('', 'Mobile 07011223344') == ('7011223344', '')
    f = clean_fields({'name': 'A', 'experience': '8.5', 'notice_period': '30', 'key_skills': 'SCADA, O&M',
                      'company': 'null', 'work_history': [{'company': 'X', 'designation': 'Y', 'duration': '2y'}, 'junk']})
    assert f['experience'] == 8.5 and f['notice_period'] == 30 and f['key_skills'] == ['SCADA', 'O&M']
    assert f['company'] == '' and len(f['work_history']) == 1


def test_split_by_lines(srv):
    from modules.assistant import split_by_lines
    text = 'Filters\nAsha Verma\nO&M Engineer at Azure Power 8y\nRavi Kumar\nSite Engineer at L&T 5 years\nFooter'
    p = split_by_lines(text, [{'name': 'Asha Verma', 'start': 2, 'end': 3}, {'name': 'Ravi Kumar', 'start': 4, 'end': 5},
                              {'name': 'Bad', 'start': 'x', 'end': 2}, {'name': 'Asha Verma', 'start': 2, 'end': 3}])
    assert [x['name'] for x in p] == ['Asha Verma', 'Ravi Kumar']
    assert 'Azure Power' in p[0]['text'] and 'Ravi' not in p[0]['text'] and p[0]['key'] != p[1]['key']


# ── routes ───────────────────────────────────────────────────────────────
def test_parse_page_and_cv_redacts_before_ai(srv, ds):
    ds.answer = {'name': 'Asha Verma', 'company': 'Azure Power', 'designation': 'Senior O&M Engineer',
                 'experience': 8, 'ctc_current': 12, 'notice_period': 30, 'location': 'Gurugram',
                 'key_skills': ['Solar O&M', 'SCADA']}
    c = cl(srv, ADMIN)
    r = c.post('/api/assistant/parse', data={'page_text': PAGE, 'cv': (_docx(CV), 'asha.docx')},
               content_type='multipart/form-data').get_json()
    assert r['ok'], r
    f = r['fields']
    assert f['phone'] == '9811122334' and f['email'] == 'asha.verma@example.com'
    assert f['company'] == 'Azure Power' and f['notice_period'] == 30 and r['has_cv'] and r['has_page']
    sent = json.dumps(ds.calls[-1]['payload'])
    assert '98111' not in sent and '9811122334' not in sent and 'asha.verma@' not in sent
    assert 'NAUKRI PAGE' in sent and 'CV:' in sent
    assert '98111' not in r['resume_text'] and 'SCADA' in r['resume_text']


def test_parse_cv_only_builds_profile_text(srv, ds):
    ds.answer = {'name': 'Asha Verma', 'company': 'Azure Power', 'designation': 'O&M Engineer', 'experience': 8,
                 'key_skills': ['SCADA']}
    r = cl(srv, ADMIN).post('/api/assistant/parse', data={'cv': (_docx(CV), 'a.docx')},
                            content_type='multipart/form-data').get_json()
    assert r['ok'] and not r['has_page'] and 'Azure Power' in r['candidate_text'] and 'SCADA' in r['candidate_text']


def test_parse_rejects_bad_input(srv, ds):
    c = cl(srv, ADMIN)
    assert c.post('/api/assistant/parse', data={'page_text': 'hi'}, content_type='multipart/form-data').status_code == 400
    j = c.post('/api/assistant/parse', data={'cv': (io.BytesIO(b'xx'), 'cv.png')}, content_type='multipart/form-data')
    assert j.status_code == 400 and j.get_json()['kind'] == 'cv'
    assert srv.app.test_client().post('/api/assistant/parse', data={'page_text': PAGE}).status_code == 401


def test_dupes_by_phone_email_and_scope(srv):
    _seed_dupes(srv)
    conn = srv.get_db()
    conn.execute("UPDATE candidates SET phone='+91 98111-22334', email='Asha.Verma@example.com' WHERE id=97201")
    conn.commit(); conn.close()
    c = cl(srv, ADMIN)
    j = c.post('/api/assistant/dupes', json={'mandate_id': M_A, 'name': 'A. Verma', 'phone': '9811122334'}).get_json()
    assert j['hits'][0]['candidate_id'] == 97201 and j['hits'][0]['level'] == 'exact' and j['hits'][0]['where'] == 'this_job'
    j = c.post('/api/assistant/dupes', json={'mandate_id': M_A, 'name': 'Someone', 'email': 'asha.verma@example.com'}).get_json()
    assert j['hits'][0]['candidate_id'] == 97201
    # same name, different company, other job -> not shown
    j = c.post('/api/assistant/dupes', json={'mandate_id': M_A, 'name': 'Ravi Kumar', 'company': 'Adani Green'}).get_json()
    assert j['hits'] == []
    j = c.post('/api/assistant/dupes', json={'mandate_id': M_A, 'name': 'Ravi Kumar', 'company': 'L&T Construction'}).get_json()
    assert j['hits'][0]['where'] == 'other_job' and j['hits'][0]['level'] == 'strong'
    # Riya cannot use a mandate that is not hers, and never sees Asha (M_A)
    assert cl(srv, RIYA).post('/api/assistant/dupes', json={'mandate_id': M_A, 'phone': '9811122334'}).status_code in (403, 404)
    j = cl(srv, RIYA).post('/api/assistant/dupes', json={'mandate_id': M_R, 'phone': '9811122334'}).get_json()
    assert j['hits'] == []


def test_split_route(srv, ds):
    page = 'Filters panel\nExperience 5-10\n' + 'Asha Verma\nO&M Engineer at Azure Power 8y 12 Lacs ph 9811122334\n' + \
           'Ravi Kumar\nSite Engineer at L&T Construction 5 years\nFooter links'
    ds.answer = {'profiles': [{'name': 'Asha Verma', 'start': 3, 'end': 4}, {'name': 'Ravi Kumar', 'start': 5, 'end': 6}]}
    j = cl(srv, ADMIN).post('/api/assistant/split', json={'text': page}).get_json()
    assert j['ok'] and j['count'] == 2 and '[phone]' in j['profiles'][0]['text']
    sent = json.dumps(ds.calls[-1]['payload'])
    assert '9811122334' not in sent and '3| Asha Verma' in sent
    ds.answer = {'profiles': []}
    assert cl(srv, ADMIN).post('/api/assistant/split', json={'text': page}).status_code == 422


def test_freelancers_cannot_use_assistant(srv):
    r = cl(srv, FL).post('/api/assistant/dupes', json={'mandate_id': M_A, 'phone': '9811122334'})
    assert r.status_code in (401, 403, 404)
