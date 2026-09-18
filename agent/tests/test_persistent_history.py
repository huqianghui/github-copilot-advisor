import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from advisor_agent.durable.history import BlobHistoryStore, HistoryConflict, HistoryCorrupt


NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)
PAIR = [{"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"}]


@dataclass(frozen=True)
class Document:
    body: bytes
    etag: str
    last_modified: datetime = NOW


class ObjectStore:
    """Service-side conditional writes, not a mock of the history algorithm."""

    def __init__(self):
        self.documents = {}
        self.leases = {}
        self.locks = {}
        self.version = 0
        self.fail_context_write = False
        self.fail_receipt_create = False

    def _put(self, name, body):
        self.version += 1
        doc = Document(body, str(self.version))
        self.documents[name] = doc
        return doc.etag

    async def create(self, name, body):
        if name.startswith("commits/") and self.fail_receipt_create:
            self.fail_receipt_create = False
            raise ConnectionError("receipt transport failure")
        if name in self.documents:
            return False
        self._put(name, body)
        return True

    async def read(self, name):
        return self.documents.get(name)

    async def write(self, name, body, *, etag, lease_id=None):
        if self.fail_context_write:
            self.fail_context_write = False
            raise ConnectionError("context transport failure")
        if self.documents[name].etag != etag:
            raise RuntimeError("etag conflict")
        if self.leases.get(name) != lease_id or lease_id is None:
            raise RuntimeError("missing or wrong lease")
        return self._put(name, body)

    async def list_page(self, prefix, *, cursor=None, limit=200):
        names = sorted(name for name in self.documents if name.startswith(prefix))
        start = int(cursor or "0")
        end = start + limit
        return SimpleNamespace(names=names[start:end],
                               next_cursor=str(end) if end < len(names) else None)

    @asynccontextmanager
    async def lease(self, name):
        async with self.locks.setdefault(name, asyncio.Lock()):
            token = f"lease-{self.version}-{name}"
            self.leases[name] = token

            def assert_active():
                if self.leases.get(name) != token:
                    raise RuntimeError("lease lost")

            try:
                yield SimpleNamespace(id=token, assert_active=assert_active)
            finally:
                self.leases.pop(name, None)


@pytest.fixture
def objects():
    return ObjectStore()


async def test_retained_archive_and_model_window_are_separate(objects):
    store = BlobHistoryStore(objects, utc_clock=lambda: NOW)
    for number in range(12):
        await store.append_turn("a", f"q{number}", f"a{number}", turn_id=f"id-{number}")
    assert len(await store.read_archive("a")) == 24
    history = await store.get("a")
    assert len(history) == 20
    assert history[0]["content"] == "q2"
    assert await store.get("b") == []
    context = next(doc.body for name, doc in objects.documents.items()
                   if name.startswith("context/"))
    assert b'"content"' not in context


async def test_two_store_instances_share_only_the_same_partition(objects):
    first = BlobHistoryStore(objects, utc_clock=lambda: NOW)
    second = BlobHistoryStore(objects, utc_clock=lambda: NOW)
    await first.append_turn("a", "private-a", "answer-a", turn_id="same")
    await second.append_turn("b", "private-b", "answer-b", turn_id="same")
    assert (await second.get("a"))[0]["content"] == "private-a"
    assert (await first.get("b"))[0]["content"] == "private-b"


async def test_archive_written_before_failed_index_is_recovered_once(objects):
    store = BlobHistoryStore(objects, utc_clock=lambda: NOW)
    objects.fail_context_write = True
    with pytest.raises(ConnectionError):
        await store.append_turn("a", "question", "answer", turn_id="id")
    assert await store.get("a") == []
    assert await store.read_archive("a") == []
    await store.append_turn("a", "question", "answer", turn_id="id")
    await store.append_turn("a", "question", "answer", turn_id="id")
    assert await store.get("a") == PAIR
    assert len(await store.read_archive("a")) == 2


async def test_missing_commit_receipt_is_repaired_before_next_turn(objects):
    store = BlobHistoryStore(objects, utc_clock=lambda: NOW)
    objects.fail_receipt_create = True
    with pytest.raises(ConnectionError):
        await store.append_turn("a", "question", "answer", turn_id="one")
    await store.append_turn("a", "second", "second-answer", turn_id="two")
    assert len(await store.read_archive("a")) == 4


