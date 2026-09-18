from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication
except ModuleNotFoundError:  # pragma: no cover - dependency-gated CI skip
    QApplication = None
    GUI_AVAILABLE = False
else:
    GUI_AVAILABLE = True

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.core.upload_journal import InflightJournal  # noqa: E402

if GUI_AVAILABLE:
    from tdlib_media_uploader.gui.main_window import InflightPage  # noqa: E402
else:
    InflightPage = None


@unittest.skipUnless(GUI_AVAILABLE, "PySide6 unavailable")
class InflightGuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def _row_for(self, page, text):
        for row in range(page.table.rowCount()):
            if page.table.item(row, 1).text() == text:
                return row
        self.fail(f"row not found: {text}")

    def test_file_summary_paths_status_actions_and_corrupt_presentation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal_root = root / "upload_inflight"
            journal = InflightJournal(journal_root)
            target = {
                "target_mode": "forum_topic",
                "chat_id": -1001,
                "forum_topic_id": 7,
            }
            single = root / "照片 01.jpg"
            second = root / "照片 02.jpg"
            journal.prepare("image", "single", [{"path": single}], target=target)
            journal.unknown("image", "single", "timeout", target=target)
            journal.prepare("video", "multi", [{"path": single}, {"path": second}])
            journal.unknown("video", "multi", "connection lost")
            journal.prepare("image", "confirmed", [{"path": single}], target=target)
            journal.update("image", "confirmed", "CONFIRMED", message_ids=[17], target=target)
            (journal_root / "broken.json").write_text("{", encoding="utf-8")

            with patch("tdlib_media_uploader.core.upload_journal.APP_DATA_DIR", root):
                page = InflightPage()
                self.addCleanup(page.deleteLater)
                self.assertEqual(page.table.columnCount(), 9)
                self.assertGreaterEqual(page.table.rowCount(), 4)
                single_row = next(
                    row
                    for row in range(page.table.rowCount())
                    if page.table.item(row, 1).text() == "照片 01.jpg"
                    and page.table.item(row, 8).text() == "timeout"
                )
                multi_row = self._row_for(page, "照片 01.jpg 等 2 个文件")
                self.assertEqual(page.table.item(single_row, 6).text(), str(root))
                self.assertEqual(page.table.item(multi_row, 7).text(), "2")
                self.assertIn("群组", page.table.item(single_row, 5).text())
                self.assertIn("timeout", page.table.item(single_row, 8).text())

                page.table.selectRow(multi_row)
                self.application.processEvents()
                self.assertTrue(page.not_sent_button.isEnabled())
                self.assertIn("未确认", page.table.item(multi_row, 2).text())

                confirmed_row = self._row_for(page, "照片 01.jpg")
                # There are two single-file rows; locate the one carrying the
                # repair-only status instead of relying on sort tie order.
                for row in range(page.table.rowCount()):
                    if "Telegram 已确认" in page.table.item(row, 2).text():
                        confirmed_row = row
                        break
                page.table.selectRow(confirmed_row)
                self.application.processEvents()
                self.assertEqual(page.sent_button.text(), "修复本地断点")
                self.assertFalse(page.not_sent_button.isEnabled())

                details = page._detail_text(page.table.item(confirmed_row, 0).data(256))
                self.assertIn(str(single), details)
                self.assertIn("内部 Album ID", details)

                corrupt_row = next(
                    row
                    for row in range(page.table.rowCount())
                    if "损坏的上传记录" in page.table.item(row, 2).text()
                )
                corrupt_record = page.table.item(corrupt_row, 0).data(256)
                self.assertIn("broken.json", page._detail_text(corrupt_record))
                page.table.selectRow(corrupt_row)
                self.application.processEvents()
                self.assertFalse(page.sent_button.isEnabled())
                self.assertFalse(page.not_sent_button.isEnabled())

    def test_legacy_and_windows_path_fallbacks_are_human_readable(self):
        legacy = {"kind": "image", "album_key": "legacy", "status": "UNKNOWN", "items": []}
        self.assertEqual(InflightPage._summary(legacy), "旧版记录，缺少源文件信息")
        self.assertEqual(
            InflightPage._filename(r"\\server\share\照片 01.jpg"),
            "照片 01.jpg",
        )
        self.assertIn("缺少源文件信息", InflightPage._detail_text(legacy))


if __name__ == "__main__":
    unittest.main()
