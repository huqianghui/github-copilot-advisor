# AI Search 索引配置与灌数

> 面向:第一次部署本项目、或索引出问题需要重建的人。

`search_solutions` 的知识库那一半依赖 Azure AI Search 索引。索引**不存在**或
**schema 不对**时,整条 KB 检索链路是死的 —— 而且失败方式很隐蔽,见 §4。

---

## 1. 前置

`.env` 里需要(见 `.env.example`):

```
AZURE_SEARCH_ENDPOINT=https://<name>.search.windows.net
AZURE_SEARCH_API_KEY=<admin key,不是 query key>
AZURE_SEARCH_INDEX=copilot-qa          # 可选,默认 copilot-qa
AZURE_OPENAI_ENDPOINT=...              # 灌数时做 LLM 提炼与 embedding
AZURE_OPENAI_API_KEY=...
GITHUB_TOKEN=ghp_...                   # 抓取数据源
```

**必须是 admin key**:创建索引与写文档都需要写权限,query key 不够。

---

## 2. 正常路径:直接灌数

索引**不存在**时,ingestion 会自动用正确的 schema 建好再写入:

```bash
uv run --env-file .env python -m ingestion run
```

单个数据源:

```bash
uv run --env-file .env python -m ingestion run --source copilot-cli-discussions
```

首次部署建议**先跑一个小源试水**(`copilot-cli-discussions` 量最小),
确认 schema、LLM 提炼、检索命中三件事都正常,再跑全量。

数据源在 `ingestion/sources.yaml` 里配置,加源不需要改代码(spec 决策 12)。

---

## 3. 索引已存在但 schema 不对 —— **必须先删**

> 这是 2026-09-07 真实踩过的坑,不是假想。

`ingestion/src/ingestion/search_writer.py` 的 `ensure_index()` 在索引已存在时
**静默返回,不更新 schema**:

```python
try:
    search_index_client.create_index(SearchIndex(definition))
except HttpResponseError as e:
    if e.status_code == 409 or "already exists" in str(e).lower():
        return          # ← 坏 schema 原样留着
    raise
```

所以对着一个坏索引跑 ingestion,会**跑完 GitHub 抓取和 embedding(花掉配额和钱),
最后写进一个字段不全的索引**,检索照样死。

### 先诊断

```bash
uv run --env-file .env python - <<'PY'
import os
from azure.core.credentials import AzureKeyCredential
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents import SearchClient
from advisor_shared.index_schema import build_index_definition

ep  = os.environ["AZURE_SEARCH_ENDPOINT"]
key = AzureKeyCredential(os.environ["AZURE_SEARCH_API_KEY"])
name = os.environ.get("AZURE_SEARCH_INDEX", "copilot-qa")

want = {f["name"] for f in build_index_definition(name)["fields"]}
try:
    idx = SearchIndexClient(ep, key).get_index(name)
except Exception as e:
    print(f"索引不存在({type(e).__name__}) —— 直接跑 ingestion 即可")
    raise SystemExit

have = {f.name for f in idx.fields}
print(f"文档数     : {SearchClient(ep, name, key).get_document_count()}")
print(f"应有字段 {len(want)} : {sorted(want)}")
print(f"现有字段 {len(have)} : {sorted(have)}")
print(f"缺失       : {sorted(want - have) or '无 —— schema 正确'}")
PY
```

**应有的 12 个字段**(定义在 `shared/src/advisor_shared/index_schema.py`):

| 字段 | 作用 |
|---|---|
| `id` | key,源+原始 ID 哈希,幂等 |
| `title` / `content` / `keywords` | searchable,对应 semantic 的 title/content/keywords 槽位 |
| `raw_content` | searchable 但 `retrievable=false` —— 只提高召回,不出现在结果里 |
| `content_vector` | 向量检索(3072 维,text-embedding-3-large) |
| `url` | 原始链接 |
| `source` / `doc_type` / `product_area` | filterable |
| `created_at` / `resolved_at` | filterable + sortable |

字段名与 semantic configuration 的槽位**一一对应,零映射**(spec §6.2)。

### 再删除

**删除不可逆。先确认文档数为 0,或确认那些文档可以重灌。**

```bash
uv run --env-file .env python - <<'PY'
import os
from azure.core.credentials import AzureKeyCredential
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents import SearchClient

ep  = os.environ["AZURE_SEARCH_ENDPOINT"]
key = AzureKeyCredential(os.environ["AZURE_SEARCH_API_KEY"])
name = os.environ.get("AZURE_SEARCH_INDEX", "copilot-qa")

n = SearchClient(ep, name, key).get_document_count()
print(f"文档数 = {n}")
if n != 0:
    raise SystemExit("文档数非 0,中止 —— 请人工确认这些文档可以丢弃")

SearchIndexClient(ep, key).delete_index(name)
print(f"已删除 {name!r}")
PY
```

然后回到 §2 重新灌数。

---

## 4. 为什么这个故障很隐蔽

索引坏掉时,失败**不会冒到用户面前**,原因有两层:

1. **Azure 的报错在下游**:`CannotSearchWithoutSearchableFields` 在**查询时**才抛,
   而不是写入时。所以灌数看起来是成功的。
2. **`combined.py` 吞掉异常**:`search_solutions` 对两侧检索的任何异常都
   `except` + 一条 WARNING + 返回 `[]`。于是**"索引彻底坏了"与"这次没查到"
   在工具输出上完全无法区分**(见 `docs/known-issues.md` §1.4)。

结果就是:agent 照常回答(靠 `web_search` 兜底),回答质量下降但不报错,
**没人会注意到 KB 从来没生效过**。

**所以:部署后要主动验证,不要等报错。**

---

## 5. 灌完后的验证

```bash
uv run --env-file .env python - <<'PY'
import asyncio, os
from azure.core.credentials import AzureKeyCredential
from azure.search.documents.aio import SearchClient
from openai import AsyncAzureOpenAI
from advisor_agent.search.knowledge import KnowledgeSearchClient

async def main():
    sc = SearchClient(os.environ["AZURE_SEARCH_ENDPOINT"],
                      os.environ.get("AZURE_SEARCH_INDEX", "copilot-qa"),
                      AzureKeyCredential(os.environ["AZURE_SEARCH_API_KEY"]))
    print("文档数:", await sc.get_document_count())
    kb = KnowledgeSearchClient(sc, AsyncAzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version="2024-10-21"))
    for q in ("Copilot 登录失败", "premium requests 怎么计费"):
        r = await kb.search(q, top=3)
        print(f"{q!r} -> {len(r)} 条" + (f" | {r[0].title[:50]}" if r else ""))
    await sc.close()

asyncio.run(main())
PY
```

**判读**:文档数 > 0 且两条查询都有结果,才算真的通了。
只看"ingestion 跑完没报错"**不够** —— 见 §4。

完整的行为回归用 eval:

```bash
uv run --env-file .env pytest -m integration agent/tests/test_eval_behavior.py -v
```

---

## 6. 增量与重跑

每个源维护一个 `since` 水位,存在 `.state/` 下,跑完自动推进。
`.state/` 不存在 = 全部按首次全量抓。

- **重跑安全**:写入是 `mergeOrUpload`,`id` 幂等
- **强制全量**:`--full-refresh`
- **单源失败不影响其他源**,结尾输出摘要报告,exit code 非 0 供告警(spec §6.6)
