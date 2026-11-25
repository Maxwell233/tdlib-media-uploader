"""Own the application's authenticated TDLib client across queued jobs."""

from contextlib import contextmanager
from threading import Condition

from ..core.logging import write_app_log


_SESSION_SETTINGS = (
    "API_ID", "API_HASH", "TDLIB_DATABASE_ENCRYPTION_KEY",
    "TDLIB_USE_FILE_DATABASE", "TDLIB_USE_CHAT_INFO_DATABASE",
    "TDLIB_USE_MESSAGE_DATABASE", "TDLIB_LOG_VERBOSITY", "PROXY_ENABLED",
)
_PROXY_SETTINGS = (
    "PROXY_TYPE", "PROXY_SERVER", "PROXY_PORT", "PROXY_USERNAME",
    "PROXY_PASSWORD", "PROXY_HTTP_ONLY", "PROXY_SECRET",
)


class _IdleUI:
    @staticmethod
    def _write(level, message):
        write_app_log(level, str(message), source="tdlib")

    def log(self, message):
        self._write("INFO", message)

    info = log

    def success(self, message):
        self._write("SUCCESS", message)

    def warning(self, message):
        self._write("WARNING", message)

    def error(self, message):
        self._write("ERROR", message)


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

    def _set_state(self, client, state, *, closed=False):
        with self._condition:
            if client is not self._client:
                return
            if closed:
                self._client = None
                self._settings = None
            if state == self._state:
                return
            self._state = state
            self._condition.notify_all()
        if self._on_connection_state is not None:
            self._on_connection_state(state)

    def _observe(self, client, update):
        kind = update.get("@type")
        if kind == "updateAuthorizationState":
            state = (update.get("authorization_state") or {}).get("@type")
            if state == "authorizationStateClosed":
                self._set_state(client, "disconnected", closed=True)
                return
            if state == "authorizationStateClosing":
                self._set_state(client, "closing")
                return
            with self._condition:
                if self._state == "closing" or client.close_sent:
                    return
                if state == "authorizationStateReady":
                    self._set_state(client, client.connection_state or "connecting")
                elif isinstance(state, str):
                    self._set_state(client, "authorization_required")
        elif kind == "updateConnectionState":
            state = (update.get("state") or {}).get("@type")
            with self._condition:
                if (isinstance(state, str) and self._state not in {"authorization_required", "closing"}
                        and getattr(client, "authorization_ready", True) and not client.close_sent
                        and not client.closed_event.is_set()):
                    self._set_state(client, state)

    def wait_until_ready(self, cancel_token):
        """Wake on network recovery or a request to stop at an Album boundary."""
        with self._condition:
            while not _stopped(cancel_token):
                if self._state == "disconnected":
                    raise RuntimeError("Telegram 会话已关闭")
                if self._state == "closing":
                    raise RuntimeError("Telegram 会话正在关闭")
                if self._state == "authorization_required" or not getattr(self._client, "authorization_ready", True):
                    raise RuntimeError("Telegram 会话需要重新登录")
                if self._state == "connectionStateReady":
                    return True
                self._condition.wait(0.05)
            return False

    @contextmanager
    def acquire(self, ui, device_model, *, config, factory):
        """Bind per-job target, timeouts and UI before lending a logged-in client."""
        config_key = _session_settings(config)
        with self._condition:
            if self._lease_active:
                raise RuntimeError("已有上传任务正在使用 Telegram 会话")
            self._lease_active = True
            client = self._client
        initialized = False
        try:
            if client is not None and (
                self._settings != config_key or client.close_sent or client.closed_event.is_set()
                or self.connection_state in {"disconnected", "closing", "authorization_required"}
                or not getattr(client, "authorization_ready", True)
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
                client.login()
                with self._condition:
                    if self._state != "closing" and getattr(client, "authorization_ready", True):
                        self._set_state(client, client.connection_state or "connecting")
                client.set_fast_options()
            else:
                client.bind_job(ui, config)
            client.refresh_account_limits()
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
                        release = getattr(client, "release_job", None)
                        if callable(release):
                            release(self._idle_ui)
                        else:
                            client.bind_job(self._idle_ui, config)
            finally:
                with self._condition:
                    self._lease_active = False
                    self._condition.notify_all()

    def _close_client(self, client, timeout=30.0):
        if client.closed_event.is_set():
            self._set_state(client, "disconnected", closed=True)
            return
        self._set_state(client, "closing")
        try:
            client.close(timeout=timeout)
        except TimeoutError:
            if not client.closed_event.is_set():
                raise
        self._set_state(client, "disconnected", closed=True)

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
                self._condition.notify_all()
