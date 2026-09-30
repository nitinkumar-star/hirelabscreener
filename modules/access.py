"""
RecruitOS — Recruiter access layer  (security fix, Sep 2026)

ROOT CAUSE this module fixes
----------------------------
Tenant isolation works at COMPANY level: every row stores owner_id/company_id
and core routes filter on it. A recruiter sub-account belongs to the same
company as the admin, so it passed every one of those checks. The per-person
rule ("a recruiter only works on the mandates assigned to them") existed in
three places only (mandate list, mandate open, JD). An audit that called every
API route as a recruiter found 37 read routes returning the admin's data and
36 write routes that changed or deleted it (candidates, invoices, interviews,
reminders, the company mailbox...).

THE FIX (one place, not 70 patches)
-----------------------------------
1. mandate_assignees  — a mandate can have many recruiters. mandates.
   assigned_user_id stays as the PRIMARY recruiter; SQLite triggers keep the
   two in step no matter which code path writes assigned_user_id.
2. A request guard for recruiter sessions:
     a. admin-only modules answer 403 (Invoicing, CRM, BD, Email Box,
        Analytics, Campaigns, Command Center, Org Map, Users, settings writes…)
     b. every URL that names an object (/candidates/<id>, /mandates/<id>,
        /interviews/<id>, /reminders/<id>, CV files …) is resolved to its
        mandate and must be one the recruiter is assigned to — for reads AND
        writes, including routes added in future that follow the same URLs.
        An /api path with an id this guard cannot resolve is refused, so a new
        object route is closed until someone maps it here.
     c. ids passed in the query string or JSON/form body (mandate_id,
        candidate_id, type+ref_id …) are checked the same way.
3. SQL helpers (mandate_scope_sql / candidate_scope_sql) that list endpoints
   add to their WHERE clause so lists only contain the recruiter's work.

Central Database (talent pool): recruiters may search it, open a pool
candidate read-only, and pull a pool candidate into one of THEIR mandates.

Who is scoped: a logged-in user who is not a platform admin, not a company
admin, not a freelancer (they have their own stricter guard) and not a super-
admin viewing-as a tenant. Admins are never affected.

Emergency switch: env RECRUITER_API_GUARD=off disables the guard (not the SQL
list scoping) without a redeploy.
"""

import os
import re
import json
from flask import Blueprint, request, jsonify, session, g

from modules.shared import get_db, ts, effective_company_id, real_user_id, is_company_admin, login_required, log_activity
from modules import register_migration

bp = Blueprint('access', __name__, url_prefix='/api/access')

FREELANCER_ROLE = 'freelancer_sourcer'


