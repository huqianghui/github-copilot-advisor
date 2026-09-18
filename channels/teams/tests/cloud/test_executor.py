"""Real SDK proactive/state/download pipeline; only remote boundaries are fake."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest
import aiohttp
import httpx
from multidict import CIMultiDict
from microsoft_agents.activity import Activity, ResourceResponse

from advisor_agent.factory import AdvisorComponents, AzureAdvisor
from advisor_shared.cloud import PreparedTurn, TaskPayload
from advisor_shared.telemetry import current_trace, step
from teams_adapter.extract import build_conversation_key

BOT_ID = "28:executor-bot"
APP_ID = "executor-app"


def payload_for(text="question", *, user="29:a", kind="personal",
                conversation="chat-1", tenant="tenant-a", **changes):
    activity = {
        "type": "message", "id": "incoming-1", "text": text,
        "channelId": "msteams", "serviceUrl": "https://api.botframework.com",
        "recipient": {"id": BOT_ID},
        "from": {"id": user, "name": "Test User"},
        "conversation": {"id": conversation, "conversationType": kind},
        "channelData": {"tenant": {"id": tenant}},
    }
    if kind != "personal":
        activity["text"] = f"<at>Advisor</at> {text}"
        activity["entities"] = [{
            "type": "mention", "text": "<at>Advisor</at>",
            "mentioned": {"id": BOT_ID},
        }]
    activity.update(changes)
    return TaskPayload(
        job_id=f"job-{user}-{conversation}", activity=activity,
        partition_key=build_conversation_key(activity),
        accepted_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
        trace_id=f"trace-{user}", target="teams")


class ModelBoundary:
    def __init__(self):
        self.calls = []
        self.errors = {}
        self.gate = None
        self.traces = []

    async def run(self, user_text, history, images=None):
        self.calls.append((user_text, deepcopy(history), images))
        self.traces.append(current_trace().trace_id)
        if self.gate:
            await self.gate(user_text)
        await asyncio.sleep(0)
        if user_text in self.errors:
            raise self.errors[user_text]
        return "answer" if user_text == "question" else f"answer:{user_text}"


class ConnectionBoundary:
    def get_token_provider_from_activity(self, identity, activity):
        return self

    async def get_access_token(self, audience, scope):
        return "test-only-token"


class ConnectorBoundary:
    def __init__(self):
        self.sent = []
        self.setup_error = None
        self.send_error = None
        self.close_error = None
        self.typing = asyncio.Event()
        self.identities = []

    async def create_connector_client(
            self, context, claims_identity, service_url, audience,
            scopes=None, use_anonymous=False):
        self.identities.append(claims_identity)
        if self.setup_error:
            raise self.setup_error
        return SimpleNamespace(conversations=self, close=self.close)

    async def create_user_token_client(
            self, context, claims_identity, use_anonymous=False):
        return SimpleNamespace(close=self.close)

    async def close(self):
        if self.close_error:
            raise self.close_error

    async def reply_to_activity(self, conversation_id, activity_id, activity):
        return await self._send(conversation_id, activity_id, activity)

    async def send_to_conversation(self, conversation_id, activity):
        return await self._send(conversation_id, None, activity)

    async def _send(self, conversation_id, activity_id, activity):
        if activity.type == "typing":
            self.typing.set()
        elif self.send_error:
            raise self.send_error
        self.sent.append((
            conversation_id, activity_id,
            activity.model_dump(mode="json", by_alias=True, exclude_none=True)))
        return ResourceResponse(id="reply-123")

    @property
    def final_sends(self):
        return [sent for sent in self.sent if sent[2]["type"] == "message"]


@pytest.fixture
def executor_case():
    backend = ModelBoundary()
    connector = ConnectorBoundary()
    owner = AzureAdvisor(
        backend, channel_name="teams", azure_clients=(), credential_owner=None)
    return SimpleNamespace(
        backend=backend, connector=connector,
        components=AdvisorComponents(owner), connection_manager=ConnectionBoundary())


def make_executor(case):
    from teams_adapter.cloud.executor import TeamsTurnExecutor

    return TeamsTurnExecutor(
        case.components, case.connection_manager, APP_ID,
        channel_service_client_factory=case.connector)


async def test_prepare_never_sends_final_reply_and_send_uses_checkpoint(executor_case):
    case = executor_case
    executor = make_executor(case)
    payload = payload_for()
    seed = [{"role": "user", "content": "old"},
            {"role": "assistant", "content": "old answer"}]
    prepared = await executor.prepare(payload, seed)
    assert prepared.reply == {"type": "message", "text": "answer", "entities": []}
    assert prepared.history == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    assert prepared.outcome == "success"
    assert case.backend.calls == [("question", seed, None)]
    assert case.connector.final_sends == []
    assert case.connector.sent and all(
        item[2]["type"] == "typing" for item in case.connector.sent)
    typing_count = len(case.connector.sent)

    saved = PreparedTurn.model_validate_json(prepared.model_dump_json())
    assert await executor.send(payload, saved) == "reply-123"
    assert len(case.connector.sent) == typing_count + 1
    assert len(case.backend.calls) == 1
    destination, reply_to, sent = case.connector.final_sends[0]
    assert (destination, reply_to) == ("chat-1", "incoming-1")
    assert sent["text"] == "answer"
    assert sent["from"]["id"] == BOT_ID
    assert sent["recipient"]["id"] == "29:a"
    assert [identity.get_app_id() for identity in case.connector.identities] == [
        APP_ID, APP_ID]


async def test_concurrent_turns_isolate_history_events_and_trace(executor_case):
    case = executor_case
    executor = make_executor(case)
    both_started = asyncio.Event()
    started = []

    async def rendezvous(text):
        started.append(text)
        if len(started) == 2:
            both_started.set()
        await both_started.wait()

    case.backend.gate = rendezvous
    case.backend.errors["question-b"] = RuntimeError("private-model-error")
    async with asyncio.timeout(2):
        a, b = await asyncio.gather(
            executor.prepare(payload_for("question-a"), [
                {"role": "user", "content": "A-only history"}]),
            executor.prepare(payload_for("question-b", user="29:b"), [
                {"role": "user", "content": "B-only history"}]))
    assert a.history == [
        {"role": "user", "content": "question-a"},
        {"role": "assistant", "content": "answer:question-a"},
    ]
    assert a.outcome == "success"
    assert b.outcome == "degraded" and b.history == []
    assert [(text, history) for text, history, _ in case.backend.calls] == [
        ("question-a", [{"role": "user", "content": "A-only history"}]),
        ("question-b", [{"role": "user", "content": "B-only history"}]),
    ]
    assert case.connector.final_sends == []
    assert case.backend.traces == ["trace-29:a", "trace-29:b"]


@pytest.mark.parametrize("invalid", [
    "partition", "tenant", "conversation", "sender", "missing_sender",
    "missing_tenant", "conflicting_tenant",
])
async def test_identity_is_verified_before_private_history_is_read(
        executor_case, invalid, caplog):
    from teams_adapter.cloud.errors import DeliveryFailure

    class UnreadableHistory(list):
        def __iter__(self):
            pytest.fail("unverified payload accessed private history")

    payload = payload_for("private-question")
    activity = deepcopy(payload.activity)
    if invalid == "partition":
        payload = payload.model_copy(update={"partition_key": "other-partition"})
    elif invalid == "tenant":
        activity["channelData"]["tenant"]["id"] = "other-tenant"
    elif invalid == "conversation":
        activity["conversation"]["id"] = "other-chat"
    elif invalid == "sender":
        activity["from"]["id"] = "29:other"
    elif invalid == "missing_sender":
        del activity["from"]
    elif invalid == "missing_tenant":
        del activity["channelData"]
    else:
        activity["conversation"]["tenantId"] = "conflicting-tenant"
    payload = payload.model_copy(update={"activity": activity})
    with pytest.raises(DeliveryFailure) as caught:
        await make_executor(executor_case).prepare(payload, UnreadableHistory())
    assert caught.value.safe_code == "invalid_payload"
    assert caught.value.definitely_not_sent and not caught.value.retryable
    assert executor_case.backend.calls == []
    assert executor_case.connector.sent == []
    assert "private-question" not in caplog.text


@pytest.mark.parametrize("kind", ["unmentioned", "empty", "non_message"])
async def test_ignored_turns_do_not_generate_send_or_type(executor_case, kind):
    payload = payload_for("" if kind == "empty" else "question", kind="groupChat")
    if kind == "unmentioned":
        payload.activity["entities"] = []
    elif kind == "non_message":
        payload.activity["type"] = "conversationUpdate"
    executor = make_executor(executor_case)
    prepared = await executor.prepare(payload, [])
    assert prepared == PreparedTurn(reply=None, history=[], outcome="ignored")
    assert await executor.send(payload, prepared) is None
    assert executor_case.backend.calls == []
    assert executor_case.connector.sent == []


@pytest.mark.parametrize("mode", ["original", "partial", "all", "pure_failed"])
async def test_attachments_use_sdk_state_and_are_never_redownloaded_on_send(
        executor_case, monkeypatch, mode):
    requests = []
    png = b"\x89PNG\r\n\x1a\noriginal-image"
    jpeg = b"\xff\xd8\xffsecond-image"

    def download(request):
        requests.append(request)
        if mode in {"all", "pure_failed"} or (
                mode == "partial" and request.url.path.endswith("2")):
            return httpx.Response(403)
        body = jpeg if request.url.path.endswith("2") else png
        return httpx.Response(200, content=body)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "teams_adapter.downloader.httpx.AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(download), **kwargs))
    payload = payload_for("" if mode == "pure_failed" else "question",
                          attachments=[
        {"contentType": "image/*",
         "contentUrl": f"https://api.botframework.com/image-{number}"}
        for number in (1, 2)
    ])
    executor = make_executor(executor_case)
    prepared = await executor.prepare(payload, [])
    assert len(requests) == 2
    assert executor_case.connector.final_sends == []
    if mode == "pure_failed":
        from teams_adapter.bot import IMAGE_FETCH_FAILED
        assert prepared.reply["text"] == IMAGE_FETCH_FAILED
        assert prepared.history == []
        assert executor_case.backend.calls == []
    else:
        text, _, images = executor_case.backend.calls[0]
        if mode == "original":
            assert [(image.data, image.mime_type) for image in images] == [
                (png, "image/png"), (jpeg, "image/jpeg")]
            assert text == "question"
            assert prepared.history[0]["content"] == "[图片×2] question"
        elif mode == "partial":
            assert [(image.data, image.mime_type) for image in images] == [
                (png, "image/png")]
            assert text == "question(另有 1 张图片未能获取)"
        else:
            assert images is None
            assert text == "question(另有 2 张图片未能获取)"
    assert prepared.outcome == ("success" if mode == "original" else "degraded")
    generated_count = len(executor_case.backend.calls)
    sent_count = len(executor_case.connector.sent)
    await executor.send(payload, prepared)
    assert len(requests) == 2
    assert len(executor_case.backend.calls) == generated_count
    assert len(executor_case.connector.sent) == sent_count + 1


async def test_channel_thread_and_saved_mentions_are_preserved(executor_case):
    payload = payload_for(kind="channel", conversation="19:channel;messageid=7")
    saved = PreparedTurn(reply={
        "type": "message", "text": "<at>Support</at> help",
        "entities": [{"type": "mention", "text": "<at>Support</at>",
                      "mentioned": {"id": "29:support", "name": "Support"}}],
    }, history=[], outcome="success")
    await make_executor(executor_case).send(payload, saved)
    destination, reply_to, sent = executor_case.connector.final_sends[0]
    assert (destination, reply_to) == ("19:channel;messageid=7", "incoming-1")
    assert sent["entities"] == saved.reply["entities"]
    assert sent["text"] == saved.reply["text"]
    assert executor_case.backend.calls == []
    assert len(executor_case.connector.sent) == 1


def response_error(status, *, headers=None):
    return aiohttp.ClientResponseError(
        request_info=SimpleNamespace(real_url="https://private.invalid/secret"),
        history=(), status=status, message="private-service-error", headers=headers)


@pytest.mark.parametrize(("error", "definite", "retryable", "code"), [
    (TimeoutError("private-timeout"), False, False, "delivery_unknown"),
    (response_error(403), True, False, "delivery_rejected"),
    (response_error(429), True, True, "delivery_throttled"),
    (response_error(500), False, False, "delivery_unknown"),
])
async def test_send_errors_propagate_safely_without_sdk_error_reply(
        executor_case, error, definite, retryable, code, caplog, capsys):
    from teams_adapter.cloud.errors import DeliveryFailure

    case = executor_case
    case.connector.send_error = error
    saved = PreparedTurn(
        reply={"type": "message", "text": "saved"}, history=[], outcome="success")
    with pytest.raises(DeliveryFailure) as caught:
        await make_executor(case).send(payload_for(), saved)
    assert caught.value.definitely_not_sent is definite
    assert caught.value.retryable is retryable
    assert caught.value.safe_code == code
    assert "private-" not in str(caught.value)
    assert "private-" not in caplog.text
    captured = capsys.readouterr()
    assert "private-" not in captured.out + captured.err
    assert case.connector.sent == []  # No exception reply or SDK trace activity.
    assert case.backend.calls == []


@pytest.mark.parametrize("phase", ["prepare", "send"])
async def test_pre_send_setup_timeout_is_safe_to_retry(
        executor_case, phase, caplog, capsys):
    from teams_adapter.cloud.errors import DeliveryFailure

    case = executor_case
    case.connector.setup_error = TimeoutError("private-token-provider-url")
    executor = make_executor(case)
    with pytest.raises(DeliveryFailure) as caught:
        if phase == "prepare":
            await executor.prepare(payload_for(), [])
        else:
            await executor.send(payload_for(), PreparedTurn(
                reply={"type": "message", "text": "saved"},
                history=[], outcome="success"))
    assert caught.value.definitely_not_sent and caught.value.retryable
    assert case.connector.sent == [] and case.backend.calls == []
    assert "private-token" not in caplog.text
    assert "private-token" not in capsys.readouterr().out


@pytest.mark.parametrize("error", [
    TimeoutError("private-cleanup"), response_error(429), response_error(403)])
async def test_cleanup_error_after_delivery_is_not_retryable(executor_case, error):
    from teams_adapter.cloud.errors import DeliveryFailure

    case = executor_case
    case.connector.close_error = error
    with pytest.raises(DeliveryFailure) as caught:
        await make_executor(case).send(payload_for(), PreparedTurn(
            reply={"type": "message", "text": "saved"},
            history=[], outcome="success"))
    assert not caught.value.definitely_not_sent
    assert not caught.value.retryable
    assert len(case.connector.final_sends) == 1


async def test_generation_adapter_blocks_accidental_final_send(executor_case):
    from teams_adapter.cloud.errors import DeliveryFailure

    executor = make_executor(executor_case)

    async def accidental_reply(context):
        await context.send_activity("must-not-be-sent-before-checkpoint")

    with pytest.raises(DeliveryFailure) as caught:
        await executor._generation.continue_conversation(
            APP_ID, Activity.model_validate(payload_for().activity), accidental_reply)
    assert caught.value.definitely_not_sent
    assert not caught.value.retryable
    assert executor_case.connector.final_sends == []


@pytest.mark.parametrize("reply", [
    {"type": "typing"}, {"type": "trace"}, {"text": "missing type"},
    {"type": "message", "text": {"not": "text"}},
])
async def test_send_rejects_invalid_checkpoint_before_network(executor_case, reply):
    from teams_adapter.cloud.errors import DeliveryFailure

    with pytest.raises(DeliveryFailure) as caught:
        await make_executor(executor_case).send(payload_for(), PreparedTurn(
            reply=reply, history=[], outcome="success"))
    assert caught.value.definitely_not_sent
    assert not caught.value.retryable
    assert caught.value.safe_code == "invalid_reply"
    assert executor_case.connector.identities == []
    assert executor_case.connector.sent == []


async def test_saved_reply_cannot_be_sent_to_another_partition(executor_case):
    from teams_adapter.cloud.errors import DeliveryFailure

    payload = payload_for()
    payload.activity["from"]["id"] = "29:other"
    with pytest.raises(DeliveryFailure) as caught:
        await make_executor(executor_case).send(payload, PreparedTurn(
            reply={"type": "message", "text": "private-saved-answer"},
            history=[], outcome="success"))
    assert caught.value.safe_code == "invalid_payload"
    assert executor_case.connector.sent == []


@pytest.mark.parametrize("phase", ["prepare", "send"])
async def test_sdk_validation_errors_do_not_log_private_inputs(
        executor_case, phase, caplog, capsys):
    from teams_adapter.cloud.errors import DeliveryFailure

    executor = make_executor(executor_case)
    payload = payload_for()
    with pytest.raises(DeliveryFailure):
        if phase == "prepare":
            payload.activity["text"] = {"private": "private-chat-secret"}
            await executor.prepare(payload, [])
        else:
            await executor.send(payload, PreparedTurn(
                reply={"type": "message",
                       "text": {"private": "private-chat-secret"}},
                history=[], outcome="success"))
    assert "private-chat-secret" not in caplog.text
    captured = capsys.readouterr()
    assert "private-chat-secret" not in captured.out + captured.err
    assert executor_case.connector.sent == []


async def test_invalid_trigger_data_is_a_classified_failure(executor_case):
    from teams_adapter.cloud.errors import DeliveryFailure

    payload = payload_for(kind="groupChat")
    payload.activity["entities"][0]["mentioned"] = ["private-malformed-mention"]
    with pytest.raises(DeliveryFailure) as caught:
        await make_executor(executor_case).prepare(payload, [])
    assert caught.value.definitely_not_sent
    assert not caught.value.retryable
    assert executor_case.connector.sent == []
    assert executor_case.backend.calls == []


@pytest.mark.parametrize("status", ["degraded", "not_configured"])
async def test_degraded_step_is_not_reported_as_success(executor_case, status):
    async def degraded_model(text):
        with step("model.degraded") as timing:
            timing.status = status

    executor_case.backend.gate = degraded_model
    prepared = await make_executor(executor_case).prepare(payload_for(), [])
    assert prepared.reply["text"] == "answer"
    assert prepared.outcome == "degraded"
    assert prepared.history[-1] == {"role": "assistant", "content": "answer"}


@pytest.mark.parametrize(("header", "seconds"), [
    ("0", 0), ("120", 120), ("  86400  ", 86400),
    ("100000000000000000000", 100000000000000000000),
])
async def test_send_throttling_preserves_provider_delay_through_sdk_wrappers(
        executor_case, header, seconds):
    from advisor_shared.delivery import DeliveryFailure
    from teams_adapter.cloud.errors import DeliveryFailure as TeamsDeliveryFailure

    assert TeamsDeliveryFailure is DeliveryFailure
    executor_case.connector.send_error = response_error(
        429, headers=CIMultiDict({"retry-after": header}))
    with pytest.raises(DeliveryFailure) as caught:
        await make_executor(executor_case).send(payload_for(), PreparedTurn(
            reply={"type": "message", "text": "saved"}, history=[], outcome="success"))
    assert caught.value.retry_after_seconds == seconds
    assert caught.value.definitely_not_sent and caught.value.retryable
    assert caught.value.safe_code == "delivery_throttled"
    assert executor_case.connector.sent == []


async def test_http_date_retry_after_preserves_full_provider_wait(executor_case):
    from teams_adapter.cloud.errors import DeliveryFailure

    deadline = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    executor_case.connector.send_error = response_error(
        429, headers=CIMultiDict({"Retry-After": format_datetime(deadline, usegmt=True)}))
    before = datetime.now(timezone.utc)
    with pytest.raises(DeliveryFailure) as caught:
        await make_executor(executor_case).send(payload_for(), PreparedTurn(
            reply={"type": "message", "text": "saved"}, history=[], outcome="success"))
    after = datetime.now(timezone.utc)
    delay = caught.value.retry_after_seconds
    assert (deadline - after).total_seconds() <= delay <= (
        deadline - before).total_seconds()
    assert delay > 86400


async def test_expired_retry_after_date_does_not_make_delay_negative(executor_case):
    from teams_adapter.cloud.errors import DeliveryFailure

    executor_case.connector.send_error = response_error(
        429, headers=CIMultiDict({"Retry-After": "Thu, 01 Jan 1970 00:00:00 GMT"}))
    with pytest.raises(DeliveryFailure) as caught:
        await make_executor(executor_case).send(payload_for(), PreparedTurn(
            reply={"type": "message", "text": "saved"}, history=[], outcome="success"))
    assert caught.value.retry_after_seconds == 0
    assert caught.value.retryable


@pytest.mark.parametrize("header", [
    "", "-1", "1.5", "inf", "NaN", "private-token-at-https://private.invalid",
    "Thu, 99 Jan 1970 00:00:00 GMT", "Thu, 01 Jan 1970 00:00:00",
])
async def test_invalid_retry_after_logs_only_fixed_diagnostic(
        executor_case, header, caplog, capsys):
    from teams_adapter.cloud.errors import DeliveryFailure

    executor_case.connector.send_error = response_error(
        429, headers=CIMultiDict({"Retry-After": header}))
    with pytest.raises(DeliveryFailure) as caught:
        await make_executor(executor_case).send(payload_for(), PreparedTurn(
            reply={"type": "message", "text": "saved"}, history=[], outcome="success"))
    assert caught.value.retry_after_seconds is None
    assert caught.value.definitely_not_sent and caught.value.retryable
    diagnostics = [record for record in caplog.records
                   if record.getMessage() == "teams_retry_after_invalid"]
    assert len(diagnostics) == 1
    assert diagnostics[0].args == () and diagnostics[0].exc_info is None
    assert "private-" not in caplog.text
    captured = capsys.readouterr()
    assert "private-" not in captured.out + captured.err


@pytest.mark.parametrize("phase", ["prepare", "send"])
async def test_setup_throttle_retains_retry_after(executor_case, phase):
    from teams_adapter.cloud.errors import DeliveryFailure

    executor_case.connector.setup_error = response_error(
        429, headers=CIMultiDict({"Retry-After": "75"}))
    executor = make_executor(executor_case)
    with pytest.raises(DeliveryFailure) as caught:
        if phase == "prepare":
            await executor.prepare(payload_for(), [])
        else:
            await executor.send(payload_for(), PreparedTurn(
                reply={"type": "message", "text": "saved"},
                history=[], outcome="success"))
    assert caught.value.retry_after_seconds == 75
    assert caught.value.definitely_not_sent and caught.value.retryable
    assert executor_case.connector.sent == []


async def test_wrapped_shared_failure_keeps_retry_metadata(executor_case):
    from advisor_shared.delivery import DeliveryFailure

    upstream = DeliveryFailure(
        definitely_not_sent=True, retryable=True, safe_code="delivery_throttled",
        retry_after_seconds=450)
    executor_case.connector.send_error = upstream
    with pytest.raises(DeliveryFailure) as caught:
        await make_executor(executor_case).send(payload_for(), PreparedTurn(
            reply={"type": "message", "text": "saved"},
            history=[], outcome="success"))
    assert caught.value is upstream
    assert caught.value.retry_after_seconds == 450


async def test_retry_after_cannot_make_post_acknowledgement_failure_retryable(
        executor_case):
    from teams_adapter.cloud.errors import DeliveryFailure

    executor_case.connector.close_error = response_error(
        429, headers=CIMultiDict({"Retry-After": "60"}))
    with pytest.raises(DeliveryFailure) as caught:
        await make_executor(executor_case).send(payload_for(), PreparedTurn(
            reply={"type": "message", "text": "saved"},
            history=[], outcome="success"))
    assert not caught.value.definitely_not_sent and not caught.value.retryable
    assert caught.value.safe_code == "delivery_unknown"
    assert caught.value.retry_after_seconds == 60
    assert len(executor_case.connector.final_sends) == 1
