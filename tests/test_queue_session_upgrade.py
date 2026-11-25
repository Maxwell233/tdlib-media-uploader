import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from conftest import task_for
from test_gui_queue_theme import seed, window
from test_queue_recovery import end_first, payload
from PySide6.QtCore import QPersistentModelIndex
from tdlib_media_uploader.core.models import BatchStatus, UploadBatchResult, UploadRunResult
from tdlib_media_uploader.core.upload_journal import InflightJournal
from tdlib_media_uploader.gui import main_window, scanner
from tdlib_media_uploader.gui.events import AuthBridge
from tdlib_media_uploader.gui.integration import GuiCancelToken
from tdlib_media_uploader.gui.workers import UploadWorker
from tdlib_media_uploader.media import image_pipeline
from tdlib_media_uploader.telegram.session import TDLibSessionManager
from tdlib_media_uploader.upload import reconciliation
from tdlib_media_uploader.upload.queue import AlbumStatus, TaskStatus, UploadTask, finish_task
from tdlib_media_uploader.upload.queue_store import UploadQueue


class FakeClient:
    def __init__(self, ui, device_model, *, config):
        self.ui = ui
        self.config = config
        self.closed_event = threading.Event()
        self.close_sent = False
        self.authorization_ready = False
        self.connection_state = "connectionStateReady"
        self.callbacks = []
        self.login_count = 0
        self.refresh_count = 0

    def bind_job(self, ui, config):
        self.ui, self.config = ui, config

    def add_update_callback(self, callback):
        self.callbacks.append(callback)

    def login(self):
        self.login_count += 1
        self.authorization_ready = True

    def refresh_account_limits(self):
        self.refresh_count += 1

    def set_fast_options(self):
        pass

    def connect_state(self, state):
        self.connection_state = state
        for callback in self.callbacks:
            callback({"@type": "updateConnectionState", "state": {"@type": state}})

    def authorize_state(self, state):
        self.authorization_ready = state == "authorizationStateReady"
        if state == "authorizationStateClosed":
            self.closed_event.set()
        for callback in self.callbacks:
            callback({"@type": "updateAuthorizationState", "authorization_state": {"@type": state}})

    def close(self, *, timeout):
        self.close_sent = True
        self.closed_event.set()


class QueueSessionUpgradeTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.paths = SimpleNamespace(
            IMAGE_STATE_DIR=self.root / "state",
            UPLOAD_INFLIGHT_DIR=self.root / "inflight",
        )

    def test_session_reuses_one_login_across_all_modes_and_rebuilds_for_proxy(self):
        states = []
        manager = TDLibSessionManager(states.append)
        clients = []
        for chat_id, kind in enumerate(("video", "image", "mixed"), 1):
            config = SimpleNamespace(CHAT_ID=chat_id, API_ID=10)
            ui = object()
            with manager.acquire(ui, kind, config=config, factory=FakeClient) as client:
                self.assertIs(client.ui, ui)
                self.assertEqual(client.config.CHAT_ID, chat_id)
                clients.append(client)
        self.assertIs(clients[0], clients[1])
        self.assertIs(clients[1], clients[2])
        self.assertEqual(clients[0].login_count, 1)
        self.assertEqual(clients[0].refresh_count, 3)
        self.assertIn("connectionStateReady", states)
        with manager.acquire(
            object(), "video", config=SimpleNamespace(API_ID=10, PROXY_ENABLED=True), factory=FakeClient
        ) as rebuilt:
            self.assertIsNot(rebuilt, clients[0])
            self.assertTrue(clients[0].closed_event.is_set())
        manager.disconnect()
        self.assertTrue(rebuilt.closed_event.is_set())
        self.assertEqual(states[-1], "disconnected")

    def test_disconnecting_is_visible_until_native_close(self):
        class DelayedClient(FakeClient):
            def close(self, *, timeout):
                self.close_sent = True
                raise TimeoutError("still closing")

        states = []
        manager = TDLibSessionManager(states.append)
        with manager.acquire(object(), "image", config=SimpleNamespace(), factory=DelayedClient) as client:
            pass
        with self.assertRaises(TimeoutError):
            manager.disconnect()
        self.assertEqual(manager.connection_state, "closing")
        self.assertTrue(manager.owns(client))
        client.connect_state("connectionStateReady")
        self.assertEqual(states[-1], "closing")
        client.authorize_state("authorizationStateClosed")
        self.assertFalse(manager.owns(client))
        self.assertEqual(states[-1], "disconnected")

    def test_revoked_idle_session_cannot_look_ready_and_logs_in_again(self):
        manager = TDLibSessionManager()
        config = SimpleNamespace(TDLIB_LOG_VERBOSITY=1)
        with manager.acquire(object(), "image", config=config, factory=FakeClient) as first:
            pass
        first.authorize_state("authorizationStateLoggingOut")
        first.authorize_state("authorizationStateWaitCode")
        first.connect_state("connectionStateReady")
        self.assertEqual(manager.connection_state, "authorization_required")
        with self.assertRaisesRegex(RuntimeError, "重新登录"):
            manager.wait_until_ready(GuiCancelToken())
        first.ui.log("idle log")
        first.ui.info("idle info")
        first.ui.error("idle error")
        with manager.acquire(object(), "image", config=config, factory=FakeClient) as second:
            self.assertIsNot(first, second)
            self.assertTrue(first.closed_event.is_set())
            self.assertEqual(second.login_count, 1)
        config.TDLIB_LOG_VERBOSITY = 2
        with manager.acquire(object(), "image", config=config, factory=FakeClient) as third:
            self.assertIsNot(second, third)
            self.assertTrue(second.closed_event.is_set())
        manager.disconnect()

    def test_network_gate_wakes_on_recovery_or_safe_pause(self):
        manager = TDLibSessionManager()
        with manager.acquire(object(), "video", config=SimpleNamespace(), factory=FakeClient) as client:
            client.connect_state("connectionStateWaitingForNetwork")
            for recover in (True, False):
                token = GuiCancelToken()
                ready = []
                finished = threading.Event()

                def wait():
                    ready.append(manager.wait_until_ready(token))
                    finished.set()

                thread = threading.Thread(target=wait)
                thread.start()
                self.assertFalse(finished.wait(0.07))
                if recover:
                    client.connect_state("connectionStateReady")
                else:
                    token.stop_after_current_event.set()
                self.assertTrue(finished.wait(0.5))
                thread.join()
                self.assertEqual(ready, [recover])
                client.connect_state("connectionStateWaitingForNetwork")
        manager.disconnect()

    def test_selected_retry_survives_restart_with_unmodified_album_boundary(self):
        media = self.root / "media"
        media.mkdir()
        files = [media / "bad.jpg", media / "deferred.jpg"]
        for path in files:
            path.write_bytes(b"media")
        task = task_for(media, paths=files)
        store = UploadQueue(self.root / "queue.json", runtime_paths=self.paths)
        store.enqueue(task)
        run = store.start(store.prepare(task))
        plan = run.plans[0]
        failed, deferred = plan.items
        result = UploadRunResult(
            "PARTIAL", batches=(UploadBatchResult(plan.key, BatchStatus.FAILED, error="temporary"),),
            deferred_items=(deferred,), failed_items=(failed,),
        )
        finished = store.finish(run, result)
        self.assertEqual(finished.album_statuses()[plan.key].status, AlbumStatus.FAILED)
        deferred.path.unlink()
        retry = store.retry_album(task.task_id, plan.key)
        self.assertEqual(retry.manifest_digest, finished.manifest_digest)
        reopened = UploadQueue(store.path, runtime_paths=self.paths)
        self.assertTrue(reopened.paused)
        resumed = reopened.tasks[0]
        self.assertEqual(resumed.status, TaskStatus.PENDING)
        selected, = resumed.preview()["plans"]
        self.assertEqual(selected.items, plan.items)
        self.assertEqual(selected.pending_items, (failed,))
        self.assertEqual(selected.caption, plan.caption)
        for halted in (
            UploadRunResult("CANCELLED", cancelled=True),
            UploadRunResult("FAILED", paused=True, batches=(UploadBatchResult(plan.key, BatchStatus.FAILED),)),
        ):
            with self.subTest(status=halted.status):
                reopened.set_paused(False)
                running = reopened.start(reopened.prepare(resumed))
                interrupted = reopened.finish(running, halted)
                self.assertEqual(interrupted.retry_album_key, plan.key)
                resumed = reopened.restore(task.task_id)
                selected, = resumed.execution_plans()
                self.assertEqual(selected.items, plan.items)
                self.assertEqual(selected.pending_items, (failed,))

    def test_unknown_album_cannot_be_skipped_or_retried(self):
        task = task_for(self.root / "media")
        store = UploadQueue(self.root / "queue.json", runtime_paths=self.paths)
        plan = task.plans[0]
        journal = InflightJournal(self.paths.UPLOAD_INFLIGHT_DIR)
        journal.prepare(task.kind, plan.key, [payload(item) for item in plan.items], target=dict(task.target))
        journal.unknown(task.kind, plan.key, "unknown", target=dict(task.target))
        end_first(store, task, "UNKNOWN", (UploadBatchResult(plan.key, BatchStatus.UNKNOWN),))
        for action in (store.skip_album, store.retry_album):
            with self.assertRaises(ValueError):
                action(task.task_id, plan.key)
        self.assertTrue(journal.list_unresolved())


