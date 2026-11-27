# Memory — Wave 1 (backend) deploy notes (Nov 2026)

Goal: a shared business memory inside the ATS. The team uses it in the UI
(Wave 3), and Claude reads and writes it through the MCP connector (Wave 2).
This wave adds only the tables and the REST API. Nothing in the UI changes yet.

## What changed

| File | Change |
|---|---|
| `modules/memory.py` (new) | Tables `memory_notes` + `memory_note_history`, 7 endpoints under `/api/memory` |
| `modules/__init__.py` | `'memory'` added at the end of the module list (1 line) |
| `modules/tenant_guard.py` | `/api/memory/<id>` must belong to the caller's company, else 404 (1 line) |
| `modules/access.py` | `/api/memory/` added to the recruiter guard's "checked inside the module" list (1 line). Without it, every `/api/memory/<id>` call by a recruiter would get a 403. |
| `test_memory.py` (new) | 14 tests |

Not touched: `server.py`, `index.html`, `mcp_server.py`, the RME tables
(`rme_memories` …), `mandate_client_notes`, or the candidate fields
`client_feedback`, `recruiter_feedback` and `general_comments` (they are only read).
SQLite journal mode and synchronous settings are unchanged.

Database: additive only. Two new tables and five indexes are created with
`IF NOT EXISTS` on startup. No existing table or column is altered.

## API summary

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/memory` | list/search. Filters: `scope`, `entity_id`, `entity_name`, `client_name`, `q`, `importance`, `status`, `include_archived`, `include_expired`, `limit` (default 50, max 200). Sorted by importance (high to low), then most recently updated. |
| GET | `/api/memory/<id>` | one note, plus `history` and `can_edit` |
| POST | `/api/memory` | create. `dedupe: true` updates an active note with the same scope and entity and a near-identical title (85% or more alike) instead of adding a new one. |
| PUT | `/api/memory/<id>` | update only the fields sent. A history row is written when something changes. |
| POST | `/api/memory/<id>/archive` / `/unarchive` | soft delete or restore. Notes are never hard-deleted. |
| GET | `/api/memory/context?client=` / `mandate_id=` / `candidate_id=` | one briefing (see below) |

Scopes: `general`, `client`, `mandate`, `candidate`, `person`, `team`.
`entity_id` links to `crm_clients` / `mandates` / `candidates` / `crm_contacts` / `users`
of the same company. Any other id returns 404. When a note is linked,
`entity_name` and `client_name` are filled in automatically.
`client_name` (extra column) ties a person, mandate or candidate note to a
client, so the note appears in that client's briefing.

What the context briefing returns:
- **candidate**: the candidate's notes and its 3 feedback fields; its mandate's notes and the old "client notes" on that job; the client's notes and the client-side people's notes; the agency's high-importance general notes.
- **mandate**: the mandate block above, plus the notes on its candidates, plus the client block.
- **client=** (partial name match): client and people notes, plus every matching mandate with its notes.

Who sees what:
- Admins see the whole company.
- A recruiter can **read** general and team notes, their own notes, notes on their assigned jobs and those jobs' candidates, and the client/person notes of the clients behind those jobs.
- A recruiter can **write** general notes and notes on their assigned jobs and candidates. Client, person and team notes are admin-write only.
- Freelancers cannot reach `/api/memory` at all, because of the existing allow-list.

## Verification done

- `python -m py_compile` passed for `server.py`, `modules/memory.py`, `modules/access.py` and `modules/tenant_guard.py`.
- `pytest`: 469 passed, 1 skipped. The baseline before this change was 455 passed, 1 skipped; the 14 new tests account for the difference, and no existing test changed result.
- No JS changed in this wave, so there is no `node --check` step.

## Deploy (one stage)

1. In GitHub Desktop, commit the 5 files above and push. Render redeploys.
2. In the Render log, look for `[modules] mounted: memory` with no
   `migration failed for migrate` line after it.
3. Log in as admin, then open these URLs in the same browser:
   - `https://hirelabscreener.onrender.com/api/memory` should show `{"ok": true, "count": 0, "notes": []}`.
   - `https://hirelabscreener.onrender.com/api/memory/context?mandate_id=<any job id>` should show `"type": "mandate"`.
4. Optional write test, from the browser console (F12) on the app page:
   ```js
   fetch('/api/memory',{method:'POST',headers:{'Content-Type':'application/json'},
     body:JSON.stringify({scope:'general',title:'Test note',content:'Wave 1 test'})}).then(r=>r.json()).then(console.log)
   ```
   Then archive it with `fetch('/api/memory/<id>/archive',{method:'POST'})`.
5. Open the URLs from step 3 in an incognito window. They must return `auth_required`.

## Rollback

- Redeploy the previous commit in GitHub Desktop (revert this commit, then push), or use Render's "Rollback" to the previous deploy.
- The two new tables stay in the database. They are harmless, nothing else reads them, and a later redeploy of this wave reuses them with the notes intact.
- Nothing else needs undoing. The three one-line guard and registry edits revert together with the commit.
