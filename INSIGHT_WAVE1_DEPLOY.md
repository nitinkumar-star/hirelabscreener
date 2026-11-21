# Insight Engine, Wave 1: Capture. Deploy & rollback

Wave 1 makes the ATS keep the insight it used to throw away, at the moment it is known:

* **Every "no" gets a reason.** A negative stage move (Not Interested, Interview Backout, Not Suitable,
  Screened-Out, Client Rejected on Paper / After Interview) opens a small picker with one-tap reasons and an
  optional line of detail. Cancel means the candidate stays in the stage they were in.
* **Every client rejection gets the client's feedback.** It's required in the picker, stored with the reason
  and **added** to the candidate's Client Feedback with the date. Nothing already written there is replaced.
* **Call capture card** (titled *Market note*) on the candidate profile (Overview, under Profile): a **market
  note** about a company. Nothing is asked twice: expected CTC and notice period stay in **Key Facts**, and
  yearly target, team size and the other role fields stay in the **Profile** card (profile groups).
* **Call analysis proposes the same data.** After a recording is analysed, the "AI ko call mein ye details mili"
  box can also show *Reason for no*, *Market notes*, *Direct reports*, *Indirect team* and *Yearly target*.
  Nothing is saved until the recruiter ticks it and presses **Update selected**.
* **Auto-tags.** Every new candidate gets a role family, function tags, domain tags and a level band from
  the profile / CV within a few minutes. Only empty fields are filled, never what a person typed.
  **Settings → My Account → Insight capture → Start tagging** does the existing database, newest first.
* **6:30 check** on Home, plus a red count on the Home menu item: records *you* touched today that are missing
  a reason, missing client feedback, or have expected CTC 0 at Interested or later. Each row has a **Fix** button
  (for expected CTC 0 it opens the profile with Key Facts in edit mode, cursor in Expected CTC).

Everything is additive: new tables, two new columns, one new module. No column is renamed, dropped or changed,
no route is removed, and stage moves without a reason (bulk moves, WhatsApp, AI follow-ups, reminders …)
work exactly as before.

## Files

| File | What changed |
|---|---|
| `modules/insight_capture.py` (new) | Tables, the reason helper, market notes, Call capture API, call-analysis proposals, auto-tagger + backfill thread, 6:30 check |
| `insight-capture.js` (new, repo root) | The reason picker, Call capture card, 6:30 panel and Settings panel (served at `/insight-capture.js`) |
| `modules/__init__.py` | Adds `'insight_capture'` to the module list (one line, at the end) |
| `server.py` | `move_stage`: accepts an optional `reason_code` / `reason_detail` / `client_feedback`. `analyse_call`: 5 extra keys in the DeepSeek prompt, added to the existing proposals |
| `modules/call_intel.py` | "Update selected" can also apply the new proposal types |
| `index.html` | Picker on list view, drag-drop and the profile stage dropdown; Call capture card; 6:30 card on Home; Settings section; new proposal rows |
| `team-chat.html` | Picker before a stage change from a candidate card |
| `recruitos-live.html` | Picker before a stage change |
| `test_insight_capture.py` (new) | 18 tests (see below) |
| `INSIGHT_WAVE1_DEPLOY.md` (new) | This file |

### New in the database (created automatically on start-up)

| Table / column | Holds |
|---|---|
| `candidate_reasons` | One row per reason: candidate, job, stage, reason code, detail, client feedback, source (`manual` / `call_ai` / `bulk` / `auto`), who, when. Full history; the newest row is the current reason |
| `market_notes` | One row per signal: the company (+ normalised key, same as the Company Talent Map), signal type (`hiring`, `freeze`, `exit`, `join`, `new_order_project`, `pay`, `reorg`, `other`), note, source, who, when |
| `insight_tag_log` | Which candidates the tagger has processed (this is what makes the backfill resumable) |
| `insight_backfill` | Per agency: the "Tag existing candidates" progress |
| `insight_settings` | Per agency: the `insight_capture_on` switch (default **on**) |
| `candidates.role_family`, `candidates.level_band` | New text columns, empty by default |

### Role families → the existing Function tags

The Talent filters already use a Function tag list. The tagger writes the family into the new `role_family`
column, and the matching **existing** tag words into `function_tags`, so there is still one vocabulary:

| Role family | Function tags written |
|---|---|
| Channel Sales | Channel Sales |
| Key Account / Project Sales | Key Account Management, Sales |
| Application / Pre-sales Engineering | Application Engineering, Pre-Sales |
| Project Execution | Project Management |
| Service / Field | Service & Support |
| Product Management | Product Management |
| Design / R&D Engineering | R&D, Design |
| Manufacturing / Quality | Quality, Operations |
| Procurement / SCM | Procurement, Supply Chain |
| Business Development | Business Development *(Marketing counts here)* |
| Support functions (HR / Finance / IT / Digital) | HR, Finance or IT / Digital |
| General Management / Leadership | General Management, Strategy |

