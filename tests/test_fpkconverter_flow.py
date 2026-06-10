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


def load_web_server(var_dir: Path):
    os.environ["TRIM_PKGVAR"] = str(var_dir)
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
        self.web = load_web_server(self.var_dir)
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

    def test_root_browse_dynamically_exposes_existing_fnnas_volumes(self):
        existing_paths = {f"/vol{i}" for i in range(1, 6)}

        def fake_exists(path):
            if path in existing_paths:
                return True
            return self.original_exists(path)

        def fake_isdir(path):
            if path in existing_paths:
                return True
            return self.original_isdir(path)

        self.web.os.path.exists = fake_exists
        self.web.os.path.isdir = fake_isdir

        response = self.client.get("/api/browse", query_string={"path": "/"})

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        paths = {entry["path"] for entry in data["entries"]}
        for index in range(1, 6):
            self.assertIn(f"/vol{index}", paths)
        for index in range(6, 10):
            self.assertNotIn(f"/vol{index}", paths)

    def test_root_browse_includes_saved_monitor_dir_even_when_parent_is_not_listable(self):
        granted_dir = "/vol3/1000/PORN"
        self.web.cfg["monitor_dir"] = granted_dir

        def fake_exists(path):
            if path in {"/vol3", "/vol3/1000"}:
                return False
            if path == granted_dir:
                return True
            return self.original_exists(path)

        def fake_isdir(path):
            if path == granted_dir:
                return True
            if path in {"/vol3", "/vol3/1000"}:
                return False
            return self.original_isdir(path)

        self.web.os.path.exists = fake_exists
        self.web.os.path.isdir = fake_isdir

        response = self.client.get("/api/browse", query_string={"path": "/"})

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        paths = {entry["path"] for entry in data["entries"]}
        self.assertIn(granted_dir, paths)

    def test_browse_can_open_granted_leaf_directory_without_listing_parents(self):
        granted_dir = "/vol3/1000/PORN"

        def fake_listdir(path):
            if path == granted_dir:
                return ["movie.mp4"]
            raise PermissionError(path)

        def fake_isdir(path):
            if path == granted_dir:
                return True
            if path == f"{granted_dir}/movie.mp4":
                return False
            return self.original_isdir(path)

        self.web.os.listdir = fake_listdir
        self.web.os.path.isdir = fake_isdir

        response = self.client.get("/api/browse", query_string={"path": granted_dir})

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["path"], granted_dir)
        self.assertEqual(data["entries"][0]["name"], "movie.mp4")

    def test_save_config_allows_explicit_fnnas_volume_path(self):
        response = self.client.post("/api/config", json={"monitor_dir": "/vol3/1000/PORN"})

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["config"]["monitor_dir"], "/vol3/1000/PORN")

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

    def test_qsv_failure_falls_back_to_cpu_encoder_once(self):
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

        self.assertTrue(success)
        self.assertEqual(len(ffmpeg_cmds), 2)
        self.assertIn("h264_qsv", ffmpeg_cmds[0])
        self.assertIn("libx264", ffmpeg_cmds[1])

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
