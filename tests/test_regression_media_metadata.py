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


    def test_exiftool_path_validation_runs_short_version_probe(self):
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "exiftool"
            executable.write_bytes(b"tool")
            completed = subprocess.CompletedProcess([], 0, "12.95\n", "")
            with patch.object(tools, "run_cancellable_process", return_value=completed) as run:
                self.assertEqual(tools.validate_exiftool_path(str(executable)), "")
            self.assertEqual(run.call_args.args[0], [str(executable), "-ver"])
            self.assertEqual(run.call_args.kwargs["timeout"], 5.0)


    def test_exiftool_k_variant_is_rejected_on_windows(self):
        with patch.object(gui.os, "name", "nt"):
            message = gui._validate_exiftool_path("exiftool(-k).exe")
        self.assertIn("exiftool.exe", message)


    def test_relative_path_sort_mtime_ties_use_the_same_path_comparator(self):
        root = Path("root")
        paths = [root / "B" / "x.1", root / "A" / "x.410", root / "A" / "x.41"]
        ordered = path_utils.media_path_sort(
            paths,
            root,
            mode="mtime",
            mtime_key=lambda _path: 100,
        )
        self.assertEqual(
            [path_utils.relative_name(path, root) for path in ordered],
            ["A/x.41", "A/x.410", "B/x.1"],
        )


    def test_exiftool_empty_output_is_a_nonfatal_empty_result(self):
        import subprocess
        from tdlib_media_uploader.media import legacy_video as core

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


    def test_exiftool_progress_is_monotonic_for_large_scan(self):
        import subprocess
        from tdlib_media_uploader.media import legacy_video as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "exiftool"
            executable.write_bytes(b"tool")
            paths = [root / f"clip-{index}.mp4" for index in range(623)]
            events = []

            def run(_command, **kwargs):
                batch = [Path(value) for value in kwargs["input"].splitlines()]
                return subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps([{"SourceFile": str(path)} for path in batch]),
                    "",
                )

            with patch.object(core.cfg, "EXIFTOOL_PATH", executable), \
                    patch.object(core.cfg, "VIDEO_READ_MEDIA_CREATION_DATE", True), \
                    patch.object(core.cfg, "VIDEO_DIR", root), \
                    patch.object(core, "EXIFTOOL_BATCH_SIZE", 256), \
                    patch.object(core, "EXIFTOOL_MAX_RETRIES", 0), \
                    patch.object(core.subprocess, "run", side_effect=run):
                result = core.read_exif_metadata(paths, progress_callback=events.append)

            progress = [event for event in events if event.get("phase") == "exif"]
            completed = [event["completed"] for event in progress]
            self.assertEqual(len(result), 623)
            self.assertEqual(completed[0], 0)
            self.assertEqual(completed[-1], 623)
            self.assertEqual(completed, sorted(completed))
            self.assertTrue(all(0 <= value <= 623 for value in completed))


    def test_exiftool_warning_accepts_complete_rows_without_bisection(self):
        import subprocess
        from tdlib_media_uploader.media import legacy_video as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "exiftool"
            executable.write_bytes(b"tool")
            paths = [root / f"clip-{index}.mp4" for index in range(4)]
            output = json.dumps([{"SourceFile": str(path)} for path in paths])
            completed = subprocess.CompletedProcess([], 0, output, "Warning: metadata was incomplete")
            with patch.object(core.cfg, "EXIFTOOL_PATH", executable), \
                    patch.object(core.cfg, "VIDEO_READ_MEDIA_CREATION_DATE", True), \
                    patch.object(core.cfg, "VIDEO_DIR", root), \
                    patch.object(core, "EXIFTOOL_BATCH_SIZE", 256), \
                    patch.object(core, "EXIFTOOL_MAX_RETRIES", 0), \
                    patch.object(core, "LAST_SCAN_WARNINGS", []) as warnings, \
                    patch.object(core.subprocess, "run", return_value=completed) as run:
                result = core.read_exif_metadata(paths)
                self.assertTrue(any("metadata was incomplete" in warning for warning in warnings))

            self.assertEqual(len(result), len(paths))
            self.assertEqual(run.call_count, 1)


    def test_exiftool_retries_only_missing_source_files(self):
        import subprocess
        from tdlib_media_uploader.media import legacy_video as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "exiftool"
            executable.write_bytes(b"tool")
            paths = [root / f"clip-{index}.mp4" for index in range(5)]
            calls = []
            events = []

            def run(_command, **kwargs):
                batch = [Path(value) for value in kwargs["input"].splitlines()]
                calls.append(batch)
                rows = batch if len(calls) == 1 else batch
                if len(calls) == 1:
                    rows = batch[:-1]
                return subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps([{"SourceFile": str(path)} for path in rows]),
                    "Warning: one source was unavailable" if len(calls) == 1 else "",
                )

            with patch.object(core.cfg, "EXIFTOOL_PATH", executable), \
                    patch.object(core.cfg, "VIDEO_READ_MEDIA_CREATION_DATE", True), \
                    patch.object(core.cfg, "VIDEO_DIR", root), \
                    patch.object(core, "EXIFTOOL_BATCH_SIZE", 256), \
                    patch.object(core, "EXIFTOOL_MAX_RETRIES", 0), \
                    patch.object(core.subprocess, "run", side_effect=run):
                result = core.read_exif_metadata(paths, progress_callback=events.append)

            self.assertEqual(len(result), len(paths))
            self.assertEqual([len(batch) for batch in calls], [5, 1])
            self.assertEqual(calls[1], [paths[-1]])
            self.assertEqual(events[-1]["completed"], len(paths))


    def test_file_mtime_uses_os_stat_and_keeps_fallback(self):
        result = SimpleNamespace(st_mtime=123.5)
        with patch.object(path_utils.os, "stat", return_value=result) as stat:
            self.assertEqual(path_utils.file_mtime("clip.mp4"), 123.5)
            stat.assert_called_once_with("clip.mp4")

        with patch.object(path_utils.os, "stat", side_effect=OSError("share offline")):
            self.assertEqual(path_utils.file_mtime("clip.mp4", fallback=7.25), 7.25)


    def test_video_date_priority_and_optional_media_date(self):
        from tdlib_media_uploader.media import legacy_video as core

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


    def test_mtime_fallback_does_not_probe_ffmpeg_when_media_dates_disabled(self):
        from tdlib_media_uploader.media import legacy_video as core

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(b"video")
            with patch.object(core.cfg, "VIDEO_READ_DATES", True), \
                    patch.object(core.cfg, "VIDEO_READ_MEDIA_CREATION_DATE", False), \
                    patch.object(core.cfg, "VIDEO_MISSING_DATE_POLICY", "mtime"), \
                    patch.object(core, "read_media_creation_time") as probe:
                items, missing = core.build_items([path], {})

            self.assertFalse(missing)
            self.assertEqual(items[0]["date_tag"], "FileSystem:ModifyTime")
            self.assertIsNotNone(items[0]["capture_time"])
            probe.assert_not_called()


    def test_disabled_video_dates_uses_filename_only(self):
        from tdlib_media_uploader.media import legacy_video as core

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


    def test_exiftool_date_query_keeps_full_time_batch(self):
        import subprocess
        from tdlib_media_uploader.media import legacy_video as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "exiftool"
            executable.write_bytes(b"tool")
            clip = root / "clip.mp4"
            clip.write_bytes(b"video")
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
                self.assertEqual(run.call_args.kwargs["input"], f"{clip}\n")
                self.assertNotIn(str(root), command)
                self.assertNotIn("-r", command)
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
        from tdlib_media_uploader.media import legacy_video as core

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


    def test_media_date_reader_uses_one_ffmpeg_invocation(self):
        import subprocess
        from tdlib_media_uploader.media import legacy_video as core

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
        from tdlib_media_uploader.media import legacy_video as core

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
