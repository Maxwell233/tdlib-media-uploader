import unittest
from unittest.mock import patch

from tdlib_media_uploader.core import filesystem, sorting


class TestFilesystemEdgeCases(unittest.TestCase):
    def test_is_link_or_junction_exceptions(self):
        for error in (OSError, TypeError, ValueError):
            with self.subTest(error=error.__name__), patch.object(
                filesystem.os, 'lstat', side_effect=error('cannot inspect')
            ) as inspect:
                self.assertFalse(filesystem.is_link_or_junction('some_path'))
                inspect.assert_called_once()

    def test_relative_name_edge_cases(self):
        # Patch the platform's actual relpath function, not an unused alias.
        import os
        relpath_module = sorting.ntpath if os.name == 'nt' else sorting.os.path
        with patch.object(relpath_module, 'relpath', return_value=('..\\outside' if os.name == 'nt' else '../outside')):
            self.assertEqual(sorting.relative_name('/outside/path', '/root'), 'path')
        for error in (OSError, ValueError):
            with self.subTest(error=error.__name__), patch.object(
                relpath_module, 'relpath', side_effect=error('different roots')
            ) as relative:
                self.assertEqual(sorting.relative_name('/path', '/root'), 'path')
                relative.assert_called_once()


if __name__ == '__main__':
    unittest.main()
