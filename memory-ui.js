/*
 * HireLab Screener — Memory UI (Wave 3, Nov 2026)
 * ================================================
 * The team's shared business memory (also read and written by Claude through
 * the MCP connector). Server: modules/memory.py (/api/memory). Served at
 * /memory-ui.js and loaded by index.html.
 *
 * What it draws:
 *   - the Memory page (sidebar → Memory, #memory): search, filters, list,
 *     a side drawer with the full note + edit history, + New note;
 *   - the "Notes" tab on a candidate profile  → MemUI.mountCandidate(cid)
 *   - the "Notes" tab on a job (mandate)       → MemUI.mountMandate(el, mid)
 *   - the "Memory notes" card on a CRM client  → MemUI.mountClient(id, name)
 *
 * Conventions: every click goes through ONE delegated listener on
 * data-act="mem-…" (no inline handlers); every value shown is escaped with
 * esc(); colours come from the app's :root tokens. Nothing is ever deleted —
 * notes are archived. Who may see / change what is decided by the server; the
 * UI only hides buttons the server would refuse (note.can_edit, recruiter
 * scopes).
 */
(function () {
  'use strict';
  if (window.MemUI) return;
  var MemUI = window.MemUI = {};

  var API = '/api/memory';
  var SCOPES = [['general', 'General'], ['client', 'Client'], ['mandate', 'Job'],
                ['candidate', 'Candidate'], ['person', 'Client person'], ['team', 'Team']];
  var SCOPE_LABEL = {};
  SCOPES.forEach(function (s) { SCOPE_LABEL[s[0]] = s[1]; });
  var IMPORTANCE = [['high', 'High'], ['normal', 'Normal'], ['low', 'Low']];
  var SOURCE_LABEL = { claude: 'Claude', user: 'Admin', recruiter: 'Recruiter', system: 'System' };

  // ── state ─────────────────────────────────────────────────────────────────
  var P = { q: '', scope: '', importance: '', archived: false, expired: false, notes: [], built: false };
  var QK = {};        // quick-add presets by key
  var MOUNTED = {};   // key -> re-render function (refreshed after any change)
  var DRAWER_ID = 0;  // note open in the drawer
  var FORM = null;    // {id, preset, note}
  var _qTimer = null;

  // ── helpers ───────────────────────────────────────────────────────────────
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function toastMsg(kind, msg) {
    try { if (typeof window.toast === 'function') { window.toast(kind, esc(msg)); return; } } catch (e) {}
    if (kind === 'ERR') alert(msg);
  }
  // index.html declares its state as `let S` (global lexical scope, not window.S)
  function appState() {
    try { return (typeof S !== 'undefined' && S) ? S : null; } catch (e) { return null; }
  }
  function isRecruiter() {
    try { return typeof window._isRecruiterScoped === 'function' && !!window._isRecruiterScoped(); } catch (e) { return false; }
  }
  function canWriteScope(scope) {
    return !isRecruiter() || scope === 'general' || scope === 'mandate' || scope === 'candidate';
  }
  function api(method, path, body) {
    var opts = { method: method, credentials: 'include', headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    return fetch(API + path, opts).then(function (r) {
      return r.text().then(function (t) {
        var j;
        try { j = JSON.parse(t); } catch (e) { j = { error: r.status === 404 ? 'Memory is not available on this server yet.' : 'Server error ' + r.status }; }
        if (!r.ok && !j.error) j.error = 'Server error ' + r.status;
        j._status = r.status;
        return j;
      });
    }, function () { return { error: 'Server not reachable. Check your internet connection and try again.' }; });
  }
  function qs(o) {
    var a = [];
    Object.keys(o).forEach(function (k) { if (o[k] !== '' && o[k] != null && o[k] !== false) a.push(encodeURIComponent(k) + '=' + encodeURIComponent(o[k])); });
    return a.length ? '?' + a.join('&') : '';
  }
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function fmtDate(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/.exec(String(s || ''));
    if (!m) return '';
    return parseInt(m[3], 10) + ' ' + MONTHS[parseInt(m[2], 10) - 1] + ' ' + m[1];
  }
  function fmtDateTime(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(s || ''));
    return m ? fmtDate(s) + ', ' + m[4] + ':' + m[5] : fmtDate(s);
  }
  function loading(msg) { return '<div class="mem-empty">' + esc(msg || 'Loading…') + '</div>'; }
  function scopeBadge(s) { return '<span class="mem-b mem-b-' + esc(s) + '">' + esc(SCOPE_LABEL[s] || s) + '</span>'; }
  function impBadge(i) { return i === 'high' ? '<span class="mem-b mem-imp-high">High</span>' : i === 'low' ? '<span class="mem-b mem-imp-low">Low</span>' : '<span class="mem-b mem-imp-normal">Normal</span>'; }
  function srcBadge(s) { return '<span class="mem-b mem-src-' + esc(s) + '">' + esc(SOURCE_LABEL[s] || s || '') + '</span>'; }
  function stateBadges(n) {
    var h = '';
    if (n.status === 'archived') h += '<span class="mem-b mem-st-arch">Archived</span>';
    if (n.expired) h += '<span class="mem-b mem-st-exp">Expired</span>';
    return h;
  }
  function entityLabel(n) {
    var e = n.entity_name || (n.scope === 'general' ? 'Agency-wide' : '');
    return e;
  }

  // ── styles (tokens from the app's :root) ─────────────────────────────────
  function injectCss() {
    if ($('mem-css')) return;
    var css = ''
      + '#memory-view{background:var(--paper,#f5f6f8);color:var(--ink,#1B2B3A)}'
      + '.mem-head{display:flex;align-items:center;gap:12px;padding:12px 18px;border-bottom:1px solid var(--line,#e4e7ec);background:#fff;flex-shrink:0;flex-wrap:wrap}'
      + '.mem-ttl{display:flex;align-items:center;gap:10px;min-width:0;flex:1}'
      + '.mem-ttl .ic{width:30px;height:30px;border-radius:8px;background:var(--teal-soft,#E8F4EE);color:var(--teal-deep,#127A57);display:flex;align-items:center;justify-content:center;flex-shrink:0}'
      + '.mem-ttl .t{font-size:15px;font-weight:700}.mem-ttl .s{font-size:11.5px;color:var(--muted,#8C887E);margin-top:1px}'
      + '.mem-btn{border:1px solid var(--line,#e4e7ec);background:#fff;color:var(--ink,#1B2B3A);border-radius:8px;padding:7px 12px;font-size:12.5px;font-weight:600;cursor:pointer;font-family:inherit;line-height:1.2}'
      + '.mem-btn:hover{background:var(--paper,#f5f6f8)}.mem-btn:disabled{opacity:.55;cursor:default}'
      + '.mem-btn.pri{background:var(--teal,#15966A);border-color:transparent;color:#fff}.mem-btn.pri:hover{background:var(--teal-deep,#127A57)}'
      + '.mem-btn.dang{color:#B42318;border-color:#F3C9C4}.mem-btn.dang:hover{background:#FEF3F2}'
      + '.mem-btn.sm{padding:5px 9px;font-size:12px}'
      + '.mem-filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 18px;border-bottom:1px solid var(--line,#e4e7ec);background:#fff;flex-shrink:0}'
      + '.mem-in,.mem-sel,.mem-ta{border:1px solid var(--line,#e4e7ec);border-radius:8px;padding:7px 10px;font-size:13px;font-family:inherit;background:#fff;color:var(--ink,#1B2B3A);box-sizing:border-box}'
      + '.mem-in:focus,.mem-sel:focus,.mem-ta:focus{outline:2px solid var(--teal-soft,#E8F4EE);border-color:var(--teal,#15966A)}'
      + '.mem-filters .mem-in{flex:1 1 220px;min-width:160px}'
      + '.mem-chk{display:flex;align-items:center;gap:5px;font-size:12.5px;color:var(--muted,#8C887E);cursor:pointer;white-space:nowrap}'
      + '.mem-count{margin-left:auto;font-size:12px;color:var(--muted,#8C887E)}'
      + '.mem-body{flex:1;overflow:auto;padding:14px 18px}'
      + '.mem-tbl{width:100%;border-collapse:separate;border-spacing:0;background:#fff;border:1px solid var(--line,#e4e7ec);border-radius:12px;overflow:hidden;font-size:13px}'
      + '.mem-tbl th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted,#8C887E);font-weight:700;padding:9px 12px;border-bottom:1px solid var(--line,#e4e7ec);background:var(--paper,#f5f6f8)}'
      + '.mem-tbl td{padding:10px 12px;border-bottom:1px solid var(--line,#e4e7ec);vertical-align:top}'
      + '.mem-tbl tr:last-child td{border-bottom:none}'
      + '.mem-tbl tbody tr{cursor:pointer}.mem-tbl tbody tr:hover td{background:var(--paper,#f5f6f8)}'
      + '.mem-tbl tr.is-arch td{opacity:.6}'
      + '.mem-tbl td.nw{white-space:nowrap}'
      + '.mem-tbl .ent{font-weight:600}.mem-tbl .cl{font-size:11.5px;color:var(--muted,#8C887E);margin-top:2px}'
      + '.mem-tbl .ttl{font-weight:600}.mem-tbl .sn{font-size:12px;color:var(--muted,#8C887E);margin-top:2px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}'
      + '.mem-b{display:inline-block;font-size:11px;font-weight:600;padding:2px 7px;border-radius:999px;background:var(--paper,#f5f6f8);color:var(--muted,#8C887E);white-space:nowrap;margin:0 4px 2px 0}'
      + '.mem-b-client,.mem-b-person{background:#EEF4FF;color:#3538CD}.mem-b-mandate{background:#FFF6ED;color:#C4320A}'
      + '.mem-b-candidate{background:var(--teal-soft,#E8F4EE);color:var(--teal-deep,#127A57)}.mem-b-team{background:#F4F3FF;color:#5925DC}'
      + '.mem-imp-high{background:#FEF3F2;color:#B42318}.mem-imp-low{background:transparent;color:var(--muted,#8C887E)}'
      + '.mem-src-claude{background:#FDF2E9;color:#B5703F}'
      + '.mem-st-arch{background:#F2F4F7;color:#475467}.mem-st-exp{background:#FFFAEB;color:#B54708}'
      + '.mem-empty{padding:22px;text-align:center;color:var(--muted,#8C887E);font-size:13px}'
      + '.mem-err{padding:12px 14px;border-radius:10px;background:#FEF3F2;color:#B42318;font-size:13px}'
      // embedded sections (candidate / job / client)
      + '.mem-sec{background:#fff;border:1px solid var(--line,#e4e7ec);border-radius:12px;padding:14px;margin-bottom:12px}'
      + '.mem-sec h3{font-size:13px;font-weight:700;margin:0 0 2px;display:flex;align-items:center;gap:8px}'
      + '.mem-sec .sub{font-size:11.5px;color:var(--muted,#8C887E);margin:0 0 10px}'
      + '.mem-sec details{margin-top:4px}.mem-sec summary{cursor:pointer;font-size:13px;font-weight:700;padding:2px 0;list-style-position:inside}'
      + '.mem-grp{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted,#8C887E);font-weight:700;margin:12px 0 6px}'
      + '.mem-item{border:1px solid var(--line,#e4e7ec);border-left:3px solid var(--line,#e4e7ec);border-radius:9px;padding:9px 11px;margin-bottom:8px;cursor:pointer;background:#fff}'
      + '.mem-item:hover{background:var(--paper,#f5f6f8)}.mem-item.imp-high{border-left-color:#D92D20}'
      + '.mem-item .h{display:flex;gap:8px;align-items:flex-start;justify-content:space-between}'
      + '.mem-item .t{font-size:13px;font-weight:600;min-width:0}'
      + '.mem-item .c{font-size:12.5px;color:var(--ink,#1B2B3A);margin-top:3px;white-space:pre-wrap;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}'
      + '.mem-item .m{font-size:11.5px;color:var(--muted,#8C887E);margin-top:5px}'
      + '.mem-plain{font-size:12.5px;padding:7px 10px;border-radius:8px;background:var(--paper,#f5f6f8);margin-bottom:6px;white-space:pre-wrap}'
      + '.mem-plain .m{font-size:11px;color:var(--muted,#8C887E);margin-top:3px}'
      + '.mem-qa{margin-top:10px;border-top:1px dashed var(--line,#e4e7ec);padding-top:10px}'
      + '.mem-qa .mem-ta{width:100%;min-height:58px;resize:vertical}'
      + '.mem-qa .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:7px}'
      + '.mem-qa .row label{font-size:12px;color:var(--muted,#8C887E);display:flex;align-items:center;gap:5px}'
      + '.mem-qa .row .sp{flex:1}'
      // drawer
      + '.mem-drawer{position:fixed;inset:0;z-index:9700;display:none}.mem-drawer.open{display:block}'
      + '.mem-dr-bg{position:absolute;inset:0;background:rgba(15,23,42,.35)}'
      + '.mem-dr{position:absolute;top:0;right:0;bottom:0;width:min(520px,100%);background:#fff;box-shadow:-12px 0 32px rgba(15,23,42,.18);display:flex;flex-direction:column;color:var(--ink,#1B2B3A)}'
      + '.mem-dr-h{display:flex;align-items:flex-start;gap:10px;padding:14px 16px;border-bottom:1px solid var(--line,#e4e7ec)}'
      + '.mem-dr-h .t{font-size:15px;font-weight:700;flex:1;min-width:0;word-wrap:break-word}'
      + '.mem-x{border:none;background:transparent;font-size:22px;line-height:1;cursor:pointer;color:var(--muted,#8C887E);padding:0 4px}'
      + '.mem-dr-b{flex:1;overflow:auto;padding:14px 16px}'
      + '.mem-dr-f{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--line,#e4e7ec);flex-wrap:wrap}'
      + '.mem-kv{display:grid;grid-template-columns:110px 1fr;gap:6px 10px;font-size:12.5px;margin:10px 0 14px}'
      + '.mem-kv span:nth-child(odd){color:var(--muted,#8C887E)}'
      + '.mem-content{white-space:pre-wrap;font-size:13.5px;line-height:1.5;background:var(--paper,#f5f6f8);border-radius:10px;padding:12px;word-wrap:break-word}'
      + '.mem-hist{margin-top:16px}.mem-hist h4{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted,#8C887E);margin:0 0 8px}'
      + '.mem-hi{border-left:2px solid var(--line,#e4e7ec);padding:2px 0 10px 10px;font-size:12.5px}'
      + '.mem-hi .w{color:var(--muted,#8C887E);font-size:11.5px}'
      + '.mem-hi details{margin-top:4px}.mem-hi summary{cursor:pointer;color:var(--teal-deep,#127A57);font-size:12px}'
      + '.mem-hi pre{white-space:pre-wrap;font-family:inherit;font-size:12px;background:var(--paper,#f5f6f8);border-radius:8px;padding:8px;margin:4px 0}'
      // modal
      + '.mem-modal{position:fixed;inset:0;z-index:9850;background:rgba(15,23,42,.45);display:none;align-items:center;justify-content:center;padding:20px}'
      + '.mem-modal.open{display:flex}'
      + '.mem-md{background:#fff;border-radius:16px;width:100%;max-width:620px;max-height:92vh;display:flex;flex-direction:column;box-shadow:0 20px 50px rgba(15,23,42,.25);color:var(--ink,#1B2B3A)}'
      + '.mem-md-h{display:flex;align-items:center;padding:14px 18px;border-bottom:1px solid var(--line,#e4e7ec)}.mem-md-h h2{font-size:15px;margin:0;flex:1}'
      + '.mem-md-b{padding:14px 18px;overflow:auto}'
      + '.mem-md-f{display:flex;justify-content:flex-end;gap:8px;padding:12px 18px;border-top:1px solid var(--line,#e4e7ec)}'
      + '.mem-fr{display:grid;grid-template-columns:1fr 1fr;gap:10px}'
      + '.mem-fl{display:flex;flex-direction:column;gap:4px;margin-bottom:10px;font-size:12px;font-weight:600;color:var(--muted,#8C887E)}'
      + '.mem-fl .mem-in,.mem-fl .mem-sel,.mem-fl .mem-ta{width:100%;font-weight:400}'
      + '.mem-fl .hint{font-weight:400;font-size:11.5px}'
      + '.mem-fl .mem-ta{min-height:120px;resize:vertical}'
      + '.mem-lock{font-size:12.5px;background:var(--paper,#f5f6f8);border-radius:8px;padding:8px 10px;margin-bottom:10px}'
      + '.mem-ferr{color:#B42318;font-size:12.5px;margin-right:auto;align-self:center}'
      // mobile
      + '@media (max-width:760px){'
      + '.mem-head,.mem-filters,.mem-body{padding-left:12px;padding-right:12px}'
      + '.mem-count{margin-left:0;width:100%}'
      + '.mem-tbl,.mem-tbl tbody,.mem-tbl tr,.mem-tbl td{display:block;width:100%;box-sizing:border-box}'
      + '.mem-tbl{border:none;background:transparent}.mem-tbl thead{display:none}'
      + '.mem-tbl tr{background:#fff;border:1px solid var(--line,#e4e7ec);border-radius:12px;margin-bottom:10px;padding:6px 0}'
      + '.mem-tbl td{border:none;padding:4px 12px}'
      + '.mem-tbl td[data-l]:not(.mc-main)::before{content:attr(data-l) ": ";color:var(--muted,#8C887E);font-size:11.5px}'
      + '.mem-fr{grid-template-columns:1fr}'
      + '.mem-modal{padding:0;align-items:stretch}.mem-md{max-height:none;height:100%;border-radius:0;max-width:none}'
      + '.mem-kv{grid-template-columns:95px 1fr}'
      + '}';
    var st = document.createElement('style');
    st.id = 'mem-css';
    st.textContent = css;
    (document.head || document.documentElement).appendChild(st);
  }

  // ── shared pieces ─────────────────────────────────────────────────────────
  function noteItem(n, opts) {
    opts = opts || {};
    var meta = [];
    if (opts.showEntity && entityLabel(n)) meta.push(esc(entityLabel(n)));
    meta.push(esc(SOURCE_LABEL[n.source] || n.source || ''));
    meta.push('updated ' + esc(fmtDate(n.updated_at)));
    if (n.valid_until) meta.push('until ' + esc(fmtDate(n.valid_until)));
    return '<div class="mem-item' + (n.importance === 'high' ? ' imp-high' : '') + '" data-act="mem-note" data-id="' + (+n.id) + '" role="button" tabindex="0">'
      + '<div class="h"><span class="t">' + esc(n.title || '(no title)') + '</span><span>'
      + (opts.showScope ? scopeBadge(n.scope) : '') + (n.importance === 'high' ? impBadge('high') : '') + stateBadges(n) + '</span></div>'
      + '<div class="c">' + esc(n.content) + '</div>'
      + '<div class="m">' + meta.join(' &middot; ') + '</div></div>';
  }
  function noteList(notes, opts, emptyMsg) {
    if (!notes || !notes.length) {
      if (emptyMsg === '') return '';
      return '<div class="mem-empty" style="padding:10px;text-align:left">' + esc(emptyMsg || 'No notes yet.') + '</div>';
    }
    return notes.map(function (n) { return noteItem(n, opts); }).join('');
  }
  function quickAdd(key, preset, refresh, placeholder) {
    QK[key] = { preset: preset, refresh: refresh };
    return '<div class="mem-qa" data-k="' + esc(key) + '">'
      + '<textarea class="mem-ta" data-memqa="text" placeholder="' + esc(placeholder || 'Add a note for the team (and Claude)…') + '" aria-label="New note"></textarea>'
      + '<div class="row">'
      + '<label>Importance <select class="mem-sel" data-memqa="imp">' + IMPORTANCE.map(function (i) { return '<option value="' + i[0] + '"' + (i[0] === 'normal' ? ' selected' : '') + '>' + i[1] + '</option>'; }).join('') + '</select></label>'
      + '<label title="For time-bound notes such as an interview slot">Valid until <input type="date" class="mem-in" data-memqa="until"></label>'
      + '<span class="sp"></span>'
      + '<button type="button" class="mem-btn" data-act="mem-quick-more" data-k="' + esc(key) + '">More fields…</button>'
      + '<button type="button" class="mem-btn pri" data-act="mem-quick" data-k="' + esc(key) + '">Add note</button>'
      + '</div></div>';
  }
  function remount() {
    Object.keys(MOUNTED).forEach(function (k) {
      var m = MOUNTED[k];
      if (!m.box || !document.body.contains(m.box)) { delete MOUNTED[k]; return; }
      try { m.fn(); } catch (e) {}
    });
  }
  function refreshAll() {
    var v = $('memory-view');
    if (v && v.style.display !== 'none' && P.built) loadList();
    remount();
    if (DRAWER_ID && $('mem-drawer') && $('mem-drawer').classList.contains('open')) openDrawer(DRAWER_ID);
  }

  // ── Memory page ───────────────────────────────────────────────────────────
  function buildPage() {
    var v = $('memory-view');
    if (!v) return false;
    injectCss();
    var scopeOpts = '<option value="">All scopes</option>' + SCOPES.map(function (s) { return '<option value="' + s[0] + '">' + s[1] + '</option>'; }).join('');
    var impOpts = '<option value="">Any importance</option>' + IMPORTANCE.map(function (s) { return '<option value="' + s[0] + '">' + s[1] + '</option>'; }).join('');
    v.innerHTML = ''
      + '<div class="mem-head"><div class="mem-ttl"><div class="ic" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M12 3a6 6 0 00-6 6c0 2.2 1.2 3.6 2.4 4.8.8.8 1.6 1.7 1.6 3.2h4c0-1.5.8-2.4 1.6-3.2C16.8 12.6 18 11.2 18 9a6 6 0 00-6-6zM10 20h4"/></svg></div>'
      + '<div><div class="t">Memory</div><div class="s">Shared business notes on clients, jobs, candidates and the team. Claude reads these too.</div></div></div>'
      + '<button type="button" class="mem-btn pri" data-act="mem-new">+ New note</button></div>'
      + '<div class="mem-filters" role="search">'
      + '<input type="search" class="mem-in" id="mem-q" data-memf="q" placeholder="Search notes, names, tags…" aria-label="Search notes">'
      + '<select class="mem-sel" id="mem-scope" data-memf="scope" aria-label="Scope">' + scopeOpts + '</select>'
      + '<select class="mem-sel" id="mem-imp" data-memf="importance" aria-label="Importance">' + impOpts + '</select>'
      + '<label class="mem-chk"><input type="checkbox" data-memf="archived"> Show archived</label>'
      + '<label class="mem-chk"><input type="checkbox" data-memf="expired"> Show expired</label>'
      + '<span class="mem-count" id="mem-count" aria-live="polite"></span></div>'
      + '<div class="mem-body" id="mem-list">' + loading() + '</div>';
    P.built = true;
    return true;
  }

  function loadList() {
    var box = $('mem-list');
    if (!box) return;
    var want = qs({ q: P.q, scope: P.scope, importance: P.importance, include_archived: P.archived ? 'true' : '',
                    include_expired: P.expired ? 'true' : '', limit: 200 });
    P._want = want;
    api('GET', want).then(function (j) {
      if (P._want !== want) return;                  // a newer search is running
      if (j.error) { box.innerHTML = '<div class="mem-err">' + esc(j.error) + '</div>'; $('mem-count').textContent = ''; return; }
      P.notes = j.notes || [];
      $('mem-count').textContent = P.notes.length + (P.notes.length === 200 ? '+' : '') + ' note' + (P.notes.length === 1 ? '' : 's');
      if (!P.notes.length) {
        var filtered = P.q || P.scope || P.importance;
        box.innerHTML = '<div class="mem-empty">' + (filtered ? 'No notes match these filters.' : 'No notes yet. Add the first one with “+ New note”, or ask Claude to remember something.') + '</div>';
        return;
      }
      box.innerHTML = '<table class="mem-tbl"><thead><tr><th>Entity</th><th>Scope</th><th>Title</th><th>Importance</th><th>Source</th><th>Updated</th></tr></thead><tbody>'
        + P.notes.map(function (n) {
          return '<tr data-act="mem-note" data-id="' + (+n.id) + '" tabindex="0"' + (n.status === 'archived' ? ' class="is-arch"' : '') + '>'
            + '<td class="mc-main" data-l="Entity"><div class="ent">' + esc(entityLabel(n) || '—') + '</div>'
            + (n.client_name && n.client_name !== n.entity_name ? '<div class="cl">' + esc(n.client_name) + '</div>' : '') + '</td>'
            + '<td data-l="Scope">' + scopeBadge(n.scope) + '</td>'
            + '<td class="mc-main" data-l="Title"><div class="ttl">' + esc(n.title || '(no title)') + ' ' + stateBadges(n) + '</div><div class="sn">' + esc(n.content) + '</div></td>'
            + '<td data-l="Importance">' + impBadge(n.importance) + '</td>'
            + '<td data-l="Source">' + srcBadge(n.source) + '</td>'
            + '<td class="nw" data-l="Updated">' + esc(fmtDate(n.updated_at)) + (n.valid_until ? '<div class="cl">until ' + esc(fmtDate(n.valid_until)) + '</div>' : '') + '</td></tr>';
        }).join('') + '</tbody></table>';
    });
  }

  MemUI.open = function () {
    try { if (typeof window.closeSidebarOnMobile === 'function') window.closeSidebarOnMobile(); } catch (e) {}
    try { if (typeof window._hideAllViews === 'function') window._hideAllViews(); } catch (e) {}
    var v = $('memory-view');
    if (!v) return;
    v.style.display = 'flex';
    var sb = $('sb-memory'); if (sb) sb.className = 'sb-gear active';
    var home = $('sb-home'); if (home) home.classList.remove('active');
    var st = appState(); if (st) st.settingsOpen = false;
    if (!P.built) buildPage();
    loadList();
    try { if (typeof window.writeHash === 'function') window.writeHash(); } catch (e) {}
  };

  // ── drawer: one note + history ────────────────────────────────────────────
  function ensureDrawer() {
    var d = $('mem-drawer');
    if (d) return d;
    injectCss();
    d = document.createElement('div');
    d.id = 'mem-drawer';
    d.className = 'mem-drawer';
    d.innerHTML = '<div class="mem-dr-bg" data-act="mem-close"></div>'
      + '<aside class="mem-dr" role="dialog" aria-modal="true" aria-labelledby="mem-dr-title"><div class="mem-dr-h"><div class="t" id="mem-dr-title">Note</div>'
      + '<button type="button" class="mem-x" data-act="mem-close" aria-label="Close">&times;</button></div>'
      + '<div class="mem-dr-b" id="mem-dr-body"></div><div class="mem-dr-f" id="mem-dr-foot"></div></aside>';
    document.body.appendChild(d);
    return d;
  }
  function closeDrawer() {
    var d = $('mem-drawer'); if (d) d.classList.remove('open');
    DRAWER_ID = 0;
  }
  var HIST_LABEL = { create: 'Created', update: 'Edited', archive: 'Archived', unarchive: 'Restored' };
  var FIELD_LABEL = { scope: 'scope', entity_id: 'link', entity_name: 'entity', client_name: 'client', title: 'title',
                      content: 'text', tags: 'tags', importance: 'importance', valid_until: 'valid until', status: 'status', source: 'source' };
  function histItem(h) {
    var ch = h.changes || {};
    var fields = Object.keys(ch).filter(function (k) { return k !== 'source' && k !== 'status'; }).map(function (k) { return FIELD_LABEL[k] || k; });
    var line = '<b>' + esc(HIST_LABEL[h.action] || h.action) + '</b>'
      + (h.action === 'create' && ch.source ? ' via ' + esc(SOURCE_LABEL[ch.source] || ch.source) : '')
      + (h.action === 'update' && fields.length ? ': ' + esc(fields.join(', ')) : '');
    var diff = '';
    if (h.action === 'update' && ch.content) {
      diff = '<details><summary>Show the text change</summary><div class="w">Before</div><pre>' + esc(h.old_content) + '</pre><div class="w">After</div><pre>' + esc(h.new_content) + '</pre></details>';
    }
    return '<div class="mem-hi">' + line + '<div class="w">' + esc(h.changed_by_name || 'someone') + ' &middot; ' + esc(fmtDateTime(h.changed_at)) + '</div>' + diff + '</div>';
  }
  function openDrawer(id) {
    var d = ensureDrawer();
    DRAWER_ID = id;
    d.classList.add('open');
    $('mem-dr-title').textContent = 'Note';
    $('mem-dr-body').innerHTML = loading();
    $('mem-dr-foot').innerHTML = '';
    api('GET', '/' + (+id)).then(function (j) {
      if (DRAWER_ID !== id) return;
      if (j.error) { $('mem-dr-body').innerHTML = '<div class="mem-err">' + esc(j._status === 404 ? 'This note is not available.' : j.error) + '</div>'; return; }
      var n = j.note;
      $('mem-dr-title').textContent = n.title || '(no title)';
      var tags = (n.tags || []).map(function (t) { return '<span class="mem-b">' + esc(t) + '</span>'; }).join('');
      var kv = [['Scope', scopeBadge(n.scope) + stateBadges(n)], ['About', esc(entityLabel(n) || '—')]];
      if (n.client_name && n.client_name !== n.entity_name) kv.push(['Client', esc(n.client_name)]);
      kv.push(['Importance', impBadge(n.importance)]);
      kv.push(['Source', srcBadge(n.source)]);
      if (n.valid_until) kv.push(['Valid until', esc(fmtDate(n.valid_until)) + (n.expired ? ' <span class="mem-b mem-st-exp">Expired</span>' : '')]);
      if (tags) kv.push(['Tags', tags]);
      kv.push(['Created', esc(fmtDateTime(n.created_at))]);
      kv.push(['Updated', esc(fmtDateTime(n.updated_at))]);
      $('mem-dr-body').innerHTML = '<div class="mem-kv">' + kv.map(function (r) { return '<span>' + r[0] + '</span><span>' + r[1] + '</span>'; }).join('') + '</div>'
        + '<div class="mem-content">' + esc(n.content) + '</div>'
        + '<div class="mem-hist"><h4>History</h4>' + ((n.history || []).map(histItem).join('') || '<div class="mem-empty">No history.</div>') + '</div>';
      var foot = '';
      if (n.can_edit) {
        foot += '<button type="button" class="mem-btn pri" data-act="mem-edit" data-id="' + (+n.id) + '">Edit</button>';
        foot += n.status === 'archived'
          ? '<button type="button" class="mem-btn" data-act="mem-unarchive" data-id="' + (+n.id) + '">Restore</button>'
          : '<button type="button" class="mem-btn dang" data-act="mem-archive" data-id="' + (+n.id) + '">Archive</button>';
      } else {
        foot += '<span class="mem-fl" style="margin:0;align-self:center">Read-only for your account.</span>';
      }
      foot += '<button type="button" class="mem-btn" data-act="mem-close" style="margin-left:auto">Close</button>';
      $('mem-dr-foot').innerHTML = foot;
      d._note = n;
    });
  }
  MemUI.openNote = openDrawer;

  // ── form (new / edit) ─────────────────────────────────────────────────────
  function ensureModal() {
    var m = $('mem-modal');
    if (m) return m;
    injectCss();
    m = document.createElement('div');
    m.id = 'mem-modal';
    m.className = 'mem-modal';
    m.innerHTML = '<div class="mem-md" role="dialog" aria-modal="true" aria-labelledby="mem-md-title"><div class="mem-md-h"><h2 id="mem-md-title">New note</h2>'
      + '<button type="button" class="mem-x" data-act="mem-cancel" aria-label="Close">&times;</button></div>'
      + '<div class="mem-md-b" id="mem-md-body"></div>'
      + '<div class="mem-md-f"><span class="mem-ferr" id="mem-ferr" role="alert"></span>'
      + '<button type="button" class="mem-btn" data-act="mem-cancel">Cancel</button>'
      + '<button type="button" class="mem-btn pri" data-act="mem-save" id="mem-save">Save note</button></div></div>';
    document.body.appendChild(m);
    m.addEventListener('mousedown', function (e) { if (e.target === m) m._downOnBg = true; else m._downOnBg = false; });
    m.addEventListener('click', function (e) { if (e.target === m && m._downOnBg) closeModal(); });
    return m;
  }
  function closeModal() { var m = $('mem-modal'); if (m) m.classList.remove('open'); FORM = null; }
  function mandateOptions(sel) {
    var st = appState(), ms = (st && st.mandates) || [];
    ms = ms.filter(function (m) { return m && !m._central && m.status !== 'central'; });
    return '<option value="">— pick a job —</option>' + ms.map(function (m) {
      var label = (m.role || 'Job') + (m.client ? ' — ' + m.client : '') + (m.location ? ' (' + m.location + ')' : '') + (m.status && m.status !== 'active' ? ' [' + m.status + ']' : '');
      return '<option value="' + (+m.id) + '"' + (+sel === +m.id ? ' selected' : '') + '>' + esc(label) + '</option>';
    }).join('');
  }
  function syncFormScope() {
    var s = ($('mem-f-scope') || {}).value || 'general';
    var rowM = $('mem-f-mrow'), rowE = $('mem-f-erow'), lblE = $('mem-f-elbl'), rowC = $('mem-f-crow');
    if (rowM) rowM.style.display = (s === 'mandate') ? '' : 'none';
    if (rowE) rowE.style.display = (s === 'general') ? 'none' : '';
    if (rowC) rowC.style.display = (s === 'general' || s === 'team' || s === 'client') ? 'none' : '';
    var ph = { client: 'e.g. Lauritz & Knudsen', mandate: 'Filled from the job you pick (or type a name)', candidate: 'Candidate name',
               person: 'e.g. Nishtha Bajaj', team: 'e.g. Charanpreet Kaur' };
    var ent = $('mem-f-entity'); if (ent) ent.placeholder = ph[s] || '';
    if (lblE) lblE.firstChild.nodeValue = (s === 'person' ? 'Person' : s === 'team' ? 'Team member' : s === 'client' ? 'Client' : s === 'mandate' ? 'Job name' : 'Name') + ' ';
  }
  function openForm(note, preset) {
    var m = ensureModal();
    preset = preset || {};
    FORM = { id: note ? note.id : 0, preset: preset, note: note || null };
    var src = note || preset;
    var locked = !!note || !!preset.scope;      // the link (scope + record) is fixed when editing / quick-adding
    var scope = src.scope || 'general';
    $('mem-md-title').textContent = note ? 'Edit note' : 'New note';
    var scopes = SCOPES.filter(function (s) { return canWriteScope(s[0]) && !(isRecruiter() && s[0] === 'candidate'); });
    var h = '';
    if (locked) {
      h += '<div class="mem-lock">' + scopeBadge(scope) + ' ' + esc(src.entity_name || (scope === 'general' ? 'Agency-wide' : ''))
        + (src.client_name && src.client_name !== src.entity_name ? ' &middot; ' + esc(src.client_name) : '') + '</div>'
        + '<input type="hidden" id="mem-f-scope" value="' + esc(scope) + '">';
    } else {
      h += '<label class="mem-fl">What is it about?<select class="mem-sel" id="mem-f-scope">'
        + scopes.map(function (s) { return '<option value="' + s[0] + '"' + (s[0] === scope ? ' selected' : '') + '>' + s[1] + '</option>'; }).join('')
        + '</select>' + (isRecruiter() ? '<span class="hint">To add a note on a candidate, open their profile → Notes.</span>' : '') + '</label>'
        + '<label class="mem-fl" id="mem-f-mrow">Job<select class="mem-sel" id="mem-f-mandate">' + mandateOptions(src.entity_id) + '</select></label>';
    }
    h += '<div class="mem-fr"' + (locked && !note ? ' style="display:none"' : '') + '>'
      + '<label class="mem-fl" id="mem-f-erow"><span id="mem-f-elbl">Name </span><input class="mem-in" id="mem-f-entity" maxlength="200" value="' + esc(src.entity_name || '') + '"></label>'
      + '<label class="mem-fl" id="mem-f-crow">Client (optional)<input class="mem-in" id="mem-f-client" maxlength="200" value="' + esc(src.client_name || '') + '" placeholder="Which client this belongs to"></label></div>'
      + '<label class="mem-fl">Title<input class="mem-in" id="mem-f-title" maxlength="200" value="' + esc(note ? note.title : '') + '" placeholder="One line, e.g. Wants DM-level profiles (grade 4)"></label>'
      + '<label class="mem-fl">Note<textarea class="mem-ta" id="mem-f-content" maxlength="10000" placeholder="The fact, with who said it and when">' + esc(note ? note.content : (preset.content || '')) + '</textarea></label>'
      + '<div class="mem-fr"><label class="mem-fl">Importance<select class="mem-sel" id="mem-f-imp">'
      + IMPORTANCE.map(function (i) { var cur = note ? note.importance : (preset.importance || 'normal'); return '<option value="' + i[0] + '"' + (i[0] === cur ? ' selected' : '') + '>' + i[1] + '</option>'; }).join('')
      + '</select></label>'
      + '<label class="mem-fl">Valid until<input type="date" class="mem-in" id="mem-f-until" value="' + esc(((note ? note.valid_until : preset.valid_until) || '').slice(0, 10)) + '"><span class="hint">Only for time-bound notes (e.g. an interview slot).</span></label></div>'
      + '<label class="mem-fl">Tags<input class="mem-in" id="mem-f-tags" value="' + esc(((note && note.tags) || []).join(', ')) + '" placeholder="Comma separated, e.g. rejection-pattern, DM"></label>';
    $('mem-md-body').innerHTML = h;
    $('mem-ferr').textContent = '';
    $('mem-save').disabled = false;
    syncFormScope();
    m.classList.add('open');
    setTimeout(function () { var f = $(locked ? 'mem-f-title' : 'mem-f-scope'); if (f) f.focus(); }, 30);
  }
  function val(id) { var e = $(id); return e ? String(e.value || '').trim() : ''; }
  function saveForm() {
    if (!FORM) return;
    var f = FORM, scope = val('mem-f-scope') || 'general';
    var body = {
      title: val('mem-f-title'), content: val('mem-f-content'), importance: val('mem-f-imp') || 'normal',
      valid_until: val('mem-f-until'),
      tags: val('mem-f-tags') ? val('mem-f-tags').split(',').map(function (t) { return t.trim(); }).filter(Boolean) : []
    };
    if (!body.content) { $('mem-ferr').textContent = 'Write the note first.'; return; }
    if (scope !== 'general') {
      if ($('mem-f-entity')) body.entity_name = val('mem-f-entity');
      if ($('mem-f-client') && $('mem-f-crow') && $('mem-f-crow').style.display !== 'none') body.client_name = val('mem-f-client');
    }
    var req;
    if (f.id) {
      req = api('PUT', '/' + f.id, body);            // scope / link stay as they are
    } else {
      body.scope = scope;
      var p = f.preset || {};
      if (p.scope) {
        if (p.entity_id) body.entity_id = p.entity_id;
        if (!body.entity_name && p.entity_name) body.entity_name = p.entity_name;
        if (!body.client_name && p.client_name) body.client_name = p.client_name;
      } else if (scope === 'mandate') {
        var mid = +val('mem-f-mandate');
        if (mid) body.entity_id = mid;
        else if (!body.entity_name) { $('mem-ferr').textContent = 'Pick the job.'; return; }
      }
      if (scope !== 'general' && !body.entity_id && !body.entity_name) { $('mem-ferr').textContent = 'Say who or what this note is about.'; return; }
      req = api('POST', '', body);
    }
    $('mem-save').disabled = true;
    $('mem-ferr').textContent = '';
    req.then(function (j) {
      if (!FORM) return;
      $('mem-save').disabled = false;
      if (j.error) { $('mem-ferr').textContent = j.error; return; }
      closeModal();
      toastMsg('OK', f.id ? 'Note saved' : 'Note added');
      refreshAll();
    });
  }

  // ── archive / restore ─────────────────────────────────────────────────────
  function setStatus(id, archive) {
    if (archive && !confirm('Archive this note? It is hidden from lists and from Claude, and can be restored any time.')) return;
    api('POST', '/' + (+id) + (archive ? '/archive' : '/unarchive')).then(function (j) {
      if (j.error) { toastMsg('ERR', j.error); return; }
      toastMsg('OK', archive ? 'Note archived' : 'Note restored');
      refreshAll();
    });
  }

  // ── quick add (candidate / job / client) ──────────────────────────────────
  function quickSave(key, btn) {
    var q = QK[key]; if (!q) return;
    var wrap = btn.closest('.mem-qa');
    var ta = wrap.querySelector('[data-memqa="text"]');
    var text = String(ta.value || '').trim();
    if (!text) { ta.focus(); return; }
    var body = {};
    Object.keys(q.preset).forEach(function (k) { if (q.preset[k] !== '' && q.preset[k] != null) body[k] = q.preset[k]; });
    body.content = text;
    body.importance = wrap.querySelector('[data-memqa="imp"]').value || 'normal';
    var until = wrap.querySelector('[data-memqa="until"]').value;
    if (until) body.valid_until = until;
    btn.disabled = true;
    api('POST', '', body).then(function (j) {
      btn.disabled = false;
      if (j.error) { toastMsg('ERR', j.error); return; }
      ta.value = '';
      toastMsg('OK', 'Note added');
      refreshAll();
    });
  }
  function quickMore(key, btn) {
    var q = QK[key]; if (!q) return;
    var wrap = btn.closest('.mem-qa');
    var preset = {};
    Object.keys(q.preset).forEach(function (k) { preset[k] = q.preset[k]; });
    preset.content = wrap.querySelector('[data-memqa="text"]').value || '';
    preset.importance = wrap.querySelector('[data-memqa="imp"]').value || 'normal';
    preset.valid_until = wrap.querySelector('[data-memqa="until"]').value || '';
    openForm(null, preset);
  }

  function errBox(j) {
    return '<div class="mem-err">' + esc(j._status === 404 ? 'Notes are not available for this record.' : (j.error || 'Could not load notes.')) + '</div>';
  }
  function plainLegacy(rows) {
    return (rows || []).map(function (r) {
      return '<div class="mem-plain">' + esc(r.note) + '<div class="m">Client note on the job &middot; ' + esc(fmtDate(r.created_at)) + '</div></div>';
    }).join('');
  }
  function clientGroups(c) {
    if (!c) return '';
    var h = '';
    if ((c.notes || []).length) h += '<div class="mem-grp">Client' + (c.names && c.names.length ? ' &middot; ' + esc(c.names.join(' / ')) : '') + '</div>' + noteList(c.notes);
    if ((c.people_notes || []).length) h += '<div class="mem-grp">People at the client</div>' + noteList(c.people_notes, { showEntity: true });
    return h;
  }

  // Candidate profile → Notes tab
  MemUI.mountCandidate = function (cid) {
    cid = +cid;
    var box = $('cp-memory-' + cid);
    if (!box) return;
    injectCss();
    MOUNTED['cand' + cid] = { box: box, fn: function () { MemUI.mountCandidate(cid); } };
    if (!box.innerHTML) box.innerHTML = loading();
    api('GET', '/context' + qs({ candidate_id: cid })).then(function (j) {
      if (j.error) { box.innerHTML = errBox(j); return; }
      var cand = j.candidate || {};
      var preset = { scope: 'candidate', entity_id: cid, entity_name: cand.name || '' };
      var h = '<div class="mem-sec"><h3>Notes on ' + esc(cand.name || 'this candidate') + '</h3>'
        + '<p class="sub">Shared business memory for the team. Claude reads these before answering about this candidate.</p>'
        + noteList(j.notes, {}, 'No notes on this candidate yet.')
        + quickAdd('cand' + cid, preset, null, 'e.g. Ref check not positive (client, 26 Nov 2026)')
        + '</div>';
      var f = j.candidate_fields || {};
      if (f.client_feedback || f.recruiter_feedback || f.general_comments) {
        h += '<div class="mem-sec"><h3>From the candidate record</h3><p class="sub">These fields are edited on the profile itself.</p>'
          + (f.client_feedback ? '<div class="mem-grp">Client feedback</div><div class="mem-plain">' + esc(f.client_feedback) + '</div>' : '')
          + (f.recruiter_feedback ? '<div class="mem-grp">Recruiter feedback</div><div class="mem-plain">' + esc(f.recruiter_feedback) + '</div>' : '')
          + (f.general_comments ? '<div class="mem-grp">Comments</div><div class="mem-plain">' + esc(f.general_comments) + '</div>' : '')
          + '</div>';
      }
      var m = j.mandate, c = j.client;
      var linked = 0, inner = '';
      if (m) {
        linked += (m.notes || []).length + (m.client_notes_on_job || []).length;
        if ((m.notes || []).length || (m.client_notes_on_job || []).length) {
          inner += '<div class="mem-grp">Job &middot; ' + esc((m.role || '') + (m.location ? ' (' + m.location + ')' : '')) + '</div>'
            + noteList(m.notes, {}, '') + plainLegacy(m.client_notes_on_job);
        }
      }
      if (c) { linked += (c.notes || []).length + (c.people_notes || []).length; inner += clientGroups(c); }
      if (m || c) {
        h += '<div class="mem-sec"><details><summary>Job &amp; client notes (' + linked + ')</summary>'
          + (inner || '<div class="mem-empty" style="text-align:left;padding:10px 0">No notes on the job or the client yet.</div>') + '</details></div>';
      }
      if ((j.general_high || []).length) {
        h += '<div class="mem-sec"><details><summary>Agency-wide, high importance (' + j.general_high.length + ')</summary>' + noteList(j.general_high) + '</details></div>';
      }
      box.innerHTML = h;
    });
  };

  // Job (mandate) page → Notes tab
  MemUI.mountMandate = function (el, mid) {
    mid = +mid;
    if (!el) return;
    injectCss();
    el.innerHTML = '<div id="mem-job-' + mid + '" style="max-width:900px;margin:0 auto;padding:16px">' + loading() + '</div>';
    var box = $('mem-job-' + mid);
    var run = function () {
      api('GET', '/context' + qs({ mandate_id: mid })).then(function (j) {
        if (!document.body.contains(box)) return;
        if (j.error) { box.innerHTML = errBox(j); return; }
        var m = j.mandate || {};
        var name = (m.role || 'Job') + (m.location ? ' — ' + m.location : '');
        var preset = { scope: 'mandate', entity_id: mid, entity_name: name, client_name: m.client || '' };
        var h = '<div class="mem-sec"><h3>Notes on this job</h3>'
          + '<p class="sub">Priorities, what the client wants, who handles what. Claude reads these before answering about this job.</p>'
          + noteList(m.notes, {}, 'No notes on this job yet.')
          + plainLegacy(m.client_notes_on_job)
          + quickAdd('job' + mid, preset, null, 'e.g. Client said the hire will handle the Ghaziabad branch')
          + '</div>';
        var cn = m.candidate_notes || [];
        h += '<div class="mem-sec"><h3>Notes on this job’s candidates <span class="mem-b">' + cn.length + '</span></h3>'
          + noteList(cn, { showEntity: true }, 'No candidate notes yet. Add them from a candidate’s profile → Notes.') + '</div>';
        var c = j.client, ccount = c ? (c.notes || []).length + (c.people_notes || []).length : 0;
        h += '<div class="mem-sec"><details' + (ccount ? ' open' : '') + '><summary>Client notes (' + ccount + ')</summary>'
          + (clientGroups(c) || '<div class="mem-empty" style="text-align:left;padding:10px 0">No notes on ' + esc(m.client || 'this client') + ' yet.</div>') + '</details></div>';
        box.innerHTML = h;
      });
    };
    MOUNTED['job' + mid] = { box: box, fn: run };
    run();
  };

  // CRM client record → Memory notes card
  MemUI.mountClient = function (clientId, name) {
    clientId = +clientId;
    var box = $('cd-memory-' + clientId);
    if (!box || !name) return;
    injectCss();
    MOUNTED['client' + clientId] = { box: box, fn: function () { MemUI.mountClient(clientId, name); } };
    if (!box.innerHTML) box.innerHTML = loading();
    var key = String(name).trim().toLowerCase();
    api('GET', '/context' + qs({ client: name })).then(function (j) {
      if (j.error) { box.innerHTML = errBox(j); return; }
      var c = j.client || {};
      // exact client only (the context search is a partial match)
      var mine = function (n) {
        return (n.scope === 'client' && +n.entity_id === clientId) || String(n.entity_name || '').trim().toLowerCase() === key
          || String(n.client_name || '').trim().toLowerCase() === key;
      };
      var notes = (c.notes || []).filter(mine), people = (c.people_notes || []).filter(mine);
      var h = '<div class="mem-sec" style="margin-top:14px"><h3>Memory notes</h3>'
        + '<p class="sub">Preferences, priorities, rejection patterns. Shared with the team and Claude.</p>'
        + noteList(notes, {}, 'No notes on this client yet.')
        + (people.length ? '<div class="mem-grp">People at the client</div>' + noteList(people, { showEntity: true }) : '')
        + (canWriteScope('client') ? quickAdd('client' + clientId, { scope: 'client', entity_id: clientId, entity_name: name }, null, 'e.g. Wants DM-level profiles (grade 4, 6–8 LPA)') : '')
        + '</div>';
      box.innerHTML = h;
    });
  };

  // ── one delegated listener for everything above ───────────────────────────
  document.addEventListener('click', function (e) {
    var a = e.target.closest ? e.target.closest('[data-act^="mem-"]') : null;
    if (!a) return;
    var act = a.getAttribute('data-act'), id = +a.getAttribute('data-id') || 0, k = a.getAttribute('data-k') || '';
    if (act === 'mem-nav') {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;   // new tab: let the browser do it
      e.preventDefault();
      MemUI.open();
      return;
    }
    if (act === 'mem-note') { if (e.target.closest('a,button,input,textarea,select,summary')) return; openDrawer(id); return; }
    e.preventDefault();
    if (act === 'mem-close') return closeDrawer();
    if (act === 'mem-new') return openForm(null, null);
    if (act === 'mem-edit') { var d = $('mem-drawer'); if (d && d._note && +d._note.id === id) openForm(d._note, null); return; }
    if (act === 'mem-archive') return setStatus(id, true);
    if (act === 'mem-unarchive') return setStatus(id, false);
    if (act === 'mem-cancel') return closeModal();
    if (act === 'mem-save') return saveForm();
    if (act === 'mem-quick') return quickSave(k, a);
    if (act === 'mem-quick-more') return quickMore(k, a);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      var m = $('mem-modal');
      // consume the key so other Escape handlers (panels behind) do not also fire
      if (m && m.classList.contains('open')) { e.stopImmediatePropagation(); closeModal(); return; }
      var d = $('mem-drawer');
      if (d && d.classList.contains('open')) { e.stopImmediatePropagation(); closeDrawer(); return; }
    }
    if (e.key === 'Enter' && e.target && e.target.getAttribute) {
      var row = e.target.closest && e.target.closest('[data-act="mem-note"]');
      if (row && row === e.target) { e.preventDefault(); openDrawer(+row.getAttribute('data-id')); return; }
      if ((e.ctrlKey || e.metaKey) && e.target.getAttribute('data-memqa') === 'text') {
        var btn = e.target.closest('.mem-qa').querySelector('[data-act="mem-quick"]');
        if (btn) { e.preventDefault(); quickSave(btn.getAttribute('data-k'), btn); }
        return;
      }
      if ((e.ctrlKey || e.metaKey) && FORM && e.target.closest && e.target.closest('#mem-modal')) { e.preventDefault(); saveForm(); }
    }
  });
  function onFilter(e) {
    var t = e.target;
    if (!t || !t.getAttribute) return;
    var f = t.getAttribute('data-memf');
    if (f) {
      if (f === 'archived' || f === 'expired') P[f] = !!t.checked;
      else P[f] = String(t.value || '').trim();
      clearTimeout(_qTimer);
      _qTimer = setTimeout(loadList, f === 'q' ? 300 : 0);
      return;
    }
    if (t.id === 'mem-f-scope' && e.type === 'change') syncFormScope();
  }
  document.addEventListener('input', onFilter);
  document.addEventListener('change', onFilter);
})();
