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
from ..core.models import AlbumPlan, BatchStatus, FileSnapshot, MediaItem, ScanResult
from .retry import AlbumRetryPolicy

KINDS = {"video", "image", "mixed"}


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    SKIPPED = "SKIPPED"


ACTIVE = {TaskStatus.PREPARING, TaskStatus.RUNNING}
RECOVERABLE = {TaskStatus.FAILED, TaskStatus.INTERRUPTED}
ACTION_REQUIRED = RECOVERABLE | {TaskStatus.NEEDS_REVIEW}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _runtime_setting(name: str) -> bool:
    # Credentials and account/database settings belong to the application.
    return name == "CONFIG" or name.startswith(("API_", "TDLIB_", "PROXY_"))


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
        for plan in preview["plans"]:
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
            if name.isupper() and name != "CONFIG" and _runtime_setting(name)
        }
        return FrozenConfig(**runtime, **self.config)

    def preview(self) -> dict:
        return {
            "source_dir": str(self.source_root), "target": self.target, "items": self.items,
            "plans": self.plans, "scan_result": ScanResult(self.items, errors=self.scan_errors),
            "album_count": len(self.plans), "queued": True,
            "known_confirmed": self.known_confirmed,
        }

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
        task = cls(task_id, kind, root, target, config, plans,
                   raw["created_at"], TaskStatus(raw["status"]), raw["started_at"],
                   raw["finished_at"], raw["outcome"], raw["error"], tuple(raw["scan_errors"]),
                   tuple(confirmed))
        digest = cls._digest(task._manifest())
        if "manifest_digest" in raw and raw["manifest_digest"] != digest:
            raise ValueError("原始任务清单已变化，Album 成员、顺序、目标与配置必须保持一致")
        return replace(task, manifest_digest=digest)


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
    return replace(task, status=status, finished_at=now(), outcome=result.status, error=result.error,
                   confirmed_paths=tuple(sorted(confirmed)))
