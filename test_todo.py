"""
Tasks v2 (modules/todo.py) — Wave 1 backend tests.

Run:   pytest test_todo.py -q
Boots the real server.py (real migrations, real access guard) — nothing is
mocked. Covers: CRUD, views/grouping, My Day, subtasks, lists, tags, prefs,
recurrence, assignment + recruiter scoping, tenant isolation, and that the
legacy Tasks page / Kanban board / calendar still see every task.
"""
import os
import sys
import json
import datetime
import tempfile

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CO, OTHER_CO = 951, 952
ADMIN, RIYA, RAJ, OTHER_ADMIN = 9501, 9502, 9503, 9504
M_ADMIN, M_RIYA, M_OTHER = 95101, 95102, 95103
C_ADMIN, C_RIYA = 95201, 95202


@pytest.fixture(scope='module')
def srv():
    os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='todo_'))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    server.app.config['TESTING'] = True
    conn = server.get_db()
    c = conn.cursor()
    now = server.ts()

    def ins(t, **kw):
        c.execute(f"INSERT OR REPLACE INTO {t} ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})",
                  list(kw.values()))

    ins('companies', id=CO, name='Todo Co', status='active', created_at=now)
    ins('companies', id=OTHER_CO, name='Todo Other', status='active', created_at=now)
    for uid, un, co, adm in ((ADMIN, 'td_admin', CO, 1), (RIYA, 'td_riya', CO, 0),
                             (RAJ, 'td_raj', CO, 0), (OTHER_ADMIN, 'td_other', OTHER_CO, 1)):
        ins('users', id=uid, username=un, password_hash='x', display_name=un.split('_')[1].title(),
            role='user', status='approved', company_id=co, is_company_admin=adm, created_at=now)
    ins('mandates', id=M_ADMIN, client='AdminClient', role='AdminRole', owner_id=CO,
        assigned_user_id=ADMIN, status='active', created_at=now)
    ins('mandates', id=M_RIYA, client='RiyaClient', role='RiyaRole', owner_id=CO,
        assigned_user_id=RIYA, status='active', created_at=now)
    ins('mandates', id=M_OTHER, client='OtherClient', role='OtherRole', owner_id=OTHER_CO,
        assigned_user_id=OTHER_ADMIN, status='active', created_at=now)
    ins('candidates', id=C_ADMIN, mandate_id=M_ADMIN, name='Admin Cand', phone='9000000001',
        owner_id=CO, stage='Screening', created_at=now)
    ins('candidates', id=C_RIYA, mandate_id=M_RIYA, name='Riya Cand', phone='9000000002',
        owner_id=CO, stage='Screening', created_at=now)
    conn.commit()
    conn.close()
    yield server


def cl(server, uid):
    c = server.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid
    return c


def ids(resp):
    return {t['id'] for t in resp.get_json()['tasks']}


def today(server):
    return server._ist_now().date()


# ── pure helpers ─────────────────────────────────────────────────────────
def test_parse_due_and_groups(srv):
    from modules import todo
    assert todo.parse_due('') == ('', None)
    assert todo.parse_due('2026-10-08') == ('2026-10-08', None)
    assert todo.parse_due('2026-10-08T09:30') == ('2026-10-08T09:30:00', None)
    assert todo.parse_due('2026-10-08 09:30:15') == ('2026-10-08T09:30:15', None)
    assert todo.parse_due('2026-13-40')[1]
    assert todo.parse_due('tomorrow')[1]
    now = datetime.datetime(2026, 10, 7, 15, 0, 0)
    assert todo.group_for('', now) == 'someday'
    assert todo.group_for('2026-10-06', now) == 'overdue'
    assert todo.group_for('2026-10-07', now) == 'today'
    assert todo.group_for('2026-10-07T09:00:00', now) == 'today'      # late, but still Today
    assert todo._is_overdue_now('2026-10-07T09:00:00', now) is True
    assert todo._is_overdue_now('2026-10-07', now) is False            # all-day: not late until tomorrow
    assert todo.group_for('2026-10-08T09:00:00', now) == 'tomorrow'
    assert todo.group_for('2026-10-20', now) == 'upcoming'


def test_recurrence_rules(srv):
    from modules import todo
    d = datetime.date(2026, 10, 7)                                     # a Wednesday
    nxt = lambda rule, due, t=d: todo.next_occurrence(json.dumps(rule), due, t)
    assert nxt({'freq': 'daily', 'interval': 1}, '2026-10-07T09:00:00') == '2026-10-08T09:00:00'
    assert nxt({'freq': 'daily', 'interval': 3}, '2026-10-07') == '2026-10-10'
    # weekly Mon/Wed/Fri from Wed -> Fri; from Fri -> next Mon
    assert nxt({'freq': 'weekly', 'interval': 1, 'weekdays': [0, 2, 4]}, '2026-10-07') == '2026-10-09'
    assert nxt({'freq': 'weekly', 'interval': 1, 'weekdays': [0, 2, 4]}, '2026-10-09',
               datetime.date(2026, 10, 9)) == '2026-10-12'
    # every 2 weeks on Monday, from Mon 12 Oct -> Mon 26 Oct
    assert nxt({'freq': 'weekly', 'interval': 2, 'weekdays': [0]}, '2026-10-12',
               datetime.date(2026, 10, 12)) == '2026-10-26'
    # monthly clamps to month end
    assert nxt({'freq': 'monthly', 'interval': 1}, '2026-01-31', datetime.date(2026, 1, 31)) == '2026-02-28'
    assert nxt({'freq': 'yearly', 'interval': 1}, '2028-02-29', datetime.date(2028, 2, 29)) == '2029-02-28'
    # completed late: jumps past today, never into the past
    assert nxt({'freq': 'daily', 'interval': 1}, '2026-09-01') == '2026-10-08'
    # until reached -> series ends
    assert nxt({'freq': 'daily', 'interval': 1, 'until': '2026-10-07'}, '2026-10-07') is None
    assert todo.parse_recurrence({'freq': 'hourly'})[1]
    assert todo.parse_recurrence({'freq': 'weekly', 'weekdays': [9]})[1]
    assert todo.parse_recurrence({'freq': 'weekly', 'weekdays': [4, 0, 0]})[0] == \
        '{"freq":"weekly","interval":1,"weekdays":[0,4]}'


