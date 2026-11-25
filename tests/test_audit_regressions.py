"""Section 7.2 regression tests for PR #49 / #50 audit fixes."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import threading

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from conftest import TARGET, task_for
from tdlib_media_uploader.config import loader, paths
from tdlib_media_uploader.config.snapshot import snapshot_config
from tdlib_media_uploader.contracts import MediaStrategy, ScanResult, UploadContext
from tdlib_media_uploader.core import album, logging as app_logging, upload_journal, upload_state
from tdlib_media_uploader.core.concurrency import CancellationToken
from tdlib_media_uploader.core.filesystem import file_snapshot, stable_path
from tdlib_media_uploader.core.models import (
    AlbumPlan,
    BatchStatus,
    ContentBuildResult,
    FileSnapshot,
    MediaItem,
    UploadBatchResult,
    UploadRunResult,
)
from tdlib_media_uploader.core.source_snapshot import SourceChanged
from tdlib_media_uploader.core.upload_journal import InflightJournal
from tdlib_media_uploader.core.upload_state import UploadState
from tdlib_media_uploader.core.uploader_database import database_for
from tdlib_media_uploader.gui import main_window, preferences, scanner
from tdlib_media_uploader.gui.pages.task import TaskPage
from tdlib_media_uploader.gui.pages.upload import UploadPage, UploadPageServices
from tdlib_media_uploader.gui.preferences import UiPreferences
from tdlib_media_uploader.gui.theme import initialize_theme
from tdlib_media_uploader.media import image_pipeline
from tdlib_media_uploader.telegram.send_result import SendRequestRejected
from tdlib_media_uploader.upload import preflight, reconciliation
from tdlib_media_uploader.upload.engine import UploadEngine
from tdlib_media_uploader.upload.preflight import PreflightDecision, PreflightStatus
from tdlib_media_uploader.upload.queue import AlbumOutcome, AlbumStatus, TaskStatus, UploadTask, finish_task
from tdlib_media_uploader.upload.queue_store import UploadQueue
from tdlib_media_uploader.upload.reconciliation import ReconciledAlbum, ReconciliationService
from tdlib_media_uploader.upload.retry import AlbumRetryPolicy, QueueSendPolicy


def _seed_file(path: Path, payload: bytes = b"media-bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _make_item(path: Path, source_root: Path, media_kind: str = "image") -> MediaItem:
    size, mtime_ns = file_snapshot(path)
    return MediaItem(
        path=path,
        source_root=source_root,
        media_kind=media_kind,
        snapshot=FileSnapshot(stable_path(path), size, mtime_ns),
        group_name="group-1",
    )


def _make_plan(
    key: str,
    source_root: Path,
    items: tuple[MediaItem, ...],
    *,
    number: int = 1,
    kind: str = "image",
    target: dict | None = None,
    caption: str | None = None,
) -> AlbumPlan:
    return AlbumPlan(
        key=key,
        kind=kind,
        source_root=source_root,
        group_label="26-11",
        number=number,
        items=items,
        pending_items=items,
        caption=caption if caption is not None else key,
        target=dict(target or TARGET),
    )


def _content_for(item: MediaItem) -> dict:
    return {
        "@type": "inputMessagePhoto",
        "photo": {"@type": "inputFileLocal", "path": str(item.path)},
        "caption": {"text": item.path.name},
    }


class _RecordingSink:
    def __init__(self):
        self.events = []
        self.logs = []

    def emit(self, event) -> None:
        self.events.append(event)

    def log(self, level: str, message: str) -> None:
        self.logs.append((level, message))


class _FakeRemoteAlbum:
    def __init__(self, key: str, contents: tuple = ()):
        self.key = key
        self.contents = tuple(contents)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeStager:
    def __init__(self, on_stage=None):
        self.staged: list[str] = []
        self.cleaned: list[tuple[str, bool]] = []
        self.on_stage = on_stage

    def stage(self, plan: AlbumPlan, *, context=None):
        self.staged.append(plan.key)
        if self.on_stage is not None:
            self.on_stage(plan, context)
        return plan

    def cleanup(self, plan: AlbumPlan, *, context=None, confirmed: bool = False):
        self.cleaned.append((plan.key, bool(confirmed)))


class _FakeSender:
    def __init__(self, on_send=None, *, prepare_remote_fn=None):
        self.sent: list[tuple[str, list[Path]]] = []
        self.on_send = on_send
        self._prepare_remote_fn = prepare_remote_fn
        self.remote_albums: dict[str, _FakeRemoteAlbum] = {}

    def prepare_remote(self, contents, *, context=None):
        if self._prepare_remote_fn is None:
            return None
        return self._prepare_remote_fn(contents, context)

    def send_contents(self, contents, *, target=None, plan=None, context=None, prepared_media=None, **kwargs):
        key = plan.key if plan is not None else ""
        paths_list = [item.path for item in (plan.pending_items if plan is not None else ())]
        if self.on_send is not None:
            result = self.on_send(key, contents, plan, context, prepared_media)
            self.sent.append((key, paths_list))
            return result
        self.sent.append((key, paths_list))
        return list(range(100, 100 + len(contents)))


class _StubStrategy(MediaStrategy):
    kind = "image"

    def __init__(self, plans: tuple[AlbumPlan, ...], *, builder=None, validate_source_snapshots: bool = True):
        self._plans = plans
        self._builder = builder
        self.validate_source_snapshots = validate_source_snapshots

    def scan(self, source_root, *, cancel_token, event_sink, context=None):
        del source_root, cancel_token, event_sink, context
        items = tuple(item for plan in self._plans for item in plan.items)
        return ScanResult(items=items)

    def build_plans(self, scan_result, *, target, state=None, context=None):
        return self._plans

    def build_contents(self, plan, *, cancel_token, event_sink, context=None):
        if self._builder is not None:
            return self._builder(plan, cancel_token, event_sink, context)
        return tuple(_content_for(item) for item in plan.pending_items)


def _preview_result_for(
    source_root: Path,
    target: dict,
    plans: tuple[AlbumPlan, ...],
    *,
    completed_paths: tuple[str, ...] = (),
    skipped_album_keys: tuple[str, ...] = (),
    scan_errors: tuple[str, ...] = (),
) -> dict:
    completed_set = {stable_path(path) for path in completed_paths}
    all_items = []
    album_plans = []
    for plan in plans:
        plan_item_dicts = []
        for item in plan.items:
            entry = {
                "path": item.path,
                "size": item.snapshot.size,
                "scan_size": item.snapshot.size,
                "scan_mtime_ns": item.snapshot.mtime_ns,
                "media_kind": item.media_kind,
                "capture_time": None,
            }
            plan_item_dicts.append(entry)
            all_items.append(entry)
        album_plans.append(
            {
                "key": plan.key,
                "group": plan.group_label,
                "number": plan.number,
                "items": plan_item_dicts,
                "pending_items": [
                    entry for entry in plan_item_dicts if stable_path(entry["path"]) not in completed_set
                ],
                "caption": {"text": plan.caption},
            }
        )
    return {
        "kind": plans[0].kind if plans else "image",
        "source_dir": str(source_root),
        "target": dict(target),
        "items": all_items,
        "groups": [
            {
                "label": "26-11",
                "items": list(all_items),
                "completed": 0,
                "pending": len(all_items),
                "albums": len(album_plans),
                "album_plans": album_plans,
            }
        ]
        if all_items
        else [],
        "plans": tuple(plans),
        "total_files": len(all_items),
        "total_bytes": sum(item["size"] for item in all_items),
        "completed_paths": list(completed_paths),
        "skipped_album_keys": list(skipped_album_keys),
        "scan_errors": tuple(scan_errors),
    }


def _make_upload_page(kind: str, target_getter, root_getter, prefs: UiPreferences | None = None) -> UploadPage:
    del root_getter
    services = UploadPageServices(
        config_getter=lambda key, default=None: default,
        target_getter=target_getter,
        caption_limit=1024,
    )
    page = UploadPage(kind, services=services)
    if prefs is not None:
        page.set_target_label_provider(prefs.get_target_label)
    return page


@pytest.fixture
def isolated_window(tmp_path, qt_app, monkeypatch, runtime_paths):
    app_data_dir = tmp_path / "app_data"
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr(paths, "DATA_DIR", app_data_dir)
    monkeypatch.setattr(paths, "LOG_DIR", logs_dir)
    monkeypatch.setattr(paths, "UPLOAD_INFLIGHT_DIR", runtime_paths.UPLOAD_INFLIGHT_DIR)
    monkeypatch.setattr(app_logging, "LOG_DIR", logs_dir)
    monkeypatch.setattr(app_logging, "APP_LOG_PATH", logs_dir / "app.log")
    monkeypatch.setattr(upload_journal, "APP_DATA_DIR", app_data_dir)
    monkeypatch.setattr(upload_journal, "UPLOAD_INFLIGHT_DIR", runtime_paths.UPLOAD_INFLIGHT_DIR)
    monkeypatch.setattr(main_window, "_save_history", lambda _records: None)
    monkeypatch.setattr(main_window, "_load_history", list)
    monkeypatch.setattr(album, "CAPTIONS_DIR", tmp_path / "captions")
    monkeypatch.setattr(scanner, "runtime_paths", runtime_paths)
    monkeypatch.setattr(main_window, "_target_for", lambda _kind: dict(TARGET))
    cfg = snapshot_config(loader)
    cfg.VIDEO_READ_DATES = False
    cfg.VIDEO_GENERATE_THUMBNAIL = False
    cfg.MIXED_GENERATE_THUMBNAIL = False
    cfg.STAGING_ENABLED = False
    cfg.STAGING_MODE = "off"
    cfg.PREFETCH_NEXT_ALBUM = False
    cfg.VIDEO_PREFETCH_NEXT_ALBUM = False
    monkeypatch.setattr(main_window.config_service, "cfg", cfg)
    prefs = UiPreferences(tmp_path / "ui_preferences.json")
    initialize_theme(qt_app, prefs)
    store = UploadQueue(tmp_path / "upload_queue.json", runtime_paths=runtime_paths)
    store.set_paused(True)
    win = main_window.MainWindow(queue_store=store, preferences=prefs)
    win.queue.can_start = lambda: False
    win.queue.make_worker = lambda task: pytest.fail(f"unexpected real UploadWorker for {task.task_id}")
    win.show()
    yield win, cfg, prefs
    win.close()


# ---------------------------------------------------------------------------
# Engine: pause_on_error strict sequential semantics & skip_and_retry
# ---------------------------------------------------------------------------


def test_strict_mode_partial_preflight_failure_stops_album_and_subsequent_prefetch(tmp_path):
    root = tmp_path / "album"
    a1 = _make_item(_seed_file(root / "01.jpg", b"a1"), root)
    a2 = _make_item(_seed_file(root / "02.jpg", b"a2"), root)
    b1 = _make_item(_seed_file(root / "03.jpg", b"b1"), root)
    p1 = _make_plan("alb1", root, (a1, a2), number=1)
    p2 = _make_plan("alb2", root, (b1,), number=2)

    sender = _FakeSender()
    stager = _FakeStager()
    sink = _RecordingSink()
    engine = UploadEngine(
        sender=sender,
        stager=stager,
        prefetch_next=True,
        preflight=lambda item, **kw: (
            PreflightDecision(PreflightStatus.FAILED, "损坏图片")
            if item.path == a2.path
            else PreflightDecision(PreflightStatus.READY)
        ),
    )
    context = UploadContext(
        source_root=root,
        target=dict(TARGET),
        cancel_token=CancellationToken(),
        event_sink=sink,
        kind="image",
        metadata={
            "queue_send_policy": QueueSendPolicy("pause_on_error", True, 0.0, 0.0),
            "prefetch_next": True,
        },
    )
    result = engine.run(_StubStrategy((p1, p2)), context=context)

    assert result.paused is True
    assert result.status != "COMPLETED"
    assert sender.sent == []
    assert any(item.path == a2.path for item in result.failed_items)
    assert any(batch.album_key == "alb1" and batch.status is BatchStatus.FAILED for batch in result.batches)


def test_strict_mode_partial_preflight_deferral_stops_album_without_sending_partial(tmp_path):
    root = tmp_path / "album"
    a1 = _make_item(_seed_file(root / "01.jpg", b"a1"), root)
    a2 = _make_item(_seed_file(root / "02.jpg", b"a2"), root)
    b1 = _make_item(_seed_file(root / "03.jpg", b"b1"), root)
    p1 = _make_plan("alb1", root, (a1, a2), number=1)
    p2 = _make_plan("alb2", root, (b1,), number=2)

    sender = _FakeSender()
    stager = _FakeStager()
    engine = UploadEngine(
        sender=sender,
        stager=stager,
        prefetch_next=True,
        preflight=lambda item, **kw: (
            PreflightDecision(PreflightStatus.DEFERRED, "文件正在写入")
            if item.path == a2.path
            else PreflightDecision(PreflightStatus.READY)
        ),
    )
    context = UploadContext(
        source_root=root,
        target=dict(TARGET),
        cancel_token=CancellationToken(),
        event_sink=_RecordingSink(),
        kind="image",
        metadata={
            "queue_send_policy": QueueSendPolicy("pause_on_error", True, 0.0, 0.0),
            "prefetch_next": True,
        },
    )
    result = engine.run(_StubStrategy((p1, p2)), context=context)

    assert result.paused is True
    assert result.status != "COMPLETED"
    assert sender.sent == []
    assert any(item.path == a2.path for item in result.deferred_items)


def test_strict_mode_partial_build_failure_stops_album_and_subsequent_prefetch(tmp_path):
    root = tmp_path / "album"
    a1 = _make_item(_seed_file(root / "01.jpg", b"a1"), root)
    a2 = _make_item(_seed_file(root / "02.jpg", b"a2"), root)
    b1 = _make_item(_seed_file(root / "03.jpg", b"b1"), root)
    p1 = _make_plan("alb1", root, (a1, a2), number=1)
    p2 = _make_plan("alb2", root, (b1,), number=2)

    def builder(plan, *_args):
        if plan.key == "alb1":
            return ContentBuildResult(
                contents=(_content_for(a1),),
                ready_items=(a1,),
                failed_items=(a2,),
                errors=("解码失败",),
            )
        return (_content_for(b1),)

    sender = _FakeSender()
    stager = _FakeStager()
    engine = UploadEngine(sender=sender, stager=stager, prefetch_next=True)
    context = UploadContext(
        source_root=root,
        target=dict(TARGET),
        cancel_token=CancellationToken(),
        event_sink=_RecordingSink(),
        kind="image",
        metadata={
            "queue_send_policy": QueueSendPolicy("pause_on_error", True, 0.0, 0.0),
            "prefetch_next": True,
        },
    )
    result = engine.run(_StubStrategy((p1, p2), builder=builder), context=context)

    assert result.paused is True
    assert result.status != "COMPLETED"
    assert sender.sent == []
    assert any(item.path == a2.path for item in result.failed_items)
    assert any(batch.album_key == "alb1" and batch.status is BatchStatus.FAILED for batch in result.batches)


def test_strict_mode_source_changed_during_prefetch_cleans_up_without_send(tmp_path):
    root = tmp_path / "album"
    f_a1 = _seed_file(root / "01.jpg", b"alpha")
    f_b1 = _seed_file(root / "02.jpg", b"beta")
    a1 = _make_item(f_a1, root)
    b1 = _make_item(f_b1, root)
    p1 = _make_plan("alb1", root, (a1,), number=1)
    p2 = _make_plan("alb2", root, (b1,), number=2)

    sending_alb1 = threading.Event()
    alb2_changed = threading.Event()

    def on_send(key, contents, plan, context, prepared_media):
        if key == "alb1":
            sending_alb1.set()
            assert alb2_changed.wait(5)
        return [101]

    def builder(plan, *_args):
        if plan.key == "alb2":
            assert sending_alb1.wait(5)
            # Mutate the scanned source file during background prefetch before snapshot capture.
            f_b1.write_bytes(b"beta-mutated-on-disk")
            alb2_changed.set()
        return tuple(_content_for(item) for item in plan.pending_items)

    sender = _FakeSender(on_send=on_send)
    stager = _FakeStager()
    engine = UploadEngine(sender=sender, stager=stager, prefetch_next=True)
    context = UploadContext(
        source_root=root,
        target=dict(TARGET),
        cancel_token=CancellationToken(),
        event_sink=_RecordingSink(),
        kind="image",
        metadata={
            "queue_send_policy": QueueSendPolicy("pause_on_error", True, 0.0, 0.0),
            "prefetch_next": True,
        },
    )
    result = engine.run(_StubStrategy((p1, p2), builder=builder), context=context)

    assert [key for key, _ in sender.sent] == ["alb1"]
    assert ("alb2", False) in stager.cleaned
    assert any(item.path == f_b1 for item in result.deferred_items)
    assert result.paused is True


def test_strict_mode_pause_cleans_unconsumed_prefetch_resources_deterministically(tmp_path):
    root = tmp_path / "album"
    f_a1 = _seed_file(root / "01.jpg", b"a1")
    f_b1 = _seed_file(root / "02.jpg", b"b1")
    a1 = _make_item(f_a1, root)
    b1 = _make_item(f_b1, root)
    p1 = _make_plan("alb1", root, (a1,), number=1)
    p2 = _make_plan("alb2", root, (b1,), number=2)

    alb1_entered = threading.Event()
    alb2_prepared = threading.Event()
    remote_albums: dict[str, _FakeRemoteAlbum] = {}

    def prepare_remote(contents, context):
        path_text = str(contents[0]["photo"]["path"])
        if path_text == str(f_b1):
            assert alb1_entered.wait(5)
            remote = _FakeRemoteAlbum("alb2", contents)
            remote_albums["alb2"] = remote
            alb2_prepared.set()
            return remote
        remote = _FakeRemoteAlbum("alb1", contents)
        remote_albums["alb1"] = remote
        return remote

    def on_send(key, *_args):
        if key == "alb1":
            alb1_entered.set()
            assert alb2_prepared.wait(5)
            raise SendRequestRejected(500, "telegram network failure", None)
        pytest.fail(f"unexpected send after strict pause: {key}")

    stager = _FakeStager()
    sender = _FakeSender(on_send=on_send, prepare_remote_fn=prepare_remote)
    engine = UploadEngine(sender=sender, stager=stager, prefetch_next=True)
    context = UploadContext(
        source_root=root,
        target=dict(TARGET),
        cancel_token=CancellationToken(),
        event_sink=_RecordingSink(),
        kind="image",
        metadata={
            "queue_send_policy": QueueSendPolicy("pause_on_error", True, 0.0, 0.0),
            "prefetch_next": True,
        },
    )
    result = engine.run(_StubStrategy((p1, p2)), context=context)

    assert result.paused is True
    assert [key for key, _ in sender.sent] == []
    assert ("alb2", False) in stager.cleaned
    assert remote_albums["alb2"].closed is True


def test_strict_mode_resume_with_historical_completed_file_succeeds(tmp_path):
    root = tmp_path / "album"
    f1 = _seed_file(root / "01.jpg", b"done-already")
    f2 = _seed_file(root / "02.jpg", b"pending-1")
    f3 = _seed_file(root / "03.jpg", b"pending-2")
    a1 = _make_item(f1, root)
    a2 = _make_item(f2, root)
    b1 = _make_item(f3, root)
    p1 = _make_plan("alb1", root, (a1, a2), number=1)
    p2 = _make_plan("alb2", root, (b1,), number=2)

    state = UploadState(kind="image", source_root=root, target=dict(TARGET), state_dir=tmp_path / "state")
    state.mark_album_completed(
        [{"path": str(f1), "scan_size": a1.snapshot.size, "scan_mtime_ns": a1.snapshot.mtime_ns}],
        [55],
    )

    sender = _FakeSender()
    engine = UploadEngine(sender=sender, state=state, prefetch_next=True)
    context = UploadContext(
        source_root=root,
        target=dict(TARGET),
        cancel_token=CancellationToken(),
        event_sink=_RecordingSink(),
        kind="image",
        state=state,
        metadata={
            "queue_send_policy": QueueSendPolicy("pause_on_error", True, 0.0, 0.0),
            "prefetch_next": True,
        },
    )
    result = engine.run(_StubStrategy((p1, p2)), context=context)

    assert result.paused is False
    assert result.status == "COMPLETED"
    assert sender.sent == [("alb1", [f2]), ("alb2", [f3])]
    assert result.failed_items == ()
    assert result.deferred_items == ()


def test_strict_mode_journal_finalize_failure_after_checkpoint_pauses_and_keeps_confirmed(tmp_path):
    root = tmp_path / "album"
    f1 = _seed_file(root / "01.jpg", b"ok")
    f2 = _seed_file(root / "02.jpg", b"next")
    a1 = _make_item(f1, root)
    b1 = _make_item(f2, root)
    p1 = _make_plan("alb1", root, (a1,), number=1)
    p2 = _make_plan("alb2", root, (b1,), number=2)

    state = UploadState(kind="image", source_root=root, target=dict(TARGET), state_dir=tmp_path / "state")

    class _FailingFinalizeJournal:
        def __init__(self):
            self.prepared = []
            self.confirmed = []

        def unresolved_for_items(self, *_args, **_kwargs):
            return None

        def unresolved(self, *_args, **_kwargs):
            return None

        def prepare(self, kind, key, items=None, **kwargs):
            self.prepared.append(key)

        def submitted(self, *_args, **_kwargs):
            pass

        def mark_confirmed(self, kind, key, ids=None, **kwargs):
            self.confirmed.append(key)

        def finalize(self, *_args, **_kwargs):
            raise OSError("disk full while finalizing journal")

        def failed(self, *_args, **_kwargs):
            pass

        def unknown(self, *_args, **_kwargs):
            pass

    journal = _FailingFinalizeJournal()
    sender = _FakeSender()
    engine = UploadEngine(sender=sender, state=state, journal=journal, prefetch_next=False)
    context = UploadContext(
        source_root=root,
        target=dict(TARGET),
        cancel_token=CancellationToken(),
        event_sink=_RecordingSink(),
        kind="image",
        state=state,
        journal=journal,
        metadata={
            "queue_send_policy": QueueSendPolicy("pause_on_error", True, 0.0, 0.0),
            "prefetch_next": False,
        },
    )
    result = engine.run(_StubStrategy((p1, p2)), context=context)

    assert result.paused is True
    assert [key for key, _ in sender.sent] == ["alb1"]
    assert state.is_completed(f1) is True


def test_skip_and_retry_tail_retry_bounds_unknown_pause_and_no_build_retry(tmp_path):
    root = tmp_path / "album"
    f1 = _seed_file(root / "01.jpg", b"a1")
    f2 = _seed_file(root / "02.jpg", b"b1")
    a1 = _make_item(f1, root)
    b1 = _make_item(f2, root)
    p1 = _make_plan("alb1", root, (a1,), number=1)
    p2 = _make_plan("alb2", root, (b1,), number=2)

    # 1. Definite Telegram send failure retries at tail and succeeds without breaking strict budget.
    attempts = {"alb1": 0}

    def retry_once_sender(key, contents, *_args):
        if key == "alb1":
            attempts["alb1"] += 1
            if attempts["alb1"] == 1:
                raise SendRequestRejected(500, "transient telegram send error", None)
        return [200]

    sender1 = _FakeSender(on_send=retry_once_sender)
    ctx1 = UploadContext(
        source_root=root,
        target=dict(TARGET),
        cancel_token=CancellationToken(),
        event_sink=_RecordingSink(),
        kind="image",
        metadata={
            "queue_send_policy": QueueSendPolicy("skip_and_retry", True, 0.0, 0.0),
            "album_retry": AlbumRetryPolicy(enabled=True, max_retries=1, delay_seconds=0.0),
            "prefetch_next": False,
        },
    )
    res1 = UploadEngine(sender=sender1, prefetch_next=False).run(_StubStrategy((p1, p2)), context=ctx1)
    assert res1.paused is False
    assert res1.status == "COMPLETED"
    assert attempts["alb1"] == 2
    assert [key for key, _ in sender1.sent] == ["alb2", "alb1"]

    # 1b. Persistent failure obeys max_retries budget cleanly without infinite retry.
    capped = {"alb1": 0}

    def always_reject_alb1(key, contents, *_args):
        if key == "alb1":
            capped["alb1"] += 1
            raise SendRequestRejected(500, "persistent failure", None)
        return [200]

    sender_cap = _FakeSender(on_send=always_reject_alb1)
    res_cap = UploadEngine(sender=sender_cap, prefetch_next=False).run(_StubStrategy((p1, p2)), context=ctx1)
    assert res_cap.paused is False
    assert capped["alb1"] == 2
    assert [key for key, _ in sender_cap.sent] == ["alb2"]

    # 2. Permanent build error is NOT tail-retried in skip_and_retry mode.
    build_calls = {"alb1": 0}

    def failing_build(plan, *_args):
        if plan.key == "alb1":
            build_calls["alb1"] += 1
            return ContentBuildResult(failed_items=plan.pending_items, errors=("bad EXIF",))
        return (_content_for(b1),)

    sender2 = _FakeSender()
    ctx2 = UploadContext(
        source_root=root,
        target=dict(TARGET),
        cancel_token=CancellationToken(),
        event_sink=_RecordingSink(),
        kind="image",
        metadata={
            "queue_send_policy": QueueSendPolicy("skip_and_retry", True, 0.0, 0.0),
            "album_retry": AlbumRetryPolicy(enabled=True, max_retries=2, delay_seconds=0.0),
            "prefetch_next": False,
        },
    )
    res2 = UploadEngine(sender=sender2, prefetch_next=False).run(
        _StubStrategy((p1, p2), builder=failing_build), context=ctx2
    )
    assert build_calls["alb1"] == 1
    assert [key for key, _ in sender2.sent] == ["alb2"]
    assert res2.paused is False

    # 3. UNKNOWN send result respects pause_on_unknown=True and prevents blind auto-retry.
    def timeout_sender(key, *_args):
        if key == "alb1":
            raise TimeoutError("timed out awaiting confirmation")
        return [201]

    sender3 = _FakeSender(on_send=timeout_sender)
    ctx3 = UploadContext(
        source_root=root,
        target=dict(TARGET),
        cancel_token=CancellationToken(),
        event_sink=_RecordingSink(),
        kind="image",
        metadata={
            "queue_send_policy": QueueSendPolicy("skip_and_retry", True, 0.0, 0.0),
            "album_retry": AlbumRetryPolicy(enabled=True, max_retries=2, delay_seconds=0.0),
            "prefetch_next": False,
        },
    )
    res3 = UploadEngine(sender=sender3, prefetch_next=False).run(_StubStrategy((p1, p2)), context=ctx3)
    assert res3.paused is True
    assert sender3.sent == []

    # 4. Partial success in a multi-item Album must NOT be blindly retried at tail (even with pause_on_unknown=False).
    f1b = _seed_file(root / "01b.jpg", b"a1b")
    a1b = _make_item(f1b, root)
    p1_two = _make_plan("alb1_two", root, (a1, a1b), number=1)
    partial_calls = {"alb1_two": 0}

    def partial_sender(key, *_args):
        if key == "alb1_two":
            partial_calls["alb1_two"] += 1
            return {
                "status": "PARTIAL",
                "message_ids": [301, 302],
                "succeeded_ids": [301],
                "failed_ids": [302],
                "item_results": [
                    {"index": 0, "message_id": 301, "status": "succeeded"},
                    {"index": 1, "message_id": 302, "status": "failed", "error_code": 500, "retry_after": 0.0, "requires_action": False},
                ],
            }
        return [303]

    sender4 = _FakeSender(on_send=partial_sender)
    ctx4 = UploadContext(
        source_root=root,
        target=dict(TARGET),
        cancel_token=CancellationToken(),
        event_sink=_RecordingSink(),
        kind="image",
        metadata={
            "queue_send_policy": QueueSendPolicy("skip_and_retry", False, 0.0, 0.0),
            "album_retry": AlbumRetryPolicy(enabled=True, max_retries=2, delay_seconds=0.0),
            "prefetch_next": False,
        },
    )
    res4 = UploadEngine(sender=sender4, prefetch_next=False).run(_StubStrategy((p1_two, p2)), context=ctx4)
    assert partial_calls["alb1_two"] == 1
    assert res4.paused is False

    # 5. Pre-uploaded remote file reference rejection falls back cleanly to local send.
    remote = _FakeRemoteAlbum("alb1", (_content_for(a1),))
    ref_attempts = {"count": 0}

    def ref_fallback_sender(key, contents, *_args):
        ref_attempts["count"] += 1
        if ref_attempts["count"] == 1:
            raise SendRequestRejected(400, "FILE_REFERENCE_EXPIRED", None)
        return [401]

    sender5 = _FakeSender(
        on_send=ref_fallback_sender,
        prepare_remote_fn=lambda contents, ctx: remote,
    )
    res5 = UploadEngine(sender=sender5, prefetch_next=False).run(_StubStrategy((p1,)), context=ctx1)
    assert ref_attempts["count"] == 2
    assert remote.closed is True
    assert res5.status == "COMPLETED"


# ---------------------------------------------------------------------------
# Manual Reconciliation: target / snapshot / source_root / partial / persistence failure
# ---------------------------------------------------------------------------


def test_reconciliation_sync_rejects_mismatched_target_source_and_changed_files(tmp_path, qt_app):
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    f1 = _seed_file(root_a / "01.jpg", b"img-1")
    item1 = _make_item(f1, root_a)
    plan1 = _make_plan("alb1", root_a, (item1,), target=TARGET)
    snap1 = item1.snapshot

    target_b = {**TARGET, "chat_id": -100999888777}
    page = _make_upload_page("image", lambda _k: dict(TARGET), lambda _k: root_a)
    page.set_result(_preview_result_for(root_a, TARGET, (plan1,)))

    bad_target = ReconciledAlbum(
        source_root=root_a, target=target_b, snapshots=(snap1,), kind="image",
        album_key="alb1", sent=True, confirmed_snapshots=(snap1,), syncable=True,
    )
    synced, _ = page.sync_reconciled_album(bad_target)
    assert synced is False
    assert page.result["completed_paths"] == []

    bad_root = ReconciledAlbum(
        source_root=root_b, target=TARGET, snapshots=(snap1,), kind="image",
        album_key="alb1", sent=True, confirmed_snapshots=(snap1,), syncable=True,
    )
    synced, _ = page.sync_reconciled_album(bad_root)
    assert synced is False
    assert page.result["completed_paths"] == []

    stale_snap = FileSnapshot(snap1.path, snap1.size + 10, snap1.mtime_ns)
    synced, msg = page.sync_reconciled_album(
        ReconciledAlbum(
            source_root=root_a, target=TARGET, snapshots=(stale_snap,), kind="image",
            album_key="alb1", sent=True, confirmed_snapshots=(stale_snap,), syncable=True,
        )
    )
    assert synced is False and "快照" in msg
    assert page.result["completed_paths"] == []

    # Now mutate the file on disk while preview still holds old size/mtime_ns.
    f1.write_bytes(b"img-1-changed-size")
    disk_mismatch = ReconciledAlbum(
        source_root=root_a, target=TARGET, snapshots=(snap1,), kind="image",
        album_key="alb1", sent=True, confirmed_snapshots=(snap1,), syncable=True,
    )
    synced, msg = page.sync_reconciled_album(disk_mismatch)
    assert synced is False and "变化" in msg
    assert page.result["completed_paths"] == []


def test_reconciliation_legacy_missing_target_or_snapshots_keeps_persistence_and_prompts_rescan(
    tmp_path, qt_app, monkeypatch
):
    root = tmp_path / "src"
    f1 = _seed_file(root / "01.jpg", b"legacy-1")
    item1 = _make_item(f1, root)
    plan1 = _make_plan("alb_no_target", root, (item1,))
    state_dir = tmp_path / "state"
    monkeypatch.setattr(image_pipeline, "STATE_DIR", state_dir)

    page = _make_upload_page("image", lambda _k: dict(TARGET), lambda _k: root)
    page.set_result(_preview_result_for(root, TARGET, (plan1,)))

    # 1. Legacy record without Telegram target (`sent=False`)
    journal = InflightJournal(tmp_path / "journal_no_target")
    legacy_path = journal.path_for("image", "alb_no_target", None)
    legacy_path.write_text(
        json.dumps(
            {
                "kind": "image",
                "album_key": "alb_no_target",
                "status": "UNKNOWN",
                "source_root": str(root),
                "items": [
                    {
                        "path": str(f1),
                        "source_root": str(root),
                        "scan_size": item1.snapshot.size,
                        "scan_mtime_ns": item1.snapshot.mtime_ns,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    service = ReconciliationService(journal)
    confirmation = service.reconcile_inflight("alb_no_target", sent=False, kind="image")
    assert confirmation.syncable is False
    assert confirmation.target == {}
    assert journal.unresolved("image", "alb_no_target") is None  # persistent reconciliation saved
    synced, reason = page.sync_reconciled_album(confirmation)
    assert synced is False and "重新扫描" in reason
    assert page.result["completed_paths"] == []

    # 2. Legacy record without file snapshots (`sent=False`)
    journal2 = InflightJournal(tmp_path / "journal_no_snaps")
    legacy_path2 = journal2.path_for("image", "alb_no_snaps", TARGET)
    legacy_path2.write_text(
        json.dumps(
            {
                "kind": "image",
                "album_key": "alb_no_snaps",
                "status": "UNKNOWN",
                "source_root": str(root),
                "target": dict(TARGET),
                "items": [],
            }
        ),
        encoding="utf-8",
    )
    confirmation2 = ReconciliationService(journal2).reconcile_inflight(
        "alb_no_snaps", sent=False, kind="image", target=dict(TARGET)
    )
    assert confirmation2.syncable is False
    assert confirmation2.snapshots == ()
    assert journal2.unresolved("image", "alb_no_snaps", TARGET) is None
    synced2, reason2 = page.sync_reconciled_album(confirmation2)
    assert synced2 is False and reason2


def test_reconciliation_partial_success_restores_only_unconfirmed_and_keeps_other_unknown(
    tmp_path, qt_app, monkeypatch
):
    root = tmp_path / "src"
    f1 = _seed_file(root / "01.jpg", b"ok-1")
    f2 = _seed_file(root / "02.jpg", b"fail-2")
    f3 = _seed_file(root / "03.jpg", b"other-3")
    i1, i2, i3 = _make_item(f1, root), _make_item(f2, root), _make_item(f3, root)
    p1 = _make_plan("alb_partial", root, (i1, i2), number=1)
    p2 = _make_plan("alb_other", root, (i3,), number=2)

    state_dir = tmp_path / "state"
    monkeypatch.setattr(image_pipeline, "STATE_DIR", state_dir)

    journal = InflightJournal(tmp_path / "journal")
    item_payload = [
        {"path": str(f1), "source_root": str(root), "scan_size": i1.snapshot.size, "scan_mtime_ns": i1.snapshot.mtime_ns},
        {"path": str(f2), "source_root": str(root), "scan_size": i2.snapshot.size, "scan_mtime_ns": i2.snapshot.mtime_ns},
    ]
    journal.prepare("image", "alb_partial", item_payload, target=dict(TARGET))
    journal.update(
        "image",
        "alb_partial",
        "UNKNOWN",
        message_ids=[701, 702],
        succeeded_ids=[701],
        failed_ids=[702],
        item_results=[
            {"index": 0, "path": str(f1), "message_id": 701, "status": "succeeded"},
            {"index": 1, "path": str(f2), "message_id": 702, "status": "failed", "error": "send failed"},
        ],
        target=dict(TARGET),
    )
    # Unrelated Album under protection must NOT be cleared.
    journal.prepare(
        "image",
        "alb_other",
        [{"path": str(f3), "source_root": str(root), "scan_size": i3.snapshot.size, "scan_mtime_ns": i3.snapshot.mtime_ns}],
        target=dict(TARGET),
    )
    journal.unknown("image", "alb_other", error="unknown", target=dict(TARGET))

    page = _make_upload_page("image", lambda _k: dict(TARGET), lambda _k: root)
    page.set_result(_preview_result_for(root, TARGET, (p1, p2)))
    task = UploadTask(
        "t1", "image", root, dict(TARGET), {}, (p1, p2), "2026-01-01T00:00:00Z",
        status=TaskStatus.NEEDS_REVIEW,
        album_outcomes=(
            AlbumOutcome("alb_partial", AlbumStatus.UNKNOWN, error="needs check"),
            AlbumOutcome("alb_other", AlbumStatus.UNKNOWN, error="needs check"),
        ),
    )
    page.sync_task_outcomes(task)
    assert page._album_delivery["alb_partial"] == "待核对"
    assert page._album_delivery["alb_other"] == "待核对"

    confirmation = ReconciliationService(journal).reconcile_inflight(
        "alb_partial", sent=False, kind="image", target=dict(TARGET)
    )
    assert confirmation.syncable is True
    assert [snap.path for snap in confirmation.confirmed_snapshots] == [stable_path(f1)]
    assert [snap.path for snap in confirmation.reupload_snapshots] == [stable_path(f2)]

    synced, _ = page.sync_reconciled_album(confirmation)
    assert synced is True
    assert page._file_delivery[stable_path(f1)] == "已完成"
    assert page._file_delivery[stable_path(f2)] == "待上传"
    assert page._album_delivery["alb_partial"] == "部分完成"
    assert page._album_delivery["alb_other"] == "待核对"
    assert journal.unresolved("image", "alb_other", TARGET) is not None
    state = UploadState(kind="image", source_root=root, target=dict(TARGET), state_dir=state_dir)
    assert state.is_completed(f1) is True
    assert state.is_completed(f2) is False


def test_reconciliation_persistence_failure_does_not_update_gui_completion(
    isolated_window, tmp_path, monkeypatch
):
    win, _cfg, _prefs = isolated_window
    root = tmp_path / "src"
    f1 = _seed_file(root / "01.jpg", b"persist-fail")
    i1 = _make_item(f1, root)
    p1 = _make_plan("alb_persist", root, (i1,), number=1)

    monkeypatch.setattr(main_window, "_source_root_for", lambda _k: root)
    page = win._upload_page("image")
    page.set_result(_preview_result_for(root, TARGET, (p1,)))

    journal = InflightJournal(paths.UPLOAD_INFLIGHT_DIR)
    record = journal.prepare(
        "image",
        "alb_persist",
        [{"path": str(f1), "source_root": str(root), "scan_size": i1.snapshot.size, "scan_mtime_ns": i1.snapshot.mtime_ns}],
        target=dict(TARGET),
    )
    journal.unknown("image", "alb_persist", error="unknown", target=dict(TARGET))

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text, *a: warnings.append((title, text)))

    class _FailingService:
        def reconcile_inflight(self, *args, **kwargs):
            raise OSError("sqlite write failure")

    monkeypatch.setattr(reconciliation, "ReconciliationService", _FailingService)
    win._reconcile_inflight(record, True)

    assert warnings and "人工处理未完成" in warnings[0][0]
    assert page.result["completed_paths"] == []
    assert page._album_delivery["alb_persist"] == "待上传"
    assert journal.unresolved("image", "alb_persist", TARGET) is not None


# ---------------------------------------------------------------------------
# GUI Preview Skip / Stats / 3-Button Dialog & SQLite Queue Cleanup
# ---------------------------------------------------------------------------


def test_preview_skip_restore_preserves_statuses_filters_and_five_summary_scenarios(tmp_path, qt_app):
    root = tmp_path / "gallery"
    files = [_seed_file(root / f"0{i}.jpg", f"file-{i}".encode()) for i in range(1, 7)]
    items = tuple(_make_item(path, root) for path in files)
    p_fail = _make_plan("alb_fail", root, (items[0],), number=1)
    p_defer = _make_plan("alb_defer", root, (items[1],), number=2)
    p_review = _make_plan("alb_review", root, (items[2],), number=3)
    p_partial = _make_plan("alb_partial", root, (items[3], items[4]), number=4)
    p_done = _make_plan("alb_done", root, (items[5],), number=5)

    journal = InflightJournal(tmp_path / "journal")
    journal.prepare("image", "alb_review", [{"path": str(files[2]), "source_root": str(root), "scan_size": items[2].snapshot.size, "scan_mtime_ns": items[2].snapshot.mtime_ns}], target=dict(TARGET))
    journal.unknown("image", "alb_review", error="check telegram", target=dict(TARGET))

    page = _make_upload_page("image", lambda _k: dict(TARGET), lambda _k: root)
    page.set_result(
        _preview_result_for(
            root,
            TARGET,
            (p_fail, p_defer, p_review, p_partial, p_done),
            completed_paths=(stable_path(files[3]), stable_path(files[5])),
        )
    )
    task = UploadTask(
        "task-outcomes",
        "image",
        root,
        dict(TARGET),
        {},
        (p_fail, p_defer, p_review, p_partial, p_done),
        "2026-01-01T00:00:00Z",
        status=TaskStatus.NEEDS_REVIEW,
        confirmed_paths=(stable_path(files[3]), stable_path(files[5])),
        album_outcomes=(
            AlbumOutcome("alb_fail", AlbumStatus.FAILED, error="bad image", failed_paths=(stable_path(files[0]),)),
            AlbumOutcome("alb_defer", AlbumStatus.DEFERRED, error="file changed", deferred_paths=(stable_path(files[1]),)),
            AlbumOutcome("alb_review", AlbumStatus.UNKNOWN, error="needs check"),
            AlbumOutcome("alb_partial", AlbumStatus.PARTIAL, error="one failed", failed_paths=(stable_path(files[4]),)),
            AlbumOutcome("alb_done", AlbumStatus.COMPLETED),
        ),
    )
    page.sync_task_outcomes(task)

    assert page._album_delivery["alb_fail"] == "失败"
    assert page._album_delivery["alb_defer"] == "暂缓"
    assert page._album_delivery["alb_review"] == "待核对"
    assert page._album_delivery["alb_partial"] == "部分完成"
    assert page._album_delivery["alb_done"] == "已完成"
    assert "待核对记录" in page.status_label.text()

    model = page._preview_model
    proxy = page._preview_filter

    def select_album(key: str):
        row = page._album_rows[key]
        src_idx = model.index(row.parent.children.index(row), 0)
        page.tree.setCurrentIndex(proxy.mapFromSource(src_idx))

    # Skip all four non-completed Albums (including 部分完成 & 待核对).
    for key in ("alb_fail", "alb_defer", "alb_review", "alb_partial"):
        select_album(key)
        assert page.toggle_skip_button.isEnabled()
        page.toggle_skip_button.click()

    assert page._album_rows["alb_fail"].values[0] == "失败 · 已排除"
    assert page._album_rows["alb_defer"].values[0] == "暂缓 · 已排除"
    assert page._album_rows["alb_review"].values[0] == "待核对 · 已排除"
    assert page._album_rows["alb_partial"].values[0] == "部分完成 · 已排除"
    assert page._album_rows["alb_fail"].excluded is True
    assert page._album_rows["alb_fail"].delivery_status == "失败"
    # Completed leaf inside 部分完成 remains "已完成", while unfinished leaf reflects exclusion.
    assert page._file_rows[stable_path(files[3])].values[0] == "已完成"
    assert page._file_rows[stable_path(files[3])].excluded is False
    assert page._file_rows[stable_path(files[4])].values[0] == "失败 · 已排除"

    # All unfinished Albums are skipped -> pending=0, start disabled, restore still enabled.
    assert page.result["pending_files"] == 0
    assert page.result["pending_bytes"] == 0
    assert page.result["skipped_albums"] == 4
    assert page.result["skipped_files"] == 4
    assert page.result["album_count"] == 0
    assert page.result["completed_files"] == 2
    assert not page.start_button.isEnabled()
    select_album("alb_review")
    assert page.toggle_skip_button.isEnabled() and page.toggle_skip_button.text() == "恢复该组"

    # Search + pending_only share unified PreviewRow state.
    page.pending_only.setChecked(True)
    assert proxy.rowCount() == 0
    page.pending_only.setChecked(False)

    # Restore all 4 Albums and verify exact original delivery statuses + tooltips return cleanly.
    for key in ("alb_fail", "alb_defer", "alb_review", "alb_partial"):
        select_album(key)
        page.toggle_skip_button.click()

    assert page._album_rows["alb_fail"].values[0] == "失败"
    assert page._album_rows["alb_defer"].values[0] == "暂缓"
    assert page._album_rows["alb_review"].values[0] == "待核对"
    assert page._album_rows["alb_partial"].values[0] == "部分完成"
    assert "bad image" in page._album_rows["alb_fail"].tooltips[0]
    assert journal.unresolved("image", "alb_review", TARGET) is not None

    # Verify remaining preview summary scenes cleanly:
    # A) Partial skip summary
    select_album("alb_fail")
    page.toggle_skip_button.click()
    # Clear 待核对 by completing review on page via standalone empty page for the clean 5-scenario check.
    page2 = _make_upload_page("image", lambda _k: dict(TARGET), lambda _k: root)
    p_a = _make_plan("pa", root, (items[0],), number=1)
    p_b = _make_plan("pb", root, (items[1],), number=2)
    page2.set_result(_preview_result_for(root, TARGET, (p_a, p_b)))
    idx_pa = page2._preview_filter.mapFromSource(
        page2._preview_model.index(page2._album_rows["pa"].parent.children.index(page2._album_rows["pa"]), 0)
    )
    page2.tree.setCurrentIndex(idx_pa)
    page2.toggle_skip_button.click()
    assert page2.status_label.text().startswith("待上传 1 个") and "已跳过 1 个" in page2.status_label.text()
    idx_pb = page2._preview_filter.mapFromSource(
        page2._preview_model.index(page2._album_rows["pb"].parent.children.index(page2._album_rows["pb"]), 0)
    )
    page2.tree.setCurrentIndex(idx_pb)
    page2.toggle_skip_button.click()
    assert page2.status_label.text() == "所有待上传 Album 已跳过"
    assert not page2.start_button.isEnabled()

    page2.set_result(
        _preview_result_for(
            root, TARGET, (p_a, p_b),
            completed_paths=(stable_path(files[0]), stable_path(files[1])),
        )
    )
    assert page2.status_label.text() == "全部项目已上传"

    page2.set_result(_preview_result_for(root, TARGET, ()))
    assert "未找到可上传媒体" in page2.status_label.text()


def test_three_button_enqueue_dialog_and_frozen_task_plan(isolated_window, tmp_path, monkeypatch):
    win, _cfg, _prefs = isolated_window
    root = tmp_path / "src"
    f1 = _seed_file(root / "01.jpg", b"one")
    f2 = _seed_file(root / "02.jpg", b"two")
    i1, i2 = _make_item(f1, root), _make_item(f2, root)
    p1 = _make_plan("alb1", root, (i1,), number=1)
    p2 = _make_plan("alb2", root, (i2,), number=2)

    page = win._upload_page("image")
    page.set_result(_preview_result_for(root, TARGET, (p1, p2)))

    choices = iter(["取消", "加入并继续扫描", "加入并前往任务中心"])

    def fake_exec(box):
        target_text = next(choices)
        for btn in box.buttons():
            if btn.text() == target_text:
                box._clicked = btn
                return 0
        pytest.fail(f"missing button {target_text}")

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: getattr(self, "_clicked", None))

    # 1. Cancel does not enqueue.
    win.sidebar.setCurrentRow(win._sidebar_row("image"))
    win._start_upload("image")
    assert win.queue.store.tasks == ()

    # 2. Enqueue and continue scanning stays on upload page.
    win._start_upload("image")
    assert len(win.queue.store.tasks) == 1
    assert win.sidebar.currentRow() == win._sidebar_row("image")
    assert "继续扫描" in win.statusBar().currentMessage()

    # 3. Enqueue and open task center switches sidebar and selects the task.
    root_b = tmp_path / "src_b"
    fb1 = _seed_file(root_b / "b1.jpg", b"three")
    pb1 = _make_plan("alb_b", root_b, (_make_item(fb1, root_b),), number=1)
    page.set_result(_preview_result_for(root_b, TARGET, (pb1,)))
    win._start_upload("image")
    assert len(win.queue.store.tasks) == 2
    assert win.sidebar.currentRow() == win.sidebar_rows["task"]
    assert win.task_page.queue_list.currentItem().data(0, Qt.ItemDataRole.UserRole) == win.queue.store.tasks[1].task_id

    # Skipping an Album in preview afterwards must NOT mutate already-enqueued tasks.
    frozen_snapshot = [t.to_record() for t in win.queue.store.tasks]
    row = page._album_rows["alb_b"]
    src_idx = page._preview_model.index(row.parent.children.index(row), 0)
    page.tree.setCurrentIndex(page._preview_filter.mapFromSource(src_idx))
    page.toggle_skip_button.click()
    assert page.result["skipped_album_keys"] == ("alb_b",)
    assert [t.to_record() for t in win.queue.store.tasks] == frozen_snapshot


def test_clear_finished_sqlite_persistence_and_receipts_across_restart(tmp_path, runtime_paths, monkeypatch):
    app_data_dir = tmp_path / "app_data"
    monkeypatch.setattr(paths, "DATA_DIR", app_data_dir)
    db_path = app_data_dir / "uploader.db"
    db = database_for(db_path)
    queue_path = app_data_dir / "upload_queue.json"
    queue = UploadQueue(queue_path, runtime_paths=runtime_paths, database=db)

    t_completed = task_for(tmp_path / "completed")
    t_skipped = task_for(tmp_path / "skipped")
    t_failed = task_for(tmp_path / "failed")
    t_interrupted = task_for(tmp_path / "interrupted")
    t_review = task_for(tmp_path / "review")
    t_pending = task_for(tmp_path / "pending")

    for t in (t_completed, t_skipped):
        queue.enqueue(t)

    queue.finish(queue.start(queue.prepare(t_completed)), UploadRunResult("COMPLETED"))
    queue.finish(queue.start(queue.prepare(t_skipped)), UploadRunResult("FAILED"))
    queue.skip(t_skipped.task_id)

    t_failed_done = finish_task(replace(t_failed, status=TaskStatus.RUNNING, started_at=t_failed.created_at), UploadRunResult("FAILED"))
    t_interrupted_done = finish_task(replace(t_interrupted, status=TaskStatus.RUNNING, started_at=t_interrupted.created_at), UploadRunResult("CANCELLED", cancelled=True))
    t_review_done = finish_task(replace(t_review, status=TaskStatus.RUNNING, started_at=t_review.created_at), UploadRunResult("UNKNOWN"))
    queue._commit(
        (*queue.tasks, t_failed_done, t_interrupted_done, t_review_done, t_pending),
        True,
    )

    # Persist queue_album_receipts rows for cleared vs preserved tasks directly in SQLite.
    with db.write() as conn:
        for t in (t_completed, t_skipped, t_failed, t_review):
            conn.execute(
                "INSERT INTO queue_album_receipts (task_id, album_key, paths) VALUES (?, ?, ?)",
                (t.task_id, t.plans[0].key, json.dumps(["/tmp/f.jpg"])),
            )

    removed = queue.clear_finished()
    assert removed == 2
    assert [t.status for t in queue.tasks] == [
        TaskStatus.FAILED,
        TaskStatus.INTERRUPTED,
        TaskStatus.NEEDS_REVIEW,
        TaskStatus.PENDING,
    ]

    # Reopen from SQLite cleanly to simulate app restart.
    db2 = database_for(db_path)
    reopened = UploadQueue(queue_path, runtime_paths=runtime_paths, database=db2)
    assert [t.task_id for t in reopened.tasks] == [
        t_failed.task_id,
        t_interrupted.task_id,
        t_review.task_id,
        t_pending.task_id,
    ]
    assert reopened.paused is True
    with db2.read() as conn:
        receipt_task_ids = {
            row[0] for row in conn.execute("SELECT task_id FROM queue_album_receipts").fetchall()
        }
    assert receipt_task_ids == {t_failed.task_id, t_review.task_id}


def test_task_page_switching_historical_and_active_task_shows_matching_album_files(tmp_path, qt_app):
    root_a = tmp_path / "active"
    root_h = tmp_path / "historical"
    f_a1 = _seed_file(root_a / "live_1.jpg", b"live1")
    f_a2 = _seed_file(root_a / "live_2.jpg", b"live2")
    f_h1 = _seed_file(root_h / "hist_1.jpg", b"hist1")
    i_a1, i_a2 = _make_item(f_a1, root_a), _make_item(f_a2, root_a)
    i_h1 = _make_item(f_h1, root_h)
    pa1 = _make_plan("active_alb_1", root_a, (i_a1,), number=1)
    pa2 = _make_plan("active_alb_2", root_a, (i_a2,), number=2)
    ph1 = _make_plan("hist_alb_1", root_h, (i_h1,), number=1)

    active_task = UploadTask(
        "t-active", "image", root_a, dict(TARGET), {}, (pa1, pa2), "2026-01-01T00:00:00Z",
        status=TaskStatus.RUNNING,
    )
    hist_task = UploadTask(
        "t-hist", "image", root_h, dict(TARGET), {}, (ph1,), "2026-01-01T00:00:00Z",
        status=TaskStatus.FAILED,
        album_outcomes=(AlbumOutcome("hist_alb_1", AlbumStatus.FAILED, failed_paths=(stable_path(f_h1),)),),
    )

    class _StoreStub:
        tasks = (active_task, hist_task)
        paused = False
        error = None
        blocked_reason = ""

    page = TaskPage()
    page.show_queue(_StoreStub())
    page.show_album({"album_key": "active_alb_1", "title": "A1", "subtitle": "uploading", "rows": ["LIVE ACTIVE ROW"]})
    assert [page.album_files.item(i).text() for i in range(page.album_files.count())] == ["LIVE ACTIVE ROW"]

    # Switch to historical task and select its Album: must display hist_1.jpg, not live row.
    page.select_task("t-hist")
    page.albums.setCurrentItem(page.albums.topLevelItem(0))
    hist_rows = [page.album_files.item(i).text() for i in range(page.album_files.count())]
    assert len(hist_rows) == 1 and "hist_1.jpg" in hist_rows[0]

    # Incoming live progress event must NOT overwrite selected historical task view.
    page.show_album({"album_key": "active_alb_1", "title": "A1", "subtitle": "uploading", "rows": ["LIVE ROW 2"]})
    hist_rows_after = [page.album_files.item(i).text() for i in range(page.album_files.count())]
    assert hist_rows_after == hist_rows

    # Switch back to active task: live album shows LIVE ROW 2, other album shows live_2.jpg.
    page.select_task("t-active")
    assert [page.album_files.item(i).text() for i in range(page.album_files.count())] == ["LIVE ROW 2"]
    page.albums.setCurrentItem(page.albums.topLevelItem(1))
    other_active_rows = [page.album_files.item(i).text() for i in range(page.album_files.count())]
    assert len(other_active_rows) == 1 and "live_2.jpg" in other_active_rows[0]


# ---------------------------------------------------------------------------
# Telegram Target Label Cache: rollback, retry, theme safety, multi-page sync
# ---------------------------------------------------------------------------


def test_target_label_cache_rollback_retry_theme_and_cross_page_refresh(
    isolated_window, tmp_path, monkeypatch
):
    win, _cfg, prefs = isolated_window
    log_path = app_logging.APP_LOG_PATH
    prefs.save_theme("light")

    target_a = {"target_mode": "forum_topic", "chat_id": -100111222333, "forum_topic_id": 42, "channel_chat_id": 0}
    target_b = {"target_mode": "forum_topic", "chat_id": -100444555666, "forum_topic_id": 99, "channel_chat_id": 0}

    # Route video + mixed to target_a, image to target_b across upload pages & worker lookup.
    per_kind_targets = {"video": dict(target_a), "mixed": dict(target_a), "image": dict(target_b)}
    monkeypatch.setattr(main_window, "_target_for", lambda k: dict(per_kind_targets[k]))
    for kind, p in win.upload_pages.items():
        p.services = replace(p.services, target_getter=lambda k: dict(per_kind_targets[k]))
        p.refresh_target_label()

    task_a = replace(task_for(tmp_path / "t-a"), task_id="task-a", target=dict(target_a))
    win.queue.store.enqueue(task_a)
    win._refresh_queue()

    # 1. Fail writing NEW entry: rolls back in-memory cache, logs WARNING, preserves theme.
    real_write = preferences.atomic_write_json

    def fail_write(*_args, **_kwargs):
        raise PermissionError("disk read-only")

    monkeypatch.setattr(preferences, "atomic_write_json", fail_write)
    saved = prefs.save_target_label(target_a, chat_title="New Group A", topic_name="Topic 42")
    assert saved is False
    assert prefs.get_target_label(target_a) == {}
    assert prefs.theme_mode == "light"
    assert UiPreferences(prefs.path).theme_mode == "light"
    assert log_path.exists() and "WARNING" in log_path.read_text(encoding="utf-8")

    # 2. Same label can be saved later when write succeeds.
    monkeypatch.setattr(preferences, "atomic_write_json", real_write)
    assert prefs.save_target_label(target_a, chat_title="Old Group A", topic_name="Old Topic") is True

    # 3. Updating existing entry with write failure restores the PREVIOUS cached value.
    monkeypatch.setattr(preferences, "atomic_write_json", fail_write)
    win._target_from_worker(
        {
            "target_mode": "forum_topic",
            "chat_id": target_a["chat_id"],
            "topic_id": target_a["forum_topic_id"],
            "chat_title": "Broken Update",
            "topic_name": "Broken Topic",
        }
    )
    assert prefs.get_target_label(target_a) == {"chat_title": "Old Group A", "topic_name": "Old Topic"}

    # 4. Retry saving updated label succeeds and refreshes ALL matching pages + task center row.
    monkeypatch.setattr(preferences, "atomic_write_json", real_write)
    win._target_from_worker(
        {
            "target_mode": "forum_topic",
            "chat_id": target_a["chat_id"],
            "topic_id": target_a["forum_topic_id"],
            "chat_title": "Confirmed Group A",
            "topic_name": "Confirmed Topic",
        }
    )
    assert "Confirmed Group A" in win.video_page.chat_label.text()
    assert "Confirmed Group A" in win.mixed_page.chat_label.text()
    assert win.video_page.topic_label.text() == "Confirmed Topic"
    assert win.mixed_page.topic_label.text() == "Confirmed Topic"
    assert "Confirmed Group A" not in win.image_page.chat_label.text()
    row_text = win.task_page.queue_list.topLevelItem(0).text(4)
    assert "Confirmed Group A" in row_text and "Confirmed Topic" in row_text
    reloaded = UiPreferences(prefs.path)
    assert reloaded.theme_mode == "light"
    assert reloaded.get_target_label(target_a) == {"chat_title": "Confirmed Group A", "topic_name": "Confirmed Topic"}
