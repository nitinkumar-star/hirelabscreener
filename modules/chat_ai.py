"""
Team Chat — HireLab AI (the chat assistant) and the morning summary.

Both read the ATS for ONE person, with that person's access:
  - an agency admin sees the whole agency;
  - a recruiter sees only their assigned jobs (+ the Central Database pool),
    the same rule as modules/access.py — but computed here from the user id,
    so it also works in the background loop (no web request, no session).

  snapshot(conn, cid, uid, question)  ->  compact text the AI answers from
  answer(core, conn, cid, uid, question, history) -> reply text (DeepSeek)
  digest(conn, cid, uid)              ->  the morning message (no AI, no cost)

Candidates and jobs are written as <c:ID> / <m:ID>, so the chat shows them as
live cards (and each viewer still only sees what they may see).
"""

import datetime
import re

FREELANCER_ROLE = 'freelancer_sourcer'
_CLOSED_OFFER = ('accepted', 'declined', 'withdrawn', 'rejected', 'cancelled', 'joined')
_DONE_IV = ('cancelled', 'canceled', 'completed', 'done', 'no_show', 'no-show')


def _now(core):
    try:
        return core._ist_now()
    except Exception:
        return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)


def user_row(conn, uid):
    r = conn.execute("SELECT id, role, company_id, COALESCE(is_company_admin,0) AS adm, "
                     "COALESCE(NULLIF(display_name,''), username) AS name FROM users WHERE id=?", (uid,)).fetchone()
    return dict(r) if r else None


def scope(conn, cid, uid):
    """None = the whole agency; else the set of mandate ids this person may see."""
    u = user_row(conn, uid)
    if not u or u['role'] == 'admin' or int(u['adm'] or 0) == 1:
        return None
    from modules.access import user_mandate_ids, pool_mandate_id
    ids = set(user_mandate_ids(conn, uid, cid))
    pool = pool_mandate_id(conn, cid)
    if pool:
        ids.add(pool)
    return ids


def _in(sc, mid):
    return sc is None or int(mid or 0) in sc


def _names(conn, cid):
    return {r['id']: r['name'] for r in conn.execute(
        "SELECT id, COALESCE(NULLIF(display_name,''), username) AS name FROM users WHERE company_id=?", (cid,))}


def _when(s):
    s = str(s or '')
    try:
        d = datetime.datetime.fromisoformat(s[:19])
        return d.strftime('%a %d %b %I:%M %p').replace(' 0', ' ')
    except Exception:
        return s[:16]


def _hm(s):
    try:
        return datetime.datetime.fromisoformat(str(s)[:19]).strftime('%I:%M %p').lstrip('0')
    except Exception:
        return str(s)[11:16]


# ══════════════════════════════════════════════════════════════════════════
#  DATA (one person's view)
# ══════════════════════════════════════════════════════════════════════════
def interviews(conn, cid, sc, day_from, day_to):
    rows = conn.execute(
        "SELECT i.id, i.candidate_id, i.mandate_id, i.round_name, i.mode, i.scheduled_at, i.status, i.interviewer, "
        "c.name AS cname, m.role AS mrole, m.client AS mclient FROM interviews i "
        "LEFT JOIN candidates c ON c.id=i.candidate_id LEFT JOIN mandates m ON m.id=i.mandate_id "
        "WHERE i.owner_id=? AND substr(i.scheduled_at,1,10) BETWEEN ? AND ? ORDER BY i.scheduled_at",
        (cid, day_from, day_to)).fetchall()
    return [dict(r) for r in rows if _in(sc, r['mandate_id']) and str(r['status'] or '').lower() not in ('cancelled', 'canceled')]


def my_tasks(conn, cid, uid):
    rows = conn.execute(
        "SELECT id, note, due_at, candidate_id, candidate_name, priority FROM reminders WHERE owner_id=? AND done=0 "
        "AND (COALESCE(assigned_to,0)=? OR (COALESCE(assigned_to,0)=0 AND COALESCE(created_by,0)=?)) "
        "ORDER BY CASE WHEN due_at IS NULL OR due_at='' THEN 1 ELSE 0 END, due_at LIMIT 200", (cid, uid, uid)).fetchall()
    return [dict(r) for r in rows]


def stage_moves(conn, cid, sc, since, until=None):
    sql = ("SELECT h.candidate_id, h.from_stage, h.to_stage, h.created_at, COALESCE(h.actor_id,0) AS actor, "
           "c.name AS cname, c.mandate_id FROM stage_history h JOIN candidates c ON c.id=h.candidate_id "
           "WHERE c.owner_id=? AND h.created_at>=? " + ("AND h.created_at<? " if until else '') +
           "AND COALESCE(h.to_stage,'')!=COALESCE(h.from_stage,'') ORDER BY h.id DESC LIMIT 400")
    rows = conn.execute(sql, (cid, since, until) if until else (cid, since)).fetchall()
    return [dict(r) for r in rows if _in(sc, r['mandate_id'])]


