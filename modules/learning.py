"""
Learning — the Electrical Industry Explorer inside HireLab (/learning).

A field guide for recruiters: industry map, competitor heatmap, talent map,
roles and pay, glossary, call kit and a learning log (Schneider, Siemens, L&K,
Legrand and peers). The page is learning.html, kept as one self-contained file.

What the ATS adds:
  - It is a sidebar tab, behind sign-in, with the app menu (modules/app_nav.py).
  - The page's edits (learning log entries, heatmap ratings, pay table) are kept
    PER AGENCY in the database instead of in the file: every recruiter of the
    agency sees the same notes, and other agencies never see them.

  GET  /learning                 the page, with this agency's saved edits in it
  GET  /api/learning/state       this agency's saved edits (JSON)
  PUT  /api/learning/state       save them (the page calls this on every edit)

Additive only: new table learning_state (one row per agency).
"""

import json
import os
import re

from flask import Blueprint, request, jsonify, redirect, Response

from modules import register_migration
from modules.shared import get_db, effective_company_id, login_required, ts, _core

bp = Blueprint('learning', __name__, url_prefix='/api/learning')
pages = Blueprint('learning_pages', __name__)

_PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'learning.html')
_STATE_RX = re.compile(r'(<script id="state" type="application/json">)(.*?)(</script>)', re.S)
_APP_JS = '<script id="app-js">'
MAX_BYTES = 2_000_000

# The page saves through window.claude.use("artifact").publish(...). Inside the
# ATS that call is answered here: it sends the page's STATE to /api/learning/state.
_SHIM = '''<script>
(function(){
  var api = {
    publish: function(doc){
      /* the page hands over its whole HTML; its edits are the JSON in <script id="state"> */
      var m = /<script id="state" type="application\\/json">([\\s\\S]*?)<\\/script>/.exec(String(doc || ''));
      if (!m) return Promise.reject(new Error('save_failed'));
      return fetch('/api/learning/state', {method: 'PUT', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json'}, body: m[1]})
        .then(function(r){ if (r.status === 401 || r.status === 403) { var e = new Error('not_writer'); e.code = 'not_writer'; throw e; }
                           if (!r.ok) throw new Error('save_failed'); return r.json(); });
    }
  };
  window.claude = {use: function(name){ return Promise.resolve(name === 'artifact' ? api : null); }};
})();
</script>
'''


@register_migration
def _migrate_learning(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS learning_state (
        company_id INTEGER PRIMARY KEY,
        state TEXT NOT NULL DEFAULT '{}',
        updated_by INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT ''
    )''')
    conn.commit()


def _valid(st):
    return (isinstance(st, dict) and isinstance(st.get('log', []), list)
            and isinstance(st.get('matrix', {}), dict)
            and (st.get('pay') is None or isinstance(st.get('pay'), list)))


def _load(cid):
    conn = get_db()
    try:
        row = conn.execute('SELECT state FROM learning_state WHERE company_id=?', [cid]).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        st = json.loads(row[0] or '{}')
        return st if _valid(st) else None
    except Exception:
        return None


def render_page(state):
    with open(_PAGE, encoding='utf-8') as f:
        html = f.read()
    if state is not None:
        js = json.dumps(state, ensure_ascii=False).replace('<', '\\u003c')
        html = _STATE_RX.sub(lambda m: m.group(1) + js + m.group(3), html, count=1)
    i = html.find(_APP_JS)
    return html[:i] + _SHIM + html[i:] if i >= 0 else html


@pages.route('/learning')
def learning_page():
    if not _core().current_user():
        return redirect('/')
    resp = Response(render_page(_load(effective_company_id())), mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@bp.route('/state', methods=['GET'])
@login_required
def get_state():
    return jsonify({'state': _load(effective_company_id())})


@bp.route('/state', methods=['PUT', 'POST'])
@login_required
def put_state():
    raw = request.get_data() or b''
    if len(raw) > MAX_BYTES:
        return jsonify({'error': 'Too large'}), 413
    try:
        st = json.loads(raw.decode('utf-8') or '{}')
    except Exception:
        return jsonify({'error': 'Bad JSON'}), 400
    if not _valid(st):
        return jsonify({'error': 'Bad state'}), 400
    cid = effective_company_id()
    try:
        uid = (_core().current_user() or {}).get('id') or 0
    except Exception:
        uid = 0
    body = json.dumps(st, ensure_ascii=False)
    conn = get_db()
    try:
        cur = conn.execute('UPDATE learning_state SET state=?, updated_by=?, updated_at=? WHERE company_id=?',
                           [body, uid, ts(), cid])
        if not getattr(cur, 'rowcount', 0):
            conn.execute('INSERT INTO learning_state (company_id, state, updated_by, updated_at) VALUES (?,?,?,?)',
                         [cid, body, uid, ts()])
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True})
