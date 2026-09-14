# Teams User Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teams 用户仅使用自己在当前租户、会话/线程中的历史,同键排队、不同键并行,工具上下文不串用。

**Architecture:** Teams adapter 生成版本化复合身份键,保持共享消息契约不变。核心通过按键锁协调完整回合,并绑定可恢复的请求与运行上下文。模型客户端和工具继续共享,历史及请求级状态不共享。

**Tech Stack:** Python >=3.11, asyncio/contextvars/contextlib/hashlib/json, Pydantic, Microsoft 365 Agents SDK, 现有 pytest/pytest-asyncio。

## Global Constraints

- 已批准设计:[2026-09-11-teams-user-isolation-design.md](../specs/2026-09-11-teams-user-isolation-design.md),提交 `efaf0a9`。
- 历史边界:租户 + 会话/线程 + 用户;任何一个身份维度不同都不共享历史。
- 回复可见性:仍在原来的 Teams 群/线程中公开回复,不新增私信。
- 存储策略:继续使用现有内存存储、1 小时 TTL、最多 20 条消息。
- 键格式固定为 `teams:user:v1:<digest>`,digest 为规范化三字符串 JSON 数组的 SHA-256 小写十六进制。
- 本次不增加数据库、外部缓存、Graph 查询、成员目录权限或新依赖。
- 不提供跨进程分布式排队、重启后历史恢复或 Teams 消息去重。
- 身份缺失:明确拒绝处理,不以空身份、共享键或单轮问答静默降级。
- 旧群历史:不迁移、不回退读取,上线后从新分区的空历史开始。
- 保持依赖方向 `channels/teams -> agent -> shared`;不扩大请求、响应和 backend 协议。
- 工作目录为仓库根目录;以下终端命令使用 Windows 路径。不要读取或输出 `.env` 内容。
- 仅使用现有测试设施;先跑定向测试,不默认运行付费 integration/eval 或整个仓库测试。
- 并发测试使用事件/屏障;`asyncio.timeout(5)` 只是死锁保护,不是靠时间判断顺序。
- 每个任务先红后绿;只提交本任务文件,不得撤销用户已有改动。手工编辑使用 apply_patch。
- 以下代码属于待实施计划,不是已经实现或已经运行的代码。

---

## 文件职责与任务顺序

| 文件 | 任务 | 职责 |
|---|---|---|
| `agent\src\advisor_agent\sessions.py` | 1 | 每键回合协调与锁引用回收;现有存储实现不改 |
| `agent\src\advisor_agent\core.py` | 1、2 | 完整回合排队、请求作用域与 planner 身份不变量 |
| `agent\src\advisor_agent\run_context.py` | 2 | 不可变请求上下文、可嵌套且可恢复的 scope |
| `agent\src\advisor_agent\factory.py` | 2 | provider 读取请求上下文;删除两个全局 holder/setter |
| `channels\teams\src\teams_adapter\bot.py` | 2、3 | 不再设置全局上下文;身份异常可见反馈 |
| `channels\teams\src\teams_adapter\extract.py` | 3 | 唯一身份校验和会话键构造 |
| 现有 session/core/context/factory/adapter 测试 | 1、2、3 | 对应单元和接线回归 |
| `agent\tests\test_eval_behavior.py`、`test_vision_pipeline.py` | 2 | 删除已废弃 setter 调用;请求原本已携带路由字段 |
| `channels\teams\tests\test_user_isolation.py`(新增) | 4 | SDK activity -> adapter -> core -> backend 的历史隔离验收 |
| Teams 联调、主设计和已知问题文档 | 4 | 新边界、切换影响、人工验收记录 |

按 1 -> 2 -> 3 -> 4 执行。每个任务完成后做独立审查。
本次不修改 `MAFBackend` 生产实现,通过其现有 provider/dispatch 接口验证接线。

## Task 1: 回合排队与锁生命周期

**Files:**
- Modify: `agent\src\advisor_agent\sessions.py`
- Modify: `agent\src\advisor_agent\core.py`
- Test: `agent\tests\test_sessions.py`
- Test: `agent\tests\test_core.py`

**Interfaces:**
- Consumes: 现有 `SessionStore.get(key: str) -> list[dict]` 和 `append(key: str, role: str, content: str) -> None`。
- Produces: `SessionTurnCoordinator()`;其 `turn(key: str)` 返回供 `async with` 使用的异步上下文管理器,进入值为 `None`。
- Produces: 核心实例成员 `_turns: SessionTurnCoordinator`。
- Produces: `AdvisorCore._handle_turn(request: AdvisorRequest) -> AdvisorResponse` 为原单轮处理方法;任务 2 会将它收敛为接受已规划请求和 `RunContext`。

- [ ] **Step 1: 在 session 测试中增加队列、异常和取消断言。**

保留现有四个存储测试,补充以下 imports 与测试:

```python
import asyncio

import pytest

from advisor_agent.sessions import SessionTurnCoordinator


async def test_turns_are_fifo_and_other_keys_are_independent():
    coordinator = SessionTurnCoordinator()
    started = [asyncio.Event(), asyncio.Event()]
    order = []

    async def worker(index):
        started[index].set()
        async with coordinator.turn("same"):
            order.append(index)

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        async with coordinator.turn("same"):
            tasks.create_task(worker(0))
            await started[0].wait()
            tasks.create_task(worker(1))
            await started[1].wait()
            assert coordinator._entries["same"].references == 3
            assert order == []
            async with coordinator.turn("other"):
                order.append("other")
        assert "same" in coordinator._entries
    assert order == ["other", 0, 1]
    assert coordinator._entries == {}


async def test_cancelled_waiter_does_not_delete_held_lock():
    coordinator = SessionTurnCoordinator()
    waiting = asyncio.Event()

    async def worker():
        waiting.set()
        async with coordinator.turn("same"):
            pytest.fail("cancelled waiter entered")

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        async with coordinator.turn("same"):
            task = tasks.create_task(worker())
            await waiting.wait()
            entry = coordinator._entries["same"]
            assert entry.references == 2
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert coordinator._entries["same"] is entry
            assert entry.references == 1
    assert coordinator._entries == {}
    async with coordinator.turn("same"):
        assert coordinator._entries["same"].references == 1


async def test_cancelled_holder_releases_lock_for_waiter():
    coordinator = SessionTurnCoordinator()
    held, waiting = asyncio.Event(), asyncio.Event()
    entered = []

    async def holder():
        async with coordinator.turn("same"):
            held.set()
            await asyncio.Event().wait()

    async def waiter():
        waiting.set()
        async with coordinator.turn("same"):
            entered.append("waiter")

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        task = tasks.create_task(holder())
        await held.wait()
        tasks.create_task(waiter())
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert entered == ["waiter"]
    assert coordinator._entries == {}


async def test_turn_exception_releases_and_reclaims_lock():
    coordinator = SessionTurnCoordinator()
    with pytest.raises(RuntimeError, match="failed"):
        async with coordinator.turn("same"):
            raise RuntimeError("failed")
    assert coordinator._entries == {}
    async with coordinator.turn("same"):
        assert coordinator._entries["same"].references == 1
    assert coordinator._entries == {}
```

