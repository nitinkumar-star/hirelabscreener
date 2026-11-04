"""
RecruitOS — Profile groups: your own candidate profile sections  (Nov 2026)

The candidate profile had one fixed "Sales profile" (target, team, product,
function, geography, gender) on EVERY candidate, sales or not. Now each agency
builds its own groups in Settings → Profile groups — "Sales", "Production",
"IT", … — each with its own sub-fields, and on a candidate the recruiter picks
the group and fills only those fields. Everything is searchable in Talent.

Field types
  text      one line of text
  number    a number, with an optional unit shown after it (₹ Cr, LPA, MW, yrs …)
  tags      several values (press Enter), with suggestions from what the team used
  choice    one of a fixed list of options
  system    one of the original built-in fields, stored on the candidate as before
            (and still used by the Talent filters, the Company Talent Map and the
            Chrome extension): yearly_target, team_direct, team_indirect,
            product_handles, function_tags, geography_tags, gender

Every agency gets a "Sales" group (the seven built-in fields) the first time it
opens the list; a candidate with sales data and no group shows as Sales, so
nothing filled before is lost.

Tables
  profile_groups            the groups of an agency, fields as JSON
  candidate_profile_values  custom field values (one row per candidate + group + field)
  candidates.profile_group_id

API
  GET    /api/profile-groups                    the agency's groups (+ built-in field list)
  POST   /api/profile-groups                    {name, fields}            create
  PUT    /api/profile-groups/<gid>              {name, fields}            change
  DELETE /api/profile-groups/<gid>              hide (values are kept)
  GET    /api/profile-groups/<gid>/suggest?key= values the team already used for a tags field
  GET    /api/candidates/<cid>/profile          {group_id, values}
  PUT    /api/candidates/<cid>/profile          {group_id, values}        custom values only;
         built-in values go through PUT /api/candidates/<cid> as before

Search (server._talent_where / central_search): ?pgroup=<gid>, pf_<key>=text,
pfmin_<key>= / pfmax_<key>= for numbers; the keyword box also reads custom values.
"""

import re
import json
import datetime

from flask import Blueprint, request, jsonify, session

from modules.shared import get_db, ts, effective_company_id, is_company_admin, login_required, _core
from modules import register_migration

bp = Blueprint('profile_groups', __name__)

TYPES = ('text', 'number', 'tags', 'choice', 'system')
SYSTEM = {
    'gender':          {'label': 'Gender', 'kind': 'choice', 'options': ['Male', 'Female', 'Other']},
    'yearly_target':   {'label': 'Yearly target', 'kind': 'number', 'unit': '₹ Cr / yr'},
    'team_direct':     {'label': 'Direct reports', 'kind': 'number', 'unit': 'people'},
    'team_indirect':   {'label': 'Indirect team', 'kind': 'number', 'unit': 'people'},
    'product_handles': {'label': 'Product handling', 'kind': 'tags', 'tag_type': 'product'},
    'function_tags':   {'label': 'Function', 'kind': 'tags', 'tag_type': 'function'},
    'geography_tags':  {'label': 'Geography', 'kind': 'tags', 'tag_type': 'geography'},
}
SALES_FIELDS = ['gender', 'yearly_target', 'team_direct', 'team_indirect', 'product_handles', 'function_tags', 'geography_tags']
MAX_FIELDS = 30


