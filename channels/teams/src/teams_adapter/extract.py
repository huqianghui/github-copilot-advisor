# channels/teams/src/teams_adapter/extract.py
"""Teams activity → AdvisorRequest:纯函数,不依赖 Bot SDK 对象(spec 8.2)。"""
from advisor_shared.messages import AdvisorRequest


def _bot_mentioned(activity: dict, bot_id: str) -> bool:
    return any(
        e.get("type") == "mention"
        and (e.get("mentioned") or {}).get("id") == bot_id
        for e in activity.get("entities") or []
    )


def should_respond(activity: dict, bot_id: str) -> bool:
    if activity.get("type") != "message":
        return False
    conv_type = (activity.get("conversation") or {}).get("conversationType")
    if conv_type == "personal":
        return True
    return _bot_mentioned(activity, bot_id)


def strip_mentions(text: str, entities: list[dict], bot_id: str) -> str:
    for e in entities or []:
        if e.get("type") == "mention" and \
                (e.get("mentioned") or {}).get("id") == bot_id:
            text = text.replace(e.get("text", ""), "")
    return text.strip()


def to_advisor_request(activity: dict, bot_id: str) -> AdvisorRequest:
    conv = activity.get("conversation") or {}
    is_group = conv.get("conversationType") != "personal"
    channel_id = (
        ((activity.get("channelData") or {}).get("channel") or {}).get("id")
        or conv.get("id", "")
    )
    sender = activity.get("from") or {}
    return AdvisorRequest(
        text=strip_mentions(activity.get("text", ""),
                            activity.get("entities") or [], bot_id),
        conversation_key=conv.get("id", ""),
        channel_id=channel_id,
        user_id=sender.get("id", ""),
        user_name=sender.get("name", ""),
        is_group=is_group,
    )