在现有 `test_core.py` 中复用 `make_request`、`StubBackend` 和 `InMemorySessionStore`,
新增 `import asyncio`,再增加接线测试:

```python
async def test_core_serializes_one_key_without_blocking_other_keys():
    entered = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    followup_queued = asyncio.Event()
    other_finished = asyncio.Event()

    class BlockingBackend(StubBackend):
        async def run(self, user_text, history, images=None):
            entered.append(user_text)
            if user_text == "first":
                first_started.set()
                await release_first.wait()
            return await super().run(user_text, history, images)

    backend = BlockingBackend(reply="answer")
    core = AdvisorCore(backend, InMemorySessionStore(), event_sink=lambda e: None)

    async def followup():
        followup_queued.set()
        return await core.handle(make_request("followup"))

    async def other():
        response = await core.handle(make_request("other").model_copy(
            update={"conversation_key": "ck2", "user_id": "other"}))
        other_finished.set()
        return response

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        tasks.create_task(core.handle(make_request("first")))
        await first_started.wait()
        tasks.create_task(followup())
        await followup_queued.wait()
        assert entered == ["first"]
        tasks.create_task(other())
        await other_finished.wait()
        assert entered == ["first", "other"]
        release_first.set()
    histories = dict(backend.calls)
    assert histories["other"] == []
    assert histories["followup"] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
    ]
    assert core._turns._entries == {}


async def test_failed_turn_does_not_block_next_turn_or_save_failure():
    sessions = InMemorySessionStore()
    core = AdvisorCore(StubBackend(reply="ok", fail_times=1), sessions,
                       event_sink=lambda e: None)
    assert (await core.handle(make_request("failed"))).markdown == FALLBACK_MESSAGE
    assert (await core.handle(make_request("next"))).markdown == "ok"
    assert await sessions.get("ck1") == [
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "ok"},
    ]
    assert core._turns._entries == {}
```

- [ ] **Step 2: 运行同一 runner 的相关定向测试,确认新增测试先失败。**

Run: `uv run pytest agent\tests\test_sessions.py agent\tests\test_core.py -q`

Expected: 新 coordinator 不存在或核心尚未排队导致失败,不是缺少依赖。
如果确实遇到缺包,再按仓库开发说明恢复 workspace,不要提前安装工具。

- [ ] **Step 3: 实现 coordinator 并接入核心。**

在 `sessions.py` 增加以下定义,不改 `InMemorySessionStore` 或 `SessionStore`:

```python
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class _TurnEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    references: int = 0


class SessionTurnCoordinator:
    def __init__(self):
        self._entries: dict[str, _TurnEntry] = {}

    @asynccontextmanager
    async def turn(self, key: str) -> AsyncIterator[None]:
        entry = self._entries.get(key)
        if entry is None:
            entry = _TurnEntry()
            self._entries[key] = entry
        entry.references += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.references -= 1
            if entry.references == 0:
                del self._entries[key]
```

`core.py` 从 sessions 导入 `SessionTurnCoordinator`,在构造函数增加
`self._turns = SessionTurnCoordinator()`。将原来的 `handle` 方法重命名为
`_handle_turn`,函数体保持原样;新增以下入口方法:

```python
    async def handle(self, request: AdvisorRequest) -> AdvisorResponse:
        async with self._turns.turn(request.conversation_key):
            return await self._handle_turn(request)
```

引用登记、删除均位于无 await 的区段,只依赖单事件循环语义。
不要在仍有等待者时从字典移除条目,不要增加定时删除锁的任务。

- [ ] **Step 4: 重跑 Step 2,确认全部通过且既有 TTL/消息上限测试保持绿色。**

- [ ] **Step 5: 提交本任务。**

```powershell
git add -- agent\src\advisor_agent\sessions.py agent\src\advisor_agent\core.py agent\tests\test_sessions.py agent\tests\test_core.py
git diff --cached --check
git commit -m "fix(agent): serialize turns per session key" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

## Task 2: 请求作用域与全部 provider 调用面迁移

**Files:**
- Modify: `agent\src\advisor_agent\run_context.py`
- Modify: `agent\src\advisor_agent\core.py`
- Modify: `agent\src\advisor_agent\factory.py`
- Modify: `channels\teams\src\teams_adapter\bot.py`
- Modify: `agent\tests\test_eval_behavior.py`
- Modify: `agent\tests\test_vision_pipeline.py`
- Test: `agent\tests\test_run_context.py`
- Test: `agent\tests\test_core.py`
- Test: `agent\tests\test_factory.py`
- Test: `channels\teams\tests\test_bot.py`

**Interfaces:**
- Consumes: 任务 1 的 `_turns.turn(key)`;现有 `RunContext`、`current_run` 和 `AdvisorRequest`。
- Produces: 不可变 `RequestContext(channel_id: str, is_group: bool)`。
- Produces: `get_request_context() -> RequestContext`,未绑定时抛 `RuntimeError`。
- Produces: `request_scope(channel_id: str, is_group: bool)`,同步上下文管理器,进入值为新 `RunContext`。
- Produces: `_request_identity(request: AdvisorRequest) -> tuple[str, str, str, bool]`。
- Produces: `AdvisorCore._handle_turn(request: AdvisorRequest, run: RunContext) -> AdvisorResponse`。
- Preserves: `new_run() -> RunContext`,供现有独立工具测试使用;两个 factory provider 的签名不变。
- Removes: `set_current_channel_id`、`set_current_is_group`、`_channel_id_holder`、`_is_group_holder`。

- [ ] **Step 1: 新增 scope、planner 不变量和失败/取消测试。**

`test_run_context.py` 保留现有 `new_run` 测试,增加:

```python
import asyncio

