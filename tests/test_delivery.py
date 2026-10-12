import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.core.models import BatchStatus
from tdlib_media_uploader.upload.delivery import _normalize_status

class DeliveryNormalizeStatusTest(unittest.TestCase):
    def test_normalize_status_returns_enum_unchanged(self):
        self.assertIs(_normalize_status(BatchStatus.CONFIRMED), BatchStatus.CONFIRMED)
        self.assertIs(_normalize_status(BatchStatus.FAILED), BatchStatus.FAILED)
        self.assertIs(_normalize_status(BatchStatus.UNKNOWN), BatchStatus.UNKNOWN)

    def test_normalize_status_handles_none(self):
        self.assertIsNone(_normalize_status(None))

    def test_normalize_status_maps_string_aliases(self):
        self.assertIs(_normalize_status("SUCCESS"), BatchStatus.CONFIRMED)
        self.assertIs(_normalize_status("SUCCEEDED"), BatchStatus.CONFIRMED)
        self.assertIs(_normalize_status("COMPLETE"), BatchStatus.CONFIRMED)
        self.assertIs(_normalize_status("COMPLETED"), BatchStatus.CONFIRMED)
        self.assertIs(_normalize_status("PARTIAL"), BatchStatus.UNKNOWN)
        self.assertIs(_normalize_status("ERROR"), BatchStatus.FAILED)
        self.assertIs(_normalize_status("FAIL"), BatchStatus.FAILED)

    def test_normalize_status_maps_case_insensitively_and_strips(self):
        self.assertIs(_normalize_status("  success  "), BatchStatus.CONFIRMED)
        self.assertIs(_normalize_status("Succeeded"), BatchStatus.CONFIRMED)
        self.assertIs(_normalize_status(" eRroR "), BatchStatus.FAILED)
        self.assertIs(_normalize_status("partial\n"), BatchStatus.UNKNOWN)

    def test_normalize_status_maps_actual_enum_values(self):
        self.assertIs(_normalize_status("PREPARED"), BatchStatus.PREPARED)
        self.assertIs(_normalize_status("SUBMITTED"), BatchStatus.SUBMITTED)
        self.assertIs(_normalize_status("CONFIRMED"), BatchStatus.CONFIRMED)
        self.assertIs(_normalize_status("FAILED"), BatchStatus.FAILED)
        self.assertIs(_normalize_status("UNKNOWN"), BatchStatus.UNKNOWN)

    def test_normalize_status_returns_none_for_unknown_strings(self):
        self.assertIsNone(_normalize_status("INVALID"))
        self.assertIsNone(_normalize_status(""))
        self.assertIsNone(_normalize_status("DONE"))

    def test_normalize_status_handles_other_types(self):
        # 1.0 -> "1.0", 1 -> "1", not matching anything
        self.assertIsNone(_normalize_status(1))
        self.assertIsNone(_normalize_status(False))
        self.assertIsNone(_normalize_status(["SUCCESS"]))

if __name__ == "__main__":
    unittest.main()
