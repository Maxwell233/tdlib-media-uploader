# -*- coding: utf-8 -*-
"""TDLib 图片批量 Album 上传器：递归扫描、分组编号与可编辑 Caption。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from album_metadata import CaptionStore, album_key, compose_caption, with_filename_description
from path_utils import display_path, file_mtime, iter_files, relative_name as stable_relative_name, stable_path
import app_config as cfg
from tdlib_common import HeadlessUI, TDJsonClient, formatted_text, verify_tdjson_version

PROJECT_DIR = Path(__file__).resolve().parent
STATE_DIR = PROJECT_DIR / ".image_state"
LAST_SCAN_ERRORS: list[str] = []

UI = HeadlessUI()


def format_size(value: float) -> str:
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def format_duration(seconds) -> str:
    if seconds is None:
        return "--:--"
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


def relative_name(path: Path) -> str:
    return stable_relative_name(path, cfg.IMAGE_DIR)


def file_signature(path: Path) -> str:
    stat = path.stat()
    raw = f"{relative_name(path).lower()}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def scan_images() -> list[Path]:
    global LAST_SCAN_ERRORS
    root = cfg.IMAGE_DIR
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"图片目录不存在或不是目录：{root}")
    images, LAST_SCAN_ERRORS = iter_files(root, cfg.IMAGE_EXTENSIONS)

    if cfg.IMAGE_SORT_MODE == "mtime":
        images.sort(key=lambda p: (file_mtime(p), relative_name(p).lower()))
    else:
        images.sort(key=lambda p: relative_name(p).lower())
    return images


_IMAGE_INFO_CACHE = {}


def image_info(path: Path) -> tuple[int, int]:
    stat = path.stat()
    key = (stable_path(path), stat.st_size, stat.st_mtime_ns)
    if key in _IMAGE_INFO_CACHE:
        return _IMAGE_INFO_CACHE[key]
    try:
        with Image.open(path) as image:
            width, height = int(image.width), int(image.height)
            image.verify()
    except Exception as exc:
        raise RuntimeError(f"无法读取图片：{path}\n{type(exc).__name__}: {exc}") from exc
    if width <= 0 or height <= 0:
        raise RuntimeError(f"图片尺寸异常：{path}")
    _IMAGE_INFO_CACHE[key] = (width, height)
    return width, height


def input_photo(path: Path, caption: str = "") -> dict:
    width, height = image_info(path)
    return {
        "@type": "inputMessagePhoto",
        "photo": {"@type": "inputFileLocal", "path": display_path(path)},
        "thumbnail": None,
        "added_sticker_file_ids": [],
        "width": width,
        "height": height,
        "caption": formatted_text(caption),
        "show_caption_above_media": False,
        "self_destruct_type": None,
        "has_spoiler": False,
    }


class UploadState:
    VERSION = 1

    def __init__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        identity_suffix = (
            "tdlib-image-v5"
            if getattr(cfg, "TARGET_MODE", "forum_topic") == "forum_topic"
            else "tdlib-image-v5-channel"
        )
        identity = f"{stable_path(cfg.IMAGE_DIR)}|{cfg.CHAT_ID}|{cfg.FORUM_TOPIC_ID}|{identity_suffix}"
        task_hash = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
        self.path = STATE_DIR / f"image_upload_state_{task_hash}.json"
        self.lock = threading.Lock()
        if cfg.IMAGE_RESET_STATE and self.path.exists():
            self.path.unlink()
        self.data = self._load()

    def _new(self):
        return {
            "version": self.VERSION,
            "image_dir": stable_path(cfg.IMAGE_DIR),
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
            raise RuntimeError(f"图片断点文件读取失败：{self.path}\n{exc}") from exc
        if data.get("version") != self.VERSION:
            raise RuntimeError(f"图片断点文件版本不兼容：{self.path}")
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
        return file_signature(path) in self.data["completed"]

    def mark_album_completed(self, paths: list[Path], message_ids: list[int]):
        with self.lock:
            for index, path in enumerate(paths):
                stat = path.stat()
                self.data["completed"][file_signature(path)] = {
                    "relative_path": relative_name(path),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "message_id": message_ids[index] if index < len(message_ids) else None,
                    "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            self._save(self.data)


class ImageUploadProgress:
    def __init__(self, all_paths: list[Path], completed_paths: list[Path]):
        self.sizes = {path: path.stat().st_size for path in all_paths}
        self.total_bytes = sum(self.sizes.values())
        self.total_files = len(all_paths)
        self.completed_bytes = sum(self.sizes[path] for path in completed_paths)
        self.completed_files = len(completed_paths)
        self.current_paths = {}
        self.current_uploaded = {}
        self.file_id_to_path = {}
        self.album_number = 0
        self.album_total = 0
        self.samples = deque()
        self.last_draw = 0.0
        self.lock = threading.Lock()

    def begin_album(self, paths: list[Path], number: int, total: int):
        with self.lock:
            self.album_number = number
            self.album_total = total
            self.current_paths = {stable_path(p): p for p in paths}
            self.current_uploaded = {p: 0 for p in paths}
            self.file_id_to_path = {}
            self.samples.clear()
            self.last_draw = 0.0

    @staticmethod
    def _photo_file(message):
        content = message.get("content", {})
        if content.get("@type") != "messagePhoto":
            return None
        sizes = content.get("photo", {}).get("sizes", [])
        if not sizes:
            return None
        largest = max(sizes, key=lambda s: int(s.get("width", 0)) * int(s.get("height", 0)))
        return largest.get("photo")

    def register_messages(self, messages, paths):
        with self.lock:
            for message, path in zip(messages, paths):
                file_obj = self._photo_file(message)
                if not file_obj:
                    continue
                if file_obj.get("id") is not None:
                    self.file_id_to_path[file_obj["id"]] = path
                self._apply_unlocked(file_obj)

    def _apply_unlocked(self, file_obj):
        path = None
        local_path = file_obj.get("local", {}).get("path", "")
        if local_path:
            path = self.current_paths.get(stable_path(local_path))
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
            kwargs = dict(
                kind="IMAGE",
                ratio=ratio,
                speed=speed,
                eta=eta,
                detail="每组最多 10 张；Album Caption=编号/自定义文本",
                album_number=self.album_number,
                album_total=self.album_total,
                done_files=self.completed_files,
                total_files=self.total_files,
                done_bytes=done,
                total_bytes=self.total_bytes,
            )
        UI.progress(**kwargs)

    def finish_album(self, paths):
        with self.lock:
            self.completed_bytes += sum(self.sizes[path] for path in paths)
            self.completed_files += len(paths)
            self.current_paths = {}
            self.current_uploaded = {}
            self.file_id_to_path = {}
        self.draw(force=True)
        UI.finish()


def show_file_list(images, state):
    rows = []
    for index, path in enumerate(images, 1):
        stat = path.stat()
        rows.append((
            index,
            "✓ 已完成" if state.is_completed(path) else "• 待上传",
            datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            format_size(stat.st_size),
            relative_name(path),
        ))

    UI.files(
        f"图片文件 · 共 {len(images)} 张",
        [
            ("#", {"justify": "right", "width": 5}),
            ("状态", {"no_wrap": True, "width": 10}),
            ("修改时间", {"no_wrap": True, "width": 19}),
            ("大小", {"justify": "right", "no_wrap": True, "width": 11}),
            ("文件", {"overflow": "fold"}),
        ],
        rows,
        kind="IMAGE",
        caption="图片按扫描顺序每组编号；可在 GUI 中编辑每个 Album 的 Caption。",
    )


def show_upload_summary(images, state, completed, pending, total_albums):
    total_bytes = sum(path.stat().st_size for path in images)
    pending_bytes = sum(path.stat().st_size for path in pending)

    UI.summary(
        f"上传前确认 · TDLib Media Uploader V{cfg.APP_VERSION}",
        [
            ("上传引擎", "TDLib 原生 C++ / tdjson"),
            ("图片目录", cfg.IMAGE_DIR),
            ("扫描图片", len(images)),
            ("排序方式", cfg.IMAGE_SORT_MODE),
            ("全部大小", format_size(total_bytes)),
            ("断点已完成", f"{len(completed)}/{len(images)}"),
            ("本次待上传", f"{len(pending)} · {format_size(pending_bytes)}"),
            ("本次 Album", total_albums),
            (
                "Album 规则",
                f"每组最多 {cfg.IMAGE_ALBUM_SIZE} 张；"
                f"Album Caption=编号，可追加自定义文本；"
                f"文件名清单={'开' if getattr(cfg, 'IMAGE_CAPTION_INCLUDE_FILENAMES', False) else '关'}",
            ),
            ("CHAT_ID", cfg.CHAT_ID),
            ("目标模式", "Channel 频道" if getattr(cfg, "TARGET_MODE", "forum_topic") == "channel" else "超级群组 Forum Topic"),
            ("FORUM_TOPIC_ID", cfg.FORUM_TOPIC_ID if getattr(cfg, "TARGET_MODE", "forum_topic") == "forum_topic" else "不适用"),
            ("状态文件", state.path),
        ],
        kind="IMAGE",
    )

def validate_config():
    if cfg.API_ID == 12345678 or cfg.API_HASH == "YOUR_API_HASH":
        raise RuntimeError("请先在 config.toml 中填写 API_ID / API_HASH。")
    if getattr(cfg, "TARGET_MODE", "forum_topic") == "channel":
        if cfg.CHAT_ID in {0, -1001234567890}:
            raise RuntimeError("请先在 config.toml 中填写频道 Chat ID。")
    elif cfg.CHAT_ID in {0, -1001234567890} or cfg.FORUM_TOPIC_ID <= 0 or cfg.FORUM_TOPIC_ID == 12345:
        raise RuntimeError("请先在 config.toml 中填写 CHAT_ID / FORUM_TOPIC_ID。")


def build_album_plans(images: list[Path], state=None) -> list[dict]:
    """Build stable image Albums from the complete scan, not pending-only files."""
    store = CaptionStore("image")
    plans = []
    for start in range(0, len(images), cfg.IMAGE_ALBUM_SIZE):
        album_paths = list(images[start:start + cfg.IMAGE_ALBUM_SIZE])
        number = start // cfg.IMAGE_ALBUM_SIZE + cfg.IMAGE_ALBUM_NUMBER_START
        key = album_key("image", f"Album {number}", album_paths)
        pending = [
            path for path in album_paths
            if state is None or not state.is_completed(path)
        ]
        record = store.get(key, str(number))
        base_label = record["base_label"] if record["base_label"] != f"Album {number}" else str(number)
        caption = {
            "base_label": base_label,
            "custom_text": record["custom_text"],
            "text": compose_caption(
                base_label if getattr(cfg, "IMAGE_ALBUM_NUMBERING", True) else "",
                record["custom_text"],
                getattr(cfg, "IMAGE_ALBUM_CAPTION_SEPARATOR", " · "),
            ),
        }
        plans.append({
            "key": key,
            "number": number,
            "items": album_paths,
            "pending_items": pending,
            "caption": caption,
        })
    return plans


def main():
    activate = getattr(cfg, "activate_target", None)
    if callable(activate):
        activate("image")
    validate_config()
    version = verify_tdjson_version()

    UI.banner(
        f"TDLib Media Uploader V{cfg.APP_VERSION}",
        f"图片模式 · tdjson {version}",
        accent="magenta",
    )

    UI.info(f"扫描图片目录：{cfg.IMAGE_DIR}")
    images = scan_images()
    if not images:
        UI.warning("没有找到支持的图片。")
        return

    state = UploadState()
    completed = [p for p in images if state.is_completed(p)]
    pending = [p for p in images if not state.is_completed(p)]
    plans = build_album_plans(images, state)
    pending_plans = [plan for plan in plans if plan["pending_items"]]
    total_albums = len(pending_plans)

    # GUI 调用的扫描与上传流程：保留原有 Album 与断点逻辑。
    if cfg.IMAGE_SHOW_FILE_LIST:
        show_file_list(images, state)

    show_upload_summary(images, state, completed, pending, total_albums)

    if not pending:
        UI.success("所有图片都已上传完成。")
        return

    if cfg.IMAGE_VERIFY_ALL_IMAGES:
        UI.info("开始预检本次待上传图片…")
        for index, path in enumerate(pending, 1):
            UI.info(f"预检 {index}/{len(pending)} · {path.name}")
            image_info(path)
        UI.success("图片预检完成。")

    if not UI.confirm_upload():
        UI.cancelled()
        return

    client = TDJsonClient(UI, "TDLib Image Album Uploader")
    progress = ImageUploadProgress(images, completed)
    client.add_update_callback(progress.handle_update)

    try:
        client.login()
        client.set_fast_options()
        client.validate_target()

        album_global = 0
        for plan in pending_plans:
            album_paths = plan["pending_items"]
            album_number = plan["number"]
            album_global += 1
            caption = plan["caption"]["text"]
            caption = with_filename_description(
                caption,
                album_paths,
                getattr(cfg, "IMAGE_CAPTION_INCLUDE_FILENAMES", False),
            )
            progress.begin_album(album_paths, album_global, total_albums)
            UI.album(
                kind="IMAGE",
                title=f"Album {album_number} · {album_global}/{total_albums}",
                subtitle=f"{len(album_paths)} 张待上传图片 · Caption={caption or '无'}",
                rows=[
                    f"{format_size(path.stat().st_size):>10}  {relative_name(path)}"
                    for path in album_paths
                ],
            )
            try:
                contents = [
                    input_photo(path, caption if index == 0 else "")
                    for index, path in enumerate(album_paths)
                ]
                message_ids = client.send_contents(contents, progress, album_paths)
            except Exception:
                UI.finish()
                UI.error("当前图片 Album 未写入断点；下次会重新处理这一组。")
                raise
            state.mark_album_completed(album_paths, message_ids)
            progress.finish_album(album_paths)
            UI.success(f"图片 Album {album_number} 发送成功 · Caption={caption or '无'} · 断点已保存。")

        UI.banner(
            "全部图片上传完成",
            f"共完成 {total_albums} 个 Album · 断点已保存",
            accent="green",
        )
    finally:
        client.remove_update_callback(progress.handle_update)
        client.close()
