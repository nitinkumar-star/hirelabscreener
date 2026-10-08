"""
RecruitOS — To-Do engine  (Tasks v2, Wave 1: backend)

An Any.do-style task manager built ON TOP of the existing `reminders` table,
not beside it. `reminders` already feeds the Tasks page (list + Kanban board),
the mobile push scheduler, the MCP connector (create_task / create_reminder),
the calendar module, the recruiter access guard and the candidate timeline.
Keeping one table means every one of those keeps seeing every task.

Everything is additive:
  * new columns on reminders : notes, list_id, priority, tags, my_day_date,
                               pinned, sort_order, assigned_to, recurrence,
                               completed_at, updated_at
  * new tables               : task_lists, task_subtasks, task_tag_defs,
                               todo_prefs
  * new routes               : /api/todo/...
Nothing existing is renamed, dropped or rewritten. Legacy routes
(/api/reminders, /api/tasks, /api/tasks/board) are untouched.

Field mapping
  title        -> reminders.note          (the task text, as before)
  notes        -> reminders.notes         (long description, new)
  due_at       -> reminders.due_at        ('' = Someday, 'YYYY-MM-DD' = date
                                           only, 'YYYY-MM-DDTHH:MM:SS' = timed)
  done/stage   -> reminders.done / stage  (kept in step with the Kanban board)

Visibility (same rules as the rest of the ATS, plus assignment)
  * Always limited to the caller's company (reminders.owner_id).
  * A scoped recruiter sees tasks on candidates they may see, standalone tasks
    they created, and tasks assigned to them (access.reminder_scope_sql).
  * scope=mine (default) narrows further to tasks I created or that are
    assigned to me (admins also see legacy rows with no creator recorded).
  * scope=team (admins) shows every task in the company.

SQL safety: column names never come from the client. All values are bound.
"""

import re
import json
import datetime
import calendar as _cal
from flask import Blueprint, request, jsonify

from modules.shared import (
    get_db, ts, effective_company_id, real_user_id, is_company_admin,
    login_required, _core,
)
from modules import register_migration

bp = Blueprint('todo', __name__, url_prefix='/api/todo')


# ══════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ══════════════════════════════════════════════════════════════════════════
PRIORITIES = ('none', 'low', 'medium', 'high')
RECUR_FREQS = ('daily', 'weekly', 'monthly', 'yearly')
GROUP_ORDER = ('overdue', 'today', 'tomorrow', 'upcoming', 'someday')
LIST_COLORS = ('#13A37E', '#185FA5', '#D97A4D', '#7C5CC4', '#C0392B', '#B7950B', '#5D6D7E', '#E84393')
DEFAULT_LISTS = (('Personal', '#185FA5'), ('Work', '#13A37E'))

MAX_TITLE = 500
MAX_NOTES = 20000
MAX_TAGS = 20
MAX_TAG_LEN = 40
MAX_LIST_NAME = 60
MAX_BULK = 500

_DATE_RX = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_DT_RX = re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?$')

# reminders columns added by this module: (name, definition)
_REMINDER_COLS = [
    ('notes', "TEXT DEFAULT ''"),
    ('list_id', 'INTEGER DEFAULT 0'),
    ('priority', "TEXT DEFAULT 'none'"),
    ('tags', "TEXT DEFAULT '[]'"),
    ('my_day_date', "TEXT DEFAULT ''"),
    ('pinned', 'INTEGER DEFAULT 0'),
    ('sort_order', 'REAL DEFAULT 0'),
    ('assigned_to', 'INTEGER DEFAULT 0'),
    ('recurrence', "TEXT DEFAULT ''"),
    ('completed_at', "TEXT DEFAULT ''"),
    ('updated_at', "TEXT DEFAULT ''"),
    ('auto_rule', "TEXT DEFAULT ''"),       # Wave 4: set on tasks made by an auto-task rule
]


# ══════════════════════════════════════════════════════════════════════════
#  MIGRATION  (additive, idempotent)
# ══════════════════════════════════════════════════════════════════════════
@register_migration
def _migrate_todo(conn):
    c = conn.cursor()
    # Columns the legacy code may not have created yet on very old DBs —
    # every one of these is also added by server.py / access.py; repeating
    # the ALTER is harmless and keeps this module self-sufficient.
    for col, defn in [('owner_id', 'INTEGER DEFAULT 0'), ('created_by', 'INTEGER DEFAULT 0'),
                      ('stage', "TEXT DEFAULT 'todo'")] + _REMINDER_COLS:
        try:
            c.execute(f'ALTER TABLE reminders ADD COLUMN {col} {defn}')
        except Exception:
            pass

    c.execute('''CREATE TABLE IF NOT EXISTS task_lists (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER DEFAULT 0,
        created_by INTEGER DEFAULT 0,
        name TEXT DEFAULT '',
        color TEXT DEFAULT '',
        icon TEXT DEFAULT '',
        is_shared INTEGER DEFAULT 0,
        sort_order REAL DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS task_subtasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reminder_id INTEGER NOT NULL,
        company_id INTEGER DEFAULT 0,
        text TEXT DEFAULT '',
        done INTEGER DEFAULT 0,
        sort_order REAL DEFAULT 0,
        created_by INTEGER DEFAULT 0,
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS task_tag_defs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER DEFAULT 0,
        name TEXT DEFAULT '',
        color TEXT DEFAULT '',
        created_by INTEGER DEFAULT 0,
        created_at TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS todo_auto_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER DEFAULT 0,
        stage TEXT DEFAULT '',
        title TEXT DEFAULT '',
        due_days INTEGER DEFAULT 0,
        due_time TEXT DEFAULT '',
        priority TEXT DEFAULT 'none',
        assign_to INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        is_deleted INTEGER DEFAULT 0,
        created_by INTEGER DEFAULT 0,
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS todo_auto_state (
        key TEXT PRIMARY KEY,
        value TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS todo_prefs (
        user_id INTEGER PRIMARY KEY,
        company_id INTEGER DEFAULT 0,
        prefs TEXT DEFAULT '{}',
        updated_at TEXT DEFAULT ''
    )''')
    for sql in (
        'CREATE INDEX IF NOT EXISTS idx_task_lists_co ON task_lists(company_id, is_active)',
        'CREATE INDEX IF NOT EXISTS idx_task_subtasks_rem ON task_subtasks(reminder_id)',
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_task_tag_defs_name ON task_tag_defs(company_id, name)',
        'CREATE INDEX IF NOT EXISTS idx_reminders_owner_done ON reminders(owner_id, done)',
        'CREATE INDEX IF NOT EXISTS idx_reminders_assigned ON reminders(assigned_to)',
        'CREATE INDEX IF NOT EXISTS idx_reminders_auto ON reminders(candidate_id, auto_rule, done)',
        'CREATE INDEX IF NOT EXISTS idx_todo_rules_co ON todo_auto_rules(company_id)',
    ):
        try:
            c.execute(sql)
        except Exception:
            pass
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════
def _now():
    return _core()._ist_now()


def _today():
    return _now().date()


def _uid():
    return int(real_user_id() or 0)


def _cid():
    return int(effective_company_id() or 0)


def _scoped():
    from modules.access import scoped_user
    return scoped_user()


def _rem_scope(alias='r'):
    from modules.access import reminder_scope_sql
    return reminder_scope_sql(alias)


def _err(msg, code=400):
    return jsonify({'ok': False, 'error': msg}), code


def _body():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def _bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _clean_text(v, limit):
    return str(v if v is not None else '').strip()[:limit]


def parse_due(due):
    """Normalise a due value. Returns (stored_string, error).
    '' = Someday; 'YYYY-MM-DD' = all-day; 'YYYY-MM-DDTHH:MM:SS' = timed."""
    s = str(due or '').strip()
    if not s:
        return '', None
    if _DATE_RX.match(s):
        try:
            datetime.date.fromisoformat(s)
        except ValueError:
            return None, 'invalid due date'
        return s, None
    s19 = s.replace(' ', 'T')[:19]
    if _DT_RX.match(s19):
        if len(s19) == 16:
            s19 += ':00'
        try:
            datetime.datetime.fromisoformat(s19)
        except ValueError:
            return None, 'invalid due date/time'
        return s19, None
    return None, 'due_at must be YYYY-MM-DD or YYYY-MM-DDTHH:MM'


def due_date_of(due):
    """The calendar date of a stored due value, or None for Someday."""
    if not due:
        return None
    try:
        return datetime.date.fromisoformat(str(due)[:10])
    except ValueError:
        return None


def group_for(due, now=None):
    """overdue / today / tomorrow / upcoming / someday (IST)."""
    # A timed task that passed earlier today stays in Today (flagged 'late'),
    # the way Any.do keeps it; only earlier days are Overdue.
    now = now or _now()
    today = now.date()
    d = due_date_of(due)
    if d is None:
        return 'someday'
    if d < today:
        return 'overdue'
    if d == today:
        return 'today'
    if d == today + datetime.timedelta(days=1):
        return 'tomorrow'
    return 'upcoming'


