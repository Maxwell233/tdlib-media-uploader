"""Own the application's authenticated TDLib client across queued jobs."""

from contextlib import contextmanager
from threading import Condition

from ..core.logging import write_app_log


_SESSION_SETTINGS = (
    "API_ID", "API_HASH", "TDLIB_DATABASE_ENCRYPTION_KEY",
    "TDLIB_USE_FILE_DATABASE", "TDLIB_USE_CHAT_INFO_DATABASE",
    "TDLIB_USE_MESSAGE_DATABASE", "PROXY_ENABLED",
)
_PROXY_SETTINGS = (
    "PROXY_TYPE", "PROXY_SERVER", "PROXY_PORT", "PROXY_USERNAME",
    "PROXY_PASSWORD", "PROXY_HTTP_ONLY", "PROXY_SECRET",
)
_WAITING_STATES = {
    "connecting", "connectionStateWaitingForNetwork", "connectionStateConnectingToProxy",
    "connectionStateConnecting", "connectionStateUpdating",
}


class _IdleUI:
    def warning(self, message):
        write_app_log("WARNING", str(message), source="tdlib")


def _session_settings(config):
    names = _SESSION_SETTINGS + (_PROXY_SETTINGS if getattr(config, "PROXY_ENABLED", False) else ())
    return tuple(getattr(config, name, None) for name in names)


def _stopped(cancel_token):
    return cancel_token.is_cancelled() or cancel_token.stop_after_current()


class TDLibSessionManager:
    """Lease the single TDLib client to one job at a time.

    The receive thread publishes connection state, while the job thread waits
    before submission. In-flight messages remain subject to TDLib's normal
    delivery and timeout handling.
    """

    def __init__(self, on_connection_state=None):
        self._condition = Condition()
        self._client = None
        self._lease_active = False
        self._settings = None
        self._state = "disconnected"
        self._on_connection_state = on_connection_state
        self._idle_ui = _IdleUI()

    @property
    def connection_state(self):
        with self._condition:
            return self._state

    def owns(self, client):
        with self._condition:
            return client is not None and client is self._client

    def _set_state(self, client, state):
        with self._condition:
            if client is not self._client or state == self._state:
                return
            self._state = state
            self._condition.notify_all()
        if self._on_connection_state is not None:
            self._on_connection_state(state)

    def _observe(self, client, update):
        kind = update.get("@type")
        if kind == "updateConnectionState":
            if client.close_sent or client.closed_event.is_set():
                return
            state = (update.get("state") or {}).get("@type")
            if isinstance(state, str):
                self._set_state(client, state)
        elif kind == "updateAuthorizationState":
            state = (update.get("authorization_state") or {}).get("@type")
            if state in {"authorizationStateLoggingOut", "authorizationStateClosing", "authorizationStateClosed"}:
                self._set_state(client, "disconnected")

    def wait_until_ready(self, cancel_token):
        """Wake on network recovery or a request to stop at an Album boundary."""
        with self._condition:
            while not _stopped(cancel_token):
                if self._state == "disconnected":
                    raise RuntimeError("Telegram 会话已关闭")
                if self._state not in _WAITING_STATES:
                    return True
                self._condition.wait(0.05)
            return False

    @contextmanager
    def acquire(self, ui, device_model, *, config, factory):
        """Bind per-job target, timeouts and UI before lending a logged-in client."""
        with self._condition:
            if self._lease_active:
                raise RuntimeError("已有上传任务正在使用 Telegram 会话")
            self._lease_active = True
            client = self._client
        config_key = _session_settings(config)
        initialized = False
        try:
            if client is not None and (
                self._settings != config_key or client.close_sent or client.closed_event.is_set()
                or self.connection_state == "disconnected"
            ):
                self._close_client(client)
                client = None
            if client is None:
                client = factory(ui, device_model, config=config)
                with self._condition:
                    self._client = client
                    self._settings = config_key
                self._set_state(client, "connecting")
                client.add_update_callback(lambda update, current=client: self._observe(current, update))
                with self._condition:
                    state = client.connection_state
                    if state:
                        self._set_state(client, state)
                client.login()
                client.refresh_account_limits()
                client.set_fast_options()
            else:
                client.bind_job(ui, config)
            initialized = True
            yield client
        finally:
            try:
                if client is not None:
                    unregister = getattr(ui, "unregister_client", None)
                    if callable(unregister):
                        unregister(client)
                    if not initialized or client.close_sent or client.closed_event.is_set():
                        try:
                            self._close_client(client, timeout=2.0)
                        except TimeoutError as error:
                            self._idle_ui.warning(error)
                    else:
                        client.bind_job(self._idle_ui, config)
            finally:
                with self._condition:
                    self._lease_active = False
                    self._condition.notify_all()

    def _close_client(self, client, timeout=30.0):
        try:
            client.close(timeout=timeout)
        finally:
            self._set_state(client, "disconnected")
        with self._condition:
            if self._client is client:
                self._client = None
                self._settings = None

    def disconnect(self, timeout=0.0):
        """Close an idle session. A timed-out close remains owned until TDLib acknowledges it."""
        with self._condition:
            if self._lease_active:
                raise RuntimeError("请先停止当前上传任务")
            client = self._client
            if client is None:
                return
            self._lease_active = True
        try:
            self._close_client(client, timeout=timeout)
        finally:
            with self._condition:
                self._lease_active = False
