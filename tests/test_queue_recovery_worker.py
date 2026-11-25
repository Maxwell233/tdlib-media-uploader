"""Worker, scheduler and transaction boundaries with deterministic I/O delays."""

import json
import threading
from contextlib import contextmanager

import pytest
from PySide6.QtCore import QTimer

from conftest import media, task_for
from tdlib_media_uploader.core import persistence
from tdlib_media_uploader.core.models import AlbumPlan, BatchStatus, UploadBatchResult, UploadRunResult
from tdlib_media_uploader.core.upload_journal import InflightJournal
from tdlib_media_uploader.gui.queue_controller import QueueController
from tdlib_media_uploader.upload import queue_recovery, queue_store
from tdlib_media_uploader.upload.queue import TaskStatus, UploadTask

from test_queue_recovery import checkpoint, end_first, payload


@pytest.fixture
def queued(tmp_path, runtime_paths):
    store = queue_store.UploadQueue(tmp_path / "upload_queue.json", runtime_paths=runtime_paths)
    task = task_for(tmp_path / "source", caption="原始标题")
    first = end_first(store, task, "FAILED")
    later = task_for(tmp_path / "later")
    store.enqueue(later)
    return store, first, later


@contextmanager
def delayed_operation(pump, controller):
    entered, release = threading.Event(), threading.Event()
    threads = []

    def block():
        threads.append(threading.get_ident())
        entered.set()
        if not release.wait(10):
            raise RuntimeError("test did not release the blocked operation")

    try:
        yield entered, release, threads, block
    finally:
        release.set()
        pump(lambda: not controller.recovery_busy)


def make_controller(store):
    def unexpected_worker(_task):
        pytest.fail("upload started while recovery was in progress")

    return QueueController(store, unexpected_worker, lambda: True)


def check_qt_event(pump):
    called = []
    QTimer.singleShot(0, lambda: called.append(True))
    pump(lambda: bool(called))


def test_large_manifest_and_slow_nas_validation_allow_gui_events(tmp_path, runtime_paths, pump, monkeypatch):
    root = tmp_path / "large"
    root.mkdir()
    items = []
    for i in range(240):
        file = root / f"{i}.jpg"
        file.write_bytes(b"photo")
        items.append(media(file, root))
    target = {"target_mode": "channel", "chat_id": -100123}
    plans = tuple(
        AlbumPlan(f"album-{i}", "image", root, "", i, tuple(items[i:i+10]), tuple(items[i:i+10]), "fixed", target)
        for i in range(0, len(items), 10)
    )
    task = UploadTask.from_preview("image", {
        "source_dir": str(root), "target": target, "plans": plans, "groups": (),
    }, object())
    store = queue_store.UploadQueue(tmp_path / "upload_queue.json", runtime_paths=runtime_paths)
    first = end_first(store, task, "FAILED")
    later = task_for(tmp_path / "later")
    store.enqueue(later)
    controller = make_controller(store)
    log, results = [], []
    controller.recovery_progress.connect(lambda message, _cancelable: log.append(message))
    controller.recovery_finished.connect(results.append)
    initial = store.path.read_bytes()
    caller = threading.get_ident()
    original = queue_recovery.validate_snapshots
    with delayed_operation(pump, controller) as (entered, _, threads, block):
        def slow(snapshots):
            snapshots = iter(snapshots)
            original((next(snapshots),))
            block()
            original(snapshots)

        monkeypatch.setattr(queue_recovery, "validate_snapshots", slow)
        assert controller.restore(first.task_id)
        pump(entered.is_set)
        check_qt_event(pump)
        assert "正在检查源文件 0/240" in log
        assert len(threads) == 1 and threads[0] != caller
        assert controller.recovery_busy and store.tasks == (first, later) and not results
        assert store.path.read_bytes() == initial
        assert not controller.restore(first.task_id) and not controller.skip(first.task_id)
        assert not controller.recheck(first.task_id)
        controller.set_paused(False)
        controller.schedule()
        with pytest.raises(RuntimeError, match="正在处理队列事务"):
            store.remove(later.task_id)
        with pytest.raises(RuntimeError, match="正在处理队列事务"):
            store.prepare(later)
    assert results[0].error is None and results[0].task is store.tasks[0]
    assert store.tasks[0].task_id == first.task_id and store.tasks[0].plans == first.plans
    assert store.tasks[1] == later and store.paused and not controller.worker
    assert "正在检查源文件 240/240" in log


