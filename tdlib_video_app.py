# -*- coding: utf-8 -*-
"""TDLib Media Uploader V1.7.3 视频上传流程。

核心上传/断点/缩略图逻辑复用 tdlib_video_album_uploader.py；
本文件负责视频扫描、mtime 日期策略和 GUI 使用的上传流程。
"""

from __future__ import annotations

import math
from collections import Counter

import app_config as cfg
import tdlib_video_album_uploader as core


UI = core.UI


def read_metadata():
    """ExifTool 可选；默认 mtime 时，没有 ExifTool 也可工作。"""
    if not cfg.EXIFTOOL_PATH.exists():
        if cfg.VIDEO_MISSING_DATE_POLICY == "mtime":
            UI.warning(
                f"未找到 ExifTool：{cfg.EXIFTOOL_PATH}。"
                " 本次全部使用 Windows 修改时间（mtime）。"
            )
            return {}, False
        raise RuntimeError(
            f"找不到 ExifTool：{cfg.EXIFTOOL_PATH}\n"
            '当前 missing_date_policy="error"，必须安装 ExifTool。'
        )

    return core.read_exif_metadata(), True


def _source_label(item):
    return "mtime" if item["fallback"] else item["date_tag"]


def show_file_list(items, state):
    """按月份分组显示视频文件，避免几百个文件堆在同一张表中。"""
    groups = core.make_groups(items)
    global_index = {
        item["path"]: index
        for index, item in enumerate(items, 1)
    }

    UI.info(
        f"视频文件：共 {len(items)} 个，"
        f"按 {len(groups)} 个月份分组展示。"
    )

    columns = [
        ("#", {"justify": "right", "width": 5}),
        ("状态", {"no_wrap": True, "width": 10}),
        ("日期时间", {"no_wrap": True, "width": 14}),
        ("大小", {"justify": "right", "no_wrap": True, "width": 11}),
        ("文件", {"overflow": "fold"}),
    ]

    for month_key in sorted(groups):
        month_items = groups[month_key]
        completed_count = sum(
            1
            for item in month_items
            if state.is_completed(item["path"])
        )
        pending_count = len(month_items) - completed_count
        month_bytes = sum(
            item["path"].stat().st_size
            for item in month_items
        )

        source_counts = Counter(
            _source_label(item)
            for item in month_items
        )
        source_summary = " / ".join(
            f"{source} × {count}"
            for source, count in source_counts.most_common()
        )

        rows = []

        for item in month_items:
            path = item["path"]
            completed = state.is_completed(path)

            rows.append(
                (
                    global_index[path],
                    "✓ 已完成" if completed else "• 待上传",
                    item["capture_time"].strftime("%m-%d %H:%M:%S"),
                    core.format_size(path.stat().st_size),
                    core.relative_name(path),
                )
            )

        UI.files(
            (
                f"{month_key} · {len(month_items)} 个 · "
                f"已完成 {completed_count} · "
                f"待上传 {pending_count} · "
                f"{core.format_size(month_bytes)}"
            ),
            columns,
            rows,
            kind="VIDEO",
            caption=(
                f"日期来源：{source_summary}    "
                "月份内按时间排序；已完成项由断点自动跳过。"
            ),
        )


def show_group_plan(items, state):
    groups = core.make_groups(items)
    rows = []

    for month_key in sorted(groups):
        month_items = groups[month_key]
        pending = [
            item
            for item in month_items
            if not state.is_completed(item["path"])
        ]

        album_count = (
            math.ceil(len(pending) / cfg.VIDEO_ALBUM_SIZE)
            if pending
            else 0
        )

        rows.append(
            (
                month_key,
                core.month_caption(month_key),
                len(month_items),
                len(pending),
                album_count,
            )
        )

    UI.groups("月份 / Album 计划", rows, kind="VIDEO")


def show_upload_summary(
    *,
    videos,
    items,
    missing,
    state,
    completed_items,
    pending_items,
    total_albums,
    exiftool_used,
):
    pending_bytes = sum(
        item["path"].stat().st_size
        for item in pending_items
    )

    total_bytes = sum(
        item["path"].stat().st_size
        for item in items
    )

    fallback_count = sum(
        1
        for item in items
        if item["fallback"]
    )

    date_mode = (
        "EXIF/QuickTime 优先；缺失时 mtime"
        if exiftool_used
        else "Windows 修改时间（mtime）"
    )

    UI.summary(
        f"上传前确认 · TDLib Media Uploader V{cfg.APP_VERSION}",
        [
            ("上传引擎", "TDLib 原生 C++ / tdjson"),
            ("视频目录", cfg.VIDEO_DIR),
            ("扫描视频", len(videos)),
            ("有效视频", len(items)),
            ("日期模式", date_mode),
            ("mtime 兜底", fallback_count),
            ("缺失日期", len(missing)),
            ("全部大小", core.format_size(total_bytes)),
            ("断点已完成", f"{len(completed_items)}/{len(items)}"),
            (
                "本次待上传",
                f"{len(pending_items)} · {core.format_size(pending_bytes)}",
            ),
            ("本次 Album", total_albums),
            (
                "Album 规则",
                f"每组最多 {cfg.VIDEO_ALBUM_SIZE} 个；首条 Caption=yy-m",
            ),
            ("CHAT_ID", cfg.CHAT_ID),
            ("FORUM_TOPIC_ID", cfg.FORUM_TOPIC_ID),
            ("状态文件", state.path),
        ],
        kind="VIDEO",
    )


