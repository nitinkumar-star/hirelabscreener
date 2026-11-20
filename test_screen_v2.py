"""
Screener v2 (Nov 2026) — splitter that does not lose cards, Job Breakdown v2
(companies + designations), code-read card facts + one scoring formula, and
the training loop (recruiter ✓/✗ -> prompt, rule ideas, export).

Run:   pytest test_screen_v2.py -q
Real server.py and migrations; DeepSeek is a fake that records what was sent.
"""
import os
import sys
import json
import tempfile

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CO, OTHER = 8741, 8742
ADMIN, RIYA, OADMIN = 87401, 87402, 87403
M_A, M_R, M_O = 874101, 874102, 874103


class FakeResp:
    def __init__(self, obj, status=200):
        self.status_code = status
        self._obj = obj

    def json(self):
        return {'choices': [{'message': {'content': json.dumps(self._obj)}}], 'usage': {}}


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='screen2_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db(); c = conn.cursor(); now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", list(kw.values()))

    ins('companies', id=CO, name='V2 Co', status='active', created_at=now)
    ins('companies', id=OTHER, name='V2 Other', status='active', created_at=now)
    for uid, un, co, adm in ((ADMIN, 'sv2_admin', CO, 1), (RIYA, 'sv2_riya', CO, 0), (OADMIN, 'sv2_other', OTHER, 1)):
        ins('users', id=uid, username=un, password_hash='x', display_name=un, role='user', status='approved',
            company_id=co, is_company_admin=adm, created_at=now)
    for mid, role, who, co in ((M_A, 'LV Switchgear Sales Engineer', ADMIN, CO), (M_R, 'Riya Job', RIYA, CO),
                               (M_O, 'Other Job', OADMIN, OTHER)):
        ins('mandates', id=mid, role=role, client='Schneider Electric', status='active', owner_id=co,
            assigned_user_id=who, created_at=now, jd=role + ' — sells LV switchgear to panel builders in NCR',
            ctc_min=6, ctc_max=12, exp_min=4, exp_max=8, location='Noida')
    c.execute("INSERT OR IGNORE INTO mandate_assignees (company_id, mandate_id, user_id, is_primary, added_by, added_at, is_active) "
              "VALUES (?,?,?,1,?,?,1)", (CO, M_R, RIYA, RIYA, now))
    # a person already moved forward on this job: an example for the breakdown
    ins('candidates', id=874901, mandate_id=M_A, name='Liked Person', company='Legrand India', designation='Sales Engineer',
        experience=6, stage='Shortlisted', owner_id=CO, created_at=now)
    conn.commit(); conn.close()
    yield server


@pytest.fixture
def ds(srv, monkeypatch):
    class D:
        calls = []
        answer = None

        def __call__(self, key, payload, timeout=60, endpoint='deepseek'):
            self.calls.append({'key': key, 'payload': payload, 'endpoint': endpoint})
            a = self.answer(payload, endpoint) if callable(self.answer) else self.answer
            return FakeResp(a)
    d = D()
    d.calls = []
    monkeypatch.setattr(srv, 'call_deepseek', d)
    real = srv.get_setting
    monkeypatch.setattr(srv, 'get_setting', lambda k, default='': 'test-key' if k == 'deepseek_api_key' else real(k, default))
    return d


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


BREAKDOWN = {
    'summary': 'Sells LV switchgear to panel builders in NCR.',
    'titles': ['Sales Engineer', 'Sr. Sales Engineer', 'Area Sales Manager'],
    'companies': {'target': ['ABB', 'Siemens', 'Legrand', 'Lauritz Knudsen'], 'ok': ['Havells'],
                  'avoid': ['Schneider Electric', 'Legrand']},      # Legrand in two lists: avoid wins
    'core': [{'skill': 'LV switchgear sales', 'why': 'the job', 'equivalents': ['MCB/MCCB sales']}],
    'important': [{'skill': 'Panel builder network'}], 'nice': [],
    'limits': {'locations': ['Noida', 'Delhi']}, 'not_this': ['Retail electrician supplies counter sales'],
}


