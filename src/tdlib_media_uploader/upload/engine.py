"""Shared upload lifecycle for image, mixed and video strategies.

The engine owns the generic lifecycle shared by image, mixed and video
strategies: state filtering, Album-boundary preservation, preflight, optional
staging, journal transitions, transport invocation and durable checkpointing.
Media-specific code is injected through :class:`MediaStrategy` and the small
collaborator protocols below. The GUI injects durable state, journal and
TDLib adapters; in-memory stores support standalone engine use and tests.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, wait
from dataclasses import dataclass, field, replace
import hashlib
from itertools import chain
import math
import time
from uuid import uuid4
import json
from pathlib import Path
import threading
from typing import Any, Protocol

from ..core.concurrency import CancellationRequested, UploadPreparationExecutor, cancelable_sleep
from ..core.filesystem import io_worker_count
from ..core.compat import call_supported as _call_supported
from ..telegram.send_result import SendRequestRejected
from .delivery import _as_ids, _ObservedSend, _normalize_send_result, _normalize_status
from ..config.paths import VIDEO_PROCESSED_CACHE_DIR
from ..contracts import CancelToken, EventSink, MediaStrategy, UploadContext
from ..core.identity import canonical_target
from ..core.models import (
    AlbumAttemptEvent,
    AlbumPlan,
    AlbumSettledEvent,
    BatchStatus,
    ContentBuildResult,
    LogEvent,
    MediaItem,
    ProgressEvent,
    ScanResult,
    UploadBatchResult,
    UploadRunResult,
)
from .planner import item_identity, restrict_plan, validate_plan
from .preflight import preflight_plan
from .prepared_media import PreparedMedia, SourceChanged, capture_prepared_media
from .retry import AlbumRetryPolicy, target_send_gate
from ..core.source_snapshot import validate_snapshots


RUN_COMPLETED = "COMPLETED"
RUN_PARTIAL = "PARTIAL"
RUN_FAILED = "FAILED"
RUN_CANCELLED = "CANCELLED"
RUN_UNKNOWN = "UNKNOWN"

class UploadCancelled(RuntimeError):
    """Cooperative cancellation before a send outcome is ambiguous."""

    cancelled = True


class _JournalProtectionError(RuntimeError):
    """An unsent attempt's protection could not be reliably removed."""


class StateStore(Protocol):
    """Minimum checkpoint adapter required by the generic engine."""

    def is_completed(self, item: Any) -> bool:
        """Return whether one source item is already durably completed."""

    def mark_album_completed(self, items: Sequence[Any], message_ids=None) -> None:
        """Atomically persist all source items for one confirmed Album."""


class JournalStore(Protocol):
    """Minimum durable send-journal adapter."""

    def unresolved(self, kind: str, album_key: str, target=None) -> Mapping[str, Any] | None:
        """Return a record that must block an automatic resend."""

    def prepare(self, kind: str, album_key: str, items=None, *, target=None) -> Any:
        """Persist PREPARED immediately before the sender is called."""

    def submitted(self, kind: str, album_key: str, message_ids, *, target=None) -> Any:
        """Persist that the transport request was handed off."""

    def update(self, kind: str, album_key: str, status: str, **kwargs) -> Any:
        """Persist a durable send state and observed identifiers."""

    def finalize(self, kind: str, album_key: str, *, target=None) -> bool:
        """Remove a confirmed record after state persistence succeeds."""


class Sender(Protocol):
    """Transport adapter; ``send_contents`` is also supported for TDLib V2."""

    def send(self, contents, *, target, plan, context) -> Any:
        """Send one complete Album and return an observed result."""


class Stager(Protocol):
    """Optional local staging adapter."""

    def stage(self, plan: AlbumPlan, *, context: UploadContext) -> AlbumPlan:
        """Return a content plan, retaining the source plan for identity."""

    def cleanup(self, plan: AlbumPlan, *, context: UploadContext, confirmed: bool) -> None:
        """Release staged files after a confirmed or abandoned attempt."""


class _NeverCancelToken:
    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


class _NullEventSink:
    def emit(self, event) -> None:
        return None


class _PrefetchEventSink:
    """Keep speculative and cancelled preparation from overwriting live progress."""

    def __init__(self, target_sink: EventSink | None, token: CancelToken, *, show_progress: bool):
        self.target_sink = target_sink
        self.token = token
        self.show_progress = show_progress

    def emit(self, event: Any) -> None:
        if self.token.is_cancelled() or (not self.show_progress and isinstance(event, ProgressEvent)):
            return
        if self.target_sink is not None:
            emit = getattr(self.target_sink, "emit", None)
            if callable(emit):
                try:
                    emit(event)
                except Exception:
                    pass


class _PrefetchCancelToken:
    """Cancel background preparation on either run cancellation or safe stop."""

    def __init__(self, parent: CancelToken | None):
        self.parent = parent
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def event(self):
        return self

    def is_set(self) -> bool:
        return self.is_cancelled()

    def is_cancelled(self) -> bool:
        return (
            self._cancelled.is_set()
            or _is_cancel_requested(self.parent)
            or _stop_after_current_requested(self.parent)
        )

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise UploadCancelled("后台预取已取消")



def _stable_target(target: Mapping[str, Any] | None) -> str:
    try:
        value = canonical_target(dict(target or {})) or dict(target or {})
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(target or {})


def _item_key(item: Any) -> tuple[object, ...]:
    if isinstance(item, MediaItem):
        return item_identity(item)
    if isinstance(item, Mapping):
        snapshot = item.get("snapshot")
        if snapshot is not None and hasattr(snapshot, "size"):
            size = snapshot.size
            mtime_ns = snapshot.mtime_ns
        else:
            size = item.get("scan_size", item.get("size"))
            mtime_ns = item.get("scan_mtime_ns", item.get("mtime_ns"))
        return (
            str(item.get("path", "")),
            str(item.get("source_root", "")),
            str(item.get("media_kind", "")),
            size,
            mtime_ns,
        )
    return (str(item),)


def _item_payload(item: MediaItem) -> dict[str, Any]:
    """Adapt a V2 item to the V1.9 state/journal dictionary boundary."""

    return {
        "path": item.path,
        "source_root": item.source_root,
        "scan_size": item.snapshot.size,
        "scan_mtime_ns": item.snapshot.mtime_ns,
        "media_kind": item.media_kind,
        "group_name": item.group_name,
        "month_key": item.month_key,
        "date_tag": item.date_tag,
        "capture_time": item.capture_time,
    }



def _is_cancel_requested(token: CancelToken) -> bool:
    checker = getattr(token, "is_cancelled", None)
    if callable(checker):
        return bool(checker())
    return bool(getattr(token, "cancelled", False))


def _stop_after_current_requested(token: CancelToken) -> bool:
    """Return whether the caller requested a cooperative Album-boundary stop."""

    checker = getattr(token, "stop_after_current", None)
    if callable(checker):
        return bool(checker())
    return bool(getattr(token, "stop_after_current_requested", False))


def _is_cancel_error(error: BaseException, token: CancelToken) -> bool:
    return bool(getattr(error, "cancelled", False)) or _is_cancel_requested(token)


def _check_cancel(token: CancelToken) -> None:
    raiser = getattr(token, "raise_if_cancelled", None)
    if callable(raiser):
        try:
            raiser()
        except BaseException as error:
            if bool(getattr(error, "cancelled", False)) or _is_cancel_requested(token):
                raise UploadCancelled(str(error)) from error
            raise
    if _is_cancel_requested(token):
        raise UploadCancelled("上传已取消")


def _strict_sequential(context: UploadContext | None) -> bool:
    if context is None or not isinstance(context.metadata, Mapping):
        return False
    send_policy = context.metadata.get("queue_send_policy")
    if send_policy is None:
        return False
    flag = getattr(send_policy, "is_strict_sequential", None)
    if flag is not None:
        return bool(flag)
    return str(getattr(send_policy, "error_policy", "") or "").strip().lower() == "pause_on_error"


@dataclass(slots=True)
class _PreparedBatch:
    plan: AlbumPlan
    effective_plan: AlbumPlan | None = None
    send_plan: AlbumPlan | None = None
    contents: Sequence[Mapping[str, Any]] = ()
    ready_items: tuple[MediaItem, ...] = ()
    batch_deferred: tuple[MediaItem, ...] = ()
    plan_deferred: tuple[MediaItem, ...] = ()
    build_deferred: tuple[MediaItem, ...] = ()
    build_failed: tuple[MediaItem, ...] = ()
    preflight_failed: tuple[MediaItem, ...] = ()
    preflight_deferred: tuple[MediaItem, ...] = ()
    extra_failed_items: tuple[MediaItem, ...] = ()
    errors: list[str] = field(default_factory=list)
    skipped: bool = False
    incomplete_deferred: bool = False
    cancelled: bool = False
    ambiguous: bool = False
    confirmed_recovery_failed: bool = False
    batch_result: UploadBatchResult | None = None
    managed_processed_paths: tuple[Path, ...] = ()
    prepared_media: PreparedMedia | None = None
    remote_album: Any = None



