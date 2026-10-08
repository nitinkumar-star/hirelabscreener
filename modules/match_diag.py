"""
RecruitOS — Matching diagnosis (admin, READ-ONLY)

Built before changing the matching logic, so the decision is made on the
company's real data instead of assumptions. Nothing here writes to the
database. Admins only.

  GET  /api/match/diag/overview              coverage of the AI vectors
  GET  /api/match/diag/job/<mid>             how candidates' scores spread for one job
  POST /api/match/diag/job/<mid>/compare     same job's top candidates scored three ways:
         (a) today's vectors (cosine, as the Matching tabs use now)
         (b) the same texts embedded in Jina v3 retrieval mode
             (JD = retrieval.query, profile = retrieval.passage, name removed)
         (c) Jina's re-ranker reading JD + profile together
       (b) and (c) are computed on the fly for <= 40 candidates; nothing is stored.
"""

import json
import random
import statistics
from flask import Blueprint, request, jsonify

from modules.shared import get_db, effective_company_id, is_company_admin, login_required, _core

bp = Blueprint('match_diag', __name__, url_prefix='/api/match/diag')

RERANK_MODEL = 'jina-reranker-v2-base-multilingual'
COMPARE_TOP, COMPARE_SAMPLE = 30, 10
DOC_CHARS = 4000


def _deny():
    from modules.access import scoped_user
    if scoped_user() or not is_company_admin():
        return jsonify({'ok': False, 'error': 'Only an admin can run the matching diagnosis.'}), 403
    return None


def _np():
    core = _core()
    return core._np if getattr(core, '_HAS_NUMPY', False) else None


def _vec(blob):
    np = _np()
    if not blob or np is None:
        return None
    try:
        return np.frombuffer(blob, dtype=np.float32)
    except Exception:
        return None


def _pcts(vals):
    if not vals:
        return {}
    s = sorted(vals)

    def p(q):
        k = (len(s) - 1) * q
        f = int(k)
        c = min(f + 1, len(s) - 1)
        return round((s[f] + (s[c] - s[f]) * (k - f)) * 100, 1)
    return {'min': p(0), 'p10': p(0.10), 'p25': p(0.25), 'median': p(0.5), 'p75': p(0.75),
            'p90': p(0.90), 'max': p(1.0), 'count': len(s)}


