from __future__ import annotations

from collections import deque
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tdlib_media_uploader.telegram.auth import (  # noqa: E402
    AuthAction,
    AuthState,
    authorization_transition,
    build_authentication_request,
    extract_authorization_state,
)
from tdlib_media_uploader.telegram.client import (  # noqa: E402
    TDLibClient,
    TDLibError,
    build_send_request,
    formatted_text,
)
from tdlib_media_uploader.telegram.limits import (  # noqa: E402
    CaptionLimitError,
    IMAGE_MAX_BYTES,
    VIDEO_PREMIUM_MAX_BYTES,
    VIDEO_STANDARD_MAX_BYTES,
    account_limits_from_options,
    classify_image_size,
    classify_video_size,
    validate_caption,
)
from tdlib_media_uploader.telegram.send_result import (  # noqa: E402
    SendResult,
    map_send_result,
)
from tdlib_media_uploader.telegram.target import (  # noqa: E402
    TargetValidationError,
    canonical_target,
    parse_target,
    target_identity,
    validate_target,
)


class QueueTransport:
    """Deterministic fake transport; it never opens a network connection."""

    def __init__(self, responses=()):
        self.responses = deque(responses) if not callable(responses) else deque()
        self.responder = responses if callable(responses) else None
        self.sent = []
        self.closed = False

    def send(self, request):
        self.sent.append(dict(request))
        if self.responder is not None:
            self.responses.append(self.responder(request))

    def receive(self, timeout):
        del timeout
        return self.responses.popleft() if self.responses else None

    def close(self):
        self.closed = True


class TelegramLimitsTest(unittest.TestCase):
    def test_video_boundaries_are_exact(self):
        self.assertEqual(VIDEO_STANDARD_MAX_BYTES, 4000 * 524_288)
        self.assertEqual(VIDEO_PREMIUM_MAX_BYTES, 8000 * 524_288)
        self.assertEqual(classify_video_size(VIDEO_STANDARD_MAX_BYTES).status, "allowed")
        self.assertEqual(
            classify_video_size(VIDEO_STANDARD_MAX_BYTES + 1).status,
            "requires_premium",
        )
        self.assertEqual(
            classify_video_size(VIDEO_STANDARD_MAX_BYTES + 1, is_premium=True).status,
            "allowed",
        )
        self.assertEqual(
            classify_video_size(VIDEO_PREMIUM_MAX_BYTES).status,
            "requires_premium",
        )
        self.assertEqual(
            classify_video_size(VIDEO_PREMIUM_MAX_BYTES, is_premium=True).status,
            "allowed",
        )
        self.assertEqual(classify_video_size(VIDEO_PREMIUM_MAX_BYTES + 1).status, "oversize")

    def test_image_boundary_and_compression_decision(self):
        self.assertEqual(classify_image_size(IMAGE_MAX_BYTES).status, "allowed")
        self.assertEqual(classify_image_size(IMAGE_MAX_BYTES + 1).status, "oversize")
        self.assertEqual(
            classify_image_size(IMAGE_MAX_BYTES + 1, compression_enabled=True).status,
            "requires_compression",
        )

    def test_caption_limit_is_runtime_authoritative(self):
        self.assertEqual(validate_caption("x" * 1024, 1024), "x" * 1024)
        with self.assertRaises(CaptionLimitError):
            validate_caption("x" * 1025, 1024)
        limits = account_limits_from_options(
            {"@type": "optionValueBoolean", "value": True},
            {"@type": "optionValueInteger", "value": 777},
        )
        self.assertTrue(limits.is_premium)
        self.assertEqual(limits.caption_length_max, 777)


class TelegramTargetTest(unittest.TestCase):
    def test_forum_and_channel_canonical_identity_ignore_irrelevant_fields(self):
        forum_a = canonical_target(
            {
                "target_mode": "forum_topic",
                "chat_id": 100,
                "forum_topic_id": 10,
                "channel_chat_id": 999,
            }
        )
        forum_b = canonical_target(
            {
                "target_mode": "forum_topic",
                "chat_id": 100,
                "forum_topic_id": 10,
                "channel_chat_id": 888,
            }
        )
        channel_a = canonical_target(
            {
                "target_mode": "channel",
                "channel_chat_id": 200,
                "forum_topic_id": 10,
            }
        )
        channel_b = canonical_target(
            {
                "target_mode": "channel",
                "channel_chat_id": 200,
                "forum_topic_id": 999,
            }
        )
        self.assertEqual(forum_a, forum_b)
        self.assertEqual(channel_a, channel_b)
        self.assertNotEqual(target_identity(forum_a), target_identity(channel_a))
        self.assertEqual(parse_target({"target_mode": "channel", "chat_id": 200}).chat_id, 200)

    def test_chat_and_topic_mode_validation(self):
        forum_chat = {
            "id": 100,
            "type": {"@type": "chatTypeSupergroup", "is_channel": False},
        }
        topic = {"@type": "forumTopic", "forum_topic_id": 10}
        self.assertEqual(
            validate_target(
                forum_chat,
                {"target_mode": "forum_topic", "chat_id": 100, "forum_topic_id": 10},
                topic=topic,
            ).forum_topic_id,
            10,
        )
        with self.assertRaises(TargetValidationError):
            validate_target(
                forum_chat,
                {"target_mode": "channel", "channel_chat_id": 100},
            )

        with self.assertRaises(TargetValidationError):
            validate_target(
                forum_chat,
                {"target_mode": "forum_topic", "chat_id": 100, "forum_topic_id": 10},
                topic={"@type": "forumTopic", "forum_topic_id": "invalid"},
            )

    def test_send_request_uses_topic_only_for_forum_mode(self):
        contents = ({"@type": "inputMessagePhoto"},)
        forum = build_send_request(
            contents,
            {"target_mode": "forum_topic", "chat_id": 100, "forum_topic_id": 10},
        )
        channel = build_send_request(
            contents,
            {"target_mode": "channel", "channel_chat_id": 200, "forum_topic_id": 999},
        )
        self.assertEqual(forum["topic_id"]["forum_topic_id"], 10)
        self.assertIsNone(channel["topic_id"])


