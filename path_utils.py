# -*- coding: utf-8 -*-
"""Path helpers that keep Windows UNC/SMB paths stable and local-only."""

from __future__ import annotations

import ntpath
import os
import re
import stat
import subprocess
import sys
import time
from collections import deque
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
    # macOS mounts SMB/NFS shares under /Volumes and /Network.  Linux/BSD
    # systems commonly expose NFS/SMB through /mnt, /net or /run/mount.  The
    # prefix check is intentionally conservative and only lowers concurrency;
    # it never changes path semantics or rejects a local disk mounted there.
    posix_value = _text(path).replace("\\", "/")
    if not posix_value.startswith("/"):
        return False
    prefixes = (
        ("/Volumes", "/Network", "/net", "/mnt", "/run/mount")
        if sys.platform == "darwin"
        else ("/net", "/mnt", "/run/mount", "/Volumes", "/Network")
    )
    for prefix in prefixes:
        if posix_value == prefix or posix_value.startswith(prefix + "/"):
            return True
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
    stable_checks: int = 2,
    probe: bool = True,
    probe_bytes: int = 64 * 1024,
    cancel_event=None,
) -> FileReadiness:
    """Perform a bounded stat/read/stat readiness check.

    ``stable_checks`` is the number of observations that must agree.  The
    default keeps the historical two-observation check while allowing callers
    scanning a busy network share to require a few additional confirmations.
    """

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
    checks = max(1, int(stable_checks))
    current = first
    for _index in range(1, checks):
        # Do not make a user wait the complete stability interval after a
        # cancellation request. ``cancelable_sleep`` also keeps this path
        # easy to interrupt when a scan is running on a network share.
        if stable_interval > 0 and not cancelable_sleep(
            min(float(stable_interval), 0.5), cancel_event
        ):
            return FileReadiness(DEFERRED, current, "文件稳定性检查被取消", 1)
        observed = _snapshot_object(path)
        if observed is None:
            return FileReadiness(DEFERRED, current, f"文件在检查期间变得不可读取：{path}", 1)
        if observed.as_tuple() != current.as_tuple():
            return FileReadiness(DEFERRED, observed, f"文件仍在写入或网络连接不稳定：{path}", 1)
        current = observed
    return FileReadiness(READY, current, attempts=1)


