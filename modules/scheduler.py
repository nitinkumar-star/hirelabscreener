"""
RecruitOS — Scheduler Module (Calendly-style self-booking)

Turns the ATS into a self-serve meeting scheduler:

  1. Each RECRUITER (user) publishes their own weekly availability and gets a
     stable public link  ->  /book/<token>
  2. A candidate OR a client opens that link, sees only OPEN slots, and books
     one. No login. Confirmed instantly.
  3. The booking lands in a new `meetings` table and AUTO-LINKS to the matching
     candidate (by phone/email inside the same tenant) or CRM client-contact.
  4. Guest gets an email confirmation with reschedule/cancel + Add-to-Calendar
     links. Recruiter gets a one-tap WhatsApp button (built in the UI wave) and
     the booking shows up in Tasks.

Design contract (same as every module in this package):
  - Exposes a Blueprint `bp`.
  - Talks to the core ONLY through modules.shared (no circular import).
  - Registers its own tables via @register_migration — purely ADDITIVE, it
    never alters existing tables (candidates / interviews stay untouched).
  - Public routes simply omit @login_required; there is no global auth gate.

Tenancy: every row carries company_id. Public routes resolve the tenant from
the unguessable per-recruiter token, so ids can't be enumerated across agencies.
"""

import json
import hmac
import hashlib
import secrets
import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, request, jsonify, Response

from modules.shared import (
    get_db, ts, current_user, effective_company_id, real_user_id,
    login_required, app_secret, platform_smtp_send, log_candidate_event,
)
from modules import register_migration

bp = Blueprint('scheduler', __name__, url_prefix='/api/scheduler')
# Second blueprint (no prefix) for the public, guest-facing HTML pages served at
# /book/<token> and /book/manage/<token>. register_all() mounts this too.
pages = Blueprint('scheduler_pages', __name__)

IST = ZoneInfo('Asia/Kolkata')

# Default weekly working hours: Mon–Sat 10:00–19:00, Sunday off.
_DEFAULT_HOURS = {
    'mon': [['10:00', '19:00']],
    'tue': [['10:00', '19:00']],
    'wed': [['10:00', '19:00']],
    'thu': [['10:00', '19:00']],
    'fri': [['10:00', '19:00']],
    'sat': [['10:00', '19:00']],
    'sun': [],
}
_DAY_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']  # Monday=0 .. Sunday=6
_DEFAULT_MODES = ['Phone', 'Video', 'In-person']


