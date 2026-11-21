"""
RecruitOS — Insight Engine, Wave 1: Capture  (Nov 2026)

Keeps the insight the ATS used to throw away, at the moment it is known:

  1a  A REASON for every negative stage move (Not Interested, Not Suitable,
      Client Rejected …) and the client's FEEDBACK on every client rejection.
  1b  A "Call capture" card on the candidate profile: expected CTC, notice,
      team size, yearly target and a MARKET NOTE about a company.
  1c  Call analysis proposes the same data (reason for no, market notes, team
      size, target). The recruiter approves; nothing is applied on its own.
  1d  Auto-tagging: role family, function tags, domain tags and level band from
      the profile / CV, filling EMPTY values only. New candidates are picked up
      by a background sweeper; an admin backfill does the existing database,
      newest first, throttled and resumable.
  1e  The "6:30 check": my records touched today that are missing a reason,
      missing client feedback, or have expected CTC 0 at Interested or later.

Tables (new)
  candidate_reasons   full history of reasons; the latest row is the current one
  market_notes        a signal about a company (hiring, freeze, exit …)
  insight_tag_log     which candidates the tagger has processed (resume point)
  insight_backfill    one row per agency: the admin backfill's progress
  insight_settings    per-agency switches (insight_capture_on, default ON)
Columns (new)          candidates.level_band, candidates.role_family

Routes (all @login_required, scoped to the caller's agency + recruiter scope)
  GET  /insight-capture.js                       the shared picker / card / panel script
  GET  /api/insight/config                       switch + reason catalogue
  POST /api/insight/config                       {capture_on}            (agency admin)
  GET  /api/candidates/<cid>/reasons             reason history
  POST /api/candidates/<cid>/reasons             reason for the CURRENT stage (no move)
  GET  /api/candidates/<cid>/capture             the Call capture card's data
  PUT  /api/candidates/<cid>/capture             save the card
  GET  /api/candidates/<cid>/market-notes
  POST /api/candidates/<cid>/market-notes
  GET  /api/insight/check-630                    the 6:30 panel rows + counts
  POST /api/insight/backfill/start | /stop       (agency admin)
  GET  /api/insight/backfill/status

Other code calls in through:
  validate_reason() / record_reason()   — server.move_stage, and later any stage writer
  call_prompt_fields() / call_proposals() — server.analyse_call
  apply_call_proposal()                  — call_intel.apply_call_details

ROLLBACK: remove 'insight_capture' from modules/__init__.py (or switch
insight_capture_on off in Settings). Stage moves, call analysis and the
profile behave exactly as before; the new tables and columns stay unused.
"""

import os
import sys
import json
import time
import datetime
import threading

from flask import Blueprint, request, jsonify, Response

from modules.shared import get_db, ts, effective_company_id, real_user_id, is_company_admin, login_required, _core
from modules import register_migration

