"""Main-Agent-owned service protocols for the V2 migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .core.models import (
    AlbumPlan,
    Event,
    ScanResult,
    UploadRunResult,
)


class CancelToken(Protocol):
    """Cooperative cancellation boundary shared by every long-running step."""

    def is_cancelled(self) -> bool:
        """Return whether the caller requested cancellation."""

    def raise_if_cancelled(self) -> None:
        """Raise the implementation's cancellation exception when requested."""


class EventSink(Protocol):
    """Consumer boundary for progress, log and authentication events."""

    def emit(self, event: Event) -> None:
        """Publish one event without embedding GUI or transport behavior."""


class MediaStrategy(Protocol):
    """Media-specific scan, plan and Telegram-content behavior."""

    kind: str

    def scan(
        self,
        source_root: Path,
        *,
        cancel_token: CancelToken,
        event_sink: EventSink,
    ) -> ScanResult:
        """Discover media and capture all identity-relevant snapshots."""

    def build_plans(
        self,
        scan_result: ScanResult,
        *,
        target: Mapping[str, Any],
        state: Any | None = None,
    ) -> Sequence[AlbumPlan]:
        """Create complete Album boundaries before preflight or sending."""

    def build_contents(
        self,
        plan: AlbumPlan,
        *,
        cancel_token: CancelToken,
        event_sink: EventSink,
    ) -> Sequence[Mapping[str, Any]]:
        """Translate one stable plan into TDLib input message content."""


class UploadEngine(Protocol):
    """Generic lifecycle owner for staging, journal, send and checkpointing."""

    def run(
        self,
        strategy: MediaStrategy,
        *,
        source_root: Path,
        target: Mapping[str, Any],
        cancel_token: CancelToken,
        event_sink: EventSink,
    ) -> UploadRunResult:
        """Run one media strategy while preserving the durable state machine."""


__all__ = [
    "CancelToken",
    "Event",
    "EventSink",
    "MediaStrategy",
    "UploadEngine",
]