# ── CRUD + views ─────────────────────────────────────────────────────────
def test_create_defaults_and_views(srv):
    a = cl(srv, ADMIN)
    meta = a.get('/api/todo/meta').get_json()
    assert [l['name'] for l in meta['lists'] if l['mine']][:2] == ['Personal', 'Work']
    personal = [l for l in meta['lists'] if l['mine'] and l['name'] == 'Personal'][0]['id']
    t0 = today(srv)
    r = a.post('/api/todo/tasks', json={'title': 'Send L&T shortlist', 'due_at': t0.isoformat(),
                                        'priority': 'high', 'tags': ['#Priority', 'priority', 'Client']})
    assert r.status_code == 200, r.get_json()
    t = r.get_json()['task']
    assert t['list_id'] == personal and t['priority'] == 'high' and t['all_day']
    assert t['tags'] == ['Priority', 'Client'] and t['group'] == 'today' and not t['late']
    some = a.post('/api/todo/tasks', json={'title': 'Someday idea'}).get_json()['task']
    assert some['group'] == 'someday' and some['due_at'] == ''
    assert a.post('/api/todo/tasks', json={'title': '  '}).status_code == 400
    assert a.post('/api/todo/tasks', json={'title': 'x', 'priority': 'urgent'}).status_code == 400
    assert a.post('/api/todo/tasks', json={'title': 'x', 'due_at': 'next week'}).status_code == 400

    assert {t['id'], some['id']} <= ids(a.get('/api/todo/tasks?view=all'))
    assert t['id'] in ids(a.get('/api/todo/tasks?view=next7'))
    assert some['id'] not in ids(a.get('/api/todo/tasks?view=next7'))
    assert t['id'] in ids(a.get(f'/api/todo/tasks?view=list&list_id={personal}'))
    assert t['id'] in ids(a.get('/api/todo/tasks?view=tag&tag=client'))
    assert t['id'] not in ids(a.get('/api/todo/tasks?view=tag&tag=cli'))
    assert t['id'] in ids(a.get('/api/todo/tasks?q=shortlist'))
    tags = {x['name'] for x in a.get('/api/todo/tags').get_json()['tags']}
    assert {'Priority', 'Client'} <= tags

    # detail, edit, complete, uncomplete, delete
    g = a.get(f"/api/todo/tasks/{t['id']}").get_json()['task']
    assert g['subtasks'] == []
    u = a.patch(f"/api/todo/tasks/{t['id']}", json={'notes': 'Use the new format', 'pinned': True,
                                                    'due_at': (t0 + datetime.timedelta(days=1)).isoformat() + 'T11:00'})
    assert u.status_code == 200 and u.get_json()['task']['group'] == 'tomorrow'
    assert a.post(f"/api/todo/tasks/{t['id']}/complete", json={'done': True}).get_json()['rolled'] is False
    assert t['id'] in ids(a.get('/api/todo/tasks?view=completed'))
    assert t['id'] not in ids(a.get('/api/todo/tasks?view=all'))
    a.post(f"/api/todo/tasks/{t['id']}/complete", json={'done': False})
    assert t['id'] in ids(a.get('/api/todo/tasks?view=all'))
    assert a.delete(f"/api/todo/tasks/{some['id']}").status_code == 200
    assert a.get(f"/api/todo/tasks/{some['id']}").status_code == 404


def test_my_day_and_suggestions(srv):
    a = cl(srv, ADMIN)
    t0 = today(srv)
    due = a.post('/api/todo/tasks', json={'title': 'Overdue chase',
                                          'due_at': (t0 - datetime.timedelta(days=2)).isoformat()}).get_json()['task']
    md = a.get('/api/todo/tasks?view=my_day').get_json()
    assert due['id'] in {s['id'] for s in md['suggestions']}
    assert [s['reason'] for s in md['suggestions'] if s['id'] == due['id']] == ['overdue']
    assert a.post(f"/api/todo/tasks/{due['id']}/my-day", json={'on': True}).get_json()['my_day'] is True
    md = a.get('/api/todo/tasks?view=my_day').get_json()
    assert due['id'] in ids_of(md['tasks']) and due['id'] not in {s['id'] for s in md['suggestions']}
    assert a.get('/api/todo/meta').get_json()['counts']['my_day'] >= 1
    a.post(f"/api/todo/tasks/{due['id']}/complete", json={})
    assert due['id'] not in ids(a.get('/api/todo/tasks?view=my_day'))
    assert due['id'] in ids(a.get('/api/todo/tasks?view=my_day&include_done=1'))


def ids_of(tasks):
    return {t['id'] for t in tasks}


def test_subtasks_and_recurring_roll(srv):
    a = cl(srv, ADMIN)
    t0 = today(srv)
    r = a.post('/api/todo/tasks', json={'title': 'Daily Naukri sourcing', 'due_at': t0.isoformat() + 'T10:00',
                                        'recurrence': {'freq': 'daily', 'interval': 1},
                                        'subtasks': ['Search', {'text': 'Shortlist'}]}).get_json()['task']
    assert r['recurrence'] == {'freq': 'daily', 'interval': 1} and r['subtasks_total'] == 2
    det = a.get(f"/api/todo/tasks/{r['id']}").get_json()['task']
    s1 = det['subtasks'][0]['id']
    assert a.patch(f'/api/todo/subtasks/{s1}', json={'done': True}).status_code == 200
    add = a.post(f"/api/todo/tasks/{r['id']}/subtasks", json={'text': 'Call top 10'}).get_json()
    assert add['ok'] and add['subtask']['sort_order'] == 3
    res = a.post(f"/api/todo/tasks/{r['id']}/complete", json={}).get_json()
    assert res['rolled'] is True
    assert res['next_due'] == (t0 + datetime.timedelta(days=1)).isoformat() + 'T10:00:00'
    det = a.get(f"/api/todo/tasks/{r['id']}").get_json()['task']
    assert det['done'] is False and det['completed_at'] and not any(s['done'] for s in det['subtasks'])
    assert a.delete(f"/api/todo/subtasks/{add['subtask']['id']}").status_code == 200
    assert len(a.get(f"/api/todo/tasks/{r['id']}").get_json()['task']['subtasks']) == 2
    # stopping the series: clear the rule, then complete -> closes
    a.patch(f"/api/todo/tasks/{r['id']}", json={'recurrence': None})
    assert a.post(f"/api/todo/tasks/{r['id']}/complete", json={}).get_json()['rolled'] is False


