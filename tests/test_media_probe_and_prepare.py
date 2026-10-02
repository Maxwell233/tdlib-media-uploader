# -*- coding: utf-8 -*-
"""Comprehensive tests for video media probing, image preparation, and mixed media handling.

Validates:
1. Video probe: container/codec/stream detection, native/unsupported/invalid classification,
   ffprobe discovery, safe fallbacks, payload construction with supports_streaming.
2. Image probe & prepare: Telegram photo limits, non-destructive normalization,
   downscaling, JPEG compression, WebP/BMP/TIFF transcoding, alpha compositing,
   EXIF orientation, extreme aspect ratio padding/skipping, animation skipping, cache behavior.
3. Strict guarantees: source files never modified, unsupported files never silently
   downgraded to inputMessageDocument.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from PIL import Image

from tdlib_media_uploader.config import loader as cfg
from tdlib_media_uploader.config.paths import CACHE_DIR, IMAGE_COMPRESSION_CACHE_DIR
from tdlib_media_uploader.media import legacy_image as image_core
from tdlib_media_uploader.media import legacy_mixed as mixed_core
from tdlib_media_uploader.media import legacy_video as video_core
from tdlib_media_uploader.media.image_prepare import (
    PreparedImage,
    get_image_cache_key,
    prepare_image_for_telegram,
)
from tdlib_media_uploader.media.image_probe import (
    PHOTO_TARGET_MAX_SIDE,
    TELEGRAM_PHOTO_MAX_ASPECT_RATIO,
    TELEGRAM_PHOTO_MAX_BYTES,
    TELEGRAM_PHOTO_MAX_DIMENSION_SUM,
    TELEGRAM_PHOTO_TARGET_BYTES,
    ImageMediaInfo,
    probe_image,
)
from tdlib_media_uploader.media.video_probe import (
    VideoMediaInfo,
    _find_ffprobe,
    determine_supports_streaming,
    format_unsupported_reason,
    probe_video,
)


def _ffprobe_mock_json(
    *,
    container="mov,mp4,m4a,3gp,3g2,mj2",
    video_codec="h264",
    audio_codec="aac",
    width=1920,
    height=1080,
    duration="10.5",
    fps="30/1",
    include_video_stream=True,
    include_audio_stream=True,
):
    streams = []
    if include_video_stream:
        streams.append({
            "codec_type": "video",
            "codec_name": video_codec,
            "width": width,
            "height": height,
            "r_frame_rate": fps,
        })
    if include_audio_stream:
        streams.append({
            "codec_type": "audio",
            "codec_name": audio_codec,
        })
    payload = {
        "format": {
            "format_name": container,
            "duration": str(duration),
        },
        "streams": streams,
    }
    return json.dumps(payload)


class VideoProbeAndPayloadTest(unittest.TestCase):
    """Test video media probing, compatibility evaluation, and payload generation."""

    def test_native_mp4_h264(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.mp4"
            path.write_bytes(b"dummy")
            mock_out = _ffprobe_mock_json(container="mov,mp4,m4a,3gp,3g2,mj2", video_codec="h264")
            with patch("tdlib_media_uploader.media.video_probe._find_ffprobe", return_value="ffprobe"), \
                 patch("tdlib_media_uploader.media.video_probe.run_cancellable_process",
                       return_value=subprocess.CompletedProcess([], 0, mock_out, "")):
                info = probe_video(path)

            self.assertEqual(info.compatibility, "native")
            self.assertEqual(info.video_codec, "h264")
            self.assertTrue(info.has_video_stream)
            self.assertEqual(info.width, 1920)
            self.assertEqual(info.height, 1080)
            self.assertAlmostEqual(info.duration, 10.5)
            self.assertTrue(determine_supports_streaming(info))

    def test_native_mp4_hevc(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.mp4"
            path.write_bytes(b"dummy")
            mock_out = _ffprobe_mock_json(container="mov,mp4,m4a,3gp,3g2,mj2", video_codec="hevc")
            with patch("tdlib_media_uploader.media.video_probe._find_ffprobe", return_value="ffprobe"), \
                 patch("tdlib_media_uploader.media.video_probe.run_cancellable_process",
                       return_value=subprocess.CompletedProcess([], 0, mock_out, "")):
                info = probe_video(path)

            self.assertEqual(info.compatibility, "native")
            self.assertEqual(info.video_codec, "hevc")
            self.assertTrue(determine_supports_streaming(info))

    def test_native_mov_h264(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.mov"
            path.write_bytes(b"dummy")
            mock_out = _ffprobe_mock_json(container="mov,mp4,m4a,3gp,3g2,mj2", video_codec="h264")
            with patch("tdlib_media_uploader.media.video_probe._find_ffprobe", return_value="ffprobe"), \
                 patch("tdlib_media_uploader.media.video_probe.run_cancellable_process",
                       return_value=subprocess.CompletedProcess([], 0, mock_out, "")):
                info = probe_video(path)

            self.assertEqual(info.compatibility, "native")
            self.assertEqual(info.video_codec, "h264")
            self.assertTrue(determine_supports_streaming(info))

    def test_native_mov_hevc(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.mov"
            path.write_bytes(b"dummy")
            mock_out = _ffprobe_mock_json(container="mov,mp4,m4a,3gp,3g2,mj2", video_codec="hevc")
            with patch("tdlib_media_uploader.media.video_probe._find_ffprobe", return_value="ffprobe"), \
                 patch("tdlib_media_uploader.media.video_probe.run_cancellable_process",
                       return_value=subprocess.CompletedProcess([], 0, mock_out, "")):
                info = probe_video(path)

            self.assertEqual(info.compatibility, "native")
            self.assertEqual(info.video_codec, "hevc")
            self.assertTrue(determine_supports_streaming(info))

    def test_native_m4v_h264(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.m4v"
            path.write_bytes(b"dummy")
            mock_out = _ffprobe_mock_json(container="mov,mp4,m4a,3gp,3g2,mj2", video_codec="h264")
            with patch("tdlib_media_uploader.media.video_probe._find_ffprobe", return_value="ffprobe"), \
                 patch("tdlib_media_uploader.media.video_probe.run_cancellable_process",
                       return_value=subprocess.CompletedProcess([], 0, mock_out, "")):
                info = probe_video(path)

            self.assertEqual(info.compatibility, "native")
            self.assertEqual(info.video_codec, "h264")
            self.assertTrue(determine_supports_streaming(info))

    def test_unsupported_mov_prores(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.mov"
            path.write_bytes(b"dummy")
            mock_out = _ffprobe_mock_json(container="mov,mp4,m4a,3gp,3g2,mj2", video_codec="prores")
            with patch("tdlib_media_uploader.media.video_probe._find_ffprobe", return_value="ffprobe"), \
                 patch("tdlib_media_uploader.media.video_probe.run_cancellable_process",
                       return_value=subprocess.CompletedProcess([], 0, mock_out, "")):
                info = probe_video(path)

            self.assertEqual(info.compatibility, "unsupported")
            self.assertEqual(info.video_codec, "prores")
            reason = format_unsupported_reason(path, info)
            self.assertIn("test.mov", reason)
            self.assertIn("prores", reason)
            self.assertIn("当前编码不在直接 Telegram Video 支持范围内", reason)

    def test_invalid_mov_without_video_stream(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "audio_only.mov"
            path.write_bytes(b"dummy")
            mock_out = _ffprobe_mock_json(include_video_stream=False, include_audio_stream=True)
            with patch("tdlib_media_uploader.media.video_probe._find_ffprobe", return_value="ffprobe"), \
                 patch("tdlib_media_uploader.media.video_probe.run_cancellable_process",
                       return_value=subprocess.CompletedProcess([], 0, mock_out, "")):
                info = probe_video(path)

            self.assertEqual(info.compatibility, "invalid")
            self.assertFalse(info.has_video_stream)
            reason = format_unsupported_reason(path, info)
            self.assertIn("缺少有效视频流", reason)

    def test_invalid_damaged_video(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "damaged.mov"
            path.write_bytes(b"not a valid video")
            with patch("tdlib_media_uploader.media.video_probe._find_ffprobe", return_value="ffprobe"), \
                 patch("tdlib_media_uploader.media.video_probe.run_cancellable_process",
                       return_value=subprocess.CompletedProcess([], 1, "", "Invalid data found when processing input")):
                info = probe_video(path)

            self.assertEqual(info.compatibility, "invalid")
            reason = format_unsupported_reason(path, info)
            self.assertIn("缺少有效视频流", reason)

    def test_ffprobe_missing_safe_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            mp4_path = Path(td) / "test.mp4"
            mp4_path.write_bytes(b"dummy mp4")
            mov_path = Path(td) / "test.mov"
            mov_path.write_bytes(b"dummy mov")

            # MP4 should fall back to imageio-ffmpeg reader
            fake_meta = {"size": (1280, 720), "duration": 5.0, "fps": 25.0}

            class FakeReader:
                def __iter__(self):
                    return self

                def __next__(self):
                    return fake_meta

                def close(self):
                    pass

            with patch("tdlib_media_uploader.media.video_probe._find_ffprobe", return_value=None), \
                 patch("imageio_ffmpeg.read_frames", return_value=FakeReader()):
                mp4_info = probe_video(mp4_path)

            self.assertEqual(mp4_info.compatibility, "legacy")
            self.assertEqual(mp4_info.video_codec, "unknown")
            self.assertTrue(determine_supports_streaming(mp4_info))
            self.assertEqual(mp4_info.width, 1280)
            self.assertEqual(mp4_info.height, 720)

            # MOV without ffprobe must safely downgrade to unsupported, not crash
            with patch("tdlib_media_uploader.media.video_probe._find_ffprobe", return_value=None):
                mov_info = probe_video(mov_path)

            self.assertEqual(mov_info.compatibility, "unsupported")
            self.assertIn("未找到 ffprobe", format_unsupported_reason(mov_path, mov_info))

    def test_container_validation_mkv_renamed_to_mov_is_unsupported(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fake.mov"
            path.write_bytes(b"dummy")
            # FFprobe reports matroska,webm container even though file extension is .mov
            mock_out = _ffprobe_mock_json(
                container="matroska,webm",
                video_codec="h264",
                audio_codec="aac",
            )
            with patch("tdlib_media_uploader.media.video_probe._find_ffprobe", return_value="ffprobe"), \
                 patch("tdlib_media_uploader.media.video_probe.run_cancellable_process",
                       return_value=subprocess.CompletedProcess([], 0, mock_out, "")):
                info = probe_video(path)

            self.assertEqual(info.compatibility, "unsupported")
            self.assertEqual(info.container, "matroska")
            self.assertFalse(determine_supports_streaming(info))
            reason = format_unsupported_reason(path, info)
            self.assertIn("fake.mov", reason)

    def test_probe_video_cancellation_raises_timeout_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.mp4"
            path.write_bytes(b"dummy")
            with patch("tdlib_media_uploader.media.video_probe._find_ffprobe", return_value="ffprobe"), \
                 patch("tdlib_media_uploader.media.video_probe.run_cancellable_process",
                       side_effect=TimeoutError("外部进程已取消")):
                with self.assertRaises(TimeoutError):
                    probe_video(path)

    def test_video_preflight_cancellation_preserves_timeout_error(self):
        with tempfile.TemporaryDirectory() as td:
            video_file = Path(td) / "test.mp4"
            video_file.write_bytes(b"dummy")
            cancel_event = threading.Event()
            cancel_event.set()

            items = [{"path": video_file}]
            with patch.object(video_core, "wait_for_file_ready",
                              return_value=SimpleNamespace(snapshot=SimpleNamespace(size=10, mtime_ns=1, as_tuple=lambda: (10, 1)))), \
                 patch.object(video_core, "raise_for_file_readiness"):
                with self.assertRaises(TimeoutError):
                    video_core.preflight_videos(items, cancel_event=cancel_event)

    def test_legacy_mp4_accepted_in_video_preflight_and_input_video(self):
        with tempfile.TemporaryDirectory() as td:
            video_file = Path(td) / "clip.mp4"
            video_file.write_bytes(b"video data")
            thumb_file = Path(td) / "thumb.jpg"
            thumb_file.write_bytes(b"thumb data")

            legacy_info = VideoMediaInfo(
                container="mp4",
                video_codec="unknown",
                audio_codec=None,
                width=1280,
                height=720,
                duration=10.0,
                fps=30.0,
                has_video_stream=True,
                compatibility="legacy",
            )
            # 1. preflight accepts legacy MP4 without marking as skipped
            with patch.object(video_core, "prepare_video", return_value=legacy_info), \
                 patch.object(video_core, "wait_for_file_ready", return_value=SimpleNamespace(snapshot=SimpleNamespace(size=10, mtime_ns=1, as_tuple=lambda: (10, 1)))), \
                 patch.object(video_core, "raise_for_file_readiness"):
                skipped = video_core.preflight_videos([{"path": video_file}])
            self.assertEqual(skipped, [])

            # 2. input_video creates inputMessageVideo with supports_streaming=True
            item = {"path": video_file}
            with patch.object(video_core, "video_info", return_value=legacy_info), \
                 patch.object(video_core, "wait_for_file_ready", return_value=SimpleNamespace(snapshot=SimpleNamespace(size=10, mtime_ns=1, as_tuple=lambda: (10, 1)))), \
                 patch.object(video_core, "raise_for_file_readiness"), \
                 patch.object(video_core, "should_stage", return_value=False), \
                 patch.object(video_core, "build_thumbnail", return_value=(thumb_file, 320, 180)):
                payload = video_core.input_video(item, "Caption")

            self.assertEqual(payload["@type"], "inputMessageVideo")
            self.assertTrue(payload["supports_streaming"])

    def test_input_video_payload_structure(self):
        with tempfile.TemporaryDirectory() as td:
            video_file = Path(td) / "clip.mov"
            video_file.write_bytes(b"video data")
            thumb_file = Path(td) / "thumb.jpg"
            thumb_file.write_bytes(b"thumb data")

            info = VideoMediaInfo(
                container="mov",
                video_codec="h264",
                audio_codec="aac",
                width=1920,
                height=1080,
                duration=15.0,
                fps=30.0,
                has_video_stream=True,
                compatibility="native",
            )
            item = {"path": video_file}
            with patch.object(video_core, "video_info", return_value=info), \
                 patch.object(video_core, "wait_for_file_ready", return_value=SimpleNamespace(snapshot=SimpleNamespace(size=10, mtime_ns=1, as_tuple=lambda: (10, 1)))), \
                 patch.object(video_core, "raise_for_file_readiness"), \
                 patch.object(video_core, "should_stage", return_value=False), \
                 patch.object(video_core, "build_thumbnail", return_value=(thumb_file, 320, 180)):
                payload = video_core.input_video(item, "Sample caption", generate_thumbnail=True)

            self.assertEqual(payload["@type"], "inputMessageVideo")
            self.assertEqual(payload["video"]["@type"], "inputFileLocal")
            self.assertEqual(Path(payload["video"]["path"]).resolve(), video_file.resolve())
            self.assertEqual(payload["width"], 1920)
            self.assertEqual(payload["height"], 1080)
            self.assertEqual(payload["duration"], 15)
            self.assertTrue(payload["supports_streaming"])
            # Ensure it is NEVER inputMessageDocument
            self.assertNotEqual(payload["@type"], "inputMessageDocument")

    def test_preflight_videos_skips_unsupported_without_crash(self):
        with tempfile.TemporaryDirectory() as td:
            valid_mp4 = Path(td) / "valid.mp4"
            valid_mp4.write_bytes(b"mp4")
            prores_mov = Path(td) / "prores.mov"
            prores_mov.write_bytes(b"mov")

            native_info = VideoMediaInfo(
                container="mp4", video_codec="h264", audio_codec="aac",
                width=1920, height=1080, duration=10.0, fps=30.0,
                has_video_stream=True, compatibility="native",
            )
            unsupported_info = VideoMediaInfo(
                container="mov", video_codec="prores", audio_codec="pcm",
                width=1920, height=1080, duration=10.0, fps=30.0,
                has_video_stream=True, compatibility="unsupported",
            )

            def mock_prepare(p, **_kwargs):
                if p.name == "valid.mp4":
                    return native_info
                return unsupported_info

            with patch.object(video_core, "prepare_video", side_effect=mock_prepare), \
                 patch.object(video_core, "wait_for_file_ready", return_value=SimpleNamespace(snapshot=SimpleNamespace(size=10, mtime_ns=1, as_tuple=lambda: (10, 1)))), \
                 patch.object(video_core, "raise_for_file_readiness"):
                items = [{"path": valid_mp4}, {"path": prores_mov}]
                skipped = video_core.preflight_videos(items)

            self.assertEqual(len(skipped), 1)
            self.assertEqual(skipped[0]["path"], prores_mov)
            self.assertIn("prores", skipped[0]["reason"])

    def test_video_info_cache_behavior(self):
        with tempfile.TemporaryDirectory() as td:
            video_path = Path(td) / "cached_clip.mp4"
            video_path.write_bytes(b"initial data")

            native_info = VideoMediaInfo(
                container="mp4", video_codec="h264", audio_codec="aac",
                width=1280, height=720, duration=10.0, fps=30.0,
                has_video_stream=True, compatibility="native",
            )

            probe_count = 0

            def mock_probe(_path, **_kwargs):
                nonlocal probe_count
                probe_count += 1
                return native_info

            with patch("tdlib_media_uploader.media.legacy_video.probe_video", side_effect=mock_probe):
                info1 = video_core.video_info(video_path)
                info2 = video_core.video_info(video_path)
                self.assertEqual(probe_count, 1)
                self.assertEqual(info1.width, 1280)
                self.assertEqual(info2.width, 1280)

                # Mutate file size/mtime -> must trigger re-probe
                video_path.write_bytes(b"modified video content with different size")
                info3 = video_core.video_info(video_path)
                self.assertEqual(probe_count, 2)


class ImageProbeAndPrepareTest(unittest.TestCase):
    """Test image media probe, normalization, and preparation for Telegram Photo."""

    def test_standard_jpeg_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            img_path = Path(td) / "standard.jpg"
            img = Image.new("RGB", (1920, 1080), color="blue")
            img.save(img_path, format="JPEG", quality=90)
            orig_bytes = img_path.read_bytes()

            prepared = prepare_image_for_telegram(img_path)
            self.assertFalse(prepared.transformed)
            self.assertEqual(prepared.upload_path.resolve(), img_path.resolve())
            self.assertEqual(prepared.output_format, "JPEG")
            self.assertEqual(prepared.output_width, 1920)
            self.assertEqual(prepared.output_height, 1080)
            # Source file untouched
            self.assertEqual(img_path.read_bytes(), orig_bytes)

    def test_standard_png_within_limits_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            img_path = Path(td) / "screenshot.png"
            img = Image.new("RGB", (1280, 720), color="green")
            img.save(img_path, format="PNG")
            orig_bytes = img_path.read_bytes()

            prepared = prepare_image_for_telegram(img_path)
            self.assertFalse(prepared.transformed)
            self.assertEqual(prepared.upload_path.resolve(), img_path.resolve())
            self.assertEqual(prepared.output_format, "PNG")
            self.assertEqual(img_path.read_bytes(), orig_bytes)

    def test_oversize_dimension_landscape_downscaling(self):
        with tempfile.TemporaryDirectory() as td:
            img_path = Path(td) / "huge_landscape.jpg"
            # 6000 x 4000
            img = Image.new("RGB", (6000, 4000), color="red")
            img.save(img_path, format="JPEG", quality=85)
            orig_bytes = img_path.read_bytes()

            prepared = prepare_image_for_telegram(img_path)
            self.assertTrue(prepared.transformed)
            self.assertNotEqual(prepared.upload_path, img_path)
            self.assertTrue(prepared.upload_path.is_file())
            # Max side 2560 -> 2560 x 1707
            self.assertEqual(prepared.output_width, 2560)
            self.assertEqual(prepared.output_height, 1707)
            self.assertLessEqual(prepared.output_size, TELEGRAM_PHOTO_MAX_BYTES)
            # Source untouched
            self.assertEqual(img_path.read_bytes(), orig_bytes)

    def test_oversize_dimension_portrait_downscaling(self):
        with tempfile.TemporaryDirectory() as td:
            img_path = Path(td) / "huge_portrait.jpg"
            # 4000 x 6000
            img = Image.new("RGB", (4000, 6000), color="yellow")
            img.save(img_path, format="JPEG", quality=85)
            orig_bytes = img_path.read_bytes()

            prepared = prepare_image_for_telegram(img_path)
            self.assertTrue(prepared.transformed)
            self.assertEqual(prepared.output_width, 1707)
            self.assertEqual(prepared.output_height, 2560)
            self.assertEqual(img_path.read_bytes(), orig_bytes)

    def test_non_native_bmp_transcoded_to_jpeg(self):
        with tempfile.TemporaryDirectory() as td:
            img_path = Path(td) / "sample.bmp"
            img = Image.new("RGB", (800, 600), color="cyan")
            img.save(img_path, format="BMP")
            orig_bytes = img_path.read_bytes()

            prepared = prepare_image_for_telegram(img_path)
            self.assertTrue(prepared.transformed)
            self.assertEqual(prepared.output_format, "JPEG")
            self.assertEqual(prepared.upload_path.suffix.lower(), ".jpg")
            self.assertEqual(img_path.read_bytes(), orig_bytes)

    def test_non_native_tiff_transcoded_to_jpeg(self):
        with tempfile.TemporaryDirectory() as td:
            img_path = Path(td) / "sample.tiff"
            img = Image.new("RGB", (800, 600), color="magenta")
            img.save(img_path, format="TIFF")
            orig_bytes = img_path.read_bytes()

            prepared = prepare_image_for_telegram(img_path)
            self.assertTrue(prepared.transformed)
            self.assertEqual(prepared.output_format, "JPEG")
            self.assertEqual(img_path.read_bytes(), orig_bytes)

    def test_static_webp_transcoded_to_jpeg(self):
        with tempfile.TemporaryDirectory() as td:
            img_path = Path(td) / "sample.webp"
            img = Image.new("RGB", (800, 600), color="white")
            img.save(img_path, format="WEBP")
            orig_bytes = img_path.read_bytes()

            prepared = prepare_image_for_telegram(img_path)
            self.assertTrue(prepared.transformed)
            self.assertEqual(prepared.output_format, "JPEG")
            self.assertEqual(img_path.read_bytes(), orig_bytes)

    def test_transparent_png_composited_onto_background(self):
        with tempfile.TemporaryDirectory() as td:
            img_path = Path(td) / "transparent.png"
            # 3000 x 2000 RGBA (will trigger resize to 2560 + JPEG encode)
            img = Image.new("RGBA", (3000, 2000), color=(255, 0, 0, 128))
            img.save(img_path, format="PNG")
            orig_bytes = img_path.read_bytes()

            prepared = prepare_image_for_telegram(
                img_path,
                transparency_background="#FFFFFF",
            )
            self.assertTrue(prepared.transformed)
            self.assertEqual(prepared.output_format, "JPEG")
            # Verify output image can be opened as RGB and has expected dimensions
            with Image.open(prepared.upload_path) as out_im:
                self.assertEqual(out_im.mode, "RGB")
                self.assertEqual(out_im.size, (2560, 1707))
            self.assertEqual(img_path.read_bytes(), orig_bytes)

    def test_extreme_aspect_ratio_padding_policy(self):
        with tempfile.TemporaryDirectory() as td:
            # Aspect ratio 30: 100 x 3000
            img_path = Path(td) / "very_tall.jpg"
            img = Image.new("RGB", (100, 3000), color="purple")
            img.save(img_path, format="JPEG")
            orig_bytes = img_path.read_bytes()

            prepared = prepare_image_for_telegram(
                img_path,
                extreme_aspect_policy="pad",
            )
            self.assertTrue(prepared.transformed)
            # Long side is 2560 (since 3000 > 2560).
            # Short side after downscaling was 100 * 2560 / 3000 = 85.
            # Ratio 2560 / 85 = 30 > 20.
            # Padded short side = ceil(2560 / 20) = 128.
            self.assertEqual(prepared.output_height, 2560)
            self.assertEqual(prepared.output_width, 128)
            ratio = max(prepared.output_width / prepared.output_height,
                        prepared.output_height / prepared.output_width)
            self.assertLessEqual(ratio, 20.0)
            self.assertEqual(img_path.read_bytes(), orig_bytes)

    def test_extreme_aspect_ratio_skip_policy(self):
        with tempfile.TemporaryDirectory() as td:
            img_path = Path(td) / "very_wide.jpg"
            img = Image.new("RGB", (3000, 100), color="orange")
            img.save(img_path, format="JPEG")

            with self.assertRaises(RuntimeError) as ctx:
                prepare_image_for_telegram(
                    img_path,
                    extreme_aspect_policy="skip",
                )
            self.assertIn("长宽比", str(ctx.exception))

    def test_animated_webp_skipped_in_probe_and_preflight(self):
        with tempfile.TemporaryDirectory() as td:
            img_path = Path(td) / "anim.webp"
            frame1 = Image.new("RGB", (100, 100), "red")
            frame2 = Image.new("RGB", (100, 100), "blue")
            frame1.save(img_path, format="WEBP", save_all=True, append_images=[frame2], duration=100)

            info = probe_image(img_path)
            self.assertTrue(info.animated)
            self.assertGreater(info.frame_count, 1)
            self.assertFalse(info.telegram_compatible)

            # Preflight must skip animated image with informative reason
            skipped = image_core.preflight_images([img_path])
            self.assertEqual(len(skipped), 1)
            self.assertIn("动画图片", skipped[0]["reason"])

    def test_damaged_image_skipped_in_preflight(self):
        with tempfile.TemporaryDirectory() as td:
            bad_path = Path(td) / "corrupt.jpg"
            bad_path.write_bytes(b"not an image data")

            skipped = image_core.preflight_images([bad_path])
            self.assertEqual(len(skipped), 1)
            self.assertIn("无法读取图片", skipped[0]["reason"])

    def test_cache_reuse_and_invalidation(self):
        with tempfile.TemporaryDirectory() as td:
            img_path = Path(td) / "resizable.bmp"
            img = Image.new("RGB", (800, 600), "gray")
            img.save(img_path, format="BMP")

            p1 = prepare_image_for_telegram(img_path)
            upload_path1 = p1.upload_path
            self.assertTrue(upload_path1.is_file())

            # Second call with same parameters should return the cached file
            p2 = prepare_image_for_telegram(img_path)
            self.assertEqual(p1.upload_path, p2.upload_path)

            # Modifying the source file changes mtime/size, causing cache invalidation
            img2 = Image.new("RGB", (900, 700), "blue")
            img2.save(img_path, format="BMP")
            p3 = prepare_image_for_telegram(img_path)
            self.assertNotEqual(p1.upload_path, p3.upload_path)

            # Changing policy parameters (e.g. max_side) invalidates cache
            p4 = prepare_image_for_telegram(img_path, max_side=1280)
            self.assertNotEqual(p3.upload_path, p4.upload_path)

    def test_oversize_image_accepted_and_normalized_even_if_compress_oversize_false(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            oversize_bmp = root / "large.bmp"
            # Create a 2000x2000 BMP (~12 MB)
            img = Image.new("RGB", (2000, 2000), color="blue")
            img.save(oversize_bmp, format="BMP")
            source_size = oversize_bmp.stat().st_size
            self.assertGreater(source_size, 10 * 1024**2)

            with patch.object(image_core.cfg, "IMAGE_DIR", root), \
                 patch.object(image_core.cfg, "IMAGE_EXTENSIONS", {".bmp"}), \
                 patch.object(image_core.cfg, "IMAGE_COMPRESS_OVERSIZE", False):
                # 1. scan_images() accepts the oversize image
                scanned = image_core.scan_images()
                self.assertIn(oversize_bmp, scanned)

                # 2. preflight_images() accepts it (does not raise / does not skip)
                skipped = image_core.preflight_images([oversize_bmp])
                self.assertEqual(skipped, [])

                # 3. input_photo() produces inputMessagePhoto pointing to cached JPEG
                payload = image_core.input_photo(oversize_bmp, "Large BMP")
                self.assertEqual(payload["@type"], "inputMessagePhoto")
                upload_path = Path(payload["photo"]["path"])
                self.assertTrue(upload_path.is_file())
                self.assertNotEqual(upload_path.resolve(), oversize_bmp.resolve())
                self.assertLessEqual(upload_path.stat().st_size, TELEGRAM_PHOTO_MAX_BYTES)
                # 4. Source file is NEVER modified
                self.assertEqual(oversize_bmp.stat().st_size, source_size)

    def test_encode_jpeg_under_limit_raises_when_unachievable(self):
        img = Image.new("RGB", (1000, 1000), color="green")
        # Attempting an impossibly small limit (e.g. 50 bytes) must cleanly raise RuntimeError
        with self.assertRaises(RuntimeError) as ctx:
            from tdlib_media_uploader.media.image_prepare import encode_jpeg_under_limit
            encode_jpeg_under_limit(img, target_bytes=50)
        self.assertIn("无法将图片规范化至目标大小", str(ctx.exception))


class MixedMediaIntegrationTest(unittest.TestCase):
    """Test mixed upload processing containing video and image formats."""

    def test_mixed_preflight_and_payloads(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            group_dir = root / "GroupA"
            group_dir.mkdir()

            jpg_file = group_dir / "1_photo.jpg"
            Image.new("RGB", (800, 600), "red").save(jpg_file, format="JPEG")

            png_file = group_dir / "2_photo.png"
            Image.new("RGB", (640, 480), "blue").save(png_file, format="PNG")

            webp_file = group_dir / "3_photo.webp"
            Image.new("RGB", (400, 300), "green").save(webp_file, format="WEBP")

            mp4_file = group_dir / "4_clip.mp4"
            mp4_file.write_bytes(b"mp4 data")

            mov_file = group_dir / "5_clip.mov"
            mov_file.write_bytes(b"mov data")

            m4v_file = group_dir / "6_clip.m4v"
            m4v_file.write_bytes(b"m4v data")

            native_video_info = VideoMediaInfo(
                container="mov,mp4", video_codec="h264", audio_codec="aac",
                width=1280, height=720, duration=10.0, fps=30.0,
                has_video_stream=True, compatibility="native",
            )

            items = [
                {"path": jpg_file, "media_kind": "image"},
                {"path": png_file, "media_kind": "image"},
                {"path": webp_file, "media_kind": "image"},
                {"path": mp4_file, "media_kind": "video"},
                {"path": mov_file, "media_kind": "video"},
                {"path": m4v_file, "media_kind": "video"},
            ]

            readiness_mock = SimpleNamespace(
                snapshot=SimpleNamespace(size=10, mtime_ns=1, as_tuple=lambda: (10, 1))
            )
            with patch.object(video_core, "video_info", return_value=native_video_info), \
                 patch.object(mixed_core, "wait_for_file_ready", return_value=readiness_mock), \
                 patch.object(mixed_core, "raise_for_file_readiness"):
                skipped = mixed_core.preflight_mixed(items)

            self.assertEqual(len(skipped), 0, f"Unexpected skipped items: {skipped}")

            # Verify input_photo on webp uses cache path, not source path
            prepared_webp = prepare_image_for_telegram(webp_file)
            with patch.object(image_core, "prepare_image_for_telegram", return_value=prepared_webp), \
                 patch.object(image_core, "wait_for_file_ready", return_value=readiness_mock), \
                 patch.object(image_core, "raise_for_file_readiness"), \
                 patch.object(image_core, "should_stage", return_value=False):
                photo_payload = image_core.input_photo(webp_file, "caption")

            self.assertEqual(photo_payload["@type"], "inputMessagePhoto")
            self.assertEqual(Path(photo_payload["photo"]["path"]).resolve(), prepared_webp.upload_path.resolve())
            self.assertNotEqual(Path(photo_payload["photo"]["path"]).resolve(), webp_file.resolve())

            # Verify input_video on mov uses inputMessageVideo and supports_streaming
            with patch.object(video_core, "video_info", return_value=native_video_info), \
                 patch.object(video_core, "wait_for_file_ready", return_value=readiness_mock), \
                 patch.object(video_core, "raise_for_file_readiness"), \
                 patch.object(video_core, "should_stage", return_value=False), \
                 patch.object(video_core, "build_thumbnail", return_value=(Path(td) / "thumb.jpg", 160, 90)):
                video_payload = video_core.input_video({"path": mov_file}, "caption")

            self.assertEqual(video_payload["@type"], "inputMessageVideo")
            self.assertTrue(video_payload["supports_streaming"])

    def test_mixed_preflight_cancellation_preserves_timeout_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            group_dir = root / "GroupA"
            group_dir.mkdir()
            img_file = group_dir / "pic.jpg"
            Image.new("RGB", (10, 10)).save(img_file, format="JPEG")

            cancel_event = threading.Event()
            cancel_event.set()
            items = [{"path": img_file, "media_kind": "image"}]
            with patch.object(mixed_core, "wait_for_file_ready",
                              return_value=SimpleNamespace(snapshot=SimpleNamespace(size=10, mtime_ns=1, as_tuple=lambda: (10, 1)))), \
                 patch.object(mixed_core, "raise_for_file_readiness"):
                with self.assertRaises(TimeoutError):
                    mixed_core.preflight_mixed(items, cancel_event=cancel_event)

    def test_legacy_mp4_accepted_in_mixed_preflight(self):
        with tempfile.TemporaryDirectory() as td:
            mp4_file = Path(td) / "clip.mp4"
            mp4_file.write_bytes(b"data")
            legacy_info = VideoMediaInfo(
                container="mp4",
                video_codec="unknown",
                audio_codec=None,
                width=1280,
                height=720,
                duration=10.0,
                fps=30.0,
                has_video_stream=True,
                compatibility="legacy",
            )
            items = [{"path": mp4_file, "media_kind": "video"}]
            with patch.object(video_core, "video_info", return_value=legacy_info), \
                 patch.object(mixed_core, "wait_for_file_ready",
                              return_value=SimpleNamespace(snapshot=SimpleNamespace(size=10, mtime_ns=1, as_tuple=lambda: (10, 1)))), \
                 patch.object(mixed_core, "raise_for_file_readiness"):
                skipped = mixed_core.preflight_mixed(items)
            self.assertEqual(skipped, [])


if __name__ == "__main__":
    unittest.main()
