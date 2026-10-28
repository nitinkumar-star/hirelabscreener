"""
HireLab Assistant — 1-click LinkedIn profile capture  (Oct 2026)

The Chrome extension reads the LinkedIn profile tab ONLY when the recruiter
presses "Extract this LinkedIn profile" (no content script, nothing runs in
the background, nothing is read on Naukri or any other site). It sends the
visible profile text + the public profile URL here.

Routes (JSON, under /api/assistant/linkedin, logged-in session):
  POST /parse  {url, text, title}
               -> fields (name, headline, current company/title, location,
                  experience, work history, education, skills, about),
                  public_url, and who is already in HireLab (candidate / BD)
  POST /save   {target: 'candidate'|'bd', url, fields, mandate_id?, email?, phone?}
               candidate -> Central Database (or a chosen job), source LinkedIn,
                            linkedin_url saved; same LinkedIn URL = update, never a duplicate
               bd        -> BD workspace person; the company is found or created
                            as a prospect (details can be filled later)

Additive only: no new tables (candidates.linkedin_url and crm_contacts.linkedin
already exist).
ROLLBACK: remove 'linkedin_capture' from modules/__init__.py — the extension's
LinkedIn button then shows "HireLab is not updated yet".
"""

import json
import re

from flask import Blueprint, request, jsonify

from modules.shared import get_db, ts, effective_company_id, real_user_id, _core
from modules import naukri_screen as ns

bp = Blueprint('linkedin_capture', __name__, url_prefix='/api/assistant/linkedin')

MAX_TEXT = 30000
_LI_URL = re.compile(r'^https?://([a-z]{2,3}\.)?(www\.)?linkedin\.com/in/([^/?#]+)', re.I)


def public_url(url):
    """https://www.linkedin.com/in/<slug>/ — the shareable form, without tracking bits."""
    m = _LI_URL.match(str(url or '').strip())
    if not m:
        return ''
    return 'https://www.linkedin.com/in/%s/' % m.group(3).strip().lower()


def _slug(url):
    m = _LI_URL.match(str(url or '').strip())
    return m.group(3).strip().lower() if m else ''


def _s(v, n=200):
    v = '' if v is None else str(v)
    return '' if v.strip().lower() in ('null', 'none', 'n/a', 'na', '-') else re.sub(r'\s+', ' ', v).strip()[:n]


def _num(v):
    try:
        return round(float(str(v).replace(',', '').strip() or 0), 1)
    except Exception:
        return 0.0


PROMPT = (
    'You read a LinkedIn profile page that a recruiter in India copied. The text contains menus, ads, '
    '"People also viewed" and other noise — ignore those; read ONLY the profile owner. Return ONLY a JSON object: '
    '{"name": "", "headline": "", "designation": "<current job title>", "company": "<current employer>", '
    '"location": "<city, state>", "experience": <float total years of work, computed from the Experience dates>, '
    '"about": "<the About section, max 600 chars>", '
    '"work_history": [{"company": "", "designation": "", "start_date": "<e.g. Jan 2021>", "end_date": "<e.g. Mar 2024 or Present>", '
    '"is_current": true|false, "location": "", "description": "<max 300 chars>"}], '
    '"education": [{"school": "", "degree": "", "years": ""}], '
    '"key_skills": ["<max 15 domain / technical skills>"], '
    '"industry": "<the industry of the current employer if stated>"} . '
    'Newest job first, max 8 jobs. If one company lists several roles, write one entry per role. '
    'Use "" for unknown text and 0 for unknown numbers. Never invent anything.')


