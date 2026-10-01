"""
Recruiter access + tenant isolation — permanent leak audit.

Run:   pytest tests/test_recruiter_access.py -q

How it works: an admin's mandate, candidate, invoice, e-mail, reminder …
(every one with id SECRET and the text ZZSECRET) is seeded next to a
recruiter's own mandate. Then EVERY /api route registered in the app — including
routes added after this test was written — is called:

  * as the recruiter (same company, not assigned to the admin's mandate)
  * as the admin of ANOTHER company

Reads must never return ZZSECRET; writes must never change or delete one of the
admin's rows (checked by diffing the database, with a fresh copy restored before
every call so one probe cannot hide another).

If a new feature fails here, map its URL in modules/access.py (_OBJECT_ROUTES /
_ADMIN_ONLY) or add candidate_scope_sql()/mandate_scope_sql() to its query.
"""
import os
import re
import sys
import json
import shutil
import hashlib
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CO, OTHER_CO = 901, 902
ADMIN, RIYA, RAJ, OTHER_ADMIN = 9001, 9002, 9003, 9004
SECRET, OWN, POOL_CAND, POOL_M, FOREIGN = 777777, 555555, 778778, 779779, 999999

# /api paths that are not tenant data or that the probe cannot call sensibly
SKIP_WRITES = ('/api/auth/', '/api/public/', '/api/scheduler/public/', '/api/wa/webhook',
               '/api/admin/', '/api/wa-inbound', '/api/submit')


