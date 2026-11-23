"""
Skill Engine, Phase 1  (modules/skill_engine.py + skill_taxonomy.py)

  * dictionary: integrity, brand / spelling aliases, whole-word matching
  * candidate skill depth: years from dated roles (overlaps once), last used, current,
    source; AI involvement with verified quotes; new skills; same data on copies
  * job skill spec: validated, AI-new skills / aliases / nearby links become suggestions that
    work at once for that job only (decision 1-A) and agency-wide after admin approval;
    another agency never sees them (3-A); edits are kept when the job text changes
  * routes: agency + recruiter scope, freelancers blocked, admin-only approvals and backfill,
    the switch; backfill resumable
  * Phase 1 changes no score

Run:   pytest test_skill_engine.py -q
"""
import os
import sys
import json
import tempfile
import datetime

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CO, OTHER = 94701, 94702
ADMIN, RIYA, FL, OADMIN = 9470101, 9470102, 9470103, 9470201
M1, M2, MO = 9470301, 9470302, 9470303
C1, C1C, C2, CR, CF = 9470401, 9470402, 9470403, 9470404, 9470405


def ago(days):
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
            - datetime.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S')


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='skill_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db(); c = conn.cursor(); now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", list(kw.values()))
    ins('companies', id=CO, name='Skill Co', status='active', created_at=now)
    ins('companies', id=OTHER, name='Other Agency', status='active', created_at=now)
    for uid, un, co, adm, role in ((ADMIN, 'sk_admin', CO, 1, 'user'), (RIYA, 'sk_riya', CO, 0, 'user'),
                                   (FL, 'sk_fl', CO, 0, 'freelancer_sourcer'), (OADMIN, 'sk_other', OTHER, 1, 'user')):
        ins('users', id=uid, username=un, password_hash='x', display_name=un.title(), role=role, status='approved',
            company_id=co, is_company_admin=adm, created_at=now)
    ins('mandates', id=M1, role='Area Sales Manager - LV', client='Lauritz Knudsen', status='active', owner_id=CO,
        assigned_user_id=ADMIN, created_at=ago(30), jd='Sell LV switchgear (ACB, MCCB) through dealers in North India. 5+ years channel sales.')
    ins('mandates', id=M2, role='Service Engineer', client='Havells', status='active', owner_id=CO, assigned_user_id=RIYA,
        created_at=ago(30), jd='Service of UPS systems.')
    ins('mandates', id=MO, role='X', client='Y', status='active', owner_id=OTHER, assigned_user_id=OADMIN, created_at=ago(30),
        jd='Sell MCCB.')
    c.execute('INSERT OR IGNORE INTO mandate_assignees (company_id, mandate_id, user_id, is_primary, is_active) VALUES (?,?,?,1,1)',
              (CO, M2, RIYA))
    for cid, mid, own, name, comp, desig, ks, copied in (
            (C1, M1, CO, 'Asha Verma', 'Schneider Electric', 'Area Sales Manager', '["Switchgear", "Channel Sales", "SAP"]', 0),
            (C1C, M2, CO, 'Asha Verma', 'Schneider Electric', 'Area Sales Manager', '["Switchgear"]', C1),
            (C2, M1, CO, 'Ravi Analyst', 'Sapient', 'Business Analyst', '["Excel"]', 0),
            (CR, M2, CO, 'Riya Cand', 'Polycab', 'Service Engineer', '["UPS"]', 0),
            (CF, MO, OTHER, 'Foreign Person', 'ABB', 'Sales Manager', '["MCCB"]', 0)):
        ins('candidates', id=cid, mandate_id=mid, name=name, company=comp, designation=desig, key_skills=ks, stage='Screening',
            owner_id=own, copied_from=copied, career_summary='', created_at=ago(200), updated_at=ago(5))
    for cid, comp, desig, s, e, cur, desc, so in (
            (C1, 'Schneider Electric', 'Area Sales Manager', 'Jan 2019', '', 1,
             'Headed channel sales of MCCB and Masterpact ACB through dealers in North India.', 0),
            (C1, 'Havells', 'Sales Engineer', 'Jun 2015', 'Dec 2018', 0,
             'Sold MCB and distribution boards to panel builders. Supported LV panel projects.', 1),
            (C2, 'Sapient', 'Business Analyst', '2018', '', 1, 'Worked at Sapient on requirements.', 0)):
        c.execute('INSERT INTO work_history (candidate_id, company, designation, start_date, end_date, is_current, description, sort_order) '
                  'VALUES (?,?,?,?,?,?,?,?)', (cid, comp, desig, s, e, cur, desc, so))
    conn.commit(); conn.close()
    yield server


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


