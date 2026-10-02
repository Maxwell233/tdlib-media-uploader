# -*- coding: utf-8 -*-
"""Unified video preparation and lossless remuxing pipeline for Telegram Video.

Implements safe lossless stream-copy remuxing (MKV/AVI/TS/MTS/M2TS -> MP4) for
compatible H.264/HEVC and AAC/MP3 streams, per-group cache management under
VIDEO_PROCESSED_CACHE_DIR, decoupled thumbnail generation, and strict cancellation
and cleanup guarantees without modifying source files.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from collections.abc import Mapping
from typing import Any, Sequence
import uuid

from ..config import loader as cfg
from ..config.paths import THUMBNAIL_CACHE_DIR, VIDEO_PROCESSED_CACHE_DIR
from ..core.filesystem import stable_path
from ..core.filesystem_legacy import display_path, run_cancellable_process
from .video_probe import (
    VideoMediaInfo,
    _find_ffmpeg,
    _hidden_subprocess_kwargs,
    determine_supports_streaming,
    format_unsupported_reason,
    probe_video,
)


@dataclass(frozen=True)
class PreparedVideo:
    """Result of video preparation containing source, upload path, metadata, and thumbnail."""

    source_path: Path
    upload_path: Path
    info: VideoMediaInfo
    thumbnail_path: Path | None = None
    thumbnail_width: int = 0
    thumbnail_height: int = 0
    is_remuxed: bool = False
    cache_key: str | None = None


def safe_group_key(group_key: str) -> str:
    """Sanitize group/album key into a safe filesystem folder name."""
    raw = str(group_key or "").strip()
    if not raw:
        return "default"
    # Replace unsafe characters
    safe = re.sub(r'[^a-zA-Z0-9._-]+', '_', raw).strip('._')
    if not safe:
        safe = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    elif len(safe) > 48:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        safe = f"{safe[:32]}_{digest}"
    return safe


def get_video_process_cache_key(
    path: Path | str,
    size: int,
    mtime_ns: int,
    policy_version: str = "v2",
    target_container: str = "mp4",
) -> str:
    """Generate deterministic cache key for a processed video file."""
    abs_path = stable_path(Path(path))
    payload = f"{abs_path}|{size}|{mtime_ns}|{policy_version}|{target_container}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def remux_video_lossless(
    source_path: Path,
    target_path: Path,
    cancel_event=None,
    timeout: float = 300.0,
) -> Path:
    """Losslessly remux supported video container to MP4 using stream copy.

    Invokes:
        ffmpeg -y -v error -i <source> -map 0:v:0 -map 0:a:0? -c copy -movflags +faststart <temp>
    Outputs to temporary file first, verifies output, and atomically renames.
    Any temporary file is deleted immediately on error or cancellation.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise TimeoutError("视频重新封装已取消")

    target_dir = target_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    temp_path = target_dir / f"{target_path.stem}.tmp.{uuid.uuid4().hex}.mp4"

    ffmpeg_exe = _find_ffmpeg()
    if not ffmpeg_exe:
        raise RuntimeError("未找到 FFmpeg 可执行文件，无法进行视频无损重新封装")

    command = [
        ffmpeg_exe,
        "-y",
        "-v",
        "error",
        "-i",
        display_path(source_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        display_path(temp_path),
    ]

    try:
        result = run_cancellable_process(
            command,
            cancel_event=cancel_event,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_hidden_subprocess_kwargs(),
        )
    except TimeoutError:
        temp_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        if cancel_event is not None and cancel_event.is_set():
            raise TimeoutError("视频重新封装已取消") from exc
        raise RuntimeError(f"FFmpeg 重新封装失败：{source_path.name}\n{exc}") from exc

    if result.returncode != 0:
        err = result.stderr.strip() or f"FFmpeg 退出码 {result.returncode}"
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg 重新封装失败：{source_path.name}\n{err}")

    if not temp_path.is_file() or temp_path.stat().st_size <= 0:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg 重新封装未生成有效文件：{source_path.name}")

    # Validate generated MP4 before publishing
    try:
        verified_info = probe_video(temp_path, cancel_event=cancel_event)
        if (
            verified_info.compatibility not in {"native", "legacy"}
            or not verified_info.has_video_stream
            or verified_info.width <= 1
            or verified_info.height <= 1
        ):
            temp_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"重新封装后的 MP4 验证失败（兼容性：{verified_info.compatibility}）：{source_path.name}"
            )
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    # Atomically move to target path
    temp_path.replace(target_path)
    return target_path


