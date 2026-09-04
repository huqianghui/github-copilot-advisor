from advisor_agent.core import _SUMMARY_CHARS, FALLBACK_MESSAGE, AdvisorCore
from advisor_agent.run_context import current_run
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


async def test_backend_retry_then_success():
    backend = StubBackend("ok", fail_times=1)
    core = AdvisorCore(backend, InMemorySessionStore(),
                       event_sink=lambda e: None)
    img = _png()
    resp = await core.handle(make_request(images=[img]))
    assert resp.markdown == "ok" and len(backend.calls) == 2
    # 重试必须复现同一请求,含图片。"图片本身导致失败"由 backend 内部剥图
    # 降级处理(Task 4),不该让 core 盲重试去猜。
    assert backend.images_seen == [[img], [img]]


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
