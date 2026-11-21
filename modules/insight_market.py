"""
RecruitOS — Insight Engine, Wave 3: Market Intel + Talent Pulse  (Nov 2026)

The cross-company view: what the market pays, who is open to a move, why
people say no and what is happening at companies — and a monthly "Talent
Pulse" built from those numbers.

  GET  /market-intel                                   the page (market-intel.html)
  GET  /api/market-intel/data?period=3|6|12&domain=&companies=all|clients|others&refresh=1
  GET  /api/market-intel/grid.csv?<same filters>       the salary grid as CSV
  GET  /api/market-intel/pulse?<same filters>          4–5 data-backed points (each with its n)
  POST /api/market-intel/pulse/draft {<filters>}       a NEW Market Pulse newsletter DRAFT with
                                                       those points (agency admin; nothing is sent)
  GET  /api/market-intel/config  ·  POST (admin) {on}  the insight_market_on switch (default on)

The period ("last 3 / 6 / 12 months") counts a person only when there was REAL
activity on any of their records in that window: added, a stage move, a reason,
a call analysis, a market note or a recorded field edit (audit log). A record
that was only touched by a background job does not count. The previous period
of the same length is used for every "vs last period" figure.

Salary grid: role family x level band x city. Median current CTC, median
expected CTC, median hike ask % (both CTCs > 0), n, and the companies the
sample comes from. A cell shows numbers only for n >= 3.

Owner's decisions (Nov 2026): the client-facing Talent Pulse page MAY name
companies; recruiters MAY open Market Intel (numbers limited to their own
jobs, like everywhere else); only admins create the newsletter draft.
Nothing here ever outputs a candidate's name, id, phone or individual CTC.

Needs Wave 1 (insight_capture) and Wave 2 (insight_company): it reuses their
catalogue and maths so the numbers agree across pages. Read-only: no new
tables or columns.

ROLLBACK: remove 'insight_market' from modules/__init__.py (before removing
Wave 2 / Wave 1), or switch "Market intelligence" off in Settings.
"""

import csv
import io
import json
import os
import re
import time
import datetime
import threading
import statistics

from flask import Blueprint, request, jsonify, Response, redirect

from modules.shared import get_db, ts, effective_company_id, real_user_id, is_company_admin, login_required, _core
from modules import register_migration

bp = Blueprint('insight_market', __name__)
pages = Blueprint('insight_market_pages', __name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_SECONDS = 600
MIN_N = 3
PERIODS = (3, 6, 12)

# City clean-up: the first city named in the location wins.
CITIES = [
    ('Delhi NCR', r'new delhi|delhi|noida|gurgaon|gurugram|ghaziabad|faridabad|\bncr\b|manesar'),
    ('Mumbai', r'navi mumbai|mumbai|bombay|thane|vashi|panvel|kalyan|bhiwandi'),
    ('Pune', r'pune|pimpri|chinchwad|chakan|hinjewadi|talegaon'),
    ('Bengaluru', r'bangalore|bengaluru|bengalore|banglore|whitefield|electronic city'),
    ('Chennai', r'chennai|madras|sriperumbudur|oragadam|ambattur'),
    ('Hyderabad', r'hyderabad|secunderabad|gachibowli|hitech city'),
    ('Kolkata', r'kolkata|calcutta|howrah|salt lake'),
    ('Ahmedabad', r'ahmedabad|ahmadabad|gandhinagar|sanand'),
    ('Vadodara', r'vadodara|baroda'),
    ('Surat', r'\bsurat\b'),
    ('Jaipur', r'jaipur'),
    ('Lucknow', r'lucknow'),
    ('Chandigarh', r'chandigarh|mohali|panchkula|zirakpur'),
    ('Indore', r'indore|pithampur'),
    ('Nagpur', r'nagpur'),
    ('Nashik', r'nashik|nasik'),
    ('Coimbatore', r'coimbatore'),
    ('Kochi', r'kochi|cochin|ernakulam'),
    ('Bhubaneswar', r'bhubaneswar|cuttack'),
    ('Hosur', r'hosur'),
    ('Visakhapatnam', r'visakhapatnam|vizag'),
    ('Raipur', r'raipur'),
    ('Bhopal', r'bhopal'),
    ('Guwahati', r'guwahati'),
]
_CITY_RX = [(c, re.compile(p)) for c, p in CITIES]
NOT_KNOWN = 'Not known'


@register_migration
def migrate(conn):
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
def _ic():
    from modules import insight_capture
    return insight_capture


def _co():
    from modules import insight_company
    return insight_company


def company_key(name):
    from modules.talent_map import company_key as _ck
    return _ck(name)


def normalise_city(location):
    """'Bangalore, Karnataka' -> 'Bengaluru'; unknown city -> 'Other (South)'."""
    txt = ' '.join(str(location or '').lower().split())
    if not txt:
        return NOT_KNOWN
    best = None
    for city, rx in _CITY_RX:
        m = rx.search(txt)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), city)
    if best:
        return best[1]
    try:
        from modules.segments import region_of, REGION_LABELS
        k = region_of(txt)
        if k and k != 'unknown':
            return 'Other (%s)' % REGION_LABELS.get(k, k)
    except Exception:
        pass
    return 'Other'


