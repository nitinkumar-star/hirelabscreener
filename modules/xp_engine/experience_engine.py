"""
experience_engine.py — HireLab deterministic experience intelligence (Python port of the RIE core).

WHAT THIS IS
------------
Pure-Python, standard-library only. NO AI, NO network, NO external services.
Given a candidate's dated work history (your `work_history` rows) + their skills, it DERIVES:
  - real years overall (dates merged, no double counting, gaps excluded)
  - real years PER SKILL and PER DOMAIN (solar / electrical / automation ...)
  - leadership years
  - seniority band + career archetype
...each with provenance (which companies contributed, how it was calculated).

WHY DETERMINISTIC
-----------------
An LLM *guesses* "8 years". This *calculates* it from dates. Same input -> same output,
auditable, free per run. The LLM stays your text extractor; this does the arithmetic.

FEEDING THE LEARNING LOOP
-------------------------
`derive_experience(..., lexicon=...)` takes a lexicon (skill/domain dictionary). Pass the
BASE_LEXICON for defaults, OR a merged lexicon grown by recruiter corrections (feedback_loop.py).
That is how the system "learns your domain" over time without any model training.
"""

from __future__ import annotations
import re
import datetime
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# 1) DATE PARSING  (tolerant of the messy free-text Naukri stores in work_history)
# ─────────────────────────────────────────────────────────────────────────────

_MONTHS = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9, 'oct': 10,
    'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}
_CURRENT_WORDS = ('present', 'current', 'till date', 'till now', 'now', 'ongoing', 'date')


def _clean(s) -> str:
    return (s or '').strip().lower()


def parse_month_index(s) -> Optional[int]:
    """Parse a free-text date into an absolute month index = year*12 + (month-1).
    Handles: 'Jan 2019', 'January 2019', '2019', '2019-01', '01/2019', '1-2019'.
    Returns None if no year can be found (unparseable)."""
    t = _clean(s)
    if not t:
        return None
    # 'Mon YYYY' or 'Month YYYY'
    m = re.search(r'([a-z]+)[\s\-\.]*(\d{4})', t)
    if m and m.group(1)[:3] in _MONTHS:
        mon = _MONTHS.get(m.group(1)) or _MONTHS.get(m.group(1)[:3])
        if mon:
            return int(m.group(2)) * 12 + (mon - 1)
    # 'YYYY-MM' or 'YYYY/MM'
    m = re.search(r'(\d{4})[\-/](\d{1,2})', t)
    if m:
        mon = int(m.group(2))
        if 1 <= mon <= 12:
            return int(m.group(1)) * 12 + (mon - 1)
    # 'MM-YYYY' or 'MM/YYYY'
    m = re.search(r'(\d{1,2})[\-/](\d{4})', t)
    if m:
        mon = int(m.group(1))
        if 1 <= mon <= 12:
            return int(m.group(2)) * 12 + (mon - 1)
    # bare year 'YYYY'
    m = re.search(r'(19|20)\d{2}', t)
    if m:
        return int(m.group(0)) * 12  # January of that year
    return None


def is_current_text(s) -> bool:
    t = _clean(s)
    return any(w in t for w in _CURRENT_WORDS)


def now_month_index(now: Optional[datetime.date] = None) -> int:
    d = now or datetime.date.today()
    return d.year * 12 + (d.month - 1)


def _start_index(raw) -> Optional[int]:
    return parse_month_index(raw)


def _end_exclusive(raw, is_current: bool, now_m: int) -> Optional[int]:
    """Inclusive-end (LinkedIn-style): 'Jan 2019'..'Dec 2019' counts all 12 months.
    year-only end -> through December; current/ongoing -> now+1."""
    if is_current or is_current_text(raw):
        return now_m + 1
    t = _clean(raw)
    if not t:
        return None
    # If the raw end has an explicit month -> that month is inclusive -> +1
    has_month = bool(re.search(r'[a-z]{3}', t)) or bool(re.search(r'\d{4}[\-/]\d{1,2}', t)) or bool(re.search(r'\d{1,2}[\-/]\d{4}', t))
    idx = parse_month_index(raw)
    if idx is None:
        return None
    if has_month:
        return idx + 1
    # bare year end -> through December of that year
    return idx + 12


