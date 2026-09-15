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


def run_with_instance_lock(callback, *args, lock_held: bool = False, lock_path=None, **kwargs):
    """Run a supported entry point while holding the shared data lock.

    The GUI already owns ``DATA_DIR/app.lock`` while it invokes an uploader
    in its worker thread.  It passes ``lock_held=True`` to avoid attempting a
    non-reentrant second lock.  Direct Python entry points acquire the exact
    same lock, preventing concurrent TDLib database/state writers.
    """

    if lock_held:
        return callback(*args, **kwargs)
    if lock_path is None:
        from runtime_paths import DATA_DIR

        lock_path = DATA_DIR / "app.lock"
    lock = InstanceLock(lock_path)
    if not lock.acquire():
        raise RuntimeError("TDLib Media Uploader 已在运行")
    try:
        return callback(*args, **kwargs)
    finally:
        lock.release()


__all__ = ["InstanceLock", "run_with_instance_lock"]