def market_on(conn, oid):
    r = conn.execute("SELECT value FROM insight_settings WHERE owner_id=? AND key='insight_market_on'", (oid,)).fetchone()
    return True if r is None else str(r['value'] or '').strip().lower() not in ('0', 'false', 'off', 'no', '')


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


def _now():
    try:
        return _core()._ist_now()
    except Exception:
        return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)


def windows(months, now=None):
    """(start, end), (prev_start, prev_end) as ts() strings; months of 30 days."""
    now = now or _now()
    d = datetime.timedelta(days=30 * months)
    f = lambda x: x.strftime('%Y-%m-%dT%H:%M:%S')
    return (f(now - d), f(now + datetime.timedelta(seconds=1))), (f(now - 2 * d), f(now - d))


def _filters(args):
    try:
        months = int(args.get('period') or 6)
    except Exception:
        months = 6
    if months not in PERIODS:
        months = 6
    comp = str(args.get('companies') or 'all').lower()
    if comp not in ('all', 'clients', 'others'):
        comp = 'all'
    dom = str(args.get('domain') or '').strip()
    if dom and dom not in _ic().DOMAINS:
        dom = ''
    return {'period': months, 'companies': comp, 'domain': dom}


def _med(xs):
    return round(statistics.median(xs), 2) if xs else None


def _pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def _jl(v):
    try:
        x = json.loads(v or '[]')
        return [str(i) for i in x if str(i).strip()] if isinstance(x, list) else []
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════
#  Cache
# ══════════════════════════════════════════════════════════════════════════
_CACHE, _LOCK = {}, threading.Lock()


def invalidate(oid):
    with _LOCK:
        for k in [k for k in _CACHE if k[0] == oid]:
            _CACHE.pop(k, None)


def _cached(ck):
    with _LOCK:
        hit = _CACHE.get(ck)
        return hit[1] if hit and time.time() - hit[0] < CACHE_SECONDS else None


def _store(ck, data):
    with _LOCK:
        if len(_CACHE) > 300:
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:150]:
                _CACHE.pop(k, None)
        _CACHE[ck] = (time.time(), data)


# ══════════════════════════════════════════════════════════════════════════
#  Data
# ══════════════════════════════════════════════════════════════════════════
def _client_keys(conn, oid):
    keys = set()
    try:
        keys |= {company_key(r['name']) for r in conn.execute('SELECT name FROM crm_clients WHERE company_id=?', (oid,))}
    except Exception:
        pass
    keys |= {company_key(r['client']) for r in conn.execute('SELECT client FROM mandates WHERE owner_id=?', (oid,))}
    keys.discard('')
    return keys


def _records(conn, oid):
    """Every candidate record in scope: {id: row}, and {pk: newest row}."""
    sc, sp = _scope()
    rows = conn.execute(
        'SELECT c.id, c.company, c.location, c.ctc_current, c.ctc_expected, c.role_family, c.function_tags, '
        'c.level_band, c.domain_tags, c.stage, c.created_at, c.updated_at, COALESCE(NULLIF(c.copied_from,0), c.id) AS pk '
        'FROM candidates c WHERE c.owner_id=?' + sc + ' ORDER BY c.updated_at DESC, c.id DESC', [oid] + sp).fetchall()
    by_id, newest = {}, {}
    for r in rows:
        by_id[r['id']] = r
        if r['pk'] not in newest:
            newest[r['pk']] = r
    return by_id, newest


