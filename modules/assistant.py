"""
HireLab Assistant — the Chrome side-panel helper that never touches Naukri.

The old extension read Naukri pages directly, and Naukri now blocks browsers
with such plugins. The Assistant only works on what the recruiter hands it:

  * text the recruiter copied from a Naukri page (Ctrl+A, Ctrl+C) and pasted, and
  * CV files the recruiter downloaded with Naukri's own Download button.

Routes (JSON unless noted, all under /api/assistant, logged-in session)
  POST /parse   multipart: page_text?, cv?  ->  fields, resume_text, candidate_text
  POST /dupes   {mandate_id, name, phone, email, company, text}  ->  who is already in HireLab
  POST /split   {text}  ->  profiles [{key, name, text}] read from a pasted search-results page

Scoring, the AI review, the review chat, the search screener and the push all
reuse the existing /api/extension/* routes, so the Assistant gives the same
answers as the old extension did.

Design rules
  - Additive: no new tables, no schema change.
  - Phone numbers and e-mail addresses are read with plain regexes on the
    server and removed before any text goes to DeepSeek.
  - Nothing pasted is stored; the CV file is stored only when the recruiter
    presses Push (through the existing /api/candidates/<id>/cv route).
"""

import re
import hashlib

from flask import Blueprint, request, jsonify

from modules.shared import get_db, effective_company_id, _core
from modules import naukri_screen as ns

bp = Blueprint('assistant', __name__, url_prefix='/api/assistant')

MAX_PAGE_CHARS = 20000
MAX_SPLIT_CHARS = 60000
MAX_SPLIT_PROFILES = 60
MAX_CV_BYTES = 8 * 1024 * 1024
CV_EXTS = ('.pdf', '.docx', '.doc', '.rtf')

_EMAIL = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
_PHONE = re.compile(r'(?<![\d])(?:\+?91[\s\-]?|0)?([6-9]\d{4}[\s\-]?\d{5})(?!\d)')


# ── small helpers ──────────────────────────────────────────────────────────
def find_contacts(*texts):
    """First Indian mobile number and first e-mail found, CV text first."""
    phone = email = ''
    for t in texts:
        t = str(t or '')
        if not email:
            m = _EMAIL.search(t)
            if m:
                email = m.group(0).strip('.').lower()
        if not phone:
            m = _PHONE.search(t)
            if m:
                phone = re.sub(r'\D', '', m.group(1))
    return phone, email


def _num(v, cast=float):
    try:
        return cast(float(str(v).replace(',', '').strip() or 0))
    except Exception:
        return cast(0)


def _s(v, n=200):
    v = '' if v is None else str(v)
    return '' if v.lower() in ('null', 'none', 'n/a', 'na') else v.strip()[:n]


def clean_fields(j):
    j = j if isinstance(j, dict) else {}
    skills = j.get('key_skills') or []
    if isinstance(skills, str):
        skills = [s for s in re.split(r'[,;\n]+', skills)]
    skills = [_s(s, 60) for s in skills if _s(s, 60)][:12]
    wh = []
    for w in (j.get('work_history') or [])[:8]:
        if isinstance(w, dict) and (w.get('company') or w.get('designation')):
            wh.append({'company': _s(w.get('company'), 120), 'designation': _s(w.get('designation'), 120),
                       'duration': _s(w.get('duration'), 60)})
    return {
        'name': _s(j.get('name'), 80),
        'company': _s(j.get('company'), 120),
        'designation': _s(j.get('designation'), 120),
        'experience': round(_num(j.get('experience')), 1),
        'ctc_current': round(_num(j.get('ctc_current')), 2),
        'ctc_expected': round(_num(j.get('ctc_expected')), 2),
        'notice_period': _num(j.get('notice_period'), int),
        'location': _s(j.get('location'), 80),
        'preferred_location': _s(j.get('preferred_location'), 120),
        'qualification': _s(j.get('qualification'), 120),
        'key_skills': skills,
        'career_summary': _s(j.get('career_summary'), 600),
        'work_history': wh,
    }


PARSE_PROMPT = (
    'You read a candidate for an Indian recruitment agency. You get up to two sources: '
    'NAUKRI PAGE (text the recruiter copied from the Naukri profile page; it contains menus and '
    'other noise — ignore those) and CV (the resume). Phone numbers and e-mails were removed. '
    'Return ONLY a JSON object with: name, company (current employer), designation (current title), '
    'experience (float, total years), ctc_current (float, lakhs per annum), ctc_expected (float, LPA), '
    'notice_period (int days; "Immediate"=0, "15 days or less"=15, "1 month"=30, "2 months"=60, '
    '"3 months"=90), location (current city), preferred_location, qualification (highest degree), '
    'key_skills (array, max 10, domain/technical skills only), career_summary (2 sentences), '
    'work_history (array of {company, designation, duration}, newest first, max 6). '
    'Prefer the NAUKRI PAGE for CTC, notice period and location; prefer the CV for skills and history. '
    'Use "" for unknown text and 0 for unknown numbers. Never invent values.')


