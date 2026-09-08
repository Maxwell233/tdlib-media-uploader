# -*- coding: utf-8 -*-
"""Small, dependency-free persistent logging helpers for the desktop app."""

from __future__ import annotations

from datetime import datetime
import threading
import traceback

from runtime_paths import APP_DATA_DIR


LOG_DIR = APP_DATA_DIR / "logs"
APP_LOG_PATH = LOG_DIR / "app.log"
TDLIB_LOG_PATH = LOG_DIR / "tdlib.log"

_LOCK = threading.RLock()


def _lines(message) -> list[str]:
    text = str(message).replace("\x00", "\\0")
    return text.splitlines() or [""]


def write_app_log(level: str, message, *, source: str = "app") -> None:
    """Append an event to the durable application log.

    Logging must never be able to stop an upload.  I/O errors are therefore
    deliberately swallowed after the normal UI/worker path has continued.
    """

    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    label = str(level).upper().strip() or "INFO"
    origin = str(source).strip() or "app"
    try:
        with _LOCK:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with APP_LOG_PATH.open("a", encoding="utf-8", newline="\n") as file:
                for line in _lines(message):
                    file.write(f"{timestamp} [{label}] [{origin}] {line}\n")
    except OSError:
        pass


def write_exception(message, exc: BaseException, *, source: str = "app") -> None:
    """Append an exception and traceback without exposing it to the caller."""

    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    write_app_log("ERROR", f"{message}\n{details}", source=source)
