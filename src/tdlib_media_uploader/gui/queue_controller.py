"""Run queue transactions on the GUI thread and own the upload worker."""

from dataclasses import replace

from PySide6.QtCore import QObject, Qt, Signal

from ..config import paths
from ..core.models import UploadRunResult
from ..core.upload_journal import InflightJournal
from ..core.upload_state import UploadState
from ..upload.queue import TaskStatus, now
from ..upload.queue_store import UploadQueue


def recover_task(task, *, runtime_paths=paths):
    """Inspect durable protections and checkpoints without contacting Telegram."""
    status = TaskStatus.INTERRUPTED
    try:
        state = UploadState(
            kind=task.kind, source_root=task.source_root, target=dict(task.target),
            state_dir=getattr(runtime_paths, f"{task.kind.upper()}_STATE_DIR"), create=False,
        )
        journal = InflightJournal(runtime_paths.UPLOAD_INFLIGHT_DIR)
        target = dict(task.target)
        for plan in task.plans:
            entries = [
                {"path": item.path, "size": item.snapshot.size, "mtime_ns": item.snapshot.mtime_ns}
                for item in plan.items
            ]
            if (journal.unresolved(task.kind, plan.key, target=target)
                    or journal.unresolved_for_items(task.kind, entries, target=target)):
                status = TaskStatus.NEEDS_REVIEW
                break
        confirmed = sum(
            state.is_completed({"path": item.path, "size": item.snapshot.size,
                                "mtime_ns": item.snapshot.mtime_ns}) for item in task.items
        )
        error = f"上次退出时任务仍在运行。已核对本地断点 {confirmed}/{len(task.items)}。"
        if status is TaskStatus.NEEDS_REVIEW:
            error += " 请先在“未确认上传”核对保留的 Album。"
    except Exception as exc:
        status, error = TaskStatus.NEEDS_REVIEW, f"无法核对上次任务的断点与发送记录：{exc}"
    return replace(task, status=status, finished_at=now(), outcome="RESTARTED", error=error)


class QueueController(QObject):
    changed = Signal()
    error = Signal(str)
    launched = Signal(object, object)
    ended = Signal(object)

    def __init__(self, store: UploadQueue, make_worker, can_start, parent=None):
        super().__init__(parent)
        self.store = store
        self.make_worker = make_worker
        self.can_start = can_start
        self.worker = None
        self.task = None
        self.closing = False
        self.write_blocked = False

    def _change(self, action, *args):
        try:
            action(*args)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
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

    def schedule(self):
        if self.worker is not None or self.closing or self.write_blocked:
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
