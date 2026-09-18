# Cloud Deployment Experiments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留本地开发方式,交付两套可靠、低成本的 Teams 云部署及可复现的时延/并发实验报告。

**Architecture:** ACA 与 Flex 共享轻量接收、持久任务、按用户分区的串行 worker 和既有 Agent。
Blob 保存任务/历史并提供服务器端租约约束,Queue 传递会话唤醒通知。部署脚本和实验工具放在
`deploy/`,两套环境复用同一模型与 Search,不复用任务状态。

**Tech Stack:** Python 3.12、uv、pytest/pytest-asyncio、Pydantic 2、现有 OpenAI/Search SDK、
Microsoft 365 Agents SDK 1.5、Azure Identity/Blob/Queue SDK、Functions Python v2、Bicep、PowerShell、
Docker。不增加 Redis、Service Bus、历史摘要或新的 Agent 框架。

## Global Constraints

- 设计依据:[已批准设计](../specs/2026-09-17-cloud-deployment-experiments-design.md),四部分及成文审阅均已获确认。
- 消息按写入时间保存 30 天;模型仅读取该用户会话最近 20 条历史消息。
- 分区边界为租户 ID + 完整会话/线程 ID + 发送者 ID;不能先取整个群最近 20 条再过滤用户。
- 两套实验应用与状态存储均使用 Southeast Asia。
- 资源组为 `rg-copilot-advisor-aca-exp`、`rg-copilot-advisor-flex-exp`。
- 订阅为 `adca540e-984c-4cab-bc86-5d38bf979841`;操作前核对,不得自动切换到其他订阅。
- ACA 起始配置为 0.25 vCPU/0.5 GiB;Flex 起始内存 512 MB,Always Ready 为 0。
- 不稳定时升级并重新采样,不能将不同规格结果混在同一统计组。
- Azure 依赖显式选择 Key/Entra,主实验使用托管身份,Entra 失败不自动退回 Key。
- Teams 保留 client secret/托管身份;GitHub 保留 Token,Tavily/Brave 保留 Key。
- 不修改用户 `.env`,不提交凭据、原始聊天、附件、任意异常正文或完整带凭据 URL。
- 不改变既有模型、知识库、回答策略、检索预算和本地 Bot 消息地址。
- 保留原本地启动命令;原 1 小时 TTL 改为已确认的 30 天规则属于本次有意行为变更。
- 不将 HTTP 202、健康检查或 IaC 成功当作完整问答通过。
- 不承诺严格 exactly-once 发送或平台绝对并发上限。
- 用户不设置实验费用上限;运行仍使用设计规定的有限采样和停止条件。
- 每次执行 Python 前选择/核对工作区解释器;不要另建与编辑器不一致的环境。
- 优先用编辑器测试工具执行所列测试文件;无测试提供器时使用本计划中的 uv 命令。
- 依赖变更后才同步依赖;不以本任务为由升级现有 OpenAI、MAF、Search 等无关锁定版本。
- 执行工具当前配置为 `docker`、`docker compose`;实际执行前重新核对容器工具配置。
- 每个任务红测、最小实现、绿测后单独提交;只暂存该任务文件,保留其他人的变更。
- 所有提交包含 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`。
- 本计划包含后续执行代码/命令,不表示这些操作已经运行。

---

## 0. 已查证事实与执行门槛

### 执行记录(2026-09-18)

| 任务 | 当前证据 |
|---|---|
| 1 | `cefb657`:认证/DTO 与 factory,224 项针对测试通过 |
| 2 | `780baf5`:本地 30 天保留、20 条窗口及原子提交,106 项针对测试通过 |
| 3 | Blob/租约实现进行中;Docker 引擎未运行,独立 Azurite 启动也受阻 |
| 4 | 缓冲区、组件复用、持久历史算法及故障恢复离线测试通过;实际网关/存储集成未完成 |
| 13 | 仅离线 IaC 实现/编译进行中,没有创建云资源 |

已有离线回归(排除正在开发的 `agent/tests/cloud`)为 683 passed、19 integration deselected,
附带 19 条原有 aiohttp AppKey/RequestKey 提示。后续持久历史及相关针对测试为 101 passed。
条件对象测试实现不能替代实际 Azure Storage/Azurite 的租约契约测试。
尚未执行实际云部署、模型数据面冒烟、真实 Teams 验收或性能实验。

### 0.1 依赖及宿主事实

- 当前工作区 Python 为 3.12.10,两种云宿主均选 Python 3.12。
- 当前 `uv.lock` 的 Agents hosting/core 为 1.5.0;其公共接口包括:
  `JwtTokenValidator(configuration).validate_token(token)`、
  `HttpAdapterBase.process_request(request, agent)`、
  `ChannelServiceAdapter.continue_conversation(agent_app_id, continuation_activity, callback)`。
- `continue_conversation` 接收的是保留原字段的 `Activity`,不是另一版本 SDK 的
  `ConversationReference` 参数签名。不要照搬其他语言/旧 Bot Framework 的调用。
- 新旧 embedding 均为 `text-embedding-3-large` 版本 1;实际索引为 3072 维。
  配置相容性已查证,真实数据面冒烟未执行。
- Functions 工具已提供 `http-trigger-python-azd` 基线,支持显式 `VNET_ENABLED=false`。
  工具未提供 Storage Queue 模板;Queue Trigger 必须采用官方 Python v2 绑定示例,
  不擅自替换为 Service Bus 或引入另一套 Agent 模板。
- 模板中的 Functions AVM 为 `br/public:avm/res/web/site:0.15.1`。
  执行时通过官方工具重取/核对模板,保留身份、部署存储及必要 RBAC,不复制无关 demo。

### 0.2 当前配额证据

| 检查 | 已观察结果 | 能得出的结论 |
|---|---|---|
| ACA 环境数量 | Southeast Asia 使用 0,限额 50 | 新增 1 个环境在数量限额内 |
| Storage 账户数量 | 使用 4,限额 250 | 新增 2 个账户后为 6 |
| Microsoft.Web 通用额度 | 仅返回 Total Regional VMs,且 `isQuotaApplicable=false` | 不能据此确认 Flex 内存额度 |
| ACA 环境 vCPU | 新实验环境尚未创建 | 创建基础环境后核对,不足时不执行升压 |

规划过程中,quota CLI 缺失导致只读查询失败后已安装该扩展。
没有注册新资源提供程序、申请额度或创建云资源。
部署任务必须重新检查时效性、Flex 专用额度/部署准入、RBAC 和策略;未验证不能标为通过。

### 0.3 镜像仓库选择

发现同区域已有仓库,但用户暂时无法确认复用授权。
采用已批准的回退方案:在 ACA 实验组中新建 Basic ACR。
不修改/删除任何现有仓库,固定费用单列。以后获得明确授权才可改用现有仓库。

## 1. 文件与职责地图

以下是目标文件;不是要求在任务开始时一次性创建所有空文件。

| 文件/目录 | 职责 |
|---|---|
| `shared/src/advisor_shared/cloud.py` | 跨宿主任务、历史、结果及遥测 DTO |
| `shared/src/advisor_shared/azure_auth.py` | 显式认证配置与可关闭的 Azure 凭据 |
| `shared/src/advisor_shared/entra_api.py` | 实验 API/接收器复用的专用 Entra audience/角色验证 |
| `shared/tests/test_cloud_contracts.py`, `test_azure_auth.py` | DTO/认证测试 |
| `agent/src/advisor_agent/sessions.py` | 内存过期、最近 20 条与原子问答提交协议 |
| `agent/src/advisor_agent/factory.py` | 复用重客户端,按会话存储创建轻量 Core |
| `agent/src/advisor_agent/durable/blob.py` | Blob 数据面读写、条件提交及错误分类 |
| `agent/src/advisor_agent/durable/leases.py` | 可续期租约及失租取消 |
| `agent/src/advisor_agent/durable/history.py` | 不可变归档、小型上下文索引、幂等提交 |
| `agent/src/advisor_agent/durable/jobs.py` | 任务状态、会话收件箱与受理序号 |
| `agent/src/advisor_agent/durable/queue.py` | UTF-8 通知、outbox、租约可见性 |
| `agent/src/advisor_agent/durable/worker.py` | 顺序处理、阶段恢复及发送不确定性 |
| `agent/src/advisor_agent/durable/maintenance.py` | 有界 outbox 修复、毒消息核对与过期清理 |
| `agent/tests/cloud/` | 共享假实现、故障注入、Azurite/真实存储契约测试 |
| `channels/teams/src/teams_adapter/processing.py` | 从现有 handler 提取可复用的回复准备 |
| `channels/teams/src/teams_adapter/cloud/auth.py` | 公共 SDK JWT 验证封装 |
| `channels/teams/src/teams_adapter/cloud/ingress.py` | 轻量受理,不初始化模型或下载图片 |
| `channels/teams/src/teams_adapter/cloud/executor.py` | SDK 主动会话、准备结果与最终发送 |
| `channels/teams/src/teams_adapter/cloud/runtime.py` | 进程级资源与懒加载 worker |
| `channels/teams/src/teams_adapter/cloud/functions.py` | Functions 请求适配与结果转换 |
| `channels/teams/tests/cloud/` | 云入口、主动会话、并发隔离与冷导入测试 |
| `deploy/functions/function_app.py`, `host.json`, `.funcignore` | 原生 Functions 绑定入口 |
| `deploy/functions/infra/`, `deploy/aca/infra/`, `deploy/shared/infra/` | 两套 IaC 及公共模块 |
| `deploy/aca/Dockerfile`, `deploy/aca/Dockerfile.dockerignore` | 仅在线服务的镜像 |
| `deploy/local/compose.yaml` | 具名 Azurite 测试服务 |
| `deploy/tools/` | 新 uv 成员 `advisor-deploy`:配置、包校验、负载/结果统计 |
| `deploy/bench/` | 非敏感固定用例、驱动容器、运行清单 |
| `deploy/shared/`, `deploy/aca/`, `deploy/functions/` 中的 `.ps1` | 校验、打包、部署、模式切换 |
| `deploy/reports/` | 时延、并发、成本结论文档 |
| `deploy/out/` | Git 忽略的包、临时参数和原始结果 |

不要修改 `.venv` 中的 SDK。`shared` 新增 Azure 依赖只在认证模块导入,
消息/遥测模块不因导入契约而初始化 Azure 客户端。

## 2. 任务顺序与提交规则

里程碑:

1. Tasks 1–7:认证、状态与可靠 worker,可通过本地测试验证。
2. Tasks 8–14:薄宿主、打包与部署脚本,通过模拟/本地校验后才能创建云资源。
3. Tasks 15–18:负载工具、云验证、实际实验、报告及收尾。

依赖顺序为 `1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14 → 15 → 16 → 17 → 18`。
这是默认安全执行顺序,不要求为了并行而拆出相互重叠的修改。

每个任务的提交步骤只提交列出的实际修改文件。提交消息位于任务末尾,
使用完整 trailer,不得提交 `deploy/out/`、真实运行配置、`.env` 或未脱敏结果。

## Task 1: 明确认证配置与跨宿主 DTO

**Files**
- Create: `shared/src/advisor_shared/cloud.py`, `shared/src/advisor_shared/azure_auth.py`
- Create: `shared/tests/test_cloud_contracts.py`, `shared/tests/test_azure_auth.py`
- Modify: `shared/pyproject.toml`, `agent/src/advisor_agent/factory.py`, `.env.example`, `uv.lock`

**Interfaces**
- `HistoryMessage(role, content, written_at, turn_id, sequence)`:role 为 user/assistant,
  written_at 是带时区 UTC,sequence 是该分区受理序号。
- `TaskPayload(job_id, partition_key, activity, accepted_at, trace_id, target, run_id, request_id)`:
  target 为 teams/benchmark,activity 是 JSON 值字典且不参与 repr。
  run_id/request_id 仅实验任务需要;正式 Teams 任务由 activity ID关联。
- `QueueWake(partition_key, trace_id, version=1)`:只含通知所需字段。
- `PreparedTurn(reply, history, outcome)`:reply 为 Activity JSON 或 None;
  history 是 0 或 2 条 `{role, content}`;outcome 为 success/degraded/ignored。
- `AzureAuthSettings.from_env(env: Mapping[str, str]) -> AzureAuthSettings`。
- `AzureCredentials(settings)` 提供 `openai_auth()`, `search_credential()`,
  `storage_credential()` 和 `async aclose()`;Entra 凭据进程内复用。

- [ ] **Step 1: 写 DTO/认证红测。** 放入各自测试文件:

```python
import pytest
from advisor_shared.azure_auth import AzureAuthSettings
from advisor_shared.cloud import QueueWake