# ══════════════════════════════════════════════════════════════════════════
#  card facts (pure)
# ══════════════════════════════════════════════════════════════════════════
def test_parse_numbers():
    from modules import card_facts as cf
    assert cf.parse_exp('Rahul\n5y 6m  ₹ 7.5 Lacs  Noida') == 5.5
    assert cf.parse_exp('6.5 Years exp') == 6.5
    assert cf.parse_exp('8 Yrs 2 Mos') == pytest.approx(8.2, abs=0.05)
    assert cf.parse_exp('no numbers') is None
    assert cf.parse_ctc('₹ 7.5 Lacs') == 7.5 and cf.parse_ctc('12 LPA') == 12 and cf.parse_ctc('1.2 Crore') == 120
    assert cf.parse_ctc('CTC: Not disclosed') is None
    assert cf.parse_notice('Notice period: 30 days') == 30 and cf.parse_notice('Serving notice') == 0
    assert cf.parse_notice('notice 2 months') == 60 and cf.parse_notice('nothing') is None


def test_company_class_uses_current_employer():
    from modules import card_facts as cf
    co = {'target': ['ABB', 'Siemens'], 'ok': ['Havells'], 'avoid': ['Schneider Electric']}
    assert cf.company_class('Amit\nCurrent: Sales Engineer at ABB India Ltd\nPrevious: Havells', co) == ('target', 'ABB')
    assert cf.company_class('Amit\nCurrent: Sales Engineer at Schneider Electric\nPrevious: ABB', co)[0] == 'avoid'
    # past stint at an avoided company is only a flag
    assert cf.company_class('Amit\nCurrent: Sales Engr at Polycab\nPrevious: Schneider Electric', co)[0] == 'avoid_past'
    # past stint at a target company is worth something
    assert cf.company_class('Amit\nCurrent: Sales Engr at Polycab\nPrevious: Siemens Ltd', co)[0] == 'ok'
    assert cf.company_class('Amit\nCurrent: Sales Engr at Polycab', co) == ('', '')
    # "ABB" inside another word does not count
    assert cf.company_class('Current: Engineer at Abbott Labs', co) == ('', '')


def test_combine_is_one_formula_and_hard_rules():
    from modules import card_facts as cf
    lim = {'exp_min': 4, 'exp_max': 8, 'ctc_max': 12, 'locations': ['Noida']}
    good = {'role_fit': 4, 'domain_fit': 4, 'core_fit': 3, 'reason': 'fits'}
    f = {'exp': 6, 'ctc': 10, 'company': 'target', 'company_name': 'ABB', 'location_ok': True}
    r = cf.combine(good, f, lim)
    assert r['score'] == 100 and r['verdict'] == 'open' and r['flags'][0] == 'target co.'
    # same AI answer + same facts -> same score, whatever batch it came in
    assert cf.combine(dict(good), dict(f), lim)['score'] == r['score']
    over = dict(f, ctc=15)
    r2 = cf.combine(good, over, lim)
    assert r2['verdict'] == 'skip' and r2['reason'].startswith('CTC above budget') and r2['score'] < cf.MAYBE_AT
    assert cf.combine(good, over, lim, budget_relaxed=True)['verdict'] == 'open'
    av = dict(f, company='avoid', company_name='Schneider Electric')
    assert cf.combine(good, av, lim)['verdict'] == 'skip'
    assert cf.combine(dict(good, role_fit=0), f, lim)['verdict'] == 'skip'
    assert cf.combine(dict(good, rule_break='No EPC'), f, lim)['reason'].startswith('Breaks your rule')
    mid = cf.combine({'role_fit': 2, 'domain_fit': 2, 'core_fit': 1}, {'exp': 6, 'ctc': None, 'company': '', 'location_ok': True}, lim)
    assert mid['verdict'] == 'maybe', mid


def test_anchor_lines_count_cards():
    from modules import card_facts as cf
    page = 'Filters\nExperience 2-10\n' + ''.join(
        'Person %d\n%dy %dm  ₹ %d Lacs  Noida\nCurrent: Engineer at Co%d\nKey skills: MCB\n' % (i, 3 + i % 5, i % 11, 6 + i % 4, i)
        for i in range(12)) + 'Footer'
    assert cf.estimate_cards(page) == 12


