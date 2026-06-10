import importlib.util
import json
import os
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
        self.client = self.web.app.test_client()

    def tearDown(self):
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
        script_path = self.var_dir / "start_converter.py"
        self.assertTrue(script_path.exists())
        py_compile = subprocess.run(
            [sys.executable, "-m", "py_compile", str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(py_compile.returncode, 0, py_compile.stderr)

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


class ConverterLogicTests(unittest.TestCase):
    def setUp(self):
        self.converter_module = load_converter_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.video = self.work / "sample.mp4"
        self.video.write_bytes(b"x" * (11 * 1000 * 1000))
        self.db = self.converter_module.Database(str(self.work / "test.db"))

    def tearDown(self):
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


class PackageEntryPointTests(unittest.TestCase):
    def test_cmd_main_starts_web_server_with_unbuffered_logs(self):
        cmd_main = REPO_ROOT / "cmd" / "main"
        content = cmd_main.read_text()

        self.assertTrue("PYTHONUNBUFFERED=1" in content or " -u web_server.py" in content)


if __name__ == "__main__":
    unittest.main()
