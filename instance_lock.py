# -*- coding: utf-8 -*-
"""Cross-process lock for one GUI instance per DATA_DIR."""

from __future__ import annotations

from pathlib import Path


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from PySide6.QtCore import QLockFile
        except ImportError:
            # Headless/test fallback; O_EXCL gives the same cross-process
            # property without importing Qt.
            import os
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                self._lock = "file"
                return True
            except FileExistsError:
                return False
        lock = QLockFile(str(self.path))
        # Let Qt recover a lock left by a crashed process while still keeping
        # a live second instance out.  ``0`` disables stale-lock recovery and
        # could strand users behind a lock after a forced shutdown.
        lock.setStaleLockTime(30_000)
        if not lock.tryLock(0):
            return False
        self._lock = lock
        return True

    def release(self) -> None:
        if self._lock == "file":
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
        elif self._lock is not None:
            try:
                self._lock.unlock()
            except RuntimeError:
                pass
        self._lock = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("TDLib Media Uploader 已在运行")
        return self

    def __exit__(self, *_args):
        self.release()


__all__ = ["InstanceLock"]
