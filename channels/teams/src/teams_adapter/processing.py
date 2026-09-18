"""Prepare a Teams reply without performing final delivery."""
import logging
from dataclasses import dataclass
from typing import Literal

from advisor_agent.core import FALLBACK_MESSAGE
from advisor_shared.messages import ImageInput
from advisor_shared.telemetry import step
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


@dataclass(frozen=True)
class ReplyPreparation:
    reply: dict | None
    outcome: Literal["success", "degraded", "ignored"]
    send_outcome: Literal["identity_unavailable", "image_fetch_failed"] | None = None


def _raw_image_count(activity: dict) -> int:
    # Match downloader/_to_image_inputs normalization, including image/*.
    return sum(1 for attachment in activity.get("attachments") or []
               if (attachment.get("contentType") or "").lower().startswith("image/"))


async def prepare_reply(
    activity: dict, images: list[ImageInput], core, *,
    bot_id: str, turn_id: str | None = None,
) -> ReplyPreparation:
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
        return ReplyPreparation(None, "ignored")

    try:
        request = to_advisor_request(activity, bot_id, images)
    except ConversationIdentityError as error:
        logger.warning(
            "conversation identity rejected field=%s reason=%s",
            error.field, error.reason)
        return ReplyPreparation(
            {"type": "message", "text": IDENTITY_UNAVAILABLE, "entities": []},
            "degraded", send_outcome="identity_unavailable")
    raw_images = _raw_image_count(activity)
    if is_empty(request):
        if raw_images:
            return ReplyPreparation(
                {"type": "message", "text": IMAGE_FETCH_FAILED, "entities": []},
                "degraded", send_outcome="image_fetch_failed")
        return ReplyPreparation(None, "ignored")

    skipped = raw_images - len(images)
    if skipped > 0:
        request = request.model_copy(update={
            "text": f"{request.text}(另有 {skipped} 张图片未能获取)".strip()})
    try:
        response = (await core.handle(request) if turn_id is None else
                    await core.handle(request, turn_id=turn_id))
        with step("teams.render"):
            reply = render_reply(response)
    except Exception as error:
        logger.error("core.handle failed error_type=%s", type(error).__name__)
        return ReplyPreparation(
            {"type": "message", "text": FALLBACK_MESSAGE, "entities": []},
            "degraded")
    return ReplyPreparation(reply, "degraded" if skipped > 0 else "success")