def se():
    from modules import skill_engine
    return skill_engine


@pytest.fixture
def no_ai(srv, monkeypatch):
    real = srv.get_setting
    monkeypatch.setattr(srv, 'get_setting', lambda k, d='': '' if k == 'deepseek_api_key' else real(k, d))


# ── dictionary ─────────────────────────────────────────────────────────────
def test_dictionary_integrity_and_aliases(srv):
    import skill_taxonomy as t
    assert len(t.BASE) >= 100
    m = se()
    conn = srv.get_db()
    lex = m.lexicon(conn, CO)
    conn.close()
    f = lambda s: set(m.find_skills(s, lex))
    assert f('Sold Masterpact and Emax ACBs') == {'acb'}
    assert f('Sold Masterpact and Emax breakers') == {'acb', 'circuit_breakers'}
    assert 'mccb' in f('Compact NSX, Tmax and 3VA MCCBs')
    assert 'lv_switchgear' in f('LT switchgear') and 'lv_switchgear' in f('low-voltage switchgear')
    assert f('Analyst at Sapient') == set()                      # "sap" only as a whole word
    assert 'sap' in f('SAP SD user')
    assert f('worked in the service industry with good quality') == set()   # generic words dropped
    assert 'mv_switchgear' in f('11 kV switchgear, RMU and VCB')
    assert 'data_center' in f('Tier III data centre build')


# ── candidate skill depth ──────────────────────────────────────────────────
def test_depth_years_last_used_and_source(srv, no_ai):
    m = se()
    conn = srv.get_db()
    d = m.compute_depth(conn, CO, C1, use_ai=False)
    conn.commit()
    by = {s['key']: s for s in d['skills']}
    assert by['acb']['current'] and by['acb']['years'] >= 7.5 and by['acb']['last_used'] == 'current'
    assert by['mcb']['years'] == 3.6 and by['mcb']['last_used'] == '2018-12' and not by['mcb']['current']
    assert by['channel_sales']['current'] and by['lv_panels']['years'] == 3.6
    assert by['sap']['years'] is None and by['sap']['source'] == 'profile'          # in key skills only
    assert 'switchgear' in by and by['acb']['level'] is None and d['ai'] is False
    # the same person's copy on another job carries the same data
    cp = json.loads(conn.execute('SELECT data FROM candidate_skill_depth WHERE candidate_id=?', (C1C,)).fetchone()[0])
    assert {s['key'] for s in cp['skills']} == set(by)
    d2 = m.compute_depth(conn, CO, C2, use_ai=False)
    assert 'sap' not in {s['key'] for s in d2['skills']}
    conn.close()


def test_depth_with_ai_involvement_and_quotes(srv):
    m = se()

    def fake(user):
        assert 'Masterpact' in user and '0. Area Sales Manager @ Schneider Electric' in user
        return {'skills': [
            {'key': 'acb', 'level': 'led', 'roles': [0], 'quote': 'Headed channel sales of MCCB and Masterpact ACB'},
            {'key': 'mcb', 'level': 'did', 'roles': [1], 'quote': 'designed a nuclear reactor control room'},
            {'key': '', 'name': 'Busway retrofit', 'level': 'did', 'roles': [1], 'quote': ''},
            {'key': 'nope', 'name': 'Masterpact', 'level': 'led', 'roles': [0]},
            {'key': 'ups', 'level': 'bogus', 'roles': [99]}]}
    conn = srv.get_db()
    d = m.compute_depth(conn, CO, C1, use_ai=True, classify=fake)
    conn.commit(); conn.close()
    by = {s['key']: s for s in d['skills']}
    assert d['ai'] is True
    assert by['acb']['level'] == 'led' and by['acb']['quote_ok'] and 'Masterpact' in by['acb']['quote']
    assert by['mcb']['level'] == 'did' and by['mcb']['quote'] == ''                 # invented quote dropped
    assert by['c_busway_retrofit']['new'] and by['c_busway_retrofit']['years'] == 3.6
    assert by['ups']['level'] is None and by['ups']['years'] is None              # bad level / role ignored


