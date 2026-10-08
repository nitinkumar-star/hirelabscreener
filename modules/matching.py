"""
RecruitOS — Vector matching tabs (Oct 2026)

  GET  /api/match/candidate/<cid>/jobs         Candidate profile -> "Job Matching" tab
  GET  /api/match/mandate/<mid>/candidates     Mandate -> "Matching Candidates" tab
  POST /api/match/mandate/<mid>/add            Add selected people to this job's pipeline

ONLY embeddings are used for ranking: the candidate's stored vector (profile +
resume text, see server.candidate_embed_text) against the mandate's stored JD
vector, cosine similarity — the same retrieval the AI search uses. No keyword
or skill-tag matching anywhere in this module.

A candidate row belongs to ONE mandate. Adding someone who already sits in
another job makes a COPY in this job (owner's decision, Oct 2026): profile,
resume file, work history and vectors are copied; pipeline state (stage,
offers, follow-up timers, WhatsApp state, feedback) is not. Central-database
people are MOVED (the existing pool behaviour). Copies are linked through
candidates.copied_from so a person shows ONCE in every list.

Additive only: new column candidates.copied_from; new routes. Nothing else changes.
"""

import re
import json
from flask import Blueprint, request, jsonify

from modules.shared import get_db, ts, effective_company_id, real_user_id, login_required, _core
from modules import register_migration

bp = Blueprint('matching', __name__, url_prefix='/api/match')

# Profile columns copied into a new pipeline. Everything not listed (stage,
# offers, billing, follow-up/WhatsApp timers, feedback, update tokens …) starts
# fresh in the new job.
PROFILE_COLS = (
    'name', 'company', 'designation', 'experience', 'ctc_current', 'ctc_expected', 'notice_period',
    'location', 'preferred_location', 'phone', 'email', 'qualification', 'specialization',
    'key_skills', 'secondary_skills', 'career_summary', 'industry_background', 'is_mnc',
    'key_skill_tags', 'domain_tags', 'product_handles', 'function_tags',
    'cv_path', 'cv_original_name', 'linkedin_url', 'ai_insight_cv', 'source_channel', 'referrer_name',
    'do_not_email', 'unsub_at', 'sourced_by', 'sourced_at', 'experience_intelligence', 'xp_derived_at',
    # the vector is about the PERSON, so it stays valid in the new job
    'embedding', 'embedding_text', 'embedded_at', 'embedding_model', 'embedding_version',
    'embedding_dimension', 'embedding_status', 'embedding_text_version', 'embedding_vec',
)
DEAD_STAGES = {'not interested', 'not suitable', 'screened-out', 'client rejected on paper',
               'client rejected after interview', 'interview backout'}
MAX_ADD = 200


@register_migration
def _migrate_matching(conn):
    try:
        conn.execute('ALTER TABLE candidates ADD COLUMN copied_from INTEGER DEFAULT 0')
    except Exception:
        pass
    try:
        conn.execute('CREATE INDEX IF NOT EXISTS idx_candidates_copied_from ON candidates(copied_from)')
    except Exception:
        pass
    conn.commit()


# ── helpers ──────────────────────────────────────────────────────────────
def _cid():
    return int(effective_company_id() or 0)


def _err(msg, code=400, **extra):
    return jsonify(dict({'ok': False, 'error': msg}, **extra)), code


def _digits(p):
    d = re.sub(r'\D', '', str(p or ''))
    return d[-10:] if len(d) >= 10 else ''


def _person_key(row):
    """Same person across copies: the root of the copy chain."""
    return int(row['copied_from'] or 0) or int(row['id'])


def _vec(blob):
    core = _core()
    if not blob:
        return None
    try:
        if getattr(core, '_HAS_NUMPY', False):
            return core._np.frombuffer(blob, dtype=core._np.float32)
        import array
        a = array.array('f')
        a.frombytes(blob)
        return a
    except Exception:
        return None


def _pct(sim):
    return round(max(0.0, float(sim)) * 100, 1)


def _where_kind(row, central_mid):
    """Where a person currently sits: pool | active | dead | closed."""
    if int(row['mandate_id'] or 0) == central_mid or (row['m_status'] or '') == 'central':
        return 'pool'
    if (row['m_status'] or '') != 'active':
        return 'closed'
    if (row['stage'] or '').strip().lower() in DEAD_STAGES:
        return 'dead'
    return 'active'


