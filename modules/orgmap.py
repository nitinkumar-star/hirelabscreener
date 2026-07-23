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


# ══════════════════════════════════════════════════════════════════════════
#  WAVE 2 — candidate  <->  org node
#
#  CONTACT SYNC POLICY (deliberate, and the safe reading of "auto-sync"):
#    • one side empty  -> filled automatically, silently, both directions
#    • both filled and different -> NOTHING is overwritten; the difference is
#      returned as a `conflict` for the user to resolve in one click
#  A verified phone number is expensive to obtain and impossible to notice
#  when it silently disappears, so last-write-wins is not used anywhere here.
# ══════════════════════════════════════════════════════════════════════════
SYNC_FIELDS = (            # (org_people col, candidates col)
    ('phone', 'phone'),
    ('email', 'email'),
    ('linkedin', 'linkedin_url'),
)


def _cand_cols(conn):
    return {r['name'] for r in conn.execute('PRAGMA table_info(candidates)').fetchall()}


def _slugify(s, taken):
    base = re.sub(r'[^a-z0-9]+', '-', (s or '').lower()).strip('-') or 'n'
    key, i = base, 0
    while key in taken:
        i += 1
        key = f'{base}-{i}'
    return key


def _get_candidate(conn, cid):
    """Candidate row, only if it belongs to this tenant."""
    r = conn.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
    if not r:
        return None
    cols = r.keys()
    if 'owner_id' in cols and r['owner_id'] != effective_company_id():
        return None
    return r


def _find_org_company(cur, tenant, name):
    """Match a free-text candidate company to a mapped company.

    Candidates carry whatever the recruiter typed ('ABB'), while the map holds
    the formal name ('ABB India Ltd'). Exact-key matching alone would miss that
    pair, which both empties the Reports-to picker AND silently creates a
    duplicate company on every save. So: exact key first, then containment
    either way (guarded to >=3 chars so 'LT' can't swallow 'L&T Infotech').
    """
    nk = _norm(name)
    if not nk:
        return None
    rows = cur.execute(
        'SELECT * FROM org_companies WHERE company_id=? AND is_active=1', (tenant,)).fetchall()
    for r in rows:
        if r['normalized_name'] == nk:
            return r
    if len(nk) >= 3:
        cands = [r for r in rows
                 if r['normalized_name'] and len(r['normalized_name']) >= 3
                 and (r['normalized_name'].startswith(nk) or nk.startswith(r['normalized_name']))]
        if len(cands) == 1:
            return cands[0]
    return None


def _ensure_org_company(cur, tenant, name):
    """Find (by normalised name) or create the mapped company for a candidate."""
    name = (name or '').strip() or 'Unmapped'
    row = _find_org_company(cur, tenant, name)
    if row:
        return row['id'], row['ext_key']
    taken = {r['ext_key'] for r in cur.execute(
        'SELECT ext_key FROM org_companies WHERE company_id=?', (tenant,)).fetchall()}
    ext = _slugify(name, taken)
    now = ts()
    cur.execute('''INSERT INTO org_companies
        (company_id, ext_key, name, normalized_name, created_at, updated_at)
        VALUES (?,?,?,?,?,?)''', (tenant, ext, name, _norm(name), now, now))
    return cur.lastrowid, ext


