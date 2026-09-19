# -*- coding: utf-8 -*-
"""History persistence service for TDLib Media Uploader GUI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from ..config.paths import HISTORY_PATH


def load_history() -> list[dict[str, Any]]:
    """Load upload history records from disk safely."""
    if not HISTORY_PATH.exists():
        return []
    try:
        value = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except Exception:
        return []


def save_history(records: list[dict[str, Any]]) -> None:
    """Save the most recent upload records to disk."""
    temporary = None
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=HISTORY_PATH.parent,
            prefix=f".{HISTORY_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(records[-100:], stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, HISTORY_PATH)
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def clear_history() -> None:
    """Clear all saved history records."""
    save_history([])


# Compatibility aliases
_load_history = load_history
_save_history = save_history


__all__ = [
    "HISTORY_PATH",
    "_load_history",
    "_save_history",
    "clear_history",
    "load_history",
    "save_history",
]
