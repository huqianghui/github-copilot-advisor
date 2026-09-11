# channels/teams/src/teams_adapter/extract.py
"""Teams activity → AdvisorRequest:纯函数,不依赖 Bot SDK 对象(spec 8.2)。"""
import hashlib
import json

from advisor_shared.messages import AdvisorRequest, ImageInput

_MISSING = object()


class ConversationIdentityError(ValueError):
    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


def _identity_object(value: object, field: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConversationIdentityError(field, "invalid")
    return value


def _identity_id(value: object, field: str) -> str:
    if value is _MISSING:
        raise ConversationIdentityError(field, "missing")
    if not isinstance(value, str) or not value.strip():
        raise ConversationIdentityError(field, "invalid")
    return value


def build_conversation_key(activity: dict) -> str:
    conversation = _identity_object(activity.get("conversation"), "conversation")
    sender = _identity_object(activity.get("from"), "from")
    channel_data = _identity_object(activity.get("channelData"), "channelData")
    tenant = _identity_object(channel_data.get("tenant"), "channelData.tenant")
    primary = tenant.get("id", _MISSING)
    alternate = conversation.get("tenantId", _MISSING)
    if primary is _MISSING:
        tenant_id = _identity_id(alternate, "conversation.tenantId")
    else:
        tenant_id = _identity_id(primary, "channelData.tenant.id")
        if alternate is not _MISSING:
            alternate_id = _identity_id(alternate, "conversation.tenantId")
            if alternate_id != tenant_id:
                raise ConversationIdentityError("tenant", "conflicting")
    conversation_id = _identity_id(
        conversation.get("id", _MISSING), "conversation.id")
    user_id = _identity_id(sender.get("id", _MISSING), "from.id")
    payload = json.dumps(
        [tenant_id, conversation_id, user_id],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "teams:user:v1:" + hashlib.sha256(payload).hexdigest()


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


def to_advisor_request(activity: dict, bot_id: str,
                       images: list[ImageInput] | None = None) -> AdvisorRequest:
    conversation_key = build_conversation_key(activity)
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
        conversation_key=conversation_key,
        channel_id=channel_id,
        user_id=sender.get("id", ""),
        user_name=sender.get("name", ""),
        is_group=is_group,
        images=list(images or []),
    )


def is_empty(request: AdvisorRequest) -> bool:
    """纯图片场景下 text 剥离 mention 后可能为空;两者皆空才算无内容。"""
    return not request.text and not request.images
