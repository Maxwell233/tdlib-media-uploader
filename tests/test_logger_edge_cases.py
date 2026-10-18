import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tdlib_media_uploader.core import logging as app_logging


class TestLoggerEdgeCases(unittest.TestCase):
    def test_write_app_log_edge_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / 'logs'
            log_file = log_dir / 'app.log'
            with patch.object(app_logging, 'LOG_DIR', log_dir), patch.object(
                app_logging, 'APP_LOG_PATH', log_file
            ):
                app_logging.write_app_log('', '', source=' ')
                self.assertIn(' [INFO] [app] \n', log_file.read_text())
                app_logging.write_app_log(' warning ', 'line1\nline2', source=' test ')
                lines = log_file.read_text().splitlines()
                self.assertTrue(lines[-2].endswith(' [WARNING] [test] line1'))
                self.assertTrue(lines[-1].endswith(' [WARNING] [test] line2'))
                app_logging.write_app_log('INFO', 'null\x00text')
                content = log_file.read_text()
                self.assertIn(r'null\0text', content)
                self.assertNotIn('\x00', content)

    def test_mkdir_open_and_write_errors_do_not_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / 'logs'
            log_file = log_dir / 'app.log'
            with patch.object(app_logging, 'LOG_DIR', log_dir), patch.object(
                app_logging, 'APP_LOG_PATH', log_file
            ):
                with patch.object(Path, 'mkdir', side_effect=OSError('mkdir failed')) as mkdir:
                    app_logging.write_app_log('INFO', 'event')
                mkdir.assert_called_once()
                with patch.object(Path, 'open', side_effect=OSError('open failed')) as opened:
                    app_logging.write_app_log('INFO', 'event')
                opened.assert_called_once_with('a', encoding='utf-8', newline='\n')
                stream = MagicMock()
                stream.write.side_effect = OSError('write failed')
                manager = MagicMock()
                manager.__enter__.return_value = stream
                with patch.object(Path, 'open', return_value=manager):
                    app_logging.write_app_log('INFO', 'event')
                stream.write.assert_called_once()
                manager.__exit__.assert_called_once()


if __name__ == '__main__':
    unittest.main()
