# HireLab Assistant (Chrome extension)

Side panel for the HireLab ATS. Load it from `chrome://extensions` →
Developer mode → **Load unpacked** → this folder.

## v2.5 — 1-click Naukri profile

1. Open a candidate on Naukri — job **Responses** (`hiring.naukri.com/hiring/…/apply/…`)
   or **Resdex** (`resdex.naukri.com/v3/preview…`).
2. In the side panel (Candidate tab) press **⤓ Read this Naukri profile**.
   - The candidate column is read (the "AI matched similar profiles" panel is skipped).
   - With the tick on, Naukri's own **Download** button is pressed.
3. When the CV download finishes it is attached automatically and Analyse runs.

To attach the CV automatically, switch on once:
`chrome://extensions` → HireLab Assistant → **Details** → **Allow access to file URLs**.
Without it the panel tells you the file name and you drag it in from Chrome's downloads.

The page is read only when you press the button: one read-only script in
Chrome's isolated world, no content script, nothing left on the page.
