"""
RecruitOS — Experience Intelligence module (Layer 7: Resume Intelligence, first slice)

Wraps the deterministic Experience Intelligence + Feedback-Loop engine
(modules/xp_engine/) as a Flask blueprint and connects it to the existing ATS:

  • Derives REAL per-skill / per-domain years from work_history (dated arithmetic,
    not LLM guesswork) and stores it in candidates.experience_intelligence (JSON).
  • A domain lexicon that LEARNS: when a recruiter fixes/adds a domain skill, the
    term is remembered and every future resume mentioning it is recognised.
  • Explainable sourcing: rank your existing pool for a mandate's skills/domains,
    each result carrying a "why" (which skills, how many derived years).

Everything is ADDITIVE — two new columns on candidates, two new tables. The
legacy ATS is untouched; if this module is removed the old flat fields still work.

DeepSeek stays the extractor (best on messy resumes). This runs on top of the
structured data already stored — the LLM reads text, this does the maths.
"""

from flask import Blueprint, request, jsonify

from modules.shared import (
    get_db, current_user, effective_company_id, login_required,
    is_company_admin, log_activity,
)
from modules import register_migration
from modules.xp_engine import feedback_loop as fb

bp = Blueprint('xp', __name__, url_prefix='/api/xp')


# ══════════════════════════════════════════════════════════════════════════
#  MIGRATION  (additive columns + lexicon/corrections tables + seed)
# ══════════════════════════════════════════════════════════════════════════
@register_migration
def migrate(conn):
    # The engine's own ensure_schema does all additive work idempotently:
    #   - ADD COLUMN candidates.experience_intelligence / xp_derived_at
    #   - CREATE TABLE domain_lexicon / parse_corrections
    #   - seed the starter domain dictionary once
    try:
        fb.ensure_schema(conn)
    except Exception as e:
        print(f'[xp] ensure_schema failed: {e}')


# ══════════════════════════════════════════════════════════════════════════
#  READ: derived intelligence for one candidate
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/candidate/<int:cid>', methods=['GET'])
@login_required
def get_candidate_xp(cid):
    import json
    conn = get_db()
    row = conn.execute(
        'SELECT experience_intelligence, xp_derived_at, owner_id FROM candidates WHERE id=?',
        (cid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Candidate not found'}), 404
    # Tenant guard: candidate must belong to this company
    if row['owner_id'] and row['owner_id'] != effective_company_id():
        return jsonify({'error': 'Not permitted'}), 403
    try:
        data = json.loads(row['experience_intelligence'] or '{}')
    except Exception:
        data = {}
    return jsonify({'ok': True, 'derived_at': row['xp_derived_at'] or '', 'intelligence': data})


# ══════════════════════════════════════════════════════════════════════════
#  RECOMPUTE: one candidate (call after resume save / work-history edit)
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/candidate/<int:cid>/recompute', methods=['POST'])
@login_required
def recompute_one(cid):
    conn = get_db()
    row = conn.execute('SELECT owner_id FROM candidates WHERE id=?', (cid,)).fetchone()
    if not row:
        conn.close(); return jsonify({'error': 'Candidate not found'}), 404
    if row['owner_id'] and row['owner_id'] != effective_company_id():
        conn.close(); return jsonify({'error': 'Not permitted'}), 403
    try:
        fb.recompute_candidate(conn, cid)
    except Exception as e:
        conn.close(); return jsonify({'error': f'Recompute failed: {e}'}), 500
    conn.close()
    log_activity('xp.recomputed', 'Recomputed experience intelligence',
                 entity_type='candidate', entity_id=cid)
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════
#  BACKFILL: recompute the whole pool (one-time / admin)
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/recompute-all', methods=['POST'])
@login_required
def recompute_all():
    if not is_company_admin():
        return jsonify({'error': 'Only a company admin can run a full recompute.'}), 403
    conn = get_db()
    try:
        n = fb.recompute_all(conn)
    except Exception as e:
        conn.close(); return jsonify({'error': f'Recompute failed: {e}'}), 500
    conn.close()
    log_activity('xp.recompute_all', f'Recomputed experience intelligence for {n} candidates')
    return jsonify({'ok': True, 'computed': n})


# ══════════════════════════════════════════════════════════════════════════
#  CORRECTION: recruiter fixes/adds a skill → the lexicon learns
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/candidate/<int:cid>/correct', methods=['POST'])
@login_required
def correct_candidate(cid):
    d = request.get_json(force=True) or {}
    conn = get_db()
    row = conn.execute('SELECT owner_id FROM candidates WHERE id=?', (cid,)).fetchone()
    if not row:
        conn.close(); return jsonify({'error': 'Candidate not found'}), 404
    if row['owner_id'] and row['owner_id'] != effective_company_id():
        conn.close(); return jsonify({'error': 'Not permitted'}), 403
    u = current_user()
    try:
        fb.record_correction(
            conn, cid, d.get('field', ''), d.get('old', ''), d.get('new', ''),
            user=(u['username'] if u else ''),
            learn_as_skill=bool(d.get('learn_as_skill')),
            category=d.get('category', 'skill'), domain=d.get('domain', 'unknown'))
        fb.recompute_candidate(conn, cid)
    except Exception as e:
        conn.close(); return jsonify({'error': f'Correction failed: {e}'}), 500
    conn.close()
    log_activity('xp.correction',
                 f'Corrected {d.get("field", "skill")}: {d.get("old", "")} → {d.get("new", "")}',
                 entity_type='candidate', entity_id=cid,
                 meta={'learn_as_skill': bool(d.get('learn_as_skill')), 'domain': d.get('domain', '')})
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════
#  SOURCE: explainable ranked shortlist from the existing pool
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/source', methods=['POST'])
@login_required
def source_pool():
    d = request.get_json(force=True) or {}
    conn = get_db()
    try:
        ranked = fb.rank_pool_for_mandate(
            conn,
            required_skills=d.get('skills', []),
            required_domains=d.get('domains', []),
            min_overall_years=d.get('min_years', 0),
            top=d.get('top', 25))
    except Exception as e:
        conn.close(); return jsonify({'error': f'Sourcing failed: {e}'}), 500
    conn.close()
    return jsonify({'ok': True, 'results': ranked})
