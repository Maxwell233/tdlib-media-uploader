from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader import (  # noqa: E402
    AlbumPlan,
    BatchStatus,
    FileSnapshot,
    LogEvent,
    MediaItem,
    ProgressEvent,
    ScanResult,
)
from tdlib_media_uploader.telegram.send_result import SendResult  # noqa: E402
from tdlib_media_uploader.upload.engine import (  # noqa: E402
    MemoryJournalStore,
    MemoryStateStore,
    RUN_CANCELLED,
    RUN_COMPLETED,
    UploadCancelled,
    UploadContext,
    UploadEngine,
    _PrefetchEventSink,
)


class _Token:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.safe_event = threading.Event()

    def is_cancelled(self):
        return self.cancel_event.is_set()

    def raise_if_cancelled(self):
        if self.is_cancelled():
            raise UploadCancelled("中断")

    def stop_after_current(self):
        return self.safe_event.is_set()


class _PipelinedStrategy:
    kind = "video"

    def __init__(self, count: int = 3):
        root = Path("/media")
        self.items = tuple(
            MediaItem(
                path=root / f"{index}.mp4",
                source_root=root,
                media_kind="video",
                snapshot=FileSnapshot(str(root / f"{index}.mp4"), 1000 * index, 2000 * index),
            )
            for index in range(1, count + 1)
        )
        self.contents_calls: list[str] = []
        self.prep_events: dict[str, threading.Event] = {
            f"album-{index}": threading.Event() for index in range(1, count + 1)
        }

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
                caption=f"caption-{index}",
                target=target,
            )
            for index in range(1, len(self.items) + 1)
        )

    def build_contents(self, plan, *, cancel_token, event_sink, context=None):
        self.contents_calls.append(plan.key)
        if plan.key in self.prep_events:
            self.prep_events[plan.key].set()
        return ({"@type": "inputMessageVideo", "path": str(plan.pending_items[0].path)},)


class _BlockingSender:
    def __init__(self, on_send_callback=None):
        self.on_send_callback = on_send_callback
        self.calls: list[str] = []

    def send_contents(self, contents, target=None, plan=None, context=None):
        key = getattr(plan, "key", "unknown")
        self.calls.append(key)
        if self.on_send_callback is not None:
            self.on_send_callback(key)
        return SendResult(BatchStatus.CONFIRMED, succeeded_ids=(len(self.calls),))


