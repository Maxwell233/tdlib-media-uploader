import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from tdlib_media_uploader.config.paths import read_version, ensure_data_dirs

class TestConfigPathsEdgeCases(unittest.TestCase):
    def test_read_version_edge_cases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            # missing file
            with patch('tdlib_media_uploader.config.paths.VERSION_PATH', root / "VERSION"):
                self.assertEqual(read_version(default="fallback_version"), "fallback_version")

            # empty file
            version_file = root / "VERSION"
            version_file.touch()
            with patch('tdlib_media_uploader.config.paths.VERSION_PATH', version_file):
                self.assertEqual(read_version(default="fallback_version"), "fallback_version")

            # Unreadable file (mock read_text error)
            with patch('pathlib.Path.read_text', side_effect=OSError("Mocked unreadable")):
                self.assertEqual(read_version(default="fallback_version"), "fallback_version")

    def test_ensure_data_dirs_edge_cases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            expected = ["data", "video", "image", "mixed", "telegram", "db", "files",
                        "captions", "inflight", "thumb", "compress", "staging", "logs"]

            # mock the directory list
            with patch('tdlib_media_uploader.config.paths.DATA_DIR', root / "data"), \
                 patch('tdlib_media_uploader.config.paths.VIDEO_STATE_DIR', root / "video"), \
                 patch('tdlib_media_uploader.config.paths.IMAGE_STATE_DIR', root / "image"), \
                 patch('tdlib_media_uploader.config.paths.MIXED_STATE_DIR', root / "mixed"), \
                 patch('tdlib_media_uploader.config.paths.TELEGRAM_DIR', root / "telegram"), \
                 patch('tdlib_media_uploader.config.paths.TDLIB_DATABASE_DIR', root / "db"), \
                 patch('tdlib_media_uploader.config.paths.TDLIB_FILES_DIR', root / "files"), \
                 patch('tdlib_media_uploader.config.paths.CAPTIONS_DIR', root / "captions"), \
                 patch('tdlib_media_uploader.config.paths.UPLOAD_INFLIGHT_DIR', root / "inflight"), \
                 patch('tdlib_media_uploader.config.paths.THUMBNAIL_CACHE_DIR', root / "thumb"), \
                 patch('tdlib_media_uploader.config.paths.IMAGE_COMPRESSION_CACHE_DIR', root / "compress"), \
                 patch('tdlib_media_uploader.config.paths.STAGING_CACHE_DIR', root / "staging"), \
                 patch('tdlib_media_uploader.config.paths.LOG_DIR', root / "logs"):

                # Test idempotency - run twice
                self.assertEqual(ensure_data_dirs(), root / "data")
                self.assertTrue(all((root / name).is_dir() for name in expected))

                ensure_data_dirs()

                # Test failure propagation
                with patch('pathlib.Path.mkdir', side_effect=OSError("Mocked os.makedirs error")):
                    with self.assertRaises(OSError):
                        ensure_data_dirs()

if __name__ == '__main__':
    unittest.main()
