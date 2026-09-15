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
| `DEBUG` | `step_started`,可看到长时间未结束的步骤 |
| `INFO` | `step_completed` 和 `advisor_event` 整轮摘要;正常空结果也是 INFO |
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
- `teams.message` / `teams.typing` / `teams.render` / `teams.send`:
  消息 handler、typing、渲染、发送回复。
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

当前不改变检索策略、超时或 SDK 重试。要定位延迟,先按步骤分布比较多次请求,
区分首次连接、后续请求、成功、空结果、超时及模型轮次,不要用单次请求下结论。
LLM/embedding 步骤包含 SDK 内部重试和退避;当前不单独采集每次 HTTP 重试、
DNS/TLS 或首 token 时间(现有调用不是流式)。

以后部署到 Container Apps 可收集控制台日志;需要跨服务调用图、集中指标和告警时,
再接 OpenTelemetry / Application Insights。Functions 需要结合宿主日志级别、
遥测关联及采样配置接入,不是简单增加一个日志库即可完成。云端采集、保留期、
采样和资源部署都不在本次改造内。

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
