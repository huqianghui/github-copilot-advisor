"""Offline SDK integration tests: no Azure, model, or search service calls."""
import asyncio
import base64
import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx2
import pytest
from openai import APIConnectionError, APITimeoutError, RateLimitError

from advisor_agent.factory import build_openai_client
from advisor_agent.maf_backend import MAFBackend
from advisor_agent.search.knowledge import KnowledgeSearchClient
from advisor_shared.logging import TelemetryFormatter, configure_logging
from advisor_shared.messages import ImageInput
from advisor_shared.telemetry import trace_scope

LOGGER = "advisor_agent.llm_diagnostics"
REQUEST_ID = "11111111-2222-4333-8444-555555555555"
SECRETS = ("private-question", "private-answer", "private-credential",
           "private-image", "private-vector", "private-header",
           "private-exception", "private-path", "private-query")
IMAGE_BASE64 = base64.b64encode(b"private-image").decode()


@pytest.fixture(autouse=True)
def environment(monkeypatch, caplog):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT",
                       "https://offline.invalid/private-path")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "private-credential")
    monkeypatch.setenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "test-chat")
    # ADVISOR_LOG_LEVEL is applied by configure_logging at application startup.
    monkeypatch.setenv("ADVISOR_LOG_LEVEL", "DEBUG")
    caplog.set_level(logging.WARNING, logger="openai")
    caplog.set_level(logging.WARNING, logger="httpx2")
    caplog.set_level(logging.DEBUG, logger="advisor_agent")


