# -*- coding: utf-8 -*-
"""Optional local staging for media read from SMB/NAS locations.

Staging is deliberately opt-in.  When enabled, the upload cores copy a
validated source to a local cache immediately before constructing a TDLib
input object.  State keys continue to use the original source path, so
enabling staging does not invalidate existing checkpoints.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from path_utils import (
    FileSnapshot,
    probe_readable,
    snapshot_file,
    stable_path,
)


def _snapshot_values(snapshot) -> tuple[int, int]:
    if isinstance(snapshot, FileSnapshot):
        return snapshot.size, snapshot.mtime_ns
    if snapshot is None:
        raise OSError("缺少文件快照")
    return int(snapshot[0]), int(snapshot[1])


def _stage_target(path: Path, snapshot, staging_dir: Path) -> Path:
    size, mtime_ns = _snapshot_values(snapshot)
    digest = hashlib.sha256(
        f"{stable_path(path)}|{size}|{mtime_ns}".encode("utf-8")
    ).hexdigest()
    suffix = path.suffix or ".media"
    return staging_dir / digest[:2] / f"{digest}{suffix}"


def stage_file(
    path: Path,
    snapshot=None,
    *,
    staging_dir: Path,
    cancel_event=None,
    buffer_size: int = 1024 * 1024,
) -> Path:
    """Copy a source file atomically into the local staging cache."""

    source = Path(path)
    source_snapshot = snapshot_file(source) if snapshot is None else snapshot
    if source_snapshot is None:
        raise OSError(f"文件暂时不可读取或已被删除：{source}")
    target = _stage_target(source, source_snapshot, Path(staging_dir))
    if target.is_file():
        try:
            if target.stat().st_size == _snapshot_values(source_snapshot)[0]:
                probe_readable(target, probe_bytes=min(buffer_size, 64 * 1024))
                return target
        except OSError:
            pass

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with source.open("rb") as input_file, temporary.open("wb") as output_file:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise TimeoutError("本地暂存已取消")
                chunk = input_file.read(max(64 * 1024, int(buffer_size)))
                if not chunk:
                    break
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        copied_size = temporary.stat().st_size
        expected_size, _ = _snapshot_values(source_snapshot)
        if copied_size != expected_size:
            raise OSError(
                f"暂存文件大小不一致：{source} ({copied_size} != {expected_size})"
            )
        # A source that changes during the copy must be retried on the next
        # upload attempt rather than silently sending a partial version.
        current = snapshot_file(source)
        if current is None or current.as_tuple() != _snapshot_values(source_snapshot):
            raise OSError(f"文件在暂存期间发生变化：{source}")
        os.replace(temporary, target)
        probe_readable(target, probe_bytes=min(buffer_size, 64 * 1024))
        return target
    finally:
        temporary.unlink(missing_ok=True)


def cleanup_staging(
    staging_dir: Path,
    *,
    max_age_seconds: float | None = None,
    remove_empty: bool = False,
) -> None:
    """Remove incomplete and optionally stale staging files.

    A running upload can still be using a staged file, so cleanup is age
    based and intended for startup/finally hooks.  Temporary ``*.tmp`` files
    are always safe to remove because ``stage_file`` writes them atomically.
    Cleanup is best effort: an offline share or a file locked by another
    process must never abort an upload.
    """

    root = Path(staging_dir)
    if not root.exists():
        return
    for item in root.rglob("*.tmp"):
        try:
            item.unlink()
        except OSError:
            continue
    if max_age_seconds is not None:
        try:
            age = max(0.0, float(max_age_seconds))
        except (TypeError, ValueError):
            age = 0.0
        cutoff = time.time() - age
        for item in root.rglob("*"):
            if not item.is_file() or item.name.endswith(".tmp"):
                continue
            try:
                if item.stat().st_mtime < cutoff:
                    item.unlink()
            except OSError:
                continue
    if remove_empty:
        for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            root.rmdir()
        except OSError:
            pass


__all__ = ["stage_file", "cleanup_staging"]
