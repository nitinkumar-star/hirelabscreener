"""
Insight Engine — Wave 1: Capture  (modules/insight_capture.py)

  * a reason is saved with a stage move; an invalid one is refused before any write
  * callers that send no reason keep working exactly as before
  * client feedback is required on a client rejection and APPENDED, never overwritten
  * market notes: company key normalised, signal type checked
  * Call capture card saves CTC / notice / team / target (+ linked copies) and a note
  * call-analysis proposals are only proposals until the recruiter approves them
  * auto-tagging fills only empty values and never overwrites what a person set
  * the admin backfill is resumable, newest first, stoppable, and per agency
  * the 6:30 check counts missing reasons / feedback / expected CTC
  * tenant isolation and recruiter scope on every new route; freelancers cannot write

Run:   pytest test_insight_capture.py -q
"""
import os
import sys
import json
import tempfile
import datetime

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CO, OTHER = 94101, 94102
ADMIN, OADMIN, RIYA, FL = 9410101, 9410201, 9410102, 9410103
M1, M2, M_OTHER = 9410301, 9410302, 9410303
# candidate ids (kept well below the auto-numbered range other test files rely on)
C_MOVE, C_REJ, C_INT, C_DONE, C_RIYA, C_SECRET, C_FOREIGN, C_CAP, C_CAP_COPY = range(9941001, 9941010)


def ago(days):
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
            - datetime.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S')


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='insight1_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db(); c = conn.cursor(); now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", list(kw.values()))
    ins('companies', id=CO, name='Insight Co', status='active', created_at=now)
    ins('companies', id=OTHER, name='Other Agency', status='active', created_at=now)
    for uid, un, co, adm, role in ((ADMIN, 'ins_admin', CO, 1, 'user'), (OADMIN, 'ins_other', OTHER, 1, 'user'),
                                   (RIYA, 'ins_riya', CO, 0, 'user'), (FL, 'ins_fl', CO, 0, 'freelancer_sourcer')):
        ins('users', id=uid, username=un, password_hash='x', display_name=un.title(), role=role, status='approved',
            company_id=co, is_company_admin=adm, created_at=now)
    ins('mandates', id=M1, role='LV Sales Manager', client='Lauritz Knudsen', status='active', owner_id=CO,
        assigned_user_id=ADMIN, created_at=ago(100))
    ins('mandates', id=M2, role='MV Application Engineer', client='Schneider Electric', status='active', owner_id=CO,
        assigned_user_id=RIYA, created_at=ago(100))
    ins('mandates', id=M_OTHER, role='Other Role', client='Other Client', status='active', owner_id=OTHER,
        assigned_user_id=OADMIN, created_at=ago(100))
    old = ago(200)
    for cid, mid, name, comp, stage, own in (
            (C_MOVE, M1, 'Asha Verma', 'Siemens Ltd', 'Interested', CO),
            (C_REJ, M1, 'Ravi Kumar', 'ABB India', 'Shared with Client', CO),
            (C_INT, M1, 'Neha Gupta', 'Havells', 'Called', CO),
            (C_DONE, M1, 'Vikram Rao', 'L&T', 'Called', CO),
            (C_RIYA, M2, 'Riya Cand', 'Polycab', 'Screening', CO),
            (C_SECRET, M1, 'ZZSECRET Cand', 'Secret Co', 'Screening', CO),
            (C_FOREIGN, M_OTHER, 'Foreign Cand', 'Foreign Co', 'Screening', OTHER),
            (C_CAP, M1, 'Capture Cand', 'KEI Wires & Cables Ltd', 'Interested', CO)):
        ins('candidates', id=cid, mandate_id=mid, name=name, company=comp, stage=stage, owner_id=own,
            designation='Sales Manager', ctc_current=12, ctc_expected=0, created_at=old, updated_at=old)
    # the same person on a second job (linked copy)
    ins('candidates', id=C_CAP_COPY, mandate_id=M2, name='Capture Cand', company='KEI Wires & Cables Ltd',
        stage='Screening', owner_id=CO, copied_from=C_CAP, designation='Sales Manager', created_at=old, updated_at=old)
    c.execute('UPDATE candidates SET client_feedback=? WHERE id=?', ('Earlier note from the client', C_REJ))
    conn.commit(); conn.close()
    yield server


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


