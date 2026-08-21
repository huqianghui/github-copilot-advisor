# 计划 3:Teams Adapter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建 Teams 渠道薄壳:Bot Framework 消息端点 → 提取/转换为 AdvisorRequest → 调 agent core → 把 AdvisorResponse 渲染回 Teams(markdown + 引用 + mention entity),含 typing indicator 与触发规则。

**Architecture:** `channels/teams` 是独立子项目,依赖方向 `channels/teams → agent → shared`。零业务逻辑:所有智能在 agent core。协议转换分成三个纯函数模块(extract / render / trigger 规则),Bot Framework 的 SDK 对象只出现在最外层 `bot.py` + `app.py`,纯函数模块只接受原始 dict/字符串 —— 这样绝大部分逻辑不需要 Bot SDK 对象就能单测。

**Tech Stack:** Python 3.11+,botbuilder-core / botbuilder-schema ≥ 4.16(Bot Framework SDK),aiohttp(Bot Framework 官方搭配),advisor-agent(计划 2 产物)。

**Spec:** `docs/superpowers/specs/2026-08-21-copilot-advisor-design.md`(本计划实现其第 8 节)

## Global Constraints

- 依赖方向:`channels/teams → agent → shared`,禁止反向;不 import ingestion
- 触发规则(spec 8.2):group channel 仅 @提及响应;1:1 全部响应;其余忽略
- conversation_key 规则:一律取 Teams 的 `conversation.id` —— channel 回帖场景 Teams 返回的就是 `"{channel_id};messageid={根消息ID}"`,天然实现"同一 reply thread 共享会话"(spec 8.2);1:1 场景就是会话 id
- 收到消息先发 typing activity,再跑 agent(spec 8.2:检索+LLM 5-15s)
- mention entity 由 adapter 拼装,agent core 只给 MentionDirective(spec 8.1)
- 凭据环境变量:`TEAMS_APP_ID`、`TEAMS_APP_PASSWORD`(Bot Framework 应用注册);其余沿用计划 2
- 单元测试不依赖真实 Bot Framework 连接;Teams 真实联调用 dev tunnel 手工冒烟(Task 5)
- 回复长度:markdown 超 6000 字符截断加"(内容过长已截断,完整信息见引用链接)"

---

### Task 1: channels/teams 包骨架 + activity 提取(纯函数)

**Files:**
- Create: `channels/teams/pyproject.toml`, `channels/teams/src/teams_adapter/__init__.py`, `channels/teams/tests/__init__.py`
- Create: `channels/teams/src/teams_adapter/extract.py`
- Modify: `pyproject.toml`(workspace members 加 `channels/teams`,testpaths 加 `channels/teams/tests`)
- Test: `channels/teams/tests/test_extract.py`

**Interfaces:**
- Consumes: `AdvisorRequest`(shared,计划 2 Task 1)
- Produces:
  - `should_respond(activity: dict, bot_id: str) -> bool`(触发规则:1:1 恒真;group 需 @bot;非 message 类型恒假)
  - `to_advisor_request(activity: dict, bot_id: str) -> AdvisorRequest`(剥离 @提及标记;计算 conversation_key;提取 channel_id/user)
  - `strip_mentions(text: str, entities: list[dict], bot_id: str) -> str`(剥离 `<at>bot名</at>` 与对应 mention entity 文本)

- [ ] **Step 1: 写失败测试**

