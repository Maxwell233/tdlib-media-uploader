"""Keep generated media caches within their configured startup budgets."""

import os
from pathlib import Path
import re
from stat import S_ISREG
from time import time_ns

from ..config.paths import IMAGE_COMPRESSION_CACHE_DIR, THUMBNAIL_CACHE_DIR


def record_cache_access(path, stat):
    try:
        os.utime(path, ns=(time_ns(), stat.st_mtime_ns), follow_symlinks=False)
    except OSError:
        pass


def prune_artifact_cache(directory, max_bytes, name_pattern):
    candidates = []
    size = 0
    for path in Path(directory).glob("*.jpg"):
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


def prune_media_artifacts(config):
    """Run before workers start, so cleanup cannot evict an in-use artifact."""
    prune_artifact_cache(THUMBNAIL_CACHE_DIR, config.THUMBNAIL_CACHE_MAX_BYTES, r"[0-9a-f]{40}(?:\.tmp)?\.jpg")
    prune_artifact_cache(IMAGE_COMPRESSION_CACHE_DIR, config.IMAGE_COMPRESSION_CACHE_MAX_BYTES, r"[0-9a-f]{64}\.jpg")
