"""
RecruitOS — Freelancer Sourcer Module (Phase 1: Backend Foundation)

A restricted role that lets external freelancers source candidates (upload CVs
via the Naukri extension) against mandates the admin assigns to them, WITHOUT
giving them control over the pipeline.

Core rules (locked in spec):
  - Role: 'freelancer_sourcer' (below admin & recruiter)
  - Admin assigns specific mandates (many freelancers per mandate via link table)
  - Duplicate check is per-mandate → BLOCK duplicate uploads
  - Freelancer sees FULL detail + stage of ONLY their own sourced candidates
  - Freelancer CANNOT change stages, see others' candidates, or source on
    unassigned mandates

This module is additive-only. It never modifies existing candidate/mandate rows'
structure beyond the two columns added in server.py (sourced_by, sourced_at).
"""

import json
import datetime
from flask import Blueprint, request, jsonify

from modules.shared import (
    get_db, ts, current_user, effective_company_id, real_user_id,
    is_company_admin, login_required, log_activity,
)
from modules import register_migration

bp = Blueprint('freelancer', __name__, url_prefix='/api')

FREELANCER_ROLE = 'freelancer_sourcer'


# ══════════════════════════════════════════════════════════════════════════
#  MIGRATION
# ══════════════════════════════════════════════════════════════════════════
@register_migration
def migrate(conn):
    c = conn.cursor()
    # Link table: which freelancers can source on which mandates (many-to-many)
    c.execute('''CREATE TABLE IF NOT EXISTS mandate_freelancers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        mandate_id INTEGER NOT NULL,
        freelancer_user_id INTEGER NOT NULL,
        assigned_by INTEGER DEFAULT 0,
        assigned_at TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1
    )''')
    for sql in [
        'CREATE INDEX IF NOT EXISTS idx_mf_mandate ON mandate_freelancers(mandate_id, is_active)',
        'CREATE INDEX IF NOT EXISTS idx_mf_freelancer ON mandate_freelancers(freelancer_user_id, is_active)',
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_mf_unique ON mandate_freelancers(mandate_id, freelancer_user_id)',
    ]:
        try:
            c.execute(sql)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
#  HELPERS / GUARDS
# ══════════════════════════════════════════════════════════════════════════
def _is_freelancer(u=None):
    u = u or current_user()
    return bool(u) and u.get('role') == FREELANCER_ROLE


def _require_admin():
    """Return None if OK, else (response, code)."""
    if not is_company_admin():
        return jsonify({'error': 'Admin only'}), 403
    return None


def _require_freelancer():
    if not _is_freelancer():
        return jsonify({'error': 'Freelancer only'}), 403
    return None


def freelancer_can_access_mandate(conn, freelancer_user_id, mandate_id, company_id):
    """Is this mandate assigned to this freelancer?"""
    row = conn.execute(
        'SELECT id FROM mandate_freelancers WHERE mandate_id=? AND freelancer_user_id=? '
        'AND company_id=? AND is_active=1',
        (mandate_id, freelancer_user_id, company_id)).fetchone()
    return bool(row)


def _freelancer_stats(conn, company_id, freelancer_user_id):
    """Compute uploaded / screening / interested / placed counts for a freelancer."""
    rows = conn.execute(
        'SELECT stage, COUNT(*) n FROM candidates WHERE owner_id=? AND sourced_by=? '
        'GROUP BY stage', (company_id, freelancer_user_id)).fetchall()
    total = 0
    by_stage = {}
    for r in rows:
        by_stage[r['stage'] or ''] = r['n']
        total += r['n']
    interested_stages = ('Interested', 'Shared with Client', 'Interview Inprocess')
    interested = sum(by_stage.get(s, 0) for s in interested_stages)
    placed = by_stage.get('Placed', 0)
    # "screening" = anything early
    screening_stages = ('Screening', 'Follow Up 1', 'Follow Up 2', 'Not Contacted',
                        'Called', 'Updated CV awaited')
    screening = sum(by_stage.get(s, 0) for s in screening_stages)
    return {
        'uploaded': total,
        'screening': screening,
        'interested': interested,
        'placed': placed,
        'by_stage': by_stage,
    }


