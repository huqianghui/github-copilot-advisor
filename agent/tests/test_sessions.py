from advisor_agent.sessions import InMemorySessionStore


async def test_empty_history_for_new_key():
    store = InMemorySessionStore()
    assert await store.get("k1") == []


async def test_append_and_get_roundtrip():
    store = InMemorySessionStore()
    await store.append("k1", "user", "登录失败")
    await store.append("k1", "assistant", "试试重启")
    history = await store.get("k1")
    assert history == [
        {"role": "user", "content": "登录失败"},
        {"role": "assistant", "content": "试试重启"},
    ]
    assert await store.get("k2") == []  # 隔离


async def test_max_turns_drops_oldest():
    store = InMemorySessionStore(max_turns=2)
    await store.append("k", "user", "1")
    await store.append("k", "assistant", "2")
    await store.append("k", "user", "3")
    assert [m["content"] for m in await store.get("k")] == ["2", "3"]


async def test_ttl_expires_whole_session():
    clock = [1000.0]
    store = InMemorySessionStore(ttl_seconds=60, clock=lambda: clock[0])
    await store.append("k", "user", "1")
    clock[0] += 61
    assert await store.get("k") == []