def _is_overdue_now(due, now=None):
    """True if the task's moment has passed (used for the red 'late' flag)."""
    now = now or _now()
    d = due_date_of(due)
    if d is None:
        return False
    if len(str(due)) > 10:
        try:
            return datetime.datetime.fromisoformat(str(due)[:19]) < now
        except ValueError:
            return False
    return d < now.date()


# ── Tags ─────────────────────────────────────────────────────────────────
def _norm_tags(v):
    if isinstance(v, str):
        try:
            v = json.loads(v) if v.strip().startswith('[') else [x for x in v.split(',')]
        except Exception:
            v = [x for x in v.split(',')]
    if not isinstance(v, list):
        return []
    out, seen = [], set()
    for t in v:
        t = str(t or '').strip().lstrip('#')[:MAX_TAG_LEN]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
        if len(out) >= MAX_TAGS:
            break
    return out


def _load_tags(raw):
    try:
        v = json.loads(raw or '[]')
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _ensure_tag_defs(conn, cid, tags):
    for i, t in enumerate(tags):
        try:
            conn.execute('INSERT OR IGNORE INTO task_tag_defs (company_id, name, color, created_by, created_at) '
                         'VALUES (?,?,?,?,?)', (cid, t, LIST_COLORS[(len(t) + i) % len(LIST_COLORS)], _uid(), ts()))
        except Exception:
            pass


# ── Recurrence ───────────────────────────────────────────────────────────
def parse_recurrence(v):
    """Validate a recurrence rule. Returns (json_string_or_'', error).
    Rule: {freq: daily|weekly|monthly|yearly, interval: 1..365,
           weekdays: [0..6] (Mon=0, weekly only), until: 'YYYY-MM-DD' | ''}"""
    if v in (None, '', {}, 'none'):
        return '', None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return None, 'recurrence must be an object'
    if not isinstance(v, dict):
        return None, 'recurrence must be an object'
    freq = str(v.get('freq') or '').lower()
    if freq not in RECUR_FREQS:
        return None, 'recurrence.freq must be daily, weekly, monthly or yearly'
    try:
        interval = int(v.get('interval') or 1)
    except Exception:
        return None, 'recurrence.interval must be a number'
    if interval < 1 or interval > 365:
        return None, 'recurrence.interval must be 1-365'
    rule = {'freq': freq, 'interval': interval}
    if freq == 'weekly':
        wd = v.get('weekdays') or []
        if not isinstance(wd, list):
            return None, 'recurrence.weekdays must be a list'
        try:
            wd = sorted({int(x) for x in wd})
        except Exception:
            return None, 'recurrence.weekdays must be numbers 0-6'
        if any(x < 0 or x > 6 for x in wd):
            return None, 'recurrence.weekdays must be numbers 0-6 (Mon=0)'
        if wd:
            rule['weekdays'] = wd
    until = str(v.get('until') or '').strip()
    if until:
        if not _DATE_RX.match(until):
            return None, 'recurrence.until must be YYYY-MM-DD'
        rule['until'] = until
    return json.dumps(rule, separators=(',', ':')), None


def _add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, _cal.monthrange(y, m)[1]))


def next_occurrence(rule_json, due, today=None):
    """Next due value after completing an occurrence. Keeps the time of day.
    Never returns a date in the past: if the task was done late, it jumps
    forward to the first occurrence after today. None = series finished."""
    try:
        rule = json.loads(rule_json) if isinstance(rule_json, str) else dict(rule_json or {})
    except Exception:
        return None
    freq = rule.get('freq')
    if freq not in RECUR_FREQS:
        return None
    interval = max(1, int(rule.get('interval') or 1))
    today = today or _today()
    base = due_date_of(due) or today
    time_part = str(due)[10:] if due and len(str(due)) > 10 else ''

    def step(d):
        if freq == 'daily':
            return d + datetime.timedelta(days=interval)
        if freq == 'weekly':
            wds = rule.get('weekdays') or []
            if not wds:
                return d + datetime.timedelta(weeks=interval)
            later = [w for w in wds if w > d.weekday()]
            if later:
                return d + datetime.timedelta(days=later[0] - d.weekday())
            # wrap to the first chosen weekday of the next cycle
            start_of_week = d - datetime.timedelta(days=d.weekday())
            return start_of_week + datetime.timedelta(weeks=interval, days=wds[0])
        if freq == 'monthly':
            return _add_months(d, interval)
        return _add_months(d, 12 * interval)

    nxt = step(base)
    guard = 0
    while nxt <= today and guard < 2000:          # done late: skip past occurrences
        nxt = step(nxt)
        guard += 1
    until = rule.get('until')
    if until:
        try:
            if nxt > datetime.date.fromisoformat(until):
                return None
        except ValueError:
            pass
    return nxt.isoformat() + time_part


# ── Users / lists ────────────────────────────────────────────────────────
def _company_user_ids(conn, cid):
    return {int(r['id']) for r in conn.execute(
        "SELECT id FROM users WHERE company_id=? AND COALESCE(status,'approved')='approved'", (cid,))}


def _visible_lists(conn, cid):
    """Lists the caller may see: admins see every list in the company;
    recruiters see their own lists plus shared ones."""
    if _scoped():
        return conn.execute('SELECT * FROM task_lists WHERE company_id=? AND is_active=1 '
                            'AND (created_by=? OR is_shared=1) ORDER BY sort_order, id',
                            (cid, _uid())).fetchall()
    return conn.execute('SELECT * FROM task_lists WHERE company_id=? AND is_active=1 '
                        'ORDER BY sort_order, id', (cid,)).fetchall()


def _list_ok(conn, cid, list_id, write=False):
    """May the caller put tasks in / edit this list?"""
    if not list_id:
        return True
    r = conn.execute('SELECT created_by, is_shared FROM task_lists WHERE id=? AND company_id=? AND is_active=1',
                     (list_id, cid)).fetchone()
    if not r:
        return False
    if int(r['created_by'] or 0) == _uid():
        return True
    if write == 'manage':                         # rename / delete / share
        return is_company_admin() and not _scoped()
    return bool(r['is_shared']) or not _scoped()


def _ensure_default_lists(conn, cid, uid):
    if not uid:
        return
    n = conn.execute('SELECT COUNT(*) FROM task_lists WHERE company_id=? AND created_by=?',
                     (cid, uid)).fetchone()[0]
    if n:
        return
    for i, (name, color) in enumerate(DEFAULT_LISTS):
        conn.execute('INSERT INTO task_lists (company_id, created_by, name, color, is_shared, sort_order, '
                     'is_active, created_at, updated_at) VALUES (?,?,?,?,0,?,1,?,?)',
                     (cid, uid, name, color, i + 1, ts(), ts()))
    conn.commit()


# ── Task access ──────────────────────────────────────────────────────────
def _task_where(scope='mine', alias='r'):
    """WHERE fragment + params limiting reminders to what the caller may see."""
    cid, uid = _cid(), _uid()
    sql = f' {alias}.owner_id=? '
    params = [cid]
    rs, rp = _rem_scope(alias)
    sql += rs
    params += rp
    if scope != 'team' or _scoped():
        if is_company_admin() and not _scoped():
            # admins: mine + legacy rows that never recorded a creator
            sql += (f' AND (COALESCE({alias}.created_by,0) IN (?,0) OR COALESCE({alias}.assigned_to,0)=?) ')
        else:
            sql += (f' AND (COALESCE({alias}.created_by,0)=? OR COALESCE({alias}.assigned_to,0)=? '
                    f' OR COALESCE({alias}.candidate_id,0)!=0) ')
        params += [uid, uid]
    return sql, params


def _get_task_row(conn, rid):
    """The reminder row if the caller may see it (company + recruiter scope), else None."""
    rs, rp = _rem_scope('r')
    return conn.execute('SELECT r.* FROM reminders r WHERE r.id=? AND r.owner_id=? ' + rs,
                        [rid, _cid()] + rp).fetchone()


def _users_map(conn, cid):
    return {int(r['id']): (r['display_name'] or r['username'] or '')
            for r in conn.execute('SELECT id, display_name, username FROM users WHERE company_id=?', (cid,))}


