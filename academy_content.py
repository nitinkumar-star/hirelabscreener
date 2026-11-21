"""
HireLab Academy — the guides.  (Nov 2026)

One entry per feature. Update the matching entry whenever a feature changes,
the same way a *_DEPLOY.md is written for a release.

Fields
  key       stable id, used in links (/academy#<key>)
  title     the guide's title
  section   one of SECTIONS (keys)
  roles     who sees it: 'A' agency admin, 'R' recruiter (non-admin), 'F' freelance sourcer
  where     how to open the feature
  summary   one or two sentences: what it is
  when      when to use it
  steps     how to use it, step by step
  tips      good to know
  related   keys of related guides
  link      (optional) URL that opens the feature
  keywords  extra words people may search or ask with (incl. Hinglish)
"""

SECTIONS = [
    ('start', 'Getting started'),
    ('jobs', 'Jobs & pipeline'),
    ('candidates', 'Candidates'),
    ('ai', 'Calls & AI'),
    ('clients', 'Clients & BD'),
    ('engage', 'Database & engagement'),
    ('insight', 'Insight & market'),
    ('money', 'Money'),
    ('team', 'Team & admin'),
]

ALL = 'ARF'
STAFF = 'AR'
ADMIN = 'A'

ARTICLES = [
    # ── Getting started ─────────────────────────────────────────────────────
    {
        'key': 'getting-started', 'title': 'Getting started with HireLab', 'section': 'start', 'roles': STAFF,
        'where': 'Sign in → the menu on the left',
        'summary': 'HireLab is your recruitment workspace: jobs (mandates) with their pipelines, your candidate database, '
                   'clients, e-mail, tasks and market insight in one place. The menu on the left opens every part.',
        'when': 'Your first day, or when you are not sure where something lives.',
        'steps': [
            'Open **Home** to see today\'s focus, follow-ups that are due and the 6:30 check.',
            'Open **Jobs** to see every mandate you work on. Click a job to open its tabs: Mandate Setup, Job Breakdown, '
            'Pipeline, Matching Candidates, Candidate Profiles and Recruiter Pitch.',
            'Use **Talent** to search everyone in your database and to add or upload CVs.',
            'Use **Tasks** for reminders, interviews and follow-ups.',
            'Set up your e-mail and signature once in **Settings → My Account**.',
            'Replay the product tour any time: **Settings → My Account → Product tour**.',
        ],
        'tips': [
            'The search box at the top finds candidates by name, e-mail or phone from any page.',
            'Pages that open on their own (Talent Map, Market Intel, Talent Pool …) have a ☰ Menu button to come back.',
            'Stuck? Ask the Academy in plain words, in English, Hindi or Hinglish.',
        ],
        'related': ['home', 'jobs', 'talent', 'settings', 'academy-help'],
        'keywords': ['start', 'shuru', 'new user', 'kaise use kare', 'overview', 'menu', 'navigation', 'tour'],
    },
    {
        'key': 'freelancer-guide', 'title': 'For freelance sourcers', 'section': 'start', 'roles': 'F',
        'where': 'Sign in with your freelancer account',
        'summary': 'As a freelance sourcer you upload CVs for the jobs the agency assigned to you and follow what happens to '
                   'the people you sourced. You cannot move stages or see other people\'s candidates.',
        'when': 'When you start sourcing for the agency.',
        'steps': [
            'Install the **HireLab Assistant** Chrome extension the agency shares with you, and sign in.',
            'Pick one of your assigned jobs in the Assistant.',
            'Paste a candidate\'s profile text or attach the CV you downloaded, then press **Push**. '
            'A duplicate of someone already in that job is blocked.',
            'Open HireLab to see your sourced candidates with their current stage. You get a notification whenever the '
            'agency does something with one of your candidates.',
            'If the agency assigns you calls, open **My Calls** to see who to call and log each outcome.',
        ],
        'tips': [
            'You only ever see the jobs assigned to you and the candidates you uploaded.',
            'A commission (if agreed) is recorded by the agency when your candidate is placed.',
        ],
        'related': ['assistant', 'hotlist', 'academy-help'],
        'keywords': ['freelancer', 'sourcer', 'upload cv', 'commission', 'assigned jobs'],
    },
    {
        'key': 'home', 'title': 'Home', 'section': 'start', 'roles': STAFF,
        'where': 'Menu → Home',
        'summary': 'Your day at a glance: KPIs, today\'s focus (overdue and today\'s tasks), quick actions, the pipeline '
                   'funnel, recent activity and the 6:30 check.',
        'when': 'Every morning, and at the end of the day for the 6:30 check.',
        'steps': [
            'Pick the period at the top (7 / 30 / 90 days, all time or custom dates). Admins can switch Team / Me.',
            'Work through **Today\'s focus**: click a task to open the candidate or the task.',
            'Check the **6:30 check** card before you leave (see its own guide).',
            'Use **Quick actions** to add a job, search talent or open Tasks.',
        ],
        'tips': ['Recruiters see only their own jobs on Home; admins see the whole agency.'],
        'related': ['check-630', 'tasks', 'jobs'],
        'keywords': ['dashboard', 'kpi', 'today focus', 'aaj ka kaam'],
    },
    {
        'key': 'settings', 'title': 'Settings', 'section': 'start', 'roles': STAFF,
        'where': 'Menu → Settings (bottom left)',
        'summary': 'Settings has tabs: My Account (profile, signature, plan & AI usage, profile groups, Insight capture, '
                   'Google Meet, product tour), My Templates, Workspace, Communication, Automation, Public (careers page) '
                   'and Admin (AI keys, users, billing).',
        'when': 'Once when you start, and whenever you change your mailbox, templates or team.',
        'steps': [
            '**My Account:** your name, designation, phone and e-mail (used in signatures and AI e-mails).',
            '**My Email:** recruiters can send from their own Gmail (Gmail address + App Password).',
            '**My Templates:** your own e-mail and WhatsApp templates; your copy of a company template wins for you.',
            '**Workspace / Communication / Automation / Public / Admin:** company-wide settings, admins only.',
            'Use the search box at the top of Settings to find any setting by name.',
        ],
        'tips': ['Recruiters see My Account and My Templates; the other tabs are for admins.'],
        'related': ['profile-groups', 'insight-settings', 'ai-costs', 'users', 'email-box'],
        'keywords': ['settings', 'gear', 'signature', 'app password', 'templates', 'setting kahan'],
    },
    {
        'key': 'academy-help', 'title': 'Using the Academy', 'section': 'start', 'roles': ALL,
        'where': 'Menu → Academy',
        'summary': 'The Academy explains every feature of HireLab, step by step, and answers your questions.',
        'when': 'Whenever you want to learn a feature or are not sure how to do something.',
        'steps': [
            'Browse a section, or type in the search box to find a guide.',
            'Under **Ask a question**, type your question in English, Hindi or Hinglish and press **Ask**.',
            'The answer comes only from these guides and links the guide it used. Mark it 👍 or 👎.',
            'Admins can add an **Our agency\'s note** to any guide, e.g. how your team does it.',
            'Admins see the questions the Academy could not answer, so missing guides can be added.',
        ],
        'tips': [
            'The Academy does not read candidate or client data; it only explains how HireLab works.',
            'Each person can ask up to 30 questions a day.',
        ],
        'related': ['getting-started'],
        'keywords': ['help', 'madad', 'question', 'sawal', 'guide', 'academy', 'learn'],
    },

    # ── Jobs & pipeline ─────────────────────────────────────────────────────
    {
        'key': 'jobs', 'title': 'Jobs and Job Pulse', 'section': 'jobs', 'roles': STAFF,
        'where': 'Menu → Jobs',
        'summary': 'Every mandate with what is happening on it: whose move it is (client, us or the candidate), a one-line '
                   'status, the next event, last-7-days movement and the ₹ fee you are likely to earn.',
        'when': 'Daily, to decide which jobs need you first.',
        'steps': [
            'Start with **🎯 Focus today**: the top jobs by likely ₹ × urgency, with the reason.',
            'Under each job, the coloured ball shows whose move it is: 🟠 client (waiting N days), 🔵 us, 🟢 candidate.',
            'Click the ₹ amount to set the job\'s own fee %. Otherwise the client\'s past fee or the default 8.33% is used.',
            'Use the **SPOC** filter or **Show SPOC view** to see jobs per client contact.',
            '**✉ Chase client** drafts a reminder when profiles have waited too long for feedback.',
            '**Quick look** opens pending feedback, interviews, tasks and recent activity without opening the job.',
        ],
        'tips': [
            'A job\'s SPOC is the CRM contact chosen in Mandate Setup.',
            'Admins can switch on a morning digest e-mail in ⚙ Pulse settings.',
        ],
        'related': ['mandate-setup', 'pipeline', 'crm'],
        'keywords': ['mandate', 'job pulse', 'focus today', 'spoc', 'chase client', 'fee', 'whose move'],
    },
    {
        'key': 'mandate-setup', 'title': 'Creating and setting up a job', 'section': 'jobs', 'roles': STAFF,
        'where': 'Jobs → + New job, then the job\'s Mandate Setup tab',
        'summary': 'A job (mandate) holds the client, role, location, experience and CTC band, the JD and the hiring '
                   'intent. Everything else (matching, pitch, call analysis, job mail) reads from it.',
        'when': 'When a client gives you a new requirement.',
        'steps': [
            'Click **+ New job**, pick the client and fill role, location, experience and CTC range.',
            'Paste the **JD**. Add must-have and good-to-have skills and the SOP if you have them.',
            'Choose the client **SPOC** (a CRM contact) so Job Pulse and chases know who to follow up.',
            'Under **Manage**, the **Automated e-mails** switch can stop all bulk / automatic mail to this job\'s candidates.',
            'Get the job\'s **application link** to post on LinkedIn, WhatsApp groups and so on.',
        ],
        'tips': ['A complete JD makes Job Breakdown, Matching, Pitch and Call Analysis much better.'],
        'related': ['job-breakdown', 'job-link', 'matching', 'jobs'],
        'keywords': ['new job', 'mandate setup', 'jd', 'requirement', 'naya job', 'automated emails off'],
    },
    {
        'key': 'job-breakdown', 'title': 'Job Breakdown', 'section': 'jobs', 'roles': STAFF,
        'where': 'Open a job → Job Breakdown tab',
        'summary': 'AI splits the job into core (deal-breakers), important, nice-to-have, limits and "not this" '
                   '(look-alike profiles to reject), plus target / acceptable / avoid companies and designations.',
        'when': 'Right after creating a job, and before sourcing on Naukri with the Assistant.',
        'steps': [
            'Open the tab; the breakdown is built from the JD automatically.',
            'Check each line and edit anything that is wrong. Your edits are kept.',
            'If the JD changes later, an edited breakdown is kept and marked "JD changed" so you can review it.',
        ],
        'tips': ['The Assistant\'s Naukri screener and the candidate explanations use this breakdown.'],
        'related': ['assistant', 'mandate-setup', 'pitch'],
        'keywords': ['breakdown', 'core skills', 'must have', 'target companies'],
    },
    {
        'key': 'pipeline', 'title': 'Pipeline: moving candidates through stages', 'section': 'jobs', 'roles': STAFF,
        'where': 'Open a job → Pipeline tab',
        'summary': 'The job\'s candidates by stage (Screening, Called, Interested, Shared with Client, Interview, Placed, '
                   'Joined …) as a board or a one-line list.',
        'when': 'Every time something changes with a candidate.',
        'steps': [
            'Drag a card to another column, or change the stage in the list view or on the candidate profile.',
            'Moving to a "no" stage (Not Interested, Not Suitable, Client Rejected …) asks for a reason — one tap.',
            'Client rejections also ask for the client\'s feedback.',
            'Click a card to open the candidate profile.',
        ],
        'tips': [
            'Stages can be renamed or added by an admin; Placed and Joined are fixed (invoices follow them).',
            'Every move is written to the candidate\'s journey with who did it.',
        ],
        'related': ['reasons', 'candidate-profile', 'interviews'],
        'keywords': ['stage', 'move', 'drag', 'kanban', 'not interested', 'shared with client', 'stage change'],
    },
    {
        'key': 'reasons', 'title': 'Reasons for every "no" and client feedback', 'section': 'jobs', 'roles': STAFF,
        'where': 'Any stage change to a "no" stage',
        'summary': 'When a candidate moves to Not Interested, Interview Backout, Not Suitable, Screened-Out or Client '
                   'Rejected, HireLab asks why. Client rejections also need the client\'s feedback.',
        'when': 'Every negative move. It takes a few seconds and builds your market knowledge.',
        'steps': [
            'Move the candidate as usual. The reason picker opens.',
            'Tap one reason (e.g. CTC gap, Counter offer, Location) and add one line of detail if useful.',
            'For a client rejection, write what the client said. It is added to the candidate\'s Client Feedback '
            'with the date; nothing already there is replaced.',
            'Press **Save & move**. **Cancel** leaves the candidate where they were.',
        ],
        'tips': [
            'Reasons feed the Company Intelligence tab and Market Intel ("why people say no").',
            'Missed one? The 6:30 check lists it with a Fix button.',
        ],
        'related': ['check-630', 'company-intel', 'market-intel', 'pipeline'],
        'keywords': ['reason', 'not interested reason', 'kyon mana kiya', 'client feedback', 'rejection', 'backout'],
    },
    {
        'key': 'check-630', 'title': 'The 6:30 check', 'section': 'jobs', 'roles': STAFF,
        'where': 'Home → 6:30 check card (and the red count on Home in the menu)',
        'summary': 'Records you touched today that are missing a reason, missing client feedback, or have expected CTC 0 '
                   'while at Interested or later.',
        'when': 'Before you finish for the day.',
        'steps': [
            'Open Home and look at the **6:30 check** card.',
            'Press **Fix** on a row: a missing reason opens the reason picker; expected CTC 0 opens the profile with '
            'Key Facts ready to edit.',
            'The row disappears once it is fixed.',
        ],
        'tips': ['It only lists your own work of today, so it stays short.'],
        'related': ['reasons', 'candidate-profile', 'home'],
        'keywords': ['630', '6:30', 'end of day', 'missing reason', 'expected ctc 0', 'shaam'],
    },
    {
        'key': 'matching', 'title': 'Matching Candidates and Job Matching', 'section': 'jobs', 'roles': STAFF,
        'where': 'Job → Matching Candidates tab; candidate profile → Job Matching',
        'summary': 'AI meaning-based matching (not keywords) between the job\'s JD and every profile + CV in your database.',
        'when': 'When you start a job (who do we already have?) or when a good candidate comes in (which job fits?).',
        'steps': [
            'Open the job → **Matching Candidates**. The best matches from your whole database are listed.',
            'Tick the people you want and add them to the pipeline.',
            'Someone already in another job gets a copy in this job; Central Database people move into the job.',
            '**✉ E-mail top matches** sends the job to matched people through Job-Match Mail.',
        ],
        'tips': ['Matching needs a JD on the job and works best with full CVs on candidates.'],
        'related': ['jobmail', 'talent', 'mandate-setup'],
        'keywords': ['match', 'matching', 'similar candidates', 'kaun fit hai', 'vector'],
    },
    {
        'key': 'job-link', 'title': 'Job application link', 'section': 'jobs', 'roles': STAFF,
        'where': 'Job → Mandate Setup → application link',
        'summary': 'A link for one job to post on LinkedIn, WhatsApp groups, Facebook or Naukri posts. Applicants upload '
                   'their CV and land straight in that job\'s pipeline at Screening.',
        'when': 'When you advertise a job.',
        'steps': [
            'Open the job and copy the application link (there are ready links per channel).',
            'Post it. Each application arrives in the job\'s pipeline with the CV attached, and the job\'s recruiters are notified.',
        ],
        'tips': ['The link is signed, so nobody can change it to apply to another job.'],
        'related': ['mandate-setup', 'pipeline'],
        'keywords': ['apply link', 'job post', 'linkedin post', 'careers'],
    },
    {
        'key': 'interviews', 'title': 'Scheduling interviews', 'section': 'jobs', 'roles': STAFF,
        'where': 'Candidate profile → Schedule interview',
        'summary': 'Record client interviews with date, time, round and mode, and keep everyone reminded.',
        'when': 'When a client confirms an interview slot.',
        'steps': [
            'Open the candidate → **Schedule interview**.',
            'Fill date, time, round and mode, and save.',
            'The interview shows on the profile, in Tasks and in Job Pulse ("next event").',
        ],
        'tips': ['For meetings booked through your own calendar link, use the Scheduler.'],
        'related': ['scheduler', 'tasks', 'jobs'],
        'keywords': ['interview', 'schedule', 'round', 'client interview'],
    },

    # ── Candidates ──────────────────────────────────────────────────────────
    {
        'key': 'talent', 'title': 'Talent: searching your database', 'section': 'candidates', 'roles': STAFF,
        'where': 'Menu → Talent',
        'summary': 'Search everyone in your database by skill, company, product, region, CTC, team size, profile group '
                   'fields and more. The Central Database holds people who are not on a job yet.',
        'when': 'Before sourcing outside: you may already know the right person.',
        'steps': [
            'Type keywords and use the filters (company, location, CTC, experience, tags, profile group fields).',
            'Click a phone number to call from your phone.',
            'Open a profile, or add people to a job.',
            'The **Responses** tab holds applications from your careers page.',
        ],
        'tips': ['Recruiters see the candidates of their own jobs plus the Central Database.'],
        'related': ['add-candidates', 'profile-groups', 'matching'],
        'keywords': ['search', 'database', 'central database', 'dhundo', 'find candidate', 'responses'],
    },
    {
        'key': 'add-candidates', 'title': 'Adding candidates and uploading CVs', 'section': 'candidates', 'roles': STAFF,
        'where': 'Talent → + Add candidate / Upload CVs; or inside a job',
        'summary': 'Add people one by one, upload many CV files at once (AI reads each CV), or push them from the '
                   'HireLab Assistant in Chrome.',
        'when': 'Whenever you find new candidates.',
        'steps': [
            'In **Talent**, choose **Upload CVs**, pick the Central Database or a job, and drop the files.',
            'Each CV is read by AI (name, company, CTC …) and stored with the candidate.',
            'If the same e-mail or phone is already there, HireLab tells you instead of creating a duplicate.',
            'If the AI cannot read the name, you are asked for it.',
        ],
        'tips': [
            'New candidates get role family, domain and level tags automatically within a few minutes.',
            'From Chrome, use the HireLab Assistant to add from Naukri text or LinkedIn in one click.',
        ],
        'related': ['assistant', 'talent', 'auto-tags'],
        'keywords': ['upload cv', 'add candidate', 'resume upload', 'bulk upload', 'duplicate'],
    },
    {
        'key': 'candidate-profile', 'title': 'The candidate profile', 'section': 'candidates', 'roles': STAFF,
        'where': 'Click any candidate',
        'summary': 'Everything about one person: Key Facts (company, designation, CTC, expected CTC, notice …), the '
                   'Profile card, Market note, journey, work experience, CV, communications, call analysis and the AI Agent.',
        'when': 'Whenever you work on a candidate.',
        'steps': [
            '**Stage** at the top: change it here (a "no" asks for a reason).',
            '**Key Facts:** click ✎ to edit company, CTC, expected CTC, notice period, location and contacts.',
            '**Profile card:** the candidate\'s profile group (Sales, Production …) and its fields.',
            '**Market note:** write any company news the candidate shared.',
            '**Notes & Journey:** every stage move, e-mail, reminder and note, newest first.',
            'Tabs: Overview, Jobs, Pitch, Billing, Resume, Communications, Call Analysis, AI Agent.',
        ],
        'tips': [
            'Expected CTC and notice live only in Key Facts; target and team only in the Profile card. Nothing is asked twice.',
            '"Company intel ↗" next to the company opens what you know about that company.',
        ],
        'related': ['profile-groups', 'market-note', 'call-analysis', 'ai-agent', 'pitch'],
        'keywords': ['profile', 'key facts', 'expected ctc', 'notice period', 'journey', 'candidate page'],
    },
    {
        'key': 'profile-groups', 'title': 'Profile groups (Sales, Production, Tender …)', 'section': 'candidates', 'roles': STAFF,
        'where': 'Settings → My Account → Profile groups; on a candidate: the Profile card',
        'summary': 'Your own profile sections per type of role, each with its own fields (number, tags, choice, text). '
                   'Every agency starts with a Sales group: yearly target, team, products, function, geography, gender.',
        'when': 'When your roles need different information: production, pre-sales, tender, design …',
        'steps': [
            'In Settings → Profile groups, create a group (e.g. "Production") and add fields '
            '(e.g. Plant capacity — number in MW; Software — tags; Shift — choice).',
            'On a candidate, open the **Profile** card, pick the group and fill its fields.',
            'Everything is searchable in Talent and appears on the Company Talent Map per group.',
        ],
        'tips': [
            'Renaming a field keeps its saved values.',
            'Number fields show as median / range on the company card once 3 or more people have them.',
        ],
        'related': ['candidate-profile', 'company-talent-map', 'talent'],
        'keywords': ['profile group', 'sales profile', 'custom field', 'production', 'tender', 'design', 'presales',
                     'yearly target', 'team size'],
    },
    {
        'key': 'market-note', 'title': 'Market notes', 'section': 'candidates', 'roles': STAFF,
        'where': 'Candidate profile → Market note card',
        'summary': 'News about a company that a candidate shared on a call: hiring, freeze, people leaving or joining, a '
                   'new order or project, pay moves, re-org.',
        'when': 'Right after a call where you heard something about a company.',
        'steps': [
            'Open the candidate → **Market note**.',
            'The company is pre-filled with their current company; change it if the news is about another one.',
            'Pick the signal type, write one line, and press **Save note** (or Enter).',
        ],
        'tips': [
            'Call Analysis can propose market notes from a recording; they are saved only when you tick them.',
            'Notes appear on the company\'s Intelligence tab and in Market Intel.',
        ],
        'related': ['company-intel', 'market-intel', 'call-analysis'],
        'keywords': ['market note', 'news', 'hiring freeze', 'company update', 'signal'],
    },
    {
        'key': 'auto-tags', 'title': 'Automatic role, domain and level tags', 'section': 'candidates', 'roles': STAFF,
        'where': 'Runs by itself; admins: Settings → My Account → Insight capture → Tag existing candidates',
        'summary': 'AI reads each new candidate and fills role family (e.g. Channel Sales), function tags, domain '
                   '(LV, MV, Solar, Data Center …) and level band (Engineer … GM+) — only where they are empty.',
        'when': 'Automatically for new candidates; an admin runs it once for the existing database.',
        'steps': [
            'New candidates are tagged within a few minutes.',
            'For older records, an admin presses **Start tagging** in Settings → Insight capture. It works newest '
            'first; **Stop** pauses it and **Start again** continues.',
        ],
        'tips': ['Tags a person typed are never overwritten.'],
        'related': ['insight-settings', 'company-intel', 'market-intel'],
        'keywords': ['tagging', 'role family', 'level band', 'domain', 'backfill'],
    },

    # ── Calls & AI ──────────────────────────────────────────────────────────
    {
        'key': 'pitch', 'title': 'Recruiter Pitch and Final Result', 'section': 'ai', 'roles': STAFF,
        'where': 'Candidate profile → Pitch tab (or the job\'s Recruiter Pitch tab)',
        'summary': 'Before the call, AI checks the CV against the job\'s must-haves and gives a checklist of what to '
                   'verify. After the call, Final Result combines CV and call evidence into one decision.',
        'when': 'Just before you call a candidate for a job, and after the call.',
        'steps': [
            'Open the candidate on the job → **Pitch** → generate. You get match, strengths, gaps and points to verify.',
            'Call the candidate and cover the checklist.',
            'Analyse the call recording (Call Analysis).',
            'Back on Pitch, **Final Result** shows each must-have as MET / NOT MET / UNCLEAR with the reason, and the decision.',
        ],
        'tips': [
            'The client\'s budget is never put in the pitch.',
            'A candidate who said they are not interested never comes out as Shortlisted.',
        ],
        'related': ['call-analysis', 'job-breakdown'],
        'keywords': ['pitch', 'call script', 'checklist', 'final result', 'shortlist', 'evaluation'],
    },
    {
        'key': 'call-analysis', 'title': 'Call Analysis', 'section': 'ai', 'roles': STAFF,
        'where': 'Candidate profile → Call Analysis tab',
        'summary': 'Upload a call recording (Hindi, English or Hinglish). HireLab writes the transcript and a report: '
                   'interest, CTC, notice, concerns, fit against the JD, consistency with the CV, next step.',
        'when': 'After every important call.',
        'steps': [
            'Open the candidate → **Call Analysis** → upload the recording (mp3, m4a, wav, amr, 3gp …).',
            'Read the report. The box "AI ko call mein ye details mili" lists what the call revealed: CTC, expected '
            'CTC, notice, company, team, target, reason for no, market notes.',
            'Keep the ticks on what is correct and press **Update selected**. Nothing changes before that.',
            'Download the report: internal, or a client version without CTC, concerns or the transcript.',
        ],
        'tips': [
            'Very long recordings are shrunk automatically; the original is kept.',
            'Phone recorder files (.amr, .3gp) are converted for you.',
        ],
        'related': ['pitch', 'market-note', 'reasons'],
        'keywords': ['call recording', 'recording upload', 'transcript', 'call report', 'analysis', 'update selected'],
    },
    {
        'key': 'ai-agent', 'title': 'AI Agent on the candidate', 'section': 'ai', 'roles': STAFF,
        'where': 'Candidate profile → ✨ AI Agent tab, or the "Tell the AI agent…" box on Overview',
        'summary': 'Write the work in plain words ("send a follow-up e-mail for the JD at 11 AM tomorrow"). The agent '
                   'reads the candidate and proposes e-mails, WhatsApp drafts, reminders or answers for you to approve.',
        'when': 'For follow-ups and quick questions about a candidate.',
        'steps': [
            'Type the instruction and press **Run**.',
            'Check each card and edit it. **Approve & schedule** sends the e-mail at the time on the card.',
            'WhatsApp is never sent automatically: at the time, you get a notification and send it yourself.',
            'A reminder becomes a task; a question is answered on the spot.',
        ],
        'tips': [
            'If the candidate replies before a scheduled e-mail goes out, it is held and you are asked.',
            'Without a time, the card is set to the next working day at 10:00 AM.',
        ],
        'related': ['whatsapp', 'tasks', 'email-box'],
        'keywords': ['ai agent', 'follow up email', 'automatic email', 'agent'],
    },
    {
        'key': 'assistant', 'title': 'HireLab Assistant (Chrome extension)', 'section': 'ai', 'roles': ALL,
        'where': 'Chrome → HireLab Assistant side panel',
        'summary': 'A Chrome side panel that works on what you give it: text copied from a Naukri page, a downloaded CV, or '
                   'a LinkedIn profile (one click). It screens, checks duplicates, scores against a job and pushes the candidate.',
        'when': 'While sourcing on Naukri or LinkedIn.',
        'steps': [
            'Open the Assistant and pick the job.',
            'On a Naukri search page, copy the page (Ctrl+A, Ctrl+C), paste it in the **Search** tab: each profile '
            'gets open / maybe / skip with a reason. Mark ✓ Open or ✗ Skip to train it.',
            'For one candidate, paste the profile text and/or attach the CV, check duplicates and the AI review, then **Push**.',
            'On LinkedIn, press **Extract this LinkedIn profile** and save to the Central Database, a job, or BD.',
        ],
        'tips': [
            'The Assistant never reads Naukri pages by itself, so Naukri does not block it.',
            'Chat with the screener ("10+ years only for this job") — it saves those as rules for that job.',
        ],
        'related': ['job-breakdown', 'add-candidates', 'freelancer-guide'],
        'keywords': ['extension', 'chrome', 'naukri', 'linkedin', 'screener', 'push cv', 'resdex'],
    },
    {
        'key': 'whatsapp', 'title': 'WhatsApp', 'section': 'ai', 'roles': STAFF,
        'where': 'Candidate profile → Communications; Settings → Communication / My Templates',
        'summary': 'Message candidates on WhatsApp from their profile using templates. With the WhatsApp Business API '
                   'connected, the WhatsApp AI agent can send intros, share the JD and answer known questions, and '
                   'escalates anything new to you.',
        'when': 'For first outreach and quick follow-ups.',
        'steps': [
            'Open the candidate → Communications → WhatsApp, pick a template and send (or open WhatsApp on your phone/Web).',
            'Set your own message templates in Settings → My Templates.',
            'When the AI agent escalates a question, answer it — the agent learns your answer for next time.',
        ],
        'tips': ['Without the Business API, messages open in WhatsApp Web or on your phone.'],
        'related': ['ai-agent', 'settings'],
        'keywords': ['whatsapp', 'wa', 'message', 'template'],
    },

    # ── Clients & BD ────────────────────────────────────────────────────────
    {
        'key': 'crm', 'title': 'Clients (CRM)', 'section': 'clients', 'roles': ADMIN,
        'where': 'Menu → Clients (CRM)',
        'summary': 'Your clients: companies, contacts (hiring managers, HR, decision makers), their org chart, jobs, '
                   'invoices and a full timeline of every activity.',
        'when': 'When you add a client or need everything about one client in one place.',
        'steps': [
            'Add a client company, then its contacts.',
            'Open a client to see contacts, jobs, activities and e-mails together.',
            'Prospects live in the BD workspace and become clients with **Convert to client**.',
        ],
        'tips': ['Duplicates are blocked; deleting is soft, so an admin can restore.'],
        'related': ['bd', 'company-intel', 'invoicing'],
        'keywords': ['client', 'crm', 'contact', 'hiring manager', 'company'],
    },
    {
        'key': 'bd', 'title': 'BD workspace and BD Command Center', 'section': 'clients', 'roles': ADMIN,
        'where': 'Clients → BD workspace',
        'summary': 'Business development for companies you want as clients: prospects, people, deals, tasks and notes, '
                   'with saved views, custom columns and several workspaces (e.g. "IT Staffing", "Leadership Hiring").',
        'when': 'Daily BD work.',
        'steps': [
            'BD Home shows today\'s follow-ups, overdue items, meetings and new prospects.',
            'Add prospects and people; track deals in the Opportunities view.',
            'Add custom columns with + at the right end of the table header.',
            'When a prospect becomes a client, press **Convert to client**.',
        ],
        'tips': ['BD is for admins; recruiter accounts do not see it.'],
        'related': ['bd-calling', 'crm'],
        'keywords': ['bd', 'business development', 'prospect', 'deal', 'opportunity', 'workspace'],
    },
    {
        'key': 'bd-calling', 'title': 'BD Calling sheet', 'section': 'clients', 'roles': ADMIN,
        'where': 'Clients → BD workspace → Calling',
        'summary': 'A cold-calling sheet: call, note what the company does, log the outcome, send AI-written e-mails and '
                   'automatic follow-ups — on one screen.',
        'when': 'For cold-calling and prospect outreach.',
        'steps': [
            'Work the **Today** list: callbacks due first, then replies, opens and not-called prospects.',
            'Log the outcome with one click or keys 1–7 (No answer, Callback, Interested, Send details, Meeting, '
            'Not interested, Wrong number). The next step is set for you.',
            'Use **✨ Research** to fill company details from its website, and **🎙 Call plan** for your opener and objections.',
            '**✉ E-mail** gives 3 AI versions; bulk e-mails are reviewed one by one before they go.',
        ],
        'tips': [
            'Follow-ups stop when the prospect replies.',
            'The playbook (⚙) sets how your agency sounds; agency clichés are flagged.',
        ],
        'related': ['bd', 'crm'],
        'keywords': ['cold call', 'calling sheet', 'prospect email', 'playbook', 'follow up'],
    },
    {
        'key': 'scheduler', 'title': 'Scheduler and Google Meet', 'section': 'clients', 'roles': STAFF,
        'where': 'Menu → Scheduler',
        'summary': 'Publish your free slots and share your booking link. Candidates and clients book a time themselves; '
                   'confirmations and calendar invites go out automatically.',
        'when': 'To stop going back and forth on timings.',
        'steps': [
            'Set your weekly availability and copy your booking link.',
            'Share it. Bookings appear in Scheduler and as tasks with a 10-minute alert.',
            'Save your permanent Google Meet link in Settings → My Account → Google Meet: video bookings then get a '
            'real calendar invite with that link.',
        ],
        'tips': ['Bookings link themselves to the matching candidate or client contact.'],
        'related': ['tasks', 'interviews'],
        'keywords': ['booking', 'calendar', 'meeting', 'slot', 'google meet', 'calendly'],
    },

    # ── Database & engagement ───────────────────────────────────────────────
    {
        'key': 'talent-pool', 'title': 'Talent Pool', 'section': 'engage', 'roles': ADMIN,
        'where': 'Menu → Talent Pool',
        'summary': 'Your database in segments (industry, function, seniority, region) with how fresh each CV is and a log '
                   'of every profile update.',
        'when': 'To see what your database holds and how up to date it is.',
        'steps': [
            'Open it; the first visit scans the database.',
            'Click a matrix cell to see the people in it.',
            '"Recently updated" shows who refreshed their profile.',
        ],
        'tips': ['A CV counts as fresh for 6 months by default (⚙).'],
        'related': ['reengage', 'market-pulse', 'hotlist'],
        'keywords': ['segment', 'talent pool', 'cv freshness', 'database health'],
    },
    {
        'key': 'reengage', 'title': 'Re-engage', 'section': 'engage', 'roles': ADMIN,
        'where': 'Menu → Re-engage',
        'summary': 'Every 6 months each candidate gets one e-mail asking them to update their profile through a personal '
                   'link, so your database stays fresh.',
        'when': 'Set it up once; it runs by itself.',
        'steps': [
            'Switch it on and check the e-mail text (✎ Edit text).',
            'Choose the reminder if someone does not update (default 7 days).',
            'Updated profiles show in Talent Pool and in each candidate\'s update log.',
        ],
        'tips': ['People live in a pipeline (shared, interview, offer, placed) are never mailed.'],
        'related': ['talent-pool', 'jobmail'],
        'keywords': ['re-engage', 'update profile email', 'refresh cv'],
    },
    {
        'key': 'jobmail', 'title': 'Job-Match Mail', 'section': 'engage', 'roles': STAFF,
        'where': 'Job → Matching Candidates → ✉ E-mail top matches (or Menu → Job Mail)',
        'summary': 'E-mail a job to the best-matched people in your database. AI checks each person, you approve, and '
                   '"Yes, I\'m interested" puts them on the job with a call task.',
        'when': 'When a job is open and you want quick interest from your own database.',
        'steps': [
            'Open the job → Matching Candidates → **✉ E-mail top matches**.',
            'Run the AI check, edit the e-mail text if needed and **Send a test to me**.',
            'Approve the people to mail. Answers come back to the job.',
        ],
        'tips': ['Anyone gets at most one HireLab e-mail in 7 days (job mail > re-engage > newsletter).'],
        'related': ['matching', 'hotlist'],
        'keywords': ['job mail', 'email candidates', 'bulk email job', 'interested'],
    },
    {
        'key': 'hotlist', 'title': 'Hot List and My Calls', 'section': 'engage', 'roles': ALL,
        'where': 'Menu → Hot List (admins); My Calls (callers)',
        'summary': 'An engagement score for every person (interest in job mails, profile updates, clicks …) and a "who to '
                   'call today" list. Admins assign calls; callers log outcomes on My Calls.',
        'when': 'To call the people most likely to respond.',
        'steps': [
            'Admin: open Hot List, set a minimum score and assign people to callers (recruiters or freelancers).',
            'Caller: open **My Calls**, call, and log the outcome. It goes to the candidate\'s journey.',
        ],
        'tips': ['Scores fade over time, so recent activity counts most.'],
        'related': ['jobmail', 'talent-pool', 'freelancer-guide'],
        'keywords': ['hot list', 'who to call', 'my calls', 'engagement score'],
    },
    {
        'key': 'market-pulse', 'title': 'Market Pulse newsletter', 'section': 'engage', 'roles': ADMIN,
        'where': 'Menu → Market Pulse',
        'summary': 'A monthly newsletter to Talent Pool segments. Give 4–5 points; AI drafts subject, intro and sections. '
                   'Every mail also asks the reader to refresh their CV.',
        'when': 'Once a month, to stay in touch with your network.',
        'steps': [
            'Create a **New issue** and pick the audience segments.',
            'Write your points (or use **Talent Pulse** from Market Intel) and press **Draft with AI**. Edit freely.',
            '**Send a test to me**, then approve. Nothing is sent before you approve.',
        ],
        'tips': ['Results show sent, opened, clicked, profiles updated and unsubscribes.'],
        'related': ['talent-pulse', 'talent-pool'],
        'keywords': ['newsletter', 'market pulse', 'monthly mail'],
    },
    {
        'key': 'email-box', 'title': 'Email Box', 'section': 'engage', 'roles': STAFF,
        'where': 'Menu → Email Box',
        'summary': 'Your Gmail inside HireLab: inbox and sent mail, with candidate replies linked to their profile, '
                   'Gmail-style search, and campaigns (admins).',
        'when': 'For candidate and client e-mail without leaving HireLab.',
        'steps': [
            'Connect Gmail once (company mailbox in Settings; your own in My Account → My Email) with an App Password.',
            'Mail syncs every few minutes. Search with words or Gmail operators like from:, subject:, has:attachment, after:.',
            'Deleting here also moves the mail to Gmail\'s Trash; Restore brings it back.',
        ],
        'tips': ['If the App Password stops working, the status explains what to do.'],
        'related': ['settings', 'ai-agent'],
        'keywords': ['email', 'gmail', 'inbox', 'app password', 'mail sync'],
    },

    # ── Insight & market ────────────────────────────────────────────────────
    {
        'key': 'company-talent-map', 'title': 'Company Talent Map', 'section': 'insight', 'roles': STAFF,
        'where': 'Menu → Company Talent Map',
        'summary': 'Everyone you know at one company side by side, grouped by their profile group (Sales, Production, '
                   'Tender …) with each group\'s own fields, charts and a client view you can print as PDF.',
        'when': 'Before a client meeting, or when mapping a competitor.',
        'steps': [
            'Type the company name (spellings like "Ltd" / "Limited" match) and press **Show talent**.',
            'Use the chips: **All people** (the classic view) or one profile group to see its fields.',
            'Tick **Include former employees** to add people who used to work there.',
            'Tick **Client view** to hide names and contacts, then **Print / Save PDF**.',
        ],
        'tips': [
            'In client view, number fields appear only as bands (3+ people) and notes are never shown.',
            'Recruiters see only people from their own jobs.',
        ],
        'related': ['company-intel', 'profile-groups', 'market-intel'],
        'keywords': ['talent map', 'company map', 'competitor', 'client view', 'pdf'],
    },
    {
        'key': 'company-intel', 'title': 'Company Intelligence tab', 'section': 'insight', 'roles': STAFF,
        'where': 'Company Talent Map → Intelligence tab (or "Company intel ↗" on a profile or job)',
        'summary': 'What you know about one company: people by role and level, pay bands, notice periods, how open its '
                   'people are to a move, why they say no, market notes, and — for a client — why the client rejects '
                   'and why candidates turn the client down.',
        'when': 'Before pitching to a client or approaching a company\'s people.',
        'steps': [
            'Open the company on the Talent Map and click **Intelligence**.',
            'Pay bands show only for 3+ people ("n too small" otherwise).',
            '**Openness** counts people we contacted (Called or later).',
            'Tick **Client view** to share it: counts and pay bands only, no names, CTCs or notes.',
        ],
        'tips': ['Numbers refresh every 10 minutes; ↻ Refresh recalculates now.'],
        'related': ['company-talent-map', 'reasons', 'market-note', 'market-intel'],
        'keywords': ['company intelligence', 'pay band', 'openness', 'why no', 'employer brand'],
    },
    {
        'key': 'market-intel', 'title': 'Market Intel', 'section': 'insight', 'roles': STAFF,
        'where': 'Menu → Market Intel',
        'summary': 'The whole market across companies: salary benchmark by role × level × city, who is open to a move by '
                   'company (vs the previous period), top reasons for "no" and market signals.',
        'when': 'For salary discussions, client advice and planning sourcing.',
        'steps': [
            'Pick the period (3 / 6 / 12 months of real activity), domain and companies (all / clients / non-clients).',
            'Read the **Salary benchmark** (n ≥ 3 only) and download it as CSV.',
            '**Market movement** shows each company\'s "said no" % against the previous period.',
        ],
        'tips': ['Recruiters see numbers from their own jobs only.'],
        'related': ['talent-pulse', 'company-intel'],
        'keywords': ['salary benchmark', 'market', 'hike', 'ctc benchmark', 'city', 'trend'],
    },
    {
        'key': 'talent-pulse', 'title': 'Talent Pulse', 'section': 'insight', 'roles': ADMIN,
        'where': 'Market Intel → Draft this month\'s Talent Pulse',
        'summary': '4–5 market points written from your real numbers (each with its n) and opened as a Market Pulse '
                   'newsletter draft, plus a printable client version.',
        'when': 'Monthly, for your newsletter or client updates.',
        'steps': [
            'In Market Intel, press **Draft this month\'s Talent Pulse**.',
            '**Open in Market Pulse as a draft** creates a draft with the points; edit, test and approve there.',
            '**Client version (print / PDF)** gives a clean page for clients.',
        ],
        'tips': ['Nothing is sent automatically, and no candidate names or individual CTCs appear.'],
        'related': ['market-intel', 'market-pulse'],
        'keywords': ['talent pulse', 'monthly report', 'market report'],
    },
    {
        'key': 'insight-settings', 'title': 'Insight capture switches', 'section': 'insight', 'roles': ADMIN,
        'where': 'Settings → My Account → Insight capture',
        'summary': 'Switch on/off the reason picker, Market note card and 6:30 check; Company intelligence; and Market '
                   'intelligence — per agency. Also runs "Tag existing candidates".',
        'when': 'Once, or if you need to pause a feature.',
        'steps': [
            'Untick a switch to hide that feature for the whole agency; tick it again to bring it back.',
            'Use **Start tagging** to tag older candidates; watch the progress bar.',
        ],
        'tips': ['Nothing is deleted when you switch a feature off.'],
        'related': ['auto-tags', 'reasons', 'company-intel', 'market-intel'],
        'keywords': ['insight capture', 'switch off', 'tagging', 'settings insight'],
    },

    # ── Money ───────────────────────────────────────────────────────────────
    {
        'key': 'invoicing', 'title': 'Invoicing', 'section': 'money', 'roles': ADMIN,
        'where': 'Menu → Invoicing',
        'summary': 'Raise GST invoices for placements and see what is paid and what is still due.',
        'when': 'When a candidate is placed or joins.',
        'steps': [
            'Create the invoice for the placement (client, candidate, fee, GST).',
            'Mark payments as they arrive.',
            'For profit, expenses, GST/TDS and cash flow, open **Expenses & profit**.',
        ],
        'tips': ['Your company details for invoices are set in Settings (Workspace).'],
        'related': ['finance', 'crm'],
        'keywords': ['invoice', 'gst', 'bill', 'payment due'],
    },
    {
        'key': 'finance', 'title': 'Profit & Cash', 'section': 'money', 'roles': ADMIN,
        'where': 'Invoicing → Expenses & profit (or /finance)',
        'summary': 'Real profit without GST, expenses (with bills read by AI), GST and TDS, a 13-week cash forecast, '
                   'collections, freelancer commission and an Excel file for your CA.',
        'when': 'Monthly, and before talking to your CA.',
        'steps': [
            'Check ⚙ Setup once: client accounts e-mails, monthly repeating costs and budgets.',
            '**Overview:** revenue → costs → net profit, target and trend.',
            '**Cash & collections:** unpaid invoices by age; **✉ Remind** e-mails the client\'s accounts team.',
            '**Excel for CA:** P&L, invoices, expenses, GST and TDS for the period.',
        ],
        'tips': ['GST and TDS figures are estimates; check them with your CA before filing.'],
        'related': ['invoicing', 'freelancers'],
        'keywords': ['profit', 'expense', 'tds', 'cash flow', 'collections', 'ca'],
    },

    # ── Team & admin ────────────────────────────────────────────────────────
    {
        'key': 'tasks', 'title': 'Tasks', 'section': 'team', 'roles': STAFF,
        'where': 'Menu → Tasks',
        'summary': 'Reminders, interviews and follow-ups in one list, with lists, priorities, tags, My Day, repeating '
                   'tasks and an alert 10 minutes before a timed task.',
        'when': 'All day.',
        'steps': [
            'Type a task in plain words, e.g. "kal 11 baje Rahul ko call L&T ke liye #urgent". The date and time are picked up.',
            'Assign tasks with @name, set priority with !high, repeat with "har somvar".',
            'Use the daily plan for a ranked list of what to do today.',
        ],
        'tips': ['Any Team Chat message can be turned into a task.'],
        'related': ['home', 'ai-agent', 'scheduler'],
        'keywords': ['task', 'reminder', 'todo', 'follow up', 'kal', 'alert'],
    },
    {
        'key': 'team-chat', 'title': 'Team Chat', 'section': 'team', 'roles': STAFF,
        'where': 'Menu → Team Chat (and the Chat button on every page)',
        'summary': 'Channels and direct messages for your team, with threads, @mentions, files, candidate and job cards, '
                   'audio/video calls with screen sharing, and "Catch me up" summaries.',
        'when': 'For team conversations about jobs and candidates.',
        'steps': [
            'Create a channel (or link one to a job) and invite people.',
            'Share a candidate or job as a live card; each person sees only what they may see.',
            'Start an audio or video call from a conversation.',
            '**Catch me up** summarises what you missed.',
        ],
        'tips': ['Moving a stage from a candidate card in chat asks for a reason too.'],
        'related': ['tasks'],
        'keywords': ['chat', 'message', 'channel', 'video call', 'team'],
    },
    {
        'key': 'team-activity', 'title': 'Team Activity', 'section': 'team', 'roles': ADMIN,
        'where': 'Menu → Team Activity',
        'summary': 'Who worked when (active time and breaks), stage moves by recruiter, actions (calls, e-mails, CVs …) '
                   'and insights such as stuck candidates and untouched jobs.',
        'when': 'Daily or weekly team reviews.',
        'steps': ['Pick the dates and a team member.', 'Read time on app, moves, actions and insights.'],
        'tips': ['Activity is recorded from the day this feature was deployed.'],
        'related': ['users'],
        'keywords': ['team activity', 'productivity', 'who worked', 'recruiter report'],
    },
    {
        'key': 'freelancers', 'title': 'Freelancers', 'section': 'team', 'roles': ADMIN,
        'where': 'Admin Panel → Freelancers tab',
        'summary': 'Give jobs to freelance sourcers. They upload CVs for those jobs only; you see who sourced whom and '
                   'their commission is tracked.',
        'when': 'When you work with external sourcers.',
        'steps': [
            'Create the freelancer login and assign jobs.',
            'Their uploads notify the job\'s recruiter and admins; duplicates in a job are blocked.',
            'Set their commission in Profit & Cash (default 15% of the fee).',
        ],
        'tips': ['Freelancers cannot change stages or see other candidates.'],
        'related': ['freelancer-guide', 'finance', 'hotlist'],
        'keywords': ['freelancer', 'sourcer', 'commission', 'external recruiter'],
    },
    {
        'key': 'users', 'title': 'Team members and what recruiters can see', 'section': 'team', 'roles': ADMIN,
        'where': 'Settings → Admin → User Management',
        'summary': 'Add recruiters and admins. Recruiters work on the jobs assigned to them (plus the Central Database); '
                   'admins see everything.',
        'when': 'When someone joins or changes role.',
        'steps': [
            'Add the person and choose admin or recruiter.',
            'Assign jobs to recruiters on each job.',
            'Recruiters do not see CRM, BD, invoicing, Team Activity or company settings.',
        ],
        'tips': ['Each agency\'s data is fully separate from every other agency.'],
        'related': ['team-activity', 'settings'],
        'keywords': ['user', 'recruiter', 'access', 'permission', 'role', 'add team member'],
    },
    {
        'key': 'org-map', 'title': 'Org Map', 'section': 'team', 'roles': ADMIN,
        'where': 'Menu → Org Map',
        'summary': 'Map a company\'s people and reporting lines for talent mapping. Mapped people are linked to candidate '
                   'records when they enter a pipeline.',
        'when': 'For leadership mapping or a competitor study.',
        'steps': ['Add the company, then its people and who reports to whom.', 'Link a mapped person to a candidate when they enter a job.'],
        'tips': ['Mapped people are not candidates, so they do not clutter pipelines.'],
        'related': ['company-talent-map'],
        'keywords': ['org chart', 'org map', 'reporting line', 'mapping'],
    },
    {
        'key': 'learning', 'title': 'Learning (Electrical Industry Explorer)', 'section': 'team', 'roles': STAFF,
        'where': 'Menu → Learning',
        'summary': 'A field guide to the industry: who makes what, competitor heatmap, talent pools, roles and pay, '
                   'glossary, a call kit and your team\'s learning log. Editable per agency.',
        'when': 'Before calls in a new segment, or to train new team members.',
        'steps': ['Open Learning and browse the sections.', 'Use **Edit content** to add your agency\'s knowledge.'],
        'tips': ['This is industry knowledge; the Academy is about how to use HireLab.'],
        'related': ['academy-help'],
        'keywords': ['industry', 'glossary', 'call kit', 'switchgear', 'learning'],
    },
    {
        'key': 'ai-costs', 'title': 'AI keys and the AI cost meter', 'section': 'team', 'roles': ADMIN,
        'where': 'Settings → Admin → AI API Keys; Settings → My Account → My plan & usage',
        'summary': 'HireLab uses DeepSeek for most AI work (parsing, analysis, drafts, the Academy) and Groq for call '
                   'transcripts. The AI cost meter shows spending in ₹ per recruiter and per feature.',
        'when': 'At setup, and to watch costs.',
        'steps': ['Add the DeepSeek (and Groq) keys once.', 'Check the AI cost meter for the last 30 days.'],
        'tips': ['When the monthly AI limit is reached, AI features pause until next month or a higher limit.'],
        'related': ['settings'],
        'keywords': ['ai cost', 'deepseek key', 'api key', 'token', 'limit'],
    },
    {
        'key': 'backup', 'title': 'Backup', 'section': 'team', 'roles': ADMIN,
        'where': 'Account menu (your name, bottom left) → Full backup (database + CVs)',
        'summary': 'Download the complete database and all CV files to your own computer.',
        'when': 'Regularly, and before big changes.',
        'steps': ['Open Full backup.', 'Download the database file and the CVs zip, and keep both safely.'],
        'tips': ['Only the platform admin can download backups.'],
        'related': ['settings'],
        'keywords': ['backup', 'download data', 'export'],
    },
]

_KEYS = [a['key'] for a in ARTICLES]
assert len(_KEYS) == len(set(_KEYS)), 'duplicate academy key'
