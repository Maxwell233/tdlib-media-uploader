"""Bounded, cancellable checks for files that may still be changing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .concurrency import cancelable_sleep, is_cancelled
from .models import FileSnapshot
from .filesystem import is_transient_fs_error, snapshot_file_with_code


READY = "READY"
DEFERRED = "DEFERRED"
UNREADABLE = "UNREADABLE"
CHANGED = "CHANGED"
CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class FileReadiness:
    """Result of a bounded stat/read/stat readiness check."""

    status: str
    snapshot: FileSnapshot | None = None
    reason: str = ""
    attempts: int = 0
    code: str = ""

    @property
    def ready(self) -> bool:
        return self.status == READY and self.snapshot is not None


class FileReadinessError(RuntimeError):
    """Structured error for callers that require a ready source file."""

    def __init__(self, path, readiness: FileReadiness):
        self.path = path
        self.readiness = readiness
        reason = readiness.reason or f"文件暂时不可读取：{path}"
        super().__init__(reason)


def readiness_category(readiness: FileReadiness) -> str:
    """Map detailed states to the stable reporting categories."""

    if readiness.status == CANCELLED or readiness.code == "cancelled":
        return "cancelled"
    if readiness.status in {DEFERRED, CHANGED}:
        return "deferred"
    return "unreadable"


def raise_for_file_readiness(path, readiness: FileReadiness) -> None:
    if not readiness.ready:
        raise FileReadinessError(path, readiness)


def probe_readable(
    path,
    *,
    snapshot: FileSnapshot | None = None,
    probe_bytes: int = 64 * 1024,
) -> None:
    """Read a bounded head/tail window without loading the complete file."""

    current = snapshot
    if current is None:
        current, _code = snapshot_file_with_code(path)
    if current is None:
        raise OSError(f"文件暂时不可读取或为空：{path}")

    amount = max(1, int(probe_bytes))
    with open(current.path, "rb") as stream:
        head = stream.read(min(amount, current.size))
        if not head:
            raise OSError("读探针未返回数据")
        if current.size > amount:
            stream.seek(max(0, current.size - amount))
            tail = stream.read(min(amount, current.size))
            if not tail:
                raise OSError("读探针未返回文件尾部数据")


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
    cancel_token=None,
) -> FileReadiness:
    """Perform a bounded stat/read/stat readiness check.

    ``stable_checks`` counts observations that must agree.  The first
    observation is always the snapshot returned to the caller when possible;
    no extra stat is made by the read probe.
    """

    if is_cancelled(cancel_token, cancel_event):
        return FileReadiness(CANCELLED, reason="操作已取消", code="cancelled")

    first, first_code = snapshot_file_with_code(path)
    if first is None:
        status = (
            UNREADABLE
            if first_code in {"not_regular", "empty", "link_or_junction"}
            else DEFERRED
        )
        reason = (
            f"文件不是可读取的普通文件：{path}"
            if first_code in {"not_regular", "link_or_junction"}
            else f"文件为空或不可读取：{path}"
        )
        return FileReadiness(status, reason=reason, attempts=1, code=first_code)

    if expected_size is not None and int(expected_size) != first.size:
        return FileReadiness(
            CHANGED,
            first,
            f"文件在扫描后大小发生变化：{path}",
            1,
            "size_changed",
        )
    if expected_mtime_ns is not None and int(expected_mtime_ns) != first.mtime_ns:
        return FileReadiness(
            CHANGED,
            first,
            f"文件在扫描后修改时间发生变化：{path}",
            1,
            "mtime_changed",
        )

    try:
        if probe:
            probe_readable(path, snapshot=first, probe_bytes=probe_bytes)
    except (OSError, ValueError) as exc:
        return FileReadiness(
            DEFERRED,
            first,
            f"文件读取失败：{path} · {exc}",
            1,
            "read_failed",
        )

    checks = max(1, int(stable_checks))
    current = first
    for _index in range(1, checks):
        if stable_interval > 0 and not cancelable_sleep(
            float(stable_interval),
            cancel_event,
            cancel_token=cancel_token,
        ):
            return FileReadiness(
                CANCELLED,
                current,
                "文件稳定性检查被取消",
                1,
                "cancelled",
            )
        if is_cancelled(cancel_token, cancel_event):
            return FileReadiness(
                CANCELLED,
                current,
                "文件稳定性检查被取消",
                1,
                "cancelled",
            )
        observed, observed_code = snapshot_file_with_code(path)
        if observed is None:
            status = (
                UNREADABLE
                if observed_code in {"not_regular", "empty", "link_or_junction"}
                else DEFERRED
            )
            return FileReadiness(
                status,
                current,
                f"文件在检查期间变得不可读取：{path}",
                1,
                observed_code,
            )
        if observed.as_tuple() != current.as_tuple():
            return FileReadiness(
                CHANGED,
                observed,
                f"文件仍在写入或网络连接不稳定：{path}",
                1,
                "unstable",
            )
        current = observed
    return FileReadiness(READY, current, attempts=1, code="ready")


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
    cancel_token=None,
) -> FileReadiness:
    """Retry transient readiness failures with a finite exponential backoff."""

    limit = max(1, int(attempts))
    last = FileReadiness(
        DEFERRED,
        reason=f"文件暂时不可读取：{path}",
        code="stat_failed",
    )
    for attempt in range(1, limit + 1):
        if is_cancelled(cancel_token, cancel_event):
            return FileReadiness(
                CANCELLED,
                last.snapshot,
                "操作已取消",
                attempt - 1,
                "cancelled",
            )
        result = check_file_readiness(
            path,
            expected_size=expected_size,
            expected_mtime_ns=expected_mtime_ns,
            stable_interval=stable_interval,
            stable_checks=stable_checks,
            probe=probe,
            probe_bytes=probe_bytes,
            cancel_event=cancel_event,
            cancel_token=cancel_token,
        )
        last = FileReadiness(
            result.status,
            result.snapshot,
            result.reason,
            attempt,
            result.code,
        )
        if result.ready:
            return last
        if result.status in {CHANGED, UNREADABLE} or result.code in {
            "size_changed",
            "mtime_changed",
        }:
            return last
        if attempt < limit and not cancelable_sleep(
            initial_delay * (float(backoff) ** (attempt - 1)),
            cancel_event,
            cancel_token=cancel_token,
        ):
            return FileReadiness(
                CANCELLED,
                result.snapshot,
                "操作已取消",
                attempt,
                "cancelled",
            )
    return last


def is_file_stable(
    path,
    interval: float = 0.03,
    stable_checks: int = 2,
    *,
    cancel_event=None,
    cancel_token=None,
) -> bool:
    result = check_file_readiness(
        path,
        stable_interval=interval,
        stable_checks=stable_checks,
        probe=False,
        cancel_event=cancel_event,
        cancel_token=cancel_token,
    )
    return result.ready


def revalidate_file(path, *, expected_size=None, expected_mtime_ns=None) -> FileSnapshot:
    """Return a current snapshot only when it matches the scan facts."""

    snapshot, _code = snapshot_file_with_code(path)
    if snapshot is None:
        raise RuntimeError(f"文件暂时不可读取或已被删除：{path}")
    if expected_size is not None and int(expected_size) != snapshot.size:
        raise RuntimeError(f"文件在扫描后发生变化：{path}")
    if expected_mtime_ns is not None and int(expected_mtime_ns) != snapshot.mtime_ns:
        raise RuntimeError(f"文件在扫描后发生变化：{path}")
    return snapshot


__all__ = [
    "CANCELLED",
    "CHANGED",
    "DEFERRED",
    "FileReadiness",
    "FileReadinessError",
    "READY",
    "UNREADABLE",
    "check_file_readiness",
    "is_file_stable",
    "is_transient_fs_error",
    "probe_readable",
    "readiness_category",
    "raise_for_file_readiness",
    "revalidate_file",
    "wait_for_file_ready",
]
