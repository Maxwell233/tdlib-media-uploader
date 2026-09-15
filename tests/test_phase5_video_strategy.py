from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader import (  # noqa: E402
    AlbumPlan,
    MediaItem,
    ScanResult,
    UploadContext,
)
from tdlib_media_uploader.media.video import VideoStrategy  # noqa: E402


class _Token:
    def __init__(self, cancelled: bool = False):
        self.cancelled = cancelled

    def is_cancelled(self):
        return self.cancelled

    def raise_if_cancelled(self):
        if self.cancelled:
            error = RuntimeError("cancelled")
            error.cancelled = True
            raise error


class _Events:
    def __init__(self):
        self.values = []

    def emit(self, event):
        self.values.append(event)


class _LegacyVideo:
    def __init__(self, root: Path):
        self.original_root = root / "old-config"
        self.original_snapshots = {"old": (1, 2)}
        self.cfg = SimpleNamespace(
            VIDEO_DIR=self.original_root,
            VIDEO_READ_DATES=True,
            VIDEO_CAPTION_INCLUDE_FILENAMES=True,
            VIDEO_CAPTION_INCLUDE_FILENAME_NUMBERS=True,
        )
        self.LAST_SCAN_SNAPSHOTS = dict(self.original_snapshots)
        self.LAST_SCAN_WARNINGS = []
        self.LAST_SCAN_ERRORS = []
        self.LAST_SCAN_SIZE_SKIPS = []
        self.scan_event = None
        self.metadata_calls = []
        self.plan_calls = []
        self.content_calls = []

    @staticmethod
    def normalize_path(path):
        return str(Path(path))

    def scan_videos(self, cancel_event=None):
        self.scan_event = cancel_event
        root = Path(self.cfg.VIDEO_DIR)
        paths = (root / "01.mp4", root / "02.mp4")
        self.LAST_SCAN_SNAPSHOTS = {
            str(paths[0]): (101, 1001),
            str(paths[1]): (202, 2002),
        }
        return list(paths)

    def read_exif_metadata(self, paths=None, cancel_event=None, progress_callback=None):
        self.metadata_calls.append((tuple(paths or ()), cancel_event, progress_callback))
        if progress_callback is not None:
            progress_callback({"phase": "metadata", "completed": len(paths), "total": len(paths)})
        return {str(path): {"CreateDate": "2024:01:02 03:04:05"} for path in paths}

    def build_items(self, videos, metadata, progress_callback=None, cancel_event=None):
        items = []
        for index, path in enumerate(videos, start=1):
            if self.cfg.VIDEO_READ_DATES:
                capture_time = datetime(2024, 1, index, 3, 4, 5)
                month_key = "2024-01"
                date_tag = f"2024-01-0{index}"
            else:
                capture_time = None
                month_key = "filename"
                date_tag = "未读取日期"
            size, mtime_ns = self.LAST_SCAN_SNAPSHOTS[str(path)]
            items.append(
                {
                    "path": path,
                    "capture_time": capture_time,
                    "month_key": month_key,
                    "date_tag": date_tag,
                    "fallback": False,
                    "scan_size": size,
                    "scan_mtime_ns": mtime_ns,
                }
            )
        return items, []

    def build_album_plans(self, items, state=None):
        self.plan_calls.append((tuple(items), state))
        pending = [
            item
            for item in items
            if state is None or not state.is_completed(item)
        ]
        return [
            {
                "key": "legacy-video-album",
                "month_key": "2024-01",
                "number": 1,
                "items": list(items),
                "pending_items": pending,
                "caption": {"text": "旅行标题"},
            }
        ]

    def with_filename_description(self, caption, items, enabled, numbered=True, max_chars=1024):
        return f"{caption}|{len(items)}"

    @staticmethod
    def formatted_text(text):
        return {"formatted": text}

    def build_video_contents(self, items, caption, ui=None, cancel_event=None):
        self.content_calls.append((tuple(items), caption, ui, cancel_event))
        ui.info("video content builder called")
        return (
            [{"@type": "video", "caption": "initial"}],
            list(items[:1]),
            [
                {
                    "item": items[1],
                    "path": items[1]["path"],
                    "category": "deferred",
                    "reason": "source is still changing",
                }
            ],
        )


