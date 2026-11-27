"""
RecruitOS — Memory (shared business memory for the team and for Claude)
Wave 1: tables + REST API.   (Nov 2026)

WHAT IT IS
----------
Durable business notes that live inside the ATS: client preferences, rejection
patterns, who handles what, standing instructions, time-bound items such as an
interview slot. Claude reads them through the MCP connector (recall_memory /
get_memory_context) and writes what it learned (save_memory); the team reads
and edits them in the UI.

Separate from the older RME tables (rme_memories …, server.py "Sprint 5"):
those are left untouched. This module adds its own tables only.

TABLES (additive, CREATE TABLE IF NOT EXISTS)
---------------------------------------------
memory_notes         one note. company_id = tenant; owner_id = tenant as well
                     (same meaning as candidates.owner_id / mandates.owner_id);
                     the person is created_by / updated_by.
                     client_name = the client a person / mandate / candidate
                     note belongs to, so it shows in that client's briefing.
memory_note_history  every create / edit / archive / unarchive (audit).

SCOPES
------
general    agency-wide facts                      entity_id must be empty
client     a client company                        entity_id = crm_clients.id (optional)
mandate    a job                                   entity_id = mandates.id (optional)
candidate  a candidate                             entity_id = candidates.id (optional)
person     a client-side person (HR, hiring mgr)   entity_id = crm_contacts.id (optional)
team       a HireLab team member                   entity_id = users.id (optional)

WHO SEES WHAT
-------------
Admins (company admin / platform owner / view-as): every note of their company.
Recruiter sub-accounts (modules.access.scoped_user):
  read : general + team notes, their own notes, notes on their assigned
         mandates and those mandates' candidates (and Central DB candidates,
         read-only, as everywhere else), and client / person notes of the
         clients behind their assigned mandates.
  write: general notes, and mandate / candidate notes they can write to
         (assigned mandate). Client / person / team notes are admin-only
         to write; a recruiter can edit / archive only notes they may write.
Freelancer accounts never reach /api/memory (allow-list in modules/freelancer.py).
Another company's note answers 404 (modules/tenant_guard.py rule + checks here).

ENDPOINTS
---------
GET  /api/memory                  list / search
GET  /api/memory/<id>             one note + its history
POST /api/memory                  create   (dedupe=true -> update a near-identical note instead)
PUT  /api/memory/<id>             update   (writes a history row)
POST /api/memory/<id>/archive     soft delete
POST /api/memory/<id>/unarchive   restore
GET  /api/memory/context          one briefing for client= | mandate_id= | candidate_id=

Nothing is ever hard-deleted.
"""

import re
import json
import datetime
import difflib

from flask import Blueprint, request, jsonify

from modules.shared import (get_db, ts, effective_company_id, real_user_id,
                            login_required, log_activity, _core)
from modules import register_migration

bp = Blueprint('memory', __name__, url_prefix='/api/memory')

SCOPES = ('general', 'client', 'mandate', 'candidate', 'person', 'team')
IMPORTANCE = ('high', 'normal', 'low')
SOURCES = ('claude', 'user', 'recruiter', 'system')
STATUSES = ('active', 'archived')

# Recruiters may create / edit notes in these scopes only.
RECRUITER_WRITE_SCOPES = ('general', 'mandate', 'candidate')

MAX_TITLE = 200
MAX_CONTENT = 10000
MAX_NAME = 200
MAX_TAGS = 20
MAX_TAG_LEN = 40
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
DEDUPE_RATIO = 0.85          # title similarity at or above this = "same note"

_IMP_ORDER = "CASE n.importance WHEN 'high' THEN 3 WHEN 'normal' THEN 2 ELSE 1 END"
_DATE_RX = re.compile(r'^\d{4}-\d{2}-\d{2}')


