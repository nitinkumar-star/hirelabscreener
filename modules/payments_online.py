"""
RecruitOS — Online payment with Razorpay + automatic activation  (Nov 2026)

When an agency's free trial (or a paid month) ends, the agency admin pays
online — UPI, card, net-banking — and the ATS opens again by itself. No
monthly fee: Razorpay only takes a per-payment fee. Nothing changes until the
keys are set; without them the "How to pay" text (manual UPI) is shown.

  GET  /api/billing/pay/config     is online payment on? amount, key id
  POST /api/billing/pay/order      create a Razorpay order for this month's bill
  POST /api/billing/pay/verify     checkout success -> check the signature -> payment recorded, agency active
  POST /api/billing/razorpay/webhook   Razorpay -> us (backup if the browser closed before /verify)

Each payment: one row in payments (invoice number, method 'razorpay', the
Razorpay payment id), billing_status 'active', and paid_until moved forward by
one billing period (server.extend_paid_until). The same payment arriving twice
(verify + webhook) is recorded once.

Keys (Render → Environment):
  RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET    Razorpay Dashboard → Account & Settings → API Keys
  RAZORPAY_WEBHOOK_SECRET                 Dashboard → Webhooks → URL <your app>/api/billing/razorpay/webhook,
                                          events payment.captured and order.paid
"""

import os
import hmac
import json
import hashlib

import requests
from flask import Blueprint, request, jsonify

from modules.shared import get_db, ts, effective_company_id, login_required, _core
from modules import register_migration

bp = Blueprint('payments_online', __name__, url_prefix='/api/billing')

API = 'https://api.razorpay.com/v1'


@register_migration
def migrate(conn):
    for col in ("paid_until TEXT DEFAULT ''",):
        try:
            conn.execute('ALTER TABLE companies ADD COLUMN ' + col)
        except Exception:
            pass
    try:
        conn.execute("ALTER TABLE payments ADD COLUMN gateway_ref TEXT DEFAULT ''")
    except Exception:
        pass
    conn.execute('''CREATE TABLE IF NOT EXISTS online_orders (
        order_id TEXT PRIMARY KEY,
        company_id INTEGER NOT NULL,
        user_id INTEGER DEFAULT 0,
        amount_inr REAL DEFAULT 0,
        status TEXT DEFAULT 'created',
        payment_id TEXT DEFAULT '',
        created_at TEXT,
        paid_at TEXT DEFAULT ''
    )''')
    conn.commit()


def keys():
    return ((os.environ.get('RAZORPAY_KEY_ID', '') or '').strip(),
            (os.environ.get('RAZORPAY_KEY_SECRET', '') or '').strip())


def enabled():
    k, s = keys()
    return bool(k and s)


def _sig(secret, msg):
    return hmac.new(secret.encode(), msg.encode() if isinstance(msg, str) else msg, hashlib.sha256).hexdigest()


def _bill(cid):
    b = _core().compute_company_bill(cid, 30) or {}
    return round(float(b.get('total_inr') or 0), 2), b


def _admin_ok():
    core = _core()
    return core.is_company_admin() or core.is_admin()


def record_online_payment(conn, cid, amount, payment_id, order_id=''):
    """Record a captured payment once and open the ATS for one more period."""
    core = _core()
    if payment_id and conn.execute('SELECT 1 FROM payments WHERE gateway_ref=?', (payment_id,)).fetchone():
        return None                                    # already recorded (verify + webhook)
    invoice_no = core._next_invoice_no(conn)
    import datetime
    conn.execute('INSERT INTO payments (company_id,invoice_no,amount_inr,period,method,note,created_at,gateway_ref) '
                 'VALUES (?,?,?,?,?,?,?,?)',
                 (cid, invoice_no, amount, datetime.date.today().strftime('%b %Y'), 'razorpay',
                  'Online payment ' + (order_id or ''), ts(), payment_id))
    conn.execute("UPDATE companies SET billing_status='active', status='active' WHERE id=?", (cid,))
    until = core.extend_paid_until(conn, cid)
    if order_id:
        conn.execute("UPDATE online_orders SET status='paid', payment_id=?, paid_at=? WHERE order_id=?",
                     (payment_id, ts(), order_id))
    conn.commit()
    try:
        name = (conn.execute('SELECT name FROM companies WHERE id=?', (cid,)).fetchone() or {'name': cid})['name']
        core.log_activity('payment', f'{name} ₹{amount} ({invoice_no}, Razorpay {payment_id})')
    except Exception:
        pass
    return {'invoice_no': invoice_no, 'paid_until': until}


