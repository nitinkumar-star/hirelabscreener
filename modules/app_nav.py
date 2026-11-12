"""
App menu on every page — the SAME menu as the main app's sidebar.

The main app (index.html) has the sidebar. The separate pages (Talent Pool,
Market Pulse, Hot List, Team Activity, Re-engage, Scheduler, Org Map, Talent
Map, Profit & Cash, Backup, Storage, Job Mail, My Calls) get this menu.

Nov 2026: the menu here is no longer a second hand-written list (it had drifted:
other order, other names, other items). The items are READ FROM index.html's
#sidebar — ids, links, names, icons, order — so a change to the main sidebar
shows up here by itself. Who sees what follows the same rules as the main app
(recruiter sub-accounts, platform admin, workspace wording / hidden modules,
Org Map add-on). Desktop: the same fixed sidebar, 248 px; phones: a drawer
behind a "Menu" button. Items that live inside the main app open it on that
view through /?go=<item>.

It works by adding one <script src="/app-nav.js"> tag to the HTML of the pages
listed in NAV_PAGES (after the response is built) — no page file needs editing.
Public candidate pages (update-profile, careers, apply, e-mail landing pages)
never get it.
"""

import re

from flask import Blueprint, Response, request

bp = Blueprint('app_nav', __name__)

NAV_PAGES = re.compile(r'^/(jobmail|talent-map|talent-pool|newsletter|hotlist|my-calls|reengage|backup|schedule|orgmap|admin/storage|finance|team-activity)/?$')
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


# ── The main sidebar, read from index.html ─────────────────────────────────
# Never in the menu on any page (index.html force-hides these too: BD is a tab
# of Clients, Freelancers a tab of Admin, Mandates lives under Jobs).
ALWAYS_HIDDEN = {'sb-bd', 'sb-freelancers', 'sb-mandates', 'sb-command', 'sb-analytics', 'sb-responses', 'sb-ai', 'sb-waai'}
BOTTOM = ('sb-admin', 'sb-gear')          # pinned to the bottom, as in the app
_A_RX = re.compile(r'<a\b([^>]*\bid="(sb-[a-z0-9-]+)"[^>]*)>(.*?)</a>', re.S)
_CACHE = {'mtime': None, 'menu': None}


def _attr(attrs, name):
    m = re.search(r'\b%s="([^"]*)"' % name, attrs)
    return m.group(1) if m else ''


def _text(html_):
    import html as _h
    return _h.unescape(re.sub(r'<[^>]+>', '', html_ or '')).strip()


def parse_sidebar(page):
    """[{id, href, title, svg, rs_hide, bottom}] from the main app's #sidebar, in order."""
    i = page.find('<div id="sidebar">')
    if i < 0:
        return {'items': [], 'build': ''}
    j = page.find('<div id="main"', i)
    side = page[i:j if j > 0 else i + 60000]
    items = []
    for m in _A_RX.finditer(side):
        attrs, sid, inner = m.group(1), m.group(2), m.group(3)
        if sid in ALWAYS_HIDDEN or 'sb-gear' not in _attr(attrs, 'class').split():
            continue
        t = re.search(r'<span class="sb-nav-title">(.*?)</span>', inner, re.S)
        if t:
            title = _text(t.group(1))
        else:   # e.g. Admin Panel / Settings: the first plain <span> with words in it
            title = next((_text(x) for x in re.findall(r'<span>(.*?)</span>', inner, re.S) if _text(x)), '')
        svg = re.search(r'<svg\b[^>]*class="sb-nav-ic[^"]*"[^>]*>(.*?)</svg>', inner, re.S)
        href = _attr(attrs, 'href') or '#'
        items.append({'id': sid, 'title': title, 'svg': svg.group(1).strip() if svg else '',
                      'href': ('/?go=' + sid[3:]) if href.startswith('#') else href,
                      'rs_hide': 'rs-hide' in _attr(attrs, 'class').split(),
                      'bottom': sid in BOTTOM})
    b = re.search(r'class="sb-logo-s">(.*?)</div>', side, re.S)
    return {'items': items, 'build': _text(b.group(1)) if b else ''}


