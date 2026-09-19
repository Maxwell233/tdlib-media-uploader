# -*- coding: utf-8 -*-
"""Comprehensive verification tests for TDLib Media Uploader 1.9.3."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QDoubleSpinBox,
)

from tdlib_media_uploader.config.paths import read_version
from tdlib_media_uploader.gui.components.sidebar import NavigationSidebar
from tdlib_media_uploader.gui.components.telegram_target_editor import TelegramTargetEditor
from tdlib_media_uploader.gui.dialogs.target_dialog import TargetDialog
from tdlib_media_uploader.gui.dialogs.caption_dialog import CaptionEditDialog
from tdlib_media_uploader.gui.icons import get_svg_icon
from tdlib_media_uploader.gui.main_window import MainWindow
from tdlib_media_uploader.gui.pages.settings import SettingsPage
from tdlib_media_uploader.gui.pages.upload import UploadPage
from tdlib_media_uploader.gui.settings import (
    AdvancedPanel,
    EnvironmentLicensePanel,
    GeneralPanel,
    StoragePanel,
    TelegramPanel,
    UploadPanel,
)
from tdlib_media_uploader.gui.tools import detect_ffmpeg


class Beta5SettingsAndUXTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_version_is_stable(self):
        version = read_version()
        self.assertEqual(version, "1.9.3")

    def test_settings_page_six_inline_categories(self):
        page = SettingsPage()
        try:
            self.assertEqual(page.nav_list.count(), 6)
            categories = [page.nav_list.item(i).text() for i in range(6)]
            self.assertEqual(
                categories,
                ["常规", "Telegram", "上传参数", "高级选项", "存储与缓存", "环境与许可"],
            )

            # Check panels in stack
            self.assertIsInstance(page.stack.widget(0), GeneralPanel)
            self.assertIsInstance(page.stack.widget(1), TelegramPanel)
            self.assertIsInstance(page.stack.widget(2), UploadPanel)
            self.assertIsInstance(page.stack.widget(3), AdvancedPanel)
            self.assertIsInstance(page.stack.widget(4), StoragePanel)
            self.assertIsInstance(page.stack.widget(5), EnvironmentLicensePanel)

            # Test tab switching
            for i in range(6):
                page.nav_list.setCurrentRow(i)
                self.assertEqual(page.stack.currentIndex(), i)
        finally:
            page.deleteLater()

    def test_settings_dirty_tracking_and_revert(self):
        page = SettingsPage()
        try:
            self.assertFalse(page.btn_save.isEnabled())
            self.assertFalse(page.btn_revert.isEnabled())

            # Edit GeneralPanel input
            orig_video_dir = page.general_panel.video_dir.text()
            page.general_panel.video_dir.setText(orig_video_dir + "_modified")

            self.assertTrue(page.general_panel.is_dirty())
            self.assertTrue(page.btn_save.isEnabled())
            self.assertTrue(page.btn_revert.isEnabled())

            # Click revert
            page.revert_changes()
            self.assertEqual(page.general_panel.video_dir.text(), orig_video_dir)
            self.assertFalse(page.general_panel.is_dirty())
            self.assertFalse(page.btn_save.isEnabled())
            self.assertFalse(page.btn_revert.isEnabled())
            self.assertIn("已恢复", page.save_status_label.text())
        finally:
            page.deleteLater()

    def test_settings_validation_and_save_status(self):
        page = SettingsPage()
        try:
            # Set invalid API ID
            page.telegram_panel.api_id.setText("not_a_number")
            self.assertTrue(page.telegram_panel.is_dirty())

            # Attempt to save
            page.save_changes()
            self.assertIn("保存失败", page.save_status_label.text())
            self.assertEqual(page.save_status_label.property("status"), "danger")
        finally:
            page.deleteLater()

    def test_telegram_panel_sensitive_fields_mask_and_toggle(self):
        panel = TelegramPanel()
        try:
            # Check default echo mode is Password
            self.assertEqual(panel.api_hash.echoMode(), QLineEdit.EchoMode.Password)
            self.assertEqual(panel.proxy_password.echoMode(), QLineEdit.EchoMode.Password)
            self.assertEqual(panel.proxy_secret.echoMode(), QLineEdit.EchoMode.Password)

            # Click toggle on api_hash
            panel.toggle_hash_btn.setChecked(True)
            self.assertEqual(panel.api_hash.echoMode(), QLineEdit.EchoMode.Normal)
            self.assertEqual(panel.toggle_hash_btn.text(), "隐藏")

            panel.toggle_hash_btn.setChecked(False)
            self.assertEqual(panel.api_hash.echoMode(), QLineEdit.EchoMode.Password)
            self.assertEqual(panel.toggle_hash_btn.text(), "显示")

            # Direct vs proxy mode visibility
            panel.radio_direct.setChecked(True)
            self.assertTrue(panel.proxy_server.isHidden())

            panel.radio_proxy.setChecked(True)
            self.assertFalse(panel.proxy_server.isHidden())
        finally:
            panel.deleteLater()

    def test_upload_panel_segmented_switcher_and_target_editor(self):
        panel = UploadPanel()
        try:
            self.assertEqual(panel.stack.currentIndex(), 0)

            # Switch to image
            panel.btn_image.click()
            self.assertEqual(panel.stack.currentIndex(), 1)

            # Switch to mixed
            panel.btn_mixed.click()
            self.assertEqual(panel.stack.currentIndex(), 2)

            # Check TelegramTargetEditor integration
            target_editor = panel.video_target_editor
            self.assertIsInstance(target_editor, TelegramTargetEditor)

            forum_idx = target_editor.target_mode.findData("forum_topic")
            target_editor.target_mode.setCurrentIndex(forum_idx)
            self.assertFalse(target_editor.chat_id.isHidden())
            self.assertFalse(target_editor.topic_id.isHidden())
            self.assertTrue(target_editor.channel_chat_id.isHidden())

            chan_idx = target_editor.target_mode.findData("channel")
            target_editor.target_mode.setCurrentIndex(chan_idx)
            self.assertTrue(target_editor.chat_id.isHidden())
            self.assertTrue(target_editor.topic_id.isHidden())
            self.assertFalse(target_editor.channel_chat_id.isHidden())
        finally:
            panel.deleteLater()

    def test_advanced_panel_no_buttons_and_collapsibles(self):
        panel = AdvancedPanel()
        try:
            # Check all spin boxes have NoButtons
            all_spins = panel.findChildren(QSpinBox) + panel.findChildren(QDoubleSpinBox)
            self.assertTrue(len(all_spins) >= 10)
            for spin in all_spins:
                self.assertEqual(
                    spin.buttonSymbols(),
                    QSpinBox.ButtonSymbols.NoButtons,
                    f"SpinBox {spin} should have NoButtons",
                )

            # Empty ExifTool path is valid
            panel.exiftool_path.setText("")
            valid, msg = panel.validate()
            self.assertTrue(valid)
        finally:
            panel.deleteLater()

    def test_storage_panel_signals_and_cache(self):
        panel = StoragePanel()
        try:
            clear_all_received = []
            clear_thumb_received = []
            panel.clear_all_requested.connect(lambda: clear_all_received.append(True))
            panel.clear_thumb_requested.connect(lambda: clear_thumb_received.append(True))

            panel.clear_all_requested.emit()
            self.assertEqual(len(clear_all_received), 1)

            panel.clear_thumb_requested.emit()
            self.assertEqual(len(clear_thumb_received), 1)

            # Status label
            self.assertIn("缓存", panel.cache_status.text())
        finally:
            panel.deleteLater()

    def test_environment_license_panel_github_and_deps(self):
        panel = EnvironmentLicensePanel()
        try:
            self.assertIn("https://github.com/Maxwell233/tdlib-media-uploader", panel.repo_link.text())
            self.assertIsInstance(panel.open_github_btn, QPushButton)

            all_labels = [lbl.text() for lbl in panel.findChildren(QLabel)]
            self.assertTrue(any("GPL-3.0-only" in text for text in all_labels))

            # Check dependency keys exist
            for dep in ("PySide6", "tdjson", "Pillow", "imageio-ffmpeg", "ExifTool", "代理"):
                self.assertIn(dep, panel.env_labels)
        finally:
            panel.deleteLater()

    def test_target_dialog_compact_and_reusable_editor(self):
        dialog = TargetDialog("video")
        try:
            self.assertIsInstance(dialog.target_editor, TelegramTargetEditor)
            self.assertIn("修改视频上传目标", dialog.windowTitle())

            # Legacy attribute properties
            self.assertTrue(hasattr(dialog, "video_filename_numbers"))
            self.assertTrue(hasattr(dialog, "mixed_filename_numbers"))
            self.assertTrue(hasattr(dialog, "target_mode"))
            self.assertTrue(hasattr(dialog, "chat_id"))
            self.assertTrue(hasattr(dialog, "channel_chat_id"))
            self.assertTrue(hasattr(dialog, "topic_id"))
        finally:
            dialog.deleteLater()

    def test_upload_page_simplified_buttons_and_caption_edit(self):
        page = UploadPage("video")
        try:
            # Check button text
            all_buttons = [btn.text() for btn in page.findChildren(QPushButton)]
            self.assertIn("选择目录", all_buttons)
            self.assertIn("修改目标", all_buttons)
            self.assertIn("编辑视频上传参数", all_buttons)
            self.assertNotIn("浏览目录…", all_buttons)
            self.assertNotIn("配置视频目标…", all_buttons)

            # Caption edit button is restored and not hidden, hint label is removed
            self.assertFalse(page.edit_caption_button.isHidden())
            page.show()
            self.assertTrue(page.edit_caption_button.isVisible())
            self.assertEqual(page.edit_caption_button.text(), "编辑标题")
            self.assertIn("编辑所选媒体组标题", page.edit_caption_button.toolTip())
            self.assertFalse(page.edit_caption_button.isEnabled())
            all_labels = [lbl.text() for lbl in page.findChildren(QLabel)]
            self.assertNotIn("双击媒体组可编辑标题", all_labels)
            self.assertTrue(hasattr(page, "tree"))
            self.assertEqual(page.tree.contextMenuPolicy(), Qt.ContextMenuPolicy.CustomContextMenu)
        finally:
            page.deleteLater()

    def test_task_album_files_dark_and_light_styling(self):
        from tdlib_media_uploader.gui.pages.task import TaskPage
        from tdlib_media_uploader.gui.theme import build_stylesheet

        task_page = TaskPage()
        try:
            self.assertEqual(task_page.album_files.objectName(), "albumFilesList")
            self.assertTrue(task_page.album_files.alternatingRowColors())

            dark_qss = build_stylesheet("dark")
            self.assertIn("QListWidget#albumFilesList", dark_qss)
            self.assertIn("alternate-background-color: #121926", dark_qss)
            self.assertIn("QTreeWidget, QTableWidget, QListWidget", dark_qss)

            light_qss = build_stylesheet("light")
            self.assertIn("QListWidget#albumFilesList", light_qss)
            self.assertIn("alternate-background-color: #f8fafc", light_qss)
        finally:
            task_page.deleteLater()

    def test_upload_page_edit_parameters_shortcut(self):
        for kind, label in (("video", "视频"), ("image", "图片"), ("mixed", "混合")):
            page = UploadPage(kind)
            try:
                self.assertEqual(page.edit_params_button.text(), f"编辑{label}上传参数")
                emitted = []
                page.edit_parameters_requested.connect(emitted.append)
                page.edit_params_button.click()
                self.assertEqual(emitted, [kind])
            finally:
                page.deleteLater()

    def test_main_window_open_upload_parameters_navigation(self):
        window = MainWindow()
        try:
            # Video
            window._open_upload_parameters("video")
            self.assertEqual(window.sidebar.currentRow(), window.sidebar_rows["settings"])
            self.assertEqual(window.settings_page.nav_list.currentRow(), 2)
            self.assertEqual(window.settings_page.upload_panel.stack.currentIndex(), 0)
            self.assertTrue(window.settings_page.upload_panel.btn_video.isChecked())

            # Image
            window._open_upload_parameters("image")
            self.assertEqual(window.settings_page.nav_list.currentRow(), 2)
            self.assertEqual(window.settings_page.upload_panel.stack.currentIndex(), 1)
            self.assertTrue(window.settings_page.upload_panel.btn_image.isChecked())

            # Mixed
            window._open_upload_parameters("mixed")
            self.assertEqual(window.settings_page.nav_list.currentRow(), 2)
            self.assertEqual(window.settings_page.upload_panel.stack.currentIndex(), 2)
            self.assertTrue(window.settings_page.upload_panel.btn_mixed.isChecked())
        finally:
            window.close()
            window.deleteLater()

    def test_sidebar_brand_header_and_version(self):
        sidebar = NavigationSidebar(version="1.9.3")
        try:
            # Badge should not be visible in header
            self.assertFalse(sidebar.badge.isVisible())
            self.assertEqual(sidebar.version_label.text(), "Version 1.9.3")
        finally:
            sidebar.deleteLater()

    def test_svg_icons_availability(self):
        for icon_name in ("github", "sliders", "eye", "eye-off", "external_link"):
            icon = get_svg_icon(icon_name, 16, 16)
            self.assertFalse(icon.isNull(), f"Icon {icon_name} should not be null")

    def test_main_window_integration_stable(self):
        window = MainWindow()
        try:
            self.assertIn("1.9.3", window.windowTitle())
            self.assertEqual(window.nav_sidebar.version_label.text(), "Version 1.9.3")
            self.assertFalse(window.nav_sidebar.badge.isVisible())
            self.assertEqual(window.settings_page.nav_list.count(), 6)
        finally:
            window.close()
            window.deleteLater()


if __name__ == "__main__":
    unittest.main()
