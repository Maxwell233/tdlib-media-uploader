from __future__ import annotations

import ast
import os
from pathlib import Path
import sys
import unittest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_app  # noqa: E402
from tdlib_media_uploader.gui.pages import HomePage, TaskPage  # noqa: E402


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class Phase10GuiPagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_package_owns_the_migrated_pages_without_root_imports(self):
        pages_root = SRC_ROOT / "tdlib_media_uploader" / "gui" / "pages"
        for relative in ("__init__.py", "home.py", "task.py"):
            self.assertTrue((pages_root / relative).is_file(), relative)
        for path in (pages_root / "__init__.py", pages_root / "home.py", pages_root / "task.py"):
            imports = _imported_names(path)
            self.assertNotIn("gui_app", imports)
            self.assertFalse(any(name.startswith("gui_app.") for name in imports))

    def test_root_gui_keeps_compatibility_exports(self):
        self.assertIs(gui_app.HomePage, HomePage)
        self.assertIs(gui_app.TaskPage, TaskPage)

    def test_home_page_keeps_scan_summary_contract(self):
        page = HomePage(app_version="test", size_formatter=lambda value: f"size={value}")
        page.update_scan({"total_files": 3, "total_bytes": 12})
        self.assertEqual(page.today_value.text(), "3 个文件 · size=12")
        page.clear_scan()
        self.assertEqual(page.today_value.text(), "—")
        page.deleteLater()

    def test_task_page_keeps_progress_and_stop_contracts(self):
        page = TaskPage(
            size_formatter=lambda value: f"size={value}",
            eta_formatter=lambda value: f"eta={value}",
        )
        page.start_session(
            "image",
            {"pending_files": 2, "pending_bytes": 20, "album_count": 1},
        )
        self.assertEqual(page.title.text(), "任务中心 · 图片")
        self.assertTrue(page.stop_button.isEnabled())
        page.show_progress(
            {
                "ratio": 0.5,
                "speed": 4,
                "eta": 7,
                "album_number": 1,
                "album_total": 1,
                "done_files": 1,
                "total_files": 2,
                "done_bytes": 10,
                "total_bytes": 20,
            }
        )
        self.assertEqual(page.progress.value(), 500)
        self.assertIn("eta=7", page.metrics.text())
        page.finish_session(False, "任务失败")
        self.assertFalse(page.stop_button.isEnabled())
        page.deleteLater()


if __name__ == "__main__":
    unittest.main()
