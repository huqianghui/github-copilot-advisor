"""Immutable turn archives and a leased, bounded context projection."""
from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Protocol

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator,
)
from typing_extensions import Self

from advisor_agent.sessions import HISTORY_RETENTION_SECONDS, _aware_utc
from advisor_shared.cloud import HistoryMessage, TurnMessage


class HistoryConflict(RuntimeError):
    pass


class HistoryCorrupt(RuntimeError):
    pass


class _Document(Protocol):
    @property
    def body(self) -> bytes: ...

    @property
    def etag(self) -> str: ...


class _Page(Protocol):
    @property
    def names(self) -> Sequence[str]: ...

    @property
    def next_cursor(self) -> str | None: ...


class _Lease(Protocol):
    @property
    def id(self) -> str: ...

    def assert_active(self) -> None: ...


class HistoryGateway(Protocol):
    async def read(self, name: str) -> _Document | None: ...

    async def create(self, name: str, body: bytes) -> bool: ...

    async def write(self, name: str, body: bytes, *, etag: str,
                    lease_id: str | None = None) -> str: ...

    async def list_page(self, prefix: str, *, cursor: str | None = None,
                        limit: int = 200) -> _Page: ...

    def lease(self, name: str) -> AbstractAsyncContextManager[_Lease]: ...


