"""
Market Pulse — the HireLab candidate newsletter (Wave 3 of candidate engagement).

One ISSUE = one audience (a Talent Pool segment) + one piece of content.

  1. AUDIENCE  Pick segments exactly as in the Talent Pool segment builder
               (industry / function / seniority / region / CV age). Always:
               e-mailable only (has e-mail, not unsubscribed, not switched off).
               Default: leave out people live in a pipeline.
  2. CONTENT   Either type 4–5 points (news, hiring trends, salary moves) and let
               DeepSeek draft subject + intro + sections + closing for THAT segment,
               or write it yourself. Everything stays editable.
  3. PERSONAL  Every mail is personalised: first name, and a profile block — if
               the CV is old/missing: "your profile was last updated N months ago,
               refresh it in 2 minutes" + personal update link; if fresh: "open to
               new roles? keep your preferences current". This is what keeps the
               database fresh (objective 1) while the content builds the brand
               (objective 2).
  4. APPROVE   Preview, send yourself a test, approve. Nothing goes out before.
  5. SEND      Background thread, company mailbox, Mon–Sat in the window, daily cap.
               Frequency guard: anyone who got ANY HireLab mail (job mail, re-engage,
               another newsletter) inside the gap window is skipped — the newsletter
               is the lowest priority mail. Re-engagement also waits for people who
               just got a newsletter (the newsletter already carries the CV ask).
  6. RESULTS   Sent, opened, clicked, profiles updated after the mail, unsubscribed.

Additive only: new tables newsletter_settings, newsletter_issues, newsletter_recipients.
"""

import datetime
import hashlib
import hmac
import html
import json
import math
import os
import re
import sys
import threading
import time

from flask import Blueprint, request, jsonify, redirect, Response

from modules.shared import get_db, ts, effective_company_id, real_user_id, is_company_admin, login_required, _core
from modules import register_migration

bp = Blueprint('newsletter', __name__, url_prefix='/api/newsletter')
pages = Blueprint('newsletter_pages', __name__)

IST = datetime.timedelta(hours=5, minutes=30)
DEFAULTS = {'daily_cap': 150, 'start_hour': 10, 'end_hour': 19}
TICK_SECONDS = 120
MAX_PER_TICK = 10
DEFAULT_AUDIENCE = {'industries': [], 'functions': [], 'seniority': [], 'regions': [], 'freshness': '',
                    'exclude_live': True}