class VideoStrategyTest(unittest.TestCase):
    def _strategy(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / "videos"
        return root, _LegacyVideo(root), _Token(), _Events()

    def test_scan_reads_metadata_and_restores_legacy_root_and_snapshots(self):
        root, legacy, token, events = self._strategy()
        strategy = VideoStrategy(legacy)

        result = strategy.scan(root, cancel_token=token, event_sink=events)

        self.assertEqual([item.snapshot.as_tuple() for item in result.items], [(101, 1001), (202, 2002)])
        self.assertEqual([item.month_key for item in result.items], ["2024-01", "2024-01"])
        self.assertIs(legacy.scan_event.token, token)
        self.assertEqual(len(legacy.metadata_calls), 1)
        self.assertIs(legacy.metadata_calls[0][1].token, token)
        self.assertEqual(legacy.cfg.VIDEO_DIR, legacy.original_root)
        self.assertEqual(legacy.LAST_SCAN_SNAPSHOTS, legacy.original_snapshots)
        self.assertTrue(any(getattr(event, "kind", "") == "video" for event in events.values))

    def test_build_plans_adapts_state_and_keeps_the_complete_album_boundary(self):
        root, legacy, token, events = self._strategy()
        strategy = VideoStrategy(legacy)
        scan_result = strategy.scan(root, cancel_token=token, event_sink=events)

        class State:
            def is_completed(self, item):
                if not isinstance(item, MediaItem):
                    raise TypeError("V2 state expects MediaItem")
                return item.path.name == "01.mp4"

        plans = strategy.build_plans(scan_result, target={"channel_chat_id": -1001}, state=State())

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].items, scan_result.items)
        self.assertEqual(plans[0].pending_items, (scan_result.items[1],))
        self.assertEqual(plans[0].group_label, "2024-01")
        self.assertEqual(plans[0].target["channel_chat_id"], -1001)
        self.assertEqual(legacy.cfg.VIDEO_DIR, legacy.original_root)
        self.assertEqual(legacy.LAST_SCAN_SNAPSHOTS, legacy.original_snapshots)

    def test_build_contents_maps_runtime_skip_and_relabels_caption(self):
        root, legacy, token, events = self._strategy()
        strategy = VideoStrategy(legacy)
        scan_result = strategy.scan(root, cancel_token=token, event_sink=events)
        plan = AlbumPlan(
            key="video",
            kind="video",
            source_root=root,
            group_label="2024-01",
            number=1,
            items=scan_result.items,
            pending_items=scan_result.items,
            caption="旅行标题",
            target={},
        )
        context = UploadContext(
            source_root=root,
            target={},
            cancel_token=token,
            event_sink=events,
            metadata={"caption_limit": 64},
        )

        result = strategy.build_contents(
            plan,
            cancel_token=token,
            event_sink=events,
            context=context,
        )

        self.assertEqual(result.ready_items, (scan_result.items[0],))
        self.assertEqual(result.deferred_items, (scan_result.items[1],))
        self.assertEqual(result.errors, ("source is still changing",))
        self.assertEqual(result.contents[0]["caption"], {"formatted": "旅行标题|1"})
        raw_items, caption, _ui, cancel_event = legacy.content_calls[0]
        self.assertEqual(caption, "旅行标题|2")
        self.assertEqual([item["path"] for item in raw_items], [item.path for item in scan_result.items])
        self.assertIs(cancel_event.token, token)
        self.assertTrue(any(getattr(event, "source", "") == "video-strategy" for event in events.values))
        self.assertEqual(legacy.cfg.VIDEO_DIR, legacy.original_root)
        self.assertEqual(legacy.LAST_SCAN_SNAPSHOTS, legacy.original_snapshots)

    def test_dates_disabled_skip_metadata_reader_and_keep_capture_time_empty(self):
        root, legacy, token, events = self._strategy()
        legacy.cfg.VIDEO_READ_DATES = False
        strategy = VideoStrategy(legacy)

        result = strategy.scan(root, cancel_token=token, event_sink=events)

        self.assertEqual(legacy.metadata_calls, [])
        self.assertEqual([item.capture_time for item in result.items], [None, None])
        self.assertEqual([item.month_key for item in result.items], ["filename", "filename"])


if __name__ == "__main__":
    unittest.main()
