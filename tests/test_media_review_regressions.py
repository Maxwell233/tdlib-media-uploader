"""Focused regressions for media preparation, captions and release diagnostics."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tdlib_media_uploader.core import album, self_test
from tdlib_media_uploader.core.concurrency import CancellationRequested
from tdlib_media_uploader.core.source_snapshot import SourceChanged
from tdlib_media_uploader.media import video_pipeline, video_prepare, video_probe


def native_info():
    return video_probe.VideoMediaInfo(
        "mp4", "h264", "aac", 320, 240, 10.0, 25.0, True, "native",
    )


class VideoInfoCacheTests(unittest.TestCase):
    def test_lru_retains_recent_hits_and_evicts_oldest_metadata(self):
        with patch.object(video_pipeline, "_VIDEO_INFO_CACHE", OrderedDict()) as cache, \
                patch.object(video_pipeline, "_VIDEO_INFO_CACHE_MAX_ENTRIES", 2), \
                patch.object(video_pipeline, "file_snapshot", return_value=(1, 2)), \
                patch.object(video_pipeline, "probe_video", return_value=native_info()) as probe:
            for name in ("a.mp4", "b.mp4", "a.mp4", "c.mp4"):
                video_pipeline.video_info(Path(name))
            self.assertEqual([Path(key[0]).name for key in cache], ["a.mp4", "c.mp4"])
            self.assertEqual(probe.call_count, 3)
            video_pipeline.video_info(Path("b.mp4"))
            self.assertEqual(probe.call_count, 4)
            self.assertEqual(len(cache), 2)

    def test_parallel_readers_keep_cache_bounded_and_stat_changes_reprobe(self):
        with patch.object(video_pipeline, "_VIDEO_INFO_CACHE", OrderedDict()) as cache, \
                patch.object(video_pipeline, "_VIDEO_INFO_CACHE_MAX_ENTRIES", 7), \
                patch.object(video_pipeline, "file_snapshot", return_value=(1, 2)) as snapshot, \
                patch.object(video_pipeline, "probe_video", return_value=native_info()) as probe:
            with ThreadPoolExecutor(max_workers=8) as workers:
                results = list(workers.map(video_pipeline.video_info, [Path(f"{i}.mp4") for i in range(100)]))
            self.assertEqual(len(results), 100)
            self.assertLessEqual(len(cache), 7)
            video_pipeline.video_info(Path("changed.mp4"))
            count = probe.call_count
            snapshot.return_value = (1, 3)
            video_pipeline.video_info(Path("changed.mp4"))
            self.assertEqual(probe.call_count, count + 1)


class OptionalThumbnailTests(unittest.TestCase):
    def test_native_and_remux_videos_continue_without_optional_thumbnail(self):
        for remux in (False, True):
            with self.subTest(remux=remux), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / ("clip.ts" if remux else "clip.mp4")
                source.write_bytes(b"video")
                info = replace(native_info(), container="mpegts", compatibility="remux") if remux else native_info()

                def remux_video(_source, target, **_kwargs):
                    target.write_bytes(b"remuxed")
                    return target

                with patch.object(video_pipeline, "build_thumbnail", side_effect=RuntimeError("decoder failed")), \
                        patch.object(video_pipeline.UI, "warning") as warning, \
                        patch.object(video_prepare, "VIDEO_PROCESSED_CACHE_DIR", root / "cache"), \
                        patch.object(video_prepare, "remux_video_lossless", side_effect=remux_video), \
                        patch.object(video_prepare, "probe_video", return_value=native_info()):
                    prepared = video_prepare.prepare_video_for_telegram(source, info=info)
                self.assertTrue(prepared.upload_path.is_file())
                self.assertEqual(prepared.is_remuxed, remux)
                self.assertIsNone(prepared.thumbnail_path)
                self.assertEqual((prepared.thumbnail_width, prepared.thumbnail_height), (0, 0))
                warning.assert_called_once()
                self.assertIn("继续上传", warning.call_args.args[0])

    def test_cancellation_and_source_change_do_not_become_thumbnail_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mp4"
            source.write_bytes(b"video")
            marked_cancel = TimeoutError("cancelled")
            marked_cancel.cancelled = True
            for error in (marked_cancel, CancellationRequested("cancelled"), SourceChanged("changed")):
                with self.subTest(error=type(error)), \
                        patch.object(video_pipeline, "build_thumbnail", side_effect=error), \
                        patch.object(video_pipeline.UI, "warning") as warning:
                    with self.assertRaises(type(error)):
                        video_prepare.prepare_video_for_telegram(source, info=native_info())
                    warning.assert_not_called()
            event = threading.Event()

            def cancelled(*_args, **_kwargs):
                event.set()
                raise RuntimeError("extractor wrapped cancellation")

            with patch.object(video_pipeline, "build_thumbnail", side_effect=cancelled), \
                    patch.object(video_pipeline.UI, "warning") as warning:
                with self.assertRaises(CancellationRequested):
                    video_prepare.prepare_video_for_telegram(source, info=native_info(), cancel_event=event)
                warning.assert_not_called()

    def test_ordinary_thumbnail_timeout_uses_optional_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mp4"
            source.write_bytes(b"video")
            with patch.object(video_pipeline, "build_thumbnail", side_effect=TimeoutError("extraction timed out")), \
                    patch.object(video_pipeline.UI, "warning") as warning:
                prepared = video_prepare.prepare_video_for_telegram(source, info=native_info())
            self.assertIsNone(prepared.thumbnail_path)
            self.assertEqual(prepared.upload_path, source)
            warning.assert_called_once()

    def test_preflight_thumbnail_failure_keeps_video_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mp4"
            source.write_bytes(b"video")
            info = native_info()
            with patch.object(video_pipeline.cfg, "VIDEO_GENERATE_THUMBNAIL", True), \
                    patch.object(video_pipeline, "video_info", return_value=info), \
                    patch.object(video_pipeline, "build_thumbnail", side_effect=OSError("cache read-only")), \
                    patch.object(video_pipeline.UI, "warning") as warning:
                self.assertIs(video_pipeline.prepare_video(source), info)
            warning.assert_called_once()

    def test_source_mutation_during_failed_thumbnail_still_requires_rescan(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mp4"
            source.write_bytes(b"video")

            def changed(*_args, **_kwargs):
                source.write_bytes(b"different video")
                raise RuntimeError("thumbnail failed")

            with patch.object(video_pipeline, "build_thumbnail", side_effect=changed), \
                    patch.object(video_pipeline.UI, "warning"):
                with self.assertRaises(SourceChanged):
                    video_prepare.prepare_video_for_telegram(source, info=native_info())


class CaptionTruncationTests(unittest.TestCase):
    def test_user_title_is_preserved_at_and_near_limit(self):
        for title in ("x" * 30, "x" * 29, "x" * 28):
            value = album.with_filename_description(title, [Path("a-long-name.mp4")], True, max_chars=30)
            self.assertTrue(value.startswith(title))
            self.assertLessEqual(len(value), 30)
        self.assertEqual(album.with_filename_description("x" * 30, [Path("clip.mp4")], True, max_chars=30), "x" * 30)
        with self.assertRaises(album.CaptionLimitError):
            album.with_filename_description("x" * 31, [], True, max_chars=30)

    def test_omitted_filename_text_is_identified_when_room_allows(self):
        title = "用户标题"
        value = album.with_filename_description(title, [Path("long-filename-" * 10 + ".mp4")], True, max_chars=30)
        self.assertTrue(value.startswith(title + "\n"))
        self.assertIn("文件名已省略", value)
        self.assertEqual(album.filename_description([Path("clip.mp4")], max_chars=0), "")

    def test_caption_rejection_is_visible_and_does_not_save(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QDialog, QTreeWidgetItem
        from tdlib_media_uploader.gui.pages import upload

        app = QApplication.instance() or QApplication([])
        store = Mock()
        page = upload.UploadPage("video", services=upload.UploadPageServices(
            config_getter=lambda _key, default=None: default,
            target_getter=lambda _kind: {},
            caption_limit=10,
            caption_store_factory=lambda _kind: store,
        ))
        plan = {"key": "album", "caption": {"base_label": "old"}, "items": []}
        row = QTreeWidgetItem(page.tree, ["1", "old"])
        row.setData(0, Qt.ItemDataRole.UserRole, plan)
        dialog = SimpleNamespace(base_text="x" * 11, custom_text="", exec=lambda: QDialog.DialogCode.Accepted)
        try:
            with patch.object(upload, "CaptionEditDialog", return_value=dialog), \
                    patch.object(upload.QMessageBox, "warning") as warning:
                page._edit_album(row)
            warning.assert_called_once()
            self.assertIn("标题未保存", page.status_label.text())
            self.assertEqual(row.data(0, Qt.ItemDataRole.UserRole)["caption"]["base_label"], "old")
            store.set.assert_not_called()
        finally:
            page.close()
            page.deleteLater()
            app.processEvents()


class NumericSettingsVisibilityTests(unittest.TestCase):
    def test_numeric_editors_have_visible_text_in_both_fusion_themes(self):
        from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QLineEdit, QPushButton, QScrollArea, QSpinBox
        from tdlib_media_uploader.gui.settings import advanced_panel, general_panel
        from tdlib_media_uploader.gui.theme import build_stylesheet

        app = QApplication.instance() or QApplication([])
        old_style = app.style().objectName()
        old_stylesheet = app.styleSheet()
        app.setStyle("Fusion")
        try:
            for mode in ("light", "dark"):
                app.setStyleSheet(build_stylesheet(mode))
                for module, panel_class in ((general_panel, general_panel.GeneralPanel), (advanced_panel, advanced_panel.AdvancedPanel)):
                    with self.subTest(mode=mode, panel=panel_class.__name__), \
                            patch.object(module, "_cfg", side_effect=lambda _key, default=None: default):
                        panel = panel_class()
                        try:
                            panel.resize(720, 620)
                            for button in panel.findChildren(QPushButton):
                                if button.isCheckable():
                                    button.setChecked(True)
                            panel.show()
                            app.processEvents()
                            self.assertFalse(panel.grab().isNull())
                            spins = panel.findChildren(QSpinBox) + panel.findChildren(QDoubleSpinBox)
                            self.assertTrue(spins)
                            scroll = panel.findChild(QScrollArea)
                            for spin in spins:
                                scroll.ensureWidgetVisible(spin)
                                app.processEvents()
                                editor = spin.findChild(QLineEdit)
                                self.assertTrue(editor.isVisible())
                                self.assertGreater(editor.visibleRegion().boundingRect().width(), 20)
                                self.assertTrue(editor.text().strip())
                        finally:
                            panel.close()
                            panel.deleteLater()
                            app.processEvents()
        finally:
            app.setStyle(old_style)
            app.setStyleSheet(old_stylesheet)


class ReleaseMediaDiagnosticsTests(unittest.TestCase):
    def run_check(self, *, frozen, ffmpeg, ffprobe, process_error=None):
        from tdlib_media_uploader.telegram import tdlib_common
        from tdlib_media_uploader.processes import runner

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory).resolve()
            data = root / "data"
            data.mkdir()
            template = root / "default.toml"
            template.write_text("", encoding="utf-8")
            if ffmpeg and not Path(ffmpeg).is_absolute():
                ffmpeg = str(root / "resources" / ffmpeg)
            if ffprobe and not Path(ffprobe).is_absolute():
                ffprobe = str(root / "resources" / ffprobe)
            for name, value in {
                "IS_FROZEN": frozen, "DATA_DIR": data, "RESOURCE_DIR": root / "resources",
                "TDLIB_DATABASE_DIR": data / "telegram/database", "TDLIB_FILES_DIR": data / "telegram/files",
                "TEMPLATE_CONFIG_PATH": template, "ensure_data_dirs": lambda: None,
            }.items():
                stack.enter_context(patch.object(self_test, name, value))
            stack.enter_context(patch.object(tdlib_common, "build_tdlib_parameters", return_value={
                "database_directory": str(data / "telegram/database"), "files_directory": str(data / "telegram/files"),
            }))
            imports = stack.enter_context(patch.object(self_test.importlib, "import_module"))
            find_ffmpeg = stack.enter_context(patch.object(video_probe, "_find_ffmpeg", return_value=ffmpeg))
            find_ffprobe = stack.enter_context(patch.object(video_probe, "_find_ffprobe", return_value=ffprobe))
            process = stack.enter_context(patch.object(runner, "run_cancellable_process", side_effect=process_error))
            output = []
            result = self_test.run_self_test(emit=output.append)
            find_ffmpeg.assert_called_once()
            find_ffprobe.assert_called_once()
            imports.assert_any_call("PySide6.QtSvg")
            return result, output, process.call_args_list

    def test_source_check_reports_optional_degraded_media_tools(self):
        result, output, calls = self.run_check(frozen=False, ffmpeg=None, ffprobe=None)
        self.assertEqual(result, 0)
        self.assertFalse(calls)
        self.assertTrue(any("FFprobe" in line and "功能受限" in line for line in output))

    def test_packaged_check_fails_for_either_missing_tool(self):
        for ffmpeg, ffprobe in ((None, "/ffprobe"), ("/ffmpeg", None)):
            with self.subTest(ffmpeg=ffmpeg, ffprobe=ffprobe):
                result, output, _calls = self.run_check(frozen=True, ffmpeg=ffmpeg, ffprobe=ffprobe)
                self.assertEqual(result, 1)
                self.assertTrue(any("发布包缺少必需的" in line for line in output))

    def test_production_tool_paths_are_executed_and_failure_is_reported(self):
        ffmpeg = str(Path("chosen/ffmpeg").resolve())
        ffprobe = str(Path("chosen/ffprobe").resolve())
        result, output, calls = self.run_check(frozen=False, ffmpeg=ffmpeg, ffprobe=ffprobe)
        self.assertEqual(result, 0)
        self.assertEqual([call.args[0] for call in calls], [[ffmpeg, "-version"], [ffprobe, "-version"]])
        result, output, _calls = self.run_check(frozen=False, ffmpeg=ffmpeg, ffprobe=ffprobe, process_error=OSError("broken tool"))
        self.assertEqual(result, 1)
        self.assertTrue(any("FFprobe 检查失败" in line for line in output))

    def test_packaged_check_requires_bundled_tools_even_when_system_tools_exist(self):
        result, output, calls = self.run_check(frozen=True, ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
        self.assertEqual(result, 1)
        self.assertFalse(calls)
        self.assertEqual(sum("外部工具不能代替发布包文件" in line for line in output), 2)
        result, output, calls = self.run_check(frozen=True, ffmpeg="tools/ffmpeg", ffprobe="tools/ffprobe")
        self.assertEqual(result, 0, output)
        self.assertEqual(len(calls), 2)

    def test_default_template_header_matches_current_version(self):
        root = Path(__file__).resolve().parents[1]
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
        header = (root / "resources/default_config.toml").read_text(encoding="utf-8").splitlines()[:3]
        self.assertIn(f"# TDLib Media Uploader V{version}", header)

    def test_windows_ffmpeg_pin_matches_redistribution_notice(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "tools/ffmpeg-windows.json").read_text(encoding="utf-8"))
        notice = (root / "THIRD_PARTY_LICENSES.md").read_text(encoding="utf-8")
        for value in manifest.values():
            self.assertIn(value, notice)
        self.assertRegex(manifest["sha256"], r"^[0-9a-f]{64}$")
        workflow = (root / ".github/workflows/build-platforms.yml").read_text(encoding="utf-8")
        self.assertIn('Get-Content -Raw -LiteralPath "tools/ffmpeg-windows.json" | ConvertFrom-Json', workflow)
        for name in manifest:
            self.assertIn(f"$ffmpegManifest.{name}", workflow)
        spec = (root / "tdlib_media_uploader.spec").read_text(encoding="utf-8")
        self.assertIn('"PySide6.QtSvg"', spec)


if __name__ == "__main__":
    unittest.main()
