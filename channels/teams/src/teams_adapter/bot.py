# channels/teams/src/teams_adapter/bot.py
"""Teams handler 注册:触发判定 → typing → agent core → 渲染回复(spec 8.2)。
纯逻辑仍在 extract/render;此处只做 Agents SDK 对象与 dict 的桥接。"""
import logging

from advisor_shared.messages import ImageInput
from microsoft_agents.activity import Activity
from microsoft_agents.hosting.core import AgentApplication, TurnContext, TurnState

from advisor_agent.core import FALLBACK_MESSAGE
from advisor_agent.factory import set_current_channel_id, set_current_is_group
from teams_adapter.extract import is_empty, should_respond, to_advisor_request
from teams_adapter.render import render_reply

logger = logging.getLogger(__name__)

IMAGE_FETCH_FAILED = "图片没取到,能否把错误信息贴成文字?"


def _activity_to_dict(activity: Activity) -> dict:
    # Pydantic Activity → Bot-Schema-aliased dict,使 extract/render 继续看到
    # conversationType / channelData / channelId(camelCase),而非 snake_case。
    return activity.model_dump(by_alias=True, exclude_none=True)


def _to_image_inputs(state: TurnState) -> list[ImageInput]:
    """state.temp.input_files 由 TeamsImageDownloader 填充(图片输入 spec §5)。

    过滤非 image/* 不是防御性冗余:input_files 是 SDK 的通用附件管线,
    将来接入别的 downloader 就会混入 text/html 之类;而 ImageInput.mime_type
    带 ^image/ 正则,非图片直接抛 ValidationError 拖垮整个 turn。
    """
    files = getattr(state.temp, "input_files", None) or []
    return [ImageInput(data=f.content, mime_type=f.content_type)
            for f in files if (f.content_type or "").startswith("image/")]


def _raw_image_count(activity: dict) -> int:
    """原始附件里的图片数。与 _to_image_inputs 同口径(都只认 image/*),
    两者相减才是"被下载器丢掉的图片数",不会把 text/html 卡片算进去。"""
    return sum(1 for a in activity.get("attachments") or []
               if (a.get("contentType") or "").startswith("image/"))


def register_handlers(agent_app: AgentApplication, core):
    @agent_app.activity("message")
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
        request = to_advisor_request(activity, bot_id, images)
        raw_images = _raw_image_count(activity)

        if is_empty(request):
            # 有图片附件却一张都没存活 = 下载全失败,必须告知而非静默
            if raw_images:
                await context.send_activity(Activity(
                    type="message", text=IMAGE_FETCH_FAILED))
            return

        skipped = raw_images - len(images)
        if skipped > 0:
            request = request.model_copy(update={
                "text": f"{request.text}(另有 {skipped} 张图片未能获取)".strip()})

        await context.send_activity(Activity(type="typing"))
        set_current_channel_id(request.channel_id)
        set_current_is_group(request.is_group)
        try:
            response = await core.handle(request)
            reply = render_reply(response)
        except Exception:
            logger.exception("core.handle failed")
            reply = {"type": "message", "text": FALLBACK_MESSAGE,
                     "entities": []}
        await context.send_activity(Activity(
            type=reply["type"], text=reply["text"],
            entities=reply["entities"] or None))

    return on_message
