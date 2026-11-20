"""
Team Chat — Slack-style messaging for an agency's team, inside HireLab (/team-chat).

  - Channels (public or private) and direct messages, per agency.
  - Threads, @mentions (@name, @channel), emoji reactions, pins, edit / delete.
  - Files on a message (stored under DATA_DIR/chat_files/<company>/).
  - Candidate and job links inside a message: <c:123> / <m:45>. They are shown
    as live cards (name, company, stage …) resolved for EACH viewer: a recruiter
    who may not see that candidate or job gets "restricted", never the details.
  - A channel can be linked to a job (job channel).
  - Search, a Mentions view, unread counts, typing and who is online.
  - "Catch me up": an AI summary of what you missed in a channel (DeepSeek).
  - Make a task from a message: the page calls the existing /api/todo/tasks.

Updates reach the page by polling /api/chat/poll every few seconds. Every
change to a message (new, edit, delete, reaction, reply, pin) gives it a new
`rev` (milliseconds, strictly increasing in this process); the page asks for
rev > what it has, with a few seconds of overlap so a write that committed late
is not missed.

Who: every approved user of the agency except freelancer accounts (the
freelancer API guard already blocks /api/chat for them). Recruiter sub-accounts
take part fully; only the candidate / job cards follow their access.
@mentions and DMs also go to the HireLab bell (notifications, kind chat_*),
unless the person has the chat open right now.

Additive only: new tables chat_channels, chat_members, chat_messages,
chat_reactions.
"""

import os
import re
import json
import time
import uuid
import threading

from flask import Blueprint, request, jsonify, redirect, Response, send_file

from modules import register_migration
from modules.shared import get_db, ts, effective_company_id, real_user_id, login_required, _core

bp = Blueprint('team_chat', __name__, url_prefix='/api/chat')
pages = Blueprint('team_chat_pages', __name__)

FREELANCER_ROLE = 'freelancer_sourcer'
MAX_BODY = 8000
MAX_FILE = 25 * 1024 * 1024
PAGE = 50
EMOJIS = ['👍', '✅', '🎉', '🔥', '👀', '🙏', '😂', '❤️', '💯', '🚀']
_MENTION = re.compile(r'<@(\d+)>')
_REF = re.compile(r'<([cm]):(\d+)>')
_BROADCAST = re.compile(r'(^|\s)@(channel|here|everyone)\b', re.I)

_lock = threading.Lock()
_last_rev = [0]
_typing = {}      # channel_id -> {user_id: time}
_seen = {}        # user_id -> time of last poll (online / chat open)


def next_rev():
    with _lock:
        r = max(int(time.time() * 1000), _last_rev[0] + 1)
        _last_rev[0] = r
        return r


