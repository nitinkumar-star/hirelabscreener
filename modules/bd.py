"""
RecruitOS — Business Development Module (Layer 3)

Powers two features from one clean data structure:
  1. BD Command Center  — the daily-use dashboard (today's follow-ups, overdue,
     stale clients, open requirements, new prospects, upcoming meetings)
  2. Client Timeline    — every activity against a client, chronologically

Core object: crm_activities — a unified log of every BD touch-point:
  meeting | call | followup | note | email | requirement | task

Each activity can carry a due_at (for follow-ups/tasks/meetings) so the same
row serves both "history" (timeline) and "what's due" (command center).

Architecture mirrors the CRM module: Repository → Service → API, all writing
to the universal activity timeline + audit log from the foundation layer.
"""

import json
import datetime
from flask import Blueprint, request, jsonify

from modules.shared import (
    get_db, ts, current_user, effective_company_id, real_user_id,
    is_company_admin, login_required, log_activity,
)
from modules import register_migration

bp = Blueprint('bd', __name__, url_prefix='/api/bd')


# ══════════════════════════════════════════════════════════════════════════
#  MIGRATION
# ══════════════════════════════════════════════════════════════════════════
ACTIVITY_TYPES = {'meeting', 'call', 'followup', 'note', 'email', 'requirement', 'task'}
ACTIVITY_STATUS = {'open', 'done', 'cancelled'}


