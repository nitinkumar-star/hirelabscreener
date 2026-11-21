/* HireLab — Insight Engine, Wave 1: Capture  (modules/insight_capture.py)
 *
 * One script, used by index.html, team-chat.html and recruitos-live.html:
 *   ICap.withReason(stage, body, opts)  -> body + reason fields, or null when cancelled
 *                                          (returns body unchanged for a positive stage or when switched off)
 *   ICap.reasonOnly(cid, stage, opts)   -> reason for the CURRENT stage, without moving it
 *   ICap.mountCapture(cid)              -> the "Call capture" card into #icap-card-<cid>
 *   ICap.openCapture(cid, opts)         -> the same card in a pop-up
 *   ICap.mountCheck630(wrap)            -> the "6:30 check" card on Home
 *   ICap.refreshBadge()                 -> red count on the Home menu item
 *   ICap.mountSettings(el)              -> Settings: switch + "Tag existing candidates"
 * Everything hides itself when the agency switched insight_capture_on off.
 */
(function () {
  'use strict';
  if (window.ICap) return;
  var ICap = window.ICap = {};
  var cfg = null, cfgP = null;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }
  function api(method, path, body) {
    var o = {method: method, credentials: 'include', headers: {}};
    if (body !== undefined) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
    return fetch(path, o).then(function (r) {
      return r.json().catch(function () { return {error: 'The server answered HTTP ' + r.status + '.'}; });
    }).catch(function (e) { return {error: 'Network error: ' + e.message}; });
  }
  function note(kind, msg) {
    try { if (typeof window.toast === 'function') { window.toast(kind === 'ok' ? '✅' : '⚠️', esc(msg)); return; } } catch (e) {}
    if (kind !== 'ok') alert(msg);
  }

  ICap.load = function (force) {
    if (cfgP && !force) return cfgP;
    cfgP = api('GET', '/api/insight/config').then(function (r) {
      cfg = (r && r.ok) ? r : {capture_on: false, catalog: null};
      if (!(r && r.ok)) cfgP = null;          // not signed in yet / error: ask again next time
      return cfg;
    });
    return cfgP;
  };
  ICap.on = function () { return !!(cfg && cfg.capture_on && cfg.catalog); };

  function famFor(stage) {
    if (!cfg || !cfg.catalog) return null;
    var s = String(stage || '').toLowerCase().replace(/\s+/g, ' ').trim();
    var fams = cfg.catalog.families;
    for (var f in fams) if (fams[f].stages.indexOf(s) >= 0) return f;
    return null;
  }
  ICap.familyFor = famFor;
  function codeLabel(fam, code) {
    var f = cfg && cfg.catalog && cfg.catalog.families[fam];
    if (f) for (var i = 0; i < f.codes.length; i++) if (f.codes[i][0] === code) return f.codes[i][1];
    return code || '';
  }
  ICap.label = function (code) {
    if (!cfg || !cfg.catalog) return code;
    for (var f in cfg.catalog.families) { var l = codeLabel(f, code); if (l !== code) return l; }
    return code;
  };

  /* ── styles ─────────────────────────────────────────────────────────── */
  function css() {
    if (document.getElementById('icap-css')) return;
    var st = document.createElement('style'); st.id = 'icap-css';
    st.textContent =
      '.icap-ov{position:fixed;inset:0;background:rgba(20,20,18,.45);z-index:100000;display:flex;align-items:center;justify-content:center;padding:16px}' +
      '.icap-box{background:#fff;color:#1F1E1B;border-radius:14px;max-width:460px;width:100%;max-height:92vh;overflow:auto;box-shadow:0 18px 50px rgba(0,0,0,.25);padding:18px 18px 16px;font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}' +
      '.icap-box h3{margin:0 0 2px;font-size:16px;font-weight:700}' +
      '.icap-sub{font-size:12.5px;color:#6B675E;margin-bottom:12px}' +
      '.icap-chips{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:12px}' +
      '.icap-chip{border:1px solid #D9D5CB;background:#FAF9F6;color:#1F1E1B;border-radius:999px;padding:7px 12px;font-size:13px;cursor:pointer;line-height:1.1}' +
      '.icap-chip:hover{border-color:#0F6E56}' +
      '.icap-chip[aria-pressed="true"]{background:#0F6E56;border-color:#0F6E56;color:#fff;font-weight:600}' +
      '.icap-in{width:100%;box-sizing:border-box;border:1px solid #D9D5CB;border-radius:8px;padding:8px 10px;font:inherit;font-size:13px;background:#fff;color:#1F1E1B}' +
      '.icap-in:focus,.icap-chip:focus-visible,.icap-btn:focus-visible{outline:2px solid #0F6E56;outline-offset:1px}' +
      '.icap-lbl{display:block;font-size:11.5px;font-weight:600;color:#6B675E;margin:0 0 4px}' +
      '.icap-row{margin-bottom:10px}' +
      '.icap-act{display:flex;gap:8px;justify-content:flex-end;margin-top:14px;flex-wrap:wrap}' +
      '.icap-btn{border:1px solid #D9D5CB;background:#fff;color:#1F1E1B;border-radius:8px;padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer}' +
      '.icap-btn.pri{background:#0F6E56;border-color:#0F6E56;color:#fff}' +
      '.icap-btn[disabled]{opacity:.45;cursor:not-allowed}' +
      '.icap-err{color:#B42318;font-size:12px;margin-top:6px;min-height:0}' +
      '.icap-card{border:1px solid #E7E3D8;border-radius:12px;padding:12px 14px;margin-bottom:12px;background:#fff}' +
      '.icap-card .icap-h{display:flex;align-items:center;gap:8px;margin-bottom:8px}' +
      '.icap-card .icap-t{font-size:12px;font-weight:700;letter-spacing:.02em;text-transform:uppercase;color:#0F6E56}' +
      '.icap-g{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px 10px}' +
      '.icap-warn{color:#B42318;font-size:11.5px;font-weight:600;margin-top:3px}' +
      '.icap-mn{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:4px}' +
      '.icap-mn .icap-wide{grid-column:1/-1}' +
      '.icap-notes{margin-top:8px;font-size:12px;color:#4A4740}' +
      '.icap-notes div{padding:5px 0;border-top:1px solid #F0ECE2}' +
      '.icap-tag{display:inline-block;font-size:10.5px;font-weight:700;border-radius:6px;padding:1px 6px;background:#EEF6F2;color:#0F6E56;margin-right:5px}' +
      '.icap-tag.red{background:#FDECEC;color:#B42318}.icap-tag.amb{background:#FFF4E0;color:#8A5A00}' +
      '.icap-630 .icap-r{display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid #F0ECE2}' +
      '.icap-630 .icap-r:first-of-type{border-top:0}' +
      '.icap-630 .icap-nm{flex:1;min-width:0}' +
      '.icap-630 .icap-nm b{display:block;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}' +
      '.icap-630 .icap-nm span{font-size:12px;color:#6B675E;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
      '.icap-bar{height:8px;background:#EFEBE1;border-radius:99px;overflow:hidden;margin:8px 0}' +
      '.icap-bar i{display:block;height:100%;background:#0F6E56;width:0}' +
      '@media (max-width:520px){.icap-mn{grid-template-columns:1fr}.icap-box{padding:16px 14px}}';
    document.head.appendChild(st);
  }

  function overlay(html) {
    css();
    var ov = document.createElement('div');
    ov.className = 'icap-ov';
    ov.innerHTML = '<div class="icap-box" role="dialog" aria-modal="true">' + html + '</div>';
    document.body.appendChild(ov);
    return ov;
  }

  /* ── 1a  the reason picker ─────────────────────────────────────────── */
  function ask(fam, stage, opts) {
    opts = opts || {};
    var F = cfg.catalog.families[fam];
    return new Promise(function (resolve) {
      var chips = F.codes.map(function (c) {
        return '<button type="button" class="icap-chip" aria-pressed="false" data-code="' + esc(c[0]) + '">' + esc(c[1]) + '</button>';
      }).join('');
      var ov = overlay(
        '<h3>' + esc(F.title) + '</h3>' +
        '<div class="icap-sub">' + (opts.name ? esc(opts.name) + ' &rarr; ' : 'Moving to ') + '<b>' + esc(stage) + '</b>' +
        (opts.reasonOnly ? ' (already in this stage)' : '') + '</div>' +
        '<div class="icap-chips" role="group" aria-label="Reason">' + chips + '</div>' +
        '<div class="icap-row"><label class="icap-lbl" for="icap-det">One line of detail (optional)</label>' +
        '<input class="icap-in" id="icap-det" maxlength="300" placeholder="e.g. wants 22 LPA, offer is 18"></div>' +
        (F.needs_feedback ? '<div class="icap-row"><label class="icap-lbl" for="icap-fb">Client feedback (required)</label>' +
          '<textarea class="icap-in" id="icap-fb" rows="3" maxlength="2000" placeholder="What the client said, in their words"></textarea></div>' : '') +
        '<div class="icap-err" id="icap-err" role="alert"></div>' +
        '<div class="icap-act"><button type="button" class="icap-btn" data-x="cancel">Cancel</button>' +
        '<button type="button" class="icap-btn pri" data-x="ok" disabled>' + (opts.reasonOnly ? 'Save reason' : 'Save &amp; move') + '</button></div>');
      var code = '', okB = ov.querySelector('[data-x="ok"]'), fb = ov.querySelector('#icap-fb'), det = ov.querySelector('#icap-det');
      function valid() { return !!code && (!fb || fb.value.trim().length > 0); }
      function sync() { okB.disabled = !valid(); }
      function done(v) { document.removeEventListener('keydown', onKey, true); ov.remove(); resolve(v); }
      function submit() {
        if (!valid()) { ov.querySelector('#icap-err').textContent = !code ? 'Pick a reason.' : "Write the client's feedback."; return; }
        done({reason_code: code, reason_detail: det.value.trim(), client_feedback: fb ? fb.value.trim() : ''});
      }
      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); done(null); }
        else if (e.key === 'Enter' && e.target === det) { e.preventDefault(); submit(); }
      }
      document.addEventListener('keydown', onKey, true);
      ov.addEventListener('click', function (e) {
        var ch = e.target.closest('[data-code]');
        if (ch) {
          code = ch.getAttribute('data-code');
          ov.querySelectorAll('[data-code]').forEach(function (b) { b.setAttribute('aria-pressed', String(b === ch)); });
          ov.querySelector('#icap-err').textContent = '';
          sync(); (fb && !fb.value.trim() ? fb : det).focus();
          return;
        }
        var x = e.target.getAttribute && e.target.getAttribute('data-x');
        if (x === 'cancel' || e.target === ov) done(null);
        else if (x === 'ok') submit();
      });
      if (fb) fb.addEventListener('input', sync);
      var first = ov.querySelector('[data-code]'); if (first) first.focus();
    });
  }

  ICap.withReason = function (stage, body, opts) {
    return ICap.load().then(function () {
      var fam = ICap.on() ? famFor(stage) : null;
      if (!fam) return body;
      return ask(fam, stage, opts).then(function (r) {
        if (!r) return null;
        var out = {}; for (var k in body) out[k] = body[k];
        for (var j in r) out[j] = r[j];
        setTimeout(function () { ICap.refreshBadge(); }, 1500);
        return out;
      });
    });
  };

  ICap.reasonOnly = function (cid, stage, opts) {
    return ICap.load().then(function () {
      var fam = famFor(stage);
      if (!fam) { note('err', 'This stage does not take a reason.'); return null; }
      return ask(fam, stage, Object.assign({reasonOnly: true}, opts || {})).then(function (r) {
        if (!r) return null;
        return api('POST', '/api/candidates/' + cid + '/reasons', r).then(function (j) {
          if (!j || !j.ok) { note('err', (j && j.error) || 'Could not save the reason.'); return null; }
          note('ok', 'Reason saved'); ICap.refreshBadge(); return j.reason;
        });
      });
    });
  };

  /* ── 1b  Call capture card ─────────────────────────────────────────── */
  function numVal(v) { return (v === null || v === undefined || v === '' || Number(v) === 0) ? '' : String(v); }
  function cardHTML(c) {
    var sig = (cfg.catalog.signals || []).map(function (s) { return '<option value="' + esc(s[0]) + '">' + esc(s[1]) + '</option>'; }).join('');
    var p = 'icap-' + c.id + '-';
    var zero = !(Number(c.ctc_expected) > 0);
    var rsn = c.reason ? '<div class="icap-notes"><div><span class="icap-tag amb">Reason</span>' + esc(c.reason.label) +
      ' <span style="color:#6B675E">(' + esc(c.reason.stage) + ')</span>' + (c.reason.detail ? ' &mdash; ' + esc(c.reason.detail) : '') + '</div></div>' : '';
    var notes = (c.notes || []).length ? '<div class="icap-notes">' + c.notes.map(function (n) {
      var sl = (cfg.catalog.signals || []).filter(function (s) { return s[0] === n.signal_type; })[0];
      return '<div><span class="icap-tag">' + esc(sl ? sl[1] : n.signal_type) + '</span><b>' + esc(n.company_name) + '</b>: ' + esc(n.note) +
        ' <span style="color:#9A958A">' + esc(String(n.created_at || '').slice(0, 10)) + (n.source === 'call_ai' ? ' · from call' : '') + '</span></div>';
    }).join('') + '</div>' : '';
    return '<div class="icap-h"><span class="icap-t">Call capture</span><span style="font-size:11.5px;color:#6B675E">about a minute per call</span>' +
      '<span id="' + p + 'st" style="margin-left:auto;font-size:11.5px;color:#0F6E56"></span></div>' +
      '<div class="icap-g">' +
      '<div><label class="icap-lbl" for="' + p + 'ectc">Expected CTC (LPA)</label><input class="icap-in" id="' + p + 'ectc" inputmode="decimal" value="' + esc(numVal(c.ctc_expected)) + '" placeholder="e.g. 18">' +
        (zero ? '<div class="icap-warn" id="' + p + 'warn">&#8377;0: ask on the call</div>' : '') + '</div>' +
      '<div><label class="icap-lbl" for="' + p + 'np">Notice (days)</label><input class="icap-in" id="' + p + 'np" inputmode="numeric" value="' + esc(numVal(c.notice_period)) + '" placeholder="e.g. 60"></div>' +
      '<div><label class="icap-lbl" for="' + p + 'td">Team: direct</label><input class="icap-in" id="' + p + 'td" inputmode="numeric" value="' + esc(numVal(c.team_direct)) + '"></div>' +
      '<div><label class="icap-lbl" for="' + p + 'ti">Team: indirect</label><input class="icap-in" id="' + p + 'ti" inputmode="numeric" value="' + esc(numVal(c.team_indirect)) + '"></div>' +
      '<div><label class="icap-lbl" for="' + p + 'yt">Yearly target (&#8377; Cr)</label><input class="icap-in" id="' + p + 'yt" inputmode="decimal" value="' + esc(numVal(c.yearly_target)) + '"></div>' +
      '</div>' +
      '<div style="margin-top:10px"><span class="icap-lbl">Market note</span><div class="icap-mn">' +
      '<input class="icap-in" id="' + p + 'mc" aria-label="Company the note is about" value="' + esc(c.company) + '" placeholder="Company">' +
      '<select class="icap-in" id="' + p + 'ms" aria-label="Signal type">' + sig + '</select>' +
      '<input class="icap-in icap-wide" id="' + p + 'mt" maxlength="600" aria-label="Market note" placeholder="e.g. Siemens Bangalore sales freeze till March">' +
      '</div></div>' +
      '<div class="icap-err" id="' + p + 'err" role="alert"></div>' +
      '<div class="icap-act" style="margin-top:8px"><button type="button" class="icap-btn pri" id="' + p + 'save">Save</button></div>' +
      rsn + notes;
  }
  function wireCard(el, c, opts) {
    var p = 'icap-' + c.id + '-', $ = function (id) { return el.querySelector('#' + p + id); };
    var e = $('ectc');
    if (e && $('warn')) e.addEventListener('input', function () { $('warn').style.display = Number(e.value) > 0 ? 'none' : ''; });
    $('save').addEventListener('click', function () {
      var b = $('save'); b.disabled = true; b.textContent = 'Saving…'; $('err').textContent = '';
      var body = {ctc_expected: $('ectc').value.trim(), notice_period: $('np').value.trim(),
        team_direct: $('td').value.trim(), team_indirect: $('ti').value.trim(), yearly_target: $('yt').value.trim()};
      if ($('mt').value.trim()) body.market_note = {company: $('mc').value.trim(), signal_type: $('ms').value, note: $('mt').value.trim()};
      api('PUT', '/api/candidates/' + c.id + '/capture', body).then(function (j) {
        if (!j || !j.ok) { b.disabled = false; b.textContent = 'Save'; $('err').textContent = (j && j.error) || 'Save failed.'; return; }
        syncProfile(c.id, j.capture);
        render(el, j.capture, opts);
        var st = el.querySelector('#' + p + 'st'); if (st) st.textContent = 'Saved ✓';
        ICap.refreshBadge();
        if (opts && opts.onSave) opts.onSave(j.capture);
      });
    });
  }
  function render(el, c, opts) { el.innerHTML = cardHTML(c); wireCard(el, c, opts); }
  // keep the open profile form in step, so its own "Save" does not put old values back
  function syncProfile(cid, c) {
    var set = function (id, v) { var x = document.getElementById(id); if (x) x.value = (v === null || v === undefined) ? '' : v; };
    set('pf-ectc', c.ctc_expected || ''); set('pf-notice', c.notice_period || '');
    try {
      var S0 = window._sp && window._sp[cid];
      if (S0 && S0.c) {
        S0.c.team_direct = c.team_direct; S0.c.team_indirect = c.team_indirect; S0.c.yearly_target = c.yearly_target;
        S0.c.ctc_expected = c.ctc_expected; S0.c.notice_period = c.notice_period;
        if (!S0.edit && typeof window._spPaint === 'function') window._spPaint(cid);
      }
    } catch (e) {}
  }

  ICap.mountCapture = function (cid, el) {
    el = el || document.getElementById('icap-card-' + cid);
    if (!el) return;
    ICap.load().then(function () {
      if (!ICap.on()) { el.innerHTML = ''; el.style.display = 'none'; return; }
      css();
      el.className = 'icap-card'; el.style.display = '';
      el.innerHTML = '<div style="font-size:12px;color:#6B675E">Loading call capture&hellip;</div>';
      api('GET', '/api/candidates/' + cid + '/capture').then(function (j) {
        if (!j || !j.ok) { el.style.display = 'none'; return; }
        render(el, j.capture, {});
      });
    });
  };

  ICap.openCapture = function (cid, opts) {
    return ICap.load().then(function () {
      var ov = overlay('<div style="display:flex;align-items:center;margin-bottom:6px"><h3 style="flex:1">' + esc((opts && opts.name) || 'Call capture') + '</h3>' +
        '<button type="button" class="icap-btn" data-x="close" aria-label="Close">&times;</button></div><div class="icap-card" id="icap-pop" style="border:0;padding:0;margin:0">Loading&hellip;</div>');
      function close() { document.removeEventListener('keydown', onKey, true); ov.remove(); }
      function onKey(e) { if (e.key === 'Escape') close(); }
      document.addEventListener('keydown', onKey, true);
      ov.addEventListener('click', function (e) {
        if (e.target === ov || (e.target.getAttribute && e.target.getAttribute('data-x') === 'close')) close();
      });
      api('GET', '/api/candidates/' + cid + '/capture').then(function (j) {
        var el = ov.querySelector('#icap-pop');
        if (!j || !j.ok) { el.textContent = (j && j.error) || 'Could not load.'; return; }
        render(el, j.capture, {onSave: function (c) { if (opts && opts.onSave) opts.onSave(c); if (Number(c.ctc_expected) > 0) setTimeout(close, 600); }});
        var f = el.querySelector('input'); if (f) f.focus();
      });
    });
  };

  /* ── 1e  the 6:30 check ────────────────────────────────────────────── */
  var ISSUE = {missing_reason: ['No reason', 'amb'], missing_feedback: ['No client feedback', 'red'], ctc_zero: ['Expected CTC 0', 'red']};
  ICap.refreshBadge = function () {
    return ICap.load().then(function () {
      var home = document.getElementById('sb-home'); if (!home) return null;
      var b = document.getElementById('nav-icap-badge');
      if (!ICap.on()) { if (b) b.style.display = 'none'; return null; }
      return api('GET', '/api/insight/check-630').then(function (j) {
        var n = (j && j.ok && j.on) ? (j.total || 0) : 0;
        if (!b) {
          b = document.createElement('span'); b.id = 'nav-icap-badge'; b.className = 'sb-nav-badge';
          b.style.background = '#B42318'; home.appendChild(b);
        }
        b.textContent = n; b.style.display = n ? '' : 'none';
        b.title = n ? '6:30 check: ' + n + ' record' + (n === 1 ? '' : 's') + ' need a reason, client feedback or expected CTC' : '';
        return j;
      });
    });
  };
  ICap.mountCheck630 = function (wrap) {
    if (!wrap) return;
    ICap.load().then(function () {
      var old = wrap.querySelector('.icap-630');
      if (!ICap.on()) { if (old) old.remove(); return; }
      css();
      api('GET', '/api/insight/check-630').then(function (j) {
        if (!j || !j.ok || !j.on) return;
        var sec = old || document.createElement('section');
        sec.className = 'hm-card big icap-630';
        sec.style.marginBottom = '14px';
        var cnt = j.counts || {};
        var head = '<div class="hm-ttl"><h2>6:30 check' + (j.total ? ' <span class="hm-pill red">' + j.total + '</span>' : '') + '</h2>' +
          '<span style="font-size:12px;color:var(--muted,#6B675E)">Records you touched today</span></div>';
        var body = j.total ? (
          '<div style="font-size:12px;color:var(--muted,#6B675E);margin:-4px 0 6px">' +
          [['missing_reason', 'without a reason'], ['missing_feedback', 'without client feedback'], ['ctc_zero', 'with expected CTC 0']]
            .filter(function (x) { return cnt[x[0]]; }).map(function (x) { return cnt[x[0]] + ' ' + x[1]; }).join(' &middot; ') + '</div>' +
          j.rows.slice(0, 25).map(function (r) {
            var fix = r.issues.indexOf('ctc_zero') >= 0 && r.issues.length === 1 ? 'capture' : 'reason';
            return '<div class="icap-r"><div class="icap-nm"><b data-open="' + r.id + '" data-mid="' + (r.mandate_id || '') + '" role="link" tabindex="0">' + esc(r.name) + '</b>' +
              '<span>' + esc([r.designation, r.company].filter(Boolean).join(' · ')) + ' &middot; ' + esc(r.stage) + (r.job ? ' &middot; ' + esc(r.job) : '') + '</span></div>' +
              r.issues.map(function (i) { return '<span class="icap-tag ' + ISSUE[i][1] + '">' + ISSUE[i][0] + '</span>'; }).join('') +
              '<button type="button" class="icap-btn pri" style="padding:5px 12px;font-size:12px" data-fix="' + fix + '" data-cid="' + r.id + '" data-stage="' + esc(r.stage) + '" data-name="' + esc(r.name) + '">Fix</button></div>';
          }).join('') + (j.rows.length > 25 ? '<div style="font-size:12px;color:#6B675E;padding-top:6px">Showing 25 of ' + j.rows.length + '</div>' : ''))
          : '<div class="hm-empty">All captured today &mdash; every &ldquo;no&rdquo; has a reason. &#10003;</div>';
        sec.innerHTML = head + body;
        if (!old) {
          var after = wrap.querySelector('.hm-head');
          if (after && after.nextSibling) wrap.insertBefore(sec, after.nextSibling); else wrap.insertBefore(sec, wrap.firstChild);
          sec.addEventListener('click', function (e) {
            var f = e.target.closest('[data-fix]');
            var again = function () { ICap.mountCheck630(wrap); ICap.refreshBadge(); };
            if (f) {
              var cid = +f.getAttribute('data-cid'), nm = f.getAttribute('data-name');
              if (f.getAttribute('data-fix') === 'capture') ICap.openCapture(cid, {name: nm, onSave: again});
              else ICap.reasonOnly(cid, f.getAttribute('data-stage'), {name: nm}).then(function (x) { if (x) again(); });
              return;
            }
            var o = e.target.closest('[data-open]');
            if (o && typeof window.openProfileById === 'function') window.openProfileById(+o.getAttribute('data-open'), +o.getAttribute('data-mid') || null);
          });
        }
        var b = document.getElementById('nav-icap-badge');
        if (b) { b.textContent = j.total; b.style.display = j.total ? '' : 'none'; } else ICap.refreshBadge();
      });
    });
  };

  /* ── Settings ──────────────────────────────────────────────────────── */
  var pollT = null;
  ICap.mountSettings = function (el) {
    if (!el) return;
    ICap.load(true).then(function (c) {
      css();
      if (!c.catalog) { el.innerHTML = ''; return; }
      if (!c.is_admin) {
        el.innerHTML = '<div class="icap-card"><div class="icap-t" style="font-size:12px;font-weight:700;color:#0F6E56">INSIGHT CAPTURE</div>' +
          '<div style="font-size:12.5px;color:#6B675E;margin-top:4px">' + (c.capture_on ? 'On: you are asked for a reason on every &ldquo;no&rdquo;.' : 'Off.') + ' Managed by your admin.</div></div>';
        return;
      }
      el.innerHTML = '<div class="icap-card">' +
        '<label style="display:flex;gap:10px;align-items:flex-start;cursor:pointer"><input type="checkbox" id="icap-on" style="width:18px;height:18px;margin-top:2px;accent-color:#0F6E56"' + (c.capture_on ? ' checked' : '') + '>' +
        '<span><b>Insight capture</b><span style="display:block;font-size:12.5px;color:#6B675E">Ask for a reason on every negative stage move and client feedback on every client rejection, ' +
        'show the Call capture card on the profile, the 6:30 check on Home, and tag new candidates automatically.</span></span></label>' +
        '<div style="border-top:1px solid #F0ECE2;margin-top:12px;padding-top:12px"><b>Tag existing candidates</b>' +
        '<div style="font-size:12.5px;color:#6B675E">Fills role family, function, domain and level where they are empty, newest records first. ' +
        'Never changes what a person typed. Uses DeepSeek (about one small call per person) and stops at the monthly AI limit.</div>' +
        '<div id="icap-bf"></div></div></div>';
      el.querySelector('#icap-on').addEventListener('change', function (e) {
        api('POST', '/api/insight/config', {capture_on: e.target.checked}).then(function (j) {
          if (!j || !j.ok) { note('err', (j && j.error) || 'Could not save'); e.target.checked = !e.target.checked; return; }
          note('ok', 'Insight capture ' + (j.capture_on ? 'on' : 'off'));
          ICap.load(true).then(function () { ICap.refreshBadge(); });
        });
      });
      bf(el.querySelector('#icap-bf'));
      // Wave 2 switch (only when that module is installed)
      api('GET', '/api/talent-map/intel/config').then(function (j) {
        if (!j || !j.ok) return;
        var card = el.querySelector('.icap-card'); if (!card) return;
        var d = document.createElement('div');
        d.style.cssText = 'border-top:1px solid #F0ECE2;margin-top:12px;padding-top:12px';
        d.innerHTML = '<label style="display:flex;gap:10px;align-items:flex-start;cursor:pointer"><input type="checkbox" id="icap-intel"' +
          ' style="width:18px;height:18px;margin-top:2px;accent-color:#0F6E56"' + (j.on ? ' checked' : '') + '>' +
          '<span><b>Company intelligence</b><span style="display:block;font-size:12.5px;color:#6B675E">The Intelligence tab on the Company Talent Map and the ' +
          '&ldquo;Company intel&rdquo; links on profiles and jobs.</span></span></label>';
        card.appendChild(d);
        d.querySelector('#icap-intel').addEventListener('change', function (e) {
          api('POST', '/api/talent-map/intel/config', {on: e.target.checked}).then(function (r) {
            if (!r || !r.ok) { note('err', (r && r.error) || 'Could not save'); e.target.checked = !e.target.checked; return; }
            note('ok', 'Company intelligence ' + (r.on ? 'on' : 'off'));
          });
        });
      });
      // Wave 3 switch (only when that module is installed)
      api('GET', '/api/market-intel/config').then(function (j) {
        if (!j || !j.ok) return;
        var card = el.querySelector('.icap-card'); if (!card) return;
        var d = document.createElement('div');
        d.style.cssText = 'border-top:1px solid #F0ECE2;margin-top:12px;padding-top:12px';
        d.innerHTML = '<label style="display:flex;gap:10px;align-items:flex-start;cursor:pointer"><input type="checkbox" id="icap-market"' +
          ' style="width:18px;height:18px;margin-top:2px;accent-color:#0F6E56"' + (j.on ? ' checked' : '') + '>' +
          '<span><b>Market intelligence</b><span style="display:block;font-size:12.5px;color:#6B675E">The Market Intel page (salary benchmark, ' +
          'market movement, Talent Pulse) and its menu item.</span></span></label>';
        card.appendChild(d);
        d.querySelector('#icap-market').addEventListener('change', function (e) {
          api('POST', '/api/market-intel/config', {on: e.target.checked}).then(function (r) {
            if (!r || !r.ok) { note('err', (r && r.error) || 'Could not save'); e.target.checked = !e.target.checked; return; }
            note('ok', 'Market intelligence ' + (r.on ? 'on' : 'off'));
            var a = document.getElementById('sb-marketintel'); if (a) a.style.display = r.on ? '' : 'none';
          });
        });
      });
    });
  };
  function bf(box, data) {
    if (!box || !document.body.contains(box)) { if (pollT) clearTimeout(pollT); pollT = null; return; }
    var go = function (j) {
      var b = (j && j.backfill) || {};
      var total = Math.max(b.total || 0, (b.done || 0) + (b.remaining || 0)), pct = total ? Math.round(100 * (b.done || 0) / total) : 0;
      var running = b.status === 'running';
      var word = {running: 'Running', done: 'Finished', stopped: 'Stopped', capped: 'Paused (AI limit)', idle: 'Not started'}[b.status] || b.status;
      box.innerHTML = '<div class="icap-bar" aria-hidden="true"><i style="width:' + pct + '%"></i></div>' +
        '<div style="font-size:12.5px">' + esc(word) + ' &middot; ' + (b.done || 0) + ' processed' + (b.failed ? ' (' + b.failed + ' failed)' : '') +
        ' &middot; ' + (b.remaining || 0) + ' still to tag' + (b.message ? '<div style="color:#B42318;margin-top:3px">' + esc(b.message) + '</div>' : '') + '</div>' +
        '<div class="icap-act" style="justify-content:flex-start">' +
        (running ? '<button type="button" class="icap-btn" data-bf="stop">Stop</button>'
                 : '<button type="button" class="icap-btn pri" data-bf="start"' + (b.remaining ? '' : ' disabled') + '>' + (b.status === 'idle' ? 'Start tagging' : 'Start again') + '</button>') +
        '</div>';
      box.onclick = function (e) {
        var a = e.target.getAttribute && e.target.getAttribute('data-bf'); if (!a) return;
        e.target.disabled = true;
        api('POST', '/api/insight/backfill/' + a).then(function (r) { if (!r || !r.ok) note('err', (r && r.error) || 'Failed'); bf(box, r); });
      };
      if (pollT) clearTimeout(pollT);
      pollT = running ? setTimeout(function () { bf(box); }, 5000) : null;
    };
    if (data) go(data); else api('GET', '/api/insight/backfill/status').then(go);
  }
})();
