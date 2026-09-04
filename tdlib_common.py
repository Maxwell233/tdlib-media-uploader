# -*- coding: utf-8 -*-
"""TDLib shared client/helpers for the media uploaders."""

from __future__ import annotations

import getpass
import importlib.metadata
import json
import queue
import threading
import time
import uuid
from pathlib import Path

import tdjson

import app_config as cfg

REQUIRED_TDJSON_VERSION = "1.8.64.post1"
PROJECT_DIR = Path(__file__).resolve().parent
TDLIB_DATABASE_DIR = PROJECT_DIR / "tdlib_data"
TDLIB_FILES_DIR = PROJECT_DIR / "tdlib_files"


class TDLibError(RuntimeError):
    def __init__(self, code: int, message: str):
        self.code = int(code)
        self.message = str(message)
        super().__init__(f"TDLib error {self.code}: {self.message}")


class TDLibCancelled(RuntimeError):
    """Raised when a GUI or caller requests an immediate upload stop."""


def verify_tdjson_version() -> str:
    try:
        installed = importlib.metadata.version("tdjson")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("未安装 tdjson，请先运行 .\\setup.ps1") from exc
    if installed != REQUIRED_TDJSON_VERSION:
        raise RuntimeError(
            "tdjson 版本不符合要求。\n\n"
            f"当前：{installed}\n要求：{REQUIRED_TDJSON_VERSION}\n\n"
            "请运行 .\\setup.ps1 重新安装固定版本。"
        )
    return installed


def formatted_text(text: str = "") -> dict:
    return {"@type": "formattedText", "text": text, "entities": []}


class HeadlessUI:
    """Minimal internal adapter used until the GUI binds its own signals.

    Upload modules still call a small presentation interface while scanning
    and sending.  Keeping this no-op implementation in the backend lets the
    project ship a GUI-only entry point without carrying a second terminal UI
    implementation or an extra display dependency.
    """

    def register_client(self, client):
        self.client = client

    def log(self, text=""):
        pass

    def info(self, text):
        pass

    def success(self, text):
        pass

    def warning(self, text):
        pass

    def error(self, text):
        pass

    def banner(self, title, subtitle="", *, accent="cyan"):
        pass

    def summary(self, title, rows, *, kind="VIDEO"):
        pass

    def files(self, title, columns, rows, *, kind="VIDEO", caption=None):
        pass

    def groups(self, title, rows, *, kind="VIDEO"):
        pass

    def target(self, chat_title, topic_name, chat_id, topic_id):
        pass

    def album(self, *, kind, title, subtitle="", rows=None):
        pass

    def progress(self, **kwargs):
        pass

    def finish(self):
        pass

    def cancelled(self):
        pass

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


class TDJsonClient:
    """Small synchronous wrapper around TDLib's JSON interface."""

    def __init__(self, ui, device_model: str):
        self.ui = ui
        self.device_model = device_model
        TDLIB_DATABASE_DIR.mkdir(parents=True, exist_ok=True)
        TDLIB_FILES_DIR.mkdir(parents=True, exist_ok=True)

        self.execute({
            "@type": "setLogVerbosityLevel",
            "new_verbosity_level": int(cfg.TDLIB_LOG_VERBOSITY),
        })

        self.client_id = tdjson.td_create_client_id()
        self.pending: dict[str, queue.Queue] = {}
        self.pending_lock = threading.Lock()
        self.auth_queue: queue.Queue = queue.Queue()
        self.send_events: dict[int, tuple[str, dict]] = {}
        self.send_condition = threading.Condition()
        # Keep close idempotent because a stop request can arrive while a
        # sendMessage request is still waiting for its response.
        self.close_lock = threading.Lock()
        self.close_sent = False
        self.update_callbacks = []
        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()
        register_client = getattr(self.ui, "register_client", None)
        if callable(register_client):
            register_client(self)
        self.receiver_thread = threading.Thread(
            target=self._receiver_loop,
            name="TDLibReceiver",
            daemon=True,
        )
        self.receiver_thread.start()

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
        """Stop promptly and cooperatively, including an active TDLib upload."""
        self.cancel_event.set()
        # TDLib 1.8.64 does not expose a generic cancelUploadFile method for
        # media sent through sendMessage/sendMessageAlbum.  Closing the client
        # is the supported way to abort the in-flight transfer; send it here
        # immediately instead of waiting for the worker's finally block.
        self._send_close_now()
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
        self.send_raw(payload)
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
                    with self.send_condition:
                        self.send_events[old_id] = ("success", obj)
                        self.send_condition.notify_all()
                elif kind == "updateMessageSendFailed":
                    old_id = obj.get("old_message_id")
                    with self.send_condition:
                        self.send_events[old_id] = ("failed", obj)
                        self.send_condition.notify_all()
                for callback in list(self.update_callbacks):
                    try:
                        callback(obj)
                    except Exception:
                        pass
            except Exception as exc:
                self.ui.warning(f"TDLib receiver 异常：{type(exc).__name__}: {exc}")
                time.sleep(1)

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
                self.request({
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
                    "device_model": self.device_model,
                    "system_version": "Windows",
                    "application_version": cfg.APP_VERSION,
                })
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
        final_ids = []
        for message in messages:
            message_id = message.get("id")
            sending_state = message.get("sending_state")
            if not sending_state:
                final_ids.append(message_id)
                continue
            if sending_state.get("@type") == "messageSendingStateFailed":
                raise RuntimeError(f"消息立即进入失败状态：{message_id}")
            pending_ids.append(message_id)

        if not pending_ids:
            return final_ids

        deadline = time.monotonic() + timeout
        results = {}
        with self.send_condition:
            while len(results) < len(pending_ids):
                self._raise_if_cancelled()
                for old_id in pending_ids:
                    if old_id in results:
                        continue
                    event = self.send_events.get(old_id)
                    if event is None:
                        continue
                    status, update = event
                    if status == "failed":
                        error_obj = update.get("error", {})
                        raise TDLibError(
                            error_obj.get("code", 0),
                            error_obj.get("message", "message send failed"),
                        )
                    results[old_id] = update
                if len(results) >= len(pending_ids):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("等待 Telegram 确认发送成功超时")
                self.send_condition.wait(min(1, remaining))

        sent_ids = list(final_ids)
        for old_id in pending_ids:
            sent_ids.append(results[old_id].get("message", {}).get("id"))
            with self.send_condition:
                self.send_events.pop(old_id, None)
        return sent_ids

    def send_contents(self, contents, progress=None, items=None):
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
                    raise RuntimeError(
                        f"TDLib sendMessageAlbum 返回消息数量异常：{len(messages)}/{len(contents)}"
                    )
        except TDLibError as exc:
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
        if progress is not None and items is not None:
            progress.register_messages(messages, items)
        return self.wait_for_send_results(messages)

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
        if self.receiver_thread.is_alive():
            self.receiver_thread.join(timeout=3)
