"""Pure TDLib authorization-state extraction and transition helpers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class AuthState(str, Enum):
    WAIT_TDLIB_PARAMETERS = "authorizationStateWaitTdlibParameters"
    WAIT_PHONE_NUMBER = "authorizationStateWaitPhoneNumber"
    WAIT_CODE = "authorizationStateWaitCode"
    WAIT_PASSWORD = "authorizationStateWaitPassword"
    WAIT_EMAIL_ADDRESS = "authorizationStateWaitEmailAddress"
    WAIT_EMAIL_CODE = "authorizationStateWaitEmailCode"
    WAIT_OTHER_DEVICE_CONFIRMATION = "authorizationStateWaitOtherDeviceConfirmation"
    WAIT_REGISTRATION = "authorizationStateWaitRegistration"
    WAIT_PREMIUM_PURCHASE = "authorizationStateWaitPremiumPurchase"
    READY = "authorizationStateReady"
    LOGGING_OUT = "authorizationStateLoggingOut"
    CLOSING = "authorizationStateClosing"
    CLOSED = "authorizationStateClosed"


class AuthAction(str, Enum):
    INITIALIZE = "initialize"
    REQUEST_PHONE_NUMBER = "request_phone_number"
    REQUEST_CODE = "request_code"
    REQUEST_PASSWORD = "request_password"
    REQUEST_EMAIL_ADDRESS = "request_email_address"
    REQUEST_EMAIL_CODE = "request_email_code"
    CONFIRM_OTHER_DEVICE = "confirm_other_device"
    READY = "ready"
    WAIT = "wait"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class AuthTransition:
    """The UI-neutral consequence of one authorization update."""

    state: str
    action: str
    message: str = ""
    link: str | None = None
    requires_input: bool = False
    ready: bool = False
    terminal: bool = False


def _state_payload(value: Any) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    current: Any = value
    # A few wrappers used by test transports and older adapters put the actual
    # authorization object below ``state``.  TDLib itself uses one level.
    for _ in range(4):
        if not isinstance(current, Mapping):
            return None
        value_type = str(current.get("@type", ""))
        if value_type.startswith("authorizationState"):
            return current
        nested = current.get("authorization_state")
        if isinstance(nested, Mapping):
            current = nested
            continue
        nested = current.get("state")
        if isinstance(nested, Mapping):
            current = nested
            continue
        return None
    return None


def authorization_state_payload(update: Any) -> Mapping[str, Any] | None:
    """Extract the nested authorization state object from a TDLib update."""

    if not isinstance(update, Mapping):
        return None
    value_type = str(update.get("@type", ""))
    if value_type == "updateAuthorizationState":
        return _state_payload(update.get("authorization_state"))
    return _state_payload(update)


def extract_authorization_state(update: Any) -> str | None:
    """Return the raw ``authorizationState...`` type from an update."""

    payload = authorization_state_payload(update)
    if payload is None:
        return None
    value = payload.get("@type")
    return str(value) if value else None


authorization_state = extract_authorization_state


_TRANSITIONS: dict[str, tuple[AuthAction, str, bool, bool, bool]] = {
    AuthState.WAIT_TDLIB_PARAMETERS.value: (
        AuthAction.INITIALIZE,
        "正在初始化 TDLib。",
        False,
        False,
        False,
    ),
    AuthState.WAIT_PHONE_NUMBER.value: (
        AuthAction.REQUEST_PHONE_NUMBER,
        "需要 Telegram 手机号。",
        True,
        False,
        False,
    ),
    AuthState.WAIT_CODE.value: (
        AuthAction.REQUEST_CODE,
        "需要 Telegram 登录验证码。",
        True,
        False,
        False,
    ),
    AuthState.WAIT_PASSWORD.value: (
        AuthAction.REQUEST_PASSWORD,
        "需要 Telegram 两步验证密码。",
        True,
        False,
        False,
    ),
    AuthState.WAIT_EMAIL_ADDRESS.value: (
        AuthAction.REQUEST_EMAIL_ADDRESS,
        "需要 Telegram 登录邮箱地址。",
        True,
        False,
        False,
    ),
    AuthState.WAIT_EMAIL_CODE.value: (
        AuthAction.REQUEST_EMAIL_CODE,
        "需要邮箱验证码。",
        True,
        False,
        False,
    ),
    AuthState.WAIT_OTHER_DEVICE_CONFIRMATION.value: (
        AuthAction.CONFIRM_OTHER_DEVICE,
        "请在已经登录 Telegram 的设备确认此次登录。",
        False,
        False,
        False,
    ),
    AuthState.READY.value: (AuthAction.READY, "TDLib 登录成功。", False, True, False),
    AuthState.CLOSED.value: (AuthAction.CLOSED, "TDLib 已关闭。", False, False, True),
    AuthState.CLOSING.value: (AuthAction.WAIT, "TDLib 正在关闭。", False, False, False),
    AuthState.LOGGING_OUT.value: (AuthAction.WAIT, "TDLib 正在退出登录。", False, False, False),
    AuthState.WAIT_REGISTRATION.value: (
        AuthAction.WAIT,
        "TDLib 等待注册流程。",
        False,
        False,
        False,
    ),
    AuthState.WAIT_PREMIUM_PURCHASE.value: (
        AuthAction.WAIT,
        "TDLib 等待 Premium 流程。",
        False,
        False,
        False,
    ),
}


def authorization_transition(update: Any) -> AuthTransition | None:
    """Map a nested update to a deterministic auth transition."""

    payload = authorization_state_payload(update)
    state = extract_authorization_state(update)
    if payload is None or state is None:
        return None
    action, message, requires_input, ready, terminal = _TRANSITIONS.get(
        state,
        (AuthAction.WAIT, f"等待 TDLib 授权状态：{state}", False, False, False),
    )
    link = payload.get("link")
    return AuthTransition(
        state=state,
        action=action.value,
        message=message,
        link=str(link) if link else None,
        requires_input=requires_input,
        ready=ready,
        terminal=terminal,
    )


transition_for = authorization_transition


def _state_value(state_or_update: Any) -> str | None:
    if isinstance(state_or_update, Mapping):
        return extract_authorization_state(state_or_update)
    if isinstance(state_or_update, AuthState):
        return state_or_update.value
    value = str(state_or_update or "")
    return value or None


def build_authentication_request(state_or_update: Any, value: Any) -> dict[str, Any]:
    """Build the next TDLib auth request without reading input or sending it."""

    state = _state_value(state_or_update)
    text = "" if value is None else str(value).strip()
    if not text:
        raise ValueError("Telegram 授权输入不能为空。")
    if state == AuthState.WAIT_PHONE_NUMBER.value:
        return {"@type": "setAuthenticationPhoneNumber", "phone_number": text}
    if state == AuthState.WAIT_CODE.value:
        return {"@type": "checkAuthenticationCode", "code": text}
    if state == AuthState.WAIT_PASSWORD.value:
        return {"@type": "checkAuthenticationPassword", "password": text}
    if state == AuthState.WAIT_EMAIL_ADDRESS.value:
        return {"@type": "setAuthenticationEmailAddress", "email_address": text}
    if state == AuthState.WAIT_EMAIL_CODE.value:
        return {
            "@type": "checkAuthenticationEmailCode",
            "code": {"@type": "emailAddressAuthenticationCode", "code": text},
        }
    raise ValueError(f"授权状态不接受用户输入：{state or 'unknown'}")


def is_ready(update_or_state: Any) -> bool:
    state = _state_value(update_or_state)
    return state == AuthState.READY.value


__all__ = [
    "AuthAction",
    "AuthState",
    "AuthTransition",
    "authorization_state",
    "authorization_state_payload",
    "authorization_transition",
    "build_authentication_request",
    "extract_authorization_state",
    "is_ready",
    "transition_for",
]
