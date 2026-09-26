"""GUI-free directory discovery and immutable file snapshots."""

from __future__ import annotations

import errno
import ntpath
import os
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, TypeVar

from .concurrency import (
    cancelable_sleep,
    io_worker_count as _bounded_io_worker_count,
    is_cancelled,
)
from .models import FileSnapshot
from .sorting import (
    media_path_sort,
    natural_compare,
    natural_sort,
    natural_sort_key,
    relative_name,
    relative_path_compare,
)


DISCOVERY_ATTEMPTS = 3
DISCOVERY_INITIAL_DELAY_SECONDS = 0.15
DISCOVERY_MAX_DELAY_SECONDS = 1.0

_MAC_MOUNT_CACHE_LOCK = threading.Lock()
_MAC_MOUNT_CACHE: tuple[float, tuple[tuple[str, str], ...]] | None = None
_T = TypeVar("_T")


def _text(path: object) -> str:
    return os.fspath(path) if hasattr(path, "__fspath__") else str(path)


def _is_reparse_info(info) -> bool:
    if stat.S_ISLNK(getattr(info, "st_mode", 0)):
        return True
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(int(getattr(info, "st_file_attributes", 0)) & reparse_flag)


def is_link_or_junction(value) -> bool:
    """Return whether *value* is a symlink or Windows reparse junction."""

    try:
        if isinstance(value, os.DirEntry):
            if value.is_symlink():
                return True
            info = value.stat(follow_symlinks=False)
        else:
            info = os.lstat(_text(value))
    except (OSError, TypeError, ValueError):
        # An entry that cannot be inspected is handled by the walker's normal
        # error path; it is not safe to recurse into it here.
        return False
    return _is_reparse_info(info)


def _snapshot_from_info(path: object, info) -> FileSnapshot | None:
    if _is_reparse_info(info) or not stat.S_ISREG(info.st_mode):
        return None
    if int(info.st_size) <= 0:
        return None
    mtime_ns = getattr(info, "st_mtime_ns", None)
    if mtime_ns is None:
        mtime_ns = int(float(info.st_mtime) * 1_000_000_000)
    return FileSnapshot(_text(path), int(info.st_size), int(mtime_ns))


def snapshot_file_with_code(path) -> tuple[FileSnapshot | None, str]:
    """Capture one non-following stat snapshot and a machine-readable code."""

    value = _text(path)
    try:
        info = os.lstat(value)
    except OSError:
        return None, "stat_failed"
    if _is_reparse_info(info):
        return None, "link_or_junction"
    if not stat.S_ISREG(info.st_mode):
        return None, "not_regular"
    if int(info.st_size) <= 0:
        return None, "empty"
    snapshot = _snapshot_from_info(value, info)
    return (snapshot, "ready") if snapshot is not None else (None, "unreadable")


def snapshot_file(path) -> FileSnapshot | None:
    """Return one structured snapshot, or ``None`` when unavailable."""

    return snapshot_file_with_code(path)[0]


def file_snapshot(path):
    """Return the historical two-value snapshot tuple when available."""

    snapshot = snapshot_file(path)
    return snapshot.as_tuple() if snapshot is not None else None


def stable_path(path) -> str:
    """Return a non-resolving identity path suitable for dictionaries."""

    value = _text(path)
    if os.name == "nt":
        if is_unc_path(value):
            return ntpath.normcase(ntpath.normpath(value))
        return ntpath.normcase(ntpath.normpath(ntpath.abspath(value)))
    return os.path.normcase(os.path.normpath(os.path.abspath(value)))


def display_path(path) -> str:
    """Return a normalized path string suitable for a media-process input."""

    value = _text(path)
    if os.name == "nt" and is_unc_path(value):
        return ntpath.normpath(value)
    return str(Path(value).absolute())


def is_unc_path(path) -> bool:
    value = _text(path).replace("/", "\\")
    return value.startswith("\\\\")


def file_mtime(path, fallback: float = 0.0) -> float:
    try:
        return float(os.stat(path).st_mtime)
    except OSError:
        return fallback


