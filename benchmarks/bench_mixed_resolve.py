"""Measure path-only mixed outcomes; run from any directory, without TDLib I/O."""

import argparse
from pathlib import Path
import sys
from time import perf_counter
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tdlib_media_uploader.core.models import AlbumPlan, FileSnapshot, MediaItem
from tdlib_media_uploader.media.mixed import MixedMediaStrategy


def run_benchmark(num_items: int) -> float:
    if num_items < 1:
        raise ValueError("items must be positive")
    root = Path("/benchmark")
    pending = tuple(
        MediaItem(
            path=root / f"file_{index}.jpg",
            source_root=root,
            media_kind="image",
            snapshot=FileSnapshot(str(root / f"file_{index}.jpg"), 1024, index),
        )
        for index in range(num_items)
    )
    plan = AlbumPlan("benchmark", "mixed", root, "benchmark", 1, pending, pending, "")
    skipped = [{"path": str(item.path), "category": "unreadable"} for item in pending]
    strategy = MixedMediaStrategy(legacy=SimpleNamespace())

    started = perf_counter()
    result = strategy._normalize_contents(([], [], skipped), plan, context=None)
    elapsed = perf_counter() - started
    if result.failed_items != pending or result.ready_items or result.deferred_items or result.contents:
        raise AssertionError("benchmark did not resolve every path to the expected outcome")
    return elapsed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, nargs="+", default=[5000, 10000])
    args = parser.parse_args()
    for count in args.items:
        print(f"{count} items: {run_benchmark(count):.4f} seconds")