# ══════════════════════════════════════════════════════════════════════════
@bp.route('/overview', methods=['GET'])
@login_required
def overview():
    deny = _deny()
    if deny:
        return deny
    co = int(effective_company_id() or 0)
    conn = get_db()
    try:
        q = lambda sql, *a: conn.execute(sql, (co,) + a).fetchone()[0]
        total = q('SELECT COUNT(*) FROM candidates WHERE owner_id=?')
        with_vec = q("SELECT COUNT(*) FROM candidates WHERE owner_id=? AND ((embedding_vec IS NOT NULL AND length(embedding_vec)>0) "
                     "OR (embedding IS NOT NULL AND embedding NOT IN ('', '[]')))")
        json_only = q("SELECT COUNT(*) FROM candidates WHERE owner_id=? AND (embedding_vec IS NULL OR length(embedding_vec)=0) "
                      "AND embedding IS NOT NULL AND embedding NOT IN ('', '[]')")
        with_cv = q("SELECT COUNT(*) FROM candidates WHERE owner_id=? AND COALESCE(cv_path,'')!=''")
        resume_in = q("SELECT COUNT(*) FROM candidates WHERE owner_id=? AND embedding_text LIKE '%Resume:%'")
        cv_not_in = q("SELECT COUNT(*) FROM candidates WHERE owner_id=? AND COALESCE(cv_path,'')!='' "
                      "AND embedding_vec IS NOT NULL AND COALESCE(embedding_text,'') NOT LIKE '%Resume:%'")
        by_model = [dict(model=r[0] or '(unknown)', dim=r[1], n=r[2]) for r in conn.execute(
            "SELECT embedding_model, length(embedding_vec)/4 d, COUNT(*) FROM candidates WHERE owner_id=? "
            "AND embedding_vec IS NOT NULL AND length(embedding_vec)>0 GROUP BY embedding_model, d ORDER BY 3 DESC", (co,))]
        by_text = [dict(version=r[0] or '(none)', n=r[1]) for r in conn.execute(
            "SELECT embedding_text_version, COUNT(*) FROM candidates WHERE owner_id=? AND embedding_vec IS NOT NULL "
            "GROUP BY embedding_text_version ORDER BY 2 DESC", (co,))]
        failed = q("SELECT COUNT(*) FROM candidates WHERE owner_id=? AND embedding_status='failed'")
        act = q("SELECT COUNT(*) FROM mandates WHERE owner_id=? AND status='active'")
        jd_vec = [dict(model=r[0] or '(unknown)', dim=r[1], n=r[2]) for r in conn.execute(
            "SELECT mv.embedding_model, length(mv.embedding_vec)/4 d, COUNT(*) FROM mandate_vectors mv JOIN mandates m "
            "ON m.id=mv.mandate_id WHERE m.owner_id=? AND m.status='active' AND mv.status='completed' GROUP BY 1, 2", (co,))]
        jd_empty = q("SELECT COUNT(*) FROM mandates WHERE owner_id=? AND status='active' AND COALESCE(TRIM(jd),'')='' "
                     "AND COALESCE(TRIM(must_have_skills),'') IN ('','[]') AND COALESCE(TRIM(boolean_query),'')=''")
        ctc = {
            'jobs_with_budget': q("SELECT COUNT(*) FROM mandates WHERE owner_id=? AND status='active' AND COALESCE(ctc_max,0)>0"),
            'active_jobs': act,
            'candidates_with_expected_ctc': q("SELECT COUNT(*) FROM candidates WHERE owner_id=? AND COALESCE(ctc_expected,0)>0"),
            'candidates_with_current_ctc': q("SELECT COUNT(*) FROM candidates WHERE owner_id=? AND COALESCE(ctc_current,0)>0"),
        }
        dims = {x['dim'] for x in by_model} | {x['dim'] for x in jd_vec}
        core = _core()
        return jsonify({'ok': True, 'candidates': {
            'total': total, 'with_vector': with_vec, 'without_vector': total - with_vec, 'failed': failed,
            'vector_only_in_old_json_form': json_only, 'with_cv': with_cv, 'resume_text_in_vector': resume_in, 'cv_attached_but_not_in_vector': cv_not_in,
            'by_model': by_model, 'by_text_version': by_text},
            'jobs': {'active': act, 'jd_vectors': jd_vec, 'active_without_jd': jd_empty},
            'mixed_dimensions': len(dims) > 1, 'ctc_fields': ctc,
            'embedding_call': {'base_url': core.get_setting('embedding_base_url', '') or 'https://api.jina.ai/v1',
                               'model': core.get_setting('embedding_model', '') or 'jina-embeddings-v3',
                               'task_mode_used': False}})
    finally:
        conn.close()


def _rank_all(conn, co, jd):
    """Cosine of EVERY embedded candidate vs the JD vector (exactly what the tab does)."""
    np = _np()
    rows = conn.execute("SELECT id, embedding_vec, embedding FROM candidates WHERE owner_id=? AND "
                        "((embedding_vec IS NOT NULL AND length(embedding_vec)>0) OR "
                        "(embedding IS NOT NULL AND embedding NOT IN ('', '[]')))", (co,)).fetchall()
    ids, mats, skipped = [], [], 0
    for r in rows:
        v = _vec(r['embedding_vec'])
        if v is None:                                # old rows: vector only stored as JSON
            try:
                v = np.asarray(json.loads(r['embedding']), dtype=np.float32)
            except Exception:
                v = None
        if v is None or len(v) != len(jd):
            skipped += 1
            continue
        ids.append(r['id']); mats.append(v)
    if not ids:
        return [], skipped
    m = np.asarray(mats, dtype=np.float32)
    q = np.asarray(jd, dtype=np.float32)
    sims = (m @ q) / (np.linalg.norm(m, axis=1).clip(1e-9) * (np.linalg.norm(q) or 1e-9))
    return sorted(zip(sims.tolist(), ids), reverse=True), skipped


