# Naukri Screener analyses: export and check with Claude

Every **Analyze this page** in the Chrome extension (Naukri search page) is now kept in the ATS for 60 days. Batches from the same recruiter on the same job within 20 minutes count as one analysis.

## Where to export
- **ATS:** open the job, go to **Job Breakdown**, then **Naukri screening analyses**. Each analysis has these buttons:
  - **Copy for Claude**: copies the Markdown, ready to paste
  - **.md**: the Markdown file
  - **CSV**: one row per card, with empty `your_verdict` / `your_note` columns for your own marking
  - **JSON**: everything
- **Claude connector:** Claude can read the analyses itself. Ask it, for example, "Check the last Naukri screening for mandate 97". It finds `/api/screen-runs` with `ats_catalog` and reads it with `ats_read`.
- **Extension:** every answer from `POST /api/extension/screen` now also carries:
  - `analysis_id`
  - `export_url`: a link to the Markdown file

  To get an Export button in the extension panel, add this one line after an analysis finishes:
  ```js
  chrome.tabs.create({ url: response.export_url });   // or: window.open(response.export_url)
  ```

## What the Markdown contains
- how the score is worked out, and the OPEN / MAYBE thresholds
- the job limits (experience, CTC, locations)
- your rules for the job
- the exact instructions the AI got (job, breakdown, rules, past decisions)
- every card:
  - verdict, score and score parts (role_fit, domain_fit, core_fit, company, experience, CTC, location)
  - the facts the program read (experience, CTC, notice, company class)
  - reason and flags
  - whether the person is already in the ATS
  - the card text (phone numbers and e-mails removed)
- a checklist that asks Claude to:
  - validate every verdict
  - check the facts
  - find patterns
  - suggest fixes as recruiter rules, breakdown changes and scoring/prompt changes

## API
| Route | What |
|---|---|
| `GET /api/screen-runs?mandate_id=&limit=` | list of analyses |
| `GET /api/screen-runs/<id>?format=json\|md\|csv&download=1` | one analysis |
| `GET /api/screen-runs/last?mandate_id=&format=` | the latest analysis of a job |

Access rules: own company only. A recruiter sees only the jobs assigned to them.
