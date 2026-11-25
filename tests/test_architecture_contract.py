import ast
import subprocess
import sys
import unittest
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1] / "src" / "tdlib_media_uploader"


def imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield getattr(node, "module", None) or ""
            yield from (alias.name for alias in node.names)


class ArchitectureContractTest(unittest.TestCase):
    def test_lower_layers_are_qt_free_and_do_not_import_gui(self):
        for layer in ("core", "processes", "telegram", "media", "upload", "config"):
            for path in (PACKAGE / layer).rglob("*.py"):
                with self.subTest(path=path.relative_to(PACKAGE)):
                    for name in imports(path):
                        self.assertFalse(set(name.split(".")) & {"gui", "PySide6"}, name)

    def test_gui_models_and_integration_are_qt_free(self):
        for name in ("models.py", "integration.py"):
            self.assertFalse(any(value.split(".")[0] == "PySide6" for value in imports(PACKAGE / "gui" / name)))

    def test_gui_application_does_not_load_qt_on_import(self):
        subprocess.run([sys.executable, "-c", "import sys; import tdlib_media_uploader.gui.application; "
                        "assert not any(name.startswith('PySide6') for name in sys.modules)"], check=True)

    def test_gui_pages_events_and_workers_do_not_import_main_window(self):
        gui = PACKAGE / "gui"
        for path in (gui / "events.py", gui / "workers.py", *(gui / "pages").rglob("*.py")):
            self.assertFalse(any("main_window" in name.split(".") for name in imports(path)), str(path))
