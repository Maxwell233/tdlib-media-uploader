import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tdlib_media_uploader.core.upload_state import UploadState


class TestUploadStateEdgeCases(unittest.TestCase):
    def test_upload_state_is_completed_signature_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = UploadState(kind='image', source_root=root, state_dir=root / 'state')
            item = {'path': root / 'a.jpg', 'size': 100, 'mtime_ns': 1000}
            for error in (OSError, TypeError, ValueError):
                with self.subTest(error=error.__name__), patch.object(
                    state, 'signature', side_effect=error('unavailable')
                ) as signature:
                    self.assertFalse(state.is_completed(item))
                    signature.assert_called_once_with(item)
            with patch.object(state, 'signature', side_effect=RuntimeError('unexpected')):
                with self.assertRaises(RuntimeError):
                    state.is_completed(item)

    def test_mark_album_completed_empty_input_and_short_message_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kwargs = dict(kind='image', source_root=root, state_dir=root / 'state')
            state = UploadState(**kwargs)
            state.mark_album_completed([], message_ids=[123])
            self.assertEqual(state.data['completed'], {})
            self.assertEqual(json.loads(state.path.read_text())['completed'], {})
            first = {'path': root / 'a.jpg', 'size': 100, 'mtime_ns': 1000}
            second = dict(first, path=root / 'b.jpg')
            state.mark_album_completed(iter([first, second]), message_ids=[123])
            saved = json.loads(state.path.read_text())['completed']
            self.assertEqual(saved[state.signature(first)]['message_id'], 123)
            self.assertIsNone(saved[state.signature(second)]['message_id'])
            restored = UploadState(**kwargs)
            self.assertTrue(restored.is_completed(first))
            self.assertTrue(restored.is_completed(second))
            self.assertEqual(restored.data['completed'], saved)


if __name__ == '__main__':
    unittest.main()