# ══════════════════════════════════════════════════════════════════════════
#  MIGRATION  (additive only)
# ══════════════════════════════════════════════════════════════════════════
@register_migration
def migrate(conn):
    c = conn.cursor()

    # Per-recruiter availability + public token. One row per user.
    c.execute('''CREATE TABLE IF NOT EXISTS meeting_availability (
        user_id         INTEGER PRIMARY KEY,          -- FK -> users.id (the host)
        company_id      INTEGER NOT NULL,             -- tenant
        enabled         INTEGER DEFAULT 1,
        token           TEXT DEFAULT '',              -- stable public link token
        timezone        TEXT DEFAULT 'Asia/Kolkata',
        weekly_hours    TEXT DEFAULT '',              -- JSON {mon:[[from,to]], ...}
        slot_minutes    INTEGER DEFAULT 30,
        buffer_minutes  INTEGER DEFAULT 10,
        min_notice_hours INTEGER DEFAULT 2,
        horizon_days    INTEGER DEFAULT 14,
        blocked_dates   TEXT DEFAULT '[]',            -- JSON ["YYYY-MM-DD", ...]
        modes           TEXT DEFAULT '',              -- JSON ["Phone","Video","In-person"]
        default_location TEXT DEFAULT '',             -- default video link / address
        headline        TEXT DEFAULT '',              -- shown on booking page
        created_at      TEXT DEFAULT '',
        updated_at      TEXT DEFAULT ''
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_avail_token ON meeting_availability(token)')

    # Bookings.
    c.execute('''CREATE TABLE IF NOT EXISTS meetings (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id      INTEGER NOT NULL,             -- tenant
        host_user_id    INTEGER NOT NULL,             -- recruiter the slot belongs to
        guest_kind      TEXT DEFAULT 'other',         -- candidate|client|other
        candidate_id    INTEGER DEFAULT 0,            -- auto-linked (0 = none)
        crm_client_id   INTEGER DEFAULT 0,            -- auto-linked client
        crm_contact_id  INTEGER DEFAULT 0,            -- auto-linked contact
        guest_name      TEXT DEFAULT '',
        guest_phone     TEXT DEFAULT '',
        guest_email     TEXT DEFAULT '',
        mode            TEXT DEFAULT '',              -- Phone|Video|In-person
        location        TEXT DEFAULT '',              -- link/address for this meeting
        purpose         TEXT DEFAULT '',              -- guest-entered reason / notes
        start_at        TEXT DEFAULT '',              -- ISO local IST 'YYYY-MM-DDTHH:MM'
        end_at          TEXT DEFAULT '',
        duration_minutes INTEGER DEFAULT 30,
        status          TEXT DEFAULT 'confirmed',     -- confirmed|cancelled|completed|no_show
        manage_token    TEXT DEFAULT '',              -- guest reschedule/cancel token
        source          TEXT DEFAULT 'self_book',     -- self_book|manual
        created_at      TEXT DEFAULT '',
        updated_at      TEXT DEFAULT ''
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_meet_company ON meetings(company_id, start_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_meet_host ON meetings(host_user_id, start_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_meet_manage ON meetings(manage_token)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_meet_candidate ON meetings(candidate_id)')
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  TOKENS  (HMAC — unforgeable without the server secret, no storage needed)
# ══════════════════════════════════════════════════════════════════════════
def _secret():
    k = app_secret() or 'fallback-secret'
    return k.encode() if isinstance(k, str) else k


def _book_token(user_id):
    """Stable public booking token for a recruiter. Stored on the availability
    row for fast reverse lookup, but derived so it's reproducible."""
    return hmac.new(_secret(), ('book:%d' % user_id).encode(),
                    hashlib.sha256).hexdigest()[:20]


def _ref_token(kind, oid):
    """Signed token that pre-binds a booking link to a specific candidate or
    CRM contact (used by the 'Send booking link' buttons in the UI wave)."""
    return hmac.new(_secret(), ('ref:%s:%d' % (kind, oid)).encode(),
                    hashlib.sha256).hexdigest()[:24]


def _verify_ref(kind, oid, tok):
    return bool(tok) and secrets.compare_digest(tok, _ref_token(kind, oid))


# ══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════
def _norm_phone(p):
    """Digits only, keep last 10 (Indian mobiles) for tolerant matching."""
    d = ''.join(ch for ch in (p or '') if ch.isdigit())
    return d[-10:] if len(d) >= 10 else d


def _row_or_default(conn, user_id, company_id):
    """Return the availability row for a user, creating a default one (with a
    stable token) on first access so the recruiter always has a live link."""
    r = conn.execute('SELECT * FROM meeting_availability WHERE user_id=?',
                     (user_id,)).fetchone()
    if r:
        # Back-fill a token if an older row somehow lacks one.
        if not r['token']:
            tok = _book_token(user_id)
            conn.execute('UPDATE meeting_availability SET token=?, updated_at=? WHERE user_id=?',
                         (tok, ts(), user_id))
            conn.commit()
            r = conn.execute('SELECT * FROM meeting_availability WHERE user_id=?',
                             (user_id,)).fetchone()
        return r
    tok = _book_token(user_id)
    conn.execute(
        'INSERT INTO meeting_availability '
        '(user_id, company_id, enabled, token, timezone, weekly_hours, slot_minutes, '
        ' buffer_minutes, min_notice_hours, horizon_days, blocked_dates, modes, '
        ' default_location, headline, created_at, updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (user_id, company_id, 1, tok, 'Asia/Kolkata', json.dumps(_DEFAULT_HOURS),
         30, 10, 2, 14, '[]', json.dumps(_DEFAULT_MODES), '', '', ts(), ts()))
    conn.commit()
    return conn.execute('SELECT * FROM meeting_availability WHERE user_id=?',
                        (user_id,)).fetchone()


def _avail_dict(r):
    """Availability row -> plain dict with JSON fields parsed."""
    def _j(v, fb):
        try:
            return json.loads(v) if v else fb
        except Exception:
            return fb
    return {
        'user_id': r['user_id'],
        'enabled': bool(r['enabled']),
        'token': r['token'],
        'timezone': r['timezone'] or 'Asia/Kolkata',
        'weekly_hours': _j(r['weekly_hours'], _DEFAULT_HOURS),
        'slot_minutes': r['slot_minutes'] or 30,
        'buffer_minutes': r['buffer_minutes'] or 0,
        'min_notice_hours': r['min_notice_hours'] or 0,
        'horizon_days': r['horizon_days'] or 14,
        'blocked_dates': _j(r['blocked_dates'], []),
        'modes': _j(r['modes'], _DEFAULT_MODES),
        'default_location': r['default_location'] or '',
        'headline': r['headline'] or '',
    }


def _parse_hhmm(day, hhmm):
    h, m = hhmm.split(':')
    return day.replace(hour=int(h), minute=int(m), second=0, microsecond=0)


def _open_slots(av, existing, now_ist):
    """Compute open slot start-times as ISO strings 'YYYY-MM-DDTHH:MM'.

    av        : parsed availability dict
    existing  : list of (start_iso, end_iso) already-booked intervals (IST local)
    now_ist   : timezone-aware datetime in IST
    """
    slot = int(av['slot_minutes'])
    buf = int(av['buffer_minutes'])
    horizon = int(av['horizon_days'])
    notice = datetime.timedelta(hours=int(av['min_notice_hours']))
    earliest = now_ist + notice
    blocked = set(av['blocked_dates'])

    # Expand existing bookings by buffer on each side for overlap testing.
    busy = []
    for s_iso, e_iso in existing:
        try:
            s = datetime.datetime.fromisoformat(s_iso).replace(tzinfo=IST)
            e = datetime.datetime.fromisoformat(e_iso).replace(tzinfo=IST)
        except Exception:
            continue
        busy.append((s - datetime.timedelta(minutes=buf),
                     e + datetime.timedelta(minutes=buf)))

    out = {}  # date -> [ 'HH:MM', ... ]
    today = now_ist.date()
    for offset in range(0, horizon + 1):
        day_date = today + datetime.timedelta(days=offset)
        dstr = day_date.isoformat()
        if dstr in blocked:
            continue
        windows = av['weekly_hours'].get(_DAY_KEYS[day_date.weekday()], [])
        if not windows:
            continue
        day_dt = datetime.datetime.combine(day_date, datetime.time(0, 0), tzinfo=IST)
        day_slots = []
        for win in windows:
            try:
                w_start = _parse_hhmm(day_dt, win[0])
                w_end = _parse_hhmm(day_dt, win[1])
            except Exception:
                continue
            cur = w_start
            step = datetime.timedelta(minutes=slot)
            while cur + step <= w_end + datetime.timedelta(seconds=1):
                slot_end = cur + step
                if cur >= earliest:
                    clash = any(cur < b_end and slot_end > b_start
                                for (b_start, b_end) in busy)
                    if not clash:
                        day_slots.append(cur.strftime('%H:%M'))
                cur += step
        if day_slots:
            out[dstr] = day_slots
    return out


def _auto_link(conn, company_id, kind, phone, email, ref_kind, ref_id, ref_tok):
    """Resolve (candidate_id, client_id, contact_id). Prefer a signed ref token
    (exact bind from a per-candidate/-contact link); else fuzzy-match by phone
    then email inside this tenant."""
    cand_id, client_id, contact_id = 0, 0, 0

    # 1) Exact bind via signed ref token.
    if ref_kind == 'candidate' and _verify_ref('candidate', ref_id, ref_tok):
        row = conn.execute('SELECT id FROM candidates WHERE id=? AND owner_id=?',
                           (ref_id, company_id)).fetchone()
        if row:
            return row['id'], 0, 0
    if ref_kind == 'contact' and _verify_ref('contact', ref_id, ref_tok):
        row = conn.execute('SELECT id, client_id FROM crm_contacts WHERE id=? AND company_id=?',
                           (ref_id, company_id)).fetchone()
        if row:
            return 0, row['client_id'], row['id']

    nphone = _norm_phone(phone)
    nemail = (email or '').strip().lower()

    # 2) Candidate match (phone, then email) within tenant.
    if kind != 'client':
        if nphone:
            row = conn.execute(
                "SELECT id FROM candidates WHERE owner_id=? AND "
                "substr(replace(replace(replace(replace(phone,'+',''),' ',''),'-',''),'(',''),-10)=? "
                "ORDER BY id DESC LIMIT 1", (company_id, nphone)).fetchone()
            if row:
                cand_id = row['id']
        if not cand_id and nemail:
            row = conn.execute(
                "SELECT id FROM candidates WHERE owner_id=? AND lower(trim(email))=? "
                "ORDER BY id DESC LIMIT 1", (company_id, nemail)).fetchone()
            if row:
                cand_id = row['id']

    # 3) CRM contact match (client meetings) within tenant.
    if kind != 'candidate' and not cand_id:
        row = None
        if nemail:
            row = conn.execute(
                "SELECT id, client_id FROM crm_contacts WHERE company_id=? AND lower(trim(email))=? "
                "AND is_active=1 LIMIT 1", (company_id, nemail)).fetchone()
        if not row and nphone:
            row = conn.execute(
                "SELECT id, client_id FROM crm_contacts WHERE company_id=? AND "
                "substr(replace(replace(replace(replace(phone,'+',''),' ',''),'-',''),'(',''),-10)=? "
                "AND is_active=1 LIMIT 1", (company_id, nphone)).fetchone()
        if row:
            client_id, contact_id = row['client_id'], row['id']

    return cand_id, client_id, contact_id


def _nice_dt(iso):
    try:
        return datetime.datetime.fromisoformat(iso).strftime('%a %d %b %Y, %I:%M %p')
    except Exception:
        return iso


def _gcal_link(m):
    """Build a Google Calendar 'add event' URL (guest one-click)."""
    def _fmt(iso):
        try:
            dt = datetime.datetime.fromisoformat(iso).replace(tzinfo=IST)
            return dt.astimezone(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        except Exception:
            return ''
    from urllib.parse import quote
    title = quote('Meeting: ' + (m.get('purpose') or 'HireLab call'))
    dates = _fmt(m['start_at']) + '/' + _fmt(m['end_at'])
    details = quote((m.get('mode') or '') + (('\n' + m['location']) if m.get('location') else ''))
    return ('https://calendar.google.com/calendar/render?action=TEMPLATE'
            '&text=%s&dates=%s&details=%s' % (title, dates, details))


def _send_guest_confirmation(base_url, host_name, m):
    """Best-effort email confirmation to the guest. Never raises."""
    to = (m.get('guest_email') or '').strip()
    if not to:
        return
    manage = base_url.rstrip('/') + '/book/manage/' + m['manage_token']
    gcal = _gcal_link(m)
    when = _nice_dt(m['start_at'])
    loc_line = ('\nLocation / Link: ' + m['location']) if m.get('location') else ''
    subject = 'Meeting confirmed — %s' % when
    plain = (
        'Hi %s,\n\nYour meeting with %s is confirmed.\n\n'
        'When: %s (IST)\nMode: %s%s\n\n'
        'Reschedule or cancel: %s\nAdd to Google Calendar: %s\n\n— HireLab'
        % (m.get('guest_name') or 'there', host_name, when,
           m.get('mode') or 'To be confirmed', loc_line, manage, gcal))
    html = (
        '<div style="font-family:sans-serif;font-size:14px;color:#1c1c1c;max-width:520px">'
        '<p>Hi %s,</p><p>Your meeting with <b>%s</b> is confirmed.</p>'
        '<table style="font-size:14px;line-height:1.9"><tr><td style="color:#888">When</td>'
        '<td style="padding-left:14px"><b>%s</b> (IST)</td></tr>'
        '<tr><td style="color:#888">Mode</td><td style="padding-left:14px">%s</td></tr>%s</table>'
        '<p style="margin-top:16px">'
        '<a href="%s" style="display:inline-block;background:#0F6E56;color:#fff;padding:9px 16px;'
        'border-radius:8px;text-decoration:none;font-weight:600;margin-right:8px">Add to Google Calendar</a>'
        '<a href="%s" style="display:inline-block;color:#0F6E56;padding:9px 4px;text-decoration:none">'
        'Reschedule / cancel</a></p><p style="font-size:12px;color:#999">— HireLab</p></div>'
        % (m.get('guest_name') or 'there', host_name, when, m.get('mode') or 'To be confirmed',
           (('<tr><td style="color:#888">Where</td><td style="padding-left:14px">%s</td></tr>' % m['location'])
            if m.get('location') else ''),
           gcal, manage))
    try:
        platform_smtp_send(to, subject, plain, html)
    except Exception as e:
        print('[scheduler] guest email failed: %s' % e)


def _existing_intervals(conn, host_user_id):
    rows = conn.execute(
        "SELECT start_at, end_at FROM meetings WHERE host_user_id=? AND status='confirmed' "
        "AND start_at<>''", (host_user_id,)).fetchall()
    return [(r['start_at'], r['end_at']) for r in rows]


# ══════════════════════════════════════════════════════════════════════════
#  AUTHENTICATED ROUTES  (recruiter-facing)
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/availability', methods=['GET'])
@login_required
def get_availability():
    uid = real_user_id()
    conn = get_db()
    r = _row_or_default(conn, uid, effective_company_id())
    av = _avail_dict(r)
    conn.close()
    base = request.host_url.rstrip('/')
    av['public_url'] = base + '/book/' + av['token']
    return jsonify({'ok': True, 'availability': av})


@bp.route('/availability', methods=['PUT'])
@login_required
def update_availability():
    d = request.json or {}
    uid = real_user_id()
    conn = get_db()
    _row_or_default(conn, uid, effective_company_id())  # ensure row + token exist

    fields, vals = [], []

    def _set(col, val):
        fields.append(col + '=?')
        vals.append(val)

    if 'enabled' in d:
        _set('enabled', 1 if d['enabled'] else 0)
    if 'weekly_hours' in d and isinstance(d['weekly_hours'], dict):
        _set('weekly_hours', json.dumps(d['weekly_hours']))
    if 'slot_minutes' in d:
        _set('slot_minutes', max(5, int(d['slot_minutes'])))
    if 'buffer_minutes' in d:
        _set('buffer_minutes', max(0, int(d['buffer_minutes'])))
    if 'min_notice_hours' in d:
        _set('min_notice_hours', max(0, int(d['min_notice_hours'])))
    if 'horizon_days' in d:
        _set('horizon_days', max(1, min(90, int(d['horizon_days']))))
    if 'blocked_dates' in d and isinstance(d['blocked_dates'], list):
        _set('blocked_dates', json.dumps(d['blocked_dates']))
    if 'modes' in d and isinstance(d['modes'], list):
        _set('modes', json.dumps(d['modes']))
    if 'default_location' in d:
        _set('default_location', (d.get('default_location') or '').strip())
    if 'headline' in d:
        _set('headline', (d.get('headline') or '').strip())

    if fields:
        _set('updated_at', ts())
        conn.execute('UPDATE meeting_availability SET ' + ','.join(fields) +
                     ' WHERE user_id=?', (*vals, uid))
        conn.commit()
    r = conn.execute('SELECT * FROM meeting_availability WHERE user_id=?', (uid,)).fetchone()
    av = _avail_dict(r)
    conn.close()
    base = request.host_url.rstrip('/')
    av['public_url'] = base + '/book/' + av['token']
    return jsonify({'ok': True, 'availability': av})


@bp.route('/meetings', methods=['GET'])
@login_required
def list_meetings():
    """List meetings for the tenant. ?scope=upcoming|past|all  ?mine=1"""
    scope = request.args.get('scope', 'upcoming')
    mine = request.args.get('mine') == '1'
    cid = effective_company_id()
    now = datetime.datetime.now(IST).strftime('%Y-%m-%dT%H:%M')
    conn = get_db()
    q = ("SELECT m.*, c.name AS candidate_name, u.display_name AS host_name, u.username AS host_username "
         "FROM meetings m "
         "LEFT JOIN candidates c ON c.id=m.candidate_id "
         "LEFT JOIN users u ON u.id=m.host_user_id "
         "WHERE m.company_id=?")
    args = [cid]
    if mine:
        q += " AND m.host_user_id=?"
        args.append(real_user_id())
    if scope == 'upcoming':
        q += " AND m.start_at>=? AND m.status='confirmed'"
        args.append(now)
        q += " ORDER BY m.start_at ASC"
    elif scope == 'past':
        q += " AND (m.start_at<? OR m.status<>'confirmed')"
        args.append(now)
        q += " ORDER BY m.start_at DESC"
    else:
        q += " ORDER BY m.start_at DESC"
    rows = conn.execute(q, args).fetchall()
    conn.close()
    return jsonify({'ok': True, 'meetings': [dict(r) for r in rows]})


@bp.route('/meetings', methods=['POST'])
@login_required
def create_meeting_manual():
    """Recruiter books a meeting on behalf of a guest (manual entry)."""
    d = request.json or {}
    start_at = (d.get('start_at') or '').strip()
    if not start_at:
        return jsonify({'error': 'start_at required'}), 400
    uid = real_user_id()
    cid = effective_company_id()
    conn = get_db()
    av = _avail_dict(_row_or_default(conn, uid, cid))
    dur = int(d.get('duration_minutes') or av['slot_minutes'])
    try:
        end_at = (datetime.datetime.fromisoformat(start_at) +
                  datetime.timedelta(minutes=dur)).strftime('%Y-%m-%dT%H:%M')
    except Exception:
        conn.close(); return jsonify({'error': 'bad start_at'}), 400
    kind = (d.get('guest_kind') or 'other')
    cand_id, client_id, contact_id = _auto_link(
        conn, cid, kind, d.get('guest_phone'), d.get('guest_email'),
        d.get('ref_kind'), int(d.get('ref_id') or 0), d.get('ref_tok'))
    if d.get('candidate_id'):
        cand_id = int(d['candidate_id'])
    manage_token = secrets.token_urlsafe(24)
    conn.execute(
        'INSERT INTO meetings (company_id,host_user_id,guest_kind,candidate_id,crm_client_id,'
        'crm_contact_id,guest_name,guest_phone,guest_email,mode,location,purpose,start_at,end_at,'
        'duration_minutes,status,manage_token,source,created_at,updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (cid, uid, kind, cand_id, client_id, contact_id,
         (d.get('guest_name') or '').strip(), (d.get('guest_phone') or '').strip(),
         (d.get('guest_email') or '').strip(), (d.get('mode') or '').strip(),
         (d.get('location') or av['default_location']).strip(), (d.get('purpose') or '').strip(),
         start_at, end_at, dur, 'confirmed', manage_token, 'manual', ts(), ts()))
    conn.commit()
    if cand_id:
        try:
            log_candidate_event(cand_id, 'note',
                'Meeting booked — %s (%s)' % (_nice_dt(start_at), (d.get('mode') or 'call')))
        except Exception:
            pass
    conn.close()
    return jsonify({'ok': True})


@bp.route('/meetings/<int:mid>/status', methods=['POST'])
@login_required
def set_meeting_status(mid):
    d = request.json or {}
    st = (d.get('status') or '').strip()
    if st not in ('confirmed', 'cancelled', 'completed', 'no_show'):
        return jsonify({'error': 'bad status'}), 400
    cid = effective_company_id()
    conn = get_db()
    r = conn.execute('SELECT id FROM meetings WHERE id=? AND company_id=?', (mid, cid)).fetchone()
    if not r:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    conn.execute('UPDATE meetings SET status=?, updated_at=? WHERE id=?', (st, ts(), mid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@bp.route('/meetings/<int:mid>', methods=['DELETE'])
@login_required
def delete_meeting(mid):
    cid = effective_company_id()
    conn = get_db()
    conn.execute('DELETE FROM meetings WHERE id=? AND company_id=?', (mid, cid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@bp.route('/ref-token', methods=['GET'])
@login_required
def ref_token():
    """Return a signed ref token so the UI can build a per-candidate/-contact
    booking link. ?kind=candidate|contact&id=<id> — tenant-checked."""
    kind = request.args.get('kind', '')
    oid = int(request.args.get('id') or 0)
    cid = effective_company_id()
    conn = get_db()
    ok = False
    if kind == 'candidate':
        ok = bool(conn.execute('SELECT 1 FROM candidates WHERE id=? AND owner_id=?',
                               (oid, cid)).fetchone())
    elif kind == 'contact':
        ok = bool(conn.execute('SELECT 1 FROM crm_contacts WHERE id=? AND company_id=?',
                               (oid, cid)).fetchone())
    # host token = the current recruiter's public link
    r = _row_or_default(conn, real_user_id(), cid)
    conn.close()
    if not ok:
        return jsonify({'error': 'Not found'}), 404
    base = request.host_url.rstrip('/')
    url = '%s/book/%s?ref=%s&rid=%d&tok=%s' % (base, r['token'], kind, oid, _ref_token(kind, oid))
    return jsonify({'ok': True, 'url': url})


# ══════════════════════════════════════════════════════════════════════════
#  PUBLIC ROUTES  (guest-facing — NO login)
# ══════════════════════════════════════════════════════════════════════════
def _host_by_token(conn, token):
    r = conn.execute('SELECT * FROM meeting_availability WHERE token=?', (token,)).fetchone()
    if not r or not r['enabled']:
        return None, None
    u = conn.execute('SELECT id, display_name, username, company_id FROM users WHERE id=?',
                     (r['user_id'],)).fetchone()
    return r, u


@bp.route('/public/<token>', methods=['GET'])
def public_config(token):
    """Safe public subset of a recruiter's booking config (no internal ids)."""
    conn = get_db()
    r, u = _host_by_token(conn, token)
    conn.close()
    if not r or not u:
        return jsonify({'error': 'Not found'}), 404
    av = _avail_dict(r)
    host_name = (u['display_name'] or u['username'] or 'HireLab')
    return jsonify({'ok': True, 'host_name': host_name,
                    'headline': av['headline'], 'modes': av['modes'],
                    'slot_minutes': av['slot_minutes'], 'timezone': av['timezone']})


@bp.route('/public/<token>/slots', methods=['GET'])
def public_slots(token):
    conn = get_db()
    r, u = _host_by_token(conn, token)
    if not r or not u:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    av = _avail_dict(r)
    existing = _existing_intervals(conn, u['id'])
    conn.close()
    slots = _open_slots(av, existing, datetime.datetime.now(IST))
    return jsonify({'ok': True, 'slots': slots})


@bp.route('/public/<token>/book', methods=['POST'])
def public_book(token):
    d = request.json or {}
    start_at = (d.get('start_at') or '').strip()
    name = (d.get('guest_name') or '').strip()
    if not start_at or not name:
        return jsonify({'error': 'Name and time slot are required'}), 400

    conn = get_db()
    r, u = _host_by_token(conn, token)
    if not r or not u:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    av = _avail_dict(r)
    company_id = u['company_id']
    host_name = (u['display_name'] or u['username'] or 'HireLab')

    # Re-validate the slot against live availability (prevents double-book / tampering).
    existing = _existing_intervals(conn, u['id'])
    valid = _open_slots(av, existing, datetime.datetime.now(IST))
    day = start_at[:10]
    hhmm = start_at[11:16]
    if day not in valid or hhmm not in valid[day]:
        conn.close()
        return jsonify({'error': 'That slot is no longer available. Please pick another.'}), 409

    dur = int(av['slot_minutes'])
    end_at = (datetime.datetime.fromisoformat(day + 'T' + hhmm) +
              datetime.timedelta(minutes=dur)).strftime('%Y-%m-%dT%H:%M')

    kind = (d.get('guest_kind') or 'other')
    if kind not in ('candidate', 'client', 'other'):
        kind = 'other'
    ref_kind = d.get('ref_kind') or ''
    ref_id = int(d.get('ref_id') or 0)
    ref_tok = d.get('ref_tok') or ''
    cand_id, client_id, contact_id = _auto_link(
        conn, company_id, kind, d.get('guest_phone'), d.get('guest_email'),
        ref_kind, ref_id, ref_tok)
    if cand_id and kind == 'other':
        kind = 'candidate'
    elif client_id and kind == 'other':
        kind = 'client'

    mode = (d.get('mode') or '').strip()
    if av['modes'] and mode and mode not in av['modes']:
        mode = ''
    location = (d.get('location') or av['default_location'] or '').strip()
    manage_token = secrets.token_urlsafe(24)

    cur = conn.execute(
        'INSERT INTO meetings (company_id,host_user_id,guest_kind,candidate_id,crm_client_id,'
        'crm_contact_id,guest_name,guest_phone,guest_email,mode,location,purpose,start_at,end_at,'
        'duration_minutes,status,manage_token,source,created_at,updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (company_id, u['id'], kind, cand_id, client_id, contact_id, name,
         (d.get('guest_phone') or '').strip(), (d.get('guest_email') or '').strip(),
         mode, location, (d.get('purpose') or '').strip(),
         day + 'T' + hhmm, end_at, dur, 'confirmed', manage_token, 'self_book', ts(), ts()))
    mid = cur.lastrowid
    conn.commit()

    if cand_id:
        try:
            log_candidate_event(cand_id, 'note',
                'Booked a %s slot — %s' % (mode or 'meeting', _nice_dt(day + 'T' + hhmm)))
        except Exception:
            pass
    conn.close()

    m = {'guest_name': name, 'guest_email': (d.get('guest_email') or '').strip(),
         'mode': mode, 'location': location, 'purpose': (d.get('purpose') or '').strip(),
         'start_at': day + 'T' + hhmm, 'end_at': end_at, 'manage_token': manage_token}
    _send_guest_confirmation(request.host_url, host_name, m)

    return jsonify({'ok': True, 'meeting_id': mid, 'manage_token': manage_token,
                    'host_name': host_name, 'when': _nice_dt(day + 'T' + hhmm),
                    'gcal_url': _gcal_link(m)})


@bp.route('/public/manage/<manage_token>', methods=['GET'])
def public_manage(manage_token):
    conn = get_db()
    m = conn.execute(
        "SELECT m.*, u.display_name AS host_name, u.username AS host_username, a.token AS host_token "
        "FROM meetings m LEFT JOIN users u ON u.id=m.host_user_id "
        "LEFT JOIN meeting_availability a ON a.user_id=m.host_user_id "
        "WHERE m.manage_token=?", (manage_token,)).fetchone()
    conn.close()
    if not m:
        return jsonify({'error': 'Not found'}), 404
    host_name = (m['host_name'] or m['host_username'] or 'HireLab')
    return jsonify({'ok': True, 'meeting': {
        'guest_name': m['guest_name'], 'host_name': host_name,
        'mode': m['mode'], 'location': m['location'], 'purpose': m['purpose'],
        'start_at': m['start_at'], 'when': _nice_dt(m['start_at']),
        'status': m['status'], 'host_token': m['host_token']}})


@bp.route('/public/manage/<manage_token>/cancel', methods=['POST'])
def public_cancel(manage_token):
    conn = get_db()
    m = conn.execute('SELECT id, status FROM meetings WHERE manage_token=?', (manage_token,)).fetchone()
    if not m:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    if m['status'] == 'cancelled':
        conn.close(); return jsonify({'ok': True, 'already': True})
    conn.execute('UPDATE meetings SET status=?, updated_at=? WHERE id=?', ('cancelled', ts(), m['id']))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@bp.route('/public/manage/<manage_token>/reschedule', methods=['POST'])
def public_reschedule(manage_token):
    d = request.json or {}
    new_start = (d.get('start_at') or '').strip()
    if not new_start:
        return jsonify({'error': 'start_at required'}), 400
    conn = get_db()
    m = conn.execute('SELECT * FROM meetings WHERE manage_token=?', (manage_token,)).fetchone()
    if not m:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    av_row = _row_or_default(conn, m['host_user_id'], m['company_id'])
    av = _avail_dict(av_row)
    # Exclude the current booking so its own slot counts as free.
    existing = [(s, e) for (s, e) in _existing_intervals(conn, m['host_user_id'])
                if s != m['start_at']]
    valid = _open_slots(av, existing, datetime.datetime.now(IST))
    day, hhmm = new_start[:10], new_start[11:16]
    if day not in valid or hhmm not in valid[day]:
        conn.close()
        return jsonify({'error': 'That slot is not available. Please pick another.'}), 409
    dur = m['duration_minutes'] or av['slot_minutes']
    end_at = (datetime.datetime.fromisoformat(day + 'T' + hhmm) +
              datetime.timedelta(minutes=dur)).strftime('%Y-%m-%dT%H:%M')
    conn.execute('UPDATE meetings SET start_at=?, end_at=?, status=?, updated_at=? WHERE id=?',
                 (day + 'T' + hhmm, end_at, 'confirmed', ts(), m['id']))
    conn.commit()
    if m['candidate_id']:
        try:
            log_candidate_event(m['candidate_id'], 'note',
                'Meeting rescheduled — %s' % _nice_dt(day + 'T' + hhmm))
        except Exception:
            pass
    conn.close()
    return jsonify({'ok': True, 'when': _nice_dt(day + 'T' + hhmm)})


# ══════════════════════════════════════════════════════════════════════════
#  PUBLIC PAGES  (guest-facing HTML — no login)
#  Served by the `pages` blueprint (no url_prefix) at /book/<token> and
#  /book/manage/<token>. Self-contained: no external CSS/JS, mobile-first.
# ══════════════════════════════════════════════════════════════════════════
_BOOK_CSS = """
:root{--ink:#1b1a17;--muted:#8a857a;--line:#e9e6e0;--bg:#faf9f7;--em:#0f6e56;
--em-d:#0b5946;--em-l:#e7f2ee;--card:#fff;--warn:#b4541f;--radius:14px}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
background:var(--bg);color:var(--ink);line-height:1.5;font-size:15px}
.wrap{max-width:560px;margin:0 auto;padding:20px 16px 60px}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:18px}
.brand .logo{width:30px;height:30px;border-radius:8px;background:var(--em);color:#fff;
display:flex;align-items:center;justify-content:center;font-weight:800;font-size:15px}
.brand .name{font-weight:700;font-size:15px;letter-spacing:-.2px}
.brand .sub{font-size:11px;color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
padding:20px 18px;box-shadow:0 1px 2px rgba(20,20,20,.04)}
h1{font-size:19px;margin:0 0 3px;letter-spacing:-.4px}
.host{font-size:13px;color:var(--muted);margin-bottom:2px}
.headline{font-size:14px;color:#5c5850;margin:8px 0 0}
.sec{margin-top:22px}
.label{font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin-bottom:9px}
.dates{display:flex;gap:8px;overflow-x:auto;padding-bottom:6px;scrollbar-width:thin}
.date{flex:0 0 auto;min-width:62px;text-align:center;padding:9px 8px;border:1px solid var(--line);
border-radius:11px;cursor:pointer;background:#fff;transition:.12s}
.date:hover{border-color:#cfcabf}
.date.on{background:var(--em);border-color:var(--em);color:#fff}
.date .dow{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;opacity:.75}
.date .d{font-size:18px;font-weight:700;line-height:1.25}
.date .mon{font-size:10px;opacity:.75}
.times{display:grid;grid-template-columns:repeat(auto-fill,minmax(88px,1fr));gap:8px}
.time{padding:10px 6px;border:1px solid var(--line);border-radius:10px;background:#fff;
text-align:center;font-weight:600;font-size:13.5px;cursor:pointer;transition:.12s}
.time:hover{border-color:var(--em);color:var(--em)}
.time.on{background:var(--em);border-color:var(--em);color:#fff}
.seg{display:flex;gap:6px;flex-wrap:wrap}
.seg button{flex:1;min-width:90px;padding:9px;border:1px solid var(--line);background:#fff;border-radius:10px;
font-size:13px;font-weight:600;color:var(--ink);cursor:pointer}
.seg button.on{background:var(--em-l);border-color:var(--em);color:var(--em-d)}
label.f{display:block;margin-top:13px}
label.f .t{font-size:12.5px;font-weight:600;color:#5c5850;margin-bottom:5px;display:block}
input,textarea{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:10px;
font-size:15px;font-family:inherit;background:#fff;color:var(--ink)}
input:focus,textarea:focus{outline:none;border-color:var(--em);box-shadow:0 0 0 3px var(--em-l)}
textarea{min-height:64px;resize:vertical}
.btn{display:block;width:100%;margin-top:20px;padding:14px;border:none;border-radius:12px;
background:var(--em);color:#fff;font-size:15px;font-weight:700;cursor:pointer;transition:.12s}
.btn:hover{background:var(--em-d)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.ghost{background:#fff;border:1px solid var(--line);color:var(--ink)}
.btn.warn{background:#fff;border:1px solid #eecfc0;color:var(--warn)}
.muted{color:var(--muted);font-size:13px}
.err{background:#fdecec;border:1px solid #f4c9c9;color:#a12b2b;padding:10px 12px;border-radius:10px;
font-size:13px;margin-top:14px}
.center{text-align:center}
.check{width:56px;height:56px;border-radius:50%;background:var(--em-l);color:var(--em);
display:flex;align-items:center;justify-content:center;font-size:28px;margin:6px auto 14px}
.detail{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px solid var(--line);font-size:14px}
.detail:last-child{border-bottom:none}
.detail .k{color:var(--muted)}
.detail .v{font-weight:600;text-align:right}
.spin{width:20px;height:20px;border:2.5px solid var(--em-l);border-top-color:var(--em);
border-radius:50%;animation:sp .7s linear infinite;margin:24px auto}
@keyframes sp{to{transform:rotate(360deg)}}
.foot{text-align:center;margin-top:22px;font-size:11.5px;color:#b7b2a8}
a{color:var(--em-d)}
.pill{display:inline-block;font-size:11px;font-weight:700;padding:3px 9px;border-radius:20px;
background:var(--em-l);color:var(--em-d);margin-left:8px}
.pill.cx{background:#fdecec;color:#a12b2b}
"""

_BOOK_HTML = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="robots" content="noindex"><title>Book a meeting · HireLab</title>
<style>__CSS__</style></head><body><div class="wrap">
<div class="brand"><div class="logo">H</div>
<div><div class="name">HireLab</div><div class="sub">Schedule a meeting</div></div></div>
<div id="root"><div class="spin"></div></div>
<div class="foot">Powered by HireLab · your details are shared only with your recruiter</div>
</div><script>
var TOKEN="__TOKEN__", REF=__REF__;
var CFG=null, SLOTS={}, sel={date:null,time:null,mode:null};
var API="/api/scheduler/public/"+TOKEN;
function el(id){return document.getElementById(id);}
function esc(s){return (s||"").replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}
function fmtDate(iso){var d=new Date(iso+"T00:00:00");return{dow:d.toLocaleDateString("en-IN",{weekday:"short"}),
day:d.getDate(),mon:d.toLocaleDateString("en-IN",{month:"short"})};}
function load(){
  Promise.all([fetch(API).then(r=>r.json()),fetch(API+"/slots").then(r=>r.json())])
  .then(function(res){var c=res[0],s=res[1];
    if(!c.ok){el("root").innerHTML='<div class="card center"><div class="muted">This booking link is not available.</div></div>';return;}
    CFG=c;SLOTS=(s&&s.slots)||{};render();
  }).catch(function(){el("root").innerHTML='<div class="card center"><div class="muted">Could not load. Please refresh.</div></div>';});
}
function render(){
  var dates=Object.keys(SLOTS).sort();
  var head='<div class="card"><h1>'+esc(CFG.host_name)+'</h1>'
    +'<div class="host">'+CFG.slot_minutes+'-minute meeting · IST</div>'
    +(CFG.headline?'<div class="headline">'+esc(CFG.headline)+'</div>':'')+'</div>';
  if(!dates.length){el("root").innerHTML=head+'<div class="sec card center"><div class="muted">No open times in the next couple of weeks. Please check back soon.</div></div>';return;}
  var datesHtml=dates.map(function(dt){var f=fmtDate(dt);return '<div class="date" data-d="'+dt+'" onclick="pickDate(\\''+dt+'\\')">'
    +'<div class="dow">'+f.dow+'</div><div class="d">'+f.day+'</div><div class="mon">'+f.mon+'</div></div>';}).join("");
  el("root").innerHTML=head
    +'<div class="sec"><div class="label">Pick a day</div><div class="dates" id="dates">'+datesHtml+'</div></div>'
    +'<div class="sec" id="timesWrap" style="display:none"><div class="label">Pick a time</div><div class="times" id="times"></div></div>'
    +'<div id="formWrap"></div>';
  pickDate(dates[0]);
}
function pickDate(dt){sel.date=dt;sel.time=null;
  [].forEach.call(document.querySelectorAll(".date"),function(e){e.classList.toggle("on",e.getAttribute("data-d")===dt);});
  var times=(SLOTS[dt]||[]);
  el("timesWrap").style.display="block";
  el("times").innerHTML=times.map(function(t){return '<div class="time" data-t="'+t+'" onclick="pickTime(\\''+t+'\\')">'+t12(t)+'</div>';}).join("");
  el("formWrap").innerHTML="";
}
function t12(t){var p=t.split(":"),h=+p[0],m=p[1];var ap=h>=12?"PM":"AM";var hh=h%12;if(hh===0)hh=12;return hh+":"+m+" "+ap;}
function pickTime(t){sel.time=t;
  [].forEach.call(document.querySelectorAll(".time"),function(e){e.classList.toggle("on",e.getAttribute("data-t")===t);});
  showForm();
}
function showForm(){
  var modes=(CFG.modes||[]);sel.mode=sel.mode||modes[0]||"";
  var modeHtml=modes.length?('<div class="label" style="margin-top:16px">Meeting type</div><div class="seg" id="modeSeg">'
    +modes.map(function(m){return '<button type="button" data-m="'+esc(m)+'" onclick="pickMode(\\''+esc(m)+'\\')" class="'+(m===sel.mode?"on":"")+'">'+esc(m)+'</button>';}).join("")+'</div>'):"";
  var kindHtml=REF?"":('<div class="label" style="margin-top:16px">You are a</div><div class="seg" id="kindSeg">'
    +[["candidate","Candidate"],["client","Client"],["other","Guest"]].map(function(k,i){return '<button type="button" data-k="'+k[0]+'" onclick="pickKind(\\''+k[0]+'\\')" class="'+(i===0?"on":"")+'">'+k[1]+'</button>';}).join("")+'</div>');
  window._kind=REF?REF.kind_guest:"candidate";
  el("formWrap").innerHTML='<div class="sec card">'
    +'<div class="label">Your details · '+fmtLong(sel.date)+' at '+t12(sel.time)+'</div>'
    +'<label class="f"><span class="t">Full name *</span><input id="g_name" autocomplete="name" placeholder="Your name"></label>'
    +'<label class="f"><span class="t">Phone (WhatsApp)</span><input id="g_phone" inputmode="tel" autocomplete="tel" placeholder="e.g. 98765 43210"></label>'
    +'<label class="f"><span class="t">Email (for confirmation)</span><input id="g_email" inputmode="email" autocomplete="email" placeholder="you@email.com"></label>'
    +modeHtml+kindHtml
    +'<label class="f"><span class="t">Anything to add?</span><textarea id="g_note" placeholder="Optional — role, agenda, questions"></textarea></label>'
    +'<div id="bookErr"></div>'
    +'<button class="btn" id="bookBtn" onclick="submit()">Confirm booking</button></div>';
  el("formWrap").scrollIntoView({behavior:"smooth",block:"nearest"});
}
function pickMode(m){sel.mode=m;[].forEach.call(document.querySelectorAll("#modeSeg button"),function(e){e.classList.toggle("on",e.getAttribute("data-m")===m);});}
function pickKind(k){window._kind=k;[].forEach.call(document.querySelectorAll("#kindSeg button"),function(e){e.classList.toggle("on",e.getAttribute("data-k")===k);});}
function fmtLong(iso){var d=new Date(iso+"T00:00:00");return d.toLocaleDateString("en-IN",{weekday:"long",day:"numeric",month:"short"});}
function submit(){
  var name=el("g_name").value.trim();
  if(!name){el("bookErr").innerHTML='<div class="err">Please enter your name.</div>';return;}
  var btn=el("bookBtn");btn.disabled=true;btn.textContent="Booking…";el("bookErr").innerHTML="";
  var body={start_at:sel.date+"T"+sel.time,guest_name:name,guest_phone:el("g_phone").value.trim(),
    guest_email:el("g_email").value.trim(),mode:sel.mode,purpose:el("g_note").value.trim(),guest_kind:window._kind};
  if(REF){body.ref_kind=REF.kind;body.ref_id=REF.id;body.ref_tok=REF.tok;}
  fetch(API+"/book",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})
  .then(r=>r.json().then(j=>({s:r.status,j:j}))).then(function(o){
    if(o.s===200&&o.j.ok){done(o.j);}
    else{btn.disabled=false;btn.textContent="Confirm booking";
      el("bookErr").innerHTML='<div class="err">'+esc(o.j.error||"Could not book. Please try another slot.")+'</div>';
      if(o.s===409){setTimeout(load,900);}}
  }).catch(function(){btn.disabled=false;btn.textContent="Confirm booking";
    el("bookErr").innerHTML='<div class="err">Network error. Please try again.</div>';});
}
function done(j){
  var manage=location.origin+"/book/manage/"+j.manage_token;
  el("root").innerHTML='<div class="card center"><div class="check">&#10003;</div>'
    +'<h1>You\\'re booked!</h1><div class="muted" style="margin-bottom:16px">A confirmation has been emailed to you (if you shared an email).</div>'
    +'<div style="text-align:left"><div class="detail"><span class="k">With</span><span class="v">'+esc(j.host_name)+'</span></div>'
    +'<div class="detail"><span class="k">When</span><span class="v">'+esc(j.when)+' IST</span></div>'
    +(sel.mode?'<div class="detail"><span class="k">Type</span><span class="v">'+esc(sel.mode)+'</span></div>':'')+'</div>'
    +'<a class="btn" href="'+j.gcal_url+'" target="_blank" rel="noopener" style="margin-top:18px;text-decoration:none">Add to Google Calendar</a>'
    +'<a class="btn ghost" href="'+manage+'" style="margin-top:10px;text-decoration:none">Reschedule or cancel</a></div>';
  window.scrollTo(0,0);
}
load();
</script></body></html>"""

_MANAGE_HTML = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="robots" content="noindex"><title>Manage booking · HireLab</title>
<style>__CSS__</style></head><body><div class="wrap">
<div class="brand"><div class="logo">H</div>
<div><div class="name">HireLab</div><div class="sub">Manage booking</div></div></div>
<div id="root"><div class="spin"></div></div>
<div class="foot">HireLab</div></div><script>
var MT="__MTOKEN__", M=null, HOST=null, SLOTS={}, rs={date:null,time:null};
function el(id){return document.getElementById(id);}
function esc(s){return (s||"").replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}
function t12(t){var p=t.split(":"),h=+p[0],m=p[1];var ap=h>=12?"PM":"AM";var hh=h%12;if(hh===0)hh=12;return hh+":"+m+" "+ap;}
function fmtDate(iso){var d=new Date(iso+"T00:00:00");return{dow:d.toLocaleDateString("en-IN",{weekday:"short"}),day:d.getDate(),mon:d.toLocaleDateString("en-IN",{month:"short"})};}
function load(){fetch("/api/scheduler/public/manage/"+MT).then(r=>r.json()).then(function(j){
  if(!j.ok){el("root").innerHTML='<div class="card center"><div class="muted">Booking not found.</div></div>';return;}
  M=j.meeting;render();}).catch(function(){el("root").innerHTML='<div class="card center"><div class="muted">Could not load.</div></div>';});}
function render(){
  var cx=M.status==="cancelled";
  var pill=cx?'<span class="pill cx">Cancelled</span>':(M.status==="confirmed"?'<span class="pill">Confirmed</span>':'<span class="pill">'+esc(M.status)+'</span>');
  el("root").innerHTML='<div class="card"><h1>Your meeting'+pill+'</h1>'
    +'<div style="margin-top:12px"><div class="detail"><span class="k">With</span><span class="v">'+esc(M.host_name)+'</span></div>'
    +'<div class="detail"><span class="k">When</span><span class="v">'+esc(M.when)+' IST</span></div>'
    +(M.mode?'<div class="detail"><span class="k">Type</span><span class="v">'+esc(M.mode)+'</span></div>':'')
    +(M.location?'<div class="detail"><span class="k">Where</span><span class="v">'+esc(M.location)+'</span></div>':'')+'</div>'
    +(cx?'<div class="muted center" style="margin-top:16px">This booking was cancelled.</div>'
      :'<button class="btn ghost" onclick="startResched()">Reschedule</button>'
       +'<button class="btn warn" onclick="cancelBk()">Cancel booking</button>')
    +'</div><div id="rsWrap"></div>';
}
function cancelBk(){if(!confirm("Cancel this meeting?"))return;
  fetch("/api/scheduler/public/manage/"+MT+"/cancel",{method:"POST"}).then(r=>r.json()).then(function(){M.status="cancelled";render();window.scrollTo(0,0);});}
function startResched(){
  if(!M.host_token){alert("Reschedule unavailable for this booking.");return;}
  el("rsWrap").innerHTML='<div class="sec card"><div class="spin"></div></div>';
  fetch("/api/scheduler/public/"+M.host_token+"/slots").then(r=>r.json()).then(function(s){
    SLOTS=(s&&s.slots)||{};var dates=Object.keys(SLOTS).sort();
    if(!dates.length){el("rsWrap").innerHTML='<div class="sec card center"><div class="muted">No open times available right now.</div></div>';return;}
    var dh=dates.map(function(dt){var f=fmtDate(dt);return '<div class="date" data-d="'+dt+'" onclick="rDate(\\''+dt+'\\')"><div class="dow">'+f.dow+'</div><div class="d">'+f.day+'</div><div class="mon">'+f.mon+'</div></div>';}).join("");
    el("rsWrap").innerHTML='<div class="sec card"><div class="label">Pick a new day</div><div class="dates">'+dh+'</div>'
      +'<div class="sec" id="rTimesW" style="display:none"><div class="label">Pick a time</div><div class="times" id="rTimes"></div></div>'
      +'<div id="rsErr"></div><button class="btn" id="rBtn" style="display:none" onclick="doResched()">Confirm new time</button></div>';
    rDate(dates[0]);el("rsWrap").scrollIntoView({behavior:"smooth",block:"nearest"});
  });
}
function rDate(dt){rs.date=dt;rs.time=null;
  [].forEach.call(document.querySelectorAll("#rsWrap .date"),function(e){e.classList.toggle("on",e.getAttribute("data-d")===dt);});
  el("rTimesW").style.display="block";
  el("rTimes").innerHTML=(SLOTS[dt]||[]).map(function(t){return '<div class="time" data-t="'+t+'" onclick="rTime(\\''+t+'\\')">'+t12(t)+'</div>';}).join("");
  el("rBtn").style.display="none";
}
function rTime(t){rs.time=t;[].forEach.call(document.querySelectorAll("#rTimes .time"),function(e){e.classList.toggle("on",e.getAttribute("data-t")===t);});el("rBtn").style.display="block";}
function doResched(){var b=el("rBtn");b.disabled=true;b.textContent="Saving…";
  fetch("/api/scheduler/public/manage/"+MT+"/reschedule",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({start_at:rs.date+"T"+rs.time})}).then(r=>r.json().then(j=>({s:r.status,j:j}))).then(function(o){
    if(o.s===200&&o.j.ok){M.when=o.j.when;M.status="confirmed";el("rsWrap").innerHTML="";render();window.scrollTo(0,0);}
    else{b.disabled=false;b.textContent="Confirm new time";el("rsErr").innerHTML='<div class="err">'+esc(o.j.error||"Could not reschedule.")+'</div>';if(o.s===409)startResched();}
  }).catch(function(){b.disabled=false;b.textContent="Confirm new time";el("rsErr").innerHTML='<div class="err">Network error.</div>';});
}
load();
</script></body></html>"""


def _render_page(html, **subs):
    out = html.replace('__CSS__', _BOOK_CSS)
    for k, v in subs.items():
        out = out.replace(k, v)
    return Response(out, mimetype='text/html')


@pages.route('/book/<token>', methods=['GET'])
def book_page(token):
    """Public booking page for a recruiter's link. Optional ref params
    (?ref=candidate|contact&rid=<id>&tok=<sig>) pre-bind the booking."""
    conn = get_db()
    r = conn.execute('SELECT user_id, enabled FROM meeting_availability WHERE token=?',
                     (token,)).fetchone()
    conn.close()
    if not r or not r['enabled']:
        return Response('<div style="font-family:sans-serif;padding:40px;text-align:center;color:#888">'
                        'This booking link is not available.</div>', mimetype='text/html', status=404)
    ref_js = 'null'
    ref = request.args.get('ref', '')
    rid = request.args.get('rid', '')
    tok = request.args.get('tok', '')
    if ref in ('candidate', 'contact') and rid.isdigit() and tok:
        if _verify_ref(ref, int(rid), tok):
            kind_guest = 'candidate' if ref == 'candidate' else 'client'
            ref_js = json.dumps({'kind': ref, 'id': int(rid), 'tok': tok,
                                 'kind_guest': kind_guest})
    return _render_page(_BOOK_HTML, __TOKEN__=token, __REF__=ref_js)


@pages.route('/book/manage/<mtoken>', methods=['GET'])
def manage_page(mtoken):
    """Public reschedule/cancel page for a specific booking."""
    return _render_page(_MANAGE_HTML, __MTOKEN__=mtoken)


# ══════════════════════════════════════════════════════════════════════════
#  RECRUITER CONSOLE  (/schedule) — login-protected, served by `pages`
#  Set availability, copy your public link, and manage all bookings.
# ══════════════════════════════════════════════════════════════════════════
_CONSOLE_CSS = """
:root{--ink:#1b1a17;--muted:#8a857a;--soft:#5c5850;--line:#e9e6e0;--bg:#faf9f7;
--em:#0f6e56;--em-d:#0b5946;--em-l:#e7f2ee;--card:#fff;--warn:#b4541f;--wl:#fbeee6;--radius:12px}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
background:var(--bg);color:var(--ink);font-size:14px;line-height:1.5}
.top{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
padding:12px 18px;display:flex;align-items:center;gap:12px}
.top a.back{color:var(--muted);text-decoration:none;font-size:13px;display:flex;align-items:center;gap:5px}
.top .logo{width:26px;height:26px;border-radius:7px;background:var(--em);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:13px}
.top h1{font-size:15px;margin:0;font-weight:700;letter-spacing:-.2px}
.wrap{max-width:820px;margin:0 auto;padding:20px 16px 80px}
.grid{display:grid;grid-template-columns:1fr;gap:16px}
@media(min-width:720px){.grid{grid-template-columns:1fr 1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:16px 16px}
.card h2{font-size:12px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin:0 0 12px;font-weight:700}
.linkrow{display:flex;gap:8px;align-items:center}
.linkrow input{flex:1;font-family:ui-monospace,Menlo,monospace;font-size:12.5px;background:var(--bg)}
input,select,textarea{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:9px;font-size:14px;font-family:inherit;background:#fff;color:var(--ink)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--em);box-shadow:0 0 0 3px var(--em-l)}
input[type=checkbox]{width:auto}
.btn{padding:9px 14px;border:none;border-radius:9px;background:var(--em);color:#fff;font-size:13.5px;font-weight:700;cursor:pointer;white-space:nowrap}
.btn:hover{background:var(--em-d)}
.btn.sm{padding:6px 10px;font-size:12.5px}
.btn.ghost{background:#fff;border:1px solid var(--line);color:var(--ink)}
.btn.wa{background:#e7f7ee;border:1px solid #bfe6cf;color:#0b6b3a}
.btn.warn{background:var(--wl);border:1px solid #eecfc0;color:var(--warn)}
.day{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--line)}
.day:last-child{border-bottom:none}
.day .nm{width:42px;font-weight:600;font-size:12.5px;color:var(--soft)}
.day input[type=time]{width:auto;flex:1;min-width:0}
.day .to{color:var(--muted);font-size:12px}
.day.off input[type=time]{opacity:.4;pointer-events:none}
.field{margin-bottom:12px}
.field label{display:block;font-size:12px;font-weight:600;color:var(--soft);margin-bottom:5px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.modes{display:flex;gap:8px;flex-wrap:wrap}
.modes label{display:flex;align-items:center;gap:6px;font-size:13px;font-weight:500;padding:7px 11px;border:1px solid var(--line);border-radius:9px;cursor:pointer}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{background:var(--em-l);color:var(--em-d);border-radius:20px;padding:4px 10px;font-size:12px;font-weight:600;display:flex;align-items:center;gap:6px}
.chip b{cursor:pointer}
.tabs{display:flex;gap:6px;margin-bottom:14px}
.tabs button{padding:7px 14px;border:1px solid var(--line);background:#fff;border-radius:9px;font-size:13px;font-weight:600;color:var(--soft);cursor:pointer}
.tabs button.on{background:var(--em);border-color:var(--em);color:#fff}
.mtg{border:1px solid var(--line);border-radius:11px;padding:12px 13px;margin-bottom:10px}
.mtg .when{font-weight:700;font-size:14px}
.mtg .who{font-size:13px;color:var(--soft);margin-top:2px}
.mtg .meta{font-size:12px;color:var(--muted);margin-top:3px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.mtg .acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.pill{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:20px;background:var(--em-l);color:var(--em-d)}
.pill.link{background:#eef;color:#3a3a86}
.pill.cx{background:#fdecec;color:#a12b2b}
.pill.done{background:#eef6ea;color:#3b6d11}
.save-bar{position:sticky;bottom:0;background:linear-gradient(transparent,var(--bg) 30%);padding:14px 0 4px;margin-top:6px}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--ink);color:#fff;padding:10px 18px;border-radius:10px;font-size:13px;opacity:0;transition:.25s;pointer-events:none;z-index:20}
.toast.show{opacity:1}
.muted{color:var(--muted)}.spin{width:20px;height:20px;border:2.5px solid var(--em-l);border-top-color:var(--em);border-radius:50%;animation:sp .7s linear infinite;margin:30px auto}@keyframes sp{to{transform:rotate(360deg)}}
.empty{text-align:center;color:var(--muted);padding:24px 10px;font-size:13px}
"""

_CONSOLE_HTML = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scheduler · HireLab</title><style>__CSS__</style></head><body>
<div class="top"><a class="back" href="/">&#8592; App</a><div class="logo">H</div><h1>Scheduler</h1></div>
<div class="wrap"><div id="root"><div class="spin"></div></div></div>
<div class="toast" id="toast"></div>
<script>
var AV=null, DAYS=[["mon","Mon"],["tue","Tue"],["wed","Wed"],["thu","Thu"],["fri","Fri"],["sat","Sat"],["sun","Sun"]];
var scope="upcoming", MEET=[];
function el(id){return document.getElementById(id);}
function esc(s){return (s||"").replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}
function toast(t){var e=el("toast");e.textContent=t;e.classList.add("show");setTimeout(function(){e.classList.remove("show");},1800);}
function t12(t){if(!t)return"";var p=t.split(":"),h=+p[0],m=p[1];var ap=h>=12?"PM":"AM";var hh=h%12||12;return hh+":"+m+" "+ap;}
function whenFmt(iso){try{var d=new Date(iso);return d.toLocaleDateString("en-IN",{weekday:"short",day:"numeric",month:"short"})+", "+t12(iso.slice(11,16));}catch(e){return iso;}}

function boot(){
  fetch("/api/scheduler/availability").then(function(r){if(r.status===401){location.href="/login";return null;}return r.json();})
  .then(function(j){if(!j)return;AV=j.availability;loadMeetings();}).catch(function(){el("root").innerHTML='<div class="empty">Could not load. Refresh?</div>';});
}
function render(){
  el("root").innerHTML=
   '<div class="card" style="margin-bottom:16px"><h2>Your booking link</h2>'
   +'<div class="linkrow"><input id="pub" readonly value="'+esc(AV.public_url)+'">'
   +'<button class="btn sm" onclick="copyLink()">Copy</button>'
   +'<a class="btn sm ghost" href="'+esc(AV.public_url)+'" target="_blank">Open</a></div>'
   +'<label style="display:flex;align-items:center;gap:8px;margin-top:12px;font-size:13px"><input type="checkbox" id="en" '+(AV.enabled?"checked":"")+'> Accept new bookings</label></div>'
   +'<div class="grid"><div class="card"><h2>Availability</h2><div id="days"></div>'
     +'<div class="field" style="margin-top:14px"><label>Headline (shown on booking page)</label><input id="hl" value="'+esc(AV.headline)+'" placeholder="Quick screening call"></div>'
     +'<div class="row2"><div class="field"><label>Slot length</label><select id="slot">'
       +[15,20,30,45,60].map(function(n){return '<option value="'+n+'" '+(AV.slot_minutes==n?"selected":"")+'>'+n+' min</option>';}).join("")+'</select></div>'
     +'<div class="field"><label>Buffer between (min)</label><input type="number" id="buf" value="'+AV.buffer_minutes+'" min="0"></div></div>'
     +'<div class="row2"><div class="field"><label>Min notice (hours)</label><input type="number" id="notice" value="'+AV.min_notice_hours+'" min="0"></div>'
     +'<div class="field"><label>Bookable ahead (days)</label><input type="number" id="hz" value="'+AV.horizon_days+'" min="1" max="90"></div></div>'
     +'<div class="field"><label>Meeting types</label><div class="modes" id="modes"></div></div>'
     +'<div class="field"><label>Default location / video link</label><input id="loc" value="'+esc(AV.default_location)+'" placeholder="e.g. Google Meet link or office address"></div>'
     +'<div class="field"><label>Blocked dates</label><div style="display:flex;gap:8px"><input type="date" id="bd"><button class="btn sm ghost" onclick="addBlocked()">Add</button></div><div class="chips" id="blocked"></div></div>'
     +'<div class="save-bar"><button class="btn" style="width:100%" onclick="saveAv()">Save availability</button></div></div>'
   +'<div class="card"><h2>Bookings</h2><div class="tabs"><button id="tb-up" class="on" onclick="setScope(\\'upcoming\\')">Upcoming</button><button id="tb-pa" onclick="setScope(\\'past\\')">Past</button></div><div id="mlist"><div class="spin"></div></div></div></div>';
  renderDays();renderModes();renderBlocked();renderMeetings();
}
function renderDays(){
  el("days").innerHTML=DAYS.map(function(d){var w=(AV.weekly_hours[d[0]]||[]);var on=w.length>0;
    var f=on?w[0][0]:"10:00",t=on?w[0][1]:"19:00";
    return '<div class="day'+(on?"":" off")+'" data-d="'+d[0]+'"><span class="nm">'+d[1]+'</span>'
      +'<input type="checkbox" '+(on?"checked":"")+' onchange="toggleDay(this)"> '
      +'<input type="time" class="fr" value="'+f+'"> <span class="to">to</span> <input type="time" class="tt" value="'+t+'"></div>';}).join("");
}
function toggleDay(cb){var row=cb.closest(".day");row.classList.toggle("off",!cb.checked);}
function renderModes(){var all=["Phone","Video","In-person"];
  el("modes").innerHTML=all.map(function(m){var on=(AV.modes||[]).indexOf(m)>=0;
    return '<label><input type="checkbox" value="'+m+'" '+(on?"checked":"")+'> '+m+'</label>';}).join("");}
function renderBlocked(){el("blocked").innerHTML=(AV.blocked_dates||[]).map(function(d){
  return '<span class="chip">'+d+' <b onclick="rmBlocked(\\''+d+'\\')">&times;</b></span>';}).join("");}
function addBlocked(){var v=el("bd").value;if(!v)return;AV.blocked_dates=AV.blocked_dates||[];if(AV.blocked_dates.indexOf(v)<0)AV.blocked_dates.push(v);AV.blocked_dates.sort();renderBlocked();}
function rmBlocked(d){AV.blocked_dates=AV.blocked_dates.filter(function(x){return x!==d;});renderBlocked();}
function copyLink(){var i=el("pub");i.select();navigator.clipboard.writeText(i.value).then(function(){toast("Link copied");});}
function saveAv(){
  var wh={};document.querySelectorAll(".day").forEach(function(row){var d=row.getAttribute("data-d");
    if(row.classList.contains("off")){wh[d]=[];}else{wh[d]=[[row.querySelector(".fr").value,row.querySelector(".tt").value]];}});
  var modes=[];document.querySelectorAll("#modes input:checked").forEach(function(c){modes.push(c.value);});
  var body={enabled:el("en").checked,weekly_hours:wh,slot_minutes:+el("slot").value,buffer_minutes:+el("buf").value,
    min_notice_hours:+el("notice").value,horizon_days:+el("hz").value,blocked_dates:AV.blocked_dates||[],modes:modes,
    default_location:el("loc").value,headline:el("hl").value};
  fetch("/api/scheduler/availability",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})
  .then(r=>r.json()).then(function(j){AV=j.availability;toast("Saved");}).catch(function(){toast("Save failed");});
}
function setScope(s){scope=s;el("tb-up").classList.toggle("on",s==="upcoming");el("tb-pa").classList.toggle("on",s==="past");loadMeetings();}
function loadMeetings(){if(el("mlist"))el("mlist").innerHTML='<div class="spin"></div>';
  fetch("/api/scheduler/meetings?scope="+scope).then(r=>r.json()).then(function(j){MEET=(j&&j.meetings)||[];if(!AV.public_url){AV.public_url=location.origin+"/book/"+(AV.token||"");}if(!el("days"))render();else renderMeetings();}).catch(function(){});}
function renderMeetings(){var box=el("mlist");if(!box)return;
  if(!MEET.length){box.innerHTML='<div class="empty">No '+scope+' bookings yet.<br>Share your link to get started.</div>';return;}
  box.innerHTML=MEET.map(function(m){
    var link=m.candidate_name?('<span class="pill link">'+esc(m.candidate_name)+'</span>'):(m.crm_client_id?'<span class="pill link">Client</span>':'');
    var st=m.status==="cancelled"?'<span class="pill cx">Cancelled</span>':(m.status==="completed"?'<span class="pill done">Done</span>':(m.status==="no_show"?'<span class="pill cx">No-show</span>':'<span class="pill">Confirmed</span>'));
    var acts="";
    if(m.status==="confirmed"){
      if(m.guest_phone)acts+='<button class="btn sm wa" onclick="waConfirm('+m.id+')">WhatsApp</button>';
      if(scope==="past"||true){acts+='<button class="btn sm ghost" onclick="mstatus('+m.id+',\\'completed\\')">Done</button>';
      acts+='<button class="btn sm ghost" onclick="mstatus('+m.id+',\\'no_show\\')">No-show</button>';}
      acts+='<button class="btn sm warn" onclick="mstatus('+m.id+',\\'cancelled\\')">Cancel</button>';
    }
    return '<div class="mtg"><div class="when">'+whenFmt(m.start_at)+'</div>'
      +'<div class="who">'+esc(m.guest_name||"Guest")+(m.guest_phone?' &middot; '+esc(m.guest_phone):"")+'</div>'
      +'<div class="meta">'+st+link+(m.mode?'<span>'+esc(m.mode)+'</span>':"")+(m.purpose?'<span>&middot; '+esc(m.purpose)+'</span>':"")+'</div>'
      +(acts?'<div class="acts">'+acts+'</div>':"")+'</div>';}).join("");
}
function waConfirm(id){var m=MEET.filter(function(x){return x.id===id;})[0];if(!m)return;
  var ph=(m.guest_phone||"").replace(/[^0-9]/g,"");if(ph.length===10)ph="91"+ph;
  var msg="Hi "+(m.guest_name||"")+", confirming our meeting on "+whenFmt(m.start_at)+" IST"+(m.mode?" ("+m.mode+")":"")+". — "+(m.host_name||"HireLab");
  window.open("https://wa.me/"+ph+"?text="+encodeURIComponent(msg),"_blank");}
function mstatus(id,st){if(st==="cancelled"&&!confirm("Cancel this booking?"))return;
  fetch("/api/scheduler/meetings/"+id+"/status",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({status:st})})
  .then(r=>r.json()).then(function(){toast("Updated");loadMeetings();});}
boot();
</script></body></html>"""


@pages.route('/schedule', methods=['GET'])
@login_required
def scheduler_console():
    """Recruiter console: availability + bookings. Auth via session (redirects
    to /login if not signed in)."""
    return _render_page_console(_CONSOLE_HTML)


def _render_page_console(html):
    return Response(html.replace('__CSS__', _CONSOLE_CSS), mimetype='text/html')
