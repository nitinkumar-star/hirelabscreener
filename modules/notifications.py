"""
RecruitOS — In-app Notifications (Wave 2 of the freelancer upgrade)

Two event streams, both stored in one `notifications` table:

  1. fl_upload  — a freelancer pushed a CV into a mandate.
                  Recipients: the mandate's assigned recruiter + every company
                  admin (deduped; the uploader never notifies themselves).

  2. fl_action  — a recruiter/admin did ANYTHING to a freelancer-sourced
                  candidate (stage, edit, comment, CV, interview, email, WA...).
                  Recipient: the freelancer who sourced the candidate.

How #2 catches "every action" without touching ~25 routes:
  a before/after request hook pair snapshots the candidate row, the last
  stage_history id and the last candidate_events id BEFORE any mutating
  /api/candidates/<id>... request, then diffs them AFTER a 2xx response.
  Anything that changed becomes one readable summary line. Routes that
  change nothing (AI drafts, previews) produce no notification.

Client feedback is NEVER put into a freelancer notification (spec: hidden
from freelancers). Billing routes are skipped for the same reason.

Rapid successive actions by the same person on the same candidate within
3 minutes are merged into one notification, so a recruiter saving a form
that fires 3 requests does not spam the freelancer with 3 pop-ups.

Additive only: one new table, no changes to existing tables.
"""

import re
import json
import datetime
from flask import Blueprint, request, jsonify, g

from modules.shared import (
    get_db, ts, current_user, effective_company_id, real_user_id,
    login_required,
)
from modules import register_migration

bp = Blueprint('notifications', __name__, url_prefix='/api')

FREELANCER_ROLE = 'freelancer_sourcer'
MERGE_WINDOW_MIN = 3
MAX_LINES = 8
DASH = '\u2014'


