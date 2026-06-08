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
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


ALLOWED_PATH_PATTERN = re.compile(r'^/[A-Za-z0-9_./\-\s]+$')


class Database:
    def __init__(self, db_path=None):
        if db_path is None:
            self.db_path = 'fpk_converter.db'
        else:
            self.db_path = db_path
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
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
    MAX_CONCURRENT_TASKS = 50

    def __init__(self, db, target_quality=23, codec='libx264', container='mp4', preset='medium', threads=1, use_gpu=True):
        self.db = db
        self.target_quality = max(1, min(51, int(target_quality)))
        self.codec = codec if codec in self.ALLOWED_CODECS else 'libx265'
        self.container = container if container in self.ALLOWED_CONTAINERS else 'mp4'
        self.preset = preset if preset in self.ALLOWED_PRESETS else 'medium'
        self.threads = max(1, min(16, int(threads)))
        self.use_gpu = bool(use_gpu)
        self.pending_files = {}
        self.lock = threading.Lock()
        self.recent_events = {}
        self.event_lock = threading.Lock()

    def is_video_file(self, filepath):
        return filepath.suffix.lower() in self.VIDEO_EXTENSIONS

    def _validate_path(self, filepath):
        try:
            filepath = Path(filepath).resolve()
        except (OSError, ValueError, RuntimeError):
            return None
        if not filepath.is_file():
            return None
        return filepath

    def _deduplicate_event(self, filepath_str, debounce_seconds=5):
        with self.event_lock:
            now = time.time()
            last_time = self.recent_events.get(filepath_str, 0)
            if now - last_time < debounce_seconds:
                return False
            self.recent_events[filepath_str] = now
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
        filepath = self._validate_path(filepath)
        if not filepath:
            return
        
        filepath_str = str(filepath)
        if not self._deduplicate_event(filepath_str):
            return
        
        with self.lock:
            if len(self.pending_files) >= self.MAX_CONCURRENT_TASKS:
                print(f"待处理队列已满，跳过: {filepath}")
                return
            
            self.pending_files[filepath_str] = time.time()
            print(f"文件加入队列: {filepath_str}，等待 {self.TRANSCODE_DELAY} 秒后处理")
        
        thread = threading.Thread(target=self._delayed_process, args=(filepath,))
        thread.daemon = True
        thread.start()

    def _delayed_process(self, filepath):
        filepath_str = str(filepath)
        try:
            time.sleep(self.TRANSCODE_DELAY)
        except Exception:
            return
        
        with self.lock:
            if filepath_str not in self.pending_files:
                return
            
            try:
                queued_at = self.pending_files[filepath_str]
                if not os.path.exists(filepath):
                    print(f"文件已被删除: {filepath_str}")
                    del self.pending_files[filepath_str]
                    return
                
                mtime = os.path.getmtime(filepath)
                if mtime > queued_at:
                    print(f"文件在等待期间被修改，重新排队: {filepath_str}")
                    self.pending_files[filepath_str] = time.time()
                    threading.Thread(target=self._delayed_process, args=(filepath,)).start()
                    return
                
                del self.pending_files[filepath_str]
            except Exception as e:
                print(f"检查文件修改时间失败: {e}")
                if filepath_str in self.pending_files:
                    del self.pending_files[filepath_str]
                return
        
        try:
            print(f"开始处理: {filepath_str}")
            self.convert_video(filepath)
        except Exception as e:
            print(f"处理文件时发生未预期错误: {e}")
            traceback.print_exc()

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

        unique_id = f"{os.getpid()}_{int(time.time())}"
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
                err_msg = result.stderr[-500:] if result.stderr else 'Unknown error'
                print(f"FFmpeg 错误: {err_msg}")
                self.db.add_processed_file(input_path, original_size, False)
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


class FolderMonitor:
    def __init__(self, folder_path, converter):
        self.folder_path = Path(folder_path).resolve()
        if not self.folder_path.is_dir():
            raise ValueError(f"监控路径不存在或不是目录: {self.folder_path}")
        self.converter = converter
        self.observer = Observer()

    class Handler(FileSystemEventHandler):
        def __init__(self, converter):
            self.converter = converter

        def on_created(self, event):
            if not event.is_directory:
                self._safe_process_file(Path(event.src_path))

        def on_modified(self, event):
            if not event.is_directory:
                self._safe_process_file(Path(event.src_path))

        def _safe_process_file(self, filepath):
            try:
                if self.converter.is_video_file(filepath):
                    self.converter.queue_file(filepath)
            except Exception as e:
                print(f"处理文件事件失败: {e}")

    def start(self):
        event_handler = self.Handler(self.converter)
        self.observer.schedule(event_handler, str(self.folder_path), recursive=False)
        self.observer.start()
        print(f"开始监控文件夹: {self.folder_path}")
        print(f"延迟转码时间: {self.converter.TRANSCODE_DELAY} 秒")
        
        self._scan_existing_files()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.observer.stop()
        self.observer.join()

    def _scan_existing_files(self):
        print("扫描现有文件...")
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
        monitor = FolderMonitor(folder_path, converter)
        monitor.start()
    except KeyboardInterrupt:
        print("\n用户中断")
    except Exception as e:
        print(f"程序异常退出: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