def test_invalid_auth_mode_is_an_error():
    with pytest.raises(ValueError, match="AZURE_OPENAI_AUTH_MODE"):
        AzureAuthSettings.from_env({"AZURE_OPENAI_AUTH_MODE": "automatic"})

def test_managed_identity_requires_explicit_client_id():
    with pytest.raises(ValueError, match="AZURE_CLIENT_ID"):
        AzureAuthSettings.from_env({
            "AZURE_OPENAI_AUTH_MODE": "entra",
            "ADVISOR_ENTRA_CREDENTIAL": "managed_identity",
        })

def test_queue_message_never_accepts_activity_or_bearer():
    with pytest.raises(ValueError):
        QueueWake.model_validate({
            "partition_key": "partition-a", "trace_id": "trace-a",
            "activity": {"text": "private"}, "authorization": "private",
        })
```

另写参数化测试:三种 Azure 依赖各自 key/entra、空 Key、无效 URL、CLI/MI 来源、
环境存在旧 Key 但 Entra token 获取失败、重复关闭客户端、DTO UTC/extra 字段校验。
Entra 失败测试断言没有产生任何使用 Key 的请求,不是只检查配置枚举。

- [ ] **Step 2: 运行并确认因新接口不存在而失败。**
  `uv run --no-sync pytest shared\tests\test_cloud_contracts.py shared\tests\test_azure_auth.py -q`

- [ ] **Step 3: 加依赖并实现显式分支。**

```powershell
uv add --package advisor-shared "azure-identity>=1.19,<2" "azure-core>=1.30,<2"
```

DTO 使用 Pydantic 的 `extra="forbid"`、`hide_input_in_errors=True`、
`AwareDatetime` 和 `JsonValue`。禁止把 Authorization 放入 TaskPayload。
认证环境变量固定为:

| 变量 | 默认/要求 |
|---|---|
| `AZURE_OPENAI_AUTH_MODE`, `AZURE_SEARCH_AUTH_MODE` | key,保持现有配置可用 |
| `ADVISOR_STORAGE_AUTH_MODE` | entra;本地 memory 模式不要求配置 Storage |
| `ADVISOR_ENTRA_CREDENTIAL` | azure_cli 或 managed_identity |
| `AZURE_CLIENT_ID` | managed_identity 必填 |
| `ADVISOR_STORAGE_ACCOUNT_NAME`, `ADVISOR_STORAGE_ACCOUNT_KEY` | Storage key 模式需要 |
| `ADVISOR_BLOB_SERVICE_URL`, `ADVISOR_QUEUE_SERVICE_URL` | 云端必须 HTTPS |

token provider 按官方异步接口构造,不改变现有 OpenAI API 版本、超时和重试:

```python
from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
from azure.identity.aio import get_bearer_token_provider

def make_entra_credential(source: str, client_id: str | None):
    if source == "azure_cli":
        return AzureCliCredential()
    if source == "managed_identity" and client_id:
        return ManagedIdentityCredential(client_id=client_id)
    raise ValueError("invalid Entra credential configuration")

def make_openai_provider(credential):
    return get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default")
```

为真实实现添加 `AsyncTokenCredential` 和 TypedDict 返回类型。
key 模式使用 AzureKeyCredential/AzureNamedKeyCredential;entra 模式不读取 Key 来补救错误。
`build_openai_client` 仍是 OpenAI 出厂入口,chat/embedding 均使用它。

- [ ] **Step 4: 运行新测试与 `agent/tests/test_factory.py`。**
  `uv run --no-sync pytest shared\tests\test_cloud_contracts.py shared\tests\test_azure_auth.py agent\tests\test_factory.py -q`
- [ ] **Step 5: 提交。** 消息:`feat(auth): add explicit Azure key and Entra modes`。

## Task 2: 30 天保存与最近 20 条读取解耦

**Files**
- Modify: `agent/src/advisor_agent/sessions.py`, `agent/src/advisor_agent/core.py`
- Modify: `agent/tests/test_sessions.py`, `agent/tests/test_core.py`
- Create: `agent/tests/test_history_retention.py`

**Interfaces**
- `SessionStore.get(key: str) -> list[dict[str, str]]` 继续返回模型可用历史。
- 新增 `append_turn(key, user_content, assistant_content, *, turn_id, written_at=None) -> None`。
- InMemorySessionStore 保留 `append` 兼容旧调用;Core 正式路径改用一次原子 append_turn。
- `AdvisorCore.handle(request, *, turn_id: str | None = None) -> AdvisorResponse`:
  原调用不变,无 turn_id 时生成新 ID;worker 显式提供 job_id。
- 新增 `read_archive(key: str) -> list[HistoryMessage]` 供测试/恢复使用,不自动喂给模型。

- [ ] **Step 1: 写每条消息过期、窗口及隔离红测。**

```python
from datetime import datetime, timedelta, timezone
from advisor_agent.sessions import InMemorySessionStore

async def test_history_is_retained_beyond_context_window():
    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    store = InMemorySessionStore(utc_clock=lambda: now)
    for number in range(12):
        await store.append_turn(
            "tenant-chat-user-a", f"q{number}", f"a{number}",
            turn_id=f"turn-{number}", written_at=now)
    context = await store.get("tenant-chat-user-a")
    assert len(context) == 20
    assert context[0]["content"] == "q2"
    assert len(await store.read_archive("tenant-chat-user-a")) == 24
    assert await store.get("tenant-chat-user-b") == []

async def test_recent_activity_does_not_extend_old_message_retention():
    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    store = InMemorySessionStore(utc_clock=lambda: now)
    await store.append_turn(
        "a", "old", "old-answer", turn_id="old",
        written_at=now - timedelta(days=30))
    await store.append_turn("a", "new", "new-answer", turn_id="new",
                            written_at=now)
    assert [m["content"] for m in await store.get("a")] == ["new", "new-answer"]
```

补测 30 天减 1 微秒/恰好 30 天、相同 turn_id 重复提交、不同键同 ID、
失败问答不入历史、成功问答两条原子可见、max_turns 的旧参数兼容。

- [ ] **Step 2: 运行。**
  `uv run --no-sync pytest agent\tests\test_history_retention.py agent\tests\test_sessions.py -q`
- [ ] **Step 3: 实现单独的选择函数与原子提交。**

```python
from datetime import datetime, timedelta
from advisor_shared.cloud import HistoryMessage

def select_history(records: list[HistoryMessage], now: datetime,
                   limit: int = 20) -> list[dict[str, str]]:
    if limit <= 0:
        raise ValueError("history limit must be positive")
    cutoff = now - timedelta(days=30)
    valid = [message for message in records if message.written_at > cutoff]
    return [{"role": message.role, "content": message.content}
            for message in valid[-limit:]]
```

用写入时刻替代“最后活跃时刻续期整份历史”。普通 get 不销毁第 21 条以前的有效归档。
在 Core 的成功分支用一次 append_turn 替换两个 append,保留失败不写入、planner 身份检查、
原有 event/timing 及本地 FIFO 锁。使用语义工具检查 handle 签名新增参数的全部调用面。

- [ ] **Step 4: 运行。**
  `uv run --no-sync pytest agent\tests\test_history_retention.py agent\tests\test_sessions.py agent\tests\test_core.py channels\teams\tests\test_user_isolation.py -q`
- [ ] **Step 5: 提交。** 消息:`feat(history): retain messages for 30 days with isolated context windows`。

## Task 3: Blob 条件写入与真正的租约约束

**Files**
- Create: `agent/src/advisor_agent/durable/__init__.py`, `blob.py`, `leases.py`
- Create: `agent/tests/cloud/conftest.py`, `test_blob.py`, `test_leases.py`
- Create: `deploy/local/compose.yaml`
- Modify: `agent/pyproject.toml`, `uv.lock`

**Interfaces**
- `BlobDocument(body: bytes, etag: str, last_modified: datetime)`。
- `BlobGateway.read(name) -> BlobDocument | None`。
- `BlobGateway.list_page(prefix, *, cursor=None, limit=200) -> BlobPage`:
  BlobPage 包含 names/next_cursor,供有界维护扫描使用。
- `BlobGateway.create(name, body) -> bool`:仅 BlobAlreadyExists 返回 False。
- `BlobGateway.write(name, body, *, etag, lease_id=None) -> str`:返回新 ETag。
- `BlobGateway.delete(name, *, etag, lease_id=None) -> None`。
- `BlobGateway.lease(name) -> AsyncContextManager[LeaseGuard]`。
- `LeaseGuard.id: str`, `assert_active() -> None`, `lost: asyncio.Event`。
- 明确错误类型:`BlobConflict`, `LeaseBusy`, `LeaseLost`;401/403/5xx 不映射为空数据。

- [ ] **Step 1: 建立契约测试及内存故障注入 fixture。**
  `conftest.py` 定义 `blob_gateway`、`expire_lease`、`fail_next_operation` fixtures。
  假实现必须检查 ETag 和目标对象的 lease ID,不能把任何写入都默认放行。

```python
import pytest
from advisor_agent.durable.blob import BlobConflict
from advisor_agent.durable.leases import LeaseLost

async def test_stale_etag_is_rejected(blob_gateway):
    await blob_gateway.create("context/a", b"one")
    first = await blob_gateway.read("context/a")
    assert first is not None
    await blob_gateway.write("context/a", b"two", etag=first.etag)
    with pytest.raises(BlobConflict):
        await blob_gateway.write("context/a", b"stale", etag=first.etag)

async def test_old_owner_cannot_write_after_lease_loss(
        blob_gateway, expire_lease):
    await blob_gateway.create("context/a", b"one")
    async with blob_gateway.lease("context/a") as lease:
        first = await blob_gateway.read("context/a")
        assert first is not None
        await expire_lease("context/a")
        with pytest.raises(LeaseLost):
            await blob_gateway.write(
                "context/a", b"stale", etag=first.etag, lease_id=lease.id)
```

- [ ] **Step 2: 红测。**
  `uv run --no-sync pytest agent\tests\cloud\test_blob.py agent\tests\cloud\test_leases.py -q`
- [ ] **Step 3: 实现 SDK 网关,再实现租约守卫。**

```powershell
uv add --package advisor-agent "azure-storage-blob>=12.24,<13" "azure-storage-queue>=12.12,<13"
```

条件写入的核心必须把 ETag/lease 交给服务端:

```python
from azure.core import MatchConditions

async def write_document(client, body: bytes, etag: str,
                         lease_id: str | None) -> str:
    result = await client.upload_blob(
        body, overwrite=True, etag=etag,
        match_condition=MatchConditions.IfNotModified, lease=lease_id)
    return result["etag"]
