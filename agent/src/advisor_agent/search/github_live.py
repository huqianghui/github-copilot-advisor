"""GitHub live 检索:open issues/discussions,查"还在讨论中"的问题(spec 7.2)。"""
import logging
import os

import httpx

from advisor_agent.search.models import SearchResult
from advisor_shared.telemetry import step

logger = logging.getLogger(__name__)

DEFAULT_LIVE_REPOS = [
    "microsoft/vscode",
    "microsoft/vscode-copilot-release",
    "microsoft/copilot-intellij-feedback",
    "github/copilot-cli",
    "community/community",
]

_BODY_SNIPPET_CHARS = 500

# GitHub 的 issue 搜索现在要求 query 必须带 `is:issue` 或 `is:pull-request`,
# 否则一律 422 "Query must include 'is:issue' or 'is:pull-request'"。
# 本工具的定位是"还在被讨论/跟进中的 issue"(spec 7.2),所以只加 is:issue。
_ISSUE_QUALIFIER = "is:issue"

# GitHub 限制:query 中的**检索词**最长 256 字符 —— 限定符(repo:/state:/is:)
# 不计入这个预算。超限返回 422 "Validation Failed"。
# https://docs.github.com/en/search-github/searching-on-github/troubleshooting-search-queries
_MAX_QUERY_TERM_CHARS = 256

# 实测(2026-09,打真实 API 拟合 20+ 组样本,4/4 盲预测命中):这 256 的账
# 不是按 len() 记的 —— **一个空格记 3 个字符**,其余字符各记 1 个(CJK 也只
# 记 1:200 个汉字、零空格的检索词能过)。空格记 3 大概是因为 GitHub 内部按
# " " -> "%20" 归一化后再量长度,但 CJK 不按这个规则,所以只把"空格记 3"当
# 成实测规律用,别外推。
# 后果:一段 40 词的英文报错 ≈ 230 个字符,len() 看着没超,实际成本 ≈ 308,
# 照样 422 —— 用 len() 花这笔预算是修不好的。
_SPACE_COST = 3


def _term_cost(terms: str) -> int:
    """检索词占用的预算 —— 空格记 3 个字符,其余各记 1 个。"""
    return len(terms) + (_SPACE_COST - 1) * terms.count(" ")


def _fit_terms(query: str) -> str:
    """把检索词裁进 GitHub 的预算,只在词边界下刀(半个单词会污染检索)。"""
    terms = " ".join(query.split())          # 归一化空白,成本计算才自洽
    if _term_cost(terms) <= _MAX_QUERY_TERM_CHARS:
        return terms

    kept: list[str] = []
    cost = 0
    for word in terms.split(" "):
        added = len(word) + (_SPACE_COST if kept else 0)
        if cost + added > _MAX_QUERY_TERM_CHARS:
            break
        kept.append(word)
        cost += added

    if kept:
        trimmed = " ".join(kept)
    else:
        # 首个词自己就超预算 —— 没有词边界可切(中文报错文本整段不含空格就是
        # 这种情况)。硬切也好过送出一个只剩限定符的 query:那会把仓库里所有
        # open issue 当成"相关结果"捞回来。
        trimmed = terms[:_MAX_QUERY_TERM_CHARS]

    logger.warning(
        "github-live 检索词超出 GitHub 的 %d 字符预算,已截断:"
        "%d 字符(成本 %d)-> %d 字符(成本 %d)",
        _MAX_QUERY_TERM_CHARS, len(terms), _term_cost(terms),
        len(trimmed), _term_cost(trimmed),
    )
    return trimmed


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
        terms = _fit_terms(query)
        repo_scope = " ".join(f"repo:{r}" for r in self.repos)
        qualifiers = f"{repo_scope} state:open {_ISSUE_QUALIFIER}"
        with step("search.github.request", top=top):
            resp = await self._client.get(
                "/search/issues",
                params={"q": f"{terms} {qualifiers}", "per_page": top})
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
