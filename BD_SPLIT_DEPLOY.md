# BD workspace separated from Client CRM — deploy notes (Oct 2026, build 33)

The BD workspace (same place: Clients -> "BD workspace" tab) now holds only
prospects. Client CRM holds only clients. A prospect moves to Clients with
"Convert to client".

## What changed

| Area | Before | Now |
|---|---|---|
| BD workspace: Companies, People | Every company, prospects and clients | Prospects only. "+ New" adds a prospect |
| BD Home, Tasks, Notes | Follow-ups of all companies | Prospects only. A client's follow-ups stay on the client's own page |
| BD Opportunities (deals) | Any company | Open deals: prospects only. Won/lost deals stay as BD history |
| Client CRM list, job client picker, invoice client picker | Every company | Clients only |
| Convert | Not available (a won deal only set status to Active) | "Convert to client" button on the BD side panel and on the company page. Contacts, e-mails, notes, follow-ups and files move along (same record). Open deals are marked won. A deal set to Won converts automatically |
| Undo | — | "Move to BD" on a client page (admin). Refused when the client already has a job or an invoice |
| Duplicates | Checked | Still checked across both lists; the message says where the company already is |
| E-mail to a prospect | Company page | Same company page (the BD panel's "Open full page" button), with AI drafts |

## Database (additive only)

- New columns on `crm_clients`: `record_type` (`prospect` / `client`, default
  `client`), `converted_at`, `converted_by`. Nothing removed or renamed.
- On the first boot after deploy only: companies whose status is "Prospect"
  move to the BD workspace, except ones that already have a job or an invoice
  (those stay in Clients). Every other company stays in Clients.
- Open deals on companies that stay in Clients are no longer listed in BD
  (kept in the database, not deleted).

Files: `modules/crm.py`, `modules/bd_records.py`, `modules/bd.py`,
`server.py`, `index.html`, tests `test_bd_split.py` (new, 7 tests) and
`test_bd_records.py`.

## Deploy (one stage)

1. Upload the files (or the full build zip) to GitHub; Render redeploys.
2. Check:
   - Clients (CRM) lists your clients; no "Prospect" filter any more.
   - BD workspace -> Companies lists your old prospects.
   - Add a test prospect in BD, open it, click "Convert to client", and check
     it appears in Clients with its contacts. Then use "Move to BD" to send it
     back, or delete it.
   - Create a job: the client list shows clients only.

## Rollback

Redeploy build 32 (`hirelabscreener-main_32.zip`). The new columns stay in the
database and are simply ignored by build 32, which shows all companies in
both lists again, as before. No data is lost either way.

## Test

    pytest -q                      # 218 tests
    pytest test_bd_split.py -q     # this feature only
