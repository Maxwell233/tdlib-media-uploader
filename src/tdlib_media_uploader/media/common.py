"""Shared readiness, diagnostics and staging policy for media pipelines."""

from __future__ import annotations

from collections.abc import Iterable, MutableMapping, Sequence
from pathlib import Path
from typing import Any

from ..core.readiness import FileReadinessError, readiness_category
from ..core.filesystem import stable_path
from ..upload.staging import cleanup_staging, remove_staged_file


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
            staged = mapping.pop(key, None)
            if staged is not None:
                remove_staged_file(staged)
