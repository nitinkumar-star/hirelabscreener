"""
Meeting tasks + task alerts.

1. Every scheduler meeting becomes a task for its host
   - A booking (guest link or manual) creates a task on the host's Tasks page,
     at the meeting time, with the Google Meet link (Video) or the guest's
     number (Phone) on it.
   - A reschedule moves the task; a cancel or delete removes it.
   - On first deploy, upcoming confirmed meetings are copied in once.

2. Alerts 10 minutes before a timed task is due
   GET  /api/task-alerts/upcoming?within=30   my open, timed tasks due soon
   POST /api/task-alerts/<id>/claim {due_at}  "I am showing the alert now";
        only the first caller (ATS tab or the Assistant extension) gets ok=true,
        so the same alert never pops twice.

Additive only: three columns on `reminders` (meeting_id, join_url,
alert_sent_for). The scheduler calls sync_meeting_task(); nothing else changes.
"""

import datetime
import re
import sqlite3

from flask import Blueprint, request, jsonify

from modules.shared import get_db, ts, effective_company_id, real_user_id, login_required
from modules import register_migration

bp = Blueprint('meeting_tasks', __name__, url_prefix='/api/task-alerts')

IST = datetime.timedelta(hours=5, minutes=30)
_DT = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}')


@register_migration
def _migrate_meeting_tasks(conn):
    for col, defn in (('meeting_id', 'INTEGER DEFAULT 0'), ('join_url', "TEXT DEFAULT ''"),
                      ('alert_sent_for', "TEXT DEFAULT ''")):
        try:
            conn.execute('ALTER TABLE reminders ADD COLUMN %s %s' % (col, defn))
        except Exception:
            pass
    try:
        conn.execute('CREATE INDEX IF NOT EXISTS idx_rem_meeting ON reminders(meeting_id)')
    except Exception:
        pass
    conn.commit()
    # One-time copy of upcoming meetings (idempotent: skips meetings that already have a task).
    try:
        now = (datetime.datetime.utcnow() + IST).strftime('%Y-%m-%dT%H:%M')
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM meetings WHERE status='confirmed' AND start_at>=? ", (now,)).fetchall()]
        for mid in ids:
            sync_meeting_task(conn, mid)
        conn.commit()
    except Exception as e:
        print('[meeting_tasks] backfill skipped: %s' % e)


# ── meeting -> task ───────────────────────────────────────────────────────
def _rows(conn, sql, params=()):
    """Rows as dicts whatever the connection's row_factory is."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return [dict(r) for r in cur.execute(sql, params).fetchall()]


def _host_list(conn, company_id, host):
    """The host's 'Work' list if they have one, else their first list, else 0."""
    try:
        rows = _rows(conn, 'SELECT id, name FROM task_lists WHERE company_id=? AND created_by=? AND is_active=1 '
                           'ORDER BY sort_order, id', (company_id, host))
    except Exception:
        return 0
    for r in rows:
        if (r['name'] or '').strip().lower() == 'work':
            return r['id']
    return rows[0]['id'] if rows else 0


def _join_url(m):
    for v in (m.get('meet_link'), m.get('location')):
        v = str(v or '').strip()
        if v.startswith('http://') or v.startswith('https://'):
            return v[:500]
        if v.startswith('meet.google.com/'):
            return 'https://' + v[:480]
    return ''


def task_fields(m):
    """Title, notes, due and join link for one meeting row (dict)."""
    who = (m.get('guest_name') or 'Guest').strip()
    mode = (m.get('mode') or '').strip()
    purpose = (m.get('purpose') or '').strip()
    title = 'Meeting: %s' % who + (' — %s' % purpose.split('\n')[0][:80] if purpose else '') + (' (%s)' % mode if mode else '')
    lines = []
    if mode:
        lines.append('Mode: ' + mode)
    if m.get('guest_phone'):
        lines.append('Phone: ' + str(m['guest_phone']))
    if m.get('guest_email'):
        lines.append('Email: ' + str(m['guest_email']))
    link = _join_url(m)
    if link:
        lines.append('Join: ' + link)
    elif m.get('location') and mode == 'In-person':
        lines.append('Where: ' + str(m['location']))
    if purpose:
        lines.append('')
        lines.append(purpose[:2000])
    lines.append('')
    lines.append('Added automatically from your Scheduler.')
    start = str(m.get('start_at') or '')
    due = (start[:16] + ':00') if _DT.match(start) else ''
    return title[:500], '\n'.join(lines), due, link


def sync_meeting_task(conn, meeting_id):
    """Create / move / remove the host's task for this meeting. Never raises."""
    try:
        return _sync(conn, meeting_id)
    except Exception as e:
        print('[meeting_tasks] sync failed for meeting %s: %s' % (meeting_id, e))
        return {'state': 'error'}


