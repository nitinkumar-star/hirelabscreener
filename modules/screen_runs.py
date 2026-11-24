"""
Naukri Screener analyses — kept, so they can be exported and checked (Nov 2026).

The Screener (modules/naukri_screen.py) judges Naukri search cards for a job.
Until now nothing of an analysis was kept, so a recruiter could not take an
analysis to Claude and ask "are these verdicts right? what is the screener
getting wrong?". Now every batch the extension sends is saved; batches of the
same recruiter on the same job within 20 minutes form ONE analysis.

Export (in the ATS: the job's Job Breakdown card -> "Naukri screening analyses";
from the extension: open the export_url that every Screen answer now carries;
from Claude: the connector reads /api/screen-runs ... with ats_read):
  * Markdown — made for Claude: how the score works, the exact instructions
    the AI got (job, breakdown, rules, past decisions), every card with the
    AI's verdict, score, score parts, facts read from the card, reason and
    flags, and a checklist of what to validate and how to suggest fixes.
  * CSV (one row per card) and JSON (everything).

Data: card text is stored REDACTED (no phone numbers, no e-mail addresses) and
capped, as screen_feedback.py already does. Analyses older than 60 days are
removed. Same access rules: own company, and a recruiter only sees analyses
of jobs assigned to them.

Routes
  GET /api/screen-runs?mandate_id=&limit=        analyses (newest first)
  GET /api/screen-runs/<id>?format=json|md|csv   one analysis (download=1 -> file)
  GET /api/screen-runs/last?mandate_id=&format=  the latest analysis of a job
"""

import io
import csv
import json
import hashlib
import datetime

from flask import Blueprint, request, jsonify, Response

from modules.shared import get_db, ts, effective_company_id, real_user_id, login_required, _core
from modules import register_migration

bp = Blueprint('screen_runs', __name__)

GROUP_MINUTES = 20
KEEP_DAYS = 60
CARD_CHARS = 1800


