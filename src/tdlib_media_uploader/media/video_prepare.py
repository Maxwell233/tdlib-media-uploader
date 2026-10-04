# -*- coding: utf-8 -*-
"""Unified video preparation and lossless remuxing pipeline for Telegram Video.

Implements safe lossless stream-copy remuxing of probed media into MP4, per-group cache management under
VIDEO_PROCESSED_CACHE_DIR, decoupled thumbnail generation, and strict cancellation
and cleanup guarantees without modifying source files.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from collections.abc import Mapping
from typing import Any, Sequence
import uuid

from ..core.source_snapshot import capture_snapshot, validate_snapshots
from ..config import loader as cfg
from ..config.paths import THUMBNAIL_CACHE_DIR, VIDEO_PROCESSED_CACHE_DIR
from ..core.filesystem import stable_path
from ..core.filesystem_legacy import display_path, run_cancellable_process
from .legacy_video import build_thumbnail, format_size, video_limit_text
from .video_probe import (
    VideoMediaInfo,
    _find_ffmpeg,
    _hidden_subprocess_kwargs,
    determine_supports_streaming,
    format_unsupported_reason,
    probe_video,
)

MANIFEST_FILE_NAME = "manifest.json"


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
    output_size: int = 0
    source_signature: str | None = None
    working_path: Path | None = None


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
    """Generate deterministic internal cache signature for a video file."""
    abs_path = stable_path(Path(path))
    payload = f"{abs_path}|{size}|{mtime_ns}|{policy_version}|{target_container}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_group_manifest(group_dir: Path) -> dict[str, Any]:
    """Read the internal manifest file from a group's processed directory."""
    manifest_path = group_dir / MANIFEST_FILE_NAME
    if manifest_path.is_file():
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    if not isinstance(data.get("outputs"), dict):
                        data["outputs"] = {}
                    return data
        except Exception:
            pass
    return {"version": 1, "outputs": {}}


def _save_group_manifest(group_dir: Path, manifest: dict[str, Any]) -> None:
    """Safely persist the internal manifest for reuse after interruption."""
    try:
        group_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = group_dir / MANIFEST_FILE_NAME
        temp_path = group_dir / f"{MANIFEST_FILE_NAME}.tmp.{uuid.uuid4().hex}"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        temp_path.replace(manifest_path)
    except Exception:
        pass


