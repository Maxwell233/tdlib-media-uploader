"""Persist queue transactions before exposing them to the scheduler."""

import json
from dataclasses import replace
from pathlib import Path

from ..config import paths
from ..core.persistence import atomic_write_json, atomic_write_text
from .queue import ACTION_REQUIRED, ACTIVE, RECOVERABLE, TaskStatus, UploadTask, finish_task, now
from .queue_recovery import protection_for, recover_task, review_matches_task, verify_task


class UploadQueue:
    def __init__(self, path: Path, recover=None, *, runtime_paths=paths):
        self.path = path
        self.runtime_paths = runtime_paths
        self.tasks: tuple[UploadTask, ...] = ()
        self.paused = False
        self.error = None
        try:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return
            if raw["schema_version"] != 1 or type(raw["paused"]) is not bool:
                raise ValueError("队列文件版本或格式不受支持")
            tasks = tuple(UploadTask.from_record(entry) for entry in raw["tasks"])
            if len({task.task_id for task in tasks}) != len(tasks):
                raise ValueError("队列包含重复任务 ID")
            self.tasks, self.paused = tasks, True
            if any(task.status in ACTIVE for task in tasks) or not raw["paused"]:
                if recover is None:
                    recover = lambda task: recover_task(task, runtime_paths=runtime_paths)
                recovered = tuple(recover(task) if task.status in ACTIVE else task for task in tasks)
                self._commit(recovered, True)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
            self.paused = True
            self.error = f"无法恢复上传队列（{path}）：{exc}"

    def _commit(self, tasks, paused):
        if self.error:
            raise RuntimeError(self.error)
        atomic_write_json(self.path, {
            "schema_version": 1, "paused": paused,
            "tasks": [task.to_record() for task in tasks],
        })
        self.tasks, self.paused = tasks, paused

    def enqueue(self, task):
        if any(entry.status in ACTIVE | {TaskStatus.PENDING} and entry.overlaps(task) for entry in self.tasks):
            raise ValueError("队列已有同类型、同目录、同目标且文件范围重叠的任务")
        if task.status is not TaskStatus.PENDING or any(entry.task_id == task.task_id for entry in self.tasks):
            raise ValueError("任务状态或 ID 无效")
        self._commit((*self.tasks, task), self.paused)

    def _review_task(self):
        return next((task for task in self.tasks if task.status is TaskStatus.NEEDS_REVIEW), None)

    @property
    def blocked_reason(self):
        review = self._review_task()
        if review is not None:
            return f"队列无法继续。{review.source_root} 的任务待核对，请先处理未确认上传记录。"
        first = self._head()
        if first is not None and first.status in RECOVERABLE:
            return f"队列无法继续。请选择恢复或跳过 {first.source_root} 的任务。"
        return None

    def _head(self):
        waiting = RECOVERABLE | {TaskStatus.PENDING}
        return next((task for task in self.tasks if task.status in waiting), None)

    def set_paused(self, paused):
        reason = self.blocked_reason if not paused else None
        if reason:
            raise ValueError(reason)
        self._commit(self.tasks, paused)

    def next_pending(self):
        if self.paused or self.error or self.blocked_reason or any(task.status in ACTIVE for task in self.tasks):
            return None
        return self._head()

    def _action_task(self, task_id, allowed):
        if any(task.status in ACTIVE for task in self.tasks):
            raise ValueError("请等待当前上传结束后再处理任务")
        task = next((task for task in self.tasks if task.task_id == task_id), None)
        if task is None or task.status not in allowed:
            raise ValueError("该任务状态不支持此操作")
        return task

    def _commit_recovery(self, tasks):
        # The previous queue normally stays in place on a write failure.
        # Restore it if directory fsync failed after the atomic replace.
        previous = self.path.read_bytes()
        saved = json.loads(previous)
        if (saved["schema_version"] != 1 or saved["paused"] != self.paused
                or tuple(UploadTask.from_record(task) for task in saved["tasks"]) != self.tasks):
            raise ValueError("磁盘上的队列文件已变化，请重启后再次核对")
        try:
            self._commit(tasks, paused=True)
        except OSError:
            try:
                if self.path.read_bytes() != previous:
                    atomic_write_text(self.path, previous.decode("utf-8"))
            except OSError as rollback_error:
                self.error = f"无法确认队列文件已恢复：{rollback_error}"
                self.paused = True
            raise

    def _save_recovery(self, updated):
        self._commit_recovery(tuple(updated if task.task_id == updated.task_id else task for task in self.tasks))
        return updated

    def restore(self, task_id):
        task = self._action_task(task_id, ACTION_REQUIRED)
        if any(entry.status is TaskStatus.PENDING and entry.overlaps(task) for entry in self.tasks):
            raise ValueError("队列中有重叠的等待任务，请先移除重复项")
        confirmed = verify_task(task, self.runtime_paths)
        return self._save_recovery(replace(
            task, status=TaskStatus.PENDING, confirmed_paths=confirmed,
            started_at=None, finished_at=None, outcome=None, error=None,
        ))

    def recheck(self, task_id):
        task = self._action_task(task_id, {TaskStatus.NEEDS_REVIEW})
        return self._save_recovery(self._reviewed(task))

    def _reviewed(self, task):
        confirmed = verify_task(task, self.runtime_paths, verify_source=False)
        return replace(task, status=TaskStatus.INTERRUPTED, confirmed_paths=confirmed,
                       error="发送记录和断点已核对。请选择恢复或跳过此任务。")

    def refresh_reviews(self, record):
        if not any(task.status is TaskStatus.NEEDS_REVIEW for task in self.tasks):
            return
        if any(task.status in ACTIVE for task in self.tasks):
            raise ValueError("请等待当前上传结束后再核对队列")
        tasks = []
        for task in self.tasks:
            if task.status is TaskStatus.NEEDS_REVIEW and review_matches_task(record, task):
                try:
                    task = self._reviewed(task)
                except (OSError, ValueError, TypeError, RuntimeError):
                    # A single manual decision may leave other Albums protected.
                    pass
            tasks.append(task)
        if tuple(tasks) != self.tasks:
            self._commit_recovery(tuple(tasks))

    def skip(self, task_id):
        task = self._action_task(task_id, RECOVERABLE)
        if protection_for(task, self.runtime_paths) is not None:
            raise ValueError("任务仍有未确认发送记录，请先核对后再跳过")
        return self._save_recovery(replace(
            task, status=TaskStatus.SKIPPED, finished_at=now(), outcome="SKIPPED",
            error="用户选择跳过未完成的任务。",
        ))

    def _pending_index(self, task_id):
        for index, task in enumerate(self.tasks):
            if task.task_id == task_id and task.status is TaskStatus.PENDING:
                return index
        raise ValueError("只能操作等待中的任务")

    def remove(self, task_id):
        index = self._pending_index(task_id)
        self._commit(self.tasks[:index] + self.tasks[index + 1:], self.paused)

    def move(self, task_id, direction):
        indices = [i for i, task in enumerate(self.tasks) if task.status is TaskStatus.PENDING]
        index = self._pending_index(task_id)
        destination = indices.index(index) + direction
        if 0 <= destination < len(indices):
            other = indices[destination]
            tasks = list(self.tasks)
            tasks[index], tasks[other] = tasks[other], tasks[index]
            self._commit(tuple(tasks), self.paused)

    def _replace(self, task, *, paused):
        self._commit(tuple(task if value.task_id == task.task_id else value for value in self.tasks), paused)
        return task

    def prepare(self, task):
        if self.next_pending() != task:
            raise ValueError("任务当前不能启动")
        return self._replace(replace(task, status=TaskStatus.PREPARING, started_at=now()), paused=self.paused)

    def start(self, task):
        if task.status is not TaskStatus.PREPARING or task not in self.tasks or self._review_task() is not None:
            raise ValueError("任务尚未进入准备状态")
        return self._replace(replace(task, status=TaskStatus.RUNNING), paused=self.paused)

    def finish(self, task, result):
        if task.status not in ACTIVE or task not in self.tasks:
            raise ValueError("任务已结束或不存在")
        finished = finish_task(task, result)
        return self._replace(finished, paused=self.paused or finished.status is not TaskStatus.COMPLETED)