```

按 Azure 异常的状态码和服务错误码区分缺失、冲突、争锁与失租,其余重抛。
租约 60 秒、每 20 秒续期;续期失败设置 lost 并取消受保护的处理,finally 释放并关闭续期任务。
每个可变对象使用它自己的 lease ID。不能拿“锁标记 Blob”的 lease 保护其他 Blob 的写入。
不可变归档使用条件创建,不能附上别的对象的 lease ID。

创建仅用于测试的 Azurite compose 服务,使用固定服务名但独立 project name,
只绑定本机地址。不得修改/删除其他容器。实际使用前核对容器工具配置。

- [ ] **Step 4: 跑假实现契约及同一组 Azurite 契约。**

```powershell
uv run --no-sync pytest agent\tests\cloud\test_blob.py agent\tests\cloud\test_leases.py -q
docker compose -p advisor-cloud-tests -f deploy\local\compose.yaml up -d --wait
uv run --no-sync pytest agent\tests\cloud\test_blob.py agent\tests\cloud\test_leases.py --cloud-store=azurite -q
```

`--cloud-store` 由本任务 conftest 注册,默认 memory,Azurite 使用本机测试连接配置。
无法运行 Azurite时记录阻塞,不以假实现通过代替真实租约契约。
- [ ] **Step 5: 提交。** 消息:`feat(storage): add conditional Blob writes and renewable fenced leases`。

## Task 4: 持久历史归档、上下文索引及缓冲提交

**Files**
- Create: `agent/src/advisor_agent/durable/history.py`
- Create: `agent/tests/cloud/test_history.py`, `agent/tests/test_buffered_sessions.py`
- Modify: `agent/src/advisor_agent/sessions.py`, `agent/src/advisor_agent/factory.py`

**Interfaces**
- `BlobHistoryStore(gateway, *, utc_clock)` 实现 Task 2 的 SessionStore。
- `BlobHistoryStore.now() -> datetime` 返回注入的 UTC 时钟;不混用单调时钟作持久时间戳。
- `leased(partition_key) -> AsyncContextManager[LeasedHistory]` 获取实际上下文 Blob 的租约。
- `LeasedHistory.get()`, `commit(turn_id, sequence, messages, written_at)`、
  `assert_active()`;commit 的 messages 为两条用户/助手消息。
- `BufferedSessionStore(history)` 实现 get/append_turn,仅捕获 `delta` 与 `turn_id`,
  不向持久层写入。
- `AdvisorComponents.new_core(sessions: SessionStore, *, event_sink=None) -> AdvisorCore` 创建轻量 Core;
  同一 components 复用 backend、tools、HTTP 客户端,并提供 `async aclose()`。
- `build_advisor(channel_name="generic")` 保持原入口,内部复用新的装配。

- [ ] **Step 1: 写红测。**

```python
from advisor_agent.sessions import BufferedSessionStore

async def test_buffered_turn_does_not_modify_input_history():
    seed = [{"role": "user", "content": "before"}]
    store = BufferedSessionStore(seed)
    await store.append_turn("a", "question", "answer", turn_id="job-a")
    assert seed == [{"role": "user", "content": "before"}]
    assert store.delta == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]

async def test_committed_turn_is_not_added_twice(history_store):
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    async with history_store.leased("a") as history:
        await history.commit("job-a", 1, messages, history_store.now())
        await history.commit("job-a", 1, messages, history_store.now())
        assert len(await history.get()) == 2
```

另测归档已写但索引未写时崩溃恢复、索引成功后重试、旧租约索引写入被拒、
24 条保存/20 条读取、多个独立 store 对同一分区并发及 30 天边界。

- [ ] **Step 2: 运行。**
  `uv run --no-sync pytest agent\tests\cloud\test_history.py agent\tests\test_buffered_sessions.py -q`
- [ ] **Step 3: 实现归档和索引算法。**

对象布局固定为:

```text
context/<partition-digest>.json
history/<partition-digest>/<turn-id>.json
```

partition-digest 由内部会话键再做 SHA-256,不使用原始用户输入拼接路径。
context 只保存最近窗口的引用/时间戳、最后提交序号/任务及恢复信息,
不重复保存会被后续活跃不断延长物理寿命的旧历史正文。
get 并行读取最多 10 个问答归档对象,再按 Task 2 的规则返回消息;
history 对象保存完整已准备问答。
不可变归档先创建,再以 context 的有效 lease+ETag 提交窗口。
恢复时校验归档内容与任务 ID一致,禁止把“对象已存在”无条件当成内容相同。
同一任务在索引提交成功后重试是 no-op,不能更新写入时间来延长 TTL。

```python
async def append_buffered_turn(store, key: str, user: str, assistant: str,
                               turn_id: str) -> None:
    if store.delta:
        if store.turn_id == turn_id:
            return
        raise ValueError("one buffer cannot hold two turns")
    store.turn_id = turn_id
    store.delta = [
        {"role": "user", "content": user},
        {"role": "assistant", "content": assistant},
    ]
```

生产类实现该逻辑时仍校验 key 不跨分区。new_core 不得每轮重新创建 OpenAI/Search 客户端。
用构造次数测试确认跨任务共享客户端,但不共享 buffer 或 request ContextVar。

- [ ] **Step 4: 运行相关历史、factory、Core、用户隔离测试,再以 Azurite 重跑持久层契约。**
  `uv run --no-sync pytest agent\tests\cloud\test_history.py agent\tests\test_buffered_sessions.py agent\tests\test_factory.py agent\tests\test_core.py channels\teams\tests\test_user_isolation.py -q`
- [ ] **Step 5: 提交。** 消息:`feat(history): persist archives with bounded isolated context indexes`。

## Task 5: 任务状态与独立收件箱

**Files**
- Create: `agent/src/advisor_agent/durable/jobs.py`, `agent/tests/cloud/test_jobs.py`
- Modify: `shared/src/advisor_shared/cloud.py`, `agent/tests/cloud/conftest.py`

**Interfaces**
- `JobStore.submit(payload: TaskPayload) -> Receipt`。
- `JobStore.read(job_id) -> JobRecord | None`。
- `JobStore.pending(partition_key) -> list[TaskRef]` 返回已受理的未完成项,不跨分区。
- `JobStore.leased_job(job_id) -> AsyncContextManager[LeasedJob]`。
- `LeasedJob.save_result(prepared, written_at=None)`, `mark_sending()`,
  `complete(receipt)`, `fail(code)`, `mark_delivery_unknown(code)`。
- `InboxStore.head(partition) -> TaskRef | None`, `advance(partition, job_id)`、
  `snapshot_outbox(partition) -> OutboxSnapshot`、
  `confirm_notified(partition, generation)`。
- `Receipt(job_id, sequence, state, duplicate)`;`TaskRef(job_id, sequence)`。
- `JobRecord` 包含 job_id、partition_key、sequence、state、accepted_at、
  可选 result/result_written_at、各阶段尝试次数及安全终态信息。
- `inbox.add_test_task` 仅为 conftest 的测试 fixture 辅助方法,通过正式 submit 路径增加任务;
  不在生产 InboxStore 上增加测试后门。

- [ ] **Step 1: 写红测。**

```python
import asyncio

async def test_duplicate_activity_gets_one_sequence(job_store, make_payload):
    payload = make_payload(activity_id="same", user="a")
    receipts = await asyncio.gather(
        job_store.submit(payload), job_store.submit(payload))
    assert receipts[0].job_id == receipts[1].job_id
    assert receipts[0].sequence == receipts[1].sequence
    assert len(await job_store.pending(payload.partition_key)) == 1

async def test_other_user_with_same_activity_id_is_independent(
        job_store, make_payload):
    a = await job_store.submit(make_payload(activity_id="same", user="a"))
    b = await job_store.submit(make_payload(activity_id="same", user="b"))
    assert a.job_id != b.job_id
```

`make_payload` fixture 生成规范 Teams activity,使用现有 build_conversation_key;
不是直接假设所有用户共用一个分区。增加全故障点恢复及不合法状态跳转测试。

- [ ] **Step 2: 运行。**
  `uv run --no-sync pytest agent\tests\cloud\test_jobs.py -q`
- [ ] **Step 3: 实现三个独立对象及明确状态跳转。**

```text
payload/<job-id>.json             不可变原消息,无 Authorization
jobs/<job-id>.json                实际 job lease 保护的状态/结果
inbox/<partition-digest>.json     短租约保护的受理序号、pending、outbox generation
```

任务 ID包含版本、Bot 身份、分区和 activity ID:

```python
import hashlib

def make_job_id(bot_id: str, partition_key: str, activity_id: str) -> str:
    if not all(isinstance(value, str) and value.strip()
               for value in (bot_id, partition_key, activity_id)):
        raise ValueError("job identity is incomplete")
    value = "\0".join(("job-v1", bot_id, partition_key, activity_id))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
```

submit 顺序:条件保存 payload/初始 job,短租约内确认是否已分配序号,追加 pending 并增加
outbox generation。重试先查 job 与 inbox 已有记录,不重新分配。只有通知任务确认后
接收层才返回已受理。完整 turn 锁不能是 inbox 的短锁。

状态允许路径为 pending_notify→queued→running→result_ready→sending→completed,
以及明确的 retry_wait/failed/expired/delivery_unknown。发送超时不回到 result_ready。
完成后清理重复正文;终态元数据保留 7 天,失败/未完成正文自受理起最多 24 小时。
所有 job 可变状态写入携带 job 自己的 lease,不能依赖本地检查代替服务器端约束。

- [ ] **Step 4: 运行。**
  `uv run --no-sync pytest agent\tests\cloud\test_jobs.py agent\tests\cloud\test_leases.py -q`
- [ ] **Step 5: 提交。** 消息:`feat(queue): add durable job ledger and ordered session inboxes`。

## Task 6: Queue 通知、outbox 与失效可见性

**Files**
- Create: `agent/src/advisor_agent/durable/queue.py`, `agent/tests/cloud/test_queue.py`
- Modify: `agent/tests/cloud/conftest.py`

**Interfaces**
- `SessionQueue.publish(wake: QueueWake) -> None`。
- `SessionQueue.receive() -> AsyncIterator[Delivery]` 用于 ACA。
- `Delivery.body: bytes`, `ack()`, `renew_visibility()`, `release(delay_seconds)`。
- `OutboxDispatcher.notify(partition_key, trace_id) -> None`:
  读取 generation,发送通知,再确认该 generation。
- `OutboxDispatcher.repair_pending() -> int`:补发尚未确认通知的收件箱。

- [ ] **Step 1: 写红测。**

```python
import pytest

async def test_failed_publish_never_confirms_outbox(dispatcher, queue, inbox):
    queue.fail_next_publish = True
    with pytest.raises(ConnectionError):
        await dispatcher.notify("a", "trace-a")
    assert (await inbox.snapshot_outbox("a")).needs_notification

async def test_new_arrival_is_not_cleared_by_old_confirmation(inbox):
    first = await inbox.snapshot_outbox("a")
    await inbox.add_test_task("a", "second")
    await inbox.confirm_notified("a", first.generation)
    assert (await inbox.snapshot_outbox("a")).needs_notification
```

测试“通知已发、确认前崩溃”只造成可去重通知;验证 UTF-8/中文通知、64 KB 限制、
可见性续期和失去 PopReceipt 后不能删除新持有者的消息。

- [ ] **Step 2: 运行。**
  `uv run --no-sync pytest agent\tests\cloud\test_queue.py -q`
- [ ] **Step 3: 使用 SDK 实现通知,不混用 base64。**

```python
from advisor_shared.cloud import QueueWake

async def publish_wake(client, wake: QueueWake) -> None:
    body = wake.model_dump_json()
    if len(body.encode("utf-8")) > 64 * 1024:
        raise ValueError("queue wake exceeds Storage Queue size limit")
    await client.send_message(body, time_to_live=24 * 60 * 60)
```

Azure Queue SDK 不设置 base64 编码策略;Functions 将 messageEncoding 显式设为 none。
确认 generation 时用 max(已确认值,本次发送值),不得清除新到达任务的待通知状态。
ACA 可见性初始 60 秒、每 20 秒更新,使用更新后 PopReceipt;只在安全结束后 ack。
Functions 由宿主管理消息可见性,不得另写一层删除宿主消息的代码。

持有会话执行租约的 worker 要在结束前排空或可靠重发余下通知。
争锁者可结束重复唤醒的前提是持有者仍保留原始未确认投递;不能将所有唤醒都确认后
只剩一个无人负责的持久 pending 列表。

- [ ] **Step 4: 在 memory/Azurite 两个后端运行通知及 ledger 测试。**
  `uv run --no-sync pytest agent\tests\cloud\test_queue.py agent\tests\cloud\test_jobs.py -q`
- [ ] **Step 5: 提交。** 消息:`feat(queue): deliver recoverable session wakes without payload leakage`。

## Task 7: 可恢复 worker 与结果先落地

**Files**
- Create: `agent/src/advisor_agent/durable/worker.py`, `agent/tests/cloud/test_worker.py`
- Create: `agent/src/advisor_agent/durable/maintenance.py`, `agent/tests/cloud/test_maintenance.py`
- Modify: `agent/src/advisor_agent/durable/jobs.py`, `agent/tests/cloud/conftest.py`

**Interfaces**
- `TurnExecutor.prepare(payload, history: list[dict[str, str]]) -> PreparedTurn`。
- `TurnExecutor.send(payload, prepared: PreparedTurn) -> str | None`:返回发送回执 ID。
- `DeliveryFailure` 带 `definitely_not_sent: bool`, `retryable: bool`, `safe_code: str`。
- `SessionWorker.process_session(partition_key, trace_id) -> str`:
  返回 drained/rescheduled/busy,不将失败装成 drained。
- LeasedJob 暴露 `job_id`, `sequence`, `state`, `payload`, `result`,
  `result_written_at` 和 `assert_active()`;保存方法更新其已确认快照。
- `MAX_TURNS_PER_INVOCATION=4`;结束前有余项则可靠通知,不能只返回成功。
- `Maintenance.run_once() -> MaintenanceResult` 在实际维护状态 Blob 上取得短租约,
  分页扫描 outbox/job/poison 状态;返回扫描/补发/过期/失败计数与游标。
- `get_maintenance() -> Maintenance` 在后续 runtime 提供轻量实例,不初始化 Agent。

- [ ] **Step 1: 写故障恢复红测。**

```python
import pytest
from advisor_agent.durable.worker import DeliveryFailure