def clean(j):
    j = j if isinstance(j, dict) else {}
    wh = []
    for w in (j.get('work_history') or [])[:8]:
        if isinstance(w, dict) and (w.get('company') or w.get('designation')):
            end = _s(w.get('end_date'), 30)
            wh.append({'company': _s(w.get('company'), 120), 'designation': _s(w.get('designation'), 120),
                       'start_date': _s(w.get('start_date'), 30), 'end_date': '' if end.lower() == 'present' else end,
                       'is_current': bool(w.get('is_current')) or end.lower() == 'present',
                       'location': _s(w.get('location'), 80), 'description': _s(w.get('description'), 300)})
    edu = []
    for e in (j.get('education') or [])[:4]:
        if isinstance(e, dict) and (e.get('school') or e.get('degree')):
            edu.append({'school': _s(e.get('school'), 120), 'degree': _s(e.get('degree'), 120), 'years': _s(e.get('years'), 30)})
    skills = j.get('key_skills') or []
    if isinstance(skills, str):
        skills = re.split(r'[,;\n]+', skills)
    skills = [x for x in (_s(s, 60) for s in skills) if x][:15]
    f = {'name': _s(j.get('name'), 80), 'headline': _s(j.get('headline'), 220),
         'designation': _s(j.get('designation'), 120), 'company': _s(j.get('company'), 120),
         'location': _s(j.get('location'), 80), 'experience': _num(j.get('experience')),
         'about': _s(j.get('about'), 600), 'work_history': wh, 'education': edu, 'key_skills': skills,
         'industry': _s(j.get('industry'), 80)}
    if wh and not f['company']:
        f['company'] = wh[0]['company']
    if wh and not f['designation']:
        f['designation'] = wh[0]['designation']
    return f


def find_existing(conn, oid, url, name='', company=''):
    """{'candidate': {...}|None, 'bd': {...}|None} for this LinkedIn profile."""
    slug = _slug(url)
    out = {'candidate': None, 'bd': None}
    if slug:
        like = '%linkedin.com/in/' + slug + '%'
        r = conn.execute("SELECT c.id, c.name, c.mandate_id, c.stage, m.role, m.client, m.status FROM candidates c "
                         "LEFT JOIN mandates m ON m.id=c.mandate_id WHERE c.owner_id=? AND LOWER(COALESCE(c.linkedin_url,'')) LIKE ? "
                         "ORDER BY c.id DESC LIMIT 1", (oid, like)).fetchone()
        if r:
            out['candidate'] = {'id': r['id'], 'name': r['name'], 'mandate_id': r['mandate_id'], 'stage': r['stage'] or '',
                                'where': 'Central Database' if (r['status'] or '') == 'central'
                                else ((r['role'] or 'a job') + (' — ' + r['client'] if r['client'] else ''))}
        try:
            b = conn.execute("SELECT p.id, p.name, c.name AS company FROM crm_contacts p JOIN crm_clients c ON c.id=p.client_id "
                             "WHERE p.company_id=? AND p.is_active=1 AND LOWER(COALESCE(p.linkedin,'')) LIKE ? "
                             "ORDER BY p.id DESC LIMIT 1", (oid, like)).fetchone()
            if b:
                out['bd'] = {'id': b['id'], 'name': b['name'], 'company': b['company']}
        except Exception:
            pass
    return out


@bp.route('/parse', methods=['POST', 'OPTIONS'])
@ns._ext_route
def li_parse():
    core = _core()
    d = request.get_json(silent=True) or {}
    url = public_url(d.get('url'))
    text = str(d.get('text') or '')[:MAX_TEXT]
    if not url:
        return ns._err('Open a LinkedIn profile page (linkedin.com/in/…) first.', 400)
    if len(text.strip()) < 80:
        return ns._err('Could not read this profile. Scroll the page once so LinkedIn loads it, then try again.', 400)
    key = ns._deepseek_key(core)
    if not key:
        return jsonify({'ok': False, 'kind': 'nokey', 'error': 'The DeepSeek API key is not set in HireLab Settings.'}), 400
    if not ns._rate_ok('linkedin-parse', ns.real_user_id(), 60):
        return ns._err('Too many requests — wait a minute and try again.', 429)
    try:
        j = ns._ask_deepseek(core, key, PROMPT, 'PAGE TITLE: ' + _s(d.get('title'), 200) + '\n\nPROFILE PAGE:\n'
                             + ns.redact(text)[:14000], 1800, 'linkedin-parse')
    except Exception as exc:
        return ns._ai_error(core, exc)
    f = clean(j)
    if not f['name']:
        t = _s(d.get('title'), 200)
        f['name'] = _s(re.split(r'\s[|\-–]\s', t)[0], 80) if t else ''
    conn = get_db()
    try:
        ex = find_existing(conn, effective_company_id(), url, f['name'], f['company'])
    finally:
        conn.close()
    return jsonify({'ok': True, 'fields': f, 'public_url': url, 'existing': ex})


