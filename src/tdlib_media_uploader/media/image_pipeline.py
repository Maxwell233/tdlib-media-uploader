# -*- coding: utf-8 -*-
"""图片媒体管线：递归扫描、规范化、Album 分组及内容构建。"""

from __future__ import annotations

from collections import OrderedDict
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..core.album import CaptionStore, album_key, compose_caption
from ..core.album import with_filename_description as with_filename_description
from ..core.identity import media_file_signature
from ..core.upload_state import UploadState as SharedUploadState
from ..core.filesystem import display_path, file_snapshot, is_network_path, io_worker_count, iter_files, stable_path, validate_scan_root
from ..core.sorting import media_path_sort, relative_name as stable_relative_name
from ..core.concurrency import ordered_bounded_map
from ..core.readiness import raise_for_file_readiness, wait_for_file_ready
from .common import (
    cleanup_confirmed_staging as _cleanup_confirmed_staging,
    format_size,
    prune_staging_cache as _prune_staging_cache,
    readiness_options as _shared_readiness_options,
    readiness_record as _readiness_record,
)
from ..config import loader as cfg
from ..telegram.tdlib_common import HeadlessUI, formatted_text
from ..config.paths import IMAGE_STATE_DIR, IMAGE_COMPRESSION_CACHE_DIR
from ..upload.staging import should_stage, stage_file
from .image_probe import (
    TELEGRAM_PHOTO_MAX_ASPECT_RATIO,
    probe_image,
)
from .image_prepare import (
    prepare_image_for_telegram,
)

STATE_DIR = IMAGE_STATE_DIR
LAST_SCAN_ERRORS: list[str] = []
LAST_SCAN_WARNINGS: list[str] = []
LAST_SCAN_SIZE_SKIPS: list[dict] = []
IMAGE_UPLOAD_PATHS: dict[str, Path] = {}
STAGED_UPLOAD_PATHS: dict[str, Path] = {}
IMAGE_SCAN_SNAPSHOTS: dict[str, tuple[int, int]] = {}
COMPRESSED_IMAGE_DIR = IMAGE_COMPRESSION_CACHE_DIR
READINESS_ATTEMPTS = int(getattr(cfg, "SCAN_READINESS_ATTEMPTS", 3))

UI = HeadlessUI()


def _readiness_options(path=None) -> dict:
    return _shared_readiness_options(
        cfg,
        network=is_network_path(path or cfg.IMAGE_DIR),
        default_attempts=READINESS_ATTEMPTS,
    )


def relative_name(path: Path) -> str:
    return stable_relative_name(path, cfg.IMAGE_DIR)


def cleanup_staging_cache(*, startup: bool = False) -> None:
    _prune_staging_cache(cfg, startup=startup)


def cleanup_confirmed_staging(paths) -> None:
    _cleanup_confirmed_staging(paths, config=cfg, staged_maps=(STAGED_UPLOAD_PATHS,))


def _snapshot_for_path(path: Path):
    # Prefer the immutable snapshot captured by the scanner.  A late stat is
    # only a fallback for direct API callers that did not scan first.
    return IMAGE_SCAN_SNAPSHOTS.get(stable_path(path)) or file_snapshot(path)


def file_signature(path: Path, snapshot=None) -> str:
    snapshot = snapshot or _snapshot_for_path(path)
    if snapshot is None:
        raise OSError(f"文件暂时不可读取：{path}")
    return media_file_signature(path, root=cfg.IMAGE_DIR, snapshot=snapshot)


