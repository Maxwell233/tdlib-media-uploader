import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from conftest import task_for
from tdlib_media_uploader.core.upload_journal import CONFIRMED, CORRUPT, InflightJournal
from tdlib_media_uploader.core.upload_state import UploadState
from tdlib_media_uploader.core.uploader_database import UploaderDatabase
from tdlib_media_uploader.gui import cache_service
from tdlib_media_uploader.upload.queue import TaskStatus
from tdlib_media_uploader.upload.queue_store import UploadQueue


class DatabaseStorageTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.state_dir = self.root / "state" / "image"
        self.journal_dir = self.root / "upload_inflight"
        self.item = {"path": self.source / "example.jpg", "size": 11, "mtime_ns": 1234}
        self.target = {"target_mode": "channel", "channel_chat_id": -1001}

    def checkpoint(self, **kwargs):
        return UploadState(kind="image", source_root=self.source, state_dir=self.state_dir, target=self.target, **kwargs)

    def test_import_paths_once_and_preserve_the_old_files(self):
        state = self.checkpoint()
        state.mark_album_completed([self.item], [456])
        legacy_file = state.path.parent.parent / state.path.name
        state.path.rename(legacy_file)
        legacy_state = legacy_file.read_bytes()
        journal = InflightJournal(self.journal_dir)
        journal.prepare("image", "album", [self.item], target=self.target)
        journal.submitted("image", "album", [123], target=self.target)
        journal.unknown("image", "album", "connection lost", target=self.target)
        old_journal = journal.path_for("image", "album", self.target)
        old_journal_bytes = old_journal.read_bytes()
        (self.journal_dir / "corrupt.json").write_text("invalid", encoding="utf-8")
        queue_path = self.root / "upload_queue.json"
        queue_path.write_text(json.dumps({"schema_version": 1, "tasks": [], "paused": False}), encoding="utf-8")

        database = UploaderDatabase(self.root / "uploader.db")
        self.assertTrue(self.checkpoint(database=database).is_completed(self.item))
        migrated = InflightJournal(self.journal_dir, database=database)
        self.assertEqual(migrated.unresolved("image", "album", self.target)["status"], "UNKNOWN")
        self.assertEqual(migrated.unresolved("image", "other", self.target)["status"], CORRUPT)
        migrated.mark_not_sent("image", "album", target=self.target)
        self.assertTrue(UploadQueue(queue_path, database=database).paused)

        reopened = UploaderDatabase(self.root / "uploader.db")
        self.assertTrue(self.checkpoint(database=reopened).is_completed(self.item))
        self.assertEqual([rec["status"] for rec in
                          InflightJournal(self.journal_dir, database=reopened).list_unresolved()], [CORRUPT])
        self.assertEqual(legacy_file.read_bytes(), legacy_state)
        self.assertEqual(old_journal.read_bytes(), old_journal_bytes)
        self.assertFalse(json.loads(queue_path.read_text(encoding="utf-8"))["paused"])
        with reopened.read() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_failed_receipt_rolls_back_checkpoint_and_keeps_confirmed_protection(self):
        database = UploaderDatabase(self.root / "uploader.db")
        state = self.checkpoint(database=database)
        journal = InflightJournal(self.journal_dir, database=database)
        journal.prepare("image", "album", [self.item], target=self.target)
        journal.submitted("image", "album", [123], target=self.target)
        journal.mark_confirmed("image", "album", [456], target=self.target)

        def finalize():
            return journal.complete_album(state, [self.item], [456], kind="image", album_key="album",
                                          target=self.target, queue_task_id="task")

        with patch.object(journal, "_save_queue_receipt", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                finalize()
        self.assertFalse(state.is_completed(self.item))
        self.assertFalse(self.checkpoint(database=database).is_completed(self.item))
        self.assertEqual(journal.unresolved("image", "album", self.target)["status"], CONFIRMED)
        self.assertTrue(finalize())
        self.assertTrue(state.is_completed(self.item))
        self.assertTrue(self.checkpoint(database=database).is_completed(self.item))
        self.assertIsNone(journal.unresolved("image", "album", self.target))
        with database.read() as connection:
            receipt = connection.execute("SELECT paths FROM queue_album_receipts").fetchone()
        self.assertEqual(json.loads(receipt[0]), [str(self.item["path"])])

    def test_cache_reset_clears_checkpoint_rows_without_reviving_backup_or_losing_unknown(self):
        database = UploaderDatabase(self.root / "uploader.db")
        legacy = self.checkpoint()
        legacy.mark_album_completed([self.item], [12])
        backup = legacy.path.read_bytes()
        self.assertTrue(self.checkpoint(database=database).is_completed(self.item))
        journal = InflightJournal(self.journal_dir, database=database)
        journal.prepare("image", "album", [self.item], target=self.target)
        journal.unknown("image", "album", "timeout", target=self.target)
        with database.write() as connection:
            connection.execute("INSERT INTO media_metadata_cache VALUES ('test', 1, 2, 'probe', '{}')")
        targets = {
            "image_state": ("Images", self.state_dir),
            "media_metadata": ("Metadata", database.path),
            "upload_inflight": ("Journal", self.journal_dir),
        }
        with patch.object(cache_service, "DATA_DIR", self.root), patch.object(cache_service, "CACHE_TARGETS", targets):
            removed, errors = cache_service.clear_cache(tuple(targets), targets)
        self.assertEqual((removed, errors), (["Images", "Metadata"], []))
        self.assertEqual(journal.unresolved("image", "album", self.target)["status"], "UNKNOWN")
        self.assertFalse(self.checkpoint(database=database).is_completed(self.item))
        self.assertEqual(legacy.path.read_bytes(), backup)
        with database.read() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM media_metadata_cache").fetchone()[0], 0)

    def test_restarted_queue_observes_unknown_and_rechecks_the_same_sqlite_transfer(self):
        task = task_for(self.root / "media")
        database = UploaderDatabase(self.root / "uploader.db")
        paths = SimpleNamespace(IMAGE_STATE_DIR=self.state_dir, UPLOAD_INFLIGHT_DIR=self.journal_dir)
        store_path = self.root / "upload_queue.json"
        store = UploadQueue(store_path, runtime_paths=paths, database=database)
        store.enqueue(task)
        store.start(store.prepare(task))
        journal = InflightJournal(self.journal_dir, database=database)
        plan = task.plans[0]
        target = dict(task.target)
        items = [{"path": item.path, "size": item.snapshot.size, "mtime_ns": item.snapshot.mtime_ns}
                 for item in plan.items]
        journal.prepare(task.kind, plan.key, items, target=target)
        journal.unknown(task.kind, plan.key, "timeout", target=target)

        store = UploadQueue(store_path, runtime_paths=paths, database=database)
        self.assertEqual(store.tasks[0].status, TaskStatus.NEEDS_REVIEW)
        with self.assertRaisesRegex(ValueError, "未解决"):
            store.restore(task.task_id)
        journal.mark_not_sent(task.kind, plan.key, target=target)
        self.assertEqual(store.recheck(task.task_id).status, TaskStatus.INTERRUPTED)
        self.assertEqual(store.restore(task.task_id).status, TaskStatus.PENDING)
