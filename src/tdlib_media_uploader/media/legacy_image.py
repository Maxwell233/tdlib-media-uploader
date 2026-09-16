# -*- coding: utf-8 -*-
"""TDLib 图片批量 Album 上传器：递归扫描、分组编号与可编辑 Caption。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from ..core.album import CaptionStore, album_key, compose_caption, with_filename_description
from ..core.identity import media_file_signature
from ..core.upload_state import UploadState as SharedUploadState
from ..core.filesystem_legacy import (
    display_path,
    FileReadinessError,
    file_snapshot,
    is_network_path,
    io_worker_count,
    iter_files,
    media_path_sort,
    ordered_bounded_map,
    readiness_category,
    relative_name as stable_relative_name,
    raise_for_file_readiness,
    run_cancellable_process,
    stable_path,
    validate_scan_root,
    wait_for_file_ready,
)
from ..config import loader as cfg
from ..telegram.tdlib_common import HeadlessUI, TDJsonClient, formatted_text, verify_tdjson_version
from ..config.paths import APP_DATA_DIR, RESOURCE_DIR, IMAGE_STATE_DIR, IMAGE_COMPRESSION_CACHE_DIR
from ..upload.staging import cleanup_staging, remove_staged_file, should_stage, stage_file

PROJECT_DIR = RESOURCE_DIR
STATE_DIR = IMAGE_STATE_DIR
LAST_SCAN_ERRORS: list[str] = []
LAST_SCAN_WARNINGS: list[str] = []
LAST_SCAN_SIZE_SKIPS: list[dict] = []
DEFERRED_STATUS = "DEFERRED"
IMAGE_UPLOAD_PATHS: dict[str, Path] = {}
STAGED_UPLOAD_PATHS: dict[str, Path] = {}
IMAGE_SCAN_SNAPSHOTS: dict[str, tuple[int, int]] = {}
COMPRESSED_IMAGE_DIR = IMAGE_COMPRESSION_CACHE_DIR
FFMPEG_COMPRESS_TIMEOUT_SECONDS = float(
    getattr(cfg, "FFMPEG_COMPRESSION_TIMEOUT_SECONDS", 45)
)
READINESS_ATTEMPTS = int(getattr(cfg, "SCAN_READINESS_ATTEMPTS", 3))

UI = HeadlessUI()


def _readiness_options(path=None) -> dict:
    """Return the shared, bounded source-file readiness configuration."""
    network = is_network_path(path or cfg.IMAGE_DIR)
    legacy_interval = getattr(cfg, "SCAN_STABILITY_INTERVAL_SECONDS", None)
    legacy_checks = getattr(cfg, "SCAN_STABILITY_CHECKS", None)
    interval_default = 0.5 if network else 0.05
    checks_default = 3 if network else 2
    return {
        "attempts": int(getattr(cfg, "SCAN_READINESS_ATTEMPTS", READINESS_ATTEMPTS)),
        "stable_interval": float(getattr(
            cfg,
            "SCAN_STABILITY_INTERVAL_NETWORK_SECONDS" if network else "SCAN_STABILITY_INTERVAL_LOCAL_SECONDS",
            legacy_interval if legacy_interval is not None else interval_default,
        )),
        "stable_checks": int(getattr(
            cfg,
            "SCAN_STABILITY_CHECKS_NETWORK" if network else "SCAN_STABILITY_CHECKS_LOCAL",
            legacy_checks if legacy_checks is not None else checks_default,
        )),
        "probe_bytes": int(getattr(cfg, "SCAN_READ_PROBE_BYTES", 64 * 1024)),
    }


def _readiness_record(exc: BaseException) -> dict | None:
    """Return structured readiness fields for a skipped image."""

    if not isinstance(exc, FileReadinessError):
        return None
    readiness = exc.readiness
    return {
        "readiness_status": readiness.status,
        "readiness_code": readiness.code,
        "readiness_attempts": readiness.attempts,
        "category": readiness_category(readiness),
    }


def _process_timeout(config_name: str, fallback: float) -> float:
    try:
        return max(1.0, float(getattr(cfg, config_name, fallback)))
    except (TypeError, ValueError):
        return float(fallback)


def format_size(value: float) -> str:
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def format_duration(seconds) -> str:
    if seconds is None:
        return "--:--"
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


def relative_name(path: Path) -> str:
    return stable_relative_name(path, cfg.IMAGE_DIR)


def cleanup_staging_cache(*, startup: bool = False) -> None:
    """Prune stale local staging artifacts without affecting source state."""

    if startup and not getattr(cfg, "STAGING_CLEANUP_ON_START", True):
        return
    # The mode only controls new staging operations. Continue pruning an old
    # cache after staging is disabled so switching modes cannot strand files.
    cleanup_staging(
        cfg.STAGING_DIR,
        staging_base_dir=getattr(cfg, "STAGING_BASE_DIR", cfg.STAGING_DIR),
        max_age_seconds=float(getattr(cfg, "STAGING_CLEANUP_DAYS", 7)) * 86400,
    )


def cleanup_confirmed_staging(paths) -> None:
    """Delete staged copies only after the corresponding Album is confirmed."""

    if not getattr(cfg, "STAGING_CLEANUP_AFTER_SUCCESS", True):
        return
    for path in paths or []:
        staged = STAGED_UPLOAD_PATHS.pop(stable_path(path), None)
        if staged is not None:
            remove_staged_file(staged)


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
                "action": "compress" if cfg.IMAGE_COMPRESS_OVERSIZE else "skip",
                "reason": (
                    f"文件大小 {format_size(size)} 超过 Telegram Photo 上限 "
                    f"{format_size(cfg.IMAGE_MAX_BYTES)}"
                ),
            }
            LAST_SCAN_SIZE_SKIPS.append(record)
            if not cfg.IMAGE_COMPRESS_OVERSIZE:
                continue
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


_IMAGE_INFO_CACHE = {}
_IMAGE_INFO_CACHE_LOCK = threading.Lock()


def _hidden_subprocess_kwargs() -> dict:
    """Keep FFmpeg from opening a console window in the Windows GUI build."""

    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": startupinfo,
    }


def _find_ffmpeg() -> str | None:
    configured = os.environ.get("IMAGEIO_FFMPEG_EXE", "").strip()
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for candidate in (
        PROJECT_DIR / "tools" / "ffmpeg" / name,
        PROJECT_DIR / "tools" / name,
        APP_DATA_DIR / "tools" / "ffmpeg" / name,
        APP_DATA_DIR / "tools" / name,
    ):
        if candidate.is_file():
            return str(candidate.resolve())
    return shutil.which("ffmpeg")


def _compressed_path(path: Path) -> Path:
    stat = path.stat()
    digest = hashlib.sha1(
        f"{stable_path(path)}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()
    return COMPRESSED_IMAGE_DIR / f"{digest}.jpg"


def compress_image(path: Path, cancel_event=None) -> Path:
    """Create a temporary JPEG under the Telegram photo limit with FFmpeg."""

    target = int(getattr(cfg, "IMAGE_COMPRESSION_TARGET_BYTES", int(9.5 * 1024 ** 2)))
    final_path = _compressed_path(path)
    if final_path.is_file() and final_path.stat().st_size <= target:
        try:
            with Image.open(final_path) as image:
                image.verify()
            return final_path
        except (OSError, ValueError):
            final_path.unlink(missing_ok=True)

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("找不到 FFmpeg，无法压缩超限图片")
    COMPRESSED_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    last_error = ""
    # First preserve the original dimensions, then reduce dimensions only if
    # quality reduction alone cannot get under the safety margin.
    for scale, qualities in (
        (1.0, (2, 4, 6, 8, 10, 12, 15, 18, 22, 26, 30, 34)),
        (0.9, (4, 8, 12, 16, 20, 24, 28, 32)),
        (0.8, (4, 8, 12, 16, 20, 24, 28, 32)),
        (0.7, (4, 8, 12, 16, 20, 24, 28, 32)),
        (0.6, (4, 8, 12, 16, 20, 24, 28, 32)),
    ):
        if scale == 1.0:
            video_filter = "format=yuv420p"
        else:
            video_filter = (
                f"scale=trunc(iw*{scale}/2)*2:trunc(ih*{scale}/2)*2,format=yuv420p"
            )
        for quality in qualities:
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("图片压缩已取消")
            temp_path = final_path.with_name(
                f".{final_path.stem}.{scale:g}.{quality}.tmp.jpg"
            )
            temp_path.unlink(missing_ok=True)
            try:
                command = [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(path),
                        "-map_metadata",
                        "-1",
                        "-frames:v",
                        "1",
                        "-vf",
                        video_filter,
                        "-c:v",
                        "mjpeg",
                        "-q:v",
                        str(quality),
                        str(temp_path),
                    ]
                process_kwargs = {
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.PIPE,
                    "text": True,
                    "encoding": "utf-8",
                    "errors": "replace",
                    "timeout": _process_timeout(
                        "FFMPEG_COMPRESSION_TIMEOUT_SECONDS",
                        FFMPEG_COMPRESS_TIMEOUT_SECONDS,
                    ),
                    **_hidden_subprocess_kwargs(),
                }
                result = run_cancellable_process(
                    command,
                    cancel_event=cancel_event,
                    **process_kwargs,
                )
                last_error = result.stderr.strip() or f"FFmpeg 退出码 {result.returncode}"
                if result.returncode == 0 and temp_path.is_file() and temp_path.stat().st_size > 0:
                    with Image.open(temp_path) as image:
                        image.verify()
                    if temp_path.stat().st_size <= target:
                        os.replace(temp_path, final_path)
                        return final_path
            except (OSError, subprocess.SubprocessError, ValueError, TimeoutError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            finally:
                temp_path.unlink(missing_ok=True)
    raise RuntimeError(
        f"FFmpeg 无法将图片压到 {format_size(target)} 以下"
        + (f"：{last_error[:300]}" if last_error else "")
    )


def upload_path(path: Path) -> Path:
    return IMAGE_UPLOAD_PATHS.get(stable_path(path), path)


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


def image_info(path: Path) -> tuple[int, int]:
    stat = path.stat()
    key = (stable_path(path), stat.st_size, stat.st_mtime_ns)
    with _IMAGE_INFO_CACHE_LOCK:
        cached = _IMAGE_INFO_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        with Image.open(path) as image:
            width, height = int(image.width), int(image.height)
            image.verify()
    except Exception as exc:
        raise RuntimeError(f"无法读取图片：{path}\n{type(exc).__name__}: {exc}") from exc
    if width <= 0 or height <= 0:
        raise RuntimeError(f"图片尺寸异常：{path}")
    result = (width, height)
    with _IMAGE_INFO_CACHE_LOCK:
        _IMAGE_INFO_CACHE[key] = result
    return result


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
            if size > cfg.IMAGE_MAX_BYTES:
                reason = (
                    f"文件大小 {format_size(size)} 超过 Telegram Photo 上限 "
                    f"{format_size(cfg.IMAGE_MAX_BYTES)}"
                )
                if not cfg.IMAGE_COMPRESS_OVERSIZE:
                    raise RuntimeError(reason)

                IMAGE_UPLOAD_PATHS.pop(stable_path(path), None)
                image_info(path)
                return {"path": path, "oversize": True}
            else:
                IMAGE_UPLOAD_PATHS.pop(stable_path(path), None)
                image_info(path)
                return None
        except Exception as exc:
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
            ordered_bounded_map(executor, paths, worker, worker_count),
            1,
        ):
            if cancel_event is not None and cancel_event.is_set():
                break
            if result and result.get("oversize"):
                target.info(
                    f"预检发现超限图片，将在上传时使用 FFmpeg 压缩：{relative_name(result['path'])}"
                )
            elif result:
                skipped.append(result)
                target.warning(f"跳过图片：{relative_name(result['path'])}")
                target.log(f"跳过图片详情：{result['path']}\n原因：{result['reason']}")

            if getattr(cfg, "IMAGE_VERIFY_ALL_IMAGES", False):
                target.info(f"预检图片 {index}/{total} · {paths[index-1].name}")

    return skipped


def report_skipped_images(skipped, ui=None, *, final=False) -> None:
    if not skipped:
        return
    target = ui or UI
    prefix = "本次任务结束" if final else "图片预检完成"
    size_count = sum(record.get("category") == "size" for record in skipped)
    deferred_count = sum(record.get("category") == "deferred" for record in skipped)
    cancelled_count = sum(record.get("category") == "cancelled" for record in skipped)
    unreadable_count = len(skipped) - size_count - deferred_count - cancelled_count
    parts = []
    if unreadable_count:
        parts.append(f"{unreadable_count} 个无法读取的图片")
    if size_count:
        parts.append(f"{size_count} 个超过 10 MiB 上限的图片")
    if deferred_count:
        parts.append(f"{deferred_count} 个暂时不可读的图片（DEFERRED）")
    if cancelled_count:
        parts.append(f"{cancelled_count} 个因取消而未检查的图片")
    target.warning(
        f"{prefix}：已跳过{'、'.join(parts) or f'{len(skipped)} 个图片'}；"
        "这些文件未写入上传断点，修复后可重新扫描上传。"
    )


def report_scan_size_skips(skipped, ui=None) -> None:
    """Report image size decisions made while scanning the directory."""

    if not skipped:
        return
    target = ui or UI
    compressing = [record for record in skipped if record.get("action") == "compress"]
    rejected = [record for record in skipped if record.get("action") == "skip"]
    if compressing:
        target.warning(
            f"扫描提醒：发现 {len(compressing)} 个超过 10 MiB 的图片；"
            "上传时将尝试用 FFmpeg 生成临时压缩副本。"
        )
    if rejected:
        target.warning(
            f"扫描时跳过 {len(rejected)} 个超过 Telegram Photo 10 MiB 上限的图片；"
            "这些文件未加入上传计划。"
        )
    for record in skipped:
        target.log(
            f"扫描图片大小检查：{record['path']}\n"
            f"处理：{'上传时压缩' if record.get('action') == 'compress' else '跳过'}\n"
            f"原因：{record['reason']}"
        )


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
    source_path = upload_path(path)
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
    if snapshot[0] > cfg.IMAGE_MAX_BYTES and cfg.IMAGE_COMPRESS_OVERSIZE:
        original_size = snapshot[0]
        UI.warning(
            f"图片开始上传，正在使用 FFmpeg 生成临时压缩副本：{relative_name(path)}"
        )
        source_path = compress_image(
            source_path,
            cancel_event,
        ) if cancel_event is not None else compress_image(source_path)
        IMAGE_UPLOAD_PATHS[stable_path(path)] = source_path
        UI.info(
            f"图片压缩完成：{relative_name(path)} · "
            f"{format_size(original_size)} → {format_size(source_path.stat().st_size)}；原文件未修改"
        )
    elif snapshot[0] > cfg.IMAGE_MAX_BYTES:
        raise RuntimeError(
            f"文件大小 {format_size(snapshot[0])} 超过 Telegram Photo 上限 "
            f"{format_size(cfg.IMAGE_MAX_BYTES)}"
        )
    width, height = image_info(source_path)
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


class ImageUploadProgress:
    def __init__(self, all_paths: list[Path], completed_paths: list[Path]):
        self.sizes = {
            path: int((_snapshot_for_path(path) or (0, 0))[0])
            for path in all_paths
        }
        self.total_bytes = sum(self.sizes.values())
        self.total_files = len(all_paths)
        self.completed_bytes = sum(self.sizes[path] for path in completed_paths)
        self.completed_files = len(completed_paths)
        self.current_paths = {}
        self.current_uploaded = {}
        self.file_id_to_path = {}
        self.album_number = 0
        self.album_total = 0
        self.samples = deque()
        self.last_draw = 0.0
        self.lock = threading.Lock()

    def begin_album(self, paths: list[Path], number: int, total: int):
        with self.lock:
            self.album_number = number
            self.album_total = total
            self.current_paths = {stable_path(p): p for p in paths}
            self.current_uploaded = {p: 0 for p in paths}
            self.file_id_to_path = {}
            self.samples.clear()
            self.last_draw = 0.0

    def skip_items(self, paths: list[Path]):
        """Remove files that failed local preparation from progress totals."""

        with self.lock:
            for path in paths:
                size = self.sizes.pop(path, 0)
                self.total_bytes = max(0, self.total_bytes - size)
                self.total_files = max(0, self.total_files - 1)
                self.current_uploaded.pop(path, None)
        self.draw(force=True)

    @staticmethod
    def _photo_file(message):
        content = message.get("content", {})
        if content.get("@type") != "messagePhoto":
            return None
        sizes = content.get("photo", {}).get("sizes", [])
        if not sizes:
            return None
        largest = max(sizes, key=lambda s: int(s.get("width", 0)) * int(s.get("height", 0)))
        return largest.get("photo")

    def register_messages(self, messages, paths):
        with self.lock:
            for message, path in zip(messages, paths):
                file_obj = self._photo_file(message)
                if not file_obj:
                    continue
                if file_obj.get("id") is not None:
                    self.file_id_to_path[file_obj["id"]] = path
                self._apply_unlocked(file_obj)

    def _apply_unlocked(self, file_obj):
        path = None
        local_path = file_obj.get("local", {}).get("path", "")
        if local_path:
            path = self.current_paths.get(stable_path(local_path))
        if path is None:
            path = self.file_id_to_path.get(file_obj.get("id"))
        if path is None:
            return
        remote = file_obj.get("remote", {})
        uploaded = self.sizes[path] if remote.get("is_uploading_completed") else int(remote.get("uploaded_size", 0) or 0)
        self.current_uploaded[path] = max(self.current_uploaded.get(path, 0), min(uploaded, self.sizes[path]))

    def handle_update(self, obj):
        if obj.get("@type") != "updateFile" or not obj.get("file"):
            return
        with self.lock:
            self._apply_unlocked(obj["file"])
        self.draw()

    def _speed_unlocked(self):
        now = time.monotonic()
        uploaded = sum(self.current_uploaded.values())
        self.samples.append((now, uploaded))
        while len(self.samples) > 2 and now - self.samples[0][0] > 3:
            self.samples.popleft()
        if len(self.samples) < 2:
            return 0.0
        old_time, old_bytes = self.samples[0]
        duration = now - old_time
        return max(0.0, (uploaded - old_bytes) / duration) if duration > 0 else 0.0

    def draw(self, force=False):
        with self.lock:
            now = time.monotonic()
            if not force and now - self.last_draw < 0.15:
                return
            self.last_draw = now
            current = sum(self.current_uploaded.values())
            done = self.completed_bytes + current
            ratio = min(done / self.total_bytes, 1.0) if self.total_bytes else 0.0
            speed = self._speed_unlocked()
            eta = (self.total_bytes - done) / speed if speed > 0 else None
            kwargs = dict(
                kind="IMAGE",
                ratio=ratio,
                speed=speed,
                eta=eta,
                detail="每组最多 10 张；Album Caption=编号/自定义文本",
                album_number=self.album_number,
                album_total=self.album_total,
                done_files=self.completed_files,
                total_files=self.total_files,
                done_bytes=done,
                total_bytes=self.total_bytes,
            )
        UI.progress(**kwargs)

    def finish_album(self, paths):
        with self.lock:
            self.completed_bytes += sum(self.sizes[path] for path in paths)
            self.completed_files += len(paths)
            self.current_paths = {}
            self.current_uploaded = {}
            self.file_id_to_path = {}
        self.draw(force=True)
        UI.finish()


def show_file_list(images, state):
    rows = []
    for index, path in enumerate(images, 1):
        snapshot = _snapshot_for_path(path)
        if snapshot is None:
            continue
        size, mtime_ns = snapshot
        rows.append((
            index,
            "✓ 已完成" if state.is_completed(path) else "• 待上传",
            datetime.fromtimestamp(mtime_ns / 1_000_000_000).strftime("%Y-%m-%d %H:%M:%S"),
            format_size(size),
            relative_name(path),
        ))

    UI.files(
        f"图片文件 · 共 {len(images)} 张",
        [
            ("#", {"justify": "right", "width": 5}),
            ("状态", {"no_wrap": True, "width": 10}),
            ("修改时间", {"no_wrap": True, "width": 19}),
            ("大小", {"justify": "right", "no_wrap": True, "width": 11}),
            ("文件", {"overflow": "fold"}),
        ],
        rows,
        kind="IMAGE",
        caption="图片按扫描顺序每组编号；可在 GUI 中编辑每个 Album 的 Caption。",
    )


def show_upload_summary(images, state, completed, pending, total_albums, skipped_items):
    total_bytes = sum(int((_snapshot_for_path(path) or (0, 0))[0]) for path in images)
    pending_bytes = sum(int((_snapshot_for_path(path) or (0, 0))[0]) for path in pending)

    UI.summary(
        f"上传前确认 · TDLib Media Uploader V{cfg.APP_VERSION}",
        [
            ("上传引擎", "TDLib 原生 C++ / tdjson"),
            ("图片目录", cfg.IMAGE_DIR),
            ("扫描图片", len(images)),
            ("跳过坏图片", len(skipped_items)),
            ("排序方式", cfg.IMAGE_SORT_MODE),
            ("全部大小", format_size(total_bytes)),
            ("断点已完成", f"{len(completed)}/{len(images)}"),
            ("本次待上传", f"{len(pending)} · {format_size(pending_bytes)}"),
            ("本次 Album", total_albums),
            (
                "Album 规则",
                f"每组最多 {cfg.IMAGE_ALBUM_SIZE} 张；"
                f"Album Caption=编号，可追加自定义文本；"
                f"文件名清单={'开' if getattr(cfg, 'IMAGE_CAPTION_INCLUDE_FILENAMES', False) else '关'}",
            ),
            ("CHAT_ID", cfg.CHAT_ID),
            ("目标模式", "Channel 频道" if getattr(cfg, "TARGET_MODE", "forum_topic") == "channel" else "超级群组 Forum Topic"),
            ("FORUM_TOPIC_ID", cfg.FORUM_TOPIC_ID if getattr(cfg, "TARGET_MODE", "forum_topic") == "forum_topic" else "不适用"),
            ("状态文件", state.path),
        ],
        kind="IMAGE",
    )

def validate_config():
    if cfg.API_ID == 12345678 or cfg.API_HASH == "YOUR_API_HASH":
        raise RuntimeError("请先在 config.toml 中填写 API_ID / API_HASH。")
    if getattr(cfg, "TARGET_MODE", "forum_topic") == "channel":
        if cfg.CHAT_ID in {0, -1001234567890}:
            raise RuntimeError("请先在 config.toml 中填写频道 Chat ID。")
    elif cfg.CHAT_ID in {0, -1001234567890} or cfg.FORUM_TOPIC_ID <= 0 or cfg.FORUM_TOPIC_ID == 12345:
        raise RuntimeError("请先在 config.toml 中填写 CHAT_ID / FORUM_TOPIC_ID。")


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


def _main_impl():
    activate = getattr(cfg, "activate_target", None)
    if callable(activate):
        activate("image")
    validate_config()
    version = verify_tdjson_version()

    UI.banner(
        f"TDLib Media Uploader V{cfg.APP_VERSION}",
        f"图片模式 · tdjson {version}",
        accent="magenta",
    )

    cancel_event = getattr(UI, "cancel_event", None)
    cleanup_staging_cache(startup=True)
    UI.info(f"扫描图片目录：{cfg.IMAGE_DIR}")
    images = (
        scan_images()
        if cancel_event is None
        else scan_images(cancel_event=cancel_event)
    )
    report_scan_size_skips(LAST_SCAN_SIZE_SKIPS, UI)
    if LAST_SCAN_WARNINGS:
        UI.warning("图片目录扫描提醒：" + "；".join(LAST_SCAN_WARNINGS[:5]))
        UI.log("图片目录扫描提醒详情：\n" + "\n".join(LAST_SCAN_WARNINGS))
    if not images:
        UI.warning("没有找到支持的图片。")
        return

    state = UploadState()
    completed = [p for p in images if state.is_completed(p)]
    pending = [p for p in images if not state.is_completed(p)]
    skipped_items = [
        record for record in LAST_SCAN_SIZE_SKIPS
        if record.get("action") == "skip"
    ]
    # Album membership is derived from the complete scan.  Preflight only
    # marks files that cannot be sent in this run; it must never remove them
    # and let later files move into an earlier Album.
    plans = build_album_plans(images, state)
    preflight_skipped_paths = set()
    if pending:
        UI.info(f"检查 {len(pending)} 个待上传图片的媒体数据…")
        preflight_skipped = (
            preflight_images(pending, UI)
            if cancel_event is None
            else preflight_images(pending, UI, cancel_event=cancel_event)
        )
        skipped_items.extend(preflight_skipped)
        preflight_skipped_paths = {
            stable_path(record["path"])
            for record in preflight_skipped
        }
        if preflight_skipped_paths:
            report_skipped_images(skipped_items, UI)
    pending_plans = [plan for plan in plans if plan["pending_items"]]
    total_albums = len(pending_plans)

    # GUI 调用的扫描与上传流程：保留原有 Album 与断点逻辑。
    if cfg.IMAGE_SHOW_FILE_LIST:
        show_file_list(images, state)

    show_upload_summary(images, state, completed, pending, total_albums, skipped_items)

    sendable_pending = [
        path for path in pending
        if stable_path(path) not in preflight_skipped_paths
    ]
    if not pending or not sendable_pending:
        if skipped_items:
            report_skipped_images(skipped_items, UI, final=True)
        else:
            UI.success("所有图片都已上传完成。")
        cleanup_compressed_images()
        return

    if not UI.confirm_upload():
        cleanup_compressed_images()
        UI.cancelled()
        return

    client = TDJsonClient(UI, "TDLib Image Album Uploader")
    progress = ImageUploadProgress(images, completed)
    if preflight_skipped_paths:
        progress.skip_items([
            path for path in pending
            if stable_path(path) in preflight_skipped_paths
        ])
    client.add_update_callback(progress.handle_update)

    try:
        client.login()
        client.refresh_account_limits()
        caption_limit = int(getattr(client, "caption_length_limit", None) or 1024)
        client.set_fast_options()
        client.validate_target()

        album_global = 0
        for plan in pending_plans:
            album_paths = [
                path for path in plan["pending_items"]
                if stable_path(path) not in preflight_skipped_paths
            ]
            if not album_paths:
                continue
            album_number = plan["number"]
            caption = plan["caption"]["text"]
            caption = with_filename_description(
                caption,
                album_paths,
                getattr(cfg, "IMAGE_CAPTION_INCLUDE_FILENAMES", False),
                max_chars=caption_limit,
            )
            if cancel_event is None:
                contents, ready_paths, runtime_skipped = build_image_contents(
                    album_paths,
                    caption,
                    UI,
                )
            else:
                contents, ready_paths, runtime_skipped = build_image_contents(
                    album_paths,
                    caption,
                    UI,
                    cancel_event,
                )
            if runtime_skipped:
                skipped_items.extend(runtime_skipped)
                progress.skip_items([record["path"] for record in runtime_skipped])
            if not ready_paths:
                UI.warning("当前图片 Album 没有可读取的图片，已跳过。")
                continue
            if ready_paths != album_paths and getattr(cfg, "IMAGE_CAPTION_INCLUDE_FILENAMES", False):
                # ``build_image_contents`` has already validated and created
                # the inputs.  Reusing them avoids a second image decode when
                # a later file is deferred during JIT revalidation.
                caption = with_filename_description(
                    plan["caption"]["text"],
                    ready_paths,
                    True,
                    max_chars=caption_limit,
                )
                if contents:
                    contents[0]["caption"] = formatted_text(caption)
            album_global += 1
            progress.begin_album(ready_paths, album_global, total_albums)
            UI.album(
                kind="IMAGE",
                title=f"Album {album_number} · {album_global}/{total_albums}",
                subtitle=f"{len(ready_paths)} 张待上传图片 · Caption={caption or '无'}",
                rows=[
                    f"{format_size((_snapshot_for_path(path) or (0, 0))[0]):>10}  {relative_name(path)}"
                    for path in ready_paths
                ],
            )
            try:
                # Keep the scanner-owned snapshot in the in-flight journal;
                # ``ready_paths`` is intentionally kept as Paths for the
                # progress adapter, while journal identity must not depend
                # on another network stat at send time.
                journal_items = []
                for path in ready_paths:
                    journal_item = {"path": path}
                    snapshot = IMAGE_SCAN_SNAPSHOTS.get(stable_path(path))
                    if snapshot is not None:
                        journal_item["scan_size"], journal_item["scan_mtime_ns"] = snapshot
                    journal_items.append(journal_item)
                message_ids = client.send_contents(
                    contents,
                    progress,
                    ready_paths,
                    album_key=plan["key"],
                    kind="image",
                    journal_items=journal_items,
                )
            except Exception:
                UI.finish()
                UI.error("当前图片 Album 未写入断点；下次会重新处理这一组。")
                raise
            state.mark_album_completed(ready_paths, message_ids)
            client.finalize_inflight(plan["key"], kind="image", message_ids=message_ids)
            cleanup_confirmed_staging(ready_paths)
            progress.finish_album(ready_paths)
            UI.success(f"图片 Album {album_number} 发送成功 · Caption={caption or '无'} · 断点已保存。")

        UI.banner(
            "全部图片上传完成",
            f"共完成 {total_albums} 个 Album · 断点已保存",
            accent="green",
        )
        report_skipped_images(skipped_items, UI, final=True)
    finally:
        client.remove_update_callback(progress.handle_update)
        client.close()
        cleanup_staging_cache()
        cleanup_compressed_images()
