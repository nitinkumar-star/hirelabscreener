/* HireLab Team Chat — calls on every page: start / join a call, and ring for incoming ones.
   Served at /chat-ring.js; loaded by the Team Chat page and by the chat windows
   (chat-dock.js) on every other ATS page. Exposes window.HLCalls:
     HLCalls.start(channelId, 'audio'|'video')   start a call (or join the one running there)
     HLCalls.join(callId)                         open the call window
     HLCalls.active                               {callId: call} running in my chats
     HLCalls.on(fn)                               fn() whenever that list changes
   The call itself runs in its own window (/call/<id>, chat-call.html). */
(function(){
  'use strict';
  if (window.HLCalls) return;
  var H = window.HLCalls = {active: {}, me: 0, fns: [], on: function(fn){ this.fns.push(fn); }};
  var handledKey = 'hlcall_handled', ringing = null, ringTimer = null, actx = null, stopped = false, pollT = null;

  function api(m, p, b){
    return fetch('/api/chat' + p, {method: m, credentials: 'same-origin', headers: b ? {'Content-Type':'application/json'} : {}, body: b ? JSON.stringify(b) : undefined})
      .then(function(r){ return r.json().catch(function(){ return {}; }).then(function(j){ j = j || {}; j._status = r.status; return j; }); })
      .catch(function(){ return {ok: false, _status: 0}; });
  }
  function esc(x){ return String(x == null ? '' : x).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function handled(){ try { return JSON.parse(localStorage.getItem(handledKey) || '{}'); } catch(e){ return {}; } }
  function markHandled(id){ try { var h = handled(); h[id] = Date.now(); Object.keys(h).forEach(function(k){ if (Date.now() - h[k] > 864e5) delete h[k]; }); localStorage.setItem(handledKey, JSON.stringify(h)); } catch(e){} }
  function features(){ var w = Math.min(1200, screen.availWidth - 60), h = Math.min(780, screen.availHeight - 80);
    return 'width=' + w + ',height=' + h + ',left=' + Math.max(0, (screen.availWidth - w) / 2) + ',top=' + Math.max(0, (screen.availHeight - h) / 2); }
  function openWin(id){
    var w = window.open('/call/' + id, 'hlcall' + id, features());
    if (!w) location.href = '/call/' + id;                // popup blocked: use this tab
    else try { w.focus(); } catch(e){}
  }
  H.join = function(id){ markHandled(id); stopRing(); openWin(id); };
  H.start = function(chid, kind){
    // open the window inside the click (popup blockers), then point it at the call
    var w = window.open('', 'hlcall_new', features());
    if (w) try { w.document.write('<body style="background:#0B1F19;color:#fff;font:16px sans-serif;display:flex;align-items:center;justify-content:center;height:90vh">Starting the call…</body>'); } catch(e){}
    return api('POST', '/channels/' + chid + '/call', {kind: kind}).then(function(r){
      if (!r.ok){ if (w) w.close(); alert(r.error || 'Could not start the call'); return r; }
      markHandled(r.call.id);
      if (w){ w.location.href = '/call/' + r.call.id; try { w.name = 'hlcall' + r.call.id; } catch(e){} }
      else location.href = '/call/' + r.call.id;
      poll();
      return r;
    });
  };

  /* ---------- incoming call screen (own shadow root) ---------- */
  var host = document.createElement('div'); host.style.cssText = 'position:fixed;inset:auto;z-index:2147483000';
  var root = host.attachShadow({mode: 'open'});
  ['keydown','keyup','keypress'].forEach(function(t){ root.addEventListener(t, function(e){ e.stopPropagation(); }); });
  root.innerHTML = '<style>'
    + '.ov{position:fixed;inset:0;background:rgba(10,20,17,.55);display:flex;align-items:center;justify-content:center;font-family:"DM Sans",-apple-system,"Segoe UI",Roboto,sans-serif}'
    + '.box{background:#0E3B30;color:#fff;border-radius:18px;padding:26px 30px;width:min(340px,calc(100vw - 32px));text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.4)}'
    + '.av{width:86px;height:86px;border-radius:50%;margin:0 auto 12px;background:#1D9A78;display:flex;align-items:center;justify-content:center;font-size:32px;font-weight:700;animation:p 1.6s infinite}'
    + '@keyframes p{0%,100%{box-shadow:0 0 0 0 rgba(61,214,140,.6)}50%{box-shadow:0 0 0 20px rgba(61,214,140,0)}}'
    + 'h2{margin:0 0 4px;font-size:20px}p{margin:0 0 20px;color:#A9D3C4;font-size:14px}'
    + '.row{display:flex;gap:14px;justify-content:center}'
    + 'button{border:0;border-radius:28px;padding:12px 20px;font:inherit;font-weight:700;font-size:15px;cursor:pointer;color:#fff}'
    + '.no{background:#E5484D}.yes{background:#1D9A78}.yes:hover{background:#178a6a}.no:hover{background:#c93c41}'
    + '@media (prefers-reduced-motion:reduce){.av{animation:none}}'
    + '</style><div id="slot"></div>';
  var slot = root.getElementById('slot');
  root.addEventListener('click', function(e){
    var b = e.target.closest('[data-a]'); if (!b || !ringing) return;
    var id = ringing.id;
    if (b.dataset.a === 'yes') H.join(id);
    else { markHandled(id); stopRing(); api('POST', '/calls/' + id + '/decline'); }
  });
  function initials(n){ return String(n||'?').split(/\s+/).filter(Boolean).slice(0,2).map(function(w){ return w[0].toUpperCase(); }).join(''); }
  function showRing(c){
    if (ringing && ringing.id === c.id) return;
    ringing = c;
    if (!host.isConnected) document.body.appendChild(host);
    slot.innerHTML = '<div class="ov" role="alertdialog" aria-label="Incoming call"><div class="box"><div class="av">'+esc(initials(c.by_name))+'</div>'
      + '<h2>'+esc(c.by_name)+'</h2><p>Incoming '+(c.kind === 'video' ? 'video' : 'audio')+' call'+(c.dm ? '' : ' in #' + esc(c.chat))+'</p>'
      + '<div class="row"><button class="no" data-a="no">✕ Decline</button><button class="yes" data-a="yes">'+(c.kind === 'video' ? '🎥' : '📞')+' Accept</button></div></div></div>';
    tone();
    clearInterval(ringTimer); ringTimer = setInterval(tone, 2200);
    try {
      if (document.hidden && window.Notification && Notification.permission === 'granted'){
        var n = new Notification('📞 ' + c.by_name + ' is calling', {body: (c.kind === 'video' ? 'Video' : 'Audio') + ' call - click to answer', tag: 'hlcall-' + c.id, requireInteraction: true});
        n.onclick = function(){ window.focus(); n.close(); };
      }
    } catch(e){}
  }
  function stopRing(){ ringing = null; slot.innerHTML = ''; clearInterval(ringTimer); ringTimer = null; }
  function tone(){
    try {
      var AC = window.AudioContext || window.webkitAudioContext; if (!AC) return;
      actx = actx || new AC(); if (actx.state === 'suspended') actx.resume();
      [[660,0],[880,.18],[660,.5],[880,.68]].forEach(function(p){ var o = actx.createOscillator(), g = actx.createGain(); o.frequency.value = p[0]; var t = actx.currentTime + p[1];
        g.gain.setValueAtTime(.0001, t); g.gain.exponentialRampToValueAtTime(.3, t + .02); g.gain.exponentialRampToValueAtTime(.0001, t + .16);
        o.connect(g); g.connect(actx.destination); o.start(t); o.stop(t + .18); });
    } catch(e){}
  }

  /* ---------- polling ---------- */
  function poll(){
    clearTimeout(pollT);
    if (stopped) return;
    api('GET', '/calls/active').then(function(r){
      if (r._status === 403) { stopped = true; return; }          // freelancers: no chat
      if (r._status === 401) { pollT = setTimeout(poll, 30000); return; }
      if (r.ok){
        H.me = r.me;
        var before = JSON.stringify(Object.keys(H.active).sort().map(function(k){ return [k, H.active[k].people]; }));
        H.active = {}; (r.calls || []).forEach(function(c){ H.active[c.id] = c; });
        var after = JSON.stringify(Object.keys(H.active).sort().map(function(k){ return [k, H.active[k].people]; }));
        if (before !== after) H.fns.forEach(function(fn){ try { fn(); } catch(e){} });
        var h = handled(), inc = (r.calls || []).filter(function(c){
          return c.dm && c.ringing && c.started_by !== r.me && c.people.indexOf(r.me) < 0 && c.declined.indexOf(r.me) < 0 && !h[c.id] && c.people.length;
        })[0];
        if (inc) showRing(inc);
        else if (ringing && (!H.active[ringing.id] || !H.active[ringing.id].ringing || handled()[ringing.id])) stopRing();
      }
      pollT = setTimeout(poll, document.hidden ? 8000 : 4000);
    });
  }
  window.addEventListener('storage', function(e){ if (e.key === handledKey && ringing && handled()[ringing.id]) stopRing(); });
  document.addEventListener('visibilitychange', function(){ if (!document.hidden) poll(); });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', poll); else poll();
})();