def q(server, sql, *a):
    conn = server.get_db()
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def one(server, sql, *a):
    r = q(server, sql, *a)
    return r[0] if r else None


def ic():
    from modules import insight_capture
    return insight_capture


# ── 1a  reasons ────────────────────────────────────────────────────────────
def test_catalog_and_switch_default_on(srv):
    a = cl(srv, ADMIN)
    r = a.get('/api/insight/config').get_json()
    assert r['ok'] and r['capture_on'] is True and r['is_admin'] is True
    fams = r['catalog']['families']
    assert 'ctc_gap' in [c for c, _ in fams['candidate_no']['codes']]
    assert 'not interested' in fams['candidate_no']['stages'] and 'interview backout' in fams['candidate_no']['stages']
    assert fams['client_reject']['needs_feedback'] is True
    assert len(r['catalog']['role_families']) == 12
    assert 'General Management / Leadership' in r['catalog']['role_families']


def test_reason_saved_with_stage_move(srv):
    a = cl(srv, ADMIN)
    r = a.post(f'/api/candidates/{C_MOVE}/stage', json={'stage': 'Not Interested', 'note': 'Changed from List view',
                                                         'reason_code': 'ctc_gap', 'reason_detail': 'wants 22, offer 18'})
    assert r.status_code == 200 and r.get_json()['ok']
    assert one(srv, 'SELECT stage FROM candidates WHERE id=?', C_MOVE)['stage'] == 'Not Interested'
    rr = one(srv, 'SELECT * FROM candidate_reasons WHERE candidate_id=? ORDER BY id DESC', C_MOVE)
    assert rr['reason_code'] == 'ctc_gap' and rr['owner_id'] == CO and rr['user_id'] == ADMIN
    assert rr['source'] == 'manual' and rr['stage'] == 'Not Interested' and rr['mandate_id'] == M1
    h = one(srv, 'SELECT note FROM stage_history WHERE candidate_id=? ORDER BY id DESC', C_MOVE)
    assert 'Changed from List view' in h['note'] and 'Reason: CTC gap' in h['note'] and 'wants 22' in h['note']
    hist = a.get(f'/api/candidates/{C_MOVE}/reasons').get_json()['reasons']
    assert hist[0]['label'] == 'CTC gap'


def test_invalid_reason_refused_before_any_write(srv):
    a = cl(srv, ADMIN)
    n_hist = len(q(srv, 'SELECT id FROM stage_history WHERE candidate_id=?', C_INT))
    for body in ({'stage': 'Not Interested', 'reason_code': 'made_up'},
                 {'stage': 'Not Suitable', 'reason_code': 'ctc_gap'},          # a candidate_no code on a screen-out
                 {'stage': 'Interested', 'reason_code': 'ctc_gap'}):           # positive stage takes no reason
        r = a.post(f'/api/candidates/{C_INT}/stage', json=body)
        assert r.status_code == 400 and r.get_json()['error']
    assert one(srv, 'SELECT stage FROM candidates WHERE id=?', C_INT)['stage'] == 'Called'
    assert len(q(srv, 'SELECT id FROM stage_history WHERE candidate_id=?', C_INT)) == n_hist
    assert not q(srv, 'SELECT id FROM candidate_reasons WHERE candidate_id=?', C_INT)


