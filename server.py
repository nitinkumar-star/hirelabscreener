from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
import sqlite3, json, os, datetime, requests, shutil, io, re
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

def log_activity(action, detail=''):
    try:
        u = current_user()
        conn = get_db()
        conn.execute('INSERT INTO activity_log (user_id,username,action,detail,created_at) VALUES (?,?,?,?,?)',
                     (u['id'] if u else 0, u['username'] if u else 'system', action, detail, ts()))
        conn.commit(); conn.close()
    except Exception:
        pass

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
    Helps debug 'data disappeared / logged out' issues."""
    info = {'data_dir': DATA_DIR, 'db_path': DB_PATH}
    try:
        info['db_exists'] = os.path.exists(DB_PATH)
        info['data_dir_writable'] = os.access(DATA_DIR, os.W_OK)
        info['secret_key_file_exists'] = os.path.exists(os.path.join(DATA_DIR, '.secret_key'))
        info['reset_marker'] = os.path.exists(os.path.join(DATA_DIR, '.last_reset'))
        info['reset_data_env'] = bool(os.environ.get('RESET_DATA'))
        info['secret_key_env'] = bool(os.environ.get('SECRET_KEY'))
        conn = get_db(); c = conn.cursor()
        for t in ['users', 'mandates', 'candidates']:
            try: info[t + '_count'] = c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
            except Exception as e: info[t + '_count'] = f'err: {e}'
        conn.close()
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
        'role': u['role'], 'company': own_company
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
    conn.execute("INSERT INTO companies (name,status,plan,created_at) VALUES (?,?,?,?)",
                 (display_company, 'active', 'owner', ts()))
    company_id = conn.execute('SELECT id FROM companies ORDER BY id DESC LIMIT 1').fetchone()['id']
    conn.execute('INSERT INTO users (username,password_hash,display_name,role,created_at,status,company_name,company_id) VALUES (?,?,?,?,?,?,?,?)',
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
    conn.execute('''INSERT INTO users (username,password_hash,display_name,role,created_at,status,company_name,requested_at,company_id)
                     VALUES (?,?,?,?,?,?,?,?,?)''',
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
        comp = conn.execute('SELECT status FROM companies WHERE id=?', (u['company_id'],)).fetchone()
        if comp and comp['status'] == 'suspended':
            conn.close()
            return jsonify({'error': 'Your agency account is currently suspended. Please contact support.'}), 403
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


# ── Super-Admin: per-tenant (company) summary + activity ──────────────────
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

CLAUDE_URL   = 'https://api.anthropic.com/v1/messages'
CLAUDE_MODEL = 'claude-sonnet-4-20250514'

for d in [DATA_DIR, CV_DIR, BAK_DIR]:
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

def ts():
    return datetime.datetime.now().isoformat(timespec='seconds')

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
            notes TEXT DEFAULT ''
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
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
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
        ('template_msg1', 'Hi {Name}, this is {RecruiterName} from HireLab. I wanted to speak about a {Position} opportunity at {Location}.\n\nIf you are interested, please suggest the best time to connect.'),
        ('template_fu1', 'Hi {Name}, I had messaged you earlier about a {Position} role at {Location}.\n\nJust following up — would love to connect for a quick 10-minute call.\n\nLooking forward to hearing from you!'),
        ('template_fu2', 'Hi {Name}, this is my last follow up regarding the {Position} opportunity at {Location}.\n\nIf the timing is not right, no worries. But do let me know if you would like to explore this.\n\nHave a great day!'),
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
    ]:
        try:
            c.execute(f'ALTER TABLE candidates ADD COLUMN {col} {defn}')
        except Exception:
            pass

    # Migrate: add signup-approval + company columns to existing 'users' table
    for col, defn in [
        ('status', "TEXT DEFAULT 'approved'"),
        ('company_name', "TEXT DEFAULT ''"),
        ('requested_at', 'TEXT'),
        ('company_id', 'INTEGER DEFAULT 0'),
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

def daily_backup():
    if not os.path.exists(DB_PATH):
        return
    bak = os.path.join(BAK_DIR, f'hirelab_{datetime.date.today()}.db')
    if not os.path.exists(bak):
        shutil.copy2(DB_PATH, bak)
        print(f'[BACKUP] {bak}')
    for old in sorted(Path(BAK_DIR).glob('hirelab_*.db'))[:-7]:
        old.unlink()

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

def call_claude(api_key, system_msg, messages, max_tokens=8000):
    return requests.post(CLAUDE_URL,
        headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
        json={'model': CLAUDE_MODEL, 'max_tokens': max_tokens, 'system': system_msg, 'messages': messages},
        timeout=120)

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
        resp = requests.post('https://api.deepseek.com/chat/completions',
            headers={'Authorization': 'Bearer ' + ds_key, 'Content-Type': 'application/json'},
            json={'model': 'deepseek-chat', 'temperature': 0.3, 'max_tokens': 900,
                  'messages': [{'role': 'system', 'content': system_msg},
                                {'role': 'user', 'content': text[:12000]}]},
            timeout=60)
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
        'SELECT * FROM reminders WHERE done=0 AND owner_id=? ORDER BY due_at ASC',
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
def mark_reminder_done(rid):
    conn = get_db()
    conn.execute('UPDATE reminders SET done=1 WHERE id=?', (rid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/reminders/<int:rid>', methods=['DELETE'])
def delete_reminder(rid):
    conn = get_db()
    conn.execute('DELETE FROM reminders WHERE id=?', (rid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/dashboard-tasks')
@login_required
def dashboard_tasks():
    """Aggregate all tasks for the dashboard:
    1. Manual reminders (due today or overdue)
    2. WA messages sent but no response logged (any stage)
    3. New submissions from public form (today)
    """
    conn = get_db()
    now  = datetime.datetime.now()
    today_str = now.strftime('%Y-%m-%d')

    # ── 1. Manual reminders (pending) — exclude hold/closed mandates ────────
    rem_rows = conn.execute(
        "SELECT r.* FROM reminders r "
        "LEFT JOIN mandates m ON m.id = r.mandate_id "
        "WHERE r.done=0 AND r.owner_id=? "
        "  AND (m.id IS NULL OR m.status NOT IN ('hold','closed')) "
        "ORDER BY r.due_at ASC",
        (effective_user_id(),)
    ).fetchall()
    reminders = []
    for r in rem_rows:
        d = dict(r)
        try:
            due = datetime.datetime.fromisoformat(d['due_at'])
            d['overdue'] = due < now
            d['due_today'] = due.date() == now.date()
        except Exception:
            d['overdue'] = False; d['due_today'] = False
        reminders.append(d)

    # ── 2. WA sent but no response (any mandate, any stage) ─────────────────
    wa_pending = []
    cands = conn.execute(
        "SELECT c.*, m.role, m.client FROM candidates c "
        "LEFT JOIN mandates m ON m.id=c.mandate_id "
        "WHERE (c.msg1_sent_at!='' OR c.fu1_sent_at!='' OR c.fu2_sent_at!='') "
        "  AND (c.wa_response IS NULL OR c.wa_response='') "
        "  AND c.owner_id=? "
        "  AND (m.id IS NULL OR m.status NOT IN ('hold','closed')) "
        "ORDER BY c.updated_at DESC",
        (effective_user_id(),)
    ).fetchall()
    for c in cands:
        d = dict(c)
        # Calculate days since last WA message
        last_sent = d.get('fu2_sent_at') or d.get('fu1_sent_at') or d.get('msg1_sent_at') or ''
        days_ago = None
        if last_sent:
            try:
                sent_dt = datetime.datetime.fromisoformat(last_sent)
                days_ago = (now - sent_dt).days
            except Exception:
                pass
        d['days_since_wa'] = days_ago
        d['mandate_label'] = (d.get('role','') + ' — ' + d.get('client','')) if d.get('role') else ''
        wa_pending.append(d)

    # ── 3. New submissions today ─────────────────────────────────────────────
    sub_rows = conn.execute(
        "SELECT * FROM submissions WHERE status='new' AND created_at LIKE ? ORDER BY created_at DESC",
        (today_str + '%',)
    ).fetchall()
    new_submissions = [dict(r) for r in sub_rows]

    conn.close()
    return jsonify({
        'ok': True,
        'reminders': reminders,
        'wa_pending': wa_pending,
        'new_submissions': new_submissions,
        'counts': {
            'reminders': len(reminders),
            'wa_pending': len(wa_pending),
            'new_submissions': len(new_submissions),
        }
    })

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

    # Verify the mandate belongs to the current (effective) user
    _conn = get_db()
    _own = _conn.execute('SELECT owner_id FROM mandates WHERE id=?', (mid,)).fetchone()
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
    c.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
              (cid, '', 'Screening', 'Pushed from Naukri extension', ts()))
    _save_wh_for(conn, cid, d.get('work_history'))
    conn.commit(); conn.close()
    embed_candidate_async(cid)  # auto-index for semantic search
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

def get_setting(key, default=''):
    # Env var takes priority for sensitive keys (see _ENV_KEY_MAP)
    env_name = _ENV_KEY_MAP.get(key)
    if env_name:
        env_val = os.environ.get(env_name, '').strip()
        if env_val:
            return env_val
    conn = get_db()
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    conn.close()
    return (row['value'] if row else '') or default

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

def candidate_embed_text(c):
    """Build the text blob we embed for a candidate. Mandate-agnostic on
    purpose so a candidate can surface for ANY role they fit."""
    try:
        skills = json.loads(c['key_skills'] or '[]')
    except Exception:
        skills = []
    if isinstance(skills, list):
        skills = ', '.join(str(s) for s in skills)
    parts = [
        c['name'] or '',
        (c['designation'] or '') + (' at ' + c['company'] if c['company'] else ''),
        'Experience: ' + str(c['experience'] or 0) + ' years',
        'Location: ' + (c['location'] or ''),
        'Skills: ' + str(skills),
        c['career_summary'] or '',
    ]
    # Include product/function tags if present
    for col in ('product_handles', 'function_tags'):
        try:
            v = c[col]
            if v:
                tags = json.loads(v) if v.strip().startswith('[') else v
                if isinstance(tags, list): tags = ', '.join(tags)
                if tags: parts.append(str(tags))
        except Exception:
            pass
    return '\n'.join(p for p in parts if p and p.strip())

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
        rr = requests.post('https://api.deepseek.com/chat/completions',
            headers={'Authorization': 'Bearer ' + ds_key, 'Content-Type': 'application/json'},
            json={'model': 'deepseek-chat', 'temperature': 0.2, 'max_tokens': 400,
                  'messages': [{'role': 'user', 'content': prompt}]},
            timeout=60)
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

def embed_candidate_async(cid):
    """Best-effort: embed a single candidate right after creation. Failures are
    silent so they never block the main add flow; reindex can catch them later."""
    try:
        api_key = get_setting('gemini_api_key')
        if not api_key:
            return
        conn = get_db()
        c = conn.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
        if not c:
            conn.close(); return
        txt = candidate_embed_text(c)
        if not txt.strip():
            conn.close(); return
        vec = gemini_embed(txt, api_key)
        if isinstance(vec, list) and vec:
            conn.execute('UPDATE candidates SET embedding=?, embedding_text=?, embedded_at=? WHERE id=?',
                         (json.dumps(vec), txt, ts(), cid))
            conn.commit()
        conn.close()
    except Exception:
        pass


@app.route('/api/ai/index-status', methods=['GET'])
@login_required
def ai_index_status():
    conn = get_db()
    total = conn.execute('SELECT COUNT(*) n FROM candidates').fetchone()['n']
    done = conn.execute("SELECT COUNT(*) n FROM candidates WHERE embedding!='' AND embedding IS NOT NULL").fetchone()['n']
    conn.close()
    has_key = bool(get_setting('gemini_api_key'))
    return jsonify({'ok': True, 'total': total, 'indexed': done,
                    'pending': total - done, 'has_gemini_key': has_key})


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
            conn.execute("UPDATE candidates SET embedding=''")
            conn.commit()
            rows = conn.execute('SELECT * FROM candidates ORDER BY id LIMIT ?', (batch,)).fetchall()
    rows = conn.execute("SELECT * FROM candidates WHERE embedding='' OR embedding IS NULL ORDER BY id LIMIT ?", (batch,)).fetchall()

    done, failed, skipped = 0, 0, 0
    first_error = ''
    for c in rows:
        txt = candidate_embed_text(c)
        if not txt.strip():
            # mark as embedded-empty so we don't loop forever on blanks
            conn.execute("UPDATE candidates SET embedding='[]', embedded_at=? WHERE id=?", (ts(), c['id']))
            conn.commit()
            skipped += 1
            continue
        vec = gemini_embed(txt, api_key)
        if isinstance(vec, dict) and vec.get('error'):
            failed += 1
            first_error = vec['error']
            if 'API key' in vec['error'] or 'PERMISSION' in vec['error'].upper() or 'API_KEY' in vec['error'].upper():
                conn.close()
                return jsonify({'error': 'Gemini error: ' + vec['error'],
                                'indexed': done, 'failed': failed}), 400
            continue
        if not vec:
            failed += 1
            continue
        conn.execute('UPDATE candidates SET embedding=?, embedding_text=?, embedded_at=? WHERE id=?',
                     (json.dumps(vec), txt, ts(), c['id']))
        conn.commit()
        done += 1

    # remaining count
    pending = conn.execute("SELECT COUNT(*) n FROM candidates WHERE embedding='' OR embedding IS NULL").fetchone()['n']
    conn.close()
    return jsonify({'ok': True, 'indexed': done, 'failed': failed, 'skipped': skipped,
                    'pending': pending, 'first_error': first_error})


@app.route('/api/ai/search', methods=['POST'])
@login_required
def ai_search():
    """Semantic talent search across ALL candidates (ignores mandate
    boundaries) so someone saved under one role surfaces for another."""
    d = request.json or {}
    query = (d.get('query') or '').strip()
    top_k = int(d.get('top_k') or 10)
    if not query:
        return jsonify({'error': 'Empty query'}), 400

    api_key = get_setting('gemini_api_key')
    if not api_key:
        return jsonify({'error': 'Gemini API key not set. Add it in Settings.'}), 400

    qvec = gemini_embed(query, api_key)
    if isinstance(qvec, dict) and qvec.get('error'):
        return jsonify({'error': 'Gemini error: ' + qvec['error']}), 400
    if not qvec:
        return jsonify({'error': 'Could not embed query'}), 400

    conn = get_db()
    rows = conn.execute(
        "SELECT c.*, m.role AS mandate_role, m.client AS mandate_client, m.status AS mandate_status "
        "FROM candidates c LEFT JOIN mandates m ON m.id=c.mandate_id "
        "WHERE c.embedding!='' AND c.embedding IS NOT NULL"
    ).fetchall()

    scored = []
    for c in rows:
        try:
            vec = json.loads(c['embedding'])
        except Exception:
            continue
        sim = cosine(qvec, vec)
        scored.append((sim, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for sim, c in scored[:top_k]:
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
            'score': round(sim * 100, 1),
        })

    # Optional AI reasoning over the top results (why they fit)
    explain = bool(d.get('explain'))
    reasoning = ''
    if explain and results:
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
                rr = requests.post('https://api.deepseek.com/chat/completions',
                    headers={'Authorization': 'Bearer ' + ds_key, 'Content-Type': 'application/json'},
                    json={'model': 'deepseek-chat', 'temperature': 0.3, 'max_tokens': 400,
                          'messages': [{'role': 'user', 'content': prompt}]},
                    timeout=60)
                if rr.status_code == 200:
                    reasoning = rr.json()['choices'][0]['message']['content'].strip()
            except Exception:
                pass

    conn.close()
    return jsonify({'ok': True, 'results': results, 'reasoning': reasoning,
                    'searched': len(scored)})


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
        rr = requests.post('https://api.deepseek.com/chat/completions',
            headers={'Authorization': 'Bearer ' + ds_key, 'Content-Type': 'application/json'},
            json={'model': 'deepseek-chat', 'temperature': 0.2, 'max_tokens': 800,
                  'messages': [{'role': 'user', 'content': prompt}]},
            timeout=90)
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
    col_map = {'product': 'product_handles', 'function': 'function_tags'}
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
    col_map = {'product': 'product_handles', 'function': 'function_tags'}
    col = col_map.get(tag_type)
    if not col:
        return jsonify({'ok': False, 'error': 'Invalid tag type'}), 400
    if not isinstance(tags, list):
        return jsonify({'ok': False, 'error': 'tags must be a list'}), 400
    tags = [str(t).strip() for t in tags if str(t).strip()]
    conn = get_db()
    conn.execute(f'UPDATE candidates SET {col}=?, updated_at=? WHERE id=?', (json.dumps(tags), ts(), cid))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'tags': tags})

@app.route('/api/health')
def health():
    return jsonify({'ok': True, 'data_dir': DATA_DIR, 'db': DB_PATH})

# Settings
@app.route('/api/settings', methods=['GET'])
def get_settings():
    conn = get_db()
    rows = conn.execute('SELECT key,value FROM settings').fetchall()
    conn.close()
    return jsonify({r['key']: r['value'] for r in rows})

@app.route('/api/settings', methods=['POST'])
def save_settings():
    conn = get_db()
    for k, v in (request.json or {}).items():
        conn.execute('INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)', (k, str(v)))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# Mandates
@app.route('/api/mandates', methods=['GET'])
@login_required
def list_mandates():
    conn = get_db()
    rows = conn.execute('SELECT * FROM mandates WHERE owner_id=? ORDER BY created_at DESC',
                        (effective_user_id(),)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/mandates', methods=['POST'])
@login_required
def create_mandate():
    d = request.json or {}
    if not d.get('client') or not d.get('role'):
        return jsonify({'error': 'Client and Role required'}), 400
    conn = get_db(); c = conn.cursor()
    c.execute('INSERT INTO mandates (client,role,location,division,ctc_min,ctc_max,jd,status,created_at,owner_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
              (d['client'], d['role'], d.get('location',''), d.get('division',''),
               float(d.get('ctc_min', 0)), float(d.get('ctc_max', 0)), d.get('jd',''), 'active', ts(), effective_user_id()))
    mid = c.lastrowid; conn.commit(); conn.close()
    log_activity('create_mandate', d['role'] + ' @ ' + d['client'])
    return jsonify({'ok': True, 'id': mid})

@app.route('/api/mandates/<int:mid>', methods=['GET'])
@login_required
def get_mandate(mid):
    conn = get_db()
    r = conn.execute('SELECT * FROM mandates WHERE id=? AND owner_id=?', (mid, effective_user_id())).fetchone()
    conn.close()
    return jsonify(dict(r)) if r else (jsonify({'error': 'Not found'}), 404)

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
        d = dict(r)
        try: d['key_skills'] = json.loads(d['key_skills'] or '[]')
        except: d['key_skills'] = []
        try: d['secondary_skills'] = json.loads(d['secondary_skills'] or '[]')
        except: d['secondary_skills'] = []
        out.append(d)
    return jsonify(out)

@app.route('/api/candidates/<int:cid>')
def get_candidate(cid):
    conn = get_db()
    r = conn.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
    if not r: conn.close(); return jsonify({'error': 'Not found'}), 404
    d = dict(r)
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

@app.route('/api/candidates/<int:cid>', methods=['PUT'])
def update_candidate(cid):
    d = request.json or {}
    conn = get_db()
    c = conn.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
    if not c: conn.close(); return jsonify({'error': 'Not found'}), 404

    fields = ['name','company','designation','experience','ctc_current','ctc_expected',
              'notice_period','location','phone','email','qualification','career_summary',
              'key_skills','secondary_skills','recruiter_feedback','client_feedback','general_comments']
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

        # Log feedback changes in history
        notes = []
        if 'recruiter_feedback' in d and d['recruiter_feedback'] and d['recruiter_feedback'] != (c['recruiter_feedback'] or ''):
            notes.append('Recruiter feedback updated: ' + str(d['recruiter_feedback'])[:120])
        if 'client_feedback' in d and d['client_feedback'] and d['client_feedback'] != (c['client_feedback'] or ''):
            notes.append('Client feedback: ' + str(d['client_feedback'])[:120])
        if 'general_comments' in d and d['general_comments'] and d['general_comments'] != (c['general_comments'] or ''):
            notes.append('Comment: ' + str(d['general_comments'])[:120])
        if 'key_skills' in d:
            new_skills = d['key_skills'] if isinstance(d['key_skills'], list) else json.loads(d['key_skills'] or '[]')
            old_skills = json.loads(c['key_skills'] or '[]')
            if set(new_skills) != set(old_skills):
                notes.append('Skills updated: ' + ', '.join(new_skills[:6]))
        if 'phone' in d and d['phone'] and d['phone'] != (c['phone'] or ''):
            notes.append('Phone added: ' + str(d['phone']))

        for note in notes:
            conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                         (cid, c['stage'], c['stage'], note, ts()))

        conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/candidates/<int:cid>/stage', methods=['POST'])
def move_stage(cid):
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
    return jsonify({'ok': True, 'count': len(items)})


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
                  'location, qualification, key_skills (array max 6), secondary_skills (array), '
                  'career_summary (2 sentences), is_mnc (bool). '
                  'Use null for missing strings, 0 for missing numbers.')
    try:
        resp = requests.post('https://api.deepseek.com/chat/completions',
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
            json={'model': 'deepseek-chat', 'temperature': 0, 'max_tokens': 800,
                  'messages': [{'role': 'system', 'content': system_msg}, {'role': 'user', 'content': raw}],
                  'response_format': {'type': 'json_object'}},
            timeout=30)
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
                  'location (current city), qualification (highest degree), '
                  'key_skills (array of top 8 technical/domain skills), '
                  'secondary_skills (array of other skills), '
                  'career_summary (2-3 sentences about background and strengths), '
                  'industry_background (e.g. FMCG, Manufacturing, IT), is_mnc (bool). '
                  'Use null for missing strings, 0 for missing numbers.')
    try:
        resp = requests.post('https://api.deepseek.com/chat/completions',
            headers={'Authorization': 'Bearer ' + ds_key, 'Content-Type': 'application/json'},
            json={'model': 'deepseek-chat', 'temperature': 0, 'max_tokens': 1000,
                  'messages': [{'role': 'system', 'content': system_msg},
                                {'role': 'user', 'content': 'Extract from this resume:\n\n' + text[:8000]}],
                  'response_format': {'type': 'json_object'}},
            timeout=45)
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
        resp = requests.post('https://api.deepseek.com/chat/completions',
            headers={'Authorization': 'Bearer ' + ds_key, 'Content-Type': 'application/json'},
            json={'model': 'deepseek-chat', 'temperature': 0, 'max_tokens': 3000,
                  'messages': [{'role': 'system', 'content': system_msg},
                                {'role': 'user', 'content': 'Extract all candidates from:\n\n' + raw}]},
            timeout=60)
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

    transcript = whisper_resp.json().get('text', '').strip()
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
        d = dict(r)
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
        resp = requests.post('https://api.deepseek.com/chat/completions',
            headers={'Authorization': 'Bearer ' + ds_key, 'Content-Type': 'application/json'},
            json={'model': 'deepseek-chat', 'temperature': 0, 'max_tokens': 800,
                  'messages': [{'role': 'system', 'content': sys_msg},
                                {'role': 'user', 'content': 'Extract:\n\n' + text[:8000]}],
                  'response_format': {'type': 'json_object'}},
            timeout=45)
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
    migrate_old()
    daily_backup()
    init_db()
    check_timers()
    print('\n' + '='*50)
    print('  HireLab Screener v2 - Ready')
    print('  Open: http://localhost:5000')
    print('  Data: ' + DATA_DIR)
    print('='*50 + '\n')
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
