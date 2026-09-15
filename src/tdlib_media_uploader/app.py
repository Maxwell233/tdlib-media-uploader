"""Package-owned application entry point during the incremental migration.

The package GUI bootstrap owns launch dispatch and self-test handling.  The
existing widgets remain behind that boundary until their page-by-page move is
complete.
"""

from __future__ import annotations

import sys
from pathlib import Path


if not getattr(sys, "frozen", False):
    # Keep direct source-file launches working while the project is not
    # installed as a wheel yet.  Module launches already have ``src`` on the
    # path, but a file launch starts below it.
    source_root = Path(__file__).resolve().parents[1]
    project_root = source_root.parent
    for search_path in (source_root, project_root):
        if str(search_path) not in sys.path:
            sys.path.insert(0, str(search_path))

from tdlib_media_uploader.gui.application import main, run_self_test  # noqa: E402


__all__ = ["main", "run_self_test"]


if __name__ == "__main__":
    raise SystemExit(main())
