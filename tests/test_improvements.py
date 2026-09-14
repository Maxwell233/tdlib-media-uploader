"""Offline regressions: no Telegram login or network requests."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import album_metadata as metadata
import app_logging
import gui_app as gui
import path_utils
from PySide6.QtWidgets import QApplication


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

    def test_mixed_scan_groups_and_splits_albums(self):
        import tdlib_mixed_album_uploader as core

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
        import tdlib_mixed_album_uploader as core

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

    def test_exiftool_empty_output_is_a_nonfatal_empty_result(self):
        import subprocess
        import tdlib_video_album_uploader as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "exiftool"
            executable.write_bytes(b"tool")
            completed = subprocess.CompletedProcess([], 0, "", "")
            with patch.object(core.cfg, "EXIFTOOL_PATH", executable), \
                    patch.object(core.cfg, "VIDEO_DIR", root), \
                    patch.object(core.cfg, "VIDEO_EXTENSIONS", {".mp4"}), \
                    patch.object(core.cfg, "VIDEO_READ_MEDIA_CREATION_DATE", True), \
                    patch.object(core.subprocess, "run", return_value=completed):
                self.assertEqual(core.read_exif_metadata(), {})

    def test_invalid_config_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            original = '[paths]\nvideo_dir = "old"\n'
            config.write_text(original, encoding="utf-8")
            with patch.object(gui, "CONFIG_PATH", config), patch.object(gui, "_reload_config", side_effect=["空路径", ""]):
                self.assertEqual(gui._write_config_values({("paths", "video_dir"): ""}), "空路径")
            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_toml_update_preserves_other_sections(self):
        text = '[telegram]\napi_id = 42\n[telegram.image]\nchat_id = -1001\n[paths]\nimage_dir = "old"\n'
        updated = gui._update_toml_value(text, "paths", "image_dir", 'C:\\中文\\"quoted"')
        parsed = gui.tomllib.loads(updated)
        self.assertEqual(parsed["telegram"]["image"]["chat_id"], -1001)
        self.assertEqual(parsed["paths"]["image_dir"], 'C:\\中文\\"quoted"')

    def sample_result(self, directory):
        files = [Path(directory) / name for name in ("first.jpg", "second.jpg")]
        for file in files:
            file.write_bytes(b"image")
        plan = {"key": "abc", "number": 1, "items": files, "pending_items": files[1:], "caption": {"base_label": "1", "custom_text": "", "text": "1"}}
        return {"groups": [{"label": "Album 1", "items": files, "completed": 1, "pending": 1, "albums": 1, "album_plans": [plan]}], "completed_paths": [path_utils.stable_path(files[0])], "total_files": 2, "total_bytes": 10, "completed_files": 1, "pending_files": 1, "album_count": 1, "core_available": True}

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

    def test_image_plan_preserves_complete_groups_and_custom_titles(self):
        import tdlib_image_album_uploader as core
        with tempfile.TemporaryDirectory() as directory:
            files = [Path(directory) / f"{i}.jpg" for i in range(23)]
            for file in files:
                file.write_bytes(b"image")
            class State:
                def is_completed(self, path):
                    return path in files[:10]
            with patch.object(metadata, "PROJECT_DIR", Path(directory)), patch.object(core.cfg, "IMAGE_ALBUM_SIZE", 10):
                plans = core.build_album_plans(files, State())
                self.assertEqual([len(p["items"]) for p in plans], [10, 10, 3])
                self.assertEqual([len(p["pending_items"]) for p in plans], [0, 10, 3])
                store = metadata.CaptionStore("image")
                store.set(plans[1]["key"], base_label="2", custom_text="旅行")
                again = core.build_album_plans(files, State())
                self.assertEqual(plans[1]["key"], again[1]["key"])
                self.assertIn("旅行", again[1]["caption"]["text"])

    def test_directory_scan_uses_one_stat_per_matching_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.jpg").write_bytes(b"image")
            (root / "empty.jpg").write_bytes(b"")
            (root / "other.txt").write_text("text")
            original = Path.stat
            calls = []
            def counted(path, *args, **kwargs):
                calls.append(str(path))
                return original(path, *args, **kwargs)
            with patch.object(Path, "stat", counted):
                files, errors = path_utils.iter_files(root, {".jpg"})
            self.assertEqual([p.name for p in files], ["a.jpg"])
            self.assertFalse(errors)
            self.assertEqual(len(calls), 0)

    def test_file_mtime_uses_os_stat_and_keeps_fallback(self):
        result = SimpleNamespace(st_mtime=123.5)
        with patch.object(path_utils.os, "stat", return_value=result) as stat:
            self.assertEqual(path_utils.file_mtime("clip.mp4"), 123.5)
            stat.assert_called_once_with("clip.mp4")

        with patch.object(path_utils.os, "stat", side_effect=OSError("share offline")):
            self.assertEqual(path_utils.file_mtime("clip.mp4", fallback=7.25), 7.25)

    def test_extension_filter_preserves_path_suffix_edge_cases(self):
        self.assertEqual(path_utils._entry_suffix("photo."), "")
        self.assertEqual(path_utils._entry_suffix("photo.."), "")
        self.assertEqual(path_utils._entry_suffix(".hidden"), "")
        self.assertEqual(path_utils._entry_suffix("photo.jpg"), ".jpg")

    def test_media_scan_applies_telegram_size_limits(self):
        import tdlib_image_album_uploader as image_core
        import tdlib_video_album_uploader as video_core

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
                self.assertEqual(video_core.scan_videos(), [small_video])
                self.assertEqual(len(video_core.LAST_SCAN_SIZE_SKIPS), 1)

    def test_oversize_image_compression_is_deferred_until_upload(self):
        import tdlib_image_album_uploader as core
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
        import tdlib_video_album_uploader as core
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

    def test_video_date_priority_and_optional_media_date(self):
        import tdlib_video_album_uploader as core

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(b"video")
            os.utime(path, (1784935138, 1784935138))
            row = {
                "Keys:CreationDate": "2024-06-29 05:48:00+0000",
                "ExifIFD:DateTimeOriginal": "2023:05:02 10:20:30",
            }
            with patch.object(core.cfg, "VIDEO_MISSING_DATE_POLICY", "mtime"), \
                    patch.object(core.cfg, "VIDEO_QUICKTIME_UTC_TARGET_ZONE", None), \
                    patch.object(core.cfg, "VIDEO_READ_MEDIA_CREATION_DATE", True):
                selected = core.choose_capture_time(path, row)
                self.assertEqual(selected["tag"], "ExifIFD:DateTimeOriginal")
                self.assertEqual(selected["datetime"].year, 2023)

                media = core.choose_capture_time(
                    path,
                    {"QuickTime:MediaCreateDate": "2024-06-29 05:48:00+0000"},
                )
                self.assertEqual(media["tag"], "QuickTime:MediaCreateDate")
                self.assertFalse(media["fallback"])

            with patch.object(core.cfg, "VIDEO_MISSING_DATE_POLICY", "mtime"), \
                    patch.object(core.cfg, "VIDEO_READ_MEDIA_CREATION_DATE", False):
                fallback = core.choose_capture_time(
                    path,
                    {"QuickTime:MediaCreateDate": "2024-06-29 05:48:00+0000"},
                )
                self.assertEqual(fallback["tag"], "FileSystem:ModifyTime")
                self.assertEqual(fallback["datetime"].timestamp(), path.stat().st_mtime)

    def test_disabled_video_dates_uses_filename_only(self):
        import tdlib_video_album_uploader as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "b.mp4"
            second = root / "a.mp4"
            first.write_bytes(b"video")
            second.write_bytes(b"video")
            os.utime(first, (200, 200))
            os.utime(second, (100, 100))
            with patch.object(core.cfg, "VIDEO_DIR", root), \
                    patch.object(core.cfg, "VIDEO_EXTENSIONS", {".mp4"}), \
                    patch.object(core.cfg, "VIDEO_MAX_BYTES", 10_000), \
                    patch.object(core.cfg, "VIDEO_READ_DATES", False), \
                    patch.object(core.cfg, "VIDEO_SORT_MODE", "mtime"), \
                    patch.object(core.cfg, "VIDEO_GROUP_MODE", "date"), \
                    patch.object(core, "read_media_creation_time", side_effect=AssertionError("不应读取媒体日期")):
                paths = core.scan_videos()
                items, missing = core.build_items(paths, {})

            self.assertEqual([path.name for path in paths], ["a.mp4", "b.mp4"])
            self.assertEqual(missing, [])
            self.assertEqual([item["capture_time"] for item in items], [None, None])
            with patch.object(core.cfg, "VIDEO_READ_DATES", False), \
                    patch.object(core.cfg, "VIDEO_GROUP_MODE", "date"), \
                    patch.object(core, "STATE_DIR", root / "state"), \
                    patch.object(core.cfg, "VIDEO_RESET_STATE", False):
                self.assertTrue(core.force_ten_per_album())
                self.assertEqual({item["month_key"] for item in items}, {core.FORCED_GROUP_KEY})
                plans = core.build_album_plans(items)
                self.assertEqual([len(plan["items"]) for plan in plans], [2])
                state = core.UploadState()
                state.mark_album_completed(items, [1, 2])
                saved = json.loads(state.path.read_text(encoding="utf-8"))
            self.assertTrue(all(record["capture_time"] is None for record in saved["completed"].values()))

    def test_filename_only_video_list_handles_missing_dates(self):
        import tdlib_video_app as entry
        import tdlib_video_album_uploader as core

        class State:
            @staticmethod
            def is_completed(_path):
                return False

        class UI:
            def __init__(self):
                self.messages = []

            def info(self, text):
                self.messages.append(str(text))

            def files(self, *args, **kwargs):
                self.messages.append(kwargs.get("caption", ""))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(b"video")
            item = {
                "path": path,
                "capture_time": None,
                "month_key": core.FORCED_GROUP_KEY,
                "date_tag": "未读取日期",
                "fallback": False,
            }
            ui = UI()
            with patch.object(core.cfg, "VIDEO_READ_DATES", False), patch.object(entry, "UI", ui):
                entry.show_file_list([item], State())
            self.assertTrue(any("未读取日期" in message for message in ui.messages))

    def test_jit_revalidation_detects_video_changes_after_scan(self):
        import tdlib_video_album_uploader as core

        class UI:
            def warning(self, _text):
                pass

            def log(self, _text):
                pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(b"video")
            with patch.object(core.cfg, "VIDEO_MISSING_DATE_POLICY", "mtime"), \
                    patch.object(core.cfg, "VIDEO_READ_MEDIA_CREATION_DATE", False):
                items, _missing = core.build_items([path], {})
            path.write_bytes(b"video changed")
            with patch.object(core, "prepare_video") as prepare:
                skipped = core.preflight_videos(items, UI())
            prepare.assert_not_called()
            self.assertEqual(skipped[0]["category"], "deferred")

    def test_exiftool_date_query_keeps_full_time_batch(self):
        import subprocess
        import tdlib_video_album_uploader as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "exiftool"
            executable.write_bytes(b"tool")
            output = json.dumps([{"SourceFile": str(root / "clip.mp4")}])
            completed = subprocess.CompletedProcess([], 0, output, "")
            with patch.object(core.cfg, "EXIFTOOL_PATH", executable), \
                    patch.object(core.cfg, "VIDEO_DIR", root), \
                    patch.object(core.cfg, "VIDEO_EXTENSIONS", {".mp4"}), \
                    patch.object(core.cfg, "VIDEO_READ_MEDIA_CREATION_DATE", True), \
                    patch.object(core.subprocess, "run", return_value=completed) as run:
                core.read_exif_metadata()
                command = run.call_args.args[0]
                self.assertIn("-charset", command)
                self.assertIn("FileName=UTF8", command)
                self.assertEqual(command[-2:], ["-@", "-"])
                self.assertEqual(run.call_args.kwargs["input"], f"{root}\n")
                self.assertNotIn(str(root), command)
                self.assertIn("-time:all", command)
                self.assertNotIn("-fast", command)

            with patch.object(core.cfg, "EXIFTOOL_PATH", executable), \
                    patch.object(core.cfg, "VIDEO_DIR", root), \
                    patch.object(core.cfg, "VIDEO_EXTENSIONS", {".mp4"}), \
                    patch.object(core.cfg, "VIDEO_READ_MEDIA_CREATION_DATE", False), \
                    patch.object(core.subprocess, "run", return_value=completed) as run:
                core.read_exif_metadata()
                command = run.call_args.args[0]
                self.assertIn("-time:all", command)

    def test_build_items_probes_missing_media_dates_with_four_workers(self):
        import datetime
        import threading
        import time
        import tdlib_video_album_uploader as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for index in range(8):
                path = root / f"{index}.mp4"
                path.write_bytes(b"video")
                paths.append(path)

            state = {"active": 0, "maximum": 0, "calls": 0}
            lock = threading.Lock()

            def probe(_path):
                with lock:
                    state["active"] += 1
                    state["calls"] += 1
                    state["maximum"] = max(state["maximum"], state["active"])
                time.sleep(0.02)
                with lock:
                    state["active"] -= 1
                return (
                    datetime.datetime(2024, 6, 29, 5, 48, tzinfo=datetime.timezone.utc),
                    "Media:creation_time",
                    True,
                )

            progress = []
            with patch.object(core.cfg, "VIDEO_MISSING_DATE_POLICY", "mtime"), \
                    patch.object(core.cfg, "VIDEO_READ_MEDIA_CREATION_DATE", True), \
                    patch.object(core, "read_media_creation_time", side_effect=probe):
                items, missing = core.build_items(paths, {}, progress_callback=progress.append)

            self.assertEqual(len(items), len(paths))
            self.assertFalse(missing)
            self.assertEqual(state["calls"], len(paths))
            self.assertGreater(state["maximum"], 1)
            self.assertLessEqual(state["maximum"], core.MEDIA_DATE_MAX_WORKERS)
            self.assertEqual(progress[-1]["phase"], "media_date")
            self.assertEqual(progress[-1]["completed"], len(paths))

    def test_embedded_date_does_not_probe_ffmpeg(self):
        import tdlib_video_album_uploader as core

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(b"video")
            row = {"ExifIFD:DateTimeOriginal": "2023:05:02 10:20:30"}
            with patch.object(core.cfg, "VIDEO_READ_MEDIA_CREATION_DATE", True), \
                    patch.object(core.cfg, "VIDEO_MISSING_DATE_POLICY", "mtime"), \
                    patch.object(core, "read_media_creation_time") as probe:
                items, missing = core.build_items([path], {core.normalize_path(path): row})

            self.assertFalse(missing)
            self.assertEqual(items[0]["date_tag"], "ExifIFD:DateTimeOriginal")
            probe.assert_not_called()

    def test_media_date_reader_uses_one_ffmpeg_invocation(self):
        import subprocess
        import tdlib_video_album_uploader as core

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(b"video")

            def run(command, **_kwargs):
                Path(command[-1]).write_text(
                    ";FFMETADATA1\ncreation_time=2024-06-29T05:48:00Z\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            core._media_creation_metadata.cache_clear()
            with patch.object(core.imageio_ffmpeg, "get_ffmpeg_exe", return_value="ffmpeg"), \
                    patch.object(core.subprocess, "run", side_effect=run) as ffmpeg:
                media = core.read_media_creation_time(path)

            self.assertIsNotNone(media)
            self.assertEqual(media[1], "Media:stream:creation_time")
            self.assertEqual(ffmpeg.call_count, 1)

    def test_media_date_failure_is_not_negative_cached(self):
        import datetime
        import tdlib_video_album_uploader as core

        core._media_creation_metadata.cache_clear()
        successful = (
            datetime.datetime(2024, 6, 29, 5, 48, tzinfo=datetime.timezone.utc),
            "Media:creation_time",
            True,
        )
        with patch.object(
            core,
            "_read_media_creation_metadata",
            side_effect=[None, successful],
        ) as reader:
            self.assertIsNone(core._media_creation_metadata("share/clip.mp4", 1, 2))
            self.assertEqual(
                core._media_creation_metadata("share/clip.mp4", 1, 2),
                successful,
            )
        self.assertEqual(reader.call_count, 2)

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

    def test_invalid_api_id_is_not_silently_replaced(self):
        dialog = gui.ConfigDialog()
        dialog.fields["api_id"].setText("invalid")
        with patch.object(gui.QMessageBox, "warning") as warning, patch.object(gui, "_write_config_values") as save:
            dialog._save()
            warning.assert_called_once()
            save.assert_not_called()

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
        import tdlib_video_album_uploader as core
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
        import tdlib_video_album_uploader as core

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

    def test_unreadable_video_does_not_abort_album_content_build(self):
        import tdlib_video_album_uploader as core

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

    def test_all_cache_clear_removes_persistent_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            log_dir.mkdir()
            (log_dir / "app.log").write_text("app", encoding="utf-8")
            (log_dir / "tdlib.log").write_text("tdlib", encoding="utf-8")
            with patch.object(gui, "CACHE_TARGETS", {"logs": ("运行日志", log_dir)}):
                removed, errors = gui._clear_cache(("logs",))
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


if __name__ == "__main__":
    unittest.main()