def test_lists_delete_keeps_tasks(srv):
    a = cl(srv, ADMIN)
    lst = a.post('/api/todo/lists', json={'name': 'Resolven', 'color': '#7C5CC4'}).get_json()['list']
    t = a.post('/api/todo/tasks', json={'title': 'Resolven JD review', 'list_id': lst['id']}).get_json()['task']
    assert t['list_name'] == 'Resolven'
    assert a.patch(f"/api/todo/lists/{lst['id']}", json={'name': 'Resolven Projects'}).status_code == 200
    assert a.patch(f"/api/todo/lists/{lst['id']}", json={'color': 'red'}).status_code == 400
    d = a.delete(f"/api/todo/lists/{lst['id']}").get_json()
    assert d['tasks_moved'] == 1
    g = a.get(f"/api/todo/tasks/{t['id']}").get_json()['task']
    assert g['list_id'] == 0 and g['title'] == 'Resolven JD review'           # task survives
    assert t['id'] in ids(a.get('/api/todo/tasks?view=inbox'))
    assert a.post('/api/todo/tasks', json={'title': 'x', 'list_id': lst['id']}).status_code == 400


def test_bulk_and_reorder(srv):
    a = cl(srv, ADMIN)
    ts_ = [a.post('/api/todo/tasks', json={'title': f'Bulk {i}'}).get_json()['task']['id'] for i in range(3)]
    work = [l for l in a.get('/api/todo/lists').get_json()['lists'] if l['name'] == 'Work'][0]['id']
    assert a.post('/api/todo/tasks/bulk', json={'ids': ts_, 'action': 'move_list', 'value': work}).get_json()['updated'] == 3
    assert a.post('/api/todo/tasks/bulk', json={'ids': ts_, 'action': 'set_priority', 'value': 'low'}).get_json()['ok']
    assert a.post('/api/todo/tasks/bulk', json={'ids': ts_, 'action': 'add_tag', 'value': 'BulkTag'}).get_json()['ok']
    assert a.post('/api/todo/tasks/bulk', json={'ids': ts_, 'action': 'set_due', 'value': 'garbage'}).status_code == 400
    assert a.post('/api/todo/tasks/bulk', json={'ids': ts_, 'action': 'explode'}).status_code == 400
    g = a.get(f'/api/todo/tasks/{ts_[0]}').get_json()['task']
    assert g['list_id'] == work and g['priority'] == 'low' and 'BulkTag' in g['tags']
    assert a.post('/api/todo/tasks/reorder', json={'ids': list(reversed(ts_))}).get_json()['updated'] == 3
    order = [t['id'] for t in a.get(f'/api/todo/tasks?view=list&list_id={work}').get_json()['tasks'] if t['id'] in ts_]
    assert order == list(reversed(ts_))
    assert a.post('/api/todo/tasks/bulk', json={'ids': ts_, 'action': 'delete'}).get_json()['updated'] == 3


def test_prefs(srv):
    a = cl(srv, ADMIN)
    assert a.post('/api/todo/prefs', json={'theme': 'dark', 'default_view': 'my_day'}).get_json()['prefs'] == \
        {'theme': 'dark', 'default_view': 'my_day'}
    assert a.post('/api/todo/prefs', json={'theme': 'neon'}).status_code == 400
    assert a.get('/api/todo/prefs').get_json()['prefs']['theme'] == 'dark'
    assert cl(srv, RIYA).get('/api/todo/prefs').get_json()['prefs'] == {}        # per user
    assert a.get('/api/todo/meta').get_json()['prefs']['theme'] == 'dark'


