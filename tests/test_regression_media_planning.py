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


    def test_caption_store_reads_once_and_preserves_other_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(metadata, "PROJECT_DIR", Path(directory)):
                store = metadata.CaptionStore("image")
                store.path.write_text(json.dumps({"first": {"custom_text": "原有"}}), encoding="utf-8")
                with patch.object(store, "_load", wraps=store._load) as read:
                    for index in range(1000):
                        store.get(str(index), str(index))
                    self.assertEqual(read.call_count, 1)
                another = metadata.CaptionStore("image")
                another.set("second", base_label="2", custom_text="其他编辑")
                store.set("third", base_label="3", custom_text="本次编辑")
                saved = json.loads(store.path.read_text(encoding="utf-8"))
                self.assertEqual(set(saved), {"first", "second", "third"})
                self.assertEqual(store.get("third", "3")["custom_text"], "本次编辑")


    def test_filename_captions_use_stem_without_extension(self):
        names = metadata.filename_description(
            [Path("clip.mp4"), Path("archive.tar.gz"), Path(".hidden")],
            numbered=False,
        )
        self.assertEqual(names.splitlines(), ["clip", "archive.tar", ".hidden"])


    def test_album_key_uses_scan_snapshot_without_late_stat(self):
        root = Path("snapshot-root")
        path = root / "Day2" / "clip10.mp4"
        item = {
            "path": path,
            "scan_size": 123,
            "scan_mtime_ns": 456,
        }
        with patch.object(metadata, "file_snapshot", create=True, side_effect=AssertionError("late stat")):
            key = metadata.album_key(
                "video",
                "2026-09",
                [item],
                root=root,
            )
        self.assertEqual(len(key), 24)


    def test_album_key_rejects_path_without_scan_snapshot(self):
        path = Path("clip.mp4")
        with self.assertRaisesRegex(ValueError, "缺少扫描快照"):
            metadata.album_key("video", "2026-09", [path], root=Path("."))


    def test_unknown_media_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            metadata.path_for("audio")
        with self.assertRaises(ValueError):
            gui._target_for("audio")


    def test_mixed_scan_groups_and_splits_albums(self):
        from tdlib_media_uploader.media import legacy_mixed as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "mixed"
            first = root / "旅行"
            second = root / "工作"
            (first / "nested").mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "b.mp4").write_bytes(b"video")
            (first / "a.jpg").write_bytes(b"image")
            (first / "nested" / "c.png").write_bytes(b"image")
            (second / "x.mov").write_bytes(b"video")
            root_media = root / "root.mp4"
            root_media.write_bytes(b"video")
            state_dir = Path(directory) / "state"
            with patch.object(core.cfg, "MIXED_DIR", root), \
                    patch.object(core.cfg, "MIXED_IMAGE_EXTENSIONS", {".jpg", ".png"}), \
                    patch.object(core.cfg, "MIXED_VIDEO_EXTENSIONS", {".mp4", ".mov"}), \
                    patch.object(core.cfg, "MIXED_EXTENSIONS", {".jpg", ".png", ".mp4", ".mov"}), \
                    patch.object(core.cfg, "MIXED_ALBUM_SIZE", 2), \
                    patch.object(core.cfg, "MIXED_RESET_STATE", False), \
                    patch.object(core, "STATE_DIR", state_dir), \
                    patch.object(metadata, "PROJECT_DIR", Path(directory)):
                groups = core.scan_mixed_groups()
                self.assertEqual(core.LAST_SCAN_IGNORED_ROOT_MEDIA, [root_media])
                self.assertEqual([group["group_name"] for group in groups], ["工作", "旅行"])
                self.assertEqual(
                    [item["path"].name for item in groups[1]["items"]],
                    ["a.jpg", "b.mp4", "c.png"],
                )
                state = core.UploadState()
                plans = core.build_album_plans(groups, state)
            self.assertEqual([len(plan["items"]) for plan in plans], [1, 2, 1])
            self.assertTrue(all(plan["group_name"] in {"工作", "旅行"} for plan in plans))


    def test_mixed_contents_keep_photo_video_order(self):
        from tdlib_media_uploader.media import legacy_mixed as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo = root / "photo.jpg"
            video = root / "video.mp4"
            photo.write_bytes(b"photo")
            video.write_bytes(b"video")
            items = [
                {"path": photo, "media_kind": "image"},
                {"path": video, "media_kind": "video"},
            ]
            with patch.object(core.image_core, "input_photo", side_effect=lambda path, caption: {"@type": "photo", "caption": caption}), \
                    patch.object(core, "_mixed_input_video", side_effect=lambda item, caption: {"@type": "video", "caption": caption}):
                contents, valid, skipped = core.build_mixed_contents(items, "标题")
            self.assertFalse(skipped)
            self.assertEqual(valid, items)
            self.assertEqual([item["@type"] for item in contents], ["photo", "video"])
            self.assertEqual(contents[0]["caption"], "标题")
            self.assertEqual(contents[1]["caption"], "")


    def test_mixed_extension_conflicts_are_rejected(self):
        from tdlib_media_uploader.media import legacy_mixed as core

        with patch.object(core.cfg, "MIXED_IMAGE_EXTENSIONS", {".jpg"}), \
                patch.object(core.cfg, "MIXED_VIDEO_EXTENSIONS", {".jpg"}):
            with self.assertRaises(RuntimeError):
                core._validate_extensions()


    def test_media_scan_applies_telegram_size_limits(self):
        from tdlib_media_uploader.media import legacy_image as image_core
        from tdlib_media_uploader.media import legacy_video as video_core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            small_image = root / "small.jpg"
            large_image = root / "large.jpg"
            small_video = root / "small.mp4"
            large_video = root / "large.mp4"
            small_image.write_bytes(b"1234")
            large_image.write_bytes(b"12345")
            small_video.write_bytes(b"1234")
            large_video.write_bytes(b"12345")

            with patch.object(image_core.cfg, "IMAGE_DIR", root), \
                    patch.object(image_core.cfg, "IMAGE_EXTENSIONS", {".jpg"}), \
                    patch.object(image_core.cfg, "IMAGE_MAX_BYTES", 4), \
                    patch.object(image_core.cfg, "IMAGE_COMPRESS_OVERSIZE", False):
                self.assertEqual(image_core.scan_images(), [small_image])
                self.assertEqual(len(image_core.LAST_SCAN_SIZE_SKIPS), 1)
                self.assertEqual(image_core.LAST_SCAN_SIZE_SKIPS[0]["action"], "skip")

            with patch.object(image_core.cfg, "IMAGE_DIR", root), \
                    patch.object(image_core.cfg, "IMAGE_EXTENSIONS", {".jpg"}), \
                    patch.object(image_core.cfg, "IMAGE_MAX_BYTES", 4), \
                    patch.object(image_core.cfg, "IMAGE_COMPRESS_OVERSIZE", True):
                self.assertEqual(
                    {path.name for path in image_core.scan_images()},
                    {"large.jpg", "small.jpg"},
                )
                self.assertEqual(image_core.LAST_SCAN_SIZE_SKIPS[0]["action"], "compress")

            with patch.object(video_core.cfg, "VIDEO_DIR", root), \
                    patch.object(video_core.cfg, "VIDEO_EXTENSIONS", {".mp4"}), \
                    patch.object(video_core.cfg, "VIDEO_MAX_BYTES", 4):
                self.assertEqual(
                    {path.name for path in video_core.scan_videos()},
                    {"small.mp4", "large.mp4"},
                )
                self.assertEqual(len(video_core.LAST_SCAN_SIZE_SKIPS), 1)
                self.assertEqual(video_core.LAST_SCAN_SIZE_SKIPS[0]["path"].name, "large.mp4")
                self.assertEqual(video_core.LAST_SCAN_SIZE_SKIPS[0]["action"], "preflight")


    def test_oversize_image_compression_is_deferred_until_upload(self):
        from tdlib_media_uploader.media import legacy_image as core
        from PIL import Image

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
            original = root / "large.jpg"
            compressed = root / "compressed.jpg"
            Image.new("RGB", (20, 20), "red").save(original, format="JPEG")
            Image.new("RGB", (10, 10), "red").save(compressed, format="JPEG")
            ui = UI()
            core.IMAGE_UPLOAD_PATHS.clear()
            with patch.object(core.cfg, "IMAGE_MAX_BYTES", 10), \
                    patch.object(core.cfg, "IMAGE_COMPRESS_OVERSIZE", True), \
                    patch.object(core, "compress_image", return_value=compressed) as compress, \
                    patch.object(core, "UI", ui):
                skipped = core.preflight_images([original], ui)
                compress.assert_not_called()
                self.assertEqual(skipped, [])

                payload = core.input_photo(original, "测试")
                compress.assert_called_once_with(original)
                self.assertEqual(payload["photo"]["@type"], "inputFileLocal")
                self.assertEqual(Path(payload["photo"]["path"]), compressed)

            core.cleanup_compressed_images()


    def test_video_monthly_and_forced_grouping(self):
        from tdlib_media_uploader.media import legacy_video as core
        import datetime
        with tempfile.TemporaryDirectory() as directory:
            items = []
            for index in range(23):
                path = Path(directory) / f"{index}.mp4"
                path.write_bytes(b"video")
                month = "2025-01" if index < 12 else "2025-02"
                items.append({"path": path, "month_key": month, "capture_time": datetime.datetime(2025, 1 if index < 12 else 2, 1)})
            with patch.object(metadata, "PROJECT_DIR", Path(directory)), patch.object(core.cfg, "VIDEO_ALBUM_SIZE", 10), patch.object(core.cfg, "VIDEO_FORCE_TEN_PER_ALBUM", False):
                plans = core.build_album_plans(items)
                self.assertEqual([len(p["items"]) for p in plans], [10, 2, 10, 1])
                with patch.object(core.cfg, "VIDEO_FORCE_TEN_PER_ALBUM", True):
                    forced = core.build_album_plans(items)
                    self.assertEqual([len(p["items"]) for p in forced], [10, 10, 3])
                    self.assertEqual([p["caption"]["text"] for p in forced], ["Album 1", "Album 2", "Album 3"])


    def test_unreadable_video_does_not_abort_album_content_build(self):
        from tdlib_media_uploader.media import legacy_video as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good.mp4"
            bad = root / "bad.mp4"
            good.write_bytes(b"good")
            bad.write_bytes(b"bad")
            items = [{"path": good}, {"path": bad}]

            def build(item, caption):
                if item["path"] == bad:
                    raise RuntimeError("无法读取")
                return {"path": str(item["path"]), "caption": caption}

            with patch.object(core, "input_video", side_effect=build):
                contents, valid, skipped = core.build_video_contents(items, "标题")

            self.assertEqual(valid, [items[0]])
            self.assertEqual(contents[0]["caption"], "标题")
            self.assertEqual([record["path"] for record in skipped], [bad])
