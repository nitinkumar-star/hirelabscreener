# Job Pulse + Call Analysis fix: deploy (on top of the 18 files already live)

## Files (5), all replaced or added at the same paths
| File | Change |
|---|---|
| `modules/job_pulse.py` | NEW: Job Pulse (status line, whose move, next event, 7-day movement, ₹ value, Focus today, SPOC view, quick look, chase client, morning digest) |
| `modules/__init__.py` | +1 line (`'job_pulse'`) |
| `index.html` | Job Pulse on the Jobs page (one script at the end) + clearer upload errors for Call Analysis |
| `server.py` | Call Analysis only: convert/shrink recordings, clearer Groq errors, stay inside the 180 s limit |
| `requirements.txt` | +1 line `imageio-ffmpeg==0.6.0` (the first build takes about 1 minute longer) |

If you already deployed the 3-file Call Analysis fix, `server.py` and `requirements.txt` here are the same files, so copying them again changes nothing.

Database changes are additive only: new tables `job_pulse_fee`, `job_pulse_chase` and `job_pulse_settings`. Nothing existing is changed.

## What you will see on Jobs
- **Under every job:** whose move it is (🟠 Client · N days / 🔵 Us / 🟢 Candidate), a one-line status, the next event, last-7-days movement, ₹ likely of ₹ full fee, the SPOC, **✉ Chase client** (when profiles have waited 4+ days), and **Quick look**.
- **Above the table:**
  - **🎯 Focus today:** the top 5 jobs by ₹ likely × urgency, with the reason.
  - **Job pulse tiles:** ₹ likely in the pipeline, how many jobs are waiting on the client, how many need our move.
- **SPOC:**
  - A **SPOC** filter in the toolbar.
  - **Show SPOC view:** one card per client SPOC (jobs, profiles with the client and for how long, interviews, ₹). Click a card to filter.
- **Quick look:** a side panel with pending feedback, interviews, open tasks, Interested replies and recent activity, without opening the job.
- **₹ value:**
  - The fee % comes from, in order: the job's own fee % (click the ₹ amount to set it); the client's past placements (Placed/Joined only); the default 8.33% (⚙ Pulse settings).
  - The CTC is the middle of the job's CTC band. Jobs without a band show "₹ —".
- **Morning digest (⚙ Pulse settings, admin only):** off by default. When it is on, admins get every active job each morning and recruiters get only their own jobs. Use "Send me today's digest now" to preview it.
- **SPOC source:** a job's SPOC is the CRM contact chosen in Mandate Setup. Jobs without one show under "No SPOC set".

## Deploy
1. Copy the 5 files, then commit and push. Render redeploys.
2. Check the logs for `[modules] mounted: job_pulse`.
3. Open **Jobs**. The pulse lines, Focus today and the SPOC filter appear after a second.

## Rollback
Remove `'job_pulse'` from `modules/__init__.py` (or redeploy the previous commit). The Jobs page goes back to exactly how it was, because the script does nothing when the API is not there. The new tables stay unused.
