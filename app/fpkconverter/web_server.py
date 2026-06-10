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
MIN_CRF = 18
MAX_CRF = 32
MAX_THREADS = 4
BLOCKED_PATH_PREFIXES = (
    '/bin', '/boot', '/dev', '/etc', '/lib', '/lib64', '/proc', '/root',
    '/run', '/sbin', '/sys', '/usr', '/var'
)
VOLUME_SCAN_LIMIT = 32
VOLUME_ENTRY_CANDIDATES = tuple(f'/vol{i}' for i in range(1, VOLUME_SCAN_LIMIT + 1)) + tuple(
    f'/volume{i}' for i in range(1, VOLUME_SCAN_LIMIT + 1)
)
ROOT_BROWSE_CANDIDATES = VOLUME_ENTRY_CANDIDATES + ('/mnt', '/media', '/home', '/share', '/shares', '/tmp')
cfg = dict(DEFAULT)
proc = None
conv_log_file = None
last_error = ''
proc_started_at = None
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

def _normalize_abs_path(path):
    try:
        value = str(path or '').strip()
        if not value or len(value) > 240:
            return None, '路径为空或过长'
        if not value.startswith('/'):
            return None, '路径必须是绝对路径'
        normalized = os.path.normpath(value)
        if normalized != value or '..' in normalized.split(os.sep):
            return None, '路径包含不安全的跳转'
        return normalized, ''
    except Exception:
        return None, '路径格式无效'

def _is_blocked_system_path(path):
    normalized, _ = _normalize_abs_path(path)
    if not normalized:
        return True
    if normalized == '/':
        return True
    return any(normalized == prefix or normalized.startswith(prefix + os.sep)
               for prefix in BLOCKED_PATH_PREFIXES)

def _validate_user_directory(path, must_exist=False):
    normalized, err = _normalize_abs_path(path)
    if not normalized:
        return None, err
    if _is_blocked_system_path(normalized):
        return None, f'禁止选择系统目录: {normalized}'
    if must_exist and not os.path.isdir(normalized):
        return None, f'目录不存在: {normalized}'
    return normalized, ''

def _is_running():
    global proc
    if proc is None:
        return False
    try:
        return proc.poll() is None
    except Exception:
        return False

def _tail_lines(path, max_lines=80, max_bytes=65536):
    try:
        if not os.path.exists(path):
            return []
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read().decode('utf-8', errors='replace')
        return data.splitlines()[-max_lines:]
    except Exception as e:
        return [f'读取日志失败: {e}']

def _parse_process_state(lines):
    state = {'current_file':'', 'current_activity':'', 'last_error':''}
    error_keywords = ('错误', '失败', '异常', '未找到', 'Traceback', 'PermissionError', 'Error')
    activity_keywords = ('[SERIAL] 开始处理:', '开始转码:', '视频信息:', 'ffmpeg 命令:', 'QSV 转码失败', '转码完成:', '已替换原文件:')
    for line in lines:
        clean = line.strip()
        if not clean:
            continue
        if '[SERIAL] 开始处理:' in clean:
            state['current_file'] = clean.split('[SERIAL] 开始处理:', 1)[1].strip()
            state['current_activity'] = clean
            continue
        if clean.startswith('开始转码:'):
            rest = clean.split('开始转码:', 1)[1].strip()
            state['current_file'] = rest.split(' (大小:', 1)[0].strip()
            state['current_activity'] = clean
            continue
        if any(k in clean for k in error_keywords):
            state['last_error'] = clean
            continue
        if any(k in clean for k in activity_keywords):
            state['current_activity'] = clean
    return state

def _process_snapshot():
    running = _is_running()
    lines = _tail_lines(CONV_LOG)
    parsed = _parse_process_state(lines)
    pid = None
    returncode = None
    with proc_lock:
        if proc is not None:
            pid = getattr(proc, 'pid', None)
            try:
                returncode = proc.poll()
            except Exception:
                returncode = None
    uptime = None
    if proc_started_at and running:
        uptime = max(0, int(time.time() - proc_started_at))
    return {
        'running': running,
        'pid': pid,
        'started_at': proc_started_at,
        'uptime_seconds': uptime,
        'returncode': returncode,
        **parsed
    }, lines

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
                vs, err = _validate_user_directory(v, must_exist=False)
                if not vs:
                    return jsonify({'success':False, 'error':err}), 400
                cfg[k] = vs
            elif k == 'crf':
                try: cfg[k] = max(MIN_CRF, min(MAX_CRF, int(v)))
                except: pass
            elif k == 'codec' and v in CODECS: cfg[k] = v
            elif k == 'container' and v in CONTAINERS: cfg[k] = v
            elif k == 'preset' and v in PRESETS: cfg[k] = v
            elif k == 'threads':
                try: cfg[k] = max(1, min(MAX_THREADS, int(v)))
                except: pass
            elif k == 'use_gpu': cfg[k] = bool(v)
        save_cfg()
        return jsonify({'success':True, 'config':cfg})
    return jsonify(cfg)