def menu():
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'index.html')
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {'items': [], 'build': ''}
    if _CACHE['mtime'] != mt:
        with open(path, encoding='utf-8') as f:
            _CACHE['menu'] = parse_sidebar(f.read())
        _CACHE['mtime'] = mt
    return _CACHE['menu']


_JS = r"""(function(){
  if (window.__hlNav) return; window.__hlNav = true;
  var MENU = __MENU__;
  // Same rules as the main app: recruiter sub-accounts do not get these (index.html: body.rs-scoped …)
  var RS_IDS = ['sb-crm','sb-bd','sb-invoicing','sb-command','sb-analytics','sb-orgmap','sb-freelancers','sb-admin'];
  var W = 248, BP = 1000;
  var ALIAS = {'/finance': 'sb-invoicing', '/my-calls': 'sb-hotlist'};   // pages opened from inside another menu item
  var F = '"DM Sans","Plus Jakarta Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';
  var css = ''
    + '#hln-sb{position:fixed;top:0;left:0;bottom:0;width:' + W + 'px;background:#fff;border-right:1px solid #E4E7EC;z-index:99990;display:flex;flex-direction:column;font:13px/1.3 ' + F + ';color:#334155;box-sizing:border-box;padding:10px 10px 0}'
    + '#hln-sb *{box-sizing:border-box;font-family:' + F + ' !important;letter-spacing:normal !important;text-transform:none !important}'
    + '#hln-sb div,#hln-sb span,#hln-sb a{line-height:1.3 !important}'
    + '#hln-sb .lt,#hln-sb .ls,#hln-sb .un,#hln-sb .ur,#hln-sb a.it span{white-space:nowrap !important;overflow:hidden;text-overflow:ellipsis}'
    + '#hln-sb .lg{display:flex;align-items:center;gap:11px;padding:4px 6px 12px}'
    + '#hln-sb .lb{width:34px;height:34px;border-radius:9px;background:#0F766E;color:#fff;font-weight:700;font-size:13px;display:flex;align-items:center;justify-content:center;flex-shrink:0;text-decoration:none}'
    + '#hln-sb .lt{font-size:14px !important;font-weight:700 !important;color:#0F172A !important;line-height:1.5 !important;margin:0 !important}#hln-sb .ls{font-size:11px !important;color:#5B6475 !important;margin:0 !important}'
    + '#hln-sb .cl{margin-left:auto;border:1px solid #E4E7EC;background:#fff;border-radius:8px;width:30px;height:30px;display:flex;align-items:center;justify-content:center;cursor:pointer;color:#475569}'
    + '#hln-sb nav{flex:1;overflow-y:auto;padding:6px 0 2px;margin:0 -2px}'
    + '#hln-sb a.it{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;color:#334155;text-decoration:none !important;font-size:13.5px !important;font-weight:500 !important;margin:0 0 1px !important}'
    + '#hln-sb a.it:hover{background:#F4F2EC}'
    + '#hln-sb a.it.cur{background:#ECFDF5;color:#127A57;font-weight:600}#hln-sb a.it.cur svg{color:#127A57}'
    + '#hln-sb a.it svg{width:17px;height:17px;flex-shrink:0;color:#475569}'
    + '#hln-sb .n{margin-left:auto;font-size:11px;font-weight:600;padding:1px 7px;border-radius:9px;background:#EEF0F3;color:#334155}'
    + '#hln-sb .ad{margin-left:auto;font-size:9.5px;font-weight:700;background:#F8EFDD;color:#9A6512;border-radius:6px;padding:2px 6px}'
    + '#hln-sb .bt{padding:8px 0 4px;border-top:1px solid #EEF0F3}'
    + '#hln-sb .us{display:flex;align-items:center;gap:10px;padding:10px 4px 12px;border-top:1px solid #EEF0F3}'
    + '#hln-sb .av{width:32px;height:32px;border-radius:50%;background:#C2410C;color:#fff;font-weight:700;font-size:12px;display:flex;align-items:center;justify-content:center;flex-shrink:0}'
    + '#hln-sb .un{font-size:13px !important;font-weight:700 !important;color:#0F172A !important;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}#hln-sb .ur{font-size:11.5px !important;color:#5B6475 !important;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + 'html.hln-pin body{margin-left:' + W + 'px !important}'
    + '#hln-btn{display:none;align-items:center;gap:6px;border:1px solid #E4E7EC;background:#fff;color:#0F172A;border-radius:9px;padding:6px 11px;font:600 13px/1.2 ' + F + ';cursor:pointer;position:fixed;top:10px;left:10px;z-index:99989;box-shadow:0 2px 8px rgba(0,0,0,.12)}'
    + 'html.hln-drawer #hln-btn{display:inline-flex}'
    + '#hln-btn.inbar{position:static;box-shadow:none;margin-right:8px;flex-shrink:0}'
    + 'html.hln-drawer body.hln-float{padding-top:52px !important}'
    + 'html.hln-drawer #hln-sb{transform:translateX(-102%);transition:transform .18s ease;box-shadow:4px 0 24px rgba(0,0,0,.15);width:min(300px,86vw)}'
    + 'html.hln-drawer #hln-sb.on{transform:none}'
    + '#hln-ov{position:fixed;inset:0;background:rgba(14,42,71,.32);z-index:99988;display:none}#hln-ov.on{display:block}'
    + '@media print{#hln-sb,#hln-btn,#hln-ov{display:none !important}html.hln-pin body{margin-left:0 !important}}';
  function el(tag, attrs, html){ var e = document.createElement(tag); for (var k in attrs) e.setAttribute(k, attrs[k]); if (html != null) e.innerHTML = html; return e; }
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function get(u){ return fetch(u, {credentials: 'same-origin'}).then(function(r){ return r.ok ? r.json() : {__status: r.status}; }).catch(function(){ return {}; }); }
  var collapsed = false; try { collapsed = localStorage.getItem('hln_collapsed') === '1'; } catch(e) {}
  function layout(){
    var small = window.innerWidth < BP, drawer = small || collapsed;
    document.documentElement.classList.toggle('hln-drawer', drawer);
    document.documentElement.classList.toggle('hln-pin', !drawer);
    var sb = document.getElementById('hln-sb'); if (sb && !drawer) sb.classList.remove('on');
    var ov = document.getElementById('hln-ov'); if (ov && !drawer) ov.classList.remove('on');
  }
  function freelancerItems(){   // freelance callers have their own small app: Home + My Calls
    return [{id:'sb-home', title:'Home', href:'/?go=home', svg:'<path d="M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10"/>'},
            {id:'sb-mycalls', title:'My Calls', href:'/my-calls', svg:'<path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2"/>'}];
  }
  function build(st, ws){
    var user = st && st.user, role = user ? user.role : '', viewingAs = !!(st && st.viewing_as);
    var fl = role === 'freelancer_sourcer';
    var scoped = !!user && !(role === 'admin' || user.is_company_admin) && !fl && !viewingAs;
    var hidden = (ws && ws.hidden_modules) || [], vocab = (ws && ws.vocab) || {};
    var T = function(k, d){ return vocab[k] || d; };
    var path = location.pathname.replace(/\/$/, '') || '/';
    var items = fl ? freelancerItems() : MENU.items.filter(function(it){
      if (scoped && (it.rs_hide || RS_IDS.indexOf(it.id) >= 0)) return false;
      if (it.id === 'sb-admin' && !(role === 'admin' && !viewingAs)) return false;
      if (hidden.indexOf(it.id.slice(3)) >= 0) return false;
      return true;
    });
    function row(it){
      var title = it.id === 'sb-jobs' ? T('mandate_plural', 'Mandates') : it.id === 'sb-crm' ? T('crm', it.title) : it.title;
      var cur = (it.href.charAt(1) !== '?' && path === it.href) || (ALIAS[path] === it.id);
      return '<a class="it' + (cur ? ' cur' : '') + '" id="hln-' + it.id + '" href="' + esc(it.href) + '"' + (cur ? ' aria-current="page"' : '') + '>'
        + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + it.svg + '</svg>'
        + '<span>' + esc(title) + '</span>' + (it.id === 'sb-admin' ? '<span class="ad">ADMIN</span>' : '<span class="n" style="display:none"></span>') + '</a>';
    }
    var top = items.filter(function(x){ return !x.bottom; }).map(row).join('');
    var bot = items.filter(function(x){ return x.bottom; }).map(row).join('');
    var name = user ? (user.display_name || user.username || '') : '';
    var ini = name.split(/\s+/).filter(Boolean).slice(0, 2).map(function(w){ return w.charAt(0).toUpperCase(); }).join('') || 'H';
    var roleLine = role === 'admin' ? 'Admin' : (user && user.is_company_admin) ? 'Agency admin' : fl ? 'Freelance caller' : 'Recruiter';
    var sb = el('aside', {id: 'hln-sb', 'aria-label': 'Main menu'},
      '<div class="lg"><a class="lb" href="/?go=home" aria-label="Home">HL</a><div style="min-width:0"><div class="lt">HireLab Screener</div><div class="ls">' + esc(MENU.build || 'SCREENER') + '</div></div>'
      + '<button class="cl" type="button" title="Hide the menu" aria-label="Hide the menu"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 4h16v16H4zM9 4v16"/></svg></button></div>'
      + '<nav>' + top + '</nav>' + (bot ? '<div class="bt">' + bot + '</div>' : '')
      + '<div class="us"><div class="av">' + esc(ini) + '</div><div style="min-width:0"><div class="un">' + esc(name) + '</div><div class="ur">' + esc(roleLine) + '</div></div></div>');
    var ov = el('div', {id: 'hln-ov'});
    var btn = el('button', {id: 'hln-btn', type: 'button', 'aria-label': 'Open menu', title: 'Menu'},
      '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg>Menu');
    document.body.appendChild(ov); document.body.appendChild(sb);
    // the Menu button sits at the start of the page's own top bar (so it never covers the title); else it floats
    var bar = document.querySelector('.top') || document.querySelector('header') || document.querySelector('.topbar');
    if (bar && bar.getBoundingClientRect().top < 80) { btn.classList.add('inbar'); bar.insertBefore(btn, bar.firstChild); }
    else { document.body.appendChild(btn); document.body.classList.add('hln-float'); }
    function open(){ sb.classList.add('on'); ov.classList.add('on'); }
    function close(){ sb.classList.remove('on'); ov.classList.remove('on'); }
    btn.onclick = function(){
      if (collapsed && window.innerWidth >= BP) { collapsed = false; try { localStorage.setItem('hln_collapsed', '0'); } catch(e) {} layout(); return; }
      open();
    };
    ov.onclick = close;
    sb.querySelector('.cl').onclick = function(){
      if (window.innerWidth < BP) { close(); return; }
      collapsed = true; try { localStorage.setItem('hln_collapsed', '1'); } catch(e) {} layout();
    };
    document.addEventListener('keydown', function(e){ if (e.key === 'Escape') close(); });
    window.addEventListener('resize', layout);
    layout();
    if (fl) return;
    // Org Map is an add-on: hidden when this workspace does not have it (same check as the app)
    if (document.getElementById('hln-sb-orgmap')) get('/api/org/stats').then(function(r){ if (r && r.__status === 403) { var o = document.getElementById('hln-sb-orgmap'); if (o) o.remove(); } });
    // The same counts as the app's sidebar
    function badge(id, n){ var b = document.querySelector('#hln-' + id + ' .n'); if (b && n) { b.textContent = n > 99 ? '99+' : String(n); b.style.display = ''; } }
    if (document.getElementById('hln-sb-jobs')) get('/api/mandates').then(function(r){ var l = Array.isArray(r) ? r : (r && (r.mandates || r.items)) || [];
      badge('sb-jobs', l.filter(function(m){ return (m.status || 'active') === 'active'; }).length); });
    get('/api/tasks?stale_limit=0').then(function(r){ if (r && r.counts) badge('sb-tasks', (r.counts.overdue || 0) + (r.counts.today || 0)); });
    get('/api/emailbox/counts').then(function(r){ if (r && r.ok) badge('sb-email', r.unread); });
  }
  function start(){
    document.head.appendChild(el('style', {}, css));
    layout();
    Promise.all([get('/api/auth/status'), get('/api/workspace')]).then(function(a){ build(a[0], a[1]); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
"""


@bp.route('/app-nav.js', methods=['GET'])
def nav_js():
    import json
    js = _JS.replace('__MENU__', json.dumps(menu()).replace('</', '<\\/'))
    return Response(js, mimetype='application/javascript', headers={'Cache-Control': 'no-cache'})
