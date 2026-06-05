from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
import sqlite3, json, os, datetime, requests, shutil, io
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

# ── Login Protection ──────────────────────────────────────────────────────────
from functools import wraps
from flask import session, redirect as flask_redirect

app.secret_key = os.environ.get('SECRET_KEY', 'hirelab-2024-secret')
APP_PASSWORD   = os.environ.get('APP_PASSWORD', '')

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if APP_PASSWORD and not session.get('logged_in'):
            return flask_redirect('/login')
        return f(*args, **kwargs)
    return decorated

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    err = ''
    if request.method == 'POST':
        if request.form.get('password') == APP_PASSWORD:
            session['logged_in'] = True
            return flask_redirect('/')
        err = 'Wrong password'
    return (
        '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>HireLab Login</title>'
        '<style>body{font-family:sans-serif;background:#0a2540;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}'
        '.b{background:#fff;padding:32px;border-radius:10px;width:300px;text-align:center}'
        'h2{color:#0a2540;margin-bottom:4px;font-size:20px}'
        'h2 span{color:#1D9E75}'
        'p{color:#888;font-size:12px;margin-bottom:20px}'
        'input{width:100%;padding:9px;border:1px solid #ddd;border-radius:5px;font-size:13px;margin-bottom:12px;box-sizing:border-box}'
        'button{width:100%;padding:10px;background:#1D9E75;color:#fff;border:none;border-radius:5px;font-size:14px;cursor:pointer}'
        '.e{color:red;font-size:12px;margin-bottom:8px}</style></head>'
        '<body><div class="b">'
        '<h2>Hire<span>Lab</span></h2><p>Internal Recruitment Tool</p>'
        + ('<div class="e">' + err + '</div>' if err else '') +
        '<form method="post"><input type="password" name="password" placeholder="Password" autofocus>'
        '<button>Login</button></form></div></body></html>'
    )

@app.route('/logout')
def logout():
    session.clear()
    return flask_redirect('/login')


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
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=60000')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn

def ts():
    return datetime.datetime.now().isoformat(timespec='seconds')

