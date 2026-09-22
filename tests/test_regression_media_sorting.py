"""Offline regressions: no Telegram login or network requests."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.core import album as metadata
from tdlib_media_uploader.core import logging as app_logging
from tdlib_media_uploader.gui import main_window as gui
from tdlib_media_uploader.gui import config_service, tools, cache_service
from tdlib_media_uploader.core import filesystem_legacy as path_utils
from PySide6.QtWidgets import QApplication, QGroupBox, QHBoxLayout, QScrollArea


class ImprovementsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


    def test_natural_filename_sort_uses_ascending_numeric_runs(self):
        names = [
            "x.1",
            "x.10",
            "x.100",
            "x.101",
            "x.102",
            "x.103",
            "x.409",
            "x.41",
            "x.410",
        ]
        self.assertEqual(
            path_utils.natural_sort(names),
            ["x.1", "x.10", "x.41", "x.100", "x.101", "x.102", "x.103", "x.409", "x.410"],
        )


    def test_natural_sort_is_deterministic_for_equal_case_and_leading_zero_runs(self):
        values = ["x001", "a1", "x1", "A1", "x01"]
        expected = ["A1", "a1", "x1", "x01", "x001"]
        for _ in range(8):
            shuffled = list(values)
            random.shuffle(shuffled)
            self.assertEqual(path_utils.natural_sort(shuffled), expected)


    def test_natural_sort_handles_unicode_numeric_runs_and_folder_names(self):
        self.assertEqual(
            path_utils.natural_sort(["a（10）", "a（1）", "a（11）", "a（5）"]),
            ["a（1）", "a（5）", "a（10）", "a（11）"],
        )
        root = Path("root")
        paths = [
            root / "Folder20" / "clip1.jpg",
            root / "Folder2" / "clip1.jpg",
            root / "Folder10" / "clip1.jpg",
            root / "Folder1" / "clip1.jpg",
        ]
        ordered = path_utils.media_path_sort(paths, root)
        self.assertEqual(
            [path_utils.relative_name(path, root) for path in ordered],
            [
                "Folder1/clip1.jpg",
                "Folder2/clip1.jpg",
                "Folder10/clip1.jpg",
                "Folder20/clip1.jpg",
            ],
        )


    def test_natural_path_sort_compares_each_directory_component_first(self):
        root = Path("root")
        paths = [
            root / "Day2" / "x.100.jpg",
            root / "Day10" / "x.1.jpg",
            root / "Day2" / "x.10.jpg",
        ]
        ordered = path_utils.media_path_sort(paths, root)
        self.assertEqual(
            [path_utils.relative_name(path, root) for path in ordered],
            ["Day2/x.10.jpg", "Day2/x.100.jpg", "Day10/x.1.jpg"],
        )


    def test_relative_path_sort_compares_directories_before_filenames(self):
        root = Path("root")
        paths = [
            root / "B2" / "x.41.jpg",
            root / "A" / "x.1.jpg",
            root / "B2" / "x.410.jpg",
            root / "A" / "x.100.jpg",
            root / "B10" / "x.2.jpg",
        ]
        ordered = path_utils.media_path_sort(paths, root)
        self.assertEqual(
            [path_utils.relative_name(path, root) for path in ordered],
            ["A/x.1.jpg", "A/x.100.jpg", "B2/x.41.jpg", "B2/x.410.jpg", "B10/x.2.jpg"],
        )


    def test_image_filename_scan_uses_natural_numeric_order(self):
        from tdlib_media_uploader.media import legacy_image as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ["x.1.jpg", "x.10.jpg", "x.100.jpg", "x.41.jpg", "x.410.jpg"]
            for name in names:
                (root / name).write_bytes(b"image")
            with patch.object(core.cfg, "IMAGE_DIR", root), \
                    patch.object(core.cfg, "IMAGE_EXTENSIONS", {".jpg"}), \
                    patch.object(core.cfg, "IMAGE_MAX_BYTES", 100), \
                    patch.object(core.cfg, "IMAGE_SORT_MODE", "path"):
                paths = core.scan_images()
            self.assertEqual(
                [path.name for path in paths],
                ["x.1.jpg", "x.10.jpg", "x.41.jpg", "x.100.jpg", "x.410.jpg"],
            )


    def test_video_filename_scan_uses_natural_numeric_order(self):
        from tdlib_media_uploader.media import legacy_video as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ["x.1.mp4", "x.10.mp4", "x.41.mp4", "x.409.mp4", "x.410.mp4"]
            for name in names:
                (root / name).write_bytes(b"video")
            with patch.object(core.cfg, "VIDEO_DIR", root), \
                    patch.object(core.cfg, "VIDEO_EXTENSIONS", {".mp4"}), \
                    patch.object(core.cfg, "VIDEO_MAX_BYTES", 100), \
                    patch.object(core.cfg, "VIDEO_READ_DATES", False), \
                    patch.object(core.cfg, "VIDEO_SORT_MODE", "name"):
                paths = core.scan_videos()
            self.assertEqual(
                [path.name for path in paths],
                ["x.1.mp4", "x.10.mp4", "x.41.mp4", "x.409.mp4", "x.410.mp4"],
            )


    def test_all_media_scanners_keep_directory_order_before_basename_order(self):
        from tdlib_media_uploader.media import legacy_image as image_core
        from tdlib_media_uploader.media import legacy_mixed as mixed_core
        from tdlib_media_uploader.media import legacy_video as video_core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = ["A/x.1", "A/x.100", "B/x.41", "B/x.410"]
            image_root = root / "images"
            video_root = root / "videos"
            mixed_root = root / "mixed"
            for base, extension in ((image_root, ".jpg"), (video_root, ".mp4")):
                for relative in expected:
                    path = base / f"{relative}{extension}"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"media")
            for relative in expected:
                path = mixed_root / f"{relative}.jpg" if relative.startswith("A/") else mixed_root / f"{relative}.mp4"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"media")

            with patch.object(image_core.cfg, "IMAGE_DIR", image_root), \
                    patch.object(image_core.cfg, "IMAGE_EXTENSIONS", {".jpg"}), \
                    patch.object(image_core.cfg, "IMAGE_MAX_BYTES", 100), \
                    patch.object(image_core.cfg, "IMAGE_SORT_MODE", "path"):
                image_names = [path.relative_to(image_root).with_suffix("").as_posix() for path in image_core.scan_images()]
            with patch.object(video_core.cfg, "VIDEO_DIR", video_root), \
                    patch.object(video_core.cfg, "VIDEO_EXTENSIONS", {".mp4"}), \
                    patch.object(video_core.cfg, "VIDEO_MAX_BYTES", 100), \
                    patch.object(video_core.cfg, "VIDEO_READ_DATES", False):
                video_names = [path.relative_to(video_root).with_suffix("").as_posix() for path in video_core.scan_videos()]
            with patch.object(mixed_core.cfg, "MIXED_DIR", mixed_root), \
                    patch.object(mixed_core.cfg, "MIXED_IMAGE_EXTENSIONS", {".jpg"}), \
                    patch.object(mixed_core.cfg, "MIXED_VIDEO_EXTENSIONS", {".mp4"}), \
                    patch.object(mixed_core.cfg, "MIXED_EXTENSIONS", {".jpg", ".mp4"}), \
                    patch.object(mixed_core.cfg, "MIXED_ALBUM_SIZE", 10), \
                    patch.object(mixed_core.cfg, "MIXED_SORT_MODE", "name"):
                mixed_groups = mixed_core.scan_mixed_groups()
                mixed_names = [
                    f"{group['group_name']}/{path_utils.relative_name(item['path'], group['group_path']).rsplit('.', 1)[0]}"
                    for group in mixed_groups
                    for item in group["items"]
                ]
            self.assertEqual(image_names, expected)
            self.assertEqual(video_names, expected)
            self.assertEqual(mixed_names, expected)


    def test_extension_filter_preserves_path_suffix_edge_cases(self):
        self.assertEqual(path_utils._entry_suffix("photo."), "")
        self.assertEqual(path_utils._entry_suffix("photo.."), "")
        self.assertEqual(path_utils._entry_suffix(".hidden"), "")
        self.assertEqual(path_utils._entry_suffix("photo.jpg"), ".jpg")

    def test_video_scanner_ignores_document_only_extensions(self):
        from tdlib_media_uploader.media import legacy_video as core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "nested"
            nested.mkdir()
            (nested / "clip.WMV").write_bytes(b"video")
            (nested / "clip.MP4").write_bytes(b"video")
            with patch.object(core.cfg, "VIDEO_DIR", root), \
                    patch.object(core.cfg, "VIDEO_EXTENSIONS", {".mp4"}), \
                    patch.object(core.cfg, "VIDEO_MAX_BYTES", 100), \
                    patch.object(core.cfg, "VIDEO_READ_DATES", False), \
                    patch.object(core.cfg, "VIDEO_SORT_MODE", "name"):
                paths = core.scan_videos()
            self.assertEqual([path.name for path in paths], ["clip.MP4"])
