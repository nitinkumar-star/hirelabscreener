"""
RecruitOS — Speed: indexes for the core tables (Oct 2026)

Root cause of the slow app: the busiest tables had no index on the columns
every screen filters by. `candidates` had none on owner_id / mandate_id /
stage / email, and `stage_history` none on candidate_id, so opening Home,
Tasks or Analytics made SQLite read every row of those tables — once per
candidate. On a 30,000-candidate copy, Home never finished loading
(/api/tasks 118 s, /api/analytics 384 s).

This module only ADDS indexes (no table or data changes). Each index is
created inside its own try, so a column that does not exist on an older
database is simply skipped. `CREATE INDEX IF NOT EXISTS` makes every boot
after the first a no-op. Finally `PRAGMA optimize` refreshes SQLite's
statistics, bounded by `analysis_limit` so boot stays fast on a large DB.

To roll back: `DROP INDEX <name>` for any of the names below. Nothing else
depends on them; queries just get slow again.
"""

from modules import register_migration

INDEXES = [
    # candidates — filtered by tenant, job, stage, e-mail on almost every screen
    ('idx_cand_owner_mandate', 'candidates(owner_id, mandate_id)'),
    ('idx_cand_mandate_stage', 'candidates(mandate_id, stage)'),
    ('idx_cand_owner_stage', 'candidates(owner_id, stage)'),
    ('idx_cand_owner_created', 'candidates(owner_id, created_at)'),
    ('idx_cand_email_lower', 'candidates(LOWER(email))'),
    ('idx_cand_phone', 'candidates(phone)'),
    # per-candidate history and details (profile, journey, tasks, analytics)
    ('idx_sh_cand_time', 'stage_history(candidate_id, created_at)'),
    ('idx_sh_stage_cand', 'stage_history(to_stage, candidate_id)'),
    ('idx_ce_cand_time', 'candidate_events(candidate_id, created_at)'),
    ('idx_wh_cand', 'work_history(candidate_id)'),
    ('idx_rem_cand', 'reminders(candidate_id)'),
    ('idx_iv_cand', 'interviews(candidate_id)'),
    ('idx_iv_mandate', 'interviews(mandate_id)'),
    ('idx_iv_owner_status', 'interviews(owner_id, status)'),
    ('idx_emails_cand', 'emails(candidate_id)'),
    ('idx_sub_owner_status', 'submissions(owner_id, status)'),
    # jobs list and scope checks
    ('idx_mand_owner_status', 'mandates(owner_id, status)'),
    ('idx_users_company', 'users(company_id, status)'),
]


@register_migration
def _migrate_perf_indexes(conn):
    made = 0
    for name, target in INDEXES:
        try:
            conn.execute('CREATE INDEX IF NOT EXISTS %s ON %s' % (name, target))
            made += 1
        except Exception as e:          # table/column missing on an older DB: skip
            print('[perf] index %s skipped: %s' % (name, e))
    conn.commit()
    try:
        conn.execute('PRAGMA analysis_limit=1000')        # sample, so a big DB still boots fast
        has_stats = conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'").fetchone()
        conn.execute('PRAGMA optimize' if has_stats else 'ANALYZE')
        conn.commit()
    except Exception as e:
        print('[perf] optimize skipped: %s' % e)
    print('[perf] %d indexes ready' % made)
