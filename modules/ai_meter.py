"""
RecruitOS — AI cost meter in rupees  (Nov 2026)

Every AI call (DeepSeek, Claude, Whisper …) is already written to api_usage
with its company and an estimated USD cost (server.log_api_usage). This module
adds WHO made the call (api_usage.user_id, the signed-in user; 0 = automatic /
background work) and turns the numbers into rupees:

  GET /api/ai-meter?days=30            the caller's agency: total ₹, per recruiter, per feature, per day
                                       (agency admin; the platform owner may pass ?company=<id>)
  GET /api/ai-meter/agencies?days=30   platform owner: every agency in ₹ (cost and billed)

Rupees = cost_usd × billing_usd_inr. "Billed" also applies billing_token_markup,
the same numbers the agency's monthly bill uses (server.compute_company_bill).
"""

import datetime

from flask import Blueprint, request, jsonify

from modules.shared import get_db, effective_company_id, is_company_admin, login_required, _core
from modules import register_migration

bp = Blueprint('ai_meter', __name__, url_prefix='/api/ai-meter')

FEATURES = {
    'resume-parse': 'CV reading', 'parse': 'CV reading', 'bulk-parse': 'Bulk CV reading', 'extract': 'Data extraction',
    'screening': 'Screening', 'naukri-screen': 'Naukri screening', 'naukri-screen-chat': 'Naukri screening chat',
    'analysis': 'Candidate analysis', 'deep-analysis': 'Deep analysis', 'reasoning': 'AI reasoning', 'rerank': 'Matching',
    'email-agent': 'Email agent', 'ai-compose': 'Email drafting', 'crm_email_draft': 'BD email drafting',
    'outreach': 'Outreach drafting', 'candidate-agent': 'Candidate AI agent', 'agent': 'AI agent',
    'call-analysis': 'Call analysis', 'transcription': 'Call transcription', 'recruiter-pitch': 'Pitch script',
    'jd-rewriter': 'JD rewriter', 'jd-rewriter-fix': 'JD rewriter', 'jd_intel': 'JD intelligence',
    'newsletter-draft': 'Newsletter drafting', 'talent-segments': 'Talent segments', 'jobmail-verify': 'Job-match mail',
    'task-gen': 'Task suggestions', 'todo-quick-add': 'Smart task add', 'todo-daily-plan': 'Daily plan',
    'intent-judge': 'Reply understanding', 'finance-bill': 'Finance', 'finance-insight': 'Finance insights',
    'deepseek': 'Other AI', 'claude': 'Other AI', '': 'Other AI',
}


@register_migration
def migrate(conn):
    try:
        conn.execute('ALTER TABLE api_usage ADD COLUMN user_id INTEGER DEFAULT 0')
    except Exception:
        pass
    try:
        conn.execute('CREATE INDEX IF NOT EXISTS idx_api_usage_co_time ON api_usage(company_id, created_at)')
    except Exception:
        pass
    conn.commit()


def _rates():
    core = _core()
    try:
        usd_inr = float(core.get_setting('billing_usd_inr', '88') or 88)
    except Exception:
        usd_inr = 88.0
    try:
        markup = float(core.get_setting('billing_token_markup', '1.0') or 1.0)
    except Exception:
        markup = 1.0
    return usd_inr, markup


def _since(days):
    try:
        days = max(1, min(int(days or 30), 366))
    except Exception:
        days = 30
    return days, (_core()._ist_now() - datetime.timedelta(days=days)).isoformat()


def _rs(usd, rate):
    return round((usd or 0) * rate, 2)


