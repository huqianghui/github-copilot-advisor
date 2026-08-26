# Teams 渠道迁移到 Microsoft 365 Agents SDK 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Teams channel adapter 从已归档的 Bot Framework SDK(`botbuilder-*`)迁移到 Microsoft 365 Agents SDK(`microsoft-agents-*`),行为完全等价、硬切换环境变量。

**Architecture:** 本仓库已把「纯 dict 逻辑」(`extract.py`/`render.py`)与「SDK 桥接层」(`bot.py`/`app.py`/`__main__.py`/`config.py`)分离。迁移只触及桥接层:`bot.py` 从 `ActivityHandler` 子类改为 `register_handlers(agent_app, core)` + `@agent_app.activity("message")` 装饰器;`app.py` 用 `start_agent_process` + `jwt_authorization_decorator`;`__main__.py` 成为完整 composition root(`load_configuration_from_env → MsalConnectionManager → CloudAdapter → MemoryStorage → Authorization → AgentApplication`)。`extract.py`/`render.py`/`agent/`/`shared/` 零改动,`test_extract.py`/`test_render.py` 作为行为等价性回归 oracle。

**Tech Stack:** Python ≥3.10,`microsoft-agents-hosting-core` / `-hosting-aiohttp` / `-authentication-msal` / `-activity`(均 1.4.0),`aiohttp`,`uv` workspace,`pytest` + `pytest-asyncio`(`asyncio_mode = "auto"`)。

---

## SDK 事实(已针对已安装的 1.4.0 实证确认,非假设)

本计划所依赖的每个 API 都已 introspect 真实安装包确认,**无遗留「实现期确认」**:

1. **导入路径**(下划线,非点):
   - `from microsoft_agents.activity import Activity, load_configuration_from_env`
   - `from microsoft_agents.hosting.core import AgentApplication, TurnContext, TurnState, Authorization, MemoryStorage`
   - `from microsoft_agents.hosting.aiohttp import CloudAdapter, start_agent_process, jwt_authorization_decorator`
   - `from microsoft_agents.authentication.msal import MsalConnectionManager`
2. **`start_agent_process(request, agent_application, adapter)`** —— 三个位置参数,内部即 `adapter.process(request, agent_application)`。它**不读 app state**;传对象进去。
3. **JWT app-state 契约**:`jwt_authorization_decorator` 内部读 `request.app.get("agent_configuration")`。缺失则 HTTP 500「Agent Authentication configuration not found」。因此 `app["agent_configuration"]` 是**必需的精确键名**。匿名路径:当 `agent_configuration.ANONYMOUS_ALLOWED` 为 True 且无 `Authorization` 头,装饰器返回匿名 claims 并放行 —— 本地/emulator 无需真 JWT,**也无需条件性跳过装饰器**。
4. **`MsalConnectionManager.get_default_connection_configuration()`** —— 取 `agent_configuration` 的确切访问器(已确认)。
5. **`AgentApplication` 构造**:需要 `connection_manager`,否则抛 `ApplicationError: requires a connection_manager`。`storage=`/`adapter=` 经 `**kwargs` 进 `ApplicationOptions`。单元测试可用 `AgentApplication[TurnState](storage=MemoryStorage(), adapter=None, connection_manager=<裸 stub 对象>)`,无需 MSAL/env。
6. **两个行为改变型默认值(必须覆盖以保行为等价)**:
   - `ApplicationOptions.start_typing_timer` 默认 **True** → SDK 自动发 typing。handler 已手动发 typing,故设 **False**。
   - `ApplicationOptions.remove_recipient_mention` 默认 **True** → SDK 从 `context.activity.text` 剥离 bot @提及(仅改 `.text`,不动 mention entity)。为保 `extract.strip_mentions` 为唯一剥离来源,设 **False**。
   - 二者经 `AgentApplication[TurnState](..., start_typing_timer=False, remove_recipient_mention=False)` 传入(kwargs → ApplicationOptions)。
7. `@agent_app.activity("message")` 通过 selector 闭包匹配 `context.activity.type == "message"`,装饰器**原样返回 handler** —— 故 `register_handlers` 返回该 handler 供单测直接调用可行。

---

## 文件结构图

