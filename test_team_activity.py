"""
Team Activity — modules/team_activity.py

Run:   pytest test_team_activity.py -q
"""
import datetime

from test_naukri_screen import srv, cl, CO, ADMIN, RIYA, M_A, M_R  # noqa: F401  (fixtures)


def _ta():
    from modules import team_activity
    return team_activity


def test_segments_and_buckets(srv):
    ta = _ta()
    # 10:00-10:04 active, 10:08 (gap 4 < 10, same block), then 11:00-11:01 after a break
    assert ta.segments([600, 601, 602, 603, 604, 608, 660, 661]) == [[600, 609], [660, 662]]
    assert ta.bucket('Shared with Client') == 'client'
    assert ta.bucket('Interview Scheduled') == 'interview'
    assert ta.bucket('Offer Released') == 'offer'
    assert ta.bucket('Joined') == 'placed'
    assert ta.bucket('Not Interested') == 'dropped'


def test_ping_records_one_row_per_minute(srv):
    c = cl(srv, RIYA)
    assert c.post('/api/presence/ping').get_json()['ok']
    assert c.post('/api/presence/ping').get_json()['ok']          # same minute -> still one row
    conn = srv.get_db()
    n = conn.execute('SELECT COUNT(*) FROM team_presence WHERE user_id=?', (RIYA,)).fetchone()[0]
    co = conn.execute('SELECT company_id FROM team_presence WHERE user_id=?', (RIYA,)).fetchone()[0]
    conn.close()
    assert n == 1 and co == CO
    assert srv.app.test_client().post('/api/presence/ping').status_code == 401


def test_stage_move_is_stamped_with_the_user(srv):
    conn = srv.get_db()
    conn.execute("INSERT OR REPLACE INTO candidates (id, mandate_id, name, stage, owner_id, phone, created_at, updated_at) "
                 "VALUES (98801, ?, 'Tara Singh', 'Screening', ?, '9000000001', '2026-01-01T10:00:00', '2026-01-01T10:00:00')",
                 (M_R, CO))
    conn.commit(); conn.close()
    r = cl(srv, RIYA).post('/api/candidates/98801/stage', json={'stage': 'Shared with Client'})
    assert r.status_code == 200, r.get_data(as_text=True)
    conn = srv.get_db()
    row = conn.execute('SELECT actor_id, from_stage, to_stage FROM stage_history WHERE candidate_id=98801 '
                       'ORDER BY id DESC LIMIT 1').fetchone()
    conn.close()
    assert row['actor_id'] == RIYA and row['to_stage'] == 'Shared with Client'


def test_rows_written_outside_a_request_stay_system(srv):
    conn = srv.get_db()
    conn.execute("INSERT INTO stage_history (candidate_id, from_stage, to_stage, note, created_at) "
                 "VALUES (98801, 'A', 'B', 'cron', '2026-01-01T10:00:00')")
    conn.commit()
    row = conn.execute('SELECT actor_id FROM stage_history ORDER BY id DESC LIMIT 1').fetchone()
    conn.close()
    assert row['actor_id'] == 0


def test_actions_are_recorded(srv):
    c = cl(srv, RIYA)
    assert c.post('/api/candidates/98801/note', json={'text': 'called, interested'}).status_code == 200
    r = c.post('/api/candidates/98801/wa-send-log', json={'text': 'Hi Tara'})
    assert r.status_code == 200
    conn = srv.get_db()
    kinds = [x[0] for x in conn.execute('SELECT kind FROM team_events WHERE user_id=? AND candidate_id=98801 ORDER BY id', (RIYA,))]
    conn.close()
    assert kinds == ['note', 'whatsapp']
    # a failed request records nothing
    assert c.post('/api/candidates/99999999/note', json={'text': 'x'}).status_code >= 400
    conn = srv.get_db()
    assert conn.execute('SELECT COUNT(*) FROM team_events WHERE candidate_id=99999999').fetchone()[0] == 0
    conn.close()


def test_overview_is_admin_only_and_adds_up(srv):
    assert cl(srv, RIYA).get('/api/team/overview').status_code == 403
    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)).strftime('%Y-%m-%d')
    conn = srv.get_db()
    for m in range(600, 660):                                    # an hour in the morning
        conn.execute('INSERT OR IGNORE INTO team_presence VALUES (?,?,?,?)', (CO, RIYA, today, m))
    for m in range(900, 930):                                    # half an hour after a break
        conn.execute('INSERT OR IGNORE INTO team_presence VALUES (?,?,?,?)', (CO, RIYA, today, m))
    conn.commit(); conn.close()
    j = cl(srv, ADMIN).get('/api/team/overview?from=%s&to=%s' % (today, today)).get_json()
    assert j['ok']
    riya = next(u for u in j['users'] if u['id'] == RIYA)
    day = riya['days'][today]
    assert day['first'] == '10:00' and riya['active_min'] >= 90
    assert any(b[0] == 660 and b[1] == 900 for b in day['breaks'])
    assert riya['submissions'] >= 1 and riya['actions'].get('whatsapp') == 1 and riya['actions'].get('note') == 1
    assert 'Riya' in j['digest'] or 'sc_riya' in j['digest']
    assert isinstance(j['insights'], list)
    log = cl(srv, ADMIN).get('/api/team/user/%d/log?from=%s&to=%s' % (RIYA, today, today)).get_json()
    assert any(x['kind'] == 'move' and x['to'] == 'Shared with Client' for x in log['items'])


def test_page_and_ping_script(srv):
    assert cl(srv, RIYA).get('/team-activity').status_code == 302          # recruiters are sent home
    r = cl(srv, ADMIN).get('/team-activity')
    assert r.status_code == 200 and b'Team Activity' in r.data and b'/team-ping.js' in r.data
    js = srv.app.test_client().get('/team-ping.js')
    assert js.status_code == 200 and b'/api/presence/ping' in js.data


def test_freelancer_ping_counts_too(srv):
    from test_naukri_screen import FL
    r = cl(srv, FL).post('/api/presence/ping')
    assert r.status_code == 200 and r.get_json()['ok']
    assert cl(srv, FL).get('/api/team/overview').status_code in (401, 403)
