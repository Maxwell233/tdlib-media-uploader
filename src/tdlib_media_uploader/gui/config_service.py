# -*- coding: utf-8 -*-
"""Configuration management and TOML persistence service for the GUI layer."""

from __future__ import annotations

import functools
import importlib
import json
import os
from pathlib import Path
import re
import sys
import tomllib
from typing import Any

from ..config.paths import CONFIG_PATH, RESOURCE_DIR, TEMPLATE_CONFIG_PATH
from ..core.persistence import atomic_write_text
from .tools import KIND_PATH_KEYS, require_kind

PROJECT_DIR = RESOURCE_DIR


def ensure_config_file() -> bool:
    """Ensure the user config.toml exists on startup, copying the bundled template."""
    if "--self-test" in sys.argv[1:]:
        return False
    if CONFIG_PATH.exists():
        if os.name == "posix":
            CONFIG_PATH.chmod(0o600)
        return False
    if not TEMPLATE_CONFIG_PATH.exists():
        return False
    atomic_write_text(CONFIG_PATH, TEMPLATE_CONFIG_PATH.read_text(encoding="utf-8"))
    return True


_CONFIG_CREATED = False
_CONFIG_ERROR = ""
cfg = None
try:
    # GUI imports must remain safe on a fresh or unwritable installation.
    # Creation is deferred until main() can display a startup error dialog.
    if CONFIG_PATH.exists() or "--self-test" in sys.argv[1:]:
        from ..config import loader as cfg
except Exception as exc:
    _CONFIG_ERROR = str(exc)


def initialize_config() -> str:
    """Create and load the user config at the controlled GUI startup boundary."""

    global cfg, _CONFIG_CREATED, _CONFIG_ERROR
    try:
        _CONFIG_CREATED = ensure_config_file() or _CONFIG_CREATED
    except OSError as exc:
        cfg = None
        _CONFIG_ERROR = f"无法初始化配置文件：{CONFIG_PATH}\n{type(exc).__name__}: {exc}"
        return _CONFIG_ERROR
    return reload_config()


def reload_config() -> str:
    """Reload the config.loader module in-place, returning an error string on failure."""
    global cfg, _CONFIG_ERROR
    try:
        if "tdlib_media_uploader.config.loader" in sys.modules:
            cfg = importlib.reload(sys.modules["tdlib_media_uploader.config.loader"])
        else:
            cfg = importlib.import_module("tdlib_media_uploader.config.loader")
        _CONFIG_ERROR = ""
        return ""
    except Exception as exc:
        cfg = None
        _CONFIG_ERROR = str(exc)
        return _CONFIG_ERROR


def get_config() -> Any:
    """Return the active config loader module or None."""
    global cfg
    # A media/test entry point may have initialized the file after this
    # module was imported. Reuse it without creating files during import.
    if cfg is None and not _CONFIG_ERROR and CONFIG_PATH.exists():
        reload_config()
    return cfg


def get_cfg(name: str, default: Any = None) -> Any:
    """Safe attribute getter from the active config loader."""
    current = get_config()
    return getattr(current, name, default) if current is not None else default


def target_for(kind: str) -> dict[str, Any]:
    """Retrieve the Telegram destination target dict for the specified media kind."""
    normalized = require_kind(kind)
    if cfg is not None and callable(getattr(cfg, "target_for", None)):
        return dict(cfg.target_for(normalized) or {})
    return {
        "target_mode": "forum_topic",
        "group_chat_id": get_cfg("GROUP_CHAT_ID", get_cfg("CHAT_ID", 0)),
        "channel_chat_id": get_cfg("CHANNEL_CHAT_ID", 0),
        "forum_topic_id": get_cfg("FORUM_TOPIC_ID", 0),
        "chat_id": get_cfg("CHAT_ID", 0),
    }


def source_root_for(kind: str) -> Path:
    """Return the configured source root for a worker boundary call."""
    normalized = require_kind(kind)
    return Path(get_cfg(KIND_PATH_KEYS[normalized], PROJECT_DIR))


@functools.lru_cache(maxsize=128)
def get_section_re(section: str):
    # TOML permits indentation and a trailing comment on table headers.
    # Both normal and array tables terminate the selected table's body.
    return re.compile(
        rf"(?ms)^([ \t]*\[{re.escape(section)}\][ \t]*(?:\#[^\r\n]*)?\r?$)"
        rf"(.*?)(?=^[ \t]*\[|\Z)"
    )


@functools.lru_cache(maxsize=128)
def get_key_re(key: str):
    return re.compile(rf"(?m)^([ \t]*{re.escape(key)}[ \t]*=[ \t]*)[^\r\n]*")


def update_toml_value(text: str, section: str, key: str, value: Any) -> str:
    """Update or insert a key/value pair in TOML text preserving formatting and comments."""
    newline = "\r\n" if "\r\n" in text else "\n"
    if isinstance(value, bool):
        literal = "true" if value else "false"
    elif isinstance(value, int):
        literal = str(value)
    elif isinstance(value, float):
        literal = repr(value)
    else:
        literal = json.dumps(str(value), ensure_ascii=False)

    section_re = get_section_re(section)
    match = section_re.search(text)
    if not match:
        suffix = newline if text and not text.endswith("\n") else ""
        return f"{text}{suffix}{newline}[{section}]{newline}{key} = {literal}{newline}"

    body = match.group(2)
    key_re = get_key_re(key)
    key_match = key_re.search(body)
    if key_match:
        body = body[: key_match.start()] + key_match.group(1) + literal + body[key_match.end() :]
    else:
        if not body or not body.endswith("\n"):
            body += newline
        body += f"{key} = {literal}{newline}"
    return text[: match.start(2)] + body + text[match.end(2) :]


def write_config_values(
    values: dict[tuple[str, str], object],
    config_path: Path | None = None,
    reloader=None,
) -> str:
    """Write key/value updates to config.toml with rollback on reload error."""
    path = config_path if config_path is not None else CONFIG_PATH
    reloader_fn = reloader if reloader is not None else reload_config
    try:
        if not path.exists():
            if not TEMPLATE_CONFIG_PATH.exists():
                return "找不到 config.toml 和默认配置资源。"
            atomic_write_text(path, TEMPLATE_CONFIG_PATH.read_text(encoding="utf-8"))
        with path.open(encoding="utf-8", newline="") as stream:
            previous = stream.read()
        text = previous
        for (section, key), value in values.items():
            text = update_toml_value(text, section, key, value)
        tomllib.loads(text)
        atomic_write_text(path, text)
        error = reloader_fn()
        if error:
            atomic_write_text(path, previous)
            reloader_fn()
        return error
    except Exception as exc:
        return f"配置保存失败：{type(exc).__name__}: {exc}"


__all__ = [
    "_CONFIG_CREATED",
    "_CONFIG_ERROR",
    "cfg",
    "ensure_config_file",
    "get_cfg",
    "get_config",
    "get_key_re",
    "get_section_re",
    "initialize_config",
    "reload_config",
    "source_root_for",
    "target_for",
    "update_toml_value",
    "write_config_values",
]
