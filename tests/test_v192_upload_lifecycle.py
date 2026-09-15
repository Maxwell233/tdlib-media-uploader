"""V1.9.2 upload lifecycle and diagnostic regressions."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gui_app as gui
import tdlib_common
import tdlib_image_album_uploader as image_core
import upload_journal
from PySide6.QtWidgets import QApplication


class UploadLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_gui_safe_stop_does_not_cancel_active_client(self):
        class Client:
            def __init__(self):
                self.graceful = 0
                self.cancelled = 0

            def request_graceful_stop(self):
                self.graceful += 1

            def cancel(self):
                self.cancelled += 1

        ui = gui.GuiConsoleUI(gui.AuthBridge(), "video")
        client = Client()
        ui.register_client(client)
        ui.request_stop()
        self.assertTrue(ui.stop_requested)
        self.assertFalse(ui.force_stop_requested)
        self.assertEqual(client.graceful, 1)
        self.assertEqual(client.cancelled, 0)

        ui.force_stop()
        self.assertTrue(ui.force_stop_requested)
        self.assertEqual(client.cancelled, 1)

    def test_safe_stop_waking_auth_prompt_does_not_escalate_to_force_stop(self):
        class Bridge:
            def ask(self, _text, _password=False):
                return ""

            def answer(self, _value):
                return None

        ui = gui.GuiConsoleUI(Bridge(), "video")
        ui.request_stop()
        self.assertEqual(ui.prompt("code"), "")
        self.assertTrue(ui.stop_requested)
        self.assertFalse(ui.force_stop_requested)

    def test_tdjson_binding_is_pinned_to_1867(self):
        self.assertEqual(tdlib_common.REQUIRED_TDJSON_VERSION, "1.8.67")
        self.assertEqual(tdlib_common.verify_tdjson_version(), "1.8.67")

    def test_input_message_photo_schema_accepts_local_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            path.write_bytes(b"photo")
            tdlib_common.validate_input_message_contents([{
                "@type": "inputMessagePhoto",
                "photo": {"@type": "inputFileLocal", "path": str(path)},
                "thumbnail": None,
            }], items=[{"path": path}])

    def test_input_message_schema_rejects_missing_or_malformed_files(self):
        invalid_contents = [
            {
                "@type": "inputMessagePhoto",
                "photo": None,
            },
            {
                "@type": "inputMessagePhoto",
                "photo": {},
            },
            {
                "@type": "inputMessagePhoto",
                "photo": {"@type": "inputFileLocal", "path": ""},
            },
            {
                "@type": "inputMessageVideo",
                "video": None,
            },
            {
                "@type": "inputMessageVideo",
                "video": {"@type": "inputFileId", "id": 1},
                "thumbnail": {
                    "@type": "inputThumbnail",
                    "thumbnail": None,
                    "width": 1,
                    "height": 1,
                },
            },
        ]
        for content in invalid_contents:
            with self.subTest(content=content):
                with self.assertRaises(tdlib_common.InvalidTDLibInputPayload):
                    tdlib_common.validate_input_message_contents([content])

    def test_input_message_video_schema_accepts_optional_thumbnail_and_cover(self):
        tdlib_common.validate_input_message_contents([{
            "@type": "inputMessageVideo",
            "video": {"@type": "inputFileId", "id": 7},
            "thumbnail": None,
            "cover": None,
        }])

    def test_input_thumbnail_schema_validates_nested_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "clip.mp4"
            thumb = root / "clip.jpg"
            video.write_bytes(b"video")
            thumb.write_bytes(b"thumbnail")
            tdlib_common.validate_input_message_contents([{
                "@type": "inputMessageVideo",
                "video": {"@type": "inputFileLocal", "path": str(video)},
                "thumbnail": {
                    "@type": "inputThumbnail",
                    "thumbnail": {"@type": "inputFileLocal", "path": str(thumb)},
                    "width": 320,
                    "height": 180,
                },
                "cover": {"@type": "inputFileLocal", "path": str(thumb)},
            }], items=[{"path": video}])

    def test_mixed_video_delegates_to_standalone_payload_builder(self):
        import tdlib_mixed_album_uploader as mixed_core

        item = {"path": Path("clip.mp4"), "media_kind": "video"}
        expected = {"@type": "inputMessageVideo", "video": {"@type": "inputFileId", "id": 1}}
        with patch.object(mixed_core.video_core, "input_video", return_value=expected) as builder, \
                patch.object(mixed_core.cfg, "MIXED_GENERATE_THUMBNAIL", False):
            result = mixed_core._mixed_input_video(item, "caption")
        self.assertIs(result, expected)
        builder.assert_called_once_with(item, "caption", generate_thumbnail=False)

    def test_mixed_payload_serialization_keeps_photo_and_video_input_files(self):
        import tdlib_mixed_album_uploader as mixed_core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo_path = root / "photo.jpg"
            video_path = root / "clip.mp4"
            photo_path.write_bytes(b"photo")
            video_path.write_bytes(b"video")
            items = [
                {"path": photo_path, "media_kind": "image"},
                {"path": video_path, "media_kind": "video"},
            ]
            photo_content = {
                "@type": "inputMessagePhoto",
                "photo": {"@type": "inputFileLocal", "path": str(photo_path)},
                "thumbnail": None,
            }
            video_content = {
                "@type": "inputMessageVideo",
                "video": {"@type": "inputFileLocal", "path": str(video_path)},
                "thumbnail": None,
                "cover": None,
            }
            with patch.object(mixed_core.image_core, "input_photo", return_value=photo_content), \
                    patch.object(mixed_core, "_mixed_input_video", return_value=video_content):
                contents, valid, skipped = mixed_core.build_mixed_contents(items, "Album")
            self.assertEqual(len(valid), 2)
            self.assertEqual(skipped, [])
            serialized = json.loads(json.dumps(contents, ensure_ascii=False))
            tdlib_common.validate_input_message_contents(serialized, items=items)
            self.assertEqual(serialized[0]["photo"]["@type"], "inputFileLocal")
            self.assertEqual(serialized[1]["video"]["@type"], "inputFileLocal")

    def test_malformed_payload_fails_before_journal_prepare(self):
        class Journal:
            def __init__(self):
                self.calls = []

            def unresolved(self, *args, **kwargs):
                return None

            def prepare(self, *args, **kwargs):
                self.calls.append("prepare")

        journal = Journal()
        client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
        client.ui = type("UI", (), {"warning": lambda *_args: None})()
        client.cancel_event = threading.Event()
        client.inflight_journal = journal
        client._last_request_dispatched = False
        client._activity_lock = threading.Lock()
        client._active_album_context = None
        client._active_file_ids = set()
        client.caption_length_limit = None
        client._safe_diagnose_upload_failure = lambda *_args: None
        client.request = lambda _query: self.fail("malformed payload reached TDLib")
        with self.assertRaises(tdlib_common.InvalidTDLibInputPayload):
            client.send_contents(
                [{"@type": "inputMessagePhoto", "photo": None}],
                album_key="malformed",
                kind="image",
                items=[{"path": Path("photo.jpg")}],
            )
        self.assertEqual(journal.calls, [])

    def test_upload_failure_diagnostic_includes_each_payload_shape(self):
        client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
        contents = [
            {
                "@type": "inputMessagePhoto",
                "photo": {"@type": "inputFileId", "id": 1},
                "thumbnail": None,
            },
            {
                "@type": "inputMessageVideo",
                "video": {"@type": "inputFileRemote", "id": "remote-1"},
                "thumbnail": None,
                "cover": None,
            },
        ]
        rows = client._payload_diagnostic_lines(
            contents,
            [{"path": Path("photo.jpg")}, {"path": Path("clip.mp4")}],
        )
        self.assertEqual(len(rows), 2)
        self.assertIn("content=inputMessagePhoto", rows[0])
        self.assertIn("photo=type=inputFileId", rows[0])
        self.assertIn("content=inputMessageVideo", rows[1])
        self.assertIn("video=type=inputFileRemote", rows[1])

    def test_stale_image_upload_mapping_falls_back_to_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "photo.jpg"
            source.write_bytes(b"source")
            missing = Path(directory) / "stale-compressed.jpg"
            key = image_core.stable_path(source)
            image_core.IMAGE_UPLOAD_PATHS[key] = missing
            try:
                self.assertEqual(image_core.upload_path(source), source)
                self.assertNotIn(key, image_core.IMAGE_UPLOAD_PATHS)
            finally:
                image_core.IMAGE_UPLOAD_PATHS.pop(key, None)

    def test_missing_local_input_fails_before_journal_prepare(self):
        class Journal:
            def __init__(self):
                self.calls = []

            def unresolved(self, *args, **kwargs):
                return None

            def prepare(self, *args, **kwargs):
                self.calls.append("prepare")

            def failed(self, *args, **kwargs):
                self.calls.append("failed")

            def unknown(self, *args, **kwargs):
                self.calls.append("unknown")

        journal = Journal()
        client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
        client.ui = type("UI", (), {"warning": lambda *_args: None})()
        client.cancel_event = threading.Event()
        client.inflight_journal = journal
        client._last_request_dispatched = False
        client._activity_lock = threading.Lock()
        client._active_album_context = None
        client._active_file_ids = set()
        client.caption_length_limit = None
        client._safe_diagnose_upload_failure = lambda *_args: None

        contents = [{
            "@type": "inputMessageVideo",
            "video": {
                "@type": "inputFileLocal",
                "path": str(Path(tempfile.gettempdir()) / "tdlib-missing-input.mp4"),
            },
            "caption": {"text": "", "entities": []},
        }]
        with self.assertRaises(FileNotFoundError):
            client.send_contents(
                contents,
                album_key="missing-input",
                kind="video",
                items=[{"path": Path("missing-input.mp4")}],
            )
        self.assertEqual(journal.calls, [])

    def test_force_stop_before_dispatch_does_not_leave_unknown_journal(self):
        class Journal:
            def __init__(self):
                self.calls = []

            def unresolved(self, *args, **kwargs):
                return None

            def prepare(self, *args, **kwargs):
                self.calls.append("prepare")

            def failed(self, *args, **kwargs):
                self.calls.append("failed")

            def unknown(self, *args, **kwargs):
                self.calls.append("unknown")

        client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
        client.ui = type("UI", (), {"warning": lambda *_args: None})()
        client.cancel_event = threading.Event()
        client.cancel_event.set()
        client.inflight_journal = Journal()
        client._last_request_dispatched = False
        client._activity_lock = threading.Lock()
        client._active_album_context = None
        client._active_file_ids = set()
        client.caption_length_limit = None
        client._safe_diagnose_upload_failure = lambda *_args: None

        with self.assertRaises(tdlib_common.TDLibCancelled):
            client.send_contents(
                [{
                    "@type": "inputMessagePhoto",
                    "photo": {"@type": "inputFileId", "id": 1},
                    "caption": {"text": "", "entities": []},
                }],
                album_key="album-before-send",
                kind="image",
                items=[{"path": Path("clip.jpg")}],
            )
        self.assertIn("prepare", client.inflight_journal.calls)
        self.assertIn("failed", client.inflight_journal.calls)
        self.assertNotIn("unknown", client.inflight_journal.calls)

    def test_force_stop_after_dispatch_preserves_unknown(self):
        class Journal:
            def __init__(self):
                self.calls = []

            def unresolved(self, *args, **kwargs):
                return None

            def prepare(self, *args, **kwargs):
                self.calls.append("prepare")

            def unknown(self, *args, **kwargs):
                self.calls.append("unknown")

        client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
        client.ui = type("UI", (), {"warning": lambda *_args: None})()
        client.cancel_event = threading.Event()
        client.inflight_journal = Journal()
        client._last_request_dispatched = False
        client._activity_lock = threading.Lock()
        client._active_album_context = None
        client._active_file_ids = set()
        client.caption_length_limit = None
        client._safe_diagnose_upload_failure = lambda *_args: None

        def dispatched_request(_query):
            client._last_request_dispatched = True
            raise tdlib_common.TDLibCancelled("forced")

        client.request = dispatched_request
        with self.assertRaises(tdlib_common.TDLibCancelled):
            client.send_contents(
                [{
                    "@type": "inputMessagePhoto",
                    "photo": {"@type": "inputFileId", "id": 1},
                    "caption": {"text": "", "entities": []},
                }],
                album_key="album-after-send",
                kind="image",
                items=[{"path": Path("clip.jpg")}],
            )
        self.assertIn("unknown", client.inflight_journal.calls)

    def test_stall_watchdog_reports_target_and_file_diagnostics(self):
        class UI:
            def __init__(self):
                self.messages = []

            def warning(self, text):
                self.messages.append(text)

        client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
        client.ui = UI()
        client._activity_lock = threading.Lock()
        client._active_album_context = {
            "album_key": "album-stalled",
            "kind": "video",
            "target": {"target_mode": "forum_topic", "chat_id": 100, "forum_topic_id": 10},
            "items": [{"path": "C:/Videos/clip.1.mp4", "expected_size": 42}],
            "message_ids": [7],
            "pending_ids": [7],
            "succeeded_ids": [],
            "failed_ids": [],
        }
        client._last_activity_time = time.monotonic() - 5
        client._last_activity_reason = "album-submitted"
        client._file_progress = {11: 12}
        with patch.object(tdlib_common.cfg, "TDLIB_UPLOAD_STALL_TIMEOUT", 1), \
                patch.object(tdlib_common, "write_app_log") as write_log:
            with self.assertRaises(tdlib_common.UploadStalledError) as raised:
                client._check_upload_stall()
        text = str(raised.exception)
        self.assertIn("album-stalled", text)
        self.assertIn("chat_id", text)
        self.assertIn("clip.1.mp4", text)
        self.assertIn("uploaded_size=12", text)
        write_log.assert_called_once()
        self.assertTrue(client.ui.messages)

    def test_file_activity_refreshes_watchdog_only_for_active_files(self):
        client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
        client._activity_lock = threading.Lock()
        client._active_album_context = {"message_ids": [7]}
        client._active_file_ids = {11}
        client._file_progress = {}
        client._file_states = {}
        client._last_activity_time = 1.0
        client._last_activity_reason = "old"
        with patch.object(tdlib_common.time, "monotonic", return_value=20.0):
            client._record_file_activity({"id": 99, "remote": {"uploaded_size": 10}})
            self.assertEqual(client._last_activity_time, 1.0)
            client._record_file_activity({"id": 11, "remote": {"uploaded_size": 10}})
        self.assertEqual(client._last_activity_time, 20.0)

    def test_session_rotation_threshold_counts_confirmed_albums(self):
        client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
        with patch.object(tdlib_common.cfg, "TDLIB_SESSION_ROTATION_ALBUMS", 20):
            self.assertFalse(client.should_rotate(0))
            self.assertFalse(client.should_rotate(19))
            self.assertTrue(client.should_rotate(20))
            self.assertFalse(client.should_rotate(21))
            self.assertTrue(client.should_rotate(40))

    def test_empty_groups_do_not_create_phantom_album_row(self):
        page = gui.UploadPage("video")
        try:
            result = {
                "groups": [{
                    "label": "全部视频（按顺序分组）",
                    "items": [],
                    "completed": 0,
                    "pending": 0,
                    "albums": 0,
                    "album_plans": [],
                }],
                "completed_paths": [],
                "total_files": 0,
                "total_bytes": 0,
                "completed_files": 0,
                "pending_files": 0,
                "album_count": 0,
                "core_available": True,
            }
            page.set_result(result)
            self.assertEqual(page.tree.topLevelItemCount(), 0)
        finally:
            page.deleteLater()
            self.app.processEvents()

    def test_unexpected_zero_scan_retries_with_fresh_enumeration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            zero = {"total_files": 0, "cancelled": False, "core_available": True}
            recovered = {"total_files": 623, "cancelled": False, "core_available": True}
            with patch.object(gui, "_cfg", return_value=root), \
                    patch.object(gui, "_scan_result_once", side_effect=lambda *args, **kwargs: calls.append(1) or (zero if len(calls) == 1 else recovered)), \
                    patch.object(gui.time, "sleep"):
                key = ("video", gui.stable_path(root))
                old = gui._LAST_SUCCESSFUL_SCAN_COUNTS.get(key)
                gui._LAST_SUCCESSFUL_SCAN_COUNTS[key] = 623
                try:
                    result = gui._scan_result("video")
                finally:
                    if old is None:
                        gui._LAST_SUCCESSFUL_SCAN_COUNTS.pop(key, None)
                    else:
                        gui._LAST_SUCCESSFUL_SCAN_COUNTS[key] = old
            self.assertEqual(result["total_files"], 623)
            self.assertEqual(len(calls), 2)

    def test_true_empty_scan_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            empty = {"total_files": 0, "cancelled": False, "core_available": True}
            with patch.object(gui, "_cfg", return_value=root), \
                    patch.object(gui, "_scan_result_once", side_effect=lambda *args, **kwargs: calls.append(1) or empty), \
                    patch.object(gui.time, "sleep"):
                result = gui._scan_result("video")
            self.assertEqual(result["total_files"], 0)
            self.assertEqual(len(calls), 1)

    def test_inflight_page_shows_filenames_and_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal_root = root / "upload_inflight"
            journal = upload_journal.InflightJournal(journal_root)
            journal.prepare(
                "video",
                "album-visible",
                [{"path": root / "Cebu Milker.602.mp4"}, {"path": root / "Cebu Milker.603.mp4"}],
            )
            with patch.object(upload_journal, "APP_DATA_DIR", root):
                page = gui.InflightPage()
                try:
                    page.reload_records()
                    self.assertEqual(page.table.rowCount(), 1)
                    files = page.table.item(0, 1)
                    self.assertIn("Cebu Milker.602.mp4", files.text())
                    self.assertIn("Cebu Milker.603.mp4", files.toolTip())
                finally:
                    page.deleteLater()
                    self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