@app.route('/api/status')
def api_status():
    global last_error
    process, lines = _process_snapshot()
    display_error = last_error or process.get('last_error', '')
    return jsonify({'running':process['running'], 'config':cfg, 'error':display_error,
                    'process':process, 'recent_log':lines})

TEMP_DIR = os.path.join(VAR_DIR, 'temp')

@app.route('/api/start', methods=['POST'])
def api_start():
    global proc, last_error, conv_log_file, proc_started_at
    # 先在锁外做验证和文件准备
    last_error = ''
    if _is_running():
        return jsonify({'success':False, 'error':'已在运行中'})
    md = cfg.get('monitor_dir','')
    if not md:
        last_error = '请先选择监控文件夹'
        return jsonify({'success':False, 'error':last_error})
    md, err = _validate_user_directory(md, must_exist=True)
    if not md:
        last_error = err
        return jsonify({'success':False, 'error':last_error})
    os.makedirs(TEMP_DIR, exist_ok=True)
    jp = os.path.join(VAR_DIR, 'start_config.json')
    with open(jp,'w') as f: json.dump({
        'db_path':DB_PATH, 'code_dir':SCRIPT_DIR, 'monitor_dir':md,
        'crf':cfg.get('crf',23), 'codec':cfg.get('codec','libx264'),
        'container':cfg.get('container','mp4'), 'preset':cfg.get('preset','medium'),
        'threads':cfg.get('threads',1), 'use_gpu':cfg.get('use_gpu',True),
        'temp_dir': TEMP_DIR, 'max_depth': 5}, f)
    sc = os.path.join(VAR_DIR, 'start_converter.py')
    script_content = '''import sys,os,json,subprocess,traceback
sd=os.path.dirname(os.path.abspath(__file__))
print("=== 转码进程启动 ===")
print(f"Python: {sys.executable}")
print(f"PID: {os.getpid()}")
print(f"CWD: {os.getcwd()}")
print(f"PATH: {os.environ.get('PATH', 'N/A')}")
# 检查 ffmpeg
try:
    r=subprocess.run(["ffmpeg","-version"],capture_output=True,text=True,timeout=10)
    print(f"ffmpeg: 可用 (返回码 {r.returncode})")
    if r.stdout:
        first_line=r.stdout.strip().split("\\n")[0]
        print(f"ffmpeg 版本: {first_line}")
except FileNotFoundError:
    print("ffmpeg: 未找到! 请确认已安装")
except Exception as e:
    print(f"ffmpeg 检查失败: {e}")
# 检查 ffprobe
try:
    r=subprocess.run(["ffprobe","-version"],capture_output=True,text=True,timeout=10)
    print("ffprobe: 可用")
except FileNotFoundError:
    print("ffprobe: 未找到!")
except Exception as e:
    print(f"ffprobe 检查失败: {e}")
jv=os.environ.get("TRIM_PKGVAR") or os.path.join(sd,"..","app","fpkconverter","data")
with open(os.path.join(jv,"start_config.json")) as fh:c=json.load(fh)
print(f"配置: {json.dumps(c, indent=2)}")
sys.path.insert(0,c["code_dir"])
pk=os.path.join(c["code_dir"],"packages")
if os.path.isdir(pk):
    sys.path.insert(0,pk)
from fpk_converter import Database,VideoConverter,FolderScanner
db=Database(c["db_path"])
td=c.get("temp_dir","")
print(f"temp_dir: {td}")
if td:
    os.makedirs(td,exist_ok=True)
    print(f"temp_dir 可写: {os.access(td, os.W_OK)}")
vc=VideoConverter(db,c["crf"],c["codec"],c["container"],c["preset"],c["threads"],c["use_gpu"],temp_dir=td if td else None)
print(f"编码器: {vc.codec}, GPU: {vc.use_gpu}")
FolderScanner(c["monitor_dir"],vc,max_depth=c.get("max_depth",3)).start()
'''
    with open(sc, 'w') as f:
        f.write(script_content)
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
        # fnOS 中 sys.executable 可能为空，回退到 python3；使用无缓冲模式确保日志实时写入
        py = sys.executable if sys.executable else 'python3'
        env = dict(os.environ)
        env['PYTHONUNBUFFERED'] = '1'
        proc = subprocess.Popen([py, '-u', sc], cwd=VAR_DIR, start_new_session=True,
                                stdout=conv_log_file, stderr=subprocess.STDOUT, env=env)
        proc_started_at = int(time.time())
    # 非阻塞健康检查：3秒后验证
    def _health_check():
        global proc, last_error, conv_log_file, proc_started_at
        time.sleep(3)
        with proc_lock:
            if proc and not _is_running():
                last_error = '转码进程启动后立刻退出，请检查日志'
                if conv_log_file:
                    try: conv_log_file.close()
                    except: pass
                    conv_log_file = None
                proc = None
                proc_started_at = None
    threading.Thread(target=_health_check, daemon=True).start()
    cfg['enabled'] = True; save_cfg()
    return jsonify({'success':True})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    global proc, last_error, conv_log_file, proc_started_at
    with proc_lock:
        last_error = ''
        if proc:
            try:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
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
            proc_started_at = None
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

