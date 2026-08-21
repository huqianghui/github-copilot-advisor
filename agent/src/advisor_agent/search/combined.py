"""组合检索:KB 与 GitHub live 并行,预算内合并,KB 优先(spec 7.2)。
合并策略由代码保证,不交给模型决定。"""
import asyncio
import logging

from advisor_agent.search.models import SearchResult

logger = logging.getLogger(__name__)

SEARCH_BUDGET_SECONDS = 8.0


class CombinedSearch:
    def __init__(self, kb, live,
                 budget_seconds: float = SEARCH_BUDGET_SECONDS):
        self.kb = kb
        self.live = live
        self.budget = budget_seconds

    async def search_solutions(self, query: str,
                               product_area: str | None = None) -> dict:
        kb_task = asyncio.create_task(
            self.kb.search(query, product_area=product_area))
        live_task = asyncio.create_task(self.live.search(query))
        done, pending = await asyncio.wait(
            {kb_task, live_task}, timeout=self.budget)
        for task in pending:
            task.cancel()

        def collect(task) -> list[SearchResult]:
            if task not in done:
                return []
            try:
                return task.result()
            except Exception:
                logger.warning("search side failed", exc_info=True)
                return []

        kb_results = collect(kb_task)
        live_results = collect(live_task)
        seen_urls = {r.url for r in kb_results}
        merged = kb_results + [r for r in live_results
                               if r.url not in seen_urls]
        return {
            "no_results": not merged,
            "results": [r.model_dump() for r in merged],
        }
