from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


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
    RUN_PARTIAL,
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

    def test_inflight_kind_can_be_inferred_for_manual_reconciliation(self):
        from tdlib_media_uploader.telegram import tdlib_common

        with tempfile.TemporaryDirectory() as directory:
            journal = InflightJournal(Path(directory))
            journal.prepare("mixed", "album-key", [{"path": "clip.jpg"}])
            journal.unknown("mixed", "album-key", "connection lost")
            client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
            client.inflight_journal = journal
            client.reconcile_inflight("album-key", sent=False)
            self.assertIsNone(journal.get("mixed", "album-key"))

    def test_manual_sent_reconciliation_writes_checkpoint_before_removing_journal(self):
        from tdlib_media_uploader.telegram import tdlib_common
        from tdlib_media_uploader.media import legacy_image as image_core

        target = _target()
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
        from tdlib_media_uploader.telegram import tdlib_common
        from tdlib_media_uploader.media import legacy_image as image_core

        target = {"target_mode": "forum_topic", "chat_id": -1001, "forum_topic_id": 7}
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
                [{"path": path, "size": snapshot.st_size, "mtime_ns": snapshot.st_mtime_ns}],
                target=target,
            )
            journal.unknown("image", "album-key", "timeout", target=target)
            state_dir = Path(directory) / "state"
            with patch.object(image_core.cfg, "IMAGE_DIR", root), patch.object(image_core, "STATE_DIR", state_dir):
                image_core.UploadState(target=target)
                client = tdlib_common.TDJsonClient.__new__(tdlib_common.TDJsonClient)
                client.inflight_journal = journal
                with patch.object(image_core.UploadState, "_save", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        client.reconcile_inflight(
                            "album-key", sent=True, kind="image", target=target
                        )
            self.assertIsNotNone(journal.unresolved("image", "album-key", target=target))

    def test_legacy_reconciliation_uses_kind_target_instead_of_global_target(self):
        from tdlib_media_uploader.telegram import tdlib_common
        from tdlib_media_uploader.media import legacy_image as image_core
        from tdlib_media_uploader.media import legacy_mixed as mixed_core
        from tdlib_media_uploader.media import legacy_video as video_core

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
        from tdlib_media_uploader.telegram import tdlib_common
        from tdlib_media_uploader.media import legacy_image as image_core
        from tdlib_media_uploader.core.upload_journal import normalize_target

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

    def test_engine_reports_confirmed_recovery_as_partial_and_keeps_protection(self):
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

        class FailingState(MemoryStateStore):
            def mark_album_completed(self, items, message_ids=None):
                raise OSError("disk full")

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
            state=FailingState(),
            journal=journal,
        ).run(Strategy(), source_root=root, target={})

        self.assertEqual(result.status, RUN_PARTIAL)
        self.assertEqual(sender.calls, 0)
        self.assertEqual(result.batches[0].status, BatchStatus.CONFIRMED)
        self.assertIn("CONFIRMED 断点恢复失败", result.error)
        self.assertEqual(journal.unresolved("image", "album")["status"], CONFIRMED)


if __name__ == "__main__":
    unittest.main()