def _activity(conn, oid, by_id, start, end):
    """{pk: set(stages reached in the window)} for everyone with real activity in it."""
    act = {}

    def hit(cid, stage=None):
        r = by_id.get(cid)
        if r is None:
            return
        s = act.setdefault(r['pk'], set())
        if stage:
            s.add(' '.join(str(stage).lower().split()))
    for r in by_id.values():
        if start <= (r['created_at'] or '') < end:
            hit(r['id'])
    for r in conn.execute('SELECT sh.candidate_id, sh.to_stage FROM stage_history sh JOIN candidates c ON c.id=sh.candidate_id '
                          'WHERE c.owner_id=? AND sh.created_at>=? AND sh.created_at<?', (oid, start, end)):
        hit(r['candidate_id'], r['to_stage'])
    for sql in ('SELECT candidate_id FROM candidate_reasons WHERE owner_id=? AND created_at>=? AND created_at<?',
                'SELECT candidate_id FROM market_notes WHERE owner_id=? AND candidate_id>0 AND created_at>=? AND created_at<?',
                'SELECT candidate_id FROM candidate_call_analysis WHERE owner_id=? AND created_at>=? AND created_at<?',
                "SELECT entity_id FROM audit_log WHERE company_id=? AND entity_type='candidate' AND created_at>=? AND created_at<?"):
        try:
            for r in conn.execute(sql, (oid, start, end)):
                hit(r[0])
        except Exception:
            pass                                  # a table missing on an old database
    return act


def _person(r, client_keys):
    co = _co()
    fam = co._family(r)
    return {'pk': r['pk'], 'company': (r['company'] or '').strip(), 'ckey': company_key(r['company']),
            'city': normalise_city(r['location']), 'family': fam,
            'band': (r['level_band'] or '').strip() or co.NOT_TAGGED,
            'domains': [d.lower() for d in _jl(r['domain_tags'])],
            'ctc': float(r['ctc_current'] or 0), 'ectc': float(r['ctc_expected'] or 0),
            'client': company_key(r['company']) in client_keys}


def _keep(p, f):
    if f['domain'] and f['domain'].lower() not in p['domains']:
        return False
    if f['companies'] == 'clients' and not p['client']:
        return False
    if f['companies'] == 'others' and p['client']:
        return False
    return True


def _grid(people):
    groups = {}
    for p in people:
        groups.setdefault((p['family'], p['band'], p['city']), []).append(p)
    out = []
    for (fam, band, city), ps in groups.items():
        cur = [p['ctc'] for p in ps if p['ctc'] > 0]
        exp = [p['ectc'] for p in ps if p['ectc'] > 0]
        hike = [100.0 * (p['ectc'] - p['ctc']) / p['ctc'] for p in ps if p['ctc'] > 0 and p['ectc'] > 0]
        both = [p for p in ps if p['ctc'] > 0 and p['ectc'] > 0]      # the same people on both sides
        n = len(cur)
        cell = {'family': fam, 'band': band, 'city': city, 'n': n}
        if n >= MIN_N:
            comps, shown = {}, {}
            for p in ps:
                if p['ctc'] > 0 and p['ckey']:
                    comps[p['ckey']] = comps.get(p['ckey'], 0) + 1
                    shown.setdefault(p['ckey'], p['company'])
            comps = {shown[k]: v for k, v in comps.items()}
            cell.update({'median': _med(cur), 'median_expected': _med(exp) if len(exp) >= MIN_N else None,
                         'n_expected': len(exp),
                         'median_hike_pct': round(statistics.median(hike), 1) if len(hike) >= MIN_N else None,
                         'n_hike': len(hike),
                         'paired_current': _med([p['ctc'] for p in both]) if len(both) >= MIN_N else None,
                         'paired_expected': _med([p['ectc'] for p in both]) if len(both) >= MIN_N else None,
                         'companies': [{'name': k, 'n': v} for k, v in sorted(comps.items(), key=lambda x: (-x[1], x[0]))][:8]})
        else:
            cell['small'] = True
        if n:
            out.append(cell)
    out.sort(key=lambda c: (-c['n'], c['family'], c['band'], c['city']))
    return out


