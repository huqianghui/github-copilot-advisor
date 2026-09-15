"""组合检索:KB 与 GitHub live 并行,预算内合并,KB 优先(spec 7.2)。
合并策略由代码保证,不交给模型决定。"""
import asyncio
import logging

from advisor_agent.search.models import SearchResult
from advisor_agent.search.telemetry import SearchTrace
from advisor_shared.telemetry import step, timed

logger = logging.getLogger(__name__)

SEARCH_BUDGET_SECONDS = 8.0


class CombinedSearch:
    def __init__(self, kb, live,
                 budget_seconds: float = SEARCH_BUDGET_SECONDS):
        self.kb = kb
        self.live = live
        self.budget = budget_seconds

    @timed("search.combined")
    async def search_solutions(self, query: str,
                               product_area: str | None = None) -> dict:
        kb_trace = SearchTrace("kb", "azure_ai_search", self.budget)
        live_trace = SearchTrace("github-live", "github", self.budget)
        kb_task = asyncio.create_task(kb_trace.run(
            self.kb.search(query, product_area=product_area)))
        live_task = asyncio.create_task(live_trace.run(self.live.search(query)))
        traces = {kb_task: kb_trace, live_task: live_trace}
        pending = set()
        try:
            done, pending = await asyncio.wait(traces, timeout=self.budget)
        finally:
            for task in traces:
                if not task.done():
                    if task in pending:
                        traces[task].mark_budget_timeout()
                    task.cancel()
            # Finish cancellation before emitting the event, including on interruption.
            await asyncio.gather(*traces, return_exceptions=True)

        def collect(task) -> list[SearchResult]:
            if task not in done:
                return []
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
