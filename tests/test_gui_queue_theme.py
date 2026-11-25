from dataclasses import replace
import json
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace

import pytest
from PIL import Image
from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QMessageBox, QSplitter

from conftest import TARGET, task_for
from tdlib_media_uploader.config import loader, paths
from tdlib_media_uploader.config.snapshot import snapshot_config
from tdlib_media_uploader.core import album, logging as app_logging, upload_journal
from tdlib_media_uploader.core.models import AlbumSettledEvent, BatchStatus, UploadBatchResult, UploadRunResult
from tdlib_media_uploader.core.upload_journal import InflightJournal
from tdlib_media_uploader.gui import main_window, preferences, scanner
from tdlib_media_uploader.gui.integration import run_v2_upload
from tdlib_media_uploader.gui.icons import get_svg_icon
from tdlib_media_uploader.gui.pages import inflight, upload
from tdlib_media_uploader.gui.pages.task import TaskPage
from tdlib_media_uploader.gui.preferences import UiPreferences
from tdlib_media_uploader.gui.theme import DARK_PALETTE, LIGHT_PALETTE, build_stylesheet, initialize_theme
from tdlib_media_uploader.gui.workers import UploadWorker
from tdlib_media_uploader.upload import queue_recovery, queue_store, reconciliation
from tdlib_media_uploader.upload.queue import AlbumOutcome, AlbumStatus, TaskStatus
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
    logs_dir = tmp_path / "logs"
    app_data_dir = tmp_path / "app_data"
    monkeypatch.setattr(paths, "DATA_DIR", app_data_dir)
    monkeypatch.setattr(paths, "LOG_DIR", logs_dir)
    monkeypatch.setattr(paths, "UPLOAD_INFLIGHT_DIR", runtime_paths.UPLOAD_INFLIGHT_DIR)
    monkeypatch.setattr(app_logging, "LOG_DIR", logs_dir)
    monkeypatch.setattr(app_logging, "APP_LOG_PATH", logs_dir / "app.log")
    monkeypatch.setattr(upload_journal, "APP_DATA_DIR", app_data_dir)
    monkeypatch.setattr(upload_journal, "UPLOAD_INFLIGHT_DIR", runtime_paths.UPLOAD_INFLIGHT_DIR)
    monkeypatch.setattr(main_window, "_save_history", lambda _records: None)
    monkeypatch.setattr(main_window, "_load_history", list)
    monkeypatch.setattr(album, "CAPTIONS_DIR", tmp_path / "captions")
    monkeypatch.setattr(scanner, "runtime_paths", runtime_paths)
    monkeypatch.setattr(main_window, "_target_for", lambda _kind: TARGET.copy())
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)

    def _auto_confirm_box(box):
        button = box.defaultButton()
        if button is not None:
            button.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", _auto_confirm_box)
    config = snapshot_config(loader)
    config.VIDEO_READ_DATES = False
    config.VIDEO_GENERATE_THUMBNAIL = False
    config.MIXED_GENERATE_THUMBNAIL = False
    config.STAGING_ENABLED = False
    config.STAGING_MODE = "off"
    config.PREFETCH_NEXT_ALBUM = False
    config.VIDEO_PREFETCH_NEXT_ALBUM = False
    config.IMAGE_ALBUM_SIZE = 2
    # These fixtures are fully written before scanning. Keep the snapshot
    # checks without waiting between observations of a stable local file.
    config.SCAN_STABILITY_INTERVAL_LOCAL_SECONDS = 0.0
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
        tree_model = b_page.tree.model()
        tree_item = tree_model.index(0, 0)
        if scan_kind != "image":
            tree_item = tree_model.index(0, 0, tree_item)
        b_page.tree.setCurrentIndex(tree_item)
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
        assert cancel_event.wait(30)
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


