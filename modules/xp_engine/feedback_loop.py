"""
feedback_loop.py — HireLab learning layer + sourcing payoff (ADDITIVE, data-safe).

WHAT THIS DOES
--------------
1. LEARNS your domain over time: every recruiter correction to a parsed skill grows a
   `domain_lexicon` table, so the experience engine recognises more of YOUR terms next time.
   (This is "the parser gets smarter" — realised as a growing data asset, not model training.)
2. STORES derived intelligence on each candidate in a NEW json column (experience_intelligence).
3. FEEDS sourcing: ranks your existing pool for a mandate using the derived features + the
   outcome signal already sitting in your data (who historically advanced).

DATA SAFETY (your 570 candidates are safe)
------------------------------------------
- Every schema change is additive & idempotent (ADD COLUMN / CREATE TABLE IF NOT EXISTS,
  wrapped like your existing migrations). Nothing is dropped.
- recompute_* only UPDATEs the two NEW columns on a candidate. It never deletes a row and
  never touches name/phone/skills/stage/CV or any existing column.
"""

from __future__ import annotations
import json, datetime
from . import experience_engine as xe


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA (additive, idempotent — mirrors your existing migration style)
# ─────────────────────────────────────────────────────────────────────────────

def ensure_schema(conn):
    """Call once at startup. Safe to run every boot; does nothing if already applied."""
    # New columns on candidates (additive)
    for col, typ in [('experience_intelligence', 'TEXT'), ('xp_derived_at', 'TEXT')]:
        try:
            conn.execute(f'ALTER TABLE candidates ADD COLUMN {col} {typ} DEFAULT ""')
        except Exception:
            pass  # already exists

    # Growable domain dictionary
    conn.execute('''CREATE TABLE IF NOT EXISTS domain_lexicon (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        term TEXT NOT NULL UNIQUE,
        canonical TEXT NOT NULL,
        category TEXT DEFAULT 'skill',
        domain TEXT DEFAULT 'unknown',
        source TEXT DEFAULT 'seed',
        weight INTEGER DEFAULT 1,
        created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT ''
    )''')

    # Every recruiter correction to a parsed field (the raw learning signal + audit trail)
    conn.execute('''CREATE TABLE IF NOT EXISTS parse_corrections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER,
        field TEXT DEFAULT '',
        old_value TEXT DEFAULT '',
        new_value TEXT DEFAULT '',
        corrected_by TEXT DEFAULT '',
        created_at TEXT DEFAULT ''
    )''')
    conn.commit()

    # Seed the lexicon table from the engine's base dictionary ONCE (only if empty)
    n = conn.execute('SELECT COUNT(*) AS n FROM domain_lexicon').fetchone()['n']
    if n == 0:
        now = _utcnow()
        for term, (canon, cat, dom) in xe.BASE_LEXICON.items():
            try:
                conn.execute('INSERT INTO domain_lexicon (term,canonical,category,domain,source,created_at) '
                             'VALUES (?,?,?,?,?,?)', (term, canon, cat, dom, 'seed', now))
            except Exception:
                pass
        conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# LEXICON (load merged base+learned; grow it from corrections)
# ─────────────────────────────────────────────────────────────────────────────

def load_lexicon(conn) -> dict:
    """Return {term: (canonical, category, domain)} = seeded base + everything learned."""
    lex = dict(xe.BASE_LEXICON)  # start from code base (in case table not seeded yet)
    try:
        for r in conn.execute('SELECT term,canonical,category,domain FROM domain_lexicon'):
            lex[(r['term'] or '').strip().lower()] = (r['canonical'], r['category'], r['domain'])
    except Exception:
        pass
    return lex


def learn_term(conn, term, canonical=None, category='skill', domain='unknown', source='correction', user=''):
    """Add/strengthen a lexicon term. This is how the system 'learns' your domain vocabulary."""
    term_l = (term or '').strip().lower()
    if not term_l:
        return
    canonical = (canonical or term).strip()
    now = _utcnow()
    existing = conn.execute('SELECT id,weight FROM domain_lexicon WHERE term=?', (term_l,)).fetchone()
    if existing:
        conn.execute('UPDATE domain_lexicon SET weight=weight+1, canonical=?, category=?, domain=? WHERE id=?',
                     (canonical, category, domain, existing['id']))
    else:
        conn.execute('INSERT INTO domain_lexicon (term,canonical,category,domain,source,created_by,created_at) '
                     'VALUES (?,?,?,?,?,?,?)', (term_l, canonical, category, domain, source, user, now))
    conn.commit()


def record_correction(conn, candidate_id, field, old_value, new_value, user='', *,
                      learn_as_skill=False, category='skill', domain='unknown'):
    """
    Save a recruiter's correction to a parsed field (audit + learning signal).
    If learn_as_skill=True (recruiter fixed/added a skill), also teach it to the lexicon
    so the next parse recognises it -> the parser 'gets smarter' at your domain.
    """
    now = _utcnow()
    conn.execute('INSERT INTO parse_corrections (candidate_id,field,old_value,new_value,corrected_by,created_at) '
                 'VALUES (?,?,?,?,?,?)',
                 (candidate_id, field, str(old_value or ''), str(new_value or ''), user, now))
    conn.commit()
    if learn_as_skill and new_value:
        learn_term(conn, new_value, new_value, category, domain, source='correction', user=user)


# ─────────────────────────────────────────────────────────────────────────────
# RECOMPUTE (derive intelligence, store in the NEW column only — never touches others)
# ─────────────────────────────────────────────────────────────────────────────

