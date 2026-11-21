"""
RecruitOS — HireLab Academy  (Nov 2026)

Every feature explained step by step, and a place to ask questions.

  GET  /academy                                  the page (academy.html)
  GET  /api/academy/articles                     sections + the guides this person may see
                                                 (+ the agency's own note on each guide)
  PUT  /api/academy/notes/<key>      {note}      the agency's note on a guide (agency admin; '' removes it)
  POST /api/academy/ask              {question}  an answer from the guides only, with the guides used
  POST /api/academy/ask/<id>/feedback {helpful: 1|-1}
  GET  /api/academy/unanswered                   questions the Academy could not answer or that got 👎
                                                 (agency admin) — tells you which guides to add
  POST /api/academy/unanswered/<id>/done         mark one as handled (agency admin)

The guides live in academy_content.py (shipped with the app; owner's decision 1-A).
Who sees what (decision 3-A): agency admins every guide, recruiters the guides
marked for recruiters, freelance sourcers only the guides for what they can use.

Questions (decision 2-A) are answered by DeepSeek from the guides ONLY: the most
relevant guides (simple word match) plus a one-line index of all of them go in
the prompt, the answer must cite guide keys from that list, and anything the
guides do not cover is answered "not covered" and listed for the admins. The
Academy never reads candidate, client or job data. 30 questions per person per
day; every call goes through server.call_deepseek (AI cost meter + monthly cap).

Tables (new): academy_questions, academy_notes.
ROLLBACK: remove 'academy' from modules/__init__.py. The menu item then opens
a page that is not there; remove it from index.html too for a clean rollback.
"""

import os
import re
import sys
import json

from flask import Blueprint, request, jsonify, redirect, Response

from modules.shared import get_db, ts, effective_company_id, real_user_id, is_company_admin, login_required, _core
from modules import register_migration

