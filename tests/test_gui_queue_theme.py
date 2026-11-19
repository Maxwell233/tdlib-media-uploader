import json
from pathlib import Path
import shutil
import threading

import pytest
from PIL import Image
from PySide6.QtWidgets import QDialog, QMessageBox

from conftest import TARGET, task_for
from tdlib_media_uploader.config import loader
from tdlib_media_uploader.config.snapshot import snapshot_config
from tdlib_media_uploader.core import album
from tdlib_media_uploader.core.models import UploadRunResult
from tdlib_media_uploader.core.upload_journal import InflightJournal
from tdlib_media_uploader.gui import main_window, preferences, scanner
from tdlib_media_uploader.gui.integration import run_v2_upload
from tdlib_media_uploader.gui.icons import get_svg_icon
from tdlib_media_uploader.gui.pages import upload
from tdlib_media_uploader.gui.preferences import UiPreferences
from tdlib_media_uploader.gui.theme import DARK_PALETTE, LIGHT_PALETTE, initialize_theme
from tdlib_media_uploader.gui.workers import UploadWorker
from tdlib_media_uploader.upload import reconciliation
from tdlib_media_uploader.upload.queue import TaskStatus
from tdlib_media_uploader.upload.queue_store import UploadQueue

VIDEO = Path(__file__).parent / "fixtures" / "remux_h264_aac.mp4"


def seed(root, kind):
    root.mkdir(parents=True)
    if kind == "mixed":
        folder = root / "album"
        folder.mkdir()
    else:
        folder = root
    if kind != "video":
        Image.new("RGB", (20, 20)).save(folder / "sample.jpg")
    if kind != "image":
        shutil.copyfile(VIDEO, folder / "sample.mp4")
    return root


@pytest.fixture
def window(tmp_path, qt_app, monkeypatch, runtime_paths):
    monkeypatch.setattr(main_window, "_save_history", lambda _records: None)
    monkeypatch.setattr(main_window, "_load_history", list)
    monkeypatch.setattr(album, "CAPTIONS_DIR", tmp_path / "captions")
    monkeypatch.setattr(scanner, "runtime_paths", runtime_paths)
    monkeypatch.setattr(main_window, "_target_for", lambda _kind: TARGET.copy())
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    config = snapshot_config(loader)
    config.VIDEO_READ_DATES = False
    config.VIDEO_GENERATE_THUMBNAIL = False
    config.MIXED_GENERATE_THUMBNAIL = False
    config.STAGING_ENABLED = False
    config.STAGING_MODE = "off"
    config.PREFETCH_NEXT_ALBUM = False
    config.VIDEO_PREFETCH_NEXT_ALBUM = False
    config.IMAGE_ALBUM_SIZE = 2
    monkeypatch.setattr(main_window.config_service, "cfg", config)
    ui_prefs = UiPreferences(tmp_path / "ui_preferences.json")
    initialize_theme(qt_app, ui_prefs)
    store = UploadQueue(tmp_path / "upload_queue.json", runtime_paths=runtime_paths)
    window = main_window.MainWindow(queue_store=store, preferences=ui_prefs)
    window.queue.can_start = lambda: True
    window.show()
    yield window, config, ui_prefs
    window.close()


