"""Keep generated media caches within their configured budgets."""

import os
from pathlib import Path
import re
from stat import S_ISREG
from time import time_ns

from ..config.paths import IMAGE_COMPRESSION_CACHE_DIR, THUMBNAIL_CACHE_DIR
from ..core.filesystem import is_link_or_junction
from ..core.uploader_database import prune_media_metadata


def record_cache_access(path, stat):
    try:
        os.utime(path, ns=(time_ns(), stat.st_mtime_ns), follow_symlinks=False)
    except (OSError, NotImplementedError):
        pass


def prune_artifact_cache(directory, max_bytes, name_pattern):
    directory = Path(directory)
    if is_link_or_junction(directory):
        return
    candidates = []
    size = 0
    for path in directory.glob("*.jpg"):
        if not re.fullmatch(name_pattern, path.name):
            continue
        try:
            stat = path.stat(follow_symlinks=False)
        except OSError:
            continue
        if S_ISREG(stat.st_mode):
            size += stat.st_size
            candidates.append((max(stat.st_mtime_ns, stat.st_atime_ns), path, stat.st_size))
    if size <= max_bytes:
        return
    for _last_access, path, file_bytes in sorted(candidates):
        if size <= max_bytes:
            break
        try:
            path.unlink()
        except OSError:
            continue
        size -= file_bytes


def prune_media_artifacts(config, *, startup=False):
    """Enforce artifact budgets at startup and after a settled upload."""
    if startup:
        prune_media_metadata(config)
    thumbnail_pattern = r"[0-9a-f]{40}(?:\.tmp)?\.jpg" if startup else r"[0-9a-f]{40}\.jpg"
    for directory, limit, pattern in (
        (THUMBNAIL_CACHE_DIR, getattr(config, "THUMBNAIL_CACHE_MAX_BYTES", None), thumbnail_pattern),
        (IMAGE_COMPRESSION_CACHE_DIR, getattr(config, "IMAGE_COMPRESSION_CACHE_MAX_BYTES", None), r"[0-9a-f]{64}\.jpg"),
    ):
        if limit is not None:
            prune_artifact_cache(directory, limit, pattern)