def _read_cv(f):
    """(text, filename, error)"""
    name = (f.filename or 'cv').strip()
    low = name.lower()
    if not low.endswith(CV_EXTS):
        return '', name, 'Only PDF, Word (.docx/.doc) or RTF CVs can be read.'
    data = f.read(MAX_CV_BYTES + 1)
    if len(data) > MAX_CV_BYTES:
        return '', name, 'This CV is larger than 8 MB.'
    core = _core()
    text, err = core.extract_text_from_file(data, name)
    if err or not text or len(text.strip()) < 50:
        return '', name, ('This CV has no readable text (it is probably a scanned image). '
                          'Ask the candidate for a PDF/Word CV, or paste the Naukri page instead.')
    return text, name, None


# ── routes ─────────────────────────────────────────────────────────────────
@bp.route('/parse', methods=['POST', 'OPTIONS'])
@ns._ext_route
def assistant_parse():
    core = _core()
    page = str(request.form.get('page_text') or (request.get_json(silent=True) or {}).get('page_text') or '')
    page = page[:MAX_PAGE_CHARS]
    resume, cv_name = '', ''
    f = request.files.get('cv')
    if f:
        resume, cv_name, err = _read_cv(f)
        if err:
            return jsonify({'ok': False, 'kind': 'cv', 'error': err}), 400
    if len(page.strip()) < 40 and not resume:
        return ns._err('Paste the Naukri page text or add a CV first.', 400)
    phone, email = find_contacts(resume, page)

    key = ns._deepseek_key(core)
    if not key:
        return jsonify({'ok': False, 'kind': 'nokey', 'error': 'The DeepSeek API key is not set in HireLab Settings.'}), 400
    if not ns._rate_ok('assistant-parse', ns.real_user_id(), 120):
        return ns._err('Too many requests — wait a minute and try again.', 429)
    parts = []
    if page.strip():
        parts.append('NAUKRI PAGE:\n' + ns.redact(page)[:9000])
    if resume:
        parts.append('CV:\n' + ns.redact(resume)[:9000])
    try:
        j = ns._ask_deepseek(core, key, PARSE_PROMPT, '\n\n'.join(parts), 1200, 'assistant-parse')
    except Exception as exc:
        return ns._ai_error(core, exc)
    fields = clean_fields(j)
    fields['phone'], fields['email'] = phone, email
    # The profile text used for scoring: the pasted page if there is one,
    # else a compact profile built from the parsed fields.
    if page.strip():
        cand_text = ns.redact(page)[:6000]
    else:
        cand_text = '\n'.join(x for x in [
            fields['name'], fields['designation'] + (' at ' + fields['company'] if fields['company'] else ''),
            ('%s years experience' % fields['experience']) if fields['experience'] else '',
            ('Current CTC %s LPA' % fields['ctc_current']) if fields['ctc_current'] else '',
            ('Location ' + fields['location']) if fields['location'] else '',
            'Skills: ' + ', '.join(fields['key_skills']) if fields['key_skills'] else '',
            fields['career_summary']] if x)
    return jsonify({'ok': True, 'fields': fields, 'cv_name': cv_name,
                    'resume_text': ns.redact(resume)[:12000], 'candidate_text': cand_text,
                    'has_cv': bool(resume), 'has_page': bool(page.strip())})


def _digits(p):
    return re.sub(r'\D', '', str(p or ''))[-10:]


