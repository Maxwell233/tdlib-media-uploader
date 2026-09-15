"""Main-Agent-owned service protocols for the V2 migration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .core.models import (
    AlbumPlan,
    Event,
    ContentBuildResult,
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


@dataclass(frozen=True, slots=True)
class UploadContext:
    """Immutable collaborators and run data shared by one engine execution.

    The context is deliberately dependency-injected.  ``state``, ``journal``,
    ``sender``, ``stager`` and ``preflight`` are opaque to the public contract
    so the package can be tested with small fakes and the GUI layer can later
    provide the durable V1.9 adapters without importing GUI code here.
    """

    source_root: Path
    target: Mapping[str, Any]
    cancel_token: CancelToken
    event_sink: EventSink
    kind: str = ""
    state: Any | None = None
    journal: Any | None = None
    sender: Any | None = None
    stager: Any | None = None
    preflight: Any | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class MediaStrategy(Protocol):
    """Media-specific scan, plan and Telegram-content behavior."""

    kind: str

    def scan(
        self,
        source_root: Path,
        *,
        cancel_token: CancelToken,
        event_sink: EventSink,
        context: UploadContext | None = None,
    ) -> ScanResult:
        """Discover media and capture all identity-relevant snapshots."""

    def build_plans(
        self,
        scan_result: ScanResult,
        *,
        target: Mapping[str, Any],
        state: Any | None = None,
        context: UploadContext | None = None,
    ) -> Sequence[AlbumPlan]:
        """Create complete Album boundaries before preflight or sending."""

    def build_contents(
        self,
        plan: AlbumPlan,
        *,
        cancel_token: CancelToken,
        event_sink: EventSink,
        context: UploadContext | None = None,
    ) -> Sequence[Mapping[str, Any]] | ContentBuildResult:
        """Translate one stable plan into TDLib content and item outcomes."""


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
        context: UploadContext | None = None,
    ) -> UploadRunResult:
        """Run one media strategy while preserving the durable state machine.

        ``context`` is optional for compatibility with the Phase 0 protocol;
        an engine implementation may use it to receive injected adapters.
        """


__all__ = [
    "CancelToken",
    "Event",
    "EventSink",
    "MediaStrategy",
    "UploadContext",
    "UploadEngine",
]