# ══════════════════════════════════════════════════════════════════════════
#  MIGRATION
# ══════════════════════════════════════════════════════════════════════════
@register_migration
def migrate(conn):
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL DEFAULT 0,
        user_id INTEGER NOT NULL,            -- recipient
        kind TEXT DEFAULT '',                -- fl_upload | fl_action
        title TEXT DEFAULT '',
        body TEXT DEFAULT '',                -- newline-separated summary lines
        candidate_id INTEGER DEFAULT 0,
        mandate_id INTEGER DEFAULT 0,
        actor_id INTEGER DEFAULT 0,
        actor_name TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        read_at TEXT DEFAULT '',             -- seen in the bell panel
        acked_at TEXT DEFAULT ''             -- pop-up dismissed ("Got it"/"Open")
    )''')
    for sql in [
        'CREATE INDEX IF NOT EXISTS idx_ntf_user ON notifications(user_id, acked_at)',
        'CREATE INDEX IF NOT EXISTS idx_ntf_user_created ON notifications(user_id, created_at)',
    ]:
        try:
            c.execute(sql)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
#  CORE WRITE HELPER
# ══════════════════════════════════════════════════════════════════════════
def _now_dt():
    try:
        return datetime.datetime.fromisoformat(ts())
    except Exception:
        return datetime.datetime.now()


def _create(conn, company_id, user_id, kind, title, lines, candidate_id=0,
            mandate_id=0, actor_id=0, actor_name='', merge=False):
    """Insert a notification, or (merge=True) fold the lines into a recent
    un-acknowledged one from the same actor about the same candidate."""
    lines = [l for l in (lines or []) if l]
    if not user_id or not lines:
        return
    now = ts()
    if merge and candidate_id:
        cutoff = (_now_dt() - datetime.timedelta(minutes=MERGE_WINDOW_MIN)).isoformat(timespec='seconds')
        ex = conn.execute(
            "SELECT id, body FROM notifications WHERE user_id=? AND kind=? AND candidate_id=? "
            "AND actor_id=? AND acked_at='' AND updated_at>=? ORDER BY id DESC LIMIT 1",
            (user_id, kind, candidate_id, actor_id, cutoff)).fetchone()
        if ex:
            old = [l for l in (ex['body'] or '').split('\n') if l]
            for l in lines:
                if l not in old:
                    old.append(l)
            conn.execute(
                "UPDATE notifications SET body=?, title=?, updated_at=?, read_at='' WHERE id=?",
                ('\n'.join(old[-MAX_LINES:]), title, now, ex['id']))
            return
    conn.execute(
        'INSERT INTO notifications (company_id,user_id,kind,title,body,candidate_id,mandate_id,'
        "actor_id,actor_name,created_at,updated_at,read_at,acked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'','')",
        (company_id, user_id, kind, title, '\n'.join(lines[:MAX_LINES]), candidate_id or 0,
         mandate_id or 0, actor_id or 0, actor_name or '', now, now))


def _fmt_num(v):
    try:
        f = float(v or 0)
    except Exception:
        return ''
    if not f:
        return ''
    return str(int(f)) if f == int(f) else str(round(f, 2))


def _pretty(v):
    """8.0 -> '8', '' / None / 0 -> dash, text stays text."""
    if v in (None, '', 0, 0.0):
        return DASH
    if isinstance(v, float):
        return _fmt_num(v) or DASH
    return str(v)


def _short(s, n=140):
    s = re.sub(r'<[^>]+>', ' ', str(s or ''))
    s = re.sub(r'\s+', ' ', s).strip()
    return (s[:n - 1] + '\u2026') if len(s) > n else s


# ══════════════════════════════════════════════════════════════════════════
#  STREAM 1 — freelancer uploaded a CV  → recruiter + admins
#  Called from server.py (extension push route) right after the insert commits.
# ══════════════════════════════════════════════════════════════════════════
def notify_freelancer_upload(candidate_id):
    try:
        conn = get_db()
        c = conn.execute(
            'SELECT c.*, m.role AS m_role, m.client AS m_client, m.location AS m_location, '
            'm.assigned_user_id AS m_assigned, m.owner_id AS m_owner '
            'FROM candidates c JOIN mandates m ON m.id=c.mandate_id WHERE c.id=?',
            (candidate_id,)).fetchone()
        if not c:
            conn.close(); return
        company_id = c['m_owner']
        actor = real_user_id()
        cu = current_user() or {}
        fl_name = cu.get('display_name') or cu.get('username') or 'A freelancer'

        recipients = set()
        if c['m_assigned']:
            recipients.add(int(c['m_assigned']))
        for r in conn.execute(
                "SELECT id FROM users WHERE company_id=? AND (role='admin' OR is_company_admin=1) "
                "AND COALESCE(status,'approved')='approved'", (company_id,)).fetchall():
            recipients.add(int(r['id']))
        recipients.discard(int(actor or 0))

        position = (c['m_role'] or '') + (' @ ' + c['m_client'] if c['m_client'] else '')
        title = f'{fl_name} uploaded a CV \u2014 {position}'
        prof = ' \u00b7 '.join(x for x in [
            c['designation'] or '', c['company'] or '',
            (_fmt_num(c['experience']) + ' yrs') if _fmt_num(c['experience']) else '',
        ] if x)
        money = ' \u00b7 '.join(x for x in [
            ('CTC ' + _fmt_num(c['ctc_current']) + ' L') if _fmt_num(c['ctc_current']) else '',
            ('Exp ' + _fmt_num(c['ctc_expected']) + ' L') if _fmt_num(c['ctc_expected']) else '',
            ('NP ' + str(c['notice_period']) + ' d') if c['notice_period'] else '',
            c['location'] or '',
        ] if x)
        lines = [
            'Candidate: ' + (c['name'] or ''),
            'Position: ' + position + (' (' + c['m_location'] + ')' if c['m_location'] else ''),
            prof,
            money,
            'Uploaded by: ' + fl_name,
        ]
        for uid in recipients:
            _create(conn, company_id, uid, 'fl_upload', title, lines,
                    candidate_id=candidate_id, mandate_id=c['mandate_id'],
                    actor_id=actor, actor_name=fl_name)
        conn.commit(); conn.close()
    except Exception as e:
        print(f'[notifications] upload notify failed: {e}')


# ══════════════════════════════════════════════════════════════════════════
#  STREAM 2 — recruiter action on a freelancer's candidate → freelancer
# ══════════════════════════════════════════════════════════════════════════
_CAND_PATH = re.compile(r'^/api/candidates/(\d+)(?:/([a-z0-9-]+))?/?$')
_WA_SUGG_PATH = re.compile(r'^/api/wa-suggestions/(\d+)/approve/?$')

# Routes that never notify: AI drafts/previews and commercial data.
_SKIP_SUFFIX = {'ai-compose', 'deep-analysis', 'wa-draft', 'email-agent', 'billing', 'journey'}

# Action label for routes whose effect is not visible in the candidate row.
_ROUTE_LABEL = {
    ('POST', 'interviews'): 'Interview scheduled',
    ('POST', 'offers'): 'Offer created',
    ('POST', 'hiring-decision'): 'Hiring decision recorded',
    ('POST', 'send-email'): 'Email sent to candidate',
    ('POST', 'interview-message'): 'Interview message sent to candidate',
    ('POST', 'request-update'): 'Profile update requested from candidate',
    ('POST', 'cv'): 'CV uploaded / replaced',
    ('DELETE', 'cv'): 'CV removed',
    ('POST', 'work-history'): 'Work history updated',
    ('POST', 'tags'): 'Tags updated',
    ('POST', 'rate'): 'Candidate rated',
    ('POST', 'analyse-call'): 'Call analysed',
    ('POST', 'wa-send-log'): 'WhatsApp message sent',
    ('POST', 'wa-queue-send'): 'WhatsApp message queued',
    ('POST', 'do-not-email'): 'Email preference changed',
}

# Candidate fields a freelancer may see changes of. client_feedback is
# deliberately absent (hidden from freelancers).
_FIELD_LABEL = [
    ('name', 'Name'), ('company', 'Company'), ('designation', 'Designation'),
    ('experience', 'Experience'), ('ctc_current', 'Current CTC'),
    ('ctc_expected', 'Expected CTC'), ('ctc_offered', 'Offered CTC'),
    ('offer_date', 'Offer date'), ('notice_period', 'Notice period'),
    ('location', 'Location'), ('preferred_location', 'Preferred location'),
    ('phone', 'Phone'), ('email', 'Email'), ('qualification', 'Qualification'),
    ('specialization', 'Specialization'), ('linkedin_url', 'LinkedIn URL'),
]
_TEXT_FIELDS = [('recruiter_feedback', 'Recruiter feedback'), ('general_comments', 'Comment')]
_QUIET_FIELDS = [('career_summary', 'Summary updated'), ('key_skills', 'Skills updated'),
                 ('secondary_skills', 'Secondary skills updated')]
# Journey notes the server writes itself that would leak hidden info / duplicate.
_NOTE_SKIP = ('client feedback updated', 'recruiter feedback updated')


def _is_freelancer_user(u):
    return bool(u) and u.get('role') == FREELANCER_ROLE


def _snapshot(conn, cid):
    row = conn.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
    if not row:
        return None
    sh = conn.execute('SELECT COALESCE(MAX(id),0) m FROM stage_history WHERE candidate_id=?',
                      (cid,)).fetchone()['m']
    try:
        ev = conn.execute('SELECT COALESCE(MAX(id),0) m FROM candidate_events WHERE candidate_id=?',
                          (cid,)).fetchone()['m']
    except Exception:
        ev = 0
    return {'row': dict(row), 'sh': sh, 'ev': ev}


@bp.before_app_request
def _ntf_before():
    try:
        if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return
        path = request.path or ''
        cid, suffix = None, ''
        m = _CAND_PATH.match(path)
        if m:
            cid, suffix = int(m.group(1)), (m.group(2) or '')
        else:
            m2 = _WA_SUGG_PATH.match(path)
            if not m2:
                return
            suffix = 'wa-suggestion'
        if suffix in _SKIP_SUFFIX:
            return
        u = current_user()
        if not u or _is_freelancer_user(u):
            return
        conn = get_db()
        try:
            if cid is None:
                s = conn.execute('SELECT candidate_id FROM wa_suggestions WHERE id=?',
                                 (int(m2.group(1)),)).fetchone()
                if not s:
                    return
                cid = int(s['candidate_id'])
            snap = _snapshot(conn, cid)
        finally:
            conn.close()
        if not snap or not int(snap['row'].get('sourced_by') or 0):
            return   # not a freelancer's candidate — nothing to track
        g._ntf = {'cid': cid, 'suffix': suffix, 'method': request.method, 'snap': snap}
    except Exception as e:
        print(f'[notifications] before-hook: {e}')


@bp.after_app_request
def _ntf_after(response):
    try:
        info = getattr(g, '_ntf', None)
        if not info or not (200 <= response.status_code < 300):
            return response
        g._ntf = None
        _build_action_notification(info)
    except Exception as e:
        print(f'[notifications] after-hook: {e}')
    return response


def _build_action_notification(info):
    cid, suffix, method, snap = info['cid'], info['suffix'], info['method'], info['snap']
    before = snap['row']
    fl_id = int(before.get('sourced_by') or 0)
    actor = int(real_user_id() or 0)
    if not fl_id or fl_id == actor:
        return
    cu = current_user() or {}
    actor_name = cu.get('display_name') or cu.get('username') or 'Recruiter'

    conn = get_db()
    try:
        fl = conn.execute('SELECT status FROM users WHERE id=? AND role=?',
                          (fl_id, FREELANCER_ROLE)).fetchone()
        if not fl or (fl['status'] or 'approved') != 'approved':
            return

        lines = []
        after_row = conn.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()

        if method == 'DELETE' and not suffix:
            if after_row is None:
                lines.append('Candidate removed from the pipeline')
        elif after_row is not None:
            after = dict(after_row)
            # 1) Stage changes / journey notes (stage_history)
            for h in conn.execute(
                    'SELECT from_stage, to_stage, note FROM stage_history WHERE candidate_id=? '
                    'AND id>? ORDER BY id', (cid, snap['sh'])).fetchall():
                frm, to, note = (h['from_stage'] or ''), (h['to_stage'] or ''), _short(h['note'], 100)
                if to and frm != to:
                    lines.append(f'Stage: {frm or DASH} \u2192 {to}' + (f' ({note})' if note else ''))
                elif note:
                    lines.append(note)
            # 2) Mandate move
            if before.get('mandate_id') != after.get('mandate_id'):
                mm = conn.execute('SELECT role, client FROM mandates WHERE id=?',
                                  (after.get('mandate_id'),)).fetchone()
                if mm:
                    lines.append(f'Moved to mandate: {mm["role"]} @ {mm["client"]}')
            # 3) Field edits
            for f, lbl in _FIELD_LABEL:
                if f in before and str(before.get(f) or '') != str(after.get(f) or ''):
                    lines.append(f'{lbl}: {_pretty(before.get(f))} \u2192 {_pretty(after.get(f))}')
            for f, lbl in _TEXT_FIELDS:
                if f in before and (before.get(f) or '') != (after.get(f) or '') and after.get(f):
                    lines.append(f'{lbl}: {_short(after.get(f))}')
            for f, lbl in _QUIET_FIELDS:
                if f in before and (before.get(f) or '') != (after.get(f) or ''):
                    lines.append(lbl)
            if (before.get('wa_response') or '') != (after.get('wa_response') or '') and after.get('wa_response'):
                lines.append('Response logged: ' + str(after.get('wa_response')).replace('_', ' '))
            # 4) Journey events (comments, calls, emails) written by this request
            try:
                for e in conn.execute(
                        'SELECT event_type, detail FROM candidate_events WHERE candidate_id=? '
                        'AND id>? ORDER BY id', (cid, snap['ev'])).fetchall():
                    det = (e['detail'] or '').strip()
                    if e['event_type'] == 'edit':
                        continue     # already covered field-by-field (and may mention client feedback)
                    if not det or det.lower().startswith(_NOTE_SKIP):
                        continue
                    if 'client feedback' in det.lower():
                        continue
                    prefix = 'Comment: ' if e['event_type'] == 'note' else ''
                    lines.append(prefix + _short(det))
            except Exception:
                pass

        # 5) Route-level label (interview, email, CV...) when nothing more specific was found
        lbl = _ROUTE_LABEL.get((method, suffix))
        if lbl and lbl not in lines:
            lines.insert(0, lbl)
        if suffix == 'wa-suggestion' and not lines:
            lines.append('WhatsApp follow-up approved')

        # de-dup, keep order
        seen, uniq = set(), []
        for l in lines:
            if l and l not in seen:
                seen.add(l); uniq.append(l)
        if not uniq:
            return

        src = after_row if after_row is not None else before
        mm = conn.execute('SELECT role, client FROM mandates WHERE id=?', (src['mandate_id'],)).fetchone()
        position = (mm['role'] + ' @ ' + mm['client']) if mm else ''
        cand_name = src['name'] or 'your candidate'
        title = f'{actor_name} updated {cand_name}' + (f' \u2014 {position}' if position else '')
        _create(conn, effective_company_id(), fl_id, 'fl_action', title, uniq,
                candidate_id=cid, mandate_id=src['mandate_id'], actor_id=actor,
                actor_name=actor_name, merge=True)
        conn.commit()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  READ / ACK ENDPOINTS (used by the pop-up + bell, for every role)
# ══════════════════════════════════════════════════════════════════════════
def _rows(conn, sql, params):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


@bp.route('/notifications/poll', methods=['GET'])
@login_required
def ntf_poll():
    """Pop-up feed: every un-acknowledged notification (newest first) + unread count."""
    uid = real_user_id()
    conn = get_db()
    pending = _rows(conn,
                    "SELECT * FROM notifications WHERE user_id=? AND acked_at='' "
                    "ORDER BY updated_at DESC, id DESC LIMIT 30", (uid,))
    unread = conn.execute("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND read_at=''",
                          (uid,)).fetchone()['n']
    conn.close()
    return jsonify({'ok': True, 'pending': pending, 'unread': unread})


@bp.route('/notifications', methods=['GET'])
@login_required
def ntf_list():
    uid = real_user_id()
    conn = get_db()
    items = _rows(conn, 'SELECT * FROM notifications WHERE user_id=? '
                        'ORDER BY updated_at DESC, id DESC LIMIT 60', (uid,))
    conn.close()
    return jsonify({'ok': True, 'notifications': items})


@bp.route('/notifications/ack', methods=['POST'])
@login_required
def ntf_ack():
    """Dismiss pop-ups. Body: {ids:[...]} or {all:true}. Also marks them read."""
    d = request.json or {}
    uid = real_user_id()
    now = ts()
    conn = get_db()
    if d.get('all'):
        conn.execute("UPDATE notifications SET acked_at=?, read_at=CASE WHEN read_at='' THEN ? ELSE read_at END "
                     "WHERE user_id=? AND acked_at=''", (now, now, uid))
    else:
        ids = [int(x) for x in (d.get('ids') or []) if str(x).isdigit()]
        for i in ids:
            conn.execute("UPDATE notifications SET acked_at=?, read_at=CASE WHEN read_at='' THEN ? ELSE read_at END "
                         "WHERE id=? AND user_id=?", (now, now, i, uid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@bp.route('/notifications/read-all', methods=['POST'])
@login_required
def ntf_read_all():
    uid = real_user_id()
    now = ts()
    conn = get_db()
    conn.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at=''", (now, uid))
    conn.execute("UPDATE notifications SET acked_at=? WHERE user_id=? AND acked_at=''", (now, uid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})
