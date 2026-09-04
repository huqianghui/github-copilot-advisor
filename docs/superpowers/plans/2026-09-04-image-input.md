# 图片输入与分析 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 advisor agent 能接收并读懂 Teams 用户粘贴的 inline image(报错截图、配置界面截图、纯图片),把图中信息接入既有的五工具检索升级瀑布。

**Architecture:** 原生多模态直通 —— 图片以 content parts 形式与文本一起送 Azure OpenAI vision 模型,模型在整个 tool loop 中都能回看原图。不新增工具(工具面仍为 5 个),不改 `SessionStore` 契约(图片信息靠 system prompt 要求模型复述,复述随回答进历史)。Teams 侧自建 `InputFileDownloader` 实现,安全约束(精确 host 白名单、不跟随重定向、401 不重试)是该模块的主要设计驱动。

**Tech Stack:** Python 3.11+ / pydantic / openai SDK(AsyncAzureOpenAI)/ httpx / Microsoft 365 Agents SDK 1.4 / pytest + respx

**Spec:** `docs/superpowers/specs/2026-09-04-image-input-design.md`

---

## 前置检查(开始前必读)

**运行环境**:企业网络下 `uv sync` 前需 `export UV_INDEX_URL=<内部代理>`,见 `DEVELOPMENT.md`。

**测试命令**:
- 全量单元测试:`uv run pytest`(默认 `-m 'not integration'`,不需要任何凭据)
- 单文件:`uv run pytest channels/teams/tests/test_downloader.py -v`
- eval(需真实凭据,默认不跑):`uv run pytest -m integration agent/tests/test_eval_behavior.py`

**模型现状(2026-09-04 已查明)**:`AZURE_OPENAI_CHAT_DEPLOYMENT=gpt-5-mini`。
按 Microsoft Learn 的 GPT-5 能力矩阵,`gpt-5-mini (2025-08-07)` **同时支持
image input、functions/tools 与 Chat Completions API** —— 本计划的方案 A 成立,
无需改用 Responses API。

**待验证的风险(必须在 Task 3 之前做掉,见 Task 0)**:`.env` 未设置
`AZURE_OPENAI_API_VERSION`,因此走 `maf_backend.py` 的默认值 `2024-10-21`。
这是 GPT-4o 时代的 GA 版本。纯文本 tool loop 目前能跑通,**不能推出**它对
gpt-5-mini 的图片 content parts 也 OK。若 Task 0 探测失败,把 `.env` 与
`.env.example` 的 `AZURE_OPENAI_API_VERSION` 设为 `2025-04-01-preview` 再重试。

**gpt-5 系列的既有约束(现有代码已满足,改动时勿引入)**:
- 不支持 `max_tokens`(须用 `max_completion_tokens`)、`temperature`、`top_p`、
  `presence_penalty`、`frequency_penalty`。现有 `_run_tool_loop` 只传
  `model` / `messages` / `tools`,**保持这样**
- Chat Completions 的图片 part 格式是 `{"type": "image_url",
  "image_url": {"url": ...}}`(对象),与 Responses API 的 `input_image`
  (字符串)不同。Task 3 用的是前者,正确
- 单请求最多 10 张图、单张 ≤ 20MB。本计划限额(4 张 / 4MB)在其内

**未来升级风险(记录备查,本期不处理)**:`gpt-5.6` 及以后的模型在
Chat Completions 上带 `tools` 会直接报错,必须迁到 Responses API 或每次显式设
`reasoning_effort='none'`。届时受影响的是 `_run_tool_loop` 整体,与图片输入无关。

**已验证的 SDK 事实**(不必重新调研):
- `ApplicationOptions.file_downloaders: list[InputFileDownloader]` 存在,且 `AgentApplication(**kwargs)` 会透传该键
- `TurnState()` 裸构造即含 `.temp`,`state.temp.input_files` 默认 `[]`,setter 可用
- `InputFile` 是 dataclass,字段仅 `content: bytes` / `content_type: str` / `content_url: Optional[str]` —— **没有 `filename`**
- `Attachment` 的 python 属性名是 `content_type` / `content_url`(JSON 别名 `contentType` / `contentUrl`)
- `ClaimsIdentity.get_token_audience() -> str`、`get_token_scope() -> list[str]`
- `MsalConnectionManager.get_token_provider_from_activity(claims_identity, activity) -> AccessTokenProviderBase`
- `AccessTokenProviderBase.get_access_token(resource_url: str, scopes: list[str], force_refresh: bool = False) -> str`

---

## File Structure

| 文件 | 职责 | 状态 |
|------|------|------|
| `shared/src/advisor_shared/messages.py` | 跨渠道契约:`ImageInput`、`AdvisorRequest.images` | 修改 |
| `shared/src/advisor_shared/events.py` | 可观测性契约:`AdvisorEvent.image_count` | 修改 |
| `agent/src/advisor_agent/backend.py` | 编排后端协议:`run()` 加 `images` | 修改 |
| `agent/src/advisor_agent/maf_backend.py` | 多模态消息构造 + vision 不可用降级 | 修改 |
| `agent/src/advisor_agent/core.py` | images 透传、历史标记、事件计数 | 修改 |
| `agent/src/advisor_agent/prompts.py` | 策略规则 9(图片处理)、10(图中敏感信息) | 修改 |
| `channels/teams/src/teams_adapter/downloader.py` | **唯一**承载附件筛选、host 白名单、下载与限额 | **新建** |
| `channels/teams/src/teams_adapter/extract.py` | 纯函数:`to_advisor_request` 带 images、`is_empty` | 修改 |
| `channels/teams/src/teams_adapter/bot.py` | SDK 桥接:读 `state.temp.input_files`、丢弃回传三分支 | 修改 |
| `channels/teams/src/teams_adapter/__main__.py` | 装配:注入 `file_downloaders` | 修改 |
| `channels/teams/pyproject.toml` | 显式声明 `httpx`(现为 `advisor-agent` 的传递依赖) | 修改 |

安全逻辑全部收敛在 `downloader.py` 一个文件里,便于审计 —— 这是刻意的边界选择。

**对 spec 的两处修正**(实现时以本计划为准):
1. spec §6.1 称协议变更"现有测试无需改动"。实际上 `agent/tests/test_core.py` 的 `StubBackend.run` 是 2 参数签名,`core.py` 传第 3 个参数会 `TypeError`。Task 2 会以**纯追加**方式改造该 stub(保留 `calls` 为 2-tuple,新增 `images_seen`),不破坏第 55 行的 `_, history = backend.calls[0]` 解包。
2. spec §11 称"无新增第三方依赖"。包级别确实没有新包,但 `channels/teams/pyproject.toml` 未声明 `httpx`(靠 `advisor-agent` 传递而来)。Task 10 补上显式声明。

---

## Task 0: 探测 api-version 与 vision 的兼容性 —— ✅ 已完成(2026-09-04)

> **探测结果:通过,无需任何改动。**
> 实测输出:`deployment=gpt-5-mini api_version=2024-10-21` → `OK: 品红色`
> —— 模型正确识别了 1x1 像素图的颜色,且请求同时带了 `tools`。
> 结论:默认 api-version `2024-10-21` 对 gpt-5-mini 的图片 content parts
> 与 function calling 同时有效,`.env` / `.env.example` **不需要改**。
> 影响:Task 3 的 `image_url` 对象格式确认可用;Task 4 的剥图降级从"预期路径"
> 降级为"纯保险";Task 11 的 eval 图片用例具备跑通前提。
> 以下步骤保留备查(日后换部署时可重跑)。

