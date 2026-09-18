from datetime import datetime, timedelta, timezone

from azure.core.exceptions import HttpResponseError
import pytest

from advisor_agent.durable.history import BlobHistoryStore, HistoryConflict
from advisor_agent.durable.leases import LeaseLost


NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)
PAIR = [{"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"}]


async def test_persisted_archive_exceeds_model_window_without_user_mixup(blob_gateway):
    first = BlobHistoryStore(blob_gateway, utc_clock=lambda: NOW)
    for number in range(12):
        await first.append_turn("a", f"q{number}", f"a{number}", turn_id=f"id-{number}")
    await first.append_turn("b", "private-b", "answer-b", turn_id="id-0")
    restarted = BlobHistoryStore(blob_gateway, utc_clock=lambda: NOW)
    assert len(await restarted.read_archive("a")) == 24
    history = await restarted.get("a")
    assert len(history) == 20
    assert history[0]["content"] == "q2"
    assert history[-1]["content"] == "a11"
    assert (await restarted.get("b"))[0]["content"] == "private-b"


async def test_index_write_failure_is_recovered_without_duplicate_turn(
        blob_gateway, fail_next_operation):
    store = BlobHistoryStore(blob_gateway, utc_clock=lambda: NOW)
    fail_next_operation("write", 503, "ServerBusy")
    with pytest.raises(HttpResponseError):
        await store.append_turn("a", "question", "answer", turn_id="id")
    assert await store.get("a") == []
    assert await store.read_archive("a") == []
    await store.append_turn("a", "question", "answer", turn_id="id")
    await store.append_turn("a", "question", "answer", turn_id="id")
    assert await store.get("a") == PAIR
    assert len(await store.read_archive("a")) == 2


async def test_duplicate_turn_cannot_change_content_or_refresh_time(blob_gateway):
    clock = [NOW]
    store = BlobHistoryStore(blob_gateway, utc_clock=lambda: clock[0])
    await store.append_turn("a", "question", "answer", turn_id="id")
    clock[0] += timedelta(days=29)
    await store.append_turn("a", "question", "answer", turn_id="id")
    with pytest.raises(HistoryConflict):
        await store.append_turn("a", "changed", "answer", turn_id="id")
    assert (await store.read_archive("a"))[0].written_at == NOW
    clock[0] += timedelta(days=1)
    assert await store.get("a") == []
    assert await store.read_archive("a") == []


async def test_permission_failure_is_not_new_or_empty_history(
        blob_gateway, fail_next_operation):
    store = BlobHistoryStore(blob_gateway, utc_clock=lambda: NOW)
    fail_next_operation("read", 403, "AuthorizationPermissionMismatch")
    with pytest.raises(HttpResponseError):
        await store.get("a")


async def test_broken_lease_cannot_publish_history(blob_gateway, expire_lease):
    store = BlobHistoryStore(blob_gateway, utc_clock=lambda: NOW)
    async with store.leased("a") as history:
        context = (await blob_gateway.list_page("context/")).names[0]
        await expire_lease(context)
        with pytest.raises(LeaseLost):
            await history.commit("id", 1, PAIR, NOW)
    assert await store.get("a") == []
    assert await store.read_archive("a") == []
