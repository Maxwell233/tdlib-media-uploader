# -*- coding: utf-8 -*-
"""图片媒体管线：递归扫描、规范化、Album 分组及内容构建。"""

from __future__ import annotations

from collections import OrderedDict
import threading
from pathlib import Path

from ..core.album import CaptionStore, album_key, compose_caption
from ..core.album import with_filename_description as with_filename_description
from ..core.identity import media_file_signature
from ..core.upload_state import UploadState as SharedUploadState
from ..core.filesystem import display_path, file_snapshot, is_network_path, iter_files, stable_path, validate_scan_root
from ..core.sorting import media_path_sort, relative_name as stable_relative_name
from ..core.compat import call_supported
from ..core.readiness import raise_for_file_readiness, wait_for_file_ready
from .common import (
    cleanup_confirmed_staging as _cleanup_confirmed_staging,
    format_size,
    media_io_results,
    prune_staging_cache as _prune_staging_cache,
    readiness_options as _shared_readiness_options,
    readiness_record as _readiness_record,
    revalidate_ready_source,
)
from ..config import loader as cfg
from .runtime import pipeline_config, pipeline_locals, pipeline_paths_lock
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
    config = pipeline_config(cfg)
    return _shared_readiness_options(
        config,
        network=is_network_path(path or config.IMAGE_DIR),
        default_attempts=int(getattr(config, "SCAN_READINESS_ATTEMPTS", READINESS_ATTEMPTS)),
    )


def relative_name(path: Path) -> str:
    return stable_relative_name(path, pipeline_config(cfg).IMAGE_DIR)


def cleanup_staging_cache(*, startup: bool = False) -> None:
    _prune_staging_cache(pipeline_config(cfg), startup=startup)


def cleanup_confirmed_staging(paths) -> None:
    _cleanup_confirmed_staging(paths, config=pipeline_config(cfg), staged_maps=(pipeline_locals(globals())["STAGED_UPLOAD_PATHS"],))


def _snapshot_for_path(path: Path):
    # Prefer the immutable snapshot captured by the scanner.  A late stat is
    # only a fallback for direct API callers that did not scan first.
    return pipeline_locals(globals())["IMAGE_SCAN_SNAPSHOTS"].get(stable_path(path)) or file_snapshot(path)


def file_signature(path: Path, snapshot=None) -> str:
    snapshot = snapshot or _snapshot_for_path(path)
    if snapshot is None:
        raise OSError(f"文件暂时不可读取：{path}")
    return media_file_signature(path, root=pipeline_config(cfg).IMAGE_DIR, snapshot=snapshot)