@bp.route('/pay/config', methods=['GET'])
@login_required
def pay_config():
    cid = effective_company_id()
    amount, b = _bill(cid)
    me = _core().current_user() or {}
    return jsonify({'ok': True, 'enabled': enabled(), 'key_id': keys()[0] if enabled() else '',
                    'amount_inr': amount, 'company': b.get('company', ''), 'recruiters': b.get('recruiters', 0),
                    'email': me.get('email') or '', 'name': me.get('display_name') or me.get('username') or '',
                    'paid_until': b.get('paid_until', ''), 'can_pay': _admin_ok()})


@bp.route('/pay/order', methods=['POST'])
@login_required
def pay_order():
    if not enabled():
        return jsonify({'error': 'Online payment is not switched on yet.'}), 400
    if not _admin_ok():
        return jsonify({'error': 'Only your agency admin can pay the subscription.'}), 403
    cid = effective_company_id()
    amount, b = _bill(cid)
    if amount < 1:
        return jsonify({'error': 'Nothing to pay right now.'}), 400
    key_id, secret = keys()
    try:
        r = requests.post(API + '/orders', auth=(key_id, secret), timeout=20, json={
            'amount': int(round(amount * 100)), 'currency': 'INR', 'receipt': ('co%d-%s' % (cid, ts()))[:40],
            'notes': {'company_id': str(cid), 'company': (b.get('company') or '')[:200]}})
        o = r.json()
    except Exception as e:
        return jsonify({'error': 'Could not reach Razorpay: %s' % str(e)[:120]}), 502
    if r.status_code >= 300 or not o.get('id'):
        return jsonify({'error': 'Razorpay: ' + str(((o.get('error') or {}).get('description')) or r.status_code)}), 502
    conn = get_db()
    try:
        conn.execute('INSERT OR REPLACE INTO online_orders (order_id, company_id, user_id, amount_inr, status, created_at) '
                     "VALUES (?,?,?,?,'created',?)", (o['id'], cid, int(_core().real_user_id() or 0), amount, ts()))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True, 'order_id': o['id'], 'amount_paise': int(round(amount * 100)), 'amount_inr': amount,
                    'key_id': key_id, 'company': b.get('company', '')})


@bp.route('/pay/verify', methods=['POST'])
@login_required
def pay_verify():
    d = request.get_json(silent=True) or {}
    oid, pid, sig = (str(d.get(k) or '') for k in ('razorpay_order_id', 'razorpay_payment_id', 'razorpay_signature'))
    _key, secret = keys()
    if not (enabled() and oid and pid and sig):
        return jsonify({'error': 'Payment details missing.'}), 400
    if not hmac.compare_digest(_sig(secret, oid + '|' + pid), sig):
        return jsonify({'error': 'The payment could not be verified. If money was taken, it will be matched automatically or contact us.'}), 400
    conn = get_db()
    try:
        o = conn.execute('SELECT * FROM online_orders WHERE order_id=?', (oid,)).fetchone()
        if not o or int(o['company_id']) != int(effective_company_id() or 0):
            return jsonify({'error': 'Unknown order.'}), 404
        res = record_online_payment(conn, int(o['company_id']), float(o['amount_inr'] or 0), pid, oid)
        if res is None:
            row = conn.execute('SELECT paid_until FROM companies WHERE id=?', (o['company_id'],)).fetchone()
            res = {'paid_until': row['paid_until'] if row else '', 'already': True}
    finally:
        conn.close()
    return jsonify(dict(res, ok=True))


@bp.route('/razorpay/webhook', methods=['POST'])
def razorpay_webhook():
    secret = (os.environ.get('RAZORPAY_WEBHOOK_SECRET', '') or '').strip()
    raw = request.get_data() or b''
    if not secret or not hmac.compare_digest(_sig(secret, raw), request.headers.get('X-Razorpay-Signature', '')):
        return jsonify({'error': 'bad signature'}), 400
    try:
        ev = json.loads(raw.decode('utf-8'))
    except Exception:
        return jsonify({'error': 'bad body'}), 400
    if ev.get('event') not in ('payment.captured', 'order.paid'):
        return jsonify({'ok': True, 'ignored': True})
    pay = (((ev.get('payload') or {}).get('payment') or {}).get('entity')) or {}
    oid, pid = pay.get('order_id') or '', pay.get('id') or ''
    if not (oid and pid):
        return jsonify({'ok': True, 'ignored': True})
    conn = get_db()
    try:
        o = conn.execute('SELECT * FROM online_orders WHERE order_id=?', (oid,)).fetchone()
        if not o:
            return jsonify({'ok': True, 'ignored': True})      # not an ATS subscription order
        paid = round(float(pay.get('amount') or 0) / 100.0, 2) or float(o['amount_inr'] or 0)
        record_online_payment(conn, int(o['company_id']), paid, pid, oid)
    finally:
        conn.close()
    return jsonify({'ok': True})
