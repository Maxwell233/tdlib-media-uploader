"""Behavior contracts for the scan/preview responsibility extraction."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from tdlib_media_uploader.core.models import AlbumPlan, FileSnapshot, MediaItem, ScanResult
from tdlib_media_uploader.gui import models, scanner
from tdlib_media_uploader.gui.pages.upload import UploadPage, UploadPageServices
import tdlib_media_uploader.media as media


class _Captions:
    def __init__(self, _kind):
        pass

    def get(self, _key, default):
        return {'base_label': default, 'custom_text': ''}


class PreviewRefactorContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_legacy_scan_respects_injected_state_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for kind in ('video', 'image', 'mixed'):
                with self.subTest(kind=kind):
                    core = types.ModuleType(f'tdlib_media_uploader.media.legacy_{kind}')
                    observed = []
                    def state():
                        observed.append(core.STATE_DIR)
                        return SimpleNamespace(path=root / 'state.json')
                    core.UploadState = state
                    core.scan_videos = lambda **kwargs: []
                    core.build_items = lambda *args, **kwargs: ([], [])
                    core.scan_images = lambda **kwargs: []
                    core.scan_mixed = lambda *args, **kwargs: []
                    values = {f'{kind.upper()}_DIR': root, 'VIDEO_READ_DATES': False}
                    selected = root / f'{kind}-injected'
                    with patch.object(media, f'legacy_{kind}', core, create=True), \
                         patch.dict(sys.modules, {core.__name__: core}), \
                         patch.object(scanner, 'get_config', return_value=SimpleNamespace()), \
                         patch.object(scanner, 'get_cfg', side_effect=lambda name, default=None: values.get(name, default)), \
                         patch.object(scanner, 'target_for', return_value={}), \
                         patch.object(scanner, 'CaptionStore', _Captions), \
                         patch.object(scanner, f'{kind.upper()}_STATE_DIR', selected):
                        result = scanner.legacy_scan_result(kind)
                    self.assertEqual(observed, [selected])
                    self.assertEqual(result['groups'], [])
                    self.assertEqual(result['total_files'], 0)
                    self.assertTrue(result['core_available'])

    def test_image_groups_and_completion_use_full_snapshot_identity(self):
        root = Path('preview-root')
        path = root / 'same.jpg'
        first = MediaItem(path=path, source_root=root, media_kind='image',
                          snapshot=FileSnapshot(str(path), 10, 1))
        second = MediaItem(path=path, source_root=root, media_kind='image',
                           snapshot=FileSnapshot(str(path), 20, 2))
        plans = tuple(AlbumPlan(
            key=key, kind='image', source_root=root, group_label=key, number=number,
            items=(item,), pending_items=() if number == 1 else (item,), caption=key,
        ) for number, (key, item) in enumerate((('first', first), ('second', second)), 1))
        bundle = SimpleNamespace(kind='image', source_root=root, target={},
                                 state=SimpleNamespace(path=''), legacy=SimpleNamespace(),
                                 scan_result=ScanResult((first, second)), plans=plans, missing=())
        factories = []
        def captions(kind):
            factories.append(kind)
            return _Captions(kind)
        result = models.scan_result(bundle, logger=None, caption_store_factory=captions)
        self.assertEqual(factories, ['image'])
        self.assertEqual([group['pending'] for group in result['groups']], [0, 1])
        self.assertEqual([group['completed'] for group in result['groups']], [1, 0])
        self.assertEqual(result['completed_files'], 1)
        self.assertEqual(result['pending_files'], 1)
        self.assertEqual(result['total_bytes'], 30)
        self.assertEqual(result['pending_bytes'], 20)

    def _page_and_result(self, kind):
        services = UploadPageServices(config_getter=lambda name, default=None: default,
                                      target_getter=lambda _: {}, caption_store_factory=_Captions)
        page = UploadPage(kind, services=services)
        self.addCleanup(page.deleteLater)
        item = {'path': Path('preview.jpg'), 'scan_size': 5, 'scan_mtime_ns': 1}
        plan = {'key': 'album', 'number': 1, 'items': [item], 'pending_items': [item],
                'caption': {'text': 'Caption', 'base_label': 'Caption', 'custom_text': ''}}
        result = dict(kind=kind, completed_paths=[], total_files=1, total_bytes=5,
                      completed_files=0, pending_files=1, album_count=1, core_available=True,
                      groups=[dict(label='Group', items=[item], completed=0, pending=1,
                                   albums=1, album_plans=[plan])])
        return page, result, plan

    def test_preview_hierarchy_and_start_state(self):
        for kind in ('video', 'image', 'mixed'):
            for condition in ('ready', 'running', 'cancelled', 'no_core'):
                with self.subTest(kind=kind, condition=condition):
                    page, result, plan = self._page_and_result(kind)
                    page._running = condition == 'running'
                    result['cancelled'] = condition == 'cancelled'
                    result['core_available'] = condition != 'no_core'
                    page.set_result(result)
                    self.assertEqual(page.tree.topLevelItemCount(), 1)
                    top = page.tree.topLevelItem(0)
                    album = top if kind == 'image' else top.child(0)
                    self.assertEqual(album.data(0, Qt.ItemDataRole.UserRole), plan)
                    self.assertEqual(album.toolTip(1), 'Caption')
                    self.assertEqual(album.childCount(), 1)
                    self.assertIn('1 个', page.summary_label.text())
                    self.assertEqual(page.start_button.isEnabled(), condition == 'ready')
                    self.assertTrue(page.tree.updatesEnabled())

    def test_render_error_always_restores_tree_updates(self):
        for kind in ('video', 'image', 'mixed'):
            with self.subTest(kind=kind):
                page, result, _plan = self._page_and_result(kind)
                def failed_size(_value):
                    raise ValueError('formatting failed')
                page.services.size_formatter = failed_size
                with self.assertRaisesRegex(ValueError, 'formatting failed'):
                    page.set_result(result)
                self.assertTrue(page.tree.updatesEnabled())


if __name__ == '__main__':
    unittest.main()