def _people_in_mandate(conn, mid, company_id):
    """Person keys already in this mandate (originals and copies)."""
    keys, phones = set(), set()
    for r in conn.execute('SELECT id, copied_from, phone FROM candidates WHERE mandate_id=? AND owner_id=?',
                          (mid, company_id)):
        keys.add(_person_key(r))
        keys.add(int(r['id']))
        if _digits(r['phone']):
            phones.add(_digits(r['phone']))
    return keys, phones


# ══════════════════════════════════════════════════════════════════════════
#  Candidate -> matching jobs
# ══════════════════════════════════════════════════════════════════════════
@bp.route('/candidate/<int:cid>/jobs', methods=['GET'])
@login_required
def candidate_jobs(cid):
    from modules.access import mandate_scope_sql
    core = _core()
    co = _cid()
    conn = get_db()
    try:
        c = conn.execute('SELECT id, name, phone, copied_from, embedding_vec, embedding_status, embedding_text_version, '
                         'cv_path FROM candidates WHERE id=? AND owner_id=?', (cid, co)).fetchone()
        if not c:
            return _err('Not found', 404)
        cvec = _vec(c['embedding_vec'])
        if cvec is None:
            try:
                core.queue_embedding_job(cid, conn)
            except Exception:
                pass
            return jsonify({'ok': True, 'pending': True, 'results': [],
                            'message': 'This candidate\'s AI profile is being built. Open this tab again in a minute.'})
        # every job this PERSON already sits in (this record + its copies)
        root = _person_key(c)
        mine = {int(r['mandate_id'] or 0) for r in conn.execute(
            'SELECT mandate_id FROM candidates WHERE owner_id=? AND (id=? OR copied_from=? OR id=?)',
            (co, root, root, cid))}
        ms, mp = mandate_scope_sql('m.id')
        rows = conn.execute(
            "SELECT m.*, mv.embedding_vec AS jd_vec FROM mandates m "
            "JOIN mandate_vectors mv ON mv.mandate_id=m.id "
            "WHERE m.owner_id=? AND m.status='active' AND mv.status='completed' AND mv.embedding_vec IS NOT NULL" + ms,
            [co] + mp).fetchall()
        out, no_jd = [], 0
        for r in rows:
            if not _has_jd_content(core, r):
                no_jd += 1
                continue
            jv = _vec(r['jd_vec'])
            if jv is None or len(jv) != len(cvec):
                continue
            sim = core.cosine(list(cvec), list(jv))
            out.append({'mandate_id': r['id'], 'role': r['role'] or '', 'client': r['client'] or '',
                        'location': r['location'] or '', 'score': _pct(sim), 'in_this_job': r['id'] in mine})
        out.sort(key=lambda x: -x['score'])
        no_vec = conn.execute(
            "SELECT COUNT(*) FROM mandates m LEFT JOIN mandate_vectors mv ON mv.mandate_id=m.id AND mv.status='completed' "
            "WHERE m.owner_id=? AND m.status='active' AND mv.mandate_id IS NULL" + ms, [co] + mp).fetchone()[0]
        has_resume = bool(c['cv_path']) and (c['embedding_text_version'] or '').endswith('v2')
        return jsonify({'ok': True, 'candidate_id': cid, 'results': out,
                        'jobs_without_vector': no_vec + no_jd, 'resume_in_vector': has_resume})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  Mandate -> matching candidates
# ══════════════════════════════════════════════════════════════════════════
def _has_jd_content(core, m):
    """A role title alone is not a reliable basis for matching: require the JD
    body, skills or a boolean query (what mandate_jd_text embeds besides names)."""
    keys = m.keys()
    if str(m['jd'] if 'jd' in keys else '').strip():
        return True
    if 'boolean_query' in keys and str(m['boolean_query'] or '').strip():
        return True
    try:
        return bool(core.mandate_skills(m, 'must_have_skills') or core.mandate_skills(m, 'good_to_have_skills'))
    except Exception:
        return False


def _jd_vector(conn, mid):
    """Stored JD vector; if missing, build it now (one embedding call)."""
    core = _core()
    m = conn.execute('SELECT * FROM mandates WHERE id=?', (mid,)).fetchone()
    if not m:
        return None, 'Not found'
    if not _has_jd_content(core, m):
        return None, 'This job has no JD yet. Add the JD (or must-have skills) in Mandate Setup to get matches.'
    v = core._mandate_jd_vector(conn, mid)
    if v is not None:
        return v, None
    key = core.get_setting('embedding_api_key', '')
    if not key:
        return None, 'Embedding API key is not set (Settings).'
    try:
        core.embed_mandate_jd(conn, m, key)
        conn.commit()
    except Exception as e:
        print(f'[matching] JD embed failed for {mid}: {e}')
    v = core._mandate_jd_vector(conn, mid)
    return (v, None) if v is not None else (None, 'Could not build the JD vector right now. Try again in a minute.')


