"""Regressions for content-based discovery, cancellation and preflight costs."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image, features

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tdlib_media_uploader.config import loader as cfg
from tdlib_media_uploader.media import mixed_pipeline as mixed_core
from tdlib_media_uploader.media import video_pipeline as video_core
from tdlib_media_uploader.media.video_probe import (
    VideoMediaInfo,
    has_video_container_signature,
)


def _ftyp(major, *compatible):
    payload = b"ftyp" + major + b"\x00" * 4 + b"".join(compatible)
    return (len(payload) + 4).to_bytes(4, "big") + payload


class DetectedContainerRegressionTest(unittest.TestCase):
    def test_image_brands_are_not_video_signatures(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            for brand in (b"avif", b"avis", b"mif1", b"msf1", b"heic", b"heix", b"hevc", b"hevx"):
                for major, compatible in ((brand, b"isom"), (b"isom", brand)):
                    with self.subTest(major=major, compatible=compatible):
                        path.write_bytes(_ftyp(major, compatible))
                        self.assertFalse(has_video_container_signature(path))

    def test_video_signatures_still_accept_unknown_suffixes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.bin"
            path.write_bytes(_ftyp(b"isom", b"mp42"))
            self.assertTrue(has_video_container_signature(path))
            path.write_bytes(b"\x47" + b"\x00" * 187 + b"\x47" + b"\x00" * 187)
            self.assertTrue(has_video_container_signature(path))

    def test_image_brand_bytes_outside_ftyp_do_not_reclassify_video(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.bin"
            path.write_bytes(_ftyp(b"isom", b"mp42") + b"\x00\x00\x00\x0cmdatavif")
            self.assertTrue(has_video_container_signature(path))

    @unittest.skipUnless(features.check("avif"), "Pillow AVIF codec is unavailable")
    def test_misnamed_avif_photo_survives_mixed_scan_and_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo = root / "photo.jpg"
            Image.new("RGB", (32, 32), "blue").save(photo, format="AVIF")
            with patch.object(cfg, "MIXED_DIR", root), \
                    patch.object(cfg, "MIXED_IMAGE_EXTENSIONS", {".jpg"}), \
                    patch.object(cfg, "MIXED_VIDEO_EXTENSIONS", {".mp4"}), \
                    patch.object(cfg, "SCAN_STABILITY_CHECKS_LOCAL", 1):
                items = mixed_core._group_items(root, "album")
                self.assertEqual([(item["path"], item["media_kind"]) for item in items], [(photo, "image")])
                self.assertEqual(mixed_core.preflight_mixed(items), [])

    def test_real_mp4_with_image_or_unknown_suffix_is_still_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = (PROJECT_ROOT / "tests/fixtures/remux_h264_aac.mp4").read_bytes()
            paths = [root / "clip.bin", root / "clip.jpg"]
            for path in paths:
                path.write_bytes(fixture)
            with patch.object(cfg, "MIXED_IMAGE_EXTENSIONS", {".jpg"}), \
                    patch.object(cfg, "MIXED_VIDEO_EXTENSIONS", {".mp4"}):
                items = mixed_core._group_items(root, "album")
            self.assertEqual({item["path"] for item in items}, set(paths))
            self.assertTrue(all(item["media_kind"] == "video" for item in items))

    def _cancel_header_scan(self, mixed):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(32):
                (root / f"clip-{index:02}.bin").write_bytes(_ftyp(b"isom", b"mp42"))
            event = threading.Event()
            opened = []

            def read_header(path):
                opened.append(path)
                detected = has_video_container_signature(path)
                event.set()
                return detected

            with patch.object(cfg, "VIDEO_DIR", root), \
                    patch.object(cfg, "VIDEO_EXTENSIONS", {".mp4"}), \
                    patch("tdlib_media_uploader.media.video_probe.has_video_container_signature", side_effect=read_header):
                if mixed:
                    result = mixed_core._group_items(root, "album", cancel_event=event)
                else:
                    result = video_core.scan_videos(cancel_event=event)
            self.assertEqual(len(opened), 1, "Cancellation must stop before opening another file")
            self.assertEqual(result, [])

    def test_video_scan_cancellation_stops_remaining_header_reads(self):
        self._cancel_header_scan(mixed=False)

    def test_mixed_scan_cancellation_stops_remaining_header_reads(self):
        self._cancel_header_scan(mixed=True)

    def test_oversized_native_video_is_rejected_without_thumbnail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes((PROJECT_ROOT / "tests/fixtures/remux_h264_aac.mp4").read_bytes())
            info = VideoMediaInfo(container="mp4", video_codec="h264", audio_codec="aac", width=64, height=48, duration=1.0, fps=10.0, has_video_stream=True, compatibility="native")
            with patch.object(cfg, "VIDEO_MAX_BYTES", path.stat().st_size - 1), \
                    patch.object(cfg, "VIDEO_GENERATE_THUMBNAIL", True), \
                    patch.object(cfg, "SCAN_STABILITY_CHECKS_LOCAL", 1), \
                    patch.object(video_core, "video_info", return_value=info), \
                    patch.object(video_core, "build_thumbnail") as thumbnail:
                skipped = video_core.preflight_videos([{"path": path}])
            self.assertEqual(len(skipped), 1)
            self.assertEqual(skipped[0]["category"], "size")
            thumbnail.assert_not_called()


if __name__ == "__main__":
    unittest.main()
