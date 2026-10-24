"""
RecruitOS — Team Activity (Oct 2026)

Admin-only view of what each recruiter does in the ATS, in broad strokes:

  * time on the app   — first activity, last activity, active minutes, breaks
  * stage moves       — who moved which candidates into which stage
  * actions           — WhatsApp, e-mails, calls, CVs, notes, interviews …
  * insights          — effort vs outcome, stuck candidates, first-contact speed,
                        productive hours, jobs nobody touched, a daily summary

How the data is collected (all additive)
  1. PRESENCE  /team-ping.js is added to every app page. Once a minute, if the
     user moved the mouse / typed / scrolled in the last 5 minutes, it POSTs
     /api/presence/ping and that minute is stored (one row per user per minute;
     several open tabs still count once). Idle > 5 min = no rows = a break.
  2. STAGE ACTOR  stage_history gets an `actor_id` column. Every DB connection
     opened through server.get_db() carries a TEMP trigger that stamps the
     logged-in user on each new stage_history row — so all ~27 places that move
     a stage are covered without touching them. Rows written outside a request
     (cron, workers) stay 0 = "system".
  3. ACTIONS  an after-request hook records successful candidate actions
     (WhatsApp sent, e-mail sent, CV uploaded, note, interview, call …) by the
     route that handled them, with the user from the session.

Nothing is backfilled: presence, stage actors and actions start on the day
this is deployed. Stage history before that has no actor and is shown as
"not recorded".

Routes
  POST /api/presence/ping                 heartbeat (any logged-in user)
  GET  /api/team/overview?from=&to=&user= the dashboard data (company admin)
  GET  /team-activity                     the dashboard page (company admin)
  GET  /team-ping.js                      the heartbeat script

Tables (new): team_presence, team_events. Column (new): stage_history.actor_id.
"""

import datetime
import os
import re

from flask import Blueprint, Response, jsonify, redirect, request, session, g

from modules.shared import get_db, effective_company_id, real_user_id, is_company_admin, _core
from modules import register_migration

bp = Blueprint('team_activity', __name__, url_prefix='/api/team')
pages = Blueprint('team_activity_pages', __name__)
hooks = Blueprint('team_activity_hooks', __name__)
# The heartbeat lives outside /api/team (which is admin-only in modules/access.py)
# because every user — recruiters and freelancers included — sends it.
presence = Blueprint('team_activity_presence', __name__, url_prefix='/api/presence')

IST = datetime.timedelta(hours=5, minutes=30)
IDLE_MIN = 5                 # minutes without input before the user counts as idle
BREAK_MIN = 10               # a gap at least this long is shown as a break
STUCK_DAYS = 5               # candidate in a working stage with no movement this long
UNTOUCHED_DAYS = 3           # active job with no activity this long
MAX_RANGE_DAYS = 92

ACTOR_COL_OK = None          # set once stage_history.actor_id is known to exist


def now_ist():
    return datetime.datetime.utcnow() + IST