@register_migration
def migrate(conn):
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS crm_activities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        client_id INTEGER NOT NULL,               -- FK -> crm_clients.id
        contact_id INTEGER DEFAULT 0,             -- optional FK -> crm_contacts.id
        activity_type TEXT DEFAULT 'note',        -- meeting|call|followup|note|email|requirement|task
        subject TEXT DEFAULT '',
        body TEXT DEFAULT '',
        outcome TEXT DEFAULT '',                  -- free text result
        due_at TEXT DEFAULT '',                   -- for followup/task/meeting
        completed_at TEXT DEFAULT '',
        status TEXT DEFAULT 'open',               -- open|done|cancelled
        owner_user_id INTEGER DEFAULT 0,          -- who owns this action
        meta TEXT DEFAULT '',                     -- JSON: structured extras
        is_active INTEGER DEFAULT 1,
        created_by INTEGER DEFAULT 0,
        updated_by INTEGER DEFAULT 0,
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    )''')
    for sql in [
        'CREATE INDEX IF NOT EXISTS idx_bd_act_company ON crm_activities(company_id, is_active)',
        'CREATE INDEX IF NOT EXISTS idx_bd_act_client ON crm_activities(client_id, is_active)',
        'CREATE INDEX IF NOT EXISTS idx_bd_act_due ON crm_activities(company_id, status, due_at)',
        'CREATE INDEX IF NOT EXISTS idx_bd_act_owner ON crm_activities(owner_user_id, status)',
    ]:
        try:
            c.execute(sql)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════
def _now():
    return datetime.datetime.now()


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None


def _activity_public(row, client_name=''):
    d = dict(row)
    d.pop('meta', None)
    if client_name:
        d['client_name'] = client_name
    return d


class ValidationError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


# ══════════════════════════════════════════════════════════════════════════
#  REPOSITORY
# ══════════════════════════════════════════════════════════════════════════
class ActivityRepo:
    @staticmethod
    def get(conn, company_id, aid):
        return conn.execute(
            'SELECT * FROM crm_activities WHERE id=? AND company_id=? AND is_active=1',
            (aid, company_id)).fetchone()

    @staticmethod
    def insert(conn, data):
        cols = ('company_id,client_id,contact_id,activity_type,subject,body,outcome,'
                'due_at,completed_at,status,owner_user_id,meta,is_active,created_by,'
                'updated_by,created_at,updated_at')
        conn.execute(
            f'INSERT INTO crm_activities ({cols}) VALUES ({",".join("?"*17)})',
            (data['company_id'], data['client_id'], data['contact_id'],
             data['activity_type'], data['subject'], data['body'], data['outcome'],
             data['due_at'], data['completed_at'], data['status'],
             data['owner_user_id'], data['meta'], 1, data['actor'], data['actor'],
             data['now'], data['now']))
        return conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']

    @staticmethod
    def update(conn, aid, fields, actor, now):
        sets = ', '.join(f'{k}=?' for k in fields) + ', updated_by=?, updated_at=?'
        conn.execute(f'UPDATE crm_activities SET {sets} WHERE id=?',
                     list(fields.values()) + [actor, now, aid])

    @staticmethod
    def timeline(conn, company_id, client_id, limit=100):
        return conn.execute(
            'SELECT * FROM crm_activities WHERE company_id=? AND client_id=? AND is_active=1 '
            'ORDER BY COALESCE(NULLIF(created_at,\'\'), due_at) DESC, id DESC LIMIT ?',
            (company_id, client_id, limit)).fetchall()

    @staticmethod
    def due_between(conn, company_id, start_iso, end_iso, owner_user_id=None):
        q = ('SELECT * FROM crm_activities WHERE company_id=? AND is_active=1 '
             "AND status='open' AND due_at!='' AND due_at>=? AND due_at<=?")
        params = [company_id, start_iso, end_iso]
        if owner_user_id:
            q += ' AND owner_user_id=?'
            params.append(owner_user_id)
        q += ' ORDER BY due_at ASC'
        return conn.execute(q, params).fetchall()

    @staticmethod
    def overdue(conn, company_id, now_iso, owner_user_id=None):
        q = ('SELECT * FROM crm_activities WHERE company_id=? AND is_active=1 '
             "AND status='open' AND due_at!='' AND due_at<? ")
        params = [company_id, now_iso]
        if owner_user_id:
            q += ' AND owner_user_id=?'
            params.append(owner_user_id)
        q += ' ORDER BY due_at ASC'
        return conn.execute(q, params).fetchall()

    @staticmethod
    def by_type_due(conn, company_id, atype, start_iso, end_iso, owner_user_id=None):
        q = ('SELECT * FROM crm_activities WHERE company_id=? AND is_active=1 '
             "AND status='open' AND activity_type=? AND due_at>=? AND due_at<=?")
        params = [company_id, atype, start_iso, end_iso]
        if owner_user_id:
            q += ' AND owner_user_id=?'
            params.append(owner_user_id)
        q += ' ORDER BY due_at ASC'
        return conn.execute(q, params).fetchall()


# ══════════════════════════════════════════════════════════════════════════
#  SERVICE
# ══════════════════════════════════════════════════════════════════════════
class ActivityService:
    @staticmethod
    def create(conn, payload):
        company_id = effective_company_id()
        actor = real_user_id()
        now = ts()

        client_id = int(payload.get('client_id') or 0)
        if not client_id:
            raise ValidationError('client_id is required.')
        # Verify client belongs to tenant
        client = conn.execute(
            'SELECT id, name FROM crm_clients WHERE id=? AND company_id=? AND is_active=1',
            (client_id, company_id)).fetchone()
        if not client:
            raise ValidationError('Client not found.', 404)

        atype = (payload.get('activity_type') or 'note').strip().lower()
        if atype not in ACTIVITY_TYPES:
            raise ValidationError(f'Invalid activity_type. Allowed: {", ".join(sorted(ACTIVITY_TYPES))}')

        subject = (payload.get('subject') or '').strip()
        body = (payload.get('body') or '').strip()
        outcome = (payload.get('outcome') or '').strip()
        due_at = (payload.get('due_at') or '').strip()
        # Validate due_at if provided
        if due_at and not _parse_iso(due_at):
            raise ValidationError('due_at must be ISO format (YYYY-MM-DDTHH:MM:SS).')

        # A note/email/meeting/call with no due date is "logged history" → done immediately.
        # A followup/task always stays open until completed.
        if atype in ('followup', 'task'):
            status = 'open'
            completed_at = ''
        elif due_at:
            status = 'open'      # scheduled meeting/call in future
            completed_at = ''
        else:
            status = 'done'      # logged past activity
            completed_at = now

        owner = int(payload.get('owner_user_id') or actor)
        meta = ''
        if payload.get('meta') is not None:
            try:
                meta = json.dumps(payload['meta'])
            except Exception:
                meta = ''

        data = {
            'company_id': company_id, 'client_id': client_id,
            'contact_id': int(payload.get('contact_id') or 0),
            'activity_type': atype, 'subject': subject, 'body': body,
            'outcome': outcome, 'due_at': due_at, 'completed_at': completed_at,
            'status': status, 'owner_user_id': owner, 'meta': meta,
            'actor': actor, 'now': now,
        }
        aid = ActivityRepo.insert(conn, data)
        conn.commit()

        log_activity(f'bd.{atype}',
                     f'{atype.title()} logged for "{client["name"]}"'
                     + (f': {subject}' if subject else ''),
                     entity_type='client', entity_id=client_id,
                     meta={'activity_id': aid, 'type': atype, 'due_at': due_at})
        return aid

    @staticmethod
    def complete(conn, aid, outcome=''):
        company_id = effective_company_id()
        actor = real_user_id()
        now = ts()
        act = ActivityRepo.get(conn, company_id, aid)
        if not act:
            raise ValidationError('Activity not found.', 404)
        fields = {'status': 'done', 'completed_at': now}
        if outcome:
            fields['outcome'] = outcome.strip()
        ActivityRepo.update(conn, aid, fields, actor, now)
        conn.commit()
        log_activity('bd.completed',
                     f'{act["activity_type"].title()} marked done',
                     entity_type='client', entity_id=act['client_id'],
                     meta={'activity_id': aid})

    @staticmethod
    def update(conn, aid, payload):
        company_id = effective_company_id()
        actor = real_user_id()
        now = ts()
        act = ActivityRepo.get(conn, company_id, aid)
        if not act:
            raise ValidationError('Activity not found.', 404)
        fields = {}
        for f in ['subject', 'body', 'outcome']:
            if f in payload:
                fields[f] = (payload.get(f) or '').strip()
        if 'due_at' in payload:
            due = (payload.get('due_at') or '').strip()
            if due and not _parse_iso(due):
                raise ValidationError('due_at must be ISO format.')
            fields['due_at'] = due
        if 'status' in payload:
            st = (payload.get('status') or '').strip().lower()
            if st not in ACTIVITY_STATUS:
                raise ValidationError('Invalid status.')
            fields['status'] = st
            if st == 'done' and not act['completed_at']:
                fields['completed_at'] = now
        if 'owner_user_id' in payload:
            fields['owner_user_id'] = int(payload.get('owner_user_id') or 0)
        if not fields:
            return
        ActivityRepo.update(conn, aid, fields, actor, now)
        conn.commit()
        log_activity('bd.updated', f'{act["activity_type"].title()} updated',
                     entity_type='client', entity_id=act['client_id'],
                     meta={'activity_id': aid})

    @staticmethod
    def delete(conn, aid):
        company_id = effective_company_id()
        actor = real_user_id()
        now = ts()
        act = ActivityRepo.get(conn, company_id, aid)
        if not act:
            raise ValidationError('Activity not found.', 404)
        ActivityRepo.update(conn, aid, {'is_active': 0}, actor, now)
        conn.commit()
        log_activity('bd.deleted', f'{act["activity_type"].title()} deleted',
                     entity_type='client', entity_id=act['client_id'],
                     meta={'activity_id': aid})


# ══════════════════════════════════════════════════════════════════════════
#  COMMAND CENTER  (the aggregation logic)
# ══════════════════════════════════════════════════════════════════════════
def _client_name_map(conn, company_id):
    rows = conn.execute(
        'SELECT id, name, status FROM crm_clients WHERE company_id=? AND is_active=1',
        (company_id,)).fetchall()
    return {r['id']: {'name': r['name'], 'status': r['status']} for r in rows}


def build_command_center(conn, company_id, scope_me=False):
    """Aggregate everything a BD person needs for their day."""
    now = _now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
    now_iso = now.isoformat()
    stale_days = int(float(_get_setting('bd_stale_days', '21') or 21))

    owner = real_user_id() if scope_me else None
    cmap = _client_name_map(conn, company_id)

    def _decorate(rows):
        out = []
        for r in rows:
            d = _activity_public(r)
            ci = cmap.get(r['client_id'], {})
            d['client_name'] = ci.get('name', 'Unknown')
            out.append(d)
        return out

    # Today's follow-ups + tasks (due today)
    todays = _decorate(ActivityRepo.due_between(conn, company_id, today_start, today_end, owner))

    # Overdue (due before now, still open)
    overdue = _decorate(ActivityRepo.overdue(conn, company_id, today_start, owner))

    # Upcoming meetings (next 7 days)
    week_end = (now + datetime.timedelta(days=7)).isoformat()
    meetings = _decorate(ActivityRepo.by_type_due(conn, company_id, 'meeting',
                                                   now_iso, week_end, owner))
    calls = _decorate(ActivityRepo.by_type_due(conn, company_id, 'call',
                                                now_iso, today_end, owner))

    # New prospects (clients with status=prospect)
    prospect_rows = conn.execute(
        "SELECT id, name, industry, city, created_at FROM crm_clients "
        "WHERE company_id=? AND is_active=1 AND status='prospect' "
        "ORDER BY created_at DESC LIMIT 20", (company_id,)).fetchall()
    prospects = [dict(r) for r in prospect_rows]

    # Stale clients: active clients with no activity in stale_days
    stale = []
    cutoff = (now - datetime.timedelta(days=stale_days)).isoformat()
    active_clients = conn.execute(
        "SELECT id, name, industry, city FROM crm_clients "
        "WHERE company_id=? AND is_active=1 AND status='active'", (company_id,)).fetchall()
    for cl in active_clients:
        last = conn.execute(
            'SELECT MAX(COALESCE(NULLIF(created_at,\'\'), due_at)) AS last_at '
            'FROM crm_activities WHERE client_id=? AND is_active=1', (cl['id'],)).fetchone()
        last_at = last['last_at'] if last else None
        if not last_at or last_at < cutoff:
            days = None
            if last_at:
                dt = _parse_iso(last_at)
                if dt:
                    days = (now - dt).days
            stale.append({'id': cl['id'], 'name': cl['name'], 'industry': cl['industry'],
                          'city': cl['city'], 'days_silent': days, 'last_at': last_at})
    stale.sort(key=lambda x: (x['days_silent'] is None, -(x['days_silent'] or 0)), reverse=False)
    stale = sorted(stale, key=lambda x: (x['days_silent'] or 9999), reverse=True)[:20]

    # Open requirements per client (from mandates linked by client name)
    # Mandates use a free-text `client` field; match against crm_clients names.
    open_reqs = []
    try:
        mrows = conn.execute(
            "SELECT id, role, client, status FROM mandates WHERE owner_id=? "
            "AND (status IS NULL OR status='active')", (company_id,)).fetchall()
        for m in mrows:
            open_reqs.append({'mandate_id': m['id'], 'role': m['role'],
                              'client': m['client'], 'status': m['status'] or 'active'})
    except Exception:
        pass

    return {
        'todays_followups': todays,
        'overdue': overdue,
        'meetings': meetings,
        'calls': calls,
        'prospects': prospects,
        'stale_clients': stale,
        'open_requirements': open_reqs,
        'stale_days': stale_days,
        'counts': {
            'todays': len(todays), 'overdue': len(overdue),
            'meetings': len(meetings), 'calls': len(calls),
            'prospects': len(prospects), 'stale': len(stale),
            'open_requirements': len(open_reqs),
        }
    }


def _get_setting(key, default=''):
    try:
        from modules.shared import _core
        return _core().get_setting(key, default)
    except Exception:
        return default


# ══════════════════════════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════════════════════════
def _err(e):
    return jsonify({'error': e.message}), e.code


@bp.route('/command-center', methods=['GET'])
@login_required
def command_center():
    scope_me = request.args.get('scope') == 'me'
    conn = get_db()
    data = build_command_center(conn, effective_company_id(), scope_me)
    conn.close()
    return jsonify({'ok': True, **data})


@bp.route('/clients/<int:client_id>/timeline', methods=['GET'])
@login_required
def client_timeline(client_id):
    company_id = effective_company_id()
    conn = get_db()
    client = conn.execute(
        'SELECT name FROM crm_clients WHERE id=? AND company_id=? AND is_active=1',
        (client_id, company_id)).fetchone()
    if not client:
        conn.close(); return jsonify({'error': 'Client not found'}), 404
    rows = ActivityRepo.timeline(conn, company_id, client_id, limit=200)
    conn.close()
    return jsonify({'ok': True, 'client_name': client['name'],
                    'activities': [_activity_public(r) for r in rows]})


@bp.route('/activities', methods=['POST'])
@login_required
def create_activity():
    conn = get_db()
    try:
        aid = ActivityService.create(conn, request.json or {})
        row = ActivityRepo.get(conn, effective_company_id(), aid)
        conn.close()
        return jsonify({'ok': True, 'activity': _activity_public(row)})
    except ValidationError as e:
        conn.close(); return _err(e)


@bp.route('/activities/<int:aid>', methods=['PUT'])
@login_required
def update_activity(aid):
    conn = get_db()
    try:
        ActivityService.update(conn, aid, request.json or {})
        row = ActivityRepo.get(conn, effective_company_id(), aid)
        conn.close()
        return jsonify({'ok': True, 'activity': _activity_public(row) if row else None})
    except ValidationError as e:
        conn.close(); return _err(e)


@bp.route('/activities/<int:aid>/complete', methods=['POST'])
@login_required
def complete_activity(aid):
    d = request.json or {}
    conn = get_db()
    try:
        ActivityService.complete(conn, aid, d.get('outcome', ''))
        conn.close()
        return jsonify({'ok': True})
    except ValidationError as e:
        conn.close(); return _err(e)


@bp.route('/activities/<int:aid>', methods=['DELETE'])
@login_required
def delete_activity(aid):
    conn = get_db()
    try:
        ActivityService.delete(conn, aid)
        conn.close()
        return jsonify({'ok': True})
    except ValidationError as e:
        conn.close(); return _err(e)


@bp.route('/meta', methods=['GET'])
@login_required
def bd_meta():
    return jsonify({'ok': True,
                    'activity_types': sorted(ACTIVITY_TYPES),
                    'statuses': sorted(ACTIVITY_STATUS)})
