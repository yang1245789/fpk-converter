#!/usr/bin/env python3
import os, sys, traceback

# === 启动信号 (在任何 import 之前，验证脚本确实被启动了) ===
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BOOT_LOG = os.path.join(SCRIPT_DIR, 'boot.log')
try:
    with open(BOOT_LOG, 'w') as f:
        f.write(f'BOOTED: {os.path.abspath(__file__)}\n')
        f.write(f'SCRIPT_DIR: {SCRIPT_DIR}\n')
        f.write(f'PID: {os.getpid()}\n')
        f.write(f'Python: {sys.version}\n')
        f.write(f'CWD: {os.getcwd()}\n')
        f.write(f'TRIM_PKG: {os.environ.get("TRIM_PKG","unset")}\n')
        f.write(f'TRIM_PKGVAR: {os.environ.get("TRIM_PKGVAR","unset")}\n')
except:
    pass

# === 崩溃兜底：任何未捕获异常写入 app 目录下的 crash.log ===
CRASH_LOG = os.path.join(SCRIPT_DIR, 'crash.log')
def _global_excepthook(etype, value, tb):
    try:
        with open(CRASH_LOG, 'w') as f:
            f.write(f'CRASH at {__file__}\n')
            f.write(f'Python: {sys.version}\n')
            f.write(f'CWD: {os.getcwd()}\n')
            f.write(f'sys.path: {sys.path[:10]}\n')
            traceback.print_exception(etype, value, tb, file=f)
    except:
        pass
    sys.__excepthook__(etype, value, tb)
sys.excepthook = _global_excepthook

# === 路径自检测 ===
PKG_DIR    = os.path.join(SCRIPT_DIR, 'packages')
VAR_DIR    = os.environ.get('TRIM_PKGVAR') or os.path.join(SCRIPT_DIR, 'data')
os.makedirs(VAR_DIR, exist_ok=True)

if os.path.isdir(PKG_DIR) and PKG_DIR not in sys.path:
    sys.path.insert(0, PKG_DIR)

DB_PATH     = os.path.join(VAR_DIR, 'fpk_converter.db')
CONFIG_PATH = os.path.join(VAR_DIR, 'config.json')

import subprocess, sqlite3, time, json, threading

# === Flask 导入 ===
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
DEFAULT = {'monitor_dir':'/tmp/videos','crf':23,'codec':'libx264','container':'mp4',
           'preset':'medium','threads':1,'use_gpu':True,'enabled':False}
cfg = dict(DEFAULT)
proc = None

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
    c = sqlite3.connect(DB_PATH)
    c.execute('''CREATE TABLE IF NOT EXISTS processed_files(
        id INTEGER PRIMARY KEY AUTOINCREMENT, filepath TEXT UNIQUE NOT NULL,
        file_size INTEGER NOT NULL, processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        success INTEGER DEFAULT 0, saved_size INTEGER DEFAULT 0)''')
    c.commit(); c.close()

load_cfg(); init_db()

@app.route('/')
def index(): return render_template_string(HTML, config=cfg)

@app.route('/api/config', methods=['GET','POST'])
def api_cfg():
    if request.method == 'POST':
        d = request.json
        if not isinstance(d, dict): return jsonify({'success':False}), 400
        for k,v in d.items():
            if k not in ALLOWED: continue
            if k == 'monitor_dir':
                vs = str(v)
                if vs.startswith('/') and '..' not in vs: cfg[k] = vs
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
    running = proc is not None and proc.poll() is None
    return jsonify({'running':running, 'config':cfg})

TEMP_DIR = os.path.join(SCRIPT_DIR, 'temp')

