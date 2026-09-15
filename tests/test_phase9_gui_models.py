from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.core.models import AlbumPlan, FileSnapshot, MediaItem, ScanResult  # noqa: E402
from tdlib_media_uploader.gui import models  # noqa: E402


class _CaptionStore:
    def __init__(self, _kind):
        pass

    def get(self, _key, default):
        return {"base_label": default, "custom_text": "custom"}


class Phase9GuiModelsTest(unittest.TestCase):
    def _bundle(self, *, cancelled=False):
        root = Path("/tmp/phase9-preview")
        first = MediaItem(
            path=root / "one.jpg",
            source_root=root,
            media_kind="image",
            snapshot=FileSnapshot(str(root / "one.jpg"), 10, 1),
            group_name="one",
        )
        second = MediaItem(
            path=root / "two.jpg",
            source_root=root,
            media_kind="image",
            snapshot=FileSnapshot(str(root / "two.jpg"), 20, 2),
            group_name="two",
        )
        plan = AlbumPlan(
            key="phase9-album",
            kind="image",
            source_root=root,
            group_label="1",
            number=1,
            items=(first, second),
            pending_items=(second,),
            caption="Caption",
        )
        return SimpleNamespace(
            kind="image",
            source_root=root,
            target={"target_mode": "channel", "chat_id": 7},
            state=SimpleNamespace(path=root / "state.json"),
            legacy=SimpleNamespace(LAST_SCAN_SIZE_SKIPS=[], LAST_SCAN_IGNORED_ROOT_MEDIA=[]),
            scan_result=ScanResult(
                items=() if cancelled else (first, second),
                errors=("unreadable",) if cancelled else (),
                cancelled=cancelled,
            ),
            plans=() if cancelled else (plan,),
            missing=(),
        )

    def test_scan_result_preserves_plan_partition_and_counts(self):
        logs = []
        result = models.scan_result(
            self._bundle(),
            size_resolver=lambda value: 10 if str(value["path"]).endswith("one.jpg") else 20,
            logger=lambda *args, **kwargs: logs.append((args, kwargs)),
            caption_store_factory=_CaptionStore,
        )

        self.assertEqual(result["total_files"], 2)
        self.assertEqual(result["completed_files"], 1)
        self.assertEqual(result["pending_files"], 1)
        self.assertEqual(result["total_bytes"], 30)
        self.assertEqual(result["pending_bytes"], 20)
        self.assertEqual(result["album_count"], 1)
        self.assertEqual(result["groups"][0]["pending"], 1)
        self.assertEqual(
            result["groups"][0]["album_plans"][0]["pending_items"][0]["path"],
            Path("/tmp/phase9-preview/two.jpg"),
        )
        self.assertEqual(result["groups"][0]["caption"], "Caption")
        self.assertEqual(logs, [])

    def test_cancelled_scan_can_keep_root_specific_result_factory(self):
        calls = []

        def cancelled(kind, **kwargs):
            calls.append((kind, kwargs))
            return {"kind": kind, "cancelled": True, **kwargs}

        result = models.scan_result(
            self._bundle(cancelled=True),
            cancelled_result_factory=cancelled,
        )

        self.assertTrue(result["cancelled"])
        self.assertEqual(calls[0][0], "image")
        self.assertEqual(calls[0][1]["scan_errors"], ("unreadable",))


if __name__ == "__main__":
    unittest.main()
