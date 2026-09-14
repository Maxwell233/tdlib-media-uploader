# -*- coding: utf-8 -*-
"""TDLib Media Uploader V1.9.0 视频上传流程。

核心上传/断点/缩略图逻辑复用 tdlib_video_album_uploader.py；
本文件负责视频扫描、mtime 日期策略和 GUI 使用的上传流程。
"""

from __future__ import annotations

from collections import Counter

import app_config as cfg
import tdlib_video_album_uploader as core
from album_metadata import with_filename_description


UI = core.UI


def _path_size(path):
    snapshot = core._snapshot_for_path(path)
    return int(snapshot[0]) if snapshot is not None else 0


def read_metadata(videos=None, cancel_event=None):
    """读取批量日期元数据；没有 ExifTool 时保留 FFmpeg 媒体日期回退。"""
    if not core.video_dates_enabled():
        return {}, False
    if not cfg.EXIFTOOL_PATH.exists():
        if cfg.VIDEO_READ_MEDIA_CREATION_DATE:
            UI.warning(
                f"未找到 ExifTool：{cfg.EXIFTOOL_PATH}。"
                " EXIF 日期不可用，将尝试通过 FFmpeg 读取媒体创建日期；"
                "读取失败后按当前缺失日期策略处理。"
            )
            return {}, False
        if cfg.VIDEO_MISSING_DATE_POLICY == "mtime":
            UI.warning(
                f"未找到 ExifTool：{cfg.EXIFTOOL_PATH}。"
                " 本次缺失 EXIF 的视频使用文件修改时间（mtime）。"
            )
            return {}, False
        raise RuntimeError(
            f"找不到 ExifTool：{cfg.EXIFTOOL_PATH}\n"
            '当前未启用媒体创建日期，且 missing_date_policy="error"，必须安装 ExifTool。'
        )

    try:
        # Pass the Python scanner's accepted paths so the upload entry point
        # does not walk the source directory a second time.
        if cancel_event is None:
            return core.read_exif_metadata(videos), True
        return core.read_exif_metadata(videos, cancel_event=cancel_event), True
    except Exception as exc:
        # A transient network share or malformed ExifTool response must not
        # prevent the normal media-date/mtime fallback from running.
        UI.warning(f"ExifTool 读取失败，将使用后备日期：{exc}")
        return {}, False


def _source_label(item):
    return "mtime" if item["fallback"] else item["date_tag"]


def show_file_list(items, state):
    """按当前视频分组策略显示文件，避免几百个文件堆在同一张表中。"""
    groups = core.make_groups(items)
    global_index = {
        item["path"]: index
        for index, item in enumerate(items, 1)
    }

    if core.force_ten_per_album():
        grouping_text = f"忽略日期，按扫描顺序每 {cfg.VIDEO_ALBUM_SIZE} 个一组"
    else:
        grouping_text = f"按 {len(groups)} 个月份分组"
    UI.info(f"视频文件：共 {len(items)} 个，{grouping_text}展示。")

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
        month_bytes = sum(_path_size(item["path"]) for item in month_items)

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
            capture_time = item.get("capture_time")
            capture_text = (
                capture_time.strftime("%m-%d %H:%M:%S")
                if capture_time is not None
                else "未读取日期"
            )

            rows.append(
                (
                    global_index[path],
                    "✓ 已完成" if completed else "• 待上传",
                    capture_text,
                    core.format_size(_path_size(path)),
                    core.relative_name(path),
                )
            )

        group_label = core.group_display_name(month_key)
        UI.files(
            (
                f"{group_label} · {len(month_items)} 个 · "
                f"已完成 {completed_count} · "
                f"待上传 {pending_count} · "
                f"{core.format_size(month_bytes)}"
            ),
            columns,
            rows,
            kind="VIDEO",
            caption=(
                f"日期来源：{source_summary}    "
                + (
                    f"分组忽略日期；每组 {cfg.VIDEO_ALBUM_SIZE} 个，文件按扫描排序。"
                    if core.force_ten_per_album()
                    else "月份内按时间排序；已完成项由断点自动跳过。"
                )
            ),
        )


