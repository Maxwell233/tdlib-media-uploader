from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication, QMessageBox
except ModuleNotFoundError:  # pragma: no cover - dependency-gated CI skip
    QApplication = None
    QMessageBox = None
    GUI_AVAILABLE = False
else:
    GUI_AVAILABLE = True

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.core.upload_journal import InflightJournal  # noqa: E402
from tdlib_media_uploader.core.filesystem_legacy import stable_path  # noqa: E402

if GUI_AVAILABLE:
    from tdlib_media_uploader.gui import main_window as gui  # noqa: E402
    InflightPage = gui.InflightPage
else:
    gui = None
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
            journal.prepare("image", "confirmed", [], target=target)
            path, record = journal.get_entry("image", "confirmed", target)
            record["items"] = journal._normalize_items([{"path": single}])
            journal._write(path, record)
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
                # Compare the project path identity, not platform-specific
                # spelling or case (Windows runners may use an 8.3 parent).
                self.assertEqual(stable_path(page.table.item(single_row, 6).text()), stable_path(root))
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
                detail_path = next(
                    line[3:]
                    for line in details.splitlines()
                    if line.startswith("1. ")
                )
                self.assertEqual(stable_path(detail_path), stable_path(single))
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

    def test_gui_inflight_page_loads_unresolved_records(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = InflightJournal(Path(directory) / "upload_inflight")
            journal.prepare("image", "gui-album", [{"path": "photo.jpg"}])
            journal.unknown("image", "gui-album", "timeout")
            with patch.object(gui, "APP_DATA_DIR", Path(directory)), \
                    patch("tdlib_media_uploader.core.upload_journal.APP_DATA_DIR", Path(directory)):
                page = InflightPage()
                self.addCleanup(page.deleteLater)
                self.assertEqual(page.table.rowCount(), 1)

    def test_gui_legacy_target_warning_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = InflightJournal(Path(directory) / "upload_inflight")
            journal.prepare("image", "legacy-gui-album", [{"path": "photo.jpg"}])
            journal.unknown("image", "legacy-gui-album", "timeout")
            with patch.object(gui, "APP_DATA_DIR", Path(directory)), \
                    patch("tdlib_media_uploader.core.upload_journal.APP_DATA_DIR", Path(directory)):
                page = InflightPage()
                self.addCleanup(page.deleteLater)
                self.assertEqual(page.table.item(0, 5).text(), "⚠ 旧版记录：目标未知")
                page.table.selectRow(0)
                with patch.object(
                    gui.QMessageBox,
                    "warning",
                    return_value=QMessageBox.StandardButton.No,
                ) as warning:
                    page._emit_choice(True)
                warning_text = warning.call_args.args[2]
                self.assertIn("旧版未记录 Telegram 目标", warning_text)
                self.assertIn("当前配置的 Telegram 目标", warning_text)
                self.assertIn("如果目标不一致，请不要确认已发送", warning_text)


if __name__ == "__main__":
    unittest.main()
