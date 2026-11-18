# Screener v2 + Training — Deploy Guide (Nov 2026)

Fixes the Naukri Search-page screener (missing profiles, wrong ranking, weak Job Breakdown)
and adds the training loop (✓ Open / ✗ Skip on every card) to the HireLab Assistant.

Two parts, deployed in this order: **Stage 1 = ATS (server)**, **Stage 2 = Chrome extension**.
The old extension keeps working against the new server, so there is no downtime between stages.

---

## What changed

| Problem | Root cause | Fix |
|---|---|---|
| Not all profiles come through | The whole page (1,000+ lines) went to DeepSeek in one call, which skips cards on long lists; pages over 60,000 characters were cut silently | Page is read in windows of 180 lines (4 in parallel); summary lines (experience / pay) are counted in code and any card the AI missed is recovered; limit raised to 250,000 characters; panel shows "Found 38 profiles (page seems to have about 40)" |
| Ranking is wrong | Each batch of 10 was scored on its own curve ("only a few strong per page"); experience / CTC were guessed by the AI; nothing was learned from your decisions | Experience, CTC, notice and employer are read **in code**; the AI answers 3 fixed questions per card (role fit 0-4, domain fit 0-4, core fit 0-3); **one formula** gives the score for every card; hard rules in code: avoid-company → skip, CTC over budget → skip (unless a rule allows), wrong role → skip |
| Job Breakdown is weak | Built only from the JD, as a skills list — but a search card shows employer and designation, not skills | Breakdown v2 adds **Target / Acceptable / Avoid companies** and **Designations that fit**; the AI also learns from people you already moved forward on the job; every old AI breakdown is rebuilt once automatically (edited ones are kept — add companies in Edit) |
| Does not learn | — | **Training**: ✓ Open / ✗ Skip + optional reason on each card; pushing a CV marks that card "CV downloaded"; your last decisions (disagreements first) are put in every screening prompt; "Suggest rules" proposes new rules from disagreements; agreement meter; JSONL export for model training |

Score formula (0-100): role fit ×7 (28) + domain fit ×6 (24) + core fit ×6 (18) + company (target 12 / acceptable 7 / unknown 4 / ex-avoid 3) + experience (in range 8 / within 1-2 yrs 4 / far 0) + CTC (within budget 5 / up to +10% 2) + location (match 5 / unknown 3 / other 1).
Open ≥ 68, Maybe 45-67, Skip < 45.

---

## Files

**ATS (upload to GitHub repo root, same paths):**

| File | Status |
|---|---|
| `modules/card_facts.py` | NEW — reads numbers off a card, one scoring formula, card counting |
| `modules/screen_feedback.py` | NEW — training routes + `screen_feedback` table |
| `modules/assistant.py` | changed — new splitter (`/api/assistant/split`) |
| `modules/naukri_screen.py` | changed — rubric prompt + code facts + past decisions |
| `modules/job_profile.py` | changed — breakdown v2 (companies, designations), auto-upgrade |
| `modules/__init__.py` | changed — one line: registers `screen_feedback` |
| `index.html` | changed — Job Breakdown tab shows/edits companies + designations |
| `test_screen_v2.py` | NEW — 18 tests |

Database: **additive only** — one new table `screen_feedback`. No existing table or column changes.
Card text is stored only when you press ✓ / ✗ or push a CV, with phone numbers and e-mails removed.

**Extension:** `hirelab-assistant-v2.5.0/` (manifest, sidepanel.html/.js/.css changed).

---

## Stage 1 — ATS

**Via the pull request:** merging the PR on GitHub *is* Stage 1 — Render auto-deploys; then do the checks in step 5.
**Rollback:** on the merged PR press **Revert** and merge the revert PR (Render redeploys the old code).

**Via the zip (manual upload):**

1. Take your usual backup first (Render disk / Backup.bat).
2. GitHub → repo `nitinkumar-star/hirelabscreener` → **Add file → Upload files**.
3. Drag the **`modules` folder** and **`index.html`** and **`test_screen_v2.py`** from this zip (not the `rollback` folder). Commit.
4. Wait for Render to finish deploying (~2-3 min), then **Ctrl+Shift+R** on the ATS.
5. Check:
   - Open any job → **Job Breakdown** tab. First open rebuilds it with AI (~15 sec) and now shows **Companies** and **Designations**. Correct the company lists once (e.g. remove the client from Avoid if you do source from it).
   - Old extension v2.4 still screens a pasted search page (now with the new scoring).

### Rollback (Stage 1)
Upload the files from the `rollback/` folder of this zip (original `assistant.py`, `naukri_screen.py`,
`job_profile.py`, `__init__.py`, `index.html`) over the new ones and commit. With the old `__init__.py`
the training routes are no longer mounted. `card_facts.py` / `screen_feedback.py` can stay (unused)
or be deleted on GitHub. The `screen_feedback` table stays in the database and is harmless.
Breakdowns saved by v2 keep working with the old code (extra fields are ignored).

---

## Stage 2 — Extension

**Important:** replace the files **inside the same folder** you loaded v2.4.0 from. A new folder gives
the extension a new ID, and the ATS only trusts the IDs in `ALLOWED_EXTENSION_IDS` on Render.

1. Open the folder you loaded HireLab Assistant from (chrome://extensions → HireLab Assistant → Details → "Loaded from").
2. Copy all files from `hirelab-assistant-v2.5.0/` into it, overwriting.
3. chrome://extensions → HireLab Assistant → **Reload** (↻). Version shows **2.5.0**.
4. Check: Search page tab → paste a Naukri results page → **Screen these profiles**:
   - "Found N profiles (page seems to have about M)"
   - each card shows years · LPA · notice · 🎯 target / ⛔ avoid company
   - ✓ Open / ✗ Skip buttons; the **Training** card shows today / this job / total and agreement %.

### Rollback (Stage 2)
Copy the v2.4.0 files back into the same folder and press Reload.

---

## Daily use (training)

- After each screening, press **✓ Open** or **✗ Skip** on every card you have an opinion on. A yellow
  border means you and the AI disagree — those teach the most; add a one-line reason (Win+H lets you speak it).
- Download the CVs you like and push them from the Candidate tab — the card is marked "CV downloaded" automatically.
- Every few days press **💡 Suggest rules from my decisions** and add the rules that are right.
- **⤓ Export training data** (admin) downloads a JSONL file (job + breakdown + card + your label) — this is the
  dataset for training your own model later. Target: ~3,000 decisions.
