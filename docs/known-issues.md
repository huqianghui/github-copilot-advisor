# 已知问题清单

> 最后更新:2026-09-07
> 基线:253 passed, 17 deselected

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

**修法**:`uv run --env-file .env python -m ingestion run`

**阻塞**:会消耗 GitHub API 配额、产生 Azure OpenAI embedding 费用、
真的写索引。**属于环境决策,需要人拍板。**

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

### 1.3 GitHub live search 返回 422 —— 已实证,**根因已确诊**

2026-09-07 真机冒烟抓到完整 query,实测:

```
完整 query 长度 : 279 字符
  错误文本部分  : 118
  限定符部分    : 161   (5 个 repo: + state:open)
GitHub 上限     : 256
超出            : 23
```

**根因是结构性的,不是这一次的偶然**:5 个硬编码的 `repo:` 限定符独占
**161 字符,占预算的 63%**,只给实际检索词留下 95 字符。任何超过 95 字符的
查询都会 422。

**且它与 prompt 规则 9 直接冲突**:规则 9 明确要求「把图中读到的错误文本作为
`search_solutions` 的 query 主体」。真实报错文本轻易超过 95 字符
(本次那条 118 字符)。**也就是说,图片输入功能每次正常工作,都会打死
github-live 这一半检索。**

**影响**:`search_solutions` 两半同时死(KB 因 1.1 空索引、github-live 因本条),
模型只能靠 `web_search` 无 grounding 作答。

**修法方向**(未实现):按剩余预算截断检索词最简单且确定 ——
动态算出限定符长度,把检索词截到 `256 - len(qualifiers)`。
拆多次请求(每 repo 一次)代价高;减少 repo 列表会牺牲覆盖面。

### 1.4 群聊回答过长 —— 已实证

同一次冒烟的回答约 1500 字、5 个小节、20 余个要点,而 prompt「回答风格」明确
写着「**群聊中保持简洁:先给结论/方案,细节收进编号步骤**」。

**双重代价**:群聊里刷屏是 UX 问题;同时长回答是本次 80 秒总耗时的主要成分
(见 5.2)。

**注意**:内容本身是对的、可执行的 —— 问题是**篇幅**,不是质量。修的时候
不要把有用的排查步骤删掉,而是压缩表述、把细节收进折叠或引用。

---

## 2. 测试基础设施

### 2.1 没有任何机制阻止单元测试打真实网络 —— 已两次实证

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

## 3. 可观测性:工具记账不一致

`AdvisorEvent.tool_latencies_ms` 是运营判断「哪些工具被调用了、值不值得留」的
**唯一数据来源**(主 spec §10.2)。当前它的语义在不同工具间不一致。

### 3.1 `escalate_to_human` 完全不记账 —— 已实证

`tool_latencies_ms` 在该函数里出现 **0 次**。没有 `start`,没有写入。
它只能通过 `run.stage = "escalated"` 被间接观测 —— 这正是
`billing-escalate-quota` 用 `expected_stage_in` 而非 `expect_tool_called` 的原因。

**按驱动 `copilot_usage_lookup` 修复的同一条逻辑,这个工具在"哪些工具被调用了"
的数据里是完全隐形的。**

### 3.2 三个兄弟工具的异常路径漏记 —— 已查证

`search_solutions`(第 31 行)、`web_search`(第 49 行)、
`network_diagnostics`(第 86 行)都是 `start` 在前、**成功路径**无条件记账,
但**没有 `try/finally`** —— 若它们的 `await` 抛异常,记账行被跳过、异常继续上抛。

对比:`copilot_usage_lookup` 已于 `ff3c782` 改为 `try/finally`。

### 3.3 `copilot_usage_lookup` 的延迟指标现在混淆 —— 已查证

`not_configured` 与 `privacy_blocked` 分支记 ~0ms,真实 API 调用记 ~800ms,
**同一个 key**。「是否被调用」的信号是对的(那是修复的目的),但主 spec §10.2 的
健康视图(延迟 P50/P95)这个 key 单独已经答不了"真实查询有多慢"。

**修法**:3.1 与 3.2 是同一句契约 —— **每个工具无条件记账,包括早返回和异常路径**。
建议直接补进主 spec §10.2 然后实现,三处改动加起来不到 30 行,不值得单开 spec。
3.3 需要设计决策(加第二个字段还是分 key),可以缓。

---

## 4. 图片功能的收尾项

功能已完成并经真机冒烟发现并修复了一个致命 bug(见 §6)。以下是剩余项。

### 4.1 SDK 管线端到端测试缺失 —— 已实证

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

### 4.2 下载没有总时间预算,大小上限事后才判 —— 已查证

`httpx.Timeout(10.0)` 是**每阶段/每次读取**的超时,不是每请求总时长。
慢速滴送的服务器能把一个 turn 拖住**任意久**(最坏情况**无界**,不是
4 × 10s = 40s)。同时 `client.get()` 把整个 body 读进内存后才判大小。

