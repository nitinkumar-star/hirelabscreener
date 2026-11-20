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
  - Chat windows on every ATS page (/chat-dock.js, like Google Chat in Gmail):
    a Chat button with the unread count, small windows per conversation.

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

More (Nov 2026):
  - Buttons on a candidate card (the page calls the existing ATS routes):
    stage, assign (a task for a teammate), interview, add to a job.
  - HireLab AI: "@AI ..." in any chat, or the private "HireLab AI" chat that
    everyone has. It answers from the ATS with the asker's own access
    (modules/chat_ai.py). In a channel it answers in a thread.
  - Morning summary: every day, the first time you open HireLab after 08:30
    IST, a message in your HireLab AI chat (yesterday + what is pending
    today). It can be turned off.
  - Scheduled messages: write now, send later (delivered by the background
    loop every minute, and on the next poll as a fallback).
  - Status: "On calls", "In an interview", "On leave" ... shown next to names.

Additive only: new tables chat_channels, chat_members, chat_messages,
chat_reactions, chat_prefs, chat_scheduled.
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

BOT_ID = 0                     # HireLab AI writes as user 0
AI_SYNC = False                # tests: answer inline instead of in a thread
_AI_RX = re.compile(r'(^|[\s(])@ai\b', re.I)
DIGEST_AT = (8, 30)            # IST
STATUS_PRESETS = [['📞', 'On calls'], ['🎯', 'In an interview'], ['🤝', 'In a meeting'], ['🍽️', 'Lunch break'],
                  ['🏠', 'Working from home'], ['🚗', 'Travelling'], ['🏖️', 'On leave'], ['🤒', 'Out sick']]
DEFAULT_STAGES = ['Screening', 'Follow Up 1', 'Follow Up 2', 'Not Contacted', 'Called', 'Interested', 'Updated CV awaited',
                  'Not Interested', 'Shared with Client', 'Interview Inprocess', 'Not Suitable', 'Client Rejected on Paper',
                  'Client Rejected After Interview']
