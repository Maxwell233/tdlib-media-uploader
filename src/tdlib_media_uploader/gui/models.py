"""Preview models and the legacy dictionary translation at the GUI edge.

The pages still consume the V1.9 dictionary-shaped preview payload.  Keeping
that translation here lets the package own the V2-to-Qt boundary without
making the page widgets know about strategy internals.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from functools import lru_cache
import os
from pathlib import Path
from typing import Any

from ..core.album import CaptionStore
from ..core.logging import write_app_log
from ..core.filesystem_legacy import stable_path

from ..core.models import MediaItem


@lru_cache(maxsize=32768)
def _path_size(path: str) -> int:
    try:
        return int(os.stat(path).st_size)
    except OSError:
        return 0


def item_size(value: Any) -> int:
    """Read a preview item's current size, matching the legacy page contract."""

    if isinstance(value, Mapping):
        raw_size = value.get("scan_size", value.get("size"))
        if raw_size is not None:
            try:
                return max(0, int(raw_size))
            except (TypeError, ValueError):
                pass
        path = value.get("path")
    else:
        snapshot = getattr(value, "snapshot", None)
        raw_size = getattr(snapshot, "size", None)
        if raw_size is not None:
            try:
                return max(0, int(raw_size))
            except (TypeError, ValueError):
                pass
        path = getattr(value, "path", value)
    return _path_size(str(path))


def item_identity(item: MediaItem) -> tuple[object, ...]:
    return (
        stable_path(item.path),
        stable_path(item.source_root),
        str(item.media_kind),
        str(item.snapshot.path),
        int(item.snapshot.size),
        int(item.snapshot.mtime_ns),
    )


def item_dict(item: MediaItem) -> dict:
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


def caption_payload(
    plan: Any,
    kind: str,
    *,
    caption_store_factory: Callable[..., Any] = CaptionStore,
) -> dict:
    default_label = (
        str(plan.number)
        if kind == "image"
        else str(plan.group_label or ("Album " + str(plan.number)))
    )
    try:
        record = caption_store_factory(kind).get(plan.key, default_label)
    except (OSError, TypeError, ValueError):
        record = {
            "base_label": default_label,
            "custom_text": "",
        }
    return {
        "base_label": str(record.get("base_label", default_label) or ""),
        "custom_text": str(record.get("custom_text", "") or ""),
        "text": str(plan.caption or ""),
    }


def plan_dict(
    plan: Any,
    kind: str,
    *,
    caption_store_factory: Callable[..., Any] = CaptionStore,
) -> dict:
    full_items = [item_dict(item) for item in plan.items]
    pending_keys = {item_identity(item) for item in plan.pending_items}
    pending_items = [
        item
        for item, model in zip(full_items, plan.items)
        if item_identity(model) in pending_keys
    ]
    result = {
        "key": plan.key,
        "number": plan.number,
        "items": full_items,
        "pending_items": pending_items,
        "caption": caption_payload(
            plan,
            kind,
            caption_store_factory=caption_store_factory,
        ),
    }
    if kind == "video":
        result["month_key"] = plan.items[0].month_key if plan.items else plan.group_label
    elif kind == "mixed":
        result["group_name"] = plan.group_label
        if plan.items:
            metadata = plan.items[0].metadata
            if isinstance(metadata, Mapping) and metadata.get("group_path") is not None:
                result["group_path"] = metadata["group_path"]
    return result


def group_key(kind: str, item: Any, fallback: str = "") -> tuple[str, str]:
    if kind == "video":
        return kind, str(item.month_key or fallback or "__all_videos__")
    if kind == "mixed":
        metadata = item.metadata if isinstance(item.metadata, Mapping) else {}
        group_path = metadata.get("group_path")
        if group_path is not None:
            return kind, str(group_path)
        return kind, str(item.group_name or fallback)
    return kind, stable_path(item.path)


