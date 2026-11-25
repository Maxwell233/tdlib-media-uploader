# -*- coding: utf-8 -*-
"""Durable upload checkpoints for video, image and mixed uploads."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from .identity import canonical_target, media_file_signature, relative_media_path, target_identity
from .filesystem import file_snapshot, stable_path
from .persistence import atomic_write_json, ensure_private_directory
from .uploader_database import database_for, encode_record


class LegacyCheckpointError(RuntimeError):
    """An individual legacy checkpoint cannot be safely imported."""


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
    """Store an Album's checkpoints in one durable write."""

    VERSION = 2

    def __init__(
        self,
        *,
        kind: str,
        source_root,
        target=None,
        state_dir,
        reset: bool = False,
        create: bool = True,
        filename_prefix: str | None = None,
        snapshot_provider=None,
        database=None,
    ):
        normalized_kind = str(kind).strip().lower()
        if normalized_kind not in {"video", "image", "mixed"}:
            raise ValueError(f"未知媒体类型：{kind}")
        self.kind = normalized_kind
        self.source_root = Path(source_root)
        self.snapshot_provider = snapshot_provider
        self.state_dir = Path(state_dir)
        ensure_private_directory(self.state_dir)
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
        if database is None:
            from ..config import paths
            if self.state_dir == getattr(paths, f"{self.kind.upper()}_STATE_DIR"):
                database = database_for(paths.DATA_DIR / "uploader.db")
        self.database = database
        if database is None:
            self.data = self._load(reset=reset, create=create)
        else:
            self._source_key = database.key(self.path)
            self.data = self._load_database(reset=reset, create=create)

    def _new(self):
        return {
            "version": self.VERSION,
            "kind": self.kind,
            "source_root": stable_path(self.source_root),
            "target": dict(self.target),
            "completed": {},
        }

    def _load(self, *, reset=False, create=True):
        try:
            if reset:
                raise FileNotFoundError
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = self._new()
            if create or reset:
                self._save(data)
            return data
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(f"断点文件读取失败：{self.path}\n{exc}") from exc
        if not isinstance(data, dict) or data.get("version") != self.VERSION:
            raise RuntimeError(f"V1.9 断点文件版本不兼容：{self.path}")
        if not isinstance(data.get("completed", {}), dict):
            raise RuntimeError(f"断点文件格式不兼容：{self.path}")
        data.setdefault("completed", {})
        return data

    def _load_database(self, *, reset, create):
        def import_legacy(connection):
            # Before per-kind directories were introduced, state files lived
            # directly under data/state with the same hashed filenames.
            candidates = (self.path, self.path.parent.parent / self.path.name)
            for candidate in candidates:
                try:
                    raw = json.loads(candidate.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, TypeError) as exc:
                    raise LegacyCheckpointError(f"断点文件读取失败：{candidate}\n{exc}") from exc
                expected = self._new()
                if (not isinstance(raw, dict) or not isinstance(raw.get("completed", {}), dict)
                        or any(raw.get(field) != expected[field] for field in (
                            "version", "kind", "source_root", "target"
                        ))):
                    raise LegacyCheckpointError(f"V1.9 断点文件格式不兼容：{candidate}")
                self._store_records(connection, raw.get("completed", {}), metadata=raw)
                break

        self.database.migrate("checkpoint", self._source_key, import_legacy)
        if reset:
            with self.database.write() as connection:
                connection.execute("DELETE FROM checkpoint_sources WHERE source_key=?", (self._source_key,))
                self._store_records(connection, {}, metadata=self._new())
        with self.database.read() as connection:
            source = connection.execute(
                "SELECT metadata FROM checkpoint_sources WHERE source_key=?", (self._source_key,)
            ).fetchone()
            if source is not None:
                data = json.loads(source[0])
                data["completed"] = {
                    key: json.loads(value) for key, value in connection.execute(
                        "SELECT signature, metadata FROM checkpoint_items WHERE source_key=?", (self._source_key,)
                    )
                }
                return data
        data = self._new()
        if create:
            with self.database.write() as connection:
                self._store_records(connection, {}, metadata=data)
        return data

    def _store_records(self, connection, records, *, metadata):
        source = {key: value for key, value in metadata.items() if key != "completed"}
        connection.execute(
            "INSERT INTO checkpoint_sources VALUES (?, ?) "
            "ON CONFLICT(source_key) DO UPDATE SET metadata=excluded.metadata",
            (self._source_key, encode_record(source)),
        )
        connection.executemany(
            "INSERT INTO checkpoint_items VALUES (?, ?, ?) "
            "ON CONFLICT(source_key, signature) DO UPDATE SET metadata=excluded.metadata",
            ((self._source_key, signature, encode_record(value)) for signature, value in records.items()),
        )

    def _save(self, data):
        data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        atomic_write_json(self.path, data)

    def signature(self, item_or_path, snapshot=None) -> str:
        item = item_or_path if isinstance(item_or_path, dict) else None
        path = Path(item["path"] if item else item_or_path)
        values = snapshot or _snapshot_for_item(item, path, self.snapshot_provider)
        if values is None:
            raise OSError(f"文件暂时不可读取：{path}")
        return media_file_signature(path, root=self.source_root, snapshot=values)

    def is_completed(self, item_or_path) -> bool:
        try:
            with self.lock:
                return self.signature(item_or_path) in self.data.get("completed", {})
        except (OSError, ValueError, TypeError):
            return False

    def _album_records(self, items, message_ids):
        values = list(items or [])
        ids = list(message_ids or [])
        sent_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        records = {}
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
                "sent_at": sent_at,
            }
            capture_time = item.get("capture_time")
            record["capture_time"] = (
                capture_time.isoformat() if hasattr(capture_time, "isoformat") else capture_time
            )
            for key in ("media_kind", "group_name", "month_key", "date_tag"):
                if item.get(key) is not None:
                    record[key] = item[key]
            records[signature] = record
        return records, sent_at

    def persist_album(self, connection, items, message_ids=None):
        """Write Album rows with the caller's transaction and return its publish callback."""
        records, sent_at = self._album_records(items, message_ids)
        self._store_records(connection, records, metadata={**self._new(), "updated_at": sent_at})

        def publish():
            with self.lock:
                self.data["completed"].update(records)
                self.data["updated_at"] = sent_at

        return publish

    def mark_album_completed(self, items, message_ids=None):
        if self.database is not None:
            with self.database.write() as connection:
                publish = self.persist_album(connection, items, message_ids)
            publish()
            return
        with self.lock:
            records, _sent_at = self._album_records(items, message_ids)
            # Publish the complete Album only after the atomic JSON write succeeds.
            data = {**self.data, "completed": {**self.data["completed"], **records}}
            self._save(data)
            self.data = data


