"""
HireLab Academy  (modules/academy.py + academy_content.py)

  * the guides are complete and consistent (keys, sections, related links, roles)
  * who sees what: admins all, recruiters their guides, freelancers only theirs
  * agency notes: admin only, per agency
  * questions are answered from the visible guides only (no other guide, no candidate data),
    sources are limited to those guides, "not covered" and 👎 reach the admin list
  * daily limit; no AI key -> related guides without AI
  * tenant isolation

Run:   pytest test_academy.py -q
"""
import os
import sys
import json
import tempfile

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CO, OTHER = 94601, 94602
ADMIN, REC, FL, OADMIN = 9460101, 9460102, 9460103, 9460201


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='academy_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db(); c = conn.cursor(); now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", list(kw.values()))
    ins('companies', id=CO, name='Academy Co', status='active', created_at=now)
    ins('companies', id=OTHER, name='Other Agency', status='active', created_at=now)
    for uid, un, co, adm, role in ((ADMIN, 'ac_admin', CO, 1, 'user'), (REC, 'ac_rec', CO, 0, 'user'),
                                   (FL, 'ac_fl', CO, 0, 'freelancer_sourcer'), (OADMIN, 'ac_other', OTHER, 1, 'user')):
        ins('users', id=uid, username=un, password_hash='x', display_name=un.title(), role=role, status='approved',
            company_id=co, is_company_admin=adm, created_at=now)
    ins('candidates', id=9460501, mandate_id=0, name='ZZSECRET Candidate', owner_id=CO, stage='Screening', created_at=now)
    conn.commit(); conn.close()
    yield server


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


class _Resp:
    status_code = 200

    def __init__(self, body):
        self._b = body

    def json(self):
        return {'choices': [{'message': {'content': json.dumps(self._b)}}], 'usage': {}}


@pytest.fixture
def ai(srv, monkeypatch):
    """Fake DeepSeek: records the prompt, replies with whatever the test sets."""
    box = {'reply': {'answer': 'Do this.', 'sources': [], 'answered': True}, 'prompts': []}

    def fake(key, payload, **k):
        box['prompts'].append(payload['messages'][1]['content'])
        return _Resp(box['reply'])
    monkeypatch.setattr(srv, 'call_deepseek', fake)
    real = srv.get_setting
    monkeypatch.setattr(srv, 'get_setting', lambda k, d='': 'test-key' if k == 'deepseek_api_key' else real(k, d))
    return box


def test_content_is_complete():
    import academy_content as c
    keys = [a['key'] for a in c.ARTICLES]
    sections = {k for k, _t in c.SECTIONS}
    assert len(keys) == len(set(keys)) and len(keys) >= 45
    for a in c.ARTICLES:
        for f in ('key', 'title', 'section', 'roles', 'where', 'summary', 'when', 'steps', 'tips', 'related', 'keywords'):
            assert a.get(f) not in (None, ''), (a['key'], f)
        assert a['section'] in sections and set(a['roles']) <= set('ARF') and a['steps']
        assert all(r in keys for r in a['related']), a['key']
    for k in sections:
        assert any(a['section'] == k for a in c.ARTICLES), k
    # every main feature has a guide
    for k in ('jobs', 'pipeline', 'reasons', 'check-630', 'talent', 'candidate-profile', 'profile-groups', 'market-note',
              'call-analysis', 'ai-agent', 'assistant', 'crm', 'bd', 'bd-calling', 'scheduler', 'talent-pool', 'reengage',
              'jobmail', 'hotlist', 'market-pulse', 'email-box', 'company-talent-map', 'company-intel', 'market-intel',
              'talent-pulse', 'invoicing', 'finance', 'tasks', 'team-chat', 'team-activity', 'freelancers', 'org-map',
              'learning', 'backup', 'academy-help'):
        assert k in keys, k


def test_who_sees_what(srv):
    a = cl(srv, ADMIN).get('/api/academy/articles').get_json()
    r = cl(srv, REC).get('/api/academy/articles').get_json()
    f = cl(srv, FL).get('/api/academy/articles')
    assert f.status_code == 200
    f = f.get_json()
    import academy_content as c
    assert a['is_admin'] and len(a['articles']) == len([x for x in c.ARTICLES if 'A' in x['roles']])
    rk = {x['key'] for x in r['articles']}
    assert 'market-intel' in rk and 'pipeline' in rk
    assert 'finance' not in rk and 'team-activity' not in rk and not r['is_admin']
    fk = {x['key'] for x in f['articles']}
    assert fk == {x['key'] for x in c.ARTICLES if 'F' in x['roles']} and 'freelancer-guide' in fk and 'pipeline' not in fk
    for x in r['articles']:                                   # related links never point to hidden guides
        assert all(k in rk for k in x['related'])


def test_agency_notes(srv):
    assert cl(srv, REC).put('/api/academy/notes/pipeline', json={'note': 'x'}).status_code == 403
    assert cl(srv, ADMIN).put('/api/academy/notes/nope', json={'note': 'x'}).status_code == 404
    assert cl(srv, ADMIN).put('/api/academy/notes/pipeline', json={'note': 'For L&K ask notice first'}).get_json()['ok']
    p = next(x for x in cl(srv, REC).get('/api/academy/articles').get_json()['articles'] if x['key'] == 'pipeline')
    assert p['note'] == 'For L&K ask notice first'
    o = next(x for x in cl(srv, OADMIN).get('/api/academy/articles').get_json()['articles'] if x['key'] == 'pipeline')
    assert o['note'] == ''


