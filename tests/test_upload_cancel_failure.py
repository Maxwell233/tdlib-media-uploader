"""A broken stop provider must not silently authorize further upload work."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tdlib_media_uploader.core.models import ScanResult
from tdlib_media_uploader.upload.engine import UploadEngine


class UploadCancelFailureTest(unittest.TestCase):
    def strategy(self):
        return SimpleNamespace(kind="image", scan=Mock(return_value=ScanResult(())),
                               build_plans=Mock(return_value=()))

    def test_cancel_query_failure_propagates_before_scan(self):
        token = SimpleNamespace(
            is_cancelled=Mock(side_effect=RuntimeError("cancel query failed")),
            raise_if_cancelled=lambda: None,
        )
        strategy = self.strategy()
        with self.assertRaisesRegex(RuntimeError, "cancel query failed"):
            UploadEngine().run(strategy, source_root=Path("."), target={}, cancel_token=token)
        strategy.scan.assert_not_called()

    def test_safe_stop_query_failure_propagates_before_scan(self):
        token = SimpleNamespace(is_cancelled=lambda: False,
            stop_after_current=Mock(side_effect=RuntimeError("stop query failed")))
        strategy = self.strategy()
        with self.assertRaisesRegex(RuntimeError, "stop query failed"):
            UploadEngine().run(strategy, source_root=Path("."), target={}, cancel_token=token)
        strategy.scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