class _Stored(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    version: int = Field(default=1, strict=True, ge=1, le=1)


class _Ref(_Stored):
    turn_id: str = Field(min_length=1)
    sequence: int = Field(strict=True, gt=0)
    written_at: AwareDatetime
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    _normalize_time = field_validator("written_at")(_aware_utc)


class _Archive(_Stored):
    partition: str
    turn_id: str = Field(min_length=1)
    sequence: int = Field(strict=True, gt=0)
    written_at: AwareDatetime
    history: list[TurnMessage] = Field(repr=False, min_length=2, max_length=2)

    _normalize_time = field_validator("written_at")(_aware_utc)

    @field_validator("history")
    @classmethod
    def validate_pair(cls, value: list[TurnMessage]) -> list[TurnMessage]:
        if [message["role"] for message in value] != ["user", "assistant"]:
            raise ValueError("history must be a user/assistant pair")
        return value


class _Context(_Stored):
    partition: str
    refs: list[_Ref] = Field(default_factory=list, max_length=10)
    last_sequence: int = Field(default=0, strict=True, ge=0)
    last_commit: _Ref | None = None

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        if self.last_commit is None:
            if self.last_sequence or self.refs:
                raise ValueError("history projection has no commit")
        elif self.last_commit.sequence != self.last_sequence:
            raise ValueError("history projection sequence mismatch")
        sequences = [ref.sequence for ref in self.refs]
        if sequences != sorted(set(sequences)):
            raise ValueError("history projection must have distinct ordered turns")
        if any(sequence > self.last_sequence for sequence in sequences):
            raise ValueError("history projection contains an uncommitted sequence")
        if len({ref.turn_id for ref in self.refs}) != len(self.refs):
            raise ValueError("history projection contains duplicate turns")
        if self.refs and self.refs[-1].sequence == self.last_sequence:
            if self.refs[-1] != self.last_commit:
                raise ValueError("history projection commit mismatch")
        return self


def _digest(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("history identity must not be empty")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _encode(value: BaseModel) -> bytes:
    return value.model_dump_json().encode("utf-8")


class BlobHistoryStore:
    def __init__(self, gateway: HistoryGateway, *,
                 utc_clock: Callable[[], datetime] | None = None):
        self.gateway = gateway
        self._clock = utc_clock or (lambda: datetime.now(timezone.utc))

    def now(self) -> datetime:
        return _aware_utc(self._clock())

    def _cutoff(self) -> datetime:
        return self.now() - timedelta(seconds=HISTORY_RETENTION_SECONDS)

    @staticmethod
    def _context_name(partition: str) -> str:
        return f"context/{partition}.json"

    @staticmethod
    def _archive_name(partition: str, turn_id: str) -> str:
        return f"history/{partition}/{_digest(turn_id)}.json"

    @staticmethod
    def _receipt_name(partition: str, turn_id: str) -> str:
        return f"commits/{partition}/{_digest(turn_id)}.json"

    @staticmethod
    def _context(document: _Document, partition: str) -> _Context:
        snapshot = _Context.model_validate_json(document.body)
        if snapshot.partition != partition:
            raise HistoryCorrupt("history projection belongs to another partition")
        return snapshot

    async def _load(self, partition: str, ref: _Ref) -> _Archive:
        document = await self.gateway.read(self._archive_name(partition, ref.turn_id))
        if document is None:
            raise HistoryCorrupt("committed history archive is missing")
        if hashlib.sha256(document.body).hexdigest() != ref.digest:
            raise HistoryCorrupt("history archive checksum mismatch")
        archive = _Archive.model_validate_json(document.body)
        if (archive.partition != partition or archive.turn_id != ref.turn_id
                or archive.sequence != ref.sequence or archive.written_at != ref.written_at):
            raise HistoryCorrupt("history archive does not match its reference")
        return archive

    async def _receipt(self, partition: str, ref: _Ref) -> None:
        name = self._receipt_name(partition, ref.turn_id)
        if not await self.gateway.create(name, _encode(ref)):
            existing = await self.gateway.read(name)
            if existing is None or _Ref.model_validate_json(existing.body) != ref:
                raise HistoryCorrupt("history commit receipt mismatch")

    async def _model_history(self, snapshot: _Context) -> list[dict]:
        cutoff = self._cutoff()
        tasks = [asyncio.create_task(self._load(snapshot.partition, ref))
                 for ref in snapshot.refs if ref.written_at > cutoff]
        try:
            archives = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        cutoff = self._cutoff()
        return [dict(message) for archive in archives if archive.written_at > cutoff
                for message in archive.history]

    async def get(self, key: str) -> list[dict]:
        partition = _digest(key)
        document = await self.gateway.read(self._context_name(partition))
        if document is None:
            return []
        return await self._model_history(self._context(document, partition))

    @asynccontextmanager
    async def leased(self, key: str) -> AsyncGenerator[LeasedHistory, None]:
        partition = _digest(key)
        name = self._context_name(partition)
        await self.gateway.create(name, _encode(_Context(partition=partition)))
        async with self.gateway.lease(name) as guard:
            document = await self.gateway.read(name)
            if document is None:
                raise HistoryCorrupt("leased history projection is missing")
            snapshot = self._context(document, partition)
            handle = LeasedHistory(self, snapshot, document.etag, guard)
            # Index commit can succeed immediately before receipt persistence fails.
            if snapshot.last_commit and snapshot.last_commit.written_at > self._cutoff():
                await self._receipt(partition, snapshot.last_commit)
            yield handle

    async def append_turn(
        self, key: str, user_content: str, assistant_content: str, *,
        turn_id: str, written_at: datetime | None = None,
    ) -> None:
        async with self.leased(key) as history:
            existing = await self.gateway.read(
                self._archive_name(history.partition, turn_id))
            sequence = (_Archive.model_validate_json(existing.body).sequence
                        if existing else history.last_sequence + 1)
            await history.commit(
                turn_id, sequence,
                [{"role": "user", "content": user_content},
                 {"role": "assistant", "content": assistant_content}],
                self.now() if written_at is None else written_at)

    async def read_archive(self, key: str) -> list[HistoryMessage]:
        partition = _digest(key)
        if await self.gateway.read(self._context_name(partition)) is None:
            return []
        async with self.leased(key):
            records: list[HistoryMessage] = []
            cursor = None
            cutoff = self._cutoff()
            while True:
                page = await self.gateway.list_page(
                    f"commits/{partition}/", cursor=cursor, limit=200)
                for name in page.names:
                    document = await self.gateway.read(name)
                    if document is None:
                        raise HistoryCorrupt("listed history receipt is missing")
                    ref = _Ref.model_validate_json(document.body)
                    if ref.written_at <= cutoff:
                        continue
                    archive = await self._load(partition, ref)
                    records.extend(HistoryMessage(
                        role=message["role"], content=message["content"],
                        written_at=ref.written_at, turn_id=ref.turn_id,
                        sequence=ref.sequence) for message in archive.history)
                cursor = page.next_cursor
                if cursor is None:
                    break
            cutoff = self._cutoff()
            return sorted(
                (record for record in records if record.written_at > cutoff),
                key=lambda item: (item.sequence, item.role == "assistant"))


class LeasedHistory:
    def __init__(self, store: BlobHistoryStore, snapshot: _Context,
                 etag: str, guard: _Lease):
        self._store = store
        self._snapshot = snapshot
        self._etag = etag
        self._guard = guard

    @property
    def partition(self) -> str:
        return self._snapshot.partition

    @property
    def last_sequence(self) -> int:
        return self._snapshot.last_sequence

    def assert_active(self) -> None:
        self._guard.assert_active()

    async def get(self) -> list[dict]:
        self.assert_active()
        return await self._store._model_history(self._snapshot)

    async def commit(self, turn_id: str, sequence: int,
                     messages: list[TurnMessage], written_at: datetime) -> None:
        self.assert_active()
        candidate = _Archive(
            partition=self.partition, turn_id=turn_id, sequence=sequence,
            history=messages, written_at=written_at)
        name = self._store._archive_name(self.partition, turn_id)
        existing = await self._store.gateway.read(name)
        if existing is not None:
            archive = _Archive.model_validate_json(existing.body)
            if (archive.partition != candidate.partition or archive.turn_id != candidate.turn_id
                    or archive.sequence != candidate.sequence or archive.history != candidate.history):
                raise HistoryConflict("turn was recorded with different history")
            candidate, body = archive, existing.body
        else:
            body = _encode(candidate)
        ref = _Ref(turn_id=turn_id, sequence=sequence, written_at=candidate.written_at,
                   digest=hashlib.sha256(body).hexdigest())
        receipt = await self._store.gateway.read(
            self._store._receipt_name(self.partition, turn_id))
        if receipt is not None:
            if _Ref.model_validate_json(receipt.body) != ref:
                raise HistoryCorrupt("history receipt does not match the archive")
            if existing is None or sequence > self.last_sequence:
                raise HistoryCorrupt("committed history state is missing")
            if sequence == self.last_sequence and self._snapshot.last_commit != ref:
                raise HistoryCorrupt("history receipt conflicts with current projection")
            if any(item.turn_id == turn_id and item != ref for item in self._snapshot.refs):
                raise HistoryCorrupt("history receipt conflicts with its context reference")
            return
        if sequence <= self.last_sequence:
            raise HistoryConflict("history sequence cannot move backwards")
        self.assert_active()
        if existing is None and not await self._store.gateway.create(name, body):
            raise HistoryConflict("history archive was created by another writer")
        cutoff = self._store._cutoff()
        refs = [item for item in [*self._snapshot.refs, ref] if item.written_at > cutoff]
        updated = _Context(partition=self.partition, refs=refs[-10:],
                           last_sequence=sequence, last_commit=ref)
        self.assert_active()
        self._etag = await self._store.gateway.write(
            self._store._context_name(self.partition), _encode(updated),
            etag=self._etag, lease_id=self._guard.id)
        self._snapshot = updated
        await self._store._receipt(self.partition, ref)