def months_to_years(months: int) -> float:
    return round(months / 12.0, 1)


def index_to_iso(idx: int) -> str:
    y, m = idx // 12, (idx % 12) + 1
    return f"{y}-{m:02d}"


# ─────────────────────────────────────────────────────────────────────────────
# 2) INTERVAL MERGE  (the heart: overlapping periods counted ONCE, gaps excluded)
# ─────────────────────────────────────────────────────────────────────────────

def merge_intervals(intervals):
    """intervals: list of (start, end_exclusive). Returns (total_months, min_start, max_end)."""
    clean = [(s, e) for (s, e) in intervals if s is not None and e is not None and e > s]
    if not clean:
        return 0, None, None
    clean.sort()
    total = 0
    cur_s, cur_e = clean[0]
    lo, hi = clean[0][0], clean[0][1]
    for s, e in clean[1:]:
        hi = max(hi, e)
        if s > cur_e:                 # gap -> close previous block
            total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:                          # overlap/adjacent -> extend
            cur_e = max(cur_e, e)
    total += cur_e - cur_s
    return total, lo, hi


# ─────────────────────────────────────────────────────────────────────────────
# 3) DOMAIN LEXICON  (HireLab's solar / electrical / automation knowledge — GROWABLE)
#    feedback_loop.py loads this + learned terms from the DB and passes a merged copy.
# ─────────────────────────────────────────────────────────────────────────────

# term (lowercased)  ->  (canonical, category, domain)
BASE_LEXICON = {
    # automation / controls
    'scada': ('SCADA', 'tool', 'automation'),
    'plc': ('PLC', 'tool', 'automation'),
    'dcs': ('DCS', 'tool', 'automation'),
    'hmi': ('HMI', 'tool', 'automation'),
    'tia portal': ('TIA Portal', 'tool', 'automation'),
    'wincc': ('WinCC', 'tool', 'automation'),
    'modbus': ('Modbus', 'protocol', 'automation'),
    'profibus': ('Profibus', 'protocol', 'automation'),
    'profinet': ('Profinet', 'protocol', 'automation'),
    'siemens s7': ('Siemens S7', 'product', 'automation'),
    's7-1200': ('S7-1200', 'product', 'automation'),
    's7-1500': ('S7-1500', 'product', 'automation'),
    'allen bradley': ('Allen Bradley', 'product', 'automation'),
    'rockwell': ('Rockwell', 'product', 'automation'),
    'vfd': ('VFD', 'product', 'automation'),
    'plc programming': ('PLC Programming', 'skill', 'automation'),
    'instrumentation': ('Instrumentation', 'skill', 'instrumentation'),
    # electrical
    'eplan': ('EPLAN', 'tool', 'electrical'),
    'etap': ('ETAP', 'tool', 'electrical'),
    'autocad electrical': ('AutoCAD Electrical', 'tool', 'electrical'),
    'iec 61439': ('IEC 61439', 'standard', 'electrical'),
    'iec61439': ('IEC 61439', 'standard', 'electrical'),
    'iec 61850': ('IEC 61850', 'standard', 'electrical'),
    'iec61850': ('IEC 61850', 'standard', 'electrical'),
    'switchgear': ('Switchgear', 'skill', 'electrical'),
    'lt panel': ('LT Panel', 'product', 'electrical'),
    'ht panel': ('HT Panel', 'product', 'electrical'),
    'mcc panel': ('MCC Panel', 'product', 'electrical'),
    'pcc panel': ('PCC Panel', 'product', 'electrical'),
    'busbar': ('Busbar', 'skill', 'electrical'),
    'relay coordination': ('Relay Coordination', 'skill', 'electrical'),
    'transformer': ('Transformer', 'skill', 'power'),
    'substation': ('Substation', 'skill', 'power'),
    # solar / renewable
    'solar': ('Solar', 'domain', 'solar'),
    'solar pv': ('Solar PV', 'skill', 'solar'),
    'photovoltaic': ('Photovoltaic', 'skill', 'solar'),
    'pv syst': ('PVsyst', 'tool', 'solar'),
    'pvsyst': ('PVsyst', 'tool', 'solar'),
    'inverter': ('Inverter', 'product', 'solar'),
    'mppt': ('MPPT', 'skill', 'solar'),
    'string inverter': ('String Inverter', 'product', 'solar'),
    'net metering': ('Net Metering', 'skill', 'solar'),
    'epc': ('EPC', 'skill', 'solar'),
    'rooftop solar': ('Rooftop Solar', 'skill', 'solar'),
    'ground mount': ('Ground Mount', 'skill', 'solar'),
    'bess': ('BESS', 'skill', 'renewable'),
    'battery storage': ('Battery Storage', 'skill', 'renewable'),
    'wind': ('Wind', 'domain', 'renewable'),
    # mechanical / general engineering tools
    'autocad': ('AutoCAD', 'tool', 'mechanical'),
    'solidworks': ('SolidWorks', 'tool', 'mechanical'),
    'ansys': ('ANSYS', 'tool', 'mechanical'),
    'hvac': ('HVAC', 'domain', 'hvac'),
    # IT (recruiters do get software folks too)
    'python': ('Python', 'programming_language', 'it'),
    'sql': ('SQL', 'programming_language', 'it'),
    'sap': ('SAP', 'product', 'it'),
    'sap pm': ('SAP PM', 'product', 'it'),
    'erp': ('ERP', 'skill', 'it'),
}