# ══════════════════════════════════════════════════════════════════════════
#  MIGRATION  (additive)
# ══════════════════════════════════════════════════════════════════════════
@register_migration
def migrate(conn):
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS mandate_assignees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        mandate_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        is_primary INTEGER DEFAULT 0,
        added_by INTEGER DEFAULT 0,
        added_at TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1
    )''')
    for sql in [
        'CREATE UNIQUE INDEX IF NOT EXISTS ux_ma_mandate_user ON mandate_assignees(mandate_id, user_id)',
        'CREATE INDEX IF NOT EXISTS idx_ma_user ON mandate_assignees(user_id, company_id, is_active)',
    ]:
        try:
            c.execute(sql)
        except Exception:
            pass

    # Keep the primary recruiter (mandates.assigned_user_id) mirrored into
    # mandate_assignees whatever code path writes it — mandate create, the
    # legacy single "assign", mandate edit, imports, the mobile app.
    c.execute('''CREATE TRIGGER IF NOT EXISTS trg_ma_mandate_insert
        AFTER INSERT ON mandates
        WHEN COALESCE(NEW.assigned_user_id,0)>0 AND COALESCE(NEW.status,'')!='central'
        BEGIN
            INSERT OR IGNORE INTO mandate_assignees (company_id, mandate_id, user_id, is_primary, added_by, added_at, is_active)
                VALUES (COALESCE(NEW.owner_id,0), NEW.id, NEW.assigned_user_id, 1, NEW.assigned_user_id, datetime('now'), 1);
            UPDATE mandate_assignees SET is_active=1, is_primary=1
                WHERE mandate_id=NEW.id AND user_id=NEW.assigned_user_id;
        END''')
    # Changing the primary: the previous primary is removed (legacy single-
    # assignment meaning). The multi-recruiter API writes the full set right
    # after, so co-recruiters it wants to keep are re-activated there.
    c.execute('''CREATE TRIGGER IF NOT EXISTS trg_ma_mandate_reassign
        AFTER UPDATE OF assigned_user_id ON mandates
        WHEN COALESCE(OLD.assigned_user_id,0) != COALESCE(NEW.assigned_user_id,0)
        BEGIN
            UPDATE mandate_assignees SET is_active=0, is_primary=0
                WHERE mandate_id=NEW.id AND user_id=COALESCE(OLD.assigned_user_id,0);
            UPDATE mandate_assignees SET is_primary=0
                WHERE mandate_id=NEW.id AND user_id!=COALESCE(NEW.assigned_user_id,0);
            INSERT OR IGNORE INTO mandate_assignees (company_id, mandate_id, user_id, is_primary, added_by, added_at, is_active)
                SELECT COALESCE(NEW.owner_id,0), NEW.id, NEW.assigned_user_id, 1, NEW.assigned_user_id, datetime('now'), 1
                WHERE COALESCE(NEW.assigned_user_id,0)>0;
            UPDATE mandate_assignees SET is_active=1, is_primary=1
                WHERE mandate_id=NEW.id AND user_id=COALESCE(NEW.assigned_user_id,0);
        END''')
    c.execute('''CREATE TRIGGER IF NOT EXISTS trg_ma_mandate_owner
        AFTER UPDATE OF owner_id ON mandates
        BEGIN
            UPDATE mandate_assignees SET company_id=COALESCE(NEW.owner_id,0) WHERE mandate_id=NEW.id;
        END''')

    # Who created a task. Standalone tasks (no candidate) were visible to the
    # whole company; now a recruiter sees their own. 0 = created before this.
    try:
        c.execute('ALTER TABLE reminders ADD COLUMN created_by INTEGER DEFAULT 0')
    except Exception:
        pass

    # Backfill existing assignments (idempotent: INSERT OR IGNORE never
    # re-activates a recruiter an admin has removed).
    c.execute('''INSERT OR IGNORE INTO mandate_assignees
                   (company_id, mandate_id, user_id, is_primary, added_by, added_at, is_active)
                 SELECT COALESCE(owner_id,0), id, assigned_user_id, 1, assigned_user_id, COALESCE(created_at,''), 1
                   FROM mandates
                  WHERE COALESCE(assigned_user_id,0)>0 AND COALESCE(status,'')!='central' ''')


# ══════════════════════════════════════════════════════════════════════════
#  WHO IS SCOPED
# ══════════════════════════════════════════════════════════════════════════
def _session_user():
    uid = session.get('user_id')
    if not uid:
        return None
    cached = g.get('_acc_user', False)
    if cached is not False:
        return cached
    conn = get_db()
    try:
        u = conn.execute('SELECT id, role, company_id, COALESCE(is_company_admin,0) AS is_company_admin, '
                         'status FROM users WHERE id=?', (uid,)).fetchone()
    finally:
        conn.close()
    g._acc_user = dict(u) if u else None
    return g._acc_user


def scoped_user():
    """(user_id, company_id) when this request belongs to a recruiter whose
    view must be limited to their assigned mandates, else None."""
    try:
        if session.get('view_as_company'):
            return None                      # super-admin acting as the tenant's admin
        u = _session_user()
    except RuntimeError:                     # no request context (background thread)
        return None
    if not u:
        return None
    if u['role'] in ('admin', FREELANCER_ROLE) or int(u['is_company_admin'] or 0) == 1:
        return None
    return (int(u['id']), int(u['company_id'] or 0))


def is_scoped():
    return scoped_user() is not None


def pool_mandate_id(conn, company_id):
    """The tenant's Central Database pool mandate id (never creates it)."""
    try:
        r = conn.execute("SELECT value FROM tenant_settings WHERE company_id=? AND key='central_mandate_id'",
                         (company_id,)).fetchone()
        if r and str(r['value'] or '').strip().isdigit():
            return int(r['value'])
    except Exception:
        pass
    r = conn.execute("SELECT id FROM mandates WHERE owner_id=? AND status='central' ORDER BY id LIMIT 1",
                     (company_id,)).fetchone()
    return int(r['id']) if r else 0


def allowed_mandate_ids(conn=None):
    """Mandates the scoped recruiter is assigned to (None = unrestricted)."""
    su = scoped_user()
    if not su:
        return None
    cached = g.get('_acc_mids')
    if cached is not None:
        return cached
    own = conn is None
    conn = conn or get_db()
    try:
        rows = conn.execute(
            'SELECT a.mandate_id FROM mandate_assignees a JOIN mandates m ON m.id=a.mandate_id '
            'WHERE a.user_id=? AND a.company_id=? AND a.is_active=1 AND m.owner_id=?',
            (su[0], su[1], su[1])).fetchall()
        ids = {int(r['mandate_id']) for r in rows}
        g._acc_pool = pool_mandate_id(conn, su[1])
    finally:
        if own:
            conn.close()
    g._acc_mids = ids
    return ids


def _pool_id(conn=None):
    if g.get('_acc_pool') is None:
        allowed_mandate_ids(conn)
    return g.get('_acc_pool') or 0


def user_mandate_ids(conn, user_id, company_id):
    """Assigned mandates for any user (used by per-recruiter dashboards)."""
    return {int(r['mandate_id']) for r in conn.execute(
        'SELECT mandate_id FROM mandate_assignees WHERE user_id=? AND company_id=? AND is_active=1',
        (user_id, company_id))}


# ── SQL fragments for list endpoints ────────────────────────────────────────
def mandate_scope_sql(col):
    """(' AND <col> IN (...)', params) limiting a query to the recruiter's
    assigned mandates; ('', []) for everyone else. `col` is trusted SQL."""
    su = scoped_user()
    if not su:
        return '', []
    return (f' AND {col} IN (SELECT mandate_id FROM mandate_assignees '
            f'WHERE user_id=? AND company_id=? AND is_active=1)', [su[0], su[1]])


def candidate_scope_sql(mandate_col, include_pool=True):
    """Like mandate_scope_sql, plus (optionally) the Central Database pool."""
    su = scoped_user()
    if not su:
        return '', []
    frag, params = mandate_scope_sql(mandate_col)
    if not include_pool:
        return frag, params
    pool = _pool_id()
    inner = frag[len(' AND '):]
    return f' AND ({inner} OR {mandate_col}=?)', params + [pool]


def candidate_id_scope_sql(col, include_pool=False):
    """(' AND <col> IN (candidate ids the recruiter may see)', params) for rows
    that point at a candidate (reminders, interviews, conversations …)."""
    su = scoped_user()
    if not su:
        return '', []
    inner, params = candidate_scope_sql('k.mandate_id', include_pool=include_pool)
    return (f' AND {col} IN (SELECT k.id FROM candidates k WHERE k.owner_id=?{inner})',
            [su[1]] + params)


def reminder_scope_sql(alias='r'):
    """Tasks/reminders a recruiter may see: on their candidates, or standalone
    tasks they created themselves."""
    su = scoped_user()
    if not su:
        return '', []
    inner, params = candidate_id_scope_sql(f'{alias}.candidate_id')
    return (f' AND (({inner[len(" AND "):]}) OR (COALESCE({alias}.candidate_id,0)=0 '
            f'AND COALESCE({alias}.created_by,0)=?))', params + [su[0]])


def can_see_mandate(conn, mid, write=False):
    ids = allowed_mandate_ids(conn)
    if ids is None:
        return True
    try:
        mid = int(mid)
    except Exception:
        return False
    if mid in ids:
        return True
    return (not write) and mid == _pool_id(conn) and mid != 0


def can_see_candidate(conn, cid, write=False):
    ids = allowed_mandate_ids(conn)
    if ids is None:
        return True
    su = scoped_user()
    r = conn.execute('SELECT mandate_id, owner_id FROM candidates WHERE id=?', (cid,)).fetchone()
    if not r or int(r['owner_id'] or 0) != su[1]:
        return False
    return can_see_mandate(conn, r['mandate_id'], write=write)


# ══════════════════════════════════════════════════════════════════════════
#  GUARD RULES
# ══════════════════════════════════════════════════════════════════════════
ANY = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')
W = ('POST', 'PUT', 'PATCH', 'DELETE')

# Modules a recruiter never uses (decided with the owner, Sep 2026).
_ADMIN_ONLY = [(ANY, r'^/api/(activity|audit|analytics|bd|billing|campaigns|command|crm|crm-link|emailbox|'
                     r'expenses|export|import|invoices|movement|org|rme|rkg|users|freelancers|admin|ses|'
                     r'vector|my-team|team|diag|companies)(/|$)'),
               (ANY, r'^/api/email/(?!signature/?$)'),               # company mailbox setup / sync
               (W,   r'^/api/email/signature/?$'),
               (ANY, r'^/api/ai/(?!search/?$)'),                     # index/queue maintenance
               (W,   r'^/api/(settings|form-config|email-templates|wa-templates|skill-graph)/?$'),
               (W,   r'^/api/workspace/vocab/?$'),
               (W,   r'^/api/email-agent/(kb|scan)/?$'),
               (ANY, r'^/api/wa/(learned|webhook)(/|$)'),
               (ANY, r'^/api/(wa-learn-style|wa-auto-categories|wa-inbound-config)/?$'),
               (W,   r'^/api/wa-suggestions/scan-now/?$'),
               (W,   r'^/api/(departments|hiring-managers)(/|$)'),
               (W,   r'^/api/xp/(recompute-all|source)/?$'),
               (W,   r'^/api/mandates/\d+/(assign|assignees|freelancers|approval)(/|$)'),
               (W,   r'^/api/access/mandates/\d+/assignees/?$'),
               (('DELETE',), r'^/api/mandates/\d+/?$')]
_ADMIN_ONLY = [(m, re.compile(p)) for m, p in _ADMIN_ONLY]

# Object URLs -> kind of id. Order matters (more specific first).
_OBJECT_ROUTES = [(re.compile(p), k) for p, k in [
    (r'^/api/candidates/(\d+)(/|$)', 'candidate'),
    (r'^/api/xp/candidate/(\d+)(/|$)', 'candidate'),
    (r'^/api/mandates/(\d+)(/|$)', 'mandate'),
    (r'^/api/access/mandates/(\d+)(/|$)', 'mandate'),
    (r'^/api/review/(\d+)(/|$)', 'mandate'),
    (r'^/api/interviews/feedback/(\d+)(/|$)', 'feedback'),
    (r'^/api/interviews/(\d+)(/|$)', 'interview'),
    (r'^/api/scorecards/(\d+)(/|$)', 'feedback'),
    (r'^/api/reminders/(\d+)(/|$)', 'reminder'),
    (r'^/api/submissions/(\d+)(/|$)', 'submission'),
    (r'^/api/submission-drafts/(\d+)(/|$)', 'draft'),
    (r'^/api/offers/(\d+)(/|$)', 'offer'),
    (r'^/api/wa/conversations/(\d+)(/|$)', 'waconv'),
    (r'^/api/wa/escalations/(\d+)(/|$)', 'waesc'),
    (r'^/api/wa-suggestions/(\d+)(/|$)', 'wasugg'),
    (r'^/api/email-agent/item/(\d+)(/|$)', 'agentitem'),
    (r'^/api/scheduler/meetings/(\d+)(/|$)', 'meeting'),
    (r'^/api/(?:cv|cv-view)/(.+)$', 'cvfile'),
    (r'^/api/calls/(.+)$', 'callfile'),
]]

# Paths with a number in them that are NOT tenant objects.
_NUMERIC_OK = [re.compile(p) for p in [
    r'^/api/scheduler/public/',
    r'^/api/public/',
    r'^/api/notifications(/|$)',
]]
_HAS_ID = re.compile(r'/\d+(/|$)')

# Pool candidates are read-only for recruiters, except being pulled into
# one of their own mandates.
_POOL_WRITE_OK = re.compile(r'^/api/candidates/\d+/move/?$')

# (table, id column, how to reach the mandate)
_RESOLVE_SQL = {
    'interview':  "SELECT i.mandate_id AS m, i.candidate_id AS c FROM interviews i WHERE i.id=? AND i.owner_id=?",
    'feedback':   "SELECT f.mandate_id AS m, f.candidate_id AS c FROM interview_feedback f WHERE f.id=? AND f.owner_id=?",
    'reminder':   "SELECT r.mandate_id AS m, r.candidate_id AS c, COALESCE(r.created_by,0) AS u FROM reminders r WHERE r.id=? AND r.owner_id=?",
    'submission': "SELECT s.mandate_id AS m, 0 AS c FROM submissions s WHERE s.id=? AND s.owner_id=?",
    'draft':      "SELECT d.mandate_id AS m, 0 AS c FROM submission_drafts d WHERE d.id=? AND d.owner_id=?",
    'offer':      "SELECT o.mandate_id AS m, o.candidate_id AS c FROM offers o WHERE o.id=? AND o.company_id=?",
    'waconv':     "SELECT w.mandate_id AS m, w.candidate_id AS c FROM wa_conversations w WHERE w.id=? AND w.company_id=?",
    'agentitem':  "SELECT a.mandate_id AS m, a.candidate_id AS c FROM agent_items a WHERE a.id=? AND a.owner_id=?",
    'outreach':   "SELECT o.mandate_id AS m, o.candidate_id AS c FROM outreach_log o WHERE o.id=? AND o.owner_id=?",
}


def _deny(msg='Not found', code=404):
    return jsonify({'error': msg}), code


def _obj_ok(conn, kind, ident, write, path):
    """Can the scoped recruiter touch this object?"""
    su = scoped_user()
    if kind == 'candidate':
        pool_ok = (not write) or bool(_POOL_WRITE_OK.match(path))
        return can_see_candidate(conn, int(ident), write=not pool_ok)
    if kind == 'mandate':
        return can_see_mandate(conn, int(ident), write=write)
    if kind == 'meeting':
        r = conn.execute('SELECT host_user_id, candidate_id FROM meetings WHERE id=? AND company_id=?',
                         (int(ident), su[1])).fetchone()
        if not r:
            return False
        return int(r['host_user_id'] or 0) == su[0] or (
            bool(r['candidate_id']) and can_see_candidate(conn, r['candidate_id'], write=write))
    if kind == 'cvfile':
        safe = os.path.basename(ident or '')
        r = conn.execute('SELECT id FROM candidates WHERE cv_path=? AND owner_id=? ORDER BY id DESC LIMIT 1',
                         (safe, su[1])).fetchone()
        return bool(r) and can_see_candidate(conn, r['id'])
    if kind == 'callfile':
        m = re.match(r'call_(\d+)_', os.path.basename(ident or ''))
        return bool(m) and can_see_candidate(conn, int(m.group(1)))
    if kind in ('waesc', 'wasugg'):
        table = 'wa_escalations' if kind == 'waesc' else 'wa_suggestions'
        try:
            r = conn.execute(f'SELECT * FROM {table} WHERE id=?', (int(ident),)).fetchone()
        except Exception:
            return False
        if not r:
            return False
        d = dict(r)
        if d.get('company_id') not in (None, su[1]) and d.get('owner_id') not in (None, su[1]):
            return False
        if d.get('candidate_id'):
            return can_see_candidate(conn, d['candidate_id'], write=write)
        if d.get('mandate_id'):
            return can_see_mandate(conn, d['mandate_id'], write=write)
        if d.get('conversation_id'):
            return _obj_ok(conn, 'waconv', d['conversation_id'], write, path)
        return False
    sql = _RESOLVE_SQL.get(kind)
    if not sql:
        return False
    r = conn.execute(sql, (int(ident), su[1])).fetchone()
    if not r:
        return False
    if kind == 'reminder' and not r['c'] and not r['m']:
        return int(r['u'] or 0) == su[0]                 # own standalone task
    if r['m']:
        return can_see_mandate(conn, r['m'], write=write)
    if r['c']:
        return can_see_candidate(conn, r['c'], write=write)
    return False


_TASK_TYPES = {'reminder': 'reminder', 'interview': 'interview', 'submission': 'submission',
               'stale': 'candidate', 'promise': 'candidate', 'updated': 'candidate'}


def _collect_ids(src):
    """Pull ids a request names in its query string / body / form."""
    out = []
    if not isinstance(src, dict):
        return out
    for key, kind in (('candidate_id', 'candidate'), ('cid', 'candidate'),
                      ('mandate_id', 'mandate'), ('mid', 'mandate'),
                      ('target_mandate_id', 'mandate'), ('to_mandate_id', 'mandate')):
        v = src.get(key)
        if v not in (None, '', 0, '0'):
            out.append((kind, v))
    for key, kind in (('candidate_ids', 'candidate'), ('mandate_ids', 'mandate')):
        v = src.get(key)
        if isinstance(v, list):
            out.extend((kind, x) for x in v[:500] if x not in (None, '', 0, '0'))
    if src.get('type') in _TASK_TYPES and src.get('ref_id') not in (None, '', 0):
        out.append((_TASK_TYPES[src['type']], src['ref_id']))
    return out


def _check_ids(conn, pairs, write, path):
    for kind, v in pairs:
        try:
            ident = int(v)
        except Exception:
            return False
        if kind == 'mandate':
            # a body mandate_id on a write is where work goes: must be assigned
            # (the pool may only be read) — except parking one's own candidate
            # back in the Central Database via /move.
            if write and _POOL_WRITE_OK.match(path) and ident == _pool_id(conn) and ident:
                continue
            if not can_see_mandate(conn, ident, write=write):
                return False
        elif kind == 'candidate':
            pool_ok = (not write) or bool(_POOL_WRITE_OK.match(path))
            if not can_see_candidate(conn, ident, write=not pool_ok):
                return False
        elif not _obj_ok(conn, kind, ident, write, path):
            return False
    return True


@bp.before_app_request
def _recruiter_guard():
    path = request.path or ''
    if not path.startswith('/api/') or request.method in ('OPTIONS', 'HEAD'):
        return None
    if (os.environ.get('RECRUITER_API_GUARD', 'on') or 'on').strip().lower() == 'off':
        return None
    try:
        su = scoped_user()
    except Exception:
        su = None
    if not su:
        return None
    method = request.method
    write = method in W

    # a) admin-only modules
    for methods, rx in _ADMIN_ONLY:
        if method in methods and rx.match(path):
            return _deny('Only an admin can access this.', 403)

    conn = get_db()
    try:
        # b) object URLs
        matched = False
        for rx, kind in _OBJECT_ROUTES:
            m = rx.match(path)
            if m:
                matched = True
                if not _obj_ok(conn, kind, m.group(1), write, path):
                    return _deny()
                break
        if not matched and _HAS_ID.search(path) and not any(rx.match(path) for rx in _NUMERIC_OK):
            print(f'[access] refused unmapped object route for recruiter uid={su[0]}: {method} {path}')
            return _deny('Not available for recruiter accounts.', 403)

        # c) ids named in the query string / body / form
        pairs = _collect_ids(request.args.to_dict())
        if write:
            body = request.get_json(silent=True)
            pairs += _collect_ids(body if isinstance(body, dict) else {})
            if request.form:
                pairs += _collect_ids(request.form.to_dict())
        if write and path.rstrip('/') == '/api/outreach/update':
            body = request.get_json(silent=True) or {}
            if body.get('id') not in (None, ''):
                pairs.append(('outreach', body.get('id')))
        if pairs and not _check_ids(conn, pairs, write, path):
            return _deny()

        # d) a recruiter cannot hand a mandate to someone else (or take one)
        if write:
            body = request.get_json(silent=True)
            if isinstance(body, dict) and 'assigned_user_id' in body:
                m = re.match(r'^/api/mandates/(\d+)/?$', path)
                cur = None
                if m:
                    r = conn.execute('SELECT assigned_user_id FROM mandates WHERE id=?', (int(m.group(1)),)).fetchone()
                    cur = int(r['assigned_user_id'] or 0) if r else None
                try:
                    want = int(body.get('assigned_user_id') or 0)
                except Exception:
                    want = -1
                # editing an existing mandate: the value must stay as it is;
                # creating one: it can only be the recruiter themselves
                allowed = (want == cur) if cur is not None else (want in (0, su[0]))
                if not allowed:
                    return _deny('Only an admin can change who a position is assigned to.', 403)
    except Exception as e:
        print(f'[access] check failed ({method} {path}): {e}')
        return _deny('Access check failed.', 403)
    finally:
        conn.close()
    return None


# ══════════════════════════════════════════════════════════════════════════
#  MULTI-RECRUITER ASSIGNMENT API
# ══════════════════════════════════════════════════════════════════════════
def mandate_assignees(conn, mid):
    return [dict(r) for r in conn.execute(
        'SELECT a.user_id, a.is_primary, a.added_at, COALESCE(NULLIF(u.display_name,\'\'), u.username) AS name '
        'FROM mandate_assignees a JOIN users u ON u.id=a.user_id '
        'WHERE a.mandate_id=? AND a.is_active=1 ORDER BY a.is_primary DESC, name COLLATE NOCASE', (mid,))]


def assignees_by_mandate(conn, company_id):
    out = {}
    for r in conn.execute(
            'SELECT a.mandate_id, a.user_id, a.is_primary, COALESCE(NULLIF(u.display_name,\'\'), u.username) AS name '
            'FROM mandate_assignees a JOIN users u ON u.id=a.user_id '
            'WHERE a.company_id=? AND a.is_active=1 ORDER BY a.is_primary DESC, name COLLATE NOCASE',
            (company_id,)):
        out.setdefault(r['mandate_id'], []).append(
            {'user_id': r['user_id'], 'is_primary': r['is_primary'], 'name': r['name']})
    return out


@bp.route('/mandates/<int:mid>/assignees', methods=['GET'])
@login_required
def get_assignees(mid):
    conn = get_db()
    try:
        m = conn.execute('SELECT id FROM mandates WHERE id=? AND owner_id=?', (mid, effective_company_id())).fetchone()
        if not m or not can_see_mandate(conn, mid):
            return _deny()
        return jsonify({'ok': True, 'assignees': mandate_assignees(conn, mid)})
    finally:
        conn.close()


@bp.route('/mandates/<int:mid>/assignees', methods=['PUT'])
@login_required
def set_assignees(mid):
    """Admin sets the full recruiter list for a mandate.
    body: {user_ids: [..], primary_user_id: n}  (primary defaults to the first)"""
    if not is_company_admin():
        return _deny('Only an admin can assign recruiters.', 403)
    d = request.get_json(silent=True) or {}
    company_id = effective_company_id()
    try:
        ids = [int(x) for x in (d.get('user_ids') or [])]
        primary = int(d.get('primary_user_id') or (ids[0] if ids else 0))
    except Exception:
        return _deny('Invalid recruiter list.', 400)
    ids = list(dict.fromkeys(ids))[:50]
    if primary and primary not in ids:
        ids.insert(0, primary)
    conn = get_db()
    try:
        m = conn.execute("SELECT id, role, client, assigned_user_id FROM mandates WHERE id=? AND owner_id=? "
                         "AND COALESCE(status,'')!='central'", (mid, company_id)).fetchone()
        if not m:
            return _deny('Mandate not found')
        if ids:
            ok = {r['id'] for r in conn.execute(
                f"SELECT id FROM users WHERE company_id=? AND status='approved' AND role!=? "
                f"AND id IN ({','.join('?' * len(ids))})", [company_id, FREELANCER_ROLE] + ids)}
            bad = [i for i in ids if i not in ok]
            if bad:
                return _deny('Every recruiter must be an active member of your company.', 400)
        before = {a['user_id'] for a in mandate_assignees(conn, mid)}
        now, actor = ts(), real_user_id()
        # primary first (the reassign trigger runs here), then the full set
        conn.execute('UPDATE mandates SET assigned_user_id=? WHERE id=?', (primary or 0, mid))
        conn.execute('UPDATE mandate_assignees SET is_active=0, is_primary=0 WHERE mandate_id=?', (mid,))
        for uid in ids:
            conn.execute('INSERT OR IGNORE INTO mandate_assignees (company_id, mandate_id, user_id, is_primary, '
                         'added_by, added_at, is_active) VALUES (?,?,?,?,?,?,1)',
                         (company_id, mid, uid, 1 if uid == primary else 0, actor, now))
            conn.execute('UPDATE mandate_assignees SET is_active=1, is_primary=?, company_id=? '
                         'WHERE mandate_id=? AND user_id=?', (1 if uid == primary else 0, company_id, mid, uid))
        conn.commit()
        after = mandate_assignees(conn, mid)
        added = [a for a in after if a['user_id'] not in before]
        removed = before - {a['user_id'] for a in after}
        log_activity('mandate.assignees', f"{m['role']} @ {m['client']}: "
                     + ', '.join(a['name'] + (' (primary)' if a['is_primary'] else '') for a in after),
                     entity_type='mandate', entity_id=mid,
                     meta={'added': [a['user_id'] for a in added], 'removed': sorted(removed)})
        _notify_added(conn, company_id, m, added, actor)
        return jsonify({'ok': True, 'assignees': after})
    finally:
        conn.close()


def _notify_added(conn, company_id, m, added, actor):
    """Tell newly added recruiters (best effort — never blocks the save)."""
    if not added:
        return
    try:
        who = conn.execute("SELECT COALESCE(NULLIF(display_name,''), username) n FROM users WHERE id=?",
                           (actor,)).fetchone()
        now = ts()
        for a in added:
            if a['user_id'] == actor:
                continue
            conn.execute('INSERT INTO notifications (company_id, user_id, kind, title, body, mandate_id, actor_id, '
                         'actor_name, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (company_id, a['user_id'], 'mandate_assigned',
                          f"New position assigned: {m['role']}",
                          f"{m['role']} @ {m['client']}", m['id'], actor, who['n'] if who else '', now, now))
        conn.commit()
    except Exception as e:
        print(f'[access] assignment notification skipped: {e}')
