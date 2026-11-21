"""
RecruitOS — Insight Engine, Wave 2: Company Intelligence  (Nov 2026)

One answer to "what do we know about this company's talent and market",
built from the candidates we already have plus the Wave 1 capture data
(candidate_reasons, market_notes, role_family / level_band). It is the
"Intelligence" tab of the Company Talent Map (/talent-map).

  GET  /api/talent-map/intel?name=&view=internal|client&refresh=1
  GET  /api/talent-map/intel/config          {on, is_admin}
  POST /api/talent-map/intel/config          {on}             (agency admin)

What the answer holds
  people       current / former counts, split role family x level band
  pay          per role family x level band: n, min / median / max current CTC,
               median expected CTC, median hike ask % (both CTCs > 0).
               A band is shown only when n >= 3.
  notice       0-30 / 31-60 / 61-90 / 90+ days
  openness     of the people we actually contacted (reached Called or any later
               stage, Not Interested included): share who reached Interested or
               later, share who said no, with n
  reasons      top reasons for "no" (+ recent detail lines, internal view only)
  notes        market notes timeline (text in the internal view only)
  client       when the company is one of our clients: client rejection reasons /
               feedback across its jobs and why candidates said no to it
  quality      % with expected CTC, % with role family, % of "no"s with a reason

Client view (view=client) is enforced HERE, on the server: no names, no ids, no
individual CTCs, no free text (reason details, client feedback, note text) —
only counts, percentages and pay bands of 3 or more people.

Scope: the caller's agency (owner_id) and, for a recruiter, only the jobs they
work on (same rule as the Talent Map). Read-only: no new tables or columns.
Answers are cached 10 minutes per (agency, company, scope, view); Wave 1 clears
the company's cache whenever a reason or market note is written.

ROLLBACK: remove 'insight_company' from modules/__init__.py, or switch
"Company intelligence" off in Settings → My Account → Insight capture.
"""

import time
import threading
import statistics

from flask import Blueprint, request, jsonify

from modules.shared import get_db, ts, effective_company_id, is_company_admin, login_required, _core
from modules import register_migration

bp = Blueprint('insight_company', __name__)

CACHE_SECONDS = 600
MIN_N = 3
NOT_TAGGED = 'Not tagged'

POSITIVE = {'interested', 'updated cv awaited', 'shared with client', 'interview inprocess', 'placed', 'joined',
            'client rejected on paper', 'client rejected after interview'}
NEGATIVE = {'not interested', 'interview backout'}
CONTACTED = POSITIVE | NEGATIVE | {'called'}
NOTICE_BUCKETS = [('0–30 days', 0, 30), ('31–60 days', 31, 60), ('61–90 days', 61, 90), ('90+ days', 91, 10 ** 6)]


@register_migration
def migrate(conn):
    # the per-agency switch lives in the Wave 1 settings table (same schema, idempotent)
    conn.execute('''CREATE TABLE IF NOT EXISTS insight_settings (
        owner_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        value TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        PRIMARY KEY (owner_id, key)
    )''')
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════
def company_key(name):
    from modules.talent_map import company_key as _ck
    return _ck(name)


def _ic():
    """Wave 1 catalogue (labels, families). None when Wave 1 is rolled back."""
    try:
        from modules import insight_capture
        return insight_capture
    except Exception:
        return None


def _scope():
    try:
        return _core()._cs('c.mandate_id')
    except Exception:
        return '', []


def _scope_sig():
    try:
        from modules.access import scoped_user
        su = scoped_user()
        return su[0] if su else 0
    except Exception:
        return 0


def intel_on(conn, oid):
    r = conn.execute("SELECT value FROM insight_settings WHERE owner_id=? AND key='insight_company_on'", (oid,)).fetchone()
    return True if r is None else str(r['value'] or '').strip().lower() not in ('0', 'false', 'off', 'no', '')


def _stage(s):
    return ' '.join(str(s or '').lower().split())


def _median(xs):
    return round(statistics.median(xs), 2) if xs else None


def _pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def _label(code):
    ic = _ic()
    return ic.reason_label(code) if ic else code


def _family_of(code_stage):
    ic = _ic()
    return ic.family_for(code_stage) if ic else None


