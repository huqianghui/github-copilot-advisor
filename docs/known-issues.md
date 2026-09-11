# 已知问题清单

> 最后更新:2026-09-07
> 基线:260 passed, 17 deselected
> 图片功能:真机冒烟 5/7 行为项通过,基本可用

本文档收集**已确认存在但尚未修复**的问题。分散在对话、review 报告和实现计划里的
条目集中到这里,避免丢失。

**每条都标注了证据强度**:
- **已实证** —— 我们跑过、看到过实际输出
- **已查证** —— 对着代码或官方文档核对过,但没有跑出复现
- **reported** —— review 中提出,未独立验证

不要把 reported 当成已实证。本项目中已有多次"看似合理的诊断被实测推翻"的记录
(见 §6)。

---

## 1. eval 信号不可信(最高优先级)

这一组的共同后果:**任何基于 eval 的回归判断当前都不可靠**。已经发生过多次
"先花力气区分是代码问题还是环境问题"的返工。

### 1.1 AI Search 索引是空的且 schema 不对 —— 已实证

```
索引: copilot-qa
全部字段: ['id']          ← 只有一个
searchable 字段: 【空】
文档数: 0
```

**影响**:每次 KB 查询报 `CannotSearchWithoutSearchableFields`,
`search_solutions` 的知识库那一半**对所有 eval 用例都是死的**。
期望 `kb_hit` 的用例永远不可能命中。

**更根本的是**:本产品的核心设计是「**KB 优先**,GitHub live 补充,web 兜底」
(主 spec §3 升级瀑布),而 **KB 那一半从未被真实验证过**。

**2026-09-07 进展**:坏索引已删除并由 ingestion 用正确的 12 字段 schema
重建(5 个 searchable + `content_vector` 向量字段)。**schema 问题已解决。**

**但内容会残缺** —— 见 §2:7 个数据源里只有 3 个真的能出数据
(cli-issues、cli-discussions、community)。覆盖面最大的三个 `microsoft/*`
源被企业策略拒绝,`copilot-faq` 的过滤条件永远不匹配。

**所以"KB 优先"这条设计仍然没有被完整验证** —— 现在能验证的是
"KB 能命中 cli/community 的内容",而不是 spec §4 设想的全部覆盖面。
判读 eval 的 `kb_hit` 时要记得这个边界。

索引配置与重建步骤见 [`docs/search-index-setup.md`](search-index-setup.md)。

### 1.2 `usage-credits-legacy-term` 是一条绿着说谎的测试 —— 已实证

这条用例存在的意义是证明「模型把 premium requests 映射到 `credits_usage`」。
但在 `not_configured` 路径上,**配置检查发生在 `question_type` 被读取之前**,
所以模型传 `premium_usage`(一个 `usage.py` 明确拒绝的值)或任何垃圾字符串,
这条用例照样绿。

**它不测试它名字承诺的东西。** 绿色的假证据比红色更危险 —— 红的会逼人去看。

**修法**:把模型实际传入的 `question_type` 记到 `RunContext` 上并断言。
这在 `not_configured` 路径上就能工作(参数在任何分支之前就已选定),
**不需要 org、token 或实时账单数据**。

**成本**:小。触及 run-context 与 event 表面,值得单独 review。

### 1.3 GitHub live search 返回 422 —— **已修复**(`a93ebd7`)

> **本节先后有两个错误诊断,都由实测推翻。保留经过以免重蹈。**
> 见 §7 的方法论条目。

**真因(读响应体一眼可见)**:

```json
{"message": "Query must include 'is:issue' or 'is:pull-request'", "status": "422"}
```

GitHub 现在**要求** `/search/issues` 的 query 含 `is:issue` 或 `is:pull-request`。
我们的 query 只有 `state:open`。

**严重程度**:不是"长查询会失败",是**每一次调用都 422** —— 连 `q=copilot`
这样的最小查询都被拒。`github_live_search` **从来没有成功过**。

结合 1.1 的空索引:`search_solutions` 的**两半从未同时活过**,产品的核心设计
(KB 优先 → GitHub live 补充 → web 兜底)在本部署里**只有第三级在跑**。

**为什么长期没被发现**:所有单元测试都 mock 掉了 GitHub 的响应,
**mock 让我们测的是"我们以为 GitHub 会怎么回应",而不是它实际怎么回应**。
这与 `image/*` 那次(Teams 发通配符而我们假设具体类型)是**同一模式的第二个实例**。