def test_retrieval_finds_the_right_guide():
    from modules import academy
    import academy_content as c
    top = lambda q: [a['key'] for a in academy.retrieve(q, c.ARTICLES)]
    assert top('client rejection ka feedback kahan likhun')[0] == 'reasons'
    assert 'call-analysis' in top('call recording upload kaise kare')[:2]
    assert 'invoicing' in top('invoice kaise banaye GST')[:2]
    assert 'profile-groups' in top('production profile group fields')[:2]
    assert top('kya') == []


def test_ask_uses_only_visible_guides(srv, ai):
    ai['reply'] = {'answer': 'Settings → Profile groups.', 'sources': ['profile-groups', 'finance', 'made-up'], 'answered': True}
    r = cl(srv, REC).post('/api/academy/ask', json={'question': 'How do I add a production profile group?'}).get_json()
    assert r['ok'] and r['ai'] and r['answered']
    assert [s['key'] for s in r['sources']] == ['profile-groups']          # hidden / unknown keys dropped
    prompt = ai['prompts'][-1]
    assert 'GUIDE profile-groups' in prompt
    assert 'finance:' not in prompt and 'Profit & Cash' not in prompt          # admin-only guide not even in the index
    assert 'ZZSECRET' not in prompt                                             # never candidate data
    assert 'production profile group' in prompt


def test_not_covered_and_thumbs_down_reach_admins(srv, ai):
    ai['reply'] = {'answer': 'The Academy does not cover this yet.', 'sources': [], 'answered': False}
    r1 = cl(srv, REC).post('/api/academy/ask', json={'question': 'How do I connect SAP payroll?'}).get_json()
    assert r1['answered'] is False
    ai['reply'] = {'answer': 'Open Tasks.', 'sources': ['tasks'], 'answered': True}
    r2 = cl(srv, REC).post('/api/academy/ask', json={'question': 'How do I add a task for tomorrow?'}).get_json()
    assert r2['answered'] is True
    assert cl(srv, ADMIN).post('/api/academy/ask/%d/feedback' % r2['id'], json={'helpful': -1}).status_code == 404  # not their question
    assert cl(srv, REC).post('/api/academy/ask/%d/feedback' % r2['id'], json={'helpful': -1}).get_json()['ok']
    assert cl(srv, REC).get('/api/academy/unanswered').status_code == 403
    qs = cl(srv, ADMIN).get('/api/academy/unanswered').get_json()['questions']
    ids = {q['id']: q for q in qs}
    assert ids[r1['id']]['not_covered'] and ids[r2['id']]['thumbs_down']
    assert cl(srv, OADMIN).get('/api/academy/unanswered').get_json()['questions'] == []        # other agency
    assert cl(srv, OADMIN).post('/api/academy/unanswered/%d/done' % r1['id']).status_code == 404
    assert cl(srv, ADMIN).post('/api/academy/unanswered/%d/done' % r1['id']).get_json()['ok']
    assert r1['id'] not in {q['id'] for q in cl(srv, ADMIN).get('/api/academy/unanswered').get_json()['questions']}


def test_freelancer_can_ask_only_their_guides(srv, ai):
    ai['reply'] = {'answer': 'Push from the Assistant.', 'sources': ['assistant', 'pipeline'], 'answered': True}
    r = cl(srv, FL).post('/api/academy/ask', json={'question': 'How do I upload a CV from Naukri?'}).get_json()
    assert r['ok'] and [s['key'] for s in r['sources']] == ['assistant']
    assert 'GUIDE pipeline' not in ai['prompts'][-1]


def test_daily_limit_and_bad_input(srv, ai, monkeypatch):
    from modules import academy
    assert cl(srv, ADMIN).post('/api/academy/ask', json={'question': 'a'}).status_code == 400
    monkeypatch.setattr(academy, 'DAILY_LIMIT', 1)
    c = cl(srv, ADMIN)
    assert c.post('/api/academy/ask', json={'question': 'How does Job Pulse work?'}).status_code == 200
    r = c.post('/api/academy/ask', json={'question': 'How does Job Pulse work again?'})
    assert r.status_code == 429 and 'limit' in r.get_json()['error']


def test_without_ai_key_shows_related_guides(srv, monkeypatch):
    real = srv.get_setting
    monkeypatch.setattr(srv, 'get_setting', lambda k, d='': '' if k == 'deepseek_api_key' else real(k, d))
    r = cl(srv, REC).post('/api/academy/ask', json={'question': 'call recording upload'}).get_json()
    assert r['ok'] and r['ai'] is False and 'call-analysis' in [s['key'] for s in r['sources']]


def test_page_and_menu(srv):
    r = cl(srv, REC).get('/academy')
    assert r.status_code == 200 and b'Ask a question' in r.data
    assert cl(srv, REC).get('/academy').status_code == 200
    html = open(os.path.join(ROOT, 'index.html'), encoding='utf-8').read()
    assert 'id="sb-academy"' in html and "['sb-academy'" in html
    for f, key in (('talent-map.html', 'company-talent-map'), ('market-intel.html', 'market-intel'),
                   ('newsletter.html', 'market-pulse'), ('talent-pool.html', 'talent-pool'), ('hotlist.html', 'hotlist')):
        assert '/academy#' + key in open(os.path.join(ROOT, f), encoding='utf-8').read()
