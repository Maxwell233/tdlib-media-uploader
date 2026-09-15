import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _probe_runtime_paths(*, platform: str, frozen: bool, executable: Path, meipass: Path, home: Path | None = None):
    script = """
import json
import sys
sys.platform = %r
sys.frozen = %r
sys.executable = %r
sys._MEIPASS = %r
import runtime_paths as paths
print(json.dumps({
    "resource": str(paths.RESOURCE_DIR),
    "data": str(paths.DATA_DIR),
    "database": str(paths.TDLIB_DATABASE_DIR),
    "files": str(paths.TDLIB_FILES_DIR),
}, ensure_ascii=False))
""" % (platform, frozen, str(executable), str(meipass))
    env = os.environ.copy()
    if home is not None:
        # Path.home() uses USERPROFILE on Windows even when sys.platform is
        # overridden for this isolated probe.
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env["HOMEDRIVE"] = ""
        env["HOMEPATH"] = ""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


class DataLayoutTest(unittest.TestCase):
    def test_source_run_uses_repository_data_telegram_directories(self):
        result = _probe_runtime_paths(
            platform=sys.platform,
            frozen=False,
            executable=Path(sys.executable),
            meipass=PROJECT_ROOT,
        )
        data = PROJECT_ROOT / "data"
        self.assertEqual(Path(result["data"]), data)
        self.assertEqual(Path(result["database"]), data / "telegram" / "database")
        self.assertEqual(Path(result["files"]), data / "telegram" / "files")

    def test_frozen_windows_uses_executable_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = _probe_runtime_paths(
                platform="win32",
                frozen=True,
                executable=root / "TDLib Media Uploader.exe",
                meipass=root / "_internal",
            )
            data = root / "data"
            self.assertEqual(Path(result["data"]), data)
            self.assertEqual(Path(result["database"]), data / "telegram" / "database")
            self.assertEqual(Path(result["files"]), data / "telegram" / "files")
            self.assertNotEqual(Path(result["database"]), root / "_internal" / "tdlib_data")

    def test_frozen_macos_uses_application_support_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            result = _probe_runtime_paths(
                platform="darwin",
                frozen=True,
                executable=Path(directory) / "App.app" / "Contents" / "MacOS" / "TDLib Media Uploader",
                meipass=Path(directory) / "App.app" / "Contents" / "Resources",
                home=home,
            )
            data = home / "Library" / "Application Support" / "TDLib Media Uploader" / "data"
            self.assertEqual(Path(result["data"]), data)
            self.assertEqual(Path(result["database"]), data / "telegram" / "database")
            self.assertEqual(Path(result["files"]), data / "telegram" / "files")

    def test_self_test_reports_tdlib_paths_under_data_dir(self):
        from self_test import run_self_test

        output = []
        self.assertEqual(run_self_test(emit=output.append), 0)
        self.assertTrue(any("TDLib 数据库目录" in line for line in output))
        self.assertTrue(any("TDLib 文件目录" in line for line in output))
        self.assertEqual(output[-1], "SELF-TEST OK")

    def test_tdlib_parameter_payload_uses_data_telegram_paths(self):
        from tdlib_common import build_tdlib_parameters

        payload = build_tdlib_parameters("test")
        data = Path(__file__).resolve().parents[1] / "data"
        self.assertEqual(
            Path(payload["database_directory"]),
            (data / "telegram" / "database").resolve(),
        )
        self.assertEqual(
            Path(payload["files_directory"]),
            (data / "telegram" / "files").resolve(),
        )


if __name__ == "__main__":
    unittest.main()