#### 顺带确立的长度规则(实测,非文档)

修复时用真实 token 打了 20+ 个样本,拟合出:

- **限定符不计入预算** —— 169 字符的限定符块与 8 字符的,阈值完全相同
- **一个空格消耗 3 个字符配额,非空格字符消耗 1**(CJK 也是 1 —— 200 个汉字、
  百分号编码后 1800 字节,照样通过)

据此做的 4 次盲预测**全中**。实际后果:一段 40 词的英文错误 `len()` 约 230,
"看起来在预算内",真实消耗约 308,**仍然 422**。
**按 `len()` 做的守卫会漏掉长查询这一类** —— 这正是本节第二个错误诊断
会导致的结果。

机制未知(空格→`%20` 的归一化解释不了 CJK 为何算 1),已在代码里标注为
**经验规则,勿外推**。

### 1.4 `combined.py` 吞掉了检索侧的全部异常 —— 已查证,**这是 1.3 长期隐形的原因**

`agent/src/advisor_agent/search/combined.py:33-37` 对两侧检索的**任何**异常都
`except` + 记一条 `"search side failed"` WARNING + 返回 `[]`。

于是**一个 100% 坏掉的 live 客户端,在工具输出上与"live 这次没查到"完全无法区分**。
一次彻底的服务中断因此存活了整个开发周期。

**修法方向**:区分 **4xx**(我们的 query 错了 —— 应该响亮)与**超时/5xx**
(预期内,可以静默降级)。前者应该以某种方式抵达运营视野,而不是与"没结果"混为一谈。

### 1.5 `github_live.py` 未固定 `X-GitHub-Api-Version` —— 已查证

`usage.py` 给 credits 端点固定了 API 版本,`github_live.py` 没有。
**未固定版本正是这次契约变更能悄无声息落到我们头上的方式。**

未修:加版本头可能改变该端点的其他行为,而我们没有证据说明哪个版本能恢复/改变
search 语义。**需要先查证再动。**

### 1.6 群聊回答过长 —— 已实证

同一次冒烟的回答约 1500 字、5 个小节、20 余个要点,而 prompt「回答风格」明确
写着「**群聊中保持简洁:先给结论/方案,细节收进编号步骤**」。

**双重代价**:群聊里刷屏是 UX 问题;同时长回答是本次 80 秒总耗时的主要成分
(见 6.2)。

**注意**:内容本身是对的、可执行的 —— 问题是**篇幅**,不是质量。修的时候
不要把有用的排查步骤删掉,而是压缩表述、把细节收进折叠或引用。

**追问轮更严重,而且成因不同** —— 2026-09-07 实测:模型手里**有**上一轮的
完整回答(记忆是通的,见 §5.0),但从头把整套排查流程又答了一遍。

根因是 prompt 的 `## 回答风格` 只有三条,而且:
- 「群聊中保持简洁」**没有量化** —— 模型理解的"简洁"就是 1500 字
- **完全没有多轮相关的约束** —— 没有任何一条说"追问时别重复上一轮说过的"

**修法方向**(未实施):给"简洁"一个具体目标;新增一条多轮规则要求**只答增量**。
叠加 §5.1(工具结果不入历史 → 每轮重跑)一起看,两者共同造成了重复感。

**改 prompt 前必读**:本仓改 prompt 规则已有一次教训 —— 上次改规则 8 的措辞,
导致一条**不相关**的 eval 用例从通过变成误升级。**必须跑全量 eval 对照,
不能只看目标行为。**


---

## 2. 数据源与灌数

2026-09-07 首次全量灌数时逐源实测。**7 个源里只有 3 个真的能出数据。**

### 2.1 三个 `microsoft/*` 源被企业策略拒绝 —— 已实证

```
GET /repos/microsoft/vscode/issues -> 403
message: The 'Microsoft Open Source' enterprise forbids access via a
         fine-grained personal access tokens if the token's lifetime
         is greater than 8 days.
```

**不是限流** —— 查过 `core: 5000/5000` 配额是满的。是 Microsoft Open Source
企业策略禁止有效期 > 8 天的 fine-grained PAT。

影响 `vscode-copilot-issues`、`vscode-copilot-release`、`intellij-copilot`
三个源 —— **恰好是设计里覆盖面最大的三个**(spec §4 数据源清单)。

