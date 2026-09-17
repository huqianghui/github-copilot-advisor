"""Best-effort typing, owned by the authenticated adapter turn, not the reply."""
import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable

from microsoft_agents.activity import Activity, ResourceResponse
from microsoft_agents.hosting.core import TurnContext

from advisor_shared.telemetry import trace_scope
from teams_adapter.bot import _activity_to_dict, _raw_image_count
from teams_adapter.extract import (
    ConversationIdentityError,
    build_conversation_key,
    should_respond,
    strip_mentions,
)

logger = logging.getLogger(__name__)


class _TypingSession:
    def __init__(self, context: TurnContext, interval: float, timeout: float):
        self.context = context
        self.interval = interval
        self.timeout = timeout
        self.stopped = False
        self.attempts = 0
        self.sent = 0
        self.timeouts = 0
        self.error_type: str | None = None
        self.task = asyncio.create_task(self._run(), name="advisor-typing")
        self.task.add_done_callback(self._finished)

    async def _run(self) -> None:
        while not self.stopped:
            started = time.perf_counter()
            self.attempts += 1
            activity = TurnContext.apply_conversation_reference(
                Activity(type="typing"),
                self.context.activity.get_conversation_reference())
            try:
                async with asyncio.timeout(self.timeout):
                    # Bypass send hooks and responded-state mutation for a hint.
                    await self.context.adapter.send_activities(self.context, [activity])
            except TimeoutError:
                self.timeouts += 1
                logger.warning("typing_send_timeout", extra={"telemetry": {
                    "attempt": self.attempts, "timeout_seconds": self.timeout}})
            else:
                self.sent += 1
                logger.debug("typing_send_completed", extra={"telemetry": {
                    "attempt": self.attempts, "parallel": True,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3)}})
            await asyncio.sleep(max(0, self.interval - (time.perf_counter() - started)))

    def _finished(self, task: asyncio.Task[None]) -> None:
        if self.error_type is None and not task.cancelled():
            error = task.exception()
            if error is not None:
                self.error_type = type(error).__name__
                logger.warning("typing_send_failed", extra={"telemetry": {
                    "attempt": self.attempts, "error_type": self.error_type}})

    def stop(self) -> None:
        if not self.stopped:
            self.stopped = True
            self.task.cancel()

    async def close(self) -> None:
        self.stop()
        # Join only after processing/reply; task errors are reported by _finished.
        await asyncio.gather(self.task, return_exceptions=True)
        self._finished(self.task)
        logger.info("typing_summary", extra={"telemetry": {
            "attempt_count": self.attempts, "sent_count": self.sent,
            "timeout_count": self.timeouts, "error_type": self.error_type,
            "parallel": True}})


class NonBlockingTypingMiddleware:
    def __init__(self, interval_seconds: float = 3.0,
                 send_timeout_seconds: float = 2.0):
        for name, value in (("interval_seconds", interval_seconds),
                            ("send_timeout_seconds", send_timeout_seconds)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and greater than zero")
        self.interval = interval_seconds
        self.timeout = send_timeout_seconds

    async def on_turn(self, context: TurnContext,
                      logic: Callable[[TurnContext], Awaitable[None]]) -> None:
        activity = _activity_to_dict(context.activity)
        recipient = context.activity.recipient
        bot_id = recipient.id if recipient else ""
        if not should_respond(activity, bot_id):
            await logic(context)
            return
        try:
            build_conversation_key(activity)
        except ConversationIdentityError as error:
            logger.debug("typing_skipped_invalid_identity", extra={"telemetry": {
                "field": error.field, "reason": error.reason}})
            await logic(context)
            return
        text = strip_mentions(activity.get("text") or "",
                              activity.get("entities") or [], bot_id)
        if not text and not _raw_image_count(activity):
            await logic(context)
            return

        with trace_scope():
            session = _TypingSession(context, self.interval, self.timeout)

            async def stop_before_reply(
                    _context: TurnContext, activities: list[Activity],
                    next_handler: Callable[[], Awaitable[list[ResourceResponse]]],
                    ) -> list[ResourceResponse]:
                if any(activity.type == "message" for activity in activities):
                    session.stop()
                return await next_handler()

            context.on_send_activities(stop_before_reply)
            try:
                await logic(context)
            finally:
                await session.close()
