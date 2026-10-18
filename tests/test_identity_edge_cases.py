import unittest

from tdlib_media_uploader.core.identity import canonical_target


class TestIdentityEdgeCases(unittest.TestCase):
    def test_canonical_target_invalid_inputs(self):
        for value in (None, [], 'invalid', {}, {'target_mode': 'other', 'chat_id': 1}):
            with self.subTest(value=value):
                self.assertEqual(canonical_target(value), {})

    def test_canonical_target_conversion_errors(self):
        for value in ('not an integer', [1, 2], float('inf')):
            targets = (
                {'target_mode': 'forum_topic', 'chat_id': value, 'forum_topic_id': 1},
                {'target_mode': 'forum_topic', 'chat_id': 1, 'forum_topic_id': value},
                {'target_mode': 'channel', 'channel_chat_id': value, 'chat_id': value},
            )
            for target in targets:
                with self.subTest(target=target):
                    self.assertEqual(canonical_target(target), {})

    def test_canonical_target_nested_as_int_public_api(self):
        self.assertEqual(
            canonical_target({'target_mode': 'forum_topic', 'chat_id': '123', 'forum_topic_id': '456'}),
            {'target_mode': 'forum_topic', 'chat_id': 123, 'forum_topic_id': 456, 'channel_chat_id': 0},
        )
        self.assertEqual(
            canonical_target({'target_mode': 'channel', 'channel_chat_id': '789'}),
            {'target_mode': 'channel', 'chat_id': 789, 'forum_topic_id': 0, 'channel_chat_id': 789},
        )
        self.assertEqual(
            canonical_target({'target_mode': 'channel', 'group_chat_id': '999'}),
            {'target_mode': 'channel', 'chat_id': 999, 'forum_topic_id': 0, 'channel_chat_id': 999},
        )

    def test_canonical_target_preserve_irrelevant_fields(self):
        target = {'target_mode': 'channel', 'channel_chat_id': 111, 'irrelevant_field': 'test'}
        self.assertEqual(canonical_target(target), {
            'target_mode': 'channel', 'chat_id': 111, 'forum_topic_id': 0, 'channel_chat_id': 111,
        })


if __name__ == '__main__':
    unittest.main()