def test_existing_callers_without_reason_still_work(srv):
    a = cl(srv, ADMIN)
    r = a.post(f'/api/candidates/{C_DONE}/stage', json={'stage': 'Not Interested', 'note': 'Moved via drag-drop'})
    assert r.status_code == 200 and r.get_json()['ok']
    assert one(srv, 'SELECT stage FROM candidates WHERE id=?', C_DONE)['stage'] == 'Not Interested'
    assert one(srv, 'SELECT note FROM stage_history WHERE candidate_id=? ORDER BY id DESC', C_DONE)['note'] == 'Moved via drag-drop'
    r = a.post(f'/api/candidates/{C_DONE}/stage', json={'stage': None, 'note': 'Comment: hello', 'keep_stage': True})
    assert r.get_json()['ok']
    assert one(srv, 'SELECT stage FROM candidates WHERE id=?', C_DONE)['stage'] == 'Not Interested'
    assert not q(srv, 'SELECT id FROM candidate_reasons WHERE candidate_id=?', C_DONE)


def test_client_feedback_required_and_appended(srv):
    a = cl(srv, ADMIN)
    r = a.post(f'/api/candidates/{C_REJ}/stage', json={'stage': 'Client Rejected on Paper', 'reason_code': 'ctc_high'})
    assert r.status_code == 400 and 'feedback' in r.get_json()['error'].lower()
    assert one(srv, 'SELECT stage FROM candidates WHERE id=?', C_REJ)['stage'] == 'Shared with Client'
    r = a.post(f'/api/candidates/{C_REJ}/stage', json={'stage': 'Client Rejected on Paper', 'reason_code': 'ctc_high',
                                                        'client_feedback': 'Budget is 20 LPA max'})
    assert r.status_code == 200
    fb = one(srv, 'SELECT client_feedback FROM candidates WHERE id=?', C_REJ)['client_feedback']
    assert fb.startswith('Earlier note from the client') and 'Budget is 20 LPA max' in fb and '\n---\n' in fb
    rr = one(srv, 'SELECT * FROM candidate_reasons WHERE candidate_id=?', C_REJ)
    assert rr['client_feedback'] == 'Budget is 20 LPA max'


def test_reason_only_endpoint_uses_current_stage(srv):
    a = cl(srv, ADMIN)
    r = a.post(f'/api/candidates/{C_DONE}/reasons', json={'reason_code': 'happy_in_current'})
    assert r.status_code == 200 and r.get_json()['reason']['stage'] == 'Not Interested'
    assert one(srv, 'SELECT stage FROM candidates WHERE id=?', C_DONE)['stage'] == 'Not Interested'
    # a positive stage takes no reason
    r = a.post(f'/api/candidates/{C_CAP}/reasons', json={'reason_code': 'ctc_gap'})
    assert r.status_code == 400


# ── 1b  market notes + call capture card ───────────────────────────────────
def test_market_note_company_key_normalised(srv):
    a = cl(srv, ADMIN)
    r = a.post(f'/api/candidates/{C_CAP}/market-notes', json={'signal_type': 'freeze', 'note': 'No hiring till March'})
    n = r.get_json()['note']
    assert n['company_name'] == 'KEI Wires & Cables Ltd'                 # defaults to the candidate's company
    from modules.talent_map import company_key
    assert n['company_key'] == company_key('kei wires and cables') == 'kei wires and cables'
    r = a.post(f'/api/candidates/{C_CAP}/market-notes', json={'company': 'Siemens Pvt. Ltd.', 'signal_type': 'hiring',
                                                              'note': '3 openings in Bangalore'})
    assert r.get_json()['note']['company_key'] == 'siemens'
    assert a.post(f'/api/candidates/{C_CAP}/market-notes', json={'signal_type': 'rumour', 'note': 'x'}).status_code == 400
    assert a.post(f'/api/candidates/{C_CAP}/market-notes', json={'signal_type': 'hiring', 'note': ' '}).status_code == 400