bp = Blueprint('insight_capture', __name__)
pages = Blueprint('insight_capture_pages', __name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ══════════════════════════════════════════════════════════════════════════
#  Catalogue
# ══════════════════════════════════════════════════════════════════════════
REASONS = {
    'candidate_no': [
        ('ctc_gap', 'CTC gap'), ('location', 'Location'), ('recently_promoted', 'Recently promoted'),
        ('counter_offer', 'Counter offer'), ('other_offer', 'Took another offer'),
        ('client_brand', 'Client brand / company'), ('role_scope', 'Role / scope'),
        ('notice_period', 'Notice period'), ('happy_in_current', 'Happy in current job'),
        ('personal', 'Personal reasons'), ('other', 'Other'),
    ],
    'screen_out': [
        ('experience_gap', 'Experience gap'), ('domain_mismatch', 'Domain mismatch'),
        ('ctc_out_of_band', 'CTC out of band'), ('location', 'Location'),
        ('communication', 'Communication'), ('stability', 'Stability / job hopping'), ('other', 'Other'),
    ],
    'client_reject': [
        ('profile_mismatch', 'Profile mismatch'), ('ctc_high', 'CTC too high'), ('notice_long', 'Notice too long'),
        ('experience_level', 'Experience level'), ('communication', 'Communication'),
        ('technical_depth', 'Technical depth'), ('culture_fit', 'Culture fit'),
        ('position_on_hold', 'Position on hold'), ('other', 'Other'),
    ],
}
FAMILY_STAGES = {
    'candidate_no': ('not interested', 'interview backout'),
    'screen_out': ('not suitable', 'screened-out', 'screened out'),
    'client_reject': ('client rejected on paper', 'client rejected after interview'),
}
FAMILY_TITLE = {'candidate_no': 'Why did the candidate say no?', 'screen_out': 'Why is the candidate not suitable?',
                'client_reject': 'Why did the client reject?'}
CODES = {f: [c for c, _ in v] for f, v in REASONS.items()}
LABELS = {f: dict(v) for f, v in REASONS.items()}

SIGNALS = [('hiring', 'Hiring'), ('freeze', 'Hiring freeze'), ('exit', 'People leaving'), ('join', 'People joining'),
           ('new_order_project', 'New order / project'), ('pay', 'Pay / increments'), ('reorg', 'Re-org'), ('other', 'Other')]
SIGNAL_CODES = [s for s, _ in SIGNALS]
SOURCES = ('manual', 'call_ai', 'bulk', 'auto')

# Role families (owner's list + "General Management / Leadership"), each mapped
# onto the EXISTING Function tag words the Talent filters already use
# (server.get_tag_suggestions), so there is one vocabulary.
SUPPORT = 'Support functions (HR / Finance / IT / Digital)'
FAMILY_TAGS = {
    'Channel Sales': ['Channel Sales'],
    'Key Account / Project Sales': ['Key Account Management', 'Sales'],
    'Application / Pre-sales Engineering': ['Application Engineering', 'Pre-Sales'],
    'Project Execution': ['Project Management'],
    'Service / Field': ['Service & Support'],
    'Product Management': ['Product Management'],
    'Design / R&D Engineering': ['R&D', 'Design'],
    'Manufacturing / Quality': ['Quality', 'Operations'],
    'Procurement / SCM': ['Procurement', 'Supply Chain'],
    'Business Development': ['Business Development'],
    SUPPORT: [],                                  # HR / Finance / IT / Digital, from support_sub
    'General Management / Leadership': ['General Management', 'Strategy'],
}
ROLE_FAMILIES = list(FAMILY_TAGS)
SUPPORT_SUBS = {'hr': 'HR', 'finance': 'Finance', 'it': 'IT / Digital', 'digital': 'IT / Digital', 'it / digital': 'IT / Digital'}
# existing tag (lower case) -> family. The first list wins over the generic one.
_TAG_SPECIFIC = {
    'channel sales': 'Channel Sales', 'key account management': 'Key Account / Project Sales',
    'technical sales': 'Key Account / Project Sales', 'application engineering': 'Application / Pre-sales Engineering',
    'pre-sales': 'Application / Pre-sales Engineering', 'presales': 'Application / Pre-sales Engineering',
    'project management': 'Project Execution', 'service & support': 'Service / Field',
    'after sales service': 'Service / Field', 'product management': 'Product Management',
    'r&d': 'Design / R&D Engineering', 'design': 'Design / R&D Engineering', 'quality': 'Manufacturing / Quality',
    'procurement': 'Procurement / SCM', 'supply chain': 'Procurement / SCM',
    'business development': 'Business Development', 'marketing': 'Business Development',
    'hr': SUPPORT, 'finance': SUPPORT, 'it / digital': SUPPORT,
    'general management': 'General Management / Leadership', 'strategy': 'General Management / Leadership',
}
_TAG_GENERIC = {'sales': 'Key Account / Project Sales', 'operations': 'Manufacturing / Quality',
                'engineering': 'Design / R&D Engineering'}
DOMAINS = ['LV', 'MV', 'HV / GIS', 'Industrial Automation', 'Solar / Renewables', 'Data Center', 'BMS',
           'Cables & Wires', 'Power Electronics / UPS', 'Other']
LEVELS = ['Engineer / Executive', 'Asst / Dy Manager', 'Manager', 'Sr Manager', 'DGM / AGM', 'GM+']

# stages that count as "at or past Interested" for the expected-CTC check
_POST_INTERESTED = {'interested', 'updated cv awaited', 'shared with client', 'interview inprocess', 'placed', 'joined'}

NEW_WINDOW_HOURS = 48     # the sweeper tags candidates created in this window
THROTTLE_SECONDS = 3      # gap between two AI calls
IDLE_SECONDS = 60         # nothing to do: look again after this


@register_migration
def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS candidate_reasons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        candidate_id INTEGER NOT NULL,
        mandate_id INTEGER DEFAULT 0,
        stage TEXT DEFAULT '',
        reason_code TEXT DEFAULT '',
        detail TEXT DEFAULT '',
        client_feedback TEXT DEFAULT '',
        source TEXT DEFAULT 'manual',
        user_id INTEGER DEFAULT 0,
        created_at TEXT DEFAULT ''
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_creasons_cand ON candidate_reasons(owner_id, candidate_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_creasons_code ON candidate_reasons(owner_id, reason_code)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_creasons_time ON candidate_reasons(owner_id, created_at)')
    conn.execute('''CREATE TABLE IF NOT EXISTS market_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        candidate_id INTEGER DEFAULT 0,
        mandate_id INTEGER DEFAULT 0,
        company_name TEXT DEFAULT '',
        company_key TEXT DEFAULT '',
        signal_type TEXT DEFAULT 'other',
        note TEXT DEFAULT '',
        source TEXT DEFAULT 'manual',
        user_id INTEGER DEFAULT 0,
        created_at TEXT DEFAULT ''
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_mnotes_company ON market_notes(owner_id, company_key)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_mnotes_time ON market_notes(owner_id, created_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_mnotes_cand ON market_notes(owner_id, candidate_id)')
    conn.execute('''CREATE TABLE IF NOT EXISTS insight_tag_log (
        candidate_id INTEGER PRIMARY KEY,
        owner_id INTEGER DEFAULT 0,
        status TEXT DEFAULT '',
        filled TEXT DEFAULT '',
        error TEXT DEFAULT '',
        tried_at TEXT DEFAULT ''
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_itag_owner ON insight_tag_log(owner_id, status)')
    conn.execute('''CREATE TABLE IF NOT EXISTS insight_backfill (
        owner_id INTEGER PRIMARY KEY,
        status TEXT DEFAULT 'idle',
        total INTEGER DEFAULT 0,
        done INTEGER DEFAULT 0,
        failed INTEGER DEFAULT 0,
        message TEXT DEFAULT '',
        started_by INTEGER DEFAULT 0,
        started_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        finished_at TEXT DEFAULT ''
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS insight_settings (
        owner_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        value TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        PRIMARY KEY (owner_id, key)
    )''')
    for col in ('level_band', 'role_family'):
        try:
            conn.execute(f"ALTER TABLE candidates ADD COLUMN {col} TEXT DEFAULT ''")
        except Exception:
            pass                                   # already there
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  Small helpers
# ══════════════════════════════════════════════════════════════════════════
def _clean(v, n=500):
    return ' '.join(str(v or '').split())[:n]


def _clean_block(v, n=2000):
    """Like _clean but keeps line breaks (client feedback can be a paragraph)."""
    lines = [' '.join(x.split()) for x in str(v or '').replace('\r', '').split('\n')]
    return '\n'.join(x for x in lines if x)[:n]


def company_key(name):
    try:
        from modules.talent_map import company_key as _ck
        return _ck(name)
    except Exception:
        return ' '.join(str(name or '').lower().split())


def family_for(stage):
    s = ' '.join(str(stage or '').lower().split())
    for fam, names in FAMILY_STAGES.items():
        if s in names:
            return fam
    return None


def reason_label(code, family=None):
    for fam in ([family] if family else []) + list(LABELS):
        if fam and code in LABELS.get(fam, {}):
            return LABELS[fam][code]
    return code or ''


def _scope():
    try:
        return _core()._cs('c.mandate_id')
    except Exception:
        return '', []


def _own_cand(conn, cid):
    """The candidate row if it is in the caller's agency AND recruiter scope."""
    sc, sp = _scope()
    return conn.execute('SELECT c.* FROM candidates c WHERE c.id=? AND c.owner_id=?' + sc,
                        [cid, effective_company_id()] + sp).fetchone()


def _fl_block():
    try:
        from modules.freelancer import block_if_freelancer
        return block_if_freelancer()
    except Exception:
        return None


def _uid():
    try:
        return int(real_user_id() or 0)
    except Exception:
        return 0


def setting(conn, oid, key, default='1'):
    r = conn.execute('SELECT value FROM insight_settings WHERE owner_id=? AND key=?', (oid, key)).fetchone()
    return default if r is None else (r['value'] or '')


def capture_on(conn, oid):
    return str(setting(conn, oid, 'insight_capture_on', '1')).strip().lower() not in ('0', 'false', 'off', 'no', '')


def _set_setting(conn, oid, key, value):
    conn.execute('INSERT OR REPLACE INTO insight_settings (owner_id, key, value, updated_at) VALUES (?,?,?,?)',
                 (oid, key, str(value), ts()))


# ══════════════════════════════════════════════════════════════════════════
#  1a  Reasons — the one helper every stage writer can call
# ══════════════════════════════════════════════════════════════════════════
def validate_reason(stage, code, client_feedback='', family=None, require_feedback=True):
    """-> (family, code, feedback). Raises ValueError with a plain message."""
    fam = family or family_for(stage)
    if not fam:
        raise ValueError('A reason can only be given for a negative stage '
                         '(Not Interested, Not Suitable, Client Rejected …), not "%s".' % (stage or ''))
    c = str(code or '').strip().lower()
    if c not in CODES[fam]:
        raise ValueError('"%s" is not a valid reason for %s.' % (code or '', stage or fam))
    fb = _clean_block(client_feedback)
    if fam == 'client_reject' and require_feedback and not fb:
        raise ValueError("Write the client's feedback for a client rejection.")
    return fam, c, fb


def _append_client_feedback(conn, oid, cid, stage, fb):
    """Add a dated line to candidates.client_feedback. Never overwrites."""
    r = conn.execute('SELECT client_feedback FROM candidates WHERE id=? AND owner_id=?', (cid, oid)).fetchone()
    if not r:
        return
    try:
        day = datetime.datetime.strptime(ts()[:10], '%Y-%m-%d').strftime('%d %b %Y')
    except Exception:
        day = ts()[:10]
    line = '%s · %s: %s' % (day, stage, fb)
    old = (r['client_feedback'] or '').rstrip()
    conn.execute('UPDATE candidates SET client_feedback=? WHERE id=? AND owner_id=?',
                 ((old + '\n---\n' + line) if old else line, cid, oid))


def record_reason(conn, oid, cid, mandate_id, stage, code, detail='', client_feedback='',
                  source='manual', user_id=0, family=None, require_feedback=True):
    """Validate and store one reason (same connection / transaction as the
    caller — the caller commits). Client feedback is appended to the
    candidate's client_feedback column too. Returns the new row id."""
    fam, c, fb = validate_reason(stage, code, client_feedback, family, require_feedback)
    src = source if source in SOURCES else 'manual'
    cur = conn.execute('INSERT INTO candidate_reasons (owner_id, candidate_id, mandate_id, stage, reason_code, detail, '
                       'client_feedback, source, user_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (oid, cid, int(mandate_id or 0), str(stage or ''), c, _clean(detail, 300), fb, src,
                        int(user_id or 0), ts()))
    if fb:
        _append_client_feedback(conn, oid, cid, stage, fb)
    return cur.lastrowid


def reason_note(stage, code, detail=''):
    """Short text for the stage_history note."""
    lab = reason_label(code, family_for(stage))
    d = _clean(detail, 120)
    return 'Reason: ' + lab + ((' — ' + d) if d else '')


def latest_reason(conn, oid, cid):
    r = conn.execute('SELECT * FROM candidate_reasons WHERE owner_id=? AND candidate_id=? ORDER BY id DESC LIMIT 1',
                     (oid, cid)).fetchone()
    return dict(r) if r else None


def _reason_out(r):
    d = dict(r)
    d['label'] = reason_label(d.get('reason_code'), family_for(d.get('stage')))
    return d


# ══════════════════════════════════════════════════════════════════════════
#  1b  Market notes
# ══════════════════════════════════════════════════════════════════════════
def add_market_note(conn, oid, cid, mandate_id, company, signal_type, note, source='manual', user_id=0):
    company = _clean(company, 120)
    note = _clean(note, 600)
    st = str(signal_type or 'other').strip().lower()
    if st not in SIGNAL_CODES:
        raise ValueError('Unknown signal type "%s".' % signal_type)
    if not company:
        raise ValueError('Which company is this note about?')
    if not note:
        raise ValueError('Write the market note.')
    cur = conn.execute('INSERT INTO market_notes (owner_id, candidate_id, mandate_id, company_name, company_key, '
                       'signal_type, note, source, user_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (oid, int(cid or 0), int(mandate_id or 0), company, company_key(company), st, note,
                        source if source in ('manual', 'call_ai') else 'manual', int(user_id or 0), ts()))
    return cur.lastrowid


# ══════════════════════════════════════════════════════════════════════════
#  1c  Call analysis
# ══════════════════════════════════════════════════════════════════════════
def call_prompt_fields(oid=None):
    """Extra JSON keys for the analyse_call() prompt ('' when switched off)."""
    try:
        conn = get_db()
        try:
            if not capture_on(conn, oid or effective_company_id()):
                return ''
        finally:
            conn.close()
    except Exception:
        return ''
    return ('  "reason_for_no": null or one of "%s" (ONLY when the candidate clearly declines or is not interested: the main reason),\n'
            '  "reason_for_no_detail": null or "one short line: why, in the candidate\'s words",\n'
            '  "team_direct_discussed": null or int (people reporting directly to the candidate, only if stated),\n'
            '  "team_indirect_discussed": null or int (people under them through their reports, only if stated),\n'
            '  "yearly_target_cr_discussed": null or float (annual sales target in Rs crore, only if stated; convert lakhs to crore),\n'
            '  "market_notes": [ {"company": "the company the news is about", "signal_type": "%s", '
            '"note": "one line: a fact the candidate shared about that company"} ] or [] '
            '(only hiring, freeze, people leaving / joining, new orders / projects, pay, re-org news actually said),\n'
            % ('|'.join(CODES['candidate_no']), '|'.join(SIGNAL_CODES)))


def call_proposals(conn, oid, cand, analysis):
    """Proposals for the existing proposed_updates approval flow."""
    out = {}
    if not isinstance(analysis, dict) or not capture_on(conn, oid):
        return out
    core = _core()
    c = dict(cand)
    for key, ak in (('team_direct', 'team_direct_discussed'), ('team_indirect', 'team_indirect_discussed')):
        v = core.clean_team(analysis.get(ak))
        if v is not None and v > 0 and v != c.get(key):
            out[key] = {'current': c.get(key) if c.get(key) is not None else '', 'proposed': v}
    t = core.clean_target(analysis.get('yearly_target_cr_discussed'))
    if t is not None and t != c.get('yearly_target'):
        out['yearly_target'] = {'current': c.get('yearly_target') if c.get('yearly_target') is not None else '',
                                'proposed': t}
    code = str(analysis.get('reason_for_no') or '').strip().lower()
    if code in CODES['candidate_no']:
        cur = latest_reason(conn, oid, c['id'])
        out['reason_for_no'] = {'current': reason_label(cur['reason_code'], family_for(cur['stage'])) if cur else '',
                                'proposed': code, 'label': LABELS['candidate_no'][code],
                                'detail': _clean(analysis.get('reason_for_no_detail'), 300)}
    notes = []
    for n in (analysis.get('market_notes') or [])[:8] if isinstance(analysis.get('market_notes'), list) else []:
        if not isinstance(n, dict):
            continue
        st = str(n.get('signal_type') or '').strip().lower()
        txt = _clean(n.get('note'), 600)
        comp = _clean(n.get('company') or c.get('company'), 120)
        if st in SIGNAL_CODES and txt and comp:
            notes.append({'company': comp, 'signal_type': st, 'note': txt})
    if notes:
        out['market_notes'] = {'current': '', 'proposed': notes[:5]}
    return out


CALL_FIELDS = {'team_direct': 'Direct reports', 'team_indirect': 'Indirect team', 'yearly_target': 'Yearly target',
               'reason_for_no': 'Reason for no', 'market_notes': 'Market notes'}


def apply_call_proposal(conn, oid, cand, key, prop, user_id=0):
    """Apply ONE approved proposal. Returns a short summary for the history."""
    core = _core()
    c = dict(cand)
    val = prop.get('proposed') if isinstance(prop, dict) else None
    if key in ('team_direct', 'team_indirect', 'yearly_target'):
        core.save_sales_profile(conn, c['id'], {key: val})
        try:
            core._sync_person_fields(conn, c['id'], [key])
        except Exception:
            pass
        return '%s %s → %s' % (CALL_FIELDS[key], c.get(key) if c.get(key) not in (None, '') else '—', val)
    if key == 'reason_for_no':
        record_reason(conn, oid, c['id'], c.get('mandate_id'), c.get('stage') or '', val,
                      prop.get('detail') or '', '', 'call_ai', user_id, family='candidate_no')
        return 'Reason for no: ' + reason_label(val, 'candidate_no')
    if key == 'market_notes':
        n = 0
        for x in (val or []):
            try:
                add_market_note(conn, oid, c['id'], c.get('mandate_id'), x.get('company'), x.get('signal_type'),
                                x.get('note'), 'call_ai', user_id)
                n += 1
            except (ValueError, AttributeError):
                continue
        return '%d market note%s' % (n, '' if n == 1 else 's')
    raise ValueError('Unknown field ' + key)


# ══════════════════════════════════════════════════════════════════════════
#  1d  Auto-tagging
# ══════════════════════════════════════════════════════════════════════════
class NoKey(Exception):
    pass


def _empty(v):
    return v is None or str(v).strip() in ('', '[]', 'null', 'None')


_NEEDS_SQL = ("(COALESCE(c.function_tags,'') IN ('','[]') OR COALESCE(c.domain_tags,'') IN ('','[]') "
              "OR COALESCE(c.level_band,'')='' OR COALESCE(c.role_family,'')='')")
TAG_FIELDS = ('role_family', 'function_tags', 'domain_tags', 'level_band')


def family_from_tags(tags):
    lo = [str(t).strip().lower() for t in (tags or [])]
    for t in lo:
        if t in _TAG_SPECIFIC:
            return _TAG_SPECIFIC[t]
    for t in lo:
        if t in _TAG_GENERIC:
            return _TAG_GENERIC[t]
    return ''


def _jl(v):
    try:
        x = json.loads(v or '[]')
        return [str(i) for i in x if str(i).strip()] if isinstance(x, list) else []
    except Exception:
        return [s.strip() for s in str(v or '').split(',') if s.strip()]


def _profile_text(conn, c):
    parts = [('Designation', c['designation']), ('Current company', c['company']),
             ('Experience (years)', c['experience']), ('Industry', c['industry_background']),
             ('Key skills', ', '.join(_jl(c['key_skills']))), ('Products handled', ', '.join(_jl(c['product_handles']))),
             ('Summary', _clean(c['career_summary'], 1500))]
    lines = ['%s: %s' % (k, v) for k, v in parts if v not in (None, '', 0)]
    try:
        wh = conn.execute('SELECT designation, company, start_date, end_date FROM work_history WHERE candidate_id=? '
                          'ORDER BY COALESCE(is_current,0) DESC, sort_order LIMIT 4', (c['id'],)).fetchall()
        if wh:
            lines.append('Work history: ' + '; '.join(
                '%s @ %s (%s-%s)' % (w['designation'] or '', w['company'] or '', w['start_date'] or '', w['end_date'] or '')
                for w in wh))
    except Exception:
        pass
    text = '\n'.join(lines)
    if len(_clean(c['career_summary'], 2000)) + len(' '.join(_jl(c['key_skills']))) < 300 and c['cv_path']:
        try:
            cv = _core()._candidate_cv_text(c, 3000)
            if cv:
                text += '\nCV extract: ' + cv
        except Exception:
            pass
    return text


_TAG_SYSTEM = (
    'You classify candidates of an Indian electrical / power / automation recruitment agency. '
    'Return ONLY JSON: {"role_family": one of %s, "support_sub": null or one of ["HR","Finance","IT / Digital"] '
    '(only for the support family), "domains": up to 3 of %s, "level_band": one of %s}. '
    'role_family = what the person DOES now (their function), not the industry. Key Account / Project Sales covers '
    'direct / project / institutional / technical sales; Business Development covers marketing too. '
    'domains = the electrical product / market areas they work in (LV = low-voltage switchgear, MCB, MCCB, ACB, wiring devices; '
    'MV = medium-voltage switchgear, RMU, VCB; HV / GIS = high voltage, GIS, substations). Use "Other" only if none fit. '
    'level_band from designation and experience (Engineer / Executive ~0-5y, Asst / Dy Manager, Manager, Sr Manager, '
    'DGM / AGM, GM+ for GM / VP / Director / Head / CXO). Never invent; use the closest value.'
    % (json.dumps(ROLE_FAMILIES), json.dumps(DOMAINS), json.dumps(LEVELS)))


def _classify(profile_text):
    """One cheap DeepSeek call. Tests replace this function."""
    core = _core()
    key = core.get_setting('deepseek_api_key')
    if not key:
        raise NoKey()
    resp = core.call_deepseek(key, {
        'model': 'deepseek-chat', 'temperature': 0, 'max_tokens': 160,
        'response_format': {'type': 'json_object'},
        'messages': [{'role': 'system', 'content': _TAG_SYSTEM},
                     {'role': 'user', 'content': profile_text[:6000]}]}, timeout=40, endpoint='insight-tag')
    if resp.status_code != 200:
        raise RuntimeError('DeepSeek HTTP %s' % resp.status_code)
    return core.parse_json(resp.json()['choices'][0]['message']['content'] or '') or {}


def _norm_ai(res):
    """Keep only values from our lists."""
    out = {}
    if not isinstance(res, dict):
        return out
    fam = next((f for f in ROLE_FAMILIES if f.lower() == str(res.get('role_family') or '').strip().lower()), '')
    if fam:
        out['role_family'] = fam
        tags = list(FAMILY_TAGS[fam])
        if fam == SUPPORT:
            sub = SUPPORT_SUBS.get(str(res.get('support_sub') or '').strip().lower())
            if sub:
                tags = [sub]
        if tags:
            out['function_tags'] = tags
    doms = res.get('domains') or []
    if isinstance(doms, str):
        doms = [doms]
    dl = [next((d for d in DOMAINS if d.lower() == str(x).strip().lower()), '') for x in doms if x]
    dl = [d for i, d in enumerate(dl) if d and d not in dl[:i]][:3]
    if dl:
        out['domain_tags'] = dl
    lv = next((l for l in LEVELS if l.lower() == str(res.get('level_band') or '').strip().lower()), '')
    if lv:
        out['level_band'] = lv
    return out


def tag_person(conn, oid, cid, classify=None):
    """Fill the EMPTY tag fields of one person (and of every linked copy of the
    same person on other jobs). Returns (status, used_ai). The caller commits.
    Raises NoKey (nothing logged) or the core TokenCapError."""
    c = conn.execute('SELECT * FROM candidates WHERE id=? AND owner_id=?', (cid, oid)).fetchone()
    if not c:
        return 'missing', False
    root = int(c['copied_from'] or 0) or int(c['id'])
    group = conn.execute('SELECT * FROM candidates WHERE owner_id=? AND (id=? OR copied_from=?)',
                         (oid, root, root)).fetchall() or [c]
    ids = [int(g['id']) for g in group]
    need = {f for f in TAG_FIELDS for g in group if _empty(g[f])}
    vals, used_ai, status, err = {}, False, 'done', ''
    if need:
        human_tags = next((_jl(g['function_tags']) for g in group if not _empty(g['function_tags'])), [])
        fam = family_from_tags(human_tags)
        if fam:
            vals['role_family'] = fam
        info = ' '.join(str(c[k] or '') for k in ('designation', 'career_summary', 'key_skills')).strip()
        if need - set(vals):
            if not info and not c['cv_path']:
                status = 'skipped'
            else:
                try:
                    ai = _norm_ai((classify or _classify)(_profile_text(conn, c)))
                    used_ai = True
                except NoKey:
                    raise
                except Exception as e:
                    if type(e).__name__ == 'TokenCapError':
                        raise
                    ai, status, err = {}, 'error', str(e)[:200]
                for k, v in ai.items():
                    vals.setdefault(k, v)
    filled = set()
    for g in group:
        sets = {}
        for f in TAG_FIELDS:
            if f in vals and _empty(g[f]):
                sets[f] = json.dumps(vals[f]) if isinstance(vals[f], list) else vals[f]
        if sets:
            filled.update(sets)
            conn.execute('UPDATE candidates SET ' + ', '.join(k + '=?' for k in sets) + ' WHERE id=? AND owner_id=?',
                         list(sets.values()) + [g['id'], oid])
    if status == 'done' and not filled and need:
        status = 'skipped'
    for i in ids:
        conn.execute('INSERT OR REPLACE INTO insight_tag_log (candidate_id, owner_id, status, filled, error, tried_at) '
                     'VALUES (?,?,?,?,?,?)', (i, oid, status, ','.join(sorted(filled)), err, ts()))
    return status, used_ai


def _since(hours):
    try:
        now = _core()._ist_now()
    except Exception:
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    return (now - datetime.timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%S')


def _capped(oid):
    try:
        return bool(_core().over_token_cap(oid))
    except Exception:
        return False


def _next_backfill_cid(conn, oid):
    r = conn.execute('SELECT c.id FROM candidates c LEFT JOIN insight_tag_log l ON l.candidate_id=c.id '
                     'WHERE c.owner_id=? AND l.candidate_id IS NULL AND ' + _NEEDS_SQL +
                     ' ORDER BY COALESCE(c.updated_at, c.created_at) DESC, c.id DESC LIMIT 1', (oid,)).fetchone()
    return r['id'] if r else None


def _remaining(conn, oid):
    return conn.execute('SELECT COUNT(*) FROM candidates c LEFT JOIN insight_tag_log l ON l.candidate_id=c.id '
                        'WHERE c.owner_id=? AND l.candidate_id IS NULL AND ' + _NEEDS_SQL, (oid,)).fetchone()[0]


def _next_job(conn):
    """('new'|'backfill', owner, candidate) or None. New candidates first."""
    rows = conn.execute('SELECT c.id, c.owner_id FROM candidates c LEFT JOIN insight_tag_log l ON l.candidate_id=c.id '
                        'WHERE l.candidate_id IS NULL AND COALESCE(c.owner_id,0)>0 AND c.created_at>=? AND ' + _NEEDS_SQL +
                        ' ORDER BY c.id DESC LIMIT 50', (_since(NEW_WINDOW_HOURS),)).fetchall()
    seen = {}
    for r in rows:
        oid = r['owner_id']
        if oid not in seen:
            seen[oid] = capture_on(conn, oid) and not _capped(oid)
        if seen[oid]:
            return 'new', oid, r['id']
    for b in conn.execute("SELECT owner_id FROM insight_backfill WHERE status='running' ORDER BY updated_at").fetchall():
        oid = b['owner_id']
        if _capped(oid):
            conn.execute("UPDATE insight_backfill SET status='capped', message=?, updated_at=? WHERE owner_id=?",
                         ('Stopped: the monthly AI limit is reached. Start again next month or raise the limit.', ts(), oid))
            conn.commit()
            continue
        cid = _next_backfill_cid(conn, oid)
        if cid is None:
            conn.execute("UPDATE insight_backfill SET status='done', message='', finished_at=?, updated_at=? WHERE owner_id=?",
                         (ts(), ts(), oid))
            conn.commit()
            continue
        return 'backfill', oid, cid
    return None


_tick_lock = threading.Lock()


def tick(classify=None):
    """Tag one person. Returns True when an AI call was made (so the loop waits)."""
    with _tick_lock:
        core = _core()
        conn = get_db()
        try:
            job = _next_job(conn)
            if not job:
                return False
            kind, oid, cid = job
            core._bg_tenant_set(oid)
            try:
                status, used = tag_person(conn, oid, cid, classify)
                if kind == 'backfill':
                    conn.execute('UPDATE insight_backfill SET done=done+1, failed=failed+?, message=?, updated_at=? '
                                 'WHERE owner_id=?', (1 if status == 'error' else 0, '', ts(), oid))
                conn.commit()
                return used
            except NoKey:
                conn.rollback()
                if kind == 'backfill':
                    conn.execute("UPDATE insight_backfill SET status='stopped', message=?, updated_at=? WHERE owner_id=?",
                                 ('No DeepSeek API key. Add it in Settings → Admin → AI API Keys, then start again.', ts(), oid))
                    conn.commit()
                return 'nokey'
            except Exception as e:
                conn.rollback()
                if type(e).__name__ == 'TokenCapError':
                    conn.execute("UPDATE insight_backfill SET status='capped', message=?, updated_at=? WHERE owner_id=?",
                                 ('Stopped: the monthly AI limit is reached.', ts(), oid))
                    conn.commit()
                    return False
                raise
            finally:
                core._bg_tenant_clear()
        finally:
            conn.close()


def _loop():
    time.sleep(45)
    while True:
        try:
            r = tick()
        except Exception as e:
            print('[insight] tagger error: %s' % e)
            r = None
        if r is True:                       # an AI call was made: throttle
            wait = THROTTLE_SECONDS
        elif r is False and _has_more():    # no AI needed for that one: carry on
            wait = 0.2
        else:                               # nothing to do, no key, or an error
            wait = IDLE_SECONDS
        time.sleep(wait)


def _has_more():
    """Cheap check so a run of no-AI records does not wait a minute each."""
    try:
        conn = get_db()
        try:
            return _next_job(conn) is not None
        finally:
            conn.close()
    except Exception:
        return False


_started = False


def start_worker():
    global _started
    if _started or 'pytest' in sys.modules or os.environ.get('HIRELAB_NO_WORKERS'):
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name='insight-tagger').start()
    print('[insight] tagging thread started')


start_worker()


# ══════════════════════════════════════════════════════════════════════════
#  1e  The 6:30 check
# ══════════════════════════════════════════════════════════════════════════
def _post_interested():
    s = set(_POST_INTERESTED)
    try:
        raw = _core().get_setting('pipeline_stages', '') or ''
        lst = json.loads(raw) if raw else []
        names = [str(x if isinstance(x, str) else (x or {}).get('id') or '').strip().lower() for x in lst]
        names = [n for n in names if n]
        if 'interested' in names:
            for n in names[names.index('interested'):]:
                if not family_for(n):
                    s.add(n)
    except Exception:
        pass
    return s


def check_630(conn, oid, uid):
    today = ts()[:10]
    ids = set()
    try:
        ids |= {r[0] for r in conn.execute(
            'SELECT DISTINCT sh.candidate_id FROM stage_history sh JOIN candidates c ON c.id=sh.candidate_id '
            'WHERE c.owner_id=? AND sh.actor_id=? AND sh.created_at>=?', (oid, uid, today))}
    except Exception:
        pass
    ids |= {r[0] for r in conn.execute('SELECT DISTINCT candidate_id FROM candidate_reasons WHERE owner_id=? AND user_id=? '
                                       'AND created_at>=?', (oid, uid, today))}
    ids |= {r[0] for r in conn.execute('SELECT DISTINCT candidate_id FROM market_notes WHERE owner_id=? AND user_id=? '
                                       'AND created_at>=? AND candidate_id>0', (oid, uid, today))}
    try:
        ids |= {r[0] for r in conn.execute('SELECT DISTINCT candidate_id FROM candidate_call_analysis WHERE owner_id=? '
                                           'AND created_by=? AND created_at>=?', (oid, uid, today))}
    except Exception:
        pass
    rows, counts = [], {'missing_reason': 0, 'missing_feedback': 0, 'ctc_zero': 0}
    if not ids:
        return rows, counts
    sc, sp = _scope()
    post = _post_interested()
    idl = sorted(ids)[:500]
    cands = conn.execute('SELECT c.id, c.name, c.company, c.designation, c.stage, c.mandate_id, c.ctc_expected, '
                         'm.role AS m_role, m.client AS m_client FROM candidates c LEFT JOIN mandates m ON m.id=c.mandate_id '
                         'WHERE c.owner_id=? AND c.id IN (%s)' % ','.join('?' * len(idl)) + sc,
                         [oid] + idl + sp).fetchall()
    for c in cands:
        stage = c['stage'] or ''
        fam = family_for(stage)
        issues = []
        if fam:
            mv = conn.execute('SELECT MAX(created_at) FROM stage_history WHERE candidate_id=? AND to_stage=? '
                              "AND COALESCE(from_stage,'')!=to_stage", (c['id'], stage)).fetchone()[0] or ''
            rr = conn.execute("SELECT MAX(created_at), MAX(CASE WHEN COALESCE(client_feedback,'')!='' THEN created_at END) "
                              'FROM candidate_reasons WHERE owner_id=? AND candidate_id=? AND LOWER(stage)=LOWER(?)',
                              (oid, c['id'], stage)).fetchone()
            if not rr[0] or rr[0] < mv:
                issues.append('missing_reason')
            if fam == 'client_reject' and (not rr[1] or rr[1] < mv):
                issues.append('missing_feedback')
        elif stage.strip().lower() in post and not float(c['ctc_expected'] or 0):
            issues.append('ctc_zero')
        if issues:
            for i in issues:
                counts[i] += 1
            rows.append({'id': c['id'], 'name': c['name'] or '', 'company': c['company'] or '',
                         'designation': c['designation'] or '', 'stage': stage, 'family': fam,
                         'mandate_id': c['mandate_id'], 'job': c['m_role'] or '', 'client': c['m_client'] or '',
                         'issues': issues})
    rows.sort(key=lambda r: (r['name'] or '').lower())
    return rows, counts


# ══════════════════════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════════════════════
@pages.route('/insight-capture.js')
def insight_js():
    with open(os.path.join(ROOT, 'insight-capture.js'), encoding='utf-8') as f:
        return Response(f.read(), mimetype='application/javascript',
                        headers={'Cache-Control': 'no-cache'})


def catalog():
    return {
        'families': {f: {'title': FAMILY_TITLE[f], 'stages': list(FAMILY_STAGES[f]),
                         'codes': [[c, l] for c, l in REASONS[f]], 'needs_feedback': f == 'client_reject'}
                     for f in REASONS},
        'signals': [[s, l] for s, l in SIGNALS],
        'role_families': ROLE_FAMILIES, 'domains': DOMAINS, 'levels': LEVELS,
    }


@bp.route('/api/insight/config', methods=['GET'])
@login_required
def get_config():
    conn = get_db()
    try:
        on = capture_on(conn, effective_company_id())
    finally:
        conn.close()
    return jsonify({'ok': True, 'capture_on': on, 'is_admin': bool(is_company_admin()), 'catalog': catalog()})


@bp.route('/api/insight/config', methods=['POST'])
@login_required
def set_config():
    if not is_company_admin():
        return jsonify({'error': 'Only an admin can change this.'}), 403
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        if 'capture_on' in d:
            _set_setting(conn, effective_company_id(), 'insight_capture_on', '1' if d.get('capture_on') else '0')
        conn.commit()
        on = capture_on(conn, effective_company_id())
    finally:
        conn.close()
    return jsonify({'ok': True, 'capture_on': on})


@bp.route('/api/candidates/<int:cid>/reasons', methods=['GET'])
@login_required
def list_reasons(cid):
    conn = get_db()
    try:
        if not _own_cand(conn, cid):
            return jsonify({'error': 'Not found'}), 404
        rows = conn.execute('SELECT * FROM candidate_reasons WHERE owner_id=? AND candidate_id=? ORDER BY id DESC',
                            (effective_company_id(), cid)).fetchall()
    finally:
        conn.close()
    return jsonify({'ok': True, 'reasons': [_reason_out(r) for r in rows]})


@bp.route('/api/candidates/<int:cid>/reasons', methods=['POST'])
@login_required
def add_reason(cid):
    blocked = _fl_block()
    if blocked:
        return blocked
    d = request.get_json(silent=True) or {}
    oid = effective_company_id()
    conn = get_db()
    try:
        c = _own_cand(conn, cid)
        if not c:
            return jsonify({'error': 'Not found'}), 404
        try:
            rid = record_reason(conn, oid, cid, c['mandate_id'], c['stage'], d.get('reason_code'),
                                d.get('reason_detail'), d.get('client_feedback'),
                                'bulk' if d.get('reason_source') == 'bulk' else 'manual', _uid())
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                     (cid, c['stage'], c['stage'], reason_note(c['stage'], d.get('reason_code'), d.get('reason_detail')), ts()))
        conn.commit()
        r = conn.execute('SELECT * FROM candidate_reasons WHERE id=?', (rid,)).fetchone()
    finally:
        conn.close()
    return jsonify({'ok': True, 'reason': _reason_out(r)})


@bp.route('/api/candidates/<int:cid>/market-notes', methods=['GET'])
@login_required
def list_notes(cid):
    conn = get_db()
    try:
        if not _own_cand(conn, cid):
            return jsonify({'error': 'Not found'}), 404
        rows = conn.execute('SELECT * FROM market_notes WHERE owner_id=? AND candidate_id=? ORDER BY id DESC LIMIT 50',
                            (effective_company_id(), cid)).fetchall()
    finally:
        conn.close()
    return jsonify({'ok': True, 'notes': [dict(r) for r in rows]})


@bp.route('/api/candidates/<int:cid>/market-notes', methods=['POST'])
@login_required
def add_note(cid):
    blocked = _fl_block()
    if blocked:
        return blocked
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        c = _own_cand(conn, cid)
        if not c:
            return jsonify({'error': 'Not found'}), 404
        try:
            nid = add_market_note(conn, effective_company_id(), cid, c['mandate_id'],
                                  d.get('company') or c['company'], d.get('signal_type') or 'other', d.get('note'),
                                  'manual', _uid())
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        conn.commit()
        r = conn.execute('SELECT * FROM market_notes WHERE id=?', (nid,)).fetchone()
    finally:
        conn.close()
    return jsonify({'ok': True, 'note': dict(r)})


def _capture_out(conn, c):
    oid = effective_company_id()
    notes = conn.execute('SELECT * FROM market_notes WHERE owner_id=? AND candidate_id=? ORDER BY id DESC LIMIT 5',
                         (oid, c['id'])).fetchall()
    lr = latest_reason(conn, oid, c['id'])
    return {'id': c['id'], 'name': c['name'] or '', 'company': c['company'] or '', 'stage': c['stage'] or '',
            'ctc_current': c['ctc_current'] or 0, 'ctc_expected': c['ctc_expected'] or 0,
            'notice_period': c['notice_period'] or 0, 'team_direct': c['team_direct'],
            'team_indirect': c['team_indirect'], 'yearly_target': c['yearly_target'],
            'role_family': c['role_family'] or '', 'level_band': c['level_band'] or '',
            'family': family_for(c['stage']), 'reason': _reason_out(lr) if lr else None,
            'notes': [dict(n) for n in notes]}


@bp.route('/api/candidates/<int:cid>/capture', methods=['GET'])
@login_required
def get_capture(cid):
    conn = get_db()
    try:
        c = _own_cand(conn, cid)
        if not c:
            return jsonify({'error': 'Not found'}), 404
        out = _capture_out(conn, c)
    finally:
        conn.close()
    return jsonify({'ok': True, 'capture': out})


def _num(v, cast=float):
    if v is None or str(v).strip() == '':
        return None
    try:
        n = cast(float(str(v).replace(',', '').strip()))
    except Exception:
        raise ValueError('"%s" is not a number.' % v)
    if n < 0:
        raise ValueError('Numbers cannot be negative.')
    return n


@bp.route('/api/candidates/<int:cid>/capture', methods=['PUT'])
@login_required
def save_capture(cid):
    blocked = _fl_block()
    if blocked:
        return blocked
    d = request.get_json(silent=True) or {}
    core = _core()
    conn = get_db()
    try:
        c = _own_cand(conn, cid)
        if not c:
            return jsonify({'error': 'Not found'}), 404
        sets = {}
        try:
            if 'ctc_expected' in d and _num(d.get('ctc_expected')) is not None:
                sets['ctc_expected'] = _num(d.get('ctc_expected'))
            if 'notice_period' in d and _num(d.get('notice_period'), int) is not None:
                sets['notice_period'] = _num(d.get('notice_period'), int)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        before = dict(c)
        if sets:
            conn.execute('UPDATE candidates SET ' + ', '.join(k + '=?' for k in sets) + ', updated_at=? WHERE id=?',
                         list(sets.values()) + [ts(), cid])
        sales = {k: d.get(k) for k in ('team_direct', 'team_indirect', 'yearly_target') if k in d}
        if sales:
            core.save_sales_profile(conn, cid, sales)
        changed = list(sets) + list(sales)
        if changed:
            try:
                core._sync_person_fields(conn, cid, changed)
            except Exception:
                pass
        note = d.get('market_note') if isinstance(d.get('market_note'), dict) else None
        if note and _clean(note.get('note')):
            try:
                add_market_note(conn, effective_company_id(), cid, c['mandate_id'], note.get('company') or c['company'],
                                note.get('signal_type') or 'other', note.get('note'), 'manual', _uid())
            except ValueError as e:
                conn.rollback()
                return jsonify({'error': str(e)}), 400
        conn.commit()
        after = conn.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
        out = _capture_out(conn, after)
    finally:
        conn.close()
    try:
        for k in changed:
            if before.get(k) != after[k]:
                core.log_audit('candidate', cid, k, before.get(k), after[k])
    except Exception:
        pass
    return jsonify({'ok': True, 'capture': out})


@bp.route('/api/insight/check-630', methods=['GET'])
@login_required
def get_check_630():
    conn = get_db()
    try:
        oid = effective_company_id()
        if not capture_on(conn, oid):
            return jsonify({'ok': True, 'on': False, 'rows': [], 'counts': {}, 'total': 0})
        rows, counts = check_630(conn, oid, _uid())
    finally:
        conn.close()
    return jsonify({'ok': True, 'on': True, 'rows': rows, 'counts': counts, 'total': len(rows)})


def _bf_status(conn, oid):
    b = conn.execute('SELECT * FROM insight_backfill WHERE owner_id=?', (oid,)).fetchone()
    d = dict(b) if b else {'status': 'idle', 'total': 0, 'done': 0, 'failed': 0, 'message': ''}
    d['remaining'] = _remaining(conn, oid)
    d['tagged'] = conn.execute("SELECT COUNT(*) FROM insight_tag_log WHERE owner_id=? AND status='done'", (oid,)).fetchone()[0]
    d.pop('owner_id', None)
    return d


@bp.route('/api/insight/backfill/status', methods=['GET'])
@login_required
def backfill_status():
    if not is_company_admin():
        return jsonify({'error': 'Only an admin can see this.'}), 403
    conn = get_db()
    try:
        out = _bf_status(conn, effective_company_id())
    finally:
        conn.close()
    return jsonify({'ok': True, 'backfill': out})


@bp.route('/api/insight/backfill/start', methods=['POST'])
@login_required
def backfill_start():
    if not is_company_admin():
        return jsonify({'error': 'Only an admin can start this.'}), 403
    oid = effective_company_id()
    conn = get_db()
    try:
        # records that failed before get another try
        conn.execute("DELETE FROM insight_tag_log WHERE owner_id=? AND status='error'", (oid,))
        total = _remaining(conn, oid)
        b = conn.execute('SELECT status FROM insight_backfill WHERE owner_id=?', (oid,)).fetchone()
        if b and b['status'] == 'running':
            conn.execute('UPDATE insight_backfill SET total=done+?, updated_at=? WHERE owner_id=?', (total, ts(), oid))
        else:
            conn.execute('INSERT OR REPLACE INTO insight_backfill (owner_id, status, total, done, failed, message, '
                         'started_by, started_at, updated_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (oid, 'running' if total else 'done', total, 0, 0, '', _uid(), ts(), ts(), '' if total else ts()))
        conn.commit()
        out = _bf_status(conn, oid)
    finally:
        conn.close()
    return jsonify({'ok': True, 'backfill': out})


@bp.route('/api/insight/backfill/stop', methods=['POST'])
@login_required
def backfill_stop():
    if not is_company_admin():
        return jsonify({'error': 'Only an admin can stop this.'}), 403
    oid = effective_company_id()
    conn = get_db()
    try:
        conn.execute("UPDATE insight_backfill SET status='stopped', message='Stopped by an admin.', updated_at=? "
                     "WHERE owner_id=? AND status='running'", (ts(), oid))
        conn.commit()
        out = _bf_status(conn, oid)
    finally:
        conn.close()
    return jsonify({'ok': True, 'backfill': out})
