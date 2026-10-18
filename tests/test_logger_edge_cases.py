import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from tdlib_media_uploader.core.logging import write_app_log


class TestLoggerEdgeCases(unittest.TestCase):
    def test_write_app_log_edge_cases(self):
        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td) / "logs"
            log_file = log_dir / "app.log"

            with (
                patch("tdlib_media_uploader.core.logging.LOG_DIR", log_dir),
                patch("tdlib_media_uploader.core.logging.APP_LOG_PATH", log_file),
            ):
                # empty
                write_app_log(level="info", message="")
                log_files = list(log_dir.glob("*.log"))
                self.assertEqual(len(log_files), 1)
                content = log_files[0].read_text(encoding="utf-8")
                self.assertIn(" [INFO] ", content)

                # multiline
                write_app_log(level="info", message="line1\nline2")
                content = log_files[0].read_text(encoding="utf-8")
                self.assertIn("line1\n", content)
                self.assertIn("line2", content)

                # null text
                write_app_log(level="info", message="null\x00text")
                content = log_files[0].read_text(encoding="utf-8")
                # null byte might be escaped by _lines function
                self.assertIn("null", content)
                self.assertIn("text", content)

                # label defaults
                write_app_log(level="", message="custom_label")
                content = log_files[0].read_text(encoding="utf-8")
                self.assertIn(" [INFO] ", content)

                # OSError handling during mkdir/open/write - handled silently
                with patch(
                    "pathlib.Path.mkdir",
                    side_effect=OSError("Mocked permission denied"),
                ):
                    write_app_log(level="info", message="should not raise")

                with patch(
                    "builtins.open", side_effect=OSError("Mocked file write error")
                ):
                    write_app_log(level="info", message="should not raise")


if __name__ == "__main__":
    unittest.main()