def wait_for_file_ready(
    path,
    *,
    expected_size=None,
    expected_mtime_ns=None,
    attempts: int = 3,
    initial_delay: float = 0.12,
    backoff: float = 2.0,
    stable_interval: float = 0.05,
    stable_checks: int = 2,
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
            stable_checks=stable_checks,
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


def natural_compare(left, right) -> int:
    """Compare names using case-insensitive text and ascending integer runs.

    Consecutive digits are compared as integers, so ``x.1``, ``x.10`` and
    ``x.100`` keep their natural numeric order instead of lexical order.
    """

    a_parts = _natural_parts(left)
    b_parts = _natural_parts(right)
    for (a_type, a_value), (b_type, b_value) in zip(a_parts, b_parts):
        if a_type != b_type:
            result = -1 if a_type < b_type else 1
        elif a_value == b_value:
            continue
        else:
            result = -1 if a_value < b_value else 1
        return result
    if len(a_parts) == len(b_parts):
        # Numeric values such as 01 and 1 compare equal above; use the raw
        # spelling only as a stable final tie-breaker.
        a_text, b_text = str(left).casefold(), str(right).casefold()
        return (a_text > b_text) - (a_text < b_text)
    return -1 if len(a_parts) < len(b_parts) else 1


def natural_sort(values, *, key=None) -> list:
    """Return a naturally ordered copy of *values*.

    ``key`` follows ``sorted`` and is evaluated once per value, which matters
    for network paths where repeatedly formatting a path is relatively costly.
    """

    key = key or (lambda value: value)
    decorated = [(key(value), index, value) for index, value in enumerate(values)]

    def compare(left, right):
        result = natural_compare(left[0], right[0])
        return result or (left[1] - right[1])

    return [value for _, _, value in sorted(decorated, key=cmp_to_key(compare))]


def _relative_components(path, root=None) -> tuple[str, ...]:
    """Return normalized relative path components for deterministic sorting."""

    if root is None:
        value = _text(path)
    else:
        value = relative_name(path, root)
    value = value.replace("\\", "/")
    # Drive/UNC prefixes are not expected after relative_name(), but keeping
    # non-empty components makes this helper safe for callers that omit root.
    return tuple(component for component in value.split("/") if component not in {"", "."})


def relative_path_compare(left, right, root=None) -> int:
    """Compare two paths component by component using the natural comparator.

    Directory components are compared before their descendants.  This is the
    shared ordering contract for video, image, mixed and GUI fallback scans:
    text runs sort case-insensitively ascending and numeric runs sort as
    integers ascending.
    """

    left_parts = _relative_components(left, root)
    right_parts = _relative_components(right, root)
    for left_part, right_part in zip(left_parts, right_parts):
        result = natural_compare(left_part, right_part)
        if result:
            return result
    if len(left_parts) != len(right_parts):
        return -1 if len(left_parts) < len(right_parts) else 1
    # Equal primary components (case or leading-zero variants) are resolved
    # by the original relative spelling so the result is deterministic while
    # preserving natural numeric ordering as the primary rule.
    left_raw = "/".join(left_parts).casefold()
    right_raw = "/".join(right_parts).casefold()
    return (left_raw > right_raw) - (left_raw < right_raw)


def media_path_sort(
    values,
    root,
    *,
    mode: str = "name",
    path_key=None,
    mtime_key=None,
) -> list:
    """Sort media values with one shared relative-path/mtime implementation.

    ``mode="name"`` compares every relative directory and filename component
    with :func:`natural_compare`.  ``mode="mtime"`` keeps the historical
    oldest-first order and uses the exact same relative-path comparator for
    ties.  ``path_key`` and ``mtime_key`` support dict-backed mixed items.
    """

    normalized_mode = str(mode).strip().lower()
    # ``path`` is the historical image configuration value.  Treat it as
    # the shared name mode so older callers reach exactly the same comparator.
    if normalized_mode == "path":
        normalized_mode = "name"
    if normalized_mode not in {"name", "mtime"}:
        raise ValueError(f"不支持的媒体排序方式：{mode}")
    path_key = path_key or (lambda value: value)
    decorated = []
    for index, value in enumerate(values):
        path = path_key(value)
        mtime = None
        if normalized_mode == "mtime":
            try:
                raw_mtime = mtime_key(value) if mtime_key is not None else file_mtime(path)
                mtime = float(raw_mtime)
            except (OSError, TypeError, ValueError):
                mtime = 0.0
        decorated.append((path, mtime, index, value))

    def compare(left, right):
        if normalized_mode == "mtime" and left[1] != right[1]:
            return -1 if left[1] < right[1] else 1
        result = relative_path_compare(left[0], right[0], root)
        return result or (left[2] - right[2])

    return [value for _, _, _, value in sorted(decorated, key=cmp_to_key(compare))]


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


def ordered_bounded_map(executor, items, worker, max_workers: int):
    """Yield worker results in input order with a bounded pending queue.

    Scanning and media preflight use this helper so local and network roots
    share the same queue discipline. At most ``max_workers`` operations are
    submitted at once; a large directory therefore cannot allocate one
    future per file before the first result is consumed.
    """

    iterator = iter(items)
    pending = deque()
    limit = max(1, int(max_workers))
    for _ in range(limit):
        try:
            pending.append(executor.submit(worker, next(iterator)))
        except StopIteration:
            break
    while pending:
        yield pending.popleft().result()
        try:
            pending.append(executor.submit(worker, next(iterator)))
        except StopIteration:
            pass


def run_cancellable_process(command, *, cancel_event=None, timeout=None, **kwargs):
    """Run a bounded external process and honour cancellation while waiting.

    The no-event path deliberately delegates to :func:`subprocess.run` so
    existing integrations can continue to patch or instrument it.  A caller
    that supplies an event gets a small polling wrapper which terminates the
    child promptly instead of waiting for the full timeout.
    """

    if cancel_event is None:
        return subprocess.run(command, timeout=timeout, **kwargs)
    if _cancelled(cancel_event):
        raise TimeoutError("外部进程已取消")
    input_data = kwargs.pop("input", None)
    if input_data is not None:
        kwargs.setdefault("stdin", subprocess.PIPE)
    process = subprocess.Popen(command, **kwargs)
    if input_data is not None and process.stdin is not None:
        try:
            process.stdin.write(input_data)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            # The child may reject a batch before consuming stdin; its exit
            # status/stderr below remains the authoritative diagnostic.
            try:
                process.stdin.close()
            except OSError:
                pass
    started = time.monotonic()
    try:
        while process.poll() is None:
            if _cancelled(cancel_event):
                try:
                    process.terminate()
                except OSError:
                    pass
                try:
                    process.kill()
                except OSError:
                    pass
                process.communicate()
                raise TimeoutError("外部进程已取消")
            if timeout is not None and time.monotonic() - started >= float(timeout):
                try:
                    process.kill()
                except OSError:
                    pass
                process.communicate()
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(0.05)
        stdout, stderr = process.communicate()
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass


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


def is_file_stable(path, interval: float = 0.03, stable_checks: int = 2) -> bool:
    """Check that a file remains unchanged across bounded observations."""
    result = check_file_readiness(
        path,
        stable_interval=interval,
        stable_checks=stable_checks,
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


def is_link_or_junction(value) -> bool:
    """Return whether *value* is a symlink or a Windows reparse junction."""

    try:
        # ``Path.is_symlink()`` delegates to ``Path.stat()`` on some Python
        # versions.  Use lstat for Path/string values so a directory walk
        # still needs only the single ``DirEntry.stat`` performed below.
        if isinstance(value, os.DirEntry):
            if value.is_symlink():
                return True
            info = value.stat(follow_symlinks=False)
        else:
            info = os.lstat(_text(value))
    except (OSError, ValueError, TypeError):
        # An entry that cannot be inspected is treated as unsafe to recurse
        # into; the caller will report the original access error separately.
        return False
    return _is_reparse_info(info)


def _is_reparse_info(info) -> bool:
    if stat.S_ISLNK(getattr(info, "st_mode", 0)):
        return True
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(int(getattr(info, "st_file_attributes", 0)) & reparse_flag)


def iter_files(root, extensions, cancel_event=None):
    """Walk a local or UNC tree without following links or blocking forever."""
    accepted = {str(ext).lower() for ext in extensions}
    paths = []
    errors = []

    if is_link_or_junction(root):
        return paths, [f"跳过符号链接或 junction：{root}"]

    # Keep the traversal iterative. A recursive scanner can hit Python's
    # recursion limit on exported camera/archive trees with many nested
    # folders, while os.walk handles those trees without growing the call
    # stack. ``follow_symlinks=False`` retains os.walk's default behavior.
    stack = [_text(root)]
    while stack:
        if _cancelled(cancel_event):
            errors.append("目录扫描已取消")
            break
        directory = stack.pop()
        if is_link_or_junction(directory):
            errors.append(f"跳过符号链接或 junction：{directory}")
            continue
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    if _cancelled(cancel_event):
                        errors.append("目录扫描已取消")
                        break
                    try:
                        if entry.is_symlink():
                            errors.append(f"跳过符号链接或 junction：{entry.path}")
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if _is_reparse_info(info):
                            errors.append(f"跳过符号链接或 junction：{entry.path}")
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            stack.append(entry.path)
                            continue

                        # Avoid constructing Path objects for files that will
                        # be rejected by the extension filter. This spelling
                        # matches pathlib.Path.suffix for names such as
                        # ``photo.`` and ``.hidden`` (both have no suffix).
                        ext = _entry_suffix(entry.name)
                        if ext.lower() not in accepted:
                            continue
                        if stat.S_ISREG(info.st_mode) and info.st_size > 0:
                            paths.append(Path(entry.path))
                    except OSError as error:
                        errors.append(f"{entry.path}: {error}")
        except OSError as error:
            errors.append(str(error))

    return paths, errors