**在写任何代码之前做掉。** 这是唯一一个可能推翻实现细节的未知项:
默认 api-version `2024-10-21` 能否对 gpt-5-mini 同时发送图片 content parts
与 `tools`。探测比事后返工便宜得多。

**Files:** 无(临时脚本,跑完删除)

- [ ] **Step 1: 写探测脚本**

Create `probe_vision.py`(仓库根目录,临时文件):

```python
"""临时探测:当前 api-version + 部署能否同时吃图片与 tools。跑完删除。"""
import asyncio
import os

from openai import AsyncAzureOpenAI

# 1x1 红点 PNG,合法且最小
ONE_PX_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
              "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

TOOLS = [{
    "type": "function",
    "function": {
        "name": "search_solutions",
        "description": "搜索已解决的知识库问答。",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    },
}]


async def main():
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
    print(f"deployment={os.environ['AZURE_OPENAI_CHAT_DEPLOYMENT']} "
          f"api_version={api_version}")
    client = AsyncAzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=api_version,
    )
    response = await client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"],
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "这张图是什么颜色?只回答颜色。"},
            {"type": "image_url", "image_url": {
                "url": f"data:image/png;base64,{ONE_PX_PNG}",
                "detail": "auto"}},
        ]}],
        tools=TOOLS,
    )
    print("OK:", response.choices[0].message.content)


asyncio.run(main())
```

- [ ] **Step 2: 运行探测**

Run: `uv run --env-file .env python probe_vision.py`

判读结果:

| 输出 | 结论 | 动作 |
|------|------|------|
| `OK: <颜色>` | 当前 api-version 可用 | 无需改动,进 Task 1 |
| 400 `invalid_image` / `image_url is not supported` | api-version 太老 | 进 Step 3 |
| 400 提到 `tools` 与 reasoning 冲突 | 部署已升到 gpt-5.6+ | **停下来找负责人**,需要迁 Responses API,超出本计划范围 |
| 401 / 404 | 凭据或部署名不对 | 先修环境,与本功能无关 |

- [ ] **Step 3: 仅在 Step 2 报图片相关 400 时执行**

在 `.env` 中加入(并同步到 `.env.example`,值不含机密):

```
AZURE_OPENAI_API_VERSION=2025-04-01-preview
```

重跑 Step 2 确认变为 `OK:`。若仍失败,记录完整错误信息后停下来 —— 不要
硬着头皮往下写,方案 A 的前提已经不成立。

- [ ] **Step 4: 清理**

```bash
rm probe_vision.py
```

- [ ] **Step 5: 若改了 .env.example 则提交**

```bash
git add .env.example
git commit -m "chore: pin Azure OpenAI api-version for gpt-5-mini vision support"
```

(Step 2 直接通过、未改任何文件时,本任务无提交。)

---

## Task 1: shared 契约

**Files:**
- Modify: `shared/src/advisor_shared/messages.py`
- Modify: `shared/src/advisor_shared/events.py`
- Test: `shared/tests/test_messages.py`, `shared/tests/test_events.py`

- [ ] **Step 1: 写失败测试**

追加到 `shared/tests/test_messages.py` 末尾(文件顶部的 import 行改为
`from advisor_shared.messages import AdvisorRequest, AdvisorResponse, Citation, ImageInput, MentionDirective`,
按该文件既有的 import 实际情况补上 `ImageInput` 即可):

```python
def _req(**kw):
    base = dict(text="hi", conversation_key="c", channel_id="ch",
                user_id="u", user_name="n", is_group=False)
    base.update(kw)
    return AdvisorRequest(**base)


def test_request_defaults_to_no_images():
    assert _req().images == []


def test_request_carries_images():
    req = _req(text="", images=[ImageInput(data=b"\x89PNG", mime_type="image/png")])
    assert req.images[0].data == b"\x89PNG"
    assert req.images[0].mime_type == "image/png"
    assert req.images[0].name == ""       # Teams inline image 无文件名
```

追加到 `shared/tests/test_events.py` 末尾:

```python
def test_event_image_count_defaults_to_zero_and_serializes():
    event = AdvisorEvent(conversation_key="c", channel="teams",
                         question_summary="q", stage="kb_hit")
    assert event.image_count == 0
    assert '"image_count":0' in event.to_log_line()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest shared/tests/test_messages.py shared/tests/test_events.py -v`
Expected: FAIL —— `ImportError: cannot import name 'ImageInput'`,以及 `AdvisorEvent` 无 `image_count` 属性

- [ ] **Step 3: 实现**

`shared/src/advisor_shared/messages.py` —— 在 `Citation` 之前插入:

```python
class ImageInput(BaseModel):
    """用户发送的图片。存原始字节,base64 编码是 backend 的实现细节。"""
    data: bytes
    mime_type: str
    name: str = ""
```

同文件 `AdvisorRequest` 末尾追加一行字段:

```python
    images: list[ImageInput] = []
```

`shared/src/advisor_shared/events.py` —— `AdvisorEvent` 的 `mentioned_human` 之后追加:

```python
    image_count: int = 0
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest shared/ -v`
Expected: PASS(含原有全部用例)

- [ ] **Step 5: 提交**

```bash
git add shared/
git commit -m "feat(shared): ImageInput contract and AdvisorEvent.image_count"
```

---

## Task 2: backend 协议 + AdvisorCore 透传

**Files:**
- Modify: `agent/src/advisor_agent/backend.py`
- Modify: `agent/src/advisor_agent/core.py:44-88`
- Test: `agent/tests/test_core.py`

- [ ] **Step 1: 改造 StubBackend 并写失败测试**

`agent/tests/test_core.py` —— 顶部 import 行加入 `ImageInput`:

```python
from advisor_shared.messages import AdvisorRequest, ImageInput, MentionDirective
```

`make_request` 加 `images` 形参:

```python
def make_request(text="Copilot 登录失败", images=None) -> AdvisorRequest:
    return AdvisorRequest(text=text, conversation_key="ck1",
                          channel_id="19:abc", user_id="u", user_name="n",
                          is_group=True, images=images or [])
```

`StubBackend` **纯追加**改造 —— `calls` 保持 2-tuple(第 55 行的解包依赖它),
图片记录到新字段:

```python
class StubBackend:
    """记录调用;可注入副作用(模拟工具执行)与失败次数。"""
    def __init__(self, reply="答案", fail_times=0, side_effect=None):
        self.reply, self.fail_times = reply, fail_times
        self.side_effect = side_effect
        self.calls: list[tuple[str, list[dict]]] = []
        self.images_seen: list[list] = []

    async def run(self, user_text, history, images=None):
        self.calls.append((user_text, list(history)))
        self.images_seen.append(list(images or []))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("llm down")
        if self.side_effect:
            self.side_effect()
        return self.reply
```

文件末尾追加三个新测试:

