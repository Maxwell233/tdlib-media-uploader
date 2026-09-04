# -*- coding: utf-8 -*-
"""统一读取 config.toml。正常使用时无需修改本文件。"""

from __future__ import annotations

import tomllib
from pathlib import Path

APP_VERSION = "1.8.0"

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.toml"
TEMPLATE_CONFIG_PATH = PROJECT_DIR / "config.example.toml"


def _load():
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            "找不到配置文件：\n"
            f"{CONFIG_PATH}\n\n"
            "请先复制 config.example.toml 为 config.toml，或运行 .\\setup.ps1 / .\\run.ps1 自动创建。"
        )

    try:
        with CONFIG_PATH.open("rb") as file:
            return tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise RuntimeError(
            "config.toml 格式错误：\n"
            f"{error}"
        ) from error


CONFIG = _load()


def _section(name: str):
    value = CONFIG.get(name)

    if not isinstance(value, dict):
        raise RuntimeError(
            f"config.toml 缺少 [{name}] 配置段。"
        )

    return value


def _optional_section(name: str):
    """Return an optional TOML section while keeping old configs compatible."""
    value = CONFIG.get(name, {})
    if not isinstance(value, dict):
        raise RuntimeError(f"config.toml 中的 [{name}] 配置段必须是表格。")
    return value


def _required(section, key):
    if key not in section:
        raise RuntimeError(
            f"config.toml 缺少配置项：{key}"
        )

    return section[key]


def _resolve_path(value):
    text = str(value).strip()

    if not text:
        raise RuntimeError(
            "config.toml 中存在空路径。"
        )

    path = Path(text)

    if path.is_absolute():
        return path

    return PROJECT_DIR / path


def _extensions(values):
    if not isinstance(values, list):
        raise RuntimeError(
            "extensions 必须写成 TOML 数组，"
            '例如 [".mp4", ".mov"]。'
        )

    result = set()

    for value in values:
        ext = str(value).strip().lower()

        if not ext:
            continue

        if not ext.startswith("."):
            ext = "." + ext

        result.add(ext)

    if not result:
        raise RuntimeError(
            "extensions 不能为空。"
        )

    return result


telegram = _section("telegram")
paths = _section("paths")
video = _section("video")
image = _section("image")
tdlib = _section("tdlib")
proxy = _optional_section("proxy")


# Telegram
API_ID = int(_required(telegram, "api_id"))
API_HASH = str(_required(telegram, "api_hash"))
GROUP_CHAT_ID = int(telegram.get("chat_id", 0) or 0)
CHANNEL_CHAT_ID = int(telegram.get("channel_chat_id", 0) or 0)
TARGET_MODE = str(telegram.get("target_mode", "forum_topic")).strip().lower()
if TARGET_MODE not in {"forum_topic", "channel"}:
    raise RuntimeError('[telegram].target_mode 只能是 "forum_topic" 或 "channel"。')
if TARGET_MODE == "channel":
    if CHANNEL_CHAT_ID == 0:
        raise RuntimeError("频道模式必须填写 [telegram].channel_chat_id。")
    CHAT_ID = CHANNEL_CHAT_ID
else:
    if "chat_id" not in telegram:
        raise RuntimeError("config.toml 缺少配置项：chat_id")
    CHAT_ID = GROUP_CHAT_ID
FORUM_TOPIC_ID = int(telegram.get("forum_topic_id", 0) or 0)
if TARGET_MODE == "forum_topic" and "forum_topic_id" not in telegram:
    raise RuntimeError("config.toml 缺少配置项：forum_topic_id")

# 路径
VIDEO_DIR = _resolve_path(
    _required(
        paths,
        "video_dir"
    )
)

IMAGE_DIR = _resolve_path(
    _required(
        paths,
        "image_dir"
    )
)

EXIFTOOL_PATH = _resolve_path(
    _required(
        paths,
        "exiftool_path"
    )
)


# 视频
VIDEO_EXTENSIONS = _extensions(
    _required(
        video,
        "extensions"
    )
)

VIDEO_MISSING_DATE_POLICY = str(
    video.get(
        "missing_date_policy",
        "mtime"
    )
).strip().lower()

if VIDEO_MISSING_DATE_POLICY not in {
    "error",
    "mtime",
}:
    raise RuntimeError(
        '[video].missing_date_policy 只能是 '
        '"error" 或 "mtime"。'
    )

_video_zone = str(
    video.get(
        "quicktime_utc_target_zone",
        ""
    )
).strip()

VIDEO_QUICKTIME_UTC_TARGET_ZONE = (
    _video_zone
    if _video_zone
    else None
)

VIDEO_ALBUM_SIZE = int(
    video.get(
        "album_size",
        10
    )
)

if not (
    1
    <=
    VIDEO_ALBUM_SIZE
    <=
    10
):
    raise RuntimeError(
        "[video].album_size 必须为 1~10。"
    )

VIDEO_CAPTION_YEAR_DIGITS = int(
    video.get(
        "caption_year_digits",
        2
    )
)

VIDEO_GENERATE_THUMBNAIL = bool(
    video.get(
        "generate_thumbnail",
        True
    )
)

