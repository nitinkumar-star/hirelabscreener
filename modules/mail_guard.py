"""
RecruitOS — per-job "Automated e-mails OFF" switch  (Oct 2026)

Mandate Setup → Manage → "Automated e-mails to this job's candidates".
When a job is switched OFF, NO automated / bulk e-mail goes to anyone who is
in that job's pipeline:

  * Job Mail (job-match mails about any job)       modules/jobmail.py
  * Re-engage (6-monthly profile update + reminder) modules/reengage.py
  * Mass e-mail campaigns (Email Box → Campaigns)   server.py _camp_*
  * Email Agent follow-ups                          server.py _email_agent_scan_impl
  * Market Pulse newsletter                         modules/newsletter.py (via Talent Pool "emailable")

A PERSON is protected, not only a record: if the same e-mail address sits in a
switched-off job, the person is skipped everywhere — so a copy of them in the
Central Database does not get mailed either.

Nothing is deleted or rewritten: the senders simply check this flag at send
time, so switching the job back ON restores everything exactly as it was.
1:1 e-mails a recruiter writes by hand are never blocked.

Additive only: one new column mandates.auto_mail_off (default 0 = ON).
ROLLBACK: remove 'mail_guard' from modules/__init__.py — every helper below
then fails soft (callers wrap it) and mails flow as before.
"""

from flask import Blueprint, request, jsonify

from modules.shared import get_db, ts, effective_company_id, login_required, _core
from modules import register_migration

bp = Blueprint('mail_guard', __name__, url_prefix='/api')

REASON = 'Automated e-mails switched off for this job'


@register_migration
def migrate(conn):
    ensure(conn)


def ensure(conn):
    try:
        have = {r[1] for r in conn.execute('PRAGMA table_info(mandates)').fetchall()}
        if have and 'auto_mail_off' not in have:
            conn.execute('ALTER TABLE mandates ADD COLUMN auto_mail_off INTEGER DEFAULT 0')
            conn.commit()
    except Exception as e:
        print('[mail_guard] migrate:', e)


def _has_col(conn):
    try:
        return 'auto_mail_off' in {r[1] for r in conn.execute('PRAGMA table_info(mandates)').fetchall()}
    except Exception:
        return False


def blocked(conn, oid):
    """(candidate_ids, emails, label_by_email) of everyone in a switched-off job of company `oid`.
    Emails are lower-cased. Cheap: one indexed join."""
    if not _has_col(conn):
        return set(), set(), {}
    rows = conn.execute(
        "SELECT c.id, LOWER(TRIM(COALESCE(c.email,''))) AS em, m.role, m.client FROM candidates c "
        "JOIN mandates m ON m.id=c.mandate_id AND m.owner_id=c.owner_id "
        "WHERE c.owner_id=? AND COALESCE(m.auto_mail_off,0)=1", (oid,)).fetchall()
    ids, ems, lab = set(), set(), {}
    for r in rows:
        ids.add(r['id'])
        if r['em'] and '@' in r['em']:
            ems.add(r['em'])
            lab.setdefault(r['em'], (r['role'] or 'a job') + (' — ' + r['client'] if r['client'] else ''))
    return ids, ems, lab


def is_blocked(conn, oid, candidate_id=0, email=''):
    """True if this record, or anyone with this e-mail, is in a switched-off job."""
    if not _has_col(conn):
        return False
    em = str(email or '').strip().lower()
    q = ("SELECT 1 FROM candidates c JOIN mandates m ON m.id=c.mandate_id AND m.owner_id=c.owner_id "
         "WHERE c.owner_id=? AND COALESCE(m.auto_mail_off,0)=1 AND (c.id=?" + (" OR LOWER(c.email)=?" if em else "")
         + ") LIMIT 1")
    args = [oid, int(candidate_id or 0)] + ([em] if em else [])
    return conn.execute(q, args).fetchone() is not None


def reason_for(conn, oid, candidate_id=0, email=''):
    if not is_blocked(conn, oid, candidate_id, email):
        return ''
    return REASON


@bp.route('/mandates/<int:mid>/auto-mail', methods=['GET', 'POST'])
@login_required
def auto_mail(mid):
    oid = effective_company_id()
    conn = get_db()
    try:
        ensure(conn)
        m = conn.execute('SELECT id, role, client, COALESCE(auto_mail_off,0) AS off FROM mandates WHERE id=? AND owner_id=?',
                         (mid, oid)).fetchone()
        if not m:
            return jsonify({'error': 'Job not found'}), 404
        if request.method == 'POST':
            try:
                from modules.access import can_see_mandate
                if not can_see_mandate(conn, mid, write=True):
                    return jsonify({'error': 'You cannot change this job.'}), 403
            except Exception:
                pass
            d = request.get_json(silent=True) or {}
            off = 1 if d.get('off') else 0
            conn.execute('UPDATE mandates SET auto_mail_off=? WHERE id=? AND owner_id=?', (off, mid, oid))
            conn.commit()
            try:
                _core().log_activity('mandate.auto_mail', ('Automated e-mails switched OFF' if off else 'Automated e-mails switched ON')
                                     + f" for {m['role'] or 'job'}", entity_type='mandate', entity_id=mid)
            except Exception:
                pass
            m = conn.execute('SELECT id, role, client, COALESCE(auto_mail_off,0) AS off FROM mandates WHERE id=?', (mid,)).fetchone()
        n = conn.execute('SELECT COUNT(*) FROM candidates WHERE mandate_id=? AND owner_id=?', (mid, oid)).fetchone()[0]
        pend = 0
        try:
            pend = conn.execute("SELECT COUNT(*) FROM campaign_recipients r JOIN candidates c ON c.id=r.candidate_id "
                                "WHERE c.mandate_id=? AND r.owner_id=? AND r.status='pending'", (mid, oid)).fetchone()[0]
        except Exception:
            pend = 0
        return jsonify({'ok': True, 'off': bool(m['off']), 'candidates': n, 'pending_campaign_mails': pend})
    finally:
        conn.close()
