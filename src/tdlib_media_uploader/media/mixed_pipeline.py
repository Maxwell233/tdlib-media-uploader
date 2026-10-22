# -*- coding: utf-8 -*-
"""Mixed photo/video Album uploader.

Each first-level directory below ``MIXED_DIR`` is an independent group. Files
inside that directory (including nested directories) keep one deterministic
order and are split into Telegram Albums of at most ten items. The module
reuses the photo/video input builders while the caller owns transport and
checkpoint finalization. Mixed Albums retain per-item error isolation.
"""

from __future__ import annotations

import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..core.album import CaptionStore, album_key, compose_caption
from ..core.album import with_filename_description as with_filename_description
from ..core.identity import media_file_signature
from ..core.upload_state import UploadState as SharedUploadState
from ..core.filesystem import file_snapshot, io_worker_count, iter_files, is_network_path, file_mtime, is_link_or_junction, retry_fs_operation, stable_path, iter_directory_entries_with_retry, validate_scan_root
from ..core.readiness import CHANGED, DEFERRED, FileReadinessError, raise_for_file_readiness, wait_for_file_ready
from ..core.sorting import media_path_sort, relative_name as stable_relative_name
from ..core.concurrency import ordered_bounded_map
from .common import (
    cleanup_confirmed_staging as _cleanup_confirmed_staging,
    format_size as _format_size,
    prune_staging_cache as _prune_staging_cache,
    readiness_options as _shared_readiness_options,
    readiness_record as _readiness_record,
)
from ..config import loader as cfg
from ..telegram.tdlib_common import HeadlessUI
from ..telegram.tdlib_common import formatted_text as formatted_text
from ..config.paths import MIXED_STATE_DIR

from . import image_pipeline as image_core
from . import video_pipeline as video_core


STATE_DIR = MIXED_STATE_DIR
LAST_SCAN_ERRORS: list[str] = []
LAST_SCAN_WARNINGS: list[str] = []
LAST_SCAN_SIZE_SKIPS: list[dict] = []
LAST_SCAN_IGNORED_ROOT_MEDIA: list[Path] = []
UI = HeadlessUI()
MEDIA_DATE_MAX_WORKERS = 4
READINESS_ATTEMPTS = 3


def _readiness_options(path=None) -> dict:
    return _shared_readiness_options(
        cfg,
        network=is_network_path(path or cfg.MIXED_DIR),
        default_attempts=READINESS_ATTEMPTS,
    )


def _validate_extensions() -> None:
    overlap = set(getattr(cfg, "MIXED_IMAGE_EXTENSIONS", set())) & set(
        getattr(cfg, "MIXED_VIDEO_EXTENSIONS", set())
    )
    if overlap:
        values = ", ".join(sorted(overlap))
        raise RuntimeError(
            f"混合上传的图片和视频扩展名冲突：{values}。请在 [image]/[video] 中移除重复扩展名。"
        )


def format_size(value: float | int) -> str:
    return _format_size(value or 0)


def relative_name(path: Path, root=None) -> str:
    return stable_relative_name(path, root or cfg.MIXED_DIR)


def cleanup_staging_cache(*, startup: bool = False) -> None:
    _prune_staging_cache(cfg, startup=startup)


def cleanup_confirmed_staging(items) -> None:
    paths = (item.get("path") if isinstance(item, dict) else item for item in items or ())
    _cleanup_confirmed_staging(
        (path for path in paths if path is not None),
        config=cfg,
        staged_maps=(
            getattr(image_core, "STAGED_UPLOAD_PATHS", {}),
            getattr(video_core, "STAGED_UPLOAD_PATHS", {}),
        ),
    )


def file_signature(path: Path, snapshot=None) -> str:
    if snapshot is None:
        snapshot = file_snapshot(path)
    if snapshot is None:
        raise OSError(f"文件暂时不可读取：{path}")
    return media_file_signature(path, root=cfg.MIXED_DIR, snapshot=snapshot)


def _kind_for(path: Path) -> str | None:
    _validate_extensions()
    suffix = path.suffix.lower()
    from .video_probe import has_video_container_signature
    if has_video_container_signature(path) or suffix in cfg.MIXED_VIDEO_EXTENSIONS:
        return "video"
    if suffix in cfg.MIXED_IMAGE_EXTENSIONS:
        return "image"
    return None


