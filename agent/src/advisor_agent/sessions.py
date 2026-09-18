"""Per-partition history retention, model windows and local turn coordination."""
import asyncio
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol
from uuid import uuid4

from pydantic import ConfigDict, TypeAdapter

from advisor_shared.cloud import HistoryMessage, TurnMessage
from advisor_shared.telemetry import step

HISTORY_RETENTION_SECONDS = 30 * 24 * 60 * 60


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("history timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_settings(limit: int, retention: float) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("history message limit must be a positive integer")
    if not math.isfinite(retention) or retention <= 0:
        raise ValueError("history retention must be finite and positive")


def select_history(
    records: list[HistoryMessage], now: datetime, limit: int = 20,
    *, ttl_seconds: float = HISTORY_RETENTION_SECONDS,
) -> list[dict[str, str]]:
    _validate_settings(limit, ttl_seconds)
    cutoff = _aware_utc(now) - timedelta(seconds=ttl_seconds)
    valid = [record for record in records if record.written_at > cutoff]
    return [{"role": record.role, "content": record.content}
            for record in valid[-limit:]]


class SessionStore(Protocol):
    async def get(self, key: str) -> list[dict]: ...

    async def append_turn(
        self, key: str, user_content: str, assistant_content: str, *,
        turn_id: str, written_at: datetime | None = None,
    ) -> None: ...


@dataclass
class _TurnEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    references: int = 0


class SessionTurnCoordinator:
    def __init__(self):
        self._entries: dict[str, _TurnEntry] = {}

    @asynccontextmanager
    async def turn(self, key: str) -> AsyncIterator[None]:
        entry = self._entries.get(key)
        if entry is None:
            entry = _TurnEntry()
            self._entries[key] = entry
        entry.references += 1
        try:
            with step("session.queue"):
                await entry.lock.acquire()
            try:
                yield
            finally:
                entry.lock.release()
        finally:
            entry.references -= 1
            if entry.references == 0:
                del self._entries[key]


class InMemorySessionStore:
    def __init__(
        self, max_turns: int = 20, ttl_seconds: float = HISTORY_RETENTION_SECONDS,
        clock: Callable[[], float] | None = None, *,
        utc_clock: Callable[[], datetime] | None = None,
    ):
        _validate_settings(max_turns, ttl_seconds)
        if clock is not None and utc_clock is not None:
            raise ValueError("choose clock or utc_clock, not both")
        self._data: dict[str, list[HistoryMessage]] = {}
        self.max_turns = max_turns
        self.ttl = ttl_seconds
        if clock is not None:
            origin, started = clock(), datetime.now(timezone.utc)
            self._utc_clock = lambda: started + timedelta(seconds=clock() - origin)
        else:
            self._utc_clock = utc_clock or (lambda: datetime.now(timezone.utc))

    def _retained(self, key: str) -> list[HistoryMessage]:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("session key must not be empty")
        cutoff = _aware_utc(self._utc_clock()) - timedelta(seconds=self.ttl)
        records = [record for record in self._data.get(key, [])
                   if record.written_at > cutoff]
        if records:
            self._data[key] = records
        else:
            self._data.pop(key, None)
        return records

    async def get(self, key: str) -> list[dict]:
        return select_history(
            self._retained(key), self._utc_clock(), self.max_turns,
            ttl_seconds=self.ttl)

    async def read_archive(self, key: str) -> list[HistoryMessage]:
        return [record.model_copy(deep=True) for record in self._retained(key)]

    async def append(self, key: str, role: str, content: str) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("history role must be user or assistant")
        if not isinstance(content, str):
            raise ValueError("history content must be text")
        records = self._retained(key)
        message = HistoryMessage(
            role=role, content=content, written_at=_aware_utc(self._utc_clock()),
            turn_id=uuid4().hex,
            sequence=max((record.sequence for record in records), default=0) + 1,
        )
        self._data[key] = [*records, message]

    async def append_turn(
        self, key: str, user_content: str, assistant_content: str, *,
        turn_id: str, written_at: datetime | None = None,
    ) -> None:
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("turn_id must not be empty")
        if not isinstance(user_content, str) or not isinstance(assistant_content, str):
            raise ValueError("history content must be text")
        timestamp = _aware_utc(written_at if written_at is not None else self._utc_clock())
        records = self._retained(key)
        existing = [record for record in records if record.turn_id == turn_id]
        if existing:
            if [(record.role, record.content) for record in existing] != [
                ("user", user_content), ("assistant", assistant_content),
            ]:
                raise ValueError("turn_id was already committed with different content")
            return
        sequence = max((record.sequence for record in records), default=0) + 1
        user = HistoryMessage(role="user", content=user_content, written_at=timestamp,
                              turn_id=turn_id, sequence=sequence)
        assistant = HistoryMessage(
            role="assistant", content=assistant_content, written_at=timestamp,
            turn_id=turn_id, sequence=sequence)
        self._data[key] = [*records, user, assistant]


_history_adapter = TypeAdapter(
    list[TurnMessage], config=ConfigDict(extra="forbid", hide_input_in_errors=True))


class BufferedSessionStore:
    """One turn's private snapshot and staged delta; no durable writes."""

    def __init__(self, history: list[dict], *, partition_key: str | None = None):
        self._history = _history_adapter.validate_python(history)
        self._delta: list[TurnMessage] = []
        self._partition_key: str | None = None
        self.turn_id: str | None = None
        self.written_at: datetime | None = None
        if partition_key is not None:
            self._bind(partition_key)

    def _bind(self, key: str) -> None:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("partition key must not be empty")
        if self._partition_key is None:
            self._partition_key = key
        elif self._partition_key != key:
            raise ValueError("buffer cannot cross session partitions")

    @property
    def delta(self) -> list[TurnMessage]:
        return [message.copy() for message in self._delta]

    async def get(self, key: str) -> list[dict]:
        self._bind(key)
        return [dict(message) for message in self._history]

    async def append_turn(
        self, key: str, user_content: str, assistant_content: str, *,
        turn_id: str, written_at: datetime | None = None,
    ) -> None:
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("turn_id must not be empty")
        timestamp = _aware_utc(written_at) if written_at is not None else None
        messages = _history_adapter.validate_python([
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": assistant_content},
        ])
        self._bind(key)
        if self._delta:
            if self.turn_id != turn_id:
                raise ValueError("buffer can hold only one turn")
            if self._delta != messages:
                raise ValueError("turn_id was staged with different content")
            return
        self.turn_id = turn_id
        self.written_at = timestamp
        self._delta = messages