def completion():
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 1,
        "model": "test-chat",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": "private-answer"}}],
        "usage": {
            "prompt_tokens": 17, "completion_tokens": 3, "total_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    }


@asynccontextmanager
async def sdk_client(monkeypatch, handler, *, request_hooks=()):
    """Replace only the outbound transport; exercise factory, hooks and SDK retries."""
    transport = httpx2.MockTransport(handler)
    monkeypatch.setattr(httpx2.AsyncClient, "_init_transport",
                        lambda self, **kwargs: transport)
    monkeypatch.setattr(httpx2.AsyncClient, "_get_proxy_map",
                        lambda *args, **kwargs: {})
    client = build_openai_client("2024-10-21").with_options(
        default_query={"key": "private-query"})
    client._client.event_hooks["request"][:0] = request_hooks
    try:
        yield client
    finally:
        await client.close()


async def chat(client, images=None):
    backend = MAFBackend(None, lambda: "channel", lambda: False, client=client)
    return await backend.run("private-question", [], images)


def records(caplog, event):
    return [r.telemetry for r in caplog.records
            if r.name == LOGGER and r.msg == event]


def assert_private(caplog):
    output = "\n".join(
        TelemetryFormatter().format(r) for r in caplog.records
        if r.name.startswith(("advisor_agent", "advisor_shared")))
    for secret in (*SECRETS, IMAGE_BASE64, "[0.125, 0.25]"):
        assert secret not in output
    for record in caplog.records:
        if record.name == LOGGER:
            assert record.levelno == logging.DEBUG
            assert record.exc_info is None


async def test_chat_sdk_retry_attempts_have_safe_headers_gap_and_summary(
        monkeypatch, caplog):
    seen = []

    async def handler(request):
        assert IMAGE_BASE64 in request.content.decode()
        seen.append(request.headers["x-stainless-retry-count"])
        if len(seen) == 1:
            return httpx2.Response(429, json={"error": {"message": "private-answer"}},
                                  headers={
                                      "retry-after-ms": "1",
                                      "x-request-id": REQUEST_ID,
                                      "x-ratelimit-remaining-tokens": "0",
                                      "authorization": "private-credential",
                                      "x-private": "private-header",
                                  })
        return httpx2.Response(200, json=completion())

    async with sdk_client(monkeypatch, handler) as client:
        with trace_scope() as trace:
            assert await chat(client, [ImageInput(
                data=b"private-image", mime_type="image/png")]) == "private-answer"
    assert seen == ["0", "1"]
    attempts = records(caplog, "llm.http.attempt_completed")
    assert len(attempts) == 2
    assert [a["sdk_retry_count"] for a in attempts] == [0, 1]
    assert [a["attempt"] for a in attempts] == [1, 2]
    assert [a["http_status"] for a in attempts] == [429, 200]
    assert attempts[0]["request_ids"] == {"x-request-id": REQUEST_ID}
    assert attempts[0]["numeric_headers"] == {
        "retry-after-ms": 1, "x-ratelimit-remaining-tokens": 0}
    assert attempts[0]["inter_attempt_gap_ms"] is None
    assert attempts[1]["inter_attempt_gap_ms"] > 0
    assert all(a["duration_ms"] >= 0 for a in attempts)
    assert "receive_response_headers" not in attempts[0]["phase_durations_ms"]
    summary, = records(caplog, "llm.sdk_call_completed")
    assert summary["operation"] == "chat.completions"
    assert summary["status"] == "success"
    assert summary["attempt_count"] == 2
    assert summary["duration_ms"] >= sum(a["duration_ms"] for a in attempts)
    assert summary["usage"] == {
        "input_tokens": 17, "output_tokens": 3,
        "cached_input_tokens": 4, "reasoning_tokens": 2}
    span = next(s for s in trace.timings if s.name == "llm.completion")
    assert all(a["trace_id"] == trace.trace_id and a["span_id"] == span.span_id
               and a["call_id"] == summary["call_id"] for a in attempts)
    assert span.attributes["input_tokens"] == 17
    assert span.attributes["output_tokens"] == 3
    assert_private(caplog)


async def test_sdk_default_redirect_behavior_does_not_increment_retry_ordinal(
        monkeypatch, caplog):
    seen = []

    async def handler(request):
        seen.append(request.headers["x-stainless-retry-count"])
        await request.extensions["trace"]("http11.send_request_body.started", {})
        await request.extensions["trace"]("http11.send_request_body.complete", {})
        if len(seen) == 1:
            return httpx2.Response(
                307, headers={"location": "https://offline.invalid/redirected"})
        return httpx2.Response(200, json=completion())

    async with sdk_client(monkeypatch, handler) as client:
        assert await chat(client) == "private-answer"
    assert seen == ["0", "0"]
    attempts = records(caplog, "llm.http.attempt_completed")
    assert [a["http_status"] for a in attempts] == [307, 200]
    assert [a["sdk_retry_count"] for a in attempts] == [0, 0]
    assert [a["end_boundary"] for a in attempts] == [
        "next_request_hook", "http_client_send"]
    assert all(a["inter_attempt_gap_ms"] is None for a in attempts)
    assert len(records(caplog, "llm.http.phase")) == 4


async def test_info_does_not_collect_state_or_attach_trace(monkeypatch, caplog):
    import advisor_agent.llm_diagnostics as diagnostics

    monkeypatch.setenv("ADVISOR_LOG_LEVEL", "INFO")
    caplog.set_level(logging.INFO, logger="advisor_agent")

    def unexpected_state(*args, **kwargs):
        pytest.fail("INFO must not build detailed diagnostic state")

    monkeypatch.setattr(diagnostics, "_Call", unexpected_state)

    async def handler(request):
        assert "trace" not in request.extensions
        return httpx2.Response(200, json=completion())

    async with sdk_client(monkeypatch, handler) as client:
        with trace_scope() as trace:
            assert await chat(client) == "private-answer"
    assert not [r for r in caplog.records if r.name == LOGGER]
    span = next(s for s in trace.timings if s.name == "llm.completion")
    assert span.attributes["input_tokens"] == 17
    assert span.attributes["output_tokens"] == 3


@pytest.mark.parametrize("level, expected_calls", [("INFO", 0), ("DEBUG", 1)])
async def test_application_logging_setting_controls_diagnostics_not_sdk_logging(
        monkeypatch, caplog, level, expected_calls):
    loggers = [logging.getLogger(name) for name in (
        "", "advisor_shared", "advisor_agent", "teams_adapter",
        "openai", "httpx", "httpx2", "httpcore", "httpcore2", "azure")]
    for app_logger in loggers:
        caplog.set_level(app_logger.level, logger=app_logger.name)
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", list(root.handlers))
    for handler in logging.getLogger().handlers:
        monkeypatch.setattr(handler, "formatter", handler.formatter)
    monkeypatch.setenv("ADVISOR_LOG_LEVEL", level)
    configure_logging()
    caplog.handler.setLevel(logging.DEBUG)

    async def handler(request):
        assert ("trace" in request.extensions) == (level == "DEBUG")
        return httpx2.Response(200, json=completion())

    async with sdk_client(monkeypatch, handler) as client:
        await chat(client)
    assert len(records(caplog, "llm.sdk_call_completed")) == expected_calls
    assert logging.getLogger("openai").getEffectiveLevel() == logging.INFO
    assert all(not logging.getLogger(name).isEnabledFor(logging.DEBUG)
               for name in ("httpx", "httpx2", "httpcore", "httpcore2"))
    assert_private(caplog)


async def test_embedding_scope_uses_same_diagnostics_without_logging_vectors(
        monkeypatch, caplog):
    class Search:
        async def search(self, **kwargs):
            assert kwargs["vector_queries"][0].vector == [0.125, 0.25]

            async def documents():
                yield {"title": "result", "content": "private-vector",
                       "url": "https://example.test", "@search.reranker_score": 3}
            return documents()

    async def handler(request):
        assert request.url.path.endswith("/embeddings")
        assert "private-question" in request.content.decode()
        return httpx2.Response(200, json={
            "object": "list", "model": "test-embedding",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.125, 0.25]}],
            "usage": {"prompt_tokens": 7, "total_tokens": 7},
        })

    async with sdk_client(monkeypatch, handler) as client:
        with trace_scope() as trace:
            result = await KnowledgeSearchClient(
                Search(), client, "test-embedding").search("private-question")
    assert result[0].content == "private-vector"
    summary, = records(caplog, "llm.sdk_call_completed")
    span = next(s for s in trace.timings if s.name == "search.kb.embedding")
    assert summary["operation"] == "embeddings"
    assert summary["span_id"] == span.span_id
    assert summary["trace_id"] == trace.trace_id
    assert summary["usage"] == {"input_tokens": 7}
    assert_private(caplog)