# ══════════════════════════════════════════════════════════════════════════
#  splitter
# ══════════════════════════════════════════════════════════════════════════
def test_chunk_windows_cover_every_line_once():
    from modules.assistant import chunk_windows
    w = chunk_windows(1000, size=180, overlap=60, pre=8)
    owned = [ln for (a, b, _ws, _we) in w for ln in range(a, b + 1)]
    assert owned == list(range(1, 1001))
    assert all(ws <= a and we >= b for (a, b, ws, we) in w)


def test_tidy_spans_cuts_overlaps():
    from modules.assistant import tidy_spans
    t = tidy_spans([{'name': 'B', 'start': 5, 'end': 9}, {'name': 'A', 'start': 1, 'end': 7}, {'name': 'A', 'start': 1, 'end': 3}], 20)
    assert [(x['start'], x['end']) for x in t] == [(1, 4), (5, 9)]


def _big_page(n):
    lines = ['Search results', 'Filters: Experience 4-8, Noida']
    for i in range(n):
        lines += ['Cand Name%02d' % i, '%dy %dm  ₹ %d Lacs  Noida' % (4 + i % 4, i % 11, 7 + i % 3),
                  'Current: Sales Engineer at Co%02d' % i, 'Previous: Engineer at Old%02d' % i,
                  'Education: B.Tech', 'Key skills: MCB, MCCB, ACB', 'Pref. Location: Noida, Delhi']
    lines.append('Footer links')
    return '\n'.join(lines)


def test_split_long_page_uses_windows_and_recovers_missed_cards(srv, ds):
    page = _big_page(50)          # ~352 lines -> 2 windows
    lines = page.split('\n')

    def answer(payload, endpoint):
        user = payload['messages'][-1]['content']
        nums = [int(l.split('|')[0]) for l in user.split('\n') if '|' in l]
        out = []
        for n in nums:
            l = lines[n - 1]
            if l.startswith('Cand Name'):
                idx = int(l[-2:])
                if idx in (7, 31):          # the AI "forgets" two cards
                    continue
                out.append({'name': l, 'start': n, 'end': n + 6})
        return {'profiles': out}
    ds.answer = answer
    j = cl(srv, ADMIN).post('/api/assistant/split', json={'text': page}).get_json()
    assert j['ok'], j
    assert len([c for c in ds.calls if c['endpoint'] == 'assistant-split']) >= 2
    assert j['count'] == 50 and j['expected'] == 50 and j['recovered'] == 2, (j['count'], j['recovered'])
    texts = [p['text'] for p in j['profiles']]
    assert any('Co07' in t for t in texts) and any('Co31' in t for t in texts)
    assert len({p['key'] for p in j['profiles']}) == 50 and not j['warnings']
    # every card holds only its own text
    p7 = [p for p in j['profiles'] if 'Co07' in p['text']][0]
    assert p7['text'].startswith('Cand Name07') and 'Cand Name08' not in p7['text']


def test_split_warns_when_cards_are_missing(srv, ds):
    page = _big_page(6) + '\n' + '\n'.join('%dy 1m ₹ 5 Lacs' % (i + 3) for i in range(0))
    ds.answer = {'profiles': [{'name': 'Cand Name00', 'start': 3, 'end': 9}]}
    # break the summary lines of cards 1..5 so recovery cannot find them, but
    # keep enough anchors for an estimate
    j = cl(srv, ADMIN).post('/api/assistant/split', json={'text': page}).get_json()
    assert j['ok'] and j['expected'] == 6
    assert j['count'] == 6       # recovered from the summary lines
    assert j['recovered'] == 5


# ══════════════════════════════════════════════════════════════════════════
#  Job Breakdown v2
# ══════════════════════════════════════════════════════════════════════════
def test_clean_profile_keeps_companies_and_titles(srv):
    from modules.job_profile import clean_profile, profile_text
    p = clean_profile(BREAKDOWN)
    assert p['companies']['avoid'] == ['Schneider Electric', 'Legrand']
    assert 'Legrand' not in p['companies']['target'] and p['titles'][0] == 'Sales Engineer' and p['v'] == 2
    t = profile_text(p)
    assert 'TARGET COMPANIES' in t and 'ABB' in t and 'AVOID' in t and 'DESIGNATIONS THAT FIT' in t
    assert clean_profile({'companies': {'target': 'ABB, Siemens\nHavells'}})['companies']['target'] == ['ABB', 'Siemens', 'Havells']


