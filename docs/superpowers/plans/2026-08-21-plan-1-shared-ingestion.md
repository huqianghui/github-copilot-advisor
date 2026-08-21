# 计划 1:shared 契约包 + Ingestion 管道 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 monorepo 骨架、shared 契约包(索引 schema + 文档模型),以及配置驱动的 ingestion 管道:从 GitHub issues/discussions 和本地人工问答文件抓取 → 清洗 → LLM 提炼 → embedding → 写入 Azure AI Search。

**Architecture:** monorepo 三 project(本计划只建 shared + ingestion),依赖单向 `ingestion → shared`。每种数据源类型一个 connector(统一接口),归一化为 QADocument(QA 不切割、一条记录),LLM 提炼出 content 字段,raw_content 只做召回(retrievable=false)。每源独立执行、独立失败,since 水位增量同步。

**Tech Stack:** Python 3.11+,uv(包管理,workspace 模式),pydantic v2(模型),httpx(GitHub API),azure-search-documents ≥ 11.5,openai(Azure OpenAI),PyYAML,pytest + pytest-asyncio + respx(HTTP mock)。

**Spec:** `docs/superpowers/specs/2026-08-21-copilot-advisor-design.md`(本计划实现其第 4、5、6 节)

## Global Constraints

- Python ≥ 3.11;全部包用 uv workspace 管理(根 `pyproject.toml` + 各项目自己的 `pyproject.toml`)
- 索引字段名固定:`id, title, content, keywords, raw_content, url, source, doc_type, product_area, created_at, resolved_at, content_vector`(spec 6.2,与 AI Search semantic 槽位对齐)
- `raw_content`:searchable 但 `retrievable=false`;超长截断(32k 字符)
- QA 不切割:一个 QA = 一条索引记录;`content` 为 LLM 提炼结果(≤500 token 目标)
- embedding 打在 `title + "\n" + content` 上,模型 `text-embedding-3-large`(3072 维)
- id 幂等:`sha256(f"{source}:{native_id}")` 前 32 位十六进制
- 单源失败不影响其他源;摘要报告 + 失败时 exit code 非 0
- 凭据只从环境变量读:`GITHUB_TOKEN`、`AZURE_SEARCH_ENDPOINT`、`AZURE_SEARCH_API_KEY`、`AZURE_OPENAI_ENDPOINT`、`AZURE_OPENAI_API_KEY`;不落盘、不进 git
- 所有网络调用的单元测试用 respx mock;真实资源集成测试标记 `@pytest.mark.integration`,默认不跑(`-m "not integration"`)

---

### Task 1: Monorepo 骨架 + uv workspace

**Files:**
- Create: `pyproject.toml`(workspace 根)
- Create: `shared/pyproject.toml`, `shared/src/advisor_shared/__init__.py`
- Create: `ingestion/pyproject.toml`, `ingestion/src/ingestion/__init__.py`
- Create: `ingestion/tests/__init__.py`, `shared/tests/__init__.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: 可安装的 `advisor-shared` 与 `advisor-ingestion` 包;`uv run pytest` 可执行

- [ ] **Step 1: 写 workspace 根 pyproject.toml**

```toml
# pyproject.toml(仓库根)
[project]
name = "github-copilot-advisor"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[tool.uv.workspace]
members = ["shared", "ingestion"]

[tool.uv.sources]
advisor-shared = { workspace = true }

[dependency-groups]
dev = ["pytest>=8.0", "pytest-asyncio>=0.24", "respx>=0.21"]

[tool.pytest.ini_options]
testpaths = ["shared/tests", "ingestion/tests"]
asyncio_mode = "auto"
markers = ["integration: requires real Azure/GitHub resources"]
addopts = "-m 'not integration'"
```

- [ ] **Step 2: 写 shared 包定义**

```toml
# shared/pyproject.toml
[project]
name = "advisor-shared"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["pydantic>=2.7"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/advisor_shared"]
```

```python
# shared/src/advisor_shared/__init__.py
```

- [ ] **Step 3: 写 ingestion 包定义**

```toml
# ingestion/pyproject.toml
[project]
name = "advisor-ingestion"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "advisor-shared",
    "httpx>=0.27",
    "azure-search-documents>=11.5.1",
    "openai>=1.40",
    "pyyaml>=6.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ingestion"]
```

```python
# ingestion/src/ingestion/__init__.py
```

空的 `shared/tests/__init__.py`、`ingestion/tests/__init__.py` 也一并创建。

- [ ] **Step 4: 更新 .gitignore 并验证 workspace**

`.gitignore` 追加:

```
.venv/
*.egg-info/
.pytest_cache/
uv.lock
.state/
```

Run: `uv sync && uv run python -c "import advisor_shared, ingestion; print('ok')"`
Expected: 输出 `ok`

Run: `uv run pytest`
Expected: `no tests ran`(空测试树,退出码 5 属正常——此步只验证 pytest 可启动)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml shared/ ingestion/ .gitignore
git commit -m "chore: scaffold uv workspace with shared and ingestion packages"
```

---

### Task 2: shared — QADocument 模型与索引 schema 定义

**Files:**
- Create: `shared/src/advisor_shared/documents.py`
- Create: `shared/src/advisor_shared/index_schema.py`
- Test: `shared/tests/test_documents.py`

**Interfaces:**
- Produces:
  - `QADocument`(pydantic):字段 `id: str, title: str, content: str, keywords: list[str], raw_content: str, url: str, source: str, doc_type: Literal["issue","discussion","manual_qa"], product_area: str, created_at: datetime, resolved_at: datetime | None, content_vector: list[float] | None = None`
  - `QADocument.make_id(source: str, native_id: str) -> str`(staticmethod,sha256 前 32 hex)
  - `QADocument.embedding_text() -> str`(返回 `f"{title}\n{content}"`)
  - `QADocument.to_search_document() -> dict`(datetime 转 ISO8601 字符串,`content_vector` 为 None 时不含该键)
  - `RAW_CONTENT_MAX_CHARS = 32_000`(常量,truncation 在 connector 侧使用)
  - `index_schema.build_index_definition(name: str, vector_dimensions: int = 3072) -> dict`:返回 AI Search 创建索引的完整字段/semantic/vector 配置 dict

- [ ] **Step 1: 写失败测试**

```python
# shared/tests/test_documents.py
from datetime import datetime, timezone

from advisor_shared.documents import QADocument, RAW_CONTENT_MAX_CHARS
from advisor_shared.index_schema import build_index_definition


def make_doc(**overrides) -> QADocument:
    defaults = dict(
        id=QADocument.make_id("vscode", "12345"),
        title="Copilot chat times out",
        content="升级到最新版插件并检查代理设置。",
        keywords=["github-copilot", "network"],
        raw_content="original thread text " * 10,
        url="https://github.com/microsoft/vscode/issues/12345",
        source="vscode-copilot-issues",
        doc_type="issue",
        product_area="vscode",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        resolved_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return QADocument(**defaults)


def test_make_id_is_deterministic_32_hex():
    a = QADocument.make_id("vscode", "12345")
    b = QADocument.make_id("vscode", "12345")
    assert a == b and len(a) == 32 and int(a, 16) is not None


def test_make_id_differs_by_source():
    assert QADocument.make_id("a", "1") != QADocument.make_id("b", "1")


def test_embedding_text_is_title_newline_content():
    doc = make_doc()
    assert doc.embedding_text() == doc.title + "\n" + doc.content


def test_to_search_document_serializes_datetimes_and_omits_none_vector():
    d = make_doc().to_search_document()
    assert d["created_at"] == "2026-01-01T00:00:00+00:00"
    assert "content_vector" not in d
    doc2 = make_doc(content_vector=[0.1] * 4)
    assert doc2.to_search_document()["content_vector"] == [0.1] * 4


def test_raw_content_max_chars_constant():
    assert RAW_CONTENT_MAX_CHARS == 32_000


def test_index_definition_matches_spec_fields():
    idx = build_index_definition("qa-index")
    fields = {f["name"]: f for f in idx["fields"]}
    assert set(fields) == {
        "id", "title", "content", "keywords", "raw_content", "url",
        "source", "doc_type", "product_area", "created_at", "resolved_at",
        "content_vector",
    }
    assert fields["id"]["key"] is True
    assert fields["raw_content"]["searchable"] is True
    assert fields["raw_content"]["retrievable"] is False
    assert fields["keywords"]["filterable"] is True
    assert fields["content_vector"]["dimensions"] == 3072
    sem = idx["semantic"]["configurations"][0]["prioritizedFields"]
    assert sem["titleField"]["fieldName"] == "title"
    assert sem["prioritizedContentFields"][0]["fieldName"] == "content"
    assert sem["prioritizedKeywordsFields"][0]["fieldName"] == "keywords"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest shared/tests/test_documents.py -v`
