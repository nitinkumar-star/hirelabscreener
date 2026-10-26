"""
RecruitOS — Company (tenant) guard  (isolation fix, Oct 2026)

ROOT CAUSE this module fixes
----------------------------
Every row carries its company (owner_id / company_id), but the check
"does this row belong to the caller's company?" was written inside each
endpoint, one by one. Where an endpoint forgot it, another agency could read or
change the row just by putting its id in the URL — and ids are 1, 2, 3 … so
every row could be walked. The deep audit found five such endpoints
(candidate AI-compose, interview message, e-mail history, a job's e-mail
templates, WhatsApp learned replies).

THE FIX (one place, deny by default)
------------------------------------
Before any /api request runs, every object id in the URL is looked up and its
company compared with the caller's company. A row of another company -> 404,
exactly as if it did not exist. This covers the five endpoints AND every
future endpoint that follows the same URL shapes, so a forgotten check can no
longer leak across agencies. (modules/access.py does the same one level down:
recruiter -> only their assigned jobs.)

Rows that do not exist pass through (the endpoint answers its own 404).
Rows with no company (0/NULL) are platform-wide defaults: anyone may read them,
only the platform owner may change them.

The platform owner (role 'admin') is not limited here: the super-admin panel
works across companies by design, and "view as" sets the company to act on.

Emergency switch: env TENANT_GUARD=off disables this guard without a
redeploy (the per-endpoint checks remain).

Suspended agencies (same request hook): the login screen refused a suspended
agency or an expired trial, but a session opened before that kept working for
up to 31 days. Now every /api request re-checks the agency: suspended -> the
session ends (401); trial over -> 402 except sign-in / billing, so they can pay.
"""

import os
import re
import datetime

from flask import Blueprint, request, jsonify, session, g

from modules.shared import get_db

bp = Blueprint('tenant_guard', __name__)

W = ('POST', 'PUT', 'PATCH', 'DELETE')

