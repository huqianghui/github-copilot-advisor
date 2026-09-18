"""Ordered, fenced execution using persisted preparation and delivery checkpoints."""
import asyncio
from collections.abc import Awaitable
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import inspect
import math
from typing import Literal, Protocol

from pydantic import ValidationError

from advisor_agent.durable.history import BlobHistoryStore, LeasedHistory
from advisor_agent.durable.jobs import (
    BODY_RETENTION, JobConflict, JobExpired, JobStateError, JobStore, LeasedJob,
)
from advisor_agent.durable.leases import LeaseBusy, _log_failure
from advisor_agent.durable.queue import OutboxDispatcher
from advisor_shared.cloud import PreparedTurn, TaskPayload
from advisor_shared.delivery import DeliveryFailure
from advisor_shared.telemetry import step, trace_scope


MAX_TURNS_PER_INVOCATION = 4
MAX_STAGE_ATTEMPTS = 3


class TurnExecutor(Protocol):
    def validate(self, payload: TaskPayload) -> None | Awaitable[None]: ...

    async def prepare(self, payload: TaskPayload,
                      history: list[dict[str, str]]) -> PreparedTurn: ...

    async def send(self, payload: TaskPayload, prepared: PreparedTurn) -> str | None: ...


class SessionWorker:
    def __init__(self, jobs: JobStore, history: BlobHistoryStore, outbox: OutboxDispatcher,
                 executor: TurnExecutor, *, max_turns_per_invocation: int = MAX_TURNS_PER_INVOCATION,
                 max_stage_attempts: int = MAX_STAGE_ATTEMPTS):
        if (type(max_turns_per_invocation) is not int
                or not 1 <= max_turns_per_invocation <= MAX_TURNS_PER_INVOCATION):
            raise ValueError("max_turns_per_invocation must be between 1 and 4")
        if type(max_stage_attempts) is not int or max_stage_attempts < 1:
            raise ValueError("max_stage_attempts must be positive")
        self.jobs, self.history, self.outbox, self.executor = jobs, history, outbox, executor
        self.max_turns_per_invocation = max_turns_per_invocation
        self.max_stage_attempts = max_stage_attempts

    async def _validate(self, payload: TaskPayload) -> None:
        result = self.executor.validate(payload)
        if inspect.isawaitable(result):
            result = await result
        if result is not None:
            raise ValueError("executor validation must return None or raise")

    def _retry_time(self, failure: DeliveryFailure) -> datetime:
        try:
            return self.jobs.now() + timedelta(seconds=failure.retry_after_seconds or 0)
        except OverflowError:
            return datetime.max.replace(tzinfo=timezone.utc)

    async def _process_job(self, job: LeasedJob, history: LeasedHistory) -> None:
        if job.record.terminal:
            return
        if self.jobs.now() >= job.record.accepted_at + BODY_RETENTION:
            await job.expire()
            return
        if job.state == "sending":
            await job.mark_delivery_unknown("delivery_unknown")
            return
        if job.record.retry_at and self.jobs.now() < job.record.retry_at:
            return
        try:
            payload = await self.jobs.load_payload(job.job_id)
        except JobExpired:
            await job.expire()
            return
        except (JobStateError, ValidationError) as error:
            _log_failure("worker.payload_inconsistent", error)
            await job.fail("invalid_payload")
            return
        if payload.partition_key != job.partition_key:
            raise JobConflict("job payload partition mismatch")
        job.payload = payload
        with ExitStack() as stack:
            try:
                stack.enter_context(trace_scope(new=True, trace_id=payload.trace_id))
            except ValueError as error:
                _log_failure("worker.trace_invalid", error)
                await job.fail("invalid_payload")
                return
            with step("worker.job") as timing:
                try:
                    await self._process_payload(job, history, payload)
                    timing.attributes["job_state"] = job.state
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    _log_failure("worker.job_failed", error)
                    raise

    async def _process_payload(self, job: LeasedJob, history: LeasedHistory,
                               payload: TaskPayload) -> None:
        try:
            await self._validate(payload)
        except (DeliveryFailure, ValueError) as error:
            _log_failure("worker.validation_failed", error)
            await job.fail("invalid_payload")
            return
        if job.result is None:
            if job.record.generation_attempts >= self.max_stage_attempts:
                await job.fail("retry_exhausted")
                return
            if job.state == "running":
                await job.retry("generation_failed")
            messages = await history.get()
            history.assert_active()
            job.assert_active()
            await job.mark_running()
            try:
                prepared = await self.executor.prepare(payload, messages)
            except DeliveryFailure as error:
                _log_failure("worker.preparation_failed", error)
                if (error.definitely_not_sent is True and error.retryable is True
                        and job.record.generation_attempts < self.max_stage_attempts):
                    await job.retry("generation_failed", retry_at=self._retry_time(error))
                else:
                    await job.fail("generation_failed")
                return
            try:
                prepared = PreparedTurn.model_validate(prepared)
            except ValidationError as error:
                _log_failure("worker.preparation_invalid", error)
                await job.fail("generation_failed")
                return
            history.assert_active()
            job.assert_active()
            await job.save_result(prepared)
        prepared, written_at = job.result, job.result_written_at
        if prepared is None or written_at is None or job.sequence is None:
            raise JobStateError("saved result checkpoint is incomplete")
        if prepared.history and not job.record.history_committed:
            if job.record.history_attempts >= self.max_stage_attempts:
                await job.fail("history_failed")
                return
            await job.mark_committing()
            await history.commit(job.job_id, job.sequence, prepared.history, written_at)
            await job.mark_history_committed()
        if prepared.reply is None:
            await job.complete(None)
            return
        if job.record.delivery_attempts >= self.max_stage_attempts:
            await job.fail("retry_exhausted")
            return
        history.assert_active()
        job.assert_active()
        await job.mark_sending()
        history.assert_active()
        job.assert_active()
        try:
            receipt = await self.executor.send(payload, prepared)
        except DeliveryFailure as error:
            _log_failure("worker.delivery_failed", error)
            if error.definitely_not_sent is not True:
                await job.mark_delivery_unknown("delivery_unknown")
            elif error.retryable and job.record.delivery_attempts < self.max_stage_attempts:
                await job.retry_delivery(error)
            else:
                await job.fail_delivery(error)
            return
        history.assert_active()
        job.assert_active()
        await job.complete(receipt)

    async def process_session(self, partition_key: str, trace_id: str
                              ) -> Literal["drained", "rescheduled", "busy"]:
        """The transport must retain/release busy deliveries, never silently ack them."""
        delay = 0
        try:
            # Never invert this order: context -> job -> brief inbox mutations.
            async with self.history.leased(partition_key) as history:
                for _ in range(self.max_turns_per_invocation):
                    history.assert_active()
                    head = await self.jobs.inbox.head(partition_key)
                    if head is None:
                        return "drained"
                    async with self.jobs.leased_job(head.job_id, include_payload=False) as job:
                        if job.partition_key != partition_key or job.sequence != head.sequence:
                            raise JobConflict("inbox head does not match its leased job")
                        await self._process_job(job, history)
                        if job.record.terminal:
                            await job.cleanup_terminal()
                            history.assert_active()
                            job.assert_active()
                            await self.jobs.inbox.advance(partition_key, job.job_id)
                            continue
                        remaining = ((job.record.retry_at - self.jobs.now()).total_seconds()
                                     if job.record.retry_at else 0)
                        # The checkpoint retains the FULL provider delay. Wakes poll at
                        # most once a minute; a wake never authorizes an early send.
                        delay = min(60, max(1, math.ceil(remaining)))
                        break
                if await self.jobs.inbox.head(partition_key) is None:
                    return "drained"
        except LeaseBusy:
            return "busy"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_failure("worker.processing_failed", error)
            raise
        await self.outbox.notify(partition_key, trace_id, delay_seconds=delay)
        return "rescheduled"
