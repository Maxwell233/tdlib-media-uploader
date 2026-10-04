"""GUI navigation, page interactions, progress, and resource rendering."""
from __future__ import annotations

import os
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from tdlib_media_uploader.config.paths import read_version
from tdlib_media_uploader.gui import main_window as gui_app
from tdlib_media_uploader.gui.dialogs import CaptionEditDialog, TargetDialog
from tdlib_media_uploader.gui.icons import _SVG_PATHS, build_svg_markup, get_svg_icon, get_svg_pixmap
from tdlib_media_uploader.gui.main_window import MainWindow
from tdlib_media_uploader.gui.pages import HomePage, TaskPage, UploadHubPage, UploadPage, UploadPageServices
from tdlib_media_uploader.gui.tools import prepare_qt_plugins


class GuiPagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        prepare_qt_plugins()
        cls.app = QApplication.instance() or QApplication([])

    def test_caption_dialog_live_counter_and_extra_text(self):
        dialog = CaptionEditDialog(
            base_label="这是标题",
            custom_text="",
            caption_limit=4096,
        )
        try:
            self.assertEqual(dialog.base_edit.text(), "这是标题")
            self.assertIn("这是标题", dialog.preview.toPlainText())
            dialog.custom_edit.setPlainText("#Tag1 #Tag2")
            self.assertIn("#Tag1 #Tag2", dialog.preview.toPlainText())
            self.assertEqual(dialog.custom_text, "#Tag1 #Tag2")
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_target_dialog_channel_vs_topic_toggle(self):
        dialog = TargetDialog(kind="video")
        try:
            # Set to forum topic mode
            forum_idx = dialog.target_mode.findData("forum_topic")
            dialog.target_mode.setCurrentIndex(forum_idx)
            self.assertFalse(dialog.topic_id.isHidden())
            self.assertTrue(dialog.channel_chat_id.isHidden())

            # Switch to channel mode
            chan_idx = dialog.target_mode.findData("channel")
            dialog.target_mode.setCurrentIndex(chan_idx)
            self.assertTrue(dialog.topic_id.isHidden())
            self.assertFalse(dialog.channel_chat_id.isHidden())
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_home_page_signals_and_cards(self):
        home = HomePage(app_version="test")
        try:
            emitted = []
            home.start_upload.connect(lambda k: emitted.append(f"upload:{k}"))

            QTest.mouseClick(home.video_card, Qt.MouseButton.LeftButton)
            self.assertIn("upload:video", emitted)

            QTest.mouseClick(home.image_card, Qt.MouseButton.LeftButton)
            self.assertIn("upload:image", emitted)

            QTest.mouseClick(home.mixed_card, Qt.MouseButton.LeftButton)
            self.assertIn("upload:mixed", emitted)

        finally:
            home.deleteLater()

    def test_upload_finished_safely_handles_empty_active_kind(self):
        window = gui_app.MainWindow()
        try:
            window.active_kind = ""
            # Must not raise ValueError or crash when active_kind is empty
            window._upload_finished(False, "任务异常中止")
            self.assertEqual(window.statusBar().currentMessage(), "任务异常中止")
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_svg_vector_icon_generation_and_loading(self):
        icon_keys = [
            "video", "image", "mixed", "upload", "dashboard", "task",
            "inflight", "history", "settings", "folder", "search", "refresh",
            "delete", "sun", "moon", "telegram", "check", "expand", "collapse",
            "github", "sliders", "eye", "eye-off", "external_link",
        ]
        for key in icon_keys:
            self.assertIn(key, _SVG_PATHS)
            markup = build_svg_markup(key)
            self.assertIn("<svg", markup)
            self.assertIn("viewBox=\"0 0 24 24\"", markup)

            # Test QPixmap generation
            pixmap = get_svg_pixmap(key, size=24)
            self.assertIsInstance(pixmap, QPixmap)
            self.assertFalse(pixmap.isNull())
            self.assertEqual(pixmap.width(), 24)
            self.assertEqual(pixmap.height(), 24)

            # Test QIcon generation
            icon = get_svg_icon(key, size=18)
            self.assertIsInstance(icon, QIcon)
            self.assertFalse(icon.isNull())

    def test_upload_hub_page_segmented_switching(self):
        video_p = UploadPage("video")
        image_p = UploadPage("image")
        mixed_p = UploadPage("mixed")
        hub = UploadHubPage(video_p, image_p, mixed_p)
        try:
            self.assertEqual(hub.current_kind(), "video")
            self.assertEqual(hub.stack.currentIndex(), 0)
            self.assertTrue(hub.buttons["video"].isChecked())
            self.assertFalse(hub.buttons["image"].isChecked())

            hub.buttons["image"].click()
            self.assertEqual(hub.current_kind(), "image")
            self.assertEqual(hub.stack.currentIndex(), 1)
            self.assertTrue(hub.buttons["image"].isChecked())
            self.assertFalse(hub.buttons["video"].isChecked())

            hub.set_current_kind("mixed")
            self.assertEqual(hub.current_kind(), "mixed")
            self.assertEqual(hub.stack.currentIndex(), 2)
            self.assertTrue(hub.buttons["mixed"].isChecked())
        finally:
            hub.deleteLater()

    def test_main_window_version_media_routes_and_navigation(self):
        window = MainWindow()
        try:
            version = read_version()
            self.assertEqual(version, (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip())
            self.assertIn(version, window.windowTitle())
            self.assertEqual(window.nav_sidebar.version_label.text(), f"Version {version}")
            for kind in ("video", "image", "mixed"):
                page = getattr(window, f"{kind}_page")
                self.assertIsInstance(page, UploadPage)
                self.assertEqual(page.kind, kind)
                self.assertEqual(window.sidebar_rows[kind], window.sidebar_rows["upload"])
            window.resize(860, 560)
            window.show()
            self.app.processEvents()

            # Verify navigation and page visibility at the supported minimum size.
            for i in range(window.sidebar.count()):
                window.sidebar.setCurrentRow(i)
                self.app.processEvents()
                self.assertEqual(window.stack.currentIndex(), i)
                current_w = window.stack.currentWidget()
                self.assertIsNotNone(current_w)
                self.assertTrue(current_w.isVisible())
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_home_task_lifecycle_transitions(self):
        home = HomePage(app_version="test")
        try:
            # 1. Initial idle state
            self.assertEqual(home.task_value.text(), "无正在运行的上传任务")
            self.assertIn("在上方选择媒体类型", home.task_hint.text())

            # 2. Running state
            home.set_task_running("video", "待上传 8 个文件 · 1 个 Album")
            self.assertIn("视频上传中", home.task_value.text())
            self.assertIn("待上传 8 个文件", home.task_hint.text())

            # 3. Progress update
            home.set_task_progress(3, 8, "2.4 MB/s", "00:45")
            self.assertIn("3 / 8 个文件", home.task_hint.text())
            self.assertIn("2.4 MB/s", home.task_hint.text())
            self.assertIn("ETA 00:45", home.task_hint.text())

            # 4. Back to idle
            home.set_task_idle()
            self.assertEqual(home.task_value.text(), "无正在运行的上传任务")
            self.assertIn("在上方选择媒体类型", home.task_hint.text())
        finally:
            home.deleteLater()

    def test_home_page_keeps_scan_summary_contract(self):
        page = HomePage(app_version="test", size_formatter=lambda value: f"size={value}")
        page.update_scan({"total_files": 3, "total_bytes": 12})
        self.assertEqual(page.today_value.text(), "3 个文件 · size=12")
        page.clear_scan()
        self.assertEqual(page.today_value.text(), "—")
        page.deleteLater()

    def test_task_page_keeps_progress_and_stop_contracts(self):
        page = TaskPage(
            size_formatter=lambda value: f"size={value}",
            eta_formatter=lambda value: f"eta={value}",
        )
        page.start_session(
            "image",
            {"pending_files": 2, "pending_bytes": 20, "album_count": 1},
        )
        self.assertEqual(page.title.text(), "任务中心 · 图片")
        self.assertTrue(page.stop_button.isEnabled())
        page.show_progress(
            {
                "ratio": 0.5,
                "speed": 4,
                "eta": 7,
                "album_number": 1,
                "album_total": 1,
                "done_files": 1,
                "total_files": 2,
                "done_bytes": 10,
                "total_bytes": 20,
            }
        )
        signals = []
        page.safe_stop_requested.connect(lambda: signals.append("safe"))
        page.immediate_stop_requested.connect(lambda: signals.append("immediate"))
        page.stop_button.click()
        page.immediate_stop_button.click()
        self.assertEqual(signals, ["safe", "immediate"])
        self.assertEqual(page.progress.value(), 500)
        self.assertIn("eta=7", page.metrics.text())
        page.finish_session(False, "任务失败")
        self.assertFalse(page.stop_button.isEnabled())
        page.deleteLater()

    def test_package_page_accepts_explicit_services_for_headless_contracts(self):
        services = UploadPageServices(
            config_getter=lambda _name, default=None: default,
            target_getter=lambda _kind: {"target_mode": "channel", "chat_id": 7},
            project_dir=Path("/tmp/package-upload-page"),
        )
        page = UploadPage("mixed", services=services)
        try:
            self.assertEqual(page.kind, "mixed")
            self.assertEqual(page.chat_label.text(), "频道 · 7")
            self.assertEqual(page.topic_label.text(), "不适用（频道不使用 Topic）")
        finally:
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