Expected: FAIL,`ModuleNotFoundError: advisor_shared.documents`

- [ ] **Step 3: 实现 documents.py**

```python
# shared/src/advisor_shared/documents.py
"""QA 文档模型 — ingestion 写入与 agent 检索共同遵守的契约。"""
import hashlib
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

RAW_CONTENT_MAX_CHARS = 32_000


class QADocument(BaseModel):
    id: str
    title: str
    content: str
    keywords: list[str]
    raw_content: str
    url: str
    source: str
    doc_type: Literal["issue", "discussion", "manual_qa"]
    product_area: str
    created_at: datetime
    resolved_at: datetime | None
    content_vector: list[float] | None = None

    @staticmethod
    def make_id(source: str, native_id: str) -> str:
        return hashlib.sha256(f"{source}:{native_id}".encode()).hexdigest()[:32]

    def embedding_text(self) -> str:
        return f"{self.title}\n{self.content}"

    def to_search_document(self) -> dict:
        d = self.model_dump()
        d["created_at"] = self.created_at.isoformat()
        d["resolved_at"] = self.resolved_at.isoformat() if self.resolved_at else None
        if self.content_vector is None:
            d.pop("content_vector")
        return d
```

- [ ] **Step 4: 实现 index_schema.py**

```python
# shared/src/advisor_shared/index_schema.py
"""Azure AI Search 索引定义 — 字段名与 semantic 槽位一一对应(spec 6.2)。"""


def _field(name: str, type_: str = "Edm.String", *, key: bool = False,
           searchable: bool = False, filterable: bool = False,
           facetable: bool = False, sortable: bool = False,
           retrievable: bool = True) -> dict:
    return {
        "name": name, "type": type_, "key": key, "searchable": searchable,
        "filterable": filterable, "facetable": facetable,
        "sortable": sortable, "retrievable": retrievable,
    }


def build_index_definition(name: str, vector_dimensions: int = 3072) -> dict:
    fields = [
        _field("id", key=True, filterable=True),
        _field("title", searchable=True),
        _field("content", searchable=True),
        {**_field("keywords", "Collection(Edm.String)", searchable=True,
                  filterable=True, facetable=True)},
        _field("raw_content", searchable=True, retrievable=False),
        _field("url"),
        _field("source", filterable=True, facetable=True),
        _field("doc_type", filterable=True),
        _field("product_area", filterable=True, facetable=True),
        _field("created_at", "Edm.DateTimeOffset", filterable=True, sortable=True),
        _field("resolved_at", "Edm.DateTimeOffset", filterable=True, sortable=True),
        {
            "name": "content_vector", "type": "Collection(Edm.Single)",
            "searchable": True, "retrievable": False,
            "dimensions": vector_dimensions,
            "vectorSearchProfile": "vprofile",
        },
    ]
    return {
        "name": name,
        "fields": fields,
        "vectorSearch": {
            "algorithms": [{"name": "hnsw", "kind": "hnsw"}],
            "profiles": [{"name": "vprofile", "algorithm": "hnsw"}],
        },
        "semantic": {
            "configurations": [{
                "name": "default",
                "prioritizedFields": {
                    "titleField": {"fieldName": "title"},
                    "prioritizedContentFields": [{"fieldName": "content"}],
                    "prioritizedKeywordsFields": [{"fieldName": "keywords"}],
                },
            }]
        },
    }
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest shared/tests/test_documents.py -v`
Expected: 7 项全部 PASS

- [ ] **Step 6: Commit**

```bash
git add shared/
git commit -m "feat(shared): QADocument model and AI Search index schema"
```

---

### Task 3: ingestion — sources.yaml 配置加载

**Files:**
- Create: `ingestion/src/ingestion/config.py`
- Create: `ingestion/sources.yaml`
- Test: `ingestion/tests/test_config.py`

**Interfaces:**
- Produces:
  - `SourceConfig`(pydantic):`name: str, type: Literal["github_issues","github_discussions","manual_qa"], repo: str | None, repo_org: str | None, path: str | None, product_area: str, filters: SourceFilters`
  - `SourceFilters`(pydantic):`labels: list[str] = [], state: str | None = None, answered: bool | None = None, since: str | None = None`
  - `load_sources(path: Path) -> list[SourceConfig]`(YAML 校验失败抛 `ValueError`,含出错源名)

- [ ] **Step 1: 写失败测试**

```python
# ingestion/tests/test_config.py
from pathlib import Path

import pytest

from ingestion.config import load_sources


def write_yaml(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "sources.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_valid_sources(tmp_path):
    p = write_yaml(tmp_path, """
sources:
  - name: vscode-copilot-issues
    type: github_issues
    repo: microsoft/vscode
    product_area: vscode
    filters:
      labels: [github-copilot]
      state: closed
  - name: teams-qa
    type: manual_qa
    path: ./data/teams_qa/
    product_area: general
""")
    sources = load_sources(p)
    assert len(sources) == 2
    assert sources[0].filters.labels == ["github-copilot"]
    assert sources[1].type == "manual_qa"


def test_unknown_type_raises_with_source_name(tmp_path):
    p = write_yaml(tmp_path, """
sources:
  - name: bad-one
    type: rss_feed
    product_area: general
""")
    with pytest.raises(ValueError, match="bad-one"):
        load_sources(p)


def test_github_issues_requires_repo(tmp_path):
    p = write_yaml(tmp_path, """
sources:
  - name: no-repo
    type: github_issues
    product_area: vscode
""")
    with pytest.raises(ValueError, match="no-repo"):
        load_sources(p)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest ingestion/tests/test_config.py -v`
Expected: FAIL,`ModuleNotFoundError: ingestion.config`

- [ ] **Step 3: 实现 config.py**

```python
# ingestion/src/ingestion/config.py
"""sources.yaml 加载与校验 — 配置驱动的数据源清单(spec 6.1)。"""
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ValidationError, model_validator


class SourceFilters(BaseModel):
    labels: list[str] = []
    state: str | None = None
    answered: bool | None = None
    since: str | None = None


class SourceConfig(BaseModel):
    name: str
    type: Literal["github_issues", "github_discussions", "manual_qa"]
    repo: str | None = None
    repo_org: str | None = None
    path: str | None = None
    product_area: str
    filters: SourceFilters = SourceFilters()

    @model_validator(mode="after")
    def check_type_requirements(self):
        if self.type == "github_issues" and not self.repo:
            raise ValueError("github_issues source requires 'repo'")
        if self.type == "github_discussions" and not (self.repo or self.repo_org):
            raise ValueError("github_discussions source requires 'repo' or 'repo_org'")
        if self.type == "manual_qa" and not self.path:
            raise ValueError("manual_qa source requires 'path'")
        return self


def load_sources(path: Path) -> list[SourceConfig]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = []
    for entry in raw.get("sources", []):
        name = entry.get("name", "<unnamed>")
        try:
            sources.append(SourceConfig(**entry))
        except (ValidationError, ValueError) as e:
            raise ValueError(f"invalid source '{name}': {e}") from e
    return sources
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest ingestion/tests/test_config.py -v`
Expected: 3 项 PASS

