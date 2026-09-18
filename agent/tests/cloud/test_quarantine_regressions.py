"""Fenced quarantine regressions; run unchanged through SDK memory and Azurite."""
import hashlib
from contextlib import asynccontextmanager

from azure.core.exceptions import HttpResponseError
import pytest

from advisor_agent.durable.blob import BlobConflict
from advisor_agent.durable.jobs import JobQuarantined, JobRecord, QuarantineRecord, TaskRef
from advisor_agent.durable.leases import LeaseLost
from advisor_agent.durable.maintenance import Maintenance
from advisor_shared.cloud import PreparedTurn, QueueWake


async def run_path(case, poison_harness, path):
    poison = None
    if path == "poison":
        poison = poison_harness[0]
        await poison.publish(QueueWake(partition_key=case.partition(), trace_id="review"))
        await case.jobs.gateway.create("maintenance/state.json", b'{"version":1,"phase":"poison"}')
    return await Maintenance(
        case.jobs, case.outbox, history=case.history, poison=poison).run_once()


async def delete_job(case, job_id):
    name = f"jobs/{job_id}.json"
    document = await case.jobs.gateway.read(name)
    await case.jobs.gateway.delete(name, etag=document.etag)


@pytest.mark.parametrize("path", ["outbox", "poison"])
async def test_missing_job_resubmission_wins_before_quarantine_reservation(
        worker_case, poison_queue_harness, monkeypatch, path):
    case = worker_case
    original = await case.accept()
    payload = case.payloads["a"]
    await delete_job(case, original.job_id)
    create = case.jobs.gateway.create
    repaired = []
    fired = False

    async def interleaved(name, body):
        nonlocal fired
        if not fired and name in {
                f"jobs/{original.job_id}.json", f"poison/job-{original.job_id}.json"}:
            fired = True
            repaired.append(await case.jobs.submit(payload))
            await case.outbox.notify(payload.partition_key, payload.trace_id)
        return await create(name, body)

    monkeypatch.setattr(case.jobs.gateway, "create", interleaved)
    await run_path(case, poison_queue_harness, path)
    assert fired and repaired[0].sequence == 1
    head = await case.jobs.inbox.head(case.partition())
    assert head is not None and head.job_id == original.job_id
    assert not (await case.jobs.read(original.job_id)).terminal
    assert await case.jobs.gateway.read(f"poison/job-{original.job_id}.json") is None
    assert await case.worker.process_session(case.partition(), "review") == "drained"
    assert case.executor.send_calls == 1


@pytest.mark.parametrize("path", ["outbox", "poison"])
async def test_reserved_quarantine_rejects_resubmission_before_retiring_head(
        worker_case, poison_queue_harness, monkeypatch, path):
    case = worker_case
    receipt = await case.accept()
    await delete_job(case, receipt.job_id)
    create = case.jobs.gateway.create
    rejected = []

    async def during_marker(name, body):
        if name == f"poison/job-{receipt.job_id}.json" and not rejected:
            try:
                await case.jobs.submit(case.payloads["a"])
            except JobQuarantined:
                rejected.append(True)
            else:
                rejected.append(False)
        return await create(name, body)

    monkeypatch.setattr(case.jobs.gateway, "create", during_marker)
    await run_path(case, poison_queue_harness, path)
    assert rejected == [True]
    assert await case.jobs.inbox.head(case.partition()) is None
    with pytest.raises(JobQuarantined):
        await case.jobs.read(receipt.job_id)


@pytest.mark.parametrize("path", ["outbox", "poison"])
async def test_malformed_record_is_quarantined_and_next_job_can_run(
        worker_case, poison_queue_harness, path, caplog):
    case = worker_case
    bad = await case.accept("a")
    next_job = await case.accept("next")
    name = f"jobs/{bad.job_id}.json"
    document = await case.jobs.gateway.read(name)
    await case.jobs.gateway.write(name, b'{"version":1}', etag=document.etag)
    result = await run_path(case, poison_queue_harness, path)
    assert result.quarantined == 1
    assert (await case.jobs.inbox.head(case.partition())).job_id == next_job.job_id
    with pytest.raises(JobQuarantined):
        await case.jobs.read(bad.job_id)
    if path == "poison":
        assert await poison_queue_harness[1].peek_messages() == []
    assert await case.worker.process_session(case.partition(), "review") == "drained"
    assert case.executor.sent == ["next"]
    for record in caplog.records:
        if record.name.startswith("advisor_agent"):
            assert record.exc_info is None


