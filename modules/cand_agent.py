"""
RecruitOS — Candidate AI Agent  (Oct 2026, Phase 1)

A recruiter types the work in plain words on the candidate profile
("Please drop a follow-up email for the JD shared, at 11 AM tomorrow").
The agent reads the whole candidate — profile, CV, job + JD, stage history,
e-mails and WhatsApp messages, interviews — and proposes what to do:

    email      sent at the stated time, after the recruiter approves the card
    whatsapp   saved as a draft; at the stated time the recruiter is notified and
               sends it (WhatsApp Business API where configured, else wa.me)
    reminder   a task for the recruiter (reuses the Tasks / reminders table)
    answer     a question about the candidate, answered from their data

Nothing reaches a candidate without a recruiter's approval. Before a scheduled
e-mail goes out the agent checks again: if the candidate replied after the
approval, the e-mail is held and the recruiter is asked.

Routes (all under /api/candidates/<cid>/agent, so the tenant guard and the
recruiter access guard check the candidate for every call):
  GET    ''                         tasks + capabilities
  POST   /plan        {instruction} -> proposed tasks (status 'draft')
  POST   /tasks/<tid>/approve       {subject, body, to, run_at}  draft|held -> scheduled / sent / ready
  POST   /tasks/<tid>/cancel
  POST   /tasks/<tid>/wa-send       {body}   send through the WhatsApp Business API
  POST   /tasks/<tid>/wa-done       {body}   recruiter sent it from WhatsApp themself

Design rules
  - Additive: one new table (cand_agent_tasks); e-mail, WhatsApp, reminders and
    notifications reuse the existing code paths.
  - Phone numbers and e-mail addresses are masked before any text goes to the
    AI; the server fills in the recipient itself.
  - E-mails go out from the approving recruiter's own mailbox when they set one
    up (same rule as every manual send), else the company mailbox.
"""

import os
import re
import sys
import json
import time
import datetime
import threading

from flask import Blueprint, request, jsonify

from modules.shared import (
    get_db, ts, effective_company_id, real_user_id, current_user, login_required, _core, log_activity,
)
from modules import register_migration

bp = Blueprint('cand_agent', __name__, url_prefix='/api')

ACTION_TYPES = ('email', 'whatsapp', 'reminder', 'answer')
OPEN_STATUSES = ('draft', 'scheduled', 'held', 'ready')
MAX_INSTRUCTION = 1000
MAX_ACTIONS = 3
DEFAULT_HOUR = 10            # "no time given" -> next working day 10:00 IST
QUIET_FROM, QUIET_TO = 21, 8  # a default time never lands in 21:00-08:00
TICK_SECONDS = 60
AI_LIMIT, AI_WINDOW = 30, 600   # plans per user per 10 minutes

_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
_PHONE_RE = re.compile(r'(?<![\d])(?:\+?\d{1,3}[\s\-]?)?\d{5}[\s\-]?\d{5}(?![\d])')
_VALID_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

_rl_lock = threading.Lock()
_rl = {}


class AgentError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message, self.code = message, code