@pytest.fixture(scope='module')
def env():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='acc_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    # No real network in the probe: every SMTP/IMAP connect fails at once.
    import smtplib, imaplib

    def _refuse(*a, **k):
        raise ConnectionRefusedError('network disabled in tests')
    smtplib.SMTP = smtplib.SMTP_SSL = _refuse
    imaplib.IMAP4_SSL = _refuse
    conn = server.get_db(); c = conn.cursor()
    now = server.ts()
    try:
        server._ensure_wa_suggestions(conn)
    except Exception:
        pass

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})",
                  list(kw.values()))

    ins('companies', id=CO, name='HireLab Test', status='active', created_at=now)
    ins('companies', id=OTHER_CO, name='Other Agency', status='active', created_at=now)
    for uid, un, co, adm in ((ADMIN, 'acc_admin', CO, 1), (RIYA, 'acc_riya', CO, 0),
                             (RAJ, 'acc_raj', CO, 0), (OTHER_ADMIN, 'acc_other', OTHER_CO, 1)):
        ins('users', id=uid, username=un, password_hash='x', display_name=un.split('_')[1].title(),
            role='user', status='approved', company_id=co, is_company_admin=adm, created_at=now)
    ins('mandates', id=SECRET, client='ZZSECRET Client', role='ZZSECRET Role', owner_id=CO,
        assigned_user_id=ADMIN, status='active', created_at=now, jd='ZZSECRET jd')
    ins('mandates', id=OWN, client='RiyaClient', role='RiyaRole', owner_id=CO, assigned_user_id=RIYA,
        status='active', created_at=now, jd='Riya jd')
    ins('mandates', id=POOL_M, client='save for later', role='Central Database', owner_id=CO,
        assigned_user_id=ADMIN, status='central', created_at=now)
    ins('mandates', id=FOREIGN, client='Other', role='OtherRole', owner_id=OTHER_CO,
        assigned_user_id=OTHER_ADMIN, status='active', created_at=now)
    c.execute("INSERT OR REPLACE INTO tenant_settings (company_id, key, value) VALUES (?,?,?)",
              (CO, 'central_mandate_id', str(POOL_M)))
    ins('candidates', id=SECRET, mandate_id=SECRET, name='ZZSECRET Cand', email='zzsecret@x.com',
        phone='9990777777', owner_id=CO, stage='Screening', created_at=now, key_skills='["zzsecret"]',
        company='ZZSECRET Employer', cv_path='zzsecret_cv.pdf')
    ins('candidates', id=OWN, mandate_id=OWN, name='Riya Cand', email='riyacand@x.com',
        phone='9990555555', owner_id=CO, stage='Screening', created_at=now, company='Acme')
    ins('candidates', id=POOL_CAND, mandate_id=POOL_M, name='ZZPOOL Cand', email='zzpool@x.com',
        phone='9990778778', owner_id=CO, stage='Central DB', created_at=now, company='PoolCo')
    ins('invoices', id=SECRET, owner_id=CO, invoice_no='ZZSECRET/1', buyer_name='ZZSECRET Buyer',
        candidate_name='ZZSECRET Cand', mandate_id=SECRET, candidate_id=SECRET, amount=100000,
        created_at=now, status='unpaid')
    ins('emails', id=SECRET, owner_id=CO, subject='ZZSECRET mail', from_addr='boss@x.com',
        to_addr='client@x.com', body='ZZSECRET body', folder='INBOX', candidate_id=SECRET,
        created_at=now, date_ts=1)
    ins('email_messages', id=SECRET, company_id=CO, candidate_id=SECRET, subject='ZZSECRET em',
        body='ZZSECRET', direction='out', created_at=now)
    ins('reminders', id=SECRET, candidate_id=SECRET, mandate_id=SECRET, candidate_name='ZZSECRET Cand',
        note='ZZSECRET rem', due_at='2099-01-01T10:00:00', owner_id=CO, created_at=now, done=0)
    # (due far ahead: the background reminder notifier stamps due reminders,
    #  which is housekeeping, not a probe's doing)
    ins('interviews', id=SECRET, candidate_id=SECRET, mandate_id=SECRET, owner_id=CO,
        round_name='ZZSECRET round', scheduled_at=now, status='scheduled', created_at=now)
    ins('interview_feedback', id=SECRET, interview_id=SECRET, candidate_id=SECRET, owner_id=CO,
        strengths='ZZSECRET fb', mandate_id=SECRET, created_at=now)
    ins('submissions', id=SECRET, owner_id=CO, mandate_id=SECRET, name='ZZSECRET Sub',
        email='zzsub@x.com', status='new', created_at=now)
    ins('submission_drafts', id=SECRET, owner_id=CO, mandate_id=SECRET, subject='ZZSECRET draft',
        candidate_ids=json.dumps([SECRET]), created_at=now)
    ins('offers', id=SECRET, company_id=CO, candidate_id=SECRET, mandate_id=SECRET, status='released',
        notes='ZZSECRET offer', created_at=now)
    ins('command_tasks', id=SECRET, owner_id=CO, text='ZZSECRET task', created_at=now, task_date=now[:10])
    ins('expenses', id=SECRET, owner_id=CO, payee='ZZSECRET payee', amount=5, date=now[:10], created_at=now)
    ins('campaigns', id=SECRET, owner_id=CO, created_by=ADMIN, name='ZZSECRET camp', mandate_id=SECRET,
        subject='ZZSECRET', created_at=now, status='draft')
    ins('notifications', id=SECRET, company_id=CO, user_id=ADMIN, kind='x', title='ZZSECRET notif',
        created_at=now)
    ins('outreach_log', id=SECRET, owner_id=CO, candidate_id=SECRET, mandate_id=SECRET, channel='wa',
        message='ZZSECRET out', created_at=now)
    ins('wa_conversations', id=SECRET, company_id=CO, candidate_id=SECRET, mandate_id=SECRET,
        candidate_name='ZZSECRET wa', created_at=now)
    ins('wa_messages', id=SECRET, conversation_id=SECRET, direction='in', content='ZZSECRET msg', created_at=now)
    ins('wa_escalations', id=SECRET, company_id=CO, conversation_id=SECRET, message_id=SECRET,
        candidate_question='ZZSECRET esc', status='open', created_at=now)
    ins('wa_suggestions', id=SECRET, company_id=CO, candidate_id=SECRET, conversation_id=SECRET,
        message='ZZSECRET sugg', status='pending', created_at=now)
    ins('agent_items', id=SECRET, owner_id=CO, candidate_id=SECRET, mandate_id=SECRET,
        subject='ZZSECRET agent', status='pending', created_at=now)
    ins('meetings', id=SECRET, company_id=CO, host_user_id=ADMIN, candidate_id=SECRET,
        guest_name='ZZSECRET guest', start_at=now[:16], end_at=now[:16], status='confirmed', created_at=now)
    ins('mandate_client_notes', id=SECRET, mandate_id=SECRET, owner_id=CO, note='ZZSECRET note',
        created_by=ADMIN, created_at=now, is_active=1)
    ins('activity_log', user_id=ADMIN, username='acc_admin', action='x', detail='ZZSECRET log',
        created_at=now, company_id=CO)
    ins('crm_clients', id=SECRET, company_id=CO, name='ZZSECRET CRM', status='active', created_at=now)
    ins('bd_opportunities', id=SECRET, company_id=CO, client_id=SECRET, name='ZZSECRET deal',
        stage='lead', created_at=now)
    ins('candidate_events', candidate_id=SECRET, event_type='note', detail='ZZSECRET event', created_at=now)
    conn.commit(); conn.close()
    base = server.DB_PATH + '.acc_base'
    shutil.copy(server.DB_PATH, base)
    yield server, base
    shutil.copy(base, server.DB_PATH)


