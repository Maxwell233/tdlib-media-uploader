"""Only a definite rejection or an explicitly retryable failed message is retried."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tdlib_media_uploader.telegram import tdlib_common as td
from tdlib_media_uploader.telegram.send_result import map_send_result
import test_telegram_runtime as runtime_tests


def failed(message_id, retry_after=0, **options):
    return {"id": message_id, "sending_state": {
        "@type": "messageSendingStateFailed", "can_retry": True,
        "retry_after": retry_after, **options,
    }}


class RateLimitRecoveryTests(unittest.TestCase):
    def client(self):
        client = runtime_tests.TelegramRuntimeTest().client()
        client._config = SimpleNamespace(TDLIB_MESSAGE_SEND_TIMEOUT=0)
        client._wait_for_retry = Mock()
        return client

    def test_failed_update_keeps_retry_metadata_and_actual_failed_message_id(self):
        result = map_send_result([{"id": -1, "sending_state": {"@type": "messageSendingStatePending"}}], [
            {"@type": "updateMessageSendFailed", "old_message_id": -1,
             "message": failed(41, 7.5), "error": {"code": 429, "message": "retry after 7.5"}},
        ])
        self.assertEqual(result.failed_ids, (41,))
        self.assertEqual(result.item_results[0]["retry_after"], 7.5)
        self.assertTrue(result.item_results[0]["can_retry"])

    def test_explicit_request_limit_waits_and_retries_once(self):
        for text in ("Too Many Requests: retry after 3", "FLOOD_WAIT_3"):
            with self.subTest(text=text):
                client = self.client()
                client._request_once = Mock(side_effect=[td.TDLibError(429, text), {"@type": "ok"}])
                self.assertEqual(client.request({"@type": "sendMessage"})["@type"], "ok")
                client._wait_for_retry.assert_called_once_with(3)
                self.assertEqual(client._request_once.call_count, 2)

    def test_timeout_is_not_retried(self):
        client = self.client()
        client._request_once = Mock(side_effect=TimeoutError("response lost"))
        with self.assertRaises(TimeoutError):
            client.request({"@type": "sendMessage"})
        client._request_once.assert_called_once()
        client._wait_for_retry.assert_not_called()

    def test_retry_wait_can_be_cancelled_immediately(self):
        client = self.client()
        client.cancel_event.set()
        client.ui.rate_limit = Mock()
        with self.assertRaises(td.TDLibCancelled) as caught:
            td.TDJsonClient._wait_for_retry(client, 60)
        self.assertIs(caught.exception.submitted, False)
        self.assertEqual(client.ui.rate_limit.call_args.args, (0,))

    def test_sorted_resend_ids_keep_original_file_positions(self):
        client = self.client()
        client.request = Mock(return_value={"messages": [{"id": 110}, {"id": 120}]})
        result = client.wait_for_send_results([failed(20, 2), failed(10, 5)], chat_id=-1001)
        self.assertEqual(client.request.call_args.args[0]["message_ids"], [10, 20])
        self.assertEqual(result["succeeded"], [120, 110])
        client._wait_for_retry.assert_called_once_with(5)

    def test_resend_uses_failed_message_id_instead_of_previous_temporary_id(self):
        client = self.client()
        client.send_events[-1] = ("failed", {"message": failed(41), "error": {"code": 429}})
        client.request = Mock(return_value={"messages": [{"id": 101}]})
        result = client.wait_for_send_results([
            {"id": -1, "sending_state": {"@type": "messageSendingStatePending"}},
        ], chat_id=-1001)
        self.assertEqual(client.request.call_args.args[0]["message_ids"], [41])
        self.assertEqual(result["succeeded"], [101])

    def test_pending_and_successful_messages_are_not_resent(self):
        client = self.client()
        snapshots = []
        def resend(query):
            self.assertEqual(query["message_ids"], [22])
            self.assertEqual([row["status"] for row in snapshots[-1]["item_results"]],
                             ["succeeded", "pending", "pending"])
            return {"messages": [{"id": 222}]}
        client.request = Mock(side_effect=resend)
        with self.assertRaises(td.SendResultUnknown) as caught:
            client.wait_for_send_results([
                {"id": 11}, failed(22),
                {"id": -3, "sending_state": {"@type": "messageSendingStatePending"}},
            ], chat_id=-1001, on_result=snapshots.append)
        self.assertEqual(caught.exception.result["succeeded"], [11, 222])
        self.assertEqual(caught.exception.result["pending"], [-3])
        client.request.assert_called_once()

    def test_lost_resend_response_remains_pending(self):
        client = self.client()
        client.request = Mock(side_effect=TimeoutError("resend response lost"))
        with self.assertRaises(td.SendResultUnknown) as caught:
            client.wait_for_send_results([{"id": 11}, failed(22)], chat_id=-1001)
        self.assertEqual(caught.exception.result["succeeded"], [11])
        self.assertEqual(caught.exception.result["failed"], [])
        self.assertEqual(caught.exception.result["pending"], [22])

    def test_user_action_or_payment_is_not_automatically_authorized(self):
        for options in ({"need_another_sender": True}, {"need_another_reply_quote": True},
                        {"need_drop_reply": True}, {"required_paid_message_star_count": 1},
                        {"can_retry": False}):
            with self.subTest(options=options):
                client = self.client()
                client.request = Mock()
                result = client.wait_for_send_results([failed(22, **options)], chat_id=-1001)
                self.assertEqual(result["status"], "FAILED")
                client.request.assert_not_called()

    def test_null_or_repeat_failure_does_not_loop(self):
        for response in (None, failed(33)):
            with self.subTest(response=response):
                client = self.client()
                client.request = Mock(return_value={"messages": [response]})
                result = client.wait_for_send_results([failed(22)], chat_id=-1001)
                self.assertEqual(result["status"], "FAILED")
                client.request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