def is_network_path(path) -> bool:
    """Detect common network-share paths without changing path semantics."""

    value = _text(path).replace("/", "\\")
    if is_unc_path(value):
        return True
    if os.name == "nt" and len(value) >= 3 and value[1] == ":":
        try:
            import ctypes

            return int(ctypes.windll.kernel32.GetDriveTypeW(value[:3])) == 4
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    posix_value = _text(path).replace("\\", "/")
    if not posix_value.startswith("/"):
        return False
    if sys.platform == "darwin":
        filesystem_type = _macos_filesystem_type(posix_value)
        if filesystem_type:
            network_types = {
                "smbfs",
                "nfs",
                "webdav",
                "afpfs",
                "sshfs",
                "cifs",
                "9p",
            }
            local_types = {
                "apfs",
                "hfs",
                "hfsplus",
                "exfat",
                "msdos",
                "ufs",
                "zfs",
            }
            normalized = filesystem_type.casefold()
            if normalized in network_types:
                return True
            if normalized in local_types:
                return False

    prefixes = (
        ("/Volumes", "/Network", "/net", "/mnt", "/run/mount")
        if sys.platform == "darwin"
        else ("/net", "/mnt", "/run/mount", "/Volumes", "/Network")
    )
    return any(
        posix_value == prefix or posix_value.startswith(prefix + "/")
        for prefix in prefixes
    )