def _serialize(r, lists_by_id=None, users=None, subtask_counts=None, now=None):
    keys = r.keys()
    g = lambda k, d=None: (r[k] if k in keys else d)
    due = g('due_at', '') or ''
    done = bool(g('done', 0))
    list_id = int(g('list_id', 0) or 0)
    lst = (lists_by_id or {}).get(list_id)
    assigned = int(g('assigned_to', 0) or 0)
    sc = (subtask_counts or {}).get(int(r['id']), (0, 0))
    rec = g('recurrence', '') or ''
    try:
        rec_obj = json.loads(rec) if rec else None
    except Exception:
        rec_obj = None
    title = (g('note', '') or '').strip()
    return {
        'id': r['id'],
        'title': title or (g('candidate_name', '') or 'Task'),
        'notes': g('notes', '') or '',
        'due_at': due,
        'all_day': bool(due) and len(due) == 10,
        'group': 'done' if done else group_for(due, now),
        'late': (not done) and _is_overdue_now(due, now),
        'done': done,
        'stage': 'done' if done else (g('stage', 'todo') or 'todo'),
        'completed_at': g('completed_at', '') or '',
        'priority': g('priority', 'none') or 'none',
        'tags': _load_tags(g('tags', '[]')),
        'list_id': list_id,
        'list_name': (lst['name'] if lst else ''),
        'list_color': (lst['color'] if lst else ''),
        'my_day': (g('my_day_date', '') or '') == (now or _now()).date().isoformat(),
        'pinned': bool(g('pinned', 0)),
        'sort_order': g('sort_order', 0) or 0,
        'assigned_to': assigned,
        'assigned_name': (users or {}).get(assigned, '') if assigned else '',
        'created_by': int(g('created_by', 0) or 0),
        'created_by_name': (users or {}).get(int(g('created_by', 0) or 0), ''),
        'recurrence': rec_obj,
        'candidate_id': int(g('candidate_id', 0) or 0),
        'candidate_name': g('candidate_name', '') or '',
        'candidate_phone': g('cand_phone', '') or '',
        'mandate_id': g('mandate_id'),
        'mandate_label': g('mandate_label', '') or '',
        'subtasks_total': sc[0],
        'subtasks_done': sc[1],
        'created_at': g('created_at', '') or '',
        'updated_at': g('updated_at', '') or '',
    }


def _subtask_counts(conn, ids):
    out = {}
    ids = [int(i) for i in ids]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        if not chunk:
            continue
        q = ','.join('?' * len(chunk))
        for row in conn.execute(f'SELECT reminder_id, COUNT(*) n, SUM(CASE WHEN done=1 THEN 1 ELSE 0 END) d '
                                f'FROM task_subtasks WHERE reminder_id IN ({q}) GROUP BY reminder_id', chunk):
            out[int(row['reminder_id'])] = (int(row['n'] or 0), int(row['d'] or 0))
    return out


def _serialize_many(conn, rows, now=None):
    cid = _cid()
    lists_by_id = {int(l['id']): l for l in conn.execute('SELECT id, name, color FROM task_lists WHERE company_id=?',
                                                        (cid,))}
    users = _users_map(conn, cid)
    counts = _subtask_counts(conn, [r['id'] for r in rows])
    now = now or _now()
    return [_serialize(r, lists_by_id, users, counts, now) for r in rows]


def _link_candidate(conn, cid, cand_id):
    """Returns (candidate_id, candidate_name, mandate_id, mandate_label) or None if not in tenant."""
    cand = conn.execute('SELECT id, name, mandate_id FROM candidates WHERE id=? AND owner_id=?',
                        (cand_id, cid)).fetchone()
    if not cand:
        return None
    m = conn.execute('SELECT role, client FROM mandates WHERE id=?', (cand['mandate_id'],)).fetchone()
    label = ((m['role'] or '') + ' — ' + (m['client'] or '')) if m else ''
    return cand['id'], cand['name'] or '', cand['mandate_id'], label


def _link_mandate(conn, cid, mid):
    m = conn.execute('SELECT id, role, client FROM mandates WHERE id=? AND owner_id=?', (mid, cid)).fetchone()
    if not m:
        return None
    return m['id'], (m['role'] or '') + ' — ' + (m['client'] or '')


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _apply_fields(conn, d, creating=False, existing=None):
    """Validate the editable fields in `d`. Returns (sets: dict, error)."""
    cid = _cid()
    sets = {}
    if 'title' in d or 'note' in d:
        sets['note'] = _clean_text(d.get('title', d.get('note')), MAX_TITLE)
    if 'notes' in d:
        sets['notes'] = _clean_text(d.get('notes'), MAX_NOTES)
    if 'due_at' in d:
        due, e = parse_due(d.get('due_at'))
        if e:
            return None, e
        sets['due_at'] = due
    if 'priority' in d:
        p = str(d.get('priority') or 'none').lower()
        if p not in PRIORITIES:
            return None, 'priority must be none, low, medium or high'
        sets['priority'] = p
    if 'tags' in d:
        tags = _norm_tags(d.get('tags'))
        sets['tags'] = json.dumps(tags)
        _ensure_tag_defs(conn, cid, tags)
    if 'list_id' in d:
        lid = _int_or_none(d.get('list_id')) or 0
        if lid and not _list_ok(conn, cid, lid):
            return None, 'list not found'
        sets['list_id'] = lid
    if 'pinned' in d:
        sets['pinned'] = 1 if _bool(d.get('pinned')) else 0
    if 'my_day' in d:
        sets['my_day_date'] = _today().isoformat() if _bool(d.get('my_day')) else ''
    if 'sort_order' in d:
        try:
            sets['sort_order'] = float(d.get('sort_order') or 0)
        except (TypeError, ValueError):
            return None, 'sort_order must be a number'
    if 'assigned_to' in d:
        a = _int_or_none(d.get('assigned_to')) or 0
        if a and a not in _company_user_ids(conn, cid):
            return None, 'assignee must be a user in your company'
        sets['assigned_to'] = a
    if 'recurrence' in d:
        rec, e = parse_recurrence(d.get('recurrence'))
        if e:
            return None, e
        sets['recurrence'] = rec
    # Linking: a candidate brings its own mandate. A mandate on its own may be
    # linked only when the task has no candidate (else it follows the candidate).
    cand_linked = False
    if 'candidate_id' in d:
        cand_id = _int_or_none(d.get('candidate_id')) or 0
        if cand_id:
            link = _link_candidate(conn, cid, cand_id)
            if not link:
                return None, 'candidate not found'
            sets['candidate_id'], sets['candidate_name'], sets['mandate_id'], sets['mandate_label'] = link
            cand_linked = True
        else:
            sets['candidate_id'], sets['candidate_name'] = 0, ''
            sets['mandate_id'], sets['mandate_label'] = None, ''
    has_existing_cand = (existing is not None and int(existing['candidate_id'] or 0) != 0
                         and 'candidate_id' not in d)
    if 'mandate_id' in d and not cand_linked and not has_existing_cand:
        mid = _int_or_none(d.get('mandate_id')) or 0
        if mid:
            link = _link_mandate(conn, cid, mid)
            if not link:
                return None, 'mandate not found'
            sets['mandate_id'], sets['mandate_label'] = link
        else:
            sets['mandate_id'], sets['mandate_label'] = None, ''
    if 'stage' in d:
        st = str(d.get('stage') or '').lower()
        if st not in ('todo', 'doing', 'done'):
            return None, 'stage must be todo, doing or done'
        sets['stage'] = st
        sets['done'] = 1 if st == 'done' else 0
        sets['completed_at'] = ts() if st == 'done' else ''
    if 'due_at' in sets and not creating:
        # a new time means the push notifier should fire fresh
        sets.update({'notified_at': '', 'early_warned': 0, 'snoozed_until': '', 'notify_count': 0})
    return sets, None


def _reminder_columns(conn):
    return {r[1] for r in conn.execute('PRAGMA table_info(reminders)')}


def _update_row(conn, rid, sets):
    cols = _reminder_columns(conn)
    sets = {k: v for k, v in sets.items() if k in cols}    # never trust keys blindly
    if not sets:
        return
    sets['updated_at'] = ts()
    assign = ', '.join(f'{k}=?' for k in sets)
    conn.execute(f'UPDATE reminders SET {assign} WHERE id=? AND owner_id=?', list(sets.values()) + [rid, _cid()])


def complete_reminder(conn, rid, owner_id, done=True):
    """THE single place a task is marked done / undone. Session-free, so the
    mobile app (token auth), the legacy Tasks list, the Kanban board and the
    new Tasks page all behave the same. A recurring task rolls forward to its
    next occurrence instead of closing. Caller commits. Returns a dict."""
    row = conn.execute('SELECT * FROM reminders WHERE id=? AND owner_id=?', (rid, owner_id)).fetchone()
    if not row:
        return {'ok': False}
    cols = _reminder_columns(conn)

    def upd(sets):
        sets = {k: v for k, v in sets.items() if k in cols}
        sets['updated_at'] = ts()
        conn.execute('UPDATE reminders SET ' + ', '.join(f'{k}=?' for k in sets) + ' WHERE id=? AND owner_id=?',
                     list(sets.values()) + [rid, owner_id])

    if not done:
        upd({'done': 0, 'stage': 'todo', 'completed_at': ''})
        return {'ok': True, 'rolled': False}
    rec = row['recurrence'] if 'recurrence' in row.keys() else ''
    if rec:
        nxt = next_occurrence(rec, row['due_at'] or '')
        if nxt:
            upd({'due_at': nxt, 'done': 0, 'stage': 'todo', 'completed_at': ts(),
                 'my_day_date': '', 'notified_at': '', 'early_warned': 0,
                 'snoozed_until': '', 'notify_count': 0})
            conn.execute('UPDATE task_subtasks SET done=0, updated_at=? WHERE reminder_id=?', (ts(), rid))
            return {'ok': True, 'rolled': True, 'next_due': nxt}
    upd({'done': 1, 'stage': 'done', 'completed_at': ts()})
    return {'ok': True, 'rolled': False}


