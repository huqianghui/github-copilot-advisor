import asyncio

from advisor_agent.search.combined import SEARCH_BUDGET_SECONDS, CombinedSearch
from advisor_agent.search.models import SearchResult


def r(title, origin, url=None, score=2.0):
    return SearchResult(title=title, content=f"c-{title}",
                        url=url or f"https://x/{title}",
                        origin=origin, score=score)


class StubKB:
    def __init__(self, results=(), delay=0.0, error=None):
        self.results, self.delay, self.error = list(results), delay, error

    async def search(self, query, product_area=None, top=5):
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.results


class StubLive:
    def __init__(self, results=(), delay=0.0, error=None):
        self.results, self.delay, self.error = list(results), delay, error

    async def search(self, query, top=5):
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.results


async def test_merges_kb_first_and_dedupes_by_url():
    combined = CombinedSearch(
        StubKB([r("kb1", "kb", url="https://same")]),
        StubLive([r("live1", "github-live", url="https://same"),
                  r("live2", "github-live")]))
    out = await combined.search_solutions("q")
    assert out["no_results"] is False
    assert [x["title"] for x in out["results"]] == ["kb1", "live2"]


async def test_slow_side_dropped_after_budget():
    combined = CombinedSearch(
        StubKB([r("kb1", "kb")]),
        StubLive([r("live1", "github-live")], delay=5),
        budget_seconds=0.05)
    out = await combined.search_solutions("q")
    assert [x["title"] for x in out["results"]] == ["kb1"]


async def test_one_side_error_uses_other():
    combined = CombinedSearch(
        StubKB(error=RuntimeError("search down")),
        StubLive([r("live1", "github-live")]))
    out = await combined.search_solutions("q")
    assert out["no_results"] is False
    assert [x["title"] for x in out["results"]] == ["live1"]


async def test_both_empty_signals_no_results():
    out = await CombinedSearch(StubKB(), StubLive()).search_solutions("q")
    assert out == {"no_results": True, "results": []}


def test_budget_constant():
    assert SEARCH_BUDGET_SECONDS == 8.0