def _macos_filesystem_type(path: str) -> str | None:
    if sys.platform != "darwin":
        return None
    now = time.monotonic()
    global _MAC_MOUNT_CACHE
    with _MAC_MOUNT_CACHE_LOCK:
        if _MAC_MOUNT_CACHE is None or now - _MAC_MOUNT_CACHE[0] >= 5.0:
            mounts: list[tuple[str, str]] = []
            try:
                # This diagnostic is bounded and does not depend on GUI or
                # TDLib.  Keep it local so ordinary path checks stay cheap.
                result = subprocess.run(
                    ["/sbin/mount"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=2.0,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                result = None
            for raw_line in str(result.stdout if result is not None else "").splitlines():
                if " on " not in raw_line or "(" not in raw_line:
                    continue
                _source, mount_and_options = raw_line.split(" on ", 1)
                mount_point, options = mount_and_options.split("(", 1)
                mount_point = (
                    mount_point.strip()
                    .replace("\\040", " ")
                    .replace("\\011", "\t")
                )
                filesystem = options.split(",", 1)[0].strip().rstrip(")")
                if mount_point and filesystem:
                    mounts.append((mount_point, filesystem))
            _MAC_MOUNT_CACHE = (now, tuple(mounts))
        mounts = _MAC_MOUNT_CACHE[1]

    best_mount = ""
    best_type = None
    for mount_point, filesystem in mounts:
        if path == mount_point or path.startswith(mount_point + "/"):
            if len(mount_point) >= len(best_mount):
                best_mount = mount_point
                best_type = filesystem
    return best_type


def io_worker_count(root, *, local: int = 4, network: int = 2) -> int:
    """Choose a bounded I/O worker count for local or network media."""

    return _bounded_io_worker_count(
        root,
        local=local,
        network=network,
        network_predicate=is_network_path,
    )


def _cancelled(cancel_token=None, cancel_event=None) -> bool:
    return is_cancelled(cancel_token, cancel_event)


_PERMANENT_FS_ERRORS = {
    code
    for code in (
        getattr(errno, "EACCES", None),
        getattr(errno, "EPERM", None),
        getattr(errno, "EINVAL", None),
        getattr(errno, "ENAMETOOLONG", None),
        getattr(errno, "ENOTDIR", None),
    )
    if code is not None
}
_TRANSIENT_FS_ERRORS = {
    code
    for code in (
        getattr(errno, "ETIMEDOUT", None),
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "ECONNABORTED", None),
        getattr(errno, "ENETUNREACH", None),
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENOTCONN", None),
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EBUSY", None),
        getattr(errno, "ESTALE", None),
        getattr(errno, "EIO", None),
    )
    if code is not None
}
_TRANSIENT_FS_WIN_ERRORS = {21, 32, 33, 53, 64, 121, 1231, 1236, 1450, 995}
_TRANSIENT_FS_MARKERS = (
    "timed out",
    "timeout",
    "temporarily unavailable",
    "try again",
    "connection reset",
    "connection aborted",
    "network is unreachable",
    "network path",
    "sharing violation",
    "resource busy",
    "stale file",
    "network name",
    "device not ready",
    "再试",
    "暂时不可用",
    "网络",
    "连接重置",
    "共享冲突",
)


def is_transient_fs_error(exc: BaseException) -> bool:
    """Classify errors for bounded retry; permanent errors fail immediately."""

    if not isinstance(exc, OSError):
        return False

    # ⚡ Bolt: Use module-level constants instead of rebuilding sets dynamically per-call
    # to eliminate redundant allocations inside tight file iteration loops.
    err_code = getattr(exc, "errno", None)
    if err_code in _PERMANENT_FS_ERRORS:
        return False
    if err_code in _TRANSIENT_FS_ERRORS:
        return True

    if getattr(exc, "winerror", None) in _TRANSIENT_FS_WIN_ERRORS:
        return True

    text = str(exc).casefold()
    if any(marker in text for marker in _TRANSIENT_FS_MARKERS):
        return True

    # Unknown platform-specific OSErrors remain retryable, but the caller's
    # attempt limit guarantees termination.
    return True


def retry_fs_operation(
    operation: Callable[[], object],
    *,
    attempts: int = DISCOVERY_ATTEMPTS,
    initial_delay: float = DISCOVERY_INITIAL_DELAY_SECONDS,
    max_delay: float = DISCOVERY_MAX_DELAY_SECONDS,
    cancel_event=None,
    cancel_token=None,
    retry_if: Callable[[BaseException], bool] | None = None,
):
    """Run a filesystem operation with bounded, cancellation-aware retries."""

    limit = max(1, int(attempts))
    delay = max(0.0, float(initial_delay))
    ceiling = max(0.0, float(max_delay))
    for attempt in range(1, limit + 1):
        if _cancelled(cancel_token, cancel_event):
            raise TimeoutError("目录扫描已取消")
        try:
            return operation()
        except OSError as exc:
            should_retry = retry_if(exc) if retry_if is not None else is_transient_fs_error(exc)
            if not should_retry or attempt >= limit:
                raise
            wait_seconds = min(ceiling, delay * (2 ** (attempt - 1)))
            if not cancelable_sleep(
                wait_seconds,
                cancel_event,
                cancel_token=cancel_token,
            ):
                raise TimeoutError("目录扫描已取消") from exc
    raise RuntimeError("文件系统重试未返回结果")


def retry_with_backoff(
    operation: Callable[[], _T],
    *,
    attempts: int = 3,
    initial_delay: float = 0.12,
    backoff: float = 2.0,
    retry_if: Callable[[BaseException], bool] | None = None,
    cancel_event=None,
    cancel_token=None,
) -> _T:
    """Run a small I/O operation with generic bounded retry semantics."""

    limit = max(1, int(attempts))
    for attempt in range(1, limit + 1):
        if _cancelled(cancel_token, cancel_event):
            raise TimeoutError("操作已取消")
        try:
            return operation()
        except Exception as exc:
            should_retry = (
                retry_if(exc)
                if retry_if is not None
                else isinstance(exc, (OSError, TimeoutError))
            )
            if not should_retry or attempt >= limit:
                raise
            if not cancelable_sleep(
                initial_delay * (float(backoff) ** (attempt - 1)),
                cancel_event,
                cancel_token=cancel_token,
            ):
                raise TimeoutError("操作已取消") from exc
    raise RuntimeError("重试操作未返回结果")


def _open_scandir(
    directory,
    *,
    attempts: int,
    initial_delay: float,
    max_delay: float,
    cancel_event=None,
    cancel_token=None,
):
    return retry_fs_operation(
        lambda: os.scandir(directory),
        attempts=attempts,
        initial_delay=initial_delay,
        max_delay=max_delay,
        cancel_event=cancel_event,
        cancel_token=cancel_token,
    )


def validate_scan_root(
    root,
    *,
    attempts: int = DISCOVERY_ATTEMPTS,
    initial_delay: float = DISCOVERY_INITIAL_DELAY_SECONDS,
    max_delay: float = DISCOVERY_MAX_DELAY_SECONDS,
    cancel_event=None,
    cancel_token=None,
):
    """Validate a scan root with the same retry policy as directory entries."""

    path = Path(root)
    try:
        info = retry_fs_operation(
            lambda: os.lstat(_text(path)),
            attempts=attempts,
            initial_delay=initial_delay,
            max_delay=max_delay,
            cancel_event=cancel_event,
            cancel_token=cancel_token,
        )
    except TimeoutError:
        raise
    except OSError as exc:
        raise RuntimeError(f"目录不存在或暂时无法读取：{path}\n{exc}") from exc
    if _is_reparse_info(info):
        raise RuntimeError(f"目录不能是符号链接或 junction：{path}")
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"路径不是目录：{path}")
    return path


