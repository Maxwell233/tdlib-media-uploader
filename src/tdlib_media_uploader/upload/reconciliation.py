"""Local checkpoint recovery independent of TDLib transport."""

from datetime import datetime
import importlib
from pathlib import Path

from ..core.upload_journal import (
    CORRUPT,
    CONFIRMED,
    InflightJournal,
    _record_target,
    normalize_target,
)


class SourceRootRequired(ValueError):
    """An old journal needs an explicitly selected original source root."""


def has_item_delivery(record: dict) -> bool:
    return record.get("status") != CONFIRMED and bool(
        record.get("item_results") or record.get("succeeded_ids")
        or record.get("failed_ids") or record.get("pending_ids")
        or record.get("reconciled")
    )


def item_delivery(record: dict) -> list[dict]:
    """Keep source positions authoritative; legacy grouped IDs cannot identify files."""
    items = record.get("items", [])
    rows = [{"index": index, "status": "pending", "message_id": None,
             "old_message_id": None} for index in range(len(items))]
    observed = record.get("item_results") or ()
    if not observed:
        return rows
    if len(observed) != len(items):
        raise ValueError("上传日志的文件与消息数量不一致，已保留保护记录")
    seen = set()
    confirmed_ids = set()
    old_ids = set()
    for raw in observed:
        if not isinstance(raw, dict):
            raise ValueError("上传日志中的逐项结果无效")
        index = raw.get("index")
        if type(index) is not int or not 0 <= index < len(rows) or index in seen:
            raise ValueError("上传日志中的文件顺序无效，已保留保护记录")
        status = raw.get("status")
        if status not in {"succeeded", "failed", "pending"}:
            raise ValueError("上传日志中的文件状态无效")
        for field, values in (("old_message_id", old_ids), ("message_id", confirmed_ids)):
            value = raw.get(field)
            if value is not None:
                if type(value) is not int or value == 0 or value in values:
                    raise ValueError("上传日志包含无效或重复的消息 ID，已保留保护记录")
                values.add(value)
        if status == "succeeded" and raw.get("message_id") is None:
            raise ValueError("成功消息缺少最终 ID，已保留保护记录")
        rows[index] = dict(raw)
        seen.add(index)
    return rows


