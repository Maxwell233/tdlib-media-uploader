"""Main-Agent-owned UploadEngine reference implementation.

The engine owns the generic lifecycle shared by image, mixed and video
strategies: state filtering, Album-boundary preservation, preflight, optional
staging, journal transitions, transport invocation and durable checkpointing.
Media-specific code is injected through :class:`MediaStrategy` and the small
collaborator protocols below.  The reference stores are intentionally
in-memory; production adapters can wrap the existing V1.9 state, journal and
TDLib implementations without making this package import GUI code.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import inspect
import json
from pathlib import Path
from typing import Any, Protocol

from ..contracts import CancelToken, EventSink, MediaStrategy, UploadContext
from ..core.identity import canonical_target
from ..core.models import (
    AlbumPlan,
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


RUN_COMPLETED = "COMPLETED"
RUN_PARTIAL = "PARTIAL"
RUN_FAILED = "FAILED"
RUN_CANCELLED = "CANCELLED"
RUN_UNKNOWN = "UNKNOWN"

# These are the only durable per-Album states.  Run-level summaries above are
# deliberately separate and must never be written into a journal record.
PREPARED = BatchStatus.PREPARED.value
SUBMITTED = BatchStatus.SUBMITTED.value
CONFIRMED = BatchStatus.CONFIRMED.value
FAILED = BatchStatus.FAILED.value
UNKNOWN = BatchStatus.UNKNOWN.value
SEND_STATES = (PREPARED, SUBMITTED, CONFIRMED, FAILED, UNKNOWN)
SEND_STATE_FLOW = (PREPARED, SUBMITTED, CONFIRMED)


class UploadCancelled(RuntimeError):
    """Cooperative cancellation before a send outcome is ambiguous."""

    cancelled = True


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


def _as_ids(values: Any) -> tuple[int, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        values = (values,)
    result: list[int] = []
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            result.append(int(value))
        except (TypeError, ValueError, OverflowError):
            continue
    return tuple(result)


def _call_supported(function: Callable[..., Any], args: Sequence[Any] = (), **kwargs: Any) -> Any:
    """Call a collaborator while allowing small fakes with fewer keywords."""

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*args, **kwargs)

    parameters = signature.parameters
    positional = list(args)
    accepted: dict[str, Any] = {}
    var_keyword = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    for name, value in kwargs.items():
        parameter = parameters.get(name)
        if parameter is not None and parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            positional.append(value)
        elif parameter is not None or var_keyword:
            accepted[name] = value
    return function(*positional, **accepted)


def _is_cancel_requested(token: CancelToken) -> bool:
    checker = getattr(token, "is_cancelled", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return bool(getattr(token, "cancelled", False))


def _stop_after_current_requested(token: CancelToken) -> bool:
    """Return whether the caller requested a cooperative Album-boundary stop."""

    checker = getattr(token, "stop_after_current", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
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


@dataclass(frozen=True, slots=True)
class _ObservedSend:
    status: BatchStatus
    message_ids: tuple[int, ...] = ()
    succeeded_ids: tuple[int, ...] = ()
    failed_ids: tuple[int, ...] = ()
    pending_ids: tuple[int, ...] = ()
    error: str | None = None


def _normalize_status(value: Any) -> BatchStatus | None:
    if isinstance(value, BatchStatus):
        return value
    if value is None:
        return None
    aliases = {
        "SUCCESS": BatchStatus.CONFIRMED,
        "SUCCEEDED": BatchStatus.CONFIRMED,
        "COMPLETE": BatchStatus.CONFIRMED,
        "COMPLETED": BatchStatus.CONFIRMED,
        "PARTIAL": BatchStatus.UNKNOWN,
        "ERROR": BatchStatus.FAILED,
        "FAIL": BatchStatus.FAILED,
    }
    raw = str(value).strip().upper()
    return aliases.get(raw, BatchStatus._value2member_map_.get(raw))


def _normalize_send_result(value: Any, expected_count: int) -> _ObservedSend:
    succeeded: tuple[int, ...] = ()
    failed: tuple[int, ...] = ()
    pending: tuple[int, ...] = ()
    message_ids: tuple[int, ...] = ()
    error: str | None = None
    status: BatchStatus | None = None

    if isinstance(value, Mapping):
        status = _normalize_status(value.get("status"))
        succeeded = _as_ids(value.get("succeeded_ids", value.get("succeeded")))
        failed = _as_ids(value.get("failed_ids", value.get("failed")))
        pending = _as_ids(value.get("pending_ids", value.get("pending")))
        message_ids = _as_ids(value.get("message_ids"))
        error_value = value.get("error", value.get("message"))
        error = str(error_value) if error_value else None
    elif isinstance(value, UploadBatchResult):
        status = _normalize_status(value.status)
        message_ids = _as_ids(value.message_ids)
        succeeded = message_ids
        error = value.error
    elif value is not None and not isinstance(value, (str, bytes)):
        status = _normalize_status(getattr(value, "status", None))
        succeeded = _as_ids(
            getattr(value, "succeeded_ids", getattr(value, "confirmed_ids", getattr(value, "succeeded", ())))
        )
        failed = _as_ids(getattr(value, "failed_ids", getattr(value, "failed", ())))
        pending = _as_ids(getattr(value, "pending_ids", getattr(value, "pending", ())))
        message_ids = _as_ids(getattr(value, "message_ids", ()))
        error_value = getattr(value, "error", None)
        error = str(error_value) if error_value else None
        if status is None and isinstance(value, Sequence):
            message_ids = _as_ids(value)
            succeeded = message_ids
    elif isinstance(value, (str, bytes)):
        error = str(value)

    if not message_ids:
        message_ids = succeeded + failed + pending
    if status is None:
        if len(succeeded) == expected_count and not failed and not pending and expected_count:
            status = BatchStatus.CONFIRMED
        elif failed and not succeeded and not pending:
            status = BatchStatus.FAILED
        else:
            status = BatchStatus.UNKNOWN

    if status is BatchStatus.CONFIRMED and (failed or pending):
        status = BatchStatus.UNKNOWN
        error = error or "确认结果包含失败或待确认消息"
    if status is BatchStatus.CONFIRMED and len(succeeded or message_ids) != expected_count:
        status = BatchStatus.UNKNOWN
        error = error or "发送器确认的消息数量少于 Album 项数"
    if status is BatchStatus.FAILED and (succeeded or pending):
        status = BatchStatus.UNKNOWN
        error = error or "发送结果同时包含成功或待确认消息"
    if status is BatchStatus.CONFIRMED and not succeeded:
        succeeded = message_ids
    return _ObservedSend(status, message_ids, succeeded, failed, pending, error)


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

    def prepare(self, kind: str, album_key: str, items=None, *, target=None):
        record = {
            "kind": str(kind).strip().lower(),
            "album_key": str(album_key),
            "status": BatchStatus.PREPARED.value,
            "items": list(items or []),
            "message_ids": [],
            "target": dict(target or {}),
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
    """Reference implementation of the V2 generic upload lifecycle."""

    def __init__(
        self,
        *,
        sender: Sender | Any | None = None,
        state: StateStore | Any | None = None,
        journal: JournalStore | Any | None = None,
        stager: Stager | Any | None = None,
        preflight: Callable[..., Any] | Any | None = None,
        state_factory: Callable[[UploadContext], Any] | None = None,
        journal_factory: Callable[[UploadContext], Any] | None = None,
    ):
        self.sender = sender
        self.state = state
        self.journal = journal
        self.stager = stager
        self.preflight = preflight
        self.state_factory = state_factory
        self.journal_factory = journal_factory

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
        )

    @staticmethod
    def _make_collaborator(
        current: Any | None,
        factory: Callable[[UploadContext], Any] | None,
        context: UploadContext,
        fallback: Callable[[], Any],
    ) -> Any:
        if current is not None:
            return current
        if factory is not None:
            try:
                signature = inspect.signature(factory)
            except (TypeError, ValueError):
                value = factory(context)
            else:
                positional = [
                    parameter
                    for parameter in signature.parameters.values()
                    if parameter.kind
                    in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                ]
                if positional:
                    value = factory(context)
                elif "context" in signature.parameters:
                    value = factory(context=context)
                else:
                    value = factory()
            if value is not None:
                return value
        return fallback()

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
    def _send(sender: Any, contents: Sequence[Mapping[str, Any]], plan: AlbumPlan, context: UploadContext) -> Any:
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
        )

    @staticmethod
    def _stage(stager: Any, plan: AlbumPlan, context: UploadContext) -> AlbumPlan:
        function = getattr(stager, "stage", None)
        if not callable(function):
            function = getattr(stager, "stage_plan", None)
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

        state = self._make_collaborator(
            run_context.state,
            self.state_factory,
            run_context,
            MemoryStateStore,
        )
        run_context = replace(run_context, state=state)
        journal = self._make_collaborator(
            run_context.journal,
            self.journal_factory,
            run_context,
            MemoryJournalStore,
        )
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

        try:
            _check_cancel(token)
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
            raw_plans = _call_supported(
                getattr(strategy, "build_plans"),
                (scan_result,),
                target=run_context.target,
                state=state,
                context=run_context,
            )
            plans = tuple(raw_plans)
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

        for plan in plans:
            if _stop_after_current_requested(token):
                cancelled = True
                break
            plan_deferred: tuple[MediaItem, ...] = ()
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
                    errors.append(message)
                    batches.append(
                        UploadBatchResult(plan.key, BatchStatus.UNKNOWN, error=message)
                    )
                    ambiguous = True
                    continue
                if unresolved is not None:
                    status = str(unresolved.get("status", "UNKNOWN")).upper()
                    if status == CONFIRMED:
                        from .reconciliation import ReconciliationService

                        try:
                            repaired = ReconciliationService(journal).recover_confirmed(
                                kind,
                                plan.key,
                                target=run_context.target,
                                state=state,
                                record=unresolved,
                            )
                        except Exception as recovery_error:
                            message = f"Album {plan.key} CONFIRMED 断点恢复失败：{recovery_error}"
                            self._log(sink, "ERROR", message)
                            errors.append(message)
                            confirmed_recovery_failed = True
                            batches.append(
                                UploadBatchResult(
                                    plan.key,
                                    BatchStatus.CONFIRMED,
                                    error=message,
                                )
                            )
                            continue
                        if repaired:
                            self._log(sink, "INFO", f"Album {plan.key} 已恢复本地断点并清理保护记录")
                            continue
                    message = (
                        f"Album {plan.key} 的发送状态为 {status}，"
                        "为避免重复上传已阻止自动重试"
                    )
                    self._log(sink, "WARNING", message)
                    errors.append(message)
                    batches.append(UploadBatchResult(plan.key, BatchStatus.UNKNOWN, error=message))
                    ambiguous = True
                    continue
                pending = tuple(
                    item for item in plan.pending_items if not self._state_completed(state, item)
                )
                current_plan = restrict_plan(plan, pending)
            except Exception as error:
                message = f"Album {getattr(plan, 'key', '<unknown>')} 状态检查失败：{error}"
                errors.append(message)
                batches.append(
                    UploadBatchResult(
                        album_key=str(getattr(plan, "key", "<unknown>")),
                        status=BatchStatus.FAILED,
                        error=message,
                    )
                )
                failed_items.extend(tuple(getattr(plan, "pending_items", ())))
                continue

            if not current_plan.pending_items:
                continue

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
                        cancelled = True
                        break
                    message = f"Album {plan.key} 预检失败：{error}"
                    errors.append(message)
                    failed_items.extend(plan.pending_items)
                    batches.append(
                        UploadBatchResult(plan.key, BatchStatus.FAILED, error=message)
                    )
                    continue
                plan_deferred = preflight_result.deferred_items
                deferred_items.extend(plan_deferred)
                failed_items.extend(preflight_result.failed_items)
                if preflight_result.cancelled:
                    cancelled = True
                    break
                if preflight_result.failed_items:
                    reason = "; ".join(
                        reason
                        for path, reason in preflight_result.reasons
                        if any(str(item.path) == path for item in preflight_result.failed_items)
                    ) or "预检失败"
                    if not current_plan.pending_items:
                        batches.append(
                            UploadBatchResult(
                                plan.key,
                                BatchStatus.FAILED,
                                error=reason,
                                deferred_items=preflight_result.deferred_items,
                            )
                        )
                        continue
                if not current_plan.pending_items:
                    continue

            ready_items = tuple(current_plan.pending_items)
            batch_deferred = tuple(plan_deferred)
            effective_plan = current_plan
            if run_context.stager is not None:
                try:
                    effective_plan = self._stage(run_context.stager, current_plan, run_context)
                except Exception as error:
                    message = f"Album {plan.key} 暂存失败：{error}"
                    errors.append(message)
                    failed_items.extend(ready_items)
                    batches.append(
                        UploadBatchResult(
                            plan.key,
                            BatchStatus.FAILED,
                            error=message,
                            deferred_items=plan_deferred,
                        )
                    )
                    continue

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
                deferred_items.extend(build_deferred)
                failed_items.extend(build_failed)
                for build_error in build_errors:
                    errors.append(f"Album {plan.key} 内容构建提示：{build_error}")
                if not ready_items:
                    if run_context.stager is not None:
                        try:
                            self._cleanup(
                                run_context.stager,
                                effective_plan,
                                run_context,
                                confirmed=False,
                            )
                        except Exception as cleanup_error:
                            errors.append(f"Album {plan.key} 暂存清理失败：{cleanup_error}")
                    if build_failed:
                        message = "; ".join(build_errors) or "内容构建失败"
                        errors.append(f"Album {plan.key} 内容构建失败：{message}")
                        batches.append(
                            UploadBatchResult(
                                plan.key,
                                BatchStatus.FAILED,
                                error=message,
                                deferred_items=batch_deferred,
                            )
                        )
                    continue
                if not contents:
                    raise ValueError("不能发送空的 Telegram Album")
            except Exception as error:
                if _is_cancel_error(error, token):
                    cancelled = True
                    break
                message = f"Album {plan.key} 内容构建失败：{error}"
                errors.append(message)
                failed_items.extend(ready_items)
                if run_context.stager is not None:
                    try:
                        self._cleanup(
                            run_context.stager,
                            effective_plan,
                            run_context,
                            confirmed=False,
                        )
                    except Exception as cleanup_error:
                        errors.append(f"Album {plan.key} 暂存清理失败：{cleanup_error}")
                batches.append(UploadBatchResult(plan.key, BatchStatus.FAILED, error=message))
                continue

            if _stop_after_current_requested(token):
                if run_context.stager is not None:
                    try:
                        self._cleanup(
                            run_context.stager,
                            effective_plan,
                            run_context,
                            confirmed=False,
                        )
                    except Exception as cleanup_error:
                        errors.append(f"Album {plan.key} 暂存清理失败：{cleanup_error}")
                cancelled = True
                break

            send_plan = restrict_plan(effective_plan, ready_items)

            if run_context.sender is None:
                message = "UploadEngine 未配置 sender，未发起 Telegram 请求"
                errors.append(f"Album {plan.key} {message}")
                failed_items.extend(ready_items)
                batches.append(UploadBatchResult(plan.key, BatchStatus.FAILED, error=message))
                continue

            try:
                _check_cancel(token)
                self._journal_call(
                    journal,
                    "prepare",
                    kind,
                    plan.key,
                    [_item_payload(item) for item in ready_items],
                    target=run_context.target,
                )
            except Exception as error:
                if _is_cancel_error(error, token):
                    cancelled = True
                    break
                message = f"Album {plan.key} 无法写入 PREPARED 日志：{error}"
                errors.append(message)
                failed_items.extend(ready_items)
                batches.append(UploadBatchResult(plan.key, BatchStatus.FAILED, error=message))
                continue

            if _is_cancel_requested(token) or _stop_after_current_requested(token):
                # The sender has not been called yet, so the PREPARED guard is
                # safe to remove.  If deletion itself fails, retain the guard
                # and let the next run fail closed instead of risking resend.
                try:
                    self._journal_call(
                        journal,
                        "finalize",
                        kind,
                        plan.key,
                        target=run_context.target,
                    )
                except Exception as cleanup_error:
                    errors.append(f"Album {plan.key} 停止前清理 PREPARED 日志失败：{cleanup_error}")
                cancelled = True
                break

            observed: _ObservedSend
            try:
                raw_result = self._send(run_context.sender, contents, send_plan, run_context)
                observed = _normalize_send_result(raw_result, len(ready_items))
                if _is_cancel_requested(token) and observed.status is not BatchStatus.CONFIRMED:
                    cancelled = True
                    observed = replace(
                        observed,
                        status=BatchStatus.UNKNOWN,
                        error=observed.error or "发送后取消，结果无法确认",
                    )
            except Exception as error:
                sender_cancelled = _is_cancel_error(error, token)
                if sender_cancelled:
                    cancelled = True
                submitted = getattr(error, "submitted", None)
                status = BatchStatus.FAILED if submitted is False else BatchStatus.UNKNOWN
                observed = _ObservedSend(status, error=str(error) or status.value)

            if observed.status is BatchStatus.FAILED:
                try:
                    self._journal_call(
                        journal,
                        "failed",
                        kind,
                        plan.key,
                        observed.error or "Album 发送失败",
                        target=run_context.target,
                    )
                except Exception as journal_error:
                    errors.append(f"Album {plan.key} FAILED 日志写入失败：{journal_error}")
                failed_items.extend(ready_items)
                if observed.error:
                    errors.append(f"Album {plan.key} 发送失败：{observed.error}")
                batches.append(
                    UploadBatchResult(
                        plan.key,
                        BatchStatus.FAILED,
                        message_ids=observed.message_ids,
                        error=observed.error,
                        deferred_items=batch_deferred,
                    )
                )
                continue

            try:
                if observed.status is not BatchStatus.PREPARED:
                    self._journal_call(
                        journal,
                        "submitted",
                        kind,
                        plan.key,
                        observed.message_ids,
                        target=run_context.target,
                    )
                if observed.status is BatchStatus.UNKNOWN:
                    self._journal_call(
                        journal,
                        "unknown",
                        kind,
                        plan.key,
                        observed.error or "Album 发送结果无法确认",
                        target=run_context.target,
                    )
                elif observed.status is BatchStatus.PREPARED:
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
                batches.append(
                    UploadBatchResult(
                        plan.key,
                        BatchStatus.UNKNOWN,
                        message_ids=observed.message_ids,
                        error=message,
                    )
                )
                ambiguous = True
                if cancelled:
                    break
                continue

            if observed.status is not BatchStatus.CONFIRMED:
                message = observed.error or "Album 发送结果无法确认"
                errors.append(f"Album {plan.key} 发送结果未确认：{message}")
                batches.append(
                    UploadBatchResult(
                        plan.key,
                        observed.status,
                        message_ids=observed.message_ids,
                        error=message,
                    )
                )
                ambiguous = True
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
                batches.append(
                    UploadBatchResult(
                        plan.key,
                        BatchStatus.UNKNOWN,
                        message_ids=observed.message_ids,
                        error=message,
                    )
                )
                ambiguous = True
                continue

            ids = observed.succeeded_ids or observed.message_ids
            try:
                self._state_mark_completed(state, ready_items, ids)
            except Exception as state_error:
                # CONFIRMED remains durable and blocks a duplicate until the
                # checkpoint can be repaired; do not finalize this journal.
                message = f"Album {plan.key} 状态保存失败：{state_error}"
                errors.append(message)
                batches.append(
                    UploadBatchResult(
                        plan.key,
                        BatchStatus.CONFIRMED,
                        message_ids=ids,
                        confirmed_items=ready_items,
                        error=message,
                    )
                )
                continue

            try:
                self._journal_call(
                    journal,
                    "finalize",
                    kind,
                    plan.key,
                    target=run_context.target,
                )
                if run_context.stager is not None:
                    self._cleanup(
                        run_context.stager,
                        effective_plan,
                        run_context,
                        confirmed=True,
                    )
            except Exception as cleanup_error:
                # The send and state checkpoint are complete.  Keep the
                # successful batch but surface cleanup/finalization failure.
                errors.append(f"Album {plan.key} 收尾失败：{cleanup_error}")
            batches.append(
                UploadBatchResult(
                    plan.key,
                    BatchStatus.CONFIRMED,
                    message_ids=ids,
                    confirmed_items=ready_items,
                )
            )
            confirmed_count += 1
            self._emit(
                sink,
                ProgressEvent(
                    kind=kind,
                    phase="upload",
                    completed=confirmed_count,
                    total=len(plans),
                    path=str(ready_items[-1].path),
                    message=f"Album {plan.key} 已确认",
                ),
            )

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
            batches=tuple(batches),
            error="；".join(errors) if errors else None,
            deferred_items=tuple(deferred_items),
            failed_items=tuple(failed_items),
            scanned_items=scanned_items,
            cancelled=False,
        )


ReferenceUploadEngine = UploadEngine


__all__ = [
    "JournalStore",
    "MemoryJournalStore",
    "MemoryStateStore",
    "CONFIRMED",
    "FAILED",
    "PREPARED",
    "RUN_CANCELLED",
    "RUN_COMPLETED",
    "RUN_FAILED",
    "RUN_PARTIAL",
    "RUN_UNKNOWN",
    "SEND_STATES",
    "SEND_STATE_FLOW",
    "Sender",
    "StateStore",
    "Stager",
    "SUBMITTED",
    "UNKNOWN",
    "UploadCancelled",
    "UploadEngine",
    "ReferenceUploadEngine",
]
