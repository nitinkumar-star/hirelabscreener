# Insight Engine, Wave 2: Company Intelligence. Deploy & rollback

Wave 2 adds an **Intelligence** tab to the Company Talent Map. It is one page per company that answers
"what do we know about this company's talent and market". It fills itself from the candidates already in
the database plus the Wave 1 capture data (reasons, market notes, role family, level band).

**Wave 1 must be live first.** Wave 2 reads Wave 1's tables, and its on/off switch sits in Wave 1's Settings section.

## What you will see

Company Talent Map → pick a company → **People** (the page you know, unchanged) | **Intelligence** (new).

| Section | What it shows |
|---|---|
| Top numbers | People known (current / former), openness %, "said no" %, number of market notes |
| People known | Current employees in a role family × level band grid |
| Pay | Per role family × level: n, current CTC min · median · max, median expected CTC, median hike ask %. Only for **3 or more people**; otherwise "n too small". Hike ask only counts people with both CTCs filled in |
| Notice period | 0–30 / 31–60 / 61–90 / 90+ days |
| Openness | Of the people we **contacted** (reached Called or any later stage, Not Interested included): how many reached Interested or later, how many said no, how many have no answer yet. People still in Screening / Not Contacted are not counted |
| Why people said no | Top reasons (latest reason per person), plus the 3 most recent detail lines |
| Market notes | Newest first, with a filter per signal type |
| *Company* as our client | Only when the company matches a CRM client or a job's client name: why the client rejected candidates (with their feedback), and why candidates said no to this client. The employer-brand signals (client brand, CTC gap, role / scope) are listed separately |
| Data quality | % of people with expected CTC, % with a role family, % of "no"s that have a reason |

**Client view** (the existing tick box) is enforced **on the server** for this tab. The answer the browser
receives has no names, no ids, no phone numbers, no individual CTCs and no free text (reason details, client
feedback, market-note text), only counts, percentages and pay bands of 3 or more people. Ticking Client view
also ticks Hide CTC, as on the People tab, so untick **Hide CTC** to show the pay bands in a client PDF.
**Print / Save PDF** prints whichever tab is open.

**"Company intel ↗" links** open this tab in a new window from:
* the candidate profile → Key Facts → Company
* the job header (Jobs / *client name*)

Recruiters see only the candidates of their own jobs, exactly like the People tab. Numbers are kept for 10
minutes per company. Saving a reason or a market note refreshes that company at once; **↻ Refresh** forces a
fresh calculation.

## Files

| File | What changed |
|---|---|
| `modules/insight_company.py` (new) | `GET /api/talent-map/intel`, the calculations, the 10-minute cache, the on/off switch |
| `modules/__init__.py` | Adds `'insight_company'` (one line, after `insight_capture`) |
| `modules/insight_capture.py` | Clears Wave 2's cache when a reason or market note is saved (does nothing if Wave 2 is off) |
| `talent-map.html` | People / Intelligence tabs and the Intelligence tab itself |
| `index.html` | "Company intel ↗" links (profile Key Facts, job header) |
| `insight-capture.js` | Settings → Insight capture: new **Company intelligence** switch |
| `test_insight_company.py` (new) | 11 tests |
| `INSIGHT_WAVE2_DEPLOY.md` (new) | This file |

**No new tables or columns.** Wave 2 only reads. Its switch (`insight_company_on`, default **on**) is one row
in the Wave 1 `insight_settings` table.

## Deploy (GitHub Desktop → Render)

1. Open **GitHub Desktop** and choose the `hirelabscreener` repository.
2. Copy the files from the table above into the repository folder, replacing the old ones at the same paths.
3. Write a summary such as *Insight Wave 2: company intelligence*, press **Commit to main**, then **Push origin**.
4. Wait for Render to show **Live** (2–4 minutes). The log shows `[modules] mounted: insight_company`.
5. Hard-refresh the app once (**Ctrl + Shift + R**).

## Check it in the live app

1. Menu → **Company Talent Map** → type *Siemens* (or any company you know well) → **Show talent**.
   Two tabs appear: **People** and **Intelligence**. People looks exactly as before.
2. Open **Intelligence**. The numbers fill in. A pay row with fewer than 3 people shows "n too small".
3. Tick **Client view**, then untick **Hide CTC**. The reason detail lines, client feedback and note texts
   disappear; counts and pay bands stay. **Print / Save PDF** gives the client version.
4. Open the company of one of your clients (e.g. *Schneider Electric*). The "… as our client" card appears.
5. Open any candidate profile. Next to the company in **Key Facts** there is **Company intel ↗**. The same link
   is in the job header next to the client name.
6. Save a market note on a profile (Call capture card), go back to the Intelligence tab and refresh the browser
   page: the note is there at once.
7. Sign in as a recruiter: the tab shows only people from that recruiter's own jobs.

## Tests

```bash
python -m py_compile server.py modules/*.py
```
```bash
node --check insight-capture.js
```
```bash
python -m pytest test_insight_company.py -q
```
```bash
python -m pytest -q
```

`test_insight_company.py` covers: people grid (a person on two jobs counted once, role family taken from Function
tags when empty, former employees); pay medians, median expected and median hike % on fixture data; the n ≥ 3
rule; notice buckets; openness (only contacted people); top reasons; the client branch; market notes and data
quality; client view leaks no names, phones, ids, individual CTCs or free text; cache and its clearing by Wave 1
writes; recruiter scope; tenant isolation; the switch (admin only, per agency).

## Rollback

1. **Hide it, keep the code:** Settings → My Account → *Insight capture* → untick **Company intelligence**.
   The tab and the "Company intel" links disappear for the whole agency.
2. **Switch the module off:** in `modules/__init__.py` delete the line `'insight_company', # Insight Wave 2 …`,
   commit, push and wait for Live. The Talent Map shows only the People tab again. Wave 1 keeps working (its
   cache clearing simply does nothing).
3. **Full undo:** GitHub Desktop → **History** → right-click the *Insight Wave 2* commit →
   **Revert changes in commit** → **Push origin**.

Nothing was added to the database except the switch row, so there is nothing to clean up.
