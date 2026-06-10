import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class FpkPackageValidityTests(unittest.TestCase):
    def setUp(self):
        self.fnpack = Path(os.environ.get("FN_PACK", "/data/user/work/fnpack"))

    def _clean_python_cache(self):
        for path in REPO_ROOT.rglob("__pycache__"):
            shutil.rmtree(path, ignore_errors=True)
        for path in REPO_ROOT.rglob("*.pyc"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def test_fnpack_is_available(self):
        self.assertTrue(self.fnpack.exists(), f"缺少官方 fnpack: {self.fnpack}")
        result = subprocess.run(
            [str(self.fnpack), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("fnOS", result.stdout)

    def test_required_fpk_source_files_match_fnpack_expectations(self):
        self.assertTrue((REPO_ROOT / "manifest").is_file())
        self.assertTrue((REPO_ROOT / "ICON.PNG").is_file())
        self.assertTrue((REPO_ROOT / "ICON_256.PNG").is_file())
        self.assertTrue((REPO_ROOT / "cmd" / "main").is_file())
        self.assertTrue((REPO_ROOT / "config" / "privilege").is_file())
        self.assertTrue((REPO_ROOT / "config" / "resource").is_file())
        self.assertTrue((REPO_ROOT / "app" / "ui" / "config").is_file())
        self.assertFalse((REPO_ROOT / "app" / "ui" / "config").is_dir())

    def test_repository_has_single_official_build_script(self):
        script = REPO_ROOT / "scripts" / "build_fpk.sh"
        self.assertTrue(script.is_file(), "必须提供统一构建脚本，避免手工 tar 生成无效包")
        content = script.read_text()
        self.assertIn("fnpack", content)
        self.assertIn("build", content)
        self.assertIn("__pycache__", content)

    def test_fnpack_builds_installable_layout_without_python_cache(self):
        self._clean_python_cache()
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [str(self.fnpack), "build", "-d", str(REPO_ROOT)],
                cwd=tmp,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("Packing successfully", result.stdout)

            package_path = Path(tmp) / "fpkconverter.fpk"
            self.assertTrue(package_path.exists())
            with tarfile.open(package_path) as outer:
                outer_names = set(outer.getnames())
                self.assertEqual(
                    outer_names,
                    {
                        "app.tgz",
                        "cmd",
                        "cmd/main",
                        "cmd/config_callback",
                        "cmd/config_init",
                        "cmd/install_callback",
                        "cmd/install_init",
                        "cmd/uninstall_callback",
                        "cmd/uninstall_init",
                        "cmd/upgrade_callback",
                        "cmd/upgrade_init",
                        "config",
                        "config/privilege",
                        "config/resource",
                        "ICON.PNG",
                        "ICON_256.PNG",
                        "manifest",
                    },
                )
                app_tgz = outer.extractfile("app.tgz")
                self.assertIsNotNone(app_tgz)
                app_bytes = app_tgz.read()

            app_archive = Path(tmp) / "app.tgz"
            app_archive.write_bytes(app_bytes)
            with tarfile.open(app_archive) as inner:
                inner_names = set(inner.getnames())
                self.assertIn("fpkconverter/web_server.py", inner_names)
                self.assertIn("fpkconverter/fpk_converter.py", inner_names)
                self.assertIn("ui/config", inner_names)
                self.assertFalse(any("__pycache__" in name for name in inner_names))
                self.assertFalse(any(name.endswith(".pyc") for name in inner_names))


if __name__ == "__main__":
    unittest.main()
