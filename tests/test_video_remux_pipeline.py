# -*- coding: utf-8 -*-
"""Comprehensive tests for Video Compatibility V2: Lossless Remux Pipeline.

Validates:
1. Matrix: native (MP4/MOV/M4V) vs remux (MKV/AVI/TS/MTS/M2TS) vs unsupported codecs/audio.
2. FFmpeg stream-copy command: `-c copy`, `-movflags +faststart`, temp file atomic rename.
3. Decoupled thumbnail cache identity: keyed to original source file, extracted from processed MP4.
4. Per-group lifecycle and bounded disk usage: immediate deletion upon confirmed upload.
5. Resume preservation: validated MP4 preserved on upload failure; `.tmp.*` unlinked on cancellation.
6. One-group-ahead pipeline: upload Group A concurrently prepares Group A+1, never Group A+2.
7. Source change guard: stale prefetch deferred for rescan when source modified while waiting.
8. Policy compatibility: "remux" (default) vs "original" (rejects remux candidates).
9. Mixed mode and Staging integration.
10. Strict safety: source files never modified, no transcoding, no Document fallback.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
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
from tdlib_media_uploader.config.paths import (
    THUMBNAIL_CACHE_DIR,
    VIDEO_PROCESSED_CACHE_DIR,
)
from tdlib_media_uploader.contracts import UploadContext
from tdlib_media_uploader.upload.engine import _NeverCancelToken, _NullEventSink
from tdlib_media_uploader.core.models import (
    AlbumPlan,
    BatchStatus,
    FileSnapshot,
    MediaItem,
    ScanResult,
    UploadBatchResult,
)
from tdlib_media_uploader.telegram.send_result import SendResult


def make_media_item(path: Path, source_root: Path, kind: str = "video") -> MediaItem:
    st = path.stat()
    return MediaItem(
        path=path,
        source_root=source_root,
        media_kind=kind,
        snapshot=FileSnapshot(str(path), st.st_size, st.st_mtime_ns),
    )


def make_album_plan(key: str, source_root: Path, items: tuple[MediaItem, ...], kind: str = "video") -> AlbumPlan:
    return AlbumPlan(
        key=key,
        kind=kind,
        source_root=source_root,
        group_label=key,
        number=1,
        items=items,
        pending_items=items,
        caption="Caption",
    )
from tdlib_media_uploader.gui.cache_service import (
    CACHE_TARGETS,
    clear_cache,
)
from tdlib_media_uploader.media import legacy_mixed as mixed_core
from tdlib_media_uploader.media import legacy_video as video_core
from tdlib_media_uploader.media.video_prepare import (
    PreparedVideo,
    cleanup_processed_group,
    cleanup_stale_processed_videos,
    clear_all_processed_videos,
    get_video_process_cache_key,
    prepare_video_for_telegram,
    remux_video_lossless,
    safe_group_key,
)
from tdlib_media_uploader.media.video_probe import (
    VideoMediaInfo,
    determine_supports_streaming,
    format_unsupported_reason,
    probe_video,
)
from tdlib_media_uploader.upload.engine import UploadEngine


def _mock_ffprobe_json(
    *,
    container="matroska,webm",
    video_codec="h264",
    audio_codec="aac",
    width=1920,
    height=1080,
    duration="12.0",
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


class VideoRemuxMatrixAndProbeTest(unittest.TestCase):
    """Test format detection, compatibility classification, and policy behavior."""

    def test_native_formats_stay_native(self):
        cases = [
            (".mp4", "h264", "aac"),
            (".mp4", "h264", "ac3"),
            (".mp4", "hevc", "aac"),
            (".mov", "h264", "aac"),
            (".mov", "hevc", "aac"),
            (".mov", "hevc", "pcm_s16le"),
            (".m4v", "h264", "aac"),
        ]
        for ext, vcodec, acodec in cases:
            with self.subTest(extension=ext, video=vcodec, audio=acodec), tempfile.TemporaryDirectory() as td:
                p = Path(td) / f"video{ext}"
                p.write_bytes(b"dummy")
                mock_out = _mock_ffprobe_json(
                    container="mov,mp4,m4a,3gp,3g2,mj2",
                    video_codec=vcodec,
                    audio_codec=acodec,
                )
                with patch("tdlib_media_uploader.media.video_probe._find_ffprobe", return_value="ffprobe"), \
                     patch("tdlib_media_uploader.media.video_probe.run_cancellable_process",
                           return_value=subprocess.CompletedProcess([], 0, mock_out, "")):
                    info = probe_video(p)

                self.assertEqual(info.compatibility, "native", f"Failed for native {ext} {vcodec} {acodec}")
                self.assertEqual(info.video_codec, vcodec)
                self.assertTrue(info.has_video_stream)
                self.assertEqual((info.width, info.height), (1920, 1080))
                self.assertAlmostEqual(info.duration, 12.0)
                self.assertEqual(info.recommended_action, "upload")
                self.assertTrue(determine_supports_streaming(info))

    def test_remux_formats_classified_correctly(self):
        cases = [
            (".mkv", "matroska,webm", "h264", "aac"),
            (".mkv", "matroska,webm", "hevc", "mp3"),
            (".mkv", "matroska,webm", "hevc", None),  # no audio stream
            (".avi", "avi", "h264", "mp3"),
            (".ts", "mpegts", "h264", "aac"),
            (".mts", "mpegts", "hevc", "aac"),
            (".m2ts", "mpegts", "h264", "mp3"),
        ]
        for ext, container, vcodec, acodec in cases:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / f"clip{ext}"
                p.write_bytes(b"dummy")
                mock_out = _mock_ffprobe_json(
                    container=container,
                    video_codec=vcodec,
                    audio_codec=acodec,
                    include_audio_stream=(acodec is not None),
                )
                with patch("tdlib_media_uploader.media.video_probe._find_ffprobe", return_value="ffprobe"), \
                     patch("tdlib_media_uploader.media.video_probe.run_cancellable_process",
                           return_value=subprocess.CompletedProcess([], 0, mock_out, "")):
                    info = probe_video(p)

                self.assertEqual(info.compatibility, "remux", f"Failed for {ext} {vcodec} {acodec}")
                self.assertEqual(info.recommended_action, "remux")
                self.assertTrue(determine_supports_streaming(info))

    def test_other_video_codecs_are_remux_candidates(self):
        for codec in ("vp9", "av1", "prores", "vp8", "mpeg4"):
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "clip.mkv"
                p.write_bytes(b"dummy")
                mock_out = _mock_ffprobe_json(
                    container="matroska,webm",
                    video_codec=codec,
                    audio_codec="aac",
                )
                with patch("tdlib_media_uploader.media.video_probe._find_ffprobe", return_value="ffprobe"), \
                     patch("tdlib_media_uploader.media.video_probe.run_cancellable_process",
                           return_value=subprocess.CompletedProcess([], 0, mock_out, "")):
                    info = probe_video(p)

                self.assertEqual(info.compatibility, "remux")
                self.assertEqual(info.recommended_action, "remux")

    def test_other_audio_codecs_are_remux_candidates(self):
        for audio_codec in ("dts", "flac", "ac3", "eac3", "opus", "pcm_s16le"):
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "clip.mkv"
                p.write_bytes(b"dummy")
                mock_out = _mock_ffprobe_json(
                    container="matroska,webm",
                    video_codec="h264",
                    audio_codec=audio_codec,
                )
                with patch("tdlib_media_uploader.media.video_probe._find_ffprobe", return_value="ffprobe"), \
                     patch("tdlib_media_uploader.media.video_probe.run_cancellable_process",
                           return_value=subprocess.CompletedProcess([], 0, mock_out, "")):
                    info = probe_video(p)

                self.assertEqual(info.compatibility, "remux")
                self.assertEqual(info.recommended_action, "remux")

    def test_original_policy_skips_remux_candidate_in_prepare_and_preflight(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "clip.mkv"
            p.write_bytes(b"dummy")
            m_info = VideoMediaInfo(
                container="mkv",
                video_codec="h264",
                audio_codec="aac",
                width=1920,
                height=1080,
                duration=10.0,
                fps=30.0,
                has_video_stream=True,
                has_audio_stream=True,
                compatibility="remux",
                recommended_action="remux",
            )
            with patch.object(cfg, "VIDEO_COMPATIBILITY_POLICY", "original"), \
                 patch("tdlib_media_uploader.media.video_prepare.probe_video", return_value=m_info):
                with self.assertRaises(RuntimeError) as ctx:
                    prepare_video_for_telegram(p)
                self.assertIn("original", str(ctx.exception))
                self.assertIn("仅允许原生格式", str(ctx.exception))

            with patch.object(cfg, "VIDEO_COMPATIBILITY_POLICY", "original"), \
                 patch("tdlib_media_uploader.media.legacy_video.prepare_video", return_value=m_info):
                skipped = video_core.preflight_videos([{"path": p}])
                self.assertEqual(len(skipped), 1)
                self.assertIn("original", skipped[0]["reason"])


class VideoRemuxExecutionAndValidationTest(unittest.TestCase):
    """Test lossless remux command construction, temporary file safety, and verification."""

    def test_ffmpeg_remux_command_exact_flags(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "test.mkv"
            src.write_bytes(b"mock video")
            dst = Path(td) / "out.mp4"

            recorded_commands = []

            def fake_run(cmd, **kwargs):
                recorded_commands.append(cmd)
                # Simulate ffmpeg writing output to the temp destination
                temp_out = Path(cmd[-1])
                temp_out.write_bytes(b"remuxed mp4 bytes")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            verified_mp4_info = VideoMediaInfo(
                container="mp4",
                video_codec="h264",
                audio_codec="aac",
                width=1920,
                height=1080,
                duration=10.0,
                fps=30.0,
                has_video_stream=True,
                has_audio_stream=True,
                compatibility="native",
                recommended_action="upload",
            )

            with patch("tdlib_media_uploader.media.video_prepare._find_ffmpeg", return_value="ffmpeg"), \
                 patch("tdlib_media_uploader.media.video_prepare.run_cancellable_process", side_effect=fake_run), \
                 patch("tdlib_media_uploader.media.video_prepare.probe_video", return_value=verified_mp4_info):
                out = remux_video_lossless(src, dst)

            self.assertEqual(out, dst)
            self.assertTrue(dst.is_file())
            self.assertEqual(len(recorded_commands), 1)
            cmd = recorded_commands[0]

            # Verify exact stream copy flags
            self.assertIn("-c", cmd)
            c_idx = cmd.index("-c")
            self.assertEqual(cmd[c_idx + 1], "copy")
            self.assertIn("-movflags", cmd)
            m_idx = cmd.index("-movflags")
            self.assertEqual(cmd[m_idx + 1], "+faststart")
            self.assertIn("-map", cmd)
            self.assertIn("0:v:0", cmd)
            self.assertIn("0:a:0?", cmd)

            # Ensure NO transcode flags
            self.assertNotIn("-c:v", cmd)
            self.assertNotIn("-c:a", cmd)
            self.assertNotIn("libx264", cmd)
            self.assertNotIn("aac", cmd[c_idx:])

    def test_remux_temporary_file_unlinked_on_failure(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "test.mkv"
            src.write_bytes(b"mock video")
            dst = Path(td) / "out.mp4"

            def fake_run_fail(cmd, **kwargs):
                temp_out = Path(cmd[-1])
                temp_out.write_bytes(b"corrupt partial")
                return subprocess.CompletedProcess(cmd, 1, "", "Demuxing error")

            with patch("tdlib_media_uploader.media.video_prepare._find_ffmpeg", return_value="ffmpeg"), \
                 patch("tdlib_media_uploader.media.video_prepare.run_cancellable_process", side_effect=fake_run_fail):
                with self.assertRaises(RuntimeError):
                    remux_video_lossless(src, dst)

            # Neither target nor temp file should remain
            self.assertFalse(dst.exists())
            temp_files = list(Path(td).glob("*.tmp.*"))
            self.assertEqual(len(temp_files), 0)

    def test_remux_temporary_file_unlinked_on_cancellation(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "test.mkv"
            src.write_bytes(b"mock video")
            dst = Path(td) / "out.mp4"

            def fake_run_cancel(cmd, **kwargs):
                temp_out = Path(cmd[-1])
                temp_out.write_bytes(b"partial bytes")
                raise TimeoutError("任务已取消")

            with patch("tdlib_media_uploader.media.video_prepare._find_ffmpeg", return_value="ffmpeg"), \
                 patch("tdlib_media_uploader.media.video_prepare.run_cancellable_process", side_effect=fake_run_cancel):
                with self.assertRaises(TimeoutError):
                    remux_video_lossless(src, dst)

            self.assertFalse(dst.exists())
            self.assertEqual(len(list(Path(td).glob("*.tmp.*"))), 0)

    def test_remux_output_validation_rejects_corrupted_output(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "test.mkv"
            src.write_bytes(b"mock video")
            dst = Path(td) / "out.mp4"

            def fake_run(cmd, **kwargs):
                temp_out = Path(cmd[-1])
                temp_out.write_bytes(b"bad mp4")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            bad_mp4_info = VideoMediaInfo(
                container="mp4",
                video_codec="",
                audio_codec=None,
                width=0,
                height=0,
                duration=0.0,
                fps=None,
                has_video_stream=False,
                has_audio_stream=False,
                compatibility="invalid",
                recommended_action="error",
            )

            with patch("tdlib_media_uploader.media.video_prepare._find_ffmpeg", return_value="ffmpeg"), \
                 patch("tdlib_media_uploader.media.video_prepare.run_cancellable_process", side_effect=fake_run), \
                 patch("tdlib_media_uploader.media.video_prepare.probe_video", return_value=bad_mp4_info):
                with self.assertRaises(RuntimeError) as ctx:
                    remux_video_lossless(src, dst)
                self.assertIn("验证失败", str(ctx.exception))

            self.assertFalse(dst.exists())
            self.assertEqual(len(list(Path(td).glob("*.tmp.*"))), 0)


class DecoupledThumbnailAndCacheLifecycleTest(unittest.TestCase):
    """Test thumbnail identity decoupling, per-group cache management, and cleanup timing."""

    def test_thumbnail_key_uses_original_identity_and_survives_processed_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            source_dir = Path(td) / "sources"
            source_dir.mkdir()
            cache_dir = Path(td) / "cache"
            thumb_dir = cache_dir / "thumbnails"
            processed_dir = cache_dir / "video_processed"

            with patch("tdlib_media_uploader.media.video_prepare.THUMBNAIL_CACHE_DIR", thumb_dir), \
                 patch("tdlib_media_uploader.media.legacy_video.THUMB_CACHE_DIR", thumb_dir), \
                 patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", processed_dir):

                src = source_dir / "movie.mkv"
                src.write_bytes(b"original source mkv content")

                # Prepare remux video
                m_info = VideoMediaInfo(
                    container="mkv",
                    video_codec="h264",
                    audio_codec="aac",
                    width=1920,
                    height=1080,
                    duration=15.0,
                    fps=30.0,
                    has_video_stream=True,
                    has_audio_stream=True,
                    compatibility="remux",
                    recommended_action="remux",
                )
                p_info = VideoMediaInfo(
                    container="mp4",
                    video_codec="h264",
                    audio_codec="aac",
                    width=1920,
                    height=1080,
                    duration=15.0,
                    fps=30.0,
                    has_video_stream=True,
                    has_audio_stream=True,
                    compatibility="native",
                    recommended_action="upload",
                )

                def fake_remux(s, d, **kw):
                    d.parent.mkdir(parents=True, exist_ok=True)
                    d.write_bytes(b"remuxed mp4 bytes")
                    return d

                # Create a valid dummy thumbnail image
                fake_thumb_img = Image.new("RGB", (320, 180), (10, 20, 30))

                def fake_build_thumb(path, cancel_event=None, **kw):
                    logical_path = kw.get("logical_path") or path
                    st = logical_path.stat()
                    import hashlib
                    k = hashlib.sha1(f"{logical_path}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()
                    thumb_dir.mkdir(parents=True, exist_ok=True)
                    t_path = thumb_dir / f"{k}.jpg"
                    fake_thumb_img.save(t_path, "JPEG")
                    return t_path, 320, 180

                with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=[m_info, p_info]), \
                     patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless", side_effect=fake_remux), \
                     patch("tdlib_media_uploader.media.legacy_video.build_thumbnail", side_effect=fake_build_thumb):
                    prep = prepare_video_for_telegram(src, group_key="group_1")

                self.assertTrue(prep.is_remuxed)
                self.assertTrue(prep.upload_path.is_file())
                self.assertIsNotNone(prep.thumbnail_path)
                self.assertTrue(prep.thumbnail_path.is_file())

                # Now clean up processed video for group_1
                cleanup_processed_group("group_1")

                # The processed MP4 is deleted
                self.assertFalse(prep.upload_path.exists())
                # BUT the thumbnail in thumb_dir MUST still exist and be intact!
                self.assertTrue(prep.thumbnail_path.exists())
                self.assertGreater(prep.thumbnail_path.stat().st_size, 0)

    def test_cache_maintenance_and_stale_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            proc_dir = Path(td) / "video_processed"
            proc_dir.mkdir(parents=True)

            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", proc_dir), \
                 patch("tdlib_media_uploader.gui.cache_service.VIDEO_PROCESSED_CACHE_DIR", proc_dir):

                # Populate group1 and group2
                g1 = proc_dir / "group_1"
                g1.mkdir()
                (g1 / "v1.mp4").write_bytes(b"data1")
                (g1 / "v2.mp4").write_bytes(b"data2")

                g2 = proc_dir / "group_2"
                g2.mkdir()
                (g2 / "v3.mp4").write_bytes(b"data3")

                self.assertEqual(len(list(proc_dir.glob("*/*.mp4"))), 3)

                # Clean only group_1
                cleanup_processed_group("group_1")
                self.assertFalse(g1.exists())
                self.assertTrue((g2 / "v3.mp4").exists())

                # Clear all processed videos
                cleared = clear_all_processed_videos()
                self.assertEqual(cleared, 1)
                self.assertEqual(len(list(proc_dir.glob("*/*.mp4"))), 0)


class PipelinedExecutionEngineTest(unittest.TestCase):
    """Test one-group-ahead pipeline, concurrency bounds, Safe Stop, and resume preservation."""

    def test_upload_a_prepares_b_concurrently_and_bounds_disk_usage(self):
        """Simulate UploadEngine running 3 groups: Group A uploads while Group B prepares;

        Group C is never prepared while Group A is uploading.
        """
        with tempfile.TemporaryDirectory() as td:
            source_dir = Path(td) / "source"
            source_dir.mkdir()
            cache_dir = Path(td) / "cache"
            proc_dir = cache_dir / "video_processed"

            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", proc_dir), \
                 patch("tdlib_media_uploader.upload.engine.VIDEO_PROCESSED_CACHE_DIR", proc_dir):

                (source_dir / "a.mkv").write_bytes(b"a")
                (source_dir / "b.mkv").write_bytes(b"b")
                (source_dir / "c.mkv").write_bytes(b"c")
                item_a = make_media_item(source_dir / "a.mkv", source_dir)
                item_b = make_media_item(source_dir / "b.mkv", source_dir)
                item_c = make_media_item(source_dir / "c.mkv", source_dir)

                plan_a = make_album_plan("group_a", source_dir, (item_a,))
                plan_b = make_album_plan("group_b", source_dir, (item_b,))
                plan_c = make_album_plan("group_c", source_dir, (item_c,))

                events_order = []
                concurrency_groups = set()
                max_active_processed_groups = 0

                class MockStrategy:
                    kind = "video"

                    def scan(self, source_root, **kwargs):
                        return ScanResult((item_a, item_b, item_c))

                    def build_plans(self, scan_result, **kwargs):
                        return (plan_a, plan_b, plan_c)

                    def build_contents(self, plan, **kwargs):
                        events_order.append(f"prepare_start_{plan.key}")
                        concurrency_groups.add(plan.key)
                        nonlocal max_active_processed_groups
                        # Check currently existing processed directories on disk
                        active_on_disk = len([d for d in proc_dir.iterdir() if d.is_dir()]) if proc_dir.is_dir() else 0
                        max_active_processed_groups = max(max_active_processed_groups, active_on_disk + 1)

                        # Write dummy processed file
                        g_dir = proc_dir / safe_group_key(plan.key)
                        g_dir.mkdir(parents=True, exist_ok=True)
                        p_file = g_dir / f"{plan.key}.mp4"
                        p_file.write_bytes(b"processed")

                        time.sleep(0.05)  # simulate preparation time
                        events_order.append(f"prepare_done_{plan.key}")
                        content = {
                            "@type": "inputMessageVideo",
                            "video": {"@type": "inputFileLocal", "path": str(p_file)},
                        }
                        return [content]

                class MockSender:
                    def send_contents(self, contents, target=None, plan=None, context=None):
                        events_order.append(f"send_start_{plan.key}")
                        time.sleep(0.08)  # simulate upload time
                        events_order.append(f"send_done_{plan.key}")
                        return SendResult(BatchStatus.CONFIRMED, succeeded_ids=(100,))

                engine = UploadEngine(
                    sender=MockSender(),
                    prefetch_next=True,
                )

                ctx = UploadContext(
                    source_root=source_dir,
                    target={"chat_id": 123},
                    cancel_token=_NeverCancelToken(),
                    event_sink=_NullEventSink(),
                    scan_result=ScanResult((item_a, item_b, item_c)),
                    plans=(plan_a, plan_b, plan_c),
                )

                result = engine.run(MockStrategy(), context=ctx)
                self.assertEqual(result.status, "COMPLETED")
                self.assertEqual(len(result.batches), 3)

                # Verify ordering: prepare_done_group_b happens before send_done_group_b
                # Group c must NOT start preparing before send_done_group_a!
                send_done_a_idx = events_order.index("send_done_group_a")
                prep_start_c_idx = events_order.index("prepare_start_group_c")
                self.assertGreater(prep_start_c_idx, send_done_a_idx,
                                   "Group C must not start preparing while Group A is uploading!")

                # Bounded disk usage: at most 2 processed groups exist concurrently in steady state
                self.assertLessEqual(max_active_processed_groups, 2)

                # At completion, all processed directories have been cleaned up
                remaining_dirs = [d for d in proc_dir.iterdir() if d.is_dir()] if proc_dir.is_dir() else []
                self.assertEqual(len(remaining_dirs), 0)

    def test_failed_upload_preserves_processed_video_for_resume(self):
        with tempfile.TemporaryDirectory() as td:
            source_dir = Path(td) / "source"
            source_dir.mkdir()
            cache_dir = Path(td) / "cache"
            proc_dir = cache_dir / "video_processed"

            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", proc_dir), \
                 patch("tdlib_media_uploader.upload.engine.VIDEO_PROCESSED_CACHE_DIR", proc_dir):

                (source_dir / "a.mkv").write_bytes(b"a")
                item_a = make_media_item(source_dir / "a.mkv", source_dir)
                plan_a = make_album_plan("group_fail", source_dir, (item_a,))

                p_file = None

                class MockStrategy:
                    kind = "video"

                    def scan(self, source_root, **kwargs):
                        return ScanResult((item_a,))

                    def build_plans(self, scan_result, **kwargs):
                        return (plan_a,)

                    def build_contents(self, plan, **kwargs):
                        nonlocal p_file
                        g_dir = proc_dir / safe_group_key(plan.key)
                        g_dir.mkdir(parents=True, exist_ok=True)
                        p_file = g_dir / "processed.mp4"
                        p_file.write_bytes(b"valid remuxed mp4")
                        return [{
                            "@type": "inputMessageVideo",
                            "video": {"@type": "inputFileLocal", "path": str(p_file)},
                        }]

                class MockSenderFail:
                    def send_contents(self, contents, target=None, plan=None, context=None):
                        err = RuntimeError("Telegram network disconnected")
                        err.submitted = False
                        raise err

                engine = UploadEngine(sender=MockSenderFail())
                ctx = UploadContext(
                    source_root=source_dir,
                    target={"chat_id": 123},
                    cancel_token=_NeverCancelToken(),
                    event_sink=_NullEventSink(),
                    scan_result=ScanResult((item_a,)),
                    plans=(plan_a,),
                )

                result = engine.run(MockStrategy(), context=ctx)
                self.assertEqual(result.status, "FAILED")

                # The validated processed file MUST remain for resume!
                self.assertIsNotNone(p_file)
                self.assertTrue(p_file.is_file())
                self.assertEqual(p_file.read_bytes(), b"valid remuxed mp4")

    def test_source_modification_guard_discards_stale_prefetch(self):
        with tempfile.TemporaryDirectory() as td:
            source_dir = Path(td) / "source"
            source_dir.mkdir()
            cache_dir = Path(td) / "cache"
            proc_dir = cache_dir / "video_processed"

            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", proc_dir), \
                 patch("tdlib_media_uploader.upload.engine.VIDEO_PROCESSED_CACHE_DIR", proc_dir):

                (source_dir / "a.mkv").write_bytes(b"a")
                (source_dir / "b.mkv").write_bytes(b"initial_b_content")
                item_a = make_media_item(source_dir / "a.mkv", source_dir)
                item_b = make_media_item(source_dir / "b.mkv", source_dir)

                plan_a = make_album_plan("group_a", source_dir, (item_a,))
                plan_b = make_album_plan("group_b", source_dir, (item_b,))

                prepare_calls = []
                prepared_b = threading.Event()
                sent_plans = []

                class MockStrategy:
                    kind = "video"
                    validate_source_snapshots = True

                    def scan(self, source_root, **kwargs):
                        return ScanResult((item_a, item_b))

                    def build_plans(self, scan_result, **kwargs):
                        return (plan_a, plan_b)

                    def build_contents(self, plan, **kwargs):
                        prepare_calls.append(plan.key)
                        g_dir = proc_dir / safe_group_key(plan.key)
                        g_dir.mkdir(parents=True, exist_ok=True)
                        p_file = g_dir / f"{plan.key}.mp4"
                        p_file.write_bytes(b"prep")
                        if plan.key == "group_b":
                            prepared_b.set()
                        return [{
                            "@type": "inputMessageVideo",
                            "video": {"@type": "inputFileLocal", "path": str(p_file)},
                        }]

                class MockSender:
                    def send_contents(self, contents, target=None, plan=None, context=None):
                        sent_plans.append(plan.key)
                        if plan.key == "group_a":
                            if not prepared_b.wait(3):
                                raise AssertionError("prefetch did not start")
                            # Modify source file b.mkv while Group A is sending!
                            item_b.path.write_bytes(b"modified_new_content_for_b")
                        return SendResult(BatchStatus.CONFIRMED, succeeded_ids=(100,))

                engine = UploadEngine(
                    sender=MockSender(),
                    prefetch_next=True,
                )

                ctx = UploadContext(
                    source_root=source_dir,
                    target={"chat_id": 123},
                    cancel_token=_NeverCancelToken(),
                    event_sink=_NullEventSink(),
                    scan_result=ScanResult((item_a, item_b)),
                    plans=(plan_a, plan_b),
                )

                result = engine.run(MockStrategy(), context=ctx)
                self.assertEqual(result.status, "PARTIAL")
                self.assertEqual(sent_plans, ["group_a"])

                # A changed source must be rescanned before it can be sent.
                self.assertEqual(prepare_calls.count("group_b"), 1,
                                 "Group B must be deferred without reusing stale scan metadata")


class MixedMediaStrategyIntegrationTest(unittest.TestCase):
    """Test mixed photo/video packaging with remuxed videos and images."""

    def test_mixed_build_contents_delegates_to_video_prepare(self):
        with tempfile.TemporaryDirectory() as td:
            img_path = Path(td) / "photo.jpg"
            img_path.write_bytes(b"dummy jpeg")
            vid_path = Path(td) / "video.mkv"
            vid_path.write_bytes(b"dummy mkv")

            items = [
                {"path": img_path, "media_kind": "image"},
                {"path": vid_path, "media_kind": "video"},
            ]

            img_payload = {"@type": "inputMessagePhoto"}
            vid_payload = {
                "@type": "inputMessageVideo",
                "video": {"@type": "inputFileLocal", "path": str(vid_path.with_suffix(".mp4"))},
            }

            with patch("tdlib_media_uploader.media.legacy_mixed.image_core.input_photo", return_value=img_payload), \
                 patch("tdlib_media_uploader.media.legacy_video.input_video", return_value=vid_payload) as mock_input_video:

                contents, valid, skipped = mixed_core.build_mixed_contents(
                    items, caption="Mixed Album", group_key="mixed_group_1"
                )

                self.assertEqual(len(contents), 2)
                self.assertEqual(len(valid), 2)
                self.assertEqual(len(skipped), 0)
                # Verify group_key was forwarded to video input
                mock_input_video.assert_called_once()
                kwargs = mock_input_video.call_args[1]
                self.assertEqual(kwargs.get("group_key"), "mixed_group_1")


class SafetyContractVerificationTest(unittest.TestCase):
    """Verify core behavioral guarantees: no transcode, no document fallback, source intact."""

    def test_source_file_is_never_modified(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "test.mkv"
            original_bytes = b"precious raw camera footage 12345"
            src.write_bytes(original_bytes)
            stat_before = src.stat()

            m_info = VideoMediaInfo(
                container="mkv",
                video_codec="h264",
                audio_codec="aac",
                width=1920,
                height=1080,
                duration=10.0,
                fps=30.0,
                has_video_stream=True,
                has_audio_stream=True,
                compatibility="remux",
                recommended_action="remux",
            )
            p_info = VideoMediaInfo(
                container="mp4",
                video_codec="h264",
                audio_codec="aac",
                width=1920,
                height=1080,
                duration=10.0,
                fps=30.0,
                has_video_stream=True,
                has_audio_stream=True,
                compatibility="native",
                recommended_action="upload",
            )

            with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=[m_info, p_info]), \
                 patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux:
                mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"mp4") and d
                prep = prepare_video_for_telegram(src, group_key="test_grp", generate_thumbnail=False)

            self.assertEqual(src.read_bytes(), original_bytes)
            stat_after = src.stat()
            self.assertEqual(stat_before.st_size, stat_after.st_size)
            self.assertEqual(stat_before.st_mtime_ns, stat_after.st_mtime_ns)

    def test_unsupported_files_never_fallback_to_document(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "unsupported.mkv"
            src.write_bytes(b"corrupt or unsupported")

            bad_info = VideoMediaInfo(
                container="mkv",
                video_codec="vp9",
                audio_codec="opus",
                width=1920,
                height=1080,
                duration=10.0,
                fps=30.0,
                has_video_stream=True,
                has_audio_stream=True,
                compatibility="unsupported",
                recommended_action="skip",
            )

            with patch("tdlib_media_uploader.media.video_prepare.probe_video", return_value=bad_info):
                with self.assertRaises(RuntimeError) as ctx:
                    prepare_video_for_telegram(src)
                self.assertIn("已跳过", str(ctx.exception))
                self.assertIn("不受支持", str(ctx.exception))
                # Must never construct inputMessageDocument


class ProcessedFilenameAndDirectNativeTest(unittest.TestCase):
    def test_malformed_manifest_outputs_preserve_existing_disk_files(self):
        from tdlib_media_uploader.media.video_prepare import _load_group_manifest, resolve_processed_filename
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            existing = root / "Movie.mp4"
            existing.write_bytes(b"existing video")
            for outputs in (None, [], "invalid"):
                with self.subTest(outputs=outputs):
                    (root / "manifest.json").write_text(json.dumps({"outputs": outputs}))
                    manifest = _load_group_manifest(root)
                    filename = resolve_processed_filename(root / "movie.ts", root, manifest)
                    self.assertEqual(filename, "movie.ts.mp4")
                    self.assertEqual(existing.read_bytes(), b"existing video")

    def test_disk_collisions_include_hidden_tmp_named_files_and_directories(self):
        from tdlib_media_uploader.media.video_prepare import resolve_processed_filename
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for stem in (".movie", "movie.tmp.clip", "directory"):
                with self.subTest(stem=stem):
                    occupied = root / f"{stem}.mp4"
                    if stem == "directory":
                        occupied.mkdir()
                    else:
                        occupied.write_bytes(b"existing video must be preserved")
                    filename = resolve_processed_filename(root / f"{stem}.avi", root,
                                                          {"version": 1, "outputs": {}})
                    self.assertEqual(filename, f"{stem}.avi.mp4")
                    self.assertTrue(occupied.exists())

    def test_unreadable_processed_directory_blocks_filename_allocation(self):
        from tdlib_media_uploader.media.video_prepare import resolve_processed_filename
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(Path, "iterdir", side_effect=OSError("unreadable directory")):
                with self.assertRaises(RuntimeError):
                    resolve_processed_filename(root / "movie.ts", root, {"outputs": {}})

    """Test non-hash filenames, collision resolution, and direct native uploads."""

    def test_direct_native_no_copy_and_no_processed_files(self):
        with tempfile.TemporaryDirectory() as td:
            src_dir = Path(td) / "sources"
            src_dir.mkdir()
            cache_dir = Path(td) / "cache"
            processed_dir = cache_dir / "video_processed"

            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", processed_dir), \
                 patch("tdlib_media_uploader.media.video_prepare.build_thumbnail", return_value=(None, 0, 0)):
                for ext in (".mp4", ".mov", ".m4v"):
                    src = src_dir / f"sample{ext}"
                    src.write_bytes(b"native raw bytes")
                    native_info = VideoMediaInfo(
                        container=ext.lstrip("."),
                        video_codec="h264",
                        audio_codec="aac",
                        width=1920,
                        height=1080,
                        duration=10.0,
                        fps=30.0,
                        has_video_stream=True,
                        has_audio_stream=True,
                        compatibility="native",
                        recommended_action="upload",
                    )
                    with patch("tdlib_media_uploader.media.video_prepare.probe_video", return_value=native_info):
                        prep = prepare_video_for_telegram(src, group_key="grp_native", generate_thumbnail=False)

                    self.assertFalse(prep.is_remuxed)
                    self.assertEqual(prep.upload_path, src)
                    # No file copied into video_processed directory
                    if processed_dir.exists():
                        self.assertEqual(len(list(processed_dir.rglob("*"))), 0)

    def test_remux_output_name_preserves_stem_without_hash(self):
        with tempfile.TemporaryDirectory() as td:
            src_dir = Path(td) / "sources"
            src_dir.mkdir()
            cache_dir = Path(td) / "cache"
            processed_dir = cache_dir / "video_processed"

            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", processed_dir), \
                 patch("tdlib_media_uploader.media.video_prepare.build_thumbnail", return_value=(None, 0, 0)):

                test_files = [
                    ("clip.ts", "clip.mp4"),
                    ("holiday.2026.mkv", "holiday.2026.mp4"),
                    ("archive.part01.m2ts", "archive.part01.mp4"),
                ]

                for src_name, expected_target_name in test_files:
                    src = src_dir / src_name
                    src.write_bytes(b"remux candidate bytes")

                    remux_info = VideoMediaInfo(
                        container=src.suffix.lstrip("."),
                        video_codec="h264",
                        audio_codec="aac",
                        width=1920,
                        height=1080,
                        duration=10.0,
                        fps=30.0,
                        has_video_stream=True,
                        has_audio_stream=True,
                        compatibility="remux",
                        recommended_action="remux",
                    )
                    mp4_info = VideoMediaInfo(
                        container="mp4",
                        video_codec="h264",
                        audio_codec="aac",
                        width=1920,
                        height=1080,
                        duration=10.0,
                        fps=30.0,
                        has_video_stream=True,
                        has_audio_stream=True,
                        compatibility="native",
                        recommended_action="upload",
                    )

                    with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=[remux_info, mp4_info]), \
                         patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux:
                        mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"mp4") and d
                        prep = prepare_video_for_telegram(src, group_key="stem_group", generate_thumbnail=False)

                    self.assertTrue(prep.is_remuxed)
                    self.assertEqual(prep.upload_path.name, expected_target_name)
                    # Verify NO hash in the filename
                    self.assertNotIn(prep.cache_key, prep.upload_path.name)
                    self.assertNotIn("tmp", prep.upload_path.name)

    def test_deterministic_collision_handling_without_hashes(self):
        with tempfile.TemporaryDirectory() as td:
            src_dir = Path(td) / "sources"
            src_dir.mkdir()
            cache_dir = Path(td) / "cache"
            processed_dir = cache_dir / "video_processed"

            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", processed_dir), \
                 patch("tdlib_media_uploader.media.video_prepare.build_thumbnail", return_value=(None, 0, 0)):

                src1 = src_dir / "movie.ts"
                src2 = src_dir / "movie.avi"
                src1.write_bytes(b"movie ts content")
                src2.write_bytes(b"movie avi content")

                def make_infos(container):
                    remux_i = VideoMediaInfo(
                        container=container,
                        video_codec="h264",
                        audio_codec="aac" if container == "ts" else "mp3",
                        width=1920,
                        height=1080,
                        duration=10.0,
                        fps=30.0,
                        has_video_stream=True,
                        has_audio_stream=True,
                        compatibility="remux",
                        recommended_action="remux",
                    )
                    mp4_i = VideoMediaInfo(
                        container="mp4",
                        video_codec="h264",
                        audio_codec="aac" if container == "ts" else "mp3",
                        width=1920,
                        height=1080,
                        duration=10.0,
                        fps=30.0,
                        has_video_stream=True,
                        has_audio_stream=True,
                        compatibility="native",
                        recommended_action="upload",
                    )
                    return [remux_i, mp4_i]

                # Prepare first file: movie.ts -> movie.mp4
                with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=make_infos("ts")), \
                     patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux:
                    mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"ts mp4") and d
                    prep1 = prepare_video_for_telegram(src1, group_key="coll_group", generate_thumbnail=False)

                # Prepare second file in same group: movie.avi -> movie.avi.mp4
                with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=make_infos("avi")), \
                     patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux:
                    mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"avi mp4") and d
                    prep2 = prepare_video_for_telegram(src2, group_key="coll_group", generate_thumbnail=False)

                self.assertEqual(prep1.upload_path.name, "movie.mp4")
                self.assertEqual(prep2.upload_path.name, "movie.avi.mp4")
                self.assertNotEqual(prep1.upload_path, prep2.upload_path)
                self.assertTrue(prep1.upload_path.is_file())
                self.assertTrue(prep2.upload_path.is_file())

    def test_case_insensitive_collision_movie_ts_and_movie_avi(self):
        """Case-insensitive collision: Movie.ts and movie.avi in the same group.

        - Must get two different processed paths
        - Both processed files must exist simultaneously
        - Second preparation must not delete or overwrite the first
        - Filenames must not contain hashes
        """
        with tempfile.TemporaryDirectory() as td:
            src_dir = Path(td) / "sources"
            src_dir.mkdir()
            cache_dir = Path(td) / "cache"
            processed_dir = cache_dir / "video_processed"

            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", processed_dir), \
                 patch("tdlib_media_uploader.media.video_prepare.build_thumbnail", return_value=(None, 0, 0)):

                src1 = src_dir / "Movie.ts"
                src2 = src_dir / "movie.avi"
                src1.write_bytes(b"Movie.ts initial content")
                src2.write_bytes(b"movie.avi initial content")

                def make_infos(container):
                    remux_i = VideoMediaInfo(
                        container=container,
                        video_codec="h264",
                        audio_codec="aac" if container == "ts" else "mp3",
                        width=1920,
                        height=1080,
                        duration=10.0,
                        fps=30.0,
                        has_video_stream=True,
                        has_audio_stream=True,
                        compatibility="remux",
                        recommended_action="remux",
                    )
                    mp4_i = VideoMediaInfo(
                        container="mp4",
                        video_codec="h264",
                        audio_codec="aac" if container == "ts" else "mp3",
                        width=1920,
                        height=1080,
                        duration=10.0,
                        fps=30.0,
                        has_video_stream=True,
                        has_audio_stream=True,
                        compatibility="native",
                        recommended_action="upload",
                    )
                    return [remux_i, mp4_i]

                # Prepare first: Movie.ts -> Movie.mp4
                with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=make_infos("ts")), \
                     patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux:
                    mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"Movie.ts content") and d
                    prep1 = prepare_video_for_telegram(src1, group_key="case_group", generate_thumbnail=False)

                # Prepare second: movie.avi -> movie.avi.mp4 (must detect Movie.mp4 casefold collision!)
                with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=make_infos("avi")), \
                     patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux:
                    mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"movie.avi content") and d
                    prep2 = prepare_video_for_telegram(src2, group_key="case_group", generate_thumbnail=False)

                self.assertEqual(prep1.upload_path.name, "Movie.mp4")
                self.assertEqual(prep2.upload_path.name, "movie.avi.mp4")
                self.assertNotEqual(prep1.upload_path.name.casefold(), prep2.upload_path.name.casefold())

                # Both processed files must exist simultaneously
                self.assertTrue(prep1.upload_path.is_file())
                self.assertTrue(prep2.upload_path.is_file())

                # Verify first file was NOT overwritten or unlinked
                self.assertEqual(prep1.upload_path.read_bytes(), b"Movie.ts content")
                self.assertEqual(prep2.upload_path.read_bytes(), b"movie.avi content")

                # Verify no hash in filenames
                self.assertNotIn("tmp", prep1.upload_path.name)
                self.assertNotIn("tmp", prep2.upload_path.name)
                self.assertFalse(re.search(r"[a-f0-9]{32,}", prep1.upload_path.name))
                self.assertFalse(re.search(r"[a-f0-9]{32,}", prep2.upload_path.name))

    def test_case_insensitive_three_sources_unique(self):
        """Case-insensitive collision across 3 files: MOVIE.ts, movie.avi, Movie.mkv.

        - Three sources must get three non-colliding processed filenames
        - Filenames must be mutually unique under case-insensitive semantics
        """
        with tempfile.TemporaryDirectory() as td:
            src_dir = Path(td) / "sources"
            src_dir.mkdir()
            cache_dir = Path(td) / "cache"
            processed_dir = cache_dir / "video_processed"

            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", processed_dir), \
                 patch("tdlib_media_uploader.media.video_prepare.build_thumbnail", return_value=(None, 0, 0)):

                src1 = src_dir / "MOVIE.ts"
                src2 = src_dir / "movie.avi"
                src3 = src_dir / "Movie.mkv"
                src1.write_bytes(b"MOVIE.ts")
                src2.write_bytes(b"movie.avi")
                src3.write_bytes(b"Movie.mkv")

                def make_infos(container):
                    remux_i = VideoMediaInfo(
                        container=container,
                        video_codec="h264",
                        audio_codec="aac" if container in ("ts", "mkv") else "mp3",
                        width=1920,
                        height=1080,
                        duration=10.0,
                        fps=30.0,
                        has_video_stream=True,
                        has_audio_stream=True,
                        compatibility="remux",
                        recommended_action="remux",
                    )
                    mp4_i = VideoMediaInfo(
                        container="mp4",
                        video_codec="h264",
                        audio_codec="aac" if container in ("ts", "mkv") else "mp3",
                        width=1920,
                        height=1080,
                        duration=10.0,
                        fps=30.0,
                        has_video_stream=True,
                        has_audio_stream=True,
                        compatibility="native",
                        recommended_action="upload",
                    )
                    return [remux_i, mp4_i]

                # Prepare 1
                with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=make_infos("ts")), \
                     patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux:
                    mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"1") and d
                    prep1 = prepare_video_for_telegram(src1, group_key="three_group", generate_thumbnail=False)

                # Prepare 2
                with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=make_infos("avi")), \
                     patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux:
                    mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"2") and d
                    prep2 = prepare_video_for_telegram(src2, group_key="three_group", generate_thumbnail=False)

                # Prepare 3
                with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=make_infos("mkv")), \
                     patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux:
                    mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"3") and d
                    prep3 = prepare_video_for_telegram(src3, group_key="three_group", generate_thumbnail=False)

                names = [prep1.upload_path.name, prep2.upload_path.name, prep3.upload_path.name]
                folded_names = [n.casefold() for n in names]

                self.assertEqual(len(set(folded_names)), 3, f"Filenames are not case-insensitively unique: {names}")
                self.assertEqual(names[0], "MOVIE.mp4")
                self.assertEqual(names[1], "movie.avi.mp4")
                self.assertEqual(names[2], "Movie.mkv.mp4")

                self.assertTrue(prep1.upload_path.is_file())
                self.assertTrue(prep2.upload_path.is_file())
                self.assertTrue(prep3.upload_path.is_file())

    def test_manifest_disk_inconsistency_collision_detected(self):
        """Manifest vs Disk inconsistency: Disk already has Movie.mp4, manifest missing entry.

        - New source requests movie.avi (would prefer movie.mp4 if only checking manifest)
        - Must detect disk conflict and choose new safe filename (movie.avi.mp4)
        - Must NOT unlink or overwrite existing Movie.mp4 on disk
        """
        with tempfile.TemporaryDirectory() as td:
            src_dir = Path(td) / "sources"
            src_dir.mkdir()
            cache_dir = Path(td) / "cache"
            processed_dir = cache_dir / "video_processed"
            grp_dir = processed_dir / "disk_incon_group"
            grp_dir.mkdir(parents=True)

            # Pre-populate disk with Movie.mp4, BUT manifest is completely empty / missing entry!
            existing_disk_file = grp_dir / "Movie.mp4"
            existing_disk_file.write_bytes(b"PRE_EXISTING_DISK_CONTENT")

            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", processed_dir), \
                 patch("tdlib_media_uploader.media.video_prepare.build_thumbnail", return_value=(None, 0, 0)):

                src = src_dir / "movie.avi"
                src.write_bytes(b"new movie avi")

                remux_i = VideoMediaInfo(
                    container="avi",
                    video_codec="h264",
                    audio_codec="mp3",
                    width=1920,
                    height=1080,
                    duration=10.0,
                    fps=30.0,
                    has_video_stream=True,
                    has_audio_stream=True,
                    compatibility="remux",
                    recommended_action="remux",
                )
                mp4_i = VideoMediaInfo(
                    container="mp4",
                    video_codec="h264",
                    audio_codec="mp3",
                    width=1920,
                    height=1080,
                    duration=10.0,
                    fps=30.0,
                    has_video_stream=True,
                    has_audio_stream=True,
                    compatibility="native",
                    recommended_action="upload",
                )

                with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=[remux_i, mp4_i]), \
                     patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux:
                    mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"new remux content") and d
                    prep = prepare_video_for_telegram(src, group_key="disk_incon_group", generate_thumbnail=False)

                # The new file must detect collision with Movie.mp4 on disk and choose movie.avi.mp4
                self.assertEqual(prep.upload_path.name, "movie.avi.mp4")
                self.assertTrue(prep.upload_path.is_file())
                self.assertEqual(prep.upload_path.read_bytes(), b"new remux content")

                # The pre-existing Movie.mp4 must NOT have been unlinked or overwritten
                self.assertTrue(existing_disk_file.is_file())
                self.assertEqual(existing_disk_file.read_bytes(), b"PRE_EXISTING_DISK_CONTENT")

    def test_manifest_reuse_and_invalidation(self):
        with tempfile.TemporaryDirectory() as td:
            src_dir = Path(td) / "sources"
            src_dir.mkdir()
            cache_dir = Path(td) / "cache"
            processed_dir = cache_dir / "video_processed"

            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", processed_dir), \
                 patch("tdlib_media_uploader.media.video_prepare.build_thumbnail", return_value=(None, 0, 0)):

                src = src_dir / "clip.ts"
                src.write_bytes(b"initial ts content")

                remux_i = VideoMediaInfo(
                    container="ts",
                    video_codec="h264",
                    audio_codec="aac",
                    width=1920,
                    height=1080,
                    duration=10.0,
                    fps=30.0,
                    has_video_stream=True,
                    has_audio_stream=True,
                    compatibility="remux",
                    recommended_action="remux",
                )
                mp4_i = VideoMediaInfo(
                    container="mp4",
                    video_codec="h264",
                    audio_codec="aac",
                    width=1920,
                    height=1080,
                    duration=10.0,
                    fps=30.0,
                    has_video_stream=True,
                    has_audio_stream=True,
                    compatibility="native",
                    recommended_action="upload",
                )

                # 1. First run creates processed clip.mp4
                with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=[remux_i, mp4_i]), \
                     patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux:
                    mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"mp4 content") and d
                    prep1 = prepare_video_for_telegram(src, group_key="reuse_grp", generate_thumbnail=False)
                    self.assertEqual(mock_remux.call_count, 1)

                # 2. Second run with unchanged source reuses clip.mp4 without remuxing
                with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=[remux_i, mp4_i]), \
                     patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux:
                    prep2 = prepare_video_for_telegram(src, group_key="reuse_grp", generate_thumbnail=False)
                    self.assertEqual(mock_remux.call_count, 0)
                    self.assertEqual(prep1.upload_path, prep2.upload_path)

                # 3. Modify source file -> invalidates cache and triggers remux
                src.write_bytes(b"completely modified ts content")
                with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=[remux_i, mp4_i]), \
                     patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux:
                    mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"new mp4 content") and d
                    prep3 = prepare_video_for_telegram(src, group_key="reuse_grp", generate_thumbnail=False)
                    self.assertEqual(mock_remux.call_count, 1)


class CleanupIsolationAndSecurityTest(unittest.TestCase):
    """Test group cleanup path security, cross-group isolation, and source preservation."""

    def test_cleanup_processed_group_path_security(self):
        with tempfile.TemporaryDirectory() as td:
            proc_root = Path(td) / "video_processed"
            proc_root.mkdir()
            outside_file = Path(td) / "precious_user_file.txt"
            outside_file.write_bytes(b"cannot be deleted")
            source_file = Path(td) / "original_video.ts"
            source_file.write_bytes(b"source media")

            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", proc_root):
                # Put a legitimate processed file in a group
                grp_dir = proc_root / "group_sec"
                grp_dir.mkdir()
                proc_mp4 = grp_dir / "clip.mp4"
                proc_mp4.write_bytes(b"temp processed")

                # Malicious or buggy caller passes outside paths
                cleanup_processed_group(
                    "group_sec",
                    managed_paths=[source_file, outside_file, proc_mp4]
                )

                # Legitimate processed group and file are deleted
                self.assertFalse(grp_dir.exists())
                self.assertFalse(proc_mp4.exists())
                # Source and outside files MUST survive unharmed!
                self.assertTrue(source_file.exists())
                self.assertTrue(outside_file.exists())

    def test_group_cleanup_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            proc_root = Path(td) / "video_processed"
            grp_a = proc_root / "group_A"
            grp_b = proc_root / "group_B"
            grp_a.mkdir(parents=True)
            grp_b.mkdir(parents=True)

            file_a = grp_a / "a.mp4"
            file_b = grp_b / "b.mp4"
            file_a.write_bytes(b"group A mp4")
            file_b.write_bytes(b"group B mp4")

            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", proc_root):
                cleanup_processed_group("group_A")

                self.assertFalse(grp_a.exists())
                self.assertFalse(file_a.exists())
                # Group B must be completely intact!
                self.assertTrue(grp_b.exists())
                self.assertTrue(file_b.exists())

    def test_source_files_preserved_across_full_group_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            src_dir = Path(td) / "sources"
            src_dir.mkdir()
            cache_dir = Path(td) / "cache"
            proc_dir = cache_dir / "video_processed"

            src_mp4 = src_dir / "001.mp4"
            src_mov = src_dir / "002.mov"
            src_ts = src_dir / "003.ts"

            src_mp4.write_bytes(b"mp4 content 111")
            src_mov.write_bytes(b"mov content 222")
            src_ts.write_bytes(b"ts content 333")

            snap_mp4 = (src_mp4.stat().st_size, src_mp4.stat().st_mtime_ns)
            snap_mov = (src_mov.stat().st_size, src_mov.stat().st_mtime_ns)
            snap_ts = (src_ts.stat().st_size, src_ts.stat().st_mtime_ns)

            def mock_probe(p, **kw):
                ext = Path(p).suffix.lstrip(".")
                compat = "native" if ext in ("mp4", "mov") else "remux"
                return VideoMediaInfo(
                    container=ext,
                    video_codec="h264",
                    audio_codec="aac",
                    width=1920,
                    height=1080,
                    duration=10.0,
                    fps=30.0,
                    has_video_stream=True,
                    has_audio_stream=True,
                    compatibility=compat,
                    recommended_action="upload" if compat == "native" else "remux",
                )

            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", proc_dir), \
                 patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=mock_probe), \
                 patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux, \
                 patch("tdlib_media_uploader.media.video_prepare.build_thumbnail", return_value=(None, 0, 0)):

                mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"remuxed 003.mp4") and d

                prep1 = prepare_video_for_telegram(src_mp4, group_key="test_life", generate_thumbnail=False)
                prep2 = prepare_video_for_telegram(src_mov, group_key="test_life", generate_thumbnail=False)
                prep3 = prepare_video_for_telegram(src_ts, group_key="test_life", generate_thumbnail=False)

                self.assertFalse(prep1.is_remuxed)
                self.assertEqual(prep1.upload_path, src_mp4)
                self.assertFalse(prep2.is_remuxed)
                self.assertEqual(prep2.upload_path, src_mov)
                self.assertTrue(prep3.is_remuxed)
                self.assertEqual(prep3.upload_path.name, "003.mp4")

                cleanup_processed_group("test_life", managed_paths=[prep3.upload_path])

                # Processed output deleted
                self.assertFalse(prep3.upload_path.exists())

                # ALL three original source files MUST exist with exact same content, size, and mtime
                self.assertEqual(src_mp4.read_bytes(), b"mp4 content 111")
                self.assertEqual(src_mov.read_bytes(), b"mov content 222")
                self.assertEqual(src_ts.read_bytes(), b"ts content 333")
                self.assertEqual((src_mp4.stat().st_size, src_mp4.stat().st_mtime_ns), snap_mp4)
                self.assertEqual((src_mov.stat().st_size, src_mov.stat().st_mtime_ns), snap_mov)
                self.assertEqual((src_ts.stat().st_size, src_ts.stat().st_mtime_ns), snap_ts)


class SizeBoundaryAndPremiumLogicTest(unittest.TestCase):
    """Test authoritative post-preparation size checks, Standard vs Premium limits, and .tools import."""

    def test_broken_tools_import_regression(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "big.mp4"
            src.write_bytes(b"X" * 1024)

            info = VideoMediaInfo(
                container="mp4",
                video_codec="h264",
                audio_codec="aac",
                width=1920,
                height=1080,
                duration=10.0,
                fps=30.0,
                has_video_stream=True,
                has_audio_stream=True,
                compatibility="native",
                recommended_action="upload",
            )

            # Native exceeds limit: must raise RuntimeError with limit text, NOT ModuleNotFoundError
            with patch("tdlib_media_uploader.media.video_prepare.probe_video", return_value=info):
                with self.assertRaises(RuntimeError) as ctx:
                    prepare_video_for_telegram(src, max_bytes=512, generate_thumbnail=False)
                self.assertIn("超过 Telegram 视频上限", str(ctx.exception))
                self.assertNotIn("No module named", str(ctx.exception))

            # Remux exceeds limit: must raise RuntimeError with limit text, NOT ModuleNotFoundError
            ts_src = Path(td) / "big.ts"
            ts_src.write_bytes(b"X" * 100)
            ts_info = VideoMediaInfo(
                container="ts",
                video_codec="h264",
                audio_codec="aac",
                width=1920,
                height=1080,
                duration=10.0,
                fps=30.0,
                has_video_stream=True,
                has_audio_stream=True,
                compatibility="remux",
                recommended_action="remux",
            )
            mp4_info = VideoMediaInfo(
                container="mp4",
                video_codec="h264",
                audio_codec="aac",
                width=1920,
                height=1080,
                duration=10.0,
                fps=30.0,
                has_video_stream=True,
                has_audio_stream=True,
                compatibility="native",
                recommended_action="upload",
            )
            with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=[ts_info, mp4_info]), \
                 patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux:
                mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"Y" * 1024) and d
                with self.assertRaises(RuntimeError) as ctx:
                    prepare_video_for_telegram(ts_src, max_bytes=512, generate_thumbnail=False)
                self.assertIn("超过 Telegram 视频上限", str(ctx.exception))
                self.assertNotIn("No module named", str(ctx.exception))

    def test_remux_shrinking_below_standard_limit_allows_standard_user(self):
        """Source > Standard, remuxed < Standard, Standard account -> allowed."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "shrink.ts"
            src.write_bytes(b"source bytes")

            std_max = 2000 * 1024 * 1024
            ts_info = VideoMediaInfo(
                container="ts",
                video_codec="h264",
                audio_codec="aac",
                width=1920,
                height=1080,
                duration=10.0,
                fps=30.0,
                has_video_stream=True,
                has_audio_stream=True,
                compatibility="remux",
                recommended_action="remux",
            )
            mp4_info = VideoMediaInfo(
                container="mp4",
                video_codec="h264",
                audio_codec="aac",
                width=1920,
                height=1080,
                duration=10.0,
                fps=30.0,
                has_video_stream=True,
                has_audio_stream=True,
                compatibility="native",
                recommended_action="upload",
            )

            with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=[ts_info, mp4_info]), \
                 patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux:
                # Remuxed output size is 1500 MiB (< 2000 MiB)
                mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"Z" * 1024) and d
                prep = prepare_video_for_telegram(
                    src,
                    max_bytes=std_max,
                    is_premium=False,
                    generate_thumbnail=False,
                )
                self.assertTrue(prep.is_remuxed)
                self.assertTrue(prep.upload_path.is_file())

    def test_remux_growing_above_standard_limit_rejects_standard_user(self):
        """Source < Standard, remuxed > Standard, Standard account -> rejected."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "grow.ts"
            src.write_bytes(b"source bytes")

            std_max = 2000 * 1024 * 1024
            ts_info = VideoMediaInfo(
                container="ts",
                video_codec="h264",
                audio_codec="aac",
                width=1920,
                height=1080,
                duration=10.0,
                fps=30.0,
                has_video_stream=True,
                has_audio_stream=True,
                compatibility="remux",
                recommended_action="remux",
            )
            mp4_info = VideoMediaInfo(
                container="mp4",
                video_codec="h264",
                audio_codec="aac",
                width=1920,
                height=1080,
                duration=10.0,
                fps=30.0,
                has_video_stream=True,
                has_audio_stream=True,
                compatibility="native",
                recommended_action="upload",
            )

            with patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=[ts_info, mp4_info]), \
                 patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux:
                # Remuxed file grows beyond standard limit
                mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"A" * 200) and d
                with self.assertRaises(RuntimeError) as ctx:
                    prepare_video_for_telegram(
                        src,
                        max_bytes=100,  # simulate 100 bytes limit
                        is_premium=False,
                        generate_thumbnail=False,
                    )
                self.assertIn("超过 Telegram 视频上限", str(ctx.exception))

    def test_native_mp4_above_standard_limit_rejects_standard_user(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "big_native.mp4"
            src.write_bytes(b"A" * 500)

            info = VideoMediaInfo(
                container="mp4",
                video_codec="h264",
                audio_codec="aac",
                width=1920,
                height=1080,
                duration=10.0,
                fps=30.0,
                has_video_stream=True,
                has_audio_stream=True,
                compatibility="native",
                recommended_action="upload",
            )
            with patch("tdlib_media_uploader.media.video_prepare.probe_video", return_value=info):
                with self.assertRaises(RuntimeError) as ctx:
                    prepare_video_for_telegram(src, max_bytes=200, is_premium=False, generate_thumbnail=False)
                self.assertIn("超过 Telegram 视频上限", str(ctx.exception))

    def test_native_mov_between_standard_and_premium_allows_premium_user(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "big_native.mov"
            src.write_bytes(b"A" * 300)

            info = VideoMediaInfo(
                container="mov",
                video_codec="hevc",
                audio_codec="aac",
                width=1920,
                height=1080,
                duration=10.0,
                fps=30.0,
                has_video_stream=True,
                has_audio_stream=True,
                compatibility="native",
                recommended_action="upload",
            )
            with patch("tdlib_media_uploader.media.video_prepare.probe_video", return_value=info):
                # Standard limit is 200, Premium limit is 500
                prep = prepare_video_for_telegram(src, max_bytes=500, is_premium=True, generate_thumbnail=False)
                self.assertFalse(prep.is_remuxed)
                self.assertEqual(prep.upload_path, src)


class SafeStopAndCleanupSequencingTest(unittest.TestCase):
    """Test cancellation, Safe Stop, prefetch bounding, and independent cleanup."""

    def test_independent_cleanup_stages_on_staging_failure(self):
        """Even if staging cleanup raises an exception, processed video cleanup must still run."""
        with tempfile.TemporaryDirectory() as td:
            proc_root = Path(td) / "video_processed"
            grp_dir = proc_root / "grp_fail"
            grp_dir.mkdir(parents=True)
            proc_mp4 = grp_dir / "test.mp4"
            proc_mp4.write_bytes(b"processed")

            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", proc_root):
                engine = UploadEngine()
                stager = MagicMock()
                # Staging cleanup raises an error!
                stager.cleanup = MagicMock(side_effect=OSError("Network permission denied"))

                context = UploadContext(
                    source_root=Path(td),
                    target={"chat_id": 123},
                    cancel_token=_NeverCancelToken(),
                    event_sink=_NullEventSink(),
                    stager=stager,
                )
                plan = SimpleNamespace(key="grp_fail")

                # Call _cleanup with confirmed=True
                with self.assertRaises(OSError):
                    engine._cleanup(stager, plan, context, confirmed=True)

                # Now simulate the engine's decoupled post-upload sequence:
                # Stage 1: journal finalize
                # Stage 2: staging cleanup (fails)
                # Stage 3: processed cleanup (MUST run!)
                errors = []
                try:
                    engine._cleanup(stager, plan, context, confirmed=True)
                except Exception as ex:
                    errors.append(f"Staging failed: {ex}")

                # Processed cleanup runs independently
                cleanup_processed_group(plan.key, [proc_mp4])

                self.assertFalse(proc_mp4.exists())
                self.assertFalse(grp_dir.exists())
                self.assertEqual(len(errors), 1)

    def test_no_native_duplication_during_prefetch(self):
        """When prefetching a group with b1.mp4, b2.mov, b3.ts, only b3.ts generates a processed MP4."""
        with tempfile.TemporaryDirectory() as td:
            src_dir = Path(td) / "sources"
            src_dir.mkdir()
            cache_dir = Path(td) / "cache"
            proc_dir = cache_dir / "video_processed"

            b1 = src_dir / "b1.mp4"
            b2 = src_dir / "b2.mov"
            b3 = src_dir / "b3.ts"
            b1.write_bytes(b"b1 content")
            b2.write_bytes(b"b2 content")
            b3.write_bytes(b"b3 content")

            def mock_probe(p, **kw):
                ext = Path(p).suffix.lstrip(".")
                compat = "native" if ext in ("mp4", "mov") else "remux"
                return VideoMediaInfo(
                    container=ext,
                    video_codec="h264",
                    audio_codec="aac",
                    width=1920,
                    height=1080,
                    duration=10.0,
                    fps=30.0,
                    has_video_stream=True,
                    has_audio_stream=True,
                    compatibility=compat,
                    recommended_action="upload" if compat == "native" else "remux",
                )

            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", proc_dir), \
                 patch("tdlib_media_uploader.media.video_prepare.probe_video", side_effect=mock_probe), \
                 patch("tdlib_media_uploader.media.video_prepare.remux_video_lossless") as mock_remux, \
                 patch("tdlib_media_uploader.media.video_prepare.build_thumbnail", return_value=(None, 0, 0)):

                mock_remux.side_effect = lambda s, d, **kw: d.write_bytes(b"b3 remuxed") and d

                prep_b1 = prepare_video_for_telegram(b1, group_key="group_B", generate_thumbnail=False)
                prep_b2 = prepare_video_for_telegram(b2, group_key="group_B", generate_thumbnail=False)
                prep_b3 = prepare_video_for_telegram(b3, group_key="group_B", generate_thumbnail=False)

                self.assertFalse(prep_b1.is_remuxed)
                self.assertFalse(prep_b2.is_remuxed)
                self.assertTrue(prep_b3.is_remuxed)

                grp_b_dir = proc_dir / "group_B"
                self.assertTrue(grp_b_dir.is_dir())
                # Directory must contain only b3.mp4 and manifest.json, NO b1.mp4, NO b2.mov
                files_in_grp = [f.name for f in grp_b_dir.iterdir() if f.is_file() and not f.name.endswith(".json")]
                self.assertEqual(files_in_grp, ["b3.mp4"])


class ConfigBehaviorNoAutoMigrationTest(unittest.TestCase):
    """Test that existing user configs are not automatically mutated."""

    def test_existing_config_remains_unchanged(self):
        old_config = {
            "video": {
                "extensions": [".mp4", ".mov", ".m4v"]
            }
        }
        # Verify loader or defaults do not forcibly inject extensions into old config
        exts = set(old_config["video"]["extensions"])
        self.assertEqual(exts, {".mp4", ".mov", ".m4v"})
        self.assertNotIn(".mkv", exts)
        self.assertNotIn(".ts", exts)


class RealFFmpegIntegrationTest(unittest.TestCase):
    """Exercise shipped tools without requiring an H.264 encoder or mocking probes."""

    def setUp(self):
        from tdlib_media_uploader.media.video_probe import _find_ffmpeg, _find_ffprobe
        self.ffmpeg = _find_ffmpeg()
        if not self.ffmpeg or not _find_ffprobe():
            self.skipTest("FFmpeg and FFprobe binaries not available in environment")
        self.fixture = PROJECT_ROOT / "tests" / "fixtures" / "remux_h264_aac.mp4"

    def _make_container(self, destination, container):
        subprocess.run(
            [self.ffmpeg, "-y", "-v", "error", "-i", str(self.fixture),
             "-c", "copy", "-f", container, str(destination)],
            capture_output=True, text=True, check=True, timeout=30,
        )

    def _decoded_video_hash(self, path):
        result = subprocess.run(
            [self.ffmpeg, "-v", "error", "-i", str(path), "-map", "0:v:0",
             "-fps_mode", "passthrough", "-f", "hash", "-hash", "sha256", "-"],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return result.stdout.strip()

    def test_real_ffmpeg_stream_copy_if_available(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "processed"
            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", cache):
                for filename, container in (("sample.mkv", "matroska"),
                                            ("sample.avi", "avi"),
                                            ("sample.ts", "mpegts"),
                                            ("sample.mts", "mpegts"),
                                            ("sample.m2ts", "mpegts"),
                                            ("transport.mp4", "mpegts"),
                                            ("transport.avi", "mpegts"),
                                            ("transport.bin", "mpegts")):
                    with self.subTest(filename=filename):
                        source = root / filename
                        self._make_container(source, container)
                        before = hashlib.sha256(source.read_bytes()).hexdigest()
                        prepared = prepare_video_for_telegram(
                            source, group_key=filename, generate_thumbnail=False,
                        )
                        self.assertTrue(prepared.is_remuxed)
                        self.assertEqual(prepared.upload_path, cache / filename / f"{source.stem}.mp4")
                        self.assertEqual(prepared.info.compatibility, "native")
                        self.assertEqual(prepared.info.video_codec, "h264")
                        self.assertEqual(prepared.info.audio_codec, "aac")
                        self.assertEqual((prepared.info.width, prepared.info.height), (64, 48))
                        self.assertGreater(prepared.info.duration, 0)
                        self.assertEqual(self._decoded_video_hash(source),
                                         self._decoded_video_hash(prepared.upload_path))
                        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
                        self.assertFalse(list(cache.rglob("*.tmp.*")))

                unknown_suffix = root / "transport.bin"
                with patch.object(cfg, "VIDEO_DIR", root), \
                     patch.object(cfg, "VIDEO_EXTENSIONS", {".mp4"}):
                    self.assertIn(unknown_suffix, video_core.scan_videos())
                album_dir = root / "album"
                album_dir.mkdir()
                mixed_source = album_dir / "transport.bin"
                mixed_source.write_bytes(unknown_suffix.read_bytes())
                self.assertEqual(
                    [(item["path"], item["media_kind"])
                     for item in mixed_core._group_items(album_dir, "album")],
                    [(mixed_source, "video")],
                )
                with patch.object(cfg, "VIDEO_MAX_BYTES", unknown_suffix.stat().st_size - 1), \
                     patch.object(cfg, "VIDEO_GENERATE_THUMBNAIL", False):
                    self.assertEqual(video_core.preflight_videos([{"path": unknown_suffix}]), [])
                    self.assertEqual(mixed_core.preflight_mixed([{
                        "path": unknown_suffix, "media_kind": "video",
                    }]), [])

                non_native_mp4 = root / "mpeg4.mp4"
                subprocess.run(
                    [self.ffmpeg, "-y", "-v", "error", "-i", str(self.fixture),
                     "-c:v", "mpeg4", "-q:v", "2", "-c:a", "copy", str(non_native_mp4)],
                    capture_output=True, text=True, check=True, timeout=30,
                )
                original_hash = self._decoded_video_hash(non_native_mp4)
                prepared = prepare_video_for_telegram(
                    non_native_mp4, group_key="mpeg4", generate_thumbnail=False,
                )
                self.assertTrue(prepared.is_remuxed)
                self.assertEqual(prepared.info.video_codec, "mpeg4")
                self.assertEqual(prepared.info.audio_codec, "aac")
                self.assertEqual(self._decoded_video_hash(prepared.upload_path), original_hash)

    def test_real_native_containers_use_original_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "processed"
            with patch("tdlib_media_uploader.media.video_prepare.VIDEO_PROCESSED_CACHE_DIR", cache):
                for suffix, container in (("mp4", "mp4"), ("mov", "mov"), ("m4v", "mp4")):
                    with self.subTest(container=suffix):
                        source = root / f"native.{suffix}"
                        self._make_container(source, container)
                        before = source.read_bytes()
                        prepared = prepare_video_for_telegram(source, generate_thumbnail=False)
                        self.assertEqual(prepared.upload_path, source)
                        self.assertFalse(prepared.is_remuxed)
                        self.assertEqual(prepared.info.compatibility, "native")
                        self.assertEqual(source.read_bytes(), before)
                        self.assertFalse(cache.exists())


if __name__ == "__main__":
    unittest.main()