Domains: LV, MV, HV / GIS, Industrial Automation, Solar / Renewables, Data Center, BMS, Cables & Wires,
Power Electronics / UPS, Other. Level bands: Engineer / Executive, Asst / Dy Manager, Manager, Sr Manager,
DGM / AGM, GM+.

If a person already set Function tags, the family is taken from those tags (no AI), and the tags are kept.

## Deploy (GitHub Desktop → Render)

1. Open **GitHub Desktop** and choose the `hirelabscreener` repository.
2. Copy the files from the table above into the repository folder, replacing the old ones at the same paths.
   `insight-capture.js` goes in the top folder, next to `index.html`.
3. GitHub Desktop now lists the changed files. Write a summary such as *Insight Wave 1: capture* and press
   **Commit to main**, then **Push origin**.
4. Render starts a new deploy by itself. Wait until the Render dashboard shows **Live** (usually 2–4 minutes).
   The new tables are created on start-up; the log shows `[modules] mounted: insight_capture` and
   `[insight] tagging thread started`.
5. In the browser, hard-refresh the app once (**Ctrl + Shift + R**, or Cmd + Shift + R on a Mac).

AI cost: tagging uses DeepSeek, about one small call (~1,200 tokens) per person. The full backfill for about
1,840 candidates is roughly 2.2 million tokens. It shows in the AI cost meter under the endpoint
`insight-tag`, and it stops on its own at the agency's monthly AI limit. Call analysis adds about 150 output
tokens per call.

## Check it in the live app

1. **Reason picker.** Open any job → Pipeline → drag a test candidate to *Not Interested*. The picker opens.
   Press **Cancel**: the card goes back. Drag again, pick *CTC gap*, type a line, press **Save & move**.
   Open the candidate: the Journey shows "… · Reason: CTC gap — your line".
2. **Client feedback.** Move a candidate to *Client Rejected on Paper*. **Save & move** stays grey until the
   feedback box has text. Afterwards, the candidate's Client Feedback shows the old text, then `---`, then
   today's date and the new feedback.
3. **Existing moves untouched.** Move someone to *Interested* or *Shared with Client*: no picker, same as before.
4. **Market note.** Open a candidate profile → Overview: the *Market note* card is under Profile (no CTC,
   notice, target or team fields: those are in Key Facts and the Profile card). Write a note → **Save note** →
   "Saved ✓" and the note appears below.
5. **Call analysis.** Analyse a call where the candidate says no. The proposal box can show *Reason for no* and
   *Market notes*. They are only saved after **Update selected**.
6. **6:30 check.** Go to Home. The *6:30 check* card is under the greeting. The Home menu item shows a red count
   when something is missing. Press **Fix** on a row and save: the row disappears.
7. **Tagging.** Settings → My Account → *Insight capture* → **Start tagging**. The bar moves every few seconds
   (about 20 people a minute). **Stop** pauses it; **Start again** continues where it stopped.
8. **Switch.** Untick *Insight capture* in the same place. The picker, card and 6:30 check disappear for
   everyone in the agency. Tick it again to bring them back.

## Tests

```bash
python -m py_compile server.py modules/*.py
```
```bash
node --check insight-capture.js
```
```bash
python -m pytest test_insight_capture.py -q
```
```bash
python -m pytest -q
```

`test_insight_capture.py` covers: reason saved with a stage move; invalid reason refused before anything is
written; moves without a reason unchanged; client feedback required and appended (never overwritten);
reason-only fix; market note company key normalised; Call capture card incl. the linked copy on another job;
call-analysis proposals never auto-applied (and not approvable by another agency); tagging never overwrites;
backfill newest first, stoppable, resumable, failed records retried, other agencies untouched; new-candidate
sweeper; 6:30 counts and fixes; the switch (admin only, per agency); tenant isolation on every new route;
recruiter scope; freelancers blocked.

## Rollback

Pick the smallest step that solves the problem:

1. **Hide everything, keep the code:** Settings → My Account → *Insight capture* → untick. The picker, Call capture
   card, 6:30 check and the new call-proposal keys go away for that agency. The tagging of *new* candidates stops
   too. A backfill that was started keeps running until **Stop** is pressed.
2. **Stop the tagging only:** press **Stop** under *Tag existing candidates*.
3. **Switch the module off:** in `modules/__init__.py` delete the line
   `'insight_capture', # Insight Wave 1 …`, commit and push (GitHub Desktop), and wait for Render to show Live.
   Stage moves, call analysis and the profile then behave exactly as before Wave 1. A reason sent by an old
   browser tab is ignored, and the move still happens. The picker script returns nothing, so no picker appears.
4. **Full undo:** in GitHub Desktop open **History**, right-click the *Insight Wave 1* commit →
   **Revert changes in commit**, then **Push origin**.

The new tables and the two new columns can stay in the database. Nothing else reads them, so they are harmless.