**待决**:
- 换 ≤8 天有效期的 fine-grained PAT —— 能解锁,但每 8 天要换一次,运维成本高
- 换 classic PAT —— 策略消息只点名 fine-grained,classic 是否可行**未验证**
- 接受 `microsoft/*` 不可用 —— 知识库只覆盖 cli + community

### 2.2 `copilot-faq` 的 `answered: true` 永远不匹配 —— 已实证

```
githubcopilotfaq/copilotfaq: 57 条 discussions
有采纳答案(answer 非空): 0 条
分类: 使用技巧汇总(25) 使用问题汇总(18) 最新功能更新(9) General(5)
      —— 四个分类全部 isAnswerable=False
```

**结构性,不是偶然**:GitHub 在 `isAnswerable=False` 的分类下**根本不允许标记
采纳答案**,所以 `answer` 恒为 null。`sources.yaml` 里的
`filters: { answered: true }` 在这个 repo 上永远匹配 0 条。

**这个过滤用错了地方**:那些分类名(使用技巧汇总、使用问题汇总)说明它是
**人工策展的 FAQ 合集** —— 内容本身就是答案,不存在"提问 → 采纳"的形态。
`answered` 作为质量门槛是为"社区问答"设计的,不适用于策展内容。

**建议**(未实施,改变入库内容,需拍板):去掉该源的 `answered` 过滤。
57 条策展 FAQ 恰恰是知识库最想要的东西。

### 2.3 `teams-qa` 无本地数据 —— 已实证

`sources.yaml` 指向 `./data/teams_qa/`,该目录不存在,产出 0 条。
spec §4 说这是"群内已解决问答(人工整理)",属于**尚未开展的工作**,不是缺陷。

### 2.4 ingestion 全程无进度输出 —— 已查证

`pipeline.run_pipeline()` **跑完全部源才返回**,`__main__` 才打摘要;
`logger.info("source %s done")` 也**只在单个源完成时**触发一次。
中途没有任何进度信号,而首次全量可能跑几十分钟。

叠加两件事让它更难判断:
- 文档只在**每满 50 条**才 upsert(`_UPSERT_BATCH = 50`),所以索引文档数
  长时间停在 0 是**正常**的
- 重定向到文件时 stdout 有缓冲,输出文件在结束前是空的

**结果**:唯一可用的进度信号是轮询索引文档数,而它还有 50 条的粒度延迟。
排查一次"是不是卡住了"花了约 15 分钟。

**修法方向**:逐源开始/结束打日志,或每个 upsert 批次打一行。

### 2.5 `ensure_index()` 不更新已存在索引的 schema —— 已实证

撞到 409 直接静默返回。对着一个 schema 不对的索引跑 ingestion,会**跑完
GitHub 抓取与 embedding(花掉配额和钱)最后写进一个字段不全的索引**。

已在 [`docs/search-index-setup.md`](search-index-setup.md) §3 写明"schema 不对
必须先删"的操作步骤。**代码未改** —— 让 `ensure_index` 检测 schema 漂移并
报错(而非静默)是更好的做法,但那是行为变更,需单独评估。

---

## 3. 测试基础设施

### 3.1 没有任何机制阻止单元测试打真实网络 —— 已两次实证

一条单元测试向生产端点 `smba.trafficmanager.net` 发出带
`Authorization: Bearer` 的请求、收到线上 401、然后 **PASSED**。

仓库**没有任何 `conftest.py`**。`pyproject.toml` 的
`addopts = "-m 'not integration'"` 是**选择过滤器,不是沙箱**,拦不住这个。

**修法**:autouse fixture 阻断 `socket.socket.connect`,对没有 `integration`
marker 的测试生效;放行 loopback。**不要用全局 respx 拦截** —— 会与本仓
已在用的逐测试 `@respx.mock` router 打架。

**风险**:影响 4 个 project / 250 个测试,但**一次运行即可发现全部影响**,
且它打破的任何测试按定义都是在偷偷碰网络的。

**时机**:尽快,但**独立提交**。

---

## 4. 可观测性:工具记账不一致

`AdvisorEvent.tool_latencies_ms` 是运营判断「哪些工具被调用了、值不值得留」的
**唯一数据来源**(主 spec §10.2)。当前它的语义在不同工具间不一致。

### 4.1 `escalate_to_human` 完全不记账 —— 已实证

