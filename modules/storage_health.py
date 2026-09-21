"""
RecruitOS — Storage Health (platform owner only)

Why this exists: the Render disk (DATA_DIR, 1 GB by default) filled up and
SQLite started failing EVERY write with "database or disk is full". This page
shows, with numbers, what is using the disk and lets the owner free space
safely.

  /admin/storage                      HTML page (open in the browser as admin)
  GET  /api/admin/storage             same data as JSON
  POST /api/admin/storage/prune-backups   {"keep": N}  delete older daily backups
  POST /api/admin/storage/vacuum          compact the live DB (only if space allows)

Only files inside DATA_DIR/backups matching hirelab_*.db (and leftover
*.partial files from interrupted copies) can ever be deleted. The live
database, CVs and call recordings are never touched.
"""

import os
import shutil
from pathlib import Path
from flask import Blueprint, request, jsonify, Response

from modules.shared import get_db, current_user, _core, log_activity

bp = Blueprint('storage_health', __name__)

MB = 1024 * 1024


def _is_owner():
    u = current_user()
    return bool(u) and u.get('role') == 'admin'


def _dir_size(path):
    total = 0
    try:
        for e in os.scandir(path):
            try:
                if e.is_dir(follow_symlinks=False):
                    total += _dir_size(e.path)
                else:
                    total += e.stat(follow_symlinks=False).st_size
            except Exception:
                pass
    except Exception:
        pass
    return total


def _backups():
    core = _core()
    out = []
    for p in sorted(Path(core.BAK_DIR).glob('hirelab_*.db'), reverse=True):
        try:
            out.append({'name': p.name, 'mb': round(p.stat().st_size / MB, 1)})
        except Exception:
            pass
    partial = []
    for p in Path(core.BAK_DIR).glob('*.partial'):
        try:
            partial.append({'name': p.name, 'mb': round(p.stat().st_size / MB, 1)})
        except Exception:
            pass
    return out, partial


def _report():
    core = _core()
    data_dir = core.DATA_DIR
    du = shutil.disk_usage(data_dir)
    rep = {
        'data_dir': data_dir,
        'disk': {'total_mb': round(du.total / MB), 'used_mb': round(du.used / MB),
                 'free_mb': round(du.free / MB), 'used_pct': round(du.used * 100 / du.total, 1) if du.total else 0},
    }
    try:
        t = shutil.disk_usage('/tmp')
        rep['tmp'] = {'total_mb': round(t.total / MB), 'free_mb': round(t.free / MB)}
    except Exception:
        pass

    items = []
    for e in os.scandir(data_dir):
        try:
            size = _dir_size(e.path) if e.is_dir(follow_symlinks=False) else e.stat().st_size
            items.append({'name': e.name + ('/' if e.is_dir() else ''), 'mb': round(size / MB, 1)})
        except Exception:
            pass
    items.sort(key=lambda x: -x['mb'])
    rep['top_level'] = items

    rep['backups'], rep['partial_backups'] = _backups()
    rep['backup_status'] = getattr(core, '_BACKUP_STATUS', {})

    conn = get_db()
    try:
        ps = conn.execute('PRAGMA page_size').fetchone()[0]
        pc = conn.execute('PRAGMA page_count').fetchone()[0]
        fl = conn.execute('PRAGMA freelist_count').fetchone()[0]
        rep['db'] = {'size_mb': round(ps * pc / MB, 1), 'free_pages_mb': round(ps * fl / MB, 1)}
        try:
            wal = os.path.getsize(core.DB_PATH + '-wal')
        except Exception:
            wal = 0
        rep['db']['wal_mb'] = round(wal / MB, 1)
        try:
            rows = conn.execute('SELECT name, SUM(pgsize) s FROM dbstat GROUP BY name ORDER BY s DESC LIMIT 15').fetchall()
            rep['db']['largest'] = [{'name': r[0], 'mb': round(r[1] / MB, 1)} for r in rows]
        except Exception:
            rep['db']['largest'] = 'dbstat not available on this server'
        heavy = {}
        for col in ('embedding', 'embedding_text', 'embedding_vec', 'deep_analysis', 'ai_insight_cv',
                    'experience_intelligence', 'career_summary'):
            try:
                v = conn.execute(f'SELECT COALESCE(SUM(LENGTH({col})),0) FROM candidates').fetchone()[0]
                heavy[col] = round((v or 0) / MB, 1)
            except Exception:
                pass
        rep['db']['candidate_columns_mb'] = heavy
    finally:
        conn.close()
    return rep