def push_targets(row):
    """Which user(s) a task's phone reminder goes to. Phones register under
    the real user id, while reminders.owner_id holds the COMPANY id — so the
    old loop only reached a phone when those two ids happened to match.
    Assignee first, else the creator; legacy rows with neither keep the old
    behaviour (owner id) so nothing that works today stops working."""
    keys = row.keys()
    a = int((row['assigned_to'] if 'assigned_to' in keys else 0) or 0)
    c = int((row['created_by'] if 'created_by' in keys else 0) or 0)
    if a:
        return [a]
    if c:
        return [c]
    o = int((row['owner_id'] if 'owner_id' in keys else 0) or 0)
    return [o] if o else []


def _complete(conn, row, done):
    """Mark done / undone for the caller's company (see complete_reminder)."""
    res = complete_reminder(conn, row['id'], _cid(), done)
    res.pop('ok', None)
    return res


# ══════════════════════════════════════════════════════════════════════════
#  META  (sidebar: lists, tags, counts, team, prefs)
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/meta', methods=['GET'])
@login_required
def todo_meta():
    cid, uid = _cid(), _uid()
    conn = get_db()
    _ensure_default_lists(conn, cid, uid)
    scope = 'team' if request.args.get('scope') == 'team' else 'mine'
    where, params = _task_where(scope)
    rows = conn.execute('SELECT r.id, r.due_at, r.list_id, r.tags, r.my_day_date FROM reminders r '
                        'WHERE r.done=0 AND ' + where, params).fetchall()
    now = _now()
    today = now.date()
    today_s = today.isoformat()
    wk = today + datetime.timedelta(days=7)
    counts = {'my_day': 0, 'next7': 0, 'all': len(rows), 'overdue': 0, 'today': 0, 'someday': 0,
              'lists': {}, 'tags': {}, 'inbox': 0}
    for r in rows:
        dd = due_date_of(r['due_at'])
        grp = group_for(r['due_at'], now)
        if (r['my_day_date'] or '') == today_s:
            counts['my_day'] += 1
        if dd is not None and dd <= wk:
            counts['next7'] += 1
        if grp == 'overdue':
            counts['overdue'] += 1
        elif grp == 'today':
            counts['today'] += 1
        elif grp == 'someday':
            counts['someday'] += 1
        lid = int(r['list_id'] or 0)
        if lid:
            counts['lists'][str(lid)] = counts['lists'].get(str(lid), 0) + 1
        else:
            counts['inbox'] += 1
        for t in _load_tags(r['tags']):
            counts['tags'][t] = counts['tags'].get(t, 0) + 1
    users = _users_map(conn, cid)
    lists = [{'id': l['id'], 'name': l['name'], 'color': l['color'], 'icon': l['icon'],
              'is_shared': bool(l['is_shared']), 'mine': int(l['created_by'] or 0) == uid,
              'owner_name': users.get(int(l['created_by'] or 0), ''), 'sort_order': l['sort_order']}
             for l in _visible_lists(conn, cid)]
    tags = [{'id': t['id'], 'name': t['name'], 'color': t['color']}
            for t in conn.execute('SELECT * FROM task_tag_defs WHERE company_id=? ORDER BY name COLLATE NOCASE',
                                  (cid,))]
    active_ids = _company_user_ids(conn, cid)
    team = [{'id': i, 'name': n} for i, n in sorted(users.items(), key=lambda x: x[1].lower())
            if i in active_ids]
    pr = conn.execute('SELECT prefs FROM todo_prefs WHERE user_id=?', (uid,)).fetchone()
    conn.close()
    try:
        prefs = json.loads(pr['prefs']) if pr else {}
    except Exception:
        prefs = {}
    return jsonify({'ok': True, 'lists': lists, 'tags': tags, 'counts': counts, 'team': team,
                    'me': uid, 'is_admin': bool(is_company_admin() and not _scoped()),
                    'today': today_s, 'prefs': prefs})


# ══════════════════════════════════════════════════════════════════════════
#  TASK LIST (views)
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/tasks', methods=['GET'])
@login_required
def todo_tasks():
    """view = my_day | next7 | all | list | tag | inbox | completed | assigned | candidate
    Optional: list_id, tag, assigned_to, candidate_id, mandate_id, q, include_done=1, scope=mine|team."""
    view = (request.args.get('view') or 'all').lower()
    scope = 'team' if request.args.get('scope') == 'team' else 'mine'
    include_done = _bool(request.args.get('include_done'))
    q = (request.args.get('q') or '').strip()[:100]
    where, params = _task_where(scope)
    now = _now()
    today = now.date()
    extra = ''
    if view == 'completed':
        extra += ' AND r.done=1 '
    elif not include_done:
        extra += ' AND r.done=0 '
    if view == 'my_day':
        # open My Day tasks, plus ones finished today so the day's progress shows
        extra = (' AND r.my_day_date=? ' if include_done else ' AND r.my_day_date=? AND r.done=0 ')
        params = params + [today.isoformat()]
    elif view == 'next7':
        extra += " AND r.due_at!='' AND substr(r.due_at,1,10)<=? "
        params = params + [(today + datetime.timedelta(days=7)).isoformat()]
    elif view == 'list':
        lid = _int_or_none(request.args.get('list_id')) or 0
        extra += ' AND r.list_id=? '
        params = params + [lid]
    elif view == 'inbox':
        extra += ' AND COALESCE(r.list_id,0)=0 '
    elif view == 'tag':
        extra += " AND r.tags IS NOT NULL AND r.tags NOT IN ('', '[]') "   # exact match below
    elif view == 'range':                               # calendar: due dates in [from, to]
        frm = (request.args.get('from') or '')[:10]
        to = (request.args.get('to') or '')[:10]
        if not (_DATE_RX.match(frm) and _DATE_RX.match(to)):
            return _err('from and to (YYYY-MM-DD) required')
        if (datetime.date.fromisoformat(to) - datetime.date.fromisoformat(frm)).days > 62:
            return _err('range too long (max 62 days)')
        extra += " AND r.due_at!='' AND substr(r.due_at,1,10) BETWEEN ? AND ? "
        params = params + [frm, to]
    elif view == 'person':                              # admin: one teammate's tasks
        if _scoped() or not is_company_admin():
            return _err('Only an admin can open a teammate\'s tasks.', 403)
        pid = _int_or_none(request.args.get('user_id')) or 0
        where, params = _task_where('team')
        extra = extra + (' AND (COALESCE(r.assigned_to,0)=? OR (COALESCE(r.assigned_to,0)=0 AND COALESCE(r.created_by,0)=?)) ')
        params = params + [pid, pid]
    elif view == 'assigned':
        aid = _int_or_none(request.args.get('assigned_to')) or _uid()
        extra += ' AND COALESCE(r.assigned_to,0)=? '
        params = params + [aid]
    cand = _int_or_none(request.args.get('candidate_id'))
    if cand:
        extra += ' AND r.candidate_id=? '
        params = params + [cand]
    mid = _int_or_none(request.args.get('mandate_id'))
    if mid:
        extra += ' AND r.mandate_id=? '
        params = params + [mid]
    if q:
        extra += ' AND (r.note LIKE ? OR r.notes LIKE ? OR r.candidate_name LIKE ?) '
        params = params + ['%' + q + '%'] * 3
    conn = get_db()
    rows = conn.execute(
        'SELECT r.*, c.phone AS cand_phone FROM reminders r LEFT JOIN candidates c ON c.id=r.candidate_id '
        'WHERE ' + where + extra + ' ORDER BY r.pinned DESC, r.sort_order ASC, '
        "CASE WHEN r.due_at='' OR r.due_at IS NULL THEN 1 ELSE 0 END, r.due_at ASC, r.id DESC LIMIT 2000",
        params).fetchall()
    tasks = _serialize_many(conn, rows, now)
    if view == 'tag':                                  # exact, case-insensitive tag match
        tag_l = (request.args.get('tag') or '').strip().lstrip('#').lower()
        tasks = [t for t in tasks if tag_l in [x.lower() for x in t['tags']]]
    if view == 'completed':
        tasks.sort(key=lambda t: t['completed_at'] or '', reverse=True)
    out = {'ok': True, 'view': view, 'tasks': tasks}
    if view == 'my_day':
        out['suggestions'] = _my_day_suggestions(conn, scope, now)
        if request.args.get('ats') != '0':
            in_day = {t['candidate_id'] for t in tasks if t['candidate_id'] and not t['done']}
            out['ats_suggestions'] = _ats_suggestions(in_day)
    conn.close()
    return jsonify(out)


ATS_KIND_LABEL = {
    'stale': 'Stale candidate', 'promise': 'Promised follow-up', 'interview': 'Interview',
    'updated': 'Updated CV received', 'submission': 'New application',
}


