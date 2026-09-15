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

行为回归报告现在会在每轮事件的 `search_attempts` 中记录各检索分支的结果、
超时和错误类型,可据此区分这些情况;工具输出和原有兜底逻辑保持不变。

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

每次运行结束会自动生成 `agent/tests/output/<UTC时间戳>.json`
(例如 `20260906T015321_238000Z.json`),终端会显示报告路径。该目录已加入
`.gitignore`,历史报告不会覆盖,也不会自动清理。

报告包含运行起止时间、退出码、逐用例状态与耗时、预期条件、失败/跳过原因,
以及每轮问题、完整回复(含引用和 @人信息)、执行事件。图片仅记录文件名,
不保存原始字节或 base64;问答文本本身仍可能包含敏感信息,分享报告前请检查。
缺环境变量或截图而跳过、用例收集失败时也会保存报告;使用 `-x` 提前停止时,
后续选中但未执行的用例标为 `not_run`。没有选中行为评估用例且无该文件的
收集错误时不生成报告,不影响普通单元测试。

### 查看检索链路是否走通

每轮的 `events[].search_attempts` 记录 KB/GitHub 实时检索的每次调用,
以及 Web 每个 provider 的每次尝试。重复调用不会覆盖旧记录,新一轮重新计数。
这些是服务调用级记录,不展开 SDK 内部的 HTTP 重试。

| 字段 | 含义 |
|---|---|
| `source` | `kb` / `github-live` / `web` |
| `provider` | `azure_ai_search` / `github` / `tavily` / `brave`;未配置 Web provider 时为 `null` |
| `status` | `success` 有结果;`empty` 正常返回但无结果;`timeout` 超时;`error` 异常;`not_configured` 未配置;`cancelled` 被取消 |
| `result_count` | 本次分支/provider 的结果条数;失败、超时、未配置时为 `null`,不是 `0` |
| `duration_ms` | 该分支/provider 的实际耗时,不是组合检索整体耗时 |
| `timeout_seconds` | 组合检索预算或 Web provider 的外层超时限制 |
| `error_type` | 异常类型,如 `HTTPStatusError`、`HttpResponseError`、`ConnectTimeout` |
| `http_status` / `error_code` | 可获取的 HTTP 状态码和服务错误码,如 `403`、`429`、`CannotSearchWithoutSearchableFields` |

KB 的计数是经过语义分数过滤后的结果数,耗时包含问题 embedding 和 AI Search
检索。各来源计数发生在合并去重及回复引用截断之前,不等于最终引用条数。
`empty` 表示调用正常结束,但没有可用命中;`success` 且 `result_count > 0`
才表示拿到了可用结果。Web 是否成功应看具体 provider 的这些字段,
不能仅凭最终 `stage = web` 或 `failover_count = 0`。

检索错误不会写入原始异常消息、请求 URL、响应体或密钥;报告保留错误类型、
HTTP 状态码及格式受限的服务错误码用于排查。`error = null` 仍只表示本轮
Agent 没有整体失败,不代表每次检索都成功。旧报告没有这些数据,需重新运行生成。

### 细粒度检索计时

`tool_latencies_ms.search_solutions` **不是 Azure AI Search 服务耗时**。
它是工具调用耗时,包含 KB 与 GitHub live 并行检索、等待、取消清理和合并。
同一轮重复调用工具时,这个旧字段现在累计所有调用(包括失败/取消);
逐次调用保存在新增的 `events[].timings` 中,不再仅剩最后一次耗时。

| 步骤名 | 计时边界 |
|---|---|
| `tool.search_solutions` | 一次完整工具调用,包含结果序列化 |
| `search.combined` | 一次组合检索,共享等待预算仍为 8 秒 |
| `search.kb` | KB 分支整体,包含 embedding、Search 和本地过滤 |
| `search.kb.embedding` | 问题 embedding SDK 调用 |
| `search.kb.azure_search` | hybrid + semantic 查询及**完整遍历分页结果**,不含 embedding |
| `search.kb.filter` | 本地 reranker 分数过滤和结果构造;记录原始、保留、过滤条数 |
| `search.github-live` / `search.github.request` | GitHub 分支整体 / HTTP 调用 |
| `search.merge` | KB 优先合并、去重及结果构造 |
| `tool.web_search` / `search.web` | Web 工具整体 / 每次 provider 尝试 |
| `search.web.filter` | Web 来源和内容过滤;记录 scope、provider 和条数 |

Azure Search SDK 的 `await search_client.search(...)` 返回惰性 pager;
当前版本在 `async for` 时才发起请求。若只给 `await search(...)` 计时,
会把真正的网络耗时漏掉。本项目的新计时覆盖创建 pager 到全部取数完成。
这个值仍是**客户端观察到的耗时**,包含网络、服务处理、SDK 重试及反序列化,
不能继续凭它拆出服务端 BM25、向量召回或 semantic ranker 各自用了多久。

Portal 的全量 `search=*` 与本项目的“问题 embedding + hybrid + semantic +
GitHub 并行查询”不是等价查询。比较 Search 本身时,应使用相同的查询类型、
向量、过滤条件、top、索引和客户端环境,并区分首次请求与热连接。

每条 `timings` 记录有唯一 `span_id` 和 `parent_span_id`,以及相对于同一请求
起点的 `start_offset_ms`、实际 `duration_ms`、状态和安全诊断字段。
`search_attempts[].span_id` 指向对应的分支/provider 步骤。按起始偏移排序,
而不是按照记录在数组中的完成顺序判断执行先后。

**不要把所有步骤耗时相加**:父步骤包含子步骤,KB 和 GitHub 还互相重叠。
例如 KB 已完成但 GitHub 仍未返回,组合检索仍会等到 GitHub 完成或 8 秒预算到期。
8 秒是等待预算,不是含取消清理和合并在内的硬性总时长上限。预算耗尽的分支记
`timeout`,外层请求取消记 `cancelled`;超时分支内部被取消的子步骤可记 `cancelled`。

日志级别、JSON/文本格式、LLM/Teams 计时和隐私边界见
[Development](../DEVELOPMENT.md#分级日志与延迟分析)。

---

## 6. 增量与重跑

每个源维护一个 `since` 水位,存在 `.state/` 下,跑完自动推进。
`.state/` 不存在 = 全部按首次全量抓。

- **重跑安全**:写入是 `mergeOrUpload`,`id` 幂等
- **强制全量**:`--full-refresh`
- **单源失败不影响其他源**,结尾输出摘要报告,exit code 非 0 供告警(spec §6.6)