def test_task_center_fits_long_details_and_keeps_live_status_at_narrow_width(qt_app, tmp_path):
    source = tmp_path / ("long-shared-source-" * 2)
    source.mkdir()
    media_path = source / ("long-file-name-" * 5 + ".jpg")
    media_path.write_bytes(b"snapshot")
    active = replace(task_for(tmp_path / "active"), status=TaskStatus.RUNNING)
    reviewed = task_for(source, paths=[media_path])
    reason = "源文件不可访问\n" + str(media_path) + "\n请检查共享目录是否可读。"
    reviewed = replace(
        reviewed, status=TaskStatus.NEEDS_REVIEW,
        album_outcomes=(AlbumOutcome(reviewed.plans[0].key, AlbumStatus.UNKNOWN, error=reason),),
    )
    store = SimpleNamespace(tasks=(active, reviewed), paused=False, error=None, blocked_reason="任务待核对。")
    page = TaskPage()
    page.setStyleSheet(build_stylesheet(LIGHT_PALETTE))
    page.set_target_label_provider(lambda _: {"chat_title": "很长的频道名称" * 12})
    page.show_queue(store)
    page.resize(630, 530)
    page.show()
    qt_app.processEvents()
    assert page.progress.isVisible() and page.progress.value() == 0

    page.start_session("image", {"task_name": "正在上传的目录", "pending_files": 10, "pending_bytes": 100, "album_count": 2})
    page.show_album({"title": "组内 Album 1 · 1/2", "rows": [media_path.name]})
    page.show_progress({"ratio": 0.42, "done_files": 4, "total_files": 10})
    page.show_rate_limit(35)
    page.set_connection_status("等待网络恢复", False)
    page.add_message("warning", reason)
    qt_app.processEvents()

    assert page.progress.isVisible() and page.progress.value() == 420
    assert page.width() == 630
    assert not page.findChildren(QSplitter)
    queue_origin = page.queue_list.mapTo(page, QPoint())
    details_origin = page.detail_tabs.mapTo(page, QPoint())
    assert queue_origin.x() + page.queue_list.width() < details_origin.x()
    assert page.queue_list.isColumnHidden(4)
    assert page.albums.isColumnHidden(2)
    assert page.queue_inflight.isVisible()
    assert "35 秒" in page.transfer_notice.text() and "等待网络恢复" in page.transfer_notice.text()
    assert page.queue_list.topLevelItem(0).text(2) == "42%"
    page.select_task(reviewed.task_id)
    assert "正在上传的目录" in page.active_task_label.text()
    for tree in (page.queue_list, page.albums):
        assert tree.header().length() <= tree.viewport().width()
        assert not tree.horizontalScrollBar().isVisible()

    assert page.albums.isVisible() and not page.album_files.isVisible()
    album = page.albums.topLevelItem(0)
    QTest.mouseClick(page.albums.viewport(), Qt.MouseButton.LeftButton, pos=page.albums.visualItemRect(album).center())
    qt_app.processEvents()
    assert page.album_reason.isVisible()
    assert page.album_reason.toPlainText() == reason
    assert page.album_files.item(0).toolTip() == str(media_path)
    assert page.album_reason.horizontalScrollBar().maximum() == 0
    assert not page.album_files.horizontalScrollBar().isVisible()

    page.detail_tabs.setCurrentIndex(1)
    qt_app.processEvents()
    assert reason in page.log.toPlainText()
    assert page.log.horizontalScrollBar().maximum() == 0

    page.resize(1040, 700)
    page.detail_tabs.setCurrentIndex(0)
    qt_app.processEvents()
    assert not page.queue_list.isColumnHidden(4)
    assert not page.albums.isColumnHidden(2)
    page.finish_session(False, "已停止")
    assert page.progress.isVisible() and page.progress.value() == 420
    page.start_session("image", {"pending_files": 1, "pending_bytes": 100, "album_count": 1})
    assert page.progress.isVisible() and page.progress.value() == 0
    page.finish_session(True, "已完成")
    assert page.progress.isVisible() and page.progress.value() == page.progress.maximum()


def test_task_center_finishes_first_album_while_second_is_sending(window, tmp_path, pump, monkeypatch, runtime_paths):
    win, config, _ = window
    source = seed(tmp_path / "photos", "image")
    Image.new("RGB", (20, 20)).save(source / "next.jpg")
    config.IMAGE_ALBUM_SIZE = 1
    monkeypatch.setattr(main_window, "_source_root_for", lambda _kind: source)
    second_started = threading.Event()
    finish_second = threading.Event()

    class Client:
        def __init__(self, _ui, _device_model, *, config):
            self.sends = 0

        def send_contents(self, contents, *, on_submitted, **kwargs):
            self.sends += 1
            ids = [100 + self.sends]
            assert len(contents) == 1
            on_submitted(ids)
            if self.sends == 2:
                second_started.set()
                assert finish_second.wait(10)
            return ids

        def close(self):
            pass

    def run(kind, **kwargs):
        return run_v2_upload(kind, **{**kwargs, "runtime_paths": runtime_paths}, client_factory=Client)

    def make_worker(task):
        worker = UploadWorker(task.kind, main_window.AuthBridge(), task.preview(), target_provider=lambda _: task.target,
                              config=task.execution_config(config), upload_runner=run)
        worker.wait_for_scheduler = True
        return worker

    win.queue.make_worker = make_worker
    try:
        assert win._scan("image")
        pump(lambda: not win.scanners)
        win._start_upload("image")
        pump(second_started.is_set)
        page = win.task_page
        assert win.worker is not None and win.queue.store.tasks[0].status is TaskStatus.RUNNING
        first_row, second_row = (page.albums.topLevelItem(index) for index in range(2))
        assert (first_row.text(1), first_row.text(3)) == ("已完成", "1/1")
        assert second_row.text(1) == "上传中"
        page.albums.setCurrentItem(first_row)
        assert "已完成" in page.album_files.item(0).text()
        win._refresh_queue()
        assert page.albums.topLevelItem(0).text(1) == "已完成"
    finally:
        finish_second.set()
        pump(lambda: win.worker is None)