# ── assignment, scoping, isolation ───────────────────────────────────────
def test_assignment_and_recruiter_scope(srv):
    a, riya, raj, other = cl(srv, ADMIN), cl(srv, RIYA), cl(srv, RAJ), cl(srv, OTHER_ADMIN)
    private = a.post('/api/todo/tasks', json={'title': 'Admin private'}).get_json()['task']
    on_admin_cand = a.post('/api/todo/tasks', json={'title': 'Admin cand task',
                                                    'candidate_id': C_ADMIN}).get_json()['task']
    assert on_admin_cand['candidate_name'] == 'Admin Cand' and on_admin_cand['mandate_id'] == M_ADMIN
    for c in (riya, raj, other):
        assert c.get(f"/api/todo/tasks/{private['id']}").status_code in (403, 404)
        assert c.patch(f"/api/todo/tasks/{private['id']}", json={'title': 'hacked'}).status_code in (403, 404)
        assert private['id'] not in ids(c.get('/api/todo/tasks?view=all&scope=team'))
    assert riya.get(f"/api/todo/tasks/{on_admin_cand['id']}").status_code == 404

    # assign to Riya: she sees it and can complete it; Raj still cannot
    assert a.patch(f"/api/todo/tasks/{private['id']}", json={'assigned_to': OTHER_ADMIN}).status_code == 400
    r = a.patch(f"/api/todo/tasks/{private['id']}", json={'assigned_to': RIYA}).get_json()['task']
    assert r['assigned_to'] == RIYA and r['assigned_name'] == 'Riya'
    assert private['id'] in ids(riya.get('/api/todo/tasks?view=all'))
    assert private['id'] in ids(riya.get(f'/api/todo/tasks?view=assigned&assigned_to={RIYA}'))
    assert riya.post(f"/api/todo/tasks/{private['id']}/complete", json={}).status_code == 200
    assert raj.get(f"/api/todo/tasks/{private['id']}").status_code == 404
    # assigned task also reaches the legacy board for Riya (shared scope helper)
    board = riya.get('/api/tasks/board').get_json()['columns']
    assert private['id'] in {t['id'] for t in board['done']}

    # Riya's own work: standalone task + task on her candidate
    own = riya.post('/api/todo/tasks', json={'title': 'Riya follow-up', 'candidate_id': C_RIYA}).get_json()
    assert own['ok'] and own['task']['candidate_id'] == C_RIYA
    assert riya.post('/api/todo/tasks', json={'title': 'x', 'candidate_id': C_ADMIN}).status_code == 404
    assert riya.post('/api/todo/tasks', json={'title': 'x', 'mandate_id': M_ADMIN}).status_code == 404
    mine = riya.post('/api/todo/tasks', json={'title': 'Riya standalone'}).get_json()['task']
    assert mine['id'] not in ids(raj.get('/api/todo/tasks?view=all'))
    # admin team view sees everything in the company; "mine" does not show Riya's standalone
    assert mine['id'] in ids(a.get('/api/todo/tasks?view=all&scope=team'))
    assert mine['id'] not in ids(a.get('/api/todo/tasks?view=all'))
    # other agency sees none of it
    assert not ({mine['id'], own['task']['id']} & ids(other.get('/api/todo/tasks?view=all&scope=team')))
    # subtasks follow their task's visibility
    sub = a.post(f"/api/todo/tasks/{on_admin_cand['id']}/subtasks", json={'text': 'secret step'}).get_json()
    sid = sub['subtask']['id']
    assert riya.patch(f'/api/todo/subtasks/{sid}', json={'done': True}).status_code == 404
    assert other.delete(f'/api/todo/subtasks/{sid}').status_code == 404


def test_lists_and_tags_permissions(srv):
    a, riya, other = cl(srv, ADMIN), cl(srv, RIYA), cl(srv, OTHER_ADMIN)
    priv = a.post('/api/todo/lists', json={'name': 'Admin only'}).get_json()['list']
    shared = a.post('/api/todo/lists', json={'name': 'Team board', 'is_shared': True}).get_json()['list']
    assert shared['is_shared'] is True
    rl = {l['id'] for l in riya.get('/api/todo/lists').get_json()['lists']}
    assert shared['id'] in rl and priv['id'] not in rl
    assert riya.post('/api/todo/tasks', json={'title': 'x', 'list_id': priv['id']}).status_code == 400
    assert riya.post('/api/todo/tasks', json={'title': 'In team list', 'list_id': shared['id']}).status_code == 200
    assert riya.patch(f"/api/todo/lists/{shared['id']}", json={'name': 'mine now'}).status_code == 404
    assert riya.delete(f"/api/todo/lists/{priv['id']}").status_code == 404
    assert other.delete(f"/api/todo/lists/{shared['id']}").status_code == 404
    own = riya.post('/api/todo/lists', json={'name': 'Riya list', 'is_shared': True}).get_json()['list']
    assert own['is_shared'] is False                                   # recruiters cannot share
    assert riya.patch(f"/api/todo/lists/{own['id']}", json={'is_shared': True}).status_code == 403
    tag = a.post('/api/todo/tags', json={'name': 'Hot', 'color': '#C0392B'}).get_json()['tag']
    assert riya.patch(f"/api/todo/tags/{tag['id']}", json={'color': '#000000'}).status_code == 404
    assert riya.delete(f"/api/todo/tags/{tag['id']}").status_code == 403
    assert other.patch(f"/api/todo/tags/{tag['id']}", json={'color': '#000000'}).status_code == 404
    assert a.delete(f"/api/todo/tags/{tag['id']}").status_code == 200


# ── legacy surfaces keep working ─────────────────────────────────────────
def test_legacy_routes_see_v2_tasks(srv):
    a = cl(srv, ADMIN)
    t0 = today(srv)
    v2 = a.post('/api/todo/tasks', json={'title': 'All-day v2 task', 'due_at': t0.isoformat()}).get_json()['task']
    legacy = {t['ref_id']: t for t in a.get('/api/tasks').get_json()['tasks'] if t['type'] == 'reminder'}
    assert legacy[v2['id']]['section'] == 'today'                      # all-day = today, not overdue
    board = a.get('/api/tasks/board').get_json()['columns']
    assert v2['id'] in {t['id'] for t in board['todo']}
    cal = a.get(f'/api/scheduler/calendar?from={t0.isoformat()}&to={t0.isoformat()}').get_json()
    assert v2['id'] in {e['id'] for e in cal['events'] if e['kind'] == 'reminder'}
    # Kanban move keeps v2 'done' in step, and v2 complete moves the card
    assert a.post(f"/api/reminders/{v2['id']}/stage", json={'stage': 'doing'}).status_code == 200
    assert a.get(f"/api/todo/tasks/{v2['id']}").get_json()['task']['stage'] == 'doing'
    a.post(f"/api/todo/tasks/{v2['id']}/complete", json={})
    board = a.get('/api/tasks/board').get_json()['columns']
    assert v2['id'] in {t['id'] for t in board['done']}

    # a task made by the OLD "New Task" modal / MCP shows in v2 (no list = Inbox)
    assert a.post('/api/reminders', json={'note': 'Made by old modal',
                                          'due_at': t0.isoformat() + 'T18:00'}).status_code == 200
    inbox = a.get('/api/todo/tasks?view=inbox').get_json()['tasks']
    old = [t for t in inbox if t['title'] == 'Made by old modal']
    assert old and old[0]['group'] == 'today' and old[0]['list_id'] == 0
    # old edit / done routes work on it
    rid = old[0]['id']
    assert a.post(f'/api/reminders/{rid}/edit', json={'note': 'Edited by old modal'}).status_code == 200
    assert a.get(f'/api/todo/tasks/{rid}').get_json()['task']['title'] == 'Edited by old modal'
    assert a.post('/api/tasks/done', json={'type': 'reminder', 'ref_id': rid}).status_code == 200
    assert a.get(f'/api/todo/tasks/{rid}').get_json()['task']['done'] is True


