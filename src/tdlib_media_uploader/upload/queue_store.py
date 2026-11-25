"""Persist queue transactions before exposing them to the scheduler."""

import json
from dataclasses import replace
from functools import wraps
from pathlib import Path
from threading import Lock

from ..config import paths
from ..core.concurrency import CancellationRequested, raise_if_cancelled
from ..core.filesystem import stable_path
from ..core.persistence import atomic_write_json, atomic_write_text
from ..core.uploader_database import database_for, encode_record
from .queue import (
    ACTION_REQUIRED, ACTIVE, RECOVERABLE, AlbumOutcome, AlbumStatus, TaskStatus, UploadTask, finish_task, now,
    retry_failed_album, skip_album,
)
from .queue_recovery import recover_task, review_matches_task, verify_delivery, verify_task


class QueueRecoveryConflict(ValueError):
    """The verified task or queue no longer matches its current state."""


def _queue_write(action):
    @wraps(action)
    def guarded(self, *args, **kwargs):
        if not self._writer.acquire(blocking=False):
            raise RuntimeError("正在处理队列事务，请等待当前操作结束")
        try:
            if self.error:
                raise RuntimeError(self.error)
            return action(self, *args, **kwargs)
        finally:
            self._writer.release()

    return guarded


class UploadQueue:
    """Serialize mutations across threads. Readers see the last durable task tuple."""

    def __init__(self, path: Path, recover=None, *, runtime_paths=paths, database=None):
        self.path = Path(path)
        self.runtime_paths = runtime_paths
        self.tasks: tuple[UploadTask, ...] = ()
        self.paused = False
        self.error = None
        self._writer = Lock()
        self._revision = 0
        self._recovering = False
        self.database = database
        try:
            if self.path == paths.DATA_DIR / "upload_queue.json":
                from ..core.upload_journal import InflightJournal
                from ..core.upload_state import migrate_checkpoints

                if self.database is None:
                    self.database = database_for(paths.DATA_DIR / "uploader.db")
                migrate_checkpoints(self.database, paths.STATE_DIR)
                InflightJournal(paths.UPLOAD_INFLIGHT_DIR, database=self.database)
            if self.database is not None:
                self._queue_key = self.database.key(self.path)
                self.database.migrate("queue", self._queue_key, self._import_legacy)
                with self.database.read() as connection:
                    raw = self._read_database(connection)
                    tasks = self._database_tasks(connection, raw) if raw is not None else ()
            else:
                raw = self._read_legacy()
                tasks = self._read_tasks(raw) if raw is not None else ()
            if raw is None:
                return
            self.tasks, self.paused = tasks, True
            if any(task.status in ACTIVE for task in tasks) or not raw["paused"]:
                if recover is None:
                    recover = lambda task: recover_task(task, runtime_paths=runtime_paths, database=self.database)
                recovered = tuple(recover(task) if task.status in ACTIVE else task for task in tasks)
                self._commit(recovered, True)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
            self.paused = True
            self.error = f"无法恢复上传队列（{path}）：{exc}"

    def _read_legacy(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    @staticmethod
    def _read_tasks(raw):
        if raw["schema_version"] != 1 or type(raw["paused"]) is not bool:
            raise ValueError("队列文件版本或格式不受支持")
        tasks = tuple(UploadTask.from_record(entry) for entry in raw["tasks"])
        if len({task.task_id for task in tasks}) != len(tasks):
            raise ValueError("队列包含重复任务 ID")
        return tasks

    def _import_legacy(self, connection):
        raw = self._read_legacy()
        if raw is not None:
            self._read_tasks(raw)
            self._write_database(connection, raw)

    def _read_database(self, connection):
        row = connection.execute(
            "SELECT metadata FROM upload_queues WHERE queue_key=?", (self._queue_key,)
        ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def _write_database(self, connection, raw):
        connection.execute(
            "INSERT INTO upload_queues VALUES (?, ?) ON CONFLICT(queue_key) DO UPDATE SET metadata=excluded.metadata",
            (self._queue_key, encode_record(raw)),
        )

    def _database_tasks(self, connection, raw):
        tasks = self._read_tasks(raw)
        return tuple(self._incorporate_receipts(connection, task) for task in tasks)

    @staticmethod
    def _incorporate_receipts(connection, task):
        rows = connection.execute(
            "SELECT paths FROM queue_album_receipts WHERE task_id=?", (task.task_id,)
        ).fetchall()
        if not rows:
            return task
        confirmed = set(task.confirmed_paths)
        for (saved,) in rows:
            confirmed.update(json.loads(saved))
        return replace(task, confirmed_paths=tuple(sorted(confirmed)))

    @property
    def revision(self):
        return self._revision

    @staticmethod
    def _record(tasks, paused):
        return {
            "schema_version": 1, "paused": paused,
            "tasks": [task.to_record() for task in tasks],
        }

    def _publish(self, tasks, paused):
        self.tasks, self.paused = tasks, paused
        self._revision += 1

    def _commit(self, tasks, paused):
        raw = self._record(tasks, paused)
        if self.database is None:
            atomic_write_json(self.path, raw)
        else:
            with self.database.write() as connection:
                self._write_database(connection, raw)
                removed = {task.task_id for task in self.tasks} - {task.task_id for task in tasks}
                connection.executemany("DELETE FROM queue_album_receipts WHERE task_id=?", ((key,) for key in removed))
        self._publish(tasks, paused)

    @_queue_write
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

    @_queue_write
    def set_paused(self, paused):
        reason = self.blocked_reason if not paused else None
        if reason:
            raise ValueError(reason)
        self._commit(self.tasks, paused)

    def next_pending(self):
        if (self._recovering or self.paused or self.error or self.blocked_reason
                or any(task.status in ACTIVE for task in self.tasks)):
            return None
        return self._head()

    def _action_task(self, task_id, allowed):
        if any(task.status in ACTIVE for task in self.tasks):
            raise ValueError("请等待当前上传结束后再处理任务")
        task = next((task for task in self.tasks if task.task_id == task_id), None)
        if task is None or task.status not in allowed:
            raise ValueError("该任务状态不支持此操作")
        return task

    def _check_revision(self, revision):
        if self.revision != revision:
            raise QueueRecoveryConflict("队列状态已变化，请重新验证任务")

    def _disk_conflict(self, detail):
        # Prevent later mutations from overwriting this file with the stale in-memory queue.
        self.error = f"无法确认磁盘上的队列文件，请重启后再次核对：{detail}"
        return QueueRecoveryConflict(self.error)

    def _read_recovery_queue(self):
        try:
            return self.path.read_bytes()
        except OSError as exc:
            raise self._disk_conflict(exc) from exc

    def _commit_recovery(self, tasks, verified, revision, cancel_event, progress):
        progress("正在准备保存队列状态…", True)
        if self.database is not None:
            raise_if_cancelled(cancel_event=cancel_event)
            progress("正在保存队列状态…", False)
            with self.database.write() as connection:
                self._check_revision(revision)
                saved = self._read_database(connection)
                if (saved is None or saved["paused"] is not self.paused
                        or self._database_tasks(connection, saved) != self.tasks):
                    raise self._disk_conflict("磁盘上的队列状态已变化")
                for task, expected in verified:
                    if verify_delivery(task, self.runtime_paths, database=self.database) != expected:
                        raise QueueRecoveryConflict("上传断点已变化，请重新验证任务")
                self._write_database(connection, self._record(tasks, paused=True))
            self._publish(tasks, paused=True)
            return
        previous = self._read_recovery_queue()
        try:
            saved = json.loads(previous)
            matches = (
                saved["schema_version"] == 1 and saved["paused"] is self.paused
                and tuple(UploadTask.from_record(task) for task in saved["tasks"]) == self.tasks
            )
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise self._disk_conflict(exc) from exc
        if not matches:
            raise self._disk_conflict("磁盘上的队列文件已变化")
        replacement = self._record(tasks, paused=True)
        replace_attempted = False

        def check_delivery():
            self._check_revision(revision)
            for task, expected in verified:
                if verify_delivery(task, self.runtime_paths) != expected:
                    raise QueueRecoveryConflict("上传断点已变化，请重新验证任务")

        def before_replace():
            nonlocal replace_attempted
            check_delivery()
            if self._read_recovery_queue() != previous:
                raise self._disk_conflict("磁盘上的队列文件已变化")
            # After this point only roll back a copy of our own replacement.
            replace_attempted = True

        raise_if_cancelled(cancel_event=cancel_event)
        progress("正在保存队列状态…", False)
        try:
            atomic_write_json(self.path, replacement, before_replace=before_replace)
            # A concurrent external journal change during directory fsync
            # must not publish a result based on the earlier validation.
            check_delivery()
            if json.loads(self._read_recovery_queue()) != replacement:
                raise self._disk_conflict("保存时磁盘上的队列文件已变化")
        except (OSError, ValueError, TypeError, RuntimeError):
            if replace_attempted:
                self._rollback_recovery(previous, replacement)
            raise
        self._publish(tasks, paused=True)

    def _rollback_recovery(self, previous, replacement):
        try:
            current = self.path.read_bytes()
            if current != previous:
                if json.loads(current) != replacement:
                    raise QueueRecoveryConflict("磁盘上的队列已被修改")
                atomic_write_text(self.path, previous.decode("utf-8"))
        except (OSError, ValueError) as exc:
            self.error = f"无法确认队列文件已恢复：{exc}"
            self.paused = True

    @_queue_write
    def recover(self, action, subject, *, expected_revision=None, cancel_event=None, progress=None):
        """Validate and commit one recovery under the same exclusive writer lock."""
        revision = self.revision
        if expected_revision is not None:
            self._check_revision(expected_revision)
        self._recovering = True
        try:
            if progress is None:
                progress = lambda _message, _cancelable: None
            progress("正在验证任务…", True)
            raise_if_cancelled(cancel_event=cancel_event)
            if action == "refresh_reviews":
                updates = self._review_updates(subject, cancel_event, progress)
                result = None
            else:
                result = self._recover_task(action, subject, cancel_event, progress)
                updates = {result.task_id: result}

            if updates:
                tasks = tuple(updates.get(task.task_id, task) for task in self.tasks)
                verified = [(task, updates[task.task_id].confirmed_paths)
                            for task in self.tasks if task.task_id in updates]
                self._commit_recovery(tasks, verified, revision, cancel_event, progress)
            return result
        finally:
            self._recovering = False

    def _recover_task(self, action, subject, cancel_event, progress):
        album_action = action in {"skip_album", "retry_album"}
        task_id, album_key = subject if album_action else (subject, None)
        task = self._action_task(task_id, {
            "restore": ACTION_REQUIRED, "skip": RECOVERABLE, "recheck": {TaskStatus.NEEDS_REVIEW},
            "skip_album": RECOVERABLE | {TaskStatus.PENDING}, "retry_album": RECOVERABLE,
        }[action])
        if action in {"restore", "retry_album"} and any(
            entry.status is TaskStatus.PENDING and entry.overlaps(task) for entry in self.tasks
        ):
            raise ValueError("队列中有重叠的等待任务，请先移除重复项")
        confirmed = verify_task(
            task, self.runtime_paths, verify_source=action == "restore",
            cancel_event=cancel_event, progress=progress, database=self.database,
        )
        if action == "skip_album":
            return skip_album(task, album_key, confirmed)
        if action == "retry_album":
            retry = retry_failed_album(task, album_key, confirmed)
            verify_task(retry, self.runtime_paths, cancel_event=cancel_event, progress=progress, database=self.database)
            return retry
        confirmed_set = set(confirmed)
        plans_by_key = {plan.key: plan for plan in task.plans}
        outcomes = []
        for outcome in task.album_outcomes:
            if outcome.status is not AlbumStatus.UNKNOWN:
                outcomes.append(outcome)
                continue
            plan = plans_by_key.get(outcome.album_key)
            if plan is None or outcome.failure_stage == "persistence":
                continue
            pending = {stable_path(item.path) for item in plan.pending_items}
            remaining = sorted(pending - confirmed_set)
            if (pending & confirmed_set) and remaining:
                outcomes.append(
                    AlbumOutcome(
                        outcome.album_key,
                        AlbumStatus.PARTIAL,
                        outcome.error,
                        failed_paths=tuple(remaining),
                        failure_stage="partial_delivery",
                    )
                )
        outcomes = tuple(outcomes)
        if action == "restore":
            return replace(task, status=TaskStatus.PENDING, confirmed_paths=confirmed, album_outcomes=outcomes,
                           started_at=None, finished_at=None, outcome=None, error=None, paused_by_policy=False)
        if action == "skip":
            return replace(task, status=TaskStatus.SKIPPED, confirmed_paths=confirmed,
                           finished_at=now(), outcome="SKIPPED", error="用户选择跳过未完成的任务。",
                           paused_by_policy=False)
        return replace(task, status=TaskStatus.INTERRUPTED, confirmed_paths=confirmed, album_outcomes=outcomes,
                       error="发送记录和断点已核对。请选择恢复或跳过此任务。")

    def _review_updates(self, record, cancel_event, progress):
        updates = {}
        for task in self.tasks:
            raise_if_cancelled(cancel_event=cancel_event)
            if task.status is TaskStatus.NEEDS_REVIEW and review_matches_task(record, task):
                try:
                    updates[task.task_id] = self._recover_task("recheck", task.task_id, cancel_event, progress)
                except CancellationRequested:
                    raise
                except (OSError, ValueError, TypeError, RuntimeError):
                    # A single manual decision may leave other Albums protected.
                    pass
        return updates

    def restore(self, task_id):
        return self.recover("restore", task_id)

    def recheck(self, task_id):
        return self.recover("recheck", task_id)

    def refresh_reviews(self, record):
        return self.recover("refresh_reviews", record)

    def skip(self, task_id):
        return self.recover("skip", task_id)

    def skip_album(self, task_id, album_key):
        return self.recover("skip_album", (task_id, album_key))

    def retry_album(self, task_id, album_key):
        return self.recover("retry_album", (task_id, album_key))

    def _pending_index(self, task_id):
        for index, task in enumerate(self.tasks):
            if task.task_id == task_id and task.status is TaskStatus.PENDING:
                return index
        raise ValueError("只能操作等待中的任务")

    @_queue_write
    def remove(self, task_id):
        index = self._pending_index(task_id)
        self._commit(self.tasks[:index] + self.tasks[index + 1:], self.paused)

    @_queue_write
    def clear_finished(self):
        terminal = {TaskStatus.COMPLETED, TaskStatus.SKIPPED}
        remaining = tuple(task for task in self.tasks if task.status not in terminal)
        removed = len(self.tasks) - len(remaining)
        if removed:
            self._commit(remaining, self.paused)
        return removed

    @_queue_write
    def move(self, task_id, direction):
        indices = [i for i, task in enumerate(self.tasks) if task.status is TaskStatus.PENDING]
        index = self._pending_index(task_id)
        destination = indices.index(index) + direction
        if 0 <= destination < len(indices):
            other = indices[destination]
            tasks = list(self.tasks)
            tasks[index], tasks[other] = tasks[other], tasks[index]
            self._commit(tuple(tasks), self.paused)

    @_queue_write
    def move_to_top(self, task_id):
        index = self._pending_index(task_id)
        indices = [i for i, task in enumerate(self.tasks) if task.status is TaskStatus.PENDING]
        destination = indices.index(index)
        if destination > 0:
            tasks = list(self.tasks)
            for old, new in zip(reversed(indices[:destination]), reversed(indices[1:destination + 1])):
                tasks[new] = self.tasks[old]
            tasks[indices[0]] = self.tasks[index]
            self._commit(tuple(tasks), self.paused)

    def _replace(self, task, *, paused):
        self._commit(tuple(task if value.task_id == task.task_id else value for value in self.tasks), paused)
        return task

    @_queue_write
    def prepare(self, task):
        if self.next_pending() != task:
            raise ValueError("任务当前不能启动")
        return self._replace(replace(task, status=TaskStatus.PREPARING, started_at=now()), paused=self.paused)

    @_queue_write
    def start(self, task):
        if task.status is not TaskStatus.PREPARING or task not in self.tasks or self._review_task() is not None:
            raise ValueError("任务尚未进入准备状态")
        return self._replace(replace(task, status=TaskStatus.RUNNING), paused=self.paused)

    @_queue_write
    def finish(self, task, result):
        if task.status not in ACTIVE or task not in self.tasks:
            raise ValueError("任务已结束或不存在")
        finished = finish_task(task, result)
        return self._replace(finished, paused=self.paused or result.paused or finished.status is not TaskStatus.COMPLETED)
