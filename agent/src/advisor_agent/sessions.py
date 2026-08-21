"""会话存储:v1 内存实现,接口留给 Cosmos DB/Redis(spec 7.4)。"""
import time
from typing import Callable, Protocol


class SessionStore(Protocol):
    async def get(self, key: str) -> list[dict]: ...

    async def append(self, key: str, role: str, content: str) -> None: ...


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
