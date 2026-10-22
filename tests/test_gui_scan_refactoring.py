"""Isolated contracts retained from the Jules preview characterization PR."""
import builtins
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tdlib_media_uploader.core.models import ScanResult
from tdlib_media_uploader.gui import models, scanner


class TestRefactoringBoundaries(unittest.TestCase):
    def test_scan_preserves_integration_import_error(self):
        real_import = builtins.__import__

        def import_without_integration(name, globals=None, locals=None, fromlist=(), level=0):
            if name == 'integration' and 'scan_v2' in (fromlist or ()):
                raise ImportError('bundled integration unavailable')
            return real_import(name, globals, locals, fromlist, level)

        with patch('builtins.__import__', side_effect=import_without_integration), \
             patch.object(scanner, 'get_config', return_value=SimpleNamespace()):
            with self.assertRaisesRegex(ImportError, 'bundled integration unavailable'):
                scanner.scan_result('image')

    def test_basic_mixed_preview_sorts_dictionary_items_by_source_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            group = root / 'album'
            group.mkdir()
            for name in ('photo10.jpg', 'photo2.jpg'):
                (group / name).write_bytes(b'photo')
            values = {
                'IMAGE_EXTENSIONS': {'.jpg'}, 'VIDEO_EXTENSIONS': {'.mp4'},
                'MIXED_SORT_MODE': 'name',
            }
            with patch.object(scanner, 'get_cfg', side_effect=lambda name, default=None: values.get(name, default)):
                groups, ignored, errors, warnings, skipped = scanner.basic_mixed_scan(root)
            self.assertEqual([item['path'].name for item in groups[0]['items']],
                             ['photo2.jpg', 'photo10.jpg'])
            self.assertEqual((ignored, errors, warnings, skipped), ([], [], [], []))

    def test_legacy_scan_result_uses_basic_preview_without_optional_dependency(self):
        real_import = builtins.__import__
        def import_without_image(name, globals=None, locals=None, fromlist=(), level=0):
            if 'image_pipeline' in (fromlist or ()):
                raise ImportError('optional image dependency unavailable')
            return real_import(name, globals, locals, fromlist, level)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('builtins.__import__', side_effect=import_without_image), \
                 patch.object(scanner, 'get_config', return_value=SimpleNamespace()), \
                 patch.object(scanner, 'get_cfg', side_effect=lambda name, default=None: root if name == 'IMAGE_DIR' else default), \
                 patch.object(scanner, 'target_for', return_value={}), \
                 patch.object(scanner, 'basic_paths', return_value=[]) as scan, \
                 patch.object(scanner, 'CaptionStore'):
                result = scanner.legacy_scan_result('image')
            scan.assert_called_once_with('image', cancel_event=None)
            self.assertEqual(result['kind'], 'image')
            self.assertEqual(result['source_dir'], str(root))
            self.assertEqual(result['groups'], [])
            self.assertEqual(result['items'], [])
            self.assertEqual(result['total_files'], 0)
            self.assertEqual(result['total_bytes'], 0)
            self.assertFalse(result['core_available'])
            self.assertIn('optional image dependency unavailable', result['warning'])

    def test_models_scan_result_preserves_empty_bundle_contract(self):
        root = Path('empty-preview-root')
        target = {'target_mode': 'channel', 'chat_id': 7}
        bundle = SimpleNamespace(
            kind='video', scan_result=ScanResult(()), plans=(), missing=(),
            source_root=root, state=None, legacy=SimpleNamespace(), target=target,
        )
        captions = Mock()
        result = models.scan_result(bundle, caption_store_factory=captions, logger=None)
        captions.assert_called_once_with('video')
        self.assertEqual(result['groups'], [])
        self.assertEqual(result['items'], [])
        self.assertEqual(result['plans'], ())
        for field in ('total_files', 'pending_files', 'completed_files',
                      'total_bytes', 'pending_bytes', 'album_count'):
            self.assertEqual(result[field], 0, field)
        self.assertEqual(result['source_dir'], str(root))
        self.assertEqual(result['target'], target)
        self.assertIsNot(result['target'], target)
        self.assertFalse(result['cancelled'])
        self.assertEqual(result['warning'], '')


if __name__ == '__main__':
    unittest.main()