def _summary(f):
    parts = []
    if f.get('headline'):
        parts.append(f['headline'])
    if f.get('about'):
        parts.append(f['about'])
    if f.get('education'):
        parts.append('Education: ' + '; '.join(', '.join(x for x in (e.get('degree'), e.get('school'), e.get('years')) if x)
                                              for e in f['education']))
    return '\n'.join(parts)[:1500]


def _save_candidate(conn, core, f, url, mid_raw, email, phone):
    oid = effective_company_id()
    to_central = str(mid_raw or 'central').strip().lower() in ('', 'central')
    if to_central:
        mid = core.get_or_create_central_mandate()
    else:
        try:
            mid = int(mid_raw)
        except Exception:
            return None, ('Choose a job or the Central Database.', 400)
        m = conn.execute('SELECT id FROM mandates WHERE id=? AND owner_id=?', (mid, oid)).fetchone()
        if not m:
            return None, ('That job is not in your workspace.', 403)
        try:
            from modules.access import can_see_mandate
            if not can_see_mandate(conn, mid, write=True):
                return None, ('This job is not assigned to you.', 403)
        except Exception:
            pass
    skills = json.dumps(f.get('key_skills') or [])
    summary = _summary(f)
    slug = _slug(url)
    like = '%linkedin.com/in/' + slug + '%'
    # same LinkedIn profile already in this job / the pool → update it (stage & journey untouched)
    ex = conn.execute("SELECT * FROM candidates WHERE mandate_id=? AND owner_id=? AND LOWER(COALESCE(linkedin_url,'')) LIKE ? "
                      "ORDER BY id DESC LIMIT 1", (mid, oid, like)).fetchone() if slug else None
    if not ex and email:
        ex = conn.execute('SELECT * FROM candidates WHERE mandate_id=? AND owner_id=? AND LOWER(email)=LOWER(?) LIMIT 1',
                          (mid, oid, email)).fetchone()
    now = ts()
    if ex:
        conn.execute('UPDATE candidates SET name=?, company=?, designation=?, experience=COALESCE(NULLIF(?,0),experience), '
                     'location=COALESCE(NULLIF(?,\'\'),location), linkedin_url=?, key_skills=CASE WHEN ?=\'[]\' THEN key_skills ELSE ? END, '
                     'career_summary=COALESCE(NULLIF(?,\'\'),career_summary), email=COALESCE(NULLIF(email,\'\'),?), '
                     'phone=COALESCE(NULLIF(phone,\'\'),?), updated_at=? WHERE id=?',
                     (f['name'] or ex['name'], f['company'], f['designation'], f['experience'], f['location'], url,
                      skills, skills, summary, email, phone, now, ex['id']))
        if f.get('work_history'):
            core._save_wh_for(conn, ex['id'], f['work_history'])
        conn.commit()
        return {'action': 'updated', 'candidate_id': ex['id'], 'mandate_id': mid, 'central': to_central}, None
    stage = core.CENTRAL_STAGE if to_central else 'Screening'
    cur = conn.execute('INSERT INTO candidates (mandate_id,name,company,designation,experience,location,phone,email,'
                       'career_summary,key_skills,screening_decision,ai_reasoning,stage,created_at,updated_at) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (mid, f['name'], f['company'], f['designation'], f['experience'], f['location'], phone, email,
                        summary, skills, 'worth_opening', 'Captured from LinkedIn', stage, now, now))
    cid = cur.lastrowid
    conn.execute('UPDATE candidates SET linkedin_url=? WHERE id=?', (url, cid))
    try:
        conn.execute("UPDATE candidates SET source_channel='linkedin' WHERE id=?", (cid,))
    except Exception:
        pass
    conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                 (cid, '', stage, 'Captured from LinkedIn' + (' into Central Database' if to_central else ''), now))
    if f.get('work_history'):
        core._save_wh_for(conn, cid, f['work_history'])
    try:
        core._link_to_known_person(conn, cid)
    except Exception:
        pass
    conn.commit()
    try:
        core._touch_mandate_usage(mid)
    except Exception:
        pass
    try:
        core.queue_embedding_job(cid)
    except Exception:
        pass
    return {'action': 'added', 'candidate_id': cid, 'mandate_id': mid, 'central': to_central}, None


