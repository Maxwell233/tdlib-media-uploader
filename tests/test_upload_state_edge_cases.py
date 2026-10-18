import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from tdlib_media_uploader.core.upload_state import UploadState


class TestUploadStateEdgeCases(unittest.TestCase):
    def test_upload_state_is_completed_signature_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = UploadState(
                kind="image", source_root=root, state_dir=root / "state"
            )

            item = {"path": str(root / "a.jpg"), "size": 100, "mtime_ns": 1000}

            # mock signature error
            with patch(
                "tdlib_media_uploader.core.upload_state.media_file_signature",
                side_effect=ValueError("Mocked signature error"),
            ):
                # Should be caught and return False
                self.assertFalse(state.is_completed(item))

    def test_mark_album_completed_empty_input_and_short_message_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = UploadState(
                kind="image", source_root=root, state_dir=root / "state"
            )

            # empty items
            state.mark_album_completed([], message_ids=[123])

            item1 = {"path": str(root / "a.jpg"), "size": 100, "mtime_ns": 1000}
            item2 = {"path": str(root / "b.jpg"), "size": 100, "mtime_ns": 1000}

            # short message_ids
            state.mark_album_completed([item1, item2], message_ids=[123])

            # verify the state reflects completion
            self.assertTrue(state.is_completed(item1))
            self.assertTrue(state.is_completed(item2))


if __name__ == "__main__":
    unittest.main()