@pytest.mark.parametrize(("upload_kind", "scan_kind"), [
    ("video", "image"), ("video", "video"), ("image", "mixed")
])
def test_scan_edit_enqueue_while_active_upload_keeps_media_and_theme_isolated(
    window, tmp_path, pump, monkeypatch, runtime_paths, upload_kind, scan_kind,
):
    win, config, prefs = window
    first = seed(tmp_path / "upload-a", upload_kind)
    second = seed(tmp_path / "scan-b", scan_kind)
    third = seed(tmp_path / "scan-c", scan_kind)
    roots = {upload_kind: first}
    target_a = TARGET.copy()
    target_b = {**TARGET, "chat_id": -100678}
    targets = {upload_kind: target_a}
    monkeypatch.setattr(main_window, "_source_root_for", roots.__getitem__)
    monkeypatch.setattr(main_window, "_target_for", targets.__getitem__)
    started, release_a = threading.Event(), threading.Event()
    upload_order, sent, closed = [], [], []
    old_uis = []

    def make_worker(task):
        class Client:
            def __init__(self, ui, device_model, *, config):
                self.ui = ui
                self.config = config
                upload_order.append(task.source_root)
                if task.source_root == first:
                    old_uis.append(ui)

            def send_contents(self, contents, *, target, on_submitted, **kwargs):
                assert all(old != task.source_root for old in closed)
                source = task.source_root
                if source == first:
                    started.set()
                    assert release_a.wait(10)
                assert self.config.IMAGE_ALBUM_SIZE == (2 if source == first else 1)
                sent.append((source, target["chat_id"], contents[0]["caption"]["text"]))
                ids = list(range(100, 100 + len(contents)))
                on_submitted(ids)
                return ids

            def close(self):
                closed.append(task.source_root)

        def run(kind, **kwargs):
            kwargs["runtime_paths"] = runtime_paths
            return run_v2_upload(kind, **kwargs, client_factory=Client)
        worker = UploadWorker(task.kind, main_window.AuthBridge(), task.preview(),
                              target_provider=lambda _kind: task.target,
                              config=task.execution_config(config), upload_runner=run)
        worker.wait_for_scheduler = True
        return worker

    win.queue.make_worker = make_worker
    try:
        assert win._scan(upload_kind)
        pump(lambda: not win.scanners)
        a_page = win._upload_page(upload_kind)
        assert a_page.result["pending_files"] > 0
        win._start_upload(upload_kind)
        pump(started.is_set)
        assert win.worker is not None

        win.nav_sidebar._toggle_theme()
        assert prefs.theme_mode == "light"
        config.IMAGE_ALBUM_SIZE = 1
        roots[scan_kind] = second
        targets[scan_kind] = target_b
        assert win._scan(scan_kind)
        win.upload_hub.set_current_kind("mixed" if scan_kind != "mixed" else "video")
        pump(lambda: not win.scanners)
        b_page = win._upload_page(scan_kind)
        result = b_page.result
        assert result["source_dir"] == str(second)
        assert b_page.start_button.isEnabled() and b_page.scan_button.isEnabled()
        assert not release_a.is_set()
        class CaptionDialog:
            base_text = "edited while A sends"
            custom_text = ""

            def __init__(self, **kwargs):
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

        monkeypatch.setattr(upload, "CaptionEditDialog", CaptionDialog)
        tree_item = b_page.tree.topLevelItem(0)
        if scan_kind != "image":
            tree_item = tree_item.child(0)
        b_page.tree.setCurrentItem(tree_item)
        assert b_page.edit_caption_button.isEnabled()
        b_page.edit_caption_button.click()
        win._start_upload(scan_kind)
        assert win.queue.store.tasks[1].plans[0].caption == "edited while A sends"

        def save_draft(values):
            roots[scan_kind] = Path(next(iter(values.values())))
            setattr(config, f"{scan_kind.upper()}_DIR", roots[scan_kind])
            return ""

        monkeypatch.setattr(main_window, "_write_config_values", save_draft)
        b_page.path_selected.emit(scan_kind, str(third))
        assert b_page.result is None
        assert getattr(win.settings_page.general_panel, f"{scan_kind}_dir").text() == str(third)
        assert win._scan(scan_kind)
        pump(lambda: not win.scanners)
        latest = b_page.result
        win._start_upload(scan_kind)
        release_a.set()
        pump(lambda: win.worker is None and all(job.status == TaskStatus.COMPLETED for job in win.queue.store.tasks))
        assert upload_order == closed == [first, second, third]
        assert b_page.result is latest
        assert sent[0][:2] == (first, target_a["chat_id"])
        assert (second, target_b["chat_id"], "edited while A sends") in sent
        old_uis[0].progress(done_files=999)
        pump(lambda: True)
        assert "999" not in win.task_page.metrics.text()
    finally:
        release_a.set()
        if win.worker is not None:
            win.queue.closing = True
            win.worker.request_immediate_stop()
            pump(lambda: win.worker is None)


