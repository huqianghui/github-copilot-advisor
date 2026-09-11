import asyncio

import pytest

from advisor_agent.core import _SUMMARY_CHARS, FALLBACK_MESSAGE, AdvisorCore
from advisor_agent.run_context import current_run, get_request_context, request_scope
from advisor_agent.sessions import InMemorySessionStore
from advisor_shared.events import AdvisorEvent
from advisor_shared.messages import AdvisorRequest, ImageInput, MentionDirective


def make_request(text="Copilot 登录失败", images=None) -> AdvisorRequest:
    return AdvisorRequest(text=text, conversation_key="ck1",
                          channel_id="19:abc", user_id="u", user_name="n",
                          is_group=True, images=images or [])


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


def collect_events(bucket):
    return bucket.append


def _png(tag=b"x"):
    return ImageInput(data=tag, mime_type="image/png")


async def test_happy_path_returns_response_and_persists_session():
    events: list[AdvisorEvent] = []
    sessions = InMemorySessionStore()
    core = AdvisorCore(StubBackend("试试重启"), sessions,
                       event_sink=collect_events(events),
                       channel_name="teams")
    resp = await core.handle(make_request())
    assert resp.markdown == "试试重启"
    history = await sessions.get("ck1")
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert events[0].channel == "teams"
    assert events[0].stage == "generic_advice"  # 无工具调用时默认


async def test_history_passed_to_backend():
    sessions = InMemorySessionStore()
    await sessions.append("ck1", "user", "之前的问题")
    backend = StubBackend()
    core = AdvisorCore(backend, sessions, event_sink=lambda e: None)
    await core.handle(make_request("追问"))
    _, history = backend.calls[0]
    assert history[0]["content"] == "之前的问题"


async def test_citations_and_mentions_from_run_context():
    def side_effect():
        run = current_run.get()
        run.stage = "kb_hit"
        run.citations_seen.extend([
            {"title": "a", "url": "https://x/a"},
            {"title": "a-dup", "url": "https://x/a"},   # 同 url 去重
            {"title": "b", "url": "https://x/b"},
        ])
        run.mentions.append(MentionDirective(
            name="李四", platform_user_id="29:1", role="CSAM"))

    events: list[AdvisorEvent] = []
    core = AdvisorCore(StubBackend(side_effect=side_effect),
                       InMemorySessionStore(),
                       event_sink=collect_events(events))
    resp = await core.handle(make_request())
    assert [c.url for c in resp.citations] == ["https://x/a", "https://x/b"]
    assert resp.mentions[0].name == "李四"
    assert events[0].stage == "kb_hit"
    assert events[0].mentioned_human is True


async def test_core_does_not_retry_backend():
    """core 不再重试 —— 重试是 openai SDK 的职责(见 factory._MAX_RETRIES)。

    `fail_times=1` 是刻意选的判别值:旧的"重试 2 次"行为会在第二次成功、
    返回 "ok";现在必须一次就放弃并走兜底。core 这层的重试重跑的是整个
    tool loop,会重复执行 search_solutions / web_search —— 重复的 GitHub API
    调用和重复的 web search 计费,盲重试付不起这个代价。
    """
    events: list[AdvisorEvent] = []
    backend = StubBackend("ok", fail_times=1)
    core = AdvisorCore(backend, InMemorySessionStore(),
                       event_sink=collect_events(events))
    resp = await core.handle(make_request(images=[_png()]))
    assert len(backend.calls) == 1
    assert resp.markdown == FALLBACK_MESSAGE
    # 单次失败也要如实上报 —— 不重试不等于把错误吞掉。
    assert "llm down" in events[0].error


async def test_backend_exhausted_returns_fallback_and_skips_session():
    events: list[AdvisorEvent] = []
    sessions = InMemorySessionStore()
    core = AdvisorCore(StubBackend(fail_times=99), sessions,
                       event_sink=collect_events(events))
    resp = await core.handle(make_request())
    assert resp.markdown == FALLBACK_MESSAGE
    assert await sessions.get("ck1") == []
    assert "llm down" in events[0].error


async def test_images_forwarded_to_backend():
    backend = StubBackend()
    core = AdvisorCore(backend, InMemorySessionStore(),
                       event_sink=lambda e: None)
    img = _png()
    await core.handle(make_request(text="这是什么错误", images=[img]))
    assert backend.images_seen[0] == [img]


async def test_history_marks_images_when_text_empty():
    sessions = InMemorySessionStore()
    core = AdvisorCore(StubBackend(), sessions, event_sink=lambda e: None)
    await core.handle(make_request(text="", images=[_png(), _png(b"y")]))
    history = await sessions.get("ck1")
    assert history[0] == {"role": "user", "content": "[图片×2]"}


async def test_history_keeps_text_alongside_image_marker():
    sessions = InMemorySessionStore()
    core = AdvisorCore(StubBackend(), sessions, event_sink=lambda e: None)
    await core.handle(make_request(text="报这个错", images=[_png()]))
    history = await sessions.get("ck1")
    assert history[0]["content"] == "[图片×1] 报这个错"


async def test_event_records_image_count():
    events: list[AdvisorEvent] = []
    core = AdvisorCore(StubBackend(), InMemorySessionStore(),
                       event_sink=collect_events(events))
    await core.handle(make_request(images=[_png()]))
    assert events[0].image_count == 1


async def test_event_summary_marks_images_when_text_empty():
    """纯图片消息的摘要不能是空串 —— 事件是运营可见性的唯一出口。"""
    events: list[AdvisorEvent] = []
    core = AdvisorCore(StubBackend(), InMemorySessionStore(),
                       event_sink=collect_events(events))
    await core.handle(make_request(text="", images=[_png()]))
    assert events[0].question_summary == "[图片×1]"


async def test_event_summary_is_truncated():
    """摘要必须截断 —— 长问题不能把整段正文灌进事件日志。"""
    events: list[AdvisorEvent] = []
    core = AdvisorCore(StubBackend(), InMemorySessionStore(),
                       event_sink=collect_events(events))
    await core.handle(make_request(text="错" * 200))
    assert len(events[0].question_summary) == _SUMMARY_CHARS


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
