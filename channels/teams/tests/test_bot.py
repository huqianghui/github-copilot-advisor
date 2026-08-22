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


async def test_sets_current_channel_id_and_is_group():
    from advisor_agent.factory import _channel_id_holder, _is_group_holder
    core = StubCore()
    bot = AdvisorBot(core, BOT_ID)
    ctx = FakeTurnContext(group_activity_dict())
    await bot.on_message_activity(ctx)
    assert _channel_id_holder["value"] == "19:c"
    assert _is_group_holder["value"] is True
