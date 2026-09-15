from __future__ import annotations

import ast
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class Phase1CliRemovalTest(unittest.TestCase):
    def test_legacy_setup_and_run_wrappers_are_removed(self):
        for stem in ("setup", "run"):
            for suffix in (".cmd", ".command", ".ps1", ".sh"):
                self.assertFalse(
                    (REPO_ROOT / f"{stem}{suffix}").exists(),
                    f"legacy user wrapper still exists: {stem}{suffix}",
                )

    def test_gui_is_the_only_product_source_entrypoint_and_keeps_self_test(self):
        gui_source = (REPO_ROOT / "gui_app.py").read_text(encoding="utf-8")
        tree = ast.parse(gui_source, filename="gui_app.py")
        functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertIn("main", functions)
        self.assertIn('"--self-test"', gui_source)
        self.assertIn("run_self_test", gui_source)
        spec = (REPO_ROOT / "tdlib_media_uploader.spec").read_text(encoding="utf-8")
        self.assertIn('[str(PROJECT_DIR / "gui_app.py")]', spec)

    def test_source_run_documentation_uses_direct_gui_commands(self):
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
        self.assertIn(".venv\\Scripts\\python.exe .\\gui_app.py", readme)
        self.assertIn(".venv/bin/python gui_app.py", readme)


if __name__ == "__main__":
    unittest.main()
