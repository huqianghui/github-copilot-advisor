# 计划 2:Agent & Tools 编排 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建平台无关的 advisor agent 核心:MAF(Microsoft Agent Framework)+ Azure OpenAI 编排,三个工具(search_solutions 组合检索 / web_search 多 provider failover / escalate_to_human),多轮会话,QueryPlanner/AnswerEvaluator 扩展点,结构化事件。

**Architecture:** `agent` 项目只依赖 `shared`。所有外部能力(AI Search、GitHub、web 搜索、LLM)都在接口后面:MAF 的使用收敛在一个 `MAFBackend` 适配器里(`AgentBackend` 协议),单元测试全部用 fake,MAF/真实资源只出现在 integration 测试。工具通过 `contextvars` 的 `RunContext` 与核心管线交换副作用(mentions、瀑布阶段),避免解析 LLM 自由文本。

**Tech Stack:** Python 3.11+,agent-framework-core ≥1.0 + agent-framework-azure-ai ≥1.0(MAF,2026-04 GA),openai(query embedding),azure-search-documents,httpx,PyYAML,pytest + respx。

**Spec:** `docs/superpowers/specs/2026-08-21-copilot-advisor-design.md`(本计划实现其第 3、7 节及 10.2 事件部分)

## Global Constraints

- 依赖方向:`agent → shared`,禁止反向;agent 对渠道零感知,契约是 `AdvisorRequest`/`AdvisorResponse`
- 索引字段名与计划 1 一致:`title, content, keywords, url, source, product_area`;semantic 配置名 `default`;向量字段 `content_vector`
- `search_solutions` 总超时预算 8 秒(`SEARCH_BUDGET_SECONDS = 8.0`);合并时 KB 结果排前;两路都空/失败返回 `no_results=True`
- KB 命中阈值:semantic reranker score ≥ 1.5(`MIN_RERANKER_SCORE`),低于视为未命中
- web_search 仅当 search_solutions 无结果时才被调用(system prompt 规则,工具描述中也写明)
- 瀑布阶段枚举固定:`kb_hit | live_hit | web | generic_advice | escalated`(spec 10.2 事件用)
- 凭据只从环境变量读:`AZURE_OPENAI_ENDPOINT`、`AZURE_OPENAI_API_KEY`、`AZURE_OPENAI_CHAT_DEPLOYMENT`(默认 `gpt-4o`)、`AZURE_SEARCH_ENDPOINT`、`AZURE_SEARCH_API_KEY`、`AZURE_SEARCH_INDEX`、`GITHUB_TOKEN`、`TAVILY_API_KEY`、`BRAVE_API_KEY`
- 真实 CSAM/CSA 联系人不进公开仓库:只提交 `escalation.example.yaml`,`agent/escalation.yaml` 加入 `.gitignore`
- 单元测试不打真网(respx/fake);integration 测试沿用计划 1 的 marker 约定
- LLM 回答失败时的兜底文案(spec 10.1)固定为:`抱歉,我这边暂时出了点问题,请稍后重试。如果持续失败,请联系群里的支持人员。`

---

### Task 1: agent 包骨架 + shared 消息契约(AdvisorRequest/Response)

**Files:**
- Create: `agent/pyproject.toml`, `agent/src/advisor_agent/__init__.py`, `agent/tests/__init__.py`
- Create: `shared/src/advisor_shared/messages.py`
- Modify: `pyproject.toml`(workspace members 加 `agent`,testpaths 加 `agent/tests`)
- Test: `shared/tests/test_messages.py`

**Interfaces:**
- Produces:
  - `Citation`(pydantic):`title: str, url: str`
  - `MentionDirective`(pydantic):`name: str, platform_user_id: str, role: str`
  - `AdvisorRequest`(pydantic):`text: str, conversation_key: str, channel_id: str, user_id: str, user_name: str, is_group: bool`
  - `AdvisorResponse`(pydantic):`markdown: str, citations: list[Citation] = [], mentions: list[MentionDirective] = []`
  - 可安装的 `advisor-agent` 包(依赖 advisor-shared)

- [ ] **Step 1: 写失败测试**

```python
# shared/tests/test_messages.py
from advisor_shared.messages import (
    AdvisorRequest,
    AdvisorResponse,
    Citation,
    MentionDirective,
)


def test_request_roundtrip():
    req = AdvisorRequest(
        text="Copilot 登录不上", conversation_key="19:abc;messageid=5",
        channel_id="19:abc@thread.tacv2", user_id="29:u1",
        user_name="张三", is_group=True,
    )
    assert req.is_group is True
    assert AdvisorRequest(**req.model_dump()) == req


def test_response_defaults_empty_lists():
    resp = AdvisorResponse(markdown="试试重启 VS Code。")
    assert resp.citations == [] and resp.mentions == []


def test_response_with_citation_and_mention():
    resp = AdvisorResponse(
        markdown="见链接",
        citations=[Citation(title="FAQ", url="https://github.com/x")],
        mentions=[MentionDirective(name="李四", platform_user_id="29:csam",
                                   role="CSAM")],
    )
    assert resp.citations[0].url == "https://github.com/x"
    assert resp.mentions[0].role == "CSAM"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest shared/tests/test_messages.py -v`
Expected: FAIL,`ModuleNotFoundError: advisor_shared.messages`

- [ ] **Step 3: 实现 messages.py**

```python
# shared/src/advisor_shared/messages.py
"""渠道适配层与 agent core 的唯一消息契约(spec 8.1)。"""
from pydantic import BaseModel


class Citation(BaseModel):
    title: str
    url: str


class MentionDirective(BaseModel):
    """agent 输出的结构化 @建议;mention entity 由各渠道 adapter 拼装。"""
    name: str
    platform_user_id: str
    role: str


class AdvisorRequest(BaseModel):
    text: str
    conversation_key: str
    channel_id: str
    user_id: str
    user_name: str
    is_group: bool


class AdvisorResponse(BaseModel):
    markdown: str
    citations: list[Citation] = []
    mentions: list[MentionDirective] = []
```

- [ ] **Step 4: 建 agent 包并入 workspace**

```toml
# agent/pyproject.toml
[project]
name = "advisor-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "advisor-shared",
    "agent-framework-core>=1.0.0",
    "agent-framework-azure-ai>=1.0.0",
    "openai>=1.40",
    "azure-search-documents>=11.5.1",
    "httpx>=0.27",
    "pyyaml>=6.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/advisor_agent"]
```

根 `pyproject.toml` 两处修改:

```toml
[tool.uv.workspace]
members = ["shared", "ingestion", "agent"]

[tool.pytest.ini_options]
testpaths = ["shared/tests", "ingestion/tests", "agent/tests"]
```

空 `agent/src/advisor_agent/__init__.py`、`agent/tests/__init__.py` 一并创建。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv sync && uv run pytest shared/tests/test_messages.py -v`
Expected: 3 项 PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml shared/ agent/
git commit -m "feat(shared,agent): advisor message contracts and agent package scaffold"
```

---

### Task 2: shared — 结构化事件模型(AdvisorEvent)

**Files:**
- Create: `shared/src/advisor_shared/events.py`
- Test: `shared/tests/test_events.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `Stage = Literal["kb_hit","live_hit","web","generic_advice","escalated"]`
  - `AdvisorEvent`(pydantic):`conversation_key: str, channel: str, question_summary: str, stage: Stage, tool_latencies_ms: dict[str, int] = {}, failover_count: int = 0, mentioned_human: bool = False, error: str | None = None`
  - `AdvisorEvent.to_log_line() -> str`(单行 JSON,固定 key 顺序无要求,供 App Insights 采集)

- [ ] **Step 1: 写失败测试**

```python
# shared/tests/test_events.py
import json

from advisor_shared.events import AdvisorEvent


def test_event_minimal_and_log_line_is_json():
    e = AdvisorEvent(
        conversation_key="19:a;messageid=1", channel="teams",
        question_summary="登录失败", stage="kb_hit",
    )
    line = e.to_log_line()
    parsed = json.loads(line)
    assert parsed["stage"] == "kb_hit"
    assert parsed["failover_count"] == 0
    assert "\n" not in line


