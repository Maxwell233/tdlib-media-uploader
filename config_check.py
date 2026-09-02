# -*- coding: utf-8 -*-
"""Standalone TOML configuration validator.

This module intentionally does not import app_config, so it can validate a broken
config.toml before the uploader imports any project configuration.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.toml"


_ERROR_POS_RE = re.compile(r"\(at line (\d+), column (\d+)\)\s*$")
_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")
_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")


def _error_position(error: tomllib.TOMLDecodeError):
    line = getattr(error, "lineno", None)
    column = getattr(error, "colno", None)

    if line is not None and column is not None:
        return int(line), int(column)

    match = _ERROR_POS_RE.search(str(error))
    if match:
        return int(match.group(1)), int(match.group(2))

    return None, None


def _find_duplicate_key(lines: list[str], error_line: int | None):
    if not error_line or error_line < 1 or error_line > len(lines):
        return None

    current_line = lines[error_line - 1]
    key_match = _KEY_RE.match(current_line)
    if not key_match:
        return None

    key = key_match.group(1)
    current_section = ""

    for index in range(error_line - 1):
        line = lines[index]
        section_match = _SECTION_RE.match(line)
        if section_match:
            current_section = section_match.group(1).strip()

    target_section = current_section
    section = ""
    previous_line = None

    for index, line in enumerate(lines[: error_line - 1], 1):
        section_match = _SECTION_RE.match(line)
        if section_match:
            section = section_match.group(1).strip()
            continue

        key_match = _KEY_RE.match(line)
        if key_match and section == target_section and key_match.group(1) == key:
            previous_line = index

    if previous_line:
        section_text = f"[{target_section}]" if target_section else "顶层"
        return key, previous_line, section_text

    return None


def format_toml_error(path: Path, error: tomllib.TOMLDecodeError) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except Exception:
        return f"config.toml 格式错误：\n{error}"

    lines = text.splitlines()
    line_no, column_no = _error_position(error)

    result = [
        "config.toml 格式错误",
        "",
        f"解析器：{error}",
    ]

    if line_no:
        result.append(f"位置：第 {line_no} 行" + (f"，第 {column_no} 列" if column_no else ""))
        result.append("")

        start = max(1, line_no - 2)
        end = min(len(lines), line_no + 2)
        width = len(str(end))

        for number in range(start, end + 1):
            marker = ">" if number == line_no else " "
            content = lines[number - 1]
            result.append(f"{marker} {number:>{width}} | {content}")

            if number == line_no and column_no:
                # Prefix includes marker, number, separator and one leading content space.
                pointer_indent = 2 + width + 3 + max(0, column_no - 1)
                result.append(" " * pointer_indent + "^")

    message = str(error)

    if "Cannot overwrite a value" in message:
        duplicate = _find_duplicate_key(lines, line_no)
        result.extend([
            "",
            "原因：TOML 检测到同一个配置项被重复定义，或同一个名称同时被当作值和配置段使用。",
        ])

        if duplicate:
            key, previous_line, section_text = duplicate
            result.append(
                f"检测到重复项：{section_text} 中的 {key}，之前已在第 {previous_line} 行定义。"
            )

        result.extend([
            "处理：保留其中一条定义，删除重复项后保存 config.toml。",
            "提示：不要把整段旧配置粘贴到模板后面；只修改现有配置项右侧的值。",
        ])

    elif "Expected newline or end of document" in message:
        result.extend([
            "",
            "原因：某一行在值结束后还有 TOML 无法识别的内容。",
            "处理：检查引号、注释符号 #，并确保每个配置项单独占一行。",
        ])

    elif "Unclosed array" in message or "Unclosed inline table" in message:
        result.extend([
            "",
            "原因：数组或内联表没有正确闭合。",
            "处理：检查 [] 或 {} 是否成对出现。",
        ])

    result.extend([
        "",
        f"配置文件：{path}",
    ])

    return "\n".join(result)


def load_config(path: Path = CONFIG_PATH):
    if not path.exists():
        raise RuntimeError(
            "找不到配置文件：\n"
            f"{path}\n\n"
            "请先复制 config.example.toml 为 config.toml，或运行 setup.cmd。"
        )

    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise RuntimeError(format_toml_error(path, error)) from error


def main() -> int:
    try:
        load_config()
    except Exception as exc:
        print()
        print("=" * 72)
        print("CONFIG CHECK FAILED")
        print("=" * 72)
        print(exc)
        print("=" * 72)
        print()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