- [ ] **Step 5: 写真实 sources.yaml(spec 第 4 节数据源清单)**

```yaml
# ingestion/sources.yaml
sources:
  - name: copilot-faq
    type: github_discussions
    repo_org: githubcopilotfaq
    product_area: general
    filters: { answered: true }

  - name: vscode-copilot-issues
    type: github_issues
    repo: microsoft/vscode
    product_area: vscode
    filters:
      labels: [github-copilot]
      state: closed

  - name: vscode-copilot-release
    type: github_issues
    repo: microsoft/vscode-copilot-release
    product_area: vscode
    filters: { state: closed }

  - name: intellij-copilot
    type: github_issues
    repo: microsoft/copilot-intellij-feedback
    product_area: intellij
    filters: { state: closed }

  - name: copilot-cli-issues
    type: github_issues
    repo: github/copilot-cli
    product_area: cli
    filters: { state: closed }

  - name: copilot-cli-discussions
    type: github_discussions
    repo: github/copilot-cli
    product_area: cli
    filters: { answered: true }

  - name: community-copilot
    type: github_discussions
    repo: community/community
    product_area: general
    filters: { answered: true }

  - name: teams-qa
    type: manual_qa
    path: ./data/teams_qa/
    product_area: general
```

- [ ] **Step 6: Commit**

```bash
git add ingestion/
git commit -m "feat(ingestion): config-driven source list loading"
```

---

### Task 4: ingestion — Connector 接口 + manual_qa connector

**Files:**
- Create: `ingestion/src/ingestion/connectors/__init__.py`
- Create: `ingestion/src/ingestion/connectors/base.py`
- Create: `ingestion/src/ingestion/connectors/manual_qa.py`
- Create: `ingestion/tests/test_manual_qa_connector.py`
- Create: `ingestion/data/teams_qa/.gitkeep`

**Interfaces:**
- Consumes: `SourceConfig`(Task 3)、`QADocument`/`RAW_CONTENT_MAX_CHARS`(Task 2)
- Produces:
  - `RawQA`(pydantic,connector 输出、提炼前的中间形态):`native_id: str, title: str, question: str, answer: str, url: str, labels: list[str], doc_type: str, created_at: datetime, resolved_at: datetime | None`
  - `Connector`(ABC):`async def fetch(self, since: datetime | None) -> AsyncIterator[RawQA]`;构造 `__init__(self, config: SourceConfig)`
  - `ManualQAConnector(Connector)`:读 `config.path` 目录下 `*.md`(frontmatter:title/url/labels/created_at,正文 `## Question` / `## Answer` 两节)
  - `connectors.create(config: SourceConfig) -> Connector`(工厂,type → 类)

- [ ] **Step 1: 写失败测试**

```python
# ingestion/tests/test_manual_qa_connector.py
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ingestion.config import SourceConfig
from ingestion.connectors import create
from ingestion.connectors.manual_qa import ManualQAConnector

QA_MD = """---
title: Copilot 在企业代理下无法登录
url: https://teams.example.com/thread/123
labels: [network, auth]
created_at: 2026-03-01
---
## Question
公司代理环境下 VS Code Copilot 登录一直转圈。

## Answer
设置 http.proxy 并将 *.githubcopilot.com 加入代理白名单后解决。
"""


@pytest.fixture
def qa_dir(tmp_path: Path) -> Path:
    d = tmp_path / "teams_qa"
    d.mkdir()
    (d / "proxy-login.md").write_text(QA_MD, encoding="utf-8")
    return d


def make_config(path: Path) -> SourceConfig:
    return SourceConfig(
        name="teams-qa", type="manual_qa", path=str(path), product_area="general"
    )


async def collect(connector, since=None):
    return [qa async for qa in connector.fetch(since)]


async def test_parses_frontmatter_and_sections(qa_dir):
    items = await collect(ManualQAConnector(make_config(qa_dir)))
    assert len(items) == 1
    qa = items[0]
    assert qa.native_id == "proxy-login.md"
    assert qa.title == "Copilot 在企业代理下无法登录"
    assert "转圈" in qa.question
    assert "白名单" in qa.answer
    assert qa.labels == ["network", "auth"]
    assert qa.doc_type == "manual_qa"
    assert qa.created_at == datetime(2026, 3, 1, tzinfo=timezone.utc)


async def test_since_filters_older_files(qa_dir):
    items = await collect(
        ManualQAConnector(make_config(qa_dir)),
        since=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    assert items == []


async def test_skips_file_missing_answer_section(qa_dir):
    (qa_dir / "bad.md").write_text(
        "---\ntitle: t\ncreated_at: 2026-03-01\n---\n## Question\nq only\n",
        encoding="utf-8",
    )
    items = await collect(ManualQAConnector(make_config(qa_dir)))
    assert len(items) == 1  # bad.md 被跳过


def test_factory_dispatches_by_type(qa_dir):
    assert isinstance(create(make_config(qa_dir)), ManualQAConnector)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest ingestion/tests/test_manual_qa_connector.py -v`
Expected: FAIL,`ModuleNotFoundError: ingestion.connectors`

- [ ] **Step 3: 实现 base.py 与 RawQA**

```python
# ingestion/src/ingestion/connectors/base.py
"""Connector 统一接口:每种数据源类型一个实现(spec 6.1)。"""
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime

from pydantic import BaseModel

from ingestion.config import SourceConfig


class RawQA(BaseModel):
    """connector 输出、LLM 提炼前的中间形态。"""
    native_id: str
    title: str
    question: str
    answer: str
    url: str
    labels: list[str] = []
    doc_type: str
    created_at: datetime
    resolved_at: datetime | None = None


class Connector(ABC):
    def __init__(self, config: SourceConfig):
        self.config = config

    @abstractmethod
    def fetch(self, since: datetime | None) -> AsyncIterator[RawQA]:
        """按时间水位增量产出 RawQA。"""
```

- [ ] **Step 4: 实现 manual_qa.py**

```python
# ingestion/src/ingestion/connectors/manual_qa.py
"""本地人工整理问答目录:一文件一 QA,frontmatter + Question/Answer 两节。"""
import re
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from ingestion.connectors.base import Connector, RawQA

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
_SECTION = re.compile(r"^## (Question|Answer)\s*\n(.*?)(?=^## |\Z)", re.DOTALL | re.MULTILINE)


def _to_utc(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    raise ValueError(f"invalid created_at: {value!r}")


class ManualQAConnector(Connector):
    async def fetch(self, since: datetime | None) -> AsyncIterator[RawQA]:
        for path in sorted(Path(self.config.path).glob("*.md")):
            qa = self._parse(path)
            if qa is None:
                continue
            if since and qa.created_at <= since:
                continue
            yield qa

    def _parse(self, path: Path) -> RawQA | None:
        m = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
        if not m:
            return None
        meta = yaml.safe_load(m.group(1)) or {}
        sections = {name: body.strip() for name, body in _SECTION.findall(m.group(2))}
        if "Question" not in sections or "Answer" not in sections:
            return None
        created = _to_utc(meta.get("created_at"))
        return RawQA(
            native_id=path.name,
            title=meta.get("title", path.stem),
            question=sections["Question"],
            answer=sections["Answer"],
            url=meta.get("url", ""),
            labels=list(meta.get("labels", [])),
            doc_type="manual_qa",
            created_at=created,
            resolved_at=created,
        )
```

- [ ] **Step 5: 实现工厂 __init__.py**

```python
# ingestion/src/ingestion/connectors/__init__.py
from ingestion.config import SourceConfig
from ingestion.connectors.base import Connector, RawQA
from ingestion.connectors.manual_qa import ManualQAConnector

_REGISTRY: dict[str, type[Connector]] = {
    "manual_qa": ManualQAConnector,
}


def create(config: SourceConfig) -> Connector:
    try:
        cls = _REGISTRY[config.type]
    except KeyError:
        raise ValueError(f"no connector for source type '{config.type}'") from None
    return cls(config)


__all__ = ["Connector", "RawQA", "ManualQAConnector", "create"]
```