async def test_saved_result_is_reused_after_history_commit_crash(worker_case):
    case = worker_case
    await case.accept("job-a", user="a")
    case.fail_once_at("after_history_commit")
    with pytest.raises(ConnectionError):
        await case.worker.process_session(case.partition("a"), "trace-a")
    await case.worker.process_session(case.partition("a"), "trace-a")
    assert case.executor.prepare_calls == 1
    assert len(await case.history.get(case.partition("a"))) == 2
    assert case.executor.send_calls == 1

async def test_ambiguous_send_is_not_replayed(worker_case):
    case = worker_case
    receipt = await case.accept("job-a", user="a")
    case.executor.send_error = DeliveryFailure(
        definitely_not_sent=False, retryable=False, safe_code="send_timeout")
    await case.worker.process_session(case.partition("a"), "trace-a")
    await case.worker.process_session(case.partition("a"), "trace-a")
    assert case.executor.send_calls == 1
    record = await case.jobs.read(receipt.job_id)
    assert record is not None
    assert record.state == "delivery_unknown"
```

`worker_case` fixture 组合真实 JobStore/HistoryStore 算法与可注入网关/发送器。
增加以下断言:反序到达的唤醒仍按受理序号处理;A/B 各自 history;
争锁不消耗业务重试预算;所有重复唤醒已结束时持有者崩溃仍能恢复;
失租后不调用 send;超过 4 轮时余项一定被重新通知。

- [ ] **Step 2: 运行。**
  `uv run --no-sync pytest agent\tests\cloud\test_worker.py -q`
- [ ] **Step 3: 实现阶段顺序和错误分流。**

```python
async def finish_prepared_job(job, history, executor):
    if job.state == "sending":
        await job.mark_delivery_unknown("recovered_during_send")
        return
    if job.state in {"completed", "failed", "expired", "delivery_unknown"}:
        return
    if job.payload is None:
        raise RuntimeError("active job payload missing")
    if job.result is None:
        prepared = await executor.prepare(job.payload, await history.get())
        await job.save_result(prepared)
    prepared = job.result
    if prepared is None or job.result_written_at is None:
        raise RuntimeError("result checkpoint missing")
    if prepared.history:
        await history.commit(job.job_id, job.sequence, prepared.history,
                             job.result_written_at)
    if prepared.reply is None:
        await job.complete(None)
        return
    history.assert_active()
    job.assert_active()
    await job.mark_sending()
    receipt = await executor.send(job.payload, prepared)
    await job.complete(receipt)
```

`save_result` 为首次结果设置固定 UTC written_at;重复保存不得刷新它。
上面的核心函数外层精确处理 DeliveryFailure:明确未发出且可重试才进入有界 retry_wait;
其他发送不确定进入 delivery_unknown。持久层失败抛出以保留队列投递,
不调用 Core 再生成已经存在的结果。

会话上下文租约与当前 job 租约共同守卫操作,任何一个失租都取消处理。
先持有 context,再持有 job;inbox 仅短暂读写,接收方永远不等待 context,避免锁环。
终态任务推进 inbox 后才能开始下一序号;无法恢复的任务明确结束,不永久堵住后续消息。
定义消息/body 24 小时过期和终态去重 7 天清理,清理只处理确认没有活动租约/在途引用的对象。
任务已经达到 24 小时时先转 expired,不因 payload 已清理而重建任务。
未过期却缺失正文属于存储一致性故障,明确记录,不能当作正常空任务。

维护每次最多检查 200 个对象或运行 30 秒,保存游标后结束,后续从游标继续。
只补发仍未确认的 outbox;毒队列项先核对 job 终态/应用恢复预算,
终态不重新入队,无法解析的项记录安全错误码后隔离,不能无限循环重投。
过期处理遵守有效租约,确认没有在途工作后清理正文;
不得在历史仍有效时删除其引用归档。以下测试还需覆盖多实例维护互斥及游标恢复:

```python
async def test_maintenance_recovers_unpublished_outbox(maintenance_case):
    case = maintenance_case
    await case.accept_without_notification()
    result = await case.maintenance.run_once()
    assert result.republished == 1
    assert case.queue.published_count == 1
    await case.maintenance.run_once()
    assert case.queue.published_count == 1
```

- [ ] **Step 4: 运行 worker 全部故障点,同时跑持久存储契约。**
  `uv run --no-sync pytest agent\tests\cloud -q`
- [ ] **Step 5: 提交。** 消息:`feat(worker): recover staged turns with ordered fenced execution`。

## Task 8: 提取 Teams 回复准备并接入主动会话

**Files**
- Create: `channels/teams/src/teams_adapter/processing.py`
- Create: `channels/teams/src/teams_adapter/cloud/__init__.py`, `executor.py`
- Create: `channels/teams/tests/cloud/test_executor.py`
- Modify: `channels/teams/src/teams_adapter/bot.py`, `agent/src/advisor_agent/factory.py`
- Modify: `channels/teams/tests/test_bot.py`, `test_user_isolation.py`

**Interfaces**
- `prepare_reply(activity, images, core, *, bot_id, turn_id=None) -> ReplyPreparation`。
- `ReplyPreparation(reply: dict | None, outcome: str)` 不执行最终网络发送。
- `AdvisorComponents.new_core(sessions, *, event_sink=None) -> AdvisorCore`:
  event_sink 可捕获单轮事件,不通过共享可变 `last_event` 传递结果。
- `TeamsTurnExecutor.prepare(payload, history)` 与 `send(payload, prepared)` 实现 Task 7 接口。
- 现有 `register_handlers`, `_activity_to_dict`, `_to_image_inputs` 的调用契约保留,
  如果实现移动则提供本仓库内转发,不在 SDK 上补丁。

- [ ] **Step 1: 写“准备阶段绝不发送最终回复”的红测。**

```python
async def test_prepare_does_not_send_final_message(executor_case):
    case = executor_case
    prepared = await case.executor.prepare(case.payload, [])
    assert prepared.reply is not None
    assert prepared.reply["text"] == "answer"
    assert prepared.history == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    assert case.final_sends == []
    await case.executor.send(case.payload, prepared)
    assert len(case.final_sends) == 1

async def test_concurrent_preparations_do_not_share_buffers(executor_case):
    import asyncio
    a, b = await asyncio.gather(
        executor_case.prepare_for("a", "question-a"),
        executor_case.prepare_for("b", "question-b"))
    assert a.history[0]["content"] == "question-a"
    assert b.history[0]["content"] == "question-b"
```

fixture 使用真实 Agents SDK adapter 与替换的 connector/模型,不能跳过 SDK state、
附件管线和序列化。补测原图/部分图失败、缺身份、非 @、无内容、群线程回复及 typing。

- [ ] **Step 2: 运行。**
  `uv run --no-sync pytest channels\teams\tests\cloud\test_executor.py channels\teams\tests\test_bot.py -q`
- [ ] **Step 3: 提取现有 handler,不重写回答业务。**

direct handler 变为“准备→原 context 发送”;无 turn_id 时仍用 `core.handle(request)`。
队列 executor 为每轮创建 BufferedSessionStore/轻量 Core/结果捕获,复用进程级重客户端。
用公开 SDK 主动会话方法执行保留的原 activity:

```python
from microsoft_agents.activity import Activity

async def continue_original_turn(adapter, bot_app_id, activity_json, callback):
    activity = Activity.model_validate(activity_json)
    await adapter.continue_conversation(bot_app_id, activity, callback)
```

generation adapter 配置现有非阻塞 typing 和附件管线;其 callback 只准备结果。
send adapter 不重新执行 Agent/附件管线,只发送已保存 Activity。
两者复用 connection manager。所有云 adapter 的 on_turn_error 向 worker 抛出安全分类错误,
不得使用 SDK 默认的“打印完整异常并发送异常正文”路径破坏阶段边界。

PreparedTurn 的 success/degraded 来自本轮捕获的 AdvisorEvent/步骤终态;
Core 返回兜底道歉不是成功回答。没有回复的正常忽略用 ignored,不能伪造空成功。

- [ ] **Step 4: 运行渠道回归及 Core/隔离测试。**
  `uv run --no-sync pytest channels\teams\tests agent\tests\test_core.py agent\tests\test_vision_pipeline.py -q`
- [ ] **Step 5: 提交。** 消息:`refactor(teams): separate reply preparation from durable delivery`。

## Task 9: 轻量接收、显式 JWT 与 Functions 原生入口

**Files**
- Create: `channels/teams/src/teams_adapter/cloud/auth.py`, `ingress.py`, `runtime.py`, `functions.py`
- Create: `channels/teams/tests/cloud/test_ingress.py`, `test_functions.py`, `test_cold_import.py`
- Create: `deploy/functions/function_app.py`, `host.json`, `.funcignore`
- Modify: `channels/teams/pyproject.toml`, `uv.lock`

**Interfaces**
- `validate_bot_request(headers, config) -> ClaimsIdentity` 使用公开 JwtTokenValidator。
- `CloudIngress.handle_activity(activity, claims) -> HttpResult`:
  `HttpResult(status: int, body: dict | None, headers: dict[str, str])`。
- `cloud/functions.py` 的 `async handle_function_request(req: func.HttpRequest) -> func.HttpResponse`。
- `get_ingress() -> CloudIngress` 为轻量进程级缓存。
- `async get_worker() -> SessionWorker` 懒加载重依赖,同一进程只构建一次。
- `FunctionRequestAdapter` 实现 HttpRequestProtocol 的 method、headers、json、
  get_claims_identity、get_path_param,不启动 HTTP 服务器。

- [ ] **Step 1: 写无认证拒绝、入队失败和冷导入红测。**

```python
async def test_missing_jwt_never_enqueues(ingress_case):
    response = await ingress_case.post_message(headers={})
    assert response.status == 401
    assert ingress_case.accepted == []
    assert ingress_case.worker_builds == 0

async def test_queue_failure_does_not_return_accepted(ingress_case):
    ingress_case.dispatcher.fail_next = True
    response = await ingress_case.post_authenticated_message()
    assert response.status == 503
    assert ingress_case.worker_builds == 0

def test_functions_import_is_lightweight(cold_import):
    imported = cold_import("function_app", directory=r"deploy\functions")
    assert "agent_framework" not in imported
    assert "openai" not in imported
```

补测 SDK 真正验证过的 ClaimsIdentity、无效/过期/错误 audience/issuer、
认证基础设施不可用返回 503、activity 身份冲突、有效重复投递、invoke 的同步回应。
测试用签名与 JWKS fixture 不进入生产配置;不能 monkeypatch 掉全部鉴权后宣称鉴权通过。

- [ ] **Step 2: 运行红测,再添加必需 Functions 依赖。**

```powershell
uv run --no-sync pytest channels\teams\tests\cloud\test_ingress.py channels\teams\tests\cloud\test_functions.py -q
uv add --package advisor-teams --optional functions "azure-functions>=1.21,<2"
uv sync --all-packages --all-extras
```

- [ ] **Step 3: 按官方 v2 装饰器绑定到共享 runtime。**

```python
import azure.functions as func
from advisor_shared.cloud import QueueWake
from teams_adapter.cloud.functions import handle_function_request
from teams_adapter.cloud.runtime import get_worker, get_maintenance

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