async def test_malformed_record_with_an_active_job_lease_is_not_quarantined(worker_case):
    case = worker_case
    receipt = await case.accept()
    name = f"jobs/{receipt.job_id}.json"
    document = await case.jobs.gateway.read(name)
    await case.jobs.gateway.write(name, b'{"version":1}', etag=document.etag)
    async with case.jobs.gateway.lease(name):
        result = await Maintenance(case.jobs, case.outbox).run_once()
        assert result.skipped >= 1
        assert (await case.jobs.inbox.head(case.partition())).job_id == receipt.job_id
        assert not (await case.jobs.gateway.list_page("poison/")).names
    assert (await Maintenance(case.jobs, case.outbox).run_once()).quarantined == 1


async def test_valid_repair_is_rechecked_after_acquiring_actual_job_lease(worker_case, monkeypatch):
    case = worker_case
    receipt = await case.accept()
    name = f"jobs/{receipt.job_id}.json"
    original = await case.jobs.gateway.read(name)
    await case.jobs.gateway.write(name, b'{"version":1}', etag=original.etag)
    lease = case.jobs.gateway.lease
    repaired = False

    @asynccontextmanager
    async def interleaved(target):
        nonlocal repaired
        if target == name and not repaired:
            repaired = True
            current = await case.jobs.gateway.read(name)
            await case.jobs.gateway.write(name, original.body, etag=current.etag)
        async with lease(target) as guard:
            yield guard

    monkeypatch.setattr(case.jobs.gateway, "lease", interleaved)
    await Maintenance(case.jobs, case.outbox).run_once()
    assert repaired
    assert (await case.jobs.inbox.head(case.partition())).job_id == receipt.job_id
    assert not (await case.jobs.gateway.list_page("poison/")).names
    await case.worker.process_session(case.partition(), "review")
    assert case.executor.send_calls == 1


@pytest.mark.parametrize("status", [403, 503])
async def test_storage_failures_are_not_malformed_record_quarantine(worker_case, monkeypatch, status):
    case = worker_case
    receipt = await case.accept()
    read = case.jobs.gateway.read
    error = HttpResponseError("injected storage failure")
    error.status_code = status

    async def denied(name):
        if name == f"jobs/{receipt.job_id}.json":
            raise error
        return await read(name)

    with monkeypatch.context() as patch:
        patch.setattr(case.jobs.gateway, "read", denied)
        result = await Maintenance(case.jobs, case.outbox).run_once()
        assert result.failed > 0 and result.quarantined == 0
    assert (await case.jobs.inbox.head(case.partition())).job_id == receipt.job_id
    assert not (await case.jobs.gateway.list_page("poison/")).names


@pytest.mark.parametrize("legacy", [False, True])
async def test_quarantined_payload_expires_independently_of_seven_day_marker(worker_case, legacy):
    case = worker_case
    receipt = await case.accept()
    await delete_job(case, receipt.job_id)
    marker_name = f"poison/job-{receipt.job_id}.json"
    if legacy:
        marker = QuarantineRecord(
            code="job_missing", body_digest=hashlib.sha256(receipt.job_id.encode()).hexdigest(),
            quarantined_at=case.clock(), job_id=receipt.job_id, sequence=receipt.sequence)
        await case.jobs.gateway.create(marker_name, marker.model_dump_json().encode())
        await case.jobs.inbox.advance(case.partition(), receipt.job_id)
    else:
        await Maintenance(case.jobs, case.outbox).run_once()
    original_marker = await case.jobs.gateway.read(marker_name)
    case.clock.advance(hours=25)
    result = await case.jobs.prune()
    assert result.failed == 0
    assert await case.jobs.gateway.read(f"payload/{receipt.job_id}.json") is None
    assert (await case.jobs.gateway.read(marker_name)).body == original_marker.body
    with pytest.raises(JobQuarantined):
        await case.jobs.read(receipt.job_id)
    case.clock.advance(days=6)
    await case.jobs.prune()
    assert await case.jobs.gateway.read(marker_name) is None
    assert await case.jobs.read(receipt.job_id) is None