`tool_latencies_ms` 在该函数里出现 **0 次**。没有 `start`,没有写入。
它只能通过 `run.stage = "escalated"` 被间接观测 —— 这正是
`billing-escalate-quota` 用 `expected_stage_in` 而非 `expect_tool_called` 的原因。

**按驱动 `copilot_usage_lookup` 修复的同一条逻辑,这个工具在"哪些工具被调用了"
的数据里是完全隐形的。**

### 4.2 三个兄弟工具的异常路径漏记 —— 已查证

`search_solutions`(第 31 行)、`web_search`(第 49 行)、
`network_diagnostics`(第 86 行)都是 `start` 在前、**成功路径**无条件记账,
但**没有 `try/finally`** —— 若它们的 `await` 抛异常,记账行被跳过、异常继续上抛。

对比:`copilot_usage_lookup` 已于 `ff3c782` 改为 `try/finally`。

### 4.3 `copilot_usage_lookup` 的延迟指标现在混淆 —— 已查证

`not_configured` 与 `privacy_blocked` 分支记 ~0ms,真实 API 调用记 ~800ms,
**同一个 key**。「是否被调用」的信号是对的(那是修复的目的),但主 spec §10.2 的
健康视图(延迟 P50/P95)这个 key 单独已经答不了"真实查询有多慢"。

**修法**:3.1 与 3.2 是同一句契约 —— **每个工具无条件记账,包括早返回和异常路径**。
建议直接补进主 spec §10.2 然后实现,三处改动加起来不到 30 行,不值得单开 spec。
3.3 需要设计决策(加第二个字段还是分 key),可以缓。

---

## 5. 图片功能的收尾项

**功能已完成并基本验证可用**(2026-09-07):真机冒烟 **5/7 行为项 + 2/3 日志项**
通过,含三条对着历史真实缺陷的验证 —— 只贴图不打字(曾是零响应)、
贴图后追问(多轮记忆)、发 `.docx`(曾会误报幻影图片)。清单见
[`docs/teams-setup.md`](teams-setup.md)「四、冒烟清单」。

**未验证的三条**:1:1 私聊贴图(`is_group` 分支)、一次贴 6 张
(`MAX_IMAGES=4` 截断)、贴含 token 的截图(规则 10 脱敏)。

冒烟过程中修复了一个**致命 bug**(`image/*` 通配符,见 §8)。以下是剩余加固项。

### 5.0 已验证成立的两条行为(记录以免被误判为缺陷)

**多轮记忆确实工作。** 硬证据:第二轮的 GitHub 检索 query 里带着
**只可能来自截图的完整报错原文**,而第二条用户消息里没有任何报错文本。
prompt 规则 9 的复述 → 写入会话历史 → 下一轮可用,这条设计链路成立。

**丢失记忆有两个独立条件**,都不是缺陷:
- 间隔 > **1 小时**(`InMemorySessionStore` TTL,实测 3599s 保留 / 3601s 清空)
- **进程重启**(纯内存;SDK 的 `MemoryStorage` 同理)

主 spec §10.1 已把"会话存储丢失 → 优雅降级为单轮问答"列为接受的失败姿态。
生产含义:**每次发版都会让进行中的对话失忆**,表现为 bot 重新问已答过的问题。
要消除需换 Cosmos/Redis 实现,`SessionStore` Protocol 已预留。

### 5.1 会话历史不含工具结果,每轮重跑整套瀑布 —— 已实证

`core.py` 只把 `marked_text`(user)与 `response.markdown`(assistant)写进历史,
**不含任何工具结果**。所以模型在追问轮看得到上一轮的**回答**,却看不到产生它的
**事实**,只能把工具全部重跑。

实测同一会话相邻两轮(间隔 100 秒):

| 轮次 | tool_latencies_ms |
|---|---|
| 第 1 轮 | `search_solutions` 6594 + `web_search` 4766 + `network_diagnostics` 4000 |
| 第 2 轮 | `search_solutions` 8000 + `web_search` 3983 + `network_diagnostics` 3718 |

`network_diagnostics` 尤其浪费 —— 100 秒内重跑一次网络探测,结果不可能变。

**双重代价**:每轮 60–90 秒(见 §6.2),以及回答重复(见 §1.6 ——
模型手里有上一轮回答但没有增量作答的指令,于是从头完整答一遍)。