def _item_for_path(path: Path, group_name: str, snapshot=None) -> dict | None:
    media_kind = _kind_for(path)
    if media_kind is None:
        return None
    snapshot = snapshot or file_snapshot(path)
    if snapshot is None:
        LAST_SCAN_ERRORS.append(f"{path}: 文件暂时不可读取或为空")
        return None
    size, mtime_ns = snapshot
    limit = cfg.VIDEO_MAX_BYTES if media_kind == "video" else cfg.IMAGE_MAX_BYTES
    if size > limit:
        limit_label = (
            video_core.video_limit_text(limit)
            if media_kind == "video"
            else format_size(limit)
        )
        LAST_SCAN_SIZE_SKIPS.append({
            "path": path,
            "size": size,
            "limit": limit,
            "category": "size",
            "action": (
                "normalize" if media_kind == "image"
                else "preflight"
            ),
            "media_kind": media_kind,
            "reason": (
                f"源文件大小 {format_size(size)} 超过 Telegram "
                f"{'视频' if media_kind == 'video' else 'Photo'} 上限 {limit_label}；"
                f"上传前将按{'转封装后的输出' if media_kind == 'video' else '规范化后的输出'}大小复核"
            ),
        })
    return {
        "path": path,
        "media_kind": media_kind,
        "group_name": group_name,
        "scan_size": size,
        "scan_mtime_ns": mtime_ns,
        "requires_premium": (
            media_kind == "video"
            and size > getattr(cfg, "VIDEO_STANDARD_MAX_BYTES", 4000 * 524_288)
        ),
    }


