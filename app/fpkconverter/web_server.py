#!/usr/bin/env python3
"""飞牛视频转码 Web 服务 - 零外部依赖，纯 stdlib 实现"""
import os, sys, json, sqlite3, html, shutil, time, subprocess, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# === 环境变量 ===
PKG_DIR = os.environ.get('TRIM_PKG', '/app')
VAR_DIR = os.environ.get('TRIM_PKGVAR', '/var/lib/fpkconverter')
CODE_DIR = os.path.join(PKG_DIR, 'app', 'fpkconverter')
DB_PATH = os.path.join(VAR_DIR, 'fpk_converter.db')
CONFIG_PATH = os.path.join(VAR_DIR, 'config.json')

os.makedirs(VAR_DIR, exist_ok=True)

# === 白名单 ===
ALLOWED_CONFIG_KEYS = {'monitor_dir', 'crf', 'codec', 'container', 'preset', 'threads', 'use_gpu', 'enabled'}
ALLOWED_CODECS = ('libx264', 'libx265')
ALLOWED_CONTAINERS = ('mp4', 'mkv')
ALLOWED_PRESETS = ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow')

DEFAULT_CONFIG = {
    'monitor_dir': '/tmp/videos', 'crf': 23, 'codec': 'libx264',
    'container': 'mp4', 'preset': 'medium', 'threads': 1, 'use_gpu': True, 'enabled': False
}
config = dict(DEFAULT_CONFIG)
converter_process = None
converter_thread = None

# === 配置读写 ===
def load_config():
    global config
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                config = {**DEFAULT_CONFIG, **{k: v for k, v in saved.items() if k in ALLOWED_CONFIG_KEYS}}
        except: pass

def save_config():
    try:
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w') as f: json.dump(config, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except: pass

# === 数据库 ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS processed_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT, filepath TEXT UNIQUE NOT NULL,
        file_size INTEGER NOT NULL, processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        success INTEGER DEFAULT 0, saved_size INTEGER DEFAULT 0)''')
    conn.commit(); conn.close()

# === 转码监控（子进程） ===
def run_converter():
    monitor_dir = config.get('monitor_dir', '/tmp/videos')
    if not monitor_dir.startswith('/') or '..' in monitor_dir:
        return
    os.makedirs(monitor_dir, exist_ok=True)

    cfg_data = {
        'db_path': DB_PATH, 'code_dir': CODE_DIR, 'monitor_dir': monitor_dir,
        'crf': config.get('crf', 23), 'codec': config.get('codec', 'libx264'),
        'container': config.get('container', 'mp4'), 'preset': config.get('preset', 'medium'),
        'threads': config.get('threads', 1), 'use_gpu': config.get('use_gpu', True)
    }
    json_path = os.path.join(VAR_DIR, 'start_config.json')
    with open(json_path, 'w') as f: json.dump(cfg_data, f)

    script = os.path.join(VAR_DIR, 'start_converter.py')
    with open(script, 'w') as f:
        f.write('''import sys,os,json;j=os.path.join;jp=os.environ.get("TRIM_PKGVAR","/var/lib/fpkconverter")