def _ats_suggestions(skip_candidates):
    """Today's / overdue auto follow-ups from the ATS engine (/api/tasks:
    stale candidates, promised call-backs, interviews, updated CVs, new
    applications) — the same list the "ATS follow-ups" view shows, already
    scoped to the caller. Manual reminders are left out (they are tasks) and
    candidates that already have an open task in My Day are skipped."""
    try:
        resp = _core().get_tasks()
        data = resp.get_json() if hasattr(resp, 'get_json') else {}
    except Exception as e:                              # never break My Day
        print(f'[todo] ats suggestions skipped: {e}')
        return []
    out = []
    for t in (data or {}).get('tasks', []):
        if t.get('type') == 'reminder' or t.get('section') not in ('overdue', 'today'):
            continue
        if t.get('candidate_id') and t['candidate_id'] in skip_candidates:
            continue
        out.append({
            'key': f"{t.get('type')}-{t.get('ref_id')}", 'type': t.get('type'), 'ref_id': t.get('ref_id'),
            'kind': ATS_KIND_LABEL.get(t.get('type'), t.get('type')), 'title': t.get('title') or '',
            'subtitle': t.get('subtitle') or '', 'candidate_id': t.get('candidate_id') or 0,
            'mandate_id': t.get('mandate_id'), 'phone': t.get('phone') or '', 'section': t.get('section'),
            'due_at': t.get('due_at') or '',
        })
        if len(out) >= 25:
            break
    return out


def _my_day_suggestions(conn, scope, now):
    """Open tasks not yet in My Day that are due today/overdue, or were added
    recently. (Wave 3 adds the ATS auto-tasks: stale, promised, interviews.)"""
    where, params = _task_where(scope)
    today = now.date().isoformat()
    recent = (now - datetime.timedelta(days=3)).isoformat(timespec='seconds')
    rows = conn.execute(
        'SELECT r.*, c.phone AS cand_phone FROM reminders r LEFT JOIN candidates c ON c.id=r.candidate_id '
        'WHERE ' + where + " AND r.done=0 AND COALESCE(r.my_day_date,'')!=? AND "
        "((r.due_at!='' AND substr(r.due_at,1,10)<=?) OR r.created_at>=?) "
        'ORDER BY r.due_at ASC LIMIT 30', params + [today, today, recent]).fetchall()
    out = []
    for t in _serialize_many(conn, rows, now):
        t['reason'] = 'overdue' if t['group'] == 'overdue' else ('due_today' if t['group'] == 'today' else 'recent')
        out.append(t)
    return out


# ══════════════════════════════════════════════════════════════════════════
#  TASK CRUD
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/tasks', methods=['POST'])
@login_required
def todo_create():
    d = _body()
    conn = get_db()
    sets, e = _apply_fields(conn, d, creating=True)
    if e:
        conn.close()
        return _err(e)
    if not sets.get('note') and not sets.get('candidate_id'):
        conn.close()
        return _err('title required')
    cid, uid = _cid(), _uid()
    if 'list_id' not in sets:
        # default: the caller's first list (Any.do drops new tasks in Personal)
        _ensure_default_lists(conn, cid, uid)
        first = conn.execute('SELECT id FROM task_lists WHERE company_id=? AND created_by=? AND is_active=1 '
                             'ORDER BY sort_order, id LIMIT 1', (cid, uid)).fetchone()
        sets['list_id'] = first['id'] if first and not d.get('no_default_list') else 0
    row = {
        'candidate_id': 0, 'mandate_id': None, 'candidate_name': '', 'mandate_label': '',
        'note': '', 'due_at': '', 'done': 0, 'stage': 'todo', 'created_at': ts(),
        'owner_id': cid, 'created_by': uid, 'updated_at': ts(),
    }
    row.update(sets)
    if row.get('done'):
        row['completed_at'] = ts()
    cols = _reminder_columns(conn)
    row = {k: v for k, v in row.items() if k in cols}
    cur = conn.execute(f"INSERT INTO reminders ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                       list(row.values()))
    rid = cur.lastrowid
    for i, s in enumerate((d.get('subtasks') or [])[:100] if isinstance(d.get('subtasks'), list) else []):
        txt = _clean_text(s.get('text') if isinstance(s, dict) else s, MAX_TITLE)
        if txt:
            conn.execute('INSERT INTO task_subtasks (reminder_id, company_id, text, done, sort_order, created_by, '
                         'created_at, updated_at) VALUES (?,?,?,0,?,?,?,?)', (rid, cid, txt, i + 1, uid, ts(), ts()))
    conn.commit()
    r = _get_task_row(conn, rid)
    task = _serialize_many(conn, [r])[0] if r else {'id': rid}
    conn.close()
    return jsonify({'ok': True, 'task': task})


@bp.route('/tasks/<int:rid>', methods=['GET'])
@login_required
def todo_get(rid):
    conn = get_db()
    r = conn.execute('SELECT r.*, c.phone AS cand_phone FROM reminders r LEFT JOIN candidates c ON c.id=r.candidate_id '
                     'WHERE r.id=? AND r.owner_id=? ' + _rem_scope('r')[0], [rid, _cid()] + _rem_scope('r')[1]).fetchone()
    if not r:
        conn.close()
        return _err('Not found', 404)
    task = _serialize_many(conn, [r])[0]
    task['subtasks'] = [dict(id=s['id'], text=s['text'], done=bool(s['done']), sort_order=s['sort_order'])
                        for s in conn.execute('SELECT * FROM task_subtasks WHERE reminder_id=? AND company_id=? '
                                              'ORDER BY sort_order, id', (rid, _cid()))]
    conn.close()
    return jsonify({'ok': True, 'task': task})


@bp.route('/tasks/<int:rid>', methods=['PATCH', 'PUT', 'POST'])
@login_required
def todo_update(rid):
    d = _body()
    conn = get_db()
    row = _get_task_row(conn, rid)
    if not row:
        conn.close()
        return _err('Not found', 404)
    sets, e = _apply_fields(conn, d, existing=row)
    if e:
        conn.close()
        return _err(e)
    done_change = None
    if 'done' in d and 'stage' not in d:
        done_change = _bool(d.get('done'))
    if sets.get('note') == '' and not (sets.get('candidate_id', row['candidate_id'])):
        conn.close()
        return _err('title required')
    _update_row(conn, rid, sets)
    result = {}
    if done_change is not None:
        result = _complete(conn, _get_task_row(conn, rid), done_change)
    conn.commit()
    r = conn.execute('SELECT r.*, c.phone AS cand_phone FROM reminders r LEFT JOIN candidates c ON c.id=r.candidate_id '
                     'WHERE r.id=? AND r.owner_id=?', (rid, _cid())).fetchone()
    task = _serialize_many(conn, [r])[0]
    conn.close()
    return jsonify(dict({'ok': True, 'task': task}, **result))


@bp.route('/tasks/<int:rid>/complete', methods=['POST'])
@login_required
def todo_complete(rid):
    d = _body()
    conn = get_db()
    row = _get_task_row(conn, rid)
    if not row:
        conn.close()
        return _err('Not found', 404)
    res = _complete(conn, row, _bool(d.get('done', True)))
    conn.commit()
    conn.close()
    return jsonify(dict({'ok': True}, **res))


@bp.route('/tasks/<int:rid>/my-day', methods=['POST'])
@login_required
def todo_my_day(rid):
    d = _body()
    conn = get_db()
    if not _get_task_row(conn, rid):
        conn.close()
        return _err('Not found', 404)
    on = _bool(d.get('on', True))
    _update_row(conn, rid, {'my_day_date': _today().isoformat() if on else ''})
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'my_day': on})


@bp.route('/tasks/<int:rid>', methods=['DELETE'])
@login_required
def todo_delete(rid):
    conn = get_db()
    if not _get_task_row(conn, rid):
        conn.close()
        return _err('Not found', 404)
    conn.execute('DELETE FROM task_subtasks WHERE reminder_id=? AND company_id=?', (rid, _cid()))
    conn.execute('DELETE FROM reminders WHERE id=? AND owner_id=?', (rid, _cid()))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@bp.route('/tasks/reorder', methods=['POST'])
@login_required
def todo_reorder():
    """Body: {ids: [id, id, ...]} in the new visual order."""
    ids = _body().get('ids') or []
    if not isinstance(ids, list):
        return _err('ids must be a list')
    conn = get_db()
    n = 0
    for i, v in enumerate(ids[:MAX_BULK]):
        rid = _int_or_none(v)
        if rid and _get_task_row(conn, rid):
            conn.execute('UPDATE reminders SET sort_order=?, updated_at=? WHERE id=? AND owner_id=?',
                         (i + 1, ts(), rid, _cid()))
            n += 1
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'updated': n})


_BULK_ACTIONS = ('complete', 'uncomplete', 'delete', 'move_list', 'set_due', 'set_priority',
                 'add_my_day', 'remove_my_day', 'assign', 'add_tag')


