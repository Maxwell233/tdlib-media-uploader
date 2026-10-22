"""Durability, private storage, and visible configuration bootstrap failures."""

import importlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from tdlib_media_uploader.config import paths
from tdlib_media_uploader.core import persistence
from tdlib_media_uploader.core.upload_journal import InflightJournal, UNKNOWN
from tdlib_media_uploader.core.upload_state import UploadState
from tdlib_media_uploader.gui import config_service


class PrivatePersistenceTests(unittest.TestCase):
    def test_new_directory_entries_are_synced_from_parent_to_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "new" / "journal" / "checkpoint.json"
            with patch.object(persistence, "fsync_directory", wraps=persistence.fsync_directory) as sync:
                persistence.atomic_write_json(path, {"status": "PREPARED"})
            self.assertEqual([call.args[0] for call in sync.call_args_list],
                             [root, root / "new", path.parent])

    def test_atomic_write_syncs_contents_before_replace_and_directory_after(self):
        if os.name != "posix":
            self.skipTest("POSIX directory durability")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            events = []
            real_sync, real_replace = os.fsync, os.replace

            def sync(descriptor):
                events.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
                real_sync(descriptor)

            def replace(source, destination):
                events.append("replace")
                real_replace(source, destination)

            with patch.object(persistence.os, "fsync", side_effect=sync), \
                    patch.object(persistence.os, "replace", side_effect=replace):
                persistence.atomic_write_json(path, {"completed": [1]})
            self.assertEqual(events, ["file", "replace", "directory"])
            self.assertEqual(json.loads(path.read_text()), {"completed": [1]})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_file_sync_preserves_old_file_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            path.write_text("old")
            with patch.object(persistence.os, "fsync", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    persistence.atomic_write_json(path, {"completed": [1]})
            self.assertEqual(path.read_text(), "old")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_directory_sync_failure_cannot_promote_checkpoint_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = UploadState(kind="image", source_root=root, state_dir=root / "state")
            item = {"path": root / "image.jpg", "size": 1, "mtime_ns": 2}
            with patch.object(persistence, "fsync_directory", side_effect=OSError("sync failed")):
                with self.assertRaisesRegex(OSError, "sync failed"):
                    state.mark_album_completed([item], [42])
            self.assertFalse(state.is_completed(item))
            # replace can already be visible; the error still must reach the
            # delivery caller so it retains the corresponding journal.
            self.assertTrue(json.loads(state.path.read_text())["completed"])

    def test_journal_delete_syncs_directory_and_surfaces_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = InflightJournal(Path(directory) / "journal")
            journal.prepare("image", "album")
            path = journal.path_for("image", "album")
            with patch.object(persistence, "fsync_directory") as sync:
                self.assertTrue(journal.finalize("image", "album"))
            sync.assert_called_once_with(path.parent)
            journal.prepare("image", "album")
            with patch.object(persistence, "fsync_directory", side_effect=OSError("sync failed")):
                with self.assertRaisesRegex(OSError, "sync failed"):
                    journal.mark_not_sent("image", "album")
            self.assertIsNone(journal.get("image", "album"))

    def test_journal_persists_ordered_item_results_and_pending_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = InflightJournal(root / "journal")
            items = [{"path": root / "image.jpg", "size": 1, "mtime_ns": 2}]
            journal.prepare("image", "album", items)
            results = [{"index": 0, "message_id": 42, "status": "pending"}]
            journal.update("image", "album", UNKNOWN, item_results=results,
                           items=items, reconciled=True)
            record = InflightJournal(journal.root).get("image", "album")
            self.assertEqual(record["item_results"], results)
            self.assertEqual(record["items"][0]["size"], 1)
            self.assertTrue(record["reconciled"])

    def test_private_creation_does_not_change_existing_ancestors_or_source(self):
        if os.name != "posix":
            self.skipTest("POSIX permissions")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            source = root / "source"
            source.mkdir(mode=0o755)
            source_file = source / "photo.jpg"
            source_file.write_bytes(b"image")
            source_file.chmod(0o644)
            path = root / "data" / "state" / "checkpoint.json"
            persistence.atomic_write_json(path, {})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.parent.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(source_file.stat().st_mode), 0o644)

    def test_windows_directory_sync_uses_supported_file_flush_fallback(self):
        with patch.object(persistence.sys, "platform", "win32"), \
                patch.object(persistence.os, "open") as open_directory:
            persistence.fsync_directory(Path("unused"))
        open_directory.assert_not_called()


class ConfigBootstrapTests(unittest.TestCase):
    def test_config_import_never_creates_a_missing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "data" / "config.toml"
            try:
                with patch.object(paths, "CONFIG_PATH", missing), \
                        patch.object(persistence, "atomic_write_text", side_effect=OSError("denied")) as write:
                    importlib.reload(config_service)
                    self.assertFalse(config_service._CONFIG_CREATED)
                    self.assertIsNone(config_service.cfg)
                    write.assert_not_called()
                    error = config_service.initialize_config()
                    self.assertIn("denied", error)
                    self.assertIn(str(missing), error)
                    self.assertFalse(missing.exists())
            finally:
                importlib.reload(config_service)

    def test_config_creation_and_save_make_credentials_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "config.toml"
            with patch.object(config_service, "CONFIG_PATH", path):
                self.assertTrue(config_service.ensure_config_file())
            self.assertEqual(config_service.write_config_values(
                {("telegram", "api_hash"): "secret"}, config_path=path, reloader=lambda: ""
            ), "")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_bootstrap_creation_error_is_reported_in_gui_and_log(self):
        from PySide6.QtWidgets import QApplication
        from tdlib_media_uploader.gui import main_window

        app = QApplication.instance() or QApplication([])
        error = "无法初始化配置文件：data/config.toml\nPermissionError: denied"
        with patch.object(main_window.sys, "frozen", True, create=True), \
                patch.object(main_window.sys, "argv", ["uploader"]), \
                patch.object(main_window, "ensure_data_dirs"), \
                patch.object(config_service, "initialize_config", return_value=error), \
                patch.object(main_window.QMessageBox, "critical") as dialog, \
                patch.object(main_window, "write_app_log") as log, \
                patch.object(main_window, "MainWindow") as window:
            self.assertEqual(main_window.main(), 1)
        self.assertEqual(dialog.call_args.args[2], error)
        log.assert_called_once_with("ERROR", error, source="startup")
        window.assert_not_called()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