def test_revision_captured_before_worker_start_rejects_a_newer_queue(queued, tmp_path, pump, monkeypatch):
    store, first, later = queued
    controller = make_controller(store)
    original = store.recover
    results = []
    controller.recovery_finished.connect(results.append)
    with delayed_operation(pump, controller) as (entered, _, _, block):
        def wait_before_lock(*args, **kwargs):
            block()
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "recover", wait_before_lock)
        assert controller.restore(first.task_id)
        pump(entered.is_set)
        third = task_for(tmp_path / "new")
        version = store.revision
        store.enqueue(third)
        assert store.revision == version + 1
        newest = store.path.read_bytes()
    assert "队列状态已变化" in results[0].error
    assert store.tasks == (first, later, third) and store.path.read_bytes() == newest


def test_slow_queue_write_keeps_events_responsive_until_persisted(queued, pump, monkeypatch):
    store, first, later = queued
    controller = make_controller(store)
    real_write = queue_store.atomic_write_json
    log, results = [], []
    controller.recovery_progress.connect(lambda *values: log.append(values))
    controller.recovery_finished.connect(results.append)
    with delayed_operation(pump, controller) as (entered, _, threads, block):
        def slow_write(path, value, **options):
            assert options.get("before_replace")
            block()
            real_write(path, value, **options)

        monkeypatch.setattr(queue_store, "atomic_write_json", slow_write)
        assert controller.restore(first.task_id)
        pump(entered.is_set)
        check_qt_event(pump)
        assert threads[0] != threading.get_ident()
        assert ("正在保存队列状态…", False) in log
        assert store.tasks == (first, later) and not results
        assert not controller.enqueue(later)
        controller.cancel_recovery()
    assert not results[0].cancelled and results[0].error is None
    assert store.tasks[0].status is TaskStatus.PENDING and store.paused


@pytest.mark.parametrize("after_replace", [False, True])
def test_review_does_not_publish_after_journal_changes(queued, runtime_paths, pump, monkeypatch, after_replace):
    store, first, _ = queued
    store.set_paused(True)
    # A second queue provides a genuine NEEDS_REVIEW transition.
    review_store = queue_store.UploadQueue(store.path.with_name("reviews.json"), runtime_paths=runtime_paths)
    review = end_first(review_store, task_for(first.source_root, paths=[first.items[0].path]), "UNKNOWN")
    plan = review.plans[0]
    target = dict(review.target)
    record = {"kind": review.kind, "album_key": plan.key, "target": target}
    journal = InflightJournal(runtime_paths.UPLOAD_INFLIGHT_DIR)
    previous = review_store.path.read_bytes()
    controller = make_controller(review_store)
    results = []
    controller.recovery_finished.connect(results.append)
    original = queue_store.atomic_write_json
    with delayed_operation(pump, controller) as (entered, _, _, block):
        def slow_write(path, value, **options):
            if after_replace:
                original(path, value, **options)
            block()
            if not after_replace:
                original(path, value, **options)

        monkeypatch.setattr(queue_store, "atomic_write_json", slow_write)
        assert controller.refresh_reviews(record)
        pump(entered.is_set)
        journal.prepare(review.kind, plan.key, [payload(review.items[0])], target=target)
    assert "仍有未解决" in results[0].error
    assert review_store.tasks == (review,) and review_store.path.read_bytes() == previous
    assert journal.unresolved(review.kind, plan.key, target) is not None


def test_external_queue_edit_is_not_overwritten(queued, pump, monkeypatch):
    store, first, later = queued
    controller = make_controller(store)
    results = []
    controller.recovery_finished.connect(results.append)
    original = queue_store.atomic_write_json
    with delayed_operation(pump, controller) as (entered, _, _, block):
        def slow_write(path, value, **options):
            block()
            original(path, value, **options)

        monkeypatch.setattr(queue_store, "atomic_write_json", slow_write)
        assert controller.restore(first.task_id)
        pump(entered.is_set)
        new_record = json.loads(store.path.read_bytes())
        new_record["tasks"].pop()
        external = json.dumps(new_record).encode("utf-8")
        store.path.write_bytes(external)
    assert "磁盘上的队列文件已变化" in results[0].error
    assert store.error and store.next_pending() is None
    with pytest.raises(RuntimeError, match="磁盘上的队列文件已变化"):
        store.remove(later.task_id)
    assert store.tasks == (first, later) and store.path.read_bytes() == external


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_real_atomic_write_failure_keeps_old_queue(queued, pump, monkeypatch, failure):
    store, first, later = queued
    controller = make_controller(store)
    results = []
    controller.recovery_finished.connect(results.append)
    before = store.path.read_bytes()
    if failure == "replace":
        real_replace = persistence.os.replace

        def bad_replace(source, destination):
            if destination == store.path:
                raise OSError("simulated replace")
            return real_replace(source, destination)

        monkeypatch.setattr(persistence.os, "replace", bad_replace)
    else:
        real_sync = persistence.fsync_directory
        failed = False

        def bad_sync(path):
            nonlocal failed
            if path == store.path.parent and store.path.read_bytes() != before and not failed:
                failed = True
                raise OSError("simulated fsync")
            return real_sync(path)

        monkeypatch.setattr(persistence, "fsync_directory", bad_sync)
    assert controller.restore(first.task_id)
    pump(lambda: not controller.recovery_busy)
    assert failure in results[0].error
    assert store.tasks == (first, later) and store.path.read_bytes() == before