from dataclasses import FrozenInstanceError

import pytest

from advisor_agent.run_context import get_request_context, request_scope


def test_request_context_requires_binding():
    with pytest.raises(RuntimeError, match="request context is not bound"):
        get_request_context()


def test_request_context_is_immutable():
    with request_scope("outer", False):
        with pytest.raises(FrozenInstanceError):
            setattr(get_request_context(), "is_group", True)


def test_nested_scopes_restore_request_and_run():
    with request_scope("outer", False) as outer:
        outer.stage = "kb_hit"
        with request_scope("inner", True) as inner:
            assert get_request_context().channel_id == "inner"
            assert get_request_context().is_group is True
            assert current_run.get() is inner
            assert inner is not outer
        assert get_request_context().channel_id == "outer"
        assert get_request_context().is_group is False
        assert current_run.get() is outer
        assert outer.stage == "kb_hit"
    with pytest.raises(RuntimeError):
        get_request_context()


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_scope_restores_both_contexts_after_failure(failure):
    with request_scope("outer", False) as outer:
        with pytest.raises(failure):
            with request_scope("inner", True):
                raise failure()
        assert get_request_context().channel_id == "outer"
        assert current_run.get() is outer
```

`test_core.py` 增加 `pytest`、`get_request_context`、`request_scope` imports,
以及以下测试。`StubBackend`、`make_request`、`current_run` 均为本文件已有符号。

```python
@pytest.mark.parametrize("in_place", [False, True])
@pytest.mark.parametrize(("field", "value"), [
    ("conversation_key", "wrong-key"),
    ("channel_id", "wrong-channel"),
    ("user_id", "wrong-user"),
    ("is_group", False),
])
async def test_planner_cannot_change_identity(field, value, in_place):
    class Planner:
        async def plan(self, request):
            if in_place:
                setattr(request, field, value)
                return request
            return request.model_copy(update={field: value})

    backend = StubBackend()
    sessions = InMemorySessionStore()
    core = AdvisorCore(backend, sessions, planner=Planner(),
                       event_sink=lambda e: None)
    with pytest.raises(ValueError, match="planner changed request identity"):
        await core.handle(make_request())
    assert backend.calls == []
    assert await sessions.get("ck1") == []
    assert await sessions.get("wrong-key") == []
    assert core._turns._entries == {}


async def test_planner_can_rewrite_text_inside_request_scope():
    class Planner:
        async def plan(self, request):
            assert get_request_context().channel_id == "19:abc"
            assert get_request_context().is_group is True
            return request.model_copy(update={"text": "rewritten"})

    backend = StubBackend()
    core = AdvisorCore(backend, InMemorySessionStore(), planner=Planner(),
                       event_sink=lambda e: None)
    with request_scope("outer", False) as outer:
        await core.handle(make_request())
        assert get_request_context().channel_id == "outer"
        assert current_run.get() is outer
    assert backend.calls[0][0] == "rewritten"


@pytest.mark.parametrize("failure_at", ["planner", "evaluator"])
async def test_core_failure_restores_context_and_releases_turn(failure_at):
    class Planner:
        async def plan(self, request):
            if failure_at == "planner":
                raise RuntimeError("failed")
            return request

    class Evaluator:
        async def evaluate(self, request, response):
            raise RuntimeError("failed")

    sessions = InMemorySessionStore()
    core = AdvisorCore(StubBackend(), sessions, planner=Planner(),
                       evaluator=Evaluator(), event_sink=lambda e: None)
    with request_scope("outer", False) as outer:
        with pytest.raises(RuntimeError, match="failed"):
            await core.handle(make_request())
        assert get_request_context().channel_id == "outer"
        assert current_run.get() is outer
    assert await sessions.get("ck1") == []
    assert core._turns._entries == {}


async def test_core_cancellation_restores_child_context_and_releases_turn():
    entered = asyncio.Event()

    class BlockingBackend(StubBackend):
        async def run(self, user_text, history, images=None):
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("blocking backend unexpectedly resumed")

    sessions = InMemorySessionStore()
    core = AdvisorCore(BlockingBackend(), sessions, event_sink=lambda e: None)
    with request_scope("outer", False) as outer:
        async def call():
            try:
                await core.handle(make_request())
            except asyncio.CancelledError:
                assert get_request_context().channel_id == "outer"
                assert current_run.get() is outer
                raise

        async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
            task = tasks.create_task(call())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert core._turns._entries == {}
    assert await sessions.get("ck1") == []
```

- [ ] **Step 2: 在 factory 测试中增加真实 dispatch 的交错验收。**

在 `test_factory.py` 使用已有 `azure_env` fixture 和 `build_advisor`。
该测试只 mock 模型循环、外部诊断客户端和用量客户端,保留真实 provider、
`MAFBackend._dispatch` 和 `AdvisorTools` 隐私门禁。增加以下 imports 与代码:

```python
import asyncio
import json
from unittest.mock import AsyncMock, call

from advisor_agent.factory import _channel_id_provider, _is_group_provider
from advisor_agent.run_context import current_run
from advisor_shared.messages import AdvisorRequest


def test_factory_providers_require_a_bound_request():
    for provider in (_channel_id_provider, _is_group_provider):
        with pytest.raises(RuntimeError, match="request context is not bound"):
            provider()