def test_call_capture_card_saves_and_syncs_copy(srv):
    a = cl(srv, ADMIN)
    r = a.get(f'/api/candidates/{C_CAP}/capture').get_json()['capture']
    assert r['ctc_expected'] == 0 and r['company'] == 'KEI Wires & Cables Ltd'
    r = a.put(f'/api/candidates/{C_CAP}/capture', json={'ctc_expected': '18.5', 'notice_period': '60', 'team_direct': '4',
                                                        'team_indirect': '12', 'yearly_target': '35',
                                                        'market_note': {'company': 'Polycab', 'signal_type': 'pay',
                                                                        'note': 'Increments of 12% this year'}})
    j = r.get_json()
    assert j['ok'] and j['capture']['ctc_expected'] == 18.5 and j['capture']['notice_period'] == 60
    assert j['capture']['team_direct'] == 4 and j['capture']['team_indirect'] == 12 and j['capture']['yearly_target'] == 35
    assert j['capture']['notes'][0]['company_key'] == 'polycab'
    cp = one(srv, 'SELECT ctc_expected, team_direct, yearly_target FROM candidates WHERE id=?', C_CAP_COPY)
    assert cp['ctc_expected'] == 18.5 and cp['team_direct'] == 4 and cp['yearly_target'] == 35
    assert a.put(f'/api/candidates/{C_CAP}/capture', json={'ctc_expected': 'abc'}).status_code == 400


# ── 1c  call analysis proposals ────────────────────────────────────────────
def test_call_proposals_do_not_auto_apply(srv):
    m = ic()
    with srv.app.test_request_context():
        from flask import session
        session['user_id'] = ADMIN
        assert 'ctc_gap' in m.call_prompt_fields(CO) and 'new_order_project' in m.call_prompt_fields(CO)
    conn = srv.get_db()
    cand = conn.execute('SELECT * FROM candidates WHERE id=?', (C_INT,)).fetchone()
    analysis = {'reason_for_no': 'counter_offer', 'reason_for_no_detail': 'Got a counter of 20%',
                'team_direct_discussed': 6, 'team_indirect_discussed': None, 'yearly_target_cr_discussed': '45',
                'market_notes': [{'company': 'Havells', 'signal_type': 'exit', 'note': 'Two RSMs left in Q3'},
                                 {'company': 'X', 'signal_type': 'gossip', 'note': 'dropped: bad type'}]}
    prop = m.call_proposals(conn, CO, cand, analysis)
    assert set(prop) == {'reason_for_no', 'team_direct', 'yearly_target', 'market_notes'}
    assert prop['reason_for_no']['proposed'] == 'counter_offer' and len(prop['market_notes']['proposed']) == 1
    assert m.call_proposals(conn, CO, cand, {'reason_for_no': 'invented'}) == {}
    # nothing was written by proposing
    after = conn.execute('SELECT team_direct, yearly_target FROM candidates WHERE id=?', (C_INT,)).fetchone()
    assert after['team_direct'] is None and after['yearly_target'] is None
    assert not conn.execute('SELECT 1 FROM candidate_reasons WHERE candidate_id=?', (C_INT,)).fetchone()
    assert not conn.execute("SELECT 1 FROM market_notes WHERE candidate_id=? AND source='call_ai'", (C_INT,)).fetchone()
    # stored as a saved call analysis the recruiter can approve
    cur = conn.execute('INSERT INTO candidate_call_analysis (owner_id, candidate_id, mandate_id, analysis, proposed_updates, '
                       'applied_fields, created_at) VALUES (?,?,?,?,?,?,?)',
                       (CO, C_INT, M1, json.dumps(analysis), json.dumps(prop), '[]', srv.ts()))
    aid = cur.lastrowid
    conn.commit(); conn.close()
    # another agency cannot approve it
    assert cl(srv, OADMIN).post(f'/api/candidates/{C_INT}/call-analysis/{aid}/apply',
                                json={'fields': ['team_direct']}).status_code in (403, 404)
    a = cl(srv, ADMIN)
    r = a.post(f'/api/candidates/{C_INT}/call-analysis/{aid}/apply',
               json={'fields': ['reason_for_no', 'market_notes', 'team_direct']}).get_json()
    assert r['ok'] and set(r['applied']) == {'reason_for_no', 'market_notes', 'team_direct'}
    assert one(srv, 'SELECT team_direct FROM candidates WHERE id=?', C_INT)['team_direct'] == 6
    assert one(srv, 'SELECT yearly_target FROM candidates WHERE id=?', C_INT)['yearly_target'] is None   # not ticked
    rr = one(srv, 'SELECT * FROM candidate_reasons WHERE candidate_id=?', C_INT)
    assert rr['reason_code'] == 'counter_offer' and rr['source'] == 'call_ai'
    mn = one(srv, "SELECT * FROM market_notes WHERE candidate_id=? AND source='call_ai'", C_INT)
    assert mn['company_key'] == 'havells' and mn['signal_type'] == 'exit'
    # applying twice does nothing more
    r = a.post(f'/api/candidates/{C_INT}/call-analysis/{aid}/apply', json={'fields': ['reason_for_no']}).get_json()
    assert r['applied'] == []
    assert len(q(srv, 'SELECT id FROM candidate_reasons WHERE candidate_id=?', C_INT)) == 1


