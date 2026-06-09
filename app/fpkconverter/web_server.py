#!/usr/bin/env python3
import os, sys, traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BOOT_LOG = os.path.join(SCRIPT_DIR, 'boot.log')
try:
    with open(BOOT_LOG, 'w') as f:
        f.write(f'BOOTED: {os.path.abspath(__file__)}\n')
        f.write(f'PID: {os.getpid()}\n')
        f.write(f'Python: {sys.version}\n')
except: pass

CRASH_LOG = os.path.join(SCRIPT_DIR, 'crash.log')
def _global_excepthook(etype, value, tb):
    try:
        with open(CRASH_LOG, 'w') as f:
            traceback.print_exception(etype, value, tb, file=f)
    except: pass
    sys.__excepthook__(etype, value, tb)
sys.excepthook = _global_excepthook

PKG_DIR    = os.path.join(SCRIPT_DIR, 'packages')
VAR_DIR    = os.environ.get('TRIM_PKGVAR') or os.path.join(SCRIPT_DIR, 'data')
os.makedirs(VAR_DIR, exist_ok=True)

if os.path.isdir(PKG_DIR) and PKG_DIR not in sys.path:
    sys.path.insert(0, PKG_DIR)

DB_PATH     = os.path.join(VAR_DIR, 'fpk_converter.db')
CONFIG_PATH = os.path.join(VAR_DIR, 'config.json')
CONV_LOG    = os.path.join(VAR_DIR, 'converter.log')

import subprocess, sqlite3, time, json, threading
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)

@app.after_request
def add_headers(r):
    r.headers['X-Content-Type-Options'] = 'nosniff'
    return r

ALLOWED = {'monitor_dir','crf','codec','container','preset','threads','use_gpu','enabled'}
CODECS = ('libx264','libx265')
CONTAINERS = ('mp4','mkv')
PRESETS = ('ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow')
DEFAULT = {'monitor_dir':'','crf':23,'codec':'libx264','container':'mp4',
           'preset':'medium','threads':1,'use_gpu':True,'enabled':False}
cfg = dict(DEFAULT)
proc = None
conv_log_file = None
last_error = ''
proc_lock = threading.Lock()

def load_cfg():
    global cfg
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f: d = json.load(f)
            if isinstance(d, dict): cfg = {**DEFAULT, **{k:v for k,v in d.items() if k in ALLOWED}}
        except: pass

def save_cfg():
    try:
        t = CONFIG_PATH + '.tmp'
        with open(t,'w') as f: json.dump(cfg, f, indent=2)
        os.replace(t, CONFIG_PATH)
    except: pass

def init_db():
    try:
        c = sqlite3.connect(DB_PATH)
        try:
            c.execute('''CREATE TABLE IF NOT EXISTS processed_files(
                id INTEGER PRIMARY KEY AUTOINCREMENT, filepath TEXT UNIQUE NOT NULL,
                file_size INTEGER NOT NULL, processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                success INTEGER DEFAULT 0, saved_size INTEGER DEFAULT 0)''')
            c.commit()
        finally:
            c.close()
    except Exception as e:
        print(f"init_db error: {e}")

load_cfg(); init_db()

def _is_running():
    global proc
    if proc is None:
        return False
    try:
        return proc.poll() is None
    except Exception:
        return False

@app.route('/')
def index(): return render_template_string(HTML, config=cfg, var_dir=VAR_DIR, conv_log=CONV_LOG, db_path=DB_PATH)

@app.route('/api/config', methods=['GET','POST'])
def api_cfg():
    if request.method == 'POST':
        d = request.json
        if not isinstance(d, dict): return jsonify({'success':False}), 400
        for k,v in d.items():
            if k not in ALLOWED: continue
            if k == 'monitor_dir':
                vs = str(v)
                if vs.startswith('/') and '..' not in vs and os.path.normpath(vs) == vs:
                    cfg[k] = vs
            elif k == 'crf':
                try: cfg[k] = max(1, min(51, int(v)))
                except: pass
            elif k == 'codec' and v in CODECS: cfg[k] = v
            elif k == 'container' and v in CONTAINERS: cfg[k] = v
            elif k == 'preset' and v in PRESETS: cfg[k] = v
            elif k == 'threads':
                try: cfg[k] = max(1, min(16, int(v)))
                except: pass
            elif k == 'use_gpu': cfg[k] = bool(v)
        save_cfg()
        return jsonify({'success':True, 'config':cfg})
    return jsonify(cfg)