def _sync(conn, meeting_id):
    ms = _rows(conn, 'SELECT * FROM meetings WHERE id=?', (meeting_id,))
    existing = _rows(conn, 'SELECT id FROM reminders WHERE meeting_id=?', (meeting_id,))
    m = ms[0] if ms else None
    if not m or (m.get('status') or '') == 'cancelled':
        for r in existing:
            try:
                conn.execute('DELETE FROM task_subtasks WHERE reminder_id=?', (r['id'],))
            except Exception:
                pass
            conn.execute('DELETE FROM reminders WHERE id=?', (r['id'],))
        conn.commit()
        return {'state': 'removed' if existing else 'none'}
    title, notes, due, link = task_fields(m)
    if not due:
        return {'state': 'skipped'}
    cand, cname = int(m.get('candidate_id') or 0), ''
    if cand:
        c = _rows(conn, 'SELECT name FROM candidates WHERE id=? AND owner_id=?', (cand, m['company_id']))
        cname, cand = (c[0]['name'] or '', cand) if c else ('', 0)
    if existing:
        rid = existing[0]['id']
        conn.execute('UPDATE reminders SET note=?, notes=?, due_at=?, join_url=?, candidate_id=?, candidate_name=?, '
                     'updated_at=? WHERE id=?', (title, notes, due, link, cand, cname, ts(), rid))
        conn.commit()
        return {'state': 'updated', 'task_id': rid}
    host = int(m.get('host_user_id') or 0)
    row = {'candidate_id': cand, 'mandate_id': None, 'candidate_name': cname, 'mandate_label': '',
           'note': title, 'notes': notes, 'due_at': due, 'done': 0, 'stage': 'todo', 'created_at': ts(),
           'owner_id': m['company_id'], 'created_by': host, 'assigned_to': host, 'updated_at': ts(),
           'list_id': _host_list(conn, m['company_id'], host), 'meeting_id': m['id'], 'join_url': link}
    cols = {r['name'] for r in _rows(conn, 'PRAGMA table_info(reminders)')}
    row = {k: v for k, v in row.items() if k in cols}
    cur = conn.execute('INSERT INTO reminders (%s) VALUES (%s)' % (','.join(row), ','.join('?' * len(row))),
                       list(row.values()))
    conn.commit()
    return {'state': 'created', 'task_id': cur.lastrowid}


# ── alerts ────────────────────────────────────────────────────────────────
def due_epoch_ms(due):
    """'YYYY-MM-DDTHH:MM[:SS]' in IST -> epoch milliseconds (None for date-only)."""
    due = str(due or '')
    if not _DT.match(due):
        return None
    try:
        d = datetime.datetime.strptime(due[:16], '%Y-%m-%dT%H:%M')
    except ValueError:
        return None
    return int(((d - IST) - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)


@bp.route('/upcoming', methods=['GET'])
@login_required
def upcoming():
    try:
        within = max(1, min(240, int(request.args.get('within', 30))))
    except ValueError:
        within = 30
    now_ist = datetime.datetime.utcnow() + IST
    lo = (now_ist - datetime.timedelta(minutes=2)).strftime('%Y-%m-%dT%H:%M')
    hi = (now_ist + datetime.timedelta(minutes=within)).strftime('%Y-%m-%dT%H:%M:59')
    uid, cid = real_user_id(), effective_company_id()
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT r.id, r.note, r.notes, r.due_at, r.candidate_id, r.candidate_name, "
            "COALESCE(r.join_url,'') AS join_url, COALESCE(r.meeting_id,0) AS meeting_id, "
            "COALESCE(r.alert_sent_for,'') AS alert_sent_for, c.phone AS cand_phone "
            "FROM reminders r LEFT JOIN candidates c ON c.id=r.candidate_id "
            "WHERE r.owner_id=? AND COALESCE(r.done,0)=0 AND length(r.due_at)>10 AND r.due_at>=? AND r.due_at<=? "
            "AND (COALESCE(r.assigned_to,0)=? OR (COALESCE(r.assigned_to,0)=0 AND COALESCE(r.created_by,0)=?)) "
            "ORDER BY r.due_at LIMIT 50", (cid, lo, hi, uid, uid)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        out.append({'id': r['id'], 'title': (r['note'] or r['candidate_name'] or 'Task').strip(),
                    'due_at': r['due_at'], 'due_ms': due_epoch_ms(r['due_at']),
                    'join_url': r['join_url'], 'meeting_id': r['meeting_id'],
                    'candidate_name': r['candidate_name'] or '', 'phone': r['cand_phone'] or '',
                    'alerted': r['alert_sent_for'] == r['due_at']})
    now_ms = int((datetime.datetime.utcnow() - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)
    return jsonify({'ok': True, 'tasks': out, 'now_ms': now_ms})


@bp.route('/<int:rid>/claim', methods=['POST'])
@login_required
def claim(rid):
    """Atomically mark this task's alert as shown for its current due time."""
    d = request.get_json(silent=True) or {}
    due = str(d.get('due_at') or '')[:25]
    uid, cid = real_user_id(), effective_company_id()
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE reminders SET alert_sent_for=? WHERE id=? AND owner_id=? AND due_at=? "
            "AND COALESCE(alert_sent_for,'')<>? "
            "AND (COALESCE(assigned_to,0)=? OR (COALESCE(assigned_to,0)=0 AND COALESCE(created_by,0)=?))",
            (due, rid, cid, due, due, uid, uid))
        conn.commit()
        return jsonify({'ok': cur.rowcount == 1})
    finally:
        conn.close()