async def test_factory_tool_context_isolated_during_interleaved_turns(
        azure_env, monkeypatch, tmp_path):
    config = tmp_path / "channels.yaml"
    config.write_text(
        "defaults: {}\n"
        "channels:\n"
        "  - channel_id: '19:group'\n"
        "    enterprise_slug: group-enterprise\n"
        "    github_org: group-org\n"
        "    org_token_env: GROUP_TOKEN\n"
        "    contacts: []\n"
        "  - channel_id: '19:private'\n"
        "    enterprise_slug: private-enterprise\n"
        "    github_org: private-org\n"
        "    org_token_env: PRIVATE_TOKEN\n"
        "    contacts: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ESCALATION_CONFIG", str(config))
    monkeypatch.setenv("GROUP_TOKEN", "test-group-token")
    monkeypatch.setenv("PRIVATE_TOKEN", "test-private-token")
    core = build_advisor("teams")
    assert isinstance(core.backend, MAFBackend)
    backend = core.backend
    tools = backend._tools
    diagnostic_client = AsyncMock(return_value={})
    usage_client = AsyncMock(return_value={"source": "private"})
    monkeypatch.setattr(tools._diagnostics, "run", diagnostic_client)
    monkeypatch.setattr(tools._usage, "lookup", usage_client)
    escalation = AsyncMock(wraps=tools.escalate_to_human)
    diagnostics = AsyncMock(wraps=tools.network_diagnostics)
    usage = AsyncMock(wraps=tools.copilot_usage_lookup)
    monkeypatch.setattr(tools, "escalate_to_human", escalation)
    monkeypatch.setattr(tools, "network_diagnostics", diagnostics)
    monkeypatch.setattr(tools, "copilot_usage_lookup", usage)
    group_started, private_started = asyncio.Event(), asyncio.Event()
    group_dispatched = asyncio.Event()
    results = {}

    async def fake_loop(messages):
        label = messages[-1]["content"]
        if label == "group":
            group_started.set()
            await private_started.wait()
        else:
            private_started.set()
            await group_dispatched.wait()
        current_run.get().citations_seen.append(
            {"title": label, "url": f"https://example.test/{label}"})
        await backend._dispatch("escalate_to_human", {"reason": label})
        await backend._dispatch("network_diagnostics", {})
        results[label] = json.loads(await backend._dispatch(
            "copilot_usage_lookup",
            {"question_type": "user_usage", "username": "alice"}))
        if label == "group":
            group_dispatched.set()
        return label

    monkeypatch.setattr(backend, "_run_tool_loop", fake_loop)

    def request(label, is_group):
        return AdvisorRequest(
            text=label, conversation_key=label,
            channel_id=f"19:{label}", user_id=label, user_name=label,
            is_group=is_group,
        )

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        group = tasks.create_task(core.handle(request("group", True)))
        await group_started.wait()
        private = tasks.create_task(core.handle(request("private", False)))
    assert escalation.await_args_list == [
        call("19:group", "group"), call("19:private", "private")]
    assert diagnostics.await_args_list == [
        call("19:group"), call("19:private")]
    assert usage.await_args_list == [
        call("19:group", True, "user_usage", "alice"),
        call("19:private", False, "user_usage", "alice")]
    assert diagnostic_client.await_args_list == [
        call(enterprise_slug="group-enterprise"),
        call(enterprise_slug="private-enterprise")]
    assert results["group"]["status"] == "privacy_blocked"
    assert results["private"]["status"] == "ok"
    usage_client.assert_awaited_once_with(
        "user_usage", "private-org", "test-private-token", "alice")
    assert [c.title for c in group.result().citations] == ["group"]
    assert [c.title for c in private.result().citations] == ["private"]
```

- [ ] **Step 3: 先运行新增 scope/core/factory 测试,确认失败指向未实现行为。**

Run: `uv run pytest agent\tests\test_run_context.py agent\tests\test_core.py agent\tests\test_factory.py -q`

Expected: scope API 尚不存在、provider 未绑定仍返回默认值,或 planner 改写身份未被拒绝。

- [ ] **Step 4: 实现请求作用域,集中在核心绑定。**

向 `run_context.py` 增加以下 imports 与定义,保留已有 `RunContext`、`current_run`
和独立工具测试使用的 `new_run`:

```python
from collections.abc import Iterator
from contextlib import contextmanager


@dataclass(frozen=True)
class RequestContext:
    channel_id: str
    is_group: bool


_current_request: ContextVar[RequestContext] = ContextVar("current_request")


def get_request_context() -> RequestContext:
    try:
        return _current_request.get()
    except LookupError:
        raise RuntimeError("request context is not bound") from None


@contextmanager
def request_scope(channel_id: str, is_group: bool) -> Iterator[RunContext]:
    request_token = _current_request.set(RequestContext(channel_id, is_group))
    run = RunContext()
    run_token = current_run.set(run)
    try:
        yield run
    finally:
        current_run.reset(run_token)
        _current_request.reset(request_token)
```

`core.py` 改为导入 `RunContext, request_scope`,去掉 `new_run` import。
增加模块级身份快照函数:

```python
def _request_identity(request: AdvisorRequest) -> tuple[str, str, str, bool]:
    return (request.conversation_key, request.channel_id,
            request.user_id, request.is_group)
```

用以下方法替换任务 1 的入口:

```python
    async def handle(self, request: AdvisorRequest) -> AdvisorResponse:
        identity = _request_identity(request)
        async with self._turns.turn(identity[0]):
            with request_scope(identity[1], identity[3]) as run:
                planned = await self.planner.plan(request)
                if _request_identity(planned) != identity:
                    raise ValueError("planner changed request identity")
                return await self._handle_turn(planned, run)
```

原 `_handle_turn` 的签名改为下面这一行,并删去其开头原有的
`request = await self.planner.plan(request)` 与 `run = new_run()` 两行。
从 `history = await self.sessions.get(...)` 到原 `return response` 的代码保持原样,
包括图片摘要、backend 错误处理、evaluator、历史追加和事件生成。

```python
    async def _handle_turn(self, request: AdvisorRequest,
                           run: RunContext) -> AdvisorResponse:
```

`factory.py` 导入 `get_request_context`,删除两个 holder 与两个 setter,
两个 provider 改为:

```python
def _channel_id_provider() -> str:
    return get_request_context().channel_id


def _is_group_provider() -> bool:
    return get_request_context().is_group
