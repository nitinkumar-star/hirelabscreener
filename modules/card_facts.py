"""
Card facts — read the hard numbers off a Naukri search-result card IN CODE,
and turn the AI's rubric into one comparable score.

Why this exists (Nov 2026 fix)
  The old Screener let DeepSeek read experience / CTC / location and also
  pick a 0-100 score on its own. Two problems followed:
    1. Numbers were guessed, so over-budget profiles slipped through and
       good ones were skipped.
    2. Each batch of 10 cards was scored on its own curve, so a 70 in one
       batch did not mean the same as a 70 in the next one.
  Now the numbers are read here with plain patterns, the AI only answers
  three fixed questions per card (role fit, domain fit, core skills), and
  the final score is computed here with ONE formula for every card.

Pure functions only — no database, no Flask. Unit tested in
test_screen_v2.py.
"""

import re

# ── what a card looks like ────────────────────────────────────────────────
# Naukri cards show experience as "5y 2m", "5 Yrs 2 Mos", "5.2 Years", and pay
# as "₹ 6.5 Lacs", "6.5 LPA", "12 Lakh". The patterns below accept all of them.
_EXP_YM = re.compile(r'(?<![\d.])(\d{1,2})\s*(?:y|yr|yrs|year|years)\b\.?\s*(?:(\d{1,2})\s*(?:m|mo|mos|month|months)\b)?', re.I)
_EXP_DEC = re.compile(r'(?<![\d.])(\d{1,2}(?:\.\d{1,2})?)\s*\+?\s*(?:years?|yrs?)\b', re.I)
_CTC = re.compile(r'(?:₹|rs\.?|inr)?\s*(\d{1,3}(?:\.\d{1,2})?)\s*(?:-\s*\d{1,3}(?:\.\d{1,2})?\s*)?'
                  r'(lacs?|lakhs?|lpa|l\.p\.a\.?|crores?|cr)\b', re.I)
_NOTICE_DAYS = re.compile(r'notice[^\n\d]{0,30}?(\d{1,3})\s*(?:days?|d)\b|(\d{1,3})\s*days?\s*(?:or less\s*)?notice', re.I)
_NOTICE_MONTHS = re.compile(r'notice[^\n\d]{0,30}?(\d)\s*months?', re.I)
_IMMEDIATE = re.compile(r'\b(immediate(?:ly)?\s*(?:joiner|available|join)?|serving notice|currently serving)\b', re.I)
_CURRENT = re.compile(r'^\s*(?:current(?:ly)?|curr\.?|present)\b', re.I)
_PREVIOUS = re.compile(r'^\s*(?:previous|prev\.?|past)\b', re.I)


def parse_exp(text):
    """Total experience in years (float) or None."""
    t = str(text or '')
    m = _EXP_YM.search(t)
    if m:
        y = int(m.group(1))
        mo = int(m.group(2) or 0)
        if y <= 45 and mo < 12:
            return round(y + mo / 12.0, 1)
    m = _EXP_DEC.search(t)
    if m:
        v = float(m.group(1))
        if v <= 45:
            return round(v, 1)
    return None