async def test_quarantine_cannot_retire_head_after_losing_job_fence(
        worker_case, expire_lease, monkeypatch):
    case = worker_case
    receipt = await case.accept()
    await delete_job(case, receipt.job_id)
    create = case.jobs.gateway.create
    broke = False

    async def lose_before_retirement(name, body):
        nonlocal broke
        result = await create(name, body)
        if name == f"poison/job-{receipt.job_id}.json" and not broke:
            broke = True
            await expire_lease(f"jobs/{receipt.job_id}.json")
        return result

    monkeypatch.setattr(case.jobs.gateway, "create", lose_before_retirement)
    with pytest.raises(LeaseLost):
        await Maintenance(case.jobs, case.outbox).run_once()
    assert (await case.jobs.inbox.head(case.partition())).job_id == receipt.job_id


@pytest.mark.parametrize("boundary", ["marker", "before_inbox", "after_inbox"])
async def test_quarantine_resumes_each_durable_retirement_boundary(
        worker_case, monkeypatch, boundary):
    case = worker_case
    bad = await case.accept()
    good = await case.accept("next", notify=False)
    await delete_job(case, bad.job_id)
    create, write = case.jobs.gateway.create, case.jobs.gateway.write
    fired = False

    async def fail_marker(name, body):
        nonlocal fired
        if boundary == "marker" and name == f"poison/job-{bad.job_id}.json" and not fired:
            fired = True
            raise ConnectionError("interrupted quarantine audit")
        return await create(name, body)

    async def fail_inbox(name, body, **kwargs):
        nonlocal fired
        eligible = name.startswith("inbox/") and not fired
        if eligible and boundary == "before_inbox":
            fired = True
            raise ConnectionError("interrupted retirement")
        result = await write(name, body, **kwargs)
        if eligible and boundary == "after_inbox":
            fired = True
            raise ConnectionError("retirement acknowledgement lost")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(case.jobs.gateway, "create", fail_marker)
        patch.setattr(case.jobs.gateway, "write", fail_inbox)
        first = await Maintenance(case.jobs, case.outbox, max_objects=1).run_once()
        assert first.failed == 1 and fired
    with pytest.raises(JobQuarantined):
        await case.jobs.submit(case.payloads["a"])
    await Maintenance(case.jobs, case.outbox).run_once()
    assert (await case.jobs.inbox.head(case.partition())).job_id == good.job_id
    await case.worker.process_session(case.partition(), "review")
    assert case.executor.sent == ["next"]


async def test_quarantined_body_cleanup_respects_an_active_fence_lease(worker_case):
    case = worker_case
    receipt = await case.accept()
    await delete_job(case, receipt.job_id)
    await Maintenance(case.jobs, case.outbox).run_once()
    case.clock.advance(hours=25)
    async with case.jobs.gateway.lease(f"jobs/{receipt.job_id}.json"):
        result = await case.jobs.prune()
        assert result.skipped > 0
        assert await case.jobs.gateway.read(f"payload/{receipt.job_id}.json") is not None
    await case.jobs.prune()
    assert await case.jobs.gateway.read(f"payload/{receipt.job_id}.json") is None


async def corrupt_finished_head(case, *, corrupt_live=False):
    first = await case.accept("a")
    second = await case.accept("next")
    async with case.jobs.leased_job(first.job_id) as job:
        await job.mark_running()
        await job.save_result(PreparedTurn(
            reply={"type": "message", "text": "private-answer"},
            history=[{"role": "user", "content": "private-question"},
                     {"role": "assistant", "content": "private-answer"}], outcome="success"))
        await job.mark_sending()
        await job.complete("first-receipt")
    live_name = f"jobs/{first.job_id}.json"
    live = await case.jobs.gateway.read(live_name)
    terminal_name = f"terminal-metadata/{first.job_id}.json"
    terminal = await case.jobs.gateway.read(terminal_name)
    await case.jobs.gateway.write(terminal_name, b'{"version":1}', etag=terminal.etag)
    if corrupt_live:
        await case.jobs.gateway.write(live_name, b'{"version":1}', etag=live.etag)
    return first, second, live.body, terminal_name