@register_migration
def _migrate_newsletter(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS newsletter_settings (
        company_id INTEGER PRIMARY KEY,
        daily_cap INTEGER DEFAULT 150,
        start_hour INTEGER DEFAULT 10,
        end_hour INTEGER DEFAULT 19,
        tick_at TEXT DEFAULT '',
        last_error TEXT DEFAULT '',
        updated_by INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT ''
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS newsletter_issues (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        title TEXT DEFAULT '',
        status TEXT DEFAULT 'draft',
        audience TEXT DEFAULT '{}',
        points TEXT DEFAULT '',
        subject TEXT DEFAULT '',
        preheader TEXT DEFAULT '',
        intro TEXT DEFAULT '',
        sections TEXT DEFAULT '[]',
        closing TEXT DEFAULT '',
        ai_status TEXT DEFAULT '',
        ai_error TEXT DEFAULT '',
        created_by INTEGER DEFAULT 0,
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        approved_by INTEGER DEFAULT 0,
        approved_at TEXT DEFAULT '',
        finished_at TEXT DEFAULT '',
        queued INTEGER DEFAULT 0,
        skipped_guard INTEGER DEFAULT 0
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS newsletter_recipients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        issue_id INTEGER NOT NULL,
        company_id INTEGER NOT NULL,
        candidate_id INTEGER NOT NULL,
        email TEXT NOT NULL,
        name TEXT DEFAULT '',
        cv_date TEXT DEFAULT '',
        status TEXT DEFAULT 'queued',
        skip_reason TEXT DEFAULT '',
        error TEXT DEFAULT '',
        sent_at TEXT DEFAULT '',
        opened_at TEXT DEFAULT '',
        clicked_at TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        UNIQUE(issue_id, email)
    )''')
    # Designed e-mail (Oct 2026): a pasted, formatted e-mail (tables, colours,
    # images) sent as-is instead of intro + sections + closing.
    for col, defn in (('body_html', "TEXT DEFAULT ''"), ('cv_box', 'INTEGER DEFAULT 1')):
        try:
            conn.execute('ALTER TABLE newsletter_issues ADD COLUMN %s %s' % (col, defn))
        except Exception:
            pass
    for sql in ('CREATE INDEX IF NOT EXISTS idx_nl_rec_status ON newsletter_recipients(company_id, status)',
                'CREATE INDEX IF NOT EXISTS idx_nl_rec_email ON newsletter_recipients(company_id, email, sent_at)',
                'CREATE INDEX IF NOT EXISTS idx_nl_rec_issue ON newsletter_recipients(issue_id)'):
        conn.execute(sql)
    conn.commit()


# ── helpers ────────────────────────────────────────────────────────────────
def now_ist():
    return datetime.datetime.utcnow() + IST


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _err(msg, code=400):
    return jsonify({'ok': False, 'error': msg}), code


def _oid():
    return int(effective_company_id() or 0)


def _jl(v, default=None):
    try:
        x = json.loads(v or '')
        return x
    except Exception:
        return default


def get_settings(conn, oid):
    r = conn.execute('SELECT * FROM newsletter_settings WHERE company_id=?', (oid,)).fetchone()
    s = dict(DEFAULTS, last_error='')
    if r:
        for k in list(s):
            if r[k] is not None:
                s[k] = r[k]
    return s


def gap_days(conn, oid):
    """One guard setting for every HireLab mail — lives in Job Mail settings."""
    try:
        from modules.jobmail import get_settings as jm
        return int(jm(conn, oid)['gap_days'])
    except Exception:
        return 7


def first_name(name):
    n = str(name or '').strip()
    for t in ('mr.', 'mrs.', 'ms.', 'dr.', 'er.', 'mr', 'mrs', 'ms', 'dr', 'er'):
        if n.lower().startswith(t + ' '):
            n = n[len(t) + 1:].strip()
            break
    return (n.split() or ['there'])[0].strip(',').title()


def _sign(raw):
    key = _core().app.secret_key
    key = key if isinstance(key, bytes) else str(key or 'hirelab').encode()
    return hmac.new(key, ('nl:' + raw).encode(), hashlib.sha256).hexdigest()[:16]


def token_for(rid):
    return '%d-%s' % (rid, _sign(str(rid)))


def parse_token(tok):
    try:
        rid, sig = str(tok).split('-', 1)
        rid = int(rid)
    except Exception:
        return None
    return rid if hmac.compare_digest(_sign(str(rid)), sig) else None


def _base():
    try:
        return _core()._camp_base_url().rstrip('/')
    except Exception:
        return (os.environ.get('PUBLIC_BASE_URL', '') or 'https://hirelabscreener.onrender.com').rstrip('/')


def _has_signature():
    try:
        return bool((_core()._build_signature() or ('', ''))[0])
    except Exception:
        return False


def _issue(conn, oid, iid):
    r = conn.execute('SELECT * FROM newsletter_issues WHERE id=? AND company_id=?', (iid, oid)).fetchone()
    return dict(r) if r else None


def _audience(issue):
    a = dict(DEFAULT_AUDIENCE)
    a.update(_jl(issue.get('audience'), {}) or {})
    return a


def recent_mail_emails(conn, oid, days, exclude_issue=0):
    """Every address that got ANY HireLab mail in the last `days` days (or has one queued)."""
    out = set()
    try:
        from modules.jobmail import recent_mail_map
        out |= set(recent_mail_map(conn, oid, days).keys())
    except Exception:
        pass
    since = (now_ist() - datetime.timedelta(days=int(days))).strftime('%Y-%m-%dT%H:%M:%S')
    for r in conn.execute("SELECT LOWER(TRIM(email)) FROM newsletter_recipients WHERE company_id=? AND issue_id<>? AND "
                          "((status='sent' AND sent_at>=?) OR status IN ('queued','sending'))", (oid, exclude_issue, since)):
        out.add(r[0])
    return out


def mailed_recently(conn, oid):
    """Addresses with a newsletter sent/queued inside the guard window (used by re-engagement)."""
    since = (now_ist() - datetime.timedelta(days=gap_days(conn, oid))).strftime('%Y-%m-%dT%H:%M:%S')
    return {r[0] for r in conn.execute("SELECT LOWER(TRIM(email)) FROM newsletter_recipients WHERE company_id=? AND "
                                       "((status='sent' AND sent_at>=?) OR status IN ('queued','sending'))", (oid, since))}


def audience_people(conn, oid, aud):
    """Talent Pool people for this audience — e-mailable only."""
    from modules import segments as sg
    f = sg._clean_filter(dict(aud, emailable_only=True))
    people = sg.segment_people(conn, oid, f)
    try:                    # per-job "automated e-mails OFF" (modules/mail_guard.py) — live, not only at the last scan
        from modules import mail_guard as _mg
        _ids, _ems, _ = _mg.blocked(conn, oid)
        if _ids or _ems:
            people = [p for p in people if (p.get('email') or '').lower() not in _ems
                      and int(p.get('candidate_id') or 0) not in _ids]
    except Exception:
        pass
    return people


# ══════════════════════════════════════════════════════════════════════════
#  AI draft
# ══════════════════════════════════════════════════════════════════════════
AI_SYSTEM = (
    'You write "Market Pulse", a short monthly e-mail newsletter from HireLab, a recruitment agency in India for '
    'Data Center, Solar & Renewables, Electrical & Power, Industrial Automation and Testing/Inspection/Certification '
    'roles. Readers are engineers and managers in our talent database. Tone: warm, expert, factual, no hype, no '
    'emojis, Indian English. Use ONLY the facts in the recruiter\'s points — never invent numbers, companies or news; '
    'if a point is vague keep it general. Do not mention specific job openings or promise jobs. 2–4 short sections, '
    'each 40–90 words, plain text (no markdown, no bullet symbols). Reply ONLY with JSON: '
    '{"subject":"<max 70 chars>","preheader":"<max 110 chars>","intro":"<1–2 sentences>",'
    '"sections":[{"heading":"<max 8 words>","body":"<text>"}],"closing":"<1–2 sentences>"}')


def _audience_words(aud):
    from modules import segments as sg
    parts = []
    if aud.get('industries'):
        parts.append('industries: ' + ', '.join(sg.IND_LABELS.get(k, k) for k in aud['industries']))
    if aud.get('functions'):
        parts.append('functions: ' + ', '.join(sg.FUN_LABELS.get(k, k) for k in aud['functions']))
    if aud.get('seniority'):
        parts.append('seniority: ' + ', '.join(sg.SEN_LABELS.get(k, k) for k in aud['seniority']))
    if aud.get('regions'):
        parts.append('regions: ' + ', '.join(sg.REGION_LABELS.get(k, k) for k in aud['regions']))
    return '; '.join(parts) or 'all candidates across our sectors'


def _ask_ai(core, key, points, aud, month):
    user = ('Issue month: %s\nAudience: %s\nRecruiter\'s points:\n%s' % (month, _audience_words(aud), points.strip()))
    payload = {'model': 'deepseek-chat', 'temperature': 0.4, 'max_tokens': 1400, 'response_format': {'type': 'json_object'},
               'messages': [{'role': 'system', 'content': AI_SYSTEM}, {'role': 'user', 'content': user}]}
    resp = core.call_deepseek(key, payload, timeout=90, endpoint='newsletter-draft')
    if resp.status_code != 200:
        raise RuntimeError('DeepSeek returned HTTP %s' % resp.status_code)
    raw = resp.json()['choices'][0]['message']['content']
    try:
        j = json.loads(raw)
    except Exception:
        s, e = raw.find('{'), raw.rfind('}')
        j = json.loads(raw[s:e + 1]) if 0 <= s < e else {}
    if not isinstance(j, dict) or not j.get('subject'):
        raise ValueError('The AI reply could not be read. Try again.')
    secs = []
    for x in (j.get('sections') or [])[:5]:
        if isinstance(x, dict) and (x.get('heading') or x.get('body')):
            secs.append({'heading': str(x.get('heading') or '')[:90], 'body': str(x.get('body') or '')[:1500]})
    return {'subject': str(j.get('subject') or '')[:120], 'preheader': str(j.get('preheader') or '')[:160],
            'intro': str(j.get('intro') or '')[:800], 'sections': secs, 'closing': str(j.get('closing') or '')[:600]}


# ══════════════════════════════════════════════════════════════════════════
#  The e-mail
# ══════════════════════════════════════════════════════════════════════════
def cv_block(p, fresh_days, now=None):
    """(heading, text, button) — the personal profile ask."""
    now = now or now_ist()
    d = str(p.get('cv_date') or '')[:10]
    days = None
    if len(d) == 10:
        try:
            days = (now - datetime.datetime.strptime(d, '%Y-%m-%d')).days
        except ValueError:
            days = None
    if days is None:
        return ('Is your profile with us complete?',
                'We do not have a CV on file for you yet. Add your latest CV and details — it takes about 2 minutes '
                'and helps us match you with the right openings.', 'Add my CV')
    if days > int(fresh_days):
        months = max(1, round(days / 30.0))
        when = ('%d months' % months) if months < 12 else ('over %s' % ('a year' if months < 24 else '%d years' % (months // 12)))
        return ('Your profile was last updated %s ago' % when,
                'Roles, salaries and notice periods change fast. Refresh your current role, CTC, notice period and CV '
                '— it takes about 2 minutes and keeps you in front of the right openings.', 'Refresh my profile')
    return ('Open to the right move?',
            'Your profile with us is up to date — thank you. If anything has changed (role, CTC, location or what you '
            'are looking for), you can update it any time.', 'Update my preferences')


# ══════════════════════════════════════════════════════════════════════════
#  Designed e-mail: clean pasted HTML (allow-list) + host pasted images
# ══════════════════════════════════════════════════════════════════════════
MAX_BODY_HTML = 400000
MAX_IMG_BYTES = 3 * 1024 * 1024
_ALLOWED_TAGS = {'a', 'b', 'strong', 'i', 'em', 'u', 's', 'p', 'br', 'hr', 'div', 'span', 'h1', 'h2', 'h3', 'h4', 'h5',
                 'h6', 'ul', 'ol', 'li', 'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'caption', 'col',
                 'colgroup', 'img', 'blockquote', 'pre', 'code', 'small', 'sup', 'sub', 'font', 'center'}
_VOID_TAGS = {'br', 'hr', 'img', 'col'}
_DROP_WITH_CONTENT = {'script', 'style', 'iframe', 'object', 'embed', 'form', 'input', 'button', 'select', 'textarea',
                      'svg', 'math', 'noscript', 'template', 'head', 'title', 'meta', 'link', 'base', 'frame', 'frameset'}
_ALLOWED_ATTRS = {'style', 'href', 'src', 'alt', 'title', 'width', 'height', 'align', 'valign', 'bgcolor', 'color',
                  'colspan', 'rowspan', 'cellpadding', 'cellspacing', 'border', 'role', 'face', 'size', 'target', 'dir'}
_BAD_CSS = re.compile(r'expression\s*\(|javascript:|vbscript:|behavior\s*:|-moz-binding|@import', re.I)
_DATA_IMG = re.compile(r'^data:image/(png|jpe?g|gif|webp);base64,([A-Za-z0-9+/=\s]+)$', re.I)
_IMG_NAME = re.compile(r'^[0-9]+_[0-9a-f]{20}\.(png|jpg|gif|webp)$')


def _img_dir():
    d = os.path.join(_core().DATA_DIR, 'newsletter_images')
    os.makedirs(d, exist_ok=True)
    return d


def save_image(oid, raw_bytes, ext):
    """Store one image; returns its file name (content-addressed, so re-saving is free)."""
    ext = 'jpg' if ext in ('jpeg', 'jpg') else ext
    name = '%d_%s.%s' % (int(oid), hashlib.sha256(raw_bytes).hexdigest()[:20], ext)
    fp = os.path.join(_img_dir(), name)
    if not os.path.exists(fp):
        with open(fp, 'wb') as fh:
            fh.write(raw_bytes)
    return name


def _safe_url(v, oid, for_img):
    v = (v or '').strip()
    if for_img:
        m = _DATA_IMG.match(v)
        if m:
            import base64
            try:
                raw = base64.b64decode(re.sub(r'\s+', '', m.group(2)), validate=False)
            except Exception:
                return ''
            if not raw or len(raw) > MAX_IMG_BYTES:
                return ''
            # inline images are blocked by Gmail / Outlook: host them and link instead
            return '%s/nl/img/%s' % (_base(), save_image(oid, raw, m.group(1).lower()))
        return v if re.match(r'^https?://', v, re.I) else ''
    return v if re.match(r'^(https?://|mailto:|tel:|#)', v, re.I) else ''


def clean_html(raw, oid):
    """Pasted e-mail HTML -> safe HTML for sending: allow-listed tags and
    attributes, inline styles kept (that is what draws tables, bars and colours),
    scripts / forms / event handlers / javascript: links removed."""
    from html.parser import HTMLParser
    out, skip = [], []

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if skip:
                if tag in _DROP_WITH_CONTENT and tag not in _VOID_TAGS:
                    skip.append(tag)
                return
            if tag in _DROP_WITH_CONTENT:
                skip.append(tag)
                return
            if tag not in _ALLOWED_TAGS:
                return
            keep = []
            for k, v in attrs:
                k = (k or '').lower()
                if k not in _ALLOWED_ATTRS or v is None:
                    continue
                if k in ('href', 'src'):
                    v = _safe_url(v, oid, k == 'src' and tag == 'img')
                    if not v:
                        continue
                if k == 'style' and _BAD_CSS.search(v):
                    continue
                if k == 'target':
                    v = '_blank'
                keep.append(' %s="%s"' % (k, html.escape(v, quote=True)))
            if tag == 'img' and not any(a.startswith(' src=') for a in keep):
                return
            if tag == 'a' and any(a.startswith(' target=') for a in keep):
                keep.append(' rel="noopener"')
            out.append('<%s%s>' % (tag, ''.join(keep)))

        def handle_endtag(self, tag):
            if skip:
                if tag == skip[-1]:
                    skip.pop()
                return
            if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
                out.append('</%s>' % tag)

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)

        def handle_data(self, data):
            if not skip:
                out.append(html.escape(data, quote=False))

        def handle_entityref(self, name):
            if not skip:
                out.append('&%s;' % name)

        def handle_charref(self, name):
            if not skip:
                out.append('&#%s;' % name)

    p = P(convert_charrefs=False)
    p.feed(str(raw or '')[:20000000])          # pasted images arrive inline; they leave as short links
    p.close()
    res = ''.join(out).strip()
    return res[:MAX_BODY_HTML]


def compose(issue, p, cv_link, unsub, pixel='', fresh_days=180, has_signature=True):
    fn = first_name(p.get('name'))
    e = html.escape
    if (issue.get('body_html') or '').strip():
        return _compose_designed(issue, fn, cv_link, unsub, pixel, fresh_days, p)
    secs = _jl(issue.get('sections'), []) or []
    subject = (issue.get('subject') or 'Market Pulse from HireLab').replace('{first_name}', fn)
    intro = (issue.get('intro') or '').replace('{first_name}', fn)
    h, t, btn = cv_block(p, fresh_days)
    sign_off = 'Warm regards,' if has_signature else 'Warm regards,\nTeam HireLab'
    plain = ['Hi %s,' % fn, '', intro, '']
    for s in secs:
        plain += [s.get('heading', '').upper(), s.get('body', ''), '']
    if issue.get('closing'):
        plain += [issue['closing'], '']
    plain += [h, t, '%s: %s' % (btn, cv_link), '', sign_off, '', '---',
              'You are receiving Market Pulse because your profile is with HireLab. To stop these e-mails: %s' % unsub]
    para = lambda txt: ''.join('<p style="margin:0 0 12px">%s</p>' % e(x.strip()) for x in str(txt or '').split('\n') if x.strip())
    body = ('<div style="max-width:600px">'
            '%s'
            '<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#0F6E56;font-weight:700;margin:0 0 6px">'
            'HireLab Market Pulse</div>'
            '<p style="margin:0 0 12px">Hi %s,</p>%s'
            % (('<div style="display:none;max-height:0;overflow:hidden">%s</div>' % e(issue.get('preheader') or '')) if issue.get('preheader') else '',
               e(fn), para(intro)))
    for s in secs:
        body += ('<h3 style="font-size:16px;color:#0E2A47;margin:18px 0 6px">%s</h3>%s' % (e(s.get('heading', '')), para(s.get('body', ''))))
    if issue.get('closing'):
        body += para(issue['closing'])
    body += ('<div style="border:1px solid #D9E7E1;background:#F2F8F5;border-radius:10px;padding:14px 16px;margin:18px 0">'
             '<div style="font-weight:700;color:#0E2A47;margin-bottom:4px">%s</div><div style="font-size:14px;color:#333;margin-bottom:10px">%s</div>'
             '<a href="%s" style="display:inline-block;background:#0F6E56;color:#fff;padding:10px 20px;border-radius:8px;'
             'text-decoration:none;font-weight:600">%s</a></div>'
             '<p>%s</p>'
             '<p style="font-size:11px;color:#888;margin-top:22px">You are receiving Market Pulse because your profile is with '
             'HireLab, a recruitment agency for Data Center, Solar, Electrical and Automation roles. '
             '<a href="%s" style="color:#888">Unsubscribe</a></p>%s</div>'
             % (e(h), e(t), e(cv_link), e(btn), e(sign_off).replace('\n', '<br>'), e(unsub),
                ('<img src="%s" width="1" height="1" alt="" style="display:block">' % e(pixel)) if pixel else ''))
    return subject, '\n'.join(plain), body


def _compose_designed(issue, fn, cv_link, unsub, pixel, fresh_days, p):
    """A pasted, designed e-mail goes out exactly as designed. HireLab only adds
    the optional personal profile box and the unsubscribe line (required)."""
    e = html.escape
    subject = (issue.get('subject') or issue.get('title') or 'Market Pulse from HireLab').replace('{first_name}', fn)
    designed = issue['body_html'].replace('{first_name}', e(fn))
    box_html, box_plain = '', []
    if int(issue.get('cv_box') if issue.get('cv_box') is not None else 1):
        h, t, btn = cv_block(p, fresh_days)
        box_html = ('<div style="border:1px solid #D9E7E1;background:#F2F8F5;border-radius:10px;padding:14px 16px;margin:18px 0;'
                    'font-family:Arial,Helvetica,sans-serif"><div style="font-weight:700;color:#0E2A47;margin-bottom:4px">%s</div>'
                    '<div style="font-size:14px;color:#333;margin-bottom:10px">%s</div><a href="%s" style="display:inline-block;'
                    'background:#0F6E56;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:600">%s</a></div>'
                    % (e(h), e(t), e(cv_link), e(btn)))
        box_plain = ['', h, t, '%s: %s' % (btn, cv_link)]
    body = ('%s<div style="max-width:680px">%s%s'
            '<p style="font-size:11px;color:#888;margin-top:22px;font-family:Arial,Helvetica,sans-serif">You are receiving this '
            'because your profile is with HireLab. <a href="%s" style="color:#888">Unsubscribe</a></p>%s</div>'
            % ((('<div style="display:none;max-height:0;overflow:hidden">%s</div>' % e(issue.get('preheader') or ''))
                if issue.get('preheader') else ''),
               designed, box_html, e(unsub),
               ('<img src="%s" width="1" height="1" alt="" style="display:block">' % e(pixel)) if pixel else ''))
    try:
        text = _core().html_to_text(designed)
    except Exception:
        text = re.sub(r'<[^>]+>', ' ', designed)
    plain = [text.strip()] + box_plain + ['', '---', 'To stop these e-mails: %s' % unsub]
    return subject, '\n'.join(plain), body


# ══════════════════════════════════════════════════════════════════════════
#  Send
# ══════════════════════════════════════════════════════════════════════════
def _fresh_days(conn, oid):
    try:
        from modules.segments import get_settings as sgs
        return int(sgs(conn, oid)['fresh_days'])
    except Exception:
        return 180


def _mail_off(conn, oid, rec):
    try:
        from modules import mail_guard as _mg
        return _mg.is_blocked(conn, oid, rec['candidate_id'], rec['email'])
    except Exception:
        return False


def send_one(conn, oid, rec, issue, fresh_days, gap):
    core = _core()
    cur = conn.execute("UPDATE newsletter_recipients SET status='sending' WHERE id=? AND status='queued'", (rec['id'],))
    conn.commit()
    if cur.rowcount != 1:
        return False, 'already handled'
    skip = ''
    dne = conn.execute("SELECT MAX(COALESCE(do_not_email,0)), MAX(COALESCE(reengage_off,0)) FROM candidates "
                       "WHERE owner_id=? AND LOWER(TRIM(email))=?", (oid, rec['email'])).fetchone()
    if dne and (dne[0] or dne[1]):
        skip = 'Unsubscribed / switched off before sending'
    elif _mail_off(conn, oid, rec):
        skip = 'Automated e-mails switched off for their job'
    elif rec['email'] in recent_mail_emails(conn, oid, gap, exclude_issue=issue['id']):
        skip = 'Got another HireLab e-mail in the last %d days' % gap
    if skip:
        conn.execute("UPDATE newsletter_recipients SET status='skipped', skip_reason=? WHERE id=?", (skip, rec['id']))
        conn.commit()
        return False, 'skipped'
    try:
        from modules.reengage import _ensure_link
        _ensure_link(conn, rec['candidate_id'])
        base, t = _base(), token_for(rec['id'])
        unsub = '%s/u/%s' % (base, core._unsub_token(oid, rec['candidate_id']))
        subject, plain, body = compose(issue, rec, '%s/nl/c/%s' % (base, t), unsub, '%s/nl/o/%s.gif' % (base, t),
                                       fresh_days, _has_signature())
        ok, err, _m, _t = core.email_service_send(
            rec['email'], subject, plain, body_html=body, candidate_id=rec['candidate_id'],
            extra_headers={'List-Unsubscribe': '<%s>' % unsub, 'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click'})
    except Exception as exc:
        ok, err = False, '%s: %s' % (type(exc).__name__, exc)
    if ok:
        conn.execute("UPDATE newsletter_recipients SET status='sent', sent_at=? WHERE id=?", (ts(), rec['id']))
        try:
            conn.execute('INSERT INTO candidate_events (candidate_id,event_type,detail,created_at) VALUES (?,?,?,?)',
                         (rec['candidate_id'], 'note', 'Market Pulse newsletter sent: %s' % (issue.get('title') or issue.get('subject') or ''), ts()))
        except Exception:
            pass
    else:
        conn.execute("UPDATE newsletter_recipients SET status='failed', error=? WHERE id=?", (str(err or '')[:300], rec['id']))
    conn.commit()
    return bool(ok), err


def sent_today(conn, oid):
    day = now_ist().strftime('%Y-%m-%d')
    return conn.execute("SELECT COUNT(*) FROM newsletter_recipients WHERE company_id=? AND status='sent' AND sent_at LIKE ?",
                        (oid, day + '%')).fetchone()[0]


def _finish(conn, oid):
    for r in conn.execute("SELECT id FROM newsletter_issues WHERE company_id=? AND status='sending'", (oid,)).fetchall():
        left = conn.execute("SELECT COUNT(*) FROM newsletter_recipients WHERE issue_id=? AND status IN ('queued','sending')",
                            (r['id'],)).fetchone()[0]
        if not left:
            conn.execute("UPDATE newsletter_issues SET status='done', finished_at=? WHERE id=?", (ts(), r['id']))
    conn.commit()


_NET_WORDS = ('not configured', 'auth', 'username and password', 'connect', 'timed out', 'timeout', 'name or service',
              'network', 'temporarily', 'try again later')


def run_company(conn, oid, force=False, limit=None):
    s = get_settings(conn, oid)
    now = now_ist()
    if not force and (now.weekday() == 6 or not (int(s['start_hour']) <= now.hour < int(s['end_hour']))):
        return 0
    left = int(s['daily_cap']) - sent_today(conn, oid)
    if left <= 0:
        return 0
    if limit is None:
        end = now.replace(hour=min(23, int(s['end_hour'])), minute=0, second=0, microsecond=0)
        ticks = max(1, int((end - now).total_seconds() // TICK_SECONDS))
        limit = min(MAX_PER_TICK, max(1, math.ceil(left / ticks)))
    limit = min(limit, left)
    recs = _rows(conn, "SELECT r.* FROM newsletter_recipients r JOIN newsletter_issues i ON i.id=r.issue_id "
                       "WHERE r.company_id=? AND r.status='queued' AND i.status='sending' ORDER BY i.approved_at, r.id LIMIT ?",
                 (oid, max(limit * 4, 20)))
    issues, n = {}, 0
    fd, gap = _fresh_days(conn, oid), gap_days(conn, oid)
    for rec in recs:
        if n >= limit:
            break
        if rec['issue_id'] not in issues:
            issues[rec['issue_id']] = _issue(conn, oid, rec['issue_id'])
        ok, err = send_one(conn, oid, rec, issues[rec['issue_id']], fd, gap)
        if ok:
            n += 1
        elif err and err not in ('already handled', 'skipped') and any(w in err.lower() for w in _NET_WORDS):
            conn.execute("UPDATE newsletter_recipients SET status='queued', error='' WHERE id=?", (rec['id'],))
            conn.execute('UPDATE newsletter_settings SET last_error=? WHERE company_id=?', (err[:300], oid))
            conn.commit()
            break
    if n:
        conn.execute("UPDATE newsletter_settings SET last_error='' WHERE company_id=?", (oid,))
        conn.commit()
    _finish(conn, oid)
    return n


def tick():
    core = _core()
    conn = get_db()
    try:
        cos = [r[0] for r in conn.execute("SELECT DISTINCT r.company_id FROM newsletter_recipients r JOIN newsletter_issues i "
                                          "ON i.id=r.issue_id WHERE r.status='queued' AND i.status='sending'").fetchall()]
        stamp = ts()
        for oid in cos:
            conn.execute('INSERT OR IGNORE INTO newsletter_settings (company_id) VALUES (?)', (oid,))
            cur = conn.execute('UPDATE newsletter_settings SET tick_at=? WHERE company_id=? AND (tick_at IS NULL OR tick_at<?)',
                               (stamp, oid, (now_ist() - datetime.timedelta(seconds=TICK_SECONDS - 20)).strftime('%Y-%m-%dT%H:%M:%S')))
            conn.commit()
            if cur.rowcount != 1:
                continue
            core._bg_tenant_set(oid)
            try:
                run_company(conn, oid)
            except Exception as e:
                print('[newsletter] company %s failed: %s' % (oid, e))
            finally:
                core._bg_tenant_clear()
    finally:
        conn.close()


def _loop():
    time.sleep(100)
    while True:
        try:
            tick()
        except Exception as e:
            print('[newsletter] loop error: %s' % e)
        time.sleep(TICK_SECONDS)


_started = False


def start_worker():
    global _started
    if _started or 'pytest' in sys.modules or os.environ.get('HIRELAB_NO_WORKERS'):
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
    print('[newsletter] background thread started')


start_worker()


# ══════════════════════════════════════════════════════════════════════════
#  API — company admins only (this mails the whole database)
# ══════════════════════════════════════════════════════════════════════════
def _admin():
    if not is_company_admin():
        return _err('Only an admin can manage the newsletter.', 403)
    return None


def _counts(conn, iid):
    r = conn.execute("SELECT COUNT(*) AS total, "
                     "SUM(CASE WHEN status IN ('queued','sending') THEN 1 ELSE 0 END) AS queued, "
                     "SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent, "
                     "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
                     "SUM(CASE WHEN status IN ('skipped','cancelled') THEN 1 ELSE 0 END) AS skipped, "
                     "SUM(CASE WHEN COALESCE(opened_at,'')<>'' THEN 1 ELSE 0 END) AS opened, "
                     "SUM(CASE WHEN COALESCE(clicked_at,'')<>'' THEN 1 ELSE 0 END) AS clicked "
                     "FROM newsletter_recipients WHERE issue_id=?", (iid,)).fetchone()
    k = {x: int(r[x] or 0) for x in r.keys()}
    k['updated'] = conn.execute("SELECT COUNT(DISTINCT r.email) FROM newsletter_recipients r JOIN talent_updates u "
                                "ON u.company_id=r.company_id AND u.email=r.email AND u.created_at>=r.sent_at "
                                "WHERE r.issue_id=? AND r.status='sent'", (iid,)).fetchone()[0]
    k['unsubscribed'] = conn.execute("SELECT COUNT(DISTINCT r.email) FROM newsletter_recipients r JOIN candidates c "
                                     "ON c.owner_id=r.company_id AND LOWER(TRIM(c.email))=r.email "
                                     "WHERE r.issue_id=? AND r.status='sent' AND COALESCE(c.do_not_email,0)=1 "
                                     "AND COALESCE(c.unsub_at,'')>=r.sent_at", (iid,)).fetchone()[0]
    return k


def _out(conn, i):
    o = {k: i[k] for k in ('id', 'title', 'status', 'points', 'subject', 'preheader', 'intro', 'closing', 'ai_status',
                           'ai_error', 'created_at', 'updated_at', 'approved_at', 'finished_at', 'queued', 'skipped_guard')}
    o['audience'] = _audience(i)
    o['sections'] = _jl(i['sections'], []) or []
    o['body_html'] = i['body_html'] if 'body_html' in i.keys() else ''
    o['cv_box'] = bool(i['cv_box'] if 'cv_box' in i.keys() and i['cv_box'] is not None else 1)
    o['counts'] = _counts(conn, i['id'])
    return o


@bp.route('/settings', methods=['GET'])
@login_required
def settings_get():
    e = _admin()
    if e:
        return e
    conn = get_db()
    try:
        try:
            sender = _core()._smtp_params()[0]
        except Exception:
            sender = ''
        return jsonify({'ok': True, 'settings': get_settings(conn, _oid()), 'gap_days': gap_days(conn, _oid()),
                        'fresh_days': _fresh_days(conn, _oid()), 'sender': sender, 'sent_today': sent_today(conn, _oid())})
    finally:
        conn.close()


@bp.route('/settings', methods=['PUT', 'POST'])
@login_required
def settings_save():
    e = _admin()
    if e:
        return e
    d = request.get_json(silent=True) or {}
    oid = _oid()
    conn = get_db()
    try:
        s = get_settings(conn, oid)

        def num(k, lo, hi):
            try:
                return max(lo, min(hi, int(d.get(k, s[k]))))
            except (TypeError, ValueError):
                return s[k]
        cap, sh, eh = num('daily_cap', 1, 2000), num('start_hour', 0, 23), num('end_hour', 1, 24)
        if eh <= sh:
            return _err('End hour must be after start hour.')
        conn.execute('INSERT OR IGNORE INTO newsletter_settings (company_id) VALUES (?)', (oid,))
        conn.execute('UPDATE newsletter_settings SET daily_cap=?, start_hour=?, end_hour=?, updated_by=?, updated_at=? '
                     'WHERE company_id=?', (cap, sh, eh, real_user_id(), ts(), oid))
        conn.commit()
        return jsonify({'ok': True, 'settings': get_settings(conn, oid)})
    finally:
        conn.close()


@bp.route('/issues', methods=['GET'])
@login_required
def issues_list():
    e = _admin()
    if e:
        return e
    conn = get_db()
    try:
        rows = _rows(conn, 'SELECT * FROM newsletter_issues WHERE company_id=? AND status<>\'deleted\' ORDER BY id DESC LIMIT 100',
                     (_oid(),))
        return jsonify({'ok': True, 'issues': [_out(conn, i) for i in rows]})
    finally:
        conn.close()


@bp.route('/issues', methods=['POST'])
@login_required
def issue_create():
    e = _admin()
    if e:
        return e
    d = request.get_json(silent=True) or {}
    oid = _oid()
    conn = get_db()
    try:
        src = _issue(conn, oid, int(d.get('copy_of') or 0)) if d.get('copy_of') else None
        month = now_ist().strftime('%B %Y')
        vals = {'company_id': oid, 'title': (src['title'] + ' (copy)') if src else 'Market Pulse — %s' % month,
                'status': 'draft', 'audience': src['audience'] if src else json.dumps(DEFAULT_AUDIENCE),
                'points': src['points'] if src else '', 'subject': src['subject'] if src else '',
                'preheader': src['preheader'] if src else '', 'intro': src['intro'] if src else '',
                'sections': src['sections'] if src else '[]', 'closing': src['closing'] if src else '',
                'body_html': (src.get('body_html') or '') if src else '',
                'cv_box': (1 if src.get('cv_box') is None else src['cv_box']) if src else 1,
                'created_by': int(real_user_id() or 0), 'created_at': ts(), 'updated_at': ts()}
        cur = conn.execute('INSERT INTO newsletter_issues (%s) VALUES (%s)' % (','.join(vals), ','.join('?' * len(vals))),
                           list(vals.values()))
        conn.commit()
        return jsonify({'ok': True, 'issue': _out(conn, _issue(conn, oid, cur.lastrowid))})
    finally:
        conn.close()


def _load_draft(conn, iid):
    i = _issue(conn, _oid(), iid)
    if not i:
        return None, _err('Not found', 404)
    return i, None


@bp.route('/issue/<int:iid>', methods=['GET'])
@login_required
def issue_get(iid):
    e = _admin()
    if e:
        return e
    conn = get_db()
    try:
        i, er = _load_draft(conn, iid)
        if er:
            return er
        recs = []
        if i['status'] != 'draft':
            recs = _rows(conn, 'SELECT r.id, r.candidate_id, r.email, r.name, r.status, r.skip_reason, r.error, r.sent_at, '
                               'r.opened_at, r.clicked_at, COALESCE(c.mandate_id,0) AS mandate_id FROM newsletter_recipients r '
                               'LEFT JOIN candidates c ON c.id=r.candidate_id WHERE r.issue_id=? ORDER BY r.id LIMIT 2000', (iid,))
        return jsonify({'ok': True, 'issue': _out(conn, i), 'recipients': recs})
    finally:
        conn.close()


@bp.route('/issue/<int:iid>', methods=['PUT', 'POST'])
@login_required
def issue_update(iid):
    e = _admin()
    if e:
        return e
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        i, er = _load_draft(conn, iid)
        if er:
            return er
        if i['status'] != 'draft':
            return _err('This issue was already approved.')
        sets = {}
        for k, n in (('title', 140), ('points', 4000), ('subject', 140), ('preheader', 200), ('intro', 1500), ('closing', 1000)):
            if k in d:
                sets[k] = str(d[k] or '').strip()[:n]
        if 'body_html' in d:
            sets['body_html'] = clean_html(d.get('body_html') or '', _oid())
        if 'cv_box' in d:
            sets['cv_box'] = 1 if d.get('cv_box') else 0
        if isinstance(d.get('sections'), list):
            secs = [{'heading': str(x.get('heading') or '').strip()[:120], 'body': str(x.get('body') or '').strip()[:3000]}
                    for x in d['sections'][:8] if isinstance(x, dict) and (x.get('heading') or x.get('body'))]
            sets['sections'] = json.dumps(secs)
        if isinstance(d.get('audience'), dict):
            from modules import segments as sg
            f = sg._clean_filter(d['audience'])
            sets['audience'] = json.dumps({k: f[k] for k in ('industries', 'functions', 'seniority', 'regions', 'freshness',
                                                             'exclude_live')})
        if sets:
            sets['updated_at'] = ts()
            conn.execute('UPDATE newsletter_issues SET %s WHERE id=?' % ','.join(k + '=?' for k in sets),
                         list(sets.values()) + [iid])
            conn.commit()
        return jsonify({'ok': True, 'issue': _out(conn, _issue(conn, _oid(), iid))})
    finally:
        conn.close()


@bp.route('/issue/<int:iid>/audience', methods=['GET'])
@login_required
def issue_audience(iid):
    """How many people the audience reaches right now, and how many the guard will hold back."""
    e = _admin()
    if e:
        return e
    oid = _oid()
    conn = get_db()
    try:
        i, er = _load_draft(conn, iid)
        if er:
            return er
        from modules import segments as sg
        if not sg.get_settings(conn, oid)['last_scan_at']:
            sg.scan_company(conn, oid)
        people = audience_people(conn, oid, _audience(i))
        gap = gap_days(conn, oid)
        recent = recent_mail_emails(conn, oid, gap, exclude_issue=iid)
        held = sum(1 for p in people if p['email'] in recent)
        fd = _fresh_days(conn, oid)
        stale = sum(1 for p in people if not sg.is_fresh(p, fd))
        s = get_settings(conn, oid)
        n = len(people) - held
        return jsonify({'ok': True, 'reach': len(people), 'held_by_guard': held, 'will_send': n, 'stale_cv': stale,
                        'gap_days': gap, 'days_to_send': math.ceil(n / max(1, int(s['daily_cap']))) if n else 0,
                        'sample': [{'name': p['name'], 'email': p['email'], 'designation': p['designation'],
                                    'company': p['company']} for p in people[:5]]})
    finally:
        conn.close()


@bp.route('/issue/<int:iid>/draft', methods=['POST'])
@login_required
def issue_ai_draft(iid):
    e = _admin()
    if e:
        return e
    core = _core()
    conn = get_db()
    try:
        i, er = _load_draft(conn, iid)
        if er:
            return er
        if i['status'] != 'draft':
            return _err('This issue was already approved.')
        points = str((request.get_json(silent=True) or {}).get('points', i['points']) or '').strip()
        if len(points) < 20:
            return _err('Write a few points first (news, hiring trends, salary moves) — the AI only uses what you give it.')
        key = (core.get_setting('deepseek_api_key') or '').strip()
        if not key:
            return _err('DeepSeek API key is not set (Settings).')
        try:
            d = _ask_ai(core, key, points, _audience(i), now_ist().strftime('%B %Y'))
        except Exception as exc:
            conn.execute("UPDATE newsletter_issues SET ai_status='failed', ai_error=? WHERE id=?", (str(exc)[:200], iid))
            conn.commit()
            return _err('AI draft failed: %s' % exc)
        conn.execute("UPDATE newsletter_issues SET points=?, subject=?, preheader=?, intro=?, sections=?, closing=?, "
                     "ai_status='done', ai_error='', updated_at=? WHERE id=?",
                     (points, d['subject'], d['preheader'], d['intro'], json.dumps(d['sections']), d['closing'], ts(), iid))
        conn.commit()
        return jsonify({'ok': True, 'issue': _out(conn, _issue(conn, _oid(), iid))})
    finally:
        conn.close()


def _sample(conn, oid, i, fresh_days):
    try:
        ppl = audience_people(conn, oid, _audience(i))
    except Exception:
        ppl = []
    from modules import segments as sg
    stale = next((p for p in ppl if not sg.is_fresh(p, fresh_days)), None)
    return stale or (ppl[0] if ppl else {'name': 'Asha Verma', 'email': '', 'cv_date': ''})


@bp.route('/issue/<int:iid>/preview', methods=['GET'])
@login_required
def issue_preview(iid):
    e = _admin()
    if e:
        return e
    oid = _oid()
    conn = get_db()
    try:
        i, er = _load_draft(conn, iid)
        if er:
            return er
        fd = _fresh_days(conn, oid)
        p = _sample(conn, oid, i, fd)
        b = _base()
        subject, plain, body = compose(i, p, b + '/nl/c/…', b + '/u/…', '', fd, _has_signature())
        return jsonify({'ok': True, 'subject': subject, 'html': body, 'text': plain, 'to': p.get('email', ''),
                        'name': p.get('name', '')})
    finally:
        conn.close()


@bp.route('/issue/<int:iid>/test', methods=['POST'])
@login_required
def issue_test(iid):
    e = _admin()
    if e:
        return e
    core = _core()
    oid = _oid()
    conn = get_db()
    try:
        i, er = _load_draft(conn, iid)
        if er:
            return er
        sender = core._smtp_params()[0]
        if not sender:
            return _err('Company mailbox is not set. Settings → Email Configuration.')
        fd = _fresh_days(conn, oid)
        p = _sample(conn, oid, i, fd)
        b = _base()
        subject, plain, body = compose(i, p, b + '/update-profile?token=TEST', b + '/u/TEST', '', fd, _has_signature())
        ok, err, _m, _t = core.email_service_send(sender, '[TEST] ' + subject, plain, body_html=body)
        return jsonify({'ok': bool(ok), 'error': err or '', 'to': sender})
    finally:
        conn.close()


@bp.route('/issue/<int:iid>/approve', methods=['POST'])
@login_required
def issue_approve(iid):
    e = _admin()
    if e:
        return e
    core = _core()
    oid = _oid()
    conn = get_db()
    try:
        i, er = _load_draft(conn, iid)
        if er:
            return er
        if i['status'] != 'draft':
            return _err('This issue was already approved.')
        if not (i['subject'] or '').strip() or not ((i['intro'] or '').strip() or (_jl(i['sections'], []) or [])):
            return _err('Add a subject and some content first.')
        try:
            if not core._smtp_params()[0]:
                return _err('Company mailbox is not set. Settings → Email Configuration.')
        except Exception:
            pass
        from modules import segments as sg
        sg.scan_company(conn, oid)                          # audience from fresh data
        people = audience_people(conn, oid, _audience(i))
        gap = gap_days(conn, oid)
        recent = recent_mail_emails(conn, oid, gap, exclude_issue=iid)
        now = ts()
        q = held = 0
        for p in people:
            if p['email'] in recent:
                held += 1
                continue
            cur = conn.execute('INSERT OR IGNORE INTO newsletter_recipients (issue_id, company_id, candidate_id, email, name, '
                               'cv_date, status, created_at) VALUES (?,?,?,?,?,?,?,?)',
                               (iid, oid, p['candidate_id'], p['email'], p['name'] or '', p['cv_date'] or '', 'queued', now))
            q += cur.rowcount
        if not q:
            conn.rollback()
            return _err('Nobody to send to: the audience is empty or everyone got a HireLab e-mail in the last %d days.' % gap)
        conn.execute("UPDATE newsletter_issues SET status='sending', approved_by=?, approved_at=?, queued=?, skipped_guard=? "
                     "WHERE id=?", (int(real_user_id() or 0), now, q, held, iid))
        conn.execute('INSERT OR IGNORE INTO newsletter_settings (company_id) VALUES (?)', (oid,))
        conn.commit()
        s = get_settings(conn, oid)
        return jsonify({'ok': True, 'queued': q, 'held_by_guard': held, 'days': math.ceil(q / max(1, int(s['daily_cap']))),
                        'issue': _out(conn, _issue(conn, oid, iid))})
    finally:
        conn.close()


@bp.route('/issue/<int:iid>/send-now', methods=['POST'])
@login_required
def issue_send_now(iid):
    e = _admin()
    if e:
        return e
    conn = get_db()
    try:
        i, er = _load_draft(conn, iid)
        if er:
            return er
        n = run_company(conn, _oid(), force=True, limit=10)
        return jsonify({'ok': True, 'sent': n, 'last_error': get_settings(conn, _oid()).get('last_error', ''),
                        'issue': _out(conn, _issue(conn, _oid(), iid))})
    finally:
        conn.close()


@bp.route('/issue/<int:iid>/cancel', methods=['POST'])
@login_required
def issue_cancel(iid):
    e = _admin()
    if e:
        return e
    conn = get_db()
    try:
        i, er = _load_draft(conn, iid)
        if er:
            return er
        if i['status'] == 'draft':
            conn.execute("UPDATE newsletter_issues SET status='deleted', finished_at=? WHERE id=?", (ts(), iid))
        else:
            conn.execute("UPDATE newsletter_recipients SET status='cancelled' WHERE issue_id=? AND status='queued'", (iid,))
            conn.execute("UPDATE newsletter_issues SET status=CASE WHEN status='sending' THEN 'stopped' ELSE status END, "
                         "finished_at=? WHERE id=?", (ts(), iid))
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


# ── public: open pixel + CV-refresh click ─────────────────────────────────
_GIF = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,'
        b'\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')


@pages.route('/nl/img/<name>')
def nl_image(name):
    """Images pasted into a designed e-mail. Public (mail clients fetch them),
    content-addressed names, nothing else in the folder can be reached."""
    if not _IMG_NAME.match(name or ''):
        return Response('Not found', status=404)
    fp = os.path.join(_img_dir(), name)
    if not os.path.isfile(fp):
        return Response('Not found', status=404)
    from flask import send_file
    resp = send_file(fp, max_age=31536000)
    resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return resp


@pages.route('/nl/o/<tok>.gif')
def track_open(tok):
    rid = parse_token(tok)
    if rid:
        conn = get_db()
        try:
            conn.execute("UPDATE newsletter_recipients SET opened_at=? WHERE id=? AND COALESCE(opened_at,'')=''", (ts(), rid))
            conn.commit()
        finally:
            conn.close()
    return Response(_GIF, mimetype='image/gif', headers={'Cache-Control': 'no-store, max-age=0'})


@pages.route('/nl/c/<tok>')
def track_click(tok):
    rid = parse_token(tok)
    if not rid:
        return redirect(_base() + '/')
    conn = get_db()
    try:
        r = conn.execute('SELECT r.candidate_id, c.update_token FROM newsletter_recipients r LEFT JOIN candidates c '
                         'ON c.id=r.candidate_id WHERE r.id=?', (rid,)).fetchone()
        if not r or not r['update_token']:
            return Response('<p style="font-family:sans-serif;margin:15vh auto;max-width:420px">This link is no longer valid. '
                            'Please reply to our e-mail and we will send you a new one.</p>', mimetype='text/html')
        conn.execute("UPDATE newsletter_recipients SET clicked_at=? WHERE id=? AND COALESCE(clicked_at,'')=''", (ts(), rid))
        conn.execute("UPDATE newsletter_recipients SET opened_at=? WHERE id=? AND COALESCE(opened_at,'')=''", (ts(), rid))
        conn.execute('UPDATE candidates SET update_requested_at=? WHERE id=?', (ts(), r['candidate_id']))
        conn.commit()
        return redirect('/update-profile?token=' + r['update_token'])
    finally:
        conn.close()


@pages.route('/newsletter')
def newsletter_page():
    core = _core()
    if not core.current_user():
        return redirect('/')
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'newsletter.html')
    with open(p, encoding='utf-8') as f:
        return Response(f.read(), mimetype='text/html')