def _openness_groups(people_stages, key):
    co = _co()
    groups = {}
    for p, stages in people_stages:
        if not (stages & co.CONTACTED):
            continue
        g = groups.setdefault(key(p), {'contacted': 0, 'pos': 0, 'neg': 0})
        g['contacted'] += 1
        if stages & co.POSITIVE:
            g['pos'] += 1
        elif stages & co.NEGATIVE:
            g['neg'] += 1
    return groups


def _movement(cur_ps, prev_ps):
    """Per company (+ city x role family segments): openness now vs the previous period."""
    out = []
    cur_c = _openness_groups(cur_ps, lambda p: p['ckey'])
    prev_c = _openness_groups(prev_ps, lambda p: p['ckey'])
    names = {}
    for p, _s in cur_ps + prev_ps:
        names.setdefault(p['ckey'], {}).setdefault(p['company'], 0)
        names[p['ckey']][p['company']] += 1
    seg_now = _openness_groups(cur_ps, lambda p: (p['ckey'], p['city'], p['family']))
    seg_prev = _openness_groups(prev_ps, lambda p: (p['ckey'], p['city'], p['family']))
    for k, g in cur_c.items():
        if not k:
            continue
        pv = prev_c.get(k, {'contacted': 0, 'pos': 0, 'neg': 0})
        row = {'company': max(names[k].items(), key=lambda x: x[1])[0], 'key': k, 'client': False,
               'contacted': g['contacted'], 'interested_or_later': g['pos'], 'said_no': g['neg'],
               'openness_pct': _pct(g['pos'], g['contacted']), 'no_pct': _pct(g['neg'], g['contacted']),
               'small': g['contacted'] < MIN_N,
               'prev': {'contacted': pv['contacted'], 'said_no': pv['neg'], 'interested_or_later': pv['pos'],
                        'no_pct': _pct(pv['neg'], pv['contacted']), 'openness_pct': _pct(pv['pos'], pv['contacted'])}}
        if not row['small'] and pv['contacted'] >= MIN_N:
            row['no_change_pts'] = round(row['no_pct'] - row['prev']['no_pct'], 1)
        row['segments'] = []
        for (ck, city, fam), sg in seg_now.items():
            if ck == k and sg['contacted'] >= MIN_N:
                sp = seg_prev.get((ck, city, fam), {'contacted': 0, 'neg': 0, 'pos': 0})
                row['segments'].append({'city': city, 'family': fam, 'contacted': sg['contacted'], 'said_no': sg['neg'],
                                        'interested_or_later': sg['pos'],
                                        'prev_contacted': sp['contacted'], 'prev_said_no': sp['neg']})
        row['segments'].sort(key=lambda s: -s['contacted'])
        out.append(row)
    out.sort(key=lambda r: (-r['contacted'], r['company']))
    return out


def _reasons(conn, oid, by_id, f, persons, start, end):
    ic = _ic()
    codes = set(ic.CODES['candidate_no'])
    latest = {}
    for r in conn.execute('SELECT candidate_id, stage, reason_code FROM candidate_reasons WHERE owner_id=? AND created_at>=? '
                          'AND created_at<? ORDER BY id DESC', (oid, start, end)):
        c = by_id.get(r['candidate_id'])
        if c is None or r['reason_code'] not in codes or ic.family_for(r['stage']) not in (None, 'candidate_no'):
            continue
        if c['pk'] in persons and c['pk'] not in latest:
            latest[c['pk']] = r['reason_code']
    counts = {}
    for code in latest.values():
        counts[code] = counts.get(code, 0) + 1
    return {'n': len(latest), 'top': [{'code': c, 'label': ic.reason_label(c, 'candidate_no'), 'n': n}
                                     for c, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]}


