"""Small shared adapters for media strategy boundaries.

Media scanners and builders keep their own grouping, snapshot fallback and
outcome rules; these helpers only translate the protocols they share.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..contracts import CancelToken, EventSink
from ..core.models import FileSnapshot, LogEvent, MediaItem


class StrategyCancelled(RuntimeError):
    """Cancellation recognized by the upload engine at a strategy boundary."""

    cancelled = True


def is_cancelled(token: CancelToken | None) -> bool:
    if token is None:
        return False
    checker = getattr(token, "is_cancelled", None)
    if callable(checker):
        return bool(checker())
    return bool(getattr(token, "cancelled", False))


def check_cancelled(token: CancelToken | None, *, message: str) -> None:
    if is_cancelled(token):
        raise StrategyCancelled(message)
    if token is None:
        return
    raiser = getattr(token, "raise_if_cancelled", None)
    if callable(raiser):
        try:
            raiser()
        except BaseException as error:
            if is_cancelled(token) or bool(getattr(error, "cancelled", False)):
                raise StrategyCancelled(str(error) or message) from error
            raise


class CancelEventBridge:
    """Expose a cancellation token through the threading.Event read protocol."""

    def __init__(self, token: CancelToken | None):
        self.token = token

    def is_set(self) -> bool:
        return is_cancelled(self.token)


def emit_event(event_sink: EventSink | None, event: Any) -> None:
    emit = getattr(event_sink, "emit", None)
    if callable(emit):
        try:
            emit(event)
        except Exception:
            # Presentation must not change a media decision.
            pass


class StrategyLogUI:
    """Forward builder diagnostics without importing GUI widgets."""

    def __init__(self, event_sink: EventSink | None, *, source: str):
        self.event_sink = event_sink
        self.source = source

    def _emit(self, level: str, message: Any) -> None:
        emit = getattr(self.event_sink, "emit", None)
        if not callable(emit):
            return
        try:
            emit(LogEvent(level=level, message=str(message), source=self.source))
        except Exception:
            # Message conversion and presentation cannot change a decision.
            pass

    def warning(self, message: Any) -> None:
        self._emit("WARNING", message)

    def info(self, message: Any) -> None:
        self._emit("INFO", message)

    def log(self, message: Any) -> None:
        self._emit("INFO", message)


def model_identity(item: MediaItem) -> tuple[object, ...]:
    return (
        str(Path(item.path)),
        str(Path(item.source_root)),
        str(item.media_kind),
        item.snapshot.path,
        item.snapshot.size,
        item.snapshot.mtime_ns,
    )


def raw_snapshot(raw: Mapping[str, Any], path: Path, *, item_label: str) -> FileSnapshot:
    value = raw.get("snapshot")
    if isinstance(value, FileSnapshot):
        return value
    if value is not None:
        size = getattr(value, "size", None)
        mtime_ns = getattr(value, "mtime_ns", None)
        snapshot_path = getattr(value, "path", str(path))
        if size is None or mtime_ns is None:
            try:
                size, mtime_ns = value[0], value[1]
            except (IndexError, KeyError, TypeError):
                size = mtime_ns = None
        if size is not None and mtime_ns is not None:
            return FileSnapshot(str(snapshot_path), int(size), int(mtime_ns))

    size = raw.get("scan_size", raw.get("size"))
    mtime_ns = raw.get("scan_mtime_ns", raw.get("mtime_ns"))
    if size is None or mtime_ns is None:
        raise ValueError(f"{item_label}项缺少扫描快照：{path}")
    return FileSnapshot(str(path), int(size), int(mtime_ns))


def state_completed(
    state: Any,
    item: MediaItem,
    item_converter: Callable[[MediaItem], Mapping[str, Any]],
) -> bool:
    if state is None:
        return False
    checker = getattr(state, "is_completed", None)
    if not callable(checker):
        raise TypeError("state 必须提供 is_completed()")
    pipeline_item = item_converter(item)
    for candidate in (item, pipeline_item):
        try:
            if bool(checker(candidate)):
                return True
        except (OSError, TypeError, ValueError, AttributeError):
            continue
    return False


def snapshot_item(item: MediaItem) -> dict[str, Any]:
    """Copy the snapshot fields accepted by pipeline checkpoint callbacks."""

    return {
        "path": item.path,
        "source_root": item.source_root,
        "media_kind": item.media_kind,
        "scan_size": item.snapshot.size,
        "scan_mtime_ns": item.snapshot.mtime_ns,
    }


def target_dict(target: Any) -> dict[str, Any]:
    if isinstance(target, Mapping):
        return dict(target)
    converter = getattr(target, "as_dict", None)
    if callable(converter):
        value = converter()
        return dict(value) if isinstance(value, Mapping) else {}
    return {}


def build_item_lookup(
    candidates: Sequence[MediaItem],
    normalize_path: Callable[[Any], str],
    *,
    kind: str,
    item_label: str,
) -> dict[str, dict]:
    by_identity: dict[tuple[object, ...], MediaItem] = {}
    by_path: dict[tuple[str, str], list[MediaItem]] = {}
    for item in candidates:
        normalized_path = normalize_path(item.path)
        media_kind = str(item.media_kind or kind)
        key = (
            normalized_path,
            int(item.snapshot.size),
            int(item.snapshot.mtime_ns),
            media_kind,
        )
        if key in by_identity:
            raise ValueError(f"{item_label} MediaItem identity 重复：{item.path}")
        by_identity[key] = item
        by_path.setdefault((normalized_path, media_kind), []).append(item)
    return {"by_identity": by_identity, "by_path": by_path}