# ══════════════════════════════════════════════════════════════════════════
#  MIGRATION
# ══════════════════════════════════════════════════════════════════════════
@register_migration
def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS cand_agent_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        candidate_id INTEGER NOT NULL,
        mandate_id INTEGER DEFAULT 0,
        user_id INTEGER DEFAULT 0,            -- who gave the instruction
        batch TEXT DEFAULT '',                -- one instruction -> 1..3 tasks
        instruction TEXT DEFAULT '',
        understood TEXT DEFAULT '',           -- the agent's one-line reading of the instruction
        action TEXT NOT NULL,                 -- email | whatsapp | reminder | answer
        status TEXT DEFAULT 'draft',          -- draft | scheduled | held | ready | sent | done | failed | cancelled
        run_at TEXT DEFAULT '',               -- IST 'YYYY-MM-DDTHH:MM:SS'
        time_stated INTEGER DEFAULT 0,        -- 1 = the time came from the instruction
        to_addr TEXT DEFAULT '',
        subject TEXT DEFAULT '',
        body TEXT DEFAULT '',
        reply_to_msg_id TEXT DEFAULT '',      -- e-mail Message-ID this one answers (threading)
        reason TEXT DEFAULT '',               -- why / what it is based on, or why it is held
        result TEXT DEFAULT '',
        error TEXT DEFAULT '',
        approved_by INTEGER DEFAULT 0,
        approved_at TEXT DEFAULT '',
        done_at TEXT DEFAULT '',
        reminder_id INTEGER DEFAULT 0,
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    )''')
    for sql in ('CREATE INDEX IF NOT EXISTS idx_cagent_cand ON cand_agent_tasks(company_id, candidate_id)',
                'CREATE INDEX IF NOT EXISTS idx_cagent_due ON cand_agent_tasks(status, run_at)'):
        conn.execute(sql)


# ══════════════════════════════════════════════════════════════════════════
#  TIME
# ══════════════════════════════════════════════════════════════════════════
def _now():
    """IST now (ts() is the app's IST clock)."""
    return datetime.datetime.fromisoformat(ts()[:19])


def _fmt(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S')


def parse_when(s):
    s = str(s or '').strip().replace(' ', 'T')
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s[:19]).replace(second=0, microsecond=0)
    except Exception:
        return None


def default_time(now):
    """Next working day (Mon-Sat) at DEFAULT_HOUR, or today if that is still ahead."""
    d = now.replace(hour=DEFAULT_HOUR, minute=0, second=0, microsecond=0)
    if d <= now:
        d += datetime.timedelta(days=1)
    while d.weekday() == 6:           # Sunday
        d += datetime.timedelta(days=1)
    return d


def _in_quiet_hours(dt):
    return dt.hour >= QUIET_FROM or dt.hour < QUIET_TO


# ══════════════════════════════════════════════════════════════════════════
#  CONTEXT  (everything the agent reads, with contact details masked)
# ══════════════════════════════════════════════════════════════════════════
def mask(text):
    s = _EMAIL_RE.sub('[email]', str(text or ''))
    return _PHONE_RE.sub('[phone]', s)


def _clip(s, n):
    s = re.sub(r'\s+', ' ', str(s or '')).strip()
    return s if len(s) <= n else s[:n] + '…'


def _candidate(conn, cid, oid):
    r = conn.execute('SELECT * FROM candidates WHERE id=? AND owner_id=?', (cid, oid)).fetchone()
    return dict(r) if r else None


def _recruiter_name(conn, uid):
    r = conn.execute('SELECT display_name, username FROM users WHERE id=?', (uid,)).fetchone()
    return ((r['display_name'] or r['username']) if r else '') or 'Recruiter'


def _wa_number(phone):
    d = re.sub(r'\D', '', str(phone or ''))
    if len(d) == 10:
        d = '91' + d
    return d


def build_context(conn, c, oid, now, recruiter):
    """-> (text for the AI, emails list [{id, message_id, subject}])"""
    L = []
    L.append(f'NOW: {now.strftime("%A, %d %b %Y, %H:%M")} IST')
    L.append(f'RECRUITER (you write as this person): {recruiter}')
    L.append('')
    L.append('CANDIDATE')
    for label, key in (('Name', 'name'), ('Current company', 'company'), ('Designation', 'designation'),
                       ('Experience (years)', 'experience'), ('Current CTC', 'ctc_current'),
                       ('Expected CTC', 'ctc_expected'), ('Notice period', 'notice_period'),
                       ('Location', 'location'), ('Preferred location', 'preferred_location'),
                       ('Qualification', 'qualification'), ('Key skills', 'key_skills'),
                       ('Pipeline stage', 'stage'), ('Career summary', 'career_summary'),
                       ('Recruiter feedback', 'recruiter_feedback'), ('Client feedback', 'client_feedback'),
                       ('Comments', 'general_comments'), ('Offered CTC', 'offered_ctc'),
                       ('Joining date', 'joining_date')):
        v = c.get(key)
        if v not in (None, '', 0, 0.0):
            L.append(f'- {label}: {_clip(mask(v), 600)}')
    L.append(f'- E-mail on file: {"yes" if (c.get("email") or "").strip() else "NO"}')
    L.append(f'- Phone on file: {"yes" if (c.get("phone") or "").strip() else "NO"}')
    if c.get('do_not_email') or c.get('unsub_at'):
        L.append('- Has UNSUBSCRIBED from e-mails: do not e-mail.')

    if c.get('mandate_id'):
        m = conn.execute('SELECT * FROM mandates WHERE id=? AND owner_id=?', (c['mandate_id'], oid)).fetchone()
        if m:
            m = dict(m)
            L.append('')
            L.append('JOB THE CANDIDATE IS ON')
            L.append(f'- Role: {m.get("role") or ""} at {m.get("client") or ""}'
                     f' · {m.get("location") or ""} · status {m.get("status") or "open"}')
            if m.get('ctc_min') or m.get('ctc_max'):
                L.append(f'- Budget: {m.get("ctc_min") or "?"} - {m.get("ctc_max") or "?"}')
            if m.get('experience') or m.get('exp_min'):
                L.append(f'- Experience asked: {m.get("experience") or ""} {m.get("exp_min") or ""}-{m.get("exp_max") or ""}')
            if (m.get('jd') or '').strip():
                L.append('- JD: ' + _clip(mask(m['jd']), 2500))
    try:
        cv = _core()._candidate_cv_text(c, 5000)
    except Exception:
        cv = ''
    if cv:
        L.append('')
        L.append('CV (excerpt)')
        L.append(_clip(mask(cv), 5000))

    ev = conn.execute('SELECT event_type, detail, created_at FROM candidate_events WHERE candidate_id=? '
                      'ORDER BY id DESC LIMIT 25', (c['id'],)).fetchall()
    if ev:
        L.append('')
        L.append('TIMELINE (oldest first)')
        for r in reversed(ev):
            L.append(f'- {(r["created_at"] or "")[:16]} [{r["event_type"]}] {_clip(mask(r["detail"]), 260)}')

    emails = []
    rows = conn.execute('SELECT id, direction, subject, body, message_id, sent_at, created_at FROM email_messages '
                        'WHERE company_id=? AND candidate_id=? ORDER BY COALESCE(NULLIF(sent_at,\'\'), created_at) DESC, id DESC '
                        'LIMIT 12', (oid, c['id'])).fetchall()
    if rows:
        L.append('')
        L.append('E-MAILS (oldest first; # = id)')
        for r in reversed(rows):
            emails.append({'id': r['id'], 'message_id': r['message_id'] or '', 'subject': r['subject'] or ''})
            who = 'CANDIDATE -> you' if (r['direction'] or '') == 'received' else 'YOU -> candidate'
            L.append(f'- #{r["id"]} {(r["sent_at"] or r["created_at"] or "")[:16]} {who} | Subject: '
                     f'{_clip(r["subject"], 140)} | {_clip(mask(r["body"]), 700)}')

    try:
        wrows = conn.execute(
            'SELECT m.direction, m.content, m.created_at FROM wa_messages m JOIN wa_conversations w '
            'ON w.id=m.conversation_id WHERE w.company_id=? AND w.candidate_id=? ORDER BY m.id DESC LIMIT 15',
            (oid, c['id'])).fetchall()
    except Exception:
        wrows = []
    if wrows:
        L.append('')
        L.append('WHATSAPP (oldest first)')
        for r in reversed(wrows):
            who = 'YOU' if (r['direction'] or '').startswith('out') else 'CANDIDATE'
            L.append(f'- {(r["created_at"] or "")[:16]} {who}: {_clip(mask(r["content"]), 300)}')

    try:
        iv = conn.execute('SELECT round_name, mode, scheduled_at, status, result FROM interviews '
                          'WHERE candidate_id=? AND owner_id=? ORDER BY scheduled_at', (c['id'], oid)).fetchall()
    except Exception:
        iv = []
    if iv:
        L.append('')
        L.append('INTERVIEWS')
        for r in iv:
            L.append(f'- {r["round_name"] or "Round"} ({r["mode"] or ""}) {r["scheduled_at"] or ""} '
                     f'status {r["status"] or ""} {("result " + r["result"]) if r["result"] else ""}')
    return '\n'.join(L), emails


# ══════════════════════════════════════════════════════════════════════════
#  AI PLANNER
# ══════════════════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """You are the AI agent of a recruitment agency's ATS. A recruiter gives you an instruction about ONE candidate. Read the candidate data and decide the concrete action(s), then write them.

Action types:
- "email": an e-mail to the candidate. Give "subject" and "body".
- "whatsapp": a WhatsApp message to the candidate. Give "body". The recruiter sends it themself.
- "reminder": a task for the RECRUITER (no message to the candidate). Give "body" = the task title.
- "answer": the recruiter asked a QUESTION about the candidate. Give "body" = the answer from the data.

Timing ("when"):
- The time at which the action must happen, as "YYYY-MM-DDTHH:MM" in IST, worked out from NOW.
- A time attached to the action verb ("send ... at 11 AM tomorrow", "drop a mail tomorrow 11") is the send time.
- "tomorrow 11 AM" = the next calendar day at 11:00. "Monday" = the next Monday. "in 2 hours" = NOW + 2h.
- If the instruction gives no time, "when" is null and "time_stated" is false. "now"/"immediately"/"asap" = NOW, time_stated true.
- If a phrase can be read two ways, pick the most likely reading and say so in "understood".

Writing rules:
- Write as the recruiter, first person, warm and professional, Indian business English (unless the instruction asks for another language, e.g. Hinglish or Hindi).
- Greet the candidate by first name. Keep e-mails under 170 words, WhatsApp under 70 words.
- Use ONLY facts present in the data. Never invent salaries, dates, interview slots, company names or JD details. If something the message needs is missing, ask the recruiter in "question" and return no actions.
- A follow-up refers to the earlier message it follows (role, company, when it was sent).
- E-mail body: plain text, no signature block (one is added automatically). WhatsApp: may end with the recruiter's first name.
- Never write e-mail addresses or phone numbers; the system fills in the recipient.
- If the candidate has unsubscribed from e-mails, do not create an e-mail; suggest WhatsApp in "question".
- If the e-mail continues an earlier e-mail, set "reply_to_email_id" to that e-mail's # and the subject to "Re: <its subject>".
- Actions you cannot do (move a stage, book an interview with the client, send documents): say so in "question" and offer what you can do (a reminder, a message).
- At most 3 actions.

Reply with ONLY this JSON:
{"understood": "one short sentence: what you will do and when",
 "actions": [{"type": "email|whatsapp|reminder|answer", "when": "YYYY-MM-DDTHH:MM" or null, "time_stated": true|false,
              "subject": "", "body": "", "reply_to_email_id": null, "reason": "what in the data this is based on"}],
 "question": null or "one question for the recruiter"}"""


def _ai_key():
    try:
        return (_core().get_setting('deepseek_api_key') or '').strip()
    except Exception:
        return ''


def ai_plan(context, instruction):
    """Ask the model. Returns the parsed dict. Tests replace this function."""
    core = _core()
    key = _ai_key()
    if not key:
        raise AgentError('AI is not set up for your company yet (Settings -> AI keys -> DeepSeek).')
    payload = {'model': 'deepseek-chat', 'temperature': 0.3, 'max_tokens': 1600,
               'response_format': {'type': 'json_object'},
               'messages': [{'role': 'system', 'content': SYSTEM_PROMPT},
                            {'role': 'user', 'content': context + '\n\nINSTRUCTION FROM THE RECRUITER:\n' + instruction}]}
    try:
        resp = core.call_deepseek(key, payload, timeout=75, endpoint='candidate-agent')
    except getattr(core, 'TokenCapError', Exception):
        raise AgentError('Your company has reached its monthly AI limit.')
    except Exception as e:
        raise AgentError(f'The AI did not answer ({type(e).__name__}). Please try again.', 502)
    if resp.status_code != 200:
        raise AgentError(f'The AI did not answer (HTTP {resp.status_code}). Please try again.', 502)
    try:
        text = resp.json()['choices'][0]['message']['content']
    except Exception:
        raise AgentError('The AI answer could not be read. Please try again.', 502)
    plan = core.parse_json(text) if text else None
    if not isinstance(plan, dict):
        raise AgentError('The AI answer could not be read. Please try again.', 502)
    return plan


def _rate_ok(uid):
    now = time.time()
    with _rl_lock:
        q = [t for t in _rl.get(uid, []) if now - t < AI_WINDOW]
        if len(q) >= AI_LIMIT:
            _rl[uid] = q
            return False
        q.append(now)
        _rl[uid] = q
    return True


def make_tasks(plan, c, emails, now):
    """Validated task dicts from the model's plan (no DB writes)."""
    out = []
    by_id = {e['id']: e for e in emails}
    for a in (plan.get('actions') or [])[:MAX_ACTIONS]:
        if not isinstance(a, dict):
            continue
        kind = str(a.get('type') or '').strip().lower()
        if kind not in ACTION_TYPES:
            continue
        body = str(a.get('body') or '').strip()[:6000]
        if not body:
            continue
        t = {'action': kind, 'subject': '', 'body': body, 'to_addr': '', 'reply_to_msg_id': '',
             'reason': _clip(a.get('reason'), 400), 'run_at': '', 'time_stated': 0, 'error': ''}
        when = parse_when(a.get('when'))
        stated = bool(a.get('time_stated')) and when is not None
        if kind != 'answer':
            if not when or not stated:
                when = default_time(now)
            elif when < now:
                when = now               # "now" / a moment ago
            t['run_at'], t['time_stated'] = _fmt(when), int(stated)
        if kind == 'email':
            t['subject'] = _clip(a.get('subject'), 200) or 'Following up'
            t['to_addr'] = (c.get('email') or '').strip()
            try:
                ref = by_id.get(int(a.get('reply_to_email_id') or 0))
            except Exception:
                ref = None
            if ref and ref['message_id']:
                t['reply_to_msg_id'] = ref['message_id']
            if not t['to_addr']:
                t['error'] = 'No e-mail address on this candidate. Add one before approving.'
            elif c.get('do_not_email') or c.get('unsub_at'):
                t['error'] = 'This candidate unsubscribed from e-mails.'
        elif kind == 'whatsapp':
            t['to_addr'] = (c.get('phone') or '').strip()
            if not t['to_addr']:
                t['error'] = 'No phone number on this candidate. Add one before approving.'
        out.append(t)
    return out


# ══════════════════════════════════════════════════════════════════════════
#  EXECUTION  (approve-now and the background worker share this)
# ══════════════════════════════════════════════════════════════════════════
def _notify(conn, task, kind, title, lines):
    try:
        from modules import notifications as ntf
        uid = task['approved_by'] or task['user_id']
        ntf._create(conn, task['company_id'], uid, kind, title, lines,
                    candidate_id=task['candidate_id'], mandate_id=task['mandate_id'] or 0,
                    actor_name='AI Agent')
        conn.commit()
    except Exception as e:
        print(f'[cand_agent] notify failed: {e}')


def _set(conn, tid, **vals):
    vals['updated_at'] = ts()
    conn.execute('UPDATE cand_agent_tasks SET ' + ', '.join(f'{k}=?' for k in vals) + ' WHERE id=?',
                 list(vals.values()) + [tid])
    conn.commit()


def _replied_since(conn, task, since):
    """True when the candidate wrote (e-mail or WhatsApp) after `since`."""
    if not since:
        return False
    r = conn.execute("SELECT 1 FROM email_messages WHERE company_id=? AND candidate_id=? AND direction='received' "
                     "AND COALESCE(NULLIF(sent_at,''), created_at)>? LIMIT 1",
                     (task['company_id'], task['candidate_id'], since)).fetchone()
    if r:
        return True
    try:
        r = conn.execute("SELECT 1 FROM wa_messages m JOIN wa_conversations w ON w.id=m.conversation_id "
                         "WHERE w.company_id=? AND w.candidate_id=? AND m.direction LIKE 'in%' AND m.created_at>? LIMIT 1",
                         (task['company_id'], task['candidate_id'], since)).fetchone()
    except Exception:
        r = None
    return bool(r)


def send_email(conn, task, recheck):
    """Send one approved e-mail. recheck=True when it runs later than approval."""
    core = _core()
    c = _candidate(conn, task['candidate_id'], task['company_id'])
    if not c:
        _set(conn, task['id'], status='failed', error='The candidate no longer exists.', done_at=ts())
        return False
    if c.get('do_not_email') or c.get('unsub_at'):
        _set(conn, task['id'], status='failed', error='The candidate unsubscribed from e-mails.', done_at=ts())
        _notify(conn, task, 'agent_failed', 'AI Agent: e-mail not sent', [
            f'Candidate: {c["name"]}', 'Reason: the candidate unsubscribed from e-mails.'])
        return False
    if recheck and _replied_since(conn, task, task['approved_at']):
        _set(conn, task['id'], status='held', reason='The candidate replied after you approved this. Check before sending.')
        _notify(conn, task, 'agent_held', 'AI Agent: e-mail on hold', [
            f'Candidate: {c["name"]}', f'Subject: {task["subject"]}',
            'They replied after you approved it. Open the AI Agent tab to send anyway or cancel.'])
        return False
    to = (task['to_addr'] or '').strip()
    with core.tenant_pin(task['company_id']), core.mail_sender(task['approved_by']):
        ok, err, mid, _tid = core.email_service_send(
            to, task['subject'], task['body'],
            in_reply_to=task['reply_to_msg_id'] or '', references=task['reply_to_msg_id'] or '',
            candidate_id=task['candidate_id'])
    if not ok:
        _set(conn, task['id'], status='failed', error=str(err or 'Send failed')[:300], done_at=ts())
        _notify(conn, task, 'agent_failed', 'AI Agent: e-mail failed', [
            f'Candidate: {c["name"]}', f'Subject: {task["subject"]}', f'Error: {_clip(err, 160)}'])
        return False
    who = _recruiter_name(conn, task['approved_by'])
    conn.execute('INSERT INTO candidate_events (candidate_id,event_type,detail,created_at) VALUES (?,?,?,?)',
                 (task['candidate_id'], 'email',
                  f'Email sent to {to}\nSubject: {task["subject"]}\n\n{task["body"]}\n— {who} (AI Agent)', ts()))
    _set(conn, task['id'], status='sent', result=mid or '', error='', done_at=ts())
    if recheck:
        _notify(conn, task, 'agent_sent', 'AI Agent: e-mail sent', [
            f'Candidate: {c["name"]}', f'Subject: {task["subject"]}'])
    return True


def wa_ready(conn, task, notify=True):
    c = _candidate(conn, task['candidate_id'], task['company_id']) or {}
    _set(conn, task['id'], status='ready')
    if notify:
            _notify(conn, task, 'agent_wa', 'WhatsApp draft ready to send', [
            f'Candidate: {c.get("name") or ""}', f'Message: {_clip(task["body"], 120)}',
            'Open the AI Agent tab to send it.'])


def make_reminder(conn, task):
    c = _candidate(conn, task['candidate_id'], task['company_id']) or {}
    uid = task['approved_by'] or task['user_id']
    label = ''
    if task['mandate_id']:
        m = conn.execute('SELECT role, client FROM mandates WHERE id=?', (task['mandate_id'],)).fetchone()
        if m:
            label = (m['role'] or '') + (' — ' + m['client'] if m['client'] else '')
    row = {'candidate_id': task['candidate_id'], 'mandate_id': task['mandate_id'] or None,
           'candidate_name': c.get('name') or '', 'mandate_label': label,
           'note': _clip(task['body'], 300), 'notes': 'Created by the AI Agent from: ' + _clip(task['instruction'], 400),
           'due_at': task['run_at'][:16], 'done': 0, 'stage': 'todo', 'created_at': ts(),
           'owner_id': task['company_id'], 'created_by': uid, 'assigned_to': uid, 'updated_at': ts()}
    cols = {r[1] for r in conn.execute('PRAGMA table_info(reminders)')}
    row = {k: v for k, v in row.items() if k in cols}
    rid = conn.execute('INSERT INTO reminders (%s) VALUES (%s)' % (','.join(row), ','.join('?' * len(row))),
                       list(row.values())).lastrowid
    _set(conn, task['id'], status='done', reminder_id=rid, done_at=ts())


def execute(conn, task, recheck=False):
    task = dict(task)
    if task['action'] == 'email':
        return send_email(conn, task, recheck)
    if task['action'] == 'whatsapp':
        return wa_ready(conn, task, notify=recheck)
    if task['action'] == 'reminder':
        return make_reminder(conn, task)


def run_due(now=None):
    """Run every approved task whose time has come. Returns how many ran."""
    now = now or _now()
    conn = get_db()
    n = 0
    try:
        due = conn.execute("SELECT * FROM cand_agent_tasks WHERE status='scheduled' AND run_at<=? "
                           "ORDER BY run_at LIMIT 50", (_fmt(now),)).fetchall()
        for t in due:
            claimed = conn.execute("UPDATE cand_agent_tasks SET status='running', updated_at=? "
                                   "WHERE id=? AND status='scheduled'", (ts(), t['id'])).rowcount
            conn.commit()
            if not claimed:
                continue        # another worker took it
            core = _core()
            core._bg_tenant_set(t['company_id'])
            try:
                execute(conn, t, recheck=True)
                n += 1
            except Exception as e:
                print(f'[cand_agent] task {t["id"]} failed: {e}')
                _set(conn, t['id'], status='failed', error=f'{type(e).__name__}: {e}'[:300], done_at=ts())
            finally:
                core._bg_tenant_clear()
    finally:
        conn.close()
    return n


def _loop():
    time.sleep(45)
    while True:
        try:
            run_due()
        except Exception as e:
            print(f'[cand_agent] loop error: {e}')
        time.sleep(TICK_SECONDS)


_started = False


def start_worker():
    global _started
    if _started or 'pytest' in sys.modules or os.environ.get('HIRELAB_NO_WORKERS'):
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
    print('[cand_agent] background thread started')


start_worker()


# ══════════════════════════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════════════════════════
def _err(e):
    return jsonify({'error': e.message}), e.code


def _public(t):
    d = dict(t)
    d['to_display'] = d.get('to_addr') or ''
    if d['action'] == 'whatsapp':
        d['wa_number'] = _wa_number(d.get('to_addr'))
    return d


def _wa_api_ready():
    try:
        from modules.wa_agent import _wa_config
        cfg = _wa_config()
        return bool(cfg['token'] and cfg['phone_id'])
    except Exception:
        return False


def _task(conn, cid, tid):
    r = conn.execute('SELECT * FROM cand_agent_tasks WHERE id=? AND candidate_id=? AND company_id=?',
                     (tid, cid, effective_company_id())).fetchone()
    if not r:
        raise AgentError('Not found.', 404)
    return dict(r)


def _list(conn, cid):
    rows = conn.execute('SELECT * FROM cand_agent_tasks WHERE company_id=? AND candidate_id=? '
                        "AND status!='running' ORDER BY id DESC LIMIT 60",
                        (effective_company_id(), cid)).fetchall()
    return [_public(r) for r in rows]


@bp.route('/candidates/<int:cid>/agent', methods=['GET'])
@login_required
def agent_home(cid):
    conn = get_db()
    try:
        c = _candidate(conn, cid, effective_company_id())
        if not c:
            return jsonify({'error': 'Candidate not found'}), 404
        return jsonify({'ok': True, 'tasks': _list(conn, cid), 'wa_api': _wa_api_ready(),
                        'ai_ready': bool(_ai_key()), 'has_email': bool((c.get('email') or '').strip()),
                        'has_phone': bool((c.get('phone') or '').strip()), 'now': ts()[:19]})
    finally:
        conn.close()


@bp.route('/candidates/<int:cid>/agent/plan', methods=['POST'])
@login_required
def agent_plan(cid):
    d = request.get_json(silent=True) or {}
    instruction = re.sub(r'\s+', ' ', str(d.get('instruction') or '')).strip()[:MAX_INSTRUCTION]
    conn = get_db()
    try:
        if len(instruction) < 3:
            raise AgentError('Write what the agent should do.')
        oid, uid = effective_company_id(), real_user_id()
        c = _candidate(conn, cid, oid)
        if not c:
            raise AgentError('Candidate not found.', 404)
        if not _rate_ok(uid):
            raise AgentError('Too many requests. Please wait a few minutes.', 429)
        now = _now()
        context, emails = build_context(conn, c, oid, now, _recruiter_name(conn, uid))
        plan = ai_plan(context, instruction)
        tasks = make_tasks(plan, c, emails, now)
        understood = _clip(plan.get('understood'), 300)
        question = _clip(plan.get('question'), 400) if plan.get('question') else ''
        batch = f'{uid}-{int(time.time() * 1000)}'
        stamp = ts()
        for t in tasks:
            status = 'done' if t['action'] == 'answer' else 'draft'
            conn.execute(
                'INSERT INTO cand_agent_tasks (company_id,candidate_id,mandate_id,user_id,batch,instruction,understood,'
                'action,status,run_at,time_stated,to_addr,subject,body,reply_to_msg_id,reason,error,created_at,updated_at,'
                'done_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (oid, cid, c.get('mandate_id') or 0, uid, batch, instruction, understood, t['action'], status,
                 t['run_at'], t['time_stated'], t['to_addr'], t['subject'], t['body'], t['reply_to_msg_id'],
                 t['reason'], t['error'], stamp, stamp, stamp if status == 'done' else ''))
        conn.commit()
        log_activity('agent.plan', f'AI Agent: "{_clip(instruction, 120)}"', entity_type='candidate', entity_id=cid,
                     meta={'actions': [t['action'] for t in tasks]})
        return jsonify({'ok': True, 'understood': understood, 'question': question, 'batch': batch,
                        'tasks': _list(conn, cid)})
    except AgentError as e:
        return _err(e)
    finally:
        conn.close()


@bp.route('/candidates/<int:cid>/agent/tasks/<int:tid>/approve', methods=['POST'])
@login_required
def agent_approve(cid, tid):
    """Approve a draft (or a held e-mail) with the recruiter's final edits."""
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        t = _task(conn, cid, tid)
        if t['status'] not in ('draft', 'held'):
            raise AgentError('This action was already handled.', 409)
        if t['action'] not in ('email', 'whatsapp', 'reminder'):
            raise AgentError('Nothing to approve.')
        now = _now()
        vals = {}
        body = str(d.get('body', t['body']) or '').strip()[:6000]
        if not body:
            raise AgentError('The message is empty.')
        vals['body'] = body
        if t['action'] == 'email':
            subject = re.sub(r'\s+', ' ', str(d.get('subject', t['subject']) or '')).strip()[:200]
            to = str(d.get('to', t['to_addr']) or '').strip()
            if not subject:
                raise AgentError('The subject is empty.')
            if not _VALID_EMAIL.match(to):
                raise AgentError('Enter a valid e-mail address.')
            c = _candidate(conn, cid, effective_company_id()) or {}
            if c.get('do_not_email') or c.get('unsub_at'):
                raise AgentError('This candidate unsubscribed from e-mails.')
            vals.update(subject=subject, to_addr=to)
        if t['action'] == 'whatsapp':
            to = str(d.get('to', t['to_addr']) or '').strip()
            if len(re.sub(r'\D', '', to)) < 10:
                raise AgentError('Enter a valid phone number.')
            vals['to_addr'] = to
        if d.get('now') or t['status'] == 'held':
            when = now
        else:
            when = parse_when(d.get('run_at')) if d.get('run_at') else parse_when(t['run_at'])
            if not when:
                raise AgentError('Pick a date and time.')
            if when < now - datetime.timedelta(minutes=2):
                raise AgentError('That time has already passed. Pick a new time.')
        vals.update(run_at=_fmt(max(when, now)), approved_by=real_user_id(), approved_at=ts(), error='')
        due_now = when <= now + datetime.timedelta(minutes=1)
        vals['status'] = 'running' if due_now or t['action'] == 'reminder' else 'scheduled'
        _set(conn, tid, **vals)
        if vals['status'] == 'running':
            execute(conn, _task(conn, cid, tid), recheck=False)
        t = _task(conn, cid, tid)
        if t['status'] == 'failed':
            return jsonify({'error': t['error'] or 'Failed', 'task': _public(t)}), 400
        log_activity('agent.approved', f'AI Agent {t["action"]} approved', entity_type='candidate', entity_id=cid,
                     meta={'task_id': tid, 'status': t['status'], 'run_at': t['run_at']})
        return jsonify({'ok': True, 'task': _public(t)})
    except AgentError as e:
        return _err(e)
    finally:
        conn.close()


@bp.route('/candidates/<int:cid>/agent/tasks/<int:tid>/cancel', methods=['POST'])
@login_required
def agent_cancel(cid, tid):
    conn = get_db()
    try:
        t = _task(conn, cid, tid)
        if t['status'] not in OPEN_STATUSES and t['action'] != 'answer':
            raise AgentError('This action was already handled.', 409)
        _set(conn, tid, status='cancelled', done_at=ts())
        return jsonify({'ok': True, 'task': _public(_task(conn, cid, tid))})
    except AgentError as e:
        return _err(e)
    finally:
        conn.close()


def _wa_log(conn, t, body, via, wa_id=''):
    who = _recruiter_name(conn, real_user_id())
    conn.execute('INSERT INTO candidate_events (candidate_id,event_type,detail,created_at) VALUES (?,?,?,?)',
                 (t['candidate_id'], 'whatsapp', f'WhatsApp sent ({via})\n\n{body}\n— {who} (AI Agent draft)', ts()))
    _set(conn, t['id'], status='sent', body=body, result=wa_id or via, done_at=ts())


def _wa_body(d, t):
    body = str(d.get('body', t['body']) or '').strip()[:4000]
    if not body:
        raise AgentError('The message is empty.')
    return body


@bp.route('/candidates/<int:cid>/agent/tasks/<int:tid>/wa-send', methods=['POST'])
@login_required
def agent_wa_send(cid, tid):
    """The recruiter presses Send: deliver through the WhatsApp Business API."""
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        t = _task(conn, cid, tid)
        if t['action'] != 'whatsapp' or t['status'] not in ('ready', 'draft'):
            raise AgentError('This message was already handled.', 409)
        if not _wa_api_ready():
            raise AgentError('The WhatsApp Business API is not connected. Use "Open WhatsApp" instead.')
        body = _wa_body(d, t)
        from modules.wa_agent import wa_send_text
        ok, res = wa_send_text(t['to_addr'], body)
        if not ok:
            raise AgentError(f'WhatsApp did not accept the message: {_clip(res, 160)}', 502)
        _wa_log(conn, t, body, 'WhatsApp Business API', res)
        return jsonify({'ok': True, 'task': _public(_task(conn, cid, tid))})
    except AgentError as e:
        return _err(e)
    finally:
        conn.close()


@bp.route('/candidates/<int:cid>/agent/tasks/<int:tid>/wa-done', methods=['POST'])
@login_required
def agent_wa_done(cid, tid):
    """The recruiter sent it from WhatsApp on their phone / WhatsApp Web."""
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        t = _task(conn, cid, tid)
        if t['action'] != 'whatsapp' or t['status'] not in ('ready', 'draft'):
            raise AgentError('This message was already handled.', 409)
        _wa_log(conn, t, _wa_body(d, t), 'WhatsApp')
        return jsonify({'ok': True, 'task': _public(_task(conn, cid, tid))})
    except AgentError as e:
        return _err(e)
    finally:
        conn.close()
