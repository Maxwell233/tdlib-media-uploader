# -*- coding: utf-8 -*-
"""Small, dependency-free persistent logging helpers for the desktop app."""

from __future__ import annotations

from datetime import datetime
import threading
import traceback
import os

from runtime_paths import APP_DATA_DIR


LOG_DIR = APP_DATA_DIR / "logs"
APP_LOG_PATH = LOG_DIR / "app.log"
TDLIB_LOG_PATH = LOG_DIR / "tdlib.log"

_LOCK = threading.RLock()
MAX_APP_LOG_BYTES = 10 * 1024 * 1024
APP_LOG_BACKUPS = 3


def _rotate_app_log() -> None:
    """Keep a bounded set of UTF-8 application logs."""

    try:
        if not APP_LOG_PATH.exists() or APP_LOG_PATH.stat().st_size < MAX_APP_LOG_BYTES:
            return
        oldest = LOG_DIR / f"app.log.{APP_LOG_BACKUPS}"
        if oldest.exists():
            oldest.unlink()
        for index in range(APP_LOG_BACKUPS - 1, 0, -1):
            source = LOG_DIR / f"app.log.{index}"
            if source.exists():
                os.replace(source, LOG_DIR / f"app.log.{index + 1}")
        os.replace(APP_LOG_PATH, LOG_DIR / "app.log.1")
    except OSError:
        # Logging must never interfere with a scan or upload.
        return


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
            _rotate_app_log()
            with APP_LOG_PATH.open("a", encoding="utf-8", newline="\n") as file:
                for line in _lines(message):
                    file.write(f"{timestamp} [{label}] [{origin}] {line}\n")
    except OSError:
        pass


def write_exception(message, exc: BaseException, *, source: str = "app") -> None:
    """Append an exception and traceback without exposing it to the caller."""

    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    write_app_log("ERROR", f"{message}\n{details}", source=source)