(github_issues / github_discussions 在 Task 5/6 注册进 `_REGISTRY`。)

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest ingestion/tests/test_manual_qa_connector.py -v`
Expected: 4 项 PASS

- [ ] **Step 7: Commit**

```bash
git add ingestion/
git commit -m "feat(ingestion): connector interface and manual_qa connector"
```

---

### Task 5: ingestion — github_issues connector(REST + 答案提取)

**Files:**
- Create: `ingestion/src/ingestion/connectors/github_issues.py`
- Create: `ingestion/src/ingestion/github_client.py`
- Modify: `ingestion/src/ingestion/connectors/__init__.py`(注册 `github_issues`)
- Test: `ingestion/tests/test_github_issues_connector.py`

**Interfaces:**
- Consumes: `Connector`/`RawQA`(Task 4)、`SourceConfig`(Task 3)
- Produces:
  - `GitHubClient`:`__init__(self, token: str | None = None, base_url: str = "https://api.github.com")`;`async def paged_get(self, path: str, params: dict) -> AsyncIterator[dict]`(自动翻页,处理 403/429 限流:读 `Retry-After`/`X-RateLimit-Reset` 退避,最多 3 次)
  - `GitHubIssuesConnector(Connector)`:REST `GET /repos/{repo}/issues`(state/labels/since 来自 filters),对每个 issue 拉 comments 并提取答案
  - `extract_answer(issue: dict, comments: list[dict]) -> str | None`(模块级函数,便于单测):优先 OWNER/MEMBER/COLLABORATOR 的最后实质评论(>40 字符),其次 +1 reaction 最多的评论,否则 None(调用方保留讨论串);过滤 `user.type == "Bot"`

- [ ] **Step 1: 写失败测试(respx mock REST API)**

```python
# ingestion/tests/test_github_issues_connector.py
import httpx
import pytest
import respx

from ingestion.config import SourceConfig
from ingestion.connectors import create
from ingestion.connectors.github_issues import (
    GitHubIssuesConnector,
    extract_answer,
)

API = "https://api.github.com"


def make_config() -> SourceConfig:
    return SourceConfig(
        name="vscode-copilot-issues", type="github_issues",
        repo="microsoft/vscode", product_area="vscode",
        filters={"labels": ["github-copilot"], "state": "closed"},
    )


def issue_payload(number=1, title="Copilot hangs", body="It hangs.") -> dict:
    return {
        "number": number, "title": title, "body": body,
        "html_url": f"https://github.com/microsoft/vscode/issues/{number}",
        "labels": [{"name": "github-copilot"}],
        "created_at": "2026-01-01T00:00:00Z",
        "closed_at": "2026-01-05T00:00:00Z",
        "user": {"login": "someuser", "type": "User"},
    }


def comment(body, assoc="NONE", plus_one=0, user_type="User") -> dict:
    return {
        "body": body, "author_association": assoc,
        "user": {"login": "u", "type": user_type},
        "reactions": {"+1": plus_one},
    }


def test_extract_answer_prefers_last_member_comment():
    comments = [
        comment("try restarting, that fixed it for me " * 2, "NONE", plus_one=10),
        comment("Fixed in v1.97 — upgrade the extension please.", "MEMBER"),
    ]
    assert "v1.97" in extract_answer(issue_payload(), comments)


def test_extract_answer_falls_back_to_most_plus_one():
    comments = [
        comment("me too", "NONE", plus_one=1),
        comment("Downgrading the proxy setting resolved this for our whole team.",
                "NONE", plus_one=7),
    ]
    assert "proxy" in extract_answer(issue_payload(), comments)


def test_extract_answer_ignores_bots_and_short_comments():
    comments = [
        comment("This issue is stale.", "NONE", user_type="Bot", plus_one=99),
        comment("+1", "MEMBER"),
    ]
    assert extract_answer(issue_payload(), comments) is None


@respx.mock
async def test_fetch_yields_rawqa_and_skips_pull_requests():
    respx.get(f"{API}/repos/microsoft/vscode/issues").mock(
        return_value=httpx.Response(200, json=[
            issue_payload(number=1),
            {**issue_payload(number=2), "pull_request": {"url": "x"}},  # PR 要跳过
        ])
    )
    respx.get(f"{API}/repos/microsoft/vscode/issues/1/comments").mock(
        return_value=httpx.Response(200, json=[
            comment("Fixed in v1.97 — upgrade the extension please.", "MEMBER"),
        ])
    )
    connector = create(make_config())
    assert isinstance(connector, GitHubIssuesConnector)
    items = [qa async for qa in connector.fetch(None)]
    assert len(items) == 1
    qa = items[0]
    assert qa.native_id == "1"
    assert qa.doc_type == "issue"
    assert "v1.97" in qa.answer
    assert qa.labels == ["github-copilot"]


@respx.mock
async def test_fetch_skips_issue_without_answer():
    respx.get(f"{API}/repos/microsoft/vscode/issues").mock(
        return_value=httpx.Response(200, json=[issue_payload(number=3)])
    )
    respx.get(f"{API}/repos/microsoft/vscode/issues/3/comments").mock(
        return_value=httpx.Response(200, json=[])
    )
    items = [qa async for qa in create(make_config()).fetch(None)]
    assert items == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest ingestion/tests/test_github_issues_connector.py -v`
Expected: FAIL,`ModuleNotFoundError: ingestion.connectors.github_issues`

- [ ] **Step 3: 实现 github_client.py**

```python
# ingestion/src/ingestion/github_client.py
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

    async def aclose(self):
        await self._client.aclose()
```

- [ ] **Step 4: 实现 github_issues.py**

```python
# ingestion/src/ingestion/connectors/github_issues.py
"""GitHub issues connector:REST 拉取 + 答案提取(spec 6.1)。"""
from collections.abc import AsyncIterator
from datetime import datetime

from ingestion.connectors.base import Connector, RawQA
from ingestion.github_client import GitHubClient

_MAINTAINER = {"OWNER", "MEMBER", "COLLABORATOR"}
_MIN_ANSWER_CHARS = 40


def _substantive(c: dict) -> bool:
    return (c.get("user") or {}).get("type") != "Bot" and \
        len(c.get("body") or "") > _MIN_ANSWER_CHARS


def extract_answer(issue: dict, comments: list[dict]) -> str | None:
    candidates = [c for c in comments if _substantive(c)]
    maintainer = [c for c in candidates
                  if c.get("author_association") in _MAINTAINER]
    if maintainer:
        return maintainer[-1]["body"]
    by_plus_one = [c for c in candidates
                   if (c.get("reactions") or {}).get("+1", 0) > 0]
    if by_plus_one:
        return max(by_plus_one,
                   key=lambda c: c["reactions"]["+1"])["body"]
    return None


class GitHubIssuesConnector(Connector):
    def __init__(self, config, client: GitHubClient | None = None):
        super().__init__(config)
        self.client = client or GitHubClient()

    async def fetch(self, since: datetime | None) -> AsyncIterator[RawQA]:
        f = self.config.filters
        params: dict = {"state": f.state or "closed"}
        if f.labels:
            params["labels"] = ",".join(f.labels)
        if since:
            params["since"] = since.isoformat()
        async for issue in self.client.paged_get(
                f"/repos/{self.config.repo}/issues", params):
            if "pull_request" in issue:
                continue
            comments = [c async for c in self.client.paged_get(
                f"/repos/{self.config.repo}/issues/{issue['number']}/comments", {})]
            answer = extract_answer(issue, comments)
            if not answer:
                continue
            yield RawQA(
                native_id=str(issue["number"]),
                title=issue["title"],
                question=issue.get("body") or "",
                answer=answer,
                url=issue["html_url"],
                labels=[l["name"] for l in issue.get("labels", [])],
                doc_type="issue",
                created_at=datetime.fromisoformat(
                    issue["created_at"].replace("Z", "+00:00")),
                resolved_at=datetime.fromisoformat(
                    issue["closed_at"].replace("Z", "+00:00"))
                if issue.get("closed_at") else None,
            )
