# -*- coding: utf-8 -*-
"""TDLib shared client/helpers for the media uploaders."""

from __future__ import annotations

import getpass
import importlib.metadata
import json
import platform
import queue
import sys
import threading
import time
import uuid
from pathlib import Path

import tdjson

import app_config as cfg
from app_logging import TDLIB_LOG_PATH, write_app_log, write_exception
from path_utils import probe_readable, snapshot_file, stable_path
from runtime_paths import APP_DATA_DIR, TDLIB_DATABASE_DIR, TDLIB_FILES_DIR
from upload_journal import (
    CONFIRMED,
    FAILED,
    InflightJournal,
    UNKNOWN,
    _record_target,
    normalize_target,
)

REQUIRED_TDJSON_VERSION = "1.8.67"
PROJECT_DIR = APP_DATA_DIR


class TDLibError(RuntimeError):
    def __init__(self, code: int, message: str):
        self.code = int(code)
        self.message = str(message)
        super().__init__(f"TDLib error {self.code}: {self.message}")


class TDLibCancelled(RuntimeError):
    """Raised when a GUI or caller requests an immediate upload stop."""


class TDLibGracefulStop(RuntimeError):
    """Raised when a boundary-safe stop is requested before an Album starts."""


class UploadUnknownError(RuntimeError):
    """The Telegram request may have been accepted but was not confirmed."""


class UploadStalledError(RuntimeError):
    """No upload bytes or TDLib send-state activity was observed in time."""


class SendResultUnknown(TimeoutError):
    """A submitted Album has known and/or unknown individual outcomes."""

    def __init__(self, message: str, result: dict):
        super().__init__(message)
        self.result = result


def graceful_stop_requested(ui) -> bool:
    """Return whether the caller requested a boundary-safe stop."""

    return bool(getattr(ui, "stop_requested", False)) and not bool(
        getattr(ui, "force_stop_requested", False)
    )


def force_stop_requested(ui) -> bool:
    return bool(getattr(ui, "force_stop_requested", False))


def verify_tdjson_version() -> str:
    try:
        installed = importlib.metadata.version("tdjson")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("未安装 tdjson，请先运行对应平台的 setup 脚本。") from exc
    if installed != REQUIRED_TDJSON_VERSION:
        raise RuntimeError(
            "tdjson 版本不符合要求。\n\n"
            f"当前：{installed}\n要求：{REQUIRED_TDJSON_VERSION}\n\n"
            "请运行对应平台的 setup 脚本重新安装固定版本。"
        )
    return installed


def formatted_text(text: str = "") -> dict:
    return {"@type": "formattedText", "text": text, "entities": []}


class HeadlessUI:
    """Minimal internal adapter used when the GUI is not active.

    Upload modules still call a small presentation interface while scanning
    and sending.  Keeping this lightweight implementation in the backend lets
    the project retain its command-line-compatible core while recording the
    same durable diagnostics as the GUI.
    """

    def register_client(self, client):
        self.client = client

    @staticmethod
    def _write_log(level, text):
        write_app_log(level, text, source="ui")

    def log(self, text=""):
        self._write_log("INFO", text)

    def info(self, text):
        self._write_log("INFO", text)

    def success(self, text):
        self._write_log("INFO", text)

    def warning(self, text):
        self._write_log("WARNING", text)

    def error(self, text):
        self._write_log("ERROR", text)

    def banner(self, title, subtitle="", *, accent="cyan"):
        self._write_log("INFO", f"{title}\n{subtitle}".strip())

    def summary(self, title, rows, *, kind="VIDEO"):
        self._write_log("INFO", "\n".join([str(title)] + [f"{key}: {value}" for key, value in rows]))

    def files(self, title, columns, rows, *, kind="VIDEO", caption=None):
        text = str(title)
        if caption:
            text += f"\n{caption}"
        self._write_log("INFO", text)

    def groups(self, title, rows, *, kind="VIDEO"):
        self._write_log("INFO", title)

    def target(self, chat_title, topic_name, chat_id, topic_id):
        self._write_log("INFO", f"Telegram 目标：{chat_title} / {topic_name or '频道'} ({chat_id})")

    def album(self, *, kind, title, subtitle="", rows=None):
        self._write_log("INFO", f"{title}\n{subtitle}".strip())

    def progress(self, **kwargs):
        pass

    def finish(self):
        self._write_log("INFO", "当前媒体组处理结束")

    def cancelled(self):
        self._write_log("WARNING", "上传任务已取消")

    def confirm_upload(self):
        return True

    def prompt(self, text: str, *, password: bool = False):
        return getpass.getpass(text) if password else input(text)


def topic_object() -> dict | None:
    if getattr(cfg, "TARGET_MODE", "forum_topic") == "channel":
        return None
    return {
        "@type": "messageTopicForum",
        "forum_topic_id": int(cfg.FORUM_TOPIC_ID),
    }


def build_tdlib_parameters(device_model: str) -> dict:
    """Build the one canonical TDLib initialization payload.

    Keeping this payload in a pure helper makes the writable data locations
    inspectable by ``--self-test`` without creating a TDLib client or opening
    a network connection.  Both paths are always below the V1.9 ``DATA_DIR``.
    """

    return {
        "@type": "setTdlibParameters",
        "use_test_dc": False,
        "database_directory": str(TDLIB_DATABASE_DIR.resolve()),
        "files_directory": str(TDLIB_FILES_DIR.resolve()),
        "database_encryption_key": cfg.TDLIB_DATABASE_ENCRYPTION_KEY,
        "use_file_database": cfg.TDLIB_USE_FILE_DATABASE,
        "use_chat_info_database": cfg.TDLIB_USE_CHAT_INFO_DATABASE,
        "use_message_database": cfg.TDLIB_USE_MESSAGE_DATABASE,
        "use_secret_chats": False,
        "api_id": int(cfg.API_ID),
        "api_hash": cfg.API_HASH,
        "system_language_code": "zh-Hans",
        "device_model": str(device_model),
        "system_version": "macOS" if sys.platform == "darwin" else platform.system(),
        "application_version": cfg.APP_VERSION,
    }