# ══════════════════════════════════════════════════════════════════════════
#  MIGRATION
# ══════════════════════════════════════════════════════════════════════════
@register_migration
def _migrate_chat(conn):
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS chat_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        kind TEXT DEFAULT 'channel',          -- channel | dm
        name TEXT DEFAULT '',
        topic TEXT DEFAULT '',
        is_private INTEGER DEFAULT 0,
        mandate_id INTEGER DEFAULT 0,         -- job channel
        dm_key TEXT DEFAULT '',               -- "<low uid>:<high uid>" for a DM
        created_by INTEGER DEFAULT 0,
        created_at TEXT DEFAULT '',
        archived INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER NOT NULL,
        company_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        last_read_id INTEGER DEFAULT 0,
        joined_at TEXT DEFAULT '',
        muted INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        parent_id INTEGER DEFAULT 0,
        body TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        edited_at TEXT DEFAULT '',
        deleted INTEGER DEFAULT 0,
        reply_count INTEGER DEFAULT 0,
        last_reply_at TEXT DEFAULT '',
        pinned INTEGER DEFAULT 0,
        file_name TEXT DEFAULT '',
        file_path TEXT DEFAULT '',
        file_size INTEGER DEFAULT 0,
        file_type TEXT DEFAULT '',
        rev INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat_reactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER NOT NULL,
        company_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        emoji TEXT NOT NULL
    )''')
    for sql in [
        'CREATE UNIQUE INDEX IF NOT EXISTS ux_chat_member ON chat_members(channel_id, user_id)',
        'CREATE INDEX IF NOT EXISTS idx_chat_member_user ON chat_members(user_id, company_id)',
        'CREATE INDEX IF NOT EXISTS idx_chat_msg_channel ON chat_messages(channel_id, parent_id, id)',
        'CREATE INDEX IF NOT EXISTS idx_chat_msg_rev ON chat_messages(company_id, rev)',
        'CREATE INDEX IF NOT EXISTS idx_chat_msg_parent ON chat_messages(parent_id)',
        'CREATE UNIQUE INDEX IF NOT EXISTS ux_chat_react ON chat_reactions(message_id, user_id, emoji)',
        'CREATE INDEX IF NOT EXISTS idx_chat_channel_co ON chat_channels(company_id, kind)',
    ]:
        try:
            c.execute(sql)
        except Exception:
            pass
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════
def _err(msg, code=400):
    return jsonify({'ok': False, 'error': msg}), code


def _me():
    return int(real_user_id() or 0), int(effective_company_id() or 0)


def _is_admin():
    try:
        return bool(_core().is_company_admin())
    except Exception:
        return False


def _users(conn, cid):
    rows = conn.execute(
        "SELECT id, username, COALESCE(NULLIF(display_name,''), username) AS name, role, "
        "COALESCE(is_company_admin,0) AS is_admin FROM users WHERE company_id=? "
        "AND COALESCE(status,'approved')='approved' AND COALESCE(role,'user')!=? ORDER BY name COLLATE NOCASE",
        (cid, FREELANCER_ROLE)).fetchall()
    return [dict(r) for r in rows]


def _user_ids(conn, cid):
    return {u['id'] for u in _users(conn, cid)}


def _ensure_general(conn, cid, uid):
    """Every agency gets #general; everyone is a member of it."""
    r = conn.execute("SELECT id FROM chat_channels WHERE company_id=? AND kind='channel' AND name='general' "
                     "ORDER BY id LIMIT 1", (cid,)).fetchone()
    if r:
        gid = r['id']
    else:
        cur = conn.execute("INSERT INTO chat_channels (company_id, kind, name, topic, is_private, created_by, created_at) "
                           "VALUES (?, 'channel', 'general', 'Team-wide announcements and chat', 0, ?, ?)",
                           (cid, uid, ts()))
        gid = cur.lastrowid
    if uid and not conn.execute('SELECT 1 FROM chat_members WHERE channel_id=? AND user_id=?', (gid, uid)).fetchone():
        top = conn.execute('SELECT COALESCE(MAX(id),0) m FROM chat_messages WHERE channel_id=?', (gid,)).fetchone()['m']
        conn.execute('INSERT OR IGNORE INTO chat_members (channel_id, company_id, user_id, last_read_id, joined_at) '
                     'VALUES (?,?,?,?,?)', (gid, cid, uid, top, ts()))
    conn.commit()
    return gid


def _channel(conn, chid, cid):
    r = conn.execute('SELECT * FROM chat_channels WHERE id=? AND company_id=?', (chid, cid)).fetchone()
    return dict(r) if r else None


def _is_member(conn, chid, uid):
    return bool(conn.execute('SELECT 1 FROM chat_members WHERE channel_id=? AND user_id=?', (chid, uid)).fetchone())


def _can_read(conn, ch, uid):
    if not ch:
        return False
    if ch['kind'] == 'channel' and not ch['is_private']:
        return True
    return _is_member(conn, ch['id'], uid)


def _can_post(conn, ch, uid):
    return bool(ch) and not ch['archived'] and _is_member(conn, ch['id'], uid)


def _member_ids(conn, chid):
    return [r['user_id'] for r in conn.execute('SELECT user_id FROM chat_members WHERE channel_id=?', (chid,))]


def _visible_channel_ids(conn, cid, uid):
    rows = conn.execute(
        "SELECT c.id FROM chat_channels c WHERE c.company_id=? AND (c.id IN (SELECT channel_id FROM chat_members WHERE user_id=?) "
        "OR (c.kind='channel' AND c.is_private=0))", (cid, uid)).fetchall()
    return [r['id'] for r in rows]


def _clean_body(s):
    s = str(s or '').replace('\r\n', '\n').replace('\r', '\n')
    return s.strip()[:MAX_BODY]


def _slug(name):
    s = re.sub(r'[^a-z0-9\-_]+', '-', str(name or '').strip().lower()).strip('-_')
    return s[:40]


def _refs(conn, cid, bodies):
    """Cards for <c:id> / <m:id> in these bodies, as THIS viewer may see them."""
    from modules import access
    cids, mids = set(), set()
    for b in bodies:
        for kind, n in _REF.findall(b or ''):
            (cids if kind == 'c' else mids).add(int(n))
    out = {}
    for n in list(cids)[:200]:
        r = conn.execute('SELECT c.id, c.name, c.company, c.designation, c.stage, c.mandate_id, c.experience, '
                         'm.role AS m_role, m.client AS m_client, m.status AS m_status '
                         'FROM candidates c LEFT JOIN mandates m ON m.id=c.mandate_id '
                         'WHERE c.id=? AND c.owner_id=?', (n, cid)).fetchone()
        if not r:
            out['c:%d' % n] = {'kind': 'c', 'id': n, 'missing': True}
        elif not access.can_see_candidate(conn, n):
            out['c:%d' % n] = {'kind': 'c', 'id': n, 'restricted': True}
        else:
            d = dict(r)
            central = (d.get('m_status') == 'central')
            out['c:%d' % n] = {'kind': 'c', 'id': n, 'name': d['name'] or '', 'company': d['company'] or '',
                               'designation': d['designation'] or '', 'stage': d['stage'] or '',
                               'experience': d['experience'], 'mandate_id': d['mandate_id'] or 0,
                               'job': '' if central else ' · '.join(x for x in (d['m_role'], d['m_client']) if x)}
    for n in list(mids)[:200]:
        r = conn.execute('SELECT id, role, client, status, location FROM mandates WHERE id=? AND owner_id=?',
                         (n, cid)).fetchone()
        if not r:
            out['m:%d' % n] = {'kind': 'm', 'id': n, 'missing': True}
        elif not access.can_see_mandate(conn, n):
            out['m:%d' % n] = {'kind': 'm', 'id': n, 'restricted': True}
        else:
            d = dict(r)
            cnt = conn.execute('SELECT COUNT(*) n FROM candidates WHERE mandate_id=?', (n,)).fetchone()['n']
            out['m:%d' % n] = {'kind': 'm', 'id': n, 'role': d['role'] or '', 'client': d['client'] or '',
                               'status': d['status'] or '', 'location': d['location'] or '', 'candidates': cnt}
    return out


def _reactions(conn, ids):
    out = {}
    if not ids:
        return out
    q = ','.join('?' * len(ids))
    for r in conn.execute(f'SELECT message_id, emoji, user_id FROM chat_reactions WHERE message_id IN ({q}) ORDER BY id',
                          list(ids)):
        m = out.setdefault(r['message_id'], {})
        m.setdefault(r['emoji'], []).append(r['user_id'])
    return out


def _ser(rows, conn):
    rows = [dict(r) for r in rows]
    reacts = _reactions(conn, [r['id'] for r in rows])
    out = []
    for r in rows:
        d = {k: r[k] for k in ('id', 'channel_id', 'user_id', 'parent_id', 'created_at', 'edited_at', 'deleted',
                               'reply_count', 'last_reply_at', 'pinned', 'rev')}
        d['body'] = '' if r['deleted'] else r['body']
        if r['file_name'] and not r['deleted']:
            d['file'] = {'name': r['file_name'], 'size': r['file_size'], 'type': r['file_type']}
        d['reactions'] = [{'emoji': e, 'users': u} for e, u in reacts.get(r['id'], {}).items()]
        out.append(d)
    return out


def _with_refs(conn, cid, msgs):
    return {'messages': msgs, 'refs': _refs(conn, cid, [m['body'] for m in msgs])}


def _touch(conn, mid):
    conn.execute('UPDATE chat_messages SET rev=? WHERE id=?', (next_rev(), mid))


def _channel_summaries(conn, cid, uid):
    """The caller's channels (member) with unread / mention counts."""
    rows = conn.execute(
        'SELECT c.*, m.last_read_id, m.muted FROM chat_channels c JOIN chat_members m ON m.channel_id=c.id '
        'WHERE c.company_id=? AND m.user_id=? ORDER BY c.kind, c.name COLLATE NOCASE', (cid, uid)).fetchall()
    out = []
    tag = '<@%d>' % uid
    for r in rows:
        d = dict(r)
        un = conn.execute('SELECT COUNT(*) n, COALESCE(MAX(id),0) mx FROM chat_messages WHERE channel_id=? AND parent_id=0 '
                          'AND id>? AND user_id!=? AND deleted=0', (d['id'], d['last_read_id'] or 0, uid)).fetchone()
        men = conn.execute("SELECT COUNT(*) n FROM chat_messages WHERE channel_id=? AND id>? AND user_id!=? AND deleted=0 "
                           "AND (instr(body, ?)>0 OR (? AND (lower(body) LIKE '%@channel%' OR lower(body) LIKE '%@here%')))",
                           (d['id'], d['last_read_id'] or 0, uid, tag, 1)).fetchone()
        last = conn.execute('SELECT id, created_at FROM chat_messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT 1',
                            (d['id'],)).fetchone()
        item = {'id': d['id'], 'kind': d['kind'], 'name': d['name'], 'topic': d['topic'], 'is_private': d['is_private'],
                'mandate_id': d['mandate_id'] or 0, 'archived': d['archived'], 'created_by': d['created_by'],
                'unread': un['n'], 'mentions': men['n'], 'last_read_id': d['last_read_id'] or 0,
                'last_at': last['created_at'] if last else d['created_at'], 'muted': d['muted']}
        if d['kind'] == 'dm':
            item['members'] = _member_ids(conn, d['id'])
        out.append(item)
    return out


def _notify(conn, cid, uid, ch, msg_id, body, parent_id):
    """@mentions and DMs go to the HireLab bell too (unless the person has the chat open)."""
    try:
        users = {u['id']: u for u in _users(conn, cid)}
        me = users.get(uid, {}).get('name', 'Someone')
        members = set(_member_ids(conn, ch['id']))
        targets = {}
        if ch['kind'] == 'dm':
            for m in members:
                if m != uid:
                    targets[m] = ('chat_dm', 'New message from %s' % me)
        mentioned = {int(x) for x in _MENTION.findall(body)}
        if _BROADCAST.search(body) and ch['kind'] == 'channel':
            mentioned |= members
        for m in mentioned:
            if m != uid and m in users and (m in members or (ch['kind'] == 'channel' and not ch['is_private'])):
                where = 'a thread in #%s' % ch['name'] if parent_id else '#%s' % ch['name']
                if ch['kind'] == 'dm':
                    where = 'a direct message'
                targets[m] = ('chat_mention', '%s mentioned you in %s' % (me, where))
        if not targets:
            return
        now = time.time()
        line = _plain(conn, cid, body)[:160] or '(file)'
        for m, (kind, title) in targets.items():
            if now - _seen.get(m, 0) < 20:
                continue                      # they have the chat open: the page shows it
            ex = conn.execute("SELECT id, body FROM notifications WHERE user_id=? AND kind=? AND actor_id=? AND title=? "
                              "AND acked_at='' ORDER BY id DESC LIMIT 1", (m, kind, uid, title)).fetchone()
            if ex:
                lines = [l for l in (ex['body'] or '').split('\n') if l][-5:] + [line]
                conn.execute("UPDATE notifications SET body=?, updated_at=?, read_at='' WHERE id=?",
                             ('\n'.join(lines[-6:]), ts(), ex['id']))
            else:
                conn.execute('INSERT INTO notifications (company_id,user_id,kind,title,body,candidate_id,mandate_id,'
                             "actor_id,actor_name,created_at,updated_at,read_at,acked_at) VALUES (?,?,?,?,?,0,0,?,?,?,?,'','')",
                             (cid, m, kind, title, line, uid, me, ts(), ts()))
    except Exception as e:
        print('[team-chat] notify failed: %s' % e)


def _plain(conn, cid, body):
    """Readable text: <@12> -> @Name, <c:5> -> the candidate's name (if this user may see it)."""
    names = {u['id']: u['name'] for u in _users(conn, cid)}
    s = _MENTION.sub(lambda m: '@' + names.get(int(m.group(1)), 'someone'), body or '')
    refs = _refs(conn, cid, [s])

    def rr(m):
        x = refs.get('%s:%s' % (m.group(1), m.group(2))) or {}
        if x.get('name'):
            return x['name']
        if x.get('role'):
            return x['role'] + (' (%s)' % x['client'] if x.get('client') else '')
        return '[candidate]' if m.group(1) == 'c' else '[job]'
    return _REF.sub(rr, s).strip()


# ══════════════════════════════════════════════════════════════════════════
#  PAGE
# ══════════════════════════════════════════════════════════════════════════
@pages.route('/team-chat')
def team_chat_page():
    u = _core().current_user()
    if not u:
        return redirect('/')
    if (u.get('role') or '') == FREELANCER_ROLE:
        return redirect('/')
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'team-chat.html')
    with open(path, encoding='utf-8') as f:
        resp = Response(f.read(), mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


# ══════════════════════════════════════════════════════════════════════════
#  STATE + POLL
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/state', methods=['GET'])
@login_required
def chat_state():
    uid, cid = _me()
    conn = get_db()
    try:
        _ensure_general(conn, cid, uid)
        users = _users(conn, cid)
        mine = {c['id'] for c in _channel_summaries(conn, cid, uid)}
        browse = [dict(r) for r in conn.execute(
            "SELECT id, name, topic, mandate_id, archived, (SELECT COUNT(*) FROM chat_members m WHERE m.channel_id=c.id) AS n "
            "FROM chat_channels c WHERE company_id=? AND kind='channel' AND is_private=0 ORDER BY name COLLATE NOCASE",
            (cid,)).fetchall() if r['id'] not in mine]
        _seen[uid] = time.time()
        return jsonify({'ok': True, 'me': uid, 'is_admin': _is_admin(), 'users': users,
                        'channels': _channel_summaries(conn, cid, uid), 'browse': browse,
                        'emojis': EMOJIS, 'rev': _last_rev[0] or next_rev(),
                        'ai': bool(_ai_key())})
    finally:
        conn.close()


@bp.route('/poll', methods=['GET'])
@login_required
def chat_poll():
    uid, cid = _me()
    try:
        since = int(request.args.get('since') or 0)
    except ValueError:
        since = 0
    now = time.time()
    _seen[uid] = now
    conn = get_db()
    try:
        ids = _visible_channel_ids(conn, cid, uid)
        msgs = []
        if ids and since:
            q = ','.join('?' * len(ids))
            rows = conn.execute(f'SELECT * FROM chat_messages WHERE company_id=? AND rev>? AND channel_id IN ({q}) '
                                'ORDER BY rev LIMIT 300', [cid, since - 5000] + ids).fetchall()
            msgs = _ser(rows, conn)
        out = _with_refs(conn, cid, msgs)
        typing = {}
        for chid, who in list(_typing.items()):
            names = [u for u, t in who.items() if now - t < 6 and u != uid]
            if names and chid in ids:
                typing[chid] = names
        online = [u for u, t in _seen.items() if now - t < 45]
        out.update({'ok': True, 'channels': _channel_summaries(conn, cid, uid), 'typing': typing,
                    'online': online, 'rev': _last_rev[0]})
        return jsonify(out)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  CHANNELS
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/channels', methods=['POST'])
@login_required
def create_channel():
    uid, cid = _me()
    d = request.get_json(silent=True) or {}
    name = _slug(d.get('name'))
    if not name:
        return _err('Give the channel a name (letters, numbers, - or _).')
    conn = get_db()
    try:
        if conn.execute("SELECT 1 FROM chat_channels WHERE company_id=? AND kind='channel' AND name=?", (cid, name)).fetchone():
            return _err('A channel called #%s already exists.' % name)
        job = 0
        if d.get('job'):
            from modules import access
            try:
                job = int(d.get('job'))
            except (TypeError, ValueError):
                return _err('Unknown job.')
            if not conn.execute('SELECT 1 FROM mandates WHERE id=? AND owner_id=?', (job, cid)).fetchone() \
                    or not access.can_see_mandate(conn, job):
                return _err('Unknown job.')
        cur = conn.execute('INSERT INTO chat_channels (company_id, kind, name, topic, is_private, mandate_id, created_by, created_at) '
                           "VALUES (?, 'channel', ?, ?, ?, ?, ?, ?)",
                           (cid, name, str(d.get('topic') or '')[:250], 1 if d.get('private') else 0, job, uid, ts()))
        chid = cur.lastrowid
        valid = _user_ids(conn, cid)
        members = {uid} | {int(x) for x in (d.get('members') or []) if str(x).isdigit() and int(x) in valid}
        for m in members:
            conn.execute('INSERT OR IGNORE INTO chat_members (channel_id, company_id, user_id, joined_at) VALUES (?,?,?,?)',
                         (chid, cid, m, ts()))
        conn.commit()
        return jsonify({'ok': True, 'id': chid})
    finally:
        conn.close()


@bp.route('/channels/<int:chid>', methods=['PATCH', 'POST'])
@login_required
def edit_channel(chid):
    uid, cid = _me()
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        ch = _channel(conn, chid, cid)
        if not ch or ch['kind'] != 'channel' or not _can_read(conn, ch, uid):
            return _err('Not found', 404)
        sets = {}
        if 'topic' in d:
            if not _is_member(conn, chid, uid):
                return _err('Join the channel first.', 403)
            sets['topic'] = str(d.get('topic') or '')[:250]
        owner = ch['created_by'] == uid or _is_admin()
        if 'name' in d:
            if not owner or ch['name'] == 'general':
                return _err('Only the person who made the channel or an admin can rename it.', 403)
            nm = _slug(d.get('name'))
            if not nm:
                return _err('Give the channel a name.')
            if conn.execute("SELECT 1 FROM chat_channels WHERE company_id=? AND kind='channel' AND name=? AND id!=?",
                            (cid, nm, chid)).fetchone():
                return _err('A channel called #%s already exists.' % nm)
            sets['name'] = nm
        if 'archived' in d:
            if not owner or ch['name'] == 'general':
                return _err('Only the person who made the channel or an admin can archive it.', 403)
            sets['archived'] = 1 if d.get('archived') else 0
        if sets:
            conn.execute('UPDATE chat_channels SET %s WHERE id=?' % ','.join('%s=?' % k for k in sets),
                         list(sets.values()) + [chid])
            conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


@bp.route('/channels/<int:chid>/join', methods=['POST'])
@login_required
def join_channel(chid):
    uid, cid = _me()
    conn = get_db()
    try:
        ch = _channel(conn, chid, cid)
        if not ch or ch['kind'] != 'channel' or ch['is_private']:
            return _err('Not found', 404)
        top = conn.execute('SELECT COALESCE(MAX(id),0) m FROM chat_messages WHERE channel_id=?', (chid,)).fetchone()['m']
        conn.execute('INSERT OR IGNORE INTO chat_members (channel_id, company_id, user_id, last_read_id, joined_at) '
                     'VALUES (?,?,?,?,?)', (chid, cid, uid, top, ts()))
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


@bp.route('/channels/<int:chid>/leave', methods=['POST'])
@login_required
def leave_channel(chid):
    uid, cid = _me()
    conn = get_db()
    try:
        ch = _channel(conn, chid, cid)
        if not ch or ch['kind'] != 'channel':
            return _err('Not found', 404)
        if ch['name'] == 'general':
            return _err('Everyone stays in #general.')
        conn.execute('DELETE FROM chat_members WHERE channel_id=? AND user_id=?', (chid, uid))
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


@bp.route('/channels/<int:chid>/members', methods=['GET', 'POST'])
@login_required
def channel_members(chid):
    uid, cid = _me()
    conn = get_db()
    try:
        ch = _channel(conn, chid, cid)
        if not ch or not _can_read(conn, ch, uid):
            return _err('Not found', 404)
        if request.method == 'POST':
            if ch['kind'] != 'channel' or not _is_member(conn, chid, uid):
                return _err('Join the channel first.', 403)
            d = request.get_json(silent=True) or {}
            valid = _user_ids(conn, cid)
            for x in (d.get('add') or []):
                if str(x).isdigit() and int(x) in valid:
                    conn.execute('INSERT OR IGNORE INTO chat_members (channel_id, company_id, user_id, joined_at) '
                                 'VALUES (?,?,?,?)', (chid, cid, int(x), ts()))
            for x in (d.get('remove') or []):
                if str(x).isdigit() and (ch['created_by'] == uid or _is_admin()) and ch['name'] != 'general':
                    conn.execute('DELETE FROM chat_members WHERE channel_id=? AND user_id=?', (chid, int(x)))
            conn.commit()
        return jsonify({'ok': True, 'members': _member_ids(conn, chid)})
    finally:
        conn.close()


@bp.route('/dm', methods=['POST'])
@login_required
def open_dm():
    uid, cid = _me()
    d = request.get_json(silent=True) or {}
    try:
        other = int(d.get('user') or 0)
    except (TypeError, ValueError):
        other = 0
    conn = get_db()
    try:
        if other not in _user_ids(conn, cid):
            return _err('Unknown person.', 404)
        key = '%d:%d' % (min(uid, other), max(uid, other))
        r = conn.execute("SELECT id FROM chat_channels WHERE company_id=? AND kind='dm' AND dm_key=?", (cid, key)).fetchone()
        if r:
            chid = r['id']
        else:
            chid = conn.execute("INSERT INTO chat_channels (company_id, kind, name, dm_key, is_private, created_by, created_at) "
                                "VALUES (?, 'dm', '', ?, 1, ?, ?)", (cid, key, uid, ts())).lastrowid
        for m in {uid, other}:
            conn.execute('INSERT OR IGNORE INTO chat_members (channel_id, company_id, user_id, joined_at) VALUES (?,?,?,?)',
                         (chid, cid, m, ts()))
        conn.commit()
        return jsonify({'ok': True, 'id': chid})
    finally:
        conn.close()


@bp.route('/channels/<int:chid>/read', methods=['POST'])
@login_required
def mark_read(chid):
    uid, cid = _me()
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        top = conn.execute('SELECT COALESCE(MAX(id),0) m FROM chat_messages WHERE channel_id=? AND company_id=?',
                           (chid, cid)).fetchone()['m']
        try:
            upto = min(int(d.get('upto') or top), top)
        except (TypeError, ValueError):
            upto = top
        conn.execute('UPDATE chat_members SET last_read_id=MAX(COALESCE(last_read_id,0), ?) WHERE channel_id=? AND user_id=?',
                     (upto, chid, uid))
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


@bp.route('/channels/<int:chid>/typing', methods=['POST'])
@login_required
def typing(chid):
    uid, _cid = _me()
    _typing.setdefault(chid, {})[uid] = time.time()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════
#  MESSAGES
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/channels/<int:chid>/messages', methods=['GET'])
@login_required
def list_messages(chid):
    uid, cid = _me()
    conn = get_db()
    try:
        ch = _channel(conn, chid, cid)
        if not _can_read(conn, ch, uid):
            return _err('Not found', 404)
        try:
            before = int(request.args.get('before') or 0)
        except ValueError:
            before = 0
        sql = 'SELECT * FROM chat_messages WHERE channel_id=? AND parent_id=0'
        args = [chid]
        if before:
            sql += ' AND id<?'
            args.append(before)
        rows = conn.execute(sql + ' ORDER BY id DESC LIMIT ?', args + [PAGE + 1]).fetchall()
        more = len(rows) > PAGE
        rows = list(reversed(rows[:PAGE]))
        out = _with_refs(conn, cid, _ser(rows, conn))
        pins = _ser(conn.execute('SELECT * FROM chat_messages WHERE channel_id=? AND pinned=1 AND deleted=0 ORDER BY id DESC LIMIT 20',
                                 (chid,)).fetchall(), conn)
        out['refs'].update(_refs(conn, cid, [p['body'] for p in pins]))
        if ch['mandate_id']:
            out['refs'].update(_refs(conn, cid, ['<m:%d>' % ch['mandate_id']]))
        out.update({'ok': True, 'more': more, 'pins': pins, 'member': _is_member(conn, chid, uid),
                    'members': _member_ids(conn, chid)})
        return jsonify(out)
    finally:
        conn.close()


@bp.route('/messages/<int:mid>/thread', methods=['GET'])
@login_required
def thread(mid):
    uid, cid = _me()
    conn = get_db()
    try:
        p = conn.execute('SELECT * FROM chat_messages WHERE id=? AND company_id=?', (mid, cid)).fetchone()
        if not p or not _can_read(conn, _channel(conn, p['channel_id'], cid), uid):
            return _err('Not found', 404)
        rows = conn.execute('SELECT * FROM chat_messages WHERE parent_id=? ORDER BY id', (mid,)).fetchall()
        out = _with_refs(conn, cid, _ser([p] + list(rows), conn))
        out['ok'] = True
        return jsonify(out)
    finally:
        conn.close()


def _post(conn, ch, uid, cid, body, parent_id, file_info=None):
    if parent_id:
        p = conn.execute('SELECT id, parent_id FROM chat_messages WHERE id=? AND channel_id=?', (parent_id, ch['id'])).fetchone()
        if not p:
            return None, 'That message is gone.'
        if p['parent_id']:
            parent_id = p['parent_id']           # a reply to a reply stays in the same thread
    fi = file_info or {}
    cur = conn.execute('INSERT INTO chat_messages (company_id, channel_id, user_id, parent_id, body, created_at, '
                       'file_name, file_path, file_size, file_type, rev) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (cid, ch['id'], uid, parent_id or 0, body, ts(), fi.get('name', ''), fi.get('path', ''),
                        fi.get('size', 0), fi.get('type', ''), next_rev()))
    mid = cur.lastrowid
    if parent_id:
        conn.execute('UPDATE chat_messages SET reply_count=(SELECT COUNT(*) FROM chat_messages WHERE parent_id=? AND deleted=0), '
                     'last_reply_at=?, rev=? WHERE id=?', (parent_id, ts(), next_rev(), parent_id))
    else:
        conn.execute('UPDATE chat_members SET last_read_id=MAX(COALESCE(last_read_id,0), ?) WHERE channel_id=? AND user_id=?',
                     (mid, ch['id'], uid))
    if ch['kind'] == 'dm':                       # a DM shows up for the other person even if they "left" it
        pass
    _typing.get(ch['id'], {}).pop(uid, None)
    _notify(conn, cid, uid, ch, mid, body, parent_id)
    conn.commit()
    row = conn.execute('SELECT * FROM chat_messages WHERE id=?', (mid,)).fetchone()
    return _ser([row], conn)[0], None


@bp.route('/channels/<int:chid>/messages', methods=['POST'])
@login_required
def send_message(chid):
    uid, cid = _me()
    d = request.get_json(silent=True) or {}
    body = _clean_body(d.get('body'))
    if not body:
        return _err('Write a message first.')
    conn = get_db()
    try:
        ch = _channel(conn, chid, cid)
        if not _can_read(conn, ch, uid):
            return _err('Not found', 404)
        if not _can_post(conn, ch, uid):
            return _err('Join the channel to post here.' if not ch['archived'] else 'This channel is archived.', 403)
        try:
            parent = int(d.get('parent_id') or 0)
        except (TypeError, ValueError):
            parent = 0
        msg, e = _post(conn, ch, uid, cid, body, parent)
        if e:
            return _err(e)
        out = _with_refs(conn, cid, [msg])
        return jsonify({'ok': True, 'message': msg, 'refs': out['refs']})
    finally:
        conn.close()


def _files_dir(cid):
    d = os.path.join(_core().DATA_DIR, 'chat_files', str(int(cid)))
    os.makedirs(d, exist_ok=True)
    return d


@bp.route('/channels/<int:chid>/upload', methods=['POST'])
@login_required
def upload(chid):
    uid, cid = _me()
    f = request.files.get('file')
    if not f or not f.filename:
        return _err('Choose a file.')
    conn = get_db()
    try:
        ch = _channel(conn, chid, cid)
        if not _can_read(conn, ch, uid):
            return _err('Not found', 404)
        if not _can_post(conn, ch, uid):
            return _err('Join the channel to post here.', 403)
        name = os.path.basename(f.filename).replace('\x00', '')[:180] or 'file'
        safe = re.sub(r'[^A-Za-z0-9._-]+', '_', name)[-80:]
        rel = '%s_%s' % (uuid.uuid4().hex, safe)
        path = os.path.join(_files_dir(cid), rel)
        f.save(path)
        size = os.path.getsize(path)
        if size > MAX_FILE:
            os.remove(path)
            return _err('Files up to 25 MB.')
        try:
            parent = int(request.form.get('parent_id') or 0)
        except ValueError:
            parent = 0
        body = _clean_body(request.form.get('body'))
        msg, e = _post(conn, ch, uid, cid, body, parent,
                       {'name': name, 'path': rel, 'size': size, 'type': (f.mimetype or '')[:80]})
        if e:
            os.remove(path)
            return _err(e)
        out = _with_refs(conn, cid, [msg])
        return jsonify({'ok': True, 'message': msg, 'refs': out['refs']})
    finally:
        conn.close()


@bp.route('/files/<int:mid>', methods=['GET'])
@login_required
def download(mid):
    uid, cid = _me()
    conn = get_db()
    try:
        r = conn.execute('SELECT * FROM chat_messages WHERE id=? AND company_id=?', (mid, cid)).fetchone()
        if not r or r['deleted'] or not r['file_path'] or not _can_read(conn, _channel(conn, r['channel_id'], cid), uid):
            return _err('Not found', 404)
        path = os.path.join(_files_dir(cid), os.path.basename(r['file_path']))
        if not os.path.exists(path):
            return _err('The file is no longer on the server.', 404)
        inline = request.args.get('inline') == '1' and (r['file_type'] or '').startswith(('image/', 'application/pdf'))
        return send_file(path, mimetype=r['file_type'] or 'application/octet-stream',
                         as_attachment=not inline, download_name=r['file_name'] or 'file')
    finally:
        conn.close()


@bp.route('/messages/<int:mid>', methods=['PATCH', 'DELETE'])
@login_required
def edit_message(mid):
    uid, cid = _me()
    conn = get_db()
    try:
        r = conn.execute('SELECT * FROM chat_messages WHERE id=? AND company_id=?', (mid, cid)).fetchone()
        if not r or r['deleted']:
            return _err('Not found', 404)
        ch = _channel(conn, r['channel_id'], cid)
        if not _can_read(conn, ch, uid):
            return _err('Not found', 404)
        if request.method == 'DELETE':
            if r['user_id'] != uid and not _is_admin():
                return _err('You can delete only your own messages.', 403)
            if r['file_path']:
                try:
                    os.remove(os.path.join(_files_dir(cid), os.path.basename(r['file_path'])))
                except OSError:
                    pass
            conn.execute("UPDATE chat_messages SET deleted=1, body='', file_name='', file_path='', pinned=0, rev=? WHERE id=?",
                         (next_rev(), mid))
            conn.execute('DELETE FROM chat_reactions WHERE message_id=?', (mid,))
            if r['parent_id']:
                conn.execute('UPDATE chat_messages SET reply_count=(SELECT COUNT(*) FROM chat_messages WHERE parent_id=? AND deleted=0), '
                             'rev=? WHERE id=?', (r['parent_id'], next_rev(), r['parent_id']))
        else:
            if r['user_id'] != uid:
                return _err('You can edit only your own messages.', 403)
            body = _clean_body((request.get_json(silent=True) or {}).get('body'))
            if not body and not r['file_path']:
                return _err('Write a message first.')
            conn.execute('UPDATE chat_messages SET body=?, edited_at=?, rev=? WHERE id=?', (body, ts(), next_rev(), mid))
        conn.commit()
        row = conn.execute('SELECT * FROM chat_messages WHERE id=?', (mid,)).fetchone()
        msg = _ser([row], conn)[0]
        return jsonify({'ok': True, 'message': msg, 'refs': _refs(conn, cid, [msg['body']])})
    finally:
        conn.close()


@bp.route('/messages/<int:mid>/react', methods=['POST'])
@login_required
def react(mid):
    uid, cid = _me()
    emoji = str((request.get_json(silent=True) or {}).get('emoji') or '')[:16]
    if not emoji:
        return _err('Pick an emoji.')
    conn = get_db()
    try:
        r = conn.execute('SELECT * FROM chat_messages WHERE id=? AND company_id=?', (mid, cid)).fetchone()
        if not r or r['deleted'] or not _can_post(conn, _channel(conn, r['channel_id'], cid), uid):
            return _err('Not found', 404)
        ex = conn.execute('SELECT id FROM chat_reactions WHERE message_id=? AND user_id=? AND emoji=?', (mid, uid, emoji)).fetchone()
        if ex:
            conn.execute('DELETE FROM chat_reactions WHERE id=?', (ex['id'],))
        else:
            conn.execute('INSERT INTO chat_reactions (message_id, company_id, user_id, emoji) VALUES (?,?,?,?)',
                         (mid, cid, uid, emoji))
        _touch(conn, mid)
        conn.commit()
        return jsonify({'ok': True, 'message': _ser([conn.execute('SELECT * FROM chat_messages WHERE id=?', (mid,)).fetchone()], conn)[0]})
    finally:
        conn.close()


@bp.route('/messages/<int:mid>/pin', methods=['POST'])
@login_required
def pin(mid):
    uid, cid = _me()
    conn = get_db()
    try:
        r = conn.execute('SELECT * FROM chat_messages WHERE id=? AND company_id=?', (mid, cid)).fetchone()
        if not r or r['deleted'] or not _can_post(conn, _channel(conn, r['channel_id'], cid), uid):
            return _err('Not found', 404)
        conn.execute('UPDATE chat_messages SET pinned=?, rev=? WHERE id=?', (0 if r['pinned'] else 1, next_rev(), mid))
        conn.commit()
        return jsonify({'ok': True, 'pinned': not r['pinned']})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  SEARCH, MENTIONS, LOOKUP
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/search', methods=['GET'])
@login_required
def search():
    uid, cid = _me()
    q = str(request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify({'ok': True, 'messages': [], 'refs': {}})
    conn = get_db()
    try:
        ids = _visible_channel_ids(conn, cid, uid)
        if not ids:
            return jsonify({'ok': True, 'messages': [], 'refs': {}})
        qs = ','.join('?' * len(ids))
        sql = f'SELECT * FROM chat_messages WHERE company_id=? AND deleted=0 AND channel_id IN ({qs})'
        args = [cid] + ids
        # people: "from:riya"; words: every word must appear (names of @people and files count too)
        names = {u['name'].lower(): u['id'] for u in _users(conn, cid)}
        for w in q.split()[:8]:
            if w.lower().startswith('from:'):
                who = w[5:].lower()
                hit = [i for n, i in names.items() if n.startswith(who) or who in n]
                sql += ' AND user_id IN (%s)' % (','.join(str(int(i)) for i in hit) or '0')
                continue
            like = '%' + w.replace('%', '').replace('_', '') + '%'
            ment = [i for n, i in names.items() if w.lower().lstrip('@') in n]
            extra = ''.join(" OR body LIKE '%%<@%d>%%'" % int(i) for i in ment[:5])
            sql += ' AND (body LIKE ? OR file_name LIKE ?%s)' % extra
            args += [like, like]
        rows = conn.execute(sql + ' ORDER BY id DESC LIMIT 60', args).fetchall()
        out = _with_refs(conn, cid, _ser(rows, conn))
        out['ok'] = True
        return jsonify(out)
    finally:
        conn.close()


@bp.route('/mentions', methods=['GET'])
@login_required
def mentions():
    uid, cid = _me()
    conn = get_db()
    try:
        ids = [i for i in _visible_channel_ids(conn, cid, uid) if _is_member(conn, i, uid)]
        if not ids:
            return jsonify({'ok': True, 'messages': [], 'refs': {}})
        qs = ','.join('?' * len(ids))
        rows = conn.execute(f"SELECT * FROM chat_messages WHERE company_id=? AND deleted=0 AND user_id!=? AND channel_id IN ({qs}) "
                            "AND (instr(body, ?)>0 OR lower(body) LIKE '%@channel%' OR lower(body) LIKE '%@here%') "
                            'ORDER BY id DESC LIMIT 60', [cid, uid] + ids + ['<@%d>' % uid]).fetchall()
        out = _with_refs(conn, cid, _ser(rows, conn))
        out['ok'] = True
        return jsonify(out)
    finally:
        conn.close()


@bp.route('/lookup', methods=['GET'])
@login_required
def lookup():
    """Candidates and jobs to link in a message (the caller's own access)."""
    uid, cid = _me()
    q = str(request.args.get('q') or '').strip()
    conn = get_db()
    try:
        from modules import access
        like = '%' + q.replace('%', '') + '%'
        sc, sp = access.candidate_scope_sql('c.mandate_id')
        cands = [dict(r) for r in conn.execute(
            'SELECT c.id, c.name, c.company, c.designation, c.stage, m.role AS job FROM candidates c '
            'LEFT JOIN mandates m ON m.id=c.mandate_id WHERE c.owner_id=? AND (c.name LIKE ? OR c.company LIKE ? '
            'OR c.phone LIKE ? OR c.email LIKE ?)' + sc + ' ORDER BY c.id DESC LIMIT 8',
            [cid, like, like, like, like] + sp).fetchall()] if len(q) >= 2 else []
        mc, mp = access.mandate_scope_sql('m.id')
        jobs = [dict(r) for r in conn.execute(
            "SELECT m.id, m.role, m.client, m.status FROM mandates m WHERE m.owner_id=? AND m.status!='central' "
            "AND (m.role LIKE ? OR m.client LIKE ?)" + mc + " ORDER BY (m.status='active') DESC, m.id DESC LIMIT 8",
            [cid, like, like] + mp).fetchall()]
        return jsonify({'ok': True, 'candidates': cands, 'jobs': jobs})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  AI: CATCH ME UP
# ══════════════════════════════════════════════════════════════════════════
def _ai_key():
    try:
        return (_core().get_setting('deepseek_api_key') or '').strip()
    except Exception:
        return ''


CATCHUP_PROMPT = """You summarise a recruitment agency's team chat for a recruiter who was away.
Write in the same language mix the team uses (English / Hinglish).
Give: 1) a 2-3 line summary, 2) decisions made, 3) action items with the person's name,
4) anything that needs THIS reader ({me}) — questions to them or @mentions.
Use short bullet points. Skip greetings and small talk. If nothing important happened, say so in one line.
Return JSON: {{"summary": "...", "decisions": ["..."], "actions": ["..."], "for_you": ["..."]}}"""


@bp.route('/channels/<int:chid>/catchup', methods=['POST'])
@login_required
def catchup(chid):
    uid, cid = _me()
    core = _core()
    key = _ai_key()
    if not key:
        return _err('AI is not set up. Add the DeepSeek API key in ATS Settings.', 400)
    conn = get_db()
    try:
        ch = _channel(conn, chid, cid)
        if not _can_read(conn, ch, uid):
            return _err('Not found', 404)
        last = conn.execute('SELECT last_read_id FROM chat_members WHERE channel_id=? AND user_id=?', (chid, uid)).fetchone()
        since = int((last['last_read_id'] if last else 0) or 0)
        rows = conn.execute('SELECT * FROM chat_messages WHERE channel_id=? AND deleted=0 AND id>? ORDER BY id LIMIT 300',
                            (chid, since)).fetchall()
        what = 'unread'
        if len(rows) < 3:
            rows = list(reversed(conn.execute('SELECT * FROM chat_messages WHERE channel_id=? AND deleted=0 '
                                              'ORDER BY id DESC LIMIT 60', (chid,)).fetchall()))
            what = 'recent'
        if not rows:
            return jsonify({'ok': True, 'empty': True, 'what': what})
        names = {u['id']: u['name'] for u in _users(conn, cid)}
        lines = []
        for r in rows:
            txt = _plain(conn, cid, r['body'])
            if r['file_name']:
                txt += ' [file: %s]' % r['file_name']
            lines.append('%s %s%s: %s' % ((r['created_at'] or '')[:16], '  ↳ ' if r['parent_id'] else '',
                                         names.get(r['user_id'], 'someone'), txt[:600]))
        payload = {'model': 'deepseek-chat', 'temperature': 0.2, 'max_tokens': 700,
                   'response_format': {'type': 'json_object'},
                   'messages': [{'role': 'system', 'content': CATCHUP_PROMPT.format(me=names.get(uid, 'the reader'))},
                                {'role': 'user', 'content': 'CHANNEL: %s\n\n%s' % (
                                    ch['name'] or 'direct message', '\n'.join(lines)[-24000:])}]}
        try:
            resp = core.call_deepseek(key, payload, timeout=60, endpoint='team-chat-catchup')
            if resp.status_code != 200:
                raise RuntimeError('DeepSeek returned HTTP %s' % resp.status_code)
            raw = resp.json()['choices'][0]['message']['content']
            try:
                j = json.loads(raw)
            except Exception:
                j = core.parse_json(raw) or {}
        except Exception as exc:
            if type(exc).__name__ == 'TokenCapError':
                return _err('Your AI usage cap for this period is reached. Ask the admin to raise it.', 429)
            print('[team-chat] catch-up failed: %s: %s' % (type(exc).__name__, exc))
            return _err('The AI could not summarise right now. Try again in a moment.', 502)
        if not isinstance(j, dict):
            j = {'summary': str(j)}
        clean = lambda v: [str(x)[:400] for x in v][:12] if isinstance(v, list) else []
        return jsonify({'ok': True, 'what': what, 'count': len(rows), 'summary': str(j.get('summary') or '')[:1500],
                        'decisions': clean(j.get('decisions')), 'actions': clean(j.get('actions')),
                        'for_you': clean(j.get('for_you'))})
    finally:
        conn.close()
