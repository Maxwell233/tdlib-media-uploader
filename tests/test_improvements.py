"""Offline regressions: no Telegram login or network requests."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import tempfile
import unittest
from pathlib import Path
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
            self.assertEqual(len(calls), 2)

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
