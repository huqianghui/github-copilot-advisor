# channels/teams/tests/test_extract.py
from copy import deepcopy
import hashlib

import pytest

from advisor_shared.messages import ImageInput
from teams_adapter.extract import (
    ConversationIdentityError,
    build_conversation_key,
    is_empty,
    should_respond,
    strip_mentions,
    to_advisor_request,
)

BOT_ID = "28:bot-app-id"
ABSENT = object()


def group_activity(text="<at>Advisor</at> Copilot 登录失败", mentions_bot=True):
    entities = []
    if mentions_bot:
        entities.append({
            "type": "mention",
            "mentioned": {"id": BOT_ID, "name": "Advisor"},
            "text": "<at>Advisor</at>",
        })
    return {
        "type": "message",
        "text": text,
        "entities": entities,
        "conversation": {
            "id": "19:chan@thread.tacv2;messageid=170001",
            "conversationType": "channel",
        },
        "channelData": {
            "channel": {"id": "19:chan@thread.tacv2"},
            "tenant": {"id": "tenant-a"},
        },
        "from": {"id": "29:user1", "name": "张三"},
    }


def personal_activity(text="额度怎么看?"):
    return {
        "type": "message",
        "text": text,
        "entities": [],
        "conversation": {
            "id": "a:1to1conv",
            "conversationType": "personal",
            "tenantId": "tenant-a",
        },
        "from": {"id": "29:user2", "name": "李四"},
    }


def test_group_with_mention_responds():
    assert should_respond(group_activity(), BOT_ID) is True


def test_group_without_mention_ignored():
    assert should_respond(group_activity(mentions_bot=False), BOT_ID) is False


def test_personal_always_responds():
    assert should_respond(personal_activity(), BOT_ID) is True


def test_non_message_ignored():
    activity = group_activity()
    activity["type"] = "conversationUpdate"
    assert should_respond(activity, BOT_ID) is False


def test_strip_mentions_removes_at_tag():
    a = group_activity()
    assert strip_mentions(a["text"], a["entities"], BOT_ID) == "Copilot 登录失败"


def test_to_advisor_request_group():
    req = to_advisor_request(group_activity(), BOT_ID)
    assert req.text == "Copilot 登录失败"
    assert req.is_group is True
    assert req.channel_id == "19:chan@thread.tacv2"
    # 同一租户、线程和发送者复用历史键
    assert req.conversation_key == "teams:user:v1:" + hashlib.sha256(
        b'["tenant-a","19:chan@thread.tacv2;messageid=170001","29:user1"]'
    ).hexdigest()
    assert req.user_id == "29:user1" and req.user_name == "张三"


def test_to_advisor_request_personal():
    req = to_advisor_request(personal_activity(), BOT_ID)
    assert req.is_group is False
    assert req.conversation_key == "teams:user:v1:" + hashlib.sha256(
        b'["tenant-a","a:1to1conv","29:user2"]'
    ).hexdigest()
    assert req.channel_id == "a:1to1conv"   # 1:1 无 channel,退化为会话 id


def test_to_advisor_request_defaults_to_no_images():
    assert to_advisor_request(group_activity(), BOT_ID).images == []


def test_to_advisor_request_carries_images():
    # 多张且逐张可区分:单张样本无法区分"全量透传"与"只取第一张"
    # (images[:1] 变异体会存活);顺序承重,同时挡住反转与去重。
    imgs = [
        ImageInput(data=b"PNG1", mime_type="image/png", name="a.png"),
        ImageInput(data=b"JPG2", mime_type="image/jpeg", name="b.jpg"),
        ImageInput(data=b"PNG3", mime_type="image/png", name="c.png"),
    ]
    req = to_advisor_request(group_activity(), BOT_ID, imgs)
    assert req.images == imgs


def test_is_empty_true_for_no_text_no_image():
    req = to_advisor_request(group_activity(text="<at>Advisor</at>"), BOT_ID)
    assert req.text == ""
    assert is_empty(req) is True


def test_is_empty_false_when_image_present():
    req = to_advisor_request(group_activity(text="<at>Advisor</at>"), BOT_ID,
                             [ImageInput(data=b"PNG", mime_type="image/png")])
    assert is_empty(req) is False


def test_is_empty_false_when_text_present():
    assert is_empty(to_advisor_request(group_activity(), BOT_ID)) is False


def test_key_stable_when_text_and_display_name_change():
    activity = group_activity()
    key = build_conversation_key(activity)
    activity["text"] = "different question"
    activity["from"]["name"] = "renamed"
    assert build_conversation_key(activity) == key


@pytest.mark.parametrize("dimension", ["tenant", "conversation", "user"])
def test_each_identity_dimension_partitions_history(dimension):
    first = group_activity()
    second = deepcopy(first)
    if dimension == "tenant":
        second["channelData"]["tenant"]["id"] = "tenant-b"
    elif dimension == "conversation":
        second["conversation"]["id"] = "19:chan@thread.tacv2;messageid=170002"
    else:
        second["from"]["id"] = "29:user2"
    assert (to_advisor_request(first, BOT_ID).conversation_key !=
            to_advisor_request(second, BOT_ID).conversation_key)


def test_structured_key_has_no_delimiter_ambiguity():
    first, second = group_activity(), group_activity()
    first["conversation"]["id"], first["from"]["id"] = "a:b", "c"
    second["conversation"]["id"], second["from"]["id"] = "a", "b:c"
    assert build_conversation_key(first) != build_conversation_key(second)


def test_tenant_fallback_matches_primary_source():
    activity = group_activity()
    key = build_conversation_key(activity)
    activity["conversation"]["tenantId"] = "tenant-a"
    assert build_conversation_key(activity) == key
    del activity["channelData"]["tenant"]
    assert build_conversation_key(activity) == key


def test_conflicting_tenants_are_rejected():
    activity = group_activity()
    activity["conversation"]["tenantId"] = "tenant-b"
    with pytest.raises(ConversationIdentityError) as error:
        to_advisor_request(activity, BOT_ID)
    assert error.value.reason == "conflicting"


@pytest.mark.parametrize("dimension", ["tenant", "conversation", "user"])
@pytest.mark.parametrize("value", [ABSENT, None, "", " \t", 123, [], {}])
def test_missing_or_invalid_identity_is_rejected(dimension, value):
    activity = group_activity()
    if dimension == "tenant":
        parent, field = activity["channelData"]["tenant"], "id"
    elif dimension == "conversation":
        parent, field = activity["conversation"], "id"
    else:
        parent, field = activity["from"], "id"
    if value is ABSENT:
        del parent[field]
    else:
        parent[field] = value
    with pytest.raises(ConversationIdentityError):
        to_advisor_request(activity, BOT_ID)


@pytest.mark.parametrize("value", [None, "", 123, []])
def test_invalid_primary_tenant_does_not_use_valid_fallback(value):
    activity = group_activity()
    activity["channelData"]["tenant"]["id"] = value
    activity["conversation"]["tenantId"] = "tenant-a"
    with pytest.raises(ConversationIdentityError):
        build_conversation_key(activity)


@pytest.mark.parametrize("field", ["channelData", "conversation", "from"])
def test_malformed_identity_container_is_rejected(field):
    activity = group_activity()
    activity[field] = "invalid-container"
    with pytest.raises(ConversationIdentityError):
        build_conversation_key(activity)
