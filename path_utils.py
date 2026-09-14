# -*- coding: utf-8 -*-
"""Path helpers that keep Windows UNC/SMB paths stable and local-only."""

from __future__ import annotations

import ntpath
import os
import re
import stat
import time
from dataclasses import dataclass
from functools import cmp_to_key
from typing import Callable, TypeVar
from pathlib import Path


READY = "READY"
DEFERRED = "DEFERRED"
UNREADABLE = "UNREADABLE"


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """A cheap identity for a regular file.

    The snapshot deliberately contains only attributes that can be obtained
    with one ``stat`` call.  It is safe to pass between the scanner, media
    probes and upload worker without keeping a file descriptor open.
    """

    path: str
    size: int
    mtime_ns: int

    def as_tuple(self) -> tuple[int, int]:
        return self.size, self.mtime_ns

    def __iter__(self):
        # Keep the object convenient for callers that previously unpacked the
        # tuple returned by ``file_snapshot``.
        yield self.size
        yield self.mtime_ns


@dataclass(frozen=True, slots=True)
class FileReadiness:
    """Result of a bounded source-file readiness check."""

    status: str
    snapshot: FileSnapshot | None = None
    reason: str = ""
    attempts: int = 0

    @property
    def ready(self) -> bool:
        return self.status == READY and self.snapshot is not None


def _snapshot_object(path) -> FileSnapshot | None:
    value = _text(path)
    try:
        info = os.stat(value)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        return None
    mtime_ns = getattr(info, "st_mtime_ns", None)
    if mtime_ns is None:
        mtime_ns = int(info.st_mtime * 1_000_000_000)
    return FileSnapshot(value, int(info.st_size), int(mtime_ns))


def _text(path) -> str:
    return os.fspath(path)


def is_network_path(path) -> bool:
    """Return whether a path is on a network share we can identify cheaply."""

    value = _text(path).replace("/", "\\")
    if is_unc_path(value):
        return True
    # A mapped drive is not written as UNC.  Windows exposes its provider
    # type through GetDriveTypeW; query it only for a drive-letter path so
    # ordinary local paths remain cheap on every other platform.
    if os.name == "nt" and len(value) >= 3 and value[1] == ":":
        try:
            import ctypes

            drive = value[:3]
            # DRIVE_REMOTE = 4.  A failed query is deliberately treated as a
            # local path because callers can still override the worker count.
            return int(ctypes.windll.kernel32.GetDriveTypeW(drive)) == 4
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    return False


def io_worker_count(root, *, local: int = 4, network: int = 2) -> int:
    """Choose a bounded worker count for local and UNC media directories."""

    limit = network if is_network_path(root) else local
    return max(1, int(limit))


def snapshot_file(path) -> FileSnapshot | None:
    """Return a structured snapshot, or ``None`` when the file is unavailable."""

    return _snapshot_object(path)


def probe_readable(path, *, snapshot: FileSnapshot | None = None, probe_bytes: int = 64 * 1024) -> None:
    """Read a small head/tail window to catch transient share/read errors.

    A media file can successfully answer ``stat`` while its contents are still
    being copied.  The probe intentionally avoids reading the complete file;
    it only verifies that the source can be opened and that representative
    bytes can be fetched from both ends.
    """

    current = snapshot or _snapshot_object(path)
    if current is None:
        raise OSError(f"文件暂时不可读取或为空：{path}")
    amount = max(1, int(probe_bytes))
    with open(current.path, "rb") as stream:
        stream.read(min(amount, current.size))
        if current.size > amount:
            stream.seek(max(0, current.size - amount))
            stream.read(min(amount, current.size))


def _cancelled(cancel_event) -> bool:
    return bool(cancel_event is not None and cancel_event.is_set())


def cancelable_sleep(seconds: float, cancel_event=None, *, quantum: float = 0.05) -> bool:
    """Sleep in short slices; return ``False`` when cancellation is requested."""

    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if _cancelled(cancel_event):
            return False
        delay = min(float(quantum), remaining)
        time.sleep(delay)
        remaining -= delay
    return not _cancelled(cancel_event)


def check_file_readiness(
    path,
    *,
    expected_size=None,
    expected_mtime_ns=None,
    stable_interval: float = 0.05,
    probe: bool = True,
    probe_bytes: int = 64 * 1024,
    cancel_event=None,
) -> FileReadiness:
    """Perform one bounded stat/read/stat readiness check."""

    first = _snapshot_object(path)
    if first is None:
        return FileReadiness(DEFERRED, reason=f"文件暂时不可读取或为空：{path}", attempts=1)
    if expected_size is not None and int(expected_size) != first.size:
        return FileReadiness(DEFERRED, first, f"文件在扫描后大小发生变化：{path}", 1)
    if expected_mtime_ns is not None and int(expected_mtime_ns) != first.mtime_ns:
        return FileReadiness(DEFERRED, first, f"文件在扫描后修改时间发生变化：{path}", 1)
    try:
        if probe:
            probe_readable(path, snapshot=first, probe_bytes=probe_bytes)
    except (OSError, ValueError) as exc:
        return FileReadiness(DEFERRED, first, f"文件读取失败：{path} · {exc}", 1)
    if stable_interval > 0:
        # Do not make a user wait the complete stability interval after a
        # cancellation request.  ``cancelable_sleep`` also keeps this path
        # easy to interrupt when a scan is running on a network share.
        if not cancelable_sleep(min(float(stable_interval), 0.5), cancel_event):
            return FileReadiness(DEFERRED, first, "文件稳定性检查被取消", 1)
    second = _snapshot_object(path)
    if second is None:
        return FileReadiness(DEFERRED, first, f"文件在检查期间变得不可读取：{path}", 1)
    if second.as_tuple() != first.as_tuple():
        return FileReadiness(DEFERRED, second, f"文件仍在写入或网络连接不稳定：{path}", 1)
    return FileReadiness(READY, second, attempts=1)


