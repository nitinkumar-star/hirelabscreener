"""
Hot List — engagement score + "who to call today" (Wave 4 of candidate engagement).

Every person in the Talent Pool gets an ENGAGEMENT SCORE (0–100) from what they
actually did with HireLab's e-mails and forms in the last 120 days. Each signal
fades with time (half-life 30 days), so last week's click beats last quarter's.

  signal                                              points
  ─────────────────────────────────────────────────── ──────
  Said "Yes, I'm interested" to a job mail                 40
  Submitted the update-profile form                        25
    + changed company                                     +15
    + new CV                                              +10
    + CTC / notice period / location changed       +5 each
  Clicked a job mail (no answer yet)                       12
  Clicked a newsletter / re-engagement mail                10
  Opened a job mail                                         4
  Opened a newsletter / re-engagement mail                  3
  "Not right now — contact me in 3 months"  -10 now; +20 once the 3 months are up
  "Not right now" for any other reason                    -15

The HOT LIST is everyone with score ≥ the cut-off (default 20) who is NOT live in
a pipeline, NOT unsubscribed, has a phone number, and has not been handled since
their latest signal. Each row says WHY in plain words ("Changed company Schneider →
ABB · 3 days ago").

Actions (admin): assign to a caller (any user, including freelance accounts) —
which also creates a call task — snooze 30 days, or record an outcome directly.
Callers work from /my-calls: their list with click-to-call, and one tap per
outcome (Interested / Call back / Not interested / Not reachable / Wrong number).
An outcome closes the row; a NEW signal later brings the person back.

Additive only: new tables hotlist_state, hotlist_log.
"""

import datetime
import json
import os
import re

from flask import Blueprint, request, jsonify, redirect, Response

from modules.shared import get_db, ts, effective_company_id, real_user_id, is_company_admin, login_required, _core
from modules import register_migration

bp = Blueprint('hotlist', __name__, url_prefix='/api/hotlist')
pages = Blueprint('hotlist_pages', __name__)
cand_bp = Blueprint('hotlist_cand', __name__)

IST = datetime.timedelta(hours=5, minutes=30)
WINDOW_DAYS = 120
HALF_LIFE = 30.0
DEFAULT_MIN = 20
OUTCOMES = [('interested', 'Interested'), ('callback', 'Call back later'), ('not_interested', 'Not interested'),
            ('not_reachable', 'Not reachable'), ('wrong_number', 'Wrong number')]
OUTCOME_LABELS = dict(OUTCOMES)