@app.route(route="api/messages", methods=["POST"])
async def messages(req: func.HttpRequest) -> func.HttpResponse:
    return await handle_function_request(req)

@app.route(route="healthz", methods=["GET"])
async def healthz(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("ok", status_code=200)

@app.queue_trigger(arg_name="msg", queue_name="%ADVISOR_QUEUE_NAME%",
                   connection="AdvisorQueue")
async def process_queue(msg: func.QueueMessage) -> None:
    wake = QueueWake.model_validate_json(msg.get_body())
    worker = await get_worker()
    await worker.process_session(wake.partition_key, wake.trace_id)

@app.timer_trigger(schedule="%ADVISOR_MAINTENANCE_SCHEDULE%",
                   arg_name="timer", run_on_startup=False, use_monitor=True)
async def maintenance(timer: func.TimerRequest) -> None:
    await get_maintenance().run_once()
```

Timer 来自已获取的 `timer-trigger-python-azd` 官方模板,改为默认每 5 分钟
(`0 */5 * * * *`)、不开启启动即运行。维护不是 ingestion,只实现已批准的恢复/清理。
它使用独立函数缩放组且不加载模型;冷启动报告记录维护活动,
不能因维护函数在运行就假定 HTTP/worker 热或冷。

ANONYMOUS 仅指 Functions 宿主不要求 Function Key,正式业务路由仍强制 Bot JWT。
云端拒绝 `ANONYMOUS_ALLOWED=True`。使用 SDK 的 issuer 校验开关并断言配置生效;
当前 SDK 的配置项为 `VALIDATE_ISSUER`,构造参数为 `validate_issuer`。
不要把未验签 claims 用于授权。JWK 网络故障与无效 JWT 分开分类。
入队路径不挂 typing 或图片下载中间件。其他协议活动走 SDK 同步处理。
Functions 类型和转换只在 cloud/functions.py 导入;通用 runtime/ingress 不导入 azure.functions,
因此 ACA 不需要安装 Functions 可选依赖。

host.json 使用官方扩展 bundle 与 Queue 设置,去掉 HTTP 模板中无关的 Service Bus demo:

```json
{
  "version": "2.0",
  "extensionBundle": {
    "id": "Microsoft.Azure.Functions.ExtensionBundle",
    "version": "[4.*, 5.0.0)"
  },
  "concurrency": {"dynamicConcurrencyEnabled": false},
  "extensions": {
    "http": {"routePrefix": ""},
    "queues": {
      "messageEncoding": "none",
      "batchSize": 4,
      "newBatchThreshold": 0,
      "maxPollingInterval": "00:00:02",
      "visibilityTimeout": "00:00:30",
      "maxDequeueCount": 5
    }
  }
}
```

区分宿主的 5 次投递与应用的阶段恢复预算。争锁不能通过不断抛异常耗尽宿主投递次数。
空 routePrefix 保证路径是 `/api/messages` 和 `/healthz`,不是重复的 `/api/api/messages`。

- [ ] **Step 4: 导入/绑定元数据测试及 Functions Core Tools 本地冒烟。**
  `uv run --no-sync pytest channels\teams\tests\cloud\test_ingress.py channels\teams\tests\cloud\test_functions.py channels\teams\tests\cloud\test_cold_import.py -q`
  使用编辑器任务启动 Functions;缺少 `func` 时使用官方安装指引。
  本地实际 host 验证路由、队列 UTF-8 解析、context 参数和错误终态,然后停止本次进程。
- [ ] **Step 5: 提交。** 消息:`feat(functions): add lightweight native HTTP and Queue entry points`。

## Task 10: ACA 生命周期与跨进程遥测

**Files**
- Modify: `channels/teams/src/teams_adapter/__main__.py`, `app.py`, `cloud/runtime.py`
- Modify: `shared/src/advisor_shared/telemetry.py`, `events.py`, `cloud.py`
- Create: `channels/teams/tests/cloud/test_aca_lifecycle.py`
- Create: `shared/tests/test_distributed_telemetry.py`
- Modify: `DEVELOPMENT.md`

**Interfaces**
- `ADVISOR_PROCESSING_MODE=direct|queued`,默认 direct。
- `create_cloud_app(ingress, worker_factory) -> aiohttp.web.Application`。
- `serve_queue(stop: asyncio.Event, *, parallelism=4) -> None` 由 cleanup_ctx 管理。
- `serve_maintenance(stop: asyncio.Event, *, interval_seconds=300) -> None` 在 ACA 同样受监督,
  调用 Task 7 的有界 run_once,不单独创建常驻资源。
- `trace_scope(new=False, trace_id=None)` 支持验证后的关联 ID,新进程仍新建时钟起点。
- DTO 增加可选 upstream span/trace 链接;每进程有 boot_id、segment_id 和实例元数据。

- [ ] **Step 1: 写生命周期与时钟域红测。**

```python
async def test_shutdown_waits_for_worker_cleanup(aca_case):
    async with aca_case.running_app():
        assert aca_case.consumer_started.is_set()
    assert aca_case.consumer_stopped.is_set()
    assert aca_case.clients_closed == 1

def test_linked_trace_does_not_reuse_remote_monotonic_clock():
    from advisor_shared.telemetry import trace_scope
    with trace_scope(new=True, trace_id="a" * 32) as first:
        origin = first.started_at
    with trace_scope(new=True, trace_id="a" * 32) as second:
        assert second.trace_id == first.trace_id
        assert second.started_at >= origin
        assert second.timings == []
```

补测取消持有者、消费者异常导致不健康而非悄悄停止、重复初始化不重复消费/关闭、
不同并发任务 ContextVar 不混用、非法关联 ID 被拒绝、无正文/凭据日志。

- [ ] **Step 2: 运行。**
  `uv run --no-sync pytest channels\teams\tests\cloud\test_aca_lifecycle.py shared\tests\test_distributed_telemetry.py -q`
- [ ] **Step 3: 实现受监督任务和独立时钟域。**

```python
import asyncio

async def queue_lifecycle(app):
    stop = asyncio.Event()
    consumer = asyncio.create_task(app["runtime"].serve_queue(stop))
    app["consumer"] = consumer
    try:
        yield
    finally:
        stop.set()
        await app["runtime"].stop_consumer(consumer)
        await app["runtime"].aclose()
```

stop_consumer 有明确等待/取消策略并保留失败日志,不能吞异常继续报健康。
维护任务与消费者使用同一生命周期控制,同时关闭;上面的简化示例在实际实现中
扩展为受监督任务组,任何非预期退出都可观测,不遗留维护后台进程。
不设置 detached/nohup。direct 分支保留现有启动装配和测试 monkeypatch 面。
worker 每个实际 job 使用其 payload.trace_id,不能把一批会话中的所有任务记为同一个唤醒 trace。
跨进程仅传播 ID/链接和 UTC 元数据,不传播 `_trace.started_at` 或直接相减两个进程的单调时钟。
扩展已有安全日志格式,不打开 OpenAI/HTTP SDK 的正文 DEBUG。

- [ ] **Step 4: 运行原遥测、typing、入口及新的生命周期测试。**
  `uv run --no-sync pytest shared\tests\test_telemetry.py shared\tests\test_distributed_telemetry.py channels\teams\tests\test_app.py channels\teams\tests\test_typing.py channels\teams\tests\cloud\test_aca_lifecycle.py -q`
- [ ] **Step 5: 提交。** 消息:`feat(hosting): supervise ACA workers and link distributed timings`。

## Task 11: 独立实验入口与测试发送目标

**Files**
- Create: `channels/teams/src/teams_adapter/cloud/benchmark.py`
- Create: `channels/teams/src/teams_adapter/cloud/benchmark_connector.py`
- Create: `shared/src/advisor_shared/entra_api.py`, `shared/tests/test_entra_api.py`
- Create: `channels/teams/tests/cloud/test_benchmark_access.py`
- Modify: `channels/teams/src/teams_adapter/cloud/ingress.py`,
  `channels/teams/src/teams_adapter/cloud/executor.py`,
  `channels/teams/src/teams_adapter/cloud/runtime.py`,
  `channels/teams/src/teams_adapter/cloud/functions.py`, `deploy/functions/function_app.py`
- Modify: `shared/src/advisor_shared/cloud.py`, `shared/pyproject.toml`, `.env.example`, `uv.lock`

**Interfaces**
- `EntraApiValidator.verify(token: str) -> EntraPrincipal` 放在 shared/entra_api.py,
  配置固定 tenant、audience、允许的调用应用和所需角色,供目标入口/driver 接收器复用。
- `BenchmarkAuth.verify(headers) -> EntraPrincipal`。
- `EntraPrincipal(object_id, application_id, roles)` 来自验证后的 Entra claims。
- 显式 `BENCHMARK_AUTH_MODE=entra|key`;默认 entra,Key 仅在明确选择时使用。
- `BENCHMARK_ENABLED=false` 默认关闭。
- `BenchmarkRequest(run_id, request_id, case_id, virtual_user, scenario)` 不接收 arbitrary callback URL。
- `BenchmarkReplySink.send(payload, prepared) -> receipt` 实现与 Teams 相同的发送端口。
- `cloud/functions.py` 增加 `handle_benchmark_function_request(req)`;aiohttp 增加对应薄路由。
- `/bench/messages` 走相同 JobStore/outbox/worker,仅生成受控测试身份及切换发送目的地。
- `BenchmarkConnectorFactory` 实现 SDK 公共 ChannelServiceClientFactoryBase 的
  create_connector_client/create_user_token_client,不向真实 Teams 地址外呼。

- [ ] **Step 1: 写授权与隔离红测。**

```python
async def test_disabled_benchmark_is_not_available(bench_case):
    bench_case.enabled = False
    response = await bench_case.submit_valid_case()
    assert response.status == 404
    assert bench_case.jobs_created == 0

async def test_caller_cannot_choose_real_user_or_callback(bench_case):
    response = await bench_case.submit({
        "run_id": "run-a", "request_id": "request-a",
        "case_id": "kb-01", "virtual_user": 1,
        "scenario": "real_agent",
        "user_id": "real-user", "callback_url": "https://example.invalid",
    })
    assert response.status == 400
    assert bench_case.jobs_created == 0

async def test_bot_token_does_not_authorize_benchmark(bench_case):
    response = await bench_case.submit_with_bot_token()
    assert response.status == 401
```

补测错误 issuer/audience/role/应用 ID、过期 Token、错误 Key、Entra 失败不回退、
测试 run 跨读/跨写、关闭入口后正式 Bot 仍工作、合成处理器不能从正式入口调用。

- [ ] **Step 2: 运行。**
  `uv run --no-sync pytest channels\teams\tests\cloud\test_benchmark_access.py -q`
- [ ] **Step 3: 实现显式鉴权及命名空间。**

```powershell
uv add --package advisor-shared "PyJWT[crypto]>=2.10,<3"
```

```python
import hmac

def verify_benchmark_key(expected: str, supplied: str | None) -> None:
    if not expected or supplied is None:
        raise PermissionError("benchmark credential required")
    if not hmac.compare_digest(expected, supplied):
        raise PermissionError("benchmark credential rejected")
```

Entra 使用独立 API audience、确定的 tenant/issuer、`Benchmark.Invoke` 应用角色及允许的
调用身份;不接受面向 ARM 或 Bot 的 Token。为 driver 和两个实验应用授予明确角色,
没有注册/授权权限时阻塞此模式,不临时打开匿名或自动使用 Key。
共享 EntraApiValidator 使用固定租户的 JWKS URL、RS256、明确 audience/issuer,
验证 exp/iat/tid/oid/azp/roles,不使用未验证的 issuer 拼接网络地址。
同步 JWKS 获取放入受控线程并缓存,不阻塞事件循环。不同路由有不同调用方白名单:
只有 driver 可提交,只有目标应用可回传。
Key 模式只保存受保护的短期实验凭据,不进入模板明文/报告。

服务端从已注册 run、case 和虚拟用户生成专用租户/会话/用户分区,保持现有会话键算法。
每个新的逻辑问答都有新的 request_id,HTTP 重试保留同一 request_id。
不同 profile/重复实验使用新 run_id,避免上轮幂等记录让测试变成缓存命中。
run 限定用例、有效期及提交预算,不能用一个有效实验凭据无限提交任意任务。
callback 取受控部署配置,不能由任务请求正文覆盖。
合成任务固定 async 等待 1000 ms,显式标记 synthetic;不得伪装真实模型回答或业务兜底。

复用相同 SDK 应用/回复准备流程,仅为 benchmark 选择受控 connector factory:
typing 和发送只去测试接收器,不得对虚构会话 ID 调真实 Teams API。
未覆盖的用户 OAuth/附件操作明确拒绝,真实图片管线在 Teams 冒烟中验证。
per-turn connector 代理不关闭 Runtime 拥有的共享 HTTP client;Runtime 统一关闭。
添加测试断言:benchmark 全流程没有对真实 Teams service URL 的请求。
Functions 实验路由绑定到专用转换函数,不在通用 runtime 中导入 Functions SDK:

```python
from teams_adapter.cloud.functions import handle_benchmark_function_request

@app.route(route="bench/messages", methods=["POST"])
async def benchmark_messages(req: func.HttpRequest) -> func.HttpResponse:
    return await handle_benchmark_function_request(req)
```

- [ ] **Step 4: 运行授权、生产 JWT、队列隔离回归。**
  `uv run --no-sync pytest shared\tests\test_entra_api.py channels\teams\tests\cloud\test_benchmark_access.py channels\teams\tests\cloud\test_ingress.py channels\teams\tests\test_user_isolation.py -q`
- [ ] **Step 5: 提交。** 消息:`feat(bench): isolate authenticated experiment traffic from Teams`。

## Task 12: 可复现打包与配置安全导入

**Files**
- Create: `deploy/tools/pyproject.toml`, `deploy/tools/src/advisor_deploy/__init__.py`
- Create: `deploy/tools/src/advisor_deploy/config.py`, `package.py`, `__main__.py`
- Create: `deploy/tools/tests/test_deploy_config.py`, `test_package.py`
- Create: `deploy/shared/Package.ps1`, `deploy/aca/Dockerfile`, `deploy/aca/Dockerfile.dockerignore`
- Modify: root `pyproject.toml`, `uv.lock`, `.gitignore`, `DEVELOPMENT.md`

**Interfaces**
- 新 uv 成员 `advisor-deploy`,依赖 advisor-shared、httpx、azure-identity、python-dotenv;
  仅在其所属打包/驱动任务需要时增加其他直接依赖。
- `load_deploy_config(env_file: Path, target: str) -> DeployConfig`:
  显式白名单、secret 字段隐藏、Linux 配置路径转换。
- `build_package(target: str, output: Path) -> PackageManifest`。
- `validate_package(path: Path) -> PackageManifest` 验证条目、哈希与入口。
- CLI:`python -m advisor_deploy package --target aca|functions --output PATH`、
  `python -m advisor_deploy verify-package PATH`。

- [ ] **Step 1: 写泄露防护及包完整性红测。**

```python
import pytest
from advisor_deploy.config import load_deploy_config
from advisor_deploy.package import validate_package

def test_shell_text_in_env_is_data_not_executed(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("UNUSED=$(Write-Output secret)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="configuration"):
        load_deploy_config(env_file, target="aca")
    assert not (tmp_path / "secret").exists()

def test_package_rejects_credentials_or_local_venv(package_case):
    archive = package_case.zip_with({
        "function_app.py": "import azure.functions\n",
        ".env": "PRIVATE=value\n",
    })
    with pytest.raises(ValueError, match="forbidden"):
        validate_package(archive)
```

再测路径穿越条目、重复 zip 条目、缺 host.json/模块、错误 wheel 哈希、
缺真实 escalation YAML、配置文件目标路径、日志/manifest 不输出 Key。

- [ ] **Step 2: 注册 workspace 后运行红测。**
  新包开发环境依赖在 manifest 变更后用 uv 同步,并将 `deploy/tools/tests` 加入根 pytest testpaths。
  使用 test_deploy_config.py 避免与 ingestion 的 test_config.py 发生收集模块名冲突。
  `uv run --no-sync pytest deploy\tools\tests\test_deploy_config.py deploy\tools\tests\test_package.py -q`
- [ ] **Step 3: 使用 uv 锁文件导出,只带在线闭包。**

```powershell
uv export --locked --package advisor-teams --no-dev --no-editable --no-emit-workspace --emit-index-url --output-file deploy\out\requirements-aca.txt
uv export --locked --package advisor-teams --extra functions --no-dev --no-editable --no-emit-workspace --emit-index-url --output-file deploy\out\requirements-functions.txt
uv build --package advisor-shared --wheel --out-dir deploy\out\wheels
uv build --package advisor-agent --wheel --out-dir deploy\out\wheels
uv build --package advisor-teams --wheel --out-dir deploy\out\wheels
```

为每个本地 wheel 计算 SHA-256并添加带 hash 的 requirements 条目,
否则已有依赖 hash 会让远程 pip 的 require-hashes 检查拒绝本地 wheel。
保留明确的企业包源,验证云构建能访问它;不能偷偷换源或重解锁依赖。

配置只读入白名单,不打印值。`ESCALATION_CONFIG` 等源路径被映射到
ACA `/app/runtime-config/` 或 Functions `/home/site/wwwroot/runtime-config/`。
未提供文件时失败,不复制示例冒充真实配置。

镜像只使用打包产物作为构建上下文:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt /app/requirements.txt
COPY wheels /app/wheels
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY runtime-config /app/runtime-config
USER 10001
EXPOSE 3978
CMD ["python", "-m", "teams_adapter"]
```

执行构建时固定记录 base image digest,不可只记录可变 tag。
加入 `.gitignore`: `deploy/out/`, `deploy/functions/local.settings.json`,
`deploy/reports/raw/`;不得忽略正式脚本、IaC 或脱敏报告。
不复制 Windows venv 到 Linux,不把测试/dev依赖、`.env` 或 `.state` 放进部署包。

- [ ] **Step 4: 验证 zip 清单与 Linux 导入,不调用真实模型。**

```powershell
uv run --no-sync --package advisor-deploy python -m advisor_deploy package --target functions --output deploy\out\functions.zip
uv run --no-sync --package advisor-deploy python -m advisor_deploy verify-package deploy\out\functions.zip
uv run --no-sync pytest deploy\tools\tests -q
```

在构建镜像内验证在线模块可导入及配置缺失会明确报错。
本任务产生本地包/镜像,不上传 Registry、不创建云资源。
- [ ] **Step 5: 提交。** 消息:`feat(deploy): package locked online dependencies without local secrets`。

## Task 13: 最小成本 IaC 与身份边界

**Files**
- Create: `deploy/shared/infra/resource-groups.bicep`, `state.bicep`, `bot.bicep`,
  `dependency-roles.bicep`, `monitoring.bicep`
- Create: `deploy/aca/infra/main.bicep`, `container-app.bicep`, `registry.bicep`
- Create: `deploy/functions/infra/main.bicep`, `api.bicep`, `plan.bicep`, `rbac.bicep`
- Create: `deploy/functions/LICENSE.azure-samples`
- Create: `deploy/bench/infra/main.bicep`
- Create: `deploy/tools/tests/test_infra_contracts.py`

**Interfaces**
- 两个 main.bicep 接收 location、环境名、模型/Search 资源 ID、模型部署名、镜像/包标识、
  auth mode、最大实例数、worker 并发、benchmark enable/audience/principal 列表。
- 敏感 app settings 参数使用 `@secure()`;输出只含资源 ID、非敏感 endpoint、身份 ID、
  配置摘要和本次授权 ID,不输出 secrets。
- `DeploymentOutputs` 字段固定为 target、resource_group、app_id、app_name、
  endpoint、storage_id、blob_url、queue_url、identity_id、identity_client_id、
  identity_principal_id、bot_id、created_role_assignment_ids。
- 默认 worker 并发 4、单进程;默认最大实例数参数 40,须通过当前平台/配额验证后才使用。

- [ ] **Step 1: 写编译产物契约红测。**

```python
def test_flex_defaults_are_not_legacy_consumption(compiled_infra):
    flex = compiled_infra("functions")
    assert flex.parameter_default("instanceMemoryMB") == 512
    assert flex.parameter_default("runtimeVersion") == "3.12"
    assert flex.has_plan_sku("FC1")
    assert not flex.has_plan_sku("Y1")

def test_deployment_outputs_do_not_expose_secrets(compiled_infra):
    for target in ("aca", "functions"):
        outputs = compiled_infra(target).outputs
        assert not any("secret" in name.lower() or "key" in name.lower()
                       for name in outputs)
```

fixture 只解析真实 `az bicep build` 输出,包括嵌套模板。
补测独立 Bot/UAMI、Storage 私有容器/TLS、Queue 角色、模型/Search 只读权限、
正确订阅级 roleDefinitionId、无 Always Ready、benchmark 默认关闭和无无关 VNet/Redis。
这些测试只验证模板,部署后还必须读取实际生效配置。

- [ ] **Step 2: 运行,确认缺少目标模板导致失败。**
  `uv run --no-sync pytest deploy\tools\tests\test_infra_contracts.py -q`
- [ ] **Step 3: 基于已获取官方模板组合资源,保留许可。**

执行时再次通过 Functions 模板发现/获取工具核对 HTTP 基线。
沿用其 Flex 部署存储/UAMI/AVM 模式,显式选择模板支持的 VNet 关闭选项,
runtimeVersion=3.12、instanceMemoryMB=512、enableQueue=true、enableTable=false。
不使用 `func init` 或 `azd init -t` 覆盖本仓库。

Functions 的每个 UAMI 绑定都需要这三个设置:

```bicep
param queueServiceUri string
param identityClientId string

var queueBindingSettings = {
  AdvisorQueue__queueServiceUri: queueServiceUri
  AdvisorQueue__credential: 'managedidentity'
  AdvisorQueue__clientId: identityClientId
}
```

AzureWebJobsStorage 同样配置其 blob/queue URI、credential、clientId;
host 与部署存储默认始终使用托管身份。不能拿 runtime 存储 Key 修复 Entra 403。
部署 `ADVISOR_MAINTENANCE_SCHEDULE=0 */5 * * * *`,并将维护计费/存储操作纳入费用摘要。
应用显式 Key 模式需要的账户设置变化只允许作用于本次账户且需明确确认,不静默降低
主实验账户安全配置。兼容测试可先使用 Azurite/明确允许 Key 的测试账户。

ACA 队列缩放使用已核对的 `custom.identity`:

```bicep
param managedIdentityId string
param storageAccountName string
param incomingQueueName string = 'advisor-incoming'
param workerParallelism int = 4

var queueRule = {
  name: 'advisor-queue'
  custom: {
    type: 'azure-queue'
    identity: managedIdentityId
    metadata: {
      accountName: storageAccountName
      queueName: incomingQueueName
      queueLength: string(workerParallelism)
    }
  }
}
```

同时配置 HTTP 缩放、标准健康探针、单活 revision、托管身份拉镜像及最小副本参数。
新 Basic ACR 放在 ACA 实验组,不操作已存在仓库。
两个 Bot 为 F0/UAMI,启用 Teams 渠道,各自生成安装包。

角色最小集合:

| 身份/目标 | 角色 |
|---|---|
| 应用→OpenAI | Cognitive Services OpenAI User |
| 应用→Search | Search Index Data Reader |
| ACA 应用→状态 Blob/Queue | Storage Blob Data Contributor / Storage Queue Data Contributor |
| Flex 宿主→自身存储 | 保留官方模板要求的 Storage Blob Data Owner、Queue Data Contributor |
| ACA/driver→本次 ACR | AcrPull |
| Flex→自身 Application Insights | 模板要求的 Monitoring Metrics Publisher |

角色 ID从官方定义核对,使用 subscriptionResourceId 构造定义路径。
外部模型/Search 的授权放在明确 scope 的模块中并输出本次 assignment ID,不授予资源组 Owner。

历史生命周期只针对不可变历史对象;最新上下文索引保存引用而不是长期重复的历史正文。
payload 按 1 天清理、历史按 30 天清理、非正文 job 元数据按 7 天策略处理;
job 中尚未清理的结果正文也服从任务正文的 24 小时上限,不能因 job 元数据保存 7 天而延长它。
活跃协调对象不能仅因年龄被粗暴删除。读取/worker 仍执行严格逻辑过期。
采用最小必要日志资源,明确保留期,不将问答正文作为遥测。

- [ ] **Step 4: 编译并运行契约测试,不创建资源。**

```powershell
az bicep build --file deploy\aca\infra\main.bicep --outfile deploy\out\aca.json
az bicep build --file deploy\functions\infra\main.bicep --outfile deploy\out\functions.json
az bicep build --file deploy\bench\infra\main.bicep --outfile deploy\out\bench.json
uv run --no-sync pytest deploy\tools\tests\test_infra_contracts.py -q
```

- [ ] **Step 5: 提交。** 消息:`feat(infra): define isolated ACA and Flex experiment resources`。

## Task 14: 幂等部署、前置校验及精确清理脚本

**Files**
- Create: `deploy/shared/Azure.ps1`, `Preflight.ps1`, `Ensure-BenchmarkIdentity.ps1`
- Create: `deploy/aca/Deploy.ps1`, `Set-Mode.ps1`, `deploy/functions/Deploy.ps1`
- Create: `deploy/Cleanup.ps1`, `deploy/README.md`
- Create: `deploy/tools/src/advisor_deploy/manifest.py`
- Create: `deploy/tools/tests/test_deploy_manifest.py`, `test_powershell_contracts.py`
- Modify: `.azure/deployment-plan.md`

**Interfaces**
- `Invoke-CheckedAz -Arguments <string[]>` 检查退出码,解析错误时失败,不返回空成功对象。
- `Preflight.ps1 -Target aca|functions -ParametersFile PATH` 不创建资源,输出结构化通过/阻塞项。
- `Deploy.ps1 -ParametersFile PATH -Mode WhatIf|Apply` 默认 WhatIf。
- `Set-Mode.ps1 -OutputsFile PATH -Mode Resident|ScaleToZero` 只改变本实验 app 的 minReplicas。
- `Cleanup.ps1 -OutputsFile PATH -WhatIf` 默认预览,真实删除需要确认范围。
- manifest 保存运行标识、资源所有权、参数摘要、artifact hashes、创建的身份/授权,
  不保存 secrets 或整个 `.env`。

- [ ] **Step 1: 写命令与删除边界红测。**

```python
import pytest
from advisor_deploy.manifest import validate_cleanup_scope

def test_cleanup_rejects_existing_model_resource():
    with pytest.raises(ValueError, match="outside experiment"):
        validate_cleanup_scope(
            requested_ids=["/subscriptions/s/resourceGroups/rg-ai-services-all"],
            owned_ids=["/subscriptions/s/resourceGroups/rg-copilot-advisor-aca-exp"])

def test_cleanup_accepts_only_recorded_resources():
    owned = ["/subscriptions/s/resourceGroups/rg-copilot-advisor-aca-exp"]
    assert validate_cleanup_scope(requested_ids=owned, owned_ids=owned) == owned
```

PowerShell 契约测试通过假的 az 可执行入口记录 argv,验证非零退出码不被吞掉、
WhatIf 不调用 create/delete、subscription 显式传入、secret 只走受保护文件、
同名无归属资源会阻塞、重跑不覆盖其他资源及未确认时不会删除。
不要使用对真实订阅有副作用的单元测试。

- [ ] **Step 2: 运行。**
  `uv run --no-sync pytest deploy\tools\tests\test_deploy_manifest.py deploy\tools\tests\test_powershell_contracts.py -q`
- [ ] **Step 3: 实现显式 apply 与安全参数文件。**

脚本用参数数组调用 CLI,不使用 Invoke-Expression,不将 `.env` 的内容当命令。
成功输出与错误输出分开处理;错误保留安全的退出码、服务码和阶段,对敏感字段脱敏。
临时参数文件设置仅当前操作者可读,用 try/finally 删除该具体文件,不得递归删除工作区。

已核对的 Azure CLI 操作形状:

```powershell
az deployment group validate --subscription $SubscriptionId --resource-group $ResourceGroup --template-file $BicepFile --parameters "@$ParametersFile"
az deployment group what-if --subscription $SubscriptionId --resource-group $ResourceGroup --template-file $BicepFile --parameters "@$ParametersFile"
az deployment group create --subscription $SubscriptionId --resource-group $ResourceGroup --template-file $BicepFile --parameters "@$ParametersFile" --mode Incremental
```

这些变量全部由脚本必填参数/已验证 manifest 赋值,不得从不可信输出拼成命令文本。
不存在的资源组仅在批准的 Apply 阶段创建;不能为了让 validate 成功而偷偷创建。
首次部署先使用 resource-groups.bicep 做订阅级 validate/what-if,
获准 Apply 后创建资源组,再验证组内模板。组内验证失败时如实保留失败状态和已创建资源清单。

Flex 使用支持的 zip/OneDeploy 路径与远程构建,发布由 Task 12 校验过的包。
ACA 使用固定 digest 的本次镜像。等待 RBAC 传播与真实数据面可用,
而不是重试失败后改成匿名/Key。创建后重新读取实际 app 配置,校验 minReplicas、
内存、运行时、队列并发、身份及模型引用。

Benchmark Entra 身份单独建立,设置单租户、v2 access tokens 和 `Benchmark.Invoke` 应用角色。
应用 ID/服务主体/角色 assignment 写入所有权 manifest,允许重跑复用本次已创建对象。
只允许 driver 提交测试,只允许两个目标应用发送测试回执。
没有目录注册/授权权限时记录阻塞,不复用无关应用或降低鉴权。

Preflight 检查:配置/包、区域、当前配额、实际依赖存在性、Search 向量维度、
资源/数据面权限、既有资源认证设置和工作区安全状态。
通用 Web VM 额度不能作为 Flex 内存证明;没有适用数据时保留阻塞项,
通过适用服务检查或经确认的配额证据解决。

- [ ] **Step 4: 所有脚本用 WhatIf/假 CLI 验证,更新部署计划但不标 Deployed。**
  `uv run --no-sync pytest deploy\tools\tests -q`
  `.azure/deployment-plan.md` 记录已生成文件和通过证据;尚未完成的 Azure 验证保持未完成。
- [ ] **Step 5: 提交。** 消息:`feat(deploy): add guarded idempotent deployment and cleanup commands`。

## Task 15: 完整问答负载生成、冷证据与统计

**Files**
- Create: `deploy/tools/src/advisor_deploy/bench/__init__.py`, `models.py`, `runner.py`,
  `receiver.py`, `stats.py`, `cold.py`, `report.py`
- Create: `deploy/tools/tests/test_bench_runner.py`, `test_bench_stats.py`, `test_cold_evidence.py`
- Create: `deploy/bench/cases.json`, `run.example.json`, `Dockerfile`, `Run.ps1`
- Modify: `deploy/tools/pyproject.toml`, `__main__.py`, `uv.lock`

**Interfaces**
- `RunPlan` 包含 target/profile、case IDs、seed、concurrency、样本量/期限、
  auth scope、worker/host 配置摘要,不包含凭据。
- `RunResult` 保存 attempted/completed/success/degraded/failed/unknown/censored、
  每请求同一观察者时钟的开始/完成时间及安全阶段数据。
- `ReplyReceiver.register(request_id) -> Future[ObservedReply]` 在提交前注册。
  不能要求提交前已知由服务器生成的 job_id;ACK 将二者关联,回执也携带原 request_id。
- `run_level(plan, submit, receiver) -> RunResult` 收到完整结果才释放并发槽。
- `summarize(samples, window_seconds) -> Summary`。
- `classify_cold(evidence: ColdEvidence) -> str` 返回 natural_zero/deployment/restart/warm/unverified。
- CLI:`bench run --plan PATH`, `bench report --input PATH --output PATH`。

- [ ] **Step 1: 写统计和并发定义红测。**

```python
from advisor_deploy.bench.stats import percentile, summarize
from advisor_deploy.bench.cold import ColdEvidence, classify_cold

def test_percentiles_use_complete_answer_durations():
    assert percentile([10.0, 20.0, 30.0, 40.0], 0.95) == 38.5
    assert summarize([], window_seconds=10).mean_ms is None

def test_waiting_alone_is_not_cold_start_evidence():
    evidence = ColdEvidence(idle_seconds=3600, zero_confirmed=False,
                            new_boot_confirmed=True, reason="idle")
    assert classify_cold(evidence) == "unverified"

async def test_http_ack_does_not_release_inflight_slot(runner_case):
    case = runner_case
    run = case.start(concurrency=1, count=2)
    await case.first_ack_received.wait()
    assert case.submitted_count == 1
    case.complete_first_reply()
    await run
    assert case.submitted_count == 2
```

补测失败/降级单列、空样本、NaN/无限值拒绝、吞吐分母为观察窗口、超时右删失、
串行用户 vs 独立用户、晚到/重复回执、回执在 ACK 前到达、驱动端重启与停止条件。

- [ ] **Step 2: 运行。**
  `uv run --no-sync pytest deploy\tools\tests\test_bench_runner.py deploy\tools\tests\test_bench_stats.py deploy\tools\tests\test_cold_evidence.py -q`
- [ ] **Step 3: 实现明确的统计及闭环负载。**

```powershell
uv add --package advisor-deploy "aiohttp>=3.9,<4"
```

```python
import math

def percentile(values: list[float], quantile: float) -> float | None:
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between zero and one")
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("durations must be finite and nonnegative")
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
```

driver 和 callback 接收器运行在同一个独立短时服务中,用同一单调时钟测量完整应用链路。
不使用无 ingress 的 Job 实例充当需要接收 HTTP 回执的服务器。
接收器复用 Task 11 的 EntraApiValidator,只接受目标应用的回执;
typing 不完成等待中的 Future,只有最终结果或明确的终态拒绝才完成。
驱动端预先准备自身身份/连接,但不探活目标应用;记录本身 CPU/内存/事件循环延迟,
饱和时停止并标为驱动端瓶颈。

数据集固定非敏感问题,包含 KB、Web 候选、空历史/20 条历史、多用户/同用户。
每次逻辑问答新建 request_id;HTTP 重试复用该 ID,不同运行新建 run_id。
空历史组使用新会话,20 条历史组以相同内容/顺序预置独立会话;
不能让连续压测自动累积历史后仍称为空历史组。
真实检索路径以实际事件为准,不因问题名字叫 kb-01 就记为 KB 成功。
合成任务与真实 Agent 单独成组。case 文件只保留自己编写的问题/预期结构,不复制客户群聊。

冷证据同时包含平台归零证据、新 boot/instance、首次处理及控制动作分类。
分别记录 HTTP/Queue scale group;不靠第一次 import 或等待时间推断整个 Flex 已归零。
查结果通过独立接收器/Storage/平台遥测,不反复调用目标的 status/health 路由。

运行默认值复制设计:冷样本目标 10、最多 15 次/配置、每次归零等待上限 60 分钟;
热态 100 次;并发阶梯 1–128;每档至少 50 次且 3 分钟,最多提交 30 分钟;
单请求观察/收尾各 600 秒;60 秒趋势窗口及设计中的限流/积压/OOM 停止条件。
运行清单必须保存所有实际参数,缺少参数或非有限数值时拒绝运行。

- [ ] **Step 4: 用受控本地接收器跑小型完整闭环,不调用真实模型。**

```powershell
uv run --no-sync pytest deploy\tools\tests\test_bench_runner.py deploy\tools\tests\test_bench_stats.py deploy\tools\tests\test_cold_evidence.py -q
uv run --no-sync --package advisor-deploy python -m advisor_deploy bench run --plan deploy\bench\run.example.json
```

example 默认只连接本机假接收器,不允许指向真实云目标而没有部署 manifest。
验证输出包含原始样本、明确失败/删失、统计口径,不包含原始回复正文或 Token。
- [ ] **Step 5: 提交。** 消息:`feat(bench): measure completed turns and verified cold starts`。

## Task 16: 本地总回归、Azure 校验与两套云功能验证

**Files**
- Create: `deploy/tools/src/advisor_deploy/smoke.py`, `deploy/tools/tests/test_smoke_results.py`
- Modify: `.azure/deployment-plan.md`, `deploy/README.md`, `docs/teams-setup.md`
- Generated, ignored: `deploy/out/*/deployment.json`, `smoke.json`, 两个 Teams 应用包

**Interfaces**
- CLI:`smoke --deployment PATH --auth-mode key|entra`。
- `SmokeResult` 分别报告模型、3072 维 embedding、实际 Search、
  Blob/Queue、Bot/benchmark 鉴权、完整结果与各验证证据。
- 未授权、not_run、blocked、failed、passed 是不同状态。

- [ ] **Step 1: 写“健康但不能回答”不得通过的红测。**

```python
from advisor_deploy.smoke import evaluate_smoke

def test_health_and_ack_do_not_prove_bot_success():
    result = evaluate_smoke({
        "health": "passed", "ingress_ack": "passed",
        "embedding": "failed", "complete_answer": "not_run",
    })
    assert not result.passed
    assert "embedding" in result.failed_checks
```

- [ ] **Step 2: 执行本地回归,再生成最终部署包。**

```powershell
uv run --no-sync pytest shared\tests agent\tests channels\teams\tests ingestion\tests deploy\tools\tests -q
```

这是跨包更改后的整体验证门槛,不是每个小任务都运行全套。
同时完成 Azurite 契约、原本地命令及本地队列模式验证。
若已有编辑器构建任务,先使用该任务。没有依赖缺失不要重新安装环境。

- [ ] **Step 3: 执行 Azure 验证门槛,不绕过失败项。**

更新 `.azure/deployment-plan.md` 为 Ready for Validation 仅表示准备产物已就绪,
不是云部署已成功。按 `azure-validate → azure-deploy` 顺序执行。
校验中的配额/策略/权限阻塞必须解决后才 Apply。
再次确认资源范围;不能因用户离线而扩大授权或删除范围。

先 ACA 后 Flex,每套先最小资源/配置验证,再确认实际 Key/Entra 数据面能力。
有效证据至少包括一次真实 embedding 的长度=3072、一次实际 KB 查询成功、
完整问答与新实例恢复历史。不要记录向量、密钥或检索正文。
HTTP/队列的失败路径与未认证拒绝也必须实际验证。

- [ ] **Step 4: 做两套 Teams 验收,如需人工则明确等待。**

```powershell
uv run --no-sync --package advisor-deploy python -m advisor_deploy smoke --deployment deploy\out\aca\deployment.json --auth-mode entra
uv run --no-sync --package advisor-deploy python -m advisor_deploy smoke --deployment deploy\out\functions\deployment.json --auth-mode entra
```

Key 模式在明确允许的测试资源上验证,不为修复 Entra 失败偷偷打开 Key。
让用户侧载两个独立实验 Bot,验证私聊、群 @、图片、A/B/A隔离与连续消息。
没有用户反馈的真实客户端测试保持 not_run,自动压测可以另行推进但不能冒称 Teams 已通过。

- [ ] **Step 5: 提交已验证代码/文档。** 消息:`test(deploy): verify local and cloud functional gates`。
  不提交带凭据的运行文件,云验证事实写入脱敏记录。

## Task 17: 执行时延与并发实验

**Files**
- Create: `deploy/reports/latency-report.md`, `concurrency-report.md`
- Generated, ignored: `deploy/reports/raw/<run-id>/`, `deploy/out/bench/`
- Modify: `deploy/bench/Run.ps1`, 仅修复实验发现的本次改造问题

**Interfaces**
- Run.ps1 根据部署 manifest 生成真实运行清单,不依靠默认订阅或 URL 猜测。
- 结果标签至少含 profile、auth_mode、code/image版本、模型/SKU/版本、实例/并发配置、
  case/seed/history组、冷证据、业务终态与驱动端状态。

- [ ] **Step 1: 先运行小型有效性检查。**

```powershell
.\deploy\bench\Run.ps1 -AcaOutputs deploy\out\aca\deployment.json -FlexOutputs deploy\out\functions\deployment.json -Suite Sanity
```

Sanity 每个目标提交一个合成任务和一个真实问答,断言 driver 完整回执、关联 ID、
入队/worker/发送阶段可串联,且没有向真实群发送压测回复。
任一失败就修复并回归,不继续积累无效压测样本。

- [ ] **Step 2: 按轮换顺序执行热态/归零测试。**

```powershell
.\deploy\aca\Set-Mode.ps1 -OutputsFile deploy\out\aca\deployment.json -Mode Resident
.\deploy\bench\Run.ps1 -AcaOutputs deploy\out\aca\deployment.json -FlexOutputs deploy\out\functions\deployment.json -Suite Latency
```

Latency 内部按已保存计划完成 aca-resident、flex-on-demand 及 aca-zero 补充测试,
每次模式改变单独记录。自然冷、部署冷、重启冷、未确认冷各自归档。
若无法取得平台归零证据,必须报告该项未确认,不能拿人工重启补足“10 个自然冷样本”。

- [ ] **Step 3: 执行阶梯并发和一致性负载。**

```powershell
.\deploy\bench\Run.ps1 -AcaOutputs deploy\out\aca\deployment.json -FlexOutputs deploy\out\functions\deployment.json -Suite Concurrency
```

逐档执行,任一时刻只对一个宿主做主负载,避免共享模型/搜索配额相互影响。
合成、真实 Agent、同用户串行单独成组。遇持续 429/资源故障/积压停止升压,
记录是宿主、队列、状态、模型、外部搜索还是 driver 瓶颈。
需要修改资源规格时产生新的配置组与运行标识,不得覆盖原失败证据。

- [ ] **Step 4: 根据原始证据生成报告并复核统计。**

```powershell
uv run --no-sync --package advisor-deploy python -m advisor_deploy bench report --input deploy\out\bench --output deploy\reports
```

报告列明目标和实际样本数、成功/降级/失败/未知/未完成、完整回答吞吐与均值/分位数。
驱动端或渠道未验证的范围显式说明。无 SLA 时只能报告已验证的 C 并发及对应质量/时延,
不能输出脱离配置/依赖配额的“系统最大并发”。
用单元测试中的确定样本再次验证汇总算法,抽查原始记录与报告行一致。

- [ ] **Step 5: 提交脱敏报告。** 消息:`docs(bench): report measured latency and concurrency results`。
  报告只能在真实运行后创建为结果文档,不能现在写虚构数值或预填“通过”。

## Task 18: 成本摘要、收尾与最终文档

**Files**
- Create: `deploy/reports/summary.md`
- Modify: `deploy/README.md`, `README.md`, `DEVELOPMENT.md`, `.env.example`,
  `docs/teams-setup.md`, `.azure/deployment-plan.md`
- Modify: `deploy/Cleanup.ps1` 或其他本次脚本,仅修复验证发现的问题

**Interfaces**
- summary 分列托管、镜像仓库、状态/队列、日志、driver 和模型/API 增量。
- Azure 成本查询记录数据时效;未出账单的部分标估算,不伪装已结算。
- 最终状态分别记录部署、功能、真实 Teams、实验与清理,不只给一个笼统绿色标记。

- [ ] **Step 1: 复核费用归属与报告差异。**
  使用真实 Southeast Asia 单价和实际运行配置重新估算。
  不复用早期 East Asia 的 $0.055/$0.334 会话示例作为总成本。
  既有 Search 固定费与新增资源费分开;新 Basic ACR 固定费必须展示。

- [ ] **Step 2: 停止负载并处理在途任务。**
  记录仍未完成任务,明确决定继续等待或取消,不记成零时延。
  关闭 benchmark 入口并测试它不再可用,同时确认正式 Bot 入口仍鉴权。
  将 ACA 恢复 minReplicas=0,确认 Flex Always Ready 仍为0。

- [ ] **Step 3: 预览清理,经用户确认才删除。**

```powershell
.\deploy\Cleanup.ps1 -OutputsFile deploy\out\bench\deployment.json -WhatIf
.\deploy\Cleanup.ps1 -OutputsFile deploy\out\aca\deployment.json -WhatIf
.\deploy\Cleanup.ps1 -OutputsFile deploy\out\functions\deployment.json -WhatIf
```

如果用户暂时无法确认,保留资源并清楚告知其后续费用,不自动删除。
临时本地文件只按本次明确路径清理,不递归删除仓库、用户目录或整个会话目录。
已有模型、Search、本地 Bot、其他 Registry 不在清理范围。

- [ ] **Step 4: 更新可执行文档并做最终检查。**
  README 链接本地、ACA、Flex、实验和报告入口;环境模板分别解释 Key/Entra、
  30 天历史与最近 20 条、用户隔离、queued/direct及 benchmark 默认关闭。
  记录已验证配置及明确限制,包括 GlobalStandard 不保证本区域推理。
  复查现有功能未被部署改造改变;未实际执行的验证项不标通过。

- [ ] **Step 5: 最终提交。** 消息:`docs(deploy): document verified operating modes and experiment costs`。
  只提交必要代码/配置/脱敏报告,确认工作区没有意外暂存的凭据或原始结果。

## 3. 计划自检与规格覆盖表

| 设计要求 | 实现任务 |
|---|---|
| 保留本地方式,不部署 ingestion | 1、2、8、10、12、18 |
| Key/Entra 与独立 Bot 身份 | 1、9、11、13、14、16 |
| 持久队列与快速确认 | 5、6、7、9 |
| 租约、顺序、通知恢复、发送不确定 | 3、4、5、6、7、8 |
| 30 天保存/20 条上下文/用户隔离 | 2、4、5、8、16 |
| 原图片/typing/引用/升级行为 | 8、10、16 |
| 两套最低成本托管与隔离资源 | 12、13、14、16 |
| 冷启动、平均时延与并发实测 | 10、11、15、17 |
| 真实 Teams 与应用链路区分 | 8、11、16、17 |
| 脱敏原始证据与成本报告 | 12、14、15、17、18 |
| 不误删/不隐式降级/结束后归零 | 1、11、14、18 |

执行交接前检查接口名/参数是否一致,特别是:

- `save_result` 的时间戳职责、history.commit 的参数顺序。
- get_ingress 不导入/初始化 worker 或 Functions 专用依赖。
- receiver 注册在提交前,ACK 不释放并发槽。
- Blob 的 lease ID仅用于它实际对应的对象。
- TaskPayload/QueueWake 不包含原始 JWT或任意 callback URL。
- 所有脚本失败状态、not_run、blocked、censored 均不被汇总成成功。

## 4. 已使用的官方来源

- [Python 异步 Azure Identity/token provider](https://learn.microsoft.com/python/api/azure-identity/azure.identity.aio?view=azure-python)。
- [异步 Blob 租约](https://learn.microsoft.com/azure/storage/blobs/storage-blob-lease-python#lease-blobs-asynchronously)。
- [Functions Python Queue Trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-storage-queue-trigger?pivots=programming-language-python)。
- [Queue host.json 配置](https://learn.microsoft.com/azure/azure-functions/functions-bindings-storage-queue#hostjson-settings)。
- [Flex 配置](https://learn.microsoft.com/azure/azure-functions/flex-consumption-how-to)。
- [ACA 自定义缩放/身份](https://learn.microsoft.com/azure/container-apps/scale-app?pivots=container-apps-bicep#custom)。
- Azure Functions 模板工具:已发现并获取 `http-trigger-python-azd`、
  `timer-trigger-python-azd`,Python v2项目布局。
- Azure Bicep schema 工具:已查询 Microsoft.Web/sites、Microsoft.App/containerApps。

## 5. 执行交接

计划保存后由用户选择 subagent-driven-development 或 executing-plans。
若用户不可用,不把文档批准误解为可以跳过云授权/删除确认门槛。
未选择执行方式时,停留在计划已就绪状态,不要自动创建云资源。
