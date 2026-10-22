# Profit & Cash (Invoicing → Expenses & profit): deploy

## Before you start
This zip is built on top of the **Job Pulse 5-file zip** (`hirelab_job_pulse_5_files.zip`). If that zip is not live yet, deploy it first (or deploy both together, and use the `index.html` and `modules/__init__.py` from **this** zip). The reason: `modules/__init__.py` here also lists `job_pulse`.

## Files (5)
| File | Change |
|---|---|
| `modules/finance.py` | NEW: all the finance logic and APIs (`/api/finance/...`) plus the `/finance` page |
| `finance.html` | NEW: the Profit & Cash page |
| `modules/__init__.py` | +1 line (`'finance'`) |
| `modules/app_nav.py` | `/finance` gets the ☰ Menu, and "Profit & Cash" is added under Business |
| `index.html` | One script at the end: for admins, Invoicing → **Expenses & profit** shows the new page (with "Open full screen" and "Old view" links). Non-admins still see the old view. |

No new packages are needed: `openpyxl` and `pdfplumber` are already in `requirements.txt`.

## Database (additive only)
- **New columns on `expenses`:** `gst_amount`, `kind`, `payment_mode`, `status`, `client_key`, `client_name`, `mandate_id`, `candidate_id`, `freelancer_user_id`, `receipt_path`, `receipt_name`, `source_key`, `recurring_id`, `paid_date`, `updated_at`.
  - Old rows keep working: GST is treated as 0 and status as paid.
- **New tables:** `finance_settings`, `finance_client`, `finance_invoice_meta`, `finance_recurring`, `finance_budget`, `finance_freelancer_rate`, `finance_insight`.
- **Bill files** are saved in `DATA_DIR/receipts/` on the persistent disk.
- Invoices are only read, never changed. The old `/api/expenses` and `/api/invoices/summary` stay as they are.

## Your settings (already the defaults; change them in ⚙ Setup)
- **GST 18%:** taken from each invoice's own GST rate, which is 18% by default.
- **TDS 10%:** of the invoice value without GST. It can be changed per client.
- **Freelancer commission 15% of the fee:** this is the default for every freelancer login. It can be changed per freelancer (a % or a fixed ₹ per placement), and 0 means no commission.
  - The commission is added automatically as an **unpaid** expense when a freelancer-sourced candidate is invoiced, or joins.
  - It only applies to placements from **the day you first open the page**, so commissions you already paid are not added again. Move "Auto-commission for joinings from" back in Setup if you want older ones added.
- **Monthly target:** ₹30 L (without GST).

## Root causes fixed
1. **Net profit included GST.** "Received" contained 18% GST that belongs to the government. Revenue is now the invoice value **without GST**, and each expense's cost is its amount minus the GST you can claim back.
2. **TDS was not tracked.** A paid invoice with no received amount now counts as invoice − TDS. If a client paid less than that, the invoice is flagged as **short-paid**.
3. **Only all-time totals.** You now get month, quarter and FY views (April–March), custom dates, and a 12-month trend against the target.
4. **"Probable" was always ₹0.** It needed an offered CTC, which nobody fills in at interview stage. It is replaced by the 90-day forecast (unpaid invoices, plus joinings with a CTC) and the "Joined but not invoiced" list.

## What is on the page
- **Overview:**
  - Revenue → direct costs → gross profit → operating costs → net profit, with margins.
  - Profit view vs Cash view.
  - The monthly target with "placements still needed".
  - A 12-month chart that also has a table view.
  - Alerts, and where the money went.
  - An AI summary (DeepSeek, using your numbers only).
- **Expenses:**
  - Add, edit and delete expenses, record GST, and mark them paid or unpaid.
  - Link an expense to a client, freelancer or candidate.
  - Upload a bill and AI fills the form: PDFs use DeepSeek; photos need the Claude key in Settings.
  - Monthly repeating expenses are posted automatically, once per month.
  - Category budgets give an alert at 80% and when crossed.
- **Cash & collections:**
  - The next 13 weeks of money in and out.
  - Unpaid invoices by age (0–30 / 31–60 / 61–90 / 90+ days).
  - **✉ Remind** sends a payment reminder to the client's accounts e-mail. The e-mail is remembered per client, with a count of reminders sent.
  - The "Joined but not invoiced" list.
- **GST & TDS:** GST by month (output − input = estimated payable) and a TDS register per client per FY, to match with Form 26AS.
- **Who makes money:** profit per client (with average payment days and late money), per placement and per person, plus job-board cost per hire.
- **Excel for CA:** one file with P&L, Invoices, Expenses, GST and TDS sheets for the selected period.
- **Access:** admins only (company admin or platform admin).

> All GST and TDS figures are management estimates. Check them with your CA before filing.

## Deploy
1. Take a backup first: Backup page → Download database.
2. Copy the 5 files, then commit and push. Render redeploys.
3. In the logs, look for `[modules] mounted: finance`.
4. Open **Invoicing → Expenses & profit**, or `/finance`. Check ⚙ Setup once: add the client accounts e-mails, monthly repeating costs (rent, salaries, tools) and budgets.

## Rollback
- **Full rollback:** remove `'finance'` from `modules/__init__.py`, or redeploy the previous commit. The Expenses & profit tab then shows the old view by itself, because the script checks that the finance API is there. For an exact restore, also put back the previous `index.html`.
- **Data:** the new columns and tables stay unused. Old expenses are untouched.
