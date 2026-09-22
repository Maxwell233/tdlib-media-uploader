"""Offline coverage for configurable video thumbnail timestamps."""

from __future__ import annotations

import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from PIL import Image
from PySide6.QtWidgets import QApplication, QDoubleSpinBox

from tdlib_media_uploader.config import loader as cfg
from tdlib_media_uploader.config.snapshot import snapshot_config
from tdlib_media_uploader.gui import config_service
from tdlib_media_uploader.gui.settings import upload_panel as upload_panel_module
from tdlib_media_uploader.gui.settings.upload_panel import UploadPanel
from tdlib_media_uploader.media import legacy_mixed as mixed_core
from tdlib_media_uploader.media import legacy_video as video_core


class ThumbnailTimestampConfigTest(unittest.TestCase):
    def test_defaults_and_mixed_inheritance(self):
        self.assertEqual(cfg._thumbnail_timestamp_seconds({}, "thumbnail_timestamp_seconds"), 1.0)
        video_value = cfg._thumbnail_timestamp_seconds(
            {"thumbnail_timestamp_seconds": 1.234},
            "thumbnail_timestamp_seconds",
        )
        self.assertEqual(video_value, 1.23)
        self.assertEqual(
            cfg._thumbnail_timestamp_seconds(
                {},
                "thumbnail_timestamp_seconds",
                video_value,
            ),
            1.23,
        )
        self.assertEqual(cfg.VIDEO_THUMBNAIL_TIMESTAMP_SECONDS, 1.0)
        self.assertEqual(cfg.MIXED_THUMBNAIL_TIMESTAMP_SECONDS, 1.0)

    def test_valid_values_are_normalized_to_centiseconds(self):
        for value, expected in (
            (0, 0.0),
            (0.01, 0.01),
            (1, 1.0),
            (1.0, 1.0),
            (1.000, 1.0),
            (1.23, 1.23),
            (3600.987, 3600.99),
        ):
            self.assertEqual(
                cfg._thumbnail_timestamp_seconds(
                    {"thumbnail_timestamp_seconds": value},
                    "thumbnail_timestamp_seconds",
                ),
                expected,
            )

    def test_negative_nan_and_infinity_are_rejected(self):
        for value in (-0.01, math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    cfg._thumbnail_timestamp_seconds(
                        {"thumbnail_timestamp_seconds": value},
                        "thumbnail_timestamp_seconds",
                    )

    def test_runtime_snapshot_contains_both_timestamp_values(self):
        frozen = snapshot_config(cfg)
        self.assertEqual(
            frozen.VIDEO_THUMBNAIL_TIMESTAMP_SECONDS,
            cfg.VIDEO_THUMBNAIL_TIMESTAMP_SECONDS,
        )
        self.assertEqual(
            frozen.MIXED_THUMBNAIL_TIMESTAMP_SECONDS,
            cfg.MIXED_THUMBNAIL_TIMESTAMP_SECONDS,
        )

    def test_template_declares_video_and_mixed_values(self):
        import tomllib

        template = tomllib.loads(
            (PROJECT_ROOT / "resources" / "default_config.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(template["video"]["thumbnail_timestamp_seconds"], 1.0)
        self.assertEqual(template["mixed"]["thumbnail_timestamp_seconds"], 1.0)
        self.assertEqual(template["video"]["extensions"], [".mp4"])
        self.assertEqual(
            set(template["image"]["extensions"]),
            {".jpg", ".jpeg", ".png"},
        )

    def test_existing_config_filters_document_only_video_extensions(self):
        self.assertEqual(cfg.VIDEO_EXTENSIONS, {".mp4"})
        self.assertEqual(cfg.MIXED_VIDEO_EXTENSIONS, {".mp4"})
        for extension in (".wmv", ".mkv", ".mov", ".hevc"):
            self.assertNotIn(extension, cfg.VIDEO_EXTENSIONS)

    def test_existing_config_filters_non_native_image_extensions(self):
        self.assertEqual(cfg.IMAGE_EXTENSIONS, {".jpg", ".jpeg", ".png"})
        for extension in (".webp", ".bmp", ".tiff", ".avif", ".gif"):
            with self.subTest(extension=extension):
                self.assertNotIn(extension, cfg.IMAGE_EXTENSIONS)

    def test_mp4_container_remains_supported_for_h265_video(self):
        self.assertIn(".mp4", cfg.VIDEO_EXTENSIONS)

    def test_config_service_persists_centisecond_values_in_both_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                "[video]\ngenerate_thumbnail = true\n[mixed]\ngenerate_thumbnail = true\n",
                encoding="utf-8",
            )
            error = config_service.write_config_values(
                {
                    ("video", "thumbnail_timestamp_seconds"): 1.23,
                    ("mixed", "thumbnail_timestamp_seconds"): 2.34,
                },
                config_path=config_path,
                reloader=lambda: "",
            )
            self.assertEqual(error, "")
            import tomllib

            parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["video"]["thumbnail_timestamp_seconds"], 1.23)
            self.assertEqual(parsed["mixed"]["thumbnail_timestamp_seconds"], 2.34)


class ThumbnailTimestampGuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_video_and_mixed_controls_are_configured_double_spin_boxes(self):
        panel = UploadPanel()
        try:
            for control in (panel.video_thumbnail_timestamp, panel.mixed_thumbnail_timestamp):
                self.assertIsInstance(control, QDoubleSpinBox)
                self.assertEqual(control.decimals(), 2)
                self.assertEqual(control.singleStep(), 0.01)
                self.assertEqual(control.minimum(), 0.0)
                self.assertEqual(control.suffix(), " 秒")
                self.assertEqual(control.value(), 1.0)
        finally:
            panel.deleteLater()

    def test_values_participate_in_dirty_save_and_revert(self):
        panel = UploadPanel()
        try:
            initial = panel.video_thumbnail_timestamp.value()
            panel.video_thumbnail_timestamp.setValue(1.23)
            self.assertTrue(panel.is_dirty())
            values = panel.collect_values()
            self.assertEqual(values[("video", "thumbnail_timestamp_seconds")], 1.23)

            panel.reset()
            self.assertEqual(panel.video_thumbnail_timestamp.value(), initial)
            self.assertFalse(panel.is_dirty())
        finally:
            panel.deleteLater()

    def test_reload_preserves_saved_value_and_disabled_control_keeps_value(self):
        panel = UploadPanel()
        original_get_cfg = upload_panel_module._cfg

        def get_cfg(name, default=None):
            if name == "VIDEO_THUMBNAIL_TIMESTAMP_SECONDS":
                return 1.23
            if name == "MIXED_THUMBNAIL_TIMESTAMP_SECONDS":
                return 2.34
            return original_get_cfg(name, default)

        try:
            with patch.object(upload_panel_module, "_cfg", side_effect=get_cfg):
                panel.load()
            self.assertEqual(panel.video_thumbnail_timestamp.value(), 1.23)
            self.assertEqual(panel.mixed_thumbnail_timestamp.value(), 2.34)

            panel.video_thumbnail.setChecked(False)
            self.assertFalse(panel.video_thumbnail_timestamp.isEnabled())
            panel.video_thumbnail_timestamp.setValue(3.45)
            self.assertEqual(panel.video_thumbnail_timestamp.value(), 3.45)
            panel.video_thumbnail.setChecked(True)
            self.assertTrue(panel.video_thumbnail_timestamp.isEnabled())
            self.assertEqual(panel.video_thumbnail_timestamp.value(), 3.45)

            panel.mixed_thumbnail.setChecked(False)
            self.assertFalse(panel.mixed_thumbnail_timestamp.isEnabled())
            panel.mixed_thumbnail.setChecked(True)
            self.assertTrue(panel.mixed_thumbnail_timestamp.isEnabled())
            self.assertEqual(panel.mixed_thumbnail_timestamp.value(), 2.34)
        finally:
            panel.deleteLater()


class ThumbnailTimestampProcessTest(unittest.TestCase):
    def _run_thumbnail(self, timestamp, *, duration=None, returncodes=(0,)):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "clip.mp4"
            source.write_bytes(b"not a real video; ffmpeg is mocked")
            cache = root / "thumbnails"
            commands = []
            results = iter(returncodes)

            def run(command, **_kwargs):
                commands.append(command)
                returncode = next(results)
                if returncode == 0:
                    Image.new("RGB", (64, 32), "red").save(Path(command[-1]))
                return subprocess.CompletedProcess(
                    command,
                    returncode,
                    "",
                    "mock ffmpeg failure" if returncode else "",
                )

            with patch.object(video_core, "THUMB_CACHE_DIR", cache), \
                    patch.object(video_core, "_FFMPEG_OVERRIDE", "ffmpeg"), \
                    patch.object(video_core, "run_cancellable_process", side_effect=run):
                result = video_core.build_thumbnail(
                    source,
                    timestamp_seconds=timestamp,
                    duration=duration,
                )
            return result, commands

    def test_ffmpeg_receives_two_decimal_timestamp(self):
        _result, commands = self._run_thumbnail(1.23, duration=10.0)
        self.assertEqual(commands[0][commands[0].index("-ss") + 1], "1.23")

    def test_zero_is_formatted_and_not_repeated_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "clip.mp4"
            source.write_bytes(b"video")
            commands = []

            def fail(command, **_kwargs):
                commands.append(command)
                return subprocess.CompletedProcess(command, 1, "", "failed")

            with patch.object(video_core, "THUMB_CACHE_DIR", root / "thumbnails"), \
                    patch.object(video_core, "_FFMPEG_OVERRIDE", "ffmpeg"), \
                    patch.object(video_core, "run_cancellable_process", side_effect=fail), \
                    self.assertRaises(RuntimeError):
                video_core.build_thumbnail(source, timestamp_seconds=0)
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0][commands[0].index("-ss") + 1], "0.00")

    def test_duration_overrun_falls_back_to_zero(self):
        _result, commands = self._run_thumbnail(12.34, duration=5.0)
        self.assertEqual(
            [command[command.index("-ss") + 1] for command in commands],
            ["0.00"],
        )

    def test_failed_requested_timestamp_retries_at_zero(self):
        _result, commands = self._run_thumbnail(1.23, duration=10.0, returncodes=(1, 0))
        self.assertEqual(
            [command[command.index("-ss") + 1] for command in commands],
            ["1.23", "0.00"],
        )

    def test_cache_key_is_unchanged_by_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "clip.mp4"
            source.write_bytes(b"video")
            cache = root / "thumbnails"
            calls = []

            def succeed(command, **_kwargs):
                calls.append(command)
                Image.new("RGB", (64, 32), "blue").save(Path(command[-1]))
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(video_core, "THUMB_CACHE_DIR", cache), \
                    patch.object(video_core, "_FFMPEG_OVERRIDE", "ffmpeg"), \
                    patch.object(video_core, "run_cancellable_process", side_effect=succeed):
                first = video_core.build_thumbnail(source, timestamp_seconds=1.0)
                second = video_core.build_thumbnail(source, timestamp_seconds=10.0)
            self.assertEqual(first[0], second[0])
            self.assertEqual(len(calls), 1)