# Free-text industry / designation phrase  ->  canonical domain
DOMAIN_HINTS = {
    'solar': 'solar', 'renewable': 'renewable', 'photovoltaic': 'solar',
    'automation': 'automation', 'control': 'automation', 'instrumentation': 'instrumentation',
    'electrical': 'electrical', 'switchgear': 'electrical', 'panel': 'electrical',
    'power': 'power', 'substation': 'power', 'transmission': 'power',
    'manufacturing': 'manufacturing', 'production': 'manufacturing', 'plant': 'manufacturing',
    'hvac': 'hvac', 'mechanical': 'mechanical', 'oil': 'oil_gas', 'gas': 'oil_gas',
    'software': 'it', 'it services': 'it',
}

LEADERSHIP_CUES = (
    'led ', 'lead ', 'leading', 'managed', 'managing', 'manager', 'headed', 'head of',
    'heading', 'supervised', 'supervising', 'in-charge', 'in charge', 'incharge',
    'spearheaded', 'team of', 'reported to me', 'mentored', 'oversaw', 'project lead',
    'team lead', 'plant head', 'site incharge',
)
MANAGER_CUES = ('manager', 'head of', 'plant head', 'gm ', 'general manager', 'director', 'vp ', 'avp')


def _folded(*parts) -> str:
    return ' ' + ' '.join(_clean(p) for p in parts if p) + ' '


def resolve_domain(text: str, lexicon: dict) -> str:
    """Best-effort canonical domain from an industry / designation string."""
    t = _clean(text)
    for phrase, dom in DOMAIN_HINTS.items():
        if phrase in t:
            return dom
    return 'unknown'


def match_terms(text: str, lexicon: dict):
    """Return set of (canonical, category, domain) for lexicon terms present in text (word-ish boundary)."""
    t = _folded(text)
    hits = set()
    for term, (canon, cat, dom) in lexicon.items():
        # boundary-safe contains
        if (' ' + term + ' ') in t or t.find(' ' + term) >= 0 and re.search(r'\b' + re.escape(term) + r'\b', t):
            hits.add((canon, cat, dom))
    return hits