@bp.route('/api/admin/storage', methods=['GET'])
def storage_json():
    if not _is_owner():
        return jsonify({'error': 'Platform owner only'}), 403
    return jsonify({'ok': True, **_report()})


@bp.route('/api/admin/storage/prune-backups', methods=['POST'])
def prune_backups():
    if not _is_owner():
        return jsonify({'error': 'Platform owner only'}), 403
    d = request.get_json(silent=True) or {}
    try:
        keep = max(1, int(d.get('keep', 2)))
    except Exception:
        keep = 2
    core = _core()
    freed, deleted = 0, []
    for p in Path(core.BAK_DIR).glob('*.partial'):
        try:
            freed += p.stat().st_size; p.unlink(); deleted.append(p.name)
        except Exception:
            pass
    baks = sorted(Path(core.BAK_DIR).glob('hirelab_*.db'), reverse=True)   # newest first
    for p in baks[keep:]:
        try:
            freed += p.stat().st_size; p.unlink(); deleted.append(p.name)
        except Exception as e:
            print(f'[storage] could not delete {p}: {e}')
    try:
        log_activity('storage.prune_backups', f'Deleted {len(deleted)} backup file(s), kept newest {keep}')
    except Exception:
        pass
    return jsonify({'ok': True, 'deleted': deleted, 'freed_mb': round(freed / MB, 1)})


@bp.route('/api/admin/storage/vacuum', methods=['POST'])
def vacuum_db():
    """Rebuild the DB file to give free pages back to the disk. VACUUM needs
    roughly the DB size again in free space while it runs, so refuse unless
    that space is available."""
    if not _is_owner():
        return jsonify({'error': 'Platform owner only'}), 403
    core = _core()
    db_size = os.path.getsize(core.DB_PATH)
    free = shutil.disk_usage(core.DATA_DIR).free
    if free < db_size * 1.2 + 50 * MB:
        return jsonify({'error': f'Not enough free space to compact safely (need ~{round((db_size * 1.2) / MB)} MB free, have {round(free / MB)} MB). Free space first.'}), 400
    before = db_size
    conn = get_db()
    try:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.execute('VACUUM')
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    finally:
        conn.close()
    after = os.path.getsize(core.DB_PATH)
    return jsonify({'ok': True, 'before_mb': round(before / MB, 1), 'after_mb': round(after / MB, 1)})


