from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader import (  # noqa: E402
    AlbumPlan,
    FileSnapshot,
    MediaItem,
    ScanResult,
    UploadContext,
)
from tdlib_media_uploader.media.image import ImageStrategy  # noqa: E402


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


class _LegacyImage:
    def __init__(self, root: Path):
        self.original_root = root / "old-config"
        self.original_snapshots = {"old": (1, 2)}
        self.cfg = SimpleNamespace(
            IMAGE_DIR=self.original_root,
            IMAGE_CAPTION_INCLUDE_FILENAMES=True,
            IMAGE_ALBUM_NUMBERING=True,
        )
        self.IMAGE_SCAN_SNAPSHOTS = dict(self.original_snapshots)
        self.LAST_SCAN_ERRORS = []
        self.LAST_SCAN_WARNINGS = ["network share warning"]
        self.LAST_SCAN_SIZE_SKIPS = []
        self.scan_event = None
        self.plan_calls = []
        self.content_calls = []
        self.caption_calls = []
        self.skip_second = False

    @staticmethod
    def stable_path(path):
        return str(Path(path))

    def scan_images(self, cancel_event=None):
        self.scan_event = cancel_event
        root = Path(self.cfg.IMAGE_DIR)
        paths = (root / "01.jpg", root / "02.jpg")
        self.IMAGE_SCAN_SNAPSHOTS = {
            str(paths[0]): (11, 101),
            str(paths[1]): (22, 202),
        }
        return list(paths)

    def build_album_plans(self, paths, state=None):
        self.plan_calls.append((tuple(paths), state))
        pending = [
            path
            for path in paths
            if state is None or not state.is_completed({"path": path})
        ]
        return [
            {
                "key": "legacy-image-album",
                "number": 1,
                "items": list(paths),
                "pending_items": pending,
                "caption": {"text": "旅行标题"},
            }
        ]

    def with_filename_description(self, caption, items, enabled, max_chars=1024):
        self.caption_calls.append((caption, tuple(items), enabled, max_chars))
        return f"{caption}|{len(items)}"

    @staticmethod
    def formatted_text(text):
        return {"formatted": text}

    def build_image_contents(self, paths, caption, ui=None, cancel_event=None):
        self.content_calls.append((tuple(paths), caption, ui, cancel_event))
        if self.skip_second:
            ui.warning("second image deferred")
            return (
                [{"@type": "photo", "caption": "initial"}],
                list(paths[:1]),
                [
                    {
                        "path": paths[1],
                        "category": "deferred",
                        "reason": "source is still changing",
                    }
                ],
            )
        return (
            [{"@type": "photo", "caption": caption} for _path in paths],
            list(paths),
            [],
        )


class ImageStrategyTest(unittest.TestCase):
    def _strategy(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / "images"
        return root, _LegacyImage(root), _Token(), _Events()

    def test_scan_translates_snapshots_and_restores_legacy_globals(self):
        root, legacy, token, events = self._strategy()
        strategy = ImageStrategy(legacy)

        result = strategy.scan(root, cancel_token=token, event_sink=events)

        self.assertEqual([item.snapshot.as_tuple() for item in result.items], [(11, 101), (22, 202)])
        self.assertEqual({item.source_root for item in result.items}, {root})
        self.assertIs(legacy.scan_event.token, token)
        self.assertEqual(legacy.cfg.IMAGE_DIR, legacy.original_root)
        self.assertEqual(legacy.IMAGE_SCAN_SNAPSHOTS, legacy.original_snapshots)
        self.assertIn("network share warning", result.warnings)

    def test_build_plans_adapts_v2_state_and_preserves_full_boundary(self):
        root, legacy, token, events = self._strategy()
        strategy = ImageStrategy(legacy)
        scan_result = strategy.scan(root, cancel_token=token, event_sink=events)

        class State:
            def is_completed(self, item):
                if not isinstance(item, MediaItem):
                    raise TypeError("V2 state expects MediaItem")
                return item.path.name == "01.jpg"

        plans = strategy.build_plans(scan_result, target={"channel_chat_id": -1001}, state=State())

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].items, scan_result.items)
        self.assertEqual(plans[0].pending_items, (scan_result.items[1],))
        self.assertEqual(plans[0].caption, "旅行标题")
        self.assertEqual(plans[0].target["channel_chat_id"], -1001)
        self.assertEqual(legacy.cfg.IMAGE_DIR, legacy.original_root)
        self.assertEqual(legacy.IMAGE_SCAN_SNAPSHOTS, legacy.original_snapshots)

    def test_build_contents_maps_runtime_skip_and_relabels_caption(self):
        root, legacy, token, events = self._strategy()
        legacy.skip_second = True
        strategy = ImageStrategy(legacy)
        scan_result = strategy.scan(root, cancel_token=token, event_sink=events)
        plan = AlbumPlan(
            key="image",
            kind="image",
            source_root=root,
            group_label="Album 1",
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
        self.assertEqual(result.failed_items, ())
        self.assertEqual(result.errors, ("source is still changing",))
        self.assertEqual(result.contents[0]["caption"], {"formatted": "旅行标题|1"})
        raw_paths, caption, ui, cancel_event = legacy.content_calls[0]
        self.assertEqual(raw_paths, tuple(item.path for item in scan_result.items))
        self.assertEqual(caption, "旅行标题|2")
        self.assertIs(cancel_event.token, token)
        self.assertTrue(any(getattr(event, "source", "") == "image-strategy" for event in events.values))
        self.assertEqual(legacy.cfg.IMAGE_DIR, legacy.original_root)

    def test_build_contents_rejects_an_unaccounted_item(self):
        root, legacy, token, events = self._strategy()
        strategy = ImageStrategy(legacy)
        scan_result = strategy.scan(root, cancel_token=token, event_sink=events)
        plan = AlbumPlan(
            key="image",
            kind="image",
            source_root=root,
            group_label="Album 1",
            number=1,
            items=scan_result.items,
            pending_items=scan_result.items,
            caption="",
            target={},
        )

        def incomplete(paths, caption, ui=None, cancel_event=None):
            return ([{"@type": "photo"}], list(paths[:1]), [])

        legacy.build_image_contents = incomplete
        with self.assertRaises(ValueError):
            strategy.build_contents(plan, cancel_token=token, event_sink=events)


if __name__ == "__main__":
    unittest.main()