def _ensure_node_for_candidate(cur, tenant, cand):
    """Return the org node for this candidate, creating + linking it if needed."""
    row = cur.execute(
        '''SELECT p.* FROM org_people p
           JOIN org_person_links l ON l.org_person_id = p.id
           WHERE l.company_id=? AND l.candidate_id=? LIMIT 1''',
        (tenant, cand['id'])).fetchone()
    if row:
        return row

    oc_id, _ = _ensure_org_company(cur, tenant, cand['company'])
    taken = {r['ext_key'] for r in cur.execute(
        'SELECT ext_key FROM org_people WHERE org_company_id=?', (oc_id,)).fetchall()}
    ext = _slugify(cand['name'], taken)
    now = ts()
    cur.execute('''INSERT INTO org_people
        (company_id, org_company_id, ext_key, name, title, city, phone, email, linkedin,
         type, status, source, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,'Employee','contacted','ats',?,?)''',
        (tenant, oc_id, ext, cand['name'] or '', cand['designation'] or '',
         cand['location'] or '', cand['phone'] or '', cand['email'] or '',
         (cand['linkedin_url'] if 'linkedin_url' in cand.keys() else '') or '', now, now))
    pid = cur.lastrowid
    cur.execute('''INSERT OR IGNORE INTO org_person_links
        (company_id, org_person_id, candidate_id, link_source, linked_by, linked_at)
        VALUES (?,?,?,'auto_from_ats',?,?)''',
        (tenant, pid, cand['id'], real_user_id() or 0, now))
    return cur.execute('SELECT * FROM org_people WHERE id=?', (pid,)).fetchone()


def _sync_contacts(cur, node, cand, cand_cols):
    """Fill blanks both ways; report (never silently resolve) real differences."""
    conflicts, node_set, cand_set = [], {}, {}
    for ncol, ccol in SYNC_FIELDS:
        if ccol not in cand_cols:
            continue
        nv = (node[ncol] or '').strip()
        cv = (cand[ccol] or '').strip()
        if nv and not cv:
            cand_set[ccol] = nv
        elif cv and not nv:
            node_set[ncol] = cv
        elif nv and cv and nv.replace(' ', '') != cv.replace(' ', ''):
            conflicts.append({'field': ncol, 'org': nv, 'ats': cv})
    if node_set:
        cur.execute('UPDATE org_people SET {} , updated_at=? WHERE id=?'.format(
            ', '.join(f'{k}=?' for k in node_set)),
            tuple(node_set.values()) + (ts(), node['id']))
    if cand_set:
        cur.execute('UPDATE candidates SET {} WHERE id=?'.format(
            ', '.join(f'{k}=?' for k in cand_set)),
            tuple(cand_set.values()) + (cand['id'],))
    return conflicts, bool(node_set or cand_set)


def _node_brief(r, extra=None):
    d = {'pid': r['id'], 'ext_key': r['ext_key'], 'name': r['name'],
         'title': r['title'], 'city': r['city'], 'region': r['region'],
         'phone': r['phone'], 'email': r['email'], 'status': r['status'],
         'target': bool(r['target'])}
    if extra:
        d.update(extra)
    return d


