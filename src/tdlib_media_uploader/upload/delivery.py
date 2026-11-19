"""Normalize transport outcomes without changing checkpoint policy."""
import math
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any
from ..core.models import BatchStatus, UploadBatchResult
from ..telegram.send_result import SendRequestRejected

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
    item_results: tuple[Mapping[str, Any], ...] = ()
    preserve_journal: bool = False
    request_rejection: SendRequestRejected | None = None

    def album_retry_delay(self, expected_count: int, *, complete_album=True) -> float | None:
        """Require a distinct, terminal and transient failure for every item."""
        if self.status is not BatchStatus.FAILED or self.succeeded_ids or self.pending_ids:
            return None
        rejection = self.request_rejection
        if rejection is not None:
            if rejection.rate_limited and not (self.message_ids or self.failed_ids or self.item_results):
                return rejection.retry_after
            return None
        if (not complete_album or not expected_count
                or len(self.failed_ids) != expected_count or len(self.item_results) != expected_count
                or not all(self.failed_ids) or len(set(self.failed_ids)) != expected_count):
            return None
        ids = []
        delays = []
        for index, row in enumerate(self.item_results):
            if (not isinstance(row, Mapping) or row.get("index") != index
                    or row.get("status") != "failed" or row.get("requires_action") is not False):
                return None
            code = row.get("error_code")
            if type(code) is not int or not (
                code in {420, 429} or 500 <= code <= 599 or (code == 0 and row.get("can_retry") is True)
            ):
                return None
            delay = row.get("retry_after")
            if type(delay) not in {int, float} or not math.isfinite(delay) or delay < 0:
                return None
            ids.append(row.get("message_id") or row.get("old_message_id"))
            delays.append(delay)
        return max(delays) if tuple(ids) == self.failed_ids else None


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
    item_results = ()

    if isinstance(value, Mapping):
        item_results = tuple(value.get("item_results") or ())
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
        item_results = tuple(getattr(value, "item_results", ()) or ())
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
    if status is BatchStatus.CONFIRMED and len(set(succeeded or message_ids)) != expected_count:
        status = BatchStatus.UNKNOWN
        error = error or "发送器确认结果包含重复消息 ID"
    if status is BatchStatus.FAILED and (succeeded or pending or (
        failed and (len(failed) != expected_count or len(set(failed)) != expected_count or not all(failed))
    )):
        status = BatchStatus.UNKNOWN
        error = error or "发送结果同时包含成功或待确认消息"
    if status is BatchStatus.CONFIRMED and not succeeded:
        succeeded = message_ids
    return _ObservedSend(status, message_ids, succeeded, failed, pending, error, item_results)
