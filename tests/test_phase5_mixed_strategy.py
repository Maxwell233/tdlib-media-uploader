from __future__ import annotations

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
    BatchStatus,
    FileSnapshot,
    MediaItem,
    ScanResult,
    UploadContext,
)
from tdlib_media_uploader.media.mixed import MixedMediaStrategy  # noqa: E402
from tdlib_media_uploader.telegram.send_result import SendResult  # noqa: E402
from tdlib_media_uploader.upload import (  # noqa: E402
    MemoryJournalStore,
    MemoryStateStore,
    RUN_COMPLETED,
    UploadEngine,
)


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


class _FakeLegacy:
    def __init__(self, root: Path):
        self.original_root = root / "old-config"
        self.cfg = SimpleNamespace(
            MIXED_DIR=self.original_root,
            MIXED_CAPTION_INCLUDE_FILENAMES=True,
            MIXED_CAPTION_INCLUDE_FILENAME_NUMBERS=True,
        )
        self.LAST_SCAN_ERRORS = ["temporary scan warning"]
        self.LAST_SCAN_WARNINGS = ["network share warning"]
        self.LAST_SCAN_SIZE_SKIPS = [
            {"action": "skip"},
            {"action": "compress"},
        ]
        self.LAST_SCAN_IGNORED_ROOT_MEDIA = [root / "root.jpg"]
        self.scan_event = None
        self.planning_groups = None
        self.content_calls = []
        self.skip_second = False
        self.caption_calls = []

    def scan_mixed_groups(self, cancel_event=None):
        self.scan_event = cancel_event
        root = Path(self.cfg.MIXED_DIR)
        return [
            {
                "group_name": "旅行",
                "group_path": root / "旅行",
                "items": [
                    {
                        "path": root / "旅行" / "01.jpg",
                        "media_kind": "image",
                        "scan_size": 11,
                        "scan_mtime_ns": 101,
                    },
                    {
                        "path": root / "旅行" / "02.mp4",
                        "media_kind": "video",
                        "scan_size": 22,
                        "scan_mtime_ns": 202,
                        "requires_premium": True,
                    },
                ],
            }
        ]

    def build_album_plans(self, groups, state=None):
        self.planning_groups = groups
        result = []
        for group in groups:
            items = list(group["items"])
            pending = [
                item for item in items if state is None or not state.is_completed(item)
            ]
            result.append(
                {
                    "key": "legacy-mixed-album",
                    "group_name": group["group_name"],
                    "number": 1,
                    "items": items,
                    "pending_items": pending,
                    "caption": {"text": "旅行标题"},
                }
            )
        return result

    def with_filename_description(self, caption, items, enabled, numbered, max_chars=1024):
        self.caption_calls.append((caption, tuple(items), enabled, numbered, max_chars))
        return f"{caption}|{len(items)}"

    @staticmethod
    def formatted_text(text):
        return {"formatted": text}

    def build_mixed_contents(self, items, caption, ui=None, cancel_event=None):
        self.content_calls.append((tuple(items), caption, ui, cancel_event))
        if self.skip_second:
            ui.warning("second item deferred")
            return (
                [{"@type": "mixed", "caption": "initial"}],
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
        return (
            [
                {"@type": "mixed", "caption": caption}
                for _item in items
            ],
            list(items),
            [],
        )


class MixedStrategyTest(unittest.TestCase):
    def _strategy(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / "mixed"
        return root, _FakeLegacy(root), _Token(), _Events()

    def test_scan_translates_legacy_groups_and_restores_legacy_root(self):
        root, legacy, token, events = self._strategy()
        strategy = MixedMediaStrategy(legacy)

        result = strategy.scan(root, cancel_token=token, event_sink=events)

        self.assertIsInstance(result, ScanResult)
        self.assertEqual([item.media_kind for item in result.items], ["image", "video"])
        self.assertEqual([item.snapshot.as_tuple() for item in result.items], [(11, 101), (22, 202)])
        self.assertEqual({item.source_root for item in result.items}, {root})
        self.assertEqual(result.items[0].group_name, "旅行")
        self.assertTrue(result.items[1].metadata["requires_premium"])
        self.assertIs(legacy.scan_event._token, token)
        self.assertEqual(legacy.cfg.MIXED_DIR, legacy.original_root)
        self.assertIn("temporary scan warning", result.errors)
        self.assertTrue(any("一级子文件夹" in warning for warning in result.warnings))

    def test_build_plans_reuses_legacy_state_filter_and_preserves_album_boundary(self):
        root, legacy, token, events = self._strategy()
        strategy = MixedMediaStrategy(legacy)
        scan_result = strategy.scan(root, cancel_token=token, event_sink=events)

        class State:
            def is_completed(self, item):
                return Path(item["path"]).name == "01.jpg"

        plans = strategy.build_plans(
            scan_result,
            target={"target_mode": "channel", "channel_chat_id": -1001},
            state=State(),
        )

        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertIsInstance(plan, AlbumPlan)
        self.assertEqual(plan.kind, "mixed")
        self.assertEqual(plan.items, scan_result.items)
        self.assertEqual(plan.pending_items, (scan_result.items[1],))
        self.assertEqual(plan.group_label, "旅行")
        self.assertEqual(plan.target["channel_chat_id"], -1001)
        self.assertEqual(legacy.cfg.MIXED_DIR, legacy.original_root)
        self.assertEqual(len(legacy.planning_groups[0]["items"]), 2)

    def test_build_contents_maps_runtime_skip_and_relabels_filename_caption(self):
        root, legacy, token, events = self._strategy()
        legacy.skip_second = True
        strategy = MixedMediaStrategy(legacy)
        scan_result = strategy.scan(root, cancel_token=token, event_sink=events)
        plan = AlbumPlan(
            key="mixed",
            kind="mixed",
            source_root=root,
            group_label="旅行",
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
        self.assertEqual(len(legacy.content_calls), 1)
        raw_items, caption, ui, cancel_event = legacy.content_calls[0]
        self.assertEqual(caption, "旅行标题|2")
        self.assertEqual([item["media_kind"] for item in raw_items], ["image", "video"])
        self.assertIs(cancel_event._token, token)
        self.assertTrue(
            any(getattr(event, "source", "") == "mixed-strategy" for event in events.values)
        )
        self.assertEqual(legacy.cfg.MIXED_DIR, legacy.original_root)

    def test_adapter_runs_through_v2_engine_without_owning_transport(self):
        root, legacy, token, events = self._strategy()
        legacy.LAST_SCAN_ERRORS = []
        legacy.cfg.MIXED_CAPTION_INCLUDE_FILENAMES = False
        strategy = MixedMediaStrategy(legacy)

        class Sender:
            def __init__(self):
                self.calls = []

            def send_contents(self, contents, target, **_kwargs):
                self.calls.append((tuple(contents), dict(target)))
                return SendResult(BatchStatus.CONFIRMED, succeeded_ids=(101, 102))

        sender = Sender()
        result = UploadEngine(
            sender=sender,
            state=MemoryStateStore(),
            journal=MemoryJournalStore(),
        ).run(
            strategy,
            source_root=root,
            target={"target_mode": "channel", "channel_chat_id": -1001},
            cancel_token=token,
            event_sink=events,
        )

        self.assertEqual(result.status, RUN_COMPLETED)
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual([item["media_kind"] for item in legacy.content_calls[0][0]], ["image", "video"])


if __name__ == "__main__":
    unittest.main()
