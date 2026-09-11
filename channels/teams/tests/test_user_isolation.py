import pytest
from microsoft_agents.activity import Activity

from advisor_agent.core import AdvisorCore
from advisor_agent.sessions import InMemorySessionStore
from advisor_shared.messages import AdvisorRequest, ImageInput
from teams_adapter.bot import _activity_to_dict
from teams_adapter.extract import to_advisor_request

BOT_ID = "28:isolation-test"


def make_request(text="question", *, tenant="tenant-a",
                 conversation="chat-1", user="29:a",
                 kind="groupChat", images=None) -> AdvisorRequest:
    activity = Activity.model_validate({
        "type": "message",
        "text": f"<at>Advisor</at> {text}",
        "recipient": {"id": BOT_ID},
        "entities": [{
            "type": "mention",
            "mentioned": {"id": BOT_ID},
            "text": "<at>Advisor</at>",
        }],
        "conversation": {"id": conversation, "conversationType": kind},
        "channelData": {"tenant": {"id": tenant}},
        "from": {"id": user, "name": "display-name"},
    })
    return to_advisor_request(_activity_to_dict(activity), BOT_ID, images)


class RecordingBackend:
    def __init__(self):
        self.calls: list[tuple[str, list[dict]]] = []

    async def run(self, user_text: str, history: list[dict],
                  images: list[ImageInput] | None = None) -> str:
        self.calls.append((user_text, [dict(message) for message in history]))
        return f"answer:{user_text}"


async def test_a_b_a_receives_only_own_questions_and_answers():
    backend = RecordingBackend()
    events = []
    core = AdvisorCore(backend, InMemorySessionStore(), event_sink=events.append)
    first = make_request("A-only")
    second = make_request("B-only", user="29:b")
    third = make_request("A-followup")
    await core.handle(first)
    await core.handle(second)
    await core.handle(third)
    assert backend.calls == [
        ("A-only", []),
        ("B-only", []),
        ("A-followup", [
            {"role": "user", "content": "A-only"},
            {"role": "assistant", "content": "answer:A-only"},
        ]),
    ]
    assert [event.conversation_key for event in events] == [
        first.conversation_key, second.conversation_key, first.conversation_key]
    assert first.channel_id == second.channel_id == "chat-1"


@pytest.mark.parametrize(("tenant", "conversation", "kind"), [
    ("tenant-b", "chat-1", "groupChat"),
    ("tenant-a", "chat-2", "groupChat"),
    ("tenant-a", "personal-1", "personal"),
    ("tenant-a", "19:channel;messageid=1", "channel"),
])
async def test_same_user_does_not_inherit_history_in_another_context(
        tenant, conversation, kind):
    backend = RecordingBackend()
    core = AdvisorCore(backend, InMemorySessionStore(), event_sink=lambda e: None)
    await core.handle(make_request("original"))
    await core.handle(make_request(
        "elsewhere", tenant=tenant, conversation=conversation, kind=kind))
    assert backend.calls[-1] == ("elsewhere", [])


async def test_channel_threads_remain_separate_for_same_user():
    backend = RecordingBackend()
    core = AdvisorCore(backend, InMemorySessionStore(), event_sink=lambda e: None)
    await core.handle(make_request(
        "thread-one", conversation="19:channel;messageid=1", kind="channel"))
    await core.handle(make_request(
        "thread-two", conversation="19:channel;messageid=2", kind="channel"))
    assert backend.calls[-1] == ("thread-two", [])


async def test_new_key_never_reads_legacy_shared_history():
    sessions = InMemorySessionStore()
    await sessions.append("chat-1", "user", "legacy-shared-question")
    await sessions.append("chat-1", "assistant", "legacy-shared-answer")
    backend = RecordingBackend()
    core = AdvisorCore(backend, sessions, event_sink=lambda e: None)
    await core.handle(make_request("new"))
    assert backend.calls == [("new", [])]


async def test_image_history_marker_stays_with_its_sender():
    backend = RecordingBackend()
    core = AdvisorCore(backend, InMemorySessionStore(), event_sink=lambda e: None)
    await core.handle(make_request("image-context", images=[
        ImageInput(data=b"PNG", mime_type="image/png")]))
    await core.handle(make_request("B-followup", user="29:b"))
    await core.handle(make_request("A-followup"))
    assert backend.calls[1] == ("B-followup", [])
    assert backend.calls[2] == ("A-followup", [
        {"role": "user", "content": "[图片×1] image-context"},
        {"role": "assistant", "content": "answer:image-context"},
    ])
