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


@bp.route('/company', methods=['GET'])
@login_required
def company():
    key = company_key(request.args.get('name'))
    if not key:
        return jsonify({'error': 'Pick a company.'}), 400
    want_former = request.args.get('former') == '1'
    oid = effective_company_id()
    sc, sp = _scope()
    cols = ('c.id, c.mandate_id, c.name, c.company, c.designation, c.experience, c.ctc_current, c.ctc_expected, '
            'c.notice_period, c.location, c.product_handles, c.geography_tags, c.function_tags, c.yearly_target, '
            'c.team_direct, c.team_indirect, '
            'c.qualification, c.gender, c.stage, c.cv_path, c.phone, c.email, c.updated_at, '
            'COALESCE(NULLIF(c.copied_from,0), c.id) AS pk, m.status AS m_status, m.role AS m_role')
    conn = get_db()
    try:
        # pre-filter in SQL on the first word, then match the normalised key exactly
        first = key.split(' ')[0]
        rows = conn.execute('SELECT ' + cols + ' FROM candidates c LEFT JOIN mandates m ON m.id=c.mandate_id '
                            'WHERE c.owner_id=? AND LOWER(c.company) LIKE ? ' + sc + ' ORDER BY c.updated_at DESC',
                            [oid, '%' + first + '%'] + sp).fetchall()
        seen, current = set(), []
        for r in rows:
            if company_key(r['company']) != key or r['pk'] in seen:
                continue
            seen.add(r['pk'])
            current.append(_person(r))
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
                for r in conn.execute('SELECT ' + cols + ' FROM candidates c LEFT JOIN mandates m ON m.id=c.mandate_id '
                                      'WHERE c.id IN (%s)' % ','.join('?' * len(hits)), list(hits)).fetchall():
                    if r['pk'] in seen:
                        continue
                    seen.add(r['pk'])
                    former.append(_person(r, hits[r['id']]))
    finally:
        conn.close()
    name = current[0]['current_company'] if current else request.args.get('name', '')
    return jsonify({'ok': True, 'company': name, 'key': key, 'people': current, 'former': former})
