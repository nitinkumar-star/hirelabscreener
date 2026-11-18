"""
Screener training — the recruiter's own decisions on Naukri search cards.

This is the data that teaches HireLab to source the way the recruiter does.
In the HireLab Assistant's Search tab every screened card gets two buttons:
  ✓ Open   — "I would open this profile"
  ✗ Skip   — "I would not"
plus an optional one-line reason, and "CV downloaded" is marked automatically
when that person's CV is later pushed from the Candidate tab.

What it feeds, straight away:
  - the Screener prompt: the latest decisions (disagreements first) are shown
    to the AI as "the recruiter's own past decisions" (naukri_screen.py);
  - the Job Breakdown: people marked Open become examples (job_profile.py);
  - "Suggest rules": the AI reads the disagreements and proposes new rules,
    which the recruiter accepts one by one;
  - the agreement meter: how often the AI and the recruiter agree;
  - Export: JSONL training file (job + card + label) for training a model.

Design rules
  - Additive: one new table, nothing existing changes.
  - Card text is stored REDACTED (no phone numbers, no e-mail addresses) and
    capped, and only when the recruiter presses a button or pushes a CV.
  - Same access rules as the Screener (logged-in session, own company's
    mandate, recruiter scope via modules/access.py).

Routes (JSON, under /api/extension/screen/feedback)
  POST   ''        {mandate_id, items:[{key,name,text,ai_verdict,ai_score,ai_reason,
                     my_verdict?, downloaded?, reason?}]}           -> saved + stats
  GET    /stats?mandate_id=                                         -> counts, agreement
  POST   /suggest  {mandate_id}                                     -> proposed rules (not saved)
  GET    /export?mandate_id=&format=jsonl                           -> training file (company admin)
"""

import re
import json
import hashlib
import datetime

from flask import Blueprint, request, jsonify, Response

from modules.shared import get_db, ts, effective_company_id, real_user_id, is_company_admin, _core
from modules import register_migration
from modules import naukri_screen as ns

bp = Blueprint('screen_feedback', __name__, url_prefix='/api/extension/screen/feedback')

MAX_ITEMS = 60
MAX_TEXT = 1800
MAX_REASON = 300
TARGET_DECISIONS = 3000
VERDICTS = ('open', 'skip', 'maybe', '')