**已验证的成本**(review 时做过原型):`client.stream()` + `aiter_bytes()`
增量截断约 10 行,**现有 respx 测试全部无需修改**即可通过。

**并行化不是答案** —— `asyncio.gather` 只把"无界串行"变成"无界并行"。
正解是**总预算**,本仓 `search/combined.py` 已有该惯用法
(`SEARCH_BUDGET_SECONDS` + `asyncio.wait(timeout=...)`)。

### 4.3 `httpx2` logger 未钉死 —— 已查证

`OPENAI_LOG=debug` 会把 `httpx2` logger 也设成 DEBUG,而
`_configure_logging()` 只钉了 `openai`。

**今天无泄漏** —— 已核实 `httpx2/_client.py` 只有两处 `logger.info`,
内容是 method/url/status,**无请求体**。但 pin 有洞。

### 4.4 `_to_image_inputs` 对 `image/*` 的潜在 turn-kill —— reported

`bot.py` 的 `_to_image_inputs` 用 `startswith("image/")`,会放行 `image/*`,
而 `ImageInput` 的 pattern 拒绝它 → `ValidationError` → 该调用**在 `try` 之外**
→ **整个 turn 被杀死,用户什么都收不到**。

**当前不可达**(我们的 downloader 自 `bc5dcb6` 起不再产出通配符),
但若日后 SDK 管线里加了第二个 `InputFileDownloader`,这条就活了。

### 4.5 两张 eval fixture 缺失 —— **已由真机冒烟回答,建议关闭**

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

### 4.6 日志正确性无法被单元测试覆盖 —— 已接受的缺口

`bc5dcb6` 的变异 4(删掉新加的两条日志)**存活**。实现者刻意**没有**加
`caplog` 断言,理由是那只能钉住日志**文本**,改个措辞就红,还会让人误以为
日志被覆盖了。**如实记录为已接受的缺口,而非糊一层假防线。**

---

## 5. 环境

### 5.1 Azure OpenAI 间歇性 ConnectTimeout —— 已实证,**部分缓解**

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

### 5.2 单轮总耗时 80 秒 —— 已实证

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

## 6. 零碎

- **`_TOOL_SCHEMAS` 值得抽成独立模块** —— `maf_backend.py` 247 行里它占 98 行
  纯数据。建议等下一个需要动 schema 的任务顺手做,不值得为纯移动单开 commit。
  详见实现计划的「遗留观察」小节。
- **`user_usage` 名不副实** —— 返回 seat 分配信息(`last_activity_at`)而非
  消耗量。新的 AI credits 端点支持 `user` 查询参数,能直接查单人真实消耗。
  迁移时需保留 `is_group` 隐私门禁,且个人消费比 seat 活跃度更敏感。

---

## 7. 方法论备忘:被实测推翻过的诊断

本项目中已有多次"看似合理的推断被实测推翻"。记录在此,提醒下次先观测再下结论。

| 诊断 | 结局 |
|---|---|
| 「检索后端死了,模型没 grounding 所以不拿工具」 | **错**。反事实:后端照样坏,配好 channel 后 12 条全绿 |
| 「规则 8 措辞被读成二选一」 | **错**。probe `_dispatch` 直接观测:模型一直在调那个工具,4/4 复现 |
| 「规则 10 保护三处 sink」 | **错**。逐个对代码查:`question_summary` 取自用户输入、`response.markdown` 全仓无 logger 记录。**只有一处成立** |
| 「`httpx.Timeout(10)` 是每请求超时,最坏 40s」 | **错**。是每阶段超时,最坏**无界** |
| 「`billing-escalate-quota` 是既有失败」 | **错**。基线上通过 |

**共同教训**:字段名和参数名会误导直觉(`question_summary` 听起来像包含回答;
`Timeout` 听起来像总时长)。**在断言之前先去读代码或挂 probe。**

另有一类反复出现的测试缺陷,四个已知品种:

1. **假协作者行为太简单** —— 让被测的两条分支产生相同的可观测结果
2. **样本不能判别它声称的那一类** —— 例:声称守护"不用后缀匹配"的样本,
   后缀匹配的实现照样拒绝它
3. **测试数据派生自被测对象** —— 收缩常量只生成更少用例然后全绿
4. **样本维度太单一** —— 例:单张图片下五种不同实现结果相同;
   只喂 PNG 时"正确嗅探"与"无脑返回 png"结果相同

**第五类由真机冒烟暴露**:**所有夹具都是我们自己写的形状,没有一个是外部系统
真实发送的形状**。Teams 发的是 `image/*`(字面通配符),而我们所有测试都用
具体类型 —— 250 个单元测试、两轮深度审查、16 个变异体全部漏掉,
一次真机冒烟一次抓到。
