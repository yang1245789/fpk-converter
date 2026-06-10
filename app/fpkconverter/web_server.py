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
# 由 cmd/config_callback 把 TRIM_DATA_ACCESSIBLE_PATHS 写入此文件，
# 让 web 进程在用户变更授权后无需重启即可感知。
ACCESSIBLE_PATHS_FILE = os.path.join(VAR_DIR, 'accessible_paths')
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3

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
# 严格按照飞牛官方规范：应用只能浏览
# 1) TRIM_DATA_ACCESSIBLE_PATHS 中由用户在"应用设置→授权目录"显式授权的目录
# 2) TRIM_DATA_SHARE_PATHS 中由 config/resource:data-share 声明的共享目录
# 这两个变量由 fnOS 在脚本/服务启动时注入；变更时通过 cmd/config_callback 通知。
# 应用从不主动扫描 /、/vol*、/mnt 等系统/挂载目录。
BLOCKED_PATH_PREFIXES = (
    '/bin', '/boot', '/dev', '/etc', '/lib', '/lib64', '/proc', '/root',
    '/run', '/sbin', '/sys', '/usr', '/var'
)
cfg = dict(DEFAULT)
proc = None
conv_log_file = None
last_error = ''
proc_started_at = None
proc_lock = threading.Lock()

def _rotate_log_if_needed(path, max_bytes=None, backup_count=None):
    max_bytes = LOG_MAX_BYTES if max_bytes is None else max_bytes
    backup_count = LOG_BACKUP_COUNT if backup_count is None else backup_count
    try:
        if not path or not os.path.exists(path) or os.path.getsize(path) <= max_bytes:
            return False
        for idx in range(backup_count, 0, -1):
            src = f'{path}.{idx}'
            dst = f'{path}.{idx + 1}'
            if os.path.exists(src):
                if idx >= backup_count:
                    os.remove(src)
                else:
                    os.replace(src, dst)
        os.replace(path, f'{path}.1')
        return True
    except Exception as e:
        try:
            with open(path, 'a') as f:
                f.write(f'\n日志轮转失败: {e}\n')
        except Exception:
            pass
        return False

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

def _split_path_list(s):
    """按官方约定（冒号分隔）解析 TRIM_DATA_* 类路径列表。"""
    if not s:
        return []
    result = []
    for raw in str(s).split(':'):
        item = raw.strip()
        if not item or not item.startswith('/'):
            continue
        n, _ = _normalize_abs_path(item)
        if n and not _is_blocked_system_path(n):
            if n not in result:
                result.append(n)
    return result

def _read_accessible_paths_file():
    """从持久化文件读取（cmd/config_callback 写入）。"""
    if not ACCESSIBLE_PATHS_FILE or not os.path.exists(ACCESSIBLE_PATHS_FILE):
        return []
    try:
        with open(ACCESSIBLE_PATHS_FILE) as f:
            return _split_path_list(f.read().replace('\n', ':'))
    except Exception:
        return []

def get_authorized_roots():
    """返回应用当前可访问的根目录列表，严格按官方规范：
    1. TRIM_DATA_ACCESSIBLE_PATHS（用户在"应用设置→授权目录"中授权的）
    2. TRIM_DATA_SHARE_PATHS（config/resource:data-share 声明的）
    3. 持久化文件（cmd/config_callback 写入）作为运行时 env 不更新的回退
    """
    roots = []
    for src in (
        os.environ.get('TRIM_DATA_ACCESSIBLE_PATHS', ''),
        os.environ.get('TRIM_DATA_SHARE_PATHS', ''),
    ):
        for p in _split_path_list(src):
            if p not in roots:
                roots.append(p)
    for p in _read_accessible_paths_file():
        if p not in roots:
            roots.append(p)
    return roots

def _is_under_authorized_root(path):
    """检查 path 是否位于已授权的根目录之下（含相等）。"""
    if not path:
        return False
    for root in get_authorized_roots():
        if path == root or path.startswith(root.rstrip('/') + os.sep):
            return True
    return False

def _validate_user_directory(path, must_exist=False):
    normalized, err = _normalize_abs_path(path)
    if not normalized:
        return None, err
    if _is_blocked_system_path(normalized):
        return None, f'禁止选择系统目录: {normalized}'
    if not _is_under_authorized_root(normalized):
        return None, (f'目录未授权访问: {normalized}\n'
                      f'请前往"应用设置→授权目录"对该目录授予读写权限')
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