class TDJsonClient:
    """Small synchronous wrapper around TDLib's JSON interface."""

    # tdjson exposes one process-wide receive queue.  More than one consumer
    # can silently steal another client's update, so session rotation must wait
    # until the previous receiver has actually exited.
    _receiver_guard = threading.Lock()
    _active_receiver: threading.Thread | None = None

    def __init__(self, ui, device_model: str):
        with TDJsonClient._receiver_guard:
            active = TDJsonClient._active_receiver
            if active is not None and active.is_alive():
                raise RuntimeError("TDLib receiver 尚未退出，不能创建新的客户端。")
        self.ui = ui
        self.device_model = device_model
        TDLIB_DATABASE_DIR.mkdir(parents=True, exist_ok=True)
        TDLIB_FILES_DIR.mkdir(parents=True, exist_ok=True)

        # Keep TDLib's native diagnostic stream beside the durable app log so
        # wrapped errors such as upload code 400 can be investigated later.
        try:
            TDLIB_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            result = self.execute({
                "@type": "setLogStream",
                "log_stream": {
                    "@type": "logStreamFile",
                    "path": str(TDLIB_LOG_PATH),
                    "max_file_size": 20 * 1024 * 1024,
                    "redirect_stderr": False,
                },
            })
            if isinstance(result, dict) and result.get("@type") == "error":
                write_app_log(
                    "WARNING",
                    f"TDLib 日志流配置失败：{result.get('message', result)}",
                    source="tdlib",
                )
        except Exception as exc:
            write_exception("TDLib 日志流配置失败", exc, source="tdlib")

        self.execute({
            "@type": "setLogVerbosityLevel",
            "new_verbosity_level": int(cfg.TDLIB_LOG_VERBOSITY),
        })

        self.client_id = tdjson.td_create_client_id()
        self.pending: dict[str, queue.Queue] = {}
        self.pending_lock = threading.Lock()
        # Set immediately after an outbound request reaches ``td_send``.  A
        # force stop that races the response must keep the Album UNKNOWN even
        # when ``request()`` raises before returning a response.
        self._last_request_dispatched = False
        self.auth_queue: queue.Queue = queue.Queue()
        self.send_events: dict[int, tuple[str, dict]] = {}
        self.send_condition = threading.Condition()
        self._activity_lock = threading.Lock()
        self._last_activity_time = time.monotonic()
        self._last_activity_reason = "client-created"
        self._file_progress: dict[int, int] = {}
        self._file_states: dict[int, tuple] = {}
        self._active_file_ids: set[int] = set()
        self._active_album_context: dict | None = None
        # Keep close idempotent because a stop request can arrive while a
        # sendMessage request is still waiting for its response.
        self.close_lock = threading.Lock()
        self.close_sent = False
        self.update_callbacks = []
        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()
        self.is_premium: bool | None = None
        self.caption_length_limit: int | None = None
        self.inflight_journal = InflightJournal()
        register_client = getattr(self.ui, "register_client", None)
        if callable(register_client):
            register_client(self)
        self.receiver_thread = threading.Thread(
            target=self._receiver_loop,
            name="TDLibReceiver",
            daemon=True,
        )
        with TDJsonClient._receiver_guard:
            active = TDJsonClient._active_receiver
            if active is not None and active.is_alive():
                raise RuntimeError("TDLib receiver 尚未退出，不能创建新的客户端。")
            TDJsonClient._active_receiver = self.receiver_thread
            try:
                self.receiver_thread.start()
            except Exception:
                TDJsonClient._active_receiver = None
                raise

    @staticmethod
    def _encode(obj: dict) -> bytes:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _decode(raw):
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    @classmethod
    def execute(cls, query: dict):
        raw = tdjson.td_execute(cls._encode(query))
        return cls._decode(raw) if raw else None

    def send_raw(self, query: dict):
        tdjson.td_send(self.client_id, self._encode(query))

    def cancel(self):
        """Force-stop the client and preserve UNKNOWN semantics for sent media."""
        self.cancel_event.set()
        # TDLib does not expose a generic cancelUploadFile method for
        # media sent through sendMessage/sendMessageAlbum.  Closing the client
        # is the supported way to abort the in-flight transfer; send it here
        # immediately instead of waiting for the worker's finally block.
        self._send_close_now()
        with self.send_condition:
            self.send_condition.notify_all()

    def request_graceful_stop(self):
        """Mark a caller's graceful stop without interrupting the current Album.

        The GUI owns the boundary decision.  This method intentionally does
        not set ``cancel_event`` or close TDLib; the active Album can therefore
        reach its normal confirmation/checkpoint path.
        """

        with self.send_condition:
            self.send_condition.notify_all()

    def _send_close_now(self):
        """Send TDLib's close command once, without a cancellable waiter."""
        with self.close_lock:
            if self.close_sent:
                return
            try:
                self.send_raw({"@type": "close"})
            except Exception:
                # Let a later finally/close call retry if the first send raced
                # TDLib teardown or failed before reaching the native client.
                return
            self.close_sent = True

    def _raise_if_cancelled(self):
        if self.cancel_event.is_set():
            raise TDLibCancelled("上传任务已立即停止")

    def request(self, query: dict, timeout: int | float | None = None):
        self._raise_if_cancelled()
        if timeout is None:
            timeout = cfg.TDLIB_REQUEST_TIMEOUT
        extra = "req:" + uuid.uuid4().hex
        payload = dict(query)
        payload["@extra"] = extra
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self.pending_lock:
            self.pending[extra] = waiter
        self._last_request_dispatched = False
        try:
            self.send_raw(payload)
            self._last_request_dispatched = True
        except Exception:
            with self.pending_lock:
                self.pending.pop(extra, None)
            raise
        deadline = time.monotonic() + float(timeout)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"TDLib 请求超时：{query.get('@type')}")
                try:
                    response = waiter.get(timeout=min(1.0, remaining))
                    break
                except queue.Empty:
                    self._raise_if_cancelled()
        except (queue.Empty, TimeoutError, TDLibCancelled) as exc:
            with self.pending_lock:
                self.pending.pop(extra, None)
            if isinstance(exc, TDLibCancelled):
                raise
            raise TimeoutError(f"TDLib 请求超时：{query.get('@type')}") from exc
        if response.get("@type") == "error":
            raise TDLibError(response.get("code", 0), response.get("message", "unknown error"))
        return response

    def add_update_callback(self, callback):
        self.update_callbacks.append(callback)

    def remove_update_callback(self, callback):
        try:
            self.update_callbacks.remove(callback)
        except ValueError:
            pass

    def _receiver_loop(self):
        while not self.stop_event.is_set():
            try:
                raw = tdjson.td_receive(1.0)
                if not raw:
                    continue
                obj = self._decode(raw)
                if not isinstance(obj, dict):
                    continue
                client_id = obj.get("@client_id")
                if client_id is not None and client_id != self.client_id:
                    continue

                extra = obj.get("@extra")
                if extra is not None:
                    with self.pending_lock:
                        waiter = self.pending.pop(extra, None)
                    if waiter is not None:
                        try:
                            waiter.put_nowait(obj)
                        except queue.Full:
                            pass

                kind = obj.get("@type")
                if kind == "updateAuthorizationState":
                    state = obj.get("authorization_state")
                    if state:
                        self.auth_queue.put(state)
                elif kind == "updateMessageSendSucceeded":
                    old_id = obj.get("old_message_id")
                    self._mark_message_activity(old_id, "message-send-succeeded")
                    with self.send_condition:
                        self.send_events[old_id] = ("success", obj)
                        self.send_condition.notify_all()
                elif kind == "updateMessageSendFailed":
                    old_id = obj.get("old_message_id")
                    self._mark_message_activity(old_id, "message-send-failed")
                    with self.send_condition:
                        self.send_events[old_id] = ("failed", obj)
                        self.send_condition.notify_all()
                elif kind == "updateFile":
                    self._record_file_activity(obj.get("file") or {})
                for callback in list(self.update_callbacks):
                    try:
                        callback(obj)
                    except Exception:
                        pass
            except Exception as exc:
                self.ui.warning(f"TDLib receiver 异常：{type(exc).__name__}: {exc}")
                time.sleep(1)
        # A receiver can also leave the loop after TDLib reports a closed
        # client.  Clear the process-wide guard here as well as in ``close``
        # so a failed/remote close cannot leave a stale thread identity behind.
        with TDJsonClient._receiver_guard:
            if TDJsonClient._active_receiver is threading.current_thread():
                TDJsonClient._active_receiver = None

    def _mark_activity(self, reason: str) -> None:
        with getattr(self, "_activity_lock", threading.Lock()):
            self._last_activity_time = time.monotonic()
            self._last_activity_reason = str(reason)

    def _mark_message_activity(self, message_id, reason: str) -> None:
        """Refresh the watchdog only for messages in the active Album."""

        context = getattr(self, "_active_album_context", None)
        if isinstance(context, dict):
            message_ids = {
                value for value in context.get("message_ids", []) if value is not None
            }
            if message_ids and message_id not in message_ids:
                return
        self._mark_activity(reason)

    @staticmethod
    def _uploaded_size(file_obj: dict) -> int:
        values = []
        for section_name in ("remote", "local"):
            section = file_obj.get(section_name) or {}
            for key in ("uploaded_size", "downloaded_size"):
                value = section.get(key)
                if isinstance(value, (int, float)):
                    values.append(int(value))
        return max(values or [0])

    def _record_file_activity(self, file_obj: dict) -> None:
        file_id = file_obj.get("id")
        uploaded = self._uploaded_size(file_obj)
        if file_id is None:
            if uploaded:
                self._mark_activity("file-progress")
            return
        active_ids = getattr(self, "_active_file_ids", set())
        if active_ids and file_id not in active_ids:
            return
        with getattr(self, "_activity_lock", threading.Lock()):
            previous = getattr(self, "_file_progress", {}).get(file_id, -1)
            remote = file_obj.get("remote") or {}
            local = file_obj.get("local") or {}
            state = (
                bool(remote.get("is_uploading_completed")),
                bool(remote.get("is_uploading_active")),
                bool(local.get("is_downloading_completed")),
                bool(local.get("is_downloading_active")),
            )
            previous_state = getattr(self, "_file_states", {}).get(file_id)
            self._file_progress[file_id] = uploaded
            self._file_states[file_id] = state
            changed = uploaded != previous
            completed = bool(remote.get("is_uploading_completed"))
            if changed or completed or state != previous_state:
                self._last_activity_time = time.monotonic()
                self._last_activity_reason = (
                    "file-completed" if completed else "file-progress"
                )

    def _begin_album_watchdog(self, album_key, kind, items, messages=None) -> None:
        records = []
        for item in items or []:
            path = item.get("path") if isinstance(item, dict) else item
            expected = None
            if isinstance(item, dict):
                expected = item.get("scan_size") or item.get("size")
            records.append({
                "path": str(path),
                "expected_size": int(expected) if expected is not None else None,
            })
        file_ids = set()
        for message in messages or []:
            content = (message or {}).get("content") if isinstance(message, dict) else None
            if not isinstance(content, dict):
                continue
            media = content.get("video") or content.get("photo") or {}
            candidates = []
            if isinstance(media, dict):
                if isinstance(media.get("video"), dict):
                    candidates.append(media["video"])
                candidates.extend(
                    value.get("photo") for value in media.get("sizes", [])
                    if isinstance(value, dict) and isinstance(value.get("photo"), dict)
                )
            file_ids.update(
                value.get("id") for value in candidates
                if isinstance(value, dict) and value.get("id") is not None
            )
        with getattr(self, "_activity_lock", threading.Lock()):
            self._active_album_context = {
                "album_key": album_key or "",
                "kind": kind or "unknown",
                # Capture the effective destination at submission time so a
                # later configuration change cannot make a stall diagnostic
                # point at the wrong chat/topic.
                "target": self._target_identity(),
                "items": records,
                "message_ids": [m.get("id") for m in (messages or []) if isinstance(m, dict)],
                "pending_ids": [],
                "succeeded_ids": [],
                "failed_ids": [],
                "started_at": time.time(),
            }
            self._last_activity_time = time.monotonic()
            self._last_activity_reason = "album-submitted"
            self._file_progress = {}
            self._file_states = {}
            self._active_file_ids = {int(value) for value in file_ids}

    def _stall_snapshot(self) -> tuple[float, str, dict | None, dict[int, int]]:
        lock = getattr(self, "_activity_lock", threading.Lock())
        with lock:
            return (
                float(getattr(self, "_last_activity_time", time.monotonic())),
                str(getattr(self, "_last_activity_reason", "unknown")),
                getattr(self, "_active_album_context", None),
                dict(getattr(self, "_file_progress", {})),
            )

    def _check_upload_stall(self) -> None:
        raw_timeout = getattr(cfg, "TDLIB_UPLOAD_STALL_TIMEOUT", 300)
        try:
            stall_timeout = float(raw_timeout)
        except (TypeError, ValueError):
            stall_timeout = 0.0
        if stall_timeout <= 0:
            return
        last_activity, reason, context, file_progress = self._stall_snapshot()
        if context is None:
            return
        duration = time.monotonic() - last_activity
        if duration < stall_timeout:
            return
        rows = [
            f"file_id={file_id} uploaded_size={uploaded}"
            for file_id, uploaded in sorted(file_progress.items(), key=lambda value: str(value[0]))
        ]
        rows.extend(
            f"path={item.get('path')} expected_size={item.get('expected_size')}"
            for item in context.get("items", [])
        )
        status_text = (
            f"pending_ids={context.get('pending_ids', [])} "
            f"succeeded_ids={context.get('succeeded_ids', [])} "
            f"failed_ids={context.get('failed_ids', [])}"
        )
        message = (
            f"Album 上传疑似卡住：{context.get('album_key')} "
            f"kind={context.get('kind')} stall={duration:.1f}s "
            f"target={context.get('target', {})} "
            f"last_activity_time={last_activity:.6f} "
            f"last_activity={reason} message_ids={context.get('message_ids', [])} {status_text}\n"
            + "\n".join(rows)
        )
        write_app_log("ERROR", message, source="upload/stall")
        warning = getattr(self.ui, "warning", None)
        if callable(warning):
            try:
                warning("当前 Album 长时间没有上传进度，已标记为未确认上传；请在页面中人工核对。")
            except Exception:
                pass
        raise UploadStalledError(message)

    @staticmethod
    def _configured_proxy() -> dict:
        """Build the TDLib proxy object from the optional local config."""
        proxy_type = cfg.PROXY_TYPE
        if proxy_type == "socks5":
            type_payload = {
                "@type": "proxyTypeSocks5",
                "username": cfg.PROXY_USERNAME,
                "password": cfg.PROXY_PASSWORD,
            }
        elif proxy_type == "http":
            type_payload = {
                "@type": "proxyTypeHttp",
                "username": cfg.PROXY_USERNAME,
                "password": cfg.PROXY_PASSWORD,
                "http_only": cfg.PROXY_HTTP_ONLY,
            }
        elif proxy_type == "mtproto":
            type_payload = {
                "@type": "proxyTypeMtproto",
                "secret": cfg.PROXY_SECRET,
            }
        else:
            # app_config validates this, but keep the client defensive when
            # tests or an embedding application provide a custom config.
            raise RuntimeError(f"不支持的代理类型：{proxy_type}")
        return {
            "@type": "proxy",
            "server": cfg.PROXY_SERVER,
            "port": int(cfg.PROXY_PORT),
            "type": type_payload,
        }

    def _configure_proxy(self):
        """Apply the independent proxy setting before authentication."""
        if not cfg.PROXY_ENABLED:
            # A previous run may have enabled a proxy in TDLib's database.
            # Explicitly disable it so the unchecked setting always means a
            # direct connection.
            self.request({"@type": "disableProxy"}, timeout=30)
            self.ui.info("代理未启用，使用直连。")
            return

        proxy_payload = self._configured_proxy()
        proxies_response = self.request({"@type": "getProxies"}, timeout=30)
        existing = None
        for entry in proxies_response.get("proxies", []):
            configured = entry.get("proxy") or {}
            configured_type = (configured.get("type") or {}).get("@type")
            if (
                configured.get("server") == proxy_payload["server"]
                and int(configured.get("port", -1)) == proxy_payload["port"]
                and configured_type == proxy_payload["type"]["@type"]
            ):
                existing = entry
                break

        if existing is not None and isinstance(existing.get("id"), int):
            self.request({
                "@type": "editProxy",
                "proxy_id": existing["id"],
                "proxy": proxy_payload,
                "enable": True,
            }, timeout=30)
        else:
            self.request({
                "@type": "addProxy",
                "proxy": proxy_payload,
                "enable": True,
            }, timeout=30)

        labels = {"socks5": "SOCKS5", "http": "HTTP", "mtproto": "MTProto"}
        self.ui.info(
            f"代理已启用：{labels.get(cfg.PROXY_TYPE, cfg.PROXY_TYPE)} "
            f"{cfg.PROXY_SERVER}:{cfg.PROXY_PORT}"
        )

    def login(self):
        try:
            self.request({"@type": "getOption", "name": "version"}, timeout=30)
        except Exception:
            pass

        proxy_configured = False
        while True:
            self._raise_if_cancelled()
            auth_deadline = time.monotonic() + 120
            remaining = auth_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("等待 TDLib 授权状态超时")
            try:
                state = self.auth_queue.get(timeout=min(1, remaining))
            except queue.Empty:
                continue

            state_type = state.get("@type")
            if state_type == "authorizationStateWaitTdlibParameters":
                self.ui.info("正在初始化 TDLib…")
                self.request(build_tdlib_parameters(self.device_model))
                if not proxy_configured:
                    self._configure_proxy()
                    proxy_configured = True
            elif not proxy_configured:
                # Existing TDLib databases can resume at a later auth state,
                # so configure the network path on the first state we see.
                self._configure_proxy()
                proxy_configured = True

            if state_type == "authorizationStateWaitPhoneNumber":
                phone = self._prompt(
                    "请输入 Telegram 手机号（国际格式，例如 +491234...）："
                )
                self.request({"@type": "setAuthenticationPhoneNumber", "phone_number": phone})
            elif state_type == "authorizationStateWaitCode":
                code = self._prompt("请输入 Telegram 登录验证码：")
                self.request({"@type": "checkAuthenticationCode", "code": code})
            elif state_type == "authorizationStateWaitPassword":
                password = self._prompt("请输入 Telegram 两步验证密码：", password=True)
                self.request({"@type": "checkAuthenticationPassword", "password": password})
            elif state_type == "authorizationStateWaitEmailAddress":
                email = self._prompt("请输入 Telegram 要求的邮箱地址：")
                self.request({"@type": "setAuthenticationEmailAddress", "email_address": email})
            elif state_type == "authorizationStateWaitEmailCode":
                code = self._prompt("请输入邮箱验证码：")
                self.request({
                    "@type": "checkAuthenticationEmailCode",
                    "code": {"@type": "emailAddressAuthenticationCode", "code": code},
                })
            elif state_type == "authorizationStateWaitOtherDeviceConfirmation":
                self.ui.warning("请在已经登录 Telegram 的设备确认此次登录：")
                if state.get("link"):
                    self.ui.log(state["link"])
            elif state_type == "authorizationStateReady":
                self.ui.success("TDLib 登录成功。")
                return
            elif state_type == "authorizationStateClosed":
                raise RuntimeError("TDLib 已关闭。")

    def _prompt(self, text: str, *, password: bool = False) -> str:
        prompt = getattr(self.ui, "prompt", None)
        if callable(prompt):
            value = prompt(text, password=password)
        elif password:
            value = getpass.getpass(text)
        else:
            value = input(text)
        value = str(value).strip()
        if not value:
            if graceful_stop_requested(self.ui):
                raise TDLibGracefulStop("上传已请求安全停止")
            self._raise_if_cancelled()
        return value

    def _try_get_chat(self, chat_id: int):
        try:
            return self.request({"@type": "getChat", "chat_id": int(chat_id)})
        except TDLibError as exc:
            if exc.code == 400:
                return None
            raise

    def _load_chat_list_until_found(self, chat_list, label: str, max_rounds: int = 100):
        self.ui.info(f"正在加载 Telegram {label}，以定位目标群…")
        for index in range(1, max_rounds + 1):
            chat = self._try_get_chat(cfg.CHAT_ID)
            if chat is not None:
                self.ui.success(f"已在{label}中找到目标聊天。")
                return chat
            try:
                self.request({
                    "@type": "loadChats",
                    "chat_list": chat_list,
                    "limit": 100,
                }, timeout=60)
            except TDLibError as exc:
                if exc.code == 404:
                    return self._try_get_chat(cfg.CHAT_ID)
                raise
            time.sleep(0.05)
            chat = self._try_get_chat(cfg.CHAT_ID)
            if chat is not None:
                self.ui.success(f"已在{label}中找到目标聊天（第 {index} 批）。")
                return chat
        return None

    def ensure_target_chat(self):
        chat = self._try_get_chat(cfg.CHAT_ID)
        if chat is not None:
            return chat
        self.ui.info("当前 TDLib 数据库尚未加载目标聊天，开始加载聊天列表。")
        chat = self._load_chat_list_until_found(None, "主聊天列表")
        if chat is not None:
            return chat
        chat = self._load_chat_list_until_found({"@type": "chatListArchive"}, "归档聊天列表")
        if chat is not None:
            return chat
        if cfg.CHAT_ID <= -1000000000001:
            supergroup_id = -int(cfg.CHAT_ID) - 1000000000000
            try:
                return self.request({
                    "@type": "createSupergroupChat",
                    "supergroup_id": supergroup_id,
                    "force": True,
                })
            except TDLibError:
                pass
        raise RuntimeError(
            "找不到目标聊天。请确认登录账号仍在目标群组/频道中，且 config.toml 中的 chat_id 正确。"
        )

    def validate_target(self):
        chat = self.ensure_target_chat()
        chat_type = chat.get("type") or {}
        if chat_type.get("@type") != "chatTypeSupergroup":
            raise RuntimeError("目标聊天必须是超级群组或频道。")

        if getattr(cfg, "TARGET_MODE", "forum_topic") == "channel":
            if not chat_type.get("is_channel", False):
                raise RuntimeError("当前 Chat ID 不是频道，请在目标设置中选择正确的频道。")
            self.ui.target(
                chat.get("title", ""),
                "",
                cfg.CHAT_ID,
                None,
            )
            return chat, None

        if chat_type.get("is_channel", False):
            raise RuntimeError("当前 Chat ID 是频道；请切换目标模式为 Channel 频道。")
        try:
            topic = self.request({
                "@type": "getForumTopic",
                "chat_id": int(cfg.CHAT_ID),
                "forum_topic_id": int(cfg.FORUM_TOPIC_ID),
            })
        except TDLibError as exc:
            raise RuntimeError(
                f"CHAT_ID 已找到，但 Topic 无法解析。FORUM_TOPIC_ID={cfg.FORUM_TOPIC_ID}；{exc}"
            ) from exc
        topic_name = topic.get("info", {}).get("name", "")
        self.ui.target(
            chat.get("title", ""),
            topic_name,
            cfg.CHAT_ID,
            cfg.FORUM_TOPIC_ID,
        )
        return chat, topic

    def set_fast_options(self):
        for name, value in (("use_quick_ack", True), ("online", True)):
            try:
                self.request({
                    "@type": "setOption",
                    "name": name,
                    "value": {"@type": "optionValueBoolean", "value": value},
                })
            except Exception:
                pass

    def wait_for_send_results(self, messages, timeout: int | None = None):
        self._raise_if_cancelled()
        if timeout is None:
            timeout = cfg.TDLIB_MESSAGE_SEND_TIMEOUT
        pending_ids = []
        succeeded_ids = []
        failed_ids = []
        for message in messages:
            message_id = message.get("id")
            sending_state = message.get("sending_state")
            if not sending_state:
                succeeded_ids.append(message_id)
                continue
            if sending_state.get("@type") == "messageSendingStateFailed":
                failed_ids.append(message_id)
                continue
            pending_ids.append(message_id)

        def result_payload(pending=None):
            return {
                "succeeded": list(succeeded_ids),
                "failed": list(failed_ids),
                "pending": list(pending or []),
            }

        if not pending_ids:
            return result_payload()

        deadline = time.monotonic() + timeout
        results = {}
        context = getattr(self, "_active_album_context", None)
        if isinstance(context, dict):
            context["pending_ids"] = list(pending_ids)
        with self.send_condition:
            while len(results) < len(pending_ids):
                if getattr(self, "cancel_event", threading.Event()).is_set():
                    unknown = [value for value in pending_ids if value not in results]
                    raise SendResultUnknown(
                        "上传在 Telegram 状态确认前被取消",
                        result_payload(unknown),
                    )
                self._check_upload_stall()
                for old_id in pending_ids:
                    if old_id in results:
                        continue
                    event = self.send_events.get(old_id)
                    if event is None:
                        continue
                    status, update = event
                    if status == "failed":
                        error_obj = update.get("error", {})
                        failed_ids.append(old_id)
                        if isinstance(context, dict):
                            context["failed_ids"] = list(failed_ids)
                        results[old_id] = update
                    else:
                        new_id = update.get("message", {}).get("id") or old_id
                        succeeded_ids.append(new_id)
                        if isinstance(context, dict):
                            context["succeeded_ids"] = list(succeeded_ids)
                        results[old_id] = update
                if len(results) >= len(pending_ids):
                    break
                self._check_upload_stall()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    unknown = [value for value in pending_ids if value not in results]
                    raise SendResultUnknown(
                        "等待 Telegram 确认发送成功超时",
                        result_payload(unknown),
                    )
                stall_timeout = float(getattr(cfg, "TDLIB_UPLOAD_STALL_TIMEOUT", 300) or 0)
                if stall_timeout > 0:
                    last_activity, _reason, _context, _progress = self._stall_snapshot()
                    until_stall = max(0.05, stall_timeout - (time.monotonic() - last_activity))
                else:
                    until_stall = 1.0
                self.send_condition.wait(min(1.0, remaining, until_stall))

        for old_id in pending_ids:
            with self.send_condition:
                self.send_events.pop(old_id, None)
        return result_payload()

    @staticmethod
    def _item_source(item):
        """Get the original local source path from a media item when present."""

        if isinstance(item, dict):
            value = item.get("path")
        else:
            value = item
        return Path(value) if value else None

    def _diagnose_upload_failure(self, contents, items, exc) -> None:
        """Persist source-file health details for otherwise opaque TDLib errors."""

        records = []
        for index, item in enumerate(items or [], 1):
            path = self._item_source(item)
            if path is None:
                records.append(f"媒体 {index}: 未提供本地源路径")
                continue
            snapshot = snapshot_file(path)
            if snapshot is None:
                records.append(f"媒体 {index}: 源文件不存在或不可 stat · {path}")
                continue
            try:
                probe_readable(path, snapshot=snapshot, probe_bytes=4096)
                state = "源文件可读取"
            except Exception as probe_error:
                state = f"源文件读取失败：{type(probe_error).__name__}: {probe_error}"
            # Keep this comparison defensive for legacy callers that pass
            # tuples/Path objects instead of scanner item dictionaries.
            if isinstance(item, dict):
                expected_size = item.get("scan_size")
                expected_mtime_ns = item.get("scan_mtime_ns")
                if expected_size is not None and expected_mtime_ns is not None and (
                    int(expected_size), int(expected_mtime_ns)
                ) != snapshot.as_tuple():
                    state += "；相对扫描快照已发生变化"
            records.append(
                f"媒体 {index}: {state} · {path}"
                f" (size={snapshot.size}, mtime_ns={snapshot.mtime_ns}, id={stable_path(path)})"
            )
        detail = "\n".join(records) or "未提供媒体列表，无法检查本地源文件"
        message = (
            f"TDLib 上传失败源诊断：{type(exc).__name__}: {exc}\n"
            f"{detail}"
        )
        write_app_log("ERROR", message, source="upload")
        warning = getattr(self.ui, "warning", None)
        if callable(warning):
            try:
                warning("TDLib 上传失败，已记录源文件诊断；请查看运行日志。")
            except Exception:
                pass

    def refresh_account_limits(self) -> tuple[bool | None, int | None]:
        """Read account Premium and caption limits from the active TDLib."""

        try:
            value = self.request({"@type": "getOption", "name": "is_premium"}, timeout=30)
            if isinstance(value, dict) and value.get("@type") == "optionValueBoolean":
                self.is_premium = bool(value.get("value", False))
        except Exception:
            # Older TDLib builds may not expose the option. Keep ``None`` so
            # callers can make a conservative, visible decision.
            self.is_premium = None
        for option_name in ("message_caption_length_max", "message_caption_length_maximum"):
            try:
                value = self.request({"@type": "getOption", "name": option_name}, timeout=30)
            except Exception:
                continue
            if isinstance(value, dict):
                raw = value.get("value")
                try:
                    if raw is not None:
                        self.caption_length_limit = int(raw)
                        break
                except (TypeError, ValueError):
                    pass
        return self.is_premium, self.caption_length_limit

    def _safe_diagnose_upload_failure(self, contents, items, exc) -> None:
        """Never let diagnostics hide the original upload exception."""

        try:
            self._diagnose_upload_failure(contents, items, exc)
        except Exception as diagnostic_error:
            write_exception(
                "TDLib 上传失败源诊断自身失败",
                diagnostic_error,
                source="upload",
            )

    def send_contents(
        self,
        contents,
        progress=None,
        items=None,
        *,
        album_key: str | None = None,
        kind: str | None = None,
        journal_items=None,
    ):
        return self._send_contents(
            contents,
            progress=progress,
            items=items,
            album_key=album_key,
            kind=kind,
            journal_items=journal_items,
        )

    @staticmethod
    def _infer_upload_kind(contents) -> str:
        kinds = set()
        for content in contents or []:
            content_type = str((content or {}).get("@type", ""))
            if "Photo" in content_type:
                kinds.add("image")
            elif "Video" in content_type:
                kinds.add("video")
        if len(kinds) == 1:
            return next(iter(kinds))
        if len(kinds) > 1:
            return "mixed"
        return "unknown"

    def _validate_caption_length(self, contents) -> None:
        """Reject an over-limit user caption before preparing a journal.

        Uploaders normally build captions with the same limit, but keeping
        this guard in the shared sender protects direct integrations and
        ensures a user-authored caption can never be silently truncated or
        turn into an UNKNOWN send attempt.
        """

        raw_limit = getattr(self, "caption_length_limit", None)
        try:
            limit = int(raw_limit) if raw_limit is not None else 0
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return
        for content in contents or []:
            caption = (content or {}).get("caption")
            if not isinstance(caption, dict):
                continue
            text = caption.get("text", "")
            if text is None:
                continue
            if len(str(text)) > limit:
                raise ValueError(
                    f"标题超过 Telegram Caption 限制（{len(str(text))}/{limit} 字符）"
                )

    @staticmethod
    def _iter_local_input_paths(value):
        """Yield every ``inputFileLocal`` path contained in a TDLib payload."""

        if isinstance(value, dict):
            if value.get("@type") == "inputFileLocal":
                yield value.get("path")
                return
            for nested in value.values():
                yield from TDJsonClient._iter_local_input_paths(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                yield from TDJsonClient._iter_local_input_paths(nested)

    def _validate_local_input_paths(self, contents) -> None:
        """Fail before a TDLib request when a generated local input vanished.

        TDLib reports this situation as the generic 400 ``Can't find real file
        path`` error.  The media builders already perform readiness checks, but
        a temporary compression/staging artifact can still disappear between
        construction and request dispatch (especially after an interrupted
        task).  Checking the exact paths recursively here avoids submitting an
        Album that TDLib cannot read and keeps the in-flight journal clean.
        """

        missing = []
        for raw_path in self._iter_local_input_paths(contents):
            if not raw_path:
                missing.append("<empty path>")
                continue
            try:
                exists = Path(raw_path).is_file()
            except (OSError, TypeError, ValueError):
                exists = False
            if not exists:
                missing.append(str(raw_path))
        if missing:
            preview = ", ".join(missing[:5])
            if len(missing) > 5:
                preview += f" …（另有 {len(missing) - 5} 个）"
            raise FileNotFoundError(
                f"TDLib 本地输入文件不存在或不可读取：{preview}"
            )

    @staticmethod
    def _target_identity() -> dict:
        """Capture the effective Telegram destination for journal scoping."""

        return normalize_target({
            "target_mode": getattr(cfg, "TARGET_MODE", "forum_topic"),
            "chat_id": getattr(cfg, "CHAT_ID", 0),
            "forum_topic_id": getattr(cfg, "FORUM_TOPIC_ID", 0),
            "channel_chat_id": getattr(cfg, "CHANNEL_CHAT_ID", 0),
        })

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
                    item["capture_time"] = __import__("datetime").datetime.fromisoformat(capture_time)
                except ValueError:
                    item["capture_time"] = None
            values.append(item)
        return values

    def _state_for_journal(self, kind: str, record: dict, *, fallback_target=None):
        """Create the matching uploader state without touching source files.

        A target recorded with the send attempt is authoritative.  Legacy
        target-less records use an explicitly supplied fallback, then the
        media-kind-specific configured target, and only lastly the historical
        mutable global target values.
        """

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
            import tdlib_video_album_uploader as module
        elif kind == "image":
            import tdlib_image_album_uploader as module
        elif kind == "mixed":
            import tdlib_mixed_album_uploader as module
        else:
            raise ValueError(f"无法为未知媒体类型恢复上传断点：{kind}")
        return module.UploadState(target=target)

    def finalize_inflight(
        self,
        album_key: str,
        *,
        kind: str | None = None,
        message_ids=None,
        target=None,
    ) -> None:
        """Remove a confirmed journal after the uploader persisted its state."""

        if not album_key:
            return
        selected_kind = kind or self._journal_kind_for(album_key)
        target = normalize_target(target) or self._target_identity()
        # The send path already wrote CONFIRMED before the uploader checkpoint
        # call.  This operation only removes the durable guard after that
        # checkpoint has succeeded.
        self.inflight_journal.finalize(selected_kind, album_key, target=target)

    def reconcile_inflight(
        self,
        album_key: str,
        *,
        sent: bool,
        kind: str | None = None,
        message_ids=None,
        target=None,
    ) -> None:
        """Manually resolve an UNKNOWN send without querying Telegram history."""

        requested_target = normalize_target(target)
        effective_target = requested_target or self._target_identity()
        selected_kind = kind or self._journal_kind_for(album_key, target=effective_target)
        _path, record = self.inflight_journal.get_entry(selected_kind, album_key, effective_target)
        if record is None:
            raise RuntimeError(f"未找到未确认上传记录：{selected_kind} / {album_key}")
        if not sent:
            self.inflight_journal.mark_not_sent(selected_kind, album_key, target=effective_target)
            return
        items = self._journal_items(record)
        if not items:
            # A legacy path-only record can still be resolved when its source
            # files remain available.  If they do not, retain the journal so
            # the user is never allowed to accidentally create duplicates.
            raise RuntimeError("上传日志缺少可恢复的文件快照，已保留记录以避免重复上传。")
        ids = list(message_ids if message_ids is not None else record.get("message_ids", []))
        state = self._state_for_journal(
            selected_kind,
            record,
            fallback_target=requested_target or None,
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

    def _journal_kind_for(self, album_key: str, *, target=None) -> str:
        record = self.inflight_journal.find_album(album_key, target=target)
        if record:
            value = str(record.get("kind", "")).strip().lower()
            if value:
                return value
        return "unknown"

    def _send_contents(
        self,
        contents,
        progress=None,
        items=None,
        *,
        album_key=None,
        kind=None,
        journal_items=None,
    ):
        """Send media and persist PREPARED/SUBMITTED/UNKNOWN transitions."""
        journal = getattr(self, "inflight_journal", None)
        selected_kind = kind or self._infer_upload_kind(contents)
        journal_active = bool(album_key and journal is not None)
        journal_target = self._target_identity() if journal_active else None
        submitted = False
        journal_terminal = False
        self._last_request_dispatched = False
        with getattr(self, "_activity_lock", threading.Lock()):
            self._active_album_context = None
            self._active_file_ids = set()
        if journal_active:
            unresolved = journal.unresolved(selected_kind, album_key, target=journal_target)
            if unresolved is not None:
                message = (
                    "发送状态未知，为避免重复未自动重试："
                    f"{selected_kind} Album {album_key}。请在 Telegram 中确认后再手动处理。"
                )
                warning = getattr(self.ui, "warning", None)
                if callable(warning):
                    warning(message)
                raise UploadUnknownError(message)
        self._validate_caption_length(contents)
        # Validate all generated inputFileLocal paths before PREPARED is
        # persisted.  A missing temporary artifact is a clean pre-dispatch
        # failure and must not create an UNKNOWN journal entry.
        try:
            self._validate_local_input_paths(contents)
        except Exception as exc:
            self._safe_diagnose_upload_failure(contents, items, exc)
            raise
        if journal_active:
            journal.prepare(
                selected_kind,
                album_key,
                items if journal_items is None else journal_items,
                target=journal_target,
            )
        try:
            if len(contents) == 1:
                message = self.request({
                    "@type": "sendMessage",
                    "chat_id": int(cfg.CHAT_ID),
                    "topic_id": topic_object(),
                    "reply_to": None,
                    "options": None,
                    "reply_markup": None,
                    "input_message_content": contents[0],
                })
                messages = [message]
            else:
                response = self.request({
                    "@type": "sendMessageAlbum",
                    "chat_id": int(cfg.CHAT_ID),
                    "topic_id": topic_object(),
                    "reply_to": None,
                    "options": None,
                    "input_message_contents": contents,
                })
                messages = response.get("messages", [])
                if len(messages) != len(contents):
                    # TDLib accepted the request but returned an incomplete
                    # response.  The missing message IDs make the delivery
                    # outcome ambiguous, so keep the prepared journal in the
                    # UNKNOWN path instead of allowing an automatic resend.
                    submitted = journal_active
                    raise RuntimeError(
                        f"TDLib sendMessageAlbum 返回消息数量异常：{len(messages)}/{len(contents)}"
                    )
            # The request has reached TDLib even when one item is already in
            # a failed sending state. Record the whole message set so a mixed
            # success/failure outcome can never be mistaken for a clean retry.
            if journal_active:
                journal.submitted(
                    selected_kind,
                    album_key,
                    [message.get("id") for message in messages],
                    target=journal_target,
                )
                submitted = True
            if progress is not None and items is not None:
                progress.register_messages(messages, items)
            self._begin_album_watchdog(album_key, selected_kind, items, messages)
            result = self.wait_for_send_results(messages)
            if not isinstance(result, dict):
                result = {"succeeded": list(result or []), "failed": [], "pending": []}
            succeeded = list(result.get("succeeded", []))
            failed = list(result.get("failed", []))
            pending = list(result.get("pending", []))
            if failed or pending or len(succeeded) != len(messages):
                if journal_active:
                    status = FAILED if not succeeded and not pending and failed else UNKNOWN
                    journal.update(
                        selected_kind,
                        album_key,
                        status,
                        message_ids=succeeded + failed + pending,
                        succeeded_ids=succeeded,
                        failed_ids=failed,
                        pending_ids=pending,
                        error="Album 内消息结果不完整" if status == UNKNOWN else "Album 内消息全部失败",
                        target=journal_target,
                    )
                    journal_terminal = True
                if not succeeded and not pending:
                    raise RuntimeError("Album 内消息全部发送失败")
                raise UploadUnknownError(
                    "Album 仅部分消息确认，已标记 UNKNOWN，暂不自动重试。"
                )
            if journal_active:
                journal.update(
                    selected_kind,
                    album_key,
                    CONFIRMED,
                    message_ids=succeeded,
                    succeeded_ids=succeeded,
                    target=journal_target,
                )
                journal_terminal = True
            return succeeded
        except SendResultUnknown as exc:
            if journal_active:
                delivery = exc.result if isinstance(exc.result, dict) else {}
                journal.update(
                    selected_kind,
                    album_key,
                    UNKNOWN,
                    message_ids=(delivery.get("succeeded", []) + delivery.get("failed", []) + delivery.get("pending", [])),
                    succeeded_ids=delivery.get("succeeded", []),
                    failed_ids=delivery.get("failed", []),
                    pending_ids=delivery.get("pending", []),
                    error=str(exc),
                    target=journal_target,
                )
            self._safe_diagnose_upload_failure(contents, items, exc)
            raise
        except TDLibError as exc:
            if journal_active:
                journal.failed(selected_kind, album_key, str(exc), target=journal_target)
            self._safe_diagnose_upload_failure(contents, items, exc)
            error_text = exc.message.lower()
            forbidden = any(
                marker in error_text
                for marker in (
                    "permission",
                    "forbidden",
                    "write_forbidden",
                    "not enough rights",
                    "rights_required",
                )
            )
            if getattr(cfg, "TARGET_MODE", "forum_topic") == "channel" and forbidden:
                raise RuntimeError("当前账号没有在该频道发布内容的权限。") from exc
            raise
        except (TimeoutError, TDLibCancelled) as exc:
            if journal_active:
                # A request that was submitted but never fully observed is
                # always UNKNOWN, including cancellation after submission.
                dispatched = bool(getattr(self, "_last_request_dispatched", False))
                if isinstance(exc, TDLibCancelled) and not submitted and not dispatched:
                    # Force-stop before sendMessage/sendMessageAlbum reached
                    # TDLib is a clean abort.  Keep a terminal FAILED record
                    # (rather than UNKNOWN) so the prepared journal cannot
                    # block the next run.
                    journal.failed(selected_kind, album_key, str(exc), target=journal_target)
                else:
                    journal.unknown(selected_kind, album_key, str(exc), target=journal_target)
            self._safe_diagnose_upload_failure(contents, items, exc)
            raise
        except Exception as exc:
            if isinstance(exc, UploadStalledError):
                # A stalled transfer must tear down this TDLib session before
                # control returns to the uploader.  The journal transition
                # below still records UNKNOWN, so a later run cannot resend
                # an Album whose server-side outcome is uncertain.
                self.cancel()
            if journal_active:
                if journal_terminal:
                    pass
                elif submitted or bool(getattr(self, "_last_request_dispatched", False)):
                    journal.unknown(selected_kind, album_key, str(exc), target=journal_target)
                else:
                    journal.failed(selected_kind, album_key, str(exc), target=journal_target)
            self._safe_diagnose_upload_failure(contents, items, exc)
            raise

    def close(self):
        try:
            if self.cancel_event.is_set():
                self._send_close_now()
            elif self.close_sent:
                pass
            else:
                self.request({"@type": "close"}, timeout=30)
                with self.close_lock:
                    self.close_sent = True
        except Exception:
            pass
        self.stop_event.set()
        with self.send_condition:
            self.send_condition.notify_all()
        receiver = getattr(self, "receiver_thread", None)
        if receiver is not None and receiver.is_alive():
            # td_receive() uses a one-second timeout.  Waiting without a
            # bounded join is intentional: creating a replacement client
            # while this thread is alive can consume the replacement client's
            # updates from tdjson's process-wide queue.
            receiver.join()
        with TDJsonClient._receiver_guard:
            if getattr(TDJsonClient, "_active_receiver", None) is receiver:
                TDJsonClient._active_receiver = None

    def should_rotate(self, confirmed_albums: int) -> bool:
        try:
            threshold = int(getattr(cfg, "TDLIB_SESSION_ROTATION_ALBUMS", 0))
        except (TypeError, ValueError):
            threshold = 0
        return threshold > 0 and confirmed_albums > 0 and confirmed_albums % threshold == 0

    def rotate_session(self, update_callback=None, device_model: str | None = None):
        """Rotate after a caller has finalized a confirmed Album.

        The old client is fully closed before constructing the replacement, so
        two receiver threads never compete for tdjson.td_receive().
        """

        callback = update_callback
        if callback is not None:
            self.remove_update_callback(callback)
        ui = self.ui
        model = device_model or self.device_model
        self.close()
        replacement = None
        try:
            replacement = type(self)(ui, model)
            replacement.login()
            replacement.refresh_account_limits()
            replacement.set_fast_options()
            replacement.validate_target()
        except Exception:
            if replacement is not None:
                try:
                    replacement.close()
                except Exception:
                    pass
            raise
        if callback is not None:
            replacement.add_update_callback(callback)
        write_app_log(
            "INFO",
            f"TDLib session 已在 Album 边界轮换：{model}",
            source="tdlib",
        )
        return replacement
