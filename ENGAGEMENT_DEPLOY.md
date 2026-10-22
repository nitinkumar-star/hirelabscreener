# Candidate Engagement (Waves 1–4): one-time deploy guide

This single deploy adds four connected features to HireLab Screener:

| Wave | Feature | Page | Who |
|---|---|---|---|
| 1 | **Job-Match Mail**: e-mail a job to AI-matched candidates (vector search + filters + AI check + your approval). "Interested" puts the candidate on the job and creates a call task. | `/jobmail` + "✉ E-mail top matches" on every job's Matching tab | Admins + recruiters (their own jobs) |
| 2 | **Talent Pool**: automatic segmentation (industry / function / seniority / region), CV-freshness tracking, a "what changed" log of every profile update, candidate-chosen interests, and a re-engage reminder | `/talent-pool` + 🏷 chip on the profile | Admins |
| 3 | **Market Pulse**: a monthly newsletter to segments, AI-drafted from your points, with a personal "refresh your CV" box in every mail | `/newsletter` | Admins |
| + | **Backup**: download the complete database (.db.gz) and all CVs (.zip) — replaces the broken JSON export | `/backup` + ⋯ menu → "Full backup" | Platform admin only |
| + | **Editable e-mail text**: Job Mail (subject, opening, question, closing line, per job, plus "save as default") and Re-engage (subject + text of the job / Central DB / reminder mails), with {placeholders} | Job Mail → "✎ Edit the e-mail text"; Re-engage → "✎ Edit text" | Job Mail: anyone who can send it; default text + Re-engage: admins |
| + | **☰ Menu on every page**: the full app menu on every separate page (Job Mail, Talent Pool, Market Pulse, Hot List, My Calls, Re-engage, Scheduler, Org Map, Backup, Storage) | top-left "☰ Menu" | Role-aware |
| 4 | **Hot List**: an engagement score per person and a "who to call today" list. Assign it to callers, including freelancers, who log outcomes on `/my-calls`. | `/hotlist`, `/my-calls` + 🔥 line on the profile | Admins; callers see only their own calls |

**One rule across all mails:** a person gets at most one HireLab mail in 7 days. The priority is Job mail > Re-engage > Newsletter. Change the gap on the Job Mail page (⚙).

---

## Before you deploy
- [ ] **Backup for THIS deploy:** Render takes an automatic snapshot of the disk every 24 hours and keeps it for at least 7 days (Render dashboard → your service → **Disks**). If anything goes wrong, restore yesterday's snapshot there. (The old in-app "Export JSON" fails on large / AI-matched databases; `Backup.bat` only works for a local Windows install.)
- [ ] **From now on:** after this deploy, use the new **Backup** page (`/backup`, or ⋯ account menu → "Full backup") to download the full database and all CVs to your own computer.
- [ ] These keys should already be set (they are used today): `DEEPSEEK_API_KEY`, `JINA_API_KEY`.
- [ ] The company mailbox is set in Settings → Email Configuration (Gmail + App Password).

## Deploy (one stage)
1. Replace the repo contents with this zip, then commit and push. Render redeploys automatically.
2. Check the startup logs. All of these lines should appear:
   ```
   [modules] mounted: jobmail
   [modules] mounted: segments
   [modules] mounted: newsletter
   [modules] mounted: hotlist
   [jobmail] background thread started
   [segments] background thread started
   [newsletter] background thread started
   ```
3. The sidebar now shows **Job Mail**, **Talent Pool**, **Market Pulse** and **Hot List** (the last three are admin only).

The database changes happen automatically on startup and are additive only:
- **12 new tables:** `jobmail_*` ×3, `talent_*` ×3, `newsletter_*` ×3, `hotlist_*` ×2, `reengage_text`
- **New columns:** `reengage_settings.reminder_days`; `subject_tpl`, `intro_tpl`, `ask_tpl`, `after_tpl` on `jobmail_campaigns` and `jobmail_settings`

Nothing existing is changed or removed.

