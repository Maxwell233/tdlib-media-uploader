"""Delivery recovery must never turn a partial Album into missing or duplicate files."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from tdlib_media_uploader import BatchStatus, FileSnapshot, MediaItem
from tdlib_media_uploader.core.identity import validate_send_target
from tdlib_media_uploader.core.upload_journal import InflightJournal
from tdlib_media_uploader.core.upload_state import UploadState
from tdlib_media_uploader.telegram.send_result import SendResult, map_send_result
from tdlib_media_uploader.telegram.tdlib_common import TDJsonClient, TDLibError, TDLibCancelled, SendResultUnknown
from tdlib_media_uploader.upload.engine import UploadEngine, MemoryJournalStore, MemoryStateStore, _item_payload
from tdlib_media_uploader.upload.reconciliation import ReconciliationService
from test_phase5_engine_contract import _Strategy, _Sender, _items
import test_gui_integration as gui_tests


TARGET = {"target_mode": "channel", "chat_id": -1001}
CONTENT = {"@type": "inputMessagePhoto", "photo": {"@type": "inputFileId", "id": 1}}


class DeliveryReviewTest(unittest.TestCase):
    def result(self):
        return map_send_result([
            {"id": -1, "sending_state": {"@type": "messageSendingStatePending"}},
            {"id": -2, "sending_state": {"@type": "messageSendingStateFailed"}},
            {"id": -3, "sending_state": {"@type": "messageSendingStatePending"}},
        ], [{"@type": "updateMessageSendSucceeded", "old_message_id": -1,
             "message": {"id": 101}}])

    def setup_record(self, root):
        items = []
        for index in range(3):
            path = root / f"{index}.jpg"
            path.write_bytes(b"photo")
            stat = path.stat()
            items.append(MediaItem(path, root, "image", FileSnapshot(str(path), stat.st_size, stat.st_mtime_ns)))
        journal = InflightJournal(root / "journal")
        journal.prepare("image", "album-1", [_item_payload(item) for item in items], target=TARGET)
        result = self.result()
        journal.update("image", "album-1", "UNKNOWN", target=TARGET,
                       message_ids=result.message_ids, succeeded_ids=result.succeeded_ids,
                       failed_ids=result.failed_ids, pending_ids=result.pending_ids,
                       item_results=result.item_results)
        state = UploadState(kind="image", source_root=root, target=TARGET, state_dir=root / "state")
        service = ReconciliationService(journal)
        service._state_for_journal = lambda *_args, **_kwargs: state
        return items, journal, state, service

    def test_message_order_survives_final_id_replacement(self):
        result = map_send_result([
            {"id": -1, "sending_state": {"@type": "messageSendingStateFailed"}},
            {"id": -2, "sending_state": {"@type": "messageSendingStatePending"}},
        ], [{"@type": "updateMessageSendSucceeded", "old_message_id": -2, "message": {"id": 202}}])
        self.assertEqual(result.message_ids, (-1, 202))
        self.assertEqual(result.item_results[1]["old_message_id"], -2)
        self.assertEqual(result.item_results[1]["message_id"], 202)

    def test_partial_recovery_only_checkpoints_success_and_protects_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            items, journal, state, service = self.setup_record(Path(directory))
            service.reconcile_inflight("album-1", kind="image", sent=True, target=TARGET)
            self.assertTrue(state.is_completed(_item_payload(items[0])))
            self.assertFalse(state.is_completed(_item_payload(items[1])))
            self.assertFalse(state.is_completed(_item_payload(items[2])))
            self.assertIsNone(journal.unresolved_for_items("image", [_item_payload(items[1])], target=TARGET))
            remaining = journal.unresolved_for_items("image", [_item_payload(items[2])], target=TARGET)
            self.assertEqual(len(remaining["items"]), 1)
            self.assertTrue(remaining["reconciled"])

            sender = _Sender(SendResult(BatchStatus.CONFIRMED, succeeded_ids=(202,)))
            run = UploadEngine(sender=sender, journal=journal, state=state, prefetch_next=False).run(
                _Strategy(items), source_root=Path(directory), target=TARGET)
            self.assertEqual(sender.calls, 1)
            self.assertTrue(state.is_completed(_item_payload(items[1])))
            self.assertFalse(state.is_completed(_item_payload(items[2])))
            self.assertNotEqual(run.status, "COMPLETED")
            self.assertIsNotNone(journal.unresolved("image", "album-1", target=TARGET))

    def test_failed_checkpoint_keeps_original_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            _items_, journal, state, service = self.setup_record(Path(directory))
            with patch.object(state, "mark_album_completed", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    service.reconcile_inflight("album-1", kind="image", sent=True, target=TARGET)
            self.assertEqual(len(journal.get("image", "album-1", target=TARGET)["items"]), 3)

    def test_known_success_cannot_be_released_or_entire_partial_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            _items_, journal, _state, service = self.setup_record(Path(directory))
            for kwargs in ({"sent": False}, {"sent": True, "item_decisions": {0: "not_sent"}}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    service.reconcile_inflight("album-1", kind="image", target=TARGET, **kwargs)
            self.assertEqual(len(journal.get("image", "album-1", target=TARGET)["items"]), 3)

    def test_legacy_grouped_results_require_explicit_file_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            items, journal, state, service = self.setup_record(Path(directory))
            journal.update("image", "album-1", "UNKNOWN", item_results=[], target=TARGET)
            service.reconcile_inflight("album-1", kind="image", sent=True, target=TARGET)
            self.assertFalse(state.is_completed(_item_payload(items[0])))
            self.assertEqual(len(journal.get("image", "album-1", target=TARGET)["items"]), 3)
            service.reconcile_inflight("album-1", kind="image", sent=True, target=TARGET,
                                       item_decisions={0: "sent", 1: "not_sent"})
            self.assertTrue(state.is_completed(_item_payload(items[0])))
            self.assertFalse(state.is_completed(_item_payload(items[1])))
            self.assertEqual(len(journal.get("image", "album-1", target=TARGET)["items"]), 1)

    def test_duplicate_item_mapping_does_not_release_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            _items_, journal, _state, service = self.setup_record(Path(directory))
            rows = [dict(row) for row in self.result().item_results]
            rows[1]["index"] = 0
            journal.update("image", "album-1", "UNKNOWN", item_results=rows, target=TARGET)
            with self.assertRaises(ValueError):
                service.reconcile_inflight("album-1", kind="image", sent=True, target=TARGET)
            self.assertEqual(len(journal.get("image", "album-1", target=TARGET)["items"]), 3)

    def test_missing_item_path_cannot_shift_a_success_to_a_failed_file(self):
        with tempfile.TemporaryDirectory() as directory:
            _items_, journal, state, service = self.setup_record(Path(directory))
            path, record = journal.get_entry("image", "album-1", TARGET)
            record["items"][0] = {}
            journal._write(path, record)
            with self.assertRaises(RuntimeError):
                service.reconcile_inflight("album-1", kind="image", sent=True, target=TARGET)
            self.assertEqual(state.data["completed"], {})
            self.assertIsNotNone(journal.get("image", "album-1", target=TARGET))

    def client(self):
        client = TDJsonClient.__new__(TDJsonClient)
        client._safe_diagnose_upload_failure = Mock()
        client.caption_length_limit = 10
        return client

    def test_production_local_validation_failure_is_safe_to_retry(self):
        client = self.client()
        client.request = Mock()
        content = {**CONTENT, "caption": {"@type": "formattedText", "text": "x" * 11}}
        with self.assertRaises(ValueError) as caught:
            client.send_contents([content], target=TARGET)
        self.assertIs(caught.exception.submitted, False)
        client.request.assert_not_called()
        journal = MemoryJournalStore()
        run = UploadEngine(sender=_Sender(raise_error=caught.exception), journal=journal).run(
            _Strategy(_items()), source_root=Path("/media"), target={})
        self.assertEqual(run.status, "FAILED")

    def test_local_gui_adapter_failure_does_not_claim_submission(self):
        from tdlib_media_uploader.gui.integration import TDLibSender
        client = Mock()
        progress = Mock()
        progress.begin_album.side_effect = ValueError("invalid local progress data")
        sender = TDLibSender(client, progress, Mock())
        with self.assertRaises(ValueError) as caught:
            sender.send_contents([CONTENT], target=TARGET,
                                 plan=SimpleNamespace(pending_items=_items()))
        self.assertIs(caught.exception.submitted, False)
        client.send_contents.assert_not_called()

    def test_error_after_request_remains_unknown(self):
        client = self.client()
        client.request = Mock(return_value={"id": -1})
        client.wait_for_send_results = Mock(side_effect=ValueError("broken response handler"))
        with self.assertRaises(ValueError) as caught:
            client.send_contents([CONTENT], target=TARGET)
        self.assertIs(caught.exception.submitted, True)
        run = UploadEngine(sender=_Sender(raise_error=caught.exception)).run(
            _Strategy(_items()), source_root=Path("/media"), target={})
        self.assertEqual(run.status, "UNKNOWN")

    def test_explicit_tdlib_rejection_is_safe_to_retry(self):
        client = self.client()
        client.request = Mock(side_effect=TDLibError(400, "INVALID_REQUEST"))
        with self.assertRaises(TDLibError) as caught:
            client.send_contents([CONTENT], target=TARGET)
        self.assertIs(caught.exception.submitted, False)

    def test_cancel_before_native_request_is_not_ambiguous(self):
        client = self.client()
        client.cancel_event = threading.Event()
        client.cancel_event.set()
        client.send_raw = Mock()
        with self.assertRaises(TDLibCancelled) as caught:
            client.send_contents([CONTENT], target=TARGET)
        self.assertIs(caught.exception.submitted, False)
        client.send_raw.assert_not_called()

    def test_gui_import_does_not_create_config_before_startup_dialog(self):
        code = """