def _signals(conn, oid, start, end, client_keys, f):
    sc, sp = _scope()
    rows = conn.execute('SELECT n.signal_type, n.company_key, n.company_name FROM market_notes n LEFT JOIN candidates c '
                        'ON c.id=n.candidate_id WHERE n.owner_id=? AND n.created_at>=? AND n.created_at<? '
                        'AND (COALESCE(n.candidate_id,0)=0 OR (c.owner_id=?' + sc + '))',
                        [oid, start, end, oid] + sp).fetchall()
    by = {}
    for r in rows:
        if f['companies'] == 'clients' and r['company_key'] not in client_keys:
            continue
        if f['companies'] == 'others' and r['company_key'] in client_keys:
            continue
        s = by.setdefault(r['signal_type'], {'n': 0, 'companies': {}})
        s['n'] += 1
        s['companies'][r['company_name']] = s['companies'].get(r['company_name'], 0) + 1
    ic = _ic()
    labels = dict(ic.SIGNALS)
    return [{'signal_type': k, 'label': labels.get(k, k), 'n': v['n'],
             'companies': [x for x, _ in sorted(v['companies'].items(), key=lambda x: (-x[1], x[0]))][:6]}
            for k, v in sorted(by.items(), key=lambda x: -x[1]['n'])]


def build(conn, oid, f):
    (s, e), (ps, pe) = windows(f['period'])
    by_id, newest = _records(conn, oid)
    ck = _client_keys(conn, oid)
    act_now, act_prev = _activity(conn, oid, by_id, s, e), _activity(conn, oid, by_id, ps, pe)
    people = {pk: _person(r, ck) for pk, r in newest.items()}
    cur = [people[pk] for pk in act_now if pk in people and _keep(people[pk], f)]
    prev = [people[pk] for pk in act_prev if pk in people and _keep(people[pk], f)]
    cur_ps = [(p, act_now[p['pk']]) for p in cur]
    prev_ps = [(p, act_prev[p['pk']]) for p in prev]
    move = _movement(cur_ps, prev_ps)
    for m in move:
        m['client'] = m['key'] in ck
    tot = _openness_groups(cur_ps, lambda p: 'all').get('all', {'contacted': 0, 'pos': 0, 'neg': 0})
    totp = _openness_groups(prev_ps, lambda p: 'all').get('all', {'contacted': 0, 'pos': 0, 'neg': 0})
    data = {
        'filters': f, 'window': {'from': s[:10], 'to': e[:10], 'prev_from': ps[:10], 'prev_to': pe[:10]},
        'generated_at': ts(), 'min_n': MIN_N,
        'people': len(cur), 'companies': len({p['ckey'] for p in cur if p['ckey']}),
        'grid': _grid(cur), 'grid_prev': _grid(prev),
        'movement': move,
        'market': {'contacted': tot['contacted'], 'interested_or_later': tot['pos'], 'said_no': tot['neg'],
                   'openness_pct': _pct(tot['pos'], tot['contacted']), 'no_pct': _pct(tot['neg'], tot['contacted']),
                   'prev': {'contacted': totp['contacted'], 'openness_pct': _pct(totp['pos'], totp['contacted']),
                            'no_pct': _pct(totp['neg'], totp['contacted'])},
                   'companies': len({p['ckey'] for p, st in cur_ps if st & _co().CONTACTED and p['ckey']})},
        'reasons': _reasons(conn, oid, by_id, f, {p['pk'] for p in cur}, s, e),
        'reasons_prev': _reasons(conn, oid, by_id, f, {p['pk'] for p in prev}, ps, pe),
        'signals': _signals(conn, oid, s, e, ck, f),
        'domains': _ic().DOMAINS,
    }
    return data


def get_data(conn, oid, f, refresh=False):
    key = (oid, _scope_sig(), f['period'], f['companies'], f['domain'])
    data = None if refresh else _cached(key)
    cached = data is not None
    if data is None:
        data = build(conn, oid, f)
        _store(key, data)
    return dict(data, cached=cached)


# ══════════════════════════════════════════════════════════════════════════
#  Talent Pulse: 4–5 points, written from the numbers (no AI here)
# ══════════════════════════════════════════════════════════════════════════
def _l(v):
    return ('%.1f' % v).rstrip('0').rstrip('.')


