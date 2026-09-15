"""Canonical Telegram target parsing and validation.

Forum topics and channels share a TDLib ``chat_id`` field but are different
destinations.  Canonicalization therefore retains only the fields relevant to
the active mode, matching the V1.9 journal identity rules.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping


FORUM_TOPIC = "forum_topic"
CHANNEL = "channel"
TARGET_MODES = frozenset({FORUM_TOPIC, CHANNEL})


class TargetValidationError(ValueError):
    """The configured target or TDLib chat does not match its mode."""


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True, slots=True)
class TelegramTarget:
    """Validated, mode-aware target identity used by TDLib requests."""

    target_mode: str
    chat_id: int
    forum_topic_id: int = 0
    channel_chat_id: int = 0

    @property
    def mode(self) -> str:
        return self.target_mode

    @property
    def is_channel(self) -> bool:
        return self.target_mode == CHANNEL

    def as_dict(self) -> dict[str, int | str]:
        return {
            "target_mode": self.target_mode,
            "chat_id": self.chat_id,
            "forum_topic_id": self.forum_topic_id,
            "channel_chat_id": self.channel_chat_id,
        }

    def topic_object(self) -> dict[str, int | str] | None:
        return build_topic_object(self)

    def identity(self) -> str:
        return target_identity(self)


def canonical_target(target: Any = None) -> dict[str, int | str]:
    """Return the mode-aware canonical target, or ``{}`` when malformed.

    This intentionally mirrors V1.9 ``media_identity.canonical_target``:
    irrelevant fields are ignored and invalid values do not leak into state or
    journal identities.
    """

    if isinstance(target, TelegramTarget):
        return target.as_dict()
    if not isinstance(target, Mapping):
        return {}
    mode = str(target.get("target_mode", target.get("mode", ""))).strip().lower()
    if mode == FORUM_TOPIC:
        chat = _as_int(target.get("chat_id", target.get("group_chat_id", 0)))
        topic = _as_int(target.get("forum_topic_id", 0))
        if chat is None or topic is None:
            return {}
        return {
            "target_mode": FORUM_TOPIC,
            "chat_id": chat,
            "forum_topic_id": topic,
            "channel_chat_id": 0,
        }
    if mode == CHANNEL:
        channel = _as_int(target.get("channel_chat_id", 0))
        if not channel:
            channel = _as_int(target.get("chat_id", target.get("group_chat_id", 0)))
        if channel is None:
            return {}
        return {
            "target_mode": CHANNEL,
            "chat_id": channel,
            "forum_topic_id": 0,
            "channel_chat_id": channel,
        }
    return {}


normalize_target = canonical_target


def parse_target(target: Any, *, require_values: bool = True) -> TelegramTarget:
    """Parse a mapping and optionally enforce non-zero destination values."""

    canonical = canonical_target(target)
    if not canonical:
        raise TargetValidationError(
            'Telegram 目标模式只能是 "forum_topic" 或 "channel"，且目标 ID 必须是整数。'
        )
    mode = str(canonical["target_mode"])
    chat_id = int(canonical["chat_id"])
    topic_id = int(canonical["forum_topic_id"])
    channel_id = int(canonical["channel_chat_id"])
    if require_values:
        if mode == FORUM_TOPIC and chat_id == 0:
            raise TargetValidationError("Forum Topic 模式必须填写 chat_id。")
        if mode == FORUM_TOPIC and topic_id <= 0:
            raise TargetValidationError("Forum Topic 模式必须填写大于 0 的 forum_topic_id。")
        if mode == CHANNEL and channel_id == 0:
            raise TargetValidationError("Channel 模式必须填写 channel_chat_id。")
    return TelegramTarget(mode, chat_id, topic_id, channel_id)


def target_identity(target: Any = None) -> str:
    """Serialize a canonical target for durable state/journal scoping."""

    canonical = canonical_target(target)
    if not canonical:
        return ""
    return json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_topic_object(target: Any) -> dict[str, int | str] | None:
    """Build TDLib's ``topic_id`` object for a forum target."""

    parsed = target if isinstance(target, TelegramTarget) else parse_target(target)
    if parsed.is_channel:
        return None
    return {
        "@type": "messageTopicForum",
        "forum_topic_id": parsed.forum_topic_id,
    }


def _chat_type(chat: Mapping[str, Any]) -> Mapping[str, Any]:
    value = chat.get("type")
    return value if isinstance(value, Mapping) else {}


def validate_chat_for_target(chat: Any, target: Any) -> TelegramTarget:
    """Validate TDLib's chat type against the configured target mode."""

    parsed = parse_target(target)
    if not isinstance(chat, Mapping):
        raise TargetValidationError("TDLib 未返回有效的目标聊天。")
    chat_type = _chat_type(chat)
    if chat_type.get("@type") != "chatTypeSupergroup":
        raise TargetValidationError("目标聊天必须是超级群组或频道。")
    is_channel = bool(chat_type.get("is_channel", False))
    if parsed.is_channel and not is_channel:
        raise TargetValidationError("当前 Chat ID 不是频道，请选择正确的频道目标。")
    if not parsed.is_channel and is_channel:
        raise TargetValidationError("当前 Chat ID 是频道；请切换目标模式为 Channel 频道。")
    returned_id = _as_int(chat.get("id"))
    if returned_id is None or returned_id != parsed.chat_id:
        raise TargetValidationError(
            f"TDLib 返回的聊天 ID 与目标不一致：{returned_id} != {parsed.chat_id}。"
        )
    return parsed


def validate_forum_topic(topic: Any, target: Any) -> TelegramTarget:
    """Validate a resolved forum topic without performing a TDLib request."""

    parsed = parse_target(target)
    if parsed.is_channel:
        raise TargetValidationError("Channel 目标不应携带 forum topic。")
    if not isinstance(topic, Mapping):
        raise TargetValidationError("TDLib 未返回有效的 Forum Topic。")
    topic_id = _as_int(topic.get("forum_topic_id"))
    if "forum_topic_id" not in topic:
        info = topic.get("info")
        if isinstance(info, Mapping):
            topic_id = _as_int(info.get("forum_topic_id"))
    if topic_id is None or topic_id != parsed.forum_topic_id:
        raise TargetValidationError(
            f"TDLib 返回的 Topic ID 与目标不一致：{topic_id} != {parsed.forum_topic_id}。"
        )
    return parsed


def validate_target(
    chat: Any,
    target: Any,
    *,
    topic: Any = None,
) -> TelegramTarget:
    """Validate a chat and, for Forum Topic mode, its resolved topic."""

    parsed = validate_chat_for_target(chat, target)
    if not parsed.is_channel:
        if topic is None:
            raise TargetValidationError("Forum Topic 目标缺少已解析的 Topic。")
        validate_forum_topic(topic, parsed)
    return parsed


__all__ = [
    "CHANNEL",
    "FORUM_TOPIC",
    "TARGET_MODES",
    "TargetValidationError",
    "TelegramTarget",
    "build_topic_object",
    "canonical_target",
    "normalize_target",
    "parse_target",
    "target_identity",
    "validate_chat_for_target",
    "validate_forum_topic",
    "validate_target",
]