```python
# channels/teams/tests/test_extract.py
from teams_adapter.extract import (
    should_respond,
    strip_mentions,
    to_advisor_request,
)

BOT_ID = "28:bot-app-id"


def group_activity(text="<at>Advisor</at> Copilot 登录失败", mentions_bot=True):
    entities = []
    if mentions_bot:
        entities.append({
            "type": "mention",
            "mentioned": {"id": BOT_ID, "name": "Advisor"},
            "text": "<at>Advisor</at>",
        })
    return {
        "type": "message",
        "text": text,
        "entities": entities,
        "conversation": {
            "id": "19:chan@thread.tacv2;messageid=170001",
            "conversationType": "channel",
        },
        "channelData": {
            "channel": {"id": "19:chan@thread.tacv2"},
        },
        "from": {"id": "29:user1", "name": "张三"},
    }


def personal_activity(text="额度怎么看?"):
    return {
        "type": "message",
        "text": text,
        "entities": [],
        "conversation": {"id": "a:1to1conv", "conversationType": "personal"},
        "from": {"id": "29:user2", "name": "李四"},
    }


def test_group_with_mention_responds():
    assert should_respond(group_activity(), BOT_ID) is True


def test_group_without_mention_ignored():
    assert should_respond(group_activity(mentions_bot=False), BOT_ID) is False


def test_personal_always_responds():
    assert should_respond(personal_activity(), BOT_ID) is True


def test_non_message_ignored():
    activity = group_activity()
    activity["type"] = "conversationUpdate"
    assert should_respond(activity, BOT_ID) is False


def test_strip_mentions_removes_at_tag():
    a = group_activity()
    assert strip_mentions(a["text"], a["entities"], BOT_ID) == "Copilot 登录失败"


def test_to_advisor_request_group():
    req = to_advisor_request(group_activity(), BOT_ID)
    assert req.text == "Copilot 登录失败"
    assert req.is_group is True
    assert req.channel_id == "19:chan@thread.tacv2"
    # 同一 reply thread 共享会话:conversation.id 已含 messageid
    assert req.conversation_key == "19:chan@thread.tacv2;messageid=170001"
    assert req.user_id == "29:user1" and req.user_name == "张三"


def test_to_advisor_request_personal():
    req = to_advisor_request(personal_activity(), BOT_ID)
    assert req.is_group is False
    assert req.conversation_key == "a:1to1conv"
    assert req.channel_id == "a:1to1conv"   # 1:1 无 channel,退化为会话 id
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest channels/teams/tests/test_extract.py -v`
Expected: FAIL,`ModuleNotFoundError: teams_adapter`

- [ ] **Step 3: 建包并入 workspace**

```toml
# channels/teams/pyproject.toml
[project]
name = "advisor-teams"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "advisor-agent",
    "advisor-shared",
    "botbuilder-core>=4.16",
    "aiohttp>=3.9",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/teams_adapter"]
```

根 `pyproject.toml` 修改:

```toml
[tool.uv.workspace]
members = ["shared", "ingestion", "agent", "channels/teams"]

[tool.uv.sources]
advisor-shared = { workspace = true }
advisor-agent = { workspace = true }

[tool.pytest.ini_options]
testpaths = ["shared/tests", "ingestion/tests", "agent/tests",
             "channels/teams/tests"]
```

空 `__init__.py` 两个一并创建。

- [ ] **Step 4: 实现 extract.py**