def parse_ctc(text):
    """Current CTC in lakhs per annum (float) or None. 'Not disclosed' -> None."""
    t = str(text or '')
    for m in _CTC.finditer(t):
        v = float(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith('cr'):
            v *= 100
        if 0.5 <= v <= 500:
            return round(v, 2)
    return None


def parse_notice(text):
    """Notice period in days (int) or None. Immediate / serving -> 0."""
    t = str(text or '')
    m = _NOTICE_DAYS.search(t)
    if m:
        return int(m.group(1) or m.group(2))
    m = _NOTICE_MONTHS.search(t)
    if m:
        return int(m.group(1)) * 30
    if _IMMEDIATE.search(t):
        return 0
    return None


def current_line(text):
    """The card's 'current role' line, or '' when it cannot be told apart."""
    lines = [l.strip() for l in str(text or '').split('\n') if l.strip()]
    for l in lines:
        if _CURRENT.match(l):
            return l
    for l in lines[1:6]:
        if _PREVIOUS.match(l):
            continue
        if re.search(r'\bat\b|@', l, re.I):
            return l
    return ''


# ── company / title matching ──────────────────────────────────────────────
_CORP_TAIL = {'pvt', 'private', 'ltd', 'limited', 'llp', 'inc', 'co', 'corp', 'corporation', 'company', 'india', 'the'}


def _seq(t):
    return re.findall(r'[a-z0-9&]+', str(t or '').lower())


def phrase_in(text, phrase):
    """True when the phrase's words appear together, in order, in the text
    ('Tata Power' matches 'Sr Engineer at Tata Power Co Ltd', not 'Tata Projects')."""
    want = _seq(phrase)
    while want and want[-1] in _CORP_TAIL:
        want.pop()
    while want and want[0] in _CORP_TAIL:
        want.pop(0)
    if not want or not any(len(w) >= 2 for w in want):
        return False
    have, n = _seq(text), len(want)
    return any(have[i:i + n] == want for i in range(len(have) - n + 1))


def _names(lst):
    return [str(x).strip() for x in (lst or []) if str(x or '').strip()]


def company_class(text, companies):
    """('target'|'ok'|'avoid'|'avoid_past'|'', matched_name).
    Avoid counts only when it is the CURRENT employer; a past stint at an
    avoided company is reported as 'avoid_past' (a flag, not a reject)."""
    companies = companies if isinstance(companies, dict) else {}
    cur = current_line(text)
    scope = cur or str(text or '')
    for c in _names(companies.get('avoid')):
        if phrase_in(scope, c):
            return 'avoid', c
    for c in _names(companies.get('target')):
        if phrase_in(scope, c):
            return 'target', c
    for c in _names(companies.get('ok')):
        if phrase_in(scope, c):
            return 'ok', c
    if cur:
        # current employer is known and is none of the listed ones; a past
        # stint at a target company still counts for something
        for c in _names(companies.get('target')):
            if phrase_in(text, c):
                return 'ok', c
        for c in _names(companies.get('avoid')):
            if phrase_in(text, c):
                return 'avoid_past', c
    return '', ''


def location_fit(text, locations):
    """True / False, or None when the job has no location limit."""
    locs = _names(locations)
    if not locs:
        return None
    t = str(text or '')
    for l in locs:
        if l.lower() in ('anywhere', 'pan india', 'any', 'all india', 'remote'):
            return True
        if phrase_in(t, l):
            return True
    return False


def card_facts(text, limits=None, companies=None):
    """Everything code can read off one card, for the prompt and the score."""
    limits = limits if isinstance(limits, dict) else {}
    cc, cname = company_class(text, companies)
    return {
        'exp': parse_exp(text),
        'ctc': parse_ctc(text),
        'notice': parse_notice(text),
        'company': cc,
        'company_name': cname,
        'location_ok': location_fit(text, limits.get('locations')),
    }


def facts_line(f):
    """One short line the AI can read (so it does not re-guess the numbers)."""
    bits = []
    bits.append('exp %s yrs' % ('%g' % f['exp'] if f.get('exp') is not None else 'unknown'))
    bits.append('CTC %s' % ('%g LPA' % f['ctc'] if f.get('ctc') is not None else 'unknown'))
    if f.get('notice') is not None:
        bits.append('notice %d days' % f['notice'])
    cls = {'target': 'TARGET company', 'ok': 'acceptable company', 'avoid': 'AVOID company (current)',
           'avoid_past': 'worked at an avoid company earlier'}.get(f.get('company') or '', '')
    if cls:
        bits.append('%s: %s' % (cls, f.get('company_name') or ''))
    if f.get('location_ok') is False:
        bits.append('location outside the job locations')
    return 'CODE-READ FACTS: ' + '; '.join(bits)


# ── the one scoring formula ───────────────────────────────────────────────
# AI part (70): role_fit 0-4 x7, domain_fit 0-4 x6, core_fit 0-3 x6
# Code part (30): company 12, experience 8, CTC 5, location 5
OPEN_AT, MAYBE_AT = 68, 45


def _clip(v, lo, hi):
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, v))


def rules_mention_budget(rules):
    return any(re.search(r'\b(ctc|budget|lpa|lakh|lac|salary|package)\b', str(r), re.I) for r in (rules or []))


