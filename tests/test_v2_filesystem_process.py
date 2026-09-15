"""Focused tests for the GUI-free filesystem/process extraction."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tdlib_media_uploader.core import concurrency, filesystem, readiness, sorting  # noqa: E402
from tdlib_media_uploader.core.models import FileSnapshot  # noqa: E402
from tdlib_media_uploader.processes.runner import (  # noqa: E402
    ProcessCancelled,
    run_cancellable_process,
)


class _FakeEntry:
    def __init__(self, path: Path, info):
        self.path = str(path)
        self.name = path.name
        self._info = info
        self.stat_calls = 0

    def stat(self, *, follow_symlinks=False):
        self.stat_calls += 1
        return self._info


class _FakeScanner:
    def __init__(self, entries):
        self.entries = list(entries)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def __iter__(self):
        return iter(self.entries)

    def close(self):
        self.closed = True


class SortingExtractionTest(unittest.TestCase):
    def test_natural_sort_uses_numeric_runs_and_deterministic_ties(self):
        self.assertEqual(
            sorting.natural_sort(["x.1", "x.10", "x.100", "x.41"]),
            ["x.1", "x.10", "x.41", "x.100"],
        )
        values = ["x001", "a1", "x1", "A1", "x01"]
        self.assertEqual(
            sorting.natural_sort(values),
            ["A1", "a1", "x1", "x01", "x001"],
        )

    def test_relative_path_sort_compares_directories_before_filenames(self):
        root = Path("root")
        paths = [
            root / "B2" / "x.41.jpg",
            root / "A" / "x.1.jpg",
            root / "B2" / "x.410.jpg",
            root / "A" / "x.100.jpg",
            root / "B10" / "x.2.jpg",
        ]
        ordered = sorting.media_path_sort(paths, root)
        self.assertEqual(
            [sorting.relative_name(path, root) for path in ordered],
            ["A/x.1.jpg", "A/x.100.jpg", "B2/x.41.jpg", "B2/x.410.jpg", "B10/x.2.jpg"],
        )

    def test_sort_key_is_evaluated_once_per_value(self):
        calls = []
        values = ["a10", "a2", "a1"]
        ordered = sorting.natural_sort(values, key=lambda value: calls.append(value) or value)
        self.assertEqual(ordered, ["a1", "a2", "a10"])
        self.assertEqual(calls, values)


class FilesystemExtractionTest(unittest.TestCase):
    def test_discovery_captures_one_stat_snapshot_for_matching_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "clip1.jpg"
            path.write_bytes(b"media")
            entry = _FakeEntry(path, path.stat())
            scanner = _FakeScanner([entry])
            with patch.object(filesystem, "is_link_or_junction", return_value=False), \
                    patch.object(filesystem.os, "scandir", return_value=scanner), \
                    patch.object(filesystem, "snapshot_file", side_effect=AssertionError("late stat")):
                result = filesystem.iter_files(
                    root,
                    {".jpg"},
                    discovery_initial_delay=0,
                    discovery_max_delay=0,
                )
            self.assertEqual(result.paths, [path])
            self.assertEqual(entry.stat_calls, 1)
            snapshot = result.snapshots[filesystem.stable_path(path)]
            self.assertIsInstance(snapshot, FileSnapshot)
            self.assertEqual(snapshot.size, 5)

    def test_discovery_skips_file_and_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_dir = root / "real"
            real_dir.mkdir()
            (real_dir / "inside.jpg").write_bytes(b"media")
            target = root / "target.jpg"
            target.write_bytes(b"media")
            try:
                (root / "link.jpg").symlink_to(target)
                (root / "link-dir").symlink_to(real_dir, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            result = filesystem.iter_files(
                root,
                {".jpg"},
                discovery_initial_delay=0,
                discovery_max_delay=0,
            )
            self.assertEqual([path.name for path in result.paths], ["target.jpg", "inside.jpg"])
            self.assertEqual(len(result.warnings), 2)

    def test_discovery_honours_pre_cancelled_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "clip.jpg").write_bytes(b"media")
            event = threading.Event()
            event.set()
            result = filesystem.iter_files(root, {".jpg"}, cancel_event=event)
            self.assertTrue(result.cancelled)
            self.assertEqual(result.paths, [])

    def test_retry_is_finite_and_retries_transient_failure(self):
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            raise OSError(errno.EIO, "temporary share failure")

        with self.assertRaises(OSError):
            filesystem.retry_fs_operation(
                operation,
                attempts=3,
                initial_delay=0,
                max_delay=0,
            )
        self.assertEqual(calls, 3)

    def test_retry_succeeds_after_bounded_transient_failures(self):
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise OSError(errno.EIO, "temporary share failure")
            return "ok"

        self.assertEqual(
            filesystem.retry_fs_operation(
                operation,
                attempts=3,
                initial_delay=0,
                max_delay=0,
            ),
            "ok",
        )
        self.assertEqual(calls, 3)


class ReadinessExtractionTest(unittest.TestCase):
    def test_readiness_ready_and_uses_expected_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(b"media")
            snapshot = filesystem.snapshot_file(path)
            self.assertIsNotNone(snapshot)
            result = readiness.check_file_readiness(
                path,
                expected_size=snapshot.size,
                expected_mtime_ns=snapshot.mtime_ns,
                stable_interval=0,
                stable_checks=2,
            )
            self.assertTrue(result.ready)
            self.assertEqual(result.status, readiness.READY)

    def test_readiness_distinguishes_changed_empty_and_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "clip.mp4"
            path.write_bytes(b"media")
            snapshot = filesystem.snapshot_file(path)
            changed = readiness.check_file_readiness(
                path,
                expected_size=snapshot.size + 1,
                stable_interval=0,
                probe=False,
            )
            self.assertEqual(changed.status, readiness.CHANGED)
            empty = root / "empty.mp4"
            empty.touch()
            self.assertEqual(
                readiness.check_file_readiness(empty, probe=False).status,
                readiness.UNREADABLE,
            )
            missing = root / "missing.mp4"
            self.assertEqual(
                readiness.check_file_readiness(missing, probe=False).status,
                readiness.DEFERRED,
            )

    def test_readiness_detects_change_between_stability_observations(self):
        first = FileSnapshot("clip.mp4", 5, 10)
        second = FileSnapshot("clip.mp4", 6, 10)
        with patch.object(
            readiness,
            "snapshot_file_with_code",
            side_effect=[(first, "ready"), (second, "ready")],
        ):
            result = readiness.check_file_readiness(
                "clip.mp4",
                stable_interval=0,
                stable_checks=2,
                probe=False,
            )
        self.assertEqual(result.status, readiness.CHANGED)
        self.assertEqual(result.code, "unstable")

    def test_readiness_cancellation_and_bounded_retry(self):
        event = threading.Event()
        event.set()
        cancelled = readiness.check_file_readiness("missing", cancel_event=event)
        self.assertEqual(cancelled.status, readiness.CANCELLED)

        calls = 0

        def missing(_path):
            nonlocal calls
            calls += 1
            return None, "stat_failed"

        with patch.object(readiness, "snapshot_file_with_code", side_effect=missing):
            deferred = readiness.wait_for_file_ready(
                "missing",
                attempts=3,
                initial_delay=0,
                probe=False,
            )
        self.assertEqual(deferred.status, readiness.DEFERRED)
        self.assertEqual(deferred.attempts, 3)
        self.assertEqual(calls, 3)


class ConcurrencyExtractionTest(unittest.TestCase):
    def test_ordered_bounded_map_preserves_order_and_worker_bound(self):
        active = 0
        peak = 0
        lock = threading.Lock()

        def worker(value):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01 if value == 0 else 0)
            with lock:
                active -= 1
            return value * 2

        with ThreadPoolExecutor(max_workers=2) as executor:
            values = list(concurrency.ordered_bounded_map(executor, range(6), worker, 2))
        self.assertEqual(values, [0, 2, 4, 6, 8, 10])
        self.assertLessEqual(peak, 2)

    def test_concurrency_token_cancels_sleep_and_map(self):
        token = concurrency.CancellationToken()
        token.cancel()
        self.assertTrue(token.is_cancelled())
        self.assertFalse(concurrency.cancelable_sleep(1, cancel_token=token))
        with ThreadPoolExecutor(max_workers=2) as executor:
            with self.assertRaises(concurrency.CancellationRequested):
                list(
                    concurrency.ordered_bounded_map(
                        executor,
                        range(4),
                        lambda value: value,
                        2,
                        cancel_token=token,
                    )
                )


class ProcessRunnerExtractionTest(unittest.TestCase):
    def _run(self, script, **kwargs):
        kwargs.setdefault("cancel_event", threading.Event())
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
        kwargs.setdefault("text", True)
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
        return run_cancellable_process([sys.executable, "-u", "-c", script], **kwargs)

    def test_process_runner_returns_output_and_reports_exit_code(self):
        result = self._run("print('ok'); print('warn', file=__import__('sys').stderr)")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "ok")
        self.assertEqual(result.stderr.strip(), "warn")
        with self.assertRaises(subprocess.CalledProcessError) as context:
            self._run("print('bad'); raise SystemExit(7)", check=True)
        self.assertEqual(context.exception.returncode, 7)

    def test_process_runner_drains_large_stdout_and_stderr(self):
        script = (
            "import sys; "
            "sys.stdout.write('o'*500000); sys.stdout.flush(); "
            "sys.stderr.write('e'*500000); sys.stderr.flush()"
        )
        result = self._run(script, timeout=10)
        self.assertEqual(len(result.stdout), 500000)
        self.assertEqual(len(result.stderr), 500000)

    def test_process_runner_supports_input_and_output_limit(self):
        result = self._run(
            "import sys; sys.stdout.write(sys.stdin.read())",
            input="中文输入",
            max_output_bytes=2,
        )
        self.assertEqual(result.stdout, "中文")

    def test_process_runner_timeout_and_cancellation_terminate_child(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self._run("import time; time.sleep(10)", timeout=0.1, terminate_grace_seconds=0.05)
        self.assertLess(time.monotonic() - started, 3)

        event = threading.Event()

        def cancel_later():
            time.sleep(0.1)
            event.set()

        threading.Thread(target=cancel_later, daemon=True).start()
        with self.assertRaises(ProcessCancelled):
            self._run("import time; time.sleep(10)", cancel_event=event)


if __name__ == "__main__":
    unittest.main()