```

- [ ] **Step 5: 同步所有旧 setter 调用面及 adapter 契约测试。**

语义引用分析已确认以下位置,不能只改 adapter:

1. `bot.py`:删除两个 setter 的 import 和调用;`await core.handle(request)` 原样保留。
2. `test_eval_behavior.py::test_eval_case`:import 只保留 `build_advisor`,删除
   `set_current_channel_id("19:eval")`;现有 `AdvisorRequest` 已携带渠道和群聊标记。
3. `test_vision_pipeline.py::test_image_bytes_actually_reach_the_model`:import
   只保留 `build_advisor`,删除两个 setter 调用;不改图片、请求或真实评估断言。
4. 将 `test_bot.py::test_sets_current_channel_id_and_is_group` 替换为:

```python
async def test_passes_channel_and_group_mode_to_core():
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    await handler(FakeTurnContext(group_activity()), TurnState())
    assert core.requests[0].channel_id == "19:c"
    assert core.requests[0].is_group is True
```

移除旧调用是删除操作,不要添加临时兼容 setter 或返回默认值的 shim。

- [ ] **Step 6: 合并运行受影响的单元测试,检查引用和诊断。**

Run: `uv run pytest agent\tests\test_sessions.py agent\tests\test_core.py agent\tests\test_run_context.py agent\tests\test_factory.py agent\tests\test_tools.py agent\tests\test_maf_backend.py channels\teams\tests\test_bot.py -q`

Expected: 全部定向单元测试通过,不执行真实模型请求。
用 Pylance 检查修改文件及两个 integration 测试文件的诊断;
再检索生产源码和测试,确认四个旧 holder/setter 名称无残留。
不要为了这次 import 清理执行真实 eval/vision integration。

- [ ] **Step 7: 提交本任务。**

```powershell
git add -- agent\src\advisor_agent\run_context.py agent\src\advisor_agent\core.py agent\src\advisor_agent\factory.py channels\teams\src\teams_adapter\bot.py agent\tests\test_run_context.py agent\tests\test_core.py agent\tests\test_factory.py channels\teams\tests\test_bot.py agent\tests\test_eval_behavior.py agent\tests\test_vision_pipeline.py
git diff --cached --check
git commit -m "fix(agent): scope tool context to each request" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

## Task 3: Teams 身份键、严格校验与用户反馈

**Files:**
- Modify: `channels\teams\src\teams_adapter\extract.py`
- Modify: `channels\teams\src\teams_adapter\bot.py`
- Test: `channels\teams\tests\test_extract.py`
- Test: `channels\teams\tests\test_bot.py`

**Interfaces:**
- Consumes: 现有 `to_advisor_request(activity: dict, bot_id: str, images: list[ImageInput] | None = None) -> AdvisorRequest`。
- Produces: `ConversationIdentityError(field: str, reason: str)`,继承 `ValueError`,只保存安全字段名与错误类别。
- Produces: `build_conversation_key(activity: dict) -> str`。
- Produces: `_identity_object(value: object, field: str) -> dict`、`_identity_id(value: object, field: str) -> str`,仅服务此模块的校验。
- Produces: adapter 常量 `IDENTITY_UNAVAILABLE`,使用设计中已确认的固定中文文案。

- [ ] **Step 1: 补全旧 fixture 的租户字段并更新旧的裸键断言。**

`test_extract.py` 的 `group_activity` 在 `channelData` 中保留 channel,增加
`"tenant": {"id": "tenant-a"}`;`personal_activity` 在 conversation 中增加
`"tenantId": "tenant-a"`。增加 `hashlib` import。

替换原来断言群/私聊使用裸 conversation ID 的两条断言:

```python
    assert req.conversation_key == "teams:user:v1:" + hashlib.sha256(
        b'["tenant-a","19:chan@thread.tacv2;messageid=170001","29:user1"]'
    ).hexdigest()
```

```python
    assert req.conversation_key == "teams:user:v1:" + hashlib.sha256(
        b'["tenant-a","a:1to1conv","29:user2"]'
    ).hexdigest()
```

将原“同一 reply thread 共享会话”的注释改为“同一租户、线程和发送者复用历史键”。
所有既有渠道 ID、用户名、图片及 @触发断言保留。
`test_bot.py` 的两种 activity fixture 同样加入租户,位置分别为 `channelData.tenant`
和 `conversation.tenantId`,其余属性不变。

- [ ] **Step 2: 增加纯提取的身份矩阵测试。**

在 `test_extract.py` 增加 `deepcopy`、`pytest`、
`ConversationIdentityError, build_conversation_key` imports 和以下代码:

```python
from copy import deepcopy

import pytest

from teams_adapter.extract import (
    ConversationIdentityError,
    build_conversation_key,
)

ABSENT = object()


def test_key_stable_when_text_and_display_name_change():
    activity = group_activity()
    key = build_conversation_key(activity)
    activity["text"] = "different question"
    activity["from"]["name"] = "renamed"
    assert build_conversation_key(activity) == key


@pytest.mark.parametrize("dimension", ["tenant", "conversation", "user"])
def test_each_identity_dimension_partitions_history(dimension):
    first = group_activity()
    second = deepcopy(first)
    if dimension == "tenant":
        second["channelData"]["tenant"]["id"] = "tenant-b"
    elif dimension == "conversation":
        second["conversation"]["id"] = "19:chan@thread.tacv2;messageid=170002"
    else:
        second["from"]["id"] = "29:user2"
    assert (to_advisor_request(first, BOT_ID).conversation_key
            != to_advisor_request(second, BOT_ID).conversation_key)


def test_structured_key_has_no_delimiter_ambiguity():
    first, second = group_activity(), group_activity()
    first["conversation"]["id"], first["from"]["id"] = "a:b", "c"
    second["conversation"]["id"], second["from"]["id"] = "a", "b:c"
    assert build_conversation_key(first) != build_conversation_key(second)


def test_tenant_fallback_matches_primary_source():
    activity = group_activity()
    key = build_conversation_key(activity)
    activity["conversation"]["tenantId"] = "tenant-a"
    assert build_conversation_key(activity) == key
    del activity["channelData"]["tenant"]
    assert build_conversation_key(activity) == key


def test_conflicting_tenants_are_rejected():
    activity = group_activity()
    activity["conversation"]["tenantId"] = "tenant-b"
    with pytest.raises(ConversationIdentityError) as error:
        to_advisor_request(activity, BOT_ID)
    assert error.value.reason == "conflicting"


@pytest.mark.parametrize("dimension", ["tenant", "conversation", "user"])
@pytest.mark.parametrize("value", [ABSENT, None, "", " \t", 123, [], {}])
def test_missing_or_invalid_identity_is_rejected(dimension, value):
    activity = group_activity()
    if dimension == "tenant":
        parent, field = activity["channelData"]["tenant"], "id"
    elif dimension == "conversation":
        parent, field = activity["conversation"], "id"
    else:
        parent, field = activity["from"], "id"
    if value is ABSENT:
        del parent[field]
    else:
        parent[field] = value
    with pytest.raises(ConversationIdentityError):
        to_advisor_request(activity, BOT_ID)


@pytest.mark.parametrize("value", [None, "", 123, []])
def test_invalid_primary_tenant_does_not_use_valid_fallback(value):
    activity = group_activity()
    activity["channelData"]["tenant"]["id"] = value
    activity["conversation"]["tenantId"] = "tenant-a"
    with pytest.raises(ConversationIdentityError):
        build_conversation_key(activity)


@pytest.mark.parametrize("field", ["channelData", "conversation", "from"])
def test_malformed_identity_container_is_rejected(field):
    activity = group_activity()
    activity[field] = "invalid-container"
    with pytest.raises(ConversationIdentityError):
        build_conversation_key(activity)
```

