"""会话存储:v1 内存实现,接口留给 Cosmos DB/Redis(spec 7.4)。"""
import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Callable, Protocol


class SessionStore(Protocol):
    async def get(self, key: str) -> list[dict]: ...

    async def append(self, key: str, role: str, content: str) -> None: ...


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
            async with entry.lock:
                yield
        finally:
            entry.references -= 1
            if entry.references == 0:
                del self._entries[key]


class InMemorySessionStore:
    def __init__(self, max_turns: int = 20, ttl_seconds: float = 3600,
                 clock: Callable[[], float] = time.monotonic):
        self._data: dict[str, tuple[float, list[dict]]] = {}
        self.max_turns = max_turns
        self.ttl = ttl_seconds
        self.clock = clock

    async def get(self, key: str) -> list[dict]:
        entry = self._data.get(key)
        if not entry:
            return []
        touched, messages = entry
        if self.clock() - touched > self.ttl:
            del self._data[key]
            return []
        return list(messages)

    async def append(self, key: str, role: str, content: str) -> None:
        messages = await self.get(key)
        messages.append({"role": role, "content": content})
        self._data[key] = (self.clock(), messages[-self.max_turns:])
