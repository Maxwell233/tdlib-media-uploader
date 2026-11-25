"""Verify queued snapshots and durable delivery state before allowing recovery."""

from dataclasses import replace
from time import monotonic

from ..config import paths
from ..core.concurrency import raise_if_cancelled
from ..core.filesystem import stable_path
from ..core.identity import relative_media_path
from ..core.source_snapshot import validate_snapshots
from ..core.upload_journal import InflightJournal, _target_matches
from ..core.upload_state import UploadState
from .planner import validate_plan
from .queue import TaskStatus, UploadTask, now


def _snapshots(task):
    return [
        {"path": item.path, "size": item.snapshot.size, "mtime_ns": item.snapshot.mtime_ns}
        for item in task.items
    ]


def review_matches_task(record, task):
    if record.get("kind") != task.kind or not _target_matches(record, dict(task.target)):
        return False
    if any(plan.key == record.get("album_key") for plan in task.plans):
        return True
    paths = {stable_path(item["path"]) for item in record.get("items", ())
             if isinstance(item, dict) and item.get("path")}
    return any(stable_path(item.path) in paths for item in task.items)


def protection_for(task, runtime_paths, *, cancel_event=None, database=None):
    journal = InflightJournal(runtime_paths.UPLOAD_INFLIGHT_DIR, database=database)
    target = dict(task.target)
    for plan in task.plans:
        raise_if_cancelled(cancel_event=cancel_event)
        record = journal.unresolved(task.kind, plan.key, target=target)
        if record is not None:
            return record
    return journal.unresolved_for_items(task.kind, _snapshots(task), target=target)


def _checkpoint_paths(task, runtime_paths, *, cancel_event=None, database=None):
    state = UploadState(
        kind=task.kind, source_root=task.source_root, target=dict(task.target),
        state_dir=getattr(runtime_paths, f"{task.kind.upper()}_STATE_DIR"), create=False, database=database,
    )
    data = state.data
    if (data.get("kind") != task.kind or data.get("source_root") != stable_path(task.source_root)
            or data.get("target") != task.target):
        raise ValueError(f"上传断点的来源、媒体类型或 Telegram 目标不匹配：{state.path}")
    records = data["completed"]
    confirmed = set()
    for item in _snapshots(task):
        raise_if_cancelled(cancel_event=cancel_event)
        signature = state.signature(item)
        if signature not in records:
            continue
        record = records[signature]
        if (not isinstance(record, dict) or record.get("size") != item["size"]
                or record.get("mtime_ns") != item["mtime_ns"]
                or record.get("relative_path") != relative_media_path(item["path"], task.source_root)):
            raise ValueError(f"上传断点损坏：{item['path']}")
        confirmed.add(stable_path(item["path"]))
    known = task.known_confirmed
    missing = known - confirmed
    if missing:
        raise ValueError(
            f"已确认发送的文件缺少有效断点（{len(missing)}/{len(known)}），例如：{min(missing)}。"
            "请先核对发送记录或修复断点。"
        )
    return tuple(sorted(confirmed))


def verify_delivery(task, runtime_paths, *, cancel_event=None, database=None):
    """Verify the target-scoped journal and checkpoints without accessing source files."""
    raise_if_cancelled(cancel_event=cancel_event)
    record = protection_for(task, runtime_paths, cancel_event=cancel_event, database=database)
    if record is not None:
        raise ValueError(
            f"仍有未解决的上传记录（{record['status']}，Album {record.get('album_key') or '未知'}）。"
            "请先在“未确认上传”核对。"
        )
    return _checkpoint_paths(task, runtime_paths, cancel_event=cancel_event, database=database)


def _validate_files(task, cancel_event, progress):
    if not task.source_root.is_dir():
        raise ValueError(f"原始来源目录不可用：{task.source_root}")
    if task.retry_album_key is not None or task.skipped_albums:
        items = tuple(item for plan in task.execution_plans() for item in plan.pending_items)
    else:
        items = task.items
    total = len(items)

    def snapshots():
        last_update = monotonic()
        progress(f"正在检查源文件 0/{total}", True)
        for count, item in enumerate(items, 1):
            raise_if_cancelled(cancel_event=cancel_event)
            yield item.path, item.snapshot.size, item.snapshot.mtime_ns
            current = monotonic()
            if count == total or current - last_update >= 0.1:
                progress(f"正在检查源文件 {count}/{total}", True)
                last_update = current

    validate_snapshots(snapshots())


def verify_task(task, runtime_paths, *, verify_source=True, cancel_event=None, progress=None, database=None):
    """Read the original task and its latest protections without opening a Worker."""
    if progress is None:
        progress = lambda _message, _cancelable: None
    for plan in task.plans:
        raise_if_cancelled(cancel_event=cancel_event)
        validate_plan(plan)
        if plan.kind != task.kind or plan.source_root != task.source_root or plan.target != task.target:
            raise ValueError("原始 Album 的媒体类型、来源或 Telegram 目标不一致")
    UploadTask.from_record(task.to_record())
    if verify_source:
        raise_if_cancelled(cancel_event=cancel_event)
        _validate_files(task, cancel_event, progress)
    progress("正在核对上传断点和发送记录…", True)
    return verify_delivery(task, runtime_paths, cancel_event=cancel_event, database=database)


def recover_task(task, *, runtime_paths=paths, database=None):
    """Record an interrupted run and keep any uncertain delivery blocked."""
    try:
        confirmed = verify_task(task, runtime_paths, verify_source=False, database=database)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        return replace(task, status=TaskStatus.NEEDS_REVIEW, finished_at=now(), outcome="RESTARTED",
                       error=f"上次运行的任务需要核对：{exc}")
    return replace(
        task, status=TaskStatus.INTERRUPTED, finished_at=now(), outcome="RESTARTED",
        confirmed_paths=confirmed, error=f"上次运行中断，已核对 {len(confirmed)}/{len(task.items)} 个文件的断点。",
    )
