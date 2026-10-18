import unittest
from unittest.mock import patch
from tdlib_media_uploader.core.filesystem_legacy import (
    is_link_or_junction,
    relative_name,
)


class TestFilesystemEdgeCases(unittest.TestCase):
    @patch("tdlib_media_uploader.core.filesystem_legacy.os.lstat")
    def test_is_link_or_junction_exceptions(self, mock_lstat):
        mock_lstat.side_effect = OSError("Mocked OSError")
        self.assertFalse(is_link_or_junction("some_path"))

        mock_lstat.side_effect = TypeError("Mocked TypeError")
        self.assertFalse(is_link_or_junction("some_path"))

        mock_lstat.side_effect = ValueError("Mocked ValueError")
        self.assertFalse(is_link_or_junction("some_path"))

    def test_relative_name_edge_cases(self):
        # outside root fallback returns the path name
        with (
            patch(
                "tdlib_media_uploader.core.filesystem_legacy.ntpath.relpath",
                return_value="../outside",
            ),
            patch(
                "tdlib_media_uploader.core.filesystem_legacy.os.path.relpath",
                return_value="../outside",
            ),
        ):
            self.assertEqual(relative_name("/outside/path", "/root"), "path")

        with (
            patch(
                "tdlib_media_uploader.core.filesystem_legacy.ntpath.relpath",
                side_effect=OSError("Mocked OSError"),
            ),
            patch(
                "tdlib_media_uploader.core.filesystem_legacy.os.path.relpath",
                side_effect=OSError("Mocked OSError"),
            ),
        ):
            self.assertEqual(relative_name("/path", "/root"), "path")

        with (
            patch(
                "tdlib_media_uploader.core.filesystem_legacy.ntpath.relpath",
                side_effect=ValueError("Mocked ValueError"),
            ),
            patch(
                "tdlib_media_uploader.core.filesystem_legacy.os.path.relpath",
                side_effect=ValueError("Mocked ValueError"),
            ),
        ):
            self.assertEqual(relative_name("/path", "/root"), "path")


if __name__ == "__main__":
    unittest.main()
