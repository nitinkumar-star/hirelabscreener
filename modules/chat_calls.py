"""
Team Chat — audio calls, video calls and screen sharing (WebRTC).

How it works
  - The sound and the picture go straight between the browsers (WebRTC, peer to
    peer; every person in a call connects to every other one, so a call holds up
    to MAX_PEOPLE). This server only introduces them to each other: it keeps
    who is in which call and passes their connection notes ("signals": offer /
    answer / ICE candidates) back and forth. No audio or video touches it.
  - A call lives in memory (the app runs as one process); its start and end are
    written into the chat as a message, so the chat keeps a record:
    "📞 Video call · 12 min · 3 people".
  - A call opens in its own small window (/call/<id>), like Google Meet, so
    moving around HireLab does not drop it.
  - Direct message calls ring the other person (the chat page and the small
    chat windows poll /api/chat/calls/active every few seconds). Not answered
    in 45 s -> "Missed call" in the chat and on the HireLab bell.

NAT / firewalls: browsers find each other with public STUN servers. On some
office or mobile networks that is not enough; then a TURN relay is needed. Set
TURN_URLS (comma separated, e.g. turn:turn.example.com:3478), TURN_USERNAME and
TURN_CREDENTIAL in the environment and every call uses it.

Routes (/api/chat/..., signed in; every one checks company + channel membership)
  GET  /calls/active                   calls in my chats (for ringing / join banners)
  POST /channels/<id>/call {kind}      start (or join the running call of) a chat
  GET  /calls/<id>                     the call, its people, ICE servers
  POST /calls/<id>/join | leave | decline
  POST /calls/<id>/signal {to, type, data}
  GET  /calls/<id>/signals?since=N     my signals + who is in the call (polled ~1 s)
  GET  /call/<id>                      the call window (page)
"""

import os
import time
import json
import threading

from flask import Blueprint, request, jsonify, redirect, Response

from modules.shared import get_db, ts, _core, login_required

bp = Blueprint('chat_calls', __name__, url_prefix='/api/chat')
pages = Blueprint('chat_calls_pages', __name__)

MAX_PEOPLE = 6
RING_SECONDS = 45
GONE_SECONDS = 25           # no heartbeat for this long -> left
SIGNAL_TTL = 120

_lock = threading.Lock()
CALLS = {}                  # id -> call dict
SIGNALS = {}                # id -> [ {seq, frm, to, type, data, at} ]
_seq = [0]
_next_id = [int(time.time()) % 1000000 * 10]


def _tc():
    from modules import team_chat
    return team_chat


def _err(msg, code=400):
    return jsonify({'ok': False, 'error': msg}), code


def ice_servers():
    out = [{'urls': ['stun:stun.l.google.com:19302', 'stun:stun1.l.google.com:19302']}]
    urls = [u.strip() for u in (os.environ.get('TURN_URLS') or '').split(',') if u.strip()]
    if urls:
        out.append({'urls': urls, 'username': os.environ.get('TURN_USERNAME', ''),
                    'credential': os.environ.get('TURN_CREDENTIAL', '')})
    return out


def _public(c, uid=None):
    now = time.time()
    return {'id': c['id'], 'channel_id': c['channel_id'], 'kind': c['kind'], 'started_by': c['by'],
            'started_at': c['started_iso'], 'people': sorted(c['people'].keys()),
            'ringing': not c['answered'] and now - c['started'] < RING_SECONDS,
            'answered': c['answered'], 'ended': c['ended'], 'declined': sorted(c['declined']),
            'msg_id': c['msg_id'], 'max': MAX_PEOPLE}


