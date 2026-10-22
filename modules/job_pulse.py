"""
Job Pulse — what is happening on every mandate, at a glance (Jobs page).

For every job the caller can see (admin: whole company; recruiter: assigned jobs):

  status line   one plain sentence built from the data (no AI):
                "3 with client, oldest 6 days · Interview: Ravi Thu 11:00 · 2 overdue tasks"
  ball          whose move it is — US (source / call / share), CLIENT (feedback or
                interview result pending, N days), CANDIDATE (CV / documents / offer reply)
  next          the next interview, else the next task; overdue tasks counted
  moves7        last 7 days: +added · →client · →interview · →placed; "stalled N days"
  value         ₹ expected fee = fee% × CTC × chance, using the job's fee %, else the
                client's past placements, else the company default (8.33%)
  focus         ranking: ₹ expected × urgency (client waiting, interview soon, stalled)
  spoc          the client SPOC set on the job (CRM contact) — for the SPOC filter/summary
  team          who added / called candidates on this job this week; last touch
  peek          (separate call) recent events, tasks, interviews, pending feedback, interested
  chase         draft + send a feedback-chaser to the SPOC for profiles waiting N+ days
  digest        optional morning e-mail with the same picture (admins: all jobs;
                recruiters: their jobs)

Read-only except: per-job fee %, chase log, digest settings.
Additive only: new tables job_pulse_fee, job_pulse_chase, job_pulse_settings.
"""

import datetime
import html
import json
import os
import re
import sys
import threading
import time

from flask import Blueprint, request, jsonify

from modules.shared import get_db, ts, effective_company_id, real_user_id, is_company_admin, login_required, _core
from modules import register_migration

bp = Blueprint('job_pulse', __name__, url_prefix='/api/job-pulse')

IST = datetime.timedelta(hours=5, minutes=30)
DEFAULT_FEE = 8.33
CHASE_AFTER_DAYS = 4
# chance that one candidate at this stage ends in a placement
CHANCE = {'sourced': 0.02, 'engaged': 0.05, 'client': 0.15, 'interview': 0.35, 'offer': 0.70}
CANDIDATE_WAIT = ('updated cv awaited', 'cv awaited', 'documents', 'docs awaited', 'offer', 'offered')
DEFAULT_SETTINGS = {'digest_enabled': 0, 'digest_hour': 9, 'digest_recruiters': 1, 'default_fee': DEFAULT_FEE}