bp = Blueprint('academy', __name__)
pages = Blueprint('academy_pages', __name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAILY_LIMIT = 30
TOP_N = 4


def content():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import academy_content
    return academy_content


@register_migration
def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS academy_questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        user_id INTEGER DEFAULT 0,
        question TEXT DEFAULT '',
        answer TEXT DEFAULT '',
        sources TEXT DEFAULT '[]',
        answered INTEGER DEFAULT 1,
        helpful INTEGER,
        reviewed INTEGER DEFAULT 0,
        created_at TEXT DEFAULT ''
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_acq_owner ON academy_questions(owner_id, created_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_acq_user ON academy_questions(user_id, created_at)')
    conn.execute('''CREATE TABLE IF NOT EXISTS academy_notes (
        owner_id INTEGER NOT NULL,
        article_key TEXT NOT NULL,
        note TEXT DEFAULT '',
        updated_by INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT '',
        PRIMARY KEY (owner_id, article_key)
    )''')
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  Who sees what
# ══════════════════════════════════════════════════════════════════════════
def role_letter():
    """'A' agency admin, 'R' recruiter, 'F' freelance sourcer."""
    try:
        u = _core().current_user() or {}
    except Exception:
        u = {}
    if (u.get('role') or '') == 'freelancer_sourcer':
        return 'F'
    try:
        if is_company_admin():
            return 'A'
    except Exception:
        pass
    return 'R'


def visible(role):
    c = content()
    return [a for a in c.ARTICLES if role in a['roles']]


def _uid():
    try:
        return int(real_user_id() or 0)
    except Exception:
        return 0


def _clean(v, n):
    return ' '.join(str(v or '').split())[:n]


# ══════════════════════════════════════════════════════════════════════════
#  Finding the right guides for a question
# ══════════════════════════════════════════════════════════════════════════
_STOP = set('''a an the is are was were be to of in on for with and or but how what where when why who which do does did
can could should would i me my we our you your it this that these those there here from by at as about into kya kaise
kahan kab kyun kyon kaun ka ki ke ko hai hain tha thi the se me mein par pe aur ya bhi nahi na mujhe hum aap apna apne
karna karne karu karun karein kare kar ho hota hoti hote jaye jata jati use using please plz batao bataye bata sakte
sakta sakti chahiye wala wali wale ek koi kuch sab'''.split())


def _words(text):
    return [w for w in re.findall(r'[a-z0-9&]+', str(text or '').lower()) if w not in _STOP and len(w) > 1]


def _article_text(a):
    return ' '.join([a['title'], a['summary'], a.get('when', ''), a.get('where', ''), ' '.join(a.get('steps', [])),
                     ' '.join(a.get('tips', []))])


def retrieve(question, articles, n=TOP_N):
    """The n guides that share the most words with the question (title and
    keywords count more). Returns [] when nothing matches at all."""
    q = _words(question)
    if not q:
        return []
    qs = set(q)
    scored = []
    for a in articles:
        title = set(_words(a['title']))
        keys = set(_words(' '.join(a.get('keywords', [])))) | set(_words(a['key'].replace('-', ' ')))
        body = _words(_article_text(a))
        bset = set(body)
        s = 4 * len(qs & title) + 3 * len(qs & keys) + len(qs & bset)
        # partial words ("invoic" in "invoicing", "tag" in "tagging")
        s += 0.5 * sum(1 for w in qs if len(w) >= 4 and any(w in k or k in w for k in (title | keys) if len(k) >= 4))
        if s > 0:
            scored.append((s, a))
    scored.sort(key=lambda x: -x[0])
    return [a for _s, a in scored[:n]]


def _render(a, note=''):
    out = ['GUIDE %s — %s' % (a['key'], a['title']), 'Where: ' + a.get('where', ''), a['summary']]
    if a.get('when'):
        out.append('When to use: ' + a['when'])
    if a.get('steps'):
        out.append('Steps:\n' + '\n'.join('%d. %s' % (i + 1, s) for i, s in enumerate(a['steps'])))
    if a.get('tips'):
        out.append('Tips:\n' + '\n'.join('- ' + t for t in a['tips']))
    if note:
        out.append("Our agency's note: " + note)
    return '\n'.join(out)


_SYSTEM = (
    'You are the HireLab Academy assistant. HireLab is a recruitment ATS. You answer questions about HOW TO USE '
    'HireLab, using ONLY the guides given below. Rules:\n'
    '1. Use only facts from the guides. Never invent menu names, buttons, numbers or features.\n'
    '2. If the guides do not answer the question, set "answered" to false and say briefly, kindly, that the Academy '
    'does not cover it yet (an admin will be told), and point to the closest guide if there is one.\n'
    '3. Reply in the SAME language and script the user wrote in (English, Hindi, or Hinglish in Latin script).\n'
    '4. Be short and practical: a sentence or two, then numbered steps if needed. Use the exact menu path from the guide.\n'
    '5. You do not have access to any candidate, client, job or company data, and must not pretend to.\n'
    'Return ONLY JSON: {"answer": "...", "sources": ["guide-key", ...], "answered": true|false}. "sources" may only '
    'contain keys of guides you actually used.'
)


def _notes(conn, oid):
    return {r['article_key']: r['note'] for r in conn.execute(
        "SELECT article_key, note FROM academy_notes WHERE owner_id=? AND COALESCE(note,'')!=''", (oid,))}


# ══════════════════════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════════════════════
@pages.route('/academy')
def academy_page():
    if not _core().current_user():
        return redirect('/')
    with open(os.path.join(ROOT, 'academy.html'), encoding='utf-8') as fh:
        return Response(fh.read(), mimetype='text/html')


@bp.route('/api/academy/articles', methods=['GET'])
@login_required
def articles():
    role = role_letter()
    c = content()
    conn = get_db()
    try:
        notes = _notes(conn, effective_company_id())
    finally:
        conn.close()
    arts = []
    for a in visible(role):
        d = {k: a.get(k) for k in ('key', 'title', 'section', 'where', 'summary', 'when', 'steps', 'tips', 'related',
                                   'link', 'keywords')}
        d['who'] = {'A': 'Admins', 'R': 'Recruiters', 'F': 'Freelancers'}
        d['who'] = [d['who'][x] for x in a['roles']]
        d['note'] = notes.get(a['key'], '')
        arts.append(d)
    keys = {a['key'] for a in arts}
    for d in arts:
        d['related'] = [k for k in (d['related'] or []) if k in keys]
    sections = [{'key': k, 'title': t} for k, t in c.SECTIONS if any(a['section'] == k for a in arts)]
    return jsonify({'ok': True, 'role': role, 'is_admin': role == 'A', 'sections': sections, 'articles': arts,
                    'daily_limit': DAILY_LIMIT})


@bp.route('/api/academy/notes/<key>', methods=['PUT'])
@login_required
def save_note(key):
    if role_letter() != 'A':
        return jsonify({'error': 'Only an admin can add an agency note.'}), 403
    if key not in {a['key'] for a in content().ARTICLES}:
        return jsonify({'error': 'No such guide.'}), 404
    note = str((request.get_json(silent=True) or {}).get('note') or '').strip()[:2000]
    conn = get_db()
    try:
        conn.execute('INSERT OR REPLACE INTO academy_notes (owner_id, article_key, note, updated_by, updated_at) '
                     'VALUES (?,?,?,?,?)', (effective_company_id(), key, note, _uid(), ts()))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True, 'note': note})