# ─────────────────────────────────────────────────────────────────────────────
# 4) SPANS + DERIVATION
# ─────────────────────────────────────────────────────────────────────────────

class Span:
    __slots__ = ('company', 'designation', 'start', 'end', 'is_current', 'text', 'domain', 'terms', 'leadership', 'manager')

    def __init__(self, company, designation, start, end, is_current, text, domain, terms, leadership, manager):
        self.company = company; self.designation = designation
        self.start = start; self.end = end; self.is_current = is_current
        self.text = text; self.domain = domain; self.terms = terms
        self.leadership = leadership; self.manager = manager

    def interval(self):
        return (self.start, self.end)


def build_spans(work_history, lexicon, now_m):
    spans = []
    for w in work_history or []:
        company = (w.get('company') or '').strip()
        desig = (w.get('designation') or '').strip()
        desc = (w.get('description') or '').strip()
        is_cur = bool(w.get('is_current')) or is_current_text(w.get('end_date'))
        start = _start_index(w.get('start_date'))
        end = _end_exclusive(w.get('end_date'), is_cur, now_m)
        text = _folded(desig, desc, company)
        domain = resolve_domain(_folded(desig, desc, w.get('industry')), lexicon)
        terms = match_terms(text, lexicon)
        # promote domain from matched terms if industry text was ambiguous
        if domain == 'unknown' and terms:
            domain = sorted(terms, key=lambda x: x[2])[0][2]
        leadership = any(cue in text for cue in LEADERSHIP_CUES)
        manager = any(cue in text for cue in MANAGER_CUES)
        spans.append(Span(company, desig, start, end, is_cur, text, domain, terms, leadership, manager))
    return spans


def _derive_seniority(overall_years, leadership_years):
    oy, ly = overall_years, leadership_years
    if oy >= 18 and ly >= 8: return 'executive'
    if oy >= 15 and ly >= 6: return 'director'
    if oy >= 10 and ly >= 3: return 'manager'
    if oy >= 12:             return 'principal'
    if oy >= 8 and ly >= 1:  return 'lead'
    if oy >= 5:              return 'senior'
    if oy >= 3:              return 'mid_level'
    if oy >= 1:              return 'junior'
    return 'entry'


def _derive_archetype(overall_years, leadership_years, has_manager, deepest_skill_y, deepest_domain_y, skill_count, domain_count):
    if overall_years <= 2:                                   return 'emerging_talent'
    if leadership_years >= 3 and has_manager:                return 'people_manager'
    if leadership_years >= 3:                                return 'technical_leader'
    broad = skill_count >= 12 or domain_count >= 3
    if deepest_domain_y >= 6 and not broad:                  return 'domain_expert'
    if deepest_skill_y >= 6 and not broad:                   return 'specialist'
    if broad:                                                return 'generalist'
    return 'specialist'