# ── job skill spec ─────────────────────────────────────────────────────────
def _fake_spec(user):
    assert 'acb=ACB' in user and 'Sell LV switchgear' in user
    return {'skills': [
        {'key': 'channel_sales', 'importance': 'core', 'min_years': 5, 'involvement': 'led', 'recent_years': 3,
         'why': 'Dealer channel is the job', 'aliases': ['stockist network', 'ACB'],
         'near': [{'key': 'specification_selling', 'credit': 0.4}]},
        {'key': '', 'name': 'Masterpact', 'importance': 'core'},
        {'key': '', 'name': 'Busway retrofit selling', 'importance': 'nice'},
        {'key': 'mccb', 'importance': 'important', 'near': [{'name': 'Tmax breakers', 'credit': 0.7}]},
        {'key': 'acb', 'importance': 'nice'},
        {'key': 'mcb', 'importance': 'weird', 'involvement': 'strange', 'min_years': 99}]}


def test_spec_generation_and_suggestions_per_job(srv):
    m = se()
    conn = srv.get_db()
    mand = conn.execute('SELECT * FROM mandates WHERE id=?', (M1,)).fetchone()
    spec = m.generate_spec(conn, CO, mand, ask=_fake_spec)
    keys = [s['key'] for s in spec['skills']]
    assert keys[:2] == ['channel_sales', 'acb']                            # core first; "Masterpact" = ACB, no duplicate
    cs = spec['skills'][0]
    assert cs['min_years'] == 5 and cs['involvement'] == 'led' and cs['recent_years'] == 3
    assert any(n['key'] == 'specification_selling' and n['credit'] == 0.4 for n in cs['near'])
    mcb = next(s for s in spec['skills'] if s['key'] == 'mcb')
    assert mcb['importance'] == 'important' and mcb['involvement'] == 'any' and mcb['min_years'] is None
    new = next(s for s in spec['skills'] if s['key'] == 'c_busway_retrofit_selling')
    assert new['pending']
    pend = conn.execute("SELECT kind, skill_key, value FROM skill_custom WHERE owner_id=? AND status='pending' AND mandate_id=?",
                        (CO, M1)).fetchall()
    kinds = {(r['kind'], r['skill_key'], r['value']) for r in pend}
    assert ('alias', 'channel_sales', 'stockist network') in kinds
    assert ('near', 'channel_sales', 'specification_selling') in kinds
    assert not any(r['value'] == 'acb' for r in pend if r['kind'] == 'alias')       # already a known name
    assert not any(r['kind'] == 'near' and r['skill_key'] == 'mccb' for r in pend)  # "Tmax" is MCCB itself
    # decision 1-A: the suggestion works for THIS job at once, not yet for other jobs
    f = lambda mid: set(m.find_skills('Built a stockist network in UP', m.lexicon(conn, CO, mid)))
    assert 'channel_sales' in f(M1) and 'channel_sales' not in f(M2) and 'channel_sales' not in f(0)
    conn.close()


def test_admin_approval_makes_it_agency_wide_and_other_agency_never_sees_it(srv):
    m = se()
    a = cl(srv, ADMIN)
    assert cl(srv, RIYA).get('/api/skills/suggestions').status_code == 403
    sug = a.get('/api/skills/suggestions').get_json()['suggestions']
    alias = next(s for s in sug if s['kind'] == 'alias' and s['value'] == 'stockist network')
    assert alias['job'].startswith('Area Sales Manager')
    assert cl(srv, OADMIN).post('/api/skills/suggestions/%d' % alias['id'], json={'decision': 'approve'}).status_code == 404
    assert a.post('/api/skills/suggestions/%d' % alias['id'], json={'decision': 'approve'}).get_json()['ok']
    conn = srv.get_db()
    assert 'channel_sales' in m.find_skills('Stockist network', m.lexicon(conn, CO, 0))
    assert 'channel_sales' not in m.find_skills('Stockist network', m.lexicon(conn, OTHER, 0))   # 3-A
    conn.close()
    assert cl(srv, OADMIN).get('/api/skills/suggestions').get_json()['suggestions'] == []
    rej = next(s for s in sug if s['kind'] == 'skill')
    assert a.post('/api/skills/suggestions/%d' % rej['id'], json={'decision': 'reject'}).get_json()['ok']
    assert a.post('/api/skills/suggestions/%d' % rej['id'], json={'decision': 'approve'}).status_code == 404