def scan_images(cancel_event=None) -> list[Path]:
    config = pipeline_config(cfg)
    runtime = pipeline_locals(globals())
    try:
        root = validate_scan_root(
            config.IMAGE_DIR,
            attempts=getattr(config, "SCAN_DISCOVERY_ATTEMPTS", 3),
            initial_delay=getattr(config, "SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
            max_delay=getattr(config, "SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
            cancel_event=cancel_event,
        )
    except TimeoutError:
        runtime["LAST_SCAN_ERRORS"] = []
        runtime["LAST_SCAN_WARNINGS"] = ["目录扫描已取消"]
        runtime["LAST_SCAN_SIZE_SKIPS"] = []
        runtime["IMAGE_SCAN_SNAPSHOTS"] = {}
        return []
    scan_result = iter_files(
        root,
        config.IMAGE_EXTENSIONS,
        cancel_event=cancel_event,
        discovery_attempts=getattr(config, "SCAN_DISCOVERY_ATTEMPTS", 3),
        discovery_initial_delay=getattr(config, "SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
        discovery_max_delay=getattr(config, "SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
    )
    images = scan_result.paths
    runtime["LAST_SCAN_ERRORS"] = list(scan_result.errors)
    runtime["LAST_SCAN_WARNINGS"] = list(scan_result.warnings)
    if scan_result.cancelled:
        runtime["LAST_SCAN_WARNINGS"].append("目录扫描已取消")
    runtime["LAST_SCAN_SIZE_SKIPS"] = []
    runtime["IMAGE_SCAN_SNAPSHOTS"] = {
        key: snapshot.as_tuple()
        for key, snapshot in getattr(scan_result, "snapshots", {}).items()
    }
    accepted = []
    for path in images:
        snapshot = runtime["IMAGE_SCAN_SNAPSHOTS"].get(stable_path(path)) or file_snapshot(path)
        if snapshot is None:
            runtime["LAST_SCAN_ERRORS"].append(f"{path}: 文件暂时不可读取或为空")
            continue
        size, _mtime_ns = snapshot
        if size > config.IMAGE_MAX_BYTES:
            record = {
                "path": path,
                "size": size,
                "limit": config.IMAGE_MAX_BYTES,
                "category": "size",
                "action": "normalize",
                "reason": (
                    f"文件大小 {format_size(size)} 超过 Telegram Photo 上限 "
                    f"{format_size(config.IMAGE_MAX_BYTES)}，将在上传前规范化"
                ),
            }
            runtime["LAST_SCAN_SIZE_SKIPS"].append(record)
        accepted.append(path)
        runtime["IMAGE_SCAN_SNAPSHOTS"][stable_path(path)] = snapshot
    images = accepted

    mode = "mtime" if config.IMAGE_SORT_MODE == "mtime" else "name"
    return media_path_sort(
        images,
        root,
        mode=mode,
        mtime_key=lambda path: runtime["IMAGE_SCAN_SNAPSHOTS"].get(stable_path(path), (0, 0))[1],
    )


_IMAGE_INFO_CACHE = OrderedDict()
_IMAGE_INFO_CACHE_LIMIT = 512
_IMAGE_INFO_CACHE_LOCK = threading.Lock()


def compress_image(path: Path, cancel_event=None, *, source_info=None) -> Path:
    """Create a temporary JPEG under the Telegram photo limit."""
    config = pipeline_config(cfg)
    extreme_aspect_policy = getattr(config, "IMAGE_EXTREME_ASPECT_POLICY", "pad")
    transparency_background = getattr(config, "IMAGE_TRANSPARENCY_BACKGROUND", "#FFFFFF")
    prepared = prepare_image_for_telegram(
        path,
        cancel_event=cancel_event,
        source_info=source_info,
        aspect_policy=extreme_aspect_policy,
        background=transparency_background,
    )
    return prepared.upload_path


def cleanup_compressed_images() -> None:
    tracked_paths = pipeline_locals(globals())["IMAGE_UPLOAD_PATHS"]
    with pipeline_paths_lock:
        paths = set(tracked_paths.values())
        tracked_paths.clear()
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


def preflight_images(paths, ui=None, cancel_event=None, *, executor=None) -> list[dict]:
    """Find unreadable images without aborting the complete upload task."""
    config = pipeline_config(cfg)

    target = ui or UI
    skipped = []
    total = len(paths)

    def worker(path):
        try:
            expected = pipeline_locals(globals())["IMAGE_SCAN_SNAPSHOTS"].get(stable_path(path))
            readiness = wait_for_file_ready(
                path,
                expected_size=expected[0] if expected else None,
                expected_mtime_ns=expected[1] if expected else None,
                **_readiness_options(path),
                cancel_event=cancel_event,
            )
            raise_for_file_readiness(path, readiness)
            size = readiness.snapshot.size
            info = cached_image_probe(path)
            if info.animated:
                raise RuntimeError(f"检测到动画图片，不会自动转换为静态 Photo：{path.name}")
            extreme_aspect_policy = getattr(config, "IMAGE_EXTREME_ASPECT_POLICY", "pad")
            if info.needs_aspect_fix and extreme_aspect_policy == "skip":
                ratio = (
                    max(info.width / info.height, info.height / info.width)
                    if info.width and info.height
                    else 0.0
                )
                raise RuntimeError(
                    f"图片长宽比（{ratio:.2f}）超过限制 {TELEGRAM_PHOTO_MAX_ASPECT_RATIO}，且配置为跳过：{path.name}"
                )
            if info.needs_normalization or size > config.IMAGE_MAX_BYTES:
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

    for index, result in enumerate(
        media_io_results(paths, worker, config=config, root=config.IMAGE_DIR,
                         cancel_event=cancel_event, executor=executor),
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

        if getattr(config, "IMAGE_VERIFY_ALL_IMAGES", False):
            target.info(f"预检图片 {index}/{total} · {paths[index-1].name}")

    return skipped


def input_photo(
    path: Path,
    caption: str = "",
    *,
    expected_size=None,
    expected_mtime_ns=None,
    cancel_event=None,
    ui=None,
    staged_paths=None,
    preflight_verified=False,
    use_scan_snapshot=True,
) -> dict:
    config = pipeline_config(cfg)
    runtime = pipeline_locals(globals())
    key = stable_path(path)
    if use_scan_snapshot and expected_size is None and expected_mtime_ns is None:
        expected = runtime["IMAGE_SCAN_SNAPSHOTS"].get(key)
        if expected is not None:
            expected_size, expected_mtime_ns = expected
    if preflight_verified and expected_size is not None and expected_mtime_ns is not None:
        snapshot = revalidate_ready_source(
            path, expected_size=expected_size, expected_mtime_ns=expected_mtime_ns,
            cancel_event=cancel_event,
        )
    else:
        readiness = wait_for_file_ready(
            path, expected_size=expected_size, expected_mtime_ns=expected_mtime_ns,
            **_readiness_options(path), cancel_event=cancel_event,
        )
        raise_for_file_readiness(path, readiness)
        snapshot = readiness.snapshot
    source_path = path if staged_paths is None else staged_paths.get(key, path)
    if staged_paths is None:
        staging_mode = getattr(config, "STAGING_MODE", "off")
        if not staging_mode or (staging_mode == "off" and getattr(config, "STAGING_ENABLED", False)):
            staging_mode = "always" if getattr(config, "STAGING_ENABLED", False) else "off"
        if should_stage(path, staging_mode):
            source_path = stage_file(
                path, snapshot, staging_dir=config.STAGING_DIR,
                staging_base_dir=getattr(config, "STAGING_BASE_DIR", config.STAGING_DIR),
                cancel_event=cancel_event,
            )
            with pipeline_paths_lock:
                runtime["STAGED_UPLOAD_PATHS"][key] = source_path
    info = cached_image_probe(source_path)
    if info.needs_normalization or snapshot.size > config.IMAGE_MAX_BYTES:
        source_path = compress_image(
            source_path,
            cancel_event,
            source_info=info,
        ) if cancel_event is not None else compress_image(source_path, source_info=info)
        with pipeline_paths_lock:
            runtime["IMAGE_UPLOAD_PATHS"][key] = source_path
        upload_size = source_path.stat().st_size
        if upload_size != snapshot.size:
            (ui or UI).info(
                f"图片规范化完成：{relative_name(path)} · "
                f"{format_size(snapshot.size)} → {format_size(upload_size)}；原文件未修改"
            )
        width, height = image_info(source_path)
    else:
        width, height = info.width, info.height
    return {
        "@type": "inputMessagePhoto",
        "photo": {
            "@type": "inputPhoto",
            "photo": {"@type": "inputFileLocal", "path": display_path(source_path)},
            "thumbnail": None,
            "video": None,
            "added_sticker_file_ids": [],
            "width": width,
            "height": height,
        },
        "caption": formatted_text(caption),
        "show_caption_above_media": False,
        "self_destruct_type": None,
        "has_spoiler": False,
    }


def build_image_contents(
    paths, caption: str, ui=None, cancel_event=None, *, staged_paths=None,
    preflight_verified=False, executor=None,
):
    """Build an Album while isolating images that became unreadable later."""

    target = ui or UI
    config = pipeline_config(cfg)
    paths = tuple(paths)

    def build(path):
        try:
            expected = pipeline_locals(globals())["IMAGE_SCAN_SNAPSHOTS"].get(stable_path(path))
            kwargs = {}
            if expected is not None:
                kwargs.update(expected_size=expected[0], expected_mtime_ns=expected[1])
            if cancel_event is not None:
                kwargs["cancel_event"] = cancel_event
            if ui is not None:
                kwargs["ui"] = ui
            if staged_paths is not None:
                kwargs["staged_paths"] = staged_paths
            if preflight_verified:
                kwargs["preflight_verified"] = True
            return path, call_supported(input_photo, (path, ""), **kwargs), None
        except Exception as exc:
            try:
                current_size = path.stat().st_size
            except OSError:
                current_size = 0
            record = {
                "path": path,
                "reason": f"{type(exc).__name__}: {exc}",
                "category": "size" if current_size > config.IMAGE_MAX_BYTES else "unreadable",
            }
            readiness_record = _readiness_record(exc)
            if readiness_record:
                record.update(readiness_record)
                record["category"] = readiness_record["category"]
            elif isinstance(exc, (OSError, TimeoutError)):
                record["category"] = "deferred"
            return path, None, record

    contents, valid_paths, skipped = [], [], []
    for path, content, record in media_io_results(
        paths, build, config=config, root=config.IMAGE_DIR, cancel_event=cancel_event, executor=executor,
    ):
        if record:
            skipped.append(record)
            target.warning(f"跳过上传前变得无法读取的图片：{relative_name(path)}")
            target.log(f"跳过图片详情：{path}\n原因：{record['reason']}")
        else:
            if not contents:
                content = {**content, "caption": formatted_text(caption)}
            contents.append(content)
            valid_paths.append(path)
    return contents, valid_paths, skipped


class UploadState(SharedUploadState):
    """Image-specific facade over the shared V1.9 checkpoint format."""

    def __init__(self, target=None, *, state_dir: Path | None = None, create=True):
        super().__init__(
            kind="image",
            create=create,
            source_root=pipeline_config(cfg).IMAGE_DIR,
            target=target or getattr(pipeline_config(cfg), "target_for", lambda _kind: {}) ("image"),
            state_dir=STATE_DIR if state_dir is None else state_dir,
            filename_prefix="image_upload_state",
            snapshot_provider=_snapshot_for_path,
        )


def build_album_plans(images: list[Path], state=None) -> list[dict]:
    """Build stable image Albums from the complete scan, not pending-only files."""
    config = pipeline_config(cfg)
    runtime = pipeline_locals(globals())
    store = CaptionStore("image")
    # Normal scans populate ``IMAGE_SCAN_SNAPSHOTS`` before planning.  Keep
    # direct callers (including integrations that provide a list of Paths)
    # deterministic as well by materializing each missing snapshot once at
    # the planning boundary.  ``album_key`` itself never performs a late stat;
    # it only consumes this immutable map.
    for path in images:
        key = stable_path(path)
        if key not in runtime["IMAGE_SCAN_SNAPSHOTS"]:
            snapshot = file_snapshot(path)
            if snapshot is not None:
                runtime["IMAGE_SCAN_SNAPSHOTS"][key] = (int(snapshot[0]), int(snapshot[1]))
    plans = []
    for start in range(0, len(images), config.IMAGE_ALBUM_SIZE):
        album_paths = list(images[start:start + config.IMAGE_ALBUM_SIZE])
        number = start // config.IMAGE_ALBUM_SIZE + config.IMAGE_ALBUM_NUMBER_START
        key = album_key(
            "image",
            f"Album {number}",
            album_paths,
            root=config.IMAGE_DIR,
            snapshot_provider=lambda path: runtime["IMAGE_SCAN_SNAPSHOTS"].get(stable_path(path)),
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
                base_label if getattr(config, "IMAGE_ALBUM_NUMBERING", True) else "",
                record["custom_text"],
                getattr(config, "IMAGE_ALBUM_CAPTION_SEPARATOR", " · "),
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
