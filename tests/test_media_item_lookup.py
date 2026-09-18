from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader import FileSnapshot, MediaItem, ScanResult  # noqa: E402
from tdlib_media_uploader.media.image import ImageStrategy  # noqa: E402
from tdlib_media_uploader.media.video import VideoStrategy  # noqa: E402


def _items(kind: str, count: int = 4):
    root = Path("/media")
    return tuple(
        MediaItem(
            path=root / f"{index}.{'jpg' if kind == 'image' else 'mp4'}",
            source_root=root,
            media_kind=kind,
            snapshot=FileSnapshot(
                str(root / f"{index}.{'jpg' if kind == 'image' else 'mp4'}"),
                100 + index,
                1000 + index,
            ),
        )
        for index in range(count)
    )


class MediaItemLookupTest(unittest.TestCase):
    def test_image_index_resolves_dict_and_media_item_without_candidate_walk(self):
        legacy = SimpleNamespace(stable_path=lambda value: str(Path(value).resolve()))
        strategy = ImageStrategy(legacy=legacy)
        items = _items("image", 100)
        with patch.object(strategy, "_normalize_path", wraps=strategy._normalize_path) as normalize:
            lookup = strategy._build_item_lookup(items)
            built_calls = normalize.call_count
            raw = {
                "path": items[77].path,
                "scan_size": items[77].snapshot.size,
                "scan_mtime_ns": items[77].snapshot.mtime_ns,
            }
            self.assertIs(strategy._resolve_item(raw, items, lookup), items[77])
            self.assertIs(strategy._resolve_item(items[77], items, lookup), items[77])
            self.assertEqual(normalize.call_count, built_calls + 2)

    def test_video_index_resolves_dict_and_media_item_without_candidate_walk(self):
        legacy = SimpleNamespace(normalize_path=lambda value: str(Path(value).resolve()))
        strategy = VideoStrategy(legacy=legacy)
        items = _items("video", 100)
        with patch.object(strategy, "_normalize_path", wraps=strategy._normalize_path) as normalize:
            lookup = strategy._build_item_lookup(items)
            built_calls = normalize.call_count
            raw = {
                "path": items[77].path,
                "scan_size": items[77].snapshot.size,
                "scan_mtime_ns": items[77].snapshot.mtime_ns,
            }
            self.assertIs(strategy._match_item(raw, items, Path("/media"), lookup), items[77])
            self.assertIs(strategy._match_item(items[77], items, Path("/media"), lookup), items[77])
            self.assertEqual(normalize.call_count, built_calls + 2)

    def test_duplicate_identity_is_rejected_for_image_and_video(self):
        for kind, factory in (
            ("image", lambda: ImageStrategy(legacy=SimpleNamespace(stable_path=str))),
            ("video", lambda: VideoStrategy(legacy=SimpleNamespace(normalize_path=str))),
        ):
            with self.subTest(kind=kind):
                item = _items(kind, 1)[0]
                with self.assertRaises(ValueError):
                    factory()._build_item_lookup((item, item))

    def test_snapshot_mismatch_and_unknown_item_are_not_silently_matched(self):
        image = ImageStrategy(legacy=SimpleNamespace(stable_path=lambda value: str(Path(value).resolve())))
        video = VideoStrategy(legacy=SimpleNamespace(normalize_path=lambda value: str(Path(value).resolve())))
        image_items = _items("image", 1)
        video_items = _items("video", 1)
        image_lookup = image._build_item_lookup(image_items)
        video_lookup = video._build_item_lookup(video_items)
        mismatch = {
            "path": image_items[0].path,
            "scan_size": image_items[0].snapshot.size + 1,
            "scan_mtime_ns": image_items[0].snapshot.mtime_ns,
        }
        with self.assertRaises(ValueError):
            image._resolve_item(mismatch, image_items, image_lookup)
        with self.assertRaises(ValueError):
            video._match_item(
                {"path": video_items[0].path, "scan_size": 999, "scan_mtime_ns": 999},
                video_items,
                Path("/media"),
                video_lookup,
            )
        with self.assertRaises(ValueError):
            image._resolve_item({"path": Path("/media/missing.jpg")}, image_items, image_lookup)

    def test_path_only_identity_requires_unambiguous_snapshot(self):
        root = Path("/media")
        items = (
            MediaItem(root / "same.jpg", root, "image", FileSnapshot(str(root / "same.jpg"), 1, 10)),
            MediaItem(root / "same.jpg", root, "image", FileSnapshot(str(root / "same.jpg"), 2, 20)),
        )
        strategy = ImageStrategy(legacy=SimpleNamespace(stable_path=lambda value: str(Path(value).resolve())))
        lookup = strategy._build_item_lookup(items)
        with self.assertRaises(ValueError):
            strategy._resolve_item({"path": root / "same.jpg"}, items, lookup)

    def test_video_planner_conversion_uses_snapshot_identity(self):
        items = _items("video", 2)

        class Legacy:
            normalize_path = staticmethod(lambda value: str(Path(value).resolve()))

            def build_album_plans(self, values, state=None):
                return [{
                    "key": "album",
                    "items": [
                        {
                            "path": values[0]["path"],
                            "scan_size": values[0]["scan_size"],
                            "scan_mtime_ns": values[0]["scan_mtime_ns"],
                        }
                    ],
                    "pending_items": [values[0]],
                }]

        strategy = VideoStrategy(legacy=Legacy())
        plans = strategy.build_plans(ScanResult(items), target={})
        self.assertEqual(plans[0].items, (items[0],))
        self.assertEqual(plans[0].pending_items, (items[0],))


if __name__ == "__main__":
    unittest.main()
