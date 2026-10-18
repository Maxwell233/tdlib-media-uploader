import json
import tempfile
import unittest
from pathlib import Path

from tdlib_media_uploader.core.upload_journal import InflightJournal


class TestUnresolvedEdgeCases(unittest.TestCase):
    def test_list_unresolved_empty_filter_refresh_and_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = InflightJournal(root)
            self.assertEqual(journal.list_unresolved(), [])

            def write_record(name, status, updated):
                (root / f'{name}.json').write_text(json.dumps({
                    'kind': 'image', 'album_key': name, 'status': status,
                    'updated_at': updated,
                }), encoding='utf-8')

            write_record('failed', 'FAILED', '2024-01-04T00:00:00Z')
            write_record('prepared', 'PREPARED', '2024-01-01T00:00:00Z')
            write_record('confirmed', 'CONFIRMED', '2024-01-02T00:00:00Z')
            write_record('unknown', 'UNKNOWN', '2024-01-03T00:00:00Z')
            # External edits are visible only at the explicit refresh boundary.
            self.assertEqual(journal.list_unresolved(), [])
            self.assertEqual(
                [r['album_key'] for r in journal.list_unresolved(refresh=True)],
                ['unknown', 'confirmed', 'prepared'],
            )
            for index in range(100):
                write_record(f'prepared_{index:03}', 'PREPARED', f'2024-02-01T00:{index // 60:02}:{index % 60:02}Z')
            self.assertEqual(len(journal.list_unresolved()), 3)
            records = journal.list_unresolved(refresh=True)
            self.assertEqual(len(records), 103)
            self.assertNotIn('failed', [r['album_key'] for r in records])
            self.assertEqual(
                [r['updated_at'] for r in records],
                sorted([r['updated_at'] for r in records], reverse=True),
            )


if __name__ == '__main__':
    unittest.main()
