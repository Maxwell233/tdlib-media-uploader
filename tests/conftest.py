import os
from pathlib import Path
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import PySide6
_plugins_dir = Path(PySide6.__file__).resolve().parent / "Qt" / "plugins"
if _plugins_dir.is_dir() and not os.environ.get("QT_PLUGIN_PATH"):
    os.environ["QT_PLUGIN_PATH"] = str(_plugins_dir)

import pytest
from PySide6.QtWidgets import QApplication

from tdlib_media_uploader.config import paths
from tdlib_media_uploader.core import album, logging as app_logging, upload_journal
from tdlib_media_uploader.core.models import AlbumPlan, FileSnapshot, MediaItem
from tdlib_media_uploader.gui import history_service, preferences
from tdlib_media_uploader.upload.queue import UploadTask

TARGET = {"target_mode": "channel", "chat_id": -100123}


@pytest.fixture(autouse=True)
def _isolate_test_data_dirs(request, tmp_path, monkeypatch):
    """Prevent tests from writing logs or SQLite databases into repo ./data."""
    if getattr(request.module, "__name__", "") == "test_data_layout":
        return
    base = tmp_path / "_isolated_app_data"
    logs_dir = base / "logs"
    inflight_dir = base / "upload_inflight"
    state_dir = base / "state"
    cache_dir = base / "cache"
    telegram_dir = base / "telegram"
    captions_dir = base / "captions"
    monkeypatch.setattr(paths, "DATA_DIR", base)
    monkeypatch.setattr(paths, "APP_DATA_DIR", base)
    monkeypatch.setattr(paths, "LOG_DIR", logs_dir)
    monkeypatch.setattr(paths, "UPLOAD_INFLIGHT_DIR", inflight_dir)
    monkeypatch.setattr(paths, "STATE_DIR", state_dir)
    monkeypatch.setattr(paths, "VIDEO_STATE_DIR", state_dir / "video")
    monkeypatch.setattr(paths, "IMAGE_STATE_DIR", state_dir / "image")
    monkeypatch.setattr(paths, "MIXED_STATE_DIR", state_dir / "mixed")
    monkeypatch.setattr(paths, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(paths, "THUMBNAIL_CACHE_DIR", cache_dir / "thumbnails")
    monkeypatch.setattr(paths, "IMAGE_COMPRESSION_CACHE_DIR", cache_dir / "image_compression")
    monkeypatch.setattr(paths, "VIDEO_PROCESSED_CACHE_DIR", cache_dir / "video_processed")
    monkeypatch.setattr(paths, "STAGING_CACHE_DIR", cache_dir / "staging")
    monkeypatch.setattr(paths, "TELEGRAM_DIR", telegram_dir)
    monkeypatch.setattr(paths, "TDLIB_DATABASE_DIR", telegram_dir / "database")
    monkeypatch.setattr(paths, "TDLIB_FILES_DIR", telegram_dir / "files")
    monkeypatch.setattr(paths, "CAPTIONS_DIR", captions_dir)
    monkeypatch.setattr(paths, "HISTORY_PATH", base / "history.json")
    monkeypatch.setattr(paths, "CONFIG_PATH", base / "config.toml")
    monkeypatch.setattr(app_logging, "APP_DATA_DIR", base)
    monkeypatch.setattr(app_logging, "LOG_DIR", logs_dir)
    monkeypatch.setattr(app_logging, "APP_LOG_PATH", logs_dir / "app.log")
    monkeypatch.setattr(app_logging, "TDLIB_LOG_PATH", logs_dir / "tdlib.log")
    monkeypatch.setattr(upload_journal, "APP_DATA_DIR", base)
    monkeypatch.setattr(upload_journal, "UPLOAD_INFLIGHT_DIR", inflight_dir)
    monkeypatch.setattr(album, "CAPTIONS_DIR", captions_dir)
    monkeypatch.setattr(history_service, "HISTORY_PATH", base / "history.json")
    monkeypatch.setattr(preferences, "PREFERENCES_PATH", base / "ui_preferences.json", raising=False)


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def pump(qt_app):
    def until(condition, timeout=5):
        deadline = time.monotonic() + timeout
        while not condition():
            qt_app.processEvents()
            if time.monotonic() > deadline:
                pytest.fail("Qt callback did not arrive")
            time.sleep(0.005)
        qt_app.processEvents()
    return until


@pytest.fixture
def runtime_paths(tmp_path):
    return SimpleNamespace(
        IMAGE_STATE_DIR=tmp_path / "state" / "image",
        VIDEO_STATE_DIR=tmp_path / "state" / "video",
        MIXED_STATE_DIR=tmp_path / "state" / "mixed",
        UPLOAD_INFLIGHT_DIR=tmp_path / "inflight",
    )


def media(path, root, kind="image"):
    stat = path.stat()
    return MediaItem(path, root, kind, FileSnapshot(str(path), stat.st_size, stat.st_mtime_ns))


def task_for(root, *, kind="image", target=None, paths=None, config=None, caption="title"):
    target = target or TARGET
    root.mkdir(exist_ok=True, parents=True)
    if paths is None:
        path = root / ("test.mp4" if kind == "video" else "test.jpg")
        path.write_bytes(b"snapshot")
        paths = [path]
    items = tuple(media(path, root, "image" if kind == "mixed" else kind) for path in paths)
    plan = AlbumPlan("stable-key", kind, root, "group", 1, items, items, "scan caption", target)
    preview = {
        "source_dir": str(root), "target": target, "plans": (plan,), "groups": (),
        "caption_overrides": {plan.key: caption},
    }
    config = config or SimpleNamespace(IMAGE_ALBUM_SIZE=2, API_HASH="private")
    return UploadTask.from_preview(kind, preview, config)