def _cand_rows(conn, ids):
    out = {}
    for i in range(0, len(ids), 500):
        ch = ids[i:i + 500]
        for r in conn.execute(f"SELECT c.*, m.role AS m_role, m.client AS m_client FROM candidates c "
                              f"LEFT JOIN mandates m ON m.id=c.mandate_id WHERE c.id IN ({','.join('?' * len(ch))})", ch):
            out[r['id']] = r
    return out


def _brief(r, score=None):
    return {'id': r['id'], 'name': r['name'] or '', 'designation': r['designation'] or '', 'company': r['company'] or '',
            'experience': r['experience'], 'ctc_expected': r['ctc_expected'], 'ctc_current': r['ctc_current'],
            'in_job': ((r['m_role'] or '') + (' — ' + r['m_client'] if r['m_client'] else '')),
            'stage': r['stage'] or '', 'score': round(score * 100, 1) if score is not None else None}


def _job(conn, co, mid):
    m = conn.execute('SELECT * FROM mandates WHERE id=? AND owner_id=?', (mid, co)).fetchone()
    if not m:
        return None, None, 'Not found'
    jd = _core()._mandate_jd_vector(conn, mid)
    if jd is None:
        return m, None, 'This job has no JD vector yet.'
    return m, list(jd), None


@bp.route('/job/<int:mid>', methods=['GET'])
@login_required
def job(mid):
    deny = _deny()
    if deny:
        return deny
    co = int(effective_company_id() or 0)
    conn = get_db()
    try:
        m, jd, err = _job(conn, co, mid)
        if not m:
            return jsonify({'ok': False, 'error': err}), 404
        if jd is None:
            return jsonify({'ok': True, 'pending': True, 'message': err})
        ranked, skipped = _rank_all(conn, co, jd)
        vals = [s for s, _ in ranked]
        hist = [0] * 10
        for v in vals:
            hist[max(0, min(9, int(v * 10)))] += 1
        rows = _cand_rows(conn, [c for _, c in ranked[:15]] + [c for _, c in ranked[-5:]])
        mv = conn.execute('SELECT embedding_model, embedding_text, embedding_text_version FROM mandate_vectors '
                          'WHERE mandate_id=?', (mid,)).fetchone()
        spread = (_pcts(vals).get('p90', 0) - _pcts(vals).get('p10', 0)) if vals else 0
        return jsonify({'ok': True, 'job': {'id': mid, 'role': m['role'], 'client': m['client'],
                                            'exp_min': m['exp_min'], 'exp_max': m['exp_max'],
                                            'ctc_min': m['ctc_min'], 'ctc_max': m['ctc_max']},
                        'jd_text_embedded': ((mv['embedding_text'] if mv else '') or '')[:1200],
                        'jd_vector_model': (mv['embedding_model'] if mv else ''),
                        'distribution': _pcts(vals), 'spread_p10_p90': round(spread, 1),
                        'histogram': [{'range': f'{i * 10}-{i * 10 + 10}%', 'n': n} for i, n in enumerate(hist)],
                        'above': {str(t): sum(1 for v in vals if v * 100 >= t) for t in (40, 50, 60, 70, 80)},
                        'skipped_dimension_mismatch': skipped,
                        'top15': [_brief(rows[c], s) for s, c in ranked[:15] if c in rows],
                        'bottom5': [_brief(rows[c], s) for s, c in ranked[-5:] if c in rows]})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  Side-by-side comparison on live data (Jina retrieval mode + re-ranker)
# ══════════════════════════════════════════════════════════════════════════
def _profile_text(core, conn, r):
    """The candidate's embedding text without the name line (a name says nothing about fit)."""
    t = core.candidate_embed_text(r, conn) or ''
    lines = t.split('\n')
    if lines and lines[0].strip() == (r['name'] or '').strip():
        lines = lines[1:]
    return '\n'.join(lines)[:DOC_CHARS]


