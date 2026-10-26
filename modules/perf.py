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


# ══════════════════════════════════════════════════════════════════════════
#  Compression (Wave 3) — gzip text responses at the WSGI layer
# ══════════════════════════════════════════════════════════════════════════
# Nothing in the app compressed its responses: index.html went out as 1.8 MB
# and /api/tasks as 4 MB on every call. This wraps the WSGI app, so it runs
# AFTER every Flask hook (the menu and heartbeat <script> tags are already in
# the HTML). Only text types are compressed; files (CVs, PDFs, images, zips,
# backups), partial/ranged replies, event streams and anything already encoded
# pass through untouched.
import gzip as _gzip

COMPRESS_TYPES = ('text/html', 'text/css', 'text/plain', 'text/csv', 'text/javascript',
                  'application/json', 'application/javascript', 'application/x-javascript',
                  'image/svg+xml', 'application/manifest+json')
MIN_BYTES = 1400          # smaller than one network packet: not worth it
LEVEL = 5                 # good ratio, ~10x faster than level 9


class GzipMiddleware:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        if 'gzip' not in (environ.get('HTTP_ACCEPT_ENCODING') or '').lower() or environ.get('HTTP_RANGE'):
            return self.app(environ, start_response)
        captured = {}

        def capture(status, headers, exc_info=None):
            captured['status'], captured['headers'], captured['exc'] = status, headers, exc_info
            return lambda data: captured.setdefault('written', []).append(data)   # legacy write(), rarely used

        body_iter = self.app(environ, capture)
        status, headers = captured.get('status', '500 Internal Server Error'), captured.get('headers', [])
        h = {k.lower(): v for k, v in headers}
        ctype = (h.get('content-type') or '').split(';')[0].strip().lower()
        code = int(status.split(' ', 1)[0]) if status[:3].isdigit() else 500
        ok = (ctype in COMPRESS_TYPES and 'content-encoding' not in h and code not in (204, 206, 304)
              and environ.get('REQUEST_METHOD') != 'HEAD' and not captured.get('written'))
        if not ok:
            start_response(status, headers, captured.get('exc'))
            return body_iter
        try:
            data = b''.join(body_iter)
        finally:
            if hasattr(body_iter, 'close'):
                body_iter.close()
        if len(data) < MIN_BYTES:
            start_response(status, headers, captured.get('exc'))
            return [data]
        gz = _gzip.compress(data, LEVEL)
        out = []
        for k, v in headers:
            kl = k.lower()
            if kl == 'content-length':
                continue
            if kl == 'etag' and not v.startswith('W/'):
                v = 'W/' + v            # the bytes differ from the uncompressed ones
            if kl == 'vary':
                continue
            out.append((k, v))
        vary = [x.strip() for x in (h.get('vary') or '').split(',') if x.strip()]
        if 'accept-encoding' not in [x.lower() for x in vary]:
            vary.append('Accept-Encoding')
        out += [('Content-Encoding', 'gzip'), ('Content-Length', str(len(gz))), ('Vary', ', '.join(vary))]
        start_response(status, out, captured.get('exc'))
        return [gz]


def _install_gzip():
    try:
        from modules.shared import _core
        app = _core().app
        if not getattr(app.wsgi_app, '_hl_gzip', False):
            app.wsgi_app = GzipMiddleware(app.wsgi_app)
            app.wsgi_app._hl_gzip = True
    except Exception as e:                      # never block boot over compression
        print('[perf] gzip not installed: %s' % e)


_install_gzip()
