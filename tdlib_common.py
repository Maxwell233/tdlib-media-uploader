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


def topic_object() -> dict:
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
        self.update_callbacks = []
        self.stop_event = threading.Event()
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

    def request(self, query: dict, timeout: int | float | None = None):
        if timeout is None:
            timeout = cfg.TDLIB_REQUEST_TIMEOUT
        extra = "req:" + uuid.uuid4().hex
        payload = dict(query)
        payload["@extra"] = extra
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self.pending_lock:
            self.pending[extra] = waiter
        self.send_raw(payload)
        try:
            response = waiter.get(timeout=timeout)
        except queue.Empty as exc:
            with self.pending_lock:
                self.pending.pop(extra, None)
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

    def login(self):
        try:
            self.request({"@type": "getOption", "name": "version"}, timeout=30)
        except Exception:
            pass

        while True:
            try:
                state = self.auth_queue.get(timeout=120)
            except queue.Empty as exc:
                raise TimeoutError("等待 TDLib 授权状态超时") from exc

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
            elif state_type == "authorizationStateWaitPhoneNumber":
                phone = input("请输入 Telegram 手机号（国际格式，例如 +491234...）：").strip()
                self.request({"@type": "setAuthenticationPhoneNumber", "phone_number": phone})
            elif state_type == "authorizationStateWaitCode":
                code = input("请输入 Telegram 登录验证码：").strip()
                self.request({"@type": "checkAuthenticationCode", "code": code})
            elif state_type == "authorizationStateWaitPassword":
                password = getpass.getpass("请输入 Telegram 两步验证密码：")
                self.request({"@type": "checkAuthenticationPassword", "password": password})
            elif state_type == "authorizationStateWaitEmailAddress":
                email = input("请输入 Telegram 要求的邮箱地址：").strip()
                self.request({"@type": "setAuthenticationEmailAddress", "email_address": email})
            elif state_type == "authorizationStateWaitEmailCode":
                code = input("请输入邮箱验证码：").strip()
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
        self.ui.info("当前 TDLib 数据库尚未加载目标群，开始加载聊天列表。")
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
            "找不到 CHAT_ID。请确认登录账号仍在该群中，且 config.toml 中的 chat_id 正确。"
        )

    def validate_target(self):
        chat = self.ensure_target_chat()
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
                self.send_condition.wait(min(5, remaining))

        sent_ids = list(final_ids)
        for old_id in pending_ids:
            sent_ids.append(results[old_id].get("message", {}).get("id"))
            with self.send_condition:
                self.send_events.pop(old_id, None)
        return sent_ids

    def send_contents(self, contents, progress=None, items=None):
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
        if progress is not None and items is not None:
            progress.register_messages(messages, items)
        return self.wait_for_send_results(messages)

    def close(self):
        try:
            self.request({"@type": "close"}, timeout=30)
        except Exception:
            pass
        self.stop_event.set()
        if self.receiver_thread.is_alive():
            self.receiver_thread.join(timeout=3)
