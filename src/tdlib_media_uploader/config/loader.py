"""Compatibility import for the V1.9 configuration loader.

The implementation remains in ``app_config.py`` until the dedicated config
phase.  This module is the package-facing name used by new imports.
"""

from __future__ import annotations

import sys
from pathlib import Path


if not getattr(sys, "frozen", False):
    project_root = Path(__file__).resolve().parents[3]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

import app_config as _legacy_loader  # noqa: E402

# Preserve the complete legacy module surface, including private test hooks
# such as ``_load``.  A module alias also keeps monkeypatches coherent while
# callers transition from ``app_config`` to this package path.
sys.modules[__name__] = _legacy_loader