@app.route('/api/status')
def api_status():
    global last_error
    return jsonify({'running':_is_running(), 'config':cfg, 'error':last_error})

TEMP_DIR = os.path.join(VAR_DIR, 'temp')

@app.route('/api/start', methods=['POST'])
def api_start():
    global proc, last_error, conv_log_file
    # 先在锁外做验证和文件准备
    last_error = ''
    if _is_running():
        return jsonify({'success':False, 'error':'已在运行中'})
    md = cfg.get('monitor_dir','')
    if not md or not md.startswith('/') or '..' in md or os.path.normpath(md) != md:
        last_error = '请先选择监控文件夹'
        return jsonify({'success':False, 'error':last_error})
    if not os.path.isdir(md):
        last_error = f'目录不存在: {md}'
        return jsonify({'success':False, 'error':last_error})
    os.makedirs(TEMP_DIR, exist_ok=True)
    jp = os.path.join(VAR_DIR, 'start_config.json')
    with open(jp,'w') as f: json.dump({
        'db_path':DB_PATH, 'code_dir':SCRIPT_DIR, 'monitor_dir':md,
        'crf':cfg.get('crf',23), 'codec':cfg.get('codec','libx264'),
        'container':cfg.get('container','mp4'), 'preset':cfg.get('preset','medium'),
        'threads':cfg.get('threads',1), 'use_gpu':cfg.get('use_gpu',True),
        'temp_dir': TEMP_DIR, 'max_depth': 3}, f)
    sc = os.path.join(VAR_DIR, 'start_converter.py')
    with open(sc,'w') as f: f.write(
        'import sys,os,json\n'
        'sd=os.path.dirname(os.path.abspath(__file__))\n'
        'jv=os.environ.get("TRIM_PKGVAR") or os.path.join(sd,"..","app","fpkconverter","data")\n'
        'with open(os.path.join(jv,"start_config.json")) as fh:c=json.load(fh)\n'
        'sys.path.insert(0,c["code_dir"])\n'
        'pk=os.path.join(c["code_dir"],"packages")\n'
        'if os.path.isdir(pk):\n'
        '    sys.path.insert(0,pk)\n'
        'from fpk_converter import Database,VideoConverter,FolderScanner\n'
        'db=Database(c["db_path"])\n'
        'td=c.get("temp_dir","")\n'
        'vc=VideoConverter(db,c["crf"],c["codec"],c["container"],c["preset"],c["threads"],c["use_gpu"],temp_dir=td if td else None)\n'
        'FolderScanner(c["monitor_dir"],vc,max_depth=c.get("max_depth",3)).start()')
    os.chmod(sc, 0o755)
    # 只在启动进程和更新状态时持锁，缩小锁范围
    with proc_lock:
        if _is_running():
            return jsonify({'success':False, 'error':'已在运行中'})
        if conv_log_file:
            try: conv_log_file.close()
            except: pass
            conv_log_file = None
        conv_log_file = open(CONV_LOG, 'a')
        proc = subprocess.Popen([sys.executable, sc], cwd=VAR_DIR, start_new_session=True,
                                stdout=conv_log_file, stderr=subprocess.STDOUT)
    # 非阻塞健康检查：3秒后验证
    def _health_check():
        global proc, last_error, conv_log_file
        time.sleep(3)
        with proc_lock:
            if proc and not _is_running():
                last_error = '转码进程启动后立刻退出，请检查日志'
                if conv_log_file:
                    try: conv_log_file.close()
                    except: pass
                    conv_log_file = None
                proc = None
    threading.Thread(target=_health_check, daemon=True).start()
    cfg['enabled'] = True; save_cfg()
    return jsonify({'success':True})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    global proc, last_error, conv_log_file
    with proc_lock:
        last_error = ''
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            try:
                # 确保进程已终止
                if proc.poll() is None:
                    import signal
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            proc = None
        if conv_log_file:
            try: conv_log_file.close()
            except: pass
            conv_log_file = None
        cfg['enabled'] = False; save_cfg()
    return jsonify({'success':True})

