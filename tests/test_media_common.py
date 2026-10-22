"""Shared media policies preserve configuration and confirmed-file boundaries."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.core.readiness import FileReadiness, FileReadinessError
from tdlib_media_uploader.core.filesystem import stable_path
from tdlib_media_uploader.media import common


class MediaCommonTest(unittest.TestCase):
    def test_readiness_defaults_and_old_settings_remain_compatible(self):
        cases = (
            ({}, False, (0.05, 2)),
            ({}, True, (0.5, 3)),
            ({"SCAN_STABILITY_INTERVAL_SECONDS": 0.8, "SCAN_STABILITY_CHECKS": 4}, False, (0.8, 4)),
            ({"SCAN_STABILITY_INTERVAL_SECONDS": 0.8, "SCAN_STABILITY_CHECKS": 4}, True, (0.8, 4)),
            ({"SCAN_STABILITY_INTERVAL_LOCAL_SECONDS": 0.2, "SCAN_STABILITY_CHECKS_LOCAL": 5}, False, (0.2, 5)),
            ({"SCAN_STABILITY_INTERVAL_NETWORK_SECONDS": 1.2, "SCAN_STABILITY_CHECKS_NETWORK": 6}, True, (1.2, 6)),
        )
        for values, network, expected in cases:
            with self.subTest(values=values, network=network):
                config = SimpleNamespace(**values)
                actual = common.readiness_options(config, network=network, default_attempts=7)
                self.assertEqual((actual["stable_interval"], actual["stable_checks"]), expected)
                self.assertEqual(actual["attempts"], 7)
                self.assertEqual(actual["probe_bytes"], 65536)
                self.assertEqual(vars(config), values)

    def test_parallel_configs_do_not_share_readiness_settings(self):
        first = SimpleNamespace(SCAN_READINESS_ATTEMPTS=1, SCAN_READ_PROBE_BYTES=1024)
        second = SimpleNamespace(SCAN_READINESS_ATTEMPTS=9, SCAN_READ_PROBE_BYTES=2048)
        configs = [first, second] * 20
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda config: common.readiness_options(config, network=False), configs))
        self.assertEqual([result["attempts"] for result in results], [1, 9] * 20)
        self.assertEqual([result["probe_bytes"] for result in results], [1024, 2048] * 20)

    def test_readiness_failures_keep_reporting_categories(self):
        for status, code, category in (
            ("DEFERRED", "stat_failed", "deferred"),
            ("CHANGED", "changed", "deferred"),
            ("CANCELLED", "cancelled", "cancelled"),
            ("UNREADABLE", "read_failed", "unreadable"),
        ):
            with self.subTest(status=status):
                error = FileReadinessError("source.jpg", FileReadiness(status, attempts=2, code=code))
                self.assertEqual(common.readiness_record(error), {
                    "readiness_status": status,
                    "readiness_code": code,
                    "readiness_attempts": 2,
                    "category": category,
                })
        self.assertIsNone(common.readiness_record(TimeoutError("media probe timed out")))

    def test_size_format_uses_identical_unit_boundaries(self):
        for value, expected in ((0, "0 B"), (1023, "1023 B"), (1024, "1.00 KiB"),
                                (1024**2, "1.00 MiB"), (1024**4, "1.00 TiB")):
            with self.subTest(value=value):
                self.assertEqual(common.format_size(value), expected)

    def test_startup_cleanup_switch_does_not_disable_later_cleanup(self):
        config = SimpleNamespace(STAGING_DIR=Path("cache/staging"), STAGING_CLEANUP_ON_START=False,
                                 STAGING_MODE="off", STAGING_CLEANUP_DAYS=2)
        with patch.object(common, "cleanup_staging") as cleanup:
            common.prune_staging_cache(config, startup=True)
            cleanup.assert_not_called()
            common.prune_staging_cache(config)
            cleanup.assert_called_once_with(config.STAGING_DIR, staging_base_dir=config.STAGING_DIR,
                                            max_age_seconds=2 * 86400)

    def test_confirmed_cleanup_preserves_other_paths_and_honors_switch(self):
        confirmed, pending = Path("source/confirmed.jpg"), Path("source/pending.jpg")
        first_copy, second_copy, pending_copy = Path("stage/image.jpg"), Path("stage/video.mp4"), Path("stage/pending.jpg")
        first = {stable_path(confirmed): first_copy, stable_path(pending): pending_copy}
        second = {stable_path(confirmed): second_copy}
        with patch.object(common, "remove_staged_file") as remove:
            common.cleanup_confirmed_staging([confirmed], config=SimpleNamespace(STAGING_CLEANUP_AFTER_SUCCESS=False),
                                             staged_maps=(first, second))
            remove.assert_not_called()
            self.assertEqual(len(first), 2)
            self.assertEqual(len(second), 1)
            common.cleanup_confirmed_staging([confirmed], config=SimpleNamespace(), staged_maps=(first, second))
            self.assertEqual(remove.call_args_list, [call(first_copy), call(second_copy)])
        self.assertEqual(first, {stable_path(pending): pending_copy})
        self.assertEqual(second, {})

    def test_mixed_pipeline_keeps_item_adaptation_and_empty_size_behavior(self):
        from tdlib_media_uploader.media import mixed_pipeline as mixed

        confirmed = Path("source/confirmed.jpg")
        image_copy, video_copy = Path("stage/image.jpg"), Path("stage/video.mp4")
        images, videos = {stable_path(confirmed): image_copy}, {stable_path(confirmed): video_copy}
        with patch.object(mixed, "cfg", SimpleNamespace()), \
             patch.object(mixed.image_core, "STAGED_UPLOAD_PATHS", images), \
             patch.object(mixed.video_core, "STAGED_UPLOAD_PATHS", videos), \
             patch.object(common, "remove_staged_file") as remove:
            mixed.cleanup_confirmed_staging([None, {"path": None}, {"path": confirmed}])
            self.assertEqual(remove.call_args_list, [call(image_copy), call(video_copy)])
        self.assertEqual(images, {})
        self.assertEqual(videos, {})
        self.assertEqual(mixed.format_size(None), "0 B")


if __name__ == "__main__":
    unittest.main()
