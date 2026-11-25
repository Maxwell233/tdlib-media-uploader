from __future__ import annotations

from pathlib import Path
import sys
import threading
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader import (  # noqa: E402
    AlbumPlan,
    BatchStatus,
    FileSnapshot,
    MediaItem,
    ScanResult,
)
from tdlib_media_uploader.telegram.send_result import SendResult  # noqa: E402
from tdlib_media_uploader.upload.engine import (  # noqa: E402
    MemoryJournalStore,
    MemoryStateStore,
    RUN_CANCELLED,
    RUN_UNKNOWN,
    UploadCancelled,
    UploadEngine,
)


class _Token:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.safe_event = threading.Event()

    def is_cancelled(self):
        return self.cancel_event.is_set()

    def raise_if_cancelled(self):
        if self.is_cancelled():
            raise UploadCancelled("立即中断")

    def stop_after_current(self):
        return self.safe_event.is_set()


class _Strategy:
    kind = "image"

    def __init__(self):
        root = Path("/media")
        self.items = tuple(
            MediaItem(
                path=root / f"{index}.jpg",
                source_root=root,
                media_kind="image",
                snapshot=FileSnapshot(str(root / f"{index}.jpg"), 10 + index, 100 + index),
            )
            for index in (1, 2)
        )
        self.contents_calls = []

    def scan(self, source_root, *, cancel_token, event_sink, context=None):
        return ScanResult(self.items)

    def build_plans(self, scan_result, *, target, state=None, context=None):
        return tuple(
            AlbumPlan(
                key=f"album-{index}",
                kind=self.kind,
                source_root=self.items[index - 1].source_root,
                group_label="group",
                number=index,
                items=(self.items[index - 1],),
                pending_items=(self.items[index - 1],),
                caption="caption",
                target=target,
            )
            for index in (1, 2)
        )

    def build_contents(self, plan, *, cancel_token, event_sink, context=None):
        self.contents_calls.append(plan.key)
        return ({"@type": "inputMessagePhoto", "path": str(plan.pending_items[0].path)},)


class _Sender:
    def __init__(self, token=None, *, safe_after_first=False, abort_after_first=False):
        self.token = token
        self.safe_after_first = safe_after_first
        self.abort_after_first = abort_after_first
        self.calls = 0

    def send_contents(self, contents, target=None, plan=None, context=None):
        self.calls += 1
        if self.safe_after_first:
            self.token.safe_event.set()
        if self.abort_after_first:
            self.token.cancel_event.set()
            raise UploadCancelled("sender cancelled after submit")
        return SendResult(BatchStatus.CONFIRMED, succeeded_ids=(100 + self.calls,))


class StopSemanticsTest(unittest.TestCase):
    def test_safe_stop_before_first_album_does_not_call_sender(self):
        token = _Token()
        token.safe_event.set()
        sender = _Sender(token)
        result = UploadEngine(sender=sender).run(
            _Strategy(), source_root=Path("/media"), target={}, cancel_token=token
        )
        self.assertEqual(result.status, RUN_CANCELLED)
        self.assertTrue(result.cancelled)
        self.assertEqual(sender.calls, 0)

    def test_safe_stop_finishes_current_album_and_skips_next(self):
        token = _Token()
        strategy = _Strategy()
        state = MemoryStateStore()
        journal = MemoryJournalStore()
        sender = _Sender(token, safe_after_first=True)
        result = UploadEngine(sender=sender, state=state, journal=journal).run(
            strategy, source_root=Path("/media"), target={}, cancel_token=token
        )
        self.assertEqual(result.status, RUN_CANCELLED)
        self.assertTrue(result.cancelled)
        self.assertEqual(sender.calls, 1)
        self.assertEqual(strategy.contents_calls, ["album-1"])
        self.assertEqual(result.batches[0].status, BatchStatus.CONFIRMED)
        self.assertEqual(journal.records, {})
        self.assertEqual(len(state.completed), 1)

    def test_immediate_abort_before_sender_does_not_create_dangerous_journal(self):
        token = _Token()
        token.cancel_event.set()
        sender = _Sender(token)
        journal = MemoryJournalStore()
        result = UploadEngine(sender=sender, journal=journal).run(
            _Strategy(), source_root=Path("/media"), target={}, cancel_token=token
        )
        self.assertEqual(result.status, RUN_CANCELLED)
        self.assertEqual(sender.calls, 0)
        self.assertEqual(journal.records, {})

    def test_immediate_abort_after_sender_preserves_unknown_and_blocks_restart(self):
        token = _Token()
        strategy = _Strategy()
        journal = MemoryJournalStore()
        sender = _Sender(token, abort_after_first=True)
        result = UploadEngine(sender=sender, journal=journal).run(
            strategy, source_root=Path("/media"), target={}, cancel_token=token
        )
        self.assertEqual(result.status, RUN_UNKNOWN)
        self.assertTrue(result.cancelled)
        self.assertEqual(sender.calls, 1)
        self.assertEqual(journal.unresolved("image", "album-1")["status"], "UNKNOWN")

        restart_sender = _Sender(_Token())
        restart = UploadEngine(sender=restart_sender, journal=journal).run(
            _Strategy(), source_root=Path("/media"), target={}
        )
        self.assertEqual(restart.status, RUN_UNKNOWN)
        self.assertEqual(restart_sender.calls, 1)

    def test_journal_read_error_is_fail_closed_before_sender(self):
        class BrokenJournal(MemoryJournalStore):
            def unresolved(self, *args, **kwargs):
                raise PermissionError("journal unreadable")

        sender = _Sender(_Token())
        result = UploadEngine(sender=sender, journal=BrokenJournal()).run(
            _Strategy(), source_root=Path("/media"), target={}
        )
        self.assertEqual(result.status, RUN_UNKNOWN)
        self.assertEqual(sender.calls, 0)


if __name__ == "__main__":
    unittest.main()