def _group_items(group_path: Path, group_name: str, cancel_event=None) -> list[dict]:
    scan_result = iter_files(
        group_path,
        None,
        cancel_event=cancel_event,
        discovery_attempts=getattr(cfg, "SCAN_DISCOVERY_ATTEMPTS", 3),
        discovery_initial_delay=getattr(cfg, "SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
        discovery_max_delay=getattr(cfg, "SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
    )
    LAST_SCAN_ERRORS.extend(scan_result.errors)
    LAST_SCAN_WARNINGS.extend(scan_result.warnings)
    if scan_result.cancelled:
        LAST_SCAN_WARNINGS.append("目录扫描已取消")
        return []
    candidates = scan_result.paths
    snapshots = getattr(scan_result, "snapshots", {})
    items = []
    for path in candidates:
        if cancel_event is not None and cancel_event.is_set():
            break
        snapshot = snapshots.get(stable_path(path))
        item = _item_for_path(path, group_name, snapshot.as_tuple() if snapshot else None)
        if item is not None:
            items.append(item)
    if cancel_event is not None and cancel_event.is_set():
        LAST_SCAN_WARNINGS.append("目录扫描已取消")
        return []
    sort_mode = str(getattr(cfg, "MIXED_SORT_MODE", "name")).strip().lower()
    def item_mtime(item):
        snapshot_mtime = item.get("scan_mtime_ns")
        return snapshot_mtime if snapshot_mtime is not None else file_mtime(item["path"])
    return media_path_sort(
        items,
        group_path,
        mode=sort_mode,
        path_key=lambda item: item["path"],
        mtime_key=item_mtime,
    )


def scan_mixed_groups(cancel_event=None) -> list[dict]:
    """Scan each first-level directory as a separate mixed-media group."""
    global LAST_SCAN_ERRORS, LAST_SCAN_WARNINGS, LAST_SCAN_SIZE_SKIPS, LAST_SCAN_IGNORED_ROOT_MEDIA
    root = Path(cfg.MIXED_DIR)
    _validate_extensions()
    LAST_SCAN_ERRORS = []
    LAST_SCAN_WARNINGS = []
    LAST_SCAN_SIZE_SKIPS = []
    LAST_SCAN_IGNORED_ROOT_MEDIA = []
    try:
        root = validate_scan_root(
            root,
            attempts=getattr(cfg, "SCAN_DISCOVERY_ATTEMPTS", 3),
            initial_delay=getattr(cfg, "SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
            max_delay=getattr(cfg, "SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
            cancel_event=cancel_event,
        )
    except TimeoutError:
        LAST_SCAN_WARNINGS.append("目录扫描已取消")
        return []
    groups = []
    directories = []
    root_files = []
    try:
        # Iterate the root directly so a large mixed directory can honour
        # cancellation and recover when an SMB iterator drops mid-enumeration.
        for entry in iter_directory_entries_with_retry(
            root,
            attempts=getattr(cfg, "SCAN_DISCOVERY_ATTEMPTS", 3),
            initial_delay=getattr(cfg, "SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
            max_delay=getattr(cfg, "SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
            cancel_event=cancel_event,
        ):
            if cancel_event is not None and cancel_event.is_set():
                LAST_SCAN_WARNINGS.append("目录扫描已取消")
                break
            try:
                info = retry_fs_operation(
                    lambda entry=entry: entry.stat(follow_symlinks=False),
                    attempts=getattr(cfg, "SCAN_DISCOVERY_ATTEMPTS", 3),
                    initial_delay=getattr(cfg, "SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
                    max_delay=getattr(cfg, "SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
                    cancel_event=cancel_event,
                )
                if stat.S_ISDIR(info.st_mode):
                    if is_link_or_junction(entry):
                        LAST_SCAN_WARNINGS.append(f"跳过符号链接或 junction：{entry.path}")
                        continue
                    directories.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode) and _kind_for(Path(entry.path)):
                    root_files.append(Path(entry.path))
            except OSError as exc:
                LAST_SCAN_ERRORS.append(f"{entry.path}: {exc}")
    except TimeoutError:
        LAST_SCAN_WARNINGS.append("目录扫描已取消")
    except OSError as exc:
        # Keep groups discovered before an iterator failure.  A reconnecting
        # network share must not discard the portion we already enumerated or
        # abort the entire mixed scan.
        LAST_SCAN_ERRORS.append(f"{root}: {exc}")
    directories = media_path_sort(directories, root, mode="name")
    for group_path in directories:
        if cancel_event is not None and cancel_event.is_set():
            break
        items = _group_items(group_path, group_path.name, cancel_event=cancel_event)
        if items:
            groups.append({
                "group_name": group_path.name,
                "group_path": group_path,
                "items": items,
            })
    # A mixed root is a container, not an upload group.  Files placed directly
    # in it are intentionally ignored so they cannot silently change Album
    # membership or be mixed with a first-level folder.
    if root_files:
        LAST_SCAN_IGNORED_ROOT_MEDIA = list(root_files)
    return groups


class UploadState(SharedUploadState):
    """Mixed-specific facade over the shared V1.9 checkpoint format."""

    def __init__(self, target=None):
        super().__init__(
            kind="mixed",
            source_root=cfg.MIXED_DIR,
            target=target or getattr(cfg, "target_for", lambda _kind: {}) ("mixed"),
            state_dir=STATE_DIR,
            filename_prefix="mixed_upload_state",
        )


def build_album_plans(groups, state=None) -> list[dict]:
    plans = []
    store = CaptionStore("mixed")
    for group in groups:
        group_name = str(group["group_name"])
        items = list(group.get("items", []))
        for offset in range(0, len(items), cfg.MIXED_ALBUM_SIZE):
            album_items = items[offset:offset + cfg.MIXED_ALBUM_SIZE]
            number = offset // cfg.MIXED_ALBUM_SIZE + 1
            key = album_key("mixed", f"{group_name}:{number}", album_items, root=cfg.MIXED_DIR)
            pending = [item for item in album_items if state is None or not state.is_completed(item)]
            record = store.get(key, group_name)
            base_label = record["base_label"] if cfg.MIXED_CAPTION_INCLUDE_GROUP_TITLE else ""
            if cfg.MIXED_CAPTION_INCLUDE_GROUP_TITLE and not base_label:
                base_label = group_name
            plans.append({
                "key": key,
                "group_name": group_name,
                "group_path": group.get("group_path"),
                "number": number,
                "items": album_items,
                "pending_items": pending,
                "caption": {
                    "base_label": base_label,
                    "custom_text": record["custom_text"],
                    "text": compose_caption(base_label, record["custom_text"], cfg.MIXED_ALBUM_CAPTION_SEPARATOR),
                },
            })
    return plans


def _deferred(exc: Exception) -> bool:
    if isinstance(exc, FileReadinessError):
        return exc.readiness.status in {DEFERRED, CHANGED}
    return isinstance(exc, (OSError, TimeoutError))


def preflight_mixed(items, ui=None, cancel_event=None) -> list[dict]:
    target = ui or UI
    skipped = []

    def worker(item):
        path = item["path"]
        try:
            readiness = wait_for_file_ready(
                path,
                expected_size=item.get("scan_size"),
                expected_mtime_ns=item.get("scan_mtime_ns"),
                **_readiness_options(path),
                cancel_event=cancel_event,
            )
            raise_for_file_readiness(path, readiness)
            snapshot = readiness.snapshot.as_tuple()
            if item.get("media_kind") == "video":
                v_info = (
                    video_core.video_info(path)
                    if cancel_event is None
                    else video_core.video_info(path, cancel_event=cancel_event)
                )
                if snapshot[0] > cfg.VIDEO_MAX_BYTES and not video_core.can_remux_video(path, info=v_info):
                    raise RuntimeError(
                        f"文件大小 {format_size(snapshot[0])} 超过 Telegram 视频上限 "
                        f"{video_core.video_limit_text(cfg.VIDEO_MAX_BYTES)}"
                    )
                if getattr(v_info, "compatibility", "native") not in {"native", "legacy"}:
                    policy = getattr(cfg, "VIDEO_COMPATIBILITY_POLICY", "remux")
                    if getattr(v_info, "compatibility", "") == "remux":
                        if policy == "original":
                            raise RuntimeError(
                                f"视频格式为 {v_info.container.upper()}，当前策略配置为 original（仅允许原生格式），已跳过：{path.name}"
                            )
                    else:
                        from .video_probe import VideoMediaInfo, format_unsupported_reason
                        raise RuntimeError(
                            format_unsupported_reason(path, v_info)
                            if isinstance(v_info, VideoMediaInfo)
                            else f"当前编码不在直接 Telegram Video 支持范围内，已跳过：{path.name}"
                        )
            else:
                img_info = image_core.cached_image_probe(path)
                if img_info.animated:
                    raise RuntimeError(f"检测到动画图片，不会自动转换为静态 Photo：{path.name}")
                extreme_aspect_policy = getattr(cfg, "IMAGE_EXTREME_ASPECT_POLICY", "pad")
                if img_info.needs_aspect_fix and extreme_aspect_policy == "skip":
                    from .image_probe import TELEGRAM_PHOTO_MAX_ASPECT_RATIO
                    ratio = (
                        max(img_info.width / img_info.height, img_info.height / img_info.width)
                        if img_info.width and img_info.height
                        else 0.0
                    )
                    raise RuntimeError(
                        f"图片长宽比（{ratio:.2f}）超过限制 {TELEGRAM_PHOTO_MAX_ASPECT_RATIO}，且配置为跳过：{path.name}"
                    )
            return None
        except TimeoutError as exc:
            if (cancel_event is not None and cancel_event.is_set()) or "取消" in str(exc) or "cancelled" in str(exc).lower():
                raise
            readiness_record = _readiness_record(exc)
            record = {
                "item": item,
                "path": path,
                "reason": f"{type(exc).__name__}: {exc}" if "\n" not in str(exc) else str(exc),
                "category": (
                    readiness_record["category"] if readiness_record
                    else "deferred"
                ),
            }
            if readiness_record:
                record.update(readiness_record)
            return record
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("混合预检已取消")
            text = str(exc)
            category = (
                "size" if "超过 Telegram" in text
                else "unsupported" if "不在直接 Telegram Video 支持范围内" in text or "已跳过" in text
                else "deferred" if _deferred(exc)
                else "unreadable"
            )
            readiness_record = _readiness_record(exc)
            record = {
                "item": item,
                "path": path,
                "reason": f"{type(exc).__name__}: {exc}" if "\n" not in text else text,
                "category": readiness_record["category"] if readiness_record else category,
            }
            if readiness_record:
                record.update(readiness_record)
            return record

    worker_count = io_worker_count(
        cfg.MIXED_DIR,
        local=getattr(cfg, "IO_WORKERS_LOCAL", MEDIA_DATE_MAX_WORKERS),
        network=getattr(cfg, "IO_WORKERS_NETWORK", 2),
    )
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="tdlib-mixed-preflight") as executor:
        for index, result in enumerate(
            ordered_bounded_map(executor, items, worker, worker_count),
            1,
        ):
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("混合预检已取消")
            if result:
                skipped.append(result)
                target.warning(f"跳过无法读取的混合媒体：{relative_name(result['path'])}")
                target.log(f"跳过混合媒体详情：{result['path']}\n原因：{result['reason']}")
            if getattr(cfg, "MIXED_VERIFY_MEDIA", False):
                target.info(f"预检混合媒体 {index}/{len(items)} · {Path(items[index - 1]['path']).name}")
    return skipped


def _mixed_input_video(
    item,
    caption,
    cancel_event=None,
    group_key: str | None = None,
    max_bytes: int | None = None,
    is_premium: bool | None = None,
):
    # Keep one authoritative TDLib video payload implementation.  Mixed
    # orchestration still owns grouping/captions/state, while the shared
    # builder owns readiness, staging, metadata and thumbnail shape.
    kwargs = {
        "generate_thumbnail": getattr(cfg, "MIXED_GENERATE_THUMBNAIL", True),
        "thumbnail_timestamp_seconds": getattr(
            cfg,
            "MIXED_THUMBNAIL_TIMESTAMP_SECONDS",
            getattr(cfg, "VIDEO_THUMBNAIL_TIMESTAMP_SECONDS", 1.0),
        ),
    }
    if group_key is not None:
        kwargs["group_key"] = group_key
    if cancel_event is not None:
        kwargs["cancel_event"] = cancel_event
    if max_bytes is not None:
        kwargs["max_bytes"] = max_bytes
    if is_premium is not None:
        kwargs["is_premium"] = is_premium
    return video_core.input_video(item, caption, **kwargs)


def build_mixed_contents(
    items,
    caption: str,
    ui=None,
    cancel_event=None,
    group_key: str | None = None,
    max_bytes: int | None = None,
    is_premium: bool | None = None,
):
    target = ui or UI
    image_core.UI = target
    video_core.UI = target
    contents, valid, skipped = [], [], []
    for item in items:
        media_kind = item.get("media_kind")
        if media_kind not in {"video", "image"}:
            raise ValueError(f"未知混合媒体类型：{media_kind!r}")
        try:
            if media_kind == "video":
                item_caption = caption if not valid else ""
                video_kwargs = {}
                if group_key is not None:
                    video_kwargs["group_key"] = group_key
                if cancel_event is not None:
                    video_kwargs["cancel_event"] = cancel_event
                if max_bytes is not None:
                    video_kwargs["max_bytes"] = max_bytes
                if is_premium is not None:
                    video_kwargs["is_premium"] = is_premium
                content = _mixed_input_video(item, item_caption, **video_kwargs)
            elif media_kind == "image":
                expected_size = item.get("scan_size")
                expected_mtime_ns = item.get("scan_mtime_ns")
                if expected_size is not None or expected_mtime_ns is not None:
                    if expected_size is None or expected_mtime_ns is None:
                        # A partial snapshot cannot prove that the source is
                        # unchanged; let input_photo perform its existence
                        # check without inventing a second value.
                        expected_size = expected_mtime_ns = None
                    else:
                        image_core.IMAGE_SCAN_SNAPSHOTS[stable_path(item["path"])] = (
                            int(expected_size),
                            int(expected_mtime_ns),
                        )
                else:
                    # Do not let a snapshot left by a previous mixed scan
                    # impose a stale JIT identity on a legacy caller that
                    # supplies only a path.
                    image_core.IMAGE_SCAN_SNAPSHOTS.pop(
                        stable_path(item["path"]),
                        None,
                    )
                item_caption = caption if not valid else ""
                if cancel_event is None:
                    content = image_core.input_photo(item["path"], item_caption)
                else:
                    content = image_core.input_photo(
                        item["path"], item_caption, cancel_event=cancel_event
                    )
            contents.append(content)
            valid.append(item)
        except Exception as exc:
            path = item["path"]
            record = {
                "item": item,
                "path": path,
                "reason": f"{type(exc).__name__}: {exc}",
                "category": "deferred" if _deferred(exc) else "unreadable",
            }
            readiness_record = _readiness_record(exc)
            if readiness_record:
                record.update(readiness_record)
                record["category"] = readiness_record["category"]
            skipped.append(record)
            target.warning(f"跳过上传前无法读取的混合媒体：{relative_name(path)}")
            target.log(f"跳过混合媒体详情：{path}\n原因：{record['reason']}")
    return contents, valid, skipped