def client(server, uid):
    cl = server.app.test_client()
    with cl.session_transaction() as s:
        s['user_id'] = uid
    return cl


def _url(rule, ident):
    out = rule.rule
    for a in rule.arguments:
        conv = rule._converters[a].__class__.__name__
        v = str(ident) if conv == 'IntegerConverter' else {
            'entity_type': 'client', 'obj': 'companies', 'kind': 'candidate', 'folder': 'INBOX',
            'tag_type': 'skill', 'filename': 'zzsecret_cv.pdf'}.get(a, 'x')
        if a == 'filename' and 'calls' in rule.rule:
            v = f'call_{ident}_1.mp3'
        out = re.sub(r'<[^>]*' + a + '>', v, out)
    return out


QS = ('?mandate_id={i}&candidate_id={i}&id={i}&q=zzsecret&search=zzsecret&query=zzsecret&all=1'
      '&scope=all&from=2000-01-01&to=2100-01-01&email=zzsecret@x.com&phone=9990777777&status=all')


def _leaks(server, uid, own_id, base=None):
    """Every GET route with the victim's ids AND with the caller's own ids
    (an endpoint on your own mandate must not pull in other people's data),
    plus every POST route that reads (search / match), checked for ZZSECRET."""
    cl = client(server, uid)
    found = []

    def _check(label, resp):
        body = resp.get_data(as_text=True)
        if resp.status_code == 200 and 'ZZSECRET' in body:
            found.append(f'{label}  -> ' + ', '.join(sorted(set(re.findall(r'ZZSECRET[\w /-]{0,14}', body)))[:4]))
            return True
        return False

    for r in sorted(server.app.url_map.iter_rules(), key=lambda r: r.rule):
        if not r.rule.startswith('/api/'):
            continue
        if 'GET' in r.methods:
            for ident in ([SECRET, own_id] if r.arguments else [SECRET]):
                u = _url(r, ident)
                hit = False
                for full in (u, u + QS.format(i=ident)):
                    try:
                        if _check(f'GET {r.rule} [{ident}]', cl.get(full)):
                            hit = True
                            break
                    except Exception:
                        pass
                if hit:
                    break
        elif 'POST' in r.methods and base and not any(x in r.rule for x in SKIP_WRITES):
            shutil.copy(base, server.DB_PATH)
            q = {'query': 'zzsecret', 'q': 'zzsecret', 'jd': 'zzsecret role engineer skills', 'text': 'zzsecret',
                 'top_k': 50, 'must_have': [], 'mandate_id': own_id, 'candidate_id': own_id}
            try:
                _check(f'POST {r.rule}', cl.post(_url(r, own_id), json=q))
            except Exception:
                pass
    if base:
        shutil.copy(base, server.DB_PATH)
    return found