def test_spec_routes_edit_and_stale(srv, no_ai):
    a = cl(srv, ADMIN)
    r = a.get('/api/mandates/%d/skill-spec' % M1).get_json()
    assert r['ok'] and r['status'] == 'ai' and r['spec']['skills'][0]['key'] == 'channel_sales' and not r['stale']
    skills = r['spec']['skills']
    skills[0]['min_years'] = 7
    skills.append({'key': 'masterpact-not-a-key', 'importance': 'core'})          # unknown key, no AI -> dropped
    r = a.put('/api/mandates/%d/skill-spec' % M1, json={'skills': skills}).get_json()
    assert r['ok'] and r['status'] == 'edited' and r['spec']['skills'][0]['min_years'] == 7
    assert 'masterpact-not-a-key' not in [s['key'] for s in r['spec']['skills']]
    conn = srv.get_db()
    conn.execute('UPDATE mandates SET jd=? WHERE id=?', ('Sell LV switchgear and busbar trunking via dealers.', M1))
    conn.commit(); conn.close()
    r = a.get('/api/mandates/%d/skill-spec?ensure=1' % M1).get_json()
    assert r['status'] == 'edited' and r['stale'] and r['spec']['skills'][0]['min_years'] == 7   # edit kept, flagged
    assert a.put('/api/mandates/%d/skill-spec' % M1, json={'skills': []}).status_code == 400


def test_route_scope_and_isolation(srv, no_ai):
    a, r, f, o = cl(srv, ADMIN), cl(srv, RIYA), cl(srv, FL), cl(srv, OADMIN)
    assert a.get('/api/candidates/%d/skill-depth' % C1).get_json()['data'] is not None
    assert r.get('/api/candidates/%d/skill-depth' % C1).status_code in (403, 404)      # not her job
    assert r.get('/api/mandates/%d/skill-spec' % M1).status_code in (403, 404)
    assert r.get('/api/mandates/%d/skill-spec' % M2).status_code == 200
    assert f.get('/api/candidates/%d/skill-depth' % C1).status_code == 403
    assert f.get('/api/skills/dictionary?q=acb').status_code == 403
    for path in ('/api/candidates/%d/skill-depth' % C1, '/api/mandates/%d/skill-spec' % M1):
        assert o.get(path).status_code == 404
    assert o.post('/api/candidates/%d/skill-depth' % C1).status_code == 404
    assert o.put('/api/mandates/%d/skill-spec' % M1, json={'skills': [{'key': 'acb'}]}).status_code == 404
    # recruiter analyses her own candidate (no AI key: years and sources only)
    x = r.post('/api/candidates/%d/skill-depth' % CR).get_json()
    assert x['ok'] and x['data']['ai'] is False and 'ups' in {s['key'] for s in x['data']['skills']}
    d = a.get('/api/skills/dictionary?q=masterpact').get_json()
    assert d['skills'][0]['key'] == 'acb'


def test_switch_and_admin_only_settings(srv):
    a = cl(srv, ADMIN)
    assert cl(srv, RIYA).post('/api/skills/config', json={'on': False}).status_code == 403
    assert a.post('/api/skills/config', json={'on': False}).get_json()['on'] is False
    assert a.get('/api/candidates/%d/skill-depth' % C1).status_code == 404
    assert a.get('/api/mandates/%d/skill-spec' % M1).status_code == 404
    assert cl(srv, OADMIN).get('/api/mandates/%d/skill-spec' % MO).status_code == 200      # other agency unaffected
    assert a.post('/api/skills/config', json={'on': True}).get_json()['on'] is True
    assert cl(srv, RIYA).post('/api/skills/backfill/start').status_code == 403


