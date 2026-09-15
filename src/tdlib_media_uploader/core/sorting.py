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


def _natural_parts(value: object) -> list[tuple[int, object, str]]:
    """Split *value* into case-folded text and integer runs."""

    parts: list[tuple[int, object, str]] = []
    for part in _NATURAL_PART_RE.split(_text(value)):
        if not part:
            continue
        if part.isdigit():
            parts.append((1, int(part), part))
        else:
            parts.append((0, part.casefold(), part))
    return parts


def natural_compare(left: object, right: object) -> int:
    """Compare names with natural numeric ordering.

    Numeric runs are compared by integer value, then by their original run
    length (``1 < 01 < 001``).  Text runs are case-insensitive primarily and
    use their original Unicode spelling as a deterministic secondary key.
    """

    a_parts = _natural_parts(left)
    b_parts = _natural_parts(right)
    for (a_type, a_value, a_raw), (b_type, b_value, b_raw) in zip(
        a_parts, b_parts
    ):
        if a_type != b_type:
            return -1 if a_type < b_type else 1
        if a_value == b_value:
            if a_type == 1 and len(a_raw) != len(b_raw):
                return -1 if len(a_raw) < len(b_raw) else 1
            if a_raw != b_raw:
                return (a_raw > b_raw) - (a_raw < b_raw)
            continue
        return -1 if a_value < b_value else 1

    if len(a_parts) != len(b_parts):
        return -1 if len(a_parts) < len(b_parts) else 1
    a_text, b_text = _text(left), _text(right)
    return (a_text > b_text) - (a_text < b_text)


def natural_sort(values, *, key: Callable[[_T], object] | None = None) -> list[_T]:
    """Return a naturally ordered copy of *values*.

    The key function is evaluated exactly once per input value.  The input
    sequence is never modified.
    """

    key_func = key or (lambda value: value)
    decorated = [
        (key_func(value), index, value) for index, value in enumerate(values)
    ]

    def compare(left, right) -> int:
        result = natural_compare(left[0], right[0])
        return result or (left[1] - right[1])

    return [value for _, _, value in sorted(decorated, key=cmp_to_key(compare))]


def natural_sort_key(value: object) -> tuple[tuple[int, object, str], ...]:
    """Expose a comparable natural key for callers that need one."""

    return tuple(_natural_parts(value))


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


def relative_path_compare(
    left: object, right: object, root: object | None = None
) -> int:
    """Compare relative path components using :func:`natural_compare`."""

    left_parts = _relative_components(left, root)
    right_parts = _relative_components(right, root)
    for left_part, right_part in zip(left_parts, right_parts):
        result = natural_compare(left_part, right_part)
        if result:
            return result
    if len(left_parts) != len(right_parts):
        return -1 if len(left_parts) < len(right_parts) else 1

    # ``natural_compare`` already resolved leading-zero numeric runs and
    # case ties component by component.  Keep the complete relative spelling
    # as a final deterministic fallback for paths that compare equal there.
    left_raw = "/".join(left_parts)
    right_raw = "/".join(right_parts)
    return (left_raw > right_raw) - (left_raw < right_raw)


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
        decorated.append((path, mtime, index, value))

    def compare(left, right) -> int:
        if normalized_mode == "mtime" and left[1] != right[1]:
            return -1 if left[1] < right[1] else 1
        result = relative_path_compare(left[0], right[0], root)
        return result or (left[2] - right[2])

    return [value for _, _, _, value in sorted(decorated, key=cmp_to_key(compare))]


__all__ = [
    "media_path_sort",
    "natural_compare",
    "natural_sort",
    "natural_sort_key",
    "relative_name",
    "relative_path_compare",
]
