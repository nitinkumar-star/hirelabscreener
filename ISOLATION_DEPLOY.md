# Data isolation fix — deploy notes (Oct 2026, build 32)

Goal: no agency can see or change another agency's data. Recruiter and
freelancer isolation inside one company were already clean and are unchanged.

## What changed

| # | Problem found in the audit | Fix |
|---|---|---|
| 1 | Another agency could read a candidate by id (AI compose, interview message, e-mail history) | `modules/tenant_guard.py`: every id in an `/api/...` URL must belong to the caller's company, else 404. The three endpoints were also fixed directly. |
| 2 | Another agency could read/overwrite a job's e-mail templates and switch off WhatsApp learned replies | Same guard + direct fixes in `server.py` and `modules/wa_agent.py` |
| 3 | "Ask AI about my data" (`/api/ai/stats`) sent EVERY agency's candidates to the AI | Now only the caller's company (and a recruiter's own jobs) |
| 4 | Any agency admin could change platform-wide settings (where public applications go, AI keys, e-mail-agent knowledge base) | Global settings are written by the platform owner only; agencies' own settings save as before |
| 5 | A new agency inherited the owner's old global settings (Gmail + app password, signature, GSTIN, knowledge base) | Only the owner's company falls back to global values; other agencies get only harmless platform defaults (AI search config, day thresholds) |
| 6 | `/api/settings` readable without login | Login required |
| 7 | Platform AI maintenance (queue / vectors / JD status and rebuild) open to every agency | Platform owner only (`/api/ai/reindex` and `index-status` stay per company) |
| 8 | Suspended agency / ended trial kept working on an open session for up to 31 days | Checked on every request: suspended -> logged out; ended trial -> 402 (sign-in and billing stay open) |
| 9 | Meta WhatsApp webhook accepted unsigned posts | Signature checked when `META_APP_SECRET` is set |

No database tables or columns were added, changed or deleted. Email-Agent
knowledge-base keys and `sig_website` are now per-company keys; the owner keeps
reading the existing global values until they save new ones.

Files: `server.py`, `modules/tenant_guard.py` (new), `modules/__init__.py`,
`modules/wa_agent.py`, tests `test_isolation_deep.py` (new, 22 tests),
`test_recruiter_access.py`, `test_mail_identity.py`.

## Deploy (one stage)

1. Upload the files (or the full build zip) to GitHub; Render redeploys.
2. In the Render log look for `[modules]` lines without errors.
3. Check, logged in as yourself:
   - Home, Jobs, a candidate profile, Settings page and Email Box open as before.
   - Settings -> save something small -> it stays saved.
4. Open `https://<your-app>/api/settings` in an incognito window: it must say
   `auth_required` (it used to show your settings).
5. Optional: if you use the Meta WhatsApp Cloud API, add env `META_APP_SECRET`
   (Meta app -> Settings -> Basic -> App secret).

If another agency is already live on the platform: its Email Box and outgoing
mail stop using your Gmail. Its admin must enter their own Gmail + app password
in Settings. That is the intended fix.

## Rollback

- Fast switch (no redeploy): set env `TENANT_GUARD=off` on Render. This turns
  off the central guard only; the direct fixes and settings changes stay.
- Full rollback: redeploy build 31 (`hirelabscreener-main_31.zip`). Nothing in
  the database needs undoing.

## Test

    pytest -q                          # 211 tests
    pytest test_isolation_deep.py -q   # isolation audit only (~1.5 min)