### 不变(零改动 —— 回归 oracle)
- `channels/teams/src/teams_adapter/extract.py` —— 纯 dict:`should_respond` / `strip_mentions` / `to_advisor_request` / `_bot_mentioned`
- `channels/teams/src/teams_adapter/render.py` —— 纯 dict:`render_reply`,`MAX_MARKDOWN_CHARS=6000`
- `channels/teams/tests/test_extract.py`、`channels/teams/tests/test_render.py`
- `agent/**`(含 `factory.build_advisor` 与 `_channel_id_holder`/`_is_group_holder`/`set_current_*`)
- `shared/**`(`advisor_shared/messages.py` 契约)

### 迁移
| 文件 | 职责(迁移后) |
|------|----------------|
| `channels/teams/pyproject.toml` | 依赖:`microsoft-agents-*`(4 个)+ `aiohttp`;移除 `botbuilder-*` |
| `channels/teams/src/teams_adapter/bot.py` | `register_handlers(agent_app, core)`;`_activity_to_dict` 一行 `model_dump`;`@agent_app.activity("message")` |
| `channels/teams/src/teams_adapter/app.py` | `create_app(agent_app, adapter, agent_configuration)`;`start_agent_process` + `jwt_authorization_decorator`;注入 `app["agent_configuration"]` |
| `channels/teams/src/teams_adapter/__main__.py` | 完整 composition root + `build_agent_app()` + `_configure_logging()` |
| `channels/teams/tests/test_bot.py` | 直接调用返回的 handler + 一条 wiring 断言 |
| `channels/teams/tests/test_app.py` | `/healthz` 公开 200;`/api/messages` 经匿名 auth 路由到 `start_agent_process` |

### 删除
- `channels/teams/src/teams_adapter/config.py`(`TeamsBotConfig`)
- `channels/teams/tests/test_config.py`(若存在)

### 文档/环境/锁
- `.env.example`、`docs/teams-setup.md`、`docs/superpowers/specs/2026-08-21-copilot-advisor-design.md` §8.2
- `uv.lock`(仓库根)—— 随依赖更新重新解析并提交

---

## Task 1: 替换依赖并重新解析锁文件

**Files:**
- Modify: `channels/teams/pyproject.toml:5-11`(dependencies)
- Modify: `uv.lock`(仓库根,由 `uv sync` 生成)

- [ ] **Step 1: 改写 dependencies**

将 `channels/teams/pyproject.toml` 的 `[project]` 段改为:

```toml
[project]
name = "advisor-teams"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "advisor-agent",
    "advisor-shared",
    "microsoft-agents-hosting-core>=1.4,<2",
    "microsoft-agents-hosting-aiohttp>=1.4,<2",
    "microsoft-agents-authentication-msal>=1.4,<2",
    "microsoft-agents-activity>=1.4,<2",
    "aiohttp>=3.9",
]
```

(不含 `microsoft-agents-hosting-teams` —— YAGNI。`[build-system]` 与 `[tool.hatch.build.targets.wheel]` 段保持不变。)

- [ ] **Step 2: 重新解析 workspace 并更新 uv.lock**

Run: `cd /c/Codes/github-copilot-advisor && uv sync --all-packages`
Expected: 成功解析,拉取 4 个 `microsoft-agents-*==1.4.0`;`uv.lock` 被更新。若耗时较长(首次下载),给足超时(可后台运行)。

- [ ] **Step 3: 验证无 botbuilder 残留**

Run: `cd /c/Codes/github-copilot-advisor && grep -c botbuilder uv.lock; grep -rc botbuilder channels/ 2>/dev/null | grep -v ':0' || echo "channels clean"`
Expected: `uv.lock` 的计数为 `0`;`channels/` 输出 `channels clean`(注:此时 `bot.py` 等尚未改,若仍有 `botbuilder` 引用属正常 —— 本步只确认 `uv.lock` 已无 `botbuilder`。允许 channels 暂存残留,Task 3-5 会清掉)。

> 说明:此 Task 先改依赖树与锁文件;源码在后续 Task 逐个改。锁文件在此提交是安全的,因为运行时代码要到 `__main__` 改完才会 import 新包。