```

- [ ] **Step 5: 注册进工厂**

`ingestion/src/ingestion/connectors/__init__.py` 的 `_REGISTRY` 增加:

```python
from ingestion.connectors.github_issues import GitHubIssuesConnector

_REGISTRY: dict[str, type[Connector]] = {
    "manual_qa": ManualQAConnector,
    "github_issues": GitHubIssuesConnector,
}
```

(`__all__` 同步加 `"GitHubIssuesConnector"`。)

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest ingestion/tests/test_github_issues_connector.py -v`
Expected: 5 项 PASS

- [ ] **Step 7: Commit**

```bash
git add ingestion/
git commit -m "feat(ingestion): github_issues connector with answer extraction"
```

---

### Task 6: ingestion — github_discussions connector(GraphQL)

**Files:**
- Create: `ingestion/src/ingestion/connectors/github_discussions.py`
- Modify: `ingestion/src/ingestion/github_client.py`(增加 `graphql` 方法)
- Modify: `ingestion/src/ingestion/connectors/__init__.py`(注册 `github_discussions`)
- Test: `ingestion/tests/test_github_discussions_connector.py`

**Interfaces:**
- Consumes: `Connector`/`RawQA`(Task 4)、`GitHubClient`(Task 5)
- Produces:
  - `GitHubClient.graphql(self, query: str, variables: dict) -> dict`(POST /graphql,错误时抛 `RuntimeError(errors)`)
  - `GitHubDiscussionsConnector(Connector)`:repo 模式查单仓库 discussions;repo_org 模式先列 org 仓库再逐仓库查。`filters.answered=true` 时只产出有 acceptedAnswer 的,answer 取 acceptedAnswer.body

- [ ] **Step 1: 写失败测试(respx mock GraphQL)**

```python
# ingestion/tests/test_github_discussions_connector.py
import httpx
import pytest
import respx

from ingestion.config import SourceConfig
from ingestion.connectors import create
from ingestion.connectors.github_discussions import GitHubDiscussionsConnector

API = "https://api.github.com"


def make_config(**over) -> SourceConfig:
    base = dict(
        name="copilot-cli-discussions", type="github_discussions",
        repo="github/copilot-cli", product_area="cli",
        filters={"answered": True},
    )
    base.update(over)
    return SourceConfig(**base)


def discussion_node(number=7, answered=True) -> dict:
    return {
        "number": number,
        "title": "How to configure MCP servers?",
        "body": "Where does copilot CLI read MCP config from?",
        "url": f"https://github.com/github/copilot-cli/discussions/{number}",
        "createdAt": "2026-02-01T00:00:00Z",
        "answerChosenAt": "2026-02-02T00:00:00Z" if answered else None,
        "answer": {"body": "Put mcp.json under ~/.copilot/ and restart."}
                  if answered else None,
        "labels": {"nodes": [{"name": "question"}]},
    }


def gql_response(nodes, has_next=False):
    return {
        "data": {"repository": {"discussions": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": has_next, "endCursor": "c1"},
        }}}
    }


@respx.mock
async def test_fetch_answered_discussions_only():
    respx.post(f"{API}/graphql").mock(
        return_value=httpx.Response(200, json=gql_response([
            discussion_node(7, answered=True),
            discussion_node(8, answered=False),
        ]))
    )
    connector = create(make_config())
    assert isinstance(connector, GitHubDiscussionsConnector)
    items = [qa async for qa in connector.fetch(None)]
    assert len(items) == 1
    qa = items[0]
    assert qa.native_id == "7"
    assert qa.doc_type == "discussion"
    assert "mcp.json" in qa.answer
    assert qa.labels == ["question"]


@respx.mock
async def test_graphql_error_raises():
    respx.post(f"{API}/graphql").mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "bad"}]})
    )
    with pytest.raises(RuntimeError, match="bad"):
        _ = [qa async for qa in create(make_config()).fetch(None)]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest ingestion/tests/test_github_discussions_connector.py -v`
Expected: FAIL,`ModuleNotFoundError: ingestion.connectors.github_discussions`

- [ ] **Step 3: GitHubClient 增加 graphql 方法**

在 `ingestion/src/ingestion/github_client.py` 的 `GitHubClient` 类中追加:

```python
    async def graphql(self, query: str, variables: dict) -> dict:
        resp = await self._client.post("/graphql",
                                       json={"query": query,
                                             "variables": variables})
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        return data["data"]
```

- [ ] **Step 4: 实现 github_discussions.py**

```python
# ingestion/src/ingestion/connectors/github_discussions.py
"""GitHub discussions connector:GraphQL(answered 过滤仅 GraphQL 支持)。"""
from collections.abc import AsyncIterator
from datetime import datetime

from ingestion.connectors.base import Connector, RawQA
from ingestion.github_client import GitHubClient

_DISCUSSIONS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    discussions(first: 50, after: $cursor,
                orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes {
        number title body url createdAt answerChosenAt
        answer { body }
        labels(first: 10) { nodes { name } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

_ORG_REPOS_QUERY = """
query($org: String!, $cursor: String) {
  organization(login: $org) {
    repositories(first: 50, after: $cursor) {
      nodes { nameWithOwner hasDiscussionsEnabled }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


class GitHubDiscussionsConnector(Connector):
    def __init__(self, config, client: GitHubClient | None = None):
        super().__init__(config)
        self.client = client or GitHubClient()

    async def fetch(self, since: datetime | None) -> AsyncIterator[RawQA]:
        for repo in await self._target_repos():
            async for qa in self._fetch_repo(repo, since):
                yield qa

    async def _target_repos(self) -> list[str]:
        if self.config.repo:
            return [self.config.repo]
        repos, cursor = [], None
        while True:
            data = await self.client.graphql(
                _ORG_REPOS_QUERY, {"org": self.config.repo_org, "cursor": cursor})
            page = data["organization"]["repositories"]
            repos += [r["nameWithOwner"] for r in page["nodes"]
                      if r["hasDiscussionsEnabled"]]
            if not page["pageInfo"]["hasNextPage"]:
                return repos
            cursor = page["pageInfo"]["endCursor"]

    async def _fetch_repo(self, repo: str,
                          since: datetime | None) -> AsyncIterator[RawQA]:
        owner, name = repo.split("/")
        cursor = None
        while True:
            data = await self.client.graphql(
                _DISCUSSIONS_QUERY,
                {"owner": owner, "name": name, "cursor": cursor})
            page = data["repository"]["discussions"]
            for node in page["nodes"]:
                created = _dt(node["createdAt"])
                if since and created <= since:
                    return  # DESC 排序,更早的都可跳过
                if self.config.filters.answered and not node.get("answer"):
                    continue
                yield RawQA(
                    native_id=str(node["number"]),
                    title=node["title"],
                    question=node.get("body") or "",
                    answer=(node.get("answer") or {}).get("body", ""),
                    url=node["url"],
                    labels=[l["name"] for l in node["labels"]["nodes"]],
                    doc_type="discussion",
                    created_at=created,
                    resolved_at=_dt(node.get("answerChosenAt")),
                )
            if not page["pageInfo"]["hasNextPage"]:
                return
            cursor = page["pageInfo"]["endCursor"]
```

- [ ] **Step 5: 注册进工厂**

