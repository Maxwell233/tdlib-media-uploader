from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "tdlib_media_uploader"
LOWER_LAYERS = {"core", "processes", "telegram", "media", "upload", "config"}


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class ArchitectureContractTest(unittest.TestCase):
    def test_first_wave_modules_use_the_declared_package_layout(self):
        expected = {
            "core/sorting.py",
            "core/filesystem.py",
            "core/readiness.py",
            "core/concurrency.py",
            "processes/runner.py",
            "telegram/client.py",
            "telegram/auth.py",
            "telegram/target.py",
            "telegram/limits.py",
            "telegram/send_result.py",
        }
        for relative_path in expected:
            self.assertTrue(
                (PACKAGE_ROOT / relative_path).is_file(),
                f"missing declared V2 module: {relative_path}",
            )
        for module_name in ("sorting", "filesystem", "readiness", "concurrency"):
            self.assertFalse(
                (PACKAGE_ROOT / f"{module_name}.py").exists(),
                f"filesystem module escaped core/: {module_name}.py",
            )

    def test_lower_layers_never_import_gui(self):
        for layer in LOWER_LAYERS:
            root = PACKAGE_ROOT / layer
            if not root.is_dir():
                continue
            for path in root.rglob("*.py"):
                imports = _imported_names(path)
                self.assertFalse(
                    any(
                        name == "gui"
                        or name.startswith("gui.")
                        or name.startswith("tdlib_media_uploader.gui")
                        for name in imports
                    ),
                    f"{path.relative_to(PROJECT_ROOT)} imports GUI code",
                )

    def test_public_contracts_are_centralized(self):
        models = (PACKAGE_ROOT / "core" / "models.py").read_text(encoding="utf-8")
        contracts = (PACKAGE_ROOT / "contracts.py").read_text(encoding="utf-8")
        self.assertIn("class FileSnapshot", models)
        self.assertIn("class MediaItem", models)
        self.assertIn("class AlbumPlan", models)
        self.assertIn("class UploadBatchResult", models)
        self.assertIn("class ProgressEvent", models)
        self.assertIn("class UploadEngine", contracts)
        self.assertIn("class MediaStrategy", contracts)
        self.assertIn("class EventSink", contracts)
        self.assertIn("class CancelToken", contracts)


if __name__ == "__main__":
    unittest.main()