def test_generate_uses_liked_people_and_new_prompt(srv, ds):
    ds.answer = BREAKDOWN
    j = cl(srv, ADMIN).post('/api/job-profile/%d/generate' % M_A).get_json()
    assert j['ok'] and j['profile']['companies']['target'][0] == 'ABB'
    call = [c for c in ds.calls if c['endpoint'] == 'job-breakdown'][-1]
    assert 'Sales Engineer at Legrand India' in call['payload']['messages'][-1]['content']
    assert 'Naukri search-result' in call['payload']['messages'][0]['content']


def test_v1_ai_breakdown_is_upgraded_once_but_edited_is_kept(srv, ds):
    conn = srv.get_db()
    from modules import job_profile as jp
    from modules.shared import effective_company_id  # noqa
    old = {'summary': 'old', 'core': [{'skill': 'X', 'equivalents': []}], 'important': [], 'nice': [], 'limits': {}, 'not_this': []}
    core = srv
    m = conn.execute('SELECT * FROM mandates WHERE id=?', (M_R,)).fetchone()
    h = __import__('hashlib').sha1(jp.source_text(core, m).encode('utf-8')).hexdigest()
    conn.execute("INSERT OR REPLACE INTO mandate_job_profile (company_id, mandate_id, profile, source_hash, status) VALUES (?,?,?,?,'ai')",
                 (CO, M_R, json.dumps(old), h))
    conn.commit(); conn.close()
    ds.answer = BREAKDOWN
    j = cl(srv, RIYA).get('/api/job-profile/%d?ensure=1' % M_R).get_json()
    assert j['profile'].get('v') == 2 and j['profile']['companies']['target']
    n = len(ds.calls)
    cl(srv, RIYA).get('/api/job-profile/%d?ensure=1' % M_R)
    assert len(ds.calls) == n            # not regenerated again
    # an edited v1 breakdown is never replaced automatically
    conn = srv.get_db()
    conn.execute("UPDATE mandate_job_profile SET profile=?, status='edited' WHERE mandate_id=?", (json.dumps(old), M_R))
    conn.commit(); conn.close()
    j = cl(srv, RIYA).get('/api/job-profile/%d?ensure=1' % M_R).get_json()
    assert j['status'] == 'edited' and j['profile']['summary'] == 'old' and len(ds.calls) == n


def test_failed_upgrade_keeps_old_breakdown(srv, ds):
    conn = srv.get_db()
    from modules import job_profile as jp
    old = {'summary': 'v1 kept', 'core': [{'skill': 'X', 'equivalents': []}], 'important': [], 'nice': [], 'limits': {}, 'not_this': []}
    m = conn.execute('SELECT * FROM mandates WHERE id=?', (M_R,)).fetchone()
    h = __import__('hashlib').sha1(jp.source_text(srv, m).encode('utf-8')).hexdigest()
    conn.execute("UPDATE mandate_job_profile SET profile=?, status='ai', source_hash=? WHERE mandate_id=?", (json.dumps(old), h, M_R))
    conn.commit(); conn.close()
    ds.answer = 'not json'
    j = cl(srv, RIYA).get('/api/job-profile/%d?ensure=1' % M_R).get_json()
    assert j['ok'] and j['profile']['summary'] == 'v1 kept'


# ══════════════════════════════════════════════════════════════════════════
#  Screener v2 route
# ══════════════════════════════════════════════════════════════════════════
def _save_breakdown(srv):
    j = cl(srv, ADMIN).put('/api/job-profile/%d' % M_A, json={'profile': BREAKDOWN}).get_json()
    assert j['ok'], j