def prepare_video_for_telegram(
    working_path: Path | str,
    original_path: Path | str | None = None,
    group_key: str = "default",
    cancel_event=None,
    *,
    generate_thumbnail: bool = True,
    thumbnail_timestamp_seconds: float | None = None,
    max_bytes: int | None = None,
    info: Any | None = None,
) -> PreparedVideo:
    """Prepare a video file for Telegram upload.

    Handles native videos (MP4/MOV/M4V) and remux candidates (MKV/AVI/TS/MTS/M2TS).
    For remux candidates:
    - Remuxes losslessly to temporary MP4 under VIDEO_PROCESSED_CACHE_DIR / <group_key>/
    - Decouples thumbnail identity to original source file
    - Validates processed output metadata and size
    Source files are never modified.
    """
    w_path = Path(working_path)
    if not w_path.is_file():
        raise RuntimeError(f"视频文件不存在：{w_path}")

    orig_path = Path(original_path) if original_path is not None else w_path
    if info is None:
        info = probe_video(w_path, cancel_event=cancel_event)

    compat = getattr(info, "compatibility", None)
    if compat is None and isinstance(info, Mapping):
        compat = info.get("compatibility", "native")
    compat = str(compat or "native").lower()

    if compat in {"invalid", "unsupported"}:
        raise RuntimeError(format_unsupported_reason(orig_path, info))

    effective_max = max_bytes if max_bytes is not None else getattr(cfg, "VIDEO_MAX_BYTES", 2000 * 1024 * 1024)

    # 1. Native / legacy format
    if compat in {"native", "legacy"}:
        size = w_path.stat().st_size
        if size > effective_max:
            from .tools import format_size, video_limit_text
            raise RuntimeError(
                f"文件大小 {format_size(size)} 超过 Telegram 视频上限 "
                f"{video_limit_text(effective_max)}"
            )

        thumb_path, tw, th = None, 0, 0
        if generate_thumbnail:
            from .legacy_video import build_thumbnail
            dur = getattr(info, "duration", None)
            if dur is None and isinstance(info, Mapping):
                dur = info.get("duration")
            thumb_kwargs = {
                "timestamp_seconds": thumbnail_timestamp_seconds,
                "duration": dur,
            }
            if orig_path != w_path:
                thumb_kwargs["logical_path"] = orig_path
            if cancel_event is not None:
                thumb_path, tw, th = build_thumbnail(
                    w_path,
                    cancel_event,
                    **thumb_kwargs,
                )
            else:
                thumb_path, tw, th = build_thumbnail(
                    w_path,
                    **thumb_kwargs,
                )

        return PreparedVideo(
            source_path=orig_path,
            upload_path=w_path,
            info=info,
            thumbnail_path=thumb_path,
            thumbnail_width=tw,
            thumbnail_height=th,
            is_remuxed=False,
            cache_key=None,
        )

    # 2. Remux candidate
    if info.compatibility == "remux":
        policy = str(
            getattr(cfg, "VIDEO_COMPATIBILITY_POLICY", getattr(cfg, "VIDEO_TRANSCODE_POLICY", "remux"))
        ).strip().lower()
        if policy == "original":
            raise RuntimeError(
                f"视频格式为 {info.container.upper()}，当前策略配置为 original（仅允许原生格式），已跳过：{orig_path.name}"
            )

        # Check source exists
        if not orig_path.is_file():
            st = w_path.stat()
        else:
            st = orig_path.stat()

        cache_key = get_video_process_cache_key(orig_path, st.st_size, st.st_mtime_ns)
        safe_group = safe_group_key(group_key)
        group_dir = VIDEO_PROCESSED_CACHE_DIR / safe_group
        group_dir.mkdir(parents=True, exist_ok=True)
        final_mp4 = group_dir / f"{cache_key}.mp4"

        # Check existing cached remux output
        processed_path: Path
        processed_info: VideoMediaInfo
        if final_mp4.is_file() and final_mp4.stat().st_size > 0:
            try:
                cached_info = probe_video(final_mp4, cancel_event=cancel_event)
                if cached_info.compatibility in {"native", "legacy"} and cached_info.has_video_stream:
                    processed_path = final_mp4
                    processed_info = cached_info
                else:
                    final_mp4.unlink(missing_ok=True)
                    processed_path = remux_video_lossless(w_path, final_mp4, cancel_event=cancel_event)
                    processed_info = probe_video(processed_path, cancel_event=cancel_event)
            except Exception:
                final_mp4.unlink(missing_ok=True)
                processed_path = remux_video_lossless(w_path, final_mp4, cancel_event=cancel_event)
                processed_info = probe_video(processed_path, cancel_event=cancel_event)
        else:
            processed_path = remux_video_lossless(w_path, final_mp4, cancel_event=cancel_event)
            processed_info = probe_video(processed_path, cancel_event=cancel_event)

        # Verify remuxed file size
        p_size = processed_path.stat().st_size
        if p_size > effective_max:
            from .tools import format_size, video_limit_text
            raise RuntimeError(
                f"重新封装后的文件大小 {format_size(p_size)} 超过 Telegram 视频上限 "
                f"{video_limit_text(effective_max)}"
            )

        # Decoupled thumbnail: extract from processed MP4, cache under original source identity
        thumb_path, tw, th = None, 0, 0
        if generate_thumbnail:
            from .legacy_video import build_thumbnail
            thumb_kwargs = {
                "timestamp_seconds": thumbnail_timestamp_seconds,
                "duration": processed_info.duration,
                "logical_path": orig_path,
            }
            if cancel_event is not None:
                thumb_path, tw, th = build_thumbnail(
                    processed_path,
                    cancel_event,
                    **thumb_kwargs,
                )
            else:
                thumb_path, tw, th = build_thumbnail(
                    processed_path,
                    **thumb_kwargs,
                )

        return PreparedVideo(
            source_path=orig_path,
            upload_path=processed_path,
            info=processed_info,
            thumbnail_path=thumb_path,
            thumbnail_width=tw,
            thumbnail_height=th,
            is_remuxed=True,
            cache_key=cache_key,
        )

    # Any other status is unsupported
    raise RuntimeError(format_unsupported_reason(orig_path, info))