def _default_cancelled_result(
    bundle: Any,
    kind: str,
    *,
    core_available: bool,
    warning: str,
    scan_errors,
    scan_warnings,
) -> dict:
    return {
        "kind": kind,
        "status": "cancelled",
        "cancelled": True,
        "items": [],
        "missing": [],
        "groups": [],
        "total_files": 0,
        "completed_files": 0,
        "pending_files": 0,
        "total_bytes": 0,
        "pending_bytes": 0,
        "album_count": 0,
        "completed_paths": [],
        "source_dir": str(bundle.source_root),
        "state_path": str(getattr(bundle.state, "path", "") or ""),
        "core_available": core_available,
        "warning": warning,
        "scan_errors": list(scan_errors or ()),
        "scan_warnings": list(scan_warnings or ()),
        "scan_size_skips": [],
        "scan_skipped_files": 0,
        "scan_compress_files": 0,
        "ignored_root_media": [],
        "target": dict(bundle.target),
    }


def scan_result(
    bundle: Any,
    *,
    progress_callback: Callable[[dict], None] | None = None,
    cancel_event: Any | None = None,
    cancelled_result_factory: Callable[..., dict] | None = None,
    size_resolver: Callable[[Any], int] | None = None,
    logger: Callable[..., Any] | None = write_app_log,
    caption_store_factory: Callable[..., Any] = CaptionStore,
) -> dict:
    """Translate a V2 scan bundle into the existing preview page payload."""

    del progress_callback
    kind = bundle.kind
    scan = bundle.scan_result
    if scan.cancelled or (cancel_event is not None and cancel_event.is_set()):
        kwargs = {
            "core_available": True,
            "warning": "扫描已取消",
            "scan_errors": scan.errors,
            "scan_warnings": scan.warnings,
        }
        if cancelled_result_factory is not None:
            return cancelled_result_factory(kind, **kwargs)
        return _default_cancelled_result(bundle, kind, **kwargs)

    size_of = size_resolver or item_size
    models = tuple(scan.items)
    item_values = [item_dict(item) for item in models]
    plans = [
        plan_dict(item, kind, caption_store_factory=caption_store_factory)
        for item in bundle.plans
    ]

    grouped_items: dict[tuple[str, str], list] = defaultdict(list)
    group_labels: dict[tuple[str, str], str] = {}
    group_plans: dict[tuple[str, str], list[dict]] = defaultdict(list)
    image_plan_for_item: dict[tuple[object, ...], tuple[str, str]] = {}
    if kind == "image":
        for model_plan in bundle.plans:
            plan_group = (kind, str(model_plan.key))
            for item in model_plan.items:
                image_plan_for_item[item_identity(item)] = plan_group
    for item in models:
        key = image_plan_for_item.get(
            item_identity(item),
            group_key(kind, item),
        )
        grouped_items[key].append(item_dict(item))
    for model_plan, plan in zip(bundle.plans, plans):
        if kind == "image":
            key = (kind, str(model_plan.key))
        else:
            first = model_plan.items[0] if model_plan.items else None
            key = group_key(kind, first, model_plan.group_label) if first is not None else (
                kind,
                str(model_plan.group_label),
            )
        group_plans[key].append(plan)
        group_labels.setdefault(key, str(model_plan.group_label or key[1]))

    groups = []
    ordered_keys = list(grouped_items)
    for key in group_plans:
        if key not in grouped_items:
            ordered_keys.append(key)
    for key in ordered_keys:
        group_items = grouped_items.get(key, [])
        plans_for_group = group_plans.get(key, [])
        pending = sum(len(plan.get("pending_items", ())) for plan in plans_for_group)
        total = sum(len(plan.get("items", ())) for plan in plans_for_group)
        if not plans_for_group:
            total = len(group_items)
            pending = len(group_items)
        groups.append(
            {
                "label": group_labels.get(key, key[1]),
                "caption": (
                    plans_for_group[0].get("caption", {}).get("text", "")
                    if plans_for_group
                    else ""
                ),
                "items": group_items,
                "pending": pending,
                "completed": max(0, total - pending),
                "albums": sum(bool(plan.get("pending_items")) for plan in plans_for_group),
                "album_plans": plans_for_group,
            }
        )

    completed_keys = {
        item_identity(item)
        for model_plan in bundle.plans
        for item in model_plan.items
        if item_identity(item)
        not in {item_identity(pending) for pending in model_plan.pending_items}
    }
    completed_paths = [
        stable_path(item.path)
        for item in models
        if item_identity(item) in completed_keys
    ]
    scan_errors = [str(value) for value in scan.errors if str(value)]
    scan_warnings = [str(value) for value in scan.warnings if str(value)]
    scan_size_skips = [
        dict(value)
        for value in (getattr(bundle.legacy, "LAST_SCAN_SIZE_SKIPS", ()) or ())
        if isinstance(value, dict)
    ]
    ignored_root_media = list(getattr(bundle.legacy, "LAST_SCAN_IGNORED_ROOT_MEDIA", ()) or ())
    warning = ""
    if scan_errors:
        if logger is not None:
            logger(
                "WARNING",
                "目录扫描跳过项目：\n" + "\n".join(scan_errors),
                source=f"scan/{kind}",
            )
        warning = f"扫描时跳过 {len(scan_errors)} 个暂时无法读取的项目（可能是 SMB 连接中断）。"
    if scan_warnings:
        if logger is not None:
            logger(
                "WARNING",
                "目录扫描提醒：\n" + "\n".join(scan_warnings),
                source=f"scan/{kind}",
            )
        warning = "；".join(scan_warnings) + (f"；{warning}" if warning else "")
    rejected = [record for record in scan_size_skips if record.get("action", "skip") == "skip"]
    preflight_size = [record for record in scan_size_skips if record.get("action") == "preflight"]
    compressing = [record for record in scan_size_skips if record.get("action") == "compress"]
    if rejected:
        warning = (
            f"扫描时跳过 {len(rejected)} 个超过 Telegram 限制的项目"
            + (f"；{warning}" if warning else "")
        )
    if compressing:
        warning = (
            f"发现 {len(compressing)} 个超限图片，将在上传时使用 FFmpeg 压缩临时副本"
            + (f"；{warning}" if warning else "")
        )
    if preflight_size:
        warning = (
            f"已扫描到 {len(preflight_size)} 个超过 Telegram 视频上限的文件，上传前会跳过"
            + (f"；{warning}" if warning else "")
        )
    if ignored_root_media:
        warning = (
            f"混合根目录中有 {len(ignored_root_media)} 个媒体已忽略，请移动到一级子文件夹后重新扫描"
            + (f"；{warning}" if warning else "")
        )

    total_bytes = sum(size_of(item) for item in item_values)
    pending_items = [
        item
        for model_plan in bundle.plans
        for item in model_plan.pending_items
    ]
    pending_bytes = sum(size_of(item_dict(item)) for item in pending_items)
    return {
        "kind": kind,
        "scan_result": scan,
        "plans": tuple(bundle.plans),
        "items": item_values,
        "missing": [str(path) for path in bundle.missing],
        "groups": groups,
        "total_files": len(item_values),
        "completed_files": len(completed_keys),
        "pending_files": len(item_values) - len(completed_keys),
        "total_bytes": total_bytes,
        "pending_bytes": pending_bytes,
        "album_count": sum(bool(plan.get("pending_items")) for plan in plans),
        "completed_paths": completed_paths,
        "source_dir": str(bundle.source_root),
        "state_path": str(getattr(bundle.state, "path", "") or ""),
        "core_available": True,
        "warning": warning,
        "scan_errors": scan_errors,
        "scan_warnings": scan_warnings,
        "cancelled": False,
        "scan_size_skips": scan_size_skips,
        "scan_skipped_files": len(rejected),
        "scan_compress_files": len(compressing),
        "ignored_root_media": [str(path) for path in ignored_root_media],
        "target": dict(bundle.target),
    }


__all__ = [
    "caption_payload",
    "group_key",
    "item_dict",
    "item_identity",
    "item_size",
    "plan_dict",
    "scan_result",
]