@bp.route('/for-candidate/<int:cid>', methods=['GET'])
@login_required
@orgmap_required
def for_candidate(cid):
    """Everything the candidate-profile widget shows, in one call."""
    tenant = effective_company_id()
    conn = get_db()
    try:
        cand = _get_candidate(conn, cid)
        if not cand:
            return jsonify({'error': 'not_found'}), 404
        cur = conn.cursor()
        cols = _cand_cols(conn)

        link = cur.execute(
            '''SELECT p.* FROM org_people p
               JOIN org_person_links l ON l.org_person_id=p.id
               WHERE l.company_id=? AND l.candidate_id=? LIMIT 1''',
            (tenant, cid)).fetchone()

        out = {'candidate': {'id': cid, 'name': cand['name'],
                             'company': cand['company'], 'phone': cand['phone']},
               'node': None, 'manager': None, 'reports': [],
               'suggestions': [], 'conflicts': [], 'other_rows': [],
               'org_company': None, 'synced': False}

        if not link:
            # not mapped yet -> offer likely matches (phone first, then name)
            phone = re.sub(r'\D', '', cand['phone'] or '')[-10:]
            sugg = []
            if phone:
                sugg = cur.execute(
                    '''SELECT * FROM org_people
                       WHERE company_id=? AND REPLACE(REPLACE(phone,' ',''),'-','') LIKE ?
                       LIMIT 5''', (tenant, '%' + phone)).fetchall()
            if not sugg and (cand['name'] or '').strip():
                sugg = cur.execute(
                    '''SELECT * FROM org_people
                       WHERE company_id=? AND LOWER(name)=LOWER(?) LIMIT 5''',
                    (tenant, cand['name'].strip())).fetchall()
            seen = {r['org_person_id'] for r in cur.execute(
                'SELECT org_person_id FROM org_person_links WHERE company_id=?',
                (tenant,)).fetchall()}
            out['suggestions'] = [_node_brief(r) for r in sugg if r['id'] not in seen]
            conn.commit()
            return jsonify(out)

        conflicts, changed = _sync_contacts(cur, link, cand, cols)
        link = cur.execute('SELECT * FROM org_people WHERE id=?', (link['id'],)).fetchone()

        co = cur.execute('SELECT * FROM org_companies WHERE id=?',
                         (link['org_company_id'],)).fetchone()
        mgr = None
        if link['manager_id']:
            m = cur.execute('SELECT * FROM org_people WHERE id=?',
                            (link['manager_id'],)).fetchone()
            if m:
                mgr = _node_brief(m)
        reports = cur.execute(
            'SELECT * FROM org_people WHERE manager_id=? ORDER BY name', (link['id'],)).fetchall()
        others = cur.execute(
            '''SELECT c.id, c.stage, m.role, m.client
               FROM org_person_links l
               JOIN candidates c ON c.id=l.candidate_id
               LEFT JOIN mandates m ON m.id=c.mandate_id
               WHERE l.org_person_id=? AND c.id<>?''', (link['id'], cid)).fetchall()

        out.update({
            'node': _node_brief(link),
            'org_company': {'ext_key': co['ext_key'], 'name': co['name']} if co else None,
            'manager': mgr,
            'reports': [_node_brief(r) for r in reports],
            'conflicts': conflicts,
            'synced': changed,
            'other_rows': [{'id': r['id'], 'stage': r['stage'],
                            'role': r['role'] or '', 'client': r['client'] or ''}
                           for r in others],
        })
        conn.commit()
        return jsonify(out)
    finally:
        conn.close()


@bp.route('/set-manager', methods=['POST'])
@login_required
@orgmap_required
def set_manager():
    """Tag a candidate's reporting manager. Creates the candidate's own node
    and (if needed) the manager's node, then wires the reporting line."""
    d = request.get_json(silent=True) or {}
    cid = int(d.get('candidate_id') or 0)
    tenant = effective_company_id()
    conn = get_db()
    try:
        cand = _get_candidate(conn, cid)
        if not cand:
            return jsonify({'error': 'not_found'}), 404
        cur = conn.cursor()
        node = _ensure_node_for_candidate(cur, tenant, cand)

        if d.get('clear'):
            cur.execute('UPDATE org_people SET manager_id=NULL, manager_ext_key="", updated_at=? WHERE id=?',
                        (ts(), node['id']))
            conn.commit()
            return jsonify({'ok': True, 'cleared': True})

        mgr_ext = (d.get('manager_ext_key') or '').strip()
        if mgr_ext:
            mgr = cur.execute('SELECT * FROM org_people WHERE org_company_id=? AND ext_key=?',
                              (node['org_company_id'], mgr_ext)).fetchone()
            if not mgr:
                return jsonify({'error': 'manager_not_found'}), 404
        else:
            name = (d.get('manager_name') or '').strip()
            if not name:
                return jsonify({'error': 'manager_name_required'}), 400
            taken = {r['ext_key'] for r in cur.execute(
                'SELECT ext_key FROM org_people WHERE org_company_id=?',
                (node['org_company_id'],)).fetchall()}
            ext = _slugify(name, taken)
            now = ts()
            cur.execute('''INSERT INTO org_people
                (company_id, org_company_id, ext_key, name, title, type, status,
                 source, confidence, created_at, updated_at)
                VALUES (?,?,?,?,?,'Employee','none','candidate-said','medium',?,?)''',
                (tenant, node['org_company_id'], ext, name,
                 (d.get('manager_title') or '').strip(), now, now))
            mgr = cur.execute('SELECT * FROM org_people WHERE id=?',
                              (cur.lastrowid,)).fetchone()

        if mgr['id'] == node['id']:
            return jsonify({'error': 'self_manager'}), 400
        # cycle guard: a person cannot report to their own subordinate
        seen, cur_id = set(), mgr['manager_id']
        while cur_id and cur_id not in seen:
            if cur_id == node['id']:
                return jsonify({'error': 'would_create_loop'}), 400
            seen.add(cur_id)
            r = cur.execute('SELECT manager_id FROM org_people WHERE id=?', (cur_id,)).fetchone()
            cur_id = r['manager_id'] if r else None

        cur.execute('UPDATE org_people SET manager_id=?, manager_ext_key=?, updated_at=? WHERE id=?',
                    (mgr['id'], mgr['ext_key'], ts(), node['id']))
        conn.commit()
        try:
            log_activity('orgmap_set_manager', f"{cand['name']} -> {mgr['name']}",
                         entity_type='candidate', entity_id=cid)
        except Exception:
            pass
        return jsonify({'ok': True, 'manager': _node_brief(mgr),
                        'node': _node_brief(node)})
    finally:
        conn.close()


