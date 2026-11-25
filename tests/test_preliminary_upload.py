import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tdlib_media_uploader.contracts import UploadContext
from tdlib_media_uploader.core.concurrency import CancellationToken
from tdlib_media_uploader.core.models import AlbumPlan, FileSnapshot, MediaItem, PreliminaryProgressEvent, ScanResult
from tdlib_media_uploader.telegram.preliminary import PreliminaryUploader
from tdlib_media_uploader.telegram.tdlib_common import TDJsonClient, TDLibCancelled
from tdlib_media_uploader.upload.engine import MemoryJournalStore, UploadEngine


class PreliminaryUploadTest(unittest.TestCase):
    def test_long_upload_can_keep_making_progress_past_the_stall_timeout(self):
        result, clock, transport, progress = self._prepare_with_byte_updates([1, 2, 3, 4, 5])
        self.assertIsNotNone(result)
        self.assertGreater(clock.elapsed, 0.4)
        self.assertEqual([event.uploaded_bytes for event in progress], [0, 1, 2, 3, 4, 5])
        self.assertEqual(result.contents[0]["photo"]["photo"], {"@type": "inputFileId", "id": 71})
        transport.cancel_preliminary_upload_file.assert_not_called()
        result.close()

    def test_duplicate_progress_does_not_extend_stall_timeout(self):
        result, clock, transport, progress = self._prepare_with_byte_updates([1, 1, 1, 1, 1])
        self.assertIsNone(result)
        self.assertAlmostEqual(clock.elapsed, 0.6)
        self.assertEqual([event.uploaded_bytes for event in progress], [0, 1])
        transport.cancel_preliminary_upload_file.assert_called_once_with(71)

    def _prepare_with_byte_updates(self, uploaded_sizes):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        source = Path(temporary.name) / "file.jpg"
        source.write_bytes(b"12345")
        content = {"@type": "inputMessagePhoto", "photo": {
            "@type": "inputPhoto", "photo": {"@type": "inputFileLocal", "path": str(source)},
        }}
        transport = Mock(spec=("preliminary_upload_file", "preliminary_file_status", "cancel_preliminary_upload_file"))
        transport.preliminary_upload_file.return_value = {"id": 71, "remote": {"uploaded_size": 0}}
        events = iter(uploaded_sizes)
        transport.preliminary_file_status.side_effect = (
            lambda file_id, **kwargs: {"id": file_id, "remote": {"uploaded_size": next(events)}}
        )
        sink = Mock()
        context = SimpleNamespace(cancel_token=CancellationToken(), event_sink=sink)
        uploader = PreliminaryUploader(transport, 1, 0.4)
        self.addCleanup(uploader.close)
        clock = SimpleNamespace(elapsed=0.0)

        def advance(seconds, **kwargs):
            clock.elapsed += seconds
            return True

        with patch("tdlib_media_uploader.telegram.preliminary.monotonic", lambda: clock.elapsed), \
             patch("tdlib_media_uploader.telegram.preliminary.cancelable_sleep", advance):
            result = uploader.prepare((content,), context=context)
        self.assertTrue(all(0 < call.kwargs["timeout"] <= 0.4
                            for call in transport.preliminary_file_status.call_args_list))
        progress = [call.args[0] for call in sink.emit.call_args_list if isinstance(call.args[0], PreliminaryProgressEvent)]
        return result, clock, transport, progress

    def test_timed_out_native_start_cancels_late_file_id_from_same_lease(self):
        client = self._idle_client()
        client._config = SimpleNamespace(TDLIB_REQUEST_TIMEOUT=0.01)
        with self.assertRaises(TimeoutError):
            client.preliminary_upload_file({"@type": "inputFileLocal", "path": "a.jpg"}, "fileTypePhoto")
        extra = client.send_raw.call_args.args[0]["@extra"]
        self.assertFalse(client.pending)
        client.release_job(SimpleNamespace())
        client._deliver_request_reply(extra, {"@type": "file", "id": 791})
        self.assertEqual(client.send_raw.call_args.args[0], {"@type": "cancelPreliminaryUploadFile", "file_id": 791})

    def test_cancelled_native_start_stops_waiting_promptly_and_fences_late_file_id(self):
        client = self._idle_client()
        client._config = SimpleNamespace(TDLIB_REQUEST_TIMEOUT=90)
        token = CancellationToken()
        client.send_raw.side_effect = lambda payload: token.cancel()
        terminated = threading.Event()
        failures = []

        def request():
            try:
                client.preliminary_upload_file({"@type": "inputFileLocal", "path": "a.jpg"}, "fileTypePhoto",
                                               cancel_token=token.is_cancelled)
            except Exception as error:
                failures.append(error)
            finally:
                terminated.set()

        threading.Thread(target=request, daemon=True).start()
        self.assertTrue(terminated.wait(0.5))
        self.assertIsInstance(failures[0], TDLibCancelled)
        extra = client.send_raw.call_args.args[0]["@extra"]
        client.preliminary_generation += 1
        client._deliver_request_reply(extra, {"@type": "file", "id": 791})
        client.send_raw.assert_called_once()

    def test_status_request_uses_configured_timeout_and_respects_stall_budget(self):
        client = self._idle_client()
        client._config = SimpleNamespace(TDLIB_REQUEST_TIMEOUT=17)
        client._request_once = Mock()
        client.preliminary_file_status(91)
        self.assertEqual(client._request_once.call_args.kwargs["timeout"], 17)
        client.preliminary_file_status(91, timeout=9)
        self.assertEqual(client._request_once.call_args.kwargs["timeout"], 9)

    def test_later_file_can_finish_first_but_messages_keep_album_order(self):
        result, transport, journal = self._run(cancel=False)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(transport.ready[:2], [2, 1])
        self.assertEqual(transport.sent, [(1, 1), (2, 2)])
        self.assertTrue(transport.prepared_before_message)
        self.assertFalse(journal.records)

    def test_cancel_during_preliminary_upload_leaves_no_unknown(self):
        result, transport, journal = self._run(cancel=True)
        self.assertEqual(result.status, "CANCELLED")
        self.assertFalse(transport.sent)
        self.assertTrue(transport.cancelled.wait(1))
        self.assertFalse(journal.records)

    def test_unsupported_preliminary_upload_falls_back_to_ordered_direct_send(self):
        result, transport, journal = self._run(cancel=False, unavailable=True)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(transport.sent, [(1, "local"), (2, "local")])
        self.assertFalse(journal.records)

    def test_released_lease_cannot_submit_a_preliminary_native_request(self):
        client = self._idle_client()
        with self.assertRaises(TDLibCancelled):
            client.preliminary_upload_file({"@type": "inputFileLocal", "path": "a.jpg"},
                                           "fileTypePhoto", generation=2)
        client.send_raw.assert_not_called()
        self.assertFalse(client.pending)

    def test_immediate_stop_cancels_setup_without_closing_ready_session(self):
        client = self._idle_client()
        client.cancel_upload_work()
        client.cancel.assert_not_called()
        with self.assertRaises(TDLibCancelled):
            client.request({"@type": "getChat"}, timeout=1)
        client.send_raw.assert_not_called()

    @staticmethod
    def _idle_client():
        client = object.__new__(TDJsonClient)
        client.authorization_ready = True
        client._message_send_active = False
        client.preliminary_generation = 3
        client._upload_work_cancelled = threading.Event()
        client._upload_work_lock = threading.Lock()
        client.cancel_event = threading.Event()
        client.pending_lock = threading.Lock()
        client.pending = {}
        client.closed_event = threading.Event()
        client.close_sent = False
        client.send_raw = Mock()
        client.cancel = Mock()
        return client

    def _run(self, *, cancel, unavailable=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        token = CancellationToken()
        journal = MemoryJournalStore()
        items = []
        for number in (1, 2):
            path = root / f"{number}.jpg"
            path.write_bytes(b"abc")
            items.append(MediaItem(path, root, "image", FileSnapshot(str(path), 3, path.stat().st_mtime_ns)))
        plans = tuple(AlbumPlan(str(index), "image", root, "", index, (item,), (item,), "")
                      for index, item in enumerate(items, 1))

        class Strategy:
            kind = "image"

            def build_contents(self, plan, **_):
                path = str(plan.items[0].path)
                return ({"@type": "inputMessagePhoto", "photo": {
                    "@type": "inputPhoto", "photo": {"@type": "inputFileLocal", "path": path},
                    "thumbnail": {"@type": "inputThumbnail", "thumbnail": {"@type": "inputFileLocal", "path": path}},
                }},)

        class Transport:
            def __init__(self):
                self.ready = []
                self.sent = []
                self.prepared_before_message = True
                self.cancelled = threading.Event()
                self.second_file_ready = threading.Event()

            def preliminary_upload_file(self, source, file_type):
                if unavailable:
                    raise RuntimeError("unsupported")
                if journal.records:
                    self.prepared_before_message = False
                number = int(Path(source["path"]).stem)
                if cancel:
                    token.cancel()
                if number == 2 and not cancel:
                    self.ready.append(number)
                    self.second_file_ready.set()
                    return {"id": number, "remote": {"uploaded_size": 3}}
                return {"id": number, "remote": {"uploaded_size": 0}}

            def preliminary_file_status(self, number):
                if self.second_file_ready.is_set():
                    self.ready.append(number)
                    return {"id": number, "remote": {"uploaded_size": 3}}
                return {"id": number, "remote": {"uploaded_size": 0}}

            def cancel_preliminary_upload_file(self, _file_id):
                self.cancelled.set()

        transport = Transport()
        uploader = PreliminaryUploader(transport, 2, 2)
        self.addCleanup(uploader.close)

        def send(contents, *, plan, **_):
            photo = contents[0]["photo"]
            self.assertEqual(photo["thumbnail"]["thumbnail"]["@type"], "inputFileLocal")
            transport.sent.append((plan.number, photo["photo"].get("id", "local")))
            return [plan.number]

        source = UploadContext(source_root=root, target={}, kind="image", journal=journal, cancel_token=token,
                               sender=SimpleNamespace(send=send, prepare_remote=uploader.prepare),
                               event_sink=SimpleNamespace(emit=lambda _: None),
                               plans=plans, scan_result=ScanResult(tuple(items)),
                               metadata={"prefetch_next": True, "prefetch_slots": 2})
        return UploadEngine().run(Strategy(), context=source), transport, journal


class PreliminaryProgressGuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_qt_progress_signal_uses_task_page_size_formatter(self):
        from PySide6.QtCore import QObject, Signal
        from tdlib_media_uploader.gui.pages.task import TaskPage

        class Emitter(QObject):
            progress = Signal(object)

        page = TaskPage(size_formatter=lambda value: f"{value}B")
        self.addCleanup(page.close)
        emitter = Emitter()
        emitter.progress.connect(page.show_preliminary_progress)
        emitter.progress.emit(PreliminaryProgressEvent(uploaded_bytes=12, total_bytes=42, bytes_per_second=3))
        self.assertEqual(page.preliminary_metrics.text(), "预上传 12B / 42B · 3B/s")
        self.assertFalse(page.preliminary_metrics.isHidden())
