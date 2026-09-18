from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.core.upload_journal import (  # noqa: E402
    CONFIRMED,
    InflightJournal,
    PREPARED,
    SUBMITTED,
    UNKNOWN,
)
from tdlib_media_uploader import AlbumPlan, BatchStatus, FileSnapshot, MediaItem, ScanResult  # noqa: E402
from tdlib_media_uploader.upload.reconciliation import ReconciliationService  # noqa: E402
from tdlib_media_uploader.upload.engine import (  # noqa: E402
    MemoryJournalStore,
    MemoryStateStore,
    RUN_COMPLETED,
    UploadEngine,
)


class _State:
    def __init__(self, journal, *, target=None, fail=False):
        self.journal = journal
        self.target = target
        self.fail = fail
        self.calls = []
        self.saw_protection = False

    def mark_album_completed(self, items, message_ids=None):
        self.saw_protection = self.journal.unresolved("image", "album", target=self.target) is not None
        self.calls.append((list(items), list(message_ids or [])))
        if self.fail:
            raise OSError("disk full")


def _target(topic=7):
    return {
        "target_mode": "forum_topic",
        "chat_id": -1001,
        "forum_topic_id": topic,
        "channel_chat_id": 0,
    }


class UploadReconciliationTest(unittest.TestCase):
    def _journal(self, directory: str, *, target=None, items=True):
        root = Path(directory) / "journal"
        journal = InflightJournal(root)
        values = None
        if items:
            source = Path(directory) / "photo.jpg"
            source.write_bytes(b"photo")
            stat = source.stat()
            values = [{"path": source, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}]
        journal.prepare("image", "album", values, target=target)
        return journal

    def test_prepared_submitted_and_unknown_can_be_marked_not_sent(self):
        for index, status in enumerate((PREPARED, SUBMITTED, UNKNOWN), start=1):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                journal = self._journal(directory)
                if status != PREPARED:
                    journal.update("image", "album", status, message_ids=[index])
                ReconciliationService(journal).reconcile_inflight(
                    "album", kind="image", sent=False
                )
                self.assertIsNone(journal.get("image", "album"))

    def test_confirmed_cannot_be_marked_not_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.update("image", "album", CONFIRMED, message_ids=[11])
            with self.assertRaises(ValueError):
                ReconciliationService(journal).reconcile_inflight(
                    "album", kind="image", sent=False
                )
            self.assertEqual(journal.unresolved("image", "album")["status"], CONFIRMED)

    def test_manual_sent_checkpoint_happens_before_finalize(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.update("image", "album", UNKNOWN, message_ids=[11])
            state = _State(journal)
            service = ReconciliationService(journal)
            service._state_for_journal = lambda *_args, **_kwargs: state
            service.reconcile_inflight("album", kind="image", sent=True)
            self.assertTrue(state.saw_protection)
            self.assertEqual(state.calls[0][1], [11])
            self.assertIsNone(journal.get("image", "album"))

    def test_checkpoint_failure_keeps_protection_record(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.update("image", "album", UNKNOWN, message_ids=[11])
            state = _State(journal, fail=True)
            service = ReconciliationService(journal)
            service._state_for_journal = lambda *_args, **_kwargs: state
            with self.assertRaises(OSError):
                service.reconcile_inflight("album", kind="image", sent=True)
            self.assertEqual(journal.unresolved("image", "album")["status"], UNKNOWN)

    def test_confirmed_recovery_repairs_state_and_then_finalizes(self):
        with tempfile.TemporaryDirectory() as directory:
            target = _target()
            journal = self._journal(directory, target=target)
            journal.update("image", "album", CONFIRMED, message_ids=[21], target=target)
            state = _State(journal, target=target)
            repaired = ReconciliationService(journal).recover_confirmed(
                "image", "album", target=target, state=state
            )
            self.assertTrue(repaired)
            self.assertTrue(state.saw_protection)
            self.assertEqual(state.calls[0][1], [21])
            self.assertIsNone(journal.get("image", "album", target=target))

    def test_confirmed_recovery_failure_retains_record(self):
        with tempfile.TemporaryDirectory() as directory:
            target = _target()
            journal = self._journal(directory, target=target)
            journal.update("image", "album", CONFIRMED, message_ids=[21], target=target)
            state = _State(journal, target=target, fail=True)
            with self.assertRaises(OSError):
                ReconciliationService(journal).recover_confirmed(
                    "image", "album", target=target, state=state
                )
            self.assertEqual(
                journal.unresolved("image", "album", target=target)["status"],
                CONFIRMED,
            )

    def test_missing_snapshot_retains_confirmed_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory, items=False)
            journal.update("image", "album", CONFIRMED, message_ids=[21])
            with self.assertRaises(RuntimeError):
                ReconciliationService(journal).recover_confirmed(
                    "image", "album", state=_State(journal)
                )
            self.assertIsNotNone(journal.unresolved("image", "album"))

    def test_legacy_record_can_still_be_removed_when_user_confirms_not_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory, items=False)
            journal.update("image", "album", UNKNOWN)
            ReconciliationService(journal).reconcile_inflight(
                "album", kind="image", sent=False
            )
            self.assertIsNone(journal.get("image", "album"))

    def test_confirmed_recovery_uses_recorded_target_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            target = _target(9)
            journal = self._journal(directory, target=target)
            journal.update("image", "album", CONFIRMED, message_ids=[31], target=target)
            state = _State(journal, target=target)
            service = ReconciliationService(journal)
            self.assertTrue(service.recover_confirmed("image", "album", state=state))
            self.assertIsNone(journal.get("image", "album", target=target))

    def test_engine_repairs_confirmed_checkpoint_without_resending(self):
        root = Path("/media")
        item = MediaItem(
            path=root / "confirmed.jpg",
            source_root=root,
            media_kind="image",
            snapshot=FileSnapshot(str(root / "confirmed.jpg"), 12, 34),
        )

        class Strategy:
            kind = "image"

            def scan(self, source_root, *, cancel_token, event_sink, context=None):
                return ScanResult((item,))

            def build_plans(self, scan_result, *, target, state=None, context=None):
                return (
                    AlbumPlan(
                        key="album",
                        kind="image",
                        source_root=root,
                        group_label="group",
                        number=1,
                        items=(item,),
                        pending_items=(item,),
                        caption="",
                        target=target,
                    ),
                )

            def build_contents(self, plan, *, cancel_token, event_sink, context=None):
                raise AssertionError("CONFIRMED recovery must not build or resend contents")

        class Sender:
            calls = 0

            def send_contents(self, contents, **kwargs):
                self.calls += 1
                return {"status": BatchStatus.CONFIRMED.value, "succeeded_ids": [99]}

        journal = MemoryJournalStore()
        journal.prepare(
            "image",
            "album",
            [{"path": item.path, "scan_size": 12, "scan_mtime_ns": 34, "media_kind": "image"}],
        )
        journal.mark_confirmed("image", "album", [88])
        sender = Sender()
        result = UploadEngine(
            sender=sender,
            state=MemoryStateStore(),
            journal=journal,
        ).run(Strategy(), source_root=root, target={})
        self.assertEqual(result.status, RUN_COMPLETED)
        self.assertEqual(sender.calls, 0)
        self.assertEqual(journal.records, {})


if __name__ == "__main__":
    unittest.main()
