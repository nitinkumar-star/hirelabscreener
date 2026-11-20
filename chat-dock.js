/* HireLab Team Chat — the small chat windows at the bottom right of every ATS page
   (like Google Chat inside Gmail). Served at /chat-dock.js; added to the main app
   (index.html) and to every separate page that has the app menu (modules/app_nav.py).

   - A "Chat" button with the unread count; it opens the list of conversations.
   - Each conversation opens in a small window (up to 3 side by side); windows can
     be minimised, popped out to the full Team Chat page, or closed, and stay open
     when you move to another page.
   - A new direct message opens its window (minimised, highlighted) with a chime.
   - Everything is drawn inside a Shadow DOM, so the page's CSS and the chat's CSS
     never touch, and key presses typed in the chat never reach the page's shortcuts.
   Uses the same /api/chat routes as the full page (modules/team_chat.py). */
(function(){
  'use strict';
  if (window.__hlChatDock) return; window.__hlChatDock = true;
  if (/^\/team-chat\/?$/.test(location.pathname)) return;          // the full page has it all

  var LS = 'hlcd_v1', COLORS = ['#0F6E56','#2D6FB3','#B54708','#7A5AF8','#C01048','#027A48','#155EEF','#DC6803','#6941C6','#0E9384'];
  var S = {me:0, users:{}, ulist:[], channels:[], rev:0, refs:{}, msgs:{}, more:{}, member:{}, wins:[], roster:false,
           q:'', online:[], typing:{}, ready:false, timer:null, drafts:{}, flash:{}, emojis:[]};
  try { var saved = JSON.parse(localStorage.getItem(LS) || '{}'); S.wins = (saved.wins || []).slice(0, 3); S.roster = !!saved.roster; S.drafts = saved.drafts || {}; } catch(e){}
  function persist(){ try { localStorage.setItem(LS, JSON.stringify({wins: S.wins, roster: S.roster, drafts: S.drafts})); } catch(e){} }

  /* ---------- helpers ---------- */
  function esc(x){ return String(x == null ? '' : x).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function api(method, path, body, isForm){
    var o = {method: method, credentials: 'same-origin', headers: {}};
    if (body && !isForm){ o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
    if (isForm) o.body = body;
    return fetch('/api/chat' + path, o).then(function(r){
      return r.json().catch(function(){ return {ok:false, error:'HTTP ' + r.status}; }).then(function(j){ j = j || {}; j._status = r.status; return j; });
    }).catch(function(){ return {ok:false, error:'Network error', _status:0}; });
  }
  function uname(id){ if (+id === 0) return 'HireLab AI'; var u = S.users[id]; return u ? u.name : 'Someone'; }
  function stEmoji(id){ var x = (S.statuses || {})[id]; return x && (x.emoji || x.text) ? ' <span title="'+esc((x.emoji||'')+' '+(x.text||''))+'">'+esc(x.emoji || '💬')+'</span>' : ''; }
  function initials(n){ return String(n||'?').split(/\s+/).filter(Boolean).slice(0,2).map(function(w){ return w.charAt(0).toUpperCase(); }).join('') || '?'; }
  function color(id){ return COLORS[Math.abs(+id||0) % COLORS.length]; }
  function av(id, size){ if (+id === 0) return '<span class="av" style="background:linear-gradient(135deg,#7A5AF8,#0F6E56);width:'+size+'px;height:'+size+'px;font-size:'+Math.round(size*.5)+'px">✨</span>';
    return '<span class="av" style="background:'+color(id)+';width:'+size+'px;height:'+size+'px;font-size:'+Math.round(size*.4)+'px">'+esc(initials(uname(id)))+'</span>'; }
  function chan(id){ for (var i = 0; i < S.channels.length; i++) if (S.channels[i].id === id) return S.channels[i]; return null; }
  function dmOther(c){ var o = (c.members||[]).filter(function(x){ return x !== S.me; })[0]; return o || S.me; }
  function title(c){ return !c ? '' : c.kind === 'ai' ? 'HireLab AI' : c.kind === 'dm' ? uname(dmOther(c)) + (dmOther(c) === S.me ? ' (you)' : '') : '#' + c.name; }
  function parseTs(s){ if (!s) return null; var d = new Date(String(s).replace(' ', 'T')); return isNaN(d) ? null : d; }
  function tm(s){ var d = parseTs(s); if (!d) return ''; var t = new Date(); return d.toDateString() === t.toDateString() ? d.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'}) : d.toLocaleDateString([], {day:'numeric', month:'short'}) + ' ' + d.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'}); }
  function muted(){ try { return JSON.parse(localStorage.getItem('tc_muted') || '{}'); } catch(e){ return {}; } }
  function idx(L, id){ for (var i = 0; i < L.length; i++) if (L[i].id === id) return i; return -1; }
  function refName(r, k){ if (!r || r.restricted) return k === 'c' ? 'a candidate' : 'a job'; if (r.missing) return k === 'c' ? 'deleted candidate' : 'deleted job';
    return k === 'c' ? (r.name || 'candidate') : ((r.role || 'job') + (r.client ? ' · ' + r.client : '')); }
  function refHref(r, k, id){ if (!r || r.restricted || r.missing) return ''; return k === 'c' ? '/?cand=' + id + (r.mandate_id ? '&mid=' + r.mandate_id : '') : '/#mandate/' + id; }
  function fmt(body){
    var s = esc(body || '');
    s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    s = s.replace(/(^|[\s(])\*\*?([^*\n]+?)\*\*?(?=[\s).,!?:;]|$)/g, '$1<b>$2</b>');
    s = s.replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,!?:;]|$)/g, '$1<i>$2</i>');
    s = s.replace(/(https?:\/\/[^\s<]+[^\s<.,;:!?)])/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
    s = s.replace(/&lt;@(\d+)&gt;/g, function(_, id){ return '<span class="at'+(+id === S.me ? ' me' : '')+'">@' + esc(uname(+id)) + '</span>'; });
    s = s.replace(/(^|\s)@(channel|here|everyone)\b/gi, '$1<span class="at me">@$2</span>');
    s = s.replace(/&lt;call:(\d+)&gt;/g, function(_, id){ var c = window.HLCalls && HLCalls.active[id];
      return c ? '<button class="joinc" data-a="joincall" data-call="'+id+'">🔴 Join · '+c.people.length+'</button>' : ''; });
    s = s.replace(/&lt;([cm]):(\d+)&gt;/g, function(_, k, id){ var r = S.refs[k + ':' + id], h = refHref(r, k, id);
      return h ? '<a class="ref" href="'+h+'">'+(k==='c'?'👤 ':'💼 ')+esc(refName(r, k))+(r && r.stage ? ' · ' + esc(r.stage) : '')+'</a>' : '<span class="ref dim">'+(k==='c'?'👤 ':'💼 ')+esc(refName(r, k))+'</span>'; });
    return s.replace(/\n/g, '<br>');
  }
  function plain(body){ return String(body || '').replace(/<call:\d+>\s*/g, '').replace(/<@(\d+)>/g, function(_, id){ return '@' + uname(+id); }).replace(/<([cm]):(\d+)>/g, function(_, k, id){ return refName(S.refs[k + ':' + id], k); }).trim(); }
  function mentionsMe(b){ return new RegExp('<@' + S.me + '>').test(b || '') || /(^|\s)@(channel|here|everyone)\b/i.test(b || ''); }
  function maxWins(){ var w = window.innerWidth; return w < 700 ? 1 : Math.max(1, Math.min(3, Math.floor((w - 380) / 336))); }
  function isPhone(){ return window.innerWidth < 700; }

  /* ---------- shadow host ---------- */
  var host = document.createElement('div'); host.id = 'hl-chat-dock';
  host.style.cssText = 'position:fixed;right:0;bottom:0;z-index:99995;width:0;height:0';
  var root = host.attachShadow({mode: 'open'});
  ['keydown','keyup','keypress','input','paste'].forEach(function(t){ root.addEventListener(t, function(e){ e.stopPropagation(); }); });
  var CSS = ''
    + ':host{all:initial}'
    + '*{box-sizing:border-box;font-family:"DM Sans","Plus Jakarta Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}'
    + '.dock{position:fixed;right:16px;bottom:0;display:flex;flex-direction:row-reverse;align-items:flex-end;gap:12px;pointer-events:none;color:#101828;font-size:13.5px;line-height:1.4}'
    + '.dock>*{pointer-events:auto}'
    + '.launch{margin-bottom:16px;display:flex;align-items:center;gap:8px;border:0;border-radius:24px;background:#0F6E56;color:#fff;font-weight:700;font-size:14px;padding:11px 18px 11px 15px;box-shadow:0 8px 24px rgba(15,110,86,.35);cursor:pointer}'
    + '.launch:hover{background:#0B5A46}'
    + '.badge{background:#E5484D;color:#fff;border-radius:10px;font-size:11.5px;font-weight:800;padding:1px 7px;min-width:20px;text-align:center}'
    + '.panel{width:320px;height:min(480px,calc(100vh - 90px));background:#fff;border-radius:12px 12px 0 0;box-shadow:0 10px 40px rgba(16,24,40,.25);display:flex;flex-direction:column;overflow:hidden;border:1px solid #E4E7EC;border-bottom:0}'
    + '.ph{display:flex;align-items:center;gap:8px;padding:10px 12px;background:#0E3B30;color:#fff}'
    + '.ph b{font-size:14.5px;flex:1}'
    + '.ib{border:0;background:transparent;color:inherit;border-radius:6px;cursor:pointer;width:28px;height:28px;display:inline-flex;align-items:center;justify-content:center;font-size:15px;text-decoration:none}'
    + '.ph .ib:hover{background:rgba(255,255,255,.15)}'
    + '.srch{padding:8px 10px;border-bottom:1px solid #EAECF0}'
    + '.srch input{width:100%;border:1px solid #D0D5DD;border-radius:8px;padding:7px 10px;font-size:13px;outline:0}'
    + '.srch input:focus{border-color:#0F6E56}'
    + '.list{flex:1;overflow-y:auto;padding:4px 0}'
    + '.sec{font-size:11px;font-weight:800;color:#667085;text-transform:uppercase;letter-spacing:.06em;padding:10px 14px 4px}'
    + '.row{display:flex;align-items:center;gap:10px;width:100%;border:0;background:none;text-align:left;padding:7px 14px;cursor:pointer;color:#101828;font-size:13.5px}'
    + '.row:hover{background:#F2F4F7}'
    + '.row .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
    + '.row .sub{display:block;font-size:12px;color:#667085;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
    + '.row.un .nm{font-weight:800}'
    + '.hash{width:28px;height:28px;border-radius:7px;background:#E7F4EF;color:#0F6E56;display:inline-flex;align-items:center;justify-content:center;font-weight:800;flex:0 0 28px}'
    + '.av{border-radius:50%;color:#fff;font-weight:700;display:inline-flex;align-items:center;justify-content:center;flex:0 0 auto;position:relative}'
    + '.on-dot{position:absolute;right:-1px;bottom:-1px;width:9px;height:9px;border-radius:50%;background:#3DD68C;border:2px solid #fff}'
    + '.foot{border-top:1px solid #EAECF0;padding:8px 12px;font-size:12.5px;display:flex;justify-content:space-between;gap:8px}'
    + '.foot a{color:#0F6E56;font-weight:700;text-decoration:none}'
    + '.win{width:320px;height:min(440px,calc(100vh - 90px));background:#fff;border-radius:12px 12px 0 0;box-shadow:0 10px 40px rgba(16,24,40,.25);display:flex;flex-direction:column;overflow:hidden;border:1px solid #E4E7EC;border-bottom:0}'
    + '.win.min{height:auto;width:250px}'
    + '.win.min .wb,.win.min .wc,.win.min .wj{display:none}'
    + '.wh{display:flex;align-items:center;gap:8px;padding:8px 8px 8px 12px;background:#0E3B30;color:#fff;cursor:pointer;min-height:44px}'
    + '.wh .t{flex:1;min-width:0;font-weight:700;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
    + '.wh .ib:hover{background:rgba(255,255,255,.15)}'
    + '.win.alert .wh{background:#0F6E56;animation:pulse 1.2s ease-in-out 4}'
    + '@keyframes pulse{50%{background:#1D9A78}}'
    + '.wb{flex:1;overflow-y:auto;padding:8px 10px 6px;background:#fff}'
    + '.m{padding:2px 0}'
    + '.m .h{display:flex;align-items:baseline;gap:6px;margin-top:8px}'
    + '.m .h b{font-size:13px}'
    + '.m .h .tm{font-size:11px;color:#98A2B3}'
    + '.m .tx{font-size:13.5px;overflow-wrap:anywhere}'
    + '.m.mine .h b{color:#0F6E56}'
    + '.m.ment{background:#FFF8E1;border-radius:6px;padding:2px 6px;margin:0 -6px}'
    + '.del{color:#98A2B3;font-style:italic}'
    + '.at{background:#EEF4FF;color:#2D6FB3;border-radius:4px;padding:0 3px;font-weight:600}'
    + '.at.me{background:#FDE68A;color:#7A4A00}'
    + '.ref{display:inline-block;background:#E7F4EF;color:#0F6E56;border-radius:6px;padding:0 6px;font-weight:600;text-decoration:none;margin:1px 0}'
    + '.ref.dim{background:#F2F4F7;color:#667085}'
    + 'code{background:#F2F4F7;border-radius:4px;padding:0 3px;font-size:12px;color:#C01048}'
    + 'a{color:#2D6FB3}'
    + '.file{display:inline-flex;align-items:center;gap:6px;border:1px solid #E4E7EC;border-radius:7px;padding:5px 8px;margin-top:3px;text-decoration:none;color:#101828;font-size:12.5px;max-width:100%}'
    + '.file span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
    + '.img{display:block;max-width:100%;max-height:160px;border-radius:7px;margin-top:3px;border:1px solid #E4E7EC}'
    + '.rx{display:inline-block;border:1px solid #E4E7EC;background:#F9FAFB;border-radius:999px;padding:0 6px;font-size:12px;margin:3px 3px 0 0;cursor:pointer}'
    + '.rx.mine{border-color:#2D6FB3;background:#EEF4FF}'
    + '.thr{font-size:12px;color:#2D6FB3;font-weight:700;text-decoration:none}'
    + '.day{text-align:center;font-size:11px;color:#98A2B3;font-weight:700;margin:10px 0 2px}'
    + '.more{display:block;margin:4px auto;border:1px solid #D0D5DD;background:#fff;border-radius:7px;font-size:12px;padding:3px 10px;cursor:pointer}'
    + '.typing{font-size:11.5px;color:#667085;padding:0 12px;height:16px}'
    + '.joinc{border:0;background:#E5484D;color:#fff;border-radius:999px;padding:1px 10px;font-weight:700;font-size:12px;cursor:pointer}'
    + '.wh .ib.live{background:#E5484D}'
    + '.wj{padding:10px;border-top:1px solid #EAECF0;text-align:center;font-size:13px}'
    + '.wc{border-top:1px solid #EAECF0;padding:6px 8px 8px;display:flex;align-items:flex-end;gap:4px}'
    + '.wc textarea{flex:1;border:1px solid #D0D5DD;border-radius:16px;padding:7px 12px;font-size:13.5px;resize:none;outline:0;max-height:110px;min-height:34px;line-height:1.35}'
    + '.wc textarea:focus{border-color:#0F6E56}'
    + '.wc .ib{color:#667085;width:32px;height:32px}'
    + '.wc .ib:hover{background:#F2F4F7;color:#101828}'
    + '.wc .send{color:#0F6E56}'
    + '.btn{border:0;background:#0F6E56;color:#fff;border-radius:7px;padding:6px 14px;font-weight:700;cursor:pointer}'
    + '.empty{padding:20px 14px;color:#667085;text-align:center;font-size:13px}'
    + '.ac{position:absolute;left:8px;right:8px;bottom:52px;background:#fff;border:1px solid #E4E7EC;border-radius:9px;box-shadow:0 8px 24px rgba(16,24,40,.18);max-height:180px;overflow:auto;padding:4px;z-index:2}'
    + '.ac .o{display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:6px;cursor:pointer;font-size:13px}'
    + '.ac .o.on,.ac .o:hover{background:#E7F4EF}'
    + '.win{position:relative}'
    + '@media (max-width:699px){'
    + '  .dock{right:8px;left:8px;justify-content:flex-start}'
    + '  .launch{margin-bottom:calc(76px + env(safe-area-inset-bottom));padding:10px 14px}'
    + '  .launch .lbl{display:none}'
    + '  .panel,.win{position:fixed;left:0;right:0;bottom:0;width:auto;height:min(78vh,560px);border-radius:14px 14px 0 0}'
    + '  .win.min{position:static;width:auto;height:auto;flex:1}'
    + '}'
    + '@media (prefers-reduced-motion:reduce){.win.alert .wh{animation:none}}';
  root.innerHTML = '<style>' + CSS + '</style><div class="dock" id="dock"></div>';
  var dock = root.getElementById('dock');

  /* ---------- render ---------- */
  function unreadTotal(){
    var m = muted();
    return S.channels.reduce(function(a, c){ return a + (m[c.id] ? 0 : (c.kind !== 'channel' ? (c.unread||0) : (c.mentions||0))); }, 0);
  }
  function render(){
    if (!S.ready) { dock.innerHTML = ''; return; }
    var h = '', total = unreadTotal();
    var openWins = S.wins.filter(function(w){ return chan(w.id); }).slice(0, maxWins());
    var phoneOpen = isPhone() && (S.roster || openWins.some(function(w){ return !w.min; }));
    if (!phoneOpen) h += '<button class="launch" data-a="roster" title="Team Chat" aria-label="Team Chat'+(total?', '+total+' unread':'')+'">💬 <span class="lbl">Chat</span>'+(total?'<span class="badge">'+total+'</span>':'')+'</button>';
    if (S.roster) h += rosterHtml();
    openWins.forEach(function(w){ if (!(isPhone() && S.roster)) h += winHtml(w); });
    var keep = {};
    root.querySelectorAll('textarea[data-w]').forEach(function(t){ keep[t.dataset.w] = {v: t.value, f: root.activeElement === t, s: t.selectionStart}; });
    var scroll = {};
    root.querySelectorAll('.wb[data-w]').forEach(function(b){ scroll[b.dataset.w] = {top: b.scrollTop, bottom: b.scrollHeight - b.scrollTop - b.clientHeight < 40}; });
    var sq = root.querySelector('.srch input'), sqf = sq && root.activeElement === sq;
    dock.innerHTML = h;
    root.querySelectorAll('textarea[data-w]').forEach(function(t){
      var k = keep[t.dataset.w]; t.value = k ? k.v : (S.drafts[t.dataset.w] || ''); autosize(t);
      if (k && k.f){ t.focus(); try { t.setSelectionRange(k.s, k.s); } catch(e){} }
    });
    root.querySelectorAll('.wb[data-w]').forEach(function(b){ var s = scroll[b.dataset.w]; if (!s || s.bottom) b.scrollTop = b.scrollHeight; else b.scrollTop = s.top; });
    if (sqf){ var n = root.querySelector('.srch input'); n.focus(); n.setSelectionRange(n.value.length, n.value.length); }
  }
  function rosterHtml(){
    var m = muted(), q = S.q.toLowerCase(), dms = {};
    S.channels.forEach(function(c){ if (c.kind === 'dm') dms[dmOther(c)] = c; });
    var recent = S.channels.filter(function(c){ return !c.archived && (c.kind === 'channel' || c.last_at); })
      .filter(function(c){ return !q || title(c).toLowerCase().indexOf(q) >= 0; })
      .sort(function(a, b){ return (b.unread>0) - (a.unread>0) || String(b.last_at||'').localeCompare(String(a.last_at||'')); });
    var h = '<div class="panel" role="dialog" aria-label="Team Chat"><div class="ph"><b>💬 Team Chat</b>'
      + '<a class="ib" href="/team-chat" title="Open full Team Chat" aria-label="Open full Team Chat">↗</a>'
      + '<button class="ib" data-a="roster" title="Close" aria-label="Close">✕</button></div>'
      + '<div class="srch"><input type="search" placeholder="Find a person or channel" aria-label="Find a person or channel" value="'+esc(S.q)+'"></div><div class="list">';
    var conv = recent.slice(0, 30);
    if (conv.length) h += '<div class="sec">Conversations</div>' + conv.map(function(c){
      var un = !m[c.id] && c.unread > 0, badge = m[c.id] ? '' : (c.kind !== 'channel' ? c.unread : c.mentions);
      var icon = c.kind === 'ai' ? av(0, 28) : c.kind === 'dm' ? '<span style="position:relative;display:inline-flex">'+av(dmOther(c), 28)+(S.online.indexOf(dmOther(c))>=0?'<span class="on-dot"></span>':'')+'</span>' : '<span class="hash">'+(c.is_private?'🔒':'#')+'</span>';
      return '<button class="row'+(un?' un':'')+'" data-open="'+c.id+'">'+icon+'<span class="nm">'+esc(title(c))+(c.kind==='dm'?stEmoji(dmOther(c)):'')+(c.kind==='channel'&&c.topic?'<span class="sub">'+esc(c.topic)+'</span>':'')+'</span>'+(badge?'<span class="badge">'+badge+'</span>':(un?'<span class="badge" style="background:#98A2B3">'+c.unread+'</span>':''))+'</button>';
    }).join('');
    var people = S.ulist.filter(function(u){ return u.id !== S.me && !dms[u.id] && (!q || u.name.toLowerCase().indexOf(q) >= 0); });
    if (people.length) h += '<div class="sec">Start a chat</div>' + people.map(function(u){
      return '<button class="row" data-dm="'+u.id+'"><span style="position:relative;display:inline-flex">'+av(u.id, 28)+(S.online.indexOf(u.id)>=0?'<span class="on-dot"></span>':'')+'</span><span class="nm">'+esc(u.name)+stEmoji(u.id)+'</span></button>';
    }).join('');
    if (!conv.length && !people.length) h += '<div class="empty">Nothing matches “'+esc(S.q)+'”.</div>';
    h += '</div><div class="foot"><span style="color:#667085">'+S.online.filter(function(x){ return x !== S.me; }).length+' online</span><a href="/team-chat">Open full chat ↗</a></div></div>';
    return h;
  }
  function winHtml(w){
    var c = chan(w.id), list = S.msgs[w.id], member = S.member[w.id] !== false;
    var dot = c.kind === 'dm' && S.online.indexOf(dmOther(c)) >= 0 ? ' 🟢' : '';
    var un = !muted()[c.id] && (c.kind !== 'channel' ? c.unread : c.mentions);
    var h = '<section class="win'+(w.min?' min':'')+(S.flash[w.id]?' alert':'')+'" aria-label="'+esc(title(c))+'">'
      + '<div class="wh" data-a="toggle" data-w="'+w.id+'">'+(c.kind==='ai'?av(0, 26):c.kind==='dm'?av(dmOther(c), 26):'<span class="hash" style="width:26px;height:26px;flex-basis:26px">#</span>')
      + '<span class="t">'+esc(title(c))+dot+(c.kind==='dm'?stEmoji(dmOther(c)):'')+'</span>'+(w.min && un ? '<span class="badge">'+un+'</span>' : '')
      + '<button class="ib" data-a="toggle" data-w="'+w.id+'" title="'+(w.min?'Open':'Minimise')+'" aria-label="'+(w.min?'Open':'Minimise')+'">'+(w.min?'▴':'▁')+'</button>'
      + callBtns(c)
      + '<a class="ib" href="/team-chat#c'+w.id+'" title="Open in full Team Chat" aria-label="Open in full Team Chat">↗</a>'
      + '<button class="ib" data-a="close" data-w="'+w.id+'" title="Close" aria-label="Close">✕</button></div>';
    if (w.min) return h + '</section>';
    h += '<div class="wb" data-w="'+w.id+'">';
    if (!list) h += '<div class="empty">Loading…</div>';
    else {
      if (S.more[w.id]) h += '<button class="more" data-a="older" data-w="'+w.id+'">Earlier messages</button>';
      if (!list.length) h += '<div class="empty">'+(c.kind==='ai' ? 'Ask HireLab AI anything, e.g. “aaj kitne interviews hain?”' : c.kind==='dm' ? 'Say hello to '+esc(title(c))+' 👋' : 'No messages in '+esc(title(c))+' yet.')+'</div>';
      var prev = null;
      list.forEach(function(m){
        var d1 = (parseTs(m.created_at)||new Date()).toDateString();
        if (!prev || d1 !== (parseTs(prev.created_at)||new Date()).toDateString()) { h += '<div class="day">'+esc(new Date(d1).toLocaleDateString([], {weekday:'short', day:'numeric', month:'short'}))+'</div>'; prev = null; }
        var grp = prev && prev.user_id === m.user_id && (parseTs(m.created_at) - parseTs(prev.created_at)) < 5*60e3;
        h += '<div class="m'+(m.user_id===S.me?' mine':'')+(m.user_id!==S.me&&mentionsMe(m.body)&&!m.deleted?' ment':'')+'">';
        if (!grp) h += '<div class="h"><b>'+esc(m.user_id===S.me?'You':uname(m.user_id))+'</b>'+(m.user_id!==S.me?stEmoji(m.user_id):'')+'<span class="tm">'+esc(tm(m.created_at))+'</span></div>';
        if (m.deleted) h += '<div class="tx del">Message deleted</div>';
        else {
          if (m.body) h += '<div class="tx">'+fmt(m.body)+(m.edited_at?' <span class="del" style="font-size:11px">(edited)</span>':'')+'</div>';
          if (m.file){ var url = '/api/chat/files/' + m.id;
            h += /^image\//.test(m.file.type) ? '<a href="'+url+'?inline=1" target="_blank" rel="noopener"><img class="img" src="'+url+'?inline=1" alt="'+esc(m.file.name)+'" loading="lazy"></a>'
                                              : '<a class="file" href="'+url+'" target="_blank" rel="noopener">📎 <span>'+esc(m.file.name)+'</span></a>'; }
          if (m.reactions && m.reactions.length) h += '<div>'+m.reactions.map(function(r){ return '<span class="rx'+(r.users.indexOf(S.me)>=0?' mine':'')+'" data-a="react" data-m="'+m.id+'" data-e="'+esc(r.emoji)+'" title="'+esc(r.users.map(uname).join(', '))+'">'+r.emoji+' '+r.users.length+'</span>'; }).join('')+'</div>';
          if (m.reply_count) h += '<a class="thr" href="/team-chat#c'+w.id+'" title="Open the thread in full Team Chat">💬 '+m.reply_count+' repl'+(m.reply_count===1?'y':'ies')+' ↗</a>';
        }
        h += '</div>'; prev = m;
      });
    }
    h += '</div><div class="typing">'+esc(typingText(w.id))+'</div>';
    if (c.archived) h += '<div class="wj">This channel is archived.</div>';
    else if (!member) h += '<div class="wj">You are reading '+esc(title(c))+'. <button class="btn" data-a="join" data-w="'+w.id+'">Join</button></div>';
    else h += '<div class="wc"><button class="ib" data-a="file" data-w="'+w.id+'" title="Attach a file" aria-label="Attach a file">📎</button>'
      + '<textarea rows="1" data-w="'+w.id+'" placeholder="Message '+esc(title(c))+'" aria-label="Message '+esc(title(c))+'"></textarea>'
      + '<button class="ib send" data-a="send" data-w="'+w.id+'" title="Send" aria-label="Send">➤</button></div>';
    return h + '</section>';
  }
  function callBtns(c){
    if (c.kind === 'ai' || c.archived || !window.HLCalls) return '';
    var live = Object.keys(HLCalls.active).map(function(k){ return HLCalls.active[k]; }).filter(function(x){ return x.channel_id === c.id; })[0];
    if (live) return '<button class="ib live" data-a="joincall" data-call="'+live.id+'" title="Join the call" aria-label="Join the call">🔴</button>';
    return '<button class="ib" data-a="call" data-kind="audio" data-w="'+c.id+'" title="Audio call" aria-label="Audio call">📞</button>'
      + '<button class="ib" data-a="call" data-kind="video" data-w="'+c.id+'" title="Video call" aria-label="Video call">🎥</button>';
  }
  function typingText(id){ var who = (S.typing[id] || []).map(uname); return who.length ? who.join(', ') + (who.length > 1 ? ' are' : ' is') + ' typing…' : ''; }
  function autosize(t){ t.style.height = 'auto'; t.style.height = Math.min(110, t.scrollHeight) + 'px'; }

  /* ---------- actions ---------- */
  function openWin(id, opts){
    opts = opts || {};
    var i = -1; S.wins.forEach(function(w, k){ if (w.id === id) i = k; });
    if (i >= 0){ if (!opts.quiet) S.wins[i].min = false; var w = S.wins.splice(i, 1)[0]; S.wins.unshift(w); }
    else { S.wins.unshift({id: id, min: !!opts.min}); }
    S.wins = S.wins.slice(0, maxWins());
    if (isPhone() && !opts.min) S.roster = false;
    persist(); render(); load(id);
    if (!opts.min && !opts.quiet) setTimeout(function(){ var t = root.querySelector('textarea[data-w="'+id+'"]'); if (t) t.focus(); markRead(id); }, 60);
  }
  function load(id){
    if (S.msgs[id]) return;
    api('GET', '/channels/' + id + '/messages').then(function(r){
      if (!r.ok){ S.msgs[id] = []; render(); return; }
      Object.assign(S.refs, r.refs || {}); S.msgs[id] = r.messages; S.more[id] = r.more; S.member[id] = r.member;
      render(); markRead(id);
    });
  }
  function upsert(m){
    if (m.parent_id) return;                              // thread replies live in the full page
    var L = S.msgs[m.channel_id]; if (!L) return;
    var i = idx(L, m.id); if (i >= 0) L[i] = m; else { L.push(m); L.sort(function(a, b){ return a.id - b.id; }); }
  }
  function markRead(id){
    var w = S.wins.filter(function(x){ return x.id === id; })[0], c = chan(id), L = S.msgs[id];
    if (!w || w.min || !c || !L || !L.length || document.hidden) return;
    var top = L[L.length - 1].id;
    delete S.flash[id];
    if (c.unread || c.mentions || (c.last_read_id||0) < top){ c.unread = 0; c.mentions = 0; c.last_read_id = top; api('POST', '/channels/' + id + '/read', {upto: top}); render(); }
  }
  function encode(text){
    var out = String(text || '');
    S.ulist.slice().sort(function(a, b){ return b.name.length - a.name.length; }).forEach(function(u){
      var rx = new RegExp('(^|[\\s(])@' + u.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?![\\w])', 'gi');
      out = out.replace(rx, '$1<@' + u.id + '>');
    });
    return out.trim();
  }
  function send(id){
    var t = root.querySelector('textarea[data-w="'+id+'"]'); if (!t) return;
    var body = encode(t.value); if (!body) return;
    t.value = ''; autosize(t); delete S.drafts[id]; persist();
    api('POST', '/channels/' + id + '/messages', {body: body}).then(function(r){
      if (!r.ok){ t.value = plain(body); flashMsg(r.error || 'Could not send'); return; }
      Object.assign(S.refs, r.refs || {}); upsert(r.message); render();
    });
  }
  function upload(id, file){
    if (file.size > 25*1024*1024){ flashMsg('Files up to 25 MB.'); return; }
    var t = root.querySelector('textarea[data-w="'+id+'"]'), fd = new FormData();
    fd.append('file', file); fd.append('body', encode(t ? t.value : ''));
    api('POST', '/channels/' + id + '/upload', fd, true).then(function(r){
      if (!r.ok){ flashMsg(r.error || 'Upload failed'); return; }
      if (t){ t.value = ''; delete S.drafts[id]; persist(); }
      Object.assign(S.refs, r.refs || {}); upsert(r.message); render();
    });
  }
  function flashMsg(text){ var d = document.createElement('div'); d.textContent = text;
    d.style.cssText = 'position:fixed;bottom:90px;right:20px;background:#101828;color:#fff;padding:8px 14px;border-radius:8px;font-size:13px;pointer-events:none';
    root.appendChild(d); setTimeout(function(){ d.remove(); }, 2600); }

  /* @ autocomplete inside a window */
  var ac = null;
  function acCheck(t){
    var pos = t.selectionStart, m = /(^|\s)@([\w .'-]{0,20})$/.exec(t.value.slice(0, pos));
    acClose(); if (!m) return;
    var q = m[2].toLowerCase().trim();
    var opts = S.ulist.filter(function(u){ return !q || u.name.toLowerCase().indexOf(q) >= 0; }).slice(0, 6);
    if ('ai'.indexOf(q) === 0) opts.unshift({id: 0, name: 'AI', bot: true});
    if (!opts.length) return;
    ac = {t: t, start: pos - m[2].length - 1, opts: opts, i: 0};
    var el = document.createElement('div'); el.className = 'ac'; ac.el = el;
    t.closest('.win').appendChild(el); acDraw();
  }
  function acDraw(){ ac.el.innerHTML = ac.opts.map(function(u, i){ return '<div class="o'+(i===ac.i?' on':'')+'" data-ac="'+i+'">'+av(u.id, 20)+esc(u.name)+'</div>'; }).join(''); }
  function acPick(i){ var u = ac.opts[i], t = ac.t, end = t.selectionStart, ins = '@' + u.name + ' ';
    t.value = t.value.slice(0, ac.start) + ins + t.value.slice(end); var p = ac.start + ins.length; acClose(); t.focus(); t.setSelectionRange(p, p); }
  function acClose(){ if (ac){ ac.el.remove(); ac = null; } }

  root.addEventListener('mousedown', function(e){ var o = e.target.closest('[data-ac]'); if (o && ac){ e.preventDefault(); acPick(+o.dataset.ac); } });
  root.addEventListener('click', function(e){
    if (e.target.closest('a')) return;                    // links (pop-out, files, cards) just open
    var el = e.target.closest('[data-open],[data-dm],[data-a]'); if (!el) return;
    if (el.dataset.open){ openWin(+el.dataset.open); return; }
    if (el.dataset.dm){ var u = +el.dataset.dm;
      api('POST', '/dm', {user: u}).then(function(r){ if (!r.ok){ flashMsg(r.error || 'Could not open'); return; }
        refreshState().then(function(){ openWin(r.id); }); }); return; }
    var a = el.dataset.a, id = +el.dataset.w;
    if (a === 'roster'){ S.roster = !S.roster; persist(); render(); if (S.roster) setTimeout(function(){ var i = root.querySelector('.srch input'); if (i && !isPhone()) i.focus(); }, 30); }
    else if (a === 'toggle'){ e.stopPropagation(); S.wins.forEach(function(w){ if (w.id === id) w.min = !w.min; }); delete S.flash[id]; persist(); render(); markRead(id);
      var w0 = S.wins.filter(function(w){ return w.id === id; })[0]; if (w0 && !w0.min) setTimeout(function(){ var t = root.querySelector('textarea[data-w="'+id+'"]'); if (t) t.focus(); }, 30); }
    else if (a === 'close'){ e.stopPropagation(); S.wins = S.wins.filter(function(w){ return w.id !== id; }); delete S.flash[id]; persist(); render(); }
    else if (a === 'send') send(id);
    else if (a === 'call'){ e.stopPropagation(); if (window.HLCalls) HLCalls.start(id, el.dataset.kind); }
    else if (a === 'joincall'){ e.stopPropagation(); if (window.HLCalls) HLCalls.join(+el.dataset.call); }
    else if (a === 'file'){ var fi = document.createElement('input'); fi.type = 'file'; fi.onchange = function(){ if (fi.files[0]) upload(id, fi.files[0]); }; fi.click(); }
    else if (a === 'join') api('POST', '/channels/' + id + '/join').then(function(){ S.member[id] = true; delete S.msgs[id]; refreshState().then(function(){ load(id); }); });
    else if (a === 'older'){ var L = S.msgs[id] || [];
      api('GET', '/channels/' + id + '/messages?before=' + (L[0] ? L[0].id : 0)).then(function(r){ if (!r.ok) return; Object.assign(S.refs, r.refs || {});
        var b = root.querySelector('.wb[data-w="'+id+'"]'), h0 = b ? b.scrollHeight : 0;
        S.msgs[id] = r.messages.concat(L); S.more[id] = r.more; render(); b = root.querySelector('.wb[data-w="'+id+'"]'); if (b) b.scrollTop = b.scrollHeight - h0; }); }
    else if (a === 'react') api('POST', '/messages/' + el.dataset.m + '/react', {emoji: el.dataset.e}).then(function(r){ if (r.ok){ upsert(r.message); render(); } });
  });
  root.addEventListener('keydown', function(e){
    var t = e.target;
    if (ac && t === ac.t){
      if (e.key === 'ArrowDown'){ e.preventDefault(); ac.i = (ac.i + 1) % ac.opts.length; acDraw(); return; }
      if (e.key === 'ArrowUp'){ e.preventDefault(); ac.i = (ac.i - 1 + ac.opts.length) % ac.opts.length; acDraw(); return; }
      if (e.key === 'Enter' || e.key === 'Tab'){ e.preventDefault(); acPick(ac.i); return; }
      if (e.key === 'Escape'){ acClose(); return; }
    }
    if (t.tagName === 'TEXTAREA' && t.dataset.w && e.key === 'Enter' && !e.shiftKey && !e.isComposing){ e.preventDefault(); send(+t.dataset.w); }
    if (e.key === 'Escape' && !ac){ if (S.roster){ S.roster = false; persist(); render(); } }
  });
  var lastTyping = 0;
  root.addEventListener('input', function(e){
    var t = e.target;
    if (t.tagName === 'TEXTAREA' && t.dataset.w){ autosize(t); acCheck(t); S.drafts[t.dataset.w] = t.value; persist();
      if (t.value && Date.now() - lastTyping > 3000){ lastTyping = Date.now(); api('POST', '/channels/' + t.dataset.w + '/typing'); } }
    if (t.matches && t.matches('.srch input')){ S.q = t.value; render(); }
  });
  root.addEventListener('paste', function(e){
    var t = e.target; if (!(t.tagName === 'TEXTAREA' && t.dataset.w)) return;
    var f = e.clipboardData && e.clipboardData.files && e.clipboardData.files[0]; if (f){ e.preventDefault(); upload(+t.dataset.w, f); }
  });
  root.addEventListener('focusin', function(e){ var t = e.target; if (t.dataset && t.dataset.w) markRead(+t.dataset.w); });

  /* ---------- polling + alerts ---------- */
  function refreshState(){
    return api('GET', '/state').then(function(r){
      if (!r.ok) return r;
      S.me = r.me; S.ulist = r.users; S.users = {}; r.users.forEach(function(u){ S.users[u.id] = u; });
      S.channels = r.channels; S.emojis = r.emojis; if (!S.rev) S.rev = r.rev; S.statuses = r.statuses || {};
      S.wins = S.wins.filter(function(w){ return chan(w.id); });
      if (!S.ready){ S.ready = true; document.body.appendChild(host); loadRing(); }
      render(); S.wins.forEach(function(w){ load(w.id); });
      return r;
    });
  }
  function loadRing(){
    if (window.HLCalls){ HLCalls.on(render); return; }
    var sc = document.createElement('script'); sc.src = '/chat-ring.js';
    sc.onload = function(){ if (window.HLCalls) HLCalls.on(render); render(); };
    document.head.appendChild(sc);
  }
  function poll(){
    clearTimeout(S.timer);
    api('GET', '/poll?since=' + S.rev).then(function(r){
      if (r._status === 401 || r._status === 403){ stop(r._status); return; }
      if (r.ok){
        var known = {}; S.channels.forEach(function(c){ known[c.id] = 1; });
        S.channels = r.channels; S.online = r.online || []; S.typing = r.typing || {}; if (r.statuses) S.statuses = r.statuses;
        Object.assign(S.refs, r.refs || {});
        var fresh = [];
        (r.messages || []).forEach(function(m){
          if (m.rev > S.rev) S.rev = m.rev;
          var L = S.msgs[m.channel_id], seen = L && idx(L, m.id) >= 0;
          if (!seen && m.user_id !== S.me && !m.deleted && !m.edited_at && !m.parent_id) fresh.push(m);
          upsert(m);
        });
        if (r.rev > S.rev) S.rev = r.rev;
        alerts(fresh);
        render();
        S.wins.forEach(function(w){ if (!w.min) markRead(w.id); });
      }
      schedule();
    });
  }
  function schedule(){
    var active = S.roster || S.wins.some(function(w){ return !w.min; });
    S.timer = setTimeout(poll, document.hidden ? 30000 : (active ? 4000 : 15000));
  }
  var stopped = false;
  function stop(code){ stopped = true; host.remove(); S.ready = false; if (code === 401) setTimeout(boot, 30000); }
  var actx = null;
  function chime(){
    try { var AC = window.AudioContext || window.webkitAudioContext; if (!AC) return; actx = actx || new AC(); if (actx.state === 'suspended') actx.resume();
      [[740,0],[988,.12]].forEach(function(p){ var o = actx.createOscillator(), g = actx.createGain(); o.frequency.value = p[0]; var t = actx.currentTime + p[1];
        g.gain.setValueAtTime(.0001, t); g.gain.exponentialRampToValueAtTime(.22, t + .02); g.gain.exponentialRampToValueAtTime(.0001, t + .14);
        o.connect(g); g.connect(actx.destination); o.start(t); o.stop(t + .16); }); } catch(e){}
  }
  function alerts(fresh){
    var m = muted(), rang = false;
    fresh.forEach(function(msg){
      var c = chan(msg.channel_id); if (!c || m[c.id]) return;
      if (c.kind === 'ai') return;                          // HireLab AI: just the count on the button
      var dm = c.kind === 'dm', men = mentionsMe(msg.body);
      if (!dm && !men) return;
      var w = S.wins.filter(function(x){ return x.id === c.id; })[0];
      var watching = w && !w.min && !document.hidden;
      if (watching) return;
      if (dm && !w){ S.flash[c.id] = 1; openWin(c.id, {min: true, quiet: true}); }
      else if (w) S.flash[c.id] = 1;
      if (!rang){ chime(); rang = true; }
      if (document.hidden && window.Notification && Notification.permission === 'granted'){
        try { var n = new Notification(uname(msg.user_id) + (dm ? '' : ' in ' + title(c)), {body: plain(msg.body).slice(0, 140) || (msg.file ? '📎 ' + msg.file.name : ''), tag: 'hlcd-' + msg.id});
          n.onclick = function(){ window.focus(); openWin(c.id); n.close(); }; } catch(e){}
      }
    });
  }
  // keep clear of the page's own bottom-right things: the e-mail compose window, the "calls to make" pill
  function avoid(){
    if (!S.ready) return;
    var r = 16, g = document.querySelector('.gc-win'), p = document.getElementById('hl-pill');
    if (g && g.offsetParent !== null && window.innerWidth >= 700) r = Math.max(r, window.innerWidth - g.getBoundingClientRect().left + 12);
    if (p && p.offsetParent !== null && window.innerWidth >= 700) r = Math.max(r, window.innerWidth - p.getBoundingClientRect().left + 12);
    var want = window.innerWidth >= 700 ? r + 'px' : '';
    if (dock.style.right !== want) dock.style.right = want;
  }
  setInterval(avoid, 1500);
  document.addEventListener('visibilitychange', function(){ if (!document.hidden && S.ready && !stopped){ poll(); } });
  window.addEventListener('resize', function(){ if (S.ready) render(); });
  window.addEventListener('storage', function(e){       // the same windows in another tab
    if (e.key !== LS || !S.ready) return;
    try { var v = JSON.parse(e.newValue || '{}'); S.wins = (v.wins || []).slice(0, 3); S.roster = !!v.roster; render(); S.wins.forEach(function(w){ load(w.id); }); } catch(err){}
  });

  function boot(){
    stopped = false;
    refreshState().then(function(r){
      if (r && r.ok){ schedule(); return; }
      if (r && r._status === 401) setTimeout(boot, 20000);   // not signed in yet (login screen): look again later
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
