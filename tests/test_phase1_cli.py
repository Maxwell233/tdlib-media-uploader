from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
PACKAGE_ROOT = SRC_ROOT / "tdlib_media_uploader"


class Phase1CliRemovalTest(unittest.TestCase):
    def test_legacy_setup_and_run_wrappers_are_removed(self):
        for stem in ("setup", "run"):
            for suffix in (".cmd", ".command", ".ps1", ".sh"):
                self.assertFalse(
                    (REPO_ROOT / f"{stem}{suffix}").exists(),
                    f"legacy user wrapper still exists: {stem}{suffix}",
                )

    def test_gui_is_the_only_product_entrypoint_and_keeps_packaged_self_test(self):
        gui_source = (PACKAGE_ROOT / "gui" / "main_window.py").read_text(encoding="utf-8")
        tree = ast.parse(gui_source, filename="main_window.py")
        functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertIn("main", functions)
        self.assertIn('"--self-test"', gui_source)
        self.assertIn("run_self_test", gui_source)
        spec = (REPO_ROOT / "tdlib_media_uploader.spec").read_text(encoding="utf-8")
        self.assertIn('[str(PACKAGE_DIR / "app.py")]', spec)

    def test_legacy_media_cli_entrypoints_are_removed(self):
        self.assertFalse((REPO_ROOT / "tdlib_video_app.py").exists())
        spec = (REPO_ROOT / "tdlib_media_uploader.spec").read_text(encoding="utf-8")
        self.assertNotIn('"tdlib_video_app"', spec)
        for relative in (
            "media/legacy_image.py",
            "media/legacy_mixed.py",
            "media/legacy_video.py",
        ):
            source = (PACKAGE_ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("def main", source, relative)

    def test_source_run_documentation_and_entrypoint_are_removed(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        for legacy_name in (
            "setup.cmd",
            "setup.command",
            "setup.ps1",
            "setup.sh",
            "run.cmd",
            "run.command",
            "run.ps1",
            "run.sh",
        ):
            self.assertNotIn(legacy_name, readme)
        self.assertNotIn("## 从源码运行", readme)
        self.assertNotIn("PYTHONPATH=src", readme)
        self.assertIn("应用仅支持从", readme)
        self.assertIn("仓库不再提供源码启动方式", readme)

    def test_source_module_entrypoint_is_rejected(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(SRC_ROOT)
        result = subprocess.run(
            [sys.executable, "-m", "tdlib_media_uploader.app"],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 2)
        # Windows may use backslash escapes for non-ASCII stderr when the
        # child process is captured without a UTF-8 console. Normalize those
        # escapes while preserving the same user-visible contract.
        stderr = re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda match: chr(int(match.group(1), 16)),
            result.stderr,
        )
        self.assertIn("仅支持从发布包运行", stderr)


if __name__ == "__main__":
    unittest.main()