def _kill_stale_converter_processes():
    """清理 web 进程重启后遗留的旧转码子进程，避免旧版本继续写日志。"""
    killed = []
    proc_root = '/proc'
    try:
        current_pid = os.getpid()
        for name in os.listdir(proc_root):
            if not name.isdigit():
                continue
            pid = int(name)
            if pid == current_pid:
                continue
            cmdline_path = os.path.join(proc_root, name, 'cmdline')
            try:
                with open(cmdline_path, 'rb') as f:
                    raw = f.read()
                if not raw:
                    continue
                cmdline = raw.replace(b'\x00', b' ').decode('utf-8', errors='replace')
                if 'start_converter.py' not in cmdline:
                    continue
                if VAR_DIR not in cmdline and 'fpkconverter' not in cmdline:
                    continue
                try:
                    os.killpg(os.getpgid(pid), 15)
                except Exception:
                    try:
                        os.kill(pid, 15)
                    except Exception:
                        continue
                killed.append(pid)
            except Exception:
                continue
        if killed:
            print(f"已清理遗留旧转码进程: {killed}", flush=True)
    except Exception as e:
        print(f"清理遗留旧转码进程失败: {e}", flush=True)
    return killed

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
    state = {'current_file':'', 'current_activity':'', 'last_error':'', 'progress_percent':0}
    error_keywords = ('错误', '失败', '异常', '未找到', 'Traceback', 'PermissionError', 'Error')
    activity_keywords = ('[SERIAL] 开始处理:', '文件已入队，等待文件稳定:', '开始转码:', '视频信息:',
                         'ffmpeg 命令:', 'QSV 转码失败', '转码进度:', '转码完成:', '已替换原文件:')
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
        if clean.startswith('文件已入队，等待文件稳定:'):
            rest = clean.split('文件已入队，等待文件稳定:', 1)[1].strip()
            state['current_file'] = rest.split('，剩余约', 1)[0].strip()
            state['current_activity'] = clean
            state['progress_percent'] = 0
            continue
        if clean.startswith('转码进度:'):
            state['current_activity'] = clean
            try:
                pct = clean.split('转码进度:', 1)[1].split('%', 1)[0].strip()
                state['progress_percent'] = float(pct)
            except Exception:
                pass
            if '当前文件:' in clean:
                state['current_file'] = clean.split('当前文件:', 1)[1].strip()
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
                    'process':process, 'recent_log':lines,
                    'authorized_roots': get_authorized_roots()})

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
import fpk_converter
print(f"视频转码核心版本: {fpk_converter.VERSION}")
fpkc=fpk_converter
db=fpkc.Database(c["db_path"])
td=c.get("temp_dir","")
print(f"temp_dir: {td}")
if td:
    os.makedirs(td,exist_ok=True)
    print(f"temp_dir 可写: {os.access(td, os.W_OK)}")
vc=fpkc.VideoConverter(db,c["crf"],c["codec"],c["container"],c["preset"],c["threads"],c["use_gpu"],temp_dir=td if td else None)
print(f"编码器: {vc.codec}, GPU: {vc.use_gpu}")
ok,msg=vc.preflight_check(c["monitor_dir"])
print(f"启动前功能自检结果: {msg}")
if not ok:
    print("启动前功能自检失败，停止进入正式转码流程")
    sys.exit(2)
print("启动前功能自检通过，进入正式扫描流程")
fpkc.FolderScanner(c["monitor_dir"],vc,max_depth=c.get("max_depth",3)).start()
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
        _kill_stale_converter_processes()
        _rotate_log_if_needed(CONV_LOG)
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
    entries = []
    def add_entry(name, path, is_dir=True, extra=None):
        if any(e.get('path') == path for e in entries):
            return
        item = {'name':name, 'path':path, 'is_dir':is_dir}
        if extra:
            item.update(extra)
        entries.append(item)
    roots = get_authorized_roots()
    if p == '/':
        # 根目录视图：仅展示用户在 fnOS"应用设置→授权目录"中授权的目录
        # 以及通过 config/resource:data-share 声明的共享目录。
        if not roots:
            return jsonify({
                'path': '/',
                'entries': [],
                'message': ('当前应用没有任何已授权的目录。\n'
                            '请前往 fnOS：应用 → 视频转码 → 应用设置 → 授权目录，\n'
                            '为需要监控的目录（例如 /vol3/1000/PORN）授予“读写”权限后再返回。')
            })
        for root in roots:
            try:
                if os.path.exists(root):
                    add_entry(root, root, os.path.isdir(root))
                else:
                    # 已授权但当前不存在（例如卷未挂载），仍展示让用户感知
                    add_entry(root + '（不可访问）', root, True, {'no_access': True})
            except OSError:
                add_entry(root + '（不可访问）', root, True, {'no_access': True})
        return jsonify({'path': '/', 'entries': entries})

    # 非根目录：必须位于已授权根之下
    if _is_blocked_system_path(p):
        return jsonify({'error': f'禁止浏览系统目录: {p}'}), 403
    if not _is_under_authorized_root(p):
        return jsonify({'error': (
            f'目录未授权访问: {p}\n'
            f'请前往“应用设置→授权目录”授予该目录的读写权限。'
        )}), 403
    if p.count('/') > 12:
        return jsonify({'error': 'Path too deep'}), 400
    try:
        items = sorted(os.listdir(p))
    except PermissionError:
        return jsonify({'error': '无权限访问此目录（请在应用设置中授予读写权限）'}), 403
    except FileNotFoundError:
        return jsonify({'error': '目录不存在'}), 404
    except OSError as e:
        return jsonify({'error': str(e)}), 500
    for item in items:
        if len(entries) >= 500:
            break
        full = os.path.join(p, item)
        try:
            is_dir = os.path.isdir(full)
            add_entry(item, full, is_dir)
        except PermissionError:
            add_entry(item, full, True, {'no_access': True})
        except OSError:
            pass
    return jsonify({'path': p, 'entries': entries})

