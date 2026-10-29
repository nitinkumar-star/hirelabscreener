"""
RecruitOS — Upload CVs from the Talent page  (Oct 2026)

Before this, "+ Add candidate" on the Talent page could READ a CV to fill the
form, but the CV file itself was never stored, and adding to a job needed a
job to be open. This endpoint takes one CV file and does the whole job:

  POST /api/talent/upload-cv   multipart: cv, mandate_id (0/empty = Central Database),
                               name?, company? (only when the AI could not read them)
    -> {ok, id, mandate_id, name, central}                    candidate created, CV attached
    -> {ok:false, duplicate:{id, mandate_id, name}}  409      same e-mail / phone already in the database
    -> {ok:false, need:['name'], guess:{...}}       422       could not read the name: ask the recruiter

The page uploads files one by one (each needs an AI read of ~5-10 s), so a
batch of 20 CVs never hits a request timeout.

Reuses the core: extract_text_from_file + _ai_parse_resume_text (same reader
as every other CV upload), the Central Database pool mandate, person linking
and the CV folder. Recruiters can only add to jobs they work on.
"""

import os
import re
import json
import datetime

from flask import Blueprint, request, jsonify

from modules.shared import get_db, ts, effective_company_id, login_required, _core

bp = Blueprint('cv_upload', __name__, url_prefix='/api/talent')

CV_EXTS = ('.pdf', '.doc', '.docx')
MAX_CV_BYTES = 10 * 1024 * 1024


def _num(v, cast=float):
    try:
        return cast(float(str(v).replace(',', '').strip())) if v not in (None, '') else 0
    except Exception:
        return 0


def _s(v, n=300):
    v = '' if v is None else str(v)
    return '' if v.strip().lower() in ('null', 'none', 'n/a') else v.strip()[:n]


def _phone10(p):
    d = re.sub(r'\D', '', str(p or ''))
    return d[-10:] if len(d) >= 10 else ''


def _guess_name(filename):
    """'Rahul_Sharma_CV (2).pdf' -> 'Rahul Sharma' (a hint only, never saved unasked)."""
    base = os.path.splitext(os.path.basename(filename or ''))[0]
    base = re.sub(r'[\(\[].*?[\)\]]', ' ', base)
    base = re.sub(r'[_\-.\d]+', ' ', base)
    base = re.sub(r'(?i)\b(cv|resume|curriculum|vitae|profile|updated|latest|final|naukri|copy)\b', ' ', base)
    words = [w for w in base.split() if len(w) > 1][:3]
    return ' '.join(w.capitalize() for w in words)


def _find_person(conn, oid, email, phone):
    em = (email or '').strip().lower()
    if em:
        r = conn.execute("SELECT id, mandate_id, name FROM candidates WHERE owner_id=? "
                         "AND LOWER(TRIM(COALESCE(email,'')))=? ORDER BY id LIMIT 1", (oid, em)).fetchone()
        if r:
            return r
    ph = _phone10(phone)
    if ph:
        for r in conn.execute("SELECT id, mandate_id, name, phone FROM candidates WHERE owner_id=? "
                              "AND COALESCE(phone,'') LIKE ? ORDER BY id", (oid, '%' + ph[-4:] + '%')):
            if _phone10(r['phone']) == ph:
                return r
    return None


def _may_add_to(conn, mid):
    core = _core()
    if not core._tenant_owns_mandate(conn, mid):
        return False
    try:
        from modules.access import scoped_user, can_see_mandate
        if scoped_user():
            return can_see_mandate(conn, mid, write=True)
    except Exception:
        pass
    return True


