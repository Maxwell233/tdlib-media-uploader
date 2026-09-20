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


    def test_filename_number_checkbox_can_be_disabled(self):
        for kind, attribute in (
            ("video", "video_filename_numbers"),
            ("mixed", "mixed_filename_numbers"),
        ):
            dialog = gui.TargetDialog(kind)
            checkbox = getattr(dialog, attribute)
            self.assertTrue(checkbox.isEnabled())
            checkbox.setChecked(False)
            self.assertFalse(checkbox.isChecked())
            dialog.deleteLater()


    def test_video_scan_builds_all_months_once(self):
        from tdlib_media_uploader.media import legacy_video as core
        import datetime
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = []
            for month in (1, 2):
                path = root / f"{month}.mp4"
                path.write_bytes(b"video")
                items.append({"path": path, "month_key": f"2025-{month:02}", "capture_time": datetime.datetime(2025, month, 1)})
            class State:
                path = root / "state.json"
                def is_completed(self, path):
                    return False
            with patch.object(metadata, "PROJECT_DIR", root), patch.object(core, "scan_videos", return_value=[i["path"] for i in items]), patch.object(core, "build_items", return_value=(items, [])), patch.object(core, "UploadState", State), patch.object(core.cfg, "EXIFTOOL_PATH", root / "absent.exe"), patch.object(core.cfg, "VIDEO_FORCE_TEN_PER_ALBUM", False), patch.object(core, "build_album_plans", wraps=core.build_album_plans) as build:
                result = gui._scan_result("video")
                self.assertEqual(build.call_count, 1)
                self.assertEqual([g["label"] for g in result["groups"]], ["2025-01", "2025-02"])
                self.assertEqual(result["pending_files"], 2)
                self.assertEqual(result["album_count"], 2)


    def test_unreadable_videos_are_isolated_before_upload(self):
        from tdlib_media_uploader.media import legacy_video as core

        class UI:
            def __init__(self):
                self.messages = []

            def info(self, text):
                self.messages.append(("info", str(text)))

            def warning(self, text):
                self.messages.append(("warning", str(text)))

            def log(self, text):
                self.messages.append(("log", str(text)))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good.mp4"
            bad = root / "bad.mp4"
            good.write_bytes(b"good")
            bad.write_bytes(b"bad")
            items = [
                {"path": good, "capture_time": None, "month_key": "2025-01", "date_tag": "test", "fallback": False},
                {"path": bad, "capture_time": None, "month_key": "2025-01", "date_tag": "test", "fallback": False},
            ]
            ui = UI()

            def prepare(path):
                if path == bad:
                    raise RuntimeError("坏视频")

            with patch.object(core, "prepare_video", side_effect=prepare):
                skipped = core.preflight_videos(items, ui)

            self.assertEqual([record["path"] for record in skipped], [bad])
            self.assertTrue(any("坏视频" in message for level, message in ui.messages if level == "log"))


    def test_all_cache_clear_removes_persistent_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            log_dir.mkdir()
            (log_dir / "app.log").write_text("app", encoding="utf-8")
            (log_dir / "tdlib.log").write_text("tdlib", encoding="utf-8")
            with patch.object(cache_service, "CACHE_TARGETS", {"logs": ("运行日志", log_dir)}):
                removed, errors = cache_service.clear_cache(("logs",))
            self.assertEqual(removed, ["运行日志"])
            self.assertFalse(errors)
            self.assertTrue(log_dir.is_dir())
            self.assertEqual(list(log_dir.iterdir()), [])


    def test_cache_usage_counts_regular_files_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "nested"
            nested.mkdir()
            (root / "one.bin").write_bytes(b"123")
            (nested / "two.bin").write_bytes(b"4567")
            self.assertEqual(gui._cache_usage(root), (2, 7))


    def test_app_log_is_persistent_and_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_dir = root / "logs"
            log_path = log_dir / "app.log"
            with patch.object(app_logging, "LOG_DIR", log_dir), patch.object(app_logging, "APP_LOG_PATH", log_path):
                app_logging.write_app_log("WARNING", "跳过坏视频：测试.mp4", source="test")
                app_logging.write_app_log("INFO", "第二次启动仍可追加", source="test")
            text = log_path.read_text(encoding="utf-8")
            self.assertIn("跳过坏视频：测试.mp4", text)
            self.assertIn("第二次启动仍可追加", text)
