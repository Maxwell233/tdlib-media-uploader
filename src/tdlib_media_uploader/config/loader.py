# -*- coding: utf-8 -*-
"""统一读取 config.toml。正常使用时无需修改本文件。"""

from __future__ import annotations

import math
import os
import sys
import tomllib
from pathlib import Path

from .paths import (
    APP_DATA_DIR,
    CONFIG_PATH,
    DATA_DIR,
    RESOURCE_DIR,
    TEMPLATE_CONFIG_PATH,
    TDLIB_DATABASE_DIR,
    TDLIB_FILES_DIR,
    read_version,
)

APP_VERSION = read_version()
PROJECT_DIR = RESOURCE_DIR

# Telegram's current upload limits used by this application.  Keep these
# checks local so an oversized file is reported during scanning instead of
# failing later inside TDLib.
# Telegram calculates the upload ceiling from the maximum upload part size
# and the account's allowed part count.  Keep the exact byte values here so
# scan-time checks match the server boundary (the UI presents these as about
# 2 GB and about 4 GB for readability).
TELEGRAM_UPLOAD_PART_SIZE_MAX = 524_288
VIDEO_STANDARD_MAX_BYTES = 4000 * TELEGRAM_UPLOAD_PART_SIZE_MAX
VIDEO_PREMIUM_MAX_BYTES = 8000 * TELEGRAM_UPLOAD_PART_SIZE_MAX
VIDEO_MAX_BYTES = VIDEO_PREMIUM_MAX_BYTES
IMAGE_MAX_BYTES = 10 * 1024 ** 2
IMAGE_COMPRESSION_TARGET_BYTES = int(9.5 * 1024 ** 2)


def video_size_status(size: int, *, is_premium: bool | None = None) -> str:
    """Classify a video without changing the complete Album plan.

    ``requires_premium`` is intentionally returned for about 2–4 GB files while
    the scanner still includes them.  The upload worker filters them only
    after TDLib reports the account status.
    """

    value = int(size)
    if value > VIDEO_PREMIUM_MAX_BYTES:
        return "oversize"
    if value > VIDEO_STANDARD_MAX_BYTES and is_premium is not True:
        return "requires_premium"
    return "allowed"


def _load():
    # The packaged health check must be independent of any user configuration
    # (including a partially written or invalid one).  Read the immutable
    # bundled template in memory and never create or modify ``data/config``.
    if "--self-test" in sys.argv[1:]:
        template = TEMPLATE_CONFIG_PATH
        if template.exists():
            try:
                with template.open("rb") as file:
                    return tomllib.load(file)
            except (OSError, tomllib.TOMLDecodeError) as error:
                raise RuntimeError(f"配置模板读取失败：{template}\n{error}") from error
    if not CONFIG_PATH.exists():
        # A fresh V1.9 installation starts with a writable data directory.
        # Normal startup copies the bundled template into the writable data
        # directory on first launch.
        template = TEMPLATE_CONFIG_PATH
        # Never look for or import a config from the old application root.
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if template.exists():
                import shutil
                shutil.copyfile(template, CONFIG_PATH)
        except OSError:
            pass
        if not CONFIG_PATH.exists():
            raise RuntimeError(
                "找不到配置模板：\n"
                f"{CONFIG_PATH}\n\n"
                "请确认默认配置资源已随程序一起安装。"
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


def _resolve_path(value, *, base=None):
    text = str(value).strip()

    if not text:
        raise RuntimeError(
            "config.toml 中存在空路径。"
        )

    path = Path(os.path.expandvars(os.path.expanduser(text)))

    if path.is_absolute():
        return path

    return Path(base or DATA_DIR) / path


def _resolve_resource_path(value):
    """Resolve a bundled tool before falling back to the writable data root."""

    text = str(value).strip()
    if not text:
        raise RuntimeError("config.toml 中存在空路径。")
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    if path.is_absolute():
        return path
    bundled = RESOURCE_DIR / path
    if bundled.exists():
        return bundled
    return DATA_DIR / path


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


def _bounded_int(section, key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(section.get(key, default))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"config.toml 中的 {key} 必须是整数。") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(
            f"config.toml 中的 {key} 必须在 {minimum}~{maximum} 范围内。"
        )
    return value


def _bounded_float(section, key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(section.get(key, default))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"config.toml 中的 {key} 必须是数字。") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(
            f"config.toml 中的 {key} 必须在 {minimum}~{maximum} 范围内。"
        )
    return value


def _thumbnail_timestamp_seconds(section, key: str, default: float = 1.0) -> float:
    """Read a finite, non-negative thumbnail timestamp at centisecond precision."""

    try:
        value = float(section.get(key, default))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"config.toml 中的 {key} 必须是数字。") from exc
    if not math.isfinite(value) or value < 0:
        raise RuntimeError(
            f"config.toml 中的 {key} 必须是大于等于 0 的有限数字。"
        )
    try:
        centiseconds = round(value * 100)
    except (OverflowError, ValueError) as exc:
        raise RuntimeError(f"config.toml 中的 {key} 数值过大。") from exc
    return centiseconds / 100


