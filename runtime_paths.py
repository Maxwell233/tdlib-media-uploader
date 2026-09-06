# -*- coding: utf-8 -*-
"""Resolve bundled resources separately from writable application data."""

from __future__ import annotations

import sys
from pathlib import Path


IS_FROZEN = bool(getattr(sys, "frozen", False))
if IS_FROZEN:
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    RESOURCE_DIR = Path(__file__).resolve().parent

# Windows remains a portable folder build, and source runs keep their existing
# layout.  A frozen macOS app must not write into its read-only .app bundle.
if sys.platform == "darwin" and IS_FROZEN:
    APP_DATA_DIR = Path.home() / "Library" / "Application Support" / "TDLib Media Uploader"
else:
    APP_DATA_DIR = RESOURCE_DIR

CONFIG_PATH = APP_DATA_DIR / "config.toml"
TEMPLATE_CONFIG_PATH = RESOURCE_DIR / "config.example.toml"