```python
# channels/teams/src/teams_adapter/extract.py
"""Teams activity → AdvisorRequest:纯函数,不依赖 Bot SDK 对象(spec 8.2)。"""
from advisor_shared.messages import AdvisorRequest


def _bot_mentioned(activity: dict, bot_id: str) -> bool:
    return any(
        e.get("type") == "mention"
        and (e.get("mentioned") or {}).get("id") == bot_id
        for e in activity.get("entities") or []
    )


def should_respond(activity: dict, bot_id: str) -> bool:
    if activity.get("type") != "message":
        return False
    conv_type = (activity.get("conversation") or {}).get("conversationType")
    if conv_type == "personal":
        return True
    return _bot_mentioned(activity, bot_id)


def strip_mentions(text: str, entities: list[dict], bot_id: str) -> str:
    for e in entities or []:
        if e.get("type") == "mention" and \
                (e.get("mentioned") or {}).get("id") == bot_id:
            text = text.replace(e.get("text", ""), "")
    return text.strip()


def to_advisor_request(activity: dict, bot_id: str) -> AdvisorRequest:
    conv = activity.get("conversation") or {}
    is_group = conv.get("conversationType") != "personal"
    channel_id = (
        ((activity.get("channelData") or {}).get("channel") or {}).get("id")
        or conv.get("id", "")
    )
    sender = activity.get("from") or {}
    return AdvisorRequest(
        text=strip_mentions(activity.get("text", ""),
                            activity.get("entities") or [], bot_id),
        conversation_key=conv.get("id", ""),
        channel_id=channel_id,
        user_id=sender.get("id", ""),
        user_name=sender.get("name", ""),
        is_group=is_group,
    )
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv sync && uv run pytest channels/teams/tests/test_extract.py -v`
Expected: 8 项 PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml channels/
git commit -m "feat(teams): adapter scaffold and activity extraction"
```

---

### Task 2: teams — 回复渲染(markdown + 引用 + mention entity)

**Files:**
- Create: `channels/teams/src/teams_adapter/render.py`
- Test: `channels/teams/tests/test_render.py`

**Interfaces:**
- Consumes: `AdvisorResponse/Citation/MentionDirective`(shared)
- Produces:
  - `MAX_MARKDOWN_CHARS = 6000`
  - `render_reply(response: AdvisorResponse) -> dict`:返回 Bot Framework activity dict:
    - `text`:markdown 正文;有 mentions 时开头拼 `<at>名字</at>` 逐个;有 citations 时结尾拼 `\n\n**参考:**\n- [title](url)` 列表
    - `entities`:每个 mention 一个 `{"type":"mention","text":"<at>名字</at>","mentioned":{"id":...,"name":...}}`
    - `type`: "message"
    - 超长截断规则见 Global Constraints

- [ ] **Step 1: 写失败测试**

```python
# channels/teams/tests/test_render.py
from advisor_shared.messages import AdvisorResponse, Citation, MentionDirective
from teams_adapter.render import MAX_MARKDOWN_CHARS, render_reply


def test_plain_markdown_no_extras():
    activity = render_reply(AdvisorResponse(markdown="重启 VS Code 试试。"))
    assert activity["type"] == "message"
    assert activity["text"] == "重启 VS Code 试试。"
    assert activity["entities"] == []


def test_citations_appended_as_reference_list():
    resp = AdvisorResponse(
        markdown="见参考。",
        citations=[Citation(title="FAQ", url="https://g/faq"),
                   Citation(title="Issue 42", url="https://g/42")])
    text = render_reply(resp)["text"]
    assert "**参考:**" in text
    assert "- [FAQ](https://g/faq)" in text
    assert "- [Issue 42](https://g/42)" in text


def test_mentions_prepended_with_entities():
    resp = AdvisorResponse(
        markdown="请跟进。",
        mentions=[MentionDirective(name="李四", platform_user_id="29:1",
                                   role="CSAM")])
    activity = render_reply(resp)
    assert activity["text"].startswith("<at>李四</at>")
    assert activity["entities"] == [{
        "type": "mention", "text": "<at>李四</at>",
        "mentioned": {"id": "29:1", "name": "李四"},
    }]


def test_overlong_markdown_truncated():
    resp = AdvisorResponse(markdown="x" * 10000)
    text = render_reply(resp)["text"]
    assert len(text) <= MAX_MARKDOWN_CHARS + 50
    assert "截断" in text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest channels/teams/tests/test_render.py -v`
Expected: FAIL,`ModuleNotFoundError: teams_adapter.render`

- [ ] **Step 3: 实现 render.py**

```python
# channels/teams/src/teams_adapter/render.py
"""AdvisorResponse → Teams activity dict:mention entity 在此拼装(spec 8.1/8.2)。"""
from advisor_shared.messages import AdvisorResponse