def _jl(v):
    import json
    try:
        x = json.loads(v or '[]')
        return [str(i) for i in x if str(i).strip()] if isinstance(x, list) else []
    except Exception:
        return []


def _family(r):
    fam = (r['role_family'] or '').strip()
    if fam:
        return fam
    ic = _ic()
    if ic:
        fam = ic.family_from_tags(_jl(r['function_tags']))
    return fam or NOT_TAGGED


# ══════════════════════════════════════════════════════════════════════════
#  Cache
# ══════════════════════════════════════════════════════════════════════════
_CACHE = {}
_LOCK = threading.Lock()


def invalidate(oid, key=None):
    """Drop cached answers for an agency (one company, or all of them)."""
    with _LOCK:
        for k in [k for k in _CACHE if k[0] == oid and (key is None or k[1] == key)]:
            _CACHE.pop(k, None)


def _cached(ck):
    with _LOCK:
        hit = _CACHE.get(ck)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    return None


def _store(ck, data):
    with _LOCK:
        if len(_CACHE) > 500:                       # keep memory small
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:250]:
                _CACHE.pop(k, None)
        _CACHE[ck] = (time.time(), data)


# ══════════════════════════════════════════════════════════════════════════
#  The calculation
# ══════════════════════════════════════════════════════════════════════════
_COLS = ('c.id, c.mandate_id, c.company, c.designation, c.experience, c.ctc_current, c.ctc_expected, c.notice_period, '
         'c.function_tags, c.role_family, c.level_band, c.stage, c.updated_at, '
         'COALESCE(NULLIF(c.copied_from,0), c.id) AS pk')


def _people(conn, oid, key, sc, sp):
    """current: {pk: newest row}, records: {pk: [ids]}, former: {pk: row}"""
    first = key.split(' ')[0]
    rows = conn.execute('SELECT ' + _COLS + ' FROM candidates c WHERE c.owner_id=? AND LOWER(c.company) LIKE ?' + sc +
                        ' ORDER BY c.updated_at DESC, c.id DESC', [oid, '%' + first + '%'] + sp).fetchall()
    current = {}
    for r in rows:
        if company_key(r['company']) == key and r['pk'] not in current:
            current[r['pk']] = r
    records = {pk: [] for pk in current}
    if current:
        pks = list(current)
        for i in range(0, len(pks), 500):
            part = pks[i:i + 500]
            q = ','.join('?' * len(part))
            for r in conn.execute('SELECT c.id, COALESCE(NULLIF(c.copied_from,0), c.id) AS pk FROM candidates c '
                                  'WHERE c.owner_id=? AND (c.id IN (%s) OR c.copied_from IN (%s))' % (q, q) + sc,
                                  [oid] + part + part + sp):
                if r['pk'] in records:
                    records[r['pk']].append(r['id'])
    former = {}
    wh = conn.execute('SELECT w.candidate_id, w.company FROM work_history w JOIN candidates c ON c.id=w.candidate_id '
                      'WHERE c.owner_id=? AND LOWER(w.company) LIKE ? AND COALESCE(w.is_current,0)=0' + sc,
                      [oid, '%' + first + '%'] + sp).fetchall()
    ids = sorted({w['candidate_id'] for w in wh if company_key(w['company']) == key})
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in conn.execute('SELECT ' + _COLS + ' FROM candidates c WHERE c.owner_id=? AND c.id IN (%s)' % ','.join('?' * len(part)),
                              [oid] + part):
            if r['pk'] not in current and r['pk'] not in former:
                former[r['pk']] = r
    return current, records, former


def _grid(rows):
    cells, fams, bands = {}, set(), set()
    for r in rows:
        f, b = _family(r), (r['level_band'] or '').strip() or NOT_TAGGED
        fams.add(f); bands.add(b)
        cells.setdefault(f, {}).setdefault(b, 0)
        cells[f][b] += 1
    ic = _ic()
    order_f = (ic.ROLE_FAMILIES if ic else []) + [NOT_TAGGED]
    order_b = (ic.LEVELS if ic else []) + [NOT_TAGGED]
    fam_l = [f for f in order_f if f in fams] + sorted(f for f in fams if f not in order_f)
    band_l = [b for b in order_b if b in bands] + sorted(b for b in bands if b not in order_b)
    return {'families': fam_l, 'bands': band_l, 'cells': cells}


