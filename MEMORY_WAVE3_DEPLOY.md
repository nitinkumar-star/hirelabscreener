# Memory — Wave 3 (UI) deploy notes (Nov 2026)

Goal: the team can see, add, edit and archive Memory notes in the ATS, on the
same notes Claude reads and writes. This wave needs Waves 1 and 2 live first.

## What changed

| File | Change |
|---|---|
| `memory-ui.js` (new) | The whole Memory UI (`window.MemUI`): Memory page, side drawer with history, New/Edit form, candidate and job Notes tabs, CRM client card. It uses one delegated click listener (`data-act="mem-…"`) and no inline handlers. Every value is escaped, and colours come from the app's `:root` tokens. |
| `modules/memory.py` | +1 route: `GET /memory-ui.js`, served the same way as `skill-engine.js` |
| `index.html` | Small hooks only (23 lines added, 8 changed; no existing behaviour changed): <br>• the script tag <br>• a "Memory" sidebar item under Tasks <br>• an empty `#memory-view` container <br>• `memory-view`, `sb-memory` and `#memory` added to the view manager and URL hash <br>• job page: 7th tab "Notes" (`#mandate/<id>/notes`) <br>• candidate profile: "Notes" tab <br>• CRM client: "Memory notes" card at the bottom of the left column |
| `test_memory.py` | +2 tests: the script is served without inline handlers, and every `index.html` hook is present |

### What the user sees

- **Memory page** (sidebar → Memory, or `#memory`):
  - search box, scope and importance filters, "Show archived" / "Show expired";
  - columns: Entity, Scope, Title, Importance, Source, Updated;
  - clicking a row opens a side drawer with the full note, its history (who changed what and when, plus the before/after text), and Edit / Archive / Restore;
  - "+ New note" opens a form. For a job note, you pick the job from a list.
- **Candidate → Notes:**
  - the candidate's notes and a quick-add box;
  - the 3 feedback fields from the record (read-only);
  - collapsed sections: "Job & client notes" and "Agency-wide, high importance".
- **Job → Notes:**
  - the job's notes, the old client notes on the job, and a quick-add box;
  - notes on this job's candidates;
  - client and people notes.
- **CRM client → Memory notes:** this client's notes, notes on people at the client, and a quick-add box.
- **Recruiters:**
  - they see only what the server allows;
  - the form offers only General and Job scopes, and candidate notes are added from the candidate's profile;
  - Edit and Archive are hidden on notes they cannot change. The server refuses those changes anyway.
- **Mobile:** the table becomes cards, the drawer and form open full-screen, and there is no sideways scrolling. The page is reached through More → Memory.
- **Keys:** Esc closes the drawer or form. Ctrl/Cmd+Enter saves the form or a quick-add box.

## Verification done

- `python -m py_compile` passed for `server.py`, `modules/memory.py` and `mcp_server.py`.
- `node --check` passed for `memory-ui.js` and for all 18 inline script blocks in `index.html`.
- `pytest` (full suite): 489 passed.
- **Headless Chromium against a seeded local copy**, with an admin and a recruiter, at 1440 px and on a phone (390 px). These all passed:
  - Memory page: search, filters, drawer and history, edit (history row appears), new job note, archive then restore;
  - job Notes tab and quick-add;
  - candidate Notes tab;
  - CRM card;
  - `#memory` deep link, and switching to Tasks hides Memory;
  - recruiter visibility and recruiter scopes;
  - mobile layout with no sideways scrolling.
- Every `/api/memory` call returned 200/201.
- The browser console showed two 403 responses, both from existing Home-screen calls (`/api/org/stats`, `/api/users`) and not from Memory.

## Deploy (one stage — the ATS service only)

1. Merge the branch and push with GitHub Desktop. Render redeploys the ATS.
2. Hard-refresh the app (Ctrl+Shift+R) so the browser loads the new `index.html`.
3. Run the QA checklist below.

The MCP service does not need a redeploy for this wave.

## Manual QA checklist

Admin account:
- [ ] Sidebar shows **Memory** under Tasks. Clicking it opens the page, and the URL ends with `#memory`.
- [ ] A refresh on `#memory` reopens the Memory page.
- [ ] Notes saved earlier (by Claude or the API) are listed. Claude's notes show a "Claude" source badge.
- [ ] Typing in search filters the list. The Scope and Importance filters work.
- [ ] "Show archived" and "Show expired" add the archived/expired notes, with a badge on each.
- [ ] Clicking a row opens the drawer with the full text and the history.
- [ ] Edit → change the text → Save. The drawer shows a new "Edited: text" history line, and "Show the text change" shows before and after.
- [ ] Archive (confirm) → the Restore button appears. The note disappears from the list unless "Show archived" is on. Restore brings it back.
- [ ] "+ New note" → scope Job → pick a job → Save. The note appears in the list.
- [ ] Open a job → **Notes** tab. Quick-add a note and it appears at once. Client notes are shown below.
- [ ] Open a candidate → **Notes** tab. Quick-add works, the feedback fields are shown, and "Job & client notes" expands.
- [ ] Clients (CRM) → open a client → the "Memory notes" card is at the bottom of the left column, and quick-add works.
- [ ] Open other pages (Home, Jobs, Tasks, Clients, Settings). The Memory page never stays on screen behind them.
- [ ] Existing tabs (Pipeline, Candidate Profiles, Communications, Pitch …) work as before.

Recruiter account (e.g. Charanpreet):
- [ ] The Memory page lists only general and team notes, notes on her jobs and their candidates, and those clients' notes.
- [ ] A client or person note opens read-only, with no Edit or Archive buttons.
- [ ] "+ New note" offers only General and Job.
- [ ] The candidate Notes tab and quick-add work on her candidates.

Phone:
- [ ] More → Memory opens the page as cards, and the drawer and form open full-screen.

## Rollback

- Redeploy the previous commit, which is Wave 2. The UI hooks, `memory-ui.js` and its route all go away together.
- The API, the data and Claude's tools are unaffected by a Wave 3 rollback. All notes stay.
- After the rollback, hard-refresh the browser (Ctrl+Shift+R).
