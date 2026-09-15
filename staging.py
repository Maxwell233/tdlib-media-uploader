# -*- coding: utf-8 -*-
"""Optional local staging for media read from SMB/NAS locations.

Staging is deliberately opt-in.  When enabled, the upload cores copy a
validated source to a local cache immediately before constructing a TDLib
input object.  State keys continue to use the original source path, so
enabling staging does not invalidate existing checkpoints.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from path_utils import (
    FileSnapshot,
    is_link_or_junction,
    is_network_path,
    probe_readable,
    snapshot_file,
    stable_path,
)


MARKER_NAME = ".marker.json"
MARKER_SCHEMA = 1
MARKER_APP = "tdlib-media-uploader"
_ARTIFACT_RE = re.compile(r"^[0-9a-f]{64}(?:\.[A-Za-z0-9._+-]+)?$")
# ``stage_file`` writes a temporary copy by appending ``.tmp`` to the same
# hash-based artifact name.  Keep this pattern just as strict as the final
# artifact pattern so an arbitrary user-created ``notes.tmp`` inside the
# managed directory is never treated as application-owned data.
_TEMP_ARTIFACT_RE = re.compile(r"^[0-9a-f]{64}(?:\.[A-Za-z0-9._+-]+)?\.tmp$")
_SHARD_RE = re.compile(r"^[0-9a-f]{2}$")


def _linked_component(path: Path) -> bool:
    """Return whether the supplied staging component is a link/reparse point.

    If the component does not exist yet, inspect the nearest existing parent
    as well.  That closes the gap where a configured parent is replaced by a
    symlink immediately before ``mkdir``.  We stop at the first existing
    ancestor rather than walking the whole filesystem path, so normal macOS
    aliases such as ``/var`` and ``/tmp`` are not rejected merely because the
    operating system exposes them through a system symlink.  Callers check
    every managed component independently: base/root, shard, artifact and
    temporary file.
    """

    current = Path(os.path.abspath(os.fspath(path)))
    while True:
        try:
            os.lstat(current)
        except FileNotFoundError:
            parent = current.parent
            if parent == current:
                return False
            current = parent
            continue
        except OSError:
            return True
        return is_link_or_junction(current)


def _assert_safe_component(path: Path, label: str) -> None:
    if _linked_component(path):
        message = f"{label} 不能包含符号链接或 junction：{path}"
        _log_staging_warning(message)
        raise RuntimeError(message)


def _log_staging_warning(message: str) -> None:
    """Record a link/reparse refusal without making logging a dependency."""

    try:
        from app_logging import write_app_log

        write_app_log("WARNING", message, source="staging")
    except Exception:
        # Staging must fail closed even when the log directory is unavailable.
        pass


def ensure_managed_staging_dir(staging_dir: Path, *, base_dir: Path | None = None) -> Path:
    """Create/validate the private staging root used by this application."""

    root = Path(staging_dir)
    if base_dir is not None:
        _assert_safe_component(Path(base_dir), "暂存目录基准")
    _assert_safe_component(root, "暂存目录")
    root.mkdir(parents=True, exist_ok=True)
    _assert_safe_component(root, "暂存目录")
    marker = root / MARKER_NAME
    if os.path.lexists(marker):
        _assert_safe_component(marker, "暂存目录标记")
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            raise RuntimeError(f"暂存目录标记无效：{marker}") from exc
        if not isinstance(value, dict) or value.get("application") != MARKER_APP or int(value.get("schema", 0)) != MARKER_SCHEMA:
            raise RuntimeError(f"暂存目录不是本程序管理的目录：{root}")
        return root
    payload = {"application": MARKER_APP, "schema": MARKER_SCHEMA, "created_at": time.time()}
    temporary = marker.with_suffix(marker.suffix + ".tmp")
    _assert_safe_component(temporary, "暂存目录标记")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, marker)
    _assert_safe_component(marker, "暂存目录标记")
    return root


def _is_managed_staging_dir(root: Path) -> bool:
    if _linked_component(Path(root)):
        return False
    marker = Path(root) / MARKER_NAME
    try:
        if is_link_or_junction(marker) or not marker.is_file():
            return False
        value = json.loads(marker.read_text(encoding="utf-8"))
        return (
            isinstance(value, dict)
            and value.get("application") == MARKER_APP
            and int(value.get("schema", 0)) == MARKER_SCHEMA
        )
    except (OSError, ValueError, TypeError, OverflowError):
        return False


def _safe_child(root: Path, candidate: Path) -> bool:
    """Return true only for a direct managed artifact beneath ``root``."""

    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return False
    if len(relative.parts) != 2:
        return False
    parent, name = relative.parts
    # The generated layout is root/<two-hex-shard>/<64-hex-artifact>.
    # Reject ``..`` and every other parent spelling before touching the path;
    # this keeps remove_staged_file safe even when handed an untrusted path.
    if not _SHARD_RE.fullmatch(parent) or name == MARKER_NAME:
        return False
    if _linked_component(root) or _linked_component(root / parent) or _linked_component(candidate):
        return False
    return bool(_ARTIFACT_RE.fullmatch(name))


def should_stage(path, mode: str = "off") -> bool:
    """Return whether a source should use the local staging cache."""

    normalized = str(mode or "off").strip().lower()
    if normalized == "always":
        return True
    if normalized == "network":
        return is_network_path(path)
    return False


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
    staging_base_dir: Path | None = None,
    cancel_event=None,
    buffer_size: int = 1024 * 1024,
) -> Path:
    """Copy a source file atomically into the local staging cache."""

    source = Path(path)
    root = ensure_managed_staging_dir(
        Path(staging_dir),
        base_dir=staging_base_dir,
    )
    source_snapshot = snapshot_file(source) if snapshot is None else snapshot
    if source_snapshot is None:
        raise OSError(f"文件暂时不可读取或已被删除：{source}")
    target = _stage_target(source, source_snapshot, root)
    _assert_safe_component(target, "暂存文件")
    if target.is_file():
        try:
            if target.stat().st_size == _snapshot_values(source_snapshot)[0]:
                probe_readable(target, probe_bytes=min(buffer_size, 64 * 1024))
                return target
        except OSError:
            pass

    _assert_safe_component(target.parent, "暂存分片目录")
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_component(target.parent, "暂存分片目录")
    temporary = target.with_suffix(target.suffix + ".tmp")
    _assert_safe_component(temporary, "暂存临时文件")
    try:
        temporary.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError(f"无法准备暂存临时文件：{temporary}") from exc
    try:
        # ``xb`` prevents a concurrent replacement from turning the temporary
        # path into an attacker-controlled link between unlink and open.
        with source.open("rb") as input_file, temporary.open("xb") as output_file:
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
        _assert_safe_component(target, "暂存文件")
        os.replace(temporary, target)
        probe_readable(target, probe_bytes=min(buffer_size, 64 * 1024))
        return target
    finally:
        temporary.unlink(missing_ok=True)


def cleanup_staging(
    staging_dir: Path,
    *,
    staging_base_dir: Path | None = None,
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
    if staging_base_dir is not None:
        base = Path(staging_base_dir)
        if os.path.lexists(base) and _linked_component(base):
            _log_staging_warning(f"拒绝使用包含符号链接或 junction 的暂存目录基准：{base}")
            return
    if os.path.lexists(root) and _linked_component(root):
        _log_staging_warning(f"拒绝使用包含符号链接或 junction 的暂存目录：{root}")
        return
    if not root.exists() or not _is_managed_staging_dir(root):
        return
    # Only inspect the two-level artifact layout created by stage_file. Never
    # recurse through arbitrary user directories, symlinks or junctions.
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for directory in children:
        try:
            if (
                directory.name == MARKER_NAME
                or not directory.is_dir()
            ):
                continue
            if _linked_component(directory):
                _log_staging_warning(f"跳过链接暂存分片目录：{directory}")
                continue
            for item in directory.iterdir():
                if _linked_component(item):
                    _log_staging_warning(f"跳过链接暂存文件：{item}")
                    continue
                if not item.is_file():
                    continue
                if not (
                    _TEMP_ARTIFACT_RE.fullmatch(item.name)
                    or _ARTIFACT_RE.fullmatch(item.name)
                ):
                    continue
                try:
                    if (
                        _TEMP_ARTIFACT_RE.fullmatch(item.name)
                        or max_age_seconds is None
                        or item.stat().st_mtime
                        < time.time() - max(0.0, float(max_age_seconds))
                    ):
                        item.unlink()
                except OSError:
                    continue
            if remove_empty:
                try:
                    directory.rmdir()
                except OSError:
                    pass
        except OSError:
            continue


def remove_staged_file(path) -> bool:
    """Remove a confirmed staged file without touching its source path."""

    if not path:
        return False
    candidate = Path(path)
    # A staged path is only removable when its parent carries our marker and
    # its filename matches the generated SHA-256 artifact format.
    root = candidate.parent.parent
    if not _is_managed_staging_dir(root) or not _safe_child(root, candidate):
        return False
    try:
        candidate.unlink(missing_ok=True)
        return True
    except OSError:
        return False


__all__ = [
    "stage_file", "cleanup_staging", "remove_staged_file", "should_stage",
    "ensure_managed_staging_dir", "MARKER_NAME",
]
