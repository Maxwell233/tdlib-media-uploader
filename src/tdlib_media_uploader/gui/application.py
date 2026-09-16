"""Packaged GUI bootstrap for TDLib Media Uploader.

The widgets remain an internal bundled implementation.  The public launch
contract is the frozen PyInstaller application; source-tree execution is not
supported.  ``--self-test`` stays lazy so it can run without creating the Qt
application window.
"""

from __future__ import annotations

import sys
from typing import Callable


def _load_root_main() -> Callable[[], int]:
    """Load the bundled widget lifecycle only when a real GUI is requested."""

    from .main_window import main as root_main  # noqa: PLC0415

    return root_main


def run_self_test(*, emit=print) -> int:
    """Run the offline health check without importing the Qt application."""

    from ..core.self_test import run_self_test as check  # noqa: PLC0415

    return check(emit=emit)


def main() -> int:
    """Dispatch the packaged GUI or its dependency-light self-test."""

    if not getattr(sys, "frozen", False):
        print(
            "此应用仅支持从发布包运行，请从 GitHub Releases 下载对应平台的程序包。",
            file=sys.stderr,
        )
        return 2

    if "--self-test" in sys.argv[1:]:
        return run_self_test()
    return _load_root_main()()


__all__ = ["main", "run_self_test"]
