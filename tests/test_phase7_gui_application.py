from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.gui import application  # noqa: E402


class Phase7GuiApplicationTest(unittest.TestCase):
    def test_package_import_does_not_load_main_window(self):
        script = """
import sys
import tdlib_media_uploader.gui.application
print('tdlib_media_uploader.gui.main_window' in sys.modules)
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC_ROOT)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.stdout.strip(), "False")

    def test_self_test_dispatch_skips_root_gui(self):
        with patch.object(application.sys, "frozen", True, create=True), \
                patch.object(application.sys, "argv", ["app.py", "--self-test"]), \
                patch.object(application, "run_self_test", return_value=7) as check, \
                patch.object(application, "_load_root_main") as load_root:
            self.assertEqual(application.main(), 7)
        check.assert_called_once_with()
        load_root.assert_not_called()

    def test_packaged_dispatch_loads_root_gui_only_on_demand(self):
        root_main = lambda: 11
        with patch.object(application.sys, "frozen", True, create=True), \
                patch.object(application.sys, "argv", ["app.py"]), \
                patch.object(application, "_load_root_main", return_value=root_main) as load_root:
            self.assertEqual(application.main(), 11)
        load_root.assert_called_once_with()

    def test_source_dispatch_is_rejected(self):
        with patch.object(application.sys, "frozen", False, create=True), \
                patch.object(application.sys, "argv", ["app.py"]), \
                patch.object(application, "_load_root_main") as load_root:
            self.assertEqual(application.main(), 2)
        load_root.assert_not_called()


if __name__ == "__main__":
    unittest.main()
