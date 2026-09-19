# -*- coding: utf-8 -*-
"""Settings panels package."""

from __future__ import annotations

from .advanced_panel import AdvancedPanel
from .base import SettingsPanel
from .environment_license_panel import EnvironmentLicensePanel
from .general_panel import GeneralPanel
from .storage_panel import StoragePanel
from .telegram_panel import TelegramPanel
from .upload_panel import UploadPanel

__all__ = [
    "AdvancedPanel",
    "EnvironmentLicensePanel",
    "GeneralPanel",
    "SettingsPanel",
    "StoragePanel",
    "TelegramPanel",
    "UploadPanel",
]
