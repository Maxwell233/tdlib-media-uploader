"""Immutable upload manifests and the persisted task lifecycle."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

from ..config.snapshot import FrozenConfig, snapshot_config
from ..core.album import with_filename_description
from ..core.filesystem import stable_path
from ..core.identity import validate_send_target
from ..core.models import AlbumPlan, BatchStatus, FAILURE_STAGES, FileSnapshot, MediaItem, ScanResult
from .retry import AlbumRetryPolicy

KINDS = {"video", "image", "mixed"}
_TDLIB_JOB_SETTINGS = {
    "TDLIB_REQUEST_TIMEOUT", "TDLIB_MESSAGE_SEND_TIMEOUT", "TDLIB_ENABLE_PRELIMINARY_UPLOAD",
    "TDLIB_MAX_CONCURRENT_FILE_UPLOADS", "TDLIB_PRELIMINARY_WAIT_SECONDS",
}


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    SKIPPED = "SKIPPED"


class AlbumStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class AlbumOutcome:
    album_key: str
    status: AlbumStatus
    error: str | None = None
    failed_paths: tuple[str, ...] = ()
    deferred_paths: tuple[str, ...] = ()
    failure_stage: str | None = None

    def to_record(self):
        record = {
            "album_key": self.album_key, "status": self.status.value, "error": self.error,
            "failed_paths": list(self.failed_paths), "deferred_paths": list(self.deferred_paths),
        }
        if self.failure_stage is not None:
            record["failure_stage"] = self.failure_stage
        return record


ACTIVE = {TaskStatus.PREPARING, TaskStatus.RUNNING}
RECOVERABLE = {TaskStatus.FAILED, TaskStatus.INTERRUPTED}
ACTION_REQUIRED = RECOVERABLE | {TaskStatus.NEEDS_REVIEW}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _runtime_setting(name: str) -> bool:
    # Credentials and account/database settings belong to the application.
    return name == "CONFIG" or (name not in _TDLIB_JOB_SETTINGS and name.startswith(("API_", "TDLIB_", "PROXY_")))


def _encode(value):
    if isinstance(value, Path):
        return {"$path": str(value)}
    if isinstance(value, (set, frozenset)):
        return {"$set": sorted(value)}
    if isinstance(value, Mapping):
        return {key: _encode(entry) for key, entry in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(entry) for entry in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"无法保存任务快照中的 {type(value).__name__}")


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {"$path"}:
            return Path(value["$path"])
        if set(value) == {"$set"}:
            return frozenset(value["$set"])
        return MappingProxyType({key: _decode(entry) for key, entry in value.items()})
    if isinstance(value, list):
        return tuple(_decode(entry) for entry in value)
    return value


def _item_record(item: MediaItem) -> dict:
    return {
        "path": str(item.path),
        "media_kind": item.media_kind,
        "size": item.snapshot.size,
        "mtime_ns": item.snapshot.mtime_ns,
        "capture_time": item.capture_time.isoformat() if item.capture_time else None,
        "group_name": item.group_name,
        "month_key": item.month_key,
        "date_tag": item.date_tag,
        "fallback": item.fallback,
        "metadata": _encode(item.metadata),
    }


def _read_item(raw: dict, root: Path) -> MediaItem:
    path = Path(raw["path"])
    normalized_path, normalized_root = Path(stable_path(path)), Path(stable_path(root))
    if (not path.is_absolute() or not normalized_path.is_relative_to(normalized_root)
            or normalized_path == normalized_root or raw["media_kind"] not in {"video", "image"}):
        raise ValueError("任务包含无效的媒体路径或类型")
    size, mtime_ns = raw["size"], raw["mtime_ns"]
    if (type(size) is not int or size <= 0 or type(mtime_ns) is not int
            or not isinstance(raw["metadata"], dict) or type(raw["fallback"]) is not bool):
        raise ValueError("任务包含无效的文件快照")
    return MediaItem(
        path, root, raw["media_kind"], FileSnapshot(str(path), size, mtime_ns),
        datetime.fromisoformat(raw["capture_time"]) if raw["capture_time"] else None,
        raw["group_name"], raw["month_key"], raw["date_tag"], raw["fallback"],
        _decode(raw["metadata"]),
    )


def _plan_record(plan: AlbumPlan) -> dict:
    pending = {str(item.path) for item in plan.pending_items}
    return {
        "key": plan.key,
        "group_label": plan.group_label,
        "number": plan.number,
        "caption": plan.caption,
        "items": [_item_record(item) for item in plan.items],
        "pending": [index for index, item in enumerate(plan.items) if str(item.path) in pending],
    }


def _read_plan(raw, kind, root, target) -> AlbumPlan:
    items = tuple(_read_item(entry, root) for entry in raw["items"])
    indices = raw["pending"]
    if (
        not 1 <= len(items) <= 10
        or not isinstance(raw["key"], str) or not raw["key"]
        or not isinstance(raw["caption"], str)
        or not isinstance(raw["group_label"], str) or type(raw["number"]) is not int
        or not isinstance(indices, list)
        or any(type(index) is not int or not 0 <= index < len(items) for index in indices)
        or sorted(set(indices)) != indices
        or any(kind != "mixed" and item.media_kind != kind for item in items)
    ):
        raise ValueError("任务包含无效的 Album")
    return AlbumPlan(raw["key"], kind, root, raw["group_label"], raw["number"],
                     items, tuple(items[index] for index in indices), raw["caption"], target)


@dataclass(frozen=True, slots=True)
class UploadTask:
    task_id: str
    kind: str
    source_root: Path
    target: Mapping
    config: Mapping
    plans: tuple[AlbumPlan, ...]
    created_at: str
    status: TaskStatus = TaskStatus.PENDING
    started_at: str | None = None
    finished_at: str | None = None
    outcome: str | None = None
    error: str | None = None
    scan_errors: tuple[str, ...] = ()
    confirmed_paths: tuple[str, ...] = ()
    manifest_digest: str | None = None
    album_outcomes: tuple[AlbumOutcome, ...] = ()
    skipped_albums: tuple[str, ...] = ()
    retry_album_key: str | None = None
    paused_by_policy: bool = False

    @classmethod
    def from_preview(cls, kind, preview, config):
        if preview.get("cancelled") or not preview.get("plans"):
            raise ValueError("请先完成扫描")
        captions = {
            entry["key"]: entry["caption"]["text"]
            for group in preview["groups"] for entry in group["album_plans"]
        }
        captions.update(preview.get("caption_overrides", {}))
        target = validate_send_target(dict(preview["target"]))
        settings = {
            name: value for name, value in vars(snapshot_config(config)).items()
            if name.isupper() and not _runtime_setting(name)
        }
        final_plans = []
        skipped = set(preview.get("skipped_album_keys", ()))
        for plan in preview["plans"]:
            if plan.key in skipped:
                continue
            base = captions.get(plan.key, plan.caption)
            caption = with_filename_description(
                base, [{"path": item.path} for item in plan.pending_items],
                bool(settings.get(f"{kind.upper()}_CAPTION_INCLUDE_FILENAMES", False)),
                bool(settings.get(f"{kind.upper()}_CAPTION_INCLUDE_FILENAME_NUMBERS", True)),
                max_chars=max(1024, len(base)),
            )
            final_plans.append(replace(plan, caption=caption))
        task = cls(
            str(uuid4()), kind, Path(preview["source_dir"]).absolute(), target, settings,
            tuple(final_plans),
            now(), scan_errors=tuple(preview.get("scan_errors", ())),
        )
        # Apply the same schema checks and deep freeze used after restart.
        return cls.from_record(task.to_record())

    @property
    def items(self):
        return tuple(item for plan in self.plans for item in plan.items)

    @property
    def known_confirmed(self) -> set[str]:
        known = set(self.confirmed_paths)
        for plan in self.plans:
            pending = {stable_path(item.path) for item in plan.pending_items}
            known.update(stable_path(item.path) for item in plan.items if stable_path(item.path) not in pending)
        return known

    @property
    def total_bytes(self):
        return sum(item.snapshot.size for item in self.items)

    def overlaps(self, other) -> bool:
        return (
            self.kind == other.kind
            and stable_path(self.source_root) == stable_path(other.source_root)
            and self.target == other.target
            and not {
                stable_path(item.path) for plan in self.plans for item in plan.pending_items
            }.isdisjoint(
                stable_path(item.path) for plan in other.plans for item in plan.pending_items
            )
        )

    def execution_config(self, application_config):
        runtime = {
            name: getattr(application_config, name) for name in dir(application_config)
            if name.isupper() and name != "CONFIG" and (_runtime_setting(name) or name in _TDLIB_JOB_SETTINGS)
        }
        runtime.update(self.config)
        return FrozenConfig(**runtime)

    def album_retry_policy(
        self, album_key: str, *, confirmed: set[str] | None = None
    ) -> tuple[str, tuple[str, ...]]:
        """Return ``(scope, target_paths)`` for retrying ``album_key`` manually."""
        if album_key in self.skipped_albums:
            return "none", ()
        plan = next((entry for entry in self.plans if entry.key == album_key), None)
        if plan is None:
            return "none", ()
        known = self.known_confirmed if confirmed is None else (set(confirmed) | self.known_confirmed)
        outcome = self._album_statuses(known).get(album_key)
        if outcome is None:
            return "none", ()
        if outcome.status is AlbumStatus.UNKNOWN or outcome.failure_stage in {"unknown", "persistence"}:
            return "review_required", ()
        unconfirmed = tuple(
            stable_path(item.path) for item in plan.pending_items if stable_path(item.path) not in known
        )
        if not unconfirmed:
            return "none", ()
        if outcome.status not in {AlbumStatus.FAILED, AlbumStatus.PARTIAL}:
            return "none", ()
        if outcome.failure_stage in {"pre_submit", "request_rejected", "send_failed"}:
            return "album", unconfirmed
        unconfirmed_set = set(unconfirmed)
        failed_set = set(outcome.failed_paths) & unconfirmed_set
        if outcome.failure_stage == "partial_delivery":
            explicit = tuple(path for path in unconfirmed if path in failed_set)
            return ("failed_files", explicit) if explicit else ("review_required", ())
        # Legacy record missing failure_stage: refuse to split an unsubmitted Album safely.
        deferred_set = set(outcome.deferred_paths) & unconfirmed_set
        missing = unconfirmed_set - failed_set - deferred_set
        if missing or not failed_set:
            return "review_required", ()
        if deferred_set or outcome.status is AlbumStatus.PARTIAL:
            return "failed_files", tuple(path for path in unconfirmed if path in failed_set)
        return "album", unconfirmed

    def execution_plans(self):
        plans = tuple(plan for plan in self.plans if plan.key not in self.skipped_albums)
        if self.retry_album_key is not None:
            plan = next(plan for plan in plans if plan.key == self.retry_album_key)
            mode, allowed = self.album_retry_policy(plan.key)
            if mode not in {"album", "failed_files"} or not allowed:
                raise ValueError("当前 Album 状态不支持选择性重试，请先人工核对或使用整任务恢复")
            allowed_set = set(allowed)
            plans = (replace(plan, pending_items=tuple(
                item for item in plan.pending_items if stable_path(item.path) in allowed_set
            )),)
        return plans

    def preview(self) -> dict:
        plans = self.execution_plans()
        items = self.items
        progress_items = items
        if self.retry_album_key is not None or self.skipped_albums:
            progress_items = tuple(item for plan in plans for item in plan.pending_items)
        return {
            "source_dir": str(self.source_root), "target": self.target, "items": progress_items,
            "plans": plans, "scan_result": ScanResult(items, errors=self.scan_errors),
            "album_count": len(plans), "queued": True,
            "known_confirmed": self.known_confirmed,
        }

    def album_statuses(self) -> dict[str, AlbumOutcome]:
        confirmed = self.known_confirmed
        return self._album_statuses(confirmed)

    def _album_statuses(self, confirmed):
        observed = {outcome.album_key: outcome for outcome in self.album_outcomes}
        skipped = set(self.skipped_albums)
        statuses = {}
        for plan in self.plans:
            if plan.key in skipped:
                statuses[plan.key] = AlbumOutcome(plan.key, AlbumStatus.SKIPPED)
            elif plan.key in observed and observed[plan.key].status is AlbumStatus.UNKNOWN:
                statuses[plan.key] = observed[plan.key]
            elif all(stable_path(item.path) in confirmed for item in plan.items):
                statuses[plan.key] = AlbumOutcome(plan.key, AlbumStatus.COMPLETED)
            else:
                statuses[plan.key] = observed.get(plan.key, AlbumOutcome(plan.key, AlbumStatus.PENDING))
        return statuses

    def _manifest(self) -> dict:
        return {
            "task_id": self.task_id, "kind": self.kind, "source_root": str(self.source_root),
            "target": dict(self.target), "config": _encode(self.config),
            "plans": [_plan_record(plan) for plan in self.plans],
            "created_at": self.created_at, "scan_errors": list(self.scan_errors),
        }

    @staticmethod
    def _digest(manifest) -> str:
        data = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(data.encode("utf-8")).hexdigest()

    def to_record(self) -> dict:
        manifest = self._manifest()
        return {
            **manifest, "manifest_digest": self.manifest_digest or self._digest(manifest),
            "started_at": self.started_at, "finished_at": self.finished_at,
            "status": self.status.value, "outcome": self.outcome, "error": self.error,
            "confirmed_paths": list(self.confirmed_paths),
            "album_outcomes": [outcome.to_record() for outcome in self.album_outcomes],
            "skipped_albums": list(self.skipped_albums), "retry_album_key": self.retry_album_key,
            "paused_by_policy": self.paused_by_policy,
        }

    @classmethod
    def from_record(cls, raw):
        task_id = str(UUID(raw["task_id"]))
        kind, root = raw["kind"], Path(raw["source_root"])
        if kind not in KINDS or not root.is_absolute():
            raise ValueError("任务包含无效的来源目录或媒体类型")
        target = MappingProxyType(validate_send_target(raw["target"]))
        plans = tuple(_read_plan(plan, kind, root, target) for plan in raw["plans"])
        paths = [stable_path(item.path) for plan in plans for item in plan.items]
        if not plans or len(set(paths)) != len(paths) or len({plan.key for plan in plans}) != len(plans):
            raise ValueError("任务包含空计划、重复文件或重复 Album")
        config = _decode(raw["config"])
        if not isinstance(config, Mapping) or any(not key.isupper() or _runtime_setting(key) for key in config):
            raise ValueError("任务配置格式无效")
        AlbumRetryPolicy.from_config(FrozenConfig(**config))
        if not isinstance(raw["created_at"], str) or not isinstance(raw["scan_errors"], list):
            raise ValueError("任务缺少有效的创建时间或扫描诊断")
        for key in ("created_at", "started_at", "finished_at"):
            if raw[key] is not None:
                datetime.fromisoformat(raw[key])
        confirmed = raw.get("confirmed_paths", [])
        if (not isinstance(confirmed, list) or not all(isinstance(path, str) for path in confirmed)
                or len(set(confirmed)) != len(confirmed) or not set(confirmed).issubset(paths)):
            raise ValueError("任务中已确认的文件不属于原始清单")
        pending_by_album = {
            plan.key: {stable_path(item.path) for item in plan.pending_items}
            for plan in plans
        }
        outcomes = []
        for entry in raw.get("album_outcomes", ()):
            album_key = entry["album_key"]
            failed = tuple(entry.get("failed_paths", ()))
            deferred = tuple(entry.get("deferred_paths", ()))
            failure_stage = entry.get("failure_stage")
            if failure_stage is not None and failure_stage not in FAILURE_STAGES:
                raise ValueError("Album 失败阶段无效")
            if (album_key not in pending_by_album
                    or any(outcome.album_key == album_key for outcome in outcomes)
                    or not set((*failed, *deferred)).issubset(pending_by_album[album_key])
                    or set(failed).intersection(deferred)):
                raise ValueError("Album 状态不属于任务的原始待上传清单")
            outcomes.append(
                AlbumOutcome(
                    album_key, AlbumStatus(entry["status"]), entry.get("error"),
                    failed, deferred, failure_stage=failure_stage,
                )
            )
        skipped = tuple(raw.get("skipped_albums", ()))
        retry = raw.get("retry_album_key")
        paused_by_policy = raw.get("paused_by_policy", False)
        if type(paused_by_policy) is not bool:
            raise ValueError("无效的队列暂停状态")
        if len(set(skipped)) != len(skipped) or not set(skipped).issubset(pending_by_album):
            raise ValueError("Album 跳过选择不属于原始任务")
        task = cls(task_id, kind, root, target, config, plans,
                   raw["created_at"], TaskStatus(raw["status"]), raw["started_at"],
                   raw["finished_at"], raw["outcome"], raw["error"], tuple(raw["scan_errors"]),
                   tuple(confirmed), album_outcomes=tuple(outcomes), skipped_albums=skipped,
                   retry_album_key=retry, paused_by_policy=paused_by_policy)
        if retry is not None and (
            retry in skipped or task.album_retry_policy(retry)[0] not in {"album", "failed_files"}
        ):
            raise ValueError("Album 跳过或重试选择不属于原始任务")
        digest = cls._digest(task._manifest())
        if "manifest_digest" in raw and raw["manifest_digest"] != digest:
            raise ValueError("原始任务清单已变化，Album 成员、顺序、目标与配置必须保持一致")
        return replace(task, manifest_digest=digest)


def skip_album(task, album_key, confirmed_paths):
    task = replace(task, confirmed_paths=confirmed_paths)
    outcome = task.album_statuses().get(album_key)
    if outcome is None or outcome.status in {AlbumStatus.COMPLETED, AlbumStatus.UNKNOWN, AlbumStatus.SKIPPED}:
        raise ValueError("此 Album 不能跳过")
    updated = replace(task, skipped_albums=(*task.skipped_albums, album_key), retry_album_key=None)
    statuses = {entry.status for entry in updated.album_statuses().values()}
    if statuses <= {AlbumStatus.COMPLETED, AlbumStatus.SKIPPED}:
        status = TaskStatus.COMPLETED if AlbumStatus.COMPLETED in statuses else TaskStatus.SKIPPED
        return replace(updated, status=status, finished_at=now(), outcome="SKIPPED",
                       error=None, paused_by_policy=False)
    if statuses.isdisjoint({AlbumStatus.FAILED, AlbumStatus.PARTIAL, AlbumStatus.UNKNOWN}):
        return replace(updated, status=TaskStatus.PENDING, started_at=None, finished_at=None,
                       outcome=None, error=None, paused_by_policy=False)
    return updated


def retry_failed_album(task, album_key, confirmed_paths):
    task = replace(task, confirmed_paths=confirmed_paths)
    scope, _ = task.album_retry_policy(album_key)
    if scope == "review_required":
        raise ValueError("此 Album 需要人工核对或使用整任务恢复，不能直接执行局部重试")
    if scope not in {"album", "failed_files"}:
        raise ValueError("此 Album 没有已明确失败且仍可重试的文件")
    return replace(task, status=TaskStatus.PENDING, retry_album_key=album_key,
                   started_at=None, finished_at=None, outcome=None, error=None, paused_by_policy=False)


def album_outcome_from_result(plan, batch, confirmed, failed_paths, deferred_paths, *, retry_prior=None, error=None):
    """Project one engine result onto the original Album's queue status."""
    paths = {stable_path(item.path) for item in plan.pending_items}
    failed = paths & failed_paths
    deferred = paths & deferred_paths
    if batch is None and not (failed or deferred):
        return None

    stage = None
    if batch is not None:
        deferred.update(stable_path(item.path) for item in batch.deferred_items)
        if batch.status is BatchStatus.FAILED and not (failed or deferred):
            failed = (set(retry_prior.failed_paths) if retry_prior is not None else paths) - confirmed
        if batch.failure_stage is not None:
            stage = batch.failure_stage
        elif batch.status is BatchStatus.CONFIRMED and batch.error:
            stage = "persistence"
        elif batch.status in {BatchStatus.PREPARED, BatchStatus.SUBMITTED, BatchStatus.UNKNOWN}:
            stage = "unknown"
        elif retry_prior is not None:
            stage = retry_prior.failure_stage
    if retry_prior is not None:
        deferred.update(set(retry_prior.deferred_paths) - failed - confirmed)

    if batch is not None and (
        batch.status in {BatchStatus.PREPARED, BatchStatus.SUBMITTED, BatchStatus.UNKNOWN}
        or batch.status is BatchStatus.CONFIRMED and batch.error
    ):
        status = AlbumStatus.UNKNOWN
    elif paths <= confirmed:
        status = AlbumStatus.COMPLETED
        stage = None
    elif paths & confirmed:
        status = AlbumStatus.PARTIAL
    elif failed:
        status = AlbumStatus.FAILED
    else:
        status = AlbumStatus.DEFERRED
    return AlbumOutcome(
        plan.key, status, batch.error if batch is not None else error,
        tuple(sorted(failed - deferred - confirmed)), tuple(sorted(deferred - confirmed)),
        failure_stage=stage,
    )


