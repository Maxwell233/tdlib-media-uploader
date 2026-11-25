from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock, Thread
from time import monotonic, sleep
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from tdlib_media_uploader.contracts import UploadContext
from tdlib_media_uploader.core.models import AlbumPlan, BatchStatus, FileSnapshot, MediaItem, ScanResult
from tdlib_media_uploader.core.upload_state import UploadState
from tdlib_media_uploader.media import image_pipeline, mixed_pipeline, video_pipeline
from tdlib_media_uploader.media.runtime import media_scope
from tdlib_media_uploader.telegram.send_result import SendResult
from tdlib_media_uploader.upload.engine import RUN_CANCELLED, UploadEngine
from tdlib_media_uploader.upload.staging import LocalStager


class Token:
    def __init__(self):
        self.immediate = Event()
        self.safe = Event()

    def is_cancelled(self):
        return self.immediate.is_set()

    def stop_after_current(self):
        return self.safe.is_set()


def make_context(root, token, *, count=4, size=6, stager=None, sender=None, **metadata):
    items = []
    for index in range(count):
        path = root / f"source-{index}.jpg"
        path.write_bytes(b"a" * size)
        items.append(MediaItem(path, root, "image", FileSnapshot(str(path), size, path.stat().st_mtime_ns)))
    plans = tuple(AlbumPlan(f"album-{index}", "image", root, "", 1, (item,), (item,), "caption", {})
                  for index, item in enumerate(items))
    return UploadContext(
        root, {}, token, SimpleNamespace(emit=lambda _: None), kind="image",
        scan_result=ScanResult(tuple(items)), plans=plans, stager=stager,
        sender=SimpleNamespace(send=sender), metadata={"prefetch_next": True, **metadata},
    )


class Strategy:
    kind = "image"

    def build_contents(self, plan, *, context, **_kwargs):
        return ({"@type": "inputMessagePhoto"},)


