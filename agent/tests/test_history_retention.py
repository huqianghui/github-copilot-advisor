from datetime import datetime, timedelta, timezone

import pytest

from advisor_agent.core import AdvisorCore
from advisor_agent.sessions import InMemorySessionStore
from advisor_shared.messages import AdvisorRequest


NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


async def test_archive_keeps_messages_outside_model_window():
    store = InMemorySessionStore(utc_clock=lambda: NOW)
    for number in range(12):
        await store.append_turn(
            "user-a", f"q{number}", f"a{number}",
            turn_id=f"turn-{number}", written_at=NOW)
    history = await store.get("user-a")
    assert len(history) == 20
    assert history[0] == {"role": "user", "content": "q2"}
    assert history[-1] == {"role": "assistant", "content": "a11"}
    assert len(await store.read_archive("user-a")) == 24
    assert await store.get("user-b") == []


async def test_message_expiry_is_not_extended_by_new_activity():
    store = InMemorySessionStore(utc_clock=lambda: NOW)
    await store.append_turn(
        "a", "expired", "expired-answer", turn_id="old",
        written_at=NOW - timedelta(days=30))
    await store.append_turn(
        "a", "retained", "retained-answer", turn_id="boundary",
        written_at=NOW - timedelta(days=30) + timedelta(microseconds=1))
    await store.append_turn("a", "new", "new-answer", turn_id="new")
    assert [message["content"] for message in await store.get("a")] == [
        "retained", "retained-answer", "new", "new-answer"]


async def test_duplicate_turn_does_not_append_or_refresh_timestamp():
    clock = [NOW]
    store = InMemorySessionStore(utc_clock=lambda: clock[0])
    await store.append_turn("a", "question", "answer", turn_id="id")
    clock[0] += timedelta(days=29)
    await store.append_turn("a", "question", "answer", turn_id="id")
    archive = await store.read_archive("a")
    assert len(archive) == 2
    assert archive[0].written_at == NOW
    clock[0] += timedelta(days=1)
    assert await store.get("a") == []


async def test_duplicate_turn_with_different_content_is_rejected():
    store = InMemorySessionStore(utc_clock=lambda: NOW)
    await store.append_turn("a", "question", "answer", turn_id="id")
    with pytest.raises(ValueError, match="different"):
        await store.append_turn("a", "changed", "answer", turn_id="id")
    assert await store.get("a") == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]


async def test_turn_ids_and_context_windows_are_partition_scoped():
    store = InMemorySessionStore(utc_clock=lambda: NOW)
    for number in range(15):
        await store.append_turn(
            "tenant/chat/a", f"a-{number}", "answer-a", turn_id=str(number))
        await store.append_turn(
            "tenant/chat/b", f"b-{number}", "answer-b", turn_id=str(number))
    for user in ("a", "b"):
        history = await store.get(f"tenant/chat/{user}")
        assert len(history) == 20
        assert history[0]["content"] == f"{user}-5"
        assert all(message["content"].startswith((f"{user}-", f"answer-{user}"))
                   for message in history)


async def test_reading_history_cannot_mutate_stored_messages():
    store = InMemorySessionStore(utc_clock=lambda: NOW)
    await store.append_turn("a", "question", "answer", turn_id="id")
    history = await store.get("a")
    history[0]["content"] = "changed"
    assert (await store.get("a"))[0]["content"] == "question"


async def test_naive_timestamp_fails_before_any_message_is_stored():
    store = InMemorySessionStore(utc_clock=lambda: NOW)
    with pytest.raises(ValueError, match="timezone|aware"):
        await store.append_turn(
            "a", "question", "answer", turn_id="id",
            written_at=datetime(2026, 9, 18))
    assert await store.get("a") == []


@pytest.mark.parametrize("kwargs", [
    {"max_turns": 0}, {"max_turns": -1}, {"ttl_seconds": 0},
    {"ttl_seconds": float("nan")}, {"ttl_seconds": float("inf")},
])
def test_invalid_history_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        InMemorySessionStore(**kwargs)


async def test_legacy_append_still_expires_each_message_independently():
    clock = [0.0]
    store = InMemorySessionStore(ttl_seconds=60, clock=lambda: clock[0])
    await store.append("a", "user", "old")
    clock[0] = 59
    await store.append("a", "assistant", "new")
    clock[0] = 60
    assert await store.get("a") == [{"role": "assistant", "content": "new"}]


async def test_core_commits_one_atomic_turn_with_supplied_id():
    class Backend:
        async def run(self, user_text, history, images=None):
            return "answer"

    class AtomicStore:
        def __init__(self):
            self.turns = []

        async def get(self, key):
            return []

        async def append_turn(self, key, user, assistant, *, turn_id,
                              written_at=None):
            self.turns.append((key, user, assistant, turn_id))

    store = AtomicStore()
    core = AdvisorCore(Backend(), store, event_sink=lambda event: None)
    request = AdvisorRequest(
        text="question", conversation_key="partition", channel_id="chat",
        user_id="user", user_name="name", is_group=False)
    await core.handle(request, turn_id="job-1")
    assert store.turns == [("partition", "question", "answer", "job-1")]
