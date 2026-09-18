import pytest

from advisor_agent.factory import AdvisorComponents, AzureAdvisor
from advisor_agent.sessions import BufferedSessionStore
from advisor_shared.messages import AdvisorRequest


class RecordingBackend:
    def __init__(self):
        self.histories = []

    async def run(self, user_text, history, images=None):
        self.histories.append(history)
        return f"answer:{user_text}"


def request(user):
    return AdvisorRequest(
        text=f"q-{user}", conversation_key=user, channel_id="chat",
        user_id=user, user_name=user, is_group=False)


async def test_component_cores_reuse_backend_but_keep_session_buffers_separate():
    backend = RecordingBackend()
    owner = AzureAdvisor(
        backend, channel_name="teams", azure_clients=(), credential_owner=None)
    components = AdvisorComponents(owner)
    a = BufferedSessionStore(
        [{"role": "user", "content": "a-only"}], partition_key="a")
    b = BufferedSessionStore([], partition_key="b")
    events = []
    await components.new_core(a, event_sink=events.append).handle(
        request("a"), turn_id="a-turn")
    await components.new_core(b, event_sink=events.append).handle(
        request("b"), turn_id="b-turn")
    assert backend.histories == [[{"role": "user", "content": "a-only"}], []]
    assert a.delta[0]["content"] == "q-a"
    assert b.delta[0]["content"] == "q-b"
    assert [event.channel for event in events] == ["teams", "teams"]
    await components.aclose()
    await components.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        components.new_core(a)


async def test_externally_closed_owner_cannot_create_new_core():
    owner = AzureAdvisor(
        RecordingBackend(), channel_name="teams", azure_clients=(),
        credential_owner=None)
    components = AdvisorComponents(owner)
    await owner.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        components.new_core(BufferedSessionStore([]))