@bp.route('/tasks/bulk', methods=['POST'])
@login_required
def todo_bulk():
    """Body: {ids: [...], action: one of _BULK_ACTIONS, value: ...}"""
    d = _body()
    ids = d.get('ids') or []
    action = str(d.get('action') or '')
    if not isinstance(ids, list) or not ids:
        return _err('ids required')
    if action not in _BULK_ACTIONS:
        return _err('unknown action')
    value = d.get('value')
    conn = get_db()
    # validate the value once, through the same rules as a single edit
    field = {'move_list': 'list_id', 'set_due': 'due_at', 'set_priority': 'priority',
             'assign': 'assigned_to'}.get(action)
    sets = {}
    if field:
        sets, e = _apply_fields(conn, {field: value})
        if e:
            conn.close()
            return _err(e)
    tag_add = _norm_tags([value]) if action == 'add_tag' else []
    if action == 'add_tag':
        if not tag_add:
            conn.close()
            return _err('tag required')
        _ensure_tag_defs(conn, _cid(), tag_add)
    n = 0
    for v in ids[:MAX_BULK]:
        rid = _int_or_none(v)
        row = _get_task_row(conn, rid) if rid else None
        if not row:
            continue
        if action == 'complete':
            _complete(conn, row, True)
        elif action == 'uncomplete':
            _complete(conn, row, False)
        elif action == 'delete':
            conn.execute('DELETE FROM task_subtasks WHERE reminder_id=? AND company_id=?', (rid, _cid()))
            conn.execute('DELETE FROM reminders WHERE id=? AND owner_id=?', (rid, _cid()))
        elif action == 'add_my_day':
            _update_row(conn, rid, {'my_day_date': _today().isoformat()})
        elif action == 'remove_my_day':
            _update_row(conn, rid, {'my_day_date': ''})
        elif action == 'add_tag':
            tags = _load_tags(row['tags'])
            _update_row(conn, rid, {'tags': json.dumps(_norm_tags(tags + tag_add))})
        else:
            _update_row(conn, rid, dict(sets))
        n += 1
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'updated': n})


# ══════════════════════════════════════════════════════════════════════════
#  SUBTASKS
# ══════════════════════════════════════════════════════════════════════════
def _subtask_row(conn, sid):
    s = conn.execute('SELECT * FROM task_subtasks WHERE id=? AND company_id=?', (sid, _cid())).fetchone()
    if not s or not _get_task_row(conn, s['reminder_id']):
        return None
    return s


@bp.route('/tasks/<int:rid>/subtasks', methods=['POST'])
@login_required
def todo_subtask_add(rid):
    d = _body()
    txt = _clean_text(d.get('text'), MAX_TITLE)
    if not txt:
        return _err('text required')
    conn = get_db()
    if not _get_task_row(conn, rid):
        conn.close()
        return _err('Not found', 404)
    mx = conn.execute('SELECT COALESCE(MAX(sort_order),0) FROM task_subtasks WHERE reminder_id=?', (rid,)).fetchone()[0]
    cur = conn.execute('INSERT INTO task_subtasks (reminder_id, company_id, text, done, sort_order, created_by, '
                       'created_at, updated_at) VALUES (?,?,?,0,?,?,?,?)',
                       (rid, _cid(), txt, (mx or 0) + 1, _uid(), ts(), ts()))
    conn.execute('UPDATE reminders SET updated_at=? WHERE id=?', (ts(), rid))
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return jsonify({'ok': True, 'subtask': {'id': sid, 'text': txt, 'done': False, 'sort_order': (mx or 0) + 1}})


@bp.route('/subtasks/<int:sid>', methods=['PATCH', 'PUT', 'POST'])
@login_required
def todo_subtask_update(sid):
    d = _body()
    conn = get_db()
    s = _subtask_row(conn, sid)
    if not s:
        conn.close()
        return _err('Not found', 404)
    sets = {}
    if 'text' in d:
        txt = _clean_text(d.get('text'), MAX_TITLE)
        if not txt:
            conn.close()
            return _err('text required')
        sets['text'] = txt
    if 'done' in d:
        sets['done'] = 1 if _bool(d.get('done')) else 0
    if 'sort_order' in d:
        try:
            sets['sort_order'] = float(d.get('sort_order') or 0)
        except (TypeError, ValueError):
            conn.close()
            return _err('sort_order must be a number')
    if sets:
        sets['updated_at'] = ts()
        conn.execute('UPDATE task_subtasks SET ' + ', '.join(f'{k}=?' for k in sets) + ' WHERE id=? AND company_id=?',
                     list(sets.values()) + [sid, _cid()])
        conn.execute('UPDATE reminders SET updated_at=? WHERE id=?', (ts(), s['reminder_id']))
        conn.commit()
    conn.close()
    return jsonify({'ok': True})


@bp.route('/subtasks/<int:sid>', methods=['DELETE'])
@login_required
def todo_subtask_delete(sid):
    conn = get_db()
    s = _subtask_row(conn, sid)
    if not s:
        conn.close()
        return _err('Not found', 404)
    conn.execute('DELETE FROM task_subtasks WHERE id=? AND company_id=?', (sid, _cid()))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════
#  LISTS
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/lists', methods=['GET'])
@login_required
def todo_lists():
    conn = get_db()
    _ensure_default_lists(conn, _cid(), _uid())
    lists = [dict(id=l['id'], name=l['name'], color=l['color'], icon=l['icon'], is_shared=bool(l['is_shared']),
                  mine=int(l['created_by'] or 0) == _uid(), sort_order=l['sort_order'])
             for l in _visible_lists(conn, _cid())]
    conn.close()
    return jsonify({'ok': True, 'lists': lists})


@bp.route('/lists', methods=['POST'])
@login_required
def todo_list_create():
    d = _body()
    name = _clean_text(d.get('name'), MAX_LIST_NAME)
    if not name:
        return _err('name required')
    color = str(d.get('color') or '').strip()[:9]
    if not re.match(r'^#[0-9A-Fa-f]{3,8}$', color):
        color = LIST_COLORS[0]
    shared = 1 if (_bool(d.get('is_shared')) and is_company_admin() and not _scoped()) else 0
    conn = get_db()
    mx = conn.execute('SELECT COALESCE(MAX(sort_order),0) FROM task_lists WHERE company_id=?', (_cid(),)).fetchone()[0]
    cur = conn.execute('INSERT INTO task_lists (company_id, created_by, name, color, icon, is_shared, sort_order, '
                       'is_active, created_at, updated_at) VALUES (?,?,?,?,?,?,?,1,?,?)',
                       (_cid(), _uid(), name, color, _clean_text(d.get('icon'), 16), shared, (mx or 0) + 1, ts(), ts()))
    conn.commit()
    lid = cur.lastrowid
    conn.close()
    return jsonify({'ok': True, 'list': {'id': lid, 'name': name, 'color': color, 'is_shared': bool(shared),
                                         'mine': True}})


@bp.route('/lists/<int:lid>', methods=['PATCH', 'PUT', 'POST'])
@login_required
def todo_list_update(lid):
    d = _body()
    conn = get_db()
    if not _list_ok(conn, _cid(), lid, write='manage'):
        conn.close()
        return _err('Not found', 404)
    sets = {}
    if 'name' in d:
        name = _clean_text(d.get('name'), MAX_LIST_NAME)
        if not name:
            conn.close()
            return _err('name required')
        sets['name'] = name
    if 'color' in d:
        color = str(d.get('color') or '').strip()[:9]
        if not re.match(r'^#[0-9A-Fa-f]{3,8}$', color):
            conn.close()
            return _err('invalid color')
        sets['color'] = color
    if 'icon' in d:
        sets['icon'] = _clean_text(d.get('icon'), 16)
    if 'sort_order' in d:
        try:
            sets['sort_order'] = float(d.get('sort_order') or 0)
        except (TypeError, ValueError):
            conn.close()
            return _err('sort_order must be a number')
    if 'is_shared' in d:
        if not (is_company_admin() and not _scoped()):
            conn.close()
            return _err('Only an admin can share a list.', 403)
        sets['is_shared'] = 1 if _bool(d.get('is_shared')) else 0
    if sets:
        sets['updated_at'] = ts()
        conn.execute('UPDATE task_lists SET ' + ', '.join(f'{k}=?' for k in sets) + ' WHERE id=? AND company_id=?',
                     list(sets.values()) + [lid, _cid()])
        conn.commit()
    conn.close()
    return jsonify({'ok': True})


@bp.route('/lists/<int:lid>', methods=['DELETE'])
@login_required
def todo_list_delete(lid):
    """Soft-delete. Tasks are never deleted with a list: they move to no list."""
    conn = get_db()
    if not _list_ok(conn, _cid(), lid, write='manage'):
        conn.close()
        return _err('Not found', 404)
    conn.execute('UPDATE task_lists SET is_active=0, updated_at=? WHERE id=? AND company_id=?', (ts(), lid, _cid()))
    moved = conn.execute('UPDATE reminders SET list_id=0, updated_at=? WHERE list_id=? AND owner_id=?',
                         (ts(), lid, _cid())).rowcount
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'tasks_moved': moved})


# ══════════════════════════════════════════════════════════════════════════
#  TAGS
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/tags', methods=['GET'])
@login_required
def todo_tags():
    conn = get_db()
    tags = [{'id': t['id'], 'name': t['name'], 'color': t['color']}
            for t in conn.execute('SELECT * FROM task_tag_defs WHERE company_id=? ORDER BY name COLLATE NOCASE',
                                  (_cid(),))]
    conn.close()
    return jsonify({'ok': True, 'tags': tags})