def show_group_plan(items, state):
    groups = core.make_groups(items)
    rows = []

    for month_key in sorted(groups):
        month_items = groups[month_key]
        plans = core.build_album_plans(month_items, state)
        pending = [item for item in month_items if not state.is_completed(item["path"])]
        album_count = sum(1 for plan in plans if plan["pending_items"])
        captions = ", ".join(
            plan["caption"]["text"] or "无"
            for plan in plans
            if plan["pending_items"]
        )

        rows.append(
            (
                core.group_display_name(month_key),
                captions or (core.month_caption(month_key) if core.include_group_title() else "无"),
                len(month_items),
                len(pending),
                album_count,
            )
        )

    UI.groups(
        "分组 / Album 计划" if core.force_ten_per_album() else "月份 / Album 计划",
        rows,
        kind="VIDEO",
    )


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
    skipped_items,
):
    pending_bytes = sum(_path_size(item["path"]) for item in pending_items)

    total_bytes = sum(_path_size(item["path"]) for item in items)

    fallback_count = sum(
        1
        for item in items
        if item["fallback"]
    )

    if exiftool_used:
        date_mode = (
            "EXIF 优先；媒体创建日期次之；缺失时 mtime"
            if cfg.VIDEO_READ_MEDIA_CREATION_DATE
            else "EXIF 优先；缺失时 mtime"
        )
    elif not core.video_dates_enabled():
        date_mode = "不读取日期（按文件名排序）"
    elif cfg.VIDEO_READ_MEDIA_CREATION_DATE:
        date_mode = "媒体创建日期（FFmpeg）；缺失时 mtime"
    else:
        date_mode = "文件修改时间（mtime）"

    UI.summary(
        f"上传前确认 · TDLib Media Uploader V{cfg.APP_VERSION}",
        [
            ("上传引擎", "TDLib 原生 C++ / tdjson"),
            ("视频目录", cfg.VIDEO_DIR),
            ("扫描视频", len(videos)),
            ("有效视频", len(items)),
            ("跳过坏视频", len(skipped_items)),
            ("日期模式", date_mode),
            ("扫描排序", "文件名" if cfg.VIDEO_SORT_MODE == "name" else "修改时间"),
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
                (
                    f"忽略日期，按扫描顺序每 {cfg.VIDEO_ALBUM_SIZE} 个视频组成一组（最后一组可少于该数量）"
                    if core.force_ten_per_album()
                    else f"按日期分组，每组最多 {cfg.VIDEO_ALBUM_SIZE} 个"
                )
                + (
                    f"；组标题={'开' if core.include_group_title() else '关'}"
                    f"；文件名={'开' if getattr(cfg, 'VIDEO_CAPTION_INCLUDE_FILENAMES', False) else '关'}"
                    f"；文件名序号={'开' if core.include_filename_numbers() else '关'}"
                ),
            ),
            ("CHAT_ID", cfg.CHAT_ID),
            ("目标模式", "Channel 频道" if getattr(cfg, "TARGET_MODE", "forum_topic") == "channel" else "超级群组 Forum Topic"),
            ("FORUM_TOPIC_ID", cfg.FORUM_TOPIC_ID if getattr(cfg, "TARGET_MODE", "forum_topic") == "forum_topic" else "不适用"),
            ("状态文件", state.path),
        ],
        kind="VIDEO",
    )


