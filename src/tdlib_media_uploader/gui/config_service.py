# -*- coding: utf-8 -*-
"""Configuration management and TOML persistence service for the GUI layer."""

from __future__ import annotations

import functools
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tomllib
from typing import Any

from ..config.paths import CONFIG_PATH, RESOURCE_DIR, TEMPLATE_CONFIG_PATH
from .tools import KIND_PATH_KEYS, require_kind

PROJECT_DIR = RESOURCE_DIR


def ensure_config_file() -> bool:
    """Ensure the user config.toml exists on startup, copying the bundled template."""
    if "--self-test" in sys.argv[1:]:
        return False
    if CONFIG_PATH.exists() or not TEMPLATE_CONFIG_PATH.exists():
        return False
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATE_CONFIG_PATH, CONFIG_PATH)
    return True


_CONFIG_CREATED = ensure_config_file()
_CONFIG_ERROR = ""
try:
    from ..config import loader as cfg
except Exception as exc:
    cfg = None
    _CONFIG_ERROR = str(exc)


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
    return re.compile(rf"(?ms)^(\[{re.escape(section)}\]\s*$)(.*?)(?=^\[|\Z)")


@functools.lru_cache(maxsize=128)
def get_key_re(key: str):
    return re.compile(rf"(?m)^(\s*{re.escape(key)}\s*=\s*).*$")


def update_toml_value(text: str, section: str, key: str, value: Any) -> str:
    """Update or insert a key/value pair in TOML text preserving formatting and comments."""
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
        suffix = "\n" if text and not text.endswith("\n") else ""
        return f"{text}{suffix}\n[{section}]\n{key} = {literal}\n"

    body = match.group(2)
    key_re = get_key_re(key)
    key_match = key_re.search(body)
    if key_match:
        body = body[: key_match.start()] + key_match.group(1) + literal + body[key_match.end() :]
    else:
        if body and not body.endswith("\n"):
            body += "\n"
        body += f"{key} = {literal}\n"
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
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(TEMPLATE_CONFIG_PATH, path)
        text = path.read_text(encoding="utf-8")
        for (section, key), value in values.items():
            text = update_toml_value(text, section, key, value)
        tomllib.loads(text)
        previous = path.read_text(encoding="utf-8")
        temp = path.with_suffix(".toml.tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
        error = reloader_fn()
        if error:
            temp.write_text(previous, encoding="utf-8")
            os.replace(temp, path)
            reloader_fn()
        return error
    except Exception as exc:
        return f"配置保存失败：{type(exc).__name__}: {exc}"


# Compatibility aliases
_ensure_config_file = ensure_config_file
_reload_config = reload_config
_cfg = get_cfg
_target_for = target_for
_source_root_for = source_root_for
_get_section_re = get_section_re
_get_key_re = get_key_re
_update_toml_value = update_toml_value
_write_config_values = write_config_values


__all__ = [
    "_CONFIG_CREATED",
    "_CONFIG_ERROR",
    "_cfg",
    "_ensure_config_file",
    "_get_key_re",
    "_get_section_re",
    "_reload_config",
    "_source_root_for",
    "_target_for",
    "_update_toml_value",
    "_write_config_values",
    "cfg",
    "ensure_config_file",
    "get_cfg",
    "get_config",
    "get_key_re",
    "get_section_re",
    "reload_config",
    "source_root_for",
    "target_for",
    "update_toml_value",
    "write_config_values",
]
