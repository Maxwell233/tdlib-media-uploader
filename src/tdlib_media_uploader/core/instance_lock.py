# -*- coding: utf-8 -*-
"""Cross-process lock for one GUI instance per DATA_DIR."""

from __future__ import annotations

from PySide6.QtCore import QLockFile


class InstanceLock:
    def __init__(self, path):
        self._lock = QLockFile(str(path))
        # Let Qt recover a lock left by a crashed process while still keeping
        # a live second instance out.  ``0`` disables stale-lock recovery and
        # could strand users behind a lock after a forced shutdown.
        self._lock.setStaleLockTime(30_000)

    def acquire(self) -> bool:
        return self._lock.tryLock(0)

    def release(self) -> None:
        self._lock.unlock()


__all__ = ["InstanceLock"]