def main():
    core.validate_config()
    version = core.verify_tdjson_version()

    UI.banner(
        f"TDLib Media Uploader V{cfg.APP_VERSION}",
        f"视频模式 · tdjson {version}",
        accent="cyan",
    )

    UI.info(f"扫描视频目录：{cfg.VIDEO_DIR}")

    videos = core.scan_videos()

    if not videos:
        UI.warning("没有找到支持的视频文件。")
        return

    UI.info("读取视频日期信息…")

    metadata_index, exiftool_used = read_metadata()

    items, missing = core.build_items(
        videos,
        metadata_index,
    )

    if (
        missing
        and cfg.VIDEO_MISSING_DATE_POLICY == "error"
    ):
        UI.error(
            "以下视频没有找到可用的 EXIF/QuickTime 日期："
        )

        for path in missing:
            UI.log(
                f"  {core.relative_name(path)}"
            )

        UI.warning(
            '当前 missing_date_policy="error"，没有开始上传。'
        )
        return

    state = core.UploadState()

    completed_items = [
        item
        for item in items
        if state.is_completed(item["path"])
    ]

    pending_items = [
        item
        for item in items
        if not state.is_completed(item["path"])
    ]

    pending_groups = core.make_groups(
        pending_items
    )

    total_albums = sum(
        math.ceil(
            len(group)
            /
            cfg.VIDEO_ALBUM_SIZE
        )
        for group in pending_groups.values()
    )

    # GUI 调用的扫描与上传流程：
    # 1. 按月份分组显示完整文件列表。
    # 2. 再显示月份/Album 计划。
    # 3. 最后显示上传摘要。
    # 4. 摘要之后才询问 y。
    if cfg.VIDEO_SHOW_FILE_LIST:
        show_file_list(
            items,
            state,
        )

    show_group_plan(
        items,
        state,
    )

    show_upload_summary(
        videos=videos,
        items=items,
        missing=missing,
        state=state,
        completed_items=completed_items,
        pending_items=pending_items,
        total_albums=total_albums,
        exiftool_used=exiftool_used,
    )

    if not pending_items:
        UI.success(
            "全部视频都已存在于断点记录中，无需上传。"
        )
        return

    if cfg.VIDEO_VERIFY_ALL_METADATA:
        UI.info(
            "开始预检本次待上传视频…"
        )

        for index, item in enumerate(
            pending_items,
            1,
        ):
            path = item["path"]

            UI.info(
                f"预检 {index}/{len(pending_items)} · {path.name}"
            )

            core.video_info(
                path
            )

        UI.success(
            "视频预检完成。"
        )

    if not UI.confirm_upload():
        UI.cancelled()
        return

    client = core.TDJsonClient(
        UI,
        "TDLib Video Album Uploader",
    )

    progress = core.VideoUploadProgress(
        items,
        completed_items,
    )

    client.add_update_callback(
        progress.handle_update
    )

    try:
        client.login()
        client.set_fast_options()
        client.validate_target()

        album_global = 0

        for month_key in sorted(
            pending_groups
        ):
            month_items = (
                pending_groups[
                    month_key
                ]
            )

            month_album_total = math.ceil(
                len(month_items)
                /
                cfg.VIDEO_ALBUM_SIZE
            )

            label = core.month_caption(
                month_key
            )

            UI.album(
                kind="VIDEO",
                title=f"月份 {month_key}",
                subtitle=(
                    f"Caption={label} · "
                    f"{len(month_items)} 个视频 · "
                    f"{month_album_total} 个 Album"
                ),
            )

            for start in range(
                0,
                len(month_items),
                cfg.VIDEO_ALBUM_SIZE,
            ):
                album_items = month_items[
                    start:
                    start
                    +
                    cfg.VIDEO_ALBUM_SIZE
                ]

                month_album_number = (
                    start
                    //
                    cfg.VIDEO_ALBUM_SIZE
                    +
                    1
                )

                album_global += 1

                progress.begin_album(
                    album_items,
                    month_key,
                    album_global,
                    total_albums,
                )

                UI.album(
                    kind="VIDEO",
                    title=(
                        f"Album "
                        f"{album_global}/"
                        f"{total_albums}"
                    ),
                    subtitle=(
                        f"{month_key} · "
                        f"月内 {month_album_number}/{month_album_total} · "
                        f"{len(album_items)} 个视频 · "
                        f"Caption={label}"
                    ),
                    rows=[
                        (
                            f"{item['capture_time'].strftime('%Y-%m-%d %H:%M:%S')}  "
                            f"{core.format_size(item['path'].stat().st_size):>10}  "
                            f"{core.relative_name(item['path'])}"
                        )
                        for item in album_items
                    ],
                )

                try:
                    contents = [
                        core.input_video(
                            item,
                            label
                            if index == 0
                            else "",
                        )
                        for index, item
                        in enumerate(
                            album_items
                        )
                    ]

                    message_ids = (
                        client.send_contents(
                            contents,
                            progress,
                            album_items,
                        )
                    )

                except Exception:
                    UI.finish()

                    UI.error(
                        "当前 Album 未写入断点；"
                        "下次会重新处理这一组。"
                    )

                    raise

                state.mark_album_completed(
                    album_items,
                    message_ids,
                )

                progress.finish_album(
                    album_items
                )

                UI.success(
                    f"Album 发送完成 · "
                    f"Caption={label} · "
                    "断点已保存。"
                )

        UI.banner(
            "全部视频上传完成",
            (
                f"共完成 {total_albums} 个 Album · "
                "断点已保存"
            ),
            accent="green",
        )

    finally:
        client.remove_update_callback(
            progress.handle_update
        )

        client.close()
