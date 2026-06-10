import importlib.util
import json
import os
import py_compile
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app" / "fpkconverter"


def load_web_server(var_dir: Path, accessible_paths=None):
    os.environ["TRIM_PKGVAR"] = str(var_dir)
    if accessible_paths is None:
        os.environ["TRIM_DATA_ACCESSIBLE_PATHS"] = ""
    else:
        os.environ["TRIM_DATA_ACCESSIBLE_PATHS"] = ":".join(accessible_paths)
    os.environ.setdefault("TRIM_DATA_SHARE_PATHS", "")
    module_name = f"web_server_test_{os.getpid()}_{id(var_dir)}"
    spec = importlib.util.spec_from_file_location(module_name, APP_DIR / "web_server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    return module


def load_converter_module():
    module_name = f"fpk_converter_test_{os.getpid()}"
    spec = importlib.util.spec_from_file_location(module_name, APP_DIR / "fpk_converter.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeProc:
    def __init__(self):
        self.pid = 12345
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class WebServerFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.var_dir = Path(self.tmp.name) / "var"
        self.var_dir.mkdir()
        self.monitor_dir = Path(self.tmp.name) / "media"
        self.monitor_dir.mkdir()
        # 模拟 fnOS 在"应用设置→授权目录"为本测试目录授予 rw
        self.web = load_web_server(self.var_dir, accessible_paths=[str(self.monitor_dir)])
        self.original_popen = self.web.subprocess.Popen
        self.original_getpgid = self.web.os.getpgid
        self.original_killpg = self.web.os.killpg
        self.original_listdir = self.web.os.listdir
        self.original_exists = self.web.os.path.exists
        self.original_isdir = self.web.os.path.isdir
        self.client = self.web.app.test_client()

    def tearDown(self):
        self.web.subprocess.Popen = self.original_popen
        self.web.os.getpgid = self.original_getpgid
        self.web.os.killpg = self.original_killpg
        self.web.os.listdir = self.original_listdir
        self.web.os.path.exists = self.original_exists
        self.web.os.path.isdir = self.original_isdir
        self.tmp.cleanup()

    def test_home_page_contains_all_user_buttons_and_controls(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("onclick=\"api('start')\"", html)
        self.assertIn("onclick=\"api('stop')\"", html)
        self.assertIn("onclick=\"openBrowser()\"", html)
        self.assertIn("onclick=\"saveCfg()\"", html)
        self.assertIn("onclick=\"selectDir()\"", html)
        self.assertIn("id=\"monitor_dir\"", html)
        self.assertIn("id=\"codec\"", html)
        self.assertIn("id=\"container\"", html)
        self.assertIn("id=\"process_pid\"", html)
        self.assertIn("id=\"current_activity\"", html)
        self.assertIn("id=\"last_error_text\"", html)
        self.assertIn("id=\"recent_log\"", html)
        self.assertIn("id=\"browser_path_input\"", html)
        self.assertIn("openBrowserFromInput()", html)

    def test_home_page_javascript_is_syntax_valid(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
        self.assertTrue(scripts)
        node_check = subprocess.run(
            ["node", "--check"],
            input="\n".join(scripts),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
        )
        self.assertEqual(node_check.returncode, 0, node_check.stdout)

    def test_save_config_button_endpoint_persists_valid_values(self):
        payload = {
            "monitor_dir": str(self.monitor_dir),
            "crf": 24,
            "preset": "fast",
            "threads": 3,
            "codec": "libx265",
            "container": "mkv",
            "use_gpu": False,
        }

        response = self.client.post("/api/config", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["config"]["monitor_dir"], str(self.monitor_dir))
        self.assertEqual(data["config"]["codec"], "libx265")
        config_file = self.var_dir / "config.json"
        self.assertTrue(config_file.exists())
        saved = json.loads(config_file.read_text())
        self.assertEqual(saved["container"], "mkv")

    def test_save_config_rejects_system_directories(self):
        response = self.client.post("/api/config", json={"monitor_dir": "/etc"})

        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data["success"])
        self.assertIn("系统目录", data["error"])
        self.assertEqual(self.web.cfg["monitor_dir"], "")

    def test_save_config_clamps_resource_intensive_values(self):
        payload = {
            "monitor_dir": str(self.monitor_dir),
            "crf": 1,
            "threads": 99,
        }

        response = self.client.post("/api/config", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["config"]["crf"], 18)
        self.assertEqual(data["config"]["threads"], 4)

    def test_browse_and_select_directory_flow_returns_entries(self):
        child = self.monitor_dir / "child"
        child.mkdir()
        video = self.monitor_dir / "sample.mp4"
        video.write_bytes(b"not real video")

        response = self.client.get("/api/browse", query_string={"path": str(self.monitor_dir)})

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        names = {entry["name"]: entry for entry in data["entries"]}
        self.assertTrue(names["child"]["is_dir"])
        self.assertFalse(names["sample.mp4"]["is_dir"])

    def test_browse_rejects_system_directories(self):
        response = self.client.get("/api/browse", query_string={"path": "/proc"})

        self.assertEqual(response.status_code, 403)
        data = response.get_json()
        self.assertIn("系统目录", data["error"])

    def test_root_browse_only_returns_safe_entry_points(self):
        response = self.client.get("/api/browse", query_string={"path": "/"})

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        for entry in data["entries"]:
            self.assertNotIn(entry["path"], {"/proc", "/sys", "/dev", "/etc", "/usr", "/var", "/run"})

    def test_root_browse_only_shows_authorized_paths(self):
        # 严格按官方规范：根目录视图只展示 TRIM_DATA_ACCESSIBLE_PATHS 中的目录
        response = self.client.get("/api/browse", query_string={"path": "/"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        paths = {entry["path"] for entry in data["entries"]}
        self.assertEqual(paths, {str(self.monitor_dir)})

    def test_root_browse_shows_helpful_message_when_no_grant(self):
        # 用户尚未在"应用设置→授权目录"授权时，应给出友好提示
        empty_var = Path(self.tmp.name) / "empty_var"
        empty_var.mkdir()
        empty_web = load_web_server(empty_var, accessible_paths=[])
        client = empty_web.app.test_client()
        response = client.get("/api/browse", query_string={"path": "/"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["entries"], [])
        self.assertIn("授权目录", data.get("message", ""))

    def test_browse_rejects_paths_outside_authorized_roots(self):
        # 即使是合法的非系统目录，也必须先被授权才能浏览
        response = self.client.get("/api/browse", query_string={"path": "/tmp"})
        self.assertEqual(response.status_code, 403)
        data = response.get_json()
        self.assertIn("未授权", data["error"])

    def test_browse_can_open_authorized_subdirectory(self):
        # 已授权的目录及其子目录可以浏览
        sub = self.monitor_dir / "sub"
        sub.mkdir()
        (sub / "movie.mp4").write_bytes(b"x")
        response = self.client.get("/api/browse", query_string={"path": str(sub)})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        names = {e["name"] for e in data["entries"]}
        self.assertIn("movie.mp4", names)

    def test_accessible_paths_file_is_a_runtime_fallback(self):
        # cmd/config_callback 写入文件后，web 进程下一次浏览即生效（无需重启）
        new_dir = Path(self.tmp.name) / "another"
        new_dir.mkdir()
        (self.var_dir / "accessible_paths").write_text(str(new_dir))
        response = self.client.get("/api/browse", query_string={"path": "/"})
        self.assertEqual(response.status_code, 200)
        paths = {e["path"] for e in response.get_json()["entries"]}
        self.assertIn(str(new_dir), paths)

    def test_status_endpoint_exposes_authorized_roots(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        roots = response.get_json().get("authorized_roots", [])
        self.assertIn(str(self.monitor_dir), roots)

    def test_start_stop_buttons_create_runtime_files_and_update_status(self):
        self.client.post("/api/config", json={"monitor_dir": str(self.monitor_dir)})
        fake_proc = FakeProc()
        popen_calls = []

        def fake_popen(args, cwd=None, start_new_session=None, stdout=None, stderr=None, env=None):
            popen_calls.append({
                "args": args,
                "cwd": cwd,
                "start_new_session": start_new_session,
                "stdout": stdout,
                "stderr": stderr,
                "env": env,
            })
            return fake_proc

        self.web.subprocess.Popen = fake_popen

        start_response = self.client.post("/api/start")
        status_response = self.client.get("/api/status")
        duplicate_start_response = self.client.post("/api/start")
        stop_response = self.client.post("/api/stop")
        final_status_response = self.client.get("/api/status")

        self.assertEqual(start_response.status_code, 200)
        self.assertTrue(start_response.get_json()["success"])
        self.assertTrue(status_response.get_json()["running"])
        self.assertEqual(duplicate_start_response.status_code, 200)
        self.assertFalse(duplicate_start_response.get_json()["success"])
        self.assertEqual(stop_response.status_code, 200)
        self.assertTrue(stop_response.get_json()["success"])
        self.assertFalse(final_status_response.get_json()["running"])
        self.assertTrue(fake_proc.terminated or fake_proc.killed)
        self.assertEqual(len(popen_calls), 1)
        self.assertTrue(popen_calls[0]["start_new_session"])
        self.assertTrue((self.var_dir / "start_config.json").exists())
        start_config = json.loads((self.var_dir / "start_config.json").read_text())
        self.assertEqual(start_config["max_depth"], 5)
        script_path = self.var_dir / "start_converter.py"
        self.assertTrue(script_path.exists())
        py_compile.compile(str(script_path), doraise=True)

    def test_start_button_uses_unbuffered_python_for_realtime_logs(self):
        self.client.post("/api/config", json={"monitor_dir": str(self.monitor_dir)})
        fake_proc = FakeProc()
        popen_calls = []

        def fake_popen(args, cwd=None, start_new_session=None, stdout=None, stderr=None, env=None):
            popen_calls.append({"args": args, "env": env})
            return fake_proc

        self.web.subprocess.Popen = fake_popen

        response = self.client.post("/api/start")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(len(popen_calls), 1)
        args = popen_calls[0]["args"]
        env = popen_calls[0]["env"] or {}
        self.assertTrue("-u" in args or env.get("PYTHONUNBUFFERED") == "1")

    def test_stop_button_terminates_converter_process_group(self):
        self.client.post("/api/config", json={"monitor_dir": str(self.monitor_dir)})
        fake_proc = FakeProc()
        killpg_calls = []

        def fake_popen(*args, **kwargs):
            return fake_proc

        def fake_getpgid(pid):
            return 54321

        def fake_killpg(pgid, signal_number):
            killpg_calls.append((pgid, signal_number))
            fake_proc.returncode = -signal_number

        self.web.subprocess.Popen = fake_popen
        self.web.os.getpgid = fake_getpgid
        self.web.os.killpg = fake_killpg

        start_response = self.client.post("/api/start")
        stop_response = self.client.post("/api/stop")

        self.assertTrue(start_response.get_json()["success"])
        self.assertTrue(stop_response.get_json()["success"])
        self.assertTrue(killpg_calls, "停止按钮应该终止整个转码进程组，避免 ffmpeg 残留")
        self.assertEqual(killpg_calls[0][0], 54321)

    def test_start_button_rejects_missing_monitor_directory(self):
        response = self.client.post("/api/start")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data["success"])
        self.assertIn("监控文件夹", data["error"])

    def test_start_button_rejects_system_directory_even_if_config_is_tampered(self):
        self.web.cfg["monitor_dir"] = "/etc"

        response = self.client.post("/api/start")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data["success"])
        self.assertIn("系统目录", data["error"])

    def test_logs_endpoint_returns_database_rows_and_total_saved(self):
        conn = sqlite3.connect(self.web.DB_PATH)
        try:
            conn.execute(
                "INSERT INTO processed_files(filepath,file_size,success,saved_size) VALUES(?,?,?,?)",
                ("/tmp/a.mp4", 10485760, 1, 5242880),
            )
            conn.commit()
        finally:
            conn.close()

        response = self.client.get("/api/logs")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["total_saved_mb"], 5.0)
        self.assertEqual(data["logs"][0]["filepath"], "/tmp/a.mp4")

    def test_status_endpoint_exposes_process_error_and_recent_transcode_output(self):
        self.client.post("/api/config", json={"monitor_dir": str(self.monitor_dir)})
        fake_proc = FakeProc()

        def fake_popen(*args, **kwargs):
            return fake_proc

        self.web.subprocess.Popen = fake_popen
        self.client.post("/api/start")
        Path(self.web.CONV_LOG).write_text(
            "\n".join([
                "=== 转码进程启动 ===",
                "[SERIAL] 开始处理: /media/a.mp4",
                "视频信息: 1920x1080, 编码: h264, 码率: 8.00 Mbps",
                "开始转码: /media/a.mp4 (大小: 100.00 MB)",
                "FFmpeg 错误 (返回码 1): qsv init failed",
            ]),
            encoding="utf-8",
        )

        response = self.client.get("/api/status")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("process", data)
        self.assertEqual(data["process"]["pid"], fake_proc.pid)
        self.assertTrue(data["process"]["running"])
        self.assertEqual(data["process"]["current_file"], "/media/a.mp4")
        self.assertIn("开始转码", data["process"]["current_activity"])
        self.assertIn("qsv init failed", data["process"]["last_error"])
        self.assertIn("recent_log", data)
        self.assertIn("FFmpeg 错误", "\n".join(data["recent_log"]))

    def test_status_endpoint_shows_stable_waiting_activity(self):
        self.client.post("/api/config", json={"monitor_dir": str(self.monitor_dir)})
        fake_proc = FakeProc()

        def fake_popen(*args, **kwargs):
            return fake_proc

        self.web.subprocess.Popen = fake_popen
        self.client.post("/api/start")
        Path(self.web.CONV_LOG).write_text(
            "\n".join([
                "=== 转码进程启动 ===",
                "文件已入队，等待文件稳定: /media/a.mp4，剩余约 300 秒",
            ]),
            encoding="utf-8",
        )

        response = self.client.get("/api/status")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["process"]["current_file"], "/media/a.mp4")
        self.assertIn("等待文件稳定", data["process"]["current_activity"])

    def test_status_endpoint_parses_current_file_progress(self):
        self.client.post("/api/config", json={"monitor_dir": str(self.monitor_dir)})
        fake_proc = FakeProc()

        def fake_popen(*args, **kwargs):
            return fake_proc

        self.web.subprocess.Popen = fake_popen
        self.client.post("/api/start")
        Path(self.web.CONV_LOG).write_text(
            "\n".join([
                "[SERIAL] 开始处理: /media/a.mp4",
                "转码进度: 42.5% | 当前文件: /media/a.mp4",
            ]),
            encoding="utf-8",
        )

        response = self.client.get("/api/status")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["process"]["current_file"], "/media/a.mp4")
        self.assertAlmostEqual(data["process"]["progress_percent"], 42.5)

    def test_start_rotates_large_converter_log_without_removing_state_files(self):
        self.client.post("/api/config", json={"monitor_dir": str(self.monitor_dir)})
        fake_proc = FakeProc()
        self.web.LOG_MAX_BYTES = 64
        self.web.LOG_BACKUP_COUNT = 2
        Path(self.web.CONV_LOG).write_text("旧日志\n" * 40, encoding="utf-8")
        Path(self.web.DB_PATH).write_text("db-data", encoding="utf-8")
        Path(self.web.CONFIG_PATH).write_text("config-data", encoding="utf-8")

        def fake_popen(*args, **kwargs):
            return fake_proc

        self.web.subprocess.Popen = fake_popen

        response = self.client.post("/api/start")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertTrue(Path(self.web.CONV_LOG + ".1").exists())
        self.assertEqual(Path(self.web.DB_PATH).read_text(encoding="utf-8"), "db-data")
        saved_config = json.loads(Path(self.web.CONFIG_PATH).read_text(encoding="utf-8"))
        self.assertEqual(saved_config["monitor_dir"], str(self.monitor_dir))


class ConverterLogicTests(unittest.TestCase):
    def setUp(self):
        self.converter_module = load_converter_module()
        self.original_subprocess_run = self.converter_module.subprocess.run
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.video = self.work / "sample.mp4"
        self.video.write_bytes(b"x" * (11 * 1000 * 1000))
        self.db = self.converter_module.Database(str(self.work / "test.db"))

    def tearDown(self):
        self.converter_module.subprocess.run = self.original_subprocess_run
        self.tmp.cleanup()

    def test_hevc_1080p_under_5mbps_is_skipped(self):
        vc = self.converter_module.VideoConverter(self.db)

        should_skip = vc.should_skip_transcode({
            "codec": "hevc",
            "height": 1080,
            "bit_rate": 4_900_000,
        })

        self.assertTrue(should_skip)

    def test_h264_1080p_572mbps_is_not_skipped(self):
        vc = self.converter_module.VideoConverter(self.db)

        should_skip = vc.should_skip_transcode({
            "codec": "h264",
            "height": 1080,
            "bit_rate": 5_720_000,
        })

        self.assertFalse(should_skip)

    def test_mkv_output_does_not_receive_mp4_faststart_flag(self):
        vc = self.converter_module.VideoConverter(
            self.db,
            target_quality=23,
            codec="libx265",
            container="mkv",
            preset="medium",
            use_gpu=False,
            temp_dir=str(self.work),
        )
        vc.get_video_info = lambda path: {"width": 1920, "height": 1080, "codec": "h264", "bit_rate": 8_000_000}
        captured_cmds = []

        def fake_run(cmd, stdout=None, stderr=None, timeout=None, **kwargs):
            captured_cmds.append(cmd)
            if cmd[:2] == ["ffmpeg", "-version"]:
                return subprocess.CompletedProcess(cmd, 0)
            output_path = Path(cmd[-1])
            output_path.write_bytes(b"x" * 1000)
            return subprocess.CompletedProcess(cmd, 0)

        self.converter_module.subprocess.run = fake_run

        vc.convert_video(self.video)

        ffmpeg_cmd = next(cmd for cmd in captured_cmds if cmd and cmd[0] == "ffmpeg" and "-i" in cmd)
        self.assertNotIn("+faststart", ffmpeg_cmd)

    def test_ffmpeg_command_limits_threads(self):
        vc = self.converter_module.VideoConverter(
            self.db,
            target_quality=1,
            codec="libx264",
            container="mp4",
            preset="medium",
            threads=99,
            use_gpu=False,
            temp_dir=str(self.work),
        )

        cmd = vc._build_ffmpeg_cmd(self.video, self.work / "out.mp4", "libx264", 1920, 1080, False)

        self.assertEqual(vc.target_quality, 18)
        self.assertEqual(vc.threads, 4)
        self.assertIn("-threads", cmd)
        self.assertEqual(cmd[cmd.index("-threads") + 1], "4")

    def test_transcode_stable_wait_is_300_seconds(self):
        self.assertEqual(self.converter_module.VideoConverter.TRANSCODE_DELAY, 300)

    def test_failed_file_retry_uses_cooldown_before_next_attempt(self):
        vc = self.converter_module.VideoConverter(self.db)
        now = [1000]
        vc._now = lambda: now[0]

        next_retry_at = vc._record_failure_for_retry(str(self.video))
        allowed, remaining, reason = vc._can_attempt_file(str(self.video))

        self.assertEqual(next_retry_at, 1000 + 1800)
        self.assertFalse(allowed)
        self.assertEqual(remaining, 1800)
        self.assertIn("等待重试冷却", reason)

        now[0] = next_retry_at + 1
        allowed, remaining, reason = vc._can_attempt_file(str(self.video))

        self.assertTrue(allowed)
        self.assertEqual(remaining, 0)
        self.assertEqual(reason, "")

    def test_failed_file_stops_after_three_retry_failures(self):
        vc = self.converter_module.VideoConverter(self.db)
        now = [1000]
        vc._now = lambda: now[0]

        next_retry_at = vc._record_failure_for_retry(str(self.video))
        now[0] = next_retry_at + 1
        next_retry_at = vc._record_failure_for_retry(str(self.video))
        now[0] = next_retry_at + 1
        stopped = vc._record_failure_for_retry(str(self.video))
        allowed, remaining, reason = vc._can_attempt_file(str(self.video))

        self.assertIsNone(stopped)
        self.assertFalse(allowed)
        self.assertEqual(remaining, 0)
        self.assertIn("已失败 3 次", reason)

    def test_gpu_mode_qsv_failure_stops_without_cpu_fallback(self):
        vc = self.converter_module.VideoConverter(
            self.db,
            target_quality=23,
            codec="libx264",
            container="mp4",
            preset="medium",
            use_gpu=True,
            temp_dir=str(self.work),
        )
        vc.get_video_info = lambda path: {"width": 1920, "height": 1080, "codec": "h264", "bit_rate": 8_000_000}
        ffmpeg_cmds = []

        def fake_run(cmd, stdout=None, stderr=None, timeout=None, **kwargs):
            if cmd[:2] == ["ffmpeg", "-version"]:
                return subprocess.CompletedProcess(cmd, 0)
            if cmd and cmd[0] == "ffmpeg" and "-i" in cmd:
                ffmpeg_cmds.append(cmd)
                if "h264_qsv" in cmd:
                    return subprocess.CompletedProcess(cmd, 1)
                output_path = Path(cmd[-1])
                output_path.write_bytes(b"x" * 1000)
                return subprocess.CompletedProcess(cmd, 0)
            return subprocess.CompletedProcess(cmd, 0)

        self.converter_module.subprocess.run = fake_run

        success, _ = vc.convert_video(self.video)

        self.assertFalse(success)
        self.assertEqual(len(ffmpeg_cmds), 1)
        self.assertIn("h264_qsv", ffmpeg_cmds[0])

    def test_gpu_mode_hevc_qsv_failure_does_not_use_libx265_fallback(self):
        vc = self.converter_module.VideoConverter(
            self.db,
            target_quality=23,
            codec="libx265",
            container="mp4",
            preset="slow",
            use_gpu=True,
            temp_dir=str(self.work),
        )
        vc.get_video_info = lambda path: {"width": 1920, "height": 1080, "codec": "h264", "bit_rate": 8_000_000}
        ffmpeg_cmds = []

        def fake_run(cmd, stdout=None, stderr=None, timeout=None, **kwargs):
            if cmd[:2] == ["ffmpeg", "-version"]:
                return subprocess.CompletedProcess(cmd, 0)
            if cmd and cmd[0] == "ffmpeg" and "-i" in cmd:
                ffmpeg_cmds.append(cmd)
                if "hevc_qsv" in cmd:
                    return subprocess.CompletedProcess(cmd, 187)
                output_path = Path(cmd[-1])
                output_path.write_bytes(b"x" * 1000)
                return subprocess.CompletedProcess(cmd, 0)
            return subprocess.CompletedProcess(cmd, 0)

        self.converter_module.subprocess.run = fake_run

        success, _ = vc.convert_video(self.video)

        self.assertFalse(success)
        self.assertEqual(len(ffmpeg_cmds), 1)
        self.assertIn("hevc_qsv", ffmpeg_cmds[0])
        self.assertFalse(any("libx265" in cmd for cmd in ffmpeg_cmds))

    def test_temp_ffmpeg_logs_are_capped_without_touching_database(self):
        vc = self.converter_module.VideoConverter(self.db, temp_dir=str(self.work))
        for i in range(70):
            p = self.work / f"ffmpeg_old_{i}.log"
            p.write_text("log", encoding="utf-8")
            os.utime(p, (1000 + i, 1000 + i))
        db_file = self.work / "test.db"
        before_db_size = db_file.stat().st_size

        vc._cleanup_transcode_logs(max_keep=50, max_age_seconds=10**9)

        logs = sorted(self.work.glob("ffmpeg_*.log"))
        self.assertLessEqual(len(logs), 50)
        self.assertTrue(db_file.exists())
        self.assertEqual(db_file.stat().st_size, before_db_size)

    def test_gpu_diagnostic_reports_missing_dri_device(self):
        vc = self.converter_module.VideoConverter(self.db, use_gpu=True, temp_dir=str(self.work))
        original_exists = self.converter_module.os.path.exists
        self.converter_module.os.path.exists = lambda path: False if path == "/dev/dri" else original_exists(path)
        try:
            message = vc._gpu_diagnostic_message()
        finally:
            self.converter_module.os.path.exists = original_exists

        self.assertIn("/dev/dri", message)
        self.assertIn("QSV", message)

    def test_qsv_command_explicitly_selects_render_device(self):
        vc = self.converter_module.VideoConverter(self.db, use_gpu=True, temp_dir=str(self.work))

        cmd = vc._build_ffmpeg_cmd(
            self.video,
            self.work / "out.mp4",
            "hevc_qsv",
            1920,
            1080,
            False,
            qsv_device="/dev/dri/renderD128",
        )

        self.assertIn("-qsv_device", cmd)
        self.assertEqual(cmd[cmd.index("-qsv_device") + 1], "/dev/dri/renderD128")
        self.assertLess(cmd.index("-qsv_device"), cmd.index("-i"))

    def test_qsv_environment_uses_fnnas_mediasrv_vaapi_stack(self):
        vc = self.converter_module.VideoConverter(self.db, use_gpu=True, temp_dir=str(self.work))
        original_exists = self.converter_module.os.path.exists

        def fake_exists(path):
            if path in {"/usr/trim/lib/mediasrv", "/usr/trim/lib/mediasrv/dri"}:
                return True
            return original_exists(path)

        self.converter_module.os.path.exists = fake_exists
        try:
            env = vc._ffmpeg_env()
        finally:
            self.converter_module.os.path.exists = original_exists

        self.assertEqual(env["LIBVA_DRIVER_NAME"], "iHD")
        self.assertEqual(env["LIBVA_DRIVERS_PATH"], "/usr/trim/lib/mediasrv/dri")
        self.assertIn("/usr/trim/lib/mediasrv", env["LD_LIBRARY_PATH"])

    def test_gpu_conversion_tries_qsv_device_before_stopping(self):
        vc = self.converter_module.VideoConverter(
            self.db,
            target_quality=23,
            codec="libx265",
            container="mp4",
            preset="medium",
            use_gpu=True,
            temp_dir=str(self.work),
        )
        vc.get_video_info = lambda path: {"width": 1920, "height": 1080, "codec": "h264", "bit_rate": 8_000_000}
        vc._render_devices = lambda: ["/dev/dri/renderD128"]
        ffmpeg_cmds = []

        def fake_run(cmd, stdout=None, stderr=None, timeout=None, env=None, **kwargs):
            if "-version" in cmd:
                return subprocess.CompletedProcess(cmd, 0)
            if cmd and "-i" in cmd:
                ffmpeg_cmds.append(cmd)
                output_path = Path(cmd[-1])
                output_path.write_bytes(b"x" * 1000)
                return subprocess.CompletedProcess(cmd, 0)
            return subprocess.CompletedProcess(cmd, 0)

        self.converter_module.subprocess.run = fake_run

        success, _ = vc.convert_video(self.video)

        self.assertTrue(success)
        self.assertEqual(len(ffmpeg_cmds), 1)
        self.assertIn("-qsv_device", ffmpeg_cmds[0])
        self.assertEqual(ffmpeg_cmds[0][ffmpeg_cmds[0].index("-qsv_device") + 1], "/dev/dri/renderD128")

    def test_temp_output_filename_is_short_and_safe(self):
        weird_video = self.work / ("A" * 120 + " @[] 中文 spaces.mp4")
        weird_video.write_bytes(b"x" * (11 * 1000 * 1000))
        vc = self.converter_module.VideoConverter(
            self.db,
            target_quality=23,
            codec="libx265",
            container="mkv",
            preset="medium",
            use_gpu=False,
            temp_dir=str(self.work),
        )
        vc.get_video_info = lambda path: {"width": 1920, "height": 1080, "codec": "h264", "bit_rate": 8_000_000}
        ffmpeg_outputs = []

        def fake_run(cmd, stdout=None, stderr=None, timeout=None, **kwargs):
            if cmd[:2] == ["ffmpeg", "-version"]:
                return subprocess.CompletedProcess(cmd, 0)
            if cmd and cmd[0] == "ffmpeg" and "-i" in cmd:
                output_path = Path(cmd[-1])
                ffmpeg_outputs.append(output_path)
                output_path.write_bytes(b"x" * 1000)
                return subprocess.CompletedProcess(cmd, 0)
            return subprocess.CompletedProcess(cmd, 0)

        self.converter_module.subprocess.run = fake_run

        vc.convert_video(weird_video)

        self.assertEqual(len(ffmpeg_outputs), 1)
        temp_name = ffmpeg_outputs[0].name
        self.assertLessEqual(len(temp_name), 120)
        for bad_char in " @[]":
            self.assertNotIn(bad_char, temp_name)


class PackageEntryPointTests(unittest.TestCase):
    def test_cmd_main_starts_web_server_with_unbuffered_logs(self):
        cmd_main = REPO_ROOT / "cmd" / "main"
        content = cmd_main.read_text()

        self.assertTrue("PYTHONUNBUFFERED=1" in content or " -u web_server.py" in content)

    def test_web_server_signal_handler_cleans_process_group(self):
        content = (APP_DIR / "web_server.py").read_text()
        signal_block = content.split("def _sigterm", 1)[1]

        self.assertIn("os.killpg", signal_block)
        self.assertIn("SIGTERM", signal_block)


if __name__ == "__main__":
    unittest.main()
