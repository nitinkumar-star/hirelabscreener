"""
RecruitOS — Tasks v2, Wave 5: smart quick-add + daily plan + morning summary.

  POST /api/todo/parse   {text, ai}  -> a DRAFT task (nothing is saved)
       Understands English + Hinglish:
         "kal 11 baje Rahul ko call L&T ke liye #urgent"
         "Send shortlist to Resolven friday 5pm !high @Riya"
         "har somvar pipeline review"
       Step 1 is a rules parser (instant, free, always available).
       Step 2 (optional) asks DeepSeek to read the sentence when the company
       has a key and AI quick-add is on; its answer is only TEXT (names, a date,
       a time). Every name is then matched against THIS tenant's own data with
       the caller's recruiter scope, so the model can never link a record the
       user may not see, and a bad answer simply falls back to the rules.
  GET  /api/todo/plan              -> today's ranked plan (+ AI summary, cached per day)
  Morning summary push (opt-in per user, prefs.morning_push) from the 60s loop.

The task is still CREATED through POST /api/todo/tasks, so every rule there
(tenancy, recruiter scope, access guard) applies unchanged.
"""

import re
import json
import time
import datetime
import threading
from flask import Blueprint, request, jsonify

from modules.shared import get_db, ts, effective_company_id, real_user_id, is_company_admin, login_required, _core
from modules import register_migration

bp = Blueprint('todo_ai', __name__, url_prefix='/api/todo')

