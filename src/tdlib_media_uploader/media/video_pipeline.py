# -*- coding: utf-8 -*-
"""视频媒体管线：日期读取、按月分组、预检及 Album 内容构建。"""

from __future__ import annotations

import functools
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, TimeoutError as FutureTimeoutError, wait
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import imageio_ffmpeg
from PIL import Image

# Some embedding environments expose ``imageio_ffmpeg`` as a namespace
# package while loading its helpers lazily.  Keep the public lookup available
# for those environments and for integrations that patch the executable.
if not hasattr(imageio_ffmpeg, "get_ffmpeg_exe"):
    imageio_ffmpeg.get_ffmpeg_exe = lambda: shutil.which("ffmpeg") or "ffmpeg"

from ..core.album import CaptionStore, album_key, compose_caption, with_filename_description
from ..core.identity import media_file_signature
from ..core.upload_state import UploadState as SharedUploadState
from ..core.concurrency import cancelable_sleep, ordered_bounded_map
from ..core.filesystem import display_path, file_mtime, file_snapshot, is_network_path, io_worker_count, iter_files, stable_path, retry_fs_operation, validate_scan_root
from ..core.sorting import media_path_sort, relative_name as stable_relative_name
from ..processes.runner import run_cancellable_process
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
from ..config.paths import APP_DATA_DIR, RESOURCE_DIR, VIDEO_STATE_DIR, THUMBNAIL_CACHE_DIR
from ..upload.staging import should_stage, stage_file
from .video_probe import (
    VideoMediaInfo,
    determine_supports_streaming,
    format_unsupported_reason,
    probe_video,
)

PROJECT_DIR = RESOURCE_DIR
STATE_DIR = VIDEO_STATE_DIR
THUMB_CACHE_DIR = THUMBNAIL_CACHE_DIR
LAST_SCAN_ERRORS: list[str] = []
LAST_SCAN_WARNINGS: list[str] = []
LAST_SCAN_SIZE_SKIPS: list[dict] = []
LAST_SCAN_PREMIUM_REQUIRED: list[dict] = []
LAST_SCAN_SNAPSHOTS: dict[str, tuple[int, int]] = {}
STAGED_UPLOAD_PATHS: dict[str, Path] = {}
DEFERRED_STATUS = "DEFERRED"


def _hidden_subprocess_kwargs() -> dict:
    """Return Windows process flags that prevent a console window flash."""

    if os.name != "nt":
        return {}

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": startupinfo,
    }


def _patch_imageio_process_flags() -> None:
    """Make imageio-ffmpeg child processes inherit the same hidden flags."""

    if os.name != "nt":
        return

    try:
        import imageio_ffmpeg._io as imageio_io
        import imageio_ffmpeg._utils as imageio_utils
    except (ImportError, AttributeError):
        return

    original = getattr(imageio_io, "_popen_kwargs", None)
    if not callable(original) or getattr(original, "_tdlib_hidden", False):
        return

    def hidden_imageio_kwargs(prevent_sigint=False):
        result = dict(original(prevent_sigint))
        hidden = _hidden_subprocess_kwargs()
        result["startupinfo"] = hidden["startupinfo"]
        result["creationflags"] = int(result.get("creationflags") or 0) | int(
            hidden["creationflags"]
        )
        return result

    hidden_imageio_kwargs._tdlib_hidden = True
    imageio_io._popen_kwargs = hidden_imageio_kwargs
    if getattr(imageio_utils, "_popen_kwargs", None) is original:
        imageio_utils._popen_kwargs = hidden_imageio_kwargs


_patch_imageio_process_flags()


def _find_ffmpeg_override() -> str | None:
    """Find the LGPL FFmpeg supplied by a portable build or the user.

    imageio-ffmpeg's Windows wheel contains its own FFmpeg executable.  That
    binary is intentionally not used by our release build because its build
    flags may enable GPL components.  Prefer the verified portable binary,
    then an explicit environment override, then a system executable.  The
    environment variable is also how imageio-ffmpeg's reader API receives the
    selected executable.
    """

    configured = os.environ.get("IMAGEIO_FFMPEG_EXE", "").strip()
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())

    names = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg",)
    candidates = [
        PROJECT_DIR / "tools" / "ffmpeg" / names[0],
        PROJECT_DIR / "tools" / names[0],
        PROJECT_DIR / names[0],
        APP_DATA_DIR / "tools" / "ffmpeg" / names[0],
        APP_DATA_DIR / "tools" / names[0],
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())

    return shutil.which("ffmpeg")


_FFMPEG_OVERRIDE = _find_ffmpeg_override()
if _FFMPEG_OVERRIDE:
    # read_frames() resolves its executable through
    # imageio-ffmpeg, so expose the selected binary through its supported
    # environment-variable override without changing the public API.
    os.environ["IMAGEIO_FFMPEG_EXE"] = _FFMPEG_OVERRIDE

UI = HeadlessUI()


def video_limit_text(limit: int) -> str:
    """Use product-facing GB labels for Telegram's protocol limits."""

    value = int(limit)
    if value == int(getattr(cfg, "VIDEO_PREMIUM_MAX_BYTES", 8000 * 524_288)):
        return "约 4 GB"
    if value == int(getattr(cfg, "VIDEO_STANDARD_MAX_BYTES", 4000 * 524_288)):
        return "约 2 GB"
    return format_size(value)


def relative_name(path: Path) -> str:
    return stable_relative_name(path, cfg.VIDEO_DIR)


def normalize_path(path) -> str:
    return stable_path(path)


def _snapshot_for_path(path: Path):
    # Prefer the immutable snapshot captured by the scanner.  A late stat is
    # only a fallback for direct API callers that did not scan first.
    return LAST_SCAN_SNAPSHOTS.get(normalize_path(path)) or file_snapshot(path)


def file_signature(path: Path, snapshot=None) -> str:
    snapshot = snapshot or _snapshot_for_path(path)
    if snapshot is None:
        raise OSError(f"文件暂时不可读取：{path}")
    return media_file_signature(path, root=cfg.VIDEO_DIR, snapshot=snapshot)


FORCED_GROUP_KEY = "__all_videos__"
FORCED_GROUP_LABEL = "全部视频（按顺序分组）"


def video_dates_enabled() -> bool:
    """Return whether date metadata should be read during a video scan."""

    return bool(getattr(cfg, "VIDEO_READ_DATES", True))


def cleanup_staging_cache(*, startup: bool = False) -> None:
    _prune_staging_cache(cfg, startup=startup)


def cleanup_confirmed_staging(paths) -> None:
    _cleanup_confirmed_staging(paths, config=cfg, staged_maps=(STAGED_UPLOAD_PATHS,))


def force_ten_per_album() -> bool:
    if not video_dates_enabled():
        return True
    group_mode = getattr(cfg, "VIDEO_GROUP_MODE", None)
    legacy_enabled = bool(getattr(cfg, "VIDEO_FORCE_TEN_PER_ALBUM", False))
    if group_mode is not None:
        # Keep extensions that still set the legacy flag working while the
        # parser's alias remains synchronized with the new group_mode value.
        return str(group_mode).strip().lower() == "fixed" or legacy_enabled
    return legacy_enabled