def _jina(path, body):
    import requests
    core = _core()
    key = core.get_setting('embedding_api_key', '')
    base = (core.get_setting('embedding_base_url', '') or 'https://api.jina.ai/v1').rstrip('/')
    r = requests.post(base + path, headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
                      json=body, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f'{path}: HTTP {r.status_code} {r.text[:200]}')
    return r.json()


def _cos(a, b):
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(y * y for y in b)) or 1e-9
    return dot / (na * nb)


@bp.route('/job/<int:mid>/compare', methods=['POST'])
@login_required
def compare(mid):
    deny = _deny()
    if deny:
        return deny
    core = _core()
    co = int(effective_company_id() or 0)
    if not core.get_setting('embedding_api_key', ''):
        return jsonify({'ok': False, 'error': 'Embedding API key is not set.'}), 400
    base = (core.get_setting('embedding_base_url', '') or 'https://api.jina.ai/v1')
    if 'jina.ai' not in base:
        return jsonify({'ok': False, 'error': 'The comparison needs Jina (current provider: %s).' % base}), 400
    conn = get_db()
    try:
        m, jd, err = _job(conn, co, mid)
        if not m:
            return jsonify({'ok': False, 'error': err}), 404
        if jd is None:
            return jsonify({'ok': False, 'error': err}), 400
        ranked, _sk = _rank_all(conn, co, jd)
        if not ranked:
            return jsonify({'ok': False, 'error': 'No embedded candidates.'}), 400
        top = ranked[:COMPARE_TOP]
        rest = ranked[COMPARE_TOP:]
        rnd = random.Random(mid)                     # same sample every run for this job
        sample = rnd.sample(rest, min(COMPARE_SAMPLE, len(rest))) if rest else []
        picked = top + sample
        rows = _cand_rows(conn, [c for _, c in picked])
        jd_text = core.mandate_jd_text(m)
        docs, items = [], []
        for s, c in picked:
            r = rows.get(c)
            if not r:
                continue
            items.append(dict(_brief(r, s), from_top=(s, c) in top))
            docs.append(_profile_text(core, conn, r))
        if not items:
            return jsonify({'ok': False, 'error': 'No candidates to compare.'}), 400
        model = core.get_setting('embedding_model', '') or 'jina-embeddings-v3'
        try:
            qv = _jina('/embeddings', {'model': model, 'task': 'retrieval.query', 'input': [jd_text[:6000]]})['data'][0]['embedding']
            pv = [d['embedding'] for d in _jina('/embeddings', {'model': model, 'task': 'retrieval.passage', 'input': docs})['data']]
            rr = _jina('/rerank', {'model': RERANK_MODEL, 'query': jd_text[:6000], 'documents': docs,
                                   'top_n': len(docs), 'return_documents': False})
        except Exception as e:
            return jsonify({'ok': False, 'error': 'Jina call failed: ' + str(e)[:300]}), 502
        rel = {x['index']: x['relevance_score'] for x in rr.get('results', [])}
        for i, it in enumerate(items):
            it['task_mode'] = round(_cos(qv, pv[i]) * 100, 1)
            it['rerank'] = round(float(rel.get(i, 0.0)) * 100, 1)
        for key in ('score', 'task_mode', 'rerank'):
            for pos, it in enumerate(sorted(items, key=lambda x: -(x[key] or 0))):
                it[key + '_rank'] = pos + 1
        items.sort(key=lambda x: -x['rerank'])
        spread = lambda k: round(max(i[k] for i in items) - min(i[k] for i in items), 1)
        return jsonify({'ok': True, 'job': {'id': mid, 'role': m['role'], 'client': m['client'],
                                            'ctc_min': m['ctc_min'], 'ctc_max': m['ctc_max']},
                        'items': items, 'rerank_model': RERANK_MODEL,
                        'spread': {'current': spread('score'), 'task_mode': spread('task_mode'), 'rerank': spread('rerank')},
                        'note': 'Top %d by today\'s vectors plus %d random others (marked). Nothing was saved.'
                                % (len(top), len(sample))})
    finally:
        conn.close()
