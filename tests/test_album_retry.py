import threading
import time
from collections import deque
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import TARGET, media
from tdlib_media_uploader.contracts import UploadContext
from tdlib_media_uploader.core.models import AlbumAttemptEvent, AlbumPlan, BatchStatus, ScanResult
from tdlib_media_uploader.core.upload_journal import InflightJournal
from tdlib_media_uploader.core.upload_state import UploadState
from tdlib_media_uploader.gui.integration import GuiCancelToken, GuiUploadProgress, TDLibSender
from tdlib_media_uploader.telegram.tdlib_common import TDJsonClient, TDLibCancelled, TDLibError, formatted_text
from tdlib_media_uploader.upload.engine import UploadEngine
from tdlib_media_uploader.upload.retry import AlbumRetryPolicy


class SimulatedTDLib(TDJsonClient):
    def __init__(self, responses, journal):
        self._config = SimpleNamespace(TDLIB_MESSAGE_SEND_TIMEOUT=0.02, TDLIB_REQUEST_TIMEOUT=0.02)
        self.ui = SimpleNamespace(warning=lambda message: None, info=lambda message: None)
        self.send_condition = threading.Condition()
        self.cancel_event = threading.Event()
        self.send_events = {}
        self.responses = deque(responses)
        self.requests = []
        self.rate_limit_retry = []
        self.request_waits = []
        self.pending_lock = threading.Lock()
        self.pending = {}
        self.snapshots = []
        self.captions = []
        self.journal = journal
        self.message_id = 0

    def _safe_diagnose_upload_failure(self, contents, items, exc):
        pass

    def _wait_for_retry(self, seconds):
        self.request_waits.append(seconds)
        return super()._wait_for_retry(seconds)

    def request(self, payload, timeout=None, *, retry_rate_limit=True):
        if payload["@type"] != "resendMessages":
            self.rate_limit_retry.append(retry_rate_limit)
        return super().request(payload, timeout, retry_rate_limit=retry_rate_limit)

    def send_raw(self, payload):
        request = payload["@type"]
        self.requests.append(request)
        if request != "resendMessages":
            contents = payload.get("input_message_contents") or [payload["input_message_content"]]
            self.captions.append(contents[0]["caption"]["text"])
            record = next(value for value in self.journal.list_unresolved() if value["status"] == "PREPARED")
            self.snapshots.append(record)
        else:
            contents = payload["message_ids"]

        response = self.responses.popleft()
        if isinstance(response, TDLibError):
            self.pending.pop(payload["@extra"]).put({
                "@type": "error", "code": response.code, "message": response.message,
            })
            return
        if isinstance(response, TimeoutError):
            return  # No response reaches request(); preserve uncertainty.
        if isinstance(response, BaseException):
            raise response
        messages = []
        for state in (response if isinstance(response, tuple) else [response] * len(contents)):
            self.message_id += 1
            message = {"id": self.message_id}
            if state == "pending":
                message["sending_state"] = {"@type": "messageSendingStatePending"}
            elif state != "ok":
                code = 403 if state == "forbidden" else 429 if state == "flood" else 500
                message["sending_state"] = {
                    "@type": "messageSendingStateFailed",
                    "can_retry": state == "message_retry",
                    "retry_after": 0.04 if state == "flood" else 0,
                    "error": {"@type": "error", "code": code, "message": state},
                }
            messages.append(message)
        reply = messages[0] if request == "sendMessage" else {"messages": messages}
        self.pending.pop(payload["@extra"]).put(reply)


