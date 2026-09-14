# -*- coding: utf-8 -*-
"""TDLib 视频按月 Album 上传器。"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import defaultdict, deque
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

from album_metadata import CaptionStore, album_key, compose_caption, with_filename_description
from path_utils import (
    cancelable_sleep,
    display_path,
    file_mtime,
    file_snapshot,
    io_worker_count,
    iter_files,
    media_path_sort,
    ordered_bounded_map,
    relative_name as stable_relative_name,
    run_cancellable_process,
    stable_path,
    wait_for_file_ready,
)
import app_config as cfg
from tdlib_common import HeadlessUI, TDJsonClient, formatted_text, verify_tdjson_version
from runtime_paths import APP_DATA_DIR, RESOURCE_DIR
from staging import cleanup_staging, stage_file

PROJECT_DIR = RESOURCE_DIR
STATE_DIR = APP_DATA_DIR / ".state"
THUMB_CACHE_DIR = APP_DATA_DIR / ".thumb_cache"
LAST_SCAN_ERRORS: list[str] = []
LAST_SCAN_SIZE_SKIPS: list[dict] = []
LAST_SCAN_SNAPSHOTS: dict[str, tuple[int, int]] = {}
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


def _find_ffprobe() -> str | None:
    configured = os.environ.get("TDLIB_FFPROBE_EXE", "").strip()
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    if _FFMPEG_OVERRIDE:
        sibling = Path(_FFMPEG_OVERRIDE).with_name(
            "ffprobe.exe" if os.name == "nt" else "ffprobe"
        )
        if sibling.is_file():
            return str(sibling.resolve())
    return shutil.which("ffprobe")


_FFMPEG_OVERRIDE = _find_ffmpeg_override()
if _FFMPEG_OVERRIDE:
    # read_frames() resolves its executable through
    # imageio-ffmpeg, so expose the selected binary through its supported
    # environment-variable override without changing the public API.
    os.environ["IMAGEIO_FFMPEG_EXE"] = _FFMPEG_OVERRIDE

UI = HeadlessUI()


def format_size(value: float) -> str:
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def relative_name(path: Path) -> str:
    return stable_relative_name(path, cfg.VIDEO_DIR)


def normalize_path(path) -> str:
    return stable_path(path)


def _snapshot_for_path(path: Path):
    return file_snapshot(path) or LAST_SCAN_SNAPSHOTS.get(normalize_path(path))


def file_signature(path: Path, snapshot=None) -> str:
    snapshot = snapshot or _snapshot_for_path(path)
    if snapshot is None:
        raise OSError(f"文件暂时不可读取：{path}")
    size, mtime_ns = snapshot
    raw = f"{relative_name(path).lower()}|{size}|{mtime_ns}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


FORCED_GROUP_KEY = "__all_videos__"
FORCED_GROUP_LABEL = "全部视频（按顺序分组）"


def video_dates_enabled() -> bool:
    """Return whether date metadata should be read during a video scan."""

    return bool(getattr(cfg, "VIDEO_READ_DATES", True))


def cleanup_staging_cache(*, startup: bool = False) -> None:
    """Prune stale local staging artifacts without affecting source state."""

    if not getattr(cfg, "STAGING_ENABLED", False):
        return
    if startup and not getattr(cfg, "STAGING_CLEANUP_ON_START", True):
        return
    cleanup_staging(
        cfg.STAGING_DIR,
        max_age_seconds=float(getattr(cfg, "STAGING_CLEANUP_DAYS", 7)) * 86400,
    )


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
    global LAST_SCAN_ERRORS, LAST_SCAN_SIZE_SKIPS, LAST_SCAN_SNAPSHOTS
    root = cfg.VIDEO_DIR
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"视频目录不存在或不是目录：{root}")
    videos, LAST_SCAN_ERRORS = iter_files(
        root, cfg.VIDEO_EXTENSIONS, cancel_event=cancel_event
    )
    LAST_SCAN_SIZE_SKIPS = []
    LAST_SCAN_SNAPSHOTS = {}
    accepted = []
    for path in videos:
        snapshot = file_snapshot(path)
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
                "reason": (
                    f"文件大小 {format_size(size)} 超过 Telegram 视频上限 "
                    f"{format_size(cfg.VIDEO_MAX_BYTES)}"
                ),
            })
            continue
        LAST_SCAN_SNAPSHOTS[normalize_path(path)] = snapshot
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


EXIFTOOL_BATCH_SIZE = 256
EXIFTOOL_MAX_RETRIES = 2
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


def _readiness_options() -> dict:
    """Return the shared, bounded source-file readiness configuration."""

    return {
        "attempts": int(getattr(cfg, "SCAN_READINESS_ATTEMPTS", READINESS_ATTEMPTS)),
        "stable_interval": float(getattr(cfg, "SCAN_STABILITY_INTERVAL_SECONDS", 0.05)),
        "stable_checks": int(getattr(cfg, "SCAN_STABILITY_CHECKS", 2)),
        "probe_bytes": int(getattr(cfg, "SCAN_READ_PROBE_BYTES", 64 * 1024)),
    }


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
            if cancel_event is None:
                result = subprocess.run(command, **process_kwargs)
            else:
                result = run_cancellable_process(
                    command,
                    cancel_event=cancel_event,
                    **process_kwargs,
                )
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
        complete = result.returncode == 0 and not stderr
        if complete and batch is not None:
            requested_paths = {normalize_path(path) for path in batch}
            returned_paths = {
                normalize_path(row["SourceFile"])
                for row in clean_rows
                if row.get("SourceFile")
            }
            missing = requested_paths - returned_paths
            if missing:
                complete = False
                last_error = (
                    f"ExifTool 输出不完整：批次中有 {len(missing)} 个文件未返回结果"
                )
        if complete:
            return clean_rows, None, True
        last_error = last_error or stderr or f"退出码 {result.returncode}"
        # Keep the parsed rows for a one-file batch so a warning attached to a
        # valid file does not discard otherwise useful metadata. Multi-file
        # batches are marked incomplete and isolated by ``read_exif_metadata``.
        if batch is not None and len(batch) <= 1:
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


def read_exif_metadata(paths=None, cancel_event=None) -> dict[str, dict]:
    """Read EXIF metadata without letting one bad network file abort a scan.

    ``paths=None`` is retained as a convenience for CLI callers, but it now
    performs the same Python discovery as the GUI and always supplies explicit
    file paths to ExifTool.  ExifTool itself never recursively scans a root.
    """
    global LAST_SCAN_ERRORS
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
        if (not complete or diagnostic) and batch is not None and len(batch) > 1:
            midpoint = max(1, len(batch) // 2)
            collect(batch[:midpoint], False)
            collect(batch[midpoint:], False)
            return
        if diagnostic and not rows:
            label = str(batch[0]) if batch else str(cfg.VIDEO_DIR)
            diagnostics.append(f"{label}: {diagnostic}")
        for row in rows:
            source = row.get("SourceFile")
            if source:
                index[normalize_path(source)] = row

    for batch, recursive in batches:
        if _cancel_requested(cancel_event):
            break
        collect(batch, recursive)
    if diagnostics:
        LAST_SCAN_ERRORS.extend(f"ExifTool：{message}" for message in diagnostics)
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
        if cancel_event is None:
            result = subprocess.run(command, **process_kwargs)
        else:
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


@functools.lru_cache(maxsize=4096)
def _cached_media_creation_metadata(path_text: str, size: int, mtime_ns: int):
    result = _read_media_creation_metadata(path_text, size, mtime_ns)
    if result is None:
        # Exceptions are not retained by functools.lru_cache.  This prevents
        # a temporary SMB/FFmpeg failure from becoming a permanent negative
        # cache entry for the rest of the process.
        raise _NoMediaDate
    return result


def _media_creation_metadata(path_text: str, size: int, mtime_ns: int, cancel_event=None):
    if cancel_event is not None:
        try:
            return _read_media_creation_metadata(
                path_text, size, mtime_ns, cancel_event=cancel_event
            )
        except _NoMediaDate:
            return None
    try:
        return _cached_media_creation_metadata(path_text, size, mtime_ns)
    except _NoMediaDate:
        return None


_media_creation_metadata.cache_clear = _cached_media_creation_metadata.cache_clear


def read_media_creation_time(path: Path, cancel_event=None):
    if not video_dates_enabled():
        return None
    try:
        if _cancel_requested(cancel_event):
            return None
        readiness = wait_for_file_ready(
            path,
            **_readiness_options(),
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
            "month_key": dt.strftime("%Y-%m"),
            "date_tag": selected["tag"],
            "fallback": selected["fallback"],
        }
        snapshot = LAST_SCAN_SNAPSHOTS.get(key) or file_snapshot(path)
        if snapshot is not None:
            item["scan_size"], item["scan_mtime_ns"] = snapshot
        items.append(item)
    if not force_ten_per_album():
        # ``videos`` was already ordered by the shared media path sorter.
        # Keep that order as the stable tie-breaker when capture dates match;
        # do not re-sort by basename or lexical path here.
        items.sort(
            key=lambda item: item["capture_time"].replace(tzinfo=None)
        )
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


def _probe_video_duration(path: Path, cancel_event=None) -> float:
    """Read duration through bounded ffprobe instead of frame counting."""

    ffprobe = _find_ffprobe()
    if not ffprobe:
        return 0.0
    try:
        command = [
                ffprobe,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "format=duration:stream=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                display_path(path),
            ]
        process_kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": _process_timeout("FFMPEG_INFO_TIMEOUT_SECONDS", FFMPEG_INFO_TIMEOUT_SECONDS),
            **_hidden_subprocess_kwargs(),
        }
        if cancel_event is None:
            result = subprocess.run(command, **process_kwargs)
        else:
            result = run_cancellable_process(
                command,
                cancel_event=cancel_event,
                **process_kwargs,
            )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("读取视频时长超时") from exc
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return 0.0
    if result.returncode != 0:
        return 0.0
    values = []
    for line in str(result.stdout or "").splitlines():
        try:
            value = float(line.strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    return max(values, default=0.0)


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
    reader = None
    try:
        reader = imageio_ffmpeg.read_frames(display_path(path))
        metadata = (
            _next_frame_metadata(
                reader,
                timeout=_process_timeout("FFMPEG_INFO_TIMEOUT_SECONDS", FFMPEG_INFO_TIMEOUT_SECONDS),
            )
            if cancel_event is None
            else _next_frame_metadata(
                reader,
                timeout=_process_timeout("FFMPEG_INFO_TIMEOUT_SECONDS", FFMPEG_INFO_TIMEOUT_SECONDS),
                cancel_event=cancel_event,
            )
        )
    except StopIteration as exc:
        raise RuntimeError(f"视频没有可读取的媒体流：{path}") from exc
    except TimeoutError:
        # Preserve the transient category so preflight can defer a network
        # timeout and retry it on the next run instead of marking it damaged.
        raise
    except Exception as exc:
        raise RuntimeError(
            f"无法读取视频媒体信息：{path}\n"
            f"{type(exc).__name__}: {exc}"
        ) from exc
    finally:
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass
    size = metadata.get("size") or metadata.get("source_size")
    duration = float(metadata.get("duration") or 0)
    if not size or len(size) != 2:
        raise RuntimeError(f"FFmpeg 无法读取分辨率：{path.name}")
    if duration <= 0:
        duration = (
            _probe_video_duration(path)
            if cancel_event is None
            else _probe_video_duration(path, cancel_event=cancel_event)
        )
    width, height = int(size[0]), int(size[1])
    if width <= 1 or height <= 1 or duration <= 0:
        raise RuntimeError(f"视频媒体属性异常：{path.name} | {width}x{height} | {duration:.3f}s")
    result = {"width": width, "height": height, "duration": duration}
    with _VIDEO_INFO_CACHE_LOCK:
        _VIDEO_INFO_CACHE[key] = result
    return result


def build_thumbnail(path: Path, cancel_event=None):
    THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stat = path.stat()
    cache_key = hashlib.sha1(
        f"{stable_path(path)}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
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
                    "-ss", str(second), "-i", display_path(path), "-frames:v", "1",
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
            if cancel_event is None:
                result = subprocess.run(command, **run_kwargs)
            else:
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

    if not extract(1.0) and not extract(0.0):
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
    if cfg.VIDEO_GENERATE_THUMBNAIL:
        build_thumbnail(path, cancel_event)
    return info


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
                **_readiness_options(),
                cancel_event=cancel_event,
            )
            if not readiness.ready:
                raise RuntimeError(readiness.reason or f"文件暂时不可读取：{path}")
            snapshot = readiness.snapshot.as_tuple()
            size = snapshot[0]
            if size > cfg.VIDEO_MAX_BYTES:
                raise RuntimeError(
                    f"文件大小 {format_size(size)} 超过 Telegram 视频上限 "
                    f"{format_size(cfg.VIDEO_MAX_BYTES)}"
                )
            # Preserve the historical one-argument call for integrations and
            # tests that provide a lightweight preparation hook.  The worker
            # passes cancellation only when a caller requested it.
            if cancel_event is None:
                prepare_video(path)
            else:
                prepare_video(path, cancel_event)
            return None
        except Exception as exc:
            return {
                "item": item,
                "path": path,
                "reason": f"{type(exc).__name__}: {exc}",
                "category": (
                    "size" if "超过 Telegram 视频上限" in str(exc)
                    else "deferred" if isinstance(exc, (OSError, TimeoutError))
                    or any(marker in str(exc) for marker in ("暂时不可读取", "发生变化", "SMB", "网络"))
                    else "unreadable"
                ),
            }

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
                break
            if getattr(cfg, "VIDEO_VERIFY_ALL_METADATA", False):
                target.info(f"预检视频 {index}/{total} · {items[index-1]['path'].name}")
            if result:
                skipped.append(result)
                target.warning(f"跳过无法读取的视频：{relative_name(result['path'])}")
                target.log(f"跳过视频详情：{result['path']}\n原因：{result['reason']}")

    return skipped


def report_scan_size_skips(skipped, ui=None) -> None:
    """Report files rejected during directory scanning."""

    if not skipped:
        return
    target = ui or UI
    target.warning(
        f"扫描时跳过 {len(skipped)} 个超过 Telegram 4 GiB 上限的视频；"
        "这些文件未加入上传计划。"
    )
    for record in skipped:
        target.log(
            f"扫描跳过视频：{record['path']}\n"
            f"原因：{record['reason']}"
        )


def report_skipped_videos(skipped, ui=None, *, final=False) -> None:
    if not skipped:
        return
    target = ui or UI
    prefix = "本次任务结束" if final else "视频预检完成"
    size_count = sum(record.get("category") == "size" for record in skipped)
    deferred_count = sum(record.get("category") == "deferred" for record in skipped)
    unreadable_count = len(skipped) - size_count - deferred_count
    parts = []
    if unreadable_count:
        parts.append(f"{unreadable_count} 个无法读取的视频")
    if size_count:
        parts.append(f"{size_count} 个超过 4 GiB 上限的视频")
    if deferred_count:
        parts.append(f"{deferred_count} 个暂时不可读的视频（DEFERRED）")
    target.warning(
        f"{prefix}：已跳过{'、'.join(parts) or f'{len(skipped)} 个视频'}；"
        "这些文件未写入上传断点，修复后可重新扫描上传。"
    )


def build_video_contents(items, caption: str, ui=None, cancel_event=None):
    """Build an Album while isolating files that became unreadable later."""

    target = ui or UI
    contents = []
    valid_items = []
    skipped = []
    for item in items:
        try:
            item_caption = caption if not valid_items else ""
            if cancel_event is None:
                contents.append(input_video(item, item_caption))
            else:
                contents.append(input_video(item, item_caption, cancel_event))
            valid_items.append(item)
        except Exception as exc:
            path = item["path"]
            record = {
                "item": item,
                "path": path,
                "reason": f"{type(exc).__name__}: {exc}",
                "category": (
                    "deferred" if isinstance(exc, (OSError, TimeoutError))
                    or any(marker in str(exc) for marker in ("暂时不可读取", "发生变化", "SMB", "网络"))
                    else "unreadable"
                ),
            }
            skipped.append(record)
            target.warning(f"跳过上传前变得无法读取的视频：{relative_name(path)}")
            target.log(f"跳过视频详情：{path}\n原因：{record['reason']}")
    return contents, valid_items, skipped


def input_video(item, caption: str, cancel_event=None):
    path = item["path"]
    readiness = wait_for_file_ready(
        path,
        expected_size=item.get("scan_size"),
        expected_mtime_ns=item.get("scan_mtime_ns"),
        **_readiness_options(),
        cancel_event=cancel_event,
    )
    if not readiness.ready:
        raise RuntimeError(readiness.reason or f"文件暂时不可读取：{path}")
    source_path = path
    if getattr(cfg, "STAGING_ENABLED", False):
        source_path = stage_file(
            path,
            readiness.snapshot,
            staging_dir=cfg.STAGING_DIR,
            cancel_event=cancel_event,
        )
    info = video_info(source_path)
    thumbnail = None
    if cfg.VIDEO_GENERATE_THUMBNAIL:
        if cancel_event is None:
            thumb_path, thumb_width, thumb_height = build_thumbnail(source_path)
        else:
            thumb_path, thumb_width, thumb_height = build_thumbnail(source_path, cancel_event)
        thumbnail = {
            "@type": "inputThumbnail",
            "thumbnail": {"@type": "inputFileLocal", "path": display_path(thumb_path)},
            "width": int(thumb_width),
            "height": int(thumb_height),
        }
    return {
        "@type": "inputMessageVideo",
        "video": {"@type": "inputFileLocal", "path": display_path(source_path)},
        "thumbnail": thumbnail,
        "cover": None,
        "start_timestamp": 0,
        "added_sticker_file_ids": [],
        "duration": int(max(1, round(info["duration"]))),
        "width": int(info["width"]),
        "height": int(info["height"]),
        "supports_streaming": True,
        "caption": formatted_text(caption),
        "show_caption_above_media": False,
        "self_destruct_type": None,
        "has_spoiler": False,
    }


class UploadState:
    VERSION = 1

    def __init__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        identity_suffix = (
            "tdlib-video-v5"
            if getattr(cfg, "TARGET_MODE", "forum_topic") == "forum_topic"
            else "tdlib-video-v5-channel"
        )
        identity = f"{stable_path(cfg.VIDEO_DIR)}|{cfg.CHAT_ID}|{cfg.FORUM_TOPIC_ID}|{identity_suffix}"
        task_hash = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
        self.path = STATE_DIR / f"upload_state_{task_hash}.json"
        self.lock = threading.Lock()
        if cfg.VIDEO_RESET_STATE and self.path.exists():
            self.path.unlink()
        self.data = self._load()

    def _new(self):
        return {
            "version": self.VERSION,
            "video_dir": stable_path(cfg.VIDEO_DIR),
            "chat_id": cfg.CHAT_ID,
            "target_mode": getattr(cfg, "TARGET_MODE", "forum_topic"),
            "channel_chat_id": getattr(cfg, "CHANNEL_CHAT_ID", 0),
            "forum_topic_id": cfg.FORUM_TOPIC_ID,
            "completed": {},
        }

    def _load(self):
        if not self.path.exists():
            data = self._new()
            self._save(data)
            return data
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"断点文件读取失败：{self.path}\n{exc}") from exc
        if data.get("version") != self.VERSION:
            raise RuntimeError(f"断点文件版本不兼容：{self.path}")
        return data

    def _save(self, data):
        data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, self.path)

    def is_completed(self, path: Path):
        try:
            return file_signature(path) in self.data["completed"]
        except OSError:
            return False

    def mark_album_completed(self, items, message_ids):
        with self.lock:
            for index, item in enumerate(items):
                path = item["path"]
                snapshot = _snapshot_for_path(path)
                if snapshot is None:
                    expected_size = item.get("scan_size")
                    expected_mtime_ns = item.get("scan_mtime_ns")
                    if expected_size is not None and expected_mtime_ns is not None:
                        snapshot = (int(expected_size), int(expected_mtime_ns))
                if snapshot is None:
                    raise RuntimeError(f"上传完成但无法记录视频断点：{path}")
                size, mtime_ns = snapshot
                self.data["completed"][file_signature(path, snapshot)] = {
                    "relative_path": relative_name(path),
                    "size": size,
                    "mtime_ns": mtime_ns,
                    "capture_time": (
                        item["capture_time"].isoformat()
                        if item.get("capture_time") is not None
                        else None
                    ),
                    "month_key": item["month_key"],
                    "date_tag": item["date_tag"],
                    "message_id": message_ids[index] if index < len(message_ids) else None,
                    "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            self._save(self.data)


class VideoUploadProgress:
    def __init__(self, all_items, completed_items):
        self.sizes = {
            item["path"]: int((_snapshot_for_path(item["path"]) or (0, 0))[0])
            for item in all_items
        }
        self.total_bytes = sum(self.sizes.values())
        self.total_files = len(all_items)
        self.completed_bytes = sum(self.sizes[item["path"]] for item in completed_items)
        self.completed_files = len(completed_items)
        self.current_paths = {}
        self.current_uploaded = {}
        self.file_id_to_path = {}
        self.month_key = ""
        self.album_number = 0
        self.album_total = 0
        self.samples = deque()
        self.last_draw = 0.0
        self.lock = threading.Lock()

    def begin_album(self, items, month_key, album_number, album_total):
        with self.lock:
            self.month_key = month_key
            self.album_number = album_number
            self.album_total = album_total
            self.current_paths = {normalize_path(item["path"]): item["path"] for item in items}
            self.current_uploaded = {item["path"]: 0 for item in items}
            self.file_id_to_path = {}
            self.samples.clear()
            self.last_draw = 0.0

    def skip_items(self, items):
        """Remove files that failed local preparation from progress totals."""

        with self.lock:
            for item in items:
                path = item["path"]
                size = self.sizes.pop(path, 0)
                self.total_bytes = max(0, self.total_bytes - size)
                self.total_files = max(0, self.total_files - 1)
                self.current_uploaded.pop(path, None)
        self.draw(force=True)

    @staticmethod
    def _video_file(message):
        content = message.get("content", {})
        if content.get("@type") != "messageVideo":
            return None
        return content.get("video", {}).get("video")

    def register_messages(self, messages, items):
        with self.lock:
            for message, item in zip(messages, items):
                file_obj = self._video_file(message)
                if not file_obj:
                    continue
                if file_obj.get("id") is not None:
                    self.file_id_to_path[file_obj["id"]] = item["path"]
                self._apply_unlocked(file_obj)

    def _apply_unlocked(self, file_obj):
        path = None
        local_path = file_obj.get("local", {}).get("path", "")
        if local_path:
            path = self.current_paths.get(normalize_path(local_path))
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
                kind="VIDEO",
                ratio=ratio,
                speed=speed,
                eta=eta,
                detail=self.month_key,
                album_number=self.album_number,
                album_total=self.album_total,
                done_files=self.completed_files,
                total_files=self.total_files,
                done_bytes=done,
                total_bytes=self.total_bytes,
            )
        UI.progress(**kwargs)

    def finish_album(self, items):
        with self.lock:
            self.completed_bytes += sum(self.sizes[item["path"]] for item in items)
            self.completed_files += len(items)
            self.current_paths = {}
            self.current_uploaded = {}
            self.file_id_to_path = {}
        self.draw(force=True)
        UI.finish()


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
                if state is None or not state.is_completed(item["path"])
            ]
            key_group = f"{month_key}:{album_number}" if month_key == FORCED_GROUP_KEY else month_key
            key = album_key("video", key_group, album_items)
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


def print_plan(items, state):
    groups = make_groups(items)
    print("\n" + "=" * 104)
    if force_ten_per_album():
        title_mode = "带组标题" if include_group_title() else "不带组标题"
        print(
            f"上传计划：按扫描顺序每 {cfg.VIDEO_ALBUM_SIZE} 个视频组成一组；"
            f"{title_mode}"
        )
    else:
        print(
            f"上传计划：EXIF/QuickTime 按月分组；同月每最多 {cfg.VIDEO_ALBUM_SIZE} 个组成 Album；"
            "每个 Album 只保留一个日期 Caption"
        )
    print("=" * 104)
    for month_key in sorted(groups):
        month_items = groups[month_key]
        pending = [item for item in month_items if not state.is_completed(item["path"])]
        group_label = group_display_name(month_key)
        caption = (
            "Album 1、Album 2…"
            if month_key == FORCED_GROUP_KEY and include_group_title()
            else month_caption(month_key) if include_group_title() else ""
        )
        print(f"\n[{group_label}] Caption={caption} | 共 {len(month_items)} | 待上传 {len(pending)}")
        for index, item in enumerate(month_items, 1):
            path = item["path"]
            status = "已完成" if state.is_completed(path) else "待上传"
            fallback = " [mtime兜底]" if item["fallback"] else ""
            capture_time = item.get("capture_time")
            capture_text = (
                capture_time.strftime("%Y-%m-%d %H:%M:%S")
                if capture_time is not None
                else "未读取日期"
            )
            print(
                f"  {index:>3}. [{status}] {capture_text}  "
                f"{format_size((_snapshot_for_path(path) or (0, 0))[0]):>10}  {relative_name(path)}  <{item['date_tag']}>{fallback}"
            )


def validate_config():
    if cfg.API_ID == 12345678 or cfg.API_HASH == "YOUR_API_HASH":
        raise RuntimeError("请先在 config.toml 中填写 API_ID / API_HASH。")
    if getattr(cfg, "TARGET_MODE", "forum_topic") == "channel":
        if cfg.CHAT_ID in {0, -1001234567890}:
            raise RuntimeError("请先在 config.toml 中填写频道 Chat ID。")
    elif cfg.CHAT_ID in {0, -1001234567890} or cfg.FORUM_TOPIC_ID <= 0 or cfg.FORUM_TOPIC_ID == 12345:
        raise RuntimeError("请先在 config.toml 中填写 CHAT_ID / FORUM_TOPIC_ID。")


def main():
    activate = getattr(cfg, "activate_target", None)
    if callable(activate):
        activate("video")
    validate_config()
    version = verify_tdjson_version()
    UI.log(f"tdjson / TDLib 绑定版本：{version}（已锁定）")

    cancel_event = getattr(UI, "cancel_event", None)
    cleanup_staging_cache(startup=True)
    videos = (
        scan_videos()
        if cancel_event is None
        else scan_videos(cancel_event=cancel_event)
    )
    if LAST_SCAN_SIZE_SKIPS:
        report_scan_size_skips(LAST_SCAN_SIZE_SKIPS)
    if not videos:
        UI.log("没有找到视频文件。")
        return

    if not video_dates_enabled():
        UI.log("已关闭日期读取，将按文件名扫描并按固定数量分组；不会调用 ExifTool、FFmpeg 或文件修改时间。")
        metadata_index = {}
    elif cfg.EXIFTOOL_PATH.exists():
        UI.log(
            "正在使用 ExifTool 批量读取 EXIF"
            + ("；缺少 EXIF 的视频再读取媒体创建日期..." if getattr(cfg, "VIDEO_READ_MEDIA_CREATION_DATE", True) else "...")
        )
        metadata_index = (
            read_exif_metadata(videos)
            if cancel_event is None
            else read_exif_metadata(videos, cancel_event=cancel_event)
        )
    elif getattr(cfg, "VIDEO_READ_MEDIA_CREATION_DATE", True):
        UI.warning(
            f"未找到 ExifTool：{cfg.EXIFTOOL_PATH}。"
            " EXIF 日期不可用，将仅尝试读取媒体创建日期；读取失败后按缺失日期策略处理。"
        )
        metadata_index = {}
    elif cfg.VIDEO_MISSING_DATE_POLICY == "mtime":
        UI.warning(
            f"未找到 ExifTool：{cfg.EXIFTOOL_PATH}。"
            " 缺失 EXIF 的视频将使用文件修改时间（mtime）。"
        )
        metadata_index = {}
    else:
        raise RuntimeError(
            f"找不到 ExifTool：{cfg.EXIFTOOL_PATH}\n"
            '当前未启用媒体创建日期，且 missing_date_policy="error"，必须安装 ExifTool。'
        )
    if cancel_event is None:
        items, missing = build_items(videos, metadata_index)
    else:
        items, missing = build_items(
            videos,
            metadata_index,
            cancel_event=cancel_event,
        )
    if missing and cfg.VIDEO_MISSING_DATE_POLICY == "error":
        UI.log("以下视频没有找到可用的 EXIF 或媒体创建日期：")
        for path in missing:
            UI.log(f"  {relative_name(path)}")
        UI.log('当前 missing_date_policy="error"，所以没有开始上传。')
        return

    state = UploadState()
    completed_items = [item for item in items if state.is_completed(item["path"])]
    pending_items = [item for item in items if not state.is_completed(item["path"])]
    skipped_items = []
    # Build all Album boundaries from the complete scan before preflight.  A
    # temporarily unavailable file is deferred for this run; removing it
    # from ``items`` here would incorrectly pull a later file into its Album.
    plans = build_album_plans(items, state)
    preflight_skipped_paths = set()
    if pending_items:
        UI.log(f"正在检查 {len(pending_items)} 个待上传视频的媒体数据…")
        skipped_items = (
            preflight_videos(pending_items)
            if cancel_event is None
            else preflight_videos(pending_items, cancel_event=cancel_event)
        )
        preflight_skipped_paths = {
            stable_path(record["path"]) for record in skipped_items
        }
        if preflight_skipped_paths:
            report_skipped_videos(skipped_items)
    pending_plans = [plan for plan in plans if plan["pending_items"]]
    total_albums = len(pending_plans)

    print("\n" + "=" * 82)
    print(f"视频目录：{cfg.VIDEO_DIR}")
    print(f"扫描视频：{len(videos)} | 可用日期：{len(items)} | 缺失日期：{len(missing)}")
    print(f"跳过坏视频：{len(skipped_items)}")
    print(f"断点已完成：{len(completed_items)}/{len(items)}")
    print(
        f"本次待上传：{len(pending_items)} | "
        f"{format_size(sum((_snapshot_for_path(item['path']) or (0, 0))[0] for item in pending_items))}"
    )
    print(f"本次 Album：{total_albums}")
    print(f"状态文件：{state.path}")
    print("=" * 82)

    if cfg.VIDEO_SHOW_FILE_LIST:
        print_plan(items, state)
    sendable_pending = [
        item for item in pending_items
        if stable_path(item["path"]) not in preflight_skipped_paths
    ]
    if not pending_items or not sendable_pending:
        if skipped_items:
            report_skipped_videos(skipped_items, final=True)
            print("\n没有可上传的有效视频；坏视频已跳过并记录。")
        else:
            print("\n全部视频已经在断点记录中，无需上传。")
        return

    if input("\n确认开始上传？输入 y 继续：").strip().lower() != "y":
        print("已取消。")
        return

    client = TDJsonClient(UI, "TDLib Video Album Uploader")
    progress = VideoUploadProgress(items, completed_items)
    if preflight_skipped_paths:
        progress.skip_items([
            item for item in pending_items
            if stable_path(item["path"]) in preflight_skipped_paths
        ])
    client.add_update_callback(progress.handle_update)

    try:
        client.login()
        client.set_fast_options()
        client.validate_target()
        album_global = 0

        month_plan_groups = defaultdict(list)
        for plan in pending_plans:
            month_plan_groups[plan["month_key"]].append(plan)
        for month_key in sorted(month_plan_groups):
            month_plans = month_plan_groups[month_key]
            month_items = [item for plan in month_plans for item in plan["pending_items"]]
            month_album_total = len(month_plans)
            UI.log("")
            UI.log("=" * 82)
            group_label = group_display_name(month_key)
            group_word = "分组" if month_key == FORCED_GROUP_KEY else "月份"
            UI.log(f"开始{group_word} {group_label}：{len(month_items)} 个视频，{month_album_total} 个 Album")
            UI.log("=" * 82)

            for plan in month_plans:
                album_items = [
                    item for item in plan["pending_items"]
                    if stable_path(item["path"]) not in preflight_skipped_paths
                ]
                if not album_items:
                    continue
                label = with_filename_description(
                    plan["caption"]["text"],
                    album_items,
                    getattr(cfg, "VIDEO_CAPTION_INCLUDE_FILENAMES", False),
                    include_filename_numbers(),
                )
                if cancel_event is None:
                    contents, ready_items, runtime_skipped = build_video_contents(
                        album_items,
                        label,
                    )
                else:
                    contents, ready_items, runtime_skipped = build_video_contents(
                        album_items,
                        label,
                        cancel_event=cancel_event,
                    )
                if runtime_skipped:
                    skipped_items.extend(runtime_skipped)
                    progress.skip_items([record["item"] for record in runtime_skipped])
                if not ready_items:
                    UI.warning("当前 Album 没有可读取的视频，已跳过。")
                    continue
                month_album_number = plan["number"]
                if ready_items != album_items and getattr(cfg, "VIDEO_CAPTION_INCLUDE_FILENAMES", False):
                    # Keep the already-built media inputs.  Rebuilding the
                    # Album here would start FFmpeg a second time for every
                    # surviving video and could reintroduce a transient
                    # failure after the JIT check.  Only the first content
                    # carries the caption, so update that field in place.
                    label = with_filename_description(
                        plan["caption"]["text"],
                        ready_items,
                        True,
                        include_filename_numbers(),
                    )
                    if contents:
                        contents[0]["caption"] = formatted_text(label)
                album_global += 1
                progress.begin_album(ready_items, month_key, album_global, total_albums)
                UI.log("")
                UI.log(
                    f"[总 {album_global}/{total_albums}] [{group_label} {month_album_number}/{month_album_total}] "
                    f"Album {len(ready_items)} 个 | Caption={label}"
                )
                for item in ready_items:
                    path = item["path"]
                    capture_time = item.get("capture_time")
                    capture_text = (
                        capture_time.strftime("%Y-%m-%d %H:%M:%S")
                        if capture_time is not None
                        else "未读取日期"
                    )
                    UI.log(
                        f"  {capture_text}  "
                        f"{format_size((_snapshot_for_path(path) or (0, 0))[0]):>10}  {relative_name(path)}"
                    )
                try:
                    message_ids = client.send_contents(contents, progress, ready_items)
                except Exception:
                    UI.finish()
                    UI.log("当前 Album 未写入断点。")
                    raise
                state.mark_album_completed(ready_items, message_ids)
                progress.finish_album(ready_items)
                UI.log(f"Album 发送完成，Caption={label or '无'}，断点已保存。")

        UI.log("\n" + "=" * 82)
        UI.log("全部视频上传完成。")
        report_skipped_videos(skipped_items, final=True)
        UI.log("=" * 82)
    finally:
        client.remove_update_callback(progress.handle_update)
        client.close()
        cleanup_staging_cache()
