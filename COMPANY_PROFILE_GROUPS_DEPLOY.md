# Company card: every profile group's fields + market-note-only Call capture. Deploy & rollback

Two changes requested after Insight Waves 1–3.

## 1. Call capture card → "Market note" card
The card on the candidate profile now asks only for the **market note**. Nothing is asked twice:

| Data | Where it is entered |
|---|---|
| Expected CTC, notice period | **Key Facts** |
| Yearly target, team size, product / function / geography and every other role field | **Profile** card (profile groups) |
| News about a company (hiring, freeze, exits …) | **Market note** card |

**Save note** works only with a note written; **Enter** also saves. Call analysis still proposes team size and
target; approving them updates the same Profile fields as before.

## 2. Company Talent Map shows every profile group
The company card used to read only the built-in sales fields. It now reads each person's **own profile group**
(Sales, Production, Pre-sales, Tender, Design …) and **that group's fields**, as set up in Settings → My Account →
Profile groups. A new group or field shows up on the company card by itself, with no code change.

**People tab**
- Chips at the top: **All people** (today's view, unchanged) · one chip per group with its head count ·
  **Not grouped**.
- **Sales** shows the familiar sales view for the Sales group's people.
- **Any other group** shows:
  - **Numbers:** n, median, range and total per number field (e.g. Plant capacity, Tender value), only when at
    least 3 people have it filled in.
  - **Bar charts** for tag and choice fields (e.g. Software: AutoCAD 3, ETAP 2).
  - **Notes fields:** how many people have them filled in.
  - A side-by-side table with that group's fields.

**Intelligence tab**
- A **"… profile fields"** card per group, with the same numbers, charts and counts.

**Which group a person is in:**
- The group chosen on their profile.
- A person with sales data and no group counts as **Sales**, the same rule as the Profile card.
- The rest are **Not grouped**.
- If the person is on several jobs, the newest value of each field is used.

**Client view (owner's decision 2-A):**
- Number fields appear only as **bands**: median and range, at least 3 people, no totals.
- Tag and choice fields appear as **counts**.
- **Text fields are never shown.**
- The server leaves the per-person numbers and text out of the client-view answer entirely, so they are not
  just hidden on screen.

## Files

| File | What changed |
|---|---|
| `insight-capture.js` | Call capture card holds only the market note |
| `modules/talent_map.py` | `GET /api/talent-map/company` also returns each person's group and group values, the groups in use and a per-group summary. New: `?view=client`. The query moved into `company_people()` (same result as before) |
| `modules/insight_company.py` | Intelligence answer gets `profile_fields` (per-group aggregates) |
| `talent-map.html` | Group chips and group view on People; profile-fields cards on Intelligence; Client view re-fetches from the server |
| `test_company_profile_groups.py` (new) | 6 tests |
| `INSIGHT_WAVE1_DEPLOY.md`, `COMPANY_PROFILE_GROUPS_DEPLOY.md` (new) | Docs |

No new tables or columns, and nothing in the database changes. The existing answer of `/api/talent-map/company`
keeps every field it had; the new fields are only added.

## Deploy (GitHub Desktop → Render)
1. Merge the PR on GitHub, or copy the files above into the repository folder in **GitHub Desktop**, then
   **Commit to main** → **Push origin**.
2. Wait for Render to show **Live** (2–4 minutes).
3. Hard-refresh the app once (**Ctrl + Shift + R**).

## Check it in the live app
1. Open a candidate → Overview: the **Market note** card has only the note (no CTC, notice, target or team fields).
   Write a note → **Save note** → "Saved ✓".
2. Company Talent Map → a company where you have, e.g., Production people → **People**: the chips show
   *All people · Sales · Production · … · Not grouped*. **All people** looks exactly as before.
3. Click **Production**: the Numbers table, the tag charts and the table with Production's own fields appear.
4. Tick **Client view**: names become *Candidate A, B …*, the per-person number and note columns disappear, and the
   numbers stay as bands.
5. **Intelligence** tab: a "Production profile fields" card with the same figures.

## Tests

```bash
python -m pytest test_company_profile_groups.py -q
```
```bash
python -m pytest -q
```

## Rollback
Revert the PR in GitHub Desktop (**History** → right-click the commit → **Revert changes in commit** → **Push
origin**). Nothing to clean up in the database.