@app.route('/api/browse')
def api_browse():
    p = request.args.get('path', '/')
    normalized, err = _normalize_abs_path(p)
    if not normalized:
        return jsonify({'error':err}), 400
    p = normalized
    if p != '/' and _is_blocked_system_path(p):
        return jsonify({'error':f'禁止浏览系统目录: {p}'}), 403
    # 限制路径深度，防止过深遍历
    if p.count('/') > 10:
        return jsonify({'error':'Path too deep'}), 400
    entries = []
    try:
        if p == '/':
            # 根目录只暴露常见媒体/挂载入口，避免用户误选系统目录。
            for full in ROOT_BROWSE_CANDIDATES:
                if _is_blocked_system_path(full):
                    continue
                if os.path.exists(full):
                    entries.append({'name':os.path.basename(full) or full, 'path':full, 'is_dir':os.path.isdir(full)})
        else:
            # 非根目录：先尝试 listdir，权限不足时尝试 isdir 回退
            try:
                items = sorted(os.listdir(p))
                for item in items:
                    if len(entries) >= 500:
                        break
                    full = os.path.join(p, item)
                    try:
                        is_dir = os.path.isdir(full)
                        entries.append({'name':item, 'path':full, 'is_dir':is_dir})
                    except PermissionError:
                        entries.append({'name':item, 'path':full, 'is_dir':True, 'no_access':True})
                    except OSError:
                        pass
            except PermissionError:
                # listdir 失败但路径可能是一个可访问的目录（如挂载点根）
                # 尝试用 isdir 确认，如果是目录则返回空列表（允许进入）
                try:
                    if os.path.isdir(p):
                        entries = []
                    else:
                        return jsonify({'error':'无权限访问此目录'}), 403
                except Exception:
                    return jsonify({'error':'无权限访问此目录'}), 403
    except PermissionError:
        return jsonify({'error':'无权限访问此目录'}), 403
    except Exception as e:
        return jsonify({'error':str(e)}), 500
    return jsonify({'path':p, 'entries':entries})

