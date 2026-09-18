# Development

This repo is a `uv` workspace with four members: `shared`, `ingestion`,
`agent` and `channels/teams`.

## Setup

```bash
uv sync --all-packages
```

Plain `uv sync` only installs the root project — it will **not** install the
workspace member packages into the venv, so their imports
(`advisor_shared.*`, `ingestion.*`, `advisor_agent.*`, `teams_adapter.*`)
will fail. Always use `--all-packages`.

## Corporate networks

If PyPI is blocked or slow on your network, set `UV_INDEX_URL` to point at
your corporate PyPI proxy before running `uv sync`.

## 历史保存与模型上下文

默认按每条消息写入时间保存 30 天,达到 30 天即不再返回;新活动不延长旧消息寿命。
先按租户/会话线程/发送者定位分区,再选择该分区最近 20 条有效历史给模型。
20 条是历史消息数量,不是 20 轮问答,也不包含系统提示、当前问题或当前工具结果。
较早的有效历史不因模型窗口限制被删除,但本次不自动摘要或检索这些历史。

`AdvisorCore` 对成功问答一次提交用户/助手两条消息,失败不提交半轮历史。
可传入 `turn_id` 为存储提交提供幂等标识;这不代表直接模式会跳过重复的模型调用,
也不代表已经实现跨实例队列或 exactly-once 发送。
本地默认仍为内存存储,重启会丢失数据,不能据此承诺跨重启保留 30 天。

云端状态实现中的 `BufferedSessionStore` 只捕获单轮待提交的消息,不直接写入历史;
`AdvisorComponents` 复用重客户端,但为每轮创建独立 Core 和历史缓冲区。
`BlobHistoryStore` 使用不可变问答归档、最多 10 个问答引用的上下文索引和提交回执,
以区分“归档已写”与“索引已提交”,并修复索引提交后回执写入失败的情况。
索引写入必须携带目标索引 Blob 的租约和 ETag;缺失/损坏的有效归档明确报错,
不会伪装成正常空历史。异步读取返回前再次过滤到期消息,读取失败时清理其余并行读取。

这些状态接口尚未接入默认 Teams 启动入口。当前离线故障注入测试不是实际 Azure Blob
租约或云端部署通过的证据;存储集成验证和接线进度见云部署计划。

## 分级日志与延迟分析

Teams 入口使用标准 Python `logging`,不需要额外日志依赖或云资源。
默认 `ADVISOR_LOG_LEVEL=INFO`、`ADVISOR_LOG_FORMAT=json`。
无现成控制台 handler 时输出到 stdout;已有控制台 handler 保留原来的输出流,
不会移除或关闭宿主的文件/APM handler。配置多次不会重复添加控制台 handler。

本地联调可在 `.env` 中设置,或使用 PowerShell:

```powershell
$env:ADVISOR_LOG_LEVEL = "DEBUG"
$env:ADVISOR_LOG_FORMAT = "text"
uv run --env-file .env python -m teams_adapter
```

| 级别 | 内容 |
|---|---|
| `DEBUG` | `step_started`、LLM HTTP 尝试/阶段诊断和单次 typing 发送耗时 |
| `INFO` | `step_completed`、`advisor_event` 和 `typing_summary`;正常空结果也是 INFO |
| `WARNING` | 超时、取消、降级、依赖未配置 |
| `ERROR` | 步骤失败;即使上层随后兜底,失败步骤也保留 |

`ADVISOR_LOG_LEVEL` 控制本应用的日志,不会把 OpenAI/Azure/HTTP SDK 一起调成
DEBUG。不要使用 `OPENAI_LOG=debug` 排查延迟,它可能输出请求正文和图片。
无效级别或格式在启动时明确报错,不会静默退回默认配置。

每次请求有新的随机 `trace_id`,每个步骤有独立的 `span_id`、
`parent_span_id`、UTC `started_at`、`start_offset_ms`、`duration_ms`、
`status` 和安全的 `attributes`。耗时及偏移来自单调时钟,保留到毫秒小数点后三位,
不是根据日志打印时间相减。按同一 `trace_id` 收集完成事件,再按
`start_offset_ms` 排序即可还原时间线;同名步骤重复调用不会覆盖。
这些是本地计时记录,尚未实现 OpenTelemetry exporter 或跨服务 trace 传播。

重要步骤覆盖:

- `app.startup`:构建应用和客户端;**不等于** Azure 平台冷启动时间。
- `teams.http`:消息 HTTP 处理(包含 SDK/认证、下载和 handler);健康检查不记步骤。
- `teams.downloads` / `teams.download_token` / `teams.download_request`:
  附件管线、取 token、逐次下载;仅记录数量、字节数和 HTTP 状态,不记录 token/URL。
- `teams.message` / `teams.render` / `teams.send`:
  消息 handler、渲染、发送回复。Typing 已移出串行关键路径,见下方说明。
- `agent.turn`:包含会话锁排队的核心处理总时长;其子步骤包括
  `session.queue`、`agent.plan`、`session.read`、`agent.backend`、
  `answer.postprocess`、`answer.evaluate`、`session.write`。
- `llm.prepare_messages`、逐轮 `llm.completion`、必要时的 `llm.vision_fallback`。
  completion 记录轮次、工具调用数及 SDK 提供的输入/输出 token 数。
