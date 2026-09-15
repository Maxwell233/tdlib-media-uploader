"""Pure preflight decisions for the generic upload lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import inspect
from collections.abc import Mapping
from typing import Any, Callable, Iterable

from ..contracts import CancelToken, EventSink, UploadContext
from ..core.models import AlbumPlan, MediaItem, ProgressEvent
from .planner import restrict_plan, validate_plan


class PreflightStatus(str, Enum):
    """The three non-transport outcomes for one source item."""

    READY = "READY"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class PreflightDecision:
    """A checker decision that cannot be confused with a send state."""

    status: PreflightStatus
    reason: str = ""

    @classmethod
    def ready(cls, reason: str = "") -> "PreflightDecision":
        return cls(PreflightStatus.READY, str(reason))

    @classmethod
    def deferred(cls, reason: str = "") -> "PreflightDecision":
        return cls(PreflightStatus.DEFERRED, str(reason))

    @classmethod
    def failed(cls, reason: str = "") -> "PreflightDecision":
        return cls(PreflightStatus.FAILED, str(reason))


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """Partition of the exact input sequence without changing its order."""

    original_items: tuple[MediaItem, ...]
    ready_items: tuple[MediaItem, ...] = ()
    deferred_items: tuple[MediaItem, ...] = ()
    failed_items: tuple[MediaItem, ...] = ()
    reasons: tuple[tuple[str, str], ...] = ()
    cancelled: bool = False

    def reason_for(self, item: MediaItem) -> str:
        path = str(item.path)
        for candidate, reason in self.reasons:
            if candidate == path:
                return reason
        return ""


def _is_cancelled(token: CancelToken | None) -> bool:
    if token is None:
        return False
    checker = getattr(token, "is_cancelled", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return bool(getattr(token, "cancelled", False))


def _looks_cancelled(error: BaseException) -> bool:
    return bool(getattr(error, "cancelled", False)) or isinstance(error, (KeyboardInterrupt,))


def _raise_if_cancelled(token: CancelToken | None) -> None:
    if token is None:
        return
    raiser = getattr(token, "raise_if_cancelled", None)
    if callable(raiser):
        raiser()
    elif _is_cancelled(token):
        raise RuntimeError("预检已取消")


def _call_checker(
    checker: Callable[..., Any] | Any,
    item: MediaItem,
    context: UploadContext | None,
) -> Any:
    target = getattr(checker, "check", checker)
    if not callable(target):
        raise TypeError("预检器必须是可调用对象或提供 check() 方法")
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return target(item, context=context) if context is not None else target(item)

    parameters = signature.parameters
    accepts_context = "context" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if accepts_context:
        return target(item, context=context)
    return target(item)


def _normalize_decision(value: Any) -> PreflightDecision:
    if isinstance(value, PreflightDecision):
        return value
    if isinstance(value, bool):
        return PreflightDecision.ready() if value else PreflightDecision.deferred()
    if value is None:
        return PreflightDecision.ready()

    raw_status: Any = None
    reason: Any = ""
    if isinstance(value, Mapping):
        raw_status = value.get("status", value.get("decision", value.get("ready")))
        reason = value.get("reason", value.get("message", ""))
        if raw_status is True:
            raw_status = PreflightStatus.READY
        elif raw_status is False:
            raw_status = PreflightStatus.DEFERRED
    else:
        raw_status = getattr(value, "status", None)
        reason = getattr(value, "reason", getattr(value, "message", ""))

    normalized = str(raw_status or PreflightStatus.READY.value).strip().upper()
    aliases = {
        "OK": PreflightStatus.READY,
        "READY": PreflightStatus.READY,
        "DEFER": PreflightStatus.DEFERRED,
        "DEFERRED": PreflightStatus.DEFERRED,
        "SKIP": PreflightStatus.DEFERRED,
        "FAILED": PreflightStatus.FAILED,
        "FAIL": PreflightStatus.FAILED,
    }
    try:
        status = aliases[normalized]
    except KeyError as exc:
        raise ValueError(f"未知预检状态：{raw_status}") from exc
    return PreflightDecision(status, str(reason or ""))


def _emit(
    event_sink: EventSink | None,
    event: ProgressEvent,
) -> None:
    emit = getattr(event_sink, "emit", None) if event_sink is not None else None
    if callable(emit):
        try:
            emit(event)
        except Exception:
            # Telemetry must not change a source decision.
            pass


def preflight_items(
    items: Iterable[MediaItem],
    checker: Callable[..., Any] | Any,
    *,
    context: UploadContext | None = None,
    cancel_token: CancelToken | None = None,
    event_sink: EventSink | None = None,
    kind: str = "",
) -> PreflightResult:
    """Classify items while preserving the strategy's original order."""

    original = tuple(items)
    ready: list[MediaItem] = []
    deferred: list[MediaItem] = []
    failed: list[MediaItem] = []
    reasons: list[tuple[str, str]] = []
    total = len(original)

    for completed, item in enumerate(original, start=1):
        try:
            _raise_if_cancelled(cancel_token)
        except BaseException as error:
            if _looks_cancelled(error) or _is_cancelled(cancel_token):
                return PreflightResult(
                    original,
                    tuple(ready),
                    tuple(deferred),
                    tuple(failed),
                    tuple(reasons),
                    cancelled=True,
                )
            raise

        try:
            decision = _normalize_decision(_call_checker(checker, item, context))
        except BaseException as error:
            if _looks_cancelled(error) or _is_cancelled(cancel_token):
                return PreflightResult(
                    original,
                    tuple(ready),
                    tuple(deferred),
                    tuple(failed),
                    tuple(reasons),
                    cancelled=True,
                )
            decision = PreflightDecision.failed(str(error))

        if decision.status is PreflightStatus.READY:
            ready.append(item)
        elif decision.status is PreflightStatus.DEFERRED:
            deferred.append(item)
        else:
            failed.append(item)
        if decision.reason:
            reasons.append((str(item.path), decision.reason))
        _emit(
            event_sink,
            ProgressEvent(
                kind=str(kind),
                phase="preflight",
                completed=completed,
                total=total,
                path=str(item.path),
                message=decision.reason,
            ),
        )

    return PreflightResult(
        original,
        tuple(ready),
        tuple(deferred),
        tuple(failed),
        tuple(reasons),
    )


def preflight_plan(
    plan: AlbumPlan,
    checker: Callable[..., Any] | Any,
    *,
    context: UploadContext | None = None,
    cancel_token: CancelToken | None = None,
    event_sink: EventSink | None = None,
) -> tuple[AlbumPlan, PreflightResult]:
    """Preflight only pending items and retain the full Album boundary."""

    validate_plan(plan)
    result = preflight_items(
        plan.pending_items,
        checker,
        context=context,
        cancel_token=cancel_token,
        event_sink=event_sink,
        kind=plan.kind,
    )
    return restrict_plan(plan, result.ready_items), result


__all__ = [
    "PreflightDecision",
    "PreflightResult",
    "PreflightStatus",
    "preflight_items",
    "preflight_plan",
]
