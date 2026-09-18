import pytest

from advisor_agent.sessions import BufferedSessionStore


async def test_buffer_is_a_private_snapshot_and_does_not_publish_staged_turn():
    seed = [{"role": "user", "content": "before"}]
    store = BufferedSessionStore(seed, partition_key="a")
    seed[0]["content"] = "external change"
    await store.append_turn("a", "question", "answer", turn_id="job-a")
    assert await store.get("a") == [{"role": "user", "content": "before"}]
    assert store.delta == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    assert store.turn_id == "job-a"


async def test_staged_delta_cannot_be_mutated_through_reader():
    store = BufferedSessionStore([])
    await store.append_turn("a", "question", "answer", turn_id="job-a")
    delta = store.delta
    delta[0]["content"] = "changed"
    assert store.delta[0]["content"] == "question"


async def test_bound_buffer_rejects_other_partition_before_returning_history():
    store = BufferedSessionStore(
        [{"role": "user", "content": "private"}], partition_key="a")
    with pytest.raises(ValueError, match="partition"):
        await store.get("b")
    with pytest.raises(ValueError, match="partition"):
        await store.append_turn("b", "q", "a", turn_id="job")
    assert store.delta == []


async def test_unbound_buffer_binds_once_and_rejects_cross_partition_commit():
    store = BufferedSessionStore([])
    assert await store.get("a") == []
    with pytest.raises(ValueError, match="partition"):
        await store.append_turn("b", "q", "a", turn_id="job")


async def test_duplicate_staging_requires_same_id_and_contents():
    store = BufferedSessionStore([])
    await store.append_turn("a", "q", "a", turn_id="id")
    await store.append_turn("a", "q", "a", turn_id="id")
    assert len(store.delta) == 2
    with pytest.raises(ValueError, match="different"):
        await store.append_turn("a", "changed", "a", turn_id="id")
    with pytest.raises(ValueError, match="one turn"):
        await store.append_turn("a", "q", "a", turn_id="another")


@pytest.mark.parametrize("history", [
    [{"role": "system", "content": "unexpected"}],
    [{"role": "user", "content": 1}],
    [{"role": "user", "content": "text", "Authorization": "private"}],
])
def test_invalid_seed_history_is_not_silently_used(history):
    with pytest.raises(ValueError):
        BufferedSessionStore(history)