`_REGISTRY` 增加 `"github_discussions": GitHubDiscussionsConnector`(import 同步加)。

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest ingestion/tests/test_github_discussions_connector.py -v`
Expected: 2 项 PASS

- [ ] **Step 7: Commit**

```bash
git add ingestion/
git commit -m "feat(ingestion): github_discussions connector via GraphQL"
```

---

### Task 7: ingestion — LLM 提炼(RawQA → QADocument)

**Files:**
- Create: `ingestion/src/ingestion/refine.py`
- Test: `ingestion/tests/test_refine.py`

**Interfaces:**
- Consumes: `RawQA`(Task 4)、`QADocument`/`RAW_CONTENT_MAX_CHARS`(Task 2)、`SourceConfig`(Task 3)
- Produces:
  - `Refiner`:`__init__(self, chat_client, model: str = "gpt-4o")`(chat_client 为 openai `AsyncAzureOpenAI` 兼容对象,测试注入 fake)
  - `async def refine(self, raw: RawQA, config: SourceConfig) -> QADocument | None`:LLM 生成提炼 content;LLM 调用失败返回 None(单条失败不中断批次,spec 10.1);raw_content = question+answer 拼接并截断;keywords = labels + product_area 去重
  - `build_refine_prompt(raw: RawQA) -> str`(模块级,便于单测断言要素)

- [ ] **Step 1: 写失败测试(fake chat client,不打真网)**

```python
# ingestion/tests/test_refine.py
from datetime import datetime, timezone
from types import SimpleNamespace

from advisor_shared.documents import RAW_CONTENT_MAX_CHARS
from ingestion.config import SourceConfig
from ingestion.connectors.base import RawQA
from ingestion.refine import Refiner, build_refine_prompt


class FakeChat:
    """最小 openai AsyncClient 形状:chat.completions.create(...)"""
    def __init__(self, reply: str | Exception):
        self._reply = reply
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._reply, Exception):
            raise self._reply
        msg = SimpleNamespace(content=self._reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def make_raw(question="Q " * 100, answer="A " * 100) -> RawQA:
    return RawQA(
        native_id="42", title="Copilot proxy issue",
        question=question, answer=answer,
        url="https://github.com/x/y/issues/42",
        labels=["network"], doc_type="issue",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def make_config() -> SourceConfig:
    return SourceConfig(name="src-a", type="github_issues",
                        repo="x/y", product_area="vscode")


async def test_refine_produces_qadocument():
    chat = FakeChat("要点:代理配置错误。解决:设置 http.proxy 后重启。")
    doc = await Refiner(chat).refine(make_raw(), make_config())
    assert doc.content.startswith("要点")
    assert doc.source == "src-a"
    assert doc.product_area == "vscode"
    assert set(doc.keywords) == {"network", "vscode"}
    assert doc.id == doc.make_id("src-a", "42")
    assert doc.content_vector is None  # embedding 在 Task 9 才加


async def test_refine_truncates_raw_content():
    raw = make_raw(question="x" * 40_000)
    doc = await Refiner(FakeChat("ok")).refine(raw, make_config())
    assert len(doc.raw_content) == RAW_CONTENT_MAX_CHARS


async def test_refine_returns_none_on_llm_failure():
    chat = FakeChat(RuntimeError("rate limited"))
    assert await Refiner(chat).refine(make_raw(), make_config()) is None


def test_prompt_contains_question_answer_and_length_rule():
    p = build_refine_prompt(make_raw(question="THE_Q", answer="THE_A"))
    assert "THE_Q" in p and "THE_A" in p and "500" in p
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest ingestion/tests/test_refine.py -v`
Expected: FAIL,`ModuleNotFoundError: ingestion.refine`

- [ ] **Step 3: 实现 refine.py**

```python
# ingestion/src/ingestion/refine.py
"""LLM 提炼:RawQA → QADocument。content 只存提炼结果,原文只做召回(spec 6.2)。"""
import logging

from advisor_shared.documents import QADocument, RAW_CONTENT_MAX_CHARS
from ingestion.config import SourceConfig
from ingestion.connectors.base import RawQA

logger = logging.getLogger(__name__)


def build_refine_prompt(raw: RawQA) -> str:
    return (
        "你是知识库编辑。把下面的问答提炼成简洁条目,用于支持机器人直接引用回答。\n"
        "要求:保留问题要点与完整解决步骤;剥离寒暄、模板、日志噪声;"
        "不超过 500 token;与原文语言保持一致。\n\n"
        f"标题:{raw.title}\n\n问题:\n{raw.question}\n\n解答:\n{raw.answer}"
    )


class Refiner:
    def __init__(self, chat_client, model: str = "gpt-4o"):
        self.chat = chat_client
        self.model = model

    async def refine(self, raw: RawQA, config: SourceConfig) -> QADocument | None:
        try:
            resp = await self.chat.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": build_refine_prompt(raw)}],
                temperature=0.1,
            )
            content = resp.choices[0].message.content.strip()
        except Exception:
            logger.exception("refine failed for %s:%s", config.name, raw.native_id)
            return None
        raw_content = f"{raw.question}\n\n{raw.answer}"[:RAW_CONTENT_MAX_CHARS]
        keywords = sorted({*raw.labels, config.product_area})
        return QADocument(
            id=QADocument.make_id(config.name, raw.native_id),
            title=raw.title,
            content=content,
            keywords=keywords,
            raw_content=raw_content,
            url=raw.url,
            source=config.name,
            doc_type=raw.doc_type,
            product_area=config.product_area,
            created_at=raw.created_at,
            resolved_at=raw.resolved_at,
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest ingestion/tests/test_refine.py -v`
Expected: 4 项 PASS

- [ ] **Step 5: Commit**

```bash
git add ingestion/
git commit -m "feat(ingestion): LLM refinement of raw QA into index documents"
```

---

### Task 8: ingestion — 水位(watermark)状态存储

**Files:**
- Create: `ingestion/src/ingestion/watermark.py`
- Test: `ingestion/tests/test_watermark.py`

**Interfaces:**
- Consumes: 无(独立单元)
- Produces:
  - `WatermarkStore`:`__init__(self, path: Path)`(JSON 文件,默认 `.state/watermarks.json`,目录自动创建)
  - `get(self, source_name: str) -> datetime | None`
  - `set(self, source_name: str, value: datetime) -> None`(立即落盘,原子写:先写 `.tmp` 再 rename)

- [ ] **Step 1: 写失败测试**

```python
# ingestion/tests/test_watermark.py
from datetime import datetime, timezone

from ingestion.watermark import WatermarkStore


def test_get_unknown_source_returns_none(tmp_path):
    store = WatermarkStore(tmp_path / "wm.json")
    assert store.get("nope") is None


def test_set_then_get_roundtrip(tmp_path):
    store = WatermarkStore(tmp_path / "wm.json")
    ts = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    store.set("vscode", ts)
    assert store.get("vscode") == ts


def test_persists_across_instances(tmp_path):
    path = tmp_path / "wm.json"
    ts = datetime(2026, 3, 1, tzinfo=timezone.utc)
    WatermarkStore(path).set("a", ts)
    assert WatermarkStore(path).get("a") == ts


def test_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "wm.json"
    WatermarkStore(path).set("a", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert path.exists()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest ingestion/tests/test_watermark.py -v`
Expected: FAIL,`ModuleNotFoundError: ingestion.watermark`

- [ ] **Step 3: 实现 watermark.py**

```python
# ingestion/src/ingestion/watermark.py
"""每源 since 水位:上次成功同步时间,JSON 落盘(spec 6.5)。"""
import json
from datetime import datetime
from pathlib import Path


class WatermarkStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict[str, str] = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, source_name: str) -> datetime | None:
        raw = self._data.get(source_name)
        return datetime.fromisoformat(raw) if raw else None

    def set(self, source_name: str, value: datetime) -> None:
        self._data[source_name] = value.isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.rename(self.path)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest ingestion/tests/test_watermark.py -v`
Expected: 4 项 PASS

- [ ] **Step 5: Commit**

```bash
git add ingestion/
git commit -m "feat(ingestion): per-source watermark store"
```

---

### Task 9: ingestion — embedding + AI Search 写入(SearchWriter)

**Files:**
- Create: `ingestion/src/ingestion/search_writer.py`
- Test: `ingestion/tests/test_search_writer.py`

**Interfaces:**
- Consumes: `QADocument`(Task 2)、`build_index_definition`(Task 2)
- Produces:
  - `SearchWriter`:`__init__(self, search_client, embed_client, embed_model: str = "text-embedding-3-large")`(两个 client 均注入,测试用 fake;生产装配在 Task 10)
  - `async def upsert(self, docs: list[QADocument]) -> int`:批量 embedding(每批 ≤16 条 embedding_text)→ 填充 content_vector → `merge_or_upload_documents`;返回成功条数
  - `ensure_index(search_index_client, index_name: str) -> None`(模块级):用 `build_index_definition` 建索引,已存在(HttpResponseError 409/"already exists")则跳过

- [ ] **Step 1: 写失败测试**

```python
# ingestion/tests/test_search_writer.py
from datetime import datetime, timezone
from types import SimpleNamespace

from advisor_shared.documents import QADocument
from ingestion.search_writer import SearchWriter


def make_doc(n: int) -> QADocument:
    return QADocument(
        id=QADocument.make_id("s", str(n)), title=f"t{n}", content=f"c{n}",
        keywords=[], raw_content="r", url="https://x", source="s",
        doc_type="issue", product_area="vscode",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), resolved_at=None,
    )


class FakeEmbeddings:
    def __init__(self):
        self.batches: list[list[str]] = []
        self.embeddings = SimpleNamespace(create=self._create)

    async def _create(self, model: str, input: list[str]):
        self.batches.append(input)
        data = [SimpleNamespace(embedding=[float(i)] * 4)
                for i in range(len(input))]
        return SimpleNamespace(data=data)


class FakeSearchClient:
    def __init__(self):
        self.uploaded: list[dict] = []

    async def merge_or_upload_documents(self, documents: list[dict]):
        self.uploaded.extend(documents)
        return [SimpleNamespace(succeeded=True) for _ in documents]


async def test_upsert_embeds_and_uploads():
    embed, search = FakeEmbeddings(), FakeSearchClient()
    writer = SearchWriter(search, embed)
    count = await writer.upsert([make_doc(1), make_doc(2)])
    assert count == 2
    assert len(search.uploaded) == 2
    assert all("content_vector" in d for d in search.uploaded)
    assert embed.batches[0] == ["t1\nc1", "t2\nc2"]


async def test_upsert_batches_embeddings_by_16():
    embed, search = FakeEmbeddings(), FakeSearchClient()
    await SearchWriter(search, embed).upsert([make_doc(i) for i in range(40)])
    assert [len(b) for b in embed.batches] == [16, 16, 8]


async def test_upsert_empty_list_is_noop():
    embed, search = FakeEmbeddings(), FakeSearchClient()
    assert await SearchWriter(search, embed).upsert([]) == 0
    assert search.uploaded == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest ingestion/tests/test_search_writer.py -v`
Expected: FAIL,`ModuleNotFoundError: ingestion.search_writer`

- [ ] **Step 3: 实现 search_writer.py**

```python
# ingestion/src/ingestion/search_writer.py
"""embedding + AI Search 幂等写入(spec 6.4)。"""
import logging

from azure.core.exceptions import HttpResponseError
from azure.search.documents.indexes.models import SearchIndex

from advisor_shared.documents import QADocument
from advisor_shared.index_schema import build_index_definition

logger = logging.getLogger(__name__)

_EMBED_BATCH = 16


def ensure_index(search_index_client, index_name: str) -> None:
    definition = build_index_definition(index_name)
    try:
        search_index_client.create_index(SearchIndex.deserialize(definition))
        logger.info("created index %s", index_name)
    except HttpResponseError as e:
        if e.status_code == 409 or "already exists" in str(e).lower():
            return
        raise


class SearchWriter:
    def __init__(self, search_client, embed_client,
                 embed_model: str = "text-embedding-3-large"):
        self.search = search_client
        self.embed = embed_client
        self.embed_model = embed_model

    async def upsert(self, docs: list[QADocument]) -> int:
        if not docs:
            return 0
        for i in range(0, len(docs), _EMBED_BATCH):
            batch = docs[i:i + _EMBED_BATCH]
            resp = await self.embed.embeddings.create(
                model=self.embed_model,
                input=[d.embedding_text() for d in batch],
            )
            for doc, item in zip(batch, resp.data):
                doc.content_vector = item.embedding
        results = await self.search.merge_or_upload_documents(
            documents=[d.to_search_document() for d in docs])
        return sum(1 for r in results if r.succeeded)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest ingestion/tests/test_search_writer.py -v`
Expected: 3 项 PASS

- [ ] **Step 5: Commit**

```bash
git add ingestion/
git commit -m "feat(ingestion): embedding batches and AI Search upsert writer"
```

---

### Task 10: ingestion — pipeline 编排 + CLI 入口

**Files:**
- Create: `ingestion/src/ingestion/pipeline.py`
- Create: `ingestion/src/ingestion/__main__.py`
- Test: `ingestion/tests/test_pipeline.py`

**Interfaces:**
- Consumes: 前面全部 —— `load_sources`(T3)、`connectors.create`(T4-6)、`Refiner`(T7)、`WatermarkStore`(T8)、`SearchWriter`/`ensure_index`(T9)
- Produces:
  - `SourceReport`(pydantic):`source: str, fetched: int, refined: int, upserted: int, skipped: int, error: str | None`
  - `run_pipeline(sources, connector_factory, refiner, writer, watermarks, run_started_at) -> list[SourceReport]`(全依赖注入;单源异常捕获进 report.error,继续下一源;成功源写入后把水位推进到 run_started_at)
  - CLI:`python -m ingestion run [--source NAME] [--full-refresh] [--sources-file PATH]`;任一源 error → exit code 1

- [ ] **Step 1: 写失败测试**

```python
# ingestion/tests/test_pipeline.py
from datetime import datetime, timezone

from ingestion.config import SourceConfig
from ingestion.connectors.base import Connector, RawQA
from ingestion.pipeline import run_pipeline
from ingestion.watermark import WatermarkStore

NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)


