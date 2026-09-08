"""Date priority tests, plus real FFmpeg fixtures when available."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import gui_app  # Creates the source-run config before importing upload cores.
import tdlib_video_album_uploader as core
import tdlib_video_app as flow


class MediaDatesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "媒体 日期.mp4"
        self.path.write_bytes(b"test")
        os.utime(self.path, (1784935138, 1784935138))
        self.policy = patch.object(core.cfg, "VIDEO_MISSING_DATE_POLICY", "mtime")
        self.policy.start()
        self.addCleanup(self.policy.stop)
        self.zone = patch.object(core.cfg, "VIDEO_QUICKTIME_UTC_TARGET_ZONE", None)
        self.zone.start()
        self.addCleanup(self.zone.stop)
        core._media_creation_metadata.cache_clear()

    def test_exif_wins_without_probing_media(self):
        with patch.object(core, "read_media_creation_time") as probe:
            selected = core.choose_capture_time(self.path, {
                "ExifIFD:DateTimeOriginal": "2023:05:02 10:20:30+02:00",
                "QuickTime:CreateDate": "2024-06-29T03:48:00Z",
            })
        self.assertEqual(selected["datetime"].year, 2023)
        self.assertEqual(selected["tag"], "ExifIFD:DateTimeOriginal")
        self.assertEqual(selected["datetime"].utcoffset().total_seconds(), 7200)
        probe.assert_not_called()

    def test_media_before_mtime_and_strict_mode_accepts_media(self):
        media = (datetime(2024, 6, 29, 3, 48, tzinfo=timezone.utc), "Media:creation_time", True)
        with patch.object(core, "read_media_creation_time", return_value=media), patch.object(core.cfg, "VIDEO_MISSING_DATE_POLICY", "error"):
            selected = core.choose_capture_time(self.path, {})
        self.assertEqual(selected["datetime"].astimezone(timezone.utc), media[0])
        self.assertFalse(selected["fallback"])
        self.assertEqual(selected["tag"], "Media:creation_time")

    def test_missing_media_uses_mtime_not_file_creation(self):
        with patch.object(core, "read_media_creation_time", return_value=None):
            selected = core.choose_capture_time(self.path, {})
            self.assertEqual(selected["datetime"].timestamp(), self.path.stat().st_mtime)
            self.assertEqual(selected["tag"], "FileSystem:ModifyTime")
            with patch.object(core.cfg, "VIDEO_MISSING_DATE_POLICY", "error"):
                self.assertIsNone(core.choose_capture_time(self.path, {}))

    def test_invalid_exif_continues_to_media(self):
        media = (datetime(2024, 6, 29), "Media:creation_time", True)
        with patch.object(core, "read_media_creation_time", return_value=media):
            selected = core.choose_capture_time(self.path, {"QuickTime:CreateDate": "1904-01-01 00:00:00", "ExifIFD:DateTimeOriginal": "0000:00:00 00:00:00"})
        self.assertTrue(selected["tag"].startswith("Media:"))

    def test_stream_fallback_cache_and_file_change(self):
        outputs = [subprocess.CompletedProcess([], 0, ";FFMETADATA1\ntitle=test\n"), subprocess.CompletedProcess([], 0, ";FFMETADATA1\ncreation_time=2024-06-29T03:48:00.123456Z\n")]
        with patch.object(core.imageio_ffmpeg, "get_ffmpeg_exe", return_value="ffmpeg"), patch.object(core.subprocess, "run", side_effect=outputs * 2) as run:
            first = core.read_media_creation_time(self.path)
            self.assertEqual(first[0].microsecond, 123456)
            self.assertEqual(core.read_media_creation_time(self.path), first)
            self.assertEqual(run.call_count, 2)
            self.assertIn("0:s:v:0", run.call_args.args[0])
            self.path.write_bytes(b"changed-size")
            core.read_media_creation_time(self.path)
            self.assertEqual(run.call_count, 4)

    def test_timeout_falls_back(self):
        with patch.object(core.imageio_ffmpeg, "get_ffmpeg_exe", return_value="ffmpeg"), patch.object(core.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 20)):
            selected = core.choose_capture_time(self.path, {})
        self.assertEqual(selected["tag"], "FileSystem:ModifyTime")

    def test_no_exiftool_does_not_block_strict_media_dates(self):
        with patch.object(core.cfg, "EXIFTOOL_PATH", self.path.parent / "absent"), patch.object(core.cfg, "VIDEO_MISSING_DATE_POLICY", "error"):
            self.assertEqual(flow.read_metadata(), ({}, False))

    def test_actual_mp4_embedded_date_and_missing_date(self):
        if not os.environ.get("TEST_MEDIA_FFMPEG"):
            self.skipTest("Set TEST_MEDIA_FFMPEG to run real media fixtures")
        executable = os.environ["TEST_MEDIA_FFMPEG"]
        for date in ("2024-06-29T03:48:00Z", None):
            command = [executable, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=32x32:d=0.1", "-c:v", "mpeg4"]
            if date:
                command.extend(["-metadata", f"creation_time={date}"])
            subprocess.run(command + [str(self.path)], check=True, capture_output=True, timeout=30, **core._hidden_subprocess_kwargs())
            os.utime(self.path, (1784935138, 1784935138))
            core._media_creation_metadata.cache_clear()
            with patch.object(core.imageio_ffmpeg, "get_ffmpeg_exe", return_value=executable):
                items, missing = core.build_items([self.path], {})
            self.assertFalse(missing)
            if date:
                self.assertEqual(items[0]["capture_time"].astimezone(timezone.utc).isoformat(), "2024-06-29T03:48:00+00:00")
                self.assertEqual(items[0]["month_key"], "2024-06")
            else:
                self.assertEqual(items[0]["date_tag"], "FileSystem:ModifyTime")