def _victim_rows(server):
    conn = server.get_db()
    out = {}
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info({t})')]
        if 'id' in cols:
            for row in conn.execute(f'SELECT * FROM {t} WHERE id=?', (SECRET,)):
                d = dict(zip(cols, row)); d.pop('updated_at', None)
                # AI-search bookkeeping (init_db queues every un-indexed row for
                # embedding) is platform housekeeping, not a change to the data.
                for k in [k for k in d if k.startswith('embedding')]:
                    d.pop(k)
                out[(t, SECRET)] = hashlib.md5(repr(sorted(d.items())).encode()).hexdigest()
    for tbl, where in (('tenant_settings', f'company_id={CO}'), ('settings', '1=1')):
        for row in conn.execute(f'SELECT key, value FROM {tbl} WHERE {where}'):
            out[(tbl, row[0])] = hashlib.md5(repr(tuple(row)).encode()).hexdigest()
    conn.close()
    return out


def _write_effects(server, base, uid, target_mandate):
    cl = client(server, uid)
    hits = []
    body = {'mandate_id': target_mandate, 'candidate_id': SECRET, 'candidate_ids': [SECRET],
            'type': 'reminder', 'ref_id': SECRET, 'id': SECRET, 'stage': 'Rejected', 'status': 'Rejected',
            'note': 'hack', 'name': 'HACKED', 'text': 'hack', 'recruiter_feedback': 'HACKED',
            'user_id': uid, 'user_ids': [uid], 'assigned_user_id': uid,
            'settings': {'zz_hacked': '1'}, 'mandates': [{'id': 1, 'role': 'x'}],
            'candidates': [{'id': 1, 'mandate_id': SECRET, 'name': 'HACKED'}], 'zz_hacked': '1'}
    for r in sorted(server.app.url_map.iter_rules(), key=lambda r: r.rule):
        if not r.rule.startswith('/api/') or any(x in r.rule for x in SKIP_WRITES):
            continue
        methods = [m for m in ('PUT', 'PATCH', 'POST', 'DELETE') if m in r.methods]
        if not methods:
            continue
        shutil.copy(base, server.DB_PATH)
        before = _victim_rows(server)
        try:
            cl.open(_url(r, SECRET), method=methods[0], data=json.dumps(body), content_type='application/json')
        except Exception:
            pass
        after = _victim_rows(server)
        changed = sorted(f'{k[0]}#{k[1]}' for k in before if before[k] != after.get(k))
        if changed:
            hits.append(f'{methods[0]} {r.rule} -> ' + ', '.join(changed[:4]))
    shutil.copy(base, server.DB_PATH)
    return hits


# ── the audit ─────────────────────────────────────────────────────────────
def test_recruiter_cannot_read_unassigned_data(env):
    server, base = env
    leaks = _leaks(server, RIYA, OWN, base)
    assert not leaks, 'Recruiter can read the admin\'s data:\n  ' + '\n  '.join(leaks)


def test_recruiter_cannot_change_unassigned_data(env):
    server, base = env
    hits = _write_effects(server, base, RIYA, OWN)
    assert not hits, 'Recruiter changed the admin\'s data:\n  ' + '\n  '.join(hits)


def test_other_agency_cannot_read(env):
    server, base = env
    leaks = _leaks(server, OTHER_ADMIN, FOREIGN, base)
    assert not leaks, 'Another agency can read this agency\'s data:\n  ' + '\n  '.join(leaks)


def test_other_agency_cannot_change(env):
    server, base = env
    hits = _write_effects(server, base, OTHER_ADMIN, FOREIGN)
    assert not hits, 'Another agency changed this agency\'s data:\n  ' + '\n  '.join(hits)


