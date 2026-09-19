# -*- coding: utf-8 -*-
"""Comprehensive verification for the 1.9.3 Beta 4 GUI redesign and hardening."""

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

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from tdlib_media_uploader.config.paths import read_version  # noqa: E402
from tdlib_media_uploader.gui import config_service, main_window as gui_app  # noqa: E402
from tdlib_media_uploader.gui.components import NavigationSidebar  # noqa: E402
from tdlib_media_uploader.gui.components.sidebar import _format_badge  # noqa: E402
from tdlib_media_uploader.gui.main_window import MainWindow  # noqa: E402
from tdlib_media_uploader.gui.theme import (  # noqa: E402
    DARK_PALETTE,
    LIGHT_PALETTE,
    THEME,
    build_stylesheet,
    get_current_theme_mode,
    set_theme_mode,
    toggle_theme,
)
from tdlib_media_uploader.gui.workers import CacheStatsWorker  # noqa: E402


class Beta4VersionAndBadgeTest(unittest.TestCase):
    def test_version_bump_is_beta4(self):
        version = read_version()
        self.assertEqual(version, "1.9.3-beta4")

    def test_badge_formatting(self):
        self.assertEqual(_format_badge("1.9.3-beta4"), "Beta 4")
        self.assertEqual(_format_badge("1.9.3-beta.1"), "Beta 1")
        self.assertEqual(_format_badge("1.9.3-rc2"), "RC 2")
        self.assertEqual(_format_badge("1.9.3-alpha1"), "Alpha 1")
        self.assertEqual(_format_badge("1.9.3-dev0"), "DEV 0")
        self.assertEqual(_format_badge("1.9.3"), "Stable")
        self.assertEqual(_format_badge(""), "Beta 4")

    def test_sidebar_badge_display(self):
        app = QApplication.instance() or QApplication([])
        sidebar = NavigationSidebar(version="1.9.3-beta4")
        try:
            self.assertEqual(sidebar.badge.text(), "Beta 4")
            self.assertEqual(sidebar.version_label.text(), "Version 1.9.3-beta4")
        finally:
            sidebar.deleteLater()


class Beta4ThemeContrastAndTokensTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_selection_tokens_defined(self):
        self.assertTrue(hasattr(DARK_PALETTE, "text_selection"))
        self.assertTrue(hasattr(LIGHT_PALETTE, "text_selection"))
        self.assertEqual(DARK_PALETTE.text_selection, "#f8fafc")
        self.assertEqual(LIGHT_PALETTE.text_selection, "#0369a1")
        self.assertEqual(LIGHT_PALETTE.bg_selection, "#bae6fd")

    def test_stylesheet_contains_text_selection(self):
        dark_qss = build_stylesheet(DARK_PALETTE)
        light_qss = build_stylesheet(LIGHT_PALETTE)
        self.assertIn(DARK_PALETTE.text_selection, dark_qss)
        self.assertIn(LIGHT_PALETTE.text_selection, light_qss)
        self.assertIn(LIGHT_PALETTE.bg_selection, light_qss)

    def test_theme_hot_reload_broadcast(self):
        set_theme_mode("dark")
        window = MainWindow()
        try:
            self.assertEqual(get_current_theme_mode(), "dark")
            # Trigger toggle via nav_sidebar
            window.nav_sidebar.theme_btn.click()
            self.assertEqual(get_current_theme_mode(), "light")
            # Ensure stylesheet was updated on the application or window
            app = QApplication.instance()
            self.assertIn(LIGHT_PALETTE.bg_base, app.styleSheet())
            # Switch back
            window.nav_sidebar.theme_btn.click()
            self.assertEqual(get_current_theme_mode(), "dark")
            self.assertIn(DARK_PALETTE.bg_base, app.styleSheet())
        finally:
            window.close()
            window.deleteLater()


class Beta4AsyncCacheStatsWorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_cache_stats_worker_execution(self):
        worker = CacheStatsWorker()
        results: list[str] = []
        worker.finished.connect(results.append)

        # Run worker synchronously for testing
        worker.run()
        self.assertEqual(len(results), 1)
        self.assertTrue(isinstance(results[0], str))
        self.assertTrue(len(results[0]) > 0)


class Beta4DynamicConfigServiceTest(unittest.TestCase):
    def test_get_config_and_get_cfg(self):
        cfg = config_service.get_config()
        self.assertIsNotNone(cfg)
        self.assertEqual(config_service.get_cfg("TELEGRAM_UPLOAD_PART_SIZE_MAX"), getattr(cfg, "TELEGRAM_UPLOAD_PART_SIZE_MAX"))
        self.assertEqual(config_service.get_cfg("NON_EXISTENT_KEY_XYZ", "default_val"), "default_val")


class Beta4TelegramConnectionDecouplingTest(unittest.TestCase):
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


class Beta4ScannerCloseSafetyTest(unittest.TestCase):
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


class Beta4ZeroReverseDependenciesTest(unittest.TestCase):
    def test_no_reverse_imports_of_main_window(self):
        gui_dir = SRC_ROOT / "tdlib_media_uploader" / "gui"
        checked_dirs = [
            gui_dir / "components",
            gui_dir / "dialogs",
            gui_dir / "pages",
        ]
        checked_files = [
            gui_dir / "cache_service.py",
            gui_dir / "config_service.py",
            gui_dir / "history_service.py",
            gui_dir / "scanner.py",
            gui_dir / "tools.py",
            gui_dir / "workers.py",
            gui_dir / "events.py",
            gui_dir / "models.py",
            gui_dir / "theme.py",
        ]

        all_files: list[Path] = []
        for d in checked_dirs:
            all_files.extend(d.glob("*.py"))
        all_files.extend(checked_files)

        for py_file in all_files:
            content = py_file.read_text(encoding="utf-8")
            self.assertNotIn(
                "main_window",
                content,
                f"File {py_file} must not reference 'main_window' directly or indirectly",
            )


if __name__ == "__main__":
    unittest.main()