MAX_MARKDOWN_CHARS = 6000
_TRUNCATION_NOTE = "\n\n(内容过长已截断,完整信息见引用链接)"


def render_reply(response: AdvisorResponse) -> dict:
    text = response.markdown
    if len(text) > MAX_MARKDOWN_CHARS:
        text = text[:MAX_MARKDOWN_CHARS] + _TRUNCATION_NOTE

    entities = []
    if response.mentions:
        at_tags = []
        for m in response.mentions:
            tag = f"<at>{m.name}</at>"
            at_tags.append(tag)
            entities.append({
                "type": "mention", "text": tag,
                "mentioned": {"id": m.platform_user_id, "name": m.name},
            })
        text = " ".join(at_tags) + " " + text

    if response.citations:
        refs = "\n".join(f"- [{c.title}]({c.url})" for c in response.citations)
        text = f"{text}\n\n**参考:**\n{refs}"

    return {"type": "message", "text": text, "entities": entities}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest channels/teams/tests/test_render.py -v`
Expected: 4 项 PASS

- [ ] **Step 5: Commit**

```bash
git add channels/
git commit -m "feat(teams): response rendering with citations and mention entities"
```

---

### Task 3: teams — AdvisorBot(ActivityHandler,typing + 编排)

**Files:**
- Create: `channels/teams/src/teams_adapter/bot.py`
- Test: `channels/teams/tests/test_bot.py`

**Interfaces:**
- Consumes: `should_respond/to_advisor_request`(T1)、`render_reply`(T2)、`AdvisorCore.handle`(计划 2)、`set_current_channel_id`(计划 2 factory)
- Produces:
  - `AdvisorBot(ActivityHandler)`:`__init__(self, core, bot_id: str)`;`on_message_activity(turn_context)`:should_respond 不过 → 直接 return;过 → 发 typing activity → `set_current_channel_id(req.channel_id)` → `core.handle(req)` → `turn_context.send_activity(渲染结果转 Activity)`
  - core.handle 抛异常时兜底:发送计划 2 的 `FALLBACK_MESSAGE`(渠道层最后防线,spec 10.1)

- [ ] **Step 1: 写失败测试(fake TurnContext,不连真服务)**

```python
# channels/teams/tests/test_bot.py
from types import SimpleNamespace

from advisor_shared.messages import AdvisorResponse
from teams_adapter.bot import AdvisorBot

BOT_ID = "28:bot"


class FakeTurnContext:
    def __init__(self, activity_dict):
        # botbuilder Activity 的属性访问方式;测试里用 SimpleNamespace 等价模拟
        self.activity = SimpleNamespace(**{
            **activity_dict,
            "as_dict": lambda: activity_dict,
        })
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


def group_activity_dict(mentions_bot=True):
    entities = ([{"type": "mention", "mentioned": {"id": BOT_ID, "name": "A"},
                  "text": "<at>A</at>"}] if mentions_bot else [])
    return {
        "type": "message", "text": "<at>A</at> 登录失败",
        "entities": entities,
        "conversation": {"id": "19:c;messageid=1",
                         "conversationType": "channel"},
        "channelData": {"channel": {"id": "19:c"}},
        "from": {"id": "29:u", "name": "n"},
    }


async def test_responds_with_typing_then_answer():
    core = StubCore()
    bot = AdvisorBot(core, BOT_ID)
    ctx = FakeTurnContext(group_activity_dict())
    await bot.on_message_activity(ctx)
    assert len(core.requests) == 1
    assert core.requests[0].text == "登录失败"
    types = [getattr(a, "type", a.get("type") if isinstance(a, dict) else None)
             for a in ctx.sent]
    assert types[0] == "typing"
    assert len(ctx.sent) == 2


async def test_ignores_group_message_without_mention():
    core = StubCore()
    bot = AdvisorBot(core, BOT_ID)
    ctx = FakeTurnContext(group_activity_dict(mentions_bot=False))
    await bot.on_message_activity(ctx)
    assert core.requests == [] and ctx.sent == []


