"""
Google Meet invites for scheduler bookings — permanent Meet link, no Google
Cloud setup.

Each recruiter saves ONE permanent Google Meet link (Settings → My Account →
Google Meet). When a guest books a VIDEO slot with that recruiter (public /book
page, or the recruiter books it in the ATS), the ATS e-mails the guest a real
calendar invite (.ics, METHOD:REQUEST) FROM THE RECRUITER'S OWN MAILBOX, with
the Meet link as the location. Gmail / Outlook show it with Yes / No buttons and
put it in the guest's calendar. A reschedule sends an updated invite (same UID,
higher SEQUENCE) and a cancel sends METHOD:CANCEL, so the guest's calendar
follows along. A copy goes to the recruiter too.

Sender: the recruiter's own Gmail (My Email in Settings) when set, else the
company mailbox (Settings → Email Configuration) — the same rule as every other
e-mail the ATS sends.

Phone / In-person bookings, and recruiters without a Meet link, are untouched.

Additive only: one column on meeting_availability (meet_link) and three on
meetings (meet_link, invite_seq, google_error).

ROLLBACK: remove 'google_meet' from modules/__init__.py. The scheduler hooks
then do nothing (their import fails softly) and bookings behave as before.
"""

import re
import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr, make_msgid

from flask import Blueprint, request, jsonify

from modules.shared import get_db, ts, effective_company_id, real_user_id, login_required, _core
from modules import register_migration

bp = Blueprint('google_meet', __name__)

MEET_RX = re.compile(r'^https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}$')


@register_migration
def _migrate(conn):
    for table, col, typ in (('meeting_availability', 'meet_link', "TEXT DEFAULT ''"),
                            ('meetings', 'meet_link', "TEXT DEFAULT ''"),
                            ('meetings', 'invite_seq', 'INTEGER DEFAULT 0'),
                            ('meetings', 'google_error', "TEXT DEFAULT ''")):
        try:
            conn.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, col, typ))
        except Exception:
            pass
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  pure helpers (unit tested)
# ══════════════════════════════════════════════════════════════════════════
def clean_meet_link(v):
    """'meet.google.com/abc-defg-hij' (any casing, with or without https,
    with ?authuser etc.) -> 'https://meet.google.com/abc-defg-hij', or ''."""
    s = str(v or '').strip()
    m = re.search(r'meet\.google\.com/([a-zA-Z]{3}-[a-zA-Z]{4}-[a-zA-Z]{3})', s)
    if not m:
        return ''
    return 'https://meet.google.com/' + m.group(1).lower()


def wants_meet(m):
    return 'video' in str(m['mode'] or '').lower()


def _ics_escape(s):
    return (str(s or '').replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,')
            .replace('\r\n', '\\n').replace('\n', '\\n'))


def _fold(line):
    """RFC 5545: lines over 75 octets continue on the next line after a space."""
    out, cur = [], ''
    for ch in line:
        if len((cur + ch).encode('utf-8')) > 74:
            out.append(cur)
            cur = ' ' + ch
        else:
            cur += ch
    out.append(cur)
    return '\r\n'.join(out)


def _utc(iso_local):
    dt = datetime.datetime.fromisoformat(str(iso_local)[:16])
    dt = dt - datetime.timedelta(hours=5, minutes=30)          # IST -> UTC (no DST in India)
    return dt.strftime('%Y%m%dT%H%M%SZ')


