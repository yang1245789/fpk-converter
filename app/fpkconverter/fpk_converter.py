#!/usr/bin/env python3
import os
import sys
import sqlite3
import subprocess
import time
import json
import threading
import re
import shutil
import traceback
from pathlib import Path
from datetime import datetime


# 路径校验正则
ALLOWED_PATH_PATTERN = re.compile(r'^/[A-Za-z0-9_./\-\s]+$')


class Database:
    def __init__(self, db_path=None):
        if db_path is None:
            self.db_path = 'fpk_converter.db'
        else:
            self.db_path = db_path
        # 确保数据库目录存在
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        except Exception:
            pass
        self._init_database()

    def _get_connection(self):
        """获取数据库连接，包含重试机制"""
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
            conn.close()
        except Exception as e:
            print(f"数据库初始化失败: {e}")

    def is_file_processed(self, filepath):
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT 1 FROM processed_files WHERE filepath = ?', (str(filepath),))
            result = cursor.fetchone()
            conn.close()
            return result is not None
        except Exception as e:
            print(f"数据库查询失败: {e}")
            return False

    def add_processed_file(self, filepath, file_size, success, saved_size=0):
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO processed_files 
                (filepath, file_size, success, saved_size)
                VALUES (?, ?, ?, ?)
            ''', (str(filepath), file_size, int(success), saved_size))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            print(f"数据库写入失败: {e}")
        except Exception as e:
            print(f"数据库异常: {e}")

    def get_processed_files(self):
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM processed_files ORDER BY processed_at DESC')
            results = cursor.fetchall()
            conn.close()
            return results
        except Exception as e:
            print(f"数据库查询失败: {e}")
            return []


class VideoConverter:
    VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm', '.m4v', '.3gp'}
    ALLOWED_CODECS = {'hevc_qsv', 'h264_qsv', 'libx264', 'libx265'}
    ALLOWED_CONTAINERS = {'mp4', 'mkv'}
    ALLOWED_PRESETS = {'ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'}
    
    MAX_BITRATE = 13 * 1000 * 1000
    SKIP_THRESHOLD = 10 * 1000 * 1000
    TRANSCODE_DELAY = 900
    TEMP_MAX_GB = 30  # 临时文件总大小上限

    def __init__(self, db, target_quality=23, codec='libx264', container='mp4',
                 preset='medium', threads=1, use_gpu=True, temp_dir=''):
        self.db = db
        self.target_quality = max(1, min(51, int(target_quality)))
        self.codec = codec if codec in self.ALLOWED_CODECS else 'libx265'
        self.container = container if container in self.ALLOWED_CONTAINERS else 'mp4'
        self.preset = preset if preset in self.ALLOWED_PRESETS else 'medium'
        self.threads = max(1, min(16, int(threads)))
        self.use_gpu = bool(use_gpu)
        # 临时目录（不写系统分区）
        self.temp_dir = Path(temp_dir) if temp_dir else None
        if self.temp_dir and not self.temp_dir.exists():
            self.temp_dir.mkdir(parents=True, exist_ok=True)
        # 串行处理队列（单线程顺序执行）
        self._queue = []
        self._queue_lock = threading.Lock()
        self._worker_running = False
        # 去重
        self.recent_events = {}
        self.event_lock = threading.Lock()

    def is_video_file(self, filepath):
        return filepath.suffix.lower() in self.VIDEO_EXTENSIONS

    def _validate_path(self, filepath):
        """校验文件路径安全性"""
        try:
            filepath = Path(filepath).resolve()
        except (OSError, ValueError, RuntimeError):
            return None
        if not filepath.is_file():
            return None
        # 拒绝隐藏文件（以 . 开头）
        if filepath.name.startswith('.'):
            return None
        # 拒绝包含路径遍历
        if '..' in str(filepath):
            return None
        return filepath

    def _deduplicate_event(self, filepath_str, debounce_seconds=5):
        """事件去重：相同文件在 debounce_seconds 内的事件合并"""
        with self.event_lock:
            now = time.time()
            last_time = self.recent_events.get(filepath_str, 0)
            if now - last_time < debounce_seconds:
                return False  # 重复事件，跳过
            self.recent_events[filepath_str] = now
            # 清理过期记录
            if len(self.recent_events) > 200:
                cutoff = now - 60
                self.recent_events = {k: v for k, v in self.recent_events.items() if v > cutoff}
            return True

    def get_file_size(self, filepath):
        try:
            return os.path.getsize(filepath)
        except Exception as e:
            print(f"获取文件大小失败: {e}")
            return None

    def get_video_info(self, filepath):
        """使用 ffprobe 获取视频信息"""
        filepath = self._validate_path(filepath)
        if not filepath:
            return None
        
        cmd = [
            'ffprobe',
            '-v', 'quiet',
            '-print_format', 'json',
            '-show_format',
            '-show_streams',
            str(filepath)
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                return None
            try:
                info = json.loads(result.stdout)
            except json.JSONDecodeError as e:
                print(f"ffprobe 输出解析失败: {e}")
                return None
            
            video_stream = None
            for stream in info.get('streams', []):
                if stream.get('codec_type') == 'video':
                    video_stream = stream
                    break
            
            if not video_stream:
                return None
            
            width = video_stream.get('width', 0)
            height = video_stream.get('height', 0)
            codec = video_stream.get('codec_name', '')
            
            bit_rate_str = video_stream.get('bit_rate')
            if not bit_rate_str:
                format_info = info.get('format', {})
                bit_rate_str = format_info.get('bit_rate')
            
            try:
                bit_rate = int(bit_rate_str) if bit_rate_str else 0
            except (ValueError, TypeError):
                bit_rate = 0
            
            return {
                'width': width,
                'height': height,
                'codec': codec,
                'bit_rate': bit_rate
            }
        except subprocess.TimeoutExpired:
            print(f"ffprobe 超时: {filepath}")
            return None
        except FileNotFoundError:
            print("ffprobe 未安装")
            return None
        except Exception as e:
            print(f"获取视频信息失败: {e}")
            return None

    def should_skip_transcode(self, video_info):
        if not video_info:
            return False
        
        codec = video_info.get('codec', '').lower()
        height = video_info.get('height', 0)
        bit_rate = video_info.get('bit_rate', 0)
        
        is_hevc = codec in ['hevc', 'h265', 'h.265']
        is_1080p = height == 1080
        is_low_bitrate = bit_rate > 0 and bit_rate < self.SKIP_THRESHOLD
        
        if is_hevc and is_1080p and is_low_bitrate:
            print(f"检测到 1080p HEVC 视频，码率 {bit_rate / 1000000:.2f} Mbps < 10 Mbps，跳过转码")
            return True
        
        return False

    def queue_file(self, filepath):
        """将文件加入串行处理队列"""
        filepath = self._validate_path(filepath)
        if not filepath:
            return
        
        filepath_str = str(filepath)
        # 事件去重
        if not self._deduplicate_event(filepath_str):
            return
        
        with self._queue_lock:
            # 检查队列里是否已有此文件
            for item in self._queue:
                if item['path'] == filepath_str:
                    return
            self._queue.append({
                'path': filepath_str,
                'queued_at': time.time()
            })
            print(f"文件加入串行队列({len(self._queue)}): {filepath_str}")
        
        self._ensure_worker()

    def _ensure_worker(self):
        """确保串行工作线程在运行"""
        with self._queue_lock:
            if self._worker_running:
                return
            self._worker_running = True
        t = threading.Thread(target=self._serial_worker, daemon=True)
        t.start()

    def _serial_worker(self):
        """串行工作线程——一个接一个处理"""
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
            
            # 等待延迟期
            wait_start = time.time()
            while time.time() - wait_start < self.TRANSCODE_DELAY:
                with self._queue_lock:
                    if not self._worker_running:
                        return
                time.sleep(5)
            
            # 检查文件是否还存在且未被修改
            if not os.path.exists(filepath_str):
                print(f"文件已删除，跳过: {filepath_str}")
                continue
            
            try:
                mtime = os.path.getmtime(filepath_str)
                if mtime > item['queued_at']:
                    print(f"文件在等待期间被修改，重新入队: {filepath_str}")
                    with self._queue_lock:
                        self._queue.append({
                            'path': filepath_str,
                            'queued_at': time.time()
                        })
                    continue
            except Exception:
                pass
            
            # 检查并清理临时目录
            self._enforce_temp_limit()
            
            # 串行执行转码
            try:
                print(f"[SERIAL] 开始处理: {filepath_str}")
                self.convert_video(filepath)
            except Exception as e:
                print(f"转码异常: {e}")
                traceback.print_exc()
            
            # 每个文件之间短暂间隔
            time.sleep(2)

    def _enforce_temp_limit(self):
        """确保临时目录不超过 TEMP_MAX_GB"""
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
            # 按修改时间排序（旧→新）
            files.sort(key=lambda x: x[0].stat().st_mtime)
            limit = self.TEMP_MAX_GB * 1024 * 1024 * 1024
            for f, sz in files:
                if total <= limit:
                    break
                try:
                    f.unlink()
                    total -= sz
                    print(f"清理临时文件(超{self.TEMP_MAX_GB}GB): {f.name}")
                except Exception as e:
                    print(f"清理临时文件失败: {e}")
        except Exception as e:
            print(f"检查临时目录失败: {e}")

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
            return False, 0

        input_path = self._validate_path(input_path)
        if not input_path:
            return False, 0

        if self.db.is_file_processed(input_path):
            return False, 0

        original_size = self.get_file_size(input_path)
        if original_size is None:
            return False, 0

        video_info = self.get_video_info(input_path)
        if not video_info:
            print(f"无法获取视频信息: {input_path}")
            return False, 0
        
        width, height = video_info['width'], video_info['height']
        codec = video_info.get('codec', 'unknown')
        bit_rate = video_info.get('bit_rate', 0)
        bit_rate_mbps = bit_rate / 1000000 if bit_rate > 0 else 0
        
        print(f"视频信息: {width}x{height}, 编码: {codec}, 码率: {bit_rate_mbps:.2f} Mbps")
        
        if self.should_skip_transcode(video_info):
            self.db.add_processed_file(input_path, original_size, True, 0)
            return True, 0
        
        if self.use_gpu:
            target_codec = 'hevc_qsv'
        else:
            target_codec = 'libx265'
        
        target_width, target_height = width, height
        needs_resize = False
        
        if height >= 1080:
            if height > 1080:
                target_height = 1080
                target_width = int(width * 1080 / height)
                target_width = target_width - (target_width % 2)
                if target_width < 2:
                    target_width = 2
                needs_resize = True
                print(f"缩放分辨率: {width}x{height} -> {target_width}x{target_height}")
        
        print(f"开始转码: {input_path} (大小: {original_size / (1024*1024):.2f} MB)")

        # 临时文件写入安装目录下的 temp/（不污染视频源目录，不写系统分区）
        unique_id = f"{os.getpid()}_{int(time.time())}"
        if self.temp_dir and self.temp_dir.is_dir():
            output_path = self.temp_dir / f"{input_path.stem}_temp_{unique_id}.{self.container}"
        else:
            output_path = input_path.parent / f"{input_path.stem}_temp_{unique_id}.{self.container}"
        
        cmd = ['ffmpeg', '-i', str(input_path)]
        cmd.extend(['-c:v', target_codec])
        cmd.extend(['-maxrate', f'{self.MAX_BITRATE}'])
        cmd.extend(['-bufsize', f'{self.MAX_BITRATE * 2}'])
        
        if self.use_gpu:
            cmd.extend(['-global_quality', str(self.target_quality)])
        else:
            cmd.extend(['-crf', str(self.target_quality)])
        
        if needs_resize:
            cmd.extend(['-vf', f'scale={target_width}:{target_height}'])
        
        cmd.extend(['-threads', str(self.threads)])
        cmd.extend(['-c:a', 'copy'])
        cmd.extend(['-y', str(output_path)])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if result.returncode != 0:
                # 限制错误信息长度，避免日志爆炸
                err_msg = result.stderr[-500:] if result.stderr else 'Unknown error'
                print(f"FFmpeg 错误: {err_msg}")
                self.db.add_processed_file(input_path, original_size, False)
                # 清理可能的部分输出文件
                if output_path.exists():
                    try:
                        output_path.unlink()
                    except Exception:
                        pass
                return False, 0
        except subprocess.TimeoutExpired:
            print(f"FFmpeg 转码超时: {input_path}")
            if output_path.exists():
                try:
                    output_path.unlink()
                except Exception:
                    pass
            self.db.add_processed_file(input_path, original_size, False)
            return False, 0
        except FileNotFoundError:
            print("ffmpeg 未安装")
            self.db.add_processed_file(input_path, original_size, False)
            return False, 0
        except Exception as e:
            print(f"转码失败: {e}")
            self.db.add_processed_file(input_path, original_size, False)
            return False, 0

        converted_size = self.get_file_size(output_path)
        if converted_size is None or converted_size == 0:
            if output_path.exists():
                try:
                    output_path.unlink()
                except Exception:
                    pass
            self.db.add_processed_file(input_path, original_size, False)
            return False, 0

        print(f"转码完成: 原大小 {original_size / (1024*1024):.2f} MB, 新大小 {converted_size / (1024*1024):.2f} MB")

        saved_size = original_size - converted_size
        if converted_size < original_size:
            print(f"转码后更小，删除原文件，保留转码文件，节省 {saved_size / (1024*1024):.2f} MB")
            try:
                input_path.unlink()
                print(f"已删除原文件: {input_path}")
            except Exception as e:
                print(f"删除原文件失败: {e}")
            
            final_output_path = input_path.parent / f"{input_path.stem}.{self.container}"
            # 如果目标文件已存在，先删除
            if final_output_path.exists():
                try:
                    final_output_path.unlink()
                except Exception:
                    pass
            try:
                output_path.rename(final_output_path)
            except Exception as e:
                print(f"重命名输出文件失败: {e}")
                if output_path.exists():
                    try:
                        output_path.unlink()
                    except Exception:
                        pass
                return False, 0
            self.db.add_processed_file(input_path, original_size, True, saved_size)
            return True, saved_size
        else:
            print(f"转码后更大或相同，删除转码文件，保留原文件")
            try:
                output_path.unlink()
            except Exception:
                pass
            self.db.add_processed_file(input_path, original_size, True, 0)
            return True, 0


class FolderScanner:
    """定时扫描模式（不使用 watchdog inotify，避免 NAS 崩溃）"""
    def __init__(self, folder_path, converter, interval=60):
        self.folder_path = Path(folder_path).resolve()
        if not self.folder_path.is_dir():
            raise ValueError(f"监控路径不存在或不是目录: {self.folder_path}")
        self.converter = converter
        self.interval = max(10, interval)
        self._stop = threading.Event()

    def start(self):
        print(f"开始定时扫描: {self.folder_path} (间隔{self.interval}秒)")
        self._scan()
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            if not self._stop.is_set():
                self._scan()

    def stop(self):
        self._stop.set()

    def _scan(self):
        try:
            for item in self.folder_path.iterdir():
                try:
                    if item.is_file() and self.converter.is_video_file(item):
                        self.converter.queue_file(item)
                except Exception as e:
                    print(f"扫描文件失败: {e}")
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
