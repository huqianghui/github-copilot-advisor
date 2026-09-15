import asyncio
from types import SimpleNamespace

import httpx
import pytest

from advisor_agent.run_context import new_run
from advisor_agent.search.combined import CombinedSearch
from advisor_agent.search.web import WebSearchChain
from test_search_solutions import StubKB, StubLive, r
from test_web_search import StubProvider


def attempts(run):
    return [attempt.model_dump() for attempt in getattr(run, "search_attempts", [])]


async def test_combined_records_each_source_before_deduplication():
    run = new_run()
    out = await CombinedSearch(
        StubKB([r("kb", "kb", url="https://same")]),
        StubLive([r("duplicate", "github-live", url="https://same"),
                  r("live", "github-live")])).search_solutions("q")
    assert len(out["results"]) == 2
    recorded = {attempt["source"]: attempt for attempt in attempts(run)}
    assert set(recorded) == {"kb", "github-live"}
    assert recorded["kb"]["provider"] == "azure_ai_search"
    assert recorded["kb"]["status"] == "success"
    assert recorded["kb"]["result_count"] == 1
    assert recorded["github-live"]["result_count"] == 2
    assert all(attempt["duration_ms"] >= 0 for attempt in recorded.values())


async def test_combined_distinguishes_schema_error_from_empty_without_secrets():
    class SchemaError(RuntimeError):
        status_code = 400
        error = SimpleNamespace(code="CannotSearchWithoutSearchableFields")

    run = new_run()
    out = await CombinedSearch(
        StubKB(error=SchemaError("api-key=private-test-value")),
        StubLive()).search_solutions("q")
    assert out == {"no_results": True, "results": []}
    recorded = {attempt["source"]: attempt for attempt in attempts(run)}
    assert set(recorded) == {"kb", "github-live"}
    assert recorded["kb"]["status"] == "error"
    assert recorded["kb"]["result_count"] is None
    assert recorded["kb"]["error_type"] == "SchemaError"
    assert recorded["kb"]["http_status"] == 400
    assert recorded["kb"]["error_code"] == "CannotSearchWithoutSearchableFields"
    assert recorded["github-live"]["status"] == "empty"
    assert recorded["github-live"]["result_count"] == 0
    assert "private-test-value" not in str(recorded)


async def test_budget_timeout_is_not_reported_as_an_empty_search():
    run = new_run()
    out = await CombinedSearch(
        StubKB([r("kb", "kb")]), StubLive(delay=5),
        budget_seconds=0.02).search_solutions("q")
    assert [item["title"] for item in out["results"]] == ["kb"]
    recorded = {attempt["source"]: attempt for attempt in attempts(run)}
    assert set(recorded) == {"kb", "github-live"}
    assert recorded["github-live"]["status"] == "timeout"
    assert recorded["github-live"]["result_count"] is None
    assert recorded["github-live"]["timeout_seconds"] == 0.02
    assert recorded["github-live"]["error_type"] == "TimeoutError"
    assert recorded["github-live"]["duration_ms"] > recorded["kb"]["duration_ms"]


async def test_budget_expiry_is_logged_as_timeout_before_event_emission(caplog):
    from advisor_shared.telemetry import trace_scope

    run = new_run()
    with trace_scope() as trace:
        await CombinedSearch(StubKB(), StubLive(delay=5),
                             budget_seconds=0.01).search_solutions("q")
    attempt = next(a for a in run.search_attempts if a.source == "github-live")
    span = next(s for s in trace.timings if s.span_id == attempt.span_id)
    assert span.status == "timeout"
    assert span.error_type == "TimeoutError"
    log = next(r.telemetry for r in caplog.records
               if r.msg == "step_completed"
               and r.telemetry["span_id"] == span.span_id)
    assert log["status"] == "timeout"


async def test_combined_cancellation_cleans_up_both_searches():
    started = asyncio.Event()
    stopped = []

    class WaitingSearch:
        async def search(self, query, **kwargs):
            started.set()
            try:
                await asyncio.sleep(5)
            finally:
                stopped.append(True)

    run = new_run()
    task = asyncio.create_task(
        CombinedSearch(WaitingSearch(), WaitingSearch()).search_solutions("q"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(stopped) == 2
    assert [attempt["status"] for attempt in attempts(run)] == [
        "cancelled", "cancelled"]


async def test_web_records_each_failover_and_keeps_repeated_calls():
    response = httpx.Response(
        429, request=httpx.Request("GET", "https://example.test/?key=private-value"))
    error = httpx.HTTPStatusError(
        "private-value", request=response.request, response=response)
    run = new_run()
    chain = WebSearchChain([
        StubProvider("tavily", error=error),
        StubProvider("brave", [r("web", "web")]),
    ])
    for _ in range(2):
        results, failovers = await chain.search("q", scope="general")
        assert len(results) == 1 and failovers == 1
    recorded = attempts(run)
    assert len(recorded) == 4
    assert [attempt["provider"] for attempt in recorded] == [
        "tavily", "brave", "tavily", "brave"]
    assert [attempt["status"] for attempt in recorded] == [
        "error", "success", "error", "success"]
    assert recorded[0]["http_status"] == 429
    assert recorded[0]["error_type"] == "HTTPStatusError"
    assert recorded[0]["result_count"] is None
    assert recorded[1]["result_count"] == 1
    assert "private-value" not in str(recorded)


@pytest.mark.parametrize("error", [
    TimeoutError("private-timeout-detail"),
    httpx.ConnectTimeout("private-timeout-detail"),
])
async def test_sdk_timeout_is_distinguished_from_other_errors(error):
    run = new_run()
    results, failovers = await WebSearchChain([
        StubProvider("tavily", error=error)]).search("q")
    assert results == [] and failovers == 1
    recorded = attempts(run)
    assert len(recorded) == 1
    assert recorded[0]["status"] == "timeout"
    assert recorded[0]["error_type"] == type(error).__name__
    assert "private-timeout-detail" not in str(recorded)


async def test_web_budget_timeout_then_empty_provider_are_separate_attempts():
    class SlowProvider:
        name = "slow"

        async def search(self, query, top):
            await asyncio.sleep(5)
            return []

    run = new_run()
    results, failovers = await WebSearchChain(
        [SlowProvider(), StubProvider("empty")],
        timeout_seconds=0.01).search("q")
    assert results == [] and failovers == 2
    recorded = attempts(run)
    assert len(recorded) == 2
    assert recorded[0]["status"] == "timeout"
    assert recorded[0]["timeout_seconds"] == 0.01
    assert recorded[1]["status"] == "empty"
    assert recorded[1]["result_count"] == 0


async def test_no_web_provider_is_explicit_and_context_resets():
    run = new_run()
    assert await WebSearchChain([]).search("q") == ([], 0)
    recorded = attempts(run)
    assert len(recorded) == 1
    assert recorded[0]["status"] == "not_configured"
    assert recorded[0]["provider"] is None
    assert recorded[0]["result_count"] is None
    assert attempts(new_run()) == []
