"""Payload-free, DEBUG-only diagnostics for non-streaming OpenAI SDK calls.

HTTP hooks observe attempts/headers, not first tokens or server/model execution.
The public HTTP client's send boundary includes body consumption; the SDK-call
boundary additionally includes retries, SDK processing and response parsing.
"""
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import re
from time import perf_counter
from types import TracebackType
from typing import Any, Literal, Protocol
from uuid import uuid4

from openai import DefaultAsyncHttpxClient

from advisor_shared.telemetry import current_span, current_trace, failure_status

logger = logging.getLogger(__name__)


class _Headers(Protocol):
    def get(self, key: str, default: str | None = None) -> str | None: ...


class _Request(Protocol):
    @property
    def headers(self) -> _Headers: ...

    @property
    def extensions(self) -> MutableMapping[str, Any]: ...


class _Response(Protocol):
    @property
    def headers(self) -> _Headers: ...

    @property
    def status_code(self) -> int: ...


_PHASES = {
    "connection.connect_tcp", "connection.connect_unix_socket",
    "connection.start_tls", "proxy.start_tls", "socks.connect_tcp",
    "socks.start_tls",
    *(f"{protocol}.{phase}" for protocol in ("http11", "http2")
      for phase in ("send_request_headers", "send_request_body",
                    "receive_response_headers", "receive_response_body")),
}
_ERROR_TYPES = {
    "CancelledError", "KeyboardInterrupt", "TimeoutError", "ConnectTimeout",
    "ReadTimeout", "WriteTimeout", "PoolTimeout", "ConnectError", "ReadError",
    "WriteError", "CloseError", "RemoteProtocolError", "LocalProtocolError",
    "ProxyError", "UnsupportedProtocol", "DecodingError", "TooManyRedirects",
    "APITimeoutError", "APIConnectionError", "APIStatusError", "RateLimitError",
    "BadRequestError", "AuthenticationError", "PermissionDeniedError",
    "NotFoundError", "ConflictError", "UnprocessableEntityError",
    "InternalServerError", "APIResponseValidationError", "OpenAIError",
    "ValueError", "TypeError", "RuntimeError",
}
_REQUEST_ID_HEADERS = ("x-request-id", "apim-request-id", "x-ms-request-id",
                       "request-id")
# Accept known opaque ID shapes, not arbitrary printable text or echoed headers.
_REQUEST_ID = re.compile(
    r"(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-"
    r"[a-fA-F0-9]{12}|req_[a-zA-Z0-9]{8,96})")
_NUMERIC_HEADERS = {
    "retry-after": 86400,
    "retry-after-ms": 86400000,
    "x-ms-retry-after-ms": 86400000,
    **{f"x-ratelimit-{kind}-{unit}": 10**12
       for kind in ("limit", "remaining") for unit in ("requests", "tokens")},
    "x-ratelimit-reset-requests": 86400,
    "x-ratelimit-reset-tokens": 86400,
}
_NUMBER = re.compile(r"[0-9]{1,13}(?:\.[0-9]{1,6})?")


def _error_type(error: BaseException | None) -> str | None:
    if error is None:
        return None
    return next((cls.__name__ for cls in type(error).__mro__
                 if cls.__name__ in _ERROR_TYPES), "OtherError")


def _milliseconds(start: float, end: float) -> float:
    return round(max(0, end - start) * 1000, 3)


def _safe_headers(headers: _Headers) -> tuple[dict[str, str], dict[str, int | float]]:
    ids = {}
    for name in _REQUEST_ID_HEADERS:
        value = headers.get(name)
        if value is not None and len(value) <= 128 and _REQUEST_ID.fullmatch(value):
            ids[name] = value
    numbers = {}
    for name, maximum in _NUMERIC_HEADERS.items():
        value = headers.get(name)
        if value is not None and len(value) <= 20 and _NUMBER.fullmatch(value):
            number = float(value) if "." in value else int(value)
            if number <= maximum:
                numbers[name] = number
    return ids, numbers