**修法有权衡,未定**:把工具结果摘要进历史会涨 token 且可能过时;
按会话缓存 `network_diagnostics` 这类时效性弱的结果更保守。
**建议先看 §1.6 的 prompt 改动能解决多少可见问题,再决定要不要动存储。**

### 5.2 SDK 管线端到端测试缺失 —— 已实证

`file_downloaders` 写入 `state.temp.input_files` 那段管线:
装配测试用 stub、接线测试手工塞 `input_files`,**中间那段没有任何测试**。

而 review 已查出这段管线有三个反直觉行为:
- SDK 在**每个 turn** 都调 `download_files`(它自己的附件门禁
  `_contains_non_text_attachments` 是**死代码,从未被调用**)
- **不做异常隔离** —— `agent_application.py` 裸 `await`,外层只
  `except ApplicationError`
- `input_files` 是**累加而非替换**(`.extend()`)—— 加第二个 downloader 会
  破坏 `skipped` 算术

**修法**:一个走 `agent_app._on_turn` + stub downloader 的测试能同时覆盖三条。

### 5.3 下载没有总时间预算,大小上限事后才判 —— 已查证

`httpx.Timeout(10.0)` 是**每阶段/每次读取**的超时,不是每请求总时长。
慢速滴送的服务器能把一个 turn 拖住**任意久**(最坏情况**无界**,不是
4 × 10s = 40s)。同时 `client.get()` 把整个 body 读进内存后才判大小。

**已验证的成本**(review 时做过原型):`client.stream()` + `aiter_bytes()`
增量截断约 10 行,**现有 respx 测试全部无需修改**即可通过。

**并行化不是答案** —— `asyncio.gather` 只把"无界串行"变成"无界并行"。
正解是**总预算**,本仓 `search/combined.py` 已有该惯用法
(`SEARCH_BUDGET_SECONDS` + `asyncio.wait(timeout=...)`)。

### 5.4 `httpx2` logger 未钉死 —— 已查证

`OPENAI_LOG=debug` 会把 `httpx2` logger 也设成 DEBUG,而
`_configure_logging()` 只钉了 `openai`。

**今天无泄漏** —— 已核实 `httpx2/_client.py` 只有两处 `logger.info`,
内容是 method/url/status,**无请求体**。但 pin 有洞。

### 5.5 `_to_image_inputs` 对 `image/*` 的潜在 turn-kill —— reported

`bot.py` 的 `_to_image_inputs` 用 `startswith("image/")`,会放行 `image/*`,
而 `ImageInput` 的 pattern 拒绝它 → `ValidationError` → 该调用**在 `try` 之外**
→ **整个 turn 被杀死,用户什么都收不到**。

**当前不可达**(我们的 downloader 自 `bc5dcb6` 起不再产出通配符),
但若日后 SDK 管线里加了第二个 `InputFileDownloader`,这条就活了。

### 5.6 两张 eval fixture 缺失 —— **已由真机冒烟回答,建议关闭**

`agent/tests/fixtures/README.md` 要求的两张截图未放置,对应用例自动 skip。

**重新评估的理由**:这两条用例唯一独有的价值,是验证我们自己做的
`detail: "auto"` 决定(而非 high)在真实报错截图的小字上够不够用。
而**真机冒烟会顺带回答同一个问题**,用的是本来就要贴的图,不用额外造和维护
fixture。链路本身已由 `test_vision_pipeline.py` 覆盖(变异验证过)。

**2026-09-07 冒烟结果:那种情况没有发生。** 模型对真实报错截图做了**逐字准确**的
OCR(红色横幅全文),并额外注意到了 Agent 下拉框的 "Fetching…" 状态 ——
一个人工看截图时容易漏掉的细节。**`detail: "auto"` 对真实报错截图足够,
不需要改 `high`,也不需要补 fixture。**

两条用例保持 skip 即可。若日后 `detail` 参数或图片预处理有改动,再回来补。

### 5.7 日志正确性无法被单元测试覆盖 —— 已接受的缺口

`bc5dcb6` 的变异 4(删掉新加的两条日志)**存活**。实现者刻意**没有**加
`caplog` 断言,理由是那只能钉住日志**文本**,改个措辞就红,还会让人误以为
日志被覆盖了。**如实记录为已接受的缺口,而非糊一层假防线。**

---

## 6. 环境

