import unittest
from unittest.mock import patch
from pathlib import Path
from tdlib_media_uploader.core.sorting import natural_sort, media_path_sort


class TestSortingEdgeCases(unittest.TestCase):
    def test_natural_sort_edge_cases(self):
        # Empty inputs
        self.assertEqual(natural_sort([]), [])

        # Generator input
        gen = (x for x in ["file10", "file2", "file1"])
        self.assertEqual(natural_sort(gen), ["file1", "file2", "file10"])

        # Stable equal keys
        class MockItem:
            def __init__(self, key, id_val):
                self.key = key
                self.id_val = id_val

            def __str__(self):
                return self.key

        item1 = MockItem("A", 1)
        item2 = MockItem("A", 2)
        sorted_items = natural_sort([item1, item2], key=lambda x: x.key)
        self.assertIs(sorted_items[0], item1)
        self.assertIs(sorted_items[1], item2)

        # Bounded larger collection
        large_list = [f"file{i}" for i in range(1000, 0, -1)]
        sorted_large = natural_sort(large_list)
        self.assertEqual(sorted_large[0], "file1")
        self.assertEqual(sorted_large[-1], "file1000")

    def test_media_path_sort_edge_cases(self):
        # empty input
        self.assertEqual(media_path_sort([], "/root", mode="mtime"), [])

        paths = [Path("b"), Path("a")]

        # Invalid mode throws ValueError
        with self.assertRaises(ValueError):
            media_path_sort(paths, "/root", mode="invalid_mode")

        # missing or invalid mtime keys when mode="mtime" fallback
        with patch(
            "tdlib_media_uploader.core.sorting._default_mtime", return_value=0.0
        ):
            sorted_mtime = media_path_sort(paths, "/root", mode="mtime")
            # it falls back to relative name ascending on tie
            self.assertEqual(sorted_mtime, [Path("a"), Path("b")])

        # invalid mtime values like a string
        sorted_invalid_mtime = media_path_sort(
            paths, "/root", mode="mtime", mtime_key=lambda x: "invalid"
        )
        self.assertEqual(sorted_invalid_mtime, [Path("a"), Path("b")])


if __name__ == "__main__":
    unittest.main()
