"""
RecruitOS — Per-recruiter templates  (Oct 2026)

A recruiter sees the company's email + WhatsApp templates and can add their
own, or customise a company one (their copy wins, for them only). The WhatsApp
outreach sequence (first message, follow-up 1, follow-up 2) and the interview
message can be personal too. The admin can look at a team member's templates
and turn a good one into a company template.

Storage (additive): user_templates(user_id, key) -> value.
  key = email_templates | wa_templates   (JSON: only the recruiter's own items)
        template_msg1 | template_fu1 | template_fu2 | interview_template (text)

Resolution lives in server.py (get_setting scalar overlay, tpl_list_for for
the two lists, used by /api/email-templates and /api/wa-templates).

ROLLBACK: remove 'user_templates' from modules/__init__.py. Recruiters then
see only the company templates again; the table can stay.
"""

import json
import time

from flask import Blueprint, request, jsonify, session

from modules.shared import get_db, ts, _core, current_user, effective_company_id, is_company_admin, \
    login_required, log_activity
from modules import register_migration

bp = Blueprint('user_templates', __name__)

SCALARS = ('template_msg1', 'template_fu1', 'template_fu2', 'interview_template')
LISTS = ('email_templates', 'wa_templates')


@register_migration
def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS user_templates (
        user_id INTEGER NOT NULL,
        company_id INTEGER DEFAULT 0,
        key TEXT NOT NULL,
        value TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        PRIMARY KEY (user_id, key)
    )''')
    conn.commit()


def _company_scalar(key):
    core = _core()
    with core.mail_sender(0):
        return core.get_setting(key, '') or ''


# ── the logged-in recruiter ────────────────────────────────────────────────
@bp.route('/api/me/templates', methods=['GET'])
@login_required
def my_templates_get():
    core = _core()
    uid = core._tpl_uid()
    out = {'personal': bool(uid), 'scalars': {}}
    for k in SCALARS:
        mine = core.tpl_personal_get(uid, k) if uid else ''
        out['scalars'][k] = {'mine': mine, 'company': _company_scalar(k)}
    return jsonify({'ok': True, 'templates': out})


@bp.route('/api/me/templates', methods=['PUT', 'POST'])
@login_required
def my_templates_put():
    """Body: {template_msg1: '...', ...}. Blank = go back to the company text."""
    core = _core()
    uid = core._tpl_uid()
    if not uid:
        return jsonify({'error': 'Admins edit the company templates in Settings → Communication.'}), 400
    d = request.json or {}
    changed = 0
    for k in SCALARS:
        if k in d:
            v = (d.get(k) or '')
            if v.strip() == _company_scalar(k).strip():
                v = ''               # identical to the company text = not personal
            core.tpl_personal_set(uid, k, v if v.strip() else '')
            changed += 1
    return jsonify({'ok': True, 'changed': changed})


# ── admin: a team member's templates ──────────────────────────────────────
def _member(conn, uid):
    me = current_user() or {}
    u = conn.execute('SELECT id, company_id, role, display_name, username FROM users WHERE id=?', (uid,)).fetchone()
    if not u:
        return None, ('User not found', 404)
    if me.get('role') == 'admin' or (is_company_admin() and u['company_id'] == effective_company_id()):
        return dict(u), None
    return None, ('Admin access required', 403)


@bp.route('/api/team/<int:uid>/templates', methods=['GET'])
@login_required
def team_templates_get(uid):
    conn = get_db()
    u, err = _member(conn, uid)
    conn.close()
    if err:
        return jsonify({'error': err[0]}), err[1]
    core = _core()
    items = []
    try:
        for t in json.loads(core.tpl_personal_get(uid, 'email_templates') or '[]'):
            items.append({'kind': 'email', 'id': t.get('id'), 'title': t.get('title', ''),
                          'subject': t.get('subject', ''), 'body': t.get('body', '')})
    except Exception:
        pass
    try:
        for c in json.loads(core.tpl_personal_get(uid, 'wa_templates') or '[]'):
            for it in c.get('items') or []:
                items.append({'kind': 'whatsapp', 'id': it.get('id'), 'cat': c.get('cat', ''),
                              'title': it.get('title', ''), 'body': it.get('body', '')})
    except Exception:
        pass
    for k in SCALARS:
        v = core.tpl_personal_get(uid, k)
        if v.strip():
            items.append({'kind': k, 'id': k, 'title': {
                'template_msg1': 'WhatsApp first outreach', 'template_fu1': 'WhatsApp follow-up 1',
                'template_fu2': 'WhatsApp follow-up 2', 'interview_template': 'Interview message'}[k], 'body': v})
    return jsonify({'ok': True, 'user': {'id': u['id'], 'name': u['display_name'] or u['username']},
                    'items': items})


@bp.route('/api/team/<int:uid>/templates/promote', methods=['POST'])
@login_required
def team_templates_promote(uid):
    """Make one of a recruiter's templates a COMPANY template (everyone gets it).
    Body: {kind: 'email'|'whatsapp'|<scalar key>, id}."""
    conn = get_db()
    u, err = _member(conn, uid)
    conn.close()
    if err:
        return jsonify({'error': err[0]}), err[1]
    core = _core()
    d = request.json or {}
    kind, tid = d.get('kind'), str(d.get('id') or '')
    with core.mail_sender(0):
        if kind == 'email':
            mine = json.loads(core.tpl_personal_get(uid, 'email_templates') or '[]')
            t = next((x for x in mine if str(x.get('id')) == tid), None)
            if not t:
                return jsonify({'error': 'Template not found'}), 404
            company = core._tpl_company_list('email_templates', core.EMAIL_DEFAULT_TEMPLATES)
            new = core._tpl_clean(t)
            idx = next((i for i, x in enumerate(company) if str(x.get('id')) == tid), None)
            if idx is None:
                new['id'] = 'c%d' % int(time.time() * 1000)
                company.append(new)
            else:
                company[idx] = new        # a customised company template replaces the original
            core.set_setting('email_templates', json.dumps(company))
            rest = [x for x in mine if str(x.get('id')) != tid]   # now a company one
            core.tpl_personal_set(uid, 'email_templates', json.dumps(rest) if rest else '')
            title = new.get('title', '')
        elif kind == 'whatsapp':
            mine = json.loads(core.tpl_personal_get(uid, 'wa_templates') or '[]')
            hit = None
            for c in mine:
                for it in c.get('items') or []:
                    if str(it.get('id')) == tid:
                        hit = (c.get('cat') or 'My templates', core._tpl_clean(it))
            if not hit:
                return jsonify({'error': 'Template not found'}), 404
            company = core._tpl_company_list('wa_templates', core.WA_DEFAULT_TEMPLATES)
            cat, new = hit
            done = False
            for c in company:
                for i, it in enumerate(c.get('items') or []):
                    if str(it.get('id')) == tid:
                        c['items'][i] = new; done = True
            if not done:
                new['id'] = 'cwa%d' % int(time.time() * 1000)
                tgt = next((c for c in company if c.get('cat') == cat), None)
                if tgt is None:
                    tgt = {'cat': cat, 'items': []}; company.append(tgt)
                tgt.setdefault('items', []).append(new)
            core.set_setting('wa_templates', json.dumps(company))
            rest = []
            for c in mine:
                its = [it for it in (c.get('items') or []) if str(it.get('id')) != tid]
                if its:
                    rest.append({'cat': c.get('cat'), 'items': its})
            core.tpl_personal_set(uid, 'wa_templates', json.dumps(rest) if rest else '')
            title = new.get('title', '')
        elif kind in SCALARS:
            v = core.tpl_personal_get(uid, kind)
            if not v.strip():
                return jsonify({'error': 'Template not found'}), 404
            core.set_setting(kind, v)
            core.tpl_personal_set(uid, kind, '')
            title = kind
        else:
            return jsonify({'error': 'Unknown template kind'}), 400
    try:
        log_activity('promote_template', 'Made "%s" a company template (from user #%d)' % (title, uid), 'user', uid)
    except Exception:
        pass
    return jsonify({'ok': True})
