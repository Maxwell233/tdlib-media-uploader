"""Long-lived dependency boundaries, independent of filenames from past refactors."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "tdlib_media_uploader"
LOWER_LAYERS = {"core", "processes", "telegram", "media", "upload", "config"}


def _references(path: Path) -> set[str]:
    """Include `from .. import gui` and literal dynamic module references."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module or ''}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if all(part.isidentifier() for part in node.value.split(".")):
                names.add(node.value)
    return names


class ArchitectureContractTest(unittest.TestCase):
    def test_lower_layers_never_import_gui(self):
        for layer in LOWER_LAYERS:
            for path in (PACKAGE_ROOT / layer).rglob("*.py"):
                with self.subTest(path=str(path.relative_to(PACKAGE_ROOT))):
                    self.assertFalse(any("gui" in name.split(".") for name in _references(path)))

    def test_gui_components_do_not_reference_main_window(self):
        # Only application.py owns the lazy bootstrap of the main window.
        for path in (PACKAGE_ROOT / "gui").rglob("*.py"):
            if path.name in {"main_window.py", "application.py"}:
                continue
            with self.subTest(path=str(path.relative_to(PACKAGE_ROOT))):
                self.assertFalse(any("main_window" in name.split(".") for name in _references(path)))

    def test_headless_gui_adapters_do_not_import_qt(self):
        for name in ("application", "integration", "models"):
            with self.subTest(module=name):
                self.assertFalse(any(ref.startswith("PySide6") for ref in _references(PACKAGE_ROOT / "gui" / f"{name}.py")))


if __name__ == "__main__":
    unittest.main()