def offers_pending(conn, cid, sc):
    try:
        rows = conn.execute("SELECT o.candidate_id, o.mandate_id, o.status, o.offered_ctc, o.expected_joining_date, c.name AS cname "
                            "FROM offers o LEFT JOIN candidates c ON c.id=o.candidate_id WHERE o.company_id=?", (cid,)).fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows if _in(sc, r['mandate_id']) and str(r['status'] or '').lower() not in _CLOSED_OFFER]


def jobs(conn, cid, sc):
    rows = conn.execute("SELECT id, role, client, location, status FROM mandates WHERE owner_id=? AND status='active' "
                        "ORDER BY id DESC LIMIT 200", (cid,)).fetchall()
    out = []
    for r in rows:
        if not _in(sc, r['id']):
            continue
        d = dict(r)
        d['stages'] = {s['stage'] or '—': s['n'] for s in conn.execute(
            'SELECT stage, COUNT(*) n FROM candidates WHERE mandate_id=? GROUP BY stage', (r['id'],))}
        out.append(d)
    return out


def new_candidates(conn, cid, sc, since, until):
    rows = conn.execute("SELECT mandate_id, COUNT(*) n FROM candidates WHERE owner_id=? AND created_at>=? AND created_at<? "
                        "GROUP BY mandate_id", (cid, since, until)).fetchall()
    return sum(r['n'] for r in rows if _in(sc, r['mandate_id']))


def find_candidates(conn, cid, sc, question):
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z.'-]{2,}", question or '') if w.lower() not in _STOP][:6]
    if not words:
        return []
    sql = ("SELECT c.id, c.name, c.company, c.designation, c.stage, c.mandate_id, c.experience, c.ctc_current, c.ctc_expected, "
           "c.notice_period, c.location, c.updated_at, m.role AS mrole, m.client AS mclient FROM candidates c "
           "LEFT JOIN mandates m ON m.id=c.mandate_id WHERE c.owner_id=? AND (" +
           ' OR '.join('LOWER(c.name) LIKE ?' for _ in words) + ') ORDER BY c.updated_at DESC LIMIT 40')
    rows = conn.execute(sql, [cid] + ['%' + w.lower() + '%' for w in words]).fetchall()
    out = []
    for r in rows:
        if not _in(sc, r['mandate_id']):
            continue
        name_words = set(re.findall(r'[a-z]+', (r['name'] or '').lower()))
        if not any(w.lower() in name_words for w in words):
            continue
        out.append(dict(r))
        if len(out) >= 6:
            break
    return out


_STOP = set('''aaj kal kitne kitna kitni kaun kya hai hain the and for with from what when where which who whom how many much
today tomorrow yesterday interviews interview offers offer pending tasks task stage stages job jobs candidate candidates
mera meri mere mujhe batao bata show list give tell please team week this that there their our your about status update
pipeline client clients shortlisted joined placed screening'''.split())


