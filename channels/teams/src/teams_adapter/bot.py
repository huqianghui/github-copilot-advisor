# channels/teams/src/teams_adapter/bot.py
"""Teams handler 注册:触发判定 → agent core → 渲染回复(spec 8.2)。
纯逻辑仍在 extract/render;此处只做 Agents SDK 对象与 dict 的桥接。"""
import logging

from advisor_shared.messages import ImageInput
from advisor_shared.telemetry import step, timed
from microsoft_agents.activity import Activity
from microsoft_agents.hosting.core import AgentApplication, TurnContext, TurnState

from advisor_agent.core import FALLBACK_MESSAGE
from teams_adapter.extract import (
    ConversationIdentityError,
    is_empty,
    should_respond,
    to_advisor_request,
)
from teams_adapter.render import render_reply

logger = logging.getLogger(__name__)

IMAGE_FETCH_FAILED = "图片没取到,能否把错误信息贴成文字?"
IDENTITY_UNAVAILABLE = (
    "无法识别本次消息的会话或用户身份,为避免混用他人的上下文,本次未处理。"
    "请重新发送;若仍失败,请联系管理员。"
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


def _raw_image_count(activity: dict) -> int:
    """原始附件里的图片数。与 _to_image_inputs 同口径(都只认 image/*,
    且都先 .lower()),两者相减才是"被下载器丢掉的图片数",
    不会把 text/html 卡片算进去。

    .lower() 不可删:downloader.select_images 比对白名单前先 .lower(),
    于是 "IMAGE/PNG" 是一张已测试、可下载的合法图片。这里若用裸 startswith,
    唯一的差分输入(image/ 前缀带大写)会被下载器接受、被这里记零,后果是
    skipped = raw - len(images) 算出 0:图片全灭时用户看到 agent 正常回答,
    以为截图被看过了(谎报);纯图片消息更会因 raw_images == 0 走 is_empty
    提前返回,一个字都不回。跨接缝用例见 test_downloader.py。
    """
    return sum(1 for a in activity.get("attachments") or []
               if (a.get("contentType") or "").lower().startswith("image/"))


def register_handlers(agent_app: AgentApplication, core):
    @agent_app.activity("message")
    @timed("teams.message")
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
        try:
            request = to_advisor_request(activity, bot_id, images)
        except ConversationIdentityError as error:
            logger.warning(
                "conversation identity rejected field=%s reason=%s",
                error.field,
                error.reason,
            )
            with step("teams.send", outcome="identity_unavailable"):
                await context.send_activity(Activity(
                    type="message", text=IDENTITY_UNAVAILABLE))
            return
        raw_images = _raw_image_count(activity)

        if is_empty(request):
            # 有图片附件却一张都没存活 = 下载全失败,必须告知而非静默
            if raw_images:
                with step("teams.send", outcome="image_fetch_failed"):
                    await context.send_activity(Activity(
                        type="message", text=IMAGE_FETCH_FAILED))
            return

        skipped = raw_images - len(images)
        if skipped > 0:
            request = request.model_copy(update={
                "text": f"{request.text}(另有 {skipped} 张图片未能获取)".strip()})

        try:
            response = await core.handle(request)
            with step("teams.render"):
                reply = render_reply(response)
        except Exception as error:
            logger.error("core.handle failed error_type=%s", type(error).__name__)
            reply = {"type": "message", "text": FALLBACK_MESSAGE,
                     "entities": []}
        with step("teams.send"):
            await context.send_activity(Activity(
                type=reply["type"], text=reply["text"],
                entities=reply["entities"] or None))

    return on_message