# ══════════════════════════════════════════════════════════════════════════
#  ADMIN ENDPOINTS — manage freelancers & assignments
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/freelancers', methods=['GET'])
@login_required
def list_freelancers():
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    company_id = effective_company_id()
    rows = conn.execute(
        "SELECT id, username, display_name, status, created_at, last_login "
        "FROM users WHERE company_id=? AND role=? ORDER BY display_name",
        (company_id, FREELANCER_ROLE)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d['stats'] = _freelancer_stats(conn, company_id, r['id'])
        # assigned mandate count
        d['mandate_count'] = conn.execute(
            'SELECT COUNT(*) n FROM mandate_freelancers WHERE freelancer_user_id=? '
            'AND company_id=? AND is_active=1', (r['id'], company_id)).fetchone()['n']
        out.append(d)
    conn.close()
    return jsonify({'ok': True, 'freelancers': out})


@bp.route('/freelancers', methods=['POST'])
@login_required
def create_freelancer():
    guard = _require_admin()
    if guard:
        return guard
    d = request.json or {}
    username = (d.get('username') or '').strip().lower()
    password = d.get('password') or ''
    display = (d.get('display_name') or '').strip() or username
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400

    from modules.shared import _core
    core = _core()
    conn = get_db()
    # Unique username check
    existing = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': 'Username already exists'}), 400
    company_id = effective_company_id()
    cu = current_user()
    company_name = cu.get('company_name', '') if cu else ''
    conn.execute(
        'INSERT INTO users (username,password_hash,display_name,role,created_at,status,'
        'company_name,company_id,is_company_admin) VALUES (?,?,?,?,?,?,?,?,0)',
        (username, core.hash_password(password), display, FREELANCER_ROLE, ts(),
         'approved', company_name, company_id))
    conn.commit()
    uid = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()['id']
    conn.close()
    log_activity('freelancer.create', f'Freelancer account created: {display} ({username})',
                 entity_type='user', entity_id=uid)
    return jsonify({'ok': True, 'id': uid, 'username': username})


@bp.route('/freelancers/<int:uid>/deactivate', methods=['POST'])
@login_required
def deactivate_freelancer(uid):
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    company_id = effective_company_id()
    u = conn.execute('SELECT id, role, company_id FROM users WHERE id=?', (uid,)).fetchone()
    if not u or u['company_id'] != company_id or u['role'] != FREELANCER_ROLE:
        conn.close()
        return jsonify({'error': 'Freelancer not found'}), 404
    conn.execute("UPDATE users SET status='disabled' WHERE id=?", (uid,))
    # Also deactivate their mandate assignments
    conn.execute('UPDATE mandate_freelancers SET is_active=0 WHERE freelancer_user_id=?', (uid,))
    conn.commit()
    conn.close()
    log_activity('freelancer.deactivate', 'Freelancer deactivated',
                 entity_type='user', entity_id=uid)
    return jsonify({'ok': True})


@bp.route('/freelancers/<int:uid>/reactivate', methods=['POST'])
@login_required
def reactivate_freelancer(uid):
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    company_id = effective_company_id()
    u = conn.execute('SELECT id, company_id, role FROM users WHERE id=?', (uid,)).fetchone()
    if not u or u['company_id'] != company_id or u['role'] != FREELANCER_ROLE:
        conn.close()
        return jsonify({'error': 'Freelancer not found'}), 404
    conn.execute("UPDATE users SET status='approved' WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@bp.route('/mandates/<int:mid>/freelancers', methods=['GET'])
@login_required
def list_mandate_freelancers(mid):
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    company_id = effective_company_id()
    rows = conn.execute(
        'SELECT mf.freelancer_user_id AS uid, u.display_name, u.username, mf.assigned_at '
        'FROM mandate_freelancers mf JOIN users u ON u.id=mf.freelancer_user_id '
        'WHERE mf.mandate_id=? AND mf.company_id=? AND mf.is_active=1',
        (mid, company_id)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'freelancers': [dict(r) for r in rows]})


@bp.route('/mandates/<int:mid>/freelancers', methods=['POST'])
@login_required
def assign_freelancer(mid):
    guard = _require_admin()
    if guard:
        return guard
    d = request.json or {}
    uid = int(d.get('freelancer_user_id') or 0)
    if not uid:
        return jsonify({'error': 'freelancer_user_id required'}), 400
    conn = get_db()
    company_id = effective_company_id()
    # Validate mandate belongs to company
    m = conn.execute('SELECT id FROM mandates WHERE id=? AND owner_id=?',
                     (mid, company_id)).fetchone()
    if not m:
        conn.close()
        return jsonify({'error': 'Mandate not found'}), 404
    # Validate freelancer belongs to company
    u = conn.execute('SELECT id FROM users WHERE id=? AND company_id=? AND role=?',
                     (uid, company_id, FREELANCER_ROLE)).fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'Freelancer not found'}), 404
    # Upsert (unique index on mandate+freelancer)
    existing = conn.execute(
        'SELECT id, is_active FROM mandate_freelancers WHERE mandate_id=? AND freelancer_user_id=?',
        (mid, uid)).fetchone()
    if existing:
        conn.execute('UPDATE mandate_freelancers SET is_active=1, assigned_at=?, assigned_by=? WHERE id=?',
                     (ts(), real_user_id(), existing['id']))
    else:
        conn.execute(
            'INSERT INTO mandate_freelancers (company_id,mandate_id,freelancer_user_id,'
            'assigned_by,assigned_at,is_active) VALUES (?,?,?,?,?,1)',
            (company_id, mid, uid, real_user_id(), ts()))
    conn.commit()
    conn.close()
    log_activity('freelancer.assign', f'Freelancer assigned to mandate {mid}',
                 entity_type='mandate', entity_id=mid, meta={'freelancer_id': uid})
    return jsonify({'ok': True})


