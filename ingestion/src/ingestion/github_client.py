"""GitHub REST 客户端:翻页 + 限流退避。"""
import asyncio
import os
from collections.abc import AsyncIterator

import httpx


class GitHubClient:
    def __init__(self, token: str | None = None,
                 base_url: str = "https://api.github.com"):
        token = token or os.environ.get("GITHUB_TOKEN")
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers,
                                         timeout=30)

    async def paged_get(self, path: str, params: dict) -> AsyncIterator[dict]:
        page = 1
        while True:
            resp = await self._get_with_retry(path, {**params, "per_page": 100,
                                                     "page": page})
            items = resp.json()
            if not items:
                return
            for item in items:
                yield item
            if len(items) < 100:
                return
            page += 1

    async def _get_with_retry(self, path: str, params: dict) -> httpx.Response:
        for attempt in range(3):
            resp = await self._client.get(path, params=params)
            if resp.status_code in (403, 429):
                wait = float(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                await asyncio.sleep(min(wait, 60))
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()
        return resp

    async def graphql(self, query: str, variables: dict) -> dict:
        resp = await self._client.post("/graphql",
                                       json={"query": query,
                                             "variables": variables})
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        return data["data"]

    async def aclose(self):
        await self._client.aclose()