@pytest.mark.parametrize("failure", ["http", "connection", "timeout"])
async def test_final_sdk_failure_is_typed_and_preserves_retries(
        monkeypatch, caplog, failure):
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        if failure == "connection":
            raise httpx2.ConnectError("private-exception", request=request)
        if failure == "timeout":
            raise httpx2.ReadTimeout("private-exception", request=request)
        return httpx2.Response(429, json={"error": {"message": "private-exception"}},
                              headers={"retry-after-ms": "1"})

    error_type = {"http": RateLimitError, "connection": APIConnectionError,
                  "timeout": APITimeoutError}[failure]
    async with sdk_client(monkeypatch, handler) as client:
        with pytest.raises(error_type):
            await chat(client)
    assert count == 2
    summary, = records(caplog, "llm.sdk_call_completed")
    assert summary["status"] == ("timeout" if failure == "timeout" else "error")
    assert summary["error_type"] == error_type.__name__
    attempts = records(caplog, "llm.http.attempt_completed")
    assert len(attempts) == 2
    assert attempts[-1]["error_type"] == {
        "http": None, "connection": "ConnectError", "timeout": "ReadTimeout"}[failure]
    assert attempts[-1]["http_status"] == (429 if failure == "http" else None)
    assert attempts[-1]["inter_attempt_gap_ms"] > 0
    assert_private(caplog)


@pytest.mark.parametrize("error, status", [
    (ValueError("private-exception"), "error"),
    (asyncio.CancelledError("private-exception"), "cancelled"),
])
def test_sdk_exit_observes_original_exception_without_handling_it(caplog, error, status):
    from advisor_agent.llm_diagnostics import sdk_call_diagnostics

    observer = sdk_call_diagnostics("chat.completions")
    observer.__enter__()
    result = observer.__exit__(type(error), error, error.__traceback__)
    assert result is None
    assert error.__traceback__ is None
    summary, = records(caplog, "llm.sdk_call_completed")
    assert summary["status"] == status
    assert summary["error_type"] == type(error).__name__
    assert_private(caplog)


async def test_cancellation_is_logged_once_and_scope_does_not_bleed(
        monkeypatch, caplog):
    started = asyncio.Event()
    first = True

    async def handler(request):
        nonlocal first
        if first:
            first = False
            started.set()
            await asyncio.Future()
        return httpx2.Response(200, json=completion())

    async with sdk_client(monkeypatch, handler) as client:
        task = asyncio.create_task(chat(client))
        await asyncio.wait_for(started.wait(), 3)
        task.cancel("private-exception")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await chat(client) == "private-answer"
        # A request outside the SDK-call scope must not inherit diagnostics.
        prior = len(records(caplog, "llm.http.attempt_started"))
        await client.chat.completions.create(model="test-chat", messages=[])
        assert len(records(caplog, "llm.http.attempt_started")) == prior
    cancelled, success = records(caplog, "llm.sdk_call_completed")
    assert cancelled["status"] == "cancelled"
    assert cancelled["error_type"] == "CancelledError"
    assert cancelled["attempt_count"] == success["attempt_count"] == 1
    assert cancelled["call_id"] != success["call_id"]
    assert records(caplog, "llm.http.attempt_completed")[0]["status"] == "cancelled"
    assert_private(caplog)


