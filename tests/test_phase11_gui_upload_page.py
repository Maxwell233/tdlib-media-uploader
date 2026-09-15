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
from tdlib_media_uploader.gui.pages import (  # noqa: E402
    ImagePage,
    MixedPage,
    UploadPage as PackageUploadPage,
    UploadPageServices,
    VideoPage,
)


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class Phase11GuiUploadPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_package_upload_modules_are_root_free(self):
        pages_root = SRC_ROOT / "tdlib_media_uploader" / "gui" / "pages"
        for relative in ("upload.py", "video.py", "image.py", "mixed.py"):
            path = pages_root / relative
            self.assertTrue(path.is_file(), relative)
            imports = _imported_names(path)
            self.assertNotIn("gui_app", imports)
            self.assertFalse(any(name.startswith("gui_app.") for name in imports))

    def test_route_pages_bind_the_expected_media_kind(self):
        for page_type, kind in (
            (VideoPage, "video"),
            (ImagePage, "image"),
            (MixedPage, "mixed"),
        ):
            page = page_type()
            self.assertIsInstance(page, PackageUploadPage)
            self.assertEqual(page.kind, kind)
            page.deleteLater()

    def test_main_window_uses_package_page_implementations(self):
        window = gui_app.MainWindow()
        try:
            self.assertIsInstance(window.video_page, PackageUploadPage)
            self.assertIsInstance(window.image_page, PackageUploadPage)
            self.assertIsInstance(window.mixed_page, PackageUploadPage)
            self.assertEqual(type(window.video_page).__name__, "VideoPage")
            self.assertEqual(type(window.image_page).__name__, "ImagePage")
            self.assertEqual(type(window.mixed_page).__name__, "MixedPage")
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_root_upload_constructor_injects_legacy_compatibility_services(self):
        page = gui_app.UploadPage("image")
        try:
            self.assertIs(page.services.require_kind, gui_app._require_kind)
            self.assertIs(page.services.config_getter, gui_app._cfg)
            self.assertIs(page.services.target_getter, gui_app._target_for)
            self.assertIs(page.services.caption_store_factory, gui_app.CaptionStore)
        finally:
            page.deleteLater()

    def test_package_page_accepts_explicit_services_for_headless_contracts(self):
        services = UploadPageServices(
            config_getter=lambda _name, default=None: default,
            target_getter=lambda _kind: {"target_mode": "channel", "chat_id": 7},
            project_dir=Path("/tmp/package-upload-page"),
        )
        page = PackageUploadPage("mixed", services=services)
        try:
            self.assertEqual(page.kind, "mixed")
            self.assertEqual(page.chat_label.text(), "频道 · 7")
            self.assertEqual(page.topic_label.text(), "不适用（频道不使用 Topic）")
        finally:
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