def iter_directory_entries_with_retry(
    directory,
    *,
    attempts: int = DISCOVERY_ATTEMPTS,
    initial_delay: float = DISCOVERY_INITIAL_DELAY_SECONDS,
    max_delay: float = DISCOVERY_MAX_DELAY_SECONDS,
    cancel_event=None,
    cancel_token=None,
):
    """Yield entries while reopening a directory after transient iteration errors."""

    seen: set[str] = set()
    limit = max(1, int(attempts))
    reopen_attempts = 0
    while True:
        if _cancelled(cancel_token, cancel_event):
            raise TimeoutError("目录扫描已取消")
        iterator = _open_scandir(
            directory,
            attempts=limit,
            initial_delay=initial_delay,
            max_delay=max_delay,
            cancel_event=cancel_event,
            cancel_token=cancel_token,
        )
        reopen = False
        try:
            with iterator as entries:
                entry_iterator = iter(entries)
                while True:
                    if _cancelled(cancel_token, cancel_event):
                        raise TimeoutError("目录扫描已取消")
                    try:
                        entry = next(entry_iterator)
                    except StopIteration:
                        return
                    except OSError as exc:
                        if not is_transient_fs_error(exc) or reopen_attempts >= limit:
                            raise
                        reopen_attempts += 1
                        delay = min(
                            max(0.0, float(max_delay)),
                            max(0.0, float(initial_delay))
                            * (2 ** (reopen_attempts - 1)),
                        )
                        if not cancelable_sleep(
                            delay,
                            cancel_event,
                            cancel_token=cancel_token,
                        ):
                            raise TimeoutError("目录扫描已取消") from exc
                        reopen = True
                        break
                    identity = stable_path(getattr(entry, "path", entry.name))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    yield entry
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                try:
                    close()
                except OSError:
                    pass
        if not reopen:
            return


@dataclass
class DirectoryScanResult:
    """Low-level discovery result with the V1 two-value unpacking adapter."""

    paths: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False
    snapshots: dict[str, FileSnapshot] = field(default_factory=dict)

    def __iter__(self):
        legacy_errors = list(self.errors) + list(self.warnings)
        if self.cancelled:
            legacy_errors.append("目录扫描已取消")
        yield self.paths
        yield legacy_errors

    def __len__(self) -> int:
        return 2

    def __getitem__(self, index: int):
        if index not in (0, 1, -2, -1):
            raise IndexError(index)
        legacy_errors = list(self.errors) + list(self.warnings)
        if self.cancelled:
            legacy_errors.append("目录扫描已取消")
        return (self.paths, legacy_errors)[index]


# Keep the low-level result explicitly named so it cannot be confused with
# the strategy-level ``core.models.ScanResult`` contract.
FileScanResult = DirectoryScanResult


def _entry_suffix(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:] if 0 < dot < len(name) - 1 else ""


