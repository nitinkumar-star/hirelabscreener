"""
RecruitOS — Call intelligence store  (Oct 2026)

Every analysed call recording is saved here (it used to live only in a note),
so the Call Analysis tab can always show the report and play the original
recording. The analysis itself (Groq transcript + DeepSeek combined report:
CV + Pitch evaluation + call, compared against the JD) runs in server.py
analyse_call(); this module owns the table, the read endpoint and the helpers
that gather the extra inputs.

ROLLBACK: remove 'call_intel' from modules/__init__.py. Analysis still runs;
the tab just will not reload a saved report after a page refresh.
"""

import json

from flask import Blueprint, jsonify

from modules.shared import get_db, ts, effective_company_id, login_required
from modules import register_migration

bp = Blueprint('call_intel', __name__)

TABLE = 'candidate_call_analysis'


@register_migration
def migrate(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER DEFAULT 0,
        candidate_id INTEGER DEFAULT 0,
        mandate_id INTEGER DEFAULT 0,
        recording_file TEXT DEFAULT '',
        recording_name TEXT DEFAULT '',
        transcript TEXT DEFAULT '',
        analysis TEXT DEFAULT '',
        sources TEXT DEFAULT '',
        updated_fields TEXT DEFAULT '',
        created_by INTEGER DEFAULT 0,
        created_by_name TEXT DEFAULT '',
        created_at TEXT DEFAULT ''
    )''')
    conn.execute(f'CREATE INDEX IF NOT EXISTS idx_cca_cand ON {TABLE}(candidate_id, owner_id)')
    conn.commit()


# ── helpers used by server.analyse_call ───────────────────────────────────
def pitch_evaluation(conn, cid, mid):
    """Latest Pitch-tab AI evaluation for this candidate on this job, or None."""
    try:
        r = conn.execute('SELECT evaluation, created_at FROM hl_candidate_call_pitch '
                         'WHERE candidate_id=? AND mandate_id=? ORDER BY id DESC LIMIT 1',
                         (cid, mid or 0)).fetchone()
    except Exception:
        return None
    if not r or not r['evaluation']:
        return None
    try:
        ev = json.loads(r['evaluation'])
        return ev if isinstance(ev, dict) else None
    except Exception:
        return None


def profile_block(conn, cand):
    """Structured profile facts (what the ATS knows besides the CV file)."""
    c = dict(cand)
    lines = []
    for label, key in (('Current designation', 'designation'), ('Current company', 'company'),
                       ('Total experience (yrs)', 'experience'), ('Current CTC (LPA)', 'ctc_current'),
                       ('Expected CTC (LPA)', 'ctc_expected'), ('Notice period (days)', 'notice_period'),
                       ('Location', 'location'), ('Preferred location', 'preferred_location'),
                       ('Qualification', 'qualification'), ('Career summary', 'career_summary')):
        v = c.get(key)
        if v not in (None, '', 0, 0.0):
            lines.append(f'{label}: {v}')
    try:
        ks = json.loads(c.get('key_skills') or '[]')
        if isinstance(ks, list) and ks:
            lines.append('Key skills: ' + ', '.join(str(x) for x in ks[:30]))
    except Exception:
        pass
    try:
        wh = conn.execute('SELECT company, designation, start_date, end_date, description FROM work_history '
                          'WHERE candidate_id=? ORDER BY sort_order LIMIT 6', (c['id'],)).fetchall()
        for w in wh:
            span = ' – '.join(x for x in (w['start_date'] or '', w['end_date'] or '') if x)
            lines.append(f"Worked: {w['designation'] or ''} at {w['company'] or ''}"
                         + (f' ({span})' if span else '')
                         + (': ' + (w['description'] or '')[:300].replace('\n', ' ') if w['description'] else ''))
    except Exception:
        pass
    return '\n'.join(lines)


def save(conn, owner_id, cid, mid, rec_file, rec_name, transcript, analysis, sources, updated, user_id, user_name):
    conn.execute(f'INSERT INTO {TABLE} (owner_id,candidate_id,mandate_id,recording_file,recording_name,transcript,'
                 'analysis,sources,updated_fields,created_by,created_by_name,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                 (owner_id, cid, mid or 0, rec_file, rec_name or '', transcript,
                  json.dumps(analysis, ensure_ascii=False), json.dumps(sources),
                  json.dumps(updated or {}), user_id or 0, user_name or '', ts()))


# ── read endpoint ─────────────────────────────────────────────────────────
@bp.route('/api/candidates/<int:cid>/call-analysis', methods=['GET'])
@login_required
def latest_call_analysis(cid):
    conn = get_db()
    own = conn.execute('SELECT owner_id FROM candidates WHERE id=?', (cid,)).fetchone()
    if not own or own['owner_id'] != effective_company_id():
        conn.close(); return jsonify({'error': 'Not found'}), 404
    r = conn.execute(f'SELECT * FROM {TABLE} WHERE candidate_id=? AND owner_id=? ORDER BY id DESC LIMIT 1',
                     (cid, effective_company_id())).fetchone()
    conn.close()
    if not r:
        return jsonify({'ok': True, 'call': None})
    d = dict(r)
    for k in ('analysis', 'sources', 'updated_fields'):
        try:
            d[k] = json.loads(d.get(k) or '{}')
        except Exception:
            d[k] = {}
    return jsonify({'ok': True, 'call': d})