# (url regex, table, company column).  First match wins: specific before general.
_RULES = [(re.compile(p), t, c) for p, t, c in [
    # candidates
    (r'^/api/candidates/(\d+)(/|$)', 'candidates', 'owner_id'),
    (r'^/api/xp/candidate/(\d+)(/|$)', 'candidates', 'owner_id'),
    (r'^/api/match/candidate/(\d+)(/|$)', 'candidates', 'owner_id'),
    (r'^/api/org/for-candidate/(\d+)(/|$)', 'candidates', 'owner_id'),
    # jobs
    (r'^/api/mandates/(\d+)(/|$)', 'mandates', 'owner_id'),
    (r'^/api/access/mandates/(\d+)(/|$)', 'mandates', 'owner_id'),
    (r'^/api/review/(\d+)(/|$)', 'mandates', 'owner_id'),
    (r'^/api/job-profile/(\d+)(/|$)', 'mandates', 'owner_id'),
    (r'^/api/match/mandate/(\d+)(/|$)', 'mandates', 'owner_id'),
    (r'^/api/match/diag/job/(\d+)(/|$)', 'mandates', 'owner_id'),
    # interviews (rows from before interviews.owner_id existed resolve via the candidate)
    (r'^/api/interviews/feedback/(\d+)(/|$)', 'interview_feedback', 'owner_id'),
    (r'^/api/scorecards/(\d+)(/|$)', 'interview_feedback', 'owner_id'),
    (r'^/api/interviews/(\d+)(/|$)', 'interviews', 'owner_id'),
    # tasks
    (r'^/api/reminders/(\d+)(/|$)', 'reminders', 'owner_id'),
    (r'^/api/todo/tasks/(\d+)(/|$)', 'reminders', 'owner_id'),
    (r'^/api/task-alerts/(\d+)(/|$)', 'reminders', 'owner_id'),
    (r'^/api/todo/subtasks/(\d+)(/|$)', 'task_subtasks', 'company_id'),
    (r'^/api/todo/lists/(\d+)(/|$)', 'task_lists', 'company_id'),
    (r'^/api/todo/tags/(\d+)(/|$)', 'task_tag_defs', 'company_id'),
    (r'^/api/todo/auto-rules/(\d+)(/|$)', 'todo_auto_rules', 'company_id'),
    # submissions / offers
    (r'^/api/submissions/(\d+)(/|$)', 'submissions', 'owner_id'),
    (r'^/api/submission-drafts/(\d+)(/|$)', 'submission_drafts', 'owner_id'),
    (r'^/api/offers/(\d+)(/|$)', 'offers', 'company_id'),
    # WhatsApp
    (r'^/api/wa/conversations/(\d+)(/|$)', 'wa_conversations', 'company_id'),
    (r'^/api/wa/escalations/(\d+)(/|$)', 'wa_escalations', 'company_id'),
    (r'^/api/wa/learned/(\d+)(/|$)', 'wa_learned_responses', 'company_id'),
    (r'^/api/wa-suggestions/(\d+)(/|$)', 'wa_suggestions', 'company_id'),
    # e-mail
    (r'^/api/email-agent/item/(\d+)(/|$)', 'agent_items', 'owner_id'),
    (r'^/api/emailbox/attachment/(\d+)(/|$)', 'email_attachments', 'owner_id'),
    (r'^/api/emailbox/draft/(\d+)(/|$)', 'emails', 'owner_id'),
    (r'^/api/emailbox/(\d+)(/|$)', 'emails', 'owner_id'),
    (r'^/api/jobmail/campaign/(\d+)(/|$)', 'jobmail_campaigns', 'company_id'),
    (r'^/api/newsletter/issue/(\d+)(/|$)', 'newsletter_issues', 'company_id'),
    (r'^/api/campaigns/(\d+)(/|$)', 'campaigns', 'owner_id'),
    # meetings
    (r'^/api/scheduler/meetings/(\d+)(/|$)', 'meetings', 'company_id'),
    (r'^/api/google/meetings/(\d+)(/|$)', 'meetings', 'company_id'),
    # money
    (r'^/api/invoices/(\d+)(/|$)', 'invoices', 'owner_id'),
    (r'^/api/expenses/(\d+)(/|$)', 'expenses', 'owner_id'),
    (r'^/api/finance/(?:expense|receipt)/(\d+)(/|$)', 'expenses', 'owner_id'),
    # CRM / BD
    (r'^/api/crm/clients/(\d+)(/|$)', 'crm_clients', 'company_id'),
    (r'^/api/crm/client/(\d+)(/|$)', 'crm_clients', 'company_id'),
    (r'^/api/bd/clients/(\d+)(/|$)', 'crm_clients', 'company_id'),
    (r'^/api/crm-link/client/(\d+)(/|$)', 'crm_clients', 'company_id'),
    (r'^/api/bd/records/companies/(\d+)(/|$)', 'crm_clients', 'company_id'),
    (r'^/api/crm/contacts?/(\d+)(/|$)', 'crm_contacts', 'company_id'),
    (r'^/api/bd/records/contacts/(\d+)(/|$)', 'crm_contacts', 'company_id'),
    (r'^/api/crm/attachments/(\d+)(/|$)', 'crm_attachments', 'company_id'),
    (r'^/api/bd/activities/(\d+)(/|$)', 'crm_activities', 'company_id'),
    (r'^/api/bd/records/opportunities/(\d+)(/|$)', 'bd_opportunities', 'company_id'),
    (r'^/api/bd/views/(\d+)(/|$)', 'bd_views', 'company_id'),
    (r'^/api/departments/(\d+)(/|$)', 'departments', 'company_id'),
    (r'^/api/rme/memory/(\d+)(/|$)', 'rme_memories', 'company_id'),
    # people
    (r'^/api/users/(\d+)(/|$)', 'users', 'company_id'),
    (r'^/api/freelancers/(\d+)(/|$)', 'users', 'company_id'),
    (r'^/api/team/user/(\d+)(/|$)', 'users', 'company_id'),
    (r'^/api/team/(\d+)(/|$)', 'users', 'company_id'),
]]

# Platform-wide data an agency may read but only the platform owner may change.
_OWNER_WRITE_ONLY = [re.compile(p) for p in [
    r'^/api/rkg(/|$)',                       # shared skill / role knowledge graph
]]

# Platform maintenance: runs over EVERY agency (re-index, rebuild, queue).
_OWNER_ONLY = [re.compile(p) for p in [
    r'^/api/ai/(queue|vectors|jd)(/|$)',      # (/api/ai/reindex and index-status are per company: open)
]]