# ── what the recruiter SHOULD be able to do ───────────────────────────────
def test_recruiter_daily_work(env):
    server, base = env
    shutil.copy(base, server.DB_PATH)
    cl = client(server, RIYA)
    ms = cl.get('/api/mandates').get_json()
    assert [m['id'] for m in ms] == [OWN]
    assert ms[0]['assignees'][0]['user_id'] == RIYA and ms[0]['assignees'][0]['is_primary']
    assert cl.get(f'/api/mandates/{OWN}').status_code == 200
    assert cl.get(f'/api/mandates/{OWN}/candidates').status_code == 200
    assert cl.get(f'/api/candidates/{OWN}').status_code == 200
    assert cl.post(f'/api/candidates/{OWN}/stage', json={'stage': 'Shortlisted'}).status_code == 200
    assert cl.post('/api/reminders', json={'candidate_id': OWN, 'note': 'call back',
                                           'due_at': server.ts()}).status_code == 200
    # admin modules are closed
    for path in ('/api/invoices', '/api/emailbox/list', '/api/crm/clients', '/api/bd/home',
                 '/api/analytics', '/api/command/tasks', '/api/campaigns', '/api/expenses'):
        assert cl.get(path).status_code == 403, path
    assert cl.post('/api/settings', json={'x': '1'}).status_code == 403
    assert cl.post('/api/import', json={'mandates': [{'id': 1}]}).status_code == 403


def test_central_pool_rules(env):
    server, base = env
    shutil.copy(base, server.DB_PATH)
    cl = client(server, RIYA)
    res = cl.get('/api/central-db/search').get_json()
    names = {r['name'] for r in res.get('results', res.get('candidates', []))} if isinstance(res, dict) else set()
    assert 'ZZPOOL Cand' in names and 'ZZSECRET Cand' not in names
    assert cl.get(f'/api/candidates/{POOL_CAND}').status_code == 200            # read-only view
    assert cl.put(f'/api/candidates/{POOL_CAND}', json={'name': 'x'}).status_code == 404
    assert cl.post(f'/api/candidates/{POOL_CAND}/move', json={'mandate_id': SECRET}).status_code == 404
    r = cl.post(f'/api/candidates/{POOL_CAND}/move', json={'mandate_id': OWN})
    assert r.status_code == 200
    conn = server.get_db()
    assert conn.execute('SELECT mandate_id FROM candidates WHERE id=?', (POOL_CAND,)).fetchone()[0] == OWN
    conn.close()
    shutil.copy(base, server.DB_PATH)


def test_multi_recruiter_assignment(env):
    server, base = env
    shutil.copy(base, server.DB_PATH)
    adm, riya, raj = client(server, ADMIN), client(server, RIYA), client(server, RAJ)
    assert raj.get(f'/api/mandates/{SECRET}').status_code == 404
    assert riya.put(f'/api/access/mandates/{SECRET}/assignees', json={'user_ids': [RIYA]}).status_code == 403
    r = adm.put(f'/api/access/mandates/{SECRET}/assignees',
                json={'user_ids': [RIYA, RAJ], 'primary_user_id': RAJ})
    assert r.status_code == 200, r.get_json()
    got = {a['user_id']: a['is_primary'] for a in r.get_json()['assignees']}
    assert got == {RIYA: 0, RAJ: 1}
    # both see it, shared pipeline
    for cl in (riya, raj):
        assert SECRET in [m['id'] for m in cl.get('/api/mandates').get_json()]
        assert cl.get(f'/api/candidates/{SECRET}').status_code == 200
    conn = server.get_db()
    assert conn.execute('SELECT assigned_user_id FROM mandates WHERE id=?', (SECRET,)).fetchone()[0] == RAJ
    n = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id IN (?,?) AND kind='mandate_assigned' "
                     "AND mandate_id=?", (RIYA, RAJ, SECRET)).fetchone()[0]
    conn.close()
    assert n == 2
    # a recruiter cannot hand the mandate to someone else through an edit
    assert riya.put(f'/api/mandates/{SECRET}', json={'assigned_user_id': RIYA}).status_code == 403
    # removing a recruiter takes effect on the next request
    r = adm.put(f'/api/access/mandates/{SECRET}/assignees', json={'user_ids': [RAJ]})
    assert r.status_code == 200
    assert riya.get(f'/api/candidates/{SECRET}').status_code == 404
    assert SECRET not in [m['id'] for m in riya.get('/api/mandates').get_json()]
    # foreign users are refused
    assert adm.put(f'/api/access/mandates/{SECRET}/assignees', json={'user_ids': [OTHER_ADMIN]}).status_code == 400
    # the legacy single "assign" still works and becomes the new primary
    assert adm.post(f'/api/mandates/{SECRET}/assign', json={'user_id': RIYA}).status_code == 200
    a = {x['user_id']: x['is_primary'] for x in adm.get(f'/api/access/mandates/{SECRET}/assignees').get_json()['assignees']}
    assert a == {RIYA: 1}
    shutil.copy(base, server.DB_PATH)


