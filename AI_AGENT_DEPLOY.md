# Candidate AI Agent — Phase 1 — deploy notes (Oct 2026)

A new **✨ AI Agent** tab on the candidate profile, plus a one-line
"Tell the AI agent…" box at the top of the Overview tab.

The recruiter writes the work in plain words, e.g. *"Please drop a follow-up
e-mail for the JD at 11 AM tomorrow"*. The agent reads the candidate's profile,
CV, job + JD, stage history, e-mails, WhatsApp messages and interviews. It then
shows what it understood and one card per action for the recruiter to check
and edit.

| Action | What happens |
|---|---|
| E-mail | Approve -> sent automatically at the time on the card (or **Send now**). Sent from the approving recruiter's own mailbox when set up, else the company mailbox, with the usual signature. Threaded as a reply when it follows an earlier e-mail. |
| WhatsApp | Never sent automatically. Approve -> at the chosen time the recruiter gets a notification ("WhatsApp draft ready"). On the card: **Send via WhatsApp Business** (when the Business API is connected) or **Open WhatsApp** + **Mark as sent**. |
| Reminder | Becomes a task in Tasks for the recruiter, due at the time given. |
| Answer | A question about the candidate ("notice period?") is answered on the spot. Nothing is sent. |

## Rules

- **Time:** the time in the instruction is used. With no time, the card is
  pre-set to the next working day (Mon-Sat) at 10:00 AM IST and says so. The
  recruiter can change it before approving. A time that has passed is refused.
- **Re-check before a scheduled e-mail:** if the candidate replied (e-mail or
  WhatsApp) after the approval, the e-mail is **held** and the recruiter is
  notified. They can then **Send anyway** or **Cancel**. Unsubscribed
  candidates are never e-mailed.
- **Who:** all recruiters, on the candidates they can already open (the
  existing recruiter access rules and company isolation apply to every call).
- **Records:** everything sent is logged in the candidate journey. E-mails
  also appear in Communications. Notifications open the AI Agent tab.
- **Privacy:** phone numbers and e-mail addresses are masked before any text
  goes to the AI. The server fills in the recipient itself.

## Needs

- A **DeepSeek API key** (Settings -> AI keys). The tab shows a warning when
  it is missing.
- E-mail set up as today (company Gmail, or the recruiter's own Gmail in
  their profile).
- Optional: WhatsApp Business API env vars (`WA_ACCESS_TOKEN`,
  `WA_PHONE_NUMBER_ID`). Without them, WhatsApp opens on the recruiter's
  phone or WhatsApp Web.

## Database (additive only)

One new table: `cand_agent_tasks`. Nothing else changes. A background
thread (60 s tick) sends the e-mails and WhatsApp reminders that are due.

Files: `modules/cand_agent.py` (new), `modules/__init__.py` (registers it),
`index.html`, test `test_cand_agent.py` (8 tests).

## Deploy

Same as every build: merge `claude/eager-hypatia-bhfwli` into `main` (or
upload the files above); Render redeploys and creates the table on start.

## Check after deploy

1. Open a candidate who has an e-mail address -> **✨ AI Agent**.
2. Type "Send a follow-up e-mail about the JD in 5 minutes" -> Run.
3. Check the card, press **Approve & schedule**. Within about a minute of
   that time the card shows **Sent**, and the e-mail is in Communications.
4. Type "WhatsApp him now to confirm interest" -> **Get it now** ->
   **Open WhatsApp**.

## Rollback

Redeploy the previous build. Scheduled agent e-mails then simply stop being
sent; the table is ignored.

## Next (Phase 2 / 3, not in this build)

Follow-up chains that stop when the candidate replies, an "Agent inbox" page
across all candidates, stage moves, interview scheduling, update requests
and call briefs.
