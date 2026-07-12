from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
import sqlite3, json, os, datetime, requests, shutil, io, re, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
try:
    import pdfplumber
    HAS_PDF = True
except ImportError:
    HAS_PDF = False
try:
    from docx import Document as DocxDocument
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# ── Safety net: never let a raw BLOB (e.g. embedding_vec bytes) crash jsonify.
# Any bytes that reach serialization become null instead of a 500. Defense in
# depth — list endpoints also strip heavy embedding columns before returning.
try:
    from flask.json.provider import DefaultJSONProvider

    class _SafeJSONProvider(DefaultJSONProvider):
        def default(self, o):
            if isinstance(o, (bytes, bytearray, memoryview)):
                return None
            return super().default(o)

    app.json = _SafeJSONProvider(app)
except Exception as _json_err:
    print(f'[json-provider] safe provider not installed: {_json_err}')


# Heavy per-candidate columns the UI never needs; stripped from list/detail
# responses to keep payloads small and memory low (fixes OOM on large mandates).
_HEAVY_CAND_COLS = ('embedding', 'embedding_text', 'embedding_vec')

def _cand_public(row):
    """Row -> client-safe dict without the heavy embedding columns."""
    d = dict(row)
    for k in _HEAVY_CAND_COLS:
        d.pop(k, None)
    return d
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024  # 64MB max upload

@app.after_request
def add_cors_headers(resp):
    # Allow the Chrome extension (running on naukri.com) to call these APIs.
    # Because the extension sends the session cookie (credentials), we must
    # echo the specific Origin and allow credentials — '*' is not permitted
    # with credentialed requests.
    origin = request.headers.get('Origin')
    if origin:
        resp.headers['Access-Control-Allow-Origin'] = origin
        resp.headers['Access-Control-Allow-Credentials'] = 'true'
        resp.headers['Vary'] = 'Origin'
    else:
        resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp


# ── Login Protection ──────────────────────────────────────────────────────────
from functools import wraps
from flask import session, redirect as flask_redirect
import hashlib, secrets

def _get_secret_key():
    # 1) Prefer env var (set SECRET_KEY in Render for best security)
    env_secret = os.environ.get('SECRET_KEY', '').strip()
    if env_secret:
        return env_secret
    # 2) Else use/create a random secret stored on the persistent disk so login
    #    sessions survive restarts. This is far safer than a hardcoded fallback.
    try:
        secret_path = os.path.join(DATA_DIR, '.secret_key')
        if os.path.exists(secret_path):
            with open(secret_path) as f:
                v = f.read().strip()
                if v:
                    return v
        import secrets as _secrets
        v = _secrets.token_hex(32)
        with open(secret_path, 'w') as f:
            f.write(v)
        return v
    except Exception:
        # 3) Last resort (ephemeral) — sessions reset on restart, but never hardcoded
        import secrets as _secrets
        return _secrets.token_hex(32)

app.secret_key = _get_secret_key()

# ─────────────────────────────────────────────────────────────────────────
#  AUTH HELPERS (multi-user)
# ─────────────────────────────────────────────────────────────────────────
def hash_password(pw, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + pw).encode()).hexdigest()
    return salt + '$' + h

def verify_password(pw, stored):
    try:
        salt, h = stored.split('$', 1)
        return hashlib.sha256((salt + pw).encode()).hexdigest() == h
    except Exception:
        return False

def any_users_exist():
    conn = get_db()
    n = conn.execute('SELECT COUNT(*) n FROM users').fetchone()['n']
    conn.close()
    return n > 0

def current_user():
    """The logged-in user. If admin is 'viewing as' another tenant, the
    EFFECTIVE workspace is that tenant, but real identity stays admin."""
    uid = session.get('user_id')
    if not uid:
        return None
    conn = get_db()
    u = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    conn.close()
    return dict(u) if u else None

def current_company_id():
    """The company (tenant) of the logged-in user, ignoring any view-as."""
    u = current_user()
    return u.get('company_id') if u else None

def effective_user_id():
    """TENANT-ID this request operates on. Historically named for the
    single-user era; it now returns the effective COMPANY (tenant) id, which
    is what every data row's owner_id column stores. A super-admin can
    impersonate another tenant via 'view_as_company'. Kept under the old name
    so all existing `WHERE owner_id = effective_user_id()` filters keep working
    and enforce tenant isolation automatically."""
    va = session.get('view_as_company')
    if va:
        return va
    return current_company_id()

# Clearer alias for new code.
def effective_company_id():
    return effective_user_id()

def is_admin():
    u = current_user()
    return u and u.get('role') == 'admin'

def is_company_admin():
    """True if the logged-in user can manage their whole company (see all
    company mandates, assign them, delete jobs). This is the platform owner OR
    a user flagged as their company's admin. When a super-admin is viewing-as a
    company, they act as that company's admin."""
    if session.get('view_as_company'):
        return True  # super-admin impersonating a tenant acts as its admin
    u = current_user()
    if not u:
        return False
    return u.get('role') == 'admin' or u.get('is_company_admin') == 1

def real_user_id():
    """The actual logged-in user id (NOT the company id that effective_user_id
    returns). Used for per-recruiter mandate assignment."""
    return session.get('user_id')

def log_activity(action, detail='', entity_type='', entity_id=0, meta=None,
                 actor_type='user', actor_name=''):
    """Universal activity timeline. Backward-compatible: existing callers that
    pass only (action, detail) keep working. New callers can attach an entity
    (candidate/client/invoice), an actor (user/client/system) and a JSON meta
    payload that future workflow-automation can consume without schema changes."""
    try:
        u = current_user()
        uid = u['id'] if u else 0
        uname = actor_name or (u['username'] if u else 'system')
        try:
            company_id = effective_company_id()
        except Exception:
            company_id = 0
        meta_json = ''
        if meta is not None:
            try:
                meta_json = json.dumps(meta)
            except Exception:
                meta_json = ''
        conn = get_db()
        conn.execute(
            'INSERT INTO activity_log (user_id,username,action,detail,created_at,'
            'company_id,entity_type,entity_id,actor_type,actor_name,meta) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (uid, uname, action, detail, ts(), company_id, entity_type, entity_id,
             actor_type, uname, meta_json))
        conn.commit(); conn.close()
    except Exception:
        pass


def log_audit(entity_type, entity_id, field, old_value, new_value,
              actor_type='user', actor_name=''):
    """Record a single field change (old → new) for audit history."""
    try:
        u = current_user()
        uid = u['id'] if u else 0
        uname = actor_name or (u['username'] if u else 'system')
        try:
            company_id = effective_company_id()
        except Exception:
            company_id = 0
        conn = get_db()
        conn.execute(
            'INSERT INTO audit_log (company_id,entity_type,entity_id,field,old_value,'
            'new_value,actor_type,actor_id,actor_name,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (company_id, entity_type, entity_id, field, str(old_value or ''),
             str(new_value or ''), actor_type, uid, uname, ts()))
        conn.commit(); conn.close()
    except Exception:
        pass


def record_changes(entity_type, entity_id, before: dict, after: dict, fields,
                   actor_type='user', actor_name=''):
    """Diff two dicts across `fields` and write one audit row per changed field.
    Returns a human-readable summary list of the changes (for activity detail)."""
    changes = []
    for f in fields:
        old_v = before.get(f) if before else None
        new_v = after.get(f) if after else None
        if str(old_v or '') != str(new_v or ''):
            log_audit(entity_type, entity_id, f, old_v, new_v, actor_type, actor_name)
            changes.append(f'{f}: {old_v or "\u2014"} \u2192 {new_v or "\u2014"}')
    return changes


def _ist_now():
    """Current time in IST (India Standard Time, UTC+5:30).
    Render servers run in UTC, so we add the offset to get correct local time."""
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)


def utcnow_iso():
    # Despite the name, we return IST so timestamps match the user's local time.
    return _ist_now().isoformat()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # API calls return 401 JSON; page loads redirect to /login
        if not session.get('user_id'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'auth_required'}), 401
            return flask_redirect('/login')
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return jsonify({'error': 'auth_required'}), 401
        if not is_admin():
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated


@app.route('/api/diag')
def diag():
    """Diagnostic: shows whether the persistent disk and data are intact.
    Helps debug 'data disappeared / logged out / asked to create new account'."""
    info = {'data_dir': DATA_DIR, 'db_path': DB_PATH}
    try:
        info['db_exists'] = os.path.exists(DB_PATH)
        info['data_dir_writable'] = os.access(DATA_DIR, os.W_OK)
        info['secret_key_file_exists'] = os.path.exists(os.path.join(DATA_DIR, '.secret_key'))
        info['reset_marker'] = os.path.exists(os.path.join(DATA_DIR, '.last_reset'))
        info['reset_data_env'] = bool(os.environ.get('RESET_DATA'))
        info['secret_key_env'] = bool(os.environ.get('SECRET_KEY'))
        info['data_dir_env_set'] = bool(os.environ.get('DATA_DIR'))

        # Storage persistence (the key signal for the 'data keeps disappearing' bug)
        info['storage_persistent'] = _PERSISTENCE.get('persistent')
        info['restarts_survived'] = _PERSISTENCE.get('boots_seen', 0)

        # Backups present on disk
        try:
            baks = sorted(Path(BAK_DIR).glob('hirelab_*.db'), reverse=True)
            info['backup_count'] = len(baks)
            info['latest_backup'] = baks[0].name if baks else None
            info['latest_backup_users'] = _db_user_count(str(baks[0])) if baks else 0
        except Exception as e:
            info['backup_error'] = str(e)

        conn = get_db(); c = conn.cursor()
        for t in ['users', 'companies', 'mandates', 'candidates']:
            try: info[t + '_count'] = c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
            except Exception as e: info[t + '_count'] = f'err: {e}'
        conn.close()

        # Plain-language diagnosis
        looks_ephemeral = (not info['data_dir_env_set']) and ('HireLab' in DATA_DIR)
        if looks_ephemeral:
            info['diagnosis'] = ('DATA_DIR is not set to the mounted disk — data is on TEMPORARY '
                                 'storage and WILL be lost on restart. On Render: attach a disk at '
                                 '/data and set env var DATA_DIR=/data.')
        elif info.get('storage_persistent') is True:
            info['diagnosis'] = 'OK — storage is persistent and has survived restarts.'
        elif info.get('users_count') == 0 and info.get('latest_backup_users', 0) > 0:
            info['diagnosis'] = 'DB is empty but a backup with users exists — auto-restore should recover on next start.'
        else:
            info['diagnosis'] = ('Persistence not yet confirmed. Restart the service once and re-check; '
                                 'restarts_survived should increase if the disk is persistent.')
    except Exception as e:
        info['error'] = str(e)
    return jsonify(info)



@app.route('/api/auth/status')
def auth_status():
    """Tells the frontend whether to show: first-run admin setup, login, or app."""
    if not any_users_exist():
        return jsonify({'state': 'setup'})
    u = current_user()
    if not u:
        return jsonify({'state': 'login'})
    va = session.get('view_as_company')
    viewing = None
    conn = get_db()
    if va:
        vc = conn.execute('SELECT id,name FROM companies WHERE id=?', (va,)).fetchone()
        viewing = {'id': vc['id'], 'name': vc['name']} if vc else None
    # The user's own company name (for the top bar)
    own_company = None
    if u.get('company_id'):
        oc = conn.execute('SELECT id,name FROM companies WHERE id=?', (u['company_id'],)).fetchone()
        own_company = {'id': oc['id'], 'name': oc['name']} if oc else None
    pending_count = 0
    if u.get('role') == 'admin':
        pending_count = conn.execute("SELECT COUNT(*) n FROM users WHERE status='pending'").fetchone()['n']
    conn.close()
    return jsonify({'state': 'app', 'user': {
        'id': u['id'], 'username': u['username'], 'display_name': u['display_name'],
        'role': u['role'], 'company': own_company,
        'is_company_admin': (u.get('role') == 'admin' or u.get('is_company_admin') == 1),
        'workflow_mode': (get_setting('workflow_mode', 'agency') or 'agency')
    }, 'viewing_as': viewing, 'pending_count': pending_count})


@app.route('/api/auth/setup', methods=['POST'])
def auth_setup():
    """First-run: create the first admin. Only works if no users exist."""
    if any_users_exist():
        return jsonify({'error': 'Setup already complete'}), 400
    d = request.json or {}
    username = (d.get('username') or '').strip()
    password = d.get('password') or ''
    display = (d.get('display_name') or username).strip()
    if not username or len(password) < 4:
        return jsonify({'error': 'Username required and password min 4 chars'}), 400
    conn = get_db()
    display_company = (d.get('company_name') or 'HireLab').strip() or 'HireLab'
    conn.execute("INSERT INTO companies (name,status,plan,billing_status,created_at) VALUES (?,?,?,?,?)",
                 (display_company, 'active', 'owner', 'owner', ts()))
    company_id = conn.execute('SELECT id FROM companies ORDER BY id DESC LIMIT 1').fetchone()['id']
    conn.execute('INSERT INTO users (username,password_hash,display_name,role,created_at,status,company_name,company_id,is_company_admin) VALUES (?,?,?,?,?,?,?,?,1)',
                 (username, hash_password(password), display, 'admin', ts(), 'approved', display_company, company_id))
    conn.commit()
    uid = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()['id']
    # This is the FIRST admin (no users existed before this). Any data already in
    # the DB therefore belongs to a previous, now-deleted user. Claim ALL of it
    # for this admin's COMPANY (tenant), so imported data is visible. Safe because
    # this branch only runs when no users existed.
    conn.execute('UPDATE mandates SET owner_id=?', (company_id,))
    conn.execute('UPDATE candidates SET owner_id=?', (company_id,))
    conn.execute('UPDATE reminders SET owner_id=?', (company_id,))
    conn.commit(); conn.close()
    session['user_id'] = uid
    return jsonify({'ok': True})


@app.route('/api/auth/signup', methods=['POST'])
def auth_signup():
    """Public self-signup. Creates the account as 'pending' — it cannot log
    in until a super-admin approves it. (If no users exist yet at all, this
    is the very first account, so it is created as an approved admin instead
    — see auth_setup for that bootstrap path.)"""
    if not any_users_exist():
        return jsonify({'error': 'No admin account exists yet. Use the initial setup screen instead.'}), 400
    d = request.json or {}
    username = (d.get('username') or '').strip().lower()
    password = d.get('password') or ''
    display = (d.get('display_name') or username).strip()
    company = (d.get('company_name') or '').strip()
    if not username or len(password) < 4:
        return jsonify({'error': 'Username required and password min 4 chars'}), 400
    if not re.match(r'^[a-z0-9._-]{3,40}$', username):
        return jsonify({'error': 'Username can only contain letters, numbers, dots, dashes and underscores'}), 400
    conn = get_db()
    exists = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
    if exists:
        conn.close()
        return jsonify({'error': 'Username already taken'}), 400
    # Create the agency's company (tenant) up front, in 'pending' status. It is
    # activated when the super-admin approves the user. New signups are regular
    # 'user' accounts within their own company — they get full access to their
    # own workspace but NOT the platform-level super-admin panel (which is
    # reserved for the platform owner).
    company_label = company or (display + "'s agency")
    conn.execute("INSERT INTO companies (name,status,plan,created_at) VALUES (?,?,?,?)",
                 (company_label, 'pending', 'standard', ts()))
    new_company_id = conn.execute('SELECT id FROM companies ORDER BY id DESC LIMIT 1').fetchone()['id']
    conn.execute('''INSERT INTO users (username,password_hash,display_name,role,created_at,status,company_name,requested_at,company_id,is_company_admin)
                     VALUES (?,?,?,?,?,?,?,?,?,1)''',
                 (username, hash_password(password), display, 'user', ts(), 'pending', company, ts(), new_company_id))
    conn.commit(); conn.close()
    log_activity('signup_requested', username + (' (' + company + ')' if company else ''))
    return jsonify({'ok': True, 'pending': True})


@app.route('/api/admin/pending-users', methods=['GET'])
@admin_required
def list_pending_users():
    conn = get_db()
    rows = conn.execute('''SELECT id, username, display_name, company_name, requested_at
                            FROM users WHERE status='pending' ORDER BY id''').fetchall()
    conn.close()
    return jsonify({'ok': True, 'pending': [dict(r) for r in rows]})


@app.route('/api/admin/pending-users/<int:uid>/approve', methods=['POST'])
@admin_required
def approve_pending_user(uid):
    conn = get_db()
    u = conn.execute('SELECT username, status, company_id FROM users WHERE id=?', (uid,)).fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'User not found'}), 404
    conn.execute("UPDATE users SET status='approved' WHERE id=?", (uid,))
    if u['company_id']:
        # Activate the company and start its free trial.
        try:
            trial_days = int(get_setting('billing_trial_days', '14') or 14)
        except Exception:
            trial_days = 14
        trial_end = (datetime.datetime.now() + datetime.timedelta(days=trial_days)).isoformat()
        conn.execute("UPDATE companies SET status='active', billing_status='trial', trial_ends_at=? WHERE id=? AND (trial_ends_at IS NULL OR trial_ends_at='')",
                     (trial_end, u['company_id']))
        # If trial was already set (re-approval), just activate.
        conn.execute("UPDATE companies SET status='active' WHERE id=?", (u['company_id'],))
    conn.commit(); conn.close()
    log_activity('approve_user', u['username'])
    return jsonify({'ok': True})


@app.route('/api/admin/pending-users/<int:uid>/reject', methods=['POST'])
@admin_required
def reject_pending_user(uid):
    conn = get_db()
    u = conn.execute('SELECT username, status FROM users WHERE id=?', (uid,)).fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'User not found'}), 404
    conn.execute("UPDATE users SET status='rejected' WHERE id=?", (uid,))
    conn.commit(); conn.close()
    log_activity('reject_user', u['username'])
    return jsonify({'ok': True})


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    d = request.json or {}
    username = (d.get('username') or '').strip()
    password = d.get('password') or ''
    conn = get_db()
    u = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not u or not verify_password(password, u['password_hash']):
        conn.close()
        return jsonify({'error': 'Invalid username or password'}), 401
    if u['status'] == 'pending':
        conn.close()
        return jsonify({'error': 'Your account is awaiting admin approval. You will be able to sign in once approved.'}), 403
    if u['status'] == 'rejected':
        conn.close()
        return jsonify({'error': 'This account request was declined. Contact your admin for access.'}), 403
    # Block login if the tenant company is suspended (super-admin can suspend
    # an agency e.g. for non-payment). The platform owner is never blocked.
    if u['role'] != 'admin' and u['company_id']:
        comp = conn.execute('SELECT status, billing_status, trial_ends_at, plan FROM companies WHERE id=?', (u['company_id'],)).fetchone()
        if comp and comp['status'] == 'suspended':
            conn.close()
            return jsonify({'error': 'Your agency account is currently suspended. Please contact support.'}), 403
        # Trial expiry: if on trial and the trial period has passed without
        # converting to a paid subscription, block until they subscribe.
        if comp and comp['plan'] != 'owner' and comp['billing_status'] == 'trial' and comp['trial_ends_at']:
            try:
                te = datetime.datetime.fromisoformat(comp['trial_ends_at'])
                if datetime.datetime.now() > te:
                    conn.execute("UPDATE companies SET billing_status='past_due' WHERE id=?", (u['company_id'],))
                    conn.commit(); conn.close()
                    return jsonify({'error': 'Your free trial has ended. Please subscribe to continue using HireLab.'}), 402
            except Exception:
                pass
    conn.execute('UPDATE users SET last_login=? WHERE id=?', (ts(), u['id']))
    conn.commit(); conn.close()
    session['user_id'] = u['id']
    session.pop('view_as_company', None)
    log_activity('login', username)
    return jsonify({'ok': True})


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    log_activity('logout')
    session.clear()
    return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════════════════════
#  CLICK-TO-CALL: device registration + push-to-dial
# ═══════════════════════════════════════════════════════════════════════════
@app.route('/api/devices/register', methods=['POST'])
@login_required
def register_device():
    """Android app sends its FCM token after login. We store it so we can push
    call requests to the user's phone later."""
    d = request.json or {}
    fcm_token = (d.get('fcm_token') or '').strip()
    device_name = (d.get('device_name') or 'Unknown device').strip()
    if not fcm_token:
        return jsonify({'error': 'fcm_token required'}), 400
    uid = real_user_id()
    conn = get_db()
    # Upsert: if this token already exists for this user, update; otherwise insert
    existing = conn.execute('SELECT id FROM devices WHERE user_id=? AND fcm_token=?',
                            (uid, fcm_token)).fetchone()
    if existing:
        conn.execute('UPDATE devices SET is_active=1, device_name=?, updated_at=? WHERE id=?',
                     (device_name, ts(), existing['id']))
    else:
        conn.execute('INSERT INTO devices (user_id,fcm_token,device_name,is_active,created_at,updated_at) '
                     'VALUES (?,?,?,1,?,?)', (uid, fcm_token, device_name, ts(), ts()))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/push-call', methods=['POST'])
@login_required
def push_call():
    """Desktop webapp calls this when user clicks "Call via Phone". We send a
    Firebase push to ALL the user's registered devices with the phone number
    and candidate name. The Android app receives it and opens the dialer.

    Uses Firebase Cloud Messaging V1 API (Legacy was shut down June 2024).
    Requires a service account JSON file on the server."""
    d = request.json or {}
    phone = (d.get('phone') or '').strip()
    name = (d.get('name') or 'Candidate').strip()
    if not phone:
        return jsonify({'error': 'Phone number required'}), 400

    uid = real_user_id()
    conn = get_db()
    tokens = conn.execute('SELECT fcm_token FROM devices WHERE user_id=? AND is_active=1',
                          (uid,)).fetchall()
    conn.close()
    if not tokens:
        return jsonify({'error': 'No phone connected. Open the HireLab Dialer app on your phone and login first.'}), 400

    # Get FCM V1 access token using service account
    access_token, project_id, err = _get_fcm_access_token()
    if err:
        return jsonify({'error': err}), 400

    # Send push to all user's devices via V1 API
    sent = 0
    for row in tokens:
        try:
            resp = requests.post(
                f'https://fcm.googleapis.com/v1/projects/{project_id}/messages:send',
                json={
                    'message': {
                        'token': row['fcm_token'],
                        'data': {'action': 'call', 'phone': phone, 'name': name},
                        'android': {'priority': 'high'},
                    }
                },
                headers={
                    'Authorization': f'Bearer {access_token}',
                    'Content-Type': 'application/json',
                },
                timeout=10)
            if resp.status_code == 200:
                sent += 1
            else:
                print(f'[push-call] FCM V1 error: {resp.status_code} {resp.text[:200]}')
        except Exception as e:
            print(f'[push-call] FCM send failed: {e}')

    if sent > 0:
        return jsonify({'ok': True, 'sent': sent, 'message': f'Call push sent! Check your phone.'})
    else:
        return jsonify({'error': 'Push failed — try re-opening the Dialer app on your phone.'}), 500


# ── FCM V1 helper: get OAuth2 access token from service account JSON ─────
_fcm_token_cache = {'token': '', 'expires': 0, 'project_id': ''}


def _send_fcm_to_user(uid, data_payload):
    """Send a data-only FCM push to all of a user's active devices.
    data_payload: dict of string->string. Returns number of devices reached."""
    conn = get_db()
    tokens = conn.execute('SELECT fcm_token FROM devices WHERE user_id=? AND is_active=1',
                          (uid,)).fetchall()
    conn.close()
    if not tokens:
        return 0
    access_token, project_id, err = _get_fcm_access_token()
    if err:
        print(f'[fcm] token error: {err}')
        return 0
    # FCM data values must all be strings
    data = {k: (str(v) if v is not None else '') for k, v in data_payload.items()}
    sent = 0
    for row in tokens:
        try:
            resp = requests.post(
                f'https://fcm.googleapis.com/v1/projects/{project_id}/messages:send',
                json={'message': {
                    'token': row['fcm_token'],
                    'data': data,
                    'android': {'priority': 'high'},
                }},
                headers={'Authorization': f'Bearer {access_token}',
                         'Content-Type': 'application/json'},
                timeout=10)
            if resp.status_code == 200:
                sent += 1
            else:
                print(f'[fcm] error {resp.status_code}: {resp.text[:150]}')
        except Exception as e:
            print(f'[fcm] send failed: {e}')
    return sent


def _reminder_candidate_details(conn, r):
    """Build a rich detail payload for a reminder's candidate (for the push)."""
    cid = r['candidate_id']
    cand = conn.execute(
        'SELECT name, phone, company, designation, stage FROM candidates WHERE id=?',
        (cid,)).fetchone()
    # last note = most recent candidate event of type 'note' or the reminder note
    last_note = r['note'] or ''
    try:
        ev = conn.execute(
            "SELECT detail FROM candidate_events WHERE candidate_id=? AND event_type='note' "
            "ORDER BY created_at DESC LIMIT 1", (cid,)).fetchone()
        if ev and ev['detail']:
            last_note = ev['detail']
    except Exception:
        pass
    name = (cand['name'] if cand else '') or r['candidate_name'] or 'Candidate'
    return {
        'action': 'reminder',
        'reminder_id': r['id'],
        'candidate_id': cid,
        'name': name,
        'phone': (cand['phone'] if cand else '') or '',
        'company': (cand['company'] if cand else '') or '',
        'designation': (cand['designation'] if cand else '') or '',
        'stage': (cand['stage'] if cand else '') or '',
        'mandate_label': r['mandate_label'] or '',
        'note': (last_note or '')[:400],
        'due_at': r['due_at'] or '',
    }


def _reminder_scheduler_loop():
    """Background loop: every 60s, check for due reminders and send push
    notifications. Repeats every 5 min until the reminder is done or snoozed.
    Also sends an early warning 5-10 min before the due time."""
    import time as _time
    # Small delay so the app is fully up before first check
    _time.sleep(15)
    while True:
        try:
            now = _ist_now()
            now_iso = now.isoformat(timespec='seconds')
            conn = get_db()
            # All active reminders not yet done
            rows = conn.execute(
                'SELECT * FROM reminders WHERE done=0 AND due_at IS NOT NULL AND due_at!=""'
            ).fetchall()
            for r in rows:
                try:
                    due_str = r['due_at']
                    # normalise: reminders may be 'YYYY-MM-DDTHH:MM' or with seconds
                    due = None
                    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M'):
                        try:
                            due = datetime.datetime.strptime(due_str[:19], fmt)
                            break
                        except Exception:
                            continue
                    if not due:
                        continue

                    owner = r['owner_id'] if ('owner_id' in r.keys()) else 0
                    if not owner:
                        continue

                    snoozed_until = r['snoozed_until'] if ('snoozed_until' in r.keys()) else ''
                    if snoozed_until:
                        try:
                            su = datetime.datetime.strptime(snoozed_until[:19], '%Y-%m-%dT%H:%M:%S')
                            if now < su:
                                continue  # still snoozed
                        except Exception:
                            pass

                    early_warned = r['early_warned'] if ('early_warned' in r.keys()) else 0
                    notified_at = r['notified_at'] if ('notified_at' in r.keys()) else ''

                    mins_to_due = (due - now).total_seconds() / 60.0

                    # EARLY WARNING: 5-10 min before due
                    if not early_warned and 0 < mins_to_due <= 10:
                        payload = _reminder_candidate_details(conn, r)
                        payload['kind'] = 'early'
                        payload['title'] = 'Upcoming: ' + payload['name']
                        _send_fcm_to_user(owner, payload)
                        conn.execute('UPDATE reminders SET early_warned=1 WHERE id=?', (r['id'],))
                        conn.commit()
                        continue

                    # DUE NOW (or overdue): notify, then repeat every 5 min
                    if mins_to_due <= 0:
                        send = False
                        if not notified_at:
                            send = True
                        else:
                            try:
                                last = datetime.datetime.strptime(notified_at[:19], '%Y-%m-%dT%H:%M:%S')
                                if (now - last).total_seconds() >= 300:  # 5 min
                                    send = True
                            except Exception:
                                send = True
                        if send:
                            payload = _reminder_candidate_details(conn, r)
                            payload['kind'] = 'due'
                            payload['title'] = 'Reminder: ' + payload['name']
                            _send_fcm_to_user(owner, payload)
                            conn.execute('UPDATE reminders SET notified_at=? WHERE id=?',
                                         (now_iso, r['id']))
                            conn.commit()
                except Exception as _re:
                    print(f'[reminder-scheduler] row error: {_re}')
            conn.close()
        except Exception as e:
            print(f'[reminder-scheduler] loop error: {e}')
        _time.sleep(60)


_reminder_thread_started = False
def _start_reminder_scheduler():
    global _reminder_thread_started
    if _reminder_thread_started:
        return
    _reminder_thread_started = True
    import threading
    t = threading.Thread(target=_reminder_scheduler_loop, daemon=True)
    t.start()
    print('[reminder-scheduler] background thread started')


def _get_fcm_access_token():
    """Get a short-lived OAuth2 access token for FCM V1 API.
    Reads the service account JSON from either:
      1. FCM_SERVICE_ACCOUNT_JSON env var (the entire JSON string), or
      2. A file at DATA_DIR/firebase-service-account.json
    Caches the token until it expires."""
    import time
    now = time.time()
    if _fcm_token_cache['token'] and _fcm_token_cache['expires'] > now + 60:
        return _fcm_token_cache['token'], _fcm_token_cache['project_id'], None

    # Load service account credentials
    sa_json = os.environ.get('FCM_SERVICE_ACCOUNT_JSON', '').strip()
    sa_path = os.path.join(DATA_DIR, 'firebase-service-account.json')

    try:
        if sa_json:
            import io
            sa_info = json.loads(sa_json)
        elif os.path.exists(sa_path):
            with open(sa_path) as f:
                sa_info = json.load(f)
        else:
            return None, None, ('FCM not configured. Either:\n'
                                '1. Upload firebase-service-account.json to your data folder, or\n'
                                '2. Set FCM_SERVICE_ACCOUNT_JSON env var on Render with the full JSON content.')
    except Exception as e:
        return None, None, f'Failed to read service account: {e}'

    project_id = sa_info.get('project_id', '')
    if not project_id:
        return None, None, 'Service account JSON missing project_id.'

    # Build a JWT and exchange for an access token (no external library needed)
    try:
        import jwt as _jwt_lib
        _has_pyjwt = True
    except ImportError:
        _has_pyjwt = False

    if _has_pyjwt:
        token, exp = _fcm_token_via_pyjwt(sa_info, now)
    else:
        token, exp = _fcm_token_via_manual_jwt(sa_info, now)

    if token:
        _fcm_token_cache['token'] = token
        _fcm_token_cache['expires'] = exp
        _fcm_token_cache['project_id'] = project_id
        return token, project_id, None
    return None, None, 'Failed to generate FCM access token. Check service account JSON.'


def _fcm_token_via_pyjwt(sa_info, now):
    """Use PyJWT library if available."""
    import jwt, time
    payload = {
        'iss': sa_info['client_email'],
        'scope': 'https://www.googleapis.com/auth/firebase.messaging',
        'aud': 'https://oauth2.googleapis.com/token',
        'iat': int(now),
        'exp': int(now) + 3600,
    }
    signed = jwt.encode(payload, sa_info['private_key'], algorithm='RS256')
    resp = requests.post('https://oauth2.googleapis.com/token', data={
        'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        'assertion': signed,
    }, timeout=15)
    data = resp.json()
    return data.get('access_token'), int(now) + data.get('expires_in', 3500)


def _fcm_token_via_manual_jwt(sa_info, now):
    """Build JWT manually without any external library (pure Python + stdlib)."""
    import base64, hashlib, hmac, struct, time
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    header = base64.urlsafe_b64encode(json.dumps(
        {'alg': 'RS256', 'typ': 'JWT'}).encode()).rstrip(b'=')
    claims = base64.urlsafe_b64encode(json.dumps({
        'iss': sa_info['client_email'],
        'scope': 'https://www.googleapis.com/auth/firebase.messaging',
        'aud': 'https://oauth2.googleapis.com/token',
        'iat': int(now), 'exp': int(now) + 3600,
    }).encode()).rstrip(b'=')
    signing_input = header + b'.' + claims

    private_key = serialization.load_pem_private_key(
        sa_info['private_key'].encode(), password=None)
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b'=')

    jwt_token = (signing_input + b'.' + sig_b64).decode()
    resp = requests.post('https://oauth2.googleapis.com/token', data={
        'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        'assertion': jwt_token,
    }, timeout=15)
    data = resp.json()
    return data.get('access_token'), int(now) + data.get('expires_in', 3500)


@app.route('/api/users', methods=['GET'])
@admin_required
def list_users():
    conn = get_db()
    rows = conn.execute('''SELECT u.id, u.username, u.display_name, u.role, u.created_at,
                                  u.last_login, u.status, u.company_name, u.company_id,
                                  co.name AS company_label, co.status AS company_status
                           FROM users u LEFT JOIN companies co ON co.id = u.company_id
                           ORDER BY u.id''').fetchall()
    out = []
    for u in rows:
        d = dict(u)
        # Counts are per-tenant (company), since owner_id stores the company id.
        cid = u['company_id']
        if cid:
            d['mandate_count'] = conn.execute('SELECT COUNT(*) n FROM mandates WHERE owner_id=?', (cid,)).fetchone()['n']
            d['candidate_count'] = conn.execute('SELECT COUNT(*) n FROM candidates WHERE owner_id=?', (cid,)).fetchone()['n']
        else:
            d['mandate_count'] = 0
            d['candidate_count'] = 0
        out.append(d)
    conn.close()
    return jsonify({'ok': True, 'users': out})

@app.route('/api/users', methods=['POST'])
@admin_required
def create_user():
    """Super-admin creates a user. By default the new user joins the
    super-admin's OWN company; pass company_id to place them in another tenant."""
    d = request.json or {}
    username = (d.get('username') or '').strip()
    password = d.get('password') or ''
    display = (d.get('display_name') or username).strip()
    role = d.get('role') if d.get('role') in ('admin', 'user') else 'user'
    company_id = d.get('company_id') or current_company_id()
    if not username or len(password) < 4:
        return jsonify({'error': 'Username required and password min 4 chars'}), 400
    conn = get_db()
    exists = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
    if exists:
        conn.close()
        return jsonify({'error': 'Username already taken'}), 400
    conn.execute('INSERT INTO users (username,password_hash,display_name,role,created_at,status,company_id) VALUES (?,?,?,?,?,?,?)',
                 (username, hash_password(password), display, role, ts(), 'approved', company_id))
    conn.commit(); conn.close()
    log_activity('create_user', username + ' (' + role + ')')
    return jsonify({'ok': True})

@app.route('/api/users/<int:uid>/password', methods=['POST'])
@admin_required
def reset_user_password(uid):
    d = request.json or {}
    password = d.get('password') or ''
    if len(password) < 4:
        return jsonify({'error': 'Password min 4 chars'}), 400
    conn = get_db()
    u = conn.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'User not found'}), 404
    conn.execute('UPDATE users SET password_hash=? WHERE id=?', (hash_password(password), uid))
    conn.commit(); conn.close()
    log_activity('reset_password', u['username'])
    return jsonify({'ok': True})

@app.route('/api/users/<int:uid>', methods=['DELETE'])
@admin_required
def delete_user(uid):
    me = current_user()
    if me and me['id'] == uid:
        return jsonify({'error': "You can't delete your own account"}), 400
    conn = get_db()
    u = conn.execute('SELECT username, role FROM users WHERE id=?', (uid,)).fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'User not found'}), 404
    # Safety: don't allow deleting the last admin
    if u['role'] == 'admin':
        admins = conn.execute("SELECT COUNT(*) n FROM users WHERE role='admin'").fetchone()['n']
        if admins <= 1:
            conn.close()
            return jsonify({'error': 'Cannot delete the only admin'}), 400
    # Data belongs to the user's COMPANY (tenant), not to the individual user,
    # so deleting a user does NOT touch any mandates/candidates — the company
    # keeps all its data for its remaining (or future) members.
    conn.execute('DELETE FROM users WHERE id=?', (uid,))
    conn.commit(); conn.close()
    log_activity('delete_user', u['username'])
    return jsonify({'ok': True})


@app.route('/api/admin/claim-orphans', methods=['POST'])
@admin_required
def claim_orphans():
    """Assign any data with owner_id=0/NULL to the admin's COMPANY (tenant) so
    it shows up in their workspace. owner_id stores the tenant/company id."""
    tenant = current_company_id()
    conn = get_db(); c = conn.cursor()
    n_m = c.execute('UPDATE mandates SET owner_id=? WHERE owner_id IS NULL OR owner_id=0', (tenant,)).rowcount
    n_c = c.execute('UPDATE candidates SET owner_id=? WHERE owner_id IS NULL OR owner_id=0', (tenant,)).rowcount
    n_r = c.execute('UPDATE reminders SET owner_id=? WHERE owner_id IS NULL OR owner_id=0', (tenant,)).rowcount
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'mandates': n_m, 'candidates': n_c, 'reminders': n_r})



# ── Super-Admin: view-as (impersonate a tenant's workspace) ───────────────
@app.route('/api/admin/view-as', methods=['POST'])
@admin_required
def admin_view_as():
    """Super-admin impersonates a COMPANY (tenant). Accepts a company_id (or a
    legacy user_id, resolved to that user's company)."""
    d = request.json or {}
    company_id = d.get('company_id')
    # Backward-compat: if a user_id is sent, resolve it to that user's company.
    if not company_id and d.get('user_id'):
        conn = get_db()
        ur = conn.execute('SELECT company_id FROM users WHERE id=?', (d.get('user_id'),)).fetchone()
        conn.close()
        company_id = ur['company_id'] if ur else None
    conn = get_db()
    if company_id:
        comp = conn.execute('SELECT name FROM companies WHERE id=?', (company_id,)).fetchone()
        conn.close()
        if not comp:
            return jsonify({'error': 'Company not found'}), 404
        session['view_as_company'] = company_id
        log_activity('view_as', comp['name'])
    else:
        conn.close()
        session.pop('view_as_company', None)
        log_activity('view_as', 'exited')
    return jsonify({'ok': True})


@app.route('/api/admin/api-usage', methods=['GET'])
@admin_required
def admin_api_usage():
    """Per-agency API usage & estimated cost. Optional ?days=N (default 30)."""
    try:
        days = int(request.args.get('days', 30))
    except Exception:
        days = 30
    since = (_ist_now() - datetime.timedelta(days=days)).isoformat()
    conn = get_db()
    companies = conn.execute('SELECT id, name FROM companies ORDER BY id').fetchall()
    name_map = {c['id']: c['name'] for c in companies}

    rows = conn.execute('''
        SELECT company_id, provider,
               SUM(input_tokens) in_tok, SUM(output_tokens) out_tok,
               SUM(audio_seconds) audio_sec, SUM(cost_usd) cost, COUNT(*) calls
        FROM api_usage WHERE created_at >= ?
        GROUP BY company_id, provider''', (since,)).fetchall()
    conn.close()

    agg = {}
    grand_total = 0.0
    for r in rows:
        cid = r['company_id']
        if cid not in agg:
            agg[cid] = {'company_id': cid,
                        'company': name_map.get(cid, '(unknown / deleted)'),
                        'total_cost': 0.0, 'total_calls': 0, 'providers': {}}
        agg[cid]['providers'][r['provider']] = {
            'calls': r['calls'], 'input_tokens': r['in_tok'] or 0,
            'output_tokens': r['out_tok'] or 0,
            'audio_minutes': round((r['audio_sec'] or 0) / 60.0, 1),
            'cost_usd': round(r['cost'] or 0, 4)}
        agg[cid]['total_cost'] += (r['cost'] or 0)
        agg[cid]['total_calls'] += r['calls']
        grand_total += (r['cost'] or 0)

    out = sorted(agg.values(), key=lambda x: x['total_cost'], reverse=True)
    for a in out:
        a['total_cost'] = round(a['total_cost'], 4)
    return jsonify({'ok': True, 'days': days, 'grand_total_usd': round(grand_total, 4),
                    'usage': out})


def compute_company_bill(company_id, days=30):
    """Compute one company's bill: (recruiters x price) + token charges (API
    cost passed through, USD→INR x markup), then GST. The platform owner's own
    company is never billed."""
    conn = get_db()
    comp = conn.execute('SELECT * FROM companies WHERE id=?', (company_id,)).fetchone()
    if not comp:
        conn.close(); return None
    recruiters = conn.execute("SELECT COUNT(*) n FROM users WHERE company_id=? AND status='approved'",
                              (company_id,)).fetchone()['n']
    since = (_ist_now() - datetime.timedelta(days=days)).isoformat()
    token_usd = conn.execute('SELECT COALESCE(SUM(cost_usd),0) c FROM api_usage WHERE company_id=? AND created_at>=?',
                             (company_id, since)).fetchone()['c'] or 0
    conn.close()

    price = int(get_setting('billing_price_per_recruiter', '700') or 700)
    usd_inr = float(get_setting('billing_usd_inr', '88') or 88)
    markup = float(get_setting('billing_token_markup', '1.0') or 1.0)
    gst_rate = float(get_setting('billing_gst_rate', '18') or 18)

    base = recruiters * price
    token_inr = round(token_usd * usd_inr * markup, 2)
    subtotal = round(base + token_inr, 2)
    gst = round(subtotal * gst_rate / 100.0, 2)
    total = round(subtotal + gst, 2)

    trial_left = None
    if comp['trial_ends_at']:
        try:
            te = datetime.datetime.fromisoformat(comp['trial_ends_at'])
            trial_left = max(0, (te - datetime.datetime.now()).days)
        except Exception:
            trial_left = None

    return {
        'company_id': company_id, 'company': comp['name'],
        'billing_status': comp['billing_status'], 'status': comp['status'],
        'recruiters': recruiters, 'price_per_recruiter': price,
        'base_inr': base, 'token_usd': round(token_usd, 4), 'token_inr': token_inr,
        'subtotal_inr': subtotal, 'gst_rate': gst_rate, 'gst_inr': gst, 'total_inr': total,
        'trial_ends_at': comp['trial_ends_at'], 'trial_days_left': trial_left,
        'is_owner': (comp['plan'] == 'owner' or comp['billing_status'] == 'owner'),
    }


@app.route('/api/admin/billing', methods=['GET'])
@admin_required
def admin_billing():
    """Super-admin billing dashboard: every agency's monthly bill + status."""
    try:
        days = int(request.args.get('days', 30))
    except Exception:
        days = 30
    conn = get_db()
    ids = [r['id'] for r in conn.execute('SELECT id FROM companies ORDER BY id').fetchall()]
    conn.close()
    bills = []
    revenue = 0.0
    for cid in ids:
        b = compute_company_bill(cid, days)
        if not b:
            continue
        bills.append(b)
        if not b['is_owner'] and b['billing_status'] in ('active', 'past_due'):
            revenue += b['total_inr']
    return jsonify({'ok': True, 'days': days, 'monthly_revenue_inr': round(revenue, 2),
                    'gstin': get_setting('billing_gstin', ''), 'bills': bills})


@app.route('/api/billing/me', methods=['GET'])
@login_required
def my_billing():
    """An agency admin sees their own current bill + trial status."""
    if not is_company_admin():
        return jsonify({'error': 'Not allowed'}), 403
    b = compute_company_bill(effective_company_id(), 30)
    return jsonify({'ok': True, 'bill': b}) if b else (jsonify({'error': 'No company'}), 404)


@app.route('/api/admin/billing/<int:cid>/status', methods=['POST'])
@admin_required
def set_billing_status(cid):
    """Super-admin sets an agency's billing status (active/suspended/trial/past_due)."""
    d = request.json or {}
    status = d.get('status', '')
    if status not in ('active', 'suspended', 'trial', 'past_due'):
        return jsonify({'error': 'Invalid status'}), 400
    conn = get_db()
    comp = conn.execute('SELECT name FROM companies WHERE id=?', (cid,)).fetchone()
    if not comp:
        conn.close(); return jsonify({'error': 'Company not found'}), 404
    # billing_status drives the badge; company.status controls actual login block.
    company_status = 'suspended' if status == 'suspended' else 'active'
    conn.execute('UPDATE companies SET billing_status=?, status=? WHERE id=?',
                 (status, company_status, cid))
    conn.commit(); conn.close()
    log_activity('billing_status', f"{comp['name']} → {status}")
    return jsonify({'ok': True})


@app.route('/api/admin/billing/<int:cid>/pay', methods=['POST'])
@admin_required
def record_payment(cid):
    """Record a manual payment (UPI/bank transfer) and activate the agency."""
    d = request.json or {}
    conn = get_db()
    comp = conn.execute('SELECT name FROM companies WHERE id=?', (cid,)).fetchone()
    if not comp:
        conn.close(); return jsonify({'error': 'Company not found'}), 404
    bill = compute_company_bill(cid, 30)
    amount = float(d.get('amount') or (bill['total_inr'] if bill else 0))
    note = d.get('note', '')
    invoice_no = _next_invoice_no(conn)
    period = datetime.date.today().strftime('%b %Y')
    conn.execute('''INSERT INTO payments (company_id,invoice_no,amount_inr,period,method,note,created_at)
                    VALUES (?,?,?,?,?,?,?)''',
                 (cid, invoice_no, amount, period, d.get('method', 'manual'), note, ts()))
    # Mark active and clear trial so they're a paying customer now.
    conn.execute("UPDATE companies SET billing_status='active', status='active' WHERE id=?", (cid,))
    conn.commit(); conn.close()
    log_activity('payment', f"{comp['name']} ₹{amount} ({invoice_no})")
    return jsonify({'ok': True, 'invoice_no': invoice_no, 'amount': amount})


def _next_invoice_no(conn):
    """Sequential invoice number like HL-2026-0001."""
    yr = datetime.date.today().year
    n = conn.execute("SELECT COUNT(*) c FROM payments").fetchone()['c'] + 1
    return f"HL-{yr}-{n:04d}"


@app.route('/api/admin/billing/<int:cid>/payments', methods=['GET'])
@admin_required
def list_payments(cid):
    conn = get_db()
    rows = conn.execute('SELECT * FROM payments WHERE company_id=? ORDER BY created_at DESC', (cid,)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'payments': [dict(r) for r in rows]})


@app.route('/api/billing/invoice/<int:cid>', methods=['GET'])
@login_required
def billing_invoice(cid):
    """Invoice data for a company's current bill. Super-admin for any company;
    a company admin only for their own."""
    if not is_admin() and not (is_company_admin() and effective_company_id() == cid):
        return jsonify({'error': 'Not allowed'}), 403
    bill = compute_company_bill(cid, 30)
    if not bill:
        return jsonify({'error': 'No company'}), 404
    conn = get_db()
    last = conn.execute('SELECT invoice_no FROM payments WHERE company_id=? ORDER BY id DESC LIMIT 1', (cid,)).fetchone()
    conn.close()
    line_items = [{'desc': f"Subscription — {bill['recruiters']} recruiter(s) × ₹{bill['price_per_recruiter']}",
                   'amount': bill['base_inr']}]
    if bill['token_inr'] > 0:
        line_items.append({'desc': 'AI / API usage charges (this period)', 'amount': bill['token_inr']})
    return jsonify({'ok': True, 'invoice': {
        'invoice_no': (last['invoice_no'] if last else 'DRAFT'),
        'date': datetime.date.today().isoformat(),
        'seller_name': get_setting('billing_legal_name', 'HireLab Talent Resource'),
        'seller_address': get_setting('billing_address', ''),
        'seller_gstin': get_setting('billing_gstin', ''),
        'buyer': bill['company'],
        'line_items': line_items,
        'subtotal': bill['subtotal_inr'], 'gst_rate': bill['gst_rate'],
        'gst': bill['gst_inr'], 'total': bill['total_inr'],
        'billing_status': bill['billing_status'],
    }})



@app.route('/api/admin/summary', methods=['GET'])
@admin_required
def admin_summary():
    conn = get_db()
    companies = conn.execute("SELECT id, name, status, plan, created_at, expires_at FROM companies ORDER BY id").fetchall()
    summary = []
    for comp in companies:
        cid = comp['id']
        mand = conn.execute('SELECT COUNT(*) n FROM mandates WHERE owner_id=?', (cid,)).fetchone()['n']
        active_mand = conn.execute("SELECT COUNT(*) n FROM mandates WHERE owner_id=? AND status='active'", (cid,)).fetchone()['n']
        cands = conn.execute('SELECT COUNT(*) n FROM candidates WHERE owner_id=?', (cid,)).fetchone()['n']
        placed = conn.execute("SELECT COUNT(*) n FROM candidates WHERE owner_id=? AND stage='Placed'", (cid,)).fetchone()['n']
        members = conn.execute("SELECT COUNT(*) n FROM users WHERE company_id=? AND status='approved'", (cid,)).fetchone()['n']
        last_login = conn.execute("SELECT MAX(last_login) m FROM users WHERE company_id=?", (cid,)).fetchone()['m']
        summary.append({
            'id': cid, 'name': comp['name'], 'status': comp['status'], 'plan': comp['plan'],
            'created_at': comp['created_at'], 'expires_at': comp['expires_at'],
            'members': members, 'last_login': last_login,
            'mandates': mand, 'active_mandates': active_mand,
            'candidates': cands, 'placed': placed,
        })
    recent = conn.execute('SELECT username, action, detail, created_at FROM activity_log ORDER BY id DESC LIMIT 50').fetchall()
    conn.close()
    return jsonify({'ok': True, 'summary': summary, 'recent_activity': [dict(r) for r in recent]})


@app.route('/api/admin/companies/<int:cid>/suspend', methods=['POST'])
@admin_required
def suspend_company(cid):
    """Suspend an agency (e.g. non-payment). Its users can't log in until
    reactivated. The platform owner's own company can't be suspended."""
    me = current_user()
    if me and me.get('company_id') == cid:
        return jsonify({'error': "You can't suspend your own company"}), 400
    conn = get_db()
    comp = conn.execute('SELECT name FROM companies WHERE id=?', (cid,)).fetchone()
    if not comp:
        conn.close()
        return jsonify({'error': 'Company not found'}), 404
    conn.execute("UPDATE companies SET status='suspended' WHERE id=?", (cid,))
    conn.commit(); conn.close()
    log_activity('suspend_company', comp['name'])
    return jsonify({'ok': True})


@app.route('/api/admin/companies/<int:cid>/activate', methods=['POST'])
@admin_required
def activate_company(cid):
    conn = get_db()
    comp = conn.execute('SELECT name FROM companies WHERE id=?', (cid,)).fetchone()
    if not comp:
        conn.close()
        return jsonify({'error': 'Company not found'}), 404
    conn.execute("UPDATE companies SET status='active' WHERE id=?", (cid,))
    conn.commit(); conn.close()
    log_activity('activate_company', comp['name'])
    return jsonify({'ok': True})


@app.route('/login')
def login_page():
    # Single-page app handles login UI; just serve the app which checks auth_status
    return flask_redirect('/')


# Data lives in user home — survives app updates
# Railway: set DATA_DIR=/data in env vars (persistent volume)
# Local: defaults to ~/HireLab
DATA_DIR = os.environ.get('DATA_DIR',
    '/data' if os.environ.get('RAILWAY_ENVIRONMENT') else
    os.path.join(os.path.expanduser('~'), 'HireLab'))
DB_PATH  = os.path.join(DATA_DIR, 'hirelab.db')
CV_DIR   = os.path.join(DATA_DIR, 'cvs')
BAK_DIR  = os.path.join(DATA_DIR, 'backups')
CRM_FILES_DIR = os.path.join(DATA_DIR, 'crm_files')

CLAUDE_URL   = 'https://api.anthropic.com/v1/messages'
CLAUDE_MODEL = 'claude-sonnet-4-20250514'

for d in [DATA_DIR, CV_DIR, BAK_DIR, CRM_FILES_DIR]:
    os.makedirs(d, exist_ok=True)

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # IMPORTANT: DELETE journal mode + FULL sync (not WAL+NORMAL).
    # WAL keeps recent writes in a separate -wal file that can fail to flush
    # into the main DB file on a cloud-host restart/redeploy — this was the
    # root cause of a critical bug where the users table (and other recent
    # writes) appeared empty after a restart, forcing a fresh "Create Admin"
    # setup and orphaning the previous data. DELETE+FULL writes every change
    # straight into the main database file, so there is no separate WAL file
    # that can be lost.
    conn.execute('PRAGMA journal_mode=DELETE')
    conn.execute('PRAGMA busy_timeout=60000')
    conn.execute('PRAGMA synchronous=FULL')
    return conn

def esc_html(s):
    """Escape a string for safe inclusion in HTML email bodies."""
    return (str(s or '').replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))

def ts():
    return _ist_now().isoformat(timespec='seconds')

def html_to_text(html):
    """Convert JD rich-text HTML to clean plain text for AI prompts / exports."""
    if not html:
        return ''
    import re as _re
    txt = html
    # Lists: prefix items with bullet/number markers before stripping tags
    txt = _re.sub(r'<li[^>]*>', '\n- ', txt, flags=_re.I)
    # Block-level tags -> newlines
    txt = _re.sub(r'</(p|div|h[1-6]|li|ul|ol)>', '\n', txt, flags=_re.I)
    txt = _re.sub(r'<br\s*/?>', '\n', txt, flags=_re.I)
    # Strip remaining tags
    txt = _re.sub(r'<[^>]+>', '', txt)
    # Decode common HTML entities
    txt = (txt.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
              .replace('&quot;', '"').replace('&#039;', "'").replace('&nbsp;', ' '))
    # Collapse excess blank lines/spaces
    txt = _re.sub(r'\n[ \t]+', '\n', txt)
    txt = _re.sub(r'\n{3,}', '\n\n', txt)
    return txt.strip()

def init_db():
    conn = get_db(); c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT DEFAULT '',
            role TEXT DEFAULT 'user',
            created_at TEXT,
            last_login TEXT,
            status TEXT DEFAULT 'approved',
            company_name TEXT DEFAULT '',
            requested_at TEXT,
            company_id INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            status TEXT DEFAULT 'active',
            plan TEXT DEFAULT 'standard',
            created_at TEXT,
            expires_at TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            billing_status TEXT DEFAULT 'trial',
            trial_ends_at TEXT DEFAULT '',
            price_per_recruiter INTEGER DEFAULT 700,
            cf_subscription_id TEXT DEFAULT '',
            gstin TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT DEFAULT '',
            action TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS mandates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            location TEXT DEFAULT '',
            division TEXT DEFAULT '',
            ctc_min REAL DEFAULT 0,
            ctc_max REAL DEFAULT 0,
            jd TEXT DEFAULT '',
            sop_text TEXT DEFAULT '',
            sop_version INTEGER DEFAULT 1,
            sop_changelog TEXT DEFAULT '[]',
            status TEXT DEFAULT 'active',
            email_templates TEXT DEFAULT '[]',
            created_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mandate_id INTEGER NOT NULL,
            name TEXT DEFAULT '',
            company TEXT DEFAULT '',
            designation TEXT DEFAULT '',
            experience REAL DEFAULT 0,
            ctc_current REAL DEFAULT 0,
            ctc_expected REAL DEFAULT 0,
            notice_period INTEGER DEFAULT 0,
            location TEXT DEFAULT '',
            preferred_location TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            email TEXT DEFAULT '',
            qualification TEXT DEFAULT '',
            key_skills TEXT DEFAULT '[]',
            secondary_skills TEXT DEFAULT '[]',
            career_summary TEXT DEFAULT '',
            industry_background TEXT DEFAULT '',
            is_mnc INTEGER DEFAULT 0,
            screening_decision TEXT DEFAULT '',
            ai_score REAL DEFAULT 0,
            ai_reasoning TEXT DEFAULT '',
            stage TEXT DEFAULT 'Screening',
            recruiter_feedback TEXT DEFAULT '',
            client_feedback TEXT DEFAULT '',
            general_comments TEXT DEFAULT '',
            cv_path TEXT DEFAULT '',
            cv_original_name TEXT DEFAULT '',
            msg1_sent_at TEXT DEFAULT '',
            fu1_sent_at TEXT DEFAULT '',
            fu2_sent_at TEXT DEFAULT '',
            wa_response TEXT DEFAULT '',
            wa_response_note TEXT DEFAULT '',
            wa_response_at TEXT DEFAULT '',
            key_skill_tags TEXT DEFAULT '[]',
            domain_tags TEXT DEFAULT '[]',
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT '',
            FOREIGN KEY (mandate_id) REFERENCES mandates(id)
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            mandate_id INTEGER,
            candidate_name TEXT DEFAULT '',
            mandate_label TEXT DEFAULT '',
            note TEXT DEFAULT '',
            due_at TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS work_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            company TEXT DEFAULT '',
            designation TEXT DEFAULT '',
            start_date TEXT DEFAULT '',
            end_date TEXT DEFAULT '',
            is_current INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            email TEXT DEFAULT '',
            company TEXT DEFAULT '',
            designation TEXT DEFAULT '',
            experience REAL DEFAULT 0,
            ctc_current REAL DEFAULT 0,
            ctc_expected REAL DEFAULT 0,
            notice_period INTEGER DEFAULT 0,
            location TEXT DEFAULT '',
            key_skills TEXT DEFAULT '[]',
            domain_tags TEXT DEFAULT '[]',
            custom_fields TEXT DEFAULT '{}',
            cv_path TEXT DEFAULT '',
            cv_original_name TEXT DEFAULT '',
            resume_parsed INTEGER DEFAULT 0,
            status TEXT DEFAULT 'new',
            notes TEXT DEFAULT '',
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS stage_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            from_stage TEXT DEFAULT '',
            to_stage TEXT DEFAULT '',
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            FOREIGN KEY (candidate_id) REFERENCES candidates(id)
        );
        CREATE TABLE IF NOT EXISTS candidate_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            event_type TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS interviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            mandate_id INTEGER,
            owner_id INTEGER,
            round_name TEXT DEFAULT '',
            mode TEXT DEFAULT '',
            location TEXT DEFAULT '',
            interviewer TEXT DEFAULT '',
            scheduled_at TEXT DEFAULT '',
            status TEXT DEFAULT 'scheduled',
            result TEXT DEFAULT '',
            task_snoozed_until TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            fcm_token TEXT NOT NULL,
            device_name TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS tenant_settings (
            company_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT DEFAULT '',
            PRIMARY KEY (company_id, key)
        );
        CREATE TABLE IF NOT EXISTS api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER DEFAULT 0,
            provider TEXT DEFAULT '',
            model TEXT DEFAULT '',
            endpoint TEXT DEFAULT '',
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            audio_seconds REAL DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            invoice_no TEXT DEFAULT '',
            amount_inr REAL DEFAULT 0,
            period TEXT DEFAULT '',
            method TEXT DEFAULT 'manual',
            note TEXT DEFAULT '',
            created_at TEXT
        );
    """)
    defaults = [
        ('recruiter_name', 'Nitin Kumar'),
        ('company_name', 'HireLab'),
        ('claude_api_key', os.environ.get('CLAUDE_API_KEY', '')),
        ('deepseek_api_key', os.environ.get('DEEPSEEK_API_KEY', '')),
        ('groq_api_key', os.environ.get('GROQ_API_KEY', '')),
        ('fu1_hours', '8'),
        ('fu2_hours', '24'),
        ('stale_days', '7'),
        ('promise_hours', '24'),
        ('analytics_stale_days', '7'),
        ('bd_stale_days', '21'),
        ('interview_template',
         'Dear {name},\n\n'
         'We are pleased to inform you that your interview for the position of {role} '
         'has been scheduled.\n\n'
         'Round: {round}\n'
         'Date & Time: {datetime}\n'
         'Mode: {mode}\n'
         '{location_line}\n\n'
         'Please be available on time. Kindly confirm your availability.\n\n'
         'Best regards,\n{recruiter}'),
        ('template_msg1', 'Hi {Name}, this is {RecruiterName} from HireLab. I wanted to speak about a {Position} opportunity at {Location}.\n\nIf you are interested, please suggest the best time to connect.'),
        ('template_fu1', 'Hi {Name}, I had messaged you earlier about a {Position} role at {Location}.\n\nJust following up — would love to connect for a quick 10-minute call.\n\nLooking forward to hearing from you!'),
        ('template_fu2', 'Hi {Name}, this is my last follow up regarding the {Position} opportunity at {Location}.\n\nIf the timing is not right, no worries. But do let me know if you would like to explore this.\n\nHave a great day!'),
        # ── Billing config (super-admin editable) ──
        ('billing_price_per_recruiter', '700'),   # INR per recruiter / month
        ('billing_trial_days', '14'),
        ('billing_usd_inr', '88'),                # rate to convert API cost USD→INR
        ('billing_token_markup', '1.0'),          # multiplier on pass-through token cost
        ('billing_gst_rate', '18'),               # GST % on the invoice
        ('billing_gstin', ''),                    # your GST number (for invoices)
        ('billing_legal_name', 'HireLab Talent Resource'),
        ('billing_address', 'Ghaziabad / NCR, India'),
    ]
    for k, v in defaults:
        c.execute('INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)', (k, v))
    # Migrate: add owner_id to mandates, candidates, reminders (multi-user)
    for tbl in ['mandates', 'candidates', 'reminders']:
        try:
            c.execute(f'ALTER TABLE {tbl} ADD COLUMN owner_id INTEGER DEFAULT 0')
        except sqlite3.OperationalError:
            pass
    # Add created_by to candidates/mandates for tracking
    for tbl in ['mandates', 'candidates']:
        try:
            c.execute(f'ALTER TABLE {tbl} ADD COLUMN created_by TEXT DEFAULT ""')
        except sqlite3.OperationalError:
            pass
    # Per-recruiter mandate assignment (within a company) + company-admin flag
    try:
        c.execute('ALTER TABLE mandates ADD COLUMN assigned_user_id INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE mandates ADD COLUMN email_templates TEXT DEFAULT '[]'")
    except sqlite3.OperationalError:
        pass
    # Link a mandate to a CRM client (Option B: proper foreign key)
    try:
        c.execute("ALTER TABLE mandates ADD COLUMN crm_client_id INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # Timestamped client notes per mandate (hidden from candidates, used in AI rating)
    c.execute('''CREATE TABLE IF NOT EXISTS mandate_client_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mandate_id INTEGER NOT NULL,
        owner_id INTEGER NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        created_by INTEGER DEFAULT 0,
        created_at TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1
    )''')
    try:
        c.execute('CREATE INDEX IF NOT EXISTS idx_mcn_mandate ON mandate_client_notes(mandate_id, is_active)')
    except sqlite3.OperationalError:
        pass
    # 2-way email: stores both sent and received messages, threaded by Message-ID
    c.execute('''CREATE TABLE IF NOT EXISTS email_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        candidate_id INTEGER DEFAULT 0,
        direction TEXT DEFAULT 'sent',
        from_addr TEXT DEFAULT '',
        to_addr TEXT DEFAULT '',
        subject TEXT DEFAULT '',
        body TEXT DEFAULT '',
        message_id TEXT DEFAULT '',
        in_reply_to TEXT DEFAULT '',
        sent_at TEXT DEFAULT '',
        created_at TEXT DEFAULT ''
    )''')
    for sql in [
        'CREATE INDEX IF NOT EXISTS idx_em_candidate ON email_messages(candidate_id, sent_at)',
        'CREATE INDEX IF NOT EXISTS idx_em_company ON email_messages(company_id)',
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_em_msgid ON email_messages(company_id, message_id)',
    ]:
        try:
            c.execute(sql)
        except sqlite3.OperationalError:
            pass
    try:
        c.execute("ALTER TABLE submissions ADD COLUMN task_snoozed_until TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # ── Audit & Activity Foundation (PRD-0) ─────────────────────────────────
    # Extend the existing activity_log (non-destructively) so any module can log
    # structured, entity-scoped events for the universal timeline.
    for col, defn in [
        ('company_id', 'INTEGER DEFAULT 0'),   # tenant scope
        ('entity_type', "TEXT DEFAULT ''"),    # e.g. 'candidate','client','invoice'
        ('entity_id', 'INTEGER DEFAULT 0'),    # id of that entity
        ('actor_type', "TEXT DEFAULT 'user'"), # 'user' | 'client' | 'system'
        ('actor_name', "TEXT DEFAULT ''"),     # display name (client contacts have no user row)
        ('meta', "TEXT DEFAULT ''"),           # optional JSON payload for automation
    ]:
        try:
            c.execute(f'ALTER TABLE activity_log ADD COLUMN {col} {defn}')
        except sqlite3.OperationalError:
            pass
    # Field-level audit table: every important change records old → new value.
    c.execute('''CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER DEFAULT 0,
        entity_type TEXT DEFAULT '',
        entity_id INTEGER DEFAULT 0,
        field TEXT DEFAULT '',
        old_value TEXT DEFAULT '',
        new_value TEXT DEFAULT '',
        actor_type TEXT DEFAULT 'user',
        actor_id INTEGER DEFAULT 0,
        actor_name TEXT DEFAULT '',
        created_at TEXT DEFAULT ''
    )''')
    # CRM attachments (Sprint 2) — files attached to a company or a contact.
    c.execute('''CREATE TABLE IF NOT EXISTS crm_attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,              -- tenant
        client_id INTEGER DEFAULT 0,              -- always resolved (contact -> its client)
        entity_type TEXT DEFAULT 'client',        -- client | contact
        entity_id INTEGER DEFAULT 0,
        category TEXT DEFAULT 'Other',            -- NDA | Agreement | PO | ...
        original_name TEXT DEFAULT '',
        stored_name TEXT DEFAULT '',              -- filename on disk (CRM_FILES_DIR)
        size_bytes INTEGER DEFAULT 0,
        mime TEXT DEFAULT '',
        uploaded_by INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,              -- soft delete
        created_at TEXT DEFAULT ''
    )''')
    for idx_sql in [
        'CREATE INDEX IF NOT EXISTS idx_activity_entity ON activity_log(entity_type, entity_id)',
        'CREATE INDEX IF NOT EXISTS idx_activity_company ON activity_log(company_id, created_at)',
        'CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id)',
        'CREATE INDEX IF NOT EXISTS idx_audit_company ON audit_log(company_id, created_at)',
        'CREATE INDEX IF NOT EXISTS idx_crm_att_entity ON crm_attachments(company_id, entity_type, entity_id, is_active)',
        'CREATE INDEX IF NOT EXISTS idx_crm_att_client ON crm_attachments(company_id, client_id, is_active)',
    ]:
        try:
            c.execute(idx_sql)
        except sqlite3.OperationalError:
            pass
    try:
        c.execute('ALTER TABLE users ADD COLUMN is_company_admin INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    # Billing columns on companies (for existing DBs)
    for col, defn in [
        ('billing_status', "TEXT DEFAULT 'trial'"),
        ('trial_ends_at', "TEXT DEFAULT ''"),
        ('price_per_recruiter', 'INTEGER DEFAULT 700'),
        ('cf_subscription_id', "TEXT DEFAULT ''"),
        ('gstin', "TEXT DEFAULT ''"),
    ]:
        try:
            c.execute(f'ALTER TABLE companies ADD COLUMN {col} {defn}')
        except sqlite3.OperationalError:
            pass
    # The platform owner's own company (the first one) is not on trial — it's
    # the owner, mark it active so the owner never bills themselves.
    try:
        c.execute("UPDATE companies SET billing_status='owner' WHERE plan='owner'")
    except sqlite3.OperationalError:
        pass
    # Backfill company-admin: platform super-admins, and the first (lowest-id)
    # user of each company (the agency's own admin from signup/setup).
    try:
        c.execute("UPDATE users SET is_company_admin=1 WHERE role='admin'")
        c.execute('''UPDATE users SET is_company_admin=1 WHERE id IN (
            SELECT MIN(id) FROM users WHERE company_id>0 GROUP BY company_id)''')
    except sqlite3.OperationalError:
        pass

    # Trigger: a candidate inherits its mandate's owner_id automatically, so
    # every insert path (manual, extension, bulk, central-db, import) is covered
    # without touching each one. Only fills when owner_id is 0/NULL.
    try:
        c.execute('''CREATE TRIGGER IF NOT EXISTS candidate_inherit_owner
                     AFTER INSERT ON candidates
                     FOR EACH ROW WHEN (NEW.owner_id IS NULL OR NEW.owner_id=0)
                     BEGIN
                       UPDATE candidates SET owner_id =
                         (SELECT owner_id FROM mandates WHERE id = NEW.mandate_id)
                       WHERE id = NEW.id;
                     END''')
    except sqlite3.OperationalError:
        pass

    # Migrate: add embedding columns to candidates for semantic search
    for col, typ in [('embedding', 'TEXT'), ('embedding_text', 'TEXT'), ('embedded_at', 'TEXT')]:
        try:
            c.execute(f'ALTER TABLE candidates ADD COLUMN {col} {typ} DEFAULT ""')
        except sqlite3.OperationalError:
            pass  # already exists

    # Migrate: embedding METADATA columns (Feature 2 — versioning). Lets us
    # migrate embedding models later without breaking existing vectors.
    # embedded_at already added above. All idempotent.
    for col, typ in [
        ('embedding_model',        'TEXT DEFAULT ""'),
        ('embedding_version',      'TEXT DEFAULT ""'),
        ('embedding_dimension',    'INTEGER DEFAULT 0'),
        ('embedding_status',       'TEXT DEFAULT ""'),
        ('embedding_error',        'TEXT DEFAULT ""'),
        ('embedding_duration_ms',  'INTEGER DEFAULT 0'),
        ('embedding_text_version', 'TEXT DEFAULT ""'),
        ('embedding_vec',          'BLOB'),  # Sprint 4: float32 fast-read cache
    ]:
        try:
            c.execute(f'ALTER TABLE candidates ADD COLUMN {col} {typ}')
        except sqlite3.OperationalError:
            pass  # already exists

    # Backfill metadata for pre-existing vectors (one-time, idempotent: only
    # rows with no status yet are touched, so this is a no-op on later boots).
    # Existing vectors were built by the OLD text builder -> template v1.
    try:
        unstamped = c.execute(
            "SELECT id, embedding FROM candidates "
            "WHERE embedding_status IS NULL OR embedding_status=''").fetchall()
        bf_completed = bf_missing = bf_pending = 0
        for row in unstamped:
            emb = row['embedding']
            if emb is None or emb == '':
                # never embedded
                c.execute("UPDATE candidates SET embedding_status='pending' WHERE id=?", (row['id'],))
                bf_pending += 1
                continue
            dim = 0
            try:
                v = json.loads(emb)
                dim = len(v) if isinstance(v, list) else 0
            except Exception:
                dim = 0
            if dim > 0:
                c.execute(
                    "UPDATE candidates SET embedding_status='completed', embedding_model=?, "
                    "embedding_version=?, embedding_dimension=?, embedding_text_version=? WHERE id=?",
                    ('gemini-embedding-001', 'v1', dim, 'candidate-template-v1', row['id']))
                bf_completed += 1
            else:
                # embedding was '[]' (blank text) or unparseable -> missing
                c.execute(
                    "UPDATE candidates SET embedding_status='missing', embedding_model=?, "
                    "embedding_version=?, embedding_dimension=0, embedding_text_version=? WHERE id=?",
                    ('gemini-embedding-001', 'v1', 'candidate-template-v1', row['id']))
                bf_missing += 1
        if unstamped:
            conn.commit()
            print(f'[embed-migrate] backfilled metadata: completed={bf_completed} '
                  f'missing={bf_missing} pending={bf_pending}')
    except sqlite3.OperationalError:
        pass  # candidates table not ready yet on a fresh DB; nothing to backfill

    # Migrate: embedding job QUEUE (Sprint 3). Persistent so queued/failed jobs
    # survive a cloud restart and resume automatically. One row per embedding
    # attempt lifecycle for a candidate.
    c.execute('''CREATE TABLE IF NOT EXISTS embedding_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER NOT NULL,
        status TEXT DEFAULT 'pending',      -- pending/processing/completed/failed/retrying/cancelled
        retry_count INTEGER DEFAULT 0,
        max_retries INTEGER DEFAULT 5,
        last_error TEXT DEFAULT '',
        duration_ms INTEGER DEFAULT 0,
        created_at TEXT DEFAULT '',
        started_at TEXT DEFAULT '',
        completed_at TEXT DEFAULT '',
        next_attempt_at TEXT DEFAULT ''
    )''')
    # Poll index (find due jobs fast) + candidate index (idempotency checks).
    for idx_sql in (
        "CREATE INDEX IF NOT EXISTS idx_embjobs_due ON embedding_jobs(status, next_attempt_at)",
        "CREATE INDEX IF NOT EXISTS idx_embjobs_cand ON embedding_jobs(candidate_id)",
    ):
        try:
            c.execute(idx_sql)
        except sqlite3.OperationalError:
            pass

    # Migrate: MULTI-VECTOR embeddings (Phase 2 / Sprint 8). One row per
    # (candidate, facet) so a query can match the RIGHT part of a profile —
    # skills vs experience vs projects — instead of one diluted whole-profile
    # vector. The whole-profile 'full' vector stays on candidates.embedding_vec
    # (search is unchanged); these are additive, blob-only (no JSON) to save disk.
    c.execute('''CREATE TABLE IF NOT EXISTS candidate_vectors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id  INTEGER NOT NULL,
        facet         TEXT NOT NULL,        -- skills / experience / projects
        embedding_vec BLOB,                 -- float32 bytes (same format as candidates.embedding_vec)
        embedding_text TEXT DEFAULT '',
        embedding_model TEXT DEFAULT '',
        embedding_version TEXT DEFAULT '',
        embedding_dimension INTEGER DEFAULT 0,
        embedding_text_version TEXT DEFAULT '',
        status        TEXT DEFAULT 'pending',
        embedded_at   TEXT DEFAULT ''
    )''')
    for idx_sql in (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_candvec ON candidate_vectors(candidate_id, facet)",
        "CREATE INDEX IF NOT EXISTS idx_candvec_cand ON candidate_vectors(candidate_id)",
        "CREATE INDEX IF NOT EXISTS idx_candvec_facet ON candidate_vectors(facet)",
    ):
        try:
            c.execute(idx_sql)
        except sqlite3.OperationalError:
            pass

    # Migrate: persistent JOB-DESCRIPTION embeddings (Phase 2 / Sprint 9).
    # One reusable vector per mandate so candidate<->JD matching never re-embeds
    # the JD. Kept in a SEPARATE table (not a BLOB on mandates) so the mandates
    # list stays light and JSON-safe.
    c.execute('''CREATE TABLE IF NOT EXISTS mandate_vectors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mandate_id    INTEGER NOT NULL,
        embedding_vec BLOB,
        embedding_text TEXT DEFAULT '',
        embedding_model TEXT DEFAULT '',
        embedding_version TEXT DEFAULT '',
        embedding_dimension INTEGER DEFAULT 0,
        embedding_text_version TEXT DEFAULT '',
        status        TEXT DEFAULT 'pending',
        embedded_at   TEXT DEFAULT ''
    )''')
    for idx_sql in (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_mandvec ON mandate_vectors(mandate_id)",
        "CREATE INDEX IF NOT EXISTS idx_mandvec_status ON mandate_vectors(status)",
    ):
        try:
            c.execute(idx_sql)
        except sqlite3.OperationalError:
            pass

    # ══════════════════════════════════════════════════════════════════
    #  RECRUITMENT MEMORY ENGINE (Sprint 5) — architecture foundation.
    #  Three generic, cross-entity tables that let ANY business object
    #  (candidate, job, client, company, recruiter, skill, industry, ...)
    #  carry long-term memories, an event timeline, and relationships.
    #  Namespaced rme_* so they COMPLEMENT (not replace) the existing
    #  candidate_events / activity_log / stage_history tables. Nothing is
    #  auto-generated yet — this sprint builds the shape only.
    #  Extensibility: JSON metadata columns + schema_version mean future
    #  fields need no migration.
    # ══════════════════════════════════════════════════════════════════
    c.execute('''CREATE TABLE IF NOT EXISTS rme_memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL,          -- candidate/job/client/company/recruiter/skill/...
        entity_id   TEXT NOT NULL,          -- id or slug of the entity (stored as text = flexible)
        memory_type TEXT DEFAULT 'fact',    -- fact/event/relationship/observation/preference/ai_insight/recruiter_note/system_note/interaction
        title       TEXT DEFAULT '',
        content     TEXT DEFAULT '',
        source      TEXT DEFAULT 'system',  -- recruiter/system/ai/import
        created_by  TEXT DEFAULT '',        -- username / user id / 'system'
        created_at  TEXT DEFAULT '',
        updated_at  TEXT DEFAULT '',
        visibility  TEXT DEFAULT 'internal',-- internal/team/private/candidate_visible
        confidence  REAL DEFAULT 1.0,       -- 0..1 (mainly for ai_insight)
        importance  INTEGER DEFAULT 0,      -- 0..5 priority
        tags        TEXT DEFAULT '[]',      -- JSON array
        metadata    TEXT DEFAULT '{}',      -- JSON, free-form future fields
        status      TEXT DEFAULT 'active',  -- active/archived/deleted
        schema_version INTEGER DEFAULT 1
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS rme_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type  TEXT NOT NULL,          -- candidate_created/resume_uploaded/interview_scheduled/...
        entity_type TEXT NOT NULL,
        entity_id   TEXT NOT NULL,
        actor       TEXT DEFAULT 'system',  -- who/what caused it
        summary     TEXT DEFAULT '',
        data        TEXT DEFAULT '{}',      -- JSON payload
        related_entity_type TEXT DEFAULT '',-- optional cross-link (e.g. candidate event about a job)
        related_entity_id   TEXT DEFAULT '',
        created_at  TEXT DEFAULT '',
        schema_version INTEGER DEFAULT 1
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS rme_relationships (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_type TEXT NOT NULL, from_id TEXT NOT NULL,
        to_type   TEXT NOT NULL, to_id   TEXT NOT NULL,
        rel_type  TEXT NOT NULL,            -- works_at/has_skill/uses_technology/in_industry/managed_by/prefers/...
        weight     REAL DEFAULT 1.0,        -- strength
        confidence REAL DEFAULT 1.0,
        source     TEXT DEFAULT 'system',
        metadata   TEXT DEFAULT '{}',
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        status     TEXT DEFAULT 'active',
        schema_version INTEGER DEFAULT 1
    )''')
    for idx_sql in (
        "CREATE INDEX IF NOT EXISTS idx_rme_mem_entity ON rme_memories(entity_type, entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_rme_mem_type   ON rme_memories(memory_type)",
        "CREATE INDEX IF NOT EXISTS idx_rme_mem_status ON rme_memories(status)",
        "CREATE INDEX IF NOT EXISTS idx_rme_evt_entity ON rme_events(entity_type, entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_rme_evt_type   ON rme_events(event_type)",
        "CREATE INDEX IF NOT EXISTS idx_rme_evt_time   ON rme_events(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_rme_rel_from   ON rme_relationships(from_type, from_id)",
        "CREATE INDEX IF NOT EXISTS idx_rme_rel_to     ON rme_relationships(to_type, to_id)",
        "CREATE INDEX IF NOT EXISTS idx_rme_rel_type   ON rme_relationships(rel_type)",
        # one row per (from, to, rel_type) — enables upsert, prevents dup edges
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_rme_rel ON rme_relationships(from_type, from_id, to_type, to_id, rel_type)",
    ):
        try:
            c.execute(idx_sql)
        except sqlite3.OperationalError:
            pass

    # ══════════════════════════════════════════════════════════════════
    #  RECRUITMENT KNOWLEDGE GRAPH (Sprint 7) — architecture foundation.
    #  A canonical CONCEPT registry (rkg_entities) + typed edges (rkg_edges).
    #  Distinct from rme_relationships (which links business objects by id):
    #  the RKG deduplicates concepts via normalization + aliases, so "MCC",
    #  "Motor Control Centre" and "Motor Control Center" collapse to ONE node.
    #  All access goes through rkg_* functions (a repository layer) so a real
    #  graph engine (Neo4j, etc.) can replace the storage later without any
    #  business-logic change. No extraction/reasoning yet — shape only.
    # ══════════════════════════════════════════════════════════════════
    c.execute('''CREATE TABLE IF NOT EXISTS rkg_entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type     TEXT NOT NULL,       -- skill/technology/company/certification/industry/product/location/...
        display_name    TEXT NOT NULL,       -- human form, e.g. "Motor Control Centre"
        normalized_name TEXT NOT NULL,       -- canonical dedup key, e.g. "motorcontrolcentre"
        aliases         TEXT DEFAULT '[]',   -- JSON array of surface forms
        description     TEXT DEFAULT '',
        status          TEXT DEFAULT 'active',
        metadata        TEXT DEFAULT '{}',
        created_at      TEXT DEFAULT '',
        updated_at      TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS rkg_edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id  INTEGER NOT NULL,         -- -> rkg_entities.id
        target_id  INTEGER NOT NULL,         -- -> rkg_entities.id
        rel_type   TEXT NOT NULL,            -- WORKED_AT/HAS_SKILL/MANUFACTURES/REQUIRES_SKILL/...
        confidence REAL DEFAULT 1.0,
        source     TEXT DEFAULT 'system',
        status     TEXT DEFAULT 'active',
        metadata   TEXT DEFAULT '{}',
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT ''
    )''')
    for idx_sql in (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_rkg_entity ON rkg_entities(entity_type, normalized_name)",
        "CREATE INDEX IF NOT EXISTS idx_rkg_entity_type ON rkg_entities(entity_type)",
        "CREATE INDEX IF NOT EXISTS idx_rkg_entity_norm ON rkg_entities(normalized_name)",
        "CREATE INDEX IF NOT EXISTS idx_rkg_edge_src  ON rkg_edges(source_id)",
        "CREATE INDEX IF NOT EXISTS idx_rkg_edge_tgt  ON rkg_edges(target_id)",
        "CREATE INDEX IF NOT EXISTS idx_rkg_edge_type ON rkg_edges(rel_type)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_rkg_edge ON rkg_edges(source_id, target_id, rel_type)",
    ):
        try:
            c.execute(idx_sql)
        except sqlite3.OperationalError:
            pass

    # Alias resolution table — makes normalization DATA-driven (not a hardcoded
    # dictionary): every surface form maps to its canonical entity so future
    # lookups of a learned alias resolve to the same node.
    c.execute('''CREATE TABLE IF NOT EXISTS rkg_aliases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id        INTEGER NOT NULL,
        entity_type      TEXT NOT NULL,
        normalized_alias TEXT NOT NULL,
        created_at       TEXT DEFAULT ''
    )''')
    for idx_sql in (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_rkg_alias ON rkg_aliases(entity_type, normalized_alias)",
        "CREATE INDEX IF NOT EXISTS idx_rkg_alias_entity ON rkg_aliases(entity_id)",
    ):
        try:
            c.execute(idx_sql)
        except sqlite3.OperationalError:
            pass

    # Migrate: add reminders table if not exists
    c.execute('''CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER NOT NULL,
        mandate_id INTEGER,
        candidate_name TEXT DEFAULT '',
        mandate_label TEXT DEFAULT '',
        note TEXT DEFAULT '',
        due_at TEXT NOT NULL,
        done INTEGER DEFAULT 0,
        created_at TEXT
    )''')

    # Migrate: add new tag columns to existing DBs
    for col, defn in [
        ('product_handles', 'TEXT DEFAULT "[]"'),
        ('function_tags', 'TEXT DEFAULT "[]"'),
        ('status_tags', 'TEXT DEFAULT "[]"'),
        ('preferred_location', "TEXT DEFAULT ''"),
        ('task_snoozed_until', "TEXT DEFAULT ''"),
        ('update_token', "TEXT DEFAULT ''"),
        ('update_requested_at', "TEXT DEFAULT ''"),
        ('update_submitted_at', "TEXT DEFAULT ''"),
        ('linkedin_url', "TEXT DEFAULT ''"),
        ('ai_insight_cv', "TEXT DEFAULT ''"),
        ('sourced_by', "INTEGER DEFAULT 0"),
        ('sourced_at', "TEXT DEFAULT ''"),
    ]:
        try:
            c.execute(f'ALTER TABLE candidates ADD COLUMN {col} {defn}')
        except Exception:
            pass

    # Migrate: reminder scheduler columns (push notifications + snooze)
    for col, defn in [
        ('notified_at', "TEXT DEFAULT ''"),       # last time a push was sent
        ('snoozed_until', "TEXT DEFAULT ''"),      # if snoozed, don't notify until this time
        ('early_warned', "INTEGER DEFAULT 0"),     # sent the 5-10 min advance warning?
        ('owner_id', "INTEGER DEFAULT 0"),
    ]:
        try:
            c.execute(f'ALTER TABLE reminders ADD COLUMN {col} {defn}')
        except Exception:
            pass

    # Migrate: add signup-approval + company columns to existing 'users' table
    for col, defn in [
        ('status', "TEXT DEFAULT 'approved'"),
        ('company_name', "TEXT DEFAULT ''"),
        ('requested_at', 'TEXT'),
        ('company_id', 'INTEGER DEFAULT 0'),
        ('profile_phone', "TEXT DEFAULT ''"),
        ('profile_designation', "TEXT DEFAULT ''"),
        ('profile_email', "TEXT DEFAULT ''"),
    ]:
        try:
            c.execute(f'ALTER TABLE users ADD COLUMN {col} {defn}')
        except Exception:
            pass
    # Backfill: any pre-existing user (created before this column existed)
    # must default to 'approved' so nobody already in the system gets locked
    # out by the new approval gate.
    try:
        c.execute("UPDATE users SET status='approved' WHERE status IS NULL OR status=''")
    except Exception:
        pass

    conn.commit()

    # ── ONE-TIME MULTI-TENANT MIGRATION ────────────────────────────────────
    # Phase 2: introduce companies (tenants). Data isolation is by company.
    # We REUSE the existing owner_id column on mandates/candidates/reminders as
    # the TENANT (company) id — every data-filtering query already filters on
    # owner_id, so isolation is enforced everywhere automatically and there is
    # no risk of a missed filter leaking data across tenants.
    #
    # This block runs only once: if there are users but no companies yet, we:
    #   1. Create one company per the user's stated company_name (or a default
    #      "HireLab" company for the admin), so existing data stays together as
    #      a single agency exactly as it is today.
    #   2. Point every user at their company.
    #   3. Remap all existing data rows: owner_id (currently a USER id) becomes
    #      the COMPANY id of whoever owned it.
    try:
        have_users = c.execute('SELECT COUNT(*) n FROM users').fetchone()['n']
        have_companies = c.execute('SELECT COUNT(*) n FROM companies').fetchone()['n']
    except Exception:
        have_users, have_companies = 0, 0

    if have_users > 0 and have_companies == 0:
        print('*** Phase-2 tenant migration: creating companies for existing users ***')
        # All existing users belong to ONE agency: "HireLab" (the original
        # single-company system). This matches the owner's mental model that
        # the existing 290 candidates / 10 mandates are HireLab's own data.
        # New signups (post-migration) each get their OWN company.
        admin_row = c.execute("SELECT id, company_name FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        default_name = ''
        if admin_row and (admin_row['company_name'] or '').strip():
            default_name = admin_row['company_name'].strip()
        if not default_name:
            default_name = 'HireLab'
        c.execute("INSERT INTO companies (name,status,plan,created_at) VALUES (?,?,?,?)",
                  (default_name, 'active', 'owner', ts()))
        hirelab_company_id = c.lastrowid
        # Point every existing user at this company.
        c.execute('UPDATE users SET company_id=?', (hirelab_company_id,))
        # Remap ALL existing data to this single company (owner_id was a user id
        # before; now it is the company id). Existing data becomes HireLab's.
        for tbl in ['mandates', 'candidates', 'reminders']:
            try:
                c.execute(f'UPDATE {tbl} SET owner_id=?', (hirelab_company_id,))
            except Exception:
                pass
        conn.commit()
        print(f'*** Tenant migration complete: company "{default_name}" (id={hirelab_company_id}) now owns all existing data ***')

    # ── RecruitOS modules: import them first (registers their migrations),
    #    then build their tables alongside core schema ──────────────────────
    try:
        import modules
        modules.import_all_modules()
        modules.run_migrations(conn)
    except Exception as e:
        print(f'[modules] migration hook skipped: {e}')

    conn.commit(); conn.close()

    # One-time safety migration: if this DB file still has a pending -wal file
    # on disk from before the journal-mode fix, force a full checkpoint so
    # those writes land in the main DB file before we proceed. Harmless no-op
    # if the DB was already in DELETE mode (no -wal file exists).
    try:
        wal_path = DB_PATH + '-wal'
        if os.path.exists(wal_path):
            _c2 = sqlite3.connect(DB_PATH, timeout=60)
            _c2.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            _c2.execute('PRAGMA journal_mode=DELETE')
            _c2.close()
            print('*** One-time WAL checkpoint completed: pending writes flushed to main DB file ***')
    except Exception as _wal_err:
        print(f'WAL checkpoint warning (non-fatal): {_wal_err}')


def migrate_old():
    if os.path.exists(DB_PATH):
        return
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for old_path in [os.path.join(script_dir, 'hirelab.db'), os.path.join(os.getcwd(), 'hirelab.db')]:
        if os.path.exists(old_path):
            shutil.copy2(old_path, DB_PATH)
            print(f'[MIGRATE] {old_path} -> {DB_PATH}')
            old_cvs = os.path.join(os.path.dirname(old_path), 'cvs')
            if os.path.exists(old_cvs):
                for f in os.listdir(old_cvs):
                    src = os.path.join(old_cvs, f)
                    dst = os.path.join(CV_DIR, f)
                    if os.path.isfile(src) and not os.path.exists(dst):
                        shutil.copy2(src, dst)
            break

def log_candidate_event(cid, event_type, detail=''):
    """Record a journey event (tag added, call analysed, etc.) for a candidate."""
    try:
        conn = get_db()
        conn.execute('INSERT INTO candidate_events (candidate_id,event_type,detail,created_at) VALUES (?,?,?,?)',
                     (cid, event_type, detail, ts()))
        conn.commit(); conn.close()
    except Exception as e:
        print(f'log_candidate_event warning: {e}')


def daily_backup():
    """Snapshot the DB once per day. NEVER snapshot an empty DB over an existing
    good backup, and refresh today's backup if the live DB now has more users
    than the stored snapshot (so a startup-time empty snapshot can't 'stick')."""
    if not os.path.exists(DB_PATH):
        return
    live_users = _db_user_count(DB_PATH)
    if live_users == 0:
        return  # Never back up an empty DB — it could clobber a good backup.
    bak = os.path.join(BAK_DIR, f'hirelab_{datetime.date.today()}.db')
    existing_users = _db_user_count(bak) if os.path.exists(bak) else -1
    if (not os.path.exists(bak)) or live_users >= existing_users:
        shutil.copy2(DB_PATH, bak)
        print(f'[BACKUP] {bak} ({live_users} users)')
    for old in sorted(Path(BAK_DIR).glob('hirelab_*.db'))[:-7]:
        old.unlink()


def _db_user_count(path):
    """How many users a given SQLite file holds (0 if unreadable/missing)."""
    try:
        if not os.path.exists(path):
            return 0
        c = sqlite3.connect(path, timeout=10)
        n = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        c.close()
        return n
    except Exception:
        return 0


def auto_restore_if_empty():
    """SAFETY NET against the 'logged out → asked to create a new account →
    old data gone' problem. If the live DB has zero users (e.g. a restart lost
    recent writes, or the file was recreated empty), but a backup on disk DOES
    contain users, restore the newest such backup automatically. This runs at
    every startup, including under gunicorn on the cloud."""
    try:
        live_users = _db_user_count(DB_PATH)
        if live_users > 0:
            return  # DB is healthy, nothing to do.
        # Pick the backup with the MOST users (most complete), newest as tiebreak.
        backups = sorted(Path(BAK_DIR).glob('hirelab_*.db'),
                         key=lambda p: (_db_user_count(str(p)), p.stat().st_mtime),
                         reverse=True)
        for bak in backups:
            if _db_user_count(str(bak)) > 0:
                # Keep a copy of the (empty) current file just in case.
                try:
                    if os.path.exists(DB_PATH):
                        shutil.copy2(DB_PATH, DB_PATH + '.empty-before-restore')
                except Exception:
                    pass
                shutil.copy2(str(bak), DB_PATH)
                print(f'*** AUTO-RESTORE: live DB had 0 users — restored from backup {bak.name} '
                      f'({_db_user_count(DB_PATH)} users recovered). ***')
                return
        if not backups:
            print('*** AUTO-RESTORE: DB empty and NO backups found on disk. '
                  'If this is a restart, your storage may NOT be persistent — see /api/diag. ***')
    except Exception as e:
        print(f'Auto-restore warning (non-fatal): {e}')


def check_storage_persistence():
    """Definitively detect whether DATA_DIR survives restarts. We write a marker
    file containing a boot counter. If the marker is MISSING on a later boot,
    the storage is ephemeral (data WILL be lost on every restart) — the true
    root cause of the 'asks me to create a new account again' problem. Returns
    a dict used by /api/diag and the startup banner."""
    marker = os.path.join(DATA_DIR, '.persistence_check')
    result = {'marker_path': marker, 'persistent': None, 'boots_seen': 0}
    try:
        prev = ''
        if os.path.exists(marker):
            with open(marker) as f:
                prev = f.read().strip()
        if prev:
            # Marker survived a previous boot → storage IS persistent.
            try:
                boots = int(prev.split('|')[0]) + 1
            except Exception:
                boots = 1
            result['persistent'] = True
            result['boots_seen'] = boots
        else:
            # First boot (or marker was wiped). Can't conclude persistence yet.
            boots = 1
            result['persistent'] = None  # unknown until we see it survive once
            result['boots_seen'] = 1
        with open(marker, 'w') as f:
            f.write(f'{boots}|{datetime.datetime.now().isoformat()}')
    except Exception as e:
        result['error'] = str(e)
    return result


# Cache the persistence result computed at startup.
_PERSISTENCE = {'persistent': None, 'boots_seen': 0}

def check_timers():
    conn = get_db(); c = conn.cursor()
    r1 = c.execute("SELECT value FROM settings WHERE key='fu1_hours'").fetchone()
    r2 = c.execute("SELECT value FROM settings WHERE key='fu2_hours'").fetchone()
    fu1_h = float(r1['value']) if r1 else 8.0
    fu2_h = float(r2['value']) if r2 else 24.0
    n = datetime.datetime.utcnow()
    for cand in c.execute("SELECT id,msg1_sent_at FROM candidates WHERE msg1_sent_at!='' AND stage='Screening'").fetchall():
        try:
            if (n - datetime.datetime.fromisoformat(cand['msg1_sent_at'])).total_seconds() >= fu1_h * 3600:
                c.execute("UPDATE candidates SET stage='Follow Up 1',updated_at=? WHERE id=?", (ts(), cand['id']))
                c.execute("INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)",
                          (cand['id'], 'Screening', 'Follow Up 1', f'Auto-moved after {fu1_h}h', ts()))
        except Exception:
            pass
    for cand in c.execute("SELECT id,fu1_sent_at FROM candidates WHERE fu1_sent_at!='' AND stage='Follow Up 1'").fetchall():
        try:
            if (n - datetime.datetime.fromisoformat(cand['fu1_sent_at'])).total_seconds() >= fu2_h * 3600:
                c.execute("UPDATE candidates SET stage='Follow Up 2',updated_at=? WHERE id=?", (ts(), cand['id']))
                c.execute("INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)",
                          (cand['id'], 'Follow Up 1', 'Follow Up 2', f'Auto-moved after {fu2_h}h', ts()))
        except Exception:
            pass
    conn.commit(); conn.close()

# ── Per-tenant API usage & cost tracking ──────────────────────────────────
# Rates are EDITABLE ESTIMATES in USD. Token COUNTS logged below are exact
# (read from each API response); only these per-unit rates are approximate and
# can be adjusted any time without touching the logged history.
#   *_in  / *_out = USD per 1,000,000 tokens
#   whisper       = USD per second of audio
API_PRICING = {
    'claude':   {'in': 3.00,  'out': 15.00},   # Claude Sonnet (per 1M tokens)
    'deepseek': {'in': 0.27,  'out': 1.10},    # DeepSeek chat   (per 1M tokens)
    'gemini':   {'in': 0.075, 'out': 0.30},    # Gemini embed/flash (per 1M tokens)
    'groq':     {'audio_per_sec': 0.111/3600}, # Groq Whisper ~ $0.111 / audio-hour
}

def log_api_usage(provider, model='', input_tokens=0, output_tokens=0, audio_seconds=0, endpoint=''):
    """Record one AI API call against the current tenant, with an estimated
    cost. Fully defensive — never raises into the calling request."""
    try:
        p = API_PRICING.get(provider, {})
        cost = 0.0
        if audio_seconds:
            cost += float(audio_seconds) * p.get('audio_per_sec', 0)
        if input_tokens:
            cost += (float(input_tokens) / 1_000_000.0) * p.get('in', 0)
        if output_tokens:
            cost += (float(output_tokens) / 1_000_000.0) * p.get('out', 0)
        try:
            company_id = effective_company_id() or 0
        except Exception:
            company_id = 0
        conn = get_db()
        conn.execute('''INSERT INTO api_usage
            (company_id,provider,model,endpoint,input_tokens,output_tokens,audio_seconds,cost_usd,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (company_id, provider, model, endpoint,
             int(input_tokens or 0), int(output_tokens or 0),
             float(audio_seconds or 0), round(cost, 6), ts()))
        conn.commit(); conn.close()
    except Exception as e:
        print(f'log_api_usage warning (non-fatal): {e}')


def call_claude(api_key, system_msg, messages, max_tokens=8000, endpoint='claude'):
    resp = requests.post(CLAUDE_URL,
        headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
        json={'model': CLAUDE_MODEL, 'max_tokens': max_tokens, 'system': system_msg, 'messages': messages},
        timeout=120)
    # Log token usage (per tenant) without breaking the caller.
    try:
        u = resp.json().get('usage', {})
        if u:
            log_api_usage('claude', CLAUDE_MODEL,
                          input_tokens=u.get('input_tokens', 0),
                          output_tokens=u.get('output_tokens', 0),
                          endpoint=endpoint)
    except Exception:
        pass
    return resp

def call_deepseek(api_key, payload, timeout=60, endpoint='deepseek'):
    """POST to DeepSeek (OpenAI-compatible) and log token usage per tenant.
    Returns the raw requests response, so existing callers work unchanged."""
    resp = requests.post('https://api.deepseek.com/chat/completions',
        headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'},
        json=payload, timeout=timeout)
    try:
        u = resp.json().get('usage', {})
        if u:
            log_api_usage('deepseek', payload.get('model', 'deepseek-chat'),
                          input_tokens=u.get('prompt_tokens', 0),
                          output_tokens=u.get('completion_tokens', 0),
                          endpoint=endpoint)
    except Exception:
        pass
    return resp

def parse_json(text):
    text = text.strip()
    if '```' in text:
        for part in text.split('```'):
            p = part.strip()
            if p.startswith('json'): p = p[4:].strip()
            if p.startswith(('{', '[')):
                text = p; break
    for bracket in [('[', ']'), ('{', '}')]:
        s = text.find(bracket[0])
        if s >= 0:
            e = text.rfind(bracket[1]) + 1
            if e > s:
                try: return json.loads(text[s:e])
                except Exception: pass
    return None

# Map sensitive setting keys to environment variable names. If the env var is
# set (e.g. on Render), it OVERRIDES whatever is stored in the DB. This lets API
# keys live safely in the host environment instead of in code or the database.
_ENV_KEY_MAP = {
    'groq_api_key': 'GROQ_API_KEY',
    'claude_api_key': 'CLAUDE_API_KEY',
    'deepseek_api_key': 'DEEPSEEK_API_KEY',
    'gemini_api_key': 'GEMINI_API_KEY',
}

def get_setting(key, default=''):
    # Env var takes priority for sensitive keys
    env_name = _ENV_KEY_MAP.get(key)
    if env_name:
        env_val = os.environ.get(env_name, '').strip()
        if env_val:
            return env_val
    conn = get_db()
    r = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    conn.close()
    return r['value'] if r else default

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ROUTES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.route('/')
def index():
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html'))


# ── PWA: installable mobile app (manifest + service worker + icons) ────────
@app.route('/manifest.webmanifest')
def pwa_manifest():
    return jsonify({
        'name': 'HireLab Screener',
        'short_name': 'HireLab',
        'description': 'Recruitment ATS with mobile call assistant',
        'start_url': '/',
        'display': 'standalone',
        'background_color': '#0E2A47',
        'theme_color': '#0E2A47',
        'orientation': 'portrait-primary',
        'icons': [
            {'src': '/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any maskable'},
            {'src': '/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any maskable'},
        ]
    })

@app.route('/sw.js')
def pwa_sw():
    # Minimal network-first service worker. Its presence (with a fetch handler)
    # is what makes the app installable to the home screen. We deliberately do
    # NOT cache API responses so tenant data is always fresh from the server.
    sw = (
        "self.addEventListener('install', e => self.skipWaiting());\n"
        "self.addEventListener('activate', e => self.clients.claim());\n"
        "self.addEventListener('fetch', function(e){\n"
        "  // Pass through to network; no offline caching of data.\n"
        "  e.respondWith(fetch(e.request).catch(function(){\n"
        "    return new Response('Offline', {status: 503});\n"
        "  }));\n"
        "});\n"
    )
    return app.response_class(sw, mimetype='application/javascript')

@app.route('/icon-192.png')
def pwa_icon_192():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon-192.png')
    return send_file(p) if os.path.exists(p) else ('', 404)

@app.route('/icon-512.png')
def pwa_icon_512():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon-512.png')
    return send_file(p) if os.path.exists(p) else ('', 404)




@app.route('/api/db-status')
def db_status():
    """Check DB health — useful for debugging Railway/Render issues."""
    try:
        conn = get_db()
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t['name'] for t in tables]
        counts = {}
        for t in table_names:
            try:
                counts[t] = conn.execute(f'SELECT COUNT(*) as c FROM {t}').fetchone()['c']
            except Exception:
                counts[t] = -1
        conn.close()
        return jsonify({
            'ok': True,
            'db_path': DB_PATH,
            'data_dir': DATA_DIR,
            'tables': table_names,
            'counts': counts
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e), 'db_path': DB_PATH, 'data_dir': DATA_DIR}), 500


@app.route('/api/shorten-jd', methods=['POST'])
def shorten_jd():
    """Use DeepSeek to shorten a long JD/role-description text so it fits
    within Gmail's compose-URL length limit, while keeping it useful for
    a candidate outreach email (role purpose + 4-6 key responsibilities/
    highlights, in plain text, no markdown)."""
    d = request.json or {}
    text = (d.get('text') or '').strip()
    max_chars = int(d.get('max_chars') or 1200)
    if not text:
        return jsonify({'error': 'No text provided'}), 400

    ds_key = get_setting('deepseek_api_key')
    if not ds_key:
        return jsonify({'error': 'DeepSeek API key not set. Go to Settings.'}), 400

    system_msg = (
        "You are condensing a long job description into a well-formatted, "
        f"recruiter-friendly summary, under roughly {max_chars} characters total. "
        "Be thoughtful — preserve the most important and specific information "
        "(role purpose, key responsibilities, must-have requirements) and cut "
        "only repetitive, generic, or low-value detail. Do not cut sentences "
        "mid-way; every section and bullet must be complete and make sense.\n\n"
        "Output format (plain text, using these exact markers so it can be "
        "converted to formatted HTML):\n"
        "- A line starting with '## ' is a section heading (use short ones like "
        "'## Job Purpose', '## Key Responsibilities', '## Requirements').\n"
        "- A line starting with '- ' is a bullet point.\n"
        "- Any other non-empty line is a normal paragraph.\n"
        "- Separate sections/paragraphs/bullet-groups with a single blank line.\n\n"
        "Structure: 1 short 'Job Purpose' paragraph, then a 'Key Responsibilities' "
        "section with 4-7 bullets, then (if relevant) a short 'Requirements' "
        "section with 2-4 bullets. Do NOT use markdown bold/italic (**, *, _). "
        "Output ONLY the formatted text, nothing else."
    )

    try:
        resp = call_deepseek(ds_key,
            {'model': 'deepseek-chat', 'temperature': 0.3, 'max_tokens': 900,
                  'messages': [{'role': 'system', 'content': system_msg},
                                {'role': 'user', 'content': text[:12000]}]},
            timeout=60, endpoint='screening')
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    if resp.status_code != 200:
        try: err = resp.json().get('error', {}).get('message', 'DeepSeek API error')
        except Exception: err = resp.text[:200]
        return jsonify({'error': err}), 500

    shortened = resp.json()['choices'][0]['message']['content'].strip()
    return jsonify({'ok': True, 'shortened': shortened})



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  REMINDERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.route('/api/reminders', methods=['GET'])
@login_required
def get_reminders():
    conn = get_db()
    rows = conn.execute(
        'SELECT r.*, c.phone AS cand_phone, c.company AS cand_company, '
        'c.designation AS cand_designation, c.stage AS cand_stage '
        'FROM reminders r LEFT JOIN candidates c ON c.id = r.candidate_id '
        'WHERE r.done=0 AND r.owner_id=? ORDER BY r.due_at ASC',
        (effective_user_id(),)
    ).fetchall()
    conn.close()
    return jsonify({'ok': True, 'reminders': [dict(r) for r in rows]})

@app.route('/api/reminders', methods=['POST'])
@login_required
def add_reminder():
    d = request.json or {}
    cid   = d.get('candidate_id')
    note  = (d.get('note') or '').strip()
    due   = (d.get('due_at') or '').strip()
    if not cid or not due:
        return jsonify({'error': 'candidate_id and due_at required'}), 400

    conn = get_db()
    cand = conn.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
    if not cand:
        conn.close()
        return jsonify({'error': 'Candidate not found'}), 404

    mandate = conn.execute('SELECT * FROM mandates WHERE id=?', (cand['mandate_id'],)).fetchone()
    mandate_label = (mandate['role'] + ' — ' + mandate['client']) if mandate else ''

    conn.execute(
        'INSERT INTO reminders (candidate_id,mandate_id,candidate_name,mandate_label,note,due_at,done,created_at,owner_id) '
        'VALUES (?,?,?,?,?,?,0,?,?)',
        (cid, cand['mandate_id'], cand['name'] or '', mandate_label, note, due, ts(), effective_user_id())
    )
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/reminders/<int:rid>/done', methods=['POST'])
@login_required
def mark_reminder_done(rid):
    # Works from webapp AND mobile app — both hit the same reminder, so marking
    # done here stops mobile notifications and clears it from the webapp list.
    conn = get_db()
    conn.execute('UPDATE reminders SET done=1 WHERE id=?', (rid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/reminders/<int:rid>/snooze', methods=['POST'])
@login_required
def snooze_reminder(rid):
    """Snooze a reminder. Body: {"minutes": 10} or {"until": "tomorrow"}.
    Supported: 10, 30, 60 minutes, or 'tomorrow' (9am next day IST)."""
    d = request.json or {}
    now = _ist_now()
    until = None
    if d.get('until') == 'tomorrow' or d.get('minutes') == 'tomorrow':
        tomorrow = (now + datetime.timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        until = tomorrow
    else:
        try:
            mins = int(d.get('minutes', 10))
        except Exception:
            mins = 10
        if mins not in (10, 30, 60):
            mins = 10
        until = now + datetime.timedelta(minutes=mins)
    until_iso = until.isoformat(timespec='seconds')
    conn = get_db()
    # Reset notification state so it fires fresh after the snooze window
    conn.execute(
        'UPDATE reminders SET snoozed_until=?, notified_at="", early_warned=0 WHERE id=?',
        (until_iso, rid))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'snoozed_until': until_iso})


@app.route('/api/reminders/<int:rid>/edit', methods=['POST'])
@login_required
def edit_reminder(rid):
    """Edit a reminder's due time and/or note. Used by webapp and mobile."""
    d = request.json or {}
    conn = get_db()
    r = conn.execute('SELECT owner_id FROM reminders WHERE id=?', (rid,)).fetchone()
    if not r or r['owner_id'] != effective_user_id():
        conn.close()
        return jsonify({'error': 'Reminder not found'}), 404
    fields = []
    params = []
    if 'due_at' in d and (d.get('due_at') or '').strip():
        fields.append('due_at=?')
        params.append((d.get('due_at') or '').strip())
        # editing the time resets notification state so it fires fresh
        fields.append('notified_at=""')
        fields.append('early_warned=0')
        fields.append('snoozed_until=""')
    if 'note' in d:
        fields.append('note=?')
        params.append((d.get('note') or '').strip())
    if not fields:
        conn.close()
        return jsonify({'error': 'Nothing to update'}), 400
    params.append(rid)
    conn.execute(f'UPDATE reminders SET {", ".join(fields)} WHERE id=?', params)
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/reminders/<int:rid>', methods=['DELETE'])
@login_required
def delete_reminder(rid):
    conn = get_db()
    conn.execute('DELETE FROM reminders WHERE id=?', (rid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/dashboard-tasks')
@login_required
def dashboard_tasks_alias():
    """Legacy alias — the dashboard/badge now reuse the unified task engine."""
    return get_tasks()

PROMISE_TAGS = ['Will Callback', 'Call Later', 'Asked to Send JD', 'Interested']

@app.route('/api/analytics')
@login_required
def analytics():
    """Dashboard analytics. Admin sees the whole company; a recruiter sees only
    their own assigned mandates + candidates. Pass ?scope=me to force self-view."""
    conn = get_db()
    cid = effective_company_id()
    scope_me = request.args.get('scope') == 'me'
    admin = is_company_admin() and not scope_me

    stale_days = float(get_setting('analytics_stale_days', '7') or 7)
    now = _ist_now()

    # ── Date range for time-based metrics (placements, added, time-to-fill, sources) ──
    range_from, range_to = None, None
    rng = request.args.get('range', '7')
    q_from = request.args.get('from', '')
    q_to = request.args.get('to', '')
    if q_from or q_to:
        try:
            if q_from: range_from = datetime.datetime.fromisoformat(q_from + 'T00:00:00')
        except Exception: range_from = None
        try:
            if q_to: range_to = datetime.datetime.fromisoformat(q_to + 'T23:59:59')
        except Exception: range_to = None
        range_label = 'Custom range'
    elif rng == 'all':
        range_label = 'All time'
    else:
        try:
            days = int(rng)
        except Exception:
            days = 7
        range_from = now - datetime.timedelta(days=days)
        range_label = f'Last {days} days'

    def _in_range(iso_str):
        if not iso_str:
            return False
        try:
            dt = datetime.datetime.fromisoformat(iso_str)
        except Exception:
            return False
        if range_from and dt < range_from:
            return False
        if range_to and dt > range_to:
            return False
        return True

    # Determine which mandate ids are in scope
    if admin:
        mrows = conn.execute('SELECT * FROM mandates WHERE owner_id=?', (cid,)).fetchall()
    else:
        mrows = conn.execute('SELECT * FROM mandates WHERE owner_id=? AND assigned_user_id=?',
                             (cid, real_user_id())).fetchall()
    mandates = [dict(m) for m in mrows]
    mandate_ids = [m['id'] for m in mandates]

    def _in(ids):
        return '(' + ','.join('?' for _ in ids) + ')' if ids else '(NULL)'

    # KPI: mandate status counts
    active = sum(1 for m in mandates if (m.get('status') or 'active') == 'active')
    hold = sum(1 for m in mandates if m.get('status') == 'hold')
    closed = sum(1 for m in mandates if m.get('status') == 'closed')

    # Candidates in scope
    if mandate_ids:
        cand_rows = conn.execute(
            f'SELECT id, stage, mandate_id, updated_at, ai_reasoning, created_at FROM candidates WHERE mandate_id IN {_in(mandate_ids)}',
            mandate_ids).fetchall()
    else:
        cand_rows = []
    cands = [dict(c) for c in cand_rows]
    total_pipeline = len([c for c in cands if c['stage'] not in ('Placed', 'Not Interested', 'Not Suitable', 'Client Rejected on Paper', 'Client Rejected After Interview')])

    # Placements within the selected date range
    placed_ids = [c['id'] for c in cands if c['stage'] == 'Placed']
    placed_this_month = 0
    for c in cands:
        if c['stage'] == 'Placed':
            sh = conn.execute("SELECT created_at FROM stage_history WHERE candidate_id=? AND to_stage='Placed' ORDER BY created_at DESC LIMIT 1", (c['id'],)).fetchone()
            when = sh['created_at'] if sh else c['updated_at']
            if _in_range(when):
                placed_this_month += 1

    # Avg time-to-fill (days from candidate created → Placed) within range
    fill_days = []
    for c in cands:
        if c['stage'] == 'Placed':
            sh = conn.execute("SELECT created_at FROM stage_history WHERE candidate_id=? AND to_stage='Placed' ORDER BY created_at DESC LIMIT 1", (c['id'],)).fetchone()
            if sh and c['created_at']:
                try:
                    d0 = datetime.datetime.fromisoformat(c['created_at'])
                    d1 = datetime.datetime.fromisoformat(sh['created_at'])
                    if _in_range(sh['created_at']):
                        fill_days.append((d1 - d0).days)
                except Exception:
                    pass
    avg_ttf = round(sum(fill_days) / len(fill_days)) if fill_days else None

    # Pipeline funnel (grouped stages)
    funnel_map = [
        ('Screening', ['Screening']),
        ('Follow Up', ['Follow Up 1', 'Follow Up 2', 'Not Contacted', 'Called']),
        ('Interested', ['Interested', 'Updated CV awaited']),
        ('Shared with Client', ['Shared with Client']),
        ('Interview', ['Interview Inprocess']),
        ('Placed', ['Placed']),
    ]
    funnel = []
    for label, stages in funnel_map:
        funnel.append({'label': label, 'count': len([c for c in cands if c['stage'] in stages]), 'stages': stages})

    # Source effectiveness (of placed candidates, by source)
    def _source(reason):
        r = (reason or '').lower()
        if 'naukri' in r: return 'Naukri extension'
        if 'bulk' in r: return 'Bulk paste'
        if 'manual' in r: return 'Manual add'
        return 'Other'
    src_counts = {}
    for c in cands:
        if c['stage'] == 'Placed':
            sh = conn.execute("SELECT created_at FROM stage_history WHERE candidate_id=? AND to_stage='Placed' ORDER BY created_at DESC LIMIT 1", (c['id'],)).fetchone()
            when = sh['created_at'] if sh else c['updated_at']
            if not _in_range(when):
                continue
            s = _source(c['ai_reasoning'])
            src_counts[s] = src_counts.get(s, 0) + 1
    total_placed = sum(src_counts.values())
    sources = [{'source': k, 'count': v, 'pct': round(v * 100 / total_placed) if total_placed else 0}
               for k, v in sorted(src_counts.items(), key=lambda x: -x[1])]

    # Recruiter leaderboard (admin only)
    leaderboard = []
    if admin:
        team = conn.execute("SELECT id, display_name, username FROM users WHERE company_id=? AND status='approved'", (cid,)).fetchall()
        for u in team:
            uid = u['id']
            u_mandates = conn.execute('SELECT id FROM mandates WHERE owner_id=? AND assigned_user_id=?', (cid, uid)).fetchall()
            u_mids = [r['id'] for r in u_mandates]
            added = placed = interviews = 0
            if u_mids:
                u_cands = conn.execute(f'SELECT id, stage, created_at, updated_at FROM candidates WHERE mandate_id IN {_in(u_mids)}', u_mids).fetchall()
                for uc in u_cands:
                    if _in_range(uc['created_at']):
                        added += 1
                    if uc['stage'] == 'Placed':
                        sh = conn.execute("SELECT created_at FROM stage_history WHERE candidate_id=? AND to_stage='Placed' ORDER BY created_at DESC LIMIT 1", (uc['id'],)).fetchone()
                        if _in_range(sh['created_at'] if sh else uc['updated_at']):
                            placed += 1
                iv_rows = conn.execute(f'SELECT created_at FROM interviews WHERE mandate_id IN {_in(u_mids)}', u_mids).fetchall()
                interviews = sum(1 for r in iv_rows if _in_range(r['created_at']))
            leaderboard.append({'name': u['display_name'] or u['username'] or 'User',
                                'added': added, 'interviews': interviews, 'placed': placed})
        leaderboard.sort(key=lambda x: (-x['placed'], -x['added']))

    # Stale mandates (no candidate activity in stale_days) + stale candidates within
    stale_mandates = []
    for m in mandates:
        if (m.get('status') or 'active') != 'active':
            continue
        m_cands = [c for c in cands if c['mandate_id'] == m['id']]
        if not m_cands:
            continue
        latest = None
        for c in m_cands:
            for t in (c['updated_at'],):
                if t:
                    try:
                        dt = datetime.datetime.fromisoformat(t)
                        if not latest or dt > latest: latest = dt
                    except Exception:
                        pass
        if latest and (now - latest).days >= stale_days:
            stale_mandates.append({'id': m['id'], 'role': m['role'], 'client': m['client'],
                                   'days': (now - latest).days})
    stale_mandates.sort(key=lambda x: -x['days'])

    conn.close()
    return jsonify({'ok': True, 'is_admin_view': admin,
                    'kpi': {'open_mandates': len(mandates), 'active': active, 'hold': hold, 'closed': closed,
                            'placed_this_month': placed_this_month, 'avg_time_to_fill': avg_ttf,
                            'pipeline_candidates': total_pipeline},
                    'funnel': funnel, 'sources': sources, 'leaderboard': leaderboard,
                    'stale_mandates': stale_mandates, 'stale_days': int(stale_days),
                    'range_label': range_label})


@app.route('/api/analytics/stage-candidates')
@login_required
def analytics_stage_candidates():
    """All candidates in a given funnel-stage-group, across every in-scope mandate.
    Used when the user clicks a funnel bar (opens in a new tab via hash route)."""
    stages_param = request.args.get('stages', '')
    stages = [s for s in stages_param.split('||') if s]
    if not stages:
        return jsonify({'ok': True, 'candidates': []})
    conn = get_db()
    cid = effective_company_id()
    if is_company_admin():
        mrows = conn.execute('SELECT id, role, client FROM mandates WHERE owner_id=?', (cid,)).fetchall()
    else:
        mrows = conn.execute('SELECT id, role, client FROM mandates WHERE owner_id=? AND assigned_user_id=?',
                             (cid, real_user_id())).fetchall()
    mmap = {m['id']: dict(m) for m in mrows}
    mandate_ids = list(mmap.keys())
    if not mandate_ids:
        conn.close(); return jsonify({'ok': True, 'candidates': []})
    stale_days = float(get_setting('analytics_stale_days', '7') or 7)
    now = _ist_now()
    ph = '(' + ','.join('?' for _ in mandate_ids) + ')'
    sph = '(' + ','.join('?' for _ in stages) + ')'
    rows = conn.execute(
        f'SELECT id, name, company, designation, phone, email, stage, mandate_id, updated_at, cv_path '
        f'FROM candidates WHERE mandate_id IN {ph} AND stage IN {sph} ORDER BY name',
        mandate_ids + stages).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        m = mmap.get(d['mandate_id'], {})
        d['mandate_role'] = m.get('role', '')
        d['mandate_client'] = m.get('client', '')
        # stale flag
        d['is_stale'] = False
        if d['updated_at']:
            try:
                if (now - datetime.datetime.fromisoformat(d['updated_at'])).days >= stale_days:
                    d['is_stale'] = True
            except Exception:
                pass
        out.append(d)
    return jsonify({'ok': True, 'candidates': out, 'stages': stages, 'stale_days': int(stale_days)})

@app.route('/api/tasks')
@login_required
def get_tasks():
    """Unified follow-up task list for the dedicated Tasks tab.
    Sources: manual reminders, stale candidates (no activity in N days),
    promised follow-ups (a promise-tag with no activity after it for N hours),
    and new submissions. Each task carries a 'section' for Overdue/Today/
    Tomorrow/Upcoming grouping on the frontend."""
    conn = get_db()
    now = _ist_now()
    today = now.date()
    uid = effective_user_id()
    stale_days = float(get_setting('stale_days', '7') or 7)
    promise_hours = float(get_setting('promise_hours', '24') or 24)

    tasks = []

    def section_for(due_dt):
        if due_dt < now: return 'overdue'
        if due_dt.date() == today: return 'today'
        if due_dt.date() == today + datetime.timedelta(days=1): return 'tomorrow'
        return 'upcoming'

    # ── 1. Manual reminders ──────────────────────────────────────────────
    rem_rows = conn.execute(
        "SELECT r.*, c.phone AS cand_phone FROM reminders r "
        "LEFT JOIN mandates m ON m.id = r.mandate_id "
        "LEFT JOIN candidates c ON c.id = r.candidate_id "
        "WHERE r.done=0 AND r.owner_id=? AND (m.id IS NULL OR m.status NOT IN ('hold','closed')) "
        "ORDER BY r.due_at ASC", (uid,)
    ).fetchall()
    for r in rem_rows:
        try:
            due = datetime.datetime.fromisoformat(r['due_at'])
        except Exception:
            due = now
        tasks.append({
            'id': 'reminder-' + str(r['id']), 'type': 'reminder', 'ref_id': r['id'],
            'candidate_id': r['candidate_id'], 'mandate_id': r['mandate_id'],
            'title': r['candidate_name'] or 'Candidate',
            'subtitle': (r['note'] or 'Reminder') + (' \u00b7 ' + r['mandate_label'] if r['mandate_label'] else ''),
            'phone': (r['cand_phone'] if ('cand_phone' in r.keys()) else '') or '',
            'due_at': r['due_at'], 'section': section_for(due),
        })

    # ── Candidate base data for stale + promise detection ────────────────
    cand_rows = conn.execute(
        "SELECT c.id, c.name, c.phone, c.mandate_id, c.updated_at, c.task_snoozed_until, "
        "m.role, m.client FROM candidates c LEFT JOIN mandates m ON m.id=c.mandate_id "
        "WHERE c.owner_id=? AND (m.id IS NULL OR m.status NOT IN ('hold','closed'))", (uid,)
    ).fetchall()

    for c in cand_rows:
        snoozed = c['task_snoozed_until']
        if snoozed:
            try:
                if datetime.datetime.fromisoformat(snoozed) > now:
                    continue  # suppressed until this candidate's snooze passes
            except Exception:
                pass

        # Most recent event (any kind) for this candidate
        ev = conn.execute(
            "SELECT event_type, detail, created_at FROM candidate_events "
            "WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1", (c['id'],)
        ).fetchone()
        stg = conn.execute(
            "SELECT created_at FROM stage_history WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1", (c['id'],)
        ).fetchone()

        candidates_ts = [c['updated_at'] or '']
        if ev: candidates_ts.append(ev['created_at'] or '')
        if stg: candidates_ts.append(stg['created_at'] or '')
        candidates_ts = [t for t in candidates_ts if t]
        try:
            last_activity = max(datetime.datetime.fromisoformat(t) for t in candidates_ts) if candidates_ts else None
        except Exception:
            last_activity = None
        if not last_activity:
            continue

        mandate_label = (c['role'] + ' \u2014 ' + c['client']) if c['role'] else ''

        # ── 2. Promised follow-up: most recent event is a promise-tag, and
        #     nothing has happened since, for longer than promise_hours ──
        is_promise = False
        if ev and ev['event_type'] == 'tag' and any(pt in (ev['detail'] or '') for pt in PROMISE_TAGS):
            hrs_since = (now - last_activity).total_seconds() / 3600
            if hrs_since >= promise_hours:
                matched_tag = next((pt for pt in PROMISE_TAGS if pt in ev['detail']), '')
                tasks.append({
                    'id': 'promise-' + str(c['id']), 'type': 'promise', 'ref_id': c['id'],
                    'candidate_id': c['id'], 'mandate_id': c['mandate_id'],
                    'title': c['name'] or 'Candidate',
                    'subtitle': 'Tagged "' + matched_tag + '" \u2014 no follow-up yet' + (' \u00b7 ' + mandate_label if mandate_label else ''),
                    'phone': c['phone'] or '',
                    'due_at': last_activity.isoformat(), 'section': 'today',
                })
                is_promise = True

        # ── 3. Stale candidate: no activity at all for stale_days ────────
        if not is_promise:
            days_since = (now - last_activity).total_seconds() / 86400
            if days_since >= stale_days:
                tasks.append({
                    'id': 'stale-' + str(c['id']), 'type': 'stale', 'ref_id': c['id'],
                    'candidate_id': c['id'], 'mandate_id': c['mandate_id'],
                    'title': c['name'] or 'Candidate',
                    'subtitle': 'No activity in ' + str(int(days_since)) + ' days' + (' \u00b7 ' + mandate_label if mandate_label else ''),
                    'phone': c['phone'] or '',
                    'due_at': last_activity.isoformat(), 'section': 'today',
                })

    # ── Interview follow-ups: day-of confirmation + next-day result chase ──
    iv_rows = conn.execute(
        "SELECT i.*, c.name, c.phone, m.role, m.client FROM interviews i "
        "LEFT JOIN candidates c ON c.id=i.candidate_id "
        "LEFT JOIN mandates m ON m.id=i.mandate_id "
        "WHERE i.owner_id=? AND i.status='scheduled'", (uid,)
    ).fetchall()
    for iv in iv_rows:
        snz = iv['task_snoozed_until']
        if snz:
            try:
                if datetime.datetime.fromisoformat(snz) > now: continue
            except Exception: pass
        try:
            sch = datetime.datetime.fromisoformat(iv['scheduled_at'])
        except Exception:
            continue
        mandate_label = (iv['role'] + ' \u2014 ' + iv['client']) if iv['role'] else ''
        nice = sch.strftime('%d %b, %I:%M %p')
        if sch.date() == today:
            # Interview is today — confirm candidate will attend
            tasks.append({
                'id': 'iv-day-' + str(iv['id']), 'type': 'interview', 'ref_id': iv['id'],
                'candidate_id': iv['candidate_id'], 'mandate_id': iv['mandate_id'],
                'title': iv['name'] or 'Candidate',
                'subtitle': iv['round_name'] + ' today at ' + nice + ' \u2014 confirm attendance' + (' \u00b7 ' + mandate_label if mandate_label else ''),
                'phone': iv['phone'] or '',
                'due_at': iv['scheduled_at'], 'section': 'today',
            })
        elif sch.date() < today:
            # Interview date passed, still 'scheduled' — chase the result from client
            tasks.append({
                'id': 'iv-result-' + str(iv['id']), 'type': 'interview', 'ref_id': iv['id'],
                'candidate_id': iv['candidate_id'], 'mandate_id': iv['mandate_id'],
                'title': iv['name'] or 'Candidate',
                'subtitle': iv['round_name'] + ' done (' + nice + ') \u2014 get result/feedback from client' + (' \u00b7 ' + mandate_label if mandate_label else ''),
                'phone': iv['phone'] or '',
                'due_at': iv['scheduled_at'], 'section': 'overdue',
            })
        elif sch.date() == today + datetime.timedelta(days=1):
            tasks.append({
                'id': 'iv-tom-' + str(iv['id']), 'type': 'interview', 'ref_id': iv['id'],
                'candidate_id': iv['candidate_id'], 'mandate_id': iv['mandate_id'],
                'title': iv['name'] or 'Candidate',
                'subtitle': iv['round_name'] + ' tomorrow at ' + nice + (' \u00b7 ' + mandate_label if mandate_label else ''),
                'phone': iv['phone'] or '',
                'due_at': iv['scheduled_at'], 'section': 'tomorrow',
            })

    # ── 4. Candidates who submitted an updated profile via self-update link ──
    upd_rows = conn.execute(
        "SELECT c.id, c.name, c.phone, c.mandate_id, c.update_submitted_at, "
        "m.role, m.client FROM candidates c LEFT JOIN mandates m ON m.id=c.mandate_id "
        "WHERE c.owner_id=? AND c.update_submitted_at!='' "
        "AND (c.task_snoozed_until IS NULL OR c.task_snoozed_until='' OR c.task_snoozed_until<?)",
        (uid, now.isoformat())
    ).fetchall()
    for c in upd_rows:
        mandate_label = (c['role'] + ' \u2014 ' + c['client']) if c['role'] else ''
        tasks.append({
            'id': 'updated-' + str(c['id']), 'type': 'updated', 'ref_id': c['id'],
            'candidate_id': c['id'], 'mandate_id': c['mandate_id'],
            'title': c['name'] or 'Candidate',
            'subtitle': 'Submitted updated profile \u2014 review now' + (' \u00b7 ' + mandate_label if mandate_label else ''),
            'phone': c['phone'] or '',
            'due_at': c['update_submitted_at'], 'section': 'overdue',
        })

    # ── 5. New submissions (not yet reviewed, not snoozed) ────────────────
    sub_rows = conn.execute(
        "SELECT * FROM submissions WHERE status='new' "
        "AND (task_snoozed_until IS NULL OR task_snoozed_until='' OR task_snoozed_until<?) "
        "ORDER BY created_at DESC", (now.isoformat(),)
    ).fetchall()
    for s in sub_rows:
        tasks.append({
            'id': 'submission-' + str(s['id']), 'type': 'submission', 'ref_id': s['id'],
            'candidate_id': None, 'mandate_id': None,
            'title': s['name'] or 'New applicant',
            'subtitle': (s['company'] or '') + (' \u00b7 ' + str(s['experience']) + 'y' if s['experience'] else ''),
            'due_at': s['created_at'], 'section': 'today',
        })

    conn.close()

    order = {'overdue': 0, 'today': 1, 'tomorrow': 2, 'upcoming': 3}
    tasks.sort(key=lambda t: (order.get(t['section'], 9), t['due_at']))
    counts = {'overdue': 0, 'today': 0, 'tomorrow': 0, 'upcoming': 0}
    for t in tasks:
        counts[t['section']] = counts.get(t['section'], 0) + 1
    counts['total'] = len(tasks)
    return jsonify({'ok': True, 'tasks': tasks, 'counts': counts})


@app.route('/api/tasks/snooze', methods=['POST'])
@login_required
def snooze_task():
    d = request.json or {}
    ttype = d.get('type')
    ref_id = d.get('ref_id')
    snoozed_until = (d.get('snoozed_until') or '').strip()
    if not ttype or not ref_id or not snoozed_until:
        return jsonify({'error': 'type, ref_id and snoozed_until required'}), 400
    conn = get_db()
    if ttype == 'reminder':
        conn.execute('UPDATE reminders SET due_at=? WHERE id=?', (snoozed_until, ref_id))
    elif ttype in ('stale', 'promise'):
        conn.execute('UPDATE candidates SET task_snoozed_until=? WHERE id=?', (snoozed_until, ref_id))
    elif ttype == 'interview':
        conn.execute('UPDATE interviews SET task_snoozed_until=? WHERE id=?', (snoozed_until, ref_id))
    elif ttype == 'submission':
        conn.execute('UPDATE submissions SET task_snoozed_until=? WHERE id=?', (snoozed_until, ref_id))
    else:
        conn.close(); return jsonify({'error': 'Unknown task type'}), 400
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/tasks/done', methods=['POST'])
@login_required
def task_done():
    d = request.json or {}
    ttype = d.get('type')
    ref_id = d.get('ref_id')
    if not ttype or not ref_id:
        return jsonify({'error': 'type and ref_id required'}), 400
    conn = get_db()
    if ttype == 'reminder':
        conn.execute('UPDATE reminders SET done=1 WHERE id=?', (ref_id,))
    elif ttype in ('stale', 'promise'):
        # Push the suppression window out by the relevant threshold so it
        # naturally resurfaces later if still untouched, rather than being
        # silenced forever.
        days = float(get_setting('stale_days', '7') or 7) if ttype == 'stale' else 1
        push_to = (_ist_now() + datetime.timedelta(days=days)).isoformat()
        conn.execute('UPDATE candidates SET task_snoozed_until=? WHERE id=?', (push_to, ref_id))
    elif ttype == 'updated':
        conn.execute("UPDATE candidates SET update_submitted_at='' WHERE id=?", (ref_id,))
    elif ttype == 'interview':
        conn.execute("UPDATE interviews SET status='completed' WHERE id=?", (ref_id,))
    elif ttype == 'submission':
        conn.execute("UPDATE submissions SET status='reviewed' WHERE id=?", (ref_id,))
    else:
        conn.close(); return jsonify({'error': 'Unknown task type'}), 400
    conn.commit(); conn.close()
    return jsonify({'ok': True})


def _save_wh_for(conn, cid, items):
    """Replace work_history for a candidate from extension data."""
    if not isinstance(items, list) or not items:
        return
    conn.execute('DELETE FROM work_history WHERE candidate_id=?', (cid,))
    for i, it in enumerate(items):
        conn.execute(
            'INSERT INTO work_history (candidate_id,company,designation,start_date,end_date,is_current,description,sort_order) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (cid, (it.get('company') or '').strip(), (it.get('designation') or '').strip(),
             (it.get('start_date') or '').strip(), (it.get('end_date') or '').strip(),
             1 if it.get('is_current') else 0, (it.get('description') or '').strip(), i)
        )

@app.route('/api/extension/push', methods=['POST', 'OPTIONS'])
def extension_push():
    """Receive a candidate pushed from the Naukri Chrome extension.
    - Requires phone OR email present (locked profiles without contact are rejected by the extension).
    - If a candidate with the same phone exists in the SAME mandate -> UPDATE it.
    - Otherwise INSERT a new candidate into the chosen mandate at 'Screening' stage.
    """
    if request.method == 'OPTIONS':
        return ('', 204)

    if not session.get('user_id'):
        return jsonify({'error': 'auth_required', 'message': 'Please log into HireLab in this browser first.'}), 401

    d = request.json or {}
    mid = d.get('mandate_id')
    name = (d.get('name') or '').strip()
    phone = (d.get('phone') or '').strip()
    email = (d.get('email') or '').strip()

    if not mid:
        return jsonify({'error': 'Please select a mandate'}), 400

    # Verify the mandate belongs to the current (effective) user.
    # Freelancers are allowed if the mandate is ASSIGNED to them.
    _conn = get_db()
    _own = _conn.execute('SELECT owner_id FROM mandates WHERE id=?', (mid,)).fetchone()
    _is_freelancer_upload = False
    _cu = current_user()
    if _cu and _cu.get('role') == 'freelancer_sourcer':
        _is_freelancer_upload = True
        try:
            from modules.freelancer import freelancer_can_access_mandate
            ok_access = freelancer_can_access_mandate(_conn, real_user_id(), int(mid), effective_company_id())
        except Exception:
            ok_access = False
        _conn.close()
        if not ok_access:
            return jsonify({'error': 'This mandate is not assigned to you'}), 403
    else:
        _conn.close()
        if not _own or _own['owner_id'] != effective_user_id():
            return jsonify({'error': 'That mandate is not in your workspace'}), 403
    if not name:
        return jsonify({'error': 'Candidate name missing'}), 400
    if not phone and not email:
        return jsonify({'error': 'Profile appears locked (no phone/email). Unlock it on Naukri first.'}), 400

    def fnum(v):
        try: return float(v or 0)
        except: return 0.0
    def inum(v):
        try: return int(float(v or 0))
        except: return 0

    skills = d.get('key_skills') or []
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(',') if s.strip()]
    skills_json = json.dumps(skills)

    conn = get_db(); c = conn.cursor()

    # ── Duplicate detection by EMAIL ──────────────────────────────────────
    # Same email in the SAME mandate  -> UPDATE the CV/profile fields, but
    #   NEVER touch the stage, journey (stage_history) or comments.
    # Same email in a DIFFERENT mandate -> still create a NEW entry here
    #   (each mandate has its own pipeline); we just tell the user it exists
    #   elsewhere so they have context.
    # Duplicate check. For FREELANCERS this is a hard block (per spec):
    # if the candidate already exists on this mandate (phone OR email match),
    # reject the upload entirely.
    if _is_freelancer_upload:
        import re as _re
        pd = _re.sub(r'[^0-9]', '', phone or '')
        dup = None
        if pd and len(pd) >= 10:
            dup = c.execute(
                "SELECT id, name FROM candidates WHERE mandate_id=? AND "
                "REPLACE(REPLACE(REPLACE(phone,' ',''),'-',''),'+','') LIKE ?",
                (mid, '%' + pd[-10:])).fetchone()
        if not dup and email:
            dup = c.execute('SELECT id, name FROM candidates WHERE mandate_id=? AND LOWER(email)=LOWER(?)',
                            (mid, email)).fetchone()
        if not dup and name:
            dup = c.execute('SELECT id, name FROM candidates WHERE mandate_id=? AND LOWER(name)=LOWER(?)',
                            (mid, name)).fetchone()
        if dup:
            conn.close()
            return jsonify({'error': 'duplicate',
                            'message': 'This candidate is already sourced on this mandate.',
                            'existing_name': dup['name']}), 409

    existing = None
    other_mandates = []
    if email:
        existing = c.execute(
            'SELECT * FROM candidates WHERE mandate_id=? AND LOWER(email)=LOWER(?) LIMIT 1',
            (mid, email)
        ).fetchone()
        # Find this person in OTHER mandates owned by the same user (for info)
        rows = c.execute(
            'SELECT c.id, m.role, m.client FROM candidates c '
            'JOIN mandates m ON m.id = c.mandate_id '
            'WHERE LOWER(c.email)=LOWER(?) AND c.mandate_id!=? AND m.owner_id=?',
            (email, mid, effective_user_id())
        ).fetchall()
        other_mandates = [ (r['role'] + ' @ ' + r['client']) for r in rows ]

    if existing:
        # UPDATE profile/CV fields ONLY. Do NOT modify stage, stage_history,
        # recruiter_feedback, client_feedback, general_comments, wa_response.
        c.execute(
            'UPDATE candidates SET name=?,company=?,designation=?,experience=?,ctc_current=?,'
            'ctc_expected=?,notice_period=?,location=?,phone=?,key_skills=?,updated_at=? WHERE id=?',
            (name, d.get('company',''), d.get('designation',''), fnum(d.get('experience')),
             fnum(d.get('ctc_current')), fnum(d.get('ctc_expected')), inum(d.get('notice_period')),
             d.get('location',''), phone or existing['phone'], skills_json, ts(), existing['id'])
        )
        _save_wh_for(conn, existing['id'], d.get('work_history'))
        conn.execute('UPDATE candidates SET qualification=?, preferred_location=?, linkedin_url=?, ai_insight_cv=? WHERE id=?',
                     (d.get('qualification',''), d.get('preferred_location',''),
                      d.get('linkedin_url',''), d.get('ai_insight_cv',''), existing['id']))
        conn.commit(); conn.close()
        return jsonify({'ok': True, 'action': 'updated', 'candidate_id': existing['id'],
                        'name': name, 'preserved': True,
                        'message': 'CV & details updated. Stage, journey and comments preserved.'})

    c.execute(
        'INSERT INTO candidates (mandate_id,name,company,designation,experience,ctc_current,'
        'ctc_expected,notice_period,location,phone,email,career_summary,key_skills,'
        'screening_decision,ai_reasoning,stage,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (mid, name, d.get('company',''), d.get('designation',''), fnum(d.get('experience')),
         fnum(d.get('ctc_current')), fnum(d.get('ctc_expected')), inum(d.get('notice_period')),
         d.get('location',''), phone, email, d.get('career_summary',''), skills_json,
         'worth_opening', 'Pushed from Naukri', 'Screening', ts(), ts())
    )
    cid = c.lastrowid
    c.execute('UPDATE candidates SET qualification=?, preferred_location=?, linkedin_url=?, ai_insight_cv=? WHERE id=?',
              (d.get('qualification',''), d.get('preferred_location',''),
               d.get('linkedin_url',''), d.get('ai_insight_cv',''), cid))
    # If a freelancer sourced this candidate, stamp attribution
    if _is_freelancer_upload:
        c.execute('UPDATE candidates SET sourced_by=?, sourced_at=? WHERE id=?',
                  (real_user_id(), ts(), cid))
    c.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
              (cid, '', 'Screening', 'Pushed from Naukri extension', ts()))
    _save_wh_for(conn, cid, d.get('work_history'))
    conn.commit(); conn.close()
    queue_embedding_job(cid)  # async: enqueue, background worker embeds (never blocks add)
    resp = {'ok': True, 'action': 'added', 'candidate_id': cid, 'name': name}
    if other_mandates:
        resp['also_in'] = other_mandates
        resp['message'] = 'Added here. This person also exists in: ' + ', '.join(other_mandates)
    return jsonify(resp)

@app.route('/api/extension/mandates', methods=['GET', 'OPTIONS'])
def extension_mandates():
    """Lightweight mandate list for the extension dropdown (active only).
    Login-aware: shows only the logged-in user's own active mandates."""
    if request.method == 'OPTIONS':
        return ('', 204)
    if not session.get('user_id'):
        return jsonify({'error': 'auth_required', 'message': 'Please log into HireLab in this browser first.'}), 401
    conn = get_db()
    # Freelancers only see mandates ASSIGNED to them; recruiters/admin see their own.
    cu = current_user()
    if cu and cu.get('role') == 'freelancer_sourcer':
        rows = conn.execute(
            "SELECT m.id, m.role, m.client, m.location FROM mandate_freelancers mf "
            "JOIN mandates m ON m.id=mf.mandate_id "
            "WHERE mf.freelancer_user_id=? AND mf.company_id=? AND mf.is_active=1 "
            "AND m.status='active' ORDER BY m.created_at DESC",
            (real_user_id(), effective_company_id())
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, role, client, location FROM mandates WHERE status='active' AND owner_id=? ORDER BY created_at DESC",
            (effective_user_id(),)
        ).fetchall()
    conn.close()
    return jsonify({'ok': True, 'mandates': [dict(r) for r in rows]})

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AI INSIGHTS — Semantic Search (embeddings) + Stats (SQL + LLM summary)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
import math

GEMINI_EMBED_URL = ('https://generativelanguage.googleapis.com/v1beta/'
                    'models/gemini-embedding-001:embedContent')

# Settings that are PER-COMPANY (each tenant has their own). Everything else
# (billing config, pricing, central ids, API keys) stays global.
TENANT_SETTINGS = {
    'recruiter_name', 'company_name',
    'template_msg1', 'template_fu1', 'template_fu2',
    'fu1_hours', 'fu2_hours',
    'workflow_mode',   # 'agency' (default) or 'corporate'
    'smtp_email', 'smtp_app_password', 'smtp_display_name',
    'imap_enabled', 'imap_last_uid',
    'email_templates',  # JSON array of {name, subject, body}
    'custom_status_tags',  # JSON array of user-created quick tags
    'stale_days', 'promise_hours',  # follow-up task detection thresholds
    'analytics_stale_days',  # dashboard stale-mandate threshold (separate)
    'bd_stale_days',  # BD command center: flag clients silent this many days
    'interview_template',  # default interview communication message
}

def _safe_company_id():
    try:
        return effective_company_id() or 0
    except Exception:
        return 0

def get_setting(key, default=''):
    # Env var takes priority for sensitive keys (see _ENV_KEY_MAP)
    env_name = _ENV_KEY_MAP.get(key)
    if env_name:
        env_val = os.environ.get(env_name, '').strip()
        if env_val:
            return env_val
    conn = get_db()
    # Per-tenant keys: prefer this company's own value, else fall back to the
    # global row (which acts as the default seed).
    if key in TENANT_SETTINGS:
        cid = _safe_company_id()
        if cid:
            tr = conn.execute('SELECT value FROM tenant_settings WHERE company_id=? AND key=?',
                              (cid, key)).fetchone()
            if tr is not None:
                conn.close()
                return (tr['value'] or '') or default
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    conn.close()
    return (row['value'] if row else '') or default

def set_setting(key, value):
    """Write a setting. Per-tenant keys go to this company's own row; global
    keys go to the shared settings table."""
    conn = get_db()
    if key in TENANT_SETTINGS:
        cid = _safe_company_id()
        if cid:
            conn.execute('INSERT OR REPLACE INTO tenant_settings (company_id,key,value) VALUES (?,?,?)',
                         (cid, key, str(value)))
            conn.commit(); conn.close()
            return
    conn.execute('INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)', (key, str(value)))
    conn.commit(); conn.close()

def gemini_embed(text, api_key):
    """Return a list[float] embedding for the given text via Gemini, or None."""
    text = (text or '').strip()
    if not text:
        return None
    try:
        resp = requests.post(
            GEMINI_EMBED_URL + '?key=' + api_key,
            headers={'Content-Type': 'application/json'},
            json={'model': 'models/gemini-embedding-001',
                  'content': {'parts': [{'text': text[:8000]}]}},
            timeout=30)
        if resp.status_code != 200:
            return {'error': resp.json().get('error', {}).get('message', resp.text[:200])}
        return resp.json()['embedding']['values']
    except Exception as e:
        return {'error': str(e)}

def _row_get(c, key, default=''):
    """Safely read a column from a sqlite3.Row. Older/partial rows may lack a
    column added by a later migration; this returns `default` instead of
    raising KeyError, so embedding text never crashes on legacy data."""
    try:
        if key in c.keys():
            v = c[key]
            return default if v is None else v
    except Exception:
        pass
    return default


def _as_list_str(raw):
    """Normalise a field that may be a JSON list, a comma string, or plain text
    into a clean 'a, b, c' string. Empty/blank items are dropped."""
    if raw is None:
        return ''
    if isinstance(raw, list):
        items = raw
    else:
        s = str(raw).strip()
        if not s:
            return ''
        if s.startswith('['):
            try:
                items = json.loads(s)
            except Exception:
                items = [s]
        else:
            items = [p for p in s.replace(';', ',').split(',')]
    seen, out = set(), []
    for it in items:
        t = str(it).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return ', '.join(out)


# Bumped whenever candidate_embed_text() changes shape, so we can tell which
# vectors were built with the old text vs the new structured text and reindex
# selectively.
#   v1 = legacy 6-field concat
#   v2 = structured labelled profile + Career History + full CV resume text
EMBED_TEXT_VERSION = 2

# ── Embedding pipeline metadata (Feature 2) ─────────────────────────────
# Stamped onto every vector so we can migrate embedding models in future
# without breaking existing vectors. Nothing here is hardcoded into search;
# it is descriptive metadata only.
EMBEDDING_MODEL    = 'gemini-embedding-001'          # the model gemini_embed() calls
EMBEDDING_VERSION  = 'v1'                             # our embedding-pipeline version
EMBED_TEXT_TEMPLATE = f'candidate-template-v{EMBED_TEXT_VERSION}'  # which text builder produced it

import array as _array
def _vec_to_blob(vec):
    """Pack a float vector into compact float32 bytes for the embedding_vec
    cache. ~5.6x smaller than the JSON form and ~1000x faster to read back."""
    try:
        return _array.array('f', vec).tobytes()
    except Exception:
        return None

# How much raw resume text to fold into the embedding. The structured labelled
# fields (high signal) always come first, so if the total exceeds the Gemini
# input cap it is the resume TAIL that gets clipped, never the structured data.
CV_EMBED_MAX_CHARS = 5000


def _candidate_cv_text(c, max_chars=CV_EMBED_MAX_CHARS):
    """Return plain text extracted from the candidate's stored CV file (Word or
    PDF), or '' if there is no file / it can't be read. Reuses the existing
    extract_text_from_file() so there is ONE resume-parsing code path.

    This is what lets the embedding capture detail the parsed columns miss —
    projects, tools, software, certifications, languages — straight from the
    resume the recruiter uploaded. Best-effort: never raises."""
    try:
        rel = str(_row_get(c, 'cv_path')).strip()
        if not rel:
            return ''
        fp = os.path.join(CV_DIR, rel)
        if not os.path.exists(fp):
            return ''
        # Prefer the original filename's extension (tells us pdf vs docx);
        # fall back to the stored name.
        name = str(_row_get(c, 'cv_original_name')).strip() or rel
        with open(fp, 'rb') as fh:
            data = fh.read()
        text, err = extract_text_from_file(data, name)
        if err or not text:
            return ''
        text = ' '.join(text.split())  # collapse whitespace/newlines
        return text[:max_chars]
    except Exception:
        return ''


def candidate_embed_text(c, conn=None):
    """Build the text blob we embed for a candidate.

    Mandate-agnostic on purpose so a candidate can surface for ANY role they
    fit. The output is a *labelled, sectioned* profile rather than a blind
    concatenation: clear field names ("Current Role:", "Domain Expertise:")
    give the embedding model structure to latch onto, which measurably improves
    semantic match quality over a bare bag-of-words.

    `conn` is optional and backward compatible: when supplied, previous
    employers are pulled from the work_history table to capture career depth.
    Existing callers that pass only `c` keep working unchanged.
    """
    lines = []

    def add(label, value):
        v = (value or '').strip() if isinstance(value, str) else value
        if v:
            lines.append(f'{label}: {v}')

    name = str(_row_get(c, 'name')).strip()
    if name:
        lines.append(name)

    # ── Current position ────────────────────────────────────────────────
    desig = str(_row_get(c, 'designation')).strip()
    company = str(_row_get(c, 'company')).strip()
    if desig and company:
        add('Current Role', f'{desig} at {company}')
    else:
        add('Current Designation', desig)
        add('Current Company', company)

    exp = _row_get(c, 'experience', 0)
    try:
        if float(exp) > 0:
            add('Total Experience', f'{exp} years')
    except Exception:
        pass

    add('Career Summary', str(_row_get(c, 'career_summary')))

    # ── Previous companies (career depth) ───────────────────────────────
    if conn is not None:
        try:
            wh = conn.execute(
                'SELECT company, designation FROM work_history '
                'WHERE candidate_id=? ORDER BY is_current DESC, sort_order ASC, id ASC',
                (_row_get(c, 'id', 0),)).fetchall()
            prev = []
            for w in wh:
                co = (w['company'] or '').strip()
                dg = (w['designation'] or '').strip()
                if co and dg:
                    prev.append(f'{dg} at {co}')
                elif co or dg:
                    prev.append(co or dg)
            if prev:
                add('Career History', '; '.join(prev[:8]))
        except Exception:
            pass

    # ── Skills, domain, products, functional expertise ──────────────────
    add('Key Skills',       _as_list_str(_row_get(c, 'key_skills')))
    add('Technical Skills', _as_list_str(_row_get(c, 'key_skill_tags')))
    add('Secondary Skills', _as_list_str(_row_get(c, 'secondary_skills')))
    add('Domain Expertise', _as_list_str(_row_get(c, 'domain_tags')))
    add('Industry',         _as_list_str(_row_get(c, 'industry_background')))
    add('Products',         _as_list_str(_row_get(c, 'product_handles')))
    add('Functional Expertise', _as_list_str(_row_get(c, 'function_tags')))

    # ── Education & location ────────────────────────────────────────────
    add('Education', str(_row_get(c, 'qualification')))
    loc = str(_row_get(c, 'location')).strip()
    pref = str(_row_get(c, 'preferred_location')).strip()
    if loc and pref and pref.lower() != loc.lower():
        add('Location', f'{loc} (open to {pref})')
    else:
        add('Location', loc or pref)

    # ── Full resume text (projects / tools / certs / languages the parsed
    #    columns don't capture). Appended LAST so structured fields survive
    #    any truncation at the embedding input cap. ──────────────────────
    cv_text = _candidate_cv_text(c)
    if cv_text:
        lines.append('Resume:\n' + cv_text)

    return '\n'.join(lines)

def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@app.route('/api/candidates/<int:cid>/rate', methods=['POST'])
def rate_candidate(cid):
    """Rate a candidate against their mandate's JD using DeepSeek.
    Returns AI suitability % + selection probability % + reasoning."""
    ds_key = get_setting('deepseek_api_key')
    if not ds_key:
        return jsonify({'error': 'DeepSeek API key not set. Add it in Settings.'}), 400

    conn = get_db()
    c = conn.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
    if not c:
        conn.close()
        return jsonify({'error': 'Candidate not found'}), 404
    m = conn.execute('SELECT * FROM mandates WHERE id=?', (c['mandate_id'],)).fetchone()
    if not m:
        conn.close()
        return jsonify({'error': 'Mandate not found'}), 404

    try:
        skills = json.loads(c['key_skills'] or '[]')
        if isinstance(skills, list): skills = ', '.join(str(s) for s in skills)
    except Exception:
        skills = ''

    jd_text = html_to_text(m['jd']) if m['jd'] else ''
    role_ctx = (f"Role: {m['role']} at {m['client']}\n"
                f"Location: {m['location']}\n"
                f"CTC band: {m['ctc_min']}-{m['ctc_max']} LPA\n"
                + (f"Job Description:\n{jd_text}" if jd_text.strip() else "Job Description: (not provided)"))

    # Pull hidden client notes for this mandate (recruiter's private intel from client)
    client_notes = ''
    try:
        note_rows = conn.execute(
            'SELECT note, created_at FROM mandate_client_notes WHERE mandate_id=? AND is_active=1 '
            'ORDER BY created_at ASC', (c['mandate_id'],)).fetchall()
        if note_rows:
            client_notes = '\n'.join('- ' + (r['note'] or '') for r in note_rows if (r['note'] or '').strip())
    except Exception:
        client_notes = ''
    if client_notes.strip():
        role_ctx += ("\n\nIMPORTANT — Private client requirements & preferences "
                     "(shared confidentially by the client; weigh these heavily):\n" + client_notes)

    cand_ctx = (f"Name: {c['name']}\n"
                f"Current: {c['designation']} at {c['company']}\n"
                f"Experience: {c['experience']} years\n"
                f"Location: {c['location']}\n"
                f"Current CTC: {c['ctc_current']} LPA, Expected: {c['ctc_expected']} LPA\n"
                f"Notice period: {c['notice_period']} days\n"
                f"Skills: {skills}\n"
                f"Summary: {c['career_summary'] or ''}")

    prompt = ("You are an expert recruiter evaluating how well a candidate fits a role. "
              "Score strictly and realistically.\n\n"
              "=== ROLE ===\n" + role_ctx + "\n\n=== CANDIDATE ===\n" + cand_ctx + "\n\n"
              "Return ONLY a JSON object (no markdown, no extra text) with exactly these keys:\n"
              '{"suitability": <0-100 integer: how well candidate matches the role requirements>, '
              '"selection_probability": <0-100 integer: realistic chance of being shortlisted by the client>, '
              '"reasoning": "<2-3 concise sentences: key strengths and gaps for THIS role>"}')

    try:
        rr = call_deepseek(ds_key,
            {'model': 'deepseek-chat', 'temperature': 0.2, 'max_tokens': 400,
                  'messages': [{'role': 'user', 'content': prompt}]},
            timeout=60, endpoint='reasoning')
        if rr.status_code != 200:
            err = rr.json().get('error', {}).get('message', rr.text[:200])
            conn.close()
            return jsonify({'error': 'DeepSeek error: ' + err}), 500
        raw = rr.json()['choices'][0]['message']['content'].strip()
        # Strip code fences if present
        raw = re.sub(r'^```(json)?|```$', '', raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
    except json.JSONDecodeError:
        conn.close()
        return jsonify({'error': 'Could not parse AI response. Try again.'}), 500
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500

    suit = max(0, min(100, int(data.get('suitability', 0))))
    prob = max(0, min(100, int(data.get('selection_probability', 0))))
    reasoning = '[Rated vs JD] ' + (data.get('reasoning', '') or '')

    conn.execute('UPDATE candidates SET ai_score=?, ai_reasoning=?, updated_at=? WHERE id=?',
                 (suit, reasoning, ts(), cid))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'suitability': suit, 'selection_probability': prob,
                    'reasoning': data.get('reasoning', '')})

def _record_embedding(conn, cid, status, *, vec=None, txt=None, error='', duration_ms=0):
    """Single authoritative writer for a candidate's embedding + all metadata.
    Centralised so every code path (single add, batch reindex, future model
    migration) stamps identical metadata. Commits itself.

    status:
      'completed' -> store the vector, text and full metadata
      'missing'   -> no embeddable text; mark embedding='[]' so it is not
                     re-selected as pending, but flag it missing
      'failed'    -> Gemini/API error; leave embedding UNTOUCHED (stays '' so
                     the existing pending query retries it) and record the error
    """
    dim = len(vec) if isinstance(vec, list) else 0
    if status == 'completed' and isinstance(vec, list) and vec:
        conn.execute(
            'UPDATE candidates SET embedding=?, embedding_vec=?, embedding_text=?, embedded_at=?, '
            'embedding_model=?, embedding_version=?, embedding_dimension=?, '
            'embedding_status=?, embedding_error=?, embedding_duration_ms=?, '
            'embedding_text_version=? WHERE id=?',
            (json.dumps(vec), _vec_to_blob(vec), txt, ts(), EMBEDDING_MODEL, EMBEDDING_VERSION, dim,
             'completed', '', int(duration_ms or 0), EMBED_TEXT_TEMPLATE, cid))
    elif status == 'missing':
        conn.execute(
            "UPDATE candidates SET embedding='[]', embedded_at=?, embedding_dimension=0, "
            "embedding_model=?, embedding_version=?, embedding_status='missing', "
            "embedding_error=?, embedding_duration_ms=?, embedding_text_version=? WHERE id=?",
            (ts(), EMBEDDING_MODEL, EMBEDDING_VERSION, (error or 'no embeddable text')[:500],
             int(duration_ms or 0), EMBED_TEXT_TEMPLATE, cid))
    else:  # 'failed' — DO NOT touch the embedding column, so it stays retryable
        conn.execute(
            "UPDATE candidates SET embedding_status='failed', embedding_error=?, "
            "embedding_duration_ms=?, embedding_model=?, embedding_version=?, "
            "embedding_text_version=? WHERE id=?",
            ((error or 'unknown error')[:500], int(duration_ms or 0), EMBEDDING_MODEL,
             EMBEDDING_VERSION, EMBED_TEXT_TEMPLATE, cid))
    conn.commit()


def embed_candidate_row(conn, c, api_key):
    """Build the embed text for candidate row `c`, call Gemini (timed), and
    store the vector + metadata via _record_embedding. Returns a small status
    dict. Never raises. This is the ONE embedding code path used by both the
    single-add flow and the batch reindex."""
    import time as _time
    cid = c['id']
    txt = candidate_embed_text(c, conn)
    if not txt.strip():
        _record_embedding(conn, cid, 'missing', error='no embeddable text')
        print(f'[embed] cid={cid} missing (no embeddable text)')
        return {'status': 'missing', 'cid': cid}

    print(f'[embed] cid={cid} started model={EMBEDDING_MODEL} chars={len(txt)}')
    t0 = _time.perf_counter()
    vec = gemini_embed(txt, api_key)
    dur = int((_time.perf_counter() - t0) * 1000)

    if isinstance(vec, dict) and vec.get('error'):
        _record_embedding(conn, cid, 'failed', txt=txt, error=vec['error'], duration_ms=dur)
        print(f'[embed] cid={cid} FAILED in {dur}ms: {str(vec["error"])[:140]}')
        return {'status': 'failed', 'cid': cid, 'error': vec['error'], 'duration_ms': dur}
    if not vec:
        _record_embedding(conn, cid, 'failed', txt=txt, error='empty vector', duration_ms=dur)
        print(f'[embed] cid={cid} FAILED in {dur}ms: empty vector')
        return {'status': 'failed', 'cid': cid, 'error': 'empty vector', 'duration_ms': dur}

    _record_embedding(conn, cid, 'completed', vec=vec, txt=txt, duration_ms=dur)
    print(f'[embed] cid={cid} completed dim={len(vec)} in {dur}ms model={EMBEDDING_MODEL}')
    return {'status': 'completed', 'cid': cid, 'dimension': len(vec), 'duration_ms': dur}


# ══════════════════════════════════════════════════════════════════════
#  ASYNC EMBEDDING QUEUE (Sprint 3)
#  A lightweight, persistent job queue on top of the existing SQLite +
#  daemon-thread architecture (same pattern as the reminder scheduler).
#  Goals: candidate creation never waits on Gemini; failed embeds retry
#  with exponential backoff; jobs survive restarts; API bursts are throttled.
# ══════════════════════════════════════════════════════════════════════

# Tunables — all overridable at runtime via Settings (get_setting), so you can
# adjust throughput/retries without a redeploy. Defaults are safe for Render.
_EMBED_CFG_DEFAULTS = {
    'embed_max_retries':     5,     # attempts before a job is marked failed
    'embed_batch_per_cycle': 10,    # jobs processed per worker wake (throughput)
    'embed_interval_ms':     1200,  # gap between Gemini calls (anti-burst / rate limit)
    'embed_poll_sec':        5,     # how often the worker wakes to look for jobs
    'embed_backoff_base_sec': 10,   # backoff = base * 2^(retry-1)
    'embed_backoff_cap_sec': 600,   # backoff never exceeds this
    'embed_reconcile_cap':   200,   # max un-embedded candidates auto-queued per sweep
}

def _embed_cfg(key):
    """Read a queue tunable from Settings, falling back to the default. Values
    are coerced to the default's type so callers always get an int."""
    default = _EMBED_CFG_DEFAULTS[key]
    try:
        v = get_setting(key)
        if v is None or str(v).strip() == '':
            return default
        return type(default)(v)
    except Exception:
        return default


# Error substrings that mean "do NOT retry" — auth/permission/validation are
# permanent; retrying only wastes quota. Everything else (timeout, network,
# rate limit / RESOURCE_EXHAUSTED, 5xx, INTERNAL, DEADLINE) is transient.
_EMBED_PERMANENT_MARKERS = (
    'API KEY', 'API_KEY', 'PERMISSION', 'UNAUTHENTICATED',
    'INVALID_ARGUMENT', 'NOT_FOUND', 'FAILED_PRECONDITION',
)

def _is_retryable_embed_error(err):
    e = str(err or '').upper()
    return not any(m in e for m in _EMBED_PERMANENT_MARKERS)


def _embed_backoff_seconds(retry_count):
    base = _embed_cfg('embed_backoff_base_sec')
    cap = _embed_cfg('embed_backoff_cap_sec')
    return min(cap, base * (2 ** max(0, retry_count - 1)))


def queue_embedding_job(cid, conn=None):
    """Enqueue an embedding job for a candidate and return immediately. This is
    what candidate-creation calls instead of embedding inline — it is a single
    cheap INSERT, so the upload/add response is instant.

    Idempotent: if the candidate already has an ACTIVE job (pending/processing/
    retrying) we don't create a duplicate. `conn` optional (reused if given)."""
    own = False
    try:
        if conn is None:
            conn = get_db(); own = True
        active = conn.execute(
            "SELECT id FROM embedding_jobs WHERE candidate_id=? "
            "AND status IN ('pending','processing','retrying') LIMIT 1", (cid,)).fetchone()
        if active:
            return active['id']
        conn.execute(
            "INSERT INTO embedding_jobs (candidate_id, status, retry_count, max_retries, "
            "created_at, next_attempt_at) VALUES (?, 'pending', 0, ?, ?, ?)",
            (cid, _embed_cfg('embed_max_retries'), ts(), ts()))
        conn.commit()
        jid = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
        print(f'[embed-queue] job {jid} created for cid={cid}')
        return jid
    except Exception as e:
        print(f'[embed-queue] enqueue error cid={cid}: {e}')
        return None
    finally:
        if own and conn is not None:
            try: conn.close()
            except Exception: pass


def _claim_next_job(conn):
    """Atomically claim the next due job. The conditional UPDATE (…WHERE status
    IN pending/retrying) means even if two workers race — or a future multi-
    worker deploy runs several copies — only one claims each job."""
    now = ts()
    row = conn.execute(
        "SELECT id FROM embedding_jobs WHERE status IN ('pending','retrying') "
        "AND (next_attempt_at IS NULL OR next_attempt_at='' OR next_attempt_at<=?) "
        "ORDER BY created_at ASC, id ASC LIMIT 1", (now,)).fetchone()
    if not row:
        return None
    cur = conn.execute(
        "UPDATE embedding_jobs SET status='processing', started_at=? "
        "WHERE id=? AND status IN ('pending','retrying')", (now, row['id']))
    conn.commit()
    if cur.rowcount != 1:
        return None  # lost the race; another claimer got it
    return conn.execute('SELECT * FROM embedding_jobs WHERE id=?', (row['id'],)).fetchone()


def _process_job(conn, job, api_key):
    """Run one claimed job through the shared embed path and update its status.
    Reuses embed_candidate_row (Sprint 2) — there is still ONE embedding code
    path; the queue only decides WHEN it runs and whether to retry."""
    jid = job['id']; cid = job['candidate_id']
    c = conn.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
    if not c:
        conn.execute("UPDATE embedding_jobs SET status='cancelled', last_error='candidate deleted', "
                     "completed_at=? WHERE id=?", (ts(), jid))
        conn.commit()
        print(f'[embed-queue] job {jid} cancelled (cid={cid} gone)')
        return 'cancelled'

    res = embed_candidate_row(conn, c, api_key)   # writes vector + metadata
    st = res.get('status')
    dur = int(res.get('duration_ms', 0) or 0)

    if st in ('completed', 'missing'):
        conn.execute("UPDATE embedding_jobs SET status='completed', duration_ms=?, "
                     "completed_at=?, last_error='' WHERE id=?", (dur, ts(), jid))
        conn.commit()
        return 'completed'

    # failed — decide retry vs give up
    err = res.get('error', 'unknown error')
    if _is_retryable_embed_error(err) and (job['retry_count'] + 1) <= job['max_retries']:
        rc = job['retry_count'] + 1
        nxt = (_ist_now() + datetime.timedelta(seconds=_embed_backoff_seconds(rc))).isoformat(timespec='seconds')
        conn.execute("UPDATE embedding_jobs SET status='retrying', retry_count=?, last_error=?, "
                     "next_attempt_at=?, duration_ms=? WHERE id=?", (rc, str(err)[:500], nxt, dur, jid))
        conn.commit()
        print(f'[embed-queue] job {jid} retry {rc}/{job["max_retries"]} in '
              f'{_embed_backoff_seconds(rc)}s ({str(err)[:80]})')
        return 'retrying'
    else:
        reason = 'permanent' if not _is_retryable_embed_error(err) else 'max retries'
        conn.execute("UPDATE embedding_jobs SET status='failed', last_error=?, completed_at=?, "
                     "duration_ms=? WHERE id=?", (str(err)[:500], ts(), dur, jid))
        conn.commit()
        print(f'[embed-queue] job {jid} FAILED ({reason}): {str(err)[:100]}')
        return 'failed'


def _backfill_embedding_blobs(conn, cap):
    """Convert legacy JSON embeddings into the float32 embedding_vec cache in the
    background (no API calls — pure local). Runs a bounded batch per idle sweep
    so existing candidates gradually gain the fast search path."""
    try:
        rows = conn.execute(
            "SELECT id, embedding FROM candidates "
            "WHERE embedding_vec IS NULL AND embedding IS NOT NULL "
            "AND embedding NOT IN ('', '[]') LIMIT ?", (cap,)).fetchall()
        n = 0
        for r in rows:
            try:
                vec = json.loads(r['embedding'])
            except Exception:
                continue
            blob = _vec_to_blob(vec)
            if blob is not None:
                conn.execute("UPDATE candidates SET embedding_vec=? WHERE id=?", (blob, r['id']))
                n += 1
        if n:
            conn.commit()
            print(f'[embed-blob] backfilled {n} float32 vector-cache row(s)')
        return n
    except Exception as e:
        print(f'[embed-blob] backfill error: {e}')
        return 0


# ══════════════════════════════════════════════════════════════════════
#  MULTI-VECTOR (FACET) EMBEDDINGS (Phase 2 / Sprint 8)
#  Separate vectors for what a candidate can DO (skills), where they've BEEN
#  (experience) and what they've BUILT (projects). Lets a focused query match
#  the right facet instead of a diluted whole-profile vector. Generation is
#  gated OFF by default so no extra Gemini cost until deliberately enabled;
#  search is unchanged unless a request opts in with multivector=true.
# ══════════════════════════════════════════════════════════════════════
MV_FACETS = ('skills', 'experience', 'projects')
MV_TEXT_VERSION = 1
MV_TEXT_TEMPLATE = f'facet-template-v{MV_TEXT_VERSION}'

_MV_CFG_DEFAULTS = {
    'multivector_enabled':   0,      # generate facet vectors in the background?
    'multivector_batch':     20,     # candidates per background sweep
    'multivector_w_full':    0.6,    # blend weight: whole-profile vector
    'multivector_w_facet':   0.4,    # blend weight: best-matching facet
}
def _mv_cfg(key):
    dflt = _MV_CFG_DEFAULTS[key]
    try:
        v = get_setting(key)
        if v is None or str(v).strip() == '':
            return dflt
        return float(v) if isinstance(dflt, float) else int(float(v))
    except Exception:
        return dflt


def candidate_facet_text(c, facet, conn=None):
    """Build the text for one facet. Reuses the same tolerant field helpers as
    the full-profile builder so there is one parsing path. Returns '' when the
    facet has no meaningful content (so we skip embedding empty facets)."""
    if facet == 'skills':
        parts = []
        for label, col in [('Skills', 'key_skills'), ('Technical Skills', 'key_skill_tags'),
                           ('Secondary Skills', 'secondary_skills'), ('Domain Expertise', 'domain_tags'),
                           ('Products', 'product_handles'), ('Functional Expertise', 'function_tags')]:
            v = _as_list_str(_row_get(c, col))
            if v:
                parts.append(f'{label}: {v}')
        desig = str(_row_get(c, 'designation')).strip()
        if desig:
            parts.insert(0, f'Role: {desig}')
        return '\n'.join(parts)

    if facet == 'experience':
        lines = []
        desig = str(_row_get(c, 'designation')).strip()
        company = str(_row_get(c, 'company')).strip()
        if desig or company:
            lines.append(f'Current Role: {desig} at {company}'.strip())
        exp = _row_get(c, 'experience', 0)
        try:
            if float(exp) > 0:
                lines.append(f'Total Experience: {exp} years')
        except Exception:
            pass
        summ = str(_row_get(c, 'career_summary')).strip()
        if summ:
            lines.append(f'Summary: {summ}')
        ind = _as_list_str(_row_get(c, 'industry_background'))
        if ind:
            lines.append(f'Industry: {ind}')
        if conn is not None:
            try:
                wh = conn.execute(
                    'SELECT company, designation FROM work_history WHERE candidate_id=? '
                    'ORDER BY is_current DESC, sort_order ASC, id ASC', (_row_get(c, 'id', 0),)).fetchall()
                prev = [f"{(w['designation'] or '').strip()} at {(w['company'] or '').strip()}".strip(' at')
                        for w in wh if (w['company'] or w['designation'])]
                prev = [p for p in prev if p]
                if prev:
                    lines.append('Career History: ' + '; '.join(prev[:8]))
            except Exception:
                pass
        return '\n'.join(lines)

    if facet == 'projects':
        chunks = []
        summ = str(_row_get(c, 'career_summary')).strip()
        if summ:
            chunks.append(summ)
        if conn is not None:
            try:
                for w in conn.execute('SELECT description FROM work_history WHERE candidate_id=?',
                                      (_row_get(c, 'id', 0),)).fetchall():
                    dsc = (w['description'] or '').strip()
                    if dsc:
                        chunks.append(dsc)
            except Exception:
                pass
        cv = _candidate_cv_text(c, max_chars=3000)
        if cv:
            chunks.append(cv)
        return '\n'.join(chunks).strip()

    return ''


def _store_facet(conn, cid, facet, vec, txt, status, error='', duration_ms=0):
    dim = len(vec) if isinstance(vec, list) else 0
    blob = _vec_to_blob(vec) if (status == 'completed' and vec) else None
    conn.execute(
        "INSERT INTO candidate_vectors (candidate_id, facet, embedding_vec, embedding_text, "
        "embedding_model, embedding_version, embedding_dimension, embedding_text_version, status, embedded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(candidate_id, facet) DO UPDATE SET embedding_vec=excluded.embedding_vec, "
        "embedding_text=excluded.embedding_text, embedding_model=excluded.embedding_model, "
        "embedding_version=excluded.embedding_version, embedding_dimension=excluded.embedding_dimension, "
        "embedding_text_version=excluded.embedding_text_version, status=excluded.status, "
        "embedded_at=excluded.embedded_at",
        (cid, facet, blob, (txt or '')[:20000], EMBEDDING_MODEL, EMBEDDING_VERSION, dim,
         MV_TEXT_TEMPLATE, status, ts()))
    conn.commit()


def embed_candidate_facets(conn, c, api_key, facets=MV_FACETS):
    """Generate + store the facet vectors for one candidate. Returns a per-facet
    status dict. Never raises."""
    cid = c['id']; out = {}
    import time as _time
    for facet in facets:
        try:
            txt = candidate_facet_text(c, facet, conn)
            if not txt.strip():
                _store_facet(conn, cid, facet, None, '', 'empty')
                out[facet] = 'empty'; continue
            t0 = _time.perf_counter()
            vec = gemini_embed(txt, api_key)
            dur = int((_time.perf_counter() - t0) * 1000)
            if isinstance(vec, dict) and vec.get('error'):
                _store_facet(conn, cid, facet, None, txt, 'failed', error=vec['error'], duration_ms=dur)
                out[facet] = 'failed'; continue
            if not vec:
                _store_facet(conn, cid, facet, None, txt, 'failed', error='empty vector', duration_ms=dur)
                out[facet] = 'failed'; continue
            _store_facet(conn, cid, facet, vec, txt, 'completed', duration_ms=dur)
            out[facet] = 'completed'
        except Exception as e:
            out[facet] = f'error:{e}'
    return out


def _backfill_candidate_facets(conn, cap, api_key):
    """Background pass (gated by multivector_enabled): generate facet vectors for
    candidates that have a completed full embedding but are missing current-
    template facet rows. Bounded per sweep so it never floods the API."""
    if not _mv_cfg('multivector_enabled') or not api_key:
        return 0
    try:
        need = _mv_cfg('multivector_batch')
        rows = conn.execute(
            "SELECT c.* FROM candidates c WHERE c.embedding_status='completed' AND c.id NOT IN ("
            "  SELECT candidate_id FROM candidate_vectors WHERE embedding_text_version=? "
            "  GROUP BY candidate_id HAVING COUNT(DISTINCT facet) >= ?"
            ") LIMIT ?", (MV_TEXT_TEMPLATE, len(MV_FACETS), min(cap, need))).fetchall()
        n = 0
        for c in rows:
            embed_candidate_facets(conn, c, api_key)
            n += 1
        if n:
            print(f'[embed-facet] generated facet vectors for {n} candidate(s)')
        return n
    except Exception as e:
        print(f'[embed-facet] backfill error: {e}')
        return 0


# ══════════════════════════════════════════════════════════════════════
#  PERSISTENT JD (JOB-DESCRIPTION) EMBEDDINGS (Phase 2 / Sprint 9)
#  Embed each mandate's JD ONCE and reuse it, so candidate<->JD matching
#  never re-embeds the JD. Mandates are few, so this runs on by default.
# ══════════════════════════════════════════════════════════════════════
JD_TEXT_VERSION = 1
JD_TEXT_TEMPLATE = f'jd-template-v{JD_TEXT_VERSION}'

def _jd_cfg_enabled():
    try:
        v = get_setting('jd_embed_enabled')
        return True if (v is None or str(v).strip() == '') else bool(int(float(v)))
    except Exception:
        return True


def mandate_jd_text(m):
    """Build the embeddable text for a mandate/JD: role, client, division,
    location, CTC band and the full JD body. Labelled for the model."""
    lines = []
    def add(label, val):
        v = (val or '').strip() if isinstance(val, str) else val
        if v:
            lines.append(f'{label}: {v}')
    add('Role', str(_row_get(m, 'role')))
    add('Client', str(_row_get(m, 'client')))
    add('Division', str(_row_get(m, 'division')))
    add('Location', str(_row_get(m, 'location')))
    cmin, cmax = _row_get(m, 'ctc_min', ''), _row_get(m, 'ctc_max', '')
    if cmin or cmax:
        add('CTC Range', f'{cmin}-{cmax} LPA')
    jd = str(_row_get(m, 'jd')).strip()
    if jd:
        lines.append('Job Description:\n' + jd[:6000])
    return '\n'.join(lines)


def _store_mandate_vec(conn, mid, vec, txt, status, duration_ms=0):
    dim = len(vec) if isinstance(vec, list) else 0
    blob = _vec_to_blob(vec) if (status == 'completed' and vec) else None
    conn.execute(
        "INSERT INTO mandate_vectors (mandate_id, embedding_vec, embedding_text, embedding_model, "
        "embedding_version, embedding_dimension, embedding_text_version, status, embedded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(mandate_id) DO UPDATE SET embedding_vec=excluded.embedding_vec, "
        "embedding_text=excluded.embedding_text, embedding_model=excluded.embedding_model, "
        "embedding_version=excluded.embedding_version, embedding_dimension=excluded.embedding_dimension, "
        "embedding_text_version=excluded.embedding_text_version, status=excluded.status, "
        "embedded_at=excluded.embedded_at",
        (mid, blob, (txt or '')[:20000], EMBEDDING_MODEL, EMBEDDING_VERSION, dim,
         JD_TEXT_TEMPLATE, status, ts()))
    conn.commit()


def embed_mandate_jd(conn, m, api_key):
    """Generate + store the JD vector for one mandate. Returns status string."""
    import time as _time
    mid = m['id']
    txt = mandate_jd_text(m)
    if not txt.strip():
        _store_mandate_vec(conn, mid, None, '', 'empty')
        return 'empty'
    t0 = _time.perf_counter()
    vec = gemini_embed(txt, api_key)
    dur = int((_time.perf_counter() - t0) * 1000)
    if isinstance(vec, dict) and vec.get('error'):
        _store_mandate_vec(conn, mid, None, txt, 'failed')
        print(f'[embed-jd] mandate {mid} FAILED: {str(vec["error"])[:100]}')
        return 'failed'
    if not vec:
        _store_mandate_vec(conn, mid, None, txt, 'failed')
        return 'failed'
    _store_mandate_vec(conn, mid, vec, txt, 'completed', dur)
    print(f'[embed-jd] mandate {mid} embedded dim={len(vec)} in {dur}ms')
    return 'completed'


def _backfill_mandate_jd(conn, cap, api_key):
    """Background pass: embed mandates that have no current JD vector. Enabled by
    default (mandates are few); gate with jd_embed_enabled=0 to disable."""
    if not _jd_cfg_enabled() or not api_key:
        return 0
    try:
        rows = conn.execute(
            "SELECT m.* FROM mandates m WHERE m.id NOT IN ("
            "  SELECT mandate_id FROM mandate_vectors WHERE status='completed' AND embedding_text_version=?"
            ") LIMIT ?", (JD_TEXT_TEMPLATE, cap)).fetchall()
        n = 0
        for m in rows:
            embed_mandate_jd(conn, m, api_key)
            n += 1
        if n:
            print(f'[embed-jd] embedded {n} mandate JD(s)')
        return n
    except Exception as e:
        print(f'[embed-jd] backfill error: {e}')
        return 0


def _mandate_jd_vector(conn, mid):
    """Load a mandate's stored JD vector (or None). No re-embedding."""
    try:
        r = conn.execute("SELECT embedding_vec FROM mandate_vectors WHERE mandate_id=? AND status='completed'",
                         (mid,)).fetchone()
        if not r or not r['embedding_vec']:
            return None
        if _HAS_NUMPY:
            return list(_np.frombuffer(r['embedding_vec'], dtype=_np.float32))
        a = _array.array('f'); a.frombytes(r['embedding_vec']); return list(a)
    except Exception:
        return None


def _reconcile_missing_embeddings(conn, cap):
    """Self-healing: enqueue candidates that have no embedding and no active OR
    failed job. This is what covers the non-Naukri creation paths (manual add,
    import, public apply) without touching each endpoint — and it re-queues
    nothing that already failed permanently, so it can't loop."""
    try:
        rows = conn.execute(
            "SELECT id FROM candidates WHERE (embedding='' OR embedding IS NULL) "
            "AND id NOT IN (SELECT candidate_id FROM embedding_jobs "
            "               WHERE status IN ('pending','processing','retrying','failed')) "
            "ORDER BY id LIMIT ?", (cap,)).fetchall()
        n = 0
        for r in rows:
            if queue_embedding_job(r['id'], conn):
                n += 1
        if n:
            print(f'[embed-queue] reconciler queued {n} un-embedded candidate(s)')
        return n
    except Exception as e:
        print(f'[embed-queue] reconcile error: {e}')
        return 0


def _embedding_worker_loop():
    """Background daemon: drains the embedding_jobs queue with retry, backoff
    and anti-burst throttling. Mirrors the reminder scheduler's shape."""
    import time as _time
    _time.sleep(20)  # let the app finish booting

    # RESTART RECOVERY: any job left 'processing' when the process died is
    # requeued so nothing is stranded mid-flight.
    try:
        conn = get_db()
        n = conn.execute("UPDATE embedding_jobs SET status='pending', started_at='' "
                         "WHERE status='processing'").rowcount
        conn.commit(); conn.close()
        if n:
            print(f'[embed-worker] recovered {n} in-flight job(s) after restart')
    except Exception as e:
        print(f'[embed-worker] recovery error: {e}')

    reconcile_every = 6  # run the self-heal sweep roughly every 6 idle-ish cycles
    cycle = 0
    while True:
        cycle += 1
        try:
            api_key = get_setting('gemini_api_key')
            if not api_key:
                _time.sleep(30)  # nothing to do without a key
                continue

            interval = max(0, _embed_cfg('embed_interval_ms')) / 1000.0
            conn = get_db()
            processed = 0
            for _ in range(_embed_cfg('embed_batch_per_cycle')):
                job = _claim_next_job(conn)
                if not job:
                    break
                try:
                    _process_job(conn, job, api_key)
                except Exception as je:
                    # never let one bad job kill the worker
                    try:
                        nxt = (_ist_now() + datetime.timedelta(seconds=30)).isoformat(timespec='seconds')
                        conn.execute("UPDATE embedding_jobs SET status='retrying', last_error=?, "
                                     "next_attempt_at=? WHERE id=?", (str(je)[:500], nxt, job['id']))
                        conn.commit()
                    except Exception:
                        pass
                    print(f'[embed-worker] job {job["id"]} crashed: {je}')
                processed += 1
                _time.sleep(interval)  # throttle between API calls

            # If the queue was empty this cycle, periodically sweep for
            # un-embedded candidates and backfill the float32 vector cache.
            if processed == 0 and (cycle % reconcile_every == 0):
                _reconcile_missing_embeddings(conn, _embed_cfg('embed_reconcile_cap'))
                _backfill_embedding_blobs(conn, _embed_cfg('embed_reconcile_cap'))
                # Multi-vector facet generation (Sprint 8) — no-op unless
                # multivector_enabled is turned on in Settings.
                _backfill_candidate_facets(conn, _embed_cfg('embed_reconcile_cap'), api_key)
                # Persistent JD embeddings (Sprint 9) — mandates are few; on by default.
                _backfill_mandate_jd(conn, _embed_cfg('embed_reconcile_cap'), api_key)

            conn.close()
        except Exception as e:
            print(f'[embed-worker] loop error: {e}')
        _time.sleep(_embed_cfg('embed_poll_sec'))


_embedding_worker_started = False
def _start_embedding_worker():
    global _embedding_worker_started
    if _embedding_worker_started:
        return
    _embedding_worker_started = True
    import threading
    t = threading.Thread(target=_embedding_worker_loop, daemon=True)
    t.start()
    print('[embed-worker] background thread started')


def embed_candidate_async(cid):
    """Backward-compat shim: previously this embedded inline (blocking). It now
    just ENQUEUES a background job so callers return instantly. Kept so any
    existing caller keeps working unchanged."""
    return queue_embedding_job(cid)


@app.route('/api/ai/index-status', methods=['GET'])
@login_required
def ai_index_status():
    conn = get_db()
    total = conn.execute('SELECT COUNT(*) n FROM candidates').fetchone()['n']
    done = conn.execute("SELECT COUNT(*) n FROM candidates WHERE embedding!='' AND embedding IS NOT NULL").fetchone()['n']
    # Optional metadata breakdown (additive — existing keys unchanged).
    by_status, by_model = {}, {}
    try:
        for r in conn.execute(
                "SELECT COALESCE(NULLIF(embedding_status,''),'unknown') s, COUNT(*) n "
                "FROM candidates GROUP BY s").fetchall():
            by_status[r['s']] = r['n']
        for r in conn.execute(
                "SELECT COALESCE(NULLIF(embedding_model,''),'none') m, COUNT(*) n "
                "FROM candidates GROUP BY m").fetchall():
            by_model[r['m']] = r['n']
    except sqlite3.OperationalError:
        pass  # metadata columns not present yet (pre-migration)
    conn.close()
    has_key = bool(get_setting('gemini_api_key'))
    return jsonify({'ok': True, 'total': total, 'indexed': done,
                    'pending': total - done, 'has_gemini_key': has_key,
                    'by_status': by_status, 'by_model': by_model,
                    'text_template': EMBED_TEXT_TEMPLATE, 'model': EMBEDDING_MODEL})


@app.route('/api/ai/queue/status', methods=['GET'])
@login_required
def ai_queue_status():
    """Live embedding-queue metrics for monitoring."""
    conn = get_db()
    counts = {'pending': 0, 'processing': 0, 'retrying': 0,
              'completed': 0, 'failed': 0, 'cancelled': 0}
    try:
        for r in conn.execute("SELECT status, COUNT(*) n FROM embedding_jobs GROUP BY status").fetchall():
            counts[r['status']] = r['n']
        avg_row = conn.execute("SELECT AVG(duration_ms) a FROM embedding_jobs "
                               "WHERE status='completed' AND duration_ms>0").fetchone()
        avg_ms = int(avg_row['a']) if avg_row and avg_row['a'] else 0
        retried = conn.execute("SELECT COUNT(*) n FROM embedding_jobs WHERE retry_count>0").fetchone()['n']
        oldest = conn.execute("SELECT MIN(created_at) c FROM embedding_jobs "
                              "WHERE status IN ('pending','retrying')").fetchone()['c'] or ''
    except sqlite3.OperationalError:
        avg_ms = retried = 0; oldest = ''
    conn.close()
    waiting = counts['pending'] + counts['retrying']
    return jsonify({'ok': True,
                    'queue_length': waiting + counts['processing'],
                    'waiting': waiting,
                    'running': counts['processing'],
                    'failed': counts['failed'],
                    'retried': retried,
                    'avg_processing_ms': avg_ms,
                    'oldest_waiting_at': oldest,
                    'by_status': counts,
                    'worker_running': _embedding_worker_started,
                    'config': {k: _embed_cfg(k) for k in _EMBED_CFG_DEFAULTS}})


@app.route('/api/ai/queue/enqueue-missing', methods=['POST'])
@login_required
def ai_queue_enqueue_missing():
    """Queue every un-embedded candidate that has no active/failed job. This is
    the entry point for large imports ('500 resumes -> queue all'). Non-blocking:
    it only creates job rows; the background worker does the embedding."""
    d = request.json or {}
    cap = int(d.get('cap') or 5000)
    conn = get_db()
    n = _reconcile_missing_embeddings(conn, cap)
    conn.close()
    return jsonify({'ok': True, 'queued': n})


@app.route('/api/ai/queue/retry-failed', methods=['POST'])
@login_required
def ai_queue_retry_failed():
    """Reset failed jobs back to pending (e.g. after fixing an API key). The
    worker will pick them up on its next cycle."""
    conn = get_db()
    try:
        n = conn.execute("UPDATE embedding_jobs SET status='pending', retry_count=0, "
                         "last_error='', next_attempt_at=? WHERE status='failed'", (ts(),)).rowcount
        conn.commit()
    except sqlite3.OperationalError:
        n = 0
    conn.close()
    return jsonify({'ok': True, 'requeued': n})


@app.route('/api/ai/reindex', methods=['POST'])
@login_required
def ai_reindex():
    """Embed a BATCH of un-embedded candidates per call (default 25). The
    frontend calls this repeatedly until pending=0, so a single HTTP request
    never runs long enough to time out, and progress is visible."""
    d = request.json or {}
    force = bool(d.get('force'))
    batch = int(d.get('batch') or 25)
    api_key = get_setting('gemini_api_key')
    if not api_key:
        return jsonify({'error': 'Gemini API key not set. Add it in Settings.'}), 400

    conn = get_db()
    if force:
        rows = conn.execute('SELECT * FROM candidates ORDER BY id LIMIT ?', (batch,)).fetchall()
        # For force, also clear so they re-embed; but simplest: process those
        # without embedding first; force re-embeds everything across calls by
        # clearing embeddings up front on the first force call.
        if d.get('reset'):
            conn.execute("UPDATE candidates SET embedding='', embedding_status='pending'")
            conn.commit()
            rows = conn.execute('SELECT * FROM candidates ORDER BY id LIMIT ?', (batch,)).fetchall()
    rows = conn.execute("SELECT * FROM candidates WHERE embedding='' OR embedding IS NULL ORDER BY id LIMIT ?", (batch,)).fetchall()

    done, failed, skipped = 0, 0, 0
    first_error = ''
    for c in rows:
        r = embed_candidate_row(conn, c, api_key)
        st = r['status']
        if st == 'missing':
            skipped += 1
        elif st == 'failed':
            failed += 1
            first_error = r.get('error', '')
            eu = str(first_error).upper()
            # Auth/permission errors won't fix themselves within this batch —
            # abort so we don't burn the whole batch (unchanged behaviour).
            if 'API KEY' in eu or 'PERMISSION' in eu or 'API_KEY' in eu:
                conn.close()
                return jsonify({'error': 'Gemini error: ' + first_error,
                                'indexed': done, 'failed': failed}), 400
        else:
            done += 1

    # remaining count
    pending = conn.execute("SELECT COUNT(*) n FROM candidates WHERE embedding='' OR embedding IS NULL").fetchone()['n']
    conn.close()
    return jsonify({'ok': True, 'indexed': done, 'failed': failed, 'skipped': skipped,
                    'pending': pending, 'first_error': first_error})


# ══════════════════════════════════════════════════════════════════════
#  SEMANTIC SEARCH — performance layer (Sprint 4)
#  Streaming chunked scan + heap top-N + optional numpy + query cache.
#  No new infrastructure: pure in-process, SQLite-backed.
# ══════════════════════════════════════════════════════════════════════
import heapq as _heapq
import collections as _collections
import threading as _search_threading

try:
    import numpy as _np
    _HAS_NUMPY = True
except Exception:
    _HAS_NUMPY = False

_SEARCH_CFG_DEFAULTS = {
    'search_max_candidates': 100000,  # hard cap on candidates evaluated
    'search_max_results':    200,     # ranked pool kept for pagination
    'search_min_score':      -1.0,    # min cosine (-1..1) to include; -1 = keep all
    'search_chunk_size':     2000,    # streaming chunk size
    'search_slow_ms':        1500,    # log searches slower than this
    'search_cache_ttl_sec':  120,     # query cache TTL
    'search_cache_max':      64,      # max cached queries
}

def _search_cfg(key):
    dflt = _SEARCH_CFG_DEFAULTS[key]
    try:
        v = get_setting(key)
        if v is None or str(v).strip() == '':
            return dflt
        return float(v) if isinstance(dflt, float) else int(float(v))
    except Exception:
        return dflt

# In-memory query cache + rolling metrics (bounded; no Redis).
_SEARCH_CACHE = {}
_SEARCH_CACHE_LOCK = _search_threading.Lock()
_SEARCH_METRICS = _collections.deque(maxlen=500)      # (duration_ms, scanned)
_SEARCH_METRICS_LOCK = _search_threading.Lock()

def _search_cache_get(key):
    import time as _time
    with _SEARCH_CACHE_LOCK:
        e = _SEARCH_CACHE.get(key)
        if not e:
            return None
        if _time.time() - e['ts'] > _search_cfg('search_cache_ttl_sec'):
            _SEARCH_CACHE.pop(key, None)
            return None
        return e

def _search_cache_put(key, payload):
    import time as _time
    with _SEARCH_CACHE_LOCK:
        payload = dict(payload); payload['ts'] = _time.time()
        _SEARCH_CACHE[key] = payload
        # evict oldest beyond cap
        cap = _search_cfg('search_cache_max')
        if len(_SEARCH_CACHE) > cap:
            for k in sorted(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k]['ts'])[:len(_SEARCH_CACHE) - cap]:
                _SEARCH_CACHE.pop(k, None)

def _search_metric_record(duration_ms, scanned):
    with _SEARCH_METRICS_LOCK:
        _SEARCH_METRICS.append((duration_ms, scanned))


def _search_rank(conn, qvec, min_score, max_cands, pool):
    """Stream candidate vectors in chunks, score against qvec, keep a top-`pool`
    heap. Returns (ranked, scanned) where ranked is a descending list of
    (similarity, candidate_id). Memory stays bounded regardless of table size
    because we never hold all vectors at once and keep only `pool` results."""
    d = len(qvec)
    chunk = _search_cfg('search_chunk_size')
    q_norm = math.sqrt(sum(x * x for x in qvec)) or 1e-9
    q_np = _np.asarray(qvec, dtype=_np.float32) if _HAS_NUMPY else None

    heap = []   # min-heap of (sim, id)
    scanned = 0

    def _push(sim, cid):
        if sim < min_score:
            return
        if len(heap) < pool:
            _heapq.heappush(heap, (sim, cid))
        elif sim > heap[0][0]:
            _heapq.heappushpop(heap, (sim, cid))

    cur = conn.execute(
        "SELECT id, embedding_vec, embedding FROM candidates "
        "WHERE embedding IS NOT NULL AND embedding NOT IN ('', '[]')")
    while True:
        batch = cur.fetchmany(chunk)
        if not batch:
            break
        ids, mats = [], []
        for r in batch:
            if scanned >= max_cands:
                break
            scanned += 1
            vec = None
            blob = r['embedding_vec']
            if blob:                                   # fast path: float32 bytes
                try:
                    if _HAS_NUMPY:
                        vec = _np.frombuffer(blob, dtype=_np.float32)
                    else:
                        vec = _array.array('f'); vec.frombytes(blob)
                except Exception:
                    vec = None
            if vec is None:                            # fallback: legacy JSON
                try:
                    vec = json.loads(r['embedding'])
                except Exception:
                    continue
            if vec is None or len(vec) != d:
                continue
            if _HAS_NUMPY:
                ids.append(r['id']); mats.append(vec)
            else:
                dot = 0.0; nb = 0.0
                for x, y in zip(qvec, vec):
                    dot += x * y; nb += y * y
                nb = math.sqrt(nb)
                if nb:
                    _push(dot / (nb * q_norm), r['id'])
        if _HAS_NUMPY and ids:
            m = _np.asarray(mats, dtype=_np.float32)          # (n, d)
            dots = m @ q_np                                    # (n,)
            norms = _np.sqrt((m * m).sum(axis=1))
            norms[norms == 0] = 1e-9
            sims = dots / (norms * q_norm)
            for cid, sim in zip(ids, sims):
                _push(float(sim), cid)
            del m, dots, norms, sims                           # release chunk memory
        if scanned >= max_cands:
            break

    ranked = sorted(heap, key=lambda x: x[0], reverse=True)
    return ranked, scanned


def _search_hydrate(conn, page_slice):
    """Load full display fields (with mandate join) ONLY for the ranked page.
    Preserves ranked order and attaches the score. Same result shape as before."""
    if not page_slice:
        return []
    score_map = {cid: sim for sim, cid in page_slice}
    ids = [cid for _, cid in page_slice]
    ph = ','.join('?' * len(ids))
    rows = conn.execute(
        "SELECT c.id, c.name, c.designation, c.company, c.experience, c.location, "
        "c.ctc_current, c.ctc_expected, c.notice_period, c.phone, c.email, c.key_skills, "
        "c.mandate_id, c.stage, m.role AS mandate_role, m.client AS mandate_client, "
        "m.status AS mandate_status "
        "FROM candidates c LEFT JOIN mandates m ON m.id=c.mandate_id "
        f"WHERE c.id IN ({ph})", ids).fetchall()
    rowmap = {r['id']: r for r in rows}
    results = []
    for cid in ids:  # preserve ranked order
        c = rowmap.get(cid)
        if not c:
            continue
        try:
            skills = json.loads(c['key_skills'] or '[]')
        except Exception:
            skills = []
        results.append({
            'id': c['id'], 'name': c['name'], 'designation': c['designation'],
            'company': c['company'], 'experience': c['experience'],
            'location': c['location'], 'ctc_current': c['ctc_current'],
            'ctc_expected': c['ctc_expected'], 'notice_period': c['notice_period'],
            'phone': c['phone'], 'email': c['email'],
            'key_skills': skills,
            'mandate_id': c['mandate_id'], 'mandate_role': c['mandate_role'],
            'mandate_client': c['mandate_client'], 'mandate_status': c['mandate_status'],
            'stage': c['stage'],
            'score': round(score_map[cid] * 100, 1),
        })
    return results


# ══════════════════════════════════════════════════════════════════════
#  HYBRID SEARCH ENGINE (Sprint 6)
#  Layers structured understanding + business-rule scoring on top of the
#  Sprint-4 semantic retrieval. Design principle: NL-extracted signals only
#  RE-RANK (never exclude) so recall is preserved and a filter-less query
#  scores exactly like pure semantic search (perfect backward compatibility).
#  Hard exclusion happens only for explicitly-supplied mandatory filters.
# ══════════════════════════════════════════════════════════════════════

_HYBRID_CFG_DEFAULTS = {
    'hybrid_enabled':      1,      # default-on; harmless for filter-less queries
    'hybrid_pool':         300,    # semantic top-N to re-rank structurally
    'hybrid_w_semantic':   0.55,
    'hybrid_w_skills':     0.20,
    'hybrid_w_industry':   0.06,
    'hybrid_w_company':    0.06,
    'hybrid_w_experience': 0.07,
    'hybrid_w_location':   0.06,
    'hybrid_w_recency':    0.0,    # off by default
    'hybrid_taxo_ttl_sec': 600,    # taxonomy cache lifetime
    'hybrid_taxo_sample':  20000,  # candidates sampled to build the vocabulary
}
def _hybrid_cfg(key):
    dflt = _HYBRID_CFG_DEFAULTS[key]
    try:
        v = get_setting(key)
        if v is None or str(v).strip() == '':
            return dflt
        return float(v) if isinstance(dflt, float) else int(float(v))
    except Exception:
        return dflt

# Noise tokens dropped from company/location vocab so "electric", "india" etc.
# don't cause spurious matches.
_HYBRID_STOP = {'electric', 'electricals', 'ltd', 'limited', 'pvt', 'private', 'india',
                'technologies', 'technology', 'solutions', 'systems', 'system', 'group',
                'co', 'company', 'corp', 'inc', 'llp', 'the', 'and', 'engineering',
                'services', 'industries', 'international', 'global'}

def _lc_tokens(s):
    return [t for t in re.findall(r'[a-z0-9]+', (s or '').lower()) if len(t) > 1]

def _parse_tag_list(raw):
    """Parse a candidate tag column (JSON list / comma string) into a lowercased
    phrase set. Reuses the same tolerant parsing as the embedder."""
    txt = _as_list_str(raw)  # 'a, b, c'
    out = set()
    for p in txt.split(','):
        p = p.strip().lower()
        if p:
            out.add(p)
    return out


_HYBRID_TAXO = {'ts': 0.0, 'data': None}
def _hybrid_taxonomy(conn):
    """Vocabulary built from EXISTING candidate data (the 'existing taxonomy'
    the brief refers to) — skill phrases, company tokens, location tokens,
    industry phrases. Cached with TTL so it isn't rebuilt every search."""
    import time as _time
    now = _time.time()
    if _HYBRID_TAXO['data'] is not None and now - _HYBRID_TAXO['ts'] < _hybrid_cfg('hybrid_taxo_ttl_sec'):
        return _HYBRID_TAXO['data']
    skills, companies, locations, industries = set(), set(), set(), set()
    try:
        sample = _hybrid_cfg('hybrid_taxo_sample')
        rows = conn.execute(
            "SELECT key_skill_tags, key_skills, secondary_skills, domain_tags, product_handles, "
            "function_tags, company, industry_background, location, preferred_location "
            "FROM candidates LIMIT ?", (sample,)).fetchall()
        for r in rows:
            for col in ('key_skill_tags', 'key_skills', 'secondary_skills', 'domain_tags',
                        'product_handles', 'function_tags'):
                skills |= _parse_tag_list(r[col])
            for tok in _lc_tokens(r['company']):
                if tok not in _HYBRID_STOP:
                    companies.add(tok)
            for col in ('location', 'preferred_location'):
                for tok in _lc_tokens(r[col]):
                    if tok not in _HYBRID_STOP:
                        locations.add(tok)
            industries |= _parse_tag_list(r['industry_background'])
        # previous employers too
        for r in conn.execute("SELECT DISTINCT company FROM work_history LIMIT ?", (sample,)).fetchall():
            for tok in _lc_tokens(r['company']):
                if tok not in _HYBRID_STOP:
                    companies.add(tok)
    except sqlite3.OperationalError:
        pass
    skills = {s for s in skills if len(s) > 1 and not s.isdigit()}
    data = {'skills': skills, 'companies': companies, 'locations': locations, 'industries': industries}
    _HYBRID_TAXO['data'] = data; _HYBRID_TAXO['ts'] = now
    return data


def _query_grams(query):
    toks = re.findall(r'[a-z0-9]+', query.lower())
    grams = set(toks)
    for n in (2, 3):
        for i in range(len(toks) - n + 1):
            grams.add(' '.join(toks[i:i + n]))
    return grams, set(toks)


def _extract_query_filters(query, taxo):
    """Detect structured signals from a natural-language recruiter query using
    the existing taxonomy + light regex. All soft (re-rank) unless promoted to
    mandatory by the caller. O(query length) via n-gram intersection, so it
    scales regardless of vocabulary size."""
    grams, toks = _query_grams(query)
    ql = ' ' + query.lower() + ' '
    detected = {'skills': [], 'companies': [], 'locations': [], 'industries': [],
                'min_experience': None, 'max_experience': None, 'education': [],
                'immediate': False, 'salary_max': None, 'employment_type': None}

    detected['skills']     = sorted(g for g in grams if g in taxo['skills'])
    detected['industries'] = sorted(g for g in grams if g in taxo['industries'])
    detected['companies']  = sorted(t for t in toks if t in taxo['companies'])
    detected['locations']  = sorted(t for t in toks if t in taxo['locations'])

    mrange = re.search(r'(\d+)\s*(?:-|to)\s*(\d+)\s*(?:\+?\s*)?(?:years?|yrs?)', ql)
    if mrange:
        detected['min_experience'] = float(mrange.group(1)); detected['max_experience'] = float(mrange.group(2))
    else:
        mexp = re.search(r'(\d+)\s*\+?\s*(?:years?|yrs?)', ql)
        if mexp:
            detected['min_experience'] = float(mexp.group(1))

    for pat, label in [(r'\bb\.?tech\b', 'btech'), (r'\bm\.?tech\b', 'mtech'),
                       (r'\bb\.?e\b', 'be'), (r'\bmba\b', 'mba'),
                       (r'\bdiploma\b', 'diploma'), (r'\bphd\b', 'phd'), (r'\bb\.?sc\b', 'bsc')]:
        if re.search(pat, ql):
            detected['education'].append(label)
    if re.search(r'\bimmediate(ly)?\b|\bimmediate joiner\b', ql):
        detected['immediate'] = True
    msal = re.search(r'(\d+(?:\.\d+)?)\s*(?:lpa|lakhs?|lac|l)\b', ql)
    if msal:
        detected['salary_max'] = float(msal.group(1))
    if re.search(r'\bcontract\b|\bcontractual\b', ql):
        detected['employment_type'] = 'contract'
    elif re.search(r'\bpermanent\b|\bfull[- ]?time\b', ql):
        detected['employment_type'] = 'permanent'
    elif re.search(r'\bfreelanc', ql):
        detected['employment_type'] = 'freelance'
    return detected


def _candidate_signal_sets(c, wh_companies):
    """Build the comparable sets for one candidate row (skills phrases, company
    tokens, location tokens, industry phrases)."""
    skills = set()
    for col in ('key_skill_tags', 'key_skills', 'secondary_skills', 'domain_tags',
                'product_handles', 'function_tags'):
        try:
            skills |= _parse_tag_list(c[col])
        except Exception:
            pass
    comp_toks = set(t for t in _lc_tokens(c['company']) if t not in _HYBRID_STOP)
    for co in (wh_companies or []):
        comp_toks |= set(t for t in _lc_tokens(co) if t not in _HYBRID_STOP)
    loc_toks = set(_lc_tokens(c['location'])) | set(_lc_tokens(c['preferred_location']))
    industries = set()
    try:
        industries |= _parse_tag_list(c['industry_background'])
    except Exception:
        pass
    return skills, comp_toks, loc_toks, industries


def _hybrid_score_one(sem, c, wh_companies, detected, weights, mandatory_skills):
    """Return (hybrid_score 0..1, structured_score 0..1, explain). Only signals
    actually present in the query contribute — so a filter-less query collapses
    to hybrid == semantic (identical to the old engine)."""
    cskills, ccomp, cloc, cind = _candidate_signal_sets(c, wh_companies)
    signals = {'semantic': max(0.0, min(1.0, sem))}
    active = {'semantic': weights['semantic']}
    explain = {'matched_skills': [], 'matched_industries': [], 'matched_companies': [],
               'matched_experience': None, 'missing_mandatory_skills': []}

    if detected['skills']:
        matched = [s for s in detected['skills'] if s in cskills]
        signals['skills'] = len(matched) / len(detected['skills'])
        active['skills'] = weights['skills']; explain['matched_skills'] = matched
    if detected['industries']:
        matched = [s for s in detected['industries'] if s in cind]
        signals['industry'] = 1.0 if matched else 0.0
        active['industry'] = weights['industry']; explain['matched_industries'] = matched
    if detected['companies']:
        matched = [s for s in detected['companies'] if s in ccomp]
        signals['company'] = 1.0 if matched else 0.0
        active['company'] = weights['company']; explain['matched_companies'] = matched
    if detected['locations']:
        matched = [s for s in detected['locations'] if s in cloc]
        signals['location'] = 1.0 if matched else 0.0
        active['location'] = weights['location']
        explain['matched_location'] = matched
    if detected['min_experience'] is not None:
        try:
            exp = float(c['experience'] or 0)
        except Exception:
            exp = 0.0
        need = detected['min_experience']
        ok = exp >= need
        signals['experience'] = 1.0 if ok else max(0.0, exp / need if need else 1.0)
        active['experience'] = weights['experience']
        explain['matched_experience'] = {'candidate': exp, 'required_min': need, 'ok': ok}

    tot = sum(active.values()) or 1.0
    hybrid = sum(signals[k] * active[k] for k in active) / tot
    struct_w = {k: v for k, v in active.items() if k != 'semantic'}
    structured = (sum(signals[k] * struct_w[k] for k in struct_w) / (sum(struct_w.values()) or 1.0)) if struct_w else 0.0

    if mandatory_skills:
        explain['missing_mandatory_skills'] = [s for s in mandatory_skills if s not in cskills]
    return hybrid, structured, explain


def _hybrid_process(conn, query, ranked, d):
    """Re-rank the semantic pool with structured signals. Returns
    (scored_results, filters_detected). Each scored result is a full display
    dict (same shape as _search_hydrate) plus 'semantic_score' and 'explain'."""
    weights = {
        'semantic':   _hybrid_cfg('hybrid_w_semantic'),
        'skills':     _hybrid_cfg('hybrid_w_skills'),
        'industry':   _hybrid_cfg('hybrid_w_industry'),
        'company':    _hybrid_cfg('hybrid_w_company'),
        'experience': _hybrid_cfg('hybrid_w_experience'),
        'location':   _hybrid_cfg('hybrid_w_location'),
        'recency':    _hybrid_cfg('hybrid_w_recency'),
    }
    taxo = _hybrid_taxonomy(conn)
    detected = _extract_query_filters(query, taxo)

    # merge explicit filters (opt-in, can be mandatory/hard)
    exp_filters = d.get('filters') or {}
    if exp_filters.get('skills'):
        detected['skills'] = sorted(set(detected['skills']) | {s.lower() for s in exp_filters['skills']})
    if exp_filters.get('min_experience') is not None:
        detected['min_experience'] = float(exp_filters['min_experience'])
    if exp_filters.get('location'):
        detected['locations'] = sorted(set(detected['locations']) | set(_lc_tokens(exp_filters['location'])))
    mandatory_skills = [s.lower() for s in (d.get('mandatory_skills') or [])]
    hard_location = _lc_tokens(exp_filters.get('location', '')) if exp_filters.get('mandatory_location') else []
    hard_min_exp = float(exp_filters['min_experience']) if (exp_filters.get('min_experience') is not None
                        and exp_filters.get('mandatory_experience')) else None

    pool_ids = [cid for _, cid in ranked[:_hybrid_cfg('hybrid_pool')]]
    sem_map = {cid: sim for sim, cid in ranked}
    if not pool_ids:
        return [], detected

    ph = ','.join('?' * len(pool_ids))
    rows = conn.execute(
        "SELECT c.id, c.name, c.designation, c.company, c.experience, c.location, c.preferred_location, "
        "c.ctc_current, c.ctc_expected, c.notice_period, c.phone, c.email, c.key_skills, "
        "c.secondary_skills, c.key_skill_tags, c.domain_tags, c.product_handles, c.function_tags, "
        "c.industry_background, c.mandate_id, c.stage, "
        "m.role AS mandate_role, m.client AS mandate_client, m.status AS mandate_status "
        "FROM candidates c LEFT JOIN mandates m ON m.id=c.mandate_id "
        f"WHERE c.id IN ({ph})", pool_ids).fetchall()
    rowmap = {r['id']: r for r in rows}
    # batch previous-employer companies for the pool
    wh_map = {}
    try:
        for r in conn.execute(f"SELECT candidate_id, company FROM work_history WHERE candidate_id IN ({ph})",
                              pool_ids).fetchall():
            wh_map.setdefault(r['candidate_id'], []).append(r['company'])
    except sqlite3.OperationalError:
        pass

    scored = []
    for cid in pool_ids:
        c = rowmap.get(cid)
        if c is None:
            continue
        # hard (mandatory) exclusion — opt-in only
        if mandatory_skills or hard_location or hard_min_exp is not None:
            cskills, ccomp, cloc, _ = _candidate_signal_sets(c, wh_map.get(cid))
            if mandatory_skills and any(s not in cskills for s in mandatory_skills):
                continue
            if hard_location and not (set(hard_location) & cloc):
                continue
            if hard_min_exp is not None:
                try:
                    if float(c['experience'] or 0) < hard_min_exp:
                        continue
                except Exception:
                    continue
        sem = sem_map.get(cid, 0.0)
        hyb, structured, explain = _hybrid_score_one(sem, c, wh_map.get(cid), detected, weights, mandatory_skills)
        try:
            skills = json.loads(c['key_skills'] or '[]')
        except Exception:
            skills = []
        scored.append({
            'id': c['id'], 'name': c['name'], 'designation': c['designation'],
            'company': c['company'], 'experience': c['experience'], 'location': c['location'],
            'ctc_current': c['ctc_current'], 'ctc_expected': c['ctc_expected'],
            'notice_period': c['notice_period'], 'phone': c['phone'], 'email': c['email'],
            'key_skills': skills, 'mandate_id': c['mandate_id'], 'mandate_role': c['mandate_role'],
            'mandate_client': c['mandate_client'], 'mandate_status': c['mandate_status'],
            'stage': c['stage'],
            'score': round(hyb * 100, 1),
            'semantic_score': round(sem * 100, 1),
            'structured_score': round(structured * 100, 1),
            'explain': explain,
            '_sem': sem,
        })
    # sort by hybrid score desc, tie-break on semantic
    scored.sort(key=lambda x: (x['score'], x['_sem']), reverse=True)
    for x in scored:
        x.pop('_sem', None)
    return scored, detected


def _rme_capture_search(query, detected, result_count, user):
    """Log the search into the Recruitment Memory Engine as a search_activity
    event (Sprint 5). Best-effort, never blocks or breaks search."""
    try:
        conn = get_db()
        rme_add_event(conn, 'search_activity', 'recruiter',
                      str((user or {}).get('id', 'system')),
                      actor=str((user or {}).get('username', 'system')),
                      summary=query[:200],
                      data={'skills': detected.get('skills', []),
                            'companies': detected.get('companies', []),
                            'locations': detected.get('locations', []),
                            'industries': detected.get('industries', []),
                            'min_experience': detected.get('min_experience'),
                            'results': result_count})
        conn.close()
    except Exception:
        pass


# ── Multi-vector (facet) re-rank (Sprint 8) ────────────────────────────
def _facet_vectors_for(conn, ids):
    """Bulk-load completed facet vectors for candidate ids.
    Returns {candidate_id: [vector, ...]}."""
    if not ids:
        return {}
    ph = ','.join('?' * len(ids))
    out = {}
    try:
        for r in conn.execute(
                f"SELECT candidate_id, embedding_vec FROM candidate_vectors "
                f"WHERE status='completed' AND embedding_vec IS NOT NULL AND candidate_id IN ({ph})",
                ids).fetchall():
            blob = r['embedding_vec']
            if not blob:
                continue
            try:
                if _HAS_NUMPY:
                    v = _np.frombuffer(blob, dtype=_np.float32)
                else:
                    v = _array.array('f'); v.frombytes(blob)
            except Exception:
                continue
            out.setdefault(r['candidate_id'], []).append(v)
    except sqlite3.OperationalError:
        pass
    return out


def _multivector_rerank(conn, qvec, ranked):
    """Opt-in: blend the whole-profile similarity with the BEST-matching facet
    (skills/experience/projects) so a focused query surfaces a candidate whose
    relevant facet matches strongly even if their full-profile vector is diluted
    by unrelated resume text. Only the semantic pool is touched; candidates with
    no facet vectors keep their original score. Returns a re-sorted (sim, id) list."""
    pool_n = _hybrid_cfg('hybrid_pool')
    pool = ranked[:pool_n]
    ids = [cid for _, cid in pool]
    fmap = _facet_vectors_for(conn, ids)
    if not fmap:
        return ranked  # no facet vectors yet -> unchanged (backward compatible)
    wf = _mv_cfg('multivector_w_full'); wx = _mv_cfg('multivector_w_facet')
    d = len(qvec)
    qn = math.sqrt(sum(x * x for x in qvec)) or 1e-9
    q_np = _np.asarray(qvec, dtype=_np.float32) if _HAS_NUMPY else None

    def _cos(v):
        if len(v) != d:
            return 0.0
        if _HAS_NUMPY:
            nb = float(_np.linalg.norm(v)) or 1e-9
            return float(_np.dot(q_np, v)) / (nb * qn)
        dot = 0.0; nb = 0.0
        for x, y in zip(qvec, v):
            dot += x * y; nb += y * y
        nb = math.sqrt(nb) or 1e-9
        return dot / (nb * qn)

    boosted = []
    for sim, cid in pool:
        facets = fmap.get(cid)
        if facets:
            best = max(_cos(v) for v in facets)
            boosted.append((wf * sim + wx * best, cid))
        else:
            boosted.append((sim, cid))
    boosted.sort(key=lambda x: x[0], reverse=True)
    return boosted + ranked[pool_n:]  # re-ranked pool + untouched tail


# ── LLM re-ranking (Sprint 10) ─────────────────────────────────────────
_RERANK_CFG_DEFAULTS = {'llm_rerank_enabled': 0, 'llm_rerank_top_n': 10}
def _rerank_cfg(key):
    dflt = _RERANK_CFG_DEFAULTS[key]
    try:
        v = get_setting(key)
        return dflt if (v is None or str(v).strip() == '') else int(float(v))
    except Exception:
        return dflt


def _llm_rerank(results, query, top_n, api_key):
    """Second-stage AI re-rank over the top-N hybrid results. Sends compact
    candidate summaries to DeepSeek and asks for a fit-ordered list with short
    reasons. Only the top-N slice is reordered; on ANY failure the original
    order is returned unchanged (never worse than hybrid)."""
    subset = results[:top_n]
    if len(subset) < 2 or not api_key:
        return results, False
    lines = []
    for r in subset:
        skills = ', '.join((r.get('key_skills') or [])[:8])
        lines.append(f"id={r['id']} | {r.get('name','')} | {r.get('designation','')} at "
                     f"{r.get('company','')} | {r.get('experience',0)}y | {r.get('location','')} | "
                     f"skills: {skills}")
    prompt = ("You are an expert technical recruiter. Rank these candidates by fit for the "
              f"search query: \"{query}\".\n\nCandidates:\n" + '\n'.join(lines) +
              "\n\nReturn ONLY a JSON array, best fit first, each item exactly: "
              '{"id": <candidate id>, "fit": <0-100 integer>, "reason": "<max 12 words>"}. '
              "No prose, no markdown.")
    try:
        resp = call_deepseek(api_key,
            {'model': 'deepseek-chat', 'temperature': 0.2, 'max_tokens': 700,
             'messages': [{'role': 'user', 'content': prompt}]},
            timeout=60, endpoint='rerank')
        if resp.status_code != 200:
            return results, False
        arr = parse_json(resp.json()['choices'][0]['message']['content'])
        if not isinstance(arr, list) or not arr:
            return results, False
    except Exception:
        return results, False

    by_id = {r['id']: r for r in subset}
    ordered, seen = [], set()
    for item in arr:
        try:
            cid = int(item.get('id'))
        except Exception:
            continue
        r = by_id.get(cid)
        if r and cid not in seen:
            r = dict(r)
            r['llm_score'] = item.get('fit')
            r['llm_reason'] = str(item.get('reason', ''))[:160]
            ordered.append(r); seen.add(cid)
    # any top-N candidates the model omitted keep their original relative order
    for r in subset:
        if r['id'] not in seen:
            ordered.append(r)
    return ordered + results[top_n:], True


@app.route('/api/ai/search', methods=['POST'])
@login_required
def ai_search():
    """Semantic talent search across ALL candidates (ignores mandate
    boundaries). Sprint 4: streams candidate vectors in chunks (bounded
    memory), keeps a top-N heap, loads full display fields only for the
    returned page, and caches query embeddings + ranked results."""
    import time as _time
    t_start = _time.perf_counter()
    d = request.json or {}
    query = (d.get('query') or '').strip()
    if not query:
        return jsonify({'error': 'Empty query'}), 400

    # ── Params / config ────────────────────────────────────────────────
    top_k       = int(d.get('top_k') or 10)
    page        = max(1, int(d.get('page') or 1))
    page_size   = int(d.get('page_size') or top_k)
    max_cands   = int(d.get('max_candidates') or _search_cfg('search_max_candidates'))
    min_score   = float(d.get('min_score') if d.get('min_score') is not None else _search_cfg('search_min_score'))
    pool        = max(_search_cfg('search_max_results'), page * page_size)
    no_cache    = bool(d.get('no_cache'))
    offset      = (page - 1) * page_size

    api_key = get_setting('gemini_api_key')
    if not api_key:
        return jsonify({'error': 'Gemini API key not set. Add it in Settings.'}), 400

    timing = {}
    ckey = f'{query.lower()}|{min_score}|{max_cands}|{pool}'

    # ── 1) Query embedding (cache) ─────────────────────────────────────
    t0 = _time.perf_counter()
    cached = None if no_cache else _search_cache_get(ckey)
    if cached:
        qvec = cached['qvec']; ranked = cached['ranked']; scanned = cached['scanned']
        timing['embed_ms'] = 0; timing['scan_ms'] = 0; from_cache = True
    else:
        qvec = gemini_embed(query, api_key)
        if isinstance(qvec, dict) and qvec.get('error'):
            return jsonify({'error': 'Gemini error: ' + qvec['error']}), 400
        if not qvec:
            return jsonify({'error': 'Could not embed query'}), 400
        timing['embed_ms'] = int((_time.perf_counter() - t0) * 1000)
        from_cache = False

    conn = get_db()

    # ── 2) Stream + score + rank (only when not cached) ────────────────
    if not from_cache:
        t0 = _time.perf_counter()
        ranked, scanned = _search_rank(conn, qvec, min_score, max_cands, pool)
        timing['scan_ms'] = int((_time.perf_counter() - t0) * 1000)
        if not no_cache:
            _search_cache_put(ckey, {'qvec': qvec, 'ranked': ranked, 'scanned': scanned})

    total = len(ranked)

    # ── 2a) Multi-vector re-rank (Sprint 8) — opt-in, off by default ────
    # Blends best-matching facet vector into the ranking. Returns ranked
    # unchanged if no facet vectors exist, so it's safe even before generation.
    mv_on = bool(d.get('multivector')) if ('multivector' in d) else bool(_mv_cfg('multivector_enabled'))
    if mv_on:
        t0 = _time.perf_counter()
        ranked = _multivector_rerank(conn, qvec, ranked)
        timing['multivector_ms'] = int((_time.perf_counter() - t0) * 1000)
        total = len(ranked)

    # ── 2b) Hybrid re-rank (Sprint 6) ──────────────────────────────────
    # Default-on but soft: with no detectable structured signals it collapses
    # to pure semantic order (identical to the old engine). Opt out with hybrid=false.
    hybrid_on = bool(d.get('hybrid')) if ('hybrid' in d) else bool(_hybrid_cfg('hybrid_enabled'))
    filters_detected = {}
    if hybrid_on:
        t0 = _time.perf_counter()
        scored_pool, filters_detected = _hybrid_process(conn, query, ranked, d)
        timing['hybrid_ms'] = int((_time.perf_counter() - t0) * 1000)
        total = len(scored_pool)
        page_items = scored_pool[offset:offset + page_size]
        results = page_items
        timing['hydrate_ms'] = 0
        page_slice = [(it['semantic_score'] / 100.0, it['id']) for it in page_items]
    else:
        # ── 3) Two-phase: full fields for THIS page only (semantic-only) ──
        t0 = _time.perf_counter()
        page_slice = ranked[offset:offset + page_size]
        results = _search_hydrate(conn, page_slice)
        timing['hydrate_ms'] = int((_time.perf_counter() - t0) * 1000)

    # ── 3b) LLM re-rank (Sprint 10) — opt-in, page 1 only ──────────────
    # A second AI pass reorders the top results and attaches fit reasons.
    # Off by default; one DeepSeek call, and it never worsens hybrid order.
    rerank_on = bool(d.get('rerank')) if ('rerank' in d) else bool(_rerank_cfg('llm_rerank_enabled'))
    reranked = False
    if rerank_on and page == 1 and len(results) >= 2:
        ds_key = get_setting('deepseek_api_key')
        if ds_key:
            t0 = _time.perf_counter()
            results, reranked = _llm_rerank(results, query, _rerank_cfg('llm_rerank_top_n'), ds_key)
            timing['rerank_ms'] = int((_time.perf_counter() - t0) * 1000)

    # ── 4) Optional AI reasoning over the page (unchanged behaviour) ───
    reasoning = ''
    if bool(d.get('explain')) and results:
        ds_key = get_setting('deepseek_api_key')
        if ds_key:
            cand_lines = []
            for r in results[:6]:
                cand_lines.append(f"- {r['name']} ({r['designation']} at {r['company']}, "
                                  f"{r['experience']}y, {r['location']}, skills: {', '.join(r['key_skills'][:6])}) "
                                  f"[currently in mandate: {r['mandate_role'] or 'N/A'}]")
            prompt = ("A recruiter searched their candidate pool for: \"" + query + "\".\n\n"
                      "Here are the top matches:\n" + '\n'.join(cand_lines) + "\n\n"
                      "In 3-5 short bullet points, explain which candidates fit best and WHY "
                      "(note if someone saved for a different role is still a strong fit). "
                      "Be concise and practical. Plain text, start each line with '- '.")
            try:
                rr = call_deepseek(ds_key,
                    {'model': 'deepseek-chat', 'temperature': 0.3, 'max_tokens': 400,
                          'messages': [{'role': 'user', 'content': prompt}]},
                    timeout=60, endpoint='reasoning')
                if rr.status_code == 200:
                    reasoning = rr.json()['choices'][0]['message']['content'].strip()
            except Exception:
                pass

    conn.close()

    total_ms = int((_time.perf_counter() - t_start) * 1000)
    timing['total_ms'] = total_ms
    avg_sim = round(sum(s for s, _ in page_slice) / len(page_slice), 4) if page_slice else 0.0
    _search_metric_record(total_ms, scanned)

    # Capture the search into the Recruitment Memory Engine — only on a fresh
    # first page, so scrolling/pagination doesn't spam duplicate events.
    if hybrid_on and page == 1 and not from_cache:
        _rme_capture_search(query, filters_detected, total, current_user())

    # slow-search + per-search logging
    if total_ms >= _search_cfg('search_slow_ms'):
        print(f'[search] SLOW {total_ms}ms q="{query[:40]}" scanned={scanned} '
              f'returned={len(results)} hybrid={hybrid_on} cache={"hit" if from_cache else "miss"} {timing}')
    else:
        print(f'[search] {total_ms}ms q="{query[:40]}" scanned={scanned} '
              f'returned={len(results)} hybrid={hybrid_on} cache={"hit" if from_cache else "miss"}')

    return jsonify({
        'ok': True,
        'results': results,
        'reasoning': reasoning,
        'searched': scanned,       # backward-compatible key (candidates scanned)
        'total': total,            # total ranked (pagination pool)
        'page': page, 'page_size': page_size,
        'has_more': offset + page_size < total,
        'avg_similarity': avg_sim,
        'from_cache': from_cache,
        'hybrid': hybrid_on,
        'multivector': mv_on,
        'reranked': reranked,
        'filters_detected': filters_detected,
        'timing': timing,
    })


@app.route('/api/ai/search/metrics', methods=['GET'])
@login_required
def ai_search_metrics():
    """Rolling search performance metrics (in-memory, last N searches)."""
    with _SEARCH_METRICS_LOCK:
        durs = sorted(x[0] for x in _SEARCH_METRICS)
        scans = [x[1] for x in _SEARCH_METRICS]
    if not durs:
        return jsonify({'ok': True, 'samples': 0})
    def _pct(a, p):
        if not a: return 0
        i = min(len(a) - 1, int(round((p / 100.0) * (len(a) - 1))))
        return a[i]
    return jsonify({'ok': True, 'samples': len(durs),
                    'avg_ms': int(sum(durs) / len(durs)),
                    'p95_ms': _pct(durs, 95),
                    'max_ms': durs[-1],
                    'avg_candidates_scanned': int(sum(scans) / len(scans)) if scans else 0})


@app.route('/api/ai/vectors/status', methods=['GET'])
@login_required
def ai_vectors_status():
    """Multi-vector (facet) coverage + config. Confirms the architecture is live."""
    conn = get_db()
    by_facet = {}
    try:
        for r in conn.execute("SELECT facet, status, COUNT(*) n FROM candidate_vectors GROUP BY facet, status"):
            by_facet.setdefault(r['facet'], {})[r['status']] = r['n']
        fully = conn.execute(
            "SELECT COUNT(*) n FROM (SELECT candidate_id FROM candidate_vectors "
            "WHERE status='completed' AND embedding_text_version=? "
            "GROUP BY candidate_id HAVING COUNT(DISTINCT facet) >= ?)",
            (MV_TEXT_TEMPLATE, len(MV_FACETS))).fetchone()['n']
        total_c = conn.execute("SELECT COUNT(*) n FROM candidates WHERE embedding_status='completed'").fetchone()['n']
    except sqlite3.OperationalError:
        fully = total_c = 0
    conn.close()
    return jsonify({'ok': True, 'enabled': bool(_mv_cfg('multivector_enabled')),
                    'facets': MV_FACETS, 'template': MV_TEXT_TEMPLATE,
                    'candidates_fully_faceted': fully, 'candidates_embedded': total_c,
                    'by_facet': by_facet,
                    'weights': {'full': _mv_cfg('multivector_w_full'),
                                'facet': _mv_cfg('multivector_w_facet')}})


@app.route('/api/ai/vectors/rebuild', methods=['POST'])
@login_required
def ai_vectors_rebuild():
    """Manually generate facet vectors for candidates that need them (bounded).
    Explicit intent, so it runs even if multivector_enabled is off — but it still
    needs a Gemini key. Use `cap` to limit how many candidates per call."""
    d = request.json or {}
    cap = min(int(d.get('cap') or 50), 500)
    api_key = get_setting('gemini_api_key')
    if not api_key:
        return jsonify({'error': 'Gemini API key not set. Add it in Settings.'}), 400
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT c.* FROM candidates c WHERE c.embedding_status='completed' AND c.id NOT IN ("
            "  SELECT candidate_id FROM candidate_vectors WHERE embedding_text_version=? "
            "  GROUP BY candidate_id HAVING COUNT(DISTINCT facet) >= ?"
            ") LIMIT ?", (MV_TEXT_TEMPLATE, len(MV_FACETS), cap)).fetchall()
        done = 0
        for c in rows:
            embed_candidate_facets(conn, c, api_key)
            done += 1
    finally:
        conn.close()
    return jsonify({'ok': True, 'processed': done})


@app.route('/api/mandates/<int:mid>/match', methods=['GET'])
@login_required
def mandate_match_candidates(mid):
    """Rank candidates for this mandate using its STORED JD vector — no
    re-embedding. Reuses the Sprint-4 retrieval + Sprint-6 hydrate."""
    conn = get_db()
    jdvec = _mandate_jd_vector(conn, mid)
    if jdvec is None:
        conn.close()
        return jsonify({'ok': False, 'error': 'JD not embedded yet. It will be generated shortly.',
                        'pending': True, 'results': []})
    top_k = min(int(request.args.get('top_k') or 20), 100)
    min_score = float(request.args.get('min_score') or -1.0)
    pool = max(_search_cfg('search_max_results'), top_k)
    ranked, scanned = _search_rank(conn, jdvec, min_score, int(_search_cfg('search_max_candidates')), pool)
    results = _search_hydrate(conn, ranked[:top_k])
    conn.close()
    return jsonify({'ok': True, 'mandate_id': mid, 'searched': scanned,
                    'total': len(ranked), 'results': results})


@app.route('/api/candidates/<int:cid>/match-mandates', methods=['GET'])
@login_required
def candidate_match_mandates(cid):
    """Reverse match: given a candidate's vector, rank the open mandates whose
    stored JD vector best fits. Mandates are few, so this is a trivial scan."""
    conn = get_db()
    cr = conn.execute("SELECT embedding_vec FROM candidates WHERE id=?", (cid,)).fetchone()
    if not cr or not cr['embedding_vec']:
        conn.close(); return jsonify({'ok': False, 'error': 'Candidate not embedded yet', 'results': []})
    try:
        cvec = _np.frombuffer(cr['embedding_vec'], dtype=_np.float32) if _HAS_NUMPY else \
               (lambda a: (a.frombytes(cr['embedding_vec']) or a))(_array.array('f'))
    except Exception:
        conn.close(); return jsonify({'ok': False, 'error': 'Bad candidate vector', 'results': []})
    rows = conn.execute(
        "SELECT mv.mandate_id, mv.embedding_vec, m.role, m.client, m.location, m.status "
        "FROM mandate_vectors mv JOIN mandates m ON m.id=mv.mandate_id "
        "WHERE mv.status='completed' AND mv.embedding_vec IS NOT NULL AND m.owner_id=?",
        (effective_company_id(),)).fetchall()
    out = []
    for r in rows:
        try:
            jv = _np.frombuffer(r['embedding_vec'], dtype=_np.float32) if _HAS_NUMPY else \
                 (lambda a: (a.frombytes(r['embedding_vec']) or a))(_array.array('f'))
            sim = cosine(list(cvec), list(jv))
        except Exception:
            continue
        out.append({'mandate_id': r['mandate_id'], 'role': r['role'], 'client': r['client'],
                    'location': r['location'], 'status': r['status'], 'score': round(sim * 100, 1)})
    out.sort(key=lambda x: x['score'], reverse=True)
    conn.close()
    return jsonify({'ok': True, 'candidate_id': cid, 'results': out[:20]})


@app.route('/api/ai/jd/status', methods=['GET'])
@login_required
def ai_jd_status():
    conn = get_db()
    by_status = {}
    try:
        for r in conn.execute("SELECT status, COUNT(*) n FROM mandate_vectors GROUP BY status"):
            by_status[r['status']] = r['n']
        total_m = conn.execute("SELECT COUNT(*) n FROM mandates").fetchone()['n']
        done = conn.execute("SELECT COUNT(*) n FROM mandate_vectors WHERE status='completed' "
                            "AND embedding_text_version=?", (JD_TEXT_TEMPLATE,)).fetchone()['n']
    except sqlite3.OperationalError:
        total_m = done = 0
    conn.close()
    return jsonify({'ok': True, 'enabled': _jd_cfg_enabled(), 'template': JD_TEXT_TEMPLATE,
                    'mandates_total': total_m, 'mandates_embedded': done, 'by_status': by_status})


# ══════════════════════════════════════════════════════════════════════
#  RECRUITMENT MEMORY ENGINE (Sprint 5) — primitives + thin API.
#  These are the reusable write/read functions future sprints will call to
#  attach memories, log events and record relationships on any entity.
#  Nothing here auto-generates data; existing flows are untouched.
# ══════════════════════════════════════════════════════════════════════

# Canonical vocabularies (documentation + /status breakdown + light validation).
# New values are allowed (extensibility) — these are the known-good set.
RME_ENTITY_TYPES = ('candidate', 'job', 'client', 'company', 'recruiter', 'placement',
                    'interview', 'submission', 'communication', 'skill', 'technology', 'industry')
RME_MEMORY_TYPES = ('fact', 'event', 'relationship', 'observation', 'preference',
                    'ai_insight', 'recruiter_note', 'system_note', 'interaction')
RME_EVENT_TYPES = ('candidate_created', 'resume_uploaded', 'candidate_updated',
                   'interview_scheduled', 'interview_feedback', 'submission',
                   'offer_released', 'offer_accepted', 'placement', 'communication',
                   'status_change', 'search_activity')
RME_VISIBILITY = ('internal', 'team', 'private', 'candidate_visible')
RME_STATUS = ('active', 'archived', 'deleted')


def _rme_json(v, default):
    """Coerce a value to a JSON string for storage (accepts list/dict/str)."""
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        try:
            return json.dumps(v)
        except Exception:
            return default
    return str(v)


def rme_add_memory(conn, entity_type, entity_id, memory_type='fact', title='', content='',
                   source='system', created_by='', visibility='internal', confidence=1.0,
                   importance=0, tags=None, metadata=None, status='active'):
    """Attach a memory to any entity. Returns the new memory id. This is the
    core primitive future sprints (AI insights, recruiter notes) build on."""
    now = ts()
    cur = conn.execute(
        "INSERT INTO rme_memories (entity_type, entity_id, memory_type, title, content, source, "
        "created_by, created_at, updated_at, visibility, confidence, importance, tags, metadata, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(entity_type), str(entity_id), memory_type, title, content, source, str(created_by),
         now, now, visibility, float(confidence), int(importance),
         _rme_json(tags, '[]'), _rme_json(metadata, '{}'), status))
    conn.commit()
    return cur.lastrowid


def rme_add_event(conn, event_type, entity_type, entity_id, actor='system', summary='',
                  data=None, related_entity_type='', related_entity_id=''):
    """Append an immutable event to the RME timeline. Returns the event id."""
    cur = conn.execute(
        "INSERT INTO rme_events (event_type, entity_type, entity_id, actor, summary, data, "
        "related_entity_type, related_entity_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (str(event_type), str(entity_type), str(entity_id), str(actor), summary,
         _rme_json(data, '{}'), str(related_entity_type), str(related_entity_id), ts()))
    conn.commit()
    return cur.lastrowid


def rme_set_relationship(conn, from_type, from_id, to_type, to_id, rel_type,
                         weight=1.0, confidence=1.0, source='system', metadata=None):
    """Upsert a relationship edge (unique per from/to/rel_type). Returns row id."""
    now = ts()
    conn.execute(
        "INSERT INTO rme_relationships (from_type, from_id, to_type, to_id, rel_type, weight, "
        "confidence, source, metadata, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(from_type, from_id, to_type, to_id, rel_type) DO UPDATE SET "
        "weight=excluded.weight, confidence=excluded.confidence, source=excluded.source, "
        "metadata=excluded.metadata, updated_at=excluded.updated_at, status='active'",
        (str(from_type), str(from_id), str(to_type), str(to_id), str(rel_type),
         float(weight), float(confidence), source, _rme_json(metadata, '{}'), now, now))
    conn.commit()
    row = conn.execute(
        "SELECT id FROM rme_relationships WHERE from_type=? AND from_id=? AND to_type=? "
        "AND to_id=? AND rel_type=?",
        (str(from_type), str(from_id), str(to_type), str(to_id), str(rel_type))).fetchone()
    return row['id'] if row else None


def _rme_row(r):
    return {k: r[k] for k in r.keys()}


@app.route('/api/rme/memory', methods=['GET', 'POST'])
@login_required
def rme_memory():
    if request.method == 'POST':
        d = request.json or {}
        et, eid = (d.get('entity_type') or '').strip(), str(d.get('entity_id') or '').strip()
        if not et or not eid:
            return jsonify({'error': 'entity_type and entity_id are required'}), 400
        conn = get_db()
        mid = rme_add_memory(
            conn, et, eid,
            memory_type=(d.get('memory_type') or 'fact'),
            title=d.get('title', ''), content=d.get('content', ''),
            source=d.get('source', 'recruiter'),
            created_by=str((current_user() or {}).get('username', '')),
            visibility=d.get('visibility', 'internal'),
            confidence=d.get('confidence', 1.0), importance=d.get('importance', 0),
            tags=d.get('tags'), metadata=d.get('metadata'))
        conn.close()
        return jsonify({'ok': True, 'id': mid})
    # GET — list memories for an entity
    et = (request.args.get('entity_type') or '').strip()
    eid = str(request.args.get('entity_id') or '').strip()
    if not et or not eid:
        return jsonify({'error': 'entity_type and entity_id are required'}), 400
    limit = min(int(request.args.get('limit') or 100), 500)
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM rme_memories WHERE entity_type=? AND entity_id=? AND status!='deleted' "
        "ORDER BY importance DESC, created_at DESC LIMIT ?", (et, eid, limit)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'memories': [_rme_row(r) for r in rows]})


@app.route('/api/rme/memory/<int:mid>/archive', methods=['POST'])
@login_required
def rme_memory_archive(mid):
    conn = get_db()
    conn.execute("UPDATE rme_memories SET status='archived', updated_at=? WHERE id=?", (ts(), mid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/rme/event', methods=['GET', 'POST'])
@login_required
def rme_event():
    if request.method == 'POST':
        d = request.json or {}
        etype = (d.get('event_type') or '').strip()
        et, eid = (d.get('entity_type') or '').strip(), str(d.get('entity_id') or '').strip()
        if not etype or not et or not eid:
            return jsonify({'error': 'event_type, entity_type and entity_id are required'}), 400
        conn = get_db()
        evid = rme_add_event(conn, etype, et, eid,
                             actor=str((current_user() or {}).get('username', 'system')),
                             summary=d.get('summary', ''), data=d.get('data'),
                             related_entity_type=d.get('related_entity_type', ''),
                             related_entity_id=d.get('related_entity_id', ''))
        conn.close()
        return jsonify({'ok': True, 'id': evid})
    et = (request.args.get('entity_type') or '').strip()
    eid = str(request.args.get('entity_id') or '').strip()
    limit = min(int(request.args.get('limit') or 100), 500)
    conn = get_db()
    if et and eid:
        rows = conn.execute("SELECT * FROM rme_events WHERE entity_type=? AND entity_id=? "
                            "ORDER BY created_at DESC, id DESC LIMIT ?", (et, eid, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM rme_events ORDER BY created_at DESC, id DESC LIMIT ?",
                            (limit,)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'events': [_rme_row(r) for r in rows]})


@app.route('/api/rme/relationship', methods=['GET', 'POST'])
@login_required
def rme_relationship():
    if request.method == 'POST':
        d = request.json or {}
        req = ['from_type', 'from_id', 'to_type', 'to_id', 'rel_type']
        if not all(str(d.get(k) or '').strip() for k in req):
            return jsonify({'error': 'from_type, from_id, to_type, to_id, rel_type are required'}), 400
        conn = get_db()
        rid = rme_set_relationship(conn, d['from_type'], d['from_id'], d['to_type'], d['to_id'],
                                   d['rel_type'], weight=d.get('weight', 1.0),
                                   confidence=d.get('confidence', 1.0),
                                   source=d.get('source', 'recruiter'), metadata=d.get('metadata'))
        conn.close()
        return jsonify({'ok': True, 'id': rid})
    ft = (request.args.get('from_type') or '').strip()
    fid = str(request.args.get('from_id') or '').strip()
    limit = min(int(request.args.get('limit') or 200), 1000)
    conn = get_db()
    if ft and fid:
        rows = conn.execute("SELECT * FROM rme_relationships WHERE from_type=? AND from_id=? "
                            "AND status='active' ORDER BY weight DESC LIMIT ?", (ft, fid, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM rme_relationships WHERE status='active' "
                            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'relationships': [_rme_row(r) for r in rows]})


@app.route('/api/rme/status', methods=['GET'])
@login_required
def rme_status():
    """Confirms the RME architecture is live and reports counts. Useful to
    verify the foundation without any memories having been generated yet."""
    conn = get_db()
    def _count(t):
        try:
            return conn.execute(f'SELECT COUNT(*) n FROM {t}').fetchone()['n']
        except sqlite3.OperationalError:
            return None
    mem_by_type, evt_by_type = {}, {}
    try:
        for r in conn.execute("SELECT memory_type m, COUNT(*) n FROM rme_memories GROUP BY m"):
            mem_by_type[r['m']] = r['n']
        for r in conn.execute("SELECT event_type e, COUNT(*) n FROM rme_events GROUP BY e"):
            evt_by_type[r['e']] = r['n']
    except sqlite3.OperationalError:
        pass
    out = {'ok': True,
           'memories': _count('rme_memories'),
           'events': _count('rme_events'),
           'relationships': _count('rme_relationships'),
           'memories_by_type': mem_by_type,
           'events_by_type': evt_by_type,
           'vocab': {'entity_types': RME_ENTITY_TYPES, 'memory_types': RME_MEMORY_TYPES,
                     'event_types': RME_EVENT_TYPES}}
    conn.close()
    return jsonify(out)


# ══════════════════════════════════════════════════════════════════════
#  RECRUITMENT KNOWLEDGE GRAPH (Sprint 7) — repository layer + interfaces.
#  Everything the rest of RecruitOS touches goes through these rkg_* helpers,
#  never raw SQL — that abstraction is what lets a graph DB replace SQLite
#  later without changing callers. Architecture only: no extraction, no
#  reasoning, no recommendations.
# ══════════════════════════════════════════════════════════════════════

RKG_ENTITY_TYPES = ('candidate', 'company', 'job', 'client', 'recruiter', 'skill', 'technology',
                    'certification', 'industry', 'product', 'location', 'education',
                    'institution', 'project', 'role', 'designation')
RKG_REL_TYPES = (
    # candidate
    'WORKED_AT', 'HAS_SKILL', 'HAS_CERTIFICATION', 'KNOWS_TECHNOLOGY', 'WORKED_ON',
    'LOCATED_IN', 'HAS_EDUCATION', 'REPORTS_TO', 'REFERRED_BY',
    # company
    'OPERATES_IN', 'COMPETES_WITH', 'USES_TECHNOLOGY', 'MANUFACTURES', 'HIRES_FOR',
    'SUPPLIES', 'PARTNERS_WITH',
    # job
    'REQUIRES_SKILL', 'REQUIRES_CERTIFICATION', 'REQUIRES_TECHNOLOGY',
    'BELONGS_TO_INDUSTRY', 'POSTED_BY',
    # recruiter
    'MANAGES', 'PLACED', 'WORKS_WITH', 'SEARCHED', 'CONTACTED',
    # generic concept graph
    'RELATED_TO',
)

# Seed alias map — small on purpose (architecture, not a big dictionary).
# Maps a canonical normalized key -> known surface variants. Extend freely.
_RKG_ALIAS_SEED = {
    'motorcontrolcentre': ['mcc', 'motor control center', 'motor control centre', 'intelligent mcc'],
    'powercontrolcentre': ['pcc', 'power control center', 'power control centre'],
    'lowvoltage':         ['lv', 'l v', 'low voltage'],
    'iec61439':           ['iec 61439', 'iec61439'],
}


def rkg_normalize(name):
    """Canonical dedup key: lowercase, punctuation/separators removed, spaces
    stripped. Collapses spacing/punctuation variants of code-like terms, e.g.
    'IEC 61439' / 'I.E.C-61439' / 'iec61439' -> 'iec61439'. Abbreviation and
    spelling variants (MCC, centre/center) are handled by the alias layer."""
    s = (name or '').lower().strip()
    s = re.sub(r'[^a-z0-9]+', '', s)
    return s


def _rkg_canonical_norm(name):
    """Resolve a surface form to its canonical normalized key via the seed alias
    map, falling back to the plain normalized form. Returns (canonical, norm)."""
    norm = rkg_normalize(name)
    if not norm:
        return '', ''
    if norm in _RKG_ALIAS_SEED:
        return norm, norm
    for canon, variants in _RKG_ALIAS_SEED.items():
        if norm == canon or any(rkg_normalize(v) == norm for v in variants):
            return canon, norm
    return norm, norm


def _rkg_register_alias(conn, entity_id, entity_type, surface):
    """Record a surface form -> entity mapping so later lookups of this alias
    resolve to the same canonical node. Idempotent; ignores conflicts."""
    na = rkg_normalize(surface)
    if not na:
        return
    try:
        conn.execute("INSERT OR IGNORE INTO rkg_aliases (entity_id, entity_type, normalized_alias, created_at) "
                     "VALUES (?,?,?,?)", (entity_id, entity_type, na, ts()))
    except sqlite3.OperationalError:
        pass


def _rkg_lookup(conn, entity_type, name):
    """Resolve a surface form to an existing entity id, trying (1) canonical
    normalized_name, then (2) the learned alias table. Returns id or None."""
    canon, norm = _rkg_canonical_norm(name)
    if not canon:
        return None
    row = conn.execute("SELECT id FROM rkg_entities WHERE entity_type=? AND normalized_name=?",
                       (entity_type, canon)).fetchone()
    if row:
        return row['id']
    try:
        arow = conn.execute("SELECT entity_id FROM rkg_aliases WHERE entity_type=? AND normalized_alias=?",
                            (entity_type, norm)).fetchone()
        if arow:
            return arow['entity_id']
    except sqlite3.OperationalError:
        pass
    return None


def rkg_get_or_create_entity(conn, entity_type, name, description='', metadata=None, source='system'):
    """Idempotently resolve/create a canonical entity. Same concept under a
    different surface form returns the SAME id (and records the new alias).
    This is THE entry point for putting anything into the graph."""
    display = (name or '').strip()
    if not display:
        return None
    canon, norm = _rkg_canonical_norm(display)
    if not canon:
        return None
    eid = _rkg_lookup(conn, entity_type, display)
    if eid:
        # record a newly-seen surface form (on the entity + alias table)
        row = conn.execute("SELECT aliases FROM rkg_entities WHERE id=?", (eid,)).fetchone()
        try:
            al = json.loads(row['aliases'] or '[]') if row else []
        except Exception:
            al = []
        if display.lower() not in [a.lower() for a in al]:
            al.append(display)
            conn.execute("UPDATE rkg_entities SET aliases=?, updated_at=? WHERE id=?",
                         (json.dumps(al), ts(), eid))
        _rkg_register_alias(conn, eid, entity_type, display)
        conn.commit()
        return eid
    now = ts()
    cur = conn.execute(
        "INSERT INTO rkg_entities (entity_type, display_name, normalized_name, aliases, description, "
        "metadata, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (entity_type, display, canon, json.dumps([display]), description,
         _rme_json(metadata, '{}'), now, now))
    eid = cur.lastrowid
    # register both the canonical form and the surface form as aliases
    _rkg_register_alias(conn, eid, entity_type, display)
    _rkg_register_alias(conn, eid, entity_type, canon)
    conn.commit()
    return eid


def rkg_add_alias(conn, entity_id, alias):
    row = conn.execute("SELECT entity_type, aliases FROM rkg_entities WHERE id=?", (entity_id,)).fetchone()
    if not row:
        return False
    try:
        al = json.loads(row['aliases'] or '[]')
    except Exception:
        al = []
    if alias and alias.lower() not in [a.lower() for a in al]:
        al.append(alias)
        conn.execute("UPDATE rkg_entities SET aliases=?, updated_at=? WHERE id=?",
                     (json.dumps(al), ts(), entity_id))
    # register in the resolution table so this alias now resolves to the node
    _rkg_register_alias(conn, entity_id, row['entity_type'], alias)
    conn.commit()
    return True


def rkg_find_entity(conn, entity_type, name):
    eid = _rkg_lookup(conn, entity_type, name)
    if not eid:
        return None
    row = conn.execute("SELECT * FROM rkg_entities WHERE id=?", (eid,)).fetchone()
    return dict(row) if row else None


def rkg_link(conn, source_id, target_id, rel_type, confidence=1.0, source='system', metadata=None):
    """Upsert a directed edge between two entities (unique per src/tgt/rel_type)."""
    now = ts()
    conn.execute(
        "INSERT INTO rkg_edges (source_id, target_id, rel_type, confidence, source, metadata, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source_id, target_id, rel_type) DO UPDATE SET "
        "confidence=excluded.confidence, source=excluded.source, metadata=excluded.metadata, "
        "updated_at=excluded.updated_at, status='active'",
        (source_id, target_id, str(rel_type), float(confidence), source,
         _rme_json(metadata, '{}'), now, now))
    conn.commit()
    row = conn.execute("SELECT id FROM rkg_edges WHERE source_id=? AND target_id=? AND rel_type=?",
                       (source_id, target_id, str(rel_type))).fetchone()
    return row['id'] if row else None


def rkg_neighbors(conn, entity_id, rel_type=None, direction='out', limit=200):
    """Traverse one hop. direction: 'out' (entity is source), 'in' (target),
    'both'. Returns connected entities with the edge type + confidence."""
    out = []
    def _q(join_on, other):
        sql = (f"SELECT e.*, k.rel_type AS _rel, k.confidence AS _conf, k.id AS _edge_id "
               f"FROM rkg_edges k JOIN rkg_entities e ON e.id=k.{other} "
               f"WHERE k.{join_on}=? AND k.status='active'")
        args = [entity_id]
        if rel_type:
            sql += " AND k.rel_type=?"; args.append(rel_type)
        sql += " LIMIT ?"; args.append(limit)
        for r in conn.execute(sql, args).fetchall():
            d = dict(r); d['_direction'] = ('out' if join_on == 'source_id' else 'in')
            out.append(d)
    if direction in ('out', 'both'):
        _q('source_id', 'target_id')
    if direction in ('in', 'both'):
        _q('target_id', 'source_id')
    return out


# ── Memory Engine integration ──────────────────────────────────────────
# A graph node can carry a memory timeline via the existing RME (Sprint 5),
# addressed as entity_type='rkg_entity'. No new memory storage needed.
def rkg_attach_memory(conn, entity_id, memory_type='fact', title='', content='',
                      source='system', created_by='', confidence=1.0, importance=0,
                      tags=None, metadata=None):
    return rme_add_memory(conn, 'rkg_entity', str(entity_id), memory_type=memory_type,
                          title=title, content=content, source=source, created_by=created_by,
                          confidence=confidence, importance=importance, tags=tags, metadata=metadata)

def rkg_memories(conn, entity_id, limit=100):
    rows = conn.execute("SELECT * FROM rme_memories WHERE entity_type='rkg_entity' AND entity_id=? "
                        "AND status!='deleted' ORDER BY importance DESC, created_at DESC LIMIT ?",
                        (str(entity_id), limit)).fetchall()
    return [_rme_row(r) for r in rows]


# ── Knowledge-extraction interface (DESIGN ONLY — no implementation) ────
# Future sprints register extractors that turn raw text (resume, JD, notes,
# email, WhatsApp) into (entity, relationship) candidates. The registry +
# signature are defined now so callers and extractors can be built against a
# stable contract; the default extractor intentionally returns nothing.
_RKG_EXTRACTORS = {}
def rkg_register_extractor(source_type, fn):
    """Register a callable fn(text, context)->{'entities':[...],'edges':[...]}."""
    _RKG_EXTRACTORS[source_type] = fn

def rkg_extract(source_type, text, context=None):
    """Dispatch to a registered extractor. No-op until extractors are added in a
    later sprint (architecture only)."""
    fn = _RKG_EXTRACTORS.get(source_type)
    if not fn:
        return {'entities': [], 'edges': [], 'implemented': False}
    try:
        return fn(text, context or {})
    except Exception as e:
        return {'entities': [], 'edges': [], 'error': str(e)}


# ── Search integration interface (exposed, NOT wired into search yet) ───
def rkg_resolve_terms(conn, terms):
    """Map query terms to canonical entities + their aliases. Hybrid search can
    call this later for synonym expansion; search behaviour is unchanged now."""
    resolved = []
    for t in (terms or []):
        # resolve type-agnostically: direct canonical match, then learned alias
        canon, norm = _rkg_canonical_norm(t)
        row = conn.execute("SELECT id, entity_type, display_name, aliases FROM rkg_entities "
                           "WHERE normalized_name=? LIMIT 1", (canon,)).fetchone()
        if not row:
            arow = conn.execute("SELECT entity_id FROM rkg_aliases WHERE normalized_alias=? LIMIT 1",
                               (norm,)).fetchone()
            if arow:
                row = conn.execute("SELECT id, entity_type, display_name, aliases FROM rkg_entities "
                                   "WHERE id=?", (arow['entity_id'],)).fetchone()
        if row:
            try:
                al = json.loads(row['aliases'] or '[]')
            except Exception:
                al = []
            resolved.append({'term': t, 'entity_id': row['id'], 'entity_type': row['entity_type'],
                             'canonical': row['display_name'], 'aliases': al})
        else:
            resolved.append({'term': t, 'entity_id': None, 'canonical': None, 'aliases': []})
    return resolved


# ── Thin API (additive, login_required) ────────────────────────────────
@app.route('/api/rkg/entity', methods=['GET', 'POST'])
@login_required
def rkg_entity():
    if request.method == 'POST':
        d = request.json or {}
        et, name = (d.get('entity_type') or '').strip(), (d.get('name') or '').strip()
        if not et or not name:
            return jsonify({'error': 'entity_type and name are required'}), 400
        conn = get_db()
        eid = rkg_get_or_create_entity(conn, et, name, description=d.get('description', ''),
                                       metadata=d.get('metadata'), source=d.get('source', 'recruiter'))
        row = conn.execute("SELECT * FROM rkg_entities WHERE id=?", (eid,)).fetchone()
        conn.close()
        return jsonify({'ok': True, 'id': eid, 'entity': dict(row) if row else None})
    conn = get_db()
    if request.args.get('id'):
        row = conn.execute("SELECT * FROM rkg_entities WHERE id=?", (request.args.get('id'),)).fetchone()
        conn.close()
        return jsonify({'ok': True, 'entity': dict(row) if row else None})
    et = (request.args.get('entity_type') or '').strip()
    name = (request.args.get('name') or '').strip()
    if et and name:
        ent = rkg_find_entity(conn, et, name); conn.close()
        return jsonify({'ok': True, 'entity': ent})
    q = "SELECT * FROM rkg_entities WHERE status='active'"
    args = []
    if et:
        q += " AND entity_type=?"; args.append(et)
    q += " ORDER BY id DESC LIMIT ?"; args.append(min(int(request.args.get('limit') or 100), 500))
    rows = conn.execute(q, args).fetchall(); conn.close()
    return jsonify({'ok': True, 'entities': [dict(r) for r in rows]})


@app.route('/api/rkg/entity/<int:eid>/alias', methods=['POST'])
@login_required
def rkg_entity_alias(eid):
    d = request.json or {}
    conn = get_db(); ok = rkg_add_alias(conn, eid, (d.get('alias') or '').strip()); conn.close()
    return jsonify({'ok': ok})


@app.route('/api/rkg/edge', methods=['POST'])
@login_required
def rkg_edge():
    d = request.json or {}
    for k in ('source_id', 'target_id', 'rel_type'):
        if not str(d.get(k) or '').strip():
            return jsonify({'error': 'source_id, target_id and rel_type are required'}), 400
    conn = get_db()
    rid = rkg_link(conn, int(d['source_id']), int(d['target_id']), d['rel_type'],
                   confidence=d.get('confidence', 1.0), source=d.get('source', 'recruiter'),
                   metadata=d.get('metadata'))
    conn.close()
    return jsonify({'ok': True, 'id': rid})


@app.route('/api/rkg/neighbors', methods=['GET'])
@login_required
def rkg_neighbors_api():
    eid = request.args.get('entity_id')
    if not eid:
        return jsonify({'error': 'entity_id is required'}), 400
    conn = get_db()
    nb = rkg_neighbors(conn, int(eid), rel_type=request.args.get('rel_type'),
                       direction=request.args.get('direction', 'out'))
    conn.close()
    return jsonify({'ok': True, 'neighbors': nb})


@app.route('/api/rkg/status', methods=['GET'])
@login_required
def rkg_status():
    conn = get_db()
    def _c(t):
        try:
            return conn.execute(f'SELECT COUNT(*) n FROM {t}').fetchone()['n']
        except sqlite3.OperationalError:
            return None
    by_type = {}
    try:
        for r in conn.execute("SELECT entity_type t, COUNT(*) n FROM rkg_entities GROUP BY t"):
            by_type[r['t']] = r['n']
    except sqlite3.OperationalError:
        pass
    out = {'ok': True, 'entities': _c('rkg_entities'), 'edges': _c('rkg_edges'),
           'entities_by_type': by_type,
           'vocab': {'entity_types': RKG_ENTITY_TYPES, 'rel_types': RKG_REL_TYPES}}
    conn.close()
    return jsonify(out)


@app.route('/api/ai/stats', methods=['POST'])
@login_required
def ai_stats():
    """Approach B: answer counting/analytics questions. We pull a compact,
    structured snapshot of all candidates+mandates and let DeepSeek reason
    over it (no embeddings needed). Good for 'how many', 'which', 'average'."""
    d = request.json or {}
    question = (d.get('question') or '').strip()
    if not question:
        return jsonify({'error': 'Empty question'}), 400
    ds_key = get_setting('deepseek_api_key')
    if not ds_key:
        return jsonify({'error': 'DeepSeek API key not set. Add it in Settings.'}), 400

    conn = get_db()
    mandates = conn.execute('SELECT id, role, client, location, status, ctc_min, ctc_max FROM mandates').fetchall()
    cands = conn.execute('SELECT name, company, designation, experience, ctc_current, '
                         'ctc_expected, notice_period, location, key_skills, stage, mandate_id '
                         'FROM candidates').fetchall()
    conn.close()

    m_lines = []
    for m in mandates:
        m_lines.append(f"Mandate#{m['id']}: {m['role']} @ {m['client']} | loc:{m['location']} | "
                       f"status:{m['status']} | CTC {m['ctc_min']}-{m['ctc_max']}L")
    c_lines = []
    for c in cands:
        try:
            sk = ', '.join(json.loads(c['key_skills'] or '[]')[:6])
        except Exception:
            sk = ''
        c_lines.append(f"{c['name']} | {c['designation']} @ {c['company']} | exp:{c['experience']}y | "
                       f"CTC cur:{c['ctc_current']} exp:{c['ctc_expected']} | NP:{c['notice_period']}d | "
                       f"loc:{c['location']} | stage:{c['stage']} | mandate:{c['mandate_id']} | skills:{sk}")

    # Keep prompt within limits; if too many candidates, note truncation
    MAX = 400
    truncated = len(c_lines) > MAX
    snapshot = ("MANDATES:\n" + '\n'.join(m_lines) + "\n\nCANDIDATES (" +
                str(len(c_lines)) + " total" + (", showing first 400" if truncated else "") + "):\n" +
                '\n'.join(c_lines[:MAX]))

    prompt = ("You are a recruitment data analyst for HireLab. Answer the recruiter's "
              "question using ONLY the data snapshot below. Be precise with numbers, "
              "and list relevant candidate names when useful. If the data is truncated "
              "and you can't be exact, say so. Keep it concise and practical.\n\n"
              "DATA:\n" + snapshot + "\n\nQUESTION: " + question + "\n\nANSWER:")

    try:
        rr = call_deepseek(ds_key,
            {'model': 'deepseek-chat', 'temperature': 0.2, 'max_tokens': 800,
                  'messages': [{'role': 'user', 'content': prompt}]},
            timeout=90, endpoint='analysis')
        if rr.status_code != 200:
            err = rr.json().get('error', {}).get('message', rr.text[:200])
            return jsonify({'error': 'DeepSeek error: ' + err}), 500
        answer = rr.json()['choices'][0]['message']['content'].strip()
        return jsonify({'ok': True, 'answer': answer, 'candidate_count': len(c_lines),
                        'truncated': truncated})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tags/<tag_type>')
def get_tag_suggestions(tag_type):
    """Return distinct tags used across all candidates for autocomplete.
    tag_type: 'product' (Product Handles) or 'function' (Function)
    """
    col_map = {'product': 'product_handles', 'function': 'function_tags', 'status': 'status_tags'}
    col = col_map.get(tag_type)
    if not col:
        return jsonify({'ok': False, 'error': 'Invalid tag type'}), 400

    defaults_map = {
        'product': [
            'Electrical Wires & Cables', 'Switches & Sockets', 'Low Voltage Switchgear',
            'Medium Voltage Switchgear', 'Circuit Breakers', 'Distribution Boards',
            'Lighting', 'Solar Inverters', 'Solar Panels', 'Energy Storage / BESS',
            'Transformers', 'Motors & Drives', 'Automation & Controls', 'Cable Management',
            'HVAC', 'Building Management Systems', 'EV Charging', 'Renewable Energy',
            'Industrial Automation', 'Power Distribution', 'Wiring Devices', 'MCCBs', 'ACBs',
            'Busbar Systems', 'UPS Systems', 'Genset / DG Sets'
        ],
        'function': [
            'Sales', 'Marketing', 'Business Development', 'Channel Sales',
            'Key Account Management', 'Product Management', 'Pre-Sales',
            'Technical Sales', 'Operations', 'Supply Chain', 'Procurement',
            'Project Management', 'Engineering', 'R&D', 'Quality',
            'Service & Support', 'After Sales Service', 'Finance', 'HR',
            'General Management', 'Strategy', 'Application Engineering', 'Design'
        ]
    }

    conn = get_db()
    rows = conn.execute(f'SELECT {col} FROM candidates WHERE {col} IS NOT NULL AND {col} != "" AND {col} != "[]"').fetchall()
    conn.close()

    used = set()
    for r in rows:
        try:
            for t in json.loads(r[col] or '[]'):
                if t and t.strip():
                    used.add(t.strip())
        except Exception:
            pass

    all_tags = sorted(used) + [d for d in defaults_map.get(tag_type, []) if d not in used]
    return jsonify({'ok': True, 'tags': all_tags})


@app.route('/api/candidates/<int:cid>/tags', methods=['POST'])
def save_candidate_tags(cid):
    """Save Product Handles or Function tags for a candidate."""
    d = request.json or {}
    tag_type = d.get('tag_type')
    tags = d.get('tags', [])
    col_map = {'product': 'product_handles', 'function': 'function_tags', 'status': 'status_tags'}
    col = col_map.get(tag_type)
    if not col:
        return jsonify({'ok': False, 'error': 'Invalid tag type'}), 400
    if not isinstance(tags, list):
        return jsonify({'ok': False, 'error': 'tags must be a list'}), 400
    tags = [str(t).strip() for t in tags if str(t).strip()]
    conn = get_db()
    conn.execute(f'UPDATE candidates SET {col}=?, updated_at=? WHERE id=?', (json.dumps(tags), ts(), cid))
    conn.commit(); conn.close()
    if tag_type == 'status' and tags:
        log_candidate_event(cid, 'tag', 'Tag updated — ' + ', '.join(tags))
    return jsonify({'ok': True, 'tags': tags})

@app.route('/api/health')
def health():
    return jsonify({'ok': True, 'data_dir': DATA_DIR, 'db': DB_PATH})

# Settings
@app.route('/api/settings', methods=['GET'])
def get_settings():
    conn = get_db()
    rows = conn.execute('SELECT key,value FROM settings').fetchall()
    out = {r['key']: r['value'] for r in rows}
    # Overlay this company's own per-tenant settings on top of the global defaults.
    cid = _safe_company_id()
    if cid:
        trows = conn.execute('SELECT key,value FROM tenant_settings WHERE company_id=?', (cid,)).fetchall()
        for r in trows:
            out[r['key']] = r['value']
    conn.close()
    # Ensure workflow_mode is always present so the UI can branch on it.
    if 'workflow_mode' not in out or not out.get('workflow_mode'):
        out['workflow_mode'] = 'agency'
    return jsonify(out)

@app.route('/api/settings', methods=['POST'])
def save_settings():
    admin = is_admin()
    for k, v in (request.json or {}).items():
        # Global billing config can only be changed by the platform owner,
        # so an agency cannot set its own price/GST.
        if k.startswith('billing_') and not admin:
            continue
        set_setting(k, v)   # routes per-tenant keys automatically
    return jsonify({'ok': True})

# Mandates
@app.route('/api/mandates', methods=['GET'])
@login_required
def list_mandates():
    conn = get_db()
    if is_company_admin():
        rows = conn.execute('SELECT * FROM mandates WHERE owner_id=? ORDER BY created_at DESC',
                            (effective_company_id(),)).fetchall()
    else:
        # Recruiter: only mandates assigned to them within their company.
        rows = conn.execute('SELECT * FROM mandates WHERE owner_id=? AND assigned_user_id=? ORDER BY created_at DESC',
                            (effective_company_id(), real_user_id())).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/mandates/<int:mid>/client-notes', methods=['GET'])
@login_required
def get_client_notes(mid):
    conn = get_db()
    m = conn.execute('SELECT owner_id FROM mandates WHERE id=?', (mid,)).fetchone()
    if not m or m['owner_id'] != effective_company_id():
        conn.close(); return jsonify({'error': 'Mandate not found'}), 404
    rows = conn.execute(
        'SELECT n.id, n.note, n.created_at, n.created_by, u.display_name AS author '
        'FROM mandate_client_notes n LEFT JOIN users u ON u.id=n.created_by '
        'WHERE n.mandate_id=? AND n.is_active=1 ORDER BY n.created_at DESC',
        (mid,)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'notes': [dict(r) for r in rows]})


@app.route('/api/mandates/<int:mid>/client-notes', methods=['POST'])
@login_required
def add_client_note(mid):
    d = request.json or {}
    note = (d.get('note') or '').strip()
    if not note:
        return jsonify({'error': 'Note text required'}), 400
    conn = get_db()
    m = conn.execute('SELECT owner_id FROM mandates WHERE id=?', (mid,)).fetchone()
    if not m or m['owner_id'] != effective_company_id():
        conn.close(); return jsonify({'error': 'Mandate not found'}), 404
    conn.execute(
        'INSERT INTO mandate_client_notes (mandate_id, owner_id, note, created_by, created_at, is_active) '
        'VALUES (?,?,?,?,?,1)',
        (mid, effective_company_id(), note, real_user_id(), ts()))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/mandates/<int:mid>/client-notes/<int:nid>', methods=['DELETE'])
@login_required
def delete_client_note(mid, nid):
    conn = get_db()
    m = conn.execute('SELECT owner_id FROM mandates WHERE id=?', (mid,)).fetchone()
    if not m or m['owner_id'] != effective_company_id():
        conn.close(); return jsonify({'error': 'Mandate not found'}), 404
    conn.execute('UPDATE mandate_client_notes SET is_active=0 WHERE id=? AND mandate_id=?', (nid, mid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/mandates', methods=['POST'])
@login_required
def create_mandate():
    d = request.json or {}
    if not d.get('client') or not d.get('role'):
        return jsonify({'error': 'Client and Role required'}), 400
    conn = get_db(); c = conn.cursor()
    # Resolve CRM client link (Option B). If a crm_client_id is passed, use it.
    # Otherwise try to auto-match by normalised name so existing CRM clients link up.
    crm_client_id = int(d.get('crm_client_id') or 0)
    if not crm_client_id and d.get('client'):
        try:
            crm_client_id = _match_crm_client_by_name(conn, effective_company_id(), d['client'])
        except Exception:
            crm_client_id = 0
    c.execute('INSERT INTO mandates (client,role,location,division,ctc_min,ctc_max,jd,status,created_at,owner_id,assigned_user_id,crm_client_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
              (d['client'], d['role'], d.get('location',''), d.get('division',''),
               float(d.get('ctc_min', 0)), float(d.get('ctc_max', 0)), d.get('jd',''), 'active', ts(), effective_company_id(), real_user_id(), crm_client_id))
    mid = c.lastrowid; conn.commit(); conn.close()
    log_activity('create_mandate', d['role'] + ' @ ' + d['client'])
    return jsonify({'ok': True, 'id': mid, 'crm_client_id': crm_client_id})


def _norm_client_name(name):
    """Normalise a company name for fuzzy matching (mirror of CRM logic)."""
    import re as _re
    s = (name or '').lower().strip()
    # strip common suffixes and punctuation
    s = _re.sub(r'[.,&\-/()]', ' ', s)
    for suffix in ['private limited', 'pvt ltd', 'pvt. ltd', 'private ltd',
                   'limited', 'ltd', 'llp', 'inc', 'incorporated', 'corporation',
                   'corp', 'technologies', 'technology', 'solutions', 'services',
                   'india', 'pvt', 'and', 'company', 'co']:
        s = _re.sub(r'\b' + _re.escape(suffix) + r'\b', ' ', s)
    s = _re.sub(r'\s+', ' ', s).strip()
    return s


def _match_crm_client_by_name(conn, company_id, client_name):
    """Return crm_clients.id whose normalised name matches, else 0."""
    target = _norm_client_name(client_name)
    if not target:
        return 0
    rows = conn.execute(
        'SELECT id, name FROM crm_clients WHERE company_id=? AND is_active=1',
        (company_id,)).fetchall()
    for r in rows:
        if _norm_client_name(r['name']) == target:
            return r['id']
    return 0


@app.route('/api/mandates/<int:mid>/link-client', methods=['POST'])
@login_required
def link_mandate_client(mid):
    """Manually set (or clear) the CRM client a mandate belongs to."""
    d = request.json or {}
    crm_client_id = int(d.get('crm_client_id') or 0)
    conn = get_db()
    m = conn.execute('SELECT id, owner_id FROM mandates WHERE id=?', (mid,)).fetchone()
    if not m or m['owner_id'] != effective_company_id():
        conn.close(); return jsonify({'error': 'Mandate not found'}), 404
    # Validate the client belongs to this tenant (if non-zero)
    if crm_client_id:
        cl = conn.execute('SELECT id FROM crm_clients WHERE id=? AND company_id=? AND is_active=1',
                          (crm_client_id, effective_company_id())).fetchone()
        if not cl:
            conn.close(); return jsonify({'error': 'CRM client not found'}), 404
    conn.execute('UPDATE mandates SET crm_client_id=? WHERE id=?', (crm_client_id, mid))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'crm_client_id': crm_client_id})


@app.route('/api/crm-link/auto-map', methods=['POST'])
@login_required
def auto_map_mandates():
    """One-click: link every unlinked mandate to a CRM client by name match.
    Returns how many were mapped and which couldn't be matched."""
    conn = get_db()
    company_id = effective_company_id()
    mandates = conn.execute(
        'SELECT id, client, crm_client_id FROM mandates WHERE owner_id=?',
        (company_id,)).fetchall()
    mapped, unmatched = 0, []
    for m in mandates:
        if m['crm_client_id']:
            continue  # already linked
        cid = _match_crm_client_by_name(conn, company_id, m['client'])
        if cid:
            conn.execute('UPDATE mandates SET crm_client_id=? WHERE id=?', (cid, m['id']))
            mapped += 1
        else:
            if m['client'] and m['client'] not in unmatched:
                unmatched.append(m['client'])
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'mapped': mapped, 'unmatched': unmatched})


@app.route('/api/crm-link/client/<int:crm_client_id>/mandates', methods=['GET'])
@login_required
def client_mandates(crm_client_id):
    """List all mandates linked to a CRM client (for the client detail page)."""
    conn = get_db()
    company_id = effective_company_id()
    rows = conn.execute(
        'SELECT id, role, location, status, created_at FROM mandates '
        'WHERE owner_id=? AND crm_client_id=? ORDER BY created_at DESC',
        (company_id, crm_client_id)).fetchall()
    # Attach candidate counts
    out = []
    for r in rows:
        d = dict(r)
        d['candidate_count'] = conn.execute(
            'SELECT COUNT(*) n FROM candidates WHERE mandate_id=?', (r['id'],)).fetchone()['n']
        out.append(d)
    conn.close()
    return jsonify({'ok': True, 'mandates': out})

# ══════════════════════════════════════════════════════════════════════════
#  CRM — Email thread + AI-assisted drafting (Sprint 1)
#  Emails live as crm_activities rows (activity_type='email'); the meta JSON
#  carries direction / to / from / sent so the thread can render richly.
#  All three endpoints are additive and only read/write the existing table.
# ══════════════════════════════════════════════════════════════════════════
def _crm_client_or_none(conn, company_id, client_id):
    return conn.execute(
        'SELECT id, name, industry, website FROM crm_clients '
        'WHERE id=? AND company_id=? AND is_active=1',
        (client_id, company_id)).fetchone()

@app.route('/api/crm/clients/<int:cid>/emails', methods=['GET'])
@login_required
def crm_client_emails(cid):
    """Return the email thread for a client (optionally scoped to one contact)."""
    conn = get_db()
    company_id = effective_company_id()
    client = _crm_client_or_none(conn, company_id, cid)
    if not client:
        conn.close()
        return jsonify({'error': 'Client not found'}), 404
    contact_id = request.args.get('contact_id', type=int)
    sql = ("SELECT * FROM crm_activities WHERE company_id=? AND client_id=? "
           "AND activity_type='email' AND is_active=1")
    params = [company_id, cid]
    if contact_id:
        sql += ' AND contact_id=?'
        params.append(contact_id)
    sql += ' ORDER BY created_at ASC, id ASC'
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            meta = json.loads(d.get('meta') or '{}')
        except Exception:
            meta = {}
        out.append({
            'id': d['id'], 'contact_id': d['contact_id'],
            'subject': d.get('subject', ''), 'body': d.get('body', ''),
            'created_at': d.get('created_at', ''),
            'direction': meta.get('direction', 'outgoing'),
            'to': meta.get('to', ''), 'from': meta.get('from', ''),
            'sent': bool(meta.get('sent', False)),
        })
    return jsonify({'ok': True, 'emails': out})


@app.route('/api/crm/clients/<int:cid>/email', methods=['POST'])
@login_required
def crm_client_email_send(cid):
    """Log an outgoing email on the client/contact timeline, optionally sending
    it via the tenant's configured SMTP. Stored as a crm_activities email row."""
    conn = get_db()
    company_id = effective_company_id()
    client = _crm_client_or_none(conn, company_id, cid)
    if not client:
        conn.close()
        return jsonify({'error': 'Client not found'}), 404
    d = request.get_json(force=True) or {}
    to_email = (d.get('to') or '').strip()
    subject = (d.get('subject') or '').strip()
    body = (d.get('body') or '').strip()
    contact_id = int(d.get('contact_id') or 0)
    do_send = bool(d.get('send'))
    if not subject and not body:
        conn.close()
        return jsonify({'error': 'Subject or body is required'}), 400

    sent_ok = False
    if do_send:
        if not to_email:
            conn.close()
            return jsonify({'error': 'A "To" email address is required to send.'}), 400
        sent_ok, send_err = _smtp_send(to_email, subject, body)
        if not sent_ok:
            conn.close()
            return jsonify({'error': send_err or 'Failed to send email'}), 400

    now = ts()
    uid = real_user_id()
    meta = json.dumps({'direction': 'outgoing', 'to': to_email,
                       'from': get_setting('smtp_email', ''), 'sent': sent_ok})
    conn.execute(
        "INSERT INTO crm_activities (company_id,client_id,contact_id,activity_type,"
        "subject,body,outcome,due_at,completed_at,status,owner_user_id,meta,"
        "is_active,created_by,updated_by,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (company_id, cid, contact_id, 'email', subject, body, '', '', now,
         'done', uid, meta, 1, uid, uid, now, now))
    new_id = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    conn.commit()
    conn.close()
    try:
        log_activity('email_' + ('sent' if sent_ok else 'logged'),
                     detail=subject, entity_type='crm_client', entity_id=cid)
    except Exception:
        pass
    return jsonify({'ok': True, 'id': new_id, 'sent': sent_ok})


@app.route('/api/crm/ai/draft-email', methods=['POST'])
@login_required
def crm_ai_draft_email():
    """AI-assisted email drafting for the CRM (tenant's DeepSeek key).
    Returns a suggested subject + body from the client/contact context and the
    recruiter's intent. Nothing is sent or stored here — draft only."""
    conn = get_db()
    company_id = effective_company_id()
    d = request.get_json(force=True) or {}
    client_id = int(d.get('client_id') or 0)
    contact_id = int(d.get('contact_id') or 0)
    intent = (d.get('intent') or '').strip()
    tone = (d.get('tone') or 'professional').strip()
    if not intent:
        conn.close()
        return jsonify({'error': 'Please describe what the email should say.'}), 400

    client = _crm_client_or_none(conn, company_id, client_id) if client_id else None
    contact = None
    if contact_id:
        contact = conn.execute(
            'SELECT name, designation, email FROM crm_contacts '
            'WHERE id=? AND company_id=? AND is_active=1',
            (contact_id, company_id)).fetchone()
    thread_ctx = ''
    if client_id:
        prev = conn.execute(
            "SELECT subject, body FROM crm_activities WHERE company_id=? AND client_id=? "
            "AND activity_type='email' AND is_active=1 ORDER BY id DESC LIMIT 3",
            (company_id, client_id)).fetchall()
        if prev:
            thread_ctx = '\n\n'.join(
                '- ' + (p['subject'] or '') + ': ' + (p['body'] or '')[:300] for p in prev)
    conn.close()

    ds_key = get_setting('deepseek_api_key')
    if not ds_key:
        return jsonify({'error': 'DeepSeek API key not configured. Add it in Settings.'}), 400

    sender_name = get_setting('smtp_display_name', '') or 'the recruitment team'
    ctx_lines = []
    if client:
        ctx_lines.append('Client company: ' + (client['name'] or '') +
                         ((' (' + client['industry'] + ')') if client['industry'] else ''))
    if contact:
        ctx_lines.append('Recipient: ' + (contact['name'] or '') +
                         ((', ' + contact['designation']) if contact['designation'] else ''))
    ctx = '\n'.join(ctx_lines) or 'A business client contact.'

    system_msg = (
        "You are an assistant that writes clear, concise, professional B2B "
        "recruitment emails for an Indian staffing agency. Write in natural "
        "business English. Keep it short and specific. Do NOT invent facts, "
        "names, numbers, or commitments that were not provided. "
        "Return ONLY a JSON object of the form {\"subject\": \"...\", \"body\": \"...\"}. "
        "The body must be ready to send and signed off as " + sender_name + "."
    )
    user_msg = (
        'Context:\n' + ctx + '\n\n'
        'Desired tone: ' + tone + '\n'
        'What the email should accomplish:\n' + intent
        + (('\n\nRecent email history for reference:\n' + thread_ctx) if thread_ctx else '')
    )
    try:
        resp = call_deepseek(ds_key,
            {'model': 'deepseek-chat', 'temperature': 0.5, 'max_tokens': 700,
             'messages': [{'role': 'system', 'content': system_msg},
                          {'role': 'user', 'content': user_msg[:8000]}]},
            timeout=60, endpoint='crm_email_draft')
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    if resp.status_code != 200:
        try: err = resp.json().get('error', {}).get('message', 'DeepSeek API error')
        except Exception: err = resp.text[:200]
        return jsonify({'error': err}), 500
    content = resp.json()['choices'][0]['message']['content'].strip()
    parsed = parse_json(content) or {}
    subject = (parsed.get('subject') or '').strip()
    body = (parsed.get('body') or '').strip()
    if not body:
        body = content
    return jsonify({'ok': True, 'subject': subject, 'body': body})


# ══════════════════════════════════════════════════════════════════════════
#  CRM — Attachments (Sprint 2). Files attached to a company or a contact,
#  stored on the persistent disk (CRM_FILES_DIR) and tracked in crm_attachments.
#  Additive; reuses the same storage approach as candidate CVs.
# ══════════════════════════════════════════════════════════════════════════
CRM_ATT_CATEGORIES = {'NDA', 'Agreement', 'PO', 'Requirement', 'Proposal',
                      'Presentation', 'Invoice', 'Resume', 'BusinessCard', 'Other'}
ALLOWED_ATT_EXT = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
                   '.png', '.jpg', '.jpeg', '.txt', '.csv', '.zip'}

def _crm_resolve_client_id(conn, company_id, entity_type, entity_id):
    """Every attachment is anchored to a client_id (a contact resolves to its
    parent client). Returns 0 if the entity does not belong to the tenant."""
    if entity_type == 'contact':
        row = conn.execute(
            'SELECT client_id FROM crm_contacts WHERE id=? AND company_id=? AND is_active=1',
            (entity_id, company_id)).fetchone()
        return row['client_id'] if row else 0
    row = conn.execute(
        'SELECT id FROM crm_clients WHERE id=? AND company_id=? AND is_active=1',
        (entity_id, company_id)).fetchone()
    return row['id'] if row else 0

def _att_public(r):
    d = dict(r)
    d.pop('stored_name', None)  # never expose the on-disk filename
    return d

@app.route('/api/crm/<entity_type>/<int:entity_id>/attachments', methods=['GET'])
@login_required
def crm_list_attachments(entity_type, entity_id):
    if entity_type not in ('client', 'contact'):
        return jsonify({'error': 'Invalid entity type'}), 400
    conn = get_db()
    company_id = effective_company_id()
    rows = conn.execute(
        'SELECT * FROM crm_attachments WHERE company_id=? AND entity_type=? AND entity_id=? '
        'AND is_active=1 ORDER BY created_at DESC, id DESC',
        (company_id, entity_type, entity_id)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'attachments': [_att_public(r) for r in rows]})

@app.route('/api/crm/<entity_type>/<int:entity_id>/attachments', methods=['POST'])
@login_required
def crm_upload_attachment(entity_type, entity_id):
    if entity_type not in ('client', 'contact'):
        return jsonify({'error': 'Invalid entity type'}), 400
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    if not f or not f.filename:
        return jsonify({'error': 'No file selected'}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_ATT_EXT:
        return jsonify({'error': 'Unsupported file type: ' + (ext or 'unknown')}), 400
    category = (request.form.get('category') or 'Other').strip()
    if category not in CRM_ATT_CATEGORIES:
        category = 'Other'
    conn = get_db()
    company_id = effective_company_id()
    client_id = _crm_resolve_client_id(conn, company_id, entity_type, entity_id)
    if not client_id:
        conn.close()
        return jsonify({'error': 'Record not found'}), 404
    now = ts()
    uid = real_user_id()
    stored = ('att_' + entity_type + str(entity_id) + '_'
              + datetime.datetime.now().strftime('%Y%m%d%H%M%S%f') + ext)
    try:
        f.save(os.path.join(CRM_FILES_DIR, stored))
    except Exception as e:
        conn.close()
        return jsonify({'error': 'Failed to save file: ' + str(e)}), 500
    try:
        size = os.path.getsize(os.path.join(CRM_FILES_DIR, stored))
    except Exception:
        size = 0
    conn.execute(
        'INSERT INTO crm_attachments (company_id,client_id,entity_type,entity_id,category,'
        'original_name,stored_name,size_bytes,mime,uploaded_by,is_active,created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (company_id, client_id, entity_type, entity_id, category, f.filename, stored,
         size, f.mimetype or '', uid, 1, now))
    aid = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    conn.commit()
    row = conn.execute('SELECT * FROM crm_attachments WHERE id=?', (aid,)).fetchone()
    conn.close()
    try:
        log_activity('attachment_added', detail=category + ': ' + f.filename,
                     entity_type='crm_' + entity_type, entity_id=entity_id)
    except Exception:
        pass
    return jsonify({'ok': True, 'attachment': _att_public(row)})

@app.route('/api/crm/attachments/<int:aid>/download', methods=['GET'])
@login_required
def crm_download_attachment(aid):
    conn = get_db()
    company_id = effective_company_id()
    r = conn.execute('SELECT * FROM crm_attachments WHERE id=? AND company_id=? AND is_active=1',
                     (aid, company_id)).fetchone()
    conn.close()
    if not r:
        return jsonify({'error': 'Not found'}), 404
    fp = os.path.join(CRM_FILES_DIR, r['stored_name'])
    if not os.path.exists(fp):
        return jsonify({'error': 'File missing on disk'}), 404
    try:
        return send_file(fp, as_attachment=True,
                         download_name=(r['original_name'] or r['stored_name']))
    except TypeError:
        # Older Flask uses attachment_filename instead of download_name
        return send_file(fp, as_attachment=True,
                         attachment_filename=(r['original_name'] or r['stored_name']))

@app.route('/api/crm/attachments/<int:aid>', methods=['DELETE'])
@login_required
def crm_delete_attachment(aid):
    conn = get_db()
    company_id = effective_company_id()
    r = conn.execute('SELECT * FROM crm_attachments WHERE id=? AND company_id=? AND is_active=1',
                     (aid, company_id)).fetchone()
    if not r:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    conn.execute('UPDATE crm_attachments SET is_active=0 WHERE id=?', (aid,))
    conn.commit()
    conn.close()
    try:
        log_activity('attachment_removed', detail=r['original_name'],
                     entity_type='crm_' + r['entity_type'], entity_id=r['entity_id'])
    except Exception:
        pass
    return jsonify({'ok': True})


@app.route('/api/mandates/<int:mid>', methods=['GET'])
@login_required
def get_mandate(mid):
    conn = get_db()
    r = conn.execute('SELECT * FROM mandates WHERE id=? AND owner_id=?', (mid, effective_company_id())).fetchone()
    conn.close()
    if not r:
        return jsonify({'error': 'Not found'}), 404
    # Recruiters can only open mandates assigned to them.
    if not is_company_admin() and r['assigned_user_id'] != real_user_id():
        return jsonify({'error': 'Not found'}), 404
    return jsonify(dict(r))


@app.route('/api/my-profile', methods=['GET'])
@login_required
def get_my_profile():
    u = current_user()
    return jsonify({'ok': True, 'profile': {
        'display_name': u.get('display_name', ''),
        'profile_phone': u.get('profile_phone', ''),
        'profile_designation': u.get('profile_designation', ''),
        'profile_email': u.get('profile_email', ''),
    }})

@app.route('/api/my-profile', methods=['POST'])
@login_required
def update_my_profile():
    d = request.json or {}
    conn = get_db()
    uid = session.get('user_id')
    for field in ('display_name', 'profile_phone', 'profile_designation', 'profile_email'):
        if field in d:
            conn.execute(f'UPDATE users SET {field}=? WHERE id=?', (d[field], uid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/my-team', methods=['GET'])
@login_required
def my_team():
    """Recruiters in the current company (for the assign-to dropdown).
    Company-admin only."""
    if not is_company_admin():
        return jsonify({'error': 'Not allowed'}), 403
    conn = get_db()
    rows = conn.execute('''SELECT id, username, display_name, is_company_admin
                           FROM users WHERE company_id=? AND status='approved'
                           ORDER BY is_company_admin DESC, id''',
                        (effective_company_id(),)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'team': [dict(r) for r in rows]})


@app.route('/api/mandates/<int:mid>/assign', methods=['POST'])
@login_required
def assign_mandate(mid):
    """Company-admin assigns a mandate to a recruiter in the same company."""
    if not is_company_admin():
        return jsonify({'error': 'Only an admin can assign mandates'}), 403
    d = request.json or {}
    target = d.get('user_id')
    conn = get_db()
    m = conn.execute('SELECT id, role, client FROM mandates WHERE id=? AND owner_id=?',
                     (mid, effective_company_id())).fetchone()
    if not m:
        conn.close(); return jsonify({'error': 'Mandate not found'}), 404
    # Target must be a user in the same company.
    u = conn.execute('SELECT id, display_name, username FROM users WHERE id=? AND company_id=?',
                     (target, effective_company_id())).fetchone()
    if not u:
        conn.close(); return jsonify({'error': 'Recruiter not in your company'}), 400
    conn.execute('UPDATE mandates SET assigned_user_id=? WHERE id=?', (target, mid))
    conn.commit(); conn.close()
    log_activity('assign_mandate', f"{m['role']} @ {m['client']} → {u['display_name'] or u['username']}")
    return jsonify({'ok': True})


@app.route('/api/mandates/<int:mid>', methods=['DELETE'])
@login_required
def delete_mandate(mid):
    """Company-admin deletes a job (mandate). Its candidates are NOT deleted —
    they are detached and kept in the company's Central Database."""
    if not is_company_admin():
        return jsonify({'error': 'Only an admin can delete a job'}), 403
    conn = get_db()
    m = conn.execute('SELECT id, role, client FROM mandates WHERE id=? AND owner_id=?',
                     (mid, effective_company_id())).fetchone()
    if not m:
        conn.close(); return jsonify({'error': 'Mandate not found'}), 404
    # Move this mandate's candidates to the company's central pool so they
    # survive in the Central Database (owner_id already = company, so they stay
    # visible there). We only repoint mandate_id to avoid a dangling reference.
    central_mid = get_or_create_central_mandate()
    kept = conn.execute('SELECT COUNT(*) n FROM candidates WHERE mandate_id=? AND owner_id=?',
                        (mid, effective_company_id())).fetchone()['n']
    conn.execute('UPDATE candidates SET mandate_id=? WHERE mandate_id=? AND owner_id=?',
                 (central_mid, mid, effective_company_id()))
    conn.execute('DELETE FROM mandates WHERE id=?', (mid,))
    conn.commit(); conn.close()
    log_activity('delete_mandate', f"{m['role']} @ {m['client']} (kept {kept} candidates in Central DB)")
    return jsonify({'ok': True, 'candidates_kept': kept})


@app.route('/api/mandates/<int:mid>', methods=['PUT'])
@login_required
def update_mandate(mid):
    d = request.json or {}
    conn = get_db()
    own = conn.execute('SELECT owner_id FROM mandates WHERE id=?', (mid,)).fetchone()
    if not own or own['owner_id'] != effective_user_id():
        conn.close(); return jsonify({'error': 'Not found'}), 404
    conn.execute('UPDATE mandates SET client=?,role=?,location=?,division=?,ctc_min=?,ctc_max=?,jd=?,status=? WHERE id=?',
                 (d.get('client',''), d.get('role',''), d.get('location',''), d.get('division',''),
                  float(d.get('ctc_min', 0)), float(d.get('ctc_max', 0)), d.get('jd',''), d.get('status','active'), mid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/mandates/<int:mid>/candidates')
def list_candidates(mid):
    check_timers()
    conn = get_db()
    rows = conn.execute('SELECT * FROM candidates WHERE mandate_id=? ORDER BY created_at DESC', (mid,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = _cand_public(r)   # drop embedding / embedding_text / embedding_vec
        try: d['key_skills'] = json.loads(d['key_skills'] or '[]')
        except: d['key_skills'] = []
        try: d['secondary_skills'] = json.loads(d['secondary_skills'] or '[]')
        except: d['secondary_skills'] = []
        out.append(d)
    return jsonify(out)

@app.route('/api/candidates/<int:cid>/ai-compose', methods=['POST'])
@login_required
def ai_compose_email(cid):
    """Use DeepSeek to draft/refine an email for a candidate based on user's command."""
    d = request.json or {}
    command = (d.get('command') or '').strip()
    context = (d.get('context') or '').strip()
    current_draft = (d.get('current_draft') or '').strip()
    if not command:
        return jsonify({'error': 'Please give a command'}), 400
    key = get_setting('deepseek_api_key', '') or os.environ.get('DEEPSEEK_API_KEY', '')
    if not key:
        return jsonify({'error': 'DeepSeek API key not configured. Add it in Settings.'}), 400

    # Get candidate info for context
    conn = get_db()
    c = conn.execute('SELECT name,company,designation,email,ctc_current,experience,location,mandate_id FROM candidates WHERE id=?', (cid,)).fetchone()
    cand_info = dict(c) if c else {}

    # Get mandate JD for context
    jd_text = ''
    if cand_info.get('mandate_id'):
        mandate = conn.execute('SELECT client,role,jd,location,division,ctc_min,ctc_max FROM mandates WHERE id=?', (cand_info['mandate_id'],)).fetchone()
        if mandate:
            jd_text = mandate['jd'] or ''
            cand_info['mandate_role'] = mandate['role'] or ''
            cand_info['mandate_client'] = mandate['client'] or ''
            cand_info['mandate_location'] = mandate['location'] or ''
            cand_info['ctc_range'] = f"{mandate['ctc_min']}-{mandate['ctc_max']} LPA"

    # Get recruiter profile for signature
    u = current_user()
    recruiter_name = (u.get('display_name') or u.get('username') or '') if u else ''
    recruiter_phone = (u.get('profile_phone') or '') if u else ''
    recruiter_email_addr = (u.get('profile_email') or get_setting('smtp_email', '')) if u else ''
    recruiter_designation = (u.get('profile_designation') or '') if u else ''
    company_name = get_setting('company_name', '') or get_setting('recruiter_name', '')
    conn.close()

    signature_block = f"{recruiter_name}"
    if recruiter_designation: signature_block += f"\n{recruiter_designation}"
    if company_name: signature_block += f"\n{company_name}"
    if recruiter_phone: signature_block += f"\nPhone: {recruiter_phone}"
    if recruiter_email_addr: signature_block += f"\nEmail: {recruiter_email_addr}"

    system_prompt = f"""You are an expert Talent Acquisition and Recruitment Communication Specialist for an Indian Executive Search and Recruitment firm.
Your responsibility is to generate highly professional, personalized recruitment emails that encourage candidates to respond.
Always write naturally like an experienced recruiter, never like AI.
---------------------------------------------------
AVAILABLE DATA
Candidate Details
- Name: {cand_info.get('name','')}
- Current Company: {cand_info.get('company','')}
- Current Designation: {cand_info.get('designation','')}
- Experience: {cand_info.get('experience','')}
- Current Location: {cand_info.get('location','')}
Job Details
- Role: {cand_info.get('mandate_role','')}
- Hiring Company / Client: {cand_info.get('mandate_client','')}
- Job Location: {cand_info.get('mandate_location','')}
- Complete Job Description:
{jd_text or '(not provided)'}
Recruiter Signature
{signature_block}
{('Previous email context:\n' + context) if context else ''}
{('Current draft in compose box (improve or continue from this):\n' + current_draft) if current_draft else ''}
---------------------------------------------------
GENERAL WRITING STYLE
Use:
\u2022 Professional
\u2022 Warm
\u2022 Personalized
\u2022 Easy to read
\u2022 Natural recruiter language
\u2022 Indian business communication style
Avoid:
\u2022 Robotic writing
\u2022 Marketing language
\u2022 AI sounding text
\u2022 Over excitement
\u2022 Emoji
\u2022 ALL CAPS
---------------------------------------------------
EMAIL STRUCTURE
Start with:
Dear {cand_info.get('name','Candidate')},
Introduce yourself in 1-2 lines.
Briefly explain why you are reaching out.
Then generate the requested content.
Always end with:
Regards,
{signature_block}
---------------------------------------------------
IF COMMAND = "create JD"
Generate a complete recruitment email including:
1. Opening paragraph
Mention:
\u2022 Candidate's current role
\u2022 Current company (if available)
\u2022 Why the profile appears relevant
2. About the Opportunity
Short paragraph introducing:
\u2022 Role
\u2022 Client
\u2022 Location
3. Key Responsibilities
Use HTML unordered list.
Only include responsibilities that exist in the provided Job Description.
Do NOT invent responsibilities.
4. Desired Skills & Experience
Use HTML unordered list.
Extract only from JD.
5. Why Consider This Opportunity
Summarize important highlights from JD such as:
\u2022 Industry
\u2022 Technologies
\u2022 Growth
\u2022 Projects
\u2022 Team
\u2022 Leadership
\u2022 Exposure
Only if mentioned.
6. Closing Paragraph
Invite candidate to share:
\u2022 Updated Resume
\u2022 Availability
\u2022 Interest
---------------------------------------------------
IF COMMAND = "follow up"
Generate a short polite follow-up email.
Maximum 120 words.
Mention that you're checking whether the candidate had a chance to review the earlier email.
Invite them to respond if interested.
---------------------------------------------------
IMPORTANT RULES
Never mention:
\u2022 Salary
\u2022 CTC
\u2022 Compensation
\u2022 Budget
unless explicitly present in the prompt AND specifically requested.
Never fabricate information.
If any information is unavailable, simply omit it.
Never write placeholders like:
[Company]
[TBD]
Not Available
---------------------------------------------------
HTML FORMAT
Body must be valid HTML.
Allowed tags:
<p>
<strong>
<ul>
<li>
<br>
No CSS.
No tables.
---------------------------------------------------
SUBJECT LINE
Generate an engaging subject.
Examples:
Opportunity for Senior Electrical Engineer | Mumbai
Business Development Opportunity | Data Centre Industry
Exciting Career Opportunity \u2013 Project Sales | Delhi NCR
Do not use clickbait.
---------------------------------------------------
OUTPUT FORMAT
Return ONLY valid JSON.
{{"subject":"...","body":"<p>...</p>"}}
Do not include markdown.
Do not include explanations.
Do not include additional text outside JSON."""

    text = ''
    try:
        resp = call_deepseek(key, {
            'model': 'deepseek-chat',
            'max_tokens': 1500,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': command}
            ]
        }, endpoint='ai-compose')
        try:
            data = resp.json()
        except Exception:
            return jsonify({'error': 'AI service returned an invalid response. Check your DeepSeek API key in Settings.'}), 502
        if isinstance(data, dict) and data.get('error'):
            emsg = data['error'].get('message', 'Unknown error') if isinstance(data['error'], dict) else str(data['error'])
            return jsonify({'error': 'DeepSeek error: ' + emsg}), 502
        choices = data.get('choices') or []
        if not choices:
            return jsonify({'error': 'AI did not return any content. Please try again.'}), 502
        text = (choices[0].get('message', {}).get('content', '') or '').strip()
        # Strip markdown fences if present
        text = text.replace('```json', '').replace('```', '').strip()
        try:
            result = json.loads(text)
            return jsonify({'ok': True, 'subject': result.get('subject', ''), 'body': result.get('body', '')})
        except json.JSONDecodeError:
            # Not JSON — treat the whole thing as the email body
            return jsonify({'ok': True, 'subject': '', 'body': text})
    except Exception as e:
        return jsonify({'error': f'AI compose failed: {str(e)}'}), 500


@app.route('/api/mandates/<int:mid>/submission-excel')
@login_required
def submission_excel(mid):
    """Generate a client-submission Excel for a mandate's candidates,
    matching the standard submission format (yellow bold headers, borders)."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
    from openpyxl.utils import get_column_letter

    conn = get_db()
    m = conn.execute('SELECT role, client FROM mandates WHERE id=? AND owner_id=?',
                     (mid, effective_company_id())).fetchone()
    if not m:
        conn.close(); return jsonify({'error': 'Mandate not found'}), 404
    stage = (request.args.get('stage') or '').strip()
    if stage:
        rows = conn.execute(
            'SELECT * FROM candidates WHERE mandate_id=? AND stage=? ORDER BY name', (mid, stage)
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT * FROM candidates WHERE mandate_id=? ORDER BY name', (mid,)
        ).fetchall()
    conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Submission'

    headers = ['Candidate Name', 'Contact Number', 'Email ID', 'Educational Qualification',
               'Current Company', 'Total Experience', 'Current CTC', 'Expected CTC',
               'Current Location', 'Preferred Location', 'Notice Period']
    widths = [26.5, 16, 31.5, 25.7, 31, 16.5, 27.7, 32.5, 16.3, 18.3, 28.3]

    header_fill = PatternFill(start_color='FFFF00', end_color='FFFF00', fill_type='solid')
    header_font = Font(bold=True, size=10, color='222222')
    thin = Side(style='thin', color='BBBBBB')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal='left', vertical='center', wrap_text=True)

    for i, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=i, value=h)
        cell.fill = header_fill; cell.font = header_font
        cell.border = border; cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = widths[i-1]
    ws.row_dimensions[1].height = 28

    def fmt_exp(v):
        try:
            v = float(v or 0)
            return f"{int(v)} Years" if v == int(v) else f"{v} Years"
        except Exception:
            return ''
    def fmt_ctc(v):
        try:
            v = float(v or 0)
            return f"{int(v)} LPA" if v == int(v) else f"{v} LPA"
        except Exception:
            return ''
    def fmt_notice(v):
        try:
            v = int(v or 0)
            return f"{v} Days" if v else ''
        except Exception:
            return ''

    r = 2
    for c in rows:
        d = dict(c)
        vals = [
            d.get('name', ''), d.get('phone', ''), d.get('email', ''),
            d.get('qualification', ''), d.get('company', ''),
            fmt_exp(d.get('experience')), fmt_ctc(d.get('ctc_current')),
            fmt_ctc(d.get('ctc_expected')), d.get('location', ''),
            d.get('preferred_location', ''), fmt_notice(d.get('notice_period')),
        ]
        for i, val in enumerate(vals, 1):
            cell = ws.cell(row=r, column=i, value=val)
            cell.border = border; cell.alignment = center; cell.font = Font(size=10)
        r += 1

    bio = io.BytesIO()
    wb.save(bio); bio.seek(0)
    safe_role = re.sub(r'[^A-Za-z0-9_-]+', '_', (m['role'] or 'Submission'))[:40]
    safe_stage = ('_' + re.sub(r'[^A-Za-z0-9_-]+', '_', stage)) if stage else ''
    fname = f"Submission_{safe_role}{safe_stage}.xlsx"
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/api/mandates/<int:mid>/email-templates', methods=['GET'])
@login_required
def get_mandate_templates(mid):
    conn = get_db()
    m = conn.execute('SELECT email_templates FROM mandates WHERE id=?', (mid,)).fetchone()
    conn.close()
    if not m:
        return jsonify({'error': 'Mandate not found'}), 404
    try:
        tpls = json.loads(m['email_templates'] or '[]')
    except Exception:
        tpls = []
    return jsonify({'ok': True, 'templates': tpls})


@app.route('/api/mandates/<int:mid>/email-templates', methods=['POST'])
@login_required
def save_mandate_templates(mid):
    d = request.json or {}
    templates = d.get('templates', [])
    conn = get_db()
    conn.execute('UPDATE mandates SET email_templates=? WHERE id=?', (json.dumps(templates), mid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/candidates/<int:cid>/email-history')
@login_required
def candidate_email_history(cid):
    """Return sent email events for a candidate."""
    conn = get_db()
    rows = conn.execute(
        "SELECT detail, created_at FROM candidate_events WHERE candidate_id=? AND event_type='email' ORDER BY created_at DESC",
        (cid,)
    ).fetchall()
    conn.close()
    return jsonify({'ok': True, 'emails': [{'text': r['detail'], 'ts': r['created_at']} for r in rows]})


@app.route('/api/candidates/<int:cid>/send-email', methods=['POST'])
@login_required
def send_candidate_email(cid):
    """Send an email to a candidate via the user's configured SMTP (Gmail app-password).
    Logs the sent email to the candidate journey."""
    d = request.json or {}
    to_email = (d.get('to') or '').strip()
    subject = (d.get('subject') or '').strip()
    body = (d.get('body') or '').strip()
    if not to_email or not subject or not body:
        return jsonify({'error': 'To, Subject and Body are required'}), 400

    smtp_email = get_setting('smtp_email', '')
    smtp_pass = get_setting('smtp_app_password', '')
    smtp_name = get_setting('smtp_display_name', '') or smtp_email
    if not smtp_email or not smtp_pass:
        return jsonify({'error': 'Email not configured. Go to Settings → Email Configuration and add your Gmail + App Password.'}), 400

    # Build the email
    msg = MIMEMultipart('alternative')
    msg['From'] = f'{smtp_name} <{smtp_email}>' if smtp_name else smtp_email
    msg['To'] = to_email
    msg['Subject'] = subject
    # Generate a stable Message-ID so replies can be threaded back to this email
    import email.utils as _eut
    domain = smtp_email.split('@')[-1] if '@' in smtp_email else 'hirelab.local'
    gen_msg_id = _eut.make_msgid(domain=domain)
    msg['Message-ID'] = gen_msg_id
    msg['Date'] = _eut.formatdate(localtime=True)
    # Send as both plain text and HTML
    msg.attach(MIMEText(body, 'plain', 'utf-8'))
    body_html = (d.get('body_html') or '').strip()
    if body_html:
        # Use the rich-text HTML from the editor
        html_content = f'<div style="font-family:sans-serif;font-size:14px">{body_html}</div>'
    else:
        html_content = f'<div style="font-family:sans-serif;font-size:14px">{body.replace(chr(10), "<br>")}</div>'
    msg.attach(MIMEText(html_content, 'html', 'utf-8'))

    # Detect SMTP server from email domain
    if '@gmail' in smtp_email.lower() or '@googlemail' in smtp_email.lower():
        smtp_host, smtp_port = 'smtp.gmail.com', 587
    elif '@outlook' in smtp_email.lower() or '@hotmail' in smtp_email.lower() or '@live' in smtp_email.lower():
        smtp_host, smtp_port = 'smtp-mail.outlook.com', 587
    elif '@yahoo' in smtp_email.lower():
        smtp_host, smtp_port = 'smtp.mail.yahoo.com', 587
    else:
        smtp_host, smtp_port = 'smtp.gmail.com', 587  # default to Gmail

    try:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
        server.starttls()
        server.login(smtp_email, smtp_pass)
        server.sendmail(smtp_email, [to_email], msg.as_string())
        server.quit()
    except smtplib.SMTPAuthenticationError:
        return jsonify({'error': 'Email authentication failed. Check your email address and app password in Settings.'}), 401
    except Exception as e:
        return jsonify({'error': f'Failed to send email: {str(e)}'}), 500

    # Log to candidate journey (full email for history)
    u = current_user()
    who = (u.get('display_name') or u.get('username') or '') if u else ''
    full_log = f'Email sent to {to_email}\nSubject: {subject}\n\n{body}'
    if who:
        full_log += f'\n— {who}'
    log_candidate_event(cid, 'email', full_log)

    # Store in the 2-way email thread table
    try:
        conn = get_db()
        conn.execute(
            'INSERT OR IGNORE INTO email_messages (company_id, candidate_id, direction, '
            'from_addr, to_addr, subject, body, message_id, in_reply_to, sent_at, created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (effective_company_id(), cid, 'sent', smtp_email, to_email, subject, body,
             gen_msg_id, '', ts(), ts()))
        conn.commit()
        conn.close()
    except Exception:
        pass

    return jsonify({'ok': True, 'message': 'Email sent successfully'})


# ═══════════════════════════════════════════════════════════════════════
#  2-WAY EMAIL — IMAP inbox sync + candidate threads
# ═══════════════════════════════════════════════════════════════════════
def _imap_host_for(email_addr):
    e = (email_addr or '').lower()
    if '@gmail' in e or '@googlemail' in e:
        return 'imap.gmail.com'
    if '@outlook' in e or '@hotmail' in e or '@live' in e:
        return 'outlook.office365.com'
    if '@yahoo' in e:
        return 'imap.mail.yahoo.com'
    return 'imap.gmail.com'


def _decode_mime_header(raw):
    """Decode an email header that may be MIME-encoded."""
    from email.header import decode_header
    if not raw:
        return ''
    parts = decode_header(raw)
    out = ''
    for txt, enc in parts:
        if isinstance(txt, bytes):
            try:
                out += txt.decode(enc or 'utf-8', errors='replace')
            except Exception:
                out += txt.decode('utf-8', errors='replace')
        else:
            out += txt
    return out


def _extract_plain_body(msg):
    """Get a plain-text body from an email.message.Message."""
    body = ''
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get('Content-Disposition') or '')
            if ctype == 'text/plain' and 'attachment' not in disp:
                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or 'utf-8'
                    body += payload.decode(charset, errors='replace')
                except Exception:
                    pass
        if not body:
            # fall back to html stripped
            for part in msg.walk():
                if part.get_content_type() == 'text/html':
                    try:
                        payload = part.get_payload(decode=True)
                        charset = part.get_content_charset() or 'utf-8'
                        body += html_to_text(payload.decode(charset, errors='replace'))
                    except Exception:
                        pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or 'utf-8'
            body = payload.decode(charset, errors='replace')
        except Exception:
            body = str(msg.get_payload())
    return body.strip()


def _sync_imap_inbox(company_id):
    """Connect via IMAP, fetch recent inbox messages, match to candidates by
    email address, and store new incoming messages. Returns (new_count, error)."""
    import imaplib, email as _email, re as _re

    smtp_email = (get_setting('smtp_email', '') or '').strip()
    smtp_pass = (get_setting('smtp_app_password', '') or '')
    if not smtp_email or not smtp_pass:
        return 0, 'Email not configured. Add your Gmail + App Password in Settings.'
    # Gmail app passwords are shown with spaces ("xxxx xxxx xxxx xxxx") but must be
    # sent without spaces. Strip them defensively.
    smtp_pass = smtp_pass.replace(' ', '').strip()

    host = _imap_host_for(smtp_email)

    # Build a map of candidate email -> candidate_id for this tenant
    conn = get_db()
    cand_rows = conn.execute(
        "SELECT id, email FROM candidates WHERE owner_id=? AND email IS NOT NULL AND email!=''",
        (company_id,)).fetchall()
    email_to_cid = {}
    for r in cand_rows:
        em = (r['email'] or '').strip().lower()
        if em:
            email_to_cid[em] = r['id']

    if not email_to_cid:
        conn.close()
        return 0, None  # no candidates with emails, nothing to match

    new_count = 0
    try:
        M = imaplib.IMAP4_SSL(host, 993)
        M.login(smtp_email, smtp_pass)
        M.select('INBOX')
        # Search last 60 days to keep it light
        import datetime as _dt
        since = (_dt.datetime.utcnow() - _dt.timedelta(days=60)).strftime('%d-%b-%Y')
        typ, data = M.search(None, f'(SINCE {since})')
        if typ != 'OK':
            M.logout(); conn.close()
            return 0, 'IMAP search failed'
        ids = data[0].split()
        # Only look at the most recent ~200 to bound work
        ids = ids[-200:]
        for num in ids:
            typ, msg_data = M.fetch(num, '(RFC822)')
            if typ != 'OK' or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            m = _email.message_from_bytes(raw)
            from_hdr = _decode_mime_header(m.get('From', ''))
            # extract bare email
            fmatch = _re.search(r'[\w\.\-\+]+@[\w\.\-]+', from_hdr)
            from_email = (fmatch.group(0).lower() if fmatch else '')
            if from_email not in email_to_cid:
                continue  # not from a known candidate
            cid = email_to_cid[from_email]
            message_id = (m.get('Message-ID', '') or '').strip()
            if not message_id:
                continue
            # Dedup: skip if we already stored this message_id
            exists = conn.execute(
                'SELECT id FROM email_messages WHERE company_id=? AND message_id=?',
                (company_id, message_id)).fetchone()
            if exists:
                continue
            subject = _decode_mime_header(m.get('Subject', ''))
            in_reply_to = (m.get('In-Reply-To', '') or '').strip()
            body = _extract_plain_body(m)
            import email.utils as _eut
            date_hdr = m.get('Date', '')
            try:
                dt = _eut.parsedate_to_datetime(date_hdr)
                sent_at = dt.strftime('%Y-%m-%dT%H:%M:%S')
            except Exception:
                sent_at = ts()
            conn.execute(
                'INSERT OR IGNORE INTO email_messages (company_id, candidate_id, direction, '
                'from_addr, to_addr, subject, body, message_id, in_reply_to, sent_at, created_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (company_id, cid, 'received', from_email, smtp_email, subject, body,
                 message_id, in_reply_to, sent_at, ts()))
            new_count += 1
        conn.commit()
        M.logout()
    except imaplib.IMAP4.error as e:
        conn.close()
        return 0, f'IMAP login failed. Check your email & app password. ({str(e)[:80]})'
    except Exception as e:
        conn.close()
        return 0, f'IMAP sync error: {str(e)[:100]}'
    conn.close()
    return new_count, None


@app.route('/api/email/sync', methods=['POST'])
@login_required
def email_sync():
    """Manually trigger an IMAP inbox sync to pull candidate replies."""
    new_count, err = _sync_imap_inbox(effective_company_id())
    if err:
        return jsonify({'error': err}), 400
    return jsonify({'ok': True, 'new_messages': new_count})


@app.route('/api/email/diagnose', methods=['GET'])
@login_required
def email_diagnose():
    """Step-by-step IMAP diagnostic so we can see exactly where sync fails."""
    import imaplib
    steps = []
    smtp_email = (get_setting('smtp_email', '') or '').strip()
    smtp_pass = (get_setting('smtp_app_password', '') or '').replace(' ', '').strip()

    steps.append({'step': 'Email configured', 'ok': bool(smtp_email),
                  'detail': smtp_email or 'No email set in Settings'})
    steps.append({'step': 'App password set', 'ok': bool(smtp_pass),
                  'detail': f'{len(smtp_pass)} characters' if smtp_pass else 'No app password set'})
    if not smtp_email or not smtp_pass:
        return jsonify({'ok': False, 'steps': steps})

    host = _imap_host_for(smtp_email)
    steps.append({'step': 'IMAP server', 'ok': True, 'detail': host + ':993'})

    # Try connect
    try:
        M = imaplib.IMAP4_SSL(host, 993)
        steps.append({'step': 'Connect to server', 'ok': True, 'detail': 'Connected'})
    except Exception as e:
        steps.append({'step': 'Connect to server', 'ok': False, 'detail': str(e)[:120]})
        return jsonify({'ok': False, 'steps': steps})

    # Try login
    try:
        M.login(smtp_email, smtp_pass)
        steps.append({'step': 'Login', 'ok': True, 'detail': 'Login successful'})
    except imaplib.IMAP4.error as e:
        msg = str(e)
        hint = ''
        if 'Invalid credentials' in msg or 'AUTHENTICATIONFAILED' in msg:
            hint = ' — The app password is wrong, or this Workspace account requires a fresh App Password. Also confirm 2-Step Verification is ON.'
        steps.append({'step': 'Login', 'ok': False, 'detail': msg[:120] + hint})
        try: M.logout()
        except Exception: pass
        return jsonify({'ok': False, 'steps': steps})

    # Try select inbox
    try:
        typ, data = M.select('INBOX')
        cnt = data[0].decode() if data and data[0] else '?'
        steps.append({'step': 'Open INBOX', 'ok': typ == 'OK', 'detail': f'{cnt} total messages in inbox'})
    except Exception as e:
        steps.append({'step': 'Open INBOX', 'ok': False, 'detail': str(e)[:120]})
        try: M.logout()
        except Exception: pass
        return jsonify({'ok': False, 'steps': steps})

    # Count candidates with emails
    conn = get_db()
    ccount = conn.execute(
        "SELECT COUNT(*) n FROM candidates WHERE owner_id=? AND email IS NOT NULL AND email!=''",
        (effective_company_id(),)).fetchone()['n']
    conn.close()
    steps.append({'step': 'Candidates with email on file', 'ok': ccount > 0,
                  'detail': f'{ccount} candidates have an email address (needed to match replies)'})

    try: M.logout()
    except Exception: pass
    return jsonify({'ok': True, 'steps': steps})


@app.route('/api/candidates/<int:cid>/email-thread', methods=['GET'])
@login_required
def candidate_email_thread(cid):
    """Return the full email conversation (sent + received) for a candidate,
    chronological order. Optionally sync IMAP first if ?sync=1."""
    if request.args.get('sync') == '1':
        _sync_imap_inbox(effective_company_id())
    conn = get_db()
    rows = conn.execute(
        'SELECT id, direction, from_addr, to_addr, subject, body, sent_at '
        'FROM email_messages WHERE company_id=? AND candidate_id=? '
        'ORDER BY sent_at ASC, id ASC',
        (effective_company_id(), cid)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'messages': [dict(r) for r in rows]})


def _smtp_send(to_email, subject, plain_body, html_body=None):
    """Send an email via the tenant's configured SMTP. Returns (ok, error)."""
    smtp_email = get_setting('smtp_email', '')
    smtp_pass = get_setting('smtp_app_password', '')
    smtp_name = get_setting('smtp_display_name', '') or smtp_email
    if not smtp_email or not smtp_pass:
        return False, 'Email not configured. Go to Settings → Email Configuration and add your Gmail + App Password.'
    msg = MIMEMultipart('alternative')
    msg['From'] = f'{smtp_name} <{smtp_email}>' if smtp_name else smtp_email
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(plain_body, 'plain', 'utf-8'))
    if html_body:
        msg.attach(MIMEText(f'<div style="font-family:sans-serif;font-size:14px">{html_body}</div>', 'html', 'utf-8'))
    else:
        msg.attach(MIMEText(f'<div style="font-family:sans-serif;font-size:14px">{plain_body.replace(chr(10), "<br>")}</div>', 'html', 'utf-8'))
    el = smtp_email.lower()
    if '@gmail' in el or '@googlemail' in el:
        host, port = 'smtp.gmail.com', 587
    elif '@outlook' in el or '@hotmail' in el or '@live' in el:
        host, port = 'smtp-mail.outlook.com', 587
    elif '@yahoo' in el:
        host, port = 'smtp.mail.yahoo.com', 587
    else:
        host, port = 'smtp.gmail.com', 587
    try:
        server = smtplib.SMTP(host, port, timeout=15)
        server.starttls()
        server.login(smtp_email, smtp_pass)
        server.sendmail(smtp_email, [to_email], msg.as_string())
        server.quit()
        return True, None
    except smtplib.SMTPAuthenticationError:
        return False, 'Email authentication failed. Check your email address and app password in Settings.'
    except Exception as e:
        return False, f'Failed to send email: {str(e)}'


@app.route('/api/activity', methods=['GET'])
@login_required
def get_activity():
    """Universal activity timeline. Filter by entity or search; paginated.
    Query params: entity_type, entity_id, q (search), page, per_page."""
    entity_type = request.args.get('entity_type', '').strip()
    entity_id = request.args.get('entity_id', type=int)
    q = request.args.get('q', '').strip()
    page = max(1, request.args.get('page', 1, type=int))
    per_page = min(100, max(1, request.args.get('per_page', 25, type=int)))
    company_id = effective_company_id()

    where = ['(company_id=? OR company_id=0)']
    params = [company_id]
    if entity_type:
        where.append('entity_type=?'); params.append(entity_type)
    if entity_id:
        where.append('entity_id=?'); params.append(entity_id)
    if q:
        where.append('(action LIKE ? OR detail LIKE ? OR username LIKE ?)')
        like = f'%{q}%'; params += [like, like, like]
    where_sql = ' AND '.join(where)

    conn = get_db()
    total = conn.execute(f'SELECT COUNT(*) n FROM activity_log WHERE {where_sql}', params).fetchone()['n']
    rows = conn.execute(
        f'SELECT * FROM activity_log WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?',
        params + [per_page, (page - 1) * per_page]).fetchall()
    conn.close()
    return jsonify({'ok': True, 'total': total, 'page': page, 'per_page': per_page,
                    'pages': (total + per_page - 1) // per_page,
                    'activity': [dict(r) for r in rows]})


@app.route('/api/audit', methods=['GET'])
@login_required
def get_audit():
    """Field-level audit trail (old → new) for a given entity."""
    entity_type = request.args.get('entity_type', '').strip()
    entity_id = request.args.get('entity_id', type=int)
    if not entity_type or not entity_id:
        return jsonify({'error': 'entity_type and entity_id required'}), 400
    company_id = effective_company_id()
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM audit_log WHERE (company_id=? OR company_id=0) AND entity_type=? AND entity_id=? '
        'ORDER BY id DESC LIMIT 200', (company_id, entity_type, entity_id)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'audit': [dict(r) for r in rows]})


@app.route('/api/candidates/<int:cid>/interviews', methods=['GET'])
@login_required
def list_interviews(cid):
    conn = get_db()
    rows = conn.execute('SELECT * FROM interviews WHERE candidate_id=? ORDER BY scheduled_at DESC', (cid,)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'interviews': [dict(r) for r in rows]})


@app.route('/api/candidates/<int:cid>/interviews', methods=['POST'])
@login_required
def create_interview(cid):
    d = request.json or {}
    round_name = (d.get('round_name') or 'Interview').strip()
    mode = (d.get('mode') or '').strip()
    location = (d.get('location') or '').strip()
    interviewer = (d.get('interviewer') or '').strip()
    scheduled_at = (d.get('scheduled_at') or '').strip()
    if not scheduled_at:
        return jsonify({'error': 'Date & time required'}), 400
    conn = get_db()
    c = conn.execute('SELECT mandate_id, name FROM candidates WHERE id=?', (cid,)).fetchone()
    if not c:
        conn.close(); return jsonify({'error': 'Candidate not found'}), 404
    conn.execute(
        'INSERT INTO interviews (candidate_id,mandate_id,owner_id,round_name,mode,location,interviewer,scheduled_at,status,created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (cid, c['mandate_id'], effective_user_id(), round_name, mode, location, interviewer, scheduled_at, 'scheduled', ts()))
    # Auto-move to Interview Inprocess stage
    conn.execute('UPDATE candidates SET stage=?, updated_at=? WHERE id=?', ('Interview Inprocess', ts(), cid))
    conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                 (cid, '', 'Interview Inprocess', f'{round_name} scheduled', ts()))
    conn.commit(); conn.close()
    # Nice human date for the journey
    try:
        dt = datetime.datetime.fromisoformat(scheduled_at)
        nice = dt.strftime('%d %b %Y, %I:%M %p')
    except Exception:
        nice = scheduled_at
    log_candidate_event(cid, 'note', f'Interview scheduled — {round_name}: {nice}' + (f' ({mode})' if mode else ''))
    return jsonify({'ok': True})


@app.route('/api/interviews/<int:iid>/result', methods=['POST'])
@login_required
def interview_result(iid):
    d = request.json or {}
    result = (d.get('result') or '').strip()
    conn = get_db()
    iv = conn.execute('SELECT candidate_id, round_name FROM interviews WHERE id=?', (iid,)).fetchone()
    if not iv:
        conn.close(); return jsonify({'error': 'Interview not found'}), 404
    conn.execute('UPDATE interviews SET status=?, result=? WHERE id=?', ('completed', result, iid))
    conn.commit(); conn.close()
    if result:
        log_candidate_event(iv['candidate_id'], 'note', f'{iv["round_name"]} result: {result}')
    return jsonify({'ok': True})


@app.route('/api/interviews/<int:iid>', methods=['DELETE'])
@login_required
def delete_interview(iid):
    conn = get_db()
    conn.execute('DELETE FROM interviews WHERE id=?', (iid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/candidates/<int:cid>/interview-message', methods=['POST'])
@login_required
def interview_message(cid):
    """Build the ready-to-send interview message from the template + details."""
    d = request.json or {}
    conn = get_db()
    c = conn.execute('SELECT name, mandate_id FROM candidates WHERE id=?', (cid,)).fetchone()
    if not c:
        conn.close(); return jsonify({'error': 'Candidate not found'}), 404
    mandate = conn.execute('SELECT role, client FROM mandates WHERE id=?', (c['mandate_id'],)).fetchone()
    conn.close()

    tpl = get_setting('interview_template', '') or 'Dear {name}, your interview is scheduled for {datetime}.'
    u = current_user()
    recruiter = (u.get('display_name') or u.get('username') or '') if u else ''
    try:
        dt = datetime.datetime.fromisoformat((d.get('scheduled_at') or '').strip())
        nice_dt = dt.strftime('%d %b %Y, %I:%M %p')
    except Exception:
        nice_dt = (d.get('scheduled_at') or '').strip()
    mode = (d.get('mode') or '').strip()
    location = (d.get('location') or '').strip()
    if mode.lower() in ('video', 'video call') and location:
        location_line = f'Meeting Link: {location}'
    elif location:
        location_line = f'Venue: {location}'
    else:
        location_line = ''
    msg = (tpl.replace('{name}', c['name'] or 'Candidate')
              .replace('{role}', (mandate['role'] if mandate else '') or 'the role')
              .replace('{client}', (mandate['client'] if mandate else '') or '')
              .replace('{round}', (d.get('round_name') or 'Interview').strip())
              .replace('{datetime}', nice_dt)
              .replace('{mode}', mode or 'To be confirmed')
              .replace('{location_line}', location_line)
              .replace('{interviewer}', (d.get('interviewer') or '').strip())
              .replace('{recruiter}', recruiter))
    # Clean any empty leftover lines
    msg = '\n'.join([ln for ln in msg.split('\n') if ln.strip() != ''] ) if False else msg
    return jsonify({'ok': True, 'message': msg})


@app.route('/api/candidates/<int:cid>/request-update', methods=['POST'])
@login_required
def request_candidate_update(cid):
    """Generate a secure self-update link and email it to the candidate."""
    import secrets as _secrets
    conn = get_db()
    c = conn.execute('SELECT * FROM candidates WHERE id=? AND owner_id=?',
                     (cid, effective_company_id())).fetchone()
    if not c:
        conn.close(); return jsonify({'error': 'Candidate not found'}), 404
    if not (c['email'] or '').strip():
        conn.close(); return jsonify({'error': 'Candidate ka email nahi hai. Pehle email add karein.'}), 400

    token = _secrets.token_urlsafe(24)
    conn.execute('UPDATE candidates SET update_token=?, update_requested_at=?, update_submitted_at=? WHERE id=?',
                 (token, ts(), '', cid))
    # Mandate + recruiter context for the email
    mandate = conn.execute('SELECT role, client FROM mandates WHERE id=?', (c['mandate_id'],)).fetchone()
    conn.commit(); conn.close()

    role = mandate['role'] if mandate else 'a role'
    u = current_user()
    recruiter_name = (u.get('display_name') or u.get('username') or 'Recruiter') if u else 'Recruiter'
    company = get_setting('company_name', '') or 'our team'

    base = request.host_url.rstrip('/')
    link = f'{base}/update-profile?token={token}'

    subject = f'Please share your updated profile — {role}'
    plain = (f"Dear {c['name'] or 'Candidate'},\n\n"
             f"Thank you for your interest in the {role} position"
             + (f" at {mandate['client']}" if mandate and mandate['client'] else '') + ".\n\n"
             f"To move ahead, please review and update your details and upload your latest resume "
             f"using the secure link below:\n\n{link}\n\n"
             f"This link is personal to you. It will take just 2 minutes.\n\n"
             f"Regards,\n{recruiter_name}\n{company}")
    html = (f"Dear {esc_html(c['name'] or 'Candidate')},<br><br>"
            f"Thank you for your interest in the <b>{esc_html(role)}</b> position"
            + (f" at <b>{esc_html(mandate['client'])}</b>" if mandate and mandate['client'] else '') + ".<br><br>"
            f"To move ahead, please review and update your details and upload your latest resume "
            f"using the secure link below:<br><br>"
            f'<a href="{link}" style="display:inline-block;background:#1D9E75;color:#fff;padding:11px 22px;border-radius:8px;text-decoration:none;font-weight:600">Update My Profile</a><br><br>'
            f'<span style="font-size:12px;color:#666">Or copy this link: {link}</span><br><br>'
            f"This link is personal to you. It will take just 2 minutes.<br><br>"
            f"Regards,<br><b>{esc_html(recruiter_name)}</b><br>{esc_html(company)}")

    ok, err = _smtp_send(c['email'], subject, plain, html)
    if not ok:
        return jsonify({'error': err}), 400
    log_candidate_event(cid, 'note', f'Requested updated resume — link emailed to {c["email"]}')
    return jsonify({'ok': True, 'message': 'Update request email sent!'})


@app.route('/update-profile')
def update_profile_page():
    return send_file('update-profile.html')


@app.route('/api/public/candidate-update/<token>', methods=['GET'])
def public_get_candidate(token):
    """Return the candidate's editable fields for the self-update page."""
    if not token or len(token) < 10:
        return jsonify({'error': 'Invalid link'}), 400
    conn = get_db()
    c = conn.execute('SELECT * FROM candidates WHERE update_token=?', (token,)).fetchone()
    conn.close()
    if not c:
        return jsonify({'error': 'This link is invalid or has expired.'}), 404
    # Expiry: 14 days from request
    try:
        req_at = datetime.datetime.fromisoformat(c['update_requested_at'])
        if (datetime.datetime.now() - req_at).days > 14:
            return jsonify({'error': 'This link has expired. Please ask your recruiter for a new one.'}), 410
    except Exception:
        pass
    try:
        skills = json.loads(c['key_skills'] or '[]')
    except Exception:
        skills = []
    return jsonify({'ok': True, 'candidate': {
        'name': c['name'] or '', 'phone': c['phone'] or '', 'email': c['email'] or '',
        'company': c['company'] or '', 'designation': c['designation'] or '',
        'experience': c['experience'] or '', 'ctc_current': c['ctc_current'] or '',
        'ctc_expected': c['ctc_expected'] or '', 'notice_period': c['notice_period'] or '',
        'location': c['location'] or '', 'preferred_location': c['preferred_location'] or '',
        'qualification': c['qualification'] or '', 'key_skills': skills,
        'already_submitted': bool(c['update_submitted_at']),
    }})


@app.route('/api/public/candidate-update/<token>', methods=['POST'])
def public_save_candidate(token):
    """Candidate submits their updated details (+ optional resume) via the link."""
    if not token or len(token) < 10:
        return jsonify({'error': 'Invalid link'}), 400
    conn = get_db()
    c = conn.execute('SELECT * FROM candidates WHERE update_token=?', (token,)).fetchone()
    if not c:
        conn.close(); return jsonify({'error': 'This link is invalid or has expired.'}), 404
    cid = c['id']

    d = request.form if request.form else (request.json or {})
    fields = ['name', 'phone', 'email', 'company', 'designation', 'location',
              'preferred_location', 'qualification']
    num_fields = ['experience', 'ctc_current', 'ctc_expected', 'notice_period']
    sets, vals = [], []
    for f in fields:
        if f in d:
            sets.append(f'{f}=?'); vals.append(str(d.get(f) or '').strip())
    for f in num_fields:
        if f in d:
            try:
                sets.append(f'{f}=?'); vals.append(float(d.get(f) or 0))
            except Exception:
                pass
    if 'key_skills' in d:
        ks = d.get('key_skills')
        if isinstance(ks, str):
            try: ks = json.loads(ks)
            except Exception: ks = [s.strip() for s in ks.split(',') if s.strip()]
        sets.append('key_skills=?'); vals.append(json.dumps(ks or []))

    # Optional resume upload
    resume_saved = False
    if 'resume' in request.files:
        f = request.files['resume']
        if f and f.filename:
            ext = Path(f.filename).suffix.lower()
            if ext in ['.pdf', '.doc', '.docx']:
                fname = 'c' + str(cid) + '_' + datetime.datetime.now().strftime('%Y%m%d%H%M%S') + ext
                f.save(os.path.join(CV_DIR, fname))
                sets.append('cv_path=?'); vals.append(fname)
                sets.append('cv_original_name=?'); vals.append(f.filename)
                resume_saved = True

    if sets:
        vals += [ts(), cid]
        conn.execute('UPDATE candidates SET ' + ','.join(sets) + ',updated_at=? WHERE id=?', vals)
    conn.execute('UPDATE candidates SET update_submitted_at=? WHERE id=?', (ts(), cid))
    conn.commit(); conn.close()

    log_candidate_event(cid, 'update', 'Candidate submitted updated profile via self-update link'
                        + (' (with new resume)' if resume_saved else ''))
    return jsonify({'ok': True, 'message': 'Thank you! Your details have been updated.'})


@app.route('/api/candidates/<int:cid>/note', methods=['POST'])
@login_required
def add_candidate_note(cid):
    """Add a free-text comment to the candidate's journey."""
    text = (request.json or {}).get('text', '').strip()
    if not text:
        return jsonify({'error': 'Empty comment'}), 400
    u = current_user()
    who = (u.get('display_name') or u.get('username') or '') if u else ''
    detail = text + (' — ' + who if who else '')
    log_candidate_event(cid, 'note', detail)
    return jsonify({'ok': True})


@app.route('/api/candidates/<int:cid>/journey')
def candidate_journey(cid):
    """Aggregate a candidate's full journey from every real event source,
    newest first. Each event: {ts, type, text, icon, color}."""
    conn = get_db()
    c = conn.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
    if not c:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    ev = []
    def add(t, text, icon, color):
        if t:
            ev.append({'ts': t, 'text': text, 'icon': icon, 'color': color})

    # Sourced / created
    add(c['created_at'], 'Candidate added to pipeline', 'user-plus', 'gray')
    # Stage changes
    for h in conn.execute('SELECT * FROM stage_history WHERE candidate_id=? ORDER BY created_at', (cid,)).fetchall():
        frm = h['from_stage'] or '—'
        add(h['created_at'], f"Stage changed — {frm} to {h['to_stage']}", 'arrow-right', 'purple')
    # WhatsApp sends
    add(c['msg1_sent_at'], 'WhatsApp intro sent', 'brand-whatsapp', 'green')
    add(c['fu1_sent_at'], 'WhatsApp follow up 1 sent', 'brand-whatsapp', 'green')
    add(c['fu2_sent_at'], 'WhatsApp follow up 2 sent', 'brand-whatsapp', 'green')
    # WhatsApp / call response
    if c['wa_response']:
        rmap = {'interested': 'Interested', 'callback': 'Callback', 'not_interested': 'Not interested', 'no_reply': 'No reply'}
        add(c['wa_response_at'] or c['updated_at'], 'Response logged — ' + rmap.get(c['wa_response'], c['wa_response']), 'message-dots', 'teal')
    # Reminders
    for r in conn.execute('SELECT * FROM reminders WHERE candidate_id=? ORDER BY created_at', (cid,)).fetchall():
        note = (r['note'] or 'Reminder')
        add(r['created_at'], 'Reminder set — ' + note, 'bell', 'amber')
        if r['done']:
            add(r['due_at'], 'Reminder completed — ' + note, 'check', 'teal')
    # Logged events (tags added, call analysed, etc.)
    for e in conn.execute('SELECT * FROM candidate_events WHERE candidate_id=? ORDER BY created_at', (cid,)).fetchall():
        icon = {'tag': 'tag', 'call': 'phone', 'note': 'note', 'email': 'mail', 'edit': 'edit'}.get(e['event_type'], 'point')
        color = {'tag': 'gray', 'call': 'teal', 'note': 'blue', 'email': 'purple', 'edit': 'amber'}.get(e['event_type'], 'gray')
        add(e['created_at'], e['detail'], icon, color)
    conn.close()

    ev = [x for x in ev if x['ts']]
    ev.sort(key=lambda x: x['ts'], reverse=True)
    return jsonify({'ok': True, 'events': ev})


@app.route('/api/candidates/<int:cid>')
def get_candidate(cid):
    conn = get_db()
    r = conn.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
    if not r: conn.close(); return jsonify({'error': 'Not found'}), 404
    d = _cand_public(r)   # drop embedding / embedding_text / embedding_vec
    try: d['key_skills'] = json.loads(d['key_skills'] or '[]')
    except: d['key_skills'] = []
    try: d['secondary_skills'] = json.loads(d['secondary_skills'] or '[]')
    except: d['secondary_skills'] = []
    hist = conn.execute('SELECT * FROM stage_history WHERE candidate_id=? ORDER BY created_at', (cid,)).fetchall()
    d['history'] = [dict(h) for h in hist]
    wh = conn.execute('SELECT * FROM work_history WHERE candidate_id=? ORDER BY is_current DESC, sort_order ASC, id ASC', (cid,)).fetchall()
    d['work_history'] = [dict(w) for w in wh]
    conn.close()
    return jsonify(d)

@app.route('/api/candidates/<int:cid>/move', methods=['POST'])
@login_required
def move_candidate(cid):
    """Move a candidate to a different mandate (within the same tenant)."""
    d = request.json or {}
    target_mid = d.get('mandate_id')
    if not target_mid:
        return jsonify({'error': 'Target mandate required'}), 400
    conn = get_db()
    cand = conn.execute('SELECT mandate_id, name FROM candidates WHERE id=?', (cid,)).fetchone()
    if not cand:
        conn.close(); return jsonify({'error': 'Candidate not found'}), 404
    # Verify target mandate belongs to this tenant
    tgt = conn.execute('SELECT id, role, client, owner_id FROM mandates WHERE id=?', (target_mid,)).fetchone()
    if not tgt or tgt['owner_id'] != effective_company_id():
        conn.close(); return jsonify({'error': 'Target mandate not found'}), 404
    old = conn.execute('SELECT role FROM mandates WHERE id=?', (cand['mandate_id'],)).fetchone()
    old_label = old['role'] if old else 'previous mandate'
    conn.execute('UPDATE candidates SET mandate_id=?, updated_at=? WHERE id=?', (target_mid, ts(), cid))
    conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                 (cid, '', '', f'Moved from "{old_label}" to "{tgt["role"]}"', ts()))
    conn.commit(); conn.close()
    log_candidate_event(cid, 'note', f'Moved to mandate: {tgt["role"]} ({tgt["client"]})')
    return jsonify({'ok': True, 'mandate_id': target_mid})


@app.route('/api/candidates/<int:cid>', methods=['PUT'])
def update_candidate(cid):
    d = request.json or {}
    conn = get_db()
    c = conn.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
    if not c: conn.close(); return jsonify({'error': 'Not found'}), 404

    fields = ['name','company','designation','experience','ctc_current','ctc_expected',
              'notice_period','location','preferred_location','phone','email','qualification','career_summary',
              'key_skills','secondary_skills','recruiter_feedback','client_feedback','general_comments',
              'linkedin_url','ai_insight_cv']
    sets = []; vals = []
    for f in fields:
        if f in d:
            sets.append(f + '=?')
            val = d[f]
            if isinstance(val, (list, dict)): val = json.dumps(val)
            vals.append(val)

    if sets:
        vals += [ts(), cid]
        conn.execute('UPDATE candidates SET ' + ','.join(sets) + ',updated_at=? WHERE id=?', vals)

        # Build a human-readable list of what changed, for the journey
        labels = {
            'name':'Name','company':'Company','designation':'Designation',
            'experience':'Experience','ctc_current':'Current CTC','ctc_expected':'Expected CTC',
            'notice_period':'Notice period','location':'Location','preferred_location':'Preferred location','phone':'Phone','email':'Email',
            'qualification':'Qualification','career_summary':'Summary',
            'linkedin_url':'LinkedIn URL','ai_insight_cv':'AI Insight (CV)'
        }
        changes = []
        for f, lbl in labels.items():
            if f in d:
                old_v = c[f] if f in c.keys() else ''
                new_v = d[f]
                if str(old_v or '') != str(new_v or ''):
                    if new_v not in (None, '', 0):
                        changes.append(f"{lbl}: {old_v or '—'} \u2192 {new_v}")
        # Skills change
        if 'key_skills' in d:
            try:
                new_skills = d['key_skills'] if isinstance(d['key_skills'], list) else json.loads(d['key_skills'] or '[]')
                old_skills = json.loads(c['key_skills'] or '[]')
                if set(new_skills) != set(old_skills):
                    changes.append('Skills updated')
            except Exception:
                pass

        notes = []
        if 'recruiter_feedback' in d and d['recruiter_feedback'] and d['recruiter_feedback'] != (c['recruiter_feedback'] or ''):
            notes.append('Recruiter feedback updated')
        if 'client_feedback' in d and d['client_feedback'] and d['client_feedback'] != (c['client_feedback'] or ''):
            notes.append('Client feedback updated')

        conn.commit(); conn.close()

        # Log each change to the journey as an edit event
        if changes:
            u = current_user()
            who = (u.get('display_name') or u.get('username') or '') if u else ''
            detail = 'Profile updated \u2014 ' + '; '.join(changes) + (f' (by {who})' if who else '')
            log_candidate_event(cid, 'edit', detail)
        for note in notes:
            log_candidate_event(cid, 'note', note)
        return jsonify({'ok': True})
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/candidates/<int:cid>/stage', methods=['POST'])
@login_required
def move_stage(cid):
    # Freelancers cannot change stages
    try:
        from modules.freelancer import block_if_freelancer
        blocked = block_if_freelancer()
        if blocked:
            return blocked
    except Exception:
        pass
    d = request.json or {}
    conn = get_db()
    r = conn.execute('SELECT stage FROM candidates WHERE id=?', (cid,)).fetchone()
    if not r: conn.close(); return jsonify({'error': 'Not found'}), 404
    old_stage = r['stage']
    # keep_stage=true means just add a note to history without changing stage
    keep_stage = d.get('keep_stage', False)
    new_stage = old_stage if keep_stage else d.get('stage', old_stage)
    if not keep_stage:
        conn.execute('UPDATE candidates SET stage=?,updated_at=? WHERE id=?', (new_stage, ts(), cid))
    conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                 (cid, old_stage, new_stage, d.get('note',''), ts()))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/mandates/<int:mid>/candidates/manual', methods=['POST'])
def add_manual(mid):
    d = request.json or {}
    if not d.get('name') or not d.get('company'):
        return jsonify({'error': 'Name and Company are required'}), 400
    conn = get_db(); c = conn.cursor()
    c.execute(
        'INSERT INTO candidates (mandate_id,name,company,designation,experience,ctc_current,'
        'ctc_expected,notice_period,location,phone,email,career_summary,key_skills,'
        'screening_decision,ai_reasoning,stage,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (mid, d['name'], d['company'], d.get('designation',''), float(d.get('experience') or 0),
         float(d.get('ctc_current') or 0), float(d.get('ctc_expected') or 0), int(d.get('notice_period') or 0),
         d.get('location',''), d.get('phone',''), d.get('email',''), d.get('career_summary',''),
         json.dumps(d.get('key_skills') or []), 'worth_opening', 'Manually added', 'Screening', ts(), ts()))
    cid = c.lastrowid
    c.execute('UPDATE candidates SET qualification=?, preferred_location=? WHERE id=?',
              (d.get('qualification',''), d.get('preferred_location',''), cid))
    c.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
              (cid, '', 'Screening', 'Manually added to pipeline', ts()))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'id': cid})

# CV
@app.route('/api/candidates/<int:cid>/cv', methods=['POST', 'OPTIONS'])
def upload_cv(cid):
    if request.method == 'OPTIONS':
        return ('', 204)
    if not session.get('user_id'):
        return jsonify({'error': 'auth_required', 'message': 'Please log into HireLab first.'}), 401
    if 'cv' not in request.files: return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['cv']
    ext = Path(f.filename).suffix.lower()
    if ext not in ['.pdf', '.doc', '.docx']: return jsonify({'error': 'PDF or Word files only'}), 400
    fname = 'c' + str(cid) + '_' + datetime.datetime.now().strftime('%Y%m%d%H%M%S') + ext
    f.save(os.path.join(CV_DIR, fname))
    conn = get_db()
    old = conn.execute('SELECT cv_path FROM candidates WHERE id=?', (cid,)).fetchone()
    if old and old['cv_path']:
        op = os.path.join(CV_DIR, old['cv_path'])
        if os.path.exists(op): os.remove(op)
    conn.execute('UPDATE candidates SET cv_path=?,cv_original_name=?,updated_at=? WHERE id=?', (fname, f.filename, ts(), cid))
    conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                 (cid, '', '', 'CV uploaded: ' + f.filename, ts()))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'filename': fname, 'original': f.filename})

@app.route('/api/cv/<path:filename>')
def serve_cv(filename):
    fp = os.path.join(CV_DIR, filename)
    return send_file(fp) if os.path.exists(fp) else (jsonify({'error': 'Not found'}), 404)


@app.route('/api/cv-view/<path:filename>')
def view_cv_html(filename):
    """Render a .docx CV as HTML so it can be shown inline in the browser
    (browsers can show PDF in an iframe natively, but not Word files)."""
    fp = os.path.join(CV_DIR, filename)
    if not os.path.exists(fp):
        return ('<p style="font-family:sans-serif;padding:20px;color:#888">CV file not found.</p>', 404)
    ext = os.path.splitext(filename)[1].lower()
    if ext != '.docx':
        return ('<p style="font-family:sans-serif;padding:20px;color:#888">Preview only supports .docx. Please download to view.</p>', 200)
    try:
        import mammoth
        with open(fp, 'rb') as f:
            result = mammoth.convert_to_html(f)
        body = result.value or '<p style="color:#888">(Empty document)</p>'
        page = (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<style>'
            'body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:13px;'
            'line-height:1.6;color:#222;max-width:800px;margin:0 auto;padding:28px 32px;background:#fff}'
            'h1,h2,h3{color:#0E2A47;margin:16px 0 8px} p{margin:6px 0} '
            'table{border-collapse:collapse;width:100%;margin:10px 0} '
            'td,th{border:1px solid #ddd;padding:6px 8px;font-size:12px} '
            'ul,ol{margin:6px 0 6px 22px} img{max-width:100%}'
            '</style></head><body>' + body + '</body></html>'
        )
        return (page, 200, {'Content-Type': 'text/html; charset=utf-8'})
    except Exception as e:
        return ('<p style="font-family:sans-serif;padding:20px;color:#C0522B">Could not render this Word file: '
                + str(e) + '. Please download to view.</p>', 200)

@app.route('/api/candidates/<int:cid>/cv', methods=['DELETE'])
def delete_cv(cid):
    conn = get_db()
    r = conn.execute('SELECT cv_path FROM candidates WHERE id=?', (cid,)).fetchone()
    if r and r['cv_path']:
        fp = os.path.join(CV_DIR, r['cv_path'])
        if os.path.exists(fp): os.remove(fp)
        conn.execute('UPDATE candidates SET cv_path="",cv_original_name="",updated_at=? WHERE id=?', (ts(), cid))
        conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                     (cid, '', '', 'CV removed', ts()))
        conn.commit()
    conn.close()
    return jsonify({'ok': True})


# Delete candidate
@app.route('/api/candidates/<int:cid>/work-history', methods=['POST'])
@login_required
def save_work_history(cid):
    """Replace the full work-history list for a candidate."""
    d = request.json or {}
    items = d.get('items', [])
    conn = get_db()
    # ownership check
    own = conn.execute('SELECT owner_id FROM candidates WHERE id=?', (cid,)).fetchone()
    if not own or own['owner_id'] != effective_user_id():
        conn.close(); return jsonify({'error': 'Not found'}), 404
    conn.execute('DELETE FROM work_history WHERE candidate_id=?', (cid,))
    for i, it in enumerate(items):
        conn.execute(
            'INSERT INTO work_history (candidate_id,company,designation,start_date,end_date,is_current,description,sort_order) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (cid, (it.get('company') or '').strip(), (it.get('designation') or '').strip(),
             (it.get('start_date') or '').strip(), (it.get('end_date') or '').strip(),
             1 if it.get('is_current') else 0, (it.get('description') or '').strip(), i)
        )
    conn.commit(); conn.close()
    _xp_recompute_safe(cid)
    return jsonify({'ok': True, 'count': len(items)})


def _xp_recompute_safe(cid):
    """Best-effort recompute of experience intelligence; never breaks the caller
    if the xp module isn't loaded or the engine errors on one candidate."""
    try:
        from modules.xp_engine import feedback_loop as _fb
        _conn = get_db()
        _fb.recompute_candidate(_conn, cid)
        _conn.close()
    except Exception as _e:
        print(f'[xp] recompute skipped for {cid}: {_e}')


@app.route('/api/candidates/<int:cid>', methods=['DELETE'])
def delete_candidate(cid):
    conn = get_db()
    r = conn.execute('SELECT cv_path FROM candidates WHERE id=?', (cid,)).fetchone()
    if r:
        if r['cv_path']:
            fp = os.path.join(CV_DIR, r['cv_path'])
            if os.path.exists(fp):
                try: os.remove(fp)
                except: pass
        conn.execute('DELETE FROM stage_history WHERE candidate_id=?', (cid,))
        conn.execute('DELETE FROM candidates WHERE id=?', (cid,))
        conn.commit()
    conn.close()
    return jsonify({'ok': True})

# DeepSeek parse
@app.route('/api/parse-naukri', methods=['POST'])
def parse_naukri():
    d = request.json or {}
    key = get_setting('deepseek_api_key') or d.get('deepseek_api_key', '')
    raw = d.get('raw', '').strip()
    if not key: return jsonify({'error': 'DeepSeek API key not set. Go to Settings.'}), 400
    if not raw: return jsonify({'error': 'No text provided'}), 400
    system_msg = ('Extract candidate details from recruiter text. Return ONLY valid JSON with these fields: '
                  'name, phone, email, company, designation, experience (float years), '
                  'ctc_current (float LPA), ctc_expected (float LPA), notice_period (int days), '
                  'location (current city), preferred_location (preferred/desired job location, if mentioned), '
                  'qualification (highest education degree e.g. B.Tech, MBA), key_skills (array max 6), secondary_skills (array), '
                  'career_summary (2 sentences), is_mnc (bool). '
                  'Use null for missing strings, 0 for missing numbers.')
    try:
        resp = call_deepseek(key,
            {'model': 'deepseek-chat', 'temperature': 0, 'max_tokens': 800,
                  'messages': [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': raw}],
                  'response_format': {'type': 'json_object'}},
            timeout=30, endpoint='parse')
    except requests.Timeout: return jsonify({'error': 'DeepSeek timeout. Try again.'}), 504
    except Exception as e: return jsonify({'error': str(e)}), 500
    if resp.status_code == 401: return jsonify({'error': 'Invalid DeepSeek API key'}), 401
    if resp.status_code != 200: return jsonify({'error': 'DeepSeek error: ' + resp.text[:150]}), 500
    text = resp.json()['choices'][0]['message']['content']
    parsed = parse_json(text)
    return jsonify({'ok': True, 'data': parsed}) if parsed else (jsonify({'error': 'Parse failed'}), 500)


# ── Resume Text Extraction ───────────────────────────────────────────────────
def extract_text_from_file(file_bytes, filename):
    """Extract plain text from PDF or Word file."""
    ext = Path(filename).suffix.lower()
    text = ''
    try:
        if ext == '.pdf':
            if HAS_PDF:
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
            else:
                return None, 'pdfplumber not installed. Run: pip install pdfplumber'
        elif ext in ['.docx']:
            if HAS_DOCX:
                doc = DocxDocument(io.BytesIO(file_bytes))
                text = '\n'.join(p.text for p in doc.paragraphs if p.text.strip())
            else:
                return None, 'python-docx not installed. Run: pip install python-docx'
        elif ext == '.doc':
            return None, '.doc format not supported. Please convert to .docx or .pdf'
        else:
            return None, 'Unsupported file type'
        return text.strip() if text.strip() else None, None
    except Exception as e:
        return None, str(e)

# ── Parse Resume (PDF/Word → DeepSeek → candidate fields) ────────────────────
@app.route('/api/parse-resume', methods=['POST'])
def parse_resume():
    if 'resume' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['resume']
    ds_key = get_setting('deepseek_api_key') or request.form.get('deepseek_api_key', '')
    if not ds_key:
        return jsonify({'error': 'DeepSeek API key required. Add in Settings.'}), 400

    file_bytes = f.read()
    text, err = extract_text_from_file(file_bytes, f.filename)
    if err:
        return jsonify({'error': err}), 400
    if not text or len(text) < 50:
        return jsonify({'error': 'Could not extract text from file. Try PDF or DOCX format.'}), 400

    system_msg = ('Extract candidate details from this resume/CV text. Return ONLY valid JSON with: '
                  'name, phone, email, company (current), designation (current title), '
                  'experience (float years total), ctc_current (float LPA, 0 if not found), '
                  'ctc_expected (float LPA, 0 if not found), notice_period (int days, 0 if not found), '
                  'location (current city), preferred_location (preferred/desired job location if mentioned), qualification (highest degree), '
                  'key_skills (array of top 8 technical/domain skills), '
                  'secondary_skills (array of other skills), '
                  'career_summary (2-3 sentences about background and strengths), '
                  'industry_background (e.g. FMCG, Manufacturing, IT), is_mnc (bool). '
                  'Use null for missing strings, 0 for missing numbers.')
    try:
        resp = call_deepseek(ds_key,
            {'model': 'deepseek-chat', 'temperature': 0, 'max_tokens': 1000,
                  'messages': [{'role': 'system', 'content': system_msg},
                                {'role': 'user', 'content': 'Extract from this resume:\n\n' + text[:8000]}],
                  'response_format': {'type': 'json_object'}},
            timeout=45, endpoint='resume-parse')
    except requests.Timeout:
        return jsonify({'error': 'DeepSeek timeout — try again'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    if resp.status_code == 401: return jsonify({'error': 'Invalid DeepSeek API key'}), 401
    if resp.status_code != 200: return jsonify({'error': 'DeepSeek error: ' + resp.text[:150]}), 500

    raw = resp.json()['choices'][0]['message']['content']
    parsed = parse_json(raw)
    return jsonify({'ok': True, 'data': parsed, 'text_length': len(text)}) if parsed else (jsonify({'error': 'Parse failed', 'raw': raw[:300]}), 500)

# ── Bulk Parse Multiple Naukri Snippets ───────────────────────────────────────
@app.route('/api/parse-naukri-bulk', methods=['POST'])
def parse_naukri_bulk():
    d = request.json or {}
    ds_key = get_setting('deepseek_api_key') or d.get('deepseek_api_key', '')
    raw = d.get('raw', '').strip()
    if not ds_key: return jsonify({'error': 'DeepSeek API key required'}), 400
    if not raw:    return jsonify({'error': 'No content provided'}), 400

    system_msg = (
        'You are parsing multiple candidate profiles from Naukri or recruiter notes. '
        'Extract each candidate and return a JSON ARRAY (not object). '
        'Each element must have: name, phone, email, company, designation, '
        'experience (float years), ctc_current (float LPA), ctc_expected (float LPA), '
        'notice_period (int days), location, qualification, '
        'key_skills (array max 6), career_summary (1-2 sentences), is_mnc (bool). '
        'Use null for missing strings, 0 for missing numbers. '
        'IMPORTANT: Return an ARRAY even if there is only one candidate. '
        'Separate candidates by looking for new profile headers, numbers, or clear breaks.'
    )
    try:
        resp = call_deepseek(ds_key,
            {'model': 'deepseek-chat', 'temperature': 0, 'max_tokens': 3000,
                  'messages': [{'role': 'system', 'content': system_msg},
                                {'role': 'user', 'content': 'Extract all candidates from:\n\n' + raw}]},
            timeout=60, endpoint='bulk-parse')
    except requests.Timeout:
        return jsonify({'error': 'DeepSeek timeout — try again'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    if resp.status_code == 401: return jsonify({'error': 'Invalid DeepSeek API key'}), 401
    if resp.status_code != 200: return jsonify({'error': 'DeepSeek error: ' + resp.text[:150]}), 500

    raw_resp = resp.json()['choices'][0]['message']['content']
    parsed = parse_json(raw_resp)
    if isinstance(parsed, dict): parsed = [parsed]   # single candidate returned as object
    if not isinstance(parsed, list): return jsonify({'error': 'Could not parse response', 'raw': raw_resp[:300]}), 500
    return jsonify({'ok': True, 'candidates': parsed, 'count': len(parsed)})

# ── Bulk Add Candidates ────────────────────────────────────────────────────────
@app.route('/api/mandates/<int:mid>/candidates/bulk', methods=['POST'])
def bulk_add_candidates(mid):
    d = request.json or {}
    candidates = d.get('candidates', [])
    if not candidates: return jsonify({'error': 'No candidates provided'}), 400

    conn = get_db(); c = conn.cursor()
    m = conn.execute('SELECT * FROM mandates WHERE id=?', (mid,)).fetchone()
    if not m: conn.close(); return jsonify({'error': 'Mandate not found'}), 404

    added = 0
    ids = []
    for cand in candidates:
        name    = str(cand.get('name') or '').strip()
        company = str(cand.get('company') or '').strip()
        if not name: continue
        c.execute(
            'INSERT INTO candidates (mandate_id,name,company,designation,experience,ctc_current,'
            'ctc_expected,notice_period,location,phone,email,career_summary,key_skills,'
            'screening_decision,ai_reasoning,stage,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (mid, name, company, cand.get('designation',''), float(cand.get('experience') or 0),
             float(cand.get('ctc_current') or 0), float(cand.get('ctc_expected') or 0),
             int(cand.get('notice_period') or 0), cand.get('location',''),
             cand.get('phone',''), cand.get('email',''), cand.get('career_summary',''),
             json.dumps(cand.get('key_skills') or []),
             'worth_opening', 'Manually added (bulk)', 'Screening', ts(), ts()))
        cid = c.lastrowid
        c.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                  (cid, '', 'Screening', 'Bulk added to pipeline', ts()))
        ids.append(cid)
        added += 1

    conn.commit(); conn.close()
    return jsonify({'ok': True, 'added': added, 'ids': ids})


# ── CALL RECORDING ANALYSIS ──────────────────────────────────────────────────

CALL_DIR = os.path.join(DATA_DIR, 'calls')
os.makedirs(CALL_DIR, exist_ok=True)

@app.route('/api/candidates/<int:cid>/analyse-call', methods=['POST'])
@login_required
def analyse_call(cid):
    # TENANT GUARD: the candidate must belong to the caller's company (tenant).
    _g = get_db()
    _own = _g.execute('SELECT owner_id FROM candidates WHERE id=?', (cid,)).fetchone()
    _g.close()
    if not _own or _own['owner_id'] != effective_company_id():
        return jsonify({'error': 'Candidate not in your workspace'}), 403

    language    = request.form.get('language', 'hi')   # hi = Hindi, en = English
    # Server keys (env var first, then DB) take priority over anything from frontend
    groq_key    = get_setting('groq_api_key') or request.form.get('groq_api_key', '').strip()
    claude_key  = get_setting('claude_api_key') or request.form.get('claude_api_key', '').strip()

    if not groq_key: return jsonify({'error': 'Groq API key required (for transcription). Add in Settings.'}), 400
    if not claude_key: return jsonify({'error': 'Claude API key required (for analysis). Add in Settings.'}), 400
    if 'recording' not in request.files: return jsonify({'error': 'No recording file uploaded'}), 400

    f = request.files['recording']
    ext = Path(f.filename).suffix.lower()
    allowed = ['.mp3', '.m4a', '.mp4', '.wav', '.ogg', '.webm', '.flac']
    if ext not in allowed:
        return jsonify({'error': f'Unsupported format. Use: {", ".join(allowed)}'}), 400

    # Save recording
    fname = f'call_{cid}_{datetime.datetime.now().strftime("%Y%m%d%H%M%S")}{ext}'
    fpath = os.path.join(CALL_DIR, fname)
    file_bytes = f.read()
    with open(fpath, 'wb') as out:
        out.write(file_bytes)

    # ── Step 1: Transcribe with Whisper ──────────────────────────────────────
    try:
        mime = {
            '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.mp4': 'audio/mp4',
            '.wav': 'audio/wav',  '.ogg': 'audio/ogg', '.webm': 'audio/webm',
            '.flac': 'audio/flac'
        }.get(ext, 'audio/mpeg')

        whisper_resp = requests.post(
            'https://api.groq.com/openai/v1/audio/transcriptions',
            headers={'Authorization': 'Bearer ' + groq_key},
            files={'file': (f.filename, file_bytes, mime)},
            data={'model': 'whisper-large-v3', 'language': language,
                  'response_format': 'verbose_json',
                  'prompt': 'This is a recruiter call with a candidate discussing a job opportunity. '
                            'The conversation may be in Hindi, English, or Hinglish.'},
            timeout=120
        )
    except requests.Timeout:
        return jsonify({'error': 'Whisper transcription timed out. Try a shorter recording.'}), 504
    except Exception as e:
        return jsonify({'error': 'Transcription error: ' + str(e)}), 500

    if whisper_resp.status_code == 401:
        return jsonify({'error': 'Invalid Groq API key'}), 401
    if whisper_resp.status_code != 200:
        try:
            err = whisper_resp.json().get('error', {}).get('message', whisper_resp.text[:200])
        except Exception:
            err = whisper_resp.text[:200]
        return jsonify({'error': 'Groq transcription error: ' + err}), 500

    _wjson = whisper_resp.json()
    transcript = _wjson.get('text', '').strip()
    # Log transcription cost per tenant (Whisper bills by audio duration).
    try:
        log_api_usage('groq', 'whisper-large-v3',
                      audio_seconds=float(_wjson.get('duration', 0) or 0),
                      endpoint='transcription')
    except Exception:
        pass
    if not transcript:
        return jsonify({'error': 'Whisper returned empty transcript. Check recording quality.'}), 400

    # ── Step 2: Get candidate + mandate context ───────────────────────────────
    conn = get_db()
    cand = conn.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
    if not cand: conn.close(); return jsonify({'error': 'Candidate not found'}), 404
    mandate = conn.execute('SELECT * FROM mandates WHERE id=?', (cand['mandate_id'],)).fetchone()
    conn.close()

    # Get CV text if available
    cv_text = ''
    if cand['cv_path']:
        cv_path = os.path.join(CV_DIR, cand['cv_path'])
        if os.path.exists(cv_path):
            cv_ext = Path(cv_path).suffix.lower()
            try:
                if cv_ext == '.pdf' and HAS_PDF:
                    import pdfplumber
                    with pdfplumber.open(cv_path) as pdf:
                        cv_text = '\n'.join(p.extract_text() or '' for p in pdf.pages)[:4000]
                elif cv_ext in ['.docx'] and HAS_DOCX:
                    from docx import Document as DocxDocument
                    doc = DocxDocument(cv_path)
                    cv_text = '\n'.join(p.text for p in doc.paragraphs if p.text.strip())[:4000]
            except Exception:
                pass

    jd_or_sop = (mandate['sop_text'] or html_to_text(mandate['jd']) or '') if mandate else ''
    cand_name  = cand['name'] or 'Candidate'
    role       = mandate['role'] if mandate else 'the position'
    client     = mandate['client'] if mandate else ''

    # ── Step 3: Claude Analysis ───────────────────────────────────────────────
    system_msg = (
        'You are an expert recruitment analyst. Analyse a recruiter-candidate call.\n'
        'Return ONLY valid JSON — no markdown, no explanation.\n\n'
        'JSON structure:\n'
        '{\n'
        '  "interest_level": "HIGH" | "MEDIUM" | "LOW",\n'
        '  "interest_reason": "one sentence why",\n'
        '  "ctc_discussed": null or float (current CTC in LPA the candidate states they earn now),\n'
        '  "ctc_expected_discussed": null or float (expected/asking CTC in LPA, if mentioned),\n'
        '  "current_company_discussed": null or "the company the candidate currently works at, if stated",\n'
        '  "notice_negotiable": true | false | null,\n'
        '  "notice_discussed_days": null or int,\n'
        '  "key_concerns": ["concern1", "concern2"],\n'
        '  "candidate_strengths": ["strength1", "strength2"],\n'
        '  "red_flags": ["flag1"] or [],\n'
        '  "fit_vs_jd": "STRONG" | "MODERATE" | "WEAK",\n'
        '  "fit_reason": "one sentence",\n'
        '  "next_step": "specific action recruiter should take",\n'
        '  "next_step_deadline": "e.g. by Wednesday" or null,\n'
        '  "recommendation": "PROCEED" | "HOLD" | "REJECT",\n'
        '  "recommendation_reason": "one sentence",\n'
        '  "call_summary": "3-4 sentences covering the full conversation",\n'
        '  "key_quotes": ["notable quote 1", "notable quote 2"],\n'
        '  "languages_detected": "Hindi / English / Hinglish"\n'
        '}'
    )

    user_msg = (
        'CANDIDATE: ' + cand_name + '\n'
        'ROLE: ' + role + ((' at ' + client) if client else '') + '\n\n'
        + ('JD / SOP:\n' + jd_or_sop[:2000] + '\n\n' if jd_or_sop else '')
        + ('CV / RESUME (extracted text):\n' + cv_text[:2000] + '\n\n' if cv_text else '')
        + 'CALL TRANSCRIPT:\n' + transcript[:6000]
    )

    claude_resp = call_claude(claude_key, system_msg, [{'role': 'user', 'content': user_msg}], max_tokens=1500)
    if claude_resp.status_code != 200:
        try: err = claude_resp.json().get('error', {}).get('message', 'Claude error')
        except Exception: err = claude_resp.text[:200]
        return jsonify({'error': 'Analysis failed: ' + err, 'transcript': transcript}), 500

    analysis_text = claude_resp.json()['content'][0]['text']
    analysis = parse_json(analysis_text)
    if not analysis:
        return jsonify({'error': 'Could not parse analysis', 'transcript': transcript, 'raw': analysis_text[:500]}), 500

    # ── Step 4: Save to DB ────────────────────────────────────────────────────
    analysis_str = json.dumps(analysis, ensure_ascii=False)
    conn = get_db()
    # Auto-update candidate fields from the call (roadmap: update CTC & company
    # from the call). Only overwrite when the call actually surfaced a value.
    updates = {}
    try:
        ctc = analysis.get('ctc_discussed')
        if ctc is not None and float(ctc) > 0:
            updates['ctc_current'] = float(ctc)
    except (TypeError, ValueError):
        pass
    try:
        ctc_e = analysis.get('ctc_expected_discussed')
        if ctc_e is not None and float(ctc_e) > 0:
            updates['ctc_expected'] = float(ctc_e)
    except (TypeError, ValueError):
        pass
    comp = (analysis.get('current_company_discussed') or '').strip()
    if comp:
        updates['company'] = comp
    try:
        nd = analysis.get('notice_discussed_days')
        if nd is not None and int(nd) >= 0:
            updates['notice_period'] = int(nd)
    except (TypeError, ValueError):
        pass

    note = '[CALL ANALYSIS ' + datetime.datetime.now().strftime('%d %b %Y %H:%M') + '] Recorded. Interest: ' + analysis.get('interest_level', '') + '. ' + analysis.get('call_summary', '')[:200]
    if updates:
        set_clause = ', '.join(f'{k}=?' for k in updates) + ', general_comments=?, updated_at=? WHERE id=?'
        conn.execute('UPDATE candidates SET ' + set_clause,
                     tuple(updates.values()) + (note, ts(), cid))
    else:
        conn.execute('UPDATE candidates SET general_comments=?,updated_at=? WHERE id=?',
                     (note, ts(), cid))
    upd_summary = ', '.join(f'{k}→{v}' for k, v in updates.items()) if updates else ''
    conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                 (cid, cand['stage'], cand['stage'],
                  'Call analysed. Interest: ' + analysis.get('interest_level','') + '. Rec: ' + analysis.get('recommendation','') + '. ' + analysis.get('next_step','') + (' | Updated: ' + upd_summary if upd_summary else ''),
                  ts()))
    conn.commit(); conn.close()
    _interest = analysis.get('interest_level', '')
    log_candidate_event(cid, 'call', 'Call analysed' + (' — interest: ' + _interest if _interest else '') + (' · updated ' + upd_summary if upd_summary else ''))

    return jsonify({
        'ok': True,
        'transcript': transcript,
        'analysis': analysis,
        'recording_file': fname,
        'updated_fields': updates,
        'cv_used': bool(cv_text),
        'jd_used': bool(jd_or_sop)
    })

@app.route('/api/calls/<path:filename>')
@login_required
def serve_call(filename):
    # Recordings are named call_<candidateId>_<timestamp>.<ext>. Verify the
    # candidate belongs to the caller's tenant before serving the audio.
    m = re.match(r'call_(\d+)_', os.path.basename(filename))
    if not m:
        return jsonify({'error': 'Not found'}), 404
    cid = int(m.group(1))
    conn = get_db()
    own = conn.execute('SELECT owner_id FROM candidates WHERE id=?', (cid,)).fetchone()
    conn.close()
    if not own or own['owner_id'] != effective_company_id():
        return jsonify({'error': 'Not found'}), 404
    fp = os.path.join(CALL_DIR, os.path.basename(filename))
    return send_file(fp) if os.path.exists(fp) else (jsonify({'error': 'Not found'}), 404)



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CENTRAL DATABASE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_or_create_central_mandate():
    """Returns the ID of the Central Database mandate."""
    conn = get_db()
    r = conn.execute("SELECT value FROM settings WHERE key='central_mandate_id'").fetchone()
    if r and r['value']:
        conn.close()
        return int(r['value'])
    # Create central mandate
    c = conn.cursor()
    c.execute("INSERT INTO mandates (client,role,location,ctc_min,ctc_max,status,created_at) VALUES (?,?,?,?,?,?,?)",
              ('HireLab', 'Central Database', 'All', 0, 99, 'active', ts()))
    mid = c.lastrowid
    c.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('central_mandate_id',?)", (str(mid),))
    conn.commit(); conn.close()
    return mid

@app.route('/api/central-db/search')
@login_required
def central_search():
    q        = request.args.get('q', '').strip().lower()
    location = request.args.get('location', '').strip().lower()
    phone    = request.args.get('phone', '').strip()
    ctc_min  = request.args.get('ctc_min', '')
    ctc_max  = request.args.get('ctc_max', '')
    exp_min  = request.args.get('exp_min', '')
    exp_max  = request.args.get('exp_max', '')
    notice   = request.args.get('notice', '')
    page     = int(request.args.get('page', 1))
    per_page = 30

    conn = get_db()
    # TENANT ISOLATION: only this company's candidates. owner_id stores the
    # tenant (company) id, so this scopes the Central Database to the current
    # agency. Without this filter, agencies would see each other's candidates.
    rows = conn.execute(
        'SELECT c.*, m.role as mandate_role, m.client as mandate_client '
        'FROM candidates c LEFT JOIN mandates m ON c.mandate_id = m.id '
        'WHERE c.owner_id = ? '
        'ORDER BY c.created_at DESC',
        (effective_company_id(),)
    ).fetchall()
    conn.close()

    results = []
    for r in rows:
        d = _cand_public(r)   # drop embedding / embedding_text / embedding_vec (not needed by UI)
        # Apply filters
        if q:
            searchable = ' '.join([
                str(d.get('name') or ''),
                str(d.get('company') or ''),
                str(d.get('designation') or ''),
                str(d.get('key_skills') or ''),
                str(d.get('career_summary') or ''),
                str(d.get('location') or ''),
                str(d.get('industry_background') or ''),
                str(d.get('email') or ''),
                str(d.get('phone') or ''),
            ]).lower()
            if q not in searchable: continue
        if location and location not in (d.get('location') or '').lower(): continue
        if phone and phone not in (d.get('phone') or ''): continue
        if ctc_min:
            try:
                if (d.get('ctc_current') or 0) < float(ctc_min): continue
            except: pass
        if ctc_max:
            try:
                if (d.get('ctc_current') or 0) > float(ctc_max): continue
            except: pass
        if exp_min:
            try:
                if (d.get('experience') or 0) < float(exp_min): continue
            except: pass
        if exp_max:
            try:
                if (d.get('experience') or 0) > float(exp_max): continue
            except: pass
        if notice:
            try:
                if (d.get('notice_period') or 0) > int(notice): continue
            except: pass
        try: d['key_skills'] = json.loads(d['key_skills'] or '[]')
        except: d['key_skills'] = []
        results.append(d)

    total = len(results)
    start = (page - 1) * per_page
    paginated = results[start:start + per_page]
    return jsonify({'ok': True, 'total': total, 'page': page, 'candidates': paginated})

@app.route('/api/central-db/add', methods=['POST'])
@login_required
def central_db_add():
    d   = request.json or {}
    mid = get_or_create_central_mandate()
    if not d.get('name') or not d.get('company'):
        return jsonify({'error': 'Name and Company required'}), 400
    tenant = effective_company_id()
    conn = get_db(); c = conn.cursor()
    c.execute(
        'INSERT INTO candidates (mandate_id,name,company,designation,experience,ctc_current,'
        'ctc_expected,notice_period,location,phone,email,career_summary,key_skills,'
        'screening_decision,ai_reasoning,stage,created_at,updated_at,owner_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (mid, d['name'], d['company'], d.get('designation',''), float(d.get('experience') or 0),
         float(d.get('ctc_current') or 0), float(d.get('ctc_expected') or 0),
         int(d.get('notice_period') or 0), d.get('location',''), d.get('phone',''), d.get('email',''),
         d.get('career_summary',''), json.dumps(d.get('key_skills') or []),
         'worth_opening', 'Added to Central Database', 'Central DB', ts(), ts(), tenant))
    cid = c.lastrowid
    c.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
              (cid, '', 'Central DB', 'Added to Central Database', ts()))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'id': cid})

@app.route('/api/central-db/bulk', methods=['POST'])
@login_required
def central_db_bulk():
    d   = request.json or {}
    mid = get_or_create_central_mandate()
    tenant = effective_company_id()
    candidates = d.get('candidates', [])
    conn = get_db(); c = conn.cursor()
    added = 0
    for cand in candidates:
        name = str(cand.get('name') or '').strip()
        company = str(cand.get('company') or '').strip()
        if not name: continue
        c.execute(
            'INSERT INTO candidates (mandate_id,name,company,designation,experience,ctc_current,'
            'ctc_expected,notice_period,location,phone,email,career_summary,key_skills,'
            'screening_decision,ai_reasoning,stage,created_at,updated_at,owner_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (mid, name, company, cand.get('designation',''), float(cand.get('experience') or 0),
             float(cand.get('ctc_current') or 0), float(cand.get('ctc_expected') or 0),
             int(cand.get('notice_period') or 0), cand.get('location',''),
             cand.get('phone',''), cand.get('email',''), cand.get('career_summary',''),
             json.dumps(cand.get('key_skills') or []),
             'worth_opening', 'Bulk added to Central Database', 'Central DB', ts(), ts(), tenant))
        cid = c.lastrowid
        c.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                  (cid, '', 'Central DB', 'Bulk added', ts()))
        added += 1
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'added': added})



# ── WhatsApp Response Tracking ────────────────────────────────────────────────
@app.route('/api/candidates/<int:cid>/wa-response', methods=['POST'])
def mark_wa_response(cid):
    d = request.json or {}
    response   = d.get('response', '')   # interested / callback / not_interested / no_reply
    note       = d.get('note', '')
    conn = get_db()
    cand = conn.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
    if not cand: conn.close(); return jsonify({'error': 'Not found'}), 404

    conn.execute('UPDATE candidates SET wa_response=?, wa_response_note=?, wa_response_at=?, updated_at=? WHERE id=?',
                 (response, note, ts(), ts(), cid))

    # Also update stage based on response
    stage_map = {
        'interested':     'Interested',
        'callback':       'Follow Up 1',
        'not_interested': 'Not Interested',
        'no_reply':       cand['stage'],  # keep current stage
    }
    new_stage = stage_map.get(response, cand['stage'])
    if new_stage != cand['stage']:
        conn.execute('UPDATE candidates SET stage=? WHERE id=?', (new_stage, cid))
        conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                     (cid, cand['stage'], new_stage,
                      'WhatsApp response: ' + response + ((' — ' + note) if note else ''), ts()))
    else:
        conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                     (cid, cand['stage'], cand['stage'],
                      'WhatsApp response logged: ' + response + ((' — ' + note) if note else ''), ts()))

    conn.commit(); conn.close()
    return jsonify({'ok': True, 'stage': new_stage})

# ── WhatsApp Follow-up Queue ───────────────────────────────────────────────────
@app.route('/api/mandates/<int:mid>/wa-queue')
def wa_queue(mid):
    """Returns candidates needing WA action — sent but no response logged."""
    import datetime as dt
    now = dt.datetime.now()
    conn = get_db()
    cands = conn.execute(
        'SELECT * FROM candidates WHERE mandate_id=? AND stage NOT IN (?,?,?,?)',
        (mid, 'Screened-Out', 'Not Interested', 'Placed', 'Central DB')
    ).fetchall()
    conn.close()

    fu_due = []
    for c in cands:
        d = dict(c)
        try: d['key_skills'] = json.loads(d['key_skills'] or '[]')
        except: d['key_skills'] = []

        # Intro sent but no response
        if d.get('msg1_sent_at') and not d.get('wa_response'):
            sent = dt.datetime.fromisoformat(d['msg1_sent_at'])
            days_since = (now - sent).days
            msg_type = 'msg1'
            if d.get('fu2_sent_at'):
                sent = dt.datetime.fromisoformat(d['fu2_sent_at'])
                days_since = (now - sent).days
                msg_type = 'fu2'
            elif d.get('fu1_sent_at'):
                sent = dt.datetime.fromisoformat(d['fu1_sent_at'])
                days_since = (now - sent).days
                msg_type = 'fu1'
            d['days_since_last_msg'] = days_since
            d['last_msg_type'] = msg_type
            d['last_msg_sent_at'] = sent.strftime('%d %b')
            fu_due.append(d)

    # Sort by days_since (longest first — most overdue)
    fu_due.sort(key=lambda x: x['days_since_last_msg'], reverse=True)
    return jsonify({'ok': True, 'queue': fu_due, 'count': len(fu_due)})



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CANDIDATE SUBMISSION FORM
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.route('/apply')
def apply_form():
    return send_file('apply.html')

@app.route('/api/public/parse-resume', methods=['POST'])
def public_parse_resume():
    if 'resume' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f = request.files['resume']
    text, err = extract_text_from_file(f.read(), f.filename)
    if err or not text:
        return jsonify({'error': err or 'Cannot extract text'}), 400
    ds_key = get_setting('deepseek_api_key')
    if not ds_key:
        return jsonify({'error': 'Resume parsing not configured. Please fill manually.'}), 400
    sys_msg = ('Extract candidate details. Return ONLY JSON: name, phone, email, company, designation, '
               'experience (float), ctc_current (float LPA), ctc_expected (float LPA), '
               'notice_period (int days), location, key_skills (array max 8). '
               'null for missing strings, 0 for missing numbers.')
    try:
        resp = call_deepseek(ds_key,
            {'model': 'deepseek-chat', 'temperature': 0, 'max_tokens': 800,
                  'messages': [{'role': 'system', 'content': sys_msg},
                                {'role': 'user', 'content': 'Extract:\n\n' + text[:8000]}],
                  'response_format': {'type': 'json_object'}},
            timeout=45, endpoint='extract')
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    if resp.status_code != 200:
        return jsonify({'error': 'Parse unavailable. Fill manually.'}), 500
    parsed = parse_json(resp.json()['choices'][0]['message']['content'])
    return jsonify({'ok': True, 'data': parsed}) if parsed else (jsonify({'error': 'Parse failed'}), 500)

@app.route('/api/submit', methods=['POST'])
def submit_form():
    name    = request.form.get('name', '').strip()
    phone   = request.form.get('phone', '').strip()
    email   = request.form.get('email', '').strip()
    company = request.form.get('company', '').strip()
    if not name or not phone or not email or not company:
        return jsonify({'error': 'Required fields missing'}), 400
    cv_path = ''; cv_name = ''
    if 'resume' in request.files:
        f = request.files['resume']
        if f.filename:
            ext  = Path(f.filename).suffix.lower()
            safe = str(int(datetime.datetime.now().timestamp())) + '_sub' + ext
            f.save(os.path.join(CV_DIR, safe))
            cv_path = safe; cv_name = f.filename
    conn = get_db(); c = conn.cursor()
    c.execute('INSERT INTO submissions (name,phone,email,company,designation,experience,'
              'ctc_current,ctc_expected,notice_period,location,key_skills,custom_fields,'
              'cv_path,cv_original_name,resume_parsed,status,created_at) '
              'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (name, phone, email, company,
         request.form.get('designation', ''),
         float(request.form.get('experience') or 0),
         float(request.form.get('ctc_current') or 0),
         float(request.form.get('ctc_expected') or 0),
         int(request.form.get('notice_period') or 0),
         request.form.get('location', ''),
         request.form.get('key_skills', '[]'),
         request.form.get('custom_fields', '{}'),
         cv_path, cv_name, 1 if cv_path else 0, 'new', ts()))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/submissions')
def get_submissions():
    q        = request.args.get('q', '').strip().lower()
    sf       = request.args.get('status', '')
    exp_r    = request.args.get('exp', '')      # e.g. "3-6"
    ctc_r    = request.args.get('ctc', '')      # e.g. "10-15"
    notice_r = request.args.get('notice', '')   # e.g. "1-30"
    loc_f    = request.args.get('loc', '').strip().lower()
    page     = int(request.args.get('page', 1)); per = 30

    conn  = get_db()
    rows  = conn.execute('SELECT * FROM submissions ORDER BY created_at DESC').fetchall()
    conn.close()

    def parse_range(s):
        try: lo, hi = s.split('-'); return float(lo), float(hi)
        except: return None, None

    results = []
    for r in rows:
        d = dict(r)
        try: d['key_skills'] = json.loads(d.get('key_skills') or '[]')
        except: d['key_skills'] = []
        try: d['custom_fields'] = json.loads(d.get('custom_fields') or '{}')
        except: d['custom_fields'] = {}

        # Status filter
        if sf and d.get('status') != sf: continue

        # Experience filter
        if exp_r:
            lo, hi = parse_range(exp_r)
            if lo is not None:
                exp = float(d.get('experience') or 0)
                if not (lo <= exp <= hi): continue

        # CTC filter
        if ctc_r:
            lo, hi = parse_range(ctc_r)
            if lo is not None:
                ctc = float(d.get('ctc_current') or 0)
                if not (lo <= ctc <= hi): continue

        # Notice period filter
        if notice_r:
            lo, hi = parse_range(notice_r)
            if lo is not None:
                notice = float(d.get('notice_period') or 0)
                if not (lo <= notice <= hi): continue

        # Location filter
        if loc_f and loc_f not in (d.get('location') or '').lower(): continue

        # Boolean text search
        if q:
            blob = ' '.join([d.get('name',''), d.get('company',''), d.get('designation',''),
                             d.get('location',''), d.get('email',''),
                             ' '.join(d.get('key_skills',[]))]).lower()
            if ' or ' in q:
                if not any(t.strip() in blob for t in q.split(' or ')): continue
            elif ' and ' in q:
                if not all(t.strip() in blob for t in q.split(' and ')): continue
            else:
                if q not in blob: continue

        results.append(d)

    total = len(results)
    return jsonify({'ok': True, 'total': total, 'page': page,
                    'submissions': results[(page-1)*per : page*per]})

@app.route('/api/submissions/<int:sid>', methods=['PUT'])
def update_submission(sid):
    d = request.json or {}
    conn = get_db()
    if 'status' in d:     conn.execute('UPDATE submissions SET status=? WHERE id=?',     (d['status'], sid))
    if 'notes' in d:      conn.execute('UPDATE submissions SET notes=? WHERE id=?',      (d['notes'], sid))
    if 'domain_tags' in d: conn.execute('UPDATE submissions SET domain_tags=? WHERE id=?', (json.dumps(d['domain_tags']), sid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/submissions/<int:sid>/add-to-pipeline', methods=['POST'])
def add_submission_to_pipeline(sid):
    d   = request.json or {}
    mid = d.get('mandate_id')
    if not mid: return jsonify({'error': 'mandate_id required'}), 400
    conn = get_db()
    sub  = conn.execute('SELECT * FROM submissions WHERE id=?', (sid,)).fetchone()
    if not sub: conn.close(); return jsonify({'error': 'Not found'}), 404
    c = conn.cursor()
    c.execute('INSERT INTO candidates (mandate_id,name,company,designation,experience,ctc_current,'
              'ctc_expected,notice_period,location,phone,email,key_skills,screening_decision,'
              'ai_reasoning,stage,cv_path,cv_original_name,created_at,updated_at) '
              'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (mid, sub['name'], sub['company'], sub['designation'], sub['experience'],
         sub['ctc_current'], sub['ctc_expected'], sub['notice_period'], sub['location'],
         sub['phone'], sub['email'], sub['key_skills'],
         'worth_opening', 'Added from submission form', 'Screening',
         sub['cv_path'], sub['cv_original_name'], ts(), ts()))
    cid = c.lastrowid
    c.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) '
              'VALUES (?,?,?,?,?)', (cid, '', 'Screening', 'Added from submission form', ts()))
    conn.execute('UPDATE submissions SET status=? WHERE id=?', ('added_to_pipeline', sid))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'candidate_id': cid})

@app.route('/api/form-config', methods=['GET', 'POST'])
def form_config():
    conn = get_db()
    if request.method == 'POST':
        conn.execute('INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)',
                     ('form_config', json.dumps(request.json or {})))
        conn.commit(); conn.close()
        return jsonify({'ok': True})
    r = conn.execute("SELECT value FROM settings WHERE key='form_config'").fetchone()
    conn.close()
    return jsonify({'ok': True, 'config': json.loads(r['value'] if r else '{}')})


# Intelligence

# Client Submission Sheet
@app.route('/api/mandates/<int:mid>/submission')
def client_submission(mid):
    conn = get_db()
    m = conn.execute('SELECT * FROM mandates WHERE id=?', (mid,)).fetchone()
    if not m: conn.close(); return jsonify({'error':'Not found'}), 404
    stage_filter = request.args.get('stage', 'Shared with Client')
    cands = conn.execute(
        'SELECT * FROM candidates WHERE mandate_id=? AND stage=? ORDER BY ai_score DESC',
        (mid, stage_filter)).fetchall()
    conn.close()

    date_str = datetime.date.today().strftime('%d %b %Y')
    rows_html = ''
    for i, c in enumerate(cands):
        skills_list = json.loads(c['key_skills'] or '[]')
        skills_str = ', '.join(skills_list[:5]) if skills_list else '--'
        bg = '#fafafa' if i % 2 else '#ffffff'
        ctc_curr = ('Rs ' + str(int(c['ctc_current'])) + 'L') if c['ctc_current'] else '--'
        ctc_exp  = ('Rs ' + str(int(c['ctc_expected'])) + 'L') if c['ctc_expected'] else '--'
        notice   = (str(c['notice_period']) + 'd') if c['notice_period'] else '--'
        rows_html += (
            '<tr style="background:' + bg + ';border-bottom:0.5px solid #f0f0f0">'
            '<td style="text-align:center;padding:9px 8px;font-weight:500;color:#888">' + str(i+1) + '</td>'
            '<td style="padding:9px 8px"><div style="font-weight:600;font-size:12px">' + (c['name'] or '--') + '</div>'
            '<div style="font-size:10px;color:#666;margin-top:2px">' + (c['designation'] or '--') + '</div></td>'
            '<td style="padding:9px 8px">' + (c['company'] or '--') + '</td>'
            '<td style="padding:9px 8px;text-align:center;font-weight:500;white-space:nowrap">' + ctc_curr + '</td>'
            '<td style="padding:9px 8px;text-align:center;font-weight:500;white-space:nowrap;color:#1D9E75">' + ctc_exp + '</td>'
            '<td style="padding:9px 8px;text-align:center;white-space:nowrap">' + notice + '</td>'
            '<td style="padding:9px 8px">' + (c['location'] or '--') + '</td>'
            '<td style="padding:9px 8px;font-size:10px;color:#555">' + skills_str + '</td>'
            '<td style="padding:9px 8px;font-size:10px;color:#444;max-width:180px">' + (c['career_summary'] or c['ai_reasoning'] or '--') + '</td>'
            '</tr>'
        )

    no_cands = '<div style="padding:20px;text-align:center;color:#888;font-size:12px;border:1px dashed #ddd;border-radius:6px">No candidates in <strong>' + stage_filter + '</strong> stage yet.</div>' if not cands else ''

    html = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<title>HireLab Client Submission</title>'
        '<style>'
        '*{box-sizing:border-box;margin:0;padding:0}'
        'body{font-family:-apple-system,"Segoe UI",sans-serif;font-size:11px;color:#1a1a1a;background:#fff;padding:32px}'
        'table{width:100%;border-collapse:collapse;margin-bottom:20px}'
        'thead{background:#0a2540;color:#fff;-webkit-print-color-adjust:exact;print-color-adjust:exact}'
        'th{padding:9px 8px;text-align:left;font-size:10px;font-weight:500;letter-spacing:.3px}'
        '@media print{.no-print{display:none}body{padding:12px}}'
        '</style></head><body>'

        # Print bar
        '<div class="no-print" style="background:#0a2540;color:#fff;padding:10px 32px;margin:-32px -32px 24px;display:flex;align-items:center;gap:12px">'
        '<span style="font-size:13px;font-weight:500">Client Submission Sheet</span>'
        '<button onclick="window.print()" style="margin-left:auto;background:#1D9E75;color:#fff;border:none;border-radius:5px;padding:7px 16px;font-size:12px;cursor:pointer">Print / Save PDF</button>'
        '<button onclick="window.close()" style="background:rgba(255,255,255,.1);color:#fff;border:0.5px solid rgba(255,255,255,.3);border-radius:5px;padding:7px 14px;font-size:12px;cursor:pointer;margin-left:6px">Close</button>'
        '</div>'

        # Header
        '<div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:20px;padding-bottom:14px;border-bottom:2px solid #1D9E75">'
        '<div>'
        '<div style="font-size:18px;font-weight:700;color:#0a2540">HireLab <span style="color:#1D9E75">Talent Resource</span></div>'
        '<div style="font-size:10px;color:#888;margin-top:2px">Intelligence-Led Recruitment  |  Ghaziabad, NCR</div>'
        '</div>'
        '<div style="text-align:right">'
        '<div style="font-size:20px;font-weight:700;color:#0a2540">Candidate Submission</div>'
        '<div style="font-size:12px;color:#666;margin-top:3px">' + (m['role'] or '') + '  |  ' + (m['client'] or '') + '</div>'
        '<div style="font-size:10px;color:#aaa;margin-top:2px">Date: ' + date_str + '  |  CONFIDENTIAL</div>'
        '</div></div>'

        # Mandate info
        '<div style="background:#f9f9f9;border:0.5px solid #e8e8e8;border-left:3px solid #1D9E75;border-radius:6px;padding:12px 16px;margin-bottom:18px;display:grid;grid-template-columns:repeat(5,1fr);gap:12px">'
        '<div><div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px">Position</div><div style="font-size:12px;font-weight:500">' + (m['role'] or '') + '</div></div>'
        '<div><div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px">Client</div><div style="font-size:12px;font-weight:500">' + (m['client'] or '') + '</div></div>'
        '<div><div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px">Location</div><div style="font-size:12px;font-weight:500">' + (m['location'] or '--') + '</div></div>'
        '<div><div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px">CTC Range</div><div style="font-size:12px;font-weight:500">Rs ' + str(int(m['ctc_min'] or 0)) + '--' + str(int(m['ctc_max'] or 0)) + ' LPA</div></div>'
        '<div><div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px">Profiles Shared</div><div style="font-size:22px;font-weight:700;color:#1D9E75">' + str(len(cands)) + '</div></div>'
        '</div>'

        # Table
        '<table>'
        '<thead><tr>'
        '<th style="width:30px;text-align:center">#</th>'
        '<th style="min-width:120px">Candidate</th>'
        '<th>Current Company</th>'
        '<th style="text-align:center">Curr CTC</th>'
        '<th style="text-align:center">Exp CTC</th>'
        '<th style="text-align:center">Notice</th>'
        '<th>Location</th>'
        '<th>Key Skills</th>'
        '<th style="min-width:150px">Summary</th>'
        '</tr></thead>'
        '<tbody>' + rows_html + '</tbody>'
        '</table>'
        + no_cands +

        # Footer
        '<div style="display:flex;align-items:center;justify-content:space-between;padding-top:14px;border-top:0.5px solid #e8e8e8;font-size:10px;color:#aaa">'
        '<span style="background:#FAEEDA;color:#854F0B;padding:3px 10px;border-radius:4px;font-weight:500">CONFIDENTIAL | For ' + (m['client'] or '') + ' use only</span>'
        '<span>HireLab Talent Resource | GSTIN: 09ECWPP1647A1Z9 | UDYAM: UP-29-0178859</span>'
        '<span>' + date_str + '</span>'
        '</div>'
        '</body></html>'
    )
    return Response(html, mimetype='text/html')


@app.route('/api/export')
def export_data():
    conn = get_db()
    candidates = [dict(r) for r in conn.execute('SELECT * FROM candidates').fetchall()]
    data = {
        'exported_at': ts(), 'app': 'HireLab Screener', 'version': '2.1',
        'mandates':   [dict(r) for r in conn.execute('SELECT * FROM mandates').fetchall()],
        'candidates': candidates,
        'history':    [dict(r) for r in conn.execute('SELECT * FROM stage_history').fetchall()],
        'settings':   {r['key']: r['value'] for r in conn.execute('SELECT * FROM settings').fetchall()},
    }
    conn.close()
    # Include actual CV files (PDF/Word) as base64 so they transfer with the backup.
    import base64 as _b64
    cv_files = {}
    for cand in candidates:
        cvp = cand.get('cv_path')
        if cvp:
            fpath = os.path.join(CV_DIR, cvp)
            if os.path.exists(fpath):
                try:
                    with open(fpath, 'rb') as _cf:
                        cv_files[cvp] = _b64.b64encode(_cf.read()).decode('ascii')
                except Exception:
                    pass
    data['cv_files'] = cv_files
    fname = 'hirelab_' + str(datetime.date.today()) + '.json'
    return Response(json.dumps(data, indent=2, ensure_ascii=False), mimetype='application/json',
                    headers={'Content-Disposition': 'attachment; filename=' + fname})

@app.route('/api/import', methods=['POST'])
@login_required
def import_data():
    import time
    # Ensure DB is initialized before import
    try:
        init_db()
    except Exception:
        pass

    for _attempt in range(5):
        try:
            data = request.json or {}
            if not data.get('mandates') and not data.get('candidates'):
                return jsonify({'error': 'Invalid backup file. Must be a HireLab JSON export.'}), 400
            conn = get_db(); c = conn.cursor()
            n = ts(); mid_map = {}; cid_map = {}; m_done = cand_done = hist_done = 0
            for k, v in (data.get('settings') or {}).items():
                c.execute('INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)', (k, str(v)))
            for m in (data.get('mandates') or []):
                old_id = m.get('id')
                c.execute('INSERT INTO mandates (client,role,location,division,ctc_min,ctc_max,jd,sop_text,sop_version,sop_changelog,status,created_at,owner_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (m.get('client',''), m.get('role',''), m.get('location',''), m.get('division',''),
                           float(m.get('ctc_min') or 0), float(m.get('ctc_max') or 0), m.get('jd',''),
                           m.get('sop_text',''), m.get('sop_version', 1), m.get('sop_changelog', '[]'),
                           m.get('status', 'active'), m.get('created_at') or n, effective_user_id()))
                mid_map[old_id] = c.lastrowid; m_done += 1
            for cand in (data.get('candidates') or []):
                old_id = cand.get('id')
                new_mid = mid_map.get(cand.get('mandate_id'), cand.get('mandate_id'))
                c.execute(
                    'INSERT INTO candidates (mandate_id,name,company,designation,experience,ctc_current,'
                    'ctc_expected,notice_period,location,phone,email,qualification,key_skills,secondary_skills,'
                    'career_summary,industry_background,is_mnc,screening_decision,ai_score,ai_reasoning,'
                    'stage,recruiter_feedback,client_feedback,general_comments,cv_path,cv_original_name,'
                    'msg1_sent_at,fu1_sent_at,fu2_sent_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (new_mid, cand.get('name',''), cand.get('company',''), cand.get('designation',''),
                     float(cand.get('experience') or 0), float(cand.get('ctc_current') or 0),
                     float(cand.get('ctc_expected') or 0), int(cand.get('notice_period') or 0),
                     cand.get('location',''), cand.get('phone',''), cand.get('email',''), cand.get('qualification',''),
                     json.dumps(cand.get('key_skills') if isinstance(cand.get('key_skills'), list) else json.loads(cand.get('key_skills') or '[]')),
                     json.dumps(cand.get('secondary_skills') if isinstance(cand.get('secondary_skills'), list) else json.loads(cand.get('secondary_skills') or '[]')),
                     cand.get('career_summary',''),
                     cand.get('industry_background',''), cand.get('is_mnc', 0), cand.get('screening_decision',''),
                     float(cand.get('ai_score') or 0), cand.get('ai_reasoning',''), cand.get('stage','Screening'),
                     cand.get('recruiter_feedback',''), cand.get('client_feedback',''), cand.get('general_comments',''),
                     cand.get('cv_path',''), cand.get('cv_original_name',''),
                     cand.get('msg1_sent_at',''), cand.get('fu1_sent_at',''), cand.get('fu2_sent_at',''),
                     cand.get('created_at') or n, cand.get('updated_at') or n))
                cid_map[old_id] = c.lastrowid; cand_done += 1
            for h in (data.get('history') or []):
                new_cid = cid_map.get(h.get('candidate_id'))
                if new_cid:
                    c.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                              (new_cid, h.get('from_stage',''), h.get('to_stage',''), h.get('note',''), h.get('created_at') or n))
                    hist_done += 1
            conn.commit(); conn.close()
            # Restore CV files (PDF/Word) that were embedded in the backup as base64.
            cv_restored = 0
            cv_files = data.get('cv_files') or {}
            if cv_files:
                import base64 as _b64
                os.makedirs(CV_DIR, exist_ok=True)
                for _fname, _b64data in cv_files.items():
                    try:
                        _dest = os.path.join(CV_DIR, _fname)
                        if not os.path.exists(_dest):   # don't overwrite existing
                            with open(_dest, 'wb') as _wf:
                                _wf.write(_b64.b64decode(_b64data))
                        cv_restored += 1
                    except Exception:
                        pass
            return jsonify({'ok': True, 'mandates': m_done, 'candidates': cand_done,
                            'history': hist_done, 'cvs': cv_restored})
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() and _attempt < 4:
                time.sleep(2)
                continue
            return jsonify({'error': 'DB locked: ' + str(e)}), 503
        except Exception as e:
            import traceback
            return jsonify({'error': str(e), 'detail': traceback.format_exc()[-500:]}), 500
    return jsonify({'error': 'Import failed after 5 retries'}), 500




# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CENTRAL DATABASE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# ── Startup: runs both with gunicorn AND python server.py ──────────────────────
# This ensures DB tables exist regardless of how the app is started
try:
    migrate_old()
    init_db()
    # ── Mount RecruitOS platform modules (CRM, etc.) as Flask blueprints ──
    try:
        import modules
        modules.register_all(app)
    except Exception as _mod_err:
        print(f'[modules] registration skipped: {_mod_err}')
    # Safety net order matters:
    # 1) check persistence (writes/reads a marker that proves the disk survives restarts)
    # 2) auto-restore if the live DB came up empty but a backup has users
    # 3) take a fresh backup of the (now-healthy) DB
    _PERSISTENCE = check_storage_persistence()
    auto_restore_if_empty()
    daily_backup()
    # Start the reminder push-notification scheduler (background thread)
    try:
        _start_reminder_scheduler()
    except Exception as _sched_err:
        print(f'[reminder-scheduler] failed to start: {_sched_err}')
    # Start the async embedding queue worker (background thread)
    try:
        _start_embedding_worker()
    except Exception as _emb_err:
        print(f'[embed-worker] failed to start: {_emb_err}')
    _ucount = _db_user_count(DB_PATH)
    print('\n' + '=' * 56)
    print('  HireLab Screener — startup')
    print('  DATA_DIR : ' + DATA_DIR)
    print('  DB_PATH  : ' + DB_PATH)
    print('  Users in DB: ' + str(_ucount))
    if _PERSISTENCE.get('persistent') is True:
        print(f'  Storage  : PERSISTENT ✓ (survived {_PERSISTENCE.get("boots_seen")} restarts)')
    else:
        print('  Storage  : NOT YET CONFIRMED persistent (first boot, or marker was wiped)')
        if DATA_DIR.rstrip('/').endswith('HireLab') or 'expanduser' in DATA_DIR:
            print('  *** WARNING: DATA_DIR is NOT the mounted disk. On Render you must set')
            print('  *** DATA_DIR=/data AND attach a persistent disk mounted at /data,')
            print('  *** otherwise ALL data is lost on every restart/spin-down. ***')
    print('=' * 56 + '\n')
except Exception as _startup_err:
    print(f'Startup init warning: {_startup_err}')

# ── SAFE DATA RESET (controlled by env var) ────────────────────────────────────
# Set RESET_DATA=yes in the host environment (e.g. Render) to wipe all mandates,
# candidates, reminders, work history and stage history on the next start.
# IMPORTANT: remove the variable again right after, so it does not wipe on every
# restart. User accounts are preserved unless RESET_DATA=all is used.
try:
    _reset = (os.environ.get('RESET_DATA') or '').strip().lower()
    if _reset in ('yes', 'all', '1', 'true'):
        # SAFETY: only run a given reset value ONCE, ever. We record which reset
        # token was last executed in a marker file on disk. If the env var still
        # holds the same value on the next restart, we SKIP it — so forgetting to
        # remove the variable can never wipe data again.
        _marker = os.path.join(DATA_DIR, '.last_reset')
        _already = ''
        try:
            if os.path.exists(_marker):
                with open(_marker) as _f: _already = _f.read().strip()
        except Exception: pass
        # Build a unique token: value + a user-supplied tag so the same 'yes' won't
        # re-run unless the user changes RESET_TAG too.
        _tag = (os.environ.get('RESET_TAG') or '').strip()
        _token = _reset + '|' + _tag
        if _token == _already:
            print(f'*** RESET_DATA={_reset} SKIPPED — already executed (token unchanged). Safe. ***')
        else:
            _conn = get_db(); _c = _conn.cursor()
            for _tbl in ['candidates', 'mandates', 'reminders', 'work_history',
                         'stage_history', 'submissions', 'activity_log']:
                try: _c.execute(f'DELETE FROM {_tbl}')
                except Exception: pass
            if _reset == 'all':
                try: _c.execute('DELETE FROM users')
                except Exception: pass
            _conn.commit(); _conn.close()
            try:
                with open(_marker, 'w') as _f: _f.write(_token)
            except Exception: pass
            print(f'*** RESET_DATA={_reset} executed ONCE — data cleared. Will NOT repeat. ***')
except Exception as _reset_err:
    print(f'Reset warning: {_reset_err}')

if __name__ == '__main__':
    check_timers()
    print('Local server: http://localhost:' + str(os.environ.get('PORT', 5000)))
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)