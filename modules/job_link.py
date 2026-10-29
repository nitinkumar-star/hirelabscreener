"""
RecruitOS — Job application link  (Oct 2026)

A link per job (mandate) that the recruiter posts on LinkedIn, WhatsApp
groups, Facebook, Naukri posts and so on. Whoever opens it sees the job and
uploads their CV; the application goes straight onto THAT job's pipeline
(Screening, CV attached), and the job's recruiters are notified.

  GET  /api/mandates/<mid>/apply-link           (login) -> {url, links per channel, role}
  GET  /api/public/job-link?c=&m=&k=            (public) -> role / location / experience for the apply page

The link reuses the public application page (/apply) and its submit route; it
only adds `k`, a signature of (company, job). Without a valid `k` an
application still lands in Responses exactly as before. Job ids are numbers,
so the signature stops anyone from walking other jobs by changing `m`.

Hook: server.submit_form() calls on_submission() after saving the application.
"""

import hmac
import hashlib

from flask import Blueprint, request, jsonify

from modules.shared import get_db, ts, effective_company_id, login_required, _core

bp = Blueprint('job_link', __name__, url_prefix='/api')

CHANNELS = (('linkedin', 'LinkedIn'), ('whatsapp', 'WhatsApp'), ('facebook', 'Facebook'),
            ('naukri', 'Naukri'), ('email', 'E-mail'), ('other', 'Other'))
CHANNEL_KEYS = {k for k, _ in CHANNELS}


def _secret():
    core = _core()
    try:
        key = core.app.secret_key
    except Exception:
        key = None
    return (key if isinstance(key, bytes) else str(key or 'hirelab').encode())


def sign(company_id, mid):
    msg = ('job-link:%d:%d' % (int(company_id), int(mid))).encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()[:16]


def verify(company_id, mid, k):
    try:
        return bool(k) and hmac.compare_digest(sign(company_id, mid), str(k))
    except Exception:
        return False


def _base():
    try:
        return _core()._camp_base_url().rstrip('/')
    except Exception:
        return request.host_url.rstrip('/')


@bp.route('/mandates/<int:mid>/apply-link', methods=['GET'])
@login_required
def apply_link(mid):
    oid = effective_company_id()
    conn = get_db()
    try:
        m = conn.execute('SELECT id, role, client, location, status FROM mandates WHERE id=? AND owner_id=?',
                         (mid, oid)).fetchone()
        if not m or (m['status'] or '') == 'central':
            return jsonify({'error': 'Job not found'}), 404
        n = conn.execute("SELECT COUNT(*) n FROM submissions WHERE owner_id=? AND mandate_id=? "
                         "AND COALESCE(job_link,0)=1", (oid, mid)).fetchone()['n']
    finally:
        conn.close()
    url = '%s/apply?c=%d&m=%d&k=%s' % (_base(), oid, mid, sign(oid, mid))
    return jsonify({'ok': True, 'url': url, 'role': m['role'] or '', 'location': m['location'] or '',
                    'active': (m['status'] or 'active') == 'active', 'applications': n,
                    'channels': [{'key': k, 'label': l, 'url': url + '&src=' + k} for k, l in CHANNELS]})


@bp.route('/public/job-link', methods=['GET'])
def public_job():
    try:
        oid, mid = int(request.args.get('c') or 0), int(request.args.get('m') or 0)
    except Exception:
        return jsonify({'error': 'Not found'}), 404
    if not verify(oid, mid, request.args.get('k')):
        return jsonify({'error': 'Not found'}), 404
    conn = get_db()
    try:
        m = conn.execute('SELECT role, location, experience, exp_min, exp_max, work_mode, employment_type, status '
                         'FROM mandates WHERE id=? AND owner_id=?', (mid, oid)).fetchone()
        co = conn.execute('SELECT name FROM companies WHERE id=?', (oid,)).fetchone()
    finally:
        conn.close()
    if not m:
        return jsonify({'error': 'Not found'}), 404
    exp = m['experience'] or ''
    if not exp and (m['exp_min'] or m['exp_max']):
        exp = '%s-%s years' % (m['exp_min'] or 0, m['exp_max'] or '')
    # The client's name is never shown: agencies keep it confidential.
    return jsonify({'ok': True, 'company': co['name'] if co else '', 'role': m['role'] or '',
                    'location': m['location'] or '', 'experience': exp, 'work_mode': m['work_mode'] or '',
                    'open': (m['status'] or 'active') == 'active'})


