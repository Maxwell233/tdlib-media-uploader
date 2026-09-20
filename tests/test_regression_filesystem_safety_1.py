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


    def test_cancellable_process_drains_pipes_and_passes_utf8_input(self):
        script = (
            "import sys; data=sys.stdin.read(); "
            "sys.stdout.write(data); sys.stdout.flush(); "
            "sys.stderr.write('e'*1200000); sys.stderr.flush()"
        )
        result = path_utils.run_cancellable_process(
            [sys.executable, "-u", "-c", script],
            input="中文输入",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cancel_event=threading.Event(),
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "中文输入")
        self.assertGreaterEqual(len(result.stderr), 1_200_000)


    def test_cancellable_process_drains_large_stdout_and_stderr(self):
        script = (
            "import sys; sys.stdout.write('o'*1200000); sys.stdout.flush(); "
            "sys.stderr.write('e'*1200000); sys.stderr.flush()"
        )
        result = path_utils.run_cancellable_process(
            [sys.executable, "-u", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cancel_event=threading.Event(),
            timeout=10,
        )
        self.assertEqual(len(result.stdout), 1_200_000)
        self.assertEqual(len(result.stderr), 1_200_000)


    def test_cancellable_process_cancel_and_timeout_reap_child(self):
        cancel = threading.Event()
        timer = threading.Timer(0.15, cancel.set)
        timer.start()
        try:
            with self.assertRaises(TimeoutError):
                path_utils.run_cancellable_process(
                    [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cancel_event=cancel,
                    timeout=5,
                )
        finally:
            timer.cancel()
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            path_utils.run_cancellable_process(
                [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cancel_event=threading.Event(),
                timeout=0.15,
            )
        self.assertLess(time.monotonic() - started, 4)


    def test_cancellable_process_kills_child_that_ignores_terminate(self):
        cancel = threading.Event()
        timer = threading.Timer(0.15, cancel.set)
        timer.start()
        script = (
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(30)"
        )
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                path_utils.run_cancellable_process(
                    [sys.executable, "-u", "-c", script],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cancel_event=cancel,
                    timeout=5,
                )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 4)


    def test_discovery_retries_scandir_and_stat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "clip.jpg").write_bytes(b"image")
            original_scandir = path_utils.os.scandir
            calls = {"scandir": 0}

            def flaky_scandir(path):
                calls["scandir"] += 1
                if calls["scandir"] == 1:
                    raise OSError("share temporarily offline")
                return original_scandir(path)

            with patch.object(path_utils.os, "scandir", side_effect=flaky_scandir):
                result = path_utils.iter_files(
                    root,
                    {".jpg"},
                    discovery_attempts=3,
                    discovery_initial_delay=0,
                    discovery_max_delay=0,
                )
            self.assertEqual([path.name for path in result.paths], ["clip.jpg"])
            self.assertEqual(calls["scandir"], 2)

            class Entry:
                name = "retry.jpg"
                path = str(root / name)

                def __init__(self):
                    self.calls = 0

                def is_symlink(self):
                    return False

                def stat(self, follow_symlinks=False):
                    self.calls += 1
                    if self.calls == 1:
                        raise OSError("stat temporarily offline")
                    return (root / self.name).stat()

            entry = Entry()

            class Entries:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def __iter__(self):
                    return iter([entry])

            (root / "retry.jpg").write_bytes(b"image")
            with patch.object(path_utils.os, "scandir", return_value=Entries()):
                result = path_utils.iter_files(
                    root,
                    {".jpg"},
                    discovery_attempts=3,
                    discovery_initial_delay=0,
                    discovery_max_delay=0,
                )
            self.assertEqual([path.name for path in result.paths], ["retry.jpg"])
            self.assertEqual(entry.calls, 2)


    def test_discovery_retry_can_be_cancelled_and_scan_result_separates_warnings(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(TimeoutError):
            path_utils.retry_fs_operation(
                lambda: (_ for _ in ()).throw(OSError("offline")),
                attempts=5,
                initial_delay=1,
                cancel_event=cancel,
            )
        result = path_utils.ScanResult([], ["read failed"], ["link skipped"], True)
        self.assertEqual(result.errors, ["read failed"])
        self.assertEqual(result.warnings, ["link skipped"])
        self.assertTrue(result.cancelled)
        _paths, legacy_errors = result
        self.assertIn("link skipped", legacy_errors)
        self.assertIn("目录扫描已取消", legacy_errors)


    def test_validate_scan_root_retries_transient_stat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = path_utils.os.lstat
            calls = {"count": 0}

            def flaky(value):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise OSError("share temporarily offline")
                return original(value)

            with patch.object(path_utils.os, "lstat", side_effect=flaky):
                self.assertEqual(path_utils.validate_scan_root(root, initial_delay=0, max_delay=0), root)
            self.assertEqual(calls["count"], 2)


    def test_directory_iterator_recovers_after_mid_enumeration_error(self):
        class Entry:
            def __init__(self, name):
                self.name = name
                self.path = name

        class Iterator:
            def __init__(self, values):
                self.values = iter(values)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                value = next(self.values)
                if value == "ERROR":
                    raise OSError("connection reset")
                return Entry(value)

        streams = iter((Iterator(["a", "ERROR"]), Iterator(["a", "b"])))
        with patch.object(path_utils.os, "scandir", side_effect=lambda _path: next(streams)):
            entries = list(path_utils.iter_directory_entries_with_retry("root", initial_delay=0, max_delay=0))
        self.assertEqual([entry.name for entry in entries], ["a", "b"])


    def test_ordered_bounded_map_keeps_submitting_behind_slow_head(self):
        from concurrent.futures import ThreadPoolExecutor

        started = set()
        lock = threading.Lock()

        def worker(value):
            with lock:
                started.add(value)
            if value == 0:
                time.sleep(0.2)
            else:
                time.sleep(0.01)
            return value

        with ThreadPoolExecutor(max_workers=2) as executor:
            values = list(path_utils.ordered_bounded_map(executor, range(6), worker, 2))
        self.assertEqual(values, list(range(6)))
        self.assertTrue({2, 3}.issubset(started))




    def test_image_plan_preserves_complete_groups_and_custom_titles(self):
        from tdlib_media_uploader.media import legacy_image as core
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


    def test_directory_scan_can_be_cancelled_and_skips_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real.jpg").write_bytes(b"image")
            nested = root / "nested"
            nested.mkdir()
            (nested / "inside.jpg").write_bytes(b"image")
            cancel = __import__("threading").Event()
            cancel.set()
            files, errors = path_utils.iter_files(root, {".jpg"}, cancel_event=cancel)
            self.assertEqual(files, [])
            self.assertIn("目录扫描已取消", errors)
            try:
                link = root / "link"
                link.symlink_to(nested, target_is_directory=True)
            except (OSError, NotImplementedError):
                link = None
            if link is not None:
                files, errors = path_utils.iter_files(root, {".jpg"})
                self.assertNotIn(link / "inside.jpg", files)
                self.assertTrue(any("符号链接" in error for error in errors))


    def test_readiness_supports_multiple_stability_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stable.bin"
            path.write_bytes(b"stable")
            result = path_utils.check_file_readiness(
                path,
                stable_checks=3,
                stable_interval=0,
                probe=True,
                probe_bytes=2,
            )
            self.assertTrue(result.ready)
            self.assertEqual(result.snapshot.size, 6)


    def test_macos_network_mounts_are_recognized(self):
        with patch.object(path_utils.sys, "platform", "darwin"):
            self.assertTrue(path_utils.is_network_path("/Volumes/CameraShare/media"))
            self.assertTrue(path_utils.is_network_path("/Network/nas/media"))
        self.assertTrue(path_utils.is_network_path(r"\\server\share\media"))


    def test_staging_cleanup_removes_only_stale_files(self):
        from tdlib_media_uploader.upload.staging import cleanup_staging, ensure_managed_staging_dir

        with tempfile.TemporaryDirectory() as directory:
            root = ensure_managed_staging_dir(Path(directory) / "staging")
            shard = root / "ab"
            shard.mkdir()
            stale = shard / ("a" * 64 + ".bin")
            fresh = shard / ("b" * 64 + ".bin")
            temporary = shard / ("c" * 64 + ".tmp")
            unrelated_tmp = shard / "notes.tmp"
            stale.write_bytes(b"stale")
            fresh.write_bytes(b"fresh")
            temporary.write_bytes(b"partial")
            unrelated_tmp.write_bytes(b"keep me")
            old = __import__("time").time() - 3600
            os.utime(stale, (old, old))
            cleanup_staging(root, max_age_seconds=60)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertFalse(temporary.exists())
            self.assertTrue(unrelated_tmp.exists())


    def test_cancelled_video_scan_is_not_reported_as_normal_empty_result(self):
        from tdlib_media_uploader.media import legacy_video as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "first.mp4", root / "second.mp4"]
            for path in paths:
                path.write_bytes(b"video")
            executable = root / "exiftool"
            executable.write_bytes(b"tool")
            cancel_event = threading.Event()

            def read_metadata(_paths, **_kwargs):
                cancel_event.set()
                return {}

            with patch.object(core.cfg, "VIDEO_DIR", root), \
                    patch.object(core.cfg, "EXIFTOOL_PATH", executable), \
                    patch.object(core.cfg, "VIDEO_READ_DATES", True), \
                    patch.object(core, "scan_videos", return_value=paths), \
                    patch.object(core, "read_exif_metadata", side_effect=read_metadata):
                result = gui._scan_result("video", cancel_event=cancel_event)

            self.assertTrue(result["cancelled"])
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(result["groups"], [])
            self.assertEqual(result["total_files"], 0)


    def test_jit_revalidation_detects_video_changes_after_scan(self):
        from tdlib_media_uploader.media import legacy_video as core

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


    def test_local_staging_copies_a_validated_snapshot(self):
        from tdlib_media_uploader.upload.staging import stage_file

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            staging = root / "staging"
            source.write_bytes(b"stable media")
            snapshot = path_utils.snapshot_file(source)
            target = stage_file(source, snapshot, staging_dir=staging)
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertNotEqual(target, source)
            # A second call reuses the same snapshot-specific local copy.
            self.assertEqual(stage_file(source, snapshot, staging_dir=staging), target)


    def test_staging_cleanup_never_enters_linked_shard_directory(self):
        from tdlib_media_uploader.upload.staging import cleanup_staging, ensure_managed_staging_dir

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            external = root / "external"
            external.mkdir()
            external_file = external / ("a" * 64 + ".mp4")
            external_file.write_bytes(b"must survive")
            ensure_managed_staging_dir(staging)
            linked_shard = staging / "ab"
            try:
                os.symlink(external, linked_shard, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("当前系统不允许创建目录符号链接")
            cleanup_staging(staging, max_age_seconds=0)
            self.assertTrue(external_file.exists())
            self.assertTrue(linked_shard.is_symlink())


    def test_embedded_date_does_not_probe_ffmpeg(self):
        from tdlib_media_uploader.media import legacy_video as core

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




    def test_invalid_api_id_is_not_silently_replaced(self):
        dialog = gui.ConfigDialog()
        dialog.fields["api_id"].setText("invalid")
        with patch.object(gui.QMessageBox, "warning") as warning, patch.object(gui, "_write_config_values") as save:
            dialog._save()
            warning.assert_called_once()
            save.assert_not_called()
