import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"


def _canonical_path(value) -> str:
    """Compare runtime paths across platform aliases used by CI.

    macOS can expose the same temporary directory as ``/var`` or
    ``/private/var``.  Windows can return an 8.3 short parent (for example
    ``RUNNER~1``) when ``Path.resolve`` is called on a synthetic executable.
    Normalize those aliases in the test without weakening runtime path
    resolution.
    """

    text = os.path.realpath(os.path.abspath(os.fspath(value)))
    if os.name == "nt":
        try:
            import ctypes

            get_long_path = ctypes.windll.kernel32.GetLongPathNameW
            get_long_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            get_long_path.restype = ctypes.c_uint32

            def expand(candidate: str) -> str | None:
                buffer = ctypes.create_unicode_buffer(32768)
                length = int(get_long_path(candidate, buffer, len(buffer)))
                if 0 < length < len(buffer):
                    return buffer.value
                return None

            expanded = expand(text)
            if expanded:
                text = expanded
            else:
                parent = expand(os.path.dirname(text))
                if parent:
                    text = os.path.join(parent, os.path.basename(text))
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    return os.path.normcase(os.path.normpath(text))


def _probe_runtime_paths(*, platform: str, frozen: bool, executable: Path, meipass: Path, home: Path | None = None):
    script = """
import json
import sys
sys.platform = %r
sys.frozen = %r
sys.executable = %r
sys._MEIPASS = %r
sys.path.insert(0, %r)
from tdlib_media_uploader.config import paths
print(json.dumps({
    "resource": str(paths.RESOURCE_DIR),
    "data": str(paths.DATA_DIR),
    "database": str(paths.TDLIB_DATABASE_DIR),
    "files": str(paths.TDLIB_FILES_DIR),
}, ensure_ascii=False))
""" % (platform, frozen, str(executable), str(meipass), str(SRC_ROOT))
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
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
        self.assertEqual(_canonical_path(result["data"]), _canonical_path(data))
        self.assertEqual(_canonical_path(result["database"]), _canonical_path(data / "telegram" / "database"))
        self.assertEqual(_canonical_path(result["files"]), _canonical_path(data / "telegram" / "files"))

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
            self.assertEqual(_canonical_path(result["data"]), _canonical_path(data))
            self.assertEqual(_canonical_path(result["database"]), _canonical_path(data / "telegram" / "database"))
            self.assertEqual(_canonical_path(result["files"]), _canonical_path(data / "telegram" / "files"))
            self.assertNotEqual(_canonical_path(result["database"]), _canonical_path(root / "_internal" / "tdlib_data"))

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
            self.assertEqual(_canonical_path(result["data"]), _canonical_path(data))
            self.assertEqual(_canonical_path(result["database"]), _canonical_path(data / "telegram" / "database"))
            self.assertEqual(_canonical_path(result["files"]), _canonical_path(data / "telegram" / "files"))

    def test_self_test_reports_tdlib_paths_under_data_dir(self):
        from tdlib_media_uploader.core.self_test import run_self_test

        output = []
        self.assertEqual(run_self_test(emit=output.append), 0)
        self.assertTrue(any("TDLib 数据库目录" in line for line in output))
        self.assertTrue(any("TDLib 文件目录" in line for line in output))
        self.assertEqual(output[-1], "SELF-TEST OK")

    def test_tdlib_parameter_payload_uses_data_telegram_paths(self):
        from tdlib_media_uploader.telegram.tdlib_common import build_tdlib_parameters

        payload = build_tdlib_parameters("test")
        data = Path(__file__).resolve().parents[1] / "data"
        self.assertEqual(
            _canonical_path(payload["database_directory"]),
            _canonical_path(data / "telegram" / "database"),
        )
        self.assertEqual(
            _canonical_path(payload["files_directory"]),
            _canonical_path(data / "telegram" / "files"),
        )


if __name__ == "__main__":
    unittest.main()
