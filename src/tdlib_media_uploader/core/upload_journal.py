# -*- coding: utf-8 -*-
"""Durable journal for Albums submitted to Telegram but not yet confirmed.

The journal deliberately keeps an ambiguous send on disk.  A crash can happen
after TDLib accepts a request and before the matching update reaches this
process; automatically submitting the Album again would then create a
duplicate.  Records are scoped to the Telegram target so the same source
Album can safely be sent to another topic/channel.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .identity import canonical_target
from .filesystem_legacy import file_snapshot, stable_path
from ..config.paths import APP_DATA_DIR


PREPARED = "PREPARED"
SUBMITTED = "SUBMITTED"
CONFIRMED = "CONFIRMED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"
CORRUPT = "CORRUPT"
UNRESOLVED = frozenset({PREPARED, SUBMITTED, CONFIRMED, UNKNOWN, CORRUPT})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_target(target=None) -> dict:
    """Return the shared canonical Telegram target identity.

    The journal keeps this public name for existing callers, while the actual
    mode-specific normalization lives in :mod:`.identity` so UploadState,
    journals and GUI reconciliation cannot drift apart.
    """

    return canonical_target(target)


def _record_target(record: dict) -> dict:
    """Read a target from current or older journal record shapes.

    Version 2 records normally contain all four target fields, while a few
    early integrations only persisted the fields relevant to their mode.  A
    record is considered target-scoped only when its mode and effective
    destination fields are present; otherwise it remains a conservative
    target-less record and blocks matching Albums for every target.
    """

    def _candidate_has_identity(value) -> bool:
        if not isinstance(value, dict):
            return False
        mode = str(value.get("target_mode", "")).strip().lower()
        if mode == "forum_topic":
            return (
                ("chat_id" in value or "group_chat_id" in value)
                and "forum_topic_id" in value
            )
        if mode == "channel":
            return any(key in value for key in ("channel_chat_id", "chat_id", "group_chat_id"))
        return False

    candidates = []
    nested = record.get("target") if isinstance(record, dict) else None
    if isinstance(nested, dict):
        candidates.append(nested)
    if isinstance(record, dict):
        candidates.append(record)
    for candidate in candidates:
        if _candidate_has_identity(candidate):
            value = normalize_target(candidate)
            if value:
                return value
    return {}


def _target_matches(record: dict, target=None) -> bool:
    recorded = _record_target(record)
    requested = normalize_target(target)
    # Old records have no target information.  They must remain conservative
    # and block a matching Album regardless of the currently selected target.
    if not recorded or not requested:
        return True
    return recorded == requested


class InflightJournal:
    """One JSON record per Album, written atomically under ``data/upload_inflight``."""

    def __init__(self, root: Path | None = None):
        # Resolve the default from APP_DATA_DIR at construction time so
        # embedders/tests can redirect the data root without re-importing the
        # module.  V1.9's APP_DATA_DIR is already DATA_DIR.
        self.root = Path(root) if root is not None else APP_DATA_DIR / "upload_inflight"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._index = None
        self._records = []
        self._corrupt = []

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
        self._index = None

    @staticmethod
    def _corrupt_record(
        path: Path,
        error: BaseException,
        *,
        kind: str | None = None,
        album_key: str | None = None,
        target=None,
    ) -> dict:
        record = {
            "status": CORRUPT,
            "journal_path": str(path),
            "error": f"{type(error).__name__}: {error}",
        }
        if kind:
            record["kind"] = str(kind).strip().lower()
        if album_key:
            record["album_key"] = str(album_key)
        normalized_target = normalize_target(target)
        if normalized_target:
            record["target"] = dict(normalized_target)
            record.update(normalized_target)
        return record

    def _read_path(
        self,
        path: Path,
        *,
        expected_kind: str | None = None,
        expected_album_key: str | None = None,
        expected_target=None,
    ) -> dict | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("journal must be an object")
            if not value.get("kind") or not value.get("album_key"):
                raise ValueError("journal identity is missing")
            status = str(value.get("status", "")).strip().upper()
            if status not in UNRESOLVED | {FAILED}:
                raise ValueError("invalid journal status")
            value["status"] = status
            if "items" in value and not isinstance(value["items"], list):
                raise ValueError("invalid item snapshot list")
            if "target" in value and not isinstance(value["target"], dict):
                raise ValueError("invalid target identity")
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError) as exc:
            return self._corrupt_record(
                path,
                exc,
                kind=expected_kind,
                album_key=expected_album_key,
                target=expected_target,
            )

    def invalidate(self):
        """Explicit refresh boundary for external edits, including in-place writes."""
        with self._lock:
            self._index = None

    def _ensure_index(self):
        if self._index is not None:
            return
        index = {}
        records = []
        corrupt = []
        for path in sorted(self.root.glob("*.json")):
            record = self._read_path(path)
            if record is None:
                continue
            records.append((path, record))
            if record.get("status") == CORRUPT:
                corrupt.append((path, record))
                continue
            key = (str(record["kind"]).strip().lower(), str(record["album_key"]))
            index.setdefault(key, []).append((path, record))
        self._index, self._records, self._corrupt = index, records, corrupt

    def _entries(self, kind: str | None, album_key: str, target=None):
        self._ensure_index()
        if kind:
            candidates = self._index.get((str(kind).strip().lower(), str(album_key)), ())
        else:
            candidates = [(p, r) for p, r in self._records
                          if str(r.get("album_key", "")) == str(album_key)]
        for path, record in candidates:
            if _target_matches(record, target):
                yield path, copy.deepcopy(record)
        # Hashed filenames cannot reliably recover an unknown identity.  Such
        # a record remains a conservative global blocker instead of being
        # mistaken for an absent journal and allowing a duplicate send.
        for path, record in self._corrupt:
            recorded_kind = str(record.get("kind", "")).strip().lower()
            recorded_album = str(record.get("album_key", ""))
            if kind and recorded_kind and recorded_kind != str(kind).strip().lower():
                continue
            if recorded_album and recorded_album != str(album_key):
                continue
            if not _target_matches(record, target):
                continue
            yield path, copy.deepcopy(record)

    def get_entry(self, kind: str, album_key: str, target=None):
        with self._lock:
            preferred = self.path_for(kind, album_key, target)
            record = self._read_path(
                preferred,
                expected_kind=kind,
                expected_album_key=album_key,
                expected_target=target,
            )
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

    def unresolved_for_items(self, kind: str, items, *, target=None):
        """Find protected source snapshots even after their Album is regrouped."""
        def identity(item):
            return (stable_path(item["path"]),
                    item.get("size", item.get("scan_size")),
                    item.get("mtime_ns", item.get("scan_mtime_ns")))
        requested = {identity(item) for item in items}
        with self._lock:
            self._ensure_index()
            for _path, record in self._records:
                if (record.get("kind") == kind and record.get("status") in UNRESOLVED
                        and _target_matches(record, target)):
                    if any(isinstance(item, dict) and item.get("path")
                           and any(identity(item)[0] == candidate[0]
                                   and all(left is None or right is None or left == right
                                           for left, right in zip(identity(item)[1:], candidate[1:]))
                                   for candidate in requested)
                           for item in record.get("items", ())):
                        return copy.deepcopy(record)
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
            if source.get("source_root") is not None:
                value["source_root"] = stable_path(source["source_root"])
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
            existing_path, existing = self.get_entry(kind, album_key, target)
            if existing is not None:
                status = str(existing.get("status", "")).upper()
                if status == CORRUPT:
                    raise RuntimeError(f"损坏的上传记录：{existing.get('journal_path')}")
                if status in UNRESOLVED:
                    raise RuntimeError(
                        f"已有未确认上传记录：{kind} / {album_key}（{status}）"
                    )
                if existing_path is not None:
                    path = existing_path
            overlap = self.unresolved_for_items(kind, record["items"], target=target)
            if overlap is not None:
                raise RuntimeError(f"源文件已有未确认上传记录：{overlap['album_key']}")
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
        succeeded_ids=None,
        failed_ids=None,
        pending_ids=None,
        target=None,
    ) -> dict:
        normalized = str(status).strip().upper()
        if normalized not in {PREPARED, SUBMITTED, CONFIRMED, FAILED, UNKNOWN}:
            raise ValueError(f"未知上传日志状态：{status}")
        with self._lock:
            existing_path, record = self.get_entry(kind, album_key, target)
            path = existing_path or self.path_for(kind, album_key, target)
            if record and record.get("status") == CORRUPT:
                raise RuntimeError(f"损坏的上传记录：{record.get('journal_path')}")
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
            if succeeded_ids is not None:
                record["succeeded_ids"] = list(succeeded_ids)
            if failed_ids is not None:
                record["failed_ids"] = list(failed_ids)
            if pending_ids is not None:
                record["pending_ids"] = list(pending_ids)
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
            if _record.get("status") == CORRUPT:
                raise RuntimeError("不能删除损坏的上传记录")
            path.unlink(missing_ok=True)
            self.invalidate()
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

    def mark_not_sent(self, kind: str, album_key: str, *, target=None) -> None:
        """Manual reconciliation: it is safe to submit the Album again."""

        with self._lock:
            path, _record = self.get_entry(kind, album_key, target)
            if _record and _record.get("status") in {CONFIRMED, CORRUPT}:
                raise ValueError("Telegram 已确认或记录损坏，不能标记为未发送")
            if path is not None:
                path.unlink(missing_ok=True)
                self.invalidate()

    def list_unresolved(self, *, refresh=False) -> list[dict]:
        with self._lock:
            if refresh:
                self.invalidate()
            self._ensure_index()
            records = [copy.deepcopy(record) for _, record in self._records
                       if record.get("status") in UNRESOLVED]
            return sorted(records, key=lambda r: str(r.get("updated_at") or
                          r.get("created_at") or ""), reverse=True)


__all__ = [
    "InflightJournal",
    "PREPARED",
    "SUBMITTED",
    "CONFIRMED",
    "FAILED",
    "UNKNOWN",
    "CORRUPT",
    "UNRESOLVED",
    "normalize_target",
]
