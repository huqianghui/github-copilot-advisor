# channels/teams/src/teams_adapter/bot.py
"""Teams bot:触发判定 → typing → agent core → 渲染回复(spec 8.2)。
纯逻辑在 extract/render;此处只做 SDK 对象与 dict 的桥接。"""
import logging

from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import Activity

from advisor_agent.core import FALLBACK_MESSAGE
from advisor_agent.factory import set_current_channel_id, set_current_is_group
from teams_adapter.extract import should_respond, to_advisor_request
from teams_adapter.render import render_reply

logger = logging.getLogger(__name__)


def _activity_to_dict(activity) -> dict:
    if hasattr(activity, "serialize"):
        serialized = activity.serialize()
        if isinstance(serialized, dict):
            for source, target in zip(
                    getattr(activity, "entities", None) or [],
                    serialized.get("entities") or []):
                additional = getattr(source, "additional_properties", None)
                if isinstance(target, dict) and isinstance(additional, dict):
                    for key, value in additional.items():
                        target.setdefault(key, value)
            return serialized
    if hasattr(activity, "as_dict"):
        d = activity.as_dict()
        return d() if callable(d) else d
    return activity


class AdvisorBot(ActivityHandler):
    def __init__(self, core, bot_id: str):
        self.core = core
        self.bot_id = bot_id

    async def on_message_activity(self, turn_context: TurnContext):
        activity = _activity_to_dict(turn_context.activity)
        respond = should_respond(activity, self.bot_id)
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
        await turn_context.send_activity(Activity(type="typing"))
        request = to_advisor_request(activity, self.bot_id)
        set_current_channel_id(request.channel_id)
        set_current_is_group(request.is_group)
        try:
            response = await self.core.handle(request)
            reply = render_reply(response)
        except Exception:
            logger.exception("core.handle failed")
            reply = {"type": "message", "text": FALLBACK_MESSAGE,
                     "entities": []}
        await turn_context.send_activity(Activity(
            type=reply["type"], text=reply["text"],
            entities=reply["entities"] or None))
