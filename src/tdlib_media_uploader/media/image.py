"""V2 adapter for the legacy image uploader.

The V1.9 image module remains the implementation of image-specific probing,
compression and TDLib input construction during the incremental migration.
This adapter translates its path/dictionary values into the V2 strategy
contracts and leaves state, journal and transport lifecycle to UploadEngine.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
import importlib
import inspect
from pathlib import Path
import threading
from typing import Any, Callable

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
_LEGACY_SCOPE_LOCK = threading.RLock()


def _load_legacy_module() -> Any:
    """Load the bundled image compatibility module."""

    return importlib.import_module("tdlib_media_uploader.media.legacy_image")


class _StrategyCancelled(RuntimeError):
    """Cancellation raised at the V2 strategy boundary."""

    cancelled = True


def _call_supported(
    function: Callable[..., Any],
    args: Sequence[Any] = (),
    **kwargs: Any,
) -> Any:
    """Call legacy hooks while tolerating their historical small signatures."""

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*args, **kwargs)

    parameters = signature.parameters
    positional = list(args)
    accepted: dict[str, Any] = {}
    accepts_any_keyword = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    for name, value in kwargs.items():
        parameter = parameters.get(name)
        if parameter is not None and parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            positional.append(value)
        elif parameter is not None or accepts_any_keyword:
            accepted[name] = value
    return function(*positional, **accepted)


def _cancelled(token: CancelToken | None) -> bool:
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
    if _cancelled(token):
        raise _StrategyCancelled("图片策略操作已取消")
    if token is None:
        return
    raiser = getattr(token, "raise_if_cancelled", None)
    if callable(raiser):
        try:
            raiser()
        except BaseException as error:
            if _cancelled(token) or bool(getattr(error, "cancelled", False)):
                raise _StrategyCancelled(str(error) or "图片策略操作已取消") from error
            raise


class _CancelEventBridge:
    """Expose the legacy ``Event.is_set`` shape for a V2 token."""

    def __init__(self, token: CancelToken | None):
        self.token = token

    def is_set(self) -> bool:
        return _cancelled(self.token)


class _StrategyUI:
    """GUI-free callback facade that forwards legacy diagnostics to EventSink."""

    def __init__(self, event_sink: EventSink | None):
        self.event_sink = event_sink

    def _emit(self, level: str, message: Any) -> None:
        emit = getattr(self.event_sink, "emit", None)
        if not callable(emit):
            return
        try:
            emit(LogEvent(level=level, message=str(message), source="image-strategy"))
        except Exception:
            # Presentation must not change a media decision.
            pass

    def warning(self, message: Any) -> None:
        self._emit("WARNING", message)

    def info(self, message: Any) -> None:
        self._emit("INFO", message)

    def log(self, message: Any) -> None:
        self._emit("INFO", message)


def _model_identity(item: MediaItem) -> tuple[object, ...]:
    return (
        str(Path(item.path)),
        str(Path(item.source_root)),
        str(item.media_kind),
        item.snapshot.path,
        item.snapshot.size,
        item.snapshot.mtime_ns,
    )


def _raw_snapshot(raw: Mapping[str, Any], path: Path) -> FileSnapshot:
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
        raise ValueError(f"图片项缺少扫描快照：{path}")
    return FileSnapshot(str(path), int(size), int(mtime_ns))


class _LegacyStateBridge:
    """Present V2 items to a legacy planner's path-only state callback."""

    def __init__(
        self,
        strategy: "ImageStrategy",
        state: Any,
        items: Sequence[MediaItem],
        lookup: Mapping[str, Mapping] | None = None,
    ):
        self.strategy = strategy
        self.state = state
        self.items = tuple(items)
        self.lookup = lookup if lookup is not None else strategy._build_item_lookup(self.items)

    def is_completed(self, value: Any) -> bool:
        item = self.strategy._resolve_item(value, self.items, self.lookup)
        return self.strategy._state_completed(self.state, item)