```python
def _png(tag=b"x"):
    return ImageInput(data=tag, mime_type="image/png")


async def test_images_forwarded_to_backend():
    backend = StubBackend()
    core = AdvisorCore(backend, InMemorySessionStore())
    img = _png()
    await core.handle(make_request(text="这是什么错误", images=[img]))
    assert backend.images_seen[0] == [img]


async def test_history_marks_images_when_text_empty():
    sessions = InMemorySessionStore()
    core = AdvisorCore(StubBackend(), sessions)
    await core.handle(make_request(text="", images=[_png(), _png(b"y")]))
    history = await sessions.get("ck1")
    assert history[0] == {"role": "user", "content": "[图片×2]"}


async def test_history_keeps_text_alongside_image_marker():
    sessions = InMemorySessionStore()
    core = AdvisorCore(StubBackend(), sessions)
    await core.handle(make_request(text="报这个错", images=[_png()]))
    history = await sessions.get("ck1")
    assert history[0]["content"] == "[图片×1] 报这个错"


async def test_event_records_image_count():
    events: list[AdvisorEvent] = []
    core = AdvisorCore(StubBackend(), InMemorySessionStore(),
                       event_sink=events.append)
    await core.handle(make_request(images=[_png()]))
    assert events[0].image_count == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest agent/tests/test_core.py -v`
Expected: FAIL —— `images_seen` 为 `[[]]`(core 没传)、历史无 `[图片×N]` 标记、`image_count` 为 0

- [ ] **Step 3: 实现**

`agent/src/advisor_agent/backend.py` 整体替换为:

```python
"""LLM 编排后端协议:MAF 是默认实现(Task 10),预留 Copilot SDK 等(spec 7.5)。"""
from typing import Protocol

from advisor_shared.messages import ImageInput


class AgentBackend(Protocol):
    async def run(self, user_text: str, history: list[dict],
                  images: list[ImageInput] | None = None) -> str:
        """跑一轮完整的 tool loop,返回最终回答文本。
        history: [{"role": "user"|"assistant", "content": str}, ...]
        images: 本轮附带的图片;历史里永远只有文本(见图片输入 spec §8)。"""
        ...
```

`agent/src/advisor_agent/core.py` —— 三处改动。

第一处,`handle` 里的 backend 调用(原第 52 行):

```python
                answer = await self.backend.run(
                    request.text, history, request.images or None)
```

第二处,user 侧历史写入(原第 73-74 行)替换为:

```python
            user_note = (f"[图片×{len(request.images)}] {request.text}".strip()
                         if request.images else request.text)
            await self.sessions.append(
                request.conversation_key, "user", user_note)
```

第三处,事件构造(原第 78-87 行)中 `mentioned_human` 之后加一行:

```python
            image_count=len(request.images),
```

**同时必须**给 `agent/src/advisor_agent/maf_backend.py:156` 的 `run` 加上形参,
否则本次提交在真实运行时会 `TypeError`(单元测试用 StubBackend,察觉不到):

```python
    async def run(self, user_text: str, history: list[dict],
                  images: list = None) -> str:
```

本任务里这个参数**先接住不用**,Task 3 才赋予它行为。这样每一次提交都是
可运行的 —— 不留"测试绿但生产崩"的中间状态。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest agent/tests/test_core.py -v`
Expected: PASS(含原有全部用例)

- [ ] **Step 5: 提交**

```bash
git add agent/src/advisor_agent/backend.py agent/src/advisor_agent/core.py agent/src/advisor_agent/maf_backend.py agent/tests/test_core.py
git commit -m "feat(agent): forward images through AdvisorCore, mark history and events"
```

---

## Task 3: MAFBackend 多模态消息构造

**Files:**
- Modify: `agent/src/advisor_agent/maf_backend.py:156-196`
- Test: `agent/tests/test_maf_backend.py`(新建)

- [ ] **Step 1: 写失败测试**

Create `agent/tests/test_maf_backend.py`:

```python
"""MAFBackend 的消息构造与 vision 降级(图片输入 spec §6)。"""
import base64

from advisor_agent.maf_backend import build_user_message
from advisor_shared.messages import ImageInput

PNG = b"\x89PNG\r\n\x1a\n"


def test_text_only_stays_plain_string():
    """回归防线:无图时请求体必须与改动前逐字节一致。"""
    assert build_user_message("登录失败", None) == {
        "role": "user", "content": "登录失败"}
    assert build_user_message("登录失败", []) == {
        "role": "user", "content": "登录失败"}


def test_image_builds_content_parts_with_text_first():
    msg = build_user_message("这是什么错", [ImageInput(data=PNG, mime_type="image/png")])
    parts = msg["content"]
    assert msg["role"] == "user"
    assert parts[0] == {"type": "text", "text": "这是什么错"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["detail"] == "auto"
    expected = base64.b64encode(PNG).decode()
    assert parts[1]["image_url"]["url"] == f"data:image/png;base64,{expected}"


def test_empty_text_gets_placeholder():
    """纯图片场景:text part 不能为空,否则模型缺少任务指令。"""
    msg = build_user_message("", [ImageInput(data=PNG, mime_type="image/png")])
    assert msg["content"][0]["text"] == "(用户只发了图片,无文字说明)"


def test_multiple_images_all_appended():
    msg = build_user_message("看图", [
        ImageInput(data=PNG, mime_type="image/png"),
        ImageInput(data=PNG, mime_type="image/jpeg"),
    ])
    assert len(msg["content"]) == 3
    assert msg["content"][2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest agent/tests/test_maf_backend.py -v`
Expected: FAIL —— `ImportError: cannot import name 'build_user_message'`

- [ ] **Step 3: 实现**

`agent/src/advisor_agent/maf_backend.py` —— 顶部 import 增加:

```python
import base64
```

并在 import 区加入契约类型:

```python
from advisor_shared.messages import ImageInput
```

在 `_MAX_TOOL_ROUNDS = 6` 之后、`_TOOL_SCHEMAS` 之前插入模块级纯函数:

```python
_NO_TEXT_PLACEHOLDER = "(用户只发了图片,无文字说明)"


def build_user_message(user_text: str,
                       images: list[ImageInput] | None) -> dict:
    """构造 user message。无图时保持纯字符串 content —— 纯文本路径零行为变化。"""
    if not images:
        return {"role": "user", "content": user_text}
    content: list[dict] = [
        {"type": "text", "text": user_text or _NO_TEXT_PLACEHOLDER}]
    for image in images:
        b64 = base64.b64encode(image.data).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image.mime_type};base64,{b64}",
                          "detail": "auto"},
        })
    return {"role": "user", "content": content}
```

同文件 `run` 方法签名与首段改为(其余循环体保持不变):

```python
    async def run(self, user_text: str, history: list[dict],
                  images: list[ImageInput] | None = None) -> str:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(history)
        messages.append(build_user_message(user_text, images))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest agent/tests/test_maf_backend.py agent/tests/test_core.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent/src/advisor_agent/maf_backend.py agent/tests/test_maf_backend.py
git commit -m "feat(agent): build multimodal content parts when images present"
```

---

## Task 4: vision 不可用时剥图降级

模型部署不支持 vision 时 Azure OpenAI 返回 400。必须在 backend 内部处理:
`core.py` 的 `_MAX_ATTEMPTS = 2` 是无差别重试,两次都会同样失败,最终落到
`FALLBACK_MESSAGE`,用户无从得知真实原因。

**Files:**
- Modify: `agent/src/advisor_agent/maf_backend.py`
- Test: `agent/tests/test_maf_backend.py`

- [ ] **Step 1: 写失败测试**

追加到 `agent/tests/test_maf_backend.py`:

```python
import httpx
import pytest
from openai import BadRequestError

