"""
RecruitOS — Final Result: resume evidence + call evidence → one decision  (Oct 2026)

Before the call the Pitch tab judges the candidate on the RESUME only
(must-have check: MET / NOT_MET / UNCLEAR). After the call the Call Analysis
tab knows what the candidate SAID. This module clubs the two:

  for every must-have of the job
      resume evidence  (from the Pitch evaluation)
    + call evidence    (from the latest analysed call)
    = final status     MET / NOT_MET / UNCLEAR, with a one-line reason

and runs the same fixed rule as the Pitch tab (modules.pitch.decide) over the
final statuses, so the decision is explainable and stable. A candidate who
said they are not interested can never come out as "Shortlisted".

Inputs are what the two tabs already saved — no new recording, no CV re-read:
  * hl_candidate_call_pitch     (latest evaluation for this candidate + job)
  * candidate_call_analysis     (latest analysed call)
The result is cached per (pitch row, call row); when either changes it is
marked stale and the page rebuilds it.

Additive only: new table hl_final_result.
ROLLBACK: remove 'final_result' from modules/__init__.py — the Pitch tab then
simply shows no Final Result card.
"""

import json

from flask import Blueprint, request, jsonify

from modules.shared import get_db, ts, current_user, effective_company_id, real_user_id, login_required, _core
from modules import register_migration

bp = Blueprint('final_result', __name__, url_prefix='/api')

TABLE = 'hl_final_result'
STATUSES = ('MET', 'NOT_MET', 'UNCLEAR')


@register_migration
def migrate(conn):
    _ensure(conn)