WEEKDAYS = {
    'monday': 0, 'mon': 0, 'somvar': 0, 'somwar': 0, 'somvaar': 0,
    'tuesday': 1, 'tue': 1, 'tues': 1, 'mangalvar': 1, 'mangalwar': 1, 'mangal': 1,
    'wednesday': 2, 'wed': 2, 'budhvar': 2, 'budhwar': 2, 'budh': 2,
    'thursday': 3, 'thu': 3, 'thurs': 3, 'guruvar': 3, 'guruwar': 3, 'veervar': 3, 'brihaspativar': 3,
    'friday': 4, 'fri': 4, 'shukravar': 4, 'shukrawar': 4, 'shukra': 4,
    'saturday': 5, 'sat': 5, 'shanivar': 5, 'shaniwar': 5, 'shani': 5,
    'sunday': 6, 'sun': 6, 'ravivar': 6, 'raviwar': 6, 'itvaar': 6, 'itwar': 6, 'etvaar': 6,
}
MONTHS = {m: i + 1 for i, m in enumerate(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'])}
MONTHS.update({'january': 1, 'february': 2, 'march': 3, 'april': 4, 'june': 6, 'july': 7, 'august': 8,
               'september': 9, 'sept': 9, 'october': 10, 'november': 11, 'december': 12})
# words never treated as a person's name when matching candidates
STOP = set('''a an the to for of on at by in with and or ko ke ki ka se me mein par pe aur bhi hai tha hain
call calls phone send share follow followup follow-up up check mail email whatsapp wa msg message remind reminder
meet meeting interview review update cv resume jd offer client candidate profile profiles feedback task todo
today tomorrow kal aaj parso next week weekly daily monthly every har roz baje am pm subah shaam sham dopahar raat
morning evening night noon urgent zaroori high medium low priority din hafte mahine baad mein ke liye liye karna
karo kar dena lena hai please pls asap about re regarding salary ctc notice joining date status'''.split())

_rl_lock = threading.Lock()
_rl = {}                  # user -> [timestamps] for AI parse rate limiting
AI_LIMIT, AI_WINDOW = 40, 600


@register_migration
def _migrate_todo_ai(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS todo_daily_plan (
        user_id INTEGER NOT NULL,
        plan_date TEXT NOT NULL,
        company_id INTEGER DEFAULT 0,
        summary TEXT DEFAULT '',
        source TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        PRIMARY KEY (user_id, plan_date)
    )''')
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  RULES PARSER  (pure function — unit tested without a database)
# ══════════════════════════════════════════════════════════════════════════
def _next_weekday(today, wd):
    add = (wd - today.weekday()) % 7
    return today + datetime.timedelta(days=add or 7)


def _hour_24(h, ampm, part):
    """Resolve a clock hour. Without am/pm, business hours are assumed:
    1-7 -> afternoon/evening, 8-11 -> morning, 12 -> noon."""
    h = int(h)
    if ampm:
        a = ampm.lower().replace('.', '')
        if a.startswith('p') and h < 12:
            return h + 12
        if a.startswith('a') and h == 12:
            return 0
        return h
    if part in ('shaam', 'sham', 'evening', 'raat', 'night', 'dopahar', 'afternoon') and h < 12:
        return h + 12 if not (part in ('raat', 'night') and h == 12) else 0
    if part in ('subah', 'morning'):
        return 0 if h == 12 else h
    if 1 <= h <= 7:
        return h + 12
    return h


def parse_rules(text, today):
    """-> dict(title, date, time, priority, tags, recurrence, assignee_hint, understood)."""
    s = ' ' + (text or '').strip() + ' '
    out = {'date': None, 'time': None, 'priority': None, 'tags': [], 'recurrence': None,
           'assignee_hint': None, 'understood': []}

    def cut(m):
        nonlocal s
        s = s[:m.start()] + ' ' + s[m.end():]

    # #tags, !priority, @assignee
    for m in list(re.finditer(r'(?<=\s)#([^\s#]{1,40})', s))[::-1]:
        out['tags'].insert(0, m.group(1)); cut(m)
    m = re.search(r'(?<=\s)!(high|medium|low|h|m|l|1|2|3)(?=\s)', s, re.I)
    if m:
        p = m.group(1).lower()
        out['priority'] = {'h': 'high', '1': 'high', 'm': 'medium', '2': 'medium', 'l': 'low', '3': 'low'}.get(p, p); cut(m)
    m = re.search(r'(?<=\s)(urgent|urgently|zaroori|zaruri|asap)(?=[\s,.!:;-])', s, re.I)
    if m and not out['priority']:
        out['priority'] = 'high'; cut(m)
    if not out['priority'] and any(t.lower() in ('urgent', 'zaroori', 'asap') for t in out['tags']):
        out['priority'] = 'high'
    m = re.search(r'(?<=\s)@([^\s@]{2,40})', s)
    if m:
        out['assignee_hint'] = m.group(1); cut(m)

    # recurrence
    m = re.search(r'(?<=\s)(?:every|har)\s+(' + '|'.join(sorted(WEEKDAYS, key=len, reverse=True)) + r')\b', s, re.I)
    if m:
        wd = WEEKDAYS[m.group(1).lower()]
        out['recurrence'] = {'freq': 'weekly', 'interval': 1, 'weekdays': [wd]}
        out['date'] = _next_weekday(today, wd) if today.weekday() != wd else today
        cut(m)
    else:
        for rx, rule in ((r'(?:every\s*day|everyday|daily|har\s+din|roz(?:ana)?|rozana)', {'freq': 'daily', 'interval': 1}),
                         (r'(?:every\s+week|weekly|har\s+hafte|har\s+hafta)', {'freq': 'weekly', 'interval': 1}),
                         (r'(?:every\s+month|monthly|har\s+mahine|har\s+mahina)', {'freq': 'monthly', 'interval': 1})):
            m = re.search(r'(?<=\s)' + rx + r'(?=[\s,.!])', s, re.I)
            if m:
                out['recurrence'] = dict(rule); cut(m)
                break

    # date
    if out['date'] is None:
        pats = [
            (r'(?:day\s+after\s+tomorrow|parso|parson)', lambda m: today + datetime.timedelta(days=2)),
            (r'(?:aaj|today|tonight|abhi)', lambda m: today),
            (r'(?:tomorrow|tmrw|tmr|kal)', lambda m: today + datetime.timedelta(days=1)),
            (r'(?:next\s+week|agle\s+hafte|agla\s+hafta)', lambda m: _next_weekday(today, 0)),
            (r'(?:in\s+(\d{1,2})\s+days?|(\d{1,2})\s+din\s+(?:baad|bad|mein|me))',
             lambda m: today + datetime.timedelta(days=int(m.group(1) or m.group(2)))),
            (r'(?:next\s+|is\s+|this\s+|agle\s+)?(' + '|'.join(sorted(WEEKDAYS, key=len, reverse=True)) + r')(?:\s+ko)?',
             lambda m: _next_weekday(today, WEEKDAYS[m.group(1).lower()])),
            (r'(\d{1,2})(?:st|nd|rd|th)?\s+(' + '|'.join(sorted(MONTHS, key=len, reverse=True)) + r')\.?(?:\s+(\d{4}))?',
             lambda m: _mkdate(today, int(m.group(1)), MONTHS[m.group(2).lower()], m.group(3))),
            (r'(' + '|'.join(sorted(MONTHS, key=len, reverse=True)) + r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?',
             lambda m: _mkdate(today, int(m.group(2)), MONTHS[m.group(1).lower()], m.group(3))),
            (r'(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?', lambda m: _mkdate(today, int(m.group(1)), int(m.group(2)), m.group(3))),
        ]
        for rx, fn in pats:
            m = re.search(r'(?<=\s)' + rx + r'(?=[\s,.!])', s, re.I)
            if m:
                d = fn(m)
                if d:
                    out['date'] = d; cut(m)
                    break

    # time (after dates so "15/10" is not read as a time)
    parts = r'(subah|morning|shaam|sham|evening|dopahar|afternoon|raat|night)'
    tpats = [
        r'(?:at\s+|by\s+)?' + parts + r'\s+(\d{1,2})(?::(\d{2}))?\s*(?:baje|bje|o\'?clock)?',
        r'(?:at\s+|by\s+)?(\d{1,2})(?::|\.)(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?(?:\s*(?:baje|bje))?',
        r'(?:at\s+|by\s+)?(\d{1,2})\s*(am|pm|a\.m\.|p\.m\.)',
        r'(?:at\s+|by\s+)?(\d{1,2})\s*(?:baje|bje|o\'?clock)',
        r'(noon|dopahar\s+12)',
    ]
    for i, rx in enumerate(tpats):
        m = re.search(r'(?<=\s)' + rx + r'(?=[\s,.!])', s, re.I)
        if not m:
            continue
        g = m.groups()
        if i == 0:
            h, mi, ap, part = g[1], g[2] or '00', None, g[0].lower()
        elif i == 1:
            h, mi, ap, part = g[0], g[1], g[2], None
        elif i == 2:
            h, mi, ap, part = g[0], '00', g[1], None
        elif i == 3:
            h, mi, ap, part = g[0], '00', None, None
        else:
            h, mi, ap, part = '12', '00', 'pm', None
        if int(h) > 23 or int(mi) > 59:
            continue
        hh = _hour_24(h, ap, part)
        if 0 <= hh <= 23:
            out['time'] = '%02d:%s' % (hh, mi)
            cut(m)
            break
    if out['time'] and out['date'] is None:
        out['date'] = today

    title = re.sub(r'\s+', ' ', s).strip(' ,.-:;')
    title = re.sub(r'^(?:ko|at|on|by|ke liye)\s+|\s+(?:ko|at|on|by)$', '', title, flags=re.I).strip(' ,.-')
    out['title'] = title
    return out


def _mkdate(today, d, mo, y):
    try:
        if y:
            y = int(y)
            if y < 100:
                y += 2000
            return datetime.date(y, mo, d)
        dt = datetime.date(today.year, mo, d)
        return dt if dt >= today - datetime.timedelta(days=1) else datetime.date(today.year + 1, mo, d)
    except ValueError:
        return None


# ══════════════════════════════════════════════════════════════════════════
#  RESOLVE NAMES -> RECORDS  (tenant + recruiter scoped; never trusts AI ids)
# ══════════════════════════════════════════════════════════════════════════
def _words(text):
    return [w for w in re.findall(r"[A-Za-z][A-Za-z.'&-]+", text or '') if len(w) >= 3 and w.lower() not in STOP]


def resolve_candidates(conn, cid, text, person=None, limit=6):
    """Candidates whose name appears in `text` (or matches `person`).
    Full-name matches rank above first-name matches; active jobs first."""
    from modules.access import can_see_candidate
    probe = _words(person) if person else _words(text)
    if not probe:
        return []
    hay = ' ' + re.sub(r'\s+', ' ', (person or text or '').lower()) + ' '
    seen, found = set(), []
    for w in probe[:8]:
        rows = conn.execute(
            "SELECT c.id, c.name, c.mandate_id, c.phone, m.role, m.client, m.status FROM candidates c "
            "LEFT JOIN mandates m ON m.id=c.mandate_id WHERE c.owner_id=? AND (c.name LIKE ? OR c.name LIKE ?) "
            "ORDER BY CASE WHEN m.status='active' THEN 0 ELSE 1 END, c.updated_at DESC LIMIT 25",
            (cid, w + '%', '% ' + w + '%')).fetchall()
        for r in rows:
            if r['id'] in seen:
                continue
            seen.add(r['id'])
            nm = (r['name'] or '').strip()
            clean = re.sub(r'["()]', ' ', nm.lower())
            toks = [t for t in clean.split() if len(t) >= 2]
            if not toks:
                continue
            full = len(toks) >= 2 and all((' ' + t + ' ') in hay or (' ' + t) in hay for t in toks[:2])
            first = (' ' + toks[0] + ' ') in hay or (' ' + toks[0] + "'") in hay
            if not (full or first):
                continue
            if not can_see_candidate(conn, r['id']):
                continue
            found.append({'id': r['id'], 'name': nm, 'mandate_id': r['mandate_id'],
                          'mandate_label': ((r['role'] or '') + ' — ' + (r['client'] or '')) if r['role'] else '',
                          'active': (r['status'] == 'active'), 'score': (2 if full else 1) + (0.5 if r['status'] == 'active' else 0)})
    found.sort(key=lambda x: -x['score'])
    return found[:limit]


def resolve_mandate(conn, cid, text, client=None):
    """An ACTIVE job whose client name appears in the text (one clear match only)."""
    from modules.access import can_see_mandate
    hay = ' ' + (client or text or '').lower() + ' '
    rows = conn.execute("SELECT id, role, client FROM mandates WHERE owner_id=? AND status='active' "
                        "AND COALESCE(client,'')!='' ORDER BY id DESC LIMIT 400", (cid,)).fetchall()
    hits = []
    for r in rows:
        cl = (r['client'] or '').strip().lower()
        if len(cl) < 2:
            continue
        if re.search(r'(?<![a-z0-9])' + re.escape(cl) + r'(?![a-z0-9])', hay) and can_see_mandate(conn, r['id']):
            hits.append(r)
    clients = {(h['client'] or '').lower() for h in hits}
    if len(hits) == 1 or (hits and len(clients) == 1 and len(hits) <= 3):
        h = hits[0]
        return {'id': h['id'], 'label': (h['role'] or '') + ' — ' + (h['client'] or ''), 'client': h['client'],
                'others': len(hits) - 1}
    return None


def resolve_assignee(conn, cid, hint):
    if not hint:
        return None
    hint = hint.strip().lower()
    for u in conn.execute("SELECT id, display_name, username FROM users WHERE company_id=? "
                          "AND COALESCE(status,'approved')='approved'", (cid,)):
        for nm in (u['display_name'] or '', u['username'] or ''):
            n = nm.lower()
            if n and (n == hint or n.split()[0] == hint or n.startswith(hint)):
                return {'id': u['id'], 'name': u['display_name'] or u['username']}
    return None


# ══════════════════════════════════════════════════════════════════════════
#  AI  (DeepSeek, optional)
# ══════════════════════════════════════════════════════════════════════════
def _ai_configured():
    """The company has a DeepSeek key (independent of any user's switch)."""
    try:
        return bool((_core().get_setting('deepseek_api_key') or '').strip())
    except Exception:
        return False


def _ai_enabled():
    """A DeepSeek key is configured AND this user has not switched AI off
    (Tasks -> ... -> AI assist; per-user, default on)."""
    core = _core()
    try:
        if not (core.get_setting('deepseek_api_key') or '').strip():
            return False
        uid = int(real_user_id() or 0)
        conn = get_db()
        try:
            r = conn.execute('SELECT prefs FROM todo_prefs WHERE user_id=?', (uid,)).fetchone()
        finally:
            conn.close()
        prefs = json.loads(r['prefs']) if r and r['prefs'] else {}
        return prefs.get('ai', True) is not False
    except Exception:
        return False


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


AI_PROMPT = """You turn a recruiter's short note into a to-do. The note may be English, Hindi or Hinglish.
Today is {today} ({weekday}), timezone India. Reply with ONLY a JSON object:
{{"title": short imperative task text in the note's language WITHOUT the date/time words (keep names),
 "date": "YYYY-MM-DD" or null, "time": "HH:MM" 24h or null,
 "priority": "high"|"medium"|"low"|null,
 "recurrence": {{"freq":"daily|weekly|monthly","interval":1,"weekdays":[0-6 Mon=0]}} or null,
 "person": candidate/person name mentioned or null, "client": company/client name mentioned or null,
 "assignee": teammate the task is FOR (e.g. "Riya ko bolo", "@Riya") or null}}
"kal" means tomorrow. Times without am/pm between 1 and 7 are pm. Do not invent anything not in the note."""


def ai_parse(text, today):
    core = _core()
    key = (core.get_setting('deepseek_api_key') or '').strip()
    payload = {'model': 'deepseek-chat', 'temperature': 0, 'max_tokens': 300,
               'response_format': {'type': 'json_object'},
               'messages': [{'role': 'system', 'content': AI_PROMPT.format(today=today.isoformat(),
                                                                          weekday=today.strftime('%A'))},
                            {'role': 'user', 'content': text[:500]}]}
    resp = core.call_deepseek(key, payload, timeout=8, endpoint='todo-quick-add')
    data = resp.json()
    raw = data['choices'][0]['message']['content']
    j = core.parse_json(raw) if hasattr(core, 'parse_json') else json.loads(raw)
    if not isinstance(j, dict):
        raise ValueError('AI answer was not an object')
    return j


def _valid_date(v, today):
    try:
        d = datetime.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None
    if d < today - datetime.timedelta(days=1) or d > today + datetime.timedelta(days=730):
        return None
    return d


# ══════════════════════════════════════════════════════════════════════════
#  /parse
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/parse', methods=['POST'])
@login_required
def todo_parse():
    from modules.todo import parse_recurrence, PRIORITIES, _norm_tags
    d = request.get_json(silent=True) or {}
    text = str(d.get('text') or '').strip()[:500]
    if not text:
        return jsonify({'ok': False, 'error': 'text required'}), 400
    cid, uid = int(effective_company_id() or 0), int(real_user_id() or 0)
    today = _core()._ist_now().date()
    r = parse_rules(text, today)
    source, ai_note = 'rules', ''
    person = client = None
    assignee_hint = r['assignee_hint']
    want_ai = bool(d.get('ai')) and _ai_enabled()
    if want_ai and not _rate_ok(uid):
        want_ai, ai_note = False, 'AI limit reached for a few minutes; used the quick parser.'
    if want_ai:
        try:
            a = ai_parse(text, today)
            source = 'ai'
            t = str(a.get('title') or '').strip()
            if t:
                r['title'] = t[:500]
            ad = _valid_date(a.get('date'), today) if a.get('date') else None
            if ad:
                r['date'] = ad
            at = str(a.get('time') or '')
            if re.match(r'^([01]\d|2[0-3]):[0-5]\d$', at):
                r['time'] = at
                r['date'] = r['date'] or today
            if not r['priority'] and a.get('priority') in ('high', 'medium', 'low'):
                r['priority'] = a['priority']
            if not r['recurrence'] and isinstance(a.get('recurrence'), dict):
                rec, err = parse_recurrence(a['recurrence'])
                if not err and rec:
                    r['recurrence'] = json.loads(rec)
            person = (str(a.get('person') or '').strip() or None)
            client = (str(a.get('client') or '').strip() or None)
            assignee_hint = assignee_hint or (str(a.get('assignee') or '').strip() or None)
        except Exception as e:
            source, ai_note = 'rules', 'AI unavailable; used the quick parser.'
            print(f'[todo-ai] parse fallback: {e}')

    conn = get_db()
    try:
        cands = resolve_candidates(conn, cid, text, person)
        if not cands and person:
            cands = resolve_candidates(conn, cid, text)
        cand = None
        if cands:
            top = cands[0]
            clear = len(cands) == 1 or top['score'] > cands[1]['score']
            cand = top if clear else None
        mand = None if cand else resolve_mandate(conn, cid, text, client)
        if not mand and client and not cand:
            mand = resolve_mandate(conn, cid, text)
        asg = resolve_assignee(conn, cid, assignee_hint)
    finally:
        conn.close()

    due = ''
    if r['date']:
        due = r['date'].isoformat() + ('T' + r['time'] if r['time'] else '')
    pr = r['priority'] if r['priority'] in PRIORITIES else None
    understood = []
    if due:
        dd = r['date']
        lbl = 'Today' if dd == today else 'Tomorrow' if dd == today + datetime.timedelta(days=1) else dd.strftime('%a %d %b')
        if r['time']:
            hh, mm = int(r['time'][:2]), r['time'][3:]
            lbl += ', %d:%s %s' % (hh % 12 or 12, mm, 'am' if hh < 12 else 'pm')
        understood.append({'k': 'due', 'label': lbl})
    if r['recurrence']:
        understood.append({'k': 'repeat', 'label': 'Repeats ' + r['recurrence']['freq']})
    if cand:
        understood.append({'k': 'candidate', 'label': cand['name']})
    elif mand:
        understood.append({'k': 'mandate', 'label': mand['label']})
    if asg:
        understood.append({'k': 'assignee', 'label': '→ ' + asg['name']})
    if pr:
        understood.append({'k': 'priority', 'label': pr.title()})
    for t in r['tags']:
        understood.append({'k': 'tag', 'label': '#' + t})
    draft = {
        'title': r['title'] or text, 'due_at': due, 'priority': pr, 'tags': _norm_tags(r['tags']),
        'recurrence': r['recurrence'],
        'candidate_id': cand['id'] if cand else 0, 'candidate_name': cand['name'] if cand else '',
        'mandate_id': (cand['mandate_id'] if cand else (mand['id'] if mand else None)),
        'mandate_label': (cand['mandate_label'] if cand else (mand['label'] if mand else '')),
        'assigned_to': asg['id'] if asg else 0, 'assigned_name': asg['name'] if asg else '',
    }
    options = [] if cand else [{'id': c['id'], 'name': c['name'], 'mandate_label': c['mandate_label']} for c in cands[:5]]
    return jsonify({'ok': True, 'draft': draft, 'understood': understood, 'source': source,
                    'candidate_options': options, 'ai_available': _ai_configured(), 'note': ai_note})


# ══════════════════════════════════════════════════════════════════════════
#  DAILY PLAN
# ══════════════════════════════════════════════════════════════════════════
PRIO_W = {'high': 30, 'medium': 15, 'low': 5, 'none': 0}
ATS_W = {'interview': 40, 'promise': 28, 'updated': 22, 'submission': 14, 'stale': 12}


def build_plan(tasks, ats, today_iso, now_hm):
    """Deterministic ranking. AI only writes the summary sentence, never the order."""
    items = []
    for t in tasks:
        if t['done']:
            continue
        due = t['due_at'] or ''
        grp = t['group']
        if grp not in ('overdue', 'today') and not t['my_day']:
            continue
        score = PRIO_W.get(t['priority'], 0)
        why = []
        if grp == 'overdue':
            days = (datetime.date.fromisoformat(today_iso) - datetime.date.fromisoformat(due[:10])).days
            score += 50 + min(days, 10) * 3
            why.append('overdue %d day%s' % (days, '' if days == 1 else 's'))
        elif grp == 'today':
            score += 35
            if len(due) > 10:
                hm = due[11:16]
                score += 15 if hm >= now_hm else 25
                why.append(('was due ' if hm < now_hm else 'at ') + hm)
            else:
                why.append('due today')
        if t['my_day']:
            score += 10
            why.append('in My Day')
        if t['candidate_id']:
            score += 8
        if t['priority'] in ('high', 'medium'):
            why.append(t['priority'] + ' priority')
        items.append({'kind': 'task', 'id': t['id'], 'title': t['title'], 'score': score, 'why': ', '.join(why),
                      'candidate_id': t['candidate_id'], 'candidate_name': t['candidate_name'],
                      'phone': t.get('candidate_phone') or '',
                      'due_at': due, 'my_day': t['my_day'], 'priority': t['priority']})
    for a in ats:
        score = ATS_W.get(a['type'], 10) + (20 if a['section'] == 'overdue' else 0)
        items.append({'kind': 'ats', 'key': a['key'], 'type': a['type'], 'ref_id': a['ref_id'], 'title': a['title'],
                      'score': score, 'why': a['kind'] + ' — ' + a['subtitle'], 'candidate_id': a['candidate_id'],
                      'mandate_id': a['mandate_id'], 'phone': a['phone'], 'section': a['section']})
    items.sort(key=lambda x: -x['score'])
    return items


def _rule_summary(items, counts):
    if not items:
        return 'Nothing urgent today. Good day to source new profiles or follow up with clients.'
    bits = []
    if counts['overdue']:
        bits.append('%d overdue' % counts['overdue'])
    if counts['interviews']:
        bits.append('%d interview%s' % (counts['interviews'], '' if counts['interviews'] == 1 else 's'))
    if counts['today']:
        bits.append('%d due today' % counts['today'])
    if counts['ats']:
        bits.append('%d ATS follow-up%s' % (counts['ats'], '' if counts['ats'] == 1 else 's'))
    first = items[0]
    return ('Today: ' + ', '.join(bits) + '. ' if bits else '') + 'Start with: ' + first['title'] + '.'


def _ai_summary(items, counts, name):
    core = _core()
    key = (core.get_setting('deepseek_api_key') or '').strip()
    top = [{'task': i['title'], 'why': i['why']} for i in items[:8]]
    payload = {'model': 'deepseek-chat', 'temperature': 0.3, 'max_tokens': 160,
               'messages': [{'role': 'system', 'content':
                             'You are a recruitment team lead. In 2 short sentences of simple Hinglish, tell the recruiter '
                             'what to focus on today and in what order. Use only the tasks given. No greetings, no lists.'},
                            {'role': 'user', 'content': json.dumps({'recruiter': name, 'counts': counts, 'top_tasks': top})}]}
    resp = core.call_deepseek(key, payload, timeout=10, endpoint='todo-daily-plan')
    txt = resp.json()['choices'][0]['message']['content'].strip()
    return re.sub(r'\s+', ' ', txt)[:400]


@bp.route('/plan', methods=['GET'])
@login_required
def todo_plan():
    from modules import todo
    core = _core()
    cid, uid = int(effective_company_id() or 0), int(real_user_id() or 0)
    now = core._ist_now()
    today_iso = now.date().isoformat()
    conn = get_db()                                   # same scope + serializer as the task list
    where, params = todo._task_where('mine')
    rows = conn.execute('SELECT r.*, c.phone AS cand_phone FROM reminders r LEFT JOIN candidates c ON c.id=r.candidate_id '
                        'WHERE ' + where + " AND r.done=0 AND ((r.due_at!='' AND substr(r.due_at,1,10)<=?) OR r.my_day_date=?)",
                        params + [today_iso, today_iso]).fetchall()
    tasks = todo._serialize_many(conn, rows, now)
    in_day = {t['candidate_id'] for t in tasks if t['candidate_id']}
    ats = todo._ats_suggestions(in_day)
    items = build_plan(tasks, ats, today_iso, now.strftime('%H:%M'))
    counts = {'overdue': sum(1 for t in tasks if t['group'] == 'overdue'),
              'today': sum(1 for t in tasks if t['group'] == 'today'),
              'interviews': sum(1 for a in ats if a['type'] == 'interview'),
              'ats': len(ats)}
    refresh = request.args.get('refresh') == '1'
    cached = conn.execute('SELECT summary, source FROM todo_daily_plan WHERE user_id=? AND plan_date=?',
                          (uid, today_iso)).fetchone()
    summary, source = (cached['summary'], cached['source']) if (cached and not refresh) else (None, None)
    if summary is None:
        summary, source = _rule_summary(items, counts), 'rules'
        if items and _ai_enabled() and _rate_ok(uid):
            try:
                u = core.current_user()
                summary = _ai_summary(items, counts, (u['display_name'] or u['username']) if u else '') or summary
                source = 'ai'
            except Exception as e:
                print(f'[todo-ai] plan summary fallback: {e}')
        conn.execute('INSERT OR REPLACE INTO todo_daily_plan (user_id, plan_date, company_id, summary, source, created_at) '
                     'VALUES (?,?,?,?,?,?)', (uid, today_iso, cid, summary, source, ts()))
        conn.commit()
    conn.close()
    return jsonify({'ok': True, 'date': today_iso, 'summary': summary, 'source': source,
                    'counts': counts, 'items': items[:12], 'total': len(items)})


# ══════════════════════════════════════════════════════════════════════════
#  MORNING SUMMARY PUSH  (opt-in: prefs.morning_push; called from the 60s loop)
# ══════════════════════════════════════════════════════════════════════════
MORNING_AT = '09:30'


def morning_counts(conn, company_id, uid, today):
    rows = conn.execute("SELECT due_at FROM reminders WHERE owner_id=? AND done=0 AND due_at!='' "
                        "AND substr(due_at,1,10)<=? AND (COALESCE(assigned_to,0)=? OR "
                        "(COALESCE(assigned_to,0)=0 AND COALESCE(created_by,0)=?))",
                        (company_id, today.isoformat(), uid, uid)).fetchall()
    overdue = sum(1 for r in rows if r['due_at'][:10] < today.isoformat())
    return overdue, len(rows) - overdue


def run_morning_push(conn, now=None, send=None):
    """Send each opted-in user one summary per day after 09:30 IST. Returns count sent."""
    core = _core()
    now = now or core._ist_now()
    if now.strftime('%H:%M') < MORNING_AT:
        return 0
    send = send or core._send_fcm_to_user
    today = now.date()
    sent = 0
    for p in conn.execute('SELECT user_id, company_id, prefs FROM todo_prefs').fetchall():
        try:
            prefs = json.loads(p['prefs'] or '{}')
        except Exception:
            continue
        if not prefs.get('morning_push'):
            continue
        key = 'mp:%d' % p['user_id']
        st = conn.execute('SELECT value FROM todo_auto_state WHERE key=?', (key,)).fetchone()
        if st and st['value'] == today.isoformat():
            continue
        u = conn.execute("SELECT company_id FROM users WHERE id=? AND COALESCE(status,'approved')='approved'",
                         (p['user_id'],)).fetchone()
        if not u:
            continue
        overdue, due_today = morning_counts(conn, int(u['company_id'] or 0), p['user_id'], today)
        conn.execute('INSERT OR REPLACE INTO todo_auto_state (key, value) VALUES (?,?)', (key, today.isoformat()))
        conn.commit()
        if not (overdue or due_today):
            continue                                 # nothing to say: no push
        body = ', '.join(x for x in ((('%d overdue' % overdue) if overdue else ''),
                                     (('%d due today' % due_today) if due_today else '')) if x)
        try:
            send(p['user_id'], {'action': 'tasks', 'kind': 'morning', 'title': 'Your tasks today', 'body': body},
                 notification={'title': 'Good morning — your tasks today', 'body': body + '. Open Tasks to plan your day.'})
            sent += 1
        except Exception as e:
            print(f'[todo-ai] morning push failed for {p["user_id"]}: {e}')
    return sent
