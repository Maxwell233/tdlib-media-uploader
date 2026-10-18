import unittest
from tdlib_media_uploader.core.identity import canonical_target


class TestIdentityEdgeCases(unittest.TestCase):
    def test_canonical_target_invalid_inputs(self):
        # Invalid inputs return empty dict
        self.assertEqual(canonical_target(None), {})
        self.assertEqual(canonical_target([]), {})
        self.assertEqual(canonical_target("invalid_string"), {})

    def test_canonical_target_conversion_errors(self):
        # Non-convertible string
        target_mode_forum = {
            "target_mode": "forum_topic",
            "chat_id": "invalid_int",
            "forum_topic_id": "1",
        }
        self.assertEqual(canonical_target(target_mode_forum), {})

        # Test TypeError (e.g. nested lists)
        target_mode_channel = {
            "target_mode": "channel",
            "channel_chat_id": [1, 2],
            "group_chat_id": [3, 4],
        }
        self.assertEqual(canonical_target(target_mode_channel), {})

        # Test OverflowError
        # A float('inf') raises OverflowError when converted to int
        target_mode_channel_overflow = {
            "target_mode": "channel",
            "channel_chat_id": float("inf"),
            "group_chat_id": float("inf"),
        }
        self.assertEqual(canonical_target(target_mode_channel_overflow), {})

    def test_canonical_target_nested_as_int_public_api(self):
        # Valid conversion
        valid_forum = {
            "target_mode": "forum_topic",
            "chat_id": "123",
            "forum_topic_id": "456",
        }
        expected_forum = {
            "target_mode": "forum_topic",
            "chat_id": 123,
            "forum_topic_id": 456,
            "channel_chat_id": 0,
        }
        self.assertEqual(canonical_target(valid_forum), expected_forum)

        valid_channel = {"target_mode": "channel", "channel_chat_id": "789"}
        expected_channel = {
            "target_mode": "channel",
            "chat_id": 789,
            "forum_topic_id": 0,
            "channel_chat_id": 789,
        }
        self.assertEqual(canonical_target(valid_channel), expected_channel)

        # Fallback to group_chat_id/chat_id
        valid_channel_fallback = {"target_mode": "channel", "group_chat_id": "999"}
        expected_channel_fallback = {
            "target_mode": "channel",
            "chat_id": 999,
            "forum_topic_id": 0,
            "channel_chat_id": 999,
        }
        self.assertEqual(
            canonical_target(valid_channel_fallback), expected_channel_fallback
        )

    def test_canonical_target_preserve_irrelevant_fields(self):
        # The result only returns the identified canonical keys, stripping the irrelevant ones
        mixed_target = {
            "target_mode": "channel",
            "channel_chat_id": 111,
            "irrelevant_field": "test",
        }
        expected = {
            "target_mode": "channel",
            "chat_id": 111,
            "forum_topic_id": 0,
            "channel_chat_id": 111,
        }
        self.assertEqual(canonical_target(mixed_target), expected)


if __name__ == "__main__":
    unittest.main()
