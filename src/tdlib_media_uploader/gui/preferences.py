"""Versioned desktop preferences stored alongside the application's data."""

import json
from pathlib import Path

from ..config import paths
from ..core.persistence import atomic_write_json


class UiPreferences:
    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else paths.DATA_DIR / "ui_preferences.json"
        self.error = None
        self.theme_mode = "dark"
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema_version", 1) != 1:
                raise ValueError("界面偏好文件版本或格式不受支持")
            mode = value.get("theme_mode")
            if mode in ("light", "dark"):
                self.theme_mode = mode
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            self.error = f"无法读取界面偏好（{self.path}）：{exc}"

    def save_theme(self, mode):
        if self.error:
            raise ValueError(self.error)
        if mode not in ("dark", "light"):
            raise ValueError("无效的主题模式")
        atomic_write_json(self.path, {"schema_version": 1, "theme_mode": mode})
        self.theme_mode = mode