@bp.route('/mandates/<int:mid>/freelancers/<int:uid>', methods=['DELETE'])
@login_required
def unassign_freelancer(mid, uid):
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    company_id = effective_company_id()
    conn.execute(
        'UPDATE mandate_freelancers SET is_active=0 WHERE mandate_id=? AND freelancer_user_id=? '
        'AND company_id=?', (mid, uid, company_id))
    conn.commit()
    conn.close()
    log_activity('freelancer.unassign', f'Freelancer unassigned from mandate {mid}',
                 entity_type='mandate', entity_id=mid, meta={'freelancer_id': uid})
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════
#  FREELANCER ENDPOINTS — their own restricted view
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/freelancer/my-mandates', methods=['GET'])
@login_required
def my_mandates():
    guard = _require_freelancer()
    if guard:
        return guard
    conn = get_db()
    company_id = effective_company_id()
    uid = real_user_id()
    rows = conn.execute(
        'SELECT m.id, m.role, m.client, m.location, m.status, m.jd '
        'FROM mandate_freelancers mf JOIN mandates m ON m.id=mf.mandate_id '
        'WHERE mf.freelancer_user_id=? AND mf.company_id=? AND mf.is_active=1 '
        'ORDER BY m.created_at DESC',
        (uid, company_id)).fetchall()
    # attach how many this freelancer sourced per mandate
    out = []
    for r in rows:
        d = dict(r)
        d['my_sourced'] = conn.execute(
            'SELECT COUNT(*) n FROM candidates WHERE mandate_id=? AND sourced_by=?',
            (r['id'], uid)).fetchone()['n']
        out.append(d)
    conn.close()
    return jsonify({'ok': True, 'mandates': out})