# ══════════════════════════════════════════════════════════════════════════
#  SNAPSHOT -> AI
# ══════════════════════════════════════════════════════════════════════════
def snapshot(core, conn, cid, uid, question=''):
    sc = scope(conn, cid, uid)
    now = _now(core)
    today = now.date()
    d = lambda x: x.isoformat()
    names = _names(conn, cid)
    u = user_row(conn, uid) or {}
    L = ['NOW: %s (IST). Asking: %s (%s).' % (now.strftime('%A %d %B %Y, %I:%M %p'), u.get('name', ''),
                                              'agency admin - sees the whole agency' if sc is None else 'recruiter - sees only their own jobs')]
    ivs = interviews(conn, cid, sc, d(today - datetime.timedelta(days=1)), d(today + datetime.timedelta(days=7)))
    L.append('\nINTERVIEWS (yesterday .. next 7 days):')
    L += ['- %s | <c:%s> %s | %s | %s%s | job <m:%s> %s' % (_when(i['scheduled_at']), i['candidate_id'], i['cname'] or '', i['round_name'] or '',
                                                        i['mode'] or '', (' | ' + i['status']) if i['status'] else '', i['mandate_id'], i['mrole'] or '')
          for i in ivs[:40]] or ['- none']
    tasks = my_tasks(conn, cid, uid)
    L.append('\nMY OPEN TASKS (%d):' % len(tasks))
    L += ['- %s%s%s' % (t['note'] or 'Task', (' | due ' + _when(t['due_at'])) if t['due_at'] else ' | no date',
                        (' | <c:%s>' % t['candidate_id']) if t['candidate_id'] else '') for t in tasks[:30]] or ['- none']
    offs = offers_pending(conn, cid, sc)
    L.append('\nOFFERS NOT CLOSED (%d):' % len(offs))
    L += ['- <c:%s> %s | %s | CTC %s | joining %s | job <m:%s>' % (o['candidate_id'], o['cname'] or '', o['status'] or '', o['offered_ctc'] or '-',
                                                                   o['expected_joining_date'] or '-', o['mandate_id']) for o in offs[:30]] or ['- none']
    moves = stage_moves(conn, cid, sc, d(today - datetime.timedelta(days=7)))
    L.append('\nSTAGE CHANGES (last 7 days, newest first, %d):' % len(moves))
    L += ['- %s | <c:%s> %s | %s -> %s | by %s' % (_when(m['created_at']), m['candidate_id'], m['cname'] or '', m['from_stage'] or '-',
                                                  m['to_stage'] or '-', names.get(m['actor'], 'system') if m['actor'] else 'system')
          for m in moves[:60]] or ['- none']
    js = jobs(conn, cid, sc)
    L.append('\nACTIVE JOBS (%d) with candidates per stage:' % len(js))
    L += ['- <m:%s> %s (%s, %s): %s' % (j['id'], j['role'] or '', j['client'] or '', j['location'] or '',
                                        ', '.join('%s %d' % (k, v) for k, v in sorted(j['stages'].items(), key=lambda kv: -kv[1])) or 'no candidates')
          for j in js[:40]] or ['- none']
    found = find_candidates(conn, cid, sc, question)
    if found:
        L.append('\nCANDIDATES NAMED IN THE QUESTION:')
        for c in found:
            L.append('- <c:%s> %s | %s at %s | %s yrs | CTC %s -> %s | notice %s | %s | stage %s | job <m:%s> %s (%s)' % (
                c['id'], c['name'], c['designation'] or '-', c['company'] or '-', c['experience'] or '-', c['ctc_current'] or '-',
                c['ctc_expected'] or '-', c['notice_period'] or '-', c['location'] or '-', c['stage'] or '-', c['mandate_id'],
                c['mrole'] or '', c['mclient'] or ''))
            for h in conn.execute('SELECT from_stage, to_stage, created_at FROM stage_history WHERE candidate_id=? ORDER BY id DESC LIMIT 4',
                                  (c['id'],)):
                L.append('    history: %s %s -> %s' % (_when(h['created_at']), h['from_stage'] or '-', h['to_stage'] or '-'))
    L.append('\nTEAM: ' + ', '.join(sorted(set(names.values()))))
    return '\n'.join(L)[:24000]


AI_PROMPT = """You are "HireLab AI", the assistant inside a recruitment agency's team chat (HireLab ATS).
Answer the question using ONLY the DATA below. It is live from the ATS and already limited to what this
person may see. If the answer is not in the DATA, say so plainly and suggest where in HireLab to look.
Rules:
- Reply in the language of the question (English, Hindi or Hinglish), short and friendly, like a teammate.
- Use short bullet lines for lists. Give counts first ("Aaj 4 interviews hain:").
- When you mention a candidate write <c:ID>, a job <m:ID> - exactly those tokens, the chat turns them into cards.
- Never invent candidates, numbers, dates or people. Do not show phone numbers or e-mail addresses.
- You can only read. If asked to change something (move a stage, schedule, assign), say how to do it:
  the buttons on a candidate card in the chat (Stage, Assign, Interview, Add to job).

DATA
{data}"""


