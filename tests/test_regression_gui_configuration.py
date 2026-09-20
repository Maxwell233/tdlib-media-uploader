"""Offline regressions: no Telegram login or network requests."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.core import album as metadata
from tdlib_media_uploader.core import logging as app_logging
from tdlib_media_uploader.gui import main_window as gui
from tdlib_media_uploader.gui import config_service, tools, cache_service
from tdlib_media_uploader.core import filesystem_legacy as path_utils
from PySide6.QtWidgets import QApplication, QGroupBox, QHBoxLayout, QScrollArea


class ImprovementsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


    def test_settings_page_scrolls_and_groups_data_and_log_cards(self):
        window = gui.MainWindow()
        try:
            settings_index = window.sidebar_rows["settings"]
            settings_scroll = window.stack.widget(settings_index)
            self.assertIsInstance(settings_scroll, QScrollArea)
            self.assertIs(settings_scroll.widget(), window.settings_page)

            boxes = {
                box.title(): box
                for box in window.settings_page.findChildren(QGroupBox)
            }
            data_box = boxes["用户数据目录"]
            log_box = boxes["运行日志"]
            self.assertIs(data_box.parentWidget(), log_box.parentWidget())
            self.assertIsInstance(data_box.parentWidget().layout(), QHBoxLayout)
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()


    def test_scan_tools_have_a_separate_scrollable_dialog(self):
        config = gui.ConfigDialog()
        scan_tools = gui.ScanToolsDialog()
        try:
            config_boxes = {
                box.title() for box in config.findChildren(QGroupBox)
            }
            scan_boxes = {
                box.title() for box in scan_tools.findChildren(QGroupBox)
            }
            self.assertNotIn("扫描与外部工具", config_boxes)
            self.assertIn("扫描稳定性与并发", scan_boxes)
            self.assertIn("外部工具", scan_boxes)
            self.assertTrue(config.findChildren(QScrollArea))
            self.assertTrue(scan_tools.findChildren(QScrollArea))
            self.assertNotIn("exiftool_path", config.fields)
            self.assertIn("exiftool_path", scan_tools.fields)

            window = gui.MainWindow()
            try:
                self.assertTrue(hasattr(window.settings_page, "open_scan_tools"))
            finally:
                window.close()
                window.deleteLater()
                self.app.processEvents()
        finally:
            config.close()
            config.deleteLater()
            scan_tools.close()
            scan_tools.deleteLater()
            self.app.processEvents()


    def test_config_dialog_and_scan_tools_save_only_their_own_sections(self):
        config = gui.ConfigDialog()
        scan_tools = gui.ScanToolsDialog()
        try:
            with patch.object(config_service, "write_config_values", return_value="") as save, \
                    patch.object(tools, "validate_exiftool_path", return_value=""):
                config.fields["api_hash"].setText("valid-api-hash")
                config._save()
                config_values = save.call_args.args[0]
            self.assertIn(("telegram", "api_id"), config_values)
            self.assertIn(("paths", "video_dir"), config_values)
            self.assertIn(("proxy", "enabled"), config_values)
            self.assertNotIn(("paths", "exiftool_path"), config_values)
            self.assertFalse(any(section in {"scan", "process"} for section, _ in config_values))

            with patch.object(config_service, "write_config_values", return_value="") as save:
                with patch.object(tools, "validate_exiftool_path", return_value=""):
                    scan_tools._save()
                scan_values = save.call_args.args[0]
            self.assertIn(("paths", "exiftool_path"), scan_values)
            self.assertIn(("scan", "readiness_attempts"), scan_values)
            self.assertIn(("process", "exiftool_timeout_seconds"), scan_values)
            self.assertNotIn(("telegram", "api_id"), scan_values)
            self.assertNotIn(("proxy", "enabled"), scan_values)
        finally:
            config.close()
            config.deleteLater()
            scan_tools.close()
            scan_tools.deleteLater()
            self.app.processEvents()


    def test_stability_interval_honours_configured_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stable.bin"
            path.write_bytes(b"stable")
            with patch.object(path_utils, "cancelable_sleep", return_value=True) as sleep:
                result = path_utils.check_file_readiness(path, stable_interval=1.2, stable_checks=2, probe=False)
            self.assertTrue(result.ready)
            self.assertEqual(sleep.call_args.args[0], 1.2)


    def test_filesystem_error_classification_distinguishes_permanent_failures(self):
        import errno

        self.assertFalse(path_utils.is_transient_fs_error(OSError(errno.EACCES, "denied")))
        self.assertFalse(path_utils.is_transient_fs_error(OSError(errno.EINVAL, "bad argument")))
        self.assertTrue(path_utils.is_transient_fs_error(OSError(errno.EAGAIN, "try again")))
        self.assertTrue(path_utils.is_transient_fs_error(OSError("provider reset")))


    def test_gui_fallback_uses_the_shared_path_sorter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("Day2/x.100.jpg", "Day10/x.1.jpg", "Day2/x.10.jpg"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"image")
            with patch.object(gui.cfg, "IMAGE_DIR", root), \
                    patch.object(gui.cfg, "IMAGE_EXTENSIONS", {".jpg"}), \
                    patch.object(gui.cfg, "IMAGE_MAX_BYTES", 100), \
                    patch.object(gui.cfg, "IMAGE_SORT_MODE", "path"):
                paths = gui._basic_paths("image")
            self.assertEqual(
                [path_utils.relative_name(path, root) for path in paths],
                ["Day2/x.10.jpg", "Day2/x.100.jpg", "Day10/x.1.jpg"],
            )


    def test_invalid_config_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            original = '[paths]\nvideo_dir = "old"\n'
            config.write_text(original, encoding="utf-8")
            with patch.object(config_service, "CONFIG_PATH", config), patch.object(config_service, "reload_config", side_effect=["空路径", ""]):
                self.assertEqual(config_service.write_config_values({("paths", "video_dir"): ""}), "空路径")
            self.assertEqual(config.read_text(encoding="utf-8"), original)


    def test_toml_update_preserves_other_sections(self):
        text = '[telegram]\napi_id = 42\n[telegram.image]\nchat_id = -1001\n[paths]\nimage_dir = "old"\n'
        updated = gui._update_toml_value(text, "paths", "image_dir", 'C:\\中文\\"quoted"')
        parsed = gui.tomllib.loads(updated)
        self.assertEqual(parsed["telegram"]["image"]["chat_id"], -1001)
        self.assertEqual(parsed["paths"]["image_dir"], 'C:\\中文\\"quoted"')


    def test_image_preview_filter_and_scan_invalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            page = gui.UploadPage("image")
            page.set_result(self.sample_result(directory))
            self.assertEqual(page.tree.topLevelItemCount(), 1)
            album = page.tree.topLevelItem(0)
            self.assertEqual(album.childCount(), 2)  # No duplicate Album parent.
            page.pending_only.setChecked(True)
            self.assertTrue(album.child(0).isHidden())
            self.assertFalse(album.child(1).isHidden())
            page.search_edit.setText("first")
            page._filter_preview()
            self.assertTrue(album.isHidden())
            page.pending_only.setChecked(False)
            self.assertFalse(album.isHidden())
            page.tree.setCurrentItem(album.child(0))
            self.assertTrue(page.edit_caption_button.isEnabled())
            page.set_running(True)
            self.assertFalse(page.edit_caption_button.isEnabled())
            page.set_running(False)
            page.set_scanning(True)
            self.assertIsNone(page.result)
            self.assertFalse(page.start_button.isEnabled())
            page.deleteLater()


    def test_macos_mount_type_distinguishes_network_and_local_volumes(self):
        mount_output = (
            "/dev/disk3s1 on /Volumes/Local (apfs, local, journaled)\n"
            "//user@nas/share on /Volumes/Camera Share (smbfs, nodev)\n"
        )
        completed = subprocess.CompletedProcess(
            ["/sbin/mount"], 0, mount_output, ""
        )
        with patch.object(path_utils.sys, "platform", "darwin"), \
                patch.object(path_utils, "_MAC_MOUNT_CACHE", None), \
                    patch.object(path_utils, "run_cancellable_process", return_value=completed):
            self.assertFalse(path_utils.is_network_path("/Volumes/Local/clip.mp4"))
            self.assertTrue(path_utils.is_network_path("/Volumes/Camera Share/clip.mp4"))


    def sample_result(self, directory):
        files = [Path(directory) / name for name in ("first.jpg", "second.jpg")]
        for file in files:
            file.write_bytes(b"image")
        plan = {"key": "abc", "number": 1, "items": files, "pending_items": files[1:], "caption": {"base_label": "1", "custom_text": "", "text": "1"}}
        return {"groups": [{"label": "Album 1", "items": files, "completed": 1, "pending": 1, "albums": 1, "album_plans": [plan]}], "completed_paths": [path_utils.stable_path(files[0])], "total_files": 2, "total_bytes": 10, "completed_files": 1, "pending_files": 1, "album_count": 1, "core_available": True}


    def test_title_edit_keeps_tree_rows(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(metadata, "PROJECT_DIR", Path(directory)):
            page = gui.UploadPage("image")
            page.set_result(self.sample_result(directory))
            row = page.tree.topLevelItem(0)
            row.setExpanded(True)
            with patch.object(gui.QDialog, "exec", return_value=gui.QDialog.DialogCode.Accepted):
                page._edit_album(row)
            self.assertIs(page.tree.topLevelItem(0), row)
            self.assertTrue(row.isExpanded())
            self.assertTrue((Path(directory) / '.image_album_captions.json').exists())