@bp.route('/people-search', methods=['GET'])
@login_required
@orgmap_required
def people_search():
    """Type-ahead for the Reports-to picker."""
    q = (request.args.get('q') or '').strip()
    tenant = effective_company_id()
    cid = request.args.get('candidate_id')
    conn = get_db()
    try:
        sql = '''SELECT p.*, c.name conm, c.ext_key coext FROM org_people p
                 JOIN org_companies c ON c.id=p.org_company_id
                 WHERE p.company_id=? AND c.is_active=1'''
        args = [tenant]
        # Keep the picker inside the candidate's own company when we can match
        # it. If we can't, show everything rather than an empty box — a wrong
        # scope that hides real people is worse than a slightly wider list.
        scoped = False
        if cid:
            cand = _get_candidate(conn, int(cid))
            if cand and (cand['company'] or '').strip():
                co = _find_org_company(conn.cursor(), tenant, cand['company'])
                if co:
                    sql += ' AND c.id=?'
                    args.append(co['id'])
                    scoped = True
        if q:
            sql += ' AND (p.name LIKE ? OR p.title LIKE ?)'
            args += [f'%{q}%', f'%{q}%']
        sql += ' ORDER BY (p.manager_id IS NULL) DESC, p.name LIMIT 20'
        rows = conn.execute(sql, args).fetchall()
        return jsonify({'people': [_node_brief(r, {'company': r['conm']}) for r in rows], 'scoped': scoped})
    finally:
        conn.close()


@bp.route('/link', methods=['POST'])
@login_required
@orgmap_required
def link_person():
    d = request.get_json(silent=True) or {}
    cid = int(d.get('candidate_id') or 0)
    pid = int(d.get('org_person_id') or 0)
    tenant = effective_company_id()
    conn = get_db()
    try:
        cand = _get_candidate(conn, cid)
        node = conn.execute('SELECT * FROM org_people WHERE id=? AND company_id=?',
                            (pid, tenant)).fetchone()
        if not cand or not node:
            return jsonify({'error': 'not_found'}), 404
        cur = conn.cursor()
        cur.execute('''INSERT OR IGNORE INTO org_person_links
            (company_id, org_person_id, candidate_id, link_source, linked_by, linked_at)
            VALUES (?,?,?,?,?,?)''',
            (tenant, pid, cid, d.get('source') or 'manual', real_user_id() or 0, ts()))
        _sync_contacts(cur, node, cand, _cand_cols(conn))
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


