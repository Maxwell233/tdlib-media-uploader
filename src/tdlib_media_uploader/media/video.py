"""V2 video strategy backed by the existing V1.9 video implementation.

The migration deliberately keeps the legacy video module as the owner of
video-specific behavior: discovery, date selection, captions, readiness,
thumbnail generation and TDLib input construction.  This module is the
translation boundary that turns its dictionary-shaped values into the V2
``MediaItem``/``AlbumPlan``/``ScanResult`` contracts.  It does not send
anything to Telegram; :class:`~tdlib_media_uploader.upload.engine.UploadEngine`
owns that lifecycle.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
import importlib
import inspect
from pathlib import Path
import threading
from typing import Any

from ..contracts import CancelToken, EventSink, UploadContext
from ..core.filesystem import snapshot_file, stable_path
from ..core.models import (
    AlbumPlan,
    ContentBuildResult,
    FileSnapshot,
    LogEvent,
    MediaItem,
    ProgressEvent,
    ScanResult,
)


_MISSING = object()


def _load_legacy_module() -> Any:
    """Load the bundled video compatibility module."""

    return importlib.import_module("tdlib_media_uploader.media.legacy_video")


class _StrategyCancelled(RuntimeError):
    """Cancellation raised after a legacy call has returned control."""

    cancelled = True


class _CancelEventBridge:
    """Expose the V1.9 ``Event.is_set`` shape for a V2 cancel token."""

    def __init__(self, token: CancelToken):
        self.token = token

    def is_set(self) -> bool:
        checker = getattr(self.token, "is_cancelled", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return bool(getattr(self.token, "cancelled", False))


class _StrategyUI:
    """Small legacy UI facade that forwards diagnostics into ``EventSink``."""

    def __init__(self, event_sink: EventSink | None, kind: str = "video"):
        self.event_sink = event_sink
        self.kind = kind

    def _emit(self, event: Any) -> None:
        sink = self.event_sink
        emit = getattr(sink, "emit", None) if sink is not None else None
        if callable(emit):
            try:
                emit(event)
            except Exception:
                # A progress/log consumer must not change media behavior.
                pass

    def warning(self, message: Any) -> None:
        self._emit(LogEvent("WARNING", str(message), source="video-strategy"))

    def log(self, message: Any) -> None:
        self._emit(LogEvent("INFO", str(message), source="video-strategy"))

    def info(self, message: Any) -> None:
        self._emit(LogEvent("INFO", str(message), source="video-strategy"))

    def progress(self, **values: Any) -> None:
        try:
            completed = int(values.get("done_files", values.get("completed", 0)) or 0)
        except (TypeError, ValueError):
            completed = 0
        try:
            total = int(values.get("total_files", values.get("total", 0)) or 0)
        except (TypeError, ValueError):
            total = 0
        self._emit(
            ProgressEvent(
                kind=self.kind,
                phase="upload",
                completed=completed,
                total=total,
                message=str(values.get("detail", "") or ""),
            )
        )

    def finish(self) -> None:
        return None


def _call_supported(function: Callable[..., Any], args: Sequence[Any] = (), **kwargs: Any) -> Any:
    """Call a legacy hook while tolerating its historical small signatures."""

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


def _token_cancelled(token: CancelToken | None) -> bool:
    if token is None:
        return False
    checker = getattr(token, "is_cancelled", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return bool(getattr(token, "cancelled", False))


def _check_cancel(token: CancelToken | None) -> None:
    if _token_cancelled(token):
        raise _StrategyCancelled("视频策略操作已取消")
    if token is None:
        return
    raiser = getattr(token, "raise_if_cancelled", None)
    if callable(raiser):
        try:
            raiser()
        except BaseException as error:
            if _token_cancelled(token) or bool(getattr(error, "cancelled", False)):
                raise _StrategyCancelled(str(error) or "视频策略操作已取消") from error
            raise


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_path(value: Any, default: Path | None = None) -> Path:
    if value is None or value == "":
        if default is None:
            raise ValueError("视频项缺少 path")
        return Path(default)
    return Path(value)


class _LegacyStateBridge:
    """Present V2 items to the legacy video's path/dict state callback."""

    def __init__(
        self,
        strategy: "VideoStrategy",
        state: Any,
        items: Sequence[MediaItem],
        source_root: Path,
    ):
        self.strategy = strategy
        self.state = state
        self.items = tuple(items)
        self.source_root = source_root

    def is_completed(self, value: Any) -> bool:
        item = self.strategy._match_item(value, self.items, self.source_root)
        return self.strategy._state_completed(self.state, item)