- `tool.*`:每次工具调用;RAG 检索的细分口径见
  [检索计时说明](docs/search-index-setup.md#细粒度检索计时)。

运行摘要不再记录问题摘要、原始会话标识或原始异常消息;步骤日志不记录问题、
答案、检索文档、向量、图片或完整请求 URL。错误保留异常类型,检索分支还保留
安全的 HTTP 状态码和服务错误码。**行为评估报告是独立产物**,仍保留原有
问题/回复/异常字段,不因此变成可公开分享的数据;分享前仍需检查。

`AdvisorEvent` 新增 `trace_id` 和 `timings`,既有字段继续可读。`timings` 是
Agent 返回前的已完成步骤快照,包含完整的 `agent.turn`;之后的 Teams 渲染、
发送和 HTTP 结束记录在控制台日志中,不要把 Agent 快照当作完整的渠道端到端耗时。
规划/评估异常或请求取消时,已开始的步骤仍会记录终态并向上抛出原异常;
这些路径不保证生成原先仅在核心正常返回时产生的 `AdvisorEvent`。

要定位延迟,先按步骤分布比较多次请求,
区分首次连接、后续请求、成功、空结果、超时及模型轮次,不要用单次请求下结论。
LLM/embedding 的 INFO 步骤包含 SDK 内部重试和退避;DEBUG 可展开 HTTP 尝试及
实际可观察的传输阶段,但不提供独立 DNS、服务端排队/推理或首 token 时间。

以后部署到 Container Apps 可收集控制台日志;需要跨服务调用图、集中指标和告警时,
再接 OpenTelemetry / Application Insights。Functions 需要结合宿主日志级别、
遥测关联及采样配置接入,不是简单增加一个日志库即可完成。云端采集、保留期、
采样和资源部署都不在本次改造内。

### KB/GitHub 检索预算

`search_solutions` 同时启动 KB 和 GitHub live,分别执行有界等待:

| 环境变量 | 默认值(秒) | 含义 |
|---|---|---|
| `KB_SEARCH_BUDGET_SECONDS` | 12 | KB 全链路:问题 embedding + Azure Search 分页取数 + 本地过滤 |
| `GITHUB_SEARCH_BUDGET_SECONDS` | 4 | GitHub 分支;实际取该值与 KB 预算的较小值 |

两个值均要求有限且大于 0,允许小数;无效值在构建客户端前明确报错。
修改配置后需重启应用。GitHub 超时不会取消仍在预算内的 KB;
KB 提前完成仍会等 GitHub 完成或耗尽自己的预算。两边提前结束即返回,
总体等待上限约为 KB 预算,不是两者相加;协作式取消清理、序列化和合并另计。
这不是硬实时保证:阻塞事件循环或拒绝响应取消的依赖仍可能超出预算。

合并顺序、URL 去重、相关性阈值与工具输出形状不变。失败分支仍记录
`timeout` / `error`,正常无命中记录 `empty`;外部请求取消记录 `cancelled`
并清理子任务。`no_results=true` 仍可能表示没有完成的可用分支,
不能据此认定知识库不存在相关文档。Chat/Web 的超时与 LLM 工具轮数不变。

校准依据(2026-09-17,本地网络,非生产 SLA):
三次 Teams 日志中 KB 两次在旧 8 秒预算处取消,唯一成功耗时 7.734 秒。
另用 4 个公开通用问题做 16 次只读检索,测量预算暂放到 30 秒以减少截断:

| 客户端使用方式 | 样本数 | KB 中位数 / 最大值(秒) |
|---|---|---|
| 每次新建客户端 | 4 | 7.796 / 10.844 |
| 复用组的首次调用 | 1 | 7.578 / 7.578 |
| 连续复用连接 | 7 | 1.328 / 1.688 |
| 复用客户端,调用间隔 7 秒 | 4 | 5.016 / 5.531 |

这批 GitHub 最慢 2.984 秒;因此先采用 KB 12 秒、GitHub 4 秒并保留配置入口。
间隔组的 4 次 embedding 均重新建立 TCP/TLS,而连续复用组没有新连接;
Azure Search 在间隔组仍约 0.5-0.7 秒。连接建立开销不可忽略,
但本次不改变连接池/重试策略。样本量较小且查询不是原始截图的精确重放,
不能据此承诺 P95 或回答质量;完成查询也不等于有相关命中。
本地批测脚本、原始结果和新增回归用例均保存在仓库外,不写入 Git。

采用 12/4 秒预算后另做 16 次相同问题集复测:KB 完成 15 次,
其中 4 次耗时超过旧 8 秒预算;GitHub 16 次全部在 4 秒内完成。
仍有 1 次冷调用超时,该次 embedding 8.781 秒,KB 含超时处理实际为 12.828 秒。
因此 12 秒是当前延迟与检索完成率的折中,不是消除长尾的保证;
两批网络条件不同,不能把耗时差直接当成加速比例,也未验证 Teams 端到端或 LLM 轮次减少。

### DEBUG-only LLM 长尾诊断

在 `.env` 设置 `ADVISOR_LOG_LEVEL=DEBUG` 并重启应用即可开启,不需要额外开关、
日志依赖或云资源。`INFO` 下不会创建详细调用状态或附加诊断 transport trace,
原有每轮耗时与输入/输出 token 日志保持不变。开关不改变模型请求、非流式调用、
超时或 SDK 重试策略;不要用 `OPENAI_LOG=debug` 替代它。

诊断 logger 是 `advisor_agent.llm_diagnostics`。每条记录包含已有的
`trace_id` / `span_id`,以及一次 SDK 调用独有的 `call_id` 和
`operation=chat.completions` / `embeddings`。新增计时使用 `perf_counter()`,
避免当前 Windows/Python 3.12 的低分辨率 `monotonic()` 影响细分阶段分析。
原有 INFO 步骤计时口径没有更改。

| DEBUG 事件 | 用途 |
|---|---|
| `llm.http.attempt_started` | 一次 HTTP 尝试开始;`attempt` 从 1 起,`sdk_retry_count` 从 0 起 |
| `llm.http.response_headers` | 收到响应头;记录状态码、`headers_elapsed_ms` 和安全头字段 |
| `llm.http.phase` | transport 的连接/TLS、发送请求头/体、接收响应头/体的开始、完成或失败事件 |
| `llm.http.attempt_completed` | HTTP client `send` 返回或失败;非流式时包括响应体读取 |
| `llm.sdk_call_completed` | SDK 调用结束,包含重试、响应处理/解析;记录总耗时、结果与可用 token 明细 |

`attempt_completed` 中的 `phase_durations_ms` 仅汇总观察到完整起止事件的阶段;
缺少的阶段不填 0。异常、取消和非 2xx 结果也会记录。HTTP 重定向可能增加
`attempt`,但不增加 SDK 重试序号;重定向的 `end_boundary=next_request_hook`,
普通尝试为 `http_client_send`。

`inter_attempt_gap_ms` 是上一次 HTTP client send 结束到下一次请求 hook 的间隔,
包括 SDK 判断、准备、退避等,**不能等同于纯退避时间**。响应头耗时不是完整响应耗时,
也不是首 token 时间。等待响应头的长尾可能来自网络、网关、排队或推理,
客户端 trace 不能进一步替服务端归因。连接复用时没有新的 connect/TLS 事件是正常的。

安全字段只允许:

- `request_ids`:格式受限的 `x-request-id`、`apim-request-id`、`x-ms-request-id`、
  `request-id`;不是任意可打印字符串。
- `numeric_headers`:范围受限的数字型 Retry-After、限流 limit/remaining/reset。
  HTTP 日期、`1m30s` 等非纯数字格式及非白名单响应头不记录。
- `usage`:SDK 实际提供的 input/output/cached-input/reasoning token 数。
  不可用信息保持缺失或 `null`,不编造 0;错误只保留受限的异常类型。

问题、答案、图片、向量、认证头、完整 URL、原始异常、原始 trace info 或任意响应头
都不会进入这些诊断事件。诊断仅作为 DEBUG 日志输出,不会加入 INFO 的 `AdvisorEvent`
报告字段。对长尾先用 `trace_id + span_id + call_id` 定位具体轮次,再看尝试次数、
各阶段和尝试间隔,不要将重试等待或接收响应头的时间直接命名为“模型思考时间”。

### 非阻塞 typing

Typing 在已通过入口认证、满足回复触发条件且身份有效的消息进入 adapter
middleware 时启动,覆盖 SDK 附件下载、会话排队、RAG 和 LLM。不是在下载完成后
才开始提示。没有 @ Bot 的群聊、非消息活动、身份无效及无内容消息不会启动提示。

独立异步任务会立即安排首次发送,随后每 3 秒尝试续发;每次发送最多等待 2 秒,
不堆叠并行 typing 请求。主流程不等待首条 typing,最终回复也不等待在途 typing。
发出正式消息前同步取消续发任务,处理退出时收尾回收任务;不会把后台任务永久遗留。
超时会告警并等待下一周期;其它发送错误安全告警后停止提示,不会拖垮回答。
SDK 全局 `start_typing_timer` 保持关闭,避免双重定时器和对不应回复的消息发提示。

日志:

- `typing_send_completed` (`DEBUG`):单次成功发送的耗时和序号。
- `typing_send_timeout` / `typing_send_failed` (`WARNING`):超时或异常类型,不含原始异常消息。
- `typing_summary` (`INFO`):本回合尝试、成功、超时次数及错误类型;`parallel=true`。

旧日志中的串行 `teams.typing` 步骤不再出现。不要把 typing 的后台耗时加到 Agent
或 HTTP 总耗时上。这里保证的是主处理/最终回复不等待 typing 的网络往返,不代表
零 CPU/网络开销或客户端一定持续显示。事件循环被同步代码占用时提示仍可能延迟;
已发往 Teams 的活动也无法撤回。取消后的资源清理发生在回复之后,SDK/传输若延迟
响应取消,HTTP 请求收尾可能仍稍晚于用户收到回答的时间。

## 单次逻辑 Web 检索

模型的 `web_search` 工具只接受 `query`,每个问答回合仅执行一次逻辑检索。
内部同时启动 trusted/general 两个范围,每个范围按 Tavily → Brave 的顺序
使用已配置的 provider;没有可用结果或遇到异常/超时时才切换下一个。
同一 provider 在每个范围最多尝试一次,不盲目重试。两个范围结果合并、按 URL
去重并优先呈现可信来源;每个范围默认请求 5 条,不是用条数替代答案充分性判断。

| 配置 | 默认值 | 含义 |
|---|---:|---|
| `WEB_SEARCH_BUDGET_SECONDS` | 12 | 两个范围共享的总等待预算 |
| `WEB_SEARCH_ATTEMPT_TIMEOUT_SECONDS` | 6 | 每个 provider 尝试上限,不超过剩余总预算 |

配置必须是有限正数,非法值在启动时失败。这些设置独立于 OpenAI chat/embedding
的连接超时和重试。预算从两个分支启动前开始计算;客户端初始化、结果合并及取消清理
可能增加少量额外耗时。一个分支超时不会丢弃另一个分支已获得的结果。
同一次逻辑检索共享 HTTP 客户端和连接池,结束/异常/取消时关闭;不是跨回合常驻连接池。

工具返回 `status`、`no_results`、`retry_allowed=false`、各 `scopes` 的状态与
provider 尝试摘要及候选 `results`:

- `success`:获得候选,两个范围均有正常结果或正常空结果;仍由模型判断内容充分性。
- `partial`:有候选,但至少一个范围因超时/错误未完整完成。
- `empty`:正常完成,没有可用候选;此时 `no_results=true`。
- `timeout` / `error` / `not_configured`:没有候选且检索受限,`no_results=null`,
  不应表述成“网上没有资料”。有候选时 `no_results=false`。
- `already_attempted`:此前的逻辑检索被中断,本回合不再重新出网。

完成后,Web 工具从后续 LLM 请求的工具列表移除。额外/并发调用由回合内的锁与缓存
去重,换查询词也不重新检索;正常空结果和失败结果同样缓存。中断异常原样传播,
即使上层捕获后继续执行,本回合也不能重启 Web。下一条用户消息使用新的回合状态。

用 `search.web.retrieve` 统计逻辑检索次数,用 `search.web` / `search_attempts`
统计外部 provider 尝试,二者不能混用。仅配置 Tavily 时通常有两个并行请求;
再配置 Brave 时最多四个请求(每个范围各两个),仍受同一个总预算限制。
`failover_count` 保留原口径:失败或无可用结果的尝试数,不等于实际切换 provider 次数。
旧的底层 `WebSearchChain.search(..., scope=...)` 仍供单范围测试/调用使用,
但不再暴露给模型;Agent 始终使用 `retrieve()`。

## 图片输入的容量影响

单个满配图片请求(4 张 × 4MB,limits 见 `channels/teams/src/teams_adapter/downloader.py`)
在飞行中同时驻留:

| 部分 | 峰值 |
|------|------|
| 原始字节(`request.images`,core.handle 栈帧持有) | ~16 MB |
| base64 字符串(`messages` 里的 data URL) | ~21 MB |
| openai SDK 序列化的 JSON body(`httpx.Request.content`) | ~21 MB |
| **合计** | **~58 MB / 并发请求** |

aiohttp 单进程多并发,该数字乘并发数。据此设定 Teams 侧并发上限与容器内存。

注:`AdvisorCore` 的重试是**串行**的,上一次 `run` 的栈帧已退出,**不叠加**内存;
重试代价是 CPU(约 16MB 的 base64 重编码,一二十毫秒)。

已知未闭合项(见实现计划的「后续任务」小节):`DOWNLOAD_TIMEOUT_S` 是
**每阶段**超时而非总时长,慢速滴送的服务器能把一个 turn 拖住任意久;
且大小上限是**读完整个 body 之后**才判的。两者的修法是同一处改动
(流式截断 + 下载总预算),已记录待办。