def main():
    activate = getattr(cfg, "activate_target", None)
    if callable(activate):
        activate("video")
    core.validate_config()
    version = core.verify_tdjson_version()

    UI.banner(
        f"TDLib Media Uploader V{cfg.APP_VERSION}",
        f"视频模式 · tdjson {version}",
        accent="cyan",
    )

    UI.info(f"扫描视频目录：{cfg.VIDEO_DIR}")

    videos = core.scan_videos()
    cancel_event = getattr(UI, "cancel_event", None)

    if not videos:
        UI.warning("没有找到支持的视频文件。")
        return

    if core.video_dates_enabled():
        UI.info("读取视频日期信息…")
        metadata_index, exiftool_used = read_metadata(videos, cancel_event)
    else:
        UI.info("已关闭日期读取，将按文件名处理…")
        metadata_index, exiftool_used = {}, False

    if cancel_event is None:
        items, missing = core.build_items(videos, metadata_index)
    else:
        items, missing = core.build_items(
            videos,
            metadata_index,
            cancel_event=cancel_event,
        )

    if (
        missing
        and cfg.VIDEO_MISSING_DATE_POLICY == "error"
    ):
        UI.error(
            "以下视频没有找到可用的 EXIF 或媒体创建日期："
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

    skipped_items = []
    # Build plans from the complete scan before preflight. A deferred file
    # stays in its original Album boundary and cannot pull a later file
    # forward into this run's upload list.
    plans = core.build_album_plans(items, state)
    preflight_skipped_paths = set()
    if pending_items:
        UI.info(f"检查 {len(pending_items)} 个待上传视频的媒体数据…")
        skipped_items = (
            core.preflight_videos(pending_items, UI)
            if cancel_event is None
            else core.preflight_videos(
                pending_items,
                UI,
                cancel_event=cancel_event,
            )
        )
        preflight_skipped_paths = {
            core.stable_path(record["path"])
            for record in skipped_items
        }
        if preflight_skipped_paths:
            core.report_skipped_videos(skipped_items, UI)

    pending_plans = [plan for plan in plans if plan["pending_items"]]
    total_albums = len(pending_plans)

    # GUI 调用的扫描与上传流程：
    # 1. 按当前分组策略显示完整文件列表。
    # 2. 再显示分组/Album 计划。
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
        skipped_items=skipped_items,
    )

    sendable_pending = [
        item for item in pending_items
        if core.stable_path(item["path"]) not in preflight_skipped_paths
    ]
    if not pending_items or not sendable_pending:
        if skipped_items:
            core.report_skipped_videos(skipped_items, UI, final=True)
        else:
            UI.success("全部视频都已存在于断点记录中，无需上传。")
        return

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
    if preflight_skipped_paths:
        progress.skip_items([
            item for item in pending_items
            if core.stable_path(item["path"]) in preflight_skipped_paths
        ])

    client.add_update_callback(
        progress.handle_update
    )

    try:
        client.login()
        client.set_fast_options()
        client.validate_target()

        album_global = 0

        month_plan_groups = {}
        for plan in pending_plans:
            month_plan_groups.setdefault(plan["month_key"], []).append(plan)
        for month_key in sorted(month_plan_groups):
            month_plans = month_plan_groups[month_key]
            month_items = [item for plan in month_plans for item in plan["pending_items"]]
            month_album_total = len(month_plans)

            group_label = core.group_display_name(month_key)
            group_word = "分组" if core.force_ten_per_album() else "月份"
            UI.album(
                kind="VIDEO",
                title=f"{group_word} {group_label}",
                subtitle=(
                    f"{len(month_plans)} 个 Album · "
                    f"{len(month_items)} 个视频 · "
                    f"{month_album_total} 个 Album"
                ),
            )

            for plan in month_plans:
                album_items = [
                    item for item in plan["pending_items"]
                    if core.stable_path(item["path"]) not in preflight_skipped_paths
                ]
                if not album_items:
                    continue
                month_album_number = plan["number"]
                label = with_filename_description(
                    plan["caption"]["text"],
                    album_items,
                    getattr(cfg, "VIDEO_CAPTION_INCLUDE_FILENAMES", False),
                    core.include_filename_numbers(),
                )

                if cancel_event is None:
                    contents, ready_items, runtime_skipped = core.build_video_contents(
                        album_items,
                        label,
                        UI,
                    )
                else:
                    contents, ready_items, runtime_skipped = core.build_video_contents(
                        album_items,
                        label,
                        UI,
                        cancel_event,
                    )
                if runtime_skipped:
                    skipped_items.extend(runtime_skipped)
                    progress.skip_items([
                        record["item"]
                        for record in runtime_skipped
                    ])
                if not ready_items:
                    UI.warning("当前 Album 没有可读取的视频，已跳过。")
                    continue
                if ready_items != album_items and getattr(cfg, "VIDEO_CAPTION_INCLUDE_FILENAMES", False):
                    label = with_filename_description(
                        plan["caption"]["text"],
                        ready_items,
                        True,
                        core.include_filename_numbers(),
                    )
                    if cancel_event is None:
                        contents, rebuilt_items, rebuilt_skipped = core.build_video_contents(
                            ready_items,
                            label,
                            UI,
                        )
                    else:
                        contents, rebuilt_items, rebuilt_skipped = core.build_video_contents(
                            ready_items,
                            label,
                            UI,
                            cancel_event,
                        )
                    if rebuilt_skipped:
                        skipped_items.extend(rebuilt_skipped)
                        progress.skip_items([
                            record["item"]
                            for record in rebuilt_skipped
                        ])
                    ready_items = rebuilt_items
                    if not ready_items:
                        UI.warning("当前 Album 没有可读取的视频，已跳过。")
                        continue

                album_global += 1

                progress.begin_album(
                    ready_items,
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
                        f"{group_label} · "
                        f"组内 {month_album_number}/{month_album_total} · "
                        f"{len(ready_items)} 个视频 · "
                        f"Caption={label}"
                    ),
                    rows=[
                        (
                            f"{item['capture_time'].strftime('%Y-%m-%d %H:%M:%S')}  "
                            f"{core.format_size(_path_size(item['path'])):>10}  "
                            f"{core.relative_name(item['path'])}"
                        )
                        for item in ready_items
                    ],
                )

                try:
                    message_ids = (
                        client.send_contents(
                            contents,
                            progress,
                            ready_items,
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
                    ready_items,
                    message_ids,
                )

                progress.finish_album(
                    ready_items
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
        core.report_skipped_videos(skipped_items, UI, final=True)

    finally:
        client.remove_update_callback(
            progress.handle_update
        )

        client.close()