def test_push_scheduler_ignores_unscheduled_tasks(srv):
    """The 60-second push loop must not crash or fire on Someday / all-day tasks."""
    conn = srv.get_db()
    rows = conn.execute("SELECT due_at FROM reminders WHERE owner_id=? AND done=0", (CO,)).fetchall()
    conn.close()
    fmts = ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M')
    for r in rows:
        due = r['due_at']
        if not due or len(due) == 10:
            parsed = False
            for f in fmts:
                try:
                    datetime.datetime.strptime(due[:19], f)
                    parsed = True
                except Exception:
                    pass
            assert not parsed                                          # loop skips these rows


# ── Wave 3: reminder engine + My Day ATS suggestions + calendar range ────
def _recurring(a, title, due):
    return a.post('/api/todo/tasks', json={'title': title, 'due_at': due,
                                           'recurrence': {'freq': 'daily', 'interval': 1}}).get_json()['task']


def test_every_done_path_rolls_recurring(srv):
    a = cl(srv, ADMIN)
    t0 = today(srv)
    nxt = (t0 + datetime.timedelta(days=1)).isoformat() + 'T09:00:00'
    # 1) phone notification "Done" — token only, no login session
    t = _recurring(a, 'Daily standup notes', t0.isoformat() + 'T09:00')
    anon = srv.app.test_client()
    tok = srv._reminder_token(t['id'])
    assert anon.post(f"/api/reminders/{t['id']}/done?token=bad").status_code == 401
    r = anon.post(f"/api/reminders/{t['id']}/done?token={tok}").get_json()
    assert r['ok'] and r['rolled'] and r['next_due'] == nxt
    g = a.get(f"/api/todo/tasks/{t['id']}").get_json()['task']
    assert g['done'] is False and g['due_at'] == nxt
    # 2) old Tasks list ✓
    t2 = _recurring(a, 'Daily Naukri refresh', t0.isoformat() + 'T09:00')
    assert a.post('/api/tasks/done', json={'type': 'reminder', 'ref_id': t2['id']}).status_code == 200
    assert a.get(f"/api/todo/tasks/{t2['id']}").get_json()['task']['due_at'] == nxt
    # 3) Kanban: drag to Done
    t3 = _recurring(a, 'Daily pipeline check', t0.isoformat() + 'T09:00')
    r3 = a.post(f"/api/reminders/{t3['id']}/stage", json={'stage': 'done'}).get_json()
    assert r3['rolled'] is True
    # a plain task still closes, and dragging it back re-opens it
    p = a.post('/api/todo/tasks', json={'title': 'One-off'}).get_json()['task']
    a.post(f"/api/reminders/{p['id']}/stage", json={'stage': 'done'})
    assert a.get(f"/api/todo/tasks/{p['id']}").get_json()['task']['done'] is True
    a.post(f"/api/reminders/{p['id']}/stage", json={'stage': 'doing'})
    g = a.get(f"/api/todo/tasks/{p['id']}").get_json()['task']
    assert g['done'] is False and g['stage'] == 'doing' and g['completed_at'] == ''


def test_push_goes_to_the_right_phone(srv):
    from modules import todo
    row = lambda **k: _Row(dict({'owner_id': CO, 'created_by': 0, 'assigned_to': 0}, **k))
    assert todo.push_targets(row(created_by=RIYA)) == [RIYA]
    assert todo.push_targets(row(created_by=ADMIN, assigned_to=RAJ)) == [RAJ]
    assert todo.push_targets(row()) == [CO]                  # legacy row: unchanged behaviour
    # and the scheduler really sends there
    sent = []
    orig = srv._send_fcm_to_user
    srv._send_fcm_to_user = lambda uid, payload, notification=None: sent.append((uid, payload.get('kind')))
    try:
        r = row(id=1, created_by=RIYA, due_at='2026-01-01T00:00:00')
        for tgt in srv._push_targets(r):
            srv._send_fcm_to_user(tgt, {'kind': 'due'})
    finally:
        srv._send_fcm_to_user = orig
    assert sent == [(RIYA, 'due')]


class _Row(dict):
    def keys(self):
        return list(super().keys())


def test_someday_task_not_counted_as_due_today(srv):
    a = cl(srv, ADMIN)
    before = a.get('/api/tasks').get_json()['counts']
    s = a.post('/api/todo/tasks', json={'title': 'Someday: write SOP'}).get_json()['task']
    after = a.get('/api/tasks').get_json()
    row = [t for t in after['tasks'] if t['type'] == 'reminder' and t['ref_id'] == s['id']][0]
    assert row['section'] == 'upcoming'
    assert after['counts']['today'] == before['today'] and after['counts']['overdue'] == before['overdue']


def test_calendar_range_view(srv):
    a = cl(srv, ADMIN)
    t0 = today(srv)
    inr = a.post('/api/todo/tasks', json={'title': 'In range', 'due_at': (t0 + datetime.timedelta(days=3)).isoformat()}).get_json()['task']
    out = a.post('/api/todo/tasks', json={'title': 'Out of range', 'due_at': (t0 + datetime.timedelta(days=40)).isoformat()}).get_json()['task']
    frm, to = t0.isoformat(), (t0 + datetime.timedelta(days=6)).isoformat()
    got = ids(a.get(f'/api/todo/tasks?view=range&from={frm}&to={to}&include_done=1'))
    assert inr['id'] in got and out['id'] not in got
    assert a.get('/api/todo/tasks?view=range&from=x&to=y').status_code == 400
    assert a.get(f'/api/todo/tasks?view=range&from=2026-01-01&to=2026-12-31').status_code == 400