def _me():
    uid = session.get('user_id')
    if not uid:
        return None
    hit = g.get('_tg_user', False)
    if hit is not False:
        return hit
    conn = get_db()
    try:
        r = conn.execute('SELECT u.id, u.role, u.company_id, c.status AS co_status, c.plan AS co_plan, '
                         'c.billing_status AS co_billing, c.trial_ends_at AS co_trial '
                         'FROM users u LEFT JOIN companies c ON c.id=u.company_id WHERE u.id=?', (uid,)).fetchone()
    finally:
        conn.close()
    g._tg_user = dict(r) if r else None
    return g._tg_user


def caller_company():
    va = session.get('view_as_company')
    if va:
        return int(va)
    u = _me()
    return int(u['company_id'] or 0) if u else 0


def row_company(conn, table, col, ident):
    """(exists, company) for one row. Interviews / feedback saved before they
    carried an owner resolve through their candidate."""
    try:
        if table in ('interviews', 'interview_feedback'):
            r = conn.execute(f'SELECT {col} AS c, candidate_id AS k FROM {table} WHERE id=?', (ident,)).fetchone()
            if r and not r['c'] and r['k']:
                k = conn.execute('SELECT owner_id FROM candidates WHERE id=?', (r['k'],)).fetchone()
                return True, (int(k['owner_id'] or 0) if k else 0)
        else:
            r = conn.execute(f'SELECT {col} AS c FROM {table} WHERE id=?', (ident,)).fetchone()
    except Exception:
        return False, 0                       # table not created on this install: nothing to protect
    if not r:
        return False, 0
    return True, int(r['c'] or 0)


_ALWAYS_OPEN = re.compile(r'^/api/(auth|billing)(/|$)')


def _agency_blocked(me, path):
    """401 for a suspended agency, 402 for an ended trial (sign-in and billing stay open)."""
    if (me.get('co_status') or '') == 'suspended':
        if re.match(r'^/api/auth/(status|login|logout)/?$', path):
            session.clear()
            return None
        session.clear()
        return jsonify({'error': 'auth_required',
                        'message': 'Your agency account is currently suspended. Please contact support.'}), 401
    if (me.get('co_plan') != 'owner' and (me.get('co_billing') or '') in ('trial', 'past_due')
            and not _ALWAYS_OPEN.match(path)):
        try:            # same rule as sign-in: a trial (never converted to paid) whose end date passed
            if me.get('co_trial') and datetime.datetime.now() > datetime.datetime.fromisoformat(me['co_trial']):
                return jsonify({'error': 'Your free trial has ended. Please subscribe to continue using HireLab.'}), 402
        except Exception:
            pass
    return None


def _deny(code=404, msg='Not found'):
    return jsonify({'error': msg}), code


@bp.before_app_request
def _tenant_guard():
    path = request.path or ''
    if not path.startswith('/api/') or request.method in ('OPTIONS', 'HEAD'):
        return None
    if (os.environ.get('TENANT_GUARD', 'on') or 'on').strip().lower() == 'off':
        return None
    me = _me()
    if not me:
        return None                           # not logged in: login_required answers
    if me.get('role') == 'admin':
        return None                           # platform owner: super-admin panel spans companies
    blocked = _agency_blocked(me, path)
    if blocked is not None:
        return blocked
    write = request.method in W

    if any(rx.match(path) for rx in _OWNER_ONLY):
        return _deny(403, 'Only the platform owner can run this.')
    if write and any(rx.match(path) for rx in _OWNER_WRITE_ONLY):
        return _deny(403, 'Only the platform owner can change shared data.')

    for rx, table, col in _RULES:
        m = rx.match(path)
        if not m:
            continue
        conn = get_db()
        try:
            exists, owner = row_company(conn, table, col, int(m.group(1)))
        finally:
            conn.close()
        if not exists:
            return None
        mine = caller_company()
        if owner == mine and mine:
            return None
        if owner == 0 and not write:
            return None                       # platform default row: readable, not changeable
        print(f'[tenant-guard] refused uid={me["id"]} co={mine}: {request.method} {path} '
              f'({table} belongs to {owner or "platform"})')
        return _deny()
    return None