def scan_images(cancel_event=None) -> list[Path]:
    global LAST_SCAN_ERRORS, LAST_SCAN_WARNINGS, LAST_SCAN_SIZE_SKIPS, IMAGE_SCAN_SNAPSHOTS
    try:
        root = validate_scan_root(
            cfg.IMAGE_DIR,
            attempts=getattr(cfg, "SCAN_DISCOVERY_ATTEMPTS", 3),
            initial_delay=getattr(cfg, "SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
            max_delay=getattr(cfg, "SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
            cancel_event=cancel_event,
        )
    except TimeoutError:
        LAST_SCAN_ERRORS = []
        LAST_SCAN_WARNINGS = ["目录扫描已取消"]
        LAST_SCAN_SIZE_SKIPS = []
        IMAGE_SCAN_SNAPSHOTS = {}
        return []
    scan_result = iter_files(
        root,
        cfg.IMAGE_EXTENSIONS,
        cancel_event=cancel_event,
        discovery_attempts=getattr(cfg, "SCAN_DISCOVERY_ATTEMPTS", 3),
        discovery_initial_delay=getattr(cfg, "SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
        discovery_max_delay=getattr(cfg, "SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
    )
    images = scan_result.paths
    LAST_SCAN_ERRORS = list(scan_result.errors)
    LAST_SCAN_WARNINGS = list(scan_result.warnings)
    if scan_result.cancelled:
        LAST_SCAN_WARNINGS.append("目录扫描已取消")
    LAST_SCAN_SIZE_SKIPS = []
    IMAGE_SCAN_SNAPSHOTS = {
        key: snapshot.as_tuple()
        for key, snapshot in getattr(scan_result, "snapshots", {}).items()
    }
    accepted = []
    for path in images:
        snapshot = IMAGE_SCAN_SNAPSHOTS.get(stable_path(path)) or file_snapshot(path)
        if snapshot is None:
            LAST_SCAN_ERRORS.append(f"{path}: 文件暂时不可读取或为空")
            continue
        size, _mtime_ns = snapshot
        if size > cfg.IMAGE_MAX_BYTES:
            record = {
                "path": path,
                "size": size,
                "limit": cfg.IMAGE_MAX_BYTES,
                "category": "size",
                "action": "normalize",
                "reason": (
                    f"文件大小 {format_size(size)} 超过 Telegram Photo 上限 "
                    f"{format_size(cfg.IMAGE_MAX_BYTES)}，将在上传前规范化"
                ),
            }
            LAST_SCAN_SIZE_SKIPS.append(record)
        accepted.append(path)
        IMAGE_SCAN_SNAPSHOTS[stable_path(path)] = snapshot
    images = accepted

    mode = "mtime" if cfg.IMAGE_SORT_MODE == "mtime" else "name"
    return media_path_sort(
        images,
        root,
        mode=mode,
        mtime_key=lambda path: IMAGE_SCAN_SNAPSHOTS.get(stable_path(path), (0, 0))[1],
    )


_IMAGE_INFO_CACHE = OrderedDict()
_IMAGE_INFO_CACHE_LIMIT = 512
_IMAGE_INFO_CACHE_LOCK = threading.Lock()


def compress_image(path: Path, cancel_event=None, *, source_info=None) -> Path:
    """Create a temporary JPEG under the Telegram photo limit."""
    extreme_aspect_policy = getattr(cfg, "IMAGE_EXTREME_ASPECT_POLICY", "pad")
    transparency_background = getattr(cfg, "IMAGE_TRANSPARENCY_BACKGROUND", "#FFFFFF")
    prepared = prepare_image_for_telegram(
        path,
        cancel_event=cancel_event,
        source_info=source_info,
        aspect_policy=extreme_aspect_policy,
        background=transparency_background,
    )
    return prepared.upload_path


def cleanup_compressed_images() -> None:
    paths = set(IMAGE_UPLOAD_PATHS.values())
    IMAGE_UPLOAD_PATHS.clear()
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        if COMPRESSED_IMAGE_DIR.is_dir() and not any(COMPRESSED_IMAGE_DIR.iterdir()):
            COMPRESSED_IMAGE_DIR.rmdir()
    except OSError:
        pass


def cached_image_probe(path: Path):
    stat = path.stat()
    key = (stable_path(path), stat.st_size, stat.st_mtime_ns)
    with _IMAGE_INFO_CACHE_LOCK:
        cached = _IMAGE_INFO_CACHE.get(key)
        if cached is not None:
            _IMAGE_INFO_CACHE.move_to_end(key)
            return cached
    info = probe_image(path)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) != key[1:]:
        raise OSError(f"图片解码期间文件变化：{path}")
    with _IMAGE_INFO_CACHE_LOCK:
        _IMAGE_INFO_CACHE[key] = info
        _IMAGE_INFO_CACHE.move_to_end(key)
        while len(_IMAGE_INFO_CACHE) > _IMAGE_INFO_CACHE_LIMIT:
            _IMAGE_INFO_CACHE.popitem(last=False)
    return info


def image_info(path: Path) -> tuple[int, int]:
    info = cached_image_probe(path)
    return info.width, info.height


def preflight_images(paths, ui=None, cancel_event=None) -> list[dict]:
    """Find unreadable images without aborting the complete upload task."""

    target = ui or UI
    skipped = []
    total = len(paths)

    def worker(path):
        try:
            expected = IMAGE_SCAN_SNAPSHOTS.get(stable_path(path))
            readiness = wait_for_file_ready(
                path,
                expected_size=expected[0] if expected else None,
                expected_mtime_ns=expected[1] if expected else None,
                **_readiness_options(path),
                cancel_event=cancel_event,
            )
            raise_for_file_readiness(path, readiness)
            size = readiness.snapshot.size
            IMAGE_UPLOAD_PATHS.pop(stable_path(path), None)
            info = cached_image_probe(path)
            if info.animated:
                raise RuntimeError(f"检测到动画图片，不会自动转换为静态 Photo：{path.name}")
            extreme_aspect_policy = getattr(cfg, "IMAGE_EXTREME_ASPECT_POLICY", "pad")
            if info.needs_aspect_fix and extreme_aspect_policy == "skip":
                ratio = (
                    max(info.width / info.height, info.height / info.width)
                    if info.width and info.height
                    else 0.0
                )
                raise RuntimeError(
                    f"图片长宽比（{ratio:.2f}）超过限制 {TELEGRAM_PHOTO_MAX_ASPECT_RATIO}，且配置为跳过：{path.name}"
                )
            if info.needs_normalization or size > cfg.IMAGE_MAX_BYTES:
                return {"path": path, "oversize": True}
            return None
        except TimeoutError as exc:
            if (cancel_event is not None and cancel_event.is_set()) or "取消" in str(exc) or "cancelled" in str(exc).lower():
                raise
            readiness_record = _readiness_record(exc)
            record = {
                "path": path,
                "reason": f"{type(exc).__name__}: {exc}",
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
                raise TimeoutError("图片预检已取消")
            readiness_record = _readiness_record(exc)
            record = {
                "path": path,
                "reason": f"{type(exc).__name__}: {exc}",
                "category": (
                    "size" if "Telegram Photo 上限" in str(exc)
                    else readiness_record["category"] if readiness_record
                    else "deferred" if isinstance(exc, (OSError, TimeoutError))
                    else "unreadable"
                ),
            }
            if readiness_record:
                record.update(readiness_record)
            return record

    worker_count = io_worker_count(
        cfg.IMAGE_DIR,
        local=getattr(cfg, "IO_WORKERS_LOCAL", 4),
        network=getattr(cfg, "IO_WORKERS_NETWORK", 2),
    )
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="tdlib-preflight") as executor:
        for index, result in enumerate(
            ordered_bounded_map(executor, paths, worker, worker_count, cancel_event=cancel_event),
            1,
        ):
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("图片预检已取消")
            if result and result.get("oversize"):
                target.info(
                    f"预检发现需规范化图片，将在上传时生成兼容副本：{relative_name(result['path'])}"
                )
            elif result:
                skipped.append(result)
                target.warning(f"跳过图片：{relative_name(result['path'])}")
                target.log(f"跳过图片详情：{result['path']}\n原因：{result['reason']}")

            if getattr(cfg, "IMAGE_VERIFY_ALL_IMAGES", False):
                target.info(f"预检图片 {index}/{total} · {paths[index-1].name}")

    return skipped


def input_photo(
    path: Path,
    caption: str = "",
    *,
    expected_size=None,
    expected_mtime_ns=None,
    cancel_event=None,
) -> dict:
    if expected_size is None and expected_mtime_ns is None:
        expected = IMAGE_SCAN_SNAPSHOTS.get(stable_path(path))
        if expected is not None:
            expected_size, expected_mtime_ns = expected
    readiness = wait_for_file_ready(
        path,
        expected_size=expected_size,
        expected_mtime_ns=expected_mtime_ns,
        **_readiness_options(path),
        cancel_event=cancel_event,
    )
    raise_for_file_readiness(path, readiness)
    snapshot = readiness.snapshot.as_tuple()
    source_path = path
    staging_mode = getattr(cfg, "STAGING_MODE", None)
    if staging_mode is None or (staging_mode == "off" and getattr(cfg, "STAGING_ENABLED", False)):
        staging_mode = "always" if getattr(cfg, "STAGING_ENABLED", False) else "off"
    if should_stage(path, staging_mode):
        source_path = stage_file(
            path,
            readiness.snapshot,
            staging_dir=cfg.STAGING_DIR,
            staging_base_dir=getattr(cfg, "STAGING_BASE_DIR", cfg.STAGING_DIR),
            cancel_event=cancel_event,
        )
        STAGED_UPLOAD_PATHS[stable_path(path)] = source_path
    info = cached_image_probe(source_path)
    if info.needs_normalization or snapshot[0] > cfg.IMAGE_MAX_BYTES:
        source_path = compress_image(
            source_path,
            cancel_event,
            source_info=info,
        ) if cancel_event is not None else compress_image(source_path, source_info=info)
        IMAGE_UPLOAD_PATHS[stable_path(path)] = source_path
        if source_path.stat().st_size != snapshot[0]:
            UI.info(
                f"图片规范化完成：{relative_name(path)} · "
                f"{format_size(snapshot[0])} → {format_size(source_path.stat().st_size)}；原文件未修改"
            )
        width, height = image_info(source_path)
    else:
        width, height = info.width, info.height
    return {
        "@type": "inputMessagePhoto",
        "photo": {"@type": "inputFileLocal", "path": display_path(source_path)},
        "thumbnail": None,
        "added_sticker_file_ids": [],
        "width": width,
        "height": height,
        "caption": formatted_text(caption),
        "show_caption_above_media": False,
        "self_destruct_type": None,
        "has_spoiler": False,
    }


def build_image_contents(paths, caption: str, ui=None, cancel_event=None):
    """Build an Album while isolating images that became unreadable later."""

    target = ui or UI
    contents = []
    valid_paths = []
    skipped = []
    for path in paths:
        try:
            expected = IMAGE_SCAN_SNAPSHOTS.get(stable_path(path))
            item_caption = caption if not valid_paths else ""
            if expected is None:
                # Keep the historical two-argument call shape for embedding
                # integrations that provide their own input_photo wrapper.
                contents.append(
                    input_photo(path, item_caption, cancel_event=cancel_event)
                    if cancel_event is not None
                    else input_photo(path, item_caption)
                )
            else:
                kwargs = {
                    "expected_size": expected[0],
                    "expected_mtime_ns": expected[1],
                }
                if cancel_event is not None:
                    kwargs["cancel_event"] = cancel_event
                contents.append(input_photo(path, item_caption, **kwargs))
            valid_paths.append(path)
        except Exception as exc:
            try:
                current_size = path.stat().st_size
            except OSError:
                current_size = 0
            record = {
                "path": path,
                "reason": f"{type(exc).__name__}: {exc}",
                "category": "size" if current_size > cfg.IMAGE_MAX_BYTES else "unreadable",
            }
            readiness_record = _readiness_record(exc)
            if readiness_record:
                record.update(readiness_record)
                record["category"] = readiness_record["category"]
            elif isinstance(exc, (OSError, TimeoutError)):
                record["category"] = "deferred"
            skipped.append(record)
            target.warning(f"跳过上传前变得无法读取的图片：{relative_name(path)}")
            target.log(f"跳过图片详情：{path}\n原因：{record['reason']}")
    return contents, valid_paths, skipped


class UploadState(SharedUploadState):
    """Image-specific facade over the shared V1.9 checkpoint format."""

    def __init__(self, target=None):
        super().__init__(
            kind="image",
            source_root=cfg.IMAGE_DIR,
            target=target or getattr(cfg, "target_for", lambda _kind: {}) ("image"),
            state_dir=STATE_DIR,
            reset=getattr(cfg, "IMAGE_RESET_STATE", False),
            filename_prefix="image_upload_state",
            snapshot_provider=_snapshot_for_path,
        )


def build_album_plans(images: list[Path], state=None) -> list[dict]:
    """Build stable image Albums from the complete scan, not pending-only files."""
    store = CaptionStore("image")
    # Normal scans populate ``IMAGE_SCAN_SNAPSHOTS`` before planning.  Keep
    # direct callers (including integrations that provide a list of Paths)
    # deterministic as well by materializing each missing snapshot once at
    # the planning boundary.  ``album_key`` itself never performs a late stat;
    # it only consumes this immutable map.
    for path in images:
        key = stable_path(path)
        if key not in IMAGE_SCAN_SNAPSHOTS:
            snapshot = file_snapshot(path)
            if snapshot is not None:
                IMAGE_SCAN_SNAPSHOTS[key] = (int(snapshot[0]), int(snapshot[1]))
    plans = []
    for start in range(0, len(images), cfg.IMAGE_ALBUM_SIZE):
        album_paths = list(images[start:start + cfg.IMAGE_ALBUM_SIZE])
        number = start // cfg.IMAGE_ALBUM_SIZE + cfg.IMAGE_ALBUM_NUMBER_START
        key = album_key(
            "image",
            f"Album {number}",
            album_paths,
            root=cfg.IMAGE_DIR,
            snapshot_provider=lambda path: IMAGE_SCAN_SNAPSHOTS.get(stable_path(path)),
        )
        pending = [
            path for path in album_paths
            if state is None or not state.is_completed(path)
        ]
        record = store.get(key, str(number))
        base_label = record["base_label"] if record["base_label"] != f"Album {number}" else str(number)
        caption = {
            "base_label": base_label,
            "custom_text": record["custom_text"],
            "text": compose_caption(
                base_label if getattr(cfg, "IMAGE_ALBUM_NUMBERING", True) else "",
                record["custom_text"],
                getattr(cfg, "IMAGE_ALBUM_CAPTION_SEPARATOR", " · "),
            ),
        }
        plans.append({
            "key": key,
            "number": number,
            "items": album_paths,
            "pending_items": pending,
            "caption": caption,
        })
    return plans
