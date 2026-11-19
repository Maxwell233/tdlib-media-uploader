import json
from types import SimpleNamespace

import pytest
from PIL import Image

from conftest import task_for
from tdlib_media_uploader.config import loader
from tdlib_media_uploader.config.snapshot import snapshot_config
from tdlib_media_uploader.core.models import BatchStatus, UploadBatchResult, UploadRunResult
from tdlib_media_uploader.core.upload_journal import InflightJournal
from tdlib_media_uploader.core.upload_state import UploadState
from tdlib_media_uploader.gui.integration import run_v2_upload
from tdlib_media_uploader.gui.queue_controller import QueueController
from tdlib_media_uploader.media import image_pipeline
from tdlib_media_uploader.upload import queue_store
from tdlib_media_uploader.upload.queue import TaskStatus
from tdlib_media_uploader.upload.reconciliation import ReconciliationService


def end_first(store, task, outcome, batches=()):
    store.enqueue(task)
    task = store.start(store.prepare(task))
    return store.finish(task, UploadRunResult(outcome, batches=batches))


def checkpoint(task, paths):
    return UploadState(kind=task.kind, source_root=task.source_root, target=dict(task.target),
                       state_dir=paths.IMAGE_STATE_DIR, create=False)


def payload(item):
    return {"path": item.path, "size": item.snapshot.size, "mtime_ns": item.snapshot.mtime_ns,
            "source_root": item.source_root}


def prepare_review(store, task, paths, status="UNKNOWN"):
    reviewed = end_first(store, task, "UNKNOWN")
    journal = InflightJournal(paths.UPLOAD_INFLIGHT_DIR)
    key = task.plans[0].key
    target = dict(task.target)
    journal.prepare(task.kind, key, [payload(item) for item in task.items], target=target)
    journal.update(task.kind, key, status, target=target)
    return reviewed, journal


@pytest.fixture
def store(tmp_path, runtime_paths):
    return queue_store.UploadQueue(tmp_path / "queue.json", runtime_paths=runtime_paths)


@pytest.mark.parametrize("outcome", ["FAILED", "CANCELLED"])
def test_restore_keeps_original_manifest_id_and_position(tmp_path, runtime_paths, store, outcome):
    a = task_for(tmp_path / "a", caption="original")
    b = task_for(tmp_path / "b")
    failed = end_first(store, a, outcome)
    store.enqueue(b)
    with pytest.raises(ValueError, match="恢复或跳过"):
        store.set_paused(False)

    recovered = store.restore(a.task_id)
    assert recovered.status is TaskStatus.PENDING and store.paused
    assert recovered.plans == failed.plans
    assert recovered.config == failed.config and recovered.target == failed.target
    assert recovered.created_at == failed.created_at and recovered.task_id == failed.task_id
    assert recovered.started_at is recovered.finished_at is None
    assert store.tasks == (recovered, b)
    reopened = queue_store.UploadQueue(store.path, runtime_paths=runtime_paths)
    assert reopened.tasks == (recovered, b) and reopened.paused
    reopened.set_paused(False)
    assert reopened.next_pending() == recovered


def test_review_blocks_core_controller_and_restarted_queue(tmp_path, runtime_paths, store, pump):
    a = task_for(tmp_path / "a")
    b = task_for(tmp_path / "b")
    reviewed, journal = prepare_review(store, a, runtime_paths)
    store.enqueue(b)
    messages = []
    controller = QueueController(store, lambda _task: pytest.fail("unexpected worker"), lambda: True)
    controller.error.connect(messages.append)

    controller.set_paused(False)
    assert "待核对" in messages[-1]
    store.paused = False  # Simulate an erroneously enabled GUI or imported pause flag.
    controller.schedule()
    assert controller.worker is None and store.next_pending() is None
    with pytest.raises(ValueError, match="不能启动"):
        store.prepare(b)
    with pytest.raises(ValueError, match="未解决"):
        store.restore(reviewed.task_id)
    with pytest.raises(ValueError, match="未解决"):
        store.recheck(reviewed.task_id)
    with pytest.raises(ValueError, match="状态不支持"):
        store.skip(reviewed.task_id)
    reopened = queue_store.UploadQueue(store.path, runtime_paths=runtime_paths)
    assert reopened.tasks[0].status is TaskStatus.NEEDS_REVIEW
    with pytest.raises(ValueError, match="待核对"):
        reopened.set_paused(False)
    assert journal.list_unresolved()


