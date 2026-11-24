"""
What the Claude connector (mcp_server.py) may READ from the ATS (Nov 2026).

Goal: ask Claude anything about the ATS. Instead of one hand-written MCP tool
per screen, the connector asks this endpoint which read-only API routes exist
(every GET /api/... route, with the first lines of its docstring as the
description) and may then call any of them. New ATS features show up in the
connector by themselves.

  GET /api/mcp/catalog?q=invoice   -> {routes: [{path, params, doc}], deny: [...]}

Left out on purpose (MCP_DENY below):
  * secrets and settings, auth, backups, exports and file/CV/recording downloads
  * public token links (candidate / booking pages)
  * GET routes that CHANGE something (mark a task done, snooze, link duplicates)
    or start paid AI work (pitch, day plan)

The connector logs in as its service account, so the ATS's own tenant and
recruiter rules still decide what data comes back.
"""

import re
import inspect

from flask import Blueprint, jsonify, request, current_app

from modules.shared import login_required, get_db, effective_company_id

bp = Blueprint('mcp_catalog', __name__)

MCP_DENY = re.compile('|'.join([
    r'^/api/mcp/',
    r'^/api/auth/', r'^/api/admin/backup', r'backup',
    r'setting', r'secret', r'password', r'token', r'oauth', r'/me/mail', r'meet-link',
    r'/export', r'download', r'/attachment', r'attachments', r'/files/', r'^/api/cv', r'^/api/calls/',
    r'/receipt/', r'submission-excel', r'/report$', r'/review-link',
    r'^/api/public/', r'/public/', r'ref-token',
    r'/reminders/<int:[a-z_]+>/(done|snooze)', r'link-duplicates', r'/pitch$', r'^/api/todo/plan',
    r'^/api/extension/', r'^/api/wa-outbox', r'/preview$',
]), re.I)


def allowed(rule):
    return rule.startswith('/api/') and not MCP_DENY.search(rule)


def _doc(fn):
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    d = (getattr(fn, '__doc__', None) or '').strip()
    d = re.sub(r'\s+', ' ', d)
    return d[:300]


_ARG_RX = re.compile(r"request\.args\.get(?:list)?\(\s*['\"]([A-Za-z0-9_]+)['\"]")


def _query_args(fn):
    """The ?query= options a route reads (request.args.get('x')), so Claude knows its filters."""
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    try:
        return sorted(set(_ARG_RX.findall(inspect.getsource(fn))))[:20]
    except Exception:
        return []


def routes():
    app = current_app
    out = []
    for r in app.url_map.iter_rules():
        if 'GET' not in r.methods or not allowed(r.rule):
            continue
        fn = app.view_functions.get(r.endpoint)
        out.append({'path': r.rule, 'params': sorted(r.arguments), 'query': _query_args(fn) if fn else [],
                    'doc': _doc(fn) if fn else ''})
    out.sort(key=lambda x: x['path'])
    return out


@bp.route('/api/mcp/catalog')
@login_required
def catalog():
    q = (request.args.get('q') or '').strip().lower()
    rs = routes()
    if q:
        words = q.split()
        rs = [r for r in rs if all(w in (r['path'] + ' ' + r['doc']).lower() for w in words)]
    return jsonify({'ok': True, 'count': len(rs), 'routes': rs, 'deny': MCP_DENY.pattern})


# ── The CV itself, as text (for Claude to read and analyse) ─────────────────
# The connector cannot use the CV file download (binary, and kept out of the
# catalog). This gives the text of the candidate's stored CV instead. The URL is
# under /api/candidates/<id>/, so the access guard (modules/access.py) applies
# the same recruiter rules as opening the profile.
CV_TEXT_MAX = 30000


@bp.route('/api/candidates/<int:cid>/cv-text')
@login_required
def candidate_cv_text(cid):
    """Text of the candidate's CV (PDF / Word), for reading and analysis. ?max_chars= (default 15000, max 30000)."""
    try:
        mx = max(1000, min(int(request.args.get('max_chars') or 15000), CV_TEXT_MAX))
    except ValueError:
        mx = 15000
    conn = get_db()
    try:
        c = conn.execute('SELECT * FROM candidates WHERE id=? AND owner_id=?', (cid, effective_company_id())).fetchone()
    finally:
        conn.close()
    if not c:
        return jsonify({'error': 'Not found'}), 404
    out = {'ok': True, 'candidate_id': cid, 'name': c['name'] or '',
           'cv_file': (c['cv_original_name'] if 'cv_original_name' in c.keys() else '') or '',
           'has_cv': bool((c['cv_path'] or '').strip())}
    if not out['has_cv']:
        out.update(text='', note='No CV file is stored for this candidate. Use get_candidate for the parsed profile.')
        return jsonify(out)
    from modules.skill_engine import cv_text_light
    text = cv_text_light(c, max_chars=mx + 1, pages=12, keep_lines=True)     # bounded memory: page by page, one CV at a time
    out['truncated'] = len(text) > mx
    out['text'] = text[:mx]
    out['chars'] = len(out['text'])
    if not text:
        out['note'] = 'The CV file could not be read as text (scanned image, or an unsupported format).'
    return jsonify(out)
