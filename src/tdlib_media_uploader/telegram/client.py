"""GUI-free, transport-injected boundary around TDLib JSON requests.

The production V1.9 client talks to the native ``tdjson`` binding directly.
V2 keeps that binding behind this small protocol: tests and future adapters
provide ``send``/``receive`` without importing Qt, GUI modules or UploadEngine.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import time
import uuid
from typing import Any, Protocol

from .auth import AuthTransition, authorization_transition
from ..core.models import BatchStatus
from .limits import validate_contents_captions
from .send_result import SendResult, map_send_result
from .target import TelegramTarget, build_topic_object, parse_target


class TDLibError(RuntimeError):
    """An error object returned by TDLib."""

    def __init__(self, code: int, message: str):
        self.code = int(code)
        self.message = str(message)
        super().__init__(f"TDLib error {self.code}: {self.message}")


class TDLibCancelled(RuntimeError):
    """The caller cancelled a request before its outcome was known."""

    cancelled = True


class TDLibRequestTimeout(TimeoutError):
    """No matching response arrived before the request deadline."""


class TDLibTransport(Protocol):
    """Minimal asynchronous transport expected by :class:`TDLibClient`."""

    def send(self, request: Mapping[str, Any]) -> None:
        """Submit one JSON-shaped TDLib request."""

    def receive(self, timeout: float) -> Mapping[str, Any] | None:
        """Return one update/response, or ``None`` when the poll expires."""


def _decode(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, Mapping) else None
    return None


def formatted_text(text: str = "") -> dict[str, Any]:
    """Build the common TDLib ``formattedText`` object."""

    return {"@type": "formattedText", "text": str(text), "entities": []}


def build_tdlib_parameters(
    *,
    api_id: int,
    api_hash: str,
    database_directory: str,
    files_directory: str,
    database_encryption_key: str = "",
    device_model: str = "TDLib Media Uploader",
    application_version: str = "",
    system_language_code: str = "zh-Hans",
    system_version: str = "",
    use_test_dc: bool = False,
    use_file_database: bool = True,
    use_chat_info_database: bool = True,
    use_message_database: bool = True,
) -> dict[str, Any]:
    """Build a pure ``setTdlibParameters`` payload."""

    return {
        "@type": "setTdlibParameters",
        "use_test_dc": bool(use_test_dc),
        "database_directory": str(database_directory),
        "files_directory": str(files_directory),
        "database_encryption_key": str(database_encryption_key),
        "use_file_database": bool(use_file_database),
        "use_chat_info_database": bool(use_chat_info_database),
        "use_message_database": bool(use_message_database),
        "use_secret_chats": False,
        "api_id": int(api_id),
        "api_hash": str(api_hash),
        "system_language_code": str(system_language_code),
        "device_model": str(device_model),
        "system_version": str(system_version),
        "application_version": str(application_version),
    }


def build_send_request(
    contents: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    target: TelegramTarget | Mapping[str, Any],
) -> dict[str, Any]:
    """Build a single-message or Album request for a canonical target."""

    parsed = target if isinstance(target, TelegramTarget) else parse_target(target)
    values = list(contents)
    if not values:
        raise ValueError("不能发送空的 Telegram Album。")
    if len(values) == 1:
        return {
            "@type": "sendMessage",
            "chat_id": parsed.chat_id,
            "topic_id": build_topic_object(parsed),
            "reply_to": None,
            "options": None,
            "reply_markup": None,
            "input_message_content": values[0],
        }
    return {
        "@type": "sendMessageAlbum",
        "chat_id": parsed.chat_id,
        "topic_id": build_topic_object(parsed),
        "reply_to": None,
        "options": None,
        "input_message_contents": values,
    }


class TDLibClient:
    """Synchronous request facade over an injected asynchronous transport."""

    def __init__(
        self,
        transport: TDLibTransport,
        *,
        request_timeout: float = 30.0,
        message_send_timeout: float = 120.0,
        extra_factory: Callable[[], str] | None = None,
    ):
        if not callable(getattr(transport, "send", None)):
            raise TypeError("TDLib transport 必须提供 send(request)。")
        if not callable(getattr(transport, "receive", None)):
            raise TypeError("TDLib transport 必须提供 receive(timeout)。")
        if request_timeout <= 0 or message_send_timeout <= 0:
            raise ValueError("TDLib 超时必须大于 0。")
        self.transport = transport
        self.request_timeout = float(request_timeout)
        self.message_send_timeout = float(message_send_timeout)
        self._extra_factory = extra_factory or (lambda: "req:" + uuid.uuid4().hex)
        self._cancelled = False
        self._closed = False
        self.is_premium: bool | None = None
        self.caption_length_limit: int | None = None
        self.updates: list[Mapping[str, Any]] = []
        self.auth_transitions: list[AuthTransition] = []
        self.send_updates: list[Mapping[str, Any]] = []
        self._callbacks: list[Callable[[Mapping[str, Any]], None]] = []

    def add_update_callback(self, callback: Callable[[Mapping[str, Any]], None]) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def remove_update_callback(self, callback: Callable[[Mapping[str, Any]], None]) -> None:
        try:
            self._callbacks.remove(callback)
        except ValueError:
            pass

    def cancel(self) -> None:
        """Mark future waits cancelled and notify a transport when supported."""

        self._cancelled = True
        cancel = getattr(self.transport, "cancel", None)
        if callable(cancel):
            cancel()

    def _raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise TDLibCancelled("TDLib 请求已取消")

    def _handle_update(self, value: Any) -> Mapping[str, Any] | None:
        update = _decode(value)
        if update is None:
            return None
        self.updates.append(update)
        transition = authorization_transition(update)
        if transition is not None:
            self.auth_transitions.append(transition)
        value_type = str(update.get("@type", ""))
        if value_type in {"updateMessageSendSucceeded", "updateMessageSendFailed"}:
            self.send_updates.append(update)
        for callback in tuple(self._callbacks):
            try:
                callback(update)
            except Exception:
                # One presentation/telemetry subscriber must not prevent the
                # request waiter from observing the matching response.
                pass
        return update

    def poll(self, timeout: float = 1.0) -> Mapping[str, Any] | None:
        """Receive and record one update without issuing a request."""

        self._raise_if_cancelled()
        value = self.transport.receive(max(0.0, float(timeout)))
        return self._handle_update(value)

    @staticmethod
    def _raise_for_error(response: Mapping[str, Any]) -> None:
        if response.get("@type") == "error":
            raise TDLibError(response.get("code", 0), response.get("message", "unknown error"))

    def request(self, query: Mapping[str, Any], timeout: float | None = None) -> Mapping[str, Any]:
        """Send one request and wait for the matching ``@extra`` response."""

        self._raise_if_cancelled()
        payload = dict(query)
        extra = str(payload.get("@extra") or self._extra_factory())
        payload["@extra"] = extra
        direct_execute = getattr(self.transport, "execute", None)
        if callable(direct_execute):
            response = _decode(direct_execute(payload))
            if response is None:
                raise TDLibRequestTimeout(f"TDLib 请求无响应：{query.get('@type')}")
            self._handle_update(response)
            self._raise_for_error(response)
            return response

        self.transport.send(payload)
        wait_seconds = self.request_timeout if timeout is None else float(timeout)
        if wait_seconds <= 0:
            raise ValueError("TDLib 请求超时必须大于 0。")
        deadline = time.monotonic() + wait_seconds
        while True:
            self._raise_if_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TDLibRequestTimeout(f"TDLib 请求超时：{query.get('@type')}")
            response = self._handle_update(self.transport.receive(min(1.0, remaining)))
            if response is None:
                continue
            if str(response.get("@extra", "")) != extra:
                continue
            self._raise_for_error(response)
            return response

    def wait_for_send_results(
        self,
        messages: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
        *,
        timeout: float | None = None,
    ) -> SendResult:
        """Resolve pending message updates without treating timeout as failure."""

        values = list(messages)
        initial = map_send_result(values)
        if not initial.pending_ids:
            return initial
        wait_seconds = self.message_send_timeout if timeout is None else float(timeout)
        if wait_seconds <= 0:
            raise ValueError("Telegram 发送确认超时必须大于 0。")
        updates: list[Mapping[str, Any]] = []
        deadline = time.monotonic() + wait_seconds
        while True:
            self._raise_if_cancelled()
            result = map_send_result(values, updates)
            if not result.pending_ids:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return map_send_result(values, updates, timed_out=True, error="等待 Telegram 确认发送成功超时")
            update = self.poll(min(1.0, remaining))
            if update is not None and str(update.get("@type", "")) in {
                "updateMessageSendSucceeded",
                "updateMessageSendFailed",
            }:
                updates.append(update)

    def send_contents(
        self,
        contents: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
        target: TelegramTarget | Mapping[str, Any],
        *,
        caption_limit: int | None = None,
        timeout: float | None = None,
    ) -> SendResult:
        """Validate captions, submit contents and map all delivery states."""

        values = tuple(contents)
        effective_caption_limit = (
            self.caption_length_limit if caption_limit is None else caption_limit
        )
        if effective_caption_limit is not None:
            validate_contents_captions(values, effective_caption_limit)
        request = build_send_request(values, target)
        submitted = False
        try:
            response = self.request(request, timeout=timeout)
            submitted = True
            if len(values) == 1:
                messages = [response]
            else:
                raw_messages = response.get("messages", [])
                messages = list(raw_messages) if isinstance(raw_messages, list) else []
                if len(messages) != len(values):
                    return map_send_result(
                        messages,
                        expected_count=len(values),
                        incomplete=True,
                        error=f"TDLib sendMessageAlbum 返回消息数量异常：{len(messages)}/{len(values)}",
                    )
            return self.wait_for_send_results(messages, timeout=timeout)
        except (TDLibCancelled, TDLibRequestTimeout) as exc:
            return map_send_result(
                (),
                expected_count=len(values),
                timed_out=isinstance(exc, TimeoutError),
                cancelled=isinstance(exc, TDLibCancelled),
                error=str(exc),
            )
        except Exception as exc:
            # A transport error after request handoff is intentionally
            # ambiguous; only a local validation/pre-send failure is FAILED.
            if not submitted:
                return SendResult(BatchStatus.FAILED, error=str(exc))
            return map_send_result(
                (),
                expected_count=len(values),
                incomplete=submitted,
                error=str(exc),
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self.transport, "close", None)
        if callable(close):
            close()


__all__ = [
    "TDLibCancelled",
    "TDLibClient",
    "TDLibError",
    "TDLibRequestTimeout",
    "TDLibTransport",
    "build_send_request",
    "build_tdlib_parameters",
    "formatted_text",
]
