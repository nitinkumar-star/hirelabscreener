"""
RecruitOS — Company Talent Map  (Oct 2026)

Who do we know at a company, side by side: designation, experience, CTC,
product handling, geography handling and yearly target — with charts, and a
client view (names / contacts / CTC hidden) that prints as a clean PDF.

  GET /talent-map                               the page (talent-map.html)
  GET /api/talent-map/companies?q=              companies in the database, with head counts
  GET /api/talent-map/company?name=&former=1    the people (current + optionally former) of one company

Company names are matched on a normalised key ("KEI Wires & Cables Ltd" and
"kei wires and cables" are the same company). One row per person (records of the
same person on several jobs are linked by copied_from). Recruiters only see the
candidates of the jobs they work on (same scope as Talent search).
"""

import os
import re
import json

from flask import Blueprint, request, jsonify, redirect, Response

from modules.shared import get_db, effective_company_id, login_required, _core

bp = Blueprint('talent_map', __name__, url_prefix='/api/talent-map')
pages = Blueprint('talent_map_pages', __name__)

_SUFFIX = re.compile(r'\b(pvt|private|ltd|limited|llp|inc|corp|corporation|co|company|india|the)\b')


def company_key(name):
    s = str(name or '').lower().replace('&', ' and ')
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    s = _SUFFIX.sub(' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _jl(v):
    try:
        x = json.loads(v or '[]')
        return [str(i) for i in x if str(i).strip()] if isinstance(x, list) else []
    except Exception:
        return []


def _scope():
    try:
        return _core()._cs('c.mandate_id')
    except Exception:
        return '', []


@pages.route('/talent-map')
def talent_map_page():
    if not _core().current_user():
        return redirect('/')
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'talent-map.html')
    with open(path, encoding='utf-8') as f:
        return Response(f.read(), mimetype='text/html')


@bp.route('/companies', methods=['GET'])
@login_required
def companies():
    q = company_key(request.args.get('q'))
    sc, sp = _scope()
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT c.company, COUNT(DISTINCT COALESCE(NULLIF(c.copied_from,0), c.id)) n FROM candidates c "
            "WHERE c.owner_id=? AND TRIM(COALESCE(c.company,''))!='' " + sc + " GROUP BY LOWER(TRIM(c.company))",
            [effective_company_id()] + sp).fetchall()
    finally:
        conn.close()
    by = {}
    for r in rows:
        k = company_key(r['company'])
        if not k or (q and q not in k):
            continue
        e = by.setdefault(k, {'key': k, 'name': r['company'].strip(), 'count': 0, '_best': 0})
        e['count'] += r['n']
        if r['n'] > e['_best']:                 # show the most used spelling
            e['name'], e['_best'] = r['company'].strip(), r['n']
    out = sorted(by.values(), key=lambda e: (-e['count'], e['name'].lower()))[:40]
    for e in out:
        e.pop('_best', None)
    return jsonify({'ok': True, 'companies': out})


def _person(r, former=None):
    return {
        'id': r['id'], 'mandate_id': r['mandate_id'], 'name': r['name'] or '',
        'designation': (former or {}).get('designation') or r['designation'] or '',
        'current_company': r['company'] or '', 'current_designation': r['designation'] or '',
        'experience': r['experience'] or 0, 'ctc_current': r['ctc_current'] or 0, 'ctc_expected': r['ctc_expected'] or 0,
        'notice_period': r['notice_period'] or 0, 'location': r['location'] or '',
        'products': _jl(r['product_handles']), 'geography': _jl(r['geography_tags']),
        'functions': _jl(r['function_tags']), 'yearly_target': r['yearly_target'],
        'team_direct': r['team_direct'], 'team_indirect': r['team_indirect'],
        'team_total': (r['team_direct'] or 0) + (r['team_indirect'] or 0),
        'qualification': r['qualification'] or '', 'gender': r['gender'] or '',
        'stage': r['stage'] or '', 'job': ('Central Database' if r['m_status'] == 'central' else (r['m_role'] or '')),
        'has_cv': bool(r['cv_path']), 'phone': r['phone'] or '', 'email': r['email'] or '',
        'updated_at': r['updated_at'] or '', 'former': bool(former),
        'former_period': ((former or {}).get('period') or ''),
    }


_COLS = ('c.id, c.mandate_id, c.name, c.company, c.designation, c.experience, c.ctc_current, c.ctc_expected, '
         'c.notice_period, c.location, c.product_handles, c.geography_tags, c.function_tags, c.yearly_target, '
         'c.team_direct, c.team_indirect, '
         'c.qualification, c.gender, c.stage, c.cv_path, c.phone, c.email, c.updated_at, '
         'COALESCE(NULLIF(c.copied_from,0), c.id) AS pk, m.status AS m_status, m.role AS m_role')


