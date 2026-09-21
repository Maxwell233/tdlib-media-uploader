"""V2 adapter for the legacy mixed-media uploader.

The legacy mixed module still owns the executable scanner, caption store and
photo/video TDLib content builders.  This adapter keeps that behavior behind
the V2 :class:`~tdlib_media_uploader.contracts.MediaStrategy` boundary.  It
does not send anything itself: the V2 upload engine remains the owner of
journaling, transport calls and checkpoints.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
import importlib
import inspect
from pathlib import Path
import threading
from typing import Any

from ..contracts import CancelToken, EventSink, UploadContext
from ..core.models import (
    AlbumPlan,
    ContentBuildResult,
    FileSnapshot,
    LogEvent,
    MediaItem,
    ProgressEvent,
    ScanResult,
)


_LEGACY_SCOPE_LOCK = threading.RLock()
_MISSING = object()


def _load_legacy_module() -> Any:
    """Load the bundled mixed-media compatibility module."""

    return importlib.import_module("tdlib_media_uploader.media.legacy_mixed")


def _call_compatible(function: Any, args: Sequence[Any] = (), **kwargs: Any) -> Any:
    """Call a legacy hook while tolerating small embedding fakes."""

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


def _is_cancelled(token: CancelToken | None) -> bool:
    if token is None:
        return False
    checker = getattr(token, "is_cancelled", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            pass
    return bool(getattr(token, "cancelled", False))


def _raise_if_cancelled(token: CancelToken | None) -> None:
    if token is None:
        return
    raiser = getattr(token, "raise_if_cancelled", None)
    if callable(raiser):
        raiser()
    elif _is_cancelled(token):
        raise RuntimeError("混合媒体内容构建已取消")


class _LegacyCancelEvent:
    """Expose the V2 cancellation protocol as the legacy ``Event`` shape."""

    def __init__(self, token: CancelToken | None):
        self._token = token

    def is_set(self) -> bool:
        return _is_cancelled(self._token)


class _EventUI:
    """Small GUI-free presentation facade required by the legacy builder."""

    def __init__(self, event_sink: EventSink | None):
        self._event_sink = event_sink

    def _emit(self, level: str, message: Any) -> None:
        emit = getattr(self._event_sink, "emit", None)
        if not callable(emit):
            return
        try:
            emit(LogEvent(level=level, message=str(message), source="mixed-strategy"))
        except Exception:
            # A progress/log consumer must not change a source-read decision.
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
        raise ValueError(f"混合媒体项缺少扫描快照：{path}")
    return FileSnapshot(str(path), int(size), int(mtime_ns))


def _raw_identity(raw: Any, default_source_root: Path) -> tuple[object, ...] | None:
    if isinstance(raw, MediaItem):
        return _model_identity(raw)
    if not isinstance(raw, Mapping) or raw.get("path") is None:
        return None
    path = Path(raw["path"])
    source_root = Path(raw.get("source_root") or default_source_root)
    media_kind = str(raw.get("media_kind", ""))
    try:
        snapshot = _raw_snapshot(raw, path)
    except (TypeError, ValueError, KeyError):
        return None
    return (
        str(path),
        str(source_root),
        media_kind,
        snapshot.path,
        snapshot.size,
        snapshot.mtime_ns,
    )


def _caption_text(value: Any) -> str:
    if isinstance(value, Mapping):
        nested = value.get("caption")
        if isinstance(nested, Mapping):
            value = nested
        return str(value.get("text", "") or "")
    return str(value or "")


class _LegacyStateBridge:
    """Present V2 ``MediaItem`` values to a legacy path/dict state API."""

    def __init__(
        self,
        strategy: "MixedMediaStrategy",
        state: Any,
        *,
        lookup: Mapping[tuple[object, ...], MediaItem],
        source_root: Path,
    ):
        self.strategy = strategy
        self.state = state
        self.lookup = lookup
        self.source_root = source_root

    def is_completed(self, value: Any) -> bool:
        item = self.strategy._resolve_model(
            value,
            lookup=self.lookup,
            source_root=self.source_root,
        )
        return self.strategy._state_completed(self.state, item)


class MixedMediaStrategy:
    """Concrete V2 strategy backed by the bundled mixed-media implementation."""

    kind = "mixed"

    def __init__(
        self,
        legacy_module: Any | None = None,
        *,
        legacy: Any | None = None,
        config: Any | None = None,
        source_root: Path | None = None,
    ):
        if legacy_module is not None and legacy is not None:
            raise TypeError("legacy_module 和 legacy 只能指定一个")
        # Injection keeps the adapter independently testable while the default
        # path explicitly reuses the production V1.9 implementation.
        self._legacy = (
            legacy_module
            if legacy_module is not None
            else legacy if legacy is not None else _load_legacy_module()
        )
        self._config_override = config
        self.source_root = Path(source_root) if source_root is not None else None

    @property
    def legacy_module(self) -> Any:
        """Return the delegated module for integration diagnostics/tests."""

        return self._legacy

    @property
    def _config(self) -> Any:
        return getattr(self._legacy, "cfg", None) or self._config_override or self._legacy

    def _root(self, source_root: Path | None = None) -> Path:
        if source_root is not None:
            return Path(source_root)
        if self.source_root is not None:
            return self.source_root
        configured = getattr(self._config, "MIXED_DIR", None)
        return Path(configured) if configured is not None else Path(".")

    @contextmanager
    def _legacy_scope(self, source_root: Path):
        """Temporarily align the legacy root with the V2 run context."""

        config = getattr(self._legacy, "cfg", None) or self._config_override
        with _LEGACY_SCOPE_LOCK:
            previous = getattr(config, "MIXED_DIR", _MISSING)
            try:
                if previous is not _MISSING:
                    config.MIXED_DIR = Path(source_root)
                yield
            finally:
                if previous is not _MISSING:
                    config.MIXED_DIR = previous

    @staticmethod
    def _legacy_item(item: MediaItem) -> dict[str, Any]:
        """Translate a V2 item to the dictionary accepted by V1.9 code."""

        metadata = item.metadata if isinstance(item.metadata, Mapping) else {}
        result: dict[str, Any] = {
            "path": item.path,
            "source_root": item.source_root,
            "media_kind": item.media_kind,
            "group_name": item.group_name,
            "scan_size": item.snapshot.size,
            "scan_mtime_ns": item.snapshot.mtime_ns,
            "requires_premium": bool(metadata.get("requires_premium", False)),
        }
        if item.capture_time is not None:
            result["capture_time"] = item.capture_time
        if item.month_key is not None:
            result["month_key"] = item.month_key
        if item.date_tag:
            result["date_tag"] = item.date_tag
        return result

    @classmethod
    def _media_item(
        cls,
        raw: Any,
        *,
        source_root: Path,
        group_name: str,
        group_path: Path,
    ) -> MediaItem:
        if isinstance(raw, MediaItem):
            return raw
        if not isinstance(raw, Mapping) or raw.get("path") is None:
            raise TypeError("legacy mixed scanner 返回了无效媒体项")
        path = Path(raw["path"])
        snapshot = _raw_snapshot(raw, path)
        metadata = raw.get("metadata")
        normalized_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        normalized_metadata["group_path"] = Path(group_path)
        normalized_metadata["requires_premium"] = bool(
            raw.get("requires_premium", normalized_metadata.get("requires_premium", False))
        )
        return MediaItem(
            path=path,
            source_root=Path(source_root),
            media_kind=str(raw.get("media_kind", "")),
            snapshot=snapshot,
            capture_time=raw.get("capture_time"),
            group_name=str(raw.get("group_name", group_name) or group_name),
            month_key=raw.get("month_key"),
            date_tag=str(raw.get("date_tag", "") or ""),
            fallback=bool(raw.get("fallback", False)),
            metadata=normalized_metadata,
        )

    @classmethod
    def _resolve_model(
        cls,
        raw: Any,
        *,
        lookup: Mapping[tuple[object, ...], MediaItem],
        source_root: Path,
        group_name: str = "",
    ) -> MediaItem:
        identity = _raw_identity(raw, source_root)
        if identity is not None and identity in lookup:
            return lookup[identity]
        if isinstance(raw, MediaItem):
            raise ValueError(f"legacy mixed planner 返回了未知项：{raw.path}")
        if not isinstance(raw, Mapping):
            raise ValueError(f"legacy mixed planner 返回了未知项：{raw!r}")
        path = Path(raw.get("path")) if raw.get("path") is not None else None
        media_kind = str(raw.get("media_kind", "") or "")
        if path is not None:
            matches = [
                item
                for item in lookup.values()
                if Path(item.path) == path
                and (not media_kind or item.media_kind == media_kind)
            ]
            if len(matches) == 1:
                return matches[0]
        if path is None:
            raise ValueError("legacy mixed planner 返回了缺少 path 的项")
        path = Path(raw["path"])
        group_path = Path(raw.get("group_path") or source_root / group_name)
        return cls._media_item(
            raw,
            source_root=source_root,
            group_name=group_name,
            group_path=group_path,
        )

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

    @classmethod
    def _groups_for_scan(
        cls,
        scan_result: ScanResult,
    ) -> tuple[list[dict[str, Any]], dict[tuple[object, ...], MediaItem], Path]:
        items = tuple(scan_result.items)
        if not items:
            return [], {}, Path(".")
        source_root = Path(items[0].source_root)
        lookup: dict[tuple[object, ...], MediaItem] = {}
        groups: dict[str, dict[str, Any]] = {}
        for item in items:
            if Path(item.source_root) != source_root:
                raise ValueError("一个混合扫描结果不能包含多个 source_root")
            identity = _model_identity(item)
            if identity in lookup:
                raise ValueError(f"混合 MediaItem identity 重复：{item.path}")
            lookup[identity] = item
            group_name = str(item.group_name or "").strip()
            if not group_name:
                try:
                    group_name = item.path.relative_to(source_root).parts[0]
                except (ValueError, IndexError):
                    raise ValueError(f"混合媒体项缺少一级分组：{item.path}") from None
            metadata = item.metadata if isinstance(item.metadata, Mapping) else {}
            group_path = Path(metadata.get("group_path") or source_root / group_name)
            group = groups.setdefault(
                group_name,
                {
                    "group_name": group_name,
                    "group_path": group_path,
                    "items": [],
                },
            )
            group["items"].append(cls._legacy_item(item))
        return list(groups.values()), lookup, source_root

    @staticmethod
    def _scan_diagnostics(legacy: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
        errors = tuple(str(value) for value in getattr(legacy, "LAST_SCAN_ERRORS", ()) if str(value))
        warnings = [
            str(value)
            for value in getattr(legacy, "LAST_SCAN_WARNINGS", ())
            if str(value)
        ]
        size_skips = list(getattr(legacy, "LAST_SCAN_SIZE_SKIPS", ()))
        rejected = [record for record in size_skips if record.get("action") == "skip"]
        preflight = [record for record in size_skips if record.get("action") == "preflight"]
        compressing = [record for record in size_skips if record.get("action") == "compress"]
        if rejected:
            warnings.append(
                f"扫描时跳过 {len(rejected)} 个超过 Telegram 限制的混合媒体；"
                "这些文件未加入上传计划。"
            )
        if compressing:
            warnings.append(
                f"扫描提醒：发现 {len(compressing)} 个超限图片；"
                "上传时将尝试用 FFmpeg 生成临时压缩副本。"
            )
        if preflight:
            warnings.append(
                f"已扫描到 {len(preflight)} 个超过 Telegram 视频上限的混合媒体，"
                "这些文件将在上传前跳过。"
            )
        ignored = list(getattr(legacy, "LAST_SCAN_IGNORED_ROOT_MEDIA", ()))
        if ignored:
            warnings.append(
                f"混合根目录中有 {len(ignored)} 个媒体已忽略；"
                "请将文件移动到一级子文件夹后重新扫描。"
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
        """Delegate discovery and translate its immutable snapshots."""

        del context
        root = Path(source_root)
        cancel_event = _LegacyCancelEvent(cancel_token)
        with self._legacy_scope(root):
            raw_groups = _call_compatible(
                getattr(self._legacy, "scan_mixed_groups"),
                cancel_event=cancel_event,
            )
            errors, warnings = self._scan_diagnostics(self._legacy)
            translated: list[MediaItem] = []
            for raw_group in raw_groups or ():
                if not isinstance(raw_group, Mapping):
                    raise TypeError("legacy mixed scanner 返回了无效分组")
                group_name = str(raw_group.get("group_name", "") or "")
                group_path = Path(raw_group.get("group_path") or root / group_name)
                for raw_item in raw_group.get("items", ()):
                    translated.append(
                        self._media_item(
                            raw_item,
                            source_root=root,
                            group_name=group_name,
                            group_path=group_path,
                        )
                    )
        cancelled = _is_cancelled(cancel_token) or any("取消" in warning for warning in warnings)
        emit = getattr(event_sink, "emit", None)
        if callable(emit):
            try:
                emit(
                    ProgressEvent(
                        kind=self.kind,
                        phase="scan",
                        completed=len(translated),
                        total=len(translated),
                        message="扫描完成" if not cancelled else "扫描已取消",
                    )
                )
            except Exception:
                pass
        return ScanResult(
            items=tuple(translated),
            errors=errors,
            warnings=warnings,
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
        """Reuse legacy grouping/caption/Album-key behavior."""

        del context
        if not isinstance(scan_result, ScanResult):
            raise TypeError("MixedMediaStrategy.build_plans() 需要 ScanResult")
        groups, lookup, source_root = self._groups_for_scan(scan_result)
        if not groups:
            return ()
        state_bridge = (
            _LegacyStateBridge(
                self,
                state,
                lookup=lookup,
                source_root=source_root,
            )
            if state is not None
            else None
        )
        with self._legacy_scope(source_root):
            raw_plans = _call_compatible(
                getattr(self._legacy, "build_album_plans"),
                (groups,),
                state=state_bridge,
            )
        plans: list[AlbumPlan] = []
        for raw_plan in raw_plans or ():
            if not isinstance(raw_plan, Mapping):
                raise TypeError("legacy mixed planner 返回了无效 Album")
            group_label = str(
                raw_plan.get("group_name", raw_plan.get("group_label", "")) or ""
            )
            key = str(raw_plan.get("key", "") or "")
            if not key:
                raise ValueError("legacy mixed planner 返回了缺少稳定 key 的 Album")
            full_items = tuple(
                self._resolve_model(
                    raw_item,
                    lookup=lookup,
                    source_root=source_root,
                    group_name=group_label,
                )
                for raw_item in raw_plan.get("items", ())
            )
            raw_pending = raw_plan.get("pending_items", raw_plan.get("items", ()))
            pending_items = tuple(
                self._resolve_model(
                    raw_item,
                    lookup=lookup,
                    source_root=source_root,
                    group_name=group_label,
                )
                for raw_item in raw_pending
            )
            plans.append(
                AlbumPlan(
                    key=key,
                    kind=self.kind,
                    source_root=source_root,
                    group_label=group_label,
                    number=int(raw_plan.get("number", 1)),
                    items=full_items,
                    pending_items=pending_items,
                    caption=_caption_text(raw_plan.get("caption", "")),
                    target=dict(target or {}),
                )
            )
        return tuple(plans)

    def _caption_settings(self, context: UploadContext | None) -> tuple[bool, bool, int]:
        config = getattr(self._legacy, "cfg", None) or self._config_override
        include = bool(getattr(config, "MIXED_CAPTION_INCLUDE_FILENAMES", False))
        numbered = bool(getattr(config, "MIXED_CAPTION_INCLUDE_FILENAME_NUMBERS", True))
        limit_value = 1024
        if context is not None:
            limit_value = context.metadata.get("caption_limit", 1024)
            if limit_value is None:
                limit_value = 1024
        return include, numbered, int(limit_value)

    def _caption_for_items(
        self,
        plan: AlbumPlan,
        items: Sequence[MediaItem],
        *,
        context: UploadContext | None,
    ) -> str:
        include, numbered, limit = self._caption_settings(context)
        formatter = getattr(self._legacy, "with_filename_description")
        return str(
            _call_compatible(
                formatter,
                (plan.caption, tuple(self._legacy_item(item) for item in items), include, numbered),
                max_chars=limit,
            )
        )

    @staticmethod
    def _resolve_outcome_item(
        raw_item: Any,
        record: Mapping[str, Any],
        *,
        pending: Sequence[MediaItem],
        lookup: Mapping[tuple[object, ...], MediaItem],
        source_root: Path,
    ) -> MediaItem:
        if raw_item is not None:
            identity = _raw_identity(raw_item, source_root)
            if identity is not None and identity in lookup:
                return lookup[identity]
        raw_path = record.get("path")
        if raw_path is not None:
            matches = [item for item in pending if str(item.path) == str(raw_path)]
            if len(matches) == 1:
                return matches[0]
        raise ValueError(f"legacy mixed builder 返回了未知 outcome：{raw_item!r}")

    def _normalize_contents(
        self,
        result: Any,
        plan: AlbumPlan,
        *,
        context: UploadContext | None,
    ) -> ContentBuildResult:
        if not isinstance(result, Sequence) or len(result) != 3:
            raise TypeError("legacy mixed builder 必须返回 (contents, valid, skipped)")
        raw_contents, raw_valid, raw_skipped = result
        pending = tuple(plan.pending_items)
        pending_lookup = {_model_identity(item): item for item in pending}
        if len(pending_lookup) != len(pending):
            raise ValueError("Album 待上传项 identity 重复")
        ready: list[MediaItem] = []
        deferred: list[MediaItem] = []
        failed: list[MediaItem] = []
        errors: list[str] = []
        seen: set[tuple[object, ...]] = set()

        def add_outcome(item: MediaItem, bucket: list[MediaItem]) -> None:
            identity = _model_identity(item)
            if identity not in pending_lookup:
                raise ValueError("legacy mixed builder 返回了不属于当前 Album 的项")
            if identity in seen:
                raise ValueError("legacy mixed builder 重复返回了 item outcome")
            seen.add(identity)
            bucket.append(pending_lookup[identity])

        for raw_item in raw_valid or ():
            identity = _raw_identity(raw_item, plan.source_root)
            if identity is None or identity not in pending_lookup:
                raise ValueError("legacy mixed builder 返回了不属于当前 Album 的 ready 项")
            add_outcome(pending_lookup[identity], ready)

        for record in raw_skipped or ():
            if not isinstance(record, Mapping):
                raise TypeError("legacy mixed builder 返回了无效 skipped record")
            item = self._resolve_outcome_item(
                record.get("item"),
                record,
                pending=pending,
                lookup=pending_lookup,
                source_root=plan.source_root,
            )
            category = str(record.get("category", "unreadable") or "unreadable").strip().lower()
            bucket = deferred if category in {
                "deferred",
                "changed",
                "network",
                "cancelled",
                "read_failed",
            } else failed
            add_outcome(item, bucket)
            reason = str(record.get("reason", "") or "").strip()
            if reason:
                errors.append(reason)

        missing = [item for item in pending if _model_identity(item) not in seen]
        if missing:
            raise ValueError(
                "legacy mixed builder 未覆盖全部待上传项："
                + ", ".join(str(item.path) for item in missing)
            )

        contents = tuple(raw_contents or ())
        include, numbered, _limit = self._caption_settings(context)
        if ready and len(ready) != len(pending) and include and contents:
            caption = self._caption_for_items(plan, ready, context=context)
            formatter = getattr(self._legacy, "formatted_text", None)
            if not callable(formatter):
                raise AttributeError("legacy mixed module 缺少 formatted_text()")
            first = dict(contents[0])
            first["caption"] = formatter(caption)
            contents = (first, *contents[1:])
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
        """Delegate mixed photo/video content construction without sending."""

        if not isinstance(plan, AlbumPlan):
            raise TypeError("MixedMediaStrategy.build_contents() 需要 AlbumPlan")
        if str(plan.kind).strip().lower() != self.kind:
            raise ValueError(f"混合策略不能构建 {plan.kind!r} Album")
        if not plan.pending_items:
            return ContentBuildResult(ready_items=())

        _raise_if_cancelled(cancel_token)
        legacy_items = tuple(self._legacy_item(item) for item in plan.pending_items)
        caption = self._caption_for_items(plan, plan.pending_items, context=context)
        cancel_event = _LegacyCancelEvent(cancel_token)
        ui = _EventUI(event_sink)
        with self._legacy_scope(Path(plan.source_root)):
            result = _call_compatible(
                getattr(self._legacy, "build_mixed_contents"),
                (legacy_items, caption),
                ui=ui,
                cancel_event=cancel_event,
            )
            _raise_if_cancelled(cancel_token)
        return self._normalize_contents(result, plan, context=context)

    def preflight_item(
        self,
        item: MediaItem,
        *,
        context: UploadContext | None = None,
    ) -> Mapping[str, str]:
        """Expose the legacy mixed-media readiness check to UploadEngine."""

        if not isinstance(item, MediaItem) or item.media_kind not in {"image", "video"}:
            raise TypeError("混合媒体预检需要 image/video MediaItem")
        token = context.cancel_token if context is not None else None
        _raise_if_cancelled(token)
        checker = getattr(self._legacy, "preflight_mixed", None)
        if not callable(checker):
            return {"status": "READY"}
        sink = context.event_sink if context is not None else None
        ui = _EventUI(sink)
        cancel_event = _LegacyCancelEvent(token)
        with self._legacy_scope(Path(item.source_root)):
            skipped = _call_compatible(
                checker,
                ((self._legacy_item(item),),),
                ui=ui,
                cancel_event=cancel_event,
            )
        _raise_if_cancelled(token)
        if not skipped:
            return {"status": "READY"}
        record = skipped[0] if isinstance(skipped[0], Mapping) else {}
        category = str(record.get("category", "unreadable") or "unreadable").strip().lower()
        deferred = category in {
            "deferred",
            "changed",
            "network",
            "cancelled",
            "read_failed",
        }
        return {
            "status": "DEFERRED" if deferred else "FAILED",
            "reason": str(record.get("reason", "混合媒体预检失败") or "混合媒体预检失败"),
        }


# Keep the shorter name available for callers that use the strategy names from
# the architecture map while making the explicit V2 class the canonical one.
MixedStrategy = MixedMediaStrategy


__all__ = ["MixedMediaStrategy", "MixedStrategy"]