def _ensure(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER DEFAULT 0,
        candidate_id INTEGER DEFAULT 0,
        mandate_id INTEGER DEFAULT 0,
        pitch_id INTEGER DEFAULT 0,
        call_id INTEGER DEFAULT 0,
        result TEXT DEFAULT '',
        created_by INTEGER DEFAULT 0,
        created_by_name TEXT DEFAULT '',
        created_at TEXT DEFAULT ''
    )''')
    conn.execute(f'CREATE INDEX IF NOT EXISTS idx_hlfr_cand ON {TABLE}(candidate_id, mandate_id, company_id)')


def _j(raw, default):
    try:
        v = json.loads(raw or '')
        return v if isinstance(v, type(default)) else default
    except Exception:
        return default


# ── inputs ────────────────────────────────────────────────────────────────
def _inputs(conn, cid):
    """(candidate, mandate, pitch_row, call_row) for the caller's tenant."""
    oid = effective_company_id()
    c = conn.execute('SELECT * FROM candidates WHERE id=? AND owner_id=?', (cid, oid)).fetchone()
    if not c:
        return None, None, None, None
    m = conn.execute('SELECT * FROM mandates WHERE id=? AND owner_id=?', (c['mandate_id'], oid)).fetchone()
    p = None
    try:
        p = conn.execute('SELECT id, evaluation, created_at FROM hl_candidate_call_pitch WHERE candidate_id=? '
                         'AND mandate_id=? AND owner_id=? ORDER BY id DESC LIMIT 1',
                         (cid, c['mandate_id'] or 0, oid)).fetchone()
    except Exception:
        p = None
    k = None
    try:
        # the call for THIS job first; an older call made for another job is a fallback
        k = conn.execute('SELECT * FROM candidate_call_analysis WHERE candidate_id=? AND owner_id=? '
                         'ORDER BY (mandate_id=?) DESC, id DESC LIMIT 1',
                         (cid, oid, c['mandate_id'] or 0)).fetchone()
    except Exception:
        k = None
    return c, m, p, k


def _call_view(k):
    a = _j(k['analysis'], {})
    keep = ('interest_level', 'interest_reason', 'recommendation', 'recommendation_reason', 'call_summary',
            'overall_summary', 'requirements', 'consistency', 'pitch_followup', 'key_concerns',
            'candidate_strengths', 'red_flags', 'notice_discussed_days', 'notice_negotiable', 'fit_vs_jd')
    return {x: a[x] for x in keep if x in a}


PROMPT = """You are a senior recruiter in India making the FINAL screening decision on one candidate
for one job. You get two pieces of evidence that were collected separately:

  A) RESUME CHECK — done before the call: for each must-have of the job, what the resume/profile
     shows (status MET / NOT_MET / UNCLEAR + evidence).
  B) CALL ANALYSIS — done after the recruiter's phone call: what the candidate actually said,
     per JD requirement, plus interest level, concerns, red flags and CV-vs-call consistency.

Club them into ONE final verdict per must-have.

Return ONLY a JSON object:
{
  "rows": [
    {"requirement": "<copy the must-have text exactly from the resume check, same order>",
     "resume_evidence": "<short: what the resume shows, or 'not in resume'>",
     "call_evidence": "<short: what the candidate said on the call, or 'not discussed'>",
     "final_status": "MET" | "NOT_MET" | "UNCLEAR",
     "why": "<one line: how the two pieces of evidence were combined>"}
  ],
  "final_score": <integer 0-100: overall fit for THIS job using resume + call together>,
  "summary": "<3-4 plain sentences: the combined picture and the deciding facts>",
  "shortlist_reasons": ["<a job need + the resume/call fact that meets it>", ...],
  "reject_reasons": ["<a job need + what the resume/call shows is missing or contradicted>", ...],
  "open_points": ["<anything still not confirmed by either source that must be checked before sharing>", ...],
  "client_summary": "<3-4 sentences in professional English for the CLIENT: who the candidate is and why they fit, using only confirmed facts>"
}

HOW TO COMBINE (apply strictly):
- Resume MET  + call confirms or does not discuss      -> MET
- Resume MET  + call clearly contradicts it            -> NOT_MET (say so in "why")
- Resume UNCLEAR + call gives clear, specific evidence -> MET
- Resume UNCLEAR + call says no / weak / vague         -> NOT_MET if clearly no, else UNCLEAR
- Resume UNCLEAR + call does not discuss it            -> UNCLEAR
- Resume NOT_MET + call gives clear, specific evidence of real hands-on work -> MET (call wins, explain)
- Resume NOT_MET + call does not change it             -> NOT_MET
- A location must-have: the candidate's stated willingness to relocate/commute on the call decides it.
- Vague claims ("I have some idea", "I have seen it") are NOT evidence of MET.
- One row for EVERY must-have in the resume check, in the same order, with the same wording.
- Use only facts from the inputs. Never mention salary, CTC or budget in client_summary.
- client_summary: no weaknesses, no score, no flattery words (impressive, perfect, great fit), no contact details.
- 0-4 items in each list. Keep every text short and specific."""


def build(cid):
    """Compute and save the final result. Returns (payload, error, http_code)."""
    from modules import pitch as P
    core = _core()
    ds_key = core.get_setting('deepseek_api_key')
    if not ds_key:
        return None, 'DeepSeek API key not set. Add it in Settings.', 400
    conn = get_db()
    try:
        c, m, p, k = _inputs(conn, cid)
        if not c:
            return None, 'Candidate not found', 404
        if not p:
            return None, 'Generate the Pitch evaluation first (resume check).', 400
        if not k:
            return None, 'Upload and analyse a call recording first.', 400
        ev = _j(p['evaluation'], {})
        call = _call_view(k)
        pitch_id, call_id, mid = p['id'], k['id'], c['mandate_id'] or 0
        role = (m['role'] if m else '') or ''
        client = (m['client'] if m else '') or ''
    finally:
        conn.close()

    rows_in = ev.get('must_have_check') or []
    resume = {
        'match_score_resume_only': ev.get('match_score'),
        'must_have_check': rows_in,
        'strengths': ev.get('strengths') or [], 'gaps': ev.get('gaps') or [], 'to_verify': ev.get('to_verify') or [],
        'location_fit': ev.get('location_fit') or '',
    }
    user = (f'JOB: {role}' + (f' at {client}' if client else '') + '\n\n'
            'A) RESUME CHECK\n' + json.dumps(resume, ensure_ascii=False)[:6000] + '\n\n'
            'B) CALL ANALYSIS\n' + json.dumps(call, ensure_ascii=False)[:7000])
    try:
        out, _raw = P._ask_json(ds_key, PROMPT, user, 0.15, 2600, 'final-result')
    except P.PitchError as e:
        return None, str(e), e.code

    res = normalise(out, rows_in, call)
    u = current_user() or {}
    who = (u.get('display_name') or u.get('username') or '').strip()
    at = ts()
    conn = get_db()
    try:
        _ensure(conn)
        conn.execute(f'INSERT INTO {TABLE} (company_id, candidate_id, mandate_id, pitch_id, call_id, result, '
                     'created_by, created_by_name, created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                     (effective_company_id(), cid, mid, pitch_id, call_id, json.dumps(res, ensure_ascii=False),
                      real_user_id() or 0, who, at))
        conn.execute(f'DELETE FROM {TABLE} WHERE candidate_id=? AND mandate_id=? AND company_id=? AND id NOT IN '
                     f'(SELECT MAX(id) FROM {TABLE} WHERE candidate_id=? AND mandate_id=? AND company_id=?)',
                     (cid, mid, effective_company_id(), cid, mid, effective_company_id()))
        try:
            d = res['decision']
            conn.execute('INSERT INTO candidate_events (candidate_id, event_type, detail, created_at) VALUES (?,?,?,?)',
                         (cid, 'note', f"Final result (resume + call): {d['status'].title()} — {d['rule']}"[:500], at))
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()
    return {'result': res, 'at': at, 'by': who, 'pitch_id': pitch_id, 'call_id': call_id}, None, 200


def normalise(out, rows_in, call):
    """Clean the model output and apply the fixed decision rule."""
    from modules import pitch as P
    got = out.get('rows') if isinstance(out.get('rows'), list) else []
    by_req = {}
    for r in got:
        if isinstance(r, dict) and str(r.get('requirement') or '').strip():
            by_req[str(r['requirement']).strip().lower()] = r
    rows = []
    # keep the resume check's list and order: the decision is over the job's must-haves, not what the model chose
    src = rows_in or [{'requirement': str(r.get('requirement') or ''), 'status': 'UNCLEAR', 'evidence': ''}
                      for r in got if isinstance(r, dict)]
    for i, base in enumerate(src[:14]):
        req = str(base.get('requirement') or '').strip()
        if not req:
            continue
        r = by_req.get(req.lower()) or (got[i] if i < len(got) and isinstance(got[i], dict) else {})
        st = str(r.get('final_status') or '').strip().upper().replace(' ', '_')
        st = st if st in STATUSES else (base.get('status') if base.get('status') in STATUSES else 'UNCLEAR')
        rows.append({'requirement': req[:160], 'kind': base.get('kind') or 'other',
                     'resume_status': base.get('status') if base.get('status') in STATUSES else 'UNCLEAR',
                     'resume_evidence': str(r.get('resume_evidence') or base.get('evidence') or '').strip()[:300],
                     'call_evidence': str(r.get('call_evidence') or '').strip()[:300] or 'not discussed',
                     'status': st, 'why': str(r.get('why') or '').strip()[:240]})
    try:
        score = max(0, min(100, int(round(float(out.get('final_score', 0))))))
    except Exception:
        score = 0
    lst = lambda k: [str(x).strip() for x in (out.get(k) or []) if str(x).strip()][:5] if isinstance(out.get(k), list) else []
    res = {'rows': rows, 'final_score': score, 'summary': str(out.get('summary') or '').strip()[:1200],
           'shortlist_reasons': lst('shortlist_reasons'), 'reject_reasons': lst('reject_reasons'),
           'open_points': lst('open_points'), 'client_summary': P._clean_text(out.get('client_summary'))[:1200],
           'interest_level': str(call.get('interest_level') or '').upper(),
           'interest_reason': str(call.get('interest_reason') or ''),
           'call_recommendation': str(call.get('recommendation') or '').upper(),
           'red_flags': [str(x) for x in (call.get('red_flags') or []) if str(x).strip()][:4]
           if isinstance(call.get('red_flags'), list) else []}
    dec = P.decide({'must_have_check': rows, 'match_score': score})
    # The candidate must WANT the job: low interest can never go to the client as shortlisted.
    if res['interest_level'] == 'LOW' and dec['status'] == 'shortlisted':
        dec = {'status': 'hold', 'shareable': False, 'verify': [],
               'rule': 'Fits on paper and on the call, but interest on the call was LOW — re-confirm interest before sharing'}
    elif res['interest_level'] == 'LOW' and dec['status'] == 'hold':
        dec['rule'] += ' · interest on the call was LOW'
    res['decision'] = dec
    return res


def _cached(conn, cid, mid, pitch_id, call_id):
    try:
        r = conn.execute(f'SELECT * FROM {TABLE} WHERE candidate_id=? AND mandate_id=? AND company_id=? '
                         'ORDER BY id DESC LIMIT 1', (cid, mid or 0, effective_company_id())).fetchone()
    except Exception:
        return None
    if not r:
        return None
    return {'result': _j(r['result'], {}), 'at': r['created_at'], 'by': r['created_by_name'],
            'stale': (r['pitch_id'] != pitch_id or r['call_id'] != call_id)}


@bp.route('/candidates/<int:cid>/final-result', methods=['GET'])
@login_required
def get_final(cid):
    conn = get_db()
    try:
        _ensure(conn)
        c, m, p, k = _inputs(conn, cid)
        if not c:
            return jsonify({'error': 'Candidate not found'}), 404
        have = {'pitch': bool(p), 'call': bool(k)}
        cached = _cached(conn, cid, c['mandate_id'], p['id'] if p else 0, k['id'] if k else 0)
        conn.commit()
    finally:
        conn.close()
    if cached and cached['result'].get('decision'):
        return jsonify({'ok': True, 'have': have, **cached})
    return jsonify({'ok': True, 'have': have, 'result': None, 'stale': False})


@bp.route('/candidates/<int:cid>/final-result', methods=['POST'])
@login_required
def make_final(cid):
    try:
        out, err, code = build(cid)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Final result failed: {type(e).__name__}: {e}'}), 500
    if err:
        return jsonify({'error': err}), code
    return jsonify({'ok': True, 'stale': False, **out})
