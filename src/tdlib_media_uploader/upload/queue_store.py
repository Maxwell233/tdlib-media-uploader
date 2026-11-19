"""Persist queue transactions before exposing them to the scheduler."""

import json
from dataclasses import replace
from pathlib import Path

from ..core.persistence import atomic_write_json
from .queue import ACTIVE, TaskStatus, UploadTask, finish_task, now


class UploadQueue:
    def __init__(self, path: Path, recover):
        self.path = path
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

    def set_paused(self, paused):
        self._commit(self.tasks, paused)

    def next_pending(self):
        if self.paused or self.error or any(task.status in ACTIVE for task in self.tasks):
            return None
        return next((task for task in self.tasks if task.status is TaskStatus.PENDING), None)

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
        if task.status is not TaskStatus.PREPARING or task not in self.tasks:
            raise ValueError("任务尚未进入准备状态")
        return self._replace(replace(task, status=TaskStatus.RUNNING), paused=self.paused)

    def finish(self, task, result):
        if task.status not in ACTIVE or task not in self.tasks:
            raise ValueError("任务已结束或不存在")
        finished = finish_task(task, result)
        return self._replace(finished, paused=self.paused or finished.status is not TaskStatus.COMPLETED)