def _recruiters(conn, oid, mid):
    ids = [r['user_id'] for r in conn.execute(
        'SELECT user_id FROM mandate_assignees WHERE company_id=? AND mandate_id=? AND is_active=1', (oid, mid))]
    if not ids:
        m = conn.execute('SELECT assigned_user_id, created_by FROM mandates WHERE id=?', (mid,)).fetchone()
        ids = [x for x in ((m['assigned_user_id'], m['created_by']) if m else ()) if x]
    if not ids:
        ids = [r['id'] for r in conn.execute(
            "SELECT id FROM users WHERE company_id=? AND (is_company_admin=1 OR role='admin') "
            "AND COALESCE(status,'approved')='approved'", (oid,))]
    return list(dict.fromkeys(int(i) for i in ids))


def on_submission(sub_id, owner, mid, k, src=''):
    """Called by the public submit route. With a valid job-link signature the
    applicant is put on the job's pipeline (or the known person is placed on it)."""
    if not mid or not verify(owner, mid, k):
        return None
    core = _core()
    channel = src if src in CHANNEL_KEYS else ''
    label = dict(CHANNELS).get(channel, '')
    note = 'Applied through the job link' + (' (%s)' % label if label else '')
    conn = get_db()
    try:
        with core.tenant_pin(owner):
            m = conn.execute("SELECT id, role, client, status FROM mandates WHERE id=? AND owner_id=?",
                             (mid, owner)).fetchone()
            sub = conn.execute('SELECT * FROM submissions WHERE id=? AND owner_id=?', (sub_id, owner)).fetchone()
            if not m or not sub or (m['status'] or '') == 'central':
                return None
            try:
                conn.execute('UPDATE submissions SET job_link=1 WHERE id=?', (sub_id,))
            except Exception:
                pass
            ex = core._existing_candidate(conn, owner, sub['email'], sub['phone'])
            if ex and ex.get('id'):
                from modules.matching import place_in_job, load_src
                how, cid = place_in_job(conn, load_src(conn, owner, ex['id']), mid, note)
                cid = cid or ex['id']
                if sub['cv_path']:
                    # their newest CV, on every record of the person that has none yet
                    conn.execute("UPDATE candidates SET cv_path=?, cv_original_name=?, updated_at=? "
                                 "WHERE id=? AND owner_id=?", (sub['cv_path'], sub['cv_original_name'], ts(), cid, owner))
                status = 'existing'
            else:
                cur = conn.execute(
                    'INSERT INTO candidates (owner_id,mandate_id,name,company,designation,experience,ctc_current,'
                    'ctc_expected,notice_period,location,phone,email,key_skills,screening_decision,ai_reasoning,'
                    'stage,cv_path,cv_original_name,source_channel,created_at,updated_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (owner, mid, sub['name'], sub['company'], sub['designation'], sub['experience'],
                     sub['ctc_current'], sub['ctc_expected'], sub['notice_period'], sub['location'],
                     sub['phone'], sub['email'], sub['key_skills'], 'worth_opening', note, 'Screening',
                     sub['cv_path'], sub['cv_original_name'], 'careers', ts(), ts()))
                cid = cur.lastrowid
                conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) '
                             'VALUES (?,?,?,?,?)', (cid, '', 'Screening', note, ts()))
                core._link_to_known_person(conn, cid)
                status = 'added'
            conn.execute("UPDATE submissions SET status='added_to_pipeline' WHERE id=?", (sub_id,))
            conn.commit()
            try:
                from modules import notifications as ntf
                for uid in _recruiters(conn, owner, mid):
                    ntf._create(conn, owner, uid, 'job_apply', 'New application through the job link',
                                ['Job: %s' % (m['role'] or ''), 'Candidate: %s' % (sub['name'] or ''),
                                 'From: %s' % (label or 'job link'), 'CV: %s' % ('attached' if sub['cv_path'] else 'not sent')],
                                candidate_id=cid, mandate_id=mid, actor_name=sub['name'] or '')
                conn.commit()
            except Exception as e:
                print('[job_link] notify failed:', e)
            return {'status': status, 'candidate_id': cid}
    except Exception as e:
        print('[job_link] could not add the application to the job:', e)
        return None
    finally:
        conn.close()


def migrate(conn):
    try:
        conn.execute('ALTER TABLE submissions ADD COLUMN job_link INTEGER DEFAULT 0')
    except Exception:
        pass


try:
    from modules import register_migration
    register_migration(migrate)
except Exception:
    pass