HTML = '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>视频自动转码工具</title><style>
*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;padding:20px}.c{max-width:1000px;margin:0 auto}.hd{background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;padding:28px 32px;border-radius:12px 12px 0 0}.hd h1{font-size:24px;margin-bottom:6px}.hd p{opacity:.85;font-size:14px}.ct{padding:24px 32px;background:#fff;border-radius:0 0 12px 12px;box-shadow:0 1px 3px rgba(0,0,0,.1)}.s{margin-bottom:28px}.st{font-size:16px;font-weight:600;margin-bottom:14px;color:#1f2937;display:flex;align-items:center;gap:8px}.badge{display:inline-block;padding:3px 12px;border-radius:20px;font-size:12px;font-weight:500}.bg{background:#d1fae5;color:#065f46}.br{background:#fee2e2;color:#991b1b}.bg2{display:flex;gap:10px;margin-top:12px}.btn{padding:10px 20px;border:none;border-radius:6px;font-size:14px;font-weight:500;cursor:pointer}.bt1{background:#4f46e5;color:#fff}.bt2{background:#10b981;color:#fff}.bt3{background:#ef4444;color:#fff}.fg{margin-bottom:14px}label{display:block;margin-bottom:5px;font-weight:500;color:#374151;font-size:13px}input,select{width:100%;padding:9px 12px;border:1px solid #d1d5db;border-radius:6px;font-size:14px;background:#f9fafb}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}.sc{background:#f3f4f6;padding:18px;border-radius:8px;text-align:center}.sv{font-size:28px;font-weight:700;color:#4f46e5}.sl{color:#6b7280;margin-top:4px;font-size:13px}.tc{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #e5e7eb}th{background:#f9fafb;font-weight:600;color:#374151}.suc{color:#059669}.err{color:#dc2626}.errmsg{color:#dc2626;font-size:13px;margin-top:8px;padding:8px 12px;background:#fef2f2;border-radius:6px;display:none}.info{color:#6b7280;font-size:12px;margin-top:6px}.process{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-top:12px}.kv{background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:10px}.kl{font-size:12px;color:#6b7280;margin-bottom:4px}.vv{font-size:13px;color:#111827;word-break:break-all}.logbox{background:#111827;color:#d1d5db;border-radius:8px;padding:12px;max-height:260px;overflow:auto;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;white-space:pre-wrap}.errbox{background:#fef2f2;color:#991b1b;border:1px solid #fecaca;border-radius:8px;padding:10px;font-size:13px;white-space:pre-wrap;word-break:break-word}</style></head><body><div class="c"><div class="hd">
<h1>视频自动转码工具</h1><p>自动监测文件夹 | 智能转码 | 节省空间</p></div><div class="ct">
<div class="s"><div class="st"><span>状态</span><span id="badge" class="badge br">已停止</span></div>
<div id="errmsg" class="errmsg"></div>
<div class="bg2"><button class="btn bt2" onclick="api('start')">启动</button>
<button class="btn bt3" onclick="api('stop')">停止</button></div></div>
<div class="s"><div class="st">转码进程</div>
<div class="process">
<div class="kv"><div class="kl">PID</div><div class="vv" id="process_pid">-</div></div>
<div class="kv"><div class="kl">运行时长</div><div class="vv" id="process_uptime">-</div></div>
<div class="kv"><div class="kl">当前文件</div><div class="vv" id="current_file">-</div></div>
<div class="kv"><div class="kl">当前状态</div><div class="vv" id="current_activity">-</div></div>
</div>
<div style="margin-top:12px"><div class="kl">最近错误</div><div class="errbox" id="last_error_text">无</div></div>
<div style="margin-top:12px"><div class="kl">最近转码输出</div><pre class="logbox" id="recent_log">暂无日志</pre></div>
</div>
<div class="s"><div class="st">配置</div>
<div class="fg"><label>监控文件夹</label><div style="display:flex;gap:8px"><input type="text" id="monitor_dir" value="{{config.monitor_dir}}" placeholder="点击浏览选择目录"><button class="btn bt1" style="padding:9px 14px;white-space:nowrap" onclick="openBrowser()">浏览</button></div></div>
<div class="fg"><label>CRF (18-32, 越小质量越高；已限制最低 18 防止过载)</label><input type="number" id="crf" min="18" max="32" value="{{config.crf}}"></div>
<div class="fg"><label>编码预设 (越慢越省空间)</label><select id="preset">{% for p in ['ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow'] %}<option value="{{p}}" {%if p==config.preset%}selected{%endif%}>{{p}}</option>{%endfor%}</select></div>
<div class="fg"><label>线程数 (最多 4，保护系统负载)</label><input type="number" id="threads" min="1" max="4" value="{{config.threads}}"></div>
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
async function refresh(){try{let s=await fetch('/api/status').then(r=>{if(!r.ok)throw new Error(r.status);return r.json()});let b=el('badge');b.textContent=s.running?'运行中':'已停止';b.className='badge '+(s.running?'bg':'br');s.config&&(el('monitor_dir').value=s.config.monitor_dir,el('crf').value=s.config.crf,el('preset').value=s.config.preset,el('threads').value=s.config.threads,el('codec').value=s.config.codec,el('container').value=s.config.container,el('use_gpu').checked=s.config.use_gpu!==false);let p=s.process||{};el('process_pid').textContent=p.pid||'-';el('process_uptime').textContent=p.uptime_seconds!=null?(p.uptime_seconds+' 秒'):'-';el('current_file').textContent=p.current_file||'-';el('current_activity').textContent=p.current_activity||'-';el('last_error_text').textContent=s.error||p.last_error||'无';el('recent_log').textContent=(s.recent_log&&s.recent_log.length)?s.recent_log.join('\\n'):'暂无日志';let l=await fetch('/api/logs').then(r=>{if(!r.ok)throw new Error(r.status);return r.json()});el('ts').textContent=l.total_saved_mb;el('tc2').textContent=l.logs.length;let t=el('tb');t.innerHTML='';l.logs.forEach(r=>{let tr=document.createElement('tr');['filepath','file_size_mb','saved_size_mb'].forEach(k=>{let td=document.createElement('td');td.textContent=r[k];tr.appendChild(td)});let sd=document.createElement('td');sd.textContent=r.success?'成功':'失败';sd.className=r.success?'suc':'err';tr.appendChild(sd);let td=document.createElement('td');td.textContent=r.processed_at;tr.appendChild(td);t.appendChild(tr)})}catch(e){console.error('refresh error:',e)}}
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
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
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