@register_migration
def _migrate_screen_feedback(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS screen_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL DEFAULT 0,
        user_id INTEGER DEFAULT 0,
        mandate_id INTEGER NOT NULL,
        card_hash TEXT NOT NULL,
        card_key TEXT DEFAULT '',
        name TEXT DEFAULT '',
        card_text TEXT DEFAULT '',
        ai_verdict TEXT DEFAULT '',
        ai_score INTEGER,
        ai_reason TEXT DEFAULT '',
        my_verdict TEXT DEFAULT '',
        downloaded INTEGER DEFAULT 0,
        reason TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        UNIQUE(company_id, mandate_id, card_hash)
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_screen_feedback_mandate ON screen_feedback(company_id, mandate_id)')
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  PURE HELPERS (unit tested)
# ══════════════════════════════════════════════════════════════════════════
def card_hash(name, text):
    """Same card on a later paste -> same hash, so a decision is updated, not duplicated."""
    t = re.sub(r'\s+', ' ', str(text or '')).strip().lower()[:600]
    n = re.sub(r'\s+', ' ', str(name or '')).strip().lower()
    return hashlib.sha1((n + '|' + t).encode('utf-8', 'ignore')).hexdigest()


def agreement(rows):
    """(agree, compared). The AI's 'maybe' makes no claim, so it is left out;
    AI open vs recruiter open, and AI skip vs recruiter skip, count as agreement."""
    agree = compared = 0
    for ai, me in rows:
        if me not in ('open', 'skip') or ai not in ('open', 'skip'):
            continue
        compared += 1
        if ai == me:
            agree += 1
    return agree, compared


def pct(a, b):
    return round(100.0 * a / b) if b else None


# ══════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════
def _stats(conn, mid):
    co = effective_company_id()
    today = ts()[:10]
    week_ago = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
    q = "SELECT ai_verdict, my_verdict, mandate_id, substr(updated_at,1,10) AS d FROM screen_feedback WHERE company_id=? AND my_verdict IN ('open','skip')"
    rows = conn.execute(q, (co,)).fetchall()
    all_pairs = [(r['ai_verdict'], r['my_verdict']) for r in rows]
    job_pairs = [(r['ai_verdict'], r['my_verdict']) for r in rows if mid and r['mandate_id'] == mid]
    week_pairs = [(r['ai_verdict'], r['my_verdict']) for r in rows if (r['d'] or '') >= week_ago]
    a_all, c_all = agreement(all_pairs)
    a_job, c_job = agreement(job_pairs)
    a_wk, c_wk = agreement(week_pairs)
    dl = conn.execute('SELECT COUNT(*) FROM screen_feedback WHERE company_id=? AND downloaded=1', (co,)).fetchone()[0]
    return {'total': len(rows), 'this_job': len(job_pairs), 'today': sum(1 for r in rows if (r['d'] or '') == today),
            'downloaded': dl, 'target': TARGET_DECISIONS,
            'agree_pct': pct(a_all, c_all), 'agree_job_pct': pct(a_job, c_job), 'agree_week_pct': pct(a_wk, c_wk),
            'compared': c_all, 'compared_job': c_job,
            'disagree_job': c_job - a_job}


@bp.route('', methods=['POST', 'OPTIONS'], strict_slashes=False)
@ns._ext_route
def save_feedback():
    d = request.get_json(silent=True) or {}
    items = d.get('items') or []
    if not isinstance(items, list) or not items:
        return ns._err('Nothing to save.', 400)
    if len(items) > MAX_ITEMS:
        return ns._err('Send at most %d decisions at a time.' % MAX_ITEMS, 400)
    conn = get_db()
    try:
        m, err = ns._load_mandate(conn, d.get('mandate_id'))
        if err:
            return err
        co, uid, now = effective_company_id(), int(real_user_id() or 0), ts()
        saved = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            text = ns.redact(it.get('text'))[:MAX_TEXT]
            name = ns.redact(it.get('name'))[:80]
            if len(text) < 20:
                continue
            h = card_hash(name, text)
            ai_v = str(it.get('ai_verdict') or '')[:10]
            try:
                ai_s = int(it.get('ai_score')) if it.get('ai_score') is not None else None
            except (TypeError, ValueError):
                ai_s = None
            ai_r = re.sub(r'\s+', ' ', str(it.get('ai_reason') or ''))[:240]
            conn.execute('''INSERT INTO screen_feedback (company_id, user_id, mandate_id, card_hash, card_key, name, card_text,
                              ai_verdict, ai_score, ai_reason, created_at, updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(company_id, mandate_id, card_hash) DO UPDATE SET
                              ai_verdict=excluded.ai_verdict, ai_score=excluded.ai_score, ai_reason=excluded.ai_reason,
                              card_key=excluded.card_key, user_id=excluded.user_id''',
                         (co, uid, m['id'], h, str(it.get('key') or '')[:80], name, text, ai_v, ai_s, ai_r, now, now))
            sets, vals = [], []
            if 'my_verdict' in it:
                v = str(it.get('my_verdict') or '').lower()
                if v in VERDICTS:
                    sets.append('my_verdict=?'); vals.append(v)
            if 'downloaded' in it:
                sets.append('downloaded=?'); vals.append(1 if it.get('downloaded') else 0)
                if it.get('downloaded'):
                    # a downloaded CV is an "open" decision even without a click
                    sets.append("my_verdict=CASE WHEN my_verdict='' THEN 'open' ELSE my_verdict END")
            if 'reason' in it:
                sets.append('reason=?'); vals.append(re.sub(r'\s+', ' ', ns.redact(it.get('reason')))[:MAX_REASON])
            if sets:
                sets.append('updated_at=?'); vals.append(now)
                conn.execute('UPDATE screen_feedback SET %s WHERE company_id=? AND mandate_id=? AND card_hash=?' % ', '.join(sets),
                             vals + [co, m['id'], h])
            saved += 1
        conn.commit()
        return jsonify({'ok': True, 'saved': saved, 'stats': _stats(conn, m['id'])})
    finally:
        conn.close()


@bp.route('/stats', methods=['GET', 'OPTIONS'])
@ns._ext_route
def feedback_stats():
    conn = get_db()
    try:
        mid = 0
        if request.args.get('mandate_id') not in (None, '', 'central'):
            m, err = ns._load_mandate(conn, request.args.get('mandate_id'))
            if err:
                return err
            mid = m['id']
        return jsonify({'ok': True, 'stats': _stats(conn, mid)})
    finally:
        conn.close()


SUGGEST_PROMPT = """You help a recruiter teach a screening assistant. Below are Naukri search-result cards for ONE job where the
recruiter's decision DIFFERED from the assistant's, with the recruiter's reason when given, plus the current rules.
Find the patterns behind the recruiter's decisions and propose NEW rules that would have made the assistant agree.
Each rule: short, specific, checkable from a search card (employer, designation, experience, CTC, location, industry),
written in English, one idea per rule. Do not repeat an existing rule. Propose at most 5; propose none if there is no clear pattern.
Reply with ONLY a JSON object: {"rules": [{"rule": "...", "why": "which cards show it, max 20 words"}]}"""


@bp.route('/suggest', methods=['POST', 'OPTIONS'])
@ns._ext_route
def suggest_rules():
    core = _core()
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        m, err = ns._load_mandate(conn, d.get('mandate_id'))
        if err:
            return err
        rows = conn.execute("SELECT card_text, ai_verdict, my_verdict, reason, downloaded FROM screen_feedback "
                            "WHERE company_id=? AND mandate_id=? AND my_verdict IN ('open','skip') AND ai_verdict IN ('open','maybe','skip') "
                            "AND ai_verdict != my_verdict ORDER BY id DESC LIMIT 25",
                            (effective_company_id(), m['id'])).fetchall()
        if len(rows) < 3:
            return jsonify({'ok': True, 'rules': [], 'note': 'Need at least 3 cards where you and the AI disagreed. Keep marking ✓ / ✗.'})
        key = ns._deepseek_key(core)
        if not key:
            return ns._err('DeepSeek is not set up. Add the DeepSeek API key in ATS Settings.', 400)
        if not ns._rate_ok('fb-suggest', int(real_user_id() or 0), 20):
            return ns._err('Too many requests — wait a few minutes.', 429)
        rules, _c, _a, _b = ns._get_state(conn, m['id'])
        cards = []
        for i, r in enumerate(rows, 1):
            card = ' / '.join([l.strip() for l in str(r['card_text'] or '').split('\n') if l.strip()][:6])[:400]
            cards.append('%d. AI said %s, RECRUITER said %s%s%s\n   %s' % (
                i, r['ai_verdict'].upper(), r['my_verdict'].upper(), ' (downloaded CV)' if r['downloaded'] else '',
                (' — reason: ' + r['reason']) if r['reason'] else '', card))
        user = ('THE JOB:\n' + ns.job_brief(core, m)[:3000] + '\n\nCURRENT RULES:\n' + ns._rules_text(rules) +
                '\n\nDISAGREEMENTS:\n' + '\n'.join(cards))
        try:
            j = ns._ask_deepseek(core, key, SUGGEST_PROMPT, user, 900, 'screen-suggest', timeout=60)
        except Exception as exc:
            return ns._ai_error(core, exc)
        have = {x.lower() for x in rules}
        out = []
        for it in (j or {}).get('rules') or []:
            if isinstance(it, str):
                it = {'rule': it}
            if not isinstance(it, dict):
                continue
            r = ns.clean_rules([it.get('rule')])
            if r and r[0].lower() not in have:
                out.append({'rule': r[0], 'why': re.sub(r'\s+', ' ', str(it.get('why') or ''))[:160]})
            if len(out) >= 5:
                break
        return jsonify({'ok': True, 'rules': out, 'based_on': len(rows)})
    finally:
        conn.close()


@bp.route('/export', methods=['GET', 'OPTIONS'])
@ns._ext_route
def export_feedback():
    """Training file: one JSON object per line — job, breakdown, card, AI answer, recruiter label."""
    if not is_company_admin():
        return ns._err('Only an admin can export training data.', 403)
    core = _core()
    conn = get_db()
    try:
        co = effective_company_id()
        args = [co]
        q = "SELECT * FROM screen_feedback WHERE company_id=? AND my_verdict IN ('open','skip')"
        if request.args.get('mandate_id') not in (None, ''):
            try:
                args.append(int(request.args.get('mandate_id')))
            except (TypeError, ValueError):
                return ns._err('Invalid mandate_id', 400)
            q += ' AND mandate_id=?'
        rows = conn.execute(q + ' ORDER BY mandate_id, id', args).fetchall()
        jobs = {}
        lines = []
        for r in rows:
            mid = r['mandate_id']
            if mid not in jobs:
                m = conn.execute('SELECT * FROM mandates WHERE id=? AND owner_id=?', (mid, co)).fetchone()
                if not m:
                    jobs[mid] = None
                else:
                    bd = ''
                    try:
                        from modules.job_profile import ensure, profile_text
                        got = ensure(conn, core, m, create=False)
                        bd = profile_text((got or {}).get('profile') or {})
                    except Exception:
                        pass
                    jobs[mid] = {'role': m['role'], 'client': m['client'], 'brief': ns.job_brief(core, m), 'breakdown': bd}
            j = jobs[mid]
            if not j:
                continue
            lines.append(json.dumps({'mandate_id': mid, 'role': j['role'], 'client': j['client'],
                                     'job': j['brief'], 'breakdown': j['breakdown'], 'card': r['card_text'],
                                     'ai_verdict': r['ai_verdict'], 'ai_score': r['ai_score'], 'ai_reason': r['ai_reason'],
                                     'label': r['my_verdict'], 'downloaded': bool(r['downloaded']), 'reason': r['reason'],
                                     'at': r['updated_at']}, ensure_ascii=False))
        body = '\n'.join(lines) + ('\n' if lines else '')
        fname = 'hirelab-screener-training-%s.jsonl' % ts()[:10]
        return Response(body, mimetype='application/x-ndjson',
                        headers={'Content-Disposition': 'attachment; filename="%s"' % fname})
    finally:
        conn.close()