class MemoryStateStore:
    """Small reference checkpoint store used when no durable adapter is given."""

    def __init__(self):
        self.completed: dict[tuple[object, ...], tuple[int, ...]] = {}

    def is_completed(self, item: Any) -> bool:
        return _item_key(item) in self.completed

    def mark_album_completed(self, items: Sequence[Any], message_ids=None) -> None:
        ids = _as_ids(message_ids)
        for item in items:
            self.completed[_item_key(item)] = ids


class MemoryJournalStore:
    """Reference journal that exposes the same transition ordering as V1.9."""

    _UNRESOLVED = frozenset({"PREPARED", "SUBMITTED", "CONFIRMED", "UNKNOWN"})

    def __init__(self):
        self.records: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.history: list[tuple[str, str, str]] = []

    def _key(self, kind: str, album_key: str, target=None) -> tuple[str, str, str]:
        return (str(kind).strip().lower(), str(album_key), _stable_target(target))

    def unresolved(self, kind: str, album_key: str, target=None):
        record = self.records.get(self._key(kind, album_key, target))
        if record and str(record.get("status", "")).upper() in self._UNRESOLVED:
            return dict(record)
        return None

    def prepare(self, kind: str, album_key: str, items=None, *, target=None, **diagnostics):
        record = {
            "kind": str(kind).strip().lower(),
            "album_key": str(album_key),
            "status": BatchStatus.PREPARED.value,
            "items": list(items or []),
            "message_ids": [],
            "target": dict(target or {}),
            **diagnostics,
        }
        self.records[self._key(kind, album_key, target)] = record
        self.history.append((str(kind).strip().lower(), str(album_key), record["status"]))
        return dict(record)

    def submitted(self, kind: str, album_key: str, message_ids, *, target=None):
        return self.update(
            kind,
            album_key,
            BatchStatus.SUBMITTED.value,
            message_ids=message_ids,
            target=target,
        )

    def update(self, kind: str, album_key: str, status: str, *, target=None, **kwargs):
        key = self._key(kind, album_key, target)
        record = self.records.setdefault(
            key,
            {
                "kind": str(kind).strip().lower(),
                "album_key": str(album_key),
                "items": [],
                "target": dict(target or {}),
            },
        )
        record["status"] = str(status).strip().upper()
        self.history.append((str(kind).strip().lower(), str(album_key), record["status"]))
        for name, value in kwargs.items():
            if value is not None:
                record[name] = list(value) if name.endswith("_ids") else value
        return dict(record)

    def mark_confirmed(self, kind: str, album_key: str, message_ids=None, *, target=None):
        return self.update(
            kind,
            album_key,
            BatchStatus.CONFIRMED.value,
            message_ids=message_ids,
            target=target,
        )

    def failed(self, kind: str, album_key: str, error: str = "", *, target=None):
        return self.update(kind, album_key, BatchStatus.FAILED.value, error=error, target=target)

    def unknown(self, kind: str, album_key: str, error: str = "", *, target=None):
        return self.update(kind, album_key, BatchStatus.UNKNOWN.value, error=error, target=target)

    def finalize(self, kind: str, album_key: str, *, target=None) -> bool:
        return self.records.pop(self._key(kind, album_key, target), None) is not None