@register_migration
def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS profile_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        fields TEXT DEFAULT '[]',
        is_sales INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        sort INTEGER DEFAULT 0,
        created_by INTEGER DEFAULT 0,
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS candidate_profile_values (
        candidate_id INTEGER NOT NULL,
        group_id INTEGER NOT NULL,
        field_key TEXT NOT NULL,
        company_id INTEGER NOT NULL,
        value_text TEXT DEFAULT '',
        value_num REAL,
        value_json TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        PRIMARY KEY (candidate_id, group_id, field_key)
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_pg_company ON profile_groups(company_id, is_active)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_cpv_search ON candidate_profile_values(company_id, group_id, field_key)')
    try:
        conn.execute('ALTER TABLE candidates ADD COLUMN profile_group_id INTEGER DEFAULT 0')
    except Exception:
        pass
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  Group definitions
# ══════════════════════════════════════════════════════════════════════════
def _slug(s):
    s = re.sub(r'[^a-z0-9]+', '_', str(s or '').lower()).strip('_')
    if s.startswith('sys_'):
        s = 'f_' + s
    return (s or 'field')[:40]


def clean_fields(raw, old=None):
    """Validate a field list. Keys stay stable: a field that already exists
    keeps its key even when renamed, so saved values are not lost."""
    if not isinstance(raw, list):
        raise ValueError('Fields must be a list.')
    old_keys = {f['key'] for f in (old or [])}
    out, seen = [], set()
    for f in raw[:MAX_FIELDS]:
        if not isinstance(f, dict):
            continue
        t = str(f.get('type') or 'text').lower()
        if t not in TYPES:
            raise ValueError('Unknown field type: %s' % t)
        if t == 'system':
            sk = str(f.get('sys') or '')
            if sk not in SYSTEM:
                raise ValueError('Unknown built-in field: %s' % sk)
            key = 'sys_' + sk
            if key in seen:
                continue
            seen.add(key)
            out.append({'key': key, 'type': 'system', 'sys': sk, 'label': SYSTEM[sk]['label']})
            continue
        label = re.sub(r'\s+', ' ', str(f.get('label') or '')).strip()[:60]
        if not label:
            raise ValueError('Every field needs a name.')
        key = str(f.get('key') or '')
        if key not in old_keys or key in seen or key.startswith('sys_'):
            key = base = _slug(label)
            n = 2
            while key in seen or key in old_keys and key != f.get('key'):
                key = '%s_%d' % (base, n)
                n += 1
        seen.add(key)
        d = {'key': key, 'type': t, 'label': label}
        if t == 'number':
            d['unit'] = str(f.get('unit') or '').strip()[:20]
        if t == 'choice':
            opts = [re.sub(r'\s+', ' ', str(o)).strip()[:40] for o in (f.get('options') or [])]
            opts = [o for i, o in enumerate(opts) if o and o.lower() not in [x.lower() for x in opts[:i]]][:20]
            if len(opts) < 2:
                raise ValueError('"%s" needs at least two options.' % label)
            d['options'] = opts
        out.append(d)
    if not out:
        raise ValueError('Add at least one field.')
    return out


def _row(r):
    try:
        fields = json.loads(r['fields'] or '[]')
    except Exception:
        fields = []
    return {'id': r['id'], 'name': r['name'], 'fields': fields, 'is_sales': bool(r['is_sales']),
            'created_by': r['created_by'], 'updated_at': r['updated_at'] or ''}


def ensure_defaults(conn, oid):
    """First visit of an agency: create the Sales group from the built-in fields."""
    if not oid:
        return
    if conn.execute('SELECT 1 FROM profile_groups WHERE company_id=? LIMIT 1', (oid,)).fetchone():
        return
    fields = [{'key': 'sys_' + k, 'type': 'system', 'sys': k, 'label': SYSTEM[k]['label']} for k in SALES_FIELDS]
    conn.execute('INSERT INTO profile_groups (company_id, name, fields, is_sales, created_at, updated_at) VALUES (?,?,?,1,?,?)',
                 (oid, 'Sales', json.dumps(fields), ts(), ts()))
    conn.commit()


def groups(conn, oid, active=True):
    ensure_defaults(conn, oid)
    q = 'SELECT * FROM profile_groups WHERE company_id=?' + (' AND is_active=1' if active else '') + ' ORDER BY sort, id'
    return [_row(r) for r in conn.execute(q, (oid,))]


def group(conn, oid, gid):
    r = conn.execute('SELECT * FROM profile_groups WHERE id=? AND company_id=?', (gid, oid)).fetchone()
    return _row(r) if r else None


def _sales_group_id(conn, oid):
    r = conn.execute('SELECT id FROM profile_groups WHERE company_id=? AND is_sales=1 AND is_active=1 ORDER BY id LIMIT 1',
                     (oid,)).fetchone()
    return r['id'] if r else 0


_HAS_SALES = ("(c.yearly_target IS NOT NULL OR c.team_direct IS NOT NULL OR c.team_indirect IS NOT NULL "
              "OR COALESCE(c.product_handles,'') NOT IN ('','[]') OR COALESCE(c.function_tags,'') NOT IN ('','[]') "
              "OR COALESCE(c.geography_tags,'') NOT IN ('','[]'))")


def effective_group(conn, oid, cand):
    """The candidate's group; one with sales data and no group yet counts as Sales."""
    gid = int(cand['profile_group_id'] or 0) if 'profile_group_id' in cand.keys() else 0
    if gid:
        return gid
    has = conn.execute('SELECT 1 FROM candidates c WHERE c.id=? AND ' + _HAS_SALES, (cand['id'],)).fetchone()
    return _sales_group_id(conn, oid) if has else 0


def _me():
    return int(session.get('user_id') or 0)


def _oid():
    return int(effective_company_id() or 0)


def _err(msg, code=400):
    return jsonify({'error': msg}), code


@bp.route('/api/profile-groups', methods=['GET'])
@login_required
def list_groups():
    conn = get_db()
    try:
        out = groups(conn, _oid())
        counts = {}
        for r in conn.execute('SELECT profile_group_id g, COUNT(*) n FROM candidates WHERE owner_id=? '
                              'AND COALESCE(profile_group_id,0)>0 GROUP BY profile_group_id', (_oid(),)):
            counts[r['g']] = r['n']
        for g in out:
            g['candidates'] = counts.get(g['id'], 0)
            g['can_delete'] = bool(is_company_admin() or g['created_by'] == _me())
        system = [{'sys': k, 'label': v['label'], 'kind': v['kind'], 'unit': v.get('unit', ''),
                   'options': v.get('options', []), 'tag_type': v.get('tag_type', '')} for k, v in SYSTEM.items()]
        return jsonify({'ok': True, 'groups': out, 'system': system})
    finally:
        conn.close()


@bp.route('/api/profile-groups', methods=['POST'])
@login_required
def create_group():
    d = request.get_json(silent=True) or {}
    name = re.sub(r'\s+', ' ', str(d.get('name') or '')).strip()[:40]
    if not name:
        return _err('Give the group a name, e.g. Production.')
    try:
        fields = clean_fields(d.get('fields'))
    except ValueError as e:
        return _err(str(e))
    oid = _oid()
    conn = get_db()
    try:
        ensure_defaults(conn, oid)
        if conn.execute('SELECT 1 FROM profile_groups WHERE company_id=? AND is_active=1 AND LOWER(name)=?',
                        (oid, name.lower())).fetchone():
            return _err('A group called "%s" already exists.' % name)
        cur = conn.execute('INSERT INTO profile_groups (company_id, name, fields, sort, created_by, created_at, updated_at) '
                           'VALUES (?,?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM profile_groups WHERE company_id=?),?,?,?)',
                           (oid, name, json.dumps(fields), oid, _me(), ts(), ts()))
        conn.commit()
        return jsonify({'ok': True, 'group': group(conn, oid, cur.lastrowid)})
    finally:
        conn.close()


@bp.route('/api/profile-groups/<int:gid>', methods=['PUT'])
@login_required
def update_group(gid):
    d = request.get_json(silent=True) or {}
    oid = _oid()
    conn = get_db()
    try:
        g = group(conn, oid, gid)
        if not g:
            return _err('Not found', 404)
        name = re.sub(r'\s+', ' ', str(d.get('name') or g['name'])).strip()[:40]
        try:
            fields = clean_fields(d.get('fields', g['fields']), old=g['fields'])
        except ValueError as e:
            return _err(str(e))
        if conn.execute('SELECT 1 FROM profile_groups WHERE company_id=? AND is_active=1 AND LOWER(name)=? AND id!=?',
                        (oid, name.lower(), gid)).fetchone():
            return _err('A group called "%s" already exists.' % name)
        conn.execute('UPDATE profile_groups SET name=?, fields=?, updated_at=? WHERE id=?', (name, json.dumps(fields), ts(), gid))
        conn.commit()
        return jsonify({'ok': True, 'group': group(conn, oid, gid)})
    finally:
        conn.close()


@bp.route('/api/profile-groups/<int:gid>', methods=['DELETE'])
@login_required
def delete_group(gid):
    oid = _oid()
    conn = get_db()
    try:
        g = group(conn, oid, gid)
        if not g:
            return _err('Not found', 404)
        if not (is_company_admin() or g['created_by'] == _me()):
            return _err('Only the person who made this group, or your admin, can remove it.', 403)
        conn.execute('UPDATE profile_groups SET is_active=0, updated_at=? WHERE id=?', (ts(), gid))
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


@bp.route('/api/profile-groups/<int:gid>/suggest', methods=['GET'])
@login_required
def suggest(gid):
    key = str(request.args.get('key') or '')
    oid = _oid()
    conn = get_db()
    try:
        if not group(conn, oid, gid):
            return _err('Not found', 404)
        counts = {}
        for r in conn.execute('SELECT value_json FROM candidate_profile_values WHERE company_id=? AND group_id=? AND field_key=? '
                              'ORDER BY updated_at DESC LIMIT 2000', (oid, gid, key)):
            try:
                for t in json.loads(r['value_json'] or '[]'):
                    k = str(t).strip()
                    if k:
                        counts[k] = counts.get(k, 0) + 1
            except Exception:
                pass
        return jsonify({'ok': True, 'values': [k for k, _n in sorted(counts.items(), key=lambda x: (-x[1], x[0].lower()))][:60]})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  Values on a candidate
# ══════════════════════════════════════════════════════════════════════════
def _cand(conn, oid, cid):
    return conn.execute('SELECT * FROM candidates WHERE id=? AND owner_id=?', (cid, oid)).fetchone()


def values_of(conn, cid, gid):
    out = {}
    for r in conn.execute('SELECT field_key, value_text, value_num, value_json FROM candidate_profile_values '
                          'WHERE candidate_id=? AND group_id=?', (cid, gid)):
        if r['value_json']:
            try:
                out[r['field_key']] = json.loads(r['value_json'])
                continue
            except Exception:
                pass
        out[r['field_key']] = r['value_num'] if r['value_num'] is not None else r['value_text']
    return out


def _coerce(f, v):
    """(value_text, value_num, value_json) for a field, or None when empty."""
    t = f['type']
    if t == 'number':
        try:
            s = str(v).replace(',', '').strip()
            if s == '':
                return None
            n = float(s)
        except (TypeError, ValueError):
            raise ValueError('"%s" must be a number.' % f['label'])
        if n < 0 or n > 1e9:
            raise ValueError('"%s" is out of range.' % f['label'])
        return (('%g' % n), n, '')
    if t == 'tags':
        items = v if isinstance(v, list) else re.split(r'[,;\n]', str(v or ''))
        seen, out = set(), []
        for x in items:
            x = re.sub(r'\s+', ' ', str(x)).strip()[:60]
            if x and x.lower() not in seen:
                seen.add(x.lower())
                out.append(x)
        if not out:
            return None
        return (' | '.join(out[:20]), None, json.dumps(out[:20]))
    if t == 'choice':
        s = str(v or '').strip()
        if not s:
            return None
        match = [o for o in f.get('options', []) if o.lower() == s.lower()]
        if not match:
            raise ValueError('"%s" must be one of: %s.' % (f['label'], ', '.join(f.get('options', []))))
        return (match[0], None, '')
    s = re.sub(r'\s+', ' ', str(v or '')).strip()[:300]
    return (s, None, '') if s else None


@bp.route('/api/candidates/<int:cid>/profile', methods=['GET'])
@login_required
def get_profile(cid):
    oid = _oid()
    conn = get_db()
    try:
        c = _cand(conn, oid, cid)
        if not c:
            return _err('Not found', 404)
        ensure_defaults(conn, oid)
        gid = effective_group(conn, oid, c)
        return jsonify({'ok': True, 'group_id': gid, 'values': values_of(conn, cid, gid) if gid else {}})
    finally:
        conn.close()


@bp.route('/api/candidates/<int:cid>/profile', methods=['PUT'])
@login_required
def save_profile(cid):
    d = request.get_json(silent=True) or {}
    oid = _oid()
    try:
        gid = int(d.get('group_id') or 0)
    except (TypeError, ValueError):
        gid = 0
    conn = get_db()
    try:
        c = _cand(conn, oid, cid)
        if not c:
            return _err('Not found', 404)
        g = group(conn, oid, gid) if gid else None
        if gid and not g:
            return _err('That group does not exist.')
        vals = d.get('values') or {}
        rows = []
        if g:
            for f in g['fields']:
                if f['type'] == 'system':
                    continue
                try:
                    v = _coerce(f, vals.get(f['key']))
                except ValueError as e:
                    return _err(str(e))
                rows.append((f['key'], v))
        old_gid = int(c['profile_group_id'] or 0) if 'profile_group_id' in c.keys() else 0
        now = ts()
        for key, v in rows:
            if v is None:
                conn.execute('DELETE FROM candidate_profile_values WHERE candidate_id=? AND group_id=? AND field_key=?',
                             (cid, gid, key))
            else:
                conn.execute('INSERT INTO candidate_profile_values (candidate_id, group_id, field_key, company_id, value_text, '
                             'value_num, value_json, updated_at) VALUES (?,?,?,?,?,?,?,?) '
                             'ON CONFLICT(candidate_id, group_id, field_key) DO UPDATE SET value_text=excluded.value_text, '
                             'value_num=excluded.value_num, value_json=excluded.value_json, updated_at=excluded.updated_at',
                             (cid, gid, key, oid, v[0], v[1], v[2], now))
        conn.execute('UPDATE candidates SET profile_group_id=?, updated_at=? WHERE id=?', (gid, now, cid))
        conn.commit()
        if gid != old_gid:
            try:
                _core().log_candidate_event(cid, 'note', ('Profile group: ' + g['name']) if g else 'Profile group removed')
            except Exception:
                pass
        return jsonify({'ok': True, 'group_id': gid, 'values': values_of(conn, cid, gid) if gid else {}})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  Search (used by server._talent_where and central_search)
# ══════════════════════════════════════════════════════════════════════════
KEYWORD_SQL = ("COALESCE((SELECT GROUP_CONCAT(v.value_text, ' ') FROM candidate_profile_values v "
               "WHERE v.candidate_id=c.id AND v.group_id=COALESCE(c.profile_group_id,0)),'')")


def talent_filter(args, oid):
    """(sql fragments, params) for ?pgroup= and pf_/pfmin_/pfmax_ filters."""
    where, params = [], []
    try:
        gid = int(args.get('pgroup') or 0)
    except (TypeError, ValueError):
        gid = 0
    if not gid:
        return where, params
    conn = get_db()
    try:
        g = group(conn, oid, gid)
    finally:
        conn.close()
    if not g:
        where.append('0')
        return where, params
    if g['is_sales']:
        where.append('(c.profile_group_id=? OR (COALESCE(c.profile_group_id,0)=0 AND ' + _HAS_SALES + '))')
    else:
        where.append('c.profile_group_id=?')
    params.append(gid)
    for f in g['fields']:
        if f['type'] == 'system':
            continue
        k = f['key']
        sub = 'EXISTS (SELECT 1 FROM candidate_profile_values v WHERE v.candidate_id=c.id AND v.group_id=? AND v.field_key=? AND '
        if f['type'] == 'number':
            for arg, op in (('pfmin_' + k, '>='), ('pfmax_' + k, '<=')):
                raw = str(args.get(arg) or '').replace(',', '').strip()
                if raw:
                    try:
                        n = float(raw)
                    except ValueError:
                        continue
                    where.append(sub + 'v.value_num ' + op + ' ?)')
                    params += [gid, k, n]
        else:
            raw = str(args.get('pf_' + k) or '').strip().lower()
            if raw:
                opts = [o.strip() for o in re.split(r'[,;]', raw) if o.strip()][:10]
                if f['type'] == 'choice':
                    where.append(sub + '(' + ' OR '.join(['LOWER(v.value_text)=?'] * len(opts)) + '))')
                    params += [gid, k] + opts
                else:
                    where.append(sub + '(' + ' OR '.join(['LOWER(v.value_text) LIKE ?'] * len(opts)) + '))')
                    params += [gid, k] + ['%' + o + '%' for o in opts]
    return where, params


def summaries(conn, oid, rows):
    """id -> {group, items:[[label, text]]} for Talent result rows (custom fields, first 4)."""
    out = {}
    try:
        gs = {g['id']: g for g in groups(conn, oid, active=False)}
    except Exception:
        return out
    by = {}
    for r in rows:
        gid = int(r['profile_group_id'] or 0) if 'profile_group_id' in r.keys() else 0
        if gid and gid in gs:
            by.setdefault(gid, []).append(r['id'])
    for gid, ids in by.items():
        g = gs[gid]
        vals = {}
        for v in conn.execute('SELECT candidate_id, field_key, value_text FROM candidate_profile_values WHERE group_id=? '
                              'AND candidate_id IN (%s)' % ','.join('?' * len(ids)), [gid] + ids):
            vals.setdefault(v['candidate_id'], {})[v['field_key']] = v['value_text']
        for cid in ids:
            items = []
            for f in g['fields']:
                if f['type'] == 'system':
                    continue
                t = vals.get(cid, {}).get(f['key'])
                if t:
                    items.append([f['label'], t + ((' ' + f['unit']) if f.get('unit') and f['type'] == 'number' else '')])
                if len(items) >= 4:
                    break
            out[cid] = {'group': g['name'], 'items': items}
    return out