@register_migration
def _migrate_hotlist(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS hotlist_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        person_key TEXT NOT NULL,
        candidate_id INTEGER DEFAULT 0,
        status TEXT DEFAULT 'open',
        assigned_to INTEGER DEFAULT 0,
        assigned_by INTEGER DEFAULT 0,
        assigned_at TEXT DEFAULT '',
        task_id INTEGER DEFAULT 0,
        outcome TEXT DEFAULT '',
        note TEXT DEFAULT '',
        outcome_by INTEGER DEFAULT 0,
        outcome_at TEXT DEFAULT '',
        callback_at TEXT DEFAULT '',
        snooze_until TEXT DEFAULT '',
        score_at_action INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT '',
        UNIQUE(company_id, person_key)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS hotlist_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        person_key TEXT NOT NULL,
        candidate_id INTEGER DEFAULT 0,
        action TEXT DEFAULT '',
        detail TEXT DEFAULT '',
        user_id INTEGER DEFAULT 0,
        created_at TEXT DEFAULT ''
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_hl_state_assignee ON hotlist_state(company_id, assigned_to, status)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_hl_log_co ON hotlist_log(company_id, created_at)')
    conn.commit()


# ── helpers ────────────────────────────────────────────────────────────────
def now_ist():
    return datetime.datetime.utcnow() + IST


def _rows(conn, sql, params=()):
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:          # a source table from an earlier wave may be missing on old DBs
        return []


def _err(msg, code=400):
    return jsonify({'ok': False, 'error': msg}), code


def _oid():
    return int(effective_company_id() or 0)


def _dt(s):
    s = str(s or '').strip().replace(' ', 'T')
    for fmt, n in (('%Y-%m-%dT%H:%M:%S', 19), ('%Y-%m-%dT%H:%M', 16), ('%Y-%m-%d', 10)):
        try:
            return datetime.datetime.strptime(s[:n], fmt)
        except ValueError:
            continue
    return None


def _ago(d, now):
    days = (now - d).days
    if days <= 0:
        return 'today'
    if days == 1:
        return 'yesterday'
    if days < 31:
        return '%d days ago' % days
    return '%d months ago' % max(1, round(days / 30.0))


def _digits(p):
    d = re.sub(r'\D', '', str(p or ''))
    return d[-10:] if len(d) >= 10 else ''


# ══════════════════════════════════════════════════════════════════════════
#  Scoring
# ══════════════════════════════════════════════════════════════════════════
def _add(acc, email, pts, when, text, now, kind):
    if not email or not when:
        return
    d = _dt(when)
    if not d:
        return
    age = max(0.0, (now - d).total_seconds() / 86400.0)
    if age > WINDOW_DAYS:
        return
    eff = pts * (0.5 ** (age / HALF_LIFE)) if pts > 0 else pts * (0.5 ** (age / (HALF_LIFE * 2)))
    a = acc.setdefault(email, {'score': 0.0, 'reasons': [], 'last': ''})
    a['score'] += eff
    a['reasons'].append({'pts': round(eff, 1), 'text': text, 'when': _ago(d, now), 'at': str(when)[:19], 'kind': kind})
    if pts > 0 and str(when) > a['last']:
        a['last'] = str(when)[:19]


def compute(conn, oid, now=None):
    """{email: {'score': int, 'reasons': [...], 'last': iso}} for every person with a signal."""
    now = now or now_ist()
    since = (now - datetime.timedelta(days=WINDOW_DAYS)).strftime('%Y-%m-%dT%H:%M:%S')
    acc = {}
    # job mail
    for r in _rows(conn, "SELECT r.email, r.opened_at, r.clicked_at, r.response, r.response_reason, r.responded_at, "
                         "m.role FROM jobmail_recipients r LEFT JOIN mandates m ON m.id=r.mandate_id "
                         "WHERE r.company_id=? AND r.status='sent' AND r.sent_at>=?", (oid, since)):
        role = r['role'] or 'a job'
        if r['response'] == 'interested':
            _add(acc, r['email'], 40, r['responded_at'], 'Said Interested to %s' % role, now, 'interested')
        elif r['response'] == 'not_now':
            if 'later' in str(r['response_reason'] or '').lower() or '3 months' in str(r['response_reason'] or ''):
                d = _dt(r['responded_at'])
                if d and (now - d).days >= 85:
                    _add(acc, r['email'], 20, (d + datetime.timedelta(days=90)).strftime('%Y-%m-%dT%H:%M:%S'),
                         'Asked to be contacted in 3 months (for %s) — that time is now' % role, now, 'followup')
                else:
                    _add(acc, r['email'], -10, r['responded_at'], 'Asked to be contacted later (%s)' % role, now, 'later')
            else:
                _add(acc, r['email'], -15, r['responded_at'], 'Not right now for %s%s' % (
                    role, (': ' + r['response_reason']) if r['response_reason'] else ''), now, 'not_now')
        elif r['clicked_at']:
            _add(acc, r['email'], 12, r['clicked_at'], 'Clicked the %s job mail but did not answer' % role, now, 'click')
        elif r['opened_at']:
            _add(acc, r['email'], 4, r['opened_at'], 'Opened the %s job mail' % role, now, 'open')
    # newsletter
    for r in _rows(conn, "SELECT r.email, r.opened_at, r.clicked_at FROM newsletter_recipients r WHERE r.company_id=? "
                         "AND r.status='sent' AND r.sent_at>=?", (oid, since)):
        if r['clicked_at']:
            _add(acc, r['email'], 10, r['clicked_at'], 'Clicked "refresh profile" in Market Pulse', now, 'click')
        elif r['opened_at']:
            _add(acc, r['email'], 3, r['opened_at'], 'Opened Market Pulse', now, 'open')
    # re-engagement
    for r in _rows(conn, "SELECT email, opened_at, clicked_at FROM reengage_log WHERE company_id=? AND status='sent' "
                         "AND sent_at>=?", (oid, since)):
        if r['clicked_at']:
            _add(acc, r['email'], 10, r['clicked_at'], 'Clicked the profile-update e-mail', now, 'click')
        elif r['opened_at']:
            _add(acc, r['email'], 3, r['opened_at'], 'Opened the profile-update e-mail', now, 'open')
    # profile updates
    for r in _rows(conn, "SELECT email, changes, with_cv, created_at FROM talent_updates WHERE company_id=? AND email<>'' "
                         "AND created_at>=?", (oid, since)):
        try:
            ch = json.loads(r['changes'] or '[]')
        except Exception:
            ch = []
        _add(acc, r['email'], 25, r['created_at'], 'Updated their profile', now, 'update')
        for c in ch:
            if c.get('field') == 'company':
                _add(acc, r['email'], 15, r['created_at'], 'Changed company: %s → %s' % (c.get('from'), c.get('to')), now, 'company')
            elif c.get('field') in ('ctc_current', 'ctc_expected', 'notice_period', 'location', 'preferred_location'):
                _add(acc, r['email'], 5, r['created_at'], '%s: %s → %s' % (c.get('label'), c.get('from'), c.get('to')), now, 'change')
        if r['with_cv']:
            _add(acc, r['email'], 10, r['created_at'], 'Uploaded a new CV', now, 'cv')
    out = {}
    for em, a in acc.items():
        a['reasons'].sort(key=lambda x: -abs(x['pts']))
        out[str(em).strip().lower()] = {'score': int(max(0, min(100, round(a['score'])))), 'reasons': a['reasons'][:6],
                                        'last': a['last']}
    return out


def _states(conn, oid):
    return {r['person_key']: r for r in _rows(conn, 'SELECT * FROM hotlist_state WHERE company_id=?', (oid,))}


def _people(conn, oid):
    try:
        from modules import segments as sg
        if not sg.get_settings(conn, oid)['last_scan_at']:
            sg.scan_company(conn, oid)
    except Exception:
        pass
    return {r['email']: r for r in _rows(conn, "SELECT * FROM talent_profiles WHERE company_id=? AND email<>''", (oid,))}


def _phones(conn, oid, ids):
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ','.join('?' * len(chunk))
        for r in conn.execute('SELECT id, phone, mandate_id, do_not_email FROM candidates WHERE id IN (%s)' % q, chunk):
            out[r['id']] = r
    return out


def build_list(conn, oid, min_score=DEFAULT_MIN, include_handled=False, now=None):
    """Ranked hot list rows (dicts)."""
    now = now or now_ist()
    scores = compute(conn, oid, now)
    people = _people(conn, oid)
    states = _states(conn, oid)
    ids = [people[em]['candidate_id'] for em in scores if em in people]
    ph = _phones(conn, oid, ids)
    users = {r['id']: (r['display_name'] or r['username']) for r in _rows(conn, 'SELECT id, username, display_name FROM users '
                                                                              'WHERE company_id=?', (oid,))}
    rows = []
    nowiso = now.strftime('%Y-%m-%dT%H:%M:%S')
    for em, s in scores.items():
        p = people.get(em)
        if not p or s['score'] < min_score or not s['last']:
            continue
        c = ph.get(p['candidate_id'])
        phone = (c['phone'] if c else '') or ''
        st = states.get(p['person_key'])
        state = 'open'
        if st:
            if st['status'] == 'snoozed' and str(st['snooze_until'] or '') > nowiso:
                state = 'snoozed'
            elif st['status'] == 'assigned':
                state = 'assigned'
            elif st['status'] == 'done' and str(st['outcome_at'] or '') >= s['last']:
                state = 'done'                     # handled after the latest signal
            elif st['status'] == 'callback':
                state = 'callback' if str(st['callback_at'] or '') > nowiso[:16] else ('assigned' if st['assigned_to'] else 'open')
        hidden = ''
        if p['live']:
            hidden = 'Live in a pipeline'
        elif not p['emailable'] and c and c['do_not_email']:
            hidden = 'Unsubscribed'
        elif not _digits(phone):
            hidden = 'No phone number'
        if hidden and not include_handled:
            continue
        if state in ('done', 'snoozed', 'callback') and not include_handled:
            continue
        try:
            from modules.segments import effective, IND_LABELS
            ind, _f = effective(p)
            inds = [IND_LABELS.get(k, k) for k in ind]
        except Exception:
            inds = []
        rows.append({'person_key': p['person_key'], 'candidate_id': p['candidate_id'], 'mandate_id': c['mandate_id'] if c else 0,
                     'name': p['name'], 'email': em, 'phone': phone, 'designation': p['designation'], 'company': p['company'],
                     'experience': p['experience'], 'location': p['location'], 'industries': inds,
                     'score': s['score'], 'reasons': s['reasons'], 'last_signal': s['last'], 'state': state, 'hidden': hidden,
                     'assigned_to': (st['assigned_to'] if st else 0) or 0,
                     'assigned_name': users.get((st['assigned_to'] if st else 0) or 0, ''),
                     'outcome': (st['outcome'] if st else '') or '', 'note': (st['note'] if st else '') or '',
                     'callback_at': (st['callback_at'] if st else '') or ''})
    rows.sort(key=lambda r: r['last_signal'], reverse=True)       # newest first among equal scores
    rows.sort(key=lambda r: -r['score'])
    return rows


# ══════════════════════════════════════════════════════════════════════════
#  State changes
# ══════════════════════════════════════════════════════════════════════════
def _log(conn, oid, key, cid, action, detail):
    conn.execute('INSERT INTO hotlist_log (company_id, person_key, candidate_id, action, detail, user_id, created_at) '
                 'VALUES (?,?,?,?,?,?,?)', (oid, key, cid, action, detail[:500], int(real_user_id() or 0), ts()))


def _upsert(conn, oid, key, cid, **sets):
    sets['updated_at'] = ts()
    conn.execute('INSERT OR IGNORE INTO hotlist_state (company_id, person_key, candidate_id) VALUES (?,?,?)', (oid, key, cid))
    conn.execute('UPDATE hotlist_state SET %s WHERE company_id=? AND person_key=?' % ','.join(k + '=?' for k in sets),
                 list(sets.values()) + [oid, key])


def _person(conn, oid, key):
    r = conn.execute('SELECT * FROM talent_profiles WHERE company_id=? AND person_key=?', (oid, key)).fetchone()
    return dict(r) if r else None


def _task(conn, oid, uid, cid, title, notes, due):
    c = conn.execute('SELECT name, mandate_id FROM candidates WHERE id=?', (cid,)).fetchone()
    row = {'candidate_id': cid, 'mandate_id': None, 'candidate_name': (c['name'] if c else '') or '', 'mandate_label': '',
           'note': title, 'notes': notes, 'due_at': due, 'done': 0, 'stage': 'todo', 'created_at': ts(), 'owner_id': oid,
           'created_by': int(real_user_id() or 0), 'assigned_to': uid, 'updated_at': ts(), 'priority': 'high'}
    cols = {r[1] for r in conn.execute('PRAGMA table_info(reminders)')}
    row = {k: v for k, v in row.items() if k in cols}
    return conn.execute('INSERT INTO reminders (%s) VALUES (%s)' % (','.join(row), ','.join('?' * len(row))),
                        list(row.values())).lastrowid


def _event(conn, cid, text):
    try:
        conn.execute('INSERT INTO candidate_events (candidate_id,event_type,detail,created_at) VALUES (?,?,?,?)',
                     (cid, 'note', text, ts()))
    except Exception:
        pass


def _close_task(conn, tid):
    if not tid:
        return
    try:
        conn.execute("UPDATE reminders SET done=1, completed_at=? WHERE id=?", (ts(), tid))
    except Exception:
        try:
            conn.execute('UPDATE reminders SET done=1 WHERE id=?', (tid,))
        except Exception:
            pass


def record_outcome(conn, oid, key, outcome, note='', callback_at=''):
    p = _person(conn, oid, key)
    if not p:
        return False, 'Not found'
    if outcome not in OUTCOME_LABELS:
        return False, 'Unknown outcome'
    st = conn.execute('SELECT * FROM hotlist_state WHERE company_id=? AND person_key=?', (oid, key)).fetchone()
    status = 'done'
    if outcome == 'callback':
        cb = _dt(callback_at)
        if not cb:
            return False, 'Pick a call-back date'
        callback_at = cb.strftime('%Y-%m-%dT%H:%M')
        status = 'callback'
    who = ''
    try:
        u = _core().current_user() or {}
        who = u.get('display_name') or u.get('username') or ''
    except Exception:
        pass
    scores = compute(conn, oid)
    _upsert(conn, oid, key, p['candidate_id'], status=status, outcome=outcome, note=str(note or '')[:500],
            outcome_by=int(real_user_id() or 0), outcome_at=ts(), callback_at=callback_at if status == 'callback' else '',
            score_at_action=(scores.get(p['email'], {}) or {}).get('score', 0))
    if st and st['task_id']:
        _close_task(conn, st['task_id'])
    text = 'Hot List call: %s%s%s%s' % (OUTCOME_LABELS[outcome],
                                         (' (call back %s)' % callback_at.replace('T', ' ')) if status == 'callback' else '',
                                         (' — ' + note.strip()) if note and note.strip() else '', (' · by ' + who) if who else '')
    _event(conn, p['candidate_id'], text)
    _log(conn, oid, key, p['candidate_id'], 'outcome:' + outcome, text)
    if status == 'callback':
        uid = int((st['assigned_to'] if st else 0) or real_user_id() or 0)
        tid = _task(conn, oid, uid, p['candidate_id'], 'Call back %s' % (p['name'] or 'candidate'),
                    'Hot List call-back. %s' % (note or ''), callback_at)
        _upsert(conn, oid, key, p['candidate_id'], task_id=tid, assigned_to=uid)
    if outcome == 'interested':
        # the person who assigned the call (or an admin) picks it up from here
        boss = int((st['assigned_by'] if st else 0) or 0)
        if boss and boss != int(real_user_id() or 0):
            _task(conn, oid, boss, p['candidate_id'], 'Follow up: %s is interested (Hot List call)' % (p['name'] or 'candidate'),
                  text, (now_ist() + datetime.timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M'))
    conn.commit()
    return True, ''


# ══════════════════════════════════════════════════════════════════════════
#  API — admin
# ══════════════════════════════════════════════════════════════════════════
def _admin():
    if not is_company_admin():
        return _err('Only an admin can open the Hot List.', 403)
    return None


@bp.route('/list', methods=['GET'])
@login_required
def hot_list():
    e = _admin()
    if e:
        return e
    oid = _oid()
    try:
        mn = max(0, min(100, int(request.args.get('min') or DEFAULT_MIN)))
    except ValueError:
        mn = DEFAULT_MIN
    conn = get_db()
    try:
        rows = build_list(conn, oid, mn, include_handled=True)
        users = _rows(conn, "SELECT id, username, display_name, role FROM users WHERE company_id=? AND "
                            "COALESCE(status,'approved')='approved' ORDER BY display_name, username", (oid,))
        since = (now_ist() - datetime.timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%S')
        outcomes = {}
        for r in _rows(conn, "SELECT outcome, COUNT(*) AS n FROM hotlist_state WHERE company_id=? AND outcome_at>=? GROUP BY outcome",
                       (oid, since)):
            outcomes[r['outcome']] = r['n']
        k = {'open': sum(1 for r in rows if r['state'] == 'open' and not r['hidden']),
             'assigned': sum(1 for r in rows if r['state'] == 'assigned' and not r['hidden']),
             'callback': sum(1 for r in rows if r['state'] == 'callback'),
             'very_hot': sum(1 for r in rows if r['score'] >= 50 and r['state'] == 'open' and not r['hidden']),
             'done_7d': sum(outcomes.values()), 'interested_7d': outcomes.get('interested', 0)}
        return jsonify({'ok': True, 'rows': rows[:1000], 'kpis': k, 'min': mn,
                        'users': [{'id': u['id'], 'name': u['display_name'] or u['username'],
                                   'caller': u['role'] == 'freelancer_sourcer'} for u in users],
                        'outcomes': [{'key': a, 'label': b} for a, b in OUTCOMES]})
    finally:
        conn.close()


@bp.route('/assign', methods=['POST'])
@login_required
def assign():
    e = _admin()
    if e:
        return e
    d = request.get_json(silent=True) or {}
    oid = _oid()
    try:
        uid = int(d.get('user_id') or 0)
    except (TypeError, ValueError):
        uid = 0
    keys = [str(k) for k in (d.get('keys') or [])][:300]
    conn = get_db()
    try:
        u = conn.execute('SELECT id, display_name, username FROM users WHERE id=? AND company_id=?', (uid, oid)).fetchone() if uid else None
        if uid and not u:
            return _err('User not found')
        scores = compute(conn, oid)
        n = 0
        for key in keys:
            p = _person(conn, oid, key)
            if not p:
                continue
            st = conn.execute('SELECT task_id FROM hotlist_state WHERE company_id=? AND person_key=?', (oid, key)).fetchone()
            if st and st['task_id']:
                _close_task(conn, st['task_id'])
            if not uid:                                   # un-assign
                _upsert(conn, oid, key, p['candidate_id'], status='open', assigned_to=0, task_id=0)
                _log(conn, oid, key, p['candidate_id'], 'unassign', '')
                n += 1
                continue
            sc = scores.get(p['email'], {})
            why = '; '.join(r['text'] for r in (sc.get('reasons') or [])[:3])
            tid = _task(conn, oid, uid, p['candidate_id'], 'Call %s — Hot List (score %s)' % (p['name'] or 'candidate', sc.get('score', 0)),
                        'Why: %s' % why, now_ist().strftime('%Y-%m-%dT%H:%M'))
            _upsert(conn, oid, key, p['candidate_id'], status='assigned', assigned_to=uid, assigned_by=int(real_user_id() or 0),
                    assigned_at=ts(), task_id=tid, outcome='', note='', callback_at='', snooze_until='')
            _log(conn, oid, key, p['candidate_id'], 'assign', 'to %s' % (u['display_name'] or u['username']))
            n += 1
        conn.commit()
        return jsonify({'ok': True, 'assigned': n})
    finally:
        conn.close()


@bp.route('/snooze', methods=['POST'])
@login_required
def snooze():
    e = _admin()
    if e:
        return e
    d = request.get_json(silent=True) or {}
    oid = _oid()
    days = max(1, min(180, int(d.get('days') or 30)))
    until = (now_ist() + datetime.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S')
    conn = get_db()
    try:
        n = 0
        for key in [str(k) for k in (d.get('keys') or [])][:300]:
            p = _person(conn, oid, key)
            if not p:
                continue
            _upsert(conn, oid, key, p['candidate_id'], status='snoozed', snooze_until=until)
            _log(conn, oid, key, p['candidate_id'], 'snooze', '%d days' % days)
            n += 1
        conn.commit()
        return jsonify({'ok': True, 'snoozed': n})
    finally:
        conn.close()


@bp.route('/outcome', methods=['POST'])
@login_required
def outcome_admin():
    e = _admin()
    if e:
        return e
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        ok, err = record_outcome(conn, _oid(), str(d.get('key') or ''), d.get('outcome'), d.get('note') or '', d.get('callback_at') or '')
        return jsonify({'ok': ok, 'error': err}) if ok else _err(err)
    finally:
        conn.close()


# ── callers: their own list (any logged-in user, incl. freelance accounts) ──
@bp.route('/mine', methods=['GET'])
@login_required
def mine():
    oid = _oid()
    uid = int(real_user_id() or 0)
    conn = get_db()
    try:
        nowiso = now_ist().strftime('%Y-%m-%dT%H:%M:%S')
        sts = _rows(conn, "SELECT * FROM hotlist_state WHERE company_id=? AND assigned_to=? AND (status='assigned' OR "
                          "(status='callback' AND callback_at<=?)) ORDER BY assigned_at", (oid, uid, nowiso[:16]))
        scores = compute(conn, oid)
        out = []
        for st in sts:
            p = _person(conn, oid, st['person_key'])
            if not p:
                continue
            c = conn.execute('SELECT phone FROM candidates WHERE id=?', (p['candidate_id'],)).fetchone()
            sc = scores.get(p['email'], {'score': 0, 'reasons': []})
            out.append({'key': st['person_key'], 'name': p['name'], 'phone': (c['phone'] if c else '') or '',
                        'designation': p['designation'], 'company': p['company'], 'experience': p['experience'],
                        'location': p['location'], 'score': sc['score'], 'reasons': sc['reasons'][:4],
                        'status': st['status'], 'callback_at': st['callback_at'], 'note': st['note']})
        done = conn.execute("SELECT COUNT(*) FROM hotlist_log WHERE company_id=? AND user_id=? AND action LIKE 'outcome:%' "
                            "AND created_at>=?", (oid, uid, now_ist().strftime('%Y-%m-%d'))).fetchone()[0]
        return jsonify({'ok': True, 'calls': out, 'done_today': done, 'outcomes': [{'key': a, 'label': b} for a, b in OUTCOMES]})
    finally:
        conn.close()


@bp.route('/mine/outcome', methods=['POST'])
@login_required
def mine_outcome():
    d = request.get_json(silent=True) or {}
    oid = _oid()
    uid = int(real_user_id() or 0)
    key = str(d.get('key') or '')
    conn = get_db()
    try:
        st = conn.execute('SELECT assigned_to FROM hotlist_state WHERE company_id=? AND person_key=?', (oid, key)).fetchone()
        if not st or int(st['assigned_to'] or 0) != uid:
            return _err('Not found', 404)
        ok, err = record_outcome(conn, oid, key, d.get('outcome'), d.get('note') or '', d.get('callback_at') or '')
        return jsonify({'ok': ok, 'error': err}) if ok else _err(err)
    finally:
        conn.close()


# ── score on the candidate profile ────────────────────────────────────────
@cand_bp.route('/api/candidates/<int:cid>/engagement', methods=['GET'])
@login_required
def candidate_engagement(cid):
    oid = _oid()
    conn = get_db()
    try:
        c = conn.execute('SELECT email FROM candidates WHERE id=? AND owner_id=?', (cid, oid)).fetchone()
        if not c:
            return _err('Not found', 404)
        em = str(c['email'] or '').strip().lower()
        sc = compute(conn, oid).get(em, {'score': 0, 'reasons': [], 'last': ''}) if em else {'score': 0, 'reasons': [], 'last': ''}
        return jsonify({'ok': True, 'score': sc['score'], 'reasons': sc['reasons'][:4], 'last': sc['last']})
    finally:
        conn.close()


def _page(name):
    core = _core()
    if not core.current_user():
        return redirect('/')
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)
    with open(p, encoding='utf-8') as f:
        return Response(f.read(), mimetype='text/html')


@pages.route('/hotlist')
def hotlist_page():
    return _page('hotlist.html')


@pages.route('/my-calls')
def my_calls_page():
    return _page('my-calls.html')
