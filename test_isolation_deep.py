"""
Deep isolation audit (Oct 2026) — the gaps test_recruiter_access.py does not cover.

Run:   pytest test_isolation_deep.py -q

Re-uses the seeded world of test_recruiter_access.py (an admin's SECRET
mandate/candidate/invoice ... marked ZZSECRET, a recruiter, another agency)
and adds:

  * an anonymous visitor (no login) calling every GET route, pages included
  * a freelancer of the same company
  * real files on disk (CV, call recording, e-mail attachment, CRM file)
  * write probes aimed at PEOPLE and SETTINGS (users, company, global settings),
    which the id=SECRET diff of the older probe cannot see
  * duplicate-check probes: another agency posting the victim's phone / e-mail
  * platform settings: never readable anonymously, never writable by a tenant
"""
import os
import re
import json
import shutil
import hashlib

import pytest

from test_recruiter_access import (env, client, _url, QS, SKIP_WRITES,  # noqa: F401  (fixture)
                                   CO, OTHER_CO, ADMIN, RIYA, RAJ, OTHER_ADMIN, SECRET, OWN, FOREIGN)

FL = 9005
SELF = {}
MARK = 'ZZSECRET'


@pytest.fixture(scope='module')
def world(env):
    server, base = env
    shutil.copy(base, server.DB_PATH)
    conn = server.get_db()
    now = server.ts()
    conn.execute("INSERT OR REPLACE INTO users (id, username, password_hash, display_name, role, status, company_id, "
                 "is_company_admin, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (FL, 'acc_fl', 'x', 'Fl', 'freelancer_sourcer', 'approved', CO, 0, now))
    # platform-wide rows an agency must never read or change
    for k, v in (('email_agent_kb', 'ZZSECRET platform kb'), ('billing_legal_name', 'ZZSECRET Legal'),
                 ('cc_bank_cash', 'ZZSECRET 900000'), ('default_submission_company', str(CO))):
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)', (k, v))
    conn.commit(); conn.close()
    # real files belonging to the SECRET candidate
    files = {
        os.path.join(server.CV_DIR, 'zzsecret_cv.pdf'): b'%PDF-1.4 ZZSECRET cv body',
        os.path.join(server.DATA_DIR, 'calls', f'call_{SECRET}_1.mp3'): b'ZZSECRET audio',
        os.path.join(server.EMAIL_ATTACH_DIR, 'zzsecret_att.pdf'): b'%PDF-1.4 ZZSECRET attachment',
        os.path.join(server.CRM_FILES_DIR, 'zzsecret_crm.pdf'): b'%PDF-1.4 ZZSECRET crm',
    }
    for p, data in files.items():
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as f:
            f.write(data)
    deep = server.DB_PATH + '.deep_base'
    shutil.copy(server.DB_PATH, deep)
    yield server, deep
    shutil.copy(base, server.DB_PATH)


def _hit(resp):
    if resp.status_code != 200:
        return ''
    body = resp.get_data()
    try:
        body = body.decode('utf-8', 'ignore')
    except Exception:
        body = str(body)
    if MARK in body:
        return ', '.join(sorted(set(re.findall(MARK + r'[\w /%.-]{0,16}', body)))[:3])
    return ''


def _get_probe(server, cl, idents, include_pages=True):
    found = []
    names = {'filename': ['zzsecret_cv.pdf', f'call_{SECRET}_1.mp3', 'zzsecret_att.pdf', 'zzsecret_crm.pdf'],
             'path': ['zzsecret_cv.pdf'], 'fname': ['zzsecret_cv.pdf', 'zzsecret_att.pdf'],
             'name': ['zzsecret_cv.pdf', 'zzsecret_att.pdf', 'zzsecret_crm.pdf']}
    for r in sorted(server.app.url_map.iter_rules(), key=lambda r: r.rule):
        if 'GET' not in r.methods:
            continue
        if not include_pages and not r.rule.startswith('/api/'):
            continue
        urls = set()
        for ident in (idents if r.arguments else idents[:1]):
            u = _url(r, ident)
            urls.add(u)
            urls.add(u + QS.format(i=ident))
            for a in r.arguments:                       # real file names for file routes
                for fn in names.get(a, []):
                    urls.add(re.sub(r'<[^>]*' + a + '>', fn, r.rule))
        for u in sorted(urls):
            try:
                h = _hit(cl.get(u))
            except Exception:
                h = ''
            if h:
                found.append(f'GET {u} -> {h}')
                break
    return found


