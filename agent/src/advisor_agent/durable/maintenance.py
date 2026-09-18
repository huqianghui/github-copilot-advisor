"""Bounded, resumable maintenance; no Agent/model initialization or history deletion."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
import hashlib
import math
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from advisor_agent.durable.history import BlobHistoryStore
from advisor_agent.durable.jobs import (
    BODY_RETENTION, JobConflict, JobQuarantined, JobRecord, JobStateError, JobStore, PruneCursor,
    QuarantineRecord, TaskRef, _Inbox,
)
from advisor_agent.durable.leases import LeaseBusy, LeaseLost, _log_failure
from advisor_agent.durable.queue import Delivery, DeliveryLost, OutboxDispatcher, SessionQueue, decode_wake


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    version: Literal[1] = 1
    phase: Literal["outbox", "prune", "poison"] = "outbox"
    outbox_cursor: str | None = Field(default=None, repr=False)
    prune_cursor: PruneCursor | None = Field(default=None, repr=False)


class MaintenanceResult(BaseModel):
    scanned: int = 0
    republished: int = 0
    expired: int = 0
    failed: int = 0
    quarantined: int = 0
    skipped: int = 0
    busy: bool = False
    timed_out: bool = False
    cursors: dict = Field(default_factory=dict, repr=False)


class Maintenance:
    def __init__(self, jobs: JobStore, outbox: OutboxDispatcher, *,
                 history: BlobHistoryStore | None = None,
                 poison: SessionQueue | None = None, max_objects: int = 200,
                 max_seconds: float = 30, max_stage_attempts: int = 3,
                 state_name: str = "maintenance/state.json",
                 monotonic_clock: Callable[[], float] = monotonic):
        if type(max_objects) is not int or not 1 <= max_objects <= 200:
            raise ValueError("max_objects must be between 1 and 200")
        if (isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
                or not math.isfinite(max_seconds)
                or not 0 < max_seconds <= 30):
            raise ValueError("max_seconds must be between zero and 30")
        if type(max_stage_attempts) is not int or max_stage_attempts < 1:
            raise ValueError("max_stage_attempts must be positive")
        if poison is not None and history is None:
            raise ValueError("poison recovery requires history for context-before-job lease order")
        if not state_name.startswith("maintenance/") or not state_name.endswith(".json"):
            raise ValueError("maintenance state must use its own metadata prefix")
        self.jobs, self.outbox, self.poison = jobs, outbox, poison
        self.history = history
        self.gateway = jobs.gateway
        self.max_objects, self.max_seconds = max_objects, max_seconds
        self.max_stage_attempts, self.state_name = max_stage_attempts, state_name
        self._monotonic = monotonic_clock

    async def _quarantine(self, body: bytes, code: str, *,
                          head: TaskRef | None = None, partition: str | None = None) -> None:
        if code == "job_missing":
            raise JobStateError("job quarantine must reserve its actual storage identity")
        digest = hashlib.sha256(body).hexdigest()
        name = (f"poison/job-{head.job_id}.json" if head and code == "job_missing"
                else f"poison/wake-{digest}.json")
        metadata = QuarantineRecord(
            code=code, body_digest=digest, quarantined_at=self.jobs.now(),
            job_id=head.job_id if head else None, sequence=head.sequence if head else None)
        await self.gateway.create(name, metadata.model_dump_json().encode("utf-8"))
        if head is not None and partition is not None:
            await self.jobs.inbox.advance(partition, head.job_id)

    async def _resolve_head(self, partition: str, head: TaskRef,
                            result: MaintenanceResult) -> JobRecord | None:
        try:
            record = await self.jobs.read(head.job_id)
        except (JobQuarantined, ValidationError) as error:
            _log_failure("maintenance.job_invalid", error)
        else:
            if record is not None:
                return record
            _log_failure("maintenance.job_missing", JobStateError("job missing"))
        if await self.jobs.quarantine_job(partition, head):
            result.quarantined += 1
            result.failed += 1
            return None
        # Submission/repair won the conditional reservation or the lease recheck.
        # Process its current state, not the earlier missing/invalid observation.
        record = await self.jobs.read(head.job_id)
        if record is None:
            raise JobConflict("repaired job disappeared")
        return record

    async def _outbox_step(self, state: _State, result: MaintenanceResult) -> None:
        page = await self.gateway.list_page("inbox/", cursor=state.outbox_cursor, limit=1)
        for name in page.names:
            result.scanned += 1
            try:
                document = await self.gateway.read(name)
                if document is None:
                    continue
                record = _Inbox.model_validate_json(document.body)
                if self.jobs.inbox.name(record.partition_key) != name:
                    raise JobConflict("inbox path mismatch")
                if record.pending:
                    head = record.pending[0]
                    job_record = await self._resolve_head(record.partition_key, head, result)
                    if job_record is not None and job_record.terminal:
                        async with self.jobs.leased_job(head.job_id, include_payload=False) as job:
                            if not job.record.terminal:
                                raise JobConflict("terminal ledger mismatch")
                            await job.cleanup_terminal()
                            await self.jobs.inbox.advance(record.partition_key, head.job_id)
                snapshot = await self.jobs.inbox.snapshot_outbox(record.partition_key)
                if snapshot.needs_notification:
                    await self.outbox.notify(record.partition_key, "maintenance")
                    result.republished += 1
            except (LeaseLost, asyncio.CancelledError):
                raise
            except LeaseBusy:
                result.skipped += 1
            except Exception as error:
                _log_failure("maintenance.outbox_failed", error)
                result.failed += 1
        state.outbox_cursor = page.next_cursor
        if page.next_cursor is None:
            state.phase = "prune"

    async def _prune_step(self, state: _State, result: MaintenanceResult) -> None:
        batch = await self.jobs.prune(limit=1, cursor=state.prune_cursor, continue_on_error=True)
        result.scanned += batch.scanned
        result.expired += batch.expired
        result.failed += batch.failed
        result.skipped += batch.skipped
        state.prune_cursor = batch.cursor
        if batch.cursor is None:
            state.phase = "poison"

    async def _handle_poison(self, delivery: Delivery, result: MaintenanceResult) -> None:
        try:
            wake = decode_wake(delivery.body)
        except (ValueError, ValidationError) as error:
            _log_failure("maintenance.poison_invalid", error)
            await self._quarantine(delivery.body, "invalid_wake")
            await delivery.ack()
            result.quarantined += 1
            result.failed += 1
            return
        head = await self.jobs.inbox.head(wake.partition_key)
        if head is None:
            await delivery.ack()
            return
        record = await self._resolve_head(wake.partition_key, head, result)
        if record is None:
            if await self.jobs.inbox.head(wake.partition_key) is not None:
                await self.outbox.notify(wake.partition_key, wake.trace_id)
                result.republished += 1
            await delivery.ack()
            return
        if record.partition_key != wake.partition_key:
            _log_failure("maintenance.poison_job_invalid", JobStateError("job inconsistent"))
            await self._quarantine(delivery.body, "invalid_job",
                                   head=head, partition=wake.partition_key)
            await delivery.ack()
            result.quarantined += 1
            result.failed += 1
            return
        async with AsyncExitStack() as stack:
            assert self.history is not None
            context = await stack.enter_async_context(self.history.leased(wake.partition_key))
            job = await stack.enter_async_context(
                self.jobs.leased_job(head.job_id, include_payload=False))
            context.assert_active()
            republish = False
            if self.jobs.now() >= job.record.accepted_at + BODY_RETENTION and not job.record.terminal:
                await job.expire()
                result.expired += 1
            if not job.record.terminal:
                if job.state == "sending":
                    await job.mark_delivery_unknown("delivery_unknown")
                    result.failed += 1
                elif job.record.recovery_pending:
                    republish = True
                elif (job.record.recovery_trace_id is not None
                      and wake.trace_id != job.record.recovery_trace_id):
                    # Only failure of the last confirmed recovery wake starts a new
                    # recovery epoch; stale duplicate poison cannot burn job budget.
                    result.skipped += 1
                elif (job.record.recovery_attempts >= self.max_stage_attempts
                      or job.result is None and job.record.generation_attempts >= self.max_stage_attempts
                      or job.result is not None and job.record.delivery_attempts >= self.max_stage_attempts
                      or not job.record.history_committed
                      and job.record.history_attempts >= self.max_stage_attempts):
                    await job.fail("retry_exhausted")
                    result.failed += 1
                else:
                    await job.note_recovery()
                    republish = True
            if job.record.terminal:
                await job.cleanup_terminal()
                await self.jobs.inbox.advance(wake.partition_key, job.job_id)
            elif republish:
                if job.record.recovery_trace_id is None:
                    raise JobStateError("recovery notification has no reservation")
                context.assert_active()
                job.assert_active()
                await self.outbox.notify(wake.partition_key, job.record.recovery_trace_id)
                await job.confirm_recovery()
                result.republished += 1
        await delivery.ack()

    async def _poison_step(self, state: _State, result: MaintenanceResult) -> bool:
        if self.poison is None:
            state.phase = "outbox"
            return True
        deliveries = [delivery async for delivery in self.poison.receive()]
        if not deliveries:
            state.phase = "outbox"
            return True
        delivery = deliveries[0]
        result.scanned += 1
        async with delivery:
            try:
                await self._handle_poison(delivery, result)
            except LeaseBusy:
                result.skipped += 1
                await delivery.release(60)
            except (LeaseLost, DeliveryLost, asyncio.CancelledError):
                raise
            except Exception as error:
                _log_failure("maintenance.poison_failed", error)
                result.failed += 1
                await delivery.release(60)
        return False

    async def run_once(self) -> MaintenanceResult:
        """At most 200 candidates and 30s work, including storage setup.

        Progress is saved after each completed candidate. Replaying an interrupted
        candidate is safe. Shielded lease cleanup can finish after the work deadline.
        Scanned counts candidate objects, not coordination reads/writes.
        """
        result = MaintenanceResult()
        started = self._monotonic()
        window = asyncio.timeout(self.max_seconds)
        try:
            async with window:
                return await self._run_locked(result, started)
        except LeaseBusy:
            result.busy = True
            return result
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            if window.expired():
                result.timed_out = True
                return result
            _log_failure("maintenance.failed", error)
            raise
        except Exception as error:
            _log_failure("maintenance.failed", error)
            raise

    async def _run_locked(self, result: MaintenanceResult, started: float) -> MaintenanceResult:
        if await self.gateway.read(self.state_name) is None:
            await self.gateway.create(self.state_name, _State().model_dump_json().encode("utf-8"))
        async with self.gateway.lease(self.state_name) as guard:
            document = await self.gateway.read(self.state_name)
            if document is None:
                raise JobConflict("leased maintenance state disappeared")
            state, etag = _State.model_validate_json(document.body), document.etag
            result.cursors = state.model_dump(mode="json")
            while result.scanned < self.max_objects:
                remaining = self.max_seconds - (self._monotonic() - started)
                if remaining <= 0:
                    result.timed_out = True
                    break
                guard.assert_active()
                completed = False
                if state.phase == "outbox":
                    await self._outbox_step(state, result)
                elif state.phase == "prune":
                    await self._prune_step(state, result)
                else:
                    completed = await self._poison_step(state, result)
                guard.assert_active()
                etag = await self.gateway.write(
                    self.state_name, state.model_dump_json().encode("utf-8"),
                    etag=etag, lease_id=guard.id)
                result.cursors = state.model_dump(mode="json")
                if completed:
                    break
            return result