def build_ics(m, method, seq, host_name, host_email, meet_link, manage_url, summary):
    status = 'CANCELLED' if method == 'CANCEL' else 'CONFIRMED'
    desc = ['Join Google Meet: ' + meet_link]
    if (m['purpose'] or '').strip():
        desc.append('About: ' + m['purpose'].strip())
    if manage_url and method != 'CANCEL':
        desc.append('Reschedule or cancel: ' + manage_url)
    lines = [
        'BEGIN:VCALENDAR', 'PRODID:-//HireLab//Scheduler//EN', 'VERSION:2.0', 'CALSCALE:GREGORIAN',
        'METHOD:' + method, 'BEGIN:VEVENT',
        'UID:hirelab-meeting-%s@hirelab' % m['id'],
        'SEQUENCE:%d' % seq,
        'DTSTAMP:' + datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ'),
        'DTSTART:' + _utc(m['start_at']),
        'DTEND:' + _utc(m['end_at'] or m['start_at']),
        'SUMMARY:' + _ics_escape(summary),
        'DESCRIPTION:' + _ics_escape('\n'.join(desc)),
        'LOCATION:' + _ics_escape(meet_link),
        'URL:' + meet_link,
        'STATUS:' + status,
        'ORGANIZER;CN=%s:mailto:%s' % (_ics_escape(host_name), host_email),
    ]
    if (m['guest_email'] or '').strip():
        lines.append('ATTENDEE;CN=%s;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:%s'
                     % (_ics_escape(m['guest_name'] or 'Guest'), m['guest_email'].strip()))
    lines.append('ATTENDEE;CN=%s;ROLE=CHAIR;PARTSTAT=ACCEPTED:mailto:%s' % (_ics_escape(host_name), host_email))
    if method != 'CANCEL':
        lines += ['BEGIN:VALARM', 'TRIGGER:-PT15M', 'ACTION:DISPLAY', 'DESCRIPTION:Reminder', 'END:VALARM']
    lines += ['END:VEVENT', 'END:VCALENDAR']
    return '\r\n'.join(_fold(l) for l in lines) + '\r\n'


def _nice(iso):
    try:
        return datetime.datetime.fromisoformat(str(iso)[:16]).strftime('%a %d %b %Y, %I:%M %p')
    except Exception:
        return str(iso)


def build_message(m, method, seq, host_name, host_email, meet_link, manage_url, kind):
    """kind: new | moved | cancelled"""
    when = _nice(m['start_at'])
    guest = (m['guest_name'] or 'there').strip()
    summary = 'Meeting: %s <> %s' % (m['guest_name'] or 'Guest', host_name)
    subject = {'new': 'Invitation: %s — %s IST' % (summary, when),
               'moved': 'Updated invitation: %s — now %s IST' % (summary, when),
               'cancelled': 'Cancelled: %s — %s IST' % (summary, when)}[kind]
    if kind == 'cancelled':
        plain = 'Hi %s,\n\nThe meeting with %s on %s (IST) has been cancelled.\n\n— %s' % (guest, host_name, when, host_name)
        html = ('<div style="font-family:sans-serif;font-size:14px;color:#1c1c1c">'
                '<p>Hi %s,</p><p>The meeting with <b>%s</b> on <b>%s</b> (IST) has been cancelled.</p><p>— %s</p></div>'
                % (guest, host_name, when, host_name))
    else:
        lead = 'Your meeting with %s is confirmed.' % host_name if kind == 'new' else \
               'Your meeting with %s has moved to a new time.' % host_name
        plain = ('Hi %s,\n\n%s\n\nWhen: %s (IST)\nJoin Google Meet: %s\n%s\n— %s'
                 % (guest, lead, when, meet_link,
                    ('Reschedule or cancel: %s\n' % manage_url) if manage_url else '', host_name))
        html = ('<div style="font-family:sans-serif;font-size:14px;color:#1c1c1c;max-width:520px">'
                '<p>Hi %s,</p><p>%s</p>'
                '<table style="font-size:14px;line-height:1.9"><tr><td style="color:#888">When</td>'
                '<td style="padding-left:14px"><b>%s</b> (IST)</td></tr>'
                '<tr><td style="color:#888">Google Meet</td><td style="padding-left:14px">'
                '<a href="%s">%s</a></td></tr></table>'
                '<p style="margin-top:16px"><a href="%s" style="display:inline-block;background:#1A73E8;color:#fff;'
                'padding:9px 16px;border-radius:8px;text-decoration:none;font-weight:600">Join Google Meet</a>%s</p>'
                '<p>— %s</p></div>'
                % (guest, lead, when, meet_link, meet_link.replace('https://', ''), meet_link,
                   ('<a href="%s" style="margin-left:12px;color:#0F6E56;text-decoration:none">Reschedule / cancel</a>'
                    % manage_url) if manage_url else '', host_name))
    ics = build_ics(m, method, seq, host_name, host_email, meet_link, manage_url, summary)

    msg = MIMEMultipart('mixed')
    alt = MIMEMultipart('alternative')
    alt.attach(MIMEText(plain, 'plain', 'utf-8'))
    alt.attach(MIMEText(html, 'html', 'utf-8'))
    cal = MIMEText(ics, 'calendar', 'utf-8')
    cal.set_param('method', method)
    alt.attach(cal)
    msg.attach(alt)
    att = MIMEBase('application', 'ics', name='invite.ics')
    att.set_payload(ics.encode('utf-8'))
    encoders.encode_base64(att)
    att.add_header('Content-Disposition', 'attachment', filename='invite.ics')
    msg.attach(att)
    msg['Subject'] = subject
    msg['From'] = formataddr((host_name, host_email))
    msg['To'] = m['guest_email'].strip() if (m['guest_email'] or '').strip() else host_email
    msg['Message-ID'] = make_msgid(domain='hirelab')
    return msg


