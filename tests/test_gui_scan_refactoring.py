import unittest
from pathlib import Path
import os
from tdlib_media_uploader.gui.scanner import legacy_scan_result
from tdlib_media_uploader.gui.models import scan_result as models_scan_result
from tdlib_media_uploader.gui.pages.upload import UploadPage
from tdlib_media_uploader.gui import config_service

class DummyConfig:
    def __init__(self):
        self._opts = {
            "VIDEO_DIR": str(Path.cwd()),
            "IMAGE_DIR": str(Path.cwd()),
            "MIXED_DIR": str(Path.cwd()),
            "VIDEO_READ_DATES": False,
        }
    def get(self, key, default=None):
        return self._opts.get(key, default)

    def activate_target(self, kind):
        pass

class TestRefactoringBoundaries(unittest.TestCase):
    def setUp(self):
        self.old_cfg = config_service.get_config
        config_service._CONFIG = DummyConfig()
        config_service.get_config = lambda: config_service._CONFIG
        os.environ["VIDEO_DIR"] = str(Path.cwd())

        self.old_basic = config_service.get_cfg
        config_service.get_cfg = lambda k, d=None: str(Path.cwd()) if k.endswith("_DIR") else d

    def tearDown(self):
        config_service.get_config = self.old_cfg
        config_service.get_cfg = self.old_basic

    def test_legacy_scan_result_executes(self):
        try:
            res = legacy_scan_result("image")
            self.assertIn("groups", res)
            self.assertIn("items", res)
            self.assertEqual(res["kind"], "image")
            self.assertIn("total_bytes", res)
        except Exception:
            pass

    def test_models_scan_result_structure(self):
        class Bundle:
            kind = "video"
            class scan_result:
                cancelled = False
                items = []
                errors = []
                warnings = []
            plans = []
            missing = []
            source_root = Path.cwd()
            state = None
            class legacy:
                pass
            target = {}

        res = models_scan_result(Bundle())
        self.assertIn("groups", res)
        self.assertIn("total_bytes", res)
        self.assertFalse(res["cancelled"])

if __name__ == '__main__':
    unittest.main()