def _pay(rows):
    groups = {}
    for r in rows:
        groups.setdefault((_family(r), (r['level_band'] or '').strip() or NOT_TAGGED), []).append(r)
    out = []
    for (f, b), rs in groups.items():
        cur = [float(r['ctc_current']) for r in rs if float(r['ctc_current'] or 0) > 0]
        exp = [float(r['ctc_expected']) for r in rs if float(r['ctc_expected'] or 0) > 0]
        hike = [100.0 * (float(r['ctc_expected']) - float(r['ctc_current'])) / float(r['ctc_current'])
                for r in rs if float(r['ctc_current'] or 0) > 0 and float(r['ctc_expected'] or 0) > 0]
        n = len(cur)
        row = {'family': f, 'band': b, 'n': n, 'people': len(rs)}
        if n >= MIN_N:
            row.update({'min': round(min(cur), 2), 'median': _median(cur), 'max': round(max(cur), 2),
                        'median_expected': _median(exp) if len(exp) >= MIN_N else None, 'n_expected': len(exp),
                        'median_hike_pct': round(statistics.median(hike), 1) if len(hike) >= MIN_N else None,
                        'n_hike': len(hike)})
        else:
            row['small'] = True
        out.append(row)
    out.sort(key=lambda x: (-x['n'], x['family'], x['band']))
    return out


def _notice(rows):
    out = []
    known = [int(r['notice_period'] or 0) for r in rows if int(r['notice_period'] or 0) > 0]
    for lab, lo, hi in NOTICE_BUCKETS:
        out.append({'bucket': lab, 'n': sum(1 for v in known if lo <= v <= hi)})
    return {'buckets': out, 'known': len(known)}


def _stages_by_person(conn, records):
    """{pk: set(stage)}: every stage each person ever reached on any job."""
    out = {pk: set() for pk in records}
    owner = {}
    for pk, ids in records.items():
        for i in ids:
            owner[i] = pk
    ids = list(owner)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        q = ','.join('?' * len(part))
        for r in conn.execute('SELECT id, stage FROM candidates WHERE id IN (%s)' % q, part):
            out[owner[r['id']]].add(_stage(r['stage']))
        for r in conn.execute('SELECT candidate_id, to_stage FROM stage_history WHERE candidate_id IN (%s)' % q, part):
            out[owner[r['candidate_id']]].add(_stage(r['to_stage']))
    return out


def _openness(stages):
    contacted = [s for s in stages.values() if s & CONTACTED]
    pos = sum(1 for s in contacted if s & POSITIVE)
    neg = sum(1 for s in contacted if not (s & POSITIVE) and (s & NEGATIVE))
    n = len(contacted)
    return {'contacted': n, 'interested_or_later': pos, 'said_no': neg, 'undecided': n - pos - neg,
            'openness_pct': _pct(pos, n), 'no_pct': _pct(neg, n), 'small': n < MIN_N}


def _reasons_for(conn, oid, cand_ids, client):
    """Latest reason per candidate record -> counts + recent details (candidate said no)."""
    if not cand_ids:
        return {'n': 0, 'top': [], 'recent': []}
    rows = []
    for i in range(0, len(cand_ids), 500):
        part = cand_ids[i:i + 500]
        rows += conn.execute('SELECT * FROM candidate_reasons WHERE owner_id=? AND candidate_id IN (%s) ORDER BY id DESC'
                             % ','.join('?' * len(part)), [oid] + part).fetchall()
    latest = {}
    for r in rows:
        if _family_of(r['stage']) in (None, 'candidate_no') and r['candidate_id'] not in latest:
            if r['reason_code'] in _codes('candidate_no'):
                latest[r['candidate_id']] = r
    return _summarise(latest.values(), client, with_feedback=False)


def _codes(fam):
    ic = _ic()
    return ic.CODES.get(fam, []) if ic else []


