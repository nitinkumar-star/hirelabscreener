# Oct 28 2026 update — deploy & rollback

All changes are additive (two new columns/tables, three new modules). No data is deleted or rewritten.

## Files

| File | What changed |
|---|---|
| `modules/final_result.py` (new) | Final Result: resume evidence + call evidence → one decision (Pitch tab) |
| `modules/mail_guard.py` (new) | Per-job "Automated e-mails" switch (new column `mandates.auto_mail_off`) |
| `modules/linkedin_capture.py` (new) | Extension 1-click LinkedIn → Central DB / BD |
| `modules/__init__.py` | registers the three new modules |
| `modules/pitch.py` | call script switched off (`SCRIPT_IN_PITCH = False`), checklist-only generation |
| `modules/segments.py` | segmentation rules fixed + `RULES_VERSION` (everyone is re-classified on the next scan) |
| `modules/jobmail.py`, `modules/reengage.py`, `modules/newsletter.py` | respect the per-job e-mail switch |
| `modules/bd_records.py` | `POST /api/bd/companies/quick` — create a company while adding a person |
| `server.py` | campaigns + Email Agent respect the per-job e-mail switch |
| `index.html` | Pitch tab (Final Result, checklist only), Mandate Setup switch, one-line pipeline list, BD "Create company", BD sheet column navigator |
| `test_oct28_changes.py` (new) | 18 tests for all of the above |

## Deploy — one stage at a time

**Stage 1 — backend (safe on its own):** upload `modules/` files + `server.py` via github.com → "Add files via upload". Wait for Render to go live.
Check: open `/api/mandates/<any job id>/auto-mail` while logged in → `{"ok": true, "off": false, ...}`.

**Stage 2 — UI:** upload `index.html`. Hard refresh (Ctrl+Shift+R).
Check: Mandate Setup → Manage → "Automated e-mails" card; candidate → Pitch tab; BD → People → + New → type a new company name.

**Stage 3 — Chrome extension v2.3.0:** chrome://extensions → remove the old HireLab Assistant → "Load unpacked" → the new folder.
Chrome asks for linkedin.com access once (needed for the 1-click read).

The Talent Pool re-classifies everyone automatically within ~10 minutes of Stage 1 (or press Rescan on /talent-pool).

## Rollback

* Any one feature: remove its name from `modules/__init__.py` (`final_result`, `mail_guard`, `linkedin_capture`) and redeploy.
* Call script back in the Pitch tab: set `SCRIPT_IN_PITCH = True` in `modules/pitch.py`.
* Everything: re-upload the previous `server.py`, `index.html` and `modules/` files. The new column / table stay unused and harmless.
* Extension: load the old v2.2.0 folder again.
