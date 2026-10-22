"""Private, atomic persistence for application-owned files.

POSIX writes sync both the file contents and the directory entry. Windows
does not support opening directories with ``os.open`` for ``fsync``; there we
flush the file before the atomic replacement and keep the inherited ACLs.
I/O errors propagate so callers cannot mistake an uncertain write for a
durable checkpoint.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Callable, TextIO


def ensure_private_directory(path: Path) -> Path:
    """Create a private directory without changing existing ancestor modes."""

    path = Path(path)
    created = not path.exists()
    if created:
        # mkdir(parents=True) applies its mode only to the final component.
        # Create missing parents individually so they are private as well.
        if not path.parent.exists():
            ensure_private_directory(path.parent)
    path.mkdir(mode=0o700, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o700)
    if created:
        fsync_directory(path.parent)
    return path


def fsync_directory(path: Path) -> None:
    """Persist a directory mutation on POSIX; Windows uses the file flush."""

    if sys.platform == "win32":
        return
    descriptor = os.open(Path(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, write: Callable[[TextIO], None]) -> None:
    path = Path(path)
    ensure_private_directory(path.parent)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            # mkstemp uses 0600 on POSIX, including when replacing a broader
            # existing config file. Never expose credentials during a write.
            if os.name == "posix":
                os.fchmod(stream.fileno(), 0o600)
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically persist UTF-8 text with private POSIX file permissions."""

    _atomic_write(path, lambda stream: stream.write(text))


def atomic_write_json(path: Path, data: Any) -> None:
    """Atomically persist JSON with the shared checkpoint durability rules."""

    _atomic_write(path, lambda stream: json.dump(data, stream, ensure_ascii=False, indent=2))


def durable_unlink(path: Path) -> None:
    """Remove a file and persist its removal before reporting success."""

    path = Path(path)
    path.unlink(missing_ok=True)
    fsync_directory(path.parent)


__all__ = [
    "atomic_write_json", "atomic_write_text", "durable_unlink",
    "ensure_private_directory", "fsync_directory",
]