# ── 1d  auto-tagging ───────────────────────────────────────────────────────
def test_tagging_never_overwrites_human_values(srv):
    m = ic()
    conn = srv.get_db()
    now = srv.ts()
    conn.execute("INSERT OR REPLACE INTO candidates (id, mandate_id, name, company, designation, stage, owner_id, "
                 "function_tags, domain_tags, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (9941050, M1, 'Tag Human', 'Havells', 'Area Sales Manager', 'Screening', CO,
                  json.dumps(['Channel Sales']), json.dumps(['Switchgear']), ago(300), ago(300)))
    conn.execute("INSERT OR REPLACE INTO candidates (id, mandate_id, name, company, designation, stage, owner_id, "
                 "copied_from, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (9941051, M2, 'Tag Human', 'Havells', 'Area Sales Manager', 'Screening', CO, 9941050, ago(300), ago(300)))
    conn.commit()
    calls = []

    def fake(text):
        calls.append(text)
        return {'role_family': 'Product Management', 'domains': ['MV', 'Nonsense'], 'level_band': 'Manager'}
    status, used = m.tag_person(conn, CO, 9941050, fake)
    conn.commit()
    assert status == 'done' and used and 'Area Sales Manager' in calls[0]
    a = conn.execute('SELECT * FROM candidates WHERE id=9941050').fetchone()
    assert json.loads(a['function_tags']) == ['Channel Sales']          # human value kept
    assert json.loads(a['domain_tags']) == ['Switchgear']               # human value kept
    assert a['role_family'] == 'Channel Sales'                          # derived from the human tag, not the AI
    assert a['level_band'] == 'Manager'
    b = conn.execute('SELECT * FROM candidates WHERE id=9941051').fetchone()   # the linked copy had nothing
    assert json.loads(b['function_tags']) == ['Product Management'] and json.loads(b['domain_tags']) == ['MV']
    assert b['role_family'] == 'Channel Sales' and b['level_band'] == 'Manager'
    assert {r['candidate_id'] for r in conn.execute('SELECT candidate_id FROM insight_tag_log WHERE candidate_id IN (9941050,9941051)')} == {9941050, 9941051}
    # a second run changes nothing and calls no AI
    status, used = m.tag_person(conn, CO, 9941050, fake)
    assert not used and len(calls) == 1
    conn.close()
    # support family -> the sub-function tag
    out = m._norm_ai({'role_family': m.SUPPORT, 'support_sub': 'IT', 'domains': 'Data Center', 'level_band': 'gm+'})
    assert out == {'role_family': m.SUPPORT, 'function_tags': ['IT / Digital'], 'domain_tags': ['Data Center'], 'level_band': 'GM+'}
    assert m.family_from_tags(['Sales', 'Marketing']) == 'Business Development'   # specific beats generic


def test_backfill_resumable_newest_first_and_stoppable(srv, monkeypatch):
    m = ic()
    monkeypatch.setattr(m, 'NEW_WINDOW_HOURS', 0)          # no "new candidate" work in this test
    conn = srv.get_db()
    # the other agency has untagged people too: they must not be touched
    ids = [9941101, 9941102, 9941103, 9941104]
    for i, cid in enumerate(ids):
        conn.execute("INSERT OR REPLACE INTO candidates (id, mandate_id, name, designation, career_summary, stage, owner_id, "
                     "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (cid, M1, 'BF %d' % i, 'Service Engineer', 'Field service of MV switchgear', 'Screening', CO,
                      ago(400), ago(400 - i)))                     # 9941104 is the most recently updated
    conn.execute("INSERT OR REPLACE INTO candidates (id, mandate_id, name, designation, stage, owner_id, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?,?)", (9941199, M_OTHER, 'Foreign BF', 'Engineer', 'Screening', OTHER, ago(400), ago(1)))
    # everything else of this agency counts as already processed for this test
    conn.execute("INSERT OR IGNORE INTO insight_tag_log (candidate_id, owner_id, status, tried_at) "
                 "SELECT id, owner_id, 'done', '' FROM candidates WHERE owner_id=? AND id NOT IN (%s)" % ','.join('?' * len(ids)),
                 [CO] + ids)
    conn.commit(); conn.close()
    seen = []

    def fake(text):
        seen.append(text.split('\n')[0])
        if 'BF 1' in text:
            pass
        return {'role_family': 'Service / Field', 'domains': ['MV'], 'level_band': 'Engineer / Executive'}
    orig_profile = m._profile_text
    monkeypatch.setattr(m, '_profile_text', lambda conn, c: 'Name: %s\n' % c['name'] + orig_profile(conn, c))

    a = cl(srv, ADMIN)
    assert cl(srv, RIYA).post('/api/insight/backfill/start').status_code == 403     # admins only
    s = a.post('/api/insight/backfill/start').get_json()['backfill']
    assert s['status'] == 'running' and s['total'] == 4 and s['remaining'] == 4
    assert m.tick(fake) is True and m.tick(fake) is True
    assert seen == ['Name: BF 3', 'Name: BF 2']                     # newest first
    s = a.get('/api/insight/backfill/status').get_json()['backfill']
    assert s['done'] == 2 and s['remaining'] == 2
    # stop: the worker does nothing
    assert a.post('/api/insight/backfill/stop').get_json()['backfill']['status'] == 'stopped'
    assert m.tick(fake) is False and len(seen) == 2
    # start again (e.g. after a restart): continues where it stopped, nothing done twice
    s = a.post('/api/insight/backfill/start').get_json()['backfill']
    assert s['status'] == 'running' and s['remaining'] == 2
    m.tick(fake); m.tick(fake)
    assert seen == ['Name: BF 3', 'Name: BF 2', 'Name: BF 1', 'Name: BF 0']
    m.tick(fake)                                                   # nothing left -> finished
    s = a.get('/api/insight/backfill/status').get_json()['backfill']
    assert s['status'] == 'done' and s['remaining'] == 0
    r = one(srv, 'SELECT role_family, level_band, function_tags FROM candidates WHERE id=?', 9941101)
    assert r['role_family'] == 'Service / Field' and json.loads(r['function_tags']) == ['Service & Support']
    # the other agency was not touched and has its own (idle) status
    assert one(srv, 'SELECT role_family FROM candidates WHERE id=9941199')['role_family'] in ('', None)
    assert cl(srv, OADMIN).get('/api/insight/backfill/status').get_json()['backfill']['status'] == 'idle'