- [ ] **Step 4: Commit**

```bash
cd /c/Codes/github-copilot-advisor
git add channels/teams/pyproject.toml uv.lock
git commit -m "chore(teams): swap Bot Framework deps for M365 Agents SDK, relock

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: 迁移 `bot.py` —— handler 注册 + Activity 转换

**Files:**
- Modify: `channels/teams/src/teams_adapter/bot.py`(整文件重写)
- Test: `channels/teams/tests/test_bot.py`(下一个 Task)

- [ ] **Step 1: 重写 `bot.py`**

整文件替换为:

```python
# channels/teams/src/teams_adapter/bot.py
"""Teams handler 注册:触发判定 → typing → agent core → 渲染回复(spec 8.2)。
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
    # Pydantic Activity → Bot-Schema-aliased dict,使 extract/render 继续看到
    # conversationType / channelData / channelId(camelCase),而非 snake_case。
    return activity.model_dump(by_alias=True, exclude_none=True)


def register_handlers(agent_app: AgentApplication, core):
    @agent_app.activity("message")
    async def on_message(context: TurnContext, _state: TurnState):
        bot_id = context.activity.recipient.id
        activity = _activity_to_dict(context.activity)
        respond = should_respond(activity, bot_id)
        conversation = activity.get("conversation") or {}
        logger.info(
            "message activity channel=%s conversation_type=%s is_group=%s "
            "mentions=%d respond=%s",
            activity.get("channelId"),
            conversation.get("conversationType"),
            conversation.get("isGroup"),
            sum(1 for entity in activity.get("entities") or []
                if entity.get("type") == "mention"),
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
            reply = {"type": "message", "text": FALLBACK_MESSAGE,
                     "entities": []}
        await context.send_activity(Activity(
            type=reply["type"], text=reply["text"],
            entities=reply["entities"] or None))

    return on_message
```

- [ ] **Step 2: 语法/导入 smoke check**

Run: `cd /c/Codes/github-copilot-advisor && uv run python -c "import teams_adapter.bot"`
Expected: 无输出、退出 0(能 import 说明新包与符号名正确)。

- [ ] **Step 3: Commit**

```bash
cd /c/Codes/github-copilot-advisor
git add channels/teams/src/teams_adapter/bot.py
git commit -m "feat(teams): register_handlers with @agent_app.activity, drop ActivityHandler

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: 迁移 `test_bot.py`

**Files:**
- Modify: `channels/teams/tests/test_bot.py`(整文件重写)

测试策略:构造真实 `AgentApplication[TurnState]`(用裸 stub `connection_manager`,见 SDK 事实 #5),`handler = register_handlers(agent_app, core)`,再用假 `TurnContext`(其 `.activity` 为携带 `recipient` 的 Pydantic `Activity`)**直接以两个参数调用** `await handler(ctx, fake_state)`。额外一条断言确认 handler 注册为 `"message"` activity。

- [ ] **Step 1: 重写 `test_bot.py`**

整文件替换为:

```python
# channels/teams/tests/test_bot.py
from advisor_shared.messages import AdvisorResponse
from microsoft_agents.activity import Activity
from microsoft_agents.hosting.core import (
    AgentApplication,
    MemoryStorage,
    TurnState,
)
from teams_adapter.bot import register_handlers

BOT_ID = "28:bot"


class StubConnectionManager:
    """AgentApplication 要求 connection_manager 存在;handler 级测试不触发认证。"""


class FakeTurnContext:
    def __init__(self, activity: Activity):
        self.activity = activity
        self.sent = []

    async def send_activity(self, activity):
        self.sent.append(activity)


class StubCore:
    def __init__(self, response=None, error=None):
        self.response = response or AdvisorResponse(markdown="答案")
        self.error = error
        self.requests = []

    async def handle(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.response


def _agent_app():
    return AgentApplication[TurnState](
        storage=MemoryStorage(),
        adapter=None,
        connection_manager=StubConnectionManager(),
        start_typing_timer=False,
        remove_recipient_mention=False,
    )


def group_activity(mentions_bot=True) -> Activity:
    entities = ([{"type": "mention", "mentioned": {"id": BOT_ID, "name": "A"},
                  "text": "<at>A</at>"}] if mentions_bot else [])
    return Activity.model_validate({
        "type": "message",
        "text": "<at>A</at> 登录失败",
        "recipient": {"id": BOT_ID, "name": "bot"},
        "entities": entities,
        "conversation": {"id": "19:c;messageid=1",
                         "conversationType": "channel"},
        "channelData": {"channel": {"id": "19:c"}},
        "from": {"id": "29:u", "name": "n"},
    })


def personal_activity() -> Activity:
    return Activity.model_validate({
        "type": "message",
        "text": "登录失败",
        "recipient": {"id": BOT_ID, "name": "bot"},
        "conversation": {"id": "19:personal", "conversationType": "personal"},
        "from": {"id": "29:u", "name": "n"},
    })


def test_handler_registered_for_message_activity():
    # wiring 断言:注册后 agent_app 至少多了一条路由,且返回的 handler 是我们的函数
    core = StubCore()
    app = _agent_app()
    before = len(app._routes)
    handler = register_handlers(app, core)
    assert len(app._routes) == before + 1
    assert handler.__name__ == "on_message"


async def test_responds_with_typing_then_answer():
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    ctx = FakeTurnContext(group_activity())
    await handler(ctx, TurnState())
    assert len(core.requests) == 1
    assert core.requests[0].text == "登录失败"
    assert ctx.sent[0].type == "typing"
    assert len(ctx.sent) == 2


async def test_responds_to_personal_activity():
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    ctx = FakeTurnContext(personal_activity())
    await handler(ctx, TurnState())
    assert len(core.requests) == 1
    assert core.requests[0].text == "登录失败"
    assert core.requests[0].user_id == "29:u"
    assert len(ctx.sent) == 2


async def test_ignores_group_message_without_mention():
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    ctx = FakeTurnContext(group_activity(mentions_bot=False))
    await handler(ctx, TurnState())
    assert core.requests == [] and ctx.sent == []


async def test_core_error_sends_fallback():
    from advisor_agent.core import FALLBACK_MESSAGE
    core = StubCore(error=RuntimeError("boom"))
    handler = register_handlers(_agent_app(), core)
    ctx = FakeTurnContext(group_activity())
    await handler(ctx, TurnState())
    assert FALLBACK_MESSAGE in ctx.sent[-1].text


async def test_sets_current_channel_id_and_is_group():
    from advisor_agent.factory import _channel_id_holder, _is_group_holder
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    ctx = FakeTurnContext(group_activity())
    await handler(ctx, TurnState())
    assert _channel_id_holder["value"] == "19:c"
    assert _is_group_holder["value"] is True
```

- [ ] **Step 2: 运行 test_bot.py,先确认 wiring 断言里的 `_routes` 名对**

Run: `cd /c/Codes/github-copilot-advisor && uv run pytest channels/teams/tests/test_bot.py -v`
Expected: 全部 PASS。

> 若 `app._routes` 属性名不符(不同 minor 版本可能改名),用以下命令 introspect 真实属性名并把 `test_handler_registered_for_message_activity` 里的 `_routes` 替换为实际的路由列表属性:
> Run: `cd /c/Codes/github-copilot-advisor && uv run python -c "from microsoft_agents.hosting.core import AgentApplication, MemoryStorage, TurnState; a=AgentApplication[TurnState](storage=MemoryStorage(), adapter=None, connection_manager=type('C',(),{})()); print([n for n in dir(a) if 'route' in n.lower()])"`
> Expected: 打印含 `_routes`(或等价名)。已针对 1.4.0 确认为 `_routes`;此步是版本漂移防护。

- [ ] **Step 3: Commit**

```bash
cd /c/Codes/github-copilot-advisor
git add channels/teams/tests/test_bot.py
git commit -m "test(teams): migrate bot tests to Agents SDK Activity + direct handler call

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: 迁移 `app.py` —— HTTP 端点 + 选择性 JWT

**Files:**
- Modify: `channels/teams/src/teams_adapter/app.py`(整文件重写)
- Test: `channels/teams/tests/test_app.py`(下一个 Task)

- [ ] **Step 1: 重写 `app.py`**

整文件替换为:

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
    # jwt_authorization_decorator 从 app["agent_configuration"] 读认证配置(SDK 契约,精确键名)。
    app["agent_configuration"] = agent_configuration
    app["agent_app"] = agent_app
    app["adapter"] = adapter
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/healthz", healthz)
    return app
```

- [ ] **Step 2: 语法/导入 smoke check**

Run: `cd /c/Codes/github-copilot-advisor && uv run python -c "import teams_adapter.app"`
Expected: 无输出、退出 0。

- [ ] **Step 3: Commit**

```bash
cd /c/Codes/github-copilot-advisor
git add channels/teams/src/teams_adapter/app.py
git commit -m "feat(teams): start_agent_process endpoint with per-route JWT, public healthz

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: 迁移 `test_app.py`

**Files:**
- Modify: `channels/teams/tests/test_app.py`(整文件重写)

策略:`/healthz` 无 auth 返回 200 锁定「公开」不变。`/api/messages` 用一个 `ANONYMOUS_ALLOWED=True` 的 `AgentAuthConfiguration` 作为 `agent_configuration`,使 JWT 装饰器匿名放行;monkeypatch `start_agent_process` 以断言路由命中且 app-state 键已注入(避免依赖真实 adapter 网络处理)。

- [ ] **Step 1: 重写 `test_app.py`**

整文件替换为:

```python
# channels/teams/tests/test_app.py
import teams_adapter.app as app_module
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from microsoft_agents.hosting.core.authorization.agent_auth_configuration import (
    AgentAuthConfiguration,
)
from teams_adapter.app import create_app


def _anon_config() -> AgentAuthConfiguration:
    # 匿名允许 → JWT 装饰器无 Authorization 头也放行(本地/emulator 同款路径)。
    return AgentAuthConfiguration(
        client_id="test", tenant_id="test", anonymous_allowed=True,
    )


async def test_healthz_ok_without_auth():
    app = create_app(object(), object(), _anon_config())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200
        assert await resp.text() == "ok"


async def test_messages_routes_to_start_agent_process(monkeypatch):
    seen = {}

    async def fake_start(request, agent_application, adapter):
        seen["agent_app"] = agent_application
        seen["adapter"] = adapter
        return web.Response(status=201)

    monkeypatch.setattr(app_module, "start_agent_process", fake_start)

    sentinel_app, sentinel_adapter = object(), object()
    app = create_app(sentinel_app, sentinel_adapter, _anon_config())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/messages", json={"type": "message"})
        assert resp.status == 201
    assert seen["agent_app"] is sentinel_app
    assert seen["adapter"] is sentinel_adapter
```

- [ ] **Step 2: 运行 test_app.py**

Run: `cd /c/Codes/github-copilot-advisor && uv run pytest channels/teams/tests/test_app.py -v`
Expected: 两个测试 PASS。

> 若 `AgentAuthConfiguration` 导入路径在此 minor 版本不同,introspect:
> Run: `cd /c/Codes/github-copilot-advisor && uv run python -c "from microsoft_agents.hosting.core.authorization.agent_auth_configuration import AgentAuthConfiguration; print('ok')"`
> Expected: 打印 `ok`(已针对 1.4.0 确认)。
> 若 `test_messages_routes_to_start_agent_process` 仍返回 500,说明匿名放行未生效 —— 确认 `_anon_config()` 的 `anonymous_allowed=True` 且 `create_app` 已把它存入 `app["agent_configuration"]`。

- [ ] **Step 3: Commit**

```bash
cd /c/Codes/github-copilot-advisor
git add channels/teams/tests/test_app.py
git commit -m "test(teams): assert public healthz and anonymous-auth message routing

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: 迁移 `__main__.py`(composition root)+ 删除 `config.py`

**Files:**
- Modify: `channels/teams/src/teams_adapter/__main__.py`(整文件重写)
- Delete: `channels/teams/src/teams_adapter/config.py`
- Delete: `channels/teams/tests/test_config.py`(若存在)

- [ ] **Step 1: 重写 `__main__.py`**

整文件替换为:

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
        start_typing_timer=False,       # behavior-preserving:handler 手动发 typing
        remove_recipient_mention=False,  # behavior-preserving:extract.strip_mentions 唯一剥离来源
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

- [ ] **Step 2: 删除 config.py 及其测试**

Run: `cd /c/Codes/github-copilot-advisor && git rm channels/teams/src/teams_adapter/config.py && (git rm channels/teams/tests/test_config.py 2>/dev/null || echo "no test_config.py")`
Expected: `config.py` 被删除;`test_config.py` 若不存在则打印提示。

- [ ] **Step 3: 确认无残留引用**

Run: `cd /c/Codes/github-copilot-advisor && grep -rn "TeamsBotConfig\|teams_adapter.config\|from teams_adapter import config" channels/ || echo "no config refs"`
Expected: `no config refs`。

- [ ] **Step 4: import smoke check**

Run: `cd /c/Codes/github-copilot-advisor && uv run python -c "import teams_adapter.__main__"`
Expected: 无输出、退出 0(仅 import 不运行 `main()`,不需真实环境变量)。

- [ ] **Step 5: Commit**

```bash
cd /c/Codes/github-copilot-advisor
git add -A channels/teams/
git commit -m "feat(teams): composition root on M365 Agents SDK, drop TeamsBotConfig

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 7: 环境变量硬切换 + 文档 + spec 更新

**Files:**
- Modify: `.env.example:9-11`
- Modify: `docs/teams-setup.md`(§三.3 与冒烟清单前的正文)
- Modify: `docs/superpowers/specs/2026-08-21-copilot-advisor-design.md`(§8.2)

- [ ] **Step 1: 更新 `.env.example`**

将第 9-11 行的三行 `TEAMS_APP_*` 替换为:

```bash
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID=00000000-0000-0000-0000-000000000000
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET=xxx
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID=00000000-0000-0000-0000-000000000000
# 本地/emulator 联调可放开匿名(生产勿开):
# CONNECTIONS__SERVICE_CONNECTION__SETTINGS__ANONYMOUS_ALLOWED=True
```

(其余行 `GITHUB_TOKEN` / `AZURE_*` / `TAVILY_API_KEY` / `BRAVE_API_KEY` / `ESCALATION_CONFIG` 不变。均为占位符,不含真实密钥。)

- [ ] **Step 2: 更新 `docs/teams-setup.md` §三.3 的 env 注释块**

将「### 3. 配置环境变量并启动」代码块中的 `.env` 注释行:

```powershell
# .env 中至少需要:
# TEAMS_APP_ID=<Microsoft App ID>
# TEAMS_APP_PASSWORD=<client secret>
# TEAMS_APP_TENANT_ID=<Directory (tenant) ID>
# 以及 AZURE_OPENAI_* / AZURE_SEARCH_* / GITHUB_TOKEN / TAVILY_API_KEY
```

替换为:

```powershell
# .env 中至少需要(SingleTenant 的 tenant id 现落在 ...__TENANTID):
# CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID=<Microsoft App ID>
# CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET=<client secret>
# CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID=<Directory (tenant) ID>
# 以及 AZURE_OPENAI_* / AZURE_SEARCH_* / GITHUB_TOKEN / TAVILY_API_KEY
```

启动命令(`uv run --env-file .env python -m teams_adapter`)、§一(Azure Bot 注册,复用同一 App ID/secret/tenant)、§二(dev tunnel)、§四(冒烟清单六项)均**不变**。

- [ ] **Step 3: 更新设计 spec §8.2 的技术标注**

在 `docs/superpowers/specs/2026-08-21-copilot-advisor-design.md` §8.2 中,把「技术:Bot Framework SDK(Python)」改为「技术:Microsoft 365 Agents SDK(Python)」,并加一句迁移动机注记(Bot Framework SDK 已归档/2025-12-31 后停止支持)。数据流条目(触发/conversation_key/render)不变 —— 描述行为而非 SDK。

Run(定位精确行): `cd /c/Codes/github-copilot-advisor && grep -n "Bot Framework SDK" docs/superpowers/specs/2026-08-21-copilot-advisor-design.md`
Expected: 打印 §8.2 相关行号供精确编辑。

- [ ] **Step 4: 验证 env 硬切换无残留**

Run: `cd /c/Codes/github-copilot-advisor && grep -rn "TEAMS_APP_" .env.example docs/ channels/ || echo "no TEAMS_APP_ refs"`
Expected: `no TEAMS_APP_ refs`。

- [ ] **Step 5: Commit**

```bash
cd /c/Codes/github-copilot-advisor
git add .env.example docs/teams-setup.md docs/superpowers/specs/2026-08-21-copilot-advisor-design.md
git commit -m "docs(teams): env var hard cutover to CONNECTIONS__*, SDK name in design spec

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 8: 全量验证(先证据,后声明「完成」)

**Files:** 无(仅验证)

- [ ] **Step 1: 确认无 `microsoft.agents` 点式导入(官方头号迁移坑)**

Run: `cd /c/Codes/github-copilot-advisor && grep -rn "microsoft\.agents" channels/ || echo "no dotted imports"`
Expected: `no dotted imports`(只允许下划线 `microsoft_agents`)。

- [ ] **Step 2: 确认全仓库无 `botbuilder` 残留**

Run: `cd /c/Codes/github-copilot-advisor && grep -rn botbuilder channels/ ; grep -c botbuilder uv.lock`
Expected: `channels/` 无输出;`uv.lock` 计数 `0`。

- [ ] **Step 3: Teams 渠道测试全绿(含不变的 extract/render oracle)**

Run: `cd /c/Codes/github-copilot-advisor && uv run pytest channels/teams/tests -v`
Expected: 全部 PASS,包含 `test_extract.py` / `test_render.py`(未改)与迁移后的 `test_bot.py` / `test_app.py`。

- [ ] **Step 4: 全仓库测试绿(确认 agent/shared/ingestion 未受影响)**

Run: `cd /c/Codes/github-copilot-advisor && uv run pytest`
Expected: 全部 PASS。

- [ ] **Step 5: 记录手动冒烟为范围外**

`docs/teams-setup.md` §四 的六项冒烟清单需真实 Azure Bot + Teams 租户,经 dev tunnel 人工逐项验证,**不在本自动化计划范围内**。实施完成后应提示用户执行该清单确认端到端行为。

> 无需额外 commit —— 本 Task 仅验证。若任一步失败,回到对应 Task 修复后重跑。

---

## 自审查记录(writing-plans 自查)

**1. Spec 覆盖:** spec §3 变更表的每一行都有对应 Task —— pyproject/uv.lock(T1)、bot.py(T2)、test_bot.py(T3)、app.py(T4)、test_app.py(T5)、__main__.py + config.py 删除(T6)、env + docs + design spec(T7)。spec §9 验证清单 7 项映射到 T8 的 5 步 + T1 的锁验证 + 手动冒烟标注。spec §10 YAGNI(不含 hosting-teams)已在 T1 依赖列表落实。

**2. Placeholder 扫描:** 无 TBD/TODO/「类似 Task N」;每个改代码的步骤都给出完整可粘贴代码;每条命令都有 Expected。两处版本漂移防护(T3 `_routes`、T5 `AgentAuthConfiguration` 导入路径)给出了 introspect 回退命令而非留空。

**3. 类型/名字一致性:** `register_handlers(agent_app, core)`(T2)与 T3 调用、T6 调用一致;`create_app(agent_app, adapter, agent_configuration)`(T4)与 T5 测试、T6 `main()` 一致;`build_agent_app()`(T6)返回三元组与 `main()` 解包一致;app-state 键 `agent_configuration`/`agent_app`/`adapter`(T4)与 SDK 事实 #3 及 T5 断言一致;`start_typing_timer=False`/`remove_recipient_mention=False` 在 T3 测试构造与 T6 生产构造两处一致。

**相较 spec 的实证修正(已并入本计划):** ① `AgentApplication` 需 `connection_manager`(spec 说 handler 测试「无需 adapter/auth」不准确 → T3 用 stub CM);② 新增两个 behavior-preserving kwargs(`start_typing_timer=False` / `remove_recipient_mention=False`)—— spec 未涵盖的 SDK 默认值行为改变;③ 确认 `app["agent_configuration"]` 是 SDK 精确契约键(非任意名),匿名放行经装饰器天然支持(spec 的「条件性跳过装饰器」回退不需要)。