from advisor_agent.maf_backend import VISION_UNAVAILABLE_NOTE, MAFBackend


def _bad_request() -> BadRequestError:
    request = httpx.Request("POST", "https://x.openai.azure.com/chat")
    return BadRequestError("image input not supported",
                           response=httpx.Response(400, request=request),
                           body=None)


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o")
    return MAFBackend(tools=None, channel_id_provider=lambda: "19:c",
                      is_group_provider=lambda: False)


async def test_vision_400_retries_without_images(backend, monkeypatch):
    seen = []

    async def fake_loop(messages):
        seen.append(messages[-1]["content"])
        if isinstance(messages[-1]["content"], list):
            raise _bad_request()
        return "先检查网络代理"

    monkeypatch.setattr(backend, "_run_tool_loop", fake_loop)
    out = await backend.run("看图", [], [ImageInput(data=PNG, mime_type="image/png")])
    assert "先检查网络代理" in out
    assert VISION_UNAVAILABLE_NOTE in out
    assert len(seen) == 2                      # 第一次带图,第二次纯文本
    assert isinstance(seen[1], str)


async def test_text_only_400_is_not_swallowed(backend, monkeypatch):
    """无图时的 400 是真错误,必须抛出,交给 core 的重试与兜底。"""
    async def fake_loop(messages):
        raise _bad_request()

    monkeypatch.setattr(backend, "_run_tool_loop", fake_loop)
    with pytest.raises(BadRequestError):
        await backend.run("登录失败", [], None)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest agent/tests/test_maf_backend.py -v`
Expected: FAIL —— `ImportError: cannot import name 'VISION_UNAVAILABLE_NOTE'`

- [ ] **Step 3: 实现**

`agent/src/advisor_agent/maf_backend.py` —— import 区改为:

```python
import base64
import json
import logging
import os
from typing import Callable

from openai import AsyncAzureOpenAI, BadRequestError

from advisor_agent.prompts import SYSTEM_PROMPT
from advisor_agent.tools import AdvisorTools
from advisor_shared.messages import ImageInput

logger = logging.getLogger(__name__)
```

在 `_NO_TEXT_PLACEHOLDER` 旁加常量:

```python
VISION_UNAVAILABLE_NOTE = (
    "(注:当前模型部署未启用图片理解,以上回答未参考你发送的图片。"
    "可以把图中的关键信息贴成文字,我再帮你看。)")
```

把现有 `run` 的循环体整体提取为 `_run_tool_loop`,`run` 只负责组装与降级:

```python
    async def run(self, user_text: str, history: list[dict],
                  images: list[ImageInput] | None = None) -> str:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(history)
        messages.append(build_user_message(user_text, images))
        try:
            return await self._run_tool_loop(messages)
        except BadRequestError:
            if not images:
                raise            # 无图时的 400 是真错误,交给 core 兜底
            logger.warning("vision request rejected, retrying without images")
            messages[-1] = build_user_message(user_text, None)
            answer = await self._run_tool_loop(messages)
            return f"{answer}\n\n{VISION_UNAVAILABLE_NOTE}"

    async def _run_tool_loop(self, messages: list[dict]) -> str:
        for _ in range(_MAX_TOOL_ROUNDS):
            response = await self._client.chat.completions.create(
                model=self._deployment,
                messages=messages,
                tools=_TOOL_SCHEMAS,
            )
            message = response.choices[0].message
            tool_calls = message.tool_calls
            if not tool_calls:
                return message.content or ""

            messages.append({
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in tool_calls
                ],
            })
            for call in tool_calls:
                arguments = json.loads(call.function.arguments or "{}")
                result = await self._dispatch(call.function.name, arguments)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })

        return "抱歉,处理这个问题花了太多轮工具调用,请换个方式描述或稍后重试。"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest agent/ -v`
Expected: PASS(全部 agent 单元测试)

- [ ] **Step 5: 提交**

```bash
git add agent/src/advisor_agent/maf_backend.py agent/tests/test_maf_backend.py
git commit -m "feat(agent): degrade gracefully when deployment lacks vision support"
```

---

## Task 5: system prompt 规则 9 / 10

**Files:**
- Modify: `agent/src/advisor_agent/prompts.py`
- Test: `agent/tests/test_prompts.py`

- [ ] **Step 1: 写失败测试**

追加到 `agent/tests/test_prompts.py` 末尾:

```python
def test_prompt_has_image_handling_rule():
    from advisor_agent.prompts import SYSTEM_PROMPT
    assert "复述" in SYSTEM_PROMPT
    assert "search_solutions 的 query 主体" in SYSTEM_PROMPT


def test_prompt_forbids_echoing_secrets_from_images():
    from advisor_agent.prompts import SYSTEM_PROMPT
    for word in ("token", "API key", "cookie"):
        assert word in SYSTEM_PROMPT
    assert "已略过" in SYSTEM_PROMPT
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest agent/tests/test_prompts.py -v`
Expected: FAIL —— AssertionError,prompt 里没有这些字符串

- [ ] **Step 3: 实现**

`agent/src/advisor_agent/prompts.py` —— 在第 8 条规则之后、`## 回答风格` 之前插入:

```
9. 用户发送图片时:先用 1-2 句复述图中关键信息(错误原文、配置项、界面位置),
   让用户能确认你有没有读对图;再按上述规则走正常的检索升级瀑布。
   把图中读到的错误文本作为 search_solutions 的 query 主体,不要用
   "用户发了一张截图"这类空泛 query。图片只说明现象时,结合上下文推断
   用户想解决什么;实在无法判断就直接问用户。
10. 图片中若出现 token、API key、cookie、完整邮箱地址、账单金额等敏感信息,
    不要把它们复述到回答里,只说明"图中含敏感信息已略过"。截图本身群成员
    都看得到,但把敏感信息转成文字会让它进入会话历史与日志。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest agent/tests/test_prompts.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent/src/advisor_agent/prompts.py agent/tests/test_prompts.py
git commit -m "feat(agent): prompt rules for image restatement and secret redaction"
```

---

## Task 6: 附件筛选与 host 白名单(纯函数)

这是安全边界的第一道闸。**host 不在白名单 → 直接跳过,连请求都不发**
—— 比"不带 token 下载"更严格,因为 inline image 必然来自 Microsoft host。

**Files:**
- Create: `channels/teams/src/teams_adapter/downloader.py`
- Test: `channels/teams/tests/test_downloader.py`(新建)

- [ ] **Step 1: 写失败测试**

Create `channels/teams/tests/test_downloader.py`:

```python
# channels/teams/tests/test_downloader.py
"""TeamsImageDownloader:筛选、白名单与下载安全(图片输入 spec §5)。"""
from microsoft_agents.activity import Activity

from teams_adapter.downloader import MAX_IMAGES, select_image_urls

GOOD_URL = "https://smba.trafficmanager.net/amer/v3/attachments/1/views/original"
EVIL_URL = "https://evil.example.com/steal"


def attachments_activity(attachments: list[dict]) -> Activity:
    return Activity.model_validate({
        "type": "message",
        "channelId": "msteams",
        "text": "看看这个",
        "conversation": {"id": "19:c", "conversationType": "channel"},
        "from": {"id": "29:u", "name": "n"},
        "attachments": attachments,
    })


def urls_for(attachments: list[dict]) -> list[str]:
    return select_image_urls(attachments_activity(attachments).attachments)


def test_keeps_only_image_attachments():
    # Teams 会把 text/html 与图片一并下发,不过滤会去下载 HTML
    assert urls_for([
        {"contentType": "text/html", "contentUrl": GOOD_URL},
        {"contentType": "application/pdf", "contentUrl": GOOD_URL},
        {"contentType": "image/png", "contentUrl": GOOD_URL},
    ]) == [GOOD_URL]


def test_rejects_non_allowlisted_host():
    assert urls_for([{"contentType": "image/png", "contentUrl": EVIL_URL}]) == []


def test_rejects_non_https_scheme():
    plain = GOOD_URL.replace("https://", "http://")
    assert urls_for([{"contentType": "image/png", "contentUrl": plain}]) == []


def test_rejects_lookalike_host_suffix():
    """trafficmanager.net 是共享命名空间,后缀匹配会放进第三方域名。"""
    lookalike = "https://evil-smba.trafficmanager.net.attacker.com/x"
    assert urls_for([{"contentType": "image/png", "contentUrl": lookalike}]) == []


def test_skips_attachment_without_url():
    assert urls_for([{"contentType": "image/png"}]) == []


def test_caps_image_count():
    many = [{"contentType": "image/png", "contentUrl": GOOD_URL}] * 10
    assert len(urls_for(many)) == MAX_IMAGES


def test_handles_none_attachments():
    assert select_image_urls(None) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest channels/teams/tests/test_downloader.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'teams_adapter.downloader'`

- [ ] **Step 3: 实现**

Create `channels/teams/src/teams_adapter/downloader.py`:

```python
# channels/teams/src/teams_adapter/downloader.py
"""Teams inline image 下载器(图片输入 spec §5)。

安全约束(GHSA-7vwx-582j-j332 —— Teams 附件下载器泄漏 bot bearer token):
  1. 精确 host 白名单,host 不在其中一律跳过,不发起任何请求
  2. 不跟随重定向 —— 跟随会把 Authorization 带到重定向目标
  3. 401/403 后绝不自动重试加 token —— 这正是该漏洞的成因
本模块刻意不支持 file upload 附件(SharePoint downloadUrl),见 spec §12。
"""
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

TEAMS_CHANNEL_ID = "msteams"
# 精确 host 相等比较。不用后缀匹配:trafficmanager.net 是共享命名空间。
ALLOWED_HOSTS = frozenset({"smba.trafficmanager.net", "api.botframework.com"})
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 4 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 10.0


def select_image_urls(attachments) -> list[str]:
    """挑出可下载的 inline image URL。纯函数,无 I/O。"""
    urls: list[str] = []
    for attachment in attachments or []:
        content_type = (getattr(attachment, "content_type", None) or "").lower()
        if not content_type.startswith("image/"):
            continue                      # 同时排除 Teams 附带的 text/html
        url = getattr(attachment, "content_url", None)
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            logger.warning(
                "skipping attachment from non-allowlisted host: %s",
                parsed.hostname)
            continue
        urls.append(url)
        if len(urls) >= MAX_IMAGES:
            break
    return urls
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest channels/teams/tests/test_downloader.py -v`
Expected: PASS(7 passed)

- [ ] **Step 5: 提交**

```bash
git add channels/teams/src/teams_adapter/downloader.py channels/teams/tests/test_downloader.py
git commit -m "feat(teams): inline image attachment selection with exact host allowlist"
```

---

## Task 7: 下载实现(token、限额、安全)

**Files:**
- Modify: `channels/teams/src/teams_adapter/downloader.py`
- Test: `channels/teams/tests/test_downloader.py`

- [ ] **Step 1: 写失败测试**

追加到 `channels/teams/tests/test_downloader.py`(顶部 import 补上
`import httpx`、`import respx`、`from teams_adapter.downloader import MAX_IMAGE_BYTES, TeamsImageDownloader`):

```python
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class StubIdentity:
    def get_token_audience(self):
        return "https://api.botframework.com"

    def get_token_scope(self):
        return ["https://api.botframework.com/.default"]


class StubProvider:
    async def get_access_token(self, resource_url, scopes, force_refresh=False):
        return "TOKEN"


class StubConnectionManager:
    def get_token_provider_from_activity(self, identity, activity):
        return StubProvider()


class FakeContext:
    def __init__(self, activity):
        self.activity = activity
        self.identity = StubIdentity()


def downloader() -> TeamsImageDownloader:
    return TeamsImageDownloader(StubConnectionManager())


async def download(attachments: list[dict], channel_id="msteams"):
    activity = attachments_activity(attachments)
    activity.channel_id = channel_id
    return await downloader().download_files(FakeContext(activity))


IMAGE_ATTACHMENT = [{"contentType": "image/png", "contentUrl": GOOD_URL}]


@respx.mock
async def test_downloads_with_bearer_token():
    route = respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=PNG, headers={"content-type": "image/png"}))
    files = await download(IMAGE_ATTACHMENT)
    assert len(files) == 1
    assert files[0].content == PNG
    assert files[0].content_type == "image/png"
    assert route.calls[0].request.headers["authorization"] == "Bearer TOKEN"


@respx.mock
async def test_never_requests_non_allowlisted_host():
    """安全回归:非白名单 host 一次请求都不能发出。"""
    route = respx.get(EVIL_URL).mock(return_value=httpx.Response(200, content=PNG))
    files = await download([{"contentType": "image/png", "contentUrl": EVIL_URL}])
    assert files == []
    assert route.called is False


@respx.mock
async def test_does_not_follow_redirects():
    """安全回归:跟随重定向会把 Authorization 带到重定向目标。"""
    first = respx.get(GOOD_URL).mock(return_value=httpx.Response(
        302, headers={"location": EVIL_URL}))
    evil = respx.get(EVIL_URL).mock(return_value=httpx.Response(200, content=PNG))
    files = await download(IMAGE_ATTACHMENT)
    assert files == []
    assert first.called is True
    assert evil.called is False


@respx.mock
async def test_401_is_not_retried():
    """安全回归:401 后重试加 token 正是 GHSA-7vwx-582j-j332 的成因。"""
    route = respx.get(GOOD_URL).mock(return_value=httpx.Response(401))
    files = await download(IMAGE_ATTACHMENT)
    assert files == []
    assert route.call_count == 1


@respx.mock
async def test_oversized_image_skipped():
    respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=b"0" * (MAX_IMAGE_BYTES + 1),
        headers={"content-type": "image/png"}))
    assert await download(IMAGE_ATTACHMENT) == []


@respx.mock
async def test_network_error_does_not_raise():
    """图片是增强,不能成为新的失败源。"""
    respx.get(GOOD_URL).mock(side_effect=httpx.ConnectError("boom"))
    assert await download(IMAGE_ATTACHMENT) == []


@respx.mock
async def test_non_image_response_content_type_normalized():
    respx.get(GOOD_URL).mock(return_value=httpx.Response(
        200, content=PNG, headers={"content-type": "application/octet-stream"}))
    files = await download(IMAGE_ATTACHMENT)
    assert files[0].content_type == "image/png"


async def test_non_teams_channel_skipped():
    assert await download(IMAGE_ATTACHMENT, channel_id="webchat") == []


async def test_no_attachments_makes_no_token_call():
    class ExplodingConnectionManager:
        def get_token_provider_from_activity(self, identity, activity):
            raise AssertionError("不应为无图消息取 token")

    activity = attachments_activity([])
    files = await TeamsImageDownloader(
        ExplodingConnectionManager()).download_files(FakeContext(activity))
    assert files == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest channels/teams/tests/test_downloader.py -v`
