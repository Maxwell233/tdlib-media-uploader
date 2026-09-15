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
    def test_phase2_package_layout_has_application_and_service_boundaries(self):
        expected = {
            "app.py",
            "config/__init__.py",
            "config/model.py",
            "config/loader.py",
            "config/paths.py",
            "gui/__init__.py",
            "media/__init__.py",
            "upload/__init__.py",
            "upload/engine.py",
            "upload/planner.py",
            "upload/preflight.py",
        }
        for relative_path in expected:
            self.assertTrue(
                (PACKAGE_ROOT / relative_path).is_file(),
                f"missing Phase 2 package boundary: {relative_path}",
            )

        app_source = (PACKAGE_ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn(
            "from tdlib_media_uploader.gui.application import main, run_self_test",
            app_source,
        )
        self.assertIn('if "--self-test" in sys.argv[1:]', (PROJECT_ROOT / "gui_app.py").read_text(encoding="utf-8"))

        default_config = PROJECT_ROOT / "resources" / "default_config.toml"
        legacy_template = PROJECT_ROOT / "config.example.toml"
        self.assertTrue(default_config.is_file(), "missing immutable default config resource")
        self.assertEqual(
            default_config.read_bytes(),
            legacy_template.read_bytes(),
            "package resource and documented config template have drifted",
        )

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

    def test_phase5_media_strategies_are_public_and_gui_free(self):
        expected = {
            "media/image.py",
            "media/mixed.py",
            "media/video.py",
        }
        for relative_path in expected:
            self.assertTrue(
                (PACKAGE_ROOT / relative_path).is_file(),
                f"missing Phase 5 strategy module: {relative_path}",
            )

        source_root = PROJECT_ROOT / "src"
        import sys

        if str(source_root) not in sys.path:
            sys.path.insert(0, str(source_root))
        from tdlib_media_uploader.media import (  # noqa: PLC0415
            ImageStrategy,
            MixedStrategy,
            VideoStrategy,
        )

        self.assertEqual(ImageStrategy.kind, "image")
        self.assertEqual(MixedStrategy.kind, "mixed")
        self.assertEqual(VideoStrategy.kind, "video")
        for path in (PACKAGE_ROOT / "media").glob("*.py"):
            if path.name == "__init__.py":
                continue
            imports = _imported_names(path)
            self.assertFalse(
                any(name == "gui" or name.startswith("gui.") for name in imports),
                f"{path.relative_to(PROJECT_ROOT)} imports GUI code",
            )

    def test_phase6_gui_integration_boundary_is_present_and_qt_free(self):
        integration = PACKAGE_ROOT / "gui" / "integration.py"
        self.assertTrue(integration.is_file(), "missing Phase 6 GUI integration boundary")
        imports = _imported_names(integration)
        self.assertFalse(
            any(name.startswith("PySide6") for name in imports),
            "V2 GUI integration adapters must remain Qt-free",
        )
        source = integration.read_text(encoding="utf-8")
        self.assertIn("class GuiCancelToken", source)
        self.assertIn("class GuiEventSink", source)
        self.assertIn("UploadEngine", source)
        self.assertIn("def run_v2_upload", source)

    def test_phase7_gui_bootstrap_is_package_owned_and_lazy(self):
        application = PACKAGE_ROOT / "gui" / "application.py"
        self.assertTrue(application.is_file(), "missing Phase 7 GUI application boundary")
        imports = _imported_names(application)
        self.assertFalse(
            any(name.startswith("PySide6") for name in imports),
            "GUI bootstrap must not import Qt at package import time",
        )
        source = application.read_text(encoding="utf-8")
        self.assertIn("def run_self_test", source)
        self.assertIn("def main", source)
        self.assertIn("from gui_app import main as root_main", source)
        self.assertIn("from self_test import run_self_test as check", source)

    def test_phase8_gui_workers_are_package_owned_and_root_free(self):
        expected = {
            "gui/events.py",
            "gui/workers.py",
        }
        for relative_path in expected:
            self.assertTrue(
                (PACKAGE_ROOT / relative_path).is_file(),
                f"missing Phase 8 GUI worker boundary: {relative_path}",
            )

        events = (PACKAGE_ROOT / "gui" / "events.py").read_text(encoding="utf-8")
        workers_path = PACKAGE_ROOT / "gui" / "workers.py"
        workers = workers_path.read_text(encoding="utf-8")
        self.assertNotIn("gui_app", events)
        self.assertNotIn("gui_app", workers)
        self.assertIn("class AuthBridge", events)
        self.assertIn("class GuiConsoleUI", events)
        self.assertIn("class ScanWorker", workers)
        self.assertIn("class UploadWorker", workers)

    def test_phase9_preview_models_are_package_owned_and_qt_free(self):
        models_path = PACKAGE_ROOT / "gui" / "models.py"
        self.assertTrue(models_path.is_file(), "missing Phase 9 GUI preview model boundary")
        imports = _imported_names(models_path)
        self.assertFalse(
            any(name.startswith("PySide6") for name in imports),
            "preview model translation must remain Qt-free",
        )
        source = models_path.read_text(encoding="utf-8")
        self.assertNotIn("gui_app", source)
        for name in (
            "def item_identity",
            "def item_dict",
            "def plan_dict",
            "def group_key",
            "def scan_result",
        ):
            self.assertIn(name, source)

    def test_phase10_page_widgets_are_package_owned_and_root_compatible(self):
        pages_root = PACKAGE_ROOT / "gui" / "pages"
        for relative_path in ("__init__.py", "home.py", "task.py"):
            self.assertTrue(
                (pages_root / relative_path).is_file(),
                f"missing Phase 10 GUI page boundary: {relative_path}",
            )
        for relative_path in ("__init__.py", "home.py", "task.py"):
            source = (pages_root / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("gui_app", source)
        root_source = (PROJECT_ROOT / "gui_app.py").read_text(encoding="utf-8")
        self.assertIn("from tdlib_media_uploader.gui.pages import (", root_source)
        self.assertIn("    HomePage,", root_source)
        self.assertIn("    TaskPage,", root_source)

    def test_phase11_upload_pages_are_package_owned_and_route_specific(self):
        pages_root = PACKAGE_ROOT / "gui" / "pages"
        expected = {
            "upload.py": ("class UploadPage", "class VideoPage", "class ImagePage", "class MixedPage"),
            "video.py": ("VideoPage",),
            "image.py": ("ImagePage",),
            "mixed.py": ("MixedPage",),
        }
        for relative_path, markers in expected.items():
            path = pages_root / relative_path
            self.assertTrue(path.is_file(), f"missing Phase 11 GUI page: {relative_path}")
            source = path.read_text(encoding="utf-8")
            imports = _imported_names(path)
            self.assertNotIn("gui_app", imports)
            self.assertFalse(any(name.startswith("gui_app.") for name in imports))
            for marker in markers:
                self.assertIn(marker, source)
        root_source = (PROJECT_ROOT / "gui_app.py").read_text(encoding="utf-8")
        self.assertIn("UploadPage as _PackageUploadPage", root_source)
        self.assertIn("_PackageVideoPage(services=page_services)", root_source)
        self.assertIn("_PackageImagePage(services=page_services)", root_source)
        self.assertIn("_PackageMixedPage(services=page_services)", root_source)

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
        self.assertIn("class UploadContext", contracts)
        self.assertIn("class EventSink", contracts)
        self.assertIn("class CancelToken", contracts)


if __name__ == "__main__":
    unittest.main()