def test_event_rejects_unknown_stage():
    import pytest
    with pytest.raises(ValueError):
        AdvisorEvent(conversation_key="k", channel="teams",
                     question_summary="q", stage="nope")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest shared/tests/test_events.py -v`
Expected: FAIL,`ModuleNotFoundError: advisor_shared.events`

- [ ] **Step 3: 实现 events.py**

```python
# shared/src/advisor_shared/events.py
"""每次问答一条结构化事件 — 可观测性契约(spec 10.2)。"""
from typing import Literal

from pydantic import BaseModel

Stage = Literal["kb_hit", "live_hit", "web", "generic_advice", "escalated"]


class AdvisorEvent(BaseModel):
    conversation_key: str
    channel: str
    question_summary: str
    stage: Stage
    tool_latencies_ms: dict[str, int] = {}
    failover_count: int = 0
    mentioned_human: bool = False
    error: str | None = None

    def to_log_line(self) -> str:
        return self.model_dump_json()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest shared/tests/test_events.py -v`
Expected: 2 项 PASS

- [ ] **Step 5: Commit**

```bash
git add shared/
git commit -m "feat(shared): structured AdvisorEvent for observability"
```

---

### Task 3: agent — SearchResult 模型 + KnowledgeSearchClient(AI Search hybrid)

**Files:**
- Create: `agent/src/advisor_agent/search/__init__.py`
- Create: `agent/src/advisor_agent/search/models.py`
- Create: `agent/src/advisor_agent/search/knowledge.py`
- Test: `agent/tests/test_knowledge_search.py`

**Interfaces:**
- Consumes: 计划 1 索引(字段 title/content/url/source/product_area,semantic 配置 `default`)
- Produces:
  - `SearchResult`(pydantic):`title: str, content: str, url: str, origin: Literal["kb","github-live","web"], score: float`
  - `MIN_RERANKER_SCORE = 1.5`
  - `KnowledgeSearchClient`:`__init__(self, search_client, embed_client, embed_model: str = "text-embedding-3-large")`;`async def search(self, query: str, product_area: str | None = None, top: int = 5) -> list[SearchResult]`(hybrid:BM25+向量+semantic;product_area 有值时加 filter;reranker 分数 < MIN_RERANKER_SCORE 的结果丢弃)

- [ ] **Step 1: 写失败测试**

```python
# agent/tests/test_knowledge_search.py
from types import SimpleNamespace

from advisor_agent.search.knowledge import KnowledgeSearchClient
from advisor_agent.search.models import MIN_RERANKER_SCORE, SearchResult


class FakeEmbeddings:
    def __init__(self):
        self.embeddings = SimpleNamespace(create=self._create)

    async def _create(self, model, input):
        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1] * 4)])


class FakeSearchClient:
    def __init__(self, docs):
        self._docs = docs
        self.kwargs = None

    async def search(self, search_text, **kwargs):
        self.kwargs = {"search_text": search_text, **kwargs}

        async def gen():
            for d in self._docs:
                yield d
        return gen()


def doc(title, score):
    return {"title": title, "content": f"c-{title}",
            "url": f"https://x/{title}", "@search.reranker_score": score}


async def test_search_returns_kb_results_above_threshold():
    fake = FakeSearchClient([doc("a", 2.5), doc("b", 1.2)])
    client = KnowledgeSearchClient(fake, FakeEmbeddings())
    results = await client.search("copilot login fails")
    assert [r.title for r in results] == ["a"]
    assert results[0].origin == "kb"
    assert results[0].score == 2.5
    assert fake.kwargs["query_type"] == "semantic"
    assert fake.kwargs["semantic_configuration_name"] == "default"


async def test_product_area_becomes_filter():
    fake = FakeSearchClient([])
    await KnowledgeSearchClient(fake, FakeEmbeddings()).search(
        "q", product_area="vscode")
    assert fake.kwargs["filter"] == "product_area eq 'vscode'"


async def test_no_filter_when_product_area_none():
    fake = FakeSearchClient([])
    await KnowledgeSearchClient(fake, FakeEmbeddings()).search("q")
    assert fake.kwargs["filter"] is None


def test_threshold_constant():
    assert MIN_RERANKER_SCORE == 1.5
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest agent/tests/test_knowledge_search.py -v`
Expected: FAIL,`ModuleNotFoundError: advisor_agent.search`

- [ ] **Step 3: 实现 models.py 与 knowledge.py**

```python
# agent/src/advisor_agent/search/models.py
"""检索结果统一形态:三路(kb/github-live/web)共用。"""
from typing import Literal

from pydantic import BaseModel

MIN_RERANKER_SCORE = 1.5


class SearchResult(BaseModel):
    title: str
    content: str
    url: str
    origin: Literal["kb", "github-live", "web"]
    score: float
```

```python
# agent/src/advisor_agent/search/knowledge.py
"""KB 检索:AI Search hybrid(BM25+向量+semantic ranker)(spec 7.2)。"""
from azure.search.documents.models import VectorizedQuery

from advisor_agent.search.models import MIN_RERANKER_SCORE, SearchResult


class KnowledgeSearchClient:
    def __init__(self, search_client, embed_client,
                 embed_model: str = "text-embedding-3-large"):
        self.search_client = search_client
        self.embed = embed_client
        self.embed_model = embed_model

    async def search(self, query: str, product_area: str | None = None,
                     top: int = 5) -> list[SearchResult]:
        emb = await self.embed.embeddings.create(model=self.embed_model,
                                                 input=[query])
        vector = VectorizedQuery(vector=emb.data[0].embedding,
                                 k_nearest_neighbors=top,
                                 fields="content_vector")
        pager = await self.search_client.search(
            search_text=query,
            vector_queries=[vector],
            query_type="semantic",
            semantic_configuration_name="default",
            filter=f"product_area eq '{product_area}'" if product_area else None,
            top=top,
        )
        results = []
        async for d in pager:
            score = d.get("@search.reranker_score") or 0.0
            if score < MIN_RERANKER_SCORE:
                continue
            results.append(SearchResult(
                title=d["title"], content=d["content"], url=d["url"],
                origin="kb", score=score))
        return results
```

`agent/src/advisor_agent/search/__init__.py` 为空文件。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest agent/tests/test_knowledge_search.py -v`
Expected: 4 项 PASS

- [ ] **Step 5: Commit**

```bash
git add agent/
git commit -m "feat(agent): knowledge search client with hybrid semantic query"
```

---

### Task 4: agent — GitHubLiveSearchClient(open issues/discussions)

**Files:**
- Create: `agent/src/advisor_agent/search/github_live.py`
- Test: `agent/tests/test_github_live_search.py`

**Interfaces:**
- Consumes: `SearchResult`(Task 3)
- Produces:
  - `DEFAULT_LIVE_REPOS = ["microsoft/vscode", "microsoft/vscode-copilot-release", "microsoft/copilot-intellij-feedback", "github/copilot-cli", "community/community"]`
  - `GitHubLiveSearchClient`:`__init__(self, token: str | None = None, repos: list[str] | None = None, base_url: str = "https://api.github.com")`;`async def search(self, query: str, top: int = 5) -> list[SearchResult]`(REST `/search/issues`,q 拼 `repo:` 限定 + `state:open`;结果 origin="github-live",score 用 GitHub 返回的 `score`;HTTP 失败抛异常 —— 由 Task 5 的组合工具吞掉)

- [ ] **Step 1: 写失败测试**

