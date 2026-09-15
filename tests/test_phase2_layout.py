from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
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
import runtime_paths as paths
from tdlib_media_uploader.config import paths as package_paths
print(json.dumps({
    "resource": str(paths.RESOURCE_DIR),
    "template": str(paths.TEMPLATE_CONFIG_PATH),
    "version": str(paths.VERSION_PATH),
    "assets": str(paths.ASSETS_DIR),
    "tools": str(paths.TOOLS_DIR),
    "ffmpeg": str(paths.FFMPEG_DIR),
    "package_template": str(package_paths.TEMPLATE_CONFIG_PATH),
    "package_version": str(package_paths.VERSION_PATH),
    "package_assets": str(package_paths.ASSETS_DIR),
    "package_tools": str(package_paths.TOOLS_DIR),
    "package_ffmpeg": str(package_paths.FFMPEG_DIR),
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


def _literal_strings_in_assignment(tree: ast.AST, name: str) -> set[str]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
                continue
            value = node.value
            return {
                item.value
                for item in ast.walk(value)
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            }
    raise AssertionError(f"spec assignment not found: {name}")


class Phase2PyInstallerLayoutTest(unittest.TestCase):
    def test_platform_build_workflow_runs_on_pull_requests(self):
        source = BUILD_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("  pull_request:", source)
        self.assertIn('run: python -m unittest discover -s tests -v', source)
        self.assertIn('run: .\\build_exe.ps1 -SkipInstall -Clean -PythonPath "python"', source)
        self.assertIn('run: ./build_macos.sh --clean --skip-install', source)
        self.assertIn('run: |\n          $exe = (Resolve-Path -LiteralPath "dist\\TDLib Media Uploader\\TDLib Media Uploader.exe").Path', source)
        self.assertIn('"$EXECUTABLE" --self-test', source)
        self.assertIn(
            "github.event.pull_request.number || github.ref_name",
            source,
            "PR artifact names must not inherit the pull_request merge ref slash",
        )

    def test_package_paths_facade_is_present_and_keeps_one_path_contract(self):
        package_path = PROJECT_ROOT / "src" / "tdlib_media_uploader" / "config" / "paths.py"
        self.assertTrue(package_path.is_file(), f"missing declared V2 module: {package_path}")

        src_root = PROJECT_ROOT / "src"
        if str(src_root) not in sys.path:
            sys.path.insert(0, str(src_root))
        import runtime_paths
        from tdlib_media_uploader.config import paths

        for name in (
            "RESOURCE_DIR",
            "TEMPLATE_CONFIG_PATH",
            "VERSION_PATH",
            "ASSETS_DIR",
            "TOOLS_DIR",
            "FFMPEG_DIR",
        ):
            self.assertIs(getattr(paths, name), getattr(runtime_paths, name))

    def test_spec_declares_both_source_roots_and_package_hidden_imports(self):
        source = SPEC_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(SPEC_PATH))
        package_modules = _literal_strings_in_assignment(tree, "PACKAGE_HIDDENIMPORTS")
        expected_modules = {
            "tdlib_media_uploader",
            "tdlib_media_uploader.app",
            "tdlib_media_uploader.contracts",
            "tdlib_media_uploader.core",
            "tdlib_media_uploader.core.models",
            "tdlib_media_uploader.core.sorting",
            "tdlib_media_uploader.core.filesystem",
            "tdlib_media_uploader.core.readiness",
            "tdlib_media_uploader.core.concurrency",
            "tdlib_media_uploader.config",
            "tdlib_media_uploader.config.model",
            "tdlib_media_uploader.config.loader",
            "tdlib_media_uploader.config.paths",
            "tdlib_media_uploader.processes",
            "tdlib_media_uploader.processes.runner",
            "tdlib_media_uploader.telegram",
            "tdlib_media_uploader.telegram.client",
            "tdlib_media_uploader.telegram.auth",
            "tdlib_media_uploader.telegram.target",
            "tdlib_media_uploader.telegram.limits",
            "tdlib_media_uploader.telegram.send_result",
            "tdlib_media_uploader.upload",
            "tdlib_media_uploader.upload.engine",
            "tdlib_media_uploader.upload.planner",
            "tdlib_media_uploader.upload.preflight",
        }
        self.assertTrue(expected_modules <= package_modules)
        self.assertIn("from PyInstaller.utils.hooks import collect_all, collect_submodules", source)
        self.assertIn("collect_submodules(PACKAGE_NAME)", source)
        self.assertIn("pathex=[str(PROJECT_DIR), str(SOURCE_DIR)]", source)
        self.assertIn('[str(SOURCE_DIR / PACKAGE_NAME / "app.py")]', source)

    def test_spec_keeps_all_source_resource_destinations_explicit(self):
        source = SPEC_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(SPEC_PATH))
        resource_literals = _literal_strings_in_assignment(tree, "RESOURCE_DATA")
        for name in (
            "config.example.toml",
            "default_config.toml",
            "resources",
            "VERSION",
            "LICENSE",
            "ATTRIBUTION",
            "THIRD_PARTY_LICENSES.md",
            "assets",
            "tdlib_media_uploader_icon.ico",
            "tdlib_media_uploader_icon.png",
            "tools",
            "README.txt",
        ):
            self.assertIn(name, resource_literals)
        self.assertIn('datas.append((str(ffmpeg_build_info), "tools/ffmpeg"))', source)
        self.assertIn('binaries.append((str(packaged_ffmpeg), "tools/ffmpeg"))', source)

    def test_source_run_resolves_repository_resources(self):
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
            "package_template": PROJECT_ROOT / "resources" / "default_config.toml",
            "package_version": PROJECT_ROOT / "VERSION",
            "package_assets": PROJECT_ROOT / "assets",
            "package_tools": PROJECT_ROOT / "tools",
            "package_ffmpeg": PROJECT_ROOT / "tools" / "ffmpeg",
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
                "package_template": meipass / "resources" / "default_config.toml",
                "package_version": meipass / "VERSION",
                "package_assets": meipass / "assets",
                "package_tools": meipass / "tools",
                "package_ffmpeg": meipass / "tools" / "ffmpeg",
            }
            for key, path in expected.items():
                self.assertEqual(_canonical_path(result[key]), _canonical_path(path))
            self.assertEqual(Path(result["version"]).read_text(encoding="utf-8").strip(), "2.0.0")


if __name__ == "__main__":
    unittest.main()
