# channels/teams/tests/test_bot.py
from advisor_shared.messages import AdvisorResponse
from microsoft_agents.activity import Activity, Attachment
from microsoft_agents.hosting.core import (
    AgentApplication,
    MemoryStorage,
    TurnState,
)
from microsoft_agents.hosting.core.app.input_file import InputFile
from teams_adapter.bot import IMAGE_FETCH_FAILED, register_handlers

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


async def test_all_images_reach_core_in_order_with_own_types():
    """两张起步且 data/mime 各不相同:单张样本下 list()/[:1]/[-1:]/reversed()
    结果全一样,判别不了实现是否真的整体保序透传。"""
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    activity = group_activity()
    activity.attachments = [_image_attachment("https://x/1"),
                            _image_attachment("https://x/2"),
                            _image_attachment("https://x/3")]
    state = _state_with_files(
        InputFile(content=b"AAA", content_type="image/png", content_url=None),
        InputFile(content=b"BB", content_type="image/jpeg", content_url=None),
        InputFile(content=b"C", content_type="image/gif", content_url=None))
    await handler(FakeTurnContext(activity), state)
    assert [(i.data, i.mime_type) for i in core.requests[0].images] == [
        (b"AAA", "image/png"), (b"BB", "image/jpeg"), (b"C", "image/gif")]
    # 一张没丢 ⇒ 正文必须原样,不许挂"另有 0 张"的尾巴
    assert core.requests[0].text == "登录失败"


async def test_non_image_input_files_are_not_forwarded():
    """input_files 不保证全是图片;非 image/* 必须过滤掉,
    且不能被算成"未能获取"的图片(它压根不是图片)。"""
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    activity = group_activity()
    activity.attachments = [
        _image_attachment(),
        Attachment(content_type="text/html", content="<div>卡片</div>")]
    state = _state_with_files(
        InputFile(content=b"<div>", content_type="text/html",
                  content_url=None),
        InputFile(content=b"PNG", content_type="image/png", content_url=None))
    await handler(FakeTurnContext(activity), state)
    assert [(i.data, i.mime_type) for i in core.requests[0].images] == [
        (b"PNG", "image/png")]
    assert core.requests[0].text == "登录失败"


async def test_text_only_message_still_has_no_images():
    """回归:无附件时行为与改动前一致。"""
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    await handler(FakeTurnContext(group_activity()), TurnState())
    assert core.requests[0].images == []
    assert core.requests[0].text == "登录失败"


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
    # 被忽略/失败的消息不该先看到"正在输入"
    assert [a.type for a in ctx.sent] == ["message"]


async def test_all_images_failed_but_text_present_still_asks_core():
    """有正文时全灭不该退化成"图片没取到"了事 —— 正文本身仍值得回答,
    丢图信息挂在正文里带给 agent。"""
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    activity = group_activity()
    activity.attachments = [_image_attachment()]
    ctx = FakeTurnContext(activity)
    await handler(ctx, TurnState())
    assert core.requests[0].text == "登录失败(另有 1 张图片未能获取)"
    assert core.requests[0].images == []
    assert IMAGE_FETCH_FAILED not in ctx.sent[-1].text


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


async def test_partial_drop_note_survives_empty_text():
    """纯图片消息只活下来一张:正文为空,提示仍要带上,且仍然调用 core。"""
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    activity = group_activity()
    activity.text = "<at>A</at>"
    activity.attachments = [_image_attachment("https://x/1"),
                            _image_attachment("https://x/2")]
    state = _state_with_files(
        InputFile(content=b"P", content_type="image/png", content_url=None))
    await handler(FakeTurnContext(activity), state)
    assert core.requests[0].text == "(另有 1 张图片未能获取)"
    assert len(core.requests[0].images) == 1