```python
# agent/tests/test_github_live_search.py
import httpx
import pytest
import respx

from advisor_agent.search.github_live import (
    DEFAULT_LIVE_REPOS,
    GitHubLiveSearchClient,
)

API = "https://api.github.com"


def gh_item(number=1, title="Copilot slow"):
    return {
        "number": number, "title": title,
        "body": "Details about slowness " * 5,
        "html_url": f"https://github.com/microsoft/vscode/issues/{number}",
        "score": 12.3,
    }


@respx.mock
async def test_search_builds_repo_scoped_query():
    route = respx.get(f"{API}/search/issues").mock(
        return_value=httpx.Response(200, json={"items": [gh_item()]})
    )
    client = GitHubLiveSearchClient(token="t", repos=["a/b", "c/d"])
    results = await client.search("copilot timeout")
    q = route.calls[0].request.url.params["q"]
    assert "repo:a/b" in q and "repo:c/d" in q and "state:open" in q
    assert results[0].origin == "github-live"
    assert results[0].url.endswith("/issues/1")


@respx.mock
async def test_http_error_propagates():
    respx.get(f"{API}/search/issues").mock(
        return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await GitHubLiveSearchClient(token="t").search("q")


def test_default_repos_match_spec():
    assert "microsoft/copilot-intellij-feedback" in DEFAULT_LIVE_REPOS
    assert len(DEFAULT_LIVE_REPOS) == 5
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest agent/tests/test_github_live_search.py -v`
Expected: FAIL,`ModuleNotFoundError: advisor_agent.search.github_live`

- [ ] **Step 3: 实现 github_live.py**

```python
# agent/src/advisor_agent/search/github_live.py
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest agent/tests/test_github_live_search.py -v`
Expected: 3 项 PASS

- [ ] **Step 5: Commit**

```bash
git add agent/
git commit -m "feat(agent): github live search for open issues"
```

---

### Task 5: agent — search_solutions 组合工具(并行 + 超时预算 + 合并)

**Files:**
- Create: `agent/src/advisor_agent/search/combined.py`
- Test: `agent/tests/test_search_solutions.py`

**Interfaces:**
- Consumes: `KnowledgeSearchClient`(T3)、`GitHubLiveSearchClient`(T4)、`SearchResult`(T3)、`RunContext`(T7 定义,本任务先定义最小版并在 T7 扩展 —— 见 Step 3)
- Produces:
  - `SEARCH_BUDGET_SECONDS = 8.0`
  - `CombinedSearch`:`__init__(self, kb: KnowledgeSearchClient, live: GitHubLiveSearchClient, budget_seconds: float = SEARCH_BUDGET_SECONDS)`
  - `async def search_solutions(self, query: str, product_area: str | None = None) -> dict`:返回 `{"no_results": bool, "results": [ {title, content, url, origin, score}, ... ]}`;两路 `asyncio.wait` 并行,预算内取完成者;单路异常按空结果处理;合并顺序 KB 全部在前;同 url 去重(KB 优先)

- [ ] **Step 1: 写失败测试**

```python
# agent/tests/test_search_solutions.py
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest agent/tests/test_search_solutions.py -v`
Expected: FAIL,`ModuleNotFoundError: advisor_agent.search.combined`

- [ ] **Step 3: 实现 combined.py**

```python
# agent/src/advisor_agent/search/combined.py
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest agent/tests/test_search_solutions.py -v`
Expected: 5 项 PASS

- [ ] **Step 5: Commit**

```bash
git add agent/
git commit -m "feat(agent): combined search tool with budget and kb-first merge"
```

---

### Task 6: agent — web_search provider 链(Tavily/Brave adapter + failover)

**Files:**
- Create: `agent/src/advisor_agent/search/web.py`
- Test: `agent/tests/test_web_search.py`

