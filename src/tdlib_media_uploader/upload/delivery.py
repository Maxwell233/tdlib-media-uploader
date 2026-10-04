"""Normalize transport outcomes without changing checkpoint policy."""
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any
from ..core.models import BatchStatus, UploadBatchResult

def _as_ids(values: Any) -> tuple[int, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        values = (values,)
    result: list[int] = []
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            result.append(int(value))
        except (TypeError, ValueError, OverflowError):
            continue
    return tuple(result)


@dataclass(frozen=True, slots=True)
class _ObservedSend:
    status: BatchStatus
    message_ids: tuple[int, ...] = ()
    succeeded_ids: tuple[int, ...] = ()
    failed_ids: tuple[int, ...] = ()
    pending_ids: tuple[int, ...] = ()
    error: str | None = None


def _normalize_status(value: Any) -> BatchStatus | None:
    if isinstance(value, BatchStatus):
        return value
    if value is None:
        return None
    aliases = {
        "SUCCESS": BatchStatus.CONFIRMED,
        "SUCCEEDED": BatchStatus.CONFIRMED,
        "COMPLETE": BatchStatus.CONFIRMED,
        "COMPLETED": BatchStatus.CONFIRMED,
        "PARTIAL": BatchStatus.UNKNOWN,
        "ERROR": BatchStatus.FAILED,
        "FAIL": BatchStatus.FAILED,
    }
    raw = str(value).strip().upper()
    return aliases.get(raw, BatchStatus._value2member_map_.get(raw))


def _normalize_send_result(value: Any, expected_count: int) -> _ObservedSend:
    succeeded: tuple[int, ...] = ()
    failed: tuple[int, ...] = ()
    pending: tuple[int, ...] = ()
    message_ids: tuple[int, ...] = ()
    error: str | None = None
    status: BatchStatus | None = None

    if isinstance(value, Mapping):
        status = _normalize_status(value.get("status"))
        succeeded = _as_ids(value.get("succeeded_ids", value.get("succeeded")))
        failed = _as_ids(value.get("failed_ids", value.get("failed")))
        pending = _as_ids(value.get("pending_ids", value.get("pending")))
        message_ids = _as_ids(value.get("message_ids"))
        error_value = value.get("error", value.get("message"))
        error = str(error_value) if error_value else None
    elif isinstance(value, UploadBatchResult):
        status = _normalize_status(value.status)
        message_ids = _as_ids(value.message_ids)
        succeeded = message_ids
        error = value.error
    elif value is not None and not isinstance(value, (str, bytes)):
        status = _normalize_status(getattr(value, "status", None))
        succeeded = _as_ids(
            getattr(value, "succeeded_ids", getattr(value, "confirmed_ids", getattr(value, "succeeded", ())))
        )
        failed = _as_ids(getattr(value, "failed_ids", getattr(value, "failed", ())))
        pending = _as_ids(getattr(value, "pending_ids", getattr(value, "pending", ())))
        message_ids = _as_ids(getattr(value, "message_ids", ()))
        error_value = getattr(value, "error", None)
        error = str(error_value) if error_value else None
        if status is None and isinstance(value, Sequence):
            message_ids = _as_ids(value)
            succeeded = message_ids
    elif isinstance(value, (str, bytes)):
        error = str(value)

    if not message_ids:
        message_ids = succeeded + failed + pending
    if status is None:
        if len(succeeded) == expected_count and not failed and not pending and expected_count:
            status = BatchStatus.CONFIRMED
        elif failed and not succeeded and not pending:
            status = BatchStatus.FAILED
        else:
            status = BatchStatus.UNKNOWN

    if status is BatchStatus.CONFIRMED and (failed or pending):
        status = BatchStatus.UNKNOWN
        error = error or "确认结果包含失败或待确认消息"
    if status is BatchStatus.CONFIRMED and len(succeeded or message_ids) != expected_count:
        status = BatchStatus.UNKNOWN
        error = error or "发送器确认的消息数量少于 Album 项数"
    if status is BatchStatus.FAILED and (succeeded or pending):
        status = BatchStatus.UNKNOWN
        error = error or "发送结果同时包含成功或待确认消息"
    if status is BatchStatus.CONFIRMED and not succeeded:
        succeeded = message_ids
    return _ObservedSend(status, message_ids, succeeded, failed, pending, error)


