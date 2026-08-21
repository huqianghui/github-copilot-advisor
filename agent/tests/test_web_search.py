import httpx
import respx

from advisor_agent.search.models import SearchResult
from advisor_agent.search.web import (
    BraveProvider,
    TavilyProvider,
    WebSearchChain,
)


class StubProvider:
    def __init__(self, name, results=(), error=None):
        self.name = name
        self._results, self._error = list(results), error
        self.called = False

    async def search(self, query, top):
        self.called = True
        if self._error:
            raise self._error
        return self._results


def r(title):
    return SearchResult(title=title, content="c", url=f"https://w/{title}",
                        origin="web", score=1.0)


async def test_first_provider_success_no_failover():
    a, b = StubProvider("a", [r("x")]), StubProvider("b", [r("y")])
    results, failovers = await WebSearchChain([a, b]).search("q")
    assert [x.title for x in results] == ["x"]
    assert failovers == 0 and b.called is False


async def test_failover_on_error_then_success():
    a = StubProvider("a", error=RuntimeError("quota"))
    b = StubProvider("b", [r("y")])
    results, failovers = await WebSearchChain([a, b]).search("q")
    assert [x.title for x in results] == ["y"]
    assert failovers == 1


async def test_empty_results_also_failover():
    a, b = StubProvider("a", []), StubProvider("b", [r("y")])
    results, failovers = await WebSearchChain([a, b]).search("q")
    assert [x.title for x in results] == ["y"] and failovers == 1


async def test_all_fail_returns_empty_and_count():
    a = StubProvider("a", error=RuntimeError("x"))
    b = StubProvider("b", [])
    results, failovers = await WebSearchChain([a, b]).search("q")
    assert results == [] and failovers == 2


@respx.mock
async def test_tavily_provider_parses_response():
    respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(200, json={"results": [
            {"title": "Copilot 1.97 release", "content": "notes...",
             "url": "https://blog/x", "score": 0.9},
        ]}))
    results = await TavilyProvider(api_key="k").search("copilot update", top=3)
    assert results[0].origin == "web"
    assert results[0].title == "Copilot 1.97 release"


@respx.mock
async def test_brave_provider_parses_response():
    respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(200, json={"web": {"results": [
            {"title": "t", "description": "d", "url": "https://b/x"},
        ]}}))
    results = await BraveProvider(api_key="k").search("q", top=3)
    assert results[0].url == "https://b/x"
