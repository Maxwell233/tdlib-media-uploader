import unittest
from pathlib import Path
from unittest.mock import patch

from tdlib_media_uploader.core.album import filename_description, album_key


class TestAlbumEdgeCases(unittest.TestCase):
    def test_filename_description_edge_cases(self):
        self.assertEqual(filename_description([]), '')
        items = [{'path': 'file1.txt'}, {'path': 'file2.jpg'}]
        self.assertEqual(filename_description(iter(items)), '1. file1\n2. file2')
        self.assertEqual(filename_description(iter(items), numbered=False), 'file1\nfile2')
        item = [{'path': 'abcdefgh.txt'}]
        for limit, expected in ((1, '…'), (4, 'abc…'), (8, 'abcdefgh')):
            with self.subTest(limit=limit):
                self.assertEqual(filename_description(item, numbered=False,
                                                     max_chars=limit), expected)

    def test_album_key_empty_and_generator_identities_without_stat(self):
        root = Path('album-root')
        first = {'path': root / 'first.jpg', 'size': 100, 'mtime_ns': 1000}
        second = {'path': root / 'second.jpg', 'size': 200, 'mtime_ns': 2000}
        with patch.object(Path, 'stat', side_effect=AssertionError('late stat')):
            empty = album_key('image', 'group', [], root=root)
            self.assertEqual(len(empty), 24)
            self.assertEqual(empty, album_key('image', 'group', iter(()), root=root))
            self.assertNotEqual(empty, album_key('image', 'other', [], root=root))
            self.assertNotEqual(empty, album_key('video', 'group', [], root=root))
            self.assertNotEqual(empty, album_key('image', 'group', [], root=Path('other-root')))
            key = album_key('image', 'group', [first, second], root=root)
            self.assertEqual(key, album_key('image', 'group', iter([first, second]), root=root))
            self.assertNotEqual(key, album_key('image', 'group', [second, first], root=root))
            for field in ('size', 'mtime_ns'):
                changed = dict(first, **{field: first[field] + 1})
                with self.subTest(field=field):
                    self.assertNotEqual(key, album_key('image', 'group', [changed, second], root=root))


if __name__ == '__main__':
    unittest.main()
