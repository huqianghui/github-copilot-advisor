"""Request-local stage timing; no exporters, payload capture or network I/O."""
import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from types import TracebackType
from typing import Literal, ParamSpec, TypeVar
from uuid import uuid4

from advisor_shared.events import StepTiming

logger = logging.getLogger(__name__)
P = ParamSpec("P")
R = TypeVar("R")
Attribute = str | int | float | bool | None


@dataclass
class TraceContext:
    trace_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: float = field(default_factory=lambda: time.monotonic())
    timings: list[StepTiming] = field(default_factory=list)


_trace: ContextVar[TraceContext | None] = ContextVar("advisor_trace", default=None)
_span: ContextVar[StepTiming | None] = ContextVar("advisor_span", default=None)


def current_trace() -> TraceContext | None:
    return _trace.get()


def current_span() -> StepTiming | None:
    return _span.get()


@contextmanager
def trace_scope(*, new: bool = False) -> Iterator[TraceContext]:
    existing = current_trace()
    if existing is not None and not new:
        yield existing
        return
    trace = TraceContext()
    trace_token = _trace.set(trace)
    span_token = _span.set(None)
    try:
        yield trace
    finally:
        _span.reset(span_token)
        _trace.reset(trace_token)


def failure_status(error: BaseException) -> Literal["cancelled", "timeout", "error"]:
    if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)):
        return "cancelled"
    # Keep shared independent of the HTTP/OpenAI packages.
    if isinstance(error, TimeoutError) or any(
            cls.__name__ in {"TimeoutException", "APITimeoutError"}
            for cls in type(error).__mro__):
        return "timeout"
    return "error"


class _Step(AbstractContextManager[StepTiming]):
    def __init__(self, trace: TraceContext, name: str,
                 attributes: dict[str, Attribute]):
        self.trace = trace
        self.name = name
        self.attributes = attributes

    def __enter__(self) -> StepTiming:
        self.started = time.monotonic()
        parent = current_span()
        self.record = StepTiming(
            name=self.name, span_id=uuid4().hex[:16],
            parent_span_id=parent.span_id if parent else None,
            started_at=datetime.now(timezone.utc).isoformat(),
            start_offset_ms=round((self.started - self.trace.started_at) * 1000, 3),
            attributes=self.attributes,
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("step_started", extra={"telemetry": {
                "trace_id": self.trace.trace_id, **self.record.model_dump()}})
        self.token: Token[StepTiming | None] = _span.set(self.record)
        return self.record

    def __exit__(self, exc_type: type[BaseException] | None,
                 error: BaseException | None,
                 traceback: TracebackType | None) -> None:
        record = self.record
        record.duration_ms = round((time.monotonic() - self.started) * 1000, 3)
        if error is not None and not (
                isinstance(error, asyncio.CancelledError) and record.status == "timeout"):
            record.status = failure_status(error)
            record.error_type = type(error).__name__
        self.trace.timings.append(record)
        level = (logging.ERROR if record.status == "error" else
                 logging.WARNING if record.status in {
                     "timeout", "cancelled", "degraded", "not_configured"
                 } else logging.INFO)
        try:
            logger.log(level, "step_completed", extra={"telemetry": {
                "trace_id": self.trace.trace_id, **record.model_dump()}})
        finally:
            _span.reset(self.token)


@contextmanager
def step(name: str, **attributes: Attribute) -> Iterator[StepTiming]:
    with trace_scope() as trace:
        with _Step(trace, name, attributes) as record:
            yield record


def timed(name: str) -> Callable[[Callable[P, Awaitable[R]]],
                                 Callable[P, Awaitable[R]]]:
    def decorate(operation: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(operation)
        async def measured(*args: P.args, **kwargs: P.kwargs) -> R:
            with step(name):
                return await operation(*args, **kwargs)
        return measured
    return decorate