`test_bot.py` 增加 `pytest`,从 bot 导入 `IDENTITY_UNAVAILABLE`,添加:

```python
@pytest.mark.parametrize("missing", ["tenant", "conversation", "user"])
async def test_missing_identity_is_visible_and_never_reaches_core(
        missing, caplog):
    payload = group_activity().model_dump(by_alias=True, exclude_none=True)
    payload["text"] = "<at>A</at> private-test-question"
    if missing == "tenant":
        del payload["channelData"]["tenant"]
    elif missing == "conversation":
        del payload["conversation"]["id"]
    else:
        del payload["from"]["id"]
    context = FakeTurnContext(Activity.model_validate(payload))
    core = StubCore()
    await register_handlers(_agent_app(), core)(context, TurnState())
    assert core.requests == []
    assert [message.text for message in context.sent] == [IDENTITY_UNAVAILABLE]
    assert "conversation identity rejected" in caplog.text
    assert "private-test-question" not in caplog.text
    assert "29:u" not in caplog.text
    assert "tenant-a" not in caplog.text


async def test_sdk_serialization_preserves_both_supported_tenant_locations():
    for activity in (group_activity(), personal_activity()):
        context = FakeTurnContext(activity)
        core = StubCore()
        await register_handlers(_agent_app(), core)(context, TurnState())
        assert len(core.requests) == 1
        assert core.requests[0].conversation_key.startswith("teams:user:v1:")
        assert len(context.sent) == 2
```

- [ ] **Step 3: 运行提取与 handler 测试,确认新键/错误接口尚未实现。**

Run: `uv run pytest channels\teams\tests\test_extract.py channels\teams\tests\test_bot.py -q`

Expected: 新 API 不存在或新键断言失败。不要删除失败的旧行为测试来“过绿”。

- [ ] **Step 4: 实现唯一身份校验/编码函数。**

在 `extract.py` 增加标准库 imports 和以下定义:

```python
import hashlib
import json

_MISSING = object()


class ConversationIdentityError(ValueError):
    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


def _identity_object(value: object, field: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConversationIdentityError(field, "invalid")
    return value


def _identity_id(value: object, field: str) -> str:
    if value is _MISSING:
        raise ConversationIdentityError(field, "missing")
    if not isinstance(value, str) or not value.strip():
        raise ConversationIdentityError(field, "invalid")
    return value


def build_conversation_key(activity: dict) -> str:
    conversation = _identity_object(activity.get("conversation"), "conversation")
    sender = _identity_object(activity.get("from"), "from")
    channel_data = _identity_object(activity.get("channelData"), "channelData")
    tenant = _identity_object(channel_data.get("tenant"), "channelData.tenant")
    primary = tenant.get("id", _MISSING)
    alternate = conversation.get("tenantId", _MISSING)
    if primary is _MISSING:
        tenant_id = _identity_id(alternate, "conversation.tenantId")
    else:
        tenant_id = _identity_id(primary, "channelData.tenant.id")
        if alternate is not _MISSING:
            alternate_id = _identity_id(alternate, "conversation.tenantId")
            if alternate_id != tenant_id:
                raise ConversationIdentityError("tenant", "conflicting")
    conversation_id = _identity_id(
        conversation.get("id", _MISSING), "conversation.id")
    user_id = _identity_id(sender.get("id", _MISSING), "from.id")
    payload = json.dumps(
        [tenant_id, conversation_id, user_id],
        ensure_ascii=True, separators=(",", ":"),
    ).encode("utf-8")
    return "teams:user:v1:" + hashlib.sha256(payload).hexdigest()
```

在 `to_advisor_request` 的首行求值
`conversation_key = build_conversation_key(activity)`,
在构造 `AdvisorRequest` 时将原 `conversation_key=conv.get("id", "")`
替换为 `conversation_key=conversation_key`。其他提取逻辑原样保留,
特别是原始 `channel_id` 和图片顺序。

- [ ] **Step 5: 在 adapter 捕获专门的身份异常。**

`bot.py` 从 extract 导入 `ConversationIdentityError`。增加文案常量:

```python
IDENTITY_UNAVAILABLE = (
    "无法识别本次消息的会话或用户身份,为避免混用他人的上下文,本次未处理。"
    "请重新发送;若仍失败,请联系管理员。"
)
```

将原来的单行请求提取替换为:

```python
        try:
            request = to_advisor_request(activity, bot_id, images)
        except ConversationIdentityError as error:
            logger.warning(
                "conversation identity rejected field=%s reason=%s",
                error.field, error.reason,
            )
            await context.send_activity(Activity(
                type="message", text=IDENTITY_UNAVAILABLE))
            return
```

保留现有 `should_respond` 的早期触发判断、图片处理、typing 和核心错误兜底。
不增加宽泛 catch,不打印 activity 或异常中包含的原始身份值。

- [ ] **Step 6: 重跑本任务测试和相邻图片/渲染回归。**