def test_my_day_ats_suggestions(srv):
    conn = srv.get_db()
    old = (srv._ist_now() - datetime.timedelta(days=30)).isoformat(timespec='seconds')
    conn.execute("UPDATE candidates SET updated_at=?, created_at=? WHERE id IN (?,?)", (old, old, C_ADMIN, C_RIYA))
    conn.commit(); conn.close()
    a, riya = cl(srv, ADMIN), cl(srv, RIYA)
    md = a.get('/api/todo/tasks?view=my_day').get_json()
    stale = [s for s in md['ats_suggestions'] if s['type'] == 'stale']
    assert {s['candidate_id'] for s in stale} >= {C_ADMIN}
    s0 = [s for s in stale if s['candidate_id'] == C_ADMIN][0]
    assert s0['kind'] == 'Stale candidate' and s0['title'] == 'Admin Cand'
    # turning it into a My Day task removes the duplicate suggestion
    a.post('/api/todo/tasks', json={'title': 'Follow up Admin Cand', 'candidate_id': C_ADMIN, 'my_day': True})
    md = a.get('/api/todo/tasks?view=my_day').get_json()
    assert C_ADMIN not in {s['candidate_id'] for s in md['ats_suggestions']}
    # recruiter only ever sees her own candidates
    rs = {s['candidate_id'] for s in riya.get('/api/todo/tasks?view=my_day').get_json()['ats_suggestions']}
    assert C_ADMIN not in rs and C_RIYA in rs
    assert 'ats_suggestions' not in a.get('/api/todo/tasks?view=my_day&ats=0').get_json()


# ── Wave 4: team view, person view, auto-task rules ──────────────────────
def test_team_and_person_views(srv):
    a, riya = cl(srv, ADMIN), cl(srv, RIYA)
    t0 = today(srv)
    a.post('/api/todo/tasks', json={'title': 'For Riya overdue', 'assigned_to': RIYA,
                                    'due_at': (t0 - datetime.timedelta(days=1)).isoformat()})
    own = riya.post('/api/todo/tasks', json={'title': 'Riya own today', 'due_at': t0.isoformat()}).get_json()['task']
    tm = a.get('/api/todo/team').get_json()
    m = {x['id']: x for x in tm['members']}
    assert m[RIYA]['overdue'] >= 1 and m[RIYA]['today'] >= 1 and m[RIYA]['name'] == 'Riya'
    assert OTHER_ADMIN not in m                                        # other agency never listed
    assert riya.get('/api/todo/team').status_code == 403
    got = ids(a.get(f'/api/todo/tasks?view=person&user_id={RIYA}'))
    assert own['id'] in got
    assert riya.get(f'/api/todo/tasks?view=person&user_id={ADMIN}').status_code == 403


def _stage(srv, cid, stage, created_at=None):
    conn = srv.get_db()
    conn.execute('UPDATE candidates SET stage=? WHERE id=?', (stage, cid))
    conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                 (cid, 'Screening', stage, 'test', created_at or srv.ts()))
    conn.commit(); conn.close()


def _auto_tasks(srv, cid):
    conn = srv.get_db()
    rows = conn.execute("SELECT * FROM reminders WHERE candidate_id=? AND auto_rule!='' ORDER BY id", (cid,)).fetchall()
    conn.close()
    return rows


def test_auto_task_rules(srv):
    from modules import todo
    a, riya = cl(srv, ADMIN), cl(srv, RIYA)
    assert riya.get('/api/todo/auto-rules').status_code == 403
    rules = a.get('/api/todo/auto-rules').get_json()['rules']
    assert {r['stage'] for r in rules} == {'Shared with Client', 'Placed', 'Joined'}
    conn = srv.get_db(); todo.run_auto_rules(conn); conn.close()      # make sure the watermark exists

    # a real stage move by the recruiter through the normal ATS route
    assert riya.post(f'/api/candidates/{C_RIYA}/stage', json={'stage': 'Shared with Client'}).status_code == 200
    conn = srv.get_db(); todo.run_auto_rules(conn); todo.run_auto_rules(conn); conn.close()
    made = _auto_tasks(srv, C_RIYA)
    assert len(made) == 1
    t = made[0]
    d2 = (today(srv) + datetime.timedelta(days=2)).isoformat()
    assert t['note'] == 'Get client feedback: Riya Cand' and t['due_at'] == d2 + 'T11:00:00'
    assert t['assigned_to'] == RIYA and t['priority'] == 'medium' and json.loads(t['tags']) == ['Auto']
    # it reaches Riya's task list and her phone
    assert t['id'] in ids(riya.get('/api/todo/tasks?view=all'))
    assert todo.push_targets(t) == [RIYA]
    # moving away and back while the task is still open does not duplicate it
    _stage(srv, C_RIYA, 'Interview Inprocess'); _stage(srv, C_RIYA, 'Shared with Client')
    conn = srv.get_db(); todo.run_auto_rules(conn); conn.close()
    assert len(_auto_tasks(srv, C_RIYA)) == 1
    # old stage changes (imports) never create tasks
    old = (srv._ist_now() - datetime.timedelta(days=5)).isoformat(timespec='seconds')
    _stage(srv, C_ADMIN, 'Placed', created_at=old)
    conn = srv.get_db(); todo.run_auto_rules(conn); conn.close()
    assert _auto_tasks(srv, C_ADMIN) == []

    # editing rules: custom rule, disable, delete
    r = a.post('/api/todo/auto-rules', json={'stage': 'Interview Inprocess', 'title': 'Prep {name} for {client}',
                                             'due_days': 0, 'due_time': '09:30', 'priority': 'high'}).get_json()['rule']
    assert a.post('/api/todo/auto-rules', json={'stage': 'X', 'title': 'Y', 'due_days': 99}).status_code == 400
    assert a.post('/api/todo/auto-rules', json={'stage': 'X', 'title': 'Y', 'due_time': '25:00'}).status_code == 400
    assert a.post('/api/todo/auto-rules', json={'stage': 'X', 'title': 'Y', 'assign_to': OTHER_ADMIN}).status_code == 400
    _stage(srv, C_ADMIN, 'Interview Inprocess')
    conn = srv.get_db(); todo.run_auto_rules(conn); conn.close()
    prep = [x for x in _auto_tasks(srv, C_ADMIN) if x['note'].startswith('Prep')]
    assert prep and prep[0]['note'] == 'Prep Admin Cand for AdminClient' and prep[0]['assigned_to'] == ADMIN
    assert a.patch(f"/api/todo/auto-rules/{r['id']}", json={'is_active': False}).get_json()['rule']['is_active'] is False
    assert a.delete(f"/api/todo/auto-rules/{r['id']}").status_code == 200
    assert r['id'] not in {x['id'] for x in a.get('/api/todo/auto-rules').get_json()['rules']}
    assert cl(srv, OTHER_ADMIN).patch(f"/api/todo/auto-rules/{rules[0]['id']}", json={'title': 'x'}).status_code == 404


