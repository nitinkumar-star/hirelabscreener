# HireLab — Commercial launch readiness

Status as of Nov 2026. Goal: open HireLab to other agencies **without new monthly spend**,
and know exactly what to switch on (and what it costs) when paying users arrive.

## 1. Already in place (no action needed)

| Area | What exists |
|---|---|
| Sign-up | Public sign-up → owner approves (`Admin Panel → Pending users`). Terms + Privacy consent is recorded. Owner now gets an **e-mail alert** for every new sign-up. |
| Tenant isolation | Every agency is a separate company; `modules/tenant_guard.py` blocks cross-agency ids on every `/api` call; recruiter scope in `modules/access.py`. Covered by `test_isolation_deep.py`. |
| Passwords | PBKDF2-SHA256 (210k rounds), old hashes upgraded on sign-in, reset links valid 1 hour, single use. |
| Abuse limits | Login: 8 failures / 15 min per IP. **New:** sign-up 5 / hour, password reset 5 / 15 min per IP. Public apply / parse endpoints are limited too. |
| Trial & billing | Free trial (days set in Billing configuration), per-recruiter price, AI usage billed at cost × markup, GST invoice, manual payment (UPI / bank) with invoice number, suspend / activate, user limit, monthly AI token cap per agency. |
| Trial end | **New:** one rule for sign-in and every request — trial date passed **and no payment ever recorded** → a clear "Your free trial has ended" screen with your payment instructions. A paying agency is never locked out by an old trial date. Agency admins see a reminder in the last 5 days. Owner can add trial days (`+ Trial days`). |
| Legal pages | `/terms`, `/privacy`, `/refund` (Indian law, processor/controller wording). |
| Security headers | **New:** `X-Content-Type-Options`, `Referrer-Policy`, HSTS on HTTPS, `X-Frame-Options` on the signed-in app (public careers / apply pages stay embeddable). `/api/health` no longer shows server paths. |
| Backups | Automatic on-disk backups + `/backup` page to download the database and CVs. |
| Products | Agency ATS and Corporate ATS per tenant (`Product` selector in Billing). |

## 2. Do once before inviting the first outside agency (free)

1. **Render → Environment** — set these:
   - `SECRET_KEY` — a long random value (keeps everyone signed in across deploys).
   - `PUBLIC_BASE_URL` — e.g. `https://hirelabscreener.onrender.com` (or your domain later). Reset links and e-mails use it instead of trusting the request's host.
   - `ALLOWED_EXTENSION_IDS` — your Chrome extension id (from `chrome://extensions`). Without it any extension is trusted.
   - `DEEPSEEK_API_KEY` — keep AI keys in the environment, not only in Settings.
2. **Settings → Billing configuration**
   - Price per recruiter, trial days, GSTIN (when you have one), legal name, address.
   - **How agencies pay** — your UPI id / bank details. This is shown on the agency's billing card and on the trial-ended screen.
3. **Your own e-mail** must be saved on the owner account (Admin Panel → users) so sign-up alerts reach you.
4. **E-mail sending** (Settings → Gmail address + app password) — used for password-reset and sign-up alert e-mails. A Gmail app password is free.
5. **Make the GitHub repo private** and download `/backup` weekly to your laptop / Google Drive.
6. Set an **AI token cap** for every new agency (Billing → Token cap) so one agency cannot run up your AI bill.

## 3. Switch on when you have paying users (costs start here)

| When | What | Approx. cost |
|---|---|---|
| First paying agency | Custom domain (e.g. `app.hirelabtalent.com`) → set `PUBLIC_BASE_URL` | ₹800–1,000 / year |
| Card / UPI auto-pay wanted | Razorpay (payment links or subscriptions); no monthly fee, ~2% per payment | % of revenue only |
| ~8+ agencies | Render disk 1 GB → 5–10 GB as CVs grow; consider a paid instance with more RAM | a few $ / month |
| Revenue > GST threshold | GST registration → fill GSTIN in Billing configuration | CA fee |
| Off-site backup automation | Daily copy of the DB + CVs to Google Drive / S3 | free (Drive) – ₹100/mo |
| Many concurrent users (50+) | Move SQLite → Postgres (code is single-file SQLite today) | managed Postgres ~$7+/mo |

## 4. Known limits (fine for the first 8–30 users)

- Single Render instance, SQLite, 1 worker × 8 threads: comfortable for tens of recruiters.
- Rate limits are in memory (reset on deploy) — fine on one instance.
- Payments are recorded manually by the owner (`Mark Paid`); there is no card auto-charge yet.
- Data export for an agency: candidates CSV (Talent → Export). A full self-serve "export / delete my agency" is handled on request (Privacy Policy §6).