def agency_meter(conn, cid, days):
    days, since = _since(days)
    usd_inr, markup = _rates()
    rate = usd_inr * markup
    names = {r['id']: (r['display_name'] or r['username']) for r in conn.execute(
        'SELECT id, username, display_name FROM users WHERE company_id=?', (cid,))}
    people = []
    for r in conn.execute('SELECT COALESCE(user_id,0) u, COUNT(*) n, SUM(input_tokens+output_tokens) tok, '
                          'SUM(audio_seconds) au, SUM(cost_usd) usd FROM api_usage WHERE company_id=? AND created_at>=? '
                          'GROUP BY COALESCE(user_id,0) ORDER BY usd DESC', (cid, since)):
        uid = int(r['u'] or 0)
        people.append({'user_id': uid, 'name': names.get(uid) or ('Automatic (background)' if not uid else 'Other user (support / former)'),
                       'calls': r['n'], 'tokens': int(r['tok'] or 0), 'audio_min': round((r['au'] or 0) / 60.0, 1),
                       'rupees': _rs(r['usd'], rate)})
    feats = {}
    for r in conn.execute('SELECT COALESCE(endpoint,"") e, COUNT(*) n, SUM(cost_usd) usd FROM api_usage '
                          'WHERE company_id=? AND created_at>=? GROUP BY COALESCE(endpoint,"")', (cid, since)):
        lbl = FEATURES.get(r['e'], r['e'].replace('-', ' ').replace('_', ' ').capitalize() or 'Other AI')
        f = feats.setdefault(lbl, {'feature': lbl, 'calls': 0, 'rupees': 0.0})
        f['calls'] += r['n']
        f['rupees'] = round(f['rupees'] + _rs(r['usd'], rate), 2)
    daily = {}
    for r in conn.execute('SELECT substr(created_at,1,10) d, SUM(cost_usd) usd FROM api_usage '
                          'WHERE company_id=? AND created_at>=? GROUP BY substr(created_at,1,10)', (cid, since)):
        daily[r['d']] = _rs(r['usd'], rate)
    today = _core()._ist_now().date()
    series = [{'day': (today - datetime.timedelta(days=i)).isoformat(),
               'rupees': daily.get((today - datetime.timedelta(days=i)).isoformat(), 0)} for i in range(min(days, 31) - 1, -1, -1)]
    total = round(sum(p['rupees'] for p in people), 2)
    return {'ok': True, 'days': days, 'company_id': cid, 'total_rupees': total,
            'calls': sum(p['calls'] for p in people), 'people': people,
            'features': sorted(feats.values(), key=lambda f: -f['rupees']), 'daily': series,
            'usd_inr': usd_inr, 'markup': markup}


@bp.route('', methods=['GET'])
@login_required
def my_meter():
    core = _core()
    me = core.current_user() or {}
    cid = effective_company_id()
    if me.get('role') == 'admin' and request.args.get('company'):
        try:
            cid = int(request.args['company'])
        except ValueError:
            return jsonify({'error': 'Bad company'}), 400
    elif not is_company_admin():
        return jsonify({'error': 'Only the agency admin can see AI costs.'}), 403
    conn = get_db()
    try:
        return jsonify(agency_meter(conn, cid, request.args.get('days')))
    finally:
        conn.close()


@bp.route('/agencies', methods=['GET'])
@login_required
def agencies():
    core = _core()
    if not core.is_admin():
        return jsonify({'error': 'Admin access required'}), 403
    days, since = _since(request.args.get('days'))
    usd_inr, markup = _rates()
    conn = get_db()
    try:
        names = {r['id']: r['name'] for r in conn.execute('SELECT id, name FROM companies')}
        rows = conn.execute('SELECT company_id c, COUNT(*) n, COUNT(DISTINCT CASE WHEN COALESCE(user_id,0)>0 THEN user_id END) people, '
                            'SUM(input_tokens+output_tokens) tok, SUM(cost_usd) usd FROM api_usage WHERE created_at>=? '
                            'GROUP BY company_id ORDER BY usd DESC', (since,)).fetchall()
    finally:
        conn.close()
    out = [{'company_id': r['c'], 'company': names.get(r['c'], '(unknown / deleted)'), 'calls': r['n'],
            'people': r['people'], 'tokens': int(r['tok'] or 0),
            'cost_rupees': _rs(r['usd'], usd_inr), 'billed_rupees': _rs(r['usd'], usd_inr * markup)} for r in rows]
    return jsonify({'ok': True, 'days': days, 'usd_inr': usd_inr, 'markup': markup, 'agencies': out,
                    'total_cost_rupees': round(sum(a['cost_rupees'] for a in out), 2),
                    'total_billed_rupees': round(sum(a['billed_rupees'] for a in out), 2)})