### 6.1 Azure OpenAI 间歇性 ConnectTimeout —— 已实证,**部分缓解**

真机冒烟时 `httpcore2.ConnectTimeout` 打到 chat completions 端点,
重试两次都超时,落到 `FALLBACK_MESSAGE`。

**根因有两半,一半是我们的配置错。** 实测该 endpoint 的**成功**连接耗时
(13 次采样,单位秒):

```
7.9  9.5  10.3  9.9  5.2  8.5  7.1  8.8  8.1  2.5  7.9  1.9  6.0
```

中位数约 8s、最大 10.9s,**只有 2 次落在 openai SDK 默认的 `connect=5.0` 之内**。
也就是说 bot 在连接本来能建立的时候就提前放弃了 —— 这部分是配置问题,已修。

**已做的缓解:**

- `connect` 超时 5s → **15s**(覆盖实测最大值并留余量)。`read`/`write`/`pool`
  保持 SDK 默认 600 不动 —— 那是 LLM 生成时间,与连接无关。
- 重试从两层收敛为一层:`core.py` 的 `_MAX_ATTEMPTS` 2 → 1(不重试),
  只保留 openai SDK 的重试(`max_retries=1`,即总 2 次尝试)。否则 2 × 3 = 6 次
  连接 × 15s = 90s,对 Teams 用户不可接受。现在最坏 ~30s。
- 策略集中在 `factory.build_openai_client()`。**注意**:chat client 原先由
  `MAFBackend` 自己 new,不经过 factory —— 只改 factory 会漏掉真正出问题的那个
  client。现在由 factory 注入。

**仍然存在的问题(未解决):** 采样中另有**约 35% 的连接彻底失败**,与超时值无关
—— 15s、60s 都连不上。这是网络本身的问题,不是代码能治的。按这个失败率,
即使配置修好,用户仍会间歇性看到兜底道歉文案。**需要联系网络组**排查 bot 主机到
Azure OpenAI endpoint 的出口路径。在此之前,冒烟"失败一次"不足以判定功能坏了。

**重跑冒烟时注意**:超时会**掩盖图片处理的真实结果**。若又超时,
先确认网络再判断功能。同一台机器上早先跑 vision pipeline 测试是通的,
说明是间歇性,或 bot 进程的出口路径与测试进程不同。

**2026-09-07 复测**:配置修复后冒烟成功,chat completions 连续三次 200 OK
(一次 0.41s 重试后成功)。**缓解有效。** 但 35% 硬失败率仍在。

### 6.2 单轮总耗时 80 秒 —— 已实证

2026-09-07 冒烟:12:13:41 收到消息 → 12:15:01 发出回复,**80 秒**。
主 spec §8.2 的设计预期是 **5-15 秒**。

已知成分:
- `search_solutions` 8.0s、`web_search` 6.1s(含 Tavily 超时 + failover 一次)
- **3 轮 chat completions**,每轮都重发图片(设计如此 —— 图片留在当前轮的
  user message 里,模型才能在 tool loop 中回看)
- 回答约 1500 字,生成本身耗时可观(见 1.4)

**尚未拆解**:没有测量单轮 LLM 调用的实际耗时,所以"主要成分是生成还是网络"
目前是推断而非实证。**要动手优化前先测**,否则又会重演本文档 §7 里那几次
"从不充分样本得出错误假设"。

---

## 7. 零碎

- **Teams 同群用户历史混用** —— 自动化回归已通过,双账号 Teams 验证尚未执行。
  原因是历史键只有 conversation.id,且工具渠道/群聊标记使用进程全局变量。
  现按租户、完整会话/线程、发送者分区,同键排队,工具上下文按请求绑定。
  旧混合历史不继承;内存 TTL、重启失忆和单进程限制保留。
  设计见 [用户隔离设计](superpowers/specs/2026-09-11-teams-user-isolation-design.md),
  人工动作见 [Teams 联调](teams-setup.md)。

- **`_TOOL_SCHEMAS` 值得抽成独立模块** —— `maf_backend.py` 247 行里它占 98 行
  纯数据。建议等下一个需要动 schema 的任务顺手做,不值得为纯移动单开 commit。
  详见实现计划的「遗留观察」小节。