@bp.route('/api/academy/ask', methods=['POST'])
@login_required
def ask():
    q = _clean((request.get_json(silent=True) or {}).get('question'), 500)
    if len(q) < 3:
        return jsonify({'error': 'Type your question first.'}), 400
    oid, uid, role = effective_company_id(), _uid(), role_letter()
    core = _core()
    arts = visible(role)
    by_key = {a['key']: a for a in arts}
    conn = get_db()
    try:
        today = ts()[:10]
        used = conn.execute('SELECT COUNT(*) FROM academy_questions WHERE owner_id=? AND user_id=? AND created_at>=?',
                            (oid, uid, today)).fetchone()[0]
        if used >= DAILY_LIMIT:
            return jsonify({'error': 'You have asked %d questions today, the daily limit. Browse the guides, or ask again '
                                     'tomorrow.' % DAILY_LIMIT}), 429
        top = retrieve(q, arts)
        notes = _notes(conn, oid)
        fallback = [{'key': a['key'], 'title': a['title']} for a in top]
        key = core.get_setting('deepseek_api_key')
        if not key:
            return jsonify({'ok': True, 'ai': False, 'answer': '', 'sources': fallback, 'answered': bool(top),
                            'message': 'AI answers are not set up yet (an admin adds the DeepSeek key in Settings → Admin → '
                                       'AI API Keys). These guides look related:'})
        index = '\n'.join('- %s: %s — %s' % (a['key'], a['title'], a['summary'][:140]) for a in arts)
        guides = '\n\n'.join(_render(a, notes.get(a['key'], '')) for a in top) or '(no guide matched the question words)'
        user = ('ALL GUIDES (index):\n%s\n\nMOST RELEVANT GUIDES (full text):\n%s\n\nQUESTION: %s' % (index, guides, q))
        try:
            resp = core.call_deepseek(key, {
                'model': 'deepseek-chat', 'temperature': 0.2, 'max_tokens': 700,
                'response_format': {'type': 'json_object'},
                'messages': [{'role': 'system', 'content': _SYSTEM}, {'role': 'user', 'content': user}]},
                timeout=45, endpoint='academy')
        except Exception as e:
            if type(e).__name__ == 'TokenCapError':
                return jsonify({'ok': True, 'ai': False, 'answer': '', 'sources': fallback, 'answered': bool(top),
                                'message': 'The agency\'s monthly AI limit is reached, so no AI answer now. These guides look related:'})
            return jsonify({'ok': True, 'ai': False, 'answer': '', 'sources': fallback, 'answered': bool(top),
                            'message': 'The AI did not answer just now. These guides look related:'})
        data = {}
        if resp.status_code == 200:
            try:
                data = core.parse_json(resp.json()['choices'][0]['message']['content'] or '') or {}
            except Exception:
                data = {}
        answer = _clean_block(data.get('answer'))
        if not answer:
            return jsonify({'ok': True, 'ai': False, 'answer': '', 'sources': fallback, 'answered': bool(top),
                            'message': 'The AI did not answer just now. These guides look related:'})
        srcs = [k for k in (data.get('sources') or []) if isinstance(k, str) and k in by_key][:4]
        answered = bool(data.get('answered', True)) and bool(srcs)
        if not srcs:
            srcs = [a['key'] for a in top[:2]]
        sources = [{'key': k, 'title': by_key[k]['title']} for k in srcs]
        cur = conn.execute('INSERT INTO academy_questions (owner_id, user_id, question, answer, sources, answered, created_at) '
                           'VALUES (?,?,?,?,?,?,?)', (oid, uid, q, answer, json.dumps(srcs), 1 if answered else 0, ts()))
        conn.commit()
        qid = cur.lastrowid
    finally:
        conn.close()
    return jsonify({'ok': True, 'ai': True, 'id': qid, 'answer': answer, 'sources': sources, 'answered': answered,
                    'left_today': max(0, DAILY_LIMIT - used - 1)})