class VideoStrategy:
    """Concrete V2 ``MediaStrategy`` adapter for video uploads.

    ``legacy`` is injectable so the boundary can be tested without starting
    FFmpeg, ExifTool or TDLib.  In production it defaults to the existing
    bundled legacy video implementation.  The optional ``source_root``
    constructor value is only a default; the V2 method argument remains the
    source of truth for each scan and plan.
    """

    kind = "video"

    # The legacy module reads ``cfg.VIDEO_DIR`` for several helpers even
    # though the V2 contract passes source_root explicitly.  The GUI runs one
    # upload task at a time, and this lock makes the temporary compatibility
    # override safe for any accidental same-process concurrent caller.
    _legacy_config_lock = threading.RLock()

    def __init__(
        self,
        legacy: Any | None = None,
        *,
        legacy_module: Any | None = None,
        config: Any | None = None,
        source_root: Path | None = None,
    ):
        if legacy is not None and legacy_module is not None:
            raise TypeError("legacy 和 legacy_module 只能指定一个")
        self.legacy = legacy_module if legacy_module is not None else legacy
        self.config = config
        self.source_root = Path(source_root) if source_root is not None else None
        self.last_missing: tuple[Path, ...] = ()
        if self.legacy is None:
            self.legacy = _load_legacy_module()

    @property
    def _config(self) -> Any:
        return getattr(self.legacy, "cfg", None) or self.config or self.legacy

    @staticmethod
    def _target_dict(target: Any) -> dict[str, Any]:
        if isinstance(target, Mapping):
            return dict(target)
        as_dict = getattr(target, "as_dict", None)
        if callable(as_dict):
            value = as_dict()
            return dict(value) if isinstance(value, Mapping) else {}
        return {}

    def _root(self, source_root: Path | None = None) -> Path:
        if source_root is not None:
            return Path(source_root)
        if self.source_root is not None:
            return self.source_root
        configured = getattr(self._config, "VIDEO_DIR", None)
        return Path(configured) if configured is not None else Path(".")

    @contextmanager
    def _legacy_root(
        self,
        source_root: Path,
        items: Sequence[MediaItem] = (),
    ):
        """Temporarily align legacy global configuration with V2 source_root."""

        holder = getattr(self.legacy, "cfg", None) or self.config
        attribute_owner = holder if hasattr(holder, "VIDEO_DIR") else None
        if attribute_owner is None and hasattr(self.legacy, "VIDEO_DIR"):
            attribute_owner = self.legacy

        with self._legacy_config_lock:
            original = (
                getattr(attribute_owner, "VIDEO_DIR")
                if attribute_owner is not None
                else _MISSING
            )
            previous_snapshots = getattr(self.legacy, "LAST_SCAN_SNAPSHOTS", _MISSING)
            try:
                if attribute_owner is not None:
                    setattr(attribute_owner, "VIDEO_DIR", Path(source_root))
                if previous_snapshots is not _MISSING:
                    working = dict(previous_snapshots or {})
                    for item in items:
                        working[self._normalize_path(item.path)] = (
                            int(item.snapshot.size),
                            int(item.snapshot.mtime_ns),
                        )
                    self.legacy.LAST_SCAN_SNAPSHOTS = working
                yield
            finally:
                if attribute_owner is not None and original is not _MISSING:
                    setattr(attribute_owner, "VIDEO_DIR", original)
                if previous_snapshots is not _MISSING:
                    self.legacy.LAST_SCAN_SNAPSHOTS = previous_snapshots

    def _cancel_event(self, token: CancelToken | None):
        return _CancelEventBridge(token) if token is not None else None

    def _emit(self, event_sink: EventSink | None, event: Any) -> None:
        emit = getattr(event_sink, "emit", None) if event_sink is not None else None
        if callable(emit):
            try:
                emit(event)
            except Exception:
                pass

    def _progress_callback(
        self,
        event_sink: EventSink | None,
        *,
        default_total: int = 0,
    ) -> Callable[[Mapping[str, Any]], None]:
        def emit_progress(payload: Mapping[str, Any]) -> None:
            if not isinstance(payload, Mapping):
                return
            self._emit(
                event_sink,
                ProgressEvent(
                    kind=self.kind,
                    phase=str(payload.get("phase", "scan")),
                    completed=_as_int(payload.get("completed"), 0),
                    total=_as_int(payload.get("total"), default_total),
                    path=(str(payload["path"]) if payload.get("path") is not None else None),
                    message=str(payload.get("message", "") or ""),
                ),
            )

        return emit_progress

    def _normalize_path(self, path: Any) -> str:
        normalizer = getattr(self.legacy, "normalize_path", None)
        if callable(normalizer):
            try:
                return str(normalizer(path))
            except (OSError, TypeError, ValueError):
                pass
        return stable_path(path)

    def _scan_snapshot(self, path: Path, raw_item: Any = None) -> FileSnapshot | None:
        raw = raw_item if isinstance(raw_item, Mapping) else {}
        candidate = raw.get("snapshot")
        if isinstance(candidate, FileSnapshot):
            return candidate
        if candidate is not None and hasattr(candidate, "size") and hasattr(candidate, "mtime_ns"):
            return FileSnapshot(str(getattr(candidate, "path", path)), int(candidate.size), int(candidate.mtime_ns))

        size = raw.get("scan_size", raw.get("size"))
        mtime_ns = raw.get("scan_mtime_ns", raw.get("mtime_ns"))
        if size is not None and mtime_ns is not None:
            return FileSnapshot(str(path), _as_int(size), _as_int(mtime_ns))

        snapshots = getattr(self.legacy, "LAST_SCAN_SNAPSHOTS", {})
        try:
            value = snapshots.get(self._normalize_path(path))
        except AttributeError:
            value = None
        if value is not None:
            if isinstance(value, FileSnapshot):
                return value
            if hasattr(value, "size") and hasattr(value, "mtime_ns"):
                return FileSnapshot(str(getattr(value, "path", path)), int(value.size), int(value.mtime_ns))
            try:
                return FileSnapshot(str(path), int(value[0]), int(value[1]))
            except (IndexError, TypeError, ValueError):
                pass

        # This is only a compatibility fallback for injected/old scanners that
        # predate LAST_SCAN_SNAPSHOTS.  The real legacy scanner has already
        # captured the snapshot before returning the path.
        return snapshot_file(path)

    def _media_item(
        self,
        raw_item: Any,
        source_root: Path,
        *,
        candidates: Sequence[MediaItem] = (),
    ) -> MediaItem:
        if isinstance(raw_item, MediaItem):
            return raw_item
        raw = raw_item if isinstance(raw_item, Mapping) else {"path": raw_item}
        path = _as_path(raw.get("path"), source_root)
        size = raw.get("scan_size", raw.get("size"))
        mtime_ns = raw.get("scan_mtime_ns", raw.get("mtime_ns"))

        for candidate in candidates:
            if self._normalize_path(candidate.path) != self._normalize_path(path):
                continue
            if size is not None and mtime_ns is not None:
                if candidate.snapshot.as_tuple() != (_as_int(size), _as_int(mtime_ns)):
                    continue
            return candidate

        snapshot = self._scan_snapshot(path, raw)
        if snapshot is None:
            raise ValueError(f"视频项缺少扫描快照：{path}")
        capture_time = raw.get("capture_time")
        if isinstance(capture_time, str) and capture_time:
            try:
                capture_time = datetime.fromisoformat(capture_time)
            except ValueError:
                pass
        month_key = raw.get("month_key")
        group_name = raw.get("group_name", month_key or "")
        metadata = raw.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        if "requires_premium" in raw:
            metadata["requires_premium"] = bool(raw["requires_premium"])
        if "date_tag" in raw:
            metadata.setdefault("date_tag", raw.get("date_tag", ""))
        return MediaItem(
            path=path,
            source_root=Path(raw.get("source_root", source_root)),
            media_kind=str(raw.get("media_kind", self.kind) or self.kind),
            snapshot=snapshot,
            capture_time=capture_time if isinstance(capture_time, datetime) else None,
            group_name=str(group_name or ""),
            month_key=str(month_key) if month_key is not None else None,
            date_tag=str(raw.get("date_tag", "") or ""),
            fallback=bool(raw.get("fallback", False)),
            metadata=metadata,
        )

    def _legacy_item(self, item: MediaItem) -> dict[str, Any]:
        values = dict(item.metadata) if isinstance(item.metadata, Mapping) else {}
        values.update(
            {
                "path": item.path,
                "source_root": item.source_root,
                "media_kind": item.media_kind,
                "scan_size": item.snapshot.size,
                "scan_mtime_ns": item.snapshot.mtime_ns,
                "capture_time": item.capture_time,
                "group_name": item.group_name,
                "month_key": item.month_key,
                "date_tag": item.date_tag,
                "fallback": item.fallback,
            }
        )
        return values

    def _state_completed(self, state: Any, item: MediaItem) -> bool:
        if state is None:
            return False
        checker = getattr(state, "is_completed", None)
        if not callable(checker):
            raise TypeError("state 必须提供 is_completed()")
        legacy_item = self._legacy_item(item)
        for candidate in (item, legacy_item):
            try:
                if bool(checker(candidate)):
                    return True
            except (OSError, TypeError, ValueError, AttributeError):
                continue
        return False

    def _match_item(
        self,
        raw_item: Any,
        candidates: Sequence[MediaItem],
        source_root: Path,
    ) -> MediaItem:
        if isinstance(raw_item, MediaItem):
            for candidate in candidates:
                if (
                    self._normalize_path(candidate.path)
                    == self._normalize_path(raw_item.path)
                    and candidate.media_kind == raw_item.media_kind
                    and candidate.snapshot.as_tuple() == raw_item.snapshot.as_tuple()
                ):
                    return candidate
            raise ValueError(f"legacy 视频计划返回了未知项：{raw_item.path}")
        raw = raw_item if isinstance(raw_item, Mapping) else {"path": raw_item}
        path = _as_path(raw.get("path"), source_root)
        size = raw.get("scan_size", raw.get("size"))
        mtime_ns = raw.get("scan_mtime_ns", raw.get("mtime_ns"))
        for candidate in candidates:
            if self._normalize_path(candidate.path) != self._normalize_path(path):
                continue
            if size is not None and mtime_ns is not None:
                if candidate.snapshot.as_tuple() != (_as_int(size), _as_int(mtime_ns)):
                    continue
            return candidate
        raise ValueError(f"legacy 视频计划返回了未知项：{path}")

    def _configured_dates_enabled(self) -> bool:
        checker = getattr(self.legacy, "video_dates_enabled", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                pass
        return bool(getattr(self._config, "VIDEO_READ_DATES", True))

    def _metadata_index(
        self,
        paths: Sequence[Path],
        *,
        cancel_event: Any,
        event_sink: EventSink | None,
    ) -> tuple[dict[str, Any], list[str]]:
        if not self._configured_dates_enabled() or not paths:
            return {}, []
        reader = getattr(self.legacy, "read_exif_metadata", None)
        if not callable(reader):
            return {}, []

        config = self._config
        configured_exif = getattr(config, "EXIFTOOL_PATH", None)
        if configured_exif is not None:
            try:
                has_exiftool = Path(configured_exif).exists()
            except (OSError, TypeError, ValueError):
                has_exiftool = False
            if not has_exiftool:
                reads_media = bool(getattr(config, "VIDEO_READ_MEDIA_CREATION_DATE", True))
                missing_policy = str(getattr(config, "VIDEO_MISSING_DATE_POLICY", "mtime")).lower()
                if reads_media or missing_policy == "mtime":
                    return {}, []

        diagnostics: list[str] = []
        try:
            value = _call_supported(
                reader,
                (tuple(paths),),
                cancel_event=cancel_event,
                progress_callback=self._progress_callback(event_sink, default_total=len(paths)),
            )
            return dict(value or {}), diagnostics
        except BaseException as error:
            if _token_cancelled(getattr(cancel_event, "token", None)):
                raise _StrategyCancelled(str(error) or "视频日期读取已取消") from error
            diagnostics.append(str(error))
            return {}, diagnostics

    def scan(
        self,
        source_root: Path,
        *,
        cancel_token: CancelToken,
        event_sink: EventSink,
        context: UploadContext | None = None,
    ) -> ScanResult:
        """Delegate discovery/date handling and return immutable V2 items."""

        root = self._root(source_root)
        _check_cancel(cancel_token)
        cancel_event = self._cancel_event(cancel_token)
        scanner = getattr(self.legacy, "scan_videos", None)
        if not callable(scanner):
            raise TypeError("legacy video module 缺少 scan_videos()")

        with self._legacy_root(root):
            raw_paths = _call_supported(scanner, cancel_event=cancel_event, source_root=root)
            if hasattr(raw_paths, "paths"):
                raw_paths = raw_paths.paths
            paths = tuple(Path(path) for path in (raw_paths or ()))
            if _token_cancelled(cancel_token):
                return ScanResult(cancelled=True)

            warnings = [str(value) for value in getattr(self.legacy, "LAST_SCAN_WARNINGS", ())]
            errors = [str(value) for value in getattr(self.legacy, "LAST_SCAN_ERRORS", ())]
            for record in getattr(self.legacy, "LAST_SCAN_SIZE_SKIPS", ()) or ():
                if isinstance(record, Mapping) and record.get("reason"):
                    warnings.append(str(record["reason"]))

            metadata, metadata_errors = self._metadata_index(
                paths,
                cancel_event=cancel_event,
                event_sink=event_sink,
            )
            errors.extend(metadata_errors)
            _check_cancel(cancel_token)

            builder = getattr(self.legacy, "build_items", None)
            if callable(builder):
                built = _call_supported(
                    builder,
                    (paths, metadata),
                    cancel_event=cancel_event,
                    progress_callback=self._progress_callback(event_sink, default_total=len(paths)),
                )
                if isinstance(built, tuple) and len(built) == 2:
                    raw_items, missing = built
                else:
                    raw_items, missing = built, ()
            else:
                raw_items, missing = paths, ()

            items: list[MediaItem] = []
            for raw_item in raw_items or ():
                try:
                    items.append(self._media_item(raw_item, root))
                except (OSError, TypeError, ValueError) as error:
                    errors.append(str(error))
            missing_paths = tuple(Path(value.get("path")) if isinstance(value, Mapping) else Path(value) for value in (missing or ()))
            self.last_missing = missing_paths
            if missing_paths:
                missing_label = "、".join(str(path) for path in missing_paths)
                missing_policy = str(getattr(self._config, "VIDEO_MISSING_DATE_POLICY", "mtime")).lower()
                message = f"以下视频没有可用日期：{missing_label}"
                (errors if missing_policy == "error" else warnings).append(message)

            cancelled = _token_cancelled(cancel_token)
            if cancelled:
                warnings.append("视频扫描已取消")
            self._emit(
                event_sink,
                ProgressEvent(
                    kind=self.kind,
                    phase="scan",
                    completed=len(items),
                    total=len(items),
                    message="扫描完成" if not cancelled else "扫描已取消",
                ),
            )
            return ScanResult(
                items=tuple(items),
                errors=tuple(errors),
                warnings=tuple(warnings),
                cancelled=cancelled,
            )

    def _plan_group_label(self, raw_plan: Mapping[str, Any]) -> str:
        explicit = raw_plan.get("group_label")
        if explicit is not None:
            return str(explicit)
        month_key = raw_plan.get("month_key")
        display = getattr(self.legacy, "group_display_name", None)
        if callable(display) and month_key is not None:
            try:
                return str(display(str(month_key)))
            except Exception:
                pass
        return str(month_key or raw_plan.get("caption", ""))

    @staticmethod
    def _plan_caption(raw_plan: Mapping[str, Any]) -> str:
        caption = raw_plan.get("caption", "")
        if isinstance(caption, Mapping):
            return str(caption.get("text", "") or "")
        return str(caption or "")

    def build_plans(
        self,
        scan_result: ScanResult,
        *,
        target: Mapping[str, Any],
        state: Any | None = None,
        context: UploadContext | None = None,
    ) -> Sequence[AlbumPlan]:
        """Reuse legacy Album boundaries without allowing cross-group refill."""

        if not isinstance(scan_result, ScanResult):
            raise TypeError("VideoStrategy.build_plans() 需要 ScanResult")
        source_root = Path(scan_result.items[0].source_root) if scan_result.items else self._root()
        if any(Path(item.source_root) != source_root for item in scan_result.items):
            raise ValueError("一个视频扫描结果不能包含多个 source_root")
        legacy_items = [self._legacy_item(item) for item in scan_result.items]
        planner = getattr(self.legacy, "build_album_plans", None)
        if not callable(planner):
            raise TypeError("legacy video module 缺少 build_album_plans()")

        state_bridge = (
            _LegacyStateBridge(self, state, scan_result.items, source_root)
            if state is not None
            else None
        )
        with self._legacy_root(source_root, scan_result.items):
            raw_plans = _call_supported(
                planner,
                (legacy_items,),
                state=state_bridge,
            )

        plans: list[AlbumPlan] = []
        all_items = tuple(scan_result.items)
        for index, raw_plan in enumerate(raw_plans or (), start=1):
            if not isinstance(raw_plan, Mapping):
                raise TypeError(f"legacy Album 计划必须是 mapping，而不是 {type(raw_plan).__name__}")
            raw_full = raw_plan.get("items", legacy_items)
            raw_pending = raw_plan.get("pending_items", raw_full)
            full_items = tuple(self._match_item(item, all_items, source_root) for item in raw_full or ())
            pending_items = tuple(self._match_item(item, all_items, source_root) for item in raw_pending or ())
            key = str(raw_plan.get("key", "") or "")
            if not key:
                raise ValueError(f"legacy Album {index} 缺少稳定 key")
            plans.append(
                AlbumPlan(
                    key=key,
                    kind=self.kind,
                    source_root=source_root,
                    group_label=self._plan_group_label(raw_plan),
                    number=_as_int(raw_plan.get("number"), index),
                    items=full_items,
                    pending_items=pending_items,
                    caption=self._plan_caption(raw_plan),
                    target=self._target_dict(target),
                )
            )
        return tuple(plans)

    def _caption_for(
        self,
        plan: AlbumPlan,
        legacy_items: Sequence[Mapping[str, Any]],
        context: UploadContext | None,
    ) -> str:
        caption = str(plan.caption or "")
        config = self._config
        enabled = bool(getattr(config, "VIDEO_CAPTION_INCLUDE_FILENAMES", False))
        helper = getattr(self.legacy, "with_filename_description", None)
        if not enabled or not callable(helper):
            return caption
        numberer = getattr(self.legacy, "include_filename_numbers", None)
        numbered = bool(numberer()) if callable(numberer) else bool(
            getattr(config, "VIDEO_CAPTION_INCLUDE_FILENAME_NUMBERS", True)
        )
        metadata = context.metadata if context is not None else {}
        limit = metadata.get("caption_limit", metadata.get("caption_length_limit", 1024))
        return str(
            _call_supported(
                helper,
                (caption, legacy_items, True),
                numbered=numbered,
                max_chars=_as_int(limit, 1024),
            )
        )

    @staticmethod
    def _skip_category(record: Mapping[str, Any]) -> str:
        category = str(record.get("category", "unreadable") or "unreadable").strip().lower()
        return "deferred" if category in {"deferred", "changed", "network", "cancelled", "read_failed"} else "failed"

    def _normalize_contents(
        self,
        value: Any,
        plan: AlbumPlan,
        source_root: Path,
    ) -> ContentBuildResult:
        """Normalize the legacy tuple while enforcing an exact item partition."""

        pending = tuple(plan.pending_items)
        pending_lookup = {
            (
                self._normalize_path(item.path),
                item.snapshot.size,
                item.snapshot.mtime_ns,
            ): item
            for item in pending
        }
        if len(pending_lookup) != len(pending):
            raise ValueError("Album 待上传项 identity 重复")

        def resolve(raw_item: Any) -> MediaItem:
            return self._match_item(raw_item, pending, source_root)

        def key(item: MediaItem) -> tuple[object, ...]:
            return (
                self._normalize_path(item.path),
                item.snapshot.size,
                item.snapshot.mtime_ns,
            )

        if isinstance(value, ContentBuildResult):
            contents = tuple(value.contents)
            deferred = tuple(resolve(item) for item in value.deferred_items)
            failed = tuple(resolve(item) for item in value.failed_items)
            if value.ready_items is None:
                excluded = {key(item) for item in (*deferred, *failed)}
                ready = tuple(item for item in pending if key(item) not in excluded)
            else:
                ready = tuple(resolve(item) for item in value.ready_items)
            errors = tuple(str(error) for error in value.errors if str(error))
        else:
            if (
                isinstance(value, (str, bytes))
                or not isinstance(value, Sequence)
                or len(value) != 3
            ):
                raise TypeError("legacy video builder 必须返回 (contents, valid, skipped)")
            raw_contents, raw_valid, raw_skipped = value
            contents = tuple(raw_contents or ())
            ready = tuple(resolve(item) for item in (raw_valid or ()))
            deferred = []
            failed = []
            errors: list[str] = []
            for raw_record in raw_skipped or ():
                if not isinstance(raw_record, Mapping):
                    raise TypeError("legacy video builder 返回了无效 skipped record")
                raw_item = raw_record.get("item", raw_record.get("path"))
                if raw_item is None:
                    raise ValueError("legacy video builder 的 skipped record 缺少 item/path")
                item = resolve(raw_item)
                (deferred if self._skip_category(raw_record) == "deferred" else failed).append(item)
                reason = str(raw_record.get("reason", "") or "").strip()
                if reason:
                    errors.append(reason)
            deferred = tuple(deferred)
            failed = tuple(failed)

        seen: set[tuple[object, ...]] = set()
        for name, values in (
            ("ready_items", ready),
            ("deferred_items", deferred),
            ("failed_items", failed),
        ):
            for item in values:
                item_key = key(item)
                if item_key not in pending_lookup:
                    raise ValueError(f"内容构建结果的 {name} 包含不属于当前 Album 的项")
                if item_key in seen:
                    raise ValueError("内容构建结果的 item outcome 重复")
                seen.add(item_key)

        pending_keys = set(pending_lookup)
        if seen != pending_keys:
            missing = [str(item.path) for item in pending if key(item) not in seen]
            raise ValueError("legacy video builder 未覆盖全部待上传项：" + ", ".join(missing))
        if len(contents) != len(ready):
            raise ValueError("legacy video builder 的 contents 数量必须与 ready_items 数量一致")
        return ContentBuildResult(
            contents=contents,
            ready_items=tuple(ready),
            deferred_items=tuple(deferred),
            failed_items=tuple(failed),
            errors=tuple(errors),
        )

    def build_contents(
        self,
        plan: AlbumPlan,
        *,
        cancel_token: CancelToken,
        event_sink: EventSink,
        context: UploadContext | None = None,
    ) -> Sequence[Mapping[str, Any]] | ContentBuildResult:
        """Reuse legacy ``input_video`` construction with item outcomes."""

        if not isinstance(plan, AlbumPlan) or plan.kind != self.kind:
            raise TypeError("VideoStrategy.build_contents() 需要 video AlbumPlan")
        _check_cancel(cancel_token)
        source_root = Path(plan.source_root)
        legacy_items = [self._legacy_item(item) for item in plan.pending_items]
        builder = getattr(self.legacy, "build_video_contents", None)
        if not callable(builder):
            raise TypeError("legacy video module 缺少 build_video_contents()")
        cancel_event = self._cancel_event(cancel_token)
        ui = _StrategyUI(event_sink, self.kind)
        caption = self._caption_for(plan, legacy_items, context)

        with self._legacy_root(source_root, plan.pending_items):
            value = _call_supported(
                builder,
                (legacy_items, caption),
                ui=ui,
                cancel_event=cancel_event,
            )
        _check_cancel(cancel_token)
        normalized = self._normalize_contents(value, plan, source_root)
        if (
            normalized.ready_items
            and len(normalized.ready_items) != len(plan.pending_items)
            and bool(getattr(self._config, "VIDEO_CAPTION_INCLUDE_FILENAMES", False))
            and normalized.contents
        ):
            relabeled = self._caption_for(
                plan,
                [self._legacy_item(item) for item in normalized.ready_items],
                context,
            )
            formatter = getattr(self.legacy, "formatted_text", None)
            if callable(formatter):
                first = dict(normalized.contents[0])
                first["caption"] = formatter(relabeled)
                normalized = ContentBuildResult(
                    contents=(first, *normalized.contents[1:]),
                    ready_items=normalized.ready_items,
                    deferred_items=normalized.deferred_items,
                    failed_items=normalized.failed_items,
                    errors=normalized.errors,
                )
        return normalized

    def preflight_item(
        self,
        item: MediaItem,
        *,
        context: UploadContext | None = None,
    ) -> Mapping[str, str]:
        """Expose the legacy video preflight as an engine-compatible checker.

        The method is intentionally an extra adapter helper rather than a new
        ``MediaStrategy`` requirement.  A GUI can inject
        ``strategy.preflight_item`` into ``UploadContext.preflight`` while the
        generic engine continues to own the partition and lifecycle.
        """

        if not isinstance(item, MediaItem) or item.media_kind != self.kind:
            raise TypeError("视频预检需要 video MediaItem")
        _check_cancel(context.cancel_token if context is not None else None)
        preflight = getattr(self.legacy, "preflight_videos", None)
        if not callable(preflight):
            return {"status": "READY"}
        token = context.cancel_token if context is not None else None
        cancel_event = self._cancel_event(token)
        event_sink = context.event_sink if context is not None else None
        ui = _StrategyUI(event_sink, self.kind)
        with self._legacy_root(item.source_root, (item,)):
            skipped = _call_supported(
                preflight,
                ([self._legacy_item(item)],),
                ui=ui,
                cancel_event=cancel_event,
            )
        _check_cancel(token)
        if not skipped:
            return {"status": "READY"}
        record = skipped[0] if isinstance(skipped[0], Mapping) else {}
        category = self._skip_category(record)
        return {
            "status": "DEFERRED" if category == "deferred" else "FAILED",
            "reason": str(record.get("reason", "视频预检失败") or "视频预检失败"),
        }


VideoMediaStrategy = VideoStrategy


__all__ = ["VideoMediaStrategy", "VideoStrategy"]
