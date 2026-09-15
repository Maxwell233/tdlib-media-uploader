# -*- coding: utf-8 -*-
"""Offline health check used by frozen package CI and support diagnostics."""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path

from runtime_paths import (
    DATA_DIR,
    IS_FROZEN,
    RESOURCE_DIR,
    TEMPLATE_CONFIG_PATH,
    TDLIB_DATABASE_DIR,
    TDLIB_FILES_DIR,
    ensure_data_dirs,
    read_version,
)


def configure_cli_encoding() -> None:
    """Make command-line diagnostics safe on legacy Windows consoles.

    PyInstaller's windowed executable can inherit a stream whose code page
    cannot represent the Chinese paths reported by the self-test.  Reusing
    the existing stream keeps redirection and CI capture intact while
    replacing unrepresentable characters instead of aborting the check.
    ``StringIO`` and other stream-like objects used by callers may not expose
    ``reconfigure``; those streams are intentionally left untouched.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, TypeError, ValueError):
            # A redirected or embedded stream may reject changing its codec.
            # The caller can still capture the diagnostic through that stream.
            continue


def _is_within(child: Path, parent: Path) -> bool:
    """Check containment without resolving links or touching network paths."""

    try:
        child_text = os.path.normcase(os.path.abspath(str(child)))
        parent_text = os.path.normcase(os.path.abspath(str(parent)))
        return os.path.commonpath((child_text, parent_text)) == parent_text
    except (OSError, ValueError):
        return False


def run_self_test(*, emit=print) -> int:
    configure_cli_encoding()
    failures: list[str] = []
    emit(f"TDLib Media Uploader V{read_version()}")
    emit(f"资源目录：{RESOURCE_DIR}")
    emit(f"数据目录：{DATA_DIR}")
    emit(f"TDLib 数据库目录：{TDLIB_DATABASE_DIR}")
    emit(f"TDLib 文件目录：{TDLIB_FILES_DIR}")
    try:
        ensure_data_dirs()
        expected_database = DATA_DIR / "telegram" / "database"
        expected_files = DATA_DIR / "telegram" / "files"
        if TDLIB_DATABASE_DIR != expected_database:
            failures.append(f"TDLib database_directory 路径错误：{TDLIB_DATABASE_DIR}")
        if TDLIB_FILES_DIR != expected_files:
            failures.append(f"TDLib files_directory 路径错误：{TDLIB_FILES_DIR}")
        if not _is_within(TDLIB_DATABASE_DIR, DATA_DIR) or not _is_within(TDLIB_FILES_DIR, DATA_DIR):
            failures.append("TDLib 登录数据目录必须位于 DATA_DIR 内。")
        # Source runs intentionally place ``data`` below the repository, so
        # RESOURCE_DIR is an ancestor there.  A frozen bundle must keep its
        # writable data beside the executable (Windows) or in Application
        # Support (macOS), never inside the read-only _internal/resource tree.
        if IS_FROZEN and (
            _is_within(TDLIB_DATABASE_DIR, RESOURCE_DIR)
            or _is_within(TDLIB_FILES_DIR, RESOURCE_DIR)
        ):
            failures.append("TDLib 登录数据目录不能位于 RESOURCE_DIR 或 PyInstaller _internal 内。")
        # Validate the payload that login actually sends, rather than only
        # checking the exported constants above.  Importing this pure helper
        # does not create a client or connect to Telegram.
        from tdlib_common import build_tdlib_parameters

        parameters = build_tdlib_parameters("self-test")
        if parameters.get("database_directory") != str(TDLIB_DATABASE_DIR.resolve()):
            failures.append("setTdlibParameters.database_directory 未使用 DATA_DIR。")
        if parameters.get("files_directory") != str(TDLIB_FILES_DIR.resolve()):
            failures.append("setTdlibParameters.files_directory 未使用 DATA_DIR。")
        if not _is_within(Path(parameters["database_directory"]), DATA_DIR):
            failures.append("TDLib 参数中的 database_directory 不在 DATA_DIR 内。")
        if not _is_within(Path(parameters["files_directory"]), DATA_DIR):
            failures.append("TDLib 参数中的 files_directory 不在 DATA_DIR 内。")
        probe = DATA_DIR / ".self-test.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        failures.append(f"data 不可写：{exc}")
    if not TEMPLATE_CONFIG_PATH.is_file():
        failures.append(f"缺少配置模板：{TEMPLATE_CONFIG_PATH}")
    for module_name in (
        "tdjson", "PySide6", "PIL", "tdlib_common",
        "tdlib_video_album_uploader", "tdlib_image_album_uploader",
        "tdlib_mixed_album_uploader",
    ):
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            failures.append(f"导入 {module_name} 失败：{type(exc).__name__}: {exc}")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        for candidate in (
            RESOURCE_DIR / "tools" / "ffmpeg" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"),
            RESOURCE_DIR / "tools" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"),
        ):
            if candidate.is_file():
                ffmpeg = str(candidate)
                break
    if ffmpeg:
        try:
            import subprocess
            from path_utils import run_cancellable_process

            run_cancellable_process(
                [ffmpeg, "-version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=True,
            )
        except Exception as exc:
            failures.append(f"FFmpeg 检查失败：{exc}")
    else:
        emit("提示：未找到 FFmpeg（可选，封面/媒体读取功能将在实际使用时提示）")
    if failures:
        for failure in failures:
            emit(f"失败：{failure}")
        return 1
    emit("SELF-TEST OK")
    return 0


__all__ = ["configure_cli_encoding", "run_self_test"]
