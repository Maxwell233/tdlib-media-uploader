"""Pure mapping of TDLib send responses and updates to durable states."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..core.models import BatchStatus


PREPARED = BatchStatus.PREPARED.value
SUBMITTED = BatchStatus.SUBMITTED.value
CONFIRMED = BatchStatus.CONFIRMED.value
FAILED = BatchStatus.FAILED.value
UNKNOWN = BatchStatus.UNKNOWN.value


@dataclass(frozen=True, slots=True)
class SendResult:
    """Observed delivery outcome for one single message or Album."""

    status: BatchStatus
    succeeded_ids: tuple[int, ...] = ()
    failed_ids: tuple[int, ...] = ()
    pending_ids: tuple[int, ...] = ()
    error: str | None = None

    @property
    def message_ids(self) -> tuple[int, ...]:
        return self.succeeded_ids + self.failed_ids + self.pending_ids

    @property
    def confirmed_ids(self) -> tuple[int, ...]:
        return self.succeeded_ids

    @property
    def succeeded(self) -> tuple[int, ...]:
        return self.succeeded_ids

    @property
    def failed(self) -> tuple[int, ...]:
        return self.failed_ids

    @property
    def pending(self) -> tuple[int, ...]:
        return self.pending_ids

    def as_dict(self) -> dict[str, object]:
        """Return the legacy result shape for an incremental adapter."""

        result: dict[str, object] = {
            "status": self.status.value,
            "succeeded": list(self.succeeded_ids),
            "failed": list(self.failed_ids),
            "pending": list(self.pending_ids),
        }
        if self.error:
            result["error"] = self.error
        return result

    @property
    def is_resolved(self) -> bool:
        return self.status in {BatchStatus.CONFIRMED, BatchStatus.FAILED}


@dataclass
class _MessageRecord:
    old_id: int | None
    state: str
    resolved_id: int | None


def _as_id(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def message_state(message: Any) -> str:
    """Classify one immediate ``sendMessage`` result."""

    if not isinstance(message, Mapping):
        return "pending"
    sending_state = message.get("sending_state")
    if not sending_state:
        return "succeeded"
    if isinstance(sending_state, Mapping):
        value_type = str(sending_state.get("@type", ""))
        if value_type == "messageSendingStateFailed":
            return "failed"
        if value_type.startswith("messageSendingState"):
            return "pending"
    # An unfamiliar sending state is not evidence of delivery.  Keep it
    # unresolved so a retry cannot create a duplicate.
    return "pending"


def _records(messages: Iterable[Any]) -> list[_MessageRecord]:
    values: list[_MessageRecord] = []
    for message in messages:
        message_id = _as_id(message.get("id")) if isinstance(message, Mapping) else None
        state = message_state(message)
        values.append(
            _MessageRecord(
                old_id=message_id,
                state=state,
                resolved_id=message_id if state == "succeeded" else None,
            )
        )
    return values


def _apply_updates(records: list[_MessageRecord], updates: Iterable[Any]) -> None:
    by_id = {record.old_id: record for record in records if record.old_id is not None}
    for update in updates:
        if not isinstance(update, Mapping):
            continue
        update_type = str(update.get("@type", ""))
        old_id = _as_id(update.get("old_message_id"))
        record = by_id.get(old_id)
        if record is None:
            # A missing old ID cannot safely be attached to an Album item.
            # Leave the original record pending rather than inventing a
            # confirmed message.
            continue
        if update_type == "updateMessageSendSucceeded":
            message = update.get("message")
            new_id = _as_id(message.get("id")) if isinstance(message, Mapping) else None
            record.state = "succeeded"
            record.resolved_id = new_id if new_id is not None else old_id
        elif update_type == "updateMessageSendFailed":
            record.state = "failed"
            record.resolved_id = None


def _ids(records: Iterable[_MessageRecord], state: str) -> tuple[int, ...]:
    result: list[int] = []
    for record in records:
        if record.state != state:
            continue
        value = record.resolved_id if state == "succeeded" else record.old_id
        if value is not None:
            result.append(value)
    return tuple(result)


def status_for_delivery(
    succeeded_count: int,
    failed_count: int,
    pending_count: int,
    expected_count: int,
    *,
    forced_unknown: bool = False,
) -> BatchStatus:
    """Apply V1.9's complete/failed/ambiguous classification rules."""

    if forced_unknown:
        return BatchStatus.UNKNOWN
    if expected_count <= 0:
        return BatchStatus.FAILED
    if succeeded_count == expected_count and failed_count == 0 and pending_count == 0:
        return BatchStatus.CONFIRMED
    if succeeded_count == 0 and failed_count >= expected_count and pending_count == 0:
        return BatchStatus.FAILED
    return BatchStatus.UNKNOWN


def map_send_result(
    messages: Iterable[Any] = (),
    updates: Iterable[Any] = (),
    *,
    expected_count: int | None = None,
    error: str | None = None,
    submitted: bool = False,
    timed_out: bool = False,
    cancelled: bool = False,
    incomplete: bool = False,
) -> SendResult:
    """Map immediate messages plus send updates to a durable send state.

    ``timed_out`` and ``cancelled`` always produce ``UNKNOWN`` because the
    request may have reached Telegram.  ``incomplete`` likewise preserves the
    no-automatic-resend rule for an accepted Album with missing response IDs.
    """

    records = _records(messages)
    _apply_updates(records, updates)
    succeeded = _ids(records, "succeeded")
    failed = _ids(records, "failed")
    pending = _ids(records, "pending")
    expected = len(records) if expected_count is None else max(0, int(expected_count))
    forced_unknown = timed_out or cancelled or incomplete
    status = status_for_delivery(
        len(succeeded),
        len(failed),
        len(pending),
        expected,
        forced_unknown=forced_unknown,
    )
    if error and not forced_unknown and not records:
        status = BatchStatus.UNKNOWN if submitted else BatchStatus.FAILED
    return SendResult(status, succeeded, failed, pending, str(error) if error else None)


def classify_exception(
    error: BaseException,
    *,
    submitted: bool = False,
    terminal: bool = False,
) -> BatchStatus:
    """Classify an exception at the request boundary.

    A timeout/cancellation or any error after submission is ambiguous.  A
    pre-send ordinary failure is ``FAILED``.  ``terminal`` is used when the
    caller already has a complete all-failed result and should not overwrite
    it with ``UNKNOWN``.
    """

    if terminal:
        return BatchStatus.FAILED
    if isinstance(error, TimeoutError) or bool(getattr(error, "cancelled", False)):
        return BatchStatus.UNKNOWN
    if submitted:
        return BatchStatus.UNKNOWN
    return BatchStatus.FAILED


__all__ = [
    "CONFIRMED",
    "FAILED",
    "PREPARED",
    "SendResult",
    "SUBMITTED",
    "UNKNOWN",
    "classify_exception",
    "map_send_result",
    "message_state",
    "status_for_delivery",
]
