# channels/teams/tests/test_bot.py
import pytest

from advisor_shared.messages import AdvisorResponse
from microsoft_agents.activity import Activity, Attachment
from microsoft_agents.hosting.core import (
    AgentApplication,
    MemoryStorage,
    TurnState,
)
from microsoft_agents.hosting.core.app.input_file import InputFile
from teams_adapter.bot import IMAGE_FETCH_FAILED, IDENTITY_UNAVAILABLE, register_handlers

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
        "channelData": {"channel": {"id": "19:c"},
                        "tenant": {"id": "tenant-a"}},
        "from": {"id": "29:u", "name": "n"},
    })


def personal_activity() -> Activity:
    return Activity.model_validate({
        "type": "message",
        "text": "登录失败",
        "recipient": {"id": BOT_ID, "name": "bot"},
        "conversation": {"id": "19:personal", "conversationType": "personal",
                          "tenantId": "tenant-a"},
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


async def test_handler_sends_only_answer_typing_is_owned_by_middleware():
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    ctx = FakeTurnContext(group_activity())
    await handler(ctx, TurnState())
    assert len(core.requests) == 1
    assert core.requests[0].text == "登录失败"
    assert ctx.sent[0].type == "message"
    assert len(ctx.sent) == 1


async def test_handler_times_render_and_send_without_inline_typing():
    from advisor_shared.telemetry import trace_scope

    handler = register_handlers(_agent_app(), StubCore())
    with trace_scope() as trace:
        await handler(FakeTurnContext(group_activity()), TurnState())
    stages = {s.name: s for s in trace.timings}
    assert {"teams.message", "teams.render", "teams.send"} <= stages.keys()
    assert "teams.typing" not in stages
    assert stages["teams.send"].parent_span_id == stages["teams.message"].span_id


async def test_failed_reply_send_is_timed_and_propagates():
    from advisor_shared.telemetry import trace_scope

    class FailingContext(FakeTurnContext):
        async def send_activity(self, activity):
            if activity.type == "message":
                raise RuntimeError("private-response")
            return await super().send_activity(activity)

    handler = register_handlers(_agent_app(), StubCore())
    with trace_scope() as trace, pytest.raises(RuntimeError):
        await handler(FailingContext(group_activity()), TurnState())
    failures = [s for s in trace.timings if s.status == "error"]
    assert {s.name for s in failures} == {"teams.send", "teams.message"}


async def test_responds_to_personal_activity():
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    ctx = FakeTurnContext(personal_activity())
    await handler(ctx, TurnState())
    assert len(core.requests) == 1
    assert core.requests[0].text == "登录失败"
    assert core.requests[0].user_id == "29:u"
    assert len(ctx.sent) == 1


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


async def test_passes_channel_and_group_mode_to_core():
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    ctx = FakeTurnContext(group_activity())
    await handler(ctx, TurnState())
    assert core.requests[0].channel_id == "19:c"
    assert core.requests[0].is_group is True


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


async def test_uppercase_mime_survives_the_raw_count_minus_images_arithmetic():
    """_to_image_inputs 与 _raw_image_count 必须同口径 —— skipped 是两者相减。

    downloader 那侧已钉死"大写 MIME 是合法、可下载的图片"
    (test_downloader.py::test_content_type_is_normalized_to_lowercase),而
    input_files 是 SDK 的通用附件管线,换个 downloader 就可能原样带着
    "IMAGE/PNG" 进来。两侧口径一旦不一致,那个减法就会说谎,且方向相反:
      - 只有 _to_image_inputs 漏大写 → skipped=1,谎称"另有 1 张未能获取",
        可图明明取到了,而且它压根没进 core(少报 + 真丢图);
      - 只有 _raw_image_count 漏大写 → skipped=0,图片全灭也不吭声(谎报)。
    所以这条与 test_downloader.py 里那条跨接缝用例是一对,各钉一个方向;
    单独留任何一条,另一侧的 .lower() 都能被"清理"掉而全绿(实测过)。

    断言 mime_type 是小写而非原样,还钉住了另一条实测约束:
    ImageInput 的正则 ^image/[\\w.+-]+$ 里 image/ 是字面量,所以不能只拿
    .lower() 做判断、再把原值传进去 —— mime_type="IMAGE/PNG" 会
    ValidationError,而这个调用在 on_message 的 try 之外,直接杀死整个 turn。
    """
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    activity = group_activity()
    activity.attachments = [
        Attachment(content_type="IMAGE/PNG", content_url="https://x/1")]
    state = _state_with_files(
        InputFile(content=b"P", content_type="IMAGE/PNG", content_url=None))
    await handler(FakeTurnContext(activity), state)
    assert [(i.data, i.mime_type) for i in core.requests[0].images] == [
        (b"P", "image/png")]
    assert core.requests[0].text == "登录失败"    # 一张没丢 ⇒ 不许挂尾巴


async def test_input_file_with_empty_content_is_skipped_not_fatal():
    """SDK 的 InputFile 是**无校验的 dataclass**,content=None 能被构造出来;
    交给 ImageInput.data 会抛 ValidationError。而 _to_image_inputs 的调用在
    on_message 的 try 之外 —— 抛出去就越过 fallback 杀死整个 turn:
    没有回复、没有 FALLBACK_MESSAGE。今天不出事只是因为 _fetch 拒绝空 body,
    那是下载器的实现细节,不是这一层的保证,换个 downloader 就没了。

    行为选的是"跳过"而不是"走 fallback":空 content 等价于这张图没取到,
    跳过后它会被算进 skipped,用户收到"另有 1 张未能获取"这句诚实的提示,
    正文照样得到回答;走 fallback 则连正文的答案一起丢掉。
    """
    core = StubCore()
    handler = register_handlers(_agent_app(), core)
    activity = group_activity()
    activity.attachments = [_image_attachment("https://x/1"),
                            _image_attachment("https://x/2")]
    state = _state_with_files(
        InputFile(content=None, content_type="image/png", content_url=None),
        InputFile(content=b"P", content_type="image/png", content_url=None))
    ctx = FakeTurnContext(activity)
    await handler(ctx, state)
    assert [i.data for i in core.requests[0].images] == [b"P"]
    assert core.requests[0].text == "登录失败(另有 1 张图片未能获取)"


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


@pytest.mark.parametrize("missing", ["tenant", "conversation", "user"])
async def test_missing_identity_is_visible_and_never_reaches_core(
        missing, caplog):
    payload = group_activity().model_dump(by_alias=True, exclude_none=True)
    payload["text"] = "<at>A</at> private-test-question"
    if missing == "tenant":
        del payload["channelData"]["tenant"]
    elif missing == "conversation":
        payload["conversation"]["id"] = " "
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
        assert len(context.sent) == 1