def _summarise(rows, client, with_feedback):
    rows = sorted(rows, key=lambda r: r['created_at'] or '', reverse=True)
    counts = {}
    for r in rows:
        counts[r['reason_code']] = counts.get(r['reason_code'], 0) + 1
    top = [{'code': c, 'label': _label(c), 'n': n} for c, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
    out = {'n': len(rows), 'top': top}
    if not client:
        recent = []
        for r in rows:
            txt = (r['client_feedback'] or '').strip() if with_feedback else ''
            det = (r['detail'] or '').strip()
            if txt or det:
                recent.append({'label': _label(r['reason_code']), 'detail': det, 'feedback': txt,
                               'stage': r['stage'], 'date': (r['created_at'] or '')[:10]})
            if len(recent) >= 3:
                break
        out['recent'] = recent
    return out


def _notes(conn, oid, key, sc, sp, client):
    rows = conn.execute('SELECT n.* FROM market_notes n LEFT JOIN candidates c ON c.id=n.candidate_id '
                        'WHERE n.owner_id=? AND n.company_key=? AND (COALESCE(n.candidate_id,0)=0 OR (c.owner_id=?' + sc + ')) '
                        'ORDER BY n.created_at DESC, n.id DESC LIMIT 200', [oid, key, oid] + sp).fetchall()
    counts = {}
    for r in rows:
        counts[r['signal_type']] = counts.get(r['signal_type'], 0) + 1
    out = {'n': len(rows), 'by_signal': [{'signal_type': s, 'n': n} for s, n in sorted(counts.items(), key=lambda x: -x[1])]}
    if not client:
        out['timeline'] = [{'signal_type': r['signal_type'], 'note': r['note'], 'company': r['company_name'],
                            'source': r['source'], 'date': (r['created_at'] or '')[:10]} for r in rows[:100]]
    return out


def _client_branch(conn, oid, key, sc, sp, client):
    crm = []
    try:
        crm = [r['id'] for r in conn.execute('SELECT id, name FROM crm_clients WHERE company_id=?', (oid,))
               if company_key(r['name']) == key]
    except Exception:
        pass
    mids = set()
    for r in conn.execute('SELECT id, client, crm_client_id FROM mandates WHERE owner_id=?', (oid,)):
        if company_key(r['client']) == key or (crm and (r['crm_client_id'] or 0) in crm):
            mids.add(r['id'])
    if not crm and not mids:
        return None
    # only the jobs this user may see
    seen_mids = sorted(mids)
    if sc and seen_mids:
        allowed = set()
        for i in range(0, len(seen_mids), 500):
            part = seen_mids[i:i + 500]
            allowed |= {r[0] for r in conn.execute('SELECT DISTINCT c.mandate_id FROM candidates c WHERE c.owner_id=? AND '
                                                   'c.mandate_id IN (%s)' % ','.join('?' * len(part)) + sc,
                                                   [oid] + part + sp)}
        seen_mids = sorted(allowed)
    rej, no = [], []
    for i in range(0, len(seen_mids), 500):
        part = seen_mids[i:i + 500]
        for r in conn.execute('SELECT * FROM candidate_reasons WHERE owner_id=? AND mandate_id IN (%s) ORDER BY id DESC'
                              % ','.join('?' * len(part)), [oid] + part):
            fam = _family_of(r['stage'])
            if r['reason_code'] in _codes('client_reject') and fam == 'client_reject':
                rej.append(r)
            elif r['reason_code'] in _codes('candidate_no') and fam in (None, 'candidate_no'):
                no.append(r)
    out = {'is_client': True, 'jobs': len(seen_mids),
           'client_rejections': _summarise(rej, client, with_feedback=True),
           'candidate_no_to_client': _summarise(no, client, with_feedback=False)}
    brand = {'client_brand', 'ctc_gap', 'role_scope'}
    out['candidate_no_to_client']['brand_signals'] = [t for t in out['candidate_no_to_client']['top'] if t['code'] in brand]
    return out


def _quality(conn, oid, current, records, stages_now):
    rows = list(current.values())
    n = len(rows)
    with_exp = sum(1 for r in rows if float(r['ctc_expected'] or 0) > 0)
    with_fam = sum(1 for r in rows if _family(r) != NOT_TAGGED)
    # negative outcomes (any "no" family) on any job, and how many carry a reason
    neg_ids = [i for i, st in stages_now.items() if _family_of(st)]
    with_reason = 0
    for i in range(0, len(neg_ids), 500):
        part = neg_ids[i:i + 500]
        for r in conn.execute('SELECT c.id, c.stage, (SELECT COUNT(*) FROM candidate_reasons x WHERE x.owner_id=? AND '
                              'x.candidate_id=c.id AND LOWER(x.stage)=LOWER(c.stage)) AS k FROM candidates c WHERE c.id IN (%s)'
                              % ','.join('?' * len(part)), [oid] + part):
            if r['k']:
                with_reason += 1
    return {'records': n, 'expected_ctc_pct': _pct(with_exp, n), 'role_family_pct': _pct(with_fam, n),
            'negatives': len(neg_ids), 'negatives_with_reason_pct': _pct(with_reason, len(neg_ids))}


def build(conn, oid, key, client=False):
    sc, sp = _scope()
    current, records, former = _people(conn, oid, key, sc, sp)
    cur_rows = list(current.values())
    all_ids = [i for ids in records.values() for i in ids]
    stages_now = {}
    for i in range(0, len(all_ids), 500):
        part = all_ids[i:i + 500]
        for r in conn.execute('SELECT id, stage FROM candidates WHERE id IN (%s)' % ','.join('?' * len(part)), part):
            stages_now[r['id']] = r['stage']
    name = ''
    if cur_rows:
        tally = {}
        for r in cur_rows:
            nm = (r['company'] or '').strip()
            tally[nm] = tally.get(nm, 0) + 1
        name = max(tally.items(), key=lambda x: x[1])[0]
    data = {
        'company': name, 'key': key, 'view': 'client' if client else 'internal', 'generated_at': ts(), 'min_n': MIN_N,
        'people': {'current': len(current), 'former': len(former),
                   'current_grid': _grid(cur_rows), 'former_grid': _grid(list(former.values()))},
        'pay': _pay(cur_rows),
        'notice': _notice(cur_rows),
        'openness': _openness(_stages_by_person(conn, records)),
        'reasons': _reasons_for(conn, oid, all_ids, client),
        'notes': _notes(conn, oid, key, sc, sp, client),
        'client': _client_branch(conn, oid, key, sc, sp, client),
        'quality': _quality(conn, oid, current, records, stages_now),
    }
    if not name and data['client'] is None and not data['notes']['n'] and not former:
        data['empty'] = True
    return data


# ══════════════════════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/api/talent-map/intel', methods=['GET'])
@login_required
def intel():
    key = company_key(request.args.get('name'))
    if not key:
        return jsonify({'error': 'Pick a company.'}), 400
    client = request.args.get('view') == 'client'
    oid = effective_company_id()
    conn = get_db()
    try:
        if not intel_on(conn, oid):
            return jsonify({'error': 'Company intelligence is switched off for your agency.', 'off': True}), 404
        ck = (oid, key, _scope_sig(), client)
        data = None if request.args.get('refresh') == '1' else _cached(ck)
        cached = data is not None
        if data is None:
            data = build(conn, oid, key, client)
            _store(ck, data)
    finally:
        conn.close()
    out = dict(data)
    if not out.get('company'):
        out['company'] = (request.args.get('name') or '').strip()
    out['cached'] = cached
    return jsonify(dict(out, ok=True))


@bp.route('/api/talent-map/intel/config', methods=['GET'])
@login_required
def intel_config():
    conn = get_db()
    try:
        on = intel_on(conn, effective_company_id())
    finally:
        conn.close()
    return jsonify({'ok': True, 'on': on, 'is_admin': bool(is_company_admin())})


@bp.route('/api/talent-map/intel/config', methods=['POST'])
@login_required
def set_intel_config():
    if not is_company_admin():
        return jsonify({'error': 'Only an admin can change this.'}), 403
    d = request.get_json(silent=True) or {}
    oid = effective_company_id()
    conn = get_db()
    try:
        conn.execute('INSERT OR REPLACE INTO insight_settings (owner_id, key, value, updated_at) VALUES (?,?,?,?)',
                     (oid, 'insight_company_on', '1' if d.get('on') else '0', ts()))
        conn.commit()
        on = intel_on(conn, oid)
    finally:
        conn.close()
    invalidate(oid)
    return jsonify({'ok': True, 'on': on})
