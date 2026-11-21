# BD workspace → Calling (cold-calling sheet) — deploy notes (Nov 2026)

A new page in the BD workspace: **Clients → BD workspace → Calling**. Use it as a
cold-calling sheet. One screen covers the whole loop: call, understand the
business, note it, e-mail, follow up.

## What the user gets

| Part | What it does |
|---|---|
| Sheet | The prospects of the open BD workspace as rows: Company, Contact, Designation, Phone, E-mail, Status, Last outcome, Next step, E-mail status, Notes score. Type straight into a cell; Enter or leaving the cell saves. If a row has no contact yet, typing a name, phone or e-mail creates one. |
| Today | Callbacks that are due (and overdue) come first. Then prospects who replied to your e-mail, then those who opened it, then prospects not called yet. Other views: Callbacks, Hot, Not called, All, one status. |
| Outcomes | One click or key 1–7: No answer, Callback, Interested, Send details, Meeting, Not interested, Wrong number. Each one logs a call on the company timeline and sets the next step (see below). |
| Shortcuts | ↑/↓ move, Enter open, C call, E e-mail, N notes, 1–7 outcome, Space select, / search, Esc close. |
| Call | 📞 opens the phone number (tel:). 📱 "On phone" rings it through the HireLab Dialer app (`/api/push-call`). |
| Business understanding | A short form per prospect: what they make, plants/offices, size, roles they hire, hiring pain, who decides, agency used today, why now, notes. Dots in the sheet show how much is filled in. |
| ✨ Research | AI reads the company website (home, about and careers pages) and your Learning page notes, then fills only the empty fields. Industry and city are filled if empty. |
| 🎤 Speak | Voice typing in Chrome/Edge (en-IN, works with Hinglish), or type instead. AI puts what you said into the right fields. It can also create the contact you mention. |
| 🎙 Call opener | A 30-second opener, 2–3 questions to ask, and answers to "we don't use agencies", "your fee is high" and "no hiring". Saved per prospect. |
| ✉ E-mail | AI writes 3 versions (short, value-led, candidate-led), each with 3 subject lines. It uses the notes, the call notes and your real placements (see below). Pick a version, edit it, then **Send now** or **Queue**. A "Send details" outcome opens this window straight away. |
| Bulk | Tick rows → ✉ Bulk e-mail → AI writes one personal draft per prospect in the background → 📝 Review drafts: edit, Ctrl+Enter approves, Ctrl+S skips. Approved e-mails go out one by one. |
| Follow-ups | No reply after 3 and 7 days (your setting): a short follow-up goes out in the same e-mail thread with "Re:". Follow-ups stop when the prospect replies, when you log Interested, Meeting, Not interested or Wrong number, or when you press Stop. |
| Replies | The Gmail sync already brings the inbox in. A message from the prospect after your e-mail counts as a reply: follow-ups stop, the bell rings ("X replied to your e-mail"), and the prospect moves to the top of Today. |
| Opens | Each e-mail carries an open pixel, so "Opened 3×" shows in the sheet and in Hot. This is approximate, as with every e-mail tool: some mail apps block images. |
| Targets | Calls today and e-mails today against your daily targets (⚙). |
| Import | Paste rows from Excel or Google Sheets, or a CSV. Headings are optional. Check shows new rows, rows for an existing prospect, and rows for an existing client (skipped). Prospects and contacts are created in the open workspace. |
| ✨ E-mail guess | The likely address from the contact's name and the company website. If a colleague at that company already has an address, their pattern is used first (⭐). Guesses are not verified. |

## Next-step rules

| Outcome | Next step |
|---|---|
| No answer | Try again in 2 hours. After every third miss, the next try is the next morning at 11. |
| Callback | A BD follow-up task at the chosen time. It shows in BD Home and Tasks. |
| Interested | Optional date. When a date is set, a follow-up task is created. |
| Send details | Opens the e-mail window. Next step: follow up in 2 days. |
| Meeting | A date is required. Creates a BD meeting activity. |
| Not interested | A reason is required. The prospect comes back on Today in 1, 2, 3 (default), 6 or 12 months. |

A new outcome closes the previous open callback task.

## What the AI may say

- **Agency numbers:** the e-mail may only use numbers counted from the ATS — total placements (stage Placed / Joined / Offer accepted), jobs and clients worked on, placements in the same industry, and similar roles placed. The window shows these facts under the versions.
- **Style:** the sender's last sent BD e-mails are given as style samples.
- **CTC and names:** CTC is never used. Candidate names are never invented.

## Sending

- **Mailbox:** e-mails go from the sender's own mailbox (Settings → Email) through the normal ATS pipeline: signature added, copy kept in the Email Box, logged on the company timeline.
- **Queue:** queued and approved e-mails go out Monday to Saturday between the start and end hour, at least the set gap apart (default 150 s plus a random part), and up to the daily cap (default 60).
- **Mailbox problems:** if the mailbox is not set up or Gmail refuses the login, a queued e-mail waits and retries after 15 minutes.

## Access

- **Who:** BD rules apply. Admins only for recruiter accounts, never freelancers, and no BD in corporate workspaces.
- **Other agencies:** the company guard covers `/api/bd/calling/companies/<id>` and `/api/bd/calling/emails/<id>`; another agency gets 404.
- **Drafts:** each user sees and approves only their own drafts.
- **Website reading:** research refuses private and internal addresses (localhost, 10.x, 192.168.x, link-local, and so on).

## Database (additive only)

- **New tables:**
  - `bd_call` — one row per prospect: calling status, next step, understanding, opener, e-mail and sequence state.
  - `bd_outreach` — every cold e-mail and follow-up: draft, queued, sent, opens, reply.
  - `bd_call_settings` — per-user targets and sending settings.
- **Reused tables:** calls, follow-ups, meetings and e-mails also go into the existing `crm_activities`.
- Nothing is renamed or removed.

**Files:**
- `modules/bd_calling.py` (new)
- `bd-calling.js` (new; served at `/bd-calling.js`)
- `modules/__init__.py`
- `modules/tenant_guard.py`
- `index.html`: Calling page in the BD navigation, `bd_reply` bell
- `test_bd_calling.py` (new, 10 tests)

**Public URL:** `/bdm/o/<token>.gif`, the open pixel (signed token).

## Deploy

1. Merge and let Render redeploy. No new environment variables.
2. Check:
   - BD workspace → Calling opens.
   - ⬆ Import a few rows.
   - Log "No answer" on one row and "Callback" on another; the callback appears on Today and in BD Tasks.
   - Write an e-mail to your own address and send it. "✓ Sent" shows in the sheet, and the e-mail is in your Gmail Sent folder.
   - Reply to it from that inbox. Within a few minutes (after the Gmail sync) the row shows "↩ Replied" and the bell rings.
3. AI features need the DeepSeek key, which is already used elsewhere in the ATS.

## Rollback

Remove `'bd_calling'` from `modules/__init__.py` and the Calling entry from `_BDW_PAGES` in `index.html`. The new tables can stay; nothing else reads them.

## Test

    pytest test_bd_calling.py -q      # this feature
    pytest -q                         # everything