@pytest.mark.parametrize(("error", "label", "count"), [(None, "部分完成", "1/2"), ("断点写入失败", "待核对", "0/2")])
def test_album_settlement_preserves_partial_and_unknown_status(qt_app, tmp_path, error, label, count):
    files = [tmp_path / name for name in ("first.jpg", "second.jpg")]
    for path in files:
        path.write_bytes(b"image snapshot")
    task = replace(task_for(tmp_path, paths=files), status=TaskStatus.RUNNING)
    plan = task.plans[0]
    page = TaskPage()
    page.show_queue(SimpleNamespace(tasks=(task,), paused=False, error=None, blocked_reason=""))
    row = page.albums.topLevelItem(0)
    page.albums.setCurrentItem(row)
    page.show_album({"album_key": plan.key, "rows": ["live file"]})
    page.show_album_settled(AlbumSettledEvent(
        UploadBatchResult(f"{plan.key}:retry:example", BatchStatus.CONFIRMED, confirmed_items=plan.items[:1], error=error),
        deferred_items=plan.items[1:],
    ))
    page.show_progress({"album_key": plan.key, "album_ratio": 1.0})
    assert (row.text(1), row.text(3)) == (label, count)
    assert page.album_files.count() == 2
    expected = ("待核对", "待核对") if error else ("已完成", "暂缓")
    assert tuple(page.album_files.item(i).data(Qt.ItemDataRole.UserRole) for i in range(2)) == expected


def test_review_controls_follow_manual_reconciliation_and_skip_history(window, monkeypatch, tmp_path, runtime_paths, pump):
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
    pump(lambda: not win.queue.recovery_busy)
    assert store.tasks[0].status is TaskStatus.INTERRUPTED
    assert store.paused and win.worker is None
    assert not page.queue_skip.isHidden() and not page.queue_restore.isHidden()
    with monkeypatch.context() as context:
        context.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
        page.queue_skip.click()
        assert store.tasks[0].status is TaskStatus.INTERRUPTED
    page.queue_skip.click()
    pump(lambda: not win.queue.recovery_busy)
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



@pytest.mark.parametrize("stage", ["validation", "commit"])
def test_window_closes_only_after_recovery_can_end_safely(window, tmp_path, pump, monkeypatch, stage):
    win, _, _ = window
    store = win.queue.store
    task = task_for(tmp_path / "failed")
    store.enqueue(task)
    failed = store.finish(store.start(store.prepare(task)), UploadRunResult("FAILED"))
    win._refresh_queue()
    old_queue = store.path.read_bytes()
    entered, release = threading.Event(), threading.Event()

    if stage == "validation":
        module, method = queue_recovery, "validate_snapshots"
    else:
        module, method = queue_store, "atomic_write_json"
    original = getattr(module, method)

    def pause(*args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("test did not release the recovery thread")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, method, pause)
    try:
        win._restore_queued_task(failed.task_id)
        pump(entered.is_set)
        assert not win.close()
        assert win.isVisible() and win.queue.recovery_busy and win.queue.closing
        assert store.tasks == (failed,) and store.path.read_bytes() == old_queue
        switched = []

        def open_task_centre():
            win.sidebar.setCurrentRow(win.sidebar_rows["task"])
            switched.append(win.task_page.isVisible())

        QTimer.singleShot(0, open_task_centre)
        pump(lambda: bool(switched))
        assert switched == [True]
        assert not win.task_page.queue_cancel.isEnabled()
    finally:
        release.set()
        pump(lambda: not win.queue.recovery_busy)
    pump(lambda: not win.isVisible())
    reopened = UploadQueue(store.path, runtime_paths=store.runtime_paths)
    assert reopened.tasks == store.tasks and reopened.paused and win.queue.worker is None
    if stage == "validation":
        assert store.tasks == (failed,) and store.path.read_bytes() == old_queue
    else:
        assert store.tasks[0].status is TaskStatus.PENDING
        assert store.tasks[0].task_id == failed.task_id