_PAGE = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Storage health</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#F5F6F8;color:#101828;margin:0;padding:24px}
.wrap{max-width:860px;margin:0 auto}.card{background:#fff;border:1px solid #E4E7EC;border-radius:12px;padding:16px 18px;margin-bottom:14px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:14px;margin:0 0 10px;color:#344054}.muted{color:#667085;font-size:12.5px}
table{width:100%;border-collapse:collapse;font-size:13px}td{padding:6px 4px;border-top:1px solid #F2F4F7}td.r{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:14px;background:#F2F4F7;border-radius:7px;overflow:hidden;margin:8px 0}.bar>div{height:100%}
button{border:none;border-radius:8px;padding:8px 14px;font-weight:600;font-size:13px;cursor:pointer}
.red{background:#B42318;color:#fff}.grey{background:#F2F4F7;color:#344054}select{padding:6px;border-radius:6px;border:1px solid #D0D5DD}
.warn{background:#FEF3F2;border-color:#FECDCA}.ok{color:#0F6E56}
</style></head><body><div class="wrap">
<h1>Storage health</h1><div class="muted">What is using the server disk. Only you (platform owner) can see this page.</div><br>
<div id="out" class="card">Loading…</div></div>
<script>
function f(n){return (n>=1024? (n/1024).toFixed(2)+' GB' : n+' MB');}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function rows(list){return '<table>'+list.map(function(x){return '<tr><td>'+esc(x.name)+'</td><td class="r">'+f(x.mb)+'</td></tr>';}).join('')+'</table>';}
async function load(){
  var r=await fetch('/api/admin/storage',{credentials:'include'}); var d=await r.json();
  var o=document.getElementById('out');
  if(!r.ok){ o.innerHTML='<b>'+esc(d.error||'Error')+'</b>'; return; }
  var pct=d.disk.used_pct, col=pct>90?'#B42318':(pct>75?'#B54708':'#0F6E56');
  var h='<h2>Disk ('+esc(d.data_dir)+')</h2><div class="bar"><div style="width:'+Math.min(pct,100)+'%;background:'+col+'"></div></div>'
    +'<div><b style="color:'+col+'">'+pct+'% used</b> &middot; '+f(d.disk.used_mb)+' of '+f(d.disk.total_mb)+' &middot; <b>'+f(d.disk.free_mb)+' free</b></div>';
  if(d.tmp) h+='<div class="muted" style="margin-top:4px">/tmp: '+f(d.tmp.free_mb)+' free of '+f(d.tmp.total_mb)+'</div>';
  h+='</div><div class="card"><h2>What is using it</h2>'+rows(d.top_level)+'</div>';
  var bsum=d.backups.reduce(function(a,b){return a+b.mb;},0)+d.partial_backups.reduce(function(a,b){return a+b.mb;},0);
  h+='<div class="card'+(bsum>d.disk.total_mb*0.3?' warn':'')+'"><h2>Daily backups &middot; '+d.backups.length+' files &middot; '+f(Math.round(bsum))+'</h2>'
    +rows(d.backups)+(d.partial_backups.length?'<div style="margin-top:6px;color:#B42318;font-size:12.5px">Broken (interrupted) backup files:</div>'+rows(d.partial_backups):'')
    +'<div style="margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">Keep newest <select id="keep"><option>1</option><option selected>2</option><option>3</option></select>'
    +'<button class="red" onclick="prune()">Delete older backups</button><span class="muted">Your live database is never touched.</span></div>';
  if(d.backup_status && d.backup_status.last) h+='<div class="muted" style="margin-top:8px">Last backup run: '+esc(d.backup_status.last)+'</div>';
  h+='</div>';
  if(d.db){
    h+='<div class="card"><h2>Live database &middot; '+f(d.db.size_mb)+'</h2>'
      +'<div class="muted">Write-ahead log: '+f(d.db.wal_mb)+' &middot; reclaimable empty pages: '+f(d.db.free_pages_mb)+'</div>';
    if(Array.isArray(d.db.largest)) h+='<div style="margin-top:8px;font-size:12px;font-weight:700;color:#667085">LARGEST TABLES</div>'+rows(d.db.largest);
    var hc=d.db.candidate_columns_mb||{}; var hl=Object.keys(hc).map(function(k){return {name:'candidates.'+k,mb:hc[k]};}).sort(function(a,b){return b.mb-a.mb;});
    if(hl.length) h+='<div style="margin-top:8px;font-size:12px;font-weight:700;color:#667085">CANDIDATE DATA BY COLUMN</div>'+rows(hl);
    h+='<div style="margin-top:12px"><button class="grey" onclick="vac()">Compact database</button> <span class="muted">Gives empty pages back to the disk. Needs free space about the size of the database.</span></div></div>';
  }
  o.outerHTML='<div id="out"><div class="card">'+h+'</div>';
}
async function prune(){
  var k=document.getElementById('keep').value;
  if(!confirm('Delete all daily backups except the newest '+k+'? The live database is not affected.')) return;
  var r=await fetch('/api/admin/storage/prune-backups',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({keep:+k})});
  var d=await r.json(); alert(r.ok?('Freed '+d.freed_mb+' MB ('+d.deleted.length+' files deleted)'):(d.error||'Failed')); location.reload();
}
async function vac(){
  if(!confirm('Compact the database now? The app may pause for a short while.')) return;
  var r=await fetch('/api/admin/storage/vacuum',{method:'POST',credentials:'include'}); var d=await r.json();
  alert(r.ok?('Database: '+d.before_mb+' MB -> '+d.after_mb+' MB'):(d.error||'Failed')); location.reload();
}
load();
</script></body></html>'''


@bp.route('/admin/storage', methods=['GET'])
def storage_page():
    u = current_user()
    if not u:
        from flask import redirect
        return redirect('/login')
    if u.get('role') != 'admin':
        return Response('Platform owner only', status=403)
    return Response(_PAGE, mimetype='text/html')