async def test_parallel_sdk_calls_have_independent_attempts_and_correlation(
        monkeypatch, caplog):
    arrived = asyncio.Event()
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        if count == 2:
            arrived.set()
        await asyncio.wait_for(arrived.wait(), 3)
        return httpx2.Response(200, json=completion())

    async def turn(client):
        with trace_scope(new=True) as trace:
            await chat(client)
            return trace.trace_id

    async with sdk_client(monkeypatch, handler) as client:
        ids = await asyncio.gather(turn(client), turn(client))
    summaries = records(caplog, "llm.sdk_call_completed")
    attempts = records(caplog, "llm.http.attempt_completed")
    assert len(summaries) == len(attempts) == 2
    assert {s["trace_id"] for s in summaries} == set(ids)
    assert len({s["call_id"] for s in summaries}) == 2
    for summary in summaries:
        attempt, = [a for a in attempts if a["call_id"] == summary["call_id"]]
        assert summary["attempt_count"] == attempt["attempt"] == 1
        assert summary["trace_id"] == attempt["trace_id"]
        assert attempt["inter_attempt_gap_ms"] is None


async def test_parallel_chat_retry_does_not_consume_embedding_attempt_state(
        monkeypatch, caplog):
    chat_started, embedding_finished = asyncio.Event(), asyncio.Event()
    chat_count = 0

    class Search:
        async def search(self, **kwargs):
            async def documents():
                if False:
                    yield
            return documents()

    async def handler(request):
        nonlocal chat_count
        if request.url.path.endswith("/embeddings"):
            await asyncio.wait_for(chat_started.wait(), 3)
            embedding_finished.set()
            return httpx2.Response(200, json={
                "object": "list", "model": "test-embedding",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.125, 0.25]}],
                "usage": {"prompt_tokens": 7, "total_tokens": 7},
            })
        chat_count += 1
        if chat_count == 1:
            chat_started.set()
            await asyncio.wait_for(embedding_finished.wait(), 3)
            return httpx2.Response(429, json={"error": {"message": "private-answer"}},
                                  headers={"retry-after-ms": "1"})
        return httpx2.Response(200, json=completion())

    async def turn(client, operation):
        with trace_scope(new=True):
            if operation == "chat":
                return await chat(client)
            return await KnowledgeSearchClient(
                Search(), client, "test-embedding").search("private-question")

    async with sdk_client(monkeypatch, handler) as client:
        assert await asyncio.gather(turn(client, "chat"), turn(client, "embedding")) == [
            "private-answer", []]
    summaries = {s["operation"]: s for s in records(caplog, "llm.sdk_call_completed")}
    assert summaries["chat.completions"]["attempt_count"] == 2
    assert summaries["embeddings"]["attempt_count"] == 1
    assert len({s["trace_id"] for s in summaries.values()}) == 2
    for operation, expected in (("chat.completions", [0, 1]), ("embeddings", [0])):
        summary = summaries[operation]
        attempts = [a for a in records(caplog, "llm.http.attempt_completed")
                    if a["call_id"] == summary["call_id"]]
        assert [a["sdk_retry_count"] for a in attempts] == expected
        assert all(a["trace_id"] == summary["trace_id"] for a in attempts)
        assert attempts[0]["inter_attempt_gap_ms"] is None
    assert_private(caplog)


async def test_transport_trace_whitelists_fields_and_chains_existing_callback(
        monkeypatch, caplog):
    forwarded = []

    async def existing(event, info):
        forwarded.append((event, info))

    async def hook(request):
        request.extensions["trace"] = existing

    async def handler(request):
        trace = request.extensions["trace"]
        info = {"request": request, "headers": "private-header",
                "url": "private-query", "return_value": "private-answer"}
        for phase in ("connection.connect_tcp", "connection.start_tls",
                      "http11.send_request_headers", "http11.send_request_body",
                      "http11.receive_response_headers", "http11.receive_response_body"):
            await trace(phase + ".started", info)
            await asyncio.sleep(0)
            await trace(phase + ".complete", info)
        await trace("private-header.started", info)
        await trace("http11.receive_response_body.failed",
                    {"exception": httpx2.ReadError("private-exception")})
        return httpx2.Response(200, json=completion())

    async with sdk_client(monkeypatch, handler, request_hooks=[hook]) as client:
        assert await chat(client) == "private-answer"
    assert len(forwarded) == 14
    phases = records(caplog, "llm.http.phase")
    assert len(phases) == 13
    assert all("info" not in p for p in phases)
    assert phases[-1]["error_type"] == "ReadError"
    attempt, = records(caplog, "llm.http.attempt_completed")
    assert set(attempt["phase_durations_ms"]) == {
        "connection.connect_tcp", "connection.start_tls",
        "http11.send_request_headers", "http11.send_request_body",
        "http11.receive_response_headers", "http11.receive_response_body"}
    assert all(value >= 0 for value in attempt["phase_durations_ms"].values())
    assert_private(caplog)