class PrefetchPipelineTest(unittest.TestCase):
    def test_pipeline_concurrent_prep_during_send(self):
        strategy = _PipelinedStrategy(count=3)
        album2_started = threading.Event()

        def on_send(plan_key: str):
            if plan_key == "album-1":
                # While album-1 is sending, wait to observe album-2 being prepared in background
                started = strategy.prep_events["album-2"].wait(timeout=2.0)
                if started:
                    album2_started.set()

        sender = _BlockingSender(on_send_callback=on_send)
        state = MemoryStateStore()
        journal = MemoryJournalStore()

        engine = UploadEngine(sender=sender, state=state, journal=journal, prefetch_next=True)
        result = engine.run(strategy, source_root=Path("/media"), target={})

        self.assertEqual(result.status, RUN_COMPLETED)
        self.assertTrue(album2_started.is_set(), "Album 2 preparation did not run concurrently during Album 1 send")
        self.assertEqual(len(result.batches), 3)
        self.assertEqual(sender.calls, ["album-1", "album-2", "album-3"])

    def test_bounded_lookahead_strictly_at_most_one(self):
        strategy = _PipelinedStrategy(count=4)
        album3_called_during_send1 = False

        def on_send(plan_key: str):
            nonlocal album3_called_during_send1
            if plan_key == "album-1":
                # Wait until album-2 preparation is active/done
                strategy.prep_events["album-2"].wait(timeout=2.0)
                time.sleep(0.05)
                # Verify album-3 has NOT been prepared yet (strictly bounded to 1 ahead)
                if "album-3" in strategy.contents_calls:
                    album3_called_during_send1 = True

        sender = _BlockingSender(on_send_callback=on_send)
        engine = UploadEngine(sender=sender, prefetch_next=True)
        result = engine.run(strategy, source_root=Path("/media"), target={})

        self.assertEqual(result.status, RUN_COMPLETED)
        self.assertFalse(album3_called_during_send1, "Album 3 was prepared too early; lookahead must be bounded to 1")

    def test_safe_stop_with_prefetch_does_not_send_or_journal_next(self):
        strategy = _PipelinedStrategy(count=3)
        token = _Token()

        def on_send(plan_key: str):
            if plan_key == "album-1":
                # Wait until album-2 is being prepared
                strategy.prep_events["album-2"].wait(timeout=2.0)
                # Trigger safe stop
                token.safe_event.set()

        sender = _BlockingSender(on_send_callback=on_send)
        state = MemoryStateStore()
        journal = MemoryJournalStore()

        engine = UploadEngine(sender=sender, state=state, journal=journal, prefetch_next=True)
        result = engine.run(strategy, source_root=Path("/media"), target={}, cancel_token=token)

        self.assertEqual(result.status, RUN_CANCELLED)
        self.assertTrue(result.cancelled)
        self.assertEqual(sender.calls, ["album-1"])
        self.assertEqual(len(result.batches), 1)
        self.assertEqual(result.batches[0].album_key, "album-1")
        self.assertEqual(result.batches[0].status, BatchStatus.CONFIRMED)
        # Journal must have no leftover records
        self.assertEqual(journal.records, {})
        # State must only have album-1 items completed
        self.assertEqual(len(state.completed), 1)

    def test_safe_stop_cancels_running_prefetch_and_cleans_staging(self):
        token = _Token()
        prefetch_cancelled = threading.Event()
        prefetch_fallback = threading.Event()

        class BlockingStrategy(_PipelinedStrategy):
            def build_contents(self, plan, *, cancel_token, event_sink, context=None):
                if plan.key != "album-2":
                    return super().build_contents(
                        plan,
                        cancel_token=cancel_token,
                        event_sink=event_sink,
                        context=context,
                    )

                self.prep_events[plan.key].set()
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if cancel_token.is_cancelled():
                        prefetch_cancelled.set()
                        raise UploadCancelled("预取收到安全停止")
                    time.sleep(0.005)

                # Bound the test even if cancellation stops being propagated.
                prefetch_fallback.set()
                return super().build_contents(
                    plan,
                    cancel_token=cancel_token,
                    event_sink=event_sink,
                    context=context,
                )

        strategy = BlockingStrategy(count=3)
        staged: list[str] = []
        cleaned: list[tuple[str, bool]] = []

        class MockStager:
            def stage(self, plan, context):
                staged.append(plan.key)
                return plan

            def cleanup(self, plan, context, *, confirmed=False):
                cleaned.append((plan.key, confirmed))

        def on_send(plan_key: str):
            if plan_key == "album-1":
                self.assertTrue(strategy.prep_events["album-2"].wait(timeout=2.0))
                token.safe_event.set()

        sender = _BlockingSender(on_send_callback=on_send)
        state = MemoryStateStore()
        journal = MemoryJournalStore()
        engine = UploadEngine(
            sender=sender,
            state=state,
            journal=journal,
            stager=MockStager(),
            prefetch_next=True,
        )

        result = engine.run(strategy, source_root=Path("/media"), target={}, cancel_token=token)

        self.assertEqual(result.status, RUN_CANCELLED)
        self.assertTrue(prefetch_cancelled.is_set(), "running prefetch did not observe safe stop")
        self.assertFalse(prefetch_fallback.is_set(), "safe stop waited for prefetch's fallback timeout")
        self.assertEqual(sender.calls, ["album-1"])
        self.assertEqual(journal.records, {})
        self.assertEqual(len(state.completed), 1)
        self.assertIn("album-2", staged)
        self.assertIn(("album-2", False), cleaned)

    def test_prefetch_context_filters_strategy_progress_but_forwards_logs(self):
        upload_active = threading.Event()
        upload_started = threading.Event()
        preflight_done = threading.Event()
        leaked_progress: list[ProgressEvent] = []
        forwarded_logs: list[LogEvent] = []
        lock = threading.Lock()

        class RecordingSink:
            def emit(self, event):
                with lock:
                    if upload_active.is_set() and isinstance(event, ProgressEvent):
                        leaked_progress.append(event)
                    if upload_active.is_set() and isinstance(event, LogEvent):
                        forwarded_logs.append(event)

        class ContextAwareStrategy(_PipelinedStrategy):
            def preflight_item(self, item, *, context=None):
                if item.path.name == "2.mp4":
                    if not upload_started.wait(timeout=2.0):
                        raise AssertionError("album 1 send did not start before prefetch callback")
                    # This mirrors VideoStrategy's legacy callback path through
                    # context.event_sink, which bypassed the explicit sink.
                    context.event_sink.emit(
                        ProgressEvent(
                            kind="video",
                            phase="upload",
                            completed=1,
                            total=1,
                            path=str(item.path),
                        )
                    )
                    context.event_sink.emit(LogEvent(level="WARNING", message="prefetch warning"))
                    preflight_done.set()
                return {"status": "READY"}

        strategy = ContextAwareStrategy(count=2)
        sink = RecordingSink()

        def on_send(plan_key: str):
            if plan_key == "album-1":
                upload_active.set()
                upload_started.set()
                self.assertTrue(preflight_done.wait(timeout=2.0))
                upload_active.clear()

        sender = _BlockingSender(on_send_callback=on_send)
        context = UploadContext(
            source_root=Path("/media"),
            target={},
            cancel_token=_Token(),
            event_sink=sink,
            preflight=strategy.preflight_item,
        )
        engine = UploadEngine(sender=sender, prefetch_next=True)

        result = engine.run(strategy, context=context)

        self.assertEqual(result.status, RUN_COMPLETED)
        self.assertEqual(sender.calls, ["album-1", "album-2"])
        self.assertEqual(leaked_progress, [])
        self.assertTrue(any(log.message == "prefetch warning" for log in forwarded_logs))

    def test_staging_cleanup_on_safe_stop(self):
        strategy = _PipelinedStrategy(count=3)
        token = _Token()

        staged_albums: list[str] = []
        cleaned_albums: list[tuple[str, bool]] = []

        class MockStager:
            def stage(self, plan, context):
                staged_albums.append(plan.key)
                return plan

            def cleanup(self, plan, context, *, confirmed=False):
                cleaned_albums.append((plan.key, confirmed))

        def on_send(plan_key: str):
            if plan_key == "album-1":
                strategy.prep_events["album-2"].wait(timeout=2.0)
                token.safe_event.set()

        sender = _BlockingSender(on_send_callback=on_send)
        stager = MockStager()

        engine = UploadEngine(sender=sender, stager=stager, prefetch_next=True)
        result = engine.run(strategy, source_root=Path("/media"), target={}, cancel_token=token)

        self.assertEqual(result.status, RUN_CANCELLED)
        # Album-1 was confirmed and cleaned up with confirmed=True
        self.assertIn(("album-1", True), cleaned_albums)
        # Album-2 was prefetched and staged, but cleaned up with confirmed=False
        self.assertIn(("album-2", False), cleaned_albums)

    def test_prefetch_event_sink_filters_progress_events(self):
        events: list[object] = []

        class MockSink:
            def emit(self, event):
                events.append(event)

        base_sink = MockSink()
        prefetch_sink = _PrefetchEventSink(base_sink)

        # ProgressEvent must be suppressed
        prefetch_sink.emit(ProgressEvent(kind="video", phase="preflight", completed=1, total=1))
        self.assertEqual(len(events), 0)

        # LogEvent must pass through
        log_event = LogEvent(level="INFO", message="scan warning")
        prefetch_sink.emit(log_event)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], log_event)


if __name__ == "__main__":
    unittest.main()