def test_backfill_resumable(srv, monkeypatch):
    m = se()
    monkeypatch.setattr(m, 'NEW_WINDOW_HOURS', -1)
    a = cl(srv, ADMIN)
    s = a.post('/api/skills/backfill/start').get_json()['backfill']
    assert s['status'] == 'running' and s['remaining'] >= 1
    calls = []
    fake = lambda u: calls.append(u) or {'skills': []}
    m.tick(fake)
    assert a.post('/api/skills/backfill/stop').get_json()['backfill']['status'] == 'stopped'
    n = len(calls)
    assert m.tick(fake) is False and len(calls) == n                    # stopped: nothing runs
    a.post('/api/skills/backfill/start')
    for _ in range(20):
        m.tick(fake)
    s = a.get('/api/skills/backfill/status').get_json()['backfill']
    assert s['remaining'] == 0 and s['status'] in ('running', 'done')
    m.tick(fake)
    assert a.get('/api/skills/backfill/status').get_json()['backfill']['status'] == 'done'
    conn = srv.get_db()
    assert conn.execute('SELECT COUNT(*) FROM candidate_skill_depth WHERE owner_id=?', (OTHER,)).fetchone()[0] == 0
    conn.close()


def test_no_score_changes_and_ui_wired(srv):
    import inspect
    src = inspect.getsource(srv._extension_score_match)
    assert 'skill_engine' not in src and 'candidate_skill_depth' not in src        # Phase 1: scores untouched
    html = open(os.path.join(ROOT, 'index.html'), encoding='utf-8').read()
    assert '/skill-engine.js' in html and 'SkillEngine.mountDepth' in html and 'SkillEngine.mountSpec' in html
    assert cl(srv, ADMIN).get('/skill-engine.js').status_code == 200
    import academy_content
    assert 'skill-engine' in {x['key'] for x in academy_content.ARTICLES}


# ── memory safety (live 502 / instance restarts, Nov 2026) ──────────────────
def test_profile_view_never_parses_cv_and_reader_is_bounded(srv, no_ai, monkeypatch):
    canvas = pytest.importorskip('reportlab.pdfgen.canvas')
    m = se()
    os.makedirs(srv.CV_DIR, exist_ok=True)
    fp = os.path.join(srv.CV_DIR, 'skill_mem_test.pdf')
    pdf = canvas.Canvas(fp)
    for i in range(10):
        pdf.drawString(72, 720, 'PAGE%02d Headed channel sales of MCCB through dealers.' % i)
        pdf.showPage()
    pdf.save()
    conn = srv.get_db()
    conn.execute("UPDATE candidates SET cv_path='skill_mem_test.pdf', cv_original_name='cv.pdf' WHERE id=?", (C2,))
    conn.commit()
    c = conn.execute('SELECT * FROM candidates WHERE id=?', (C2,)).fetchone()
    txt = m.cv_text_light(c, 7000)
    assert 'PAGE00' in txt and 'PAGE03' in txt and 'PAGE04' not in txt      # first pages only
    assert len(m.cv_text_light(c, 60)) <= 60
    m.compute_depth(conn, CO, C2, use_ai=False)
    conn.commit(); conn.close()
    # Opening the profile reads the stored result only: no CV parsing at all.
    import pdfplumber
    calls = []
    monkeypatch.setattr(pdfplumber, 'open', lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError('parsed')))
    monkeypatch.setattr(srv, 'extract_text_from_file', lambda *a, **k: calls.append(1) or (None, 'x'))
    r = cl(srv, ADMIN).get('/api/candidates/%d/skill-depth' % C2).get_json()
    assert r['data'] is not None and r['stale'] is False and calls == []
    # A new CV file (different size) marks the result as out of date, still without parsing.
    with open(fp, 'ab') as fh:
        fh.write(b'\n% extra')
    assert cl(srv, ADMIN).get('/api/candidates/%d/skill-depth' % C2).get_json()['stale'] is True and calls == []