# ── 1. nobody logged in ─────────────────────────────────────────────────────
def test_anonymous_reads_nothing(world):
    server, deep = world
    shutil.copy(deep, server.DB_PATH)
    leaks = _get_probe(server, server.app.test_client(), [SECRET])
    assert not leaks, 'Visible WITHOUT login:\n  ' + '\n  '.join(leaks)


# ── 2. a freelancer of the same company ────────────────────────────────────
def test_freelancer_reads_nothing_unassigned(world):
    server, deep = world
    shutil.copy(deep, server.DB_PATH)
    leaks = _get_probe(server, client(server, FL), [SECRET], include_pages=False)
    assert not leaks, 'Freelancer can read the admin\'s data:\n  ' + '\n  '.join(leaks)


# ── 3. files on disk ────────────────────────────────────────────────────────
@pytest.mark.parametrize('who', [RIYA, OTHER_ADMIN])
def test_files_stay_private(world, who):
    server, deep = world
    shutil.copy(deep, server.DB_PATH)
    cl = client(server, who)
    found = []
    for r in server.app.url_map.iter_rules():
        if 'GET' not in r.methods or not any(a in r.arguments for a in ('filename', 'path', 'fname', 'name')):
            continue
        for fn in ('zzsecret_cv.pdf', f'call_{SECRET}_1.mp3', 'zzsecret_att.pdf', 'zzsecret_crm.pdf'):
            u = r.rule
            for a in r.arguments:
                u = re.sub(r'<[^>]*' + a + '>', fn if a in ('filename', 'path', 'fname', 'name') else str(SECRET), u)
            h = _hit(cl.get(u))
            if h:
                found.append(f'GET {u} -> {h}')
    assert not found, 'Private files readable:\n  ' + '\n  '.join(found)


# ── 4. writes aimed at people, the company and settings ────────────────────
def _snapshot(server):
    conn = server.get_db()
    out = {}
    for uid in (ADMIN, RIYA, RAJ, FL):
        row = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        d = dict(row) if row else {}
        d.pop('last_login', None); d.pop('last_seen', None)
        if uid == SELF.get('uid'):                      # editing your own profile is fine;
            d = {k: d.get(k) for k in ('role', 'is_company_admin', 'company_id', 'status', 'username')}
        out[('user', uid)] = hashlib.md5(repr(sorted(d.items())).encode()).hexdigest()
    row = conn.execute('SELECT * FROM companies WHERE id=?', (CO,)).fetchone()
    out[('company', CO)] = hashlib.md5(repr(sorted(dict(row).items())).encode()).hexdigest()
    # whole tables: a NEW global key is a change too
    out[('settings', '*')] = hashlib.md5(repr(sorted(
        tuple(r) for r in conn.execute('SELECT key, value FROM settings'))).encode()).hexdigest()
    out[('tenant_settings', CO)] = hashlib.md5(repr(sorted(
        tuple(r) for r in conn.execute('SELECT key, value FROM tenant_settings WHERE company_id=?', (CO,)))).encode()).hexdigest()
    out[('assignees', CO)] = hashlib.md5(repr(sorted(
        tuple(r) for r in conn.execute('SELECT mandate_id, user_id, is_active, is_primary FROM mandate_assignees '
                                       'WHERE company_id=?', (CO,)))).encode()).hexdigest()
    conn.close()
    return out