async def test_core_error_sends_fallback():
    from advisor_agent.core import FALLBACK_MESSAGE
    core = StubCore(error=RuntimeError("boom"))
    bot = AdvisorBot(core, BOT_ID)
    ctx = FakeTurnContext(group_activity_dict())
    await bot.on_message_activity(ctx)
    last = ctx.sent[-1]
    text = getattr(last, "text", last.get("text") if isinstance(last, dict) else "")
    assert FALLBACK_MESSAGE in text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest channels/teams/tests/test_bot.py -v`
Expected: FAIL,`ModuleNotFoundError: teams_adapter.bot`

- [ ] **Step 3: 实现 bot.py**

```python
# channels/teams/src/teams_adapter/bot.py
"""Teams bot:触发判定 → typing → agent core → 渲染回复(spec 8.2)。
纯逻辑在 extract/render;此处只做 SDK 对象与 dict 的桥接。"""
import logging

from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import Activity

from advisor_agent.core import FALLBACK_MESSAGE
from advisor_agent.factory import set_current_channel_id
from teams_adapter.extract import should_respond, to_advisor_request
from teams_adapter.render import render_reply

logger = logging.getLogger(__name__)


def _activity_to_dict(activity) -> dict:
    if hasattr(activity, "as_dict"):
        d = activity.as_dict()
        return d() if callable(d) else d
    return activity


class AdvisorBot(ActivityHandler):
    def __init__(self, core, bot_id: str):
        self.core = core
        self.bot_id = bot_id

    async def on_message_activity(self, turn_context: TurnContext):
        activity = _activity_to_dict(turn_context.activity)
        if not should_respond(activity, self.bot_id):
            return
        await turn_context.send_activity(Activity(type="typing"))
        request = to_advisor_request(activity, self.bot_id)
        set_current_channel_id(request.channel_id)
        try:
            response = await self.core.handle(request)
            reply = render_reply(response)
        except Exception:
            logger.exception("core.handle failed")
            reply = {"type": "message", "text": FALLBACK_MESSAGE,
                     "entities": []}
        await turn_context.send_activity(Activity(
            type=reply["type"], text=reply["text"],
            entities=reply["entities"] or None))
```

注:FakeTurnContext 发送的 `Activity(type="typing")` 有 `.type` 属性,测试用 getattr 兼容 —— 如 botbuilder 的 Activity 构造在测试环境有出入,把断言改为读 `.type` 属性即可,行为不变。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest channels/teams/tests/test_bot.py -v`
Expected: 3 项 PASS

- [ ] **Step 5: Commit**

```bash
git add channels/
git commit -m "feat(teams): AdvisorBot handler with typing indicator and fallback"
```

---

### Task 4: teams — aiohttp 入口(/api/messages)+ 启动装配

**Files:**
- Create: `channels/teams/src/teams_adapter/app.py`
- Create: `channels/teams/src/teams_adapter/__main__.py`
- Test: `channels/teams/tests/test_app.py`

**Interfaces:**
- Consumes: `AdvisorBot`(T3)、`build_advisor`(计划 2 factory)
- Produces:
  - `create_app(bot, adapter) -> aiohttp.web.Application`(POST /api/messages → CloudAdapter.process;GET /healthz → 200 "ok")
  - `__main__`:`python -m teams_adapter` 启动 —— CloudAdapter(Bot Framework 认证,TEAMS_APP_ID/TEAMS_APP_PASSWORD)+ `build_advisor(channel_name="teams")` + AdvisorBot,监听 `PORT`(默认 3978)

- [ ] **Step 1: 写失败测试(健康检查与路由,adapter 用 stub)**

