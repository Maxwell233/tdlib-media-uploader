from __future__ import annotations

import sys
import unittest
from pathlib import Path


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
from tdlib_media_uploader.upload import (  # noqa: E402
    MemoryJournalStore,
    MemoryStateStore,
    SEND_STATE_FLOW,
    PreflightDecision,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PARTIAL,
    RUN_UNKNOWN,
    SEND_STATES,
    UploadCancelled,
    UploadEngine,
    preflight_plan,
    restrict_plan,
)


class _Token:
    def __init__(self, cancelled: bool = False):
        self.cancelled = cancelled

    def is_cancelled(self):
        return self.cancelled

    def raise_if_cancelled(self):
        if self.cancelled:
            raise UploadCancelled("cancelled")


class _Events:
    def __init__(self):
        self.values = []

    def emit(self, event):
        self.values.append(event)


class _Strategy:
    kind = "image"

    def __init__(self, items):
        self.items = tuple(items)
        self.contents_calls = []

    def scan(self, source_root, *, cancel_token, event_sink, context=None):
        return ScanResult(self.items)

    def build_plans(self, scan_result, *, target, state=None, context=None):
        return (
            AlbumPlan(
                key="album-1",
                kind=self.kind,
                source_root=self.items[0].source_root,
                group_label="group",
                number=1,
                items=self.items,
                pending_items=self.items,
                caption="caption",
                target=target,
            ),
        )

    def build_contents(self, plan, *, cancel_token, event_sink, context=None):
        self.contents_calls.append(plan)
        return tuple({"@type": "inputMessagePhoto", "path": str(item.path)} for item in plan.pending_items)


class _Sender:
    def __init__(self, result=None, raise_error=None):
        self.result = result
        self.raise_error = raise_error
        self.calls = 0

    def send_contents(self, contents, target, *, caption_limit=None, timeout=None):
        self.calls += 1
        if self.raise_error is not None:
            raise self.raise_error
        return self.result


def _items(count=2):
    root = Path("/media")
    return tuple(
        MediaItem(
            path=root / f"{index}.jpg",
            source_root=root,
            media_kind="image",
            snapshot=FileSnapshot(str(root / f"{index}.jpg"), 10 + index, 100 + index),
        )
        for index in range(count)
    )


