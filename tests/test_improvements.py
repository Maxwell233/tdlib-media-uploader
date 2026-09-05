"""Offline regressions: no Telegram login or network requests."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import album_metadata as metadata
import gui_app as gui
import path_utils
from PySide6.QtWidgets import QApplication


class ImprovementsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_caption_store_reads_once_and_preserves_other_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(metadata, "PROJECT_DIR", Path(directory)):
                store = metadata.CaptionStore("image")
                store.path.write_text(json.dumps({"first": {"custom_text": "原有"}}), encoding="utf-8")
                with patch.object(store, "_load", wraps=store._load) as read:
                    for index in range(1000):
                        store.get(str(index), str(index))
                    self.assertEqual(read.call_count, 1)
                another = metadata.CaptionStore("image")
                another.set("second", base_label="2", custom_text="其他编辑")
                store.set("third", base_label="3", custom_text="本次编辑")
                saved = json.loads(store.path.read_text(encoding="utf-8"))
                self.assertEqual(set(saved), {"first", "second", "third"})
                self.assertEqual(store.get("third", "3")["custom_text"], "本次编辑")

    def test_invalid_config_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            original = '[paths]\nvideo_dir = "old"\n'
            config.write_text(original, encoding="utf-8")
            with patch.object(gui, "CONFIG_PATH", config), patch.object(gui, "_reload_config", side_effect=["空路径", ""]):
                self.assertEqual(gui._write_config_values({("paths", "video_dir"): ""}), "空路径")
            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_toml_update_preserves_other_sections(self):
        text = '[telegram]\napi_id = 42\n[telegram.image]\nchat_id = -1001\n[paths]\nimage_dir = "old"\n'
        updated = gui._update_toml_value(text, "paths", "image_dir", 'C:\\中文\\"quoted"')
        parsed = gui.tomllib.loads(updated)
        self.assertEqual(parsed["telegram"]["image"]["chat_id"], -1001)
        self.assertEqual(parsed["paths"]["image_dir"], 'C:\\中文\\"quoted"')

    def sample_result(self, directory):
        files = [Path(directory) / name for name in ("first.jpg", "second.jpg")]
        for file in files:
            file.write_bytes(b"image")
        plan = {"key": "abc", "number": 1, "items": files, "pending_items": files[1:], "caption": {"base_label": "1", "custom_text": "", "text": "1"}}
        return {"groups": [{"label": "Album 1", "items": files, "completed": 1, "pending": 1, "albums": 1, "album_plans": [plan]}], "completed_paths": [path_utils.stable_path(files[0])], "total_files": 2, "total_bytes": 10, "completed_files": 1, "pending_files": 1, "album_count": 1, "core_available": True}

    def test_image_preview_filter_and_scan_invalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            page = gui.UploadPage("image")
            page.set_result(self.sample_result(directory))
            self.assertEqual(page.tree.topLevelItemCount(), 1)
            album = page.tree.topLevelItem(0)
            self.assertEqual(album.childCount(), 2)  # No duplicate Album parent.
            page.pending_only.setChecked(True)
            self.assertTrue(album.child(0).isHidden())
            self.assertFalse(album.child(1).isHidden())
            page.search_edit.setText("first")
            page._filter_preview()
            self.assertTrue(album.isHidden())
            page.pending_only.setChecked(False)
            self.assertFalse(album.isHidden())
            page.tree.setCurrentItem(album.child(0))
            self.assertTrue(page.edit_caption_button.isEnabled())
            page.set_running(True)
            self.assertFalse(page.edit_caption_button.isEnabled())
            page.set_running(False)
            page.set_scanning(True)
            self.assertIsNone(page.result)
            self.assertFalse(page.start_button.isEnabled())
            page.deleteLater()

    def test_image_plan_preserves_complete_groups_and_custom_titles(self):
        import tdlib_image_album_uploader as core
        with tempfile.TemporaryDirectory() as directory:
            files = [Path(directory) / f"{i}.jpg" for i in range(23)]
            for file in files:
                file.write_bytes(b"image")
            class State:
                def is_completed(self, path):
                    return path in files[:10]
            with patch.object(metadata, "PROJECT_DIR", Path(directory)), patch.object(core.cfg, "IMAGE_ALBUM_SIZE", 10):
                plans = core.build_album_plans(files, State())
                self.assertEqual([len(p["items"]) for p in plans], [10, 10, 3])
                self.assertEqual([len(p["pending_items"]) for p in plans], [0, 10, 3])
                store = metadata.CaptionStore("image")
                store.set(plans[1]["key"], base_label="2", custom_text="旅行")
                again = core.build_album_plans(files, State())
                self.assertEqual(plans[1]["key"], again[1]["key"])
                self.assertIn("旅行", again[1]["caption"]["text"])

    def test_directory_scan_uses_one_stat_per_matching_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.jpg").write_bytes(b"image")
            (root / "empty.jpg").write_bytes(b"")
            (root / "other.txt").write_text("text")
            original = Path.stat
            calls = []
            def counted(path, *args, **kwargs):
                calls.append(str(path))
                return original(path, *args, **kwargs)
            with patch.object(Path, "stat", counted):
                files, errors = path_utils.iter_files(root, {".jpg"})
            self.assertEqual([p.name for p in files], ["a.jpg"])
            self.assertFalse(errors)
            self.assertEqual(len(calls), 2)

    def test_video_monthly_and_forced_grouping(self):
        import tdlib_video_album_uploader as core
        import datetime
        with tempfile.TemporaryDirectory() as directory:
            items = []
            for index in range(23):
                path = Path(directory) / f"{index}.mp4"
                path.write_bytes(b"video")
                month = "2025-01" if index < 12 else "2025-02"
                items.append({"path": path, "month_key": month, "capture_time": datetime.datetime(2025, 1 if index < 12 else 2, 1)})
            with patch.object(metadata, "PROJECT_DIR", Path(directory)), patch.object(core.cfg, "VIDEO_ALBUM_SIZE", 10), patch.object(core.cfg, "VIDEO_FORCE_TEN_PER_ALBUM", False):
                plans = core.build_album_plans(items)
                self.assertEqual([len(p["items"]) for p in plans], [10, 2, 10, 1])
                with patch.object(core.cfg, "VIDEO_FORCE_TEN_PER_ALBUM", True):
                    forced = core.build_album_plans(items)
                    self.assertEqual([len(p["items"]) for p in forced], [10, 10, 3])
                    self.assertEqual([p["caption"]["text"] for p in forced], ["Album 1", "Album 2", "Album 3"])

    def test_title_edit_keeps_tree_rows(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(metadata, "PROJECT_DIR", Path(directory)):
            page = gui.UploadPage("image")
            page.set_result(self.sample_result(directory))
            row = page.tree.topLevelItem(0)
            row.setExpanded(True)
            with patch.object(gui.QDialog, "exec", return_value=gui.QDialog.DialogCode.Accepted):
                page._edit_album(row)
            self.assertIs(page.tree.topLevelItem(0), row)
            self.assertTrue(row.isExpanded())
            self.assertTrue((Path(directory) / '.image_album_captions.json').exists())

    def test_invalid_api_id_is_not_silently_replaced(self):
        dialog = gui.ConfigDialog()
        dialog.fields["api_id"].setText("invalid")
        with patch.object(gui.QMessageBox, "warning") as warning, patch.object(gui, "_write_config_values") as save:
            dialog._save()
            warning.assert_called_once()
            save.assert_not_called()

    def test_video_scan_builds_all_months_once(self):
        import tdlib_video_album_uploader as core
        import datetime
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = []
            for month in (1, 2):
                path = root / f"{month}.mp4"
                path.write_bytes(b"video")
                items.append({"path": path, "month_key": f"2025-{month:02}", "capture_time": datetime.datetime(2025, month, 1)})
            class State:
                path = root / "state.json"
                def is_completed(self, path):
                    return False
            with patch.object(metadata, "PROJECT_DIR", root), patch.object(core, "scan_videos", return_value=[i["path"] for i in items]), patch.object(core, "build_items", return_value=(items, [])), patch.object(core, "UploadState", State), patch.object(core.cfg, "EXIFTOOL_PATH", root / "absent.exe"), patch.object(core.cfg, "VIDEO_FORCE_TEN_PER_ALBUM", False), patch.object(core, "build_album_plans", wraps=core.build_album_plans) as build:
                result = gui._scan_result("video")
                self.assertEqual(build.call_count, 1)
                self.assertEqual([g["label"] for g in result["groups"]], ["2025-01", "2025-02"])
                self.assertEqual(result["pending_files"], 2)
                self.assertEqual(result["album_count"], 2)


if __name__ == "__main__":
    unittest.main()
