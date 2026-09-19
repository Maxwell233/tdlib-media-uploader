"""Deterministic, GUI-free ordering helpers for media paths.

The V1 scanner sorts path components naturally: text is compared without
case, consecutive digit runs are compared numerically, and the original
spelling is retained as a deterministic tie-breaker.  This module is the
stand-alone home for that behavior during the V2 extraction.
"""

from __future__ import annotations

import ntpath
import os
import re
from functools import cmp_to_key
from typing import Callable, TypeVar


_T = TypeVar("_T")
_NATURAL_PART_RE = re.compile(r"(\d+)")


def _text(value: object) -> str:
    return os.fspath(value) if hasattr(value, "__fspath__") else str(value)


def _natural_parts(value: object) -> tuple[tuple[int, object, int, str], ...]:
    """Split *value* into an orderable tuple of case-folded text and integer runs."""

    parts: list[tuple[int, object, int, str]] = []
    for part in _NATURAL_PART_RE.split(_text(value)):
        if not part:
            continue
        if part.isdigit():
            # Include len(part) as the 3rd element so shorter numeric runs evaluate as smaller
            # when values are equal (e.g. 1 < 01 < 001).
            parts.append((1, int(part), len(part), part))
        else:
            # 3rd element padded with 0 for tuple symmetry
            parts.append((0, part.casefold(), 0, part))
    return tuple(parts)


def natural_compare(left: object, right: object) -> int:
    """Compare names with natural numeric ordering.

    Numeric runs are compared by integer value, then by their original run
    length (``1 < 01 < 001``).  Text runs are case-insensitive primarily and
    use their original Unicode spelling as a deterministic secondary key.
    """

    a_parts = _natural_parts(left)
    b_parts = _natural_parts(right)
    if a_parts == b_parts:
        a_text, b_text = _text(left), _text(right)
        if a_text == b_text:
            return 0
        return -1 if a_text < b_text else 1

    return -1 if a_parts < b_parts else 1


def natural_sort(values, *, key: Callable[[_T], object] | None = None) -> list[_T]:
    """Return a naturally ordered copy of *values*.

    The key function is evaluated exactly once per input value.  The input
    sequence is never modified.
    """

    key_func = key or (lambda value: value)

    # Python tuple sorting naturally handles natural_compare logic:
    # 1. parts (case-insensitive strings and value-aware ints)
    # 2. original text string as fallback
    # 3. index to keep sort stable
    decorated = []
    for index, value in enumerate(values):
        key_val = key_func(value)
        decorated.append((_natural_parts(key_val), _text(key_val), index, value))

    return [value for _, _, _, value in sorted(decorated)]


def natural_sort_key(value: object) -> tuple[tuple[int, object, int, str], ...]:
    """Expose a comparable natural key for callers that need one."""

    return _natural_parts(value)


def relative_name(path: object, root: object | None = None) -> str:
    """Return a stable slash-separated name relative to *root* when possible."""

    path_text = _text(path)
    if root is None:
        return path_text.replace("\\", "/")

    if os.name == "nt":
        path_text = ntpath.normpath(path_text)
        root_text = ntpath.normpath(_text(root))
        relpath = ntpath.relpath
        parent_marker = "..\\"
        separator = "\\"
    else:
        path_text = os.path.abspath(path_text)
        root_text = os.path.abspath(_text(root))
        relpath = os.path.relpath
        parent_marker = ".." + os.sep
        separator = os.sep

    try:
        relative = relpath(path_text, root_text)
    except (OSError, ValueError):
        return ntpath.basename(path_text) if os.name == "nt" else os.path.basename(path_text)
    if relative == ".." or relative.startswith(parent_marker):
        return ntpath.basename(path_text) if os.name == "nt" else os.path.basename(path_text)
    return relative.replace(separator, "/").replace("\\", "/")


def _relative_components(path: object, root: object | None = None) -> tuple[str, ...]:
    value = relative_name(path, root)
    return tuple(component for component in value.split("/") if component not in {"", "."})


def _relative_components_parts(path: object, root: object | None = None) -> tuple[tuple[tuple[int, object, int, str], ...], ...]:
    return tuple(_natural_parts(c) for c in _relative_components(path, root))


def relative_path_compare(
    left: object, right: object, root: object | None = None
) -> int:
    """Compare relative path components using :func:`natural_compare`."""

    a_parts = _relative_components_parts(left, root)
    b_parts = _relative_components_parts(right, root)

    if a_parts != b_parts:
        return -1 if a_parts < b_parts else 1

    # ``natural_compare`` already resolved leading-zero numeric runs and
    # case ties component by component.  Keep the complete relative spelling
    # as a final deterministic fallback for paths that compare equal there.
    left_raw = "/".join(_relative_components(left, root))
    right_raw = "/".join(_relative_components(right, root))
    if left_raw == right_raw:
        return 0
    return -1 if left_raw < right_raw else 1


def _default_mtime(path: object) -> int | float:
    try:
        info = os.stat(path)
    except OSError:
        return 0
    return getattr(info, "st_mtime_ns", info.st_mtime)


def media_path_sort(
    values,
    root: object,
    *,
    mode: str = "name",
    path_key: Callable[[object], object] | None = None,
    mtime_key: Callable[[object], int | float] | None = None,
) -> list:
    """Sort media values by relative path or oldest modification time.

    ``mode="path"`` is retained as the V1 alias for name sorting.  The
    optional key functions allow callers to sort dictionaries or model
    objects without converting them first.
    """

    normalized_mode = str(mode).strip().lower()
    if normalized_mode == "path":
        normalized_mode = "name"
    if normalized_mode not in {"name", "mtime"}:
        raise ValueError(f"不支持的媒体排序方式：{mode}")

    path_func = path_key or (lambda value: value)
    decorated = []

    for index, value in enumerate(values):
        path = path_func(value)
        mtime = None
        if normalized_mode == "mtime":
            try:
                mtime = mtime_key(value) if mtime_key is not None else _default_mtime(path)
            except (OSError, TypeError, ValueError):
                mtime = 0

        parts = _relative_components_parts(path, root)
        raw_str = "/".join(_relative_components(path, root))

        decorated.append((mtime, parts, raw_str, index, value))

    if normalized_mode == "mtime":
        return [value for _, _, _, _, value in sorted(decorated, key=lambda x: (x[0], x[1], x[2], x[3]))]
    else:
        return [value for _, _, _, _, value in sorted(decorated, key=lambda x: (x[1], x[2], x[3]))]


__all__ = [
    "media_path_sort",
    "natural_compare",
    "natural_sort",
    "natural_sort_key",
    "relative_name",
    "relative_path_compare",
]