def _save_bd(conn, f, url, email, phone, company_override=''):
    from modules import bd_records as br
    from modules.crm import ContactService
    company = _s(company_override or f.get('company'), 160)
    if not company:
        return None, ('No current company on this profile — type the company name.', 400)
    oid = effective_company_id()
    try:
        client_id, cname, created = br.quick_company(conn, company, {'industry': f.get('industry') or ''})
    except br.RecordError as e:
        return None, (e.message, getattr(e, 'code', 400))
    slug = _slug(url)
    like = '%linkedin.com/in/' + slug + '%'
    ex = conn.execute("SELECT id FROM crm_contacts WHERE company_id=? AND is_active=1 AND LOWER(COALESCE(linkedin,'')) LIKE ? "
                      "ORDER BY id DESC LIMIT 1", (oid, like)).fetchone() if slug else None
    note = '\n'.join(x for x in [('LinkedIn: ' + f['headline']) if f.get('headline') else '', f.get('location') or '',
                                 (f.get('about') or '')[:400]] if x)
    if ex:
        conn.execute("UPDATE crm_contacts SET name=?, designation=COALESCE(NULLIF(?,''),designation), linkedin=?, "
                     "email=COALESCE(NULLIF(email,''),?), phone=COALESCE(NULLIF(phone,''),?), client_id=?, updated_at=? WHERE id=?",
                     (f['name'], f.get('designation') or '', url, email, phone, client_id, ts(), ex['id']))
        conn.commit()
        return {'action': 'updated', 'person_id': ex['id'], 'company_id': client_id, 'company': cname,
                'company_created': created}, None
    body = {'name': f['name'], 'designation': f.get('designation') or '', 'linkedin': url, 'notes': note}
    if email:
        body['email'] = email
    if phone:
        body['phone'] = phone
    try:
        pid = ContactService.create(conn, client_id, body)
    except Exception as e:
        msg = getattr(e, 'message', None) or str(e)
        return None, (msg, getattr(e, 'code', 400))
    return {'action': 'added', 'person_id': pid, 'company_id': client_id, 'company': cname, 'company_created': created}, None


@bp.route('/save', methods=['POST', 'OPTIONS'])
@ns._ext_route
def li_save():
    core = _core()
    d = request.get_json(silent=True) or {}
    url = public_url(d.get('url'))
    if not url:
        return ns._err('The LinkedIn profile link is missing.', 400)
    f = clean(d.get('fields') or {})
    if not f['name']:
        return ns._err('Name is missing — fill it in before saving.', 400)
    email = _s(d.get('email'), 120).lower()
    if email and not re.match(r'^[^@\s]+@[^@\s]+\.[a-z]{2,}$', email):
        return ns._err('That e-mail address looks wrong.', 400)
    phone = re.sub(r'[^\d+]', '', _s(d.get('phone'), 30))
    target = (d.get('target') or 'candidate').strip().lower()
    try:
        from modules.freelancer import _is_freelancer
        if _is_freelancer() and target == 'bd':
            return ns._err('Freelancers cannot save to the BD workspace.', 403)
    except Exception:
        pass
    conn = get_db()
    try:
        if target == 'bd':
            try:
                if core.workspace_mode() == 'corporate':
                    return ns._err('This workspace has no BD module.', 400)
            except Exception:
                pass
            out, err = _save_bd(conn, f, url, email, phone, d.get('company') or '')
        else:
            out, err = _save_candidate(conn, core, f, url, d.get('mandate_id'), email, phone)
    finally:
        conn.close()
    if err:
        return ns._err(err[0], err[1])
    try:
        core.log_activity('linkedin.capture', 'Saved LinkedIn profile %s to %s' % (f['name'], 'BD' if target == 'bd' else 'candidates'),
                          entity_type='candidate' if target != 'bd' else 'contact',
                          entity_id=out.get('candidate_id') or out.get('person_id') or 0)
    except Exception:
        pass
    return jsonify(dict(out, ok=True, public_url=url, target=target))