class TelegramAuthTest(unittest.TestCase):
    def test_nested_authorization_update_is_extracted(self):
        update = {
            "@type": "updateAuthorizationState",
            "authorization_state": {
                "@type": AuthState.WAIT_CODE.value,
                "code_info": {"type": {"@type": "authenticationCodeTypeTelegramMessage"}},
            },
        }
        self.assertEqual(extract_authorization_state(update), AuthState.WAIT_CODE.value)
        transition = authorization_transition(update)
        self.assertEqual(transition.action, AuthAction.REQUEST_CODE.value)
        self.assertTrue(transition.requires_input)

    def test_authentication_requests_preserve_nested_email_code_shape(self):
        update = {
            "@type": "updateAuthorizationState",
            "authorization_state": {"@type": AuthState.WAIT_EMAIL_CODE.value},
        }
        self.assertEqual(
            build_authentication_request(update, "123456"),
            {
                "@type": "checkAuthenticationEmailCode",
                "code": {
                    "@type": "emailAddressAuthenticationCode",
                    "code": "123456",
                },
            },
        )
        self.assertEqual(
            build_authentication_request(AuthState.WAIT_CODE, "654321")["@type"],
            "checkAuthenticationCode",
        )

    def test_other_device_link_and_ready_state(self):
        transition = authorization_transition(
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {
                    "@type": AuthState.WAIT_OTHER_DEVICE_CONFIRMATION.value,
                    "link": "tg://login?token=abc",
                },
            }
        )
        self.assertEqual(transition.link, "tg://login?token=abc")
        ready = authorization_transition(
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": AuthState.READY.value},
            }
        )
        self.assertTrue(ready.ready)


class TelegramSendResultTest(unittest.TestCase):
    def test_confirmed_failed_and_unknown_semantics(self):
        confirmed = map_send_result([{"id": 1}, {"id": 2}])
        self.assertIsInstance(confirmed, SendResult)
        self.assertEqual(confirmed.status, "CONFIRMED")

        failed = map_send_result(
            [
                {"id": 1, "sending_state": {"@type": "messageSendingStateFailed"}},
                {"id": 2, "sending_state": {"@type": "messageSendingStateFailed"}},
            ]
        )
        self.assertEqual(failed.status, "FAILED")

        unknown = map_send_result(
            [{"id": 1}, {"id": 2, "sending_state": {"@type": "messageSendingStatePending"}}]
        )
        self.assertEqual(unknown.status, "UNKNOWN")
        self.assertEqual(unknown.pending_ids, (2,))

    def test_updates_replace_pending_with_new_message_id_or_failure(self):
        result = map_send_result(
            [
                {"id": -1, "sending_state": {"@type": "messageSendingStatePending"}},
                {"id": -2, "sending_state": {"@type": "messageSendingStatePending"}},
            ],
            [
                {
                    "@type": "updateMessageSendSucceeded",
                    "old_message_id": -1,
                    "message": {"id": 11},
                },
                {
                    "@type": "updateMessageSendFailed",
                    "old_message_id": -2,
                    "error": {"code": 400, "message": "forbidden"},
                },
            ],
        )
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(result.succeeded_ids, (11,))
        self.assertEqual(result.failed_ids, (-2,))
        self.assertEqual(result.pending_ids, ())

    def test_timeout_and_incomplete_album_are_unknown(self):
        timeout = map_send_result(
            [{"id": -1, "sending_state": {"@type": "messageSendingStatePending"}}],
            timed_out=True,
        )
        incomplete = map_send_result([{"id": 1}], expected_count=2, incomplete=True)
        self.assertEqual(timeout.status, "UNKNOWN")
        self.assertEqual(incomplete.status, "UNKNOWN")


class TelegramClientTest(unittest.TestCase):
    def test_injected_transport_request_has_no_native_dependency(self):
        transport = QueueTransport()

        def response_for(request):
            return {"@type": "ok", "@extra": request["@extra"]}

        transport.responder = response_for
        client = TDLibClient(transport, request_timeout=0.1)
        response = client.request({"@type": "getOption", "name": "version"})
        self.assertEqual(response["@type"], "ok")
        self.assertEqual(transport.sent[0]["@type"], "getOption")

    def test_error_response_is_mapped_without_network(self):
        transport = QueueTransport()

        def response_for(request):
            return {
                "@type": "error",
                "@extra": request["@extra"],
                "code": 400,
                "message": "bad target",
            }

        transport.responder = response_for
        client = TDLibClient(transport, request_timeout=0.1)
        with self.assertRaises(TDLibError):
            client.request({"@type": "getChat", "chat_id": 0})

    def test_runtime_caption_limit_blocks_before_transport(self):
        transport = QueueTransport()
        client = TDLibClient(transport, request_timeout=0.1)
        client.caption_length_limit = 3
        content = {"@type": "inputMessagePhoto", "caption": formatted_text("four")}
        with self.assertRaises(CaptionLimitError):
            client.send_contents(
                (content,),
                {"target_mode": "channel", "channel_chat_id": 200},
            )
        self.assertEqual(transport.sent, [])


if __name__ == "__main__":
    unittest.main()
