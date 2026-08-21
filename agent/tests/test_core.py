from advisor_agent.core import FALLBACK_MESSAGE, AdvisorCore
from advisor_agent.run_context import current_run
from advisor_agent.sessions import InMemorySessionStore
from advisor_shared.events import AdvisorEvent
from advisor_shared.messages import AdvisorRequest, MentionDirective


def make_request(text="Copilot 登录失败") -> AdvisorRequest:
    return AdvisorRequest(text=text, conversation_key="ck1",
                          channel_id="19:abc", user_id="u", user_name="n",
                          is_group=True)


class StubBackend:
    """记录调用;可注入副作用(模拟工具执行)与失败次数。"""
    def __init__(self, reply="答案", fail_times=0, side_effect=None):
        self.reply, self.fail_times = reply, fail_times
        self.side_effect = side_effect
        self.calls: list[tuple[str, list[dict]]] = []

    async def run(self, user_text, history):
        self.calls.append((user_text, list(history)))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("llm down")
        if self.side_effect:
            self.side_effect()
        return self.reply


def collect_events(bucket):
    return bucket.append


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
    resp = await core.handle(make_request())
    assert resp.markdown == "ok" and len(backend.calls) == 2


async def test_backend_exhausted_returns_fallback_and_skips_session():
    events: list[AdvisorEvent] = []
    sessions = InMemorySessionStore()
    core = AdvisorCore(StubBackend(fail_times=99), sessions,
                       event_sink=collect_events(events))
    resp = await core.handle(make_request())
    assert resp.markdown == FALLBACK_MESSAGE
    assert await sessions.get("ck1") == []
    assert "llm down" in events[0].error