from pathlib import Path
import tempfile
from unittest.mock import patch
from tdlib_media_uploader.config import paths
from tdlib_media_uploader.core import persistence
with tempfile.TemporaryDirectory() as directory:
    missing = Path(directory) / 'data' / 'config.toml'
    with patch.object(paths, 'CONFIG_PATH', missing), patch.object(persistence, 'atomic_write_text', side_effect=AssertionError('import wrote config')):
        from tdlib_media_uploader.gui import main_window
        assert not missing.exists()
"""
        checked = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_confirmed_legacy_record_cannot_be_partially_released(self):
        with tempfile.TemporaryDirectory() as directory:
            _items_, journal, _state, service = self.setup_record(Path(directory))
            journal.update("image", "album-1", "CONFIRMED", item_results=[], target=TARGET)
            with self.assertRaises(ValueError):
                service.reconcile_inflight("album-1", kind="image", sent=True, target=TARGET,
                                           item_decisions={0: "not_sent"})
            self.assertEqual(journal.get("image", "album-1", target=TARGET)["status"], "CONFIRMED")

    def test_observed_partial_outcome_is_published_before_timeout(self):
        client = self.client()
        client._config = SimpleNamespace(TDLIB_MESSAGE_SEND_TIMEOUT=0)
        client.cancel_event = threading.Event()
        client.send_condition = threading.Condition()
        client.send_events = {-1: ("success", {"message": {"id": 101}})}
        captured = []
        with self.assertRaises(SendResultUnknown):
            client.wait_for_send_results([
                {"id": -1, "sending_state": {"@type": "messageSendingStatePending"}},
                {"id": -2, "sending_state": {"@type": "messageSendingStatePending"}},
            ], on_result=captured.append)
        self.assertEqual(captured[0]["item_results"][0]["message_id"], 101)
        self.assertEqual(captured[0]["item_results"][1]["status"], "pending")

    def test_slow_close_preserves_successful_upload_result(self):
        case = gui_tests.Phase6GuiIntegrationTest("test_upload_uses_engine_lifecycle_and_cleans_only_after_checkpoint")
        with patch.object(gui_tests._Client, "close", side_effect=TimeoutError("slow close")):
            case.test_upload_uses_engine_lifecycle_and_cleans_only_after_checkpoint()

    def test_invalid_target_is_rejected_without_changing_identity_rules(self):
        for target in ({"target_mode": "channel", "chat_id": 0},
                       {"target_mode": "forum_topic", "chat_id": -1, "forum_topic_id": 0}):
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_send_target(target)
        self.assertEqual(validate_send_target(TARGET)["chat_id"], -1001)


if __name__ == "__main__":
    unittest.main()