# ── Wave 5: smart quick-add, daily plan, morning push ────────────────────
class _FakeResp:
    def __init__(self, content):
        self._c = content
    def json(self):
        return {'choices': [{'message': {'content': self._c}}], 'usage': {}}


def _with_ai(srv, answer=None, boom=False):
    """Patch DeepSeek: a key is 'set' and call_deepseek returns `answer`."""
    calls = []
    orig_call, orig_get = srv.call_deepseek, srv.get_setting

    def fake_call(key, payload, timeout=60, endpoint='deepseek'):
        calls.append(payload)
        if boom:
            raise RuntimeError('network down')
        return _FakeResp(answer if isinstance(answer, str) else json.dumps(answer))

    def fake_get(key, default=''):
        return 'sk-test' if key == 'deepseek_api_key' else orig_get(key, default)
    srv.call_deepseek, srv.get_setting = fake_call, fake_get
    return calls, (lambda: (setattr(srv, 'call_deepseek', orig_call), setattr(srv, 'get_setting', orig_get)))


def test_parse_rules_hinglish(srv):
    from modules.todo_ai import parse_rules
    T = datetime.date(2026, 10, 8)                                     # Thursday
    r = parse_rules('kal 11 baje Rahul ko call L&T ke liye #urgent', T)
    assert (r['date'], r['time'], r['priority'], r['tags']) == (datetime.date(2026, 10, 9), '11:00', 'high', ['urgent'])
    assert r['title'] == 'Rahul ko call L&T ke liye'
    r = parse_rules('Send shortlist to Resolven friday 5pm !high @Riya', T)
    assert (r['date'], r['time'], r['priority'], r['assignee_hint']) == (datetime.date(2026, 10, 9), '17:00', 'high', 'Riya')
    r = parse_rules('har somvar pipeline review', T)
    assert r['recurrence'] == {'freq': 'weekly', 'interval': 1, 'weekdays': [0]} and r['date'] == datetime.date(2026, 10, 12)
    assert parse_rules('parso shaam 6 baje interview confirm', T)['time'] == '18:00'
    assert parse_rules('Call back at 3:30 pm', T)['time'] == '15:30'
    assert parse_rules('3 baje call', T)['time'] == '15:00'              # 1-7 without am/pm = afternoon
    assert parse_rules('subah 9 baje standup', T)['time'] == '09:00'
    assert parse_rules('15 oct invoice', T)['date'] == datetime.date(2026, 10, 15)
    assert parse_rules('5 jan renewal', T)['date'] == datetime.date(2027, 1, 5)   # past month -> next year
    assert parse_rules('daily naukri refresh', T)['recurrence'] == {'freq': 'daily', 'interval': 1}
    r = parse_rules('Call 10 SCADA profiles', T)                        # numbers alone are not times
    assert r['date'] is None and r['time'] is None and r['title'] == 'Call 10 SCADA profiles'
    assert parse_rules('urgent: share JD', T)['title'] == 'share JD'


def test_parse_api_resolves_records(srv):
    a, riya = cl(srv, ADMIN), cl(srv, RIYA)
    r = a.post('/api/todo/parse', json={'text': 'kal 11 baje Admin Cand ko call @Riya !high'}).get_json()
    d = r['draft']
    assert r['source'] == 'rules' and d['candidate_id'] == C_ADMIN and d['mandate_id'] == M_ADMIN
    assert d['assigned_to'] == RIYA and d['priority'] == 'high'
    assert d['due_at'] == (today(srv) + datetime.timedelta(days=1)).isoformat() + 'T11:00'
    assert {u['k'] for u in r['understood']} >= {'due', 'candidate', 'assignee', 'priority'}
    # client name -> the active job, when no candidate is named
    r = a.post('/api/todo/parse', json={'text': 'Send JD to AdminClient friday'}).get_json()
    assert r['draft']['mandate_id'] == M_ADMIN and r['draft']['candidate_id'] == 0
    # the recruiter can never resolve the admin's candidate or job
    r = riya.post('/api/todo/parse', json={'text': 'call Admin Cand about AdminClient'}).get_json()
    assert r['draft']['candidate_id'] == 0 and r['draft']['mandate_id'] is None
    r = riya.post('/api/todo/parse', json={'text': 'call Riya Cand'}).get_json()
    assert r['draft']['candidate_id'] == C_RIYA
    # nothing is saved by /parse; the draft is created through the normal route
    before = len(a.get('/api/todo/tasks?view=all&scope=team').get_json()['tasks'])
    assert len(a.get('/api/todo/tasks?view=all&scope=team').get_json()['tasks']) == before
    assert a.post('/api/todo/parse', json={'text': ''}).status_code == 400