# ══════════════════════════════════════════════════════════════════════════
#  Schema
# ══════════════════════════════════════════════════════════════════════════
@register_migration
def _migrate_team_activity(conn):
    global ACTOR_COL_OK
    conn.execute('''CREATE TABLE IF NOT EXISTS team_presence (
        company_id INTEGER NOT NULL DEFAULT 0,
        user_id    INTEGER NOT NULL,
        day        TEXT NOT NULL,          -- IST date YYYY-MM-DD
        minute     INTEGER NOT NULL,       -- IST minute of the day 0..1439
        PRIMARY KEY (user_id, day, minute)
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_team_presence_co_day ON team_presence(company_id, day)')
    conn.execute('''CREATE TABLE IF NOT EXISTS team_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id   INTEGER NOT NULL DEFAULT 0,
        user_id      INTEGER NOT NULL DEFAULT 0,
        kind         TEXT NOT NULL DEFAULT '',
        candidate_id INTEGER DEFAULT 0,
        created_at   TEXT DEFAULT ''      -- IST iso
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_team_events_co_time ON team_events(company_id, created_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_team_events_cand ON team_events(candidate_id)')
    try:
        conn.execute('ALTER TABLE stage_history ADD COLUMN actor_id INTEGER DEFAULT 0')
    except Exception:
        pass
    try:
        conn.execute('CREATE INDEX IF NOT EXISTS idx_stage_history_actor ON stage_history(actor_id, created_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_stage_history_time ON stage_history(created_at)')
    except Exception:
        pass
    conn.commit()
    cols = {r[1] for r in conn.execute('PRAGMA table_info(stage_history)')}
    ACTOR_COL_OK = 'actor_id' in cols


# ══════════════════════════════════════════════════════════════════════════
#  Stage actor — attached to every connection by server.get_db()
# ══════════════════════════════════════════════════════════════════════════
def _actor_uid():
    try:
        from flask import has_request_context
        if has_request_context():
            return int(session.get('user_id') or 0)
    except Exception:
        pass
    return 0


_TRIGGER = ("CREATE TEMP TRIGGER IF NOT EXISTS hl_stage_actor AFTER INSERT ON main.stage_history "
            "WHEN COALESCE(NEW.actor_id, 0) = 0 AND hl_actor() > 0 "
            "BEGIN UPDATE stage_history SET actor_id = hl_actor() WHERE id = NEW.id; END")


def attach_actor(conn):
    """Give this connection a TEMP trigger that stamps the logged-in user on new
    stage_history rows. Temp = lives only on this connection, so a connection
    opened elsewhere (scripts, tools) is never affected. Never raises."""
    global ACTOR_COL_OK
    try:
        if ACTOR_COL_OK is None:
            cols = {r[1] for r in conn.execute('PRAGMA table_info(stage_history)')}
            if 'actor_id' not in cols:
                return               # migration has not run yet — try again on a later connection
            ACTOR_COL_OK = True
        if not ACTOR_COL_OK:
            return
        conn.create_function('hl_actor', 0, _actor_uid)
        conn.execute(_TRIGGER)
    except Exception as e:
        print('[team_activity] actor trigger skipped: %s' % e)


# ══════════════════════════════════════════════════════════════════════════
#  Actions — recorded from the route that handled them
# ══════════════════════════════════════════════════════════════════════════
_CAND = r'^/api/candidates/(\d+)/'
ACTION_ROUTES = [
    ('POST', re.compile(_CAND + r'wa-(send-log|queue-send)/?$'), 'whatsapp'),
    ('POST', re.compile(_CAND + r'(send-email|interview-message)/?$'), 'email'),
    ('POST', re.compile(_CAND + r'cv/?$'), 'cv'),
    ('POST', re.compile(_CAND + r'note/?$'), 'note'),
    ('POST', re.compile(_CAND + r'interviews/?$'), 'interview'),
    ('POST', re.compile(_CAND + r'offers/?$'), 'offer'),
    ('POST', re.compile(_CAND + r'request-update/?$'), 'update_request'),
    ('POST', re.compile(_CAND + r'analyse-call/?$'), 'call'),
    ('POST', re.compile(_CAND + r'add-to-job/?$'), 'add_to_job'),
    ('POST', re.compile(r'^/api/push-call/?$'), 'call'),
    ('POST', re.compile(r'^/api/hotlist/(mine/)?outcome/?$'), 'call'),
    ('POST', re.compile(r'^/api/extension/push/?$'), 'cv_push'),
]
CONTACT_KINDS = ('whatsapp', 'email', 'call')


@hooks.after_app_request
def _record_action(resp):
    try:
        if request.method != 'POST' or not (200 <= resp.status_code < 300):
            return resp
        path = request.path or ''
        if not path.startswith('/api/'):
            return resp
        for method, rx, kind in ACTION_ROUTES:
            m = rx.match(path)
            if not m:
                continue
            uid = int(session.get('user_id') or 0)
            if not uid or session.get('view_as_company'):
                return resp
            cid = 0
            if path.startswith('/api/candidates/'):
                cid = int(m.group(1))
            elif kind == 'cv_push':
                try:
                    cid = int((resp.get_json(silent=True) or {}).get('candidate_id') or 0)
                except Exception:
                    cid = 0
            elif kind == 'call' and 'hotlist' in path:
                cid = int((request.get_json(silent=True) or {}).get('candidate_id') or 0)
            conn = get_db()
            try:
                conn.execute('INSERT INTO team_events (company_id, user_id, kind, candidate_id, created_at) VALUES (?,?,?,?,?)',
                             (int(effective_company_id() or 0), uid, kind, cid,
                              now_ist().strftime('%Y-%m-%dT%H:%M:%S')))
                conn.commit()
            finally:
                conn.close()
            break
    except Exception as e:                       # never break a request over analytics
        print('[team_activity] action not recorded: %s' % e)
    return resp


# ── heartbeat script on every app page ───────────────────────────────────
APP_PAGES = re.compile(r'^/(|index\.html|v2|jobmail|talent-pool|newsletter|hotlist|my-calls|reengage|backup|schedule|'
                       r'orgmap|admin/storage|finance|team-activity)/?$')
_TAG = b'<script src="/team-ping.js" defer></script>'


@hooks.after_app_request
def _inject_ping(resp):
    try:
        if request.method != 'GET' or resp.status_code != 200 or not APP_PAGES.match(request.path or ''):
            return resp
        if not (resp.mimetype or '').startswith('text/html'):
            return resp
        resp.direct_passthrough = False
        body = resp.get_data()
        if b'/team-ping.js' in body:
            return resp
        i = body.lower().rfind(b'</body>')
        body = (body[:i] + _TAG + body[i:]) if i >= 0 else (body + _TAG)
        resp.set_data(body)
    except Exception as e:
        print('[team_activity] ping script not added: %s' % e)
    return resp


PING_JS = r"""(function(){
  if (window.__hlPing) return; window.__hlPing = true;
  var IDLE = %d * 60000, last = Date.now(), sentAt = 0;
  function act(){ var n = Date.now(), wasIdle = n - last > IDLE; last = n; if (wasIdle) ping(); }
  ['mousemove','mousedown','keydown','wheel','scroll','touchstart','input'].forEach(function(e){
    window.addEventListener(e, act, {passive: true, capture: true});
  });
  function ping(){
    var n = Date.now();
    if (n - last > IDLE || n - sentAt < 50000) return;
    sentAt = n;
    try { fetch('/api/presence/ping', {method: 'POST', credentials: 'same-origin', keepalive: true}).catch(function(){}); } catch (e) {}
  }
  ping();
  setInterval(ping, 60000);
})();""" % IDLE_MIN


@pages.route('/team-ping.js')
def team_ping_js():
    r = Response(PING_JS, mimetype='application/javascript')
    r.headers['Cache-Control'] = 'public, max-age=3600'
    return r


@presence.route('/ping', methods=['POST'])
def team_ping():
    uid = int(session.get('user_id') or 0)
    if not uid:
        return jsonify({'ok': False}), 401
    if session.get('view_as_company'):          # super-admin looking around is not team activity
        return jsonify({'ok': True, 'skipped': True})
    n = now_ist()
    conn = get_db()
    try:
        conn.execute('INSERT OR IGNORE INTO team_presence (company_id, user_id, day, minute) VALUES (?,?,?,?)',
                     (int(effective_company_id() or 0), uid, n.strftime('%Y-%m-%d'), n.hour * 60 + n.minute))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════
#  Dashboard data
# ══════════════════════════════════════════════════════════════════════════
BUCKETS = ['sourced', 'engaged', 'client', 'interview', 'offer', 'placed', 'dropped']
WORKING = ('engaged', 'client', 'interview', 'offer')


def bucket(stage):
    low = str(stage or '').strip().lower()
    if not low:
        return 'sourced'
    try:
        b = _core().job_summary_bucket(stage)
    except Exception:
        b = 'engaged'
    if b == 'placed' and 'offer' in low and 'reject' not in low and 'declin' not in low:
        return 'offer'
    if b == 'dropped' and 'central' in low:
        return 'sourced'
    return b


def _day(s):
    return datetime.datetime.strptime(s, '%Y-%m-%d').date()


def _range():
    today = now_ist().date()
    try:
        d2 = _day(request.args.get('to') or today.isoformat())
    except ValueError:
        d2 = today
    try:
        d1 = _day(request.args.get('from') or d2.isoformat())
    except ValueError:
        d1 = d2
    if d1 > d2:
        d1, d2 = d2, d1
    if (d2 - d1).days >= MAX_RANGE_DAYS:
        d1 = d2 - datetime.timedelta(days=MAX_RANGE_DAYS - 1)
    return d1, d2


def _clock12(minute):
    minute = int(minute) % (24 * 60)
    h, m = divmod(minute, 60)
    return '%d%s%s' % (h % 12 or 12, (':%02d' % m) if m else '', 'am' if h < 12 else 'pm')


def _hm(minute):
    minute = int(minute)
    return '%02d:%02d' % (minute // 60, minute % 60)


def segments(minutes):
    """Sorted active minutes -> [[start, end_exclusive], …], joining gaps shorter than BREAK_MIN."""
    out = []
    for m in sorted(set(minutes)):
        if out and m - out[-1][1] < BREAK_MIN:
            out[-1][1] = m + 1
        else:
            out.append([m, m + 1])
    return out


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _parse_ts(s):
    s = str(s or '').strip().replace(' ', 'T')[:19]
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        return None


def _team(conn, co):
    rows = conn.execute("SELECT id, username, display_name, role, COALESCE(is_company_admin,0) AS adm FROM users "
                        "WHERE company_id=? AND COALESCE(status,'approved')='approved' "
                        "AND COALESCE(role,'user') NOT IN ('hiring_manager','client','candidate') ORDER BY id", (co,)).fetchall()
    return {int(r['id']): {'id': int(r['id']), 'name': (r['display_name'] or r['username'] or ('User %s' % r['id'])),
                          'role': r['role'] or 'user', 'admin': bool(r['adm']) or (r['role'] == 'admin')} for r in rows}


def _assignees(conn, co):
    """mandate_id -> set(user ids) responsible for it."""
    out = {}
    try:
        for r in conn.execute('SELECT mandate_id, user_id FROM mandate_assignees WHERE company_id=? AND is_active=1', (co,)):
            out.setdefault(int(r['mandate_id']), set()).add(int(r['user_id']))
    except Exception:
        pass
    for r in conn.execute("SELECT id, COALESCE(assigned_user_id,0) AS a FROM mandates WHERE owner_id=? AND status='active'", (co,)):
        if int(r['a'] or 0) and int(r['id']) not in out:
            out[int(r['id'])] = {int(r['a'])}
    return out


def build_overview(conn, co, d1, d2, only_user=0, now=None):
    now = now or now_ist()
    a = d1.isoformat()
    b_excl = (d2 + datetime.timedelta(days=1)).isoformat()
    team = _team(conn, co)
    days = [(d1 + datetime.timedelta(days=i)).isoformat() for i in range((d2 - d1).days + 1)]

    def U(uid):
        uid = int(uid or 0)
        if uid not in team:
            team[uid] = {'id': uid, 'name': 'User %d' % uid, 'role': 'user', 'admin': False, 'former': True}
        u = team[uid]
        if 'days' not in u:
            u.update({'days': {}, 'active_min': 0, 'moves': 0, 'added': 0, 'stages': {k: 0 for k in BUCKETS},
                      'actions': {}, 'hours': [0] * 24, 'out_hours': [0] * 24, 'contact_lags': [], 'stuck': 0,
                      'jobs': 0, 'untouched_jobs': 0, 'last_seen': ''})
        return u

    for uid in list(team):
        U(uid)

    # ── presence ────────────────────────────────────────────────────────
    heat = [[0] * 24 for _ in range(7)]          # weekday (Mon=0) × hour, active minutes
    pres = {}
    for r in conn.execute('SELECT user_id, day, minute FROM team_presence WHERE company_id=? AND day>=? AND day<?',
                          (co, a, b_excl)):
        pres.setdefault((int(r['user_id']), r['day']), []).append(int(r['minute']))
    for (uid, day), mins in pres.items():
        u = U(uid)
        segs = segments(mins)
        breaks = [[segs[i][1], segs[i + 1][0]] for i in range(len(segs) - 1)]
        u['days'][day] = {'first': _hm(min(mins)), 'last': _hm(max(mins) + 1), 'active_min': len(set(mins)),
                          'span_min': max(mins) + 1 - min(mins), 'segments': segs, 'breaks': breaks,
                          'first_min': min(mins), 'last_min': max(mins) + 1}
        u['active_min'] += len(set(mins))
        wd = _day(day).weekday()
        for m in set(mins):
            u['hours'][m // 60] += 1
            heat[wd][m // 60] += 1

    # live now: a ping in the last IDLE_MIN+1 minutes
    today = now.strftime('%Y-%m-%d')
    nm = now.hour * 60 + now.minute
    for r in conn.execute("SELECT user_id, MAX(day || 'T' || printf('%04d', minute)) AS last FROM team_presence "
                          "WHERE company_id=? GROUP BY user_id", (co,)):
        u = U(r['user_id'])
        day, mm = r['last'].split('T')
        mm = int(mm)
        u['last_seen'] = '%sT%s' % (day, _hm(mm))
        u['live'] = (day == today and nm - mm <= IDLE_MIN + 1)

    # ── stage moves ─────────────────────────────────────────────────────
    sh_cols = {r[1] for r in conn.execute('PRAGMA table_info(stage_history)')}
    has_actor = 'actor_id' in sh_cols
    untracked = 0
    move_rows = conn.execute(
        'SELECT sh.candidate_id, sh.from_stage, sh.to_stage, sh.created_at, %s AS actor, c.created_at AS c_created '
        'FROM stage_history sh JOIN candidates c ON c.id=sh.candidate_id '
        'WHERE c.owner_id=? AND sh.created_at>=? AND sh.created_at<? ORDER BY sh.created_at'
        % ('COALESCE(sh.actor_id,0)' if has_actor else '0'), (co, a, b_excl)).fetchall()
    added_at = {}                                  # candidate -> (actor, datetime) added in range
    first_touch = {}                               # candidate -> first contact/move after add
    for r in move_rows:
        frm, to = (r['from_stage'] or '').strip(), (r['to_stage'] or '').strip()
        if frm == to:
            continue                               # a note, not a move
        actor = int(r['actor'] or 0)
        if not actor:
            untracked += 1
            continue
        u = U(actor)
        t = _parse_ts(r['created_at'])
        if not frm:
            u['added'] += 1
            if t:
                added_at[int(r['candidate_id'])] = (actor, t)
            continue
        u['moves'] += 1
        bk = bucket(to)
        u['stages'][bk] = u['stages'].get(bk, 0) + 1
        if t and bk in ('client', 'interview', 'offer', 'placed'):
            u['out_hours'][t.hour] += 1
        cid = int(r['candidate_id'])
        if t and cid in added_at and cid not in first_touch and t > added_at[cid][1]:
            first_touch[cid] = t

    # ── actions ─────────────────────────────────────────────────────────
    for r in conn.execute('SELECT user_id, kind, candidate_id, created_at FROM team_events '
                          'WHERE company_id=? AND created_at>=? AND created_at<? ORDER BY created_at', (co, a, b_excl)):
        u = U(r['user_id'])
        k = r['kind']
        u['actions'][k] = u['actions'].get(k, 0) + 1
        cid = int(r['candidate_id'] or 0)
        t = _parse_ts(r['created_at'])
        if k in CONTACT_KINDS + ('note',) and t and cid in added_at and t > added_at[cid][1]:
            if cid not in first_touch or t < first_touch[cid]:
                first_touch[cid] = t
    for cid, (actor, t0) in added_at.items():
        if cid in first_touch:
            U(actor)['contact_lags'].append((first_touch[cid] - t0).total_seconds() / 3600.0)

    # ── stuck candidates & untouched jobs (current state, not range) ────
    assign = _assignees(conn, co)
    active = {int(r['id']): dict(r) for r in conn.execute(
        "SELECT id, role, client, created_at FROM mandates WHERE owner_id=? AND status='active'", (co,))}
    last_move = {}
    for r in conn.execute('SELECT sh.candidate_id, MAX(sh.created_at) AS t FROM stage_history sh JOIN candidates c '
                          'ON c.id=sh.candidate_id WHERE c.owner_id=? GROUP BY sh.candidate_id', (co,)):
        last_move[int(r['candidate_id'])] = r['t']
    job_last = {}
    stuck_list = []
    cand_rows = conn.execute('SELECT id, name, stage, mandate_id, updated_at FROM candidates WHERE owner_id=?', (co,)).fetchall()
    for c in cand_rows:
        mid = int(c['mandate_id'] or 0)
        lm = last_move.get(int(c['id'])) or ''
        if mid in active and lm and lm > job_last.get(mid, ''):
            job_last[mid] = lm          # only real activity (stage moves / notes), not background updates
        lt = lm or c['updated_at'] or ''
        if mid not in active or bucket(c['stage']) not in WORKING:
            continue
        t = _parse_ts(lt)
        if not t:
            continue
        idle = (now - t).days
        if idle >= STUCK_DAYS:
            owners = sorted(assign.get(mid) or [])
            stuck_list.append({'candidate_id': int(c['id']), 'name': c['name'] or '', 'stage': c['stage'] or '',
                               'mandate_id': mid, 'job': active[mid]['role'] or '', 'client': active[mid]['client'] or '',
                               'days': idle, 'owners': owners})
            for o in owners:
                U(o)['stuck'] += 1
    for r in conn.execute('SELECT t.created_at, c.mandate_id FROM team_events t JOIN candidates c ON c.id=t.candidate_id '
                          'WHERE t.company_id=? AND t.created_at>=?',
                          (co, (now - datetime.timedelta(days=30)).strftime('%Y-%m-%d'))):
        mid = int(r['mandate_id'] or 0)
        if mid in active and r['created_at'] > job_last.get(mid, ''):
            job_last[mid] = r['created_at']
    untouched = []
    for mid, m in active.items():
        ref = job_last.get(mid) or m['created_at'] or ''
        t = _parse_ts(ref)
        idle = (now - t).days if t else None
        owners = sorted(assign.get(mid) or [])
        for o in owners:
            U(o)['jobs'] += 1
        if idle is not None and idle >= UNTOUCHED_DAYS:
            untouched.append({'mandate_id': mid, 'job': m['role'] or '', 'client': m['client'] or '', 'days': idle,
                              'never': mid not in job_last, 'owners': owners})
            for o in owners:
                U(o)['untouched_jobs'] += 1
    stuck_list.sort(key=lambda x: -x['days'])
    untouched.sort(key=lambda x: -x['days'])

    # ── per-user summary ────────────────────────────────────────────────
    users = []
    for uid, u in team.items():
        if only_user and uid != only_user:
            continue
        if 'days' not in u:
            continue
        st = u['stages']
        dd = [x for x in u['days'].values()]
        u['days_active'] = len(dd)
        u['avg_first'] = _hm(sum(x['first_min'] for x in dd) / len(dd)) if dd else ''
        u['avg_last'] = _hm(sum(x['last_min'] for x in dd) / len(dd)) if dd else ''
        u['avg_active_min'] = round(u['active_min'] / len(dd)) if dd else 0
        u['submissions'], u['interviews'], u['offers'], u['placed'] = st['client'], st['interview'], st['offer'], st['placed']
        u['contacts'] = sum(u['actions'].get(k, 0) for k in CONTACT_KINDS)
        lag = _median(u.pop('contact_lags'))
        u['first_contact_h'] = round(lag, 1) if lag is not None else None
        hrs = u['active_min'] / 60.0
        u['outcome_points'] = st['client'] + 2 * st['interview'] + 3 * st['offer'] + 4 * st['placed']
        u['per_hour'] = round(u['outcome_points'] / hrs, 2) if hrs >= 0.5 else None
        u['empty'] = not (u['active_min'] or u['moves'] or u['added'] or u['actions'])
        users.append(u)
    users.sort(key=lambda u: (u['empty'], -u['active_min'], -u['moves']))

    tot = lambda k: sum(u.get(k) or 0 for u in users)
    totals = {'active_min': tot('active_min'), 'moves': tot('moves'), 'added': tot('added'),
              'submissions': tot('submissions'), 'interviews': tot('interviews'), 'offers': tot('offers'),
              'placed': tot('placed'), 'contacts': tot('contacts'),
              'live': sum(1 for u in users if u.get('live')),
              'stuck': len(stuck_list), 'untouched': len(untouched), 'untracked_moves': untracked}
    firsts = [x['first_min'] for u in users for x in u['days'].values()]
    totals['avg_first'] = _hm(sum(firsts) / len(firsts)) if firsts else ''
    out_by_hour = [sum(u['out_hours'][h] for u in users) for h in range(24)]

    names = {uid: t['name'] for uid, t in team.items()}
    for x in stuck_list + untouched:
        x['owner_names'] = [names.get(o, 'User %d' % o) for o in x['owners']]
    if only_user:
        stuck_list = [x for x in stuck_list if only_user in x['owners']]
        untouched = [x for x in untouched if only_user in x['owners']]

    data = {'ok': True, 'from': d1.isoformat(), 'to': d2.isoformat(), 'days': days, 'today': today,
            'now': now.strftime('%Y-%m-%dT%H:%M'), 'users': users, 'totals': totals, 'heat': heat,
            'out_by_hour': out_by_hour, 'stuck': stuck_list[:80], 'untouched': untouched,
            'rules': {'idle_min': IDLE_MIN, 'break_min': BREAK_MIN, 'stuck_days': STUCK_DAYS,
                      'untouched_days': UNTOUCHED_DAYS},
            'team': [{'id': t['id'], 'name': t['name']} for t in team.values() if not t.get('former')]}
    data['insights'] = insights(data)
    data['digest'] = digest(data)
    return data


# ── insights: plain sentences, only the ones the data supports ───────────
def _h(mins):
    mins = int(round(mins or 0))
    return '%dh %02dm' % (mins // 60, mins % 60) if mins >= 60 else '%dm' % mins


def insights(d):
    users = [u for u in d['users'] if not u['empty']]
    out = []
    if not users:
        return [{'tone': 'info', 'title': 'No activity recorded yet',
                 'text': 'Tracking starts once recruiters open HireLab after this update. Check back after a working day.'}]
    scored = [u for u in users if u['per_hour'] is not None and u['active_min'] >= 60]
    if len(scored) >= 2:
        best = max(scored, key=lambda u: u['per_hour'])
        if best['outcome_points']:
            out.append({'tone': 'good', 'title': 'Best results per hour', 'user': best['id'],
                        'text': '%s turned %s of app time into %d submission%s, %d interview%s and %d offer%s.'
                                % (best['name'], _h(best['active_min']), best['submissions'], 's' * (best['submissions'] != 1),
                                   best['interviews'], 's' * (best['interviews'] != 1), best['offers'], 's' * (best['offers'] != 1))})
        avg_min = sum(u['active_min'] for u in scored) / len(scored)
        busy_low = [u for u in scored if u['active_min'] >= avg_min and u['submissions'] + u['interviews'] + u['offers'] == 0]
        for u in busy_low[:2]:
            out.append({'tone': 'warn', 'title': 'Busy, but nothing moved forward', 'user': u['id'],
                        'text': '%s was active %s (team average %s) with %d stage moves, but none reached Submitted, Interview or Offer.'
                                % (u['name'], _h(u['active_min']), _h(avg_min), u['moves'])})
    for u in sorted(users, key=lambda u: -u['stuck'])[:2]:
        if u['stuck'] >= 3:
            out.append({'tone': 'warn', 'title': 'Candidates going cold', 'user': u['id'],
                        'text': '%d of %s\'s candidates have sat in a working stage for %d+ days with no movement.'
                                % (u['stuck'], u['name'], d['rules']['stuck_days'])})
    if d['untouched']:
        top = d['untouched'][0]
        out.append({'tone': 'bad', 'title': '%d job%s nobody worked on' % (len(d['untouched']), 's' * (len(d['untouched']) != 1)),
                    'text': '%s%s has had no activity for %d days. Untouched jobs are where billing slips.'
                            % (top['job'], (' — ' + top['client']) if top['client'] else '', top['days'])})
    lags = [u for u in users if u['first_contact_h'] is not None]
    if lags:
        slow = max(lags, key=lambda u: u['first_contact_h'])
        fast = min(lags, key=lambda u: u['first_contact_h'])
        if slow['first_contact_h'] >= 24:
            out.append({'tone': 'warn', 'title': 'Slow first contact', 'user': slow['id'],
                        'text': 'New candidates added by %s wait %.0f h (median) for the first call, WhatsApp or e-mail.'
                                % (slow['name'], slow['first_contact_h'])})
        elif fast['first_contact_h'] <= 2 and len(lags) > 1:
            out.append({'tone': 'good', 'title': 'Fast first contact', 'user': fast['id'],
                        'text': '%s reaches new candidates in %.1f h (median).' % (fast['name'], fast['first_contact_h'])})
    ob = d['out_by_hour']
    if sum(ob) >= 5:
        best_h = max(range(24), key=lambda h: (ob[h] + (ob[h + 1] if h < 23 else 0)))
        out.append({'tone': 'info', 'title': 'Most productive hours',
                    'text': 'Most submissions, interviews and offers are moved between %s and %s. Keep that window for calls.'
                            % (_clock12(best_h * 60), _clock12(min(24, best_h + 2) * 60))})
    late = [u for u in users if u['avg_first'] and u['avg_first'] > '11:00' and u['days_active'] >= 2]
    for u in late[:1]:
        out.append({'tone': 'info', 'title': 'Late starts', 'user': u['id'],
                    'text': '%s usually starts in HireLab around %s.' % (u['name'], _clock12(int(u['avg_first'][:2]) * 60 + int(u['avg_first'][3:])))})
    if d['totals']['untracked_moves']:
        out.append({'tone': 'info', 'title': 'Older moves not attributed',
                    'text': '%d stage moves in this period happened before tracking started (or by the system), '
                            'so they are not counted for anyone.' % d['totals']['untracked_moves']})
    return out[:8]


def digest(d):
    lines = []
    for u in d['users']:
        if u['empty']:
            continue
        a = u['actions']
        bits = ['%s active' % _h(u['active_min'])]
        if u['days_active'] == 1:
            day = list(u['days'].values())[0]
            bits[0] += ' (%s–%s)' % (day['first'], day['last'])
        bits.append('%d moves' % u['moves'])
        if u['added']:
            bits.append('%d added' % u['added'])
        for k, lbl in (('call', 'calls'), ('whatsapp', 'WhatsApp'), ('email', 'e-mails')):
            if a.get(k):
                bits.append('%d %s' % (a[k], lbl))
        if u['submissions']:
            bits.append('%d submitted' % u['submissions'])
        if u['interviews']:
            bits.append('%d interviews' % u['interviews'])
        if u['offers']:
            bits.append('%d offers' % u['offers'])
        if u['untouched_jobs']:
            bits.append('%d job%s untouched' % (u['untouched_jobs'], 's' * (u['untouched_jobs'] != 1)))
        lines.append('%s: %s' % (u['name'], ', '.join(bits)))
    head = 'HireLab team — %s' % (d['from'] if d['from'] == d['to'] else '%s to %s' % (d['from'], d['to']))
    return head + '\n' + ('\n'.join('• ' + x for x in lines) if lines else '• No activity recorded.')


def _admin_guard():
    if not session.get('user_id'):
        return jsonify({'ok': False, 'error': 'auth_required'}), 401
    if not is_company_admin():
        return jsonify({'ok': False, 'error': 'Only an admin can see team activity.'}), 403
    return None


@bp.route('/overview', methods=['GET'])
def team_overview():
    bad = _admin_guard()
    if bad:
        return bad
    d1, d2 = _range()
    try:
        only = int(request.args.get('user') or 0)
    except ValueError:
        only = 0
    conn = get_db()
    try:
        return jsonify(build_overview(conn, int(effective_company_id() or 0), d1, d2, only))
    finally:
        conn.close()


@bp.route('/user/<int:uid>/log', methods=['GET'])
def team_user_log(uid):
    """The latest moves and actions of one person (for the side drawer)."""
    bad = _admin_guard()
    if bad:
        return bad
    d1, d2 = _range()
    a, b = d1.isoformat(), (d2 + datetime.timedelta(days=1)).isoformat()
    co = int(effective_company_id() or 0)
    conn = get_db()
    try:
        items = []
        cols = {r[1] for r in conn.execute('PRAGMA table_info(stage_history)')}
        if 'actor_id' in cols:
            for r in conn.execute('SELECT sh.created_at, sh.from_stage, sh.to_stage, c.id AS cid, c.name, c.mandate_id '
                                  'FROM stage_history sh JOIN candidates c ON c.id=sh.candidate_id '
                                  'WHERE c.owner_id=? AND sh.actor_id=? AND sh.created_at>=? AND sh.created_at<? '
                                  'AND COALESCE(sh.from_stage,"")!=COALESCE(sh.to_stage,"") ORDER BY sh.created_at DESC LIMIT 150',
                                  (co, uid, a, b)):
                items.append({'at': r['created_at'], 'kind': 'added' if not (r['from_stage'] or '').strip() else 'move',
                              'from': r['from_stage'] or '', 'to': r['to_stage'] or '', 'candidate_id': r['cid'],
                              'name': r['name'] or '', 'mandate_id': r['mandate_id']})
        for r in conn.execute('SELECT t.created_at, t.kind, t.candidate_id, c.name, c.mandate_id FROM team_events t '
                              'LEFT JOIN candidates c ON c.id=t.candidate_id AND c.owner_id=t.company_id '
                              'WHERE t.company_id=? AND t.user_id=? AND t.created_at>=? AND t.created_at<? '
                              'ORDER BY t.created_at DESC LIMIT 150', (co, uid, a, b)):
            items.append({'at': r['created_at'], 'kind': r['kind'], 'candidate_id': r['candidate_id'] or 0,
                          'name': r['name'] or '', 'mandate_id': r['mandate_id'] or 0})
        items.sort(key=lambda x: x['at'], reverse=True)
        return jsonify({'ok': True, 'items': items[:200]})
    finally:
        conn.close()


@pages.route('/team-activity')
def team_activity_page():
    core = _core()
    if not core.current_user():
        return redirect('/')
    if not is_company_admin():
        return redirect('/')
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'team-activity.html')
    with open(p, encoding='utf-8') as f:
        return Response(f.read(), mimetype='text/html')
