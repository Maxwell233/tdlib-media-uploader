"""Regressions for extension filtering after directory entry extraction."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tdlib_media_uploader.core.filesystem import iter_files, stable_path


class DirectoryEntryFilterTest(unittest.TestCase):
    def test_none_filter_discovers_nested_unknown_extensions_and_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "nested"
            child.mkdir()
            paths = [root / "clip.bin", child / "clip", child / "photo.JPG"]
            for path in paths:
                path.write_bytes(b"media")
            (root / "empty.mp4").touch()
            result = iter_files(root, None)
            self.assertEqual(set(result.paths), set(paths))
            self.assertEqual(result.errors, [])
            for path in paths:
                snapshot = result.snapshots[stable_path(path)]
                self.assertEqual(snapshot.size, path.stat().st_size)
                self.assertEqual(snapshot.mtime_ns, path.stat().st_mtime_ns)

    def test_explicit_filter_still_traverses_directories_and_filters_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "nested"
            child.mkdir()
            selected = child / "photo.JPG"
            selected.write_bytes(b"photo")
            (root / "clip.bin").write_bytes(b"video")
            result = iter_files(root, {".jpg"})
            self.assertEqual(result.paths, [selected])
            self.assertEqual(set(result.snapshots), {stable_path(selected)})


if __name__ == "__main__":
    unittest.main()
