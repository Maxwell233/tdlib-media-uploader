# -*- coding: utf-8 -*-
"""Cache and temporary file maintenance service for TDLib Media Uploader GUI."""

from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Any

from ..config.paths import (
    CAPTIONS_DIR,
    HISTORY_PATH,
    IMAGE_COMPRESSION_CACHE_DIR,
    IMAGE_STATE_DIR,
    LOG_DIR,
    MIXED_STATE_DIR,
    STAGING_CACHE_DIR,
    THUMBNAIL_CACHE_DIR,
    UPLOAD_INFLIGHT_DIR,
    VIDEO_STATE_DIR,
)
from ..core.filesystem_legacy import is_link_or_junction
from .config_service import cfg
from .tools import format_size

CACHE_TARGETS: dict[str, tuple[str, Path]] = {
    "video_state": ("视频上传状态", VIDEO_STATE_DIR),
    "image_state": ("图片上传状态", IMAGE_STATE_DIR),
    "mixed_state": ("混合上传状态", MIXED_STATE_DIR),
    "thumb_cache": ("视频封面缓存", THUMBNAIL_CACHE_DIR),
    "image_compression": ("图片压缩缓存", IMAGE_COMPRESSION_CACHE_DIR),
    "video_album_captions": ("视频 Album 标题", CAPTIONS_DIR / "video.json"),
    "image_album_captions": ("图片 Album 标题", CAPTIONS_DIR / "image.json"),
    "mixed_album_captions": ("混合 Album 标题", CAPTIONS_DIR / "mixed.json"),
    "upload_inflight": ("未确认上传记录", UPLOAD_INFLIGHT_DIR),
    "staging": ("本地暂存文件", STAGING_CACHE_DIR),
    "gui_history": ("GUI 历史记录", HISTORY_PATH),
    "logs": ("运行日志", LOG_DIR),
}
ALL_CACHE_KEYS = tuple(CACHE_TARGETS)


def current_cache_targets(base_targets: dict[str, tuple[str, Path]] | None = None) -> dict[str, tuple[str, Path]]:
    """Resolve dynamic cache locations, especially configured staging."""
    if base_targets is None:
        import sys
        main_mod = sys.modules.get("tdlib_media_uploader.gui.main_window")
        if main_mod is not None and hasattr(main_mod, "CACHE_TARGETS"):
            base_targets = main_mod.CACHE_TARGETS
    targets = dict(base_targets if base_targets is not None else CACHE_TARGETS)
    targets["staging"] = ("本地暂存文件", Path(getattr(cfg, "STAGING_DIR", STAGING_CACHE_DIR)))
    return targets


def cache_usage(path: Path) -> tuple[int, int]:
    """Return file count and byte size without following a directory symlink."""
    try:
        if is_link_or_junction(path):
            return (1, path.lstat().st_size)
        if path.is_file():
            return (1, path.stat().st_size)
        if not path.is_dir():
            return (0, 0)
        count = 0
        total = 0

        stack = [os.fspath(path)]
        while stack:
            current_dir = stack.pop()
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        try:
                            if entry.name == ".marker.json":
                                continue
                            is_junction = getattr(entry, "is_junction", None)
                            if entry.is_symlink() or (
                                callable(is_junction) and is_junction()
                            ):
                                continue
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISREG(info.st_mode):
                                count += 1
                                total += info.st_size
                            elif stat.S_ISDIR(info.st_mode):
                                stack.append(entry.path)
                        except OSError:
                            continue
            except OSError:
                continue

        return (count, total)
    except OSError:
        return (0, 0)


def cache_status_text() -> str:
    """Return human-readable summary of all cache targets."""
    rows = []
    for label, path in current_cache_targets().values():
        if not (path.exists() or path.is_symlink()):
            continue
        count, total = cache_usage(path)
        rows.append(f"{label} {count} 项 · {format_size(total)}")
    return "当前应用缓存：" + (" · ".join(rows) if rows else "无")


def remove_cache_path(path: Path) -> None:
    """Clear a known cache path while keeping its directory structure."""
    path = Path(path)
    if is_link_or_junction(path):
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        else:
            path.unlink()
    elif path.is_file():
        path.unlink()
    elif path.is_dir():
        try:
            entries = list(os.scandir(path))
        except OSError:
            raise
        for entry in entries:
            child = Path(entry.path)
            try:
                junction = getattr(entry, "is_junction", None)
                if entry.is_symlink() or (callable(junction) and junction()):
                    if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                        child.rmdir()
                    else:
                        child.unlink(missing_ok=True)
                elif entry.is_file(follow_symlinks=False):
                    child.unlink(missing_ok=True)
                elif entry.is_dir(follow_symlinks=False):
                    remove_cache_path(child)
                    child.rmdir()
            except OSError:
                raise


def clear_cache(
    keys: tuple[str, ...],
    targets: dict[str, tuple[str, Path]] | None = None,
) -> tuple[list[str], list[str]]:
    """Clear specified cache components, returning lists of cleared labels and errors."""
    removed = []
    errors = []
    active_targets = targets if targets is not None else current_cache_targets()
    for key in keys:
        if key not in active_targets:
            continue
        label, path = active_targets[key]
        if not (path.exists() or path.is_symlink()):
            continue
        try:
            remove_cache_path(path)
            removed.append(label)
        except OSError as exc:
            errors.append(f"{label}：{exc}")
    return removed, errors


# Compatibility aliases
_current_cache_targets = current_cache_targets
_cache_usage = cache_usage
_cache_status_text = cache_status_text
_remove_cache_path = remove_cache_path
_clear_cache = clear_cache


__all__ = [
    "ALL_CACHE_KEYS",
    "CACHE_TARGETS",
    "_cache_status_text",
    "_cache_usage",
    "_clear_cache",
    "_current_cache_targets",
    "_remove_cache_path",
    "cache_status_text",
    "cache_usage",
    "clear_cache",
    "current_cache_targets",
    "remove_cache_path",
]
