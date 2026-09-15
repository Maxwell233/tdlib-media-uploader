"""Package-owned application entry point during the incremental migration.

The GUI implementation still lives in the V1.9 compatibility module until
Phase 6.  Keeping this adapter in the target package lets source and frozen
launches converge on one entry point without moving GUI internals early.
"""

from __future__ import annotations

import sys
from pathlib import Path


if not getattr(sys, "frozen", False):
    # ``python src/tdlib_media_uploader/app.py`` is a supported development
    # probe while the project is not installed as a wheel yet.
    project_root = Path(__file__).resolve().parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from gui_app import main, run_self_test  # noqa: E402


__all__ = ["main", "run_self_test"]


if __name__ == "__main__":
    raise SystemExit(main())
