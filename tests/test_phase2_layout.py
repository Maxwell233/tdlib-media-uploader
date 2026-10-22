from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = PROJECT_ROOT / "tdlib_media_uploader.spec"
BUILD_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "build-platforms.yml"


def _canonical_path(value) -> str:
    return os.path.normcase(os.path.normpath(os.path.realpath(os.fspath(value))))


def _probe_runtime_paths(*, frozen: bool, executable: Path, meipass: Path):
    script = """
import json
import sys
sys.frozen = %r
sys.executable = %r
sys._MEIPASS = %r
sys.path.insert(0, %r)
from tdlib_media_uploader.config import paths
print(json.dumps({
    "resource": str(paths.RESOURCE_DIR),
    "template": str(paths.TEMPLATE_CONFIG_PATH),
    "version": str(paths.VERSION_PATH),
    "assets": str(paths.ASSETS_DIR),
    "tools": str(paths.TOOLS_DIR),
    "ffmpeg": str(paths.FFMPEG_DIR),
}, ensure_ascii=False))
""" % (frozen, str(executable), str(meipass), str(PROJECT_ROOT / "src"))
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


class Phase2PyInstallerLayoutTest(unittest.TestCase):
    def test_platform_build_workflow_runs_on_pull_requests(self):
        source = BUILD_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("  pull_request:", source)
        self.assertIn('run: python -m unittest discover -s tests -v', source)
        self.assertNotIn("build_exe.ps1", source)
        self.assertNotIn("build_macos.sh", source)
        self.assertIn("python -m PyInstaller", source)
        self.assertIn('run: |\n          $exe = (Resolve-Path -LiteralPath "dist\\TDLib Media Uploader\\TDLib Media Uploader.exe").Path', source)
        self.assertIn('"$EXECUTABLE" --self-test', source)
        self.assertIn(
            "github.event.pull_request.number || github.ref_name",
            source,
            "PR artifact names must not inherit the pull_request merge ref slash",
        )

    def _run_spec(self, *, failing_hook=None):
        hooks = ModuleType("PyInstaller.utils.hooks")
        hooks.collect_submodules = Mock(return_value=[
            "tdlib_media_uploader",
            "tdlib_media_uploader.gui.settings.upload_panel",
            "tdlib_media_uploader.media.video_prepare",
        ])
        hooks.collect_all = Mock(side_effect=lambda name: (
            [(f"/{name}/metadata.json", name)],
            [("/native/tdjson.dylib", "."),
             ("/imageio_ffmpeg/binaries/ffmpeg-osx", "imageio_ffmpeg/binaries")],
            [f"{name}.native"],
        ))
        if failing_hook:
            getattr(hooks, failing_hook).side_effect = RuntimeError("discovery failed")
        api = ModuleType("PyInstaller.building.api")
        api.COLLECT = Mock()
        api.EXE = Mock()
        osx = ModuleType("PyInstaller.building.osx")
        osx.BUNDLE = Mock()
        analysis = Mock(return_value=SimpleNamespace(
            pure=[], scripts=[], binaries=[], datas=[], zipfiles=[],
        ))
        with patch.dict(sys.modules, {
            "PyInstaller.building.api": api,
            "PyInstaller.building.osx": osx,
            "PyInstaller.utils.hooks": hooks,
        }):
            runpy.run_path(str(SPEC_PATH), init_globals={
                "SPEC": str(SPEC_PATH), "Analysis": analysis, "PYZ": Mock(),
            })
        return analysis.call_args, hooks

    def test_spec_declares_src_package_and_hidden_imports(self):
        call, hooks = self._run_spec()
        self.assertEqual(call.args, ([str(PROJECT_ROOT / "src/tdlib_media_uploader/app.py")],))
        self.assertEqual(call.kwargs["pathex"], [str(PROJECT_ROOT / "src"), str(PROJECT_ROOT)])
        hooks.collect_submodules.assert_called_once_with("tdlib_media_uploader")
        self.assertEqual(set(call.kwargs["hiddenimports"]), {
            *hooks.collect_submodules.return_value,
            "tdjson", "PIL", "imageio_ffmpeg", "tdjson.native", "imageio_ffmpeg.native",
        })
        self.assertIn(("/native/tdjson.dylib", "."), call.kwargs["binaries"])
        self.assertFalse(any("ffmpeg-osx" in source for source, _dest in call.kwargs["binaries"]))

    def test_spec_reports_package_discovery_failures(self):
        for hook in ("collect_submodules", "collect_all"):
            with self.subTest(hook=hook):
                with self.assertRaisesRegex(RuntimeError, "discovery failed"):
                    self._run_spec(failing_hook=hook)

    def test_spec_keeps_all_source_resource_destinations_explicit(self):
        call, _hooks = self._run_spec()
        resources = set(call.kwargs["datas"])
        for source, destination in (
            ("resources/default_config.toml", "resources"),
            ("VERSION", "."), ("LICENSE", "."), ("ATTRIBUTION", "."),
            ("THIRD_PARTY_LICENSES.md", "."),
            ("assets/tdlib_media_uploader_icon.ico", "assets"),
            ("assets/tdlib_media_uploader_icon.png", "assets"),
            ("tools/README.txt", "tools"),
        ):
            with self.subTest(source=source):
                self.assertIn((str(PROJECT_ROOT / source), destination), resources)
        self.assertIn(("/tdjson/metadata.json", "tdjson"), resources)
        self.assertIn(("/imageio_ffmpeg/metadata.json", "imageio_ffmpeg"), resources)

    def test_build_time_imports_resolve_repository_resources(self):
        result = _probe_runtime_paths(
            frozen=False,
            executable=Path(sys.executable),
            meipass=PROJECT_ROOT / "src",
        )
        expected = {
            "resource": PROJECT_ROOT,
            "template": PROJECT_ROOT / "resources" / "default_config.toml",
            "version": PROJECT_ROOT / "VERSION",
            "assets": PROJECT_ROOT / "assets",
            "tools": PROJECT_ROOT / "tools",
            "ffmpeg": PROJECT_ROOT / "tools" / "ffmpeg",
        }
        for key, path in expected.items():
            self.assertEqual(_canonical_path(result[key]), _canonical_path(path))
        for path in (
            Path(result["template"]),
            Path(result["version"]),
            Path(result["assets"]),
            Path(result["tools"]),
        ):
            self.assertTrue(path.exists(), path)

    def test_frozen_run_resolves_every_resource_below_meipass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            meipass = root / "TDLib Media Uploader" / "_internal"
            (meipass / "assets").mkdir(parents=True)
            (meipass / "tools" / "ffmpeg").mkdir(parents=True)
            (meipass / "resources").mkdir(parents=True)
            (meipass / "resources" / "default_config.toml").write_text("# test\n", encoding="utf-8")
            (meipass / "VERSION").write_text("2.0.0\n", encoding="utf-8")
            result = _probe_runtime_paths(
                frozen=True,
                executable=root / "TDLib Media Uploader" / "TDLib Media Uploader.exe",
                meipass=meipass,
            )
            expected = {
                "resource": meipass,
                "template": meipass / "resources" / "default_config.toml",
                "version": meipass / "VERSION",
                "assets": meipass / "assets",
                "tools": meipass / "tools",
                "ffmpeg": meipass / "tools" / "ffmpeg",
            }
            for key, path in expected.items():
                self.assertEqual(_canonical_path(result[key]), _canonical_path(path))
            self.assertEqual(Path(result["version"]).read_text(encoding="utf-8").strip(), "2.0.0")


if __name__ == "__main__":
    unittest.main()