def make_config(name: str) -> SourceConfig:
    return SourceConfig(name=name, type="manual_qa", path="/tmp/x",
                        product_area="general")


def make_raw(n: int) -> RawQA:
    return RawQA(native_id=str(n), title=f"t{n}", question="q", answer="a",
                 url="https://x", doc_type="manual_qa",
                 created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))


class StubConnector(Connector):
    def __init__(self, config, items=(), error=None):
        super().__init__(config)
        self.items, self.error = list(items), error
        self.seen_since = "UNSET"

    async def fetch(self, since):
        self.seen_since = since
        if self.error:
            raise self.error
        for item in self.items:
            yield item


class StubRefiner:
    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)

    async def refine(self, raw, config):
        if raw.native_id in self.fail_ids:
            return None
        from advisor_shared.documents import QADocument
        return QADocument(
            id=QADocument.make_id(config.name, raw.native_id),
            title=raw.title, content="refined", keywords=[], raw_content="r",
            url=raw.url, source=config.name, doc_type="manual_qa",
            product_area=config.product_area,
            created_at=raw.created_at, resolved_at=None,
        )


class StubWriter:
    def __init__(self):
        self.docs = []

    async def upsert(self, docs):
        self.docs.extend(docs)
        return len(docs)


async def test_happy_path_reports_and_advances_watermark(tmp_path):
    wm = WatermarkStore(tmp_path / "wm.json")
    connectors = {"a": StubConnector(make_config("a"), [make_raw(1), make_raw(2)])}
    writer = StubWriter()
    reports = await run_pipeline(
        [make_config("a")], lambda c: connectors[c.name],
        StubRefiner(), writer, wm, run_started_at=NOW)
    assert reports[0].fetched == 2 and reports[0].upserted == 2
    assert reports[0].error is None
    assert len(writer.docs) == 2
    assert wm.get("a") == NOW


