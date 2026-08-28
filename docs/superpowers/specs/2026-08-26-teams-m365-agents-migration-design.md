# Teams 渠道从 Bot Framework SDK 迁移到 Microsoft 365 Agents SDK

> 日期:2026-08-26
> 关联:`docs/superpowers/specs/2026-08-21-copilot-advisor-design.md` §8.2

## 1. 背景与动机

Teams 渠道当前基于 **Bot Framework SDK(Python)**(`botbuilder-*`)。该 SDK 已被微软
归档:GitHub 仓库不再更新维护,支持工单在 2025-12-31 后停止服务。微软的接替方案是
**Microsoft 365 Agents SDK**(`microsoft-agents-*`),官方提供从 Bot Framework SDK
的迁移指引。本次工作将 Teams adapter 迁移到 Agents SDK。

参考:[Azure Bot Framework SDK to Microsoft 365 Agents SDK migration guidance for
Python](https://learn.microsoft.com/microsoft-365/agents-sdk/bf-migration-python)。

## 2. 决策(已与用户确认)

| # | 决策 | 取值 |
|---|------|------|
| 1 | Handler 风格 | **AgentApplication + decorators**(`@agent_app.activity("message")`),非 compat `ActivityHandler`。这是 SDK 的前瞻路径,也是未来接入 Teams extension 的前提 |
| 2 | 范围 | 代码 + 文档 + 环境变量,**硬切换**(不保留旧 `TEAMS_APP_*` 兼容 shim) |
| 3 | 功能采用 | **Behavior-preserving 迁移**:精确复刻当前行为(触发逻辑、手动 typing、fallback、渲染),不引入 SDK 的 typing/state/Teams-extension 新特性 |
| 4 | 结构方案 | **Approach A**:保持现有文件布局,在每个文件内替换 SDK,不重构成单文件、不引入 SDK 抽象层 |

## 3. 架构边界与变更范围

本仓库已有的「纯逻辑 vs. SDK 外壳」分层是本次迁移只触及约 4 个文件的原因:
`extract.py` / `render.py` 是纯 dict 函数,不 import SDK;`bot.py` / `app.py` /
`__main__.py` / `config.py` 才是 SDK 桥接层。依赖方向 `channels/teams → agent →
shared` 保持不变,不引入反向边。

### 不变(零改动)

- `extract.py`、`render.py` —— 纯 dict 函数
- `advisor_shared/messages.py`(`AdvisorRequest` / `AdvisorResponse` 契约)
- 整个 `agent/` core,包括 `factory.build_advisor()` 与 `set_current_channel_id` /
  `set_current_is_group` 两个 holder
- `test_extract.py`、`test_render.py` —— 作为行为等价性的回归 oracle,保持绿

### 迁移(桥接层)

| 文件 | 处理 |
|------|------|
| `channels/teams/pyproject.toml` | 依赖替换为 `microsoft-agents-hosting-core`、`microsoft-agents-hosting-aiohttp`、`microsoft-agents-authentication-msal`、`microsoft-agents-activity`、`aiohttp`。**不含 `microsoft-agents-hosting-teams`** —— 待真正使用 `TeamsAgentExtension` 时再加(YAGNI) |
| `uv.lock`(仓库根) | 随 `pyproject.toml` 一同更新并提交(见 §8) |
| `bot.py` | 从 `ActivityHandler` 子类改为 `register_handlers(agent_app, core)` 函数,注册 `@agent_app.activity("message")`;将 Pydantic `Activity` 转为保留 Bot Schema 别名的 dict;`bot_id` 每轮从 `context.activity.recipient.id` 取 |
| `app.py` | `/api/messages` 改为 `start_agent_process`,并用 `jwt_authorization_decorator` **仅**保护该路由;`/healthz` 保持公开;显式注入 app state |
| `__main__.py` | 完整 composition root:`load_configuration_from_env → MsalConnectionManager → CloudAdapter → MemoryStorage → Authorization → AgentApplication`,再 `register_handlers`,再 host |
| `config.py` + `test_config.py` | **删除** —— SDK 直接读 `CONNECTIONS__*` |
| `test_bot.py`、`test_app.py` | 迁移(见 §5) |

## 4. 详细设计

### 4.1 `bot.py`:handler 注册 + Activity 转换

```python
# channels/teams/src/teams_adapter/bot.py
"""Teams handler registration: 触发判定 → typing → agent core → 渲染回复.
纯逻辑仍在 extract/render;此处只做 Agents SDK 对象与 dict 的桥接。"""
import logging

from microsoft_agents.activity import Activity
from microsoft_agents.hosting.core import AgentApplication, TurnContext, TurnState

from advisor_agent.core import FALLBACK_MESSAGE
from advisor_agent.factory import set_current_channel_id, set_current_is_group
from teams_adapter.extract import should_respond, to_advisor_request
from teams_adapter.render import render_reply

logger = logging.getLogger(__name__)


def _activity_to_dict(activity: Activity) -> dict:
    # Pydantic Activity → Bot-Schema-aliased dict,使 extract.py 继续看到
    # conversationType / channelData(camelCase),而非 snake_case。
    return activity.model_dump(by_alias=True, exclude_none=True)


def register_handlers(agent_app: AgentApplication, core):
    @agent_app.activity("message")
    async def on_message(context: TurnContext, _state: TurnState):
        bot_id = context.activity.recipient.id          # 不再手工构造 28:{app_id}
        activity = _activity_to_dict(context.activity)
        respond = should_respond(activity, bot_id)
        conversation = activity.get("conversation") or {}
        logger.info(
            "message activity channel=%s conversation_type=%s is_group=%s "
            "mentions=%d respond=%s",
            activity.get("channelId"),
            conversation.get("conversationType"),
            conversation.get("isGroup"),
            sum(1 for e in activity.get("entities") or []
                if e.get("type") == "mention"),
            respond,
        )
        if not respond:
            return
        await context.send_activity(Activity(type="typing"))
        request = to_advisor_request(activity, bot_id)
        set_current_channel_id(request.channel_id)
        set_current_is_group(request.is_group)
        try:
            response = await core.handle(request)
            reply = render_reply(response)
        except Exception:
            logger.exception("core.handle failed")
            reply = {"type": "message", "text": FALLBACK_MESSAGE, "entities": []}
        await context.send_activity(Activity(
            type=reply["type"], text=reply["text"],
            entities=reply["entities"] or None))

    return on_message   # 返回已注册 handler,供单元测试直接调用(见 §5)
```

关键点:

1. **不再是 `ActivityHandler` 子类**,改为 `register_handlers(agent_app, core)` + 装饰器。
2. **`_activity_to_dict` 收敛为一行**:旧实现要在 `.serialize()` / `.as_dict()` 间分支,
   并手工把 mention entity 的 `additional_properties` 合并回去。Pydantic 的
   `Activity.model_dump(by_alias=True, exclude_none=True)` 取代全部逻辑;`by_alias=True`
   输出 Bot Schema 线上字段名(`conversationType`、`channelData`、`channelId`),正是
   `extract.py` / `render.py` 期待的,因而这两个文件保持不变。
3. **`bot_id = context.activity.recipient.id`,每轮读取**:不再构造 `28:{client_id}`,
   不重新解析环境变量。这是 Teams 实际寻址该 activity 的 id,与 `_bot_mentioned` 里
   mention 的 `mentioned.id` 天然一致。
4. **`turn_context` → `context`,`send_activity` 不变**;typing 仍是手动
   `Activity(type="typing")`(behavior-preserving)。
5. `register_handlers` **返回**已注册的 handler,便于单元测试直接调用。

**实现期需确认(非本设计假设):** `model_dump(by_alias=True)` 是否忠实往返 Teams 发来的
深层嵌套 channelData。旧代码的 `additional_properties` 合并正是因为 Bot Framework 的
`.serialize()` 会丢弃未建模字段。Pydantic v2 + `exclude_none` 应保留已建模字段;若出现
gap,回退方案是 `model_dump(by_alias=True, mode="json")` 或读取 `activity.model_extra`。
实现任务将以 `test_extract.py` fixture(已编码所需精确形状)与一条真实 channel activity
做断言。

### 4.2 `app.py`:HTTP 端点、选择性 JWT 保护、app state

```python
# channels/teams/src/teams_adapter/app.py
"""aiohttp 入口:Agents SDK 消息端点 + 公开健康检查。
认证只保护 /api/messages;/healthz 保持匿名(behavior-preserving)。"""
from aiohttp import web
from microsoft_agents.hosting.aiohttp import (
    jwt_authorization_decorator,
    start_agent_process,
)


def create_app(agent_app, adapter, agent_configuration) -> web.Application:
    @jwt_authorization_decorator          # 仅此路由校验 Azure Bot JWT
    async def messages(request: web.Request):
        return await start_agent_process(
            request,
            request.app["agent_app"],
            request.app["adapter"],
        )

    async def healthz(_: web.Request) -> web.Response:   # 公开,无 JWT
        return web.Response(text="ok")

    app = web.Application()
    # JWT 校验与 start_agent_process 从 app state 读取,显式注入:
    app["agent_configuration"] = agent_configuration
    app["agent_app"] = agent_app
    app["adapter"] = adapter
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/healthz", healthz)
    return app
```

关键点:

1. **选择性保护**:JWT 校验是 `messages` 的路由装饰器,**不是**全局
   `web.Application(middlewares=[jwt_authorization_middleware])`。因此 `/healthz` 与今天
   一样保持匿名 —— behavior-preserving。为此用 `jwt_authorization_decorator`(已确认存在于
   aiohttp 包)而非 middleware。
2. **显式 app-state 注入**:`create_app` 把 `agent_configuration` / `agent_app` /
   `adapter` 存入 `app[...]`。JWT 校验从 app state 读认证配置;`start_agent_process` 读
   `agent_app` / `adapter`。显式注入而非依赖 import-time 全局,也让函数可单测。
3. **Authorization 与 JWT 是两件事**:`Authorization`(`AgentApplication` 的 OAuth 能力,
   在 `__main__.py` 构造)与 JWT 装饰器(校验 Azure Bot 发来的入站请求)互不替代。`app.py`
   只碰 JWT 侧;`Authorization` 在 `agent_app` 内。

**实现期需确认:**

- SDK 的 JWT 校验器与 `start_agent_process` 期望的 **app-state 键名**。参考文档列出了函数
  但未列 state-key 契约;实现任务将 grep 已安装包源码确认字面键名(如 `agent_configuration`
  vs `agent_auth_configuration`)并对齐 `create_app`。composition root 按 SDK 实际读取的
  键写入。
- `jwt_authorization_decorator` 是否**尊重匿名/本地开发路径**
  (`CONNECTIONS__SERVICE_CONNECTION__SETTINGS__ANONYMOUS_ALLOWED=True`),使 emulator /
  dev-tunnel 流程无需真实 JWT。若装饰器忽略匿名模式,本地开发按条件应用(匿名启用时跳过装饰器)。

### 4.3 `__main__.py`:完整 composition root

```python
# channels/teams/src/teams_adapter/__main__.py
"""启动 Teams adapter:完整 composition root。
需 CONNECTIONS__* 认证变量与 advisor agent 的全部环境变量。"""
import logging
from os import environ

from aiohttp import web
from microsoft_agents.activity import load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import CloudAdapter
from microsoft_agents.hosting.core import (
    AgentApplication,
    Authorization,
    MemoryStorage,
    TurnState,
)

from advisor_agent.factory import build_advisor
from teams_adapter.app import create_app
from teams_adapter.bot import register_handlers


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO)
    ms = logging.getLogger("microsoft_agents")
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    ms.addHandler(handler)
    ms.setLevel(logging.INFO)


def build_agent_app():
    config = load_configuration_from_env(environ)
    connection_manager = MsalConnectionManager(**config)
    adapter = CloudAdapter(connection_manager=connection_manager)
    storage = MemoryStorage()
    authorization = Authorization(storage, connection_manager, **config)
    agent_app = AgentApplication[TurnState](
        storage=storage,
        adapter=adapter,
        authorization=authorization,
        **config,
    )
    core = build_advisor(channel_name="teams")
    register_handlers(agent_app, core)
    agent_configuration = connection_manager.get_default_connection_configuration()
    return agent_app, adapter, agent_configuration


def main() -> None:
    _configure_logging()
    agent_app, adapter, agent_configuration = build_agent_app()
    app = create_app(agent_app, adapter, agent_configuration)
    web.run_app(app, port=int(environ.get("PORT", 3978)))


if __name__ == "__main__":
    main()
```

认证链恰为 `load_configuration_from_env → MsalConnectionManager → CloudAdapter →
MemoryStorage → Authorization → AgentApplication`,再 `register_handlers`,再 host。
`build_agent_app()` 从 `main()` 抽出,使测试可装配 app 而不绑定 socket。

**实现期需确认:** `MsalConnectionManager` 上取 `agent_configuration` 的确切访问器(设计中写作
`get_default_connection_configuration()`)—— 参考文档确认了 `AgentAuthConfiguration` 存在,
但未确认该方法名。实现任务据已安装包确认,并把 §4.2 的 app-state 键对齐到 JWT 校验器实际读取
的名字。`build_advisor()` 与两个 holder 不变。

## 5. 测试迁移

| 测试文件 | 处理 |
|----------|------|
| `test_extract.py`、`test_render.py` | **不变**。纯 dict 测试,行为等价性回归 oracle |
| `config.py`、`test_config.py` | **删除**。`TeamsBotConfig` 被 `CONNECTIONS__*` + `load_configuration_from_env` 取代 |
| `test_bot.py` | **迁移**。构造真实 `AgentApplication[TurnState]` + `MemoryStorage`(handler 级测试无需 adapter/auth),`handler = register_handlers(agent_app, core)`,再用假 `TurnContext`(其 `.activity` 为携带 `recipient` 的 Pydantic `Activity`,使 `recipient.id` 产出 bot id)驱动 handler。**调用时传两个参数**:`await handler(fake_context, fake_state)`(`fake_state` 为最小 `TurnState` 形状,handler 忽略之)。断言不变:typing-then-answer、personal 恒响应、group-without-mention 忽略、core-error → fallback、`_channel_id_holder`/`_is_group_holder` 副作用。fixture 新增 `recipient` 字段(决策 #4 的唯一行为后果)。**额外断言**:handler 确实注册为 `"message"` activity,避免只测函数而漏测 decorator wiring |
| `test_app.py` | **迁移**。断言 `/healthz` 无 auth 返回 200(锁定「healthz 保持公开」);断言 `/api/messages` 经 `start_agent_process` 路由。因 `start_agent_process` / JWT 为模块级函数,测试 monkeypatch 之(或启用匿名 auth)以验证路由与 app-state 键已填充 |

`test_bot.py` handler 调用采用**返回 handler 直接调用**(而非驱动 SDK 路由机制):保持真正的
单元测试(无 SDK 路由机器),并对齐今日 `bot.py` 的测法。同时以 wiring 断言覆盖 decorator
注册,确保「函数正确 + 注册正确」两者都被验证。实现期确认 introspect `"message"` 路由的确切
方式(据已安装包)。

## 6. 环境变量硬切换

不保留旧 `TEAMS_APP_*` 兼容 shim(决策 #2)。

| 移除(Bot Framework) | 新增(Agents SDK) |
|----------------------|-------------------|
| `TEAMS_APP_ID` | `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID` |
| `TEAMS_APP_PASSWORD` | `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET` |
| `TEAMS_APP_TENANT_ID` | `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID` |
| — | `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__ANONYMOUS_ALLOWED=True`(仅本地/emulator) |

`AZURE_OPENAI_*` / `AZURE_SEARCH_*` / `GITHUB_TOKEN` / `TAVILY_API_KEY`(由 `build_advisor`
消费)不受影响。

## 7. 文档与 spec 更新

| 文件 | 变更 |
|------|------|
| `.env.example` | 三行 `TEAMS_APP_*` 替换为 `CONNECTIONS__*` 块;加注释版 `ANONYMOUS_ALLOWED` 供本地开发 |
| `docs/teams-setup.md` | §一(Azure Bot 注册)不变 —— 复用同一 App ID/secret/tenant。§三.3(env + 启动):新变量名;注明 SingleTenant 的 tenant id 现落在 `...__TENANTID`。启动命令不变(`uv run --env-file .env python -m teams_adapter`)。冒烟清单(§四)不变 —— 同六项用户可见行为 |
| `docs/superpowers/specs/2026-08-21-copilot-advisor-design.md` §8.2 | 「技术:Bot Framework SDK(Python)」→「Microsoft 365 Agents SDK(Python)」;注明 Bot Framework SDK 已归档/停维护作为迁移动机。数据流条目(触发/conversation_key/render)不变 —— 描述的是行为而非 SDK |
| `channels/teams/pyproject.toml` | 依赖按 §3 替换(不含 `hosting-teams`) |
| `uv.lock`(仓库根) | 见 §8 |

## 8. 依赖锁定

`uv.lock` 是已提交产物,`pyproject.toml` 改依赖后必须重新解析并提交,否则 lock 仍引用
`botbuilder-*`,两态发散。

- `channels/teams/pyproject.toml` 中新 `microsoft-agents-*` 包使用相互兼容的版本范围。
- 运行 `uv sync --all-packages` 重新解析 workspace 并更新 `uv.lock`,由 lock 文件固定最终
  解析版本。
- `uv.lock` 与 `.env.example`、文档、spec 一并纳入提交。

## 9. 验证清单(先证据,后声明「完成」)

1. `uv sync --all-packages` 解析新依赖成功并更新 `uv.lock`;无 `botbuilder-*` 残留
   (`grep -r botbuilder channels/` → 空;`grep botbuilder uv.lock` → 空)。
2. `grep -r "microsoft.agents" channels/` → 空(仅下划线 `microsoft_agents` —— 官方列为
   头号迁移坑)。
3. `uv run pytest channels/teams/tests` 绿,含**不变**的 `test_extract.py` /
   `test_render.py`。
4. `test_bot.py`:typing-then-answer、personal 恒响应、group-without-mention 忽略、
   core-error→fallback、holder 副作用,**以及** `"message"` wiring 断言。
5. `test_app.py`:`/healthz` 无 auth 返回 200;`/api/messages` 路由到
   `start_agent_process`。
6. 全仓库测试(`uv run pytest`)绿 —— 确认 `agent/`、`shared/`、`ingestion/` 未受影响。
7. **手动**(需真实 Azure Bot + 租户,不在自动化范围):对迁移后 adapter 经 dev tunnel 逐项
   跑 `teams-setup.md` 的六项冒烟清单。

## 10. 范围外(YAGNI)

- `microsoft-agents-hosting-teams` / `TeamsAgentExtension`
- feedback 按钮、message extensions、task modules
- SDK 原生 typing / state(`ConversationState`)惯用法
- Cosmos / Blob storage(会话存储仍由 `AdvisorCore` 的 `InMemorySessionStore` 负责)

均待真正需要时再引入。
