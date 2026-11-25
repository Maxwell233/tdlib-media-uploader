# -*- coding: utf-8 -*-
"""Durable journal for Albums submitted to Telegram but not yet confirmed.

The journal deliberately keeps an ambiguous send on disk.  A crash can happen
after TDLib accepts a request and before the matching update reaches this
process; automatically submitting the Album again would then create a
duplicate.  Records are scoped to the Telegram target so the same source
Album can safely be sent to another topic/channel.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import copy
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .identity import canonical_target
from .filesystem import file_snapshot, stable_path
from .persistence import atomic_write_json, durable_unlink, ensure_private_directory
from .uploader_database import database_for, encode_record
from ..config.paths import APP_DATA_DIR, UPLOAD_INFLIGHT_DIR


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
    """Durable Album records with indexed lookups in the application database."""

    def __init__(self, root: Path | None = None, *, database=None):
        # Resolve the default from APP_DATA_DIR at construction time so
        # embedders/tests can redirect the data root without re-importing the
        # module.  V1.9's APP_DATA_DIR is already DATA_DIR.
        self.root = Path(root) if root is not None else APP_DATA_DIR / "upload_inflight"
        ensure_private_directory(self.root)
        self._lock = threading.RLock()
        self._index = None
        self._records = []
        self._corrupt = []
        if database is None and (root is None or self.root == UPLOAD_INFLIGHT_DIR):
            database = database_for(self.root.parent / "uploader.db")
        self.database = database
        if database is not None:
            self._root_key = database.key(self.root)
            database.migrate("inflight", self._root_key, self._import_legacy)

    def _import_legacy(self, connection):
        for path in sorted(self.root.glob("*.json")):
            record = self._read_legacy_path(path)
            if record is not None:
                self._write_database(connection, path, record)

    @contextmanager
    def _mutation(self):
        with self._lock, self.database.write() if self.database is not None else nullcontext():
            yield

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
        if self.database is not None:
            with self.database.write() as connection:
                self._write_database(connection, path, record)
            return
        try:
            atomic_write_json(path, record)
        finally:
            # A directory fsync can fail after replace has changed the file.
            self._index = None

    def _write_database(self, connection, path, record):
        identity = (self._root_key, path.name)
        connection.execute(
            "INSERT INTO inflight VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(journal_root, filename) DO UPDATE SET "
            "kind=excluded.kind, album_key=excluded.album_key, status=excluded.status, metadata=excluded.metadata",
            (*identity, str(record.get("kind") or "").strip().lower() or None,
             record.get("album_key"), record["status"], encode_record(record)),
        )
        connection.execute("DELETE FROM inflight_items WHERE journal_root=? AND filename=?", identity)
        connection.executemany(
            "INSERT OR IGNORE INTO inflight_items VALUES (?, ?, ?)",
            ((*identity, stable_path(item["path"])) for item in record.get("items", ())
             if isinstance(item, dict) and item.get("path")),
        )

    def _read_path(self, path, **expected):
        if self.database is None:
            return self._read_legacy_path(path, **expected)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT metadata FROM inflight WHERE journal_root=? AND filename=?", (self._root_key, path.name)
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

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

    def _read_legacy_path(
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
            if not all(isinstance(value.get(key), str) and value[key] for key in ("kind", "album_key")):
                raise ValueError("journal identity is missing")
            status = str(value.get("status", "")).strip().upper()
            if status not in UNRESOLVED | {FAILED}:
                raise ValueError("invalid journal status")
            if status == FAILED and (value.get("succeeded_ids") or value.get("pending_ids")):
                raise ValueError("failed journal contains successful or pending messages")
            value["status"] = status
            if "items" in value and not isinstance(value["items"], list):
                raise ValueError("invalid item snapshot list")
            if any(isinstance(item, dict) and item.get("path") and not isinstance(item["path"], str)
                   for item in value.get("items", ())):
                raise ValueError("invalid item snapshot path")
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
        if self.database is not None:
            normalized_kind = str(kind).strip().lower() if kind else None
            with self.database.read() as connection:
                rows = connection.execute(
                    "SELECT filename, metadata FROM inflight WHERE journal_root=? AND ("
                    "(album_key=? AND (? IS NULL OR kind=?)) OR status=?) "
                    "ORDER BY status=? ASC, filename",
                    (self._root_key, str(album_key), normalized_kind, normalized_kind, CORRUPT, CORRUPT),
                ).fetchall()
            for name, value in rows:
                record = json.loads(value)
                if record.get("kind") and normalized_kind and record["kind"].strip().lower() != normalized_kind:
                    continue
                if record.get("album_key") and record["album_key"] != str(album_key):
                    continue
                if _target_matches(record, target):
                    yield self.root / name, record
            return
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
        with self._lock:
            record = self.get(kind, album_key, target)
            if record and record.get("status") in UNRESOLVED:
                return record
            return next((entry for _, entry in self._entries(kind, album_key, target)
                         if entry.get("status") in UNRESOLVED), None)

    def unresolved_for_items(self, kind: str, items, *, target=None):
        """Find protected source snapshots even after their Album is regrouped."""
        def identity(item):
            return (stable_path(item["path"]),
                    item.get("size", item.get("scan_size")),
                    item.get("mtime_ns", item.get("scan_mtime_ns")))

        requested_by_path = {}
        for item in items:
            ident = identity(item)
            requested_by_path.setdefault(ident[0], []).append(ident)

        with self._lock:
            if self.database is None:
                self._ensure_index()
                candidates = self._records
            else:
                # Album-sized lookups keep the overlap guard independent of
                # the number of older FAILED or unrelated unresolved records.
                with self.database.read() as connection:
                    rows = {}
                    for path in requested_by_path:
                        rows.update(connection.execute(
                            "SELECT i.filename, i.metadata FROM inflight_items s INNER JOIN inflight i "
                            "USING (journal_root, filename) WHERE s.journal_root=? AND s.stable_path=? "
                            "AND i.kind=? AND i.status<>?",
                            (self._root_key, path, kind, FAILED),
                        ))
                candidates = ((name, json.loads(value)) for name, value in rows.items())
            for _path, record in candidates:
                if (record.get("kind") == kind and record.get("status") in UNRESOLVED
                        and _target_matches(record, target)):
                    for item in record.get("items", ()):
                        if not (isinstance(item, dict) and item.get("path")):
                            continue
                        item_ident = identity(item)
                        candidates = requested_by_path.get(item_ident[0])
                        if candidates and any(
                                all(left is None or right is None or left == right
                                    for left, right in zip(item_ident[1:], candidate[1:]))
                                for candidate in candidates):
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

    def prepare(self, kind: str, album_key: str, items=None, *, target=None,
                attempt_id=None, attempt_number=1) -> dict:
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
        if attempt_id is not None:
            record.update(attempt_id=attempt_id, attempt_number=attempt_number)
        self._with_target(record, target)
        with self._mutation():
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
            if existing is not None and self.unresolved(kind, album_key, target):
                raise RuntimeError(f"已有未确认上传记录：{kind} / {album_key}")
            if attempt_number > 1:
                if (not existing or existing.get("status") != FAILED
                        or existing.get("attempt_number") != attempt_number - 1):
                    raise RuntimeError("找不到已结案的上一次 Album 发送记录")
                summary = {key: existing.get(key) for key in (
                    "attempt_id", "attempt_number", "updated_at", "error", "failed_ids", "request_rejection"
                )}
                record["previous_attempts"] = [*existing.get("previous_attempts", []), summary]
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
        item_results=None,
        items=None,
        reconciled: bool | None = None,
        request_rejection=None,
        target=None,
    ) -> dict:
        normalized = str(status).strip().upper()
        if normalized not in {PREPARED, SUBMITTED, CONFIRMED, FAILED, UNKNOWN}:
            raise ValueError(f"未知上传日志状态：{status}")
        with self._mutation():
            existing_path, record = self.get_entry(kind, album_key, target)
            path = existing_path or self.path_for(kind, album_key, target)
            if record and record.get("status") == CORRUPT:
                raise RuntimeError(f"损坏的上传记录：{record.get('journal_path')}")
            # The lookup target may match an older target-less record. Keep
            # that record's scope so any pending files stay protected.
            if record is None:
                record = {
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
            if item_results is not None:
                record["item_results"] = list(item_results)
            if request_rejection is not None:
                record["request_rejection"] = dict(request_rejection)
            if items is not None:
                record["items"] = self._normalize_items(items)
            if reconciled is not None:
                record["reconciled"] = bool(reconciled)
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

        with self._mutation():
            path, _record = self.get_entry(kind, album_key, target)
            if path is None:
                return False
            if _record.get("status") == CORRUPT:
                raise RuntimeError("不能删除损坏的上传记录")
            self._delete(path)
            return True

    def _delete(self, path):
        if self.database is not None:
            with self.database.write() as connection:
                connection.execute(
                    "DELETE FROM inflight WHERE journal_root=? AND filename=?", (self._root_key, path.name)
                )
        else:
            try:
                durable_unlink(path)
            finally:
                self.invalidate()

    def complete_album(self, state, items, ids, *, kind, album_key, target=None, queue_task_id=None, reconcile=False):
        """Atomically checkpoint and clear a confirmed Album with its queue receipt."""
        persist = getattr(state, "persist_album", None)
        state_database = getattr(state, "database", None)
        if self.database is None or state_database is None or self.database.path != state_database.path:
            return False
        if persist is None:
            raise TypeError("上传状态不支持 SQLite Album 事务")
        with self._lock, self.database.write() as connection:
            path, record = self.get_entry(kind, album_key, target)
            if record is None or record["status"] == CORRUPT or (not reconcile and record["status"] != CONFIRMED):
                raise RuntimeError(f"无法结案未确认的上传记录：{kind} / {album_key}")
            if record["status"] != CONFIRMED:
                self.mark_confirmed(kind, album_key, ids, target=target)
            publish = persist(connection, items, ids)
            if queue_task_id is not None:
                self._save_queue_receipt(connection, queue_task_id, album_key, items)
            self._delete(path)
        publish()
        return True

    @staticmethod
    def _save_queue_receipt(connection, task_id, album_key, items):
        paths = {stable_path(item["path"]) for item in items}
        saved = connection.execute(
            "SELECT paths FROM queue_album_receipts WHERE task_id=? AND album_key=?", (task_id, album_key)
        ).fetchone()
        if saved is not None:
            paths.update(json.loads(saved[0]))
        connection.execute(
            "INSERT INTO queue_album_receipts VALUES (?, ?, ?) "
            "ON CONFLICT(task_id, album_key) DO UPDATE SET paths=excluded.paths",
            (task_id, album_key, encode_record(sorted(paths))),
        )

    def failed(self, kind: str, album_key: str, error: str = "", *, target=None) -> dict:
        return self.update(kind, album_key, FAILED, error=error, target=target)

    def unknown(self, kind: str, album_key: str, error: str = "", *, target=None) -> dict:
        return self.update(kind, album_key, UNKNOWN, error=error, target=target)

    def mark_not_sent(self, kind: str, album_key: str, *, target=None) -> None:
        """Manual reconciliation: it is safe to submit the Album again."""

        with self._mutation():
            path, _record = self.get_entry(kind, album_key, target)
            if _record and _record.get("status") in {CONFIRMED, CORRUPT}:
                raise ValueError("Telegram 已确认或记录损坏，不能标记为未发送")
            if path is not None:
                self._delete(path)

    def list_unresolved(self, *, refresh=False) -> list[dict]:
        with self._lock:
            if self.database is not None:
                with self.database.read() as connection:
                    records = [json.loads(value) for (value,) in connection.execute(
                        "SELECT metadata FROM inflight WHERE journal_root=? AND status<>?", (self._root_key, FAILED)
                    )]
            else:
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
