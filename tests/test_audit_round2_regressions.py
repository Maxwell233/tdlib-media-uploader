"""Round 2 PR #50 audit regression tests covering P1/P2 findings."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import Qt

from conftest import TARGET, task_for
from tdlib_media_uploader.config import loader
from tdlib_media_uploader.config.snapshot import snapshot_config
from tdlib_media_uploader.core.concurrency import CancellationToken
from tdlib_media_uploader.core.filesystem import file_snapshot, stable_path
from tdlib_media_uploader.core.identity import validate_send_target
from tdlib_media_uploader.core.models import (
    AlbumPlan,
    BatchStatus,
    ContentBuildResult,
    FileSnapshot,
    MediaItem,
    UploadBatchResult,
    UploadRunResult,
)
from tdlib_media_uploader.core.upload_journal import InflightJournal
from tdlib_media_uploader.core.upload_state import UploadState
from tdlib_media_uploader.contracts import ScanResult, UploadContext
from tdlib_media_uploader.gui import main_window
from tdlib_media_uploader.gui.queue_controller import QueueController
from tdlib_media_uploader.gui.pages.task import TaskPage
from tdlib_media_uploader.gui.pages.upload import UploadPage, UploadPageServices
from tdlib_media_uploader.media import image_pipeline
from tdlib_media_uploader.telegram.send_result import SendRequestRejected
from tdlib_media_uploader.upload import preflight, reconciliation
from tdlib_media_uploader.upload.engine import UploadEngine
from tdlib_media_uploader.upload.preflight import PreflightDecision, PreflightStatus
from tdlib_media_uploader.upload.queue import AlbumOutcome, AlbumStatus, TaskStatus, UploadTask, finish_task
from tdlib_media_uploader.upload.queue_store import UploadQueue
from tdlib_media_uploader.upload.reconciliation import ReconciledAlbum, ReconciliationService
from tdlib_media_uploader.upload.retry import AlbumRetryPolicy, QueueSendPolicy


def _seed(path: Path, payload: bytes = b"media") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _item(path: Path, root: Path, kind: str = "image", group_name: str = "26-11") -> MediaItem:
    size, mtime_ns = file_snapshot(path)
    return MediaItem(
        path=path,
        source_root=root,
        media_kind=kind,
        snapshot=FileSnapshot(stable_path(path), size, mtime_ns),
        group_name=group_name,
    )


def _plan(
    key: str,
    root: Path,
    items: tuple[MediaItem, ...],
    *,
    number: int = 1,
    kind: str = "image",
    caption: str | None = None,
    group_label: str = "26-11",
) -> AlbumPlan:
    return AlbumPlan(
        key=key,
        kind=kind,
        source_root=root,
        group_label=group_label,
        number=number,
        items=items,
        pending_items=items,
        caption=caption if caption is not None else f"caption-{key}",
        target=validate_send_target(dict(TARGET)),
    )


def _content(item: MediaItem) -> dict:
    return {
        "@type": "inputMessagePhoto",
        "photo": {"@type": "inputFileLocal", "path": str(item.path)},
        "caption": {"text": item.path.name},
    }


class _StubStrategy:
    kind = "image"
    validate_source_snapshots = True

    def __init__(self, plans: tuple[AlbumPlan, ...], *, builder=None):
        self._plans = plans
        self._builder = builder

    def scan(self, source_root, *, cancel_token, event_sink, context=None):
        del source_root, cancel_token, event_sink, context
        return ScanResult(items=tuple(i for p in self._plans for i in p.items))

    def build_plans(self, scan_result, *, target, state=None, context=None):
        del scan_result, target, state, context
        return self._plans

    def build_contents(self, plan, *, cancel_token, event_sink, context=None):
        if self._builder is not None:
            return self._builder(plan, cancel_token, event_sink, context)
        return tuple(_content(item) for item in plan.pending_items)


class _MemJournal:
    def __init__(self):
        self.records = {}

    def unresolved(self, kind, album_key, target=None):
        rec = self.records.get((kind, album_key))
        return None if not rec or rec.get("status") in {"FINALIZED", "CONFIRMED", "FAILED"} else rec

    def prepare(self, kind, album_key, items, target=None):
        self.records[(kind, album_key)] = {"kind": kind, "album_key": album_key, "status": "PREPARED", "items": items}

    def update(self, kind, album_key, status, **extra):
        rec = self.records.setdefault((kind, album_key), {"kind": kind, "album_key": album_key})
        rec.update({"status": status, **extra})

    def submitted(self, kind, album_key, message_ids, target=None):
        self.update(kind, album_key, "SUBMITTED", message_ids=list(message_ids))

    def mark_confirmed(self, kind, album_key, message_ids, target=None):
        self.update(kind, album_key, "CONFIRMED", message_ids=list(message_ids))

    def finalize(self, kind, album_key, target=None):
        self.update(kind, album_key, "FINALIZED")


class _MemState:
    def __init__(self, pre_confirmed=()):
        self.completed = {stable_path(p) for p in pre_confirmed}

    def is_completed(self, path, **_kwargs):
        return stable_path(path) in self.completed

    def mark_album_completed(self, items, message_ids):
        del message_ids
        for entry in items:
            raw = entry["path"] if isinstance(entry, dict) else entry.path
            self.completed.add(stable_path(raw))


class _FakeSender:
    def __init__(self, on_send=None):
        self.calls = []
        self.on_send = on_send

    def send_contents(self, contents, *, target=None, plan=None, context=None, prepared_media=None, **kwargs):
        del target, context, prepared_media, kwargs
        key = plan.key if plan is not None else ""
        self.calls.append((key, list(contents)))
        if self.on_send is not None:
            return self.on_send(key, contents)
        return [100 + i for i in range(len(contents))]


def _make_upload_page(target_getter, root_getter=None) -> UploadPage:
    del root_getter
    services = UploadPageServices(
        config_getter=lambda _k, default=None: default,
        target_getter=target_getter,
        caption_limit=1024,
    )
    return UploadPage("image", services=services)


def _preview_for(root: Path, plans: tuple[AlbumPlan, ...], *, completed_paths: tuple[str, ...] = ()) -> dict:
    completed_set = {stable_path(p) for p in completed_paths}
    groups_by_label: dict[str, list[dict]] = {}
    all_items = []
    for p in plans:
        item_dicts = []
        for it in p.items:
            d = {
                "path": it.path,
                "size": it.snapshot.size,
                "scan_size": it.snapshot.size,
                "scan_mtime_ns": it.snapshot.mtime_ns,
                "media_kind": it.media_kind,
                "capture_time": None,
            }
            item_dicts.append(d)
            all_items.append(d)
        groups_by_label.setdefault(p.group_label, []).append(
            {
                "key": p.key,
                "group": p.group_label,
                "number": p.number,
                "items": item_dicts,
                "pending_items": [d for d in item_dicts if stable_path(d["path"]) not in completed_set],
                "caption": {"text": p.caption},
            }
        )
    groups = []
    for label, g_plans in groups_by_label.items():
        g_items = [d for gp in g_plans for d in gp["items"]]
        groups.append(
            {
                "label": label,
                "items": g_items,
                "completed": sum(1 for d in g_items if stable_path(d["path"]) in completed_set),
                "pending": sum(1 for d in g_items if stable_path(d["path"]) not in completed_set),
                "albums": len(g_plans),
                "album_plans": g_plans,
            }
        )
    return {
        "kind": plans[0].kind if plans else "image",
        "source_dir": str(root),
        "target": dict(TARGET),
        "items": all_items,
        "groups": groups,
        "plans": tuple(plans),
        "total_files": len(all_items),
        "total_bytes": sum(d["size"] for d in all_items),
        "completed_paths": list(completed_paths),
        "skipped_album_keys": [],
        "scan_errors": (),
    }


class _Sink:
    def __init__(self):
        self.events = []

    def emit(self, event) -> None:
        self.events.append(event)


def _context(
    source_root: Path,
    *,
    state: object,
    journal: object,
    sender: object,
    error_policy: str = "pause_on_error",
    preflight_fn=None,
    retry_enabled: bool = False,
    max_retries: int = 1,
):
    preflight_obj = None
    if preflight_fn is not None:
        class _PreflightWrapper:
            def check(self, item, *, context=None):
                del context
                return preflight_fn(item)

        preflight_obj = _PreflightWrapper()
    return UploadContext(
        source_root=source_root,
        target=dict(TARGET),
        cancel_token=CancellationToken(),
        event_sink=_Sink(),
        kind="image",
        state=state,
        journal=journal,
        sender=sender,
        preflight=preflight_obj,
        metadata={
            "queue_send_policy": QueueSendPolicy(error_policy, True, 0.0, 0.0),
            "album_retry": AlbumRetryPolicy(enabled=retry_enabled, max_retries=max_retries, delay_seconds=0.0),
            "prefetch_next": False,
        },
    )


def _make_task(root: Path, plans: tuple[AlbumPlan, ...], config_dict: dict | None = None) -> UploadTask:
    raw = UploadTask(
        str(uuid4()),
        "image",
        root,
        validate_send_target(dict(TARGET)),
        dict(config_dict or {}),
        plans,
        "2026-01-01T00:00:00Z",
    )
    return UploadTask.from_record(raw.to_record())


def _select_task_album(page: TaskPage, task_id: str, row_idx: int = 0) -> None:
    page.select_task(task_id)
    item = page.albums.topLevelItem(row_idx)
    assert item is not None
    page.albums.setCurrentItem(item)


# ---------------------------------------------------------------------------
# Section 2.5: Strict mode vs Manual Retry Conflict
# ---------------------------------------------------------------------------


def test_strict_preflight_failure_retries_complete_unconfirmed_album_end_to_end(tmp_path, runtime_paths, qt_app):
    root = tmp_path / "gallery"
    files = [_seed(root / f"0{i}.jpg", f"p{i}".encode()) for i in range(1, 5)]
    items = tuple(_item(p, root) for p in files)
    p1 = _plan("alb1", root, items, caption="strict album caption")
    task = _make_task(root, (p1,), {"UPLOAD_QUEUE_ERROR_POLICY": "pause_on_error"})
    store = UploadQueue(tmp_path / "queue.json", runtime_paths=runtime_paths)
    store.enqueue(task)
    running = store.start(store.prepare(task))

    bad_path = files[2]

    def checker(item, **_kwargs):
        if item.path == bad_path:
            return PreflightDecision(PreflightStatus.FAILED, "corrupt header")
        return PreflightDecision(PreflightStatus.READY)

    sender = _FakeSender()
    result = UploadEngine(sender=sender, prefetch_next=False).run(
        _StubStrategy((p1,)),
        context=_context(root, state=_MemState(), journal=_MemJournal(), sender=sender, preflight_fn=checker),
    )
    assert sender.calls == []
    assert result.paused is True

    finished = store.finish(running, result)
    outcome = finished.album_statuses()[p1.key]
    assert outcome.status is AlbumStatus.FAILED
    assert outcome.failure_stage == "pre_submit"

    # GUI shows "重试该 Album" instead of "仅重试失败文件".
    task_page = TaskPage()
    task_page.show_queue(store)
    _select_task_album(task_page, finished.task_id, 0)
    assert task_page.album_retry.isEnabled()
    assert task_page.album_retry.text() == "重试该 Album"

    # Fixing C and scheduling manual retry keeps A, B, C, D in original order, target & caption.
    retrying = store.retry_album(finished.task_id, p1.key)
    reopened = UploadQueue(store.path, runtime_paths=runtime_paths)
    restored = reopened.tasks[0]
    exec_plans = restored.execution_plans()
    assert len(exec_plans) == 1
    assert exec_plans[0].items == p1.items
    assert exec_plans[0].pending_items == p1.items
    assert exec_plans[0].caption == "strict album caption"
    assert dict(exec_plans[0].target) == validate_send_target(dict(TARGET))


def test_strict_build_failure_retries_all_unconfirmed_members(tmp_path, runtime_paths, qt_app):
    root = tmp_path / "gallery"
    files = [_seed(root / f"0{i}.jpg", f"b{i}".encode()) for i in range(1, 5)]
    items = tuple(_item(p, root) for p in files)
    p1 = _plan("alb_build", root, items, caption="build album")
    store = UploadQueue(tmp_path / "queue.json", runtime_paths=runtime_paths)
    task = _make_task(root, (p1,))
    store.enqueue(task)
    running = store.start(store.prepare(task))

    def builder(plan, *_args):
        return ContentBuildResult(
            contents=tuple(_content(i) for i in plan.pending_items if i.path != files[2]),
            ready_items=tuple(i for i in plan.pending_items if i.path != files[2]),
            failed_items=(items[2],),
            errors=("ffmpeg decode failed",),
        )

    sender = _FakeSender()
    result = UploadEngine(sender=sender, prefetch_next=False).run(
        _StubStrategy((p1,), builder=builder),
        context=_context(root, state=_MemState(), journal=_MemJournal(), sender=sender),
    )
    assert sender.calls == []
    finished = store.finish(running, result)
    assert finished.album_statuses()[p1.key].failure_stage == "pre_submit"

    retrying = store.retry_album(finished.task_id, p1.key)
    selected = retrying.execution_plans()[0]
    assert selected.pending_items == items


def test_strict_retry_never_duplicates_historically_confirmed_members(tmp_path, runtime_paths, qt_app):
    root = tmp_path / "gallery"
    files = [_seed(root / f"0{i}.jpg", f"h{i}".encode()) for i in range(1, 5)]
    items = tuple(_item(p, root) for p in files)
    p1 = _plan("alb_hist", root, items)

    # Mark file 0 confirmed in SQLite checkpoint BEFORE the run.
    state = UploadState(kind="image", source_root=root, target=TARGET, state_dir=runtime_paths.IMAGE_STATE_DIR)
    state.mark_album_completed(
        [{"path": str(files[0]), "size": items[0].snapshot.size, "mtime_ns": items[0].snapshot.mtime_ns, "source_root": str(root)}],
        [501],
    )

    store = UploadQueue(tmp_path / "queue.json", runtime_paths=runtime_paths)
    task = _make_task(root, (p1,))
    store.enqueue(task)
    running = store.start(store.prepare(task))

    def checker(item, **_kwargs):
        if item.path == files[2]:
            return PreflightDecision(PreflightStatus.FAILED, "temporary unreadable")
        return PreflightDecision(PreflightStatus.READY)

    sender = _FakeSender()
    result = UploadEngine(sender=sender, prefetch_next=False).run(
        _StubStrategy((p1,)),
        context=_context(root, state=state, journal=_MemJournal(), sender=sender, preflight_fn=checker),
    )
    assert sender.calls == []
    # Match integration.py behavior: pre-confirmed items from UploadState populate confirmed_before_run.
    result = replace(result, confirmed_before_run=(items[0],))
    finished = store.finish(running, result)
    assert finished.album_statuses()[p1.key].status is AlbumStatus.PARTIAL
    assert finished.album_statuses()[p1.key].failure_stage == "pre_submit"

    task_page = TaskPage()
    task_page.show_queue(store)
    _select_task_album(task_page, finished.task_id, 0)
    assert task_page.album_retry.isEnabled()
    assert task_page.album_retry.text() == "重试该 Album"

    retrying = store.retry_album(finished.task_id, p1.key)
    selected = retrying.execution_plans()[0]
    assert selected.items == items
    assert [i.path for i in selected.pending_items] == files[1:]


def test_partial_delivery_after_protection_lifted_allows_selective_retry(tmp_path, runtime_paths, qt_app, monkeypatch):
    root = tmp_path / "gallery"
    f1 = _seed(root / "01.jpg", b"ok-1")
    f2 = _seed(root / "02.jpg", b"fail-2")
    i1, i2 = _item(f1, root), _item(f2, root)
    p1 = _plan("alb_part", root, (i1, i2))

    monkeypatch.setattr(image_pipeline, "STATE_DIR", runtime_paths.IMAGE_STATE_DIR)
    task = _make_task(root, (p1,))
    store = UploadQueue(tmp_path / "queue.json", runtime_paths=runtime_paths)
    store.enqueue(task)
    running = store.start(store.prepare(task))

    journal = InflightJournal(runtime_paths.UPLOAD_INFLIGHT_DIR)
    state = UploadState(kind="image", source_root=root, target=TARGET, state_dir=runtime_paths.IMAGE_STATE_DIR)

    def partial_sender(_key, contents):
        return {
            "status": "PARTIAL",
            "message_ids": [701, 702],
            "succeeded_ids": [701],
            "failed_ids": [702],
            "item_results": [
                {"index": 0, "path": str(f1), "message_id": 701, "status": "succeeded"},
                {"index": 1, "path": str(f2), "message_id": 702, "status": "failed", "error": "rejected"},
            ],
            "error": "one item failed",
        }

    sender_p = _FakeSender(partial_sender)
    result = UploadEngine(sender=sender_p, prefetch_next=False).run(
        _StubStrategy((p1,)),
        context=_context(root, state=state, journal=journal, sender=sender_p),
    )
    reviewed = store.finish(running, result)
    assert reviewed.status is TaskStatus.NEEDS_REVIEW
    assert reviewed.album_statuses()[p1.key].status is AlbumStatus.UNKNOWN

    task_page = TaskPage()
    task_page.show_queue(store)
    _select_task_album(task_page, reviewed.task_id, 0)
    assert not task_page.album_retry.isEnabled()
    assert task_page.album_retry.text() == "需要人工核对"
    with pytest.raises(ValueError):
        store.retry_album(reviewed.task_id, p1.key)

    # Reconcile (sent=False preserves item 0 succeeded in state, keeps item 1 for reupload, lifts UNKNOWN).
    rec = journal.unresolved("image", p1.key, TARGET)
    ReconciliationService(journal).reconcile_inflight(p1.key, sent=False, kind="image", target=dict(TARGET))
    store.refresh_reviews(rec)
    after = store.tasks[0]
    outcome = after.album_statuses()[p1.key]
    assert outcome.status is AlbumStatus.PARTIAL
    assert outcome.failure_stage == "partial_delivery"
    assert outcome.failed_paths == (stable_path(f2),)

    task_page.show_queue(store)
    _select_task_album(task_page, after.task_id, 0)
    assert task_page.album_retry.isEnabled()
    assert task_page.album_retry.text() == "仅重试失败文件"

    retry_task = store.retry_album(after.task_id, p1.key)
    selected = retry_task.execution_plans()[0]
    assert [i.path for i in selected.pending_items] == [f2]


def test_unknown_and_persistence_failures_block_manual_retry_and_require_review(tmp_path, runtime_paths, qt_app):
    root = tmp_path / "gallery"
    f1 = _seed(root / "01.jpg", b"p1")
    i1 = _item(f1, root)
    p1 = _plan("alb_pers", root, (i1,))
    store = UploadQueue(tmp_path / "queue.json", runtime_paths=runtime_paths)

    task = _make_task(root, (p1,))
    store.enqueue(task)
    running = store.start(store.prepare(task))
    pers_reviewed = store.finish(
        running,
        UploadRunResult(
            "PARTIAL",
            paused=True,
            batches=(UploadBatchResult(p1.key, BatchStatus.CONFIRMED, (10,), (i1,), error="state write failed", failure_stage="persistence"),),
            error="state write failed",
        ),
    )
    assert pers_reviewed.status is TaskStatus.NEEDS_REVIEW
    task_page = TaskPage()
    task_page.show_queue(store)
    _select_task_album(task_page, pers_reviewed.task_id, 0)
    assert not task_page.album_retry.isEnabled()
    assert task_page.album_retry.text() == "需要人工核对"
    with pytest.raises(ValueError):
        store.retry_album(pers_reviewed.task_id, "alb_pers")

    # Even if engine encounters state_mark_completed failure on CONFIRMED send:
    class _FailState(_MemState):
        def mark_album_completed(self, items, message_ids):
            raise OSError("sqlite full")

    sender = _FakeSender()
    journal = InflightJournal(runtime_paths.UPLOAD_INFLIGHT_DIR)
    res = UploadEngine(sender=sender, prefetch_next=False).run(
        _StubStrategy((p1,)),
        context=_context(root, state=_FailState(), journal=journal, sender=sender),
    )
    assert res.batches[0].status is BatchStatus.CONFIRMED
    assert res.batches[0].failure_stage == "persistence"


def test_strict_retry_failure_keeps_subsequent_album_unsubmitted_and_success_resumes_queue(tmp_path, runtime_paths, qt_app):
    root = tmp_path / "gallery"
    f1 = _seed(root / "01.jpg", b"a1")
    f2 = _seed(root / "02.jpg", b"a2")
    p1 = _plan("alb_1", root, (_item(f1, root),), number=1)
    p2 = _plan("alb_2", root, (_item(f2, root),), number=2)
    store = UploadQueue(tmp_path / "queue.json", runtime_paths=runtime_paths)
    task = _make_task(root, (p1, p2))
    later = task_for(tmp_path / "later")
    store.enqueue(task)
    store.enqueue(later)

    run1 = store.start(store.prepare(task))
    sender1 = _FakeSender(lambda key, contents: (_ for _ in ()).throw(SendRequestRejected(400, "PHOTO_INVALID_DIMENSIONS", None)))
    res1 = UploadEngine(sender=sender1, prefetch_next=False).run(
        _StubStrategy((p1, p2)),
        context=_context(root, state=_MemState(), journal=_MemJournal(), sender=sender1),
    )
    assert len(sender1.calls) == 1
    assert res1.batches[0].failure_stage == "request_rejected"
    assert res1.paused is True

    failed1 = store.finish(run1, res1)
    assert failed1.album_statuses()["alb_1"].failure_stage == "request_rejected"
    assert failed1.album_statuses()["alb_2"].status is AlbumStatus.PENDING

    retry1 = store.retry_album(failed1.task_id, "alb_1")
    assert [p.key for p in retry1.execution_plans()] == ["alb_1"]
    store.set_paused(False)
    run2 = store.start(store.prepare(retry1))
    res2 = UploadEngine(sender=sender1, prefetch_next=False).run(
        _StubStrategy(run2.execution_plans()),
        context=_context(root, state=_MemState(), journal=_MemJournal(), sender=sender1),
    )
    failed_again = store.finish(run2, res2)
    assert failed_again.status is TaskStatus.FAILED
    assert failed_again.retry_album_key == "alb_1"
    assert failed_again.album_statuses()["alb_2"].status is AlbumStatus.PENDING
    # Task stays head of queue ahead of `later`.
    assert [t.task_id for t in store.tasks] == [failed_again.task_id, later.task_id]

    # Now retry succeeds on alb_1: task must NOT be marked COMPLETED while alb_2 is PENDING!
    real_state = UploadState(kind="image", source_root=root, target=TARGET, state_dir=runtime_paths.IMAGE_STATE_DIR)
    real_journal = InflightJournal(runtime_paths.UPLOAD_INFLIGHT_DIR)
    retry2 = store.restore(failed_again.task_id)
    assert [p.key for p in retry2.execution_plans()] == ["alb_1"]
    store.set_paused(False)
    run3 = store.start(store.prepare(retry2))
    sender_ok = _FakeSender()
    res3 = UploadEngine(sender=sender_ok, prefetch_next=False).run(
        _StubStrategy(run3.execution_plans()),
        context=_context(root, state=real_state, journal=real_journal, sender=sender_ok),
    )
    partial_done = store.finish(run3, res3)
    assert partial_done.status is TaskStatus.FAILED
    assert partial_done.outcome == "PARTIAL"
    assert partial_done.paused_by_policy is True
    assert partial_done.retry_album_key is None
    assert partial_done.album_statuses()["alb_1"].status is AlbumStatus.COMPLETED
    assert partial_done.album_statuses()["alb_2"].status is AlbumStatus.PENDING
    assert [t.task_id for t in store.tasks] == [partial_done.task_id, later.task_id]

    # Resuming via standard restore continues alb_2 cleanly.
    resumed_rest = store.restore(partial_done.task_id)
    assert [p.key for p in resumed_rest.execution_plans()] == ["alb_1", "alb_2"]
    assert resumed_rest.known_confirmed == {stable_path(f1)}


def test_legacy_task_without_failure_stage_uses_safe_recovery_path(tmp_path, runtime_paths, qt_app):
    root = tmp_path / "legacy"
    files = [_seed(root / f"0{i}.jpg", f"l{i}".encode()) for i in range(1, 5)]
    items = tuple(_item(p, root) for p in files)
    p1 = _plan("alb_legacy", root, items)
    store = UploadQueue(tmp_path / "queue.json", runtime_paths=runtime_paths)

    # Legacy record missing failure_stage where only 1 of 4 normal items is in failed_paths.
    pending = _make_task(root, (p1,))
    store.enqueue(pending)
    running = store.start(store.prepare(pending))
    store.finish(
        running,
        UploadRunResult(
            "FAILED",
            batches=(UploadBatchResult(p1.key, BatchStatus.FAILED, error="legacy fail"),),
            failed_items=(items[2],),
        ),
    )
    reopened = UploadQueue(store.path, runtime_paths=runtime_paths)
    loaded = reopened.tasks[0]

    task_page = TaskPage()
    task_page.show_queue(reopened)
    _select_task_album(task_page, loaded.task_id, 0)
    assert not task_page.album_retry.isEnabled()
    assert task_page.album_retry.text() == "需要人工核对"
    with pytest.raises(ValueError):
        reopened.retry_album(loaded.task_id, "alb_legacy")

    # Full restore still works cleanly across all items.
    restored = reopened.restore(loaded.task_id)
    assert restored.status is TaskStatus.PENDING
    assert restored.execution_plans()[0].pending_items == items


def test_skip_and_retry_tail_retry_does_not_regress(tmp_path):
    root = tmp_path / "skip_retry"
    f1 = _seed(root / "01.jpg", b"sr1")
    f2 = _seed(root / "02.jpg", b"sr2")
    p1 = _plan("alb_1", root, (_item(f1, root),), number=1)
    p2 = _plan("alb_2", root, (_item(f2, root),), number=2)

    attempts = {"alb_1": 0, "alb_2": 0}

    def flaky_sender(key, contents):
        attempts[key] += 1
        if key == "alb_1" and attempts[key] == 1:
            raise SendRequestRejected(500, "transient telegram send error", None)
        return [200 + attempts[key]]

    sender_sr = _FakeSender(flaky_sender)
    res = UploadEngine(sender=sender_sr, prefetch_next=False).run(
        _StubStrategy((p1, p2)),
        context=_context(
            root,
            state=_MemState(),
            journal=_MemJournal(),
            sender=sender_sr,
            error_policy="skip_and_retry",
            retry_enabled=True,
            max_retries=2,
        ),
    )
    assert res.status == "COMPLETED"
    assert attempts["alb_1"] == 2 and attempts["alb_2"] == 1

    # Also verify FloodWait (420) recovery and whole-batch terminal "send_failed" classification.
    flood_attempts = {"alb_1": 0}

    def flood_then_fail_sender(key, contents):
        flood_attempts[key] = flood_attempts.get(key, 0) + 1
        if key == "alb_1" and flood_attempts[key] == 1:
            raise SendRequestRejected(420, "FLOOD_WAIT_0", 0)
        if key == "alb_2":
            return {
                "status": "FAILED",
                "message_ids": [901],
                "failed_ids": [901],
                "item_results": [
                    {"index": 0, "status": "failed", "requires_action": False, "message_id": 901, "error_code": 400, "error": "MEDIA_INVALID"}
                ],
                "error": "MEDIA_INVALID",
            }
        return [301]

    res2 = UploadEngine(sender=_FakeSender(flood_then_fail_sender), prefetch_next=False).run(
        _StubStrategy((p1, p2)),
        context=_context(
            root,
            state=_MemState(),
            journal=_MemJournal(),
            sender=_FakeSender(flood_then_fail_sender),
            error_policy="skip_and_retry",
            retry_enabled=True,
            max_retries=2,
        ),
    )
    assert flood_attempts["alb_1"] == 2
    by_key = {b.album_key: b for b in res2.batches}
    assert by_key["alb_1"].status is BatchStatus.CONFIRMED
    assert by_key["alb_2"].status is BatchStatus.FAILED
    assert by_key["alb_2"].failure_stage == "send_failed"


# ---------------------------------------------------------------------------
# Section 3.5: Regrouped Album State Consistency
# ---------------------------------------------------------------------------


def test_regrouped_albums_keep_consistent_parent_delivery_errors_and_review_blocks(tmp_path, qt_app):
    root = tmp_path / "regroup"
    f1 = _seed(root / "01.jpg", b"r1")
    f2 = _seed(root / "02.jpg", b"r2")
    f3 = _seed(root / "03.jpg", b"r3")
    i1, i2, i3 = _item(f1, root), _item(f2, root), _item(f3, root)

    # 1. Split: historical album key had [f1, f2], preview now splits into new_a=[f1], new_b=[f2].
    old_plan = _plan("old_alb", root, (i1, i2))
    task_split = UploadTask(
        "t-split",
        "image",
        root,
        dict(TARGET),
        {},
        (old_plan,),
        "2026-01-01T00:00:00Z",
        status=TaskStatus.FAILED,
        album_outcomes=(
            AlbumOutcome("old_alb", AlbumStatus.FAILED, error="bad group", failed_paths=(stable_path(f1), stable_path(f2))),
        ),
    )
    new_a = _plan("new_a", root, (i1,), number=1)
    new_b = _plan("new_b", root, (i2,), number=2)
    page = _make_upload_page(lambda _k: dict(TARGET))
    page.set_result(_preview_for(root, (new_a, new_b)))
    page.sync_task_outcomes(task_split)
    assert page._album_delivery["new_a"] == "失败"
    assert page._album_delivery["new_b"] == "失败"
    assert page._album_rows["new_a"].values[0] == "失败"
    assert page._album_rows["new_b"].values[0] == "失败"

    # 2. Merged + UNKNOWN propagation: historical p1=[f1] DONE, p2=[f2] UNKNOWN -> merged=[f1, f2] must show 待核对!
    hp1 = _plan("hp1", root, (i1,), number=1)
    hp2 = _plan("hp2", root, (i2,), number=2)
    task_merged = UploadTask(
        "t-merged",
        "image",
        root,
        dict(TARGET),
        {},
        (hp1, hp2),
        "2026-01-01T00:00:00Z",
        status=TaskStatus.NEEDS_REVIEW,
        confirmed_paths=(stable_path(f1),),
        album_outcomes=(
            AlbumOutcome("hp1", AlbumStatus.COMPLETED),
            AlbumOutcome("hp2", AlbumStatus.UNKNOWN, error="unknown result"),
        ),
    )
    merged_plan = _plan("merged_alb", root, (i1, i2, i3), number=1)
    page2 = _make_upload_page(lambda _k: dict(TARGET))
    page2.set_result(_preview_for(root, (merged_plan,), completed_paths=(stable_path(f1),)))
    page2.sync_task_outcomes(task_merged)
    assert page2._album_delivery["merged_alb"] == "待核对"
    assert page2._album_rows["merged_alb"].values[0] == "待核对"

    # Skip/restore orthogonality on regrouped album keeps real delivery status.
    idx = page2._preview_filter.mapFromSource(
        page2._preview_model.index(page2._album_rows["merged_alb"].parent.children.index(page2._album_rows["merged_alb"]), 0)
    )
    page2.tree.setCurrentIndex(idx)
    page2._toggle_album_skip(idx)
    assert page2._album_rows["merged_alb"].values[0] == "待核对 · 已排除"
    page2._toggle_album_skip(idx)
    assert page2._album_rows["merged_alb"].values[0] == "待核对"

    # 3. Same album key with completed files + album-level persistence/unknown issue MUST stay 待核对.
    same_p = _plan("same_alb", root, (i1,), number=1)
    task_pers = UploadTask(
        "t-pers-sync",
        "image",
        root,
        dict(TARGET),
        {},
        (same_p,),
        "2026-01-01T00:00:00Z",
        status=TaskStatus.NEEDS_REVIEW,
        confirmed_paths=(stable_path(f1),),
        album_outcomes=(
            AlbumOutcome("same_alb", AlbumStatus.UNKNOWN, error="checkpoint write failed", failure_stage="persistence"),
        ),
    )
    page3 = _make_upload_page(lambda _k: dict(TARGET))
    page3.set_result(_preview_for(root, (same_p,), completed_paths=(stable_path(f1),)))
    page3.sync_task_outcomes(task_pers)
    assert page3._album_delivery["same_alb"] == "待核对"
    assert page3._album_rows["same_alb"].values[0] == "待核对"

    # 4. Snapshot mismatch on file does NOT copy historical failure to modified file.
    time.sleep(0.01)
    f1.write_bytes(b"modified-bytes-longer")
    modified_i1 = _item(f1, root)
    new_mod_plan = _plan("mod_alb", root, (modified_i1,), number=1)
    page4 = _make_upload_page(lambda _k: dict(TARGET))
    page4.set_result(_preview_for(root, (new_mod_plan,)))
    page4.sync_task_outcomes(task_split)
    assert page4._file_delivery.get(stable_path(f1)) != "失败"
    assert page4._album_delivery["mod_alb"] == "待上传"


# ---------------------------------------------------------------------------
# Section 4.3: Manual Reconciliation Actual File Stat Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure_mode", ["deleted", "source_missing", "permission_error", "changed"])
def test_reconciliation_sync_rejects_unreadable_deleted_or_changed_files_without_mutating_preview(
    tmp_path, qt_app, monkeypatch, failure_mode
):
    root = tmp_path / "src"
    f1 = _seed(root / "01.jpg", b"file-1")
    f2 = _seed(root / "02.jpg", b"file-2")
    i1, i2 = _item(f1, root), _item(f2, root)
    p1 = _plan("alb_rec", root, (i1, i2))

    state_dir = tmp_path / "state"
    monkeypatch.setattr(image_pipeline, "STATE_DIR", state_dir)
    journal = InflightJournal(tmp_path / "journal")
    journal.prepare(
        "image",
        "alb_rec",
        [
            {"path": str(f1), "source_root": str(root), "scan_size": i1.snapshot.size, "scan_mtime_ns": i1.snapshot.mtime_ns},
            {"path": str(f2), "source_root": str(root), "scan_size": i2.snapshot.size, "scan_mtime_ns": i2.snapshot.mtime_ns},
        ],
        target=dict(TARGET),
    )
    journal.unknown("image", "alb_rec", error="unknown", target=dict(TARGET))

    page = _make_upload_page(lambda _k: dict(TARGET))
    page.set_result(_preview_for(root, (p1,)))
    task = UploadTask(
        "t-rec",
        "image",
        root,
        dict(TARGET),
        {},
        (p1,),
        "2026-01-01T00:00:00Z",
        status=TaskStatus.NEEDS_REVIEW,
        album_outcomes=(AlbumOutcome("alb_rec", AlbumStatus.UNKNOWN, error="unknown"),),
    )
    page.sync_task_outcomes(task)

    confirmation = ReconciliationService(journal).reconcile_inflight("alb_rec", sent=True, kind="image", target=dict(TARGET))
    assert confirmation.syncable is True
    assert journal.unresolved("image", "alb_rec", TARGET) is None  # persistent reconciliation succeeded

    if failure_mode == "deleted":
        f2.unlink()
    elif failure_mode == "source_missing":
        root.rename(tmp_path / "src_disconnected")
    elif failure_mode == "changed":
        f2.write_bytes(b"changed-size")
    elif failure_mode == "permission_error":
        orig_stat = Path.stat

        def guarded_stat(self, *args, **kwargs):
            if stable_path(self) == stable_path(f2):
                raise PermissionError("access denied")
            return orig_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", guarded_stat)

    synced, msg = page.sync_reconciled_album(confirmation)
    assert synced is False
    assert "重新扫描" in msg
    assert page.result["completed_paths"] == []
    assert page._album_delivery["alb_rec"] == "待核对"
    assert page._file_delivery.get(stable_path(f1)) != "已完成"


# ---------------------------------------------------------------------------
# Section 5.5: Partial Preview Update Operation Complexity Contract
# ---------------------------------------------------------------------------


def test_partial_preview_updates_avoid_full_tree_resets_and_preserve_totals(tmp_path, qt_app):
    root = tmp_path / "perf"
    plans = []
    # Seed real files for alb_0..alb_1 and synthetic snapshots for remaining 198 albums (2000 items total).
    real_files = {}
    for alb_idx in range(2):
        items = []
        for j in range(10):
            p = _seed(root / f"g_{alb_idx // 20}" / f"alb_{alb_idx}_{j:02d}.jpg", f"{alb_idx}-{j}".encode())
            items.append(_item(p, root, group_name=f"g_{alb_idx // 20}"))
        real_files[alb_idx] = items
        plans.append(_plan(f"alb_{alb_idx}", root, tuple(items), number=alb_idx + 1, group_label=f"g_{alb_idx // 20}"))
    for alb_idx in range(2, 200):
        g_name = f"g_{alb_idx // 20}"
        items = []
        for j in range(10):
            p = root / g_name / f"alb_{alb_idx}_{j:02d}.jpg"
            items.append(
                MediaItem(
                    path=p,
                    source_root=root,
                    media_kind="image",
                    snapshot=FileSnapshot(stable_path(p), 512 + j, 1700000000000000000 + j),
                    group_name=g_name,
                )
            )
        plans.append(_plan(f"alb_{alb_idx}", root, tuple(items), number=alb_idx + 1, group_label=g_name))

    page = _make_upload_page(lambda _k: dict(TARGET))
    page.set_result(_preview_for(root, tuple(plans)))

    update_calls = []
    resets = []
    orig_update_row = page._preview_model.update_row
    page._preview_model.update_row = lambda row, *args, **kwargs: (update_calls.append(row), orig_update_row(row, *args, **kwargs))[1]
    page._preview_model.modelReset.connect(lambda: resets.append(1))

    row0 = page._album_rows["alb_0"]
    idx0 = page._preview_filter.mapFromSource(page._preview_model.index(row0.parent.children.index(row0), 0))
    page.tree.setCurrentIndex(idx0)

    plans_before_skip = page.result["plans"]
    update_calls.clear()
    resets.clear()
    page._toggle_album_skip(idx0)
    assert resets == []
    assert page.result["plans"] is plans_before_skip
    assert len(update_calls) <= 20  # 10 files + 1 album + 1 group (not 2000+ rows)
    assert page.result["pending_files"] == 1990
    assert page.result["skipped_albums"] == 1
    assert page.result["skipped_files"] == 10

    update_calls.clear()
    page._toggle_album_skip(idx0)
    assert resets == []
    assert page.result["plans"] is plans_before_skip
    assert len(update_calls) <= 20
    assert page.result["pending_files"] == 2000
    assert page.result["skipped_albums"] == 0

    # Single album task outcome sync only touches affected album.
    p1 = plans[1]
    task = UploadTask(
        "t-one",
        "image",
        root,
        dict(TARGET),
        {},
        (p1,),
        "2026-01-01T00:00:00Z",
        status=TaskStatus.FAILED,
        album_outcomes=(AlbumOutcome("alb_1", AlbumStatus.FAILED, error="boom", failed_paths=tuple(stable_path(i.path) for i in p1.items)),),
    )
    update_calls.clear()
    page.sync_task_outcomes(task)
    assert resets == []
    assert len(update_calls) <= 20
    assert page._album_delivery["alb_1"] == "失败"

    # Single album manual reconciliation also updates only the reconciled album.
    snaps_0 = tuple(i.snapshot for i in real_files[0])
    rec = ReconciledAlbum(
        source_root=root,
        target=dict(TARGET),
        snapshots=snaps_0,
        kind="image",
        album_key="alb_0",
        sent=False,
        confirmed_snapshots=snaps_0[:4],
        reupload_snapshots=snaps_0[4:],
        syncable=True,
    )
    update_calls.clear()
    ok, _ = page.sync_reconciled_album(rec)
    assert ok is True
    assert resets == []
    assert len(update_calls) <= 20
    assert page._album_delivery["alb_0"] == "部分完成"
    assert page.result["completed_files"] == 4
    assert page.result["pending_files"] == 1996

    # Pending-only filter reflects skipped state consistently via incremental update_album_rows.
    page.pending_only.setChecked(True)
    assert page._preview_filter.rowCount() == 200
    src_idx0 = page._preview_model.index(row0.parent.children.index(row0), 0)
    page._toggle_album_skip(page._preview_filter.mapFromSource(src_idx0))
    assert page._preview_filter.rowCount() == 199
    assert page.result["skipped_albums"] == 1
    assert page.result["pending_files"] == 1990
    page.pending_only.setChecked(False)
    page._toggle_album_skip(page._preview_filter.mapFromSource(src_idx0))
    page.pending_only.setChecked(True)
    assert page._preview_filter.rowCount() == 200
