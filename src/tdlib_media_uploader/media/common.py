"""Shared readiness, diagnostics and staging policy for media pipelines."""

from __future__ import annotations

from collections.abc import Iterable, MutableMapping, Sequence
from pathlib import Path
from typing import Any

from ..core.concurrency import ContextExecutor, ordered_bounded_map, raise_if_cancelled
from ..core.readiness import CHANGED, FileReadiness, FileReadinessError, readiness_category
from ..core.filesystem import io_worker_count, revalidate_file, stable_path
from ..upload.staging import cleanup_staging, remove_staged_file
from .runtime import pipeline_paths_lock


def readiness_options(config: Any, *, network: bool, default_attempts: int = 3) -> dict:
    """Resolve one explicit configuration without changing a pipeline's globals."""

    interval = getattr(config, "SCAN_STABILITY_INTERVAL_SECONDS", None)
    checks = getattr(config, "SCAN_STABILITY_CHECKS", None)
    return {
        "attempts": int(getattr(config, "SCAN_READINESS_ATTEMPTS", default_attempts)),
        "stable_interval": float(getattr(
            config,
            "SCAN_STABILITY_INTERVAL_NETWORK_SECONDS" if network else "SCAN_STABILITY_INTERVAL_LOCAL_SECONDS",
            interval if interval is not None else (0.5 if network else 0.05),
        )),
        "stable_checks": int(getattr(
            config,
            "SCAN_STABILITY_CHECKS_NETWORK" if network else "SCAN_STABILITY_CHECKS_LOCAL",
            checks if checks is not None else (3 if network else 2),
        )),
        "probe_bytes": int(getattr(config, "SCAN_READ_PROBE_BYTES", 64 * 1024)),
    }


def readiness_record(error: BaseException) -> dict | None:
    """Keep structured failure fields consistent across all media kinds."""

    if not isinstance(error, FileReadinessError):
        return None
    readiness = error.readiness
    return {
        "readiness_status": readiness.status,
        "readiness_code": readiness.code,
        "readiness_attempts": readiness.attempts,
        "category": readiness_category(readiness),
    }


def revalidate_ready_source(path, *, expected_size, expected_mtime_ns, cancel_event):
    """Check the same snapshot after a successful full preflight in this run."""

    raise_if_cancelled(cancel_event=cancel_event)
    try:
        return revalidate_file(path, expected_size=expected_size, expected_mtime_ns=expected_mtime_ns)
    except (RuntimeError, OSError) as exc:
        raise FileReadinessError(path, FileReadiness(CHANGED, reason=str(exc), attempts=1)) from exc


def build_source_options(context, plan):
    if context is None:
        return {}
    paths_for = getattr(context.stager, "paths_for", None)
    return {
        "staged_paths": paths_for(plan) if callable(paths_for) else None,
        "preflight_verified": bool(context.metadata.get("preflight_verified")),
        "executor": context.metadata.get("io_executor"),
    }


def media_io_results(items, worker, *, config, root, cancel_event=None, executor=None):
    """Run bounded media I/O with the same order and cancellation in each mode."""

    if len(items) <= 1 and executor is None:
        for item in items:
            raise_if_cancelled(cancel_event=cancel_event)
            yield worker(item)
        return
    count = min(len(items), io_worker_count(
        root, local=getattr(config, "IO_WORKERS_LOCAL", 4),
        network=getattr(config, "IO_WORKERS_NETWORK", 2),
    ))
    if executor is not None:
        yield from ordered_bounded_map(executor, items, worker, count, cancel_event=cancel_event)
    else:
        with ContextExecutor(max_workers=count, thread_name_prefix="tdlib-media-io") as pool:
            yield from ordered_bounded_map(pool, items, worker, count, cancel_event=cancel_event)


def format_size(value: float | int) -> str:
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024


def prune_staging_cache(config: Any, *, startup: bool = False) -> None:
    """Prune old copies even when creating new staged copies is disabled."""

    if startup and not getattr(config, "STAGING_CLEANUP_ON_START", True):
        return
    cleanup_staging(
        config.STAGING_DIR,
        staging_base_dir=getattr(config, "STAGING_BASE_DIR", config.STAGING_DIR),
        max_age_seconds=float(getattr(config, "STAGING_CLEANUP_DAYS", 7)) * 86400,
    )


def cleanup_confirmed_staging(
    paths: Iterable[Path] | None,
    *,
    config: Any,
    staged_maps: Sequence[MutableMapping[str, Path]],
) -> None:
    """Remove confirmed paths from the explicitly supplied staging maps."""

    if not getattr(config, "STAGING_CLEANUP_AFTER_SUCCESS", True):
        return
    for path in paths or ():
        key = stable_path(path)
        for mapping in staged_maps:
            with pipeline_paths_lock:
                staged = mapping.pop(key, None)
            if staged is not None:
                remove_staged_file(staged)
