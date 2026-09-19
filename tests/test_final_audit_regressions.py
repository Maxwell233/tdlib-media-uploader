from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from tdlib_media_uploader.core.album import album_key
from tdlib_media_uploader.core.filesystem_legacy import stable_path
from tdlib_media_uploader.gui import models, scanner
from tdlib_media_uploader.gui import history_service
from tdlib_media_uploader.gui.dialogs.scan_tools_dialog import ScanToolsDialog
from tdlib_media_uploader.gui.pages.upload import UploadPage, UploadPageServices
from tdlib_media_uploader.gui.settings import AdvancedPanel, TelegramPanel, UploadPanel
from tdlib_media_uploader.media import legacy_image


class _State:
    path = Path("/tmp/final-audit-upload-state.json")

    def is_completed(self, _item):
        return False


class _Captions:
    def __init__(self, _kind):
        pass

    def get(self, _key, default):
        return {"base_label": default, "custom_text": ""}


class FinalAuditRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_legacy_scan_result_matches_upload_page_contract(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_path = root / "照片 1.jpg"
            media_path.write_bytes(b"abc")
            snapshot = (3, media_path.stat().st_mtime_ns)
            fake_config = SimpleNamespace(activate_target=lambda _kind: None)

            def get_cfg(name, default=None):
                return {
                    "IMAGE_DIR": root,
                    "ALBUM_GROUP_SIZE": 10,
                    "IMAGE_ALBUM_CAPTION_SEPARATOR": " · ",
                }.get(name, default)

            with patch.object(scanner, "get_config", return_value=fake_config), \
                patch.object(scanner, "get_cfg", side_effect=get_cfg), \
                patch.object(scanner, "target_for", return_value={}), \
                patch.object(scanner, "CaptionStore", _Captions), \
                patch.object(legacy_image, "scan_images", return_value=[media_path]), \
                patch.object(legacy_image, "UploadState", return_value=_State()), \
                patch.object(
                    legacy_image,
                    "IMAGE_SCAN_SNAPSHOTS",
                    {stable_path(media_path): snapshot},
                ):
                result = scanner.legacy_scan_result("image")

            group = result["groups"][0]
            self.assertEqual(result["album_count"], 1)
            self.assertEqual(group["label"], "第 1 组")
            self.assertEqual(group["completed"], 0)
            self.assertEqual(group["pending"], 1)
            self.assertEqual(len(group["album_plans"]), 1)
            self.assertEqual(result["completed_paths"], [])

            services = UploadPageServices(
                config_getter=lambda _name, default=None: default,
                target_getter=lambda _kind: {"target_mode": "channel", "chat_id": 7},
                project_dir=root,
            )
            page = UploadPage("image", services=services)
            try:
                legacy_result = dict(result)
                legacy_result["groups"] = [
                    {
                        "album_key": group["album_key"],
                        "title": group["title"],
                        "subtitle": group["subtitle"],
                        "caption": {
                            "base_label": group["album_plans"][0]["caption"]["base_label"],
                            "custom_text": "",
                        },
                        "items": group["items"],
                    }
                ]
                page.set_result(legacy_result)
                self.assertIn("1 个", page.summary_label.text())
                self.assertEqual(page.tree.topLevelItemCount(), 1)
            finally:
                page.deleteLater()

    def test_preview_sizes_prefer_scan_snapshots(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "changed.jpg"
            path.write_bytes(b"x")
            item = {"path": path, "scan_size": 123}
            self.assertEqual(scanner.item_size(item), 123)
            self.assertEqual(models.item_size(item), 123)

    def test_fallback_scanner_matches_album_planner_settings_and_keys(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / f"照片 {index}.jpg" for index in range(3)]
            for path in paths:
                path.write_bytes(b"abc")
            snapshots = {
                stable_path(path): (3, path.stat().st_mtime_ns)
                for path in paths
            }
            fake_config = SimpleNamespace(activate_target=lambda _kind: None)

            def get_cfg(name, default=None):
                return {
                    "IMAGE_DIR": root,
                    "IMAGE_ALBUM_SIZE": 2,
                    "IMAGE_ALBUM_NUMBER_START": 7,
                    "IMAGE_ALBUM_CAPTION_SEPARATOR": " · ",
                }.get(name, default)

            with patch.object(scanner, "get_config", return_value=fake_config), \
                patch.object(scanner, "get_cfg", side_effect=get_cfg), \
                patch.object(scanner, "target_for", return_value={}), \
                patch.object(scanner, "CaptionStore", _Captions), \
                patch.object(legacy_image, "scan_images", return_value=paths), \
                patch.object(legacy_image, "UploadState", return_value=_State()), \
                patch.object(legacy_image, "IMAGE_SCAN_SNAPSHOTS", snapshots):
                result = scanner.legacy_scan_result("image")

            self.assertEqual(len(result["groups"]), 2)
            self.assertEqual(result["groups"][0]["album_plans"][0]["number"], 7)
            expected_key = album_key(
                "image",
                "Album 7",
                paths[:2],
                root=root,
                snapshot_provider=lambda path: snapshots[stable_path(path)],
            )
            self.assertEqual(result["groups"][0]["album_key"], expected_key)

    def test_fallback_size_limits_use_runtime_loader_names(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "oversized.jpg"
            path.write_bytes(b"x")

            def get_cfg(name, default=None):
                return {"IMAGE_MAX_BYTES": 0, "IMAGE_COMPRESS_OVERSIZE": False}.get(
                    name,
                    default,
                )

            with patch.object(scanner, "get_cfg", side_effect=get_cfg), \
                patch.object(
                    scanner,
                    "BASIC_SCAN_SNAPSHOTS",
                    {stable_path(path): (1, path.stat().st_mtime_ns)},
                ):
                accepted, skipped = scanner.apply_size_limits([path], "image")

            self.assertEqual(accepted, [])
            self.assertEqual(skipped[0]["limit"], 0)
            self.assertEqual(skipped[0]["action"], "skip")

    def test_settings_write_runtime_keys(self):
        upload = UploadPanel()
        advanced = AdvancedPanel()
        scan_tools = ScanToolsDialog()
        try:
            upload_values = upload.collect_values()
            self.assertIn(("video", "verify_all_metadata_before_upload"), upload_values)
            self.assertIn(("image", "verify_all_images_before_upload"), upload_values)
            self.assertIn(("mixed", "verify_media_before_upload"), upload_values)
            self.assertNotIn(("video", "validate_media"), upload_values)
            self.assertNotIn(("image", "validate_media"), upload_values)
            self.assertNotIn(("mixed", "validate_media"), upload_values)

            advanced_values = advanced.collect_values()
            self.assertIn(
                ("process", "ffmpeg_compression_timeout_seconds"),
                advanced_values,
            )
            self.assertNotIn(
                ("process", "ffmpeg_compress_timeout_seconds"),
                advanced_values,
            )
            self.assertIn(
                "ffmpeg_compression_timeout_seconds",
                scan_tools.process_timeouts,
            )
            self.assertNotIn("ffmpeg_compress_timeout_seconds", scan_tools.process_timeouts)
        finally:
            upload.deleteLater()
            advanced.deleteLater()
            scan_tools.deleteLater()

    def test_telegram_panel_rejects_missing_api_hash(self):
        panel = TelegramPanel()
        try:
            panel.api_id.setText("12345678")
            panel.api_hash.setText("")
            ok, message = panel.validate()
            self.assertFalse(ok)
            self.assertIn("API Hash", message)

            panel.api_hash.setText("YOUR_API_HASH")
            ok, _message = panel.validate()
            self.assertFalse(ok)
        finally:
            panel.deleteLater()

    def test_history_save_is_atomic_and_round_trips(self):
        with TemporaryDirectory() as directory:
            history_path = Path(directory) / "history.json"
            with patch.object(history_service, "HISTORY_PATH", history_path):
                history_service.save_history([{"kind": "image", "status": "ok"}])
                self.assertEqual(history_service.load_history()[0]["status"], "ok")
                self.assertEqual(list(history_path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