def _people_writes(server, deep, uid, idents):
    cl = client(server, uid)
    SELF['uid'] = uid
    hits = []
    body = {'user_id': ADMIN, 'id': ADMIN, 'company_id': CO, 'role': 'admin', 'is_company_admin': 1,
            'status': 'disabled', 'password': 'Hacked123!', 'new_password': 'Hacked123!',
            'display_name': 'HACKED', 'email': 'hacker@x.com', 'name': 'HACKED', 'user_limit': 999,
            'default_submission_company': str(OTHER_CO), 'email_agent_kb': 'HACKED',
            'billing_price_per_recruiter': '1', 'zz_new_global_key': 'HACKED'}
    for r in sorted(server.app.url_map.iter_rules(), key=lambda r: r.rule):
        if not r.rule.startswith('/api/') or any(x in r.rule for x in SKIP_WRITES):
            continue
        methods = [m for m in ('PUT', 'PATCH', 'POST', 'DELETE') if m in r.methods]
        if not methods:
            continue
        for ident in (idents if r.arguments else idents[:1]):
            shutil.copy(deep, server.DB_PATH)
            before = _snapshot(server)
            try:
                cl.open(_url(r, ident), method=methods[0], data=json.dumps(body), content_type='application/json')
            except Exception:
                pass
            after = _snapshot(server)
            changed = sorted(f'{k[0]}#{k[1]}' for k in before if before[k] != after.get(k))
            if changed:
                hits.append(f'{methods[0]} {_url(r, ident)} -> ' + ', '.join(changed))
                break
    shutil.copy(deep, server.DB_PATH)
    return hits


def test_other_agency_cannot_touch_people_or_settings(world):
    server, deep = world
    hits = _people_writes(server, deep, OTHER_ADMIN, [ADMIN, CO, RIYA])
    assert not hits, 'Another agency changed this agency\'s users/company/settings:\n  ' + '\n  '.join(hits)


def test_recruiter_cannot_touch_people_or_settings(world):
    server, deep = world
    hits = _people_writes(server, deep, RIYA, [ADMIN, CO, RAJ])
    assert not hits, 'A recruiter changed users/company/settings:\n  ' + '\n  '.join(hits)


# ── 5. duplicate checks must not reveal another agency's people ────────────
def test_duplicate_checks_do_not_cross_agencies(world):
    server, deep = world
    cl = client(server, OTHER_ADMIN)
    body = {'phone': '9990777777', 'email': 'zzsecret@x.com', 'mobile': '9990777777', 'name': 'Dup Person',
            'mandate_id': FOREIGN, 'text': 'Dup Person 9990777777 zzsecret@x.com',
            'candidates': [{'name': 'Dup', 'phone': '9990777777', 'email': 'zzsecret@x.com'}],
            'profiles': [{'name': 'Dup', 'phone': '9990777777', 'email': 'zzsecret@x.com'}]}
    found = []
    for r in sorted(server.app.url_map.iter_rules(), key=lambda r: r.rule):
        if not r.rule.startswith('/api/') or 'POST' not in r.methods or any(x in r.rule for x in SKIP_WRITES):
            continue
        shutil.copy(deep, server.DB_PATH)
        conn = server.get_db()
        before = dict(conn.execute('SELECT * FROM candidates WHERE id=?', (SECRET,)).fetchone())
        conn.close()
        try:
            resp = cl.post(_url(r, FOREIGN), json=body)
            h = _hit(resp)
            if h:
                found.append(f'POST {r.rule} reveals -> {h}')
        except Exception:
            pass
        conn = server.get_db()
        after = dict(conn.execute('SELECT * FROM candidates WHERE id=?', (SECRET,)).fetchone())
        conn.close()
        for d in (before, after):
            for k in [k for k in d if k.startswith('embedding') or k == 'updated_at']:
                d.pop(k)
        if before != after:
            found.append(f'POST {r.rule} changed the other agency\'s candidate')
    shutil.copy(deep, server.DB_PATH)
    assert not found, '\n  '.join(found)


# ── 6. platform settings ───────────────────────────────────────────────────
def test_platform_settings_are_not_public(world):
    server, deep = world
    shutil.copy(deep, server.DB_PATH)
    r = server.app.test_client().get('/api/settings')
    assert r.status_code in (401, 403) or MARK not in r.get_data(as_text=True)
    other = client(server, OTHER_ADMIN).get('/api/settings').get_data(as_text=True)
    assert MARK not in other, 'Another agency sees platform-only settings'


