# channels/teams/tests/test_extract.py
from teams_adapter.extract import (
    should_respond,
    strip_mentions,
    to_advisor_request,
)

BOT_ID = "28:bot-app-id"


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
        },
        "from": {"id": "29:user1", "name": "张三"},
    }


def personal_activity(text="额度怎么看?"):
    return {
        "type": "message",
        "text": text,
        "entities": [],
        "conversation": {"id": "a:1to1conv", "conversationType": "personal"},
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
    # 同一 reply thread 共享会话:conversation.id 已含 messageid
    assert req.conversation_key == "19:chan@thread.tacv2;messageid=170001"
    assert req.user_id == "29:user1" and req.user_name == "张三"


def test_to_advisor_request_personal():
    req = to_advisor_request(personal_activity(), BOT_ID)
    assert req.is_group is False
    assert req.conversation_key == "a:1to1conv"
    assert req.channel_id == "a:1to1conv"   # 1:1 无 channel,退化为会话 id
