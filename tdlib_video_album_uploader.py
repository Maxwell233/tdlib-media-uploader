# -*- coding: utf-8 -*-
"""TDLib 视频按月 Album 上传器。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import imageio_ffmpeg
from PIL import Image

from album_metadata import CaptionStore, album_key, compose_caption, with_filename_description
import app_config as cfg
from tdlib_common import HeadlessUI, TDJsonClient, formatted_text, verify_tdjson_version

PROJECT_DIR = Path(__file__).resolve().parent
STATE_DIR = PROJECT_DIR / ".state"
THUMB_CACHE_DIR = PROJECT_DIR / ".thumb_cache"


def _hidden_subprocess_kwargs() -> dict:
    """Return Windows process flags that prevent a console window flash."""

    if os.name != "nt":
        return {}

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": startupinfo,
    }


def _patch_imageio_process_flags() -> None:
    """Make imageio-ffmpeg child processes inherit the same hidden flags."""

    if os.name != "nt":
        return

    try:
        import imageio_ffmpeg._io as imageio_io
        import imageio_ffmpeg._utils as imageio_utils
    except (ImportError, AttributeError):
        return

    original = getattr(imageio_io, "_popen_kwargs", None)
    if not callable(original) or getattr(original, "_tdlib_hidden", False):
        return

    def hidden_imageio_kwargs(prevent_sigint=False):
        result = dict(original(prevent_sigint))
        hidden = _hidden_subprocess_kwargs()
        result["startupinfo"] = hidden["startupinfo"]
        result["creationflags"] = int(result.get("creationflags") or 0) | int(
            hidden["creationflags"]
        )
        return result

    hidden_imageio_kwargs._tdlib_hidden = True
    imageio_io._popen_kwargs = hidden_imageio_kwargs
    if getattr(imageio_utils, "_popen_kwargs", None) is original:
        imageio_utils._popen_kwargs = hidden_imageio_kwargs


_patch_imageio_process_flags()


def _find_ffmpeg_override() -> str | None:
    """Find the LGPL FFmpeg supplied by a portable build or the user.

    imageio-ffmpeg's Windows wheel contains its own FFmpeg executable.  That
    binary is intentionally not used by our release build because its build
    flags may enable GPL components.  Prefer the verified portable binary,
    then an explicit environment override, then a system executable.  The
    environment variable is also how imageio-ffmpeg's reader API receives the
    selected executable.
    """

    configured = os.environ.get("IMAGEIO_FFMPEG_EXE", "").strip()
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())

    names = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg",)
    candidates = [
        PROJECT_DIR / "tools" / "ffmpeg" / names[0],
        PROJECT_DIR / "tools" / names[0],
        PROJECT_DIR / names[0],
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())

    return shutil.which("ffmpeg")


_FFMPEG_OVERRIDE = _find_ffmpeg_override()
if _FFMPEG_OVERRIDE:
    # read_frames()/count_frames_and_secs() resolve their executable through
    # imageio-ffmpeg, so expose the selected binary through its supported
    # environment-variable override without changing the public API.
    os.environ["IMAGEIO_FFMPEG_EXE"] = _FFMPEG_OVERRIDE

UI = HeadlessUI()


def format_size(value: float) -> str:
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def relative_name(path: Path) -> str:
    try:
        return path.resolve().relative_to(cfg.VIDEO_DIR.resolve()).as_posix()
    except ValueError:
        return path.name


def normalize_path(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def file_signature(path: Path) -> str:
    stat = path.stat()
    raw = f"{relative_name(path).lower()}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def month_caption(month_key: str) -> str:
    year_text, month_text = month_key.split("-")
    year, month = int(year_text), int(month_text)
    return f"{year % 100:02d}-{month}" if cfg.VIDEO_CAPTION_YEAR_DIGITS == 2 else f"{year}-{month}"


def scan_videos() -> list[Path]:
    root = cfg.VIDEO_DIR
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"视频目录不存在或不是目录：{root}")
    videos = [
        path for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in cfg.VIDEO_EXTENSIONS
        and path.stat().st_size > 0
    ]
    return videos


def parse_exif_datetime(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("0000-"):
        return None
    text = text.replace("Z", "+00:00")
    if len(text) >= 5:
        tail = text[-5:]
        if tail[0] in "+-" and tail[1:].isdigit():
            text = text[:-5] + tail[:3] + ":" + tail[3:]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            pass
    return None


def read_exif_metadata() -> dict[str, dict]:
    if not cfg.EXIFTOOL_PATH.exists():
        raise RuntimeError(
            f"找不到 ExifTool：{cfg.EXIFTOOL_PATH}\n"
            "请把 exiftool.exe 放入 tools 目录。"
        )
    command = [
        str(cfg.EXIFTOOL_PATH), "-j", "-r", "-a", "-G1", "-s",
        "-api", "LargeFileSupport=1",
        "-d", "%Y-%m-%d %H:%M:%S%z",
        "-time:all",
    ]
    for ext in sorted(cfg.VIDEO_EXTENSIONS):
        command += ["-ext", ext.lstrip(".")]
    command.append(str(cfg.VIDEO_DIR))
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_hidden_subprocess_kwargs(),
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("ExifTool 执行失败：\n" + result.stderr.strip())
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ExifTool JSON 输出无法解析") from exc
    return {
        normalize_path(row["SourceFile"]): row
        for row in rows
        if row.get("SourceFile")
    }


def choose_capture_time(path: Path, row: dict | None):
    row = row or {}
    predicates = [
        lambda key: key.lower() == "keys:creationdate",
        lambda key: key.lower().endswith(":datetimeoriginal"),
        lambda key: key.lower() == "quicktime:creationdate",
        lambda key: key.lower() == "quicktime:createdate",
        lambda key: key.lower().endswith(":createdate") and "file:" not in key.lower() and "track" not in key.lower() and "media" not in key.lower(),
        lambda key: key.lower().endswith(":trackcreatedate"),
        lambda key: key.lower().endswith(":mediacreatedate"),
    ]
    for predicate in predicates:
        for key, value in row.items():
            if key == "SourceFile" or not predicate(key):
                continue
            dt = parse_exif_datetime(value)
            if dt is None:
                continue
            utc_style = (
                key.lower() == "quicktime:createdate"
                or key.lower().endswith(":trackcreatedate")
                or key.lower().endswith(":mediacreatedate")
            )
            if utc_style and cfg.VIDEO_QUICKTIME_UTC_TARGET_ZONE:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt = dt.astimezone(ZoneInfo(cfg.VIDEO_QUICKTIME_UTC_TARGET_ZONE))
            return {"datetime": dt, "tag": key, "fallback": False}
    if cfg.VIDEO_MISSING_DATE_POLICY == "mtime":
        return {
            "datetime": datetime.fromtimestamp(path.stat().st_mtime),
            "tag": "FileSystem:ModifyTime",
            "fallback": True,
        }
    return None


def build_items(videos, metadata_index):
    items, missing = [], []
    for path in videos:
        selected = choose_capture_time(path, metadata_index.get(normalize_path(path)))
        if selected is None:
            missing.append(path)
            continue
        dt = selected["datetime"]
        items.append({
            "path": path,
            "capture_time": dt,
            "month_key": dt.strftime("%Y-%m"),
            "date_tag": selected["tag"],
            "fallback": selected["fallback"],
        })
    items.sort(key=lambda item: (item["capture_time"].replace(tzinfo=None), relative_name(item["path"]).lower()))
    return items, missing


_VIDEO_INFO_CACHE = {}


def video_info(path: Path):
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    if key in _VIDEO_INFO_CACHE:
        return _VIDEO_INFO_CACHE[key]
    reader = imageio_ffmpeg.read_frames(str(path))
    try:
        metadata = next(reader)
    finally:
        reader.close()
    size = metadata.get("size") or metadata.get("source_size")
    duration = float(metadata.get("duration") or 0)
    if not size or len(size) != 2:
        raise RuntimeError(f"FFmpeg 无法读取分辨率：{path.name}")
    if duration <= 0:
        try:
            _, duration = imageio_ffmpeg.count_frames_and_secs(str(path))
            duration = float(duration)
        except Exception:
            duration = 0
    width, height = int(size[0]), int(size[1])
    if width <= 1 or height <= 1 or duration <= 0:
        raise RuntimeError(f"视频媒体属性异常：{path.name} | {width}x{height} | {duration:.3f}s")
    result = {"width": width, "height": height, "duration": duration}
    _VIDEO_INFO_CACHE[key] = result
    return result


def build_thumbnail(path: Path):
    THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stat = path.stat()
    cache_key = hashlib.sha1(
        f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()
    final_path = THUMB_CACHE_DIR / f"{cache_key}.jpg"
    temp_path = THUMB_CACHE_DIR / f"{cache_key}.tmp.jpg"
    if final_path.exists() and final_path.stat().st_size > 0:
        with Image.open(final_path) as image:
            return final_path, image.width, image.height

    ffmpeg = _FFMPEG_OVERRIDE or imageio_ffmpeg.get_ffmpeg_exe()
    process_kwargs = _hidden_subprocess_kwargs()

    def extract(second: float):
        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", str(second), "-i", str(path), "-frames:v", "1",
                "-vf", "scale=640:640:force_original_aspect_ratio=decrease",
                "-q:v", "3", str(temp_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **process_kwargs,
        )
        return result.returncode == 0 and temp_path.exists() and temp_path.stat().st_size > 0

    if not extract(1.0) and not extract(0.0):
        raise RuntimeError(f"无法生成视频预览图：{path}")

    with Image.open(temp_path) as image:
        image = image.convert("RGB")
        image.thumbnail((cfg.VIDEO_THUMB_MAX_EDGE, cfg.VIDEO_THUMB_MAX_EDGE), Image.Resampling.LANCZOS)
        for quality in (85, 75, 65, 55, 45, 35, 25):
            image.save(final_path, "JPEG", quality=quality, optimize=True)
            if final_path.stat().st_size <= cfg.VIDEO_THUMB_TARGET_BYTES:
                break
        width, height = image.width, image.height
    try:
        temp_path.unlink()
    except FileNotFoundError:
        pass
    return final_path, width, height


def input_video(item, caption: str):
    path = item["path"]
    info = video_info(path)
    thumbnail = None
    if cfg.VIDEO_GENERATE_THUMBNAIL:
        thumb_path, thumb_width, thumb_height = build_thumbnail(path)
        thumbnail = {
            "@type": "inputThumbnail",
            "thumbnail": {"@type": "inputFileLocal", "path": str(thumb_path.resolve())},
            "width": int(thumb_width),
            "height": int(thumb_height),
        }
    return {
        "@type": "inputMessageVideo",
        "video": {"@type": "inputFileLocal", "path": str(path.resolve())},
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


class UploadState:
    VERSION = 1

    def __init__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        identity_suffix = (
            "tdlib-video-v5"
            if getattr(cfg, "TARGET_MODE", "forum_topic") == "forum_topic"
            else "tdlib-video-v5-channel"
        )
        identity = f"{cfg.VIDEO_DIR.resolve()}|{cfg.CHAT_ID}|{cfg.FORUM_TOPIC_ID}|{identity_suffix}"
        task_hash = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
        self.path = STATE_DIR / f"upload_state_{task_hash}.json"
        self.lock = threading.Lock()
        if cfg.VIDEO_RESET_STATE and self.path.exists():
            self.path.unlink()
        self.data = self._load()

    def _new(self):
        return {
            "version": self.VERSION,
            "video_dir": str(cfg.VIDEO_DIR.resolve()),
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
            raise RuntimeError(f"断点文件读取失败：{self.path}\n{exc}") from exc
        if data.get("version") != self.VERSION:
            raise RuntimeError(f"断点文件版本不兼容：{self.path}")
        return data

    def _save(self, data):
        data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, self.path)

    def is_completed(self, path: Path):
        return file_signature(path) in self.data["completed"]

    def mark_album_completed(self, items, message_ids):
        with self.lock:
            for index, item in enumerate(items):
                path = item["path"]
                stat = path.stat()
                self.data["completed"][file_signature(path)] = {
                    "relative_path": relative_name(path),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "capture_time": item["capture_time"].isoformat(),
                    "month_key": item["month_key"],
                    "date_tag": item["date_tag"],
                    "message_id": message_ids[index] if index < len(message_ids) else None,
                    "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            self._save(self.data)


class VideoUploadProgress:
    def __init__(self, all_items, completed_items):
        self.sizes = {item["path"]: item["path"].stat().st_size for item in all_items}
        self.total_bytes = sum(self.sizes.values())
        self.total_files = len(all_items)
        self.completed_bytes = sum(self.sizes[item["path"]] for item in completed_items)
        self.completed_files = len(completed_items)
        self.current_paths = {}
        self.current_uploaded = {}
        self.file_id_to_path = {}
        self.month_key = ""
        self.album_number = 0
        self.album_total = 0
        self.samples = deque()
        self.last_draw = 0.0
        self.lock = threading.Lock()

    def begin_album(self, items, month_key, album_number, album_total):
        with self.lock:
            self.month_key = month_key
            self.album_number = album_number
            self.album_total = album_total
            self.current_paths = {normalize_path(item["path"]): item["path"] for item in items}
            self.current_uploaded = {item["path"]: 0 for item in items}
            self.file_id_to_path = {}
            self.samples.clear()
            self.last_draw = 0.0

    @staticmethod
    def _video_file(message):
        content = message.get("content", {})
        if content.get("@type") != "messageVideo":
            return None
        return content.get("video", {}).get("video")

    def register_messages(self, messages, items):
        with self.lock:
            for message, item in zip(messages, items):
                file_obj = self._video_file(message)
                if not file_obj:
                    continue
                if file_obj.get("id") is not None:
                    self.file_id_to_path[file_obj["id"]] = item["path"]
                self._apply_unlocked(file_obj)

    def _apply_unlocked(self, file_obj):
        path = None
        local_path = file_obj.get("local", {}).get("path", "")
        if local_path:
            path = self.current_paths.get(normalize_path(local_path))
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
                kind="VIDEO",
                ratio=ratio,
                speed=speed,
                eta=eta,
                detail=self.month_key,
                album_number=self.album_number,
                album_total=self.album_total,
                done_files=self.completed_files,
                total_files=self.total_files,
                done_bytes=done,
                total_bytes=self.total_bytes,
            )
        UI.progress(**kwargs)

    def finish_album(self, items):
        with self.lock:
            self.completed_bytes += sum(self.sizes[item["path"]] for item in items)
            self.completed_files += len(items)
            self.current_paths = {}
            self.current_uploaded = {}
            self.file_id_to_path = {}
        self.draw(force=True)
        UI.finish()


def make_groups(items):
    groups = defaultdict(list)
    for item in items:
        groups[item["month_key"]].append(item)
    return groups


def build_album_plans(items, state=None) -> list[dict]:
    """Build stable per-month Album plans from the complete scan."""
    store = CaptionStore("video")
    plans = []
    for month_key in sorted(make_groups(items)):
        month_items = make_groups(items)[month_key]
        default_label = month_caption(month_key)
        for start in range(0, len(month_items), cfg.VIDEO_ALBUM_SIZE):
            album_items = list(month_items[start:start + cfg.VIDEO_ALBUM_SIZE])
            pending = [
                item for item in album_items
                if state is None or not state.is_completed(item["path"])
            ]
            key = album_key("video", month_key, album_items)
            record = store.get(key, default_label)
            base_label = record["base_label"] or default_label
            plans.append({
                "key": key,
                "month_key": month_key,
                "number": start // cfg.VIDEO_ALBUM_SIZE + 1,
                "items": album_items,
                "pending_items": pending,
                "caption": {
                    "base_label": base_label,
                    "custom_text": record["custom_text"],
                    "text": compose_caption(
                        base_label,
                        record["custom_text"],
                        getattr(cfg, "VIDEO_ALBUM_CAPTION_SEPARATOR", " · "),
                    ),
                },
            })
    return plans


def print_plan(items, state):
    groups = make_groups(items)
    print("\n" + "=" * 104)
    print("上传计划：EXIF/QuickTime 按月分组；同月每最多 10 个组成 Album；每个 Album 只保留一个 yy-m Caption")
    print("=" * 104)
    for month_key in sorted(groups):
        month_items = groups[month_key]
        pending = [item for item in month_items if not state.is_completed(item["path"])]
        print(f"\n[{month_key}] Caption={month_caption(month_key)} | 共 {len(month_items)} | 待上传 {len(pending)}")
        for index, item in enumerate(month_items, 1):
            path = item["path"]
            status = "已完成" if state.is_completed(path) else "待上传"
            fallback = " [mtime兜底]" if item["fallback"] else ""
            print(
                f"  {index:>3}. [{status}] {item['capture_time'].strftime('%Y-%m-%d %H:%M:%S')}  "
                f"{format_size(path.stat().st_size):>10}  {relative_name(path)}  <{item['date_tag']}>{fallback}"
            )


def validate_config():
    if cfg.API_ID == 12345678 or cfg.API_HASH == "YOUR_API_HASH":
        raise RuntimeError("请先在 config.toml 中填写 API_ID / API_HASH。")
    if getattr(cfg, "TARGET_MODE", "forum_topic") == "channel":
        if cfg.CHAT_ID in {0, -1001234567890}:
            raise RuntimeError("请先在 config.toml 中填写频道 Chat ID。")
    elif cfg.CHAT_ID in {0, -1001234567890} or cfg.FORUM_TOPIC_ID <= 0 or cfg.FORUM_TOPIC_ID == 12345:
        raise RuntimeError("请先在 config.toml 中填写 CHAT_ID / FORUM_TOPIC_ID。")


def main():
    validate_config()
    version = verify_tdjson_version()
    UI.log(f"tdjson / TDLib 绑定版本：{version}（已锁定）")

    videos = scan_videos()
    if not videos:
        UI.log("没有找到视频文件。")
        return

    UI.log("正在使用 ExifTool 读取视频内嵌日期...")
    metadata_index = read_exif_metadata()
    items, missing = build_items(videos, metadata_index)
    if missing and cfg.VIDEO_MISSING_DATE_POLICY == "error":
        UI.log("以下视频没有找到可用的 EXIF/QuickTime 日期：")
        for path in missing:
            UI.log(f"  {relative_name(path)}")
        UI.log('当前 missing_date_policy="error"，所以没有开始上传。')
        return

    state = UploadState()
    completed_items = [item for item in items if state.is_completed(item["path"])]
    pending_items = [item for item in items if not state.is_completed(item["path"])]
    plans = build_album_plans(items, state)
    pending_plans = [plan for plan in plans if plan["pending_items"]]
    total_albums = len(pending_plans)

    print("\n" + "=" * 82)
    print(f"视频目录：{cfg.VIDEO_DIR}")
    print(f"扫描视频：{len(videos)} | 可用日期：{len(items)} | 缺失日期：{len(missing)}")
    print(f"断点已完成：{len(completed_items)}/{len(items)}")
    print(f"本次待上传：{len(pending_items)} | {format_size(sum(item['path'].stat().st_size for item in pending_items))}")
    print(f"本次 Album：{total_albums}")
    print(f"状态文件：{state.path}")
    print("=" * 82)

    if cfg.VIDEO_SHOW_FILE_LIST:
        print_plan(items, state)
    if not pending_items:
        print("\n全部视频已经在断点记录中，无需上传。")
        return

    if cfg.VIDEO_VERIFY_ALL_METADATA:
        for index, item in enumerate(pending_items, 1):
            path = item["path"]
            print(f"\r预检视频 {index}/{len(pending_items)}：{path.name}", end="", flush=True)
            video_info(path)
        print()

    if input("\n确认开始上传？输入 y 继续：").strip().lower() != "y":
        print("已取消。")
        return

    client = TDJsonClient(UI, "TDLib Video Album Uploader")
    progress = VideoUploadProgress(items, completed_items)
    client.add_update_callback(progress.handle_update)

    try:
        client.login()
        client.set_fast_options()
        client.validate_target()
        album_global = 0

        month_plan_groups = defaultdict(list)
        for plan in pending_plans:
            month_plan_groups[plan["month_key"]].append(plan)
        for month_key in sorted(month_plan_groups):
            month_plans = month_plan_groups[month_key]
            month_items = [item for plan in month_plans for item in plan["pending_items"]]
            month_album_total = len(month_plans)
            UI.log("")
            UI.log("=" * 82)
            UI.log(f"开始月份 {month_key}：{len(month_items)} 个视频，{month_album_total} 个 Album")
            UI.log("=" * 82)

            for plan in month_plans:
                album_items = plan["pending_items"]
                month_album_number = plan["number"]
                label = with_filename_description(
                    plan["caption"]["text"],
                    album_items,
                    getattr(cfg, "VIDEO_CAPTION_INCLUDE_FILENAMES", False),
                )
                album_global += 1
                progress.begin_album(album_items, month_key, album_global, total_albums)
                UI.log("")
                UI.log(
                    f"[总 {album_global}/{total_albums}] [{month_key} {month_album_number}/{month_album_total}] "
                    f"Album {len(album_items)} 个 | Caption={label}"
                )
                for item in album_items:
                    path = item["path"]
                    UI.log(
                        f"  {item['capture_time'].strftime('%Y-%m-%d %H:%M:%S')}  "
                        f"{format_size(path.stat().st_size):>10}  {relative_name(path)}"
                    )
                try:
                    contents = [
                        input_video(item, label if index == 0 else "")
                        for index, item in enumerate(album_items)
                    ]
                    message_ids = client.send_contents(contents, progress, album_items)
                except Exception:
                    UI.finish()
                    UI.log("当前 Album 未写入断点。")
                    raise
                state.mark_album_completed(album_items, message_ids)
                progress.finish_album(album_items)
                UI.log(f"Album 发送完成，Caption={label or '无'}，断点已保存。")

        UI.log("\n" + "=" * 82)
        UI.log("全部视频上传完成。")
        UI.log("=" * 82)
    finally:
        client.remove_update_callback(progress.handle_update)
        client.close()