Expected: FAIL —— `ImportError: cannot import name 'TeamsImageDownloader'`

- [ ] **Step 3: 实现**

`channels/teams/src/teams_adapter/downloader.py` —— import 区补充:

```python
import httpx
from microsoft_agents.hosting.core import TurnContext
from microsoft_agents.hosting.core.app.input_file import (
    InputFile,
    InputFileDownloader,
)
```

文件末尾追加:

```python
class TeamsImageDownloader(InputFileDownloader):
    """下载 Teams inline image。由 AgentApplication 的 file_downloaders 管线调用,
    结果写入 state.temp.input_files。"""

    def __init__(self, connection_manager):
        self._connection_manager = connection_manager

    async def download_files(self, context: TurnContext) -> list[InputFile]:
        if context.activity.channel_id != TEAMS_CHANNEL_ID:
            return []
        urls = select_image_urls(context.activity.attachments)
        if not urls:
            return []                     # 无图不取 token
        try:
            token = await self._access_token(context)
        except Exception:
            logger.exception("failed to acquire bot token for attachments")
            return []

        files: list[InputFile] = []
        # follow_redirects=False:跟随会把 Authorization 带到重定向目标
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_S,
                                     follow_redirects=False) as client:
            for url in urls:
                downloaded = await self._fetch(client, url, token)
                if downloaded is not None:
                    files.append(downloaded)
        return files

    async def _access_token(self, context: TurnContext) -> str:
        provider = self._connection_manager.get_token_provider_from_activity(
            context.identity, context.activity)
        return await provider.get_access_token(
            context.identity.get_token_audience(),
            context.identity.get_token_scope())

    async def _fetch(self, client: httpx.AsyncClient, url: str,
                     token: str) -> InputFile | None:
        try:
            response = await client.get(
                url, headers={"Authorization": f"Bearer {token}"})
        except Exception:
            logger.warning("attachment download failed: %s", url, exc_info=True)
            return None

        if response.status_code != 200:
            # 绝不在 401/403 后重试加 token(GHSA-7vwx-582j-j332)
            logger.warning("attachment download status=%s url=%s",
                           response.status_code, url)
            return None

        content = response.content
        if len(content) > MAX_IMAGE_BYTES:
            logger.warning("attachment too large (%d bytes), skipped",
                           len(content))
            return None

        content_type = (response.headers.get("content-type", "")
                        .split(";")[0].strip().lower())
        if not content_type.startswith("image/"):
            content_type = "image/png"    # 与官方 SDK 一致的归一化
        return InputFile(content=content, content_type=content_type,
                         content_url=url)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest channels/teams/tests/test_downloader.py -v`
Expected: PASS(17 passed)

- [ ] **Step 5: 提交**

```bash
git add channels/teams/src/teams_adapter/downloader.py channels/teams/tests/test_downloader.py
git commit -m "feat(teams): TeamsImageDownloader with token, limits and CVE-hardened fetch"
```

---

## Task 8: extract 携带 images 与空消息谓词

**Files:**
- Modify: `channels/teams/src/teams_adapter/extract.py`
- Test: `channels/teams/tests/test_extract.py`

- [ ] **Step 1: 写失败测试**

`channels/teams/tests/test_extract.py` —— import 行改为:

```python
from advisor_shared.messages import ImageInput
from teams_adapter.extract import (
    is_empty,
    should_respond,
    strip_mentions,
    to_advisor_request,
)
```

文件末尾追加:

```python
def test_to_advisor_request_defaults_to_no_images():
    assert to_advisor_request(group_activity(), BOT_ID).images == []


def test_to_advisor_request_carries_images():
    img = ImageInput(data=b"PNG", mime_type="image/png")
    req = to_advisor_request(group_activity(), BOT_ID, [img])
    assert req.images == [img]


def test_is_empty_true_for_no_text_no_image():
    req = to_advisor_request(group_activity(text="<at>Advisor</at>"), BOT_ID)
    assert req.text == ""
    assert is_empty(req) is True


def test_is_empty_false_when_image_present():
    req = to_advisor_request(group_activity(text="<at>Advisor</at>"), BOT_ID,
                             [ImageInput(data=b"PNG", mime_type="image/png")])
    assert is_empty(req) is False


def test_is_empty_false_when_text_present():
    assert is_empty(to_advisor_request(group_activity(), BOT_ID)) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest channels/teams/tests/test_extract.py -v`
Expected: FAIL —— `ImportError: cannot import name 'is_empty'`

- [ ] **Step 3: 实现**

`channels/teams/src/teams_adapter/extract.py` —— 顶部 import 改为:

```python
from advisor_shared.messages import AdvisorRequest, ImageInput
```

`to_advisor_request` 签名与返回值改为:

```python
def to_advisor_request(activity: dict, bot_id: str,
                       images: list[ImageInput] | None = None) -> AdvisorRequest:
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
        images=list(images or []),
    )
```

文件末尾追加:

```python
def is_empty(request: AdvisorRequest) -> bool:
    """纯图片场景下 text 剥离 mention 后可能为空;两者皆空才算无内容。"""
    return not request.text and not request.images
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest channels/teams/tests/test_extract.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add channels/teams/src/teams_adapter/extract.py channels/teams/tests/test_extract.py
git commit -m "feat(teams): carry images in AdvisorRequest and add is_empty predicate"
```

---

## Task 9: bot 接线与丢弃回传

下载器丢弃图片是静默的。不回传会有两个坏结果:纯图片消息在下载全失败时变成
**零响应**;超数量时 agent 无法告知用户有图被忽略。`bot.py` 持有判断所需信息
—— 原始附件里的图片数与存活数之差。

**Files:**
- Modify: `channels/teams/src/teams_adapter/bot.py`
- Test: `channels/teams/tests/test_bot.py`

- [ ] **Step 1: 写失败测试**

`channels/teams/tests/test_bot.py` —— import 区补充:

```python
from microsoft_agents.activity import Activity, Attachment
from microsoft_agents.hosting.core.app.input_file import InputFile
from teams_adapter.bot import IMAGE_FETCH_FAILED, register_handlers
```

文件末尾追加:

```python
def _image_attachment(url="https://x/y") -> Attachment:
    return Attachment(content_type="image/png", content_url=url)


def _state_with_files(*files: InputFile) -> TurnState:
    state = TurnState()
    state.temp.input_files = list(files)
    return state


async def test_images_from_state_reach_core():
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    activity = group_activity()
    activity.attachments = [_image_attachment()]
    state = _state_with_files(
        InputFile(content=b"PNG", content_type="image/png", content_url=None))
    await handler(FakeTurnContext(activity), state)
    assert len(core.requests[0].images) == 1
    assert core.requests[0].images[0].data == b"PNG"
    assert core.requests[0].images[0].mime_type == "image/png"


async def test_text_only_message_still_has_no_images():
    """回归:无附件时行为与改动前一致。"""
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    await handler(FakeTurnContext(group_activity()), TurnState())
    assert core.requests[0].images == []


async def test_all_images_failed_replies_hint_without_calling_core():
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    activity = group_activity()
    activity.text = "<at>A</at>"                 # 剥离后为空,纯图片消息
    activity.attachments = [_image_attachment()]
    ctx = FakeTurnContext(activity)
    await handler(ctx, TurnState())              # input_files 空 = 全部下载失败
    assert core.requests == []
    assert IMAGE_FETCH_FAILED in ctx.sent[-1].text


async def test_empty_message_without_attachments_is_silent():
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    activity = group_activity()
    activity.text = "<at>A</at>"
    ctx = FakeTurnContext(activity)
    await handler(ctx, TurnState())
    assert core.requests == []
    assert ctx.sent == []                        # 静默,连提示都不发


async def test_partial_drop_appends_note_to_text():
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    activity = group_activity()
    activity.attachments = [_image_attachment("https://x/1"),
                            _image_attachment("https://x/2"),
                            _image_attachment("https://x/3")]
    state = _state_with_files(
        InputFile(content=b"P", content_type="image/png", content_url=None))
    await handler(FakeTurnContext(activity), state)
    assert "另有 2 张图片未能获取" in core.requests[0].text
    assert "登录失败" in core.requests[0].text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest channels/teams/tests/test_bot.py -v`
Expected: FAIL —— `ImportError: cannot import name 'IMAGE_FETCH_FAILED'`

- [ ] **Step 3: 实现**

`channels/teams/src/teams_adapter/bot.py` 整体替换为:

```python
# channels/teams/src/teams_adapter/bot.py
"""Teams handler 注册:触发判定 → typing → agent core → 渲染回复(spec 8.2)。
纯逻辑仍在 extract/render;此处只做 Agents SDK 对象与 dict 的桥接。"""
import logging

from advisor_shared.messages import ImageInput
from microsoft_agents.activity import Activity
from microsoft_agents.hosting.core import AgentApplication, TurnContext, TurnState

from advisor_agent.core import FALLBACK_MESSAGE
from advisor_agent.factory import set_current_channel_id, set_current_is_group
from teams_adapter.extract import is_empty, should_respond, to_advisor_request
from teams_adapter.render import render_reply

logger = logging.getLogger(__name__)

IMAGE_FETCH_FAILED = "图片没取到,能否把错误信息贴成文字?"


def _activity_to_dict(activity: Activity) -> dict:
    # Pydantic Activity → Bot-Schema-aliased dict,使 extract/render 继续看到
    # conversationType / channelData / channelId(camelCase),而非 snake_case。
    return activity.model_dump(by_alias=True, exclude_none=True)


def _to_image_inputs(state: TurnState) -> list[ImageInput]:
    """state.temp.input_files 由 TeamsImageDownloader 填充(图片输入 spec §5)。"""
    files = getattr(state.temp, "input_files", None) or []
    return [ImageInput(data=f.content, mime_type=f.content_type)
            for f in files if (f.content_type or "").startswith("image/")]


def _raw_image_count(activity: dict) -> int:
    return sum(1 for a in activity.get("attachments") or []
               if (a.get("contentType") or "").startswith("image/"))


def register_handlers(agent_app: AgentApplication, core):
    @agent_app.activity("message")
    async def on_message(context: TurnContext, state: TurnState):
        recipient = context.activity.recipient
        bot_id = recipient.id if recipient else ""
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

        images = _to_image_inputs(state)
        request = to_advisor_request(activity, bot_id, images)
        raw_images = _raw_image_count(activity)

        if is_empty(request):
            # 有图片附件却一张都没存活 = 下载全失败,必须告知而非静默
            if raw_images:
                await context.send_activity(Activity(
                    type="message", text=IMAGE_FETCH_FAILED))
            return

        skipped = raw_images - len(images)
        if skipped > 0:
            request = request.model_copy(update={
                "text": f"{request.text}(另有 {skipped} 张图片未能获取)".strip()})

        await context.send_activity(Activity(type="typing"))
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

注意:typing 指示器移到了空消息判定之后 —— 被忽略的消息不应该看到"正在输入"。
原有 `test_responds_with_typing_then_answer` 断言 `ctx.sent[0].type == "typing"`
仍成立,因为请求构造过程不发送任何 activity。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest channels/teams/ -v`
Expected: PASS(含原有全部用例)

- [ ] **Step 5: 提交**

```bash
git add channels/teams/src/teams_adapter/bot.py channels/teams/tests/test_bot.py
git commit -m "feat(teams): wire downloaded images into core with drop feedback"
```

---

## Task 10: 装配下载器

**Files:**
- Modify: `channels/teams/src/teams_adapter/__main__.py:40-47`
- Modify: `channels/teams/pyproject.toml`
- Test: `channels/teams/tests/test_app.py`

- [ ] **Step 1: 写失败测试**

追加到 `channels/teams/tests/test_app.py` 末尾:

```python
def test_agent_app_wires_image_downloader(monkeypatch):
    """装配断言:AgentApplication 必须拿到 TeamsImageDownloader。

    stub 的形状是实测出来的:AgentApplication 会读 authorization.connection_manager
    (agent_application.py:186),authorization 为 None 时又会要求显式
    connection_manager,所以两者都必须是有属性的对象,不能用裸 object()。
    """
    import teams_adapter.__main__ as entry
    from teams_adapter.downloader import TeamsImageDownloader

    class StubConnectionManager:
        def get_default_connection_configuration(self):
            return {}

    stub_cm = StubConnectionManager()

    class StubAuthorization:
        connection_manager = stub_cm

    monkeypatch.setattr(entry, "load_configuration_from_env", lambda env: {})
    monkeypatch.setattr(entry, "MsalConnectionManager", lambda **_: stub_cm)
    monkeypatch.setattr(entry, "CloudAdapter", lambda **_: None)
    monkeypatch.setattr(entry, "Authorization",
                        lambda *a, **k: StubAuthorization())
    monkeypatch.setattr(entry, "build_advisor", lambda channel_name: object())

    agent_app, _, _ = entry.build_agent_app()
    downloaders = agent_app._options.file_downloaders
    assert len(downloaders) == 1
    assert isinstance(downloaders[0], TeamsImageDownloader)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest channels/teams/tests/test_app.py -v`
Expected: FAIL —— `assert len([]) == 1`,`file_downloaders` 为空

- [ ] **Step 3: 实现**

`channels/teams/src/teams_adapter/__main__.py` —— import 区补充:

```python
from teams_adapter.downloader import TeamsImageDownloader
```

`build_agent_app()` 里 `AgentApplication[TurnState](...)` 的构造参数增加一行
(放在 `remove_recipient_mention` 之后、`**config` 之前):

```python
        file_downloaders=[TeamsImageDownloader(connection_manager)],
```

`channels/teams/pyproject.toml` —— `dependencies` 列表增加(现在 `httpx` 靠
`advisor-agent` 传递而来,直接依赖必须显式声明):

```toml
    "httpx>=0.27",
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest` (全量)
Expected: PASS —— 全部单元测试通过