def _reap(conn=None):
    """Drop people who stopped sending heartbeats; end empty / unanswered calls."""
    now = time.time()
    ended = []
    with _lock:
        for c in list(CALLS.values()):
            if c['ended']:
                if now - c['ended_at'] > 300:
                    CALLS.pop(c['id'], None)
                    SIGNALS.pop(c['id'], None)
                continue
            for u, seen in list(c['people'].items()):
                if now - seen > GONE_SECONDS:
                    c['people'].pop(u, None)
            # a DM call nobody picked up; or everyone has left (a short grace after the
            # start, so the caller's call window has time to open and join)
            unanswered = c['dm'] and not c['answered'] and now - c['started'] > RING_SECONDS
            empty = not c['people'] and (c['had_people'] or now - c['started'] > 30)
            if empty or unanswered:
                c['ended'] = True
                c['ended_at'] = now
                ended.append(dict(c))
            sig = SIGNALS.get(c['id'])
            if sig:
                SIGNALS[c['id']] = [s for s in sig if now - s['at'] < SIGNAL_TTL]
    for c in ended:
        _record_end(c, conn)


def _record_end(c, conn=None):
    """Turn the call's chat message into its record, and tell about a missed DM call."""
    own = conn is None
    conn = conn or get_db()
    try:
        tc = _tc()
        mins = max(0, int(round((c['ended_at'] - c['started']) / 60.0)))
        kind = 'Video call' if c['kind'] == 'video' else 'Audio call'
        if c['answered']:
            body = '📞 %s · %s · %d %s' % (kind, ('%d min' % mins) if mins else 'under a minute',
                                         c['max_seen'], 'person' if c['max_seen'] == 1 else 'people')
        else:
            body = '📞 Missed %s' % kind.lower()
        conn.execute('UPDATE chat_messages SET body=?, rev=? WHERE id=?', (body, tc.next_rev(), c['msg_id']))
        if not c['answered']:
            ch = tc._channel(conn, c['channel_id'], c['company_id'])
            if ch and ch['kind'] == 'dm':
                names = {u['id']: u['name'] for u in tc._users(conn, c['company_id'])}
                for m in tc._member_ids(conn, ch['id']):
                    if m != c['by'] and m not in c['declined']:
                        conn.execute('INSERT INTO notifications (company_id,user_id,kind,title,body,candidate_id,mandate_id,'
                                     "actor_id,actor_name,created_at,updated_at,read_at,acked_at) VALUES (?,?,?,?,?,0,0,?,?,?,?,'','')",
                                     (c['company_id'], m, 'chat_dm', 'Missed %s from %s' % (kind.lower(), names.get(c['by'], 'a teammate')),
                                      'Open Team Chat to call back.', c['by'], names.get(c['by'], ''), ts(), ts()))
        conn.commit()
    except Exception as exc:
        print('[chat-calls] record end failed: %s' % exc)
    finally:
        if own:
            conn.close()


def _load(conn, call_id, uid, cid):
    """The call if this person may be in it (same company, can read the chat)."""
    c = CALLS.get(call_id)
    if not c or c['company_id'] != cid:
        return None, None
    tc = _tc()
    ch = tc._channel(conn, c['channel_id'], cid)
    if not ch or not tc._is_member(conn, ch['id'], uid):
        return None, None
    return c, ch


# ══════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/calls/active', methods=['GET'])
@login_required
def calls_active():
    tc = _tc()
    uid, cid = tc._me()
    conn = get_db()
    try:
        _reap(conn)
        mine = {r['channel_id'] for r in conn.execute('SELECT channel_id FROM chat_members WHERE user_id=? AND company_id=?', (uid, cid))}
        out = [_public(c, uid) for c in list(CALLS.values()) if c['company_id'] == cid and not c['ended'] and c['channel_id'] in mine]
        if out:
            names = {u['id']: u['name'] for u in tc._users(conn, cid)}
            for c in out:
                ch = tc._channel(conn, c['channel_id'], cid) or {}
                c['by_name'] = names.get(c['started_by'], 'A teammate')
                c['dm'] = ch.get('kind') == 'dm'
                c['chat'] = ch.get('name') or ''
        return jsonify({'ok': True, 'calls': out, 'me': uid})
    finally:
        conn.close()