## Smoke test after deploy (about 15 minutes, in this order)
1. **Talent Pool:** open it. The first open runs a scan of the whole database, which can take a few seconds. Check the numbers, then click a matrix cell. Open any candidate and check the 🏷 chip, then Edit it and click "Back to automatic".
2. **Job Mail:** open a job that has a JD → Matching Candidates → **✉ E-mail top matches**. Run the AI check, then **Send a test to me** and check your inbox. Approve only **one** candidate (ideally your own test profile), then **Send 5 now**. In the mail, click **Yes, I'm interested** → Confirm. The candidate should appear on the job at *Interested*, and a call task should be created.
3. **Update-profile link:** in that same thank-you page, click "Update my CV & details". Check that the "What would you like to hear about?" section shows, then submit it. Talent Pool → "Recently updated profiles" should show what changed.
4. **Market Pulse:** create a New issue and pick a small segment (one industry + one region). Paste 3–4 points → **Draft with AI** → edit → **Send a test to me**. Approve the small segment only when you are happy with it.
5. **Hot List:** set Min score to 10. You should see the test candidate from steps 2–3. Assign it to yourself → **My calls** → log an outcome → check the candidate's journey.
6. **Re-engage page:** the new "Reminder if not updated" setting should be visible (default: after 7 days).
7. **Menu:** on any separate page click **☰ Menu** → **Tasks** → the app opens on Tasks.
8. **E-mail text:** Job Mail → "✎ Edit the e-mail text" → change the question → the preview updates. Re-engage → "✎ Edit text" → Save → preview updates; "Reset to original" puts the old wording back.
9. **Backup:** ⋯ menu → **Full backup** → **Download full database**, then **Download all CVs**. Keep both files on your laptop / Google Drive.

## Settings worth checking once
| Where | Setting | Default |
|---|---|---|
| Job Mail ⚙ | mails/day · gap between any two HireLab mails · min match % · people per job | 40 · 7 days · 70% · 30 |
| Talent Pool ⚙ | CV counts as fresh for · AI classification (off) + daily cap | 6 months · off · 300 |
| Market Pulse ⚙ | mails/day · sending hours | 150 · 10 AM–7 PM |
| Re-engage | reminder if not updated | 7 days |

**Mail volume:** Gmail / Workspace allows about 2,000 mails a day per mailbox in total, and that includes your normal mail. The defaults add up to about 240 a day. If you plan to mail a database in the tens of thousands, move bulk sending to a service such as Brevo or Amazon SES first.

## Rollback
**Soft (no redeploy):**
- Job mail and newsletter: nothing is ever sent without your approval. Use **Stop sending** on a running one.
- Re-engage reminder: Re-engage page → "Reminder if not updated" → No reminder.
- AI classification: Talent Pool ⚙ → untick AI (it is off by default).

**Full:** redeploy the previous commit (`git revert` + push, or Render → "Rollback to previous deploy").
- The new tables and columns stay in the DB unused and harmless, and the old code ignores them.
- Tasks and journey notes that were already created remain as normal tasks and notes.
- No data cleanup is needed.

## Files in this release
**New:**
- `modules/jobmail.py`, `modules/segments.py`, `modules/newsletter.py`, `modules/hotlist.py`, `modules/backup_download.py`, `modules/app_nav.py`
- `jobmail.html`, `talent-pool.html`, `newsletter.html`, `hotlist.html`, `my-calls.html`
- `test_jobmail.py`, `test_segments.py`, `test_newsletter.py`, `test_hotlist.py`, `test_backup_download.py`, `test_email_text_and_menu.py` (31 tests)

**Changed (additive edits only):**
| File | Change |
|---|---|
| `modules/__init__.py` | 6 module names added |
| `modules/access.py` | `/api/jobmail/` added to the recruiter guard list; the module checks job access itself |
| `modules/freelancer.py` | freelance accounts may use `/api/hotlist/mine`, which returns only calls assigned to them |
| `modules/reengage.py` | frequency guard, durable update-log clock, reminder, editable e-mail text (new table `reengage_text`) |
| `reengage.html` | reminder setting, "✎ Edit text", Reminder preview, table no longer overflows on phones |
| `update-profile.html` | optional interests section |
| `index.html` | 4 sidebar links, "E-mail top matches" button, profile chips, calls button, "Full backup" in the account menu, `/?go=<item>` so the ☰ Menu opens the app on the right view |

`server.py` is not touched.

## Test status
- All 31 new tests pass, and all existing test files pass except one.
- The one failure is in `test_recruiter_access.py::test_recruiter_daily_work` (`/api/emailbox/list` returns 200 where the test expects 403). It fails the same way on the original uploaded code, so it is unrelated to this release.
