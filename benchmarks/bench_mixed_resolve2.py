import time
from pathlib import Path
from dataclasses import dataclass
from typing import Sequence

import sys
sys.path.insert(0, str(Path("src").resolve()))

import types
mock_tdjson = types.ModuleType("tdjson")
sys.modules["tdjson"] = mock_tdjson

from tdlib_media_uploader.media.mixed import MixedMediaStrategy
from tdlib_media_uploader.core.models import MediaItem, AlbumPlan, FileSnapshot

class MockPlan:
    def __init__(self, items):
        self.pending_items = items
        self.source_root = Path("/")
        self.type = "document"
        self.caption = ""

def run_benchmark():
    num_items = 10000
    pending = []

    for i in range(num_items):
        item = MediaItem(
            path=Path(f"/tmp/file_{i}.txt"),
            source_root=Path("/"),
            media_kind="document",
            snapshot=FileSnapshot(path=f"/tmp/file_{i}.txt", size=1024, mtime_ns=0)
        )
        pending.append(item)

    plan = MockPlan(pending)

    # Generate large number of skipped records, each missing 'item' to trigger the path lookup branch
    raw_skipped = [{"path": str(item.path)} for item in pending]

    strategy = MixedMediaStrategy(legacy=types.ModuleType("mock"))

    start_time = time.time()
    try:
        strategy._normalize_contents(
            ([], [], raw_skipped),
            plan,
            context=None
        )
    except Exception as e:
        # Catch expected exception since our mock results are not full valid records,
        # but only after _resolve_outcome_item has done its work.
        pass

    end_time = time.time()
    return end_time - start_time

if __name__ == "__main__":
    duration = run_benchmark()
    print(f"Baseline Time taken: {duration:.4f} seconds")
