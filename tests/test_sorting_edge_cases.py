import unittest
from pathlib import Path
from unittest.mock import patch

from tdlib_media_uploader.core.sorting import natural_sort, media_path_sort


class TestSortingEdgeCases(unittest.TestCase):
    def test_natural_sort_edge_cases(self):
        self.assertEqual(natural_sort([]), [])
        self.assertEqual(natural_sort(iter(['file10', 'file2', 'file1'])),
                         ['file1', 'file2', 'file10'])
        first, second = {'key': 'same', 'id': 1}, {'key': 'same', 'id': 2}
        ordered = natural_sort([first, second], key=lambda item: item['key'])
        self.assertIs(ordered[0], first)
        self.assertIs(ordered[1], second)
        self.assertEqual(natural_sort([f'file{i}' for i in range(1000, 0, -1)]),
                         [f'file{i}' for i in range(1, 1001)])

    def test_media_path_sort_empty_generator_and_stability(self):
        root = Path('sort-root')
        for mode in ('name', 'mtime'):
            with self.subTest(mode=mode):
                self.assertEqual(media_path_sort([], root, mode=mode), [])
                paths = [root / name for name in ('file10', 'file2', 'file1')]
                self.assertEqual(
                    media_path_sort(iter(paths), root, mode=mode, mtime_key=lambda _: 0),
                    [root / name for name in ('file1', 'file2', 'file10')],
                )
                first, second = {'id': 1}, {'id': 2}
                ordered = media_path_sort(
                    iter([first, second]), root, mode=mode,
                    path_key=lambda _: root / 'same.jpg', mtime_key=lambda _: 1,
                )
                self.assertIs(ordered[0], first)
                self.assertIs(ordered[1], second)

    def test_media_path_sort_error_paths(self):
        root = Path('sort-root')
        paths = [root / 'b', root / 'a']
        with self.assertRaises(ValueError):
            media_path_sort(paths, root, mode='invalid')
        self.assertEqual(media_path_sort(paths, root, mode='mtime',
                                        mtime_key=lambda _: 'invalid'), paths[::-1])
        for error in (OSError, TypeError, ValueError):
            with self.subTest(error=error.__name__), patch(
                'tdlib_media_uploader.core.sorting._default_mtime',
                side_effect=error('mtime unavailable'),
            ) as get_mtime:
                self.assertEqual(media_path_sort(paths, root, mode='mtime'), paths[::-1])
                self.assertEqual(get_mtime.call_count, 2)


if __name__ == '__main__':
    unittest.main()