def iter_files(
    root,
    extensions: Iterable[str] | str,
    cancel_event=None,
    *,
    cancel_token=None,
    discovery_attempts: int = DISCOVERY_ATTEMPTS,
    discovery_initial_delay: float = DISCOVERY_INITIAL_DELAY_SECONDS,
    discovery_max_delay: float = DISCOVERY_MAX_DELAY_SECONDS,
) -> DirectoryScanResult:
    """Walk a tree without following links and capture one file snapshot."""

    if isinstance(extensions, (str, bytes)):
        extensions = (extensions,)
    accepted = {str(ext).casefold() for ext in extensions}
    paths: list[Path] = []
    errors: list[str] = []
    warnings: list[str] = []
    snapshots: dict[str, FileSnapshot] = {}
    cancelled = False

    if is_link_or_junction(root):
        return DirectoryScanResult(
            paths,
            errors,
            [f"跳过符号链接或 junction：{root}"],
            False,
            snapshots,
        )

    stack = [_text(root)]
    while stack:
        if _cancelled(cancel_token, cancel_event):
            cancelled = True
            break
        directory = stack.pop()
        if is_link_or_junction(directory):
            warnings.append(f"跳过符号链接或 junction：{directory}")
            continue
        try:
            for entry in iter_directory_entries_with_retry(
                directory,
                attempts=discovery_attempts,
                initial_delay=discovery_initial_delay,
                max_delay=discovery_max_delay,
                cancel_event=cancel_event,
                cancel_token=cancel_token,
            ):
                if _cancelled(cancel_token, cancel_event):
                    cancelled = True
                    break
                try:
                    # This is intentionally the only stat for a matching file
                    # during discovery.  It is both the type check and the
                    # immutable identity snapshot; no late stat is performed.
                    info = retry_fs_operation(
                        lambda entry=entry: entry.stat(follow_symlinks=False),
                        attempts=discovery_attempts,
                        initial_delay=discovery_initial_delay,
                        max_delay=discovery_max_delay,
                        cancel_event=cancel_event,
                        cancel_token=cancel_token,
                    )
                    if _is_reparse_info(info):
                        warnings.append(f"跳过符号链接或 junction：{entry.path}")
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(entry.path)
                        continue
                    if _entry_suffix(entry.name).casefold() not in accepted:
                        continue
                    snapshot = _snapshot_from_info(entry.path, info)
                    if snapshot is None:
                        continue
                    path = Path(entry.path)
                    paths.append(path)
                    snapshots[stable_path(path)] = snapshot
                except TimeoutError as exc:
                    if _cancelled(cancel_token, cancel_event):
                        cancelled = True
                        break
                    errors.append(f"{entry.path}: {exc}")
                except OSError as exc:
                    errors.append(f"{entry.path}: {exc}")
            if cancelled:
                break
        except TimeoutError as exc:
            if _cancelled(cancel_token, cancel_event):
                cancelled = True
                break
            errors.append(f"{directory}: {exc}")
        except OSError as exc:
            errors.append(f"{directory}: {exc}")

    return DirectoryScanResult(paths, errors, warnings, cancelled, snapshots)


def revalidate_file(path, *, expected_size=None, expected_mtime_ns=None) -> FileSnapshot:
    """Validate a file against the discovery snapshot immediately before use."""

    snapshot = snapshot_file(path)
    if snapshot is None:
        raise RuntimeError(f"文件暂时不可读取或已被删除：{path}")
    if expected_size is not None and int(expected_size) != snapshot.size:
        raise RuntimeError(f"文件在扫描后发生变化：{path}")
    if expected_mtime_ns is not None and int(expected_mtime_ns) != snapshot.mtime_ns:
        raise RuntimeError(f"文件在扫描后发生变化：{path}")
    return snapshot


__all__ = [
    "DISCOVERY_ATTEMPTS",
    "DISCOVERY_INITIAL_DELAY_SECONDS",
    "DISCOVERY_MAX_DELAY_SECONDS",
    "DirectoryScanResult",
    "FileScanResult",
    "FileSnapshot",
    "display_path",
    "file_mtime",
    "file_snapshot",
    "io_worker_count",
    "is_link_or_junction",
    "is_network_path",
    "is_transient_fs_error",
    "iter_directory_entries_with_retry",
    "iter_files",
    "media_path_sort",
    "natural_compare",
    "natural_sort",
    "natural_sort_key",
    "revalidate_file",
    "relative_name",
    "relative_path_compare",
    "retry_fs_operation",
    "retry_with_backoff",
    "snapshot_file",
    "snapshot_file_with_code",
    "stable_path",
    "validate_scan_root",
]
