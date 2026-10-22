"""One-time, media-scoped checkpoint resets never discard unresolved sends."""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QMessageBox

from tdlib_media_uploader.core.upload_journal import InflightJournal
from tdlib_media_uploader.core.upload_state import UploadState
from tdlib_media_uploader.gui import main_window as gui
from tdlib_media_uploader.gui.pages.upload import UploadPage
from tdlib_media_uploader.media import image_pipeline, mixed_pipeline, video_pipeline


TARGET = {"target_mode": "channel", "chat_id": -1001, "channel_chat_id": -1001}


class OneTimeResetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.item = {"path": self.root / "photo.jpg", "size": 4, "mtime_ns": 1}
        self.pages = {kind: Mock() for kind in ("video", "image", "mixed")}
        self.status = Mock()
        self.window = SimpleNamespace(
            _cache_clear_worker=None,
            worker=None,
            scanners={},
            _upload_page=lambda kind: self.pages[kind],
            home=Mock(),
            statusBar=lambda: self.status,
        )
        self.targets = {
            f"{kind}_state": (kind, self.root / "state" / kind)
            for kind in self.pages
        }

    def _state(self, kind, *, source=None, target=None):
        return UploadState(kind=kind, source_root=source or self.root,
                           target=target or TARGET, state_dir=self.targets[f"{kind}_state"][1])

    def test_legacy_true_never_resets_state_when_scan_and_upload_reopen_it(self):
        for kind, pipeline in (("image", image_pipeline), ("video", video_pipeline),
                               ("mixed", mixed_pipeline)):
            with self.subTest(kind=kind):
                config = SimpleNamespace(**{
                    f"{kind.upper()}_DIR": self.root,
                    f"{kind.upper()}_RESET_STATE": True,
                    "target_for": lambda _kind: TARGET,
                })
                with patch.object(pipeline, "cfg", config), \
                        patch.object(pipeline, "STATE_DIR", self.targets[f"{kind}_state"][1]):
                    state = pipeline.UploadState()
                    state.mark_album_completed([self.item], [10])
                    scan_state = pipeline.UploadState()
                    self.assertTrue(scan_state.is_completed(self.item))
                    next_item = {**self.item, "path": self.root / "next.jpg"}
                    scan_state.mark_album_completed([next_item], [11])
                    upload_state = pipeline.UploadState()
                    self.assertTrue(upload_state.is_completed(self.item))
                    self.assertTrue(upload_state.is_completed(next_item))

    def test_confirmed_reset_only_clears_selected_kind_and_preserves_unknown_guard(self):
        image_states = [self._state("image"), self._state("image", source=self.root / "other",
                        target={"target_mode": "channel", "chat_id": -2002})]
        other_states = [self._state("video"), self._state("mixed")]
        for state in image_states + other_states:
            state.mark_album_completed([self.item], [10])
        other_bytes = {state.path: state.path.read_bytes() for state in other_states}
        journal = InflightJournal(self.root / "upload_inflight")
        journal.prepare("image", "unknown-album", [self.item], target=TARGET)
        journal.unknown("image", "unknown-album", "timeout", target=TARGET)
        journal_path, _record = journal.get_entry("image", "unknown-album", TARGET)
        journal_bytes = journal_path.read_bytes()
        self.targets["upload_inflight"] = ("unresolved", journal.root)

        with patch.object(gui, "current_cache_targets", return_value=self.targets), \
                patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes) as confirm:
            gui.MainWindow._reset_upload_state(self.window, "image")
        self.assertEqual(confirm.call_count, 1)
        self.assertIn("重复消息", confirm.call_args.args[2])
        self.assertIn("未确认上传", confirm.call_args.args[2])
        self.assertEqual(confirm.call_args.args[-1], QMessageBox.StandardButton.No)
        self.assertTrue(all(not state.path.exists() for state in image_states))
        for path, contents in other_bytes.items():
            self.assertEqual(path.read_bytes(), contents)
        self.assertEqual(journal_path.read_bytes(), journal_bytes)
        self.assertIsNotNone(InflightJournal(journal.root).unresolved_for_items(
            "image", [self.item], target=TARGET))
        self.pages["image"].clear_scan_result.assert_called_once()
        self.pages["video"].clear_scan_result.assert_not_called()
        self.pages["mixed"].clear_scan_result.assert_not_called()

        # A newly saved checkpoint persists normally after the one-time reset.
        new_state = self._state("image")
        self.assertFalse(new_state.is_completed(self.item))
        new_state.mark_album_completed([self.item], [20])
        self.assertTrue(self._state("image").is_completed(self.item))

    def test_declining_reset_preserves_checkpoint(self):
        state = self._state("image")
        state.mark_album_completed([self.item], [10])
        original = state.path.read_bytes()
        with patch.object(gui, "current_cache_targets", return_value=self.targets), \
                patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.No):
            gui.MainWindow._reset_upload_state(self.window, "image")
        self.assertEqual(state.path.read_bytes(), original)
        self.pages["image"].clear_scan_result.assert_not_called()

    def test_running_upload_scan_or_cleanup_prevents_reset(self):
        running = Mock()
        running.isRunning.return_value = True
        for worker, scanners, cleanup in ((running, {}, None), (None, {"video": running}, None),
                                           (None, {}, running)):
            with self.subTest(upload=worker is not None, scan=bool(scanners), cleanup=cleanup is not None):
                self.window.worker = worker
                self.window.scanners = scanners
                self.window._cache_clear_worker = cleanup
                with patch.object(gui, "clear_cache") as clear, \
                        patch.object(QMessageBox, "warning") as warning:
                    gui.MainWindow._reset_upload_state(self.window, "image")
                clear.assert_not_called()
                self.assertTrue(warning.call_args is None or len(warning.call_args.args) == 3)

    def test_upload_page_routes_reset_to_its_kind_and_disables_it_while_busy(self):
        for kind in self.pages:
            with self.subTest(kind=kind):
                page = UploadPage(kind)
                self.addCleanup(page.deleteLater)
                requested = []
                page.reset_state_requested.connect(requested.append)
                page.reset_state_button.click()
                self.assertEqual(requested, [kind])
                page.set_scanning(True)
                self.assertFalse(page.reset_state_button.isEnabled())
                page.set_scanning(False)
                page.set_running(True)
                self.assertFalse(page.reset_state_button.isEnabled())
                page.set_running(False)
                self.assertTrue(page.reset_state_button.isEnabled())