def resolve_processed_filename(orig_path: Path, group_dir: Path, manifest: dict[str, Any]) -> str:
    """Determine a deterministic non-hash output filename for a remuxed video.

    Standard naming: <stem>.mp4 (e.g. clip.ts -> clip.mp4).
    Collision handling: If <stem>.mp4 is already recorded or exists on disk for a
    different source file, deterministically append original suffix / index:
    <stem><ext>.mp4, <stem><ext>_2.mp4, etc.

    Collision checks are case-insensitive across all platforms (using casefold),
    considering both manifest entries and existing disk files in group_dir,
    while allowing the same source file to stably reuse its existing target name.
    """
    stable_src = stable_path(orig_path)
    outputs = manifest.setdefault("outputs", {})

    # 1. Map manifest outputs by casefolded filename: {folded: (actual_key, entry)}
    manifest_by_fold: dict[str, tuple[str, Mapping[str, Any]]] = {}
    if isinstance(outputs, Mapping):
        for k, v in outputs.items():
            if isinstance(k, str) and isinstance(v, Mapping):
                manifest_by_fold[k.casefold()] = (k, v)

    # If this source already has an output recorded in manifest, check if it can be stably reused.
    stem_fold = orig_path.stem.casefold()
    for actual_k, entry in manifest_by_fold.values():
        if entry.get("source_path") == stable_src:
            k_fold = actual_k.casefold()
            # Stably reuse if it matches stem prefix and ends with .mp4 (no legacy hashes)
            if k_fold.startswith(stem_fold) and k_fold.endswith(".mp4"):
                return actual_k

    # 2. Reserve every existing disk entry, including hidden files and directories.
    # A source stem can legitimately contain '.tmp.' or start with '.', so these
    # names cannot be discarded when deciding whether an MP4 slot is free.
    disk_by_fold: dict[str, str] = {}
    if group_dir.is_dir():
        try:
            for p in group_dir.iterdir():
                if p.name != MANIFEST_FILE_NAME:
                    disk_by_fold[p.name.casefold()] = p.name
        except OSError as exc:
            raise RuntimeError(f"无法检查已处理视频目录，已停止分配文件名：{group_dir}") from exc

    # 3. Candidate generator:
    # Candidate 0: <stem>.mp4
    # Candidate 1: <stem><ext>.mp4
    # Candidate 2+: <stem><ext>_<idx>.mp4
    def _candidates():
        stem = orig_path.stem
        ext = orig_path.suffix.lower()
        yield f"{stem}.mp4"
        yield f"{stem}{ext}.mp4"
        idx = 2
        while True:
            yield f"{stem}{ext}_{idx}.mp4"
            idx += 1

    seen_folds: set[str] = set()
    for cand_name in _candidates():
        cand_fold = cand_name.casefold()
        if cand_fold in seen_folds:
            continue
        seen_folds.add(cand_fold)

        manifest_match = manifest_by_fold.get(cand_fold)
        disk_match = disk_by_fold.get(cand_fold)

        if manifest_match is not None:
            actual_key, entry = manifest_match
            if entry.get("source_path") == stable_src:
                return actual_key
            # Recorded for another source -> collision!
            continue

        if disk_match is not None:
            # File exists on disk but manifest does not confirm it belongs to this source.
            # Avoid unlinking or overwriting this file -> collision!
            continue

        # Neither manifest nor disk has this slot -> free!
        return cand_name

    return f"{orig_path.stem}.mp4"


def _valid_stream_copy(info: VideoMediaInfo, expected_info: VideoMediaInfo | None = None) -> bool:
    """Require a readable MP4 with unchanged selected video and audio codecs."""
    return (
        info.container == "mp4"
        and info.has_video_stream
        and info.video_codec not in {"", "unknown"}
        and info.width > 1
        and info.height > 1
        and info.duration > 0
        and (expected_info is None or (
            info.video_codec == expected_info.video_codec
            and info.audio_codec == expected_info.audio_codec
        ))
    )


