"""
App menu on every page.

The main app (index.html) has the sidebar. The separate pages (Job Mail, Talent
Pool, Market Pulse, Hot List, My Calls, Re-engage, Scheduler, Org Map, Backup,
Storage) had only a "← App" link. This module puts the SAME menu on all of them:

  * a "☰ Menu" button at the top-left of the page, which opens a side drawer
    with every menu item (role-aware: recruiters do not see admin-only items,
    freelance callers see Home + My Calls), the current page highlighted;
  * items that live inside the main app (Jobs, Tasks, CRM, …) open the app
    straight on that view through /?go=<item> (index.html clicks the same
    sidebar item, so behaviour is identical).

It works by adding one <script src="/app-nav.js"> tag to the HTML of the pages
listed in NAV_PAGES (after the response is built) — no page file needs editing,
and new pages only need adding to the list. Public candidate pages
(update-profile, careers, apply, the e-mail landing pages) never get it.
"""

import re

from flask import Blueprint, Response, request

bp = Blueprint('app_nav', __name__)

NAV_PAGES = re.compile(r'^/(jobmail|talent-pool|newsletter|hotlist|my-calls|reengage|backup|schedule|orgmap|admin/storage|finance)/?$')
_TAG = b'<script src="/app-nav.js" defer></script>'


@bp.after_app_request
def _inject(resp):
    try:
        if request.method != 'GET' or resp.status_code != 200 or not NAV_PAGES.match(request.path or ''):
            return resp
        if not (resp.mimetype or '').startswith('text/html'):
            return resp
        resp.direct_passthrough = False          # pages served with send_file (e.g. Org Map)
        body = resp.get_data()
        if b'/app-nav.js' in body:
            return resp
        i = body.lower().rfind(b'</body>')
        body = (body[:i] + _TAG + body[i:]) if i >= 0 else (body + _TAG)
        resp.set_data(body)
    except Exception as e:                      # never break a page over the menu
        print('[app_nav] inject skipped: %s' % e)
    return resp