@bp.route('/freelancer/my-candidates', methods=['GET'])
@login_required
def my_candidates():
    guard = _require_freelancer()
    if guard:
        return guard
    conn = get_db()
    company_id = effective_company_id()
    uid = real_user_id()
    mandate_filter = request.args.get('mandate_id')
    q = ('SELECT c.id, c.name, c.phone, c.email, c.company, c.designation, '
         'c.experience, c.ctc_current, c.ctc_expected, c.location, c.stage, '
         'c.sourced_at, c.mandate_id, m.role AS mandate_role, m.client AS mandate_client '
         'FROM candidates c LEFT JOIN mandates m ON m.id=c.mandate_id '
         'WHERE c.owner_id=? AND c.sourced_by=?')
    params = [company_id, uid]
    if mandate_filter:
        q += ' AND c.mandate_id=?'
        params.append(int(mandate_filter))
    q += ' ORDER BY c.sourced_at DESC, c.id DESC'
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify({'ok': True, 'candidates': [dict(r) for r in rows]})


@bp.route('/freelancer/dashboard', methods=['GET'])
@login_required
def freelancer_dashboard():
    guard = _require_freelancer()
    if guard:
        return guard
    conn = get_db()
    company_id = effective_company_id()
    uid = real_user_id()
    stats = _freelancer_stats(conn, company_id, uid)
    mandate_count = conn.execute(
        'SELECT COUNT(*) n FROM mandate_freelancers WHERE freelancer_user_id=? '
        'AND company_id=? AND is_active=1', (uid, company_id)).fetchone()['n']
    conn.close()
    return jsonify({'ok': True, 'stats': stats, 'mandate_count': mandate_count})


@bp.route('/freelancer/check-duplicate', methods=['POST'])
@login_required
def check_duplicate():
    """Extension calls this before uploading. Per-mandate duplicate check.
    Returns {duplicate: true/false}. If true, the extension BLOCKS the upload."""
    guard = _require_freelancer()
    if guard:
        return guard
    d = request.json or {}
    mandate_id = int(d.get('mandate_id') or 0)
    phone = (d.get('phone') or '').strip()
    name = (d.get('name') or '').strip().lower()
    if not mandate_id:
        return jsonify({'error': 'mandate_id required'}), 400
    conn = get_db()
    company_id = effective_company_id()
    uid = real_user_id()
    # Must be assigned to this mandate
    if not freelancer_can_access_mandate(conn, uid, mandate_id, company_id):
        conn.close()
        return jsonify({'error': 'Mandate not assigned to you'}), 403
    # Normalise phone digits
    import re
    phone_digits = re.sub(r'[^0-9]', '', phone)
    dup = None
    if phone_digits and len(phone_digits) >= 10:
        # match last 10 digits on this mandate
        last10 = phone_digits[-10:]
        dup = conn.execute(
            "SELECT id, name FROM candidates WHERE mandate_id=? AND "
            "REPLACE(REPLACE(REPLACE(phone,' ',''),'-',''),'+','') LIKE ?",
            (mandate_id, '%' + last10)).fetchone()
    if not dup and name:
        dup = conn.execute(
            'SELECT id, name FROM candidates WHERE mandate_id=? AND LOWER(name)=?',
            (mandate_id, name)).fetchone()
    conn.close()
    if dup:
        return jsonify({'ok': True, 'duplicate': True,
                        'existing_name': dup['name'],
                        'message': 'This candidate is already sourced on this mandate.'})
    return jsonify({'ok': True, 'duplicate': False})


# ══════════════════════════════════════════════════════════════════════════
#  PERMISSION GUARD HOOK (called from server.py stage-change endpoints)
# ══════════════════════════════════════════════════════════════════════════
def block_if_freelancer():
    """Returns (response, code) if the current user is a freelancer, else None.
    server.py calls this at the top of stage-change / messaging endpoints."""
    if _is_freelancer():
        return jsonify({'error': 'Freelancers cannot perform this action'}), 403
    return None