class ImageStrategy:
    """Concrete V2 ``MediaStrategy`` backed by the V1.9 image module."""

    kind = "image"

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
        if self.legacy is None:
            self.legacy = _load_legacy_module()

    @property
    def _config(self) -> Any:
        return getattr(self.legacy, "cfg", None) or self.config or self.legacy

    def _root(self, source_root: Path | None = None) -> Path:
        if source_root is not None:
            return Path(source_root)
        if self.source_root is not None:
            return self.source_root
        configured = getattr(self._config, "IMAGE_DIR", None)
        return Path(configured) if configured is not None else Path(".")

    def _normalize_path(self, path: Any) -> str:
        normalizer = getattr(self.legacy, "stable_path", None)
        if callable(normalizer):
            try:
                return str(normalizer(path))
            except (OSError, TypeError, ValueError):
                pass
        return stable_path(path)

    @contextmanager
    def _legacy_scope(
        self,
        source_root: Path,
        items: Sequence[MediaItem] = (),
    ):
        """Temporarily align legacy globals with one V2 run.

        The old image helpers read ``cfg.IMAGE_DIR`` and
        ``IMAGE_SCAN_SNAPSHOTS``.  The values are scoped and restored so two
        independent strategy calls cannot leak a source root or stale
        identity into one another.
        """

        config = getattr(self.legacy, "cfg", None) or self.config
        with _LEGACY_SCOPE_LOCK:
            previous_root = getattr(config, "IMAGE_DIR", _MISSING)
            previous_snapshots = getattr(self.legacy, "IMAGE_SCAN_SNAPSHOTS", _MISSING)
            try:
                if previous_root is not _MISSING:
                    config.IMAGE_DIR = Path(source_root)
                if previous_snapshots is not _MISSING:
                    working = dict(previous_snapshots or {})
                    for item in items:
                        working[self._normalize_path(item.path)] = (
                            int(item.snapshot.size),
                            int(item.snapshot.mtime_ns),
                        )
                    self.legacy.IMAGE_SCAN_SNAPSHOTS = working
                yield
            finally:
                if previous_root is not _MISSING:
                    config.IMAGE_DIR = previous_root
                if previous_snapshots is not _MISSING:
                    self.legacy.IMAGE_SCAN_SNAPSHOTS = previous_snapshots

    def _scan_snapshot(self, path: Path, raw: Any = None) -> FileSnapshot | None:
        if isinstance(raw, Mapping):
            try:
                return _raw_snapshot(raw, path)
            except (TypeError, ValueError, KeyError):
                pass
        snapshots = getattr(self.legacy, "IMAGE_SCAN_SNAPSHOTS", {})
        try:
            value = snapshots.get(self._normalize_path(path))
        except AttributeError:
            value = None
        if isinstance(value, FileSnapshot):
            return value
        if value is not None:
            try:
                return FileSnapshot(str(path), int(value[0]), int(value[1]))
            except (IndexError, TypeError, ValueError):
                pass
        # This fallback is only for direct/injected old scanners without a
        # snapshot map. The production scanner captures one before returning.
        return snapshot_file(path)

    def _media_item(self, raw: Any, source_root: Path) -> MediaItem:
        if isinstance(raw, MediaItem):
            return raw
        value = raw if isinstance(raw, Mapping) else {"path": raw}
        path = Path(value.get("path", source_root))
        snapshot = self._scan_snapshot(path, value)
        if snapshot is None:
            raise ValueError(f"图片项缺少扫描快照：{path}")
        metadata = value.get("metadata")
        return MediaItem(
            path=path,
            source_root=Path(value.get("source_root", source_root)),
            media_kind=str(value.get("media_kind", self.kind) or self.kind),
            snapshot=snapshot,
            metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
        )

    def _resolve_item(
        self,
        raw: Any,
        candidates: Sequence[MediaItem],
        lookup: Mapping[str, Mapping] | None = None,
    ) -> MediaItem:
        index = lookup if lookup is not None else self._build_item_lookup(candidates)
        if isinstance(raw, MediaItem):
            path = Path(raw.path)
            size, mtime_ns = raw.snapshot.as_tuple()
            media_kind = str(raw.media_kind or self.kind)
        else:
            value = raw if isinstance(raw, Mapping) else {"path": raw}
            if value.get("path") is None:
                raise ValueError("legacy 图片计划返回了缺少 path 的项")
            path = Path(value["path"])
            size = value.get("scan_size", value.get("size"))
            mtime_ns = value.get("scan_mtime_ns", value.get("mtime_ns"))
            media_kind = str(value.get("media_kind", self.kind) or self.kind)
        normalized_path = self._normalize_path(path)
        by_identity = index["by_identity"]
        if size is not None and mtime_ns is not None:
            key = (normalized_path, int(size), int(mtime_ns), media_kind)
            item = by_identity.get(key)
            if item is not None:
                return item
        else:
            matches = index["by_path"].get((normalized_path, media_kind), ())
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise ValueError(f"legacy 图片项缺少快照且 identity 不唯一：{path}")
        raise ValueError(f"legacy 图片计划返回了未知项：{path}")

    def _build_item_lookup(self, candidates: Sequence[MediaItem]) -> dict[str, dict]:
        by_identity: dict[tuple[object, ...], MediaItem] = {}
        by_path: dict[tuple[str, str], list[MediaItem]] = {}
        for item in candidates:
            normalized_path = self._normalize_path(item.path)
            key = (
                normalized_path,
                int(item.snapshot.size),
                int(item.snapshot.mtime_ns),
                str(item.media_kind or self.kind),
            )
            if key in by_identity:
                raise ValueError(f"图片 MediaItem identity 重复：{item.path}")
            by_identity[key] = item
            by_path.setdefault((normalized_path, key[3]), []).append(item)
        return {"by_identity": by_identity, "by_path": by_path}

    def _state_completed(self, state: Any, item: MediaItem) -> bool:
        if state is None:
            return False
        checker = getattr(state, "is_completed", None)
        if not callable(checker):
            raise TypeError("state 必须提供 is_completed()")
        legacy_item = {
            "path": item.path,
            "source_root": item.source_root,
            "media_kind": item.media_kind,
            "scan_size": item.snapshot.size,
            "scan_mtime_ns": item.snapshot.mtime_ns,
        }
        # Legacy UploadState accepts the dictionary shape while V2 stores
        # generally accept MediaItem. Try both without allowing an old facade
        # to force a late stat when the V2 item already has a snapshot.
        for candidate in (item, legacy_item):
            try:
                if bool(checker(candidate)):
                    return True
            except (OSError, TypeError, ValueError, AttributeError):
                continue
        return False

    @staticmethod
    def _target_dict(target: Any) -> dict[str, Any]:
        if isinstance(target, Mapping):
            return dict(target)
        converter = getattr(target, "as_dict", None)
        if callable(converter):
            value = converter()
            return dict(value) if isinstance(value, Mapping) else {}
        return {}

    @staticmethod
    def _caption_text(value: Any) -> str:
        if isinstance(value, Mapping):
            return str(value.get("text", "") or "")
        return str(value or "")

    def _scan_diagnostics(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        errors = tuple(
            str(value)
            for value in getattr(self.legacy, "LAST_SCAN_ERRORS", ())
            if str(value)
        )
        warnings = [
            str(value)
            for value in getattr(self.legacy, "LAST_SCAN_WARNINGS", ())
            if str(value)
        ]
        size_skips = list(getattr(self.legacy, "LAST_SCAN_SIZE_SKIPS", ()) or ())
        rejected = [record for record in size_skips if record.get("action") == "skip"]
        compressing = [record for record in size_skips if record.get("action") == "compress"]
        if rejected:
            warnings.append(
                f"扫描时跳过 {len(rejected)} 个超过 Telegram Photo 上限的图片；"
                "这些文件未加入上传计划。"
            )
        if compressing:
            warnings.append(
                f"扫描提醒：发现 {len(compressing)} 个超限图片；"
                "上传时将尝试用 FFmpeg 生成临时压缩副本。"
            )
        return errors, tuple(warnings)

    def scan(
        self,
        source_root: Path,
        *,
        cancel_token: CancelToken,
        event_sink: EventSink,
        context: UploadContext | None = None,
    ) -> ScanResult:
        """Discover image files and translate the captured snapshots."""

        del context
        root = self._root(source_root)
        _check_cancel(cancel_token)
        scanner = getattr(self.legacy, "scan_images", None)
        if not callable(scanner):
            raise TypeError("legacy image module 缺少 scan_images()")
        cancel_event = _CancelEventBridge(cancel_token)
        with self._legacy_scope(root):
            raw_paths = _call_supported(scanner, cancel_event=cancel_event)
            if hasattr(raw_paths, "paths"):
                raw_paths = raw_paths.paths
            errors, warnings = self._scan_diagnostics()
            items: list[MediaItem] = []
            for raw in raw_paths or ():
                try:
                    items.append(self._media_item(raw, root))
                except (OSError, TypeError, ValueError) as error:
                    errors = (*errors, str(error))
            cancelled = _cancelled(cancel_token) or any("取消" in value for value in warnings)
        if cancelled:
            warnings = (*warnings, "图片扫描已取消")
        emit = getattr(event_sink, "emit", None)
        if callable(emit):
            try:
                emit(
                    ProgressEvent(
                        kind=self.kind,
                        phase="scan",
                        completed=len(items),
                        total=len(items),
                        message="扫描完成" if not cancelled else "扫描已取消",
                    )
                )
            except Exception:
                pass
        return ScanResult(
            items=tuple(items),
            errors=tuple(errors),
            warnings=tuple(warnings),
            cancelled=cancelled,
        )

    def build_plans(
        self,
        scan_result: ScanResult,
        *,
        target: Mapping[str, Any],
        state: Any | None = None,
        context: UploadContext | None = None,
    ) -> Sequence[AlbumPlan]:
        """Reuse legacy Album boundaries, then apply V2 state filtering."""

        del context
        if not isinstance(scan_result, ScanResult):
            raise TypeError("ImageStrategy.build_plans() 需要 ScanResult")
        items = tuple(scan_result.items)
        source_root = Path(items[0].source_root) if items else self._root()
        if any(Path(item.source_root) != source_root for item in items):
            raise ValueError("一个图片扫描结果不能包含多个 source_root")
        planner = getattr(self.legacy, "build_album_plans", None)
        if not callable(planner):
            raise TypeError("legacy image module 缺少 build_album_plans()")
        lookup = self._build_item_lookup(items)
        state_bridge = (
            _LegacyStateBridge(self, state, items, lookup)
            if state is not None
            else None
        )
        with self._legacy_scope(source_root, items):
            raw_plans = _call_supported(
                planner,
                (tuple(item.path for item in items),),
                state=state_bridge,
            )

        plans: list[AlbumPlan] = []
        for index, raw_plan in enumerate(raw_plans or (), start=1):
            if not isinstance(raw_plan, Mapping):
                raise TypeError("legacy image planner 返回了无效 Album")
            raw_full = raw_plan.get("items", ())
            full_items = tuple(self._resolve_item(raw, items, lookup) for raw in raw_full)
            raw_pending = raw_plan.get("pending_items", raw_full)
            pending_items = tuple(self._resolve_item(raw, items, lookup) for raw in raw_pending)
            key = str(raw_plan.get("key", "") or "")
            if not key:
                raise ValueError(f"legacy 图片 Album {index} 缺少稳定 key")
            number = int(raw_plan.get("number", index))
            caption = self._caption_text(raw_plan.get("caption", ""))
            plans.append(
                AlbumPlan(
                    key=key,
                    kind=self.kind,
                    source_root=source_root,
                    group_label=str(raw_plan.get("group_label", f"Album {number}")),
                    number=number,
                    items=full_items,
                    pending_items=pending_items,
                    caption=caption,
                    target=self._target_dict(target),
                )
            )
        return tuple(plans)

    def _caption_for_items(
        self,
        plan: AlbumPlan,
        items: Sequence[MediaItem],
        context: UploadContext | None,
    ) -> str:
        caption = str(plan.caption or "")
        config = self._config
        enabled = bool(getattr(config, "IMAGE_CAPTION_INCLUDE_FILENAMES", False))
        formatter = getattr(self.legacy, "with_filename_description", None)
        if not enabled or not callable(formatter):
            return caption
        metadata = context.metadata if context is not None else {}
        limit = metadata.get("caption_limit", metadata.get("caption_length_limit", 1024))
        return str(
            _call_supported(
                formatter,
                (caption, tuple(item.path for item in items), True),
                max_chars=int(limit or 1024),
            )
        )

    @staticmethod
    def _category(record: Mapping[str, Any]) -> str:
        category = str(record.get("category", "unreadable") or "unreadable").lower()
        return "deferred" if category in {
            "deferred",
            "changed",
            "network",
            "cancelled",
            "read_failed",
        } else "failed"

    def _normalize_contents(
        self,
        result: Any,
        plan: AlbumPlan,
    ) -> ContentBuildResult:
        if isinstance(result, ContentBuildResult):
            return result
        if not isinstance(result, Sequence) or len(result) != 3:
            raise TypeError("legacy image builder 必须返回 (contents, valid, skipped)")
        raw_contents, raw_valid, raw_skipped = result
        pending = tuple(plan.pending_items)
        lookup = self._build_item_lookup(pending)
        ready: list[MediaItem] = []
        deferred: list[MediaItem] = []
        failed: list[MediaItem] = []
        errors: list[str] = []
        pending_lookup = {_model_identity(item): item for item in pending}
        if len(pending_lookup) != len(pending):
            raise ValueError("Album 待上传项 identity 重复")
        seen: set[tuple[object, ...]] = set()

        def add(item: MediaItem, bucket: list[MediaItem]) -> None:
            identity = _model_identity(item)
            if identity not in pending_lookup:
                raise ValueError("legacy image builder 返回了不属于当前 Album 的项")
            if identity in seen:
                raise ValueError("legacy image builder 重复返回了 item outcome")
            seen.add(identity)
            bucket.append(item)

        for raw in raw_valid or ():
            add(self._resolve_item(raw, pending, lookup), ready)
        for raw_record in raw_skipped or ():
            if not isinstance(raw_record, Mapping):
                raise TypeError("legacy image builder 返回了无效 skipped record")
            raw_item = raw_record.get("item", raw_record.get("path"))
            item = self._resolve_item(raw_item, pending, lookup)
            add(item, deferred if self._category(raw_record) == "deferred" else failed)
            reason = str(raw_record.get("reason", "") or "").strip()
            if reason:
                errors.append(reason)

        missing = [item for item in pending if _model_identity(item) not in seen]
        if missing:
            raise ValueError(
                "legacy image builder 未覆盖全部待上传项："
                + ", ".join(str(item.path) for item in missing)
            )
        return ContentBuildResult(
            contents=tuple(raw_contents or ()),
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
        """Build photo inputs and translate runtime skips to item outcomes."""

        if not isinstance(plan, AlbumPlan):
            raise TypeError("ImageStrategy.build_contents() 需要 AlbumPlan")
        if str(plan.kind).strip().lower() != self.kind:
            raise ValueError(f"图片策略不能构建 {plan.kind!r} Album")
        if not plan.pending_items:
            return ContentBuildResult(ready_items=())
        _check_cancel(cancel_token)
        builder = getattr(self.legacy, "build_image_contents", None)
        if not callable(builder):
            raise TypeError("legacy image module 缺少 build_image_contents()")
        paths = tuple(item.path for item in plan.pending_items)
        caption = self._caption_for_items(plan, plan.pending_items, context)
        cancel_event = _CancelEventBridge(cancel_token)
        ui = _StrategyUI(event_sink)
        with self._legacy_scope(Path(plan.source_root), plan.pending_items):
            result = _call_supported(
                builder,
                (paths, caption),
                ui=ui,
                cancel_event=cancel_event,
            )
        _check_cancel(cancel_token)
        normalized = self._normalize_contents(result, plan)
        if (
            normalized.ready_items
            and len(normalized.ready_items) != len(plan.pending_items)
            and bool(getattr(self._config, "IMAGE_CAPTION_INCLUDE_FILENAMES", False))
            and normalized.contents
        ):
            relabeled = self._caption_for_items(plan, normalized.ready_items, context)
            first = dict(normalized.contents[0])
            formatter = getattr(self.legacy, "formatted_text", None)
            if callable(formatter):
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
        """Expose one-item legacy preflight for an engine context adapter."""

        if not isinstance(item, MediaItem) or item.media_kind != self.kind:
            raise TypeError("图片预检需要 image MediaItem")
        token = context.cancel_token if context is not None else None
        _check_cancel(token)
        checker = getattr(self.legacy, "preflight_images", None)
        if not callable(checker):
            return {"status": "READY"}
        ui = _StrategyUI(context.event_sink if context is not None else None)
        cancel_event = _CancelEventBridge(token)
        with self._legacy_scope(item.source_root, (item,)):
            skipped = _call_supported(
                checker,
                ((item.path,),),
                ui=ui,
                cancel_event=cancel_event,
            )
        _check_cancel(token)
        if not skipped:
            return {"status": "READY"}
        record = skipped[0] if isinstance(skipped[0], Mapping) else {}
        category = self._category(record)
        return {
            "status": "DEFERRED" if category == "deferred" else "FAILED",
            "reason": str(record.get("reason", "图片预检失败") or "图片预检失败"),
        }


ImageMediaStrategy = ImageStrategy


__all__ = ["ImageMediaStrategy", "ImageStrategy"]