@bp.route('/unlink', methods=['POST'])
@login_required
@orgmap_required
def unlink_person():
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        conn.execute('DELETE FROM org_person_links WHERE company_id=? AND candidate_id=? AND org_person_id=?',
                     (effective_company_id(), int(d.get('candidate_id') or 0),
                      int(d.get('org_person_id') or 0)))
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


@bp.route('/candidate-search', methods=['GET'])
@login_required
@orgmap_required
def candidate_search():
    """Search ATS candidates from inside the org map drawer."""
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'candidates': []})
    tenant = effective_company_id()
    conn = get_db()
    try:
        has_owner = 'owner_id' in _cand_cols(conn)
        sql = '''SELECT c.id, c.name, c.company, c.designation, c.phone, c.stage,
                        m.role, m.client
                 FROM candidates c LEFT JOIN mandates m ON m.id=c.mandate_id
                 WHERE (c.name LIKE ? OR c.phone LIKE ?)'''
        args = [f'%{q}%', f'%{q}%']
        if has_owner:
            sql += ' AND c.owner_id=?'
            args.append(tenant)
        sql += ' ORDER BY c.updated_at DESC LIMIT 15'
        rows = conn.execute(sql, args).fetchall()
        return jsonify({'candidates': [dict(r) for r in rows]})
    finally:
        conn.close()


@bp.route('/mandates', methods=['GET'])
@login_required
@orgmap_required
def open_mandates():
    conn = get_db()
    try:
        rows = conn.execute(
            '''SELECT id, role, client FROM mandates
               WHERE owner_id=? AND status='active' ORDER BY created_at DESC LIMIT 50''',
            (effective_company_id(),)).fetchall()
        return jsonify({'mandates': [dict(r) for r in rows]})
    finally:
        conn.close()


@bp.route('/create-candidate', methods=['POST'])
@login_required
@orgmap_required
def create_candidate_from_node():
    """Turn a mapped person into a real candidate on a chosen mandate."""
    d = request.get_json(silent=True) or {}
    pid = int(d.get('org_person_id') or 0)
    mid = int(d.get('mandate_id') or 0)
    tenant = effective_company_id()
    conn = get_db()
    try:
        node = conn.execute('SELECT * FROM org_people WHERE id=? AND company_id=?',
                            (pid, tenant)).fetchone()
        mand = conn.execute('SELECT * FROM mandates WHERE id=? AND owner_id=?',
                            (mid, tenant)).fetchone()
        if not node or not mand:
            return jsonify({'error': 'not_found'}), 404

        dup = conn.execute(
            '''SELECT c.id FROM org_person_links l JOIN candidates c ON c.id=l.candidate_id
               WHERE l.org_person_id=? AND c.mandate_id=?''', (pid, mid)).fetchone()
        if dup:
            return jsonify({'error': 'already_on_mandate', 'candidate_id': dup['id']}), 409

        co = conn.execute('SELECT name FROM org_companies WHERE id=?',
                          (node['org_company_id'],)).fetchone()
        cols = _cand_cols(conn)
        now = ts()
        cur = conn.cursor()
        fields = {
            'mandate_id': mid, 'name': node['name'], 'company': co['name'] if co else '',
            'designation': node['title'], 'location': node['city'],
            'phone': node['phone'], 'email': node['email'],
            'stage': 'Screening', 'created_at': now, 'updated_at': now,
        }
        if 'owner_id' in cols:
            fields['owner_id'] = tenant
        if 'linkedin_url' in cols:
            fields['linkedin_url'] = node['linkedin']
        if 'general_comments' in cols:
            fields['general_comments'] = ('Sourced from Org Map. ' + (node['notes'] or '')).strip()
        keys = [k for k in fields if k in cols or k == 'mandate_id']
        cur.execute('INSERT INTO candidates ({}) VALUES ({})'.format(
            ','.join(keys), ','.join('?' * len(keys))),
            tuple(fields[k] for k in keys))
        cid = cur.lastrowid
        cur.execute('''INSERT OR IGNORE INTO org_person_links
            (company_id, org_person_id, candidate_id, link_source, linked_by, linked_at)
            VALUES (?,?,?,'created_from_map',?,?)''',
            (tenant, pid, cid, real_user_id() or 0, now))
        if (node['status'] or 'none') == 'none':
            cur.execute('UPDATE org_people SET status="contacted", updated_at=? WHERE id=?',
                        (now, pid))
        conn.commit()
        try:
            log_activity('orgmap_create_candidate',
                         f"{node['name']} -> {mand['role']}", entity_type='candidate',
                         entity_id=cid)
        except Exception:
            pass
        return jsonify({'ok': True, 'candidate_id': cid})
    finally:
        conn.close()


