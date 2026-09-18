"""Focused V1.9 regression tests kept separate from the legacy suite."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.core import album as album_metadata
from tdlib_media_uploader.config import loader as app_config
from tdlib_media_uploader.gui import main_window as gui_app
from tdlib_media_uploader.core import filesystem_legacy as path_utils
from tdlib_media_uploader.core import self_test
from tdlib_media_uploader.core.instance_lock import InstanceLock, run_with_instance_lock


class V190HardeningTest(unittest.TestCase):
    def test_telegram_video_boundaries_are_exact_and_shared(self):
        standard = app_config.VIDEO_STANDARD_MAX_BYTES
        premium = app_config.VIDEO_PREMIUM_MAX_BYTES
        self.assertEqual(app_config.TELEGRAM_UPLOAD_PART_SIZE_MAX, 524_288)
        self.assertEqual(standard, 4000 * 524_288)
        self.assertEqual(premium, 8000 * 524_288)
        self.assertEqual(app_config.video_size_status(standard - 1), "allowed")
        self.assertEqual(app_config.video_size_status(standard), "allowed")
        self.assertEqual(app_config.video_size_status(standard + 1), "requires_premium")
        self.assertEqual(app_config.video_size_status(standard + 1, is_premium=True), "allowed")
        self.assertEqual(app_config.video_size_status(premium - 1, is_premium=True), "allowed")
        self.assertEqual(app_config.video_size_status(premium, is_premium=True), "allowed")
        self.assertEqual(app_config.video_size_status(premium + 1, is_premium=True), "oversize")

    def test_video_and_mixed_scanners_apply_the_same_video_limits(self):
        from tdlib_media_uploader.media import legacy_mixed as mixed_core
        from tdlib_media_uploader.media import legacy_video as video_core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "clip.mp4"
            path.write_bytes(b"media")
            standard_plus = app_config.VIDEO_STANDARD_MAX_BYTES + 1
            snapshots = {
                path_utils.stable_path(path): path_utils.FileSnapshot(
                    str(path), standard_plus, 100
                )
            }
            result = SimpleNamespace(
                paths=[path], errors=[], warnings=[], cancelled=False, snapshots=snapshots
            )
            with patch.object(video_core.cfg, "VIDEO_DIR", root), \
                    patch.object(video_core.cfg, "VIDEO_EXTENSIONS", {".mp4"}), \
                    patch.object(video_core.cfg, "VIDEO_READ_DATES", False), \
                    patch.object(video_core, "validate_scan_root", return_value=root), \
                    patch.object(video_core, "iter_files", return_value=result):
                self.assertEqual(video_core.scan_videos(), [path])
                self.assertEqual(len(video_core.LAST_SCAN_PREMIUM_REQUIRED), 1)

            mixed_core.LAST_SCAN_SIZE_SKIPS.clear()
            with patch.object(mixed_core.cfg, "MIXED_VIDEO_EXTENSIONS", {".mp4"}), \
                    patch.object(mixed_core.cfg, "MIXED_IMAGE_EXTENSIONS", set()), \
                    patch.object(mixed_core.cfg, "MIXED_EXTENSIONS", {".mp4"}):
                item = mixed_core._item_for_path(path, "Group", (standard_plus, 100))
            self.assertIsNotNone(item)
            self.assertTrue(item["requires_premium"])

            mixed_core.LAST_SCAN_SIZE_SKIPS.clear()
            with patch.object(mixed_core.cfg, "MIXED_VIDEO_EXTENSIONS", {".mp4"}), \
                    patch.object(mixed_core.cfg, "MIXED_IMAGE_EXTENSIONS", set()), \
                    patch.object(mixed_core.cfg, "MIXED_EXTENSIONS", {".mp4"}):
                rejected = mixed_core._item_for_path(
                    path, "Group", (app_config.VIDEO_PREMIUM_MAX_BYTES + 1, 100)
                )
            self.assertIsNone(rejected)
            self.assertEqual(mixed_core.LAST_SCAN_SIZE_SKIPS[-1]["action"], "skip")

    def test_album_key_includes_source_root_scope(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            root_a = Path(first)
            root_b = Path(second)
            item_a = {"path": root_a / "Trip" / "1.jpg", "scan_size": 10, "scan_mtime_ns": 20}
            item_b = {"path": root_b / "Trip" / "1.jpg", "scan_size": 10, "scan_mtime_ns": 20}
            self.assertNotEqual(
                album_metadata.album_key("image", "Trip", [item_a], root=root_a),
                album_metadata.album_key("image", "Trip", [item_b], root=root_b),
            )

    def test_caption_editor_uses_soft_limit_but_runtime_validation_stays_authoritative(self):
        self.assertGreater(gui_app.CAPTION_EDITOR_SOFT_LIMIT, 1024)
        self.assertEqual(
            album_metadata.validate_caption("x" * 1500, gui_app.CAPTION_EDITOR_SOFT_LIMIT),
            "x" * 1500,
        )
        with self.assertRaises(album_metadata.CaptionLimitError):
            album_metadata.validate_caption("x" * 1501, 1024)

    def test_runtime_caption_limit_blocks_before_tdlib_request(self):
        from tdlib_media_uploader.telegram.tdlib_common import TDJsonClient

        client = TDJsonClient.__new__(TDJsonClient)
        client.caption_length_limit = 1024
        requests = []
        client.request = requests.append
        content = {
            "@type": "inputMessagePhoto",
            "photo": {"@type": "inputFileId", "id": 1},
            "caption": {
                "@type": "formattedText",
                "text": "x" * 1025,
                "entities": [],
            },
        }
        with self.assertRaisesRegex(ValueError, "Telegram Caption"):
            client.send_contents([content], kind="image")
        self.assertEqual(requests, [])

    def test_runtime_caption_limit_above_legacy_1024_is_allowed(self):
        from tdlib_media_uploader.telegram.tdlib_common import TDJsonClient

        client = TDJsonClient.__new__(TDJsonClient)
        client.caption_length_limit = 2048
        client.request = lambda _query: {"@type": "message", "id": 7}
        client.wait_for_send_results = lambda _messages: {
            "succeeded": [7],
            "failed": [],
            "pending": [],
        }
        content = {
            "@type": "inputMessagePhoto",
            "photo": {"@type": "inputFileId", "id": 1},
            "caption": {
                "@type": "formattedText",
                "text": "x" * 1500,
                "entities": [],
            },
        }
        self.assertEqual(client.send_contents([content], kind="image"), [7])

    def test_filename_description_is_capped_by_runtime_limit(self):
        value = album_metadata.with_filename_description(
            "标题",
            [Path("clip.mp4"), Path("another-photo.jpg")],
            True,
            max_chars=20,
        )
        self.assertLessEqual(len(value), 20)

    def test_clean_self_test_does_not_create_config(self):
        template = Path(app_config.TEMPLATE_CONFIG_PATH)
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "data" / "config.toml"
            with patch.object(gui_app, "CONFIG_PATH", missing), \
                    patch.object(gui_app, "TEMPLATE_CONFIG_PATH", template), \
                    patch.object(sys, "argv", ["main_window.py", "--self-test"]):
                self.assertFalse(gui_app._ensure_config_file())
                self.assertFalse(missing.exists())
            with patch.object(app_config, "CONFIG_PATH", missing), \
                    patch.object(sys, "argv", ["main_window.py", "--self-test"]):
                loaded = app_config._load()
            self.assertIn("telegram", loaded)
            self.assertFalse(missing.exists())
            existing = Path(directory) / "data" / "config.toml"
            existing.parent.mkdir(parents=True, exist_ok=True)
            existing.write_text("this is not valid toml =", encoding="utf-8")
            with patch.object(app_config, "CONFIG_PATH", existing), \
                    patch.object(sys, "argv", ["main_window.py", "--self-test"]):
                loaded = app_config._load()
            self.assertIn("telegram", loaded)
            self.assertEqual(existing.read_text(encoding="utf-8"), "this is not valid toml =")

    def test_self_test_configures_stdout_and_stderr_for_unicode(self):
        class Stream:
            def __init__(self):
                self.calls = []

            def reconfigure(self, **kwargs):
                self.calls.append(kwargs)

        stdout = Stream()
        stderr = Stream()
        with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            self_test.configure_cli_encoding()
        self.assertEqual(stdout.calls, [{"encoding": "utf-8", "errors": "replace"}])
        self.assertEqual(stderr.calls, [{"encoding": "utf-8", "errors": "replace"}])

    def test_self_test_encoding_failure_is_non_fatal(self):
        class Stream:
            def reconfigure(self, **kwargs):
                raise ValueError("stream is not reconfigurable")

        with patch.object(sys, "stdout", Stream()), patch.object(sys, "stderr", Stream()):
            self_test.configure_cli_encoding()

    def test_self_test_configures_encoding_before_first_output(self):
        events = []
        with patch.object(
            self_test,
            "configure_cli_encoding",
            side_effect=lambda: events.append("configure"),
        ):
            result = self_test.run_self_test(emit=lambda _message: events.append("emit"))
        self.assertEqual(result, 0)
        self.assertEqual(events[0], "configure")

    def test_windows_packaged_self_test_checks_real_process_exit_code(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "build-platforms.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("Start-Process -FilePath $exe", workflow)
        self.assertIn("$process.ExitCode", workflow)
        self.assertNotIn(
            'if ($LASTEXITCODE -ne 0) { throw "Packaged Windows self-test failed',
            workflow,
        )

    def test_workflow_uses_node24_official_actions(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "build-platforms.yml"
        ).read_text(encoding="utf-8")
        self.assertEqual(workflow.count("uses: actions/checkout@v6"), 3)
        self.assertEqual(workflow.count("uses: actions/setup-python@v6"), 2)
        self.assertEqual(workflow.count("uses: actions/upload-artifact@v7"), 2)
        self.assertEqual(workflow.count("uses: actions/download-artifact@v8"), 1)
        self.assertNotIn("actions/download-artifact@v4", workflow)
        self.assertNotIn("ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION", workflow)
        self.assertNotIn("FORCE_JAVASCRIPT_ACTIONS_TO_NODE24", workflow)
        self.assertIn("pattern: tdlib-media-uploader-${{ steps.release_meta.outputs.artifact_label }}-*", workflow)
        self.assertIn("merge-multiple: true", workflow)

    def test_direct_entrypoint_uses_shared_instance_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "data" / "app.lock"
            first = InstanceLock(lock_path)
            self.assertTrue(first.acquire())
            try:
                with self.assertRaises(RuntimeError):
                    run_with_instance_lock(lambda: None, lock_path=lock_path)
            finally:
                first.release()
            self.assertEqual(
                run_with_instance_lock(lambda: "ok", lock_path=lock_path),
                "ok",
            )

    def test_staging_rejects_linked_parent(self):
        from tdlib_media_uploader.upload.staging import ensure_managed_staging_dir

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "external"
            external.mkdir()
            linked_base = root / "linked-base"
            try:
                os.symlink(external, linked_base, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("当前系统不允许创建目录符号链接")
            with self.assertRaises(RuntimeError):
                ensure_managed_staging_dir(linked_base / "managed")

    def test_staging_rejects_linked_configured_base(self):
        from tdlib_media_uploader.upload.staging import ensure_managed_staging_dir

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "external"
            external.mkdir()
            linked_base = root / "linked-base"
            try:
                os.symlink(external, linked_base, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("当前系统不允许创建目录符号链接")
            with self.assertRaises(RuntimeError):
                ensure_managed_staging_dir(
                    linked_base / ".tdlib-media-uploader-staging",
                    base_dir=linked_base,
                )


if __name__ == "__main__":
    unittest.main()
