"""Qt worker boundary for V2 scans and uploads.

Workers depend on explicit callbacks supplied by the compatibility GUI.  They
do not import the root widget module, so the package can own thread lifecycle
and cancellation while preview formatting remains reversible.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import importlib
from pathlib import Path
import threading
from typing import Any

from ..core.logging import write_exception
from PySide6.QtCore import QThread, Signal

from .events import AuthBridge, GuiConsoleUI


MEDIA_KINDS = ("video", "image", "mixed")


def _require_kind(kind: str) -> str:
    normalized = str(kind).strip().lower()
    if normalized not in MEDIA_KINDS:
        raise ValueError(f"未知媒体类型：{kind}")
    return normalized


def _load_upload_runner() -> Callable[..., Any]:
    from .integration import run_v2_upload

    return run_v2_upload


class ScanWorker(QThread):
    """Run the GUI preview adapter without blocking the Qt event loop."""

    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)
    progress_changed = Signal(str, object)

    def __init__(self, kind: str, *, scan_runner: Callable[..., Mapping[str, Any]]):
        super().__init__()
        self.kind = _require_kind(kind)
        self.scan_runner = scan_runner
        self.cancel_event = threading.Event()

    def request_stop(self):
        """Request cancellation without terminating the worker thread."""

        self.cancel_event.set()

    def _report_progress(self, payload: dict):
        self.progress_changed.emit(self.kind, payload)

    def run(self):
        try:
            result = self.scan_runner(
                self.kind,
                progress_callback=self._report_progress,
                cancel_event=self.cancel_event,
            )
            if result.get("cancelled"):
                self.cancelled.emit(result)
            else:
                self.completed.emit(result)
        except Exception as exc:
            write_exception(f"{self.kind} 扫描失败", exc, source=f"scan/{self.kind}")
            self.failed.emit(f"扫描失败：{type(exc).__name__}: {exc}")


class UploadWorker(QThread):
    """Run one V2 upload and map its result to the existing task-center UI."""

    completed = Signal(bool, str)

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
    ):
        super().__init__()
        self.kind = _require_kind(kind)
        self.preview_result = dict(preview_result or {})
        self.upload_runner = upload_runner or _load_upload_runner()
        self.target_provider = target_provider or (lambda _kind: {})
        self.source_root_provider = source_root_provider
        self.config = config
        self.runtime_paths = runtime_paths
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
        self.ui.request_stop()

    def run(self):
        try:
            result = self.upload_runner(
                self.kind,
                ui=self.ui,
                source_root=self._source_root(),
                target=self._target(),
                cancel_event=self.ui.cancel_event,
                preview_result=self.preview_result,
                config=self._config(),
                runtime_paths=self._runtime_paths(),
            )
            status = str(getattr(result, "status", "")).upper()
            if self.ui.stop_requested or bool(getattr(result, "cancelled", False)):
                self.completed.emit(False, "任务已立即停止；完整完成的 Album 已保存断点。")
            elif status == "COMPLETED":
                self.completed.emit(True, "上传任务完成。")
            elif status == "UNKNOWN":
                self.completed.emit(False, "任务存在未确认的 Telegram Album；请先在‘未确认上传’中核对。")
            elif status == "PARTIAL":
                deferred = len(getattr(result, "deferred_items", ()) or ())
                failed = len(getattr(result, "failed_items", ()) or ())
                self.completed.emit(
                    False,
                    f"任务部分完成：暂缓 {deferred} 个，失败 {failed} 个；修复后可重新扫描。",
                )
            else:
                message = str(getattr(result, "error", "") or "任务未完成")
                self.completed.emit(False, f"任务失败：{message}")
        except Exception as exc:
            if bool(getattr(exc, "cancelled", False)) or type(exc).__name__ == "TDLibCancelled" or self.ui.stop_requested:
                self.completed.emit(False, "任务已立即停止；完整完成的 Album 已保存断点。")
            else:
                self.ui.error(f"程序停止：{type(exc).__name__}: {exc}")
                write_exception(
                    f"{self.kind} 上传线程失败",
                    exc,
                    source=f"upload/{self.kind}",
                )
                self.completed.emit(False, f"任务失败：{type(exc).__name__}: {exc}")


__all__ = ["AuthBridge", "GuiConsoleUI", "ScanWorker", "UploadWorker"]