async def test_old_duplicate_outside_context_does_not_reappear_in_window(objects):
    store = BlobHistoryStore(objects, utc_clock=lambda: NOW)
    for number in range(12):
        await store.append_turn("a", f"q{number}", f"a{number}", turn_id=str(number))
    await store.append_turn("a", "q0", "a0", turn_id="0")
    assert len(await store.read_archive("a")) == 24
    assert (await store.get("a"))[0]["content"] == "q2"
    with pytest.raises(HistoryConflict):
        await store.append_turn("a", "changed", "a0", turn_id="0")


async def test_expired_references_do_not_require_deleted_archive(objects):
    clock = [NOW]
    store = BlobHistoryStore(objects, utc_clock=lambda: clock[0])
    await store.append_turn("a", "question", "answer", turn_id="id")
    clock[0] += timedelta(days=30)
    for name in list(objects.documents):
        if name.startswith("history/"):
            del objects.documents[name]
    assert await store.get("a") == []
    assert await store.read_archive("a") == []


async def test_unexpired_missing_archive_is_not_empty_history(objects):
    store = BlobHistoryStore(objects, utc_clock=lambda: NOW)
    await store.append_turn("a", "question", "answer", turn_id="id")
    for name in list(objects.documents):
        if name.startswith("history/"):
            del objects.documents[name]
    with pytest.raises(HistoryCorrupt):
        await store.get("a")


async def test_archive_corruption_is_not_returned_to_model(objects):
    store = BlobHistoryStore(objects, utc_clock=lambda: NOW)
    await store.append_turn("a", "question", "answer", turn_id="id")
    name = next(name for name in objects.documents if name.startswith("history/"))
    old = objects.documents[name]
    objects.documents[name] = Document(old.body.replace(b"question", b"tampered"), old.etag)
    with pytest.raises(HistoryCorrupt):
        await store.get("a")


async def test_explicit_sequence_cannot_rewind_context(objects):
    store = BlobHistoryStore(objects, utc_clock=lambda: NOW)
    async with store.leased("a") as history:
        await history.commit("latest", 10, PAIR, NOW)
        with pytest.raises(HistoryConflict):
            await history.commit("late", 9, PAIR, NOW)
    assert len(await store.read_archive("a")) == 2


async def test_receipt_does_not_hide_a_lost_projection(objects):
    store = BlobHistoryStore(objects, utc_clock=lambda: NOW)
    await store.append_turn("a", "question", "answer", turn_id="id")
    name = next(name for name in objects.documents if name.startswith("context/"))
    del objects.documents[name]
    with pytest.raises(HistoryCorrupt):
        await store.append_turn("a", "question", "answer", turn_id="id")


async def test_duplicate_commit_does_not_hide_a_missing_archive(objects):
    store = BlobHistoryStore(objects, utc_clock=lambda: NOW)
    await store.append_turn("a", "question", "answer", turn_id="id")
    name = next(name for name in objects.documents if name.startswith("history/"))
    del objects.documents[name]
    async with store.leased("a") as history:
        with pytest.raises(HistoryCorrupt):
            await history.commit("id", 1, PAIR, NOW)


async def test_failed_history_read_cancels_other_inflight_reads(objects, monkeypatch):
    store = BlobHistoryStore(objects, utc_clock=lambda: NOW)
    await store.append_turn("a", "first", "answer", turn_id="one")
    await store.append_turn("a", "second", "answer", turn_id="two")
    slow = next(name for name, doc in objects.documents.items()
                if name.startswith("history/") and b'"turn_id":"one"' in doc.body)
    broken = next(name for name, doc in objects.documents.items()
                  if name.startswith("history/") and b'"turn_id":"two"' in doc.body)
    started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_read = objects.read

    async def read(name):
        if name == slow:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        if name == broken:
            await started.wait()
            return None
        return await original_read(name)

    monkeypatch.setattr(objects, "read", read)
    try:
        async with asyncio.timeout(2):
            with pytest.raises(HistoryCorrupt):
                await store.get("a")
        assert cancelled.is_set()
    finally:
        release.set()
        await asyncio.sleep(0)


async def test_message_expiring_during_io_is_not_sent_to_model(objects, monkeypatch):
    clock = [NOW]
    store = BlobHistoryStore(objects, utc_clock=lambda: clock[0])
    await store.append_turn("a", "question", "answer", turn_id="id")
    original_read = objects.read

    async def read(name):
        document = await original_read(name)
        if name.startswith("history/"):
            clock[0] = NOW + timedelta(days=30)
        return document

    monkeypatch.setattr(objects, "read", read)
    assert await store.get("a") == []
