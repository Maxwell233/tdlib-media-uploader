import unittest
import tempfile
import json
from pathlib import Path
from tdlib_media_uploader.core.upload_journal import InflightJournal


class TestUnresolvedEdgeCases(unittest.TestCase):
    def test_list_unresolved_edge_cases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal = InflightJournal(root)

            # empty
            self.assertEqual(journal.list_unresolved(), [])

            def write_record(name, status, updated_at):
                path = root / f"{name}.json"
                record = {
                    "kind": "image",
                    "album_key": name,
                    "status": status,
                    "updated_at": updated_at,
                }
                with path.open("w", encoding="utf-8") as f:
                    json.dump(record, f)

            write_record("failed1", "PREPARED", "2024-01-01T00:00:00Z")
            write_record("success1", "FAILED", "2024-01-02T00:00:00Z")
            write_record("unknown1", "UNKNOWN", "2024-01-03T00:00:00Z")

            # Use refresh boundary
            # UNRESOLVED includes PREPARED, UNKNOWN, but NOT FAILED
            unresolved = journal.list_unresolved(refresh=True)
            self.assertEqual(len(unresolved), 2)  # PREPARED, UNKNOWN

            # Bounded larger set
            for i in range(100):
                write_record(f"prepared_large_{i}", "PREPARED", "2024-01-01T00:00:00Z")

            journal.invalidate()
            unresolved_after_refresh = journal.list_unresolved()
            self.assertEqual(len(unresolved_after_refresh), 102)


if __name__ == "__main__":
    unittest.main()