HTML = '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>视频自动转码工具</title><style>
*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;padding:20px}.c{max-width:1000px;margin:0 auto}.hd{background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;padding:28px 32px;border-radius:12px 12px 0 0}.hd h1{font-size:24px;margin-bottom:6px}.hd p{opacity:.85;font-size:14px}.ct{padding:24px 32px;background:#fff;border-radius:0 0 12px 12px;box-shadow:0 1px 3px rgba(0,0,0,.1)}.s{margin-bottom:28px}.st{font-size:16px;font-weight:600;margin-bottom:14px;color:#1f2937;display:flex;align-items:center;gap:8px}.badge{display:inline-block;padding:3px 12px;border-radius:20px;font-size:12px;font-weight:500}.bg{background:#d1fae5;color:#065f46}.br{background:#fee2e2;color:#991b1b}.bg2{display:flex;gap:10px;margin-top:12px}.btn{padding:10px 20px;border:none;border-radius:6px;font-size:14px;font-weight:500;cursor:pointer}.bt1{background:#4f46e5;color:#fff}.bt2{background:#10b981;color:#fff}.bt3{background:#ef4444;color:#fff}.fg{margin-bottom:14px}label{display:block;margin-bottom:5px;font-weight:500;color:#374151;font-size:13px}input,select{width:100%;padding:9px 12px;border:1px solid #d1d5db;border-radius:6px;font-size:14px;background:#f9fafb}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}.sc{background:#f3f4f6;padding:18px;border-radius:8px;text-align:center}.sv{font-size:28px;font-weight:700;color:#4f46e5}.sl{color:#6b7280;margin-top:4px;font-size:13px}.tc{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #e5e7eb}th{background:#f9fafb;font-weight:600;color:#374151}.suc{color:#059669}.err{color:#dc2626}.errmsg{color:#dc2626;font-size:13px;margin-top:8px;padding:8px 12px;background:#fef2f2;border-radius:6px;display:none}.info{color:#6b7280;font-size:12px;margin-top:6px}.process{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-top:12px}.kv{background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:10px}.kl{font-size:12px;color:#6b7280;margin-bottom:4px}.vv{font-size:13px;color:#111827;word-break:break-all}.pwrap{margin-top:12px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:10px}.pbar{height:12px;background:#e5e7eb;border-radius:999px;overflow:hidden;margin-top:6px}.pfill{height:100%;width:0;background:linear-gradient(90deg,#10b981,#4f46e5);transition:width .3s}.ptext{font-size:12px;color:#374151;margin-top:5px}.logbox{background:#111827;color:#d1d5db;border-radius:8px;padding:12px;max-height:260px;overflow:auto;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;white-space:pre-wrap}.errbox{background:#fef2f2;color:#991b1b;border:1px solid #fecaca;border-radius:8px;padding:10px;font-size:13px;white-space:pre-wrap;word-break:break-word}</style></head><body><div class="c"><div class="hd">
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
<div class="pwrap"><div class="kl">当前文件进度</div><div class="pbar"><div class="pfill" id="progress_fill"></div></div><div class="ptext" id="progress_text">0%</div></div>
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
async function refresh(){try{let s=await fetch('/api/status').then(r=>{if(!r.ok)throw new Error(r.status);return r.json()});let b=el('badge');b.textContent=s.running?'运行中':'已停止';b.className='badge '+(s.running?'bg':'br');s.config&&(el('monitor_dir').value=s.config.monitor_dir,el('crf').value=s.config.crf,el('preset').value=s.config.preset,el('threads').value=s.config.threads,el('codec').value=s.config.codec,el('container').value=s.config.container,el('use_gpu').checked=s.config.use_gpu!==false);let p=s.process||{};el('process_pid').textContent=p.pid||'-';el('process_uptime').textContent=p.uptime_seconds!=null?(p.uptime_seconds+' 秒'):'-';el('current_file').textContent=p.current_file||'-';el('current_activity').textContent=p.current_activity||'-';let pct=Math.max(0,Math.min(100,Number(p.progress_percent||0)));el('progress_fill').style.width=pct+'%';el('progress_text').textContent=pct.toFixed(1)+'%';el('last_error_text').textContent=s.error||p.last_error||'无';el('recent_log').textContent=(s.recent_log&&s.recent_log.length)?s.recent_log.join('\\n'):'暂无日志';let l=await fetch('/api/logs').then(r=>{if(!r.ok)throw new Error(r.status);return r.json()});el('ts').textContent=l.total_saved_mb;el('tc2').textContent=l.logs.length;let t=el('tb');t.innerHTML='';l.logs.forEach(r=>{let tr=document.createElement('tr');['filepath','file_size_mb','saved_size_mb'].forEach(k=>{let td=document.createElement('td');td.textContent=r[k];tr.appendChild(td)});let sd=document.createElement('td');sd.textContent=r.success?'成功':'失败';sd.className=r.success?'suc':'err';tr.appendChild(sd);let td=document.createElement('td');td.textContent=r.processed_at;tr.appendChild(td);t.appendChild(tr)})}catch(e){console.error('refresh error:',e)}}
function el(id){return document.getElementById(id)}
var browsePath='/';
async function openBrowser(p){if(p){browsePath=p}else{let v=(el('monitor_dir').value||'').trim();browsePath=v.startsWith('/')?v:'/'}try{let d=await fetch('/api/browse?path='+encodeURIComponent(browsePath)).then(r=>{if(!r.ok)return r.json().then(j=>{throw new Error(j.error||r.status)});return r.json()});if(d.error){alert(d.error);return}let m=el('modal'),lst=el('blist');browsePath=d.path;el('bpath').textContent=d.path;if(el('browser_path_input'))el('browser_path_input').value=d.path;lst.innerHTML='';if(d.message){let tip=document.createElement('div');tip.style.cssText='padding:12px 14px;background:#fef3c7;color:#78350f;border-radius:8px;margin-bottom:10px;white-space:pre-wrap;font-size:13px;line-height:1.5';tip.textContent=d.message;lst.appendChild(tip)}if(d.path!=='/'){let b=document.createElement('div');b.className='bitem';b.textContent='.. 返回上级';b.onclick=()=>openBrowser(d.path.split('/').slice(0,-1).join('/')||'/');lst.appendChild(b)}d.entries.forEach(e=>{let b=document.createElement('div');b.className='bitem';b.textContent=e.name+(e.is_dir?'/':'');if(e.pinned){b.style.fontWeight='600';b.title='已保存的监控目录'}if(e.no_access){b.style.opacity='0.4';b.title='无权限';if(e.is_dir)b.onclick=()=>alert('无权限访问此目录（请在应用设置 → 授权目录中授予读写权限）')}else if(e.is_dir){b.onclick=()=>openBrowser(e.path)}else{b.style.opacity='0.5'}lst.appendChild(b)});m.style.display='flex'}catch(e){alert('浏览目录失败: '+e.message)}}
function openBrowserFromInput(){let p=(el('browser_path_input').value||'').trim();if(!p){alert('请输入完整路径');return}openBrowser(p)}
function selectDir(){el('monitor_dir').value=browsePath;el('modal').style.display='none';saveCfg()}
refresh();setInterval(refresh,5000)</script>
<div id="modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:999;align-items:center;justify-content:center"><div style="background:#fff;border-radius:12px;width:90%;max-width:560px;max-height:70vh;display:flex;flex-direction:column"><div style="padding:16px 20px;border-bottom:1px solid #e5e7eb"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><span id="bpath" style="font-weight:600;font-size:14px;word-break:break-all">/</span><div><button class="btn bt2" style="padding:6px 14px;font-size:13px" onclick="selectDir()">选择此目录</button><button class="btn bt3" style="padding:6px 14px;font-size:13px;margin-left:6px" onclick="el('modal').style.display='none'">关闭</button></div></div><div style="display:flex;gap:8px;margin-top:10px"><input id="browser_path_input" type="text" placeholder="可直接输入授权目录，如 /vol3/1000/PORN"><button class="btn bt1" style="padding:8px 12px;white-space:nowrap" onclick="openBrowserFromInput()">打开路径</button></div></div><div id="blist" style="overflow-y:auto;flex:1;padding:8px 12px"></div></div></div>
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