# ── 7. EVERY table, not just the ones seeded above ─────────────────────────
def _seed_every_table(server):
    """One ZZSECRET row in every table that carries a tenant (owner_id /
    company_id) or hangs off a candidate/mandate, owned by the victim."""
    conn = server.get_db()
    seeded = []
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                         "AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        cols = list(conn.execute(f'PRAGMA table_info("{t}")'))
        names = [c[1] for c in cols]
        tenant_col = next((c for c in ('owner_id', 'company_id', 'oid', 'tenant_id') if c in names), None)
        parent = next((c for c in ('candidate_id', 'mandate_id') if c in names), None)
        if not tenant_col and not parent:
            continue
        if t in ('users', 'companies', 'settings', 'tenant_settings', 'mandate_assignees'):
            continue
        row = {}
        for cid, name, typ, notnull, dflt, pk in cols:
            typ = (typ or '').upper()
            if name in ('owner_id', 'company_id', 'oid', 'tenant_id'):
                row[name] = CO
            elif name in ('candidate_id', 'mandate_id', 'conversation_id', 'interview_id', 'reminder_id',
                          'client_id', 'campaign_id', 'list_id'):
                row[name] = SECRET
            elif name in ('user_id', 'created_by', 'host_user_id', 'assigned_to', 'actor_id', 'added_by'):
                row[name] = ADMIN
            elif pk and 'INT' in typ:
                row[name] = SECRET + 17
            elif 'INT' in typ or 'REAL' in typ or 'NUM' in typ:
                row[name] = 0
            elif 'BLOB' in typ:
                continue
            elif name.endswith('_at') or name in ('date', 'day', 'task_date'):
                row[name] = server.ts()[:10] if name in ('date', 'day', 'task_date') else server.ts()
            else:
                row[name] = f'ZZSECRET {t}.{name}'
        try:
            conn.execute(f'INSERT OR IGNORE INTO "{t}" ({",".join(chr(34) + k + chr(34) for k in row)}) '
                         f'VALUES ({",".join("?" * len(row))})', list(row.values()))
            seeded.append(t)
        except Exception:
            pass
    conn.commit(); conn.close()
    return seeded


@pytest.fixture(scope='module')
def full_world(world):
    server, deep = world
    shutil.copy(deep, server.DB_PATH)
    seeded = _seed_every_table(server)
    # init_db()'s boot-time backfills tidy the synthetic rows once; run them
    # here so a route that calls init_db() (backup import) is not mistaken for a write
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        server.init_db()
    full = server.DB_PATH + '.full_base'
    shutil.copy(server.DB_PATH, full)
    yield server, full, seeded
    shutil.copy(deep, server.DB_PATH)


@pytest.mark.parametrize('who', ['anon', OTHER_ADMIN, FL])
def test_every_table_is_private(full_world, who):
    server, full, seeded = full_world
    shutil.copy(full, server.DB_PATH)
    cl = server.app.test_client() if who == 'anon' else client(server, who)
    leaks = _get_probe(server, cl, [SECRET, SECRET + 17], include_pages=(who == 'anon'))
    assert not leaks, f'{who} reads ({len(seeded)} tables seeded):\n  ' + '\n  '.join(leaks)


def test_every_table_recruiter_report(full_world):
    """Recruiter view: printed for review (company-wide modules may be shared on purpose)."""
    server, full, seeded = full_world
    shutil.copy(full, server.DB_PATH)
    leaks = _get_probe(server, client(server, RIYA), [SECRET, SECRET + 17], include_pages=False)
    # Company-wide SETTINGS and counts every member uses on purpose (no
    # candidate, job or person in them): shared tag names, the company skill
    # dictionary, Job-Match Mail defaults, outreach totals.
    shared = ('/api/jobmail/settings', '/api/outreach/stats', '/api/skill-graph', '/api/todo/meta', '/api/todo/tags')
    leaks = [x for x in leaks if not any(x.startswith('GET ' + p + ' ') or x.startswith('GET ' + p + '?')
                                         for p in shared)]
    assert not leaks, 'Recruiter reads:\n  ' + '\n  '.join(leaks)


