"""Coordinate uploads and background recovery from the GUI thread."""

from copy import deepcopy

from PySide6.QtCore import QObject, Qt, Signal

from ..core.models import UploadRunResult
from ..upload.queue_store import UploadQueue
from .workers import QueueRecoveryResult, QueueRecoveryWorker


class QueueController(QObject):
    changed = Signal()
    error = Signal(str)
    launched = Signal(object, object)
    ended = Signal(object)
    skipped = Signal(object)
    recovery_progress = Signal(str, bool)
    recovery_finished = Signal(object)

    def __init__(self, store: UploadQueue, make_worker, can_start, parent=None):
        super().__init__(parent)
        self.store = store
        self.make_worker = make_worker
        self.can_start = can_start
        self.worker = None
        self.recovery_worker = None
        self.task = None
        self.closing = False
        self.write_blocked = False

    @property
    def recovery_busy(self):
        return self.recovery_worker is not None

    def _change(self, action, *args):
        if self.recovery_busy:
            return False
        try:
            action(*args)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            self.error.emit(str(exc))
            return False
        self.changed.emit()
        return True

    def enqueue(self, task):
        if not self._change(self.store.enqueue, task):
            return False
        self.schedule()
        return True

    def set_paused(self, paused):
        if self.write_blocked:
            self.error.emit("任务结果尚未安全写入队列，请检查数据目录后重启。")
        elif self._change(self.store.set_paused, paused) and not paused:
            self.schedule()

    def move(self, task_id, direction):
        self._change(self.store.move, task_id, direction)

    def remove(self, task_id):
        self._change(self.store.remove, task_id)

    def restore(self, task_id):
        return self._start_recovery("restore", task_id)

    def recheck(self, task_id):
        return self._start_recovery("recheck", task_id)

    def refresh_reviews(self, record):
        return self._start_recovery("refresh_reviews", deepcopy(record))

    def skip(self, task_id):
        return self._start_recovery("skip", task_id)

    def _start_recovery(self, action, subject):
        if self.recovery_busy:
            return False
        if self.write_blocked or self.store.error or self.closing or self.worker is not None:
            self.error.emit("当前无法安全修改任务，请等待上传结束或检查队列文件。")
            return False

        worker = QueueRecoveryWorker(self.store, action, subject, self.store.revision)
        self.recovery_worker = worker
        worker.progress.connect(
            lambda message, cancelable, w=worker: self._recovery_progress(w, message, cancelable),
            Qt.ConnectionType.QueuedConnection,
        )
        worker.finished.connect(
            lambda w=worker: self._recovery_finished(w), Qt.ConnectionType.QueuedConnection
        )
        self.changed.emit()
        self.recovery_progress.emit("正在验证任务…", True)
        try:
            worker.start()
        except RuntimeError as exc:
            worker.result = QueueRecoveryResult(action, error=str(exc))
            self._recovery_finished(worker)
            return False
        return True

    def _recovery_progress(self, worker, message, cancelable):
        if self.recovery_worker is worker and not worker.cancel_event.is_set():
            self.recovery_progress.emit(message, cancelable)

    def cancel_recovery(self):
        if self.recovery_worker is not None:
            self.recovery_worker.request_stop()
            self.recovery_progress.emit("已请求取消，等待当前步骤安全结束…", False)

    def _recovery_finished(self, worker):
        if self.recovery_worker is not worker or worker.isRunning():
            return
        result = worker.result or QueueRecoveryResult(worker.action, error="后台操作未返回结果")
        self.recovery_worker = None
        worker.deleteLater()
        self.changed.emit()
        if result.error:
            self.error.emit(f"队列操作失败：{result.error}")
        elif result.action == "skip" and result.task is not None:
            self.skipped.emit(result.task)
        self.recovery_finished.emit(result)

    def schedule(self):
        if self.recovery_busy or self.worker is not None or self.closing or self.write_blocked:
            return
        task = self.store.next_pending()
        if task is None:
            return
        try:
            if not self.can_start():
                return
            self.task = self.store.prepare(task)
        except (OSError, ValueError, RuntimeError) as exc:
            self.error.emit(f"无法保存任务准备状态：{exc}")
            return
        try:
            worker = self.make_worker(self.task)
            self.worker = worker
            worker.ready_to_run.connect(
                lambda w=worker: self._ready(w), Qt.ConnectionType.QueuedConnection
            )
            worker.finished.connect(
                lambda w=worker: self._finished(w), Qt.ConnectionType.QueuedConnection
            )
            self.changed.emit()
            self.launched.emit(self.task, worker)
            worker.start()
        except Exception as exc:
            self._finish(UploadRunResult("FAILED", error=f"无法启动上传任务：{exc}"))

    def _ready(self, worker):
        if self.worker is not worker or self.closing:
            return
        try:
            self.task = self.store.start(self.task)
        except (OSError, ValueError, RuntimeError) as exc:
            worker.preparation_error = f"无法保存任务运行状态：{exc}"
        self.changed.emit()
        worker.prepared.set()

    def _finished(self, worker):
        if self.worker is not worker or worker.isRunning():
            return
        self._finish(worker.result or UploadRunResult("UNKNOWN", error="上传线程没有返回结果"))
        worker.deleteLater()

    def _finish(self, result):
        task = self.task
        self.worker = self.task = None
        try:
            task = self.store.finish(task, result)
        except (OSError, ValueError, RuntimeError) as exc:
            self.write_blocked = True
            self.error.emit(f"任务结果未写入，队列已停止：{exc}")
        self.changed.emit()
        self.ended.emit(task)
        self.schedule()

    def finish_after_wait(self):
        if self.worker is not None:
            self._finished(self.worker)