- **`user_usage` 名不副实** —— 返回 seat 分配信息(`last_activity_at`)而非
  消耗量。新的 AI credits 端点支持 `user` 查询参数,能直接查单人真实消耗。
  迁移时需保留 `is_group` 隐私门禁,且个人消费比 seat 活跃度更敏感。

---

## 8. 方法论备忘:被实测推翻过的诊断

本项目中已有多次"看似合理的推断被实测推翻"。记录在此,提醒下次先观测再下结论。

| 诊断 | 结局 |
|---|---|
| 「检索后端死了,模型没 grounding 所以不拿工具」 | **错**。反事实:后端照样坏,配好 channel 后 12 条全绿 |
| 「规则 8 措辞被读成二选一」 | **错**。probe `_dispatch` 直接观测:模型一直在调那个工具,4/4 复现 |
| 「规则 10 保护三处 sink」 | **错**。逐个对代码查:`question_summary` 取自用户输入、`response.markdown` 全仓无 logger 记录。**只有一处成立** |
| 「`httpx.Timeout(10)` 是每请求超时,最坏 40s」 | **错**。是每阶段超时,最坏**无界** |
| 「`billing-escalate-quota` 是既有失败」 | **错**。基线上通过 |
| 「三个源 403 是 GitHub 限流」 | **错**。配额 5000/5000 满的;真因是企业禁止长期 fine-grained PAT |
| 「ingestion 卡住了 / LLM 调用在超时」 | **错**。实测 client 4/5 成功;真相是首批 50 条未满 + 三个源被拒 |
| 「GitHub 422 是限定符占了 161/256 预算」 | **错**。14 字符的检索词照样 422 |
| 「GitHub 422 是检索词超 256 字符」 | **错**。真因是缺 `is:issue`,与长度无关 |
| 「按 `len()` 截到 256 就安全」 | **错**。空格消耗 3 配额,40 词英文 len=230 实耗 308,仍 422 |

**共同教训**:字段名和参数名会误导直觉(`question_summary` 听起来像包含回答;
`Timeout` 听起来像总时长)。**在断言之前先去读代码或挂 probe。**

GitHub 422 那三条尤其值得记:**真因就写在响应体的一行 JSON 里**
(`Query must include 'is:issue'`),而我连续两次都是盯着长度数字做推断,
**没有去取响应体**。取一次的成本远低于两轮错误诊断。

**同一个错误在 403 上又犯了一次**:看到三个源 403,第一反应是"限流",
而真因同样写在响应消息里(企业禁止长期 fine-grained PAT),配额其实是满的。
**"读响应体"这个动作的成本接近于零,而跳过它的代价是整轮错误排查。**

第三条(空格算 3)还说明另一件事:**即使方向对了,凭直觉选的具体数值也可能是错的**。
那条规则是靠 20+ 个真实样本拟合出来的,并用 4 次盲预测验证(4/4 命中)——
不是查文档查到的,文档只说 256 不说怎么算。

另有一类反复出现的测试缺陷,四个已知品种:

1. **假协作者行为太简单** —— 让被测的两条分支产生相同的可观测结果
2. **样本不能判别它声称的那一类** —— 例:声称守护"不用后缀匹配"的样本,
   后缀匹配的实现照样拒绝它
3. **测试数据派生自被测对象** —— 收缩常量只生成更少用例然后全绿
4. **样本维度太单一** —— 例:单张图片下五种不同实现结果相同;
   只喂 PNG 时"正确嗅探"与"无脑返回 png"结果相同

**第五类由真机冒烟暴露**:**所有夹具都是我们自己写的形状,没有一个是外部系统
真实发送的形状**。

这一类已经出现**四次**,每次都是"我们对外部系统的假设错了",每次都被 mock
完美掩盖,每次都是真机/真 API 一碰就露:

| 我们的假设 | 实际 | 被什么发现 |
|---|---|---|
| Teams 附件 contentType 是具体类型 | 是 `image/*` 通配符 | 真机冒烟 |
| GitHub search `state:open` 就够 | 必须含 `is:issue`,否则 **100% 422** | 真 API |
| `copilotfaq` discussions 有采纳答案 | 分类 `isAnswerable=False`,恒为 0 | 真 API |
| `GITHUB_TOKEN` 能访问 `microsoft/*` | 企业禁止长期 fine-grained PAT | 真 API |

**共同点**:单元测试全绿,因为它们验证的是"我们以为对方会怎么回应"。
**对外部契约的假设,只能用真实调用证伪。**