# ══════════════════════════════════════════════════════════════════════════
#  MIGRATION  (additive only)
# ══════════════════════════════════════════════════════════════════════════
@register_migration
def migrate(conn):
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS memory_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        owner_id INTEGER NOT NULL DEFAULT 0,
        scope TEXT NOT NULL DEFAULT 'general',
        entity_id INTEGER,
        entity_name TEXT DEFAULT '',
        client_name TEXT DEFAULT '',
        title TEXT DEFAULT '',
        content TEXT DEFAULT '',
        tags TEXT DEFAULT '[]',
        importance TEXT DEFAULT 'normal',
        status TEXT DEFAULT 'active',
        source TEXT DEFAULT 'user',
        valid_until TEXT,
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        created_by INTEGER DEFAULT 0,
        updated_by INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS memory_note_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        note_id INTEGER NOT NULL,
        company_id INTEGER NOT NULL DEFAULT 0,
        action TEXT DEFAULT 'update',
        old_content TEXT DEFAULT '',
        new_content TEXT DEFAULT '',
        changes TEXT DEFAULT '{}',
        changed_by INTEGER DEFAULT 0,
        changed_at TEXT DEFAULT ''
    )''')
    for sql in (
        'CREATE INDEX IF NOT EXISTS idx_memn_scope_entity ON memory_notes(company_id, scope, entity_id)',
        'CREATE INDEX IF NOT EXISTS idx_memn_entity_name ON memory_notes(company_id, entity_name)',
        'CREATE INDEX IF NOT EXISTS idx_memn_status ON memory_notes(company_id, status)',
        'CREATE INDEX IF NOT EXISTS idx_memn_client_name ON memory_notes(company_id, client_name)',
        'CREATE INDEX IF NOT EXISTS idx_memh_note ON memory_note_history(note_id, changed_at)',
    ):
        try:
            c.execute(sql)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
#  SMALL HELPERS
# ══════════════════════════════════════════════════════════════════════════
def _err(msg, code=400):
    return jsonify({'error': msg}), code


def _today():
    try:
        return _core()._ist_now().date().isoformat()
    except Exception:
        return (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)).date().isoformat()


def _norm(s):
    return re.sub(r'\s+', ' ', str(s or '')).strip()


def _key(s):
    """Case/space-insensitive comparison key for names."""
    return _norm(s).lower()


def _int_or_none(v):
    if v in (None, '', 0, '0'):
        return None
    try:
        i = int(v)
    except (TypeError, ValueError):
        raise ValueError('entity_id must be a number')
    return i if i > 0 else None


def _bool_arg(name):
    return str(request.args.get(name) or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _clean_tags(v):
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        if s.startswith('['):
            try:
                v = json.loads(s)
            except Exception:
                v = s.strip('[]').split(',')
        else:
            v = s.split(',')
    if not isinstance(v, (list, tuple)):
        raise ValueError('tags must be a list of strings')
    out, seen = [], set()
    for t in v:
        t = _norm(t)[:MAX_TAG_LEN]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:MAX_TAGS]


def _clean_date(v):
    """'' / None -> None.  'YYYY-MM-DD' or an ISO datetime -> kept as given (trimmed)."""
    s = _norm(v)
    if not s:
        return None
    if not _DATE_RX.match(s):
        raise ValueError('valid_until must be a date like 2026-12-31')
    try:
        datetime.date.fromisoformat(s[:10])
    except ValueError:
        raise ValueError('valid_until is not a real date')
    return s[:25]


def _note_dict(r, today=None):
    d = {k: r[k] for k in r.keys()}
    try:
        d['tags'] = json.loads(d.get('tags') or '[]')
    except Exception:
        d['tags'] = []
    vu = (d.get('valid_until') or '')[:10]
    d['expired'] = bool(vu) and vu < (today or _today())
    return d


def _like(s):
    return '%' + s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


# ══════════════════════════════════════════════════════════════════════════
#  WHO MAY SEE / WRITE  (recruiter scoping)
# ══════════════════════════════════════════════════════════════════════════
def _scoped():
    """(user_id, company_id) for a recruiter sub-account, else None (admin)."""
    try:
        from modules.access import scoped_user
        return scoped_user()
    except Exception:
        return None


class _Scope:
    """Everything a recruiter may read, resolved once per request."""

    def __init__(self, conn, su):
        from modules.access import allowed_mandate_ids, _pool_id
        self.uid, self.cid = su
        self.mids = set(allowed_mandate_ids(conn) or set())
        self.pool = int(_pool_id(conn) or 0)
        names, crm_ids = set(), set()
        if self.mids:
            ph = ','.join('?' * len(self.mids))
            for r in conn.execute(f'SELECT client, COALESCE(crm_client_id,0) AS crm FROM mandates '
                                  f'WHERE owner_id=? AND id IN ({ph})', [self.cid] + list(self.mids)):
                if _key(r['client']):
                    names.add(_key(r['client']))
                if int(r['crm'] or 0):
                    crm_ids.add(int(r['crm']))
            if crm_ids:
                ph2 = ','.join('?' * len(crm_ids))
                try:
                    for r in conn.execute(f'SELECT name FROM crm_clients WHERE company_id=? AND id IN ({ph2})',
                                          [self.cid] + list(crm_ids)):
                        if _key(r['name']):
                            names.add(_key(r['name']))
                except Exception:
                    pass
        self.client_names = names
        self.crm_ids = crm_ids

    def sql(self, a='n'):
        """WHERE fragment (starting with ' AND ') limiting notes to what the recruiter may read."""
        parts = [f"{a}.scope IN ('general','team')", f'{a}.created_by=?']
        params = [self.uid]
        read_mids = set(self.mids) | ({self.pool} if self.pool else set())
        if self.mids:
            ph = ','.join('?' * len(self.mids))
            parts.append(f"({a}.scope='mandate' AND {a}.entity_id IN ({ph}))")
            params += list(self.mids)
        if read_mids:
            ph = ','.join('?' * len(read_mids))
            parts.append(f"({a}.scope='candidate' AND {a}.entity_id IN "
                         f"(SELECT k.id FROM candidates k WHERE k.owner_id=? AND k.mandate_id IN ({ph})))")
            params += [self.cid] + list(read_mids)
        if self.client_names:
            ph = ','.join('?' * len(self.client_names))
            parts.append(f"({a}.scope IN ('client','person') AND "
                         f"(LOWER(TRIM(COALESCE({a}.client_name,''))) IN ({ph})"
                         f" OR ({a}.scope='client' AND LOWER(TRIM(COALESCE({a}.entity_name,''))) IN ({ph}))))")
            params += list(self.client_names) * 2
        if self.crm_ids:
            ph = ','.join('?' * len(self.crm_ids))
            parts.append(f"({a}.scope='client' AND {a}.entity_id IN ({ph}))")
            params += list(self.crm_ids)
        return ' AND (' + ' OR '.join(parts) + ')', params


def _scope_obj(conn):
    su = _scoped()
    return _Scope(conn, su) if su else None


def _can_read(conn, note, sc):
    if sc is None:
        return True
    frag, params = sc.sql('n')
    r = conn.execute('SELECT 1 FROM memory_notes n WHERE n.id=?' + frag, [note['id']] + params).fetchone()
    return bool(r)


def _can_write_target(conn, scope, entity_id, sc):
    """May the caller create / move a note into (scope, entity_id)?"""
    if sc is None:
        return True
    if scope not in RECRUITER_WRITE_SCOPES:
        return False
    if scope == 'mandate':
        return entity_id is not None and entity_id in sc.mids
    if scope == 'candidate':
        if entity_id is None:
            return False
        from modules.access import can_see_candidate
        return can_see_candidate(conn, entity_id, write=True)
    return True                                   # general


def _can_write_note(conn, note, sc):
    if sc is None:
        return True
    if not _can_read(conn, note, sc):
        return False
    eid = note['entity_id']
    return _can_write_target(conn, note['scope'], int(eid) if eid else None, sc)


# ══════════════════════════════════════════════════════════════════════════
#  ENTITY VALIDATION  (an entity_id must belong to the caller's company)
# ══════════════════════════════════════════════════════════════════════════
def _resolve_entity(conn, cid, scope, entity_id):
    """Returns (entity_name, client_name) defaults for a linked entity, or
    raises LookupError if the id is not one of this company's rows."""
    if entity_id is None:
        return '', ''
    if scope == 'general':
        raise ValueError('a general note cannot be linked to an entity_id')
    if scope == 'mandate':
        r = conn.execute('SELECT role, client, location FROM mandates WHERE id=? AND owner_id=?',
                         (entity_id, cid)).fetchone()
        if not r:
            raise LookupError('mandate not found')
        name = ' — '.join(x for x in (_norm(r['role']), _norm(r['location'])) if x)
        return name, _norm(r['client'])
    if scope == 'candidate':
        r = conn.execute('SELECT k.name, m.client, m.status FROM candidates k LEFT JOIN mandates m ON m.id=k.mandate_id '
                         'WHERE k.id=? AND k.owner_id=?', (entity_id, cid)).fetchone()
        if not r:
            raise LookupError('candidate not found')
        client = '' if (r['status'] or '') == 'central' else _norm(r['client'])   # Central DB: no client
        return _norm(r['name']), client
    if scope == 'client':
        try:
            r = conn.execute('SELECT name FROM crm_clients WHERE id=? AND company_id=?',
                             (entity_id, cid)).fetchone()
        except Exception:
            r = None
        if not r:
            raise LookupError('client not found')
        return _norm(r['name']), _norm(r['name'])
    if scope == 'person':
        try:
            r = conn.execute('SELECT p.name, c.name AS client FROM crm_contacts p '
                             'LEFT JOIN crm_clients c ON c.id=p.client_id WHERE p.id=? AND p.company_id=?',
                             (entity_id, cid)).fetchone()
        except Exception:
            r = None
        if not r:
            raise LookupError('contact not found')
        return _norm(r['name']), _norm(r['client'])
    if scope == 'team':
        r = conn.execute("SELECT COALESCE(NULLIF(display_name,''), username) AS n FROM users "
                         "WHERE id=? AND company_id=?", (entity_id, cid)).fetchone()
        if not r:
            raise LookupError('team member not found')
        return _norm(r['n']), ''
    return '', ''


def _build_fields(conn, cid, d, base=None):
    """Validate a create / update body. `base` = existing note for updates
    (fields not given keep their value). Returns a dict of columns."""
    base = base or {}
    out = {}

    scope = _norm(d.get('scope', base.get('scope', ''))).lower()
    if scope not in SCOPES:
        raise ValueError('scope must be one of: ' + ', '.join(SCOPES))
    out['scope'] = scope

    if 'entity_id' in d:
        entity_id = _int_or_none(d.get('entity_id'))
    else:
        entity_id = _int_or_none(base.get('entity_id'))
    if scope == 'general' and 'entity_id' not in d and 'scope' in d:
        entity_id = None                       # moving a note to general drops its link
    out['entity_id'] = entity_id

    # Validate the link on create, or when an update changes scope / entity.
    # (A plain text edit of an old note must keep working even if the linked
    # candidate or job has since been removed.)
    link_touched = (not base) or 'entity_id' in d or 'scope' in d
    if link_touched:
        def_name, def_client = _resolve_entity(conn, cid, scope, entity_id)
    else:
        def_name, def_client = '', ''
    linked_changed = link_touched and bool(base) and bool(def_name)

    name = _norm(d['entity_name']) if 'entity_name' in d else ('' if linked_changed else _norm(base.get('entity_name', '')))
    out['entity_name'] = (name or def_name)[:MAX_NAME]

    if 'client_name' in d:
        client = _norm(d['client_name'])
    else:
        client = '' if linked_changed else _norm(base.get('client_name', ''))
    if not client:
        client = def_client
    if scope == 'client' and not client:
        client = out['entity_name']
    out['client_name'] = client[:MAX_NAME]

    if scope in ('client', 'mandate', 'candidate', 'person', 'team') and not out['entity_name'] and entity_id is None:
        raise ValueError(f'a {scope} note needs entity_name or entity_id')

    content = d['content'] if 'content' in d else base.get('content', '')
    content = str(content or '').strip()
    if not content:
        raise ValueError('content is required')
    if len(content) > MAX_CONTENT:
        raise ValueError(f'content is too long (max {MAX_CONTENT} characters)')
    out['content'] = content

    title = _norm(d['title']) if 'title' in d else _norm(base.get('title', ''))
    if not title:
        title = _norm(content.splitlines()[0])[:120]
    out['title'] = title[:MAX_TITLE]

    if 'tags' in d:
        out['tags'] = json.dumps(_clean_tags(d.get('tags')))
    else:
        out['tags'] = base.get('tags') or '[]'

    imp = _norm(d.get('importance', base.get('importance') or 'normal')).lower() or 'normal'
    if imp not in IMPORTANCE:
        raise ValueError('importance must be high, normal or low')
    out['importance'] = imp

    out['valid_until'] = _clean_date(d['valid_until']) if 'valid_until' in d else base.get('valid_until')
    return out


def _history(conn, note_id, cid, action, old_content, new_content, changes, uid):
    conn.execute('INSERT INTO memory_note_history (note_id, company_id, action, old_content, new_content, '
                 'changes, changed_by, changed_at) VALUES (?,?,?,?,?,?,?,?)',
                 (note_id, cid, action, old_content or '', new_content or '',
                  json.dumps(changes or {}), uid or 0, ts()))


def _get_note(conn, nid, cid):
    return conn.execute('SELECT * FROM memory_notes WHERE id=? AND company_id=?', (nid, cid)).fetchone()


_EDITABLE = ('scope', 'entity_id', 'entity_name', 'client_name', 'title', 'content',
             'tags', 'importance', 'valid_until')


def _apply_update(conn, note, fields, cid, uid, action='update'):
    """Write changed columns + one history row. Returns the list of changed fields."""
    changes = {}
    for k in _EDITABLE:
        if k in fields and (fields[k] or None) != (note[k] or None):
            changes[k] = {'from': note[k], 'to': fields[k]}
    if not changes:
        return []
    sets = ', '.join(f'{k}=?' for k in changes) + ', updated_at=?, updated_by=?'
    conn.execute(f'UPDATE memory_notes SET {sets} WHERE id=? AND company_id=?',
                 [fields[k] for k in changes] + [ts(), uid or 0, note['id'], cid])
    small = {k: v for k, v in changes.items() if k != 'content'}
    if 'content' in changes:
        small['content'] = True
    _history(conn, note['id'], cid, action, note['content'], fields.get('content', note['content']),
             small, uid)
    return list(changes)


def _find_duplicate(conn, cid, fields, sc):
    """An active note with the same scope + entity and a near-identical title."""
    q = "SELECT * FROM memory_notes n WHERE n.company_id=? AND n.status='active' AND n.scope=?"
    params = [cid, fields['scope']]
    if fields['entity_id'] is not None:
        q += ' AND n.entity_id=?'
        params.append(fields['entity_id'])
    else:
        q += " AND LOWER(TRIM(COALESCE(n.entity_name,'')))=?"
        params.append(_key(fields['entity_name']))
    if sc is not None:
        frag, p2 = sc.sql('n')
        q += frag
        params += p2
    want = _key(fields['title'])
    best, best_ratio = None, 0.0
    for r in conn.execute(q + ' ORDER BY n.updated_at DESC LIMIT 200', params):
        ratio = difflib.SequenceMatcher(None, want, _key(r['title'])).ratio()
        if ratio > best_ratio:
            best, best_ratio = r, ratio
    return best if best is not None and best_ratio >= DEDUPE_RATIO else None


# ══════════════════════════════════════════════════════════════════════════
#  QUERY BUILDER  (shared by list + context)
# ══════════════════════════════════════════════════════════════════════════
def _select(conn, cid, sc, where='', params=None, include_archived=False, include_expired=False,
            limit=DEFAULT_LIMIT, today=None):
    today = today or _today()
    q = 'SELECT n.* FROM memory_notes n WHERE n.company_id=?'
    p = [cid]
    if not include_archived:
        q += " AND n.status='active'"
    if not include_expired:
        q += " AND (COALESCE(n.valid_until,'')='' OR SUBSTR(n.valid_until,1,10) >= ?)"
        p.append(today)
    if where:
        q += ' AND (' + where + ')'
        p += list(params or [])
    if sc is not None:
        frag, p2 = sc.sql('n')
        q += frag
        p += p2
    q += f' ORDER BY {_IMP_ORDER} DESC, n.updated_at DESC, n.id DESC LIMIT ?'
    p.append(int(limit))
    return [_note_dict(r, today) for r in conn.execute(q, p)]


def _limit_arg(default=DEFAULT_LIMIT):
    try:
        n = int(request.args.get('limit') or default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_LIMIT))


# ══════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════
@bp.route('', methods=['GET'])
@bp.route('/', methods=['GET'])
@login_required
def list_notes():
    """Search business memory notes. Filters: scope, entity_id, entity_name (partial),
    client_name (partial), q (words over title/content/tags/names), importance,
    include_archived, include_expired, limit (default 50). Sorted by importance, then newest."""
    cid = effective_company_id()
    conn = get_db()
    try:
        sc = _scope_obj(conn)
        where, params = [], []
        scope = _norm(request.args.get('scope')).lower()
        if scope:
            if scope not in SCOPES:
                return _err('scope must be one of: ' + ', '.join(SCOPES))
            where.append('n.scope=?')
            params.append(scope)
        if request.args.get('entity_id'):
            try:
                eid = _int_or_none(request.args.get('entity_id'))
            except ValueError as e:
                return _err(str(e))
            if eid is not None:
                where.append('n.entity_id=?')
                params.append(eid)
        en = _norm(request.args.get('entity_name'))
        if en:
            where.append("LOWER(COALESCE(n.entity_name,'')) LIKE ? ESCAPE '\\'")
            params.append(_like(en.lower()))
        cn = _norm(request.args.get('client_name'))
        if cn:
            where.append("LOWER(COALESCE(n.client_name,'')) LIKE ? ESCAPE '\\'")
            params.append(_like(cn.lower()))
        imp = _norm(request.args.get('importance')).lower()
        if imp:
            if imp not in IMPORTANCE:
                return _err('importance must be high, normal or low')
            where.append('n.importance=?')
            params.append(imp)
        status = _norm(request.args.get('status')).lower()
        if status:
            if status not in STATUSES:
                return _err('status must be active or archived')
            where.append('n.status=?')
            params.append(status)
        q = _norm(request.args.get('q'))
        for word in q.lower().split()[:8]:
            where.append("(LOWER(COALESCE(n.title,'')) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(n.content,'')) LIKE ? ESCAPE '\\'"
                         " OR LOWER(COALESCE(n.tags,'')) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(n.entity_name,'')) LIKE ? ESCAPE '\\'"
                         " OR LOWER(COALESCE(n.client_name,'')) LIKE ? ESCAPE '\\')")
            params += [_like(word)] * 5
        notes = _select(conn, cid, sc, ' AND '.join(where), params,
                        include_archived=_bool_arg('include_archived') or status == 'archived',
                        include_expired=_bool_arg('include_expired'),
                        limit=_limit_arg())
        return jsonify({'ok': True, 'count': len(notes), 'notes': notes})
    finally:
        conn.close()


@bp.route('/<int:nid>', methods=['GET'])
@login_required
def get_note(nid):
    """One memory note with its full edit history."""
    cid = effective_company_id()
    conn = get_db()
    try:
        r = _get_note(conn, nid, cid)
        sc = _scope_obj(conn)
        if not r or not _can_read(conn, r, sc):
            return _err('Not found', 404)
        note = _note_dict(r)
        note['can_edit'] = _can_write_note(conn, r, sc)
        hist = []
        for h in conn.execute(
                "SELECT h.*, COALESCE(NULLIF(u.display_name,''), u.username, '') AS changed_by_name "
                "FROM memory_note_history h LEFT JOIN users u ON u.id=h.changed_by "
                "WHERE h.note_id=? AND h.company_id=? ORDER BY h.id DESC LIMIT 100", (nid, cid)):
            d = {k: h[k] for k in h.keys()}
            try:
                d['changes'] = json.loads(d.get('changes') or '{}')
            except Exception:
                d['changes'] = {}
            hist.append(d)
        note['history'] = hist
        return jsonify({'ok': True, 'note': note})
    finally:
        conn.close()


@bp.route('', methods=['POST'])
@bp.route('/', methods=['POST'])
@login_required
def create_note():
    """Create a memory note. Body: scope, entity_name, entity_id, client_name, title, content,
    tags, importance, valid_until, source. dedupe=true updates an active note with the same
    scope + entity and a near-identical title instead of adding a duplicate."""
    d = request.get_json(silent=True) or {}
    if not isinstance(d, dict):
        return _err('JSON body required')
    cid = effective_company_id()
    uid = real_user_id() or 0
    if not cid:
        return _err('No company', 403)
    conn = get_db()
    try:
        sc = _scope_obj(conn)
        try:
            fields = _build_fields(conn, cid, d)
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e))
        if not _can_write_target(conn, fields['scope'], fields['entity_id'], sc):
            return _err('You cannot add notes here.', 403)
        source = _norm(d.get('source')).lower() or ('recruiter' if sc is not None else 'user')
        if source not in SOURCES:
            return _err('source must be one of: ' + ', '.join(SOURCES))

        if d.get('dedupe') in (True, 1, '1', 'true', 'yes'):
            dup = _find_duplicate(conn, cid, fields, sc)
            if dup is not None and _can_write_note(conn, dup, sc):
                # Merge like PUT: only the fields the caller sent change. (Scope and
                # entity already match the duplicate, so they are not re-applied;
                # otherwise unsent fields such as importance / client_name would be
                # reset to their defaults.)
                sent = {k: v for k, v in d.items() if k in _EDITABLE and k not in ('scope', 'entity_id')}
                try:
                    fields = _build_fields(conn, cid, sent, base={k: dup[k] for k in dup.keys()})
                except LookupError as e:
                    return _err(str(e), 404)
                except ValueError as e:
                    return _err(str(e))
                changed = _apply_update(conn, dup, fields, cid, uid)
                conn.commit()
                log_activity('memory_update', f"Memory note updated (dedupe): {fields['title'][:80]}",
                             'memory_note', dup['id'], {'fields': changed, 'source': source})
                return jsonify({'ok': True, 'id': dup['id'], 'action': 'updated', 'deduped': True,
                                'changed': changed, 'note': _note_dict(_get_note(conn, dup['id'], cid))})

        now = ts()
        cur = conn.execute(
            'INSERT INTO memory_notes (company_id, owner_id, scope, entity_id, entity_name, client_name, title, '
            'content, tags, importance, status, source, valid_until, created_at, updated_at, created_by, updated_by) '
            "VALUES (?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?)",
            (cid, cid, fields['scope'], fields['entity_id'], fields['entity_name'], fields['client_name'],
             fields['title'], fields['content'], fields['tags'], fields['importance'], source,
             fields['valid_until'], now, now, uid, uid))
        nid = cur.lastrowid
        _history(conn, nid, cid, 'create', '', fields['content'], {'source': source}, uid)
        conn.commit()
        log_activity('memory_create', f"Memory note added: {fields['title'][:80]}",
                     'memory_note', nid, {'scope': fields['scope'], 'source': source})
        return jsonify({'ok': True, 'id': nid, 'action': 'created', 'deduped': False,
                        'note': _note_dict(_get_note(conn, nid, cid))}), 201
    finally:
        conn.close()


@bp.route('/<int:nid>', methods=['PUT'])
@login_required
def update_note(nid):
    """Update a memory note (only the fields sent change). Every change is kept in the history."""
    d = request.get_json(silent=True) or {}
    if not isinstance(d, dict):
        return _err('JSON body required')
    cid = effective_company_id()
    uid = real_user_id() or 0
    conn = get_db()
    try:
        r = _get_note(conn, nid, cid)
        sc = _scope_obj(conn)
        if not r or not _can_read(conn, r, sc):
            return _err('Not found', 404)
        if not _can_write_note(conn, r, sc):
            return _err('You cannot edit this note.', 403)
        try:
            fields = _build_fields(conn, cid, d, base={k: r[k] for k in r.keys()})
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e))
        if not _can_write_target(conn, fields['scope'], fields['entity_id'], sc):
            return _err('You cannot move this note there.', 403)
        changed = _apply_update(conn, r, fields, cid, uid)
        conn.commit()
        if changed:
            log_activity('memory_update', f"Memory note edited: {fields['title'][:80]}",
                         'memory_note', nid, {'fields': changed})
        return jsonify({'ok': True, 'id': nid, 'changed': changed,
                        'note': _note_dict(_get_note(conn, nid, cid))})
    finally:
        conn.close()


def _set_status(nid, status):
    cid = effective_company_id()
    uid = real_user_id() or 0
    conn = get_db()
    try:
        r = _get_note(conn, nid, cid)
        sc = _scope_obj(conn)
        if not r or not _can_read(conn, r, sc):
            return _err('Not found', 404)
        if not _can_write_note(conn, r, sc):
            return _err('You cannot change this note.', 403)
        if r['status'] != status:
            conn.execute('UPDATE memory_notes SET status=?, updated_at=?, updated_by=? WHERE id=? AND company_id=?',
                         (status, ts(), uid, nid, cid))
            action = 'archive' if status == 'archived' else 'unarchive'
            _history(conn, nid, cid, action, r['content'], r['content'],
                     {'status': {'from': r['status'], 'to': status}}, uid)
            conn.commit()
            log_activity('memory_' + action, f"Memory note {action}d: {(r['title'] or '')[:80]}",
                         'memory_note', nid)
        return jsonify({'ok': True, 'id': nid, 'status': status})
    finally:
        conn.close()


@bp.route('/<int:nid>/archive', methods=['POST'])
@login_required
def archive_note(nid):
    """Archive (soft delete) a memory note. Nothing is ever hard-deleted."""
    return _set_status(nid, 'archived')


@bp.route('/<int:nid>/unarchive', methods=['POST'])
@login_required
def unarchive_note(nid):
    """Restore an archived memory note."""
    return _set_status(nid, 'active')


# ══════════════════════════════════════════════════════════════════════════
#  CONTEXT  (one briefing for Claude / the UI)
# ══════════════════════════════════════════════════════════════════════════
def _client_block(conn, cid, sc, names, crm_ids, inc_exp, today):
    """Client + person notes for a set of client names / CRM ids."""
    keys = sorted({_key(n) for n in names if _key(n)})
    crm_ids = sorted({int(i) for i in crm_ids if i})
    cw, cp = [], []
    if keys:
        ph = ','.join('?' * len(keys))
        cw.append(f"LOWER(TRIM(COALESCE(n.entity_name,''))) IN ({ph})")
        cw.append(f"LOWER(TRIM(COALESCE(n.client_name,''))) IN ({ph})")
        cp += keys * 2
    if crm_ids:
        ph = ','.join('?' * len(crm_ids))
        cw.append(f'n.entity_id IN ({ph})')
        cp += crm_ids
    if not cw:
        return {'names': [], 'crm_client_ids': [], 'notes': [], 'people_notes': []}
    client_notes = _select(conn, cid, sc, "n.scope='client' AND (" + ' OR '.join(cw) + ')', cp,
                           include_expired=inc_exp, limit=MAX_LIMIT, today=today)
    people = []
    if keys:
        ph = ','.join('?' * len(keys))
        people = _select(conn, cid, sc, f"n.scope='person' AND LOWER(TRIM(COALESCE(n.client_name,''))) IN ({ph})",
                         keys, include_expired=inc_exp, limit=MAX_LIMIT, today=today)
    return {'names': sorted({_norm(n) for n in names if _norm(n)}), 'crm_client_ids': crm_ids,
            'notes': client_notes, 'people_notes': people}


def _legacy_mandate_notes(conn, cid, mid):
    try:
        return [{'id': r['id'], 'note': r['note'], 'created_at': r['created_at']} for r in conn.execute(
            'SELECT id, note, created_at FROM mandate_client_notes WHERE mandate_id=? AND owner_id=? '
            'AND is_active=1 ORDER BY id DESC LIMIT 50', (mid, cid))]
    except Exception:
        return []


def _mandate_block(conn, cid, sc, m, inc_exp, today, with_candidates=False):
    mid = int(m['id'])
    out = {'id': mid, 'role': m['role'], 'client': m['client'], 'location': m['location'],
           'status': m['status'],
           'notes': _select(conn, cid, sc, "n.scope='mandate' AND n.entity_id=?", [mid],
                            include_expired=inc_exp, limit=MAX_LIMIT, today=today),
           'client_notes_on_job': _legacy_mandate_notes(conn, cid, mid)}
    if with_candidates:
        out['candidate_notes'] = _select(
            conn, cid, sc, "n.scope='candidate' AND n.entity_id IN "
                           "(SELECT k.id FROM candidates k WHERE k.owner_id=? AND k.mandate_id=?)",
            [cid, mid], include_expired=inc_exp, limit=50, today=today)
    return out


def _mandate_client_keys(conn, cid, m):
    names, crm = {m['client'] or ''}, set()
    try:
        crm_id = int(m['crm_client_id'] or 0)
    except Exception:
        crm_id = 0
    if crm_id:
        crm.add(crm_id)
        try:
            r = conn.execute('SELECT name FROM crm_clients WHERE id=? AND company_id=?', (crm_id, cid)).fetchone()
            if r:
                names.add(r['name'] or '')
        except Exception:
            pass
    return names, crm


_M_COLS = 'id, role, client, location, status, COALESCE(crm_client_id,0) AS crm_client_id'


@bp.route('/context', methods=['GET'])
@login_required
def context():
    """Briefing for ONE entity: client=<name> | mandate_id=<id> | candidate_id=<id>.
    Returns its active notes plus linked notes (candidate -> its mandate -> that client,
    incl. client-side people) and the candidate's feedback fields. include_expired=true
    also returns notes past valid_until."""
    cid = effective_company_id()
    inc_exp = _bool_arg('include_expired')
    today = _today()
    client = _norm(request.args.get('client'))
    try:
        mandate_id = _int_or_none(request.args.get('mandate_id'))
        candidate_id = _int_or_none(request.args.get('candidate_id'))
    except ValueError:
        return _err('mandate_id / candidate_id must be numbers')
    if sum(1 for x in (client, mandate_id, candidate_id) if x) != 1:
        return _err('give exactly one of: client, mandate_id, candidate_id')

    conn = get_db()
    try:
        sc = _scope_obj(conn)
        general = _select(conn, cid, sc, "n.scope='general' AND n.importance='high'", [],
                          include_expired=inc_exp, limit=20, today=today)

        if candidate_id:
            k = conn.execute('SELECT id, name, stage, mandate_id, company, designation, client_feedback, '
                             'recruiter_feedback, general_comments FROM candidates WHERE id=? AND owner_id=?',
                             (candidate_id, cid)).fetchone()
            if not k:
                return _err('Not found', 404)
            if sc is not None:
                from modules.access import can_see_candidate
                if not can_see_candidate(conn, candidate_id):
                    return _err('Not found', 404)
            out = {'ok': True, 'type': 'candidate',
                   'candidate': {'id': k['id'], 'name': k['name'], 'stage': k['stage'],
                                 'mandate_id': k['mandate_id'], 'company': k['company'],
                                 'designation': k['designation']},
                   'candidate_fields': {'client_feedback': k['client_feedback'] or '',
                                        'recruiter_feedback': k['recruiter_feedback'] or '',
                                        'general_comments': k['general_comments'] or ''},
                   'notes': _select(conn, cid, sc, "n.scope='candidate' AND n.entity_id=?", [candidate_id],
                                    include_expired=inc_exp, limit=MAX_LIMIT, today=today),
                   'mandate': None, 'client': None, 'general_high': general}
            m = conn.execute(f'SELECT {_M_COLS} FROM mandates WHERE id=? AND owner_id=?',
                             (k['mandate_id'], cid)).fetchone()
            if m and (m['status'] or '') != 'central':
                out['mandate'] = _mandate_block(conn, cid, sc, m, inc_exp, today)
                names, crm = _mandate_client_keys(conn, cid, m)
                out['client'] = _client_block(conn, cid, sc, names, crm, inc_exp, today)
            return jsonify(out)

        if mandate_id:
            m = conn.execute(f'SELECT {_M_COLS} FROM mandates WHERE id=? AND owner_id=?',
                             (mandate_id, cid)).fetchone()
            if not m:
                return _err('Not found', 404)
            if sc is not None:
                from modules.access import can_see_mandate
                if not can_see_mandate(conn, mandate_id):
                    return _err('Not found', 404)
            names, crm = _mandate_client_keys(conn, cid, m)
            return jsonify({'ok': True, 'type': 'mandate',
                            'mandate': _mandate_block(conn, cid, sc, m, inc_exp, today, with_candidates=True),
                            'client': _client_block(conn, cid, sc, names, crm, inc_exp, today),
                            'general_high': general})

        # client=<name>: partial, case-insensitive match on names
        like = _like(client.lower())
        names, crm = {client}, set()
        try:
            for r in conn.execute("SELECT id, name FROM crm_clients WHERE company_id=? AND LOWER(name) LIKE ? ESCAPE '\\' "
                                  'LIMIT 20', (cid, like)):
                crm.add(int(r['id']))
                names.add(r['name'] or '')
        except Exception:
            pass
        mq = (f"SELECT {_M_COLS} FROM mandates WHERE owner_id=? AND COALESCE(status,'')!='central' "
              f"AND (LOWER(client) LIKE ? ESCAPE '\\'")
        mp = [cid, like]
        if crm:
            mq += f" OR crm_client_id IN ({','.join('?' * len(crm))})"
            mp += sorted(crm)
        mq += ')'
        if sc is not None:
            from modules.access import mandate_scope_sql
            frag, p2 = mandate_scope_sql('id')
            mq += frag
            mp += p2
        mandates = conn.execute(mq + ' ORDER BY (status=\'active\') DESC, id DESC LIMIT 30', mp).fetchall()
        for m in mandates:
            n2, c2 = _mandate_client_keys(conn, cid, m)
            names |= n2
            crm |= c2
        block = _client_block(conn, cid, sc, names, crm, inc_exp, today)
        # partial-name notes too (e.g. "L&K" typed, note saved as "L&K Talent Attraction")
        seen = {n['id'] for n in block['notes']} | {n['id'] for n in block['people_notes']}
        extra = _select(conn, cid, sc,
                        "n.scope IN ('client','person') AND (LOWER(COALESCE(n.entity_name,'')) LIKE ? ESCAPE '\\' "
                        "OR LOWER(COALESCE(n.client_name,'')) LIKE ? ESCAPE '\\')", [like, like],
                        include_expired=inc_exp, limit=MAX_LIMIT, today=today)
        for n in extra:
            if n['id'] not in seen:
                (block['notes'] if n['scope'] == 'client' else block['people_notes']).append(n)
        return jsonify({'ok': True, 'type': 'client', 'query': client, 'client': block,
                        'mandates': [_mandate_block(conn, cid, sc, m, inc_exp, today) for m in mandates],
                        'general_high': general})
    finally:
        conn.close()
