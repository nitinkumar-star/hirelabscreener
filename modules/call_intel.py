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


# ══════════════════════════════════════════════════════════════════════════
#  SHARE TO HIRELAB — match a shared phone recording to a candidate
# ══════════════════════════════════════════════════════════════════════════
import re as _re
from flask import request as _request

_STOP = {'call', 'calls', 'recording', 'recordings', 'record', 'recorded', 'audio', 'voice', 'phone',
         'sim', 'incoming', 'outgoing', 'in', 'out', 'mp3', 'm4a', 'amr', 'wav', 'aac', 'ogg', 'opus',
         'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec', 'pm', 'am',
         'new', 'file', 'track', 'unknown', 'number', 'private', 'with', 'from', 'and'}


def _digits(s):
    return _re.sub(r'\D', '', s or '')


def _scope(conn):
    """(sql, params) limiting candidates to what this user may work on."""
    try:
        from modules.access import candidate_id_scope_sql, scoped_user
        if scoped_user():
            sql, params = candidate_id_scope_sql('c.id', include_pool=False)
            return sql, params
    except Exception:
        pass
    return '', []


def _row(r, how):
    return {'id': r['id'], 'name': r['name'] or '', 'phone': r['phone'] or '', 'company': r['company'] or '',
            'designation': r['designation'] or '', 'stage': r['stage'] or '', 'mandate_id': r['mandate_id'],
            'role': r['role'] or '', 'client': r['client'] or '', 'match': how}


@bp.route('/api/share-call/suggest', methods=['GET'])
@login_required
def share_call_suggest():
    """Best candidate matches for a shared recording, from its file name
    (phone number and/or contact name, as Samsung / Xiaomi / Realme / OnePlus
    dialers write them), plus a free-text search and recent candidates."""
    fname = (_request.args.get('name') or '')[:200]
    q = (_request.args.get('q') or '').strip()[:80]
    oid = effective_company_id()
    conn = get_db()
    ssql, sparams = _scope(conn)
    base = ('SELECT c.id, c.name, c.phone, c.company, c.designation, c.stage, c.mandate_id, '
            'm.role, m.client FROM candidates c LEFT JOIN mandates m ON m.id = c.mandate_id '
            "WHERE c.owner_id=? AND COALESCE(m.status,'') != 'central' ")
    out, seen = [], set()

    def add(rows, how):
        for r in rows:
            if r['id'] not in seen:
                seen.add(r['id']); out.append(_row(r, how))

    stem = _re.sub(r'\.[A-Za-z0-9]{2,5}$', '', fname)
    # 1) phone number in the file name (last 10 digits)
    for num in _re.findall(r'\+?\d[\d\s-]{8,16}\d', stem):
        d = _digits(num)
        if len(d) < 10:
            continue
        last10 = d[-10:]
        rows = conn.execute(base + ssql + ' AND c.phone LIKE ? ORDER BY c.updated_at DESC LIMIT 20',
                            [oid] + sparams + ['%' + last10[-5:] + '%']).fetchall()
        add([r for r in rows if _digits(r['phone'])[-10:] == last10], 'phone')
    # 2) contact name in the file name
    words = [w for w in _re.findall(r'[A-Za-z]{3,}', stem) if w.lower() not in _STOP]
    if words:
        like = ' OR '.join(['LOWER(c.name) LIKE ?'] * len(words))
        rows = conn.execute(base + ssql + ' AND (' + like + ') ORDER BY c.updated_at DESC LIMIT 40',
                            [oid] + sparams + ['%' + w.lower() + '%' for w in words]).fetchall()
        lw = [w.lower() for w in words]
        rows = sorted(rows, key=lambda r: -sum(1 for w in lw if w in (r['name'] or '').lower()))
        full = [r for r in rows if all(w in (r['name'] or '').lower() for w in lw)]
        add(full, 'name')
        add(rows[:8], 'name-part')
    # 3) typed search
    if q:
        qd = _digits(q)
        if len(qd) >= 5:
            rows = conn.execute(base + ssql + ' AND c.phone LIKE ? ORDER BY c.updated_at DESC LIMIT 15',
                                [oid] + sparams + ['%' + qd[-8:] + '%']).fetchall()
        else:
            rows = conn.execute(base + ssql + ' AND (LOWER(c.name) LIKE ? OR LOWER(c.company) LIKE ?) '
                                'ORDER BY c.updated_at DESC LIMIT 15',
                                [oid] + sparams + ['%' + q.lower() + '%'] * 2).fetchall()
        add(rows, 'search')
    # 4) recently worked-on candidates
    if not q:
        rows = conn.execute(base + ssql + ' ORDER BY c.updated_at DESC LIMIT 8', [oid] + sparams).fetchall()
        add(rows, 'recent')
    conn.close()
    return jsonify({'ok': True, 'candidates': out[:25]})