def cleanup_processed_group(group_key: str, managed_paths: Sequence[Path] | None = None) -> None:
    """Clean up processed videos for a confirmed group.

    Deletes the group directory under VIDEO_PROCESSED_CACHE_DIR and unlinks any
    specified managed paths. Preserves thumbnail cache intact.
    """
    safe_group = safe_group_key(group_key)
    group_dir = VIDEO_PROCESSED_CACHE_DIR / safe_group
    if group_dir.is_dir():
        try:
            shutil.rmtree(group_dir, ignore_errors=True)
        except OSError:
            pass
    if managed_paths:
        for p in managed_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass


def clear_all_processed_videos() -> int:
    """Clear all processed videos in VIDEO_PROCESSED_CACHE_DIR.

    Returns the number of deleted files.
    """
    if not VIDEO_PROCESSED_CACHE_DIR.is_dir():
        return 0
    count = 0
    for root, dirs, files in os.walk(VIDEO_PROCESSED_CACHE_DIR, topdown=False):
        for f in files:
            try:
                (Path(root) / f).unlink(missing_ok=True)
                count += 1
            except OSError:
                pass
        for d in dirs:
            try:
                (Path(root) / d).rmdir()
            except OSError:
                pass
    return count


def cleanup_stale_processed_videos(max_age_seconds: float = 86400 * 7) -> int:
    """Remove orphaned processed video files older than max_age_seconds."""
    if not VIDEO_PROCESSED_CACHE_DIR.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for root, dirs, files in os.walk(VIDEO_PROCESSED_CACHE_DIR, topdown=False):
        for f in files:
            p = Path(root) / f
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                pass
        for d in dirs:
            p = Path(root) / d
            try:
                p.rmdir()
            except OSError:
                pass
    return removed


__all__ = [
    "PreparedVideo",
    "cleanup_processed_group",
    "cleanup_stale_processed_videos",
    "clear_all_processed_videos",
    "get_video_process_cache_key",
    "prepare_video_for_telegram",
    "remux_video_lossless",
    "safe_group_key",
]
