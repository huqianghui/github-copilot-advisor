import asyncio

import pytest

from advisor_agent.sessions import InMemorySessionStore
from advisor_agent.sessions import SessionTurnCoordinator


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


async def test_turns_are_fifo_and_other_keys_are_independent():
    coordinator = SessionTurnCoordinator()
    started = [asyncio.Event(), asyncio.Event()]
    order = []

    async def worker(index):
        started[index].set()
        async with coordinator.turn("same"):
            order.append(index)

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        async with coordinator.turn("same"):
            tasks.create_task(worker(0))
            await started[0].wait()
            tasks.create_task(worker(1))
            await started[1].wait()
            assert coordinator._entries["same"].references == 3
            assert order == []
            async with coordinator.turn("other"):
                order.append("other")
        assert "same" in coordinator._entries
    assert order == ["other", 0, 1]
    assert coordinator._entries == {}


async def test_cancelled_waiter_does_not_delete_held_lock():
    coordinator = SessionTurnCoordinator()
    waiting = asyncio.Event()

    async def worker():
        waiting.set()
        async with coordinator.turn("same"):
            pytest.fail("cancelled waiter entered")

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        async with coordinator.turn("same"):
            task = tasks.create_task(worker())
            await waiting.wait()
            entry = coordinator._entries["same"]
            assert entry.references == 2
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert coordinator._entries["same"] is entry
            assert entry.references == 1
    assert coordinator._entries == {}
    async with coordinator.turn("same"):
        assert coordinator._entries["same"].references == 1


async def test_cancelled_holder_releases_lock_for_waiter():
    coordinator = SessionTurnCoordinator()
    held, waiting = asyncio.Event(), asyncio.Event()
    entered = []

    async def holder():
        async with coordinator.turn("same"):
            held.set()
            await asyncio.Event().wait()

    async def waiter():
        waiting.set()
        async with coordinator.turn("same"):
            entered.append("waiter")

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        task = tasks.create_task(holder())
        await held.wait()
        tasks.create_task(waiter())
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert entered == ["waiter"]
    assert coordinator._entries == {}


async def test_turn_exception_releases_and_reclaims_lock():
    coordinator = SessionTurnCoordinator()
    with pytest.raises(RuntimeError, match="failed"):
        async with coordinator.turn("same"):
            raise RuntimeError("failed")
    assert coordinator._entries == {}
    async with coordinator.turn("same"):
        assert coordinator._entries["same"].references == 1
    assert coordinator._entries == {}