VIDEO_THUMB_MAX_EDGE = int(
    video.get(
        "thumb_max_edge",
        320
    )
)

VIDEO_THUMB_TARGET_BYTES = int(
    video.get(
        "thumb_target_kib",
        80
    )
) * 1024

VIDEO_SHOW_FILE_LIST = bool(
    video.get(
        "show_file_list",
        True
    )
)

VIDEO_VERIFY_ALL_METADATA = bool(
    video.get(
        "verify_all_metadata_before_upload",
        False
    )
)

VIDEO_RESET_STATE = bool(
    video.get(
        "reset_state",
        False
    )
)


# 图片
IMAGE_EXTENSIONS = _extensions(
    _required(
        image,
        "extensions"
    )
)

IMAGE_ALBUM_SIZE = int(
    image.get(
        "album_size",
        10
    )
)

if not (
    1
    <=
    IMAGE_ALBUM_SIZE
    <=
    10
):
    raise RuntimeError(
        "[image].album_size 必须为 1~10。"
    )

IMAGE_ALBUM_NUMBERING = bool(image.get("album_numbering", True))
IMAGE_ALBUM_NUMBER_START = int(image.get("album_number_start", 1))
if IMAGE_ALBUM_NUMBER_START < 1:
    raise RuntimeError("[image].album_number_start 必须大于等于 1。")
IMAGE_ALBUM_CAPTION_SEPARATOR = str(
    image.get("album_caption_separator", " · ")
)

VIDEO_ALBUM_CAPTION_SEPARATOR = str(
    video.get("album_caption_separator", " · ")
)
VIDEO_CAPTION_INCLUDE_FILENAMES = bool(
    video.get("caption_include_filenames", False)
)
IMAGE_CAPTION_INCLUDE_FILENAMES = bool(
    image.get("caption_include_filenames", False)
)

IMAGE_SORT_MODE = str(
    image.get(
        "sort_mode",
        "mtime"
    )
).strip().lower()

if IMAGE_SORT_MODE not in {
    "mtime",
    "path",
}:
    raise RuntimeError(
        '[image].sort_mode 只能是 '
        '"mtime" 或 "path"。'
    )

IMAGE_SHOW_FILE_LIST = bool(
    image.get(
        "show_file_list",
        True
    )
)

IMAGE_VERIFY_ALL_IMAGES = bool(
    image.get(
        "verify_all_images_before_upload",
        False
    )
)

IMAGE_RESET_STATE = bool(
    image.get(
        "reset_state",
        False
    )
)


# 网络代理（由 TDLib 原生处理；默认关闭时明确使用直连）
PROXY_ENABLED = bool(proxy.get("enabled", False))
PROXY_TYPE = str(proxy.get("type", "socks5")).strip().lower()
PROXY_SERVER = str(proxy.get("server", "")).strip()
try:
    PROXY_PORT = int(proxy.get("port", 1080))
except (TypeError, ValueError) as exc:
    if PROXY_ENABLED:
        raise RuntimeError("启用代理时 [proxy].port 必须是整数。") from exc
    PROXY_PORT = 1080
PROXY_USERNAME = str(proxy.get("username", ""))
PROXY_PASSWORD = str(proxy.get("password", ""))
PROXY_SECRET = str(proxy.get("secret", "")).strip()
PROXY_HTTP_ONLY = bool(proxy.get("http_only", False))

if PROXY_TYPE not in {"socks5", "http", "mtproto"}:
    if PROXY_ENABLED:
        raise RuntimeError(
            '[proxy].type 只能是 "socks5"、"http" 或 "mtproto"。'
        )
    PROXY_TYPE = "socks5"

if not 1 <= PROXY_PORT <= 65535:
    if PROXY_ENABLED:
        raise RuntimeError("[proxy].port 必须为 1~65535。")
    PROXY_PORT = 1080

if PROXY_ENABLED:
    if not PROXY_SERVER:
        raise RuntimeError("启用代理时必须填写 [proxy].server。")
    if PROXY_TYPE == "mtproto" and not PROXY_SECRET:
        raise RuntimeError("使用 MTProto 代理时必须填写 [proxy].secret。")


# TDLib
TDLIB_LOG_VERBOSITY = int(
    tdlib.get(
        "log_verbosity",
        1
    )
)

TDLIB_REQUEST_TIMEOUT = int(
    tdlib.get(
        "request_timeout_seconds",
        120
    )
)

TDLIB_MESSAGE_SEND_TIMEOUT = int(
    tdlib.get(
        "message_send_timeout_seconds",
        86400
    )
)

TDLIB_DATABASE_ENCRYPTION_KEY = str(
    tdlib.get(
        "database_encryption_key",
        ""
    )
)

TDLIB_USE_FILE_DATABASE = bool(
    tdlib.get(
        "use_file_database",
        True
    )
)

TDLIB_USE_CHAT_INFO_DATABASE = bool(
    tdlib.get(
        "use_chat_info_database",
        True
    )
)

TDLIB_USE_MESSAGE_DATABASE = bool(
    tdlib.get(
        "use_message_database",
        False
    )
)
