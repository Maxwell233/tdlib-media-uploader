from __future__ import annotations

import unittest
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.config import loader as config
from tdlib_media_uploader.core.album import CaptionLimitError, validate_caption
from tdlib_media_uploader.core.identity import canonical_target, target_identity
from tdlib_media_uploader.telegram.send_result import (  # noqa: E402
    SendResult,
    map_send_result,
)


class TelegramLimitsTest(unittest.TestCase):
    def test_video_boundaries_are_exact(self):
        standard = config.VIDEO_STANDARD_MAX_BYTES
        premium = config.VIDEO_PREMIUM_MAX_BYTES
        self.assertEqual(config.IMAGE_MAX_BYTES, 10 * 1024**2)
        self.assertEqual(config.TELEGRAM_UPLOAD_PART_SIZE_MAX, 524_288)
        self.assertEqual(standard, 4000 * 524_288)
        self.assertEqual(premium, 8000 * 524_288)
        for size, premium_user, expected in (
            (standard - 1, None, "allowed"),
            (standard, None, "allowed"),
            (standard + 1, None, "requires_premium"),
            (standard + 1, True, "allowed"),
            (premium - 1, True, "allowed"),
            (premium, None, "requires_premium"),
            (premium, True, "allowed"),
            (premium + 1, None, "oversize"),
            (premium + 1, True, "oversize"),
        ):
            with self.subTest(size=size, is_premium=premium_user):
                options = {} if premium_user is None else {"is_premium": premium_user}
                self.assertEqual(config.video_size_status(size, **options), expected)

    def test_caption_limit_is_runtime_authoritative(self):
        self.assertEqual(validate_caption("x" * 1024, 1024), "x" * 1024)
        with self.assertRaises(CaptionLimitError):
            validate_caption("x" * 1025, 1024)


class TelegramTargetTest(unittest.TestCase):
    def test_forum_and_channel_canonical_identity_ignore_irrelevant_fields(self):
        forum_a = canonical_target(
            {
                "target_mode": "forum_topic",
                "chat_id": 100,
                "forum_topic_id": 10,
                "channel_chat_id": 999,
            }
        )
        forum_b = canonical_target(
            {
                "target_mode": "forum_topic",
                "chat_id": 100,
                "forum_topic_id": 10,
                "channel_chat_id": 888,
            }
        )
        channel_a = canonical_target(
            {
                "target_mode": "channel",
                "channel_chat_id": 200,
                "forum_topic_id": 10,
            }
        )
        channel_b = canonical_target(
            {
                "target_mode": "channel",
                "channel_chat_id": 200,
                "forum_topic_id": 999,
            }
        )
        self.assertEqual(forum_a, forum_b)
        self.assertEqual(channel_a, channel_b)
        self.assertNotEqual(target_identity(forum_a), target_identity(channel_a))
        self.assertEqual(canonical_target({"target_mode": "channel", "chat_id": 200})["chat_id"], 200)


class TelegramSendResultTest(unittest.TestCase):
    def test_duplicate_confirmed_ids_never_confirm_two_album_items(self):
        cases = (
            ([{"id": 11}, {"id": 11}], []),
            ([{"id": 11}, {"id": -2, "sending_state": {
                "@type": "messageSendingStatePending",
            }}], [{"@type": "updateMessageSendSucceeded", "old_message_id": -2,
                   "message": {"id": 11}}]),
        )
        for messages, updates in cases:
            with self.subTest(messages=messages, updates=updates):
                result = map_send_result(messages, updates, expected_count=2)
                self.assertEqual(result.status, "UNKNOWN")

    def test_confirmed_failed_and_unknown_semantics(self):
        confirmed = map_send_result([{"id": 1}, {"id": 2}])
        self.assertIsInstance(confirmed, SendResult)
        self.assertEqual(confirmed.status, "CONFIRMED")

        failed = map_send_result(
            [
                {"id": 1, "sending_state": {"@type": "messageSendingStateFailed"}},
                {"id": 2, "sending_state": {"@type": "messageSendingStateFailed"}},
            ]
        )
        self.assertEqual(failed.status, "FAILED")

        unknown = map_send_result(
            [{"id": 1}, {"id": 2, "sending_state": {"@type": "messageSendingStatePending"}}]
        )
        self.assertEqual(unknown.status, "UNKNOWN")
        self.assertEqual(unknown.pending_ids, (2,))

    def test_updates_replace_pending_with_new_message_id_or_failure(self):
        result = map_send_result(
            [
                {"id": -1, "sending_state": {"@type": "messageSendingStatePending"}},
                {"id": -2, "sending_state": {"@type": "messageSendingStatePending"}},
            ],
            [
                {
                    "@type": "updateMessageSendSucceeded",
                    "old_message_id": -1,
                    "message": {"id": 11},
                },
                {
                    "@type": "updateMessageSendFailed",
                    "old_message_id": -2,
                    "error": {"code": 400, "message": "forbidden"},
                },
            ],
        )
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(result.succeeded_ids, (11,))
        self.assertEqual(result.failed_ids, (-2,))
        self.assertEqual(result.pending_ids, ())
        self.assertEqual(result.error, "TDLib error 400: forbidden")

    def test_timeout_and_incomplete_album_are_unknown(self):
        timeout = map_send_result(
            [{"id": -1, "sending_state": {"@type": "messageSendingStatePending"}}],
            timed_out=True,
        )
        incomplete = map_send_result([{"id": 1}], expected_count=2, incomplete=True)
        self.assertEqual(timeout.status, "UNKNOWN")
        self.assertEqual(incomplete.status, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