def _tenant_snapshot(server, seeded):
    conn = server.get_db()
    out = {}
    for t in seeded:
        cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{t}")')]
        tc = next((c for c in ('owner_id', 'company_id', 'oid', 'tenant_id') if c in cols), None)
        where = f'"{tc}"={CO}' if tc else ('candidate_id IN (%d,%d)' % (SECRET, SECRET + 17) if 'candidate_id' in cols
                                           else 'mandate_id IN (%d,%d)' % (SECRET, SECRET + 17))
        # (the background reminder notifier stamps these on due rows at any moment)
        keep = [c for c in cols if not (c.startswith('embedding') or c in ('updated_at', 'last_seen', 'last_login',
                                                                           'notified_at', 'last_run_at',
                                                                           'early_warned', 'notify_count'))]
        rows = conn.execute(f'SELECT {",".join(chr(34) + c + chr(34) for c in keep)} FROM "{t}" WHERE {where}').fetchall()
        out[t] = hashlib.md5(repr(sorted(tuple(map(str, r)) for r in rows)).encode()).hexdigest()
    conn.close()
    return out


def test_other_agency_cannot_change_any_table(full_world):
    server, full, seeded = full_world
    cl = client(server, OTHER_ADMIN)
    body = {'mandate_id': SECRET, 'candidate_id': SECRET, 'candidate_ids': [SECRET, SECRET + 17],
            'id': SECRET + 17, 'ids': [SECRET, SECRET + 17], 'ref_id': SECRET, 'type': 'reminder',
            'stage': 'Rejected', 'status': 'deleted', 'note': 'hack', 'name': 'HACKED', 'text': 'hack',
            'client_id': SECRET, 'list_id': SECRET, 'campaign_id': SECRET, 'conversation_id': SECRET}
    hits = []
    for r in sorted(server.app.url_map.iter_rules(), key=lambda r: r.rule):
        if not r.rule.startswith('/api/') or any(x in r.rule for x in SKIP_WRITES):
            continue
        methods = [m for m in ('PUT', 'PATCH', 'POST', 'DELETE') if m in r.methods]
        if not methods:
            continue
        for ident in ([SECRET, SECRET + 17] if r.arguments else [SECRET]):
            shutil.copy(full, server.DB_PATH)
            before = _tenant_snapshot(server, seeded)
            try:
                cl.open(_url(r, ident), method=methods[0], data=json.dumps(body), content_type='application/json')
            except Exception:
                pass
            after = _tenant_snapshot(server, seeded)
            changed = sorted(t for t in before if before[t] != after.get(t))
            if changed:
                hits.append(f'{methods[0]} {_url(r, ident)} -> ' + ', '.join(changed[:5]))
                break
    shutil.copy(full, server.DB_PATH)
    assert not hits, 'Another agency changed this agency\'s rows:\n  ' + '\n  '.join(hits)