- [ ] **Step 5: 提交**

```bash
git add channels/teams/src/teams_adapter/__main__.py channels/teams/pyproject.toml channels/teams/tests/test_app.py
git commit -m "feat(teams): register TeamsImageDownloader and declare httpx dependency"
```

---

## Task 11: eval 图片用例

**Files:**
- Modify: `agent/tests/test_eval_behavior.py`
- Modify: `agent/tests/eval_cases.yaml`
- Create: `agent/tests/fixtures/error_screenshot.png`、`agent/tests/fixtures/config_screenshot.png`

**这是追加用例,不修改任何现有断言。**

- [ ] **Step 1: 准备图片 fixture**

手工截取两张图并保存(每张控制在 100 KB 内)。内容要求是断言的依据,必须照做:

- `agent/tests/fixtures/error_screenshot.png` —— VS Code 中 Copilot 的错误提示,
  **须包含清晰可读的英文原文 `GitHub Copilot: Authentication failed`**
- `agent/tests/fixtures/config_screenshot.png` —— VS Code `settings.json` 中的
  Copilot 配置片段,**须包含清晰可读的 `github.copilot.enable` 这一行**

无法取到真实截图时,可用任意截图工具对着上述文本内容截屏 —— 关键是文字必须
可读,因为断言检验的正是模型的真实 OCR 与理解质量。fixture 缺失时测试会
自动 skip(见 Step 3),不会误报失败。

```bash
mkdir -p agent/tests/fixtures
```

- [ ] **Step 2: 写用例与加载逻辑**

`agent/tests/eval_cases.yaml` —— 文件末尾追加:

```yaml
  # 图片输入(2026-09-04):断言模型真实读出图中文本并接入检索瀑布
  - id: image-auth-error-screenshot
    text: "这是什么问题?怎么解决"
    images: [fixtures/error_screenshot.png]
    expected_stage_in: [kb_hit, live_hit, web, generic_advice]
    expect_answer_contains_any: ["认证", "登录", "Authentication"]
    reply_language: zh
  - id: image-config-screenshot-no-text
    text: ""
    images: [fixtures/config_screenshot.png]
    expected_stage_in: [kb_hit, live_hit, web, generic_advice]
    expect_answer_contains_any: ["github.copilot.enable", "配置", "settings"]
    reply_language: zh
```

`agent/tests/test_eval_behavior.py` —— import 区补上 `ImageInput`:

```python
from advisor_shared.messages import AdvisorRequest, ImageInput
```

`make_request` 替换为:

```python
FIXTURES_ROOT = Path(__file__).parent


def load_images(case) -> list[ImageInput]:
    images = []
    for rel_path in case.get("images") or []:
        path = FIXTURES_ROOT / rel_path
        if not path.exists():
            pytest.skip(f"missing image fixture: {path}")
        images.append(ImageInput(data=path.read_bytes(),
                                 mime_type="image/png",
                                 name=path.name))
    return images


def make_request(text: str, images: list[ImageInput] | None = None) -> AdvisorRequest:
    return AdvisorRequest(text=text, conversation_key=f"eval-{hash(text)}",
                          channel_id="19:eval", user_id="u",
                          user_name="eval", is_group=True,
                          images=images or [])
```

`test_eval_case` 的 turn 循环改为(图片只附在第一轮,与真实用户行为一致):

```python
    turns = case.get("multi_turn") or [case["text"]]
    key = f"eval-{case['id']}"
    images = load_images(case)
    for index, text in enumerate(turns):
        req = make_request(text, images if index == 0 else None)
        req = req.model_copy(update={"conversation_key": key})
        resp = await core.handle(req)
```

在现有 `reply_language` 断言之前插入:

```python
    keywords = case.get("expect_answer_contains_any") or []
    if keywords:
        lowered = resp.markdown.lower()
        assert any(k.lower() in lowered for k in keywords), \
            f"none of {keywords} in reply: {resp.markdown[:300]}"
```

- [ ] **Step 3: 确认默认测试套件不受影响**

Run: `uv run pytest`
Expected: PASS —— eval 带 `pytestmark = pytest.mark.integration`,默认 `-m 'not integration'` 不会跑到

- [ ] **Step 4: 跑 eval(需真实凭据 + vision 部署)**

Run: `uv run --env-file .env pytest -m integration agent/tests/test_eval_behavior.py -v`
Expected: 13 个用例全部 PASS。若两个图片用例 skip,说明 fixture 没放好;
若断言失败,先人工看 `resp.markdown` 判断是模型没读出文本(需换更清晰的截图)
还是关键词选得太窄(调整 `expect_answer_contains_any`)

- [ ] **Step 5: 提交**

```bash
git add agent/tests/eval_cases.yaml agent/tests/test_eval_behavior.py agent/tests/fixtures/
git commit -m "test(agent): eval cases for image error and config screenshots"
```

---

## Task 12: 收尾验证与文档

**Files:**
- Modify: `docs/superpowers/specs/2026-08-21-copilot-advisor-design.md`
- Modify: `README.md`

- [ ] **Step 1: 全量测试**

Run: `uv run pytest -v`
Expected: PASS,无 skip(integration 除外)

- [ ] **Step 2: 主 spec 交叉引用**

`docs/superpowers/specs/2026-08-21-copilot-advisor-design.md` —— §8.2 的
"关键规则"列表末尾追加一条:

```
- 图片输入:支持 Teams inline image(报错截图/配置截图/纯图片),
  详见 `2026-09-04-image-input-design.md`。图片不进会话历史,
  靠 system prompt 规则 9 的复述承载
```

同文件 §10.1 失败姿态表追加两行:

```
| 图片下载失败/超时 | 跳过该图以文本继续;无图存活且文本为空则提示改贴文字 |
| 部署不支持 vision | MAFBackend 内剥图重试一次,回答附未启用图片理解说明 |
```

- [ ] **Step 3: README 文档指引**

`README.md` 的"文档"小节追加一行:

```markdown
- 图片输入设计:`docs/superpowers/specs/2026-09-04-image-input-design.md`
```

- [ ] **Step 4: 手工冒烟(真实 Teams 租户)**

按 `docs/teams-setup.md` 启动,在测试租户里逐条验证:

1. 群里 @bot 并粘贴一张报错截图 + 文字 → 回答开头复述了图中错误
2. 群里 @bot 只粘贴截图不打字 → 正常回答,不是零响应
3. 1:1 私聊发截图 → 正常回答
4. 发一张截图后追问"那第二步怎么做" → 多轮上下文正常(历史里有复述)
5. 发一个 .docx 附件 → 被忽略,不报错

- [ ] **Step 5: 提交**

```bash
git add README.md docs/superpowers/specs/2026-08-21-copilot-advisor-design.md
git commit -m "docs: cross-reference image input design in main spec and README"
```

---

## 完成标准

- [ ] `uv run pytest` 全绿
- [ ] `uv run pytest -m integration agent/tests/test_eval_behavior.py` 全绿(需 vision 部署)
- [ ] Task 12 Step 4 的 5 条手工冒烟全部通过
- [ ] 安全回归测试存在且通过:非白名单 host 不发请求、不跟随重定向、401 不重试
- [ ] 纯文本路径无行为变化:`test_text_only_stays_plain_string`、
      `test_text_only_message_still_has_no_images` 通过