@bp.route('/tags', methods=['POST'])
@login_required
def todo_tag_create():
    d = _body()
    names = _norm_tags([d.get('name')])
    if not names:
        return _err('name required')
    color = str(d.get('color') or '').strip()[:9]
    if not re.match(r'^#[0-9A-Fa-f]{3,8}$', color):
        color = LIST_COLORS[len(names[0]) % len(LIST_COLORS)]
    conn = get_db()
    ex = conn.execute('SELECT id FROM task_tag_defs WHERE company_id=? AND name=?', (_cid(), names[0])).fetchone()
    if ex:
        conn.execute('UPDATE task_tag_defs SET color=? WHERE id=?', (color, ex['id']))
        tid = ex['id']
    else:
        tid = conn.execute('INSERT INTO task_tag_defs (company_id, name, color, created_by, created_at) '
                           'VALUES (?,?,?,?,?)', (_cid(), names[0], color, _uid(), ts())).lastrowid
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'tag': {'id': tid, 'name': names[0], 'color': color}})


@bp.route('/tags/<int:tid>', methods=['PATCH', 'PUT', 'POST'])
@login_required
def todo_tag_update(tid):
    """Only the colour is editable (renaming would need rewriting every task)."""
    d = _body()
    color = str(d.get('color') or '').strip()[:9]
    if not re.match(r'^#[0-9A-Fa-f]{3,8}$', color):
        return _err('invalid color')
    conn = get_db()
    t = conn.execute('SELECT created_by FROM task_tag_defs WHERE id=? AND company_id=?', (tid, _cid())).fetchone()
    admin = is_company_admin() and not _scoped()
    if not t or not (admin or int(t['created_by'] or 0) == _uid()):
        conn.close()
        return _err('Not found', 404)
    n = conn.execute('UPDATE task_tag_defs SET color=? WHERE id=? AND company_id=?', (color, tid, _cid())).rowcount
    conn.commit()
    conn.close()
    return (jsonify({'ok': True}) if n else _err('Not found', 404))


@bp.route('/tags/<int:tid>', methods=['DELETE'])
@login_required
def todo_tag_delete(tid):
    """Removes the tag definition (colour). Tasks keep their tag text, so no
    task data is lost; an admin-only action because tags are company-wide."""
    if _scoped() or not is_company_admin():
        return _err('Only an admin can delete a tag.', 403)
    conn = get_db()
    n = conn.execute('DELETE FROM task_tag_defs WHERE id=? AND company_id=?', (tid, _cid())).rowcount
    conn.commit()
    conn.close()
    return (jsonify({'ok': True}) if n else _err('Not found', 404))


# ══════════════════════════════════════════════════════════════════════════
#  PREFERENCES  (theme toggle, default view — per user)
# ══════════════════════════════════════════════════════════════════════════
_PREF_KEYS = {
    'theme': ('light', 'dark', 'system'),
    'default_view': ('my_day', 'next7', 'all', 'board', 'calendar'),
    'layout': ('default', 'compact'),
    'show_completed': (True, False),
}


@bp.route('/prefs', methods=['GET'])
@login_required
def todo_prefs_get():
    conn = get_db()
    r = conn.execute('SELECT prefs FROM todo_prefs WHERE user_id=?', (_uid(),)).fetchone()
    conn.close()
    try:
        prefs = json.loads(r['prefs']) if r else {}
    except Exception:
        prefs = {}
    return jsonify({'ok': True, 'prefs': prefs})


@bp.route('/prefs', methods=['POST', 'PATCH', 'PUT'])
@login_required
def todo_prefs_set():
    d = _body()
    uid = _uid()
    if not uid:
        return _err('login required', 401)
    conn = get_db()
    r = conn.execute('SELECT prefs FROM todo_prefs WHERE user_id=?', (uid,)).fetchone()
    try:
        prefs = json.loads(r['prefs']) if r else {}
    except Exception:
        prefs = {}
    for k, allowed in _PREF_KEYS.items():
        if k in d:
            v = d[k]
            if allowed == (True, False):
                v = _bool(v)
            elif v not in allowed:
                conn.close()
                return _err(f'{k} must be one of: ' + ', '.join(map(str, allowed)))
            prefs[k] = v
    conn.execute('INSERT INTO todo_prefs (user_id, company_id, prefs, updated_at) VALUES (?,?,?,?) '
                 'ON CONFLICT(user_id) DO UPDATE SET prefs=excluded.prefs, updated_at=excluded.updated_at',
                 (uid, _cid(), json.dumps(prefs), ts()))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'prefs': prefs})


# ══════════════════════════════════════════════════════════════════════════
#  TEAM VIEW  (Wave 4 — admins: who has what, who is behind)
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/team', methods=['GET'])
@login_required
def todo_team():
    if _scoped() or not is_company_admin():
        return _err('Only an admin can see the team view.', 403)
    cid = _cid()
    conn = get_db()
    now = _now()
    week_ago = (now - datetime.timedelta(days=7)).isoformat(timespec='seconds')
    users = conn.execute("SELECT id, display_name, username, is_company_admin, role FROM users "
                         "WHERE company_id=? AND COALESCE(status,'approved')='approved' ORDER BY id", (cid,)).fetchall()
    rows = conn.execute("SELECT id, due_at, done, completed_at, COALESCE(assigned_to,0) a, COALESCE(created_by,0) c "
                        "FROM reminders WHERE owner_id=?", (cid,)).fetchall()
    stats = {}
    unowned = 0
    for r in rows:
        who = r['a'] or r['c']
        if not who:
            if not r['done']:
                unowned += 1
            continue
        s = stats.setdefault(who, {'open': 0, 'overdue': 0, 'today': 0, 'upcoming': 0, 'someday': 0, 'done_7d': 0})
        if r['done']:
            if (r['completed_at'] or '') >= week_ago:
                s['done_7d'] += 1
            continue
        s['open'] += 1
        g = group_for(r['due_at'] or '', now)
        if g in ('overdue', 'today', 'someday'):
            s[g] += 1
        else:
            s['upcoming'] += 1
    out = []
    for u in users:
        s = stats.get(int(u['id']), {'open': 0, 'overdue': 0, 'today': 0, 'upcoming': 0, 'someday': 0, 'done_7d': 0})
        out.append(dict(s, id=u['id'], name=u['display_name'] or u['username'] or '',
                        is_admin=bool(u['is_company_admin']), role=u['role'] or ''))
    out.sort(key=lambda x: (-x['overdue'], -x['today'], x['name'].lower()))
    conn.close()
    return jsonify({'ok': True, 'members': out, 'unowned_open': unowned})


# ══════════════════════════════════════════════════════════════════════════
#  AUTO-TASK RULES  (Wave 4)
#  "When a candidate moves to stage X, create task T due in N days."
#  The stage can change from ~20 places in the ATS (pipeline drag, bulk move,
#  WhatsApp agent, timers, imports …). Instead of patching each one, the 60s
#  background loop reads NEW rows of stage_history (a watermark keeps its
#  place), so every path is covered without touching any of them.
# ══════════════════════════════════════════════════════════════════════════
DEFAULT_AUTO_RULES = [
    {'stage': 'Shared with Client', 'title': 'Get client feedback: {name}', 'due_days': 2,
     'due_time': '11:00', 'priority': 'medium'},
    {'stage': 'Placed', 'title': 'Confirm joining date: {name} ({client})', 'due_days': 1,
     'due_time': '11:00', 'priority': 'high'},
    {'stage': 'Joined', 'title': 'Raise invoice: {name} — {client}', 'due_days': 0,
     'due_time': '', 'priority': 'high'},
]
_TIME_RX = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
AUTO_MAX_AGE_H = 48            # stage changes older than this (e.g. imports) never create tasks


def _rules_for(conn, company_id):
    """Active rules for a company. A company that never edited its rules uses
    the defaults; once edited, its own rows (deleted ones soft-deleted) rule."""
    rows = conn.execute('SELECT * FROM todo_auto_rules WHERE company_id=?', (company_id,)).fetchall()
    if not rows:
        return [dict(r, id=0, is_active=1, assign_to=0) for r in DEFAULT_AUTO_RULES]
    return [dict(r) for r in rows if r['is_active'] and not r['is_deleted']]


def _materialize_rules(conn, company_id):
    """First edit: copy the defaults into rows so they can be changed."""
    n = conn.execute('SELECT COUNT(*) FROM todo_auto_rules WHERE company_id=?', (company_id,)).fetchone()[0]
    if n:
        return
    for r in DEFAULT_AUTO_RULES:
        conn.execute('INSERT INTO todo_auto_rules (company_id, stage, title, due_days, due_time, priority, assign_to, '
                     'is_active, is_deleted, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,0,1,0,0,?,?)',
                     (company_id, r['stage'], r['title'], r['due_days'], r['due_time'], r['priority'], ts(), ts()))


def _fill(tpl, ctx):
    out = str(tpl or '')
    for k, v in ctx.items():
        out = out.replace('{' + k + '}', str(v or ''))
    return out.strip()[:MAX_TITLE]