@bp.route('/channels/<int:chid>/call', methods=['POST'])
@login_required
def start_call(chid):
    tc = _tc()
    uid, cid = tc._me()
    kind = 'video' if (request.get_json(silent=True) or {}).get('kind') == 'video' else 'audio'
    conn = get_db()
    try:
        _reap(conn)
        ch = tc._channel(conn, chid, cid)
        if not ch or not tc._is_member(conn, chid, uid):
            return _err('Not found', 404)
        if ch['kind'] == 'ai':
            return _err('HireLab AI cannot take calls.')
        if ch['archived']:
            return _err('This channel is archived.')
        running = [c for c in CALLS.values() if c['channel_id'] == chid and not c['ended']]
        if running:
            return jsonify({'ok': True, 'call': _public(running[0]), 'joined_existing': True})
        with _lock:
            _next_id[0] += 1
            call_id = _next_id[0]
        msg, e = tc._post(conn, ch, uid, cid, '📞 <call:%d> %s call started' % (call_id, 'Video' if kind == 'video' else 'Audio'), 0)
        if e:
            return _err(e)
        now = time.time()
        with _lock:
            CALLS[call_id] = {'id': call_id, 'company_id': cid, 'channel_id': chid, 'kind': kind, 'by': uid,
                              'started': now, 'started_iso': ts(), 'people': {}, 'answered': False,
                              'declined': set(), 'ended': False, 'ended_at': 0, 'msg_id': msg['id'], 'max_seen': 1,
                              'dm': ch['kind'] == 'dm', 'had_people': False}
            SIGNALS[call_id] = []
        return jsonify({'ok': True, 'call': _public(CALLS[call_id])})
    finally:
        conn.close()


@bp.route('/calls/<int:call_id>', methods=['GET'])
@login_required
def get_call(call_id):
    tc = _tc()
    uid, cid = tc._me()
    conn = get_db()
    try:
        _reap(conn)
        c, ch = _load(conn, call_id, uid, cid)
        if not c:
            return _err('This call has ended or you are not in this chat.', 404)
        names = {u['id']: u['name'] for u in tc._users(conn, cid)}
        return jsonify({'ok': True, 'call': _public(c), 'me': uid, 'names': names,
                        'chat': ch['name'] if ch['kind'] == 'channel' else '',
                        'chat_kind': ch['kind'], 'ice': ice_servers(),
                        'members': tc._member_ids(conn, ch['id']) if ch['kind'] == 'dm' else []})
    finally:
        conn.close()


@bp.route('/calls/<int:call_id>/join', methods=['POST'])
@login_required
def join_call(call_id):
    tc = _tc()
    uid, cid = tc._me()
    conn = get_db()
    try:
        _reap(conn)
        c, ch = _load(conn, call_id, uid, cid)
        if not c or c['ended']:
            return _err('This call has ended.', 404)
        with _lock:
            if uid not in c['people'] and len(c['people']) >= MAX_PEOPLE:
                return _err('This call is full (%d people).' % MAX_PEOPLE, 409)
            others = [u for u in c['people'] if u != uid]
            c['people'][uid] = time.time()
            c['had_people'] = True
            if uid != c['by']:
                c['answered'] = True
            c['max_seen'] = max(c['max_seen'], len(c['people']))
            seq = _seq[0]
        return jsonify({'ok': True, 'call': _public(c), 'peers': others, 'since': seq})
    finally:
        conn.close()


@bp.route('/calls/<int:call_id>/leave', methods=['POST'])
@login_required
def leave_call(call_id):
    tc = _tc()
    uid, cid = tc._me()
    conn = get_db()
    try:
        c, _ch = _load(conn, call_id, uid, cid)
        if not c:
            return jsonify({'ok': True})
        with _lock:
            c['people'].pop(uid, None)
            for other in list(c['people']):
                _push(call_id, uid, other, 'bye', {})
        _reap(conn)
        return jsonify({'ok': True})
    finally:
        conn.close()