def company_people(conn, oid, key, want_former=True):
    """[(person, row)] for the current and the former employees of a company,
    one entry per person, in the caller's agency + recruiter scope."""
    sc, sp = _scope()
    # pre-filter in SQL on the first word, then match the normalised key exactly
    first = key.split(' ')[0]
    rows = conn.execute('SELECT ' + _COLS + ' FROM candidates c LEFT JOIN mandates m ON m.id=c.mandate_id '
                        'WHERE c.owner_id=? AND LOWER(c.company) LIKE ? ' + sc + ' ORDER BY c.updated_at DESC',
                        [oid, '%' + first + '%'] + sp).fetchall()
    seen, current = set(), []
    for r in rows:
        if company_key(r['company']) != key or r['pk'] in seen:
            continue
        seen.add(r['pk'])
        current.append((_person(r), r))
    former = []
    if want_former:
        wh = conn.execute(
            'SELECT w.candidate_id, w.company, w.designation, w.start_date, w.end_date FROM work_history w '
            'JOIN candidates c ON c.id=w.candidate_id WHERE c.owner_id=? AND LOWER(w.company) LIKE ? '
            'AND COALESCE(w.is_current,0)=0 ' + sc, [oid, '%' + first + '%'] + sp).fetchall()
        hits = {}
        for w in wh:
            if company_key(w['company']) == key and w['candidate_id'] not in hits:
                hits[w['candidate_id']] = {'designation': w['designation'] or '',
                                           'period': ' – '.join(x for x in (w['start_date'], w['end_date']) if x)}
        if hits:
            for r in conn.execute('SELECT ' + _COLS + ' FROM candidates c LEFT JOIN mandates m ON m.id=c.mandate_id '
                                  'WHERE c.id IN (%s)' % ','.join('?' * len(hits)), list(hits)).fetchall():
                if r['pk'] in seen:
                    continue
                seen.add(r['pk'])
                former.append((_person(r, hits[r['id']]), r))
    return current, former


# ══════════════════════════════════════════════════════════════════════════
#  Profile groups on the company card (Nov 2026)
#  Every person is shown with THEIR profile group (Sales, Production, Tender,
#  Design …) and that group's own fields — not only the built-in sales fields.
# ══════════════════════════════════════════════════════════════════════════
_SYS_PERSON = {'yearly_target': 'yearly_target', 'team_direct': 'team_direct', 'team_indirect': 'team_indirect',
               'product_handles': 'products', 'function_tags': 'functions', 'geography_tags': 'geography',
               'gender': 'gender'}


def _field_kind(f):
    """number | tags | choice | text — for a custom field or a built-in (system) one."""
    if f.get('type') == 'system':
        try:
            from modules.profile_groups import SYSTEM
            return SYSTEM.get(f.get('sys'), {}).get('kind', 'text')
        except Exception:
            return 'text'
    return f.get('type') or 'text'


def _field_unit(f):
    if f.get('type') == 'system':
        try:
            from modules.profile_groups import SYSTEM
            return SYSTEM.get(f.get('sys'), {}).get('unit', '')
        except Exception:
            return ''
    return f.get('unit', '')


def attach_profiles(conn, oid, pairs):
    """Give every person dict 'pgroup' (their profile group id, 0 = none) and 'pvals'
    (that group's custom field values: number, list for tags, text).
    Returns the groups in use: [{id, name, is_sales, fields}]. For the client view,
    call client_strip() AFTER profile_summary() (the bands need the numbers)."""
    try:
        from modules import profile_groups as pg
        gs = {g['id']: g for g in pg.groups(conn, oid, active=False)}
        sales_gid = pg._sales_group_id(conn, oid)
    except Exception:
        for p, _r in pairs:
            p['pgroup'], p['pvals'] = 0, {}
        return []
    pks = sorted({r['pk'] for _p, r in pairs})
    recs = {}
    for i in range(0, len(pks), 400):
        part = pks[i:i + 400]
        q = ','.join('?' * len(part))
        for x in conn.execute('SELECT id, COALESCE(NULLIF(copied_from,0), id) AS pk, COALESCE(profile_group_id,0) AS gid, '
                              'updated_at FROM candidates WHERE owner_id=? AND (id IN (%s) OR copied_from IN (%s))' % (q, q),
                              [oid] + part + part):
            recs.setdefault(x['pk'], []).append(x)
    for p, r in pairs:
        lst = sorted(recs.get(r['pk'], []), key=lambda x: x['updated_at'] or '', reverse=True)
        gid = next((int(x['gid']) for x in lst if int(x['gid'] or 0)), 0)
        if not gid and (p.get('yearly_target') or p.get('team_direct') is not None or p.get('team_indirect') is not None
                        or p.get('products') or p.get('functions') or p.get('geography')):
            gid = sales_gid                      # sales data and no group yet = Sales (same rule as the profile)
        p['pgroup'] = gid if gid in gs else 0
        p['pvals'] = {}
    # custom values of each person's group, newest value of each field across their records
    owner = {}
    for p, r in pairs:
        if p['pgroup']:
            for x in recs.get(r['pk'], []):
                owner[x['id']] = p
    ids = list(owner)
    newest = {}
    for i in range(0, len(ids), 400):
        part = ids[i:i + 400]
        for v in conn.execute('SELECT candidate_id, group_id, field_key, value_text, value_num, value_json, updated_at '
                              'FROM candidate_profile_values WHERE company_id=? AND candidate_id IN (%s)'
                              % ','.join('?' * len(part)), [oid] + part):
            p = owner[v['candidate_id']]
            if v['group_id'] != p['pgroup']:
                continue
            k = (id(p), v['field_key'])
            if k in newest and (newest[k][0] or '') >= (v['updated_at'] or ''):
                continue
            if v['value_json']:
                try:
                    val = json.loads(v['value_json'])
                except Exception:
                    val = v['value_text']
            else:
                val = v['value_num'] if v['value_num'] is not None else v['value_text']
            newest[k] = (v['updated_at'], val)
            p['pvals'][v['field_key']] = val
    used = []
    for gid in sorted({p['pgroup'] for p, _r in pairs if p['pgroup']}):
        g = gs[gid]
        fields = [dict(f, kind=_field_kind(f), unit=_field_unit(f)) for f in g['fields']]
        used.append({'id': g['id'], 'name': g['name'], 'is_sales': g['is_sales'], 'fields': fields})
    return used


