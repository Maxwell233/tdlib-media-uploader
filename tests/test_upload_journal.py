from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.core.upload_journal import (  # noqa: E402
    CONFIRMED,
    CORRUPT,
    InflightJournal,
    UNKNOWN,
)


class UploadJournalTest(unittest.TestCase):
    def test_direct_lookup_and_legacy_fallback_build_index_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = InflightJournal(root)
            journal.prepare(
                "image",
                "legacy-album",
                [{"path": "photo.jpg", "size": 10, "mtime_ns": 20}],
            )
            current = journal.path_for("image", "legacy-album")
            legacy_path = root / "old-name.json"
            current.rename(legacy_path)
            journal.invalidate()

            _path, first = journal.get_entry("image", "legacy-album")
            index_identity = id(journal._index)
            _path, second = journal.get_entry("image", "legacy-album")

            self.assertEqual(first["album_key"], "legacy-album")
            self.assertEqual(second["status"], "PREPARED")
            self.assertEqual(id(journal._index), index_identity)

    def test_cache_is_invalidated_after_write_update_not_sent_and_finalize(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = InflightJournal(Path(directory))
            journal.prepare("image", "album", [{"path": "photo.jpg"}])
            self.assertEqual(len(journal.list_unresolved(refresh=True)), 1)

            journal.update("image", "album", UNKNOWN, error="timeout")
            self.assertEqual(journal.unresolved("image", "album")["error"], "timeout")

            journal.mark_not_sent("image", "album")
            self.assertEqual(journal.list_unresolved(), [])

            journal.prepare("image", "album", [{"path": "photo.jpg"}])
            journal.mark_confirmed("image", "album", [11])
            self.assertEqual(journal.unresolved("image", "album")["status"], CONFIRMED)
            self.assertTrue(journal.finalize("image", "album"))
            self.assertEqual(journal.list_unresolved(), [])

    def test_target_scoping_and_unresolved_states_block_automatic_prepare(self):
        first = {"target_mode": "forum_topic", "chat_id": -100, "forum_topic_id": 1}
        second = {"target_mode": "forum_topic", "chat_id": -100, "forum_topic_id": 2}
        with tempfile.TemporaryDirectory() as directory:
            journal = InflightJournal(Path(directory))
            journal.prepare("image", "album", [{"path": "photo.jpg"}], target=first)
            journal.unknown("image", "album", "timeout", target=first)
            self.assertIsNotNone(journal.unresolved("image", "album", target=first))
            self.assertIsNone(journal.unresolved("image", "album", target=second))
            with self.assertRaises(RuntimeError):
                journal.prepare("image", "album", [{"path": "photo.jpg"}], target=first)

            journal.mark_confirmed("image", "album", [7], target=first)
            with self.assertRaises(ValueError):
                journal.mark_not_sent("image", "album", target=first)

    def test_corrupt_json_is_visible_and_remains_a_fail_closed_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "truncated.json"
            path.write_text('{"kind":"image","status":"UNKNOWN"', encoding="utf-8")
            journal = InflightJournal(root)

            records = journal.list_unresolved(refresh=True)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], CORRUPT)
            self.assertEqual(records[0]["journal_path"], str(path))
            self.assertIn("JSONDecodeError", records[0]["error"])
            self.assertEqual(journal.unresolved("image", "any-album")["status"], CORRUPT)
            with self.assertRaises(RuntimeError):
                journal.prepare("image", "any-album", [{"path": "photo.jpg"}])
            with self.assertRaises(RuntimeError):
                journal.finalize("image", "any-album")

    def test_unreadable_journal_is_reported_without_being_treated_as_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "unreadable.json"
            path.write_text(
                json.dumps({"kind": "image", "album_key": "album", "status": UNKNOWN}),
                encoding="utf-8",
            )
            journal = InflightJournal(root)
            original = Path.read_text

            def deny_only_this_path(value, *args, **kwargs):
                if value == path:
                    raise PermissionError("permission denied")
                return original(value, *args, **kwargs)

            with patch.object(Path, "read_text", deny_only_this_path):
                records = journal.list_unresolved(refresh=True)
                self.assertEqual(records[0]["status"], CORRUPT)
                self.assertIn("PermissionError", records[0]["error"])

    def test_unresolved_records_are_sorted_latest_updated_first(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = InflightJournal(root)
            for album in ("old", "new"):
                journal.prepare("image", album, [{"path": f"{album}.jpg"}])
                journal.unknown("image", album, "timeout")
            for album, updated in (
                ("old", "2026-01-01T00:00:00+00:00"),
                ("new", "2026-09-18T00:00:00+00:00"),
            ):
                path = journal.path_for("image", album)
                record = json.loads(path.read_text(encoding="utf-8"))
                record["updated_at"] = updated
                path.write_text(json.dumps(record), encoding="utf-8")

            records = journal.list_unresolved(refresh=True)
            self.assertEqual([record["album_key"] for record in records], ["new", "old"])

    def test_legacy_record_without_items_is_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = InflightJournal(Path(directory))
            journal.prepare("image", "legacy", items=None)
            record = journal.unresolved("image", "legacy")
            self.assertEqual(record["items"], [])
            self.assertEqual(record["status"], "PREPARED")


if __name__ == "__main__":
    unittest.main()
