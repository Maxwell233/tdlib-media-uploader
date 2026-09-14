# -*- coding: utf-8 -*-
"""Mixed photo/video Album uploader.

Each first-level directory below ``MIXED_DIR`` is an independent group. Files
inside that directory (including nested directories) keep one deterministic
order and are split into Telegram Albums of at most ten items. The module
reuses the existing photo/video input builders and TDLib client, so mixed
Albums retain the same resumable state and error isolation guarantees.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from album_metadata import CaptionStore, album_key, compose_caption, with_filename_description
from path_utils import (
    display_path,
    file_snapshot,
    io_worker_count,
    iter_files,
    media_path_sort,
    file_mtime,
    is_link_or_junction,
    ordered_bounded_map,
    relative_name as stable_relative_name,
    stable_path,
    wait_for_file_ready,
)
import app_config as cfg
from tdlib_common import HeadlessUI, TDJsonClient, formatted_text, verify_tdjson_version
from runtime_paths import APP_DATA_DIR, RESOURCE_DIR
from staging import cleanup_staging, stage_file

import tdlib_image_album_uploader as image_core
import tdlib_video_album_uploader as video_core


PROJECT_DIR = RESOURCE_DIR
STATE_DIR = APP_DATA_DIR / ".mixed_state"
LAST_SCAN_ERRORS: list[str] = []
LAST_SCAN_SIZE_SKIPS: list[dict] = []
LAST_SCAN_IGNORED_ROOT_MEDIA: list[Path] = []
DEFERRED_STATUS = "DEFERRED"
UI = HeadlessUI()
MEDIA_DATE_MAX_WORKERS = 4
READINESS_ATTEMPTS = 3


def _readiness_options() -> dict:
    return {
        "attempts": int(getattr(cfg, "SCAN_READINESS_ATTEMPTS", READINESS_ATTEMPTS)),
        "stable_interval": float(getattr(cfg, "SCAN_STABILITY_INTERVAL_SECONDS", 0.05)),
        "stable_checks": int(getattr(cfg, "SCAN_STABILITY_CHECKS", 2)),
        "probe_bytes": int(getattr(cfg, "SCAN_READ_PROBE_BYTES", 64 * 1024)),
    }


def _validate_extensions() -> None:
    overlap = set(getattr(cfg, "MIXED_IMAGE_EXTENSIONS", set())) & set(
        getattr(cfg, "MIXED_VIDEO_EXTENSIONS", set())
    )
    if overlap:
        values = ", ".join(sorted(overlap))
        raise RuntimeError(
            f"混合上传的图片和视频扩展名冲突：{values}。请在 [image]/[video] 中移除重复扩展名。"
        )


def format_size(value: float | int) -> str:
    value = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def relative_name(path: Path, root=None) -> str:
    return stable_relative_name(path, root or cfg.MIXED_DIR)


def cleanup_staging_cache(*, startup: bool = False) -> None:
    """Prune stale local staging artifacts without affecting source state."""

    if not getattr(cfg, "STAGING_ENABLED", False):
        return
    if startup and not getattr(cfg, "STAGING_CLEANUP_ON_START", True):
        return
    cleanup_staging(
        cfg.STAGING_DIR,
        max_age_seconds=float(getattr(cfg, "STAGING_CLEANUP_DAYS", 7)) * 86400,
    )


def file_signature(path: Path, snapshot=None) -> str:
    if snapshot is None:
        snapshot = file_snapshot(path)
    if snapshot is None:
        raise OSError(f"文件暂时不可读取：{path}")
    size, mtime_ns = snapshot
    raw = f"{relative_name(path).lower()}|{size}|{mtime_ns}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _kind_for(path: Path) -> str | None:
    _validate_extensions()
    suffix = path.suffix.lower()
    if suffix in cfg.MIXED_VIDEO_EXTENSIONS:
        return "video"
    if suffix in cfg.MIXED_IMAGE_EXTENSIONS:
        return "image"
    return None


def _item_for_path(path: Path, group_name: str) -> dict | None:
    media_kind = _kind_for(path)
    if media_kind is None:
        return None
    snapshot = file_snapshot(path)
    if snapshot is None:
        LAST_SCAN_ERRORS.append(f"{path}: 文件暂时不可读取或为空")
        return None
    size, mtime_ns = snapshot
    limit = cfg.VIDEO_MAX_BYTES if media_kind == "video" else cfg.IMAGE_MAX_BYTES
    if size > limit:
        compress = media_kind == "image" and cfg.IMAGE_COMPRESS_OVERSIZE
        LAST_SCAN_SIZE_SKIPS.append({
            "path": path,
            "size": size,
            "limit": limit,
            "category": "size",
            "action": "compress" if compress else "skip",
            "media_kind": media_kind,
            "reason": (
                f"文件大小 {format_size(size)} 超过 Telegram "
                f"{'视频 4 GiB' if media_kind == 'video' else 'Photo 10 MiB'} 上限 "
                f"{format_size(limit)}"
            ),
        })
        if not compress:
            return None
    return {
        "path": path,
        "media_kind": media_kind,
        "group_name": group_name,
        "scan_size": size,
        "scan_mtime_ns": mtime_ns,
    }


def _group_items(group_path: Path, group_name: str, cancel_event=None) -> list[dict]:
    candidates, errors = iter_files(
        group_path, cfg.MIXED_EXTENSIONS, cancel_event=cancel_event
    )
    LAST_SCAN_ERRORS.extend(errors)
    items = []
    for path in candidates:
        item = _item_for_path(path, group_name)
        if item is not None:
            items.append(item)
    sort_mode = str(getattr(cfg, "MIXED_SORT_MODE", "name")).strip().lower()
    def item_mtime(item):
        snapshot_mtime = item.get("scan_mtime_ns")
        return snapshot_mtime if snapshot_mtime is not None else file_mtime(item["path"])
    return media_path_sort(
        items,
        group_path,
        mode=sort_mode,
        path_key=lambda item: item["path"],
        mtime_key=item_mtime,
    )


def scan_mixed_groups(cancel_event=None) -> list[dict]:
    """Scan each first-level directory as a separate mixed-media group."""
    global LAST_SCAN_ERRORS, LAST_SCAN_SIZE_SKIPS, LAST_SCAN_IGNORED_ROOT_MEDIA
    root = Path(cfg.MIXED_DIR)
    _validate_extensions()
    if is_link_or_junction(root):
        raise RuntimeError(f"混合上传目录不能是符号链接或 junction：{root}")
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"混合上传目录不存在或不是目录：{root}")
    LAST_SCAN_ERRORS = []
    LAST_SCAN_SIZE_SKIPS = []
    LAST_SCAN_IGNORED_ROOT_MEDIA = []
    groups = []
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        raise RuntimeError(f"无法读取混合上传目录：{root}\n{exc}") from exc
    directories = []
    root_files = []
    for entry in entries:
        if cancel_event is not None and cancel_event.is_set():
            LAST_SCAN_ERRORS.append("目录扫描已取消")
            break
        try:
            if is_link_or_junction(entry):
                LAST_SCAN_ERRORS.append(f"跳过符号链接或 junction：{entry.path}")
                continue
            if entry.is_dir(follow_symlinks=False):
                directories.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False) and _kind_for(Path(entry.path)):
                root_files.append(Path(entry.path))
        except OSError as exc:
            LAST_SCAN_ERRORS.append(f"{entry.path}: {exc}")
    directories = media_path_sort(directories, root, mode="name")
    for group_path in directories:
        items = _group_items(group_path, group_path.name, cancel_event=cancel_event)
        if items:
            groups.append({
                "group_name": group_path.name,
                "group_path": group_path,
                "items": items,
            })
    # A mixed root is a container, not an upload group.  Files placed directly
    # in it are intentionally ignored so they cannot silently change Album
    # membership or be mixed with a first-level folder.
    if root_files:
        LAST_SCAN_IGNORED_ROOT_MEDIA = list(root_files)
    return groups


def flatten_items(groups) -> list[dict]:
    return [item for group in groups for item in group.get("items", [])]


class UploadState:
    VERSION = 1

    def __init__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        suffix = "tdlib-mixed-v1" if getattr(cfg, "TARGET_MODE", "forum_topic") == "forum_topic" else "tdlib-mixed-v1-channel"
        identity = f"{stable_path(cfg.MIXED_DIR)}|{cfg.CHAT_ID}|{cfg.FORUM_TOPIC_ID}|{suffix}"
        digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
        self.path = STATE_DIR / f"mixed_upload_state_{digest}.json"
        self.lock = threading.Lock()
        if getattr(cfg, "MIXED_RESET_STATE", False) and self.path.exists():
            self.path.unlink()
        self.data = self._load()

    def _new(self):
        return {
            "version": self.VERSION,
            "mixed_dir": stable_path(cfg.MIXED_DIR),
            "chat_id": cfg.CHAT_ID,
            "target_mode": getattr(cfg, "TARGET_MODE", "forum_topic"),
            "channel_chat_id": getattr(cfg, "CHANNEL_CHAT_ID", 0),
            "forum_topic_id": cfg.FORUM_TOPIC_ID,
            "completed": {},
        }

    def _load(self):
        if not self.path.exists():
            data = self._new()
            self._save(data)
            return data
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"混合上传断点读取失败：{self.path}\n{exc}") from exc
        if data.get("version") != self.VERSION:
            raise RuntimeError(f"混合上传断点版本不兼容：{self.path}")
        data.setdefault("completed", {})
        return data

    def _save(self, data):
        data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, self.path)

    def is_completed(self, path: Path) -> bool:
        try:
            return file_signature(path) in self.data["completed"]
        except OSError:
            return False

    def mark_album_completed(self, items, message_ids):
        with self.lock:
            for index, item in enumerate(items):
                path = item["path"]
                snapshot = file_snapshot(path)
                if snapshot is None:
                    # The source can disappear from a network share after a
                    # successful Telegram send.  The scan snapshot is the
                    # identity that was revalidated before upload, so keep
                    # the completion record instead of losing the checkpoint.
                    expected_size = item.get("scan_size")
                    expected_mtime_ns = item.get("scan_mtime_ns")
                    if expected_size is not None and expected_mtime_ns is not None:
                        snapshot = (int(expected_size), int(expected_mtime_ns))
                if snapshot is None:
                    # A legacy caller may omit snapshots.  Preserve the
                    # historical failure semantics for that exceptional case
                    # rather than writing an unverifiable checkpoint.
                    raise RuntimeError(f"上传完成但无法记录混合媒体断点：{path}")
                size, mtime_ns = snapshot
                self.data["completed"][file_signature(path, snapshot)] = {
                    "relative_path": relative_name(path),
                    "media_kind": item.get("media_kind"),
                    "group_name": item.get("group_name"),
                    "size": size,
                    "mtime_ns": mtime_ns,
                    "message_id": message_ids[index] if index < len(message_ids) else None,
                    "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            self._save(self.data)


def build_album_plans(groups, state=None) -> list[dict]:
    plans = []
    store = CaptionStore("mixed")
    for group in groups:
        group_name = str(group["group_name"])
        items = list(group.get("items", []))
        for offset in range(0, len(items), cfg.MIXED_ALBUM_SIZE):
            album_items = items[offset:offset + cfg.MIXED_ALBUM_SIZE]
            number = offset // cfg.MIXED_ALBUM_SIZE + 1
            key = album_key("mixed", f"{group_name}:{number}", album_items)
            pending = [item for item in album_items if state is None or not state.is_completed(item["path"])]
            record = store.get(key, group_name)
            base_label = record["base_label"] if cfg.MIXED_CAPTION_INCLUDE_GROUP_TITLE else ""
            if cfg.MIXED_CAPTION_INCLUDE_GROUP_TITLE and not base_label:
                base_label = group_name
            plans.append({
                "key": key,
                "group_name": group_name,
                "group_path": group.get("group_path"),
                "number": number,
                "items": album_items,
                "pending_items": pending,
                "caption": {
                    "base_label": base_label,
                    "custom_text": record["custom_text"],
                    "text": compose_caption(base_label, record["custom_text"], cfg.MIXED_ALBUM_CAPTION_SEPARATOR),
                },
            })
    return plans


def _deferred(exc: Exception) -> bool:
    return isinstance(exc, (OSError, TimeoutError)) or any(
        marker in str(exc) for marker in ("暂时不可读取", "发生变化", "SMB", "网络")
    )


def preflight_mixed(items, ui=None, cancel_event=None) -> list[dict]:
    target = ui or UI
    skipped = []

    def worker(item):
        path = item["path"]
        try:
            readiness = wait_for_file_ready(
                path,
                expected_size=item.get("scan_size"),
                expected_mtime_ns=item.get("scan_mtime_ns"),
                **_readiness_options(),
                cancel_event=cancel_event,
            )
            if not readiness.ready:
                raise RuntimeError(readiness.reason or f"文件暂时不可读取：{path}")
            snapshot = readiness.snapshot.as_tuple()
            if item.get("media_kind") == "video":
                if snapshot[0] > cfg.VIDEO_MAX_BYTES:
                    raise RuntimeError("文件大小超过 Telegram 视频上限")
                # Metadata validation is enough for the scan. Thumbnail
                # generation is deferred to the upload path and therefore is
                # performed at most once per video.
                if cancel_event is None:
                    video_core.video_info(path)
                else:
                    video_core.video_info(path, cancel_event=cancel_event)
            else:
                if snapshot[0] > cfg.IMAGE_MAX_BYTES and not cfg.IMAGE_COMPRESS_OVERSIZE:
                    raise RuntimeError("文件大小超过 Telegram Photo 上限")
                image_core.image_info(path)
            return None
        except Exception as exc:
            text = str(exc)
            category = "size" if "超过 Telegram" in text else "deferred" if _deferred(exc) else "unreadable"
            return {"item": item, "path": path, "reason": f"{type(exc).__name__}: {exc}", "category": category}

    worker_count = io_worker_count(
        cfg.MIXED_DIR,
        local=getattr(cfg, "IO_WORKERS_LOCAL", MEDIA_DATE_MAX_WORKERS),
        network=getattr(cfg, "IO_WORKERS_NETWORK", 2),
    )
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="tdlib-mixed-preflight") as executor:
        for index, result in enumerate(
            ordered_bounded_map(executor, items, worker, worker_count),
            1,
        ):
            if cancel_event is not None and cancel_event.is_set():
                break
            if result:
                skipped.append(result)
                target.warning(f"跳过无法读取的混合媒体：{relative_name(result['path'])}")
                target.log(f"跳过混合媒体详情：{result['path']}\n原因：{result['reason']}")
            if getattr(cfg, "MIXED_VERIFY_MEDIA", False):
                target.info(f"预检混合媒体 {index}/{len(items)} · {Path(items[index - 1]['path']).name}")
    return skipped


def report_skipped_mixed(skipped, ui=None):
    if not skipped:
        return
    target = ui or UI
    deferred = sum(record.get("category") == "deferred" for record in skipped)
    size = sum(record.get("category") == "size" for record in skipped)
    unreadable = len(skipped) - deferred - size
    parts = []
    if unreadable:
        parts.append(f"{unreadable} 个无法读取的混合媒体")
    if size:
        parts.append(f"{size} 个超限媒体")
    if deferred:
        parts.append(f"{deferred} 个暂时不可读媒体（DEFERRED）")
    target.warning("混合上传跳过：" + "、".join(parts))


def report_scan_size_skips(skipped, ui=None):
    """Report size decisions made before a mixed upload is planned."""

    if not skipped:
        return
    target = ui or UI
    rejected = [record for record in skipped if record.get("action") == "skip"]
    compressing = [record for record in skipped if record.get("action") == "compress"]
    if rejected:
        target.warning(
            f"扫描时跳过 {len(rejected)} 个超过 Telegram 限制的混合媒体；"
            "这些文件未加入上传计划。"
        )
    if compressing:
        target.warning(
            f"扫描提醒：发现 {len(compressing)} 个超限图片；"
            "上传时将尝试用 FFmpeg 生成临时压缩副本。"
        )
    for record in skipped:
        target.log(
            f"扫描混合媒体大小检查：{record['path']}\n"
            f"处理：{'上传时压缩' if record.get('action') == 'compress' else '跳过'}\n"
            f"原因：{record.get('reason', '')}"
        )


def _mixed_input_video(item, caption, cancel_event=None):
    path = item["path"]
    readiness = wait_for_file_ready(
        path,
        expected_size=item.get("scan_size"),
        expected_mtime_ns=item.get("scan_mtime_ns"),
        **_readiness_options(),
        cancel_event=cancel_event,
    )
    if not readiness.ready:
        raise RuntimeError(readiness.reason or f"文件暂时不可读取：{path}")
    source_path = path
    if getattr(cfg, "STAGING_ENABLED", False):
        source_path = stage_file(
            path,
            readiness.snapshot,
            staging_dir=cfg.STAGING_DIR,
            cancel_event=cancel_event,
        )
    info = (
        video_core.video_info(source_path)
        if cancel_event is None
        else video_core.video_info(source_path, cancel_event=cancel_event)
    )
    thumbnail = None
    if getattr(cfg, "MIXED_GENERATE_THUMBNAIL", True):
        if cancel_event is None:
            thumb_path, width, height = video_core.build_thumbnail(source_path)
        else:
            thumb_path, width, height = video_core.build_thumbnail(source_path, cancel_event)
        thumbnail = {
            "@type": "inputThumbnail",
            "thumbnail": {"@type": "inputFileLocal", "path": display_path(thumb_path)},
            "width": int(width),
            "height": int(height),
        }
    return {
        "@type": "inputMessageVideo",
        "video": {"@type": "inputFileLocal", "path": display_path(source_path)},
        "thumbnail": thumbnail,
        "cover": None,
        "start_timestamp": 0,
        "added_sticker_file_ids": [],
        "duration": int(max(1, round(info["duration"]))),
        "width": int(info["width"]),
        "height": int(info["height"]),
        "supports_streaming": True,
        "caption": formatted_text(caption),
        "show_caption_above_media": False,
        "self_destruct_type": None,
        "has_spoiler": False,
    }


def build_mixed_contents(items, caption: str, ui=None, cancel_event=None):
    target = ui or UI
    image_core.UI = target
    video_core.UI = target
    contents, valid, skipped = [], [], []
    for item in items:
        media_kind = item.get("media_kind")
        if media_kind not in {"video", "image"}:
            raise ValueError(f"未知混合媒体类型：{media_kind!r}")
        try:
            if media_kind == "video":
                item_caption = caption if not valid else ""
                content = (
                    _mixed_input_video(item, item_caption, cancel_event)
                    if cancel_event is not None
                    else _mixed_input_video(item, item_caption)
                )
            elif media_kind == "image":
                expected_size = item.get("scan_size")
                expected_mtime_ns = item.get("scan_mtime_ns")
                if expected_size is not None or expected_mtime_ns is not None:
                    if expected_size is None or expected_mtime_ns is None:
                        # A partial snapshot cannot prove that the source is
                        # unchanged; let input_photo perform its existence
                        # check without inventing a second value.
                        expected_size = expected_mtime_ns = None
                    else:
                        image_core.IMAGE_SCAN_SNAPSHOTS[stable_path(item["path"])] = (
                            int(expected_size),
                            int(expected_mtime_ns),
                        )
                else:
                    # Do not let a snapshot left by a previous mixed scan
                    # impose a stale JIT identity on a legacy caller that
                    # supplies only a path.
                    image_core.IMAGE_SCAN_SNAPSHOTS.pop(
                        stable_path(item["path"]),
                        None,
                    )
                item_caption = caption if not valid else ""
                if cancel_event is None:
                    content = image_core.input_photo(item["path"], item_caption)
                else:
                    content = image_core.input_photo(
                        item["path"], item_caption, cancel_event=cancel_event
                    )
            contents.append(content)
            valid.append(item)
        except Exception as exc:
            path = item["path"]
            record = {
                "item": item,
                "path": path,
                "reason": f"{type(exc).__name__}: {exc}",
                "category": "deferred" if _deferred(exc) else "unreadable",
            }
            skipped.append(record)
            target.warning(f"跳过上传前无法读取的混合媒体：{relative_name(path)}")
            target.log(f"跳过混合媒体详情：{path}\n原因：{record['reason']}")
    return contents, valid, skipped


class MixedUploadProgress:
    def __init__(self, all_items, completed_items):
        self.sizes = {}
        for item in all_items:
            snapshot = file_snapshot(item["path"])
            if snapshot is None:
                expected_size = item.get("scan_size")
                if expected_size is None:
                    raise RuntimeError(f"无法读取混合媒体大小：{item['path']}")
                size = int(expected_size)
            else:
                size = snapshot[0]
            self.sizes[item["path"]] = size
        self.total_bytes = sum(self.sizes.values())
        self.total_files = len(all_items)
        self.completed_bytes = sum(self.sizes[item["path"]] for item in completed_items)
        self.completed_files = len(completed_items)
        self.current_paths = {}
        self.current_uploaded = {}
        self.file_id_to_path = {}
        self.group_name = ""
        self.album_number = 0
        self.album_total = 0
        self.samples = deque()
        self.last_draw = 0.0
        self.lock = threading.Lock()

    def begin_album(self, items, group_name, album_number, album_total):
        with self.lock:
            self.group_name = group_name
            self.album_number = album_number
            self.album_total = album_total
            self.current_paths = {stable_path(item["path"]): item["path"] for item in items}
            self.current_uploaded = {item["path"]: 0 for item in items}
            self.file_id_to_path = {}
            self.samples.clear()
            self.last_draw = 0.0

    def skip_items(self, items):
        with self.lock:
            for item in items:
                path = item["path"]
                size = self.sizes.pop(path, 0)
                self.total_bytes = max(0, self.total_bytes - size)
                self.total_files = max(0, self.total_files - 1)
                self.current_uploaded.pop(path, None)
        self.draw(force=True)

    @staticmethod
    def _media_file(message):
        content = message.get("content", {})
        kind = content.get("@type")
        if kind == "messageVideo":
            return content.get("video", {}).get("video")
        if kind == "messagePhoto":
            sizes = content.get("photo", {}).get("sizes", [])
            if sizes:
                return max(sizes, key=lambda item: int(item.get("width", 0)) * int(item.get("height", 0))).get("photo")
        return None

    def register_messages(self, messages, items):
        with self.lock:
            for message, item in zip(messages, items):
                file_obj = self._media_file(message)
                if not file_obj:
                    continue
                if file_obj.get("id") is not None:
                    self.file_id_to_path[file_obj["id"]] = item["path"]
                self._apply_unlocked(file_obj)

    def _apply_unlocked(self, file_obj):
        path = None
        local = file_obj.get("local", {}).get("path", "")
        if local:
            path = self.current_paths.get(stable_path(local))
        if path is None:
            path = self.file_id_to_path.get(file_obj.get("id"))
        if path is None:
            return
        remote = file_obj.get("remote", {})
        uploaded = self.sizes[path] if remote.get("is_uploading_completed") else int(remote.get("uploaded_size", 0) or 0)
        self.current_uploaded[path] = max(self.current_uploaded.get(path, 0), min(uploaded, self.sizes[path]))

    def handle_update(self, obj):
        if obj.get("@type") != "updateFile" or not obj.get("file"):
            return
        with self.lock:
            self._apply_unlocked(obj["file"])
        self.draw()

    def _speed_unlocked(self):
        now = time.monotonic()
        uploaded = sum(self.current_uploaded.values())
        self.samples.append((now, uploaded))
        while len(self.samples) > 2 and now - self.samples[0][0] > 3:
            self.samples.popleft()
        if len(self.samples) < 2:
            return 0.0
        old_time, old_bytes = self.samples[0]
        duration = now - old_time
        return max(0.0, (uploaded - old_bytes) / duration) if duration > 0 else 0.0

    def draw(self, force=False):
        with self.lock:
            now = time.monotonic()
            if not force and now - self.last_draw < 0.15:
                return
            self.last_draw = now
            current = sum(self.current_uploaded.values())
            done = self.completed_bytes + current
            ratio = min(done / self.total_bytes, 1.0) if self.total_bytes else 0.0
            speed = self._speed_unlocked()
            eta = (self.total_bytes - done) / speed if speed > 0 else None
            payload = {
                "kind": "MIXED",
                "ratio": ratio,
                "speed": speed,
                "eta": eta,
                "detail": self.group_name,
                "album_number": self.album_number,
                "album_total": self.album_total,
                "done_files": self.completed_files,
                "total_files": self.total_files,
                "done_bytes": done,
                "total_bytes": self.total_bytes,
            }
        UI.progress(**payload)

    def finish_album(self, items):
        with self.lock:
            self.completed_bytes += sum(self.sizes[item["path"]] for item in items)
            self.completed_files += len(items)
            self.current_paths = {}
            self.current_uploaded = {}
            self.file_id_to_path = {}
        self.draw(force=True)
        UI.finish()


def _validate_config():
    if cfg.API_ID == 12345678 or cfg.API_HASH == "YOUR_API_HASH":
        raise RuntimeError("请先在 config.toml 中填写 API_ID / API_HASH。")
    if getattr(cfg, "TARGET_MODE", "forum_topic") == "channel":
        if cfg.CHAT_ID in {0, -1001234567890}:
            raise RuntimeError("请先在 config.toml 中填写频道 Chat ID。")
    elif cfg.CHAT_ID in {0, -1001234567890} or cfg.FORUM_TOPIC_ID <= 0 or cfg.FORUM_TOPIC_ID == 12345:
        raise RuntimeError("请先在 config.toml 中填写 CHAT_ID / FORUM_TOPIC_ID。")


def main():
    activate = getattr(cfg, "activate_target", None)
    if callable(activate):
        activate("mixed")
    _validate_config()
    version = verify_tdjson_version()
    UI.banner(f"TDLib Media Uploader V{cfg.APP_VERSION}", f"混合模式 · tdjson {version}", accent="cyan")
    cancel_event = getattr(UI, "cancel_event", None)
    cleanup_staging_cache(startup=True)
    groups = (
        scan_mixed_groups()
        if cancel_event is None
        else scan_mixed_groups(cancel_event=cancel_event)
    )
    report_scan_size_skips(LAST_SCAN_SIZE_SKIPS, UI)
    if LAST_SCAN_ERRORS:
        UI.warning(
            f"混合目录扫描跳过 {len(LAST_SCAN_ERRORS)} 个暂时不可读取的项目；"
            "网络恢复后重新扫描即可重试。"
        )
        UI.log("混合目录扫描详情：\n" + "\n".join(LAST_SCAN_ERRORS))
    if LAST_SCAN_IGNORED_ROOT_MEDIA:
        UI.warning(
            f"混合根目录中有 {len(LAST_SCAN_IGNORED_ROOT_MEDIA)} 个媒体已忽略；"
            "请将文件移动到一级子文件夹后重新扫描。"
        )
    items = flatten_items(groups)
    if not items:
        UI.warning("没有找到支持的图片或视频。")
        return
    state = UploadState()
    completed = [item for item in items if state.is_completed(item["path"])]
    pending = [item for item in items if not state.is_completed(item["path"])]
    skipped = [record for record in LAST_SCAN_SIZE_SKIPS if record.get("action") == "skip"]
    # Build the complete plans before any preflight.  The plan's ``items``
    # list is the sole Album boundary; deferred files are filtered only from
    # this run's send list and never cause later files to move forward.
    plans = build_album_plans(groups, state)
    preflight_skipped_paths = set()
    if pending:
        preflight = (
            preflight_mixed(pending, UI)
            if cancel_event is None
            else preflight_mixed(pending, UI, cancel_event=cancel_event)
        )
        skipped.extend(preflight)
        preflight_skipped_paths = {
            stable_path(record["path"]) for record in preflight
        }
    pending_plans = [plan for plan in plans if plan["pending_items"]]
    sendable_pending = [
        item for item in pending
        if stable_path(item["path"]) not in preflight_skipped_paths
    ]
    if not pending or not sendable_pending:
        report_skipped_mixed(skipped, UI)
        if pending and skipped:
            UI.warning("本轮没有可上传的混合媒体；暂时不可读文件未写入断点。")
        else:
            UI.success("所有混合媒体都已上传完成。")
        return
    if not UI.confirm_upload():
        UI.cancelled()
        return
    client = TDJsonClient(UI, "TDLib Mixed Album Uploader")
    progress = MixedUploadProgress(items, completed)
    if preflight_skipped_paths:
        progress.skip_items([
            item
            for item in pending
            if stable_path(item["path"]) in preflight_skipped_paths
        ])
    client.add_update_callback(progress.handle_update)
    try:
        client.login()
        client.set_fast_options()
        client.validate_target()
        for index, plan in enumerate(pending_plans, 1):
            # Keep the original plan boundary even when one item was deferred
            # during preflight.  Only the current send list is filtered.
            album_items = [
                item for item in plan["pending_items"]
                if stable_path(item["path"]) not in preflight_skipped_paths
            ]
            if not album_items:
                continue
            label = with_filename_description(
                plan["caption"]["text"],
                album_items,
                cfg.MIXED_CAPTION_INCLUDE_FILENAMES,
                cfg.MIXED_CAPTION_INCLUDE_FILENAME_NUMBERS,
            )
            if cancel_event is None:
                contents, ready, runtime_skipped = build_mixed_contents(
                    album_items, label, UI
                )
            else:
                contents, ready, runtime_skipped = build_mixed_contents(
                    album_items, label, UI, cancel_event
                )
            if runtime_skipped:
                skipped.extend(runtime_skipped)
                progress.skip_items([record["item"] for record in runtime_skipped])
            if not ready:
                continue
            if (
                ready != album_items
                and cfg.MIXED_CAPTION_INCLUDE_FILENAMES
                and contents
            ):
                # Keep the stable plan membership, but avoid naming a file
                # that was deferred at JIT preparation time. Only the first
                # content carries the Album caption, so no second media read
                # is needed to update it.
                label = with_filename_description(
                    plan["caption"]["text"],
                    ready,
                    True,
                    cfg.MIXED_CAPTION_INCLUDE_FILENAME_NUMBERS,
                )
                contents[0]["caption"] = formatted_text(label)
            progress.begin_album(ready, plan["group_name"], index, len(pending_plans))
            UI.album(
                kind="MIXED",
                title=f"{plan['group_name']} · Album {plan['number']} · {index}/{len(pending_plans)}",
                subtitle=f"{len(ready)} 个媒体 · Caption={label or '无'}",
                rows=[
                    f"{item['media_kind']} · {format_size(progress.sizes.get(item['path'], item.get('scan_size', 0)))} · "
                    f"{relative_name(item['path'])}"
                    for item in ready
                ],
            )
            message_ids = client.send_contents(contents, progress, ready)
            state.mark_album_completed(ready, message_ids)
            progress.finish_album(ready)
            UI.success(f"混合 Album 发送成功：{plan['group_name']} / {plan['number']}")
        report_skipped_mixed(skipped, UI)
        UI.banner("全部混合媒体上传完成", f"共完成 {len(pending_plans)} 个 Album · 断点已保存", accent="green")
    finally:
        client.remove_update_callback(progress.handle_update)
        client.close()
        cleanup_staging_cache()
        image_core.cleanup_compressed_images()