# ── 8. id routes the generic probe cannot trigger (need a real body / data shape) ──
def test_targeted_id_routes(world, monkeypatch):
    server, deep = world
    shutil.copy(deep, server.DB_PATH)
    conn = server.get_db()
    conn.execute("UPDATE mandates SET email_templates=? WHERE id=?",
                 (json.dumps([{'name': 'ZZSECRET tpl', 'subject': 'ZZSECRET', 'body': 'x'}]), SECRET))
    conn.execute("INSERT INTO candidate_events (candidate_id, event_type, detail, created_at) VALUES (?,?,?,?)",
                 (SECRET, 'email', 'ZZSECRET sent offer mail', server.ts()))
    conn.execute("INSERT OR REPLACE INTO wa_learned_responses (id, company_id, intent_pattern, response_template, "
                 "is_active, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
                 (SECRET, CO, 'ZZSECRET intent', 'ZZSECRET reply', server.ts(), server.ts()))
    conn.commit(); conn.close()

    class _R:
        status_code = 200
        def __init__(self, payload): self._p = payload
        def json(self): return {'choices': [{'message': {'content': self._p}}], 'usage': {}}
    sent = []
    monkeypatch.setattr(server, 'call_deepseek', lambda key, payload, **k: sent.append(json.dumps(payload)) or _R('ok'),
                        raising=False)
    real = server.get_setting
    monkeypatch.setattr(server, 'get_setting', lambda k, default='': 'k' if k == 'deepseek_api_key' else real(k, default))

    found = []
    for who in (OTHER_ADMIN, RIYA):
        cl = client(server, who)
        checks = [
            ('GET', f'/api/mandates/{SECRET}/email-templates', None),
            ('GET', f'/api/candidates/{SECRET}/email-history', None),
            ('POST', f'/api/candidates/{SECRET}/interview-message', {'scheduled_at': '2026-11-01T10:00'}),
            ('POST', f'/api/candidates/{SECRET}/ai-compose', {'command': 'write a follow up'}),
        ]
        for m, u, b in checks:
            sent.clear()
            r = cl.open(u, method=m, json=b) if b is not None else cl.open(u, method=m)
            if _hit(r) or any(MARK in s for s in sent):
                found.append(f'{who}: {m} {u} reveals the other side\'s data')
        cl.post(f'/api/mandates/{SECRET}/email-templates', json={'templates': []})
        cl.delete(f'/api/wa/learned/{SECRET}')
        conn = server.get_db()
        if 'ZZSECRET' not in (conn.execute('SELECT email_templates FROM mandates WHERE id=?', (SECRET,)).fetchone()[0] or ''):
            found.append(f'{who}: POST /api/mandates/<id>/email-templates overwrote the other side\'s templates')
        if conn.execute('SELECT is_active FROM wa_learned_responses WHERE id=?', (SECRET,)).fetchone()[0] != 1:
            found.append(f'{who}: DELETE /api/wa/learned/<id> switched off the other side\'s learned reply')
        conn.close()
        shutil.copy(deep, server.DB_PATH)
        conn = server.get_db()
        conn.execute("UPDATE mandates SET email_templates=? WHERE id=?",
                     (json.dumps([{'name': 'ZZSECRET tpl'}]), SECRET))
        conn.execute("INSERT INTO candidate_events (candidate_id, event_type, detail, created_at) VALUES (?,?,?,?)",
                     (SECRET, 'email', 'ZZSECRET sent offer mail', server.ts()))
        conn.execute("INSERT OR REPLACE INTO wa_learned_responses (id, company_id, intent_pattern, response_template, "
                     "is_active, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
                     (SECRET, CO, 'ZZSECRET intent', 'ZZSECRET reply', server.ts(), server.ts()))
        conn.commit(); conn.close()
    shutil.copy(deep, server.DB_PATH)
    assert not found, '\n  '.join(found)


def test_post_with_foreign_ids_reveals_nothing(full_world, monkeypatch):
    """Another agency POSTs the victim's ids in the BODY (not the URL) to every
    route; neither the response nor what is sent to the AI may carry their data."""
    server, full, seeded = full_world
    sent = []

    class _R:
        status_code = 200
        def __init__(self, p): self._p = p
        def json(self): return {'choices': [{'message': {'content': '{}'}}], 'usage': {}}
    monkeypatch.setattr(server, 'call_deepseek', lambda key, payload, **k: sent.append(json.dumps(payload)) or _R(''))
    if hasattr(server, 'call_claude'):
        monkeypatch.setattr(server, 'call_claude', lambda *a, **k: sent.append(json.dumps([str(x) for x in a])) or '')
    real = server.get_setting
    monkeypatch.setattr(server, 'get_setting', lambda k, default='': 'k' if k.endswith('api_key') else real(k, default))
    cl = client(server, OTHER_ADMIN)
    body = {'candidate_id': SECRET, 'candidate_ids': [SECRET], 'mandate_id': SECRET, 'mandate_ids': [SECRET],
            'id': SECRET, 'ids': [SECRET], 'cid': SECRET, 'mid': SECRET, 'ref_id': SECRET, 'type': 'reminder',
            'interview_id': SECRET, 'client_id': SECRET, 'conversation_id': SECRET, 'campaign_id': SECRET,
            'command': 'write a short email', 'text': 'hello', 'query': 'engineer', 'q': 'engineer',
            'scheduled_at': '2026-11-01T10:00', 'to': 'x@example.com', 'subject': 's', 'body': 'b'}
    found = []
    for r in sorted(server.app.url_map.iter_rules(), key=lambda r: r.rule):
        if not r.rule.startswith('/api/') or 'POST' not in r.methods or any(x in r.rule for x in SKIP_WRITES):
            continue
        shutil.copy(full, server.DB_PATH)
        sent.clear()
        try:
            resp = cl.post(_url(r, FOREIGN), json=body)
            h = _hit(resp)
        except Exception:
            h = ''
        if h or any(MARK in s for s in sent):
            found.append(f'POST {r.rule} -> ' + (h or 'sent to AI'))
    shutil.copy(full, server.DB_PATH)
    assert not found, 'Victim ids in the body leak data:\n  ' + '\n  '.join(found)