def test_rollback_failure_blocks_later_writes(queued, pump, monkeypatch):
    store, first, later = queued
    controller = make_controller(store)
    results = []
    controller.recovery_finished.connect(results.append)
    before = store.path.read_bytes()

    def fail_sync(path):
        if path == store.path.parent and store.path.read_bytes() != before:
            raise OSError("simulated fsync failure after replace")

    def fail_rollback(_path, _value):
        raise PermissionError("simulated rollback failure")

    monkeypatch.setattr(persistence, "fsync_directory", fail_sync)
    monkeypatch.setattr(queue_store, "atomic_write_text", fail_rollback)
    assert controller.restore(first.task_id)
    pump(lambda: not controller.recovery_busy)
    assert "simulated fsync failure" in results[0].error
    assert results[0].task is None and store.tasks == (first, later)
    assert store.error and "simulated rollback failure" in store.error and store.paused
    assert store.next_pending() is None
    with pytest.raises(RuntimeError, match="无法确认队列文件已恢复"):
        store.remove(later.task_id)
    assert json.loads(store.path.read_bytes())["tasks"][0]["status"] == "PENDING"


def test_cancel_during_file_validation_never_commits(queued, pump, monkeypatch):
    store, first, later = queued
    controller = make_controller(store)
    results = []
    controller.recovery_finished.connect(results.append)
    before = store.path.read_bytes()
    original = queue_recovery.validate_snapshots
    with delayed_operation(pump, controller) as (entered, _, _, block):
        def slow(snapshots):
            block()
            original(snapshots)

        monkeypatch.setattr(queue_recovery, "validate_snapshots", slow)
        assert controller.restore(first.task_id)
        pump(entered.is_set)
        controller.cancel_recovery()
    assert results[0].cancelled and not results[0].error
    assert store.tasks == (first, later) and store.path.read_bytes() == before


def test_async_skip_checks_lost_checkpoint_with_a_closed_journal(tmp_path, runtime_paths, pump):
    source = task_for(tmp_path / "source")
    item = source.items[0]
    batch = UploadBatchResult(source.plans[0].key, BatchStatus.CONFIRMED, (123,), (item,))
    state = checkpoint(source, runtime_paths)
    state.mark_album_completed([payload(item)], [123])
    store = queue_store.UploadQueue(tmp_path / "upload_queue.json", runtime_paths=runtime_paths)
    first = end_first(store, source, "PARTIAL", (batch,))
    state.path.unlink()
    journal = InflightJournal(runtime_paths.UPLOAD_INFLIGHT_DIR)
    assert not journal.list_unresolved()
    controller = make_controller(store)
    results, history = [], []
    controller.recovery_finished.connect(results.append)
    controller.skipped.connect(history.append)
    before = store.path.read_bytes()
    assert controller.skip(first.task_id)
    pump(lambda: not controller.recovery_busy)
    assert "缺少有效断点" in results[0].error
    assert not history and not state.path.exists()
    assert store.tasks == (first,) and store.path.read_bytes() == before


def test_late_event_from_old_worker_cannot_finish_new_operation(queued, pump, monkeypatch):
    store, first, later = queued
    controller = make_controller(store)
    results, progress = [], []
    controller.recovery_finished.connect(results.append)
    controller.recovery_progress.connect(lambda message, _: progress.append(message))
    assert controller.restore(first.task_id)
    old_worker = controller.recovery_worker
    pump(lambda: not controller.recovery_busy)

    # A new failure may use the same task ID, so callbacks must be tied to
    # the Worker instance rather than merely matching the selected row.
    store.set_paused(False)
    current = store.start(store.prepare(store.tasks[0]))
    first = store.finish(current, UploadRunResult("FAILED"))
    original = queue_recovery.validate_snapshots
    with delayed_operation(pump, controller) as (entered, _, _, block):
        def slow(snapshots):
            block()
            original(snapshots)

        monkeypatch.setattr(queue_recovery, "validate_snapshots", slow)
        assert controller.restore(first.task_id)
        pump(entered.is_set)
        new_worker = controller.recovery_worker
        controller._recovery_finished(old_worker)
        controller._recovery_progress(old_worker, "stale", False)
        assert controller.recovery_worker is new_worker
        assert len(results) == 1 and "stale" not in progress
        assert store.tasks == (first, later)
    assert len(results) == 2 and results[-1].error is None