@bp.route('/mandate/<int:mid>/candidates', methods=['GET'])
@login_required
def mandate_candidates(mid):
    core = _core()
    co = _cid()
    try:
        limit = max(1, min(int(request.args.get('limit') or 50), 200))
        min_pct = float(request.args.get('min') or 0)
    except ValueError:
        return _err('limit/min must be numbers')
    conn = get_db()
    try:
        m = conn.execute('SELECT id, role, client, status FROM mandates WHERE id=? AND owner_id=?', (mid, co)).fetchone()
        if not m:
            return _err('Not found', 404)
        jd, msg = _jd_vector(conn, mid)
        if jd is None:
            return jsonify({'ok': True, 'pending': True, 'results': [], 'message': msg})
        # rank EVERY embedded candidate the caller may see (recruiter scope is inside _search_rank)
        pool = 5000
        ranked, scanned = core._search_rank(conn, list(jd), min_pct / 100.0 if min_pct else -1.0,
                                            int(core._search_cfg('search_max_candidates')), pool, co)
        ids = [cid for _s, cid in ranked]
        sim_of = {cid: s for s, cid in ranked}
        in_keys, in_phones = _people_in_mandate(conn, mid, co)
        central_mid = 0
        try:
            central_mid = int(core.get_or_create_central_mandate() or 0)
        except Exception:
            pass
        rows = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ','.join('?' * len(chunk))
            for r in conn.execute(
                    f"SELECT c.id, c.name, c.designation, c.company, c.experience, c.location, c.phone, c.email, "
                    f"c.stage, c.mandate_id, c.copied_from, c.cv_path, c.embedding_text_version, c.ctc_current, "
                    f"c.notice_period, m.role AS m_role, m.client AS m_client, m.status AS m_status "
                    f"FROM candidates c LEFT JOIN mandates m ON m.id=c.mandate_id WHERE c.id IN ({q})", chunk):
                rows[r['id']] = r
        people, order = {}, []
        for cid in ids:                                    # best score first
            r = rows.get(cid)
            if not r:
                continue
            key = _person_key(r)
            ph = _digits(r['phone'])
            if key in in_keys or cid in in_keys or (ph and ph in in_phones):
                continue                                   # already in this job
            gk = ('p', key)
            if gk in people:
                people[gk]['also_in'] += 1
                continue
            if ph and ('ph', ph) in people:                # same phone = same person (old duplicates)
                people[('ph', ph)]['also_in'] += 1
                continue
            kind = _where_kind(r, central_mid)
            item = {
                'id': r['id'], 'name': r['name'] or '', 'designation': r['designation'] or '',
                'company': r['company'] or '', 'experience': r['experience'], 'location': r['location'] or '',
                'phone': r['phone'] or '', 'ctc_current': r['ctc_current'], 'notice_period': r['notice_period'],
                'score': _pct(sim_of[cid]), 'where': kind,
                'current_job': '' if kind == 'pool' else ((r['m_role'] or '') + (' — ' + r['m_client'] if r['m_client'] else '')),
                'current_mandate_id': r['mandate_id'], 'current_stage': r['stage'] or '',
                'resume_in_vector': bool(r['cv_path']) and (r['embedding_text_version'] or '').endswith('v2'),
                'action': 'move' if kind == 'pool' else 'copy', 'also_in': 0,
            }
            people[gk] = item
            if ph:
                people[('ph', ph)] = item
            order.append(item)
            if len(order) >= limit:
                break
        not_embedded = conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE owner_id=? AND (embedding IS NULL OR embedding IN ('', '[]'))",
            (co,)).fetchone()[0]
        return jsonify({'ok': True, 'mandate_id': mid, 'job': (m['role'] or '') + (' — ' + m['client'] if m['client'] else ''),
                        'results': order, 'scanned': scanned, 'not_embedded': not_embedded,
                        'more': len(order) >= limit})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  Add to this job's pipeline (copy, or move from the pool)