CARDS = [
    {'key': 'a', 'name': 'Amit', 'text': 'Amit Kumar\n6y 2m  ₹ 9 Lacs  Noida\nCurrent: Sales Engineer at ABB India\nKey skills: MCB, MCCB'},
    {'key': 'b', 'name': 'Bina', 'text': 'Bina Rao\n5y 0m  ₹ 8 Lacs  Noida\nCurrent: Sales Engineer at Schneider Electric\nKey skills: ACB'},
    {'key': 'c', 'name': 'Chetan', 'text': 'Chetan S\n7y 0m  ₹ 18 Lacs  Delhi\nCurrent: Area Sales Manager at Siemens\nKey skills: LV'},
    {'key': 'd', 'name': 'Dev', 'text': 'Dev P\n6y 0m  ₹ 7 Lacs  Noida\nCurrent: Site Engineer at Some Builders\nKey skills: civil'},
]


def rubric(payload, endpoint):
    user = payload['messages'][-1]['content']
    out = []
    for block in user.split('[key: ')[1:]:
        k = block.split(']')[0]
        civil = 'civil' in block
        out.append({'key': k, 'role_fit': 0 if civil else 4, 'domain_fit': 1 if civil else 4, 'core_fit': 0 if civil else 3,
                    'rule_break': '', 'budget_ok_by_rule': False, 'reason': 'r-' + k, 'flags': []})
    return {'results': out}


def test_screen_v2_scores_with_code_facts(srv, ds):
    _save_breakdown(srv)
    ds.answer = rubric
    j = cl(srv, ADMIN).post('/api/extension/screen', json={'mandate_id': M_A, 'profiles': CARDS}).get_json()
    assert j['ok'], j
    r = {x['key']: x for x in j['results']}
    assert r['a']['verdict'] == 'open' and r['a']['score'] == 100 and r['a']['facts']['company'] == 'target'
    assert r['b']['verdict'] == 'skip' and 'avoid' in r['b']['reason'].lower()
    assert r['c']['verdict'] == 'skip' and r['c']['reason'].startswith('CTC above budget')
    assert r['d']['verdict'] == 'skip'
    call = [c for c in ds.calls if c['endpoint'] == 'naukri-screen'][-1]
    sysmsg, user = call['payload']['messages'][0]['content'], call['payload']['messages'][-1]['content']
    assert 'role_fit (0-4)' in sysmsg and 'TARGET COMPANIES' in sysmsg and 'never relative' in sysmsg
    assert 'CODE-READ FACTS: exp 6.2 yrs; CTC 9 LPA; TARGET company: ABB' in user


def test_budget_rule_lets_over_budget_through(srv, ds):
    c = cl(srv, ADMIN)
    c.post('/api/extension/screen/rules', json={'mandate_id': M_A, 'rules': ['Budget may stretch to 20 LPA for Siemens/ABB people']})

    def ans(payload, endpoint):
        a = rubric(payload, endpoint)
        for it in a['results']:
            it['budget_ok_by_rule'] = it['key'] == 'c'
        return a
    ds.answer = ans
    r = {x['key']: x for x in c.post('/api/extension/screen', json={'mandate_id': M_A, 'profiles': CARDS}).get_json()['results']}
    assert r['c']['verdict'] == 'open' and 'over budget (rule allows)' in r['c']['flags']
    c.post('/api/extension/screen/rules', json={'mandate_id': M_A, 'rules': []})


# ══════════════════════════════════════════════════════════════════════════
#  training loop
# ══════════════════════════════════════════════════════════════════════════
def test_agreement_helper():
    from modules.screen_feedback import agreement, card_hash
    assert agreement([('open', 'open'), ('skip', 'open'), ('maybe', 'skip'), ('skip', 'skip'), ('dup', 'open')]) == (2, 3)
    assert card_hash('A', 'x  y') == card_hash('a', 'X y')


