#!/usr/bin/env python3
import os
import sys
import subprocess
import sqlite3
import html
import shutil
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory
import threading
import json

app = Flask(__name__)

# 安全响应头
@app.after_request
def add_security_headers(response):
    """添加安全响应头"""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response

# 错误处理
@app.errorhandler(404)
def not_found(error):
    return jsonify({'success': False, 'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'success': False, 'error': 'Internal server error'}), 500

# Get environment variables
PKG_DIR = os.environ.get('TRIM_PKG', '/app')
VAR_DIR = os.environ.get('TRIM_PKGVAR', '/var/lib/fpk-converter')
CODE_DIR = os.path.join(PKG_DIR, 'app', 'code')
DB_PATH = os.path.join(VAR_DIR, 'fpk_converter.db')
CONFIG_PATH = os.path.join(VAR_DIR, 'config.json')

# Ensure VAR_DIR exists
os.makedirs(VAR_DIR, exist_ok=True)

# 允许的配置项白名单
ALLOWED_CONFIG_KEYS = {
    'monitor_dir', 'crf', 'codec', 'container', 'preset', 'threads', 'use_gpu', 'enabled'
}

# 允许的值白名单
ALLOWED_CODECS = {'libx264', 'libx265'}
ALLOWED_CONTAINERS = {'mp4', 'mkv'}
ALLOWED_PRESETS = {'ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'}

# Default config
default_config = {
    'monitor_dir': '/tmp/videos',
    'crf': 23,
    'codec': 'libx264',
    'container': 'mp4',
    'preset': 'medium',
    'threads': 1,
    'use_gpu': True,
    'enabled': False
}

# Load or create config
def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                saved = json.load(f)
            if not isinstance(saved, dict):
                print("配置文件格式错误，使用默认配置")
                return default_config.copy()
            # 只保留白名单内的键
            validated = {**default_config, **{k: v for k, v in saved.items() if k in ALLOWED_CONFIG_KEYS}}
            return validated
        except json.JSONDecodeError as e:
            print(f"配置文件 JSON 解析失败: {e}，使用默认配置")
            try:
                backup_path = CONFIG_PATH + f".corrupted.{int(time.time())}"
                shutil.copy2(CONFIG_PATH, backup_path)
                print(f"已备份损坏配置到: {backup_path}")
            except Exception:
                pass
            return default_config.copy()
        except Exception as e:
            print(f"加载配置失败: {e}，使用默认配置")
            return default_config.copy()
    return default_config.copy()

def save_config(config):
    try:
        tmp_path = CONFIG_PATH + '.tmp'
        with open(tmp_path, 'w') as f:
            json.dump(config, f, indent=2)
        os.replace(tmp_path, CONFIG_PATH)
    except Exception as e:
        print(f"保存配置失败: {e}")

def validate_config(new_config):
    """校验配置项的安全性"""
    validated = {}
    for key, value in new_config.items():
        if key not in ALLOWED_CONFIG_KEYS:
            continue
        
        if key == 'monitor_dir':
            # 路径校验：只允许绝对路径，禁止路径遍历
            path_str = str(value)
            if not path_str.startswith('/'):
                continue
            if '..' in path_str:
                continue
            validated[key] = path_str
        
        elif key == 'crf':
            # CRF 范围 1-51
            try:
                validated[key] = max(1, min(51, int(value)))
            except (ValueError, TypeError):
                continue
        
        elif key == 'codec':
            if value in ALLOWED_CODECS:
                validated[key] = value
        
        elif key == 'container':
            if value in ALLOWED_CONTAINERS:
                validated[key] = value
        
        elif key == 'preset':
            if value in ALLOWED_PRESETS:
                validated[key] = value
        
        elif key == 'threads':
            try:
                validated[key] = max(1, min(16, int(value)))
            except (ValueError, TypeError):
                continue
        
        elif key == 'use_gpu':
            validated[key] = bool(value)
        
        elif key == 'enabled':
            validated[key] = bool(value)
    
    return validated

config = load_config()
converter_process = None

# Initialize database
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS processed_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filepath TEXT UNIQUE NOT NULL,
            file_size INTEGER NOT NULL,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            success INTEGER DEFAULT 0,
            saved_size INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

init_db()

@app.route('/')
def index():
    return render_template('index.html', config=config)

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    global config
    if request.method == 'POST':
        new_config = request.json
        if not isinstance(new_config, dict):
            return jsonify({'success': False, 'error': 'Invalid config format'}), 400
        validated = validate_config(new_config)
        config.update(validated)
        save_config(config)
        return jsonify({'success': True, 'config': config})
    return jsonify(config)

@app.route('/api/status')
def api_status():
    global converter_process
    running = converter_process is not None and converter_process.poll() is None
    return jsonify({
        'running': running,
        'config': config
    })

@app.route('/api/start', methods=['POST'])
def api_start():
    global converter_process
    if converter_process and converter_process.poll() is None:
        return jsonify({'success': False, 'error': 'Already running'})
    
    monitor_dir = config.get('monitor_dir', '/tmp/videos')
    # 安全校验路径
    if not monitor_dir.startswith('/') or '..' in monitor_dir:
        return jsonify({'success': False, 'error': 'Invalid monitor directory'}), 400
    
    os.makedirs(monitor_dir, exist_ok=True)
    
    # 使用 JSON 传递配置，避免代码注入
    config_data = {
        'db_path': os.path.join(VAR_DIR, 'fpk_converter.db'),
        'code_dir': CODE_DIR,
        'monitor_dir': monitor_dir,
        'crf': config.get('crf', 23),
        'codec': config.get('codec', 'libx264'),
        'container': config.get('container', 'mp4'),
        'preset': config.get('preset', 'medium'),
        'threads': config.get('threads', 1),
        'use_gpu': config.get('use_gpu', True)
    }
    
    config_json_path = os.path.join(VAR_DIR, 'start_config.json')
    with open(config_json_path, 'w') as f:
        json.dump(config_data, f)
    
    # 启动脚本通过读取 JSON 配置来启动，避免 f-string 注入
    startup_script = os.path.join(VAR_DIR, 'start_converter.py')
    with open(startup_script, 'w') as f:
        f.write('''#!/usr/bin/env python3
import sys
import os
import json

# 读取配置
config_path = os.path.join(os.environ.get('TRIM_PKGVAR', '/var/lib/fpk-converter'), 'start_config.json')
with open(config_path, 'r') as f:
    cfg = json.load(f)

# 添加代码目录到路径
sys.path.insert(0, cfg['code_dir'])

from fpk_converter import Database, VideoConverter, FolderMonitor

if __name__ == '__main__':
    db = Database(cfg['db_path'])
    converter = VideoConverter(
        db,
        target_quality=cfg['crf'],
        codec=cfg['codec'],
        container=cfg['container'],
        preset=cfg['preset'],
        threads=cfg['threads'],
        use_gpu=cfg['use_gpu']
    )
    monitor = FolderMonitor(cfg['monitor_dir'], converter)
    monitor.start()
''')
    os.chmod(startup_script, 0o755)
    
    cmd = [
        os.path.join(CODE_DIR, 'venv/bin/python'),
        startup_script
    ]
    
    # 使用 start_new_session=True 隔离进程组，确保子进程能被完全终止
    converter_process = subprocess.Popen(cmd, cwd=VAR_DIR, start_new_session=True)
    config['enabled'] = True
    save_config(config)
    return jsonify({'success': True})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    global converter_process
    if converter_process:
        converter_process.terminate()
        try:
            converter_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            converter_process.kill()
            converter_process.wait(timeout=5)
        converter_process = None
    config['enabled'] = False
    save_config(config)
    return jsonify({'success': True})

@app.route('/api/logs')
def api_logs():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM processed_files ORDER BY processed_at DESC LIMIT 100')
    rows = cursor.fetchall()
    logs = []
    total_saved = 0
    for row in rows:
        log = {
            'id': row[0],
            'filepath': html.escape(str(row[1])),  # XSS 防护
            'file_size': row[2],
            'file_size_mb': round(row[2] / (1024 * 1024), 2),
            'processed_at': html.escape(str(row[3])),  # XSS 防护
            'success': bool(row[4]),
            'saved_size': row[5],
            'saved_size_mb': round(row[5] / (1024 * 1024), 2) if row[5] else 0
        }
        logs.append(log)
        total_saved += row[5] if row[5] else 0
    conn.close()
    return jsonify({
        'logs': logs,
        'total_saved_mb': round(total_saved / (1024 * 1024), 2)
    })

if __name__ == '__main__':
    # Create templates directory
    templates_dir = os.path.join(os.path.dirname(__file__), 'templates')
    os.makedirs(templates_dir, exist_ok=True)
    
    # Create index.html template
    with open(os.path.join(templates_dir, 'index.html'), 'w') as f:
        f.write('''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>视频自动转码工具</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); overflow: hidden; }
        .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; }
        .header h1 { font-size: 28px; margin-bottom: 8px; }
        .header p { opacity: 0.9; }
        .content { padding: 30px; }
        .section { margin-bottom: 30px; }
        .section-title { font-size: 18px; font-weight: 600; margin-bottom: 15px; color: #333; }
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 6px; font-weight: 500; color: #555; }
        input[type="text"], input[type="number"], select { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
        .btn { padding: 10px 20px; border: none; border-radius: 4px; font-size: 14px; font-weight: 500; cursor: pointer; transition: all 0.2s; }
        .btn-primary { background: #667eea; color: white; }
        .btn-primary:hover { background: #5568d3; }
        .btn-success { background: #10b981; color: white; }
        .btn-success:hover { background: #059669; }
        .btn-danger { background: #ef4444; color: white; }
        .btn-danger:hover { background: #dc2626; }
        .btn-group { display: flex; gap: 10px; }
        .status-badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 500; }
        .status-running { background: #d1fae5; color: #065f46; }
        .status-stopped { background: #fee2e2; color: #991b1b; }
        .table-container { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #eee; }
        th { background: #f9fafb; font-weight: 600; color: #374151; }
        .success { color: #059669; }
        .error { color: #dc2626; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; }
        .stat-card { background: #f9fafb; padding: 20px; border-radius: 8px; text-align: center; }
        .stat-value { font-size: 32px; font-weight: 700; color: #667eea; }
        .stat-label { color: #6b7280; margin-top: 5px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>视频自动转码工具</h1>
            <p>自动监测文件夹、智能转码优化存储空间</p>
        </div>
        <div class="content">
            <div class="section">
                <div class="section-title">状态控制</div>
                <div id="status-section">
                    <p>当前状态: <span id="status-badge" class="status-badge status-stopped">已停止</span></p>
                    <div class="btn-group" style="margin-top: 15px;">
                        <button class="btn btn-success" onclick="startConverter()">启动服务</button>
                        <button class="btn btn-danger" onclick="stopConverter()">停止服务</button>
                    </div>
                </div>
            </div>
            
            <div class="section">
                <div class="section-title">配置</div>
                <div class="form-group">
                    <label>监控文件夹路径</label>
                    <input type="text" id="monitor_dir" placeholder="/path/to/videos">
                </div>
                <div class="form-group">
                    <label>CRF 质量 (18-28, 越小越好)</label>
                    <input type="number" id="crf" min="18" max="28" step="1" value="23">
                </div>
                <div class="form-group">
                    <label>编码 Preset (速度/质量平衡)</label>
                    <select id="preset">
                        <option value="ultrafast">ultrafast (最快)</option>
                        <option value="superfast">superfast</option>
                        <option value="veryfast">veryfast</option>
                        <option value="faster">faster</option>
                        <option value="fast">fast</option>
                        <option value="medium" selected>medium (推荐)</option>
                        <option value="slow">slow (NVENC P5 等效)</option>
                        <option value="slower">slower</option>
                        <option value="veryslow">veryslow (最慢)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>转码线程数</label>
                    <input type="number" id="threads" min="1" max="8" step="1" value="1">
                </div>
                <div class="form-group">
                    <label>视频编码器</label>
                    <select id="codec">
                        <option value="libx264">H.264 (libx264)</option>
                        <option value="libx265">H.265 (libx265)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>输出容器</label>
                    <select id="container">
                        <option value="mp4">MP4</option>
                        <option value="mkv">MKV</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>
                        <input type="checkbox" id="use_gpu" checked>
                        使用 GPU 加速转码（Intel Quick Sync）
                    </label>
                </div>
                <button class="btn btn-primary" onclick="saveConfig()">保存配置</button>
            </div>
            
            <div class="section">
                <div class="section-title">处理统计</div>
                <div class="stats">
                    <div class="stat-card">
                        <div class="stat-value" id="total-saved">0</div>
                        <div class="stat-label">已节省空间 (MB)</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value" id="total-count">0</div>
                        <div class="stat-label">处理文件数</div>
                    </div>
                </div>
            </div>
            
            <div class="section">
                <div class="section-title">处理日志</div>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>ID</th>
                                <th>文件路径</th>
                                <th>原始大小 (MB)</th>
                                <th>节省 (MB)</th>
                                <th>状态</th>
                                <th>处理时间</th>
                            </tr>
                        </thead>
                        <tbody id="logs-tbody">
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <script>
        let config = {};
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        async function loadConfig() {
            const res = await fetch('/api/config');
            config = await res.json();
            document.getElementById('monitor_dir').value = config.monitor_dir || '';
            document.getElementById('crf').value = config.crf || 23;
            document.getElementById('preset').value = config.preset || 'medium';
            document.getElementById('threads').value = config.threads || 1;
            document.getElementById('codec').value = config.codec || 'libx264';
            document.getElementById('container').value = config.container || 'mp4';
            document.getElementById('use_gpu').checked = config.use_gpu !== false;
        }
        
        async function saveConfig() {
            config.monitor_dir = document.getElementById('monitor_dir').value;
            config.crf = parseInt(document.getElementById('crf').value);
            config.preset = document.getElementById('preset').value;
            config.threads = parseInt(document.getElementById('threads').value);
            config.codec = document.getElementById('codec').value;
            config.container = document.getElementById('container').value;
            config.use_gpu = document.getElementById('use_gpu').checked;
            await fetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config)
            });
            alert('配置已保存！');
        }
        
        async function checkStatus() {
            const res = await fetch('/api/status');
            const data = await res.json();
            const badge = document.getElementById('status-badge');
            if (data.running) {
                badge.textContent = '运行中';
                badge.className = 'status-badge status-running';
            } else {
                badge.textContent = '已停止';
                badge.className = 'status-badge status-stopped';
            }
        }
        
        async function startConverter() {
            await fetch('/api/start', { method: 'POST' });
            await checkStatus();
            await loadLogs();
        }
        
        async function stopConverter() {
            await fetch('/api/stop', { method: 'POST' });
            await checkStatus();
        }
        
        async function loadLogs() {
            const res = await fetch('/api/logs');
            const data = await res.json();
            document.getElementById('total-saved').textContent = data.total_saved_mb;
            document.getElementById('total-count').textContent = data.logs.length;
            
            const tbody = document.getElementById('logs-tbody');
            tbody.innerHTML = '';
            data.logs.forEach(log => {
                const tr = document.createElement('tr');
                // 使用 textContent 而非 innerHTML，防止 XSS
                const tdId = document.createElement('td');
                tdId.textContent = log.id;
                const tdPath = document.createElement('td');
                tdPath.textContent = log.filepath;
                const tdSize = document.createElement('td');
                tdSize.textContent = log.file_size_mb;
                const tdSaved = document.createElement('td');
                tdSaved.textContent = log.saved_size_mb;
                const tdStatus = document.createElement('td');
                tdStatus.textContent = log.success ? '成功' : '失败';
                tdStatus.className = log.success ? 'success' : 'error';
                const tdTime = document.createElement('td');
                tdTime.textContent = log.processed_at;
                
                tr.appendChild(tdId);
                tr.appendChild(tdPath);
                tr.appendChild(tdSize);
                tr.appendChild(tdSaved);
                tr.appendChild(tdStatus);
                tr.appendChild(tdTime);
                tbody.appendChild(tr);
            });
        }
        
        // Initial load
        loadConfig();
        checkStatus();
        loadLogs();
        
        // Refresh periodically
        setInterval(() => {
            checkStatus();
            loadLogs();
        }, 5000);
    </script>
</body>
</html>''')
    
    # Run Flask
    app.run(host='0.0.0.0', port=5000, debug=False)