_ai_calls = {}                 # user -> [times] (rate limit)

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
    c.execute('''CREATE TABLE IF NOT EXISTS chat_prefs (
        user_id INTEGER PRIMARY KEY,
        company_id INTEGER NOT NULL,
        status_emoji TEXT DEFAULT '',
        status_text TEXT DEFAULT '',
        status_until TEXT DEFAULT '',
        digest_date TEXT DEFAULT '',
        digest_off INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat_scheduled (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        parent_id INTEGER DEFAULT 0,
        body TEXT DEFAULT '',
        send_at TEXT NOT NULL,
        created_at TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',     -- pending | sending | sent | failed | cancelled
        message_id INTEGER DEFAULT 0,
        error TEXT DEFAULT ''
    )''')
    for sql in [
        'CREATE INDEX IF NOT EXISTS idx_chat_sched_due ON chat_scheduled(status, send_at)',
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
    # threads (Nov 2026): who follows a thread + how far they have read it;
    # a reply can also be shown in the channel; a thread can be marked resolved
    c.execute('''CREATE TABLE IF NOT EXISTS chat_thread_follows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER NOT NULL,
        company_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        last_read_id INTEGER DEFAULT 0,
        muted INTEGER DEFAULT 0,
        created_at TEXT DEFAULT ''
    )''')
    for sql in ['CREATE UNIQUE INDEX IF NOT EXISTS ux_chat_follow ON chat_thread_follows(message_id, user_id)',
                'CREATE INDEX IF NOT EXISTS idx_chat_follow_user ON chat_thread_follows(user_id, muted)',
                "ALTER TABLE chat_messages ADD COLUMN broadcast INTEGER DEFAULT 0",
                "ALTER TABLE chat_messages ADD COLUMN resolved_by INTEGER DEFAULT 0",
                "ALTER TABLE chat_messages ADD COLUMN resolved_at TEXT DEFAULT ''",
                # read receipts (Nov 2026): the newest message that reached each member; opt-out like WhatsApp
                "ALTER TABLE chat_members ADD COLUMN delivered_id INTEGER DEFAULT 0",
                "ALTER TABLE chat_prefs ADD COLUMN receipts_off INTEGER DEFAULT 0"]:
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
        d['broadcast'] = r.get('broadcast') or 0
        d['resolved_by'] = r.get('resolved_by') or 0
        d['resolved_at'] = r.get('resolved_at') or ''
        if r['reply_count'] and not r['parent_id']:
            d['participants'] = [x['user_id'] for x in conn.execute(
                'SELECT user_id, MAX(id) m FROM chat_messages WHERE parent_id=? AND deleted=0 GROUP BY user_id ORDER BY m DESC LIMIT 4',
                (r['id'],))]
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


def _receipts_off(conn, cid):
    """People in this agency who turned read receipts off."""
    return {r['user_id'] for r in conn.execute('SELECT user_id FROM chat_prefs WHERE company_id=? AND receipts_off=1', (cid,))}


def _receipts(conn, cid, uid, chid, off=None):
    """{other member: [read up to, delivered up to]} for ticks on my own messages.
    Like WhatsApp: someone with receipts off never shows blue (only delivered), and if I turned
    mine off I do not see anyone's blue ticks either."""
    off = _receipts_off(conn, cid) if off is None else off
    hide_all = uid in off
    out = {}
    for r in conn.execute('SELECT user_id, last_read_id, delivered_id FROM chat_members WHERE channel_id=? AND user_id!=?',
                          (chid, uid)).fetchall():
        rd = r['last_read_id'] or 0
        dl = max(r['delivered_id'] or 0, rd)
        out[str(r['user_id'])] = [0 if (hide_all or r['user_id'] in off) else rd, dl]
    return out


def _thread_receipts(conn, cid, uid, root, off=None):
    off = _receipts_off(conn, cid) if off is None else off
    if uid in off:
        return {}
    return {str(r['user_id']): r['last_read_id'] or 0 for r in conn.execute(
        'SELECT user_id, last_read_id FROM chat_thread_follows WHERE message_id=? AND user_id!=? AND muted=0', (root, uid))
        if r['user_id'] not in off}


def _delivered(conn, cid, uid):
    """Polling means this person's app has every message so far: move their 'delivered' marker."""
    conn.execute('UPDATE chat_members SET delivered_id=(SELECT COALESCE(MAX(id),0) FROM chat_messages m '
                 'WHERE m.channel_id=chat_members.channel_id) WHERE user_id=? AND company_id=?', (uid, cid))
    conn.commit()


def _channel_summaries(conn, cid, uid):
    """The caller's channels (member) with unread / mention counts."""
    rows = conn.execute(
        'SELECT c.*, m.last_read_id, m.muted FROM chat_channels c JOIN chat_members m ON m.channel_id=c.id '
        'WHERE c.company_id=? AND m.user_id=? ORDER BY c.kind, c.name COLLATE NOCASE', (cid, uid)).fetchall()
    out = []
    tag = '<@%d>' % uid
    off = _receipts_off(conn, cid)
    for r in rows:
        d = dict(r)
        un = conn.execute('SELECT COUNT(*) n, COALESCE(MAX(id),0) mx FROM chat_messages WHERE channel_id=? AND (parent_id=0 OR broadcast=1) '
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
        if d['kind'] in ('dm', 'ai'):
            item['members'] = _member_ids(conn, d['id'])
        if d['kind'] != 'ai':
            item['rcpt'] = _receipts(conn, cid, uid, d['id'], off)
        out.append(item)
    return out


def _notify(conn, cid, uid, ch, msg_id, body, parent_id):
    """@mentions and DMs go to the HireLab bell too (unless the person has the chat open)."""
    if uid == BOT_ID or ch['kind'] == 'ai':
        return
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
        line = _plain(conn, cid, body, refs=False)[:160] or '(file)'
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


def _plain(conn, cid, body, refs=True):
    """Readable text: <@12> -> @Name, <c:5> -> the candidate's name (if this user may see it).
    refs=False (bell notifications, read by someone else): just "[candidate]" / "[job]"."""
    names = {u['id']: u['name'] for u in _users(conn, cid)}
    names[BOT_ID] = 'HireLab AI'
    s = _MENTION.sub(lambda m: '@' + names.get(int(m.group(1)), 'someone'), body or '')
    if not refs:
        return _REF.sub(lambda m: '[candidate]' if m.group(1) == 'c' else '[job]', s).strip()
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


@pages.route('/chat-dock.js')
def chat_dock_js():
    """The small chat windows at the bottom right of every ATS page (chat-dock.js)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'chat-dock.js')
    with open(path, encoding='utf-8') as f:
        resp = Response(f.read(), mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'no-cache'
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
        _ensure_ai(conn, cid, uid)
        _housekeeping(conn, cid, uid)
        _delivered(conn, cid, uid)
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
                        'ai': bool(_ai_key()), 'statuses': _statuses(conn, cid), 'presets': STATUS_PRESETS,
                        'stages': _stages(), 'digest_off': _prefs(conn, uid, cid).get('digest_off', 0),
                        'receipts_off': _prefs(conn, uid, cid).get('receipts_off') or 0,
                        'scheduled': _my_scheduled(conn, cid, uid), 'threads': _thread_unread(conn, uid)})
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
        _housekeeping(conn, cid, uid)
        _delivered(conn, cid, uid)
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
                    'online': online, 'rev': _last_rev[0], 'statuses': _statuses(conn, cid),
                    'scheduled': _my_scheduled(conn, cid, uid), 'threads': _thread_unread(conn, uid)})
        try:
            troot = int(request.args.get('thread') or 0)
        except ValueError:
            troot = 0
        if troot:
            out['thread_rcpt'] = {'root': troot, 'reads': _thread_receipts(conn, cid, uid, troot)}
        return jsonify(out)
    finally:
        conn.close()




# ══════════════════════════════════════════════════════════════════════════
#  STATUS, STAGES, PREFS
# ══════════════════════════════════════════════════════════════════════════
def _prefs(conn, uid, cid):
    r = conn.execute('SELECT * FROM chat_prefs WHERE user_id=?', (uid,)).fetchone()
    if not r:
        conn.execute('INSERT OR IGNORE INTO chat_prefs (user_id, company_id) VALUES (?,?)', (uid, cid))
        conn.commit()
        return {'user_id': uid, 'company_id': cid, 'status_emoji': '', 'status_text': '', 'status_until': '',
                'digest_date': '', 'digest_off': 0, 'receipts_off': 0}
    return dict(r)


def _statuses(conn, cid):
    now = ts()
    out = {}
    for r in conn.execute("SELECT user_id, status_emoji, status_text, status_until FROM chat_prefs WHERE company_id=? "
                          "AND (status_emoji!='' OR status_text!='')", (cid,)):
        if r['status_until'] and r['status_until'] <= now:
            continue
        out[r['user_id']] = {'emoji': r['status_emoji'], 'text': r['status_text'], 'until': r['status_until']}
    return out


def _stages():
    try:
        raw = _core().get_setting('pipeline_stages')
        lst = json.loads(raw) if raw else None
        if isinstance(lst, list) and lst:
            st = [(x.get('id') if isinstance(x, dict) else str(x)) for x in lst]
            st = [x for x in st if x and x not in ('Placed', 'Joined')]
            if st:
                return st + ['Placed', 'Joined']
    except Exception:
        pass
    return DEFAULT_STAGES + ['Placed', 'Joined']


@bp.route('/status', methods=['POST'])
@login_required
def set_status():
    uid, cid = _me()
    d = request.get_json(silent=True) or {}
    emoji = str(d.get('emoji') or '')[:16]
    text = str(d.get('text') or '').strip()[:80]
    until = ''
    if d.get('until'):
        until = to_ist(d.get('until'), d.get('tz'))
        if not until:
            return _err('Pick when the status should clear.')
    conn = get_db()
    try:
        _prefs(conn, uid, cid)
        conn.execute('UPDATE chat_prefs SET status_emoji=?, status_text=?, status_until=?, company_id=? WHERE user_id=?',
                     (emoji, text, until if (emoji or text) else '', cid, uid))
        conn.commit()
        return jsonify({'ok': True, 'statuses': _statuses(conn, cid)})
    finally:
        conn.close()


@bp.route('/prefs', methods=['POST'])
@login_required
def set_prefs():
    uid, cid = _me()
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        _prefs(conn, uid, cid)
        if 'digest_off' in d:
            conn.execute('UPDATE chat_prefs SET digest_off=? WHERE user_id=?', (1 if d.get('digest_off') else 0, uid))
        if 'receipts_off' in d:
            conn.execute('UPDATE chat_prefs SET receipts_off=? WHERE user_id=?', (1 if d.get('receipts_off') else 0, uid))
        conn.commit()
        pr = _prefs(conn, uid, cid)
        return jsonify({'ok': True, 'digest_off': pr['digest_off'], 'receipts_off': pr.get('receipts_off') or 0})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  HIRELAB AI
# ══════════════════════════════════════════════════════════════════════════
AI_WELCOME = ("👋 Hi! I am **HireLab AI**. Ask me about your work in HireLab - I read the ATS with your access.\n"
              "Try: *aaj kitne interviews hain?*, *which offers are pending?*, *Asha Verma ka status kya hai?*\n"
              "In any channel or DM, write *@AI* and your question - I answer in a thread.\n"
              "Every morning I also post a short summary here: what happened yesterday and what is pending today.")


def _ensure_ai(conn, cid, uid):
    """Everyone has a private 'HireLab AI' chat (kind 'ai')."""
    key = 'ai:%d' % uid
    r = conn.execute("SELECT * FROM chat_channels WHERE company_id=? AND kind='ai' AND dm_key=?", (cid, key)).fetchone()
    if r:
        return dict(r)
    chid = conn.execute("INSERT INTO chat_channels (company_id, kind, name, dm_key, is_private, created_by, created_at) "
                        "VALUES (?, 'ai', 'HireLab AI', ?, 1, ?, ?)", (cid, key, uid, ts())).lastrowid
    conn.execute('INSERT OR IGNORE INTO chat_members (channel_id, company_id, user_id, joined_at) VALUES (?,?,?,?)',
                 (chid, cid, uid, ts()))
    conn.execute('INSERT INTO chat_messages (company_id, channel_id, user_id, body, created_at, rev) VALUES (?,?,?,?,?,?)',
                 (cid, chid, BOT_ID, AI_WELCOME, ts(), next_rev()))
    conn.commit()
    return _channel(conn, chid, cid)


def _ai_rate_ok(uid, limit=30, window=600):
    now = time.time()
    lst = [t for t in _ai_calls.get(uid, []) if now - t < window]
    if len(lst) >= limit:
        _ai_calls[uid] = lst
        return False
    lst.append(now)
    _ai_calls[uid] = lst
    return True


def _maybe_ai(conn, ch, uid, cid, mid, body, parent_id):
    """Answer when the message is for HireLab AI: anything in your AI chat, or '@AI ...' elsewhere."""
    if ch['kind'] != 'ai' and not _AI_RX.search(body or ''):
        return
    question = _AI_RX.sub(' ', body or '').strip()
    names = {u['id']: u['name'] for u in _users(conn, cid)}
    question = _MENTION.sub(lambda m: '@' + names.get(int(m.group(1)), 'someone'), question)
    if not question:
        question = 'Give me a quick update on my work today.'
    reply_to = 0 if ch['kind'] == 'ai' else (parent_id or mid)
    # earlier turns: the AI chat's last messages, or the thread so far
    if ch['kind'] == 'ai':
        rows = conn.execute('SELECT user_id, body FROM chat_messages WHERE channel_id=? AND id<? AND deleted=0 '
                            'ORDER BY id DESC LIMIT 6', (ch['id'], mid)).fetchall()[::-1]
    else:
        root = reply_to
        rows = conn.execute('SELECT user_id, body FROM chat_messages WHERE (id=? OR parent_id=?) AND id<? AND deleted=0 '
                            'ORDER BY id LIMIT 12', (root, root, mid)).fetchall()
        rows = [r for r in rows if r['user_id'] == BOT_ID or _AI_RX.search(r['body'] or '')]
    history = [('assistant' if r['user_id'] == BOT_ID else 'user', r['body'] or '') for r in rows]
    if not _ai_rate_ok(uid):
        _bot_say(conn, ch, cid, 'You have asked a lot in the last 10 minutes - give me a minute and ask again.', reply_to)
        return
    core = _core()

    def work():
        c2 = get_db()
        try:
            _typing.setdefault(ch['id'], {})[BOT_ID] = time.time() + 60
            from modules import chat_ai
            text = chat_ai.answer(core, c2, cid, uid, question, history)
            _bot_say(c2, ch, cid, text, reply_to)
        except Exception as exc:
            print('[team-chat] AI answer failed: %s: %s' % (type(exc).__name__, exc))
            try:
                _bot_say(c2, ch, cid, 'Sorry, something went wrong while I was looking that up.', reply_to)
            except Exception:
                pass
        finally:
            _typing.get(ch['id'], {}).pop(BOT_ID, None)
            c2.close()
    if AI_SYNC:
        work()
        return
    try:
        from flask import copy_current_request_context, has_request_context
        job = copy_current_request_context(work) if has_request_context() else work
    except Exception:
        job = work
    threading.Thread(target=job, daemon=True).start()


def _bot_say(conn, ch, cid, text, parent_id=0):
    msg, _e = _post(conn, ch, BOT_ID, cid, _clean_body(text), parent_id)
    return msg


# ══════════════════════════════════════════════════════════════════════════
#  MORNING SUMMARY + SCHEDULED MESSAGES (both also run from the background loop)
# ══════════════════════════════════════════════════════════════════════════
def to_ist(local, tz_offset=None):
    """A browser time 'YYYY-MM-DDTHH:MM' + its getTimezoneOffset() (minutes, UTC - local)
    -> the same moment in IST ('YYYY-MM-DDTHH:MM'), which is how the server stores times."""
    import datetime
    local = str(local or '').strip().replace(' ', 'T')[:16]
    if not re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$', local):
        return ''
    try:
        off = int(tz_offset)
    except (TypeError, ValueError):
        return local                                  # no zone given: already IST
    if not -900 <= off <= 900:
        return local
    d = datetime.datetime.fromisoformat(local) + datetime.timedelta(minutes=off + 330)
    return d.isoformat()[:16]


def _ist():
    try:
        return _core()._ist_now()
    except Exception:
        import datetime
        return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)


def maybe_digest(conn, cid, uid):
    now = _ist()
    if (now.hour, now.minute) < DIGEST_AT:
        return False
    p = _prefs(conn, uid, cid)
    today = now.date().isoformat()
    if p.get('digest_off') or p.get('digest_date') == today:
        return False
    cur = conn.execute("UPDATE chat_prefs SET digest_date=? WHERE user_id=? AND COALESCE(digest_date,'')!=?", (today, uid, today))
    conn.commit()
    if not getattr(cur, 'rowcount', 0):
        return False                                   # another request is sending it
    try:
        from modules import chat_ai
        text = chat_ai.digest(_core(), conn, cid, uid)
        _bot_say(conn, _ensure_ai(conn, cid, uid), cid, text, 0)
        return True
    except Exception as exc:
        print('[team-chat] morning summary failed for %s: %s: %s' % (uid, type(exc).__name__, exc))
        return False


def run_due(conn, cid=None):
    """Send the scheduled messages whose time has come. Returns how many."""
    now = ts()
    sql = "SELECT * FROM chat_scheduled WHERE status='pending' AND send_at<=?" + (' AND company_id=?' if cid else '') + ' ORDER BY send_at LIMIT 50'
    rows = conn.execute(sql, (now, cid) if cid else (now,)).fetchall()
    n = 0
    for r in rows:
        cur = conn.execute("UPDATE chat_scheduled SET status='sending' WHERE id=? AND status='pending'", (r['id'],))
        conn.commit()
        if not getattr(cur, 'rowcount', 0):
            continue
        ch = _channel(conn, r['channel_id'], r['company_id'])
        if not ch or ch['archived'] or not _is_member(conn, ch['id'], r['user_id']):
            conn.execute("UPDATE chat_scheduled SET status='failed', error=? WHERE id=?",
                         ('The channel is archived or you are no longer in it.', r['id']))
            conn.commit()
            continue
        msg, e = _post(conn, ch, r['user_id'], r['company_id'], r['body'], r['parent_id'] or 0)
        conn.execute('UPDATE chat_scheduled SET status=?, message_id=?, error=? WHERE id=?',
                     ('failed' if e else 'sent', (msg or {}).get('id', 0), e or '', r['id']))
        conn.commit()
        n += 0 if e else 1
    return n


def run_background(conn):
    """Called every minute by server.py's background loop: scheduled messages only.
    The morning summary is made when the person first opens HireLab after 08:30
    (every ATS page polls the chat), so nobody's rows change while they are away
    and an account that is never used gets no summaries."""
    try:
        run_due(conn)
    except Exception as exc:
        print('[team-chat] scheduled messages: %s' % exc)


def _housekeeping(conn, cid, uid):
    """On each page load / poll: send due scheduled messages + today's summary (fallback for the loop)."""
    try:
        run_due(conn, cid)
        maybe_digest(conn, cid, uid)
    except Exception as exc:
        print('[team-chat] housekeeping: %s' % exc)


def _my_scheduled(conn, cid, uid):
    rows = conn.execute("SELECT id, channel_id, parent_id, body, send_at, status, error FROM chat_scheduled WHERE company_id=? "
                        "AND user_id=? AND (status='pending' OR (status='failed' AND send_at>=?)) ORDER BY send_at LIMIT 50",
                        (cid, uid, ts()[:10])).fetchall()
    return [dict(r) for r in rows]


@bp.route('/channels/<int:chid>/schedule', methods=['POST'])
@login_required
def schedule(chid):
    uid, cid = _me()
    d = request.get_json(silent=True) or {}
    body = _clean_body(d.get('body'))
    when = to_ist(d.get('send_at'), d.get('tz'))
    if not body:
        return _err('Write a message first.')
    if not re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$', when):
        return _err('Pick a date and time.')
    now = ts()[:16]
    if when <= now:
        return _err('Pick a time in the future.')
    import datetime
    if when > (_ist() + datetime.timedelta(days=60)).isoformat()[:16]:
        return _err('You can schedule up to 60 days ahead.')
    conn = get_db()
    try:
        ch = _channel(conn, chid, cid)
        if not _can_read(conn, ch, uid):
            return _err('Not found', 404)
        if not _can_post(conn, ch, uid):
            return _err('Join the channel to post here.', 403)
        try:
            parent = int(d.get('parent_id') or 0)
        except (TypeError, ValueError):
            parent = 0
        sid = conn.execute('INSERT INTO chat_scheduled (company_id, channel_id, user_id, parent_id, body, send_at, created_at) '
                           'VALUES (?,?,?,?,?,?,?)', (cid, chid, uid, parent, body, when + ':00', ts())).lastrowid
        conn.commit()
        return jsonify({'ok': True, 'id': sid, 'scheduled': _my_scheduled(conn, cid, uid)})
    finally:
        conn.close()


@bp.route('/scheduled/<int:sid>', methods=['DELETE', 'PATCH'])
@login_required
def scheduled_edit(sid):
    uid, cid = _me()
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM chat_scheduled WHERE id=? AND company_id=? AND user_id=?", (sid, cid, uid)).fetchone()
        if not r:
            return _err('Not found', 404)
        if request.method == 'DELETE':
            conn.execute("UPDATE chat_scheduled SET status='cancelled' WHERE id=? AND status IN ('pending','failed')", (sid,))
        else:
            d = request.get_json(silent=True) or {}
            if r['status'] != 'pending':
                return _err('This message was already sent.')
            if 'send_now' in d:
                conn.execute("UPDATE chat_scheduled SET send_at=? WHERE id=?", (ts(), sid))
            if 'body' in d:
                b = _clean_body(d.get('body'))
                if not b:
                    return _err('Write a message first.')
                conn.execute('UPDATE chat_scheduled SET body=? WHERE id=?', (b, sid))
        conn.commit()
        run_due(conn, cid)
        return jsonify({'ok': True, 'scheduled': _my_scheduled(conn, cid, uid)})
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
            around = int(request.args.get('around') or 0)
        except ValueError:
            before = around = 0
        sql = 'SELECT * FROM chat_messages WHERE channel_id=? AND (parent_id=0 OR broadcast=1)'
        newer = False
        if around:
            # a page centred on one message (search results, links to old messages)
            half = PAGE // 2
            old = conn.execute(sql + ' AND id<=? ORDER BY id DESC LIMIT ?', [chid, around, half + 1]).fetchall()
            new = conn.execute(sql + ' AND id>? ORDER BY id ASC LIMIT ?', [chid, around, half + 1]).fetchall()
            more = len(old) > half
            newer = len(new) > half
            rows = list(reversed(old[:half])) + list(new[:half])
        else:
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
        out.update({'ok': True, 'more': more, 'newer': newer, 'pins': pins, 'member': _is_member(conn, chid, uid),
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
        if p['parent_id']:
            return thread(p['parent_id'])
        rows = conn.execute('SELECT * FROM chat_messages WHERE parent_id=? ORDER BY id', (mid,)).fetchall()
        out = _with_refs(conn, cid, _ser([p] + list(rows), conn))
        f = conn.execute('SELECT last_read_id, muted FROM chat_thread_follows WHERE message_id=? AND user_id=?', (mid, uid)).fetchone()
        out.update({'ok': True, 'following': bool(f and not f['muted']), 'last_read_id': f['last_read_id'] if f else 0,
                    'reads': _thread_receipts(conn, cid, uid, mid)})
        if f and rows:                      # opening a thread reads it
            conn.execute('UPDATE chat_thread_follows SET last_read_id=MAX(last_read_id, ?) WHERE message_id=? AND user_id=?',
                         (rows[-1]['id'], mid, uid))
            conn.commit()
        return jsonify(out)
    finally:
        conn.close()


def _post(conn, ch, uid, cid, body, parent_id, file_info=None, broadcast=False):
    if parent_id:
        p = conn.execute('SELECT id, parent_id FROM chat_messages WHERE id=? AND channel_id=?', (parent_id, ch['id'])).fetchone()
        if not p:
            return None, 'That message is gone.'
        if p['parent_id']:
            parent_id = p['parent_id']           # a reply to a reply stays in the same thread
    fi = file_info or {}
    cur = conn.execute('INSERT INTO chat_messages (company_id, channel_id, user_id, parent_id, body, created_at, '
                       'file_name, file_path, file_size, file_type, rev, broadcast) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (cid, ch['id'], uid, parent_id or 0, body, ts(), fi.get('name', ''), fi.get('path', ''),
                        fi.get('size', 0), fi.get('type', ''), next_rev(), 1 if (broadcast and parent_id) else 0))
    mid = cur.lastrowid
    if parent_id:
        conn.execute('UPDATE chat_messages SET reply_count=(SELECT COUNT(*) FROM chat_messages WHERE parent_id=? AND deleted=0), '
                     'last_reply_at=?, rev=? WHERE id=?', (parent_id, ts(), next_rev(), parent_id))
        if uid != BOT_ID:
            _thread_reply(conn, ch, cid, uid, parent_id, mid, body)
    else:
        conn.execute('UPDATE chat_members SET last_read_id=MAX(COALESCE(last_read_id,0), ?) WHERE channel_id=? AND user_id=?',
                     (mid, ch['id'], uid))
    _typing.get(ch['id'], {}).pop(uid, None)
    _notify(conn, cid, uid, ch, mid, body, parent_id)
    conn.commit()
    row = conn.execute('SELECT * FROM chat_messages WHERE id=?', (mid,)).fetchone()
    if uid != BOT_ID:
        _maybe_ai(conn, ch, uid, cid, mid, body, parent_id)
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
        msg, e = _post(conn, ch, uid, cid, body, parent, broadcast=bool(d.get('broadcast')))
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
                       {'name': name, 'path': rel, 'size': size, 'type': (f.mimetype or '')[:80]},
                       broadcast=request.form.get('broadcast') in ('1', 'true'))
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
# ══════════════════════════════════════════════════════════════════════════
#  THREADS: follow, unread, notifications
# ══════════════════════════════════════════════════════════════════════════
def _follow(conn, root, cid, uid, read_to=None, muted=None):
    """Make sure uid follows thread `root`; optionally move their read marker / mute flag."""
    conn.execute('INSERT OR IGNORE INTO chat_thread_follows (message_id, company_id, user_id, last_read_id, muted, created_at) '
                 'VALUES (?,?,?,0,0,?)', (root, cid, uid, ts()))
    if read_to is not None:
        conn.execute('UPDATE chat_thread_follows SET last_read_id=MAX(last_read_id, ?) WHERE message_id=? AND user_id=?',
                     (read_to, root, uid))
    if muted is not None:
        conn.execute('UPDATE chat_thread_follows SET muted=? WHERE message_id=? AND user_id=?', (1 if muted else 0, root, uid))


def _thread_reply(conn, ch, cid, uid, root, mid, body):
    """A reply was posted: the root's author, everyone who replied and everyone @mentioned follow the thread;
    followers who are not looking at the chat get one (merged) bell notification per thread."""
    try:
        p = conn.execute('SELECT user_id FROM chat_messages WHERE id=?', (root,)).fetchone()
        if not p:
            return
        members = set(_member_ids(conn, ch['id']))
        open_ch = ch['kind'] == 'channel' and not ch['is_private']
        if p['user_id'] and p['user_id'] != BOT_ID and p['user_id'] != uid:
            _follow(conn, root, cid, p['user_id'])
        users = {u['id'] for u in _users(conn, cid)}
        mentioned = {int(x) for x in _MENTION.findall(body or '')}
        for m in mentioned:
            if m != uid and m in users and (m in members or open_ch):
                _follow(conn, root, cid, m)
        _follow(conn, root, cid, uid, read_to=mid, muted=False)
        if ch['kind'] == 'ai':
            return
        me = next((u['name'] for u in _users(conn, cid) if u['id'] == uid), 'Someone')
        where = 'your conversation with %s' % me if ch['kind'] == 'dm' else '#%s' % ch['name']
        title = 'New replies in a thread in %s' % where
        line = '%s: %s' % (me, _plain(conn, cid, body, refs=False)[:140] or '(file)')
        now = time.time()
        for f in conn.execute('SELECT user_id FROM chat_thread_follows WHERE message_id=? AND muted=0 AND user_id!=?',
                              (root, uid)).fetchall():
            t = f['user_id']
            if t in mentioned or now - _seen.get(t, 0) < 20 or not (t in members or open_ch):
                continue                      # mentions get their own bell; the chat page shows the rest
            ex = conn.execute("SELECT id, body FROM notifications WHERE user_id=? AND kind='chat_thread' AND title=? "
                              "AND acked_at='' ORDER BY id DESC LIMIT 1", (t, title)).fetchone()
            if ex:
                lines = [l for l in (ex['body'] or '').split('\n') if l][-5:] + [line]
                conn.execute("UPDATE notifications SET body=?, updated_at=?, read_at='', actor_id=?, actor_name=? WHERE id=?",
                             ('\n'.join(lines[-6:]), ts(), uid, me, ex['id']))
            else:
                conn.execute('INSERT INTO notifications (company_id,user_id,kind,title,body,candidate_id,mandate_id,'
                             "actor_id,actor_name,created_at,updated_at,read_at,acked_at) VALUES (?,?,'chat_thread',?,?,0,0,?,?,?,?,'','')",
                             (cid, t, title, line, uid, me, ts(), ts()))
    except Exception as e:
        print('[team-chat] thread follow failed: %s' % e)


def _thread_rows(conn, uid, cid, only_unread=False):
    rows = conn.execute(
        'SELECT f.message_id, f.last_read_id, f.muted, p.channel_id, '
        '(SELECT COUNT(*) FROM chat_messages r WHERE r.parent_id=f.message_id AND r.id>f.last_read_id '
        ' AND r.user_id!=f.user_id AND r.deleted=0) AS unread '
        'FROM chat_thread_follows f JOIN chat_messages p ON p.id=f.message_id '
        'WHERE f.user_id=? AND f.company_id=? AND p.deleted=0 ORDER BY p.last_reply_at DESC, p.id DESC LIMIT 200',
        (uid, cid)).fetchall()
    ok = set(_visible_channel_ids(conn, cid, uid))
    out = [dict(r) for r in rows if r['channel_id'] in ok]
    return [r for r in out if r['unread'] and not r['muted']] if only_unread else out


def _thread_unread(conn, uid, cid=None):
    if cid is None:
        cid = _me()[1]
    rows = _thread_rows(conn, uid, cid, only_unread=True)
    return {'unread': sum(r['unread'] for r in rows), 'threads': len(rows),
            'roots': {str(r['message_id']): r['unread'] for r in rows}}


def _root_for(conn, cid, uid, root):
    p = conn.execute('SELECT * FROM chat_messages WHERE id=? AND company_id=?', (root, cid)).fetchone()
    if not p:
        return None, None, 'Not found'
    if p['parent_id']:
        p = conn.execute('SELECT * FROM chat_messages WHERE id=?', (p['parent_id'],)).fetchone()
    ch = _channel(conn, p['channel_id'], cid)
    if not ch or not _can_read(conn, ch, uid):
        return None, None, 'Not found'
    return p, ch, None


@bp.route('/threads', methods=['GET'])
@login_required
def threads_list():
    """Threads I follow, newest activity first, with the latest replies of each."""
    uid, cid = _me()
    conn = get_db()
    try:
        rows = _thread_rows(conn, uid, cid)[:40]
        items, msgs = [], []
        for r in rows:
            p = conn.execute('SELECT * FROM chat_messages WHERE id=?', (r['message_id'],)).fetchone()
            last = conn.execute('SELECT * FROM chat_messages WHERE parent_id=? AND deleted=0 ORDER BY id DESC LIMIT 2',
                                (p['id'],)).fetchall()
            sp = _ser([p] + list(reversed(last)), conn)
            msgs += sp
            items.append({'root': p['id'], 'channel_id': p['channel_id'], 'unread': r['unread'], 'muted': r['muted'],
                          'last_read_id': r['last_read_id'], 'latest': [m['id'] for m in sp[1:]]})
        out = _with_refs(conn, cid, msgs)
        out.update({'ok': True, 'threads': items})
        return jsonify(out)
    finally:
        conn.close()


@bp.route('/threads/<int:root>/read', methods=['POST'])
@login_required
def thread_read(root):
    uid, cid = _me()
    conn = get_db()
    try:
        p, ch, e = _root_for(conn, cid, uid, root)
        if e:
            return _err(e, 404)
        last = conn.execute('SELECT COALESCE(MAX(id),0) m FROM chat_messages WHERE parent_id=?', (p['id'],)).fetchone()['m']
        conn.execute('UPDATE chat_thread_follows SET last_read_id=MAX(last_read_id, ?) WHERE message_id=? AND user_id=?',
                     (last, p['id'], uid))
        conn.commit()
        return jsonify({'ok': True, 'threads': _thread_unread(conn, uid, cid)})
    finally:
        conn.close()


@bp.route('/threads/<int:root>/follow', methods=['POST'])
@login_required
def thread_follow(root):
    uid, cid = _me()
    d = request.get_json(silent=True) or {}
    on = d.get('follow', True) not in (False, 0, '0', 'false')
    conn = get_db()
    try:
        p, ch, e = _root_for(conn, cid, uid, root)
        if e:
            return _err(e, 404)
        last = conn.execute('SELECT COALESCE(MAX(id),0) m FROM chat_messages WHERE parent_id=?', (p['id'],)).fetchone()['m']
        _follow(conn, p['id'], cid, uid, read_to=last, muted=not on)
        conn.commit()
        return jsonify({'ok': True, 'following': on, 'threads': _thread_unread(conn, uid, cid)})
    finally:
        conn.close()


@bp.route('/threads/<int:root>/resolve', methods=['POST'])
@login_required
def thread_resolve(root):
    """Mark a question/thread as done (or reopen it). Anyone who can post in the channel may do it."""
    uid, cid = _me()
    d = request.get_json(silent=True) or {}
    on = d.get('resolved', True) not in (False, 0, '0', 'false')
    conn = get_db()
    try:
        p, ch, e = _root_for(conn, cid, uid, root)
        if e:
            return _err(e, 404)
        if not _can_post(conn, ch, uid):
            return _err('Join the channel first', 403)
        conn.execute("UPDATE chat_messages SET resolved_by=?, resolved_at=?, rev=? WHERE id=?",
                     (uid if on else 0, ts() if on else '', next_rev(), p['id']))
        conn.commit()
        msg = _ser([conn.execute('SELECT * FROM chat_messages WHERE id=?', (p['id'],)).fetchone()], conn)[0]
        return jsonify({'ok': True, 'message': msg})
    finally:
        conn.close()


@bp.route('/threads/<int:root>/summary', methods=['POST'])
@login_required
def thread_summary(root):
    """AI: a few lines on what the thread decided, what is still open and who owns what."""
    uid, cid = _me()
    conn = get_db()
    try:
        p, ch, e = _root_for(conn, cid, uid, root)
        if e:
            return _err(e, 404)
        rows = [p] + list(conn.execute('SELECT * FROM chat_messages WHERE parent_id=? AND deleted=0 ORDER BY id',
                                       (p['id'],)).fetchall())
        names = {u['id']: u['name'] for u in _users(conn, cid)}
        names[BOT_ID] = 'HireLab AI'
        lines = []
        for r in rows[-80:]:
            t = _plain(conn, cid, r['body'])
            if r['file_name']:
                t += ' [file: %s]' % r['file_name']
            lines.append('%s (%s): %s' % (names.get(r['user_id'], 'Someone'), (r['created_at'] or '')[11:16], t[:600]))
    finally:
        conn.close()
    if len(rows) < 2:
        return jsonify({'ok': True, 'summary': 'Is thread mein abhi koi reply nahi hai.'})
    key = _ai_key()
    if not key:
        return _err('AI is not set up for this account', 400)
    payload = {'model': 'deepseek-chat', 'temperature': 0.2, 'max_tokens': 400, 'messages': [
        {'role': 'system', 'content': 'You summarise a recruiting team\'s chat thread. Reply in at most 5 short bullet '
                                      'points: what was asked, what was decided, what is still open, and who owns '
                                      'which next step (with names). Use the language the thread is written in '
                                      '(Hinglish is fine). No preamble.'},
        {'role': 'user', 'content': '\n'.join(lines)}]}
    try:
        resp = _core().call_deepseek(key, payload, timeout=60, endpoint='team-chat-thread')
        if resp.status_code != 200:
            raise RuntimeError('DeepSeek returned HTTP %s' % resp.status_code)
        txt = (resp.json()['choices'][0]['message']['content'] or '').strip()
    except Exception as exc:
        if type(exc).__name__ == 'TokenCapError':
            return _err('Your AI usage cap for this period is reached. Ask the admin to raise it.', 429)
        print('[team-chat] thread summary failed: %s: %s' % (type(exc).__name__, exc))
        return _err('The AI could not summarise right now. Try again in a moment.', 502)
    return jsonify({'ok': True, 'summary': txt or 'Summary nahi ban payi.'})


_SEARCH_TOKEN = re.compile(r'"([^"]+)"|(\S+)')


def parse_query(q):
    """'from:riya in:#sales has:file "notice period" python' -> filters + terms (phrases kept whole)."""
    f = {'from': [], 'in': [], 'has': set(), 'terms': []}
    for ph, w in _SEARCH_TOKEN.findall(q or ''):
        if ph:
            if ph.strip():
                f['terms'].append(ph.strip())
            continue
        lw = w.lower()
        for key in ('from:', 'in:', 'has:'):
            if lw.startswith(key) and len(w) > len(key):
                v = w[len(key):].lstrip('@#').lower()
                if key == 'has:':
                    f['has'].add(v)
                else:
                    f[key[:-1]].append(v)
                break
        else:
            f['terms'].append(w)
    f['terms'] = f['terms'][:8]
    return f


@bp.route('/search', methods=['GET'])
@login_required
def search():
    """Messages in channels I can read. Each word (or "quoted phrase") must match the text, a file name,
    an @person or a linked candidate / job. Filters: from:name  in:#channel  has:file|candidate|job|link."""
    uid, cid = _me()
    q = str(request.args.get('q') or '').strip()
    f = parse_query(q)
    empty = {'ok': True, 'messages': [], 'refs': {}, 'terms': f['terms']}
    if not f['terms'] and not (f['from'] or f['in'] or f['has']):
        return jsonify(empty)
    if f['terms'] and all(len(t) < 2 for t in f['terms']) and not (f['from'] or f['in'] or f['has']):
        return jsonify(empty)
    try:
        chan_only = int(request.args.get('channel') or 0)
    except ValueError:
        chan_only = 0
    conn = get_db()
    try:
        ids = _visible_channel_ids(conn, cid, uid)
        if chan_only:
            ids = [i for i in ids if i == chan_only]
        users = _users(conn, cid)
        if f['in']:
            want = []
            names = {u['id']: u['name'].lower() for u in users}
            for r in conn.execute('SELECT id, kind, name FROM chat_channels WHERE company_id=?', (cid,)).fetchall():
                if r['id'] not in ids:
                    continue
                label = (r['name'] or '').lower()
                if r['kind'] == 'dm':
                    label = ' '.join(names.get(m, '') for m in _member_ids(conn, r['id']) if m != uid)
                if r['kind'] == 'ai':
                    label = 'hirelab ai'
                if any(v in label for v in f['in']):
                    want.append(r['id'])
            ids = want
        if not ids:
            return jsonify(empty)
        qs = ','.join('?' * len(ids))
        sql = f'SELECT * FROM chat_messages WHERE company_id=? AND deleted=0 AND channel_id IN ({qs})'
        args = [cid] + ids
        if f['from']:
            hit = [u['id'] for u in users if any(v in u['name'].lower() for v in f['from'])]
            if any(v in ('ai', 'hirelab', 'bot') for v in f['from']):
                hit.append(BOT_ID)
            sql += ' AND user_id IN (%s)' % (','.join(str(int(i)) for i in hit) or '-1')
        for h in f['has']:
            if h in ('file', 'files', 'attachment'):
                sql += " AND file_name!=''"
            elif h in ('candidate', 'cand', 'cv'):
                sql += " AND body LIKE '%<c:%'"
            elif h in ('job', 'mandate', 'position'):
                sql += " AND body LIKE '%<m:%'"
            elif h in ('link', 'url'):
                sql += " AND (body LIKE '%http://%' OR body LIKE '%https://%')"
            elif h in ('reply', 'thread', 'replies'):
                sql += ' AND reply_count>0'
            elif h in ('pin', 'pinned'):
                sql += ' AND pinned=1'
        from modules import access
        sc, sp = access.candidate_scope_sql('c.mandate_id')
        mc, mp = access.mandate_scope_sql('m.id')
        for t in f['terms']:
            like = '%' + t.replace('%', '').replace('_', '') + '%'
            ors = ['body LIKE ?', 'file_name LIKE ?']
            oargs = [like, like]
            if len(t.lstrip('@')) >= 3:
                tl = t.lower().lstrip('@')
                for i in [u['id'] for u in users if tl in u['name'].lower()][:6]:
                    ors.append('instr(body, ?)>0')
                    oargs.append('<@%d>' % int(i))
                # candidates / jobs the caller may see, by name, company, role or client
                cids = [r['id'] for r in conn.execute(
                    'SELECT c.id FROM candidates c WHERE c.owner_id=? AND (c.name LIKE ? OR c.company LIKE ?)' + sc +
                    ' ORDER BY c.id DESC LIMIT 25', [cid, like, like] + sp).fetchall()]
                mids = [r['id'] for r in conn.execute(
                    'SELECT m.id FROM mandates m WHERE m.owner_id=? AND (m.role LIKE ? OR m.client LIKE ?)' + mc +
                    ' ORDER BY m.id DESC LIMIT 25', [cid, like, like] + mp).fetchall()]
                for i in cids:
                    ors.append('instr(body, ?)>0')
                    oargs.append('<c:%d>' % int(i))
                for i in mids:
                    ors.append('instr(body, ?)>0')
                    oargs.append('<m:%d>' % int(i))
            sql += ' AND (%s)' % ' OR '.join(ors)
            args += oargs
        rows = conn.execute(sql + ' ORDER BY id DESC LIMIT 80', args).fetchall()
        out = _with_refs(conn, cid, _ser(rows, conn))
        out.update({'ok': True, 'terms': f['terms']})
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
