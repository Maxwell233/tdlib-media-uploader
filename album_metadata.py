# -*- coding: utf-8 -*-
"""Stable Album captions shared by the CLI and PySide6 preview.

The metadata files live under ``DATA_DIR/captions`` and contain only local
file fingerprints plus user-entered caption text.  They are separate from
TDLib upload state so editing a caption never invalidates resumable uploads.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from path_utils import stable_path
from media_identity import media_file_identity
from runtime_paths import APP_DATA_DIR, CAPTIONS_DIR


PROJECT_DIR = CAPTIONS_DIR


class CaptionLimitError(ValueError):
    """A user-authored caption cannot fit Telegram's current limit."""


def path_for(kind: str) -> Path:
    names = {
        "video": "video.json",
        "image": "image.json",
        "mixed": "mixed.json",
    }
    normalized = str(kind).strip().lower()
    if normalized not in names:
        raise ValueError(f"未知媒体类型：{kind}")
    # A patched CaptionStore project directory is an explicit integration
    # override (used by tools/tests); retain its requested filename. The
    # normal V1.9 path is always DATA_DIR/captions/<kind>.json.
    if PROJECT_DIR != CAPTIONS_DIR:
        legacy_names = {
            "video": ".video_album_captions.json",
            "image": ".image_album_captions.json",
            "mixed": ".mixed_album_captions.json",
        }
        return PROJECT_DIR / legacy_names[normalized]
    return PROJECT_DIR / names[normalized]


def _item_path(item):
    return item["path"] if isinstance(item, dict) else item


def _item_signature(item, *, root=None, snapshot_provider=None) -> str:
    path = Path(_item_path(item))
    if isinstance(item, dict):
        size = item.get("scan_size", item.get("size"))
        mtime_ns = item.get("scan_mtime_ns", item.get("mtime_ns"))
        if size is not None and mtime_ns is not None:
            identity_root = item.get("source_root") or root or item.get("group_path") or path.parent
            return media_file_identity(path, root=identity_root, size=size, mtime_ns=mtime_ns)
    if snapshot_provider is not None:
        snapshot = snapshot_provider(path)
        if snapshot is not None:
            identity_root = root or path.parent
            return media_file_identity(path, root=identity_root, snapshot=snapshot)
    # Album identity is deliberately snapshot-only.  A late stat here would
    # make the key depend on whether an SMB/NAS share happens to be online
    # after the scan, which could change the Album boundary or caption key.
    raise ValueError(
        f"Album key 缺少扫描快照：{path}；请传入 scan_size/scan_mtime_ns 或 snapshot_provider"
    )


def album_key(kind: str, group_label: str, items, *, root=None, snapshot_provider=None) -> str:
    """Return a stable key for a complete (not pending-only) Album plan."""
    raw = "\n".join(
        [
            kind,
            str(group_label),
            *(
                _item_signature(item, root=root, snapshot_provider=snapshot_provider)
                for item in items
            ),
        ]
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


def filename_description(
    items,
    *,
    numbered: bool = True,
    max_chars: int = 950,
) -> str:
    """Format an Album's local filenames for a generated caption."""
    lines = []
    for index, item in enumerate(items, 1):
        # Captions identify the media rather than its transport format. Remove
        # only the final suffix, preserving names such as ``archive.tar`` from
        # ``archive.tar.gz``.
        name = Path(_item_path(item)).stem
        lines.append(f"{index}. {name}" if numbered else name)
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def with_filename_description(
    caption: str,
    items,
    enabled: bool,
    numbered: bool = True,
    max_chars: int = 1024,
) -> str:
    limit = max(1, int(max_chars))
    caption = str(caption or "")
    if len(caption) > limit:
        raise CaptionLimitError(
            f"用户标题超过 Telegram Caption 限制（{len(caption)}/{limit} 字符）"
        )
    if not enabled:
        return caption
    remaining = max(1, limit - len(caption) - (1 if caption else 0))
    description = filename_description(items, numbered=numbered, max_chars=remaining)
    if not description:
        return caption
    result = f"{caption}\n{description}" if caption else description
    return result[:limit].rstrip() if len(result) > limit else result


def validate_caption(caption: str, limit: int = 1024) -> str:
    """Validate a final caption without silently truncating user text."""

    value = str(caption or "")
    maximum = max(1, int(limit))
    if len(value) > maximum:
        raise CaptionLimitError(
            f"标题超过 Telegram Caption 限制（{len(value)}/{maximum} 字符）"
        )
    return value


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


def make_plan(
    kind: str,
    group_label: str,
    items: list,
    album_size: int,
    state=None,
    *,
    root=None,
    snapshot_provider=None,
) -> list[dict]:
    """Split complete groups into stable Albums and annotate pending files."""
    store = CaptionStore(kind)
    result = []
    for offset in range(0, len(items), int(album_size)):
        full_items = list(items[offset:offset + int(album_size)])
        pending_items = [
            item for item in full_items
            if state is None or not state.is_completed(item)
        ]
        key = album_key(
            kind,
            group_label,
            full_items,
            root=root,
            snapshot_provider=snapshot_provider,
        )
        result.append({
            "key": key,
            "group_label": str(group_label),
            "number": offset // int(album_size) + 1,
            "items": full_items,
            "pending_items": pending_items,
            "caption": store.get(key, str(group_label)),
        })
    return result