# ── 9. the fixes of Oct 2026, one by one ───────────────────────────────────
def test_ai_question_sees_only_own_company(world, monkeypatch):
    server, deep = world
    shutil.copy(deep, server.DB_PATH)
    sent = []

    class _R:
        status_code = 200
        def json(self): return {'choices': [{'message': {'content': 'answer'}}], 'usage': {}}
    monkeypatch.setattr(server, 'call_deepseek', lambda key, payload, **k: sent.append(json.dumps(payload)) or _R())
    real = server.get_setting
    monkeypatch.setattr(server, 'get_setting', lambda k, default='': 'k' if k == 'deepseek_api_key' else real(k, default))
    r = client(server, OTHER_ADMIN).post('/api/ai/stats', json={'question': 'list every candidate'})
    assert r.status_code == 200 and sent and MARK not in sent[-1]
    client(server, ADMIN).post('/api/ai/stats', json={'question': 'list every candidate'})
    assert MARK in sent[-1]                                   # the agency still gets its own data


def test_agency_cannot_write_platform_settings_but_owner_can(world):
    server, deep = world
    shutil.copy(deep, server.DB_PATH)
    r = client(server, OTHER_ADMIN).post('/api/settings', json={'default_submission_company': str(OTHER_CO),
                                                                 'deepseek_api_key': 'B', 'zz_new': '1',
                                                                 'recruiter_name': 'Other Agency Desk'})
    assert r.status_code == 200 and set(r.get_json().get('skipped', [])) >= {'default_submission_company',
                                                                             'deepseek_api_key', 'zz_new'}
    conn = server.get_db()
    g = dict(conn.execute('SELECT key, value FROM settings').fetchall())
    own = conn.execute("SELECT value FROM tenant_settings WHERE company_id=? AND key='recruiter_name'",
                       (OTHER_CO,)).fetchone()
    conn.close()
    assert g.get('default_submission_company') == str(CO) and 'zz_new' not in g and g.get('deepseek_api_key') != 'B'
    assert own and own[0] == 'Other Agency Desk'              # its own settings still save
    conn = server.get_db()
    conn.execute("INSERT OR REPLACE INTO users (id, username, password_hash, role, status, company_id, created_at) "
                 "VALUES (9099, 'acc_owner', 'x', 'admin', 'approved', ?, ?)", (CO, server.ts()))
    conn.commit(); conn.close()
    client(server, 9099).post('/api/settings', json={'email_sync_days': '45'})
    conn = server.get_db()
    assert conn.execute("SELECT value FROM settings WHERE key='email_sync_days'").fetchone()[0] == '45'
    conn.close()
    shutil.copy(deep, server.DB_PATH)