@app.route('/api/logs')
def api_logs():
    rows = []
    try:
        db = sqlite3.connect(DB_PATH)
        try:
            rows = db.execute('SELECT * FROM processed_files ORDER BY processed_at DESC LIMIT 100').fetchall()
        finally:
            db.close()
    except: pass
    logs, total = [], 0
    for r in rows:
        logs.append({'id':r[0],'filepath':str(r[1]),'file_size':r[2],
            'file_size_mb':round(r[2]/1048576,2),'processed_at':str(r[3]),
            'success':bool(r[4]),'saved_size':r[5] or 0,
            'saved_size_mb':round((r[5] or 0)/1048576,2)})
        total += r[5] or 0
    return jsonify({'logs':logs,'total_saved_mb':round(total/1048576,2)})

SAFE_ROOTS = ['/vol1','/vol2','/vol3','/vol4','/vol5','/vol6','/vol7','/vol8']

@app.route('/api/browse')
def api_browse():
    p = request.args.get('path', '/')
    if not p.startswith('/') or '..' in p or os.path.normpath(p) != p:
        return jsonify({'error':'Invalid path'}), 400
    # 限制路径深度，防止过深遍历
    if p.count('/') > 10:
        return jsonify({'error':'Path too deep'}), 400
    if p == '/':
        entries = []
        for vol in SAFE_ROOTS:
            try:
                if os.path.isdir(vol):
                    entries.append({'name':os.path.basename(vol), 'path':vol, 'is_dir':True})
                else:
                    # 卷存在但不是目录，也显示出来
                    try:
                        os.lstat(vol)
                        entries.append({'name':os.path.basename(vol), 'path':vol, 'is_dir':True, 'no_access':True})
                    except Exception:
                        pass
            except PermissionError:
                # 权限不足，仍然显示该卷，标记为无权限
                entries.append({'name':os.path.basename(vol), 'path':vol, 'is_dir':True, 'no_access':True})
            except: pass
        return jsonify({'path':'/', 'entries':entries})
    entries = []
    try:
        if os.path.isdir(p):
            for item in sorted(os.listdir(p)):
                if len(entries) >= 500:  # 限制最多返回500条
                    break
                full = os.path.join(p, item)
                try:
                    is_dir = os.path.isdir(full)
                    entries.append({'name':item, 'path':full, 'is_dir':is_dir})
                except PermissionError:
                    entries.append({'name':item, 'path':full, 'is_dir':True, 'no_access':True})
                except: pass
        else:
            return jsonify({'error':'目录不存在或无权限访问'}), 403
    except PermissionError:
        return jsonify({'error':'无权限访问此目录'}), 403
    except Exception as e:
        return jsonify({'error':str(e)}), 500
    return jsonify({'path':p, 'entries':entries})

