"""
Backup download — a real, complete backup you can save on your own computer.

Why this exists (root cause, Oct 2026):
  * The app's "Export" button rebuilt the data in the browser by requesting every
    candidate one by one — with thousands of candidates it stalls, and it only
    covered mandates/candidates/history/settings (no Central DB pool, tasks,
    e-mails, invoices, users …).
  * The server export (/api/export) crashes as soon as any candidate has an AI
    matching vector (a binary column): "Object of type bytes is not JSON
    serializable".

What this module does instead (platform admin only):
  GET  /backup                         page with the buttons below
  GET  /api/admin/backup/db            the WHOLE database as one file
                                       (SQLite online-backup snapshot → gzip → download)
  GET  /api/admin/backup/cvs           every CV file as one .zip
  GET  /api/admin/backup/daily/<name>  download one of the automatic daily backups
  GET  /api/admin/backup/info          sizes + free space (for the page)

The snapshot is consistent even while the app is writing (SQLite backup API).
Temporary files are written to the container's temp folder — NOT the 1 GB data
disk — and deleted as soon as the download finishes. Nothing in the live
database is changed.
"""

import datetime
import gzip
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile

from flask import Blueprint, jsonify, redirect, Response, send_file

from modules.shared import _core

bp = Blueprint('backup_download', __name__)

IST = datetime.timedelta(hours=5, minutes=30)
_DAILY_RX = re.compile(r'^hirelab_[A-Za-z0-9_.-]+\.db$')


def _admin_ok():
    core = _core()
    try:
        return bool(core.current_user()) and bool(core.is_admin())
    except Exception:
        return False


def _deny():
    return jsonify({'ok': False, 'error': 'Only the platform admin can download backups.'}), 403


def _stamp():
    return (datetime.datetime.utcnow() + IST).strftime('%Y-%m-%d_%H%M')


def _mb(n):
    return round(n / (1024 * 1024), 1)


def _tmpdir():
    d = os.environ.get('BACKUP_TMP_DIR') or tempfile.gettempdir()
    os.makedirs(d, exist_ok=True)
    _sweep(d)
    return d


def _sweep(d, older_than=1800):
    """Remove our own temp files left by an interrupted download (older than 30 min)."""
    try:
        cutoff = time.time() - older_than
        for f in os.listdir(d):
            if (f.startswith('hirelab_full_') or f.startswith('hirelab_cvs_')) and \
                    os.path.getmtime(os.path.join(d, f)) < cutoff:
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass
    except OSError:
        pass


def _send_and_delete(path, download_name, mimetype):
    resp = send_file(path, as_attachment=True, download_name=download_name, mimetype=mimetype, max_age=0)

    def _cleanup():
        try:
            os.remove(path)
        except OSError:
            pass
    resp.call_on_close(_cleanup)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def _cv_files(core):
    out = []
    cv_dir = core.CV_DIR
    if os.path.isdir(cv_dir):
        for root, _dirs, files in os.walk(cv_dir):
            for f in files:
                p = os.path.join(root, f)
                try:
                    out.append((p, os.path.relpath(p, cv_dir), os.path.getsize(p)))
                except OSError:
                    pass
    return out


@bp.route('/api/admin/backup/info', methods=['GET'])
def info():
    if not _admin_ok():
        return _deny()
    core = _core()
    db = core.DB_PATH
    db_size = os.path.getsize(db) if os.path.exists(db) else 0
    wal = db + '-wal'
    wal_size = os.path.getsize(wal) if os.path.exists(wal) else 0
    cvs = _cv_files(core)
    daily = []
    if os.path.isdir(core.BAK_DIR):
        for f in sorted(os.listdir(core.BAK_DIR), reverse=True):
            if _DAILY_RX.match(f):
                p = os.path.join(core.BAK_DIR, f)
                daily.append({'name': f, 'mb': _mb(os.path.getsize(p)),
                              'at': datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime('%Y-%m-%d %H:%M')})
    tmp = _tmpdir()
    free_tmp = shutil.disk_usage(tmp).free
    try:
        users = sqlite3.connect('file:%s?mode=ro' % db, uri=True).execute('SELECT COUNT(*) FROM users').fetchone()[0]
    except Exception:
        users = None
    return jsonify({'ok': True, 'db_mb': _mb(db_size + wal_size), 'users': users,
                    'cv_count': len(cvs), 'cv_mb': _mb(sum(c[2] for c in cvs)),
                    'daily': daily, 'tmp_free_mb': _mb(free_tmp),
                    'fits': free_tmp > (db_size + wal_size) * 2.2 + 50 * 1024 * 1024})