def wait_for_file_ready(
    path,
    *,
    expected_size=None,
    expected_mtime_ns=None,
    attempts: int = 3,
    initial_delay: float = 0.12,
    backoff: float = 2.0,
    stable_interval: float = 0.05,
    probe: bool = True,
    probe_bytes: int = 64 * 1024,
    cancel_event=None,
) -> FileReadiness:
    """Retry readiness checks with bounded exponential backoff."""

    limit = max(1, int(attempts))
    last = FileReadiness(DEFERRED, reason=f"文件暂时不可读取：{path}")
    for attempt in range(1, limit + 1):
        if _cancelled(cancel_event):
            return FileReadiness(DEFERRED, last.snapshot, "操作已取消", attempt - 1)
        result = check_file_readiness(
            path,
            expected_size=expected_size,
            expected_mtime_ns=expected_mtime_ns,
            stable_interval=stable_interval,
            probe=probe,
            probe_bytes=probe_bytes,
            cancel_event=cancel_event,
        )
        last = FileReadiness(result.status, result.snapshot, result.reason, attempt)
        if result.ready:
            return last
        # An expected scan snapshot changing is definitive for this run. Do
        # not spend the remaining backoff window waiting for a file that must
        # be rescanned before it can be uploaded safely.
        if any(marker in result.reason for marker in ("大小发生变化", "修改时间发生变化")):
            return last
        if attempt < limit and not cancelable_sleep(initial_delay * (float(backoff) ** (attempt - 1)), cancel_event):
            return FileReadiness(DEFERRED, result.snapshot, "操作已取消", attempt)
    return last


_T = TypeVar("_T")

_NATURAL_PART_RE = re.compile(r"(\d+)")


def _natural_parts(value) -> list[tuple[int, object]]:
    """Split text into case-folded text and integer runs."""

    parts = []
    for part in _NATURAL_PART_RE.split(str(value)):
        if not part:
            continue
        if part.isdigit():
            parts.append((1, int(part)))
        else:
            parts.append((0, part.casefold()))
    return parts


def natural_compare(left, right, *, numeric_descending: bool = True) -> int:
    """Compare names naturally while keeping numeric runs deterministic.

    Text portions remain ascending. Numeric portions are compared as integers,
    so ``x.410`` is adjacent to ``x.409`` instead of being placed beside
    ``x.41``; the default direction follows the uploader's filename ordering
    setting and puts larger sequence numbers first.
    """

    a_parts = _natural_parts(left)
    b_parts = _natural_parts(right)
    for (a_type, a_value), (b_type, b_value) in zip(a_parts, b_parts):
        if a_type != b_type:
            result = -1 if a_type < b_type else 1
        elif a_value == b_value:
            continue
        elif a_type == 1 and numeric_descending:
            result = -1 if a_value > b_value else 1
        else:
            result = -1 if a_value < b_value else 1
        return result
    if len(a_parts) == len(b_parts):
        # Numeric values such as 01 and 1 compare equal above; use the raw
        # spelling only as a stable final tie-breaker.
        a_text, b_text = str(left).casefold(), str(right).casefold()
        return (a_text > b_text) - (a_text < b_text)
    return -1 if len(a_parts) < len(b_parts) else 1


def natural_sort(values, *, numeric_descending: bool = True, key=None) -> list:
    """Return a naturally ordered copy of *values*.

    ``key`` follows ``sorted`` and is evaluated once per value, which matters
    for network paths where repeatedly formatting a path is relatively costly.
    """

    key = key or (lambda value: value)
    decorated = [(key(value), index, value) for index, value in enumerate(values)]

    def compare(left, right):
        result = natural_compare(
            left[0], right[0], numeric_descending=numeric_descending
        )
        return result or (left[1] - right[1])

    return [value for _, _, value in sorted(decorated, key=cmp_to_key(compare))]