**Interfaces:**
- Consumes: `SearchResult`(T3)
- Produces:
  - `WebSearchProvider`(Protocol):`name: str`;`async def search(self, query: str, top: int) -> list[SearchResult]`
  - `TavilyProvider`:`__init__(self, api_key: str | None = None)`(POST https://api.tavily.com/search)
  - `BraveProvider`:`__init__(self, api_key: str | None = None)`(GET https://api.search.brave.com/res/v1/web/search)
  - `WebSearchChain`:`__init__(self, providers: list[WebSearchProvider], timeout_seconds: float = 6.0)`;`async def search(self, query: str, top: int = 5) -> tuple[list[SearchResult], int]`(返回 (results, failover_count);按序尝试,异常/超时/空结果都触发 failover 到下一个;全失败返回 `([], n)`)
  - 结果 origin 一律 "web"
  - 注:spec 提到的 Bing Grounding 绑定 Foundry Agent 运行时、无独立 REST 端点,v1 先实现 Tavily/Brave 两个独立 API provider;Bing Grounding 作为 hosted-agent 部署形态下的后续扩展,接口已兼容

- [ ] **Step 1: 写失败测试**

```python
# agent/tests/test_web_search.py
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest agent/tests/test_web_search.py -v`
Expected: FAIL,`ModuleNotFoundError: advisor_agent.search.web`

- [ ] **Step 3: 实现 web.py**

```python
# agent/src/advisor_agent/search/web.py
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest agent/tests/test_web_search.py -v`
Expected: 6 项 PASS

- [ ] **Step 5: Commit**

```bash
git add agent/
git commit -m "feat(agent): web search provider chain with failover"
```

---

### Task 7: agent — escalation 配置 + RunContext

**Files:**
- Create: `agent/src/advisor_agent/escalation.py`
- Create: `agent/src/advisor_agent/run_context.py`
- Create: `agent/escalation.example.yaml`
- Modify: `.gitignore`(加 `agent/escalation.yaml`)
- Test: `agent/tests/test_escalation.py`, `agent/tests/test_run_context.py`

**Interfaces:**
- Consumes: `MentionDirective`(T1)
- Produces:
  - `Contact`(pydantic):`role: str, name: str, email: str, teams_user_id: str | None = None, in_channel: bool = False`
  - `EscalationConfig`:`load(path: Path) -> EscalationConfig`(classmethod);`lookup(self, channel_id: str) -> tuple[list[Contact], str]`(返回 (contacts, support_ticket_url);无匹配 channel 用 defaults)
  - `RunContext`(dataclass):`stage: str = "generic_advice"`,`mentions: list[MentionDirective]`,`citations_seen: list[dict]`,`tool_latencies_ms: dict[str, int]`,`failover_count: int = 0`;模块级 `current_run: ContextVar[RunContext]` + `new_run() -> RunContext`(工具通过它上报副作用,核心管线读取 —— 不解析 LLM 自由文本)

- [ ] **Step 1: 写失败测试**

```python
# agent/tests/test_escalation.py
from pathlib import Path

from advisor_agent.escalation import EscalationConfig

YAML = """
defaults:
  support_ticket_url: https://support.github.com/
  contacts:
    - role: CSA
      name: 默认CSA
      email: csa@example.com

channels:
  - channel_id: "19:abc@thread.tacv2"
    tenant: 客户A
    contacts:
      - role: CSAM
        name: 李四
        email: lisi@example.com
        teams_user_id: "29:1a2b"
        in_channel: true
"""


def make_config(tmp_path: Path) -> EscalationConfig:
    p = tmp_path / "escalation.yaml"
    p.write_text(YAML, encoding="utf-8")
    return EscalationConfig.load(p)


def test_lookup_known_channel(tmp_path):
    contacts, url = make_config(tmp_path).lookup("19:abc@thread.tacv2")
    assert contacts[0].name == "李四"
    assert contacts[0].in_channel is True
    assert url == "https://support.github.com/"


def test_lookup_unknown_channel_falls_back_to_defaults(tmp_path):
    contacts, url = make_config(tmp_path).lookup("19:zzz@thread.tacv2")
    assert contacts[0].role == "CSA"
    assert contacts[0].teams_user_id is None
```

```python
# agent/tests/test_run_context.py
from advisor_agent.run_context import current_run, new_run


def test_new_run_resets_context():
    run = new_run()
    run.stage = "kb_hit"
    run.failover_count = 2
    fresh = new_run()
    assert fresh.stage == "generic_advice"
    assert fresh.failover_count == 0
    assert current_run.get() is fresh


def test_tools_report_via_contextvar():
    run = new_run()
    current_run.get().tool_latencies_ms["search_solutions"] = 812
    assert run.tool_latencies_ms == {"search_solutions": 812}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest agent/tests/test_escalation.py agent/tests/test_run_context.py -v`
Expected: FAIL,ModuleNotFoundError

- [ ] **Step 3: 实现 escalation.py**

```python
# agent/src/advisor_agent/escalation.py
"""静态升级配置表:channel → CSAM/CSA(spec 9.1)。"""
from pathlib import Path

import yaml
from pydantic import BaseModel


class Contact(BaseModel):
    role: str
    name: str
    email: str
    teams_user_id: str | None = None
    in_channel: bool = False


class _ChannelEntry(BaseModel):
    channel_id: str
    tenant: str = ""
    contacts: list[Contact]


class EscalationConfig(BaseModel):
    default_contacts: list[Contact]
    support_ticket_url: str
    channels: dict[str, _ChannelEntry]

    @classmethod
    def load(cls, path: Path) -> "EscalationConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        defaults = raw.get("defaults", {})
        entries = [_ChannelEntry(**c) for c in raw.get("channels", [])]
        return cls(
            default_contacts=[Contact(**c)
                              for c in defaults.get("contacts", [])],
            support_ticket_url=defaults.get(
                "support_ticket_url", "https://support.github.com/"),
            channels={e.channel_id: e for e in entries},
        )

    def lookup(self, channel_id: str) -> tuple[list[Contact], str]:
        entry = self.channels.get(channel_id)
        contacts = entry.contacts if entry else self.default_contacts
        return contacts, self.support_ticket_url
```

- [ ] **Step 4: 实现 run_context.py**

```python
# agent/src/advisor_agent/run_context.py
"""单次问答的运行上下文:工具上报副作用,核心管线读取。
用 contextvars 而不是解析 LLM 自由文本(spec 7.1、10.2)。"""
from contextvars import ContextVar
from dataclasses import dataclass, field

from advisor_shared.messages import MentionDirective


@dataclass
class RunContext:
    stage: str = "generic_advice"
    mentions: list[MentionDirective] = field(default_factory=list)
    citations_seen: list[dict] = field(default_factory=list)
    tool_latencies_ms: dict[str, int] = field(default_factory=dict)
    failover_count: int = 0


current_run: ContextVar[RunContext] = ContextVar("current_run")


def new_run() -> RunContext:
    run = RunContext()
    current_run.set(run)
    return run
```

- [ ] **Step 5: 写 escalation.example.yaml 并更新 .gitignore**

```yaml
# agent/escalation.example.yaml — 复制为 agent/escalation.yaml 并填真实值
# 真实联系人配置不进 git(公开仓库)。
defaults:
  support_ticket_url: https://support.github.com/
  contacts:
    - role: CSA
      name: 示例CSA
      email: csa@example.com

channels:
  - channel_id: "19:REPLACE_ME@thread.tacv2"
    tenant: 示例客户
    contacts:
      - role: CSAM
        name: 示例CSAM
        email: csam@example.com
        teams_user_id: "29:REPLACE_ME"
        in_channel: true
```

`.gitignore` 追加一行:`agent/escalation.yaml`

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest agent/tests/test_escalation.py agent/tests/test_run_context.py -v`
Expected: 4 项 PASS

- [ ] **Step 7: Commit**

```bash
git add agent/ .gitignore
git commit -m "feat(agent): escalation config lookup and per-run context"
```

---

### Task 8: agent — 工具函数层(MAF 可注册的三个工具)

**Files:**
- Create: `agent/src/advisor_agent/tools.py`
- Test: `agent/tests/test_tools.py`

**Interfaces:**
- Consumes: `CombinedSearch`(T5)、`WebSearchChain`(T6)、`EscalationConfig`(T7)、`current_run`(T7)
- Produces:
  - `AdvisorTools`:`__init__(self, combined: CombinedSearch, web: WebSearchChain, escalation: EscalationConfig)`
  - `async def search_solutions(self, query: str, product_area: str | None = None) -> str`(JSON 字符串给 LLM;副作用:latency 计时入 current_run;有 kb 结果 → stage="kb_hit",仅 live → "live_hit";结果 url/title 记入 citations_seen)
  - `async def web_search(self, query: str) -> str`(JSON;stage="web";failover_count 累加)
  - `async def escalate_to_human(self, channel_id: str, reason: str) -> str`(JSON;stage="escalated";in_channel 且有 teams_user_id 的联系人 → 追加 MentionDirective 到 current_run.mentions)
  - 每个方法带完整 docstring(MAF 用 docstring 生成工具描述;web_search 的 docstring 明确"仅当 search_solutions 无结果时使用")

- [ ] **Step 1: 写失败测试**

```python
# agent/tests/test_tools.py
import json
from pathlib import Path

from advisor_agent.escalation import EscalationConfig
from advisor_agent.run_context import new_run
from advisor_agent.search.models import SearchResult
from advisor_agent.tools import AdvisorTools

ESCALATION_YAML = """
defaults:
  support_ticket_url: https://support.github.com/
  contacts:
    - role: CSA
      name: 默认CSA
      email: csa@example.com
channels:
  - channel_id: "19:abc"
    contacts:
      - role: CSAM
        name: 李四
        email: l@x.com
        teams_user_id: "29:1a2b"
        in_channel: true
      - role: CSA
        name: 王五
        email: w@x.com
        in_channel: false
"""


def r(title, origin):
    return SearchResult(title=title, content="c", url=f"https://x/{title}",
                        origin=origin, score=2.0)


class StubCombined:
    def __init__(self, payload):
        self.payload = payload

    async def search_solutions(self, query, product_area=None):
        return self.payload


class StubWeb:
    def __init__(self, results, failovers):
        self._out = (results, failovers)

    async def search(self, query, top=5):
        return self._out


def make_tools(tmp_path, combined_payload=None, web=(list(), 0)):
    p = tmp_path / "e.yaml"
    p.write_text(ESCALATION_YAML, encoding="utf-8")
    payload = combined_payload or {"no_results": True, "results": []}
    return AdvisorTools(StubCombined(payload), StubWeb(web[0], web[1]),
                        EscalationConfig.load(p))


async def test_search_solutions_sets_kb_hit_stage_and_citations(tmp_path):
    run = new_run()
    payload = {"no_results": False,
               "results": [r("a", "kb").model_dump(),
                           r("b", "github-live").model_dump()]}
    tools = make_tools(tmp_path, combined_payload=payload)
    out = json.loads(await tools.search_solutions("q"))
    assert out["no_results"] is False
    assert run.stage == "kb_hit"
    assert "search_solutions" in run.tool_latencies_ms
    assert {c["title"] for c in run.citations_seen} == {"a", "b"}


async def test_search_solutions_live_only_sets_live_hit(tmp_path):
    run = new_run()
    payload = {"no_results": False,
               "results": [r("b", "github-live").model_dump()]}
    tools = make_tools(tmp_path, combined_payload=payload)
    await tools.search_solutions("q")
    assert run.stage == "live_hit"


async def test_web_search_sets_stage_and_failover(tmp_path):
    run = new_run()
    tools = make_tools(tmp_path, web=([r("w", "web")], 2))
    out = json.loads(await tools.web_search("q"))
    assert out["results"][0]["origin"] == "web"
    assert run.stage == "web" and run.failover_count == 2


async def test_escalate_adds_mention_only_for_in_channel(tmp_path):
    run = new_run()
    tools = make_tools(tmp_path)
    out = json.loads(await tools.escalate_to_human("19:abc", "用户仍未解决"))
    assert run.stage == "escalated"
    assert len(run.mentions) == 1
    assert run.mentions[0].platform_user_id == "29:1a2b"
    roles = {c["role"] for c in out["contacts"]}
    assert roles == {"CSAM", "CSA"}


async def test_escalate_unknown_channel_uses_defaults(tmp_path):
    run = new_run()
    tools = make_tools(tmp_path)
    out = json.loads(await tools.escalate_to_human("19:zzz", "reason"))
    assert run.mentions == []
    assert out["contacts"][0]["name"] == "默认CSA"


def test_web_search_docstring_states_precondition(tmp_path):
    tools = make_tools(tmp_path)
    assert "search_solutions" in tools.web_search.__doc__
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest agent/tests/test_tools.py -v`
Expected: FAIL,`ModuleNotFoundError: advisor_agent.tools`

- [ ] **Step 3: 实现 tools.py**

```python
# agent/src/advisor_agent/tools.py
"""MAF 注册的三个工具。docstring 即工具描述,LLM 依此决定调用时机(spec 7.2/7.3)。"""
import json
import time

from advisor_agent.escalation import EscalationConfig
from advisor_agent.run_context import current_run
from advisor_shared.messages import MentionDirective


class AdvisorTools:
    def __init__(self, combined, web, escalation: EscalationConfig):
        self._combined = combined
        self._web = web
        self._escalation = escalation

    async def search_solutions(self, query: str,
                               product_area: str | None = None) -> str:
        """搜索已解决的知识库问答和 GitHub 上正在讨论的相关 issue。
        回答任何 GitHub Copilot 问题前必须先调用此工具。
        product_area 可选值:vscode / intellij / cli / web / general。"""
        run = current_run.get()
        start = time.monotonic()
        out = await self._combined.search_solutions(
            query, product_area=product_area)
        run.tool_latencies_ms["search_solutions"] = int(
            (time.monotonic() - start) * 1000)
        origins = {item["origin"] for item in out["results"]}
        if "kb" in origins:
            run.stage = "kb_hit"
        elif "github-live" in origins:
            run.stage = "live_hit"
        run.citations_seen.extend(
            {"title": item["title"], "url": item["url"]}
            for item in out["results"])
        return json.dumps(out, ensure_ascii=False)

    async def web_search(self, query: str) -> str:
        """在 web 上搜索最新信息(版本发布、技术博客、官方文档)。
        仅当 search_solutions 返回 no_results 时才使用此工具。"""
        run = current_run.get()
        start = time.monotonic()
        results, failovers = await self._web.search(query)
        run.tool_latencies_ms["web_search"] = int(
            (time.monotonic() - start) * 1000)
        run.stage = "web"
        run.failover_count += failovers
        run.citations_seen.extend(
            {"title": r.title, "url": r.url} for r in results)
        return json.dumps(
            {"results": [r.model_dump() for r in results]},
            ensure_ascii=False)

    async def escalate_to_human(self, channel_id: str, reason: str) -> str:
        """升级到人工支持(CSAM/CSA)。仅当:用户明确表示问题仍未解决或不满意;
        或问题涉及账务、合同、配额调整、组织级配置时使用。
        reason 用一句话说明已尝试的路径,便于接手人了解上下文。"""
        run = current_run.get()
        contacts, ticket_url = self._escalation.lookup(channel_id)
        run.stage = "escalated"
        for c in contacts:
            if c.in_channel and c.teams_user_id:
                run.mentions.append(MentionDirective(
                    name=c.name, platform_user_id=c.teams_user_id,
                    role=c.role))
        return json.dumps({
            "contacts": [c.model_dump() for c in contacts],
            "support_ticket_url": ticket_url,
            "reason_recorded": reason,
        }, ensure_ascii=False)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest agent/tests/test_tools.py -v`
Expected: 6 项 PASS

- [ ] **Step 5: Commit**

```bash
git add agent/
git commit -m "feat(agent): three MAF-registrable tools reporting via run context"
```

---

### Task 9: agent — AgentBackend 协议 + 会话存储接口

**Files:**
- Create: `agent/src/advisor_agent/backend.py`
- Create: `agent/src/advisor_agent/sessions.py`
- Test: `agent/tests/test_sessions.py`

**Interfaces:**
- Consumes: 无(独立单元)
- Produces:
  - `AgentBackend`(Protocol):`async def run(self, user_text: str, history: list[dict]) -> str`(history 元素 `{"role": "user"|"assistant", "content": str}`;返回 LLM 最终回答文本;工具循环在 backend 内部完成)
  - `SessionStore`(Protocol):`async def get(self, key: str) -> list[dict]`;`async def append(self, key: str, role: str, content: str) -> None`
  - `InMemorySessionStore(SessionStore)`:`__init__(self, max_turns: int = 20, ttl_seconds: float = 3600)`(超上限丢最旧;过期整段清空;时钟可注入便于测试)

- [ ] **Step 1: 写失败测试**

```python
# agent/tests/test_sessions.py
from advisor_agent.sessions import InMemorySessionStore


async def test_empty_history_for_new_key():
    store = InMemorySessionStore()
    assert await store.get("k1") == []


async def test_append_and_get_roundtrip():
    store = InMemorySessionStore()
    await store.append("k1", "user", "登录失败")
    await store.append("k1", "assistant", "试试重启")
    history = await store.get("k1")
    assert history == [
        {"role": "user", "content": "登录失败"},
        {"role": "assistant", "content": "试试重启"},
    ]
    assert await store.get("k2") == []  # 隔离


async def test_max_turns_drops_oldest():
    store = InMemorySessionStore(max_turns=2)
    await store.append("k", "user", "1")
    await store.append("k", "assistant", "2")
    await store.append("k", "user", "3")
    assert [m["content"] for m in await store.get("k")] == ["2", "3"]


async def test_ttl_expires_whole_session():
    clock = [1000.0]
    store = InMemorySessionStore(ttl_seconds=60, clock=lambda: clock[0])
    await store.append("k", "user", "1")
    clock[0] += 61
    assert await store.get("k") == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest agent/tests/test_sessions.py -v`
Expected: FAIL,`ModuleNotFoundError: advisor_agent.sessions`

- [ ] **Step 3: 实现 backend.py 与 sessions.py**

```python
# agent/src/advisor_agent/backend.py
"""LLM 编排后端协议:MAF 是默认实现(Task 10),预留 Copilot SDK 等(spec 7.5)。"""
from typing import Protocol


class AgentBackend(Protocol):
    async def run(self, user_text: str, history: list[dict]) -> str:
        """跑一轮完整的 tool loop,返回最终回答文本。
        history: [{"role": "user"|"assistant", "content": str}, ...]"""
        ...
```

```python
# agent/src/advisor_agent/sessions.py
"""会话存储:v1 内存实现,接口留给 Cosmos DB/Redis(spec 7.4)。"""
import time
from typing import Callable, Protocol


class SessionStore(Protocol):
    async def get(self, key: str) -> list[dict]: ...

    async def append(self, key: str, role: str, content: str) -> None: ...


class InMemorySessionStore:
    def __init__(self, max_turns: int = 20, ttl_seconds: float = 3600,
                 clock: Callable[[], float] = time.monotonic):
        self._data: dict[str, tuple[float, list[dict]]] = {}
        self.max_turns = max_turns
        self.ttl = ttl_seconds
        self.clock = clock

    async def get(self, key: str) -> list[dict]:
        entry = self._data.get(key)
        if not entry:
            return []
        touched, messages = entry
        if self.clock() - touched > self.ttl:
            del self._data[key]
            return []
        return list(messages)

    async def append(self, key: str, role: str, content: str) -> None:
        messages = await self.get(key)
        messages.append({"role": role, "content": content})
        self._data[key] = (self.clock(), messages[-self.max_turns:])
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest agent/tests/test_sessions.py -v`
Expected: 4 项 PASS

- [ ] **Step 5: Commit**

```bash
git add agent/
git commit -m "feat(agent): backend protocol and in-memory session store"
```

---

### Task 10: agent — AdvisorCore 管线(扩展点 + 事件 + 兜底)

**Files:**
- Create: `agent/src/advisor_agent/core.py`
- Create: `agent/src/advisor_agent/extensions.py`
- Test: `agent/tests/test_core.py`

**Interfaces:**
- Consumes: `AdvisorRequest/AdvisorResponse/Citation`(T1)、`AdvisorEvent`(T2)、`new_run/current_run`(T7)、`AgentBackend/SessionStore`(T9)
- Produces:
  - `QueryPlanner`(Protocol):`async def plan(self, request: AdvisorRequest) -> AdvisorRequest`;`NoopPlanner`
  - `AnswerEvaluator`(Protocol):`async def evaluate(self, request: AdvisorRequest, response: AdvisorResponse) -> AdvisorResponse`;`NoopEvaluator`
  - `FALLBACK_MESSAGE = "抱歉,我这边暂时出了点问题,请稍后重试。如果持续失败,请联系群里的支持人员。"`
  - `AdvisorCore`:`__init__(self, backend: AgentBackend, sessions: SessionStore, planner=NoopPlanner(), evaluator=NoopEvaluator(), event_sink: Callable[[AdvisorEvent], None] = _log_event, channel_name: str = "unknown")`
  - `async def handle(self, request: AdvisorRequest) -> AdvisorResponse`:完整管线 planner → new_run() → history 读取 → backend.run(2 次重试)→ citations 从 run.citations_seen 收集(去重、最多 5 条)→ mentions 从 run.mentions → evaluator → 会话写回 → 发 AdvisorEvent。backend 全部失败时:markdown=FALLBACK_MESSAGE、event.error 记录、会话不写入

- [ ] **Step 1: 写失败测试**

```python
# agent/tests/test_core.py
from advisor_agent.core import FALLBACK_MESSAGE, AdvisorCore
from advisor_agent.run_context import current_run
from advisor_agent.sessions import InMemorySessionStore
from advisor_shared.events import AdvisorEvent
from advisor_shared.messages import AdvisorRequest, MentionDirective


def make_request(text="Copilot 登录失败") -> AdvisorRequest:
    return AdvisorRequest(text=text, conversation_key="ck1",
                          channel_id="19:abc", user_id="u", user_name="n",
                          is_group=True)


class StubBackend:
    """记录调用;可注入副作用(模拟工具执行)与失败次数。"""
    def __init__(self, reply="答案", fail_times=0, side_effect=None):
        self.reply, self.fail_times = reply, fail_times
        self.side_effect = side_effect
        self.calls: list[tuple[str, list[dict]]] = []

    async def run(self, user_text, history):
        self.calls.append((user_text, list(history)))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("llm down")
        if self.side_effect:
            self.side_effect()
        return self.reply


def collect_events(bucket):
    return bucket.append


async def test_happy_path_returns_response_and_persists_session():
    events: list[AdvisorEvent] = []
    sessions = InMemorySessionStore()
    core = AdvisorCore(StubBackend("试试重启"), sessions,
                       event_sink=collect_events(events),
                       channel_name="teams")
    resp = await core.handle(make_request())
    assert resp.markdown == "试试重启"
    history = await sessions.get("ck1")
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert events[0].channel == "teams"
    assert events[0].stage == "generic_advice"  # 无工具调用时默认


async def test_history_passed_to_backend():
    sessions = InMemorySessionStore()
    await sessions.append("ck1", "user", "之前的问题")
    backend = StubBackend()
    core = AdvisorCore(backend, sessions, event_sink=lambda e: None)
    await core.handle(make_request("追问"))
    _, history = backend.calls[0]
    assert history[0]["content"] == "之前的问题"


async def test_citations_and_mentions_from_run_context():
    def side_effect():
        run = current_run.get()
        run.stage = "kb_hit"
        run.citations_seen.extend([
            {"title": "a", "url": "https://x/a"},
            {"title": "a-dup", "url": "https://x/a"},   # 同 url 去重
            {"title": "b", "url": "https://x/b"},
        ])
        run.mentions.append(MentionDirective(
            name="李四", platform_user_id="29:1", role="CSAM"))

    events: list[AdvisorEvent] = []
    core = AdvisorCore(StubBackend(side_effect=side_effect),
                       InMemorySessionStore(),
                       event_sink=collect_events(events))
    resp = await core.handle(make_request())
    assert [c.url for c in resp.citations] == ["https://x/a", "https://x/b"]
    assert resp.mentions[0].name == "李四"
    assert events[0].stage == "kb_hit"
    assert events[0].mentioned_human is True


async def test_backend_retry_then_success():
    backend = StubBackend("ok", fail_times=1)
    core = AdvisorCore(backend, InMemorySessionStore(),
                       event_sink=lambda e: None)
    resp = await core.handle(make_request())
    assert resp.markdown == "ok" and len(backend.calls) == 2


async def test_backend_exhausted_returns_fallback_and_skips_session():
    events: list[AdvisorEvent] = []
    sessions = InMemorySessionStore()
    core = AdvisorCore(StubBackend(fail_times=99), sessions,
                       event_sink=collect_events(events))
    resp = await core.handle(make_request())
    assert resp.markdown == FALLBACK_MESSAGE
    assert await sessions.get("ck1") == []
    assert "llm down" in events[0].error
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest agent/tests/test_core.py -v`
Expected: FAIL,`ModuleNotFoundError: advisor_agent.core`

- [ ] **Step 3: 实现 extensions.py**

```python
# agent/src/advisor_agent/extensions.py
"""多 agent 扩展点:v1 no-op,未来可替换为 MAF 子 agent(spec 7.1)。"""
from typing import Protocol

from advisor_shared.messages import AdvisorRequest, AdvisorResponse


class QueryPlanner(Protocol):
    async def plan(self, request: AdvisorRequest) -> AdvisorRequest: ...


class AnswerEvaluator(Protocol):
    async def evaluate(self, request: AdvisorRequest,
                       response: AdvisorResponse) -> AdvisorResponse: ...


class NoopPlanner:
    async def plan(self, request: AdvisorRequest) -> AdvisorRequest:
        return request


class NoopEvaluator:
    async def evaluate(self, request: AdvisorRequest,
                       response: AdvisorResponse) -> AdvisorResponse:
        return response
```

- [ ] **Step 4: 实现 core.py**

```python
# agent/src/advisor_agent/core.py
"""AdvisorCore:planner → tool loop → evaluator 管线,事件与兜底(spec 7.1/10.1/10.2)。"""
import logging
from typing import Callable

from advisor_agent.backend import AgentBackend
from advisor_agent.extensions import (
    AnswerEvaluator,
    NoopEvaluator,
    NoopPlanner,
    QueryPlanner,
)
from advisor_agent.run_context import new_run
from advisor_agent.sessions import SessionStore
from advisor_shared.events import AdvisorEvent
from advisor_shared.messages import AdvisorRequest, AdvisorResponse, Citation

logger = logging.getLogger(__name__)

FALLBACK_MESSAGE = ("抱歉,我这边暂时出了点问题,请稍后重试。"
                    "如果持续失败,请联系群里的支持人员。")

_MAX_ATTEMPTS = 2
_MAX_CITATIONS = 5
_SUMMARY_CHARS = 80


def _log_event(event: AdvisorEvent) -> None:
    logger.info("advisor_event %s", event.to_log_line())


class AdvisorCore:
    def __init__(self, backend: AgentBackend, sessions: SessionStore,
                 planner: QueryPlanner | None = None,
                 evaluator: AnswerEvaluator | None = None,
                 event_sink: Callable[[AdvisorEvent], None] = _log_event,
                 channel_name: str = "unknown"):
        self.backend = backend
        self.sessions = sessions
        self.planner = planner or NoopPlanner()
        self.evaluator = evaluator or NoopEvaluator()
        self.event_sink = event_sink
        self.channel_name = channel_name

    async def handle(self, request: AdvisorRequest) -> AdvisorResponse:
        request = await self.planner.plan(request)
        run = new_run()
        history = await self.sessions.get(request.conversation_key)

        answer, error = None, None
        for _ in range(_MAX_ATTEMPTS):
            try:
                answer = await self.backend.run(request.text, history)
                break
            except Exception as e:
                logger.exception("backend attempt failed")
                error = str(e)

        if answer is None:
            response = AdvisorResponse(markdown=FALLBACK_MESSAGE)
        else:
            seen, citations = set(), []
            for c in run.citations_seen:
                if c["url"] in seen:
                    continue
                seen.add(c["url"])
                citations.append(Citation(title=c["title"], url=c["url"]))
            response = AdvisorResponse(
                markdown=answer,
                citations=citations[:_MAX_CITATIONS],
                mentions=list(run.mentions),
            )
            response = await self.evaluator.evaluate(request, response)
            await self.sessions.append(
                request.conversation_key, "user", request.text)
            await self.sessions.append(
                request.conversation_key, "assistant", response.markdown)

        self.event_sink(AdvisorEvent(
            conversation_key=request.conversation_key,
            channel=self.channel_name,
            question_summary=request.text[:_SUMMARY_CHARS],
            stage=run.stage,
            tool_latencies_ms=run.tool_latencies_ms,
            failover_count=run.failover_count,
            mentioned_human=bool(run.mentions),
            error=error if answer is None else None,
        ))
        return response
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest agent/tests/test_core.py -v`
Expected: 5 项 PASS

- [ ] **Step 6: Commit**

```bash
git add agent/
git commit -m "feat(agent): AdvisorCore pipeline with extension points, events, fallback"
```

---

### Task 11: agent — system prompt + MAFBackend + 生产装配

**Files:**
- Create: `agent/src/advisor_agent/prompts.py`
- Create: `agent/src/advisor_agent/maf_backend.py`
- Create: `agent/src/advisor_agent/factory.py`
- Test: `agent/tests/test_prompts.py`

**Interfaces:**
- Consumes: `AdvisorTools`(T8)、`AgentBackend`(T9)、`AdvisorCore`(T10)、全部搜索客户端(T3-6)、`EscalationConfig`(T7)
- Produces:
  - `SYSTEM_PROMPT: str`(spec 7.3 五条规则的完整落地)
  - `MAFBackend(AgentBackend)`:`__init__(self, tools: AdvisorTools, channel_id_provider: Callable[[], str])`(内部构建 MAF ChatAgent:AzureOpenAIChatClient + 三个工具注册;`escalate_to_human` 的 channel_id 由 backend 包装注入,LLM 只提供 reason 参数)
  - `build_advisor(channel_name: str = "generic") -> AdvisorCore`(factory:从环境变量装配全链路 —— 这是渠道 adapter 唯一需要调用的函数)
  - 注:MAFBackend 的正确性由计划 3 的 integration 测试覆盖(需真实 Azure OpenAI);本任务单元测试只覆盖 prompt 内容与 factory 可构造性

- [ ] **Step 1: 写失败测试**

```python
# agent/tests/test_prompts.py
from advisor_agent.prompts import SYSTEM_PROMPT


def test_prompt_contains_waterfall_rules():
    # spec 7.3 的五条核心规则都要落在 prompt 里
    assert "search_solutions" in SYSTEM_PROMPT      # 规则1:永远先组合检索
    assert "no_results" in SYSTEM_PROMPT            # 规则2:no_results 才 web_search
    assert "web_search" in SYSTEM_PROMPT
    assert "escalate_to_human" in SYSTEM_PROMPT     # 规则4:升级条件
    assert "支持工单" in SYSTEM_PROMPT or "工单" in SYSTEM_PROMPT  # 规则3
    assert "语言" in SYSTEM_PROMPT                   # 规则5:语言跟随
    assert "编造" in SYSTEM_PROMPT                   # 规则5:不编造


def test_prompt_mentions_source_priority():
    assert "kb" in SYSTEM_PROMPT and "github-live" in SYSTEM_PROMPT
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest agent/tests/test_prompts.py -v`
Expected: FAIL,`ModuleNotFoundError: advisor_agent.prompts`

- [ ] **Step 3: 实现 prompts.py**

```python
# agent/src/advisor_agent/prompts.py
"""System prompt:升级瀑布策略规则(spec 7.3)。"""

SYSTEM_PROMPT = """\
你是 GitHub Copilot Advisor,帮助企业用户解决 GitHub Copilot 使用问题,
覆盖 VS Code、IntelliJ/JetBrains、CLI、GitHub 网页端等所有入口。

## 工具使用规则(严格遵守顺序)

1. 回答任何 Copilot 问题前,必须先调用 search_solutions。
   结果按来源区分:origin="kb" 是已解决的知识库问答,优先引用其内容作答;
   origin="github-live" 是还在讨论中的 open issue,只作为"该问题正在被讨论/
   跟进中"的补充信息,并给出链接。
2. 仅当 search_solutions 返回 no_results=true,才调用 web_search 查找
   最新信息(版本发布、官方博客、文档更新)。
3. 若两级检索都没有可靠答案,给出通用排查建议:检查网络与代理、重启
   IDE、重试、升级插件到最新版本;并附上开支持工单的指引(告知用户带上
   Copilot 日志与版本信息,入口见工具返回的 support_ticket_url,若无则为
   https://support.github.com/)。
4. 出现以下情形时调用 escalate_to_human:用户明确表示问题仍未解决或不满意
   (例如"还是不行""没用""找人吧");或此前已给过通用建议后用户再次求助;
   或问题涉及账务、合同、配额调整、组织级配置。reason 参数用一句话概括
   已尝试的路径。若返回的联系人 in_channel=true,告知用户会为其 @ 对应
   负责人;否则给出姓名与邮箱。
5. 语言与事实纪律:用与用户提问相同的语言回答;引用来源永远附原始链接;
   检索结果不足以支撑的内容不要编造,明确说"我不确定";不输出任何密钥或
   敏感信息。

## 回答风格

- 直接给可执行的步骤,不重复用户的问题。
- 群聊中保持简洁:先给结论/方案,细节收进编号步骤。
- 引用知识库答案时用自己的话综合,不逐字粘贴长文。
"""
```

- [ ] **Step 4: 实现 maf_backend.py**

```python
# agent/src/advisor_agent/maf_backend.py
"""MAF 编排后端:AzureOpenAIChatClient + 工具注册(spec 7.1/7.5)。
注意:agent-framework 的 API 以 1.0 GA 文档为准;若方法名有出入,
以 https://learn.microsoft.com/agent-framework 为准做等价替换。"""
from typing import Callable

from agent_framework import ChatAgent
from agent_framework.azure import AzureOpenAIChatClient

from advisor_agent.prompts import SYSTEM_PROMPT
from advisor_agent.tools import AdvisorTools


class MAFBackend:
    def __init__(self, tools: AdvisorTools,
                 channel_id_provider: Callable[[], str]):
        self._tools = tools
        self._channel_id = channel_id_provider

        async def search_solutions(query: str,
                                   product_area: str | None = None) -> str:
            """搜索已解决的知识库问答和 GitHub 上正在讨论的相关 issue。
            回答任何 GitHub Copilot 问题前必须先调用此工具。
            product_area 可选:vscode / intellij / cli / web / general。"""
            return await tools.search_solutions(query, product_area)

        async def web_search(query: str) -> str:
            """在 web 上搜索最新信息。仅当 search_solutions 返回
            no_results=true 时才使用。"""
            return await tools.web_search(query)

        async def escalate_to_human(reason: str) -> str:
            """升级到人工支持(CSAM/CSA)。仅当用户明确表示未解决/不满意,
            或问题涉及账务、合同、配额、组织级配置时使用。
            reason:一句话概括已尝试的路径。"""
            return await tools.escalate_to_human(self._channel_id(), reason)

        self._agent = ChatAgent(
            chat_client=AzureOpenAIChatClient(),  # 端点/key/deployment 走环境变量
            instructions=SYSTEM_PROMPT,
            tools=[search_solutions, web_search, escalate_to_human],
        )

    async def run(self, user_text: str, history: list[dict]) -> str:
        transcript = "".join(
            f"[{m['role']}] {m['content']}\n" for m in history)
        prompt = (f"此前对话:\n{transcript}\n用户新消息:{user_text}"
                  if transcript else user_text)
        result = await self._agent.run(prompt)
        return result.text
```

- [ ] **Step 5: 实现 factory.py**

```python
# agent/src/advisor_agent/factory.py
"""生产装配:渠道 adapter 只调用 build_advisor(),不接触内部组件。"""
import os
from pathlib import Path

from azure.core.credentials import AzureKeyCredential
from azure.search.documents.aio import SearchClient
from openai import AsyncAzureOpenAI

from advisor_agent.core import AdvisorCore
from advisor_agent.escalation import EscalationConfig
from advisor_agent.maf_backend import MAFBackend
from advisor_agent.run_context import current_run
from advisor_agent.search.combined import CombinedSearch
from advisor_agent.search.github_live import GitHubLiveSearchClient
from advisor_agent.search.knowledge import KnowledgeSearchClient
from advisor_agent.search.web import BraveProvider, TavilyProvider, WebSearchChain
from advisor_agent.sessions import InMemorySessionStore
from advisor_agent.tools import AdvisorTools

_channel_id_holder: dict[str, str] = {"value": ""}


def _channel_id_provider() -> str:
    return _channel_id_holder["value"]


def set_current_channel_id(channel_id: str) -> None:
    """渠道 adapter 在每次 handle 前调用(单 worker 内串行时安全;
    多并发部署改为 contextvars,接口不变)。"""
    _channel_id_holder["value"] = channel_id


def build_advisor(channel_name: str = "generic") -> AdvisorCore:
    embed_client = AsyncAzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version="2024-10-21",
    )
    search_client = SearchClient(
        os.environ["AZURE_SEARCH_ENDPOINT"],
        os.environ.get("AZURE_SEARCH_INDEX", "copilot-qa"),
        AzureKeyCredential(os.environ["AZURE_SEARCH_API_KEY"]),
    )
    combined = CombinedSearch(
        KnowledgeSearchClient(search_client, embed_client),
        GitHubLiveSearchClient(),
    )
    providers = []
    if os.environ.get("TAVILY_API_KEY"):
        providers.append(TavilyProvider())
    if os.environ.get("BRAVE_API_KEY"):
        providers.append(BraveProvider())
    web = WebSearchChain(providers)
    escalation = EscalationConfig.load(
        Path(os.environ.get("ESCALATION_CONFIG",
                            Path(__file__).parent.parent.parent
                            / "escalation.yaml")))
    tools = AdvisorTools(combined, web, escalation)
    backend = MAFBackend(tools, _channel_id_provider)
    return AdvisorCore(backend, InMemorySessionStore(),
                       channel_name=channel_name)
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest agent/tests/test_prompts.py -v && uv run python -c "import advisor_agent.factory, advisor_agent.maf_backend; print('imports ok')"`
Expected: 2 项 PASS;`imports ok`(若 agent_framework 导入路径与 GA 文档不符,按文档修正 maf_backend 的 import)

- [ ] **Step 7: Commit**

```bash
git add agent/
git commit -m "feat(agent): system prompt, MAF backend, production factory"
```

---

### Task 12: agent — 行为评估集(eval set)+ integration 冒烟

**Files:**
- Create: `agent/tests/eval_cases.yaml`
- Create: `agent/tests/test_eval_behavior.py`

**Interfaces:**
- Consumes: `build_advisor`(T11)、`AdvisorRequest`(T1)
- Produces: 可重复运行的行为回归集(integration marker;prompt 每次改动必跑)

- [ ] **Step 1: 写评估用例(每个 P0/P1 主题域 ≥2,来自真实群问题分类)**

```yaml
# agent/tests/eval_cases.yaml
# 行为断言:expected_stage = 事件里的瀑布终点;expect_mention = 是否应 @人
# 语言断言:reply_language = zh/en(检查回复主要语言)
cases:
  # P0 主题1:Credits/Token/计费/上下文治理
  - id: billing-premium-requests
    text: "Copilot 的 premium requests 是怎么计费的?我们团队额度用超了"
    expected_stage_in: [kb_hit, live_hit, web]
    reply_language: zh
  - id: billing-escalate-quota
    text: "我们想调整组织的 Copilot 配额和合同,找谁?"
    expected_stage_in: [escalated]
    expect_mention: true
    reply_language: zh
  # P0 主题2:稳定性/超时/登录/网络
  - id: stability-login-loop
    text: "VS Code 里 Copilot 一直让我重新登录,登录完又掉,怎么办?"
    expected_stage_in: [kb_hit, live_hit, web, generic_advice]
    reply_language: zh
  - id: stability-followup-escalate
    multi_turn:
      - "Copilot 在公司网络下一直 timeout"
      - "都试过了还是不行,帮我找个人吧"
    expected_stage_in: [escalated]
    expect_mention: true
    reply_language: zh
  # P0 主题3:Agent/Subagent/模型路由
  - id: agent-model-routing
    text: "Copilot 的 agent mode 怎么选模型?能固定用某个模型吗?"
    expected_stage_in: [kb_hit, live_hit, web]
    reply_language: zh
  - id: agent-english-question
    text: "How do subagents work in Copilot agent mode?"
    expected_stage_in: [kb_hit, live_hit, web]
    reply_language: en
  # P1:IDE/插件/Remote 兼容性
  - id: ide-intellij-version
    text: "IntelliJ 里 Copilot 插件和 IDE 版本不兼容,提示要升级"
    expected_stage_in: [kb_hit, live_hit, web, generic_advice]
    reply_language: zh
  # P1:MCP 集成与密钥管理
  - id: mcp-config
    text: "Copilot 里怎么配置 MCP server?密钥放哪里安全?"
    expected_stage_in: [kb_hit, live_hit, web]
    reply_language: zh
```

- [ ] **Step 2: 写评估测试**

```python
# agent/tests/test_eval_behavior.py
"""行为回归评估:需真实 Azure OpenAI + AI Search(灌过数据)。
prompt/工具描述每次改动必跑:uv run pytest -m integration agent/tests/test_eval_behavior.py"""
import os
import re
from pathlib import Path

import pytest
import yaml

from advisor_shared.events import AdvisorEvent
from advisor_shared.messages import AdvisorRequest

pytestmark = pytest.mark.integration

REQUIRED_ENV = ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY",
                "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_API_KEY"]

