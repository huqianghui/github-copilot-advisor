"""组合检索:KB 与 GitHub live 并行,预算内合并,KB 优先(spec 7.2)。
合并策略由代码保证,不交给模型决定。"""
import asyncio
import logging
from collections.abc import Awaitable

from advisor_agent.search.models import SearchResult
from advisor_agent.search.telemetry import SearchTrace
from advisor_agent.search.web import validate_seconds
from advisor_shared.telemetry import step, timed

logger = logging.getLogger(__name__)

SEARCH_BUDGET_SECONDS = 12.0
GITHUB_SEARCH_BUDGET_SECONDS = 4.0


async def _search_with_budget(operation: Awaitable[list[SearchResult]],
                              budget_seconds: float) -> list[SearchResult]:
    # Convert deadline cancellation to TimeoutError before SearchTrace records it.
    timer = asyncio.timeout(budget_seconds)
    async with timer:
        results = await operation
    # A dependency may suppress cancellation; its late results still miss the deadline.
    if timer.expired():
        raise TimeoutError("search budget exhausted")
    return results


class CombinedSearch:
    def __init__(self, kb, live,
                 budget_seconds: float = SEARCH_BUDGET_SECONDS, *,
                 live_budget_seconds: float = GITHUB_SEARCH_BUDGET_SECONDS):
        self.kb = kb
        self.live = live
        self.budget = validate_seconds(budget_seconds, "budget_seconds")
        self.live_budget = min(
            self.budget, validate_seconds(live_budget_seconds, "live_budget_seconds"))

    @timed("search.combined")
    async def search_solutions(self, query: str,
                               product_area: str | None = None) -> dict:
        kb_trace = SearchTrace("kb", "azure_ai_search", self.budget)
        live_trace = SearchTrace("github-live", "github", self.live_budget)
        kb_task = asyncio.create_task(kb_trace.run(
            _search_with_budget(
                self.kb.search(query, product_area=product_area), self.budget)))
        live_task = asyncio.create_task(live_trace.run(
            _search_with_budget(self.live.search(query), self.live_budget)))
        tasks = (kb_task, live_task)
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Finish cancellation before emitting the event, including on interruption.
            await asyncio.gather(*tasks, return_exceptions=True)

        def collect(task: asyncio.Task[list[SearchResult]]) -> list[SearchResult]:
            try:
                return task.result()
            except Exception as error:
                logger.warning("search side failed error_type=%s", type(error).__name__)
                return []

        kb_results = collect(kb_task)
        live_results = collect(live_task)
        with step("search.merge", kb_count=len(kb_results),
                  live_count=len(live_results)) as timing:
            seen_urls = {r.url for r in kb_results}
            merged = kb_results + [r for r in live_results
                                   if r.url not in seen_urls]
            timing.attributes["result_count"] = len(merged)
            return {
                "no_results": not merged,
                "results": [r.model_dump() for r in merged],
            }
