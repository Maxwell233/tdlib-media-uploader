from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader import (  # noqa: E402
    AlbumPlan,
    BatchStatus,
    FileSnapshot,
    MediaItem,
    ProgressEvent,
    UploadBatchResult,
)


class RefactorContractTest(unittest.TestCase):
    def test_file_snapshot_keeps_v19_tuple_compatibility(self):
        snapshot = FileSnapshot("/media/clip.mp4", 12, 34)
        self.assertEqual(snapshot.as_tuple(), (12, 34))
        self.assertEqual(tuple(snapshot), (12, 34))

    def test_media_item_carries_one_scan_snapshot(self):
        snapshot = FileSnapshot("/media/clip.mp4", 12, 34)
        item = MediaItem(
            path=Path(snapshot.path),
            source_root=Path("/media"),
            media_kind="video",
            snapshot=snapshot,
        )
        self.assertEqual(item.snapshot.as_tuple(), (12, 34))
        self.assertIsNone(item.capture_time)

    def test_album_plan_keeps_full_and_pending_boundaries(self):
        snapshot = FileSnapshot("/media/clip.mp4", 12, 34)
        item = MediaItem(
            path=Path(snapshot.path),
            source_root=Path("/media"),
            media_kind="video",
            snapshot=snapshot,
        )
        plan = AlbumPlan(
            key="album-key",
            kind="video",
            source_root=Path("/media"),
            group_label="2026-09",
            number=1,
            items=(item,),
            pending_items=(item,),
            caption="2026-09",
            target={"target_mode": "channel", "channel_chat_id": -1001},
        )
        self.assertEqual(plan.items, plan.pending_items)
        self.assertEqual(plan.target["target_mode"], "channel")

    def test_upload_result_and_progress_expose_frozen_status_values(self):
        result = UploadBatchResult("album-key", BatchStatus.UNKNOWN, error="timeout")
        event = ProgressEvent("video", "scan", 2, 5, path="/media/clip.mp4")
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(event.completed, 2)
        self.assertEqual(event.total, 5)


if __name__ == "__main__":
    unittest.main()