def test_scan_cancel_navigation_and_theme_during_scan(window, monkeypatch, pump, qt_app, tmp_path):
    win, _config, prefs = window
    monkeypatch.setattr(main_window, "_source_root_for", lambda kind: tmp_path)
    started = threading.Event()

    def run(kind, *, cancel_event, **kwargs):
        started.set()
        assert cancel_event.wait(5)
        return {"cancelled": True}

    monkeypatch.setattr(main_window, "_scan_result", run)
    assert win._scan("image")
    pump(started.is_set)
    win.upload_hub.set_current_kind("mixed")
    assert not win._scan("mixed")
    win.nav_sidebar._toggle_theme()
    assert LIGHT_PALETTE.bg_base in qt_app.styleSheet()
    assert prefs.theme_mode == "light"
    win._stop_scan("image")
    pump(lambda: not win.scanners)
    assert win._upload_page("image").result["cancelled"]
    assert not win._upload_page("image").start_button.isEnabled()
    assert win.upload_hub.current_kind() == "mixed"


def test_theme_startup_qss_icons_and_independent_files(window, qt_app, tmp_path):
    win, _config, prefs = window
    config_path = tmp_path / "config.toml"
    config_path.write_text("[other]\nexample = true\n", encoding="utf-8")
    queue = win.queue.store
    queue.set_paused(True)
    queue.enqueue(task_for(tmp_path / "other"))
    before = (queue.path.read_bytes(), config_path.read_bytes())
    button = win.nav_sidebar.theme_btn
    assert DARK_PALETTE.bg_base in qt_app.styleSheet()
    assert button.icon().pixmap(16, 16).toImage() == get_svg_icon("sun", 16, 16).pixmap(16, 16).toImage()
    button.click()
    assert json.loads(prefs.path.read_text(encoding="utf-8")) == {"schema_version": 1, "theme_mode": "light"}
    reloaded = UiPreferences(prefs.path)
    initialize_theme(qt_app, reloaded)
    restarted = main_window.MainWindow(queue_store=queue, preferences=reloaded)
    restarted.show()
    qt_app.processEvents()
    assert LIGHT_PALETTE.bg_base in qt_app.styleSheet()
    assert restarted.nav_sidebar.theme_btn.property("themeMode") == "light"
    assert "当前浅色" in restarted.nav_sidebar.theme_btn.toolTip()
    assert restarted.nav_sidebar.theme_btn.icon().pixmap(16, 16).toImage() == get_svg_icon("moon", 16, 16).pixmap(16, 16).toImage()
    assert (queue.path.read_bytes(), config_path.read_bytes()) == before
    restarted.nav_sidebar.theme_btn.click()
    assert UiPreferences(prefs.path).theme_mode == "dark"
    assert DARK_PALETTE.bg_base in qt_app.styleSheet()
    restarted.close()


@pytest.mark.parametrize("value", ["{}\n", '{"schema_version": 1, "theme_mode": "sepia"}', "{broken", None])
def test_old_unknown_and_corrupt_preferences(value, qt_app, tmp_path):
    path = tmp_path / "ui_preferences.json"
    if value is not None:
        path.write_text(value, encoding="utf-8")
    prefs = UiPreferences(path)
    initialize_theme(qt_app, prefs)
    assert DARK_PALETTE.bg_base in qt_app.styleSheet()
    assert bool(prefs.error) == (value == "{broken")
    if prefs.error:
        with pytest.raises(ValueError):
            prefs.save_theme("light")
        assert path.read_text(encoding="utf-8") == value


