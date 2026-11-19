"""Verify queued snapshots and durable delivery state before allowing recovery."""

from dataclasses import replace

from ..config import paths
from ..core.filesystem import stable_path
from ..core.identity import canonical_target
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


def protection_for(task, runtime_paths):
    journal = InflightJournal(runtime_paths.UPLOAD_INFLIGHT_DIR)
    target = dict(task.target)
    for plan in task.plans:
        record = journal.unresolved(task.kind, plan.key, target=target)
        if record is not None:
            return record
    return journal.unresolved_for_items(task.kind, _snapshots(task), target=target)


def _checkpoint_paths(task, runtime_paths):
    state = UploadState(
        kind=task.kind, source_root=task.source_root, target=dict(task.target),
        state_dir=getattr(runtime_paths, f"{task.kind.upper()}_STATE_DIR"), create=False,
    )
    data = state.data
    if (data.get("kind") != task.kind or data.get("source_root") != stable_path(task.source_root)
            or canonical_target(data.get("target")) != task.target):
        raise ValueError(f"上传断点的来源、媒体类型或 Telegram 目标不匹配：{state.path}")
    records = data["completed"]
    confirmed = set()
    for item in _snapshots(task):
        signature = state.signature(item)
        if signature not in records:
            continue
        record = records[signature]
        if (not isinstance(record, dict) or record.get("size") != item["size"]
                or record.get("mtime_ns") != item["mtime_ns"]):
            raise ValueError(f"上传断点损坏：{item['path']}")
        confirmed.add(stable_path(item["path"]))
    missing = task.known_confirmed - confirmed
    if missing:
        raise ValueError(f"已确认发送的文件缺少有效断点：{min(missing)}")
    return tuple(sorted(confirmed))


def verify_task(task, runtime_paths, *, verify_source=True):
    """Read the original task and its latest protections without opening a Worker."""
    for plan in task.plans:
        validate_plan(plan)
        if plan.kind != task.kind or plan.source_root != task.source_root or plan.target != task.target:
            raise ValueError("原始 Album 的媒体类型、来源或 Telegram 目标不一致")
    UploadTask.from_record(task.to_record())
    record = protection_for(task, runtime_paths)
    if record is not None:
        raise ValueError(
            f"仍有未解决的上传记录（{record['status']}，Album {record.get('album_key') or '未知'}）。"
            "请先在“未确认上传”核对。"
        )
    confirmed = _checkpoint_paths(task, runtime_paths)
    if verify_source:
        if not task.source_root.is_dir():
            raise ValueError(f"原始来源目录不可用：{task.source_root}")
        validate_snapshots((item.path, item.snapshot.size, item.snapshot.mtime_ns) for item in task.items)
    return confirmed


def recover_task(task, *, runtime_paths=paths):
    """Record an interrupted run and keep any uncertain delivery blocked."""
    try:
        confirmed = verify_task(task, runtime_paths, verify_source=False)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        return replace(task, status=TaskStatus.NEEDS_REVIEW, finished_at=now(), outcome="RESTARTED",
                       error=f"上次运行的任务需要核对：{exc}")
    return replace(
        task, status=TaskStatus.INTERRUPTED, finished_at=now(), outcome="RESTARTED",
        confirmed_paths=confirmed, error=f"上次运行中断，已核对 {len(confirmed)}/{len(task.items)} 个文件的断点。",
    )