@register_migration
def _migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS job_pulse_fee (
        mandate_id INTEGER PRIMARY KEY,
        company_id INTEGER NOT NULL,
        fee_percent REAL DEFAULT 0,
        updated_by INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT ''
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS job_pulse_chase (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        mandate_id INTEGER NOT NULL,
        to_email TEXT DEFAULT '',
        subject TEXT DEFAULT '',
        candidate_ids TEXT DEFAULT '[]',
        sent_by INTEGER DEFAULT 0,
        sent_at TEXT DEFAULT ''
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS job_pulse_settings (
        company_id INTEGER PRIMARY KEY,
        digest_enabled INTEGER DEFAULT 0,
        digest_hour INTEGER DEFAULT 9,
        digest_recruiters INTEGER DEFAULT 1,
        default_fee REAL DEFAULT 8.33,
        last_digest_day TEXT DEFAULT '',
        updated_by INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT ''
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_jp_chase ON job_pulse_chase(company_id, mandate_id, sent_at)')
    conn.commit()


# ── helpers ────────────────────────────────────────────────────────────────
def now_ist():
    return datetime.datetime.utcnow() + IST


def _rows(conn, sql, params=()):
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def _err(msg, code=400):
    return jsonify({'ok': False, 'error': msg}), code


def _oid():
    return int(effective_company_id() or 0)


def _dt(s):
    s = str(s or '').strip().replace(' ', 'T')
    for fmt, n in (('%Y-%m-%dT%H:%M:%S', 19), ('%Y-%m-%dT%H:%M', 16), ('%Y-%m-%d', 10)):
        try:
            return datetime.datetime.strptime(s[:n], fmt)
        except ValueError:
            continue
    return None


def _days(s, now):
    d = _dt(s)
    return None if d is None else max(0, (now.date() - d.date()).days)


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _when(s, now):
    d = _dt(s)
    if not d:
        return ''
    if d.date() == now.date():
        day = 'Today'
    elif d.date() == (now + datetime.timedelta(days=1)).date():
        day = 'Tomorrow'
    elif 0 < (d.date() - now.date()).days < 7:
        day = d.strftime('%a')
    else:
        day = '%d %s' % (d.day, d.strftime('%b'))
    return day + ('' if len(str(s or '')) <= 10 else ' ' + d.strftime('%H:%M'))


def _inr(v):
    v = float(v or 0)
    if v >= 1e7:
        return '₹%.1fCr' % (v / 1e7)
    if v >= 1e5:
        return '₹%.1fL' % (v / 1e5)
    if v >= 1e3:
        return '₹%dK' % round(v / 1e3)
    return '₹%d' % round(v)


def get_settings(conn, oid):
    r = conn.execute('SELECT * FROM job_pulse_settings WHERE company_id=?', (oid,)).fetchone()
    s = dict(DEFAULT_SETTINGS, last_digest_day='')
    if r:
        for k in list(s):
            if k in r.keys() and r[k] is not None:
                s[k] = r[k]
    return s


def _bucket(stage):
    low = str(stage or '').strip().lower()
    if 'offer' in low and 'reject' not in low and 'declin' not in low:
        return 'offer'
    return _core().job_summary_bucket(stage)


def _scope_ids(conn, oid, uid, admin):
    if admin:
        return [r[0] for r in conn.execute("SELECT id FROM mandates WHERE owner_id=? AND COALESCE(status,'')!='central'", (oid,))]
    return [r[0] for r in conn.execute(
        "SELECT id FROM mandates WHERE owner_id=? AND COALESCE(status,'')!='central' AND id IN "
        "(SELECT mandate_id FROM mandate_assignees WHERE user_id=? AND is_active=1)", (oid, uid))]


def _int(v):
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _in(ids):
    return ','.join(str(int(i)) for i in ids) or '0'


# ══════════════════════════════════════════════════════════════════════════
#  The pulse of every job
# ══════════════════════════════════════════════════════════════════════════
def build(conn, oid, mids, now=None):
    now = now or now_ist()
    nowiso = now.strftime('%Y-%m-%dT%H:%M:%S')
    s = get_settings(conn, oid)
    if not mids:
        return {}, []
    ids = _in(mids)
    mandates = {m['id']: m for m in _rows(conn, 'SELECT * FROM mandates WHERE id IN (%s)' % ids)}
    ccols = {r[1] for r in conn.execute('PRAGMA table_info(candidates)')}
    want = [c for c in ('id', 'mandate_id', 'name', 'stage', 'created_at', 'updated_at', 'ctc_expected', 'offered_ctc',
                        'fee_percent', 'created_by', 'sourced_by') if c in ccols]
    cands = _rows(conn, 'SELECT %s FROM candidates WHERE mandate_id IN (%s)' % (','.join(want), ids))
    by_job = {}
    for c in cands:
        by_job.setdefault(c['mandate_id'], []).append(c)
    cids = [c['id'] for c in cands]
    # last stage change per candidate + changes in the last 7 days
    last_move, moves = {}, {}
    since7 = (now - datetime.timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%S')
    for i in range(0, len(cids), 800):
        chunk = _in(cids[i:i + 800])
        for r in _rows(conn, 'SELECT candidate_id, MAX(created_at) AS at FROM stage_history WHERE candidate_id IN (%s) '
                             'AND COALESCE(to_stage,\'\')<>COALESCE(from_stage,\'\') GROUP BY candidate_id' % chunk):
            last_move[r['candidate_id']] = r['at']
        for r in _rows(conn, 'SELECT candidate_id, to_stage, created_at FROM stage_history WHERE candidate_id IN (%s) '
                             'AND created_at>=? AND COALESCE(to_stage,\'\')<>COALESCE(from_stage,\'\')' % chunk, (since7,)):
            moves.setdefault(r['candidate_id'], []).append(r)
    users = {r['id']: (r['display_name'] or r['username']) for r in _rows(conn, 'SELECT id, username, display_name FROM users WHERE company_id=?', (oid,))}
    ivs = _rows(conn, "SELECT i.*, c.name AS cname FROM interviews i LEFT JOIN candidates c ON c.id=i.candidate_id "
                      "WHERE i.mandate_id IN (%s) AND COALESCE(i.status,'scheduled') NOT IN ('cancelled','canceled')" % ids)
    rcols = {r[1] for r in conn.execute('PRAGMA table_info(reminders)')}
    tasks = _rows(conn, 'SELECT id, mandate_id, candidate_name, note, due_at FROM reminders WHERE mandate_id IN (%s) AND done=0' % ids)
    calls = _rows(conn, 'SELECT mandate_id, created_by, created_by_name, created_at FROM candidate_call_analysis '
                        'WHERE mandate_id IN (%s) AND created_at>=?' % ids, (since7,))
    interested = {}
    for r in _rows(conn, "SELECT mandate_id, COUNT(*) AS n FROM jobmail_recipients WHERE mandate_id IN (%s) AND response='interested' "
                         "AND responded_at>=? GROUP BY mandate_id" % ids, ((now - datetime.timedelta(days=14)).strftime('%Y-%m-%dT%H:%M:%S'),)):
        interested[r['mandate_id']] = r['n']
    chases = {}
    for r in _rows(conn, 'SELECT mandate_id, MAX(sent_at) AS at FROM job_pulse_chase WHERE company_id=? AND mandate_id IN (%s) '
                         'GROUP BY mandate_id' % ids, (oid,)):
        chases[r['mandate_id']] = r['at']
    fees = {r['mandate_id']: r['fee_percent'] for r in _rows(conn, 'SELECT mandate_id, fee_percent FROM job_pulse_fee WHERE mandate_id IN (%s)' % ids)}
    client_fee = {}
    for r in _rows(conn, "SELECT m.client AS client, AVG(c.fee_percent) AS f FROM candidates c JOIN mandates m ON m.id=c.mandate_id "
                         "WHERE m.owner_id=? AND COALESCE(c.fee_percent,0)>0 AND (LOWER(TRIM(c.stage)) IN ('placed','joined') "
                         "OR COALESCE(c.placement_fee,0)>0) GROUP BY m.client", (oid,)):
        client_fee[(r['client'] or '').strip().lower()] = r['f']
    spoc_ids = [m.get('spoc_contact_id') for m in mandates.values() if m.get('spoc_contact_id')]
    spocs = {r['id']: r for r in _rows(conn, 'SELECT id, name, email, phone, designation FROM crm_contacts WHERE id IN (%s)' % _in(spoc_ids))} if spoc_ids else {}

    out = {}
    for mid, m in mandates.items():
        cs = by_job.get(mid, [])
        live = [c for c in cs if _bucket(c['stage']) not in ('dropped', 'placed')]
        counts = {}
        for c in cs:
            b = _bucket(c['stage'])
            counts[b] = counts.get(b, 0) + 1
        # pending with client (shared, no move since)
        pend = []
        for c in cs:
            if _bucket(c['stage']) == 'client':
                at = last_move.get(c['id']) or c.get('updated_at') or c.get('created_at')
                pend.append({'id': c['id'], 'name': c['name'] or '', 'days': _days(at, now) or 0})
        pend.sort(key=lambda x: -x['days'])
        # interviews
        upcoming = sorted([i for i in ivs if i['mandate_id'] == mid and str(i['scheduled_at'] or '') >= nowiso[:16]],
                          key=lambda i: str(i['scheduled_at']))
        awaiting_result = [i for i in ivs if i['mandate_id'] == mid and i['scheduled_at'] and str(i['scheduled_at']) < nowiso[:16]
                           and not str(i.get('result') or '').strip() and str(i.get('status') or '').lower() in ('', 'scheduled')]
        cand_wait = [c for c in live if any(w in str(c['stage'] or '').lower() for w in CANDIDATE_WAIT)]
        # tasks
        jt = [t for t in tasks if t['mandate_id'] == mid and t['due_at']]
        overdue = [t for t in jt if str(t['due_at']) < nowiso[:16]]
        nxt_task = sorted([t for t in jt if str(t['due_at']) >= nowiso[:16]], key=lambda t: str(t['due_at']))
        # movement
        mv = {'added': 0, 'client': 0, 'interview': 0, 'offer': 0, 'placed': 0}
        for c in cs:
            if str(c.get('created_at') or '') >= since7:
                mv['added'] += 1
            for r in moves.get(c['id'], []):
                b = _bucket(r['to_stage'])
                if b in mv:
                    mv[b] += 1
        acts = [str(c.get('created_at') or '') for c in cs] + [str(last_move.get(c['id']) or '') for c in cs]
        last_act = max(acts) if acts else str(m.get('created_at') or '')
        stalled = _days(last_act, now) if last_act else None
        # ₹
        fee_src, fee = 'default', float(s['default_fee'] or DEFAULT_FEE)
        if fees.get(mid):
            fee, fee_src = float(fees[mid]), 'job'
        elif client_fee.get((m.get('client') or '').strip().lower()):
            fee, fee_src = float(client_fee[(m.get('client') or '').strip().lower()]), 'client history'
        lo, hi = _num(m.get('ctc_min')), _num(m.get('ctc_max'))
        ctc = (lo + hi) / 2 if (lo and hi) else (lo or hi)
        if not ctc:
            exp = [_num(c.get('ctc_expected')) for c in live if _num(c.get('ctc_expected')) > 0]
            ctc = sum(exp) / len(exp) if exp else 0
        openings = max(1, int(_num(m.get('headcount')) or 1))
        per_fee = ctc * 100000 * fee / 100.0
        chance_sum, best = 0.0, 0.0
        for c in live:
            p = CHANCE.get(_bucket(c['stage']), 0.0)
            if _num(c.get('offered_ctc')) > 0:
                p = max(p, CHANCE['offer'])
            chance_sum += p
            best = max(best, p)
        exp_hires = min(openings, chance_sum)
        placed = counts.get('placed', 0)
        value = {'fee_percent': round(fee, 2), 'fee_source': fee_src, 'ctc_lpa': round(ctc, 1), 'openings': openings,
                 'full_fee': round(per_fee * openings), 'expected': round(per_fee * exp_hires),
                 'chance': round(min(1.0, chance_sum) * 100), 'placed': placed}
        value['expected_txt'], value['full_fee_txt'] = _inr(value['expected']), _inr(value['full_fee'])
        # ball
        client_days = max([p['days'] for p in pend] + [_days(i['scheduled_at'], now) or 0 for i in awaiting_result] + [0])
        if pend or awaiting_result:
            what = []
            if pend:
                what.append('feedback on %d profile%s' % (len(pend), '' if len(pend) == 1 else 's'))
            if awaiting_result:
                what.append('%d interview result%s' % (len(awaiting_result), '' if len(awaiting_result) == 1 else 's'))
            ball = {'who': 'client', 'days': client_days, 'label': 'Client · %s' % ' + '.join(what)}
        elif cand_wait:
            d = max(_days(last_move.get(c['id']) or c.get('updated_at'), now) or 0 for c in cand_wait)
            ball = {'who': 'candidate', 'days': d, 'label': 'Candidate · %d pending (%s)' % (len(cand_wait), cand_wait[0]['stage'])}
        else:
            engaged = counts.get('engaged', 0)
            why = ('%d to move forward' % engaged) if engaged else ('needs sourcing' if not live else '%d to call' % counts.get('sourced', 0))
            ball = {'who': 'us', 'days': stalled or 0, 'label': 'Us · ' + why}
        # next event
        nxt = None
        if upcoming:
            i = upcoming[0]
            nxt = {'kind': 'interview', 'at': i['scheduled_at'], 'text': 'Interview: %s · %s%s' % (
                i.get('cname') or 'candidate', _when(i['scheduled_at'], now), (' (%s)' % i['round_name']) if i.get('round_name') else '')}
        elif nxt_task:
            t = nxt_task[0]
            nxt = {'kind': 'task', 'at': t['due_at'], 'text': 'Task: %s · %s' % ((t['note'] or '')[:60], _when(t['due_at'], now))}
        # team
        team = {}
        for c in cs:
            if str(c.get('created_at') or '') >= since7:
                who = users.get(_int(c.get('sourced_by')) or _int(c.get('created_by')))
                if who:
                    team.setdefault(who, {'added': 0, 'calls': 0})['added'] += 1
        for r in calls:
            if r['mandate_id'] == mid:
                who = users.get(_int(r['created_by'])) or r.get('created_by_name') or ''
                if who:
                    team.setdefault(who, {'added': 0, 'calls': 0})['calls'] += 1
        # status line
        parts = []
        if not cs:
            parts.append('No candidates yet')
        chased = ''
        if chases.get(mid):
            chased = 'chased %s' % (_when(chases[mid], now) if _days(chases[mid], now) else 'today')
        if pend:
            parts.append('%d with client, oldest %d day%s%s' % (len(pend), pend[0]['days'], '' if pend[0]['days'] == 1 else 's',
                                                               (' (%s)' % chased) if chased else ''))
        if awaiting_result:
            parts.append('%d interview result%s awaited' % (len(awaiting_result), '' if len(awaiting_result) == 1 else 's'))
        if nxt and nxt['kind'] == 'interview':
            parts.append(nxt['text'])
        if counts.get('offer'):
            parts.append('%d at offer' % counts['offer'])
        if overdue:
            parts.append('%d overdue task%s' % (len(overdue), '' if len(overdue) == 1 else 's'))
        if interested.get(mid):
            parts.append('%d said Interested to job mail' % interested[mid])
        if cs and stalled is not None and stalled >= 7 and (m.get('status') or 'active') == 'active':
            parts.append('no movement for %d days' % stalled)
        elif mv['added']:
            parts.append('+%d added this week' % mv['added'])
        # focus score (active jobs only)
        iv_soon = False
        if upcoming:
            ivd = _dt(upcoming[0]['scheduled_at'])
            iv_soon = bool(ivd) and (ivd - now).total_seconds() <= 2 * 86400
        urg = 1.0
        if ball['who'] == 'client' and client_days >= 3:
            urg += 0.6
        if iv_soon:
            urg += 0.5
        if stalled is not None and stalled >= 7:
            urg += 0.4
        if overdue:
            urg += 0.3
        base = value['expected'] or (per_fee * 0.02 if per_fee else 0)
        focus = round(base * urg) if (m.get('status') or 'active') == 'active' else 0
        reasons = []
        if ball['who'] == 'client' and client_days >= 3:
            reasons.append('client waiting %d days' % client_days)
        if iv_soon:
            reasons.append('interview %s' % _when(upcoming[0]['scheduled_at'], now))
        if stalled is not None and stalled >= 7:
            reasons.append('stalled %d days' % stalled)
        if overdue:
            reasons.append('%d overdue task%s' % (len(overdue), '' if len(overdue) == 1 else 's'))
        sp = spocs.get(m.get('spoc_contact_id') or 0)
        out[mid] = {
            'id': mid, 'role': m.get('role') or '', 'client': m.get('client') or '', 'status': m.get('status') or 'active',
            'status_line': ' · '.join(parts[:4]) or 'Sourcing',
            'ball': ball, 'next': nxt, 'overdue': len(overdue), 'moves7': mv, 'stalled_days': stalled,
            'pending_feedback': pend[:20], 'chase_ready': [p for p in pend if p['days'] >= CHASE_AFTER_DAYS][:20],
            'last_chase': chases.get(mid) or '', 'value': value, 'focus': focus, 'focus_reasons': reasons,
            'interested': interested.get(mid, 0),
            'team': [{'name': k, 'added': v['added'], 'calls': v['calls']} for k, v in sorted(team.items(), key=lambda x: -(x[1]['added'] + x[1]['calls']))],
            'last_touch_days': stalled,
            'spoc': {'id': sp['id'], 'name': sp['name'] or '', 'email': sp['email'] or '', 'phone': sp['phone'] or '',
                     'designation': sp['designation'] or ''} if sp else None,
        }
    focus = [j['id'] for j in sorted(out.values(), key=lambda j: -j['focus']) if j['focus'] > 0][:5]
    return out, focus


def spoc_summary(jobs):
    g = {}
    for j in jobs.values():
        if j['status'] != 'active':
            continue
        sp = j['spoc']
        key = 'spoc:%d' % sp['id'] if sp else 'none:' + (j['client'] or '').lower()
        e = g.setdefault(key, {'key': key, 'name': sp['name'] if sp else 'No SPOC set', 'client': j['client'],
                               'email': sp['email'] if sp else '', 'jobs': [], 'with_client': 0, 'oldest_days': 0,
                               'interviews': 0, 'expected': 0, 'ball_client': 0})
        e['jobs'].append(j['id'])
        e['with_client'] += len(j['pending_feedback'])
        e['oldest_days'] = max([e['oldest_days']] + [p['days'] for p in j['pending_feedback']])
        e['interviews'] += 1 if (j['next'] and j['next']['kind'] == 'interview') else 0
        e['expected'] += j['value']['expected']
        e['ball_client'] += 1 if j['ball']['who'] == 'client' else 0
    return sorted(g.values(), key=lambda e: (-e['ball_client'], -e['oldest_days'], -len(e['jobs'])))


# ══════════════════════════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════════════════════════
def _can_see(conn, mid, write=False):
    try:
        from modules.access import can_see_mandate
        return can_see_mandate(conn, mid, write=write)
    except Exception:
        return True


@bp.route('', methods=['GET'])
@login_required
def pulse():
    oid = _oid()
    conn = get_db()
    try:
        mids = _scope_ids(conn, oid, int(real_user_id() or 0), bool(is_company_admin()))
        jobs, focus = build(conn, oid, mids)
        act = [j for j in jobs.values() if j['status'] == 'active']
        totals = {'expected': sum(j['value']['expected'] for j in act), 'full_fee': sum(j['value']['full_fee'] for j in act),
                  'ball_client': sum(1 for j in act if j['ball']['who'] == 'client'),
                  'ball_us': sum(1 for j in act if j['ball']['who'] == 'us'),
                  'ball_candidate': sum(1 for j in act if j['ball']['who'] == 'candidate'),
                  'stalled': sum(1 for j in act if (j['stalled_days'] or 0) >= 7),
                  'chase_ready': sum(1 for j in act if j['chase_ready'])}
        totals['expected_txt'], totals['full_fee_txt'] = _inr(totals['expected']), _inr(totals['full_fee'])
        s = get_settings(conn, oid)
        return jsonify({'ok': True, 'jobs': {str(k): v for k, v in jobs.items()}, 'focus': focus, 'totals': totals,
                        'spocs': spoc_summary(jobs), 'is_admin': bool(is_company_admin()),
                        'settings': {k: s[k] for k in DEFAULT_SETTINGS}})
    finally:
        conn.close()


@bp.route('/peek', methods=['GET'])
@login_required
def peek():
    oid = _oid()
    try:
        mid = int(request.args.get('mandate_id') or 0)
    except ValueError:
        mid = 0
    conn = get_db()
    try:
        m = conn.execute('SELECT id FROM mandates WHERE id=? AND owner_id=?', (mid, oid)).fetchone()
        if not m or not _can_see(conn, mid):
            return _err('Not found', 404)
        now = now_ist()
        jobs, _f = build(conn, oid, [mid], now)
        j = jobs.get(mid, {})
        events = _rows(conn, "SELECT h.candidate_id, c.name, h.from_stage, h.to_stage, h.note, h.created_at FROM stage_history h "
                             "JOIN candidates c ON c.id=h.candidate_id WHERE c.mandate_id=? ORDER BY h.created_at DESC, h.id DESC LIMIT 12", (mid,))
        for e in events:
            e['when'] = _when(e['created_at'], now) or ''
        tasks = _rows(conn, 'SELECT id, candidate_name, note, due_at FROM reminders WHERE mandate_id=? AND done=0 ORDER BY due_at LIMIT 10', (mid,))
        for t in tasks:
            t['when'] = _when(t['due_at'], now)
            t['overdue'] = bool(t['due_at']) and str(t['due_at']) < now.strftime('%Y-%m-%dT%H:%M')
        ivs = _rows(conn, "SELECT i.scheduled_at, i.round_name, i.mode, i.status, i.result, c.name FROM interviews i "
                          "LEFT JOIN candidates c ON c.id=i.candidate_id WHERE i.mandate_id=? ORDER BY i.scheduled_at DESC LIMIT 8", (mid,))
        for i in ivs:
            i['when'] = _when(i['scheduled_at'], now)
        inter = _rows(conn, "SELECT name, responded_at FROM jobmail_recipients WHERE mandate_id=? AND response='interested' "
                            "ORDER BY responded_at DESC LIMIT 8", (mid,))
        return jsonify({'ok': True, 'pulse': j, 'events': events, 'tasks': tasks, 'interviews': ivs, 'interested': inter})
    finally:
        conn.close()


@bp.route('/fee', methods=['POST'])
@login_required
def set_fee():
    d = request.get_json(silent=True) or {}
    oid = _oid()
    try:
        mid = int(d.get('mandate_id') or 0)
        fee = float(d.get('fee_percent') or 0)
    except (TypeError, ValueError):
        return _err('Fee must be a number')
    if fee < 0 or fee > 50:
        return _err('Fee % must be between 0 and 50 (0 = use the default)')
    conn = get_db()
    try:
        m = conn.execute('SELECT id FROM mandates WHERE id=? AND owner_id=?', (mid, oid)).fetchone()
        if not m or not _can_see(conn, mid, write=True):
            return _err('Not found', 404)
        if fee:
            conn.execute('INSERT INTO job_pulse_fee (mandate_id, company_id, fee_percent, updated_by, updated_at) VALUES (?,?,?,?,?) '
                         'ON CONFLICT(mandate_id) DO UPDATE SET fee_percent=excluded.fee_percent, updated_by=excluded.updated_by, '
                         'updated_at=excluded.updated_at', (mid, oid, fee, int(real_user_id() or 0), ts()))
        else:
            conn.execute('DELETE FROM job_pulse_fee WHERE mandate_id=?', (mid,))
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


def _chase_draft(conn, oid, mid):
    jobs, _f = build(conn, oid, [mid])
    j = jobs.get(mid)
    if not j:
        return None, 'Not found'
    if not j['pending_feedback']:
        return None, 'No profiles are waiting for client feedback on this job.'
    sp = j['spoc']
    who = (sp['name'].split()[0] if sp and sp['name'] else 'there')
    lst = j['chase_ready'] or j['pending_feedback']
    lines = '\n'.join('• %s — shared %d day%s ago' % (p['name'], p['days'], '' if p['days'] == 1 else 's') for p in lst)
    body = ('Hi %s,\n\nHope you are doing well. Following up on the profiles we shared for the %s position:\n\n%s\n\n'
            'Could you please share your feedback, or let us know the interview slots for the shortlisted ones? '
            'If anything about the requirement has changed, do tell us and we will align the search.\n\nThank you.'
            % (who, j['role'], lines))
    return {'to': sp['email'] if sp else '', 'spoc_name': sp['name'] if sp else '', 'subject': 'Feedback on profiles — %s' % j['role'],
            'body': body, 'candidates': lst, 'has_spoc': bool(sp and sp['email'])}, None


@bp.route('/chase', methods=['GET'])
@login_required
def chase_get():
    oid = _oid()
    try:
        mid = int(request.args.get('mandate_id') or 0)
    except ValueError:
        mid = 0
    conn = get_db()
    try:
        m = conn.execute('SELECT id FROM mandates WHERE id=? AND owner_id=?', (mid, oid)).fetchone()
        if not m or not _can_see(conn, mid):
            return _err('Not found', 404)
        dr, e = _chase_draft(conn, oid, mid)
        if e:
            return _err(e)
        return jsonify(dict(dr, ok=True))
    finally:
        conn.close()


@bp.route('/chase', methods=['POST'])
@login_required
def chase_send():
    core = _core()
    d = request.get_json(silent=True) or {}
    oid = _oid()
    try:
        mid = int(d.get('mandate_id') or 0)
    except (TypeError, ValueError):
        mid = 0
    to = str(d.get('to') or '').strip()
    subject = str(d.get('subject') or '').strip()[:200]
    body = str(d.get('body') or '').strip()[:6000]
    if not re.match(r'^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$', to):
        return _err('Enter the SPOC\'s e-mail address.')
    if not subject or not body:
        return _err('Subject and message are needed.')
    conn = get_db()
    try:
        m = conn.execute('SELECT id, role FROM mandates WHERE id=? AND owner_id=?', (mid, oid)).fetchone()
        if not m or not _can_see(conn, mid, write=True):
            return _err('Not found', 404)
        e = html.escape
        body_html = ''.join('<p>%s</p>' % e(p).replace('\n', '<br>') for p in re.split(r'\n\s*\n', body) if p.strip())
        ok, err, _mid, _tid = core.email_service_send(to, subject, body, body_html=body_html)
        if not ok:
            return _err('Could not send: %s' % (err or 'mail error'))
        cids = [int(x) for x in (d.get('candidate_ids') or []) if str(x).isdigit()][:50]
        conn.execute('INSERT INTO job_pulse_chase (company_id, mandate_id, to_email, subject, candidate_ids, sent_by, sent_at) '
                     'VALUES (?,?,?,?,?,?,?)', (oid, mid, to, subject, json.dumps(cids), int(real_user_id() or 0), ts()))
        try:
            conn.execute('INSERT INTO mandate_client_notes (mandate_id, owner_id, note, created_by, created_at, is_active) '
                         'VALUES (?,?,?,?,?,1)', (mid, oid, 'Feedback chaser sent to %s: %s' % (to, subject), int(real_user_id() or 0), ts()))
        except Exception:
            pass
        for cid in cids:
            try:
                conn.execute('INSERT INTO candidate_events (candidate_id,event_type,detail,created_at) VALUES (?,?,?,?)',
                             (cid, 'note', 'Client chased for feedback (%s)' % to, ts()))
            except Exception:
                pass
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


@bp.route('/settings', methods=['PUT', 'POST'])
@login_required
def save_settings():
    if not is_company_admin():
        return _err('Only an admin can change these settings.', 403)
    d = request.get_json(silent=True) or {}
    oid = _oid()
    conn = get_db()
    try:
        s = get_settings(conn, oid)
        try:
            fee = float(d.get('default_fee', s['default_fee']))
            hour = int(d.get('digest_hour', s['digest_hour']))
        except (TypeError, ValueError):
            return _err('Check the numbers.')
        if not (0 < fee <= 50) or not (5 <= hour <= 12):
            return _err('Default fee must be 0–50% and the digest time between 5 AM and 12 noon.')
        conn.execute('INSERT OR IGNORE INTO job_pulse_settings (company_id) VALUES (?)', (oid,))
        conn.execute('UPDATE job_pulse_settings SET digest_enabled=?, digest_hour=?, digest_recruiters=?, default_fee=?, '
                     'updated_by=?, updated_at=? WHERE company_id=?',
                     (1 if d.get('digest_enabled', s['digest_enabled']) in (1, True, '1', 'true') else 0, hour,
                      1 if d.get('digest_recruiters', s['digest_recruiters']) in (1, True, '1', 'true') else 0, fee,
                      int(real_user_id() or 0), ts(), oid))
        conn.commit()
        return jsonify({'ok': True, 'settings': {k: get_settings(conn, oid)[k] for k in DEFAULT_SETTINGS}})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  Morning digest
# ══════════════════════════════════════════════════════════════════════════
BALL_ICON = {'client': '🟠', 'candidate': '🟢', 'us': '🔵'}


def digest_for(conn, oid, user, now=None):
    """(subject, plain, html) for one user, or None if they have no active jobs."""
    now = now or now_ist()
    admin = (user.get('role') == 'admin') or bool(user.get('is_company_admin'))
    mids = _scope_ids(conn, oid, user['id'], admin)
    jobs, focus = build(conn, oid, mids, now)
    act = [j for j in jobs.values() if j['status'] == 'active']
    if not act:
        return None
    e = html.escape
    exp = sum(j['value']['expected'] for j in act)
    subject = 'Job Pulse · %s · %d active jobs · %s likely' % (now.strftime('%a %d %b'), len(act), _inr(exp))
    fl = [jobs[i] for i in focus]
    rows = sorted(act, key=lambda j: (-j['focus'], j['client'], j['role']))
    plain = ['Good morning! Here is where every job stands.', '', 'FOCUS TODAY']
    for j in fl:
        plain.append('- %s (%s): %s%s' % (j['role'], j['client'], j['status_line'],
                                          (' — ' + ', '.join(j['focus_reasons'])) if j['focus_reasons'] else ''))
    plain += ['', 'ALL ACTIVE JOBS']
    for j in rows:
        plain.append('- %s (%s) [%s] %s · %s' % (j['role'], j['client'], j['ball']['label'], j['status_line'], j['value']['expected_txt'] if 'expected_txt' in j['value'] else _inr(j['value']['expected'])))
    th = 'style="text-align:left;font-size:11px;color:#667085;padding:6px 8px;border-bottom:1px solid #E4E7EC"'
    td = 'style="padding:7px 8px;border-bottom:1px solid #F0F1F4;font-size:13px;vertical-align:top"'
    body = ['<div style="font-family:Arial,sans-serif;max-width:760px">',
            '<h2 style="color:#0E2A47;margin:0 0 4px">Job Pulse</h2><div style="color:#667085;font-size:13px;margin-bottom:14px">%s · %d active jobs · %s likely fees in the pipeline</div>'
            % (e(now.strftime('%A, %d %B')), len(act), e(_inr(exp)))]
    if fl:
        body.append('<h3 style="font-size:14px;color:#C2410C;margin:10px 0 6px">Focus today</h3><ol style="padding-left:18px;margin:0 0 14px">')
        for j in fl:
            body.append('<li style="margin:4px 0"><b>%s</b> — %s<br><span style="color:#555;font-size:12.5px">%s%s</span></li>'
                        % (e(j['role']), e(j['client']), e(j['status_line']), (' · <b>' + e(', '.join(j['focus_reasons'])) + '</b>') if j['focus_reasons'] else ''))
        body.append('</ol>')
    body.append('<table style="border-collapse:collapse;width:100%%"><tr><th %s>Job</th><th %s>Whose move</th><th %s>What is happening</th><th %s>Next</th><th %s>₹ likely</th></tr>' % (th, th, th, th, th))
    for j in rows:
        body.append('<tr><td %s><b>%s</b><br><span style="color:#667085">%s%s</span></td><td %s>%s %s</td><td %s>%s</td><td %s>%s</td><td %s>%s</td></tr>'
                    % (td, e(j['role']), e(j['client']), (' · SPOC ' + e(j['spoc']['name'])) if j['spoc'] else '',
                       td, BALL_ICON[j['ball']['who']], e(j['ball']['label']), td, e(j['status_line']),
                       td, e(j['next']['text']) if j['next'] else '—', td, e(_inr(j['value']['expected']))))
    body.append('</table><p style="color:#888;font-size:11px;margin-top:14px">Sent by HireLab Screener · Jobs page → Job Pulse settings to change or stop this e-mail.</p></div>')
    return subject, '\n'.join(plain), ''.join(body)


def _digest_users(conn, oid, s):
    us = _rows(conn, "SELECT u.id, u.username, u.display_name, u.role, COALESCE(u.is_company_admin,0) AS is_company_admin, "
                     "u.email AS email FROM users u WHERE u.company_id=? AND COALESCE(u.status,'approved')='approved' "
                     "AND u.role<>'freelancer_sourcer'", (oid,)) or \
        _rows(conn, "SELECT id, username, display_name, role, COALESCE(is_company_admin,0) AS is_company_admin FROM users "
                    "WHERE company_id=? AND COALESCE(status,'approved')='approved' AND role<>'freelancer_sourcer'", (oid,))
    out = []
    for u in us:
        admin = u['role'] == 'admin' or u['is_company_admin']
        if not admin and not s['digest_recruiters']:
            continue
        em = str(u.get('email') or '').strip() or (u['username'] if '@' in str(u['username'] or '') else '')
        if em:
            out.append(dict(u, email=em))
    return out


def run_digest(conn, oid, force=False, only_user=None):
    core = _core()
    s = get_settings(conn, oid)
    now = now_ist()
    day = now.strftime('%Y-%m-%d')
    if not force and (not s['digest_enabled'] or s['last_digest_day'] == day or now.hour < int(s['digest_hour'])
                      or now.weekday() == 6):
        return 0
    if not force:
        conn.execute('INSERT OR IGNORE INTO job_pulse_settings (company_id) VALUES (?)', (oid,))
        cur = conn.execute("UPDATE job_pulse_settings SET last_digest_day=? WHERE company_id=? AND COALESCE(last_digest_day,'')<>?",
                           (day, oid, day))
        conn.commit()
        if cur.rowcount != 1:
            return 0
    n = 0
    for u in _digest_users(conn, oid, s):
        if only_user and u['id'] != only_user:
            continue
        d = digest_for(conn, oid, u, now)
        if not d:
            continue
        try:
            ok, _e, _m, _t = core.email_service_send(u['email'], d[0], d[1], body_html=d[2])
            n += 1 if ok else 0
        except Exception as exc:
            print('[job_pulse] digest to %s failed: %s' % (u['email'], exc))
    return n


def _loop():
    time.sleep(120)
    while True:
        try:
            core = _core()
            conn = get_db()
            try:
                for r in conn.execute('SELECT company_id FROM job_pulse_settings WHERE digest_enabled=1').fetchall():
                    core._bg_tenant_set(r[0])
                    try:
                        run_digest(conn, r[0])
                    except Exception as exc:
                        print('[job_pulse] digest failed for %s: %s' % (r[0], exc))
                    finally:
                        core._bg_tenant_clear()
            finally:
                conn.close()
        except Exception as e:
            print('[job_pulse] loop error: %s' % e)
        time.sleep(600)


_started = False


def start_worker():
    global _started
    if _started or 'pytest' in sys.modules or os.environ.get('HIRELAB_NO_WORKERS'):
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
    print('[job_pulse] background thread started')


start_worker()


@bp.route('/digest/test', methods=['POST'])
@login_required
def digest_test():
    """Send today's digest to me now (works even if the daily e-mail is off)."""
    oid = _oid()
    uid = int(real_user_id() or 0)
    conn = get_db()
    try:
        s = get_settings(conn, oid)
        users = [u for u in _digest_users(conn, oid, dict(s, digest_recruiters=1)) if u['id'] == uid]
        if not users:
            return _err('Your user has no e-mail address on file (username or profile e-mail).')
        n = run_digest(conn, oid, force=True, only_user=uid)
        return jsonify({'ok': bool(n), 'to': users[0]['email'], 'error': '' if n else 'Nothing to send or the mailbox failed.'})
    finally:
        conn.close()
