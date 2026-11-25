"""Qt worker boundary for V2 scans and uploads.

Workers depend on explicit callbacks supplied by the compatibility GUI.  They
do not import the root widget module, so the package can own thread lifecycle
and cancellation while preview formatting remains reversible.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import importlib
from pathlib import Path
import threading
import time
from typing import Any
from uuid import uuid4

from ..core.concurrency import CancellationRequested
from ..core.logging import write_exception
from ..core.models import UploadRunResult
from ..core.compat import call_supported
from PySide6.QtCore import QThread, Signal

from ..upload.queue import UploadTask
from .events import AuthBridge, GuiConsoleUI
from .tools import require_kind as _require_kind


def _load_upload_runner() -> Callable[..., Any]:
    from .integration import run_v2_upload

    return run_v2_upload


@dataclass(frozen=True, slots=True)
class QueueRecoveryResult:
    action: str
    task: UploadTask | None = None
    error: str | None = None
    cancelled: bool = False


class QueueRecoveryWorker(QThread):
    """Run verification and durable queue writes outside the GUI event loop."""

    progress = Signal(str, bool)

    def __init__(self, store, action, subject, revision):
        super().__init__()
        self.store = store
        self.action = action
        self.subject = subject
        self.revision = revision
        self.cancel_event = threading.Event()
        self.result: QueueRecoveryResult | None = None

    def request_stop(self):
        self.cancel_event.set()

    def run(self):
        try:
            task = self.store.recover(
                self.action, self.subject, expected_revision=self.revision,
                cancel_event=self.cancel_event, progress=self.progress.emit,
            )
        except CancellationRequested:
            self.result = QueueRecoveryResult(self.action, cancelled=True)
        except Exception as exc:
            self.result = QueueRecoveryResult(self.action, error=str(exc) or type(exc).__name__)
        else:
            self.result = QueueRecoveryResult(self.action, task=task)


class ScanWorker(QThread):
    """Run the GUI preview adapter without blocking the Qt event loop."""

    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)
    progress_changed = Signal(str, object)

    def __init__(self, kind: str, *, scan_runner: Callable[..., Mapping[str, Any]]):
        super().__init__()
        self.kind = _require_kind(kind)
        self.scan_id = str(uuid4())
        self.scan_runner = scan_runner
        self.cancel_event = threading.Event()
        self._last_progress_emit = 0.0
        self.result = None
        self.error = None

    def request_stop(self):
        """Request cancellation without terminating the worker thread."""

        self.cancel_event.set()

    def _report_progress(self, payload: dict):
        now = time.monotonic()
        completed = payload.get("completed") if isinstance(payload, Mapping) else None
        total = payload.get("total") if isinstance(payload, Mapping) else None
        is_boundary = (
            completed is None
            or total is None
            or completed == 0
            or completed >= total
            or self._last_progress_emit == 0.0
        )
        if is_boundary or (now - self._last_progress_emit) >= 0.04:
            self._last_progress_emit = now
            self.progress_changed.emit(self.kind, payload)

    def run(self):
        try:
            result = self.scan_runner(
                self.kind,
                progress_callback=self._report_progress,
                cancel_event=self.cancel_event,
            )
            self.result = result
            if result.get("cancelled"):
                self.cancelled.emit(result)
            else:
                self.completed.emit(result)
        except Exception as exc:
            write_exception(f"{self.kind} 扫描失败", exc, source=f"scan/{self.kind}")
            self.error = f"扫描失败：{type(exc).__name__}: {exc}"
            self.failed.emit(self.error)


class CacheClearWorker(QThread):
    """Own cache deletion until finished; emit progress at a bounded rate."""
    progress = Signal(str)

    def __init__(self, keys, targets, parent=None):
        super().__init__(parent)
        self.keys = tuple(keys)
        self.targets = dict(targets)
        self.cancel_event = threading.Event()
        self.result = ([], [])

    def request_stop(self):
        self.cancel_event.set()

    def run(self):
        import time
        from .cache_service import clear_cache
        last_update = 0.0
        def progress(path):
            nonlocal last_update
            now = time.monotonic()
            if now - last_update >= 0.1:
                last_update = now
                self.progress.emit(f"正在清理：{path}")
        try:
            self.result = clear_cache(self.keys, self.targets, cancel_event=self.cancel_event, progress=progress)
        except Exception as exc:
            self.result = ([], [str(exc)])


class CacheStatsWorker(QThread):
    """Calculate cache usage statistics in background without blocking Qt UI."""

    result_ready = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cancel_event = threading.Event()

    def request_stop(self):
        self.cancel_event.set()

    def run(self):
        try:
            from .cache_service import cache_status_text  # noqa: PLC0415
            text = cache_status_text(cancel_event=self.cancel_event)
        except Exception as exc:
            text = f"计算缓存占用失败：{exc}"
        self.result_ready.emit(text)


class UploadWorker(QThread):
    """Run one V2 upload and return the engine's structured outcome."""

    completed = Signal(object)
    ready_to_run = Signal()

    def __init__(
        self,
        kind: str,
        auth_bridge: AuthBridge,
        preview_result: Mapping[str, Any] | None = None,
        *,
        upload_runner: Callable[..., Any] | None = None,
        target_provider: Callable[[str], Mapping[str, Any]] | None = None,
        source_root_provider: Callable[[str], Path | str] | None = None,
        config: Any | None = None,
        runtime_paths: Any | None = None,
        session_manager=None,
        queue_task_id: str | None = None,
    ):
        super().__init__()
        self.kind = _require_kind(kind)
        self.preview_result = dict(preview_result or {})
        self.result: UploadRunResult | None = None
        self.prepared = threading.Event()
        self.preparation_error = None
        self.wait_for_scheduler = False
        self.upload_runner = upload_runner or _load_upload_runner()
        self.target_provider = target_provider or (lambda _kind: {})
        self.source_root_provider = source_root_provider
        self.config = config
        self.runtime_paths = runtime_paths
        self.session_manager = session_manager
        self.queue_task_id = queue_task_id
        self.ui = GuiConsoleUI(
            auth_bridge,
            kind,
            target_mode_provider=self._target_mode,
        )

    def _target(self) -> dict[str, Any]:
        value = self.target_provider(self.kind)
        return dict(value or {})

    def _target_mode(self) -> str:
        return str(self._target().get("target_mode", "forum_topic"))

    def _source_root(self) -> Path:
        if self.source_root_provider is not None:
            return Path(self.source_root_provider(self.kind))
        return Path(self.preview_result.get("source_dir", "."))

    def _config(self) -> Any:
        if self.config is not None:
            return self.config
        return importlib.import_module("tdlib_media_uploader.config.loader")

    def _runtime_paths(self) -> Any:
        if self.runtime_paths is not None:
            return self.runtime_paths
        return importlib.import_module("tdlib_media_uploader.config.paths")

    def request_stop(self):
        self.ui.request_immediate_stop()

    def request_safe_stop(self):
        self.ui.request_safe_stop()

    def request_immediate_stop(self):
        self.ui.request_immediate_stop()

    def _ready(self):
        if not self.wait_for_scheduler:
            return None
        self.ready_to_run.emit()
        while not self.prepared.wait(0.05):
            if self.ui.safe_stop_requested:
                return UploadRunResult("CANCELLED", error="准备期间已停止", cancelled=True)
        if self.preparation_error:
            return UploadRunResult("FAILED", error=self.preparation_error)
        return None

    def run(self):
        try:
            self.result = call_supported(
                self.upload_runner,
                (self.kind,),
                ui=self.ui,
                source_root=self._source_root(),
                target=self._target(),
                cancel_event=self.ui.cancel_event,
                stop_after_current=self.ui.stop_after_current_event,
                preview_result=self.preview_result,
                config=self._config(),
                runtime_paths=self._runtime_paths(),
                **({"session_manager": self.session_manager} if self.session_manager is not None else {}),
                **({"queue_task_id": self.queue_task_id} if self.queue_task_id is not None else {}),
                on_ready=self._ready,
            )
            if not isinstance(self.result, UploadRunResult):
                raise TypeError("上传引擎未返回结构化结果")
        except Exception as exc:
            from ..telegram.tdlib_common import TDLibCancelled
            from ..upload.engine import UploadCancelled

            if isinstance(exc, (CancellationRequested, TDLibCancelled, UploadCancelled)) or self.ui.stop_requested:
                self.result = UploadRunResult("CANCELLED", error=str(exc), cancelled=True)
            else:
                self.ui.error(f"程序停止：{type(exc).__name__}: {exc}")
                write_exception(f"{self.kind} 上传线程失败", exc, source=f"upload/{self.kind}")
                self.result = UploadRunResult("FAILED", error=f"{type(exc).__name__}: {exc}")
        self.completed.emit(self.result)


__all__ = ["AuthBridge", "CacheStatsWorker", "GuiConsoleUI", "ScanWorker", "UploadWorker"]