@bp.route('/api/admin/backup/db', methods=['GET'])
def download_db():
    if not _admin_ok():
        return _deny()
    core = _core()
    tmp = _tmpdir()
    need = (os.path.getsize(core.DB_PATH) if os.path.exists(core.DB_PATH) else 0) * 2.2
    if shutil.disk_usage(tmp).free < need + 50 * 1024 * 1024:
        return jsonify({'ok': False, 'error': 'Not enough temporary space on the server to build the backup. '
                                              'Download the latest automatic daily backup on this page instead.'}), 507
    stamp = _stamp()
    raw = os.path.join(tmp, 'hirelab_full_%s.db' % stamp)
    gz = raw + '.gz'
    try:
        src = sqlite3.connect(core.DB_PATH, timeout=30)
        dst = sqlite3.connect(raw)
        try:
            src.backup(dst)                       # consistent snapshot, safe while the app writes
        finally:
            dst.close()
            src.close()
        chk = sqlite3.connect(raw)
        try:
            ok = chk.execute('PRAGMA quick_check').fetchone()[0]
        finally:
            chk.close()
        if ok != 'ok':
            raise RuntimeError('snapshot failed its integrity check (%s)' % ok)
        with open(raw, 'rb') as fi, gzip.open(gz, 'wb', compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo, 1024 * 1024)
    except Exception as exc:
        for p in (raw, gz):
            try:
                os.remove(p)
            except OSError:
                pass
        return jsonify({'ok': False, 'error': 'Backup failed: %s' % exc}), 500
    finally:
        try:
            os.remove(raw)
        except OSError:
            pass
    try:
        core.log_activity('backup.download', 'Full database backup downloaded (%s MB compressed)' % _mb(os.path.getsize(gz)))
    except Exception:
        pass
    return _send_and_delete(gz, 'hirelab_full_%s.db.gz' % stamp, 'application/gzip')


@bp.route('/api/admin/backup/cvs', methods=['GET'])
def download_cvs():
    if not _admin_ok():
        return _deny()
    core = _core()
    files = _cv_files(core)
    total = sum(f[2] for f in files)
    tmp = _tmpdir()
    if shutil.disk_usage(tmp).free < total * 1.1 + 50 * 1024 * 1024:
        return jsonify({'ok': False, 'error': 'Not enough temporary space on the server to zip the CVs.'}), 507
    path = os.path.join(tmp, 'hirelab_cvs_%s.zip' % _stamp())
    try:
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as z:   # PDFs/DOCX are already compressed
            for full, rel, _s in files:
                z.write(full, 'cvs/' + rel)
    except Exception as exc:
        try:
            os.remove(path)
        except OSError:
            pass
        return jsonify({'ok': False, 'error': 'CV zip failed: %s' % exc}), 500
    return _send_and_delete(path, os.path.basename(path), 'application/zip')


@bp.route('/api/admin/backup/daily/<name>', methods=['GET'])
def download_daily(name):
    if not _admin_ok():
        return _deny()
    core = _core()
    if not _DAILY_RX.match(name or ''):
        return jsonify({'ok': False, 'error': 'Not found'}), 404
    p = os.path.join(core.BAK_DIR, name)
    if not os.path.isfile(p):
        return jsonify({'ok': False, 'error': 'Not found'}), 404
    return send_file(p, as_attachment=True, download_name=name, mimetype='application/octet-stream', max_age=0)


_PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backup · HireLab</title><style>
:root{--ink:#1F2328;--muted:#6B6F76;--line:#E3E1DA;--paper:#F7F6F2;--green:#0F6E56;--navy:#0E2A47;--red:#B3261E}
*{box-sizing:border-box}body{margin:0;font:14px/1.55 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:var(--ink);background:var(--paper)}
.top{display:flex;align-items:center;gap:12px;padding:12px 20px;background:#fff;border-bottom:1px solid var(--line)}
.top a{text-decoration:none;color:var(--muted);font-size:13px}.logo{width:30px;height:30px;border-radius:8px;background:var(--green);color:#fff;font-weight:700;display:flex;align-items:center;justify-content:center}
h1{font-size:17px;margin:0}.wrap{max-width:760px;margin:0 auto;padding:18px 16px 60px}
.card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}
.h{font-size:15px;font-weight:700;color:var(--navy);margin-bottom:4px}.sub{color:var(--muted);font-size:13px}
.btn{display:inline-block;border:none;border-radius:10px;padding:11px 18px;font-weight:700;cursor:pointer;text-decoration:none;font-size:14px}
.pri{background:var(--green);color:#fff}.sec{background:#fff;color:var(--navy);border:1px solid var(--line)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}td{padding:7px 4px;border-bottom:1px solid #EFEDE7}
.err{background:#FBE9E7;color:var(--red);border-radius:10px;padding:10px 12px;margin-top:10px}
.ok{background:#E6F4EE;color:var(--green);border-radius:10px;padding:10px 12px;margin-top:10px}
ol{margin:6px 0 0 18px;padding:0}li{margin:3px 0}code{background:#F2F0EA;border-radius:5px;padding:1px 5px}
</style></head><body>
<div class="top"><a href="/">&#8592; App</a><div class="logo">H</div><h1>Backup</h1></div>
<div class="wrap">
 <div class="card"><div class="h">1 · Full database</div>
  <div class="sub">Everything in one file — all workspaces, jobs, candidates (incl. Central DB), tasks, e-mails, invoices, users and settings. Safe to run while the app is in use.</div>
  <div class="sub" id="dbinfo" style="margin-top:6px">Checking…</div>
  <div class="row"><button class="btn pri" id="dbbtn">⬇ Download full database</button><span class="sub" id="dbmsg"></span></div>
 </div>
 <div class="card"><div class="h">2 · CV files</div>
  <div class="sub">The database stores which CV belongs to whom; the PDF/Word files themselves are separate. Download both for a complete backup.</div>
  <div class="sub" id="cvinfo" style="margin-top:6px"></div>
  <div class="row"><button class="btn sec" id="cvbtn">⬇ Download all CVs (.zip)</button><span class="sub" id="cvmsg"></span></div>
 </div>
 <div class="card"><div class="h">Automatic daily backups on the server</div>
  <div class="sub">The app also keeps a few daily copies on the server disk. You can download any of them.</div>
  <div id="daily"></div></div>
 <div class="card"><div class="h">How to restore (only if something goes wrong)</div>
  <ol class="sub"><li>Fastest: Render dashboard → your service → <b>Disks</b> → restore yesterday's automatic disk snapshot.</li>
  <li>Or: unzip the downloaded <code>.db.gz</code> to get <code>hirelab_full_….db</code>, rename it to <code>hirelab.db</code> and put it in the data folder (<code>/data</code>) while the service is stopped.</li></ol></div>
</div>
<script>
(function(){
 var $=function(i){return document.getElementById(i);};
 var esc=function(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});};
 async function info(){
  var r=await fetch('/api/admin/backup/info',{credentials:'same-origin'}); var j=await r.json().catch(function(){return{};});
  if(!j.ok){$('dbinfo').innerHTML='<div class="err">'+esc(j.error||'Not allowed')+'</div>';$('dbbtn').disabled=true;$('cvbtn').disabled=true;return;}
  $('dbinfo').textContent='Database: '+j.db_mb+' MB'+(j.users!=null?' · '+j.users+' users':'')+' (download is compressed, usually much smaller).';
  if(!j.fits) $('dbinfo').innerHTML+='<div class="err">Not enough temporary space to build a fresh copy — use the latest daily backup below.</div>';
  $('cvinfo').textContent=j.cv_count+' CV files · '+j.cv_mb+' MB';
  $('daily').innerHTML=j.daily.length?'<table>'+j.daily.map(function(d){return '<tr><td>'+esc(d.name)+'</td><td>'+esc(d.at)+'</td><td>'+d.mb+' MB</td><td style="text-align:right"><a href="/api/admin/backup/daily/'+encodeURIComponent(d.name)+'">Download</a></td></tr>';}).join('')+'</table>':'<div class="sub" style="margin-top:8px">No daily backups on disk yet.</div>';
 }
 function dl(url,btn,msg){
  btn.disabled=true; msg.textContent='Preparing the file on the server… your browser will start the download in a moment (a large database can take a minute). Keep this page open.';
  var f=document.createElement('iframe'); f.style.display='none'; f.src=url; document.body.appendChild(f);
  f.onload=function(){ try{ var t=f.contentDocument&&f.contentDocument.body?f.contentDocument.body.innerText:''; if(t){ var j={}; try{j=JSON.parse(t);}catch(e){} msg.innerHTML='<span style="color:#B3261E">'+esc(j.error||t.slice(0,200))+'</span>'; } }catch(e){} btn.disabled=false; };
  setTimeout(function(){ btn.disabled=false; if(msg.textContent.indexOf('Preparing')===0) msg.textContent='The file should now be in your browser\'s downloads. If nothing appeared, click again.'; }, 15000);
 }
 $('dbbtn').onclick=function(){dl('/api/admin/backup/db',$('dbbtn'),$('dbmsg'));};
 $('cvbtn').onclick=function(){dl('/api/admin/backup/cvs',$('cvbtn'),$('cvmsg'));};
 info();
})();
</script></body></html>'''


@bp.route('/backup', methods=['GET'])
def backup_page():
    core = _core()
    if not core.current_user():
        return redirect('/')
    return Response(_PAGE, mimetype='text/html')
