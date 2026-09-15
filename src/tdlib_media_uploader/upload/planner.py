"""Pure Album planning helpers owned by the V2 upload boundary.

The planner never refills an Album after preflight.  A strategy creates the
complete boundary in :class:`~tdlib_media_uploader.core.models.AlbumPlan`,
then this module may restrict ``pending_items`` while retaining ``items`` as
the original immutable boundary for state, journal and diagnostics.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Sequence

from ..core.models import AlbumPlan, MediaItem


def item_identity(item: MediaItem) -> tuple[object, ...]:
    """Return the stable in-memory identity used for boundary validation."""

    if not isinstance(item, MediaItem):
        raise TypeError(f"Album item 必须是 MediaItem，而不是 {type(item).__name__}")
    return (
        str(Path(item.path)),
        str(Path(item.source_root)),
        str(item.media_kind),
        item.snapshot.path,
        item.snapshot.size,
        item.snapshot.mtime_ns,
    )


def _subsequence_positions(
    full_items: Sequence[MediaItem],
    selected_items: Sequence[MediaItem],
) -> list[int]:
    positions: list[int] = []
    cursor = 0
    for selected in selected_items:
        selected_identity = item_identity(selected)
        for index in range(cursor, len(full_items)):
            if item_identity(full_items[index]) == selected_identity:
                positions.append(index)
                cursor = index + 1
                break
        else:
            raise ValueError("pending_items 必须是完整 Album items 的有序子序列")
    return positions


def validate_plan(plan: AlbumPlan) -> AlbumPlan:
    """Validate that a strategy did not mutate an Album boundary."""

    if not isinstance(plan, AlbumPlan):
        raise TypeError(f"策略必须返回 AlbumPlan，而不是 {type(plan).__name__}")
    full_items = tuple(plan.items)
    pending_items = tuple(plan.pending_items)
    for item in full_items:
        item_identity(item)
    _subsequence_positions(full_items, pending_items)
    return plan


def restrict_plan(plan: AlbumPlan, items: Iterable[MediaItem]) -> AlbumPlan:
    """Keep only selected pending items, preserving the original order.

    ``items`` must be a multiset subset of ``plan.pending_items``.  The
    returned plan never changes ``items``; only ``pending_items`` is narrowed.
    This makes cross-group refill and post-preflight Album reshaping
    impossible at the generic engine boundary.
    """

    validate_plan(plan)
    requested = tuple(items)
    for item in requested:
        item_identity(item)

    available = Counter(item_identity(item) for item in plan.pending_items)
    selected_counts = Counter(item_identity(item) for item in requested)
    if any(count > available[key] for key, count in selected_counts.items()):
        raise ValueError("只能从 AlbumPlan.pending_items 中选择待上传文件")

    pending_positions = _subsequence_positions(plan.items, plan.pending_items)
    by_identity: dict[tuple[object, ...], deque[int]] = defaultdict(deque)
    for position in pending_positions:
        by_identity[item_identity(plan.items[position])].append(position)
    selected_positions: list[int] = []
    for item in requested:
        selected_positions.append(by_identity[item_identity(item)].popleft())
    selected_positions.sort()
    selected = tuple(plan.items[position] for position in selected_positions)
    return replace(plan, pending_items=selected)


def pending_plans(plans: Iterable[AlbumPlan]) -> tuple[AlbumPlan, ...]:
    """Validate and return only plans that still contain pending items."""

    result: list[AlbumPlan] = []
    for plan in plans:
        validate_plan(plan)
        if plan.pending_items:
            result.append(plan)
    return tuple(result)


__all__ = [
    "item_identity",
    "pending_plans",
    "restrict_plan",
    "validate_plan",
]
