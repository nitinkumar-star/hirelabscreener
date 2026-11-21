# HireLab Academy: deploy & rollback

The **Academy** (`/academy`, menu item **Academy** under Learning) explains every HireLab feature step by step,
and lets anyone ask a question in English, Hindi or Hinglish.

## Owner's decisions (Nov 2026)

| Decision | Choice | What it means |
|---|---|---|
| 1. Who writes the guides | **Shipped with the app** | The guides live in `academy_content.py` and are updated with each release. Admins can add an **"Our agency's note"** to any guide |
| 2. Where answers come from | **Only the Academy** | The AI answers from the guides only, links the guide it used, and says plainly when something is not covered |
| 3. Who can open it | **Everyone** | Admins see every guide; recruiters the guides for what they can use; freelance sourcers only their guides |

## What you will see

- **51 guides in 9 sections:** Getting started · Jobs & pipeline · Candidates · Calls & AI · Clients & BD · Database
  & engagement · Insight & market · Money · Team & admin. Each guide has: Where, what it is, when to use it,
  step-by-step, good to know, related guides, and the agency note.
- **Search** across all guides.
- **Ask a question:**
  - The answer comes from the guides, with **Read more** links and 👍 / 👎.
  - If the guides do not cover something, it says so instead of guessing, and the question is listed for admins.
  - The Academy never reads candidate, client or job data.
  - Limit: **30 questions per person per day**.
  - Each question is one small DeepSeek call. It shows in the AI cost meter as `academy` and stops at the
    monthly AI limit. Without a DeepSeek key, the related guides are shown instead.
- **Questions to look at** (admins only): questions the Academy could not answer, or answers marked 👎. Add an
  agency note to the right guide, then **Mark handled**. This list tells you which guides to add or improve.
- **"? Learn" links** next to the page title on Company Talent Map, Market Intel, Market Pulse, Talent Pool and
  Hot List, opening that page's guide.
- The **product tour** now includes the Academy.

## Files

| File | What changed |
|---|---|
| `academy_content.py` (new) | All guide text. **Update the matching guide whenever a feature changes** |
| `modules/academy.py` (new) | Page, guides, search, questions, agency notes, admin list |
| `academy.html` (new) | The Academy page |
| `modules/__init__.py` | Adds `'academy'` |
| `modules/freelancer.py` | Freelancers may read their guides and ask questions (`/api/academy/articles`, `/api/academy/ask`) |
| `modules/access.py` | `/api/academy/` added to the recruiter guard's checked-inside list (feedback works only on your own question) |
| `index.html` | **Academy** menu item and a tour step |
| `talent-map.html`, `market-intel.html`, `newsletter.html`, `talent-pool.html`, `hotlist.html` | "? Learn" link |
| `test_academy.py` (new) | 10 tests |

**New tables:**
- `academy_questions`: question, answer, guides used, covered or not, 👍/👎, handled.
- `academy_notes`: one note per agency per guide.

## Deploy (GitHub Desktop → Render)
1. Merge the PR on GitHub, or copy the files above into the repository folder in **GitHub Desktop**, then
   **Commit to main** → **Push origin**.
2. Wait for Render to show **Live** (2–4 minutes). The log shows `[modules] mounted: academy`.
3. Hard-refresh the app once (**Ctrl + Shift + R**).

## Check it in the live app
1. The menu shows **Academy** under Learning. Open it: 9 sections of guides.
2. Open **Reasons for every "no" and client feedback**. As admin, press **Add a note**, write one line, **Save note**.
3. Ask: *"Client rejection ka feedback kahan likhun?"* → the answer comes in Hinglish with a link to that guide.
   Press 👍 or 👎.
4. Ask something HireLab does not do (e.g. *"How do I run payroll?"*) → it says it is not covered. As admin,
   **Questions to look at** lists it.
5. Sign in as a recruiter: admin-only guides (Profit & Cash, Team Activity …) are not listed.
6. Open the Company Talent Map: **? Learn** next to the title opens its guide.

## Keeping the guides up to date
When a feature changes, edit its entry in `academy_content.py` in the same release, the same way a
`*_DEPLOY.md` is written. `test_academy.py` checks that every guide is complete and every related link points to a
real guide.

## Tests

```bash
python -m pytest test_academy.py -q
```
```bash
python -m pytest -q
```

## Rollback
Revert the PR in GitHub Desktop (**History** → right-click the commit → **Revert changes in commit** → **Push
origin**). The two new tables stay and are harmless.