# ══════════════════════════════════════════════════════════════════════════
#  sending
# ══════════════════════════════════════════════════════════════════════════
def _sender(conn, host_uid, company_id):
    """(email, password, display name) the invite goes out from: the host's
    own mailbox when set, else the company mailbox."""
    core = _core()
    u = conn.execute('SELECT display_name, username FROM users WHERE id=?', (host_uid,)).fetchone()
    name = ((u['display_name'] or u['username']) if u else '') or 'HireLab'
    try:
        ident = core._mail_identity(host_uid)
    except Exception:
        ident = None
    if ident and ident.get('mailbox'):
        return ident['email'], ident['password'], name
    em = core.get_setting_for(conn, company_id, 'smtp_email', '')
    pw = core.get_setting_for(conn, company_id, 'smtp_app_password', '')
    return em, pw, name


def _smtp_host(addr):
    a = (addr or '').lower()
    if '@outlook' in a or '@hotmail' in a or '@live' in a:
        return 'smtp-mail.outlook.com'
    if '@yahoo' in a:
        return 'smtp.mail.yahoo.com'
    return 'smtp.gmail.com'


def send_message(msg, email, password, recipients):
    core = _core()
    srv, _p = core.smtp_open(_smtp_host(email), 587, email, password)
    try:
        srv.sendmail(email, recipients, msg.as_string())
    finally:
        try:
            srv.quit()
        except Exception:
            pass


def host_meet_link(conn, uid):
    r = conn.execute('SELECT meet_link FROM meeting_availability WHERE user_id=?', (uid,)).fetchone()
    return clean_meet_link(r['meet_link']) if r else ''


def sync_meeting(conn, meeting_id, base_url=''):
    """Send / update / cancel the calendar invite for one meeting so the guest's
    calendar matches the ATS. Best-effort: never raises.
    Returns {'state': 'skipped'|'created'|'updated'|'cancelled'|'error', ...}."""
    try:
        m = conn.execute('SELECT * FROM meetings WHERE id=?', (meeting_id,)).fetchone()
        if not m:
            return {'state': 'skipped', 'why': 'no meeting'}
        seq = int(m['invite_seq'] or 0)
        cancelled = m['status'] == 'cancelled'
        if cancelled and seq == 0:
            return {'state': 'skipped', 'why': 'no invite was sent'}
        if not cancelled and not wants_meet(m) and seq == 0:
            return {'state': 'skipped', 'why': 'not a video meeting'}
        link = clean_meet_link(m['meet_link']) or host_meet_link(conn, m['host_user_id'])
        if not link:
            return {'state': 'skipped', 'why': 'host has no Meet link'}
        if not cancelled and seq == 0:
            # the Meet link IS where a video meeting happens
            conn.execute('UPDATE meetings SET meet_link=?, location=? WHERE id=?', (link, link, meeting_id))
            conn.commit()
        if not (m['guest_email'] or '').strip():
            return {'state': 'skipped', 'why': 'guest gave no email', 'meet_link': link}
        email, pw, name = _sender(conn, m['host_user_id'], m['company_id'])
        if not email or not pw:
            raise RuntimeError('no mailbox to send from — set My Email or the company Email Configuration')
        manage = (base_url.rstrip('/') + '/book/manage/' + m['manage_token']) if base_url and m['manage_token'] else ''
        if cancelled:
            method, kind = 'CANCEL', 'cancelled'
        elif seq == 0:
            method, kind = 'REQUEST', 'new'
        else:
            method, kind = 'REQUEST', 'moved'
        m2 = dict(m)
        msg = build_message(m2, method, seq + 1, name, email, link, manage, kind)
        recipients = [m['guest_email'].strip()]
        if email.lower() != m['guest_email'].strip().lower():
            recipients.append(email)              # the host's own copy
        send_message(msg, email, pw, recipients)
        conn.execute("UPDATE meetings SET invite_seq=?, google_error='' WHERE id=?", (seq + 1, meeting_id))
        conn.commit()
        return {'state': {'new': 'created', 'moved': 'updated', 'cancelled': 'cancelled'}[kind],
                'meet_link': link, 'invite_sent': True, 'from': email}
    except Exception as e:
        msg = '%s: %s' % (type(e).__name__, str(e)[:200])
        print('[google-meet] meeting %s invite failed: %s' % (meeting_id, msg))
        try:
            conn.execute('UPDATE meetings SET google_error=? WHERE id=?', (msg, meeting_id))
            conn.commit()
        except Exception:
            pass
        return {'state': 'error', 'error': msg}


