import os
import time
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from tdlib_media_uploader.core.models import AlbumPlan, FileSnapshot, MediaItem
from tdlib_media_uploader.upload.queue import UploadTask

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TARGET = {"target_mode": "channel", "chat_id": -100123}


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