def test_backfill_error_is_retried_on_next_start(srv, monkeypatch):
    m = ic()
    monkeypatch.setattr(m, 'NEW_WINDOW_HOURS', 0)
    conn = srv.get_db()
    conn.execute("INSERT OR REPLACE INTO candidates (id, mandate_id, name, designation, stage, owner_id, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?,?)", (9941120, M1, 'Err Cand', 'Engineer', 'Screening', CO, ago(400), ago(0)))
    conn.commit(); conn.close()
    a = cl(srv, ADMIN)
    a.post('/api/insight/backfill/start')

    def boom(text):
        raise RuntimeError('DeepSeek HTTP 500')
    m.tick(boom)
    assert one(srv, 'SELECT status FROM insight_tag_log WHERE candidate_id=9941120')['status'] == 'error'
    s = a.post('/api/insight/backfill/start').get_json()['backfill']   # failed ones get another try
    assert s['remaining'] >= 1
    m.tick(lambda t: {'role_family': 'Project Execution', 'level_band': 'Manager', 'domains': ['LV']})
    assert one(srv, 'SELECT status FROM insight_tag_log WHERE candidate_id=9941120')['status'] == 'done'


def test_new_candidates_are_tagged_by_the_sweeper(srv):
    m = ic()
    conn = srv.get_db()
    conn.execute("INSERT OR REPLACE INTO candidates (id, mandate_id, name, designation, stage, owner_id, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?,?)", (9941990, M1, 'Fresh Cand', 'Project Manager', 'Screening', CO, srv.ts(), srv.ts()))
    conn.commit(); conn.close()
    fake = lambda t: {'role_family': 'Project Execution', 'domains': ['Solar / Renewables'], 'level_band': 'Manager'}
    for _ in range(200):            # other test modules may have fresh candidates queued too
        if one(srv, 'SELECT 1 FROM insight_tag_log WHERE candidate_id=9941990'):
            break
        m.tick(fake)
    r = one(srv, 'SELECT role_family, domain_tags FROM candidates WHERE id=9941990')
    assert r['role_family'] == 'Project Execution' and json.loads(r['domain_tags']) == ['Solar / Renewables']


