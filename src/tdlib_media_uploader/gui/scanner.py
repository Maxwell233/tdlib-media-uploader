# -*- coding: utf-8 -*-
"""Media scanner boundary and preview adapter for TDLib Media Uploader GUI."""

from __future__ import annotations

from collections import defaultdict
import datetime as _dt
import functools
import os
from pathlib import Path
import stat
from typing import Any

from ..config.paths import (
    IMAGE_STATE_DIR,
    MIXED_STATE_DIR,
    RESOURCE_DIR,
    VIDEO_STATE_DIR,
)
from ..core.album import CaptionStore, album_key, validate_caption
from ..core.filesystem_legacy import (
    file_mtime,
    is_link_or_junction,
    iter_directory_entries_with_retry,
    iter_files,
    media_path_sort,
    retry_fs_operation,
    stable_path,
    validate_scan_root,
)
from ..core.logging import write_app_log
from .config_service import _CONFIG_ERROR, cfg, get_cfg, target_for
from .models import scan_result as _translate_v2_scan_result
from .tools import (
    KIND_PATH_KEYS,
    MEDIA_KINDS,
    format_date,
    format_size,
    require_kind,
)

PROJECT_DIR = RESOURCE_DIR

BASIC_SCAN_SNAPSHOTS: dict[str, tuple[int, int]] = {}


@functools.lru_cache(maxsize=32768)
def path_size(path_str: str) -> int:
    try:
        return int(os.stat(path_str).st_size)
    except OSError:
        return 0


def item_size(item: Any) -> int:
    path = item["path"] if isinstance(item, dict) else item
    return path_size(str(path))


def apply_size_limits(paths: list[Path], kind: str) -> tuple[list[Path], list[dict]]:
    """Apply Telegram size limits for the GUI fallback scanner."""
    if kind not in {"video", "image", "mixed"}:
        raise ValueError(f"不支持基础扫描类型：{kind}")
    accepted = []
    skipped = []
    for path in paths:
        suffix = path.suffix.lower()
        if kind == "mixed":
            media_kind = (
                "video"
                if suffix in set(get_cfg("VIDEO_EXTENSIONS", set()))
                else "image"
            )
        else:
            media_kind = kind
        limit = (
            int(get_cfg("TELEGRAM_VIDEO_SIZE_LIMIT_BYTES", 2000 * 1024 * 1024))
            if media_kind == "video"
            else int(get_cfg("TELEGRAM_IMAGE_SIZE_LIMIT_BYTES", 10 * 1024 * 1024))
        )
        compress_images = bool(get_cfg("COMPRESS_OVERSIZED_IMAGES", False))
        media_label = "视频" if media_kind == "video" else "图片"
        limit_label = format_size(limit)
        snapshot = BASIC_SCAN_SNAPSHOTS.get(stable_path(path))
        size = int(snapshot[0]) if snapshot is not None else path_size(str(path))
        if size > limit:
            action = "compress" if compress_images else "skip"
            skipped.append({
                "path": path,
                "media_kind": media_kind,
                "size": size,
                "limit": limit,
                "category": "size",
                "action": action,
                "reason": (
                    f"文件大小 {format_size(size)} 超过 Telegram "
                    f"{media_label} 上限 {limit_label}"
                ),
            })
            if action == "skip":
                continue
        accepted.append(path)
    return accepted, skipped


