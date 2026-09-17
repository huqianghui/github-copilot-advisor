"""Measure search attempts without changing return values or swallowing errors."""
import asyncio
import re
import time
from collections.abc import Awaitable
from types import TracebackType

import httpx
from openai import APITimeoutError

from advisor_agent.run_context import current_run
from advisor_agent.search.models import SearchResult
from advisor_agent.search.source_policy import WebSearchScope
from advisor_shared.events import SearchAttempt, SearchSource
from advisor_shared.telemetry import step


class SearchTrace:
    def __init__(self, source: SearchSource, provider: str | None,
                 timeout_seconds: float, *, scope: WebSearchScope | None = None):
        self.attempt = SearchAttempt(
            source=source, provider=provider, timeout_seconds=timeout_seconds,
            scope=scope)
        self._run = current_run.get(None)
        self._started_at = 0.0
        self._budget_expired = False

    def __enter__(self) -> SearchAttempt:
        self._started_at = time.monotonic()
        return self.attempt

    def __exit__(self, exc_type: type[BaseException] | None,
                 error: BaseException | None,
                 traceback: TracebackType | None) -> None:
        attempt = self.attempt
        attempt.duration_ms = int((time.monotonic() - self._started_at) * 1000)
        if error is not None:
            attempt.error_type = type(error).__name__
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)):
                attempt.status = "cancelled"
            elif isinstance(error, (TimeoutError, httpx.TimeoutException,
                                    APITimeoutError)):
                attempt.status = "timeout"
            else:
                attempt.status = "error"
            # Exception messages and request URLs can contain credentials.
            status = getattr(error, "status_code", None)
            if status is None:
                status = getattr(getattr(error, "response", None), "status_code", None)
            if isinstance(status, int) and 100 <= status <= 599:
                attempt.http_status = status
            code = getattr(getattr(error, "error", None), "code", None)
            if code is None:
                code = getattr(error, "code", None)
            if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code):
                attempt.error_code = code
        elif attempt.status != "not_configured":
            attempt.status = "success" if attempt.result_count else "empty"
        if self._budget_expired:
            self.mark_budget_timeout()
        if self._run is not None:
            self._run.search_attempts.append(attempt)

    async def run(self, operation: Awaitable[list[SearchResult]]) -> list[SearchResult]:
        with step(f"search.{self.attempt.source}",
                  provider=self.attempt.provider,
                  scope=self.attempt.scope,
                  timeout_seconds=self.attempt.timeout_seconds) as timing:
            self.attempt.span_id = timing.span_id
            try:
                with self as attempt:
                    results = await operation
                    attempt.result_count = len(results)
                    return results
            finally:
                timing.status = self.attempt.status
                timing.error_type = self.attempt.error_type
                timing.attributes.update(
                    result_count=self.attempt.result_count,
                    http_status=self.attempt.http_status,
                    error_code=self.attempt.error_code)

    def mark_budget_timeout(self) -> None:
        self._budget_expired = True
        self.attempt.status = "timeout"
        self.attempt.result_count = None
        self.attempt.error_type = "TimeoutError"
        self.attempt.http_status = None
        self.attempt.error_code = None