Run: `uv run pytest channels\teams\tests\test_extract.py channels\teams\tests\test_bot.py channels\teams\tests\test_downloader.py channels\teams\tests\test_render.py -q`

Expected: 全部通过。检查编辑器诊断;调用 Pylance signature compatibility 验证
`to_advisor_request` 的既有调用仍兼容(签名应当完全不变)。

- [ ] **Step 7: 提交本任务。**

```powershell
git add -- channels\teams\src\teams_adapter\extract.py channels\teams\src\teams_adapter\bot.py channels\teams\tests\test_extract.py channels\teams\tests\test_bot.py
git diff --cached --check
git commit -m "fix(teams): partition history by tenant conversation and sender" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

## Task 4: 历史隔离跨层验收与使用文档

**Files:**
- Create: `channels\teams\tests\test_user_isolation.py`
- Modify: `docs\teams-setup.md`
- Modify: `docs\superpowers\specs\2026-08-21-copilot-advisor-design.md`
- Modify: `docs\known-issues.md`

**Interfaces:**
- Consumes: 任务 3 的 `to_advisor_request` 和现有 SDK Activity 转换;任务 1、2 的核心行为。
- Produces: 无网络的跨层隔离验收和明确标注未执行状态的 Teams 双用户人工清单。
- 不新增生产接口,不调整模型提示词来掩盖历史分区错误。

- [ ] **Step 1: 新增跨层测试文件,直接检查 backend 实参。**

```python
import pytest
from microsoft_agents.activity import Activity

from advisor_agent.core import AdvisorCore
from advisor_agent.sessions import InMemorySessionStore
from advisor_shared.messages import AdvisorRequest, ImageInput
from teams_adapter.bot import _activity_to_dict
from teams_adapter.extract import to_advisor_request

BOT_ID = "28:isolation-test"


def make_request(text="question", *, tenant="tenant-a",
                 conversation="chat-1", user="29:a",
                 kind="groupChat", images=None) -> AdvisorRequest:
    activity = Activity.model_validate({
        "type": "message",
        "text": f"<at>Advisor</at> {text}",
        "recipient": {"id": BOT_ID},
        "entities": [{
            "type": "mention",
            "mentioned": {"id": BOT_ID},
            "text": "<at>Advisor</at>",
        }],
        "conversation": {"id": conversation, "conversationType": kind},
        "channelData": {"tenant": {"id": tenant}},
        "from": {"id": user, "name": "display-name"},
    })
    return to_advisor_request(_activity_to_dict(activity), BOT_ID, images)


class RecordingBackend:
    def __init__(self):
        self.calls: list[tuple[str, list[dict]]] = []

    async def run(self, user_text: str, history: list[dict],
                  images: list[ImageInput] | None = None) -> str:
        self.calls.append((user_text, [dict(message) for message in history]))
        return f"answer:{user_text}"


async def test_a_b_a_receives_only_own_questions_and_answers():
    backend = RecordingBackend()
    events = []
    core = AdvisorCore(backend, InMemorySessionStore(), event_sink=events.append)
    first = make_request("A-only")
    second = make_request("B-only", user="29:b")
    third = make_request("A-followup")
    await core.handle(first)
    await core.handle(second)
    await core.handle(third)
    assert backend.calls == [
        ("A-only", []),
        ("B-only", []),
        ("A-followup", [
            {"role": "user", "content": "A-only"},
            {"role": "assistant", "content": "answer:A-only"},
        ]),
    ]
    assert [event.conversation_key for event in events] == [
        first.conversation_key, second.conversation_key, first.conversation_key]
    assert first.channel_id == second.channel_id == "chat-1"


@pytest.mark.parametrize(("tenant", "conversation", "kind"), [
    ("tenant-b", "chat-1", "groupChat"),
    ("tenant-a", "chat-2", "groupChat"),
    ("tenant-a", "personal-1", "personal"),
    ("tenant-a", "19:channel;messageid=1", "channel"),
])
async def test_same_user_does_not_inherit_history_in_another_context(
        tenant, conversation, kind):
    backend = RecordingBackend()
    core = AdvisorCore(backend, InMemorySessionStore(), event_sink=lambda e: None)
    await core.handle(make_request("original"))
    await core.handle(make_request(
        "elsewhere", tenant=tenant, conversation=conversation, kind=kind))
    assert backend.calls[-1] == ("elsewhere", [])


async def test_channel_threads_remain_separate_for_same_user():
    backend = RecordingBackend()
    core = AdvisorCore(backend, InMemorySessionStore(), event_sink=lambda e: None)
    await core.handle(make_request(
        "thread-one", conversation="19:channel;messageid=1", kind="channel"))
    await core.handle(make_request(
        "thread-two", conversation="19:channel;messageid=2", kind="channel"))
    assert backend.calls[-1] == ("thread-two", [])


async def test_new_key_never_reads_legacy_shared_history():
    sessions = InMemorySessionStore()
    await sessions.append("chat-1", "user", "legacy-shared-question")
    await sessions.append("chat-1", "assistant", "legacy-shared-answer")
    backend = RecordingBackend()
    core = AdvisorCore(backend, sessions, event_sink=lambda e: None)
    await core.handle(make_request("new"))
    assert backend.calls == [("new", [])]


async def test_image_history_marker_stays_with_its_sender():
    backend = RecordingBackend()
    core = AdvisorCore(backend, InMemorySessionStore(), event_sink=lambda e: None)
    await core.handle(make_request("image-context", images=[
        ImageInput(data=b"PNG", mime_type="image/png")]))
    await core.handle(make_request("B-followup", user="29:b"))
    await core.handle(make_request("A-followup"))
    assert backend.calls[1] == ("B-followup", [])
    assert backend.calls[2] == ("A-followup", [
        {"role": "user", "content": "[图片×1] image-context"},
        {"role": "assistant", "content": "answer:image-context"},
    ])