def pulse_points(d):
    pts = []
    months = d['filters']['period']
    prev_cells = {(c['family'], c['band'], c['city']): c for c in d['grid_prev'] if not c.get('small')}
    # 1–2: pay, the two biggest cells with a hike figure
    pay = [c for c in d['grid'] if not c.get('small') and c['median_hike_pct'] is not None
           and c['family'] != 'Not tagged' and c['city'] not in (NOT_KNOWN, 'Other')]
    for c in pay[:2]:
        # current and expected of the SAME people (both known), so the two figures and the hike agree
        t = ('%s (%s) in %s: people earning a median ₹%s L are asking for a median ₹%s L, a median hike ask of %s%%'
             % (c['family'], c['band'], c['city'], _l(c['paired_current']), _l(c['paired_expected']),
                _l(c['median_hike_pct'])))
        p = prev_cells.get((c['family'], c['band'], c['city']))
        if p and p.get('median_hike_pct') is not None:
            t += ' (previous %d months: %s%%, n = %d)' % (months, _l(p['median_hike_pct']), p['n_hike'])
        pts.append({'kind': 'pay', 'n': c['n_hike'], 'text': t + ' (n = %d).' % c['n_hike']})
    # 3: market openness
    m = d['market']
    if m['contacted'] >= MIN_N:
        t = ('Openness: %d of %d people we contacted across %d companies were open to a move (%s%%)'
             % (m['interested_or_later'], m['contacted'], m['companies'], _l(m['openness_pct'])))
        if m['prev']['contacted'] >= MIN_N:
            t += ', vs %s%% in the previous %d months (n = %d)' % (_l(m['prev']['openness_pct']), months, m['prev']['contacted'])
        pts.append({'kind': 'openness', 'n': m['contacted'], 'text': t + ' (n = %d).' % m['contacted']})
    # 4: the biggest openness shift at one company
    shifts = [r for r in d['movement'] if 'no_change_pts' in r]
    if shifts:
        r = max(shifts, key=lambda r: abs(r['no_change_pts']))
        pts.append({'kind': 'shift', 'n': r['contacted'],
                    'text': '%s: %d of %d people we contacted said no (%s%%), vs %d of %d (%s%%) in the previous %d months.'
                            % (r['company'], r['said_no'], r['contacted'], _l(r['no_pct']), r['prev']['said_no'],
                               r['prev']['contacted'], _l(r['prev']['no_pct']), months)})
    else:
        big = [r for r in d['movement'] if not r['small']]
        if big:
            r = big[0]
            pts.append({'kind': 'company', 'n': r['contacted'],
                        'text': '%s: %d of %d people we contacted said no (%s%%).'
                                % (r['company'], r['said_no'], r['contacted'], _l(r['no_pct']))})
    # 5: why people say no
    rs = d['reasons']
    if rs['n'] >= MIN_N and rs['top']:
        t = 'Top reason for saying no: %s (%d of %d reasons)' % (rs['top'][0]['label'], rs['top'][0]['n'], rs['n'])
        if len(rs['top']) > 1:
            t += ', then %s (%d)' % (rs['top'][1]['label'], rs['top'][1]['n'])
        pts.append({'kind': 'reasons', 'n': rs['n'], 'text': t + ' (n = %d).' % rs['n']})
    # 6: the most active market signal
    sg = [s for s in d['signals'] if s['n'] >= MIN_N]
    if sg and len(pts) < 5:
        s = sg[0]
        pts.append({'kind': 'signal', 'n': s['n'],
                    'text': '%s: %d market notes about %s (n = %d).'
                            % (s['label'], s['n'], ', '.join(s['companies'][:4]), s['n'])})
    return pts[:5]


# ══════════════════════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════════════════════
def _guard(conn):
    if not market_on(conn, effective_company_id()):
        return jsonify({'error': 'Market intelligence is switched off for your agency.', 'off': True}), 404
    return None


@pages.route('/market-intel')
def market_intel_page():
    if not _core().current_user():
        return redirect('/')
    with open(os.path.join(ROOT, 'market-intel.html'), encoding='utf-8') as fh:
        return Response(fh.read(), mimetype='text/html')