def test_feedback_save_stats_and_prompt(srv, ds):
    c = cl(srv, ADMIN)
    items = [dict(key=x['key'], name=x['name'], text=x['text'] + ' ph 9876543210', ai_verdict=v, ai_score=s, ai_reason='r')
             for x, v, s in zip(CARDS, ['open', 'skip', 'skip', 'skip'], [100, 40, 40, 10])]
    items[0]['my_verdict'] = 'open'
    items[1]['my_verdict'] = 'open'; items[1]['reason'] = 'Schneider people are fine for this client'
    items[3]['my_verdict'] = 'skip'
    j = c.post('/api/extension/screen/feedback', json={'mandate_id': M_A, 'items': items}).get_json()
    assert j['ok'] and j['saved'] == 4
    st = j['stats']
    assert st['this_job'] == 3 and st['agree_job_pct'] == 67 and st['disagree_job'] == 1 and st['target'] == 3000
    conn = srv.get_db()
    assert '9876543210' not in json.dumps([dict(r) for r in conn.execute('SELECT * FROM screen_feedback').fetchall()])
    conn.close()
    # same card again -> updated, not duplicated; downloaded marks it open
    j = c.post('/api/extension/screen/feedback', json={'mandate_id': M_A, 'items': [
        dict(key='c', name='Chetan', text=CARDS[2]['text'] + ' ph 9876543210', ai_verdict='skip', ai_score=40, downloaded=True)]}).get_json()
    assert j['stats']['this_job'] == 4 and j['stats']['downloaded'] == 1
    # the next screening shows the recruiter's decisions, disagreements first
    ds.answer = rubric
    c.post('/api/extension/screen', json={'mandate_id': M_A, 'profiles': CARDS[:1]})
    sysmsg = [x for x in ds.calls if x['endpoint'] == 'naukri-screen'][-1]['payload']['messages'][0]['content']
    assert "RECRUITER'S OWN PAST DECISIONS" in sysmsg
    i_dis = sysmsg.index('Bina Rao')
    assert 'Schneider people are fine' in sysmsg and i_dis < sysmsg.index('Dev P')


def test_suggest_rules_needs_disagreements_then_proposes(srv, ds):
    c = cl(srv, ADMIN)
    j = c.post('/api/extension/screen/feedback/suggest', json={'mandate_id': M_A}).get_json()
    assert j['ok'] and j['rules'] == [] and 'at least 3' in j['note']
    more = [dict(key='x%d' % i, name='N%d' % i, text='Person %d\n5y  ₹ 8 Lacs\nCurrent: Sales Engineer at Schneider Electric' % i,
                 ai_verdict='skip', ai_score=30, my_verdict='open') for i in range(3)]
    c.post('/api/extension/screen/feedback', json={'mandate_id': M_A, 'items': more})
    ds.answer = {'rules': [{'rule': 'Schneider Electric employees are allowed for this job', 'why': 'you opened 4 of them'},
                           {'rule': 'Budget may stretch to 20 LPA for Siemens/ABB people', 'why': 'dup of nothing'}]}
    j = c.post('/api/extension/screen/feedback/suggest', json={'mandate_id': M_A}).get_json()
    assert j['ok'] and j['rules'][0]['rule'].startswith('Schneider') and j['based_on'] >= 3
    sent = [x for x in ds.calls if x['endpoint'] == 'screen-suggest'][-1]['payload']['messages'][-1]['content']
    assert 'RECRUITER said OPEN' in sent


def test_feedback_access_and_export(srv, ds):
    # other company cannot write to this job
    r = cl(srv, OADMIN).post('/api/extension/screen/feedback', json={'mandate_id': M_A, 'items': [
        dict(key='z', name='Z', text='some card text long enough')]})
    assert r.status_code == 404
    # recruiter without assignment cannot use the admin's job
    r = cl(srv, RIYA).post('/api/extension/screen/feedback', json={'mandate_id': M_A, 'items': [
        dict(key='z', name='Z', text='some card text long enough')]})
    assert r.status_code in (403, 404)
    assert cl(srv, RIYA).get('/api/extension/screen/feedback/export').status_code == 403
    r = cl(srv, ADMIN).get('/api/extension/screen/feedback/export?mandate_id=%d' % M_A)
    assert r.status_code == 200 and 'attachment' in r.headers['Content-Disposition']
    rows = [json.loads(l) for l in r.get_data(as_text=True).strip().split('\n')]
    assert rows and all(x['label'] in ('open', 'skip') for x in rows)
    assert rows[0]['role'] == 'LV Switchgear Sales Engineer' and 'TARGET COMPANIES' in rows[0]['breakdown']
    assert cl(srv, OADMIN).get('/api/extension/screen/feedback/export').get_data(as_text=True).strip() == ''
    assert srv.app.test_client().get('/api/extension/screen/feedback/stats').status_code == 401