class Phase5EngineContractTest(unittest.TestCase):
    def test_durable_send_states_remain_the_v19_five_states(self):
        self.assertEqual(SEND_STATES, ("PREPARED", "SUBMITTED", "CONFIRMED", "FAILED", "UNKNOWN"))

    def test_restrict_plan_preserves_complete_boundary(self):
        items = _items(3)
        plan = _Strategy(items).build_plans(ScanResult(items), target={})[0]
        narrowed = restrict_plan(plan, (items[1],))
        self.assertEqual(narrowed.items, items)
        self.assertEqual(narrowed.pending_items, (items[1],))

    def test_preflight_preserves_order_and_partitions_items(self):
        items = _items(3)
        plan = _Strategy(items).build_plans(ScanResult(items), target={})[0]

        def checker(item):
            if item is items[1]:
                return PreflightDecision.deferred("network file")
            if item is items[2]:
                return PreflightDecision.failed("unreadable")
            return PreflightDecision.ready()

        narrowed, result = preflight_plan(plan, checker)
        self.assertEqual(narrowed.items, items)
        self.assertEqual(narrowed.pending_items, (items[0],))
        self.assertEqual(result.deferred_items, (items[1],))
        self.assertEqual(result.failed_items, (items[2],))

    def test_confirmed_send_checkpoints_state_then_finalizes_journal(self):
        items = _items()
        strategy = _Strategy(items)
        journal = MemoryJournalStore()
        state = MemoryStateStore()
        sender = _Sender(SendResult(BatchStatus.CONFIRMED, succeeded_ids=(11, 12)))
        result = UploadEngine(sender=sender, state=state, journal=journal).run(
            strategy,
            source_root=Path("/media"),
            target={"target_mode": "channel", "channel_chat_id": -1001},
        )
        self.assertEqual(result.status, RUN_COMPLETED)
        self.assertEqual(result.batches[0].status, BatchStatus.CONFIRMED)
        self.assertEqual(sender.calls, 1)
        self.assertEqual(journal.records, {})
        self.assertEqual(
            [status for _kind, _key, status in journal.history],
            list(SEND_STATE_FLOW),
        )
        self.assertEqual(len(state.completed), 2)

    def test_preflight_failure_does_not_create_a_send_journal(self):
        items = _items()
        strategy = _Strategy(items)
        journal = MemoryJournalStore()
        sender = _Sender(SendResult(BatchStatus.CONFIRMED, succeeded_ids=(11, 12)))
        result = UploadEngine(
            sender=sender,
            journal=journal,
            preflight=lambda item: PreflightDecision.failed("not ready"),
        ).run(strategy, source_root=Path("/media"), target={})
        self.assertEqual(result.status, RUN_FAILED)
        self.assertEqual(result.batches[0].status, BatchStatus.FAILED)
        self.assertEqual(sender.calls, 0)
        self.assertEqual(journal.records, {})

    def test_deferred_items_are_partial_and_are_not_sent(self):
        items = _items()
        strategy = _Strategy(items)
        sender = _Sender(SendResult(BatchStatus.CONFIRMED, succeeded_ids=(11,)))
        result = UploadEngine(
            sender=sender,
            preflight=lambda item: PreflightDecision.deferred("offline") if item is items[1] else True,
        ).run(strategy, source_root=Path("/media"), target={})
        self.assertEqual(result.status, RUN_PARTIAL)
        self.assertEqual(result.deferred_items, (items[1],))
        self.assertEqual(strategy.contents_calls[0].pending_items, (items[0],))
        self.assertEqual(sender.calls, 1)

    def test_cancel_before_submit_leaves_no_journal(self):
        items = _items()
        journal = MemoryJournalStore()
        sender = _Sender(SendResult(BatchStatus.CONFIRMED, succeeded_ids=(11, 12)))
        result = UploadEngine(sender=sender, journal=journal).run(
            _Strategy(items),
            source_root=Path("/media"),
            target={},
            cancel_token=_Token(cancelled=True),
        )
        self.assertEqual(result.status, RUN_CANCELLED)
        self.assertTrue(result.cancelled)
        self.assertEqual(sender.calls, 0)
        self.assertEqual(journal.records, {})

    def test_cancel_after_sender_call_is_unknown_and_blocks_retry(self):
        items = _items()
        journal = MemoryJournalStore()
        sender = _Sender(raise_error=UploadCancelled("cancelled after submit"))
        result = UploadEngine(sender=sender, journal=journal).run(
            _Strategy(items), source_root=Path("/media"), target={}
        )
        self.assertEqual(result.status, RUN_UNKNOWN)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.batches[0].status, BatchStatus.UNKNOWN)
        self.assertEqual(
            journal.records[("image", "album-1", "{}")]["status"],
            BatchStatus.UNKNOWN.value,
        )

    def test_state_save_failure_keeps_confirmed_journal(self):
        class FailingState(MemoryStateStore):
            def mark_album_completed(self, items, message_ids=None):
                raise OSError("disk full")

        items = _items()
        journal = MemoryJournalStore()
        result = UploadEngine(
            sender=_Sender(SendResult(BatchStatus.CONFIRMED, succeeded_ids=(11, 12))),
            state=FailingState(),
            journal=journal,
        ).run(_Strategy(items), source_root=Path("/media"), target={})
        self.assertEqual(result.status, RUN_FAILED)
        self.assertEqual(
            journal.records[("image", "album-1", "{}")]["status"],
            BatchStatus.CONFIRMED.value,
        )


if __name__ == "__main__":
    unittest.main()