def derive_experience(work_history, candidate_skills=None, lexicon=None, now=None):
    """
    MAIN ENTRY POINT.
    work_history: list of dicts (your work_history rows) with keys:
        company, designation, start_date, end_date, is_current, description, (optional) industry
    candidate_skills: list[str] (candidate's key_skills + secondary_skills)
    lexicon: dict term->(canonical,category,domain). Defaults to BASE_LEXICON; feedback loop passes a grown one.
    Returns a JSON-serialisable dict of derived intelligence (store this in candidates.experience_intelligence).
    """
    lex = lexicon or BASE_LEXICON
    now_m = now_month_index(now)
    spans = build_spans(work_history, lex, now_m)
    dated = [s for s in spans if s.start is not None and s.end is not None and s.end > s.start]

    # OVERALL (merge every dated job; overlaps counted once, gaps excluded)
    overall_m, lo, hi = merge_intervals([s.interval() for s in dated])
    currently_active = any(s.is_current for s in dated)
    overall_years = months_to_years(overall_m)

    # PER-DOMAIN
    per_domain = {}
    domains_seen = {s.domain for s in dated if s.domain != 'unknown'}
    for dom in domains_seen:
        ivs = [s.interval() for s in dated if s.domain == dom or any(t[2] == dom for t in s.terms)]
        m, dlo, dhi = merge_intervals(ivs)
        if m > 0:
            per_domain[dom] = {'years': months_to_years(m), 'months': m,
                               'first': index_to_iso(dlo) if dlo is not None else None,
                               'last': index_to_iso(dhi) if dhi is not None else None}

    # PER-SKILL (from candidate's listed skills, dated via the jobs that mention them)
    per_skill = {}
    listed = []
    for raw in (candidate_skills or []):
        key = _clean(raw)
        if key in lex:
            canon, cat, dom = lex[key]
        else:
            canon, cat, dom = (raw.strip(), 'skill', 'unknown')
        listed.append((canon, cat, dom, key))
    # also fold in any lexicon terms that appear in job text even if not listed
    for s in dated:
        for (canon, cat, dom) in s.terms:
            if not any(c == canon for (c, _, _, _) in listed):
                listed.append((canon, cat, dom, canon.lower()))

    for canon, cat, dom, key in listed:
        text_spans = [s for s in dated if any(t[0] == canon for t in s.terms)]
        method = 'text_match'
        use = text_spans
        if not use and dom != 'unknown':                     # not mentioned per-job -> infer via domain
            use = [s for s in dated if s.domain == dom or any(t[2] == dom for t in s.terms)]
            method = 'domain_inferred'
        if not use:
            continue
        m, slo, shi = merge_intervals([s.interval() for s in use])
        if m <= 0:
            continue
        conf = 0.9 if method == 'text_match' else 0.6
        per_skill[canon] = {
            'years': months_to_years(m), 'months': m, 'category': cat, 'domain': dom,
            'method': method, 'confidence': conf,
            'first': index_to_iso(slo) if slo is not None else None,
            'last': index_to_iso(shi) if shi is not None else None,
            'currently_used': any(s.is_current for s in use),
            'evidence': sorted({s.company for s in use if s.company})[:5],
        }

    # LEADERSHIP
    lead_ivs = [s.interval() for s in dated if s.leadership or s.manager]
    lead_m, _, _ = merge_intervals(lead_ivs)
    leadership_years = months_to_years(lead_m)
    has_manager = any(s.manager for s in dated)

    # SENIORITY + ARCHETYPE
    deepest_skill_y = max([v['years'] for v in per_skill.values()], default=0)
    deepest_domain_y = max([v['years'] for v in per_domain.values()], default=0)
    seniority = _derive_seniority(overall_years, leadership_years)
    archetype = _derive_archetype(overall_years, leadership_years, has_manager,
                                  deepest_skill_y, deepest_domain_y, len(per_skill), len(per_domain))

    # dominant (most-months) domain = "relevant" experience
    relevant = None
    if per_domain:
        top = max(per_domain.items(), key=lambda kv: kv[1]['months'])
        relevant = {'domain': top[0], **top[1]}

    return {
        'schema_version': 1,
        'engine': 'hirelab-experience-engine',
        'computed_at': (now or datetime.date.today()).isoformat(),
        'overall': {'years': overall_years, 'months': overall_m, 'currently_active': currently_active,
                    'first': index_to_iso(lo) if lo is not None else None,
                    'last': index_to_iso(hi) if hi is not None else None,
                    'dated_jobs': len(dated), 'total_jobs': len(spans),
                    'method': 'merged_intervals'},
        'relevant': relevant,
        'domains': per_domain,
        'skills': per_skill,
        'leadership': {'years': leadership_years, 'months': lead_m, 'has_manager_role': has_manager},
        'seniority': seniority,
        'archetype': archetype,
        'undated_jobs': [{'company': s.company, 'designation': s.designation}
                         for s in spans if s.start is None or s.end is None],
    }
