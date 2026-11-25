"""Shared durable storage for delivery checkpoints, journal and queue."""

from contextlib import closing, contextmanager
from contextvars import ContextVar
from functools import lru_cache
import json
import os
from pathlib import Path
import sqlite3

from .filesystem import stable_path
from .persistence import ensure_private_directory


_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoint_sources (
    source_key TEXT PRIMARY KEY,
    metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoint_items (
    source_key TEXT NOT NULL REFERENCES checkpoint_sources(source_key) ON DELETE CASCADE,
    signature TEXT NOT NULL,
    metadata TEXT NOT NULL,
    PRIMARY KEY (source_key, signature)
);
CREATE TABLE IF NOT EXISTS inflight (
    journal_root TEXT NOT NULL,
    filename TEXT NOT NULL,
    kind TEXT,
    album_key TEXT,
    status TEXT NOT NULL,
    metadata TEXT NOT NULL,
    PRIMARY KEY (journal_root, filename)
);
CREATE INDEX IF NOT EXISTS inflight_album ON inflight(journal_root, album_key, kind);
CREATE INDEX IF NOT EXISTS inflight_status ON inflight(journal_root, status);
CREATE TABLE IF NOT EXISTS inflight_items (
    journal_root TEXT NOT NULL,
    filename TEXT NOT NULL,
    stable_path TEXT NOT NULL,
    PRIMARY KEY (journal_root, filename, stable_path),
    FOREIGN KEY (journal_root, filename) REFERENCES inflight(journal_root, filename) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS inflight_item_path ON inflight_items(journal_root, stable_path);
CREATE TABLE IF NOT EXISTS upload_queues (
    queue_key TEXT PRIMARY KEY,
    metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_album_receipts (
    task_id TEXT NOT NULL,
    album_key TEXT NOT NULL,
    paths TEXT NOT NULL,
    PRIMARY KEY (task_id, album_key)
);
CREATE TABLE IF NOT EXISTS legacy_imports (
    scope TEXT NOT NULL,
    source_key TEXT NOT NULL,
    PRIMARY KEY (scope, source_key)
);
CREATE TABLE IF NOT EXISTS media_metadata_cache (
    stable_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    probe TEXT NOT NULL,
    metadata TEXT NOT NULL,
    PRIMARY KEY (stable_path, size, mtime_ns, probe)
);
PRAGMA user_version=1;
"""

_transaction = ContextVar("uploader_database_transaction", default=None)


def encode_record(record):
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


class UploaderDatabase:
    def __init__(self, path):
        self.path = Path(path).resolve()
        ensure_private_directory(self.path.parent)
        # Set the database permissions before SQLite creates WAL sidecars.
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(descriptor)
        try:
            with closing(self._connect()) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise RuntimeError(f"上传数据库版本不受支持：{version}")
                mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if mode != "wal":
                    raise RuntimeError(f"无法启用 SQLite WAL：{self.path}")
                connection.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise RuntimeError(f"无法初始化上传数据库 {self.path}：{exc}") from exc

    def _connect(self):
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    def key(self, path):
        path = Path(path).resolve()
        return str(path.relative_to(self.path.parent)) if path.is_relative_to(self.path.parent) else str(path)

    @contextmanager
    def read(self):
        current = _transaction.get()
        if current is not None and current[0] == self.path:
            yield current[1]
            return
        try:
            with closing(self._connect()) as connection:
                yield connection
        except sqlite3.Error as exc:
            raise RuntimeError(f"无法读取上传数据库 {self.path}：{exc}") from exc

    @contextmanager
    def write(self):
        current = _transaction.get()
        if current is not None and current[0] == self.path:
            yield current[1]
            return
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                token = _transaction.set((self.path, connection))
                try:
                    with connection:
                        yield connection
                finally:
                    _transaction.reset(token)
        except sqlite3.Error as exc:
            raise RuntimeError(f"无法保存上传数据库 {self.path}：{exc}") from exc

    def migrate(self, scope, source_key, import_records):
        query = "SELECT 1 FROM legacy_imports WHERE scope=? AND source_key=?"
        identity = (scope, source_key)
        with self.read() as connection:
            if connection.execute(query, identity).fetchone():
                return
        with self.write() as connection:
            if not connection.execute(query, identity).fetchone():
                import_records(connection)
                connection.execute("INSERT INTO legacy_imports VALUES (?, ?)", identity)


@lru_cache(maxsize=8)
def database_for(path):
    return UploaderDatabase(path)


def load_media_metadata(probe, snapshots):
    """Load available probe results for source snapshots, keyed by stable path."""
    from ..config.paths import DATA_DIR

    try:
        keys = [(stable_path(path), size, mtime_ns, probe) for path, size, mtime_ns in snapshots]
        if not keys:
            return {}
        with database_for(DATA_DIR / "uploader.db").read() as connection:
            found = {}
            for key in keys:
                row = connection.execute(
                    "SELECT metadata FROM media_metadata_cache "
                    "WHERE stable_path=? AND size=? AND mtime_ns=? AND probe=?", key,
                ).fetchone()
                if row is not None:
                    found[key[0]] = json.loads(row[0])
            return found
    except (OSError, RuntimeError, ValueError):
        return {}


def store_media_metadata(probe, records):
    """Cache successful probes, replacing older snapshots of the same file."""
    from ..config.paths import DATA_DIR

    try:
        rows = [(stable_path(path), size, mtime_ns, probe, encode_record(value))
                for path, size, mtime_ns, value in records]
        if not rows:
            return
        with database_for(DATA_DIR / "uploader.db").write() as connection:
            connection.executemany(
                "DELETE FROM media_metadata_cache WHERE stable_path=? AND probe=? "
                "AND (size<>? OR mtime_ns<>?)",
                ((path, kind, size, mtime_ns) for path, size, mtime_ns, kind, _ in rows),
            )
            connection.executemany("INSERT OR REPLACE INTO media_metadata_cache VALUES (?, ?, ?, ?, ?)", rows)
    except (OSError, RuntimeError, ValueError):
        # The probe has already succeeded and callers can use its fresh result.
        pass