@bp.route('/dupes', methods=['POST', 'OPTIONS'])
@ns._ext_route
def assistant_dupes():
    d = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        m, err = ns._load_mandate(conn, d.get('mandate_id'))
        if err:
            return err
        co = effective_company_id()
        try:
            from modules.access import allowed_mandate_ids, _pool_id
            allowed, pool_ok = allowed_mandate_ids(conn), _pool_id(conn)
        except ImportError:
            allowed, pool_ok = None, 0
        try:
            from modules.shared import central_mandate_id
            pool = int(central_mandate_id() or 0)
        except Exception:
            pool = 0
        name = str(d.get('name') or '')[:80]
        phone, email = _digits(d.get('phone')), str(d.get('email') or '').strip().lower()
        card = {'name': name, 'text': ns.redact(d.get('company') or '') + ' ' + ns.redact(d.get('text') or '')[:1500], 'ref': ''}
        cols = 'SELECT id, name, company, designation, mandate_id, stage, phone, email FROM candidates WHERE owner_id=? AND '
        rows, how = [], {}
        if len(phone) == 10:
            for r in conn.execute(cols + "REPLACE(REPLACE(REPLACE(phone,' ',''),'-',''),'+','') LIKE ? LIMIT 50",
                                  (co, '%' + phone)).fetchall():
                rows.append(r); how[r['id']] = 'exact'
        if email:
            for r in conn.execute(cols + 'LOWER(email)=? LIMIT 50', (co, email)).fetchall():
                rows.append(r); how[r['id']] = 'exact'
        first = (ns.norm_name(name).split() or [''])[0]
        if first:
            rows += conn.execute(cols + 'LOWER(name) LIKE ? LIMIT 300', (co, '%' + first + '%')).fetchall()
        hits, seen, mc = [], set(), {}
        for r in rows:
            r = dict(r)
            if r['id'] in seen:
                continue
            seen.add(r['id'])
            mid = int(r['mandate_id'] or 0)
            if allowed is not None and mid not in allowed and not (mid == pool_ok and mid):
                continue
            lvl = how.get(r['id']) or ns.match_level(card, r)
            if not lvl:
                continue
            if mid not in mc:
                x = conn.execute('SELECT id, role, client, status FROM mandates WHERE id=? AND owner_id=?', (mid, co)).fetchone()
                mc[mid] = dict(x) if x else None
            mm = mc[mid]
            if not mm:
                continue
            where = 'this_job' if mid == int(m['id']) else ('pool' if mid == pool else 'other_job')
            if lvl == 'name' and where != 'this_job':
                continue
            hits.append({'candidate_id': r['id'], 'mandate_id': mid, 'where': where, 'level': lvl,
                         'stage': r['stage'] or '', 'job': mm['role'] or '', 'client': mm['client'] or '',
                         'job_status': mm['status'] or '', 'name': r['name'] or ''})
        rank = {'exact': 0, 'strong': 1, 'name': 2}
        wrank = {'this_job': 0, 'other_job': 1, 'pool': 2}
        hits.sort(key=lambda h: (wrank[h['where']], rank[h['level']]))
        return jsonify({'ok': True, 'hits': hits[:8], 'mandate_id': m['id']})
    finally:
        conn.close()


SPLIT_PROMPT = (
    'The user pasted the text of a Naukri Resdex SEARCH RESULTS page. Each line starts with its '
    'line number. The page holds several candidate cards plus menus, filters and ads. '
    'Find every candidate card. Return ONLY JSON: {"profiles": [{"name": "...", "start": <first line>, '
    '"end": <last line>}]} in page order. A card starts at the candidate name and ends just before '
    'the next candidate name. Do not include filter panels, menus or footer lines. If there are no '
    'candidates, return {"profiles": []}.')


def split_by_lines(text, spans):
    lines = text.split('\n')
    out, used = [], set()
    for sp in spans:
        if not isinstance(sp, dict):
            continue
        try:
            a, b = int(sp.get('start')), int(sp.get('end'))
        except (TypeError, ValueError):
            continue
        a, b = max(1, a), min(len(lines), max(a, b))
        if a > len(lines) or (a, b) in used:
            continue
        used.add((a, b))
        body = '\n'.join(lines[a - 1:b]).strip()
        name = _s(sp.get('name'), 80) or (body.split('\n')[0][:80] if body else '')
        if len(body) < 20 or not name:
            continue
        key = 'p' + hashlib.sha1((name + '|' + body[:200]).encode('utf-8', 'ignore')).hexdigest()[:12]
        out.append({'key': key, 'name': name, 'text': body[:ns.MAX_PROFILE_CHARS]})
        if len(out) >= MAX_SPLIT_PROFILES:
            break
    return out


@bp.route('/split', methods=['POST', 'OPTIONS'])
@ns._ext_route
def assistant_split():
    core = _core()
    d = request.get_json(silent=True) or {}
    raw = str(d.get('text') or '')[:MAX_SPLIT_CHARS]
    text = ns.redact(raw)
    lines = text.split('\n')
    if len(text) < 80:
        return ns._err('Paste the whole search results page (Ctrl+A, Ctrl+C on the page).', 400)
    key = ns._deepseek_key(core)
    if not key:
        return jsonify({'ok': False, 'kind': 'nokey', 'error': 'The DeepSeek API key is not set in HireLab Settings.'}), 400
    if not ns._rate_ok('assistant-split', ns.real_user_id(), 60):
        return ns._err('Too many requests — wait a minute and try again.', 429)
    numbered = '\n'.join('%d| %s' % (i + 1, l[:300]) for i, l in enumerate(lines))
    try:
        j = ns._ask_deepseek(core, key, SPLIT_PROMPT, numbered, 3000, 'assistant-split', timeout=90)
    except Exception as exc:
        return ns._ai_error(core, exc)
    profiles = split_by_lines(text, (j or {}).get('profiles') or [])
    if not profiles:
        return ns._err('No candidate profiles were found in the pasted text. Copy the whole results page and try again.', 422)
    return jsonify({'ok': True, 'profiles': profiles, 'count': len(profiles)})
