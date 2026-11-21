# Insight Engine, Wave 3: Market Intel + Talent Pulse. Deploy & rollback

Wave 3 adds the cross-company view, **Market Intel** (`/market-intel`): what the market pays, who is open to a
move, why people say no and what is happening at companies. It also builds a monthly **Talent Pulse** from those
numbers and opens it as a draft in your existing Market Pulse newsletter. **Nothing is sent automatically.**

**Waves 1 and 2 must be live first.** Wave 3 reuses their data and maths, so the numbers agree with the Company
Talent Map.

## Your decisions (Nov 2026), built in

| Decision | Choice | What it means |
|---|---|---|
| 1. Company names on the client-facing Talent Pulse | **Show** | The client version may say "Siemens: 4 of 6 people we contacted said no". Candidate names, phones and individual CTCs never appear anywhere |
| 2. Recruiters and Market Intel | **Allowed** | Recruiters can open the page, but see only candidates from **their own jobs**. Creating the newsletter draft stays **admin only**, because the newsletter is admin only |
| 3. What "last 3 / 6 / 12 months" means | **Real activity** | A person counts only if something really happened on their record in that period: added, stage move, reason, call analysis, market note or a field edit (audit log). A record merely touched by a background job does not count |

## What you will see

Menu → **Market Intel** (next to Company Talent Map).

| Section | What it shows |
|---|---|
| Filters | Period 3 / 6 / 12 months · domain (LV, MV, HV / GIS, Solar, Data Center …) · companies: all / clients / non-clients |
| Top numbers | People with activity, contacted, % open to a move, % said no, each compared with the previous period of the same length |
| **Talent Pulse** | **Draft this month's Talent Pulse** → 4–5 points, each with its n → **Open in Market Pulse as a draft** (admins) or **Client version (print / PDF)** |
| Salary benchmark | Role family × level × city: median current CTC, median expected CTC, median hike ask %, n, and the companies the sample comes from. Fewer than 3 people shows "n too small". Filter by role family and city. **↓ Salary grid CSV** |
| Market movement | Per company: contacted, interested or later, said no, % said no, previous period, change (▲ worse / ▼ better). Under each company, the city · role family groups of 3+ people, e.g. "Bengaluru · Channel Sales: 4 of 6 said no" |
| Why people said no | Top reasons in the period (previous period in brackets) |
| Market signals | Market notes in the period by signal type, and which companies they are about |

**How Talent Pulse works:** the points are written by code from the numbers, not by AI, so no number can be made
up. Every point needs at least 3 people. The pay point compares the current and expected CTC of the *same*
people, so the two figures and the hike % agree. **Open in Market Pulse as a draft** creates a new draft issue
with the points filled in and opens it. There you choose the audience, press **Draft with AI** if you want, send
yourself a test, and approve, exactly as before.

**City clean-up:** Bangalore / Bengaluru / Whitefield become **Bengaluru**; Gurgaon / Noida / Delhi become
**Delhi NCR**; Navi Mumbai / Thane become **Mumbai**; Secunderabad becomes **Hyderabad**, and so on. When a
location names two cities, the first one wins. Unknown cities are grouped by region, e.g. *Other (South)*.

## Files

| File | What changed |
|---|---|
| `modules/insight_market.py` (new) | Page route, data / CSV / Pulse / draft / switch routes, city clean-up, 10-minute cache |
| `market-intel.html` (new) | The page and the client print version |
| `modules/newsletter.py` | **One new helper**, `create_draft()`, that adds a draft issue with points. Nothing existing changed |
| `modules/__init__.py` | Adds `'insight_market'` (one line, after `insight_company`) |
| `modules/insight_capture.py` | Also clears the Market Intel cache when a reason or market note is saved |
| `index.html` | **Market Intel** menu item (shown when the switch is on) |
| `insight-capture.js` | Settings → Insight capture: new **Market intelligence** switch |
| `test_insight_market.py` (new) | 10 tests |
| `INSIGHT_WAVE3_DEPLOY.md` (new) | This file |

**No new tables or columns.** The switch `insight_market_on` (default **on**) is one row in Wave 1's
`insight_settings` table.

## Deploy (GitHub Desktop → Render)

1. Open **GitHub Desktop** and choose the `hirelabscreener` repository.
2. Copy the files from the table above into the repository folder, replacing the old ones at the same paths.
   `market-intel.html` goes in the top folder, next to `index.html`.
3. Write a summary such as *Insight Wave 3: market intel*, press **Commit to main**, then **Push origin**.
4. Wait for Render to show **Live** (2–4 minutes). The log shows `[modules] mounted: insight_market`.
5. Hard-refresh the app once (**Ctrl + Shift + R**).

## Check it in the live app

1. The menu shows **Market Intel** under Company Talent Map. Open it.
2. Switch the period between 3, 6 and 12 months and the domain to *LV*: the numbers change.
3. In **Salary benchmark**, small groups show "n too small". Press **↓ Salary grid CSV**: the file opens in Excel.
4. **Market movement** lists companies with the previous period alongside.
5. Press **Draft this month's Talent Pulse**: 4–5 points appear, each with its n. Press
   **Open in Market Pulse as a draft**: the Market Pulse page opens on a new **draft** with the points filled in.
   Check the Market Pulse list: the issue says *draft*, and nothing was sent.
6. Press **Client version (print / PDF)**: a clean page with the points and the benchmark rows. Print or save it.
7. Sign in as a recruiter: Market Intel opens with numbers from that recruiter's jobs only, and without the
   draft button.

## Tests

```bash
python -m py_compile server.py modules/*.py
```
```bash
node --check insight-capture.js
```
```bash
python -m pytest test_insight_market.py -q
```
```bash
python -m pytest -q
```

`test_insight_market.py` covers:
- city clean-up
- salary grid maths (median current / expected / hike, n), companies merged across spellings, and the n ≥ 3 rule
- the period counting real activity only (a merely touched record is left out)
- the period, domain and client filters, and the previous-period figures
- market movement per company and per city × role family, top reasons and market signals
- Talent Pulse holding only aggregated numbers with n (no names, phones, free text or individual CTCs)
- the newsletter draft is only a draft: nothing queued, nothing sent, admin only, not visible to another agency
- recruiters see only their own jobs
- tenant isolation, CSV export, and the switch (admin only, per agency)

## Rollback

1. **Hide it, keep the code:** Settings → My Account → *Insight capture* → untick **Market intelligence**.
   The menu item disappears in the main app, and the page and its data answer "switched off".
   (On the separate pages, such as the Talent Map, the shared menu may still list the item until it is
   removed with step 2.)
2. **Switch the module off:** in `modules/__init__.py` delete the line `'insight_market', # Insight Wave 3 …`,
   commit, push and wait for Live. Do this **before** removing Wave 2 or Wave 1. Waves 1 and 2 keep working.
3. **Full undo:** GitHub Desktop → **History** → right-click the *Insight Wave 3* commit →
   **Revert changes in commit** → **Push origin**.

Talent Pulse drafts already created stay in Market Pulse as ordinary drafts; delete them there if you don't want
them. Nothing else was added to the database.