class PrefetchPipelineTest(TestCase):
    def test_completion_read_waits_for_state_lock(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = make_context(root, Token(), count=1)
            item = context.plans[0].pending_items[0]
            state = UploadState(kind="image", source_root=root, state_dir=root / "state")
            started = Event()
            finished = Event()
            result = []

            def read():
                started.set()
                result.append(state.is_completed(item.path))
                finished.set()

            with state.lock:
                worker = Thread(target=read, daemon=True)
                worker.start()
                self.assertTrue(started.wait(1))
                self.assertFalse(finished.wait(0.05))
            worker.join(1)
            self.assertEqual(result, [False])

    def test_safe_stop_immediately_removes_abandoned_staged_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            stage_dir = root / "stage"
            stager = LocalStager(SimpleNamespace(STAGING_MODE="always", STAGING_DIR=stage_dir))
            token = Token()
            second_prepared = Event()

            class SignallingEngine(UploadEngine):
                def _prepare_plan(self, strategy, plan, *args):
                    result = super()._prepare_plan(strategy, plan, *args)
                    if plan.key == "album-1":
                        second_prepared.set()
                    return result

            def send(contents, *, plan, **_kwargs):
                self.assertEqual(plan.key, "album-0")
                self.assertTrue(second_prepared.wait(1))
                self.assertEqual(len(tuple(stage_dir.glob("*/*.jpg"))), 2)
                token.safe.set()
                return SendResult(BatchStatus.CONFIRMED, succeeded_ids=(1,))

            context = make_context(root, token, stager=stager, sender=send, count=2)
            result = SignallingEngine().run(Strategy(), context=context)
            self.assertEqual(result.status, RUN_CANCELLED)
            self.assertEqual(tuple(stage_dir.glob("*/*.jpg")), ())
            self.assertTrue(all(item.path.is_file() for item in context.scan_result.items))

    def test_immediate_stop_does_not_wait_for_unresponsive_preparation(self):
        for metadata in ({}, {"prefetch_next": False}, {"prefetch_max_bytes": 1}):
            with self.subTest(metadata=metadata), TemporaryDirectory() as directory:
                root = Path(directory)
                entered = Event()
                release = Event()
                finished = Event()
                token = Token()
                exits = []

                class BlockedStrategy(Strategy):
                    def build_contents(self, *args, **kwargs):
                        entered.set()
                        release.wait(5)
                        return super().build_contents(*args, **kwargs)

                context = make_context(root, token, count=1, **metadata)

                def run():
                    exits.append(UploadEngine().run(BlockedStrategy(), context=context))
                    finished.set()

                worker = Thread(target=run, daemon=True)
                worker.start()
                try:
                    self.assertTrue(entered.wait(1))
                    token.immediate.set()
                    self.assertTrue(finished.wait(1))
                    self.assertEqual(exits[0].status, RUN_CANCELLED)
                finally:
                    release.set()
                    worker.join(1)

    def test_reservations_limit_slots_and_bytes_without_reordering(self):
        for slots, budget in ((2, 100), (4, 12)):
            with self.subTest(slots=slots, budget=budget), TemporaryDirectory() as directory:
                root = Path(directory)
                lock = Lock()
                staged_twice = Event()
                live = {}
                peaks = []
                delivered = []

                class TrackingStager:
                    def stage(self, plan, *, context):
                        with lock:
                            live[plan.key] = sum(item.snapshot.size for item in plan.pending_items)
                            peaks.append((len(live), sum(live.values())))
                            if len(live) >= 2:
                                staged_twice.set()
                        return plan

                    def cleanup(self, plan, *, context, confirmed):
                        with lock:
                            live.pop(plan.key)

                def send(contents, *, plan, **_kwargs):
                    self.assertTrue(staged_twice.wait(1))
                    delivered.append(plan.key)
                    return SendResult(BatchStatus.CONFIRMED, succeeded_ids=(len(delivered),))

                context = make_context(root, Token(), stager=TrackingStager(), sender=send,
                                       prefetch_slots=slots, prefetch_max_bytes=budget)
                result = UploadEngine().run(Strategy(), context=context)
                self.assertTrue(result.status == "COMPLETED", result.error)
                self.assertEqual(delivered, [plan.key for plan in context.plans])
                self.assertEqual(max(peaks), (2, 12))
                self.assertFalse(live)

    def test_all_builders_prepare_in_parallel_and_put_caption_on_first_valid_item(self):
        for mode, module, method, build in (
            ("image", image_pipeline, "input_photo", image_pipeline.build_image_contents),
            ("video", video_pipeline, "input_video", video_pipeline.build_video_contents),
            ("mixed", image_pipeline, "input_photo", mixed_pipeline.build_mixed_contents),
        ):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                root = Path(directory)
                paths = [root / f"{index}.jpg" for index in range(10)]
                items = paths if mode == "image" else [{"path": path, "media_kind": "image"} for path in paths]
                barrier = Barrier(len(paths))

                def construct(value, caption, **kwargs):
                    path = value if isinstance(value, Path) else value["path"]
                    barrier.wait(2)
                    sleep(0.02)
                    if path == paths[0]:
                        raise OSError("unreadable")
                    return {"path": path, "caption": module.formatted_text(caption)}

                cfg = SimpleNamespace(IMAGE_DIR=root, VIDEO_DIR=root, MIXED_DIR=root,
                                      IMAGE_MAX_BYTES=1024, IO_WORKERS_LOCAL=10)
                with media_scope(cfg), patch.object(module, method, construct):
                    started = monotonic()
                    contents, valid, skipped = build(items, "完整标题")
                self.assertLess(monotonic() - started, 0.7)
                self.assertEqual([entry["path"] for entry in contents], paths[1:])
                self.assertEqual(valid, items[1:])
                self.assertEqual([entry["path"] for entry in skipped], paths[:1])
                self.assertEqual([entry["caption"]["text"] for entry in contents], ["完整标题"] + [""] * 8)