```python
# channels/teams/tests/test_app.py
from aiohttp.test_utils import TestClient, TestServer

from teams_adapter.app import create_app


class StubAdapter:
    def __init__(self):
        self.processed = []

    async def process(self, request, bot):
        self.processed.append(request)
        from aiohttp import web
        return web.Response(status=201)


class StubBot:
    pass


async def test_healthz_ok():
    app = create_app(StubBot(), StubAdapter())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200
        assert await resp.text() == "ok"


async def test_messages_delegates_to_adapter():
    adapter = StubAdapter()
    app = create_app(StubBot(), adapter)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/messages", json={"type": "message"})
        assert resp.status == 201
        assert len(adapter.processed) == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest channels/teams/tests/test_app.py -v`
Expected: FAIL,`ModuleNotFoundError: teams_adapter.app`

- [ ] **Step 3: 实现 app.py**

```python
# channels/teams/src/teams_adapter/app.py
"""aiohttp 入口:Bot Framework 消息端点 + 健康检查。"""
from aiohttp import web


def create_app(bot, adapter) -> web.Application:
    async def messages(request: web.Request) -> web.Response:
        return await adapter.process(request, bot)

    async def healthz(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/healthz", healthz)
    return app
```

- [ ] **Step 4: 实现 __main__.py**

```python
# channels/teams/src/teams_adapter/__main__.py
"""启动:python -m teams_adapter(需 TEAMS_APP_ID/TEAMS_APP_PASSWORD 及计划 2 全部环境变量)。"""
import logging
import os

from aiohttp import web
from botbuilder.core import CloudAdapter
from botbuilder.core.integration import ConfigurationBotFrameworkAuthentication

from advisor_agent.factory import build_advisor
from teams_adapter.app import create_app
from teams_adapter.bot import AdvisorBot


class _Config:
    APP_ID = os.environ.get("TEAMS_APP_ID", "")
    APP_PASSWORD = os.environ.get("TEAMS_APP_PASSWORD", "")
    APP_TYPE = "MultiTenant"
    APP_TENANTID = ""


def main():
    logging.basicConfig(level=logging.INFO)
    adapter = CloudAdapter(ConfigurationBotFrameworkAuthentication(_Config()))
    core = build_advisor(channel_name="teams")
    bot = AdvisorBot(core, bot_id=f"28:{_Config.APP_ID}")
    app = create_app(bot, adapter)
    web.run_app(app, port=int(os.environ.get("PORT", 3978)))


if __name__ == "__main__":
    main()
```

注:`ConfigurationBotFrameworkAuthentication` 的配置对象形状以 botbuilder-core 4.16 文档为准;若属性名有出入(如需要 `MicrosoftAppId`),按官方 sample 等价调整,不改变 create_app/AdvisorBot 接口。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest channels/teams/tests/test_app.py -v && uv run python -c "import teams_adapter.__main__; print('imports ok')"`
Expected: 2 项 PASS;`imports ok`

- [ ] **Step 6: Commit**

```bash
git add channels/
git commit -m "feat(teams): aiohttp endpoint and startup assembly"
```

---

### Task 5: 端到端手工冒烟(dev tunnel + 真实 Teams)+ README

**Files:**
- Create: `README.md`
- Create: `docs/teams-setup.md`

**Interfaces:**
- Consumes: 全部
- Produces: 可跟随的部署/联调文档;冒烟通过的系统

- [ ] **Step 1: 写 docs/teams-setup.md(Azure Bot 注册 + 本地联调步骤)**

```markdown
# Teams 联调与部署

## 一、Azure Bot 注册(一次性)

1. Azure Portal → 创建资源 → **Azure Bot**
   - Bot handle:copilot-advisor-dev
   - 定价层:F0(开发)
   - 应用类型:Multi Tenant,让向导自动创建 App Registration
2. 记录 **Microsoft App ID**;在 App Registration → Certificates & secrets
   创建 client secret,记录值
3. Bot 资源 → Channels → 添加 **Microsoft Teams** 渠道

## 二、本地联调(dev tunnel)

