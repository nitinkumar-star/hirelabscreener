"""
RecruitOS — BD Workspace records engine  (BD Command Center v2, Wave 1)

Twenty-CRM-style data layer for the BD workspace. One generic engine serves
five objects through the same API, so filters / sort / views / "Calculate"
are written once and work everywhere:

    companies      -> crm_clients          (existing table, 4 new columns)
    people         -> crm_contacts         (existing table)
    opportunities  -> bd_opportunities     (NEW table — BD deals / pipeline)
    tasks          -> crm_activities       (task | followup | call | meeting)
    notes          -> crm_activities       (note)

Writes are delegated to the EXISTING services wherever they exist
(crm.ClientService, crm.ContactService, bd.ActivityService) so dedup rules,
audit trail and the activity timeline behave exactly as before. This module
only adds what those services never had.

Everything is additive:
  * new tables  : bd_opportunities, bd_views
  * new columns : crm_clients.domain / linkedin / employees / annual_revenue
                  crm_activities.opportunity_id
  * new routes  : /api/bd/objects, /api/bd/records/..., /api/bd/views...
Nothing existing is renamed, dropped or rewritten.

SQL safety: every column expression comes from the FIELD REGISTRY below.
Field keys from the client are only used to LOOK UP the registry and are never
concatenated into SQL. All values are bound parameters.
"""

import re
import json
import datetime
from flask import Blueprint, request, jsonify, session

from modules.shared import (
    get_db, ts, effective_company_id, real_user_id, is_company_admin,
    login_required, log_activity, record_changes,
)
from modules import register_migration

bp = Blueprint('bd_records', __name__, url_prefix='/api/bd')


# ══════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ══════════════════════════════════════════════════════════════════════════
# Opportunity pipeline — recruitment-agency BD stages. (key, label, default %)
OPP_STAGES = [
    ('lead',          'Lead',                    10),
    ('contacted',     'Contacted',               20),
    ('meeting_done',  'Meeting Done',            40),
    ('proposal_sent', 'Terms/Proposal Sent',     60),
    ('won',           'Agreement Signed (Won)', 100),
    ('lost',          'Lost',                     0),
]
OPP_STAGE_KEYS = [s[0] for s in OPP_STAGES]
OPP_STAGE_PROB = {s[0]: s[2] for s in OPP_STAGES}
OPP_CLOSED = ('won', 'lost')

OPP_SOURCES = [
    ('referral', 'Referral'), ('linkedin', 'LinkedIn'), ('cold_call', 'Cold Call'),
    ('cold_email', 'Cold Email'), ('inbound', 'Inbound'),
    ('existing_client', 'Existing Client'), ('event', 'Event'), ('other', 'Other'),
]

CLIENT_STATUSES = [('active', 'Active'), ('prospect', 'Prospect'),
                   ('inactive', 'Inactive'), ('lost', 'Lost')]
TASK_TYPES = [('task', 'Task'), ('followup', 'Follow-up'),
              ('call', 'Call'), ('meeting', 'Meeting')]
TASK_STATUSES = [('open', 'Open'), ('done', 'Done'), ('cancelled', 'Cancelled')]

MAX_PER_PAGE = 200
MAX_CONDITIONS = 30
MAX_FILTER_DEPTH = 3
MAX_SORTS = 3

_KEY_RE = re.compile(r'^[a-z][a-z0-9_]{0,40}$')
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_NOW_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$')


class RecordError(Exception):
    def __init__(self, message, code=400, **extra):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra


# ══════════════════════════════════════════════════════════════════════════
#  MIGRATION  (additive only)
# ══════════════════════════════════════════════════════════════════════════
@register_migration
def migrate(conn):
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS bd_opportunities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,              -- tenant
        client_id INTEGER NOT NULL,               -- FK -> crm_clients.id
        contact_id INTEGER DEFAULT 0,             -- point of contact, FK -> crm_contacts.id
        name TEXT NOT NULL DEFAULT '',
        stage TEXT DEFAULT 'lead',                -- see OPP_STAGES
        amount REAL,                              -- expected billing (NULL = not set)
        currency TEXT DEFAULT 'INR',
        probability INTEGER DEFAULT 10,           -- 0..100
        close_date TEXT DEFAULT '',               -- YYYY-MM-DD
        owner_user_id INTEGER DEFAULT 0,
        source TEXT DEFAULT '',
        lost_reason TEXT DEFAULT '',
        description TEXT DEFAULT '',
        mandate_id INTEGER DEFAULT 0,             -- set when converted to a job (later wave)
        stage_changed_at TEXT DEFAULT '',
        won_at TEXT DEFAULT '',
        lost_at TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1,
        created_by INTEGER DEFAULT 0,
        updated_by INTEGER DEFAULT 0,
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS bd_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,                 -- creator
        object TEXT NOT NULL,                     -- companies|people|opportunities|tasks|notes
        name TEXT NOT NULL DEFAULT '',
        config TEXT DEFAULT '{}',                 -- JSON: columns, filter, sort, calc, view_type
        visibility TEXT DEFAULT 'private',        -- private | team
        is_default INTEGER DEFAULT 0,             -- creator's default view for this object
        position INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    )''')

    # Twenty-style company fields the CRM never had.
    for col, defn in [('domain', "TEXT DEFAULT ''"), ('linkedin', "TEXT DEFAULT ''"),
                      ('employees', 'INTEGER'), ('annual_revenue', 'REAL')]:
        try:
            c.execute(f'ALTER TABLE crm_clients ADD COLUMN {col} {defn}')
        except Exception:
            pass

    # Tasks / notes can hang off a deal, not only a company.
    try:
        c.execute('ALTER TABLE crm_activities ADD COLUMN opportunity_id INTEGER DEFAULT 0')
    except Exception:
        pass

    for sql in [
        'CREATE INDEX IF NOT EXISTS idx_bdopp_company ON bd_opportunities(company_id, is_active, stage)',
        'CREATE INDEX IF NOT EXISTS idx_bdopp_client ON bd_opportunities(client_id, is_active)',
        'CREATE INDEX IF NOT EXISTS idx_bdopp_owner ON bd_opportunities(company_id, owner_user_id)',
        'CREATE INDEX IF NOT EXISTS idx_bdviews_obj ON bd_views(company_id, object, is_active)',
        'CREATE INDEX IF NOT EXISTS idx_bd_act_opp ON crm_activities(opportunity_id, is_active)',
        'CREATE INDEX IF NOT EXISTS idx_bd_act_contact ON crm_activities(contact_id, is_active)',
    ]:
        try:
            c.execute(sql)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
#  FIELD REGISTRY
# ══════════════════════════════════════════════════════════════════════════
TEXT_TYPES = {'text', 'long_text', 'email', 'phone', 'url', 'tags'}
NUM_TYPES = {'number', 'currency', 'percent'}
DATE_TYPES = {'date', 'datetime'}

OPS_BY_TYPE = {
    'text':     ['contains', 'not_contains', 'eq', 'neq', 'starts_with', 'is_empty', 'is_not_empty'],
    'number':   ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'between', 'is_empty', 'is_not_empty'],
    'date':     ['is', 'before', 'after', 'on_or_before', 'on_or_after', 'between',
                 'is_today', 'is_past', 'is_future', 'in_last_days', 'in_next_days',
                 'is_empty', 'is_not_empty'],
    'select':   ['in', 'not_in', 'is_empty', 'is_not_empty'],
    'bool':     ['is_true', 'is_false'],
    'user':     ['in', 'not_in', 'is_me', 'is_empty', 'is_not_empty'],
    'relation': ['in', 'not_in', 'label_contains', 'is_empty', 'is_not_empty'],
}

CALC_BY_TYPE = {
    'text':     ['count_empty', 'count_not_empty', 'count_unique', 'pct_empty', 'pct_not_empty'],
    'number':   ['sum', 'avg', 'min', 'max', 'count_empty', 'count_not_empty', 'pct_empty', 'pct_not_empty'],
    'date':     ['earliest', 'latest', 'count_empty', 'count_not_empty', 'pct_empty', 'pct_not_empty'],
    'select':   ['count_empty', 'count_not_empty', 'count_unique', 'pct_empty', 'pct_not_empty'],
    'bool':     ['count_true', 'count_false', 'pct_true'],
    'user':     ['count_empty', 'count_not_empty', 'count_unique', 'pct_empty', 'pct_not_empty'],
    'relation': ['count_empty', 'count_not_empty', 'count_unique', 'pct_empty', 'pct_not_empty'],
}


def _family(ftype):
    if ftype in TEXT_TYPES:
        return 'text'
    if ftype in NUM_TYPES:
        return 'number'
    if ftype in DATE_TYPES:
        return 'date'
    return ftype  # select | bool | user | relation


def _user_label(expr):
    return (f"(SELECT COALESCE(NULLIF(u.display_name,''), u.username) FROM users u "
            f"WHERE u.id={expr})")


def F(key, label, ftype, expr, editable=False, searchable=False, options=None,
      target=None, label_expr=None, visible=False, width=160, required=False,
      computed=False):
    """One column definition. `expr` / `label_expr` are trusted SQL written here."""
    if ftype == 'user' and label_expr is None:
        label_expr = _user_label(expr)
    return {
        'key': key, 'label': label, 'type': ftype, 'expr': expr,
        'editable': editable, 'searchable': searchable,
        'options': options or [], 'target': target, 'label_expr': label_expr,
        'visible': visible, 'width': width, 'required': required,
        'computed': computed,
    }


def _opt(pairs):
    return [{'value': v, 'label': l} for v, l in pairs]


_ACT_LAST = ("(SELECT MAX(COALESCE(NULLIF(x.created_at,''), x.due_at)) FROM crm_activities x "
             "WHERE x.{col}={ref} AND x.is_active=1)")

OBJECTS = {
    # ── Companies ─────────────────────────────────────────────────────────
    'companies': {
        'label': 'Companies', 'singular': 'Company', 'title': 'name',
        'from': 'crm_clients c',
        'where': 'c.company_id=? AND c.is_active=1 AND COALESCE(c.is_internal,0)=0',
        'id_expr': 'c.id',
        'default_sort': [{'field': 'created_at', 'dir': 'desc'}],
        'fields': [
            F('name', 'Name', 'text', 'c.name', editable=True, searchable=True, visible=True, width=220, required=True),
            F('domain', 'Domain', 'url', 'c.domain', editable=True, searchable=True, visible=True),
            F('status', 'Status', 'select', 'c.status', editable=True, options=_opt(CLIENT_STATUSES), visible=True, width=120),
            F('owner_user_id', 'Account Owner', 'user', 'c.owner_user_id', editable=True, visible=True),
            F('industry', 'Industry', 'text', 'c.industry', editable=True, searchable=True, visible=True),
            F('city', 'City', 'text', 'c.city', editable=True, searchable=True, visible=True, width=130),
            F('state', 'State', 'text', 'c.state', editable=True, width=130),
            F('country', 'Country', 'text', 'c.country', editable=True, width=120),
            F('website', 'Website', 'url', 'c.website', editable=True),
            F('linkedin', 'LinkedIn', 'url', 'c.linkedin', editable=True),
            F('employees', 'Employees', 'number', 'c.employees', editable=True, width=120),
            F('annual_revenue', 'Annual Revenue', 'currency', 'c.annual_revenue', editable=True, width=150),
            F('address', 'Address', 'long_text', 'c.address', editable=True, width=220),
            F('gstin', 'GSTIN', 'text', 'c.gstin', editable=True, searchable=True),
            F('notes', 'Notes', 'long_text', 'c.notes', editable=True, width=220),
            F('people_count', 'People', 'number',
              '(SELECT COUNT(*) FROM crm_contacts x WHERE x.client_id=c.id AND x.is_active=1)',
              computed=True, visible=True, width=100),
            F('open_jobs', 'Open Jobs', 'number',
              "(SELECT COUNT(*) FROM mandates m WHERE m.owner_id=c.company_id AND m.crm_client_id=c.id "
              "AND (m.status IS NULL OR m.status='active'))", computed=True, width=110),
            F('open_opps', 'Open Deals', 'number',
              "(SELECT COUNT(*) FROM bd_opportunities x WHERE x.client_id=c.id AND x.company_id=c.company_id "
              "AND x.is_active=1 AND x.stage NOT IN ('won','lost'))", computed=True, visible=True, width=110),
            F('pipeline_value', 'Pipeline Value', 'currency',
              "(SELECT COALESCE(SUM(x.amount),0) FROM bd_opportunities x WHERE x.client_id=c.id "
              "AND x.company_id=c.company_id AND x.is_active=1 AND x.stage NOT IN ('won','lost'))",
              computed=True, visible=True, width=140),
            F('last_activity', 'Last Activity', 'datetime', _ACT_LAST.format(col='client_id', ref='c.id'),
              computed=True, visible=True, width=150),
            F('created_by', 'Created by', 'user', 'c.created_by', computed=True),
            F('created_at', 'Creation date', 'datetime', 'c.created_at', computed=True, visible=True, width=150),
            F('updated_at', 'Last update', 'datetime', 'c.updated_at', computed=True, width=150),
        ],
    },

    # ── People ────────────────────────────────────────────────────────────
    'people': {
        'label': 'People', 'singular': 'Person', 'title': 'name',
        'from': 'crm_contacts p JOIN crm_clients c ON c.id=p.client_id AND c.company_id=p.company_id',
        'where': 'p.company_id=? AND p.is_active=1 AND c.is_active=1 AND COALESCE(c.is_internal,0)=0',
        'id_expr': 'p.id',
        'default_sort': [{'field': 'created_at', 'dir': 'desc'}],
        'fields': [
            F('name', 'Name', 'text', 'p.name', editable=True, searchable=True, visible=True, width=200, required=True),
            F('client_id', 'Company', 'relation', 'p.client_id', editable=True, target='companies',
              label_expr='c.name', visible=True, width=180, required=True),
            F('designation', 'Job Title', 'text', 'p.designation', editable=True, searchable=True, visible=True),
            F('email', 'Email', 'email', 'p.email', editable=True, searchable=True, visible=True, width=200),
            F('phone', 'Phone', 'phone', 'p.phone', editable=True, searchable=True, visible=True, width=140),
            F('department', 'Department', 'text', 'p.department', editable=True, visible=True, width=140),
            F('linkedin', 'LinkedIn', 'url', 'p.linkedin', editable=True),
            F('is_primary', 'Primary', 'bool', 'p.is_primary', editable=True, width=90),
            F('is_decision_maker', 'Decision Maker', 'bool', 'p.is_decision_maker', editable=True, width=130),
            F('role_tags', 'Roles', 'tags', 'p.role_tags', width=160),
            F('reports_to', 'Reports To', 'relation', 'p.reports_to', target='people',
              label_expr='(SELECT r.name FROM crm_contacts r WHERE r.id=p.reports_to AND r.is_active=1)'),
            F('notes', 'Notes', 'long_text', 'p.notes', editable=True, width=220),
            F('city', 'City', 'text', 'c.city', computed=True, width=130),
            F('open_opps', 'Open Deals', 'number',
              "(SELECT COUNT(*) FROM bd_opportunities x WHERE x.contact_id=p.id AND x.company_id=p.company_id "
              "AND x.is_active=1 AND x.stage NOT IN ('won','lost'))", computed=True, width=110),
            F('last_activity', 'Last Activity', 'datetime', _ACT_LAST.format(col='contact_id', ref='p.id'),
              computed=True, visible=True, width=150),
            F('created_by', 'Created by', 'user', 'p.created_by', computed=True),
            F('created_at', 'Creation date', 'datetime', 'p.created_at', computed=True, visible=True, width=150),
            F('updated_at', 'Last update', 'datetime', 'p.updated_at', computed=True, width=150),
        ],
    },

    # ── Opportunities ─────────────────────────────────────────────────────
    'opportunities': {
        'label': 'Opportunities', 'singular': 'Opportunity', 'title': 'name',
        'from': ('bd_opportunities o '
                 'JOIN crm_clients c ON c.id=o.client_id AND c.company_id=o.company_id '
                 'LEFT JOIN crm_contacts p ON p.id=o.contact_id AND p.company_id=o.company_id AND p.is_active=1'),
        'where': 'o.company_id=? AND o.is_active=1 AND c.is_active=1 AND COALESCE(c.is_internal,0)=0',
        'id_expr': 'o.id',
        'default_sort': [{'field': 'created_at', 'dir': 'desc'}],
        'fields': [
            F('name', 'Name', 'text', 'o.name', editable=True, searchable=True, visible=True, width=220, required=True),
            F('client_id', 'Company', 'relation', 'o.client_id', editable=True, target='companies',
              label_expr='c.name', visible=True, width=180, required=True),
            F('stage', 'Stage', 'select', 'o.stage', editable=True,
              options=_opt([(k, l) for k, l, _ in OPP_STAGES]), visible=True, width=170),
            F('amount', 'Amount', 'currency', 'o.amount', editable=True, visible=True, width=130),
            F('probability', 'Probability', 'percent', 'o.probability', editable=True, visible=True, width=110),
            F('weighted_amount', 'Weighted Amount', 'currency',
              'ROUND(COALESCE(o.amount,0)*COALESCE(o.probability,0)/100.0, 2)', computed=True, width=150),
            F('close_date', 'Close Date', 'date', 'o.close_date', editable=True, visible=True, width=130),
            F('owner_user_id', 'Owner', 'user', 'o.owner_user_id', editable=True, visible=True),
            F('contact_id', 'Point of Contact', 'relation', 'o.contact_id', editable=True, target='people',
              label_expr='p.name', visible=True, width=170),
            F('source', 'Source', 'select', 'o.source', editable=True, options=_opt(OPP_SOURCES), width=130),
            F('lost_reason', 'Lost Reason', 'text', 'o.lost_reason', editable=True, width=180),
            F('description', 'Description', 'long_text', 'o.description', editable=True, searchable=True, width=220),
            F('currency', 'Currency', 'text', 'o.currency', width=90),
            F('mandate_id', 'Job', 'relation', 'o.mandate_id', target='jobs',
              label_expr="(SELECT m.role FROM mandates m WHERE m.id=o.mandate_id AND m.owner_id=o.company_id)"),
            F('days_in_stage', 'Days in Stage', 'number',
              "CASE WHEN COALESCE(o.stage_changed_at,'')='' THEN NULL ELSE "
              "CAST(julianday({NOW}) - julianday(o.stage_changed_at) AS INTEGER) END",
              computed=True, width=120),
            F('stage_changed_at', 'Stage Changed', 'datetime', 'o.stage_changed_at', computed=True, width=150),
            F('won_at', 'Won On', 'datetime', 'o.won_at', computed=True, width=150),
            F('lost_at', 'Lost On', 'datetime', 'o.lost_at', computed=True, width=150),
            F('created_by', 'Created by', 'user', 'o.created_by', computed=True),
            F('created_at', 'Creation date', 'datetime', 'o.created_at', computed=True, visible=True, width=150),
            F('updated_at', 'Last update', 'datetime', 'o.updated_at', computed=True, width=150),
        ],
    },

    # ── Tasks ─────────────────────────────────────────────────────────────
    'tasks': {
        'label': 'Tasks', 'singular': 'Task', 'title': 'subject',
        'types': [k for k, _ in TASK_TYPES],
        'from': ('crm_activities a '
                 'JOIN crm_clients c ON c.id=a.client_id AND c.company_id=a.company_id '
                 'LEFT JOIN crm_contacts p ON p.id=a.contact_id AND p.company_id=a.company_id AND p.is_active=1 '
                 'LEFT JOIN bd_opportunities o ON o.id=a.opportunity_id AND o.company_id=a.company_id AND o.is_active=1'),
        'where': ("a.company_id=? AND a.is_active=1 AND c.is_active=1 AND COALESCE(c.is_internal,0)=0 "
                  "AND a.activity_type IN ('task','followup','call','meeting')"),
        'id_expr': 'a.id',
        'default_sort': [{'field': 'due_at', 'dir': 'asc'}],
        'fields': [
            F('subject', 'Title', 'text', 'a.subject', editable=True, searchable=True, visible=True, width=240),
            F('activity_type', 'Type', 'select', 'a.activity_type', editable=True,
              options=_opt(TASK_TYPES), visible=True, width=120),
            F('status', 'Status', 'select', 'a.status', editable=True, options=_opt(TASK_STATUSES), visible=True, width=110),
            F('due_at', 'Due', 'datetime', 'a.due_at', editable=True, visible=True, width=150),
            F('owner_user_id', 'Assignee', 'user', 'a.owner_user_id', editable=True, visible=True),
            F('client_id', 'Company', 'relation', 'a.client_id', editable=True, target='companies',
              label_expr='c.name', visible=True, width=170),
            F('contact_id', 'Person', 'relation', 'a.contact_id', editable=True, target='people', label_expr='p.name'),
            F('opportunity_id', 'Opportunity', 'relation', 'a.opportunity_id', editable=True,
              target='opportunities', label_expr='o.name', visible=True, width=170),
            F('body', 'Details', 'long_text', 'a.body', editable=True, searchable=True, width=240),
            F('outcome', 'Outcome', 'text', 'a.outcome', editable=True, width=200),
            F('is_overdue', 'Overdue', 'bool',
              "(a.status='open' AND COALESCE(a.due_at,'')!='' AND a.due_at<{NOW})", computed=True, width=100),
            F('completed_at', 'Completed', 'datetime', 'a.completed_at', computed=True, width=150),
            F('created_by', 'Created by', 'user', 'a.created_by', computed=True),
            F('created_at', 'Creation date', 'datetime', 'a.created_at', computed=True, width=150),
            F('updated_at', 'Last update', 'datetime', 'a.updated_at', computed=True, width=150),
        ],
    },

    # ── Notes ─────────────────────────────────────────────────────────────
    'notes': {
        'label': 'Notes', 'singular': 'Note', 'title': 'subject',
        'types': ['note'],
        'from': ('crm_activities a '
                 'JOIN crm_clients c ON c.id=a.client_id AND c.company_id=a.company_id '
                 'LEFT JOIN crm_contacts p ON p.id=a.contact_id AND p.company_id=a.company_id AND p.is_active=1 '
                 'LEFT JOIN bd_opportunities o ON o.id=a.opportunity_id AND o.company_id=a.company_id AND o.is_active=1'),
        'where': ("a.company_id=? AND a.is_active=1 AND c.is_active=1 AND COALESCE(c.is_internal,0)=0 "
                  "AND a.activity_type='note'"),
        'id_expr': 'a.id',
        'default_sort': [{'field': 'created_at', 'dir': 'desc'}],
        'fields': [
            F('subject', 'Title', 'text', 'a.subject', editable=True, searchable=True, visible=True, width=240),
            F('body', 'Body', 'long_text', 'a.body', editable=True, searchable=True, visible=True, width=320),
            F('client_id', 'Company', 'relation', 'a.client_id', editable=True, target='companies',
              label_expr='c.name', visible=True, width=170),
            F('contact_id', 'Person', 'relation', 'a.contact_id', editable=True, target='people', label_expr='p.name'),
            F('opportunity_id', 'Opportunity', 'relation', 'a.opportunity_id', editable=True,
              target='opportunities', label_expr='o.name', width=170),
            F('created_by', 'Created by', 'user', 'a.created_by', computed=True, visible=True),
            F('created_at', 'Creation date', 'datetime', 'a.created_at', computed=True, visible=True, width=150),
            F('updated_at', 'Last update', 'datetime', 'a.updated_at', computed=True, width=150),
        ],
    },
}

# key -> field lookup per object (built once)
for _o in OBJECTS.values():
    _o['by_key'] = {f['key']: f for f in _o['fields']}


def _obj(name):
    o = OBJECTS.get(name)
    if not o:
        raise RecordError('Unknown object.', 404)
    return o


def _field(o, key):
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise RecordError('Invalid field.')
    f = o['by_key'].get(key)
    if not f:
        raise RecordError(f'Unknown field "{key}".')
    return f


# ══════════════════════════════════════════════════════════════════════════
#  CONTEXT HELPERS
# ══════════════════════════════════════════════════════════════════════════
def _core():
    from modules.shared import _core as c
    return c()


def _now_iso():
    """IST 'now' (same clock as ts()), validated so it is safe to inline."""
    s = ts()[:19]
    if not _NOW_RE.match(s):
        s = datetime.datetime.now().isoformat(timespec='seconds')[:19]
    return s


def _today():
    return _now_iso()[:10]


def _sql(expr, now_iso):
    return expr.replace('{NOW}', "'" + now_iso + "'")


def _members(conn, company_id):
    rows = conn.execute(
        "SELECT id, COALESCE(NULLIF(display_name,''), username) AS name FROM users "
        "WHERE company_id=? AND COALESCE(status,'approved')='approved' ORDER BY name",
        (company_id,)).fetchall()
    return [{'id': r['id'], 'name': r['name']} for r in rows]


def _valid_member(conn, company_id, uid):
    if not uid:
        return True
    r = conn.execute('SELECT id FROM users WHERE id=? AND company_id=?', (uid, company_id)).fetchone()
    return bool(r)


def _guard():
    """Freelancers never see BD data; Corporate tenants don't have BD at all."""
    try:
        from modules.freelancer import _is_freelancer
        if _is_freelancer():
            return jsonify({'error': 'Freelancers cannot access Business Development'}), 403
    except Exception:
        pass
    try:
        if _core().workspace_mode() == 'corporate':
            return jsonify({'error': 'Not found'}), 404
    except Exception:
        pass
    return None


@bp.before_request
def _before():
    if not session.get('user_id'):
        return jsonify({'error': 'auth_required'}), 401
    return _guard()


def _err(e):
    body = {'error': e.message}
    body.update(getattr(e, 'extra', {}) or {})
    if getattr(e, 'existing_id', None):
        body['existing_id'] = e.existing_id
    return jsonify(body), getattr(e, 'code', 400)


# ══════════════════════════════════════════════════════════════════════════
#  QUERY COMPILER  (filter / sort / search / calculate)
# ══════════════════════════════════════════════════════════════════════════
def _empty_sql(f, e):
    fam = _family(f['type'])
    if fam in ('user', 'relation'):
        return f'COALESCE({e},0)=0'
    if fam == 'number':
        return f'{e} IS NULL'
    if fam == 'bool':
        return f'COALESCE({e},0)=0'
    return f"({e} IS NULL OR TRIM(CAST({e} AS TEXT))='')"


def _like_escape(s):
    return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _num(v):
    try:
        if isinstance(v, bool):
            raise ValueError
        return float(v)
    except Exception:
        raise RecordError('Number expected in filter.')


def _date(v):
    s = str(v or '')[:10]
    if not _DATE_RE.match(s):
        raise RecordError('Date expected in filter (YYYY-MM-DD).')
    return s


def _id_list(v):
    if not isinstance(v, list):
        v = [v]
    out = []
    for x in v[:200]:
        try:
            out.append(int(x))
        except Exception:
            raise RecordError('Id list expected in filter.')
    return out


def _compile_condition(o, cond, params, ctx):
    f = _field(o, cond.get('field'))
    fam = _family(f['type'])
    op = cond.get('op')
    if op not in OPS_BY_TYPE[fam]:
        raise RecordError(f'Operator "{op}" not allowed on {f["label"]}.')
    val = cond.get('value')
    e = _sql(f['expr'], ctx['now'])

    if op == 'is_empty':
        return _empty_sql(f, e)
    if op == 'is_not_empty':
        return f'NOT ({_empty_sql(f, e)})'

    if fam == 'text':
        s = str(val if val is not None else '').strip().lower()
        if op == 'contains':
            params.append('%' + _like_escape(s) + '%')
            return f"LOWER(COALESCE({e},'')) LIKE ? ESCAPE '\\'"
        if op == 'not_contains':
            params.append('%' + _like_escape(s) + '%')
            return f"LOWER(COALESCE({e},'')) NOT LIKE ? ESCAPE '\\'"
        if op == 'starts_with':
            params.append(_like_escape(s) + '%')
            return f"LOWER(COALESCE({e},'')) LIKE ? ESCAPE '\\'"
        if op == 'eq':
            params.append(s)
            return f"LOWER(TRIM(COALESCE({e},'')))=?"
        if op == 'neq':
            params.append(s)
            return f"LOWER(TRIM(COALESCE({e},'')))!=?"

    if fam == 'number':
        if op == 'between':
            if not isinstance(val, list) or len(val) != 2:
                raise RecordError('between needs [min, max].')
            params.extend([_num(val[0]), _num(val[1])])
            return f'({e} IS NOT NULL AND {e} BETWEEN ? AND ?)'
        sym = {'eq': '=', 'neq': '!=', 'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<='}[op]
        params.append(_num(val))
        return f'({e} IS NOT NULL AND {e} {sym} ?)'

    if fam == 'date':
        d = f"substr(COALESCE({e},''),1,10)"
        nonempty = f"COALESCE({e},'')!=''"
        today = ctx['now'][:10]
        if op == 'is_today':
            params.append(today)
            return f'{d}=?'
        if op == 'is_past':
            params.append(ctx['now'] if f['type'] == 'datetime' else today)
            col = e if f['type'] == 'datetime' else d
            return f'({nonempty} AND {col}<?)'
        if op == 'is_future':
            params.append(ctx['now'] if f['type'] == 'datetime' else today)
            col = e if f['type'] == 'datetime' else d
            return f'({nonempty} AND {col}>?)'
        if op in ('in_last_days', 'in_next_days'):
            try:
                n = max(0, min(3650, int(val)))
            except Exception:
                raise RecordError('Number of days expected.')
            base = datetime.date.fromisoformat(today)
            other = (base - datetime.timedelta(days=n)) if op == 'in_last_days' else (base + datetime.timedelta(days=n))
            lo, hi = (other.isoformat(), today) if op == 'in_last_days' else (today, other.isoformat())
            params.extend([lo, hi])
            return f'({nonempty} AND {d} BETWEEN ? AND ?)'
        if op == 'between':
            if not isinstance(val, list) or len(val) != 2:
                raise RecordError('between needs [from, to].')
            params.extend([_date(val[0]), _date(val[1])])
            return f'({nonempty} AND {d} BETWEEN ? AND ?)'
        sym = {'is': '=', 'before': '<', 'after': '>', 'on_or_before': '<=', 'on_or_after': '>='}[op]
        params.append(_date(val))
        return f'({nonempty} AND {d} {sym} ?)'

    if fam == 'select':
        vals = val if isinstance(val, list) else [val]
        vals = [str(v) for v in vals if v is not None][:50]
        if not vals:
            return '1=1' if op == 'not_in' else '1=0'
        params.extend(vals)
        ph = ','.join('?' * len(vals))
        if op == 'in':
            return f"COALESCE({e},'') IN ({ph})"
        return f"COALESCE({e},'') NOT IN ({ph})"

    if fam == 'bool':
        return f'COALESCE({e},0)!=0' if op == 'is_true' else f'COALESCE({e},0)=0'

    if fam in ('user', 'relation'):
        if op == 'is_me':
            params.append(ctx['me'])
            return f'{e}=?'
        if op == 'label_contains':
            s = str(val or '').strip().lower()
            params.append('%' + _like_escape(s) + '%')
            return f"LOWER(COALESCE({f['label_expr']},'')) LIKE ? ESCAPE '\\'"
        ids = _id_list(val)
        if not ids:
            return '1=1' if op == 'not_in' else '1=0'
        params.extend(ids)
        ph = ','.join('?' * len(ids))
        return f'COALESCE({e},0) IN ({ph})' if op == 'in' else f'COALESCE({e},0) NOT IN ({ph})'

    raise RecordError('Unsupported filter.')


def _compile_filter(o, node, params, ctx, depth=0, counter=None):
    """node = {"logic": "and"|"or", "conditions": [cond | node, ...]}
    or a bare list (treated as AND)."""
    if counter is None:
        counter = [0]
    if node is None or node == {} or node == []:
        return ''
    if depth > MAX_FILTER_DEPTH:
        raise RecordError('Filter nested too deeply.')
    if isinstance(node, list):
        node = {'logic': 'and', 'conditions': node}
    if not isinstance(node, dict):
        raise RecordError('Invalid filter.')
    if 'field' in node:  # a single condition
        counter[0] += 1
        if counter[0] > MAX_CONDITIONS:
            raise RecordError('Too many filter conditions.')
        return _compile_condition(o, node, params, ctx)
    logic = (node.get('logic') or 'and').lower()
    if logic not in ('and', 'or'):
        raise RecordError('Filter logic must be "and" or "or".')
    parts = []
    for child in node.get('conditions') or []:
        sql = _compile_filter(o, child, params, ctx, depth + 1, counter)
        if sql:
            parts.append('(' + sql + ')')
    if not parts:
        return ''
    return (' AND ' if logic == 'and' else ' OR ').join(parts)


def _compile_sort(o, sort, ctx):
    sort = sort or o['default_sort']
    if not isinstance(sort, list):
        raise RecordError('Invalid sort.')
    out = []
    for s in sort[:MAX_SORTS]:
        f = _field(o, (s or {}).get('field'))
        d = 'DESC' if str((s or {}).get('dir', 'asc')).lower() == 'desc' else 'ASC'
        e = _sql(f['label_expr'] if f['type'] in ('user', 'relation') else f['expr'], ctx['now'])
        if f['type'] in ('user', 'relation'):
            empty = f"({e} IS NULL OR {e}='')"
        else:
            empty = _empty_sql(f, e)
        coll = ' COLLATE NOCASE' if _family(f['type']) in ('text', 'select', 'user', 'relation') else ''
        out.append(f'CASE WHEN {empty} THEN 1 ELSE 0 END, {e}{coll} {d}')
    out.append(f"{o['id_expr']} DESC")
    return ', '.join(out)


def _where(o, company_id, spec, ctx):
    params = [company_id]
    clauses = [o['where']]
    fsql = _compile_filter(o, spec.get('filter'), params, ctx)
    if fsql:
        clauses.append('(' + fsql + ')')
    q = str(spec.get('q') or '').strip().lower()[:100]
    if q:
        cols = [f for f in o['fields'] if f['searchable']]
        like = '%' + _like_escape(q) + '%'
        parts = []
        for f in cols:
            parts.append(f"LOWER(COALESCE({f['expr']},'')) LIKE ? ESCAPE '\\'")
            params.append(like)
        if parts:
            clauses.append('(' + ' OR '.join(parts) + ')')
    return ' AND '.join(clauses), params


def _select_list(o, ctx):
    cols = [f"{o['id_expr']} AS id"]
    for f in o['fields']:
        cols.append(f"{_sql(f['expr'], ctx['now'])} AS \"{f['key']}\"")
        if f['type'] in ('user', 'relation'):
            cols.append(f"{_sql(f['label_expr'], ctx['now'])} AS \"{f['key']}__label\"")
    return ', '.join(cols)


def _row_out(o, row):
    d = dict(row)
    out = {'id': d['id']}
    for f in o['fields']:
        v = d.get(f['key'])
        if f['type'] == 'bool':
            v = bool(v)
        elif f['type'] == 'tags':
            try:
                v = json.loads(v) if v else []
            except Exception:
                v = []
        out[f['key']] = v
        if f['type'] in ('user', 'relation'):
            out[f['key'] + '_label'] = d.get(f['key'] + '__label') or ''
    return out


def _parse_calc(o, calc):
    """calc = {"field": "fn"} or [{"field":..,"fn":..}] -> [(field, fn)]"""
    if not calc:
        return []
    if isinstance(calc, dict):
        calc = [{'field': k, 'fn': v} for k, v in calc.items()]
    if not isinstance(calc, list):
        raise RecordError('Invalid calc.')
    out = []
    for c in calc[:40]:
        f = _field(o, (c or {}).get('field'))
        fn = (c or {}).get('fn')
        if fn not in CALC_BY_TYPE[_family(f['type'])]:
            raise RecordError(f'Calculation "{fn}" not allowed on {f["label"]}.')
        out.append((f, fn))
    return out


def _calc_exprs(pairs, ctx):
    exprs = []
    for f, fn in pairs:
        e = _sql(f['expr'], ctx['now'])
        empty = _empty_sql(f, e)
        if fn in ('count_empty', 'pct_empty'):
            exprs.append(f'SUM(CASE WHEN {empty} THEN 1 ELSE 0 END)')
        elif fn in ('count_not_empty', 'pct_not_empty'):
            exprs.append(f'SUM(CASE WHEN {empty} THEN 0 ELSE 1 END)')
        elif fn == 'count_unique':
            exprs.append(f'COUNT(DISTINCT CASE WHEN {empty} THEN NULL ELSE {e} END)')
        elif fn in ('sum', 'avg', 'min', 'max'):
            exprs.append(f'{fn.upper()}({e})')
        elif fn == 'earliest':
            exprs.append(f'MIN(CASE WHEN {empty} THEN NULL ELSE {e} END)')
        elif fn == 'latest':
            exprs.append(f'MAX(CASE WHEN {empty} THEN NULL ELSE {e} END)')
        elif fn in ('count_true', 'pct_true'):
            exprs.append(f'SUM(CASE WHEN COALESCE({e},0)!=0 THEN 1 ELSE 0 END)')
        elif fn == 'count_false':
            exprs.append(f'SUM(CASE WHEN COALESCE({e},0)!=0 THEN 0 ELSE 1 END)')
    return exprs


def query_records(conn, obj_name, spec, company_id=None, me=None):
    o = _obj(obj_name)
    company_id = company_id if company_id is not None else effective_company_id()
    ctx = {'now': _now_iso(), 'me': me if me is not None else real_user_id()}

    try:
        page = max(1, int(spec.get('page') or 1))
        per = max(1, min(MAX_PER_PAGE, int(spec.get('per_page') or 50)))
    except Exception:
        raise RecordError('Invalid paging.')

    where, params = _where(o, company_id, spec, ctx)
    order = _compile_sort(o, spec.get('sort'), ctx)
    calc_pairs = _parse_calc(o, spec.get('calc'))

    agg = ['COUNT(*)'] + _calc_exprs(calc_pairs, ctx)
    arow = conn.execute(f"SELECT {', '.join(agg)} FROM {o['from']} WHERE {where}", params).fetchone()
    total = arow[0] or 0
    calc_out = {}
    for i, (f, fn) in enumerate(calc_pairs, start=1):
        v = arow[i]
        if fn.startswith('pct_'):
            v = round(100.0 * (v or 0) / total, 1) if total else 0
        elif fn == 'avg' and v is not None:
            v = round(v, 2)
        calc_out[f['key']] = {'fn': fn, 'value': v}

    sql = (f"SELECT {_select_list(o, ctx)} FROM {o['from']} WHERE {where} "
           f"ORDER BY {order} LIMIT ? OFFSET ?")
    rows = conn.execute(sql, params + [per, (page - 1) * per]).fetchall()
    return {
        'object': obj_name,
        'records': [_row_out(o, r) for r in rows],
        'total': total, 'page': page, 'per_page': per,
        'pages': (total + per - 1) // per if per else 0,
        'calc': calc_out,
    }


def get_record(conn, obj_name, rid, company_id=None):
    o = _obj(obj_name)
    company_id = company_id if company_id is not None else effective_company_id()
    ctx = {'now': _now_iso(), 'me': real_user_id()}
    row = conn.execute(
        f"SELECT {_select_list(o, ctx)} FROM {o['from']} WHERE {o['where']} AND {o['id_expr']}=?",
        (company_id, rid)).fetchone()
    return _row_out(o, row) if row else None


def group_records(conn, obj_name, spec, company_id=None):
    """Counts (and optional sum) per value of a select / user / relation / bool
    field — powers Kanban columns and pipeline summaries."""
    o = _obj(obj_name)
    company_id = company_id if company_id is not None else effective_company_id()
    ctx = {'now': _now_iso(), 'me': real_user_id()}
    gf = _field(o, spec.get('group_by'))
    if _family(gf['type']) not in ('select', 'user', 'relation', 'bool'):
        raise RecordError('Group by needs a select, user, relation or yes/no field.')
    sf = None
    if spec.get('sum_field'):
        sf = _field(o, spec.get('sum_field'))
        if _family(sf['type']) != 'number':
            raise RecordError('sum_field must be a number field.')

    where, params = _where(o, company_id, spec, ctx)
    ge = _sql(gf['expr'], ctx['now'])
    if _family(gf['type']) in ('user', 'relation', 'bool'):
        gkey = f'COALESCE({ge},0)'
    else:
        gkey = f"COALESCE({ge},'')"
    lab = _sql(gf['label_expr'], ctx['now']) if gf['label_expr'] else "''"
    ssql = f", SUM({_sql(sf['expr'], ctx['now'])})" if sf else ', NULL'
    rows = conn.execute(
        f"SELECT {gkey} AS g, MAX({lab}) AS lbl, COUNT(*) AS n{ssql} AS s "
        f"FROM {o['from']} WHERE {where} GROUP BY {gkey}", params).fetchall()
    found = {r['g']: r for r in rows}

    groups = []
    if _family(gf['type']) == 'select':
        for opt in gf['options']:
            r = found.pop(opt['value'], None)
            groups.append({'value': opt['value'], 'label': opt['label'],
                           'count': r['n'] if r else 0, 'sum': (r['s'] or 0) if r else 0})
        for g, r in found.items():  # legacy / empty values
            groups.append({'value': g, 'label': g or 'No value', 'count': r['n'], 'sum': r['s'] or 0})
    elif _family(gf['type']) == 'bool':
        for v, l in ((1, 'Yes'), (0, 'No')):
            n = sum(r['n'] for g, r in found.items() if (1 if g else 0) == v)
            s = sum((r['s'] or 0) for g, r in found.items() if (1 if g else 0) == v)
            groups.append({'value': bool(v), 'label': l, 'count': n, 'sum': s})
    else:
        for g, r in sorted(found.items(), key=lambda kv: (kv[0] == 0, (kv[1]['lbl'] or '').lower())):
            groups.append({'value': g, 'label': r['lbl'] or ('Unassigned' if gf['type'] == 'user' else 'No value'),
                           'count': r['n'], 'sum': r['s'] or 0})
    if not sf:
        for g in groups:
            g.pop('sum', None)
    return {'object': obj_name, 'group_by': gf['key'], 'sum_field': sf['key'] if sf else None,
            'groups': groups, 'total': sum(g['count'] for g in groups)}


# ══════════════════════════════════════════════════════════════════════════
#  VALUE NORMALISERS (for writes)
# ══════════════════════════════════════════════════════════════════════════
def _norm_domain(v):
    s = str(v or '').strip().lower()
    s = re.sub(r'^[a-z]+://', '', s)
    s = s.split('/')[0].split('?')[0].split('#')[0]
    if s.startswith('www.'):
        s = s[4:]
    if s and not re.match(r'^[a-z0-9.-]+\.[a-z]{2,}$', s):
        raise RecordError('Domain looks invalid (example: insightcosmetics.in).')
    return s


def _opt_int(v, label, lo=0, hi=None):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        n = int(float(v))
    except Exception:
        raise RecordError(f'{label} must be a number.')
    if n < lo or (hi is not None and n > hi):
        raise RecordError(f'{label} is out of range.')
    return n


def _opt_money(v, label):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        n = float(str(v).replace(',', '').strip())
    except Exception:
        raise RecordError(f'{label} must be a number.')
    if n < 0:
        raise RecordError(f'{label} cannot be negative.')
    return round(n, 2)


def _opt_date(v, label):
    s = str(v or '').strip()[:10]
    if not s:
        return ''
    if not _DATE_RE.match(s):
        raise RecordError(f'{label} must be YYYY-MM-DD.')
    try:
        datetime.date.fromisoformat(s)
    except Exception:
        raise RecordError(f'{label} is not a real date.')
    return s


def _client_row(conn, company_id, cid):
    return conn.execute(
        'SELECT * FROM crm_clients WHERE id=? AND company_id=? AND is_active=1 AND COALESCE(is_internal,0)=0',
        (cid, company_id)).fetchone()


def _contact_row(conn, company_id, pid):
    return conn.execute(
        'SELECT * FROM crm_contacts WHERE id=? AND company_id=? AND is_active=1',
        (pid, company_id)).fetchone()


def _opp_row(conn, company_id, oid):
    return conn.execute(
        'SELECT * FROM bd_opportunities WHERE id=? AND company_id=? AND is_active=1',
        (oid, company_id)).fetchone()


def _only_editable(o, payload, allow_extra=()):
    if not isinstance(payload, dict):
        raise RecordError('JSON object expected.')
    for k in payload:
        f = o['by_key'].get(k)
        if k in allow_extra:
            continue
        if not f:
            raise RecordError(f'Unknown field "{k}".')
        if not f['editable']:
            raise RecordError(f'"{f["label"]}" cannot be edited.')


# ══════════════════════════════════════════════════════════════════════════
#  COMPANIES  (delegates to crm.ClientService)
# ══════════════════════════════════════════════════════════════════════════
_COMPANY_EXTRA = ('domain', 'linkedin', 'employees', 'annual_revenue')


def _company_extra_values(payload):
    vals = {}
    if 'domain' in payload:
        vals['domain'] = _norm_domain(payload.get('domain'))
    if 'linkedin' in payload:
        vals['linkedin'] = str(payload.get('linkedin') or '').strip()
    if 'employees' in payload:
        vals['employees'] = _opt_int(payload.get('employees'), 'Employees')
    if 'annual_revenue' in payload:
        vals['annual_revenue'] = _opt_money(payload.get('annual_revenue'), 'Annual Revenue')
    return vals


def _save_company_extra(conn, cid, before, vals):
    if not vals:
        return
    conn.execute('UPDATE crm_clients SET ' + ', '.join(f'{k}=?' for k in vals)
                 + ', updated_by=?, updated_at=? WHERE id=?',
                 list(vals.values()) + [real_user_id(), ts(), cid])
    conn.commit()
    after = dict(before or {}); after.update(vals)
    changes = record_changes('client', cid, dict(before or {}), after, list(vals.keys()))
    if changes:
        log_activity('client.updated', 'Updated client "%s"' % after.get('name', ''),
                     entity_type='client', entity_id=cid, meta={'changes': changes})


def company_create(conn, payload):
    from modules.crm import ClientService
    if not is_company_admin():
        raise RecordError('Only an admin can add a new company.', 403)
    o = OBJECTS['companies']
    _only_editable(o, payload)
    company_id = effective_company_id()
    if 'owner_user_id' in payload and not _valid_member(conn, company_id, int(payload.get('owner_user_id') or 0)):
        raise RecordError('Owner must be a member of your team.')
    extra = _company_extra_values(payload)
    core = {k: v for k, v in payload.items() if k not in _COMPANY_EXTRA}
    if 'status' not in core:
        core['status'] = 'prospect'   # a company added from BD starts as a prospect
    cid = ClientService.create(conn, core)
    if extra:
        conn.execute('UPDATE crm_clients SET ' + ', '.join(f'{k}=?' for k in extra) + ' WHERE id=?',
                     list(extra.values()) + [cid])
        conn.commit()
    return cid


def company_update(conn, cid, payload):
    from modules.crm import ClientService
    o = OBJECTS['companies']
    _only_editable(o, payload)
    company_id = effective_company_id()
    before = _client_row(conn, company_id, cid)
    if not before:
        raise RecordError('Company not found.', 404)
    if 'owner_user_id' in payload and not _valid_member(conn, company_id, int(payload.get('owner_user_id') or 0)):
        raise RecordError('Owner must be a member of your team.')
    extra = _company_extra_values(payload)       # validate before any write
    core = {k: v for k, v in payload.items() if k not in _COMPANY_EXTRA}
    if core:
        ClientService.update(conn, cid, core)
        before = _client_row(conn, company_id, cid)
    _save_company_extra(conn, cid, before, extra)


def company_delete(conn, cid):
    from modules.crm import ClientService
    if not is_company_admin():
        raise RecordError('Only a company admin can delete companies.', 403)
    ClientService.delete(conn, cid)


# ══════════════════════════════════════════════════════════════════════════
#  PEOPLE  (delegates to crm.ContactService)
# ══════════════════════════════════════════════════════════════════════════
def person_create(conn, payload):
    from modules.crm import ContactService
    o = OBJECTS['people']
    _only_editable(o, payload)
    try:
        client_id = int(payload.get('client_id') or 0)
    except Exception:
        raise RecordError('Company is required.')
    if not client_id:
        raise RecordError('Company is required.')
    if not _client_row(conn, effective_company_id(), client_id):
        raise RecordError('Company not found.', 404)
    body = {k: v for k, v in payload.items() if k != 'client_id'}
    return ContactService.create(conn, client_id, body)


def person_update(conn, pid, payload):
    from modules.crm import ContactService
    o = OBJECTS['people']
    _only_editable(o, payload)
    company_id = effective_company_id()
    before = _contact_row(conn, company_id, pid)
    if not before:
        raise RecordError('Person not found.', 404)
    move_to = None
    if 'client_id' in payload:
        try:
            move_to = int(payload.get('client_id') or 0)
        except Exception:
            raise RecordError('Invalid company.')
        if not move_to:
            raise RecordError('A person must belong to a company.')
        if move_to == before['client_id']:
            move_to = None
        elif not _client_row(conn, company_id, move_to):
            raise RecordError('Company not found.', 404)
    core = {k: v for k, v in payload.items() if k != 'client_id'}
    if core:
        ContactService.update(conn, pid, core)
    if move_to:
        old_client = before['client_id']
        # Org-chart links are per company: drop this person's manager link and
        # anyone in the old company who reported to them. Primary flag resets.
        conn.execute('UPDATE crm_contacts SET client_id=?, reports_to=0, is_primary=0, '
                     'updated_by=?, updated_at=? WHERE id=?',
                     (move_to, real_user_id(), ts(), pid))
        conn.execute('UPDATE crm_contacts SET reports_to=0 WHERE reports_to=? AND company_id=?',
                     (pid, company_id))
        conn.commit()
        record_changes('contact', pid, {'client_id': old_client}, {'client_id': move_to}, ['client_id'])
        new_c = _client_row(conn, company_id, move_to)
        log_activity('contact.moved', f'Moved contact "{before["name"]}" to "{new_c["name"]}"',
                     entity_type='client', entity_id=move_to,
                     meta={'contact_id': pid, 'from_client_id': old_client})


def person_delete(conn, pid):
    from modules.crm import ContactService
    ContactService.delete(conn, pid)


# ══════════════════════════════════════════════════════════════════════════
#  OPPORTUNITIES  (new)
# ══════════════════════════════════════════════════════════════════════════
_OPP_AUDIT = ['name', 'client_id', 'contact_id', 'stage', 'amount', 'probability',
              'close_date', 'owner_user_id', 'source', 'lost_reason', 'description']


def _opp_values(conn, company_id, payload, current=None):
    """Validate an opportunity payload. Returns the column dict to write."""
    vals = {}
    client_id = current['client_id'] if current else 0
    if 'client_id' in payload or not current:
        try:
            client_id = int(payload.get('client_id') or 0)
        except Exception:
            raise RecordError('Invalid company.')
        if not client_id:
            raise RecordError('Company is required.')
        if not _client_row(conn, company_id, client_id):
            raise RecordError('Company not found.', 404)
        vals['client_id'] = client_id

    if 'contact_id' in payload:
        try:
            pid = int(payload.get('contact_id') or 0)
        except Exception:
            raise RecordError('Invalid point of contact.')
        if pid:
            p = _contact_row(conn, company_id, pid)
            if not p:
                raise RecordError('Point of contact not found.', 404)
            if p['client_id'] != client_id:
                raise RecordError('Point of contact must work at the same company.')
        vals['contact_id'] = pid
    elif current and 'client_id' in vals and vals['client_id'] != current['client_id'] and current['contact_id']:
        p = _contact_row(conn, company_id, current['contact_id'])
        if not p or p['client_id'] != vals['client_id']:
            vals['contact_id'] = 0     # old contact doesn't belong to the new company

    if 'name' in payload:
        nm = str(payload.get('name') or '').strip()
        if not nm:
            raise RecordError('Name cannot be empty.')
        vals['name'] = nm[:200]
    elif not current:
        cl = _client_row(conn, company_id, client_id)
        vals['name'] = f'{cl["name"]} deal'

    if 'stage' in payload or not current:
        st = str(payload.get('stage') or 'lead').strip().lower()
        if st not in OPP_STAGE_KEYS:
            raise RecordError('Invalid stage.')
        vals['stage'] = st
    if 'amount' in payload:
        vals['amount'] = _opt_money(payload.get('amount'), 'Amount')
    if 'probability' in payload:
        p = _opt_int(payload.get('probability'), 'Probability', 0, 100)
        vals['probability'] = 0 if p is None else p
    if 'close_date' in payload:
        vals['close_date'] = _opt_date(payload.get('close_date'), 'Close date')
    if 'owner_user_id' in payload:
        try:
            uid = int(payload.get('owner_user_id') or 0)
        except Exception:
            raise RecordError('Invalid owner.')
        if not _valid_member(conn, company_id, uid):
            raise RecordError('Owner must be a member of your team.')
        vals['owner_user_id'] = uid
    if 'source' in payload:
        src = str(payload.get('source') or '').strip().lower()
        if src and src not in dict(OPP_SOURCES):
            raise RecordError('Invalid source.')
        vals['source'] = src
    for k, lim in (('lost_reason', 500), ('description', 5000)):
        if k in payload:
            vals[k] = str(payload.get(k) or '').strip()[:lim]
    return vals


def _apply_stage_side_effects(vals, current, now, explicit_probability):
    """Stage bookkeeping: timestamps + default probability."""
    new_stage = vals.get('stage')
    if not new_stage:
        return False
    if current and current['stage'] == new_stage:
        vals.pop('stage', None)          # no real change -> no timestamps, no log
        return False
    vals['stage_changed_at'] = now
    vals['won_at'] = now if new_stage == 'won' else ''
    vals['lost_at'] = now if new_stage == 'lost' else ''
    if not explicit_probability:
        vals['probability'] = OPP_STAGE_PROB[new_stage]
    return True


def _on_won(conn, client_id):
    """Agreement signed -> the company becomes an active client."""
    from modules.crm import ClientService
    company_id = effective_company_id()
    cl = _client_row(conn, company_id, client_id)
    if cl and (cl['status'] or '') != 'active':
        ClientService.update(conn, client_id, {'status': 'active'})


def opportunity_create(conn, payload):
    o = OBJECTS['opportunities']
    _only_editable(o, payload)
    company_id = effective_company_id()
    actor = real_user_id()
    now = ts()
    vals = _opp_values(conn, company_id, payload)
    vals.setdefault('owner_user_id', actor)
    _apply_stage_side_effects(vals, None, now, 'probability' in payload)
    vals.update({'company_id': company_id, 'currency': 'INR', 'is_active': 1,
                 'created_by': actor, 'updated_by': actor, 'created_at': now, 'updated_at': now})
    cols = list(vals.keys())
    conn.execute(f'INSERT INTO bd_opportunities ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
                 [vals[c] for c in cols])
    oid = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    conn.commit()
    cl = _client_row(conn, company_id, vals['client_id'])
    log_activity('opportunity.created',
                 f'Opportunity "{vals["name"]}" created for "{cl["name"]}"',
                 entity_type='client', entity_id=vals['client_id'],
                 meta={'opportunity_id': oid, 'stage': vals['stage'], 'amount': vals.get('amount')})
    if vals['stage'] == 'won':
        _on_won(conn, vals['client_id'])
    return oid


def opportunity_update(conn, oid, payload):
    o = OBJECTS['opportunities']
    _only_editable(o, payload)
    company_id = effective_company_id()
    actor = real_user_id()
    now = ts()
    current = _opp_row(conn, company_id, oid)
    if not current:
        raise RecordError('Opportunity not found.', 404)
    vals = _opp_values(conn, company_id, payload, current)
    stage_changed = _apply_stage_side_effects(vals, current, now, 'probability' in payload)
    if not vals:
        return
    conn.execute('UPDATE bd_opportunities SET ' + ', '.join(f'{k}=?' for k in vals)
                 + ', updated_by=?, updated_at=? WHERE id=?',
                 list(vals.values()) + [actor, now, oid])
    conn.commit()
    before = dict(current)
    after = dict(before); after.update(vals)
    changes = record_changes('opportunity', oid, before, after, [f for f in _OPP_AUDIT if f in vals])
    labels = {k: l for k, l, _ in OPP_STAGES}
    if stage_changed:
        log_activity('opportunity.stage_changed',
                     f'"{after["name"]}": {labels.get(before["stage"], before["stage"])} '
                     f'\u2192 {labels.get(after["stage"], after["stage"])}',
                     entity_type='client', entity_id=after['client_id'],
                     meta={'opportunity_id': oid, 'from': before['stage'], 'to': after['stage']})
    elif changes:
        log_activity('opportunity.updated', f'Opportunity "{after["name"]}" updated',
                     entity_type='client', entity_id=after['client_id'],
                     meta={'opportunity_id': oid, 'changes': changes})
    if stage_changed and after['stage'] == 'won':
        _on_won(conn, after['client_id'])


def opportunity_delete(conn, oid):
    company_id = effective_company_id()
    current = _opp_row(conn, company_id, oid)
    if not current:
        raise RecordError('Opportunity not found.', 404)
    conn.execute('UPDATE bd_opportunities SET is_active=0, updated_by=?, updated_at=? WHERE id=?',
                 (real_user_id(), ts(), oid))
    conn.commit()
    log_activity('opportunity.deleted', f'Opportunity "{current["name"]}" deleted',
                 entity_type='client', entity_id=current['client_id'],
                 meta={'opportunity_id': oid})


# ══════════════════════════════════════════════════════════════════════════
#  TASKS / NOTES  (delegates to bd.ActivityService)
# ══════════════════════════════════════════════════════════════════════════
def _activity_links(conn, company_id, payload, current=None):
    """Resolve + validate client / contact / opportunity links. An opportunity
    or person implies its company when the company isn't given."""
    def _int(k):
        try:
            return int(payload.get(k) or 0)
        except Exception:
            raise RecordError(f'Invalid {k}.')

    client_id = current['client_id'] if current else 0
    contact_id = current['contact_id'] if current else 0
    opp_id = (current['opportunity_id'] or 0) if current else 0
    if 'opportunity_id' in payload:
        opp_id = _int('opportunity_id')
    if 'contact_id' in payload:
        contact_id = _int('contact_id')
    if 'client_id' in payload:
        client_id = _int('client_id')

    opp = _opp_row(conn, company_id, opp_id) if opp_id else None
    if opp_id and not opp:
        raise RecordError('Opportunity not found.', 404)
    person = _contact_row(conn, company_id, contact_id) if contact_id else None
    if contact_id and not person:
        raise RecordError('Person not found.', 404)

    if 'client_id' not in payload:
        if opp and ('opportunity_id' in payload):
            client_id = opp['client_id']
        elif person and ('contact_id' in payload):
            client_id = person['client_id']
    if not client_id:
        raise RecordError('Company is required.')
    if not _client_row(conn, company_id, client_id):
        raise RecordError('Company not found.', 404)
    if opp and opp['client_id'] != client_id:
        if 'opportunity_id' in payload:
            raise RecordError('Opportunity belongs to a different company.')
        opp_id = 0            # company changed -> old deal link no longer valid
    if person and person['client_id'] != client_id:
        if 'contact_id' in payload:
            raise RecordError('Person works at a different company.')
        contact_id = 0
    return client_id, contact_id, opp_id


def activity_create(conn, obj_name, payload):
    from modules.bd import ActivityService, ValidationError as BdVE
    o = OBJECTS[obj_name]
    _only_editable(o, payload)
    company_id = effective_company_id()
    atype = str(payload.get('activity_type') or o['types'][0]).strip().lower()
    if atype not in o['types']:
        raise RecordError('Invalid type.')
    client_id, contact_id, opp_id = _activity_links(conn, company_id, payload)
    if 'owner_user_id' in payload:
        uid = int(payload.get('owner_user_id') or 0)
        if not _valid_member(conn, company_id, uid):
            raise RecordError('Assignee must be a member of your team.')
    body = {k: v for k, v in payload.items() if k in ('subject', 'body', 'outcome', 'due_at', 'owner_user_id')}
    body.update({'activity_type': atype, 'client_id': client_id, 'contact_id': contact_id})
    try:
        aid = ActivityService.create(conn, body)
    except BdVE as e:
        raise RecordError(e.message, e.code)
    extra = {}
    if opp_id:
        extra['opportunity_id'] = opp_id
    if obj_name == 'tasks' and payload.get('status') in ('done', 'cancelled'):
        extra['status'] = payload['status']
        extra['completed_at'] = ts() if payload['status'] == 'done' else ''
    if extra:
        conn.execute('UPDATE crm_activities SET ' + ', '.join(f'{k}=?' for k in extra) + ' WHERE id=?',
                     list(extra.values()) + [aid])
        conn.commit()
    return aid


def _activity_row(conn, company_id, aid, types):
    ph = ','.join('?' * len(types))
    return conn.execute(
        f'SELECT * FROM crm_activities WHERE id=? AND company_id=? AND is_active=1 AND activity_type IN ({ph})',
        [aid, company_id] + list(types)).fetchone()


def activity_update(conn, obj_name, aid, payload):
    from modules.bd import ActivityService, ValidationError as BdVE
    o = OBJECTS[obj_name]
    _only_editable(o, payload)
    company_id = effective_company_id()
    current = _activity_row(conn, company_id, aid, o['types'])
    if not current:
        raise RecordError(f'{o["singular"]} not found.', 404)
    if 'owner_user_id' in payload:
        uid = int(payload.get('owner_user_id') or 0)
        if not _valid_member(conn, company_id, uid):
            raise RecordError('Assignee must be a member of your team.')
    extra = {}
    if 'activity_type' in payload:
        at = str(payload.get('activity_type') or '').strip().lower()
        if at not in o['types']:
            raise RecordError('Invalid type.')
        if at != current['activity_type']:
            extra['activity_type'] = at
    if any(k in payload for k in ('client_id', 'contact_id', 'opportunity_id')):
        cl, ct, op = _activity_links(conn, company_id, payload, current)
        for k, v in (('client_id', cl), ('contact_id', ct), ('opportunity_id', op)):
            if v != (current[k] or 0):
                extra[k] = v
    core = {k: v for k, v in payload.items()
            if k in ('subject', 'body', 'outcome', 'due_at', 'status', 'owner_user_id')}
    try:
        if core:
            ActivityService.update(conn, aid, core)
    except BdVE as e:
        raise RecordError(e.message, e.code)
    if extra:
        conn.execute('UPDATE crm_activities SET ' + ', '.join(f'{k}=?' for k in extra)
                     + ', updated_by=?, updated_at=? WHERE id=?',
                     list(extra.values()) + [real_user_id(), ts(), aid])
        conn.commit()
        record_changes('activity', aid, dict(current), dict(current, **extra), list(extra.keys()))


def activity_delete(conn, obj_name, aid):
    from modules.bd import ActivityService, ValidationError as BdVE
    o = OBJECTS[obj_name]
    if not _activity_row(conn, effective_company_id(), aid, o['types']):
        raise RecordError(f'{o["singular"]} not found.', 404)
    try:
        ActivityService.delete(conn, aid)
    except BdVE as e:
        raise RecordError(e.message, e.code)


# ── dispatch tables ────────────────────────────────────────────────────────
def _create(conn, obj, payload):
    if obj == 'companies':
        return company_create(conn, payload)
    if obj == 'people':
        return person_create(conn, payload)
    if obj == 'opportunities':
        return opportunity_create(conn, payload)
    return activity_create(conn, obj, payload)


def _update(conn, obj, rid, payload):
    if obj == 'companies':
        return company_update(conn, rid, payload)
    if obj == 'people':
        return person_update(conn, rid, payload)
    if obj == 'opportunities':
        return opportunity_update(conn, rid, payload)
    return activity_update(conn, obj, rid, payload)


def _delete(conn, obj, rid):
    if obj == 'companies':
        return company_delete(conn, rid)
    if obj == 'people':
        return person_delete(conn, rid)
    if obj == 'opportunities':
        return opportunity_delete(conn, rid)
    return activity_delete(conn, obj, rid)


def _run_write(fn):
    """Translate the three services' error classes into one JSON shape."""
    from modules.crm import ValidationError as CrmVE
    from modules.bd import ValidationError as BdVE
    try:
        return fn(), None
    except RecordError as e:
        return None, _err(e)
    except CrmVE as e:
        return None, _err(e)
    except BdVE as e:
        return None, _err(e)


# ══════════════════════════════════════════════════════════════════════════
#  VIEWS  (saved table / kanban configurations)
# ══════════════════════════════════════════════════════════════════════════
def _clean_view_config(o, cfg):
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        raise RecordError('config must be an object.')
    out = {}
    vt = cfg.get('view_type', 'table')
    if vt not in ('table', 'kanban'):
        raise RecordError('view_type must be table or kanban.')
    out['view_type'] = vt
    cols = []
    for c in (cfg.get('columns') or [])[:60]:
        if isinstance(c, str):
            c = {'field': c}
        f = _field(o, (c or {}).get('field'))
        w = c.get('width')
        try:
            w = max(60, min(800, int(w))) if w is not None else f['width']
        except Exception:
            w = f['width']
        cols.append({'field': f['key'], 'width': w, 'visible': bool(c.get('visible', True))})
    out['columns'] = cols
    # validate filter/sort/calc by compiling them once
    ctx = {'now': _now_iso(), 'me': 0}
    _compile_filter(o, cfg.get('filter'), [], ctx)
    out['filter'] = cfg.get('filter') or None
    if cfg.get('sort'):
        _compile_sort(o, cfg['sort'], ctx)
    out['sort'] = cfg.get('sort') or []
    _parse_calc(o, cfg.get('calc'))
    out['calc'] = cfg.get('calc') or {}
    if vt == 'kanban':
        kf = _field(o, cfg.get('kanban_field') or ('stage' if 'stage' in o['by_key'] else 'status'))
        if kf['type'] != 'select':
            raise RecordError('Kanban needs a select field.')
        out['kanban_field'] = kf['key']
    if cfg.get('q'):
        out['q'] = str(cfg['q'])[:100]
    return out


def _view_public(row):
    d = dict(row)
    try:
        d['config'] = json.loads(d.get('config') or '{}')
    except Exception:
        d['config'] = {}
    d['is_default'] = bool(d.get('is_default'))
    d['is_mine'] = d.get('user_id') == real_user_id()
    d.pop('is_active', None)
    return d


def _view_row(conn, vid):
    return conn.execute('SELECT * FROM bd_views WHERE id=? AND company_id=? AND is_active=1',
                        (vid, effective_company_id())).fetchone()


def _can_edit_view(row):
    return row['user_id'] == real_user_id() or (row['visibility'] == 'team' and is_company_admin())


def default_view_config(obj_name):
    o = _obj(obj_name)
    return {
        'view_type': 'table',
        'columns': [{'field': f['key'], 'width': f['width'], 'visible': True}
                    for f in o['fields'] if f['visible']],
        'filter': None, 'sort': o['default_sort'], 'calc': {},
    }


# ══════════════════════════════════════════════════════════════════════════
#  API — metadata
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/objects', methods=['GET'])
@login_required
def objects_meta():
    conn = get_db()
    members = _members(conn, effective_company_id())
    conn.close()
    objs = {}
    for name, o in OBJECTS.items():
        objs[name] = {
            'name': name, 'label': o['label'], 'singular': o['singular'],
            'title_field': o['title'],
            'default_view': default_view_config(name),
            'fields': [{
                'key': f['key'], 'label': f['label'], 'type': f['type'],
                'editable': f['editable'], 'required': f['required'],
                'computed': f['computed'], 'options': f['options'],
                'target': f['target'], 'width': f['width'],
                'default_visible': f['visible'],
                'filter_ops': OPS_BY_TYPE[_family(f['type'])],
                'calc_fns': CALC_BY_TYPE[_family(f['type'])],
            } for f in o['fields']],
        }
    return jsonify({'ok': True, 'objects': objs, 'members': members,
                    'me': real_user_id(), 'is_admin': bool(is_company_admin()),
                    'stages': [{'value': k, 'label': l, 'probability': p} for k, l, p in OPP_STAGES]})


# ══════════════════════════════════════════════════════════════════════════
#  API — records
# ══════════════════════════════════════════════════════════════════════════
def _spec_from_args():
    spec = {'q': request.args.get('q', ''),
            'page': request.args.get('page', 1),
            'per_page': request.args.get('per_page', 50)}
    for k in ('filter', 'sort', 'calc'):
        raw = request.args.get(k)
        if raw:
            try:
                spec[k] = json.loads(raw)
            except Exception:
                raise RecordError(f'{k} must be JSON.')
    for k in ('group_by', 'sum_field'):
        if request.args.get(k):
            spec[k] = request.args.get(k)
    return spec


@bp.route('/records/<obj>', methods=['GET'])
@login_required
def list_records(obj):
    conn = get_db()
    try:
        data = query_records(conn, obj, _spec_from_args())
        return jsonify({'ok': True, **data})
    except RecordError as e:
        return _err(e)
    finally:
        conn.close()


@bp.route('/records/<obj>/query', methods=['POST'])
@login_required
def query_records_post(obj):
    """Same as GET, with the spec in the JSON body (easier for big filters)."""
    conn = get_db()
    try:
        data = query_records(conn, obj, request.get_json(silent=True) or {})
        return jsonify({'ok': True, **data})
    except RecordError as e:
        return _err(e)
    finally:
        conn.close()


@bp.route('/records/<obj>/groups', methods=['GET', 'POST'])
@login_required
def group_records_api(obj):
    conn = get_db()
    try:
        spec = (request.get_json(silent=True) or {}) if request.method == 'POST' else _spec_from_args()
        data = group_records(conn, obj, spec)
        return jsonify({'ok': True, **data})
    except RecordError as e:
        return _err(e)
    finally:
        conn.close()


@bp.route('/records/<obj>/<int:rid>', methods=['GET'])
@login_required
def get_record_api(obj, rid):
    conn = get_db()
    try:
        _obj(obj)
        rec = get_record(conn, obj, rid)
        if not rec:
            return jsonify({'error': 'Not found'}), 404
        return jsonify({'ok': True, 'record': rec})
    except RecordError as e:
        return _err(e)
    finally:
        conn.close()


@bp.route('/records/<obj>', methods=['POST'])
@login_required
def create_record_api(obj):
    conn = get_db()
    try:
        _obj(obj)
        rid, err = _run_write(lambda: _create(conn, obj, request.get_json(silent=True) or {}))
        if err:
            return err
        return jsonify({'ok': True, 'record': get_record(conn, obj, rid)})
    except RecordError as e:
        return _err(e)
    finally:
        conn.close()


@bp.route('/records/<obj>/<int:rid>', methods=['PATCH', 'PUT'])
@login_required
def update_record_api(obj, rid):
    conn = get_db()
    try:
        _obj(obj)
        _, err = _run_write(lambda: _update(conn, obj, rid, request.get_json(silent=True) or {}))
        if err:
            return err
        rec = get_record(conn, obj, rid)
        return jsonify({'ok': True, 'record': rec})
    except RecordError as e:
        return _err(e)
    finally:
        conn.close()


@bp.route('/records/<obj>/<int:rid>', methods=['DELETE'])
@login_required
def delete_record_api(obj, rid):
    conn = get_db()
    try:
        _obj(obj)
        _, err = _run_write(lambda: _delete(conn, obj, rid))
        if err:
            return err
        return jsonify({'ok': True})
    except RecordError as e:
        return _err(e)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  API — views
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/views', methods=['GET'])
@login_required
def list_views():
    obj = request.args.get('object', '')
    conn = get_db()
    try:
        _obj(obj)
        rows = conn.execute(
            "SELECT * FROM bd_views WHERE company_id=? AND object=? AND is_active=1 "
            "AND (user_id=? OR visibility='team') ORDER BY position, id",
            (effective_company_id(), obj, real_user_id())).fetchall()
        return jsonify({'ok': True, 'object': obj, 'default_config': default_view_config(obj),
                        'views': [_view_public(r) for r in rows]})
    except RecordError as e:
        return _err(e)
    finally:
        conn.close()


@bp.route('/views', methods=['POST'])
@login_required
def create_view():
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        obj = d.get('object', '')
        o = _obj(obj)
        name = str(d.get('name') or '').strip()[:80]
        if not name:
            raise RecordError('View name is required.')
        vis = d.get('visibility', 'private')
        if vis not in ('private', 'team'):
            raise RecordError('visibility must be private or team.')
        if vis == 'team' and not is_company_admin():
            raise RecordError('Only an admin can create team views.', 403)
        cfg = _clean_view_config(o, d.get('config') or default_view_config(obj))
        company_id, uid, now = effective_company_id(), real_user_id(), ts()
        pos = conn.execute('SELECT COALESCE(MAX(position),0)+1 p FROM bd_views WHERE company_id=? AND object=?',
                           (company_id, obj)).fetchone()['p']
        is_def = 1 if d.get('is_default') else 0
        if is_def:
            conn.execute('UPDATE bd_views SET is_default=0 WHERE company_id=? AND object=? AND user_id=?',
                         (company_id, obj, uid))
        conn.execute('INSERT INTO bd_views (company_id,user_id,object,name,config,visibility,is_default,'
                     'position,is_active,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,1,?,?)',
                     (company_id, uid, obj, name, json.dumps(cfg), vis, is_def, pos, now, now))
        vid = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
        conn.commit()
        return jsonify({'ok': True, 'view': _view_public(_view_row(conn, vid))})
    except RecordError as e:
        return _err(e)
    finally:
        conn.close()


@bp.route('/views/<int:vid>', methods=['PUT', 'PATCH'])
@login_required
def update_view(vid):
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        row = _view_row(conn, vid)
        if not row or (row['user_id'] != real_user_id() and row['visibility'] != 'team'):
            raise RecordError('View not found.', 404)
        if not _can_edit_view(row):
            raise RecordError('Only the creator or an admin can change this view.', 403)
        o = _obj(row['object'])
        fields = {}
        if 'name' in d:
            nm = str(d.get('name') or '').strip()[:80]
            if not nm:
                raise RecordError('View name is required.')
            fields['name'] = nm
        if 'config' in d:
            fields['config'] = json.dumps(_clean_view_config(o, d['config']))
        if 'visibility' in d:
            if d['visibility'] not in ('private', 'team'):
                raise RecordError('visibility must be private or team.')
            if d['visibility'] == 'team' and not is_company_admin():
                raise RecordError('Only an admin can share views with the team.', 403)
            fields['visibility'] = d['visibility']
        if 'position' in d:
            fields['position'] = int(d.get('position') or 0)
        if 'is_default' in d:
            fields['is_default'] = 1 if d['is_default'] else 0
            if fields['is_default']:
                conn.execute('UPDATE bd_views SET is_default=0 WHERE company_id=? AND object=? AND user_id=?',
                             (row['company_id'], row['object'], row['user_id']))
        if fields:
            conn.execute('UPDATE bd_views SET ' + ', '.join(f'{k}=?' for k in fields) + ', updated_at=? WHERE id=?',
                         list(fields.values()) + [ts(), vid])
            conn.commit()
        return jsonify({'ok': True, 'view': _view_public(_view_row(conn, vid))})
    except RecordError as e:
        return _err(e)
    finally:
        conn.close()


@bp.route('/views/<int:vid>', methods=['DELETE'])
@login_required
def delete_view(vid):
    conn = get_db()
    try:
        row = _view_row(conn, vid)
        if not row or (row['user_id'] != real_user_id() and row['visibility'] != 'team'):
            raise RecordError('View not found.', 404)
        if not _can_edit_view(row):
            raise RecordError('Only the creator or an admin can delete this view.', 403)
        conn.execute('UPDATE bd_views SET is_active=0, updated_at=? WHERE id=?', (ts(), vid))
        conn.commit()
        return jsonify({'ok': True})
    except RecordError as e:
        return _err(e)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  RECORD PANEL — timeline + calendar  (Wave 3, read-only, additive)
# ══════════════════════════════════════════════════════════════════════════
def _meta(s):
    try:
        d = json.loads(s or '{}')
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _panel_anchor(conn, obj, rid, company_id):
    """Tenant-checked record -> (record dict, client_id). None if not visible."""
    rec = get_record(conn, obj, rid, company_id)
    if not rec:
        return None, 0
    client_id = rid if obj == 'companies' else int(rec.get('client_id') or 0)
    return rec, client_id


def _activity_ids(conn, company_id, col, val):
    return {r['id'] for r in conn.execute(
        f'SELECT id FROM crm_activities WHERE company_id=? AND {col}=?', (company_id, val))}


def record_timeline(conn, obj, rid, company_id=None, limit=200):
    """Everything that happened to a record, newest first. Built from the
    universal activity_log (all CRM/BD writes land there under the company),
    narrowed to the person / deal / activity when the record is not a company."""
    _obj(obj)
    company_id = company_id if company_id is not None else effective_company_id()
    rec, client_id = _panel_anchor(conn, obj, rid, company_id)
    if not rec:
        raise RecordError('Not found.', 404)
    rows = conn.execute(
        "SELECT id, user_id, username, actor_name, actor_type, action, detail, meta, created_at, "
        + _user_label('activity_log.user_id') + " AS user_display FROM activity_log "
        "WHERE company_id=? AND entity_type='client' AND entity_id=? ORDER BY id DESC LIMIT 2000",
        (company_id, client_id)).fetchall()

    if obj == 'people':
        acts = _activity_ids(conn, company_id, 'contact_id', rid)
        keep = lambda m: m.get('contact_id') == rid or m.get('activity_id') in acts
    elif obj == 'opportunities':
        acts = _activity_ids(conn, company_id, 'opportunity_id', rid)
        keep = lambda m: m.get('opportunity_id') == rid or m.get('activity_id') in acts
    elif obj in ('tasks', 'notes'):
        keep = lambda m: m.get('activity_id') == rid
    else:
        keep = lambda m: True

    out = []
    for r in rows:
        m = _meta(r['meta'])
        if not keep(m):
            continue
        changes = m.get('changes') if isinstance(m.get('changes'), list) else []
        out.append({
            'id': r['id'], 'action': r['action'] or '', 'detail': r['detail'] or '',
            # a person's display name for user actions; the recorded actor otherwise (system, client…)
            'actor': (r['user_display'] if (r['user_id'] and (r['actor_type'] or 'user') == 'user' and r['user_display'])
                      else (r['actor_name'] or r['username'] or '')),
            'user_id': r['user_id'] or 0,
            'created_at': r['created_at'] or '',
            'changes': [str(c) for c in changes][:20],
            'from': m.get('from'), 'to': m.get('to'), 'type': m.get('type'),
        })
        if len(out) >= limit:
            break
    return out


def record_calendar(conn, obj, rid, company_id=None):
    """Meetings and calls for a record: scheduler bookings auto-linked to the
    company / contact, plus BD meetings & calls that carry a date."""
    _obj(obj)
    if obj in ('tasks', 'notes'):
        raise RecordError('Calendar is available for companies, people and opportunities.')
    company_id = company_id if company_id is not None else effective_company_id()
    rec, client_id = _panel_anchor(conn, obj, rid, company_id)
    if not rec:
        raise RecordError('Not found.', 404)
    items = []

    col = {'companies': 'client_id', 'people': 'contact_id', 'opportunities': 'opportunity_id'}[obj]
    for r in conn.execute(
            f"SELECT a.id, a.activity_type, a.subject, a.due_at, a.status, a.owner_user_id, "
            f"{_user_label('a.owner_user_id')} AS owner_name "
            f"FROM crm_activities a WHERE a.company_id=? AND a.{col}=? AND a.is_active=1 "
            f"AND a.activity_type IN ('meeting','call') AND COALESCE(a.due_at,'')!=''",
            (company_id, rid)):
        items.append({'source': 'bd', 'id': r['id'], 'kind': r['activity_type'],
                      'title': r['subject'] or r['activity_type'].title(),
                      'start_at': r['due_at'], 'end_at': '', 'status': r['status'] or '',
                      'owner': r['owner_name'] or '', 'mode': '', 'location': ''})

    if obj in ('companies', 'people'):
        mcol = 'crm_client_id' if obj == 'companies' else 'crm_contact_id'
        try:
            for r in conn.execute(
                    f"SELECT m.id, m.guest_name, m.purpose, m.start_at, m.end_at, m.mode, m.location, m.status, "
                    f"{_user_label('m.host_user_id')} AS host_name "
                    f"FROM meetings m WHERE m.company_id=? AND m.{mcol}=?", (company_id, rid)):
                items.append({'source': 'scheduler', 'id': r['id'], 'kind': 'meeting',
                              'title': r['purpose'] or ('Meeting with ' + (r['guest_name'] or 'guest')),
                              'start_at': r['start_at'] or '', 'end_at': r['end_at'] or '',
                              'status': r['status'] or '', 'owner': r['host_name'] or '',
                              'mode': r['mode'] or '', 'location': r['location'] or ''})
        except Exception:
            pass   # scheduler module not installed on this database

    now = _now_iso()[:16]
    upcoming = sorted([i for i in items if (i['start_at'] or '')[:16] >= now], key=lambda i: i['start_at'])
    past = sorted([i for i in items if (i['start_at'] or '')[:16] < now], key=lambda i: i['start_at'], reverse=True)
    return {'upcoming': upcoming, 'past': past[:100]}


@bp.route('/records/<obj>/<int:rid>/timeline', methods=['GET'])
@login_required
def record_timeline_api(obj, rid):
    conn = get_db()
    try:
        return jsonify({'ok': True, 'items': record_timeline(conn, obj, rid)})
    except RecordError as e:
        return _err(e)
    finally:
        conn.close()


@bp.route('/records/<obj>/<int:rid>/calendar', methods=['GET'])
@login_required
def record_calendar_api(obj, rid):
    conn = get_db()
    try:
        return jsonify({'ok': True, **record_calendar(conn, obj, rid)})
    except RecordError as e:
        return _err(e)
    finally:
        conn.close()