def retry_with_backoff(
    operation: Callable[[], _T],
    *,
    attempts: int = 3,
    initial_delay: float = 0.12,
    backoff: float = 2.0,
    retry_if: Callable[[BaseException], bool] | None = None,
    cancel_event=None,
) -> _T:
    """Run a small I/O operation with bounded retry/backoff semantics."""

    limit = max(1, int(attempts))
    for attempt in range(1, limit + 1):
        if _cancelled(cancel_event):
            raise TimeoutError("操作已取消")
        try:
            return operation()
        except BaseException as exc:
            should_retry = retry_if(exc) if retry_if is not None else isinstance(
                exc, (OSError, TimeoutError)
            )
            if not should_retry or attempt >= limit:
                raise
            if not cancelable_sleep(initial_delay * (float(backoff) ** (attempt - 1)), cancel_event):
                raise TimeoutError("操作已取消") from exc
    raise RuntimeError("重试操作未返回结果")


def is_unc_path(path) -> bool:
    """Return whether *path* uses a Windows UNC or extended UNC prefix."""
    value = _text(path).replace("/", "\\")
    return value.startswith("\\\\")


def stable_path(path) -> str:
    """Return a case-insensitive, non-resolving identity for a file path.

    ``Path.resolve()`` may ask Windows to resolve a network share.  A share can
    disappear briefly while a directory is being scanned, so state keys and
    UI comparisons must not depend on that network round trip.
    """
    value = _text(path)
    if os.name == "nt":
        if is_unc_path(value):
            return ntpath.normcase(ntpath.normpath(value))
        return ntpath.normcase(ntpath.normpath(ntpath.abspath(value)))
    return os.path.normcase(os.path.normpath(os.path.abspath(value)))


def display_path(path) -> str:
    """Return a normalized path string suitable for a local-file TDLib input."""
    value = _text(path)
    if os.name == "nt" and is_unc_path(value):
        # Preserve the UNC prefix; TDLib/FFmpeg can open it directly.
        return ntpath.normpath(value)
    return str(Path(value).absolute())


def file_mtime(path, fallback: float = 0.0) -> float:
    """Read a file mtime without turning a transient share error into a crash."""
    try:
        return float(os.stat(path).st_mtime)
    except OSError:
        return fallback


def file_snapshot(path):
    """Return a lightweight identity snapshot for a regular media file."""
    snapshot = _snapshot_object(path)
    return snapshot.as_tuple() if snapshot is not None else None


def is_file_stable(path, interval: float = 0.03) -> bool:
    """Check that a file remains unchanged across two short observations."""
    result = check_file_readiness(
        path,
        stable_interval=interval,
        probe=False,
    )
    return result.ready


def revalidate_file(path, *, expected_size=None, expected_mtime_ns=None):
    """Validate a source file immediately before handing it to a media tool."""
    snapshot = file_snapshot(path)
    if snapshot is None:
        raise RuntimeError(f"文件暂时不可读取或已被删除：{path}")
    if expected_size is not None and int(expected_size) != snapshot[0]:
        raise RuntimeError(f"文件在扫描后发生变化：{path}")
    if expected_mtime_ns is not None and int(expected_mtime_ns) != snapshot[1]:
        raise RuntimeError(f"文件在扫描后发生变化：{path}")
    return snapshot


def relative_name(path, root) -> str:
    """Return a stable slash-separated path relative to *root* when possible."""
    # Keep the original spelling for UI/log output (for example ``Posts``
    # rather than the lower-cased form used by ``stable_path``), while still
    # avoiding Path.resolve() and its network-share lookup.
    path_text = ntpath.normpath(_text(path)) if os.name == "nt" else os.path.abspath(_text(path))
    root_text = ntpath.normpath(_text(root)) if os.name == "nt" else os.path.abspath(_text(root))
    try:
        relative = ntpath.relpath(path_text, root_text) if os.name == "nt" else os.path.relpath(path_text, root_text)
    except (OSError, ValueError):
        return Path(path_text).name
    if relative == ".." or relative.startswith(".." + os.sep) or relative.startswith("..\\"):
        return Path(path_text).name
    return relative.replace("\\", "/")


def _entry_suffix(name: str) -> str:
    """Return the pathlib-compatible suffix for a directory entry name."""
    dot = name.rfind(".")
    return name[dot:] if 0 < dot < len(name) - 1 else ""


def iter_files(root, extensions):
    """Walk a local or UNC tree, skipping entries unavailable to the share."""
    accepted = {str(ext).lower() for ext in extensions}
    paths = []
    errors = []

    # Keep the traversal iterative. A recursive scanner can hit Python's
    # recursion limit on exported camera/archive trees with many nested
    # folders, while os.walk handles those trees without growing the call
    # stack. ``follow_symlinks=False`` retains os.walk's default behavior.
    stack = [_text(root)]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue

                        # Avoid constructing Path objects for files that will
                        # be rejected by the extension filter. This spelling
                        # matches pathlib.Path.suffix for names such as
                        # ``photo.`` and ``.hidden`` (both have no suffix).
                        ext = _entry_suffix(entry.name)
                        if ext.lower() not in accepted:
                            continue
                        info = entry.stat()
                        if stat.S_ISREG(info.st_mode) and info.st_size > 0:
                            paths.append(Path(entry.path))
                    except OSError as error:
                        errors.append(f"{entry.path}: {error}")
        except OSError as error:
            errors.append(str(error))

    return paths, errors