@pytest.fixture
def harness(tmp_path):
    journal = InflightJournal(tmp_path / "inflight")
    state = UploadState(kind="image", source_root=tmp_path, target=TARGET, state_dir=tmp_path / "state")
    paths = [tmp_path / f"file-{i}.jpg" for i in range(4)]
    for path in paths:
        path.write_bytes(b"photo or stable temporary artifact")
    media_items = tuple(media(path, tmp_path) for path in paths)
    plans = tuple(
        AlbumPlan(f"album-{number}", "image", tmp_path, "group", number, items, items, f"caption-{number}", TARGET)
        for number, items in ((1, media_items[:2]), (2, media_items[2:]))
    )

    def run(
        responses, *, enabled=True, count=2, delay=0, on_event=None,
        modify_state=None, modify_journal=None, single=False,
    ):
        client = SimulatedTDLib(responses, journal)
        cancel = client.cancel_event
        safe_stop = threading.Event()
        token = GuiCancelToken(cancel, safe_stop)
        events, builds = [], []
        if modify_state:
            modify_state(state)
        if modify_journal:
            modify_journal(journal)

        def emit(event):
            events.append(event)
            if on_event:
                on_event(event, cancel, safe_stop, paths)

        class Strategy:
            kind = "image"
            validate_source_snapshots = True

            def build_contents(self, plan, **kwargs):
                builds.append(plan.key)
                contents = []
                for i, item in enumerate(plan.pending_items):
                    artifact = tmp_path / f"prepared-{item.path.name}"
                    artifact.write_bytes(item.path.read_bytes())
                    contents.append({
                        "@type": "inputMessagePhoto",
                        "photo": {"@type": "inputPhoto", "photo": {"@type": "inputFileLocal", "path": str(artifact)}},
                        "caption": formatted_text(plan.caption if i == 0 else ""),
                    })
                return contents

        sender = TDLibSender(client, GuiUploadProgress("image"), None)
        context = UploadContext(
            tmp_path, TARGET, token, SimpleNamespace(emit=emit), "image", state, journal, sender,
            metadata={"album_retry": AlbumRetryPolicy(enabled, count, delay), "prefetch_next": True},
            scan_result=ScanResult(media_items),
            plans=plans if not single else (replace(plans[0], items=media_items[:1], pending_items=media_items[:1]),
                                            plans[1]),
        )
        result = UploadEngine().run(Strategy(), context=context)
        return result, client, events, builds

    return run, state, journal


@pytest.mark.parametrize("outcomes, extra, enabled, status, first_sends", [
    (["ok", "ok"], 2, True, "COMPLETED", 1),
    (["failed", "ok", "ok"], 2, True, "COMPLETED", 2),
    (["failed", "failed", "ok", "ok"], 2, True, "COMPLETED", 3),
    (["failed", "failed", "failed", "ok"], 2, True, "PARTIAL", 3),
    (["failed", "failed", "ok"], 1, True, "PARTIAL", 2),
    (["failed", "ok"], 2, False, "PARTIAL", 1),
])
def test_attempt_order_budget_journal_and_preparation(harness, outcomes, extra, enabled, status, first_sends):
    run, _, journal = harness
    result, client, events, builds = run(outcomes, count=extra, enabled=enabled)
    assert result.status == status
    assert client.captions == ["caption-1"] * first_sends + ["caption-2"]
    assert client.rate_limit_retry == [not enabled] * len(client.captions)
    attempts = client.snapshots[:first_sends]
    assert len({record["attempt_id"] for record in attempts}) == first_sends
    assert {record["album_key"] for record in attempts} == {"album-1"}
    assert [record["attempt_number"] for record in attempts] == list(range(1, first_sends + 1))
    if first_sends > 1:
        assert len(attempts[-1]["previous_attempts"]) == first_sends - 1
    assert [record["items"] for record in attempts] == [attempts[0]["items"]] * first_sends
    assert sorted(builds) == ["album-1", "album-2"]
    assert len(list(journal.root.parent.glob("prepared-*.jpg"))) == 4
    assert not journal.list_unresolved()
    assert len([event for event in events if isinstance(event, AlbumAttemptEvent)]) == first_sends + 1


@pytest.mark.parametrize("first,status,protected", [
    (("ok", "failed"), "UNKNOWN", True),
    (("pending", "failed"), "UNKNOWN", True),
    (TimeoutError("timeout"), "UNKNOWN", True),
    ("forbidden", "PARTIAL", False),
    (TDLibError(403, "CHAT_WRITE_FORBIDDEN"), "PARTIAL", False),
])
def test_ambiguous_and_permanent_errors_never_retry(harness, first, status, protected):
    run, _, journal = harness
    result, client, _, _ = run([first, "ok"])
    assert result.status == status
    assert client.captions == ["caption-1", "caption-2"]
    assert bool(journal.list_unresolved()) is protected
    if first == ("ok", "failed"):
        assert journal.list_unresolved()[0]["succeeded_ids"]