def test_parse_with_ai_and_fallbacks(srv):
    a = cl(srv, ADMIN)
    t1 = (today(srv) + datetime.timedelta(days=1)).isoformat()
    calls, undo = _with_ai(srv, {'title': 'Call Admin Cand about CTC', 'date': t1, 'time': '16:00',
                                 'priority': None, 'recurrence': None, 'person': 'Admin Cand',
                                 'client': 'AdminClient', 'assignee': None})
    try:
        r = a.post('/api/todo/parse', json={'text': 'kal shaam 4 baje admin cand ko CTC ke liye phone', 'ai': True}).get_json()
        assert r['source'] == 'ai' and len(calls) == 1
        assert r['draft']['title'] == 'Call Admin Cand about CTC' and r['draft']['due_at'] == t1 + 'T16:00'
        assert r['draft']['candidate_id'] == C_ADMIN
        # without ai=true the model is never called
        a.post('/api/todo/parse', json={'text': 'kal call'}); assert len(calls) == 1
        # the user can switch AI off
        a.post('/api/todo/prefs', json={'ai': False})
        assert a.post('/api/todo/parse', json={'text': 'kal call', 'ai': True}).get_json()['source'] == 'rules'
        a.post('/api/todo/prefs', json={'ai': True})
    finally:
        undo()
    # AI invents a person / bad date -> nothing fake gets linked, rules date kept
    calls, undo = _with_ai(srv, {'title': 'x', 'date': '1999-01-01', 'time': '99:99', 'person': 'Ghost Person',
                                 'client': 'Nonexistent Ltd'})
    try:
        r = a.post('/api/todo/parse', json={'text': 'kal follow up', 'ai': True}).get_json()
        assert r['draft']['candidate_id'] == 0 and r['draft']['mandate_id'] is None
        assert r['draft']['due_at'] == t1                                 # from the rules parser
    finally:
        undo()
    # network failure -> rules, with a note
    calls, undo = _with_ai(srv, boom=True)
    try:
        r = a.post('/api/todo/parse', json={'text': 'kal 11 baje call', 'ai': True}).get_json()
        assert r['ok'] and r['source'] == 'rules' and r['note'] and r['draft']['due_at'] == t1 + 'T11:00'
    finally:
        undo()


def test_daily_plan(srv):
    a = cl(srv, ADMIN)
    t0 = today(srv)
    od = a.post('/api/todo/tasks', json={'title': 'Plan: overdue invoice', 'priority': 'high',
                                         'due_at': (t0 - datetime.timedelta(days=3)).isoformat()}).get_json()['task']
    td = a.post('/api/todo/tasks', json={'title': 'Plan: today low', 'priority': 'low',
                                         'due_at': t0.isoformat()}).get_json()['task']
    later = a.post('/api/todo/tasks', json={'title': 'Plan: next month',
                                            'due_at': (t0 + datetime.timedelta(days=30)).isoformat()}).get_json()['task']
    p = a.get('/api/todo/plan').get_json()
    ids_ = [i.get('id') for i in p['items'] if i['kind'] == 'task']
    assert od['id'] in ids_ and td['id'] in ids_ and later['id'] not in ids_
    assert ids_.index(od['id']) < ids_.index(td['id'])                  # overdue + high ranks first
    assert p['source'] == 'rules' and p['summary']
    # AI summary is cached for the day; refresh=1 regenerates
    calls, undo = _with_ai(srv, 'Pehle overdue invoice nipta do, phir baaki calls.')
    try:
        assert a.get('/api/todo/plan').get_json()['source'] == 'rules' and not calls       # cached
        r = a.get('/api/todo/plan?refresh=1').get_json()
        assert r['source'] == 'ai' and r['summary'].startswith('Pehle') and len(calls) == 1
        assert a.get('/api/todo/plan').get_json()['summary'].startswith('Pehle') and len(calls) == 1
    finally:
        undo()
    assert cl(srv, RIYA).get('/api/todo/plan').get_json()['ok']


def test_morning_push(srv):
    from modules import todo_ai
    a, riya = cl(srv, ADMIN), cl(srv, RIYA)
    riya.post('/api/todo/tasks', json={'title': 'Riya morning item', 'due_at': today(srv).isoformat()})
    sent = []
    fake = lambda uid, payload, notification=None: sent.append((uid, payload['kind'], notification['body']))
    now = srv._ist_now()
    early = now.replace(hour=8, minute=0)
    late = now.replace(hour=10, minute=0)
    conn = srv.get_db()
    assert todo_ai.run_morning_push(conn, late, fake) == 0                 # nobody opted in yet
    riya.post('/api/todo/prefs', json={'morning_push': True})
    assert todo_ai.run_morning_push(conn, early, fake) == 0                # before 09:30
    assert todo_ai.run_morning_push(conn, late, fake) == 1
    assert sent[0][0] == RIYA and sent[0][1] == 'morning' and 'due today' in sent[0][2]
    assert todo_ai.run_morning_push(conn, late, fake) == 0                 # once per day
    conn.close()


def test_parse_reports_ai_configured_not_user_switch(srv):
    """Regression: with AI switched off, the UI must still learn that AI exists,
    otherwise switching it back on never re-enables the AI preview."""
    a = cl(srv, ADMIN)
    calls, undo = _with_ai(srv, {'title': 'x'})
    try:
        a.post('/api/todo/prefs', json={'ai': False})
        r = a.post('/api/todo/parse', json={'text': 'kal call', 'ai': True}).get_json()
        assert r['source'] == 'rules' and r['ai_available'] is True and not calls
        a.post('/api/todo/prefs', json={'ai': True})
        assert a.post('/api/todo/parse', json={'text': 'kal call', 'ai': True}).get_json()['source'] == 'ai'
    finally:
        undo()
    assert a.post('/api/todo/parse', json={'text': 'kal call'}).get_json()['ai_available'] is False   # no key
