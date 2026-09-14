# -*- coding: utf-8 -*-
"""Durable journal for Albums submitted to Telegram but not yet confirmed.

TDLib can accept a send request and lose the connection before the matching
``updateMessageSendSucceeded`` arrives.  The journal keeps that ambiguity
explicit so a later run never silently sends the same stable Album again.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from path_utils import stable_path
from runtime_paths import APP_DATA_DIR


PREPARED = "PREPARED"
SUBMITTED = "SUBMITTED"
CONFIRMED = "CONFIRMED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"
# A CONFIRMED record remains on disk until the uploader has durably written its
# normal checkpoint.  Treat it as unresolved during that small hand-off so a
# crash after Telegram accepted the Album cannot cause an automatic duplicate
# on the next run.  ``finalize`` removes it only after the state save succeeds.
UNRESOLVED = frozenset({PREPARED, SUBMITTED, CONFIRMED, UNKNOWN})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class InflightJournal:
    """One JSON record per Album, written atomically under ``.upload_inflight``."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else APP_DATA_DIR / ".upload_inflight"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _key_hash(kind: str, album_key: str) -> str:
        return hashlib.sha256(
            f"{str(kind).strip().lower()}\n{album_key}".encode("utf-8")
        ).hexdigest()

    def path_for(self, kind: str, album_key: str) -> Path:
        return self.root / f"{self._key_hash(kind, album_key)}.json"

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

    def get(self, kind: str, album_key: str) -> dict | None:
        with self._lock:
            return self._read_path(self.path_for(kind, album_key))

    def unresolved(self, kind: str, album_key: str) -> dict | None:
        record = self.get(kind, album_key)
        if record and str(record.get("status", "")) in UNRESOLVED:
            return record
        return None

    def find_album(self, album_key: str) -> dict | None:
        """Find a record by Album key when the media kind is not known."""

        target = str(album_key)
        with self._lock:
            for path in self.root.glob("*.json"):
                record = self._read_path(path)
                if record and str(record.get("album_key", "")) == target:
                    return record
        return None

    def prepare(self, kind: str, album_key: str, items=None) -> dict:
        """Persist PREPARED immediately before issuing a TDLib request."""

        paths = []
        for item in items or []:
            path = item.get("path") if isinstance(item, dict) else item
            if path is None:
                continue
            paths.append(stable_path(path))
        record = {
            "version": 1,
            "kind": str(kind).strip().lower(),
            "album_key": str(album_key),
            "status": PREPARED,
            "items": paths,
            "message_ids": [],
            "created_at": _now(),
            "updated_at": _now(),
        }
        with self._lock:
            self._write(self.path_for(kind, album_key), record)
        return record

    def update(self, kind: str, album_key: str, status: str, *, message_ids=None, error: str = "") -> dict:
        normalized = str(status).strip().upper()
        if normalized not in {PREPARED, SUBMITTED, CONFIRMED, FAILED, UNKNOWN}:
            raise ValueError(f"未知上传日志状态：{status}")
        with self._lock:
            path = self.path_for(kind, album_key)
            record = self._read_path(path) or {
                "version": 1,
                "kind": str(kind).strip().lower(),
                "album_key": str(album_key),
                "items": [],
                "created_at": _now(),
            }
            record["status"] = normalized
            record["updated_at"] = _now()
            if message_ids is not None:
                record["message_ids"] = list(message_ids)
            if error:
                record["error"] = str(error)
            self._write(path, record)
            return record

    def submitted(self, kind: str, album_key: str, message_ids) -> dict:
        return self.update(kind, album_key, SUBMITTED, message_ids=message_ids)

    def confirmed(self, kind: str, album_key: str, message_ids=None) -> None:
        """Mark confirmed and remove the record so it cannot block future runs."""

        with self._lock:
            path = self.path_for(kind, album_key)
            record = self._read_path(path)
            if record is not None:
                record["status"] = CONFIRMED
                record["updated_at"] = _now()
                if message_ids is not None:
                    record["message_ids"] = list(message_ids)
                self._write(path, record)
            path.unlink(missing_ok=True)

    def failed(self, kind: str, album_key: str, error: str = "") -> dict:
        return self.update(kind, album_key, FAILED, error=error)

    def unknown(self, kind: str, album_key: str, error: str = "") -> dict:
        return self.update(kind, album_key, UNKNOWN, error=error)

    def mark_sent(self, kind: str, album_key: str, message_ids=None) -> None:
        """Manual reconciliation: user confirms Telegram already has the Album."""

        self.confirmed(kind, album_key, message_ids)

    def mark_not_sent(self, kind: str, album_key: str) -> None:
        """Manual reconciliation: user confirms it is safe to retry."""

        with self._lock:
            self.path_for(kind, album_key).unlink(missing_ok=True)

    def list_unresolved(self) -> list[dict]:
        records = []
        with self._lock:
            for path in self.root.glob("*.json"):
                record = self._read_path(path)
                if record and record.get("status") in UNRESOLVED:
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
]
