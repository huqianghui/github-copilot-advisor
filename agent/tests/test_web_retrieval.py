import asyncio
from types import SimpleNamespace

import httpx
import pytest
import respx

from advisor_agent.run_context import request_scope
from advisor_agent.search.models import SearchResult
from advisor_agent.search.web import TavilyProvider, WebSearchChain
from advisor_shared.telemetry import trace_scope


def result(title, url=None):
    return SearchResult(title=title, content="Copilot troubleshooting evidence",
                        url=url or f"https://example.test/{title}",
                        origin="web", score=1.0)


class Provider:
    name = "test"

    def __init__(self, trusted=(), general=(), error=None):
        self.results = {"trusted": list(trusted), "general": list(general)}
        self.error = error
        self.calls = []
        self.clients = []

    async def search(self, query, top, *, client=None):
        scope = "trusted" if "site:" in query else "general"
        self.calls.append(scope)
        self.clients.append(client)
        if self.error:
            raise self.error
        return self.results[scope]


async def test_retrieval_starts_both_scopes_and_merges_trusted_first():
    entered = {"trusted": asyncio.Event(), "general": asyncio.Event()}
    official = result("official", "https://docs.github.com/en/copilot/help")
    community = result("community")

    class ParallelProvider(Provider):
        async def search(self, query, top, *, client=None):
            scope = "trusted" if "site:" in query else "general"
            entered[scope].set()
            await entered["general" if scope == "trusted" else "trusted"].wait()
            return await super().search(query, top, client=client)

    provider = ParallelProvider([official], [community, official])
    with request_scope("test", False) as run:
        async with asyncio.timeout(2):
            outcome = await WebSearchChain([provider]).retrieve("q")
    assert outcome.status == "success"
    assert [r.title for r in outcome.results] == ["official", "community"]
    assert {a.scope for a in run.search_attempts} == {"trusted", "general"}
    assert provider.clients[0] is provider.clients[1]
    assert provider.clients[0].is_closed


@pytest.mark.parametrize("budget", [12.0, 0.05])
async def test_scope_failover_is_bounded_and_does_not_retry_same_provider(budget):
    first = Provider(error=httpx.ConnectTimeout("private-network-detail"))
    official = result("official", "https://docs.github.com/en/copilot/help")
    second = Provider([official], [result("other")])
    second.name = "backup"
    with request_scope("test", False) as run:
        outcome = await WebSearchChain(
            [first, second], budget_seconds=budget).retrieve("q")
    assert outcome.status == "success"
    assert outcome.failovers == 2
    assert sorted(first.calls) == sorted(second.calls) == ["general", "trusted"]
    assert len(run.search_attempts) == 4
    assert "private-network-detail" not in str(outcome)


async def test_total_budget_keeps_fast_results_and_cancels_slow_branch():
    cancelled = asyncio.Event()

    class PartiallySlow(Provider):
        async def search(self, query, top, *, client=None):
            if "site:" in query:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return await super().search(query, top, client=client)

    provider = PartiallySlow(general=[result("useful")])
    with request_scope("test", False) as run, trace_scope() as trace:
        async with asyncio.timeout(2):
            outcome = await WebSearchChain(
                [provider], timeout_seconds=1, budget_seconds=0.03).retrieve("q")
    assert outcome.status == "partial"
    assert [r.title for r in outcome.results] == ["useful"]
    assert cancelled.is_set()
    assert {s.scope: s.status for s in outcome.scopes} == {
        "trusted": "timeout", "general": "success"}
    timeout = next(a for a in run.search_attempts if a.status == "timeout")
    assert timeout.scope == "trusted"
    assert 0 < timeout.timeout_seconds <= 0.03
    span = next(s for s in trace.timings if s.span_id == timeout.span_id)
    assert span.attributes["scope"] == "trusted"


async def test_expired_total_budget_does_not_start_backup_provider(monkeypatch):
    from advisor_agent.search import web

    # A coarse Windows loop clock can still read before the deadline when
    # its timer fires. An expired budget must not start a backup in that tick.
    monkeypatch.setattr(web, "asyncio", SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(time=lambda: 0.0),
        create_task=asyncio.create_task, gather=asyncio.gather,
        wait_for=asyncio.wait_for, timeout=asyncio.timeout))
    class Slow(Provider):
        async def search(self, query, top, *, client=None):
            await asyncio.Event().wait()

    backup = Provider()
    outcome = await WebSearchChain(
        [Slow(), backup], timeout_seconds=1, budget_seconds=0.02).retrieve("q")
    assert outcome.status == "timeout"
    assert not outcome.results
    assert backup.calls == []


@pytest.mark.parametrize(("error", "status"), [
    (None, "empty"),
    (TimeoutError("private"), "timeout"),
    (httpx.ConnectError("private"), "error"),
])
async def test_retrieval_distinguishes_empty_timeout_and_error(error, status):
    outcome = await WebSearchChain([Provider(error=error)]).retrieve("q")
    assert outcome.status == status
    assert outcome.results == []
    assert {s.status for s in outcome.scopes} == {status}
    assert "private" not in str(outcome)


async def test_not_configured_is_not_reported_as_empty():
    with request_scope("test", False) as run:
        outcome = await WebSearchChain([]).retrieve("q")
    assert outcome.status == "not_configured"
    assert {a.scope for a in run.search_attempts} == {"trusted", "general"}
    assert all(a.status == "not_configured" for a in run.search_attempts)


async def test_request_cancellation_awaits_both_scopes_and_closes_client():
    both_started = asyncio.Event()
    clients, stopped = [], []

    class Waiting(Provider):
        async def search(self, query, top, *, client=None):
            clients.append(client)
            if len(clients) == 2:
                both_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(True)

    with request_scope("test", False) as run:
        async with asyncio.timeout(2):
            task = asyncio.create_task(WebSearchChain([Waiting()]).retrieve("q"))
            await both_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert stopped == [True, True]
    assert clients[0] is clients[1] and clients[0].is_closed
    assert [a.status for a in run.search_attempts] == ["cancelled", "cancelled"]


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
@pytest.mark.parametrize("field", ["budget_seconds", "timeout_seconds"])
def test_invalid_budgets_fail_before_requests(field, value):
    with pytest.raises(ValueError, match=field):
        WebSearchChain([], **{field: value})


@respx.mock
async def test_real_provider_retrieval_makes_only_two_http_requests():
    route = respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(200, json={"results": [{
            "title": "Copilot docs", "content": "Useful details",
            "url": "https://docs.github.com/en/copilot/help", "score": 1.0,
        }]}))
    outcome = await WebSearchChain([TavilyProvider(api_key="test")]).retrieve("q")
    assert outcome.status == "success"
    assert len(outcome.results) == 1
    assert route.call_count == 2
