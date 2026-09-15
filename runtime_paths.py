# -*- coding: utf-8 -*-
"""Resolve immutable resources and the single writable data directory.

V1.9 keeps the application bundle read-only and puts every piece of user
data below ``DATA_DIR``.  The path rules are intentionally small and free of
configuration imports so they can also be used by the frozen self-test.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _source_resource_dir() -> Path:
    """Find the repository resource root from any source package location.

    ``runtime_paths.py`` is currently a root compatibility module.  During
    the staged migration it may be relocated below ``src/tdlib_media_uploader``
    without moving the source-level ``config.example.toml`` and ``VERSION``
    files.  Walking parents by those stable markers preserves source-run
    behavior in both layouts.
    """

    module_dir = Path(__file__).resolve().parent
    for candidate in (module_dir, *module_dir.parents):
        has_template = (
            (candidate / "resources" / "default_config.toml").is_file()
            or (candidate / "config.example.toml").is_file()
        )
        if (candidate / "VERSION").is_file() and has_template:
            return candidate
    return module_dir


IS_FROZEN = bool(getattr(sys, "frozen", False))
if IS_FROZEN:
    RESOURCE_DIR = Path(
        getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)
    ).resolve()
    APP_ROOT = Path(sys.executable).resolve().parent
else:
    RESOURCE_DIR = _source_resource_dir()
    APP_ROOT = RESOURCE_DIR

# Windows remains a portable folder build, and source runs keep their existing
# layout.  A frozen macOS app must not write into its read-only .app bundle.
if sys.platform == "darwin" and IS_FROZEN:
    DATA_BASE_DIR = Path.home() / "Library" / "Application Support" / "TDLib Media Uploader"
else:
    DATA_BASE_DIR = APP_ROOT

# All mutable files live below this directory.  APP_DATA_DIR remains as a
# source-compatible alias for integrations written before the V1.9 layout.
DATA_DIR = DATA_BASE_DIR / "data"
APP_DATA_DIR = DATA_DIR

STATE_DIR = DATA_DIR / "state"
VIDEO_STATE_DIR = STATE_DIR / "video"
IMAGE_STATE_DIR = STATE_DIR / "image"
MIXED_STATE_DIR = STATE_DIR / "mixed"
TELEGRAM_DIR = DATA_DIR / "telegram"
TDLIB_DATABASE_DIR = TELEGRAM_DIR / "database"
TDLIB_FILES_DIR = TELEGRAM_DIR / "files"
CAPTIONS_DIR = DATA_DIR / "captions"
UPLOAD_INFLIGHT_DIR = DATA_DIR / "upload_inflight"
CACHE_DIR = DATA_DIR / "cache"
THUMBNAIL_CACHE_DIR = CACHE_DIR / "thumbnails"
IMAGE_COMPRESSION_CACHE_DIR = CACHE_DIR / "image_compression"
STAGING_CACHE_DIR = CACHE_DIR / "staging"
LOG_DIR = DATA_DIR / "logs"
HISTORY_PATH = DATA_DIR / "history.json"

CONFIG_PATH = DATA_DIR / "config.toml"
PACKAGE_TEMPLATE_CONFIG_PATH = RESOURCE_DIR / "resources" / "default_config.toml"
LEGACY_TEMPLATE_CONFIG_PATH = RESOURCE_DIR / "config.example.toml"
TEMPLATE_CONFIG_PATH = (
    PACKAGE_TEMPLATE_CONFIG_PATH
    if PACKAGE_TEMPLATE_CONFIG_PATH.is_file()
    else LEGACY_TEMPLATE_CONFIG_PATH
)
VERSION_PATH = RESOURCE_DIR / "VERSION"
ASSETS_DIR = RESOURCE_DIR / "assets"
TOOLS_DIR = RESOURCE_DIR / "tools"
FFMPEG_DIR = TOOLS_DIR / "ffmpeg"


def read_version(default: str = "0.0.0") -> str:
    """Read the packaged VERSION file without ever failing application boot."""

    try:
        value = VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    return value or str(default)


def ensure_data_dirs() -> Path:
    """Create the standard writable directories and return ``DATA_DIR``."""

    for directory in (
        DATA_DIR,
        VIDEO_STATE_DIR,
        IMAGE_STATE_DIR,
        MIXED_STATE_DIR,
        TELEGRAM_DIR,
        TDLIB_DATABASE_DIR,
        TDLIB_FILES_DIR,
        CAPTIONS_DIR,
        UPLOAD_INFLIGHT_DIR,
        THUMBNAIL_CACHE_DIR,
        IMAGE_COMPRESSION_CACHE_DIR,
        STAGING_CACHE_DIR,
        LOG_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


__all__ = [
    "IS_FROZEN", "RESOURCE_DIR", "APP_ROOT", "DATA_BASE_DIR", "DATA_DIR",
    "APP_DATA_DIR", "CONFIG_PATH", "TEMPLATE_CONFIG_PATH",
    "PACKAGE_TEMPLATE_CONFIG_PATH", "LEGACY_TEMPLATE_CONFIG_PATH", "VERSION_PATH",
    "ASSETS_DIR", "TOOLS_DIR", "FFMPEG_DIR",
    "STATE_DIR", "VIDEO_STATE_DIR", "IMAGE_STATE_DIR", "MIXED_STATE_DIR",
    "TELEGRAM_DIR", "TDLIB_DATABASE_DIR", "TDLIB_FILES_DIR",
    "CAPTIONS_DIR", "UPLOAD_INFLIGHT_DIR", "CACHE_DIR", "THUMBNAIL_CACHE_DIR",
    "IMAGE_COMPRESSION_CACHE_DIR", "STAGING_CACHE_DIR", "LOG_DIR",
    "HISTORY_PATH", "read_version", "ensure_data_dirs",
]