HTML = '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>视频自动转码工具</title><style>
*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;padding:20px}.c{max-width:1000px;margin:0 auto}.hd{background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;padding:28px 32px;border-radius:12px 12px 0 0}.hd h1{font-size:24px;margin-bottom:6px}.hd p{opacity:.85;font-size:14px}.ct{padding:24px 32px;background:#fff;border-radius:0 0 12px 12px;box-shadow:0 1px 3px rgba(0,0,0,.1)}.s{margin-bottom:28px}.st{font-size:16px;font-weight:600;margin-bottom:14px;color:#1f2937;display:flex;align-items:center;gap:8px}.badge{display:inline-block;padding:3px 12px;border-radius:20px;font-size:12px;font-weight:500}.bg{background:#d1fae5;color:#065f46}.br{background:#fee2e2;color:#991b1b}.bg2{display:flex;gap:10px;margin-top:12px}.btn{padding:10px 20px;border:none;border-radius:6px;font-size:14px;font-weight:500;cursor:pointer}.bt1{background:#4f46e5;color:#fff}.bt2{background:#10b981;color:#fff}.bt3{background:#ef4444;color:#fff}.fg{margin-bottom:14px}label{display:block;margin-bottom:5px;font-weight:500;color:#374151;font-size:13px}input,select{width:100%;padding:9px 12px;border:1px solid #d1d5db;border-radius:6px;font-size:14px;background:#f9fafb}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}.sc{background:#f3f4f6;padding:18px;border-radius:8px;text-align:center}.sv{font-size:28px;font-weight:700;color:#4f46e5}.sl{color:#6b7280;margin-top:4px;font-size:13px}.tc{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #e5e7eb}th{background:#f9fafb;font-weight:600;color:#374151}.suc{color:#059669}.err{color:#dc2626}.errmsg{color:#dc2626;font-size:13px;margin-top:8px;padding:8px 12px;background:#fef2f2;border-radius:6px;display:none}.info{color:#6b7280;font-size:12px;margin-top:6px}</style></head><body><div class="c"><div class="hd">
<h1>视频自动转码工具</h1><p>自动监测文件夹 | 智能转码 | 节省空间</p></div><div class="ct">
<div class="s"><div class="st"><span>状态</span><span id="badge" class="badge br">已停止</span></div>
<div id="errmsg" class="errmsg"></div>
<div class="bg2"><button class="btn bt2" onclick="api('start')">启动</button>
<button class="btn bt3" onclick="api('stop')">停止</button></div></div>
<div class="s"><div class="st">配置</div>
<div class="fg"><label>监控文件夹</label><div style="display:flex;gap:8px"><input type="text" id="monitor_dir" value="{{config.monitor_dir}}" placeholder="点击浏览选择目录"><button class="btn bt1" style="padding:9px 14px;white-space:nowrap" onclick="openBrowser()">浏览</button></div></div>
<div class="fg"><label>CRF (18-28, 越小质量越高)</label><input type="number" id="crf" min="18" max="28" value="{{config.crf}}"></div>
<div class="fg"><label>编码预设 (越慢越省空间)</label><select id="preset">{% for p in ['ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow'] %}<option value="{{p}}" {%if p==config.preset%}selected{%endif%}>{{p}}</option>{%endfor%}</select></div>
<div class="fg"><label>线程数</label><input type="number" id="threads" min="1" max="8" value="{{config.threads}}"></div>
<div class="fg"><label>编码器</label><select id="codec"><option value="libx264" {%if config.codec=="libx264"%}selected{%endif%}>H.264 (兼容性好)</option><option value="libx265" {%if config.codec=="libx265"%}selected{%endif%}>H.265 (更省空间)</option></select></div>
<div class="fg"><label>格式</label><select id="container"><option value="mp4" {%if config.container=="mp4"%}selected{%endif%}>MP4</option><option value="mkv" {%if config.container=="mkv"%}selected{%endif%}>MKV</option></select></div>
<div class="fg"><label><input type="checkbox" id="use_gpu" {%if config.use_gpu%}checked{%endif%}> GPU加速 (QSV)</label></div>
<button class="btn bt1" onclick="saveCfg()">保存配置</button>
<div class="info">日志: {{conv_log}} | 数据库: {{db_path}}</div>
</div>
<div class="s"><div class="st">统计</div><div class="stats"><div class="sc"><div class="sv" id="ts">0</div><div class="sl">节省(MB)</div></div><div class="sc"><div class="sv" id="tc2">0</div><div class="sl">处理文件</div></div></div></div>
<div class="s"><div class="st">转码日志</div><div class="tc"><table><thead><tr><th>文件</th><th>原大小</th><th>节省</th><th>状态</th><th>时间</th></tr></thead><tbody id="tb"></tbody></table></div></div></div></div>
<script>
function api(a){fetch('/api/'+a,{method:'POST'}).then(r=>{if(!r.ok)throw new Error(r.status);return r.json()}).then(d=>{if(d.error){el('errmsg').textContent=d.error;el('errmsg').style.display='block'}else{el('errmsg').style.display='none'}refresh()}).catch(e=>{el('errmsg').textContent='请求失败: '+e.message;el('errmsg').style.display='block'})}
function saveCfg(){let d={monitor_dir:el('monitor_dir').value,crf:parseInt(el('crf').value),preset:el('preset').value,threads:parseInt(el('threads').value),codec:el('codec').value,container:el('container').value,use_gpu:el('use_gpu').checked};fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(r=>{if(!r.ok)throw new Error(r.status);return r.json()}).then(d=>{if(d.success)alert('已保存');else alert('保存失败')}).catch(e=>alert('保存失败: '+e.message))}
async function refresh(){try{let s=await fetch('/api/status').then(r=>{if(!r.ok)throw new Error(r.status);return r.json()});let b=el('badge');b.textContent=s.running?'运行中':'已停止';b.className='badge '+(s.running?'bg':'br');s.config&&(el('monitor_dir').value=s.config.monitor_dir,el('crf').value=s.config.crf,el('preset').value=s.config.preset,el('threads').value=s.config.threads,el('codec').value=s.config.codec,el('container').value=s.config.container,el('use_gpu').checked=s.config.use_gpu!==false);let l=await fetch('/api/logs').then(r=>{if(!r.ok)throw new Error(r.status);return r.json()});el('ts').textContent=l.total_saved_mb;el('tc2').textContent=l.logs.length;let t=el('tb');t.innerHTML='';l.logs.forEach(r=>{let tr=document.createElement('tr');['filepath','file_size_mb','saved_size_mb'].forEach(k=>{let td=document.createElement('td');td.textContent=r[k];tr.appendChild(td)});let sd=document.createElement('td');sd.textContent=r.success?'成功':'失败';sd.className=r.success?'suc':'err';tr.appendChild(sd);let td=document.createElement('td');td.textContent=r.processed_at;tr.appendChild(td);t.appendChild(tr)})}catch(e){console.error('refresh error:',e)}}
function el(id){return document.getElementById(id)}
var browsePath='/';
async function openBrowser(p){if(p)browsePath=p;try{let d=await fetch('/api/browse?path='+encodeURIComponent(browsePath)).then(r=>{if(!r.ok)throw new Error(r.status);return r.json()});if(d.error){alert(d.error);return}let m=el('modal'),lst=el('blist');el('bpath').textContent=d.path;lst.innerHTML='';if(d.path!=='/'){let b=document.createElement('div');b.className='bitem';b.textContent='.. 返回上级';b.onclick=()=>openBrowser(d.path.split('/').slice(0,-1).join('/')||'/');lst.appendChild(b)}d.entries.forEach(e=>{let b=document.createElement('div');b.className='bitem';b.textContent=e.name+(e.is_dir?'/':'');if(e.no_access){b.style.opacity='0.4';b.title='无权限';if(e.is_dir)b.onclick=()=>alert('无权限访问此目录')}else if(e.is_dir){b.onclick=()=>openBrowser(e.path)}else{b.style.opacity='0.5'}lst.appendChild(b)});m.style.display='flex'}catch(e){alert('浏览目录失败: '+e.message)}}
function selectDir(){el('monitor_dir').value=browsePath;el('modal').style.display='none';saveCfg()}
refresh();setInterval(refresh,5000)</script>
<div id="modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:999;align-items:center;justify-content:center"><div style="background:#fff;border-radius:12px;width:90%;max-width:500px;max-height:70vh;display:flex;flex-direction:column"><div style="padding:16px 20px;border-bottom:1px solid #e5e7eb;display:flex;justify-content:space-between;align-items:center"><span id="bpath" style="font-weight:600;font-size:14px">/</span><div><button class="btn bt2" style="padding:6px 14px;font-size:13px" onclick="selectDir()">选择此目录</button><button class="btn bt3" style="padding:6px 14px;font-size:13px;margin-left:6px" onclick="el('modal').style.display='none'">关闭</button></div></div><div id="blist" style="overflow-y:auto;flex:1;padding:8px 12px"></div></div></div>
<style>.bitem{padding:10px 12px;cursor:pointer;border-radius:6px;font-size:14px;color:#1f2937}.bitem:hover{background:#f3f4f6}</style></body></html>'''

if __name__ == '__main__':
    import signal
    def _sigterm(sig, frame):
        global proc, conv_log_file
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass
            proc = None
        if conv_log_file:
            try: conv_log_file.close()
            except: pass
            conv_log_file = None
        sys.exit(0)
    signal.signal(signal.SIGTERM, _sigterm)
    print(f'Starting on http://0.0.0.0:5000 (packages:{PKG_DIR})', flush=True)
    app.run(host='0.0.0.0', port=5000, debug=False)
