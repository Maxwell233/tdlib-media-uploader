"""Exercise the actual native-client facade without loading a Telegram session."""
from collections import deque
import json
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tdlib_media_uploader.telegram import tdlib_common as td


class TelegramRuntimeTest(unittest.TestCase):
    def client(self):
        client = object.__new__(td.TDJsonClient)
        client.client_id = 42
        client.cancel_event = threading.Event()
        client.stop_event = threading.Event()
        client.closed_event = threading.Event()
        client.pending = {}
        client.pending_lock = threading.Lock()
        client.auth_queue = queue.Queue()
        client.send_events = {}
        client.send_condition = threading.Condition()
        client.update_callbacks = []
        client.ui = SimpleNamespace(**{name: Mock() for name in (
            "info", "warning", "log", "success", "prompt", "target")})
        client._configure_proxy = Mock()
        return client

    def receive(self, client, values):
        values = deque(values)

        def receive(_timeout):
            value = values.popleft()
            if not values:
                client.stop_event.set()
            return json.dumps(dict(value, **{"@client_id": client.client_id}))

        client.stop_event.clear()
        with patch.object(td.tdjson, "td_receive", side_effect=receive):
            client._receiver_loop()

    def test_request_matches_extra_and_maps_native_error(self):
        for response in ({"@type": "ok"}, {
            "@type": "error", "code": 400, "message": "bad target",
        }):
            with self.subTest(response=response):
                client = self.client()

                def send(payload):
                    self.receive(client, [
                        {"@type": "ok", "@extra": "unrelated"},
                        dict(response, **{"@extra": payload["@extra"]}),
                    ])

                client.send_raw = Mock(side_effect=send)
                if response["@type"] == "error":
                    with self.assertRaisesRegex(td.TDLibError, "400: bad target"):
                        client.request({"@type": "getChat", "chat_id": 0})
                else:
                    self.assertEqual(client.request({"@type": "getOption", "name": "version"})["@type"], "ok")
                    self.assertEqual(client.send_raw.call_args.args[0]["@type"], "getOption")
                self.assertEqual(client.pending, {})

    def test_caption_limit_blocks_before_transport(self):
        client = self.client()
        client.caption_length_limit = 3
        client.request = Mock()
        with self.assertRaisesRegex(ValueError, "Caption"):
            client.send_contents([{
                "@type": "inputMessagePhoto", "caption": td.formatted_text("four"),
            }], target={"target_mode": "channel", "channel_chat_id": 200})
        client.request.assert_not_called()

    def test_runtime_account_options_supply_premium_and_caption_limits(self):
        client = self.client()
        client.is_premium = None
        client.caption_length_limit = None
        options = {"is_premium": {"@type": "optionValueBoolean", "value": True},
                   "message_caption_length_max": {"@type": "optionValueInteger", "value": 777}}
        client.request = Mock(side_effect=lambda payload, **kwargs: options[payload["name"]])
        self.assertEqual(client.refresh_account_limits(), (True, 777))
        self.assertTrue(client.is_premium)
        self.assertEqual(client.caption_length_limit, 777)

    def test_actual_target_lookup_accepts_matching_mode_and_rejects_wrong_mode(self):
        for mode, is_channel in (("forum_topic", False), ("channel", True)):
            with self.subTest(mode=mode):
                client = self.client()
                client._config = SimpleNamespace(TARGET_MODE=mode, CHAT_ID=100, FORUM_TOPIC_ID=10)
                chat = {"id": 100, "title": "Test", "type": {
                    "@type": "chatTypeSupergroup", "is_channel": is_channel}}
                client.ensure_target_chat = Mock(return_value=chat)
                topic = {"@type": "forumTopic", "info": {"forum_topic_id": 10, "name": "Topic"}}
                client.request = Mock(return_value=topic)
                self.assertEqual(client.validate_target(), (chat, None if is_channel else topic))
                if is_channel:
                    client.request.assert_not_called()
                else:
                    client.request.assert_called_once_with({
                        "@type": "getForumTopic", "chat_id": 100, "forum_topic_id": 10})
                    client.request.side_effect = td.TDLibError(400, "TOPIC_ID_INVALID")
                    with self.assertRaisesRegex(RuntimeError, "Topic 无法解析"):
                        client.validate_target()
                client.ui.target.assert_called_once()
                chat["type"]["is_channel"] = not is_channel
                with self.assertRaisesRegex(RuntimeError, "频道"):
                    client.validate_target()

    def test_actual_send_uses_topic_only_for_forum_target(self):
        for target, expected in (
            ({"target_mode": "forum_topic", "chat_id": 100, "forum_topic_id": 10},
             {"@type": "messageTopicForum", "forum_topic_id": 10}),
            ({"target_mode": "channel", "channel_chat_id": 200, "forum_topic_id": 999}, None),
        ):
            with self.subTest(target=target):
                client = self.client()
                client.request = Mock(return_value={"id": 17})
                result = client.send_contents([{
                    "@type": "inputMessagePhoto", "photo": {"@type": "inputFileId", "id": 1},
                }], target=target)
                self.assertEqual(result, [17])
                self.assertEqual(client.request.call_args.args[0]["topic_id"], expected)

    def test_receiver_and_login_preserve_code_and_nested_email_code_payloads(self):
        for state, request in (
            ("authorizationStateWaitCode", {"@type": "checkAuthenticationCode", "code": "123456"}),
            ("authorizationStateWaitEmailCode", {"@type": "checkAuthenticationEmailCode",
             "code": {"@type": "emailAddressAuthenticationCode", "code": "123456"}}),
        ):
            with self.subTest(state=state):
                client = self.client()
                client.ui.prompt.return_value = " 123456 "
                client.request = Mock(return_value={})
                self.receive(client, [
                    {"@type": "updateAuthorizationState", "authorization_state": {"@type": state}},
                    {"@type": "updateAuthorizationState", "authorization_state": {"@type": "authorizationStateReady"}},
                ])
                client.login()
                self.assertEqual(client.request.call_args.args[0], request)
                client.ui.prompt.assert_called_once()
                client.ui.success.assert_called_once()

    def test_login_shows_other_device_link_and_returns_on_ready(self):
        client = self.client()
        client.request = Mock(return_value={})
        self.receive(client, [
            {"@type": "updateAuthorizationState", "authorization_state": {
                "@type": "authorizationStateWaitOtherDeviceConfirmation", "link": "tg://login?token=abc"}},
            {"@type": "updateAuthorizationState", "authorization_state": {"@type": "authorizationStateReady"}},
        ])
        client.login()
        client.ui.warning.assert_called_once()
        client.ui.log.assert_called_once_with("tg://login?token=abc")
        client.ui.success.assert_called_once()
        client.ui.prompt.assert_not_called()

    def test_login_preserves_password_whitespace(self):
        client = self.client()
        client.request = Mock(return_value={})
        client.ui.prompt.return_value = " secret "
        for state in ("authorizationStateWaitPassword", "authorizationStateReady"):
            client.auth_queue.put({"@type": state})
        client.login()
        self.assertEqual(client.request.call_args.args[0], {
            "@type": "checkAuthenticationPassword", "password": " secret ",
        })
        self.assertTrue(client.ui.prompt.call_args.kwargs["password"])


if __name__ == "__main__":
    unittest.main()