async def test_failing_source_isolated_and_watermark_frozen(tmp_path):
    wm = WatermarkStore(tmp_path / "wm.json")
    connectors = {
        "bad": StubConnector(make_config("bad"), error=RuntimeError("api down")),
        "good": StubConnector(make_config("good"), [make_raw(1)]),
    }
    reports = await run_pipeline(
        [make_config("bad"), make_config("good")],
        lambda c: connectors[c.name], StubRefiner(), StubWriter(), wm,
        run_started_at=NOW)
    by_name = {r.source: r for r in reports}
    assert "api down" in by_name["bad"].error
    assert by_name["good"].error is None and by_name["good"].upserted == 1
    assert wm.get("bad") is None       # 失败源水位不推进
    assert wm.get("good") == NOW


async def test_refine_failure_counted_as_skipped(tmp_path):
    wm = WatermarkStore(tmp_path / "wm.json")
    connectors = {"a": StubConnector(make_config("a"), [make_raw(1), make_raw(2)])}
    reports = await run_pipeline(
        [make_config("a")], lambda c: connectors[c.name],
        StubRefiner(fail_ids={"1"}), StubWriter(), wm, run_started_at=NOW)
    assert reports[0].skipped == 1 and reports[0].upserted == 1


async def test_full_refresh_passes_none_since(tmp_path):
    wm = WatermarkStore(tmp_path / "wm.json")
    wm.set("a", datetime(2026, 5, 1, tzinfo=timezone.utc))
    stub = StubConnector(make_config("a"), [])
    await run_pipeline([make_config("a")], lambda c: stub, StubRefiner(),
                       StubWriter(), wm, run_started_at=NOW, full_refresh=True)
    assert stub.seen_since is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest ingestion/tests/test_pipeline.py -v`
Expected: FAIL,`ModuleNotFoundError: ingestion.pipeline`

- [ ] **Step 3: 实现 pipeline.py**

```python
# ingestion/src/ingestion/pipeline.py
"""管道编排:每源独立执行、独立失败;摘要报告(spec 6.6、10.1)。"""
import logging
from datetime import datetime

from pydantic import BaseModel

from ingestion.config import SourceConfig

logger = logging.getLogger(__name__)

_UPSERT_BATCH = 50


class SourceReport(BaseModel):
    source: str
    fetched: int = 0
    refined: int = 0
    upserted: int = 0
    skipped: int = 0
    error: str | None = None


async def run_pipeline(sources: list[SourceConfig], connector_factory,
                       refiner, writer, watermarks,
                       run_started_at: datetime,
                       full_refresh: bool = False) -> list[SourceReport]:
    reports = []
    for config in sources:
        report = SourceReport(source=config.name)
        reports.append(report)
        try:
            since = None if full_refresh else watermarks.get(config.name)
            connector = connector_factory(config)
            batch = []
            async for raw in connector.fetch(since):
                report.fetched += 1
                doc = await refiner.refine(raw, config)
                if doc is None:
                    report.skipped += 1
                    continue
                report.refined += 1
                batch.append(doc)
                if len(batch) >= _UPSERT_BATCH:
                    report.upserted += await writer.upsert(batch)
                    batch = []
            report.upserted += await writer.upsert(batch)
            watermarks.set(config.name, run_started_at)
            logger.info("source %s done: %s", config.name, report)
        except Exception as e:
            logger.exception("source %s failed", config.name)
            report.error = str(e)
    return reports
```

- [ ] **Step 4: 实现 __main__.py(CLI + 生产装配)**

```python
# ingestion/src/ingestion/__main__.py
"""CLI:python -m ingestion run [--source NAME] [--full-refresh]。"""
import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from azure.core.credentials import AzureKeyCredential
from azure.search.documents.aio import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from openai import AsyncAzureOpenAI

from ingestion import connectors
from ingestion.config import load_sources
from ingestion.pipeline import run_pipeline
from ingestion.refine import Refiner
from ingestion.search_writer import SearchWriter, ensure_index
from ingestion.watermark import WatermarkStore

INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX", "copilot-qa")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ingestion")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--source", help="只跑指定源")
    run.add_argument("--full-refresh", action="store_true")
    run.add_argument("--sources-file", type=Path,
                     default=Path(__file__).parent.parent.parent / "sources.yaml")
    return parser


async def main() -> int:
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args()

    sources = load_sources(args.sources_file)
    if args.source:
        sources = [s for s in sources if s.name == args.source]
        if not sources:
            print(f"unknown source: {args.source}", file=sys.stderr)
            return 2

    endpoint = os.environ["AZURE_SEARCH_ENDPOINT"]
    key = AzureKeyCredential(os.environ["AZURE_SEARCH_API_KEY"])
    ensure_index(SearchIndexClient(endpoint, key), INDEX_NAME)

    openai_client = AsyncAzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version="2024-10-21",
    )
    search_client = SearchClient(endpoint, INDEX_NAME, key)
    try:
        reports = await run_pipeline(
            sources, connectors.create,
            Refiner(openai_client), SearchWriter(search_client, openai_client),
            WatermarkStore(Path(".state/watermarks.json")),
            run_started_at=datetime.now(timezone.utc),
            full_refresh=args.full_refresh,
        )
    finally:
        await search_client.close()
        await openai_client.close()

    failed = False
    for r in reports:
        status = f"ERROR: {r.error}" if r.error else "ok"
        print(f"{r.source}: fetched={r.fetched} refined={r.refined} "
              f"upserted={r.upserted} skipped={r.skipped} [{status}]")
        failed = failed or bool(r.error)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 5: 运行全部测试确认通过**

Run: `uv run pytest -v`
Expected: Task 2-10 全部测试 PASS

- [ ] **Step 6: Commit**

```bash
git add ingestion/
git commit -m "feat(ingestion): pipeline orchestration and CLI entrypoint"
```

---

### Task 11: 集成冒烟测试(真实资源,可选跑)

**Files:**
- Create: `ingestion/tests/test_integration_smoke.py`
- Create: `.env.example`

**Interfaces:**
- Consumes: 全部生产装配(Task 10)

- [ ] **Step 1: 写集成测试(标记 integration,默认不跑)**

```python
# ingestion/tests/test_integration_smoke.py
"""真实资源冒烟:需要环境变量齐全,uv run pytest -m integration 才跑。"""
import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

REQUIRED_ENV = ["GITHUB_TOKEN", "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_API_KEY",
                "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY"]


@pytest.fixture(autouse=True)
def require_env():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        pytest.skip(f"missing env: {missing}")


async def test_intellij_source_end_to_end_small_window():
    """拉 copilot-intellij-feedback 最近 7 天 closed issues,走完整管道。"""
    from ingestion.__main__ import main  # noqa: F401  验证装配可导入
    from ingestion import connectors
    from ingestion.config import SourceConfig

    config = SourceConfig(
        name="intellij-copilot-smoke", type="github_issues",
        repo="microsoft/copilot-intellij-feedback", product_area="intellij",
        filters={"state": "closed"},
    )
    since = datetime.now(timezone.utc) - timedelta(days=7)
    connector = connectors.create(config)
    items = []
    async for qa in connector.fetch(since):
        items.append(qa)
        if len(items) >= 3:
            break
    # 近 7 天可能没有带答案的 closed issue;拉到 0 条也算连通成功
    for qa in items:
        assert qa.title and qa.answer and qa.url
```

- [ ] **Step 2: 写 .env.example**

```bash
# .env.example — 复制为 .env 并填入真实值(.env 已在 .gitignore)
GITHUB_TOKEN=ghp_xxx
AZURE_SEARCH_ENDPOINT=https://<name>.search.windows.net
AZURE_SEARCH_API_KEY=xxx
AZURE_SEARCH_INDEX=copilot-qa
AZURE_OPENAI_ENDPOINT=https://<name>.openai.azure.com
AZURE_OPENAI_API_KEY=xxx
```

- [ ] **Step 3: 验证默认测试套不受影响**

Run: `uv run pytest`
Expected: 单元测试全 PASS,integration 项显示 deselected

Run(有真实凭据时): `uv run pytest -m integration -v`
Expected: PASS 或 skip(缺环境变量)

- [ ] **Step 4: Commit**

```bash
git add ingestion/tests/test_integration_smoke.py .env.example
git commit -m "test(ingestion): integration smoke test and env template"
```