@dataclass
class _Call:
    operation: str
    started: float = field(default_factory=perf_counter)
    call_id: str = field(default_factory=lambda: uuid4().hex)
    trace_id: str | None = None
    span_id: str | None = None
    attempt_count: int = 0
    last_send_finished: float | None = None
    http_status: int | None = None
    sdk_retry_count: int | None = None
    usage: dict[str, int] = field(default_factory=dict)
    finished: bool = False

    def emit(self, log_event: str, **attributes: Any) -> None:
        logger.debug(log_event, extra={"telemetry": {
            "operation": self.operation, "call_id": self.call_id,
            "trace_id": self.trace_id, "span_id": self.span_id, **attributes}})

    def record_usage(self, usage: object) -> None:
        for name, source, attribute in (
            ("input_tokens", usage, "prompt_tokens"),
            ("output_tokens", usage, "completion_tokens"),
            ("cached_input_tokens", getattr(usage, "prompt_tokens_details", None),
             "cached_tokens"),
            ("reasoning_tokens", getattr(usage, "completion_tokens_details", None),
             "reasoning_tokens"),
        ):
            count = getattr(source, attribute, None)
            if type(count) is int and 0 <= count <= 10**12:
                self.usage[name] = count


@dataclass
class _Attempt:
    call: _Call
    number: int
    sdk_retry_count: int | None
    inter_attempt_gap_ms: float | None
    started: float
    http_status: int | None = None
    headers_elapsed_ms: float | None = None
    request_ids: dict[str, str] = field(default_factory=dict)
    numeric_headers: dict[str, int | float] = field(default_factory=dict)
    phase_started: dict[str, float] = field(default_factory=dict)
    phase_durations_ms: dict[str, float] = field(default_factory=dict)
    finished: bool = False

    def emit(self, log_event: str, **attributes: Any) -> None:
        self.call.emit(log_event, attempt=self.number,
                       sdk_retry_count=self.sdk_retry_count, **attributes)

    def finish(self, ended: float, error: BaseException | None,
               boundary: str) -> None:
        self.finished = True
        self.emit(
            "llm.http.attempt_completed",
            duration_ms=_milliseconds(self.started, ended),
            end_boundary=boundary,
            status=(failure_status(error) if error else
                    "http_error" if self.http_status is not None
                    and self.http_status >= 400 else "success"),
            error_type=_error_type(error), http_status=self.http_status,
            headers_elapsed_ms=self.headers_elapsed_ms,
            request_ids=self.request_ids, numeric_headers=self.numeric_headers,
            inter_attempt_gap_ms=self.inter_attempt_gap_ms,
            phase_durations_ms=dict(self.phase_durations_ms),
        )


class _Trace:
    def __init__(self, attempt: _Attempt,
                 previous: Callable[[str, Mapping[str, Any]], Awaitable[None]] | None):
        self.attempt = attempt
        # Redirect requests may copy extensions; do not retain an earlier attempt.
        self.previous = previous.previous if isinstance(previous, _Trace) else previous

    async def __call__(self, event: str, info: Mapping[str, Any]) -> None:
        phase, _, outcome = event.rpartition(".")
        send = _active_send.get()
        if (logger.isEnabledFor(logging.DEBUG) and not self.attempt.finished
                and send is not None and send.attempt is self.attempt
                and _active_call.get() is self.attempt.call
                and phase in _PHASES and outcome in {"started", "complete", "failed"}):
            now = perf_counter()
            duration = None
            if outcome == "started":
                self.attempt.phase_started[phase] = now
            else:
                started = self.attempt.phase_started.pop(phase, None)
                if started is not None:
                    duration = _milliseconds(started, now)
                    durations = self.attempt.phase_durations_ms
                    durations[phase] = round(durations.get(phase, 0) + duration, 3)
            error = info.get("exception") if outcome == "failed" else None
            self.attempt.emit(
                "llm.http.phase", event=event,
                elapsed_ms=_milliseconds(self.attempt.started, now),
                duration_ms=duration,
                error_type=_error_type(error if isinstance(error, BaseException) else None),
            )
        if self.previous is not None:
            await self.previous(event, info)


@dataclass
class _Send(AbstractContextManager["_Send"]):
    call: _Call
    attempt: _Attempt | None = None

    def __enter__(self) -> "_Send":
        self.token = _active_send.set(self)
        return self

    def __exit__(self, _exc_type: type[BaseException] | None,
                 error: BaseException | None,
                 _traceback: TracebackType | None) -> None:
        ended = perf_counter()
        try:
            if self.attempt is not None:
                self.attempt.finish(ended, error, "http_client_send")
            self.call.last_send_finished = ended
        finally:
            _active_send.reset(self.token)


