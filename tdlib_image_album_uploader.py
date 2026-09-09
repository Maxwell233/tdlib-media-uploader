# -*- coding: utf-8 -*-
"""TDLib 图片批量 Album 上传器：递归扫描、分组编号与可编辑 Caption。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
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
from runtime_paths import APP_DATA_DIR, RESOURCE_DIR

PROJECT_DIR = RESOURCE_DIR
STATE_DIR = APP_DATA_DIR / ".image_state"
LAST_SCAN_ERRORS: list[str] = []
LAST_SCAN_SIZE_SKIPS: list[dict] = []
IMAGE_UPLOAD_PATHS: dict[str, Path] = {}
COMPRESSED_IMAGE_DIR = APP_DATA_DIR / ".image_compression"

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
    global LAST_SCAN_ERRORS, LAST_SCAN_SIZE_SKIPS
    root = cfg.IMAGE_DIR
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"图片目录不存在或不是目录：{root}")
    images, LAST_SCAN_ERRORS = iter_files(root, cfg.IMAGE_EXTENSIONS)
    LAST_SCAN_SIZE_SKIPS = []
    accepted = []
    for path in images:
        try:
            size = path.stat().st_size
        except OSError as exc:
            LAST_SCAN_ERRORS.append(f"{path}: {exc}")
            continue
        if size > cfg.IMAGE_MAX_BYTES:
            record = {
                "path": path,
                "size": size,
                "limit": cfg.IMAGE_MAX_BYTES,
                "category": "size",
                "action": "compress" if cfg.IMAGE_COMPRESS_OVERSIZE else "skip",
                "reason": (
                    f"文件大小 {format_size(size)} 超过 Telegram Photo 上限 "
                    f"{format_size(cfg.IMAGE_MAX_BYTES)}"
                ),
            }
            LAST_SCAN_SIZE_SKIPS.append(record)
            if not cfg.IMAGE_COMPRESS_OVERSIZE:
                continue
        accepted.append(path)
    images = accepted

    if cfg.IMAGE_SORT_MODE == "mtime":
        images.sort(key=lambda p: (file_mtime(p), relative_name(p).lower()))
    else:
        images.sort(key=lambda p: relative_name(p).lower())
    return images


_IMAGE_INFO_CACHE = {}


def _hidden_subprocess_kwargs() -> dict:
    """Keep FFmpeg from opening a console window in the Windows GUI build."""

    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": startupinfo,
    }


def _find_ffmpeg() -> str | None:
    configured = os.environ.get("IMAGEIO_FFMPEG_EXE", "").strip()
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for candidate in (
        PROJECT_DIR / "tools" / "ffmpeg" / name,
        PROJECT_DIR / "tools" / name,
        APP_DATA_DIR / "tools" / "ffmpeg" / name,
        APP_DATA_DIR / "tools" / name,
    ):
        if candidate.is_file():
            return str(candidate.resolve())
    return shutil.which("ffmpeg")


def _compressed_path(path: Path) -> Path:
    stat = path.stat()
    digest = hashlib.sha1(
        f"{stable_path(path)}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()
    return COMPRESSED_IMAGE_DIR / f"{digest}.jpg"


def compress_image(path: Path) -> Path:
    """Create a temporary JPEG under the Telegram photo limit with FFmpeg."""

    target = int(getattr(cfg, "IMAGE_COMPRESSION_TARGET_BYTES", int(9.5 * 1024 ** 2)))
    final_path = _compressed_path(path)
    if final_path.is_file() and final_path.stat().st_size <= target:
        try:
            with Image.open(final_path) as image:
                image.verify()
            return final_path
        except (OSError, ValueError):
            final_path.unlink(missing_ok=True)

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("找不到 FFmpeg，无法压缩超限图片")
    COMPRESSED_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    last_error = ""
    # First preserve the original dimensions, then reduce dimensions only if
    # quality reduction alone cannot get under the safety margin.
    for scale, qualities in (
        (1.0, (2, 4, 6, 8, 10, 12, 15, 18, 22, 26, 30, 34)),
        (0.9, (4, 8, 12, 16, 20, 24, 28, 32)),
        (0.8, (4, 8, 12, 16, 20, 24, 28, 32)),
        (0.7, (4, 8, 12, 16, 20, 24, 28, 32)),
        (0.6, (4, 8, 12, 16, 20, 24, 28, 32)),
    ):
        if scale == 1.0:
            video_filter = "format=yuv420p"
        else:
            video_filter = (
                f"scale=trunc(iw*{scale}/2)*2:trunc(ih*{scale}/2)*2,format=yuv420p"
            )
        for quality in qualities:
            temp_path = final_path.with_name(
                f".{final_path.stem}.{scale:g}.{quality}.tmp.jpg"
            )
            temp_path.unlink(missing_ok=True)
            try:
                result = subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(path),
                        "-map_metadata",
                        "-1",
                        "-frames:v",
                        "1",
                        "-vf",
                        video_filter,
                        "-c:v",
                        "mjpeg",
                        "-q:v",
                        str(quality),
                        str(temp_path),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    **_hidden_subprocess_kwargs(),
                )
                last_error = result.stderr.strip() or f"FFmpeg 退出码 {result.returncode}"
                if result.returncode == 0 and temp_path.is_file() and temp_path.stat().st_size > 0:
                    with Image.open(temp_path) as image:
                        image.verify()
                    if temp_path.stat().st_size <= target:
                        os.replace(temp_path, final_path)
                        return final_path
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            finally:
                temp_path.unlink(missing_ok=True)
    raise RuntimeError(
        f"FFmpeg 无法将图片压到 {format_size(target)} 以下"
        + (f"：{last_error[:300]}" if last_error else "")
    )


def upload_path(path: Path) -> Path:
    return IMAGE_UPLOAD_PATHS.get(stable_path(path), path)


def cleanup_compressed_images() -> None:
    paths = set(IMAGE_UPLOAD_PATHS.values())
    IMAGE_UPLOAD_PATHS.clear()
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        if COMPRESSED_IMAGE_DIR.is_dir() and not any(COMPRESSED_IMAGE_DIR.iterdir()):
            COMPRESSED_IMAGE_DIR.rmdir()
    except OSError:
        pass


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


def preflight_images(paths, ui=None) -> list[dict]:
    """Find unreadable images without aborting the complete upload task."""

    target = ui or UI
    skipped = []
    total = len(paths)
    for index, path in enumerate(paths, 1):
        try:
            size = path.stat().st_size
            if size > cfg.IMAGE_MAX_BYTES:
                reason = (
                    f"文件大小 {format_size(size)} 超过 Telegram Photo 上限 "
                    f"{format_size(cfg.IMAGE_MAX_BYTES)}"
                )
                if not cfg.IMAGE_COMPRESS_OVERSIZE:
                    raise RuntimeError(reason)
                # Compression is deliberately deferred until the image is
                # actually being assembled for upload, after confirmation.
                target.info(
                    f"预检发现超限图片，将在上传时使用 FFmpeg 压缩：{relative_name(path)}"
                )
                IMAGE_UPLOAD_PATHS.pop(stable_path(path), None)
                image_info(path)
            else:
                IMAGE_UPLOAD_PATHS.pop(stable_path(path), None)
                image_info(path)
            if getattr(cfg, "IMAGE_VERIFY_ALL_IMAGES", False):
                target.info(f"预检图片 {index}/{total} · {path.name}")
        except Exception as exc:
            record = {
                "path": path,
                "reason": f"{type(exc).__name__}: {exc}",
                "category": "size" if "Telegram Photo 上限" in str(exc) else "unreadable",
            }
            skipped.append(record)
            target.warning(f"跳过图片：{relative_name(path)}")
            target.log(f"跳过图片详情：{path}\n原因：{record['reason']}")
    return skipped


def report_skipped_images(skipped, ui=None, *, final=False) -> None:
    if not skipped:
        return
    target = ui or UI
    prefix = "本次任务结束" if final else "图片预检完成"
    size_count = sum(record.get("category") == "size" for record in skipped)
    unreadable_count = len(skipped) - size_count
    parts = []
    if unreadable_count:
        parts.append(f"{unreadable_count} 个无法读取的图片")
    if size_count:
        parts.append(f"{size_count} 个超过 10 MiB 上限的图片")
    target.warning(
        f"{prefix}：已跳过{'、'.join(parts) or f'{len(skipped)} 个图片'}；"
        "这些文件未写入上传断点，修复后可重新扫描上传。"
    )


def report_scan_size_skips(skipped, ui=None) -> None:
    """Report image size decisions made while scanning the directory."""

    if not skipped:
        return
    target = ui or UI
    compressing = [record for record in skipped if record.get("action") == "compress"]
    rejected = [record for record in skipped if record.get("action") == "skip"]
    if compressing:
        target.warning(
            f"扫描提醒：发现 {len(compressing)} 个超过 10 MiB 的图片；"
            "上传时将尝试用 FFmpeg 生成临时压缩副本。"
        )
    if rejected:
        target.warning(
            f"扫描时跳过 {len(rejected)} 个超过 Telegram Photo 10 MiB 上限的图片；"
            "这些文件未加入上传计划。"
        )
    for record in skipped:
        target.log(
            f"扫描图片大小检查：{record['path']}\n"
            f"处理：{'上传时压缩' if record.get('action') == 'compress' else '跳过'}\n"
            f"原因：{record['reason']}"
        )


def input_photo(path: Path, caption: str = "") -> dict:
    source_path = upload_path(path)
    if path.stat().st_size > cfg.IMAGE_MAX_BYTES and cfg.IMAGE_COMPRESS_OVERSIZE:
        original_size = path.stat().st_size
        UI.warning(
            f"图片开始上传，正在使用 FFmpeg 生成临时压缩副本：{relative_name(path)}"
        )
        source_path = compress_image(path)
        IMAGE_UPLOAD_PATHS[stable_path(path)] = source_path
        UI.info(
            f"图片压缩完成：{relative_name(path)} · "
            f"{format_size(original_size)} → {format_size(source_path.stat().st_size)}；原文件未修改"
        )
    elif path.stat().st_size > cfg.IMAGE_MAX_BYTES:
        raise RuntimeError(
            f"文件大小 {format_size(path.stat().st_size)} 超过 Telegram Photo 上限 "
            f"{format_size(cfg.IMAGE_MAX_BYTES)}"
        )
    source_path = upload_path(path)
    width, height = image_info(source_path)
    return {
        "@type": "inputMessagePhoto",
        "photo": {"@type": "inputFileLocal", "path": display_path(source_path)},
        "thumbnail": None,
        "added_sticker_file_ids": [],
        "width": width,
        "height": height,
        "caption": formatted_text(caption),
        "show_caption_above_media": False,
        "self_destruct_type": None,
        "has_spoiler": False,
    }


def build_image_contents(paths, caption: str, ui=None):
    """Build an Album while isolating images that became unreadable later."""

    target = ui or UI
    contents = []
    valid_paths = []
    skipped = []
    for path in paths:
        try:
            contents.append(input_photo(path, caption if not valid_paths else ""))
            valid_paths.append(path)
        except Exception as exc:
            try:
                current_size = path.stat().st_size
            except OSError:
                current_size = 0
            record = {
                "path": path,
                "reason": f"{type(exc).__name__}: {exc}",
                "category": "size" if current_size > cfg.IMAGE_MAX_BYTES else "unreadable",
            }
            skipped.append(record)
            target.warning(f"跳过上传前变得无法读取的图片：{relative_name(path)}")
            target.log(f"跳过图片详情：{path}\n原因：{record['reason']}")
    return contents, valid_paths, skipped


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

    def skip_items(self, paths: list[Path]):
        """Remove files that failed local preparation from progress totals."""

        with self.lock:
            for path in paths:
                size = self.sizes.pop(path, 0)
                self.total_bytes = max(0, self.total_bytes - size)
                self.total_files = max(0, self.total_files - 1)
                self.current_uploaded.pop(path, None)
        self.draw(force=True)

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


def show_upload_summary(images, state, completed, pending, total_albums, skipped_items):
    total_bytes = sum(path.stat().st_size for path in images)
    pending_bytes = sum(path.stat().st_size for path in pending)

    UI.summary(
        f"上传前确认 · TDLib Media Uploader V{cfg.APP_VERSION}",
        [
            ("上传引擎", "TDLib 原生 C++ / tdjson"),
            ("图片目录", cfg.IMAGE_DIR),
            ("扫描图片", len(images)),
            ("跳过坏图片", len(skipped_items)),
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
    report_scan_size_skips(LAST_SCAN_SIZE_SKIPS, UI)
    if not images:
        UI.warning("没有找到支持的图片。")
        return

    state = UploadState()
    completed = [p for p in images if state.is_completed(p)]
    pending = [p for p in images if not state.is_completed(p)]
    skipped_items = [
        record for record in LAST_SCAN_SIZE_SKIPS
        if record.get("action") == "skip"
    ]
    if pending:
        UI.info(f"检查 {len(pending)} 个待上传图片的媒体数据…")
        preflight_skipped = preflight_images(pending, UI)
        skipped_items.extend(preflight_skipped)
        skipped_paths = {
            stable_path(record["path"])
            for record in preflight_skipped
        }
        if skipped_paths:
            images = [
                path
                for path in images
                if stable_path(path) not in skipped_paths
            ]
            completed = [path for path in images if state.is_completed(path)]
            pending = [path for path in images if not state.is_completed(path)]
            report_skipped_images(skipped_items, UI)
    plans = build_album_plans(images, state)
    pending_plans = [plan for plan in plans if plan["pending_items"]]
    total_albums = len(pending_plans)

    # GUI 调用的扫描与上传流程：保留原有 Album 与断点逻辑。
    if cfg.IMAGE_SHOW_FILE_LIST:
        show_file_list(images, state)

    show_upload_summary(images, state, completed, pending, total_albums, skipped_items)

    if not pending:
        if skipped_items:
            report_skipped_images(skipped_items, UI, final=True)
        else:
            UI.success("所有图片都已上传完成。")
        cleanup_compressed_images()
        return

    if not UI.confirm_upload():
        cleanup_compressed_images()
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
            caption = plan["caption"]["text"]
            caption = with_filename_description(
                caption,
                album_paths,
                getattr(cfg, "IMAGE_CAPTION_INCLUDE_FILENAMES", False),
            )
            contents, ready_paths, runtime_skipped = build_image_contents(
                album_paths,
                caption,
                UI,
            )
            if runtime_skipped:
                skipped_items.extend(runtime_skipped)
                progress.skip_items([record["path"] for record in runtime_skipped])
            if not ready_paths:
                UI.warning("当前图片 Album 没有可读取的图片，已跳过。")
                continue
            if ready_paths != album_paths and getattr(cfg, "IMAGE_CAPTION_INCLUDE_FILENAMES", False):
                caption = with_filename_description(
                    plan["caption"]["text"],
                    ready_paths,
                    True,
                )
                contents, rebuilt_paths, rebuilt_skipped = build_image_contents(
                    ready_paths,
                    caption,
                    UI,
                )
                if rebuilt_skipped:
                    skipped_items.extend(rebuilt_skipped)
                    progress.skip_items([record["path"] for record in rebuilt_skipped])
                ready_paths = rebuilt_paths
                if not ready_paths:
                    UI.warning("当前图片 Album 没有可读取的图片，已跳过。")
                    continue
            album_global += 1
            progress.begin_album(ready_paths, album_global, total_albums)
            UI.album(
                kind="IMAGE",
                title=f"Album {album_number} · {album_global}/{total_albums}",
                subtitle=f"{len(ready_paths)} 张待上传图片 · Caption={caption or '无'}",
                rows=[
                    f"{format_size(path.stat().st_size):>10}  {relative_name(path)}"
                    for path in ready_paths
                ],
            )
            try:
                message_ids = client.send_contents(contents, progress, ready_paths)
            except Exception:
                UI.finish()
                UI.error("当前图片 Album 未写入断点；下次会重新处理这一组。")
                raise
            state.mark_album_completed(ready_paths, message_ids)
            progress.finish_album(ready_paths)
            UI.success(f"图片 Album {album_number} 发送成功 · Caption={caption or '无'} · 断点已保存。")

        UI.banner(
            "全部图片上传完成",
            f"共完成 {total_albums} 个 Album · 断点已保存",
            accent="green",
        )
        report_skipped_images(skipped_items, UI, final=True)
    finally:
        client.remove_update_callback(progress.handle_update)
        client.close()
        cleanup_compressed_images()
