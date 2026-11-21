/* BD workspace -> Calling mode: the cold-calling sheet (modules/bd_calling.py).
   One screen for: call -> understand the business -> note -> e-mail -> follow up.
   Loaded by index.html when the "Calling" page of the BD workspace opens:
     BDCalling.open(containerEl, workspaceId)   BDCalling.close()                       */
(function(){
'use strict';
var API = '/api/bd/calling';
var S = {root:null, ws:0, view:'today', q:'', mine:false, rows:[], counts:{}, sel:null, checked:{}, today:{calls:0, emails:0},
         settings:{}, meta:null, detail:null, loading:false, busy:{}, onKey:null, timer:null, rec:null, review:null};

/* ---------- helpers ---------- */
function esc(x){ return String(x == null ? '' : x).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
function $(s, r){ return (r || S.root).querySelector(s); }
function $$(s, r){ return Array.prototype.slice.call((r || S.root).querySelectorAll(s)); }
function api(method, path, body){
  var url = API + path + (path.indexOf('?') >= 0 ? '&' : '?') + 'ws=' + (S.ws || 0);
  var o = {method: method, credentials: 'same-origin', headers: {}};
  if (body !== undefined){ o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
  return fetch(url, o).then(function(r){ return r.json().catch(function(){ return {}; }).then(function(j){ j = j || {}; if (!r.ok){ j.ok = false; j.error = j.error || ('HTTP ' + r.status); } return j; }); })
    .catch(function(){ return {ok:false, error:'Network error - check your connection.'}; });
}
function toast(t, bad){ var d = document.createElement('div'); d.className = 'bdc-toast' + (bad ? ' bad' : ''); d.textContent = t; document.body.appendChild(d); setTimeout(function(){ d.remove(); }, bad ? 4200 : 2600); }
function pad(n){ return (n < 10 ? '0' : '') + n; }
function localIso(d){ return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+'T'+pad(d.getHours())+':'+pad(d.getMinutes()); }
/* the server keeps India time (IST, no zone in the string); the browser may be anywhere */
function parseTs(s){ if (!s) return null; var x = String(s).replace(' ', 'T'); if (x.length === 16) x += ':00'; var d = new Date(x.slice(0, 19) + '+05:30'); return isNaN(d) ? null : d; }
function istIso(d){ if (!d || isNaN(d)) return ''; var t = new Date(d.getTime() + (330 + d.getTimezoneOffset()) * 60000); return localIso(t); }
function inputToIst(v){ if (!v) return ''; var d = new Date(v); return istIso(d); }
function when(s){
  var d = parseTs(s); if (!d) return '';
  var t = new Date(), y = new Date(t); y.setDate(t.getDate() - 1); var tm = new Date(t); tm.setDate(t.getDate() + 1);
  var hm = d.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
  if (d.toDateString() === t.toDateString()) return 'Today ' + hm;
  if (d.toDateString() === tm.toDateString()) return 'Tomorrow ' + hm;
  if (d.toDateString() === y.toDateString()) return 'Yesterday ' + hm;
  return d.toLocaleDateString([], {day:'numeric', month:'short'}) + ' ' + hm;
}
function statusLabel(k){ var m = (S.meta && S.meta.statuses) || []; for (var i = 0; i < m.length; i++) if (m[i][0] === k) return m[i][1]; return k; }
var ST_COLORS = {'new':'#667085', no_answer:'#B54708', callback:'#3538CD', interested:'#067647', send_details:'#0E7090',
                 meeting:'#6941C6', not_interested:'#B42318', wrong_number:'#98A2B3'};
var OUTCOMES = [['no_answer','📵','No answer','1'],['callback','🔁','Callback','2'],['interested','👍','Interested','3'],
                ['send_details','📧','Send details','4'],['meeting','🤝','Meeting','5'],['not_interested','❌','Not interested','6'],
                ['wrong_number','🚫','Wrong number','7']];
function rowById(id){ for (var i = 0; i < S.rows.length; i++) if (S.rows[i].id === id) return S.rows[i]; return null; }

/* ---------- styles ---------- */
var CSS = '#bdc-root{flex:1;display:flex;flex-direction:column;min-width:0;min-height:0;background:#fff;font-size:13px;color:#101828}'
+ '#bdc-root *{box-sizing:border-box}'
+ '.bdc-top{display:flex;align-items:center;gap:8px;padding:8px 14px;border-bottom:.5px solid #EAECF0;flex-wrap:wrap;flex-shrink:0}'
+ '.bdc-goal{display:flex;align-items:center;gap:6px;font-size:12px;color:#475467;background:#F9FAFB;border:.5px solid #EAECF0;border-radius:8px;padding:4px 8px;cursor:pointer}'
+ '.bdc-goal .bar{width:70px;height:6px;border-radius:4px;background:#EAECF0;overflow:hidden}.bdc-goal .bar i{display:block;height:100%;background:#12B76A;border-radius:4px}'
+ '.bdc-goal b{color:#101828}'
+ '.bdc-views{display:flex;gap:2px;flex-wrap:wrap}'
+ '.bdc-v{border:none;background:transparent;font:inherit;font-size:12.5px;color:#475467;padding:6px 9px;border-radius:7px;cursor:pointer;white-space:nowrap}'
+ '.bdc-v:hover{background:#F2F4F7}.bdc-v.on{background:#EEF0FF;color:#3538CD;font-weight:600}.bdc-v .n{color:#98A2B3;margin-left:3px;font-weight:500}'
+ '.bdc-search{width:180px;font:inherit;font-size:12.5px;padding:6px 10px;border:.5px solid #D0D5DD;border-radius:7px;outline:none}.bdc-search:focus{border-color:#3E63DD}'
+ '.bdc-btn{border:.5px solid #D0D5DD;background:#fff;color:#344054;font:inherit;font-size:12.5px;font-weight:600;padding:6px 10px;border-radius:7px;cursor:pointer;display:inline-flex;align-items:center;gap:5px;white-space:nowrap}'
+ '.bdc-btn:hover{background:#F9FAFB;border-color:#A4B0FF}.bdc-btn:disabled{opacity:.45;cursor:default}'
+ '.bdc-btn.p{background:#3E63DD;border-color:#3E63DD;color:#fff}.bdc-btn.p:hover{background:#3451B2}'
+ '.bdc-btn.g{background:#12B76A;border-color:#12B76A;color:#fff}.bdc-btn.ai{background:#F4F3FF;border-color:#D9D6FE;color:#5925DC}'
+ '.bdc-btn.sm{font-size:11.5px;padding:4px 8px}'
+ '.bdc-body{flex:1;display:flex;min-height:0;min-width:0}'
+ '.bdc-sheet{flex:1;overflow:auto;min-width:0;position:relative}'
+ '.bdc-t{border-collapse:separate;border-spacing:0;width:max-content;min-width:100%}'
+ '.bdc-t th{position:sticky;top:0;z-index:2;background:#F9FAFB;font-size:11.5px;font-weight:600;color:#475467;text-align:left;padding:7px 8px;border-bottom:.5px solid #EAECF0;white-space:nowrap}'
+ '.bdc-t td{border-bottom:.5px solid #F2F4F7;padding:0 4px;height:38px;vertical-align:middle;white-space:nowrap}'
+ '.bdc-t tr:hover td{background:#FCFCFD}.bdc-t tr.sel td{background:#EEF4FF}'
+ '.bdc-t td.s1,.bdc-t th.s1{position:sticky;left:0;z-index:1;background:inherit}.bdc-t th.s1{z-index:3;background:#F9FAFB}'
+ '.bdc-t tr td.s1{background:#fff}.bdc-t tr.sel td.s1{background:#EEF4FF}'
+ '.bdc-in{width:100%;border:1px solid transparent;background:transparent;font:inherit;font-size:12.5px;padding:5px 6px;border-radius:5px;color:#101828;outline:none;text-overflow:ellipsis}'
+ '.bdc-in:hover{border-color:#EAECF0;background:#fff}.bdc-in:focus{border-color:#3E63DD;background:#fff;box-shadow:0 0 0 2px rgba(62,99,221,.15)}'
+ '.bdc-in::placeholder{color:#C0C5CE}'
+ '.bdc-co{display:flex;align-items:center;gap:4px;min-width:200px;max-width:260px}.bdc-co .bdc-in{font-weight:600}'
+ '.bdc-why{font-size:10.5px;font-weight:600;border-radius:999px;padding:1px 7px;white-space:nowrap;flex-shrink:1;min-width:0;max-width:110px;overflow:hidden;text-overflow:ellipsis}'
+ '.bdc-why.b1{background:#FEF3F2;color:#B42318}.bdc-why.b2{background:#ECFDF3;color:#067647}.bdc-why.b3{background:#FFF6ED;color:#C4320A}.bdc-why.b4{background:#F2F4F7;color:#475467}'
+ '.bdc-pill{display:inline-flex;align-items:center;gap:4px;font-size:11.5px;font-weight:600;border-radius:999px;padding:2px 9px;border:none;cursor:pointer;font-family:inherit}'
+ '.bdc-ic{border:none;background:transparent;cursor:pointer;font-size:14px;padding:3px 5px;border-radius:6px;line-height:1}.bdc-ic:hover{background:#F2F4F7}'
+ '.bdc-dots{display:inline-flex;gap:2px}.bdc-dots i{width:6px;height:6px;border-radius:2px;background:#EAECF0}.bdc-dots i.on{background:#7A5AF8}'
+ '.bdc-mail{font-size:11.5px;color:#475467}.bdc-mail.op{color:#C4320A;font-weight:600}.bdc-mail.rp{color:#067647;font-weight:700}'
+ '.bdc-panel{width:440px;flex-shrink:0;border-left:.5px solid #EAECF0;overflow-y:auto;background:#FCFCFD;min-height:0}'
+ '.bdc-ph{position:sticky;top:0;z-index:2;background:#fff;border-bottom:.5px solid #EAECF0;padding:12px 14px}'
+ '.bdc-ph h3{margin:0;font-size:16px;display:flex;align-items:center;gap:8px}.bdc-ph .sub{color:#667085;font-size:12px;margin-top:2px}'
+ '.bdc-sec{padding:12px 14px;border-bottom:.5px solid #EAECF0;background:#fff;margin-bottom:6px}'
+ '.bdc-sec h4{margin:0 0 8px;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:#667085;display:flex;align-items:center;gap:6px}'
+ '.bdc-sec h4 .r{margin-left:auto;display:flex;gap:4px;text-transform:none;letter-spacing:0}'
+ '.bdc-outs{display:grid;grid-template-columns:repeat(4,1fr);gap:5px}'
+ '.bdc-out{border:.5px solid #D0D5DD;background:#fff;border-radius:8px;padding:7px 4px;font:inherit;font-size:11.5px;font-weight:600;color:#344054;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:2px}'
+ '.bdc-out:hover{border-color:#3E63DD;background:#F5F8FF}.bdc-out .k{font-size:9.5px;color:#98A2B3;font-weight:500}'
+ '.bdc-ta{width:100%;font:inherit;font-size:12.5px;border:.5px solid #D0D5DD;border-radius:8px;padding:7px 9px;resize:vertical;outline:none;min-height:34px;background:#fff}'
+ '.bdc-ta:focus{border-color:#3E63DD}'
+ '.bdc-uf{margin-bottom:7px}.bdc-uf label{display:block;font-size:11.5px;font-weight:600;color:#344054;margin-bottom:2px}'
+ '.bdc-uf .bdc-ta{min-height:30px;height:32px}'
+ '.bdc-uf.filled .bdc-ta{background:#FBFAFF;border-color:#D9D6FE}'
+ '.bdc-row{display:flex;gap:6px;align-items:center;flex-wrap:wrap}'
+ '.bdc-kv{font-size:12.5px;color:#475467;line-height:1.6}.bdc-kv b{color:#101828}'
+ '.bdc-script{font-size:13px;line-height:1.55;background:#F5F8FF;border:.5px solid #C7D7FE;border-radius:8px;padding:9px 11px;white-space:pre-wrap}'
+ '.bdc-obj{margin-top:6px;font-size:12.5px}.bdc-obj b{display:block;color:#B42318}'
+ '.bdc-hist{font-size:12px;border-left:2px solid #EAECF0;padding:3px 0 3px 9px;margin-bottom:6px;color:#475467}.bdc-hist b{color:#101828}'
+ '.bdc-em{border:.5px solid #EAECF0;border-radius:8px;padding:7px 9px;margin-bottom:6px;font-size:12px;background:#fff}'
+ '.bdc-em .t{display:flex;gap:6px;align-items:center;font-weight:600;color:#101828}.bdc-em .t span:last-child{margin-left:auto}'
+ '.bdc-tag{font-size:10.5px;font-weight:700;border-radius:999px;padding:1px 7px;background:#F2F4F7;color:#475467}'
+ '.bdc-tag.sent{background:#EEF4FF;color:#3538CD}.bdc-tag.op{background:#FFF6ED;color:#C4320A}.bdc-tag.rp{background:#ECFDF3;color:#067647}.bdc-tag.fail{background:#FEF3F2;color:#B42318}'
+ '.bdc-empty{padding:50px 24px;text-align:center;color:#667085}.bdc-empty h3{color:#101828;margin:0 0 6px}'
+ '.bdc-ov{position:fixed;inset:0;background:rgba(16,24,40,.45);z-index:9000;display:flex;align-items:flex-start;justify-content:center;padding:5vh 12px;overflow:auto}'
+ '.bdc-dlg{background:#fff;border-radius:12px;box-shadow:0 20px 50px rgba(16,24,40,.25);width:100%;max-width:640px;padding:18px 20px;font-size:13px;color:#101828}'
+ '.bdc-dlg.w{max-width:980px}.bdc-dlg h2{margin:0 0 4px;font-size:17px}.bdc-dlg .lead{color:#667085;margin:0 0 12px;font-size:12.5px}'
+ '.bdc-f{display:flex;flex-direction:column;gap:4px;margin-bottom:10px;font-size:12px;font-weight:600;color:#344054}'
+ '.bdc-f input,.bdc-f select,.bdc-f textarea{font:inherit;font-weight:400;font-size:13px;border:.5px solid #D0D5DD;border-radius:8px;padding:7px 9px;outline:none;color:#101828;background:#fff}'
+ '.bdc-f input:focus,.bdc-f textarea:focus,.bdc-f select:focus{border-color:#3E63DD}'
+ '.bdc-btns{display:flex;justify-content:flex-end;gap:8px;margin-top:8px;flex-wrap:wrap}'
+ '.bdc-chips{display:flex;flex-wrap:wrap;gap:5px}.bdc-chip{border:.5px solid #D0D5DD;background:#fff;border-radius:999px;padding:3px 10px;font:inherit;font-size:12px;cursor:pointer}'
+ '.bdc-chip:hover,.bdc-chip.on{border-color:#3E63DD;background:#EEF0FF;color:#3538CD}'
+ '.bdc-vars{display:flex;gap:6px;margin-bottom:8px}.bdc-var{flex:1;border:.5px solid #D0D5DD;border-radius:8px;padding:7px;cursor:pointer;background:#fff;font:inherit;font-size:12px;text-align:left}'
+ '.bdc-var.on{border-color:#3E63DD;background:#F5F8FF;box-shadow:0 0 0 2px rgba(62,99,221,.15)}.bdc-var b{display:block;font-size:12.5px}'
+ '.bdc-facts{font-size:11.5px;color:#475467;background:#F9FAFB;border-radius:8px;padding:6px 9px;margin-bottom:8px}'
+ '.bdc-rv{display:flex;gap:12px;min-height:420px}.bdc-rv .l{width:270px;flex-shrink:0;border:.5px solid #EAECF0;border-radius:8px;overflow:auto;max-height:62vh}'
+ '.bdc-rv .l .it{padding:8px 10px;border-bottom:.5px solid #F2F4F7;cursor:pointer;font-size:12.5px}.bdc-rv .l .it.on{background:#EEF4FF}.bdc-rv .l .it .s{color:#667085;font-size:11.5px}'
+ '.bdc-rv .r{flex:1;min-width:0}'
+ '.bdc-imp{max-height:300px;overflow:auto;border:.5px solid #EAECF0;border-radius:8px}.bdc-imp table{border-collapse:collapse;width:100%;font-size:12px}'
+ '.bdc-imp td,.bdc-imp th{border-bottom:.5px solid #F2F4F7;padding:5px 7px;text-align:left;white-space:nowrap}'
+ '.bdc-toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:#101828;color:#fff;padding:9px 16px;border-radius:9px;z-index:9999;font-size:13px;box-shadow:0 8px 24px rgba(16,24,40,.25)}'
+ '.bdc-toast.bad{background:#B42318}'
+ '.bdc-pop{position:fixed;z-index:9500;background:#fff;border:.5px solid #D0D5DD;border-radius:10px;box-shadow:0 12px 30px rgba(16,24,40,.18);padding:6px;min-width:200px;max-height:320px;overflow:auto}'
+ '.bdc-pop button{display:flex;width:100%;gap:8px;align-items:center;border:none;background:none;font:inherit;font-size:12.5px;padding:6px 8px;border-radius:6px;cursor:pointer;text-align:left}'
+ '.bdc-pop button:hover{background:#F2F4F7}.bdc-pop .s{color:#98A2B3;font-size:11px;margin-left:auto}'
+ '.bdc-keys{font-size:11px;color:#98A2B3;padding:6px 14px;border-top:.5px solid #EAECF0;flex-shrink:0}.bdc-keys kbd{font-family:inherit;background:#F2F4F7;border-radius:4px;padding:0 4px;color:#475467}'
+ '.bdc-rec{color:#B42318;font-weight:700;animation:bdcp 1s infinite}@keyframes bdcp{50%{opacity:.4}}'
+ '@media (max-width:900px){.bdc-panel{position:fixed;inset:0;width:auto;z-index:8000}.bdc-search{width:120px}.bdc-keys{display:none}}';

/* ---------- open / close ---------- */
function open(root, ws){
  S.root = root; S.ws = +ws || 0; S.sel = null; S.checked = {}; S.detail = null;
  if (!document.getElementById('bdc-style')){ var st = document.createElement('style'); st.id = 'bdc-style'; st.textContent = CSS; document.head.appendChild(st); }
  root.innerHTML = '<div class="bdc-top" id="bdc-top"></div><div class="bdc-body"><div class="bdc-sheet" id="bdc-sheet"><div class="bdc-empty">Loading…</div></div><div class="bdc-panel" id="bdc-panel" hidden></div></div>'
    + '<div class="bdc-keys"><kbd>↑</kbd><kbd>↓</kbd> move · <kbd>Enter</kbd> open · <kbd>C</kbd> call · <kbd>E</kbd> e-mail · <kbd>N</kbd> notes · <kbd>1</kbd>–<kbd>7</kbd> outcome · <kbd>Space</kbd> select · <kbd>/</kbd> search · <kbd>Esc</kbd> close</div>';
  if (!S.wired){ S.wired = true; wire(); }
  if (!S.onKey){ S.onKey = onKey; document.addEventListener('keydown', S.onKey); }
  api('GET', '/meta').then(function(m){ if (m.ok){ S.meta = m; S.settings = m.settings; S.today = m.today; } renderTop(); load(); });
  clearInterval(S.timer); S.timer = setInterval(function(){ if (document.hidden || !S.root || !S.root.isConnected) return; load(true); }, 60000);
}
function close(){
  if (S.onKey){ document.removeEventListener('keydown', S.onKey); S.onKey = null; }
  clearInterval(S.timer); stopRec(); closePop();
  var ov = document.querySelector('.bdc-ov'); if (ov) ov.remove();
}

/* ---------- data ---------- */
function load(quiet){
  if (S.loading) return; S.loading = true;
  return api('GET', '/list?view=' + S.view + '&q=' + encodeURIComponent(S.q) + (S.mine ? '&mine=1' : '')).then(function(r){
    S.loading = false;
    if (!r.ok){ if (!quiet) $('#bdc-sheet').innerHTML = '<div class="bdc-empty"><h3>Could not load</h3>' + esc(r.error) + '</div>'; return; }
    S.rows = r.rows; S.counts = r.counts; S.today = r.today; S.settings = r.settings || S.settings; S.total = r.total;
    if (S.sel && !rowById(S.sel) && !quiet) S.sel = null;
    renderTop(); renderSheet();
  });
}

/* ---------- top bar ---------- */
function goal(label, n, target){
  var pct = target ? Math.min(100, Math.round(n * 100 / target)) : 0;
  return '<div class="bdc-goal" data-act="settings" title="Daily target - click to change"><span>' + label + '</span><span class="bar"><i style="width:' + pct + '%"></i></span><b>' + n + '/' + (target || 0) + '</b>' + (target && n >= target ? ' 🎉' : '') + '</div>';
}
function renderTop(){
  var c = S.counts || {}, s = S.settings || {};
  var views = [['today','📅 Today'],['callbacks','🔁 Callbacks'],['hot','🔥 Hot'],['new','🆕 Not called'],['all','All']];
  var h = goal('📞 Calls', S.today.calls || 0, s.calls_target) + goal('✉️ E-mails', S.today.emails || 0, s.emails_target)
    + '<div class="bdc-views">' + views.map(function(v){ return '<button class="bdc-v' + (S.view === v[0] ? ' on' : '') + '" data-view="' + v[0] + '">' + v[1] + '<span class="n">' + (c[v[0]] || 0) + '</span></button>'; }).join('')
    + '<button class="bdc-v' + (STATUS_VIEW() ? ' on' : '') + '" data-act="statusmenu">' + (STATUS_VIEW() ? esc(statusLabel(S.view)) + '<span class="n">' + (c[S.view] || 0) + '</span>' : 'Status ▾') + '</button></div>'
    + '<input class="bdc-search" id="bdc-q" placeholder="Search company, person, phone…" value="' + esc(S.q) + '">'
    + '<button class="bdc-v' + (S.mine ? ' on' : '') + '" data-act="mine" title="Only prospects assigned to you (or nobody)">👤 Mine</button>'
    + '<span style="flex:1"></span>'
    + '<button class="bdc-btn" data-act="add" title="Add a prospect row">+ Prospect</button>'
    + '<button class="bdc-btn" data-act="import" title="Paste rows from Excel / CSV">⬆ Import</button>'
    + '<button class="bdc-btn" data-act="bulk" ' + (nChecked() ? '' : 'disabled') + ' title="One personalised AI e-mail for each selected prospect">✉ Bulk e-mail' + (nChecked() ? ' (' + nChecked() + ')' : '') + '</button>'
    + '<button class="bdc-btn" data-act="review" title="Review AI drafts and send">📝 Review drafts</button>'
    + '<button class="bdc-ic" data-act="settings" title="Targets, sending gap, follow-ups">⚙️</button>';
  $('#bdc-top').innerHTML = h;
}
function STATUS_VIEW(){ return ['today','callbacks','hot','new','all'].indexOf(S.view) < 0; }
function nChecked(){ return Object.keys(S.checked).filter(function(k){ return S.checked[k]; }).length; }

/* ---------- the sheet ---------- */
var COLS = [['company','Company',0],['contact_name','Contact',150],['designation','Designation',140],['phone','Phone',150],['email','E-mail',210],
            ['status','Status',120],['last','Last outcome',200],['next','Next step',170],['mail','E-mail status',120],['u','Notes',70]];
function renderSheet(){
  var el = $('#bdc-sheet');
  if (!S.rows.length){
    var empty = S.view === 'today' ? '<h3>Today\'s list is clear 🎉</h3>No callbacks due, no new replies or opens, and every prospect has been called. Check <b>All</b> or import more prospects.'
      : S.q ? '<h3>Nothing matches</h3>Try another search.' : '<h3>No prospects here yet</h3>Add a prospect, or paste your calling list from Excel with <b>⬆ Import</b>.';
    el.innerHTML = '<div class="bdc-empty">' + empty + '<div style="margin-top:14px" class="bdc-row" ><span style="flex:1"></span><button class="bdc-btn p" data-act="import">⬆ Import from Excel</button><button class="bdc-btn" data-act="add">+ Prospect</button><span style="flex:1"></span></div></div>';
    return;
  }
  var all = S.rows.every(function(r){ return S.checked[r.id]; });
  var h = '<table class="bdc-t"><thead><tr><th class="s1"><input type="checkbox" data-act="checkall" ' + (all ? 'checked' : '') + ' aria-label="Select all"> Company</th>'
    + COLS.slice(1).map(function(c){ return '<th>' + c[1] + '</th>'; }).join('') + '</tr></thead><tbody>';
  h += S.rows.map(rowHtml).join('');
  h += '</tbody></table>';
  if (S.total > S.rows.length) h += '<div class="bdc-empty" style="padding:14px">Showing the first ' + S.rows.length + ' of ' + S.total + '. Search to find the rest.</div>';
  var top = el.scrollTop, left = el.scrollLeft;
  el.innerHTML = h; el.scrollTop = top; el.scrollLeft = left;
}
function inp(r, k, v, ph, w){ return '<input class="bdc-in" data-f="' + k + '" data-id="' + r.id + '" value="' + esc(v || '') + '" placeholder="' + esc(ph) + '" style="width:' + w + 'px" autocomplete="off">'; }
function rowHtml(r){
  var c = r.contact || {}, e = r.email || {};
  var why = r.why ? '<span class="bdc-why b' + r.bucket + '">' + esc(r.why) + '</span>' : '';
  var col = ST_COLORS[r.status] || '#667085';
  var mail = '';
  if (r.replied_at) mail = '<span class="bdc-mail rp">↩ Replied</span>';
  else if (r.opens) mail = '<span class="bdc-mail op">👁 Opened ' + r.opens + '×</span>';
  else if (e.sent) mail = '<span class="bdc-mail">✓ Sent' + (e.sent > 1 ? ' (' + e.sent + ')' : '') + '</span>';
  else if (e.queued) mail = '<span class="bdc-mail">⏳ Queued</span>';
  else if (e.drafts) mail = '<span class="bdc-mail">📝 Draft</span>';
  if (r.seq_active && !r.replied_at) mail += ' <span title="Automatic follow-up ' + esc(when(r.seq_next_at)) + '">🔄</span>';
  var dots = ''; for (var i = 0; i < 8; i++) dots += '<i class="' + (i < r.u_score ? 'on' : '') + '"></i>';
  var phone = c.phone ? '<a class="bdc-ic" href="tel:' + esc(String(c.phone).replace(/[^\d+]/g, '')) + '" data-act="tel" data-id="' + r.id + '" title="Call (C)">📞</a>' : '';
  return '<tr data-row="' + r.id + '" class="' + (S.sel === r.id ? 'sel' : '') + '">'
    + '<td class="s1"><div class="bdc-co"><input type="checkbox" data-act="check" data-id="' + r.id + '" ' + (S.checked[r.id] ? 'checked' : '') + ' aria-label="Select ' + esc(r.name) + '">'
    + inp(r, 'name', r.name, 'Company', 150) + why + '</div></td>'
    + '<td>' + inp(r, 'contact_name', c.name, 'Name', 125) + '</td>'
    + '<td>' + inp(r, 'designation', c.designation, 'HR / Plant head', 115) + '</td>'
    + '<td><div class="bdc-row" style="flex-wrap:nowrap;gap:0">' + inp(r, 'phone', c.phone, 'Phone', 108) + phone + '</div></td>'
    + '<td><div class="bdc-row" style="flex-wrap:nowrap;gap:0">' + inp(r, 'email', c.email, 'name@company.com', 165) + (!c.email && c.name ? '<button class="bdc-ic" data-act="guess" data-id="' + r.id + '" title="Guess the e-mail">✨</button>' : '') + '</div></td>'
    + '<td><button class="bdc-pill" data-act="statuspick" data-id="' + r.id + '" style="background:' + col + '1A;color:' + col + '">' + esc(statusLabel(r.status)) + (r.attempts > 1 && r.status === 'no_answer' ? ' ×' + r.attempts : '') + '</button></td>'
    + '<td style="max-width:190px;overflow:hidden;text-overflow:ellipsis;color:#475467" title="' + esc(r.last_outcome) + '">' + (r.last_called_at ? '<span style="color:#98A2B3">' + esc(when(r.last_called_at)) + '</span> ' : '') + esc(r.last_outcome || '—') + '</td>'
    + '<td><button class="bdc-pill" data-act="nextpick" data-id="' + r.id + '" style="background:' + (r.next_at && r.bucket === 1 ? '#FEF3F2;color:#B42318' : '#F2F4F7;color:#344054') + '" title="' + esc(r.next_note) + '">' + (r.next_at ? '⏰ ' + esc(when(r.next_at)) : '+ Set') + '</button></td>'
    + '<td>' + (mail || '<span class="bdc-mail" style="color:#C0C5CE">—</span>') + '</td>'
    + '<td><span class="bdc-dots" title="Business understanding: ' + r.u_score + ' of 8 filled">' + dots + '</span></td></tr>';
}
function replaceRow(row){
  if (!row) return;
  for (var i = 0; i < S.rows.length; i++) if (S.rows[i].id === row.id){ S.rows[i] = row; break; }
  var tr = $('tr[data-row="' + row.id + '"]');
  if (tr){ var tmp = document.createElement('tbody'); tmp.innerHTML = rowHtml(row); var nt = tmp.firstChild; tr.parentNode.replaceChild(nt, tr); }
}
function select(id, openPanel){
  S.sel = id;
  $$('tr[data-row]').forEach(function(tr){ tr.classList.toggle('sel', +tr.dataset.row === id); });
  var tr = $('tr[data-row="' + id + '"]'); if (tr) tr.scrollIntoView({block:'nearest'});
  if (openPanel || !$('#bdc-panel').hidden) loadDetail(id);
}
function move(d){
  if (!S.rows.length) return;
  var i = -1; for (var k = 0; k < S.rows.length; k++) if (S.rows[k].id === S.sel) i = k;
  i = Math.max(0, Math.min(S.rows.length - 1, i + d));
  select(S.rows[i].id);
}

/* ---------- cell editing ---------- */
function saveCell(el){
  var id = +el.dataset.id, f = el.dataset.f, r = rowById(id); if (!r) return;
  var cur = f === 'name' ? r.name : (r.contact || {})[f === 'contact_name' ? 'name' : f];
  var v = el.value.trim();
  if ((cur || '') === v) return;
  var body = {}; body[f] = v;
  el.disabled = true;
  api('PATCH', '/companies/' + id, body).then(function(j){
    el.disabled = false;
    if (!j.ok){ toast(j.error, true); el.value = cur || ''; return; }
    replaceRow(j.row);
    if (S.detail && S.detail.company.id === id) loadDetail(id);
  });
}

/* ---------- side panel ---------- */
function loadDetail(id){
  var p = $('#bdc-panel'); p.hidden = false;
  if (!S.detail || S.detail.company.id !== id){ p.innerHTML = '<div class="bdc-empty">Loading…</div>'; p.scrollTop = 0; }
  return api('GET', '/companies/' + id).then(function(d){
    if (S.sel !== id) return;
    if (!d.ok){ p.innerHTML = '<div class="bdc-empty">' + esc(d.error) + '</div>'; return; }
    S.detail = d; renderPanel();
  });
}
function renderPanel(){
  var d = S.detail, co = d.company, call = d.call, r = rowById(co.id) || {};
  var cs = d.contacts, c = cs.filter(function(x){ return x.id === call.contact_id; })[0] || cs[0] || {};
  var p = $('#bdc-panel');
  var keep = {}; $$('textarea[data-u]', p).forEach(function(t){ if (document.activeElement === t) keep.focus = t.dataset.u; });
  var noteVal = $('#bdc-note', p) ? $('#bdc-note', p).value : '';
  var site = co.website ? (/^https?:/i.test(co.website) ? co.website : 'https://' + co.website) : '';
  var h = '<div class="bdc-ph"><h3>' + esc(co.name) + '<span style="flex:1"></span><button class="bdc-ic" data-act="closepanel" title="Close (Esc)" aria-label="Close">✕</button></h3>'
    + '<div class="sub">' + [co.industry, co.city, site ? '<a href="' + esc(site) + '" target="_blank" rel="noopener">' + esc(co.website) + '</a>' : '<a href="#" data-act="setsite">+ add website</a>'].filter(Boolean).join(' · ') + '</div>'
    + '<div class="bdc-row" style="margin-top:8px">'
    + (cs.length ? '<select class="bdc-btn sm" data-act="contactpick" style="max-width:220px">' + cs.map(function(x){ return '<option value="' + x.id + '"' + (x.id === c.id ? ' selected' : '') + '>' + esc(x.name + (x.designation ? ' · ' + x.designation : '')) + '</option>'; }).join('') + '</select>' : '<span style="color:#98A2B3;font-size:12px">No contact yet</span>')
    + '<button class="bdc-btn sm" data-act="addcontact">+ Person</button>'
    + (c.phone ? '<a class="bdc-btn sm g" href="tel:' + esc(String(c.phone).replace(/[^\d+]/g, '')) + '" data-act="tel" data-id="' + co.id + '">📞 ' + esc(c.phone) + '</a><button class="bdc-btn sm" data-act="pushcall" title="Ring it on your phone (HireLab Dialer app)">📱 On phone</button>' : '')
    + '<button class="bdc-btn sm p" data-act="compose">✉ E-mail</button></div>'
    + (c.email ? '<div class="sub" style="margin-top:4px">✉ ' + esc(c.email) + (c.linkedin ? ' · <a href="' + esc(c.linkedin) + '" target="_blank" rel="noopener">LinkedIn</a>' : '') + '</div>' : '')
    + '</div>';

  /* outcome */
  h += '<div class="bdc-sec"><h4>Log this call<span class="r">' + (call.last_called_at ? '<span style="font-weight:400;color:#98A2B3">last: ' + esc(when(call.last_called_at)) + '</span>' : '') + '</span></h4>'
    + '<textarea class="bdc-ta" id="bdc-note" rows="2" placeholder="What happened on the call? (optional)">' + esc(noteVal) + '</textarea>'
    + '<div class="bdc-outs" style="margin-top:6px">' + OUTCOMES.map(function(o){ return '<button class="bdc-out" data-out="' + o[0] + '" title="' + o[2] + ' (' + o[3] + ')">' + o[1] + '<span>' + o[2] + '</span><span class="k">' + o[3] + '</span></button>'; }).join('')
    + '<button class="bdc-out" data-act="nextpick" data-id="' + co.id + '">⏰<span>' + (call.next_at ? esc(when(call.next_at)) : 'Set next') + '</span><span class="k">next step</span></button></div>'
    + (call.next_note ? '<div class="bdc-kv" style="margin-top:6px">Next: <b>' + esc(call.next_note) + '</b>' + (call.next_at ? ' · ' + esc(when(call.next_at)) : '') + '</div>' : '')
    + '</div>';

  /* script */
  var op = d.opener || {};
  h += '<div class="bdc-sec"><h4>🎙 Call opener<span class="r">' + (S.meta && S.meta.ai ? '<button class="bdc-btn sm ai" data-act="opener">' + (op.opener ? '↻ Rewrite' : '✨ Write opener') + '</button>' : '') + '</span></h4>'
    + (op.opener ? '<div class="bdc-script">' + esc(op.opener) + '</div>'
      + (op.questions && op.questions.length ? '<div class="bdc-kv" style="margin-top:6px"><b>Ask:</b> ' + op.questions.map(esc).join(' · ') + '</div>' : '')
      + (op.objections || []).map(function(o){ return '<div class="bdc-obj"><b>“' + esc(o.q) + '”</b>' + esc(o.a) + '</div>'; }).join('')
      : '<div class="bdc-kv">A 30-second opener and answers to “we don’t use agencies”, “fee is high”, “no hiring” — written from the notes below.</div>')
    + '</div>';

  /* understanding */
  var u = d.understanding, fields = (S.meta && S.meta.u_fields) || [];
  h += '<div class="bdc-sec"><h4>🧠 Business understanding <span style="color:#7A5AF8">' + d.u_score + '/8</span><span class="r">'
    + (S.meta && S.meta.ai ? '<button class="bdc-btn sm ai" data-act="research" title="AI reads their website and your Learning notes, and fills the empty fields">✨ Research</button>' : '')
    + '<button class="bdc-btn sm" data-act="voice" title="Speak what you learned; AI puts it in the right fields">🎤 Speak</button></span></h4>'
    + '<div id="bdc-voice" hidden style="margin-bottom:8px"><textarea class="bdc-ta" id="bdc-vtext" rows="3" placeholder="Bolo ya likho — e.g. “Priya HR head hai, 3 sales engineers chahiye Pune plant ke liye, abhi Naukri use karte hain…”"></textarea>'
    + '<div class="bdc-row" style="margin-top:5px"><span id="bdc-vstate" style="font-size:12px;color:#667085"></span><span style="flex:1"></span><button class="bdc-btn sm" data-act="voicestop">Stop</button><button class="bdc-btn sm ai" data-act="voicefill">✨ Fill the fields</button></div></div>'
    + fields.map(function(f){ var v = u[f[0]] || ''; return '<div class="bdc-uf' + (v ? ' filled' : '') + '"><label>' + esc(f[1]) + '</label><textarea class="bdc-ta" data-u="' + f[0] + '" rows="1">' + esc(v) + '</textarea></div>'; }).join('')
    + '</div>';

  /* e-mails */
  h += '<div class="bdc-sec"><h4>✉ E-mails<span class="r">'
    + (call.seq_active ? '<button class="bdc-btn sm" data-act="seqstop" title="Next automatic follow-up ' + esc(when(call.seq_next_at)) + '">🔄 Follow-up ' + esc(when(call.seq_next_at)) + ' · Stop</button>' : '')
    + '<button class="bdc-btn sm p" data-act="compose">✉ Write</button></span></h4>'
    + (d.emails.length ? d.emails.map(function(e){
        var tag = e.replied_at ? '<span class="bdc-tag rp">Replied</span>' : e.opens ? '<span class="bdc-tag op">Opened ' + e.opens + '×</span>'
          : e.status === 'sent' ? '<span class="bdc-tag sent">Sent</span>' : e.status === 'failed' ? '<span class="bdc-tag fail" title="' + esc(e.error) + '">Failed</span>'
          : '<span class="bdc-tag">' + esc(e.status) + '</span>';
        return '<div class="bdc-em"><div class="t">' + (e.step ? '<span>↪ Follow-up ' + e.step + '</span>' : '') + '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(e.subject || '(drafting…)') + '</span>' + tag + '</div>'
          + '<div style="color:#667085;margin-top:2px">' + esc(e.sent_at ? when(e.sent_at) : e.scheduled_at ? 'Scheduled ' + when(e.scheduled_at) : '') + (e.user_name ? ' · ' + esc(e.user_name) : '') + ' · to ' + esc(e.to_addr) + '</div></div>';
      }).join('') : '<div class="bdc-kv">No e-mail yet. <b>✉ Write</b> gives you 3 AI versions from the notes above.</div>')
    + '</div>';

  /* history */
  h += '<div class="bdc-sec"><h4>🕑 History</h4>' + (d.history.length ? d.history.map(function(x){
    var ic = x.activity_type === 'call' ? '📞' : x.activity_type === 'meeting' ? '🤝' : x.activity_type === 'followup' ? '⏰' : '📝';
    return '<div class="bdc-hist">' + ic + ' <b>' + esc(x.subject) + '</b>' + (x.due_at ? ' · ' + esc(when(x.due_at)) : '') + '<br>' + (x.body ? esc(x.body.slice(0, 220)) + '<br>' : '') + '<span style="color:#98A2B3">' + esc(when(x.created_at)) + (x.by ? ' · ' + esc(x.by) : '') + '</span></div>';
  }).join('') : '<div class="bdc-kv">No calls logged yet.</div>') + '</div>';

  p.innerHTML = h;
  $$('textarea[data-u]', p).forEach(autosize);
  if (keep.focus){ var t = $('textarea[data-u="' + keep.focus + '"]', p); if (t) t.focus(); }
}
function autosize(t){ t.style.height = 'auto'; t.style.height = Math.min(160, Math.max(32, t.scrollHeight + 2)) + 'px'; }
function saveU(t){
  var id = S.detail && S.detail.company.id; if (!id) return;
  var k = t.dataset.u, v = t.value.trim();
  if ((S.detail.understanding[k] || '') === v) return;
  var b = {}; b[k] = v;
  api('PUT', '/companies/' + id + '/understanding', b).then(function(j){
    if (!j.ok){ toast(j.error, true); return; }
    S.detail.understanding = j.understanding; S.detail.u_score = j.u_score;
    t.parentNode.classList.toggle('filled', !!v);
    var r = rowById(id); if (r){ r.u_score = j.u_score; replaceRow(r); }
    var h4 = $('.bdc-sec h4 span[style*="7A5AF8"]', $('#bdc-panel')); if (h4) h4.textContent = j.u_score + '/8';
  });
}

/* ---------- outcomes ---------- */
function quickTimes(){
  var n = new Date(), out = [];
  var a = new Date(n.getTime() + 2 * 3600e3); out.push(['In 2 hours', a]);
  var t = new Date(n); t.setHours(16, 0, 0, 0); if (t > n) out.push(['Today 4 PM', t]);
  var tm = new Date(n); tm.setDate(n.getDate() + 1); if (tm.getDay() === 0) tm.setDate(tm.getDate() + 1); tm.setHours(11, 0, 0, 0); out.push(['Tomorrow 11 AM', tm]);
  var tm2 = new Date(tm); tm2.setHours(16, 0, 0, 0); out.push(['Tomorrow 4 PM', tm2]);
  var mon = new Date(n); mon.setDate(n.getDate() + ((8 - n.getDay()) % 7 || 7)); mon.setHours(11, 0, 0, 0); out.push(['Monday 11 AM', mon]);
  var wk = new Date(n); wk.setDate(n.getDate() + 7); wk.setHours(11, 0, 0, 0); out.push(['In a week', wk]);
  return out;
}
function dialog(html, onMount, wide){
  var ov = document.createElement('div'); ov.className = 'bdc-ov';
  ov.innerHTML = '<div class="bdc-dlg' + (wide ? ' w' : '') + '" role="dialog" aria-modal="true">' + html + '</div>';
  document.body.appendChild(ov);
  ov.addEventListener('mousedown', function(e){ if (e.target === ov) ov.remove(); });
  ov.addEventListener('keydown', function(e){ if (e.key === 'Escape'){ e.stopPropagation(); ov.remove(); } });
  if (onMount) onMount(ov);
  var f = ov.querySelector('[autofocus]') || ov.querySelector('input,textarea,select,button'); if (f) setTimeout(function(){ f.focus(); }, 30);
  return ov;
}
function doOutcome(id, st){
  var r = rowById(id) || {}, note = ($('#bdc-note') && S.detail && S.detail.company.id === id) ? $('#bdc-note').value.trim() : '';
  if (st === 'callback' || st === 'meeting' || st === 'interested'){
    var title = st === 'meeting' ? '🤝 Meeting fixed — when?' : st === 'callback' ? '🔁 Call back when?' : '👍 Interested — next step?';
    var dflt = quickTimes()[st === 'callback' ? 0 : 2][1];
    dialog('<h2>' + title + '</h2><p class="lead">' + esc(r.name || '') + '</p>'
      + '<div class="bdc-chips" style="margin-bottom:8px">' + quickTimes().map(function(q, i){ return '<button class="bdc-chip" data-qt="' + i + '">' + q[0] + '</button>'; }).join('') + '</div>'
      + '<div class="bdc-f">Date and time<input type="datetime-local" id="bdc-dt" value="' + localIso(dflt) + '"' + (st === 'interested' ? ' placeholder="optional"' : '') + '></div>'
      + '<div class="bdc-f">' + (st === 'meeting' ? 'Meeting note (place / link)' : 'Next step') + '<input id="bdc-nn" placeholder="' + (st === 'interested' ? 'e.g. Send 3 sales engineer profiles' : '') + '"></div>'
      + '<div class="bdc-f">Call note<textarea id="bdc-cn" rows="2">' + esc(note) + '</textarea></div>'
      + '<div class="bdc-btns">' + (st === 'interested' ? '<button class="bdc-btn" data-x="nodate">Save without a date</button>' : '') + '<button class="bdc-btn" data-x="cancel">Cancel</button><button class="bdc-btn p" data-x="ok">Save</button></div>',
      function(ov){
        ov.addEventListener('click', function(e){
          var q = e.target.closest('[data-qt]'); if (q){ ov.querySelector('#bdc-dt').value = localIso(quickTimes()[+q.dataset.qt][1]); return; }
          var x = e.target.closest('[data-x]'); if (!x) return;
          if (x.dataset.x === 'cancel'){ ov.remove(); return; }
          var body = {status: st, note: ov.querySelector('#bdc-cn').value, next_note: ov.querySelector('#bdc-nn').value,
                      next_at: x.dataset.x === 'nodate' ? '' : inputToIst(ov.querySelector('#bdc-dt').value)};
          ov.remove(); postOutcome(id, body);
        });
        ov.addEventListener('keydown', function(e){ if (e.key === 'Enter' && e.target.tagName !== 'TEXTAREA'){ e.preventDefault(); ov.querySelector('[data-x=ok]').click(); } });
      });
    return;
  }
  if (st === 'not_interested'){
    var rs = (S.meta && S.meta.reasons) || [];
    dialog('<h2>❌ Not interested — why?</h2><p class="lead">' + esc(r.name || '') + ' comes back on your list later, so no prospect is lost.</p>'
      + '<div class="bdc-chips" style="margin-bottom:10px">' + rs.map(function(x, i){ return '<button class="bdc-chip' + (i === 0 ? ' on' : '') + '" data-rs="' + x[0] + '">' + esc(x[1]) + '</button>'; }).join('') + '</div>'
      + '<div class="bdc-f">Try again in<select id="bdc-rd"><option value="30">1 month</option><option value="60">2 months</option><option value="90" selected>3 months</option><option value="180">6 months</option><option value="365">1 year</option></select></div>'
      + '<div class="bdc-f">Call note<textarea id="bdc-cn" rows="2">' + esc(note) + '</textarea></div>'
      + '<div class="bdc-btns"><button class="bdc-btn" data-x="cancel">Cancel</button><button class="bdc-btn p" data-x="ok">Save</button></div>',
      function(ov){
        var reason = rs.length ? rs[0][0] : 'other';
        ov.addEventListener('click', function(e){
          var c = e.target.closest('[data-rs]'); if (c){ reason = c.dataset.rs; $$('[data-rs]', ov).forEach(function(b){ b.classList.toggle('on', b === c); }); return; }
          var x = e.target.closest('[data-x]'); if (!x) return;
          if (x.dataset.x === 'cancel'){ ov.remove(); return; }
          var body = {status: st, reason: reason, retry_days: ov.querySelector('#bdc-rd').value, note: ov.querySelector('#bdc-cn').value};
          ov.remove(); postOutcome(id, body);
        });
      });
    return;
  }
  postOutcome(id, {status: st, note: note});
}
function postOutcome(id, body){
  api('POST', '/companies/' + id + '/outcome', body).then(function(j){
    if (!j.ok){ toast(j.error, true); return; }
    S.today = j.today || S.today; renderTop();
    replaceRow(j.row);
    if ($('#bdc-note')) $('#bdc-note').value = '';
    toast((OUTCOMES.filter(function(o){ return o[0] === body.status; })[0] || ['', '✓'])[1] + ' ' + statusLabel(body.status) + (j.row && j.row.next_at ? ' · next ' + when(j.row.next_at) : ''));
    if (S.detail && S.detail.company.id === id) loadDetail(id);
    if (j.compose) compose(id, 'details');
    else if (S.view === 'today' && body.status !== 'interested') setTimeout(function(){ var i = S.rows.findIndex(function(r){ return r.id === id; }); if (i >= 0 && i < S.rows.length - 1) select(S.rows[i + 1].id, !$('#bdc-panel').hidden); }, 150);
  });
}
function nextPick(anchor, id){
  var r = rowById(id) || {};
  popAt(anchor, quickTimes().map(function(q, i){ return '<button data-nq="' + i + '">⏰ ' + q[0] + '<span class="s">' + esc(when(istIso(q[1]))) + '</span></button>'; }).join('')
    + '<button data-nq="pick">📅 Pick a date…</button>' + (r.next_at ? '<button data-nq="clear" style="color:#B42318">✕ Clear next step</button>' : ''), function(pop){
    pop.addEventListener('click', function(e){
      var b = e.target.closest('[data-nq]'); if (!b) return; closePop();
      if (b.dataset.nq === 'pick'){
        dialog('<h2>⏰ Next step</h2><div class="bdc-f">When<input type="datetime-local" id="bdc-dt" value="' + localIso(quickTimes()[2][1]) + '"></div><div class="bdc-f">What<input id="bdc-nn" value="' + esc(r.next_note || '') + '" placeholder="e.g. Call Priya about the Pune plant"></div><div class="bdc-btns"><button class="bdc-btn" data-x="cancel">Cancel</button><button class="bdc-btn p" data-x="ok">Save</button></div>', function(ov){
          ov.addEventListener('click', function(e2){ var x = e2.target.closest('[data-x]'); if (!x) return; if (x.dataset.x === 'ok') setNext(id, inputToIst(ov.querySelector('#bdc-dt').value), ov.querySelector('#bdc-nn').value); ov.remove(); });
        });
        return;
      }
      setNext(id, b.dataset.nq === 'clear' ? '' : istIso(quickTimes()[+b.dataset.nq][1]), r.next_note || 'Call back');
    });
  });
}
function setNext(id, at, note){
  api('POST', '/companies/' + id + '/next', {next_at: at, next_note: note}).then(function(j){
    if (!j.ok){ toast(j.error, true); return; }
    replaceRow(j.row); if (S.detail && S.detail.company.id === id) loadDetail(id);
  });
}

/* ---------- popovers ---------- */
var pop = null;
function closePop(){ if (pop){ pop.remove(); pop = null; } }
function popAt(anchor, html, mount){
  closePop(); pop = document.createElement('div'); pop.className = 'bdc-pop'; pop.innerHTML = html; document.body.appendChild(pop);
  var r = anchor.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
  pop.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left)) + 'px';
  pop.style.top = (r.bottom + h + 8 > window.innerHeight ? Math.max(8, r.top - h - 4) : r.bottom + 4) + 'px';
  if (mount) mount(pop);
}
document.addEventListener('mousedown', function(e){ if (pop && !pop.contains(e.target)) closePop(); }, true);

/* ---------- calling ---------- */
function callRow(id){
  var r = rowById(id); if (!r || !r.contact || !r.contact.phone){ toast('Add a phone number first.', true); return; }
  location.href = 'tel:' + String(r.contact.phone).replace(/[^\d+]/g, '');
  select(id, true);
  setTimeout(function(){ var n = $('#bdc-note'); if (n) n.focus(); }, 400);
}
function pushCall(){
  var d = S.detail; if (!d) return;
  var c = d.contacts.filter(function(x){ return x.id === d.call.contact_id; })[0] || d.contacts[0];
  if (!c || !c.phone) return;
  fetch('/api/push-call', {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body: JSON.stringify({phone: c.phone, name: c.name + ' · ' + d.company.name})})
    .then(function(r){ return r.json(); }).then(function(j){ toast(j.message || j.error || 'Sent', !!j.error); }).catch(function(){ toast('Could not reach your phone', true); });
}

/* ---------- AI: research / voice / opener ---------- */
function research(){
  var d = S.detail; if (!d) return;
  if (!d.company.website){
    var w = prompt('Company website (e.g. acme-switchgear.com):', ''); if (!w) return;
    return runResearch(d.company.id, w.trim());
  }
  runResearch(d.company.id, '');
}
function runResearch(id, site){
  var b = $('[data-act=research]'); if (b){ b.disabled = true; b.textContent = '✨ Reading the website…'; }
  api('POST', '/companies/' + id + '/research', site ? {website: site} : {}).then(function(j){
    if (b) b.disabled = false;
    if (!j.ok){ toast(j.error, true); if (b) b.textContent = '✨ Research'; return; }
    toast(j.filled.length ? '✨ Filled ' + j.filled.length + ' field' + (j.filled.length === 1 ? '' : 's') + (j.read_site ? ' from the website' : '') + (j.used_learning ? ' + your Learning notes' : '') : 'Nothing new found - your fields stay as they are');
    loadDetail(id); load(true);
  });
}
function startRec(){
  var box = $('#bdc-voice'); if (!box) return; box.hidden = false;
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition, stt = $('#bdc-vstate'), ta = $('#bdc-vtext');
  if (!SR){ stt.textContent = 'Voice typing works in Chrome / Edge. Type here instead.'; ta.focus(); return; }
  stopRec();
  var rec = new SR(); rec.lang = 'en-IN'; rec.continuous = true; rec.interimResults = true;
  var base = ta.value ? ta.value.trim() + ' ' : '';
  rec.onresult = function(e){ var fin = '', tmp = ''; for (var i = 0; i < e.results.length; i++){ if (e.results[i].isFinal) fin += e.results[i][0].transcript + ' '; else tmp += e.results[i][0].transcript; } ta.value = base + fin + tmp; };
  rec.onerror = function(e){ stt.textContent = e.error === 'not-allowed' ? 'Microphone blocked - allow it in the browser, or type.' : 'Voice stopped (' + e.error + ').'; stt.className = ''; };
  rec.onend = function(){ if (S.rec === rec){ S.rec = null; stt.textContent = 'Stopped. Edit if needed, then “Fill the fields”.'; stt.className = ''; } };
  try { rec.start(); S.rec = rec; stt.textContent = '● Listening… speak now'; stt.className = 'bdc-rec'; } catch(e){ stt.textContent = 'Could not start the microphone.'; }
}
function stopRec(){ if (S.rec){ var r = S.rec; S.rec = null; try { r.stop(); } catch(e){} } }
function voiceFill(){
  stopRec();
  var d = S.detail, t = ($('#bdc-vtext') || {}).value || ''; if (!d) return;
  if (t.trim().length < 5){ toast('Say or type a bit more first.', true); return; }
  var b = $('[data-act=voicefill]'); if (b){ b.disabled = true; b.textContent = '✨ Filling…'; }
  api('POST', '/companies/' + d.company.id + '/understanding/parse', {text: t}).then(function(j){
    if (b){ b.disabled = false; b.textContent = '✨ Fill the fields'; }
    if (!j.ok){ toast(j.error, true); return; }
    toast('✨ Updated ' + j.changed.length + ' field' + (j.changed.length === 1 ? '' : 's'));
    var hint = j.contact_hint || {};
    var cur = d.contacts.filter(function(x){ return x.id === d.call.contact_id; })[0] || d.contacts[0];
    if (hint.name && !cur){ api('POST', '/companies/' + d.company.id + '/contacts', {name: hint.name, designation: hint.designation || '', phone: hint.phone || '', email: hint.email || ''}).then(function(){ loadDetail(d.company.id); load(true); }); }
    else loadDetail(d.company.id);
    var r = rowById(d.company.id); if (r){ r.u_score = j.u_score; replaceRow(r); }
  });
}
function writeOpener(){
  var d = S.detail; if (!d) return;
  var b = $('[data-act=opener]'); if (b){ b.disabled = true; b.textContent = '✨ Writing…'; }
  api('POST', '/companies/' + d.company.id + '/opener').then(function(j){
    if (!j.ok){ toast(j.error, true); if (b){ b.disabled = false; b.textContent = '✨ Write opener'; } return; }
    d.opener = j.opener; renderPanel();
  });
}

/* ---------- e-mail guess ---------- */
function guess(anchor, id){
  api('GET', '/companies/' + id + '/email-guess').then(function(j){
    if (!j.ok){ toast(j.error, true); return; }
    popAt(anchor, '<div style="font-size:11.5px;color:#667085;padding:4px 8px">Likely addresses — not verified</div>' + j.guesses.map(function(g, i){ return '<button data-g="' + i + '">' + (g.level === 'high' ? '⭐ ' : '') + esc(g.email) + '<span class="s">' + esc(g.why) + '</span></button>'; }).join(''), function(p){
      p.addEventListener('click', function(e){ var b = e.target.closest('[data-g]'); if (!b) return; closePop();
        api('PATCH', '/companies/' + id, {email: j.guesses[+b.dataset.g].email}).then(function(k){ if (!k.ok){ toast(k.error, true); return; } replaceRow(k.row); if (S.detail && S.detail.company.id === id) loadDetail(id); }); });
    });
  });
}

/* ---------- composer (3 AI versions) ---------- */
function compose(id, kind){
  var r = rowById(id) || {}, c = r.contact || {};
  var fu = S.settings.fu_days || '3,7';
  var ai = S.meta && S.meta.ai;
  var ov = dialog('<h2>✉ ' + (kind === 'details' ? 'Send the details' : 'Cold e-mail') + ' — ' + esc(r.name || '') + '</h2>'
    + '<p class="lead">' + (ai ? 'AI writes 3 versions from your business notes, call notes and your real placements. Pick one, edit, send.' : 'Write your e-mail. (Add the DeepSeek key in Settings for AI drafts.)') + '</p>'
    + '<div class="bdc-row" style="align-items:flex-end"><div class="bdc-f" style="flex:1;margin:0">To<input id="bdc-to" value="' + esc(c.email || '') + '" placeholder="name@company.com"></div>'
    + (c.name && !c.email ? '<button class="bdc-btn sm" data-x="guess" style="margin-bottom:2px">✨ Guess</button>' : '') + '</div>'
    + (ai ? '<div class="bdc-f" style="margin-top:8px">Anything specific to say? (optional)<input id="bdc-intent" placeholder="e.g. mention we have 2 ready HV design engineers"></div>'
      + '<div class="bdc-row" style="margin-bottom:8px"><button class="bdc-btn ai" data-x="ai">✨ Write 3 versions</button><span id="bdc-aist" style="font-size:12px;color:#667085"></span></div>' : '')
    + '<div id="bdc-varsbox"></div>'
    + '<div class="bdc-f">Subject<input id="bdc-subj"></div><div id="bdc-subs" class="bdc-chips" style="margin:-4px 0 8px"></div>'
    + '<div class="bdc-f">Message<textarea id="bdc-body" rows="10" placeholder="Hi …"></textarea></div>'
    + '<label style="display:flex;gap:6px;align-items:center;font-size:12.5px"><input type="checkbox" id="bdc-fu" ' + (S.settings.fu_on !== 0 ? 'checked' : '') + '> Automatic follow-up if no reply (day ' + esc(fu.replace(/,/g, ' and ')) + ') — stops when they reply</label>'
    + '<div class="bdc-btns"><span style="flex:1;font-size:11.5px;color:#98A2B3;align-self:center">Sent from your own mailbox with your signature · opens are tracked</span><button class="bdc-btn" data-x="cancel">Cancel</button><button class="bdc-btn" data-x="queue" title="Send in the next sending slot">⏳ Queue</button><button class="bdc-btn p" data-x="send">Send now ➤</button></div>', function(ov){
    var vs = [];
    function pick(i){
      var v = vs[i]; if (!v) return;
      $$('.bdc-var', ov).forEach(function(b, k){ b.classList.toggle('on', k === i); });
      ov.querySelector('#bdc-subj').value = v.subjects[0] || ''; ov.querySelector('#bdc-body').value = v.body;
      ov.querySelector('#bdc-subs').innerHTML = v.subjects.map(function(s, k){ return '<button class="bdc-chip' + (k === 0 ? ' on' : '') + '" data-sub="' + k + '">' + esc(s) + '</button>'; }).join('');
      ov.dataset.vi = i;
    }
    function runAi(){
      var st = ov.querySelector('#bdc-aist'), b = ov.querySelector('[data-x=ai]'); b.disabled = true; st.textContent = 'Writing… (10-20 s)';
      api('POST', '/companies/' + id + '/draft', {kind: kind || 'cold', note: ov.querySelector('#bdc-intent').value}).then(function(j){
        b.disabled = false; b.textContent = '↻ Rewrite';
        if (!j.ok){ st.textContent = ''; toast(j.error, true); return; }
        vs = j.variants; st.textContent = '';
        if (!ov.querySelector('#bdc-to').value && j.to) ov.querySelector('#bdc-to').value = j.to;
        var names = {short:'⚡ Short', value:'🏆 Value', candidate:'👤 Candidate-led'};
        ov.querySelector('#bdc-varsbox').innerHTML = '<div class="bdc-vars">' + vs.map(function(v, i){ return '<button class="bdc-var" data-vi="' + i + '"><b>' + (names[v.style] || esc(v.style)) + '</b>' + esc(v.body.slice(0, 70)) + '…</button>'; }).join('') + '</div>'
          + (j.facts && j.facts.length ? '<div class="bdc-facts">📊 Facts the AI may use (counted from your ATS): ' + j.facts.map(esc).join(' · ') + '</div>' : '');
        pick(0);
      });
    }
    ov.addEventListener('click', function(e){
      var v = e.target.closest('[data-vi]'); if (v && v.classList.contains('bdc-var')){ pick(+v.dataset.vi); return; }
      var sb = e.target.closest('[data-sub]'); if (sb){ var cur = vs[+ov.dataset.vi || 0]; ov.querySelector('#bdc-subj').value = cur.subjects[+sb.dataset.sub]; $$('[data-sub]', ov).forEach(function(b){ b.classList.toggle('on', b === sb); }); return; }
      var x = e.target.closest('[data-x]'); if (!x) return;
      if (x.dataset.x === 'cancel'){ ov.remove(); return; }
      if (x.dataset.x === 'ai'){ runAi(); return; }
      if (x.dataset.x === 'guess'){ api('GET', '/companies/' + id + '/email-guess').then(function(j){ if (!j.ok){ toast(j.error, true); return; } popAt(x, j.guesses.map(function(g, i){ return '<button data-g="' + i + '">' + (g.level === 'high' ? '⭐ ' : '') + esc(g.email) + '<span class="s">' + esc(g.why) + '</span></button>'; }).join(''), function(p){ p.addEventListener('click', function(e2){ var b = e2.target.closest('[data-g]'); if (!b) return; ov.querySelector('#bdc-to').value = j.guesses[+b.dataset.g].email; closePop(); }); }); }); return; }
      var body = {to: ov.querySelector('#bdc-to').value.trim(), subject: ov.querySelector('#bdc-subj').value.trim(), body: ov.querySelector('#bdc-body').value.trim(),
                  followups: ov.querySelector('#bdc-fu').checked, queue: x.dataset.x === 'queue', style: (vs[+ov.dataset.vi || 0] || {}).style || ''};
      x.disabled = true; x.textContent = x.dataset.x === 'queue' ? 'Queuing…' : 'Sending…';
      api('POST', '/companies/' + id + '/send', body).then(function(j){
        x.disabled = false; x.textContent = x.dataset.x === 'queue' ? '⏳ Queue' : 'Send now ➤';
        if (!j.ok){ toast(j.error, true); return; }
        ov.remove(); toast(j.queued ? '⏳ Queued - goes out in the next sending slot' : '✉ Sent');
        if (j.today){ S.today = j.today; renderTop(); }
        load(true); if (S.detail && S.detail.company.id === id) loadDetail(id);
      });
    });
    if (ai && (r.u_score || kind === 'details')) setTimeout(runAi, 50);
  }, true);
}

/* ---------- bulk ---------- */
function bulk(){
  var ids = Object.keys(S.checked).filter(function(k){ return S.checked[k]; }).map(Number);
  if (!ids.length) return;
  if (!(S.meta && S.meta.ai)){ toast('Bulk drafts need AI: add the DeepSeek key in Settings.', true); return; }
  var noMail = ids.filter(function(id){ var r = rowById(id); return !(r && r.contact && r.contact.email); }).length;
  dialog('<h2>✉ Bulk e-mail — ' + ids.length + ' prospect' + (ids.length === 1 ? '' : 's') + '</h2>'
    + '<p class="lead">AI writes one personal e-mail for each, from that company’s notes. Nothing is sent until you approve each one in Review.' + (noMail ? ' <b>' + noMail + ' without an e-mail address will be skipped.</b>' : '') + '</p>'
    + '<div class="bdc-f">Style<div class="bdc-chips"><button class="bdc-chip on" data-st="short">⚡ Short</button><button class="bdc-chip" data-st="value">🏆 Value</button><button class="bdc-chip" data-st="candidate">👤 Candidate-led</button></div></div>'
    + '<div class="bdc-f">Anything to say in all of them? (optional)<input id="bdc-bn" placeholder="e.g. we just placed 4 sales engineers in the switchgear industry"></div>'
    + '<label style="display:flex;gap:6px;align-items:center;font-size:12.5px"><input type="checkbox" id="bdc-bfu" checked> Automatic follow-ups if no reply</label>'
    + '<div class="bdc-btns"><button class="bdc-btn" data-x="cancel">Cancel</button><button class="bdc-btn p" data-x="go">✨ Write drafts</button></div>', function(ov){
    var style = 'short';
    ov.addEventListener('click', function(e){
      var s = e.target.closest('[data-st]'); if (s){ style = s.dataset.st; $$('[data-st]', ov).forEach(function(b){ b.classList.toggle('on', b === s); }); return; }
      var x = e.target.closest('[data-x]'); if (!x) return;
      if (x.dataset.x === 'cancel'){ ov.remove(); return; }
      x.disabled = true;
      api('POST', '/bulk-draft', {client_ids: ids, style: style, note: ov.querySelector('#bdc-bn').value, followups: ov.querySelector('#bdc-bfu').checked}).then(function(j){
        if (!j.ok){ x.disabled = false; toast(j.error, true); return; }
        ov.remove(); S.checked = {}; renderTop(); renderSheet();
        toast('✨ Writing ' + j.drafting + ' draft' + (j.drafting === 1 ? '' : 's') + (j.skipped.length ? ' · ' + j.skipped.length + ' skipped' : ''));
        review();
      });
    });
  });
}
function review(){
  var ov = dialog('<div class="bdc-row" style="margin-bottom:8px"><h2 style="margin:0">📝 Review drafts</h2><span id="bdc-rvst" style="font-size:12px;color:#667085"></span><span style="flex:1"></span><button class="bdc-btn" data-x="close">Close</button></div>'
    + '<p class="lead"><kbd>Ctrl</kbd>+<kbd>Enter</kbd> approve &amp; next · <kbd>Ctrl</kbd>+<kbd>S</kbd> skip · approved e-mails go out one by one, ' + Math.round((S.settings.gap_seconds || 150) / 60) + '+ min apart, ' + esc(S.settings.start_hour) + ':00–' + esc(S.settings.end_hour) + ':00, max ' + esc(S.settings.daily_cap) + '/day</p>'
    + '<div class="bdc-rv"><div class="l" id="bdc-rvl"></div><div class="r" id="bdc-rvr"></div></div>', null, true);
  S.review = {ov: ov, drafts: [], cur: 0, timer: null};
  function fetchList(){
    if (!ov.isConnected){ clearInterval(S.review.timer); return; }
    api('GET', '/review').then(function(j){
      if (!j.ok) return;
      var curId = (S.review.drafts[S.review.cur] || {}).id;
      S.review.drafts = j.drafts;
      var ni = j.drafts.findIndex(function(d){ return d.id === curId; }); S.review.cur = ni >= 0 ? ni : Math.min(S.review.cur, Math.max(0, j.drafts.length - 1));
      ov.querySelector('#bdc-rvst').textContent = (j.working ? '✨ Writing ' + j.working + '… · ' : '') + j.queued + ' queued to send';
      drawList(); if (!ov.querySelector('#bdc-rvr').dataset.id || ni < 0) drawCur();
      if (!j.working){ clearInterval(S.review.timer); S.review.timer = null; }
    });
  }
  function drawList(){
    var l = ov.querySelector('#bdc-rvl');
    if (!S.review.drafts.length){ l.innerHTML = '<div class="bdc-empty" style="padding:20px">No drafts waiting.</div>'; return; }
    l.innerHTML = S.review.drafts.map(function(d, i){ return '<div class="it' + (i === S.review.cur ? ' on' : '') + '" data-ri="' + i + '"><b>' + esc(d.company) + '</b><div class="s">' + esc(d.contact_name || '') + (d.status === 'drafting' ? ' · ✨ writing…' : d.status === 'draft_failed' ? ' · ⚠ ' + esc(d.error) : '') + '</div></div>'; }).join('');
  }
  function drawCur(){
    var r = ov.querySelector('#bdc-rvr'), d = S.review.drafts[S.review.cur];
    if (!d){ r.innerHTML = '<div class="bdc-empty"><h3>All done 🎉</h3>Approved e-mails are being sent from your mailbox.</div>'; r.dataset.id = ''; return; }
    r.dataset.id = d.id;
    if (d.status === 'drafting'){ r.innerHTML = '<div class="bdc-empty">✨ Writing the e-mail for ' + esc(d.company) + '…</div>'; return; }
    r.innerHTML = '<div class="bdc-f">To — ' + esc(d.contact_name || '') + (d.designation ? ', ' + esc(d.designation) : '') + ' · ' + esc(d.company) + '<input id="rv-to" value="' + esc(d.to_addr) + '"></div>'
      + '<div class="bdc-f">Subject<input id="rv-subj" value="' + esc(d.subject) + '"></div>'
      + '<div class="bdc-f">Message<textarea id="rv-body" rows="13">' + esc(d.body) + '</textarea></div>'
      + (d.status === 'draft_failed' ? '<div style="color:#B42318;font-size:12px;margin-bottom:6px">The AI could not write this one (' + esc(d.error) + '). Write it yourself, or skip.</div>' : '')
      + '<div class="bdc-btns"><button class="bdc-btn" data-x="skip">Skip (Ctrl+S)</button><button class="bdc-btn p" data-x="approve">✓ Approve &amp; next (Ctrl+Enter)</button></div>';
  }
  function act(kind){
    var d = S.review.drafts[S.review.cur]; if (!d || d.status === 'drafting') return;
    var p = kind === 'approve' ? api('POST', '/emails/' + d.id + '/approve', {to: ov.querySelector('#rv-to').value, subject: ov.querySelector('#rv-subj').value, body: ov.querySelector('#rv-body').value})
                               : api('POST', '/emails/' + d.id + '/skip');
    p.then(function(j){
      if (!j.ok){ toast(j.error, true); return; }
      S.review.drafts.splice(S.review.cur, 1); if (S.review.cur >= S.review.drafts.length) S.review.cur = Math.max(0, S.review.drafts.length - 1);
      toast(kind === 'approve' ? '✓ Approved · sends ' + when(j.scheduled_at) : 'Skipped');
      drawList(); drawCur(); load(true); fetchList();
      var b = ov.querySelector('#rv-body'); if (b) b.focus();
    });
  }
  ov.addEventListener('click', function(e){
    var it = e.target.closest('[data-ri]'); if (it){ S.review.cur = +it.dataset.ri; drawList(); drawCur(); return; }
    var x = e.target.closest('[data-x]'); if (!x) return;
    if (x.dataset.x === 'close'){ ov.remove(); return; }
    act(x.dataset.x);
  });
  function keys(e){
    if (!ov.isConnected){ document.removeEventListener('keydown', keys, true); return; }
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter'){ e.preventDefault(); e.stopPropagation(); act('approve'); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's'){ e.preventDefault(); e.stopPropagation(); act('skip'); }
  }
  document.addEventListener('keydown', keys, true);
  fetchList(); S.review.timer = setInterval(fetchList, 3000);
}

/* ---------- import ---------- */
function importDlg(){
  dialog('<h2>⬆ Import your calling list</h2><p class="lead">Copy rows from Excel / Google Sheets (or a CSV) and paste here. First row can be the headings: <b>Company, Contact, Designation, Phone, Email, Website, City, Industry</b>. Without headings, columns are read in that order.</p>'
    + '<div class="bdc-f"><textarea id="bdc-paste" rows="8" placeholder="Company\tContact\tDesignation\tPhone\tEmail\nAcme Switchgear\tPriya Nair\tHR Head\t98100 00000\tpriya@acme.in"></textarea></div>'
    + '<div id="bdc-prev"></div><div class="bdc-btns"><button class="bdc-btn" data-x="cancel">Cancel</button><button class="bdc-btn" data-x="preview">Check</button><button class="bdc-btn p" data-x="commit" disabled>Import</button></div>', function(ov){
    ov.addEventListener('click', function(e){
      var x = e.target.closest('[data-x]'); if (!x) return;
      if (x.dataset.x === 'cancel'){ ov.remove(); return; }
      var text = ov.querySelector('#bdc-paste').value;
      x.disabled = true;
      api('POST', '/import', {text: text, commit: x.dataset.x === 'commit'}).then(function(j){
        x.disabled = false;
        if (!j.ok){ toast(j.error, true); return; }
        if (x.dataset.x === 'commit'){ ov.remove(); toast('⬆ ' + j.created + ' new prospect' + (j.created === 1 ? '' : 's') + ', ' + j.contacts + ' contact' + (j.contacts === 1 ? '' : 's') + (j.skipped ? ' · ' + j.skipped + ' skipped (already clients)' : '')); S.view = 'new'; load(); return; }
        var lbl = {'new':'<span class="bdc-tag sent">New</span>', same_company:'<span class="bdc-tag">+ person</span>', exists_prospect:'<span class="bdc-tag op">Already a prospect · adds the person</span>', exists_client:'<span class="bdc-tag fail">Already a client · skipped</span>'};
        ov.querySelector('#bdc-prev').innerHTML = '<div style="margin-bottom:6px;font-size:12.5px"><b>' + j.total + '</b> rows · <b>' + j.new + '</b> new companies · <b>' + j.exists + '</b> already in HireLab</div>'
          + '<div class="bdc-imp"><table><tr><th></th><th>Company</th><th>Contact</th><th>Designation</th><th>Phone</th><th>E-mail</th></tr>' + j.preview.map(function(p){
            return '<tr><td>' + (lbl[p.state] || '') + '</td><td>' + esc(p.company) + '</td><td>' + esc(p.contact) + '</td><td>' + esc(p.designation) + '</td><td>' + esc(p.phone) + '</td><td>' + esc(p.email) + (p.email_bad ? ' ⚠' : '') + (p.contact_dup ? ' (exists)' : '') + '</td></tr>'; }).join('') + '</table></div>';
        ov.querySelector('[data-x=commit]').disabled = !(j.new || j.exists);
      });
    });
  }, true);
}

/* ---------- settings ---------- */
function settingsDlg(){
  var s = S.settings;
  dialog('<h2>⚙️ Calling targets &amp; sending</h2><p class="lead">Your own settings.</p>'
    + '<div class="bdc-row"><div class="bdc-f" style="flex:1">Calls per day<input type="number" id="st-calls" value="' + esc(s.calls_target) + '" min="0"></div><div class="bdc-f" style="flex:1">E-mails per day<input type="number" id="st-emails" value="' + esc(s.emails_target) + '" min="0"></div></div>'
    + '<div class="bdc-row"><div class="bdc-f" style="flex:1">Gap between e-mails (seconds)<input type="number" id="st-gap" value="' + esc(s.gap_seconds) + '" min="60"></div><div class="bdc-f" style="flex:1">Max e-mails per day<input type="number" id="st-cap" value="' + esc(s.daily_cap) + '" min="1"></div></div>'
    + '<div class="bdc-row"><div class="bdc-f" style="flex:1">Send between (hour)<input type="number" id="st-sh" value="' + esc(s.start_hour) + '" min="0" max="23"></div><div class="bdc-f" style="flex:1">and (hour)<input type="number" id="st-eh" value="' + esc(s.end_hour) + '" min="1" max="24"></div></div>'
    + '<div class="bdc-f">Follow-up after (days, comma separated, up to 3)<input id="st-fu" value="' + esc(s.fu_days) + '"></div>'
    + '<label style="display:flex;gap:6px;align-items:center;font-size:12.5px"><input type="checkbox" id="st-fuon" ' + (s.fu_on ? 'checked' : '') + '> Follow-ups on by default</label>'
    + '<div class="bdc-btns"><button class="bdc-btn" data-x="cancel">Cancel</button><button class="bdc-btn p" data-x="ok">Save</button></div>', function(ov){
    ov.addEventListener('click', function(e){
      var x = e.target.closest('[data-x]'); if (!x) return;
      if (x.dataset.x === 'cancel'){ ov.remove(); return; }
      var g = function(id){ return ov.querySelector(id).value; };
      api('PUT', '/settings', {calls_target: g('#st-calls'), emails_target: g('#st-emails'), gap_seconds: g('#st-gap'), daily_cap: g('#st-cap'),
                               start_hour: g('#st-sh'), end_hour: g('#st-eh'), fu_days: g('#st-fu'), fu_on: ov.querySelector('#st-fuon').checked ? 1 : 0}).then(function(j){
        if (!j.ok){ toast(j.error, true); return; }
        S.settings = j.settings; ov.remove(); renderTop(); toast('Saved');
      });
    });
  });
}

/* ---------- add prospect ---------- */
function addProspect(){
  dialog('<h2>+ New prospect</h2><div class="bdc-f">Company<input id="np-co" autofocus placeholder="Acme Switchgear Pvt Ltd"></div>'
    + '<div class="bdc-row"><div class="bdc-f" style="flex:1">Contact<input id="np-ct"></div><div class="bdc-f" style="flex:1">Phone<input id="np-ph"></div></div>'
    + '<div class="bdc-row"><div class="bdc-f" style="flex:1">E-mail<input id="np-em"></div><div class="bdc-f" style="flex:1">Website<input id="np-web"></div></div>'
    + '<div class="bdc-btns"><button class="bdc-btn" data-x="cancel">Cancel</button><button class="bdc-btn p" data-x="ok">Add</button></div>', function(ov){
    function go(){
      var g = function(id){ return ov.querySelector(id).value.trim(); };
      if (!g('#np-co')){ toast('Company name is required.', true); return; }
      var rows = [['Company','Contact','Phone','Email','Website'].join('\t'), [g('#np-co'), g('#np-ct'), g('#np-ph'), g('#np-em'), g('#np-web')].map(function(v){ return v.replace(/\t/g, ' '); }).join('\t')].join('\n');
      api('POST', '/import', {text: rows, commit: true}).then(function(j){
        if (!j.ok){ toast(j.error, true); return; }
        if (!j.created){ toast('This company is already in HireLab' + (j.skipped ? ' as a client' : '') + '.', true); return; }
        ov.remove(); toast('Added'); S.q = g('#np-co'); renderTop(); load().then(function(){ if (S.rows[0]) select(S.rows[0].id, true); });
      });
    }
    ov.addEventListener('click', function(e){ var x = e.target.closest('[data-x]'); if (!x) return; if (x.dataset.x === 'cancel') ov.remove(); else go(); });
    ov.addEventListener('keydown', function(e){ if (e.key === 'Enter'){ e.preventDefault(); go(); } });
  });
}

/* ---------- events ---------- */
var qT = null;
function wire(){
  document.addEventListener('click', function(e){
    if (!S.root || !S.root.contains(e.target)) return;
    var t = e.target, el;
    if ((el = t.closest('[data-view]'))){ S.view = el.dataset.view; S.sel = null; $('#bdc-panel').hidden = true; renderTop(); load(); return; }
    if ((el = t.closest('[data-out]'))){ if (S.detail) doOutcome(S.detail.company.id, el.dataset.out); return; }
    if ((el = t.closest('[data-act]'))){
      var a = el.dataset.act, id = +el.dataset.id || (S.detail ? S.detail.company.id : 0);
      if (a === 'check'){ S.checked[+el.dataset.id] = el.checked; renderTop(); return; }
      if (a === 'checkall'){ S.rows.forEach(function(r){ S.checked[r.id] = el.checked; }); renderSheet(); renderTop(); return; }
      if (a === 'tel'){ select(id, true); setTimeout(function(){ var n = $('#bdc-note'); if (n) n.focus(); }, 400); return; }
      e.preventDefault();
      if (a === 'statusmenu'){ popAt(el, ((S.meta && S.meta.statuses) || []).map(function(s){ return '<button data-sv="' + s[0] + '"><span style="color:' + (ST_COLORS[s[0]] || '#667085') + '">●</span> ' + esc(s[1]) + '<span class="s">' + (S.counts[s[0]] || 0) + '</span></button>'; }).join(''), function(p){ p.addEventListener('click', function(e2){ var b = e2.target.closest('[data-sv]'); if (!b) return; closePop(); S.view = b.dataset.sv; renderTop(); load(); }); }); return; }
      if (a === 'statuspick'){ select(id); popAt(el, OUTCOMES.map(function(o){ return '<button data-so="' + o[0] + '">' + o[1] + ' ' + o[2] + '<span class="s">' + o[3] + '</span></button>'; }).join(''), function(p){ p.addEventListener('click', function(e2){ var b = e2.target.closest('[data-so]'); if (!b) return; closePop(); doOutcome(id, b.dataset.so); }); }); return; }
      if (a === 'nextpick'){ nextPick(el, id); return; }
      if (a === 'mine'){ S.mine = !S.mine; renderTop(); load(); return; }
      if (a === 'add'){ addProspect(); return; }
      if (a === 'import'){ importDlg(); return; }
      if (a === 'bulk'){ bulk(); return; }
      if (a === 'review'){ review(); return; }
      if (a === 'settings'){ settingsDlg(); return; }
      if (a === 'guess'){ guess(el, id); return; }
      if (a === 'closepanel'){ $('#bdc-panel').hidden = true; S.detail = null; stopRec(); return; }
      if (a === 'compose'){ compose(id); return; }
      if (a === 'pushcall'){ pushCall(); return; }
      if (a === 'research'){ research(); return; }
      if (a === 'voice'){ startRec(); return; }
      if (a === 'voicestop'){ stopRec(); return; }
      if (a === 'voicefill'){ voiceFill(); return; }
      if (a === 'opener'){ writeOpener(); return; }
      if (a === 'setsite'){ var w = prompt('Company website:', ''); if (w) api('PATCH', '/companies/' + id, {website: w.trim()}).then(function(j){ if (!j.ok) toast(j.error, true); else { replaceRow(j.row); loadDetail(id); } }); return; }
      if (a === 'addcontact'){
        dialog('<h2>+ Person at ' + esc(S.detail.company.name) + '</h2><div class="bdc-row"><div class="bdc-f" style="flex:1">Name<input id="nc-n" autofocus></div><div class="bdc-f" style="flex:1">Designation<input id="nc-d"></div></div><div class="bdc-row"><div class="bdc-f" style="flex:1">Phone<input id="nc-p"></div><div class="bdc-f" style="flex:1">E-mail<input id="nc-e"></div></div><div class="bdc-btns"><button class="bdc-btn" data-x="cancel">Cancel</button><button class="bdc-btn p" data-x="ok">Add &amp; call this person</button></div>', function(ov){
          ov.addEventListener('click', function(e2){ var x = e2.target.closest('[data-x]'); if (!x) return; if (x.dataset.x === 'cancel'){ ov.remove(); return; }
            api('POST', '/companies/' + id + '/contacts', {name: ov.querySelector('#nc-n').value, designation: ov.querySelector('#nc-d').value, phone: ov.querySelector('#nc-p').value, email: ov.querySelector('#nc-e').value}).then(function(j){ if (!j.ok){ toast(j.error, true); return; } ov.remove(); loadDetail(id); load(true); }); });
        });
        return;
      }
      if (a === 'seqstop'){ api('POST', '/companies/' + id + '/sequence', {active: false}).then(function(j){ if (!j.ok) toast(j.error, true); else { toast('Follow-ups stopped'); loadDetail(id); load(true); } }); return; }
      return;
    }
    var tr = t.closest('tr[data-row]');
    if (tr && !t.closest('input,button,a,select')) select(+tr.dataset.row, true);
    else if (tr && t.closest('input.bdc-in')) select(+tr.dataset.row);
  });
  document.addEventListener('change', function(e){
    if (!S.root || !S.root.contains(e.target)) return;
    if (e.target.dataset.act === 'contactpick'){ var id = S.detail.company.id; api('PATCH', '/companies/' + id, {contact_id: e.target.value}).then(function(j){ if (!j.ok){ toast(j.error, true); return; } replaceRow(j.row); loadDetail(id); }); }
  });
  document.addEventListener('focusout', function(e){
    if (!S.root || !S.root.contains(e.target)) return;
    if (e.target.classList && e.target.classList.contains('bdc-in')) saveCell(e.target);
    if (e.target.dataset && e.target.dataset.u) saveU(e.target);
  });
  document.addEventListener('input', function(e){
    if (!S.root || !S.root.contains(e.target)) return;
    if (e.target.id === 'bdc-q'){ clearTimeout(qT); var v = e.target.value; qT = setTimeout(function(){ S.q = v.trim(); load(); }, 300); }
    if (e.target.dataset && e.target.dataset.u) autosize(e.target);
  });
}
function onKey(e){
  if (!S.root || !S.root.isConnected || S.root.offsetParent === null) return;
  if (document.querySelector('.bdc-ov')) return;
  var t = e.target, typing = /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable;
  if (typing){
    if (t.classList.contains('bdc-in')){
      if (e.key === 'Enter'){ e.preventDefault(); t.blur(); }
      else if (e.key === 'Escape'){ var r = rowById(+t.dataset.id); var f = t.dataset.f; t.value = f === 'name' ? r.name : ((r.contact || {})[f === 'contact_name' ? 'name' : f] || ''); t.blur(); }
      else if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && !e.shiftKey){
        e.preventDefault(); var f2 = t.dataset.f; t.blur(); move(e.key === 'ArrowDown' ? 1 : -1);
        var n = $('tr[data-row="' + S.sel + '"] input[data-f="' + f2 + '"]'); if (n) n.focus();
      }
    } else if (e.key === 'Escape') t.blur();
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  var k = e.key;
  if (k === 'ArrowDown' || k === 'j'){ e.preventDefault(); move(1); }
  else if (k === 'ArrowUp' || k === 'k'){ e.preventDefault(); move(-1); }
  else if (k === 'Enter' && S.sel){ e.preventDefault(); select(S.sel, true); }
  else if (k === 'Escape'){ if (!$('#bdc-panel').hidden){ $('#bdc-panel').hidden = true; S.detail = null; stopRec(); } }
  else if (k === '/'){ e.preventDefault(); $('#bdc-q').focus(); }
  else if (!S.sel) return;
  else if (k === 'c' || k === 'C'){ e.preventDefault(); callRow(S.sel); }
  else if (k === 'e' || k === 'E'){ e.preventDefault(); compose(S.sel); }
  else if (k === 'n' || k === 'N'){ e.preventDefault(); select(S.sel, true); setTimeout(function(){ var n = $('#bdc-note'); if (n) n.focus(); }, 350); }
  else if (k === ' '){ e.preventDefault(); S.checked[S.sel] = !S.checked[S.sel]; var cb = $('tr[data-row="' + S.sel + '"] input[data-act=check]'); if (cb) cb.checked = !!S.checked[S.sel]; renderTop(); }
  else if (/^[1-7]$/.test(k)){ e.preventDefault(); var o = OUTCOMES[+k - 1]; if (!S.detail || S.detail.company.id !== S.sel) loadDetail(S.sel); doOutcome(S.sel, o[0]); }
}

window.BDCalling = {open: open, close: close, _state: S};
})();