class ThumbnailTimestampCallChainTest(unittest.TestCase):
    def test_input_video_passes_existing_duration_and_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mp4"
            source.write_bytes(b"video")
            readiness = SimpleNamespace(snapshot=SimpleNamespace(size=5, mtime_ns=7))
            info = {"width": 640, "height": 360, "duration": 12.34}
            thumb = Path(directory) / "thumb.jpg"
            with patch.object(video_core, "wait_for_file_ready", return_value=readiness), \
                    patch.object(video_core, "raise_for_file_readiness"), \
                    patch.object(video_core, "should_stage", return_value=False), \
                    patch.object(video_core, "video_info", return_value=info) as read_info, \
                    patch.object(video_core, "build_thumbnail", return_value=(thumb, 100, 50)) as build:
                content = video_core.input_video(
                    {"path": source},
                    "caption",
                    generate_thumbnail=True,
                    thumbnail_timestamp_seconds=1.23,
                )
            read_info.assert_called_once_with(source)
            build.assert_called_once_with(
                source,
                timestamp_seconds=1.23,
                duration=12.34,
            )
            self.assertEqual(content["start_timestamp"], 0)

    def test_prepare_video_reuses_duration_for_thumbnail_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mp4"
            source.write_bytes(b"video")
            info = {"width": 640, "height": 360, "duration": 7.5}
            with patch.object(video_core, "video_info", return_value=info), \
                    patch.object(video_core.cfg, "VIDEO_GENERATE_THUMBNAIL", True), \
                    patch.object(video_core, "build_thumbnail") as build:
                self.assertEqual(video_core.prepare_video(source), info)
            build.assert_called_once_with(source, duration=7.5)

    def test_legacy_generate_thumbnail_false_remains_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mp4"
            source.write_bytes(b"video")
            readiness = SimpleNamespace(snapshot=SimpleNamespace(size=5, mtime_ns=7))
            with patch.object(video_core, "wait_for_file_ready", return_value=readiness), \
                    patch.object(video_core, "raise_for_file_readiness"), \
                    patch.object(video_core, "should_stage", return_value=False), \
                    patch.object(
                        video_core,
                        "video_info",
                        return_value={"width": 640, "height": 360, "duration": 12.34},
                    ), \
                    patch.object(video_core, "build_thumbnail") as build:
                video_core.input_video({"path": source}, "caption", generate_thumbnail=False)
            build.assert_not_called()

    def test_mixed_passes_its_own_timestamp_to_shared_video_builder(self):
        item = {"path": Path("clip.mp4"), "media_kind": "video"}
        with patch.object(mixed_core.video_core, "input_video", return_value={"video": "ok"}) as builder, \
                patch.object(mixed_core.cfg, "MIXED_GENERATE_THUMBNAIL", True), \
                patch.object(mixed_core.cfg, "MIXED_THUMBNAIL_TIMESTAMP_SECONDS", 2.34):
            mixed_core._mixed_input_video(item, "caption")
        builder.assert_called_once_with(
            item,
            "caption",
            generate_thumbnail=True,
            thumbnail_timestamp_seconds=2.34,
        )


if __name__ == "__main__":
    unittest.main()
