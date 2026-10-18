import unittest
from tdlib_media_uploader.core.album import filename_description, album_key


class TestAlbumEdgeCases(unittest.TestCase):
    def test_filename_description_edge_cases(self):
        # empty
        self.assertEqual(filename_description([]), "")

        # generator
        gen = (x for x in [{"path": "file1.txt"}, {"path": "file2.jpg"}])
        self.assertEqual(filename_description(gen, numbered=True), "1. file1\n2. file2")

        # numbering False
        self.assertEqual(
            filename_description(
                [{"path": "a.txt"}, {"path": "b.jpg"}], numbered=False
            ),
            "a\nb",
        )

        # positive length boundaries
        # Just 1 element
        self.assertEqual(filename_description([{"path": "a.txt"}]), "1. a")

    def test_album_key_edge_cases(self):
        # empty
        self.assertEqual(
            album_key("kind", "group1", [], root="/root"),
            album_key("kind", "group1", [], root="/root"),
        )

        # generator
        gen1 = (x for x in [{"path": "/root/file1", "size": 100, "mtime_ns": 1000}])
        gen2 = (x for x in [{"path": "/root/file1", "size": 100, "mtime_ns": 1000}])
        self.assertEqual(
            album_key("kind", "group1", gen1, root="/root"),
            album_key("kind", "group1", gen2, root="/root"),
        )

        # determinism / identity-sensitive changes without live stat
        item1 = {"path": "/root/file1", "size": 100, "mtime_ns": 1000}
        item2 = {"path": "/root/file1", "size": 100, "mtime_ns": 2000}
        self.assertNotEqual(
            album_key("kind", "group1", [item1], root="/root"),
            album_key("kind", "group1", [item2], root="/root"),
        )


if __name__ == "__main__":
    unittest.main()