@pytest.mark.parametrize("action", ["restore", "skip"])
def test_manual_reconciliation_enables_explicit_decision(tmp_path, runtime_paths, store, action):
    a = task_for(tmp_path / "a")
    b = task_for(tmp_path / "b")
    _, journal = prepare_review(store, a, runtime_paths)
    store.enqueue(b)
    record = journal.get(a.kind, a.plans[0].key, target=dict(a.target))
    ReconciliationService(journal).reconcile_inflight(
        a.plans[0].key, sent=False, kind=a.kind, target=dict(a.target),
    )
    store.refresh_reviews(record)
    assert store.tasks[0].status is TaskStatus.INTERRUPTED and store.paused
    if action == "restore":
        changed = store.restore(a.task_id)
        assert changed.status is TaskStatus.PENDING
        expected = changed
    else:
        changed = store.skip(a.task_id)
        assert changed.status is TaskStatus.SKIPPED
        expected = b
    assert store.next_pending() is None
    reopened = queue_store.UploadQueue(store.path, runtime_paths=runtime_paths)
    assert reopened.tasks == (changed, b) and reopened.paused
    reopened.set_paused(False)
    assert reopened.next_pending() == expected


def test_review_stays_blocked_until_all_overlapping_journals_are_resolved(tmp_path, runtime_paths, store):
    a = task_for(tmp_path / "a")
    _, journal = prepare_review(store, a, runtime_paths)
    target = dict(a.target)
    original_key = a.plans[0].key
    original = journal.get(a.kind, original_key, target=target)
    journal.mark_not_sent(a.kind, original_key, target=target)
    journal.prepare(a.kind, "regrouped", [payload(a.items[0])], target=target)
    store.refresh_reviews(original)
    assert store.tasks[0].status is TaskStatus.NEEDS_REVIEW
    with pytest.raises(ValueError, match="regrouped"):
        store.recheck(a.task_id)


@pytest.mark.parametrize("status", ["COMPLETED", "RUNNING", "PREPARING", "PENDING"])
def test_ineligible_tasks_cannot_restore_or_skip(tmp_path, store, status):
    task = task_for(tmp_path / "a")
    store.enqueue(task)
    if status != "PENDING":
        task = store.prepare(task)
        if status != "PREPARING":
            task = store.start(task)
            if status == "COMPLETED":
                task = store.finish(task, UploadRunResult("COMPLETED"))
    with pytest.raises(ValueError):
        store.restore(task.task_id)
    with pytest.raises(ValueError):
        store.skip(task.task_id)
    assert store.tasks == (task,)


@pytest.mark.parametrize("problem", ["source", "modified", "deleted", "checkpoint", "journal", "manifest"])
def test_restore_validation_keeps_original_state_and_queue_file(tmp_path, runtime_paths, store, problem):
    original = task_for(tmp_path / "a")
    task = end_first(store, original, "FAILED")
    if problem == "source":
        task.source_root.rename(tmp_path / "renamed")
    elif problem == "modified":
        task.items[0].path.write_bytes(b"changed file")
    elif problem == "deleted":
        task.items[0].path.unlink()
    elif problem == "checkpoint":
        checkpoint(task, runtime_paths).path.write_text("{ invalid", encoding="utf-8")
    elif problem == "journal":
        journal = InflightJournal(runtime_paths.UPLOAD_INFLIGHT_DIR)
        journal.path_for(task.kind, task.plans[0].key, dict(task.target)).write_text("{", encoding="utf-8")
    elif problem == "manifest":
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        raw["tasks"][0]["plans"][0]["key"] = "different-key"
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        broken = queue_store.UploadQueue(store.path, runtime_paths=runtime_paths)
        assert "原始任务清单已变化" in broken.error
        assert broken.next_pending() is None
        return

    original_bytes = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError, OSError)):
        store.restore(task.task_id)
    assert store.tasks == (task,) and store.path.read_bytes() == original_bytes


