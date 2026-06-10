#!/usr/bin/env python3
import os
import sys
import sqlite3
import subprocess
import time
import json
import threading
import shutil
import traceback
import hashlib
import re
from pathlib import Path


class Database:
    def __init__(self, db_path=None):
        if db_path is None:
            self.db_path = 'fpk_converter.db'
        else:
            self.db_path = db_path
        try:
            db_dir = os.path.dirname(os.path.abspath(self.db_path))
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
        except Exception:
            pass
        self._init_database()

    def _get_connection(self):
        max_retries = 3
        for i in range(max_retries):
            try:
                conn = sqlite3.connect(self.db_path, timeout=10)
                return conn
            except sqlite3.Error as e:
                if i == max_retries - 1:
                    raise
                time.sleep(0.5)

    def _init_database(self):
        try:
            conn = self._get_connection()
            try:
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
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_filepath ON processed_files (filepath)
                ''')
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"数据库初始化失败: {e}")

    def is_file_processed(self, filepath):
        try:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT 1, success FROM processed_files WHERE filepath = ?', (str(filepath),))
                result = cursor.fetchone()
                return result is not None and result[1] == 1
            finally:
                conn.close()
        except Exception as e:
            print(f"数据库查询失败: {e}")
            return False

    def add_processed_file(self, filepath, file_size, success, saved_size=0):
        try:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO processed_files 
                    (filepath, file_size, success, saved_size)
                    VALUES (?, ?, ?, ?)
                ''', (str(filepath), file_size, int(success), saved_size))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            print(f"数据库写入失败: {e}")

    def get_processed_files(self):
        try:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM processed_files ORDER BY processed_at DESC')
                results = cursor.fetchall()
                return results
            finally:
                conn.close()
        except Exception as e:
            print(f"数据库查询失败: {e}")
            return []