```

- [ ] **Step 2: 验证隔离测试能识别旧行为,然后验证新实现通过。**

Run: `uv run pytest channels\teams\tests\test_user_isolation.py -q`

Expected: 新实现全绿。为证明测试不是只“看起来有效”,临时在同一测试文件增加
下面的单个反事实测试,仅在测试作用域中注入旧的共享键行为:

```python
async def test_legacy_key_mutation_is_detected(monkeypatch):
    import teams_adapter.extract as extraction

    monkeypatch.setattr(
        extraction, "build_conversation_key",
        lambda activity: activity["conversation"]["id"],
    )
    with pytest.raises(AssertionError):
        await test_a_b_a_receives_only_own_questions_and_answers()
```

Run: `uv run pytest channels\teams\tests\test_user_isolation.py::test_legacy_key_mutation_is_detected -q`

Expected: 外层测试通过,证明内部 A/B 历史隔离断言在旧键行为下确实失败。
若内部断言没有识别到混用,外层将因没有抛出 AssertionError 而失败。
检查完成后删除这个临时反事实测试,保留 Step 1 的正式验收测试。
不回退 Git 文件、不留下故意失败的测试、不调用真实模型。

- [ ] **Step 3: 更新与行为直接相关的文档。**

在主设计 §2 决策 8、§7.4 和 §8.2 中,将“Teams 直接取 conversation.id/同串共享”
替换为以下规则,保留其他内容:

```markdown
Teams 历史按租户、完整会话/线程 ID 和发送者 ID 隔离,键为
`teams:user:v1:<SHA-256>`;具体编码与校验见
[用户隔离设计](2026-09-11-teams-user-isolation-design.md)。
core 将键视为不透明字符串,同键完整回合排队,不同键并行。
工具路由使用请求级上下文,不使用进程全局的渠道/群聊标记。
旧的群共享历史不迁移、不回退读取;现有内存 TTL 与条数上限不变。
```

将原主设计“MAF thread/session 维护历史”的表述改为当前实际的
`AdvisorCore + SessionStore` 维护历史,仅改该段与本次状态边界相关的措辞。

`docs\teams-setup.md` 在“冒烟清单”中明确原“同一 thread 追问”必须是同一用户,
并在图片小节之前加入:

```markdown
### 用户隔离

历史按租户、会话/线程、发送者隔离,回复仍公开发送到原群/线程。
同一分区按进入核心队列的顺序处理,其他分区可并行;不承诺按客户端发送时间重排。
上线切换不继承旧的混合群历史。历史仍在 1 小时 TTL 到期或进程重启后丢失,
最多保留 20 条消息;同一用户跨群、跨线程、跨私聊不共享。
缺少身份时会明确提示本次未处理,而不是进入共享历史。
本次不提供多实例共享历史或分布式排队。

以下双账号 Teams 验收尚未执行,执行后再逐项勾选:

- [ ] A、B 在同一群分别提供不同的非敏感测试标记,交错追问时各自只延续自己的标记。
- [ ] A 在同群连发两条问题,第二轮能够使用第一轮成功问答。
- [ ] A 在另一个群、频道线程或私聊追问,不自动继承原会话历史。
- [ ] 群聊和私聊并发,群内个人用量查询仍被隐私门禁拒绝。
- [ ] 原有 @触发、图片输入、引用和人工升级仍按原方式工作。
```

`docs\known-issues.md` 在 §7 下记录当前问题的根因和自动化覆盖,使用以下
不夸大人工验证的文字(只能在上述测试实际通过后加入):

```markdown
- **Teams 同群用户历史混用** —— 自动化回归已通过,双账号 Teams 验证尚未执行。
  原因是历史键只有 conversation.id,且工具渠道/群聊标记使用进程全局变量。
  现按租户、完整会话/线程、发送者分区,同键排队,工具上下文按请求绑定。
  旧混合历史不继承;内存 TTL、重启失忆和单进程限制保留。
  设计见 [用户隔离设计](superpowers/specs/2026-09-11-teams-user-isolation-design.md),
  人工动作见 [Teams 联调](teams-setup.md)。
```

- [ ] **Step 4: 运行最终定向回归,检查实际变更和诊断。**

Run:

```powershell
uv run pytest agent\tests\test_sessions.py agent\tests\test_core.py agent\tests\test_run_context.py agent\tests\test_factory.py agent\tests\test_tools.py agent\tests\test_maf_backend.py channels\teams\tests\test_extract.py channels\teams\tests\test_bot.py channels\teams\tests\test_user_isolation.py channels\teams\tests\test_downloader.py channels\teams\tests\test_render.py shared\tests\test_messages.py shared\tests\test_events.py -q
git diff --check
git --no-pager diff --stat
```

Expected: 所有列出的单元/接线测试通过;无 diff 空白错误;无新增编辑器诊断。
不把未执行的真实 Teams 联调、integration 或完整仓库测试写成“通过”。
若定向测试出现相关跨文件回归,再扩展测试范围;不修复与本需求无关的旧问题。

- [ ] **Step 5: 提交测试与文档,报告自动化和人工验证各自的状态。**

```powershell
git add -- channels\teams\tests\test_user_isolation.py docs\teams-setup.md docs\superpowers\specs\2026-08-21-copilot-advisor-design.md docs\known-issues.md
git diff --cached --check
git commit -m "test(teams): verify per-user history isolation end to end" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

## 计划自查与完成标准

| 设计要求 | 覆盖任务 |
|---|---|
| 版本化租户/会话/用户键、名称无关、租户 fallback/冲突、缺失拒绝 | 3 |
| 不修改路由字段、群内公开回复、旧历史不继承 | 3、4 |
| 同键完整回合排队、异键并行、引用回收、异常与两类取消 | 1、2 |
| 请求上下文成对绑定/恢复,未绑定时报错,真实工具隐私门禁 | 2 |
| planner 返回新对象或原地改变身份均不能越过分区 | 2 |
| 原始 user/assistant 历史实参、SDK tenant 序列化、真实 groupChat/频道/私聊 | 3、4 |
| 图片、引用、人工升级、TTL/条数和消息/事件契约 | 1、2、3、4 的现有回归 |
| eval/vision 的旧 setter 调用面不遗留 | 2 |
| 文档说明新历史、单进程范围、人工验证不可冒充已完成 | 4 |

实现完成必须提供:实际通过的定向命令和结果、改动文件、提交记录、
尚未执行的人工 Teams 验证。计划保存不代表这些步骤已经执行。