@bp.route('/api/market-intel/data', methods=['GET'])
@login_required
def data():
    f = _filters(request.args)
    conn = get_db()
    try:
        g = _guard(conn)
        if g:
            return g
        d = get_data(conn, effective_company_id(), f, request.args.get('refresh') == '1')
    finally:
        conn.close()
    return jsonify(dict(d, ok=True, can_draft=bool(is_company_admin())))


@bp.route('/api/market-intel/grid.csv', methods=['GET'])
@login_required
def grid_csv():
    f = _filters(request.args)
    conn = get_db()
    try:
        g = _guard(conn)
        if g:
            return g
        d = get_data(conn, effective_company_id(), f)
    finally:
        conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Role family', 'Level', 'City', 'n', 'Median current CTC (L)', 'Median expected CTC (L)', 'n expected',
                'Median hike ask %', 'n hike', 'Companies'])
    for c in d['grid']:
        if c.get('small'):
            w.writerow([c['family'], c['band'], c['city'], c['n'], 'n too small', '', '', '', '', ''])
        else:
            w.writerow([c['family'], c['band'], c['city'], c['n'], c['median'],
                        '' if c['median_expected'] is None else c['median_expected'], c['n_expected'],
                        '' if c['median_hike_pct'] is None else c['median_hike_pct'], c['n_hike'],
                        '; '.join('%s (%d)' % (x['name'], x['n']) for x in c['companies'])])
    name = 'salary-benchmark-%sm%s.csv' % (f['period'], ('-' + re.sub(r'[^a-z0-9]+', '-', f['domain'].lower())) if f['domain'] else '')
    return Response('﻿' + buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename="%s"' % name})


@bp.route('/api/market-intel/pulse', methods=['GET'])
@login_required
def pulse():
    f = _filters(request.args)
    conn = get_db()
    try:
        g = _guard(conn)
        if g:
            return g
        d = get_data(conn, effective_company_id(), f)
    finally:
        conn.close()
    return jsonify({'ok': True, 'points': pulse_points(d), 'filters': f, 'window': d['window'],
                    'market': d['market'],
                    # client version: only real benchmark rows (n >= 3, role and city known)
                    'grid': [c for c in d['grid'] if not c.get('small') and c['family'] != 'Not tagged'
                             and c['band'] != 'Not tagged' and c['city'] not in (NOT_KNOWN, 'Other')][:20],
                    'can_draft': bool(is_company_admin())})


@bp.route('/api/market-intel/pulse/draft', methods=['POST'])
@login_required
def pulse_draft():
    if not is_company_admin():
        return jsonify({'error': 'Only an admin can create a newsletter draft.'}), 403
    f = _filters(request.get_json(silent=True) or {})
    oid = effective_company_id()
    conn = get_db()
    try:
        g = _guard(conn)
        if g:
            return g
        pts = pulse_points(get_data(conn, oid, f))
        if not pts:
            return jsonify({'error': 'Not enough data in this period for a Talent Pulse (every point needs at least '
                                     '%d people). Try a longer period.' % MIN_N}), 400
        from modules import newsletter
        month = _now().strftime('%B %Y')
        text = '\n'.join('- ' + p['text'] for p in pts)
        iid = newsletter.create_draft(conn, oid, 'Talent Pulse — %s' % month, text, int(real_user_id() or 0))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True, 'issue_id': iid, 'url': '/newsletter?i=%d' % iid, 'points': pts})


@bp.route('/api/market-intel/config', methods=['GET'])
@login_required
def config():
    conn = get_db()
    try:
        on = market_on(conn, effective_company_id())
    finally:
        conn.close()
    return jsonify({'ok': True, 'on': on, 'is_admin': bool(is_company_admin())})


@bp.route('/api/market-intel/config', methods=['POST'])
@login_required
def set_config():
    if not is_company_admin():
        return jsonify({'error': 'Only an admin can change this.'}), 403
    d = request.get_json(silent=True) or {}
    oid = effective_company_id()
    conn = get_db()
    try:
        conn.execute('INSERT OR REPLACE INTO insight_settings (owner_id, key, value, updated_at) VALUES (?,?,?,?)',
                     (oid, 'insight_market_on', '1' if d.get('on') else '0', ts()))
        conn.commit()
        on = market_on(conn, oid)
    finally:
        conn.close()
    invalidate(oid)
    return jsonify({'ok': True, 'on': on})