@pytest.mark.parametrize("after_replace", [False, True])
def test_atomic_restore_failure_keeps_previous_task_and_disk_state(tmp_path, store, monkeypatch, after_replace):
    task = end_first(store, task_for(tmp_path / "a"), "FAILED")
    previous = store.path.read_bytes()
    real_write = queue_store.atomic_write_json

    def fail(path, value):
        if after_replace:
            real_write(path, value)
        raise OSError("write failed")

    monkeypatch.setattr(queue_store, "atomic_write_json", fail)
    with pytest.raises(OSError, match="write failed"):
        store.restore(task.task_id)
    assert store.tasks == (task,) and store.path.read_bytes() == previous
    assert store.next_pending() is None


def test_lost_checkpoint_for_previously_confirmed_item_blocks_restore(tmp_path, runtime_paths, store):
    a = task_for(tmp_path / "a")
    first = a.items[0]
    result = UploadBatchResult(a.plans[0].key, BatchStatus.CONFIRMED, (17,), (first,))
    task = end_first(store, a, "PARTIAL", (result,))
    assert task.confirmed_paths
    state = checkpoint(task, runtime_paths)
    state.mark_album_completed([payload(first)], [17])
    state.path.unlink()
    with pytest.raises(ValueError, match="已确认发送的文件缺少有效断点"):
        store.restore(task.task_id)


def test_reconcile_partial_then_send_only_remaining_original_members(
    tmp_path, runtime_paths, store, monkeypatch,
):
    root = tmp_path / "a"
    root.mkdir()
    files = [root / "a.jpg", root / "b.jpg"]
    for file in files:
        Image.new("RGB", (32, 32)).save(file)
    # Use complete image preparation settings while preserving their original values.
    config = snapshot_config(loader, overrides={
        "IMAGE_ALBUM_SIZE": 2, "STAGING_MODE": "off", "IMAGE_VERIFY_ALL_IMAGES": False,
        "IMAGE_CAPTION_INCLUDE_FILENAMES": False,
    })
    original = task_for(root, paths=files, caption="fixed", config=config)
    _, journal = prepare_review(store, original, runtime_paths)
    plan = original.plans[0]
    target = dict(original.target)
    partial = journal.update(original.kind, plan.key, "UNKNOWN", target=target, succeeded_ids=[51],
        item_results=[
            {"index": 0, "status": "succeeded", "message_id": 51},
            {"index": 1, "status": "failed", "message_id": 52},
        ],
    )
    monkeypatch.setattr(image_pipeline, "STATE_DIR", runtime_paths.IMAGE_STATE_DIR)
    ReconciliationService(journal).reconcile_inflight(plan.key, sent=False, kind=original.kind, target=target)
    store.refresh_reviews(partial)
    restored = store.restore(original.task_id)
    assert len(restored.confirmed_paths) == 1 and restored.plans == original.plans
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def send_contents(self, contents, *, items, target, **kwargs):
            calls.append((tuple(item["path"] for item in items), target, contents[0]["caption"]["text"]))
            return [53]

    result = run_v2_upload(restored.kind, ui=None, source_root=restored.source_root,
        target=target, preview_result=restored.preview(), config=restored.execution_config(SimpleNamespace()),
        runtime_paths=runtime_paths, client_factory=Client)
    assert result.status == "COMPLETED", result.error
    assert calls == [((files[1],), target, "fixed")]
    updated = checkpoint(restored, runtime_paths)
    assert all(updated.is_completed(payload(item)) for item in restored.items)


def test_legacy_partial_without_file_mapping_retains_protection(tmp_path, runtime_paths, store):
    a = task_for(tmp_path / "a")
    _, journal = prepare_review(store, a, runtime_paths)
    key, target = a.plans[0].key, dict(a.target)
    journal.update(a.kind, key, "UNKNOWN", succeeded_ids=[99], target=target)
    with pytest.raises(ValueError, match="无法对应"):
        ReconciliationService(journal).reconcile_inflight(key, sent=False, kind=a.kind, target=target)
    assert journal.unresolved(a.kind, key, target) is not None