@bp.route('/calls/<int:call_id>/decline', methods=['POST'])
@login_required
def decline_call(call_id):
    tc = _tc()
    uid, cid = tc._me()
    conn = get_db()
    try:
        c, _ch = _load(conn, call_id, uid, cid)
        if not c:
            return jsonify({'ok': True})
        with _lock:
            c['declined'].add(uid)
            # a 1:1 call ends when the other person declines
            if c['dm'] and not c['answered']:
                for other in list(c['people']):
                    _push(call_id, uid, other, 'declined', {})
                c['people'].clear()
        _reap(conn)
        return jsonify({'ok': True})
    finally:
        conn.close()


def _push(call_id, frm, to, typ, data):
    _seq[0] += 1
    SIGNALS.setdefault(call_id, []).append({'seq': _seq[0], 'frm': frm, 'to': to, 'type': typ, 'data': data, 'at': time.time()})


@bp.route('/calls/<int:call_id>/signal', methods=['POST'])
@login_required
def signal(call_id):
    tc = _tc()
    uid, cid = tc._me()
    d = request.get_json(silent=True) or {}
    typ = str(d.get('type') or '')
    if typ not in ('offer', 'answer', 'ice', 'state'):
        return _err('Unknown signal.')
    raw = json.dumps(d.get('data') or {})
    if len(raw) > 60000:
        return _err('Signal too large.', 413)
    conn = get_db()
    try:
        c, _ch = _load(conn, call_id, uid, cid)
        if not c or c['ended'] or uid not in c['people']:
            return _err('You are not in this call.', 404)
        with _lock:
            targets = list(c['people']) if d.get('to') in (None, '', 'all') else [d.get('to')]
            for to in targets:
                try:
                    to = int(to)
                except (TypeError, ValueError):
                    continue
                if to != uid and to in c['people']:
                    _push(call_id, uid, to, typ, d.get('data') or {})
        return jsonify({'ok': True})
    finally:
        conn.close()


@bp.route('/calls/<int:call_id>/signals', methods=['GET'])
@login_required
def signals(call_id):
    tc = _tc()
    uid, cid = tc._me()
    try:
        since = int(request.args.get('since') or 0)
    except ValueError:
        since = 0
    conn = get_db()
    try:
        c, _ch = _load(conn, call_id, uid, cid)
        if not c or c['ended']:
            return jsonify({'ok': True, 'ended': True, 'signals': [], 'people': []})
        with _lock:
            if uid in c['people']:
                c['people'][uid] = time.time()             # heartbeat
            out = [{k: s[k] for k in ('seq', 'frm', 'type', 'data')} for s in SIGNALS.get(call_id, [])
                   if s['to'] == uid and s['seq'] > since]
            last = _seq[0]
        _reap(conn)
        return jsonify({'ok': True, 'ended': c['ended'], 'signals': out, 'last': last, 'call': _public(c)})
    finally:
        conn.close()


@pages.route('/chat-ring.js')
def ring_js():
    """Incoming-call ringing + start/join helpers, shared by the chat page and the chat windows."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'chat-ring.js')
    with open(path, encoding='utf-8') as f:
        resp = Response(f.read(), mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@pages.route('/call/<int:call_id>')
def call_page(call_id):
    u = _core().current_user()
    if not u:
        return redirect('/')
    if (u.get('role') or '') == 'freelancer_sourcer':
        return redirect('/')
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'chat-call.html')
    with open(path, encoding='utf-8') as f:
        resp = Response(f.read(), mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-store'
    # camera, microphone and screen capture are allowed on this page only
    resp.headers['Permissions-Policy'] = 'camera=(self), microphone=(self), display-capture=(self)'
    return resp