@bp.route('/upload-cv', methods=['POST'])
@login_required
def upload_cv():
    core = _core()
    try:
        from modules.freelancer import block_if_freelancer
        blocked = block_if_freelancer()
        if blocked:
            return blocked
    except Exception:
        pass
    f = request.files.get('cv')
    if not f or not f.filename:
        return jsonify({'error': 'Choose a CV file.'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in CV_EXTS:
        return jsonify({'error': 'PDF or Word files only.'}), 400
    data = f.read()
    if not data:
        return jsonify({'error': 'The file is empty.'}), 400
    if len(data) > MAX_CV_BYTES:
        return jsonify({'error': 'The file is larger than 10 MB.'}), 400

    try:
        mid = int(request.form.get('mandate_id') or 0)
    except Exception:
        mid = 0
    oid = effective_company_id()
    conn = get_db()
    try:
        if mid and not _may_add_to(conn, mid):
            return jsonify({'error': 'Job not found.'}), 404
    finally:
        conn.close()

    # 1) read the CV
    text, err = core.extract_text_from_file(data, f.filename)
    parsed = {}
    note = ''
    key = (core.get_setting('deepseek_api_key') or '').strip()
    if text and len(text) >= 50 and key:
        p, perr, _code = core._ai_parse_resume_text(text, key)
        if isinstance(p, dict):
            parsed = p
        else:
            note = perr or 'The AI could not read this CV.'
    elif not key:
        note = 'AI is not set up (Settings -> DeepSeek key), so the details were not read.'
    else:
        note = 'No text could be read from this file (it may be a scanned image).'

    name = _s(request.form.get('name'), 120) or _s(parsed.get('name'), 120)
    company = _s(request.form.get('company'), 160) or _s(parsed.get('company'), 160)
    email = _s(parsed.get('email'), 200).lower()
    phone = _s(parsed.get('phone'), 40)
    if not name:
        return jsonify({'ok': False, 'need': ['name'], 'error': note or 'The name could not be read from this CV.',
                        'guess': {'name': _guess_name(f.filename), 'company': company}}), 422

    conn = get_db()
    try:
        # 2) already in the database? Never make a second copy of the person.
        dup = _find_person(conn, oid, email, phone)
        if dup:
            return jsonify({'ok': False, 'duplicate': {'id': dup['id'], 'mandate_id': dup['mandate_id'],
                                                       'name': dup['name']},
                            'error': f'{dup["name"]} is already in your database.'}), 409

        # 3) create the candidate (same columns as "+ Add candidate")
        to_central = not mid
        if to_central:
            mid = core.get_or_create_central_mandate()
        stage = core.CENTRAL_STAGE if to_central else 'Screening'
        skills = parsed.get('key_skills') if isinstance(parsed.get('key_skills'), list) else []
        sec = parsed.get('secondary_skills') if isinstance(parsed.get('secondary_skills'), list) else []
        now = ts()
        c = conn.cursor()
        c.execute(
            'INSERT INTO candidates (mandate_id,name,company,designation,experience,ctc_current,ctc_expected,'
            'notice_period,location,phone,email,career_summary,key_skills,secondary_skills,qualification,'
            'preferred_location,industry_background,screening_decision,ai_reasoning,stage,source_channel,'
            'created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (mid, name, company, _s(parsed.get('designation'), 160), _num(parsed.get('experience')),
             _num(parsed.get('ctc_current')), _num(parsed.get('ctc_expected')),
             _num(parsed.get('notice_period'), int), _s(parsed.get('location'), 120), phone, email,
             _s(parsed.get('career_summary'), 1500), json.dumps([_s(x, 60) for x in skills[:15] if _s(x, 60)]),
             json.dumps([_s(x, 60) for x in sec[:20] if _s(x, 60)]), _s(parsed.get('qualification'), 160),
             _s(parsed.get('preferred_location'), 160), _s(parsed.get('industry_background'), 160),
             'worth_opening', 'Added from an uploaded CV', stage, 'manual', now, now))
        cid = c.lastrowid
        # 4) store the CV file (same naming as the profile's CV upload)
        fname = 'c' + str(cid) + '_' + datetime.datetime.now().strftime('%Y%m%d%H%M%S') + ext
        with open(os.path.join(core.CV_DIR, fname), 'wb') as fh:
            fh.write(data)
        c.execute('UPDATE candidates SET cv_path=?, cv_original_name=? WHERE id=?', (fname, f.filename[:200], cid))
        c.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                  (cid, '', stage, ('Added to Central Database' if to_central else 'Added to the job')
                   + ' from an uploaded CV: ' + f.filename[:120], now))
        core._link_to_known_person(conn, cid)
        conn.commit()
    finally:
        conn.close()
    try:
        core.log_activity('candidate.cv_uploaded', f'Added {name} from an uploaded CV',
                          entity_type='candidate', entity_id=cid)
    except Exception:
        pass
    return jsonify({'ok': True, 'id': cid, 'mandate_id': mid, 'name': name, 'company': company,
                    'central': to_central, 'note': note,
                    'read': bool(parsed), 'designation': _s(parsed.get('designation'), 160)})
