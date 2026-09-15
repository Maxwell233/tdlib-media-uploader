"""Pure Telegram media and caption limit helpers.

The V1.9 uploaders kept these checks close to their media-specific builders.
This module is deliberately independent of configuration, Qt and TDLib's
native binding so a strategy can make the same decision during scanning and
preflight.  The numeric limits are protocol values, not display-rounded
values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


TELEGRAM_UPLOAD_PART_SIZE_MAX = 524_288
VIDEO_STANDARD_MAX_BYTES = 4000 * TELEGRAM_UPLOAD_PART_SIZE_MAX
VIDEO_PREMIUM_MAX_BYTES = 8000 * TELEGRAM_UPLOAD_PART_SIZE_MAX
IMAGE_MAX_BYTES = 10 * 1024**2
DEFAULT_CAPTION_LENGTH_MAX = 1024


class CaptionLimitError(ValueError):
    """A caption is longer than the limit reported by Telegram."""


@dataclass(frozen=True, slots=True)
class SizeDecision:
    """A stable, UI-neutral result for one media-size check."""

    media_kind: str
    size: int
    limit: int
    status: str
    requires_premium: bool = False
    requires_compression: bool = False

    @property
    def allowed(self) -> bool:
        return self.status == "allowed"


@dataclass(frozen=True, slots=True)
class AccountLimits:
    """Limits discovered from TDLib's ``getOption`` responses."""

    is_premium: bool | None = None
    caption_length_max: int | None = None


def _size(value: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"媒体大小必须是整数：{value!r}") from exc
    if result < 0:
        raise ValueError("媒体大小不能为负数")
    return result


def classify_video_size(
    size: int,
    *,
    is_premium: bool | None = None,
) -> SizeDecision:
    """Classify a video while retaining the exact V1.9 boundaries.

    Files between the standard and Premium ceilings are retained by the scan
    and marked ``requires_premium`` unless the account is known to be Premium.
    Files at either ceiling are allowed; only a byte beyond the Premium
    ceiling is ``oversize``.
    """

    value = _size(size)
    if value > VIDEO_PREMIUM_MAX_BYTES:
        return SizeDecision(
            "video",
            value,
            VIDEO_PREMIUM_MAX_BYTES,
            "oversize",
        )
    if value > VIDEO_STANDARD_MAX_BYTES and is_premium is not True:
        return SizeDecision(
            "video",
            value,
            VIDEO_STANDARD_MAX_BYTES,
            "requires_premium",
            requires_premium=True,
        )
    limit = VIDEO_PREMIUM_MAX_BYTES if is_premium is True else VIDEO_STANDARD_MAX_BYTES
    return SizeDecision("video", value, limit, "allowed")


def video_size_status(size: int, *, is_premium: bool | None = None) -> str:
    """Return the legacy status string used by the V1.9 scanners."""

    return classify_video_size(size, is_premium=is_premium).status


def classify_image_size(
    size: int,
    *,
    compression_enabled: bool = False,
) -> SizeDecision:
    """Classify a photo at the exact 10 MiB boundary.

    An oversized image is not considered immediately uploadable merely
    because compression is enabled.  ``requires_compression`` means the
    upload-time FFmpeg path must produce a compliant temporary copy first.
    """

    value = _size(size)
    if value <= IMAGE_MAX_BYTES:
        return SizeDecision("image", value, IMAGE_MAX_BYTES, "allowed")
    if compression_enabled:
        return SizeDecision(
            "image",
            value,
            IMAGE_MAX_BYTES,
            "requires_compression",
            requires_compression=True,
        )
    return SizeDecision("image", value, IMAGE_MAX_BYTES, "oversize")


def image_size_status(size: int, *, compression_enabled: bool = False) -> str:
    """Return ``allowed``, ``requires_compression`` or ``oversize``."""

    return classify_image_size(size, compression_enabled=compression_enabled).status


def caption_text(value: Any) -> str:
    """Extract caption text from plain text or a TDLib formatted-text object."""

    if isinstance(value, Mapping):
        nested = value.get("caption")
        if isinstance(nested, Mapping):
            value = nested
        text = value.get("text", "")
        return "" if text is None else str(text)
    return "" if value is None else str(value)


def validate_caption(caption: Any, limit: int = DEFAULT_CAPTION_LENGTH_MAX) -> str:
    """Validate without silently truncating user-authored text."""

    try:
        maximum = int(limit)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Caption 限制必须是整数：{limit!r}") from exc
    if maximum < 1:
        raise ValueError("Caption 限制必须大于 0")
    value = caption_text(caption)
    if len(value) > maximum:
        raise CaptionLimitError(
            f"标题超过 Telegram Caption 限制（{len(value)}/{maximum} 字符）"
        )
    return value


def validate_message_caption(
    content: Mapping[str, Any],
    limit: int = DEFAULT_CAPTION_LENGTH_MAX,
) -> str:
    """Validate one TDLib input message content's optional caption."""

    if not isinstance(content, Mapping) or "caption" not in content:
        return ""
    return validate_caption(content.get("caption"), limit)


def validate_contents_captions(
    contents: Any,
    limit: int = DEFAULT_CAPTION_LENGTH_MAX,
) -> tuple[str, ...]:
    """Validate every caption before a send request is issued."""

    return tuple(validate_message_caption(content, limit) for content in contents or ())


def parse_option_boolean(value: Any) -> bool | None:
    """Parse a TDLib ``optionValueBoolean`` or a raw boolean."""

    if isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        option_type = str(value.get("@type", ""))
        if option_type and option_type != "optionValueBoolean":
            return None
        raw = value.get("value")
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in {"true", "false"}:
            return raw.strip().lower() == "true"
    return None


def parse_option_integer(value: Any) -> int | None:
    """Parse a TDLib integer option, returning ``None`` for invalid values."""

    if isinstance(value, Mapping):
        option_type = str(value.get("@type", ""))
        if option_type and option_type != "optionValueInteger":
            return None
        value = value.get("value")
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result > 0 else None


def account_limits_from_options(
    premium_option: Any = None,
    caption_option: Any = None,
    *,
    default_caption_limit: int | None = DEFAULT_CAPTION_LENGTH_MAX,
) -> AccountLimits:
    """Build account limits from independent TDLib ``getOption`` results."""

    caption_limit = parse_option_integer(caption_option)
    if caption_limit is None:
        caption_limit = default_caption_limit
    return AccountLimits(
        is_premium=parse_option_boolean(premium_option),
        caption_length_max=caption_limit,
    )


__all__ = [
    "AccountLimits",
    "CaptionLimitError",
    "DEFAULT_CAPTION_LENGTH_MAX",
    "IMAGE_MAX_BYTES",
    "SizeDecision",
    "TELEGRAM_UPLOAD_PART_SIZE_MAX",
    "VIDEO_PREMIUM_MAX_BYTES",
    "VIDEO_STANDARD_MAX_BYTES",
    "account_limits_from_options",
    "caption_text",
    "classify_image_size",
    "classify_video_size",
    "image_size_status",
    "parse_option_boolean",
    "parse_option_integer",
    "validate_caption",
    "validate_contents_captions",
    "validate_message_caption",
    "video_size_status",
]
