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
    before = len(list(app._route_list))
    handler = register_handlers(app, core)
    assert len(list(app._route_list)) == before + 1
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