def include_group_title() -> bool:
    return bool(getattr(cfg, "VIDEO_CAPTION_INCLUDE_GROUP_TITLE", True))


def include_filename_numbers() -> bool:
    return bool(getattr(cfg, "VIDEO_CAPTION_INCLUDE_FILENAME_NUMBERS", True))


def group_display_name(group_key: str) -> str:
    return FORCED_GROUP_LABEL if group_key == FORCED_GROUP_KEY else str(group_key)


def month_caption(month_key: str) -> str:
    if month_key == FORCED_GROUP_KEY:
        return "Album"
    year_text, month_text = month_key.split("-")
    year, month = int(year_text), int(month_text)
    return f"{year % 100:02d}-{month}" if cfg.VIDEO_CAPTION_YEAR_DIGITS == 2 else f"{year}-{month}"


def scan_videos(cancel_event=None) -> list[Path]:
    global LAST_SCAN_ERRORS, LAST_SCAN_WARNINGS, LAST_SCAN_SIZE_SKIPS, LAST_SCAN_PREMIUM_REQUIRED, LAST_SCAN_SNAPSHOTS
    try:
        root = validate_scan_root(
            cfg.VIDEO_DIR,
            attempts=getattr(cfg, "SCAN_DISCOVERY_ATTEMPTS", 3),
            initial_delay=getattr(cfg, "SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
            max_delay=getattr(cfg, "SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
            cancel_event=cancel_event,
        )
    except TimeoutError:
        LAST_SCAN_ERRORS = []
        LAST_SCAN_WARNINGS = ["目录扫描已取消"]
        LAST_SCAN_SIZE_SKIPS = []
        LAST_SCAN_PREMIUM_REQUIRED = []
        LAST_SCAN_SNAPSHOTS = {}
        return []
    scan_result = iter_files(
        root,
        None,
        cancel_event=cancel_event,
        discovery_attempts=getattr(cfg, "SCAN_DISCOVERY_ATTEMPTS", 3),
        discovery_initial_delay=getattr(cfg, "SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
        discovery_max_delay=getattr(cfg, "SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
    )
    # Configured video suffixes retain their established diagnostics; other
    # names enter only when their header resembles a supported container.
    from .video_probe import has_video_container_signature
    videos = []
    for path in scan_result.paths:
        if scan_result.cancelled or _cancel_requested(cancel_event):
            break
        if path.suffix.lower() in cfg.VIDEO_EXTENSIONS or has_video_container_signature(path):
            videos.append(path)
    LAST_SCAN_ERRORS = list(scan_result.errors)
    LAST_SCAN_WARNINGS = list(scan_result.warnings)
    cancelled = scan_result.cancelled or _cancel_requested(cancel_event)
    if cancelled:
        LAST_SCAN_WARNINGS.append("目录扫描已取消")
    LAST_SCAN_SIZE_SKIPS = []
    LAST_SCAN_PREMIUM_REQUIRED = []
    LAST_SCAN_SNAPSHOTS = {
        key: snapshot.as_tuple()
        for key, snapshot in getattr(scan_result, "snapshots", {}).items()
    }
    if cancelled:
        return []
    accepted = []
    for path in videos:
        if _cancel_requested(cancel_event):
            LAST_SCAN_WARNINGS.append("目录扫描已取消")
            return []
        snapshot = LAST_SCAN_SNAPSHOTS.get(normalize_path(path)) or file_snapshot(path)
        if snapshot is None:
            LAST_SCAN_ERRORS.append(f"{path}: 文件暂时不可读取或为空")
            continue
        size, mtime_ns = snapshot
        if size > cfg.VIDEO_MAX_BYTES:
            LAST_SCAN_SIZE_SKIPS.append({
                "path": path,
                "size": size,
                "limit": cfg.VIDEO_MAX_BYTES,
                "category": "size",
                "action": "preflight",
                "reason": (
                    f"源文件大小 {format_size(size)} 超过 Telegram 视频上限 "
                    f"{video_limit_text(cfg.VIDEO_MAX_BYTES)}；转封装后将按输出大小复核"
                ),
            })
        if size > getattr(cfg, "VIDEO_STANDARD_MAX_BYTES", 4000 * 524_288):
            LAST_SCAN_PREMIUM_REQUIRED.append({
                "path": path,
                "size": size,
                "category": "premium",
                "reason": "视频超过约 2 GB，需要 Telegram Premium 才能上传",
            })
        LAST_SCAN_SNAPSHOTS[normalize_path(path)] = (int(size), int(mtime_ns))
        accepted.append(path)
    videos = accepted
    sort_mode = "name"
    if video_dates_enabled() and getattr(cfg, "VIDEO_SORT_MODE", "mtime") == "mtime":
        sort_mode = "mtime"
    return media_path_sort(
        videos,
        root,
        mode=sort_mode,
        mtime_key=lambda path: LAST_SCAN_SNAPSHOTS.get(normalize_path(path), (0, 0))[1],
    )


def parse_exif_datetime(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("0000-"):
        return None
    text = text.replace("Z", "+00:00")
    if len(text) >= 5:
        tail = text[-5:]
        if tail[0] in "+-" and tail[1:].isdigit():
            text = text[:-5] + tail[:3] + ":" + tail[3:]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            pass
    return None


def format_capture_time(value, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format an optional capture time without inventing a fallback date."""

    if value is None:
        return "未读取日期"
    formatter = getattr(value, "strftime", None)
    if callable(formatter):
        return formatter(fmt)
    return str(value)


EXIFTOOL_BATCH_SIZE = int(getattr(cfg, "EXIFTOOL_BATCH_SIZE", 256))
EXIFTOOL_MAX_RETRIES = int(getattr(cfg, "EXIFTOOL_RETRIES", 2))
# Keep module-level names for integrations that patch them, while taking the
# defaults from the shared [process]/[scan] configuration.
EXIFTOOL_TIMEOUT_SECONDS = float(getattr(cfg, "EXIFTOOL_TIMEOUT_SECONDS", 120))
FFMPEG_METADATA_TIMEOUT_SECONDS = float(getattr(cfg, "FFMPEG_METADATA_TIMEOUT_SECONDS", 30))
FFMPEG_INFO_TIMEOUT_SECONDS = float(getattr(cfg, "FFMPEG_INFO_TIMEOUT_SECONDS", 30))
FFMPEG_THUMBNAIL_TIMEOUT_SECONDS = float(getattr(cfg, "FFMPEG_THUMBNAIL_TIMEOUT_SECONDS", 45))
READINESS_ATTEMPTS = int(getattr(cfg, "SCAN_READINESS_ATTEMPTS", 3))


def _cancel_requested(cancel_event=None) -> bool:
    if cancel_event is not None and cancel_event.is_set():
        return True
    return bool(getattr(UI, "stop_requested", False))


def _readiness_options(path=None) -> dict:
    return _shared_readiness_options(
        cfg,
        network=is_network_path(path or cfg.VIDEO_DIR),
        default_attempts=READINESS_ATTEMPTS,
    )


def _process_timeout(config_name: str, fallback: float) -> float:
    """Read a live timeout value so GUI config reloads take effect."""

    try:
        return max(1.0, float(getattr(cfg, config_name, fallback)))
    except (TypeError, ValueError):
        return float(fallback)


def _exiftool_command(*, recursive: bool = False) -> list[str]:
    command = [
        str(cfg.EXIFTOOL_PATH),
        "-charset", "FileName=UTF8",
        "-j", "-a", "-G1", "-s",
        "-api", "LargeFileSupport=1",
        "-d", "%Y-%m-%d %H:%M:%S%z",
        "-time:all",
    ]
    # ExifTool must never discover a second, potentially inconsistent view of
    # a network directory.  Python supplies the explicit file list through
    # stdin; keep the legacy argument for API compatibility but ignore it.
    for ext in sorted(cfg.VIDEO_EXTENSIONS):
        command += ["-ext", ext.lstrip(".")]
    # Keep all paths in an UTF-8 argument stream. This avoids Windows codepage
    # conversion and also lets callers pass the exact files accepted by the
    # Python scanner, so ExifTool does not perform a second directory walk.
    command += ["-@", "-"]
    return command


def _exiftool_rows_detailed(
    batch,
    *,
    recursive: bool,
    cancel_event=None,
) -> tuple[list[dict], str | None, bool]:
    """Run one ExifTool batch and report whether the output is complete."""
    if not batch:
        return [], None, True
    input_text = "\n".join(str(path) for path in batch) + "\n"
    last_error = ""
    for attempt in range(EXIFTOOL_MAX_RETRIES + 1):
        if _cancel_requested(cancel_event):
            return [], "ExifTool 读取已取消", False
        try:
            command = _exiftool_command(recursive=recursive)
            process_kwargs = {
                "input": input_text,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": _process_timeout("EXIFTOOL_TIMEOUT_SECONDS", EXIFTOOL_TIMEOUT_SECONDS),
                **_hidden_subprocess_kwargs(),
            }
            result = run_cancellable_process(
                command,
                cancel_event=cancel_event,
                **process_kwargs,
            )
        except TimeoutError as exc:
            # The cancellable wrapper uses TimeoutError for an explicit stop;
            # do not turn that user action into a noisy ExifTool diagnostic.
            if _cancel_requested(cancel_event):
                return [], "ExifTool 读取已取消", False
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < EXIFTOOL_MAX_RETRIES:
                cancelable_sleep(min(0.25 * (2 ** attempt), 2.0), cancel_event)
            continue
        except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < EXIFTOOL_MAX_RETRIES:
                cancelable_sleep(min(0.25 * (2 ** attempt), 2.0), cancel_event)
            continue
        stdout = str(result.stdout or "").lstrip("\ufeff").strip()
        stderr = str(result.stderr or "").strip()
        if not stdout:
            last_error = stderr or "ExifTool 对非空文件批次没有返回 JSON"
            if attempt < EXIFTOOL_MAX_RETRIES:
                cancelable_sleep(min(0.25 * (2 ** attempt), 2.0), cancel_event)
            continue
        if result.returncode not in (0, 1):
            last_error = stderr or f"退出码 {result.returncode}"
            if attempt < EXIFTOOL_MAX_RETRIES:
                cancelable_sleep(min(0.25 * (2 ** attempt), 2.0), cancel_event)
            continue
        try:
            rows = json.loads(stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            last_error = f"JSON 输出无法解析：{exc}"
            if attempt < EXIFTOOL_MAX_RETRIES:
                cancelable_sleep(min(0.25 * (2 ** attempt), 2.0), cancel_event)
            continue
        if not isinstance(rows, list):
            last_error = "JSON 输出不是数组"
            if attempt < EXIFTOOL_MAX_RETRIES:
                cancelable_sleep(min(0.25 * (2 ** attempt), 2.0), cancel_event)
            continue
        clean_rows = [row for row in rows if isinstance(row, dict)]
        requested_paths = {normalize_path(path) for path in batch}
        returned_paths = {
            normalize_path(row["SourceFile"])
            for row in clean_rows
            if row.get("SourceFile")
        }
        missing = requested_paths - returned_paths

        # ExifTool commonly writes warnings to stderr while still returning a
        # valid row for every requested file.  The SourceFile set, rather than
        # stderr or the exit code alone, is the completeness contract.  Keep
        # the warning as a diagnostic but accept the complete rows immediately.
        diagnostic = stderr or (
            f"退出码 {result.returncode}" if result.returncode not in (0, 1) else ""
        )
        if not missing:
            return clean_rows, diagnostic or None, True

        last_error = (
            f"ExifTool 输出不完整：批次中有 {len(missing)} 个文件未返回结果"
        )
        if diagnostic:
            last_error = f"{last_error}；{diagnostic}"

        # Preserve valid rows from a partial batch.  The caller retries only
        # the missing paths, so a single bad MP4 cannot make hundreds of good
        # files run through ExifTool again.
        if clean_rows:
            return clean_rows, last_error, False
        if attempt < EXIFTOOL_MAX_RETRIES:
            cancelable_sleep(min(0.25 * (2 ** attempt), 2.0), cancel_event)
    return [], last_error or "ExifTool 未返回有效结果", False


def _exiftool_rows(batch, *, recursive: bool) -> tuple[list[dict], str | None]:
    """Backward-compatible two-value wrapper for integrations."""

    rows, diagnostic, _complete = _exiftool_rows_detailed(
        batch,
        recursive=recursive,
    )
    return rows, diagnostic


def read_exif_metadata(
    paths=None,
    cancel_event=None,
    progress_callback=None,
) -> dict[str, dict]:
    """Read EXIF metadata without letting one bad network file abort a scan.

    ``paths=None`` is retained as a convenience for CLI callers, but it now
    performs the same Python discovery as the GUI and always supplies explicit
    file paths to ExifTool.  ExifTool itself never recursively scans a root.
    """
    global LAST_SCAN_ERRORS, LAST_SCAN_WARNINGS
    if not video_dates_enabled():
        return {}
    if paths is None:
        requested = (
            scan_videos()
            if cancel_event is None
            else scan_videos(cancel_event=cancel_event)
        )
    else:
        requested = [Path(path) for path in paths]
    # An empty scan has no metadata work to do and should remain a successful
    # no-op even when ExifTool is not installed.
    if requested == []:
        return {}
    if not cfg.EXIFTOOL_PATH.exists():
        raise RuntimeError(
            f"找不到 ExifTool：{cfg.EXIFTOOL_PATH}\n"
            "请把对应平台的 ExifTool 可执行文件放入 tools 目录，或在设置中指定路径。"
        )
    requested_keys = {normalize_path(path) for path in requested}
    reported_keys: set[str] = set()

    def emit_progress(path=None):
        _emit_scan_progress(
            progress_callback,
            {
                "phase": "exif",
                "completed": min(len(reported_keys), len(requested)),
                "total": len(requested),
                **({"path": relative_name(path)} if path is not None else {}),
            },
        )

    emit_progress()

    batches = [
        (requested[offset:offset + EXIFTOOL_BATCH_SIZE], False)
        for offset in range(0, len(requested), EXIFTOOL_BATCH_SIZE)
    ]
    index = {}
    diagnostics = []

    def collect(batch, recursive):
        if _cancel_requested(cancel_event):
            return
        rows, diagnostic, complete = _exiftool_rows_detailed(
            batch,
            recursive=recursive,
            cancel_event=cancel_event,
        )
        returned = set()
        for row in rows:
            source = row.get("SourceFile")
            if source:
                key = normalize_path(source)
                if key in requested_keys:
                    index[key] = row
                    returned.add(key)
                    if key not in reported_keys:
                        reported_keys.add(key)
                        emit_progress(source)

        # A warning on a complete batch is useful for the log, but it must not
        # trigger another ExifTool invocation.  Only unresolved SourceFile
        # entries are retried or bisected.
        if diagnostic:
            label = str(batch[0]) if batch else str(cfg.VIDEO_DIR)
            LAST_SCAN_WARNINGS.append(f"ExifTool：{label}: {diagnostic}")

        missing = [
            path for path in batch
            if normalize_path(path) not in returned
        ]
        if not missing:
            if diagnostic and not complete:
                label = str(batch[0]) if batch else str(cfg.VIDEO_DIR)
                diagnostics.append(f"{label}: {diagnostic}")
            return

        if diagnostic and not rows:
            label = str(batch[0]) if batch else str(cfg.VIDEO_DIR)
            diagnostics.append(f"{label}: {diagnostic}")
        if _cancel_requested(cancel_event):
            return
        if len(missing) == 1:
            if len(batch) <= 1:
                label = str(batch[0]) if batch else str(cfg.VIDEO_DIR)
                diagnostics.append(
                    f"{label}: {diagnostic or 'ExifTool 未返回该文件的结果'}"
                )
                # A file that was isolated and exhausted its retries is still
                # completed work for the progress display.  Count it once so
                # a single broken media file does not leave the GUI parked at
                # (total - 1) forever; it remains absent from ``index`` and
                # follows the normal missing-date fallback path.
                path = missing[0]
                key = normalize_path(path)
                if key not in reported_keys:
                    reported_keys.add(key)
                    emit_progress(path)
                return
            # The one-file retry is deliberately isolated from all rows that
            # were already accepted above.
            collect(missing, False)
            return
        midpoint = max(1, len(missing) // 2)
        collect(missing[:midpoint], False)
        collect(missing[midpoint:], False)

    for batch, recursive in batches:
        if _cancel_requested(cancel_event):
            break
        collect(batch, recursive)
    if diagnostics:
        LAST_SCAN_ERRORS.extend(f"ExifTool：{message}" for message in diagnostics)
    if not _cancel_requested(cancel_event):
        emit_progress()
    return index


MEDIA_DATE_MAX_WORKERS = 4


def _parse_ffmetadata(text: str) -> dict[str, str]:
    tags = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";") or line.startswith("["):
            continue
        key, separator, value = line.partition("=")
        if separator:
            tags[key.strip().lower()] = value.strip()
    return tags


def _valid_media_datetime(value):
    dt = parse_exif_datetime(value)
    if dt is None or (dt.year == 1904 and dt.month == 1 and dt.day == 1):
        return None
    return dt


def _read_media_creation_metadata(
    path_text: str,
    size: int,
    mtime_ns: int,
    cancel_event=None,
):
    """Read media creation metadata with one FFmpeg process.

    The global and first-video-stream metadata are written by the same
    FFmpeg invocation. This avoids the old fallback's second full media read
    while keeping both common metadata locations available.
    """
    executable = imageio_ffmpeg.get_ffmpeg_exe()
    fd, stream_metadata_path = tempfile.mkstemp(
        prefix="tdlib-media-stream-",
        suffix=".ffmeta",
    )
    os.close(fd)
    try:
        if _cancel_requested(cancel_event):
            return None
        command = [
            executable,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            path_text,
            "-map_metadata",
            "0",
            "-map_chapters",
            "-1",
            "-f",
            "ffmetadata",
            "pipe:1",
            "-map_metadata",
            "0:s:v:0",
            "-map_chapters",
            "-1",
            "-f",
            "ffmetadata",
            stream_metadata_path,
        ]
        process_kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": _process_timeout("FFMPEG_METADATA_TIMEOUT_SECONDS", FFMPEG_METADATA_TIMEOUT_SECONDS),
            **_hidden_subprocess_kwargs(),
        }
        result = run_cancellable_process(
            command,
            cancel_event=cancel_event,
            **process_kwargs,
        )
        if result.returncode != 0:
            return None
        try:
            stream_text = Path(stream_metadata_path).read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            stream_text = ""

        sources = (
            ("", result.stdout),
            ("stream:", stream_text),
        )
        for prefix, text in sources:
            tags = _parse_ffmetadata(text)
            for key in ("com.apple.quicktime.creationdate", "creation_time"):
                dt = _valid_media_datetime(tags.get(key))
                if dt is not None:
                    return dt, f"Media:{prefix}{key}", True
        return None
    finally:
        try:
            Path(stream_metadata_path).unlink()
        except OSError:
            pass


class _NoMediaDate(Exception):
    """Internal signal used to keep transient/negative results out of cache."""


_media_metadata_cache = {}
_MEDIA_CACHE_MAX_SIZE = 4096
_media_metadata_cache_lock = threading.Lock()


def _media_creation_metadata(path_text: str, size: int, mtime_ns: int, cancel_event=None):
    if _cancel_requested(cancel_event):
        return None

    cache_key = (path_text, size, mtime_ns)
    with _media_metadata_cache_lock:
        if cache_key in _media_metadata_cache:
            return _media_metadata_cache[cache_key]

    try:
        result = _read_media_creation_metadata(
            path_text, size, mtime_ns, cancel_event=cancel_event
        )
        if _cancel_requested(cancel_event):
            return None
        if result is None:
            return None

        with _media_metadata_cache_lock:
            _media_metadata_cache[cache_key] = result
            if len(_media_metadata_cache) > _MEDIA_CACHE_MAX_SIZE:
                _media_metadata_cache.pop(next(iter(_media_metadata_cache)))
        return result
    except _NoMediaDate:
        return None


def _clear_media_metadata_cache():
    with _media_metadata_cache_lock:
        _media_metadata_cache.clear()

_media_creation_metadata.cache_clear = _clear_media_metadata_cache


def read_media_creation_time(path: Path, cancel_event=None):
    if not video_dates_enabled():
        return None
    try:
        if _cancel_requested(cancel_event):
            return None
        readiness = wait_for_file_ready(
            path,
            **_readiness_options(path),
            probe=True,
            cancel_event=cancel_event,
        )
        if not readiness.ready:
            return None
        info = readiness.snapshot
        if cancel_event is None:
            return _media_creation_metadata(
                display_path(path), info.size, info.mtime_ns
            )
        return _media_creation_metadata(
            display_path(path),
            info.size,
            info.mtime_ns,
            cancel_event=cancel_event,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        # Missing tools, unreadable/unsupported files or a timed-out share
        # leave the final decision to missing_date_policy.
        return None


def _normalize_media_timezone(dt, utc_style):
    zone = cfg.VIDEO_QUICKTIME_UTC_TARGET_ZONE
    if utc_style:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(zone)) if zone else dt.astimezone()
    return dt


def _choose_embedded_capture_time(row: dict | None):
    row = row or {}
    exif_candidates = []
    media_candidates = []

    for key, value in row.items():
        if key == "SourceFile":
            continue
        normalized = str(key).lower()
        tag = normalized.rsplit(":", 1)[-1]
        group = normalized.split(":", 1)[0]
        dt = _valid_media_datetime(value)
        if dt is None:
            continue

        # ExifTool may expose EXIF dates as ExifIFD/IFD0, XMP or Composite.
        # Treat DateTimeOriginal as an EXIF-style date unless it is clearly a
        # file/container date, and keep CreateDate from video groups for the
        # optional media pass below.
        exif_group = group in {"exif", "exififd", "ifd0", "xmp", "composite"}
        if tag == "datetimeoriginal" and group not in {"file", "quicktime", "keys"}:
            exif_candidates.append((0, key, dt, False))
        elif tag == "createdate" and exif_group:
            exif_candidates.append((1, key, dt, False))

        if getattr(cfg, "VIDEO_READ_MEDIA_CREATION_DATE", True):
            is_media = (
                normalized in {
                    "keys:creationdate",
                    "quicktime:creationdate",
                    "quicktime:createdate",
                }
                or tag in {"mediacreatedate", "trackcreatedate"}
                or (tag == "creationdate" and group not in {"file", "exif", "exififd", "ifd0", "xmp"})
            )
            if is_media:
                utc_style = (
                    normalized == "quicktime:createdate"
                    or tag in {"trackcreatedate", "mediacreatedate"}
                )
                media_priority = {
                    "keys:creationdate": 0,
                    "quicktime:creationdate": 1,
                    "quicktime:createdate": 2,
                }.get(normalized, 3)
                media_candidates.append((media_priority, key, dt, utc_style))

    if exif_candidates:
        _, key, dt, _ = min(exif_candidates, key=lambda item: item[0])
        return {"datetime": dt, "tag": key, "fallback": False}

    if media_candidates:
        _, key, dt, utc_style = min(media_candidates, key=lambda item: item[0])
        if utc_style and cfg.VIDEO_QUICKTIME_UTC_TARGET_ZONE:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(ZoneInfo(cfg.VIDEO_QUICKTIME_UTC_TARGET_ZONE))
        return {"datetime": dt, "tag": key, "fallback": False}

    return None


def _media_selection(media):
    if media is None:
        return None
    dt, tag, utc_style = media
    return {
        "datetime": _normalize_media_timezone(dt, utc_style),
        "tag": tag,
        "fallback": False,
    }


def _fallback_capture_time(path: Path):
    if not video_dates_enabled():
        return None
    if cfg.VIDEO_MISSING_DATE_POLICY == "mtime":
        snapshot = file_snapshot(path)
        if snapshot is None:
            return None
        return {
            "datetime": datetime.fromtimestamp(snapshot[1] / 1_000_000_000),
            "tag": "FileSystem:ModifyTime",
            "fallback": True,
        }
    return None


def choose_capture_time(
    path: Path,
    row: dict | None,
    *,
    probe_media: bool = True,
    allow_fallback: bool = True,
    cancel_event=None,
):
    if not video_dates_enabled():
        return None
    selected = _choose_embedded_capture_time(row)
    if selected is not None:
        return selected
    if probe_media and getattr(cfg, "VIDEO_READ_MEDIA_CREATION_DATE", True):
        selected = _media_selection(read_media_creation_time(path, cancel_event))
        if selected is not None:
            return selected
    return _fallback_capture_time(path) if allow_fallback else None


def _emit_scan_progress(progress_callback, payload: dict):
    if progress_callback is None:
        return
    try:
        progress_callback(payload)
    except Exception:
        # A progress display must never abort a scan.
        pass


def _probe_media_dates(paths, progress_callback=None, cancel_event=None):
    if (
        not paths
        or not video_dates_enabled()
        or not getattr(cfg, "VIDEO_READ_MEDIA_CREATION_DATE", True)
    ):
        return {}

    total = len(paths)
    completed = 0
    results = {}
    iterator = iter(paths)
    futures = {}
    _emit_scan_progress(
        progress_callback,
        {"phase": "media_date", "completed": 0, "total": total},
    )

    worker_count = io_worker_count(
        cfg.VIDEO_DIR,
        local=getattr(cfg, "IO_WORKERS_LOCAL", MEDIA_DATE_MAX_WORKERS),
        network=getattr(cfg, "IO_WORKERS_NETWORK", 2),
    )
    probe = (
        (lambda path: read_media_creation_time(path, cancel_event))
        if cancel_event is not None
        else read_media_creation_time
    )
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="tdlib-media-date",
    ) as executor:
        for _ in range(min(worker_count, total)):
            path = next(iterator, None)
            if path is None:
                break
            futures[executor.submit(probe, path)] = path

        while futures:
            if _cancel_requested(cancel_event):
                break
            done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
            for future in done:
                path = futures.pop(future)
                try:
                    results[normalize_path(path)] = future.result()
                except Exception as exc:
                    LAST_SCAN_ERRORS.append(f"{path}: {exc}")
                    results[normalize_path(path)] = None
                completed += 1
                _emit_scan_progress(
                    progress_callback,
                    {
                        "phase": "media_date",
                        "completed": completed,
                        "total": total,
                        "path": relative_name(path),
                    },
                )
                next_path = next(iterator, None)
                if next_path is not None:
                    futures[executor.submit(probe, next_path)] = next_path
    return results


def build_items(videos, metadata_index, progress_callback=None, cancel_event=None):
    items, missing = [], []
    metadata_index = metadata_index or {}

    def snapshot_fields(path):
        snapshot = LAST_SCAN_SNAPSHOTS.get(normalize_path(path)) or file_snapshot(path)
        if snapshot is None:
            return {}
        return {"scan_size": snapshot[0], "scan_mtime_ns": snapshot[1]}

    if not video_dates_enabled():
        _emit_scan_progress(
            progress_callback,
            {"phase": "date_disabled", "completed": len(videos), "total": len(videos)},
        )
        filename_items = []
        for path in videos:
            item = {
                "path": path,
                "capture_time": None,
                "month_key": FORCED_GROUP_KEY,
                "date_tag": "未读取日期",
                "fallback": False,
                "requires_premium": (
                    int((LAST_SCAN_SNAPSHOTS.get(normalize_path(path)) or (0, 0))[0])
                    > getattr(cfg, "VIDEO_STANDARD_MAX_BYTES", 4000 * 524_288)
                ),
            }
            snapshot = LAST_SCAN_SNAPSHOTS.get(normalize_path(path)) or file_snapshot(path)
            if snapshot is not None:
                item["scan_size"], item["scan_mtime_ns"] = snapshot
            filename_items.append(item)
        return filename_items, []
    embedded = {}
    pending_media = []
    unavailable = set()

    for path in videos:
        if _cancel_requested(cancel_event):
            break
        try:
            selected = choose_capture_time(
                path,
                metadata_index.get(normalize_path(path)),
                probe_media=False,
                allow_fallback=False,
            )
        except (OSError, ValueError, OverflowError) as exc:
            # A network share can disappear between os.walk() and metadata
            # handling. Treat that file as temporarily unavailable instead
            # of allowing a transient SMB error to abort the whole scan.
            LAST_SCAN_ERRORS.append(f"{path}: {exc}")
            unavailable.add(normalize_path(path))
            missing.append(path)
            continue

        key = normalize_path(path)
        if selected is None:
            pending_media.append(path)
        else:
            embedded[key] = selected

    _emit_scan_progress(
        progress_callback,
        {"phase": "exif", "completed": len(videos), "total": len(videos)},
    )
    media_dates = _probe_media_dates(
        pending_media,
        progress_callback,
        cancel_event,
    )

    for path in videos:
        if _cancel_requested(cancel_event):
            break
        key = normalize_path(path)
        if key in unavailable:
            continue
        selected = embedded.get(key)
        if selected is None:
            selected = _media_selection(media_dates.get(key))
        if selected is None:
            try:
                selected = _fallback_capture_time(path)
            except (OSError, ValueError, OverflowError) as exc:
                LAST_SCAN_ERRORS.append(f"{path}: {exc}")
                missing.append(path)
                continue
        if selected is None:
            missing.append(path)
            continue
        dt = selected["datetime"]
        item = {
            "path": path,
            "capture_time": dt,
            "month_key": format_capture_time(dt, "%Y-%m"),
            "date_tag": selected["tag"],
            "fallback": selected["fallback"],
            "requires_premium": (
                int((LAST_SCAN_SNAPSHOTS.get(key) or (0, 0))[0])
                > getattr(cfg, "VIDEO_STANDARD_MAX_BYTES", 4000 * 524_288)
            ),
        }
        snapshot = LAST_SCAN_SNAPSHOTS.get(key) or file_snapshot(path)
        if snapshot is not None:
            item["scan_size"], item["scan_mtime_ns"] = snapshot
        items.append(item)
    # ``scan_videos`` already applied the selected shared path/mtime order.
    # Capture metadata only determines month membership; it must never
    # override that order inside a month.
    return items, missing


_VIDEO_INFO_CACHE = {}
_VIDEO_INFO_CACHE_LOCK = threading.Lock()


def _next_frame_metadata(reader, *, timeout: float, cancel_event=None):
    """Read imageio's header with a timeout and close its child process."""

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tdlib-video-header")
    future = executor.submit(next, reader)
    try:
        limit = max(1.0, float(timeout))
        if cancel_event is None:
            return future.result(timeout=limit)
        deadline = time.monotonic() + limit
        while True:
            if _cancel_requested(cancel_event):
                try:
                    reader.close()
                except Exception:
                    pass
                raise TimeoutError("读取视频媒体信息已取消")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    reader.close()
                except Exception:
                    pass
                raise TimeoutError("读取视频媒体信息超时")
            try:
                return future.result(timeout=min(0.1, remaining))
            except FutureTimeoutError:
                continue
    except FutureTimeoutError as exc:
        try:
            reader.close()
        except Exception:
            pass
        raise TimeoutError("读取视频媒体信息超时") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def video_info(path: Path, cancel_event=None):
    if _cancel_requested(cancel_event):
        raise TimeoutError("读取视频媒体信息已取消")
    snapshot = file_snapshot(path)
    if snapshot is None:
        raise RuntimeError(f"视频文件暂时不可读取：{path}")
    key = (stable_path(path), snapshot[0], snapshot[1])
    with _VIDEO_INFO_CACHE_LOCK:
        cached = _VIDEO_INFO_CACHE.get(key)
    if cached is not None:
        return cached

    info = probe_video(
        path,
        cancel_event=cancel_event,
        timeout=_process_timeout(
            "FFMPEG_INFO_TIMEOUT_SECONDS", FFMPEG_INFO_TIMEOUT_SECONDS
        ),
    )
    if info.compatibility == "invalid":
        raise RuntimeError(
            f"视频媒体属性异常：{path.name} | {info.width}x{info.height} | {info.duration:.3f}s"
        )
    with _VIDEO_INFO_CACHE_LOCK:
        _VIDEO_INFO_CACHE[key] = info
    return info


def _thumbnail_timestamp_seconds(value=None) -> float:
    """Normalize a thumbnail seek position to the application's 10 ms unit."""

    raw = (
        getattr(cfg, "VIDEO_THUMBNAIL_TIMESTAMP_SECONDS", 1.0)
        if value is None
        else value
    )
    try:
        seconds = float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("视频缩略图截图时间必须是数字。") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise RuntimeError("视频缩略图截图时间必须是大于等于 0 的有限数字。")
    try:
        return round(seconds * 100) / 100
    except (OverflowError, ValueError) as exc:
        raise RuntimeError("视频缩略图截图时间数值过大。") from exc


def _thumbnail_warning(message: str) -> None:
    warning = getattr(UI, "warning", None)
    if callable(warning):
        try:
            warning(message)
        except Exception:
            pass


def build_thumbnail(
    path: Path,
    cancel_event=None,
    *,
    timestamp_seconds=None,
    duration=None,
    logical_path: Path | None = None,
):
    """Create or reuse a cached thumbnail at the requested video timestamp."""

    timestamp = _thumbnail_timestamp_seconds(timestamp_seconds)
    if duration is not None:
        try:
            duration_value = float(duration)
        except (TypeError, ValueError):
            duration_value = None
        if (
            duration_value is not None
            and math.isfinite(duration_value)
            and duration_value >= 0
            and timestamp >= duration_value
            and timestamp != 0.0
        ):
            _thumbnail_warning(
                f"视频缩略图截图时间 {timestamp:.2f} 秒已到达或超过视频时长 "
                f"{duration_value:.2f} 秒，已回退到 0.00 秒：{path}"
            )
            timestamp = 0.0

    THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    identity_path = Path(logical_path if logical_path is not None else path)
    stat = identity_path.stat()
    cache_key = hashlib.sha1(
        f"{stable_path(identity_path)}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()
    final_path = THUMB_CACHE_DIR / f"{cache_key}.jpg"
    temp_path = THUMB_CACHE_DIR / f"{cache_key}.tmp.jpg"
    if final_path.exists() and final_path.stat().st_size > 0:
        try:
            with Image.open(final_path) as image:
                image.verify()
            with Image.open(final_path) as image:
                return final_path, image.width, image.height
        except (OSError, ValueError):
            # A killed process can leave a zero-byte or partially written
            # thumbnail.  Remove it and regenerate instead of treating the
            # cache corruption as a bad source video.
            try:
                final_path.unlink()
            except OSError:
                pass

    ffmpeg = _FFMPEG_OVERRIDE or imageio_ffmpeg.get_ffmpeg_exe()
    process_kwargs = _hidden_subprocess_kwargs()

    last_error = ""

    def extract(second: float):
        nonlocal last_error
        if _cancel_requested(cancel_event):
            last_error = "FFmpeg 缩略图处理已取消"
            return False
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            command = [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{second:.2f}", "-i", display_path(path), "-frames:v", "1",
                    "-vf", "scale=640:640:force_original_aspect_ratio=decrease",
                    "-q:v", "3", str(temp_path),
                ]
            run_kwargs = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": _process_timeout("FFMPEG_THUMBNAIL_TIMEOUT_SECONDS", FFMPEG_THUMBNAIL_TIMEOUT_SECONDS),
                **process_kwargs,
            }
            result = run_cancellable_process(
                command,
                cancel_event=cancel_event,
                **run_kwargs,
            )
            last_error = result.stderr.strip() or f"FFmpeg 退出码 {result.returncode}"
            return result.returncode == 0 and temp_path.exists() and temp_path.stat().st_size > 0
        except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            return False

    attempts = [timestamp]
    if timestamp != 0.0:
        attempts.append(0.0)
    extracted = False
    for second in attempts:
        if extract(second):
            extracted = True
            break
        if _cancel_requested(cancel_event):
            break

    if not extracted:
        detail = f"；FFmpeg：{last_error[:500]}" if last_error else ""
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"无法生成视频预览图：{path}{detail}")

    try:
        with Image.open(temp_path) as image:
            image = image.convert("RGB")
            image.thumbnail((cfg.VIDEO_THUMB_MAX_EDGE, cfg.VIDEO_THUMB_MAX_EDGE), Image.Resampling.LANCZOS)
            for quality in (85, 75, 65, 55, 45, 35, 25):
                image.save(final_path, "JPEG", quality=quality, optimize=True)
                if final_path.stat().st_size <= cfg.VIDEO_THUMB_TARGET_BYTES:
                    break
            width, height = image.width, image.height
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return final_path, width, height


def prepare_video(path: Path, cancel_event=None):
    """Read all local video data required before a Telegram request."""

    info = (
        video_info(path)
        if cancel_event is None
        else video_info(path, cancel_event=cancel_event)
    )
    if cfg.VIDEO_GENERATE_THUMBNAIL and (
        Path(path).stat().st_size <= cfg.VIDEO_MAX_BYTES
        or can_remux_video(path, info=info)
    ):
        if cancel_event is None:
            build_thumbnail(path, duration=info.get("duration"))
        else:
            build_thumbnail(path, cancel_event, duration=info.get("duration"))
    return info


def can_remux_video(path: Path, *, info=None, cancel_event=None) -> bool:
    """Defer source-size checks only for verified stream-copy candidates."""
    policy = str(getattr(cfg, "VIDEO_COMPATIBILITY_POLICY", "remux")).strip().lower()
    if policy == "original":
        return False
    if info is None:
        info = video_info(path, cancel_event=cancel_event)
    return getattr(info, "compatibility", None) == "remux"


def preflight_videos(items, ui=None, cancel_event=None) -> list[dict]:
    """Find unreadable videos before login and Album construction.

    The source file remains in the scan and is intentionally not marked as
    uploaded.  A later run can retry it after the user repairs or replaces the
    file.
    """

    target = ui or UI
    skipped = []
    total = len(items)

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
            size = snapshot[0]
            # Preserve the historical one-argument call for integrations and
            # tests that provide a lightweight preparation hook.  The worker
            # passes cancellation only when a caller requested it.
            info = (
                prepare_video(path)
                if cancel_event is None
                else prepare_video(path, cancel_event)
            )
            if size > cfg.VIDEO_MAX_BYTES and not can_remux_video(path, info=info):
                raise RuntimeError(
                    f"文件大小 {format_size(size)} 超过 Telegram 视频上限 "
                    f"{video_limit_text(cfg.VIDEO_MAX_BYTES)}"
                )
            if getattr(info, "compatibility", "native") not in {"native", "legacy"}:
                policy = getattr(cfg, "VIDEO_COMPATIBILITY_POLICY", "remux")
                if getattr(info, "compatibility", "") == "remux":
                    if policy == "original":
                        raise RuntimeError(
                            f"视频格式为 {info.container.upper()}，当前策略配置为 original（仅允许原生格式），已跳过：{path.name}"
                        )
                else:
                    raise RuntimeError(
                        format_unsupported_reason(path, info)
                        if isinstance(info, VideoMediaInfo)
                        else f"当前编码不在直接 Telegram Video 支持范围内，已跳过：{path.name}"
                    )
            return None
        except TimeoutError as exc:
            if _cancel_requested(cancel_event) or "取消" in str(exc) or "cancelled" in str(exc).lower():
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
            if _cancel_requested(cancel_event):
                raise TimeoutError("视频预检已取消")
            readiness_record = _readiness_record(exc)
            record = {
                "item": item,
                "path": path,
                "reason": f"{type(exc).__name__}: {exc}" if "\n" not in str(exc) else str(exc),
                "category": (
                    "size" if "超过 Telegram 视频上限" in str(exc)
                    else "unsupported" if "不在直接 Telegram Video 支持范围内" in str(exc) or "已跳过" in str(exc)
                    else readiness_record["category"] if readiness_record
                    else "deferred" if isinstance(exc, (OSError, TimeoutError))
                    else "unreadable"
                ),
            }
            if readiness_record:
                record.update(readiness_record)
            return record

    worker_count = io_worker_count(
        cfg.VIDEO_DIR,
        local=getattr(cfg, "IO_WORKERS_LOCAL", MEDIA_DATE_MAX_WORKERS),
        network=getattr(cfg, "IO_WORKERS_NETWORK", 2),
    )
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="tdlib-preflight") as executor:
        for index, result in enumerate(
            ordered_bounded_map(executor, items, worker, worker_count),
            1,
        ):
            if _cancel_requested(cancel_event):
                raise TimeoutError("视频预检已取消")
            if getattr(cfg, "VIDEO_VERIFY_ALL_METADATA", False):
                target.info(f"预检视频 {index}/{total} · {items[index-1]['path'].name}")
            if result:
                skipped.append(result)
                target.warning(f"跳过无法读取的视频：{relative_name(result['path'])}")
                target.log(f"跳过视频详情：{result['path']}\n原因：{result['reason']}")

    return skipped


def build_video_contents(
    items,
    caption: str,
    ui=None,
    cancel_event=None,
    group_key: str | None = None,
    max_bytes: int | None = None,
    is_premium: bool | None = None,
):
    """Build an Album while isolating files that became unreadable later."""

    target = ui or UI
    contents = []
    valid_items = []
    skipped = []
    for item in items:
        try:
            item_caption = caption if not valid_items else ""
            video_kwargs = {}
            if group_key is not None:
                video_kwargs["group_key"] = group_key
            if cancel_event is not None:
                video_kwargs["cancel_event"] = cancel_event
            if max_bytes is not None:
                video_kwargs["max_bytes"] = max_bytes
            if is_premium is not None:
                video_kwargs["is_premium"] = is_premium
            contents.append(input_video(item, item_caption, **video_kwargs))
            valid_items.append(item)
        except Exception as exc:
            path = item["path"]
            record = {
                "item": item,
                "path": path,
                "reason": f"{type(exc).__name__}: {exc}",
                "category": (
                    "deferred" if isinstance(exc, (OSError, TimeoutError))
                    else "unreadable"
                ),
            }
            readiness_record = _readiness_record(exc)
            if readiness_record:
                record.update(readiness_record)
                record["category"] = readiness_record["category"]
            skipped.append(record)
            target.warning(f"跳过上传前变得无法读取的视频：{relative_name(path)}")
            target.log(f"跳过视频详情：{path}\n原因：{record['reason']}")
    return contents, valid_items, skipped


def input_video(
    item,
    caption: str,
    cancel_event=None,
    *,
    generate_thumbnail=None,
    thumbnail_timestamp_seconds=None,
    group_key: str | None = None,
    max_bytes: int | None = None,
    is_premium: bool | None = None,
):
    """Build one TDLib video content object for standalone and mixed albums.

    Mixed uploads use the same payload builder as standalone video uploads;
    only the thumbnail preference differs by mode.
    """

    path = item["path"]
    readiness = wait_for_file_ready(
        path,
        expected_size=item.get("scan_size"),
        expected_mtime_ns=item.get("scan_mtime_ns"),
        **_readiness_options(path),
        cancel_event=cancel_event,
    )
    raise_for_file_readiness(path, readiness)
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

    info = (
        video_info(source_path)
        if cancel_event is None
        else video_info(source_path, cancel_event=cancel_event)
    )
    thumbnail_enabled = (
        cfg.VIDEO_GENERATE_THUMBNAIL
        if generate_thumbnail is None
        else bool(generate_thumbnail)
    )
    actual_group_key = group_key or item.get("group_key") or item.get("album_key") or "default"
    from .video_prepare import prepare_video_for_telegram
    prep = prepare_video_for_telegram(
        working_path=source_path,
        original_path=path,
        group_key=str(actual_group_key),
        cancel_event=cancel_event,
        generate_thumbnail=thumbnail_enabled,
        thumbnail_timestamp_seconds=thumbnail_timestamp_seconds,
        max_bytes=max_bytes,
        is_premium=is_premium,
        info=info,
        config=cfg,
    )
    info = prep.info
    supports_streaming = (
        determine_supports_streaming(info)
        if isinstance(info, VideoMediaInfo)
        else bool(info.get("supports_streaming", True))
    )
    thumbnail = None
    if thumbnail_enabled and prep.thumbnail_path is not None:
        thumbnail = {
            "@type": "inputThumbnail",
            "thumbnail": {"@type": "inputFileLocal", "path": display_path(prep.thumbnail_path)},
            "width": int(prep.thumbnail_width),
            "height": int(prep.thumbnail_height),
        }
    return {
        "@type": "inputMessageVideo",
        "video": {"@type": "inputFileLocal", "path": display_path(prep.upload_path)},
        "thumbnail": thumbnail,
        "cover": None,
        "start_timestamp": 0,
        "added_sticker_file_ids": [],
        "duration": int(max(1, round(info["duration"]))),
        "width": int(info["width"]),
        "height": int(info["height"]),
        "supports_streaming": supports_streaming,
        "caption": formatted_text(caption),
        "show_caption_above_media": False,
        "self_destruct_type": None,
        "has_spoiler": False,
    }


class UploadState(SharedUploadState):
    """Video-specific facade over the shared V1.9 checkpoint format."""

    def __init__(self, target=None):
        super().__init__(
            kind="video",
            source_root=cfg.VIDEO_DIR,
            target=target or getattr(cfg, "target_for", lambda _kind: {}) ("video"),
            state_dir=STATE_DIR,
            reset=getattr(cfg, "VIDEO_RESET_STATE", False),
            filename_prefix="video_upload_state",
            snapshot_provider=_snapshot_for_path,
        )


def make_groups(items):
    if force_ten_per_album():
        return {FORCED_GROUP_KEY: list(items)}
    groups = defaultdict(list)
    for item in items:
        groups[item["month_key"]].append(item)
    return groups


def build_album_plans(items, state=None) -> list[dict]:
    """Build stable Album plans from the complete scan.

    Normal mode keeps the historical month grouping.  The optional fixed mode
    puts the complete scan into consecutive groups of the configured size,
    regardless of each video's capture month.
    """
    store = CaptionStore("video")
    # ``scan_videos`` normally supplies immutable snapshots for every item.
    # Materialize a missing snapshot once for direct API callers so the shared
    # Album key remains snapshot-only and never stats a file while hashing.
    for raw_item in items:
        path = Path(raw_item.get("path") if isinstance(raw_item, dict) else raw_item)
        key = normalize_path(path)
        if key not in LAST_SCAN_SNAPSHOTS:
            snapshot = file_snapshot(path)
            if snapshot is not None:
                LAST_SCAN_SNAPSHOTS[key] = (int(snapshot[0]), int(snapshot[1]))
    plans = []
    groups = make_groups(items)
    album_size = cfg.VIDEO_ALBUM_SIZE
    for month_key in sorted(groups):
        month_items = groups[month_key]
        for start in range(0, len(month_items), album_size):
            album_items = list(month_items[start:start + album_size])
            album_number = start // album_size + 1
            default_label = ""
            if include_group_title():
                default_label = (
                    f"Album {album_number}"
                    if month_key == FORCED_GROUP_KEY
                    else month_caption(month_key)
                )
            pending = [
                item for item in album_items
                if state is None or not state.is_completed(item)
            ]
            key_group = f"{month_key}:{album_number}" if month_key == FORCED_GROUP_KEY else month_key
            key = album_key(
                "video",
                key_group,
                album_items,
                root=cfg.VIDEO_DIR,
                snapshot_provider=lambda path: LAST_SCAN_SNAPSHOTS.get(normalize_path(path)),
            )
            record = store.get(key, default_label)
            base_label = record["base_label"] if include_group_title() else ""
            if include_group_title() and not base_label:
                base_label = default_label
            plans.append({
                "key": key,
                "month_key": month_key,
                "number": album_number,
                "items": album_items,
                "pending_items": pending,
                "caption": {
                    "base_label": base_label,
                    "custom_text": record["custom_text"],
                    "text": compose_caption(
                        base_label,
                        record["custom_text"],
                        getattr(cfg, "VIDEO_ALBUM_CAPTION_SEPARATOR", " · "),
                    ),
                },
            })
    return plans
