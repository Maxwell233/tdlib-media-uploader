"""Versioned desktop preferences stored alongside the application's data."""

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

from ..config import paths
from ..core.identity import canonical_target
from ..core.logging import write_app_log
from ..core.persistence import atomic_write_json


def target_label_key(target: Mapping[str, Any] | None) -> str:
    """Return a canonical lookup key for cached Telegram target display names."""
    if not isinstance(target, Mapping) or not target.get("chat_id"):
        return ""
    canonical = canonical_target(dict(target))
    if not canonical:
        return ""
    chat_id = canonical.get("chat_id", 0)
    if not chat_id:
        return ""
    return f"{canonical.get('target_mode', '')}:{chat_id}:{canonical.get('forum_topic_id', 0)}"


class UiPreferences:
    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else paths.DATA_DIR / "ui_preferences.json"
        self.error = None
        self.theme_mode = "dark"
        self.target_labels: dict[str, dict[str, str]] = {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema_version", 1) != 1:
                raise ValueError("界面偏好文件版本或格式不受支持")
            mode = value.get("theme_mode")
            if mode in ("light", "dark"):
                self.theme_mode = mode
            labels = value.get("target_labels")
            if isinstance(labels, dict):
                for key, entry in labels.items():
                    if isinstance(key, str) and isinstance(entry, dict):
                        self.target_labels[key] = {
                            "chat_title": str(entry.get("chat_title", "") or ""),
                            "topic_name": str(entry.get("topic_name", "") or ""),
                        }
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            self.error = f"无法读取界面偏好（{self.path}）：{exc}"

    def _payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"schema_version": 1, "theme_mode": self.theme_mode}
        if self.target_labels:
            payload["target_labels"] = dict(self.target_labels)
        return payload

    def save_theme(self, mode):
        if self.error:
            raise ValueError(self.error)
        if mode not in ("dark", "light"):
            raise ValueError("无效的主题模式")
        previous = self.theme_mode
        self.theme_mode = mode
        try:
            atomic_write_json(self.path, self._payload())
        except Exception:
            self.theme_mode = previous
            raise

    def get_target_label(self, target: Mapping[str, Any] | None) -> dict[str, str]:
        key = target_label_key(target)
        return dict(self.target_labels.get(key, {})) if key else {}

    def save_target_label(
        self,
        target: Mapping[str, Any] | None,
        *,
        chat_title: str = "",
        topic_name: str = "",
    ) -> bool:
        if self.error:
            return False
        key = target_label_key(target)
        if not key:
            return False
        cleaned_chat = str(chat_title or "").strip()
        cleaned_topic = str(topic_name or "").strip()
        if not cleaned_chat and not cleaned_topic:
            return False
        entry = {
            "chat_title": cleaned_chat or self.target_labels.get(key, {}).get("chat_title", ""),
            "topic_name": cleaned_topic or self.target_labels.get(key, {}).get("topic_name", ""),
        }
        if self.target_labels.get(key) == entry:
            return False
        had_previous = key in self.target_labels
        previous = dict(self.target_labels[key]) if had_previous else None
        self.target_labels[key] = entry
        try:
            atomic_write_json(self.path, self._payload())
        except Exception as exc:
            if had_previous and previous is not None:
                self.target_labels[key] = previous
            else:
                self.target_labels.pop(key, None)
            write_app_log(
                "WARNING",
                f"保存 Telegram 目标名称缓存失败（{self.path}）：{exc}",
                source="gui/preferences",
            )
            return False
        return True

    def format_target_summary(self, target: Mapping[str, Any] | None) -> str:
        """Format a compact target string preferring cached names with numeric fallback."""
        if not isinstance(target, Mapping):
            return "未配置"
        chat_id = target.get("chat_id")
        topic_id = target.get("forum_topic_id")
        mode = str(target.get("target_mode", "forum_topic") or "forum_topic")
        labels = self.get_target_label(target)
        chat_text = labels.get("chat_title") or (str(chat_id) if chat_id else "未配置")
        if mode == "channel":
            return f"{chat_text}（频道）" if labels.get("chat_title") else str(chat_id or "未配置")
        topic_text = labels.get("topic_name") or (str(topic_id) if topic_id else "")
        return f"{chat_text} / {topic_text}" if topic_text else chat_text