# ── 1e  the 6:30 check ─────────────────────────────────────────────────────
def test_check_630_counts_and_fix(srv):
    a = cl(srv, ADMIN)
    conn = srv.get_db()
    for cid, stage in ((9941201, 'Called'), (9941202, 'Shared with Client'), (9941203, 'Called'), (9941204, 'Called')):
        conn.execute("INSERT OR REPLACE INTO candidates (id, mandate_id, name, company, stage, owner_id, ctc_expected, "
                     "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (cid, M1, 'Panel %d' % cid, 'Siemens', stage, CO, 0, ago(50), ago(50)))
    conn.commit(); conn.close()
    a.post('/api/candidates/9941201/stage', json={'stage': 'Not Interested'})                    # no reason
    a.post('/api/candidates/9941202/stage', json={'stage': 'Client Rejected After Interview'})    # no reason, no feedback
    a.post('/api/candidates/9941203/stage', json={'stage': 'Interested'})                        # expected CTC 0
    a.post('/api/candidates/9941204/stage', json={'stage': 'Not Interested', 'reason_code': 'location'})   # complete
    j = a.get('/api/insight/check-630').get_json()
    mine = {r['id']: r['issues'] for r in j['rows'] if r['id'] in (9941201, 9941202, 9941203, 9941204)}
    assert mine[9941201] == ['missing_reason']
    assert mine[9941202] == ['missing_reason', 'missing_feedback']
    assert mine[9941203] == ['ctc_zero']
    assert 9941204 not in mine
    assert j['counts']['missing_reason'] >= 2 and j['counts']['missing_feedback'] >= 1 and j['counts']['ctc_zero'] >= 1
    total = j['total']
    # one-click fix: a reason for the current stage
    assert a.post('/api/candidates/9941201/reasons', json={'reason_code': 'other_offer'}).status_code == 200
    a.post('/api/candidates/9941202/reasons', json={'reason_code': 'technical_depth', 'client_feedback': 'Weak on protection relays'})
    a.put('/api/candidates/9941203/capture', json={'ctc_expected': 15})
    j = a.get('/api/insight/check-630').get_json()
    assert not {r['id'] for r in j['rows']} & {9941201, 9941202, 9941203}
    assert j['total'] == total - 3
    # other people's records are not "mine"
    other = cl(srv, OADMIN).get('/api/insight/check-630').get_json()
    assert not {r['id'] for r in other['rows']} & {9941201, 9941202, 9941203}


def test_switch_off_hides_and_only_admin_can_change(srv):
    m = ic()
    assert cl(srv, RIYA).post('/api/insight/config', json={'capture_on': False}).status_code == 403
    a = cl(srv, ADMIN)
    assert a.post('/api/insight/config', json={'capture_on': False}).get_json()['capture_on'] is False
    assert a.get('/api/insight/config').get_json()['capture_on'] is False
    assert a.get('/api/insight/check-630').get_json()['on'] is False
    assert m.call_prompt_fields(CO) == ''
    conn = srv.get_db()
    cand = conn.execute('SELECT * FROM candidates WHERE id=?', (C_INT,)).fetchone()
    assert m.call_proposals(conn, CO, cand, {'reason_for_no': 'ctc_gap'}) == {}
    conn.close()
    # the other agency is not affected by CO's switch
    assert cl(srv, OADMIN).get('/api/insight/config').get_json()['capture_on'] is True
    assert a.post('/api/insight/config', json={'capture_on': True}).get_json()['capture_on'] is True


# ── isolation ──────────────────────────────────────────────────────────────
def test_tenant_isolation_on_every_route(srv):
    o = cl(srv, OADMIN)
    for path in (f'/api/candidates/{C_MOVE}/reasons', f'/api/candidates/{C_CAP}/capture',
                 f'/api/candidates/{C_CAP}/market-notes'):
        r = o.get(path)
        assert r.status_code == 404, path
        assert b'Asha' not in r.data and b'KEI' not in r.data
    n_reasons = len(q(srv, 'SELECT id FROM candidate_reasons'))
    n_notes = len(q(srv, 'SELECT id FROM market_notes'))
    assert o.post(f'/api/candidates/{C_MOVE}/reasons', json={'reason_code': 'other'}).status_code == 404
    assert o.post(f'/api/candidates/{C_CAP}/market-notes', json={'signal_type': 'hiring', 'note': 'x'}).status_code == 404
    assert o.put(f'/api/candidates/{C_CAP}/capture', json={'ctc_expected': 99}).status_code == 404
    assert o.post(f'/api/candidates/{C_INT}/stage', json={'stage': 'Not Interested', 'reason_code': 'other'}).status_code == 404
    assert len(q(srv, 'SELECT id FROM candidate_reasons')) == n_reasons
    assert len(q(srv, 'SELECT id FROM market_notes')) == n_notes
    assert one(srv, 'SELECT ctc_expected FROM candidates WHERE id=?', C_CAP)['ctc_expected'] != 99
    # and CO cannot reach the other agency's candidate either
    a = cl(srv, ADMIN)
    assert a.get(f'/api/candidates/{C_FOREIGN}/capture').status_code == 404
    assert a.post(f'/api/candidates/{C_FOREIGN}/reasons', json={'reason_code': 'other'}).status_code == 404


def test_recruiter_scope_and_freelancer(srv):
    r = cl(srv, RIYA)
    assert r.get(f'/api/candidates/{C_RIYA}/capture').status_code == 200          # her own job
    assert r.get(f'/api/candidates/{C_SECRET}/capture').status_code in (403, 404)  # a job she is not on
    assert r.post(f'/api/candidates/{C_SECRET}/market-notes',
                  json={'signal_type': 'hiring', 'note': 'x'}).status_code in (403, 404)
    assert r.post('/api/insight/backfill/start').status_code == 403
    f = cl(srv, FL)
    assert f.post(f'/api/candidates/{C_RIYA}/reasons', json={'reason_code': 'other'}).status_code == 403
    assert f.put(f'/api/candidates/{C_RIYA}/capture', json={'ctc_expected': 5}).status_code == 403
    assert f.post(f'/api/candidates/{C_RIYA}/stage', json={'stage': 'Not Interested', 'reason_code': 'other'}).status_code == 403


def test_script_is_served(srv):
    r = cl(srv, ADMIN).get('/insight-capture.js')
    assert r.status_code == 200 and b'ICap.withReason' in r.data
