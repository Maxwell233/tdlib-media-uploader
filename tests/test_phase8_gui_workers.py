from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.gui.events import AuthBridge  # noqa: E402
from tdlib_media_uploader.gui.workers import ScanWorker, UploadWorker  # noqa: E402


class Phase8GuiWorkersTest(unittest.TestCase):
    def test_scan_worker_uses_explicit_preview_runner(self):
        calls = []
        results = []

        def scan_runner(kind, *, progress_callback, cancel_event):
            calls.append((kind, progress_callback, cancel_event))
            progress_callback({"phase": "scan", "completed": 1, "total": 1})
            return {"kind": kind, "cancelled": False, "items": []}

        worker = ScanWorker("image", scan_runner=scan_runner)
        worker.completed.connect(results.append)
        worker.run()

        self.assertEqual(results, [{"kind": "image", "cancelled": False, "items": []}])
        self.assertEqual(calls[0][0], "image")
        self.assertFalse(calls[0][2].is_set())

    def test_upload_worker_passes_boundary_dependencies_and_maps_result(self):
        calls = []
        messages = []
        expected_config = object()
        expected_runtime_paths = object()
        preview = {"source_dir": "/tmp/preview", "items": [], "album_count": 0}

        def upload_runner(kind, **kwargs):
            calls.append((kind, kwargs))
            return SimpleNamespace(
                status="PARTIAL",
                deferred_items=("deferred",),
                failed_items=("failed",),
                cancelled=False,
            )

        worker = UploadWorker(
            "image",
            AuthBridge(),
            preview,
            upload_runner=upload_runner,
            target_provider=lambda kind: {"target_mode": "channel", "chat_id": 42},
            source_root_provider=lambda kind: Path("/tmp/configured"),
            config=expected_config,
            runtime_paths=expected_runtime_paths,
        )
        worker.completed.connect(lambda success, message: messages.append((success, message)))
        worker.run()

        self.assertEqual(messages, [(False, "任务部分完成：暂缓 1 个，失败 1 个；修复后可重新扫描。")])
        kind, kwargs = calls[0]
        self.assertEqual(kind, "image")
        self.assertEqual(kwargs["source_root"], Path("/tmp/configured"))
        self.assertEqual(kwargs["target"], {"target_mode": "channel", "chat_id": 42})
        self.assertIs(kwargs["config"], expected_config)
        self.assertIs(kwargs["runtime_paths"], expected_runtime_paths)
        self.assertIs(kwargs["ui"], worker.ui)
        self.assertIs(kwargs["cancel_event"], worker.ui.cancel_event)


if __name__ == "__main__":
    unittest.main()