def answer(core, conn, cid, uid, question, history=None):
    key = ''
    try:
        key = (core.get_setting('deepseek_api_key') or '').strip()
    except Exception:
        pass
    if not key:
        return 'AI is not set up yet - an admin can add the DeepSeek API key in HireLab Settings.'
    data = snapshot(core, conn, cid, uid, question)
    msgs = [{'role': 'system', 'content': AI_PROMPT.format(data=data)}]
    for who, txt in (history or [])[-6:]:
        msgs.append({'role': who, 'content': txt[:1500]})
    msgs.append({'role': 'user', 'content': question[:2000]})
    payload = {'model': 'deepseek-chat', 'temperature': 0.2, 'max_tokens': 700, 'messages': msgs}
    try:
        resp = core.call_deepseek(key, payload, timeout=60, endpoint='team-chat-ai')
        if resp.status_code != 200:
            raise RuntimeError('DeepSeek returned HTTP %s' % resp.status_code)
        out = (resp.json()['choices'][0]['message']['content'] or '').strip()
    except Exception as exc:
        if type(exc).__name__ == 'TokenCapError':
            return 'The AI usage cap for this period is reached - ask your admin to raise it.'
        print('[chat-ai] DeepSeek failed: %s: %s' % (type(exc).__name__, exc))
        return 'Sorry, I could not reach the AI right now. Please try again in a minute.'
    return re.sub(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b', '[e-mail]', out)[:6000] or 'I have no answer for that.'


# ══════════════════════════════════════════════════════════════════════════
#  MORNING SUMMARY (no AI: plain counts, always correct, free)
# ══════════════════════════════════════════════════════════════════════════
def digest(core, conn, cid, uid):
    sc = scope(conn, cid, uid)
    now = _now(core)
    today = now.date()
    yest = today - datetime.timedelta(days=1)
    names = _names(conn, cid)
    first = (names.get(uid, '') or '').split(' ')[0] or 'there'
    L = ['☀️ **Good morning, %s!** Here is your day - %s' % (first, today.strftime('%A %d %B'))]

    moves = stage_moves(conn, cid, sc, yest.isoformat(), today.isoformat())
    newc = new_candidates(conn, cid, sc, yest.isoformat(), today.isoformat())
    ivy = interviews(conn, cid, sc, yest.isoformat(), yest.isoformat())
    L.append('\n**Yesterday**')
    y = []
    if moves:
        by_stage = {}
        for m in moves:
            by_stage[m['to_stage'] or '-'] = by_stage.get(m['to_stage'] or '-', 0) + 1
        y.append('• %d stage change%s: %s' % (len(moves), '' if len(moves) == 1 else 's',
                                             ', '.join('%s %d' % (k, v) for k, v in sorted(by_stage.items(), key=lambda kv: -kv[1])[:5])))
        good = [m for m in moves if re.search(r'shortlist|interview|offer|select|placed|joined|shared', m['to_stage'] or '', re.I)][:5]
        y += ['   ↳ <c:%s> → %s' % (m['candidate_id'], m['to_stage']) for m in good]
    if newc:
        y.append('• %d new candidate%s added' % (newc, '' if newc == 1 else 's'))
    if ivy:
        y.append('• %d interview%s were scheduled' % (len(ivy), '' if len(ivy) == 1 else 's'))
    if sc is None and moves:
        per = {}
        for m in moves:
            if m['actor']:
                per[names.get(m['actor'], '?')] = per.get(names.get(m['actor'], '?'), 0) + 1
        if per:
            y.append('• Team: ' + ', '.join('%s %d' % (k, v) for k, v in sorted(per.items(), key=lambda kv: -kv[1])[:6]))
    L += y or ['• Quiet day - no updates.']

    L.append('\n**Today**')
    t = []
    ivt = interviews(conn, cid, sc, today.isoformat(), today.isoformat())
    if ivt:
        t.append('🎯 %d interview%s:' % (len(ivt), '' if len(ivt) == 1 else 's'))
        t += ['   • %s - <c:%s> - %s%s' % (_hm(i['scheduled_at']), i['candidate_id'], i['round_name'] or 'Interview',
                                        (' (%s)' % i['mode']) if i['mode'] else '') for i in ivt[:8]]
    tasks = my_tasks(conn, cid, uid)
    tstr = today.isoformat()
    over = [x for x in tasks if x['due_at'] and x['due_at'][:10] < tstr]
    due = [x for x in tasks if x['due_at'] and x['due_at'][:10] == tstr]
    if due or over:
        t.append('✅ Tasks: %d due today%s' % (len(due), (', **%d overdue**' % len(over)) if over else ''))
        t += ['   • %s%s' % (x['note'] or 'Task', (' - <c:%s>' % x['candidate_id']) if x['candidate_id'] else '') for x in (over + due)[:6]]
    offs = offers_pending(conn, cid, sc)
    if offs:
        t.append('💼 %d offer%s still open: %s' % (len(offs), '' if len(offs) == 1 else 's',
                                                   ', '.join('<c:%s>' % o['candidate_id'] for o in offs[:5])))
    stale = []
    for j in jobs(conn, cid, sc)[:60]:
        last = conn.execute('SELECT MAX(h.created_at) m FROM stage_history h JOIN candidates c ON c.id=h.candidate_id '
                            'WHERE c.mandate_id=?', (j['id'],)).fetchone()['m']
        if not last or str(last)[:10] < (today - datetime.timedelta(days=7)).isoformat():
            stale.append(j)
    if stale:
        t.append('⏳ %d active job%s with no movement for 7+ days: %s' % (len(stale), '' if len(stale) == 1 else 's',
                                                                        ', '.join('<m:%s>' % j['id'] for j in stale[:4])))
    L += t or ['• Nothing pending. 🎉']
    L.append('\nAsk me anything, e.g. *@AI aaj kitne interviews hain?*')
    return '\n'.join(L)