class UploadEngine:
    """Coordinate preparation, confirmed delivery and durable checkpoints."""

    def __init__(
        self,
        *,
        sender: Sender | Any | None = None,
        state: StateStore | Any | None = None,
        journal: JournalStore | Any | None = None,
        stager: Stager | Any | None = None,
        preflight: Callable[..., Any] | Any | None = None,
        prefetch_next: bool | None = None,
    ):
        self.sender = sender
        self.state = state
        self.journal = journal
        self.stager = stager
        self.preflight = preflight
        self.prefetch_next = prefetch_next

    def _context(
        self,
        strategy: MediaStrategy,
        *,
        source_root: Path | None,
        target: Mapping[str, Any] | None,
        cancel_token: CancelToken | None,
        event_sink: EventSink | None,
        context: UploadContext | None,
    ) -> UploadContext:
        source = Path(source_root if source_root is not None else context.source_root if context else ".")
        destination = target if target is not None else context.target if context else {}
        token = cancel_token or (context.cancel_token if context else None) or _NeverCancelToken()
        sink = event_sink or (context.event_sink if context else None) or _NullEventSink()
        state = context.state if context and context.state is not None else self.state
        journal = context.journal if context and context.journal is not None else self.journal
        sender = context.sender if context and context.sender is not None else self.sender
        stager = context.stager if context and context.stager is not None else self.stager
        preflight_checker = (
            context.preflight if context and context.preflight is not None else self.preflight
        )
        metadata = dict(context.metadata) if context else {}
        return UploadContext(
            source_root=source,
            target=destination,
            cancel_token=token,
            event_sink=sink,
            kind=str(getattr(strategy, "kind", "")),
            state=state,
            journal=journal,
            sender=sender,
            stager=stager,
            preflight=preflight_checker,
            metadata=metadata,
            scan_result=context.scan_result if context else None,
            plans=context.plans if context else None,
            queue_task_id=context.queue_task_id if context else None,
        )

    @staticmethod
    def _emit(sink: EventSink, event: Any) -> None:
        emit = getattr(sink, "emit", None)
        if callable(emit):
            try:
                emit(event)
            except Exception:
                pass

    @staticmethod
    def _log(sink: EventSink, level: str, message: str) -> None:
        UploadEngine._emit(sink, LogEvent(level=level, message=message, source="upload-engine"))

    @staticmethod
    def _journal_call(
        journal: Any,
        method: str,
        *args: Any,
        target: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        function = getattr(journal, method)
        kwargs = dict(kwargs)
        kwargs["target"] = target
        return _call_supported(function, args, **kwargs)

    @staticmethod
    def _state_completed(state: Any, item: MediaItem) -> bool:
        function = getattr(state, "is_completed")
        return bool(_call_supported(function, (_item_payload(item),)))

    @staticmethod
    def _state_mark_completed(state: Any, items: Sequence[MediaItem], ids: Sequence[int]) -> None:
        function = getattr(state, "mark_album_completed")
        payloads = [_item_payload(item) for item in items]
        _call_supported(function, (payloads, ids))

    @staticmethod
    def _send(sender: Any, contents: Sequence[Mapping[str, Any]], plan: AlbumPlan, context: UploadContext, prepared_media=None) -> Any:
        function = getattr(sender, "send_contents", None)
        if not callable(function):
            function = getattr(sender, "send", None)
        if not callable(function) and callable(sender):
            function = sender
        if not callable(function):
            raise TypeError("UploadEngine 需要 sender.send() 或 sender.send_contents()")

        metadata = context.metadata
        return _call_supported(
            function,
            (tuple(contents),),
            target=context.target,
            plan=plan,
            context=context,
            caption_limit=metadata.get("caption_limit"),
            timeout=metadata.get("send_timeout"),
            prepared_media=prepared_media,
        )

    @staticmethod
    def _stage(stager: Any, plan: AlbumPlan, context: UploadContext) -> AlbumPlan:
        function = getattr(stager, "stage", None)
        if not callable(function):
            raise TypeError("stager 必须提供 stage(plan) 方法")
        staged = _call_supported(function, (plan,), context=context)
        effective = plan if staged is None else staged
        validate_plan(effective)
        if effective.key != plan.key or effective.kind != plan.kind:
            raise ValueError("stager 不得改变 Album key 或媒体类型")
        if len(effective.pending_items) != len(plan.pending_items):
            raise ValueError("stager 不得改变 Album 项数")
        return effective

    @staticmethod
    def _cleanup(
        stager: Any,
        plan: AlbumPlan,
        context: UploadContext,
        *,
        confirmed: bool,
    ) -> None:
        function = getattr(stager, "cleanup", None)
        if callable(function):
            _call_supported(function, (plan,), context=context, confirmed=confirmed)

    def _cleanup_unconfirmed_staging(
        self,
        plan: AlbumPlan,
        stager: Any,
        effective_plan: AlbumPlan | None,
        context: UploadContext,
        errors: list[str],
    ) -> None:
        if stager is None or effective_plan is None:
            return
        try:
            self._cleanup(stager, effective_plan, context, confirmed=False)
        except Exception as cleanup_error:
            errors.append(f"Album {plan.key} 暂存清理失败：{cleanup_error}")

    def _cancel_prefetch(self, future, token, context):
        token.cancel()
        future.cancel()

        def cleanup(completed):
            if completed.cancelled():
                return
            try:
                unconsumed = completed.result()
            except Exception as error:
                self._log(context.event_sink, "WARNING", f"清理未发送的预取 Album 失败：{error}")
                return
            try:
                if context.stager is not None and unconsumed.effective_plan is not None:
                    self._cleanup(context.stager, unconsumed.effective_plan, context, confirmed=False)
            except Exception as error:
                self._log(context.event_sink, "WARNING", f"清理未发送的预取 Album 失败：{error}")
            try:
                if unconsumed.remote_album is not None:
                    unconsumed.remote_album.close()
            except Exception as error:
                self._log(context.event_sink, "WARNING", f"取消未发送的预上传 Album 失败：{error}")

        future.add_done_callback(cleanup)

    @staticmethod
    def _normalize_content_result(
        value: Any,
        plan: AlbumPlan,
    ) -> tuple[
        tuple[Mapping[str, Any], ...],
        tuple[MediaItem, ...],
        tuple[MediaItem, ...],
        tuple[MediaItem, ...],
        tuple[str, ...],
    ]:
        """Normalize plain strategy output or an item-aware build result."""

        if isinstance(value, ContentBuildResult):
            contents = tuple(value.contents)
            deferred = tuple(value.deferred_items)
            failed = tuple(value.failed_items)
            explicit_ready = value.ready_items
            if explicit_ready is None:
                excluded = {_item_key(item) for item in (*deferred, *failed)}
                ready = tuple(
                    item for item in plan.pending_items if _item_key(item) not in excluded
                )
            else:
                ready = tuple(explicit_ready)
            errors = tuple(str(error) for error in value.errors if str(error))
        else:
            contents = tuple(value)
            ready = tuple(plan.pending_items)
            deferred = ()
            failed = ()
            errors = ()

        pending_keys = [_item_key(item) for item in plan.pending_items]
        pending_set = set(pending_keys)
        if len(pending_set) != len(pending_keys):
            raise ValueError("Album 待上传项 identity 重复")

        def validate_partition(name: str, items: Sequence[MediaItem]) -> tuple[MediaItem, ...]:
            normalized = tuple(items)
            keys = [_item_key(item) for item in normalized]
            if len(set(keys)) != len(keys):
                raise ValueError(f"内容构建结果的 {name} identity 重复")
            unknown = set(keys) - pending_set
            if unknown:
                raise ValueError(f"内容构建结果的 {name} 包含不属于当前 Album 的项")
            return normalized

        ready = validate_partition("ready_items", ready)
        deferred = validate_partition("deferred_items", deferred)
        failed = validate_partition("failed_items", failed)
        ready_keys = {_item_key(item) for item in ready}
        deferred_keys = {_item_key(item) for item in deferred}
        failed_keys = {_item_key(item) for item in failed}
        if ready_keys & deferred_keys or ready_keys & failed_keys or deferred_keys & failed_keys:
            raise ValueError("内容构建结果的 item outcome 不能重叠")
        if ready_keys | deferred_keys | failed_keys != pending_set:
            raise ValueError("内容构建结果没有覆盖全部 Album 待上传项")
        if len(contents) != len(ready):
            raise ValueError("策略生成的 Telegram 内容数量必须与 ready_items 数量一致")
        return contents, ready, deferred, failed, errors

    def _prepare_plan(
        self,
        strategy: Any,
        plan: AlbumPlan,
        run_context: UploadContext,
        state: Any,
        journal: Any,
        token: CancelToken | None,
        sink: EventSink | None,
        kind: str,
    ) -> _PreparedBatch:
        # Strategy adapters may emit through context.event_sink instead of the
        # explicit event_sink argument. Keep both paths isolated for prefetch.
        run_context = replace(
            run_context,
            cancel_token=token if token is not None else run_context.cancel_token,
            event_sink=sink if sink is not None else run_context.event_sink,
        )
        if _stop_after_current_requested(token) or _is_cancel_requested(token):
            return _PreparedBatch(plan=plan, cancelled=True)

        plan_deferred: tuple[MediaItem, ...] = ()
        protected_items: tuple[MediaItem, ...] = ()
        try:
            validate_plan(plan)
            try:
                unresolved = self._journal_call(
                    journal,
                    "unresolved",
                    kind,
                    plan.key,
                    target=run_context.target,
                )
            except Exception as journal_error:
                message = (
                    f"Album {plan.key} 上传日志读取失败：{journal_error}；"
                    "为避免重复上传已阻止自动重试"
                )
                self._log(sink, "ERROR", message)
                return _PreparedBatch(
                    plan=plan,
                    ambiguous=True,
                    errors=[message],
                    batch_result=UploadBatchResult(plan.key, BatchStatus.UNKNOWN, error=message),
                )
            overlap_check = getattr(journal, "unresolved_for_items", None)
            if unresolved is None and callable(overlap_check):
                unresolved = overlap_check(
                    kind,
                    [_item_payload(item) for item in plan.pending_items],
                    target=run_context.target,
                )
            if unresolved is not None and unresolved.get("reconciled") and callable(overlap_check):
                protected_items = tuple(item for item in plan.pending_items
                                        if overlap_check(kind, [_item_payload(item)],
                                                         target=run_context.target) is not None)
                protected_keys = {_item_key(item) for item in protected_items}
                retryable = tuple(item for item in plan.pending_items
                                  if _item_key(item) not in protected_keys
                                  and not self._state_completed(state, item))
                if retryable:
                    # The old Album key still guards its pending members. Use
                    # a stable attempt key for only the released source snapshots.
                    suffix = hashlib.sha256(repr(tuple(_item_key(item) for item in retryable)).encode()).hexdigest()[:16]
                    plan = replace(restrict_plan(plan, retryable), key=f"{plan.key}:retry:{suffix}")
                    plan_deferred = protected_items
                    unresolved = self._journal_call(journal, "unresolved", kind, plan.key,
                                                    target=run_context.target)
            if unresolved is not None:
                protected_key = str(unresolved.get("album_key") or plan.key)
                status = str(unresolved.get("status", BatchStatus.UNKNOWN.value)).upper()
                if status == BatchStatus.CONFIRMED.value:
                    from .reconciliation import ReconciliationService

                    try:
                        repaired = ReconciliationService(journal).recover_confirmed(
                            kind,
                            protected_key,
                            target=run_context.target,
                            state=state,
                            record=unresolved,
                        )
                    except Exception as recovery_error:
                        message = f"Album {plan.key} CONFIRMED 断点恢复失败：{recovery_error}"
                        self._log(sink, "ERROR", message)
                        return _PreparedBatch(
                            plan=plan,
                            confirmed_recovery_failed=True,
                            errors=[message],
                            batch_result=UploadBatchResult(
                                plan.key,
                                BatchStatus.CONFIRMED,
                                error=message,
                            ),
                        )
                    if repaired:
                        self._log(sink, "INFO", f"Album {plan.key} 已恢复本地断点并清理保护记录")
                        if protected_key == plan.key:
                            return _PreparedBatch(plan=plan, skipped=True)
                        unresolved = None
                message = (
                    f"Album {protected_key} 的发送状态为 {status}，"
                    "为避免重复上传已阻止自动重试"
                )
                if unresolved is not None:
                    self._log(sink, "WARNING", message)
                    return _PreparedBatch(
                        plan=plan,
                        ambiguous=True,
                        errors=[message],
                        batch_result=UploadBatchResult(plan.key, BatchStatus.UNKNOWN, error=message),
                    )
            pending = tuple(
                item for item in plan.pending_items if not self._state_completed(state, item)
            )
            current_plan = restrict_plan(plan, pending)
        except Exception as error:
            message = f"Album {getattr(plan, 'key', '<unknown>')} 状态检查失败：{error}"
            return _PreparedBatch(
                plan=plan,
                errors=[message],
                extra_failed_items=tuple(getattr(plan, "pending_items", ())),
                batch_result=UploadBatchResult(
                    album_key=str(getattr(plan, "key", "<unknown>")),
                    status=BatchStatus.FAILED,
                    error=message,
                    failure_stage="pre_submit",
                ),
            )

        if not current_plan.pending_items:
            return _PreparedBatch(plan=plan, skipped=True)

        strict_order = _strict_sequential(run_context)
        errors: list[str] = []
        preflight_failed: tuple[MediaItem, ...] = ()
        preflight_deferred: tuple[MediaItem, ...] = ()
        checker = run_context.preflight
        if checker is not None:
            try:
                current_plan, preflight_result = preflight_plan(
                    current_plan,
                    checker,
                    context=run_context,
                    cancel_token=token,
                    event_sink=sink,
                )
            except Exception as error:
                if _is_cancel_error(error, token):
                    return _PreparedBatch(plan=plan, cancelled=True)
                message = f"Album {plan.key} 预检失败：{error}"
                return _PreparedBatch(
                    plan=plan,
                    errors=[message],
                    extra_failed_items=tuple(current_plan.pending_items),
                    batch_result=UploadBatchResult(
                        plan.key, BatchStatus.FAILED, error=message, failure_stage="pre_submit",
                    ),
                )
            preflight_deferrals = preflight_result.deferred_items
            plan_deferred = tuple((*plan_deferred, *preflight_deferrals))
            preflight_deferred = plan_deferred
            preflight_failed = preflight_result.failed_items
            if preflight_result.cancelled:
                return _PreparedBatch(
                    plan=plan,
                    cancelled=True,
                    plan_deferred=plan_deferred,
                    preflight_failed=preflight_failed,
                    preflight_deferred=preflight_deferred,
                )
            failed_paths = {str(item.path) for item in preflight_failed}
            deferred_paths = {str(item.path) for item in preflight_deferrals}
            failed_reason = "; ".join(
                reason for path, reason in preflight_result.reasons if path in failed_paths
            )
            defer_reason = "; ".join(
                reason for path, reason in preflight_result.reasons if path in deferred_paths
            )
            if preflight_failed:
                failed_reason = failed_reason or "预检失败"
                errors.append(f"Album {plan.key} 预检失败：{failed_reason}")
            if preflight_deferrals and defer_reason:
                errors.append(f"Album {plan.key} 预检暂缓：{defer_reason}")
            if preflight_failed and (strict_order or not current_plan.pending_items):
                return _PreparedBatch(
                    plan=plan,
                    errors=errors,
                    plan_deferred=plan_deferred,
                    preflight_failed=preflight_failed,
                    preflight_deferred=preflight_deferred,
                    batch_result=UploadBatchResult(
                        plan.key,
                        BatchStatus.FAILED,
                        error=failed_reason,
                        deferred_items=plan_deferred,
                        failure_stage="pre_submit",
                    ),
                )
            if not current_plan.pending_items or (strict_order and preflight_deferrals):
                return _PreparedBatch(
                    plan=plan,
                    incomplete_deferred=True,
                    errors=errors,
                    plan_deferred=plan_deferred,
                    preflight_failed=preflight_failed,
                    preflight_deferred=preflight_deferred,
                )
            if getattr(checker, "validated_file_readiness", False):
                run_context = replace(run_context, metadata={**run_context.metadata, "preflight_verified": True})

        ready_items = tuple(current_plan.pending_items)
        batch_deferred = tuple(plan_deferred)
        effective_plan = current_plan
        if run_context.stager is not None:
            try:
                effective_plan = self._stage(run_context.stager, current_plan, run_context)
            except Exception as error:
                if _is_cancel_error(error, token) or _stop_after_current_requested(token):
                    cleanup_errors = list(errors)
                    self._cleanup_unconfirmed_staging(
                        plan, run_context.stager, current_plan, run_context, cleanup_errors,
                    )
                    return _PreparedBatch(
                        plan=plan,
                        cancelled=True,
                        errors=cleanup_errors,
                        plan_deferred=plan_deferred,
                        preflight_failed=preflight_failed,
                        preflight_deferred=preflight_deferred,
                    )
                message = f"Album {plan.key} 暂存失败：{error}"
                errors.append(message)
                return _PreparedBatch(
                    plan=plan,
                    errors=errors,
                    extra_failed_items=ready_items,
                    plan_deferred=plan_deferred,
                    preflight_failed=preflight_failed,
                    preflight_deferred=preflight_deferred,
                    batch_result=UploadBatchResult(
                        plan.key,
                        BatchStatus.FAILED,
                        error=message,
                        deferred_items=plan_deferred,
                        failure_stage="pre_submit",
                    ),
                )

        if (getattr(strategy, "validate_source_snapshots", False)
                and not run_context.metadata.get("preflight_verified")):
            try:
                capture_prepared_media(effective_plan.pending_items, ())
            except SourceChanged as exc:
                self._cleanup_unconfirmed_staging(
                    plan, run_context.stager, effective_plan, run_context, errors,
                )
                return _PreparedBatch(
                    plan=plan,
                    incomplete_deferred=True,
                    errors=[*errors, str(exc)],
                    plan_deferred=tuple((*plan_deferred, *effective_plan.pending_items)),
                    preflight_failed=preflight_failed,
                    preflight_deferred=preflight_deferred,
                )
        try:
            _check_cancel(token)
            built_contents = _call_supported(
                getattr(strategy, "build_contents"),
                (effective_plan,),
                cancel_token=token,
                event_sink=sink,
                context=run_context,
            )
            (
                contents,
                ready_items,
                build_deferred,
                build_failed,
                build_errors,
            ) = self._normalize_content_result(built_contents, effective_plan)
            batch_deferred = tuple((*plan_deferred, *build_deferred))
            for build_error in build_errors:
                errors.append(f"Album {plan.key} 内容构建提示：{build_error}")
            build_failed_stop = bool(build_failed and (not ready_items or strict_order))
            build_deferred_stop = bool(not build_failed and (not ready_items or (strict_order and build_deferred)))
            if build_failed_stop or build_deferred_stop:
                self._cleanup_unconfirmed_staging(
                    plan, run_context.stager, effective_plan, run_context, errors,
                )
                if build_failed:
                    message = "; ".join(build_errors) or "内容构建失败"
                    errors.append(f"Album {plan.key} 内容构建失败：{message}")
                    return _PreparedBatch(
                        plan=plan,
                        errors=errors,
                        batch_deferred=batch_deferred,
                        plan_deferred=plan_deferred,
                        build_deferred=build_deferred,
                        build_failed=build_failed,
                        preflight_failed=preflight_failed,
                        preflight_deferred=preflight_deferred,
                        batch_result=UploadBatchResult(
                            plan.key,
                            BatchStatus.FAILED,
                            error=message,
                            deferred_items=batch_deferred,
                            failure_stage="pre_submit",
                        ),
                    )
                return _PreparedBatch(
                    plan=plan,
                    incomplete_deferred=True,
                    errors=errors,
                    batch_deferred=batch_deferred,
                    plan_deferred=plan_deferred,
                    build_deferred=build_deferred,
                    build_failed=build_failed,
                    preflight_failed=preflight_failed,
                    preflight_deferred=preflight_deferred,
                )
            if not contents:
                raise ValueError("不能发送空的 Telegram Album")
        except SourceChanged as exc:
            self._cleanup_unconfirmed_staging(
                plan, run_context.stager, effective_plan, run_context, errors,
            )
            errors.append(str(exc))
            return _PreparedBatch(
                plan=plan,
                incomplete_deferred=True,
                errors=errors,
                plan_deferred=tuple((*plan_deferred, *effective_plan.pending_items)),
                preflight_failed=preflight_failed,
                preflight_deferred=preflight_deferred,
            )
        except Exception as error:
            if _is_cancel_error(error, token):
                self._cleanup_unconfirmed_staging(
                    plan, run_context.stager, effective_plan, run_context, errors,
                )
                return _PreparedBatch(plan=plan, cancelled=True, errors=errors)
            message = f"Album {plan.key} 内容构建失败：{error}"
            errors.append(message)
            self._cleanup_unconfirmed_staging(
                plan, run_context.stager, effective_plan, run_context, errors,
            )
            return _PreparedBatch(
                plan=plan,
                errors=errors,
                extra_failed_items=tuple(effective_plan.pending_items),
                plan_deferred=plan_deferred,
                preflight_failed=preflight_failed,
                preflight_deferred=preflight_deferred,
                batch_result=UploadBatchResult(
                    plan.key, BatchStatus.FAILED, error=message, failure_stage="pre_submit",
                ),
            )

        if _stop_after_current_requested(token) or _is_cancel_requested(token):
            self._cleanup_unconfirmed_staging(
                plan, run_context.stager, effective_plan, run_context, errors,
            )
            return _PreparedBatch(plan=plan, cancelled=True, errors=errors)

        managed_paths: list[Path] = []
        try:
            from ..media.video_prepare import safe_group_key
            root = VIDEO_PROCESSED_CACHE_DIR.resolve()
            group_dir = (VIDEO_PROCESSED_CACHE_DIR / safe_group_key(plan.key)).resolve()
            try:
                group_dir.relative_to(root)
                if group_dir != root and group_dir.is_dir():
                    for p in group_dir.iterdir():
                        if p.is_file() and not p.name.startswith("."):
                            managed_paths.append(p)
            except ValueError:
                pass
            for c in contents:
                if isinstance(c, Mapping) and c.get("@type") == "inputMessageVideo":
                    raw_vp = c.get("video", {}).get("video", {}).get("path")
                    if raw_vp:
                        vp = Path(str(raw_vp)).resolve()
                        try:
                            vp.relative_to(root)
                            if vp != root and vp.is_file() and vp not in managed_paths:
                                managed_paths.append(vp)
                        except ValueError:
                            pass
        except Exception:
            pass

        prepared_media = None
        if getattr(strategy, "validate_source_snapshots", False):
            try:
                prepared_media = capture_prepared_media(ready_items, contents)
            except SourceChanged as exc:
                # Defer the complete Album to preserve its caption and membership.
                self._cleanup_unconfirmed_staging(
                    plan, run_context.stager, effective_plan, run_context, errors,
                )
                errors.append(str(exc))
                deferred_all = tuple((*plan_deferred, *ready_items))
                batch_deferred_all = tuple((*deferred_all, *build_deferred))
                if preflight_failed or build_failed:
                    return _PreparedBatch(
                        plan=plan,
                        errors=errors,
                        batch_deferred=batch_deferred_all,
                        plan_deferred=deferred_all,
                        build_deferred=build_deferred,
                        build_failed=build_failed,
                        preflight_failed=preflight_failed,
                        preflight_deferred=preflight_deferred,
                        batch_result=UploadBatchResult(
                            plan.key,
                            BatchStatus.FAILED,
                            error=str(exc),
                            deferred_items=batch_deferred_all,
                            failure_stage="pre_submit",
                        ),
                    )
                return _PreparedBatch(
                    plan=plan,
                    incomplete_deferred=True,
                    errors=errors,
                    batch_deferred=batch_deferred_all,
                    plan_deferred=deferred_all,
                    build_deferred=build_deferred,
                    build_failed=build_failed,
                    preflight_failed=preflight_failed,
                    preflight_deferred=preflight_deferred,
                )

        send_plan = restrict_plan(effective_plan, ready_items)
        remote_album = None
        prepare_remote = getattr(run_context.sender, "prepare_remote", None)
        if callable(prepare_remote):
            try:
                remote_album = prepare_remote(contents, context=run_context)
            except CancellationRequested:
                return _PreparedBatch(plan=plan, effective_plan=effective_plan, cancelled=True, errors=errors)
            except Exception as error:
                self._log(sink, "WARNING", f"Album {plan.key} 预上传不可用：{error}")
        return _PreparedBatch(
            plan=plan,
            effective_plan=effective_plan,
            ambiguous=bool(protected_items),
            send_plan=send_plan,
            contents=contents,
            ready_items=ready_items,
            batch_deferred=batch_deferred,
            plan_deferred=plan_deferred,
            build_deferred=build_deferred,
            build_failed=build_failed,
            preflight_failed=preflight_failed,
            preflight_deferred=preflight_deferred,
            errors=errors,
            managed_processed_paths=tuple(managed_paths),
            prepared_media=prepared_media,
            remote_album=remote_album,
        )

    def _attempts(self, prep, context, ordinal, policy, *, first_attempt=1, last_attempt=None, previous=None):
        """Keep one prepared payload and close each failed attempt before retrying."""
        plan = prep.plan
        items = prep.ready_items
        token, sink, journal, target = (
            context.cancel_token, context.event_sink, context.journal, context.target
        )
        schedule = context.metadata.get("queue_send_policy")
        gate = context.metadata.get("target_send_gate", target_send_gate) if schedule is not None else None
        target_key = _stable_target(target)
        max_attempts = policy.max_attempts
        if not policy.enabled and schedule is not None:
            max_attempts += schedule.flood_max_retries

        def stopped():
            return _is_cancel_requested(token) or _stop_after_current_requested(token)

        last_error = previous.error if previous is not None else None
        observed = previous
        journal_attempt = previous.journal_attempt_number if previous is not None else 0
        last_attempt = max_attempts if last_attempt is None else last_attempt
        attempt = first_attempt
        while attempt <= last_attempt:
            if stopped():
                return observed, True
            if gate is not None:
                while (remaining := gate.remaining(target_key, schedule.min_interval)) > 0:
                    self._emit(sink, AlbumAttemptEvent(
                        ordinal, plan.key, attempt, max_attempts, last_error, math.ceil(remaining),
                    ))
                    if not cancelable_sleep(min(1.0, remaining), cancel_token=stopped):
                        return observed, True
            if observed is not None:
                try:
                    validate_snapshots((item.path, item.snapshot.size, item.snapshot.mtime_ns)
                                       for item in items)
                    if prep.prepared_media is not None:
                        prep.prepared_media.validate()
                except SourceChanged as exc:
                    self._log(sink, "WARNING", str(exc))
                    return replace(observed, error=str(exc), source_changed=True), False
                try:
                    if any(self._state_completed(context.state, item) for item in items):
                        return replace(observed, error="文件已有完成断点，已取消整组重试"), False
                    if self._journal_call(journal, "unresolved", plan.kind, plan.key, target=target):
                        return _ObservedSend(BatchStatus.UNKNOWN, error="发送记录仍受保护，已取消整组重试", preserve_journal=True), False
                except Exception as exc:
                    return _ObservedSend(BatchStatus.UNKNOWN, error=f"重试前状态核对失败：{exc}", preserve_journal=True), False

            wait_until_ready = getattr(context.sender, "wait_until_ready", None)
            if callable(wait_until_ready):
                try:
                    if not wait_until_ready(context):
                        return observed, True
                except Exception as error:
                    return _ObservedSend(BatchStatus.FAILED, error=str(error)), stopped()

            # The stable key still protects the same Album; each try gets a
            # separate identifier for diagnostics in its durable journal.
            attempt_id = str(uuid4())
            journal_attempt += 1
            try:
                self._journal_call(
                    journal, "prepare", plan.kind, plan.key, [_item_payload(item) for item in items],
                    target=target, attempt_id=attempt_id, attempt_number=journal_attempt,
                )
            except Exception as exc:
                message = f"Album {ordinal} 无法写入 PREPARED 日志：{exc}"
                self._log(sink, "ERROR", message)
                return _ObservedSend(BatchStatus.UNKNOWN, error=message, preserve_journal=True), stopped()

            if stopped():
                self._discard_unsent(journal, plan, target)
                return observed, True

            self._emit(sink, AlbumAttemptEvent(ordinal, plan.key, attempt, max_attempts, last_error))
            if attempt > 1:
                self._log(sink, "INFO", f"Album {ordinal} 开始第 {attempt}/{max_attempts} 次尝试")
            try:
                if prep.prepared_media is not None:
                    prep.prepared_media.validate()
                if stopped():
                    self._discard_unsent(journal, plan, target)
                    return observed, True
                if gate is not None:
                    gate.mark_send()
                contents = prep.remote_album.contents if prep.remote_album is not None else prep.contents
                raw = self._send(context.sender, contents, prep.send_plan, context, prep.prepared_media)
                observed = _normalize_send_result(raw, len(items))
            except _JournalProtectionError:
                raise
            except SourceChanged:
                self._discard_unsent(journal, plan, target)
                raise
            except SendRequestRejected as exc:
                observed = _ObservedSend(BatchStatus.FAILED, error=str(exc), request_rejection=exc)
            except Exception as exc:
                delivery = getattr(exc, "result", None)
                if isinstance(delivery, Mapping):
                    observed = _normalize_send_result(delivery, len(items))
                    observed = replace(observed, error=str(exc) or observed.error)
                else:
                    status = BatchStatus.FAILED if getattr(exc, "submitted", None) is False else BatchStatus.UNKNOWN
                    observed = _ObservedSend(status, error=str(exc) or status.value)

            observed = replace(observed, journal_attempt_number=journal_attempt)
            cancelled = _is_cancel_requested(token)
            rate_limit = observed.flood_wait()
            if gate is not None:
                if rate_limit is not None:
                    gate.cool_down(target_key, rate_limit, schedule.flood_jitter if rate_limit > 0 else 0)
            if observed.status is not BatchStatus.FAILED:
                return observed, cancelled

            last_error = observed.error or "Album 内消息发送失败"
            rejection = observed.request_rejection
            try:
                self._journal_call(
                    journal, "update", plan.kind, plan.key, BatchStatus.FAILED.value,
                    error=last_error, message_ids=observed.message_ids,
                    succeeded_ids=observed.succeeded_ids, failed_ids=observed.failed_ids,
                    pending_ids=observed.pending_ids, item_results=observed.item_results,
                    request_rejection=rejection.as_dict() if rejection is not None else None,
                    target=target,
                )
            except Exception as exc:
                return replace(observed, status=BatchStatus.UNKNOWN,
                               error=f"Album {ordinal} 无法保存 FAILED 日志：{exc}", preserve_journal=True), cancelled

            if not stopped() and prep.remote_album is not None and observed.rejected_file_reference(len(items)):
                prep.remote_album.close()
                prep.remote_album = None
                self._log(sink, "WARNING", f"Album {ordinal} 的预上传文件引用被拒绝，改用本地文件发送")
                continue

            eligible_delay = observed.album_retry_delay(
                len(items), complete_album=len(items) == len(plan.items), flood_only=not policy.enabled,
            )
            if cancelled or _stop_after_current_requested(token) or eligible_delay is None:
                return observed, stopped()
            if attempt == last_attempt:
                if max_attempts > 1 and attempt == max_attempts:
                    self._log(sink, "WARNING", f"Album {ordinal} 已达到最大尝试次数（{attempt}/{max_attempts}）")
                return observed, False

            delay = max(policy.delay_seconds if policy.enabled else 0, eligible_delay)
            reason = "发送请求被 Telegram 拒绝" if rejection is not None else "全部消息明确失败"
            self._log(sink, "WARNING",
                      f"Album {ordinal} 第 {attempt} 次尝试{reason}：{last_error}，{delay:g} 秒后重试")
            deadline = time.monotonic() + delay
            remaining = delay
            while remaining > 0:
                self._emit(sink, AlbumAttemptEvent(
                    ordinal, plan.key, attempt, max_attempts, last_error, math.ceil(remaining)
                ))
                if not cancelable_sleep(min(1.0, remaining), cancel_token=stopped):
                    return observed, True
                remaining = deadline - time.monotonic()
            attempt += 1
        return observed, stopped()

    def _discard_unsent(self, journal, plan, target):
        try:
            self._journal_call(journal, "finalize", plan.kind, plan.key, target=target)
        except Exception as exc:
            raise _JournalProtectionError(f"清理未发送的 PREPARED 记录失败：{exc}") from exc

    def run(
        self,
        strategy: MediaStrategy,
        *,
        source_root: Path | None = None,
        target: Mapping[str, Any] | None = None,
        cancel_token: CancelToken | None = None,
        event_sink: EventSink | None = None,
        context: UploadContext | None = None,
    ) -> UploadRunResult:
        """Run one strategy while preserving the five durable send states."""

        run_context = self._context(
            strategy,
            source_root=source_root,
            target=target,
            cancel_token=cancel_token,
            event_sink=event_sink,
            context=context,
        )
        token = run_context.cancel_token
        sink = run_context.event_sink
        kind = run_context.kind
        if not kind:
            return UploadRunResult(RUN_FAILED, error="MediaStrategy.kind 不能为空")
        if _stop_after_current_requested(token):
            return UploadRunResult(
                RUN_CANCELLED,
                error="已安全停止，未开始新的 Album",
                cancelled=True,
            )

        state = run_context.state if run_context.state is not None else MemoryStateStore()
        run_context = replace(run_context, state=state)
        journal = run_context.journal if run_context.journal is not None else MemoryJournalStore()
        run_context = replace(run_context, journal=journal)

        batches: list[UploadBatchResult] = []
        deferred_items: list[MediaItem] = []
        failed_items: list[MediaItem] = []
        scanned_items: tuple[MediaItem, ...] = ()
        errors: list[str] = []
        ambiguous = False
        cancelled = False
        confirmed_count = 0
        confirmed_recovery_failed = False
        retry_policy = run_context.metadata.get("album_retry", AlbumRetryPolicy())
        send_policy = run_context.metadata.get("queue_send_policy")
        reserve_tail_attempt = (retry_policy.enabled and send_policy is not None
                                and send_policy.error_policy == "skip_and_retry")
        paused = False
        tail = []
        delayed_failures = {}

        def record_batch(batch, prep=None, *, failed=()):
            batches.append(batch)
            deferred = ()
            if prep is not None:
                failed = (*prep.extra_failed_items, *prep.preflight_failed, *prep.build_failed, *failed)
                deferred = (*prep.plan_deferred, *prep.build_deferred)
            self._emit(sink, AlbumSettledEvent(batch, tuple(failed), deferred))

        def should_pause(*, unknown=False):
            return send_policy is not None and send_policy.pauses_for(unknown=unknown)

        try:
            _check_cancel(token)
            scan_result = run_context.scan_result
            if scan_result is None:
                scan_result = _call_supported(
                    getattr(strategy, "scan"),
                    (run_context.source_root,),
                    cancel_token=token,
                    event_sink=sink,
                    context=run_context,
                )
            if not isinstance(scan_result, ScanResult):
                raise TypeError("MediaStrategy.scan() 必须返回 ScanResult")
            scanned_items = tuple(scan_result.items)
            self._emit(
                sink,
                ProgressEvent(
                    kind=kind,
                    phase="scan",
                    completed=len(scanned_items),
                    total=len(scanned_items),
                    message="扫描完成",
                ),
            )
            for message in scan_result.warnings:
                self._log(sink, "WARNING", str(message))
            if scan_result.errors:
                errors.extend(str(message) for message in scan_result.errors)
                for message in scan_result.errors:
                    self._log(sink, "ERROR", str(message))
            if scan_result.cancelled:
                return UploadRunResult(
                    RUN_CANCELLED,
                    error="扫描已取消",
                    scanned_items=scanned_items,
                    cancelled=True,
                )
            _check_cancel(token)
            if _stop_after_current_requested(token):
                return UploadRunResult(
                    RUN_CANCELLED,
                    error="已安全停止，未开始新的 Album",
                    scanned_items=scanned_items,
                    cancelled=True,
                )
            raw_plans = run_context.plans
            if raw_plans is None:
                raw_plans = _call_supported(
                    getattr(strategy, "build_plans"),
                    (scan_result,),
                    target=run_context.target,
                    state=state,
                    context=run_context,
                )
            candidates = []
            candidate_bytes = []
            pending_total = 0
            for plan in raw_plans:
                _check_cancel(token)
                pending = tuple(item for item in plan.pending_items if not self._state_completed(state, item))
                if pending:
                    pending_total += 1
                    candidates.append(plan)
                    candidate_bytes.append(sum(max(0, item.snapshot.size) for item in pending))
                elif plan.pending_items:
                    # A fully checkpointed Album can still carry a protected
                    # journal record from an interrupted finalization.
                    try:
                        unresolved = self._journal_call(journal, "unresolved", kind, plan.key, target=run_context.target)
                    except Exception:
                        unresolved = True
                    if unresolved:
                        candidates.append(plan)
                        candidate_bytes.append(0)
            plans = tuple(candidates)
            run_context = replace(run_context, metadata={**run_context.metadata, "pending_plan_total": pending_total})
            if _stop_after_current_requested(token):
                return UploadRunResult(
                    RUN_CANCELLED,
                    error="已安全停止，未开始新的 Album",
                    scanned_items=scanned_items,
                    cancelled=True,
                )
        except Exception as error:
            if _is_cancel_error(error, token):
                return UploadRunResult(
                    RUN_CANCELLED,
                    error=str(error) or "上传已取消",
                    scanned_items=scanned_items,
                    cancelled=True,
                )
            return UploadRunResult(
                RUN_FAILED,
                error=str(error),
                scanned_items=scanned_items,
            )

        prefetch_enabled = (
            self.prefetch_next
            if self.prefetch_next is not None
            else bool(run_context.metadata.get("prefetch_next", False))
        )
        slots = max(1, int(run_context.metadata.get("prefetch_slots", 3)))
        byte_limit = max(1, int(run_context.metadata.get("prefetch_max_bytes", 8 * 1024 ** 3)))
        worker_count = io_worker_count(
            run_context.source_root,
            local=run_context.metadata.get("io_workers_local", 4),
            network=run_context.metadata.get("io_workers_network", 2),
        )
        io_executor = UploadPreparationExecutor(worker_count, "tdlib-media-io")
        run_context = replace(run_context, metadata={**run_context.metadata, "io_executor": io_executor})
        executor = UploadPreparationExecutor(slots if prefetch_enabled else 1, "tdlib-prepare")
        prefetched: dict[int, tuple[Future[_PreparedBatch], _PrefetchCancelToken, int]] = {}
        reserved_bytes = 0
        next_index = 0
        consumed_index: int | None = None
        active_prep = None

        def schedule(index, plan, amount, *, show_progress):
            prefetch_token = _PrefetchCancelToken(token)
            prefetch_sink = _PrefetchEventSink(sink, prefetch_token, show_progress=show_progress)
            prefetch_context = replace(run_context, cancel_token=prefetch_token, event_sink=prefetch_sink)
            future = executor.submit(
                self._prepare_plan, strategy, plan, prefetch_context,
                state, journal, prefetch_token, prefetch_sink, kind,
            )
            slot = (future, prefetch_token, amount)
            prefetched[index] = slot
            return slot

        def refill():
            nonlocal next_index, reserved_bytes
            if not prefetch_enabled:
                return
            while (next_index < len(plans) and len(prefetched) < slots
                   and not (_is_cancel_requested(token) or _stop_after_current_requested(token))):
                next_plan = plans[next_index]
                amount = candidate_bytes[next_index]
                if reserved_bytes + amount > byte_limit:
                    return
                schedule(next_index, next_plan, amount, show_progress=next_index == 0)
                reserved_bytes += amount
                next_index += 1

        try:
            primary = ((index, plan, False) for index, plan in enumerate(plans))
            for plan_index, initial_plan, tail_retry in chain(primary, tail):
                plan = initial_plan
                if active_prep is not None and active_prep.remote_album is not None:
                    active_prep.remote_album.close()
                active_prep = None
                if consumed_index is not None:
                    reserved_bytes -= prefetched.pop(consumed_index)[2]
                    consumed_index = None
                if _stop_after_current_requested(token) or _is_cancel_requested(token):
                    cancelled = True
                    break

                if tail_retry:
                    previous, _, _, retry_at = delayed_failures[initial_plan.key]
                    while (remaining := retry_at - time.monotonic()) > 0:
                        self._emit(sink, AlbumAttemptEvent(
                            plan_index + 1, initial_plan.key, retry_policy.max_attempts,
                            retry_policy.max_attempts, previous.error, math.ceil(remaining),
                        ))
                        if not cancelable_sleep(min(1.0, remaining), cancel_token=lambda: (
                            _is_cancel_requested(token) or _stop_after_current_requested(token)
                        )):
                            cancelled = True
                            break
                    if cancelled:
                        break

                if not tail_retry:
                    refill()
                slot = None if tail_retry else prefetched.get(plan_index)
                if slot is None:
                    # Prepare Albums larger than the entire budget only when
                    # active, outside the speculative buffer.
                    if not tail_retry:
                        next_index = plan_index + 1
                    slot = schedule(plan_index, plan, 0, show_progress=True)

                prefetch_future = slot[0]
                while not prefetch_future.done() and not (
                    _is_cancel_requested(token) or _stop_after_current_requested(token)
                ):
                    wait((prefetch_future,), timeout=0.05)
                if _is_cancel_requested(token) or _stop_after_current_requested(token):
                    cancelled = True
                    break
                consumed_index = plan_index
                try:
                    prep = prefetch_future.result()
                except Exception as exc:
                    message = f"Album {plan.key} 预加载异常：{exc}"
                    errors.append(message)
                    failed = tuple(getattr(plan, "pending_items", ()))
                    record_batch(
                        UploadBatchResult(plan.key, BatchStatus.FAILED, error=message, failure_stage="pre_submit"),
                        failed=failed,
                    )
                    failed_items.extend(failed)
                    if (paused := should_pause()):
                        break
                    continue

                plan = prep.plan
                active_prep = prep
                errors.extend(prep.errors)
                failed_items.extend(prep.extra_failed_items)
                failed_items.extend(prep.preflight_failed)
                failed_items.extend(prep.build_failed)
                deferred_items.extend(prep.plan_deferred)
                deferred_items.extend(prep.build_deferred)

                if prep.ambiguous:
                    ambiguous = True
                if prep.confirmed_recovery_failed:
                    confirmed_recovery_failed = True

                if prep.cancelled:
                    cancelled = True
                    self._cleanup_unconfirmed_staging(
                        plan, run_context.stager, prep.effective_plan, run_context, errors,
                    )
                    break

                if prep.batch_result is not None:
                    self._cleanup_unconfirmed_staging(
                        plan, run_context.stager, prep.effective_plan, run_context, errors,
                    )
                    record_batch(prep.batch_result, prep)
                    if prep.confirmed_recovery_failed and (paused := should_pause(unknown=True)):
                        break
                    if prep.batch_result.status is BatchStatus.FAILED and (paused := should_pause()):
                        break
                    if prep.batch_result.status in {BatchStatus.PREPARED, BatchStatus.SUBMITTED, BatchStatus.UNKNOWN}:
                        if (paused := should_pause(unknown=True)):
                            break
                    continue

                if prep.incomplete_deferred:
                    self._cleanup_unconfirmed_staging(
                        plan, run_context.stager, prep.effective_plan, run_context, errors,
                    )
                    if prep.ambiguous and (paused := should_pause(unknown=True)):
                        break
                    if (paused := should_pause()):
                        break
                    continue

                if prep.skipped:
                    if (prep.ambiguous or prep.confirmed_recovery_failed) and (paused := should_pause(unknown=True)):
                        break
                    continue

                effective_plan = prep.effective_plan
                send_plan = prep.send_plan
                contents = prep.contents
                ready_items = prep.ready_items
                batch_deferred = prep.batch_deferred

                if prep.ambiguous and should_pause(unknown=True):
                    paused = True
                    record_batch(UploadBatchResult(plan.key, BatchStatus.UNKNOWN, error="Album 有待核对的发送记录"), prep)
                    self._cleanup_unconfirmed_staging(
                        plan, run_context.stager, effective_plan, run_context, errors,
                    )
                    break

                if not ready_items or send_plan is None or not contents:
                    continue

                if _stop_after_current_requested(token) or _is_cancel_requested(token):
                    self._cleanup_unconfirmed_staging(
                        plan, run_context.stager, effective_plan, run_context, errors,
                    )
                    cancelled = True
                    break

                if run_context.sender is None:
                    message = "UploadEngine 未配置 sender，未发起 Telegram 请求"
                    errors.append(f"Album {plan.key} {message}")
                    failed_items.extend(ready_items)
                    record_batch(
                        UploadBatchResult(plan.key, BatchStatus.FAILED, error=message, failure_stage="pre_submit"),
                        prep, failed=ready_items,
                    )
                    self._cleanup_unconfirmed_staging(
                        plan, run_context.stager, effective_plan, run_context, errors,
                    )
                    if (paused := should_pause()):
                        break
                    continue

                try:
                    observed, attempt_cancelled = self._attempts(
                        prep, run_context, plan_index + 1, retry_policy,
                        first_attempt=retry_policy.max_attempts if tail_retry else 1,
                        last_attempt=retry_policy.max_attempts - 1 if reserve_tail_attempt and not tail_retry else None,
                        previous=delayed_failures[initial_plan.key][0] if tail_retry else None,
                    )
                except SourceChanged as error:
                    deferred_items.extend(ready_items)
                    errors.append(str(error))
                    self._cleanup_unconfirmed_staging(
                        plan, run_context.stager, effective_plan, run_context, errors,
                    )
                    if (paused := should_pause()):
                        break
                    continue
                except _JournalProtectionError as error:
                    observed = _ObservedSend(BatchStatus.UNKNOWN, error=str(error), preserve_journal=True)
                    attempt_cancelled = _is_cancel_requested(token) or _stop_after_current_requested(token)
                cancelled = cancelled or attempt_cancelled
                if observed is None:
                    self._cleanup_unconfirmed_staging(
                        plan, run_context.stager, effective_plan, run_context, errors,
                    )
                    break

                if tail_retry:
                    delayed_failures.pop(initial_plan.key, None)

                if observed.status is BatchStatus.FAILED:
                    message = observed.error or "Album 发送失败"
                    self._log(sink, "ERROR", message)
                    stage = "request_rejected" if observed._unsubmitted_rejection() is not None else "send_failed"
                    batch = UploadBatchResult(
                        plan.key, BatchStatus.FAILED, message_ids=observed.message_ids,
                        error=message, deferred_items=batch_deferred, failure_stage=stage,
                    )
                    tail_delay = observed.album_retry_delay(
                        len(ready_items), complete_album=len(ready_items) == len(plan.items),
                    ) if reserve_tail_attempt and not tail_retry and not cancelled else None
                    if tail_delay is not None:
                        retry_at = time.monotonic() + max(tail_delay, retry_policy.delay_seconds)
                        delayed_failures[initial_plan.key] = (observed, batch, ready_items, retry_at)
                        tail.append((plan_index, initial_plan, True))
                        self._log(sink, "INFO", f"Album {plan.key} 已排入队尾重试")
                    else:
                        failed_items.extend(ready_items)
                        errors.append(f"Album {plan.key} 发送失败：{message}")
                        record_batch(batch, prep, failed=ready_items)
                    if run_context.stager is not None and effective_plan is not None:
                        try:
                            self._cleanup(run_context.stager, effective_plan, run_context, confirmed=False)
                        except Exception as cleanup_error:
                            errors.append(f"Album {plan.key} 暂存清理失败：{cleanup_error}")
                    if cancelled or (paused := should_pause()):
                        break
                    continue

                try:
                    if not observed.preserve_journal and observed.status is not BatchStatus.PREPARED and (
                        observed.message_ids or observed.status is not BatchStatus.UNKNOWN
                    ):
                        self._journal_call(
                            journal,
                            "submitted",
                            kind,
                            plan.key,
                            observed.message_ids,
                            target=run_context.target,
                        )
                    if observed.status is BatchStatus.UNKNOWN and not observed.preserve_journal:
                        self._journal_call(
                            journal,
                            "update",
                            kind,
                            plan.key,
                            BatchStatus.UNKNOWN.value,
                            error=observed.error or "Album 发送结果无法确认",
                            succeeded_ids=observed.succeeded_ids or None,
                            failed_ids=observed.failed_ids or None,
                            pending_ids=observed.pending_ids or None,
                            item_results=observed.item_results or None,
                            target=run_context.target,
                        )
                    elif observed.status is BatchStatus.PREPARED and not observed.preserve_journal:
                        self._journal_call(
                            journal,
                            "update",
                            kind,
                            plan.key,
                            BatchStatus.PREPARED.value,
                            error=observed.error or "发送器仍处于 PREPARED",
                            target=run_context.target,
                        )
                except Exception as journal_error:
                    message = f"Album {plan.key} 发送日志状态不完整：{journal_error}"
                    errors.append(message)
                    record_batch(
                        UploadBatchResult(
                            plan.key,
                            BatchStatus.UNKNOWN,
                            message_ids=observed.message_ids,
                            error=message,
                            failure_stage="unknown",
                        ),
                        prep,
                    )
                    ambiguous = True
                    if cancelled or (paused := should_pause(unknown=True)):
                        break
                    continue

                if observed.status is not BatchStatus.CONFIRMED:
                    message = observed.error or "Album 发送结果无法确认"
                    errors.append(f"Album {plan.key} 发送结果未确认：{message}")
                    record_batch(
                        UploadBatchResult(
                            plan.key,
                            observed.status,
                            message_ids=observed.message_ids,
                            error=message,
                            failure_stage="unknown",
                        ),
                        prep,
                    )
                    ambiguous = True
                    if (paused := should_pause(unknown=True)):
                        break
                    continue

                try:
                    self._journal_call(
                        journal,
                        "mark_confirmed",
                        kind,
                        plan.key,
                        observed.succeeded_ids or observed.message_ids,
                        target=run_context.target,
                    )
                except Exception as journal_error:
                    message = f"Album {plan.key} 无法写入 CONFIRMED 日志：{journal_error}"
                    errors.append(message)
                    record_batch(
                        UploadBatchResult(
                            plan.key,
                            BatchStatus.UNKNOWN,
                            message_ids=observed.message_ids,
                            error=message,
                            failure_stage="unknown",
                        ),
                        prep,
                    )
                    ambiguous = True
                    if (paused := should_pause(unknown=True)):
                        break
                    continue

                ids = observed.succeeded_ids or observed.message_ids
                try:
                    complete_album = getattr(journal, "complete_album", None)
                    committed = callable(complete_album) and complete_album(
                        state, [_item_payload(item) for item in ready_items], ids,
                        kind=kind, album_key=plan.key, target=run_context.target,
                        queue_task_id=run_context.queue_task_id,
                    )
                    if not committed:
                        self._state_mark_completed(state, ready_items, ids)
                except Exception as state_error:
                    # CONFIRMED remains durable and blocks a duplicate until the
                    # checkpoint can be repaired; do not finalize this journal.
                    message = f"Album {plan.key} 状态保存失败：{state_error}"
                    errors.append(message)
                    confirmed_recovery_failed = True
                    record_batch(
                        UploadBatchResult(
                            plan.key,
                            BatchStatus.CONFIRMED,
                            message_ids=ids,
                            confirmed_items=ready_items,
                            error=message,
                            failure_stage="persistence",
                        ),
                        prep,
                    )
                    if (paused := should_pause(unknown=True)):
                        break
                    continue

                finalize_error = None
                if not committed:
                    try:
                        self._journal_call(
                            journal,
                            "finalize",
                            kind,
                            plan.key,
                            target=run_context.target,
                        )
                    except Exception as cleanup_error:
                        finalize_error = f"Album {plan.key} 日志确认失败：{cleanup_error}"
                        errors.append(finalize_error)
                        confirmed_recovery_failed = True

                record_batch(
                    UploadBatchResult(
                        plan.key,
                        BatchStatus.CONFIRMED,
                        message_ids=ids,
                        confirmed_items=ready_items,
                        error=finalize_error,
                        failure_stage="persistence" if finalize_error else None,
                    ),
                    prep,
                )

                if run_context.stager is not None:
                    try:
                        self._cleanup(
                            run_context.stager,
                            effective_plan,
                            run_context,
                            confirmed=True,
                        )
                    except Exception as cleanup_error:
                        errors.append(f"Album {plan.key} 暂存清理失败：{cleanup_error}")

                if prep.managed_processed_paths:
                    try:
                        from ..media.video_prepare import cleanup_processed_group
                        cleanup_processed_group(plan.key, prep.managed_processed_paths)
                    except Exception as cleanup_error:
                        errors.append(f"Album {plan.key} 临时视频清理失败：{cleanup_error}")
                confirmed_count += 1
                confirmed = getattr(run_context.sender, "confirmed", None)
                if callable(confirmed):
                    try:
                        confirmed(ready_items)
                    except Exception as exc:
                        self._log(sink, "WARNING", f"无法更新 Album 进度：{exc}")
                self._emit(
                    sink,
                    ProgressEvent(
                        kind=kind,
                        phase="upload",
                        completed=confirmed_count,
                        total=pending_total,
                        path=str(ready_items[-1].path),
                        message=f"Album {plan.key} 已确认",
                    ),
                )
                if cancelled:
                    break
                if finalize_error and (paused := should_pause(unknown=True)):
                    break
        finally:
            if active_prep is not None and active_prep.remote_album is not None:
                active_prep.remote_album.close()
            pending_unconsumed: list[Future[_PreparedBatch]] = []
            for index, (future, prefetch_token, _) in prefetched.items():
                if index != consumed_index:
                    self._cancel_prefetch(future, prefetch_token, run_context)
                    if not future.done():
                        pending_unconsumed.append(future)
            if pending_unconsumed and not _is_cancel_requested(token):
                wait(pending_unconsumed, timeout=1.0)
            executor.shutdown(wait=False, cancel_futures=True)
            io_executor.shutdown(wait=False, cancel_futures=True)

        for _, batch, items, _ in delayed_failures.values():
            record_batch(batch, failed=items)
            failed_items.extend(items)
            errors.append(f"Album {batch.album_key} 发送失败：{batch.error}")

        if cancelled:
            return UploadRunResult(
                RUN_UNKNOWN if ambiguous else RUN_CANCELLED,
                batches=tuple(batches),
                error="；".join(errors) or "上传已取消",
                deferred_items=tuple(deferred_items),
                failed_items=tuple(failed_items),
                scanned_items=scanned_items,
                cancelled=True,
            )

        if ambiguous:
            status = RUN_UNKNOWN
        elif confirmed_recovery_failed:
            # Telegram already confirmed this Album.  A local checkpoint
            # failure is actionable recovery work, not a failed send.
            status = RUN_PARTIAL
        elif confirmed_count == 0 and not deferred_items and (errors or failed_items):
            status = RUN_FAILED
        elif errors or deferred_items or failed_items:
            status = RUN_PARTIAL
        else:
            status = RUN_COMPLETED
        return UploadRunResult(
            status,
            paused=paused,
            batches=tuple(batches),
            error="；".join(errors) if errors else None,
            deferred_items=tuple(deferred_items),
            failed_items=tuple(failed_items),
            scanned_items=scanned_items,
            cancelled=False,
        )


__all__ = [
    "JournalStore",
    "MemoryJournalStore",
    "MemoryStateStore",
    "RUN_CANCELLED",
    "RUN_COMPLETED",
    "RUN_FAILED",
    "RUN_PARTIAL",
    "RUN_UNKNOWN",
    "Sender",
    "StateStore",
    "Stager",
    "UploadCancelled",
    "UploadEngine",
]