# ══════════════════════════════════════════════════════════════════════════
def copy_into_mandate(conn, src, mid, note):
    """Create this person's record in mandate `mid`. Returns the new id."""
    core = _core()
    have = {r[1] for r in conn.execute('PRAGMA table_info(candidates)')}
    cols = [c for c in PROFILE_COLS if c in have]
    vals = [src[c] for c in cols]
    now = ts()
    extra = {'mandate_id': mid, 'stage': 'Screening', 'owner_id': src['owner_id'], 'created_at': now,
             'updated_at': now, 'copied_from': _person_key(src)}
    if 'created_by' in have:
        extra['created_by'] = int(real_user_id() or 0)
    cols += list(extra.keys())
    vals += list(extra.values())
    new_id = conn.execute(f"INSERT INTO candidates ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          vals).lastrowid
    for w in conn.execute('SELECT company, designation, start_date, end_date, is_current, description, sort_order '
                          'FROM work_history WHERE candidate_id=?', (src['id'],)).fetchall():
        conn.execute('INSERT INTO work_history (candidate_id, company, designation, start_date, end_date, is_current, '
                     'description, sort_order) VALUES (?,?,?,?,?,?,?,?)', (new_id,) + tuple(w))
    try:                                                   # facet vectors (if that feature is on)
        fcols = [r[1] for r in conn.execute('PRAGMA table_info(candidate_vectors)') if r[1] not in ('id', 'candidate_id')]
        if fcols:
            conn.execute(f"INSERT INTO candidate_vectors (candidate_id, {','.join(fcols)}) "
                         f"SELECT ?, {','.join(fcols)} FROM candidate_vectors WHERE candidate_id=?", (new_id, src['id']))
    except Exception:
        pass
    conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) VALUES (?,?,?,?,?)',
                 (new_id, '', 'Screening', note, now))
    if not src['embedding_vec']:
        try:
            core.queue_embedding_job(new_id, conn)
        except Exception:
            pass
    return new_id


@bp.route('/mandate/<int:mid>/add', methods=['POST'])
@login_required
def mandate_add(mid):
    from modules.access import can_see_mandate, can_see_candidate
    core = _core()
    co = _cid()
    d = request.get_json(silent=True) or {}
    ids = d.get('candidate_ids') or []
    scores = d.get('scores') or {}
    if not isinstance(ids, list) or not ids:
        return _err('candidate_ids required')
    conn = get_db()
    try:
        m = conn.execute("SELECT id, role, client, status FROM mandates WHERE id=? AND owner_id=?", (mid, co)).fetchone()
        if not m or (m['status'] or '') == 'central':
            return _err('Not found', 404)
        if not can_see_mandate(conn, mid, write=True):
            return _err('Not found', 404)
        central_mid = 0
        try:
            central_mid = int(core.get_or_create_central_mandate() or 0)
        except Exception:
            pass
        in_keys, in_phones = _people_in_mandate(conn, mid, co)
        job = (m['role'] or '') + (' (' + m['client'] + ')' if m['client'] else '')
        results = []
        for raw in ids[:MAX_ADD]:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            src = conn.execute('SELECT c.*, m.status AS m_status, m.role AS m_role, m.client AS m_client FROM candidates c LEFT JOIN mandates m '
                               'ON m.id=c.mandate_id WHERE c.id=? AND c.owner_id=?', (cid, co)).fetchone()
            if not src or not can_see_candidate(conn, cid):
                results.append({'id': cid, 'status': 'not_found'})
                continue
            ph = _digits(src['phone'])
            if _person_key(src) in in_keys or cid in in_keys or (ph and ph in in_phones):
                results.append({'id': cid, 'status': 'already_in_job'})
                continue
            sc = scores.get(str(cid))
            note = 'Added from Matching Candidates' + (f' ({sc}% match)' if sc not in (None, '') else '')
            if int(src['mandate_id'] or 0) == central_mid or (src['m_status'] or '') == 'central':
                conn.execute('UPDATE candidates SET mandate_id=?, stage=?, updated_at=? WHERE id=?',
                             (mid, 'Screening', ts(), cid))
                conn.execute('INSERT INTO stage_history (candidate_id,from_stage,to_stage,note,created_at) '
                             'VALUES (?,?,?,?,?)', (cid, src['stage'] or '', 'Screening',
                                                    note + ' — picked from Central Database', ts()))
                new_id, how = cid, 'moved'
            else:
                frm = (src['m_role'] or 'another job') + (' (' + src['m_client'] + ')' if src['m_client'] else '')
                new_id = copy_into_mandate(conn, src, mid, note + ' — copied from ' + frm + ', stage there: ' + (src['stage'] or '-'))
                how = 'copied'
            in_keys.add(_person_key(src))
            if ph:
                in_phones.add(ph)
            conn.commit()
            try:
                core.log_candidate_event(new_id, 'note', f'{note}: {job}')
            except Exception:
                pass
            results.append({'id': cid, 'status': how, 'new_id': new_id})
        added = sum(1 for r in results if r['status'] in ('copied', 'moved'))
        return jsonify({'ok': True, 'added': added, 'results': results})
    finally:
        conn.close()