def test_checkpoint_failure_preserves_confirmed_without_retry(harness, monkeypatch):
    run, _, journal = harness

    def fail(state):
        def cannot_save(*args):
            raise OSError("checkpoint failed")
        monkeypatch.setattr(state, "mark_album_completed", cannot_save)

    result, client, _, _ = run(["ok", "ok"], modify_state=fail)
    assert result.status == "PARTIAL"
    assert client.captions == ["caption-1", "caption-2"]
    assert {value["status"] for value in journal.list_unresolved()} == {BatchStatus.CONFIRMED}


@pytest.mark.parametrize("write_status", ["PREPARED", "FAILED"])
def test_journal_write_failure_prevents_retry_and_retains_protection(harness, monkeypatch, write_status):
    run, _, journal = harness

    def fail(journal):
        real = journal._write

        def broken(path, record):
            if record["album_key"] == "album-1" and record["status"] == write_status:
                raise OSError("journal failed")
            return real(path, record)
        monkeypatch.setattr(journal, "_write", broken)

    sends = ["failed", "ok"] if write_status == "FAILED" else ["ok"]
    result, client, _, _ = run(sends, modify_journal=fail)
    assert result.status == "UNKNOWN"
    assert client.captions.count("caption-1") == (write_status == "FAILED")
    if write_status == "FAILED":
        assert journal.list_unresolved()[0]["status"] == "UNKNOWN"


@pytest.mark.parametrize("action", ["safe", "immediate", "modify"])
def test_wait_is_cancelable_and_revalidates_original_sources(harness, action):
    run, _, journal = harness

    def during_wait(event, cancel, safe_stop, paths):
        if isinstance(event, AlbumAttemptEvent) and event.retry_in_seconds:
            if action == "safe":
                safe_stop.set()
            elif action == "immediate":
                cancel.set()
            else:
                paths[0].write_bytes(b"different snapshot")

    before = time.monotonic()
    result, client, _, _ = run(["failed", "ok"], delay=0.03 if action == "modify" else 10,
                               on_event=during_wait)
    assert time.monotonic() - before < 3
    assert client.captions == (["caption-1", "caption-2"] if action == "modify" else ["caption-1"])
    assert result.status == ("PARTIAL" if action == "modify" else "CANCELLED")
    assert not journal.list_unresolved()


def test_cancel_after_submission_preserves_unknown(harness):
    run, _, journal = harness
    result, client, _, _ = run([TDLibCancelled("cancelled while awaiting response"), "ok"])
    assert result.status == "UNKNOWN"
    assert client.captions[0] == "caption-1"
    assert journal.list_unresolved()[0]["status"] == "UNKNOWN"


def test_server_delay_applies_at_zero_config_and_message_retry_is_bounded(harness):
    run, _, _ = harness
    before = time.monotonic()
    result, client, events, _ = run(["message_retry", "flood", "ok", "ok"])
    assert result.status == "COMPLETED"
    assert time.monotonic() - before >= 0.035
    assert client.requests == ["sendMessageAlbum", "resendMessages", "sendMessageAlbum", "sendMessageAlbum"]
    assert any(isinstance(event, AlbumAttemptEvent) and event.retry_in_seconds for event in events)


def test_entire_task_failure_and_retry_limit(harness):
    run, _, journal = harness
    result, client, _, _ = run(["failed"] * 6)
    assert result.status == "FAILED"
    assert len(result.failed_items) == 4
    assert client.captions == ["caption-1"] * 3 + ["caption-2"] * 3
    assert not journal.list_unresolved()


def test_retry_rechecks_protection_without_overwriting_it(harness):
    run, _, journal = harness

    def protect(event, _cancel, _stop, _paths):
        if isinstance(event, AlbumAttemptEvent) and event.retry_in_seconds:
            journal.update("image", "album-1", "CONFIRMED", message_ids=[10, 11], target=TARGET)

    result, client, _, _ = run(["failed", "ok"], delay=0.01, on_event=protect)
    assert result.status == "UNKNOWN"
    assert client.captions == ["caption-1", "caption-2"]
    assert journal.unresolved("image", "album-1", target=TARGET)["status"] == "CONFIRMED"


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(("code", "message"), [(420, "FLOOD_WAIT_0.04"), (429, "retry after 0.04")])
def test_request_rate_limit_waits_in_one_layer_and_succeeds(harness, enabled, code, message):
    run, _, _ = harness
    delays = []
    before = time.monotonic()

    def capture(event, *_args):
        if isinstance(event, AlbumAttemptEvent) and event.retry_in_seconds:
            delays.append(event)

    result, client, _, _ = run([TDLibError(code, message), "ok", "ok"], enabled=enabled,
                               delay=0.01, on_event=capture)
    assert result.status == "COMPLETED"
    assert time.monotonic() - before >= 0.035
    assert client.captions == ["caption-1", "caption-1", "caption-2"]
    first, second = client.snapshots[:2]
    if enabled:
        assert len(delays) == 1 and client.request_waits == []
        assert second["attempt_number"] == 2
        assert second["previous_attempts"][0]["request_rejection"] == {
            "submitted": False, "error_code": code, "rate_limited": True,
            "retry_after": 0.04, "error": f"TDLib error {code}: {message}",
        }
        assert first["attempt_id"] != second["attempt_id"]
    else:
        assert delays == [] and client.request_waits == [0.04]
        assert first["attempt_id"] == second["attempt_id"]


