# -*- coding: utf-8 -*-
"""Comprehensive regression test suite for Beta 4 Stage 2 GUI Redesign."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QGroupBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
)

from tdlib_media_uploader.gui.components import NavigationSidebar, StatusPill
from tdlib_media_uploader.gui.dialogs import ConfigDialog, ScanToolsDialog, TargetDialog
from tdlib_media_uploader.gui.icons import (
    _SVG_PATHS,
    build_svg_markup,
    get_svg_icon,
    get_svg_pixmap,
)
from tdlib_media_uploader.gui.main_window import MainWindow
from tdlib_media_uploader.gui.pages import (
    HistoryPage,
    InflightPage,
    SettingsPage,
    TaskPage,
    UploadHubPage,
    UploadPage,
)
from tdlib_media_uploader.gui.theme import (
    DARK_PALETTE,
    LIGHT_PALETTE,
    THEME,
    build_stylesheet,
    get_current_theme_mode,
    toggle_theme,
)
from tdlib_media_uploader.gui.tools import prepare_qt_plugins


class Beta4Stage2RedesignTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        prepare_qt_plugins()
        cls.app = QApplication.instance() or QApplication([])

    def test_settings_config_status_selector(self):
        settings = SettingsPage()
        try:
            self.assertEqual(settings.config_status.objectName(), "configStatus")
            # Verify status property updates
            for status in ("success", "warning", "danger"):
                settings.config_status.setProperty("status", status)
                self.assertEqual(settings.config_status.property("status"), status)
        finally:
            settings.deleteLater()

    def test_light_dark_semantic_status_qss(self):
        dark_qss = build_stylesheet("dark")
        light_qss = build_stylesheet("light")
        for qss in (dark_qss, light_qss):
            self.assertIn("QLabel#configStatus[status=\"success\"]", qss)
            self.assertIn("QLabel#configStatus[status=\"warning\"]", qss)
            self.assertIn("QLabel#configStatus[status=\"danger\"]", qss)
            self.assertIn("QLabel#statusPill[status=\"success\"]", qss)
            self.assertIn("QLabel#statusPill[status=\"warning\"]", qss)
            self.assertIn("QLabel#statusPill[status=\"danger\"]", qss)
            self.assertIn("QLabel#statusPill[status=\"info\"]", qss)
            self.assertIn("QLabel#statusPill[status=\"neutral\"]", qss)

    def test_qspinbox_no_buttons_and_qss_hide(self):
        scan_tools = ScanToolsDialog()
        config_dlg = ConfigDialog()
        target_dlg = TargetDialog()
        try:
            for spin in scan_tools.findChildren(QSpinBox) + scan_tools.findChildren(QDoubleSpinBox):
                self.assertEqual(
                    spin.buttonSymbols(),
                    QSpinBox.ButtonSymbols.NoButtons,
                    f"SpinBox {spin} in ScanToolsDialog should have NoButtons",
                )
            for spin in config_dlg.findChildren(QSpinBox) + config_dlg.findChildren(QDoubleSpinBox):
                self.assertEqual(
                    spin.buttonSymbols(),
                    QSpinBox.ButtonSymbols.NoButtons,
                    f"SpinBox {spin} in ConfigDialog should have NoButtons",
                )
            for spin in target_dlg.findChildren(QSpinBox) + target_dlg.findChildren(QDoubleSpinBox):
                self.assertEqual(
                    spin.buttonSymbols(),
                    QSpinBox.ButtonSymbols.NoButtons,
                    f"SpinBox {spin} in TargetDialog should have NoButtons",
                )
        finally:
            scan_tools.deleteLater()
            config_dlg.deleteLater()
            target_dlg.deleteLater()

        # Check QSS hides spinbox buttons
        qss = build_stylesheet("dark")
        self.assertIn("QSpinBox::up-button", qss)
        self.assertIn("width: 0px", qss)
        self.assertIn("QDoubleSpinBox::down-button", qss)

    def test_svg_vector_icon_generation_and_loading(self):
        icon_keys = [
            "video", "image", "mixed", "upload", "dashboard", "task",
            "inflight", "history", "settings", "folder", "search", "refresh",
            "delete", "sun", "moon", "telegram", "check", "expand", "collapse",
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

    def test_sidebar_new_navigation_mapping(self):
        sidebar = NavigationSidebar(version="1.9.3-beta4")
        try:
            self.assertEqual(sidebar.list.count(), 6)
            expected_labels = ["概览", "媒体上传", "任务中心", "未确认上传", "历史记录", "设置与诊断"]
            actual_labels = [sidebar.list.item(i).text() for i in range(sidebar.list.count())]
            self.assertEqual(actual_labels, expected_labels)

            # Check that each item has a non-null vector icon
            for i in range(sidebar.list.count()):
                item = sidebar.list.item(i)
                self.assertFalse(item.icon().isNull(), f"Item {item.text()} icon is null")
        finally:
            sidebar.deleteLater()

        window = MainWindow()
        try:
            self.assertEqual(window.sidebar.count(), 6)
            self.assertEqual(window.stack.count(), 6)
            self.assertEqual(window.sidebar_rows["dashboard"], 0)
            self.assertEqual(window.sidebar_rows["upload"], 1)
            self.assertEqual(window.sidebar_rows["video"], 1)
            self.assertEqual(window.sidebar_rows["image"], 1)
            self.assertEqual(window.sidebar_rows["mixed"], 1)
            self.assertEqual(window.sidebar_rows["task"], 2)
            self.assertEqual(window.sidebar_rows["inflight"], 3)
            self.assertEqual(window.sidebar_rows["history"], 4)
            self.assertEqual(window.sidebar_rows["settings"], 5)
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

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

            hub.set_current_kind("image")
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

    def test_settings_two_level_sub_navigation(self):
        settings = SettingsPage()
        try:
            self.assertEqual(settings.nav_list.count(), 7)
            categories = [settings.nav_list.item(i).text() for i in range(settings.nav_list.count())]
            self.assertIn("常规", categories)
            self.assertIn("Telegram", categories)
            self.assertIn("上传参数", categories)
            self.assertIn("扫描与工具", categories)
            self.assertIn("存储与缓存", categories)
            self.assertIn("环境诊断", categories)
            self.assertIn("关于与许可", categories)

            # Test switching category tabs
            for row in range(settings.nav_list.count()):
                settings.nav_list.setCurrentRow(row)
                self.assertEqual(settings.stack.currentIndex(), row)
        finally:
            settings.deleteLater()

    def test_history_uses_theme_semantic_colors(self):
        history_source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "tdlib_media_uploader"
            / "gui"
            / "pages"
            / "history.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("Qt.GlobalColor.green", history_source)
        self.assertNotIn("Qt.GlobalColor.yellow", history_source)
        self.assertIn("THEME.success", history_source)
        self.assertIn("THEME.warning", history_source)

    def test_theme_hot_reload_and_toggle(self):
        initial_mode = get_current_theme_mode()
        window = MainWindow()
        try:
            window.nav_sidebar._toggle_theme()
            new_mode = get_current_theme_mode()
            self.assertNotEqual(initial_mode, new_mode)
            self.assertEqual(window.nav_sidebar.theme_btn.property("themeMode"), new_mode)

            # Toggle back
            window.nav_sidebar._toggle_theme()
            self.assertEqual(get_current_theme_mode(), initial_mode)
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_minimum_window_layout_stability_860x560(self):
        window = MainWindow()
        try:
            window.resize(860, 560)
            window.show()
            self.app.processEvents()

            # Cycle through all pages to ensure no layout collapse or clipping errors
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

    def test_existing_upload_and_control_signals_preserved(self):
        window = MainWindow()
        try:
            # Check upload page signals
            for page in (window.video_page, window.image_page, window.mixed_page):
                self.assertTrue(hasattr(page, "scan_requested"))
                self.assertTrue(hasattr(page, "scan_cancel_requested"))
                self.assertTrue(hasattr(page, "start_requested"))
                self.assertTrue(hasattr(page, "path_selected"))
                self.assertTrue(hasattr(page, "edit_target_requested"))

            # Check task page signals
            self.assertTrue(hasattr(window.task_page, "safe_stop_requested"))
            self.assertTrue(hasattr(window.task_page, "immediate_stop_requested"))

            # Check inflight page signals
            self.assertTrue(hasattr(window.inflight_page, "reconciliation_requested"))

            # Check settings page signals
            self.assertTrue(hasattr(window.settings_page, "open_editor"))
            self.assertTrue(hasattr(window.settings_page, "open_scan_tools"))
            self.assertTrue(hasattr(window.settings_page, "clear_all_requested"))
            self.assertTrue(hasattr(window.settings_page, "clear_thumb_requested"))
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
