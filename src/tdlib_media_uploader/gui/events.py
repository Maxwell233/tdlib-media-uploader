"""Qt event bridges used by the migration-period GUI workers."""

from __future__ import annotations

import threading
from typing import Any, Callable

from ..core.logging import write_app_log
from PySide6.QtCore import QObject, Signal


class AuthBridge(QObject):
    """Bridge authentication prompts from a worker thread to the GUI."""

    requested = Signal(str, bool)

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._event: threading.Event | None = None
        self._value = ""

    def ask(self, prompt: str, password: bool = False) -> str:
        event = threading.Event()
        with self._lock:
            self._event = event
            self._value = ""
        self.requested.emit(prompt, password)
        event.wait(3600)
        with self._lock:
            value = self._value
            self._event = None
        return value

    def answer(self, value: str):
        with self._lock:
            self._value = value
            event = self._event
        if event is not None:
            event.set()


class GuiConsoleUI(QObject):
    """Translate backend callbacks into Qt signals without importing pages."""

    message_added = Signal(str, str)
    progress_changed = Signal(object)
    album_changed = Signal(object)
    target_changed = Signal(object)

    def __init__(
        self,
        auth_bridge: AuthBridge,
        kind: str = "",
        *,
        target_mode_provider: Callable[[], str] | None = None,
    ):
        super().__init__()
        self.auth_bridge = auth_bridge
        self.kind = kind
        self.target_mode_provider = target_mode_provider
        self._client = None
        self._client_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._stop_after_current = threading.Event()

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    @property
    def cancel_event(self):
        """Expose the shared event to scan/media helpers."""

        return self._stop_requested

    @property
    def stop_after_current_event(self):
        """Expose the cooperative stop marker to the V2 upload engine."""

        return self._stop_after_current

    @property
    def safe_stop_requested(self) -> bool:
        return self._stop_after_current.is_set()

    def register_client(self, client):
        with self._client_lock:
            self._client = client
            stop_already_requested = self._stop_requested.is_set()
        if stop_already_requested:
            # Only an immediate stop may cancel a client during startup.
            client.cancel()

    def request_safe_stop(self):
        """Finish the current Album, then stop at the next Album boundary."""

        self._stop_after_current.set()
        self.auth_bridge.answer("")

    def request_immediate_stop(self):
        """Abort the current transport and preserve an ambiguous send."""

        self._stop_requested.set()
        self._stop_after_current.set()
        self.auth_bridge.answer("")
        with self._client_lock:
            client = self._client
        if client is not None:
            client.cancel()

    def request_stop(self):
        """Backward-compatible alias for the high-risk immediate stop."""

        self.request_immediate_stop()

    def prompt(self, text: str, *, password: bool = False) -> str:
        value = self.auth_bridge.ask(text, password)
        if not value and not self.safe_stop_requested and not self.stop_requested:
            self.request_immediate_stop()
        return value

    def _message(self, level: str, text):
        message = str(text)
        level_name = {
            "log": "INFO",
            "info": "INFO",
            "success": "INFO",
            "banner": "INFO",
            "summary": "INFO",
            "warning": "WARNING",
            "error": "ERROR",
        }.get(level, "INFO")
        write_app_log(level_name, message, source=f"gui/{self.kind or 'app'}")
        self.message_added.emit(level, message)

    def log(self, text=""):
        self._message("log", text)

    def info(self, text):
        self._message("info", text)

    def success(self, text):
        self._message("success", text)

    def warning(self, text):
        self._message("warning", text)

    def error(self, text):
        self._message("error", text)

    def banner(self, title: str, subtitle: str = "", *, accent="cyan"):
        del accent
        self._message("banner", f"{title}\n{subtitle}".strip())

    def summary(self, title, rows, *, kind="VIDEO"):
        del kind
        body = [str(title)] + [f"{key}: {value}" for key, value in rows]
        self._message("summary", "\n".join(body))

    def files(self, title, columns, rows, *, kind="VIDEO", caption=None):
        del columns, kind
        suffix = f"\n{caption}" if caption else ""
        self._message("info", f"{title} · {len(list(rows))} 项{suffix}")

    def groups(self, title, rows, *, kind="VIDEO"):
        del kind
        self._message("info", f"{title} · {len(list(rows))} 组")

    def target(self, chat_title, topic_name, chat_id, topic_id):
        target_mode = "forum_topic"
        if self.target_mode_provider is not None:
            try:
                target_mode = str(self.target_mode_provider() or target_mode)
            except Exception:
                pass
        payload = {
            "kind": self.kind,
            "target_mode": target_mode,
            "chat_title": chat_title or "(未命名)",
            "topic_name": topic_name or "",
            "chat_id": chat_id,
            "topic_id": topic_id,
        }
        self.target_changed.emit(payload)
        suffix = f" / {payload['topic_name']}" if payload["topic_name"] else "（频道）"
        self._message("success", f"Telegram 目标：{payload['chat_title']}{suffix}")

    def album(self, *, kind, title, subtitle="", rows=None):
        self.album_changed.emit({
            "kind": kind,
            "title": title,
            "subtitle": subtitle,
            "rows": list(rows or []),
        })

    def confirm_upload(self) -> bool:
        return not self.stop_requested

    def cancelled(self):
        self.warning("已取消，没有开始上传。")

    def progress(self, **kwargs):
        self.progress_changed.emit(dict(kwargs))

    def finish(self):
        return None


__all__ = ["AuthBridge", "GuiConsoleUI"]
