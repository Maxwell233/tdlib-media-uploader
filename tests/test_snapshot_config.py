import unittest
from types import SimpleNamespace
from tdlib_media_uploader.config.snapshot import snapshot_config

class TestSnapshotConfig(unittest.TestCase):
    def test_snapshot_config_empty(self):
        config = SimpleNamespace()
        snapshot = snapshot_config(config)
        self.assertIsInstance(snapshot, SimpleNamespace)
        self.assertEqual(len([name for name in dir(snapshot) if name.isupper()]), 0)

    def test_public_uppercase_filtering(self):
        config = SimpleNamespace(
            VALID_KEY=1,
            lowercase=2,
            _HIDDEN_UPPER=3,
            _hidden_lower=4
        )
        snapshot = snapshot_config(config)
        self.assertEqual(snapshot.VALID_KEY, 1)
        self.assertFalse(hasattr(snapshot, 'lowercase'))
        self.assertFalse(hasattr(snapshot, '_HIDDEN_UPPER'))
        self.assertFalse(hasattr(snapshot, '_hidden_lower'))

    def test_mutable_nested_copy_isolation(self):
        config = SimpleNamespace(
            DATA={"a": [1, 2], "b": {3, 4}},
            LIST=[{"x": 1}]
        )
        snapshot = snapshot_config(config)

        # Modify original
        config.DATA["a"].append(3)
        config.DATA["b"].add(5)
        config.LIST[0]["y"] = 2

        # Snapshot should remain unaffected
        self.assertEqual(snapshot.DATA["a"], [1, 2])
        self.assertEqual(snapshot.DATA["b"], {3, 4})
        self.assertEqual(snapshot.LIST, [{"x": 1}])

    def test_target_override_provider_return_isolation(self):
        target_dict = {"chat_id": 123, "target_mode": "channel"}
        provider_target = {"chat_id": 456, "target_mode": "forum_topic", "forum_topic_id": 1}
        def target_for(name):
            if name == "video":
                return provider_target
            return {}

        config = SimpleNamespace(
            target_for=target_for,
            SOME_SETTING=True
        )

        # Override with target and kind
        snapshot = snapshot_config(config, kind="image", target=target_dict)

        # Verify provider results are properly populated
        self.assertEqual(snapshot.target_for("video")["chat_id"], 456)

        # Verify custom kind/target was added
        self.assertEqual(snapshot.target_for("image")["chat_id"], 123)

        # The override should be isolated - modify dict and ensure snapshot target dict does not change
        target_dict["chat_id"] = 999
        self.assertEqual(snapshot.target_for("image")["chat_id"], 123)

        # Also, check the snapshot itself got updated values from canonical_target
        self.assertEqual(snapshot.CHAT_ID, 123)
        self.assertEqual(snapshot.TARGET_MODE, "channel")
        provider_target["chat_id"] = 999
        returned = snapshot.target_for("video")
        self.assertEqual(returned["chat_id"], 456)
        returned["chat_id"] = 777
        self.assertEqual(snapshot.target_for("video")["chat_id"], 456)

    def test_frozen_config_methods(self):
        config = SimpleNamespace(A=1)
        snapshot = snapshot_config(config, immutable=True)

        with self.assertRaises(AttributeError):
            delattr(snapshot, "A")

        with self.assertRaises(AttributeError):
            snapshot.B = 2

    def test_missing_provider(self):
        config = SimpleNamespace(A=1)
        # Without target_for
        snapshot = snapshot_config(config, kind="image", target={"chat_id": 1})
        self.assertEqual(snapshot.target_for("image"), {"chat_id": 1})
        self.assertEqual(snapshot.target_for("video"), {})

    def test_copy_and_freeze_empty_collections(self):
        config = SimpleNamespace(
            EMPTY_DICT={},
            EMPTY_LIST=[],
            EMPTY_TUPLE=(),
            EMPTY_SET=set(),
            EMPTY_FROZENSET=frozenset()
        )
        snapshot = snapshot_config(config, immutable=True)
        self.assertEqual(snapshot.EMPTY_DICT, {})
        self.assertEqual(snapshot.EMPTY_LIST, ())
        self.assertEqual(snapshot.EMPTY_TUPLE, ())
        self.assertEqual(snapshot.EMPTY_SET, frozenset())
        self.assertEqual(snapshot.EMPTY_FROZENSET, frozenset())