class ReconciliationService:
    def __init__(self, journal=None):
        self.inflight_journal = journal if journal is not None else InflightJournal()

    @staticmethod
    def _target_identity():
        from ..config import loader as cfg
        return normalize_target({key: getattr(cfg, key.upper(), 0)
            for key in ("target_mode", "chat_id", "forum_topic_id", "channel_chat_id")})

    @staticmethod
    def _journal_target(record: dict) -> dict:
        # Keep journal record parsing in one place so legacy v2 records and
        # current canonical targets use identical mode-aware semantics.
        return _record_target(record)

    @staticmethod
    def _journal_items(record: dict) -> list[dict]:
        """Convert both v1 path-only and v2 snapshot records to state items."""

        values = []
        for raw in record.get("items", []) if isinstance(record, dict) else []:
            if isinstance(raw, dict):
                item = dict(raw)
            else:
                item = {"path": raw}
            if not item.get("path"):
                continue
            item["path"] = Path(item["path"])
            # UploadState implementations accept the scanner spelling while
            # journal records use concise snapshot keys.
            if item.get("size") is not None and item.get("scan_size") is None:
                item["scan_size"] = item["size"]
            if item.get("mtime_ns") is not None and item.get("scan_mtime_ns") is None:
                item["scan_mtime_ns"] = item["mtime_ns"]
            # Manual reconciliation must reproduce the exact source identity
            # that was sent.  UploadState therefore prefers these stored
            # values over a fresh stat (the source may have changed or gone
            # offline since the ambiguous request).
            item["_journal_snapshot"] = True
            capture_time = item.get("capture_time")
            if isinstance(capture_time, str) and capture_time:
                try:
                    item["capture_time"] = datetime.fromisoformat(capture_time)
                except ValueError:
                    item["capture_time"] = None
            values.append(item)
        return values

    @classmethod
    def _snapshot_items(cls, record: dict) -> list[dict]:
        items = cls._journal_items(record)
        if len(items) != len(record.get("items", [])):
            raise RuntimeError("上传日志包含缺失路径的文件，已保留保护记录")
        if not items:
            raise RuntimeError("上传日志缺少源文件快照，已保留保护记录")
        missing = [
            str(item.get("path"))
            for item in items
            if item.get("scan_size") is None or item.get("scan_mtime_ns") is None
        ]
        if missing:
            raise RuntimeError(
                "上传日志缺少完整文件快照，已保留保护记录：" + "、".join(missing)
            )
        return items

    def _state_for_journal(self, kind: str, record: dict, *, fallback_target=None, source_root=None):
        """Create the matching uploader state without touching source files.

        A target recorded with the send attempt is authoritative.  Legacy
        target-less records use an explicitly supplied fallback, then the
        media-kind-specific configured target, and only lastly the historical
        mutable global target values.
        """

        from ..config import loader as cfg
        target = self._journal_target(record)
        if not target and fallback_target is not None:
            target = normalize_target(fallback_target)
        if not target:
            target_for = getattr(cfg, "target_for", None)
            if callable(target_for):
                try:
                    target = normalize_target(target_for(kind))
                except Exception:
                    target = {}
        if not target:
            target = self._target_identity()
        if kind == "video":
            module = importlib.import_module("tdlib_media_uploader.media.video_pipeline")
        elif kind == "image":
            module = importlib.import_module("tdlib_media_uploader.media.image_pipeline")
        elif kind == "mixed":
            module = importlib.import_module("tdlib_media_uploader.media.mixed_pipeline")
        else:
            raise ValueError(f"无法为未知媒体类型恢复上传断点：{kind}")
        roots = {str(item.get("source_root")) for item in record.get("items", ())
                 if isinstance(item, dict) and item.get("source_root")}
        if record.get("source_root"):
            roots.add(str(record["source_root"]))
        if len(roots) > 1:
            raise ValueError("上传记录包含多个来源目录，已保留保护记录")
        root = next(iter(roots), None) or source_root
        if root is None:
            raise SourceRootRequired("旧记录缺少来源目录，请选择当时扫描的根目录；不会使用当前配置猜测")
        from ..core.filesystem import stable_path
        normalized_root = Path(stable_path(root))
        if any(not Path(stable_path(item["path"])).is_relative_to(normalized_root)
               for item in record.get("items", ())
               if isinstance(item, dict) and item.get("path")):
            raise ValueError("来源目录不包含上传记录中的文件，已保留保护记录")
        from ..core.upload_state import UploadState
        return UploadState(kind=kind, source_root=Path(root), target=target,
                           state_dir=module.STATE_DIR, reset=False)

    def reconcile_inflight(
        self,
        album_key: str,
        *,
        sent: bool,
        kind: str | None = None,
        message_ids=None,
        target=None,
        source_root=None,
        item_decisions=None,
    ) -> None:
        """Manually resolve an UNKNOWN send without querying Telegram history."""

        requested_target = normalize_target(target)
        effective_target = requested_target or self._target_identity()
        selected_kind = kind or self._journal_kind_for(album_key, target=effective_target)
        _path, record = self.inflight_journal.get_entry(selected_kind, album_key, effective_target)
        if record is None:
            raise RuntimeError(f"未找到未确认上传记录：{selected_kind} / {album_key}")
        if record.get("status") == CORRUPT:
            raise RuntimeError("损坏的上传记录，已保留保护")
        if record.get("status") == CONFIRMED and not sent:
            raise ValueError("Telegram 已确认，不能标记为未发送")
        if record.get("status") == CONFIRMED and item_decisions is not None:
            if not isinstance(item_decisions, dict) or any(value != "sent" for value in item_decisions.values()):
                raise ValueError("Telegram 已确认的批次只能修复本地断点")
            item_decisions = None
        if has_item_delivery(record) or item_decisions is not None:
            if not sent and item_decisions is None:
                raise ValueError("此批次包含逐项发送结果，请逐项核对，不能整组重新发送")
            if sent and item_decisions is None and len(record.get("items", [])) == 1:
                # A manual assertion for a single source has no positional ambiguity.
                item_decisions = {0: "sent"}
            self._reconcile_items(selected_kind, album_key, record,
                                  target=effective_target, source_root=source_root,
                                  decisions=item_decisions)
            return
        if not sent:
            self.inflight_journal.mark_not_sent(selected_kind, album_key, target=effective_target)
            return
        items = self._snapshot_items(record)
        ids = list(message_ids if message_ids is not None else record.get("message_ids", []))
        state = self._state_for_journal(
            selected_kind,
            record,
            fallback_target=requested_target or None,
            source_root=source_root,
        )
        # ``mark_album_completed`` performs an atomic fsync-backed save.  Do
        # not touch the journal until it returns; a save failure therefore
        # leaves the UNKNOWN/SUBMITTED record blocking automatic resends.
        state.mark_album_completed(items, ids)
        self.inflight_journal.mark_confirmed(
            selected_kind,
            album_key,
            ids,
            target=effective_target,
        )
        self.inflight_journal.finalize(selected_kind, album_key, target=effective_target)

    def _reconcile_items(self, kind, album_key, record, *, target, source_root, decisions):
        rows = item_delivery(record)
        choices = decisions or {}
        if not isinstance(choices, dict):
            raise ValueError("逐项核对结果必须按文件编号记录")
        for index, choice in choices.items():
            if type(index) is not int or not 0 <= index < len(rows) or choice not in {"sent", "not_sent"}:
                raise ValueError("逐项核对结果包含无效的文件编号或操作")
            if choice == "not_sent" and rows[index]["status"] == "succeeded":
                raise ValueError("Telegram 已确认成功的文件不能允许重新发送")
        succeeded, ids, remaining, remaining_results = [], [], [], []
        snapshots = None
        for row in rows:
            index = row["index"]
            status = row["status"]
            choice = choices.get(index)
            if choice == "sent" or status == "succeeded":
                if snapshots is None:
                    snapshots = self._snapshot_items(record)
                succeeded.append(snapshots[index])
                ids.append(row.get("message_id"))
            elif choice == "not_sent" or status == "failed":
                continue
            else:
                remaining.append(record["items"][index])
                remaining_results.append({**row, "index": len(remaining_results)})
        if succeeded:
            state = self._state_for_journal(kind, record, fallback_target=target or None,
                                            source_root=source_root)
            # A failed checkpoint must leave every original protection intact.
            state.mark_album_completed(succeeded, ids)
        if remaining:
            pending_ids = [row.get("old_message_id") for row in remaining_results
                           if row.get("old_message_id") is not None]
            self.inflight_journal.update(
                kind, album_key, "UNKNOWN", items=remaining, item_results=remaining_results,
                message_ids=pending_ids, succeeded_ids=[], failed_ids=[], pending_ids=pending_ids,
                reconciled=True, target=target,
            )
        else:
            self.inflight_journal.finalize(kind, album_key, target=target)

    def recover_confirmed(
        self,
        kind: str,
        album_key: str,
        *,
        target=None,
        state=None,
        record: dict | None = None,
    ) -> bool:
        """Repair a CONFIRMED checkpoint and only then remove its journal.

        The caller may provide the already configured UploadState used by the
        current run.  No source stat or Telegram transport is consulted; the
        journal snapshot is the authoritative identity for this repair.
        """

        requested_target = normalize_target(target)
        current = record
        if current is None:
            _path, current = self.inflight_journal.get_entry(kind, album_key, requested_target)
        if current is None:
            return False
        if current.get("status") == CORRUPT:
            raise RuntimeError(f"损坏的上传记录：{current.get('journal_path')}")
        if current.get("status") != CONFIRMED:
            return False
        recorded_target = self._journal_target(current)
        if requested_target and recorded_target and requested_target != recorded_target:
            raise RuntimeError("CONFIRMED 上传记录的 Telegram 目标与当前目标不一致")
        effective_target = recorded_target or requested_target
        items = self._snapshot_items(current)
        ids = list(current.get("message_ids", []))
        checkpoint = state or self._state_for_journal(
            kind,
            current,
            fallback_target=effective_target or None,
        )
        # The journal remains present until this durable write returns.
        checkpoint.mark_album_completed(items, ids)
        self.inflight_journal.finalize(kind, album_key, target=effective_target)
        return True

    def _journal_kind_for(self, album_key: str, *, target=None) -> str:
        record = self.inflight_journal.find_album(album_key, target=target)
        if record:
            value = str(record.get("kind", "")).strip().lower()
            if value:
                return value
        return "unknown"
