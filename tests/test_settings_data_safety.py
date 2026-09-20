"""Behavior regressions for configuration edits and local user data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication
from tdlib_media_uploader.core.album import CaptionStore
from tdlib_media_uploader.gui import cache_service
from tdlib_media_uploader.gui.pages.settings import SettingsPage
from tdlib_media_uploader.gui.workers import CacheStatsWorker
from tdlib_media_uploader.upload.staging import ensure_managed_staging_dir


class SettingsDataSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_busy_configuration_cannot_reach_persistence(self):
        page = SimpleNamespace(can_save=lambda: False, _set_status=Mock())
        with patch('tdlib_media_uploader.gui.pages.settings._write_config_values') as write:
            SettingsPage.save_changes(page)
        write.assert_not_called()
        self.assertIn('任务运行中', page._set_status.call_args.args[0])

    def test_dirty_panel_saves_only_fields_changed_since_loading(self):
        panel = Mock()
        panel.validate.return_value = (True, '')
        panel.is_dirty.return_value = True
        panel.collect_values.return_value = {'image.dir': '/old', 'image.album_size': 7}
        page = SimpleNamespace(can_save=lambda: True, panels=[panel],
            _baselines={panel: {'image.dir': '/old', 'image.album_size': 10}},
            _set_status=Mock(), _on_dirty_changed=Mock(), config_saved=Mock(), refresh=Mock())
        with patch('tdlib_media_uploader.gui.pages.settings._write_config_values', return_value=None) as write:
            SettingsPage.save_changes(page)
        # A directory changed elsewhere must not be overwritten by this stale panel.
        write.assert_called_once_with({'image.album_size': 7})

    def test_refresh_merges_external_values_into_a_dirty_panel(self):
        from tdlib_media_uploader.gui import config_service
        page = SettingsPage()
        self.addCleanup(page.deleteLater)
        panel = page.general_panel
        panel.video_dir.setText('/unsaved-video')
        with patch.object(config_service.cfg, 'IMAGE_DIR', Path('/new-image')):
            page.refresh()
            self.assertEqual(panel.image_dir.text(), '/new-image')
            self.assertEqual(panel.video_dir.text(), '/unsaved-video')
            self.assertTrue(panel.is_dirty())
            page.refresh()
            self.assertEqual(panel.video_dir.text(), '/unsaved-video')
        self.assertFalse(any(page._conflicts.values()))

    def test_conflicting_edits_cannot_silently_overwrite_another_page(self):
        from tdlib_media_uploader.gui import config_service
        page = SettingsPage()
        self.addCleanup(page.deleteLater)
        page.general_panel.image_dir.setText('/unsaved-image')
        with patch.object(config_service.cfg, 'IMAGE_DIR', Path('/new-image')):
            page.refresh()
            with patch('tdlib_media_uploader.gui.pages.settings._write_config_values') as write:
                page.save_changes()
            write.assert_not_called()
            self.assertIn('冲突', page.save_status_label.text())
            page.revert_changes()
            self.assertEqual(page.general_panel.image_dir.text(), '/new-image')
            self.assertFalse(page._conflicts)

    def test_main_window_refreshes_both_configuration_entry_points(self):
        from tdlib_media_uploader.gui import config_service
        from tdlib_media_uploader.gui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.deleteLater)
        with patch.object(config_service.cfg, 'IMAGE_DIR', Path('/synced-image')):
            window._on_settings_saved()
            self.assertIn('synced-image', window.image_page.source_edit.text())
            self.assertEqual(window.settings_page.general_panel.image_dir.text(), '/synced-image')
        target = {'target_mode': 'channel', 'chat_id': -100987, 'channel_chat_id': -100987}
        window.settings_page.upload_panel.image_album.setValue(3)
        with patch.object(config_service.cfg, 'target_for', return_value=target):
            window._refresh_pages()
            self.assertIn('-100987', window.image_page.chat_label.text())
            self.assertEqual(window.settings_page.upload_panel.image_target_editor.channel_chat_id.text(), '-100987')
            self.assertEqual(window.settings_page.upload_panel.image_album.value(), 3)

    def test_corrupt_or_unreadable_caption_file_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.json'
            path.write_text('{broken')
            store = CaptionStore('image'); store.path = path
            with self.assertRaises(RuntimeError):
                store.set('new', base_label='label', custom_text='title')
            self.assertEqual(path.read_text(), '{broken')
            path.write_text('{"existing": {"custom_text": "keep"}}')
            with patch.object(Path, 'read_text', side_effect=PermissionError('denied')):
                with self.assertRaises(RuntimeError):
                    store.set('new', base_label='label', custom_text='title')
            self.assertIn('keep', path.read_text())

    def test_gui_staging_cleanup_preserves_unmanaged_user_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'stage'
            ensure_managed_staging_dir(root)
            folder = root / 'personal'; folder.mkdir()
            user_file = folder / 'notes.txt'; user_file.write_text('keep')
            removed, errors = cache_service.clear_cache(['staging'], targets={'staging': ('stage', root)})
            self.assertFalse(errors)
            self.assertEqual(user_file.read_text(), 'keep')
            self.assertTrue((root / '.marker.json').exists())

    def test_cache_walk_and_worker_honor_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            stop = threading.Event(); stop.set()
            with patch.object(cache_service.os, 'scandir') as scan:
                self.assertEqual(cache_service.cache_usage(Path(directory), stop), (0, 0))
            scan.assert_not_called()
        worker = CacheStatsWorker(); worker.request_stop()
        results = []; worker.result_ready.connect(results.append)
        worker.run()
        self.assertEqual(results, ['统计已取消'])

    def test_close_waits_for_cache_worker_and_keeps_window_if_blocked(self):
        from tdlib_media_uploader.gui.main_window import MainWindow
        worker = Mock(); worker.isRunning.return_value = True
        page = SimpleNamespace(storage_panel=SimpleNamespace(_cache_worker=worker))
        window = SimpleNamespace(worker=None, scanners={}, settings_page=page, statusBar=Mock())
        for finished in (False, True):
            with self.subTest(finished=finished):
                worker.wait.return_value = finished
                event = Mock()
                MainWindow.closeEvent(window, event)
                worker.request_stop.assert_called()
                worker.wait.assert_called_with(1000)
                if finished:
                    event.accept.assert_called_once()
                else:
                    event.ignore.assert_called_once()
                    event.accept.assert_not_called()
