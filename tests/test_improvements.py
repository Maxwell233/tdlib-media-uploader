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

import album_metadata as metadata
import app_logging
import gui_app as gui
import path_utils
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
            with patch.object(gui, "_write_config_values", return_value="") as save, \
                    patch.object(gui, "_validate_exiftool_path", return_value=""):
                config._save()
                config_values = save.call_args.args[0]
            self.assertIn(("telegram", "api_id"), config_values)
            self.assertIn(("paths", "video_dir"), config_values)
            self.assertIn(("proxy", "enabled"), config_values)
            self.assertNotIn(("paths", "exiftool_path"), config_values)
            self.assertFalse(any(section in {"scan", "process"} for section, _ in config_values))

            with patch.object(gui, "_write_config_values", return_value="") as save:
                with patch.object(gui, "_validate_exiftool_path", return_value=""):
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

    def test_exiftool_path_validation_runs_short_version_probe(self):
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "exiftool"
            executable.write_bytes(b"tool")
            completed = subprocess.CompletedProcess([], 0, "12.95\n", "")
            with patch.object(gui, "run_cancellable_process", return_value=completed) as run:
                self.assertEqual(gui._validate_exiftool_path(str(executable)), "")
            self.assertEqual(run.call_args.args[0], [str(executable), "-ver"])
            self.assertEqual(run.call_args.kwargs["timeout"], 5.0)

    def test_exiftool_k_variant_is_rejected_on_windows(self):
        with patch.object(gui.os, "name", "nt"):
            message = gui._validate_exiftool_path("exiftool(-k).exe")
        self.assertIn("exiftool.exe", message)

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

    def test_natural_filename_sort_uses_ascending_numeric_runs(self):
        names = [
            "x.1",
            "x.10",
            "x.100",
            "x.101",
            "x.102",
            "x.103",
            "x.409",
            "x.41",
            "x.410",
        ]
        self.assertEqual(
            path_utils.natural_sort(names),
            ["x.1", "x.10", "x.41", "x.100", "x.101", "x.102", "x.103", "x.409", "x.410"],
        )

    def test_natural_sort_is_deterministic_for_equal_case_and_leading_zero_runs(self):
        values = ["x001", "a1", "x1", "A1", "x01"]
        expected = ["A1", "a1", "x1", "x01", "x001"]
        for _ in range(8):
            shuffled = list(values)
            random.shuffle(shuffled)
            self.assertEqual(path_utils.natural_sort(shuffled), expected)

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

    def test_inflight_journal_blocks_unknown_until_manual_reconciliation(self):
        from upload_journal import InflightJournal, UNKNOWN

        with tempfile.TemporaryDirectory() as directory:
            journal = InflightJournal(Path(directory))
            journal.prepare("mixed", "album-key", [{"path": "clip.jpg"}])
            journal.submitted("mixed", "album-key", [11])
            journal.unknown("mixed", "album-key", "confirmation timeout")
            self.assertEqual(journal.unresolved("mixed", "album-key")["status"], UNKNOWN)
            self.assertEqual(len(journal.list_unresolved()), 1)
            journal.mark_not_sent("mixed", "album-key")
            self.assertIsNone(journal.unresolved("mixed", "album-key"))
            journal.prepare("mixed", "album-key", [{"path": "clip.jpg"}])
            journal.confirmed("mixed", "album-key", [12])
            self.assertIsNone(journal.get("mixed", "album-key"))

    def test_confirmed_inflight_record_blocks_until_finalization(self):
        from upload_journal import CONFIRMED, InflightJournal

        with tempfile.TemporaryDirectory() as directory:
            journal = InflightJournal(Path(directory))
            journal.prepare("image", "album-key", [{"path": "clip.jpg"}])
            journal.update("image", "album-key", CONFIRMED, message_ids=[7])
            self.assertEqual(
                journal.unresolved("image", "album-key")["status"],
                CONFIRMED,
            )
            journal.confirmed("image", "album-key", [7])
            self.assertIsNone(journal.unresolved("image", "album-key"))

    def test_immediate_tdlib_send_failure_is_recorded_as_failed(self):
        import tdlib_common
        from upload_journal import FAILED, InflightJournal

        class UI:
            def warning(self, _text):
                pass

        with tempfile.TemporaryDirectory() as directory:
            journal = InflightJournal(Path(directory))
            client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
            client.ui = UI()
            client.inflight_journal = journal
            client.cancel_event = threading.Event()
            client.request = lambda _query: {
                "id": 17,
                "sending_state": {"@type": "messageSendingStateFailed"},
            }
            with patch.object(client, "_safe_diagnose_upload_failure"):
                with self.assertRaises(RuntimeError):
                    client.send_contents(
                        [{
                            "@type": "inputMessagePhoto",
                            "photo": {"@type": "inputFileId", "id": 1},
                        }],
                        album_key="album-key",
                        kind="image",
                    )
            record = journal.get("image", "album-key")
            self.assertIsNotNone(record)
            self.assertEqual(record["status"], FAILED)

    def test_inflight_kind_can_be_inferred_for_manual_reconciliation(self):
        import tdlib_common

        with tempfile.TemporaryDirectory() as directory:
            journal = __import__("upload_journal").InflightJournal(Path(directory))
            journal.prepare("mixed", "album-key", [{"path": "clip.jpg"}])
            journal.unknown("mixed", "album-key", "connection lost")
            client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
            client.inflight_journal = journal
            client.reconcile_inflight("album-key", sent=False)
            self.assertIsNone(journal.get("mixed", "album-key"))

    def test_manual_sent_reconciliation_writes_checkpoint_before_removing_journal(self):
        import tdlib_common
        import tdlib_image_album_uploader as image_core
        from upload_journal import InflightJournal

        target = {
            "target_mode": "forum_topic",
            "chat_id": -1001,
            "forum_topic_id": 7,
            "channel_chat_id": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "images"
            root.mkdir()
            path = root / "photo.jpg"
            path.write_bytes(b"image")
            snapshot = path.stat()
            journal = InflightJournal(Path(directory) / "journal")
            journal.prepare(
                "image",
                "album-key",
                [{"path": path, "scan_size": snapshot.st_size, "scan_mtime_ns": snapshot.st_mtime_ns}],
                target=target,
            )
            journal.submitted("image", "album-key", [11], target=target)
            journal.unknown("image", "album-key", "connection lost", target=target)
            client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
            client.inflight_journal = journal
            with patch.object(image_core.cfg, "IMAGE_DIR", root), \
                    patch.object(image_core, "STATE_DIR", Path(directory) / "state"):
                path.write_bytes(b"changed source")
                client.reconcile_inflight("album-key", sent=True, kind="image", target=target)
                state = image_core.UploadState(target=target)
                self.assertIn(
                    image_core.file_signature(path, (snapshot.st_size, snapshot.st_mtime_ns)),
                    state.data["completed"],
                )
            self.assertIsNone(journal.unresolved("image", "album-key", target=target))

    def test_manual_sent_state_failure_keeps_journal(self):
        import tdlib_common
        import tdlib_image_album_uploader as image_core
        from upload_journal import InflightJournal

        target = {"target_mode": "forum_topic", "chat_id": -1001, "forum_topic_id": 7}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "images"
            root.mkdir()
            path = root / "photo.jpg"
            path.write_bytes(b"image")
            snapshot = path.stat()
            journal = InflightJournal(Path(directory) / "journal")
            journal.prepare("image", "album-key", [{"path": path, "size": snapshot.st_size, "mtime_ns": snapshot.st_mtime_ns}], target=target)
            journal.unknown("image", "album-key", "timeout", target=target)
            state_dir = Path(directory) / "state"
            with patch.object(image_core.cfg, "IMAGE_DIR", root), patch.object(image_core, "STATE_DIR", state_dir):
                # Create the state file before making its durable save fail.
                image_core.UploadState(target=target)
                client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
                client.inflight_journal = journal
                with patch.object(image_core.UploadState, "_save", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        client.reconcile_inflight("album-key", sent=True, kind="image", target=target)
            self.assertIsNotNone(journal.unresolved("image", "album-key", target=target))

    def test_inflight_journal_is_scoped_to_target_and_legacy_records_are_conservative(self):
        from upload_journal import InflightJournal

        first = {"target_mode": "forum_topic", "chat_id": -1001, "forum_topic_id": 1}
        second = {"target_mode": "forum_topic", "chat_id": -1001, "forum_topic_id": 2}
        with tempfile.TemporaryDirectory() as directory:
            journal = InflightJournal(Path(directory))
            journal.prepare("image", "album", [{"path": "photo.jpg"}], target=first)
            journal.unknown("image", "album", "timeout", target=first)
            self.assertIsNotNone(journal.unresolved("image", "album", target=first))
            self.assertIsNone(journal.unresolved("image", "album", target=second))
            journal.prepare("image", "legacy", [{"path": "photo.jpg"}])
            journal.unknown("image", "legacy", "timeout")
            self.assertIsNotNone(journal.unresolved("image", "legacy", target=first))
            self.assertIsNotNone(journal.unresolved("image", "legacy", target=second))

    def test_forum_target_identity_ignores_irrelevant_channel_id(self):
        from upload_journal import normalize_target

        first = normalize_target(
            {
                "target_mode": "forum_topic",
                "chat_id": 100,
                "forum_topic_id": 10,
                "channel_chat_id": 999,
            }
        )
        second = normalize_target(
            {
                "target_mode": "forum_topic",
                "chat_id": 100,
                "forum_topic_id": 10,
                "channel_chat_id": 888,
            }
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first,
            {
                "target_mode": "forum_topic",
                "chat_id": 100,
                "forum_topic_id": 10,
                "channel_chat_id": 0,
            },
        )

    def test_channel_target_identity_ignores_irrelevant_forum_topic_id(self):
        from upload_journal import normalize_target

        first = normalize_target(
            {
                "target_mode": "channel",
                "channel_chat_id": 200,
                "forum_topic_id": 10,
            }
        )
        second = normalize_target(
            {
                "target_mode": "channel",
                "channel_chat_id": 200,
                "forum_topic_id": 999,
            }
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first,
            {
                "target_mode": "channel",
                "chat_id": 200,
                "forum_topic_id": 0,
                "channel_chat_id": 200,
            },
        )

    def test_target_normalization_parses_only_mode_relevant_fields(self):
        from upload_journal import normalize_target

        self.assertEqual(
            normalize_target(
                {
                    "target_mode": "channel",
                    "channel_chat_id": 200,
                    "chat_id": "invalid",
                }
            ),
            {
                "target_mode": "channel",
                "chat_id": 200,
                "forum_topic_id": 0,
                "channel_chat_id": 200,
            },
        )
        self.assertEqual(
            normalize_target(
                {
                    "target_mode": "channel",
                    "channel_chat_id": 0,
                    "chat_id": 200,
                }
            )["chat_id"],
            200,
        )
        self.assertEqual(
            normalize_target(
                {
                    "target_mode": "forum_topic",
                    "chat_id": 100,
                    "forum_topic_id": 10,
                    "channel_chat_id": "invalid",
                }
            )["forum_topic_id"],
            10,
        )
        self.assertEqual(
            normalize_target(
                {
                    "target_mode": "channel",
                    "channel_chat_id": 200,
                    "forum_topic_id": "invalid",
                }
            )["channel_chat_id"],
            200,
        )

    def test_legacy_reconciliation_uses_kind_target_instead_of_global_target(self):
        import tdlib_common
        import tdlib_image_album_uploader as image_core
        import tdlib_mixed_album_uploader as mixed_core
        import tdlib_video_album_uploader as video_core
        from upload_journal import InflightJournal

        modules = {
            "video": (video_core, "VIDEO_DIR", "VIDEO_RESET_STATE", "video_file.mp4"),
            "image": (image_core, "IMAGE_DIR", "IMAGE_RESET_STATE", "image_file.jpg"),
            "mixed": (mixed_core, "MIXED_DIR", "MIXED_RESET_STATE", "mixed_file.jpg"),
        }
        target_a = {
            "target_mode": "forum_topic",
            "chat_id": 100,
            "forum_topic_id": 10,
            "channel_chat_id": 0,
        }
        target_b = {
            "target_mode": "forum_topic",
            "chat_id": 200,
            "forum_topic_id": 20,
            "channel_chat_id": 0,
        }

        for kind, (module, config_dir, reset_name, filename) in modules.items():
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "media"
                root.mkdir()
                path = root / filename
                path.write_bytes(b"media")
                snapshot = path.stat()
                journal = InflightJournal(Path(directory) / "journal")
                journal.prepare(
                    kind,
                    "legacy-kind-album",
                    [{
                        "path": path,
                        "scan_size": snapshot.st_size,
                        "scan_mtime_ns": snapshot.st_mtime_ns,
                    }],
                )
                journal.unknown(kind, "legacy-kind-album", "timeout")
                client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
                client.inflight_journal = journal
                target_for = lambda requested_kind: target_a if requested_kind == kind else target_b
                patches = [
                    patch.object(tdlib_common.cfg, "target_for", side_effect=target_for),
                    patch.object(tdlib_common.cfg, "TARGET_MODE", target_b["target_mode"]),
                    patch.object(tdlib_common.cfg, "CHAT_ID", target_b["chat_id"]),
                    patch.object(tdlib_common.cfg, "FORUM_TOPIC_ID", target_b["forum_topic_id"]),
                    patch.object(tdlib_common.cfg, "CHANNEL_CHAT_ID", 0),
                    patch.object(tdlib_common.cfg, config_dir, root),
                    patch.object(tdlib_common.cfg, reset_name, False),
                    patch.object(module, "STATE_DIR", Path(directory) / "state"),
                ]
                with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
                    client.reconcile_inflight("legacy-kind-album", sent=True, kind=kind)
                    state_a = module.UploadState(target=target_a)
                    state_b = module.UploadState(target=target_b)
                    signature = module.file_signature(path, (snapshot.st_size, snapshot.st_mtime_ns))
                    self.assertIn(signature, state_a.data["completed"])
                    self.assertNotIn(signature, state_b.data["completed"])
                self.assertIsNone(journal.unresolved(kind, "legacy-kind-album"))

    def test_journal_target_takes_precedence_over_state_fallback_target(self):
        import tdlib_common
        import tdlib_image_album_uploader as image_core
        from upload_journal import normalize_target

        target_a = normalize_target(
            {"target_mode": "forum_topic", "chat_id": 100, "forum_topic_id": 10}
        )
        target_b = normalize_target(
            {"target_mode": "forum_topic", "chat_id": 200, "forum_topic_id": 20}
        )
        target_c = normalize_target(
            {"target_mode": "forum_topic", "chat_id": 300, "forum_topic_id": 30}
        )
        client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(image_core, "STATE_DIR", Path(directory) / "state"), \
                    patch.object(image_core.cfg, "IMAGE_DIR", Path(directory) / "images"), \
                    patch.object(image_core.cfg, "IMAGE_RESET_STATE", False), \
                    patch.object(tdlib_common.cfg, "target_for", return_value=target_c), \
                    patch.object(tdlib_common.cfg, "TARGET_MODE", "forum_topic"), \
                    patch.object(tdlib_common.cfg, "CHAT_ID", target_c["chat_id"]), \
                    patch.object(tdlib_common.cfg, "FORUM_TOPIC_ID", target_c["forum_topic_id"]), \
                    patch.object(tdlib_common.cfg, "CHANNEL_CHAT_ID", 0):
                state = client._state_for_journal(
                    "image",
                    {"target": target_a},
                    fallback_target=target_b,
                )
                self.assertEqual(state._chat_id, target_a["chat_id"])
                self.assertEqual(state._forum_topic_id, target_a["forum_topic_id"])

    def test_target_identity_changes_for_topic_channel_or_mode(self):
        from upload_journal import normalize_target

        forum_topic_10 = normalize_target(
            {"target_mode": "forum_topic", "chat_id": 100, "forum_topic_id": 10}
        )
        forum_topic_11 = normalize_target(
            {"target_mode": "forum_topic", "chat_id": 100, "forum_topic_id": 11}
        )
        channel_200 = normalize_target(
            {"target_mode": "channel", "channel_chat_id": 200}
        )
        channel_201 = normalize_target(
            {"target_mode": "channel", "channel_chat_id": 201}
        )
        self.assertNotEqual(forum_topic_10, forum_topic_11)
        self.assertNotEqual(channel_200, channel_201)
        self.assertNotEqual(forum_topic_10, channel_200)

    def test_forum_unknown_journal_matches_after_irrelevant_channel_change(self):
        from upload_journal import InflightJournal

        first = {
            "target_mode": "forum_topic",
            "chat_id": 100,
            "forum_topic_id": 10,
            "channel_chat_id": 999,
        }
        second = {**first, "channel_chat_id": 888}
        with tempfile.TemporaryDirectory() as directory:
            journal = InflightJournal(Path(directory))
            journal.prepare("image", "forum-album", [{"path": "photo.jpg"}], target=first)
            journal.unknown("image", "forum-album", "timeout", target=first)
            self.assertIsNotNone(journal.unresolved("image", "forum-album", target=second))

    def test_channel_unknown_journal_matches_after_irrelevant_topic_change(self):
        from upload_journal import InflightJournal

        first = {
            "target_mode": "channel",
            "channel_chat_id": 200,
            "forum_topic_id": 10,
        }
        second = {**first, "forum_topic_id": 999}
        with tempfile.TemporaryDirectory() as directory:
            journal = InflightJournal(Path(directory))
            journal.prepare("image", "channel-album", [{"path": "photo.jpg"}], target=first)
            journal.unknown("image", "channel-album", "timeout", target=first)
            self.assertIsNotNone(journal.unresolved("image", "channel-album", target=second))

    def test_version_two_target_record_is_read_with_canonical_identity(self):
        from hashlib import sha256

        from upload_journal import InflightJournal, UNKNOWN

        target = {
            "target_mode": "forum_topic",
            "chat_id": 100,
            "forum_topic_id": 10,
            "channel_chat_id": 999,
        }
        equivalent = {**target, "channel_chat_id": 888}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = InflightJournal(root)
            # Simulate the previous v2 filename hash, which included every
            # target field before mode-aware canonicalization was introduced.
            identity = json.dumps(target, sort_keys=True, separators=(",", ":"))
            old_hash = sha256(f"image\nv2-album\n{identity}".encode("utf-8")).hexdigest()
            (root / f"{old_hash}.json").write_text(
                json.dumps(
                    {
                        "version": 2,
                        "kind": "image",
                        "album_key": "v2-album",
                        "status": UNKNOWN,
                        "target": target,
                        "items": [{"path": "photo.jpg"}],
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNotNone(journal.unresolved("image", "v2-album", target=equivalent))

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

    def test_stability_interval_honours_configured_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stable.bin"
            path.write_bytes(b"stable")
            with patch.object(path_utils, "cancelable_sleep", return_value=True) as sleep:
                result = path_utils.check_file_readiness(path, stable_interval=1.2, stable_checks=2, probe=False)
            self.assertTrue(result.ready)
            self.assertEqual(sleep.call_args.args[0], 1.2)

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

    def test_filesystem_error_classification_distinguishes_permanent_failures(self):
        import errno

        self.assertFalse(path_utils.is_transient_fs_error(OSError(errno.EACCES, "denied")))
        self.assertFalse(path_utils.is_transient_fs_error(OSError(errno.EINVAL, "bad argument")))
        self.assertTrue(path_utils.is_transient_fs_error(OSError(errno.EAGAIN, "try again")))
        self.assertTrue(path_utils.is_transient_fs_error(OSError("provider reset")))

    def test_gui_inflight_page_loads_unresolved_records(self):
        from upload_journal import InflightJournal

        with tempfile.TemporaryDirectory() as directory:
            journal = InflightJournal(Path(directory) / "upload_inflight")
            journal.prepare("image", "gui-album", [{"path": "photo.jpg"}])
            journal.unknown("image", "gui-album", "timeout")
            with patch.object(gui, "APP_DATA_DIR", Path(directory)):
                # The page resolves its journal root through runtime_paths at
                # import time; replace the class-level path explicitly for a
                # deterministic, offline smoke test.
                with patch("upload_journal.APP_DATA_DIR", Path(directory)):
                    page = gui.InflightPage()
                    self.assertEqual(page.table.rowCount(), 1)
                    page.deleteLater()

    def test_gui_marks_legacy_target_unknown_and_warns_before_sent_confirmation(self):
        from upload_journal import InflightJournal

        with tempfile.TemporaryDirectory() as directory:
            journal = InflightJournal(Path(directory) / "upload_inflight")
            journal.prepare("image", "legacy-gui-album", [{"path": "photo.jpg"}])
            journal.unknown("image", "legacy-gui-album", "timeout")
            with patch.object(gui, "APP_DATA_DIR", Path(directory)), \
                    patch("upload_journal.APP_DATA_DIR", Path(directory)):
                page = gui.InflightPage()
                self.assertEqual(page.table.item(0, 5).text(), "⚠ 旧版记录：目标未知")
                page.table.selectRow(0)
                with patch.object(
                    gui.QMessageBox,
                    "warning",
                    return_value=gui.QMessageBox.StandardButton.No,
                ) as warning:
                    page._emit_choice(True)
                warning_text = warning.call_args.args[2]
                self.assertIn("旧版未记录 Telegram 目标", warning_text)
                self.assertIn("当前配置的 Telegram 目标", warning_text)
                self.assertIn("如果目标不一致，请不要确认已发送", warning_text)
                page.deleteLater()

    def test_natural_sort_handles_unicode_numeric_runs_and_folder_names(self):
        self.assertEqual(
            path_utils.natural_sort(["a（10）", "a（1）", "a（11）", "a（5）"]),
            ["a（1）", "a（5）", "a（10）", "a（11）"],
        )
        root = Path("root")
        paths = [
            root / "Folder20" / "clip1.jpg",
            root / "Folder2" / "clip1.jpg",
            root / "Folder10" / "clip1.jpg",
            root / "Folder1" / "clip1.jpg",
        ]
        ordered = path_utils.media_path_sort(paths, root)
        self.assertEqual(
            [path_utils.relative_name(path, root) for path in ordered],
            [
                "Folder1/clip1.jpg",
                "Folder2/clip1.jpg",
                "Folder10/clip1.jpg",
                "Folder20/clip1.jpg",
            ],
        )

    def test_natural_path_sort_compares_each_directory_component_first(self):
        root = Path("root")
        paths = [
            root / "Day2" / "x.100.jpg",
            root / "Day10" / "x.1.jpg",
            root / "Day2" / "x.10.jpg",
        ]
        ordered = path_utils.media_path_sort(paths, root)
        self.assertEqual(
            [path_utils.relative_name(path, root) for path in ordered],
            ["Day2/x.10.jpg", "Day2/x.100.jpg", "Day10/x.1.jpg"],
        )

    def test_relative_path_sort_compares_directories_before_filenames(self):
        root = Path("root")
        paths = [
            root / "B2" / "x.41.jpg",
            root / "A" / "x.1.jpg",
            root / "B2" / "x.410.jpg",
            root / "A" / "x.100.jpg",
            root / "B10" / "x.2.jpg",
        ]
        ordered = path_utils.media_path_sort(paths, root)
        self.assertEqual(
            [path_utils.relative_name(path, root) for path in ordered],
            ["A/x.1.jpg", "A/x.100.jpg", "B2/x.41.jpg", "B2/x.410.jpg", "B10/x.2.jpg"],
        )

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

    def test_image_filename_scan_uses_natural_numeric_order(self):
        import tdlib_image_album_uploader as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ["x.1.jpg", "x.10.jpg", "x.100.jpg", "x.41.jpg", "x.410.jpg"]
            for name in names:
                (root / name).write_bytes(b"image")
            with patch.object(core.cfg, "IMAGE_DIR", root), \
                    patch.object(core.cfg, "IMAGE_EXTENSIONS", {".jpg"}), \
                    patch.object(core.cfg, "IMAGE_MAX_BYTES", 100), \
                    patch.object(core.cfg, "IMAGE_SORT_MODE", "path"):
                paths = core.scan_images()
            self.assertEqual(
                [path.name for path in paths],
                ["x.1.jpg", "x.10.jpg", "x.41.jpg", "x.100.jpg", "x.410.jpg"],
            )

    def test_video_filename_scan_uses_natural_numeric_order(self):
        import tdlib_video_album_uploader as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ["x.1.mp4", "x.10.mp4", "x.41.mp4", "x.409.mp4", "x.410.mp4"]
            for name in names:
                (root / name).write_bytes(b"video")
            with patch.object(core.cfg, "VIDEO_DIR", root), \
                    patch.object(core.cfg, "VIDEO_EXTENSIONS", {".mp4"}), \
                    patch.object(core.cfg, "VIDEO_MAX_BYTES", 100), \
                    patch.object(core.cfg, "VIDEO_READ_DATES", False), \
                    patch.object(core.cfg, "VIDEO_SORT_MODE", "name"):
                paths = core.scan_videos()
            self.assertEqual(
                [path.name for path in paths],
                ["x.1.mp4", "x.10.mp4", "x.41.mp4", "x.409.mp4", "x.410.mp4"],
            )

    def test_all_media_scanners_keep_directory_order_before_basename_order(self):
        import tdlib_image_album_uploader as image_core
        import tdlib_mixed_album_uploader as mixed_core
        import tdlib_video_album_uploader as video_core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = ["A/x.1", "A/x.100", "B/x.41", "B/x.410"]
            image_root = root / "images"
            video_root = root / "videos"
            mixed_root = root / "mixed"
            for base, extension in ((image_root, ".jpg"), (video_root, ".mp4")):
                for relative in expected:
                    path = base / f"{relative}{extension}"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"media")
            for relative in expected:
                path = mixed_root / f"{relative}.jpg" if relative.startswith("A/") else mixed_root / f"{relative}.mp4"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"media")

            with patch.object(image_core.cfg, "IMAGE_DIR", image_root), \
                    patch.object(image_core.cfg, "IMAGE_EXTENSIONS", {".jpg"}), \
                    patch.object(image_core.cfg, "IMAGE_MAX_BYTES", 100), \
                    patch.object(image_core.cfg, "IMAGE_SORT_MODE", "path"):
                image_names = [path.relative_to(image_root).with_suffix("").as_posix() for path in image_core.scan_images()]
            with patch.object(video_core.cfg, "VIDEO_DIR", video_root), \
                    patch.object(video_core.cfg, "VIDEO_EXTENSIONS", {".mp4"}), \
                    patch.object(video_core.cfg, "VIDEO_MAX_BYTES", 100), \
                    patch.object(video_core.cfg, "VIDEO_READ_DATES", False):
                video_names = [path.relative_to(video_root).with_suffix("").as_posix() for path in video_core.scan_videos()]
            with patch.object(mixed_core.cfg, "MIXED_DIR", mixed_root), \
                    patch.object(mixed_core.cfg, "MIXED_IMAGE_EXTENSIONS", {".jpg"}), \
                    patch.object(mixed_core.cfg, "MIXED_VIDEO_EXTENSIONS", {".mp4"}), \
                    patch.object(mixed_core.cfg, "MIXED_EXTENSIONS", {".jpg", ".mp4"}), \
                    patch.object(mixed_core.cfg, "MIXED_ALBUM_SIZE", 10), \
                    patch.object(mixed_core.cfg, "MIXED_SORT_MODE", "name"):
                mixed_groups = mixed_core.scan_mixed_groups()
                mixed_names = [
                    f"{group['group_name']}/{path_utils.relative_name(item['path'], group['group_path']).rsplit('.', 1)[0]}"
                    for group in mixed_groups
                    for item in group["items"]
                ]
            self.assertEqual(image_names, expected)
            self.assertEqual(video_names, expected)
            self.assertEqual(mixed_names, expected)

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

    def test_unknown_media_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            metadata.path_for("audio")
        with self.assertRaises(ValueError):
            gui._target_for("audio")

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

    def test_exiftool_progress_is_monotonic_for_large_scan(self):
        import subprocess
        import tdlib_video_album_uploader as core

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
        import tdlib_video_album_uploader as core

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
        import tdlib_video_album_uploader as core

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

    def test_staging_cleanup_removes_only_stale_files(self):
        from staging import cleanup_staging, ensure_managed_staging_dir

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

    def test_mixed_extension_conflicts_are_rejected(self):
        import tdlib_mixed_album_uploader as core

        with patch.object(core.cfg, "MIXED_IMAGE_EXTENSIONS", {".jpg"}), \
                patch.object(core.cfg, "MIXED_VIDEO_EXTENSIONS", {".jpg"}):
            with self.assertRaises(RuntimeError):
                core._validate_extensions()

    def test_tdlib_upload_failure_logs_source_diagnosis(self):
        import tdlib_common

        class UI:
            def __init__(self):
                self.messages = []

            def warning(self, text):
                self.messages.append(str(text))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(b"video")
            client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
            client.ui = UI()
            client.request = lambda _query: (_ for _ in ()).throw(
                tdlib_common.TDLibError(400, "FILE_READ")
            )
            with patch.object(tdlib_common, "write_app_log") as write:
                with self.assertRaises(tdlib_common.TDLibError):
                    client.send_contents(
                        [{
                            "@type": "inputMessageVideo",
                            "video": {"@type": "inputFileId", "id": 1},
                            "thumbnail": None,
                            "cover": None,
                        }],
                        items=[{"path": path, "scan_size": 5, "scan_mtime_ns": path.stat().st_mtime_ns}],
                    )
            self.assertTrue(write.called)
            self.assertIn("源文件可读取", write.call_args.args[1])
            self.assertTrue(client.ui.messages)

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

    def test_mtime_fallback_does_not_probe_ffmpeg_when_media_dates_disabled(self):
        import tdlib_video_album_uploader as core

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

    def test_cancelled_video_scan_is_not_reported_as_normal_empty_result(self):
        import tdlib_video_album_uploader as core

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

    def test_no_date_upload_path_reaches_all_63_album_preparations(self):
        import builtins
        import tdlib_video_album_uploader as core
        import tdlib_video_app as entry

        class FakeUI:
            cancel_event = None

            def __init__(self):
                self.album_rows = []

            def __getattr__(self, _name):
                return lambda *args, **kwargs: None

            def confirm_upload(self):
                return True

            def album(self, *args, **kwargs):
                self.album_rows.append(kwargs)

        class FakeState:
            path = Path("state.json")

            @staticmethod
            def is_completed(_item):
                return False

            @staticmethod
            def mark_album_completed(_items, _message_ids):
                return None

        class FakeProgress:
            def __init__(self, *_args):
                pass

            def skip_items(self, *_args):
                return None

            def begin_album(self, *_args):
                return None

            def finish_album(self, *_args):
                return None

            def handle_update(self, *_args):
                return None

        class FakeClient:
            is_premium = True
            caption_length_limit = 4096

            def __init__(self, *_args):
                self.sent = []

            def add_update_callback(self, *_args):
                return None

            def remove_update_callback(self, *_args):
                return None

            def login(self):
                return None

            def refresh_account_limits(self):
                return None

            def set_fast_options(self):
                return None

            def validate_target(self):
                return None

            def send_contents(self, _contents, _progress, items, **_kwargs):
                self.sent.append(list(items))
                return list(range(len(items)))

            def finalize_inflight(self, *_args, **_kwargs):
                return None

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for index in range(623):
                path = root / f"clip-{index}.mp4"
                path.write_bytes(b"v")
                paths.append(path)
            ui = FakeUI()
            client_instances = []

            def make_client(*args):
                client = FakeClient(*args)
                client_instances.append(client)
                return client

            def fake_contents(items, *_args, **_kwargs):
                return ([{"@type": "inputMessageVideo"} for _ in items], list(items), [])

            with patch.object(entry, "UI", ui), \
                    patch.object(core, "UI", ui), \
                    patch.object(core.cfg, "VIDEO_DIR", root), \
                    patch.object(core.cfg, "VIDEO_READ_DATES", False), \
                    patch.object(core.cfg, "VIDEO_ALBUM_SIZE", 10), \
                    patch.object(core.cfg, "VIDEO_SHOW_FILE_LIST", False), \
                    patch.object(core.cfg, "VIDEO_CAPTION_INCLUDE_FILENAMES", False), \
                    patch.object(core.cfg, "VIDEO_MISSING_DATE_POLICY", "mtime"), \
                    patch.object(core, "scan_videos", return_value=paths), \
                    patch.object(core, "cleanup_staging_cache"), \
                    patch.object(core, "validate_config"), \
                    patch.object(core, "verify_tdjson_version", return_value="test"), \
                    patch.object(core, "UploadState", FakeState), \
                    patch.object(core, "preflight_videos", return_value=[]), \
                    patch.object(core, "build_video_contents", side_effect=fake_contents), \
                    patch.object(core, "VideoUploadProgress", FakeProgress), \
                    patch.object(core, "TDJsonClient", side_effect=make_client), \
                    patch.object(core, "cleanup_confirmed_staging"), \
                    patch.object(core, "report_skipped_videos"), \
                    patch.object(builtins, "input", return_value="y"):
                entry._main_impl()

            self.assertEqual(len(client_instances), 1)
            self.assertEqual(len(client_instances[0].sent), 63)
            self.assertEqual([len(album) for album in client_instances[0].sent[:-1]], [10] * 62)
            self.assertEqual(len(client_instances[0].sent[-1]), 3)
            self.assertEqual(sum(len(album) for album in client_instances[0].sent), 623)

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

    def test_local_staging_copies_a_validated_snapshot(self):
        from staging import stage_file

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
        from staging import cleanup_staging, ensure_managed_staging_dir

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

    def test_exiftool_date_query_keeps_full_time_batch(self):
        import subprocess
        import tdlib_video_album_uploader as core

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
