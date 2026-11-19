import json
import threading
from dataclasses import replace
from functools import partial
from types import SimpleNamespace

import pytest

from conftest import TARGET, task_for
from tdlib_media_uploader.core.models import UploadRunResult
from tdlib_media_uploader.core.upload_journal import InflightJournal
from tdlib_media_uploader.core.upload_state import UploadState
from tdlib_media_uploader.gui.events import AuthBridge
from tdlib_media_uploader.gui.integration import run_v2_upload
from tdlib_media_uploader.gui.queue_controller import QueueController, recover_task
from tdlib_media_uploader.gui.workers import UploadWorker
from tdlib_media_uploader.upload import queue_store
from tdlib_media_uploader.upload.queue import TaskStatus, UploadTask


def test_manifest_round_trip_and_isolation(tmp_path):
    config = SimpleNamespace(
        IMAGE_EXTENSIONS={".jpg"}, IMAGE_ALBUM_SIZE=10, API_HASH="secret",
        CONFIG={"credentials": "secret"}, EXIFTOOL_PATH=tmp_path / "exiftool",
        UPLOAD_RETRY_MAX_RETRIES=3, IMAGE_CAPTION_INCLUDE_FILENAMES=True,
    )
    original = task_for(tmp_path / "photos", config=config, caption="my edit")
    encoded = json.dumps(original.to_record())
    assert "secret" not in encoded
    restored = UploadTask.from_record(json.loads(encoded))
    config.IMAGE_EXTENSIONS.add(".png")
    config.IMAGE_ALBUM_SIZE = 1
    assert restored.plans == original.plans
    assert restored.plans[0].caption == "my edit\n1. test"
    assert restored.config["IMAGE_EXTENSIONS"] == {".jpg"}
    assert restored.config["IMAGE_ALBUM_SIZE"] == 10
    assert restored.config["UPLOAD_RETRY_MAX_RETRIES"] == 3
    with pytest.raises(TypeError):
        restored.target["chat_id"] = 1
    with pytest.raises(TypeError):
        restored.config["IMAGE_EXTENSIONS"] = {}


def test_order_and_duplicate_check_with_different_targets(tmp_path):
    store = queue_store.UploadQueue(tmp_path / "queue.json", recover_task)
    a = task_for(tmp_path / "a")
    b = task_for(tmp_path / "b")
    duplicate = replace(a, task_id=b.task_id)
    other_target = task_for(a.source_root, paths=[a.items[0].path], target={**TARGET, "chat_id": -100555})
    for task in (a, b, other_target):
        store.enqueue(task)
    with pytest.raises(ValueError, match="重叠"):
        store.enqueue(duplicate)
    assert store.next_pending() == a
    store.move(other_target.task_id, -1)
    store.remove(b.task_id)
    store.set_paused(True)
    assert [task.task_id for task in store.tasks] == [a.task_id, other_target.task_id]
    assert store.next_pending() is None
    store.set_paused(False)
    running = store.start(store.prepare(a))
    assert store.next_pending() is None
    store.finish(running, UploadRunResult("COMPLETED"))
    assert store.next_pending() == other_target


@pytest.mark.parametrize("phase", ["PREPARING", "RUNNING"])
@pytest.mark.parametrize("protected", [True, False])
def test_restart_never_requeues_running_task(tmp_path, runtime_paths, phase, protected):
    path = tmp_path / "queue.json"
    recover = partial(recover_task, runtime_paths=runtime_paths)
    store = queue_store.UploadQueue(path, recover)
    a = task_for(tmp_path / "a")
    b = task_for(tmp_path / "b")
    for task in (a, b):
        store.enqueue(task)
    prep = store.prepare(a)
    if phase == "RUNNING":
        store.start(prep)
    if protected:
        journal = InflightJournal(runtime_paths.UPLOAD_INFLIGHT_DIR)
        journal.prepare(a.kind, a.plans[0].key, target=dict(a.target))
    reopened = queue_store.UploadQueue(path, recover)
    assert reopened.paused
    assert reopened.tasks[0].status == (TaskStatus.NEEDS_REVIEW if protected else TaskStatus.INTERRUPTED)
    assert json.loads(path.read_text())["paused"] is True
    reopened.set_paused(False)
    assert reopened.next_pending() == b


def test_queue_corruption_and_atomic_failure(tmp_path, monkeypatch):
    path = tmp_path / "queue.json"
    task = task_for(tmp_path / "a")
    store = queue_store.UploadQueue(path, recover_task)
    store.enqueue(task)
    original = path.read_bytes()

    def unwritable(*args):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(queue_store, "atomic_write_json", unwritable)
        with pytest.raises(OSError):
            store.remove(task.task_id)
        assert store.tasks == (task,)
        assert path.read_bytes() == original
    path.write_text("{bad")
    broken = queue_store.UploadQueue(path, recover_task)
    assert broken.error
    with pytest.raises(RuntimeError):
        broken.enqueue(task)
    assert path.read_text() == "{bad"


