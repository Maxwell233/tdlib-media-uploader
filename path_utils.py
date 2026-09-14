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
        return float(os.stat(path).st_mtime)
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


def _entry_suffix(name: str) -> str:
    """Return the pathlib-compatible suffix for a directory entry name."""
    dot = name.rfind(".")
    return name[dot:] if 0 < dot < len(name) - 1 else ""


def iter_files(root, extensions):
    """Walk a local or UNC tree, skipping entries unavailable to the share."""
    accepted = {str(ext).lower() for ext in extensions}
    paths = []
    errors = []

    # Keep the traversal iterative. A recursive scanner can hit Python's
    # recursion limit on exported camera/archive trees with many nested
    # folders, while os.walk handles those trees without growing the call
    # stack. ``follow_symlinks=False`` retains os.walk's default behavior.
    stack = [_text(root)]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue

                        # Avoid constructing Path objects for files that will
                        # be rejected by the extension filter. This spelling
                        # matches pathlib.Path.suffix for names such as
                        # ``photo.`` and ``.hidden`` (both have no suffix).
                        ext = _entry_suffix(entry.name)
                        if ext.lower() not in accepted:
                            continue
                        info = entry.stat()
                        if stat.S_ISREG(info.st_mode) and info.st_size > 0:
                            paths.append(Path(entry.path))
                    except OSError as error:
                        errors.append(f"{entry.path}: {error}")
        except OSError as error:
            errors.append(str(error))

    return paths, errors