def public_base():
    base = request.host_url.rstrip('/')
    if request.host.split(':')[0] not in ('localhost', '127.0.0.1'):
        base = base.replace('http://', 'https://', 1)
    return base


# ══════════════════════════════════════════════════════════════════════════
#  routes
# ══════════════════════════════════════════════════════════════════════════
def _ensure_avail(conn, uid):
    if not conn.execute('SELECT 1 FROM meeting_availability WHERE user_id=?', (uid,)).fetchone():
        try:
            from modules.scheduler import _row_or_default
            _row_or_default(conn, uid, effective_company_id())
        except Exception:
            conn.execute('INSERT OR IGNORE INTO meeting_availability (user_id, company_id) VALUES (?,?)',
                         (uid, effective_company_id()))
            conn.commit()


@bp.route('/api/me/meet-link', methods=['GET'])
@login_required
def get_link():
    uid = int(real_user_id() or 0)
    conn = get_db()
    try:
        email, pw, name = _sender(conn, uid, effective_company_id())
        return jsonify({'ok': True, 'meet_link': host_meet_link(conn, uid),
                        'sender': email if (email and pw) else ''})
    finally:
        conn.close()


@bp.route('/api/me/meet-link', methods=['PUT', 'POST'])
@login_required
def set_link():
    d = request.get_json(silent=True) or {}
    raw = str(d.get('meet_link') or '').strip()
    link = clean_meet_link(raw)
    if raw and not link:
        return jsonify({'ok': False, 'error': 'That is not a Google Meet link. It looks like meet.google.com/abc-defg-hij'}), 400
    uid = int(real_user_id() or 0)
    conn = get_db()
    try:
        _ensure_avail(conn, uid)
        conn.execute('UPDATE meeting_availability SET meet_link=? WHERE user_id=?', (link, uid))
        conn.commit()
        return jsonify({'ok': True, 'meet_link': link})
    finally:
        conn.close()


@bp.route('/api/me/meet-link/test', methods=['POST'])
@login_required
def test_invite():
    """Send a sample invite to the recruiter's own inbox (tomorrow 11:00 IST)."""
    uid = int(real_user_id() or 0)
    conn = get_db()
    try:
        link = host_meet_link(conn, uid)
        if not link:
            return jsonify({'ok': False, 'error': 'Save your Google Meet link first.'}), 400
        email, pw, name = _sender(conn, uid, effective_company_id())
        if not email or not pw:
            return jsonify({'ok': False, 'error': 'No mailbox to send from. Set My Email (or the company Email Configuration) first.'}), 400
        day = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        m = {'id': 'test-%s' % uid, 'start_at': day + 'T11:00', 'end_at': day + 'T11:30', 'guest_name': name,
             'guest_email': email, 'purpose': 'Test invite from HireLab', 'manage_token': ''}
        msg = build_message(m, 'REQUEST', 1, name, email, link, '', 'new')
        try:
            send_message(msg, email, pw, [email])
        except Exception as e:
            return jsonify({'ok': False, 'error': 'Could not send: %s' % e}), 502
        return jsonify({'ok': True, 'message': 'Test invite sent to %s — check your inbox.' % email})
    finally:
        conn.close()


@bp.route('/api/google/meetings/<int:mid>/retry', methods=['POST'])
@login_required
def retry(mid):
    conn = get_db()
    try:
        m = conn.execute('SELECT id FROM meetings WHERE id=? AND company_id=?', (mid, effective_company_id())).fetchone()
        if not m:
            return jsonify({'error': 'Not found'}), 404
        res = sync_meeting(conn, mid, public_base())
        return jsonify({'ok': res.get('state') != 'error', **res})
    finally:
        conn.close()