async def test_trace_callback_cannot_record_against_an_ended_or_other_call(
        monkeypatch, caplog):
    saved, forwarded = [], []
    released = asyncio.Event()
    inherited_context_tasks = []

    async def existing(event, info):
        forwarded.append(event)

    async def hook(request):
        request.extensions["trace"] = existing

    async def handler(request):
        trace = request.extensions["trace"]

        async def late_callback():
            await released.wait()
            await trace("connection.connect_tcp.started", {})

        if saved:
            await saved[0]("connection.connect_tcp.started", {})
        else:
            inherited_context_tasks.append(asyncio.create_task(late_callback()))
        saved.append(trace)
        return httpx2.Response(200, json=completion())

    async with sdk_client(monkeypatch, handler, request_hooks=[hook]) as client:
        await chat(client)
        released.set()
        await asyncio.gather(*inherited_context_tasks)
        await saved[0]("connection.connect_tcp.started", {})
        await chat(client)
    assert forwarded == ["connection.connect_tcp.started"] * 3
    assert not records(caplog, "llm.http.phase")


async def test_inherited_context_cannot_attach_late_requests_to_a_finished_call(
        monkeypatch, caplog):
    released = asyncio.Event()
    tasks = []
    request_count = 0

    async def handler(request):
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            async def late_sdk_call():
                await released.wait()
                await client.chat.completions.create(model="test-chat", messages=[])
            tasks.append(asyncio.create_task(late_sdk_call()))
        else:
            assert "trace" not in request.extensions
        return httpx2.Response(200, json=completion())

    async with sdk_client(monkeypatch, handler) as client:
        await chat(client)
        released.set()
        await asyncio.gather(*tasks)
    assert request_count == 2
    assert len(records(caplog, "llm.http.response_headers")) == 1
    assert len(records(caplog, "llm.http.attempt_completed")) == 1
    assert len(records(caplog, "llm.sdk_call_completed")) == 1


@pytest.mark.parametrize("phase", [
    "proxy.start_tls", "socks.connect_tcp", "socks.start_tls",
    "http2.send_request_headers", "http2.send_request_body",
    "http2.receive_response_headers", "http2.receive_response_body",
])
async def test_proxy_and_http2_phases_use_installed_transport_event_names(
        monkeypatch, caplog, phase):
    async def handler(request):
        trace = request.extensions["trace"]
        await trace(phase + ".started", {"private-header": "private-credential"})
        await trace(phase + ".complete", {"return_value": "private-answer"})
        return httpx2.Response(200, json=completion())

    async with sdk_client(monkeypatch, handler) as client:
        await chat(client)
    attempt, = records(caplog, "llm.http.attempt_completed")
    assert phase in attempt["phase_durations_ms"]
    assert_private(caplog)


async def test_precise_diagnostic_clock_is_independent_of_shared_stage_clock(
        monkeypatch, caplog):
    from advisor_shared import telemetry

    monkeypatch.setattr(telemetry, "time", SimpleNamespace(monotonic=lambda: 10))

    async def handler(request):
        trace = request.extensions["trace"]
        await trace("http11.receive_response_headers.started", {})
        await asyncio.sleep(0.003)
        await trace("http11.receive_response_headers.complete", {})
        return httpx2.Response(200, json=completion())

    async with sdk_client(monkeypatch, handler) as client:
        with trace_scope() as trace:
            await chat(client)
    assert next(s for s in trace.timings if s.name == "llm.completion").duration_ms == 0
    attempt, = records(caplog, "llm.http.attempt_completed")
    assert attempt["phase_durations_ms"]["http11.receive_response_headers"] > 0
    assert attempt["duration_ms"] > 0


async def test_response_headers_are_validated_bounded_not_raw(monkeypatch, caplog):
    async def handler(request):
        data = completion()
        data.pop("usage")
        return httpx2.Response(200, json=data, headers={
            "x-request-id": "Bearer private-credential",
            "apim-request-id": REQUEST_ID,
            "x-ms-request-id": "x" * 300,
            "retry-after": "Wed, 01 Jan 2099 00:00:00 GMT",
            "retry-after-ms": "-1",
            "x-ms-retry-after-ms": "NaN",
            "x-ratelimit-remaining-requests": "private-header",
            "x-ratelimit-limit-requests": "999999999999999999999999",
            "x-ratelimit-limit-tokens": "10000",
            "x-ratelimit-reset-tokens": "1m30s",
        })

    async with sdk_client(monkeypatch, handler) as client:
        await chat(client)
    headers, = records(caplog, "llm.http.response_headers")
    assert headers["request_ids"] == {"apim-request-id": REQUEST_ID}
    assert headers["numeric_headers"] == {"x-ratelimit-limit-tokens": 10000}
    assert headers["headers_elapsed_ms"] >= 0
    summary, = records(caplog, "llm.sdk_call_completed")
    assert summary["usage"] == {}
    assert_private(caplog)


async def test_missing_metadata_and_untrusted_error_type_are_not_fabricated(
        monkeypatch, caplog):
    unknown_error = type("private-exception", (Exception,), {})

    async def hook(request):
        request.headers.pop("x-stainless-retry-count")

    async def handler(request):
        trace = request.extensions["trace"]
        await trace("connection.connect_tcp.failed",
                    {"exception": unknown_error("private-credential")})
        raise unknown_error("private-exception")

    async with sdk_client(monkeypatch, handler, request_hooks=[hook]) as client:
        with pytest.raises(APIConnectionError):
            await chat(client)
    attempts = records(caplog, "llm.http.attempt_completed")
    assert len(attempts) == 2
    for attempt in attempts:
        assert attempt["sdk_retry_count"] is None
        assert attempt["http_status"] is None
        assert attempt["headers_elapsed_ms"] is None
        assert attempt["phase_durations_ms"] == {}
        assert attempt["error_type"] == "OtherError"
    assert all(p["duration_ms"] is None and p["error_type"] == "OtherError"
               for p in records(caplog, "llm.http.phase"))
    assert_private(caplog)


async def test_uninstrumented_injected_client_does_not_claim_zero_http_attempts(caplog):
    from openai.types.chat import ChatCompletion

    async def create(**kwargs):
        return ChatCompletion.model_validate(completion())

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    assert await chat(client) == "private-answer"
    summary, = records(caplog, "llm.sdk_call_completed")
    assert summary["attempt_count"] is None
    assert summary["sdk_retry_count"] is None
    assert summary["http_status"] is None


async def test_loopback_transport_reports_body_separately_from_headers(
        monkeypatch, caplog):
    """Prove the installed httpx2 -> httpcore2 trace extension actually fires."""
    body_released = asyncio.Event()
    finished = asyncio.Event()
    body = json.dumps(completion()).encode()

    async def serve(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in headers.splitlines()
                          if line.lower().startswith(b"content-length:"))
            await reader.readexactly(length)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n")
            await writer.drain()
            await body_released.wait()
            writer.write(body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(httpx2.AsyncClient, "_get_proxy_map",
                        lambda *args, **kwargs: {})
    client = build_openai_client("2024-10-21")
    task = None
    try:
        task = asyncio.create_task(chat(client))
        async with asyncio.timeout(5):
            while not records(caplog, "llm.http.response_headers"):
                if task.done():
                    await task
                    pytest.fail("response hook did not emit header diagnostics")
                await asyncio.sleep(0.001)
            assert not task.done()
            assert not records(caplog, "llm.sdk_call_completed")
            assert not any(p["event"] == "http11.receive_response_body.complete"
                           for p in records(caplog, "llm.http.phase"))
            body_released.set()
            assert await task == "private-answer"
            await finished.wait()
    finally:
        body_released.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()
        server.close()
        await server.wait_closed()
    attempt, = records(caplog, "llm.http.attempt_completed")
    assert attempt["duration_ms"] >= attempt["headers_elapsed_ms"]
    assert "connection.connect_tcp" in attempt["phase_durations_ms"]
    assert "http11.receive_response_body" in attempt["phase_durations_ms"]
    assert "connection.start_tls" not in attempt["phase_durations_ms"]
    assert_private(caplog)
