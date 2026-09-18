# channels/teams/src/teams_adapter/bot.py
"""Teams handler 注册:触发判定 → agent core → 渲染回复(spec 8.2)。
纯逻辑仍在 extract/render;此处只做 Agents SDK 对象与 dict 的桥接。"""
from advisor_shared.messages import ImageInput
from advisor_shared.telemetry import step, timed
from microsoft_agents.activity import Activity
from microsoft_agents.hosting.core import AgentApplication, TurnContext, TurnState

from teams_adapter.extract import should_respond
from teams_adapter.processing import (
    IMAGE_FETCH_FAILED as IMAGE_FETCH_FAILED,
    IDENTITY_UNAVAILABLE as IDENTITY_UNAVAILABLE,
    _raw_image_count as _raw_image_count,
    prepare_reply,
)


def _activity_to_dict(activity: Activity) -> dict:
    # Pydantic Activity → Bot-Schema-aliased dict,使 extract/render 继续看到
    # conversationType / channelData / channelId(camelCase),而非 snake_case。
    return activity.model_dump(by_alias=True, exclude_none=True)


def _to_image_inputs(state: TurnState) -> list[ImageInput]:
    """state.temp.input_files 由 TeamsImageDownloader 填充(图片输入 spec §5)。

    过滤非 image/* 不是防御性冗余:input_files 是 SDK 的通用附件管线,
    将来接入别的 downloader 就会混入 text/html 之类;而 ImageInput.mime_type
    带 ^image/[\\w.+-]+$ 正则,非图片直接抛 ValidationError 拖垮整个 turn。
    注意这里挡住的只是"非图片":那条正则会放行 image/svg+xml,四种格式的
    白名单(SUPPORTED_IMAGE_TYPES)只存在于 downloader.py,不在这一层。

    .lower() 两处对称(此处与 _raw_image_count):downloader 的 select_images
    比对前先 .lower(),所以 "IMAGE/PNG" 是它已钉死的可下载类型。这里用裸
    startswith 会把同一张图判成非图片 —— 详见 _raw_image_count。而且必须把
    小写后的值交给 ImageInput,不能只用它做判断:那条正则的 image/ 是字面量,
    mime_type="Image/PNG" 会 ValidationError。

    跳过 content 为空的项:SDK 的 InputFile 是无校验的 dataclass,
    content=None 能构造出来,喂给 ImageInput.data 会 ValidationError。
    本函数在 on_message 的 try 之外调用,抛出去就越过 fallback 杀死整个 turn。
    在这里跳过而不是把调用挪进 try,是因为空 content 本就等价于"这张图没取到":
    跳过后它会被算进 skipped,用户收到"另有 N 张未能获取"这句诚实的提示;
    走 fallback 则会连正文的回答一起丢掉。
    """
    files = getattr(state.temp, "input_files", None) or []
    images: list[ImageInput] = []
    for file in files:
        content_type = (file.content_type or "").lower()
        if not content_type.startswith("image/") or not file.content:
            continue
        images.append(ImageInput(data=file.content, mime_type=content_type))
    return images


def register_handlers(agent_app: AgentApplication, core):
    @agent_app.activity("message")
    @timed("teams.message")
    async def on_message(context: TurnContext, state: TurnState):
        recipient = context.activity.recipient
        bot_id = recipient.id if recipient else ""
        activity = _activity_to_dict(context.activity)
        images = _to_image_inputs(state) if should_respond(activity, bot_id) else []
        prepared = await prepare_reply(
            activity, images, core, bot_id=bot_id)
        reply = prepared.reply
        if reply is None:
            return
        attributes = {"outcome": prepared.send_outcome} if prepared.send_outcome else {}
        with step("teams.send", **attributes):
            await context.send_activity(Activity(
                type=reply["type"], text=reply["text"],
                entities=reply["entities"] or None))

    return on_message
