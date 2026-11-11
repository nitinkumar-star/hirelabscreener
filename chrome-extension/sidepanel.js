// HireLab Assistant — side panel.
//
// Works on what the recruiter gives it: text pasted from a page and CV files
// they downloaded. There are no content scripts.
// v2.5: "Read this Naukri profile" reads the open Naukri candidate tab ONCE,
// only when the recruiter presses it (chrome.scripting, isolated world,
// nothing stays on the page), can press Naukri's own Download button, and
// attaches the downloaded CV (chrome.downloads + "Allow access to file URLs").
// v2.3: the LinkedIn tab reads the open linkedin.com/in/… tab ONCE, only when
// the recruiter presses "Extract this LinkedIn profile" (chrome.scripting,
// read-only, nothing injected that stays on the page).
//
// All calls go to the HireLab ATS with the recruiter's own login cookie.
(() => {
  'use strict';
  const ATS = 'https://hirelabscreener.onrender.com';
  const API = ATS + '/api';
  const MKEY = 'hla_mandate_id';
  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  let mandateId = '';
  let mandates = [];
  let hasCentral = false;
  let jobs = null;          // Find job tab: list from /assistant/jobs (null = not loaded yet)
  // The Central Database is the talent library, not a job: no JD, so no
  // scoring, review or screening — read the CV, check duplicates, save.
  const isCentral = () => mandateId === 'central';
  const midVal = () => (isCentral() ? 'central' : +mandateId);

  // ── network ──────────────────────────────────────────────────────────
  async function call(method, path, body, isForm) {
    let r;
    try {
      const opt = { method, credentials: 'include' };
      if (body !== undefined) {
        if (isForm) opt.body = body;
        else { opt.headers = { 'Content-Type': 'application/json' }; opt.body = JSON.stringify(body); }
      }
      r = await fetch(API + path, opt);
    } catch (e) {
      return { ok: false, kind: 'network', error: 'Could not reach HireLab. Check your internet connection.' };
    }
    const text = await r.text();
    let d = null;
    try { d = JSON.parse(text); } catch (e) {}
    if (r.status === 401) return { ok: false, kind: 'auth', error: 'Not signed in. Open ' + ATS + ' in this browser and log in, then press Analyse again.' };
    if (r.status === 404 && !(d && d.error)) return { ok: false, kind: 'missing', error: 'HireLab is not updated yet (' + path.split('?')[0] + ' missing). Deploy the latest ATS.' };
    if (!d) return { ok: false, kind: 'badbody', error: 'HireLab returned HTTP ' + r.status + '. Try again in a minute.' };
    if (!r.ok || d.ok === false || d.error === 'duplicate') {
      return Object.assign({}, d, { ok: false, status: r.status, kind: d.kind || (d.error === 'duplicate' ? 'duplicate' : 'server'),
                                     error: d.message || d.error || ('Failed (HTTP ' + r.status + ')') });
    }
    if (d.ok === undefined) d.ok = true;
    return d;
  }

  // ── mandates ─────────────────────────────────────────────────────────
  async function loadMandates() {
    const sel = $('mandate');
    const r = await call('GET', '/extension/mandates');
    if (!r.ok) {
      $('who').textContent = r.kind === 'auth' ? 'Not signed in to HireLab' : 'Offline';
      $('who').style.color = '#B3261E';
      sel.innerHTML = '<option value="">' + esc(r.kind === 'auth' ? 'Log in to HireLab first' : 'Could not load jobs') + '</option>';
      return;
    }
    $('who').textContent = 'Connected to HireLab';
    $('who').style.color = '';
    const all = r.mandates || [];
    hasCentral = all.some(m => m.is_central);
    mandates = all.filter(m => !m.is_central);
    const saved = await new Promise(res => chrome.storage.local.get([MKEY], v => res((v && v[MKEY]) || '')));
    const opt = m => '<option value="' + m.id + '">' + esc(m.role) + (m.client ? ' — ' + esc(m.client) : '') + '</option>';
    const recent = mandates.filter(m => m.is_recent), rest = mandates.filter(m => !m.is_recent);
    sel.innerHTML = '<option value="">Choose a job…</option>' +
      (hasCentral ? '<optgroup label="Talent pool"><option value="central">★ Central Database — save for later (no job)</option></optgroup>' : '') +
      (recent.length ? '<optgroup label="Recent">' + recent.map(opt).join('') + '</optgroup>' : '') +
      (rest.length ? '<optgroup label="' + (recent.length ? 'All open jobs' : 'Open jobs') + '">' + rest.map(opt).join('') + '</optgroup>' : '');
    if (saved && ((saved === 'central' && hasCentral) || mandates.some(m => String(m.id) === String(saved)))) sel.value = String(saved);
    mandateId = sel.value;
    loadRules();
  }
  // Switch job from anywhere (dropdown, or "Use this job" in Find job).
  function setMandate(v) {
    mandateId = String(v || '');
    $('mandate').value = mandateId;
    chrome.storage.local.set({ [MKEY]: mandateId });
    // A new job means old scores no longer apply.
    // (pushed is cleared too, so the same CV can also be pushed into the new job.)
    items.forEach(it => { it.score = it.review = it.dupes = it.chat = it.final = it.pushed = null; if (it.state === 'done') it.state = 'parsed'; });
    renderQueue(); renderResult();
    sResults = {}; renderSearch(); loadRules();
    if (jobs) renderJobs();
  }
  $('mandate').addEventListener('change', e => setMandate(e.target.value));

  // ── tabs ─────────────────────────────────────────────────────────────
  document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x === t));
    $('tab-cand').hidden = t.dataset.tab !== 'cand';
    $('tab-search').hidden = t.dataset.tab !== 'search';
    $('tab-jobs').hidden = t.dataset.tab !== 'jobs';
    $('tab-li').hidden = t.dataset.tab !== 'li';
    hideQuote();
    if (t.dataset.tab === 'jobs') openJobsTab();
    if (t.dataset.tab === 'li') liCheckTab();
  }));
  function showTab(name) { const b = document.querySelector('.tab[data-tab="' + name + '"]'); if (b) b.click(); }

  // ══════════════════════════════════════════════════════════════════════
  //  CANDIDATE: pasted profile text and/or downloaded CVs
  // ══════════════════════════════════════════════════════════════════════
  // item: {id, file, page, state: new|parsing|parsed|scoring|done|error, err,
  //        parsed, score, review, dupes, chat, pushed}
  let items = [];
  let current = null;
  let seq = 0;

  const OKEXT = /\.(pdf|docx|doc|rtf)$/i;
  function addFiles(list) {
    const files = Array.from(list || []).filter(f => OKEXT.test(f.name));
    if (!files.length) { cmsg('Only PDF, Word (.docx/.doc) or RTF files can be read.', 'err'); return; }
    files.forEach(f => items.push({ id: ++seq, file: f, page: '', state: 'new' }));
    if (!current) current = items[0];
    renderQueue();
    cmsg(files.length + ' CV' + (files.length > 1 ? 's' : '') + ' added. Press Analyse.', '');
  }
  const drop = $('drop');
  drop.addEventListener('click', () => $('file').click());
  drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('file').click(); } });
  $('file').addEventListener('change', e => { addFiles(e.target.files); e.target.value = ''; });
  ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', e => addFiles(e.dataTransfer.files));
  // Dropping anywhere on the panel also works.
  document.addEventListener('dragover', e => e.preventDefault());
  document.addEventListener('drop', e => { e.preventDefault(); if (!$('tab-cand').hidden && !drop.contains(e.target)) addFiles(e.dataTransfer.files); });

  function cmsg(t, cls) { const m = $('cmsg'); m.className = 'msg ' + (cls || ''); m.innerHTML = t; }

  const STATE = { new: 'waiting', parsing: 'reading…', parsed: 'read', scoring: 'scoring…', done: '', error: 'error' };
  function renderQueue() {
    const q = $('queue');
    if (items.length <= 1 && !(items[0] && items[0].file)) { q.innerHTML = ''; return; }
    q.innerHTML = items.map(it => {
      const nm = (it.parsed && it.parsed.fields.name) || (it.file ? it.file.name : 'Pasted profile');
      let s = STATE[it.state] || '';
      if (it.state === 'done' && it.final) s = Math.round(it.final.score) + '% · ' + it.final.verdict;
      else if (it.state === 'done' && it.score) s = Math.round(it.score.score) + '%';
      else if (it.state === 'done') s = 'ready to save';
      if (it.pushed) s += ' · ✓ pushed';
      return '<div class="qi' + (it === current ? ' on' : '') + '" data-id="' + it.id + '"><span class="n">' + esc(nm) +
        '</span><span class="s">' + esc(s) + '</span><button class="x" data-del="' + it.id + '" title="Remove">×</button></div>';
    }).join('');
  }
  $('queue').addEventListener('click', e => {
    const del = e.target.closest('[data-del]');
    if (del) {
      const id = +del.dataset.del;
      items = items.filter(x => x.id !== id);
      if (current && current.id === id) current = items[0] || null;
      renderQueue(); renderResult(); return;
    }
    const row = e.target.closest('.qi');
    if (!row) return;
    current = items.find(x => x.id === +row.dataset.id) || current;
    renderQueue(); renderResult();
  });

  $('clear').addEventListener('click', () => {
    items = []; current = null; $('page').value = ''; if (typeof nkStopWaiting === 'function') nkStopWaiting(); nkmsg('', '');
    renderQueue(); renderResult(); cmsg('', '');
  });

  $('go').addEventListener('click', async () => {
    if (!mandateId) { cmsg('Choose a job at the top first.', 'err'); return; }
    const page = $('page').value.trim();
    const fileItems = items.filter(x => x.file);
    if (page && fileItems.length > 1) {
      cmsg('Pasted text can go with one CV at a time. Remove the extra CVs, or clear the text box to analyse all CVs.', 'err');
      return;
    }
    if (!page && !items.length) { cmsg('Paste the Naukri profile text or add a CV.', 'err'); return; }
    if (page) {
      if (items.length === 1) { items[0].page = page; items[0].state = 'new'; items[0].pushed = null; }
      else items = [{ id: ++seq, file: null, page, state: 'new' }];
      current = items[0];
    }
    $('go').disabled = true;
    const todo = items.filter(x => x.state !== 'done');
    for (const it of todo) {
      current = current || it;
      await analyse(it);
    }
    $('go').disabled = false;
    const bad = items.filter(x => x.state === 'error').length;
    cmsg(bad ? bad + ' could not be analysed — open it to see why.' : 'Done.', bad ? 'err' : 'ok');
  });

  // Read the CV / pasted text once (no job needed). Returns false on error.
  async function parseItem(it) {
    if (it.parsed && it.parsedPage === it.page) return true;
    it.err = ''; it.state = 'parsing'; renderQueue(); if (it === current) renderResult();
    const fd = new FormData();
    if (it.page) fd.append('page_text', it.page);
    if (it.file) fd.append('cv', it.file, it.file.name);
    const p = await call('POST', '/assistant/parse', fd, true);
    if (!p.ok) { it.state = 'error'; it.err = p.error; renderQueue(); if (it === current) renderResult(); return false; }
    it.parsed = p; it.parsedPage = it.page;
    it.fields = Object.assign({}, p.fields);
    it.state = 'parsed';
    return true;
  }

  async function analyse(it) {
    it.err = '';
    if (!(await parseItem(it))) return;
    const p = it.parsed, f = it.fields;
    if (isCentral()) {
      // Central Database: no JD to score against — just the duplicate check.
      it.state = 'scoring'; renderQueue(); if (it === current) renderResult();
      const dupes = await call('POST', '/assistant/dupes', { mandate_id: 'central', name: f.name, phone: f.phone, email: f.email,
                                                             company: f.company, text: p.candidate_text.slice(0, 1500) });
      it.dupes = dupes.ok ? dupes.hits || [] : [];
      it.score = it.review = it.chat = it.final = null;
      it.state = 'done'; renderQueue(); if (it === current) renderResult();
      return;
    }
    it.state = 'scoring'; renderQueue(); if (it === current) renderResult();
    const [dupes, score] = await Promise.all([
      call('POST', '/assistant/dupes', { mandate_id: +mandateId, name: f.name, phone: f.phone, email: f.email,
                                         company: f.company, text: p.candidate_text.slice(0, 1500) }),
      call('POST', '/extension/score-match', {
        mandate_id: +mandateId, candidate_text: p.candidate_text, candidate_skills: f.key_skills || [],
        resume_text: p.resume_text || '', cv_status: p.has_cv ? 'ok' : 'bypassed', bypass_cv: !p.has_cv,
        run_intent_judge: false })
    ]);
    it.dupes = dupes.ok ? dupes.hits || [] : [];
    if (!score.ok) { it.state = 'error'; it.err = score.error; renderQueue(); if (it === current) renderResult(); return; }
    it.score = score; it.final = null; it.review = { loading: true };
    it.state = 'done'; renderQueue(); if (it === current) renderResult();
    const rv = await call('POST', '/extension/explain', { mandate_id: +mandateId, candidate_text: p.candidate_text,
      resume_text: p.resume_text || '', cv_status: p.has_cv ? 'ok' : 'bypassed', base_score: score.score });
    it.review = rv;
    if (rv.ok) {
      it.final = rv.final || null;
      it.chat = { log: [], quotes: [], busy: false, rules: [] };
    }
    renderQueue(); if (it === current) renderResult();
  }

  // ── result view ──────────────────────────────────────────────────────
  const SUBS = { 'Strong Match': 'Strong alignment with this job.', 'Good Fit': 'Solid match — worth a call.',
                 'Partial Match': 'Gaps below — check what is missing.', 'Not Suitable': 'Significant mismatch.' };

  function renderResult() {
    const box = $('result');
    const it = current;
    hideQuote();
    if (!it) { box.innerHTML = ''; return; }
    if (it.state === 'error') { box.innerHTML = '<div class="card"><div class="msg err">' + esc(it.err) + '</div></div>'; return; }
    if (it.state === 'new') { box.innerHTML = ''; return; }
    if (it.state === 'parsing') { box.innerHTML = '<div class="card"><div class="msg"><span class="spin"></span>Reading ' + (it.file ? 'the CV' : 'the profile') + '…</div></div>'; return; }
    const f = it.fields || {}, p = it.parsed || {};
    let h = '<div class="card who"><h2>' + esc(f.name || 'Unnamed candidate') + '</h2><p>' +
      esc([f.designation, f.company].filter(Boolean).join(' at ')) + '</p><div class="facts">' +
      [f.experience ? f.experience + ' yrs' : '', f.ctc_current ? f.ctc_current + ' LPA' : '',
       f.notice_period ? f.notice_period + 'd notice' : '',
       f.gender, f.location, f.phone ? '📞 ' + f.phone : '', f.email ? '✉ ' + f.email : '']
        .filter(Boolean).map(x => '<span>' + esc(x) + '</span>').join('') + '</div>' +
      '<details><summary>Edit details before push</summary>' + formHtml(f) + '</details></div>';

    if (it.dupes && it.dupes.length) h += dupHtml(it.dupes);
    if (isCentral() && it.state === 'done') h += '<div class="cdb">★ <b>Central Database</b> — no job selected, so there is no score. ' +
      'Check the details below and save. To see which open job fits this person, open <a href="#" id="tojobs">Find job</a>.</div>';
    if (it.state === 'scoring') h += '<div class="card"><div class="msg"><span class="spin"></span>' +
      (isCentral() ? 'Checking who is already in HireLab…' : 'Scoring against the job…') + '</div></div>';
    if (it.score) h += '<div class="card">' + scoreHtml(it) + '</div>';
    if (it.review) h += '<div class="card" id="rv">' + reviewHtml(it) + '</div>';
    if (it.chat) h += '<div class="card" id="chat">' + chatHtml(it) + '</div>';
    if (p.resume_text) h += '<div class="card"><details><summary>CV text — select a line to quote it in the chat</summary>' +
      '<div class="cv" id="cvtext">' + esc(p.resume_text) + '</div></details></div>';
    h += '<div class="card push">' + pushHtml(it) + '</div>';
    box.innerHTML = h;
    wireResult(it);
  }

  const FORM = [['name', 'Name', 'w'], ['phone', 'Phone'], ['email', 'Email'], ['company', 'Company'], ['designation', 'Designation'],
                ['experience', 'Experience (yrs)'], ['ctc_current', 'Current CTC (LPA)'], ['ctc_expected', 'Expected CTC (LPA)'],
                ['notice_period', 'Notice (days)'], ['location', 'Location'], ['preferred_location', 'Preferred location'],
                ['qualification', 'Qualification'], ['gender', 'Gender'], ['key_skills', 'Key skills (comma separated)', 'w']];
  const GENDERS = [['', 'Not known'], ['Male', 'Male'], ['Female', 'Female'], ['Other', 'Other']];
  function formHtml(f) {
    return '<div class="form">' + FORM.map(([k, l, w]) => {
      const v = k === 'key_skills' ? (f.key_skills || []).join(', ') : (f[k] == null ? '' : f[k]);
      if (k === 'gender') return '<label>' + l + '<select data-f="gender">' + GENDERS.map(([gv, gl]) =>
        '<option value="' + gv + '"' + (gv === v ? ' selected' : '') + '>' + gl + '</option>').join('') + '</select></label>';
      return '<label class="' + (w || '') + '">' + l + '<input data-f="' + k + '" value="' + esc(v) + '"></label>';
    }).join('') + '</div>';
  }

  function dupHtml(hits) {
    const here = hits.filter(h => h.where === 'this_job' && h.level !== 'name');
    const link = h => '<a href="' + ATS + '/#mandate/' + h.mandate_id + '/profiles/' + h.candidate_id + '" target="_blank" rel="noopener">' +
      esc(h.where === 'this_job' ? (isCentral() ? 'Central DB' : 'This job') : h.where === 'pool' ? 'Central DB' : (h.job || 'Another job') + (h.client ? ' — ' + h.client : '')) +
      (h.stage ? ' · ' + esc(h.stage) : '') + '</a>' + (h.level === 'name' ? ' (same name only — check)' : '');
    if (here.length && isCentral()) return '<div class="dup here">⧉ <b>Already in the Central Database</b> — ' + here.map(link).join(', ') +
      '. Saving again with the same e-mail updates that profile.</div>';
    if (here.length) return '<div class="dup here">⧉ <b>Already in this job</b> — ' + here.map(link).join(', ') +
      '. Pushing again updates that profile; stage and notes stay as they are.</div>';
    return '<div class="dup else">⧉ Also in HireLab: ' + hits.map(link).join(' · ') + '</div>';
  }

  function scoreHtml(it) {
    const d = it.score, fin = it.final;
    const sc = fin ? fin.score : d.score, verdict = fin ? fin.verdict : d.verdict, col = (fin ? fin.verdict_color : d.verdict_color) || '#8A92A0';
    const C = 2 * Math.PI * 27;
    let warn = '';
    if ((d.missing_groups || []).length) warn += '<div class="warn err">✗ Requirement not met: ' + d.missing_groups.map(g => esc((g.terms || []).join(' / '))).join('; ') + '</div>';
    if (!it.parsed.has_cv) warn += '<div class="warn">⚠ No CV yet — this score uses the pasted profile only. Add the CV to verify skills.</div>';
    return warn + '<div class="score"><div class="ring"><svg viewBox="0 0 64 64"><circle class="t" cx="32" cy="32" r="27"/>' +
      '<circle class="f" cx="32" cy="32" r="27" style="stroke:' + col + ';stroke-dasharray:' + C + ';stroke-dashoffset:' + (C - C * Math.min(100, sc) / 100) + '"/></svg>' +
      '<b>' + Math.round(sc) + '</b></div><div><span class="vb" style="background:' + col + '22;color:' + col + ';border-color:' + col + '55">' + esc(verdict || '—') + '</span>' +
      '<div class="sub">' + esc(SUBS[verdict] || '') + '</div>' +
      (fin && (fin.caps || []).length ? '<div class="caps">' + fin.caps.map(c => '⛔ ' + esc(c)).join('<br>') + '</div>' : '') +
      '<div class="sub">' + esc(d.mandate_role || '') + (d.mandate_client ? ' · ' + esc(d.mandate_client) : '') + '</div></div></div>' +
      '<div class="bars">' + bar('Semantic', d.embed_used ? d.semantic_pct : null) + bar('Skills', d.skills_defined ? d.skill_pct : null) + '</div>' +
      (fin ? '<div class="sub" style="margin-top:6px">Final = 65% match score (' + Math.round(d.score) + ') + 35% AI review</div>' : '');
  }
  function bar(name, v) {
    return '<div class="bar"><i>' + name + '</i><div><s style="width:' + (v == null ? 0 : Math.min(100, v)) + '%"></s></div><b>' + (v == null ? 'n/a' : Math.round(v) + '%') + '</b></div>';
  }

  function reviewHtml(it) {
    const res = it.review;
    if (res.loading) return '<div class="lbl">AI review</div><div class="msg"><span class="spin"></span>Reading the CV against the job breakdown…</div>';
    if (!res.ok) {
      const soft = res.kind === 'nojd' || res.kind === 'nokey';
      return '<div class="lbl">AI review</div><div class="msg ' + (soft ? '' : 'err') + '">' +
        esc(res.kind === 'nojd' ? 'No AI review: this job has no JD, boolean or must-have skills. Add them in HireLab → Mandate Setup.' : res.error) + '</div>' +
        (soft ? '' : '<div class="row"><button class="btn sm" id="rvretry">↻ Retry AI review</button></div>');
    }
    const ex = res.explanation || {}, bk = res.breakdown || {};
    const icon = r => r.status === 'cv' && r.verified !== false ? '<span class="rq ok">✓</span>'
      : r.status === 'cv' ? '<span class="rq unk" title="AI says it is in the CV, but its quote was not found">?</span>'
      : r.status === 'profile' ? '<span class="rq mid">~</span>' : r.status === 'missing' ? '<span class="rq no">✗</span>' : '<span class="rq unk">?</span>';
    const label = r => r.status === 'cv' ? (r.verified === false ? 'AI says in CV — quote not found' : 'in CV')
      : r.status === 'profile' ? 'profile only' : r.status === 'missing' ? 'not found' : 'unclear';
    const rows = l => l.map(r => '<div class="req">' + icon(r) + '<span>' + esc(r.skill) + ' <i>' + label(r) + '</i>' +
      (r.evidence ? '<span class="ev">“' + esc(r.evidence) + '”</span>' : '') + '</span></div>').join('');
    let h = '<div class="lbl">AI review <em>' + (res.breakdown_status === 'edited' ? 'your edited job breakdown' : 'AI job breakdown') + '</em></div>';
    if (ex.summary) h += '<div>' + esc(ex.summary) + '</div>';
    if ((ex.core || []).length) h += '<div class="tier core">Core — must have</div>' + rows(ex.core);
    if ((ex.important || []).length) h += '<div class="tier imp">Important — strongly preferred</div>' + rows(ex.important);
    const nice = (bk.nice || []).map(n => n.skill);
    if (nice.length) h += '<div class="tier nice">Nice-to-have</div><div class="chips">' +
      nice.map(n => '<span class="chip' + ((ex.nice_found || []).indexOf(n) >= 0 ? ' ok">+ ' : '">') + esc(n) + '</span>').join('') + '</div>';
    const ck = ex.checks || {};
    const cell = (n, c) => { c = c || {}; const cls = !c.status || c.status === 'unknown' ? '' : c.status === 'ok' ? 'ok' : 'bad';
      const w = { ok: '✓', low: '✗ low', high: '✗ high', over: '✗ over', relocation: '~ relocate', unknown: '?' }[c.status] || '?';
      return '<div class="' + cls + '" title="' + esc(c.note || '') + '">' + n + '<b>' + esc(c.value || '—') + ' ' + w + '</b></div>'; };
    h += '<div class="tier" style="color:#0E2A47">Checks</div><div class="chk">' + cell('Experience', ck.experience) + cell('CTC', ck.ctc) + cell('Location', ck.location) + '</div>';
    if ((ex.risks || []).length) h += '<div class="tier" style="color:#B3261E">Risks</div><ul class="lst">' + ex.risks.map(r => '<li>' + esc(r) + '</li>').join('') + '</ul>';
    if ((ex.questions || []).length) h += '<div class="tier" style="color:#2A73C5">Ask on the call</div><ul class="lst">' + ex.questions.map(r => '<li>' + esc(r) + '</li>').join('') + '</ul>';
    const brief = (t, l) => l && l.length ? '<p><b>' + t + ':</b> ' + l.map(x => esc(x.skill)).join(' · ') + '</p>' : '';
    h += '<details><summary>How the AI read this job</summary>' + (res.breakdown_stale ? '<p style="color:#9A6512">⚠ The JD changed after this breakdown was edited.</p>' : '') +
      (bk.summary ? '<p>' + esc(bk.summary) + '</p>' : '') + brief('Core', bk.core) + brief('Important', bk.important) + brief('Nice-to-have', bk.nice) +
      '<p><a href="' + ATS + '/#mandate/' + encodeURIComponent(res.mandate_id || mandateId) + '/breakdown" target="_blank" rel="noopener">Correct it in HireLab →</a></p></details>';
    return h;
  }

  // ── discuss the review ───────────────────────────────────────────────
  function chatHtml(it) {
    const c = it.chat;
    const log = c.log.map(m => {
      if (m.role === 'sys') return '<div class="cm sys">' + m.html + '</div>';
      if (m.role === 'err') return '<div class="cm err">' + esc(m.text) + '</div>';
      return '<div class="cm ' + m.role + '">' + (m.quotes || []).map(q => '<q>“' + esc(q.length > 220 ? q.slice(0, 220) + '…' : q) + '”</q>').join('') + esc(m.text || '') + '</div>';
    }).join('');
    return '<div class="lbl">Discuss with AI <em>disagree with a point? quote the CV</em></div><div class="log" id="clog">' + log + '</div>' +
      '<div class="qs">' + c.quotes.map((q, i) => '<div class="qc"><span>“' + esc(q.length > 160 ? q.slice(0, 160) + '…' : q) + '”</span><b data-q="' + i + '">×</b></div>').join('') + '</div>' +
      '<textarea id="cin" rows="2" placeholder="e.g. He has done SCADA — see the quoted line. Enter to send."></textarea>' +
      '<div class="row"><button class="btn sm" id="csend"' + (c.busy ? ' disabled' : '') + '>Send</button></div>';
  }

  async function sendChat(it) {
    const c = it.chat, cin = $('cin');
    const text = (cin.value || '').trim();
    if (c.busy || (!text && !c.quotes.length)) return;
    const quotes = c.quotes.slice();
    const history = c.log.filter(m => m.role === 'me' || m.role === 'ai').slice(-8)
      .map(m => ({ role: m.role, text: (m.quotes && m.quotes.length ? '[quoted] ' + m.quotes.join(' / ') + ' — ' : '') + (m.text || '') }));
    c.log.push({ role: 'me', text, quotes }); c.quotes = []; c.busy = true;
    c.log.push({ role: 'sys', html: '<span class="spin"></span>Re-reading the CV…' });
    renderResult();
    const res = await call('POST', '/extension/explain/chat', { mandate_id: +mandateId, message: text, quotes, history,
      explanation: it.review.explanation, candidate_text: it.parsed.candidate_text, resume_text: it.parsed.resume_text || '',
      base_score: it.score.score });
    c.busy = false; c.log.pop();
    if (!res.ok) { c.log.push({ role: 'err', text: res.error || 'Chat failed.' }); renderResult(); return; }
    c.log.push({ role: 'ai', text: res.reply });
    if ((res.changed || []).length && res.explanation) {
      c.log.push({ role: 'sys', html: '✓ Review updated' });
      it.review.explanation = res.explanation;
    }
    if (res.final) it.final = res.final;
    if (res.rule_added) {
      c.rules = res.rules || [];
      c.log.push({ role: 'sys', html: '📌 Rule saved for this job: “' + esc(res.rule_added) + '” — used in every review and the search screener. <a data-undo="' + esc(res.rule_added) + '">Undo</a>' });
      loadRules();
    }
    renderQueue(); renderResult();
    const cl = $('clog'); if (cl) cl.scrollTop = cl.scrollHeight;
  }

  // ── push ─────────────────────────────────────────────────────────────
  function pushHtml(it) {
    if (it.pushed) return '<div class="msg ok">✓ ' + esc(it.pushed.msg) + '</div><a class="btn sm" target="_blank" rel="noopener" href="' + ATS + '/#mandate/' + encodeURIComponent(it.pushed.mid) + '/profiles/' + it.pushed.id + '">Open in HireLab</a>';
    if (!it.file) return '<div class="msg">Add the downloaded CV to ' + (isCentral() ? 'save this candidate' : 'push this candidate to HireLab') + '.</div>';
    if (it.state !== 'done') return '<div class="msg">' + (isCentral() ? 'Saving' : 'Push') + ' is available after the analysis.</div>';
    if (isCentral()) return '<div class="msg" id="pmsg">Saves to the <b>Central Database</b> with the CV attached.</div><button class="btn navy" id="push">Save to Central DB</button>';
    return '<div class="msg" id="pmsg">Pushes to <b>' + esc(mandateName()) + '</b> with the CV attached.</div><button class="btn navy" id="push">Push to HireLab</button>';
  }
  function mandateName() {
    if (isCentral()) return 'Central Database';
    const m = mandates.find(x => String(x.id) === String(mandateId)); return m ? m.role + (m.client ? ' — ' + m.client : '') : 'this job';
  }

  function readForm(it) {
    document.querySelectorAll('#result [data-f]').forEach(inp => {
      const k = inp.dataset.f, v = inp.value.trim();
      it.fields[k] = k === 'key_skills' ? v.split(',').map(s => s.trim()).filter(Boolean) : v;
    });
  }

  async function push(it) {
    readForm(it);
    const f = it.fields, pm = $('pmsg'), btn = $('push');
    if (!f.name) { pm.className = 'msg err'; pm.textContent = 'Name is missing — fill it in "Edit details".'; return; }
    if (!f.phone && !f.email) { pm.className = 'msg err'; pm.textContent = 'No phone or e-mail found in the CV — add one in "Edit details".'; return; }
    btn.disabled = true; pm.className = 'msg'; pm.innerHTML = '<span class="spin"></span>' + (isCentral() ? 'Saving…' : 'Pushing…');
    const r = await call('POST', '/extension/push', {
      mandate_id: midVal(), name: f.name, phone: f.phone, email: f.email, company: f.company, designation: f.designation,
      experience: f.experience, ctc_current: f.ctc_current, ctc_expected: f.ctc_expected, notice_period: f.notice_period,
      location: f.location, preferred_location: f.preferred_location, qualification: f.qualification, gender: f.gender || '',
      key_skills: f.key_skills || [], career_summary: f.career_summary || '', work_history: f.work_history || [],
      ai_insight_cv: (it.review && it.review.explanation && it.review.explanation.summary) || '', naukri_ref: '' });
    if (!r.ok || !r.candidate_id) { btn.disabled = false; pm.className = 'msg err'; pm.textContent = r.error || 'Push failed.'; return; }
    pm.innerHTML = '<span class="spin"></span>Attaching the CV…';
    const fd = new FormData(); fd.append('cv', it.file, it.file.name);
    const up = await call('POST', '/candidates/' + r.candidate_id + '/cv', fd, true);
    it.pushed = { id: r.candidate_id, mid: r.mandate_id || mandateId,
                  msg: (r.action === 'updated' ? 'Updated the existing profile' : 'Added to ' + mandateName()) +
                  (up.ok ? ' with the CV.' : ' — but the CV upload failed: ' + (up.error || '') + '. Attach it in HireLab.') };
    renderQueue(); renderResult();
  }

  function wireResult(it) {
    const rr = $('rvretry');
    if (rr) rr.addEventListener('click', async () => {
      it.review = { loading: true }; renderResult();
      it.review = await call('POST', '/extension/explain', { mandate_id: +mandateId, candidate_text: it.parsed.candidate_text,
        resume_text: it.parsed.resume_text || '', cv_status: it.parsed.has_cv ? 'ok' : 'bypassed', base_score: it.score.score });
      if (it.review.ok) { it.final = it.review.final || null; it.chat = it.chat || { log: [], quotes: [], busy: false, rules: [] }; }
      renderQueue(); renderResult();
    });
    const cin = $('cin');
    if (cin) {
      cin.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(it); } });
      $('csend').addEventListener('click', () => sendChat(it));
      document.querySelectorAll('[data-q]').forEach(b => b.addEventListener('click', () => { it.chat.quotes.splice(+b.dataset.q, 1); renderResult(); }));
      document.querySelectorAll('#clog [data-undo]').forEach(a => a.addEventListener('click', async () => {
        const keep = (it.chat.rules || []).filter(r => r !== a.dataset.undo);
        const res = await call('POST', '/extension/screen/rules', { mandate_id: +mandateId, rules: keep });
        if (res.ok) { it.chat.rules = res.rules || keep; a.replaceWith(document.createTextNode('(removed)')); loadRules(); }
      }));
    }
    document.querySelectorAll('#result [data-f]').forEach(inp => inp.addEventListener('change', () => readForm(it)));
    const pb = $('push'); if (pb) pb.addEventListener('click', () => push(it));
    const tj = $('tojobs'); if (tj) tj.addEventListener('click', e => { e.preventDefault(); showTab('jobs'); });
  }

  // Select a line in the CV text → "Quote in chat".
  const qbtn = $('quote');
  function hideQuote() { qbtn.hidden = true; }
  document.addEventListener('mouseup', e => {
    if (e.target === qbtn) return;
    setTimeout(() => {
      const sel = window.getSelection(), cv = $('cvtext');
      const text = sel ? String(sel).trim() : '';
      if (!cv || !current || !current.chat || text.length < 6 || text.length > 1500 || !sel.rangeCount || !cv.contains(sel.anchorNode)) { hideQuote(); return; }
      const r = sel.getRangeAt(0).getBoundingClientRect();
      qbtn.style.left = Math.max(8, Math.min(window.innerWidth - 150, r.left)) + 'px';
      qbtn.style.top = Math.min(window.innerHeight - 40, r.bottom + 6) + 'px';
      qbtn.hidden = false; qbtn._text = text;
    }, 10);
  });
  qbtn.addEventListener('mousedown', e => {
    e.preventDefault();
    const it = current; if (!it || !it.chat) return;
    const t = (qbtn._text || '').replace(/\s+/g, ' ').trim().slice(0, 700);
    if (t && it.chat.quotes.indexOf(t) < 0) { if (it.chat.quotes.length >= 3) it.chat.quotes.shift(); it.chat.quotes.push(t); }
    hideQuote(); renderResult();
    const cin = $('cin'); if (cin) { cin.scrollIntoView({ block: 'center' }); cin.focus(); }
  });
  document.addEventListener('scroll', hideQuote, true);

  // ══════════════════════════════════════════════════════════════════════
  //  SEARCH PAGE: pasted results → open / maybe / skip
  // ══════════════════════════════════════════════════════════════════════
  let sProfiles = [], sResults = {}, sFilter = 'all', sBusy = false, sRules = [], sLog = [];
  function smsg(t, cls) { const m = $('smsg'); m.className = 'msg ' + (cls || ''); m.innerHTML = t; }

  async function loadRules() {
    if (!mandateId || isCentral()) { $('rules').innerHTML = ''; return; }
    const r = await call('GET', '/extension/screen/state?mandate_id=' + encodeURIComponent(mandateId));
    if (!r.ok) { $('rules').innerHTML = ''; return; }
    sRules = r.rules || [];
    $('rules').innerHTML = sRules.length
      ? '<b>Rules for this job</b><ol>' + sRules.map(x => '<li>' + esc(x) + '</li>').join('') + '</ol>'
      : '<span class="msg">No screening rules for this job yet — add them in the chat after screening.</span>';
  }

  $('sclear').addEventListener('click', () => { $('spage').value = ''; sProfiles = []; sResults = {}; renderSearch(); smsg('', ''); });

  $('sgo').addEventListener('click', async () => {
    if (sBusy) return;
    if (!mandateId) { smsg('Choose a job at the top first.', 'err'); return; }
    if (isCentral()) { smsg('Screening needs a job with a JD. Pick a job at the top (the Central Database has no JD).', 'err'); return; }
    const text = $('spage').value.trim();
    if (text.length < 80) { smsg('Paste the whole search results page (Ctrl+A, Ctrl+C on the page).', 'err'); return; }
    sBusy = true; $('sgo').disabled = true; sResults = {}; renderSearch();
    smsg('<span class="spin"></span>Finding the profiles in the pasted text…');
    const sp = await call('POST', '/assistant/split', { text });
    if (!sp.ok) { sBusy = false; $('sgo').disabled = false; smsg(esc(sp.error), 'err'); return; }
    sProfiles = sp.profiles;
    await screenAll();
  });

  async function screenAll() {
    sBusy = true; $('sgo').disabled = true;
    smsg('<span class="spin"></span>Checking who is already in HireLab…');
    const dr = await call('POST', '/extension/screen/dupes', { mandate_id: +mandateId,
      cards: sProfiles.map(p => ({ key: p.key, name: p.name, ref: '', text: p.text.slice(0, 1200) })) });
    const dupes = {};
    if (dr.ok) (dr.dupes || []).forEach(d => { dupes[d.key] = d; });
    const sure = p => { const d = dupes[p.key]; return d && d.best.where === 'this_job' && d.best.level !== 'name'; };
    sProfiles.filter(sure).forEach(p => {
      const d = dupes[p.key];
      sResults[p.key] = { key: p.key, verdict: 'dup', score: null, dup: d, reason: 'Already in this job' + (d.best.stage ? ' · ' + d.best.stage : '') };
    });
    const todo = sProfiles.filter(p => !sure(p));
    const batches = [];
    for (let i = 0; i < todo.length; i += 10) batches.push(todo.slice(i, i + 10));
    let done = 0, failed = 0, lastErr = '', next = 0;
    renderSearch();
    smsg('<span class="spin"></span>Screening ' + todo.length + ' profiles…');
    async function worker() {
      while (next < batches.length) {
        const b = batches[next++];
        const res = await call('POST', '/extension/screen', { mandate_id: +mandateId, profiles: b.map(p => ({ key: p.key, name: p.name, text: p.text })) });
        if (res.ok) (res.results || []).forEach(r => { sResults[r.key] = Object.assign({}, r, { dup: dupes[r.key] || null }); });
        else { failed += b.length; lastErr = res.error; if (res.kind === 'auth') next = batches.length; }
        done += b.length; renderSearch();
        smsg('<span class="spin"></span>Screened ' + Math.min(done, todo.length) + ' / ' + todo.length + '…');
      }
    }
    await Promise.all(Array.from({ length: Math.min(3, batches.length) }, worker));
    sBusy = false; $('sgo').disabled = false;
    const c = counts();
    smsg(failed ? '⚠ ' + failed + ' could not be screened: ' + esc(lastErr)
                : 'Done: ' + c.open + ' open · ' + c.maybe + ' maybe · ' + c.skip + ' skip' + (c.dup ? ' · ' + c.dup + ' already in this job' : '') +
                  '. Download the CVs of the ones you like and add them in the Candidate tab.', failed ? 'err' : 'ok');
    $('schat').hidden = false;
  }

  function counts() {
    const c = { open: 0, maybe: 0, skip: 0, dup: 0, unknown: 0 };
    sProfiles.forEach(p => { const r = sResults[p.key]; if (r) c[r.verdict] = (c[r.verdict] || 0) + 1; });
    return c;
  }

  function renderSearch() {
    const f = $('sfilters'), l = $('slist');
    if (!sProfiles.length) { f.hidden = true; l.innerHTML = ''; return; }
    const c = counts();
    f.hidden = false;
    const chip = (k, t, n) => '<span class="chip f' + (sFilter === k ? ' on' : '') + '" data-sf="' + k + '">' + t + ' ' + n + '</span>';
    f.innerHTML = chip('all', 'All', sProfiles.length) + chip('open', 'Open', c.open) + chip('maybe', 'Maybe', c.maybe) +
      chip('skip', 'Skip', c.skip) + (c.dup ? chip('dup', 'In job', c.dup) : '');
    const order = { open: 0, maybe: 1, unknown: 2, skip: 3, dup: 4 };
    const rows = sProfiles.slice().sort((a, b) => {
      const ra = sResults[a.key], rb = sResults[b.key];
      if (!ra || !rb) return (ra ? 0 : 1) - (rb ? 0 : 1);
      return (order[ra.verdict] - order[rb.verdict]) || ((rb.score || 0) - (ra.score || 0));
    }).filter(p => sFilter === 'all' || (sResults[p.key] && sResults[p.key].verdict === sFilter));
    const W = { open: '✓ OPEN', maybe: '? MAYBE', skip: '✗ SKIP', dup: '⧉ IN JOB', unknown: '· ?' };
    l.innerHTML = rows.map(p => {
      const r = sResults[p.key];
      if (!r) return '<div class="srow"><div class="t"><b>' + esc(p.name) + '</b><span class="pill unknown">' + (sBusy ? '…' : '—') + '</span></div></div>';
      const d = r.dup && r.dup.best;
      const dl = d ? '<div class="d">⧉ <a target="_blank" rel="noopener" href="' + ATS + '/#mandate/' + d.mandate_id + '/profiles/' + d.candidate_id + '">' +
        esc(d.where === 'this_job' ? 'This job' : d.where === 'pool' ? 'Central DB' : (d.job || 'Another job')) + (d.stage ? ' · ' + esc(d.stage) : '') + '</a>' +
        (d.level === 'name' ? ' (same name only — check)' : '') + '</div>' : '';
      return '<div class="srow"><div class="t"><b>' + esc(p.name) + '</b><span class="pill ' + r.verdict + '">' + W[r.verdict] +
        (r.score != null ? ' ' + r.score : '') + '</span></div><div class="r">' + esc(r.reason || '') + '</div>' + dl + '</div>';
    }).join('') || '<div class="empty">Nothing in this filter.</div>';
  }
  $('sfilters').addEventListener('click', e => { const c = e.target.closest('[data-sf]'); if (c) { sFilter = c.dataset.sf; renderSearch(); } });

  async function sendRule() {
    const inp = $('sin'), text = inp.value.trim();
    if (!text || !mandateId || isCentral()) return;
    inp.value = '';
    sLog.push({ role: 'me', text }); sLog.push({ role: 'sys', html: '<span class="spin"></span>Thinking…' }); drawSLog();
    const context = sProfiles.filter(p => sResults[p.key]).slice(0, 40)
      .map(p => ({ name: p.name, verdict: sResults[p.key].verdict, score: sResults[p.key].score, reason: sResults[p.key].reason }));
    const r = await call('POST', '/extension/screen/chat', { mandate_id: +mandateId, message: text, context });
    sLog.pop();
    if (!r.ok) { sLog.push({ role: 'err', text: r.error }); drawSLog(); return; }
    sLog.push({ role: 'ai', text: r.reply });
    if (r.changed) {
      sLog.push({ role: 'sys', html: 'Rules updated. <a data-rescreen="1">Re-screen with the new rules</a>' });
      loadRules();
    }
    drawSLog();
  }
  function drawSLog() {
    const el = $('slog');
    el.innerHTML = sLog.map(m => m.role === 'sys' ? '<div class="cm sys">' + m.html + '</div>'
      : m.role === 'err' ? '<div class="cm err">' + esc(m.text) + '</div>'
      : '<div class="cm ' + m.role + '">' + esc(m.text) + '</div>').join('');
    el.querySelectorAll('[data-rescreen]').forEach(a => a.addEventListener('click', () => { sResults = {}; screenAll(); }));
    el.scrollTop = el.scrollHeight;
  }
  $('ssend').addEventListener('click', sendRule);
  $('sin').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); sendRule(); } });

  // ══════════════════════════════════════════════════════════════════════
  //  FIND JOB: search open jobs as you type, or let AI rank them for the
  //  candidate loaded in the Candidate tab (embeddings, same as Job Matching)
  // ══════════════════════════════════════════════════════════════════════
  let jobsAi = false, jobsFor = '', jBusy = false;
  function jmsg(t, cls) { const m = $('jmsg'); m.className = 'msg ' + (cls || ''); m.innerHTML = t; }

  async function loadJobs(text) {
    jBusy = true; $('jmatch').disabled = true;
    jmsg('<span class="spin"></span>' + (text ? 'Ranking your open jobs for this candidate…' : 'Loading open jobs…'));
    const r = await call('POST', '/assistant/jobs', text ? { text } : {});
    jBusy = false; $('jmatch').disabled = false;
    if (!r.ok) { jmsg(esc(r.error), 'err'); return false; }
    jobs = r.jobs || []; jobsAi = !!r.ai;
    const n = jobs.length;
    if (!n) jmsg('No open jobs found.', '');
    else if (text && !r.ai) jmsg('⚠ ' + esc(r.ai_note || 'AI ranking is not available right now.') + ' Showing all ' + n + ' open jobs instead.', 'err');
    else if (text) jmsg('Ranked ' + n + ' open jobs for <b>' + esc(jobsFor || 'this candidate') + '</b>' +
      (r.no_vector ? ' · ' + r.no_vector + ' without a JD are at the bottom' : '') + '.', 'ok');
    else jmsg(n + ' open job' + (n > 1 ? 's' : '') + '. Type to filter.', '');
    renderJobs();
    return true;
  }

  function openJobsTab() {
    if (!jobs && !jBusy) loadJobs('');
    const it = current;
    $('jmatch').textContent = it && (it.parsed || it.file || it.page)
      ? 'Best jobs for ' + ((it.fields && it.fields.name) || (it.file ? it.file.name : 'this candidate'))
      : 'Best jobs for this candidate';
    setTimeout(() => $('jq').focus(), 0);
  }

  $('jmatch').addEventListener('click', async () => {
    if (jBusy) return;
    let it = current;
    const page = $('page').value.trim();
    if (!it && page) { it = { id: ++seq, file: null, page, state: 'new' }; items = [it]; current = it; renderQueue(); }
    else if (it && page && !it.file && it.page !== page) it.page = page;
    if (!it) { jmsg('First add the CV or paste the Naukri profile in the <b>Candidate</b> tab — then come back here.', 'err'); return; }
    if (!it.parsed || it.parsedPage !== it.page) {
      jBusy = true; $('jmatch').disabled = true; jmsg('<span class="spin"></span>Reading the CV…');
      const ok = await parseItem(it);
      jBusy = false; $('jmatch').disabled = false;
      if (!ok) { jmsg(esc(it.err), 'err'); return; }
      renderQueue();
    }
    const p = it.parsed;
    jobsFor = (it.fields && it.fields.name) || '';
    $('jmatch').textContent = 'Best jobs for ' + (jobsFor || 'this candidate');
    await loadJobs([p.resume_text || '', p.candidate_text || ''].join('\n\n').trim());
  });
  $('jreset').addEventListener('click', () => { if (!jBusy) { jobsFor = ''; $('jq').value = ''; loadJobs(''); } });
  $('jq').addEventListener('input', () => renderJobs());

  const lpa = v => (Math.round(v * 10) / 10).toString();
  function jobHay(j) {
    return [j.role, j.client, j.location, j.must.join(' '), j.good.join(' '), j.jd,
            j.exp_min || j.exp_max ? (j.exp_min || 0) + '-' + (j.exp_max || '') + ' yrs' : ''].join(' ').toLowerCase();
  }
  // Highlight search terms on the RAW text, escaping each piece (never regex over escaped HTML).
  function hl(s, terms) {
    s = String(s == null ? '' : s);
    const low = s.toLowerCase(), mark = new Array(s.length).fill(false);
    terms.forEach(t => { if (t.length < 2) return; let i = low.indexOf(t); while (i >= 0) { for (let k = i; k < i + t.length; k++) mark[k] = true; i = low.indexOf(t, i + t.length); } });
    let out = '', i = 0;
    while (i < s.length) { let j = i; while (j < s.length && mark[j] === mark[i]) j++; out += mark[i] ? '<mark>' + esc(s.slice(i, j)) + '</mark>' : esc(s.slice(i, j)); i = j; }
    return out;
  }
  function renderJobs() {
    const l = $('jlist');
    if (!jobs) { l.innerHTML = ''; return; }
    const terms = $('jq').value.toLowerCase().split(/[\s,]+/).map(s => s.trim()).filter(Boolean);
    const rows = jobs.filter(j => { const h = jobHay(j); return terms.every(t => h.indexOf(t) >= 0); });
    if (!rows.length) { l.innerHTML = '<div class="empty">No open job matches “' + esc($('jq').value) + '”.</div>'; return; }
    l.innerHTML = rows.map(j => {
      const sc = j.score;
      const pill = jobsAi ? (sc == null ? '<span class="pill unknown" title="This job has no JD vector yet">no JD</span>'
        : '<span class="pill ' + (sc >= 75 ? 'open' : sc >= 60 ? 'maybe' : 'skip') + '" title="AI match: candidate vs this JD">' + Math.round(sc) + '%</span>') : '';
      const facts = [j.client, j.location,
        (j.exp_min || j.exp_max) ? (j.exp_min ? lpa(j.exp_min) : '0') + '–' + (j.exp_max ? lpa(j.exp_max) : '') + ' yrs' : '',
        (j.ctc_min || j.ctc_max) ? '₹' + (j.ctc_min ? lpa(j.ctc_min) : '') + (j.ctc_max ? '–' + lpa(j.ctc_max) : '') + ' L' : '',
        j.pipeline + ' in pipeline'].filter(Boolean);
      const cur = String(j.id) === String(mandateId);
      return '<div class="jrow' + (cur ? ' cur' : '') + '"><div class="t"><b>' + hl(j.role || 'Untitled job', terms) + '</b>' +
        (j.is_recent ? '<span class="pill unknown">recent</span>' : '') + pill + '</div>' +
        '<div class="r">' + facts.map(x => hl(x, terms)).join(' · ') + '</div>' +
        (j.must.length ? '<div class="chips">' + j.must.slice(0, 8).map(s => '<span class="chip">' + hl(s, terms) + '</span>').join('') + '</div>' : '') +
        '<div class="row">' + (cur ? '<span class="msg ok" style="margin:0">✓ Selected job</span>'
          : '<button class="btn sm pri" data-use="' + j.id + '">Use this job</button>') +
        '<a class="btn sm ghost" target="_blank" rel="noopener" href="' + ATS + '/#mandate/' + j.id + '/pipeline">Open in HireLab</a></div></div>';
    }).join('');
  }
  $('jlist').addEventListener('click', async e => {
    const b = e.target.closest('[data-use]');
    if (!b) return;
    const id = b.dataset.use;
    if (!mandates.some(m => String(m.id) === id)) { jmsg('This job is not in your dropdown — reload the panel.', 'err'); return; }
    setMandate(id);
    showTab('cand');
    // If a candidate is already loaded, score them against the chosen job straight away.
    if (current && (current.parsed || current.file || current.page) && !$('go').disabled) $('go').click();
  });

  // ── task alerts on/off (the background worker does the alerting) ──
  const OFF_KEY = 'hla_alerts_off';
  chrome.storage.local.get([OFF_KEY], v => { $('alerts').checked = !(v && v[OFF_KEY]); });
  $('alerts').addEventListener('change', e => {
    chrome.storage.local.set({ [OFF_KEY]: !e.target.checked }, () => {
      if (e.target.checked) chrome.runtime.sendMessage({ type: 'hla-check-alerts' }, () => void chrome.runtime.lastError);
    });
  });

  // ══════════════════════════════════════════════════════════════════════
  //  LINKEDIN: 1-click capture of the open profile → Candidate DB or BD
  // ══════════════════════════════════════════════════════════════════════
  const LI_RX = /^https:\/\/([a-z]{2,3}\.)?(www\.)?linkedin\.com\/in\/[^/?#]+/i;
  const LTKEY = 'hla_li_target';
  let liTabId = null, liUrl = '', li = null, liTarget = 'candidate', liBusy = false;
  chrome.storage.local.get([LTKEY], v => { if (v && (v[LTKEY] === 'bd' || v[LTKEY] === 'candidate')) liTarget = v[LTKEY]; });
  function limsg(t, cls) { const m = $('limsg'); m.className = 'msg ' + (cls || ''); m.innerHTML = t; }
  const liPublic = u => { const m = String(u || '').match(/linkedin\.com\/in\/([^/?#]+)/i); return m ? 'https://www.linkedin.com/in/' + m[1].toLowerCase() + '/' : ''; };

  async function liCheckTab() {
    let tab = null;
    try { [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true }); } catch (e) {}
    const url = (tab && tab.url) || '';
    const ok = LI_RX.test(url);
    liTabId = ok ? tab.id : null; liUrl = ok ? url : '';
    const box = $('litab');
    box.className = 'litab' + (ok ? ' ok' : '');
    box.textContent = ok ? '✓ ' + (tab.title || liPublic(url)) : 'No LinkedIn profile is open in this window. Open one (linkedin.com/in/…) and this button switches on.';
    $('ligo').disabled = !ok || liBusy;
    $('lidot').hidden = !ok;
  }
  chrome.tabs.onActivated.addListener(() => liCheckTab());
  chrome.tabs.onUpdated.addListener((id, info) => { if (info.url || info.status === 'complete') liCheckTab(); });
  liCheckTab();

  // Runs inside the LinkedIn tab, once, read-only.
  function liReadPage() {
    const main = document.querySelector('main') || document.body;
    return { url: location.href, title: document.title, text: (main.innerText || '').slice(0, 30000) };
  }

  $('ligo').addEventListener('click', async () => {
    if (liBusy) return;
    await liCheckTab();
    if (!liTabId) { limsg('Open a LinkedIn profile first.', 'err'); return; }
    liBusy = true; $('ligo').disabled = true; li = null; $('liresult').innerHTML = '';
    limsg('<span class="spin"></span>Reading the profile…');
    let page = null;
    try {
      const res = await chrome.scripting.executeScript({ target: { tabId: liTabId }, func: liReadPage });
      page = res && res[0] && res[0].result;
    } catch (e) {
      liBusy = false; liCheckTab();
      limsg('Chrome did not allow reading this tab (' + esc(e.message || e) + '). Reload the LinkedIn page and try again.', 'err'); return;
    }
    if (!page || !page.text || page.text.length < 80) { liBusy = false; liCheckTab(); limsg('The profile has not loaded yet — scroll it once and try again.', 'err'); return; }
    limsg('<span class="spin"></span>Picking out name, company, experience and skills…');
    const r = await call('POST', '/assistant/linkedin/parse', { url: page.url, title: page.title, text: page.text });
    liBusy = false; liCheckTab();
    if (!r.ok) { limsg(esc(r.error), 'err'); return; }
    li = { f: r.fields, url: r.public_url, ex: r.existing || {}, email: '', phone: '', company: r.fields.company || '', saved: null };
    limsg('');
    liRender();
  });

  const LIF = [['name', 'Name', 'w'], ['headline', 'Headline', 'w'], ['designation', 'Current title'], ['company', 'Current company'],
               ['location', 'Location'], ['experience', 'Experience (yrs)'], ['email', 'E-mail (optional)'], ['phone', 'Phone (optional)']];
  function liRender() {
    const box = $('liresult');
    if (!li) { box.innerHTML = ''; return; }
    const f = li.f, ex = li.ex || {};
    let h = '<div class="card who"><h2>' + esc(f.name || 'Unnamed') + '</h2><p>' + esc(f.headline || [f.designation, f.company].filter(Boolean).join(' at ')) + '</p>' +
      '<div class="liurl"><a href="' + esc(li.url) + '" target="_blank" rel="noopener" title="Public profile">' + esc(li.url) + '</a>' +
      '<button class="btn sm" id="licopy">Copy link</button></div>' +
      '<div class="facts">' + [f.experience ? f.experience + ' yrs' : '', f.location, (f.work_history || []).length ? f.work_history.length + (f.work_history.length > 1 ? ' roles' : ' role') : '',
        (f.education || [])[0] ? (f.education[0].degree || f.education[0].school) : ''].filter(Boolean).map(x => '<span>' + esc(x) + '</span>').join('') + '</div>' +
      ((f.key_skills || []).length ? '<div class="chips" style="margin-top:8px">' + f.key_skills.slice(0, 12).map(x => '<span class="chip">' + esc(x) + '</span>').join('') + '</div>' : '') +
      '<details><summary>Check / edit details</summary><div class="form">' + LIF.map(([k, l, w]) => {
        const v = k === 'email' || k === 'phone' ? li[k] : (f[k] == null ? '' : f[k]);
        return '<label class="' + (w || '') + '">' + l + '<input data-lf="' + k + '" value="' + esc(v) + '"></label>';
      }).join('') + '</div>' +
      ((f.work_history || []).length ? '<div class="lihist">' + f.work_history.map(w => '<div><b>' + esc(w.designation) + '</b> · ' + esc(w.company) +
        ' <span style="color:var(--muted)">' + esc([w.start_date, w.is_current ? 'Present' : w.end_date].filter(Boolean).join(' – ')) + '</span></div>').join('') + '</div>' : '') +
      '</details></div>';
    if (ex.candidate) h += '<div class="dup here">⧉ Already in candidates: <a target="_blank" rel="noopener" href="' + ATS + '/#mandate/' + ex.candidate.mandate_id + '/profiles/' + ex.candidate.id + '">' +
      esc(ex.candidate.where) + (ex.candidate.stage ? ' · ' + esc(ex.candidate.stage) : '') + '</a>. Saving there again updates that profile.</div>';
    if (ex.bd) h += '<div class="dup else">⧉ Already in BD Workspace: <b>' + esc(ex.bd.name) + '</b> at ' + esc(ex.bd.company) + '. Saving to BD updates that person.</div>';
    const cand = liTarget === 'candidate';
    h += '<div class="card"><div class="lbl">Save this profile to</div><div class="seg2">' +
      '<label class="' + (cand ? 'on' : '') + '"><input type="radio" name="lit" value="candidate"' + (cand ? ' checked' : '') + '><span><b>Candidate database</b><span>As a candidate, with work history and skills</span></span></label>' +
      '<label class="' + (!cand ? 'on' : '') + '"><input type="radio" name="lit" value="bd"' + (!cand ? ' checked' : '') + '><span><b>BD Workspace</b><span>As a client contact (person)</span></span></label></div>';
    if (cand) {
      h += '<div class="msg">Goes to: <b>' + esc(liCandDest().label) + '</b>' + (mandateId && !isCentral() ? ' · <a href="#" id="litocdb">use Central Database instead</a>' : ' · pick a job in the dropdown at the top to add it to a job') + '</div>';
    } else {
      h += '<label class="form" style="display:block;margin-top:8px"><span style="font-size:11px;color:var(--muted)">Company in BD (created as a prospect if new)</span><input id="licomp" value="' + esc(li.company) + '"></label>';
    }
    h += '</div>';
    h += '<div class="card push">' + (li.saved ? '<div class="msg ok">✓ ' + esc(li.saved.msg) + '</div><a class="btn sm" target="_blank" rel="noopener" href="' + esc(li.saved.link) + '">Open in HireLab</a>'
      : '<div class="msg" id="lipm">' + (cand ? 'No CV needed — you can attach one later in HireLab.' : 'Name, title, company and the LinkedIn link go to BD.') + '</div><button class="btn navy" id="lisave">Save to ' + (cand ? 'candidates' : 'BD') + '</button>') + '</div>';
    box.innerHTML = h;
    box.querySelectorAll('[data-lf]').forEach(inp => inp.addEventListener('change', liReadForm));
    box.querySelectorAll('input[name="lit"]').forEach(r => r.addEventListener('change', () => { liReadForm(); liTarget = r.value; chrome.storage.local.set({ [LTKEY]: liTarget }); li.saved = null; liRender(); }));
    const cp = $('licopy'); if (cp) cp.addEventListener('click', () => { navigator.clipboard.writeText(li.url).then(() => { cp.textContent = 'Copied ✓'; setTimeout(() => { cp.textContent = 'Copy link'; }, 1500); }); });
    const tc = $('litocdb'); if (tc) tc.addEventListener('click', e => { e.preventDefault(); setMandate('central'); liRender(); });
    const sv = $('lisave'); if (sv) sv.addEventListener('click', liSave);
  }
  function liCandDest() {
    if (!mandateId || isCentral()) return { mid: 'central', label: 'Central Database' };
    return { mid: +mandateId, label: mandateName() };
  }
  function liReadForm() {
    if (!li) return;
    document.querySelectorAll('#liresult [data-lf]').forEach(inp => {
      const k = inp.dataset.lf, v = inp.value.trim();
      if (k === 'email' || k === 'phone') li[k] = v; else li.f[k] = k === 'experience' ? (parseFloat(v) || 0) : v;
    });
    const c = $('licomp'); if (c) li.company = c.value.trim();
  }
  async function liSave() {
    liReadForm();
    const pm = $('lipm'), btn = $('lisave');
    if (!li.f.name) { pm.className = 'msg err'; pm.textContent = 'Name is missing — fill it in "Check / edit details".'; return; }
    const bd = liTarget === 'bd';
    if (bd && !li.company) { pm.className = 'msg err'; pm.textContent = 'Type the company name.'; return; }
    btn.disabled = true; pm.className = 'msg'; pm.innerHTML = '<span class="spin"></span>Saving…';
    const dest = liCandDest();
    const r = await call('POST', '/assistant/linkedin/save', { target: liTarget, url: li.url, fields: li.f, email: li.email, phone: li.phone,
      mandate_id: bd ? undefined : dest.mid, company: bd ? li.company : undefined });
    if (!r.ok) { btn.disabled = false; pm.className = 'msg err'; pm.textContent = r.error || 'Save failed.'; return; }
    if (bd) {
      li.saved = { msg: (r.action === 'updated' ? 'Updated ' : 'Added ') + li.f.name + ' in BD at ' + r.company + (r.company_created ? ' (new company — add its details later)' : ''),
                   link: ATS + '/#bd/people/' + r.person_id };
      li.ex.bd = { id: r.person_id, name: li.f.name, company: r.company };
    } else {
      li.saved = { msg: (r.action === 'updated' ? 'Updated the existing profile in ' : 'Added to ') + (r.central ? 'Central Database' : dest.label) + ' with the LinkedIn link.',
                   link: ATS + '/#mandate/' + r.mandate_id + '/profiles/' + r.candidate_id };
    }
    liRender();
  }

  // ══════════════════════════════════════════════════════════════════════
  //  NAUKRI: 1-click read of the open profile + press Naukri's Download
  //  button + attach the downloaded CV (v2.5)
  // ══════════════════════════════════════════════════════════════════════
  // Works on both profile formats:
  //   hiring.naukri.com/hiring/…/apply/…   (job Responses)
  //   resdex.naukri.com/v3/preview…         (Resdex profile)
  // Only when the recruiter presses the button: one read-only script runs in
  // Chrome's isolated world, returns the profile text, optionally clicks the
  // Download button, and is gone. No content script, nothing stays on the page.
  const NK_RX = /^https:\/\/([a-z0-9-]+\.)*naukri\.com\//i;
  const NK_PROFILE_RX = /^https:\/\/(hiring\.naukri\.com\/hiring\/[^?#]*\/apply\/|resdex\.naukri\.com\/v\d+\/preview)/i;
  const NKDL_KEY = 'hla_nk_download';
  let nkTabId = null, nkBusy = false, nkWait = null;   // nkWait: {since, item, timer}
  chrome.storage.local.get([NKDL_KEY], v => { if (v && v[NKDL_KEY] === false) $('nkdl').checked = false; });
  $('nkdl').addEventListener('change', e => chrome.storage.local.set({ [NKDL_KEY]: e.target.checked }));
  function nkmsg(t, cls) { const m = $('nkmsg'); m.className = 'msg ' + (cls || ''); m.innerHTML = t; }

  async function nkCheckTab() {
    let tab = null;
    try { [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true }); } catch (e) {}
    const url = (tab && tab.url) || '';
    const onNk = NK_RX.test(url), prof = NK_PROFILE_RX.test(url);
    nkTabId = onNk ? tab.id : null;
    const box = $('nktab');
    box.className = 'litab' + (onNk ? ' ok' : '');
    box.textContent = prof ? '✓ ' + (tab.title || 'Naukri profile')
      : onNk ? '⚠ This Naukri page does not look like a candidate profile — open one candidate first.'
      : 'No Naukri page is open in this window. Open a candidate (Responses or Resdex) and this button switches on.';
    $('nkgo').disabled = !onNk || nkBusy;
  }
  chrome.tabs.onActivated.addListener(() => nkCheckTab());
  chrome.tabs.onUpdated.addListener((id, info) => { if (info.url || info.status === 'complete') nkCheckTab(); });
  nkCheckTab();

  // Runs inside the Naukri tab, once. Reads the candidate column only (not
  // the "AI matched similar profiles" panel, which holds OTHER people), and
  // optionally clicks the CV Download button.
  function naukriRead(wantDownload) {
    const SIMILAR = /AI matched similar|View all similar|Recruiters also viewed/i;
    const ANCHOR = /^(candidate details|work experience|key skills|education|summary|profile detail|work summary|it skills)$/i;
    const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
    // 1) The candidate column: the biggest block around a section heading
    //    that does not also contain the similar-profiles panel.
    const roots = new Set();
    const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n, guard = 0;
    while ((n = tw.nextNode()) && guard++ < 50000) {
      if (!ANCHOR.test((n.nodeValue || '').trim())) continue;
      let el = n.parentElement;
      if (!el || !visible(el)) continue;
      while (el.parentElement && el.parentElement !== document.body && !SIMILAR.test(el.parentElement.innerText || '')) el = el.parentElement;
      roots.add(el);
    }
    let text = '', main = null;
    roots.forEach(r => { const t = r.innerText || ''; if (t.length > text.length) { text = t; main = r; } });
    if (text.length < 200) {                      // layout not recognised: whole page minus the similar panel
      text = document.body.innerText || '';
      const cut = text.search(SIMILAR);
      if (cut > 300) text = text.slice(0, cut);
    }
    // The CV preview at the bottom is masked (****) and the CV file comes separately.
    const att = text.search(/\n\s*Attached (Resume|CV)\b/i);
    if (att > 300) text = text.slice(0, att);
    // The name / phone card sometimes sits outside the column: add the page title.
    text = (document.title || '') + '\n' + text;

    // 2) The Download button.
    let dl = { wanted: !!wantDownload, clicked: false, label: '' };
    if (wantDownload) {
      const all = Array.from(document.querySelectorAll('a,button,[role="button"],[title],[aria-label]'));
      const lab = el => ((el.innerText || '').trim() + ' ' + (el.getAttribute('title') || '') + ' ' + (el.getAttribute('aria-label') || '')).trim();
      const score = el => {
        if (!visible(el)) return 0;
        const t = (el.innerText || '').trim(), l = lab(el);
        if (/excel|app|report|all|selected/i.test(l)) return 0;
        if (/^download( cv| resume)?$/i.test(t)) return 3;
        if (/download/i.test((el.getAttribute('title') || '') + ' ' + (el.getAttribute('aria-label') || ''))) return 2;
        const cls = (typeof el.className === 'string' ? el.className : '') + ' ' + (el.id || '');
        if (/download/i.test(cls) && t.length < 30) return 1;
        return 0;
      };
      let best = null, bs = 0;
      for (const el of all) {
        const s = score(el);
        if (s > bs) { bs = s; best = el; }
      }
      if (best) {
        const target = best.closest('a,button,[role="button"]') || best;
        dl.label = (lab(best) || 'Download').slice(0, 40);
        try { target.click(); dl.clicked = true; } catch (e) { dl.error = String(e && e.message || e); }
      }
    }
    return { url: location.href, title: document.title, text: text.slice(0, 20000), dl };
  }

  // A path on disk -> file:// URL (Windows and Mac/Linux).
  function fileUrl(path) {
    const parts = String(path).replace(/\\/g, '/').split('/');
    const enc = parts.map((s, i) => (i === 0 && /^[A-Za-z]:$/.test(s)) ? s : encodeURIComponent(s)).join('/');
    return 'file://' + (enc.startsWith('/') ? '' : '/') + enc;
  }
  // Read the finished download from disk (needs "Allow access to file URLs").
  function readDownloaded(path) {
    return new Promise(res => {
      try {
        const x = new XMLHttpRequest();
        x.open('GET', fileUrl(path)); x.responseType = 'blob';
        x.onload = () => res(x.response && x.response.size ? x.response : null);
        x.onerror = () => res(null);
        x.send();
      } catch (e) { res(null); }
    });
  }
  const baseName = p => String(p || '').split(/[\\/]/).pop() || 'cv.pdf';

  // Catch the CV download that Naukri starts after the click (or that the
  // recruiter starts by hand within 2 minutes).
  function nkStopWaiting() { if (nkWait) { clearTimeout(nkWait.timer); nkWait = null; } }
  chrome.downloads.onChanged.addListener(async delta => {
    if (!nkWait || !delta.state || delta.state.current !== 'complete') return;
    const [d] = await new Promise(r => chrome.downloads.search({ id: delta.id }, r));
    if (!d || new Date(d.startTime).getTime() < nkWait.since - 2000) return;
    const name = baseName(d.filename);
    if (!OKEXT.test(name)) return;                 // not a CV (e.g. an image) — keep waiting
    const it = nkWait.item;
    nkStopWaiting();
    if (!items.includes(it)) return;               // the recruiter cleared / moved on
    const blob = await readDownloaded(d.filename);
    if (!blob) {
      const fileOk = await new Promise(r => chrome.extension.isAllowedFileSchemeAccess(r));
      nkmsg('✓ Profile read. The CV was downloaded as <b>' + esc(name) + '</b> — drag it from Chrome\'s downloads (top-right ⤓) into the box below, then press Analyse.' +
        (fileOk ? '' : '<div class="nkhint">To attach it automatically next time: open <b>chrome://extensions</b> → HireLab Assistant → <b>Details</b> → switch on <b>Allow access to file URLs</b>.</div>'), '');
      return;
    }
    it.file = new File([blob], name, { type: blob.type || '' });
    it.state = 'new'; it.parsed = null; it.pushed = null;
    renderQueue(); renderResult();
    const what = '✓ Profile read and CV <b>' + esc(name) + '</b> attached. ';
    if (!mandateId) nkmsg(what + 'Choose a job at the top, then press Analyse.', 'ok');
    else if ($('go').disabled) nkmsg(what + 'Press Analyse again when the current analysis finishes.', 'ok');
    else { nkmsg(what + 'Analysing…', 'ok'); $('go').click(); }
  });

  $('nkgo').addEventListener('click', async () => {
    if (nkBusy) return;
    await nkCheckTab();
    if (!nkTabId) { nkmsg('Open a Naukri candidate profile first.', 'err'); return; }
    const wantDl = $('nkdl').checked;
    nkBusy = true; $('nkgo').disabled = true; nkStopWaiting();
    nkmsg('<span class="spin"></span>Reading the profile…');
    const since = Date.now();
    let page = null;
    try {
      const res = await chrome.scripting.executeScript({ target: { tabId: nkTabId }, func: naukriRead, args: [wantDl] });
      page = res && res[0] && res[0].result;
    } catch (e) {
      nkBusy = false; nkCheckTab();
      nkmsg('Chrome did not allow reading this tab (' + esc(e.message || e) + '). Reload the Naukri page and try again.', 'err'); return;
    }
    nkBusy = false; nkCheckTab();
    if (!page || !page.text || page.text.length < 120) { nkmsg('The profile has not loaded yet — wait a second and try again.', 'err'); return; }
    // A new candidate: start a fresh Candidate tab with this profile.
    items = [{ id: ++seq, file: null, page: page.text, state: 'new', naukriUrl: page.url }];
    current = items[0];
    $('page').value = page.text;
    renderQueue(); renderResult(); cmsg('', '');
    const it = current;
    if (!wantDl) {
      nkmsg('✓ Profile read. ' + (mandateId ? 'Analysing…' : 'Choose a job at the top, then press Analyse.'), 'ok');
      if (mandateId) $('go').click();
      return;
    }
    nkWait = { since, item: it, timer: setTimeout(() => {
      if (!nkWait || nkWait.item !== it) return;
      nkWait = null;
      nkmsg('Profile read, but no CV download arrived. Download the CV on Naukri, drop it in the box below and press Analyse — or press Analyse now to score from the profile only.', 'err');
    }, 120000) };
    if (page.dl.clicked) nkmsg('<span class="spin"></span>✓ Profile read · pressed “' + esc(page.dl.label) + '” on Naukri — waiting for the CV to download…');
    else nkmsg('✓ Profile read, but I could not find the Download button on this page. Press Download on Naukri yourself — the CV is attached as soon as it finishes.', 'err');
  });

  loadMandates();
})();
