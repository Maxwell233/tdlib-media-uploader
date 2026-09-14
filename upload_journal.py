# -*- coding: utf-8 -*-
"""Durable journal for Albums submitted to Telegram but not yet confirmed.

The journal deliberately keeps an ambiguous send on disk.  A crash can happen
after TDLib accepts a request and before the matching update reaches this
process; automatically submitting the Album again would then create a
duplicate.  Records are scoped to the Telegram target so the same source
Album can safely be sent to another topic/channel.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from path_utils import file_snapshot, stable_path
from runtime_paths import APP_DATA_DIR


PREPARED = "PREPARED"
SUBMITTED = "SUBMITTED"
CONFIRMED = "CONFIRMED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"
UNRESOLVED = frozenset({PREPARED, SUBMITTED, CONFIRMED, UNKNOWN})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_target(target=None) -> dict:
    """Return a JSON-safe target identity or an empty legacy identity."""

    if not isinstance(target, dict):
        return {}
    result = {
        "target_mode": str(target.get("target_mode", "")).strip().lower(),
        "chat_id": int(target.get("chat_id", 0) or 0),
        "forum_topic_id": int(target.get("forum_topic_id", 0) or 0),
        "channel_chat_id": int(target.get("channel_chat_id", 0) or 0),
    }
    # A completely empty object is the legacy shape.  Keep that distinction:
    # records without a target conservatively block every target.
    if not any(result.values()):
        return {}
    return result


def _record_target(record: dict) -> dict:
    nested = record.get("target")
    if isinstance(nested, dict):
        required = {"target_mode", "chat_id", "forum_topic_id", "channel_chat_id"}
        if not required.issubset(nested):
            return {}
        value = normalize_target(nested)
        if value:
            return value
    required = ("target_mode", "chat_id", "forum_topic_id", "channel_chat_id")
    if not all(key in record for key in required):
        return {}
    fields = {key: record.get(key) for key in required}
    return normalize_target(fields)


def _target_matches(record: dict, target=None) -> bool:
    recorded = _record_target(record)
    requested = normalize_target(target)
    # Old records have no target information.  They must remain conservative
    # and block a matching Album regardless of the currently selected target.
    if not recorded or not requested:
        return True
    return recorded == requested


class InflightJournal:
    """One JSON record per Album, written atomically under ``.upload_inflight``."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else APP_DATA_DIR / ".upload_inflight"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _key_hash(kind: str, album_key: str, target=None) -> str:
        normalized_target = normalize_target(target)
        if normalized_target:
            identity = json.dumps(normalized_target, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            raw = f"{str(kind).strip().lower()}\n{album_key}\n{identity}"
        else:
            # Preserve the original filename scheme for old integrations and
            # legacy records created before target scoping was introduced.
            raw = f"{str(kind).strip().lower()}\n{album_key}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def path_for(self, kind: str, album_key: str, target=None) -> Path:
        return self.root / f"{self._key_hash(kind, album_key, target)}.json"

    def _write(self, path: Path, record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _read_path(self, path: Path) -> dict | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _entries(self, kind: str | None, album_key: str, target=None):
        expected_kind = str(kind).strip().lower() if kind else None
        expected_album = str(album_key)
        # Sorting gives deterministic selection if a previous process left a
        # duplicate legacy record beside a newly scoped one.
        for path in sorted(self.root.glob("*.json"), key=lambda value: value.name):
            record = self._read_path(path)
            if not record or str(record.get("album_key", "")) != expected_album:
                continue
            if expected_kind and str(record.get("kind", "")).strip().lower() != expected_kind:
                continue
            if not _target_matches(record, target):
                continue
            yield path, record

    def get_entry(self, kind: str, album_key: str, target=None):
        with self._lock:
            preferred = self.path_for(kind, album_key, target)
            record = self._read_path(preferred)
            if record and _target_matches(record, target):
                return preferred, record
            # Read legacy and records produced by an earlier target identity
            # implementation without requiring a migration step.
            return next(self._entries(kind, album_key, target), (None, None))

    def get(self, kind: str, album_key: str, target=None) -> dict | None:
        return self.get_entry(kind, album_key, target)[1]

    def unresolved(self, kind: str, album_key: str, target=None) -> dict | None:
        record = self.get(kind, album_key, target)
        if record and str(record.get("status", "")).upper() in UNRESOLVED:
            return record
        return None

    def find_album(self, album_key: str, kind: str | None = None, target=None) -> dict | None:
        """Find a record by Album key when the media kind is not known."""

        with self._lock:
            return next((record for _path, record in self._entries(kind, album_key, target)), None)

    @staticmethod
    def _normalize_items(items) -> list[dict]:
        normalized = []
        for item in items or []:
            source = item if isinstance(item, dict) else {"path": item}
            path = source.get("path")
            if path is None:
                continue
            value = {"path": stable_path(path)}
            for source_key, target_key in (
                ("scan_size", "size"),
                ("scan_mtime_ns", "mtime_ns"),
                ("size", "size"),
                ("mtime_ns", "mtime_ns"),
                ("media_kind", "media_kind"),
                ("group_name", "group_name"),
                ("month_key", "month_key"),
                ("date_tag", "date_tag"),
            ):
                if source_key in source and source[source_key] is not None:
                    value[target_key] = source[source_key]
            if value.get("size") is None or value.get("mtime_ns") is None:
                snapshot = file_snapshot(path)
                if snapshot is not None:
                    value["size"], value["mtime_ns"] = snapshot
            capture_time = source.get("capture_time")
            if capture_time is not None:
                value["capture_time"] = (
                    capture_time.isoformat()
                    if hasattr(capture_time, "isoformat")
                    else str(capture_time)
                )
            normalized.append(value)
        return normalized

    @staticmethod
    def _with_target(record: dict, target) -> None:
        normalized = normalize_target(target)
        if not normalized:
            return
        record["target"] = dict(normalized)
        # Keep fields at the top level too so older tools and users can inspect
        # a journal with a text editor.
        record.update(normalized)

    def prepare(self, kind: str, album_key: str, items=None, *, target=None) -> dict:
        """Persist PREPARED immediately before issuing a TDLib request."""

        now = _now()
        record = {
            "version": 2,
            "kind": str(kind).strip().lower(),
            "album_key": str(album_key),
            "status": PREPARED,
            "items": self._normalize_items(items),
            "message_ids": [],
            "created_at": now,
            "updated_at": now,
        }
        self._with_target(record, target)
        with self._lock:
            path = self.path_for(kind, album_key, target)
            self._write(path, record)
        return record

    def update(
        self,
        kind: str,
        album_key: str,
        status: str,
        *,
        message_ids=None,
        error: str = "",
        target=None,
    ) -> dict:
        normalized = str(status).strip().upper()
        if normalized not in {PREPARED, SUBMITTED, CONFIRMED, FAILED, UNKNOWN}:
            raise ValueError(f"未知上传日志状态：{status}")
        with self._lock:
            existing_path, record = self.get_entry(kind, album_key, target)
            path = existing_path or self.path_for(kind, album_key, target)
            record = record or {
                "version": 2,
                "kind": str(kind).strip().lower(),
                "album_key": str(album_key),
                "items": [],
                "created_at": _now(),
            }
            self._with_target(record, target)
            record["status"] = normalized
            record["updated_at"] = _now()
            if message_ids is not None:
                record["message_ids"] = list(message_ids)
            if error:
                record["error"] = str(error)
            self._write(path, record)
            return record

    def submitted(self, kind: str, album_key: str, message_ids, *, target=None) -> dict:
        return self.update(kind, album_key, SUBMITTED, message_ids=message_ids, target=target)

    def mark_confirmed(self, kind: str, album_key: str, message_ids=None, *, target=None) -> dict | None:
        """Mark confirmed while retaining the record until state is durable."""

        return self.update(kind, album_key, CONFIRMED, message_ids=message_ids, target=target)

    def finalize(self, kind: str, album_key: str, *, target=None) -> bool:
        """Remove a journal only after the normal checkpoint was persisted."""

        with self._lock:
            path, _record = self.get_entry(kind, album_key, target)
            if path is None:
                return False
            path.unlink(missing_ok=True)
            return True

    def confirmed(self, kind: str, album_key: str, message_ids=None, *, target=None) -> None:
        """Backward-compatible finalization helper.

        New code should call :meth:`mark_confirmed` followed by
        :meth:`finalize`; older integrations historically used ``confirmed``
        as the final delete operation, so retain that behavior here.
        """

        self.mark_confirmed(kind, album_key, message_ids, target=target)
        self.finalize(kind, album_key, target=target)

    def failed(self, kind: str, album_key: str, error: str = "", *, target=None) -> dict:
        return self.update(kind, album_key, FAILED, error=error, target=target)

    def unknown(self, kind: str, album_key: str, error: str = "", *, target=None) -> dict:
        return self.update(kind, album_key, UNKNOWN, error=error, target=target)

    def mark_sent(self, kind: str, album_key: str, message_ids=None, *, target=None) -> dict | None:
        """Mark a manually confirmed send; caller finalizes after state save."""

        return self.mark_confirmed(kind, album_key, message_ids, target=target)

    def mark_not_sent(self, kind: str, album_key: str, *, target=None) -> None:
        """Manual reconciliation: it is safe to submit the Album again."""

        with self._lock:
            path, _record = self.get_entry(kind, album_key, target)
            if path is not None:
                path.unlink(missing_ok=True)

    def list_unresolved(self) -> list[dict]:
        records = []
        with self._lock:
            for path in sorted(self.root.glob("*.json"), key=lambda value: value.name):
                record = self._read_path(path)
                if record and str(record.get("status", "")).upper() in UNRESOLVED:
                    records.append(record)
        return records


__all__ = [
    "InflightJournal",
    "PREPARED",
    "SUBMITTED",
    "CONFIRMED",
    "FAILED",
    "UNKNOWN",
    "UNRESOLVED",
    "normalize_target",
]