def test_preview_keeps_model_nodes_and_syncs_confirmed_files(window, tmp_path):
    win, config, _ = window
    directory = seed(tmp_path / "photos", "image")
    result = scanner.scan_result("image", config=config, source_root=directory, target=main_window._target_for("image"))
    page = win.image_page
    page.set_result(result)
    model = page.tree.model()
    album = model.index(0, 0)
    file_row = QPersistentModelIndex(model.index(0, 0, album))
    assert file_row.isValid() and file_row.data() == "待上传"
    task = UploadTask.from_preview("image", result, config)
    finished = finish_task(task, UploadRunResult(
        "COMPLETED", batches=(UploadBatchResult(task.plans[0].key, BatchStatus.CONFIRMED,
                                               confirmed_items=task.items),),
    ))
    page.sync_task_outcomes(finished)
    assert file_row.isValid() and file_row.data() == "已完成"
    assert page.result["pending_files"] == 0
    assert not page.start_button.isEnabled()


def test_manual_sent_syncs_one_album_even_while_the_job_requires_review(window, tmp_path, monkeypatch, pump):
    win, config, _ = window
    directory = seed(tmp_path / "photos", "image")
    (directory / "other.jpg").write_bytes((directory / "sample.jpg").read_bytes())
    config.IMAGE_ALBUM_SIZE = 1
    result = scanner.scan_result("image", config=config, source_root=directory, target=main_window._target_for("image"))
    page = win.image_page
    page.set_result(result)
    task = UploadTask.from_preview("image", result, config)
    first, second = task.plans
    store = win.queue.store
    reviewed = end_first(store, task, "UNKNOWN", tuple(
        UploadBatchResult(plan.key, BatchStatus.UNKNOWN) for plan in task.plans
    ))
    page.sync_task_outcomes(reviewed)
    journal = InflightJournal(store.runtime_paths.UPLOAD_INFLIGHT_DIR)
    for plan in task.plans:
        journal.prepare(task.kind, plan.key, [payload(item) for item in plan.items], target=dict(task.target))
        journal.unknown(task.kind, plan.key, "still unresolved", target=dict(task.target))
    service = reconciliation.ReconciliationService(journal)
    monkeypatch.setattr(reconciliation, "ReconciliationService", lambda: service)
    monkeypatch.setattr(image_pipeline, "STATE_DIR", store.runtime_paths.IMAGE_STATE_DIR)
    model = page.tree.model()
    album_rows = [QPersistentModelIndex(model.index(index, 0)) for index in (0, 1)]
    assert [row.data() for row in album_rows] == ["待核对", "待核对"]

    win._reconcile_inflight(journal.unresolved(task.kind, first.key, dict(task.target)), True)
    pump(lambda: not win.queue.recovery_busy)
    assert [row.data() for row in album_rows] == ["已完成", "待核对"]
    assert page.result["pending_files"] == 1
    assert store.tasks[0].status is TaskStatus.NEEDS_REVIEW
    assert journal.unresolved(task.kind, second.key, dict(task.target)) is not None