def test_mixed_rejection_and_message_failures_share_request_budget(harness):
    run, _, journal = harness
    result, client, events, _ = run([
        TDLibError(429, "retry after 0"), "failed", TDLibError(420, "FLOOD_WAIT_5"), "ok",
    ])
    assert result.status == "PARTIAL"
    assert client.captions == ["caption-1"] * 3 + ["caption-2"]
    assert client.request_waits == []
    assert not any(isinstance(event, AlbumAttemptEvent) and event.retry_in_seconds for event in events)
    record = journal.get("image", "album-1", target=TARGET)
    assert record["attempt_number"] == 3
    assert record["previous_attempts"][0]["request_rejection"]["submitted"] is False
    assert record["previous_attempts"][1]["failed_ids"]
    assert record["request_rejection"]["retry_after"] == 5


@pytest.mark.parametrize("immediate", [True, False])
def test_request_flood_wait_stops_without_a_second_request(harness, immediate):
    run, _, journal = harness

    def stop(event, cancel, safe_stop, _paths):
        if isinstance(event, AlbumAttemptEvent) and event.retry_in_seconds:
            (cancel if immediate else safe_stop).set()

    result, client, _, _ = run([TDLibError(429, "retry after 60")], on_event=stop)
    assert result.status == "CANCELLED"
    assert client.requests == ["sendMessageAlbum"] and client.request_waits == []
    assert not journal.list_unresolved()


@pytest.mark.parametrize(("code", "error"), [
    (400, "INVALID_ARGUMENT"), (403, "CHAT_WRITE_FORBIDDEN"), (429, "UNSPECIFIED_RATE_LIMIT")
])
def test_missing_proof_or_permanent_request_rejections_have_no_retry(harness, code, error):
    run, _, journal = harness
    result, client, _, _ = run([TDLibError(code, error), "ok"])
    assert result.status == "PARTIAL"
    assert client.captions == ["caption-1", "caption-2"] and client.request_waits == []
    assert journal.get("image", "album-1", target=TARGET)["request_rejection"]["rate_limited"] is False


def test_request_flood_rejection_must_be_persisted_before_retry(harness, monkeypatch):
    run, _, journal = harness

    def fail(journal):
        original = journal._write

        def write(path, record):
            if record.get("request_rejection"):
                raise OSError("unable to save the rejection")
            original(path, record)

        monkeypatch.setattr(journal, "_write", write)

    result, client, _, _ = run([TDLibError(429, "retry after 0"), "ok"], modify_journal=fail)
    assert result.status == "UNKNOWN"
    assert client.captions == ["caption-1", "caption-2"]
    assert journal.unresolved("image", "album-1", target=TARGET)["status"] == "PREPARED"


def test_permission_rejection_during_message_resend_blocks_new_album(harness):
    run, _, journal = harness
    result, client, _, _ = run(["message_retry", TDLibError(403, "CHAT_WRITE_FORBIDDEN"), "ok"])
    assert result.status == "PARTIAL"
    assert client.requests == ["sendMessageAlbum", "resendMessages", "sendMessageAlbum"]
    last = journal.get("image", "album-1", target=TARGET)
    assert last["status"] == "FAILED"
    assert [item["error_code"] for item in last["item_results"]] == [403, 403]


def test_single_message_requests_share_the_same_budget(harness):
    run, _, _ = harness
    result, client, _, _ = run([TDLibError(429, "retry after 0"), "ok", "ok"], single=True)
    assert result.status == "COMPLETED"
    assert client.requests == ["sendMessage", "sendMessage", "sendMessageAlbum"]
