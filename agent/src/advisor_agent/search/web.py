"""web 搜索 provider 链:配置驱动、按序 failover(spec 7.2)。"""
import asyncio
import logging
import os
from typing import Protocol

import httpx

from advisor_agent.search.models import SearchResult

logger = logging.getLogger(__name__)


class WebSearchProvider(Protocol):
    name: str

    async def search(self, query: str, top: int) -> list[SearchResult]: ...


class TavilyProvider:
    name = "tavily"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")

    async def search(self, query: str, top: int) -> list[SearchResult]:
        async with httpx.AsyncClient(timeout=10) as client:
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

    async def search(self, query: str, top: int) -> list[SearchResult]:
        async with httpx.AsyncClient(timeout=10) as client:
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
                 timeout_seconds: float = 6.0):
        self.providers = providers
        self.timeout = timeout_seconds

    async def search(self, query: str,
                     top: int = 5) -> tuple[list[SearchResult], int]:
        failovers = 0
        for provider in self.providers:
            try:
                results = await asyncio.wait_for(
                    provider.search(query, top), self.timeout)
                if results:
                    return results, failovers
                logger.info("provider %s returned empty", provider.name)
            except Exception:
                logger.warning("provider %s failed", provider.name,
                               exc_info=True)
            failovers += 1
        return [], failovers
