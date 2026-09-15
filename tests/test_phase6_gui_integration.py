from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.core.models import MediaItem, UploadRunResult  # noqa: E402
from tdlib_media_uploader.gui import integration  # noqa: E402


class _UI:
    def __init__(self):
        self.messages = []
        self.albums = []
        self.progress_values = []
        self.finished = 0

    def _message(self, value):
        self.messages.append(str(value))

    def info(self, value):
        self._message(value)

    def warning(self, value):
        self._message(value)

    def error(self, value):
        self._message(value)

    def success(self, value):
        self._message(value)

    def album(self, **value):
        self.albums.append(value)

    def progress(self, **value):
        self.progress_values.append(value)

    def finish(self):
        self.finished += 1


class _State:
    path = Path("state.json")

    def __init__(self, target=None):
        self.target = dict(target or {})
        self.completed = []

    def is_completed(self, value):
        path = value.path if isinstance(value, MediaItem) else value["path"]
        return Path(path) in self.completed

    def mark_album_completed(self, items, message_ids=None):
        self.completed.extend(
            item.path if isinstance(item, MediaItem) else Path(item["path"])
            for item in items
        )


class _LegacyImage:
    def __init__(self, root: Path):
        self.original_root = root / "old-root"
        self.cfg = SimpleNamespace(
            IMAGE_DIR=self.original_root,
            IMAGE_CAPTION_INCLUDE_FILENAMES=False,
        )
        self.IMAGE_SCAN_SNAPSHOTS = {}
        self.UploadState = _State
        self.path = root / "photo.jpg"
        self.cleaned = []

    def scan_images(self, cancel_event=None):
        return [self.path]

    def build_album_plans(self, paths, state=None):
        return [
            {
                "key": "phase6-image-album",
                "number": 1,
                "items": list(paths),
                "pending_items": [
                    path for path in paths if state is None or not state.is_completed(path)
                ],
                "caption": {"text": "Phase 6"},
            }
        ]

    def build_image_contents(self, paths, caption, ui=None, cancel_event=None):
        return (
            [{"@type": "inputMessagePhoto", "caption": caption}],
            list(paths),
            [],
        )

    def cleanup_confirmed_staging(self, paths):
        self.cleaned.extend(Path(path) for path in paths)


class _Client:
    is_premium = True
    caption_length_limit = 128

    def __init__(self, ui, device_model):
        self.ui = ui
        self.device_model = device_model
        self.calls = []
        self.callbacks = []

    def login(self):
        self.calls.append("login")

    def refresh_account_limits(self):
        self.calls.append("limits")

    def set_fast_options(self):
        self.calls.append("fast")

    def validate_target(self):
        self.calls.append("target")

    def add_update_callback(self, callback):
        self.callbacks.append(callback)

    def remove_update_callback(self, callback):
        self.callbacks.remove(callback)

    def send_contents(self, contents, progress=None, items=None):
        self.calls.append(("send", tuple(contents), tuple(items or ())))
        return [9001]

    def close(self):
        self.calls.append("close")


class _Config:
    def activate_target(self, _kind):
        return None


class Phase6GuiIntegrationTest(unittest.TestCase):
    def test_scan_v2_returns_complete_strategy_plans(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "photo.jpg"
            path.write_bytes(b"photo")
            legacy = _LegacyImage(root)
            runtime = SimpleNamespace(
                IMAGE_STATE_DIR=root / "state",
                UPLOAD_INFLIGHT_DIR=root / "journal",
            )
            with patch.object(integration, "_load_legacy", return_value=legacy):
                bundle = integration.scan_v2(
                    "image",
                    source_root=root,
                    target={"target_mode": "channel", "chat_id": -1001},
                    config=_Config(),
                    runtime_paths=runtime,
                )

            self.assertEqual(bundle.scan_result.items[0].path, path)
            self.assertEqual(len(bundle.plans), 1)
            self.assertEqual(bundle.plans[0].pending_items, bundle.scan_result.items)
            self.assertEqual(legacy.cfg.IMAGE_DIR, legacy.original_root)

    def test_upload_uses_engine_lifecycle_and_cleans_only_after_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "photo.jpg"
            path.write_bytes(b"photo")
            legacy = _LegacyImage(root)
            ui = _UI()
            runtime = SimpleNamespace(
                IMAGE_STATE_DIR=root / "state",
                UPLOAD_INFLIGHT_DIR=root / "journal",
            )
            preview = {
                "source_dir": str(root),
                "items": [
                    {
                        "path": path,
                        "scan_size": 5,
                        "scan_mtime_ns": path.stat().st_mtime_ns,
                    }
                ],
                "completed_paths": [],
                "album_count": 1,
            }
            with patch.object(integration, "_load_legacy", return_value=legacy):
                result = integration.run_v2_upload(
                    "image",
                    ui=ui,
                    source_root=root,
                    target={"target_mode": "channel", "chat_id": -1001},
                    preview_result=preview,
                    config=_Config(),
                    runtime_paths=runtime,
                    client_factory=_Client,
                )

            self.assertIsInstance(result, UploadRunResult)
            self.assertEqual(result.status, "COMPLETED")
            self.assertEqual(len(result.batches), 1)
            self.assertEqual(result.batches[0].message_ids, (9001,))
            self.assertEqual(legacy.cleaned, [path])
            self.assertEqual(list((root / "journal").glob("*.json")), [])
            self.assertEqual(ui.finished, 1)


if __name__ == "__main__":
    unittest.main()
