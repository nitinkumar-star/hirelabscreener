# Skill Engine, Phase 1: dictionary + job skill spec + candidate skill depth

Phase 1 builds the skill data and **shows** it, so you can check that the engine reads skills correctly.
**No score changes in this phase.** The new skill score comes in Phase 2, first side by side with the old one
(owner's decision 2-A).

## Owner's decisions (Nov 2026)

| Decision | Choice | What it means |
|---|---|---|
| 1. New names and nearby skills the AI suggests | **A** | They work at once for the job they came from (marked "AI"). For every job, after an admin approves them in Settings |
| 2. Switching to the new score | **A** | Phase 2 shows the old and the new score side by side for 2–3 weeks before switching |
| 3. Whose learning is it | **A** | Each agency's additions stay with that agency. The base dictionary is the same for everyone |

## What you will see

**1. Skill dictionary** (`skill_taxonomy.py`)
- 114 skills for LV, MV, HV / GIS, power, cables, wiring devices & lighting, BMS / fire / security, data center,
  UPS & batteries, automation, solar & BESS, EV, engineering work, functions (channel / key account / specification /
  pre-sales / service …), standards and tools.
- About 1,100 other names: brands (Masterpact, Emax, 3WL → **ACB**; Tmax, Compact NSX, 3VA → **MCCB**), spellings
  (LT / LV switchgear) and abbreviations.
- Nearby skills with partial credit (LV switchgear → MV switchgear 60%; pre-sales → key account sales 50%).
- Short or generic words that mean something else in CVs ("db", "noc", "service", "quality" …) are not used on
  their own.

**2. Skill spec on every job** (Job → **Job Breakdown** tab, under the breakdown)
- For every skill the job needs:
  - importance (core / important / nice)
  - minimum years
  - involvement (Led / Did it / Supported / Any)
  - how recent (last N years)
  - which nearby skills also count, and how much
- Made by AI from the job text and the Job Breakdown the first time the tab is opened.
- **Edit** changes it; **Rebuild with AI** makes it again.
- If the job text changes, an AI-made spec is rebuilt; an edited one is kept and marked "job text changed".

**3. Skill depth on every candidate** (candidate profile → Overview, under the Market note)
- For every skill:
  - **years**, calculated from the dated work history (overlapping roles counted once; never guessed)
  - **current role** or **last used**
  - **involvement** (Led / Did it / Supported / Exposure), read by AI, with the **exact CV line** it is based on
  - the **source** (CV or Naukri profile)
- A quote that is not really in the CV is dropped.
- Roles without dates show "years unknown".
- New candidates are analysed automatically within a few minutes. **Analyse skills** / **Re-analyse** runs it now
  (the button says "Refresh" when the profile changed).

**4. Settings → My Account → Skill Engine** (admins)
- The on/off switch.
- **AI suggestions to approve:** new skills, other names and nearby skills found while reading jobs.
  **Approve** makes one work for every job; **Reject** drops it.
- **Analyse existing candidates:** the whole database, newest first. **Stop** pauses; **Start again** continues.

## Files

| File | What changed |
|---|---|
| `skill_taxonomy.py` (new) | The base dictionary |
| `modules/skill_engine.py` (new) | Dictionary per agency, skill spec, skill depth, suggestions, backfill worker, routes |
| `skill-engine.js` (new) | Skill depth card, Skill spec card, Settings section |
| `modules/__init__.py` | Adds `'skill_engine'` |
| `index.html` | Loads the script; cards on the profile and the Job Breakdown tab; Settings section |
| `academy_content.py` | New Academy guide "Skill Engine: skill spec and skill depth" (+ related links) |
| `test_skill_engine.py` (new) | 10 tests |

**New tables:**
- `skill_custom`: agency additions and AI suggestions
- `mandate_skill_spec`: one spec per job
- `candidate_skill_depth`: one result per candidate record
- `skill_backfill`: progress of "Analyse existing candidates"

The switch `skill_engine_on` (default **on**) is one row in `insight_settings`. Nothing existing is changed. The
old `xp` module and its `domain_lexicon` are left as they are; the new engine only reuses `xp`'s date maths.

## AI cost
- **Job spec:** one call per job (when first opened, and when the job text changes).
- **Skill depth:** one call per person, about 2,000–3,000 tokens because it includes the dictionary and the CV.
  The backfill for about 1,840 candidates is roughly ₹400–700, once.
- Everything goes through the AI cost meter (`skill-spec`, `skill-depth`) and stops at the monthly AI limit.
  Without a DeepSeek key, skill depth still shows years and sources; involvement needs the key.

## Deploy (GitHub Desktop → Render)
1. Merge the PR on GitHub, or copy the files above in **GitHub Desktop** → **Commit to main** → **Push origin**.
2. Wait for Render to show **Live**. The log shows `[modules] mounted: skill_engine` and
   `[skill-engine] background thread started`.
3. Hard-refresh the app (**Ctrl + Shift + R**).

## Check it in the live app
1. Open a job with a JD → **Job Breakdown**. Under the breakdown, the **Skill spec** appears after about 10
   seconds. Check that the skills, years and involvement make sense.
2. Press **Edit**, type "tmax" in "+ Add a skill": MCCB is offered. Save → "Edited by you".
3. Open a candidate with a CV and work history → Overview → **Skill depth** → **Analyse skills**. Check the years
   against their roles, and that each Led / Did it / Supported has a quote from the CV.
4. Settings → My Account → **Skill Engine**: approve or reject a few AI suggestions, then **Start** "Analyse
   existing candidates" and watch the bar.
5. Tell me what looks wrong (a missing skill, a wrong alias, wrong years). It goes into the dictionary before
   Phase 2.

## Tests

```bash
python -m pytest test_skill_engine.py -q
```
```bash
python -m pytest -q
```

## Rollback
1. Untick **Skill Engine** in Settings → My Account. The cards disappear and the routes answer "switched off".
2. Or remove `'skill_engine'` from `modules/__init__.py` and redeploy.
3. Or revert the PR.

No score uses the Skill Engine yet, so nothing else changes. The new tables can stay.

## Fix: 502 / "Instance restarted" after Phase 1 (Nov 2026)
**Cause:** opening a profile made the Skill depth card read the whole CV PDF just to check whether the result was out
of date. The skill worker, the tagging worker and the search-embedding worker also read CVs, sometimes at the same time.
PDF reading uses a lot of memory, and on the 512 MB instance Render restarted the app (the 502s).

**Fix:**
- Opening a profile no longer reads the CV. "Out of date" is now based on the CV file and its size.
- The Skill Engine and the tagger read only the first 4 pages of a PDF, one page at a time.
- Only one CV is read at a time in the whole app (shared lock with the embedding worker).

Results already analysed show **Refresh** once (the out-of-date check changed). Nothing is re-analysed automatically,
so there is no extra AI cost.

Files: `modules/skill_engine.py`, `modules/insight_capture.py`, `server.py` (lock around `_candidate_cv_text` only),
`test_skill_engine.py` (+1 test).
