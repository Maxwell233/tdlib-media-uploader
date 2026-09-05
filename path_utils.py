# -*- coding: utf-8 -*-
"""Path helpers that keep Windows UNC/SMB paths stable and local-only."""

from __future__ import annotations

import ntpath
import os
import stat
from pathlib import Path


def _text(path) -> str:
    return os.fspath(path)


def is_unc_path(path) -> bool:
    """Return whether *path* uses a Windows UNC or extended UNC prefix."""
    value = _text(path).replace("/", "\\")
    return value.startswith("\\\\")


def stable_path(path) -> str:
    """Return a case-insensitive, non-resolving identity for a file path.

    ``Path.resolve()`` may ask Windows to resolve a network share.  A share can
    disappear briefly while a directory is being scanned, so state keys and
    UI comparisons must not depend on that network round trip.
    """
    value = _text(path)
    if os.name == "nt":
        if is_unc_path(value):
            return ntpath.normcase(ntpath.normpath(value))
        return ntpath.normcase(ntpath.normpath(ntpath.abspath(value)))
    return os.path.normcase(os.path.normpath(os.path.abspath(value)))


def display_path(path) -> str:
    """Return a normalized path string suitable for a local-file TDLib input."""
    value = _text(path)
    if os.name == "nt" and is_unc_path(value):
        # Preserve the UNC prefix; TDLib/FFmpeg can open it directly.
        return ntpath.normpath(value)
    return str(Path(value).absolute())


def file_mtime(path, fallback: float = 0.0) -> float:
    """Read a file mtime without turning a transient share error into a crash."""
    try:
        return float(Path(path).stat().st_mtime)
    except OSError:
        return fallback


def relative_name(path, root) -> str:
    """Return a stable slash-separated path relative to *root* when possible."""
    # Keep the original spelling for UI/log output (for example ``Posts``
    # rather than the lower-cased form used by ``stable_path``), while still
    # avoiding Path.resolve() and its network-share lookup.
    path_text = ntpath.normpath(_text(path)) if os.name == "nt" else os.path.abspath(_text(path))
    root_text = ntpath.normpath(_text(root)) if os.name == "nt" else os.path.abspath(_text(root))
    try:
        relative = ntpath.relpath(path_text, root_text) if os.name == "nt" else os.path.relpath(path_text, root_text)
    except (OSError, ValueError):
        return Path(path_text).name
    if relative == ".." or relative.startswith(".." + os.sep) or relative.startswith("..\\"):
        return Path(path_text).name
    return relative.replace("\\", "/")


def iter_files(root, extensions):
    """Walk a local or UNC tree, skipping entries unavailable to the share."""
    accepted = {str(ext).lower() for ext in extensions}
    paths = []
    errors = []

    def onerror(error):
        errors.append(str(error))

    try:
        walker = os.walk(_text(root), onerror=onerror)
        for directory, _dirnames, filenames in walker:
            for filename in filenames:
                path = Path(directory) / filename
                if path.suffix.lower() not in accepted:
                    continue
                try:
                    info = path.stat()
                    if stat.S_ISREG(info.st_mode) and info.st_size > 0:
                        paths.append(path)
                except OSError as error:
                    errors.append(f"{path}: {error}")
    except OSError as error:
        errors.append(str(error))
    return paths, errors
