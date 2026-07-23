"""
RecruitOS — ORG MAP module (Wave 1)

WHAT THIS IS
  A talent-mapping layer that sits BESIDE the ATS, not inside it.

  The people in an org map are *not* candidates. Out of 330 mapped ABB
  employees, maybe 20 will ever become candidates. Forcing them into the
  `candidates` table would (a) require a fake mandate_id for each, (b) pollute
  the pipeline, and (c) inflate every SaaS tenant's candidate/token counts.

  So org people live in their own tables and are LINKED to candidate rows when
  a mapped person actually enters the pipeline. One org person can link to
  MANY candidate rows, because the same human appears once per mandate in the
  ATS (candidates.mandate_id is NOT NULL).

TABLES (all new — nothing in the core ATS schema is touched)
  org_companies     the mapped organisations (ABB, L&T, Schneider…)
  org_people        the humans inside them + who they report to
  org_person_links  org person  <->  candidate row   (many-to-many)

IDENTITY MODEL
  The browser app addresses records by a human-readable slug ("kiran-dutt-g")
  and stores the reporting line as `managerId` -> another slug. We keep those
  slugs as `ext_key` and treat them as the client-facing identity, so the whole
  existing front-end keeps working unchanged. The integer PKs stay internal,
  and `manager_id` is resolved from `manager_ext_key` on every write so future
  SQL/graph queries can join on real ids.

ACCESS
  Owner-only for now, but NOT hard-coded: `_orgmap_allowed()` passes if the
  tenant's plan/billing_status is 'owner' OR tenant_settings.feature_orgmap is
  truthy. Selling it later as a paid add-on = flip one setting, zero code
  change.
"""

import json
import os
import re
from functools import wraps

from flask import Blueprint, request, jsonify, send_file

from modules.shared import (
    get_db, ts, current_user, effective_company_id, real_user_id,
    login_required, log_activity,
)
from modules import register_migration

bp = Blueprint('orgmap', __name__, url_prefix='/api/org')
pages = Blueprint('orgmap_pages', __name__)

# Person fields the client may write. Anything else in the payload is ignored,
# so a compromised/older front-end can never inject columns.
PERSON_FIELDS = (
    'name', 'title', 'city', 'country', 'region', 'type', 'team_size',
    'status', 'target', 'notes', 'phone', 'email', 'linkedin',
    'source', 'confidence', 'last_verified', 'left_company', 'left_on',
)

# Safety valve for the full-store sync: if a payload would delete more than
# this fraction of an established map, refuse unless the client sets force=1.
SHRINK_GUARD_RATIO = 0.5
SHRINK_GUARD_MIN = 20