def test_split_parallel_windows_with_slow_ai(srv, ds):
    """Regression (Nov 18 2026): live DeepSeek calls overlap in time; a single
    copied request context shared by all threads raised
    'ValueError: Token was created in a different Context' -> 500."""
    import time
    page = _big_page(120)                 # ~840 lines -> 5 windows, 4 threads
    lines = page.split('\n')

    def answer(payload, endpoint):
        time.sleep(0.3)
        out = []
        for l in payload['messages'][-1]['content'].split('\n'):
            if '|' in l:
                n = int(l.split('|')[0])
                if lines[n - 1].startswith('Cand Name'):
                    out.append({'name': lines[n - 1], 'start': n, 'end': n + 6})
        return {'profiles': out}
    ds.answer = answer
    r = cl(srv, ADMIN).post('/api/assistant/split', json={'text': page})
    j = r.get_json()
    assert r.status_code == 200 and j['ok'] and j['count'] == 120, j


# ── Nov 20 2026: one slow DeepSeek answer used to fail a whole batch (HTTP 502) ──
def _cards(n):
    return [{'key': 'p%d' % i, 'text': 'Sales Engineer at ABB, 6 yrs, 9 Lacs, LV switchgear to panel builders #%d' % i}
            for i in range(n)]


def test_batch_is_split_into_small_calls(srv, ds):
    ds.answer = rubric
    j = cl(srv, ADMIN).post('/api/extension/screen', json={'mandate_id': M_A, 'profiles': _cards(12)}).get_json()
    assert j['ok'] and [r['key'] for r in j['results']] == ['p%d' % i for i in range(12)]
    calls = [c for c in ds.calls if c['endpoint'] == 'naukri-screen']
    assert len(calls) == 3 and all(c['payload']['messages'][-1]['content'].count('[key: ') <= 5 for c in calls)


def test_slow_chunk_is_retried_once(srv, ds):
    seen = {}

    def ans(payload, endpoint):
        user = payload['messages'][-1]['content']
        if endpoint == 'naukri-screen' and '[key: p0]' in user and not seen.get('p0'):
            seen['p0'] = 1
            raise TimeoutError('read timed out')
        return rubric(payload, endpoint)
    ds.answer = ans
    r = cl(srv, ADMIN).post('/api/extension/screen', json={'mandate_id': M_A, 'profiles': _cards(10)})
    j = r.get_json()
    assert r.status_code == 200 and all(x['verdict'] != 'unknown' for x in j['results']), j


def test_failed_chunk_does_not_lose_the_rest(srv, ds):
    def ans(payload, endpoint):
        if endpoint == 'naukri-screen' and '[key: p0]' in payload['messages'][-1]['content']:
            raise TimeoutError('read timed out')
        return rubric(payload, endpoint)
    ds.answer = ans
    r = cl(srv, ADMIN).post('/api/extension/screen', json={'mandate_id': M_A, 'profiles': _cards(10)})
    j = r.get_json()
    assert r.status_code == 200 and j['ok'], j
    v = {x['key']: x['verdict'] for x in j['results']}
    assert all(v['p%d' % i] == 'unknown' for i in range(5)) and all(v['p%d' % i] != 'unknown' for i in range(5, 10))
    assert 'Press Screen again' in j['results'][0]['reason']


def test_all_chunks_failing_is_still_an_error(srv, ds):
    def ans(payload, endpoint):
        if endpoint == 'naukri-screen':
            raise TimeoutError('read timed out')
        return rubric(payload, endpoint)
    ds.answer = ans
    r = cl(srv, ADMIN).post('/api/extension/screen', json={'mandate_id': M_A, 'profiles': _cards(10)})
    assert r.status_code == 502 and 'TimeoutError' in r.get_json()['error']