def _work_history(conn, candidate_id):
    rows = conn.execute('SELECT company,designation,start_date,end_date,is_current,description '
                        'FROM work_history WHERE candidate_id=? ORDER BY is_current DESC, sort_order ASC, id ASC',
                        (candidate_id,)).fetchall()
    return [dict(r) for r in rows]


def _candidate_skills(conn, candidate_id):
    c = conn.execute('SELECT key_skills,secondary_skills FROM candidates WHERE id=?', (candidate_id,)).fetchone()
    if not c:
        return []
    out = []
    for col in ('key_skills', 'secondary_skills'):
        try:
            arr = json.loads(c[col] or '[]')
            if isinstance(arr, list):
                out += [str(x) for x in arr]
        except Exception:
            pass
    return out


def recompute_candidate(conn, candidate_id, lexicon=None, commit=True):
    """Derive intelligence for ONE candidate and store it in the new column. ADDITIVE."""
    lex = lexicon or load_lexicon(conn)
    wh = _work_history(conn, candidate_id)
    skills = _candidate_skills(conn, candidate_id)
    derived = xe.derive_experience(wh, skills, lex)
    now = _utcnow()
    # ONLY the two new columns are written. WHERE id=? -> exactly one row. No delete, ever.
    conn.execute('UPDATE candidates SET experience_intelligence=?, xp_derived_at=? WHERE id=?',
                 (json.dumps(derived), now, candidate_id))
    if commit:
        conn.commit()
    return derived


def recompute_all(conn, limit=None):
    """Backfill every candidate (safe for all 570). Returns how many were computed."""
    lex = load_lexicon(conn)
    ids = [r['id'] for r in conn.execute('SELECT id FROM candidates ORDER BY id').fetchall()]
    if limit:
        ids = ids[:limit]
    n = 0
    for cid in ids:
        try:
            recompute_candidate(conn, cid, lexicon=lex, commit=False)
            n += 1
        except Exception:
            pass
    conn.commit()
    return n


# ─────────────────────────────────────────────────────────────────────────────
# SOURCING PAYOFF (rank the existing pool for a mandate — features + outcome moat)
# ─────────────────────────────────────────────────────────────────────────────

def outcome_signal(conn):
    """
    READ-ONLY over your existing data. Returns, per domain, how strong an 'advanced/placed'
    history exists — the moat signal that makes ranking smarter over time.
    'Advanced' = moved past Screening OR positive screening_decision.
    """
    stats = {}
    rows = conn.execute("SELECT id, experience_intelligence, stage, screening_decision FROM candidates").fetchall()
    for r in rows:
        try:
            xi = json.loads(r['experience_intelligence'] or '{}')
        except Exception:
            xi = {}
        dom = (xi.get('relevant') or {}).get('domain')
        if not dom:
            continue
        advanced = (r['stage'] and r['stage'] not in ('Screening', 'Not Interested')) or \
                   (str(r['screening_decision'] or '').lower() in ('shortlisted', 'selected', 'yes', 'proceed'))
        s = stats.setdefault(dom, {'total': 0, 'advanced': 0})
        s['total'] += 1
        if advanced:
            s['advanced'] += 1
    for dom, s in stats.items():
        s['advance_rate'] = round(s['advanced'] / s['total'], 2) if s['total'] else 0.0
    return stats


def rank_pool_for_mandate(conn, required_skills=None, required_domains=None, min_overall_years=0, top=25):
    """
    Deterministic sourcing: score every candidate for a mandate using their DERIVED features.
    Score = derived years on each required skill (text-matched weighted higher) +
            derived years in required domains + seniority bonus + outcome-history bonus.
    Returns a ranked list of {candidate_id, name, score, why}. No AI, fully explainable.
    """
    required_skills = [s.lower() for s in (required_skills or [])]
    required_domains = [d.lower() for d in (required_domains or [])]
    outc = outcome_signal(conn)
    SEN_BONUS = {'entry': 0, 'junior': 1, 'mid_level': 2, 'senior': 3, 'lead': 4,
                 'principal': 5, 'manager': 5, 'director': 6, 'executive': 7}
    ranked = []
    for r in conn.execute("SELECT id,name,experience_intelligence FROM candidates").fetchall():
        try:
            xi = json.loads(r['experience_intelligence'] or '{}')
        except Exception:
            continue
        if not xi:
            continue
        if (xi.get('overall') or {}).get('years', 0) < min_overall_years:
            continue
        score = 0.0
        why = []
        skl = xi.get('skills') or {}
        skl_lower = {k.lower(): v for k, v in skl.items()}
        for req in required_skills:
            v = skl_lower.get(req)
            if v:
                pts = v['years'] * (1.0 if v.get('method') == 'text_match' else 0.6)
                score += pts
                why.append(f"{v.get('category','skill')} {req}:{v['years']}y")
        doms = xi.get('domains') or {}
        for req in required_domains:
            v = doms.get(req)
            if v:
                score += v['years'] * 0.8
                why.append(f"domain {req}:{v['years']}y")
        score += SEN_BONUS.get(xi.get('seniority', 'entry'), 0) * 0.5
        rel = (xi.get('relevant') or {}).get('domain')
        if rel and rel in outc:
            score += outc[rel]['advance_rate'] * 2  # moat: profiles like this historically advanced
        if score > 0:
            ranked.append({'candidate_id': r['id'], 'name': r['name'],
                           'score': round(score, 2), 'why': why[:6],
                           'seniority': xi.get('seniority'), 'archetype': xi.get('archetype')})
    ranked.sort(key=lambda x: x['score'], reverse=True)
    return ranked[:top]