def init_db():
    conn = get_db(); c = conn.cursor()
    c.executescript("""
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
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT '',
            FOREIGN KEY (mandate_id) REFERENCES mandates(id)
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
        ('claude_api_key', ''),
        ('deepseek_api_key', ''),
        ('openai_api_key', ''),
        ('fu1_hours', '8'),
        ('fu2_hours', '24'),
        ('template_msg1', 'Hi {Name}, this is {RecruiterName} from HireLab. I wanted to speak about a {Position} opportunity at {Location}.\n\nIf you are interested, please suggest the best time to connect.'),
        ('template_fu1', 'Hi {Name}, I had messaged you earlier about a {Position} role at {Location}.\n\nJust following up — would love to connect for a quick 10-minute call.\n\nLooking forward to hearing from you!'),
        ('template_fu2', 'Hi {Name}, this is my last follow up regarding the {Position} opportunity at {Location}.\n\nIf the timing is not right, no worries. But do let me know if you would like to explore this.\n\nHave a great day!'),
    ]
    for k, v in defaults:
        c.execute('INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)', (k, v))
    conn.commit(); conn.close()

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

def get_setting(key, default=''):
    conn = get_db()
    r = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    conn.close()
    return r['value'] if r else default

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ROUTES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.route('/')
@login_required
def index():
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html'))




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
def list_mandates():
    conn = get_db()
    rows = conn.execute('SELECT * FROM mandates ORDER BY created_at DESC').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/mandates', methods=['POST'])
def create_mandate():
    d = request.json or {}
    if not d.get('client') or not d.get('role'):
        return jsonify({'error': 'Client and Role required'}), 400
    conn = get_db(); c = conn.cursor()
    c.execute('INSERT INTO mandates (client,role,location,division,ctc_min,ctc_max,jd,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
              (d['client'], d['role'], d.get('location',''), d.get('division',''),
               float(d.get('ctc_min', 0)), float(d.get('ctc_max', 0)), d.get('jd',''), 'active', ts()))
    mid = c.lastrowid; conn.commit(); conn.close()
    return jsonify({'ok': True, 'id': mid})

@app.route('/api/mandates/<int:mid>', methods=['GET'])
def get_mandate(mid):
    conn = get_db()
    r = conn.execute('SELECT * FROM mandates WHERE id=?', (mid,)).fetchone()
    conn.close()
    return jsonify(dict(r)) if r else (jsonify({'error': 'Not found'}), 404)

@app.route('/api/mandates/<int:mid>', methods=['PUT'])
def update_mandate(mid):
    d = request.json or {}
    conn = get_db()
    conn.execute('UPDATE mandates SET client=?,role=?,location=?,division=?,ctc_min=?,ctc_max=?,jd=?,status=? WHERE id=?',
                 (d.get('client',''), d.get('role',''), d.get('location',''), d.get('division',''),
                  float(d.get('ctc_min', 0)), float(d.get('ctc_max', 0)), d.get('jd',''), d.get('status','active'), mid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/mandates/<int:mid>/sop', methods=['PUT'])
def update_sop(mid):
    d = request.json or {}
    conn = get_db()
    m = conn.execute('SELECT * FROM mandates WHERE id=?', (mid,)).fetchone()
    if not m: conn.close(); return jsonify({'error': 'Not found'}), 404
    v = (m['sop_version'] or 1) + 1
    log = json.loads(m['sop_changelog'] or '[]')
    log.append({'version': v, 'date': datetime.datetime.now().strftime('%d %b %Y %H:%M'), 'change': d.get('changelog_entry', 'Updated')})
    conn.execute('UPDATE mandates SET sop_text=?,sop_version=?,sop_changelog=? WHERE id=?',
                 (d.get('sop_text', ''), v, json.dumps(log), mid))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'version': v})

# SOP Chat
@app.route('/api/mandates/<int:mid>/sop/chat', methods=['POST'])
def sop_chat(mid):
    d = request.json or {}
    api_key = d.get('api_key', '')
    if not api_key: return jsonify({'error': 'Claude API key not set. Add it in Settings.'}), 400
    conn = get_db()
    m = conn.execute('SELECT * FROM mandates WHERE id=?', (mid,)).fetchone()
    conn.close()
    if not m: return jsonify({'error': 'Mandate not found'}), 404

    system_msg = (
        "You are helping a recruiter create a Screening Operating Procedure (SOP).\n"
        "Mandate: Role={role}, Client={client}, Location={location}, CTC={ctc_min}-{ctc_max} LPA\n"
        "JD: {jd}\n\n"
        "Step 1: Ask focused questions ONE AT A TIME (must-haves, deal-breakers, preferred companies, notice period limit).\n"
        "Step 2: After getting answers, generate the SOP.\n\n"
        "IMPORTANT: When generating the final SOP, respond with ONLY this JSON and nothing else:\n"
        '{{"sop_ready":true,"sop_text":"write full SOP here","changelog_entry":"Initial SOP created"}}'
    ).format(
        role=m['role'], client=m['client'], location=m['location'],
        ctc_min=m['ctc_min'], ctc_max=m['ctc_max'], jd=(m['jd'] or 'Not provided')
    )

    resp = call_claude(api_key, system_msg, d.get('messages', []), max_tokens=1500)
    if resp.status_code != 200:
        try: err = resp.json().get('error', {}).get('message', 'Claude API error')
        except: err = resp.text[:200]
        return jsonify({'error': err}), 500

    text = resp.json()['content'][0]['text']
    parsed = parse_json(text)

    if parsed and parsed.get('sop_ready'):
        return jsonify({'type': 'sop_ready', 'sop_data': parsed})

    # Fallback: detect SOP in plain text
    kws = ['must-have', 'must have', 'deal-breaker', 'notice period', 'experience', 'preferred']
    if len(text) > 200 and sum(1 for k in kws if k.lower() in text.lower()) >= 2:
        return jsonify({'type': 'sop_ready', 'sop_data': {'sop_ready': True, 'sop_text': text, 'changelog_entry': 'Initial SOP created via Q&A'}})

    return jsonify({'type': 'question', 'text': text})

# SOP Refine
@app.route('/api/mandates/<int:mid>/sop/refine', methods=['POST'])
def refine_sop(mid):
    d = request.json or {}
    api_key = d.get('api_key', '')
    if not api_key: return jsonify({'error': 'No API key'}), 400
    conn = get_db()
    m = conn.execute('SELECT * FROM mandates WHERE id=?', (mid,)).fetchone()
    conn.close()
    if not m: return jsonify({'error': 'Not found'}), 404
    system_msg = 'Refine a recruitment SOP based on rejection patterns. Respond ONLY with JSON: {"changelog_entry":"...","updated_sop":"...","pattern_found":"..."}'
    user_msg = 'Current SOP:\n' + (m['sop_text'] or '') + '\n\nRejections:\n' + json.dumps(d.get('rejections', []))
    resp = call_claude(api_key, system_msg, [{'role': 'user', 'content': user_msg}], max_tokens=2000)
    if resp.status_code != 200: return jsonify({'error': 'Claude error'}), 500
    text = resp.json()['content'][0]['text']
    parsed = parse_json(text)
    return jsonify({'ok': True, **(parsed or {}), 'raw': text})

# Batch Screen
@app.route('/api/mandates/<int:mid>/screen', methods=['POST'])
def screen_batch(mid):
    d = request.json or {}
    paste = d.get('paste', '').strip()
    api_key = d.get('api_key', '')
    if not paste: return jsonify({'error': 'Nothing pasted'}), 400
    if not api_key: return jsonify({'error': 'Claude API key missing. Add in Settings.'}), 400

    conn = get_db(); c = conn.cursor()
    m = conn.execute('SELECT * FROM mandates WHERE id=?', (mid,)).fetchone()
    if not m: conn.close(); return jsonify({'error': 'Mandate not found'}), 404

    existing = set()
    for r in conn.execute('SELECT name,company FROM candidates WHERE mandate_id=?', (mid,)).fetchall():
        existing.add((r['name'].lower().strip(), r['company'].lower().strip()))

    # Use SOP → JD → mandate info as screening criteria (in priority order)
    if m['sop_text'] and m['sop_text'].strip():
        criteria = m['sop_text'].strip()
        criteria_type = 'SOP'
    elif m['jd'] and m['jd'].strip():
        criteria = m['jd'].strip()
        criteria_type = 'JD'
    else:
        criteria = (
            'Role: ' + m['role'] + '\n'
            'Client: ' + m['client'] + '\n'
            'Location: ' + (m['location'] or 'Not specified') + '\n'
            'CTC Budget: Rs ' + str(int(m['ctc_min'])) + '-' + str(int(m['ctc_max'])) + ' LPA\n'
            'Screen for relevant experience, skills, and CTC fit.'
        )
        criteria_type = 'Role Requirements'

    system_msg = (
        'You are an expert recruiter. Screen each candidate against this ' + criteria_type + ':\n\n'
        + criteria + '\n\n'
        'Mandate: ' + m['role'] + ' at ' + m['client'] + ', ' + (m['location'] or '') +
        ', CTC ' + str(m['ctc_min']) + '-' + str(m['ctc_max']) + ' LPA\n\n'
        'Give ai_score 0-100 based on fit. 100=perfect match, 0=unsuitable.\n\n'
        'Return ONLY a valid JSON array. No markdown, no explanation. Each object:\n'
        'name, decision (worth_opening OR skip), ai_score (int 0-100), reasoning (max 2 sentences),\n'
        'company, designation, experience (float), ctc_current (float LPA), ctc_expected (float LPA),\n'
        'notice_period (int days), location, qualification, key_skills (array max 5),\n'
        'secondary_skills (array), career_summary (string), is_mnc (bool), industry_background (string)'
    )
    resp = call_claude(api_key, system_msg, [{'role': 'user', 'content': 'Screen these candidates:\n\n' + paste}])
    if resp.status_code != 200:
        conn.close()
        try: err = resp.json().get('error', {}).get('message', 'Claude API error')
        except: err = resp.text[:300]
        return jsonify({'error': err}), 500

    text = resp.json()['content'][0]['text']
    parsed = parse_json(text)
    if not isinstance(parsed, list):
        conn.close()
        return jsonify({'error': 'Could not parse Claude response. Try again.', 'raw': text[:300]}), 500

    saved = []; dups = []; worth = skip = dup_count = 0
    for cd in parsed:
        name = str(cd.get('name') or '').strip()
        company = str(cd.get('company') or '').strip()
        key = (name.lower(), company.lower())
        if key in existing:
            dup_count += 1; dups.append({**cd, 'is_duplicate': True}); continue
        existing.add(key)
        c.execute(
            'INSERT INTO candidates (mandate_id,name,company,designation,experience,ctc_current,'
            'ctc_expected,notice_period,location,qualification,key_skills,secondary_skills,'
            'career_summary,industry_background,is_mnc,screening_decision,ai_score,ai_reasoning,'
            'stage,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (mid, name, company, cd.get('designation',''), float(cd.get('experience') or 0),
             float(cd.get('ctc_current') or 0), float(cd.get('ctc_expected') or 0),
             int(cd.get('notice_period') or 0), cd.get('location',''), cd.get('qualification',''),
             json.dumps(cd.get('key_skills') or []), json.dumps(cd.get('secondary_skills') or []),
             cd.get('career_summary',''), cd.get('industry_background',''), 1 if cd.get('is_mnc') else 0,
             cd.get('decision','skip'), float(cd.get('ai_score') or 0), '[Rated vs '+criteria_type+'] '+(cd.get('reasoning') or ''),
             'Screening' if cd.get('decision') == 'worth_opening' else 'Screened-Out', ts(), ts()))
        cid = c.lastrowid
        if cd.get('decision') == 'worth_opening':
            worth += 1
            c.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                      (cid, '', 'Screening', 'AI ' + str(cd.get('ai_score',0)) + '% — Worth Opening', ts()))
        else: skip += 1
        saved.append({**cd, 'id': cid, 'is_duplicate': False})
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'total': len(parsed), 'worth': worth, 'skip': skip,
                    'duplicates': dup_count, 'candidates': saved, 'duplicate_list': dups})

# Candidates
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

@app.route('/api/candidates/<int:cid>/mark-sent', methods=['POST'])
def mark_sent(cid):
    d = request.json or {}
    t = d.get('type', 'msg1')
    field = {'msg1': 'msg1_sent_at', 'fu1': 'fu1_sent_at', 'fu2': 'fu2_sent_at'}.get(t, 'msg1_sent_at')
    label = {'msg1': 'Message 1 sent via WhatsApp', 'fu1': 'Follow Up 1 sent', 'fu2': 'Follow Up 2 sent'}.get(t, 'Message sent')
    n = ts()
    conn = get_db()
    r = conn.execute('SELECT stage FROM candidates WHERE id=?', (cid,)).fetchone()
    if not r: conn.close(); return jsonify({'error': 'Not found'}), 404
    conn.execute('UPDATE candidates SET ' + field + '=?,updated_at=? WHERE id=?', (n, n, cid))
    conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                 (cid, r['stage'], r['stage'], label, n))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'sent_at': n})

@app.route('/api/candidates/<int:cid>/not-contacted', methods=['POST'])
def not_contacted(cid):
    d = request.json or {}
    conn = get_db()
    r = conn.execute('SELECT stage FROM candidates WHERE id=?', (cid,)).fetchone()
    if not r: conn.close(); return jsonify({'error': 'Not found'}), 404
    conn.execute("UPDATE candidates SET stage='Not Contacted',updated_at=? WHERE id=?", (ts(), cid))
    conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                 (cid, r['stage'], 'Not Contacted', 'Not reachable: ' + d.get('reason',''), ts()))
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
@app.route('/api/candidates/<int:cid>/cv', methods=['POST'])
def upload_cv(cid):
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
    key = d.get('deepseek_api_key', '')
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
    ds_key = request.form.get('deepseek_api_key', '')
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
    ds_key = d.get('deepseek_api_key', '')
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
def analyse_call(cid):
    openai_key  = request.form.get('openai_api_key', '').strip()
    claude_key  = request.form.get('claude_api_key', '').strip()
    language    = request.form.get('language', 'hi')   # hi = Hindi, en = English

    if not openai_key: return jsonify({'error': 'OpenAI API key required (for transcription). Add in Settings.'}), 400
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
            'https://api.openai.com/v1/audio/transcriptions',
            headers={'Authorization': 'Bearer ' + openai_key},
            files={'file': (f.filename, file_bytes, mime)},
            data={'model': 'whisper-1', 'language': language,
                  'prompt': 'This is a recruiter call with a candidate discussing a job opportunity. '
                            'The conversation may be in Hindi, English, or Hinglish.'},
            timeout=120
        )
    except requests.Timeout:
        return jsonify({'error': 'Whisper transcription timed out. Try a shorter recording.'}), 504
    except Exception as e:
        return jsonify({'error': 'Transcription error: ' + str(e)}), 500

    if whisper_resp.status_code == 401:
        return jsonify({'error': 'Invalid OpenAI API key'}), 401
    if whisper_resp.status_code != 200:
        try:
            err = whisper_resp.json().get('error', {}).get('message', whisper_resp.text[:200])
        except Exception:
            err = whisper_resp.text[:200]
        return jsonify({'error': 'Whisper error: ' + err}), 500

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

    jd_or_sop = (mandate['sop_text'] or mandate['jd'] or '') if mandate else ''
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
        '  "ctc_discussed": null or float (LPA mentioned by candidate),\n'
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
    # Store in general_comments if empty, else append
    existing = conn.execute('SELECT general_comments FROM candidates WHERE id=?', (cid,)).fetchone()
    note = '[CALL ANALYSIS ' + datetime.datetime.now().strftime('%d %b %Y %H:%M') + '] Recorded. Interest: ' + analysis.get('interest_level', '') + '. ' + analysis.get('call_summary', '')[:200]
    conn.execute('UPDATE candidates SET general_comments=?,updated_at=? WHERE id=?',
                 (note, ts(), cid))
    conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                 (cid, cand['stage'], cand['stage'],
                  'Call analysed. Interest: ' + analysis.get('interest_level','') + '. Rec: ' + analysis.get('recommendation','') + '. ' + analysis.get('next_step',''),
                  ts()))
    conn.commit(); conn.close()

    return jsonify({
        'ok': True,
        'transcript': transcript,
        'analysis': analysis,
        'recording_file': fname,
        'cv_used': bool(cv_text),
        'jd_used': bool(jd_or_sop)
    })

@app.route('/api/calls/<path:filename>')
def serve_call(filename):
    fp = os.path.join(CALL_DIR, filename)
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
    rows = conn.execute(
        'SELECT c.*, m.role as mandate_role, m.client as mandate_client '
        'FROM candidates c LEFT JOIN mandates m ON c.mandate_id = m.id '
        'ORDER BY c.created_at DESC'
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
def central_db_add():
    d   = request.json or {}
    mid = get_or_create_central_mandate()
    if not d.get('name') or not d.get('company'):
        return jsonify({'error': 'Name and Company required'}), 400
    conn = get_db(); c = conn.cursor()
    c.execute(
        'INSERT INTO candidates (mandate_id,name,company,designation,experience,ctc_current,'
        'ctc_expected,notice_period,location,phone,email,career_summary,key_skills,'
        'screening_decision,ai_reasoning,stage,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (mid, d['name'], d['company'], d.get('designation',''), float(d.get('experience') or 0),
         float(d.get('ctc_current') or 0), float(d.get('ctc_expected') or 0),
         int(d.get('notice_period') or 0), d.get('location',''), d.get('phone',''), d.get('email',''),
         d.get('career_summary',''), json.dumps(d.get('key_skills') or []),
         'worth_opening', 'Added to Central Database', 'Central DB', ts(), ts()))
    cid = c.lastrowid
    c.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
              (cid, '', 'Central DB', 'Added to Central Database', ts()))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'id': cid})

@app.route('/api/central-db/bulk', methods=['POST'])
def central_db_bulk():
    d   = request.json or {}
    mid = get_or_create_central_mandate()
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
            'screening_decision,ai_reasoning,stage,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (mid, name, company, cand.get('designation',''), float(cand.get('experience') or 0),
             float(cand.get('ctc_current') or 0), float(cand.get('ctc_expected') or 0),
             int(cand.get('notice_period') or 0), cand.get('location',''),
             cand.get('phone',''), cand.get('email',''), cand.get('career_summary',''),
             json.dumps(cand.get('key_skills') or []),
             'worth_opening', 'Bulk added to Central Database', 'Central DB', ts(), ts()))
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


@app.route('/api/intelligence/market')
def market_intel():
    conn = get_db()
    s = conn.execute('SELECT AVG(ctc_expected) a, AVG(notice_period) b, COUNT(*) c, '
                     'SUM(CASE WHEN screening_decision="worth_opening" THEN 1 ELSE 0 END) w '
                     'FROM candidates WHERE ctc_expected>0').fetchone()
    cos = conn.execute('SELECT company, COUNT(*) s, '
                       'SUM(CASE WHEN screening_decision="worth_opening" THEN 1 ELSE 0 END) c, '
                       'SUM(CASE WHEN stage="Placed" THEN 1 ELSE 0 END) p '
                       'FROM candidates WHERE company!="" GROUP BY lower(company) ORDER BY s DESC LIMIT 10').fetchall()
    conn.close()
    total = max(s['c'] or 1, 1)
    return jsonify({'avg_expected_ctc': round(s['a'] or 0, 1), 'avg_notice_period': round(s['b'] or 0, 1),
                    'total_screened': s['c'] or 0, 'batch_conversion': round((s['w'] or 0) / total * 100, 1),
                    'source_companies': [dict(c) for c in cos]})

# Export / Import
@app.route('/api/export')
def export_data():
    conn = get_db()
    data = {
        'exported_at': ts(), 'app': 'HireLab Screener', 'version': '2.0',
        'mandates':   [dict(r) for r in conn.execute('SELECT * FROM mandates').fetchall()],
        'candidates': [dict(r) for r in conn.execute('SELECT * FROM candidates').fetchall()],
        'history':    [dict(r) for r in conn.execute('SELECT * FROM stage_history').fetchall()],
        'settings':   {r['key']: r['value'] for r in conn.execute('SELECT * FROM settings').fetchall()},
    }
    conn.close()
    fname = 'hirelab_' + str(datetime.date.today()) + '.json'
    return Response(json.dumps(data, indent=2, ensure_ascii=False), mimetype='application/json',
                    headers={'Content-Disposition': 'attachment; filename=' + fname})

@app.route('/api/import', methods=['POST'])
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
                c.execute('INSERT INTO mandates (client,role,location,division,ctc_min,ctc_max,jd,sop_text,sop_version,sop_changelog,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                          (m.get('client',''), m.get('role',''), m.get('location',''), m.get('division',''),
                           float(m.get('ctc_min') or 0), float(m.get('ctc_max') or 0), m.get('jd',''),
                           m.get('sop_text',''), m.get('sop_version', 1), m.get('sop_changelog', '[]'),
                           m.get('status', 'active'), m.get('created_at') or n))
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
                     cand.get('key_skills','[]'), cand.get('secondary_skills','[]'), cand.get('career_summary',''),
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
            return jsonify({'ok': True, 'mandates': m_done, 'candidates': cand_done, 'history': hist_done})
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() and _attempt < 4:
                time.sleep(2)
                continue
            return jsonify({'error': 'Database busy after retries. Please wait 10 seconds and try again.'}), 503
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    return jsonify({'error': 'Import failed after retries'}), 500




# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CENTRAL DATABASE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.route('/api/database/search')
def db_search():
    q        = request.args.get('q', '').strip()
    phone    = request.args.get('phone', '').strip()
    ctc_min  = float(request.args.get('ctc_min', 0) or 0)
    ctc_max  = float(request.args.get('ctc_max', 0) or 0)
    location = request.args.get('location', '').strip()
    skills   = request.args.get('skills', '').strip()
    mid      = request.args.get('mandate_id', '').strip()
    stage    = request.args.get('stage', '').strip()
    exp_min  = float(request.args.get('exp_min', 0) or 0)
    exp_max  = float(request.args.get('exp_max', 0) or 0)

    conn = get_db()
    sql = (
        'SELECT c.*, m.client, m.role as mandate_role, m.location as mandate_loc '
        'FROM candidates c LEFT JOIN mandates m ON c.mandate_id = m.id WHERE 1=1'
    )
    params = []

    if q:
        sql += ' AND (c.name LIKE ? OR c.company LIKE ? OR c.designation LIKE ? OR c.key_skills LIKE ? OR c.career_summary LIKE ?)'
        p = '%' + q + '%'
        params += [p, p, p, p, p]
    if phone:
        sql += ' AND c.phone LIKE ?'
        params.append('%' + phone + '%')
    if ctc_min > 0:
        sql += ' AND c.ctc_current >= ?'
        params.append(ctc_min)
    if ctc_max > 0:
        sql += ' AND c.ctc_current <= ?'
        params.append(ctc_max)
    if exp_min > 0:
        sql += ' AND c.experience >= ?'
        params.append(exp_min)
    if exp_max > 0:
        sql += ' AND c.experience <= ?'
        params.append(exp_max)
    if location:
        sql += ' AND c.location LIKE ?'
        params.append('%' + location + '%')
    if skills:
        sql += ' AND c.key_skills LIKE ?'
        params.append('%' + skills + '%')
    if mid:
        sql += ' AND c.mandate_id = ?'
        params.append(int(mid))
    if stage:
        sql += ' AND c.stage = ?'
        params.append(stage)

    sql += ' ORDER BY c.created_at DESC LIMIT 300'

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    out = []
    for r in rows:
        d = dict(r)
        try:    d['key_skills'] = json.loads(d['key_skills'] or '[]')
        except: d['key_skills'] = []
        out.append(d)

    return jsonify({'ok': True, 'candidates': out, 'total': len(out)})


@app.route('/api/database/stats')
def db_stats():
    conn = get_db()
    total    = conn.execute('SELECT COUNT(*) FROM candidates').fetchone()[0]
    with_cv  = conn.execute("SELECT COUNT(*) FROM candidates WHERE cv_path!=''").fetchone()[0]
    with_phone = conn.execute("SELECT COUNT(*) FROM candidates WHERE phone!=''").fetchone()[0]
    placed   = conn.execute("SELECT COUNT(*) FROM candidates WHERE stage='Placed'").fetchone()[0]
    conn.close()
    return jsonify({'total': total, 'with_cv': with_cv, 'with_phone': with_phone, 'placed': placed})


# ── Startup: runs both with gunicorn AND python server.py ──────────────────────
# This ensures DB tables exist regardless of how the app is started
try:
    migrate_old()
    init_db()
except Exception as _startup_err:
    print(f'Startup init warning: {_startup_err}')

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