@app.route('/api/start', methods=['POST'])
def api_start():
    global proc
    if proc and proc.poll() is None:
        return jsonify({'success':False, 'error':'Already running'})
    md = cfg.get('monitor_dir','/tmp/videos')
    if not md.startswith('/') or '..' in md:
        return jsonify({'success':False, 'error':'Invalid path'}), 400
    os.makedirs(md, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    jp = os.path.join(VAR_DIR, 'start_config.json')
    with open(jp,'w') as f: json.dump({
        'db_path':DB_PATH, 'code_dir':SCRIPT_DIR, 'monitor_dir':md,
        'crf':cfg.get('crf',23), 'codec':cfg.get('codec','libx264'),
        'container':cfg.get('container','mp4'), 'preset':cfg.get('preset','medium'),
        'threads':cfg.get('threads',1), 'use_gpu':cfg.get('use_gpu',True),
        'temp_dir': TEMP_DIR}, f)
    sc = os.path.join(VAR_DIR, 'start_converter.py')
    with open(sc,'w') as f: f.write(
        'import sys,os,json\nj=os.path.join;sd=os.path.dirname(os.path.abspath(__file__))\n'
        'jv=os.environ.get("TRIM_PKGVAR") or os.path.join(os.path.dirname(os.path.abspath(__file__)),"..","app","fpkconverter","data")\n'
        'with open(j(jv,"start_config.json")) as f:c=json.load(f)\n'
        'sd=c["code_dir"];sys.path.insert(0,sd)\n'
        'pk=j(sd,"packages")\nif os.path.isdir(pk):sys.path.insert(0,pk)\n'
        'from fpk_converter import Database,VideoConverter,FolderMonitor\n'
        'db=Database(c["db_path"])\n'
        'vc=VideoConverter(db,c["crf"],c["codec"],c["container"],c["preset"],c["threads"],c["use_gpu"],temp_dir=c.get("temp_dir",""))\n'
        'FolderMonitor(c["monitor_dir"],vc).start()')
    os.chmod(sc, 0o755)
    proc = subprocess.Popen([sys.executable, sc], cwd=VAR_DIR, start_new_session=True)
    cfg['enabled'] = True; save_cfg()
    return jsonify({'success':True})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    global proc
    if proc:
        try: proc.terminate(); proc.wait(timeout=10)
        except:
            try: proc.kill(); proc.wait(timeout=5)
            except: pass
        proc = None
    cfg['enabled'] = False; save_cfg()
    return jsonify({'success':True})

@app.route('/api/logs')
def api_logs():
    try:
        db = sqlite3.connect(DB_PATH)
        rows = db.execute('SELECT * FROM processed_files ORDER BY processed_at DESC LIMIT 100').fetchall()
        db.close()
    except: rows = []
    logs, total = [], 0
    for r in rows:
        logs.append({'id':r[0],'filepath':str(r[1]),'file_size':r[2],
            'file_size_mb':round(r[2]/1048576,2),'processed_at':str(r[3]),
            'success':bool(r[4]),'saved_size':r[5] or 0,
            'saved_size_mb':round((r[5] or 0)/1048576,2)})
        total += r[5] or 0
    return jsonify({'logs':logs,'total_saved_mb':round(total/1048576,2)})

HTML = '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>视频自动转码工具</title><style>
*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;padding:20px}.c{max-width:1000px;margin:0 auto}.hd{background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;padding:28px 32px;border-radius:12px 12px 0 0}.hd h1{font-size:24px;margin-bottom:6px}.hd p{opacity:.85;font-size:14px}.ct{padding:24px 32px;background:#fff;border-radius:0 0 12px 12px;box-shadow:0 1px 3px rgba(0,0,0,.1)}.s{margin-bottom:28px}.st{font-size:16px;font-weight:600;margin-bottom:14px;color:#1f2937;display:flex;align-items:center;gap:8px}.badge{display:inline-block;padding:3px 12px;border-radius:20px;font-size:12px;font-weight:500}.bg{background:#d1fae5;color:#065f46}.br{background:#fee2e2;color:#991b1b}.bg2{display:flex;gap:10px;margin-top:12px}.btn{padding:10px 20px;border:none;border-radius:6px;font-size:14px;font-weight:500;cursor:pointer}.bt1{background:#4f46e5;color:#fff}.bt2{background:#10b981;color:#fff}.bt3{background:#ef4444;color:#fff}.fg{margin-bottom:14px}label{display:block;margin-bottom:5px;font-weight:500;color:#374151;font-size:13px}input,select{width:100%;padding:9px 12px;border:1px solid #d1d5db;border-radius:6px;font-size:14px;background:#f9fafb}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}.sc{background:#f3f4f6;padding:18px;border-radius:8px;text-align:center}.sv{font-size:28px;font-weight:700;color:#4f46e5}.sl{color:#6b7280;margin-top:4px;font-size:13px}.tc{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #e5e7eb}th{background:#f9fafb;font-weight:600;color:#374151}.suc{color:#059669}.err{color:#dc2626}</style></head><body><div class="c"><div class="hd">
<h1>视频自动转码工具</h1><p>自动监测文件夹 | 智能转码 | 节省空间</p></div><div class="ct">
<div class="s"><div class="st"><span>状态</span><span id="badge" class="badge br">已停止</span></div>
<div class="bg2"><button class="btn bt2" onclick="api('start')">启动</button>
<button class="btn bt3" onclick="api('stop')">停止</button></div></div>
<div class="s"><div class="st">配置</div>
<div class="fg"><label>监控文件夹</label><input type="text" id="monitor_dir" value="{{config.monitor_dir}}"></div>
<div class="fg"><label>CRF (18-28)</label><input type="number" id="crf" min="18" max="28" value="{{config.crf}}"></div>
<div class="fg"><label>编码预设</label><select id="preset">{% for p in ['ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow'] %}<option value="{{p}}" {%if p==config.preset%}selected{%endif%}>{{p}}</option>{%endfor%}</select></div>
<div class="fg"><label>线程</label><input type="number" id="threads" min="1" max="8" value="{{config.threads}}"></div>
<div class="fg"><label>编码器</label><select id="codec"><option value="libx264" {%if config.codec=="libx264"%}selected{%endif%}>H.264</option><option value="libx265" {%if config.codec=="libx265"%}selected{%endif%}>H.265</option></select></div>
<div class="fg"><label>格式</label><select id="container"><option value="mp4" {%if config.container=="mp4"%}selected{%endif%}>MP4</option><option value="mkv" {%if config.container=="mkv"%}selected{%endif%}>MKV</option></select></div>
<div class="fg"><label><input type="checkbox" id="use_gpu" {%if config.use_gpu%}checked{%endif%}> GPU加速</label></div>
<button class="btn bt1" onclick="saveCfg()">保存配置</button></div>
<div class="s"><div class="st">统计</div><div class="stats"><div class="sc"><div class="sv" id="ts">0</div><div class="sl">节省(MB)</div></div><div class="sc"><div class="sv" id="tc2">0</div><div class="sl">处理文件</div></div></div></div>
<div class="s"><div class="st">日志</div><div class="tc"><table><thead><tr><th>文件</th><th>原大小</th><th>节省</th><th>状态</th><th>时间</th></tr></thead><tbody id="tb"></tbody></table></div></div></div></div>
<script>function api(a){fetch('/api/'+a,{method:'POST'}).then(r=>r.json()).then(d=>refresh())}
function saveCfg(){let d={monitor_dir:el('monitor_dir').value,crf:parseInt(el('crf').value),preset:el('preset').value,threads:parseInt(el('threads').value),codec:el('codec').value,container:el('container').value,use_gpu:el('use_gpu').checked};fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(r=>r.json()).then(()=>alert('已保存'))}
async function refresh(){let s=await fetch('/api/status').then(r=>r.json()),b=el('badge');b.textContent=s.running?'运行中':'已停止';b.className='badge '+(s.running?'bg':'br');s.config&&(el('monitor_dir').value=s.config.monitor_dir,el('crf').value=s.config.crf,el('preset').value=s.config.preset,el('threads').value=s.config.threads,el('codec').value=s.config.codec,el('container').value=s.config.container,el('use_gpu').checked=s.config.use_gpu!==false);let l=await fetch('/api/logs').then(r=>r.json());el('ts').textContent=l.total_saved_mb;el('tc2').textContent=l.logs.length;let t=el('tb');t.innerHTML='';l.logs.forEach(r=>{let tr=document.createElement('tr');['filepath','file_size_mb','saved_size_mb'].forEach(k=>{let td=document.createElement('td');td.textContent=r[k];tr.appendChild(td)});let sd=document.createElement('td');sd.textContent=r.success?'成功':'失败';sd.className=r.success?'suc':'err';tr.appendChild(sd);let td=document.createElement('td');td.textContent=r.processed_at;tr.appendChild(td);t.appendChild(tr)})}
function el(id){return document.getElementById(id)}refresh();setInterval(refresh,5000)</script></body></html>'''

if __name__ == '__main__':
    print(f'Starting on http://0.0.0.0:5000 (packages:{PKG_DIR})', flush=True)
    app.run(host='0.0.0.0', port=5000, debug=False)