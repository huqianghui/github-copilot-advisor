"""Real OpenAI/httpx retry pipeline; only the external HTTP transport is replaced."""
import asyncio
import logging

import httpx2
from openai import AsyncOpenAI
import pytest

from advisor_agent.llm_diagnostics import DiagnosticAsyncHttpClient, sdk_call_diagnostics
from advisor_shared.telemetry import step, trace_scope
from teams_adapter.cloud.benchmark_observations import (
    PreparationObservations, preparation_telemetry,
)


def completion():
    return {
        "id": "fixture", "object": "chat.completion", "created": 1, "model": "fixture",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "private-answer"}}],
    }


def client(handler):
    return AsyncOpenAI(
        api_key="private-key", base_url="https://offline.invalid", max_retries=1,
        http_client=DiagnosticAsyncHttpClient(
            transport=httpx2.MockTransport(handler), trust_env=False))


async def chat(sdk):
    with step("llm.completion"), sdk_call_diagnostics("chat.completions"):
        return await sdk.chat.completions.create(
            model="fixture", messages=[{"role": "user", "content": "private-question"}])


async def test_projection_counts_actual_chat_retries_and_embedding_attempts(caplog):
    caplog.set_level(logging.DEBUG, logger="advisor_agent.llm_diagnostics")
    requests = []

    async def handler(request):
        requests.append(request.url.path)
        if len(requests) == 1:
            return httpx2.Response(429, headers={"retry-after-ms": "1"}, json={
                "error": {"message": "private-throttle-body"}})
        if request.url.path.endswith("/embeddings"):
            return httpx2.Response(200, json={
                "object": "list", "model": "fixture",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.25]}],
            })
        return httpx2.Response(200, json=completion())

    async with client(handler) as sdk:
        with trace_scope(new=True, trace_id="job-model") as trace:
            response = await chat(sdk)
            chat_telemetry = preparation_telemetry(PreparationObservations(trace=trace))
            assert [item.model_dump() for item in chat_telemetry.dependencies] == [
                {"dependency": "model", "calls": 2, "throttled": 1}]
            with sdk_call_diagnostics("embeddings"):
                await sdk.embeddings.create(model="fixture", input="private-question",
                                            encoding_format="float")
    assert response.choices[0].message.content == "private-answer"
    assert requests == ["/chat/completions", "/chat/completions", "/embeddings"]
    telemetry = preparation_telemetry(PreparationObservations(trace=trace))
    assert [item.model_dump() for item in telemetry.dependencies] == [
        {"dependency": "model", "calls": 3, "throttled": 1}]
    assert telemetry.model_rounds == 1
    assert "private-" not in telemetry.model_dump_json()
    assert "offline.invalid" not in telemetry.model_dump_json()
    assert preparation_telemetry(PreparationObservations(trace=trace)) == telemetry


@pytest.mark.parametrize("debug", [True, False])
async def test_parallel_job_projection_is_isolated_and_debug_off_is_unavailable(caplog, debug):
    caplog.set_level(logging.DEBUG if debug else logging.INFO, logger="advisor_agent.llm_diagnostics")
    arrived = asyncio.Event()
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        if count == 2:
            arrived.set()
        await asyncio.wait_for(arrived.wait(), 2)
        return httpx2.Response(200, json=completion())

    async def turn(sdk, trace_id):
        with trace_scope(new=True, trace_id=trace_id) as trace:
            await chat(sdk)
        return trace, preparation_telemetry(PreparationObservations(trace=trace))

    async with client(handler) as sdk:
        first, second = await asyncio.gather(turn(sdk, "job-a"), turn(sdk, "job-b"))
    assert count == 2
    for trace, telemetry in (first, second):
        assert telemetry.model_rounds == 1
        assert [item.model_dump() for item in telemetry.dependencies] == (
            [{"dependency": "model", "calls": 1, "throttled": 0}] if debug else [])
    if debug:
        assert first[0].model_http_attempts[0].call_id != second[0].model_http_attempts[0].call_id


def test_synthetic_and_unobserved_model_spans_never_invent_http_counts():
    with trace_scope(new=True) as trace:
        with step("benchmark.synthetic"):
            pass
    synthetic = preparation_telemetry(PreparationObservations(trace=trace), synthetic=True)
    assert synthetic.dependencies == [] and synthetic.model_rounds is None
    with trace_scope(new=True) as trace:
        with step("llm.completion"):
            pass
    assert preparation_telemetry(PreparationObservations(trace=trace)).dependencies == []
