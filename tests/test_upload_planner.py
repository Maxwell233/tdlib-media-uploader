import unittest
from pathlib import Path

from tdlib_media_uploader.core.models import MediaItem, FileSnapshot
from tdlib_media_uploader.upload.planner import item_identity


class TestPlanner(unittest.TestCase):
    def test_item_identity_extracts_properties_to_tuple(self):
        snapshot = FileSnapshot(path="test/path/a.jpg", size=1024, mtime_ns=1234567890)
        item = MediaItem(
            path=Path("test/path/a.jpg"),
            source_root=Path("test/path"),
            media_kind="image",
            snapshot=snapshot,
        )

        identity = item_identity(item)

        self.assertEqual(
            identity,
            (
                str(Path("test/path/a.jpg")),
                str(Path("test/path")),
                "image",
                "test/path/a.jpg",
                1024,
                1234567890,
            ),
        )

    def test_item_identity_raises_type_error_for_non_media_item(self):
        with self.assertRaises(TypeError) as ctx:
            item_identity({"path": "a.jpg"})

        self.assertIn("Album item 必须是 MediaItem，而不是 dict", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