def test_admin_is_unaffected(env):
    server, base = env
    shutil.copy(base, server.DB_PATH)
    adm = client(server, ADMIN)
    ids = {m['id'] for m in adm.get('/api/mandates').get_json()}
    assert {SECRET, OWN} <= ids
    assert adm.get(f'/api/candidates/{SECRET}').status_code == 200
    assert adm.get('/api/invoices').status_code == 200
    assert 'ZZSECRET' in adm.get('/api/central-db/search').get_data(as_text=True)


def test_new_mandate_by_recruiter_is_theirs(env):
    server, base = env
    shutil.copy(base, server.DB_PATH)
    riya = client(server, RIYA)
    conn = server.get_db()
    conn.execute('INSERT INTO mandates (id, client, role, owner_id, assigned_user_id, status, created_at) '
                 'VALUES (?,?,?,?,?,?,?)', (123123, 'New', 'NewRole', CO, RIYA, 'active', server.ts()))
    conn.commit(); conn.close()
    assert 123123 in [m['id'] for m in riya.get('/api/mandates').get_json()]
    shutil.copy(base, server.DB_PATH)


def test_ai_ranking_is_scoped(env):
    """Semantic search, JD matching and candidate->mandate matching rank the
    whole candidate table; a recruiter must only ever get their own + pool."""
    import numpy as np
    server, base = env
    shutil.copy(base, server.DB_PATH)
    vec = np.asarray([1.0, 0.0, 0.0], dtype=np.float32).tobytes()
    conn = server.get_db()
    for cid in (SECRET, OWN, POOL_CAND):
        conn.execute("UPDATE candidates SET embedding='[1,0,0]', embedding_vec=?, key_skills='[\"scada\"]' "
                     "WHERE id=?", (vec, cid))
    for mid in (SECRET, OWN):
        conn.execute("INSERT INTO mandate_vectors (mandate_id, embedding_vec, status) VALUES (?,?,'completed')",
                     (mid, vec))
    conn.commit(); conn.close()

    def ranked_ids(uid):
        with server.app.test_request_context('/api/ai/search'):
            from flask import session
            session['user_id'] = uid
            c = server.get_db()
            ranked, _ = server._search_rank(c, [1.0, 0.0, 0.0], 0.0, 1000, 50, CO)
            hydrated = server._search_hydrate(c, ranked, CO)
            c.close()
        return {cid for _, cid in ranked}, {h['id'] for h in hydrated}

    r_ids, r_h = ranked_ids(RIYA)
    assert r_ids == {OWN, POOL_CAND} and r_h == {OWN, POOL_CAND}
    a_ids, _ = ranked_ids(ADMIN)
    assert {SECRET, OWN, POOL_CAND} <= a_ids

    with server.app.test_request_context('/api/jd/match'):
        from flask import session
        session['user_id'] = RIYA
        c = server.get_db()
        cands, _, _ = server._match_candidates(c, CO, 'Need SCADA engineer with scada experience', ['scada'])
        c.close()
    assert SECRET not in {x.get('id') for x in cands}

    riya = client(server, RIYA)
    res = riya.get(f'/api/candidates/{OWN}/match-mandates').get_json()
    assert res['ok'] and [r['mandate_id'] for r in res['results']] == [OWN]
    shutil.copy(base, server.DB_PATH)
