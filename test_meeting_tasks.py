"""
Scheduler meetings -> Tasks, and task alerts — modules/meeting_tasks.py

Run:   pytest test_meeting_tasks.py -q
Uses the Meet-invite fixtures (real scheduler routes, e-mail captured).
"""
import datetime

from test_google_meet import srv, mail, cl, slots_for, HOST, RIYA, LINK  # noqa: F401  (fixtures)


def task_for(srv, meeting_id):
    conn = srv.get_db()
    r = conn.execute('SELECT * FROM reminders WHERE meeting_id=?', (meeting_id,)).fetchall()
    conn.close()
    return [dict(x) for x in r]


def test_task_fields(srv):
    from modules.meeting_tasks import task_fields, due_epoch_ms
    t, notes, due, link = task_fields({'guest_name': 'Asha', 'mode': 'Video', 'purpose': 'Solar O&M role',
                                       'guest_phone': '9811122334', 'meet_link': LINK, 'start_at': '2026-10-20T11:00'})
    assert t == 'Meeting: Asha — Solar O&M role (Video)' and due == '2026-10-20T11:00:00' and link == LINK
    assert '9811122334' in notes and 'Join: ' + LINK in notes
    want = int((datetime.datetime(2026, 10, 20, 5, 30) - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)
    assert due_epoch_ms('2026-10-20T11:00:00') == want      # 11:00 IST = 05:30 UTC
    assert due_epoch_ms('2026-10-20') is None


def test_booking_reschedule_cancel_drive_the_task(srv, mail):
    c = cl(srv, HOST)
    token, slots = slots_for(c)
    pub = srv.app.test_client()
    j = pub.post('/api/scheduler/public/%s/book' % token, json={'start_at': slots[0], 'guest_name': 'Asha Verma',
                 'guest_email': 'asha@example.com', 'guest_phone': '9811122334', 'mode': 'Video', 'purpose': 'Intro call'}).get_json()
    t = task_for(srv, j['meeting_id'])
    assert len(t) == 1 and t[0]['due_at'] == slots[0][:16] + ':00' and t[0]['join_url'] == LINK
    assert t[0]['assigned_to'] == HOST and t[0]['created_by'] == HOST and 'Asha Verma' in t[0]['note']
    # the host sees it on the Tasks page with the join link
    tasks = c.get('/api/todo/tasks?view=all').get_json()
    mine = [x for x in tasks.get('tasks', []) if x.get('meeting_id') == j['meeting_id']]
    assert mine and mine[0]['join_url'] == LINK
    # reschedule moves the same task
    pub.post('/api/scheduler/public/manage/%s/reschedule' % j['manage_token'], json={'start_at': slots[3]})
    t2 = task_for(srv, j['meeting_id'])
    assert len(t2) == 1 and t2[0]['id'] == t[0]['id'] and t2[0]['due_at'] == slots[3][:16] + ':00'
    # cancel removes it
    pub.post('/api/scheduler/public/manage/%s/cancel' % j['manage_token'])
    assert task_for(srv, j['meeting_id']) == []


def test_manual_phone_meeting_and_delete(srv, mail):
    c = cl(srv, HOST)
    _, slots = slots_for(c)
    j = c.post('/api/scheduler/meetings', json={'start_at': slots[6], 'guest_name': 'Client X', 'mode': 'Phone',
                                                'guest_phone': '9898989898'}).get_json()
    t = task_for(srv, j['meeting_id'])
    assert len(t) == 1 and t[0]['join_url'] == '' and '9898989898' in t[0]['notes']
    c.delete('/api/scheduler/meetings/%d' % j['meeting_id'])
    assert task_for(srv, j['meeting_id']) == []


def test_upcoming_and_claim_once(srv):
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    due = (now + datetime.timedelta(minutes=12)).strftime('%Y-%m-%dT%H:%M:00')
    far = (now + datetime.timedelta(hours=5)).strftime('%Y-%m-%dT%H:%M:00')
    c = cl(srv, HOST)
    a = c.post('/api/todo/tasks', json={'title': 'Call Ravi back', 'due_at': due}).get_json()['task']['id']
    b = c.post('/api/todo/tasks', json={'title': 'Later', 'due_at': far}).get_json()['task']['id']
    d = c.post('/api/todo/tasks', json={'title': 'Date only', 'due_at': due[:10]}).get_json()['task']['id']
    up = c.get('/api/task-alerts/upcoming?within=30').get_json()
    ids = [x['id'] for x in up['tasks']]
    assert a in ids and b not in ids and d not in ids
    item = [x for x in up['tasks'] if x['id'] == a][0]
    assert item['due_ms'] and not item['alerted'] and abs(item['due_ms'] - up['now_ms'] - 12 * 60000) < 90000
    assert c.post('/api/task-alerts/%d/claim' % a, json={'due_at': due}).get_json()['ok'] is True
    assert c.post('/api/task-alerts/%d/claim' % a, json={'due_at': due}).get_json()['ok'] is False
    assert [x for x in c.get('/api/task-alerts/upcoming').get_json()['tasks'] if x['id'] == a][0]['alerted']
    # someone else cannot claim / see my task
    assert cl(srv, RIYA).post('/api/task-alerts/%d/claim' % a, json={'due_at': due}).get_json().get('ok') is not True
    assert a not in [x['id'] for x in cl(srv, RIYA).get('/api/task-alerts/upcoming').get_json()['tasks']]
    # moving the task re-arms the alert
    due2 = (now + datetime.timedelta(minutes=20)).strftime('%Y-%m-%dT%H:%M:00')
    c.patch('/api/todo/tasks/%d' % a, json={'due_at': due2})
    assert not [x for x in c.get('/api/task-alerts/upcoming').get_json()['tasks'] if x['id'] == a][0]['alerted']
    assert srv.app.test_client().get('/api/task-alerts/upcoming').status_code in (401, 302)


def test_backfill_copies_upcoming_meetings(srv):
    from modules.meeting_tasks import _migrate_meeting_tasks
    conn = srv.get_db()
    when = (datetime.datetime.utcnow() + datetime.timedelta(days=2)).strftime('%Y-%m-%dT10:00')
    cur = conn.execute("INSERT INTO meetings (company_id, host_user_id, guest_name, mode, start_at, end_at, status, created_at) "
                       "VALUES (991, ?, 'Old Booking', 'Video', ?, ?, 'confirmed', ?)", (HOST, when, when, srv.ts()))
    mid = cur.lastrowid
    conn.commit()
    _migrate_meeting_tasks(conn)
    _migrate_meeting_tasks(conn)                    # idempotent
    conn.close()
    assert len(task_for(srv, mid)) == 1


def test_recruiter_can_claim_own_task(srv):
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    due = (now + datetime.timedelta(minutes=8)).strftime('%Y-%m-%dT%H:%M:00')
    r = cl(srv, RIYA)
    tid = r.post('/api/todo/tasks', json={'title': 'Riya call', 'due_at': due}).get_json()['task']['id']
    assert tid in [x['id'] for x in r.get('/api/task-alerts/upcoming').get_json()['tasks']]
    assert r.post('/api/task-alerts/%d/claim' % tid, json={'due_at': due}).get_json()['ok'] is True