def basic_mixed_scan(
    root: Path, cancel_event=None
) -> tuple[list[dict], list[Path], list[str], list[str], list[dict]]:
    """Build a dependency-free mixed preview with the same group rules."""
    root = validate_scan_root(
        root,
        attempts=get_cfg("SCAN_DISCOVERY_ATTEMPTS", 3),
        initial_delay=get_cfg("SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
        max_delay=get_cfg("SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
        cancel_event=cancel_event,
    )
    image_extensions = set(get_cfg("IMAGE_EXTENSIONS", set()))
    video_extensions = set(get_cfg("VIDEO_EXTENSIONS", set()))
    overlap = image_extensions & video_extensions
    if overlap:
        values = ", ".join(sorted(overlap))
        raise RuntimeError(f"图片和视频扩展名不能重复：{values}")
    accepted_extensions = image_extensions | video_extensions
    directories = []
    ignored = []
    errors = []
    warnings = []
    try:
        for entry in iter_directory_entries_with_retry(
            root,
            attempts=get_cfg("SCAN_DISCOVERY_ATTEMPTS", 3),
            initial_delay=get_cfg("SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
            max_delay=get_cfg("SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
            cancel_event=cancel_event,
        ):
            if cancel_event is not None and cancel_event.is_set():
                warnings.append("目录扫描已取消")
                break
            try:
                path = Path(entry.path)
                info = retry_fs_operation(
                    lambda entry=entry: entry.stat(follow_symlinks=False),
                    attempts=get_cfg("SCAN_DISCOVERY_ATTEMPTS", 3),
                    initial_delay=get_cfg("SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
                    max_delay=get_cfg("SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
                    cancel_event=cancel_event,
                )
                if is_link_or_junction(entry):
                    warnings.append(f"跳过符号链接或 junction：{entry.path}")
                    continue
                if stat.S_ISDIR(info.st_mode):
                    directories.append(path)
                elif stat.S_ISREG(info.st_mode) and path.suffix.lower() in accepted_extensions:
                    ignored.append(path)
            except OSError as exc:
                errors.append(f"{entry.path}: {exc}")
    except TimeoutError:
        warnings.append("目录扫描已取消")
    except OSError as exc:
        errors.append(f"{root}: {exc}")
    groups = []
    size_skips = []
    for group_path in media_path_sort(directories, root, mode="name"):
        scan_result = iter_files(
            group_path,
            accepted_extensions,
            cancel_event=cancel_event,
            discovery_attempts=get_cfg("SCAN_DISCOVERY_ATTEMPTS", 3),
            discovery_initial_delay=get_cfg("SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
            discovery_max_delay=get_cfg("SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
        )
        errors.extend(scan_result.errors)
        warnings.extend(scan_result.warnings)
        if scan_result.cancelled:
            warnings.append("目录扫描已取消")
        BASIC_SCAN_SNAPSHOTS.update({
            key: snapshot.as_tuple()
            for key, snapshot in getattr(scan_result, "snapshots", {}).items()
        })
        accepted, skipped = apply_size_limits(scan_result.paths, "mixed")
        size_skips.extend(skipped)
        media_items = []
        for path in accepted:
            suffix = path.suffix.lower()
            media_kind = "video" if suffix in video_extensions else "image"
            media_items.append({
                "path": path,
                "media_kind": media_kind,
                "group_name": group_path.name,
                **(
                    {
                        "scan_size": BASIC_SCAN_SNAPSHOTS[stable_path(path)][0],
                        "scan_mtime_ns": BASIC_SCAN_SNAPSHOTS[stable_path(path)][1],
                    }
                    if stable_path(path) in BASIC_SCAN_SNAPSHOTS
                    else {}
                ),
            })
        sort_mode = str(get_cfg("MIXED_SORT_MODE", "name")).strip().lower()
        media_items = media_path_sort(
            media_items,
            group_path,
            mode=sort_mode,
            key_func=lambda item: item["path"],
        )
        groups.append({
            "group_name": group_path.name,
            "group_path": group_path,
            "items": media_items,
            "errors": scan_result.errors,
            "warnings": scan_result.warnings,
        })
    return groups, ignored, errors, warnings, size_skips


def basic_paths(kind: str, cancel_event=None) -> list[Path]:
    kind = str(kind).strip().lower()
    if kind not in MEDIA_KINDS:
        raise ValueError(f"未知媒体类型：{kind}")
    path_key = KIND_PATH_KEYS[kind]
    extension_key = {
        "video": "VIDEO_EXTENSIONS",
        "image": "IMAGE_EXTENSIONS",
        "mixed": "MIXED_EXTENSIONS",
    }[kind]
    root = Path(get_cfg(path_key, PROJECT_DIR))
    extensions = set(get_cfg(extension_key, set()))
    BASIC_SCAN_SNAPSHOTS.clear()
    try:
        root = validate_scan_root(
            root,
            attempts=get_cfg("SCAN_DISCOVERY_ATTEMPTS", 3),
            initial_delay=get_cfg("SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
            max_delay=get_cfg("SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
            cancel_event=cancel_event,
        )
    except TimeoutError:
        return []
    if kind == "mixed":
        groups, _ignored, _errors, _warnings, _skips = basic_mixed_scan(
            root, cancel_event=cancel_event
        )
        return [item["path"] for group in groups for item in group["items"]]
    scan_result = iter_files(
        root,
        extensions,
        cancel_event=cancel_event,
        discovery_attempts=get_cfg("SCAN_DISCOVERY_ATTEMPTS", 3),
        discovery_initial_delay=get_cfg("SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
        discovery_max_delay=get_cfg("SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
    )
    paths = scan_result.paths
    BASIC_SCAN_SNAPSHOTS.update({
        key: snapshot.as_tuple()
        for key, snapshot in getattr(scan_result, "snapshots", {}).items()
    })
    if kind == "image":
        mode = "mtime" if get_cfg("IMAGE_SORT_MODE", "mtime") == "mtime" else "name"
    else:
        mode = (
            "mtime"
            if get_cfg("VIDEO_READ_DATES", True)
            and get_cfg("VIDEO_SORT_MODE", "mtime") == "mtime"
            else "name"
        )
    return media_path_sort(paths, root, mode=mode)


def cancelled_scan_result(
    kind: str,
    *,
    core_available: bool,
    warning: str = "扫描已取消",
    ignored_root_media=None,
    scan_errors=None,
    scan_warnings=None,
    scan_size_skips=None,
) -> dict:
    """Build an explicit cancellation result instead of a fake empty scan."""
    kind = require_kind(kind)
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
        "source_dir": str(get_cfg(KIND_PATH_KEYS[kind], "")),
        "state_path": "",
        "core_available": core_available,
        "warning": warning,
        "scan_errors": list(scan_errors or []),
        "scan_warnings": list(scan_warnings or []),
        "scan_size_skips": list(scan_size_skips or []),
        "scan_skipped_files": 0,
        "scan_compress_files": 0,
        "ignored_root_media": [str(path) for path in (ignored_root_media or [])],
        "target": target_for(kind),
    }


def legacy_scan_result(kind: str, progress_callback=None, cancel_event=None) -> dict:
    """Scan using fallback routines when V2 bundle integration is unavailable."""
    kind = str(kind).strip().lower()
    if kind not in MEDIA_KINDS:
        raise ValueError(f"未知媒体类型：{kind}")
    path_size.cache_clear()
    if cfg is None:
        raise RuntimeError(_CONFIG_ERROR or "配置不可用。")
    activate = getattr(cfg, "activate_target", None)
    if callable(activate):
        activate(kind)

    core = None
    mixed_groups = []
    ignored_root_media: list[Path] = []
    warning = ""
    scan_errors: list[str] = []
    scan_warnings: list[str] = []
    scan_size_skips: list[dict] = []
    try:
        if kind == "video":
            from ..media import legacy_video as core_module
        elif kind == "image":
            from ..media import legacy_image as core_module
        elif kind == "mixed":
            from ..media import legacy_mixed as core_module
        else:
            raise ValueError(f"未知媒体类型：{kind}")
        core = core_module
    except Exception as exc:
        warning = f"当前环境尚未加载完整上传依赖，预览使用基础扫描：{exc}"

    state = None
    if kind == "video":
        if core is not None:
            core.STATE_DIR = VIDEO_STATE_DIR
            paths = (
                core.scan_videos()
                if cancel_event is None
                else core.scan_videos(cancel_event=cancel_event)
            )
            scan_errors = list(getattr(core, "LAST_SCAN_ERRORS", []))
            scan_warnings = list(getattr(core, "LAST_SCAN_WARNINGS", []))
            scan_size_skips = list(getattr(core, "LAST_SCAN_SIZE_SKIPS", []))
            metadata = {}
            exiftool = Path(get_cfg("EXIFTOOL_PATH", ""))
            read_dates = bool(get_cfg("VIDEO_READ_DATES", True))
            if progress_callback is not None:
                progress_callback({
                    "phase": "exif" if read_dates else "date_disabled",
                    "completed": 0,
                    "total": len(paths),
                })
            if not read_dates:
                warning = "已关闭日期读取，将按文件名扫描并按固定数量分组。"
            elif exiftool.exists():
                try:
                    metadata = core.read_exif_metadata(
                        paths,
                        cancel_event=cancel_event,
                        progress_callback=progress_callback,
                    )
                except Exception as exc:
                    warning = f"ExifTool 读取失败，将使用后备日期：{exc}"
                    scan_errors.append(str(exc))
            elif get_cfg("VIDEO_READ_MEDIA_CREATION_DATE", True):
                warning = (
                    "未找到 ExifTool，EXIF 日期不可用；"
                    "缺少 EXIF 的视频仍会尝试读取媒体创建日期，失败后使用文件修改时间。"
                )
            elif get_cfg("VIDEO_MISSING_DATE_POLICY", "mtime") == "mtime":
                warning = "未找到 ExifTool，缺失 EXIF 的视频将使用文件修改时间。"
            else:
                warning = (
                    "未找到 ExifTool，无法读取 EXIF；"
                    "当前未启用媒体日期回退，缺失日期的视频会被标记。"
                )
            if cancel_event is not None and cancel_event.is_set():
                return cancelled_scan_result(
                    kind,
                    core_available=core is not None,
                    warning="扫描已取消",
                )
            if cancel_event is None:
                items, missing = core.build_items(
                    paths,
                    metadata,
                    progress_callback=progress_callback,
                )
            else:
                items, missing = core.build_items(
                    paths,
                    metadata,
                    progress_callback=progress_callback,
                    cancel_event=cancel_event,
                )
            state = core.UploadState()
        else:
            paths, scan_size_skips = apply_size_limits(
                basic_paths(kind, cancel_event=cancel_event), kind
            )
            missing = []
            items = []
            if not get_cfg("VIDEO_READ_DATES", True):
                items = [
                    {
                        "path": path,
                        "capture_time": None,
                        "month_key": "__all_videos__",
                    }
                    for path in paths
                ]
            else:
                for path in paths:
                    items.append({
                        "path": path,
                        "capture_time": file_mtime(path),
                        "month_key": format_date(file_mtime(path), "%Y-%m", missing="未知日期"),
                    })
    elif kind == "image":
        if core is not None:
            core.STATE_DIR = IMAGE_STATE_DIR
            paths = (
                core.scan_images()
                if cancel_event is None
                else core.scan_images(cancel_event=cancel_event)
            )
            scan_errors = list(getattr(core, "LAST_SCAN_ERRORS", []))
            scan_warnings = list(getattr(core, "LAST_SCAN_WARNINGS", []))
            scan_size_skips = list(getattr(core, "LAST_SCAN_SIZE_SKIPS", []))
            missing = []
            items = paths
            state = core.UploadState()
        else:
            paths, scan_size_skips = apply_size_limits(
                basic_paths(kind, cancel_event=cancel_event), kind
            )
            missing = []
            items = paths
    elif kind == "mixed":
        if core is not None:
            core.STATE_DIR = MIXED_STATE_DIR
            root_path = Path(get_cfg("MIXED_DIR", PROJECT_DIR))
            mixed_groups = (
                core.scan_mixed(root_path)
                if cancel_event is None
                else core.scan_mixed(root_path, cancel_event=cancel_event)
            )
            ignored_root_media = list(getattr(core, "LAST_SCAN_IGNORED_ROOT_MEDIA", []))
            scan_errors = list(getattr(core, "LAST_SCAN_ERRORS", []))
            scan_warnings = list(getattr(core, "LAST_SCAN_WARNINGS", []))
            scan_size_skips = list(getattr(core, "LAST_SCAN_SIZE_SKIPS", []))
            items = [item for group in mixed_groups for item in group.get("items", [])]
            missing = []
            state = core.UploadState()
        else:
            mixed_groups, ignored_root_media, scan_errors, scan_warnings, scan_size_skips = (
                basic_mixed_scan(
                    Path(get_cfg("MIXED_DIR", PROJECT_DIR)),
                    cancel_event=cancel_event,
                )
            )
            items = [item for group in mixed_groups for item in group.get("items", [])]
            missing = []
    else:
        raise ValueError(f"未知媒体类型：{kind}")

    if cancel_event is not None and cancel_event.is_set():
        return cancelled_scan_result(
            kind,
            core_available=core is not None,
            warning="扫描已取消",
            ignored_root_media=ignored_root_media,
            scan_errors=scan_errors,
            scan_warnings=scan_warnings,
            scan_size_skips=scan_size_skips,
        )

    store = CaptionStore(kind)
    group_size = int(
        get_cfg("ALBUM_GROUP_SIZE", 10)
        if kind == "image"
        else get_cfg("MIXED_ALBUM_GROUP_SIZE", 10)
        if kind == "mixed"
        else get_cfg("VIDEO_ALBUM_GROUP_SIZE", 10)
    )
    group_size = max(1, min(group_size, 10))
    groups = []
    source_dir = str(get_cfg(KIND_PATH_KEYS[kind], ""))

    def completed(item) -> bool:
        if state is None:
            return False
        path = item["path"] if isinstance(item, dict) else item
        return state.is_completed(path)

    if kind == "video":
        read_dates = bool(get_cfg("VIDEO_READ_DATES", True))
        group_mode = str(get_cfg("VIDEO_GROUP_MODE", "date")).strip().lower()
        if not read_dates or group_mode == "fixed":
            for index, start in enumerate(range(0, len(items), group_size), start=1):
                chunk = items[start : start + group_size]
                key = album_key("video", "all", index)
                caption = store.get(key, f"第 {index} 组")
                groups.append({
                    "album_key": key,
                    "title": f"第 {index} 组",
                    "subtitle": f"第 {index} 组（共 {len(chunk)} 个视频）",
                    "caption": caption,
                    "caption_warning": validate_caption(caption, 4096),
                    "items": chunk,
                    "completed": all(completed(item) for item in chunk),
                    "completed_count": sum(1 for item in chunk if completed(item)),
                })
        else:
            by_month = defaultdict(list)
            for item in items:
                by_month[item["month_key"]].append(item)
            for month_key, month_items in by_month.items():
                month_title = f"{month_key}（共 {len(month_items)} 个视频）"
                for index, start in enumerate(range(0, len(month_items), group_size), start=1):
                    chunk = month_items[start : start + group_size]
                    key = album_key("video", month_key, index)
                    caption = store.get(key, f"{month_key} · 第 {index} 组")
                    groups.append({
                        "album_key": key,
                        "title": f"{month_key} · 第 {index} 组",
                        "subtitle": f"{month_title} · 第 {index} 组",
                        "caption": caption,
                        "caption_warning": validate_caption(caption, 4096),
                        "items": chunk,
                        "completed": all(completed(item) for item in chunk),
                        "completed_count": sum(1 for item in chunk if completed(item)),
                    })
    elif kind == "image":
        for index, start in enumerate(range(0, len(items), group_size), start=1):
            chunk = items[start : start + group_size]
            key = album_key("image", "all", index)
            caption = store.get(key, f"第 {index} 组")
            groups.append({
                "album_key": key,
                "title": f"第 {index} 组",
                "subtitle": f"第 {index} 组（共 {len(chunk)} 张图片）",
                "caption": caption,
                "caption_warning": validate_caption(caption, 4096),
                "items": chunk,
                "completed": all(completed(item) for item in chunk),
                "completed_count": sum(1 for item in chunk if completed(item)),
            })
    else:
        for group_dict in mixed_groups:
            folder_name = group_dict["group_name"]
            group_items = group_dict["items"]
            total_items = len(group_items)
            for index, start in enumerate(range(0, total_items, group_size), start=1):
                chunk = group_items[start : start + group_size]
                key = album_key("mixed", folder_name, index)
                default_caption = folder_name if total_items <= group_size else f"{folder_name} ({index})"
                caption = store.get(key, default_caption)
                groups.append({
                    "album_key": key,
                    "title": f"{folder_name} · 第 {index} 组",
                    "subtitle": f"{folder_name}（共 {total_items} 个媒体）· 第 {index} 组",
                    "caption": caption,
                    "caption_warning": validate_caption(caption, 4096),
                    "items": chunk,
                    "completed": all(completed(item) for item in chunk),
                    "completed_count": sum(1 for item in chunk if completed(item)),
                })

    all_paths = [
        item["path"] if isinstance(item, dict) else item
        for group in groups
        for item in group["items"]
    ]
    completed_paths = [path for path in all_paths if state is not None and state.is_completed(path)]
    total_bytes = sum(item_size(path) for path in all_paths)
    pending_bytes = sum(
        item_size(item["path"] if isinstance(item, dict) else item)
        for group in groups
        for item in group["items"]
        if not completed(item)
    )

    return {
        "kind": kind,
        "items": items,
        "missing": missing,
        "groups": groups,
        "total_files": len(all_paths),
        "completed_files": len(completed_paths),
        "pending_files": len(all_paths) - len(completed_paths),
        "total_bytes": total_bytes,
        "pending_bytes": pending_bytes,
        "album_count": len(groups),
        "completed_paths": completed_paths,
        "source_dir": source_dir,
        "state_path": str(getattr(state, "file_path", "")),
        "core_available": core is not None,
        "warning": warning,
        "scan_errors": scan_errors,
        "scan_warnings": scan_warnings,
        "scan_size_skips": scan_size_skips,
        "scan_skipped_files": sum(
            1 for entry in scan_size_skips if entry.get("action") == "skip"
        ),
        "scan_compress_files": sum(
            1 for entry in scan_size_skips if entry.get("action") == "compress"
        ),
        "ignored_root_media": [str(path) for path in ignored_root_media],
        "target": target_for(kind),
    }


def load_v2_gui_integration():
    """Load the package GUI boundary from the bundled application."""
    from .integration import (
        V2IntegrationUnavailable,
        run_v2_upload,
        scan_v2,
    )
    return V2IntegrationUnavailable, run_v2_upload, scan_v2


def v2_scan_result(bundle, *, progress_callback=None, cancel_event=None) -> dict:
    """Translate V2 scan/plan models into the existing preview page contract."""
    return _translate_v2_scan_result(
        bundle,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
        cancelled_result_factory=cancelled_scan_result,
        size_resolver=item_size,
        logger=write_app_log,
        caption_store_factory=CaptionStore,
    )


def scan_result(kind: str, progress_callback=None, cancel_event=None) -> dict:
    """Run the V2 strategy scan, retaining the old preview fallback."""
    kind = require_kind(kind)
    if cfg is None:
        raise RuntimeError(_CONFIG_ERROR or "配置不可用。")
    try:
        unavailable, _run_v2_upload, scan_v2 = load_v2_gui_integration()
        from ..config import paths as _runtime_paths

        bundle = scan_v2(
            kind,
            source_root=Path(get_cfg(KIND_PATH_KEYS[kind], PROJECT_DIR)),
            target=target_for(kind),
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            config=cfg,
            runtime_paths=_runtime_paths,
        )
        return v2_scan_result(bundle, progress_callback=progress_callback, cancel_event=cancel_event)
    except unavailable:
        return legacy_scan_result(
            kind,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )


# Compatibility aliases
_basic_paths = basic_paths
_path_size = path_size
_item_size = item_size
_apply_size_limits = apply_size_limits
_basic_mixed_scan = basic_mixed_scan
_cancelled_scan_result = cancelled_scan_result
_legacy_scan_result = legacy_scan_result
_load_v2_gui_integration = load_v2_gui_integration
_v2_scan_result = v2_scan_result
_scan_result = scan_result


__all__ = [
    "BASIC_SCAN_SNAPSHOTS",
    "_apply_size_limits",
    "_basic_mixed_scan",
    "_basic_paths",
    "_cancelled_scan_result",
    "_item_size",
    "_legacy_scan_result",
    "_load_v2_gui_integration",
    "_path_size",
    "_scan_result",
    "_v2_scan_result",
    "apply_size_limits",
    "basic_mixed_scan",
    "basic_paths",
    "cancelled_scan_result",
    "item_size",
    "legacy_scan_result",
    "load_v2_gui_integration",
    "path_size",
    "scan_result",
    "v2_scan_result",
]