@pytest.mark.parametrize("outcome, paused, state", [
    ("COMPLETED", False, TaskStatus.COMPLETED),
    ("FAILED", True, TaskStatus.FAILED),
    ("PARTIAL", True, TaskStatus.FAILED),
    ("UNKNOWN", True, TaskStatus.NEEDS_REVIEW),
    ("CANCELLED", True, TaskStatus.INTERRUPTED),
])
def test_controller_runs_fifo_only_after_worker_cleanup(tmp_path, pump, outcome, paused, state):
    tasks = tuple(task_for(tmp_path / name, caption=name) for name in ("a", "b", "c"))
    starts, outcomes = [], []
    a_started, release_a = threading.Event(), threading.Event()
    store = queue_store.UploadQueue(tmp_path / "queue.json", recover_task)

    def worker_for(task):
        def run(_kind, *, on_ready, **kwargs):
            halted = on_ready()
            if halted:
                return halted
            starts.append(task.task_id)
            if task is tasks[0] or task.task_id == tasks[0].task_id:
                a_started.set()
                assert release_a.wait(5)
                return UploadRunResult(outcome, cancelled=outcome == "CANCELLED")
            return UploadRunResult("COMPLETED")
        worker = UploadWorker(task.kind, AuthBridge(), upload_runner=run)
        worker.wait_for_scheduler = True
        return worker

    control = QueueController(store, worker_for, lambda: True)
    control.ended.connect(outcomes.append)
    assert control.enqueue(tasks[0])
    pump(a_started.is_set)
    assert store.tasks[0].status == TaskStatus.RUNNING
    assert control.enqueue(tasks[1])
    assert control.enqueue(tasks[2])
    assert starts == [tasks[0].task_id]
    release_a.set()
    pump(lambda: len(outcomes) == (1 if paused else 3))
    assert outcomes[0].status == state
    assert store.paused is paused
    if paused:
        assert starts == [tasks[0].task_id]
        control.set_paused(False)
        pump(lambda: len(outcomes) == 3)
    assert starts == [task.task_id for task in tasks]


def test_pause_does_not_stop_active_worker_and_finish_write_failure_stops_queue(tmp_path, pump, monkeypatch):
    store = queue_store.UploadQueue(tmp_path / "queue.json", recover_task)
    a, b = (task_for(tmp_path / name) for name in ("a", "b"))
    started, release = threading.Event(), threading.Event()
    errors = []

    def worker_for(task):
        def run(kind, *, on_ready, cancel_event, **kwargs):
            on_ready()
            started.set()
            assert release.wait(5)
            assert not cancel_event.is_set()
            return UploadRunResult("COMPLETED")
        worker = UploadWorker(task.kind, AuthBridge(), upload_runner=run)
        worker.wait_for_scheduler = True
        return worker

    controller = QueueController(store, worker_for, lambda: True)
    controller.error.connect(errors.append)
    controller.enqueue(a)
    controller.enqueue(b)
    pump(started.is_set)
    controller.set_paused(True)
    assert not controller.worker.ui.safe_stop_requested

    def disk_failure(*args):
        raise OSError("disk full")

    monkeypatch.setattr(queue_store, "atomic_write_json", disk_failure)
    release.set()
    pump(lambda: controller.write_blocked)
    assert errors and "disk full" in errors[-1]
    assert controller.worker is None and store.tasks[1].status == TaskStatus.PENDING


@pytest.mark.parametrize("change", ["deleted", "modified", "lost_checkpoint"])
def test_revalidate_complete_manifest_before_opening_client(tmp_path, runtime_paths, change):
    a = task_for(tmp_path / "photos")
    item = a.items[0]
    if change == "deleted":
        item.path.unlink()
    elif change == "modified":
        item.path.write_bytes(b"modified file")
    else:
        a = replace(a, plans=(replace(a.plans[0], pending_items=()),))

    def no_client(*args, **kwargs):
        pytest.fail("client opened with invalid manifest")

    result = run_v2_upload(a.kind, ui=None, source_root=a.source_root, target=dict(a.target),
                           preview_result=a.preview(), config=a.execution_config(SimpleNamespace()),
                           runtime_paths=runtime_paths, client_factory=no_client)
    assert result.status == "FAILED"


def test_execution_rechecks_checkpoint_written_after_scan(tmp_path, runtime_paths):
    a = task_for(tmp_path / "photos")
    item = a.items[0]
    state = UploadState(kind=a.kind, source_root=a.source_root,
                        state_dir=runtime_paths.IMAGE_STATE_DIR, target=dict(a.target))
    state.mark_album_completed([{"path": item.path, "size": item.snapshot.size,
                                 "mtime_ns": item.snapshot.mtime_ns}], [99])

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def send_contents(self, *args, **kwargs):
            pytest.fail("re-sent a file confirmed after scanning")

    result = run_v2_upload(a.kind, ui=None, source_root=a.source_root, target=dict(a.target),
                           preview_result=a.preview(), config=a.execution_config(SimpleNamespace()),
                           runtime_paths=runtime_paths, client_factory=Client)
    assert result.status == "COMPLETED"