_JS = r"""(function(){
  if (window.__hlNav) return; window.__hlNav = true;
  var I = {
    home:'<path d="M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10"/>',
    jobs:'<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    tasks:'<path d="M9 11l3 3 8-8"/><path d="M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9"/>',
    chat:'<path d="M21 12a8 8 0 0 1-11.6 7.1L4 21l1.9-5.4A8 8 0 1 1 21 12z"/>',
    crm:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9h10M7 13h6"/>',
    inv:'<path d="M6 2h9l5 5v15H6z"/><path d="M14 2v6h6M9 13h6M9 17h6"/>',
    mail:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
    team:'<circle cx="9" cy="8" r="3"/><path d="M3 20c.7-3 3-5 6-5s5.3 2 6 5"/><circle cx="17" cy="9" r="2.5"/><path d="M16 14c2.5 0 4.3 1.6 5 4"/>',
    bd:'<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
    search:'<circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/>',
    cal:'<rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18M8 2v4M16 2v4"/>',
    re:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/><path d="M17 3v4M15 5h4"/>',
    jm:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/><path d="M15 17l2 2 4-4"/>',
    pool:'<circle cx="8" cy="8" r="3"/><circle cx="16" cy="8" r="3"/><circle cx="12" cy="15" r="3"/>',
    news:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 8h10M7 12h10M7 16h6"/>',
    hot:'<path d="M12 3c1 3.5 5 5.5 5 10a5 5 0 0 1-10 0c0-2.2 1-3.6 2-4.6.3 1.6 1 2.6 2 3.1C11 8.5 11 6 12 3z"/>',
    phone:'<path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2"/>',
    org:'<rect x="9" y="2" width="6" height="5" rx="1"/><rect x="2" y="16" width="6" height="5" rx="1"/><rect x="16" y="16" width="6" height="5" rx="1"/><path d="M12 7v4M5 16v-2h14v2"/>',
    disk:'<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>'
  };
  // a: admin only · r: everyone except freelance · f: freelance callers too
  var ITEMS = [
    ['Main', null],
    ['Home', '/?go=home', 'home', 'f'],
    ['Jobs', '/?go=jobs', 'jobs', 'r'],
    ['Tasks', '/?go=tasks', 'tasks', 'r'],
    ['Talent', '/?go=central', 'search', 'r'],
    ['Communications', '/?go=waai', 'chat', 'r'],
    ['Email Box', '/?go=email', 'mail', 'r'],
    ['Scheduler', '/schedule', 'cal', 'r'],
    ['Org Map', '/orgmap', 'org', 'r'],
    ['Business', null],
    ['Clients (CRM)', '/?go=crm', 'crm', 'a'],
    ['BD Command Center', '/?go=bd', 'bd', 'a'],
    ['Invoicing', '/?go=invoicing', 'inv', 'a'],
    ['Profit & Cash', '/finance', 'inv', 'a'],
    ['Freelancers', '/?go=freelancers', 'team', 'a'],
    ['Candidate engagement', null],
    ['Job Mail', '/jobmail', 'jm', 'r'],
    ['Hot List', '/hotlist', 'hot', 'a'],
    ['My Calls', '/my-calls', 'phone', 'f'],
    ['Talent Pool', '/talent-pool', 'pool', 'a'],
    ['Market Pulse', '/newsletter', 'news', 'a'],
    ['Re-engage', '/reengage', 're', 'a'],
    ['Admin', null],
    ['Backup', '/backup', 'disk', 'o'],
    ['Storage', '/admin/storage', 'disk', 'o']
  ];
  var css = '#hln-btn{display:inline-flex;align-items:center;gap:6px;border:1px solid #E3E1DA;background:#fff;color:#0E2A47;border-radius:9px;padding:6px 11px;font:600 13px/1.2 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;cursor:pointer;flex-shrink:0}'
    + '#hln-btn:hover{background:#F7F6F2}#hln-btn.fx{position:fixed;top:10px;left:10px;z-index:99990;box-shadow:0 2px 8px rgba(0,0,0,.12)}'
    + '#hln-ov{position:fixed;inset:0;background:rgba(14,42,71,.32);z-index:99998;display:none}'
    + '#hln-dr{position:fixed;top:0;left:0;bottom:0;width:280px;max-width:86vw;background:#fff;z-index:99999;transform:translateX(-102%);transition:transform .18s ease;box-shadow:4px 0 24px rgba(0,0,0,.15);display:flex;flex-direction:column;font:14px/1.4 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:#1F2328}'
    + '#hln-dr.on{transform:none}#hln-ov.on{display:block}'
    + '#hln-dr .hd{display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid #E3E1DA}'
    + '#hln-dr .lg{width:30px;height:30px;border-radius:8px;background:#0F6E56;color:#fff;font-weight:700;display:flex;align-items:center;justify-content:center}'
    + '#hln-dr .x{margin-left:auto;border:none;background:none;font-size:22px;line-height:1;cursor:pointer;color:#6B6F76}'
    + '#hln-dr nav{overflow-y:auto;padding:6px 8px 20px;flex:1}'
    + '#hln-dr .gp{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#8A8F96;margin:14px 10px 4px}'
    + '#hln-dr a{display:flex;align-items:center;gap:11px;padding:9px 10px;border-radius:9px;color:#1F2328;text-decoration:none;font-weight:500}'
    + '#hln-dr a:hover{background:#F2F0EA}#hln-dr a.cur{background:#E6F4EE;color:#0F6E56;font-weight:700}'
    + '#hln-dr svg{width:18px;height:18px;flex-shrink:0}#hln-dr .ft{border-top:1px solid #E3E1DA;padding:10px 16px;font-size:12px;color:#6B6F76}';
  function el(tag, attrs, html){ var e = document.createElement(tag); for (var k in attrs) e.setAttribute(k, attrs[k]); if (html != null) e.innerHTML = html; return e; }
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function build(user){
    var role = user ? user.role : '', fl = role === 'freelancer_sourcer', adm = !!(user && user.is_company_admin), owner = role === 'admin';
    var path = location.pathname.replace(/\/$/, '') || '/';
    var html = '', group = '', pend = '';
    ITEMS.forEach(function(it){
      if (!it[1]) { pend = it[0]; return; }
      var lvl = it[3];
      var ok = fl ? lvl === 'f' : (lvl === 'o' ? owner : lvl === 'a' ? adm : true);
      if (!ok) return;
      if (pend) { html += '<div class="gp">' + esc(pend) + '</div>'; pend = ''; }
      var cur = it[1].charAt(1) !== '?' && path === it[1];
      html += '<a href="' + it[1] + '"' + (cur ? ' class="cur"' : '') + '><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + I[it[2]] + '</svg>' + esc(it[0]) + '</a>';
    });
    var dr = el('div', {id: 'hln-dr', role: 'dialog', 'aria-label': 'Menu'},
      '<div class="hd"><div class="lg">H</div><b>HireLab</b><button class="x" aria-label="Close menu">&times;</button></div><nav>' + html + '</nav>'
      + '<div class="ft">' + esc(user ? (user.display_name || user.username) : '') + '</div>');
    var ov = el('div', {id: 'hln-ov'});
    document.body.appendChild(ov); document.body.appendChild(dr);
    function open(){ dr.classList.add('on'); ov.classList.add('on'); }
    function close(){ dr.classList.remove('on'); ov.classList.remove('on'); }
    ov.onclick = close; dr.querySelector('.x').onclick = close;
    document.addEventListener('keydown', function(e){ if (e.key === 'Escape') close(); });
    var btn = el('button', {id: 'hln-btn', type: 'button', 'aria-label': 'Open menu', title: 'Menu'},
      '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 6h16M4 12h16M4 18h16"/></svg>Menu');
    btn.onclick = open;
    // put it at the start of the page's own top bar when there is one, else float it
    var bar = document.querySelector('.top') || document.querySelector('header') || document.querySelector('.topbar');
    if (bar && bar.getBoundingClientRect().top < 80) { bar.insertBefore(btn, bar.firstChild); }
    else { btn.className = 'fx'; document.body.appendChild(btn); }
  }
  function start(){
    document.head.appendChild(el('style', {}, css));
    fetch('/api/auth/status', {credentials: 'same-origin'}).then(function(r){ return r.json(); })
      .then(function(j){ build(j && j.user); }).catch(function(){ build(null); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
"""


@bp.route('/app-nav.js', methods=['GET'])
def nav_js():
    return Response(_JS, mimetype='application/javascript', headers={'Cache-Control': 'no-cache'})
