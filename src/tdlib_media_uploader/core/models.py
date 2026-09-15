"""Main-Agent-owned V2 data contracts.

These models are deliberately transport- and GUI-neutral.  The V1.9
dictionary-shaped results remain valid until their owning migration phase is
integrated; new modules must translate at their boundary instead of creating a
second incompatible model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class BatchStatus(str, Enum):
    """Durable send states shared by the engine, journal and UI."""

    PREPARED = "PREPARED"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """The one-stat source identity captured during discovery."""

    path: str
    size: int
    mtime_ns: int

    def as_tuple(self) -> tuple[int, int]:
        return self.size, self.mtime_ns

    def __iter__(self):
        # Preserve the convenient tuple-unpacking behavior of path_utils.
        yield self.size
        yield self.mtime_ns


@dataclass(frozen=True, slots=True)
class MediaItem:
    """A discovered media file plus the immutable facts used for identity."""

    path: Path
    source_root: Path
    media_kind: str
    snapshot: FileSnapshot
    capture_time: datetime | None = None
    group_name: str = ""
    month_key: str | None = None
    date_tag: str = ""
    fallback: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AlbumPlan:
    """A complete, stable Album boundary produced before preflight/upload."""

    key: str
    kind: str
    source_root: Path
    group_label: str
    number: int
    items: tuple[MediaItem, ...]
    pending_items: tuple[MediaItem, ...]
    caption: str
    target: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UploadBatchResult:
    """The final observed result for one Album send attempt."""

    album_key: str
    status: BatchStatus
    message_ids: tuple[int, ...] = ()
    confirmed_items: tuple[MediaItem, ...] = ()
    error: str | None = None
    deferred_items: tuple[MediaItem, ...] = ()


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """A structured progress update consumable by CLI, GUI or tests."""

    kind: str
    phase: str
    completed: int
    total: int
    path: str | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class LogEvent:
    """A durable/log-view update independent of Qt widgets."""

    level: str
    message: str
    source: str = "app"


@dataclass(frozen=True, slots=True)
class AuthEvent:
    """A TDLib authentication state update for the GUI adapter."""

    state: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class ScanResult:
    """A strategy-level scan result with explicit cancellation semantics."""

    items: tuple[MediaItem, ...] = ()
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class UploadRunResult:
    """The aggregate result of one engine-owned upload run."""

    status: str
    batches: tuple[UploadBatchResult, ...] = ()
    error: str | None = None
    deferred_items: tuple[MediaItem, ...] = ()
    failed_items: tuple[MediaItem, ...] = ()
    scanned_items: tuple[MediaItem, ...] = ()
    cancelled: bool = False


Event = ProgressEvent | LogEvent | AuthEvent


__all__ = [
    "AlbumPlan",
    "AuthEvent",
    "BatchStatus",
    "Event",
    "FileSnapshot",
    "LogEvent",
    "MediaItem",
    "ProgressEvent",
    "ScanResult",
    "UploadBatchResult",
    "UploadRunResult",
]
