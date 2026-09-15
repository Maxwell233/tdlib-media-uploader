"""Package-owned GUI bootstrap during the incremental migration.

The widgets still live in the repository-level :mod:`gui_app` module, but the
package now owns the public application and health-check entrypoints.  Root
GUI loading stays lazy so ``--self-test`` remains useful on machines that do
not have the Qt runtime installed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable


def _ensure_project_root() -> None:
    """Make migration-period root modules importable for source launches."""

    if getattr(sys, "frozen", False):
        return
    project_root = Path(__file__).resolve().parents[3]
    if project_root.is_dir() and str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))


def _load_root_main() -> Callable[[], int]:
    """Load the legacy widget lifecycle only when a real GUI is requested."""

    _ensure_project_root()
    from gui_app import main as root_main  # noqa: PLC0415

    return root_main


def run_self_test(*, emit=print) -> int:
    """Run the offline health check without importing the Qt application."""

    _ensure_project_root()
    from self_test import run_self_test as check  # noqa: PLC0415

    return check(emit=emit)


def main() -> int:
    """Dispatch the packaged GUI or its dependency-light self-test."""

    if "--self-test" in sys.argv[1:]:
        return run_self_test()
    return _load_root_main()()


__all__ = ["main", "run_self_test"]
