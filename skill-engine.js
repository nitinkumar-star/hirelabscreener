/* HireLab — Skill Engine, Phase 1  (modules/skill_engine.py)
 *   SkillEngine.mountDepth(cid, el)   "Skill depth" card on the candidate profile
 *   SkillEngine.mountSpec(mid, el)    "Skill spec" card on the Job Breakdown tab
 *   SkillEngine.mountSettings(el)     Settings: switch, AI suggestions to approve, analyse existing candidates
 * Phase 1 only SHOWS the data; no score uses it yet.
 */
(function () {
  'use strict';
  if (window.SkillEngine) return;
  var SE = window.SkillEngine = {};
  var cfgP = null;

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  function api(method, path, body) {
    var o = {method: method, credentials: 'include', headers: {}};
    if (body !== undefined) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
    return fetch(path, o).then(function (r) { return r.json().catch(function () { return {error: 'HTTP ' + r.status}; }); })
      .catch(function (e) { return {error: e.message}; });
  }
  function cfg() {
    if (!cfgP) cfgP = api('GET', '/api/skills/config').then(function (r) { if (!(r && r.ok)) cfgP = null; return r || {}; });
    return cfgP;
  }
  function css() {
    if (document.getElementById('sk-css')) return;
    var s = document.createElement('style'); s.id = 'sk-css';
    s.textContent =
      '.sk-card{border:1px solid #E7E3D8;border-radius:12px;padding:12px 14px;margin:0 0 12px;background:#fff;font-size:13px}' +
      '.sk-h{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap}' +
      '.sk-t{font-size:12px;font-weight:700;letter-spacing:.02em;text-transform:uppercase;color:#0F6E56}' +
      '.sk-sub{font-size:11.5px;color:#6B675E}' +
      '.sk-btn{border:1px solid #D9D5CB;background:#fff;color:#1F1E1B;border-radius:8px;padding:5px 11px;font-size:12px;font-weight:600;cursor:pointer}' +
      '.sk-btn.pri{background:#0F6E56;border-color:#0F6E56;color:#fff}.sk-btn[disabled]{opacity:.5;cursor:wait}' +
      '.sk-row{display:grid;grid-template-columns:minmax(120px,1.4fr) 70px 90px 92px;gap:8px;align-items:start;padding:7px 0;border-top:1px solid #F0ECE2}' +
      '.sk-row:first-of-type{border-top:0}.sk-name b{display:block;font-size:13px}.sk-name span{font-size:11.5px;color:#6B675E}' +
      '.sk-q{grid-column:1/-1;font-size:12px;color:#4A4740;border-left:3px solid #E7E3D8;padding-left:8px;margin-top:-2px}' +
      '.sk-lv{display:inline-block;font-size:11px;font-weight:700;border-radius:6px;padding:1px 7px}' +
      '.sk-lv.led{background:#E6F4EE;color:#0F6E56}.sk-lv.did{background:#EEF4FF;color:#3538CD}.sk-lv.supported{background:#FFF4E0;color:#8A5A00}' +
      '.sk-lv.exposure{background:#F3F2EE;color:#6B675E}.sk-lv.none{background:#F3F2EE;color:#9A958A}' +
      '.sk-tag{display:inline-block;font-size:10.5px;font-weight:700;border-radius:6px;padding:1px 6px;margin-left:4px;background:#FFF4E0;color:#8A5A00}' +
      '.sk-imp{display:inline-block;font-size:10.5px;font-weight:700;border-radius:6px;padding:1px 7px;text-transform:uppercase}' +
      '.sk-imp.core{background:#FDECEC;color:#B42318}.sk-imp.important{background:#EEF4FF;color:#3538CD}.sk-imp.nice{background:#F3F2EE;color:#6B675E}' +
      '.sk-tbl{width:100%;border-collapse:collapse;font-size:12.5px}.sk-tbl th{text-align:left;font-size:11px;color:#6B675E;font-weight:600;padding:6px;border-bottom:1px solid #E7E3D8}' +
      '.sk-tbl td{padding:7px 6px;border-bottom:1px solid #F0ECE2;vertical-align:top}.sk-scroll{overflow-x:auto}' +
      '.sk-in{border:1px solid #D9D5CB;border-radius:7px;padding:4px 6px;font:inherit;font-size:12px;background:#fff;max-width:100%}' +
      '.sk-note{font-size:11.5px;color:#6B675E;margin-top:8px}.sk-err{color:#B42318;font-size:12px;margin-top:6px}' +
      '@media(max-width:560px){.sk-row{grid-template-columns:1fr 60px 80px}.sk-row .sk-src{display:none}}';
    document.head.appendChild(s);
  }
  var LV = {led: 'Led', did: 'Did it', supported: 'Supported', exposure: 'Exposure'};
  var INV = {led: 'Led', did: 'Did it', supported: 'Supported', any: 'Any'};

  /* ── Candidate: Skill depth ───────────────────────────────────────── */
  SE.mountDepth = function (cid, el) {
    el = el || document.getElementById('sk-depth-' + cid);
    if (!el) return;
    cfg().then(function (c) {
      if (!c.ok || !c.on) { el.style.display = 'none'; return; }
      css(); el.className = 'sk-card'; el.style.display = '';
      el.innerHTML = '<div class="sk-sub">Loading skill depth…</div>';
      api('GET', '/api/candidates/' + cid + '/skill-depth').then(function (r) { depth(el, cid, r, false); });
    });
  };
  function depth(el, cid, r, all) {
    var head = '<div class="sk-h"><span class="sk-t">Skill depth</span><span class="sk-sub">years from dated roles · how deeply involved · CV evidence</span>'
      + '<span style="margin-left:auto"></span>';
    if (!r || r.error && !r.ok) { el.innerHTML = head + '</div><div class="sk-err">' + esc((r && r.error) || 'Could not load') + '</div>'; return; }
    var d = r.data;
    var btn = '<button class="sk-btn' + (d ? '' : ' pri') + '" data-sk="run">' + (d ? (r.stale ? 'Refresh (profile changed)' : 'Re-analyse') : 'Analyse skills') + '</button>';
    if (!d) { el.innerHTML = head + btn + '</div><div class="sk-sub">Not analysed yet. New candidates are analysed automatically within a few minutes.</div>'; wire(); return; }
    var list = d.skills || [];
    var shown = all ? list : list.slice(0, 8);
    var rows = shown.map(function (s) {
      var lv = s.level ? '<span class="sk-lv ' + s.level + '">' + LV[s.level] + '</span>' : '<span class="sk-lv none">—</span>';
      var yrs = s.years != null ? '<b>' + s.years + ' yrs</b>' : '<span class="sk-sub">years unknown</span>';
      var when = s.current ? 'current role' : (s.last_used ? 'last ' + esc(s.last_used) : '');
      return '<div class="sk-row"><div class="sk-name"><b>' + esc(s.name) + (s.new ? '<span class="sk-tag" title="Not in the dictionary yet">new</span>' : '') + '</b>'
        + '<span>' + esc((s.roles || []).map(function (x) { return [x.designation, x.company].filter(Boolean).join(' @ '); }).slice(0, 2).join(' · ')) + '</span></div>'
        + '<div>' + yrs + '</div><div class="sk-sub">' + when + '</div><div>' + lv + ' <span class="sk-sub sk-src">' + esc(s.source || '') + '</span></div>'
        + (s.quote ? '<div class="sk-q">“' + esc(s.quote) + '”</div>' : '') + '</div>';
    }).join('');
    el.innerHTML = head + btn + '</div>'
      + (list.length ? rows : '<div class="sk-sub">No dictionary skills found in this profile.</div>')
      + (list.length > 8 ? '<div style="margin-top:6px"><button class="sk-btn" data-sk="all">' + (all ? 'Show fewer' : 'Show all ' + list.length) + '</button></div>' : '')
      + '<div class="sk-note">' + (d.ai ? 'Involvement read by AI from the CV, with the quote it used.' : 'No AI reading yet: years and sources only.')
      + (d.roles ? ' ' + d.dated_roles + ' of ' + d.roles + ' roles have dates.' : ' No work history: years cannot be worked out.')
      + ' Analysed ' + esc(String(d.computed_at || '').slice(0, 16).replace('T', ' ')) + '.</div>';
    wire();
    function wire() {
      var b = el.querySelector('[data-sk="run"]');
      if (b) b.onclick = function () {
        b.disabled = true; b.textContent = 'Reading the CV…';
        api('POST', '/api/candidates/' + cid + '/skill-depth').then(function (x) {
          if (!x || !x.ok) { b.disabled = false; b.textContent = 'Analyse skills'; el.insertAdjacentHTML('beforeend', '<div class="sk-err">' + esc((x && x.error) || 'Failed') + '</div>'); return; }
          depth(el, cid, x, false);
        });
      };
      var a = el.querySelector('[data-sk="all"]');
      if (a) a.onclick = function () { depth(el, cid, r, !all); };
    }
  }

  /* ── Job: Skill spec ──────────────────────────────────────────────── */
  SE.mountSpec = function (mid, host) {
    if (!host) return;
    cfg().then(function (c) {
      if (!c.ok || !c.on) return;
      css();
      var el = host.querySelector('.sk-spec');
      if (!el) { el = document.createElement('div'); el.className = 'sk-card sk-spec'; host.appendChild(el); }
      el.innerHTML = '<div class="sk-sub">Loading the skill spec (first time, the AI reads the job — about 10 seconds)…</div>';
      api('GET', '/api/mandates/' + mid + '/skill-spec?ensure=1').then(function (r) { spec(el, mid, r, false); });
    });
  };
  function spec(el, mid, r, editing) {
    var head = '<div class="sk-h"><span class="sk-t">Skill spec</span><span class="sk-sub">Skill Engine · what each skill needs: importance, years, involvement, nearby skills</span><span style="margin-left:auto"></span>';
    if (!r || !r.ok) { el.innerHTML = head + '</div><div class="sk-err">' + esc((r && r.error) || 'Could not load') + '</div>'; return; }
    var sp = r.spec;
    if (!sp || !sp.skills) {
      el.innerHTML = head + '<button class="sk-btn pri" data-sk="gen">Make skill spec</button></div><div class="sk-sub">' + esc(r.error || 'No skill spec yet. Add a JD to the job, then make one.') + '</div>';
      wireGenFor(el, mid); return;
    }
    var st = r.status === 'edited' ? 'Edited by you' : 'AI-made';
    if (!editing) {
      el.innerHTML = head + '<span class="sk-sub">' + st + (r.stale ? ' · <b style="color:#B42318">job text changed</b>' : '') + '</span>'
        + '<button class="sk-btn" data-sk="gen">Rebuild with AI</button><button class="sk-btn" data-sk="edit">Edit</button></div>'
        + '<div class="sk-scroll"><table class="sk-tbl"><thead><tr><th>Skill</th><th>Importance</th><th>Min years</th><th>Involvement</th><th>Recent</th><th>Also counts</th></tr></thead><tbody>'
        + sp.skills.map(function (s) {
            return '<tr><td><b>' + esc(s.name) + '</b>' + (s.pending ? '<span class="sk-tag" title="Suggested by AI for this job; an admin approves it for the whole agency">AI</span>' : '')
              + (s.why ? '<div class="sk-sub">' + esc(s.why) + '</div>' : '') + '</td>'
              + '<td><span class="sk-imp ' + s.importance + '">' + s.importance + '</span></td>'
              + '<td>' + (s.min_years != null ? s.min_years + '+ yrs' : '<span class="sk-sub">—</span>') + '</td>'
              + '<td>' + INV[s.involvement] + '</td>'
              + '<td>' + (s.recent_years != null ? 'last ' + s.recent_years + ' yrs' : '<span class="sk-sub">—</span>') + '</td>'
              + '<td class="sk-sub">' + (s.near || []).slice(0, 4).map(function (n) { return esc(n.name) + ' ' + Math.round(n.credit * 100) + '%'; }).join(' · ') + '</td></tr>';
          }).join('') + '</tbody></table></div>'
        + '<div class="sk-note">Phase 1: shown for checking. Scores do not use it yet. "AI" = a skill or name the AI suggested for this job.</div>';
      wireGenFor(el, mid);
      el.querySelector('[data-sk="edit"]').onclick = function () { spec(el, mid, r, true); };
      return;
    }
    var opt = function (list, cur, lab) { return Object.keys(list).map(function (k) { return '<option value="' + k + '"' + (k === cur ? ' selected' : '') + '>' + (lab ? lab[k] : list[k]) + '</option>'; }).join(''); };
    var IMP = {core: 'Core', important: 'Important', nice: 'Nice'};
    el.innerHTML = head + '<button class="sk-btn" data-sk="cancel">Cancel</button><button class="sk-btn pri" data-sk="save">Save</button></div>'
      + '<div class="sk-scroll"><table class="sk-tbl"><thead><tr><th>Skill</th><th>Importance</th><th>Min years</th><th>Involvement</th><th>Recent (yrs)</th><th></th></tr></thead><tbody id="sk-ed">'
      + sp.skills.map(function (s, i) {
          return '<tr data-i="' + i + '"><td><b>' + esc(s.name) + '</b></td>'
            + '<td><select class="sk-in" data-f="importance">' + opt(IMP, s.importance) + '</select></td>'
            + '<td><input class="sk-in" data-f="min_years" inputmode="decimal" style="width:60px" value="' + (s.min_years != null ? s.min_years : '') + '"></td>'
            + '<td><select class="sk-in" data-f="involvement">' + opt(INV, s.involvement) + '</select></td>'
            + '<td><input class="sk-in" data-f="recent_years" inputmode="numeric" style="width:60px" value="' + (s.recent_years != null ? s.recent_years : '') + '"></td>'
            + '<td><button class="sk-btn" data-del="' + i + '" aria-label="Remove">✕</button></td></tr>';
        }).join('') + '</tbody></table></div>'
      + '<div style="margin-top:8px;position:relative"><input class="sk-in" id="sk-add" placeholder="+ Add a skill from the dictionary (type to search)" style="width:100%;padding:7px 9px"><div id="sk-sug"></div></div>'
      + '<div class="sk-err" id="sk-err"></div>';
    var work = JSON.parse(JSON.stringify(sp.skills));
    function collect() {
      el.querySelectorAll('#sk-ed tr').forEach(function (tr) {
        var w = work[+tr.dataset.i]; if (!w) return;
        tr.querySelectorAll('[data-f]').forEach(function (f) { var v = f.value.trim(); w[f.dataset.f] = (f.dataset.f === 'min_years' || f.dataset.f === 'recent_years') ? (v === '' ? null : Number(v)) : v; });
      });
    }
    el.querySelectorAll('[data-del]').forEach(function (b) { b.onclick = function () { work[+b.dataset.del] = null; b.closest('tr').style.display = 'none'; }; });
    // Cancel shows what is saved (not the unsaved additions)
    el.querySelector('[data-sk="cancel"]').onclick = function () {
      api('GET', '/api/mandates/' + mid + '/skill-spec').then(function (x) { spec(el, mid, x, false); });
    };
    var add = el.querySelector('#sk-add'), sug = el.querySelector('#sk-sug'), t = null;
    add.oninput = function () {
      clearTimeout(t); var q = add.value.trim(); if (q.length < 2) { sug.innerHTML = ''; return; }
      t = setTimeout(function () {
        api('GET', '/api/skills/dictionary?mid=' + mid + '&q=' + encodeURIComponent(q)).then(function (d) {
          sug.innerHTML = (d.skills || []).slice(0, 8).map(function (s) { return '<button class="sk-btn" style="margin:4px 4px 0 0" data-add="' + esc(s.key) + '" data-name="' + esc(s.name) + '">+ ' + esc(s.name) + '</button>'; }).join('') || '<div class="sk-sub">No match in the dictionary.</div>';
          sug.querySelectorAll('[data-add]').forEach(function (b) { b.onclick = function () {
            if (work.some(function (w) { return w && w.key === b.dataset.add; })) return;
            collect();
            work.push({key: b.dataset.add, name: b.dataset.name, importance: 'important', involvement: 'any', min_years: null, recent_years: null, near: []});
            spec(el, mid, Object.assign({}, r, {spec: Object.assign({}, sp, {skills: work.filter(Boolean)})}), true);
          }; });
        });
      }, 250);
    };
    el.querySelector('[data-sk="save"]').onclick = function () {
      collect();
      var b = this; b.disabled = true;
      api('PUT', '/api/mandates/' + mid + '/skill-spec', {skills: work.filter(Boolean), notes: sp.notes || ''}).then(function (x) {
        if (!x || !x.ok) { b.disabled = false; el.querySelector('#sk-err').textContent = (x && x.error) || 'Save failed'; return; }
        spec(el, mid, x, false);
      });
    };
  }
  function wireGenFor(el, mid) {
    var g = el.querySelector('[data-sk="gen"]');
    if (g) g.onclick = function () {
      g.disabled = true; g.textContent = 'AI is reading the job…';
      api('POST', '/api/mandates/' + mid + '/skill-spec/generate').then(function (x) {
        if (!x || !x.ok) { g.disabled = false; g.textContent = 'Rebuild with AI'; el.insertAdjacentHTML('beforeend', '<div class="sk-err">' + esc((x && x.error) || 'Failed') + '</div>'); return; }
        spec(el, mid, x, false);
      });
    };
  }

  /* ── Settings ─────────────────────────────────────────────────────── */
  var pollT = null;
  SE.mountSettings = function (el) {
    if (!el) return;
    cfgP = null;
    cfg().then(function (c) {
      css();
      if (!c.ok) { el.innerHTML = ''; return; }
      if (!c.is_admin) {
        el.innerHTML = '<div class="sk-card"><span class="sk-t">Skill Engine</span><div class="sk-sub" style="margin-top:4px">' + (c.on ? 'On' : 'Off') + '. Managed by your admin.</div></div>';
        return;
      }
      el.innerHTML = '<div class="sk-card">'
        + '<label style="display:flex;gap:10px;align-items:flex-start;cursor:pointer"><input type="checkbox" id="sk-on" style="width:18px;height:18px;margin-top:2px;accent-color:#0F6E56"' + (c.on ? ' checked' : '') + '>'
        + '<span><b>Skill Engine</b><span style="display:block;font-size:12.5px;color:#6B675E">Skill dictionary (' + c.base_skills + ' base skills with brands and other names), '
        + 'a skill spec for every job, and skill depth (years, involvement, CV evidence) for every candidate. Phase 1: shown only; scores do not use it yet.</span></span></label>'
        + '<div style="border-top:1px solid #F0ECE2;margin-top:12px;padding-top:12px"><b>AI suggestions to approve</b> <span class="sk-sub">(' + c.pending + ')</span>'
        + '<div class="sk-sub">New skills, other names and nearby skills the AI suggested while reading your jobs. They already work for that job; approve to use them for every job.</div><div id="sk-sugs"></div></div>'
        + '<div style="border-top:1px solid #F0ECE2;margin-top:12px;padding-top:12px"><b>Analyse existing candidates</b>'
        + '<div class="sk-sub">Works out skill depth for everyone already in the database, newest first. About one small AI call per person; stops at the monthly AI limit.</div><div id="sk-bf"></div></div></div>';
      el.querySelector('#sk-on').onchange = function (e) {
        api('POST', '/api/skills/config', {on: e.target.checked}).then(function (x) { if (!x || !x.ok) { e.target.checked = !e.target.checked; alert((x && x.error) || 'Could not save'); } cfgP = null; });
      };
      sugs(el.querySelector('#sk-sugs'));
      bf(el.querySelector('#sk-bf'));
    });
  };
  function sugs(box) {
    api('GET', '/api/skills/suggestions').then(function (r) {
      if (!r.ok) { box.innerHTML = ''; return; }
      if (!r.suggestions.length) { box.innerHTML = '<div class="sk-sub" style="margin-top:6px">Nothing waiting.</div>'; return; }
      var K = {skill: 'New skill', alias: 'Other name', near: 'Nearby skill'};
      box.innerHTML = '<div class="sk-scroll"><table class="sk-tbl" style="margin-top:6px"><thead><tr><th>Type</th><th>Suggestion</th><th>From job</th><th></th></tr></thead><tbody>'
        + r.suggestions.map(function (s) {
            var what = s.kind === 'skill' ? '<b>' + esc(s.value) + '</b>' : s.kind === 'alias' ? '“' + esc(s.value) + '” = <b>' + esc(s.skill) + '</b>'
              : '<b>' + esc(s.value) + '</b> counts ' + Math.round((s.credit || 0) * 100) + '% for <b>' + esc(s.skill) + '</b>';
            return '<tr><td>' + K[s.kind] + '</td><td>' + what + '</td><td class="sk-sub">' + esc(s.job || '—') + '</td>'
              + '<td style="white-space:nowrap"><button class="sk-btn pri" data-dec="approve" data-id="' + s.id + '">Approve</button> <button class="sk-btn" data-dec="reject" data-id="' + s.id + '">Reject</button></td></tr>';
          }).join('') + '</tbody></table></div>';
      box.querySelectorAll('[data-dec]').forEach(function (b) { b.onclick = function () {
        b.disabled = true;
        api('POST', '/api/skills/suggestions/' + b.dataset.id, {decision: b.dataset.dec}).then(function (x) { if (x && x.ok) b.closest('tr').remove(); else b.disabled = false; });
      }; });
    });
  }
  function bf(box, data) {
    if (!box || !document.body.contains(box)) { if (pollT) clearTimeout(pollT); return; }
    var go = function (j) {
      var b = (j && j.backfill) || {};
      var total = Math.max(b.total || 0, (b.done || 0) + (b.remaining || 0)), pct = total ? Math.round(100 * (b.done || 0) / total) : 0;
      var running = b.status === 'running';
      var word = {running: 'Running', done: 'Finished', stopped: 'Stopped', capped: 'Paused (AI limit)', idle: 'Not started'}[b.status] || b.status;
      box.innerHTML = '<div style="height:8px;background:#EFEBE1;border-radius:99px;overflow:hidden;margin:8px 0"><i style="display:block;height:100%;background:#0F6E56;width:' + pct + '%"></i></div>'
        + '<div style="font-size:12.5px">' + esc(word) + ' · ' + (b.analysed || 0) + ' analysed · ' + (b.remaining || 0) + ' still to do' + (b.failed ? ' · ' + b.failed + ' failed' : '')
        + (b.message ? '<div class="sk-err">' + esc(b.message) + '</div>' : '') + '</div>'
        + '<div style="margin-top:8px">' + (running ? '<button class="sk-btn" data-bf="stop">Stop</button>' : '<button class="sk-btn pri" data-bf="start"' + (b.remaining ? '' : ' disabled') + '>' + (b.status === 'idle' ? 'Start' : 'Start again') + '</button>') + '</div>';
      box.onclick = function (e) {
        var a = e.target.getAttribute && e.target.getAttribute('data-bf'); if (!a) return;
        e.target.disabled = true; api('POST', '/api/skills/backfill/' + a).then(function (x) { bf(box, x); });
      };
      if (pollT) clearTimeout(pollT);
      pollT = running ? setTimeout(function () { bf(box); }, 5000) : null;
    };
    if (data) go(data); else api('GET', '/api/skills/backfill/status').then(go);
  }
})();