def client_strip(people, groups):
    """Client view: per person only tags / choice values of the profile fields stay —
    numbers and free text never leave the server for one person."""
    keep = {g['id']: {f['key'] for f in g['fields'] if f['kind'] in ('tags', 'choice') and f.get('type') != 'system'}
            for g in groups}
    for p in people:
        k = keep.get(p.get('pgroup'), set())
        p['pvals'] = {x: v for x, v in (p.get('pvals') or {}).items() if x in k}


def _field_value(p, f):
    if f.get('type') == 'system':
        return p.get(_SYS_PERSON.get(f.get('sys'), ''), None)
    return p.get('pvals', {}).get(f['key'])


def profile_summary(groups, people, client=False, min_n=3):
    """Per group, per field: numbers -> n + median / min / max (n >= min_n);
    tags / choice -> counts; text -> how many filled. Never a person's value."""
    import statistics
    out = []
    for g in groups:
        ps = [p for p in people if p.get('pgroup') == g['id']]
        if not ps:
            continue
        fields = []
        for f in g['fields']:
            kind = f.get('kind') or _field_kind(f)
            vals = [_field_value(p, f) for p in ps]
            row = {'key': f['key'], 'label': f.get('label', ''), 'kind': kind, 'unit': f.get('unit') or ''}
            if kind == 'number':
                nums = []
                for v in vals:
                    try:
                        if v is not None and str(v).strip() != '' and float(v) > 0:
                            nums.append(float(v))
                    except (TypeError, ValueError):
                        pass
                row['n'] = len(nums)
                if len(nums) >= min_n:
                    row.update({'median': round(statistics.median(nums), 2), 'min': round(min(nums), 2),
                                'max': round(max(nums), 2)})
                    if not client:
                        row['total'] = round(sum(nums), 2)
                else:
                    row['small'] = True
            elif kind in ('tags', 'choice'):
                cnt = {}
                filled = 0
                for v in vals:
                    items = v if isinstance(v, list) else ([v] if v not in (None, '') else [])
                    items = [str(x).strip() for x in items if str(x).strip()]
                    if items:
                        filled += 1
                    for x in items:
                        cnt[x] = cnt.get(x, 0) + 1
                row['n'] = filled
                row['top'] = [[k, c] for k, c in sorted(cnt.items(), key=lambda x: (-x[1], x[0].lower()))][:12]
            else:
                row['n'] = sum(1 for v in vals if v not in (None, '') and str(v).strip())
            fields.append(row)
        out.append({'group_id': g['id'], 'name': g['name'], 'is_sales': g['is_sales'], 'people': len(ps), 'fields': fields})
    return out


@bp.route('/company', methods=['GET'])
@login_required
def company():
    key = company_key(request.args.get('name'))
    if not key:
        return jsonify({'error': 'Pick a company.'}), 400
    want_former = request.args.get('former') == '1'
    client = request.args.get('view') == 'client'
    oid = effective_company_id()
    conn = get_db()
    try:
        cur, frm = company_people(conn, oid, key, want_former)
        groups = attach_profiles(conn, oid, cur + frm)
    finally:
        conn.close()
    current, former = [p for p, _r in cur], [p for p, _r in frm]
    summary = profile_summary(groups, current, client)           # bands first (they need the numbers) …
    if client:
        client_strip(current + former, groups)                   # … then no per-person numbers / text
    name = current[0]['current_company'] if current else request.args.get('name', '')
    return jsonify({'ok': True, 'company': name, 'key': key, 'people': current, 'former': former, 'groups': groups,
                    'group_summary': summary})