```bash
# 1. 启动隧道(devtunnel CLI;或用 ngrok http 3978)
devtunnel host -p 3978 --allow-anonymous

# 2. Azure Bot → Configuration → Messaging endpoint 填:
#    https://<tunnel-id>.devtunnels.ms/api/messages

# 3. 环境变量(复制 .env.example 为 .env 并补充):
export TEAMS_APP_ID=<Microsoft App ID>
export TEAMS_APP_PASSWORD=<client secret>
# 计划 2 的 AZURE_OPENAI_* / AZURE_SEARCH_* / GITHUB_TOKEN / TAVILY_API_KEY 同样需要
cp agent/escalation.example.yaml agent/escalation.yaml  # 填真实联系人

# 4. 启动
uv run python -m teams_adapter
```

## 三、装进 Teams

1. https://dev.teams.microsoft.com → Apps → New app,Bot 指向上面的 App ID
2. Preview in Teams,即可 1:1 私聊;添加到某个 team 后在 channel @提及

## 四、冒烟清单

- [ ] 1:1 私聊问"Copilot 登录失败怎么办" → typing 指示 → 中文回答带引用链接
- [ ] channel 里不 @bot 发消息 → bot 无反应
- [ ] channel 里 @bot 提问 → 回答出现在同一 reply thread
- [ ] 同一 thread 里追问"还是不行,找个人吧" → 回复含 CSAM @提及或联系方式
- [ ] 英文提问 → 英文回答
- [ ] 停掉 AI Search(改错 endpoint)再提问 → 仍有回答(live/web 兜底)或明确道歉,进程不崩
```

- [ ] **Step 2: 写 README.md**

```markdown
# GitHub Copilot Advisor

面向企业用户的 GitHub Copilot 问答 agent:群聊 @提问 → 知识库/实时检索 →
分级升级(通用建议 → 工单指引 → CSAM/CSA)。

## 结构(monorepo,三个独立 project)

| 目录 | 职责 | 运行 |
|---|---|---|
| `shared/` | 契约:索引 schema、消息模型、事件 | (库) |
| `ingestion/` | 数据抓取→清洗→LLM 提炼→写入 AI Search | `uv run python -m ingestion run` |
| `agent/` | MAF + Azure OpenAI 编排与工具 | (库,由渠道装配) |
| `channels/teams/` | Teams Bot Framework 薄壳 | `uv run python -m teams_adapter` |

依赖方向:`channels/* → agent → shared`,`ingestion → shared`。

## 快速开始

```bash
uv sync                      # 安装全部 workspace
uv run pytest                # 单元测试(不需要任何凭据)
cp .env.example .env         # 填 Azure/GitHub 凭据
uv run python -m ingestion run   # 灌知识库
uv run python -m teams_adapter   # 启动 Teams bot(见 docs/teams-setup.md)
```

## 文档

- 设计 spec:`docs/superpowers/specs/2026-08-21-copilot-advisor-design.md`
- 实现计划:`docs/superpowers/plans/`
- Teams 联调:`docs/teams-setup.md`
```

- [ ] **Step 3: .env.example 追加 Teams 变量**

在计划 1 的 `.env.example` 末尾追加:

```bash
TEAMS_APP_ID=00000000-0000-0000-0000-000000000000
TEAMS_APP_PASSWORD=xxx
TAVILY_API_KEY=tvly-xxx
BRAVE_API_KEY=
ESCALATION_CONFIG=agent/escalation.yaml
```

- [ ] **Step 4: 执行冒烟清单**

按 docs/teams-setup.md 第四节逐项手工验证,全部勾选后进入下一步。
任何一项不过:按 superpowers:systematic-debugging 排查,修复后重跑该项。

- [ ] **Step 5: Commit**

```bash
git add README.md docs/teams-setup.md .env.example
git commit -m "docs: README and Teams setup guide with smoke checklist"
```
