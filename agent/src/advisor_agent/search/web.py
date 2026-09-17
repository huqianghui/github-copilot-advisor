"""web 搜索 provider 链:配置驱动、按序 failover(spec 7.2)。"""
import asyncio
import logging
import math
import os
from typing import Protocol

import httpx

from advisor_agent.search.models import SearchResult
from advisor_agent.search.retrieval import WebRetrievalResult, WebScopeResult
from advisor_agent.search.source_policy import (
    WebSearchScope,
    source_confidence,
    web_query,
)
from advisor_agent.search.telemetry import SearchTrace
from advisor_shared.telemetry import step

logger = logging.getLogger(__name__)


def validate_seconds(value: str | float, name: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise ValueError(f"{name} must be finite and greater than zero") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return seconds


class WebSearchProvider(Protocol):
    name: str

    async def search(self, query: str, top: int, *,
                     client: httpx.AsyncClient | None = None
                     ) -> list[SearchResult]: ...


async def _provider_search(provider: WebSearchProvider, query: str, top: int,
                           client: httpx.AsyncClient,
                           timer: asyncio.Timeout) -> list[SearchResult]:
    async with timer:
        return await provider.search(query, top, client=client)


class TavilyProvider:
    name = "tavily"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")

    async def search(self, query: str, top: int, *,
                     client: httpx.AsyncClient | None = None) -> list[SearchResult]:
        if client is None:
            async with httpx.AsyncClient(timeout=10) as owned:
                return await self.search(query, top, client=owned)
        resp = await client.post(
            "https://api.tavily.com/search",
            json={"api_key": self.api_key, "query": query,
                  "max_results": top})
        resp.raise_for_status()
        return [
            SearchResult(title=item["title"], content=item.get("content", ""),
                         url=item["url"], origin="web",
                         score=item.get("score") or 0.0)
            for item in resp.json().get("results", [])
        ]


class BraveProvider:
    name = "brave"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("BRAVE_API_KEY", "")

    async def search(self, query: str, top: int, *,
                     client: httpx.AsyncClient | None = None) -> list[SearchResult]:
        if client is None:
            async with httpx.AsyncClient(timeout=10) as owned:
                return await self.search(query, top, client=owned)
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": top},
            headers={"X-Subscription-Token": self.api_key})
        resp.raise_for_status()
        return [
            SearchResult(title=item["title"],
                         content=item.get("description", ""),
                         url=item["url"], origin="web", score=0.0)
            for item in resp.json().get("web", {}).get("results", [])
        ]


class WebSearchChain:
    def __init__(self, providers: list[WebSearchProvider],
                 timeout_seconds: float = 6.0, budget_seconds: float = 12.0):
        self.providers = providers
        self.timeout = validate_seconds(timeout_seconds, "timeout_seconds")
        self.budget = validate_seconds(budget_seconds, "budget_seconds")

    async def retrieve(self, query: str, top: int = 5) -> WebRetrievalResult:
        with step("search.web.retrieve", budget_seconds=self.budget) as timing:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                deadline = asyncio.get_running_loop().time() + self.budget
                tasks = [
                    asyncio.create_task(self._search_scope(
                        query, top, scope=scope, client=client, deadline=deadline))
                    for scope in ("trusted", "general")
                ]
                try:
                    scopes = await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            with step("search.web.merge") as merge:
                seen: set[str] = set()
                results = []
                for scope in scopes:
                    for item in scope.results:
                        if item.url not in seen:
                            seen.add(item.url)
                            results.append(item)
                results.sort(key=lambda item: source_confidence(item) != "high")
                merge.attributes["result_count"] = len(results)
            statuses = {scope.status for scope in scopes}
            if results:
                status = "success" if statuses <= {"success", "empty"} else "partial"
            elif statuses == {"not_configured"}:
                status = "not_configured"
            elif "timeout" in statuses:
                status = "timeout"
            elif "error" in statuses:
                status = "error"
            else:
                status = "empty"
            timing.status = "degraded" if status == "partial" else status
            timing.attributes["result_count"] = len(results)
            return WebRetrievalResult(status=status, results=results, scopes=scopes)

    async def search(self, query: str,
                     top: int = 5, *,
                     scope: WebSearchScope = "trusted"
                     ) -> tuple[list[SearchResult], int]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            outcome = await self._search_scope(query, top, scope=scope, client=client)
        return outcome.results, outcome.failovers

    async def _search_scope(self, query: str, top: int, *,
                            scope: WebSearchScope, client: httpx.AsyncClient,
                            deadline: float | None = None) -> WebScopeResult:
        query = web_query(query, scope)
        outcome = WebScopeResult(scope=scope)
        had_response = False
        with step("search.web.scope", scope=scope) as timing:
            try:
                if not self.providers:
                    with SearchTrace("web", None, self.timeout, scope=scope) as attempt:
                        attempt.status = "not_configured"
                        attempt.span_id = timing.span_id
                    outcome.attempts.append(attempt)
                    outcome.status = "not_configured"
                    return outcome
                for provider in self.providers:
                    timeout = self.timeout
                    budget_limited = False
                    if deadline is not None:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            outcome.status = "timeout"
                            return outcome
                        budget_limited = remaining <= self.timeout
                        timeout = min(timeout, remaining)
                    trace = SearchTrace("web", provider.name, timeout, scope=scope)
                    outcome.attempts.append(trace.attempt)
                    timer = asyncio.timeout(timeout)
                    try:
                        results = await trace.run(
                            _provider_search(provider, query, top, client, timer))
                    except Exception as error:
                        logger.warning("provider %s scope=%s failed error_type=%s",
                                       provider.name, scope, type(error).__name__)
                        outcome.failovers += 1
                        # Coarse clocks may still read before an already-fired
                        # deadline; trust timer expiry, not a second clock read.
                        if budget_limited and timer.expired():
                            outcome.status = "timeout"
                            return outcome
                        continue
                    had_response = True
                    with step("search.web.filter", provider=provider.name,
                              scope=scope, raw_count=len(results)) as filtering:
                        results = [
                            result for result in results if result.content.strip()
                            and (scope == "general" or source_confidence(result) == "high")
                        ]
                        results.sort(key=lambda result: source_confidence(result) != "high")
                        filtering.attributes["result_count"] = len(results)
                        filtering.status = "success" if results else "empty"
                    if results:
                        outcome.results = results
                        outcome.status = "success"
                        return outcome
                    logger.info("provider %s returned no usable %s results",
                                provider.name, scope)
                    outcome.failovers += 1
                if had_response:
                    outcome.status = "empty"
                elif any(a.status == "timeout" for a in outcome.attempts):
                    outcome.status = "timeout"
                else:
                    outcome.status = "error"
                return outcome
            finally:
                timing.status = outcome.status
                timing.attributes["result_count"] = len(outcome.results)