def _clean_block(v, n=2500):
    lines = [l.rstrip() for l in str(v or '').replace('\r', '').split('\n')]
    return '\n'.join(lines).strip()[:n]


@bp.route('/api/academy/ask/<int:qid>/feedback', methods=['POST'])
@login_required
def feedback(qid):
    h = (request.get_json(silent=True) or {}).get('helpful')
    if h not in (1, -1):
        return jsonify({'error': 'helpful must be 1 or -1'}), 400
    conn = get_db()
    try:
        cur = conn.execute('UPDATE academy_questions SET helpful=? WHERE id=? AND owner_id=? AND user_id=?',
                           (h, qid, effective_company_id(), _uid()))
        conn.commit()
        if not cur.rowcount:
            return jsonify({'error': 'Not found'}), 404
    finally:
        conn.close()
    return jsonify({'ok': True})


@bp.route('/api/academy/unanswered', methods=['GET'])
@login_required
def unanswered():
    if role_letter() != 'A':
        return jsonify({'error': 'Only an admin can see this.'}), 403
    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT q.id, q.question, q.answer, q.answered, q.helpful, q.created_at, u.display_name, u.username '
            'FROM academy_questions q LEFT JOIN users u ON u.id=q.user_id '
            'WHERE q.owner_id=? AND COALESCE(q.reviewed,0)=0 AND (q.answered=0 OR q.helpful=-1) '
            'ORDER BY q.id DESC LIMIT 200', (effective_company_id(),)).fetchall()
    finally:
        conn.close()
    return jsonify({'ok': True, 'questions': [
        {'id': r['id'], 'question': r['question'], 'answer': r['answer'], 'not_covered': not r['answered'],
         'thumbs_down': r['helpful'] == -1, 'date': (r['created_at'] or '')[:10],
         'by': r['display_name'] or r['username'] or ''} for r in rows]})


@bp.route('/api/academy/unanswered/<int:qid>/done', methods=['POST'])
@login_required
def unanswered_done(qid):
    if role_letter() != 'A':
        return jsonify({'error': 'Only an admin can do this.'}), 403
    conn = get_db()
    try:
        cur = conn.execute('UPDATE academy_questions SET reviewed=1 WHERE id=? AND owner_id=?', (qid, effective_company_id()))
        conn.commit()
        if not cur.rowcount:
            return jsonify({'error': 'Not found'}), 404
    finally:
        conn.close()
    return jsonify({'ok': True})
