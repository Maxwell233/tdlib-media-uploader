"""Cooperative cancellation and bounded concurrency primitives."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, wait
import os
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from typing import TypeVar


class CancellationRequested(TimeoutError):
    """Raised when a cooperative operation observes cancellation.

    It subclasses ``TimeoutError`` for compatibility with the V1 helpers,
    which used that standard exception for cancellation-aware waits.
    """


def is_cancelled(cancel_token=None, cancel_event=None) -> bool:
    """Read either a V2 token or a legacy ``threading.Event``."""

    if cancel_event is not None:
        checker = getattr(cancel_event, "is_set", None)
        if callable(checker) and checker():
            return True
    if cancel_token is not None:
        checker = getattr(cancel_token, "is_cancelled", None)
        if callable(checker):
            return bool(checker())
        checker = getattr(cancel_token, "is_set", None)
        if callable(checker):
            return bool(checker())
        if callable(cancel_token):
            return bool(cancel_token())
    return False


def raise_if_cancelled(cancel_token=None, cancel_event=None) -> None:
    if is_cancelled(cancel_token, cancel_event):
        raise CancellationRequested("操作已取消")


def cancelable_sleep(
    seconds: float,
    cancel_event=None,
    *,
    cancel_token=None,
    quantum: float = 0.05,
) -> bool:
    """Sleep in short slices and return false when cancellation is observed."""

    remaining = max(0.0, float(seconds))
    step = max(0.001, float(quantum))
    while remaining > 0:
        if is_cancelled(cancel_token, cancel_event):
            return False
        delay = min(step, remaining)
        # Event.wait avoids a needless full sleep when a legacy event is used.
        if cancel_event is not None and hasattr(cancel_event, "wait"):
            cancel_event.wait(delay)
        else:
            time.sleep(delay)
        remaining -= delay
    return not is_cancelled(cancel_token, cancel_event)


class CancellationToken:
    """Thread-safe concrete implementation of the shared cancellation shape."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def event(self) -> threading.Event:
        return self._event

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        raise_if_cancelled(self)

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


_T = TypeVar("_T")


def ordered_bounded_map(
    executor,
    items: Iterable[_T],
    worker: Callable[[_T], object],
    max_workers: int,
    *,
    max_ready_buffer: int | None = None,
    cancel_event=None,
    cancel_token=None,
) -> Iterator[object]:
    """Yield bounded worker results in input order.

    A slow first item does not prevent later items from being processed, but
    the reorder buffer remains bounded.  On cancellation, not-yet-started
    futures are cancelled and the caller receives ``CancellationRequested``.
    Running workers are expected to observe the same token if they perform
    cancellable I/O.
    """

    iterator = iter(items)
    limit = max(1, int(max_workers))
    buffer_limit = max(1, int(max_ready_buffer or (limit * 4)))
    pending: dict[Future, int] = {}
    ready: dict[int, Future] = {}
    next_output = 0
    next_index = 0

    def submit_one() -> bool:
        nonlocal next_index
        try:
            item = next(iterator)
        except StopIteration:
            return False
        pending[executor.submit(worker, item)] = next_index
        next_index += 1
        return True

    def cancel_pending() -> None:
        for future in pending:
            future.cancel()
        for future in ready.values():
            future.cancel()

    try:
        if is_cancelled(cancel_token, cancel_event):
            raise CancellationRequested("并发操作已取消")
        for _ in range(limit):
            if not submit_one():
                break
        while pending:
            if is_cancelled(cancel_token, cancel_event):
                cancel_pending()
                raise CancellationRequested("并发操作已取消")

            done, _ = wait(
                tuple(pending),
                timeout=0.05,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                continue
            for future in done:
                index = pending.pop(future)
                ready[index] = future

            while next_output in ready:
                if is_cancelled(cancel_token, cancel_event):
                    cancel_pending()
                    raise CancellationRequested("并发操作已取消")
                future = ready.pop(next_output)
                yield future.result()
                next_output += 1

            while len(pending) < limit and len(ready) < buffer_limit:
                if not submit_one():
                    break
    except BaseException:
        cancel_pending()
        raise


def map_bounded(
    executor,
    items: Iterable[_T],
    worker: Callable[[_T], object],
    max_workers: int,
    **kwargs,
) -> list:
    """List-returning convenience wrapper around :func:`ordered_bounded_map`."""

    return list(ordered_bounded_map(executor, items, worker, max_workers, **kwargs))


def io_worker_count(
    root=None,
    *,
    local: int = 4,
    network: int = 2,
    network_predicate: Callable[[object], bool] | None = None,
) -> int:
    """Return a conservative worker count for local or share-backed I/O."""

    if network_predicate is not None:
        is_network = bool(network_predicate(root))
    else:
        value = os.fspath(root) if root is not None and hasattr(root, "__fspath__") else str(root or "")
        normalized = value.replace("\\", "/")
        is_network = normalized.startswith("//") or any(
            normalized == prefix or normalized.startswith(prefix + "/")
            for prefix in ("/net", "/mnt", "/run/mount", "/Volumes", "/Network")
        )
    return max(1, int(network if is_network else local))


__all__ = [
    "CancellationRequested",
    "CancellationToken",
    "cancelable_sleep",
    "is_cancelled",
    "io_worker_count",
    "map_bounded",
    "ordered_bounded_map",
    "raise_if_cancelled",
]
