"""V2 public contracts for the TDLib Media Uploader refactor.

The existing V1.9 modules remain the executable implementation during the
incremental migration.  This package contains only the main-Agent-owned
contracts established in Phase 0.
"""

from .contracts import (
    CancelToken,
    Event,
    EventSink,
    MediaStrategy,
    UploadContext,
    UploadEngine,
)
from .core.models import (
    AlbumPlan,
    AuthEvent,
    BatchStatus,
    ContentBuildResult,
    FileSnapshot,
    LogEvent,
    MediaItem,
    ProgressEvent,
    ScanResult,
    UploadBatchResult,
    UploadRunResult,
)

__all__ = [
    "AlbumPlan",
    "AuthEvent",
    "BatchStatus",
    "CancelToken",
    "ContentBuildResult",
    "Event",
    "EventSink",
    "FileSnapshot",
    "LogEvent",
    "MediaItem",
    "MediaStrategy",
    "ProgressEvent",
    "ScanResult",
    "UploadBatchResult",
    "UploadContext",
    "UploadEngine",
    "UploadRunResult",
]
