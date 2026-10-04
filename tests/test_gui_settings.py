# -*- coding: utf-8 -*-
"""Comprehensive verification tests for TDLib Media Uploader 1.9.3."""

from __future__ import annotations

import unittest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton

from tdlib_media_uploader.gui.components.telegram_target_editor import TelegramTargetEditor
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


class SettingsAndUXTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

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

    def test_advanced_panel_allows_empty_exiftool_path(self):
        panel = AdvancedPanel()
        try:
            # Empty ExifTool path is valid
            panel.exiftool_path.setText("")
            valid, msg = panel.validate()
            self.assertTrue(valid)
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

if __name__ == "__main__":
    unittest.main()
