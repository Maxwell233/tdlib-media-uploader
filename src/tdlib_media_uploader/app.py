"""Packaged application entry point for TDLib Media Uploader.

The application is distributed and supported as a frozen PyInstaller build.
The offline self-test remains available from that packaged entry point.
"""

from __future__ import annotations

from .gui.application import main, run_self_test  # noqa: E402


__all__ = ["main", "run_self_test"]


if __name__ == "__main__":
    raise SystemExit(main())
