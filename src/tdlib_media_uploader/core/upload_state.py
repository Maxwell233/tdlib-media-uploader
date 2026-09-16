# -*- coding: utf-8 -*-
"""Shared V1.9 upload checkpoint persistence.

The three media uploaders use the same snapshot and target identity rules but
retain thin wrappers for their existing progress APIs.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from .identity import canonical_target, media_file_signature, relative_media_path, target_identity
from .filesystem_legacy import file_snapshot, stable_path


def _snapshot_for_item(item, path, snapshot_provider=None):
    if isinstance(item, dict):
        if item.get("size") is not None and item.get("mtime_ns") is not None:
            return int(item["size"]), int(item["mtime_ns"])
        if item.get("scan_size") is not None and item.get("scan_mtime_ns") is not None:
            return int(item["scan_size"]), int(item["scan_mtime_ns"])
    if snapshot_provider is not None:
        try:
            snapshot = snapshot_provider(path)
        except (OSError, ValueError, TypeError):
            snapshot = None
        if snapshot is not None:
            if hasattr(snapshot, "size") and hasattr(snapshot, "mtime_ns"):
                return int(snapshot.size), int(snapshot.mtime_ns)
            return int(snapshot[0]), int(snapshot[1])
    return file_snapshot(path)


class UploadState:
    """One atomic checkpoint format for video, image and mixed uploads."""

    VERSION = 2

    def __init__(
        self,
        *,
        kind: str,
        source_root,
        target=None,
        state_dir,
        reset: bool = False,
        filename_prefix: str | None = None,
        snapshot_provider=None,
    ):
        normalized_kind = str(kind).strip().lower()
        if normalized_kind not in {"video", "image", "mixed"}:
            raise ValueError(f"未知媒体类型：{kind}")
        self.kind = normalized_kind
        self.source_root = Path(source_root)
        self.snapshot_provider = snapshot_provider
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.target = canonical_target(target or {})
        self._target_mode = self.target.get("target_mode", "")
        self._chat_id = int(self.target.get("chat_id", 0) or 0)
        self._forum_topic_id = int(self.target.get("forum_topic_id", 0) or 0)
        self._channel_chat_id = int(self.target.get("channel_chat_id", 0) or 0)
        identity = json.dumps(
            {
                "kind": self.kind,
                "source_root": stable_path(self.source_root),
                "target": self.target,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = sha256(identity.encode("utf-8")).hexdigest()[:24]
        prefix = filename_prefix or f"{self.kind}_upload_state"
        self.path = self.state_dir / f"{prefix}_{digest}.json"
        self.lock = threading.RLock()
        if reset and self.path.exists():
            self.path.unlink()
        self.data = self._load()

    def _new(self):
        return {
            "version": self.VERSION,
            "kind": self.kind,
            "source_root": stable_path(self.source_root),
            "target": dict(self.target),
            "completed": {},
        }

    def _load(self):
        if not self.path.exists():
            data = self._new()
            self._save(data)
            return data
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(f"断点文件读取失败：{self.path}\n{exc}") from exc
        if not isinstance(data, dict) or data.get("version") != self.VERSION:
            raise RuntimeError(f"V1.9 断点文件版本不兼容：{self.path}")
        data.setdefault("completed", {})
        return data

    def _save(self, data):
        data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def signature(self, item_or_path, snapshot=None) -> str:
        item = item_or_path if isinstance(item_or_path, dict) else None
        path = Path(item["path"] if item else item_or_path)
        values = snapshot or _snapshot_for_item(item, path, self.snapshot_provider)
        if values is None:
            raise OSError(f"文件暂时不可读取：{path}")
        return media_file_signature(path, root=self.source_root, snapshot=values)

    def is_completed(self, item_or_path) -> bool:
        try:
            return self.signature(item_or_path) in self.data.get("completed", {})
        except (OSError, ValueError, TypeError):
            return False

    def mark_album_completed(self, items, message_ids=None):
        values = list(items or [])
        ids = list(message_ids or [])
        with self.lock:
            for index, raw_item in enumerate(values):
                item = raw_item if isinstance(raw_item, dict) else {"path": raw_item}
                path = Path(item["path"])
                snapshot = _snapshot_for_item(item, path, self.snapshot_provider)
                if snapshot is None:
                    raise RuntimeError(f"上传完成但无法记录{self.kind}断点：{path}")
                signature = self.signature(item, snapshot)
                record = {
                    "relative_path": relative_media_path(path, self.source_root),
                    "size": int(snapshot[0]),
                    "mtime_ns": int(snapshot[1]),
                    "message_id": ids[index] if index < len(ids) else None,
                    "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                if isinstance(item, dict):
                    capture_time = item.get("capture_time")
                    record["capture_time"] = (
                        capture_time.isoformat() if hasattr(capture_time, "isoformat") else capture_time
                    )
                for key in ("media_kind", "group_name", "month_key", "date_tag"):
                    if isinstance(item, dict) and item.get(key) is not None:
                        record[key] = item[key]
                self.data["completed"][signature] = record
            self._save(self.data)


__all__ = ["UploadState"]