# ══════════════════════════════════════════════════════════════════════════
#  SCHEMA
# ══════════════════════════════════════════════════════════════════════════
@register_migration
def migrate(conn):
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS org_companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id      INTEGER NOT NULL,        -- tenant (the agency)
        ext_key         TEXT NOT NULL,           -- client slug, e.g. 'abb-electrification'
        name            TEXT NOT NULL DEFAULT '',
        normalized_name TEXT DEFAULT '',         -- dedup key: 'abb' == 'ABB India Ltd'
        aliases         TEXT DEFAULT '[]',       -- JSON array of other spellings
        notes           TEXT DEFAULT '',
        sort_order      INTEGER DEFAULT 0,
        is_active       INTEGER DEFAULT 1,
        created_at      TEXT DEFAULT '',
        updated_at      TEXT DEFAULT ''
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS org_people (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id     INTEGER NOT NULL,         -- tenant (the agency)
        org_company_id INTEGER NOT NULL,         -- -> org_companies.id (e.g. ABB)
        ext_key        TEXT NOT NULL,            -- client slug, unique per org company
        name           TEXT DEFAULT '',
        title          TEXT DEFAULT '',
        city           TEXT DEFAULT '',
        country        TEXT DEFAULT '',
        region         TEXT DEFAULT '',
        type           TEXT DEFAULT 'Employee',  -- Employee | External / Contract
        team_size      INTEGER DEFAULT NULL,     -- headcount they quoted for their team
        manager_ext_key TEXT DEFAULT '',         -- who they report to (client slug)
        manager_id     INTEGER DEFAULT NULL,     -- resolved -> org_people.id
        phone          TEXT DEFAULT '',
        email          TEXT DEFAULT '',
        linkedin       TEXT DEFAULT '',
        status         TEXT DEFAULT 'none',      -- none|contacted|interested|shortlisted|rejected
        target         INTEGER DEFAULT 0,
        notes          TEXT DEFAULT '',
        source         TEXT DEFAULT '',          -- self-declared | referral | linkedin | guess
        confidence     TEXT DEFAULT '',          -- high | medium | low
        last_verified  TEXT DEFAULT '',          -- when this contact info was last checked
        left_company   INTEGER DEFAULT 0,        -- moved on? keep the node, mark it
        left_on        TEXT DEFAULT '',
        created_at     TEXT DEFAULT '',
        updated_at     TEXT DEFAULT ''
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS org_person_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id    INTEGER NOT NULL,          -- tenant
        org_person_id INTEGER NOT NULL,
        candidate_id  INTEGER NOT NULL,
        link_source   TEXT DEFAULT 'manual',     -- manual | phone_match | created_from_map
        linked_by     INTEGER DEFAULT 0,
        linked_at     TEXT DEFAULT ''
    )''')

    for idx in (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_orgco ON org_companies(company_id, ext_key)",
        "CREATE INDEX IF NOT EXISTS idx_orgco_norm ON org_companies(company_id, normalized_name)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_orgp ON org_people(org_company_id, ext_key)",
        "CREATE INDEX IF NOT EXISTS idx_orgp_tenant ON org_people(company_id, org_company_id)",
        "CREATE INDEX IF NOT EXISTS idx_orgp_mgr ON org_people(manager_id)",
        "CREATE INDEX IF NOT EXISTS idx_orgp_phone ON org_people(company_id, phone)",
        "CREATE INDEX IF NOT EXISTS idx_orgp_name ON org_people(company_id, name)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_orglink ON org_person_links(org_person_id, candidate_id)",
        "CREATE INDEX IF NOT EXISTS idx_orglink_cand ON org_person_links(company_id, candidate_id)",
    ):
        try:
            c.execute(idx)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
#  ACCESS CONTROL
# ══════════════════════════════════════════════════════════════════════════
def _orgmap_allowed():
    """Owner tenant, or any tenant explicitly given the feature flag."""
    cid = effective_company_id()
    if not cid:
        return False
    conn = get_db()
    try:
        flag = conn.execute(
            "SELECT value FROM tenant_settings WHERE company_id=? AND key='feature_orgmap'",
            (cid,)).fetchone()
        if flag and str(flag['value']).strip().lower() in ('1', 'true', 'yes', 'on'):
            return True
        row = conn.execute('SELECT plan, billing_status FROM companies WHERE id=?',
                           (cid,)).fetchone()
        if row and ((row['plan'] or '') == 'owner' or (row['billing_status'] or '') == 'owner'):
            return True
        return False
    finally:
        conn.close()


def orgmap_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not _orgmap_allowed():
            if request.path.startswith('/api/'):
                return jsonify({'error': 'orgmap_not_enabled'}), 403
            return ('Org Map is not enabled for this workspace.', 403)
        return fn(*a, **kw)
    return wrapper


# ══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════
def _norm(s):
    """Canonical company key: 'ABB India Ltd.' -> 'abbindia' (suffixes dropped)."""
    s = (s or '').lower()
    s = re.sub(r'[^a-z0-9]+', '', s)
    for suffix in ('privatelimited', 'pvtltd', 'limited', 'ltd', 'inc', 'llp', 'pvt'):
        if s.endswith(suffix) and len(s) > len(suffix) + 2:
            s = s[:-len(suffix)]
    return s


def _person_row_to_client(r):
    """DB row -> the exact object shape the browser app already understands."""
    return {
        'id': r['ext_key'],
        'managerId': r['manager_ext_key'] or None,
        'name': r['name'], 'title': r['title'],
        'city': r['city'], 'country': r['country'], 'region': r['region'],
        'type': r['type'],
        'team': r['team_size'],
        'status': r['status'] or 'none',
        'target': bool(r['target']),
        'notes': r['notes'] or '',
        'phone': r['phone'] or '', 'email': r['email'] or '',
        'linkedin': r['linkedin'] or '',
        'source': r['source'] or '', 'confidence': r['confidence'] or '',
        'lastVerified': r['last_verified'] or '',
        'leftCompany': bool(r['left_company']),
        'leftOn': r['left_on'] or '',
        '_pid': r['id'],          # internal id — used by Wave 2 candidate linking
    }


def _client_to_person_vals(p):
    """Browser object -> column values (whitelisted)."""
    team = p.get('team')
    try:
        team = int(team) if team not in (None, '', False) else None
    except (TypeError, ValueError):
        team = None
    return {
        'name': (p.get('name') or '').strip(),
        'title': (p.get('title') or '').strip(),
        'city': (p.get('city') or '').strip(),
        'country': (p.get('country') or '').strip(),
        'region': (p.get('region') or '').strip(),
        'type': (p.get('type') or 'Employee').strip(),
        'team_size': team,
        'status': (p.get('status') or 'none').strip(),
        'target': 1 if p.get('target') else 0,
        'notes': p.get('notes') or '',
        'phone': (p.get('phone') or '').strip(),
        'email': (p.get('email') or '').strip(),
        'linkedin': (p.get('linkedin') or '').strip(),
        'source': (p.get('source') or '').strip(),
        'confidence': (p.get('confidence') or '').strip(),
        'last_verified': (p.get('lastVerified') or '').strip(),
        'left_company': 1 if p.get('leftCompany') else 0,
        'left_on': (p.get('leftOn') or '').strip(),
    }


# ══════════════════════════════════════════════════════════════════════════
#  READ
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/bootstrap', methods=['GET'])
@login_required
@orgmap_required
def bootstrap():
    """Everything the page needs in ONE call: all companies + all their people,
    already shaped like the browser app's `store` object."""
    cid = effective_company_id()
    conn = get_db()
    try:
        companies = conn.execute(
            '''SELECT * FROM org_companies
               WHERE company_id=? AND is_active=1
               ORDER BY sort_order, id''', (cid,)).fetchall()
        if not companies:
            conn.close()
            return jsonify({'store': None, 'empty': True})

        people = conn.execute(
            '''SELECT p.* FROM org_people p
               JOIN org_companies c ON c.id = p.org_company_id
               WHERE p.company_id=? AND c.is_active=1
               ORDER BY p.id''', (cid,)).fetchall()

        # candidate links, so the map can show "already in ATS" badges
        links = conn.execute(
            'SELECT org_person_id, candidate_id FROM org_person_links WHERE company_id=?',
            (cid,)).fetchall()
        link_map = {}
        for l in links:
            link_map.setdefault(l['org_person_id'], []).append(l['candidate_id'])

        by_co = {}
        for r in people:
            obj = _person_row_to_client(r)
            obj['candidateIds'] = link_map.get(r['id'], [])
            by_co.setdefault(r['org_company_id'], []).append(obj)

        store = {'companies': {}, 'active': None}
        for c in companies:
            store['companies'][c['ext_key']] = {
                'id': c['ext_key'],
                'name': c['name'],
                'createdAt': c['created_at'],
                'people': by_co.get(c['id'], []),
            }
        store['active'] = companies[0]['ext_key']
        return jsonify({'store': store, 'empty': False})
    finally:
        try:
            conn.close()
        except Exception:
            pass


@bp.route('/stats', methods=['GET'])
@login_required
@orgmap_required
def stats():
    cid = effective_company_id()
    conn = get_db()
    try:
        row = conn.execute(
            '''SELECT COUNT(*) people,
                      SUM(CASE WHEN phone<>'' OR email<>'' THEN 1 ELSE 0 END) reachable,
                      SUM(CASE WHEN target=1 THEN 1 ELSE 0 END) targets
               FROM org_people WHERE company_id=?''', (cid,)).fetchone()
        cos = conn.execute(
            'SELECT COUNT(*) n FROM org_companies WHERE company_id=? AND is_active=1',
            (cid,)).fetchone()
        linked = conn.execute(
            'SELECT COUNT(DISTINCT org_person_id) n FROM org_person_links WHERE company_id=?',
            (cid,)).fetchone()
        return jsonify({
            'companies': cos['n'], 'people': row['people'] or 0,
            'reachable': row['reachable'] or 0, 'targets': row['targets'] or 0,
            'linked_to_ats': linked['n'] or 0,
        })
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  WRITE — full-store sync
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/sync', methods=['POST'])
@login_required
@orgmap_required
def sync():
    """The browser sends its whole `store` after every edit (debounced).

    Why full-store instead of granular endpoints: the existing front-end has a
    single save seam (`saveStore()`), so this keeps ~10 call sites and all the
    add/edit/delete/import/restore flows working with zero UI rewrites. The map
    is owner-only and single-writer, so there is no concurrent-edit risk.
    A shrink guard stops a half-loaded page from wiping an established map.
    """
    cid = effective_company_id()
    uid = real_user_id() or 0
    data = request.get_json(silent=True) or {}
    store = data.get('store') or {}
    force = bool(data.get('force'))
    companies = store.get('companies') or {}
    if not isinstance(companies, dict):
        return jsonify({'error': 'bad_payload'}), 400

    incoming_people = sum(len(c.get('people') or []) for c in companies.values())

    conn = get_db()
    try:
        cur = conn.cursor()
        existing_total = cur.execute(
            'SELECT COUNT(*) n FROM org_people WHERE company_id=?', (cid,)).fetchone()['n']

        if (not force and existing_total >= SHRINK_GUARD_MIN
                and incoming_people < existing_total * SHRINK_GUARD_RATIO):
            return jsonify({
                'error': 'shrink_guard',
                'message': (f'This would cut the map from {existing_total} to '
                            f'{incoming_people} people. Nothing was saved.'),
                'existing': existing_total, 'incoming': incoming_people,
            }), 409

        now = ts()
        seen_co_ids = []

        for ext_key, co in companies.items():
            name = (co.get('name') or ext_key or '').strip()
            row = cur.execute(
                'SELECT id FROM org_companies WHERE company_id=? AND ext_key=?',
                (cid, ext_key)).fetchone()
            if row:
                oc_id = row['id']
                cur.execute('''UPDATE org_companies
                               SET name=?, normalized_name=?, is_active=1, updated_at=?
                               WHERE id=?''', (name, _norm(name), now, oc_id))
            else:
                cur.execute('''INSERT INTO org_companies
                    (company_id, ext_key, name, normalized_name, created_at, updated_at)
                    VALUES (?,?,?,?,?,?)''',
                    (cid, ext_key, name, _norm(name), co.get('createdAt') or now, now))
                oc_id = cur.lastrowid
            seen_co_ids.append(oc_id)

            people = co.get('people') or []
            seen_keys = []
            for p in people:
                pk = (p.get('id') or '').strip()
                if not pk:
                    continue
                seen_keys.append(pk)
                vals = _client_to_person_vals(p)
                mgr = (p.get('managerId') or '') or ''
                ex = cur.execute(
                    'SELECT id FROM org_people WHERE org_company_id=? AND ext_key=?',
                    (oc_id, pk)).fetchone()
                if ex:
                    cur.execute(f'''UPDATE org_people SET
                        {", ".join(f"{f}=?" for f in PERSON_FIELDS)},
                        manager_ext_key=?, updated_at=?
                        WHERE id=?''',
                        tuple(vals[f] for f in PERSON_FIELDS) + (mgr, now, ex['id']))
                else:
                    cur.execute(f'''INSERT INTO org_people
                        (company_id, org_company_id, ext_key, manager_ext_key,
                         {", ".join(PERSON_FIELDS)}, created_at, updated_at)
                        VALUES (?,?,?,?,{",".join("?" * len(PERSON_FIELDS))},?,?)''',
                        (cid, oc_id, pk, mgr) +
                        tuple(vals[f] for f in PERSON_FIELDS) + (now, now))

            # remove people deleted in the browser
            if seen_keys:
                qs = ','.join('?' * len(seen_keys))
                cur.execute(
                    f'DELETE FROM org_people WHERE org_company_id=? AND ext_key NOT IN ({qs})',
                    (oc_id, *seen_keys))
            else:
                cur.execute('DELETE FROM org_people WHERE org_company_id=?', (oc_id,))

            # resolve manager slugs -> real ids (one pass, after all rows exist)
            cur.execute('''UPDATE org_people SET manager_id = (
                    SELECT m.id FROM org_people m
                    WHERE m.org_company_id = org_people.org_company_id
                      AND m.ext_key = org_people.manager_ext_key)
                WHERE org_company_id=?''', (oc_id,))

        # soft-delete companies removed in the browser
        if seen_co_ids:
            qs = ','.join('?' * len(seen_co_ids))
            cur.execute(
                f'''UPDATE org_companies SET is_active=0, updated_at=?
                    WHERE company_id=? AND id NOT IN ({qs})''',
                (now, cid, *seen_co_ids))

        conn.commit()
        total = cur.execute(
            'SELECT COUNT(*) n FROM org_people WHERE company_id=?', (cid,)).fetchone()['n']
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return jsonify({'error': 'sync_failed', 'message': str(e)}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

    try:
        log_activity('orgmap_sync', f'{len(companies)} companies, {total} people',
                     entity_type='orgmap', entity_id=0)
    except Exception:
        pass
    return jsonify({'ok': True, 'people': total, 'savedAt': ts()})


# ══════════════════════════════════════════════════════════════════════════
#  PAGE
# ══════════════════════════════════════════════════════════════════════════
@pages.route('/orgmap', methods=['GET'])
@login_required
@orgmap_required
def orgmap_page():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return send_file(os.path.join(root, 'orgmap.html'))