telegram = _section("telegram")
paths = _section("paths")
video = _section("video")
image = _section("image")
mixed = _optional_section("mixed")
tdlib = _section("tdlib")
proxy = _optional_section("proxy")
staging = _optional_section("staging")
scan = _optional_section("scan")
process = _optional_section("process")


# Telegram
API_ID = int(_required(telegram, "api_id"))
API_HASH = str(_required(telegram, "api_hash")).strip()


def _target_section(name: str):
    value = telegram.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError(f"config.toml 中的 [telegram.{name}] 配置段必须是表格。")
    return value


def _int_target_value(source, fallback, key: str) -> int:
    try:
        return int(source.get(key, fallback) or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Telegram 目标配置中的 {key} 必须是整数。") from exc


def _parse_target(source: dict, fallback: dict | None = None) -> dict:
    fallback = fallback or {}
    mode = str(source.get("target_mode", fallback.get("target_mode", "forum_topic"))).strip().lower()
    if mode not in {"forum_topic", "channel"}:
        raise RuntimeError('[telegram].target_mode 只能是 "forum_topic" 或 "channel"。')
    group_chat_id = _int_target_value(source, fallback.get("group_chat_id", 0), "chat_id")
    channel_chat_id = _int_target_value(source, fallback.get("channel_chat_id", 0), "channel_chat_id")
    forum_topic_id = _int_target_value(source, fallback.get("forum_topic_id", 0), "forum_topic_id")
    return {
        "target_mode": mode,
        "group_chat_id": group_chat_id,
        "channel_chat_id": channel_chat_id,
        "forum_topic_id": forum_topic_id,
        "chat_id": channel_chat_id if mode == "channel" else group_chat_id,
    }


_BASE_TARGET = _parse_target(telegram)
if _BASE_TARGET["target_mode"] == "channel" and _BASE_TARGET["channel_chat_id"] == 0:
    raise RuntimeError("频道模式必须填写 [telegram].channel_chat_id。")
if _BASE_TARGET["target_mode"] == "forum_topic":
    if "chat_id" not in telegram:
        raise RuntimeError("config.toml 缺少配置项：chat_id")
    if "forum_topic_id" not in telegram:
        raise RuntimeError("config.toml 缺少配置项：forum_topic_id")

_TARGETS = {
    "video": _parse_target(_target_section("video"), _BASE_TARGET),
    "image": _parse_target(_target_section("image"), _BASE_TARGET),
    "mixed": _parse_target(_target_section("mixed"), _BASE_TARGET),
}


def target_for(kind: str) -> dict:
    """Return the effective target for a media upload type."""
    normalized = str(kind).strip().lower()
    if normalized not in {"video", "image", "mixed"}:
        raise ValueError(f"未知媒体类型：{kind}")
    return dict(_TARGETS[normalized])


def activate_target(kind: str) -> dict:
    """Activate a media-specific target for the synchronous upload core."""
    target = target_for(kind)
    globals()["GROUP_CHAT_ID"] = target["group_chat_id"]
    globals()["CHANNEL_CHAT_ID"] = target["channel_chat_id"]
    globals()["TARGET_MODE"] = target["target_mode"]
    globals()["CHAT_ID"] = target["chat_id"]
    globals()["FORUM_TOPIC_ID"] = target["forum_topic_id"]
    return target


# Keep the historical flat constants available for callers that do not select
# a media type explicitly. GUI workers activate the relevant media target.
GROUP_CHAT_ID = _BASE_TARGET["group_chat_id"]
CHANNEL_CHAT_ID = _BASE_TARGET["channel_chat_id"]
TARGET_MODE = _BASE_TARGET["target_mode"]
CHAT_ID = _BASE_TARGET["chat_id"]
FORUM_TOPIC_ID = _BASE_TARGET["forum_topic_id"]

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

EXIFTOOL_PATH = _resolve_resource_path(
    _required(
        paths,
        "exiftool_path"
    )
)
# Keep the shared template usable on Windows too: its extensionless path is
# convenient on macOS, while the Windows download is conventionally named
# ``exiftool.exe``.
if os.name == "nt" and not EXIFTOOL_PATH.exists() and EXIFTOOL_PATH.suffix.lower() != ".exe":
    windows_exiftool = EXIFTOOL_PATH.with_name(EXIFTOOL_PATH.name + ".exe")
    if windows_exiftool.exists():
        EXIFTOOL_PATH = windows_exiftool


# 视频
VIDEO_EXTENSIONS = _extensions(
    _required(
        video,
        "extensions"
    )
)

VIDEO_SORT_MODE = str(
    video.get(
        "sort_mode",
        "mtime",
    )
).strip().lower()
if VIDEO_SORT_MODE not in {"mtime", "name"}:
    raise RuntimeError(
        '[video].sort_mode 只能是 "mtime" 或 "name"。'
    )

# 关闭后完全跳过 EXIF、媒体创建日期和文件修改时间读取。为了让“仅按
# 文件名处理”保持确定性，实际排序和分组会固定为文件名/固定分组。
VIDEO_READ_DATES = bool(
    video.get(
        "read_dates",
        True,
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

# 默认开启媒体创建日期以保持旧版本读取 QuickTime 日期的行为。扫描先由
# ExifTool 一次批量读取；缺少可用 EXIF 的视频才会使用 FFmpeg 回退。关闭后
# 仍优先使用 EXIF 日期，但跳过媒体日期回退，扫描更快。read_dates 关闭时
# 所有日期读取都会跳过。
VIDEO_READ_MEDIA_CREATION_DATE = bool(
    video.get(
        "read_media_creation_date",
        True,
    )
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

if not (1 <= VIDEO_ALBUM_SIZE <= 10):
    raise RuntimeError(
        "[video].album_size 必须为 1~10。"
    )

# Video grouping keeps the historical date mode and adds a configurable
# scan-order mode.  The legacy flag is accepted as a migration fallback.
_legacy_force_ten = bool(video.get("force_ten_per_album", False))
VIDEO_GROUP_MODE = str(
    video.get(
        "group_mode",
        "fixed" if _legacy_force_ten else "date",
    )
).strip().lower()
if VIDEO_GROUP_MODE not in {"date", "fixed"}:
    raise RuntimeError(
        '[video].group_mode 只能是 "date" 或 "fixed"。'
    )

if not VIDEO_READ_DATES:
    VIDEO_SORT_MODE = "name"
    VIDEO_GROUP_MODE = "fixed"

# Keep the old constant available to extensions and older integrations.
VIDEO_FORCE_TEN_PER_ALBUM = VIDEO_GROUP_MODE == "fixed"

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

VIDEO_THUMBNAIL_TIMESTAMP_SECONDS = _thumbnail_timestamp_seconds(
    video,
    "thumbnail_timestamp_seconds",
    1.0,
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
        video.get("validate_media", False),
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
VIDEO_CAPTION_INCLUDE_FILENAME_NUMBERS = bool(
    video.get("caption_include_filename_numbers", True)
)
VIDEO_CAPTION_INCLUDE_GROUP_TITLE = bool(
    video.get("caption_include_group_title", True)
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

if IMAGE_SORT_MODE == "path":
    # ``path`` was the image-only spelling before the shared media sorter.
    # Normalize it immediately so newly written configuration is consistent.
    IMAGE_SORT_MODE = "name"
if IMAGE_SORT_MODE not in {"mtime", "name"}:
    raise RuntimeError(
        '[image].sort_mode 只能是 '
        '"mtime" 或 "name"。'
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
        image.get("validate_media", False),
    )
)

IMAGE_RESET_STATE = bool(
    image.get(
        "reset_state",
        False
    )
)

IMAGE_COMPRESS_OVERSIZE = bool(
    image.get(
        "compress_oversize",
        False,
    )
)


# 扫描/媒体读取的共享 I/O 参数。新配置按本地/网络盘分别设置；旧配置
# 的全局字段继续作为两者的兼容回退。
SCAN_STABILITY_CHECKS = _bounded_int(scan, "stability_checks", 2, 1, 8)
SCAN_STABILITY_INTERVAL_SECONDS = _bounded_float(
    scan, "stability_interval_seconds", 0.05, 0.0, 5.0
)
SCAN_STABILITY_CHECKS_LOCAL = _bounded_int(
    scan, "stability_checks_local", SCAN_STABILITY_CHECKS, 1, 8
)
SCAN_STABILITY_INTERVAL_LOCAL_SECONDS = _bounded_float(
    scan,
    "stability_interval_local_seconds",
    SCAN_STABILITY_INTERVAL_SECONDS,
    0.0,
    30.0,
)
SCAN_STABILITY_CHECKS_NETWORK = _bounded_int(
    scan,
    "stability_checks_network",
    SCAN_STABILITY_CHECKS if "stability_checks" in scan else 3,
    1,
    8,
)
SCAN_STABILITY_INTERVAL_NETWORK_SECONDS = _bounded_float(
    scan,
    "stability_interval_network_seconds",
    SCAN_STABILITY_INTERVAL_SECONDS if "stability_interval_seconds" in scan else 0.5,
    0.0,
    60.0,
)
SCAN_DISCOVERY_ATTEMPTS = _bounded_int(scan, "discovery_attempts", 3, 1, 8)
SCAN_DISCOVERY_INITIAL_DELAY_SECONDS = _bounded_float(
    scan, "discovery_initial_delay_seconds", 0.15, 0.0, 10.0
)
SCAN_DISCOVERY_MAX_DELAY_SECONDS = _bounded_float(
    scan, "discovery_max_delay_seconds", 1.0, 0.0, 60.0
)
SCAN_READINESS_ATTEMPTS = _bounded_int(scan, "readiness_attempts", 3, 1, 8)
SCAN_READ_PROBE_BYTES = _bounded_int(scan, "read_probe_bytes", 64 * 1024, 1, 4 * 1024 * 1024)
IO_WORKERS_LOCAL = _bounded_int(scan, "io_workers_local", 4, 1, 32)
IO_WORKERS_NETWORK = _bounded_int(scan, "io_workers_network", 2, 1, 16)


# 外部媒体工具必须有上限；将超时集中到 [process]，避免各入口出现不一致。
EXIFTOOL_TIMEOUT_SECONDS = _bounded_float(
    process, "exiftool_timeout_seconds", 120.0, 1.0, 86_400.0
)
FFMPEG_METADATA_TIMEOUT_SECONDS = _bounded_float(
    process, "ffmpeg_metadata_timeout_seconds", 30.0, 1.0, 86_400.0
)
FFMPEG_INFO_TIMEOUT_SECONDS = _bounded_float(
    process, "ffmpeg_info_timeout_seconds", 30.0, 1.0, 86_400.0
)
FFMPEG_THUMBNAIL_TIMEOUT_SECONDS = _bounded_float(
    process, "ffmpeg_thumbnail_timeout_seconds", 45.0, 1.0, 86_400.0
)
FFMPEG_COMPRESSION_TIMEOUT_SECONDS = _bounded_float(
    process,
    "ffmpeg_compression_timeout_seconds",
    float(process.get("ffmpeg_compress_timeout_seconds", 45.0)),
    1.0,
    86_400.0,
)
EXIFTOOL_BATCH_SIZE = _bounded_int(process, "exiftool_batch_size", 256, 1, 4096)
EXIFTOOL_RETRIES = _bounded_int(process, "exiftool_retries", 2, 0, 5)


# 混合上传：未填写 [mixed] 时使用公共目标和默认设置。混合目录下的一级
# 子目录是独立组，图片和视频直接继承 [image]/[video] 的扩展名与上限。
MIXED_DIR = _resolve_path(paths.get("mixed_dir", "Mixed"))
# Keep these public names for older integrations, but deliberately derive them
# from the primary media settings so mixed uploads cannot drift from the image
# and video pages. Legacy mixed.image_extensions/video_extensions keys are
# ignored after the inheritance change.
MIXED_IMAGE_EXTENSIONS = set(IMAGE_EXTENSIONS)
MIXED_VIDEO_EXTENSIONS = set(VIDEO_EXTENSIONS)
MIXED_EXTENSIONS = MIXED_IMAGE_EXTENSIONS | MIXED_VIDEO_EXTENSIONS
MIXED_ALBUM_SIZE = int(mixed.get("album_size", 10))
if not 1 <= MIXED_ALBUM_SIZE <= 10:
    raise RuntimeError("[mixed].album_size 必须为 1~10。")
MIXED_SORT_MODE = str(mixed.get("sort_mode", "name")).strip().lower()
if MIXED_SORT_MODE not in {"name", "mtime"}:
    raise RuntimeError('[mixed].sort_mode 只能是 "name" 或 "mtime"。')
MIXED_CAPTION_INCLUDE_GROUP_TITLE = bool(
    mixed.get("caption_include_group_title", True)
)
MIXED_CAPTION_INCLUDE_FILENAMES = bool(
    mixed.get("caption_include_filenames", False)
)
MIXED_CAPTION_INCLUDE_FILENAME_NUMBERS = bool(
    mixed.get("caption_include_filename_numbers", True)
)
MIXED_ALBUM_CAPTION_SEPARATOR = str(
    mixed.get("album_caption_separator", " · ")
)
MIXED_GENERATE_THUMBNAIL = bool(
    mixed.get("generate_thumbnail", VIDEO_GENERATE_THUMBNAIL)
)
MIXED_THUMBNAIL_TIMESTAMP_SECONDS = _thumbnail_timestamp_seconds(
    mixed,
    "thumbnail_timestamp_seconds",
    VIDEO_THUMBNAIL_TIMESTAMP_SECONDS,
)
MIXED_VERIFY_MEDIA = bool(
    mixed.get("verify_media_before_upload", mixed.get("validate_media", False))
)
MIXED_RESET_STATE = bool(mixed.get("reset_state", False))


# 可选本地暂存：源文件仍以原路径参与断点和标题键，发送前才复制到本地
# 缓存，适合不稳定的 SMB/NAS。旧配置没有此段时保持关闭。
# ``enabled`` remains the legacy switch.  The new mode lets callers stage only
# network paths while preserving the old true/false behavior exactly.
_legacy_staging_enabled = bool(staging.get("enabled", False))
STAGING_MODE = str(
    staging.get("mode", "always" if _legacy_staging_enabled else "off")
).strip().lower()
if STAGING_MODE not in {"off", "network", "always"}:
    raise RuntimeError('[staging].mode 只能是 "off"、"network" 或 "always"。')
STAGING_ENABLED = STAGING_MODE != "off"
_staging_value = str(staging.get("directory", "cache/staging")).strip()
if not _staging_value:
    raise RuntimeError("config.toml 中的 staging.directory 不能为空。")
STAGING_DIR = Path(os.path.expandvars(os.path.expanduser(_staging_value)))
if not STAGING_DIR.is_absolute():
    STAGING_DIR = DATA_DIR / STAGING_DIR
# A user supplied value is a base directory.  Only this managed child is ever
# touched by cleanup; arbitrary files in the base remain safe.
STAGING_BASE_DIR = STAGING_DIR
if STAGING_DIR != DATA_DIR / "cache" / "staging" and STAGING_DIR.name != ".tdlib-media-uploader-staging":
    STAGING_DIR = STAGING_DIR / ".tdlib-media-uploader-staging"
STAGING_CLEANUP_ON_START = bool(staging.get("cleanup_on_start", True))
STAGING_CLEANUP_DAYS = _bounded_int(staging, "cleanup_days", 7, 0, 3650)
STAGING_CLEANUP_AFTER_SUCCESS = bool(staging.get("cleanup_after_success", True))


def _path_contains(parent: Path, child: Path) -> bool:
    """Lexically compare paths without resolving network mounts or links."""

    try:
        parent_parts = tuple(os.path.normcase(os.path.abspath(str(parent))).split(os.sep))
        child_parts = tuple(os.path.normcase(os.path.abspath(str(child))).split(os.sep))
        return len(child_parts) >= len(parent_parts) and child_parts[:len(parent_parts)] == parent_parts
    except (OSError, ValueError):
        return False


for _source_root_name, _source_root in (
    ("video_dir", VIDEO_DIR),
    ("image_dir", IMAGE_DIR),
    ("mixed_dir", MIXED_DIR),
):
    if _path_contains(_source_root, STAGING_DIR) or _path_contains(STAGING_DIR, _source_root):
        raise RuntimeError(
            f"staging.directory 不能与 {_source_root_name} 重叠，请选择独立的本地缓存目录。"
        )


# [image] 与 [video] 的扩展名必须互斥，否则 mixed 无法确定媒体类型。
EXTENSION_CONFLICTS = IMAGE_EXTENSIONS & VIDEO_EXTENSIONS
if EXTENSION_CONFLICTS:
    values = ", ".join(sorted(EXTENSION_CONFLICTS))
    raise RuntimeError(
        f"图片和视频扩展名不能重复（{values}）。请从 [image].extensions 或 [video].extensions 中移除冲突项。"
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