@bp.route('/resolve-conflict', methods=['POST'])
@login_required
@orgmap_required
def resolve_conflict():
    """User picks which side is right for one field. Only then do we overwrite."""
    d = request.get_json(silent=True) or {}
    cid = int(d.get('candidate_id') or 0)
    field = (d.get('field') or '').strip()
    keep = (d.get('keep') or '').strip()          # 'org' | 'ats'
    pair = dict((n, c) for n, c in SYNC_FIELDS)
    if field not in pair or keep not in ('org', 'ats'):
        return jsonify({'error': 'bad_request'}), 400
    tenant = effective_company_id()
    conn = get_db()
    try:
        cand = _get_candidate(conn, cid)
        if not cand:
            return jsonify({'error': 'not_found'}), 404
        cur = conn.cursor()
        node = cur.execute(
            '''SELECT p.* FROM org_people p JOIN org_person_links l ON l.org_person_id=p.id
               WHERE l.company_id=? AND l.candidate_id=? LIMIT 1''', (tenant, cid)).fetchone()
        if not node:
            return jsonify({'error': 'not_linked'}), 404
        ccol = pair[field]
        if keep == 'org':
            cur.execute(f'UPDATE candidates SET {ccol}=? WHERE id=?', (node[field], cid))
            val = node[field]
        else:
            cur.execute(f'UPDATE org_people SET {field}=?, last_verified=?, updated_at=? WHERE id=?',
                        (cand[ccol], ts(), ts(), node['id']))
            val = cand[ccol]
        conn.commit()
        return jsonify({'ok': True, 'value': val})
    finally:
        conn.close()


@bp.route('/node', methods=['GET'])
@login_required
@orgmap_required
def node_info():
    """Resolve a map node (by internal id, or by company+slug for nodes the
    browser created seconds ago) and list every ATS row it is linked to."""
    tenant = effective_company_id()
    pid = request.args.get('pid')
    conn = get_db()
    try:
        if pid:
            node = conn.execute('SELECT * FROM org_people WHERE id=? AND company_id=?',
                                (int(pid), tenant)).fetchone()
        else:
            node = conn.execute(
                '''SELECT p.* FROM org_people p JOIN org_companies c ON c.id=p.org_company_id
                   WHERE p.company_id=? AND c.ext_key=? AND p.ext_key=?''',
                (tenant, request.args.get('company_ext') or '',
                 request.args.get('ext_key') or '')).fetchone()
        if not node:
            return jsonify({'pid': None, 'candidates': []})
        rows = conn.execute(
            '''SELECT c.id, c.stage, c.mandate_id, m.role, m.client
               FROM org_person_links l JOIN candidates c ON c.id=l.candidate_id
               LEFT JOIN mandates m ON m.id=c.mandate_id
               WHERE l.org_person_id=? ORDER BY c.updated_at DESC''',
            (node['id'],)).fetchall()
        return jsonify({'pid': node['id'], 'ext_key': node['ext_key'],
                        'name': node['name'],
                        'candidates': [dict(r) for r in rows]})
    finally:
        conn.close()