def combine(ai, facts, limits, budget_relaxed=False):
    """Final {'verdict','score','reason','flags','parts'} for one card.
    ai: {'role_fit','domain_fit','core_fit','rule_break','reason','flags'} from DeepSeek."""
    limits = limits if isinstance(limits, dict) else {}
    flags = [str(x)[:40] for x in (ai.get('flags') or []) if x][:4] if isinstance(ai.get('flags'), list) else []
    reason = re.sub(r'\s+', ' ', str(ai.get('reason') or '')).strip()[:200]
    role = _clip(ai.get('role_fit'), 0, 4)
    dom = _clip(ai.get('domain_fit'), 0, 4)
    core = _clip(ai.get('core_fit'), 0, 3)
    role = 0 if role is None else role
    dom = 0 if dom is None else dom
    core = 0 if core is None else core
    ai_pts = role * 7 + dom * 6 + core * 6

    # company
    cc = facts.get('company') or ''
    comp_pts = {'target': 12, 'ok': 7, 'avoid_past': 3, 'avoid': 0}.get(cc, 4)
    # experience
    exp, emin, emax = facts.get('exp'), limits.get('exp_min'), limits.get('exp_max')
    exp_pts, exp_note = 4, ''
    if exp is not None and (emin or emax):
        lo, hi = (emin or 0), (emax or 99)
        if lo <= exp <= hi:
            exp_pts = 8
        elif lo - 1 <= exp <= hi + 2:
            exp_pts, exp_note = 4, ('exp low' if exp < lo else 'exp high')
        else:
            exp_pts, exp_note = 0, ('exp far below' if exp < lo else 'exp far above')
    elif exp is not None:
        exp_pts = 6
    # CTC
    ctc, cmax = facts.get('ctc'), limits.get('ctc_max')
    ctc_pts, over = 3, False
    if ctc is not None and cmax:
        if ctc <= cmax:
            ctc_pts = 5
        elif ctc <= cmax * 1.1:
            ctc_pts = 2
            flags.append('CTC slightly above budget')
        else:
            ctc_pts, over = 0, True
    elif ctc is not None:
        ctc_pts = 4
    # location
    lo = facts.get('location_ok')
    loc_pts = 5 if lo is True else (1 if lo is False else 3)
    if lo is False:
        flags.append('relocation')

    score = max(0, min(100, ai_pts + comp_pts + exp_pts + ctc_pts + loc_pts))
    verdict = 'open' if score >= OPEN_AT else ('maybe' if score >= MAYBE_AT else 'skip')

    # hard rules — the same for every card
    hard = ''
    if cc == 'avoid':
        hard = 'Current company is on the avoid list (%s)' % (facts.get('company_name') or '')
    elif over and not budget_relaxed:
        hard = 'CTC above budget (%g LPA vs %g LPA)' % (ctc, cmax)
    elif str(ai.get('rule_break') or '').strip():
        hard = 'Breaks your rule: ' + re.sub(r'\s+', ' ', str(ai.get('rule_break'))).strip()[:120]
    elif role == 0:
        hard = 'Wrong kind of role for this job'
    if hard:
        verdict = 'skip'
        score = min(score, MAYBE_AT - 1)
        reason = hard + ('. ' + reason if reason else '')
    elif over:
        flags.append('over budget (rule allows)')
    if exp_note and exp_note not in flags:
        flags.append(exp_note)
    if cc == 'target':
        flags.insert(0, 'target co.')
    seen, fl = set(), []
    for f in flags:
        if f.lower() not in seen:
            seen.add(f.lower())
            fl.append(f)
    return {'verdict': verdict, 'score': score, 'reason': reason[:220], 'flags': fl[:4],
            'parts': {'role': role, 'domain': dom, 'core': core, 'company': comp_pts,
                      'exp': exp_pts, 'ctc': ctc_pts, 'location': loc_pts}}


# ── splitting support: count the cards on a pasted page ───────────────────
# Every Naukri card carries one "summary" line with experience and/or pay.
# Counting those lines gives a fair estimate of how many cards were pasted,
# so the splitter can tell the recruiter "found 32 of about 40".
_SUMMARY = re.compile(r'(\d{1,2}\s*y(?:rs?|ears?)?\s*\d{0,2}\s*m(?:os?|onths?)?\b)|'
                      r'(\d{1,2}(?:\.\d)?\s*(?:years?|yrs?)\b.{0,60}?(?:lacs?|lakhs?|lpa|not disclosed))', re.I)


def anchor_lines(text):
    """1-based line numbers that look like a card's summary line."""
    out = []
    for i, l in enumerate(str(text or '').split('\n'), 1):
        if len(l) > 220:
            continue
        if _SUMMARY.search(l):
            if out and i - out[-1] <= 2:
                continue        # same card, the numbers wrapped onto two lines
            out.append(i)
    return out


def estimate_cards(text):
    return len(anchor_lines(text))