@pytest.mark.parametrize("path", ["outbox", "poison"])
async def test_malformed_terminal_metadata_is_reconciled_from_fenced_completed_live_row(
        worker_case, poison_queue_harness, path):
    case = worker_case
    first, second, live_body, terminal_name = await corrupt_finished_head(case)
    result = await run_path(case, poison_queue_harness, path)
    assert result.failed == 0 and result.quarantined == 0
    restored = await case.jobs.gateway.read(terminal_name)
    assert JobRecord.model_validate_json(restored.body) == JobRecord.model_validate_json(live_body)
    assert (await case.jobs.read(first.job_id)).delivery_receipt == "first-receipt"
    assert (await case.jobs.inbox.head(case.partition())).job_id == second.job_id
    if path == "poison":
        assert any(op == "ack" and "/messages/" in request.url
                   for op, request in poison_queue_harness[2].calls)
    await case.worker.process_session(case.partition(), "review")
    assert case.executor.sent == ["next"]


@pytest.mark.parametrize("path", ["outbox", "poison"])
async def test_malformed_terminal_and_live_rows_are_boundedly_quarantined(
        worker_case, poison_queue_harness, path):
    case = worker_case
    first, second, _, terminal_name = await corrupt_finished_head(case, corrupt_live=True)
    result = await run_path(case, poison_queue_harness, path)
    assert result.quarantined == 1
    with pytest.raises(JobQuarantined):
        await case.jobs.read(first.job_id)
    assert await case.jobs.gateway.read(terminal_name) is None
    assert (await case.jobs.inbox.head(case.partition())).job_id == second.job_id
    await case.worker.process_session(case.partition(), "review")
    assert case.executor.sent == ["next"]


async def test_terminal_metadata_reconciliation_respects_active_live_job_lease(worker_case):
    case = worker_case
    first, _, _, terminal_name = await corrupt_finished_head(case)
    async with case.jobs.gateway.lease(f"jobs/{first.job_id}.json"):
        result = await Maintenance(case.jobs, case.outbox).run_once()
        assert result.skipped >= 1
        assert (await case.jobs.gateway.read(terminal_name)).body == b'{"version":1}'
        assert (await case.jobs.inbox.head(case.partition())).job_id == first.job_id
    await Maintenance(case.jobs, case.outbox).run_once()
    assert (await case.jobs.read(first.job_id)).state == "completed"


@pytest.mark.parametrize("operation", ["read", "write"])
@pytest.mark.parametrize("status", [403, 503])
async def test_terminal_metadata_transport_errors_remain_errors_not_quarantine(
        worker_case, monkeypatch, operation, status):
    case = worker_case
    first, _, _, terminal_name = await corrupt_finished_head(case)
    original = getattr(case.jobs.gateway, operation)
    error = HttpResponseError("injected terminal metadata transport failure")
    error.status_code = status

    async def fail(name, *args, **kwargs):
        if name == terminal_name:
            raise error
        return await original(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(case.jobs.gateway, operation, fail)
        with pytest.raises(HttpResponseError) as observed:
            await case.jobs.quarantine_job(
                case.partition(), TaskRef(job_id=first.job_id, sequence=first.sequence))
        assert observed.value.status_code == status
    assert (await case.jobs.gateway.read(terminal_name)).body == b'{"version":1}'
    assert (await case.jobs.inbox.head(case.partition())).job_id == first.job_id
    assert not (await case.jobs.gateway.list_page("poison/")).names


async def test_terminal_metadata_reconciliation_is_conditional_on_its_etag(worker_case, monkeypatch):
    case = worker_case
    first, _, live_body, terminal_name = await corrupt_finished_head(case)
    write = case.jobs.gateway.write
    competitor = JobRecord.model_validate_json(live_body).model_copy(
        update={"delivery_receipt": "concurrent-receipt"})
    raced = False

    async def race(name, body, **kwargs):
        nonlocal raced
        if name == terminal_name and not raced:
            raced = True
            current = await case.jobs.gateway.read(name)
            await write(name, competitor.model_dump_json().encode(), etag=current.etag)
        return await write(name, body, **kwargs)

    monkeypatch.setattr(case.jobs.gateway, "write", race)
    with pytest.raises(BlobConflict):
        await case.jobs.quarantine_job(
            case.partition(), TaskRef(job_id=first.job_id, sequence=first.sequence))
    assert (await case.jobs.read(first.job_id)).delivery_receipt == "concurrent-receipt"
    assert (await case.jobs.inbox.head(case.partition())).job_id == first.job_id