def run_auto_rules(conn, limit=500):
    """Process new stage changes once. Safe to call repeatedly; returns the
    number of tasks created. Called every 60s from the reminder loop."""
    st = conn.execute("SELECT value FROM todo_auto_state WHERE key='stage_history_wm'").fetchone()
    top = conn.execute('SELECT COALESCE(MAX(id),0) FROM stage_history').fetchone()[0]
    if st is None:                                  # first run: start from now, never backfill
        conn.execute("INSERT OR REPLACE INTO todo_auto_state (key, value) VALUES ('stage_history_wm', ?)", (str(top),))
        conn.commit()
        return 0
    wm = int(st['value'] or 0)
    rows = conn.execute('SELECT id, candidate_id, to_stage, created_at FROM stage_history WHERE id>? '
                        'ORDER BY id LIMIT ?', (wm, limit)).fetchall()
    if not rows:
        return 0
    now = _core()._ist_now()
    oldest = (now - datetime.timedelta(hours=AUTO_MAX_AGE_H)).isoformat(timespec='seconds')
    rule_cache, made = {}, 0
    cols = _reminder_columns(conn)
    for h in rows:
        wm = h['id']
        try:
            if (h['created_at'] or '') < oldest:
                continue
            cand = conn.execute('SELECT c.id, c.name, c.owner_id, c.mandate_id, m.role, m.client, m.status, '
                                'm.assigned_user_id FROM candidates c LEFT JOIN mandates m ON m.id=c.mandate_id '
                                'WHERE c.id=?', (h['candidate_id'],)).fetchone()
            if not cand or not cand['owner_id'] or (cand['status'] or '') != 'active':
                continue
            co = int(cand['owner_id'])
            if co not in rule_cache:
                rule_cache[co] = _rules_for(conn, co)
            stage = (h['to_stage'] or '').strip().lower()
            for rule in rule_cache[co]:
                if (rule['stage'] or '').strip().lower() != stage:
                    continue
                key = 'stage:' + stage
                if conn.execute('SELECT 1 FROM reminders WHERE candidate_id=? AND auto_rule=? AND done=0 AND owner_id=?',
                                (cand['id'], key, co)).fetchone():
                    continue                          # an open task for this already exists
                ctx = {'name': cand['name'] or 'Candidate', 'role': cand['role'] or '', 'client': cand['client'] or '',
                       'stage': h['to_stage'] or ''}
                due_d = (now.date() + datetime.timedelta(days=int(rule.get('due_days') or 0))).isoformat()
                tm = rule.get('due_time') or ''
                due = due_d + ('T' + tm + ':00' if _TIME_RX.match(tm) else '')
                assignee = int(rule.get('assign_to') or 0) or int(cand['assigned_user_id'] or 0)
                row = {
                    'candidate_id': cand['id'], 'mandate_id': cand['mandate_id'], 'candidate_name': cand['name'] or '',
                    'mandate_label': ((cand['role'] or '') + ' — ' + (cand['client'] or '')) if cand['role'] else '',
                    'note': _fill(rule.get('title'), ctx) or ('Follow up: ' + ctx['name']),
                    'notes': 'Auto-task: moved to "' + (h['to_stage'] or '') + '".',
                    'due_at': due, 'done': 0, 'stage': 'todo', 'created_at': ts(), 'updated_at': ts(),
                    'owner_id': co, 'created_by': 0, 'assigned_to': assignee,
                    'priority': rule.get('priority') if rule.get('priority') in PRIORITIES else 'none',
                    'tags': json.dumps(['Auto']), 'auto_rule': key, 'list_id': 0,
                }
                row = {k: v for k, v in row.items() if k in cols}
                conn.execute(f"INSERT INTO reminders ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                             list(row.values()))
                try:
                    conn.execute('INSERT OR IGNORE INTO task_tag_defs (company_id, name, color, created_by, created_at) '
                                 "VALUES (?, 'Auto', '#5D6D7E', 0, ?)", (co, ts()))
                except Exception:
                    pass
                made += 1
        except Exception as e:                        # one bad row never blocks the rest
            print(f'[todo-auto] row {h["id"]} skipped: {e}')
    conn.execute("INSERT OR REPLACE INTO todo_auto_state (key, value) VALUES ('stage_history_wm', ?)", (str(wm),))
    conn.commit()
    return made


def _rule_out(r):
    return {'id': r['id'], 'stage': r['stage'], 'title': r['title'], 'due_days': r['due_days'],
            'due_time': r['due_time'], 'priority': r['priority'], 'assign_to': r['assign_to'],
            'is_active': bool(r['is_active'])}


def _admin_only():
    if _scoped() or not is_company_admin():
        return _err('Only an admin can manage auto-task rules.', 403)
    return None


def _clean_rule(d, partial=False):
    out = {}
    if 'stage' in d or not partial:
        stg = _clean_text(d.get('stage'), 80)
        if not stg:
            return None, 'stage required'
        out['stage'] = stg
    if 'title' in d or not partial:
        t = _clean_text(d.get('title'), MAX_TITLE)
        if not t:
            return None, 'title required'
        out['title'] = t
    if 'due_days' in d or not partial:
        try:
            dd = int(d.get('due_days') or 0)
        except (TypeError, ValueError):
            return None, 'due_days must be a number'
        if dd < 0 or dd > 60:
            return None, 'due_days must be 0-60'
        out['due_days'] = dd
    if 'due_time' in d:
        tm = str(d.get('due_time') or '').strip()
        if tm and not _TIME_RX.match(tm):
            return None, 'due_time must be HH:MM'
        out['due_time'] = tm
    if 'priority' in d:
        p = str(d.get('priority') or 'none')
        if p not in PRIORITIES:
            return None, 'invalid priority'
        out['priority'] = p
    if 'assign_to' in d:
        out['assign_to'] = _int_or_none(d.get('assign_to')) or 0
    if 'is_active' in d:
        out['is_active'] = 1 if _bool(d.get('is_active')) else 0
    return out, None


@bp.route('/auto-rules', methods=['GET'])
@login_required
def todo_rules_list():
    deny = _admin_only()
    if deny:
        return deny
    conn = get_db()
    _materialize_rules(conn, _cid())
    conn.commit()
    rows = conn.execute('SELECT * FROM todo_auto_rules WHERE company_id=? AND is_deleted=0 ORDER BY id',
                        (_cid(),)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'rules': [_rule_out(r) for r in rows],
                    'help': 'Placeholders: {name} {role} {client} {stage}. Due in N days at HH:MM (empty = all day). '
                            'Assign 0 = the job\'s primary recruiter.'})


@bp.route('/auto-rules', methods=['POST'])
@login_required
def todo_rules_create():
    deny = _admin_only()
    if deny:
        return deny
    vals, e = _clean_rule(_body())
    if e:
        return _err(e)
    conn = get_db()
    if vals.get('assign_to') and vals['assign_to'] not in _company_user_ids(conn, _cid()):
        conn.close()
        return _err('assignee must be a user in your company')
    _materialize_rules(conn, _cid())
    rid = conn.execute('INSERT INTO todo_auto_rules (company_id, stage, title, due_days, due_time, priority, assign_to, '
                       'is_active, is_deleted, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,0,?,?,?)',
                       (_cid(), vals['stage'], vals['title'], vals['due_days'], vals.get('due_time', ''),
                        vals.get('priority', 'none'), vals.get('assign_to', 0), vals.get('is_active', 1),
                        _uid(), ts(), ts())).lastrowid
    conn.commit()
    r = conn.execute('SELECT * FROM todo_auto_rules WHERE id=?', (rid,)).fetchone()
    conn.close()
    return jsonify({'ok': True, 'rule': _rule_out(r)})


@bp.route('/auto-rules/<int:rid>', methods=['PATCH', 'PUT', 'POST'])
@login_required
def todo_rules_update(rid):
    deny = _admin_only()
    if deny:
        return deny
    vals, e = _clean_rule(_body(), partial=True)
    if e:
        return _err(e)
    conn = get_db()
    if vals.get('assign_to') and vals['assign_to'] not in _company_user_ids(conn, _cid()):
        conn.close()
        return _err('assignee must be a user in your company')
    if not conn.execute('SELECT 1 FROM todo_auto_rules WHERE id=? AND company_id=? AND is_deleted=0',
                        (rid, _cid())).fetchone():
        conn.close()
        return _err('Not found', 404)
    if vals:
        vals['updated_at'] = ts()
        conn.execute('UPDATE todo_auto_rules SET ' + ', '.join(f'{k}=?' for k in vals) + ' WHERE id=? AND company_id=?',
                     list(vals.values()) + [rid, _cid()])
        conn.commit()
    r = conn.execute('SELECT * FROM todo_auto_rules WHERE id=?', (rid,)).fetchone()
    conn.close()
    return jsonify({'ok': True, 'rule': _rule_out(r)})


@bp.route('/auto-rules/<int:rid>', methods=['DELETE'])
@login_required
def todo_rules_delete(rid):
    deny = _admin_only()
    if deny:
        return deny
    conn = get_db()
    n = conn.execute('UPDATE todo_auto_rules SET is_deleted=1, is_active=0, updated_at=? WHERE id=? AND company_id=? '
                     'AND is_deleted=0', (ts(), rid, _cid())).rowcount
    conn.commit()
    conn.close()
    return (jsonify({'ok': True}) if n else _err('Not found', 404))