CASES = yaml.safe_load(
    (Path(__file__).parent / "eval_cases.yaml").read_text(encoding="utf-8")
)["cases"]


@pytest.fixture(autouse=True)
def require_env():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        pytest.skip(f"missing env: {missing}")


def is_mostly_chinese(text: str) -> bool:
    han = len(re.findall(r"[一-鿿]", text))
    latin = len(re.findall(r"[a-zA-Z]", text))
    return han > latin * 0.5


def make_request(text: str) -> AdvisorRequest:
    return AdvisorRequest(text=text, conversation_key=f"eval-{hash(text)}",
                          channel_id="19:eval", user_id="u",
                          user_name="eval", is_group=True)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
async def test_eval_case(case):
    from advisor_agent.factory import build_advisor, set_current_channel_id
    events: list[AdvisorEvent] = []
    core = build_advisor(channel_name="eval")
    core.event_sink = events.append
    set_current_channel_id("19:eval")

    turns = case.get("multi_turn") or [case["text"]]
    key = f"eval-{case['id']}"
    for text in turns:
        req = make_request(text)
        req = req.model_copy(update={"conversation_key": key})
        resp = await core.handle(req)

    assert events[-1].stage in case["expected_stage_in"], \
        f"stage={events[-1].stage}, want {case['expected_stage_in']}"
    if case.get("expect_mention"):
        assert resp.mentions or "@" in resp.markdown or events[-1].mentioned_human
    if case.get("reply_language") == "zh":
        assert is_mostly_chinese(resp.markdown), resp.markdown[:200]
    elif case.get("reply_language") == "en":
        assert not is_mostly_chinese(resp.markdown), resp.markdown[:200]
```

- [ ] **Step 3: 验证默认测试套不受影响**

Run: `uv run pytest`
Expected: 全部单元测试 PASS,eval/integration 显示 deselected

Run(有真实资源且已灌数据时): `uv run pytest -m integration agent/tests/test_eval_behavior.py -v`
Expected: 各 case PASS 或明确的行为偏差报告(偏差即 prompt 需要调)

- [ ] **Step 4: Commit**

```bash
git add agent/tests/
git commit -m "test(agent): behavior eval set from real customer question themes"
```