_active_call: ContextVar[_Call | None] = ContextVar("llm_diagnostic_call", default=None)
_active_send: ContextVar[_Send | None] = ContextVar("llm_diagnostic_send", default=None)


async def _request_hook(request: _Request) -> None:
    send = _active_send.get()
    if send is None or send.call.finished or not logger.isEnabledFor(logging.DEBUG):
        return
    now = perf_counter()
    gap = None
    if send.attempt is not None:
        # Redirects are HTTP attempts but are not additional SDK retries.
        send.attempt.finish(now, None, "next_request_hook")
    elif send.call.last_send_finished is not None:
        # Includes retry decisions, SDK preparation and sleep, NOT pure backoff.
        gap = _milliseconds(send.call.last_send_finished, now)
    retry_header = request.headers.get("x-stainless-retry-count")
    retry_count = (int(retry_header) if retry_header is not None
                   and re.fullmatch(r"[0-9]{1,6}", retry_header) else None)
    send.call.attempt_count += 1
    send.call.sdk_retry_count = retry_count
    send.call.http_status = None
    attempt = _Attempt(send.call, send.call.attempt_count, retry_count, gap, now)
    send.attempt = attempt
    attempt.emit("llm.http.attempt_started", inter_attempt_gap_ms=gap)
    request.extensions["trace"] = _Trace(attempt, request.extensions.get("trace"))


async def _response_hook(response: _Response) -> None:
    send = _active_send.get()
    if (send is None or send.call.finished or send.attempt is None
            or send.attempt.finished or not logger.isEnabledFor(logging.DEBUG)):
        return
    attempt = send.attempt
    attempt.http_status = send.call.http_status = response.status_code
    attempt.headers_elapsed_ms = _milliseconds(attempt.started, perf_counter())
    attempt.request_ids, attempt.numeric_headers = _safe_headers(response.headers)
    attempt.emit(
        "llm.http.response_headers", http_status=attempt.http_status,
        headers_elapsed_ms=attempt.headers_elapsed_ms,
        request_ids=attempt.request_ids, numeric_headers=attempt.numeric_headers,
    )


class DiagnosticAsyncHttpClient(DefaultAsyncHttpxClient):
    """Keep SDK transport defaults; only observe its public send/hooks API."""

    def __init__(self, **kwargs: Any):
        hooks = {name: list(values)
                 for name, values in (kwargs.pop("event_hooks", None) or {}).items()}
        hooks.setdefault("request", []).append(_request_hook)
        hooks.setdefault("response", []).append(_response_hook)
        super().__init__(event_hooks=hooks, **kwargs)

    async def send(self, *args: Any, **kwargs: Any) -> Any:
        call = _active_call.get()
        if call is None or call.finished or not logger.isEnabledFor(logging.DEBUG):
            return await super().send(*args, **kwargs)
        with _Send(call):
            return await super().send(*args, **kwargs)


class _SDKCallScope(AbstractContextManager[_Call | None]):
    def __init__(self, operation: Literal["chat.completions", "embeddings"]):
        self.operation = operation
        self.call: _Call | None = None

    def __enter__(self) -> _Call | None:
        if not logger.isEnabledFor(logging.DEBUG):
            return None
        trace, span = current_trace(), current_span()
        self.call = _Call(self.operation, trace_id=trace.trace_id if trace else None,
                          span_id=span.span_id if span else None)
        self.token = _active_call.set(self.call)
        return self.call

    def __exit__(self, _exc_type: type[BaseException] | None,
                 error: BaseException | None,
                 _traceback: TracebackType | None) -> None:
        call = self.call
        if call is None:
            return None
        call.finished = True
        try:
            call.emit(
                "llm.sdk_call_completed",
                duration_ms=_milliseconds(call.started, perf_counter()),
                status=failure_status(error) if error else "success",
                error_type=_error_type(error), attempt_count=call.attempt_count or None,
                http_status=call.http_status, sdk_retry_count=call.sdk_retry_count,
                usage=dict(call.usage),
            )
        finally:
            _active_call.reset(self.token)


def sdk_call_diagnostics(
        operation: Literal["chat.completions", "embeddings"],
) -> AbstractContextManager[_Call | None]:
    return _SDKCallScope(operation)