def test_theme_write_error_is_visible(window, monkeypatch, qt_app):
    win, _config, prefs = window

    def denied(*args):
        raise PermissionError("read only")

    monkeypatch.setattr(preferences, "atomic_write_json", denied)
    win.nav_sidebar.theme_btn.click()
    assert LIGHT_PALETTE.bg_base in qt_app.styleSheet()
    assert "保存失败" in win.statusBar().currentMessage()
    assert not prefs.path.exists()


def test_theme_switch_leaves_unsaved_retry_settings_and_skips_config_refresh(window, monkeypatch):
    win, _, prefs = window
    panel = win.settings_page.advanced_panel
    assert not panel.album_retry_enabled.isChecked()
    panel.album_retry_enabled.setChecked(True)
    panel.album_retry_count.setValue(4)
    panel.album_retry_delay.setValue(0.5)

    def must_not_refresh():
        pytest.fail("theme switch reloaded upload settings")

    monkeypatch.setattr(win.settings_page, "refresh", must_not_refresh)
    win.nav_sidebar.theme_btn.click()
    assert prefs.theme_mode == "light" and panel.is_dirty()
    values = panel.collect_values()
    assert values[("upload_retry", "enabled")] is True
    assert values[("upload_retry", "max_retries")] == 4
    assert values[("upload_retry", "retry_delay_seconds")] == 0.5


def test_review_controls_follow_manual_reconciliation_and_skip_history(window, monkeypatch, tmp_path, runtime_paths):
    win, _, _ = window
    store = win.queue.store
    task = task_for(tmp_path / "a")
    store.enqueue(task)
    reviewed = store.finish(store.start(store.prepare(task)), UploadRunResult("UNKNOWN"))
    store.enqueue(task_for(tmp_path / "b"))
    target, plan = dict(task.target), task.plans[0]
    journal = InflightJournal(runtime_paths.UPLOAD_INFLIGHT_DIR)
    record = journal.prepare(task.kind, plan.key, target=target)
    service = reconciliation.ReconciliationService(journal)
    monkeypatch.setattr(reconciliation, "ReconciliationService", lambda: service)
    history = []
    monkeypatch.setattr(main_window, "_load_history", lambda: history)
    monkeypatch.setattr(main_window, "_save_history", lambda _records: None)
    win._refresh_queue()
    page = win.task_page
    page.queue_list.setCurrentItem(page.queue_list.topLevelItem(0))
    assert page.queue_skip.isHidden() and page.queue_restore.isHidden()
    page.queue_toggle.click()
    assert store.paused and "待核对" in page.queue_notice.text()
    page.queue_inflight.click()
    assert win.sidebar.currentRow() == win.sidebar_rows["inflight"]

    win.inflight_page.reconciliation_requested.emit(record, False)
    assert store.tasks[0].status is TaskStatus.INTERRUPTED
    assert store.paused and win.worker is None
    assert not page.queue_skip.isHidden() and not page.queue_restore.isHidden()
    with monkeypatch.context() as context:
        context.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
        page.queue_skip.click()
        assert store.tasks[0].status is TaskStatus.INTERRUPTED
    page.queue_skip.click()
    assert store.paused and store.tasks[0].status is TaskStatus.SKIPPED
    assert history[-1]["task_id"] == reviewed.task_id
    assert history[-1]["status"] == "SKIPPED" and history[-1]["success"] is False


def test_cache_clear_cannot_discard_protection_for_existing_queue_tasks(window, tmp_path, monkeypatch):
    win, _, _ = window
    store = win.queue.store
    task = task_for(tmp_path / "a")
    store.enqueue(task)
    store.finish(store.start(store.prepare(task)), UploadRunResult("UNKNOWN"))
    monkeypatch.setattr(win, "_cache_operation_allowed", lambda: True)
    monkeypatch.setattr(win, "_finish_cache_clear", lambda *args, **kwargs: pytest.fail("cleared protection"))
    win._clear_all_cache()
    assert "未完成" in win.statusBar().currentMessage()
