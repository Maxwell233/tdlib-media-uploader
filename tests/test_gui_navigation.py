# -*- coding: utf-8 -*-
"""Navigation, theme switching, and scanner shutdown behavior."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication

from tdlib_media_uploader.gui import config_service
from tdlib_media_uploader.gui.components.sidebar import _format_badge  # noqa: E402
from tdlib_media_uploader.gui.main_window import MainWindow  # noqa: E402
from tdlib_media_uploader.gui.theme import DARK_PALETTE, LIGHT_PALETTE, get_current_theme_mode, set_theme_mode


class VersionBadgeTest(unittest.TestCase):
    def test_badge_formatting(self):
        self.assertEqual(_format_badge("1.9.3-beta4"), "Beta 4")
        self.assertEqual(_format_badge("1.9.3-beta.1"), "Beta 1")
        self.assertEqual(_format_badge("1.9.3-rc2"), "RC 2")
        self.assertEqual(_format_badge("1.9.3-alpha1"), "Alpha 1")
        self.assertEqual(_format_badge("1.9.3-dev0"), "DEV 0")
        self.assertEqual(_format_badge("1.9.3"), "Stable")
        self.assertEqual(_format_badge(""), "Beta 4")

class ThemeSwitchingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_theme_hot_reload_broadcast(self):
        original_mode = get_current_theme_mode()
        set_theme_mode("dark")
        window = MainWindow()
        try:
            self.assertEqual(get_current_theme_mode(), "dark")
            # Trigger toggle via nav_sidebar
            window.nav_sidebar.theme_btn.click()
            self.assertEqual(get_current_theme_mode(), "light")
            self.assertEqual(window.nav_sidebar.theme_btn.property("themeMode"), "light")
            # Ensure stylesheet was updated on the application or window
            app = QApplication.instance()
            self.assertIn(LIGHT_PALETTE.bg_base, app.styleSheet())
            # Switch back
            window.nav_sidebar.theme_btn.click()
            self.assertEqual(get_current_theme_mode(), "dark")
            self.assertIn(DARK_PALETTE.bg_base, app.styleSheet())
        finally:
            set_theme_mode(original_mode)
            window.close()
            window.deleteLater()


class DynamicConfigServiceTest(unittest.TestCase):
    def test_get_config_and_get_cfg(self):
        cfg = config_service.get_config()
        self.assertIsNotNone(cfg)
        self.assertEqual(config_service.get_cfg("TELEGRAM_UPLOAD_PART_SIZE_MAX"), getattr(cfg, "TELEGRAM_UPLOAD_PART_SIZE_MAX"))
        self.assertEqual(config_service.get_cfg("NON_EXISTENT_KEY_XYZ", "default_val"), "default_val")


class TelegramConnectionDecouplingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_connection_state_maintained_on_task_finish(self):
        window = MainWindow()
        try:
            self.assertFalse(window._telegram_connected)
            # Simulate target resolution
            window._target_from_worker({
                "kind": "video",
                "chat_id": 123456,
                "chat_title": "Test Channel",
                "target_mode": "channel",
            })
            self.assertTrue(window._telegram_connected)
            self.assertEqual(window.nav_sidebar.status_text.text(), "已连接")

            # Simulate task finish
            window._upload_finished("任务已完成", "success")
            # Connection indicator should still show connected
            self.assertTrue(window._telegram_connected)
            self.assertEqual(window.nav_sidebar.status_text.text(), "已连接")
            self.assertEqual(window.nav_sidebar.status_dot.property("connected"), "true")
        finally:
            window.close()
            window.deleteLater()


class ScannerCloseSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_close_scanner_graceful_stop(self):
        window = MainWindow()
        try:
            mock_scanner = MagicMock()
            mock_scanner.isRunning.return_value = True
            mock_scanner.wait.return_value = True
            window.scanners["video"] = mock_scanner

            event = QCloseEvent()
            window.closeEvent(event)
            mock_scanner.request_stop.assert_called_once()
            mock_scanner.wait.assert_called_once_with(5000)
            self.assertTrue(event.isAccepted())
        finally:
            window.scanners.clear()
            window.deleteLater()

    def test_close_scanner_timeout_ignores_event(self):
        window = MainWindow()
        try:
            mock_scanner = MagicMock()
            mock_scanner.isRunning.return_value = True
            mock_scanner.wait.return_value = False
            window.scanners["video"] = mock_scanner

            with patch("PySide6.QtWidgets.QMessageBox.warning") as mock_warn:
                event = QCloseEvent()
                window.closeEvent(event)
                mock_scanner.request_stop.assert_called_once()
                mock_scanner.wait.assert_called_once_with(5000)
                mock_warn.assert_called_once()
                self.assertFalse(event.isAccepted())
        finally:
            window.scanners.clear()
            window.deleteLater()


if __name__ == "__main__":
    unittest.main()