@register_migration
def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS ext_screen_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL DEFAULT 0,
        user_id INTEGER DEFAULT 0,
        mandate_id INTEGER NOT NULL,
        group_id INTEGER DEFAULT 0,
        n_cards INTEGER DEFAULT 0,
        n_open INTEGER DEFAULT 0,
        n_maybe INTEGER DEFAULT 0,
        n_skip INTEGER DEFAULT 0,
        prompt_hash TEXT DEFAULT '',
        system_prompt TEXT DEFAULT '',
        rules TEXT DEFAULT '[]',
        limits TEXT DEFAULT '{}',
        cards TEXT DEFAULT '[]',
        created_at TEXT DEFAULT ''
    )''')
    for sql in ('CREATE INDEX IF NOT EXISTS idx_ext_screen_runs_grp ON ext_screen_runs(company_id, group_id)',
                'CREATE INDEX IF NOT EXISTS idx_ext_screen_runs_m ON ext_screen_runs(company_id, mandate_id, id)'):
        conn.execute(sql)
    conn.commit()


def _now():
    return _core()._ist_now()


# ══════════════════════════════════════════════════════════════════════════
#  SAVE (called by naukri_screen.screen_profiles; never breaks a screening)
# ══════════════════════════════════════════════════════════════════════════
def save_run(conn, m, profiles, clean, results, system_prompt, rules, limits):
    """Keep one screening batch. Returns the analysis (group) id, or 0."""
    try:
        from modules.naukri_screen import redact
        co, uid = effective_company_id(), int(real_user_id() or 0)
        names = {}
        for p in profiles or []:
            if isinstance(p, dict):
                names[str(p.get('key') or '')[:80]] = str(p.get('name') or '').strip()[:120]
        text = {c['key']: c['text'] for c in clean}
        cards = []
        for r in results:
            k = r.get('key')
            t = redact(text.get(k, ''))[:CARD_CHARS]
            cards.append({'key': k, 'name': names.get(k) or (t.strip().split('\n')[0][:80] if t.strip() else ''),
                          'verdict': r.get('verdict'), 'score': r.get('score'), 'reason': r.get('reason') or '',
                          'flags': r.get('flags') or [], 'parts': r.get('parts') or {}, 'facts': r.get('facts') or {},
                          'dupe': (r.get('dupe') or {}).get('label') if isinstance(r.get('dupe'), dict) else '',
                          'card': t})
        cnt = {v: sum(1 for c in cards if c['verdict'] == v) for v in ('open', 'maybe', 'skip')}
        now = _now()
        since = (now - datetime.timedelta(minutes=GROUP_MINUTES)).isoformat(timespec='seconds')
        last = conn.execute('SELECT group_id FROM ext_screen_runs WHERE company_id=? AND user_id=? AND mandate_id=? '
                            'AND created_at>=? ORDER BY id DESC LIMIT 1', (co, uid, m['id'], since)).fetchone()
        sp = system_prompt or ''
        cur = conn.execute('INSERT INTO ext_screen_runs (company_id, user_id, mandate_id, group_id, n_cards, n_open, n_maybe, '
                           'n_skip, prompt_hash, system_prompt, rules, limits, cards, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (co, uid, m['id'], last['group_id'] if last else 0, len(cards), cnt['open'], cnt['maybe'],
                            cnt['skip'], hashlib.sha1(sp.encode('utf-8')).hexdigest()[:12], sp,
                            json.dumps(list(rules or []), ensure_ascii=False), json.dumps(limits or {}, ensure_ascii=False, default=str),
                            json.dumps(cards, ensure_ascii=False, default=str), ts()))
        gid = last['group_id'] if last else cur.lastrowid
        if not last:
            conn.execute('UPDATE ext_screen_runs SET group_id=? WHERE id=?', (gid, cur.lastrowid))
        old = (now - datetime.timedelta(days=KEEP_DAYS)).isoformat(timespec='seconds')
        conn.execute('DELETE FROM ext_screen_runs WHERE company_id=? AND created_at<?', (co, old))
        conn.commit()
        return gid
    except Exception as exc:
        print('[screen-runs] not saved: %s: %s' % (type(exc).__name__, exc))
        return 0


# ══════════════════════════════════════════════════════════════════════════
#  READ / EXPORT
# ══════════════════════════════════════════════════════════════════════════
def _err(msg, code):
    return jsonify({'ok': False, 'error': msg}), code


def _can_see(conn, mid):
    try:
        from modules.access import can_see_mandate
        return can_see_mandate(conn, mid)
    except Exception:
        return True


def _analysis(conn, gid):
    co = effective_company_id()
    rows = conn.execute('SELECT * FROM ext_screen_runs WHERE company_id=? AND group_id=? ORDER BY id', (co, gid)).fetchall()
    if not rows or not _can_see(conn, rows[0]['mandate_id']):
        return None
    m = conn.execute('SELECT id, role, client, location FROM mandates WHERE id=? AND owner_id=?',
                     (rows[0]['mandate_id'], co)).fetchone()
    u = conn.execute('SELECT display_name, username FROM users WHERE id=?', (rows[0]['user_id'],)).fetchone()
    cards, prompts = [], []
    for r in rows:
        for c in json.loads(r['cards'] or '[]'):
            c['batch'] = r['id']
            cards.append(c)
        if r['system_prompt'] and r['system_prompt'] not in prompts:
            prompts.append(r['system_prompt'])
    from modules.card_facts import OPEN_AT, MAYBE_AT
    return {
        'ok': True, 'analysis_id': gid, 'mandate_id': rows[0]['mandate_id'],
        'role': m['role'] if m else '', 'client': m['client'] if m else '', 'location': m['location'] if m else '',
        'by': (u['display_name'] or u['username']) if u else '', 'started_at': rows[0]['created_at'],
        'ended_at': rows[-1]['created_at'], 'batches': len(rows),
        'counts': {v: sum(1 for c in cards if c.get('verdict') == v) for v in ('open', 'maybe', 'skip', 'unknown')},
        'n_cards': len(cards), 'rules': json.loads(rows[-1]['rules'] or '[]'), 'limits': json.loads(rows[-1]['limits'] or '{}'),
        'thresholds': {'open_at': OPEN_AT, 'maybe_at': MAYBE_AT},
        'instructions_to_ai': prompts, 'cards': cards,
    }


SCORING = """Score (0-100) = AI part + code part.
- AI part: role_fit (0-4) x 7 + domain_fit (0-4) x 6 + core_fit (0-3) x 6  (max 70)
- Company: target 12, acceptable 7, unknown 4, past avoid-list 3, current avoid-list 0
- Experience vs the job's range: inside 8, just outside 4, far outside 0, no range 6, not read 4
- CTC vs budget: within 5, up to 10% over 2 (flag), more 0, no budget 4, not read 3
- Location: matches 5, unknown 3, does not match 1 (flag "relocation")
Verdict: OPEN if score >= {open_at}, MAYBE if >= {maybe_at}, else SKIP.
Hard rules force SKIP: current company on the avoid list; CTC above budget (unless a recruiter rule allows it);
the AI says the card breaks a recruiter rule; role_fit = 0 (wrong kind of role).
Experience, CTC, notice and company class are read from the card by a program (facts), not by the AI."""

ASK = """## What to check (Claude: please do this)
1. For EVERY card: agree or disagree with the verdict (OPEN / MAYBE / SKIP). If you disagree, say what it should be and why, quoting the card.
2. Check the facts the program read (experience, CTC, notice, company class) against the card text. List every wrong or missed fact.
3. Check the AI parts (role_fit, domain_fit, core_fit): which are too high or too low, and why.
4. Find PATTERNS, not just single mistakes: rules applied wrongly or not at all, a kind of profile always over- or under-rated, thresholds that cut in the wrong place, gaps in the job breakdown.
5. Suggest concrete fixes, each one line, grouped as:
   - New or changed RECRUITER RULES for this job (plain English, as the recruiter would type them in the Screener chat)
   - Changes to the JOB BREAKDOWN (must-haves, nice-to-haves, target / avoid companies, titles)
   - Changes to the SCORING or the AI INSTRUCTIONS (for the HireLab developer)
6. End with a short table: card number, name, screener verdict, your verdict, main reason."""


def to_markdown(a):
    L = []
    L.append('# Naukri Screener analysis — %s · %s' % (a['role'], a['client']))
    L.append('')
    c = a['counts']
    L.append('Analysis #%s · %d cards (OPEN %d · MAYBE %d · SKIP %d%s) · by %s · %s to %s'
             % (a['analysis_id'], a['n_cards'], c['open'], c['maybe'], c['skip'],
                (' · no verdict %d' % c['unknown']) if c.get('unknown') else '', a['by'] or '-',
                a['started_at'].replace('T', ' ')[:16], a['ended_at'].replace('T', ' ')[11:16]))
    L.append('')
    L.append('Phone numbers and e-mail addresses were removed from the card text before the AI saw it.')
    L.append('')
    L.append('## How the screener scores a card')
    L.append(SCORING.format(**a['thresholds']))
    L.append('')
    L.append("## The job's limits used by the program")
    L.append('```json\n' + json.dumps(a['limits'], ensure_ascii=False, indent=1, default=str) + '\n```')
    L.append('')
    L.append("## The recruiter's rules for this job (%d)" % len(a['rules']))
    L.extend(['- ' + str(r) for r in a['rules']] or ['(none)'])
    L.append('')
    L.append('## The exact instructions the AI got (job, breakdown, rules, past decisions)')
    for n, p in enumerate(a['instructions_to_ai']):
        if len(a['instructions_to_ai']) > 1:
            L.append('Version %d (the instructions changed during this analysis):' % (n + 1))
        L.append('````text\n' + p + '\n````')
    L.append('')
    L.append('## Cards and verdicts')
    for i, x in enumerate(a['cards'], 1):
        f, pr = x.get('facts') or {}, x.get('parts') or {}
        L.append('')
        L.append('### %d. %s — %s %s' % (i, x.get('name') or x.get('key'), str(x.get('verdict') or '').upper(),
                                          '' if x.get('score') is None else x['score']))
        L.append('- Reason: %s' % (x.get('reason') or '-'))
        if x.get('flags'):
            L.append('- Flags: %s' % ', '.join(map(str, x['flags'])))
        L.append('- Facts read by the program: experience %s yrs · CTC %s LPA · notice %s · current company %s (%s)'
                 % (f.get('exp'), f.get('ctc'), f.get('notice'), f.get('company_name') or '?', f.get('company') or 'unknown'))
        if pr:
            L.append('- Score parts: role_fit %s · domain_fit %s · core_fit %s · company %s · exp %s · ctc %s · location %s'
                     % tuple(pr.get(k) for k in ('role', 'domain', 'core', 'company', 'exp', 'ctc', 'location')))
        if x.get('dupe'):
            L.append('- Already in the ATS: %s' % x['dupe'])
        L.append('- Card text:')
        L.append('```text\n' + (x.get('card') or '').strip() + '\n```')
    L.append('')
    L.append(ASK)
    return '\n'.join(L) + '\n'


def to_csv(a):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['#', 'name', 'verdict', 'score', 'reason', 'flags', 'exp_yrs', 'ctc_lpa', 'notice', 'company', 'company_class',
                'role_fit', 'domain_fit', 'core_fit', 'pts_company', 'pts_exp', 'pts_ctc', 'pts_location', 'already_in_ats',
                'card_text', 'your_verdict', 'your_note'])
    for i, x in enumerate(a['cards'], 1):
        f, pr = x.get('facts') or {}, x.get('parts') or {}
        w.writerow([i, x.get('name'), x.get('verdict'), x.get('score'), x.get('reason'), '; '.join(map(str, x.get('flags') or [])),
                    f.get('exp'), f.get('ctc'), f.get('notice'), f.get('company_name'), f.get('company'),
                    pr.get('role'), pr.get('domain'), pr.get('core'), pr.get('company'), pr.get('exp'), pr.get('ctc'),
                    pr.get('location'), x.get('dupe') or '', (x.get('card') or '').strip(), '', ''])
    return '﻿' + buf.getvalue()


def _slug(s):
    import re
    return (re.sub(r'[^A-Za-z0-9]+', '-', s or '').strip('-')[:40] or 'job').lower()


def _out(a):
    fmt = (request.args.get('format') or 'json').lower()
    dl = request.args.get('download') in ('1', 'true', 'yes')
    base = 'screener-%s-%s-%s' % (_slug(a['role']), a['analysis_id'], a['started_at'][:10])
    if fmt in ('md', 'markdown'):
        body, mt, ext = to_markdown(a), 'text/markdown; charset=utf-8', 'md'
    elif fmt == 'csv':
        body, mt, ext = to_csv(a), 'text/csv; charset=utf-8', 'csv'
    else:
        if not dl:
            return jsonify(a)
        body, mt, ext = json.dumps(a, ensure_ascii=False, indent=1, default=str), 'application/json', 'json'
    h = {'Content-Disposition': '%s; filename="%s.%s"' % ('attachment' if dl else 'inline', base, ext)}
    return Response(body, headers=h, content_type=mt)


@bp.route('/api/screen-runs')
@login_required
def list_runs():
    """Naukri Screener analyses (newest first), optionally for one job (?mandate_id=)."""
    co = effective_company_id()
    try:
        limit = max(1, min(int(request.args.get('limit') or 20), 100))
    except ValueError:
        limit = 20
    q = ('SELECT r.group_id, r.mandate_id, MIN(r.created_at) AS started_at, MAX(r.created_at) AS ended_at, '
         'SUM(r.n_cards) AS n_cards, SUM(r.n_open) AS n_open, SUM(r.n_maybe) AS n_maybe, SUM(r.n_skip) AS n_skip, '
         'COUNT(*) AS batches, MAX(r.user_id) AS user_id, m.role, m.client FROM ext_screen_runs r '
         'LEFT JOIN mandates m ON m.id=r.mandate_id AND m.owner_id=r.company_id WHERE r.company_id=?')
    args = [co]
    if request.args.get('mandate_id'):
        try:
            args.append(int(request.args.get('mandate_id')))
        except ValueError:
            return _err('Invalid mandate_id', 400)
        q += ' AND r.mandate_id=?'
    q += ' GROUP BY r.group_id ORDER BY r.group_id DESC LIMIT ?'
    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute(q, args + [limit * 3]).fetchall()]
        rows = [r for r in rows if _can_see(conn, r['mandate_id'])][:limit]
        names = {}
        for r in rows:
            if r['user_id'] not in names:
                u = conn.execute('SELECT display_name, username FROM users WHERE id=?', (r['user_id'],)).fetchone()
                names[r['user_id']] = (u['display_name'] or u['username']) if u else ''
            r['by'] = names[r['user_id']]
            r['analysis_id'] = r.pop('group_id')
    finally:
        conn.close()
    return jsonify({'ok': True, 'analyses': rows, 'keep_days': KEEP_DAYS})


@bp.route('/api/screen-runs/last')
@login_required
def last_run():
    """The latest Naukri Screener analysis of a job (?mandate_id=), as json / md / csv (?format=)."""
    co = effective_company_id()
    try:
        mid = int(request.args.get('mandate_id') or 0)
    except ValueError:
        return _err('Invalid mandate_id', 400)
    conn = get_db()
    try:
        q, args = 'SELECT group_id FROM ext_screen_runs WHERE company_id=?', [co]
        if mid:
            q += ' AND mandate_id=?'
            args.append(mid)
        r = conn.execute(q + ' ORDER BY id DESC LIMIT 1', args).fetchone()
        a = _analysis(conn, r['group_id']) if r else None
    finally:
        conn.close()
    if not a:
        return _err('No screening analysis yet for this job. Run "Analyze this page" in the extension first.', 404)
    return _out(a)


@bp.route('/api/screen-runs/<int:gid>')
@login_required
def get_run(gid):
    """One Naukri Screener analysis with every card's verdict, as json / md (for Claude) / csv (?format=, ?download=1)."""
    conn = get_db()
    try:
        a = _analysis(conn, gid)
    finally:
        conn.close()
    if not a:
        return _err('Not found', 404)
    return _out(a)