def migrate_checkpoints(database, state_root):
    """Import the legacy flat and per-kind checkpoints on first application startup."""
    state_root = Path(state_root)

    def import_legacy(_connection):
        kinds = ("video", "image", "mixed")
        for kind in kinds:
            pattern = f"{kind}_upload_state_*.json"
            state_dir = state_root / kind
            for path in (*state_root.glob(pattern), *state_dir.glob(pattern)):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if (raw.get("kind") == kind and isinstance(raw["source_root"], str)
                            and isinstance(raw["target"], dict)):
                        UploadState(kind=kind, source_root=raw["source_root"], target=raw["target"],
                                    state_dir=state_dir, create=False, database=database)
                except (AttributeError, KeyError, LegacyCheckpointError, OSError, TypeError, ValueError):
                    # Keep unreadable records on disk. Accessing that source's
                    # checkpoint will block until its record can be inspected.
                    continue

    database.migrate("checkpoint_inventory", database.key(state_root), import_legacy)


def reset_checkpoints(database, state_dir, kind):
    """Clear one media kind and mark its legacy backups as already imported."""
    state_dir = Path(state_dir)
    prefix = database.key(state_dir) + os.sep
    pattern = f"{kind}_upload_state_*.json"
    backups = {database.key(state_dir / path.name) for directory in (state_dir, state_dir.parent)
               for path in directory.glob(pattern)}
    with database.write() as connection:
        connection.executemany("INSERT OR IGNORE INTO legacy_imports VALUES ('checkpoint', ?)",
                               ((key,) for key in backups))
        connection.execute("DELETE FROM checkpoint_sources WHERE substr(source_key, 1, ?)=?",
                           (len(prefix), prefix))


__all__ = ["UploadState", "migrate_checkpoints", "reset_checkpoints"]
