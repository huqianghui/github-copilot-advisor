"""GitHub live 检索:open issues/discussions,查"还在讨论中"的问题(spec 7.2)。"""
import os

import httpx

from advisor_agent.search.models import SearchResult

DEFAULT_LIVE_REPOS = [
    "microsoft/vscode",
    "microsoft/vscode-copilot-release",
    "microsoft/copilot-intellij-feedback",
    "github/copilot-cli",
    "community/community",
]

_BODY_SNIPPET_CHARS = 500


class GitHubLiveSearchClient:
    def __init__(self, token: str | None = None,
                 repos: list[str] | None = None,
                 base_url: str = "https://api.github.com"):
        token = token or os.environ.get("GITHUB_TOKEN")
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers,
                                         timeout=10)
        self.repos = repos or DEFAULT_LIVE_REPOS

    async def search(self, query: str, top: int = 5) -> list[SearchResult]:
        repo_scope = " ".join(f"repo:{r}" for r in self.repos)
        resp = await self._client.get(
            "/search/issues",
            params={"q": f"{query} {repo_scope} state:open",
                    "per_page": top})
        resp.raise_for_status()
        return [
            SearchResult(
                title=item["title"],
                content=(item.get("body") or "")[:_BODY_SNIPPET_CHARS],
                url=item["html_url"],
                origin="github-live",
                score=item.get("score") or 0.0,
            )
            for item in resp.json().get("items", [])
        ]

    async def aclose(self):
        await self._client.aclose()
