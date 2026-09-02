# -*- coding: utf-8 -*-
"""V1.6 视频入口启动器。

职责：
1. 将视频断点目录固定为 .video_state；
2. 自动兼容并迁移旧版本的 .state；
3. 再启动 tdlib_video_app.main()。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import tdlib_video_album_uploader as core


PROJECT_DIR = Path(__file__).resolve().parent
VIDEO_STATE_DIR = PROJECT_DIR / ".video_state"
LEGACY_STATE_DIR = PROJECT_DIR / ".state"


# 核心 UploadState 在实例化时读取 core.STATE_DIR。
# 在进入 V1.6 UI 前先覆盖，因此状态文件展示和实际写入路径都会变为 .video_state。
core.STATE_DIR = VIDEO_STATE_DIR


def migrate_legacy_state() -> int:
    """把旧 .state 中尚未存在于 .video_state 的视频断点复制过去。"""
    VIDEO_STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not LEGACY_STATE_DIR.exists():
        return 0

    migrated = 0

    for source in LEGACY_STATE_DIR.glob(
        "upload_state_*.json"
    ):
        target = VIDEO_STATE_DIR / source.name

        if target.exists():
            continue

        shutil.copy2(
            source,
            target,
        )

        migrated += 1

    return migrated


def main():
    migrated = migrate_legacy_state()

    if migrated:
        core.UI.info(
            f"已从旧 .state 自动迁移 {migrated} 个视频断点文件到 .video_state。"
        )

    import tdlib_video_app

    tdlib_video_app.main()


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        core.UI.finish()
        core.UI.warning(
            "已手动停止。已完成 Album 下次自动跳过；"
            "当前未完成 Album 下次重新处理。"
        )

    except Exception as exc:
        core.UI.finish()
        core.UI.error(
            f"程序停止：{type(exc).__name__}: {exc}"
        )
        raise