def _run_album_outcomes(task, result, confirmed):
    outcomes = {outcome.album_key: outcome for outcome in task.album_outcomes}
    batches = {batch.album_key.split(":retry:", 1)[0]: batch for batch in result.batches}
    failed_paths = {stable_path(item.path) for item in result.failed_items}
    deferred_paths = {stable_path(item.path) for item in result.deferred_items}
    for plan in task.plans:
        outcome = album_outcome_from_result(
            plan, batches.get(plan.key), confirmed, failed_paths, deferred_paths,
            retry_prior=outcomes.get(plan.key) if task.retry_album_key == plan.key else None,
            error=result.error,
        )
        if outcome is not None:
            outcomes[plan.key] = outcome
    return tuple(outcomes.values())


def finish_task(task: UploadTask, result) -> UploadTask:
    if result.status == "UNKNOWN" or any(
        batch.status is BatchStatus.CONFIRMED and batch.error for batch in result.batches
    ):
        status = TaskStatus.NEEDS_REVIEW
    elif result.cancelled or result.status == "CANCELLED":
        status = TaskStatus.INTERRUPTED
    elif result.status == "COMPLETED":
        status = TaskStatus.COMPLETED
    else:
        status = TaskStatus.FAILED
    confirmed = set(task.confirmed_paths)
    confirmed.update(stable_path(item.path) for item in result.confirmed_before_run)
    confirmed.update(
        stable_path(item.path) for batch in result.batches
        if batch.status is BatchStatus.CONFIRMED for item in batch.confirmed_items
    )
    updated = replace(
        task, status=status, finished_at=now(), outcome=result.status, error=result.error,
        confirmed_paths=tuple(sorted(confirmed)), album_outcomes=_run_album_outcomes(task, result, confirmed),
        retry_album_key=None, paused_by_policy=result.paused,
    )
    if task.retry_album_key is not None and (
        status is TaskStatus.INTERRUPTED or status is TaskStatus.FAILED and result.paused
    ) and updated.album_retry_policy(task.retry_album_key, confirmed=confirmed)[0] in {"album", "failed_files"}:
        return replace(updated, retry_album_key=task.retry_album_key)
    if task.retry_album_key is not None and status is TaskStatus.COMPLETED and any(
        album.status not in {AlbumStatus.COMPLETED, AlbumStatus.SKIPPED}
        for album in updated._album_statuses(confirmed).values()
    ):
        return replace(
            updated,
            status=TaskStatus.FAILED,
            outcome="PARTIAL",
            error="任务仍有未完成的 Album。",
            paused_by_policy=True,
        )
    return updated