with open(j(jp,"start_config.json")) as f:c=json.load(f)
sys.path.insert(0,c["code_dir"])
from fpk_converter import Database,VideoConverter,FolderMonitor
db=Database(c["db_path"])
vc=VideoConverter(db,c["crf"],c["codec"],c["container"],c["preset"],c["threads"],c["use_gpu"])
FolderMonitor(c["monitor_dir"],vc).start()''')
    os.chmod(script, 0o755)

    global converter_process
    converter_process = subprocess.Popen(
        [sys.executable, script], cwd=VAR_DIR, start_new_session=True)
    converter_process.wait()
    config['enabled'] = False; save_config()

def start_monitor():
    global converter_thread
    if converter_thread and converter_thread.is_alive(): return
    stop_monitor()
    converter_thread = threading.Thread(target=run_converter, daemon=True)
    converter_thread.start()

def stop_monitor():
    global converter_process, converter_thread
    if converter_process:
        try:
            converter_process.terminate()
            converter_process.wait(timeout=10)
        except:
            try: converter_process.kill(); converter_process.wait(timeout=5)
            except: pass
        converter_process = None
    if converter_thread and converter_thread.is_alive():
        converter_thread.join(timeout=2)
    config['enabled'] = False; save_config()

# === HTTP 响应辅助 ===
def json_response(handler, data, status=200):
    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.end_headers()
    handler.wfile.write(body)

def html_response(handler, body, status=200):
    data = body.encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Content-Length', str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)

def bad_request(handler, msg):
    json_response(handler, {'success': False, 'error': msg}, 400)

# === 请求处理器 ===
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # 静默日志

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == '/':
            return self.serve_index()
        elif p.path == '/api/config':
            return json_response(self, config)
        elif p.path == '/api/status':
            running = (converter_process is not None and converter_process.poll() is None)
            return json_response(self, {'running': running, 'config': config})
        elif p.path == '/api/logs':
            return self.serve_logs()
        else:
            json_response(self, {'success': False, 'error': 'Not found'}, 404)

    def do_POST(self):
        p = urlparse(self.path)
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b'{}'
        try: data = json.loads(body) if body else {}
        except: return bad_request(self, 'Invalid JSON')

        if p.path == '/api/config':
            return self.handle_config_update(data)
        elif p.path == '/api/start':
            return self.handle_start()
        elif p.path == '/api/stop':
            return self.handle_stop()
        else:
            json_response(self, {'success': False, 'error': 'Not found'}, 404)

    def serve_index(self):
        html_body = get_index_html()
        html_response(self, html_body)

    def handle_config_update(self, data):
        if not isinstance(data, dict):
            return bad_request(self, 'Invalid format')
        for k, v in data.items():
            if k not in ALLOWED_CONFIG_KEYS: continue
            if k == 'monitor_dir':
                vs = str(v)
                if vs.startswith('/') and '..' not in vs: config[k] = vs
            elif k == 'crf':
                try: config[k] = max(1, min(51, int(v)))
                except: pass
            elif k == 'codec' and v in ALLOWED_CODECS: config[k] = v
            elif k == 'container' and v in ALLOWED_CONTAINERS: config[k] = v
            elif k == 'preset' and v in ALLOWED_PRESETS: config[k] = v
            elif k == 'threads':
                try: config[k] = max(1, min(16, int(v)))
                except: pass
            elif k == 'use_gpu': config[k] = bool(v)
        save_config()
        json_response(self, {'success': True, 'config': config})

    def handle_start(self):
        global converter_process
        if converter_process and converter_process.poll() is None:
            return json_response(self, {'success': False, 'error': 'Already running'})
        config['enabled'] = True; save_config()
        threading.Thread(target=run_converter, daemon=True).start()
        time.sleep(0.5)
        json_response(self, {'success': True})

    def handle_stop(self):
        stop_monitor()
        json_response(self, {'success': True})

    def serve_logs(self):
        try:
            conn = sqlite3.connect(DB_PATH)
            rows = conn.execute(
                'SELECT * FROM processed_files ORDER BY processed_at DESC LIMIT 100').fetchall()
            conn.close()
        except: rows = []

        logs, total = [], 0
        for r in rows:
            logs.append({
                'id': r[0], 'filepath': str(r[1]), 'file_size': r[2],
                'file_size_mb': round(r[2] / 1048576, 2),
                'processed_at': str(r[3]), 'success': bool(r[4]),
                'saved_size': r[5] or 0,
                'saved_size_mb': round((r[5] or 0) / 1048576, 2)
            })
            total += r[5] or 0
        json_response(self, {'logs': logs, 'total_saved_mb': round(total / 1048576, 2)})

# === HTML 页面 ===
def get_index_html():
    return '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>视频自动转码工具</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;padding:20px}
.c{max-width:1000px;margin:0 auto}
.hd{background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;padding:28px 32px;border-radius:12px 12px 0 0}
.hd h1{font-size:24px;margin-bottom:6px}.hd p{opacity:.85;font-size:14px}
.ct{padding:24px 32px;background:#fff;border-radius:0 0 12px 12px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
.s{margin-bottom:28px}.st{font-size:16px;font-weight:600;margin-bottom:14px;color:#1f2937;display:flex;align-items:center;gap:8px}
.badge{display:inline-block;padding:3px 12px;border-radius:20px;font-size:12px;font-weight:500}
.bg{background:#d1fae5;color:#065f46}.br{background:#fee2e2;color:#991b1b}
.bg2{display:flex;gap:10px;margin-top:12px}
.btn{padding:10px 20px;border:none;border-radius:6px;font-size:14px;font-weight:500;cursor:pointer;transition:.15s}
.bt1{background:#4f46e5;color:#fff}.bt1:hover{background:#4338ca}
.bt2{background:#10b981;color:#fff}.bt2:hover{background:#059669}
.bt3{background:#ef4444;color:#fff}.bt3:hover{background:#dc2626}
.fg{margin-bottom:14px}label{display:block;margin-bottom:5px;font-weight:500;color:#374151;font-size:13px}
input[type=text],input[type=number],select{width:100%;padding:9px 12px;border:1px solid #d1d5db;border-radius:6px;font-size:14px;background:#f9fafb}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.sc{background:#f3f4f6;padding:18px;border-radius:8px;text-align:center}
.sv{font-size:28px;font-weight:700;color:#4f46e5}.sl{color:#6b7280;margin-top:4px;font-size:13px}
.tc{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #e5e7eb}
th{background:#f9fafb;font-weight:600;color:#374151}
.suc{color:#059669}.err{color:#dc2626}
</style>
</head>
<body>
<div class="c">
<div class="hd"><h1>视频自动转码工具</h1><p>自动监测文件夹 | 智能转码 | 节省存储空间</p></div>
<div class="ct">
<div class="s">
<div class="st"><span>状态控制</span><span id="badge" class="badge br">已停止</span></div>
<div class="bg2">
<button class="btn bt2" onclick="api('start')">启动服务</button>
<button class="btn bt3" onclick="api('stop')">停止服务</button>
</div>
</div>
<div class="s">
<div class="st">配置</div>
<div class="fg"><label>监控文件夹</label><input type="text" id="monitor_dir" placeholder="/vol1/videos"></div>
<div class="fg"><label>CRF 质量 (18-28)</label><input type="number" id="crf" min="18" max="28" value="23"></div>
<div class="fg"><label>编码预设</label><select id="preset"><option value="ultrafast">ultrafast</option><option value="superfast">superfast</option><option value="veryfast">veryfast</option><option value="faster">faster</option><option value="fast">fast</option><option value="medium" selected>medium</option><option value="slow">slow</option><option value="slower">slower</option><option value="veryslow">veryslow</option></select></div>
<div class="fg"><label>线程数</label><input type="number" id="threads" min="1" max="8" value="1"></div>
<div class="fg"><label>编码器</label><select id="codec"><option value="libx264">H.264</option><option value="libx265">H.265</option></select></div>
<div class="fg"><label>输出格式</label><select id="container"><option value="mp4">MP4</option><option value="mkv">MKV</option></select></div>
<div class="fg"><label><input type="checkbox" id="use_gpu" checked> GPU加速 (Intel QSV)</label></div>
<button class="btn bt1" onclick="saveCfg()">保存配置</button>
</div>
<div class="s">
<div class="st">统计</div>
<div class="stats">
<div class="sc"><div class="sv" id="ts">0</div><div class="sl">节省(MB)</div></div>
<div class="sc"><div class="sv" id="tc2">0</div><div class="sl">处理文件</div></div>
</div>
</div>
<div class="s">
<div class="st">处理日志</div><div class="tc">
<table><thead><tr><th>文件路径</th><th>原大小</th><th>节省</th><th>状态</th><th>时间</th></tr></thead><tbody id="tb"></tbody></table>
</div></div></div></div>
<script>
function api(act){fetch('/api/'+act,{method:'POST'}).then(r=>r.json()).then(d=>{refresh()})}
function saveCfg(){let d={monitor_dir:el('monitor_dir').value,crf:parseInt(el('crf').value),preset:el('preset').value,threads:parseInt(el('threads').value),codec:el('codec').value,container:el('container').value,use_gpu:el('use_gpu').checked};fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(r=>r.json()).then(()=>alert('已保存'))}
async function refresh(){let s=await(await fetch('/api/status')).json(),b=el('badge');b.textContent=s.running?'运行中':'已停止';b.className='badge '+(s.running?'bg':'br');el('monitor_dir').value=s.config.monitor_dir;el('crf').value=s.config.crf;el('preset').value=s.config.preset;el('threads').value=s.config.threads;el('codec').value=s.config.codec;el('container').value=s.config.container;el('use_gpu').checked=s.config.use_gpu!==false;let l=await(await fetch('/api/logs')).json();el('ts').textContent=l.total_saved_mb;el('tc2').textContent=l.logs.length;let t=el('tb');t.innerHTML='';l.logs.forEach(r=>{let tr=document.createElement('tr');['filepath','file_size_mb','saved_size_mb'].forEach(k=>{let td=document.createElement('td');td.textContent=r[k];tr.appendChild(td)});let sd=document.createElement('td');sd.textContent=r.success?'成功':'失败';sd.className=r.success?'suc':'err';tr.appendChild(sd);let td=document.createElement('td');td.textContent=r.processed_at;tr.appendChild(td);t.appendChild(tr)})}
function el(id){return document.getElementById(id)}
refresh();setInterval(refresh,5000);
</script>
</body></html>'''

# === 主入口 ===
if __name__ == '__main__':
    load_config(); init_db()
    addr = ('0.0.0.0', 5000)
    httpd = HTTPServer(addr, Handler)
    print(f'Serving on http://0.0.0.0:5000', flush=True)
    try: httpd.serve_forever()
    except KeyboardInterrupt: pass
    finally: stop_monitor(); httpd.server_close()