# -*- coding: utf-8 -*-
"""Regression and architecture verification for the 1.9.3 Beta 3 GUI redesign."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QGroupBox,
    QProgressBar,
    QScrollArea,
    QTableWidget,
    QTreeWidget,
)

from tdlib_media_uploader.config.paths import read_version  # noqa: E402
from tdlib_media_uploader.gui import main_window as gui_app  # noqa: E402
from tdlib_media_uploader.gui.components import (  # noqa: E402
    ActionCard,
    NavigationSidebar,
    StatCard,
    StatusPill,
)
from tdlib_media_uploader.gui.dialogs import (  # noqa: E402
    CaptionEditDialog,
    ConfigDialog,
    ScanToolsDialog,
    TargetDialog,
)
from tdlib_media_uploader.gui.pages import (  # noqa: E402
    HistoryPage,
    HomePage,
    ImagePage,
    InflightPage,
    MixedPage,
    SettingsPage,
    TaskPage,
    UploadPage,
    VideoPage,
)
from tdlib_media_uploader.gui.theme import (  # noqa: E402
    APP_STYLE,
    DARK_PALETTE,
    LIGHT_PALETTE,
    THEME,
    Palette,
    build_stylesheet,
    get_current_theme_mode,
    set_theme_mode,
    toggle_theme,
)


class Beta3ThemeAndComponentsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_theme_palette_and_stylesheet_generation(self):
        palette = Palette()
        self.assertTrue(palette.bg_base.startswith("#"))
        self.assertTrue(palette.bg_card.startswith("#"))
        self.assertTrue(palette.accent.startswith("#"))
        self.assertTrue(palette.text_primary.startswith("#"))
        self.assertTrue(palette.success.startswith("#"))
        self.assertTrue(palette.danger.startswith("#"))

        css = build_stylesheet(palette)
        self.assertIn("QMainWindow", css)
        self.assertIn("QListWidget#sidebar", css)
        self.assertIn("QProgressBar", css)
        self.assertIn("QGroupBox", css)
        self.assertTrue(len(APP_STYLE) > 500)

    def test_light_dark_theme_toggle(self):
        # Ensure initial state is dark
        set_theme_mode("dark")
        self.assertEqual(get_current_theme_mode(), "dark")
        self.assertEqual(THEME.bg_base, DARK_PALETTE.bg_base)
        self.assertEqual(THEME.text_primary, DARK_PALETTE.text_primary)

        dark_css = build_stylesheet(THEME)
        self.assertIn(DARK_PALETTE.bg_base, dark_css)

        # Toggle to light
        new_mode = toggle_theme()
        self.assertEqual(new_mode, "light")
        self.assertEqual(get_current_theme_mode(), "light")
        self.assertEqual(THEME.bg_base, LIGHT_PALETTE.bg_base)
        self.assertEqual(THEME.text_primary, LIGHT_PALETTE.text_primary)

        light_css = build_stylesheet(THEME)
        self.assertIn(LIGHT_PALETTE.bg_base, light_css)
        self.assertNotEqual(dark_css, light_css)

        # Toggle back to dark
        back_mode = toggle_theme()
        self.assertEqual(back_mode, "dark")
        self.assertEqual(get_current_theme_mode(), "dark")
        self.assertEqual(THEME.bg_base, DARK_PALETTE.bg_base)

        # Test sidebar button integration
        sidebar = NavigationSidebar()
        try:
            self.assertEqual(sidebar.theme_btn.property("themeMode"), "dark")
            sidebar.theme_btn.click()
            self.assertEqual(sidebar.theme_btn.property("themeMode"), "light")
            self.assertEqual(get_current_theme_mode(), "light")
            sidebar.theme_btn.click()
            self.assertEqual(sidebar.theme_btn.property("themeMode"), "dark")
            self.assertEqual(get_current_theme_mode(), "dark")
        finally:
            sidebar.deleteLater()

    def test_status_pill_variants_and_text(self):
        pill = StatusPill("运行中", status="info")
        self.assertEqual(pill.text(), "运行中")
        self.assertEqual(pill.variant, "info")

        pill.set_status("success", "已完成")
        self.assertEqual(pill.text(), "已完成")
        self.assertEqual(pill.variant, "success")

        for var in ("success", "warning", "danger", "info", "neutral"):
            p = StatusPill("test", status=var)
            self.assertEqual(p.variant, var)
            p.deleteLater()
        pill.deleteLater()

    def test_stat_card_updates(self):
        card = StatCard("总文件数", "128", "个")
        self.assertEqual(card.value_label.text(), "128")
        self.assertEqual(card.title_label.text(), "总文件数")
        card.set_value("256")
        self.assertEqual(card.value_label.text(), "256")
        card.set_subtitle("已扫描")
        self.assertEqual(card.subtitle_label.text(), "已扫描")
        card.set_description("已扫描最新")
        self.assertEqual(card.subtitle_label.text(), "已扫描最新")
        card.deleteLater()

    def test_action_card_clicks(self):
        clicked_events: list[str] = []
        card = ActionCard("视频上传", "批量上传本地视频", "推荐")
        card.clicked.connect(lambda: clicked_events.append("clicked"))
        card.clicked.emit()
        self.assertEqual(clicked_events, ["clicked"])
        card.deleteLater()

    def test_navigation_sidebar_structure_and_status(self):
        sidebar = NavigationSidebar(version="1.9.3-beta3")
        self.assertEqual(sidebar.list.count(), 6)
        items = [sidebar.list.item(i).text() for i in range(sidebar.list.count())]
        self.assertEqual(
            items,
            ["概览", "媒体上传", "任务中心", "未确认上传", "历史记录", "设置与诊断"],
        )
        sidebar.setCurrentRow(2)
        self.assertEqual(sidebar.currentRow(), 2)

        sidebar.set_connection_status(True, "已连接")
        self.assertEqual(sidebar.status_text.text(), "已连接")
        sidebar.set_connection_status(False, "未连接")
        self.assertEqual(sidebar.status_text.text(), "未连接")
        sidebar.deleteLater()


class Beta3DialogsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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

    def test_config_and_scan_tools_dialog_structure(self):
        config_dlg = ConfigDialog()
        scan_dlg = ScanToolsDialog()
        try:
            self.assertTrue(config_dlg.findChildren(QScrollArea))
            self.assertTrue(scan_dlg.findChildren(QScrollArea))
            self.assertIn("api_id", config_dlg.fields)
            self.assertIn("api_hash", config_dlg.fields)
            self.assertIn("exiftool_path", scan_dlg.fields)

            scan_boxes = {b.title() for b in scan_dlg.findChildren(QGroupBox)}
            self.assertIn("外部工具", scan_boxes)
            self.assertIn("扫描稳定性与并发", scan_boxes)
        finally:
            config_dlg.close()
            config_dlg.deleteLater()
            scan_dlg.close()
            scan_dlg.deleteLater()


class Beta3PagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_home_page_signals_and_cards(self):
        home = HomePage(app_version="1.9.3-beta3")
        try:
            emitted = []
            home.start_upload.connect(lambda k: emitted.append(f"upload:{k}"))
            home.open_settings.connect(lambda: emitted.append("settings"))

            home.video_card.clicked.emit()
            self.assertIn("upload:video", emitted)

            home.image_card.clicked.emit()
            self.assertIn("upload:image", emitted)

            home.mixed_card.clicked.emit()
            self.assertIn("upload:mixed", emitted)

            self.assertFalse(hasattr(home, "settings_card"))
        finally:
            home.deleteLater()

    def test_task_page_progress_and_telemetry(self):
        task = TaskPage()
        try:
            self.assertIsInstance(task.progress, QProgressBar)
            task.metrics.setText("速度 3.5 MB/s · ETA 02:15")
            self.assertIn("3.5 MB/s", task.metrics.text())

            task.start_session("video", {"pending_files": 1, "pending_bytes": 100, "album_count": 1})
            signals = []
            task.safe_stop_requested.connect(lambda: signals.append("safe"))
            task.immediate_stop_requested.connect(lambda: signals.append("immediate"))
            task.stop_button.click()
            self.assertIn("safe", signals)
            task.immediate_stop_button.click()
            self.assertIn("immediate", signals)
        finally:
            task.deleteLater()

    def test_inflight_page_table_and_columns(self):
        inflight = InflightPage()
        try:
            tables = inflight.findChildren(QTableWidget)
            self.assertTrue(tables)
            table = tables[0]
            self.assertEqual(table.columnCount(), 9)
        finally:
            inflight.deleteLater()

    def test_history_page_table_and_filter(self):
        history = HistoryPage()
        try:
            tables = history.findChildren(QTableWidget)
            self.assertTrue(tables)
            self.assertTrue(hasattr(history, "search_edit"))
            self.assertTrue(hasattr(history, "kind_filter"))
            self.assertTrue(hasattr(history, "status_filter"))
        finally:
            history.deleteLater()

    def test_settings_page_scroll_and_diagnostics(self):
        settings = SettingsPage()
        try:
            boxes = {b.title(): b for b in settings.findChildren(QGroupBox)}
            self.assertIn("用户数据目录", boxes)
            self.assertIn("运行日志", boxes)
            self.assertTrue(hasattr(settings, "open_editor"))
            self.assertTrue(hasattr(settings, "open_scan_tools"))
        finally:
            settings.deleteLater()


class Beta3MainWindowAndArchitectureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_integration_and_version(self):
        window = gui_app.MainWindow()
        try:
            version = read_version()
            self.assertEqual(version, "1.9.3-beta4")
            self.assertIn("1.9.3-beta4", window.windowTitle())

            # Sidebar and stack synchronization
            self.assertEqual(window.sidebar.count(), 6)
            self.assertEqual(window.stack.count(), 6)
            for key, row in window.sidebar_rows.items():
                window.sidebar.setCurrentRow(row)
                self.assertEqual(window.stack.currentIndex(), row)

            # Connection status propagation
            window.nav_sidebar.set_connection_status(True, "测试已连接")
            self.assertEqual(window.nav_sidebar.status_text.text(), "测试已连接")
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_architecture_boundary_lower_layers_never_import_gui(self):
        for pkg in ("core", "processes", "telegram", "media", "upload", "config"):
            pkg_path = SRC_ROOT / "tdlib_media_uploader" / pkg
            if not pkg_path.is_dir():
                continue
            for py_file in pkg_path.rglob("*.py"):
                tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            self.assertNotIn(
                                "gui",
                                alias.name.split("."),
                                f"Forbidden GUI import in lower layer: {py_file} -> {alias.name}",
                            )
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        self.assertNotIn(
                            "gui",
                            node.module.split("."),
                            f"Forbidden GUI import in lower layer: {py_file} -> {node.module}",
                        )

    def test_architecture_boundary_gui_subpackages_never_reverse_import_main_window(self):
        gui_path = SRC_ROOT / "tdlib_media_uploader" / "gui"
        for subpkg in ("pages", "dialogs", "components"):
            pkg_dir = gui_path / subpkg
            if not pkg_dir.is_dir():
                continue
            for py_file in pkg_dir.rglob("*.py"):
                source = py_file.read_text(encoding="utf-8")
                self.assertNotIn(
                    "main_window",
                    source,
                    f"Forbidden reverse reference to main_window in {py_file}",
                )
        for single_file in ("tools.py", "cache_service.py", "config_service.py", "history_service.py"):
            target = gui_path / single_file
            if target.is_file():
                self.assertNotIn(
                    "main_window",
                    target.read_text(encoding="utf-8"),
                    f"Forbidden reverse reference to main_window in {target}",
                )

    def test_main_window_scanner_cancellation_on_close(self):
        from unittest.mock import MagicMock, patch
        from PySide6.QtGui import QCloseEvent
        window = gui_app.MainWindow()
        try:
            mock_scanner = MagicMock()
            mock_scanner.isRunning.return_value = True
            mock_scanner.wait.return_value = True
            window.scanners["video"] = mock_scanner

            window.close()
            mock_scanner.request_stop.assert_called_once()
            mock_scanner.wait.assert_called_once_with(5000)

            # Also verify timeout: if wait returns False, event is ignored and warning shown
            mock_scanner2 = MagicMock()
            mock_scanner2.isRunning.return_value = True
            mock_scanner2.wait.return_value = False
            window.scanners["video"] = mock_scanner2
            event = QCloseEvent()
            with patch.object(gui_app.QMessageBox, "warning") as mock_warn:
                window.closeEvent(event)
                self.assertFalse(event.isAccepted())
                mock_warn.assert_called_once()
        finally:
            window.deleteLater()
            self.app.processEvents()

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

    def test_spec_hiddenimports_contain_new_service_modules(self):
        spec_text = (PROJECT_ROOT / "tdlib_media_uploader.spec").read_text(encoding="utf-8")
        expected_modules = [
            "tdlib_media_uploader.gui.tools",
            "tdlib_media_uploader.gui.config_service",
            "tdlib_media_uploader.gui.history_service",
            "tdlib_media_uploader.gui.cache_service",
            "tdlib_media_uploader.gui.scanner",
        ]
        for mod in expected_modules:
            self.assertIn(f'"{mod}"', spec_text, f"Missing {mod} in tdlib_media_uploader.spec")


if __name__ == "__main__":
    unittest.main()
