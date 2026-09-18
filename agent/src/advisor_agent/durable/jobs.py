"""A leased job ledger and a separate, briefly leased ordered inbox."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from advisor_agent.durable.blob import BlobGateway
from advisor_agent.durable.leases import LeaseBusy, LeaseGuard, LeaseLost, _log_failure
from advisor_shared.cloud import PreparedTurn, TaskPayload
from advisor_shared.delivery import DeliveryFailure


JobState = Literal[
    "pending_notify", "queued", "running", "result_ready", "sending",
    "retry_wait", "completed", "failed", "expired", "delivery_unknown"]
FailureCode = Literal[
    "generation_failed", "delivery_failed", "delivery_unknown", "history_failed",
    "invalid_payload", "retry_exhausted", "expired", "cancelled"]
TERMINAL_STATES = frozenset({"completed", "failed", "expired", "delivery_unknown"})
BODY_RETENTION = timedelta(hours=24)
METADATA_RETENTION = timedelta(days=7)
JobId = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class JobConflict(RuntimeError):
    pass


class JobStateError(RuntimeError):
    pass


class JobExpired(JobStateError):
    pass


class JobQuarantined(JobStateError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _identity(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError("job identity is incomplete")
    return value


def make_job_id(bot_id: str, partition_key: str, activity_id: str) -> str:
    values = (_identity(bot_id), _identity(partition_key), _identity(activity_id))
    return hashlib.sha256("\0".join(("job-v1", *values)).encode("utf-8")).hexdigest()


def _partition_digest(partition: str) -> str:
    return hashlib.sha256(_identity(partition).encode("utf-8")).hexdigest()


class _Stored(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class TaskRef(_Stored):
    job_id: JobId
    sequence: int = Field(ge=1, strict=True)


class QuarantineRecord(_Stored):
    version: Literal[1] = 1
    code: Literal["invalid_wake", "job_missing", "invalid_job"]
    body_digest: JobId
    quarantined_at: AwareDatetime
    job_id: JobId | None = None
    sequence: int | None = Field(default=None, ge=1)


class Receipt(TaskRef):
    state: JobState
    duplicate: bool


class JobRecord(_Stored):
    version: Literal[1] = 1
    job_id: JobId
    partition_key: str = Field(min_length=1, repr=False)
    sequence: int | None = Field(default=None, ge=1, strict=True)
    state: JobState = "pending_notify"
    accepted_at: AwareDatetime
    result: PreparedTurn | None = Field(default=None, repr=False)
    result_written_at: AwareDatetime | None = None
    generation_attempts: int = Field(default=0, ge=0)
    delivery_attempts: int = Field(default=0, ge=0)
    history_attempts: int = Field(default=0, ge=0)
    history_committed: bool = False
    recovery_attempts: int = Field(default=0, ge=0)
    recovery_trace_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    recovery_pending: bool = False
    retry_at: AwareDatetime | None = None
    failure_code: FailureCode | None = None
    terminal_at: AwareDatetime | None = None
    delivery_receipt: str | None = Field(default=None, repr=False, pattern=r"^[A-Za-z0-9_.:-]{1,256}$")

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class _QuarantineFence(_Stored):
    """Body-free reservation of the actual job identity, not a separate lock blob."""

    version: Literal[1] = 1
    kind: Literal["quarantined_job"] = "quarantined_job"
    partition_key: str = Field(min_length=1, repr=False)
    record: QuarantineRecord

    @model_validator(mode="after")
    def validate_record(self) -> _QuarantineFence:
        if (self.record.job_id is None or self.record.sequence is None
                or self.record.code not in ("job_missing", "invalid_job")):
            raise ValueError("invalid job quarantine reservation")
        return self


_JOB_SLOT = TypeAdapter(JobRecord | _QuarantineFence, config=ConfigDict(hide_input_in_errors=True))


class OutboxSnapshot(_Stored):
    partition_key: str = Field(repr=False)
    generation: int
    notified_generation: int
    pending_count: int

    @property
    def needs_notification(self) -> bool:
        return self.pending_count > 0 and self.generation > self.notified_generation


class OutboxPage(_Stored):
    snapshots: list[OutboxSnapshot]
    next_cursor: str | None = Field(repr=False)
    scanned: int


class PruneCursor(_Stored):
    prefix: Literal["jobs/", "payload/", "terminal-metadata/", "poison/"] = "jobs/"
    marker: str | None = Field(default=None, repr=False)


class PruneResult(_Stored):
    removed: int = 0
    scanned: int = 0
    expired: int = 0
    failed: int = 0
    skipped: int = 0
    cursor: PruneCursor | None = None

    @property
    def cursors(self) -> dict[str, str | None]:
        values = dict.fromkeys(("jobs/", "payload/", "terminal-metadata/", "poison/", "_prefix"))
        if self.cursor:
            values["_prefix"] = self.cursor.prefix
            values[self.cursor.prefix] = self.cursor.marker
        return values


class _Inbox(_Stored):
    version: Literal[1] = 1
    partition_key: str = Field(repr=False)
    next_sequence: int = Field(default=1, ge=1)
    pending: list[TaskRef] = Field(default_factory=list)
    generation: int = Field(default=0, ge=0)
    notified_generation: int = Field(default=0, ge=0)


def _encode(model: BaseModel) -> bytes:
    return model.model_dump_json().encode("utf-8")


class _InboxLease:
    def __init__(self, gateway: BlobGateway, name: str, record: _Inbox,
                 etag: str, guard: LeaseGuard):
        self.gateway, self.name, self.record = gateway, name, record
        self.etag, self.guard = etag, guard

    async def save(self) -> None:
        self.guard.assert_active()
        self.etag = await self.gateway.write(
            self.name, _encode(self.record), etag=self.etag, lease_id=self.guard.id)


class InboxStore:
    def __init__(self, gateway: BlobGateway, *, lease_wait_seconds: float = 5):
        self.gateway = gateway
        self.lease_wait_seconds = lease_wait_seconds

    @staticmethod
    def name(partition: str) -> str:
        return f"inbox/{_partition_digest(partition)}.json"

    async def _read(self, partition: str) -> _Inbox:
        document = await self.gateway.read(self.name(partition))
        record = (_Inbox.model_validate_json(document.body) if document
                  else _Inbox(partition_key=partition))
        if record.partition_key != partition:
            raise JobConflict("inbox partition mismatch")
        return record

    @asynccontextmanager
    async def _edit(self, partition: str) -> AsyncIterator[_InboxLease]:
        name = self.name(partition)
        if await self.gateway.read(name) is None:
            await self.gateway.create(name, _encode(_Inbox(partition_key=partition)))
        async with AsyncExitStack() as stack:
            async with asyncio.timeout(self.lease_wait_seconds):
                while True:
                    try:
                        guard = await stack.enter_async_context(self.gateway.lease(name))
                        break
                    except LeaseBusy:
                        await asyncio.sleep(0.01)
            document = await self.gateway.read(name)
            if document is None:
                raise JobConflict("leased inbox is missing")
            record = _Inbox.model_validate_json(document.body)
            if record.partition_key != partition:
                raise JobConflict("inbox partition mismatch")
            yield _InboxLease(self.gateway, name, record, document.etag, guard)

    async def head(self, partition: str) -> TaskRef | None:
        pending = (await self._read(partition)).pending
        return pending[0] if pending else None

    async def pending(self, partition: str) -> list[TaskRef]:
        return (await self._read(partition)).pending

    async def advance(self, partition: str, job_id: str) -> None:
        async with self._edit(partition) as edit:
            pending = edit.record.pending
            if not any(ref.job_id == job_id for ref in pending):
                return
            if pending[0].job_id != job_id:
                raise JobConflict("only the inbox head may advance")
            pending.pop(0)
            if pending:
                edit.record.generation += 1
            await edit.save()

    async def snapshot_outbox(self, partition: str) -> OutboxSnapshot:
        record = await self._read(partition)
        return OutboxSnapshot(
            partition_key=partition, generation=record.generation,
            notified_generation=record.notified_generation, pending_count=len(record.pending))

    async def _retire_terminal(self, partition: str, job_id: str) -> None:
        if await self.gateway.read(self.name(partition)) is None:
            return
        async with self._edit(partition) as edit:
            remaining = [ref for ref in edit.record.pending if ref.job_id != job_id]
            if len(remaining) == len(edit.record.pending):
                return
            edit.record.pending = remaining
            if remaining:
                edit.record.generation += 1
            await edit.save()

    async def confirm_notified(self, partition: str, generation: int) -> None:
        async with self._edit(partition) as edit:
            if type(generation) is not int or not 0 <= generation <= edit.record.generation:
                raise ValueError("invalid outbox generation")
            edit.record.notified_generation = max(edit.record.notified_generation, generation)
            await edit.save()

    async def outbox_page(self, *, cursor: str | None = None, limit: int = 200) -> OutboxPage:
        page = await self.gateway.list_page("inbox/", cursor=cursor, limit=limit)
        snapshots = []
        for name in page.names:
            document = await self.gateway.read(name)
            if document is None:
                continue
            record = _Inbox.model_validate_json(document.body)
            if self.name(record.partition_key) != name:
                raise JobConflict("inbox path mismatch")
            snapshots.append(OutboxSnapshot(
                partition_key=record.partition_key, generation=record.generation,
                notified_generation=record.notified_generation, pending_count=len(record.pending)))
        return OutboxPage(snapshots=snapshots, next_cursor=page.next_cursor, scanned=len(page.names))

    async def outboxes(self) -> AsyncIterator[OutboxSnapshot]:
        cursor = None
        while True:
            page = await self.outbox_page(cursor=cursor)
            for snapshot in page.snapshots:
                yield snapshot
            cursor = page.next_cursor
            if cursor is None:
                break


class JobStore:
    def __init__(self, gateway: BlobGateway, *, bot_id: str,
                 utc_clock: Callable[[], datetime] | None = None,
                 inbox: InboxStore | None = None):
        self.gateway = gateway
        self.bot_id = _identity(bot_id)
        self.inbox = inbox or InboxStore(gateway)
        self._clock = utc_clock or (lambda: datetime.now(timezone.utc))

    def now(self) -> datetime:
        return _utc(self._clock())

    @staticmethod
    def _name(job_id: str) -> str:
        TaskRef(job_id=job_id, sequence=1)
        return f"jobs/{job_id}.json"

    async def read(self, job_id: str) -> JobRecord | None:
        name = self._name(job_id)
        document = await self.gateway.read(f"terminal-metadata/{job_id}.json")
        if document is None:
            document = await self.gateway.read(name)
        if document is None:
            quarantine = await self.gateway.read(f"poison/job-{job_id}.json")
            if quarantine is not None:
                value = QuarantineRecord.model_validate_json(quarantine.body)
                if value.job_id != job_id or value.code not in ("job_missing", "invalid_job"):
                    raise JobConflict("quarantine marker does not match its job")
                if self.now() < value.quarantined_at + METADATA_RETENTION:
                    raise JobQuarantined("job is quarantined due to missing durable state")
            return None
        record = _JOB_SLOT.validate_json(document.body)
        if isinstance(record, _QuarantineFence):
            if record.record.job_id != job_id:
                raise JobConflict("quarantine reservation does not match its job")
            raise JobQuarantined("job identity is reserved for quarantine")
        if record.job_id != job_id:
            raise JobConflict("job identity mismatch")
        return record

    async def quarantine_job(self, partition: str, head: TaskRef) -> bool:
        """Retire only a missing/invalid job that we own; False means a repair won."""
        name = self._name(head.job_id)
        original = await self.gateway.read(name)
        marker = await self.gateway.read(f"poison/job-{head.job_id}.json")
        previous = QuarantineRecord.model_validate_json(marker.body) if marker else None
        if (previous is not None and (previous.job_id != head.job_id
                or previous.sequence != head.sequence
                or previous.code not in ("job_missing", "invalid_job"))):
            if self.now() < previous.quarantined_at + METADATA_RETENTION:
                raise JobConflict("quarantine marker identity mismatch")
            previous = None
        candidate = _QuarantineFence(
            partition_key=partition,
            record=previous or QuarantineRecord(
                code="job_missing" if original is None else "invalid_job",
                body_digest=hashlib.sha256(
                    original.body if original else head.job_id.encode("utf-8")).hexdigest(),
                quarantined_at=self.now(), job_id=head.job_id, sequence=head.sequence))
        if original is None:
            await self.gateway.create(name, _encode(candidate))
        async with self.gateway.lease(name) as guard:
            current = await self.gateway.read(name)
            if current is None:
                raise JobConflict("leased job disappeared during quarantine")
            etag = current.etag
            try:
                slot = _JOB_SLOT.validate_json(current.body)
            except ValidationError:
                slot = None

            def terminal_matches(record: JobRecord) -> bool:
                return (record.terminal and record.terminal_at is not None
                        and record.job_id == head.job_id and record.partition_key == partition
                        and record.sequence == head.sequence)

            terminal_name = f"terminal-metadata/{head.job_id}.json"
            terminal = await self.gateway.read(terminal_name)
            if terminal is not None:
                try:
                    record = JobRecord.model_validate_json(terminal.body)
                except ValidationError:
                    record = None
                if record is not None and terminal_matches(record) and record.result is None:
                    guard.assert_active()
                    await self.gateway.write(name, _encode(record), etag=etag, lease_id=guard.id)
                    return False
                if isinstance(slot, JobRecord) and terminal_matches(slot):
                    # A confirmed terminal live row can reconstruct its body-free
                    # metadata. Fence the live row and CAS the malformed metadata.
                    repaired = slot.model_copy(update={"result": None})
                    guard.assert_active()
                    await self.gateway.write(name, _encode(repaired), etag=etag, lease_id=guard.id)
                    await self.gateway.write(terminal_name, _encode(repaired), etag=terminal.etag)
                    return False
                if isinstance(slot, JobRecord) and (
                        slot.job_id != head.job_id or slot.partition_key != partition):
                    raise JobConflict("job identity mismatch during terminal reconciliation")
                if not isinstance(slot, _QuarantineFence):
                    slot = _QuarantineFence(
                        partition_key=partition,
                        record=previous or QuarantineRecord(
                            code="invalid_job", body_digest=hashlib.sha256(terminal.body).hexdigest(),
                            quarantined_at=candidate.record.quarantined_at,
                            job_id=head.job_id, sequence=head.sequence))
                if (slot.partition_key != partition or slot.record.job_id != head.job_id
                        or slot.record.sequence != head.sequence):
                    raise JobConflict("quarantine reservation identity mismatch")
                guard.assert_active()
                etag = await self.gateway.write(name, _encode(slot), etag=etag, lease_id=guard.id)
                # Neither copy proves a terminal outcome. The reserved identity
                # blocks replay while the malformed metadata is conditionally removed.
                await self.gateway.delete(terminal_name, etag=terminal.etag)
            if slot is None:
                slot = _QuarantineFence(
                    partition_key=partition,
                    record=previous or QuarantineRecord(
                        code="invalid_job", body_digest=hashlib.sha256(current.body).hexdigest(),
                        quarantined_at=candidate.record.quarantined_at,
                        job_id=head.job_id, sequence=head.sequence))
            if isinstance(slot, JobRecord):
                if slot.job_id != head.job_id or slot.partition_key != partition:
                    raise JobConflict("job identity mismatch during quarantine")
                # A conditional resubmission/repair beat the reservation. Make its
                # existing acceptance visible again; never retire that reference.
                if not slot.terminal:
                    async with self.inbox._edit(partition) as edit:
                        if any(ref.job_id == head.job_id for ref in edit.record.pending):
                            edit.record.generation += 1
                            await edit.save()
                return False
            if (slot.partition_key != partition or slot.record.job_id != head.job_id
                    or slot.record.sequence != head.sequence):
                raise JobConflict("quarantine reservation identity mismatch")
            await self._finish_quarantine(slot, etag, guard)
            return True

    async def _finish_quarantine(self, fence: _QuarantineFence, etag: str,
                                 guard: LeaseGuard) -> str:
        job_id = fence.record.job_id
        assert job_id is not None
        name = self._name(job_id)
        guard.assert_active()
        etag = await self.gateway.write(name, _encode(fence), etag=etag, lease_id=guard.id)
        marker_name = f"poison/job-{job_id}.json"
        if not await self.gateway.create(marker_name, _encode(fence.record)):
            marker = await self.gateway.read(marker_name)
            if marker is None or QuarantineRecord.model_validate_json(marker.body) != fence.record:
                raise JobConflict("quarantine marker changed")
        if await self.gateway.read(self.inbox.name(fence.partition_key)) is not None:
            async with self.inbox._edit(fence.partition_key) as edit:
                # This write rechecks SERVER ownership after publishing the audit
                # marker, immediately before the acceptance reference is retired.
                guard.assert_active()
                etag = await self.gateway.write(name, _encode(fence), etag=etag, lease_id=guard.id)
                refs = edit.record.pending
                if any(ref.job_id == job_id for ref in refs):
                    if (refs[0].job_id != job_id
                            or refs[0].sequence != fence.record.sequence):
                        raise JobConflict("quarantine no longer matches the inbox head")
                    refs.pop(0)
                    if refs:
                        edit.record.generation += 1
                    await edit.save()
        return etag

    async def load_payload(self, job_id: str) -> TaskPayload:
        self._name(job_id)
        document = await self.gateway.read(f"payload/{job_id}.json")
        if document is None:
            raise JobStateError("job payload is missing")
        payload = TaskPayload.model_validate_json(document.body)
        if payload.job_id != job_id:
            raise JobConflict("payload identity mismatch")
        if self.now() >= payload.accepted_at + BODY_RETENTION:
            raise JobExpired("job payload has expired")
        return payload

    async def submit(self, payload: TaskPayload) -> Receipt:
        expected = make_job_id(self.bot_id, payload.partition_key, payload.activity.get("id"))
        if payload.job_id != expected:
            raise JobConflict("job identity mismatch")
        existing = await self.read(payload.job_id)
        if existing and existing.terminal:
            await self._clean_terminal(existing)
            if existing.sequence is None:
                raise JobExpired("unaccepted submission has expired")
            return Receipt(job_id=existing.job_id, sequence=existing.sequence,
                           state=existing.state, duplicate=True)
        if self.now() >= payload.accepted_at + BODY_RETENTION:
            raise JobExpired("submission has expired")
        if not await self.gateway.create(f"payload/{payload.job_id}.json", _encode(payload)):
            original = await self.load_payload(payload.job_id)
            if (original.activity, original.partition_key, original.target,
                    original.run_id, original.request_id) != (
                    payload.activity, payload.partition_key, payload.target,
                    payload.run_id, payload.request_id):
                raise JobConflict("duplicate payload differs")
            payload = original
        initial = JobRecord(job_id=payload.job_id, partition_key=payload.partition_key,
                            accepted_at=payload.accepted_at)
        await self.gateway.create(self._name(payload.job_id), _encode(initial))
        # Never acquire a job or context lease inside this short inbox lease.
        async with self.inbox._edit(payload.partition_key) as edit:
            record = await self.read(payload.job_id)
            if record is None or record.partition_key != payload.partition_key:
                raise JobConflict("submission job is missing or mismatched")
            if record.terminal:
                receipt = Receipt(job_id=record.job_id, sequence=record.sequence,
                                  state=record.state, duplicate=True)
            else:
                ref = next((ref for ref in edit.record.pending if ref.job_id == payload.job_id), None)
                duplicate = ref is not None
                if ref is None:
                    if record.sequence is not None:
                        raise JobConflict("accepted nonterminal job disappeared from inbox")
                    ref = TaskRef(job_id=payload.job_id, sequence=edit.record.next_sequence)
                    edit.record.next_sequence += 1
                    edit.record.pending.append(ref)
                    edit.record.generation += 1
                    await edit.save()
                receipt = Receipt(job_id=ref.job_id, sequence=ref.sequence,
                                  state=record.state, duplicate=duplicate)
        if record.terminal:
            await self._clean_terminal(record)
        return receipt

    async def pending(self, partition_key: str) -> list[TaskRef]:
        pending = []
        for ref in await self.inbox.pending(partition_key):
            record = await self.read(ref.job_id)
            if record is None:
                raise JobConflict("inbox references a missing job")
            if record.partition_key != partition_key:
                raise JobConflict("inbox references another partition")
            if not record.terminal:
                pending.append(ref)
        return pending

    @asynccontextmanager
    async def leased_job(self, job_id: str, *,
                         include_payload: bool = True) -> AsyncIterator[LeasedJob]:
        name = self._name(job_id)
        if await self.gateway.read(name) is None:
            record = await self.read(job_id)
            if record is None or not record.terminal:
                raise JobStateError("job is missing")
            yield LeasedJob(self, record, None, None, None)
            return
        async with self.gateway.lease(name) as guard:
            document = await self.gateway.read(name)
            if document is None:
                raise JobConflict("leased job disappeared")
            record = _JOB_SLOT.validate_json(document.body)
            if isinstance(record, _QuarantineFence):
                raise JobQuarantined("job identity is reserved for quarantine")
            if record.job_id != job_id:
                raise JobConflict("leased job identity mismatch")
            payload = None
            if (include_payload and not record.terminal
                    and self.now() < record.accepted_at + BODY_RETENTION):
                payload = await self.load_payload(job_id)
                if payload.partition_key != record.partition_key:
                    raise JobConflict("job payload partition mismatch")
            handle = LeasedJob(self, record, document.etag, guard, payload)
            if not record.terminal and record.sequence is None:
                ref = next((ref for ref in await self.inbox.pending(record.partition_key)
                            if ref.job_id == job_id), None)
                if ref is None:
                    if self.now() < record.accepted_at + BODY_RETENTION:
                        raise JobStateError("job has not been accepted into its inbox")
                else:
                    await handle.bind_sequence(ref.sequence)
            yield handle

    async def _clean_terminal(self, record: JobRecord) -> None:
        tombstone = record.model_copy(update={"result": None})
        name = f"terminal-metadata/{record.job_id}.json"
        if not await self.gateway.create(name, _encode(tombstone)):
            existing = await self.gateway.read(name)
            if existing is None or JobRecord.model_validate_json(existing.body) != tombstone:
                raise JobConflict("terminal metadata mismatch")
        payload_name = f"payload/{record.job_id}.json"
        payload = await self.gateway.read(payload_name)
        if payload:
            await self.gateway.delete(payload_name, etag=payload.etag)

    async def _prune_job(self, job_id: str, result: PruneResult) -> None:
        try:
            record = await self.read(job_id)
        except JobQuarantined:
            await self._prune_quarantine(job_id, result)
            return
        if record is None or (
                not record.terminal and self.now() < record.accepted_at + BODY_RETENTION):
            return
        async with self.leased_job(job_id, include_payload=False) as job:
            if not job.record.terminal:
                await job.expire()
                result.expired += 1
            await self._clean_terminal(job.record)
            await self.inbox._retire_terminal(job.partition_key, job.job_id)
            if (job.guard is not None and job.record.terminal_at is not None
                    and self.now() >= job.record.terminal_at + METADATA_RETENTION):
                job.assert_active()
                await self.gateway.delete(self._name(job_id), etag=job.etag, lease_id=job.guard.id)
                result.removed += 1

    async def _prune_quarantine(self, job_id: str, result: PruneResult) -> None:
        name = self._name(job_id)
        if await self.gateway.read(name) is None:
            # Upgrade a legacy marker before touching its orphaned body.
            marker = await self.gateway.read(f"poison/job-{job_id}.json")
            payload = await self.gateway.read(f"payload/{job_id}.json")
            if marker is None or payload is None:
                return
            record = QuarantineRecord.model_validate_json(marker.body)
            original = TaskPayload.model_validate_json(payload.body)
            if record.job_id != job_id or original.job_id != job_id:
                raise JobConflict("quarantined payload identity mismatch")
            await self.gateway.create(name, _encode(
                _QuarantineFence(partition_key=original.partition_key, record=record)))
        async with self.gateway.lease(name) as guard:
            document = await self.gateway.read(name)
            if document is None:
                raise JobConflict("leased quarantine reservation disappeared")
            fence = _JOB_SLOT.validate_json(document.body)
            if isinstance(fence, JobRecord):
                return  # A repair won; do not delete its payload through a stale marker.
            if fence.record.job_id != job_id:
                raise JobConflict("quarantine reservation identity mismatch")
            etag = await self._finish_quarantine(fence, document.etag, guard)
            payload_name = f"payload/{job_id}.json"
            payload = await self.gateway.read(payload_name)
            if payload is not None:
                original = TaskPayload.model_validate_json(payload.body)
                if original.job_id != job_id or original.partition_key != fence.partition_key:
                    raise JobConflict("quarantined payload identity mismatch")
                if self.now() >= original.accepted_at + BODY_RETENTION:
                    guard.assert_active()
                    await self.gateway.delete(payload_name, etag=payload.etag)
                    result.removed += 1
            if self.now() >= fence.record.quarantined_at + METADATA_RETENTION:
                guard.assert_active()
                await self.gateway.delete(name, etag=etag, lease_id=guard.id)
                marker_name = f"poison/job-{job_id}.json"
                marker = await self.gateway.read(marker_name)
                if marker is not None:
                    await self.gateway.delete(marker_name, etag=marker.etag)
                result.removed += 1

    async def _prune_candidate(self, prefix: str, name: str, result: PruneResult) -> None:
        if prefix == "jobs/":
            await self._prune_job(name.removeprefix(prefix).removesuffix(".json"), result)
            return
        document = await self.gateway.read(name)
        if document is None:
            return
        if prefix == "poison/":
            marker = QuarantineRecord.model_validate_json(document.body)
            job_marker = f"poison/job-{marker.job_id}.json"
            if name not in (job_marker, f"poison/wake-{marker.body_digest}.json"):
                raise JobConflict("quarantine path mismatch")
            if self.now() >= marker.quarantined_at + METADATA_RETENTION:
                if name == job_marker and marker.job_id is not None:
                    await self._prune_quarantine(marker.job_id, result)
                    if await self.gateway.read(self._name(marker.job_id)) is not None:
                        return
                    document = await self.gateway.read(name)
                    if document is None:
                        return
                await self.gateway.delete(name, etag=document.etag)
                result.removed += 1
            return
        if prefix == "payload/":
            payload = TaskPayload.model_validate_json(document.body)
            self._name(payload.job_id)
            if name != f"payload/{payload.job_id}.json":
                raise JobConflict("payload path mismatch")
            if self.now() < payload.accepted_at + BODY_RETENTION:
                return
            try:
                record = await self.read(payload.job_id)
            except JobQuarantined:
                await self._prune_quarantine(payload.job_id, result)
                return
            if record is None:
                # Reserve the actual missing job, so no concurrent submission can
                # introduce an in-flight reference while an orphan is being removed.
                terminal = JobRecord(
                    job_id=payload.job_id, partition_key=payload.partition_key,
                    accepted_at=payload.accepted_at, state="expired",
                    terminal_at=self.now(), failure_code="expired")
                await self.gateway.create(self._name(payload.job_id), _encode(terminal))
            await self._prune_job(payload.job_id, result)
            return
        record = JobRecord.model_validate_json(document.body)
        if (name != f"terminal-metadata/{record.job_id}.json"
                or not record.terminal or record.terminal_at is None or record.result is not None):
            raise JobConflict("invalid terminal metadata")
        if self.now() < record.terminal_at + METADATA_RETENTION:
            return
        await self._prune_job(record.job_id, result)
        await self.inbox._retire_terminal(record.partition_key, record.job_id)
        await self.gateway.delete(name, etag=document.etag)
        result.removed += 1

    async def prune(self, *, limit: int = 200, cursor: PruneCursor | None = None,
                    cursors: dict[str, str | None] | None = None,
                    continue_on_error: bool = False) -> PruneResult:
        """At most limit enumerated candidates TOTAL; coordination reads are additional.

        Persist result.cursor between calls. The legacy cursors mapping remains accepted.
        """
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if cursor is not None and cursors is not None:
            raise ValueError("use one cursor representation")
        prefixes = ("jobs/", "payload/", "terminal-metadata/", "poison/")
        if cursor is None:
            prefix = (cursors or {}).get("_prefix") or "jobs/"
            cursor = PruneCursor(prefix=prefix, marker=(cursors or {}).get(prefix))
        result = PruneResult(cursor=cursor)
        while result.scanned < limit and result.cursor is not None:
            current = result.cursor
            page = await self.gateway.list_page(current.prefix, cursor=current.marker, limit=1)
            for name in page.names:
                result.scanned += 1
                try:
                    await self._prune_candidate(current.prefix, name, result)
                except LeaseBusy:
                    result.skipped += 1
                except LeaseLost:
                    raise
                except Exception as error:
                    if not continue_on_error:
                        raise
                    _log_failure("maintenance.prune_failed", error)
                    result.failed += 1
            if page.next_cursor:
                result.cursor = PruneCursor(prefix=current.prefix, marker=page.next_cursor)
            else:
                index = prefixes.index(current.prefix) + 1
                result.cursor = PruneCursor(prefix=prefixes[index]) if index < len(prefixes) else None
        return result


class LeasedJob:
    def __init__(self, store: JobStore, record: JobRecord, etag: str | None,
                 guard: LeaseGuard | None, payload: TaskPayload | None):
        self.store, self.record, self.etag, self.guard = store, record, etag, guard
        self.payload = payload

    @property
    def job_id(self) -> str:
        return self.record.job_id

    @property
    def partition_key(self) -> str:
        return self.record.partition_key

    @property
    def sequence(self) -> int | None:
        return self.record.sequence

    @property
    def state(self) -> JobState:
        return self.record.state

    @property
    def result(self) -> PreparedTurn | None:
        return self.record.result

    @property
    def result_written_at(self) -> datetime | None:
        return self.record.result_written_at

    def assert_active(self) -> None:
        if self.guard is None:
            if not self.record.terminal:
                raise JobStateError("job has no lease")
        else:
            self.guard.assert_active()

    async def _save(self, **changes) -> None:
        self.assert_active()
        if self.guard is None:
            raise JobStateError("terminal metadata is immutable")
        record = JobRecord.model_validate({**self.record.model_dump(), **changes})
        etag = await self.store.gateway.write(
            self.store._name(self.job_id), _encode(record),
            etag=self.etag, lease_id=self.guard.id)
        self.record, self.etag = record, etag

    def _require(self, *states: JobState) -> None:
        if self.state not in states:
            raise JobStateError("invalid job state transition")
        if self.store.now() >= self.record.accepted_at + BODY_RETENTION:
            raise JobExpired("job has expired")

    async def bind_sequence(self, sequence: int) -> None:
        if self.record.sequence is not None:
            if self.record.sequence != sequence:
                raise JobConflict("job sequence differs")
            return
        if self.state != "pending_notify":
            raise JobStateError("only an unbound pending job can receive a sequence")
        await self._save(sequence=sequence, state="queued")

    async def mark_running(self) -> None:
        self._require("queued", "retry_wait")
        if self.result is not None or (self.record.retry_at and self.store.now() < self.record.retry_at):
            raise JobStateError("job is not ready to generate")
        await self._save(state="running", generation_attempts=self.record.generation_attempts + 1,
                         retry_at=None, failure_code=None)

    async def save_result(self, prepared: PreparedTurn, written_at: datetime | None = None) -> None:
        if self.state in ("result_ready", "sending") and self.result == prepared:
            return
        self._require("running")
        await self._save(state="result_ready", result=prepared,
                         result_written_at=_utc(written_at or self.store.now()))

    async def mark_committing(self) -> None:
        self._require("result_ready", "retry_wait")
        if self.result is None:
            raise JobStateError("history commit requires a saved result")
        await self._save(history_attempts=self.record.history_attempts + 1)

    async def mark_history_committed(self) -> None:
        self._require("result_ready", "retry_wait")
        if self.result is None:
            raise JobStateError("history commit requires a saved result")
        await self._save(history_committed=True)

    async def note_recovery(self) -> None:
        self._require("pending_notify", "queued", "running", "retry_wait", "result_ready", "sending")
        if self.record.recovery_pending:
            return
        await self._save(recovery_attempts=self.record.recovery_attempts + 1,
                         recovery_trace_id=uuid4().hex, recovery_pending=True)

    async def confirm_recovery(self) -> None:
        self._require("pending_notify", "queued", "running", "retry_wait", "result_ready", "sending")
        if not self.record.recovery_trace_id:
            raise JobStateError("recovery notification has no reservation")
        await self._save(recovery_pending=False)

    async def cleanup_terminal(self) -> None:
        if not self.record.terminal:
            raise JobStateError("job is not terminal")
        self.assert_active()
        await self.store._clean_terminal(self.record)

    async def mark_sending(self) -> None:
        self._require("result_ready", "retry_wait")
        if self.result is None or (self.record.retry_at and self.store.now() < self.record.retry_at):
            raise JobStateError("job is not ready to send")
        await self._save(state="sending", delivery_attempts=self.record.delivery_attempts + 1,
                         retry_at=None, failure_code=None)

    async def retry_delivery(self, failure: DeliveryFailure, *,
                             retry_at: datetime | None = None) -> None:
        self._require("sending")
        if (not isinstance(failure, DeliveryFailure) or failure.definitely_not_sent is not True
                or failure.retryable is not True):
            raise JobStateError("delivery retry requires proof it was definitely unsent and retryable")
        try:
            provider_time = self.store.now() + timedelta(seconds=failure.retry_after_seconds or 0)
        except OverflowError:
            provider_time = datetime.max.replace(tzinfo=timezone.utc)
        due = max(provider_time, _utc(retry_at) if retry_at is not None else provider_time)
        await self._save(state="retry_wait", failure_code="delivery_failed", retry_at=due)

    async def fail_delivery(self, failure: DeliveryFailure, *,
                            code: FailureCode = "delivery_failed") -> None:
        self._require("sending")
        if not isinstance(failure, DeliveryFailure) or failure.definitely_not_sent is not True:
            raise JobStateError("delivery failure requires proof it was definitely unsent")
        await self._terminal("failed", failure_code=code)

    async def retry(self, code: FailureCode, *, retry_at: datetime | None = None) -> None:
        self._require("queued", "running", "retry_wait")
        await self._save(state="retry_wait", failure_code=code,
                         retry_at=_utc(retry_at or self.store.now()))

    async def _terminal(self, state: JobState, **changes) -> None:
        await self._save(state=state, terminal_at=self.store.now(), result=None, retry_at=None,
                         recovery_pending=False, **changes)
        await self.store._clean_terminal(self.record)
        self.payload = None

    async def complete(self, receipt: str | None) -> None:
        if self.state == "completed":
            if self.record.delivery_receipt != receipt:
                raise JobConflict("completion receipt differs")
            await self.store._clean_terminal(self.record)
            return
        if self.state == "result_ready" and self.result is not None and self.result.reply is None:
            self._require("result_ready")
        else:
            self._require("sending")
        await self._terminal("completed", delivery_receipt=receipt)

    async def fail(self, code: FailureCode) -> None:
        self._require("pending_notify", "queued", "running", "retry_wait", "result_ready")
        await self._terminal("failed", failure_code=code)

    async def mark_delivery_unknown(self, code: FailureCode) -> None:
        self._require("sending")
        await self._terminal("delivery_unknown", failure_code=code)

    async def expire(self) -> None:
        if self.record.terminal:
            return
        if self.store.now() < self.record.accepted_at + BODY_RETENTION:
            raise JobStateError("job is not expired")
        await self._terminal("delivery_unknown" if self.state == "sending" else "expired",
                             failure_code="expired")