def test_task_center_retries_only_failed_album(window, tmp_path, pump):
    win, _, _ = window
    store = win.queue.store
    task = task_for(tmp_path / "photos")
    plan = task.plans[0]
    end_first(store, task, "FAILED", (UploadBatchResult(plan.key, BatchStatus.FAILED),))
    win._refresh_queue()
    page = win.task_page
    page.albums.setCurrentItem(page.albums.topLevelItem(0))
    assert page.album_retry.isEnabled()
    page.album_retry.click()
    pump(lambda: not win.queue.recovery_busy)
    assert store.paused and store.tasks[0].retry_album_key == plan.key
    assert win.worker is None


def test_strict_queue_continue_restores_head_before_following_job(window, tmp_path, pump):
    win, _, _ = window
    store = win.queue.store
    first = task_for(tmp_path / "first")
    second = task_for(tmp_path / "second")
    store.enqueue(first)
    store.enqueue(second)
    started = store.start(store.prepare(first))
    stopped = store.finish(started, UploadRunResult("FAILED", paused=True, error="retry exhausted"))
    assert stopped.paused_by_policy
    win.queue.can_start = lambda: False
    win._refresh_queue()
    win.task_page.queue_toggle.click()
    pump(lambda: not win.queue.recovery_busy)
    assert not store.paused
    assert [entry.task_id for entry in store.tasks] == [first.task_id, second.task_id]
    assert store.next_pending().task_id == first.task_id


def test_window_runs_video_image_mixed_fifo_through_one_session(window, tmp_path, pump):
    win, config, _ = window
    kinds = ("video", "image", "mixed")
    tasks = [task_for(tmp_path / kind, kind=kind) for kind in kinds]
    seen = []
    clients = []

    def runner(kind, *, ui, config, on_ready, session_manager, **_options):
        with session_manager.acquire(ui, kind, config=config, factory=FakeClient) as client:
            halted = on_ready()
            if halted is not None:
                return halted
            seen.append(kind)
            clients.append(client)
            return UploadRunResult("COMPLETED")

    def make_worker(task):
        worker = UploadWorker(
            task.kind, AuthBridge(), task.preview(), upload_runner=runner,
            config=config, session_manager=win.telegram_session,
        )
        worker.wait_for_scheduler = True
        return worker

    win.queue.make_worker = make_worker
    assert win.queue.set_paused(True)
    for task in tasks:
        assert win.queue.enqueue(task)
    win.task_page.queue_toggle.click()
    pump(lambda: win.worker is None and len(seen) == 3)
    assert tuple(seen) == kinds
    assert len({id(client) for client in clients}) == 1
    assert clients[0].login_count == 1
    assert all(task.status is TaskStatus.COMPLETED for task in win.queue.store.tasks)
    win.telegram_session.disconnect()


def test_window_close_stops_upload_at_album_boundary(window, tmp_path, pump):
    win, config, _ = window
    entered = threading.Event()
    safe_stop = threading.Event()
    task = task_for(tmp_path / "source")

    def runner(_kind, *, on_ready, stop_after_current, cancel_event, **_options):
        unexpected = on_ready()
        if unexpected is not None:
            return unexpected
        entered.set()
        assert stop_after_current.wait(2)
        assert not cancel_event.is_set()
        safe_stop.set()
        return UploadRunResult("CANCELLED", cancelled=True)

    def make_worker(task):
        worker = UploadWorker(task.kind, AuthBridge(), task.preview(), upload_runner=runner, config=config)
        worker.wait_for_scheduler = True
        return worker

    win.queue.make_worker = make_worker
    assert win.queue.enqueue(task)
    pump(entered.is_set)
    assert not win.close()
    pump(lambda: safe_stop.is_set() and not win.isVisible())
    recovered = UploadQueue(win.queue.store.path, runtime_paths=win.queue.store.runtime_paths)
    assert recovered.paused and recovered.tasks[0].status is TaskStatus.INTERRUPTED