class VideoConverter:
    VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm', '.m4v', '.3gp'}
    ALLOWED_CODECS = {'hevc_qsv', 'h264_qsv', 'libx264', 'libx265'}
    ALLOWED_CONTAINERS = {'mp4', 'mkv'}
    ALLOWED_PRESETS = {'ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'}
    
    MAX_BITRATE = 13 * 1000 * 1000  # 13 Mbps
    SKIP_FILE_SIZE = 10 * 1000 * 1000  # 10 MB - 小于此大小的文件跳过
    SKIP_BITRATE = 5000 * 1000  # 5000 kbps = 5 Mbps - HEVC 1080p 低于此码率跳过
    TRANSCODE_DELAY = 900  # 15 分钟
    TEMP_MAX_GB = 30

    def __init__(self, db, target_quality=23, codec='libx264', container='mp4',
                 preset='medium', threads=1, use_gpu=True, temp_dir=''):
        self.db = db
        self.target_quality = max(1, min(51, int(target_quality)))
        self.codec = codec if codec in self.ALLOWED_CODECS else 'libx265'
        self.container = container if container in self.ALLOWED_CONTAINERS else 'mp4'
        self.preset = preset if preset in self.ALLOWED_PRESETS else 'medium'
        self.threads = max(1, min(16, int(threads)))
        self.use_gpu = bool(use_gpu)
        self.temp_dir = Path(temp_dir) if temp_dir and str(temp_dir).strip() else None
        if self.temp_dir and not self.temp_dir.exists():
            self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._queue = []
        self._queue_lock = threading.Lock()
        self._worker_running = False
        self.recent_events = {}
        self.event_lock = threading.Lock()

    def is_video_file(self, filepath):
        return filepath.suffix.lower() in self.VIDEO_EXTENSIONS

    def _validate_path(self, filepath):
        try:
            filepath = Path(filepath)
            # 在 resolve 之前检查路径遍历
            if '..' in str(filepath):
                return None
            filepath = filepath.resolve()
        except (OSError, ValueError, RuntimeError):
            return None
        if not filepath.is_file():
            return None
        if filepath.name.startswith('.'):
            return None
        return filepath

    def _deduplicate_event(self, filepath_str, debounce_seconds=60):
        with self.event_lock:
            now = time.time()
            last_time = self.recent_events.get(filepath_str, 0)
            if now - last_time < debounce_seconds:
                return False
            self.recent_events[filepath_str] = now
            if len(self.recent_events) > 200:
                cutoff = now - 300
                self.recent_events = {k: v for k, v in self.recent_events.items() if v > cutoff}
            return True

    def get_file_size(self, filepath):
        try:
            return os.path.getsize(filepath)
        except Exception as e:
            print(f"获取文件大小失败: {e}")
            return None

    def get_video_info(self, filepath):
        filepath = self._validate_path(filepath)
        if not filepath:
            return None
        
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_format', '-show_streams',
            str(filepath)
        ]
        log_file = None
        try:
            if self.temp_dir and self.temp_dir.is_dir():
                log_file = self.temp_dir / f"ffprobe_{os.getpid()}_{int(time.time())}.log"
            if log_file:
                with open(log_file, 'w') as lf:
                    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=lf, timeout=60)
            else:
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
            
            if result.returncode != 0:
                # 失败时也要清理 ffprobe 日志
                if log_file and log_file.exists():
                    try: log_file.unlink()
                    except Exception: pass
                return None
            try:
                stdout_str = result.stdout.decode('utf-8', errors='replace')
                info = json.loads(stdout_str)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # 解析失败时也要清理 ffprobe 日志
                if log_file and log_file.exists():
                    try: log_file.unlink()
                    except Exception: pass
                return None
            
            video_stream = None
            for stream in info.get('streams', []):
                if stream.get('codec_type') == 'video':
                    video_stream = stream
                    break
            
            if not video_stream:
                # 没有视频流时也要清理 ffprobe 日志
                if log_file and log_file.exists():
                    try: log_file.unlink()
                    except Exception: pass
                return None
            
            width = video_stream.get('width', 0)
            height = video_stream.get('height', 0)
            codec = video_stream.get('codec_name', '')
            
            bit_rate_str = video_stream.get('bit_rate')
            if not bit_rate_str:
                bit_rate_str = info.get('format', {}).get('bit_rate')
            
            try:
                bit_rate = int(bit_rate_str) if bit_rate_str else 0
            except (ValueError, TypeError):
                bit_rate = 0
            
            # 成功获取信息后清理 ffprobe 日志
            if log_file and log_file.exists():
                try: log_file.unlink()
                except Exception: pass
            return {'width': width, 'height': height, 'codec': codec, 'bit_rate': bit_rate}
        except subprocess.TimeoutExpired:
            print(f"ffprobe 超时: {filepath}")
            if log_file and log_file.exists():
                try: log_file.unlink()
                except Exception: pass
            return None
        except FileNotFoundError:
            print("ffprobe 未安装")
            if log_file and log_file.exists():
                try: log_file.unlink()
                except Exception: pass
            return None
        except Exception as e:
            print(f"获取视频信息失败: {e}")
            if log_file and log_file.exists():
                try: log_file.unlink()
                except Exception: pass
            return None

    def should_skip_transcode(self, video_info):
        if not video_info:
            return False
        codec = video_info.get('codec', '').lower()
        height = video_info.get('height', 0)
        bit_rate = video_info.get('bit_rate', 0)
        is_hevc = codec in ['hevc', 'h265', 'h.265']
        is_1080p_or_lower = height <= 1080
        is_low_bitrate = bit_rate > 0 and bit_rate < self.SKIP_BITRATE
        if is_hevc and is_1080p_or_lower and is_low_bitrate:
            print(f"检测到 {height}p HEVC 视频，码率 {bit_rate / 1000000:.2f} Mbps < {self.SKIP_BITRATE / 1000000:.0f} Mbps，跳过转码")
            return True
        return False

    def queue_file(self, filepath):
        filepath = self._validate_path(filepath)
        if not filepath:
            return
        
        filepath_str = str(filepath)
        if not self._deduplicate_event(filepath_str):
            return
        
        with self._queue_lock:
            for item in self._queue:
                if item['path'] == filepath_str:
                    return
            self._queue.append({'path': filepath_str, 'queued_at': time.time()})
            print(f"文件加入串行队列({len(self._queue)}): {filepath_str}")
        
        self._ensure_worker()

    def _ensure_worker(self):
        with self._queue_lock:
            if self._worker_running:
                return
            self._worker_running = True
        t = threading.Thread(target=self._serial_worker, daemon=True)
        t.start()

    def _serial_worker(self):
        while True:
            item = None
            with self._queue_lock:
                if self._queue:
                    item = self._queue.pop(0)
                else:
                    self._worker_running = False
                    return
            
            filepath_str = item['path']
            filepath = Path(filepath_str)
            queued_at = item.get('queued_at', time.time())
            
            wait_start = time.time()
            while time.time() - wait_start < self.TRANSCODE_DELAY:
                if not os.path.exists(filepath_str):
                    print(f"文件在等待期间被删除，跳过: {filepath_str}")
                    break
                time.sleep(5)
            
            if not os.path.exists(filepath_str):
                continue
            
            try:
                mtime = os.path.getmtime(filepath_str)
                if mtime > queued_at:
                    # 限制重新入队次数，防止文件持续被修改导致无限循环
                    retry_count = item.get('retry', 0)
                    if retry_count >= 3:
                        print(f"文件修改次数过多，跳过: {filepath_str}")
                        continue
                    print(f"文件在等待期间被修改，重新入队({retry_count+1}/3): {filepath_str}")
                    with self._queue_lock:
                        self._queue.append({'path': filepath_str, 'queued_at': time.time(), 'retry': retry_count + 1})
                    continue
            except Exception:
                pass
            
            self._enforce_temp_limit()
            
            try:
                print(f"[SERIAL] 开始处理: {filepath_str}")
                self.convert_video(filepath)
            except Exception as e:
                print(f"转码异常: {e}")
                traceback.print_exc()
            
            time.sleep(2)

    def _enforce_temp_limit(self):
        if not self.temp_dir or not self.temp_dir.is_dir():
            return
        try:
            total = 0
            files = []
            for f in self.temp_dir.iterdir():
                if f.is_file():
                    sz = f.stat().st_size
                    total += sz
                    files.append((f, sz))
            files.sort(key=lambda x: x[0].stat().st_mtime)
            limit = self.TEMP_MAX_GB * 1024 * 1024 * 1024
            now = time.time()
            for f, sz in files:
                if total <= limit:
                    break
                # 跳过最近5分钟内创建的文件，可能正在使用
                try:
                    if now - f.stat().st_mtime < 300:
                        continue
                except Exception:
                    continue
                try:
                    f.unlink()
                    total -= sz
                    print(f"清理临时文件(超{self.TEMP_MAX_GB}GB): {f.name}")
                except Exception as e:
                    print(f"清理临时文件失败: {e}")
        except Exception as e:
            print(f"检查临时目录失败: {e}")

    def _safe_temp_stem(self, input_path):
        raw_stem = Path(input_path).stem
        safe_stem = re.sub(r'[^A-Za-z0-9._-]+', '_', raw_stem).strip('._-')
        if not safe_stem:
            safe_stem = 'video'
        digest = hashlib.sha1(str(input_path).encode('utf-8', errors='ignore')).hexdigest()[:10]
        safe_stem = safe_stem[:60].rstrip('._-') or 'video'
        return f"{safe_stem}_{digest}"

    def _build_ffmpeg_cmd(self, input_path, output_path, target_codec, target_width, target_height, needs_resize):
        is_qsv = target_codec.endswith('_qsv')
        cmd = ['ffmpeg', '-hide_banner', '-nostats', '-i', str(input_path)]
        cmd.extend(['-c:v', target_codec])

        if is_qsv:
            cmd.extend(['-preset', 'medium'])
            cmd.extend(['-global_quality', str(self.target_quality)])
        else:
            cmd.extend(['-preset', self.preset])
            cmd.extend(['-crf', str(self.target_quality)])
            cmd.extend(['-maxrate', f'{self.MAX_BITRATE}'])
            cmd.extend(['-bufsize', f'{self.MAX_BITRATE * 2}'])

        if needs_resize:
            cmd.extend(['-vf', f'scale={target_width}:{target_height}'])

        cmd.extend(['-c:a', 'copy'])
        if self.container == 'mp4':
            cmd.extend(['-movflags', '+faststart'])
        cmd.extend(['-y', str(output_path)])
        return cmd

    def _cpu_fallback_codec(self, target_codec):
        if target_codec == 'h264_qsv':
            return 'libx264'
        if target_codec == 'hevc_qsv':
            return 'libx265'
        return None

    def _run_ffmpeg_cmd(self, cmd, ffmpeg_log):
        with open(ffmpeg_log, 'w') as log_file:
            print(f"ffmpeg 日志: {ffmpeg_log}")
            return subprocess.run(cmd, stdout=log_file, stderr=subprocess.STDOUT, timeout=3600)

    def convert_video(self, input_path):
        try:
            return self._convert_video_impl(input_path)
        except Exception as e:
            print(f"转码过程异常: {e}")
            traceback.print_exc()
            return False, 0

    def _convert_video_impl(self, input_path):
        input_path = Path(input_path)
        if not self.is_video_file(input_path):
            print(f"非视频文件，跳过: {input_path}")
            return False, 0

        input_path = self._validate_path(input_path)
        if not input_path:
            print(f"路径验证失败，跳过: {input_path}")
            return False, 0

        if self.db.is_file_processed(input_path):
            print(f"已处理过，跳过: {input_path}")
            return False, 0

        original_size = self.get_file_size(input_path)
        if original_size is None:
            print(f"获取文件大小失败，跳过: {input_path}")
            return False, 0

        if original_size < self.SKIP_FILE_SIZE:
            print(f"文件太小 ({original_size / (1024*1024):.2f} MB < 10 MB)，跳过: {input_path}")
            self.db.add_processed_file(input_path, original_size, True, 0)
            return True, 0

        # 检查 ffmpeg 是否可用
        try:
            subprocess.run(['ffmpeg', '-version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except FileNotFoundError:
            print("错误: ffmpeg 未安装或不在 PATH 中，无法转码")
            return False, 0
        except Exception as e:
            print(f"检查 ffmpeg 失败: {e}")

        video_info = self.get_video_info(input_path)
        if not video_info:
            print(f"无法获取视频信息: {input_path}")
            return False, 0
        
        width, height = video_info['width'], video_info['height']
        codec = video_info.get('codec', 'unknown')
        bit_rate = video_info.get('bit_rate', 0)
        bit_rate_mbps = bit_rate / 1000000 if bit_rate > 0 else 0
        
        print(f"视频信息: {width}x{height}, 编码: {codec}, 码率: {bit_rate_mbps:.2f} Mbps, 大小: {original_size / (1024*1024):.2f} MB")
        
        if self.should_skip_transcode(video_info):
            self.db.add_processed_file(input_path, original_size, True, 0)
            return True, 0
        
        # 尊重用户选择的编码器，GPU加速仅在用户选择HEVC时生效
        if self.use_gpu and self.codec in ('libx265', 'hevc_qsv'):
            target_codec = 'hevc_qsv'
        elif self.use_gpu and self.codec == 'libx264':
            target_codec = 'h264_qsv'
        else:
            target_codec = self.codec
        
        target_width, target_height = width, height
        needs_resize = False
        
        if height > 1080:
            target_height = 1080
            target_width = int(width * 1080 / height)
            target_width = target_width - (target_width % 2)
            if target_width < 2:
                target_width = 2
            needs_resize = True
            print(f"缩放分辨率: {width}x{height} -> {target_width}x{target_height}")
        
        print(f"开始转码: {input_path} (大小: {original_size / (1024*1024):.2f} MB)")

        unique_id = f"{os.getpid()}_{int(time.time())}"
        safe_stem = self._safe_temp_stem(input_path)
        if self.temp_dir and self.temp_dir.is_dir():
            output_path = self.temp_dir / f"{safe_stem}_tmp_{unique_id}.{self.container}"
        else:
            # 如果 temp_dir 不可用，使用系统临时目录，避免污染原文件目录
            import tempfile
            output_path = Path(tempfile.gettempdir()) / f"fpkc_{safe_stem}_tmp_{unique_id}.{self.container}"
        
        cmd = self._build_ffmpeg_cmd(input_path, output_path, target_codec, target_width, target_height, needs_resize)

        print(f"ffmpeg 命令: {' '.join(cmd)}")

        # 始终创建 ffmpeg 日志文件，确保错误可追踪
        log_dir = self.temp_dir if (self.temp_dir and self.temp_dir.is_dir()) else Path(tempfile.gettempdir())
        log_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg_log = log_dir / f"ffmpeg_{unique_id}.log"
        try:
            result = self._run_ffmpeg_cmd(cmd, ffmpeg_log)

            if result.returncode != 0:
                err_msg = 'Unknown error'
                if ffmpeg_log.exists():
                    try: err_msg = ffmpeg_log.read_text()[-1000:]
                    except: pass
                print(f"FFmpeg 错误 (返回码 {result.returncode}): {err_msg}")
                fallback_codec = self._cpu_fallback_codec(target_codec)
                if fallback_codec:
                    if output_path.exists():
                        try: output_path.unlink()
                        except: pass
                    fallback_cmd = self._build_ffmpeg_cmd(input_path, output_path, fallback_codec, target_width, target_height, needs_resize)
                    print(f"QSV 转码失败，自动降级 CPU 编码器重试: {fallback_codec}")
                    print(f"ffmpeg 降级命令: {' '.join(fallback_cmd)}")
                    result = self._run_ffmpeg_cmd(fallback_cmd, ffmpeg_log)
                    if result.returncode == 0:
                        cmd = fallback_cmd
                    else:
                        if ffmpeg_log.exists():
                            try: err_msg = ffmpeg_log.read_text()[-1000:]
                            except: pass
                        print(f"CPU 降级转码仍失败 (返回码 {result.returncode}): {err_msg}")
                        self.db.add_processed_file(input_path, original_size, False)
                        if output_path.exists():
                            try: output_path.unlink()
                            except: pass
                        return False, 0
                else:
                    self.db.add_processed_file(input_path, original_size, False)
                    if output_path.exists():
                        try: output_path.unlink()
                        except: pass
                    return False, 0
        except subprocess.TimeoutExpired:
            print(f"FFmpeg 转码超时(1小时): {input_path}")
            self.db.add_processed_file(input_path, original_size, False)
            if output_path.exists():
                try: output_path.unlink()
                except: pass
            return False, 0
        except FileNotFoundError:
            print("ffmpeg 未安装或不在 PATH 中")
            self.db.add_processed_file(input_path, original_size, False)
            return False, 0
        except Exception as e:
            print(f"转码失败: {e}")
            traceback.print_exc()
            self.db.add_processed_file(input_path, original_size, False)
            if output_path.exists():
                try: output_path.unlink()
                except: pass
            return False, 0

        converted_size = self.get_file_size(output_path)
        if converted_size is None or converted_size == 0:
            if output_path.exists():
                try: output_path.unlink()
                except: pass
            self.db.add_processed_file(input_path, original_size, False)
            return False, 0

        # 转码成功后清理 ffmpeg 日志
        if ffmpeg_log and ffmpeg_log.exists():
            try: ffmpeg_log.unlink()
            except: pass

        print(f"转码完成: 原大小 {original_size / (1024*1024):.2f} MB, 新大小 {converted_size / (1024*1024):.2f} MB")

        saved_size = original_size - converted_size
        if converted_size < original_size:
            print(f"转码后更小，节省 {saved_size / (1024*1024):.2f} MB")
            final_output_path = input_path.parent / f"{input_path.stem}.{self.container}"
            backup_path = None
            original_stem = input_path.stem
            original_suffix = input_path.suffix
            # 如果输出路径和原文件相同，先移动原文件到备份，避免数据丢失
            if final_output_path.resolve() == input_path.resolve():
                backup_path = input_path.parent / f"{original_stem}_backup_{unique_id}{original_suffix}"
                try:
                    shutil.move(str(input_path), str(backup_path))
                    input_path = backup_path  # 后续删除的是备份
                except Exception as e:
                    print(f"备份原文件失败: {e}")
                    if output_path.exists():
                        try: output_path.unlink()
                        except: pass
                    return False, 0
            elif final_output_path.exists():
                # 不同路径但目标已存在，先删除目标
                try: final_output_path.unlink()
                except: pass
            try:
                # 跨设备移动用 shutil.move 替代 Path.rename
                shutil.move(str(output_path), str(final_output_path))
            except Exception as e:
                print(f"移动输出文件失败: {e}")
                # 尝试恢复备份
                if backup_path and backup_path.exists():
                    try:
                        restore_target = final_output_path.parent / f"{original_stem}{original_suffix}"
                        shutil.move(str(backup_path), str(restore_target))
                        print("已恢复备份")
                    except Exception:
                        pass
                if output_path.exists():
                    try: output_path.unlink()
                    except: pass
                return False, 0
            try:
                # 删除原文件（或备份）
                if backup_path and backup_path.exists():
                    backup_path.unlink()
                else:
                    input_path.unlink()
                print(f"已替换原文件: {final_output_path}")
            except Exception as e:
                print(f"删除原文件失败(转码文件已保留): {e}")
            self.db.add_processed_file(str(final_output_path), original_size, True, saved_size)
            return True, saved_size
        else:
            print(f"转码后更大或相同，删除转码文件，保留原文件")
            try: output_path.unlink()
            except: pass
            self.db.add_processed_file(input_path, original_size, True, 0)
            return True, 0


class FolderScanner:
    """定时扫描模式（不使用 watchdog inotify，避免 NAS 崩溃）"""
    def __init__(self, folder_path, converter, interval=60, max_depth=3):
        self.folder_path = Path(folder_path).resolve()
        if not self.folder_path.is_dir():
            raise ValueError(f"监控路径不存在或不是目录: {self.folder_path}")
        self.converter = converter
        self.interval = max(10, interval)
        self.max_depth = max(0, max_depth)
        self._stop = threading.Event()

    def start(self):
        print(f"开始定时扫描: {self.folder_path} (间隔{self.interval}秒, 最大深度{self.max_depth})")
        self._scan(self.folder_path, 0)
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            if not self._stop.is_set():
                self._scan(self.folder_path, 0)

    def stop(self):
        self._stop.set()

    def _scan(self, directory, depth):
        """递归扫描目录，限制最大深度"""
        if depth > self.max_depth:
            return
        try:
            for item in directory.iterdir():
                try:
                    if item.is_dir():
                        self._scan(item, depth + 1)
                    elif item.is_file() and self.converter.is_video_file(item):
                        self.converter.queue_file(item)
                except PermissionError:
                    pass
                except Exception as e:
                    print(f"扫描文件失败: {e}")
        except PermissionError:
            pass
        except Exception as e:
            print(f"扫描目录失败: {e}")


def main():
    if len(sys.argv) < 2:
        print("使用方法: python fpk_converter.py <文件夹路径>")
        sys.exit(1)

    folder_path = sys.argv[1]
    if not os.path.isdir(folder_path):
        print(f"错误: '{folder_path}' 不是有效的文件夹")
        sys.exit(1)

    try:
        db = Database()
        converter = VideoConverter(db)
        scanner = FolderScanner(folder_path, converter)
        scanner.start()
    except KeyboardInterrupt:
        print("\n用户中断")
    except Exception as e:
        print(f"程序异常退出: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