def test_no_fallback_to_owner_mail_or_kb(world):
    server, deep = world
    shutil.copy(deep, server.DB_PATH)
    conn = server.get_db()
    conn.execute("INSERT OR REPLACE INTO users (id, username, password_hash, role, status, company_id, created_at) "
                 "VALUES (9099, 'acc_owner', 'x', 'admin', 'approved', ?, ?)", (CO, server.ts()))
    for k, v in (('smtp_email', 'owner@gmail.com'), ('smtp_app_password', 'abcd efgh ijkl mnop'),
                 ('seller_gstin', '09ZZSECRETGST'), ('embedding_model', 'jina-v3')):
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)', (k, v))
    conn.commit(); conn.close()
    with server.app.test_request_context():
        with server.tenant_pin(OTHER_CO):
            assert server.get_setting('smtp_email') == '' and server.get_setting('smtp_app_password') == ''
            assert server.get_setting('seller_gstin') == ''
            assert server.get_setting('embedding_model') == 'jina-v3'     # platform default stays shared
            assert 'ZZSECRET' not in server._agent_cfg()['kb']
        with server.tenant_pin(CO):                                         # the owner keeps its values
            assert server.get_setting('smtp_email') == 'owner@gmail.com'
            assert server._agent_cfg()['kb'] == 'ZZSECRET platform kb'
    body = client(server, OTHER_ADMIN).get('/api/settings').get_data(as_text=True)
    assert 'owner@gmail.com' not in body and 'ZZSECRET' not in body
    assert server.app.test_client().get('/api/settings').status_code == 401
    shutil.copy(deep, server.DB_PATH)


def test_platform_ai_maintenance_is_owner_only(world):
    server, deep = world
    shutil.copy(deep, server.DB_PATH)
    cl = client(server, OTHER_ADMIN)
    for m, u in (('GET', '/api/ai/queue/status'), ('GET', '/api/ai/jd/status'), ('GET', '/api/ai/vectors/status'),
                 ('POST', '/api/ai/queue/enqueue-missing'), ('POST', '/api/ai/queue/retry-failed'),
                 ('POST', '/api/ai/vectors/rebuild'), ('POST', '/api/rkg/edge')):
        assert cl.open(u, method=m, json={}).status_code == 403, u
    assert cl.get('/api/ai/index-status').status_code == 200          # own-company status still works


def test_suspended_agency_loses_its_open_session(world):
    server, deep = world
    shutil.copy(deep, server.DB_PATH)
    cl = client(server, OTHER_ADMIN)
    assert cl.get('/api/mandates').status_code == 200
    conn = server.get_db()
    conn.execute("UPDATE companies SET status='suspended' WHERE id=?", (OTHER_CO,))
    conn.commit(); conn.close()
    assert cl.get('/api/mandates').status_code == 401
    with cl.session_transaction() as s:
        assert 'user_id' not in s
    conn = server.get_db()
    conn.execute("UPDATE companies SET status='active', billing_status='trial', trial_ends_at='2020-01-01T00:00:00' "
                 "WHERE id=?", (OTHER_CO,))
    conn.commit(); conn.close()
    cl = client(server, OTHER_ADMIN)
    assert cl.get('/api/mandates').status_code == 402
    assert cl.get('/api/auth/status').status_code == 200              # can still see why / pay
    shutil.copy(deep, server.DB_PATH)


def test_guard_answers_404_for_foreign_ids_but_not_own(world):
    server, deep = world
    shutil.copy(deep, server.DB_PATH)
    other = client(server, OTHER_ADMIN)
    for u in (f'/api/candidates/{SECRET}/email-history', f'/api/mandates/{SECRET}/email-templates',
              f'/api/candidates/{SECRET}', f'/api/invoices/{SECRET}'):
        assert other.get(u).status_code == 404, u
    assert other.get(f'/api/mandates/{FOREIGN}/email-templates').status_code == 200
    assert client(server, ADMIN).get(f'/api/mandates/{SECRET}/email-templates').status_code == 200


def test_meta_webhook_needs_signature_when_configured(world, monkeypatch):
    import hmac, hashlib
    server, deep = world
    monkeypatch.setenv('META_APP_SECRET', 'shh')
    pub = server.app.test_client()
    raw = json.dumps({'entry': []}).encode()
    assert pub.post('/api/wa/webhook', data=raw, content_type='application/json').status_code == 403
    sig = 'sha256=' + hmac.new(b'shh', raw, hashlib.sha256).hexdigest()
    assert pub.post('/api/wa/webhook', data=raw, content_type='application/json',
                    headers={'X-Hub-Signature-256': sig}).status_code == 200
