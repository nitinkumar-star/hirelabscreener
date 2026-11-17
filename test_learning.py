"""
Learning tab — modules/learning.py (Electrical Industry Explorer inside HireLab).

Run:   pytest test_learning.py -q
"""
import json
import re

from test_naukri_screen import srv, cl, ADMIN, RIYA, OADMIN  # noqa: F401  (fixtures)


def _state(html):
    return json.loads(re.search(r'<script id="state" type="application/json">(.*?)</script>', html, re.S).group(1))


def test_page_needs_sign_in(srv):
    r = srv.app.test_client().get('/learning')
    assert r.status_code == 302
    assert srv.app.test_client().put('/api/learning/state', json={'log': []}).status_code == 401


def test_page_has_menu_and_save_bridge(srv):
    html = cl(srv, RIYA).get('/learning').get_data(as_text=True)
    assert 'Electrical Industry Explorer' in html
    assert '/app-nav.js' in html                       # the app menu
    assert "fetch('/api/learning/state'" in html       # edits go to HireLab
    assert html.index("fetch('/api/learning/state'") < html.index('<script id="app-js">')


def test_edits_are_kept_per_agency(srv):
    entry = {'id': 'n1', 'date': '2026-11-17', 'co': 'siemens', 'text': 'Ask </script> entity', 'conf': 2}
    st = {'v': 1, 'log': [entry], 'matrix': {'mcb|schneider': 1}, 'pay': None}
    assert cl(srv, RIYA).put('/api/learning/state', json=st).get_json()['ok']
    # same agency: admin sees Riya's note, in the page and the API
    page = cl(srv, ADMIN).get('/learning').get_data(as_text=True)
    assert _state(page)['log'][0]['text'] == 'Ask </script> entity'
    assert '</script> entity' not in page                # escaped inside the JSON
    assert cl(srv, ADMIN).get('/api/learning/state').get_json()['state']['matrix'] == {'mcb|schneider': 1}
    # update replaces, does not duplicate
    st['log'] = []
    assert cl(srv, ADMIN).put('/api/learning/state', json=st).get_json()['ok']
    assert cl(srv, RIYA).get('/api/learning/state').get_json()['state']['log'] == []
    # another agency never sees it: it gets the page's own starting example
    other = _state(cl(srv, OADMIN).get('/learning').get_data(as_text=True))
    assert other['log'] and other['log'][0]['id'] == 'ex1'
    assert cl(srv, OADMIN).get('/api/learning/state').get_json()['state'] is None


def test_bad_state_refused(srv):
    c = cl(srv, RIYA)
    assert c.put('/api/learning/state', json={'log': 'x'}).status_code == 400
    assert c.put('/api/learning/state', data='nope', content_type='application/json').status_code == 400


def test_everything_is_editable(srv):
    html = cl(srv, RIYA).get('/learning').get_data(as_text=True)
    assert 'Edit content' in html and 'function saveDraft' in html
    for part in ('"TEXT"', '"COS"', '"MGROUPS"', '"SEGS"', '"POOLS"', '"PAY0"', '"GLOSS"', '"QS"', '"CITY"'):
        assert part in html


def test_edited_content_is_kept_per_agency(srv):
    co = {'id': 'hagerx', 'name': 'Hager Electro India', 'short': 'Hager', 'big': True, 'facts': [{'k': 'Rs 500 cr', 't': 'Revenue', 's': ''}]}
    st = {'v': 1, 'log': [], 'matrix': {}, 'pay': None,
          'data': {'COS': [co], 'TEXT': {'ov_h1': 'Industry ko jaano'}, 'MCOLS': ['hagerx'], 'SEGS': [{'id': 'mcb', 'n': 'MCB', 'r': [2]}]}}
    assert cl(srv, ADMIN).put('/api/learning/state', json=st).get_json()['ok']
    got = _state(cl(srv, RIYA).get('/learning').get_data(as_text=True))
    assert got['data']['COS'][0]['name'] == 'Hager Electro India'
    assert got['data']['TEXT']['ov_h1'] == 'Industry ko jaano'
    assert 'data' not in _state(cl(srv, OADMIN).get('/learning').get_data(as_text=True))
    assert cl(srv, RIYA).put('/api/learning/state', json={'log': [], 'data': ['x']}).status_code == 400