def remux_video_lossless(
    source_path: Path,
    target_path: Path,
    cancel_event=None,
    timeout: float = 300.0,
    expected_info: VideoMediaInfo | None = None,
) -> Path:
    """Losslessly remux a probed video to MP4 using stream copy.

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
        if not _valid_stream_copy(verified_info, expected_info):
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
    is_premium: bool | None = None,
    info: Any | None = None,
    config=None,
) -> PreparedVideo:
    """Prepare a video file for Telegram upload.

    Handles native compatible videos and other probed stream-copy candidates.
    For native videos:
    - Directly uses working/source path without creating copies in processed cache.
    - Decoupled thumbnail generated directly from source file.
    For remux candidates:
    - Remuxes losslessly to temporary MP4 under VIDEO_PROCESSED_CACHE_DIR / <group_key>/
    - Output filename preserves source stem (<stem>.mp4) or appends suffix on collision.
    - Interrupted / repeated runs reuse existing validated MP4 if source unchanged.
    - Decouples thumbnail identity to original source file.
    - Validates processed output metadata and size against effective limit.
    Source files are never modified.
    """
    settings = cfg if config is None else config
    w_path = Path(working_path)
    if not w_path.is_file():
        raise RuntimeError(f"视频文件不存在：{w_path}")

    orig_path = Path(original_path) if original_path is not None else w_path
    working_snapshot = capture_snapshot(w_path)
    original_snapshot = capture_snapshot(orig_path) if orig_path.is_file() else working_snapshot
    source_snapshots = (working_snapshot, original_snapshot)
    if info is None:
        info = probe_video(w_path, cancel_event=cancel_event)
    probed_snapshot = getattr(info, "source_snapshot", None)
    if probed_snapshot is not None:
        validate_snapshots(((w_path, *probed_snapshot),))

    compat = getattr(info, "compatibility", None)
    if compat is None and isinstance(info, Mapping):
        compat = info.get("compatibility", "native")
    compat = str(compat or "native").lower()

    if compat in {"invalid", "unsupported"}:
        raise RuntimeError(format_unsupported_reason(orig_path, info))

    if max_bytes is not None:
        effective_max = max_bytes
    elif is_premium is True:
        effective_max = getattr(settings, "VIDEO_PREMIUM_MAX_BYTES", 8000 * 524_288)
    elif is_premium is False:
        effective_max = getattr(settings, "VIDEO_STANDARD_MAX_BYTES", 4000 * 524_288)
    else:
        effective_max = getattr(settings, "VIDEO_MAX_BYTES", 4000 * 524_288)

    # 1. Native / legacy format (MP4, MOV, M4V)
    if compat in {"native", "legacy"}:
        size = w_path.stat().st_size
        if size > effective_max:
            from .legacy_video import format_size, video_limit_text
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

        validate_snapshots(source_snapshots)
        source_sig = get_video_process_cache_key(orig_path, original_snapshot[1], original_snapshot[2])

        return PreparedVideo(
            source_path=orig_path,
            upload_path=w_path,
            info=info,
            thumbnail_path=thumb_path,
            thumbnail_width=tw,
            thumbnail_height=th,
            is_remuxed=False,
            cache_key=None,
            output_size=size,
            source_signature=source_sig,
            working_path=w_path,
        )

    # 2. Remux candidate, including an MP4 with a non-native codec.
    if info.compatibility == "remux":
        policy = str(
            getattr(settings, "VIDEO_COMPATIBILITY_POLICY", getattr(settings, "VIDEO_TRANSCODE_POLICY", "remux"))
        ).strip().lower()
        if policy == "original":
            raise RuntimeError(
                f"视频格式为 {info.container.upper()}，当前策略配置为 original（仅允许原生格式），已跳过：{orig_path.name}"
            )

        validate_snapshots(source_snapshots)
        source_size, source_mtime = original_snapshot[1:]
        cache_key = get_video_process_cache_key(orig_path, source_size, source_mtime)
        safe_group = safe_group_key(group_key)
        group_dir = VIDEO_PROCESSED_CACHE_DIR / safe_group
        group_dir.mkdir(parents=True, exist_ok=True)

        manifest = _load_group_manifest(group_dir)
        target_filename = resolve_processed_filename(orig_path, group_dir, manifest)
        final_mp4 = group_dir / target_filename

        processed_path: Path
        processed_info: VideoMediaInfo
        can_reuse = False

        if final_mp4.is_file() and final_mp4.stat().st_size > 0:
            outputs = manifest.get("outputs", {})
            entry = outputs.get(target_filename)
            if entry is None and isinstance(outputs, Mapping):
                tf_fold = target_filename.casefold()
                for k, v in outputs.items():
                    if isinstance(k, str) and k.casefold() == tf_fold:
                        entry = v
                        break
            if (
                entry is not None
                and entry.get("source_path") == stable_path(orig_path)
                and entry.get("size") == source_size
                and entry.get("mtime_ns") == source_mtime
                and entry.get("policy") == "remux-v2"
            ):
                try:
                    cached_info = probe_video(final_mp4, cancel_event=cancel_event)
                    if _valid_stream_copy(cached_info, info):
                        processed_path = final_mp4
                        processed_info = cached_info
                        can_reuse = True
                except Exception:
                    pass

        if not can_reuse:
            final_mp4.unlink(missing_ok=True)
            processed_path = remux_video_lossless(
                w_path, final_mp4, cancel_event=cancel_event, expected_info=info,
            )
            processed_info = probe_video(processed_path, cancel_event=cancel_event)
            validate_snapshots(source_snapshots)
            outputs = manifest.setdefault("outputs", {})
            tf_fold = target_filename.casefold()
            stale_keys = [
                k for k in outputs.keys()
                if isinstance(k, str) and k.casefold() == tf_fold and k != target_filename
            ]
            for sk in stale_keys:
                del outputs[sk]
            outputs[target_filename] = {
                "source_path": stable_path(orig_path),
                "size": source_size,
                "mtime_ns": source_mtime,
                "policy": "remux-v2",
            }
            _save_group_manifest(group_dir, manifest)

        p_size = processed_path.stat().st_size
        if p_size > effective_max:
            from .legacy_video import format_size, video_limit_text
            raise RuntimeError(
                f"重新封装后的文件大小 {format_size(p_size)} 超过 Telegram 视频上限 "
                f"{video_limit_text(effective_max)}"
            )

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

        validate_snapshots(source_snapshots)
        return PreparedVideo(
            source_path=orig_path,
            upload_path=processed_path,
            info=processed_info,
            thumbnail_path=thumb_path,
            thumbnail_width=tw,
            thumbnail_height=th,
            is_remuxed=True,
            cache_key=cache_key,
            output_size=p_size,
            source_signature=cache_key,
            working_path=w_path,
        )

    # Any other status is unsupported
    raise RuntimeError(format_unsupported_reason(orig_path, info))


def cleanup_processed_group(group_key: str, managed_paths: Sequence[Path] | None = None) -> None:
    """Clean up processed videos for a confirmed group.

    Deletes the group directory under VIDEO_PROCESSED_CACHE_DIR and unlinks any
    specified managed paths that are strictly contained within VIDEO_PROCESSED_CACHE_DIR.
    Source files, thumbnail cache, and other directories are strictly preserved.
    """
    root = VIDEO_PROCESSED_CACHE_DIR.resolve()
    safe_group = safe_group_key(group_key)
    group_dir = (VIDEO_PROCESSED_CACHE_DIR / safe_group).resolve()

    try:
        group_dir.relative_to(root)
        if group_dir != root and group_dir.is_dir():
            shutil.rmtree(group_dir, ignore_errors=True)
    except (ValueError, OSError):
        pass

    if managed_paths:
        for p in managed_paths:
            try:
                candidate = Path(p).resolve()
                candidate.relative_to(root)
                if candidate != root and candidate.is_file():
                    candidate.unlink(missing_ok=True)
            except (ValueError, OSError):
                pass


def clear_all_processed_videos() -> int:
    """Clear all processed videos in VIDEO_PROCESSED_CACHE_DIR.

    Returns the number of deleted files.
    """
    root = VIDEO_PROCESSED_CACHE_DIR.resolve()
    if not root.is_dir():
        return 0
    count = 0
    for dirpath, dirs, files in os.walk(root, topdown=False):
        d_p = Path(dirpath).resolve()
        try:
            d_p.relative_to(root)
        except ValueError:
            continue
        for f in files:
            p = d_p / f
            try:
                p.relative_to(root)
                if p != root:
                    p.unlink(missing_ok=True)
                    count += 1
            except (ValueError, OSError):
                pass
        for d in dirs:
            sub = d_p / d
            try:
                sub.relative_to(root)
                if sub != root:
                    sub.rmdir()
            except (ValueError, OSError):
                pass
    return count


def cleanup_stale_processed_videos(max_age_seconds: float = 86400 * 7) -> int:
    """Remove orphaned processed video files older than max_age_seconds."""
    root = VIDEO_PROCESSED_CACHE_DIR.resolve()
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for dirpath, dirs, files in os.walk(root, topdown=False):
        d_p = Path(dirpath).resolve()
        try:
            d_p.relative_to(root)
        except ValueError:
            continue
        for f in files:
            p = d_p / f
            try:
                p.relative_to(root)
                if p != root and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    removed += 1
            except (ValueError, OSError):
                pass
        for d in dirs:
            sub = d_p / d
            try:
                sub.relative_to(root)
                if sub != root:
                    sub.rmdir()
            except (ValueError, OSError):
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
    "resolve_processed_filename",
    "safe_group_key",
]
