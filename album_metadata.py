# -*- coding: utf-8 -*-
"""Stable Album captions shared by the CLI and PySide6 preview.

The metadata files intentionally live beside the application and contain only
local file fingerprints plus user-entered caption text.  They are separate
from TDLib upload state so editing a caption never invalidates resumable
uploads.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from path_utils import stable_path


PROJECT_DIR = Path(__file__).resolve().parent


def path_for(kind: str) -> Path:
    return PROJECT_DIR / (
        ".video_album_captions.json" if kind == "video"
        else ".image_album_captions.json"
    )


def _item_path(item):
    return item["path"] if isinstance(item, dict) else item


def _item_signature(item) -> str:
    path = Path(_item_path(item))
    try:
        stat = path.stat()
        return f"{stable_path(path)}|{stat.st_size}|{stat.st_mtime_ns}"
    except OSError:
        return stable_path(path)


def album_key(kind: str, group_label: str, items) -> str:
    """Return a stable key for a complete (not pending-only) Album plan."""
    raw = "\n".join(
        [kind, str(group_label), *(_item_signature(item) for item in items)]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def compose_caption(base_label: str, custom_text: str = "", separator: str = " · ") -> str:
    base = str(base_label or "").strip()
    custom = str(custom_text or "").strip()
    if not custom:
        return base
    if not base:
        return custom
    return f"{base}{separator}{custom}"


def filename_description(items, *, max_chars: int = 950) -> str:
    """Format an Album's local filenames as a numbered description."""
    lines = []
    for index, item in enumerate(items, 1):
        name = Path(_item_path(item)).name
        lines.append(f"{index}. {name}")
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def with_filename_description(caption: str, items, enabled: bool) -> str:
    if not enabled:
        return caption
    description = filename_description(items)
    if not description:
        return caption
    result = f"{caption}\n{description}" if caption else description
    # Telegram media captions are limited; keep the generated description
    # useful even when an Album contains long Windows filenames.
    return result[:1024].rstrip() if len(result) > 1024 else result


class CaptionStore:
    """Small atomic JSON store for per-Album caption overrides."""

    def __init__(self, kind: str):
        self.kind = kind
        self.path = path_for(kind)
        self._lock = threading.Lock()
        self._snapshot = None

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def get(self, key: str, default_label: str) -> dict:
        # A store belongs to one plan/scan. Parse once, not once per Album.
        if self._snapshot is None:
            self._snapshot = self._load()
        record = self._snapshot.get(key, {})
        if not isinstance(record, dict):
            record = {}
        return {
            "base_label": str(record.get("base_label", default_label) or default_label),
            "custom_text": str(record.get("custom_text", "") or ""),
        }

    def set(self, key: str, *, base_label: str, custom_text: str) -> None:
        with self._lock:
            data = self._load()
            data[key] = {
                "base_label": str(base_label or "").strip(),
                "custom_text": str(custom_text or "").strip(),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.path)
            self._snapshot = data


def make_plan(kind: str, group_label: str, items: list, album_size: int, state=None) -> list[dict]:
    """Split complete groups into stable Albums and annotate pending files."""
    store = CaptionStore(kind)
    result = []
    for offset in range(0, len(items), int(album_size)):
        full_items = list(items[offset:offset + int(album_size)])
        pending_items = [
            item for item in full_items
            if state is None or not state.is_completed(_item_path(item))
        ]
        key = album_key(kind, group_label, full_items)
        result.append({
            "key": key,
            "group_label": str(group_label),
            "number": offset // int(album_size) + 1,
            "items": full_items,
            "pending_items": pending_items,
            "caption": store.get(key, str(group_label)),
        })
    return result
