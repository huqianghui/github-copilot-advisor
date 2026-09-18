import asyncio

from azure.storage.blob.aio import BlobLeaseClient
import pytest

from advisor_agent.durable.leases import LeaseLost
from advisor_agent.durable.maintenance import Maintenance
from advisor_shared.cloud import QueueWake


async def test_recovers_unpublished_outbox_without_agent_initialization(worker_case):
    case = worker_case
    await case.accept(notify=False)
    maintenance = Maintenance(case.jobs, case.outbox)
    result = await maintenance.run_once()
    assert result.republished == 1
    assert case.executor.prepare_calls == case.executor.send_calls == 0
    result = await maintenance.run_once()
    assert result.republished == 0


async def test_budget_and_cursor_survive_a_new_maintenance_instance(worker_case):
    case = worker_case
    for user in ("a", "b", "c"):
        await case.accept(user, user, notify=False)
    first = await Maintenance(case.jobs, case.outbox, max_objects=1).run_once()
    second = await Maintenance(case.jobs, case.outbox, max_objects=1).run_once()
    third = await Maintenance(case.jobs, case.outbox, max_objects=1).run_once()
    assert [first.scanned, second.scanned, third.scanned] == [1, 1, 1]
    assert [first.republished, second.republished, third.republished] == [1, 1, 1]
    assert first.cursors != second.cursors


async def test_maintenance_uses_lease_on_actual_state_blob(worker_case, monkeypatch):
    case = worker_case
    await case.accept(notify=False)
    entered, proceed = asyncio.Event(), asyncio.Event()
    notify = case.outbox.notify

    async def paused(*args, **kwargs):
        entered.set()
        await proceed.wait()
        await notify(*args, **kwargs)

    monkeypatch.setattr(case.outbox, "notify", paused)
    first = asyncio.create_task(Maintenance(case.jobs, case.outbox).run_once())
    await asyncio.wait_for(entered.wait(), 10)
    second = await Maintenance(case.jobs, case.outbox).run_once()
    assert second.busy and second.scanned == 0
    proceed.set()
    assert (await first).republished == 1


async def test_lost_state_lease_cannot_write_progress(worker_case, storage_harness, monkeypatch):
    case = worker_case
    await case.accept(notify=False)
    notify = case.outbox.notify

    async def broken(*args, **kwargs):
        await notify(*args, **kwargs)
        await BlobLeaseClient(storage_harness[1].get_blob_client(
            "maintenance/state.json")).break_lease(lease_break_period=0)

    monkeypatch.setattr(case.outbox, "notify", broken)
    with pytest.raises(LeaseLost):
        await Maintenance(case.jobs, case.outbox).run_once()


async def test_failed_progress_write_replays_idempotently(worker_case, monkeypatch):
    case = worker_case
    await case.accept(notify=False)
    write = case.jobs.gateway.write

    async def failed(name, *args, **kwargs):
        if name == "maintenance/state.json":
            raise ConnectionError("lost progress acknowledgement")
        return await write(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(case.jobs.gateway, "write", failed)
        with pytest.raises(ConnectionError):
            await Maintenance(case.jobs, case.outbox).run_once()
    result = await Maintenance(case.jobs, case.outbox).run_once()
    assert result.republished == 0


async def test_time_budget_stops_new_work_and_persists_progress(worker_case, monkeypatch):
    case = worker_case
    for user in ("a", "b"):
        await case.accept(user, user, notify=False)
    clock = [0.0]
    notify = case.outbox.notify

    async def slow(*args, **kwargs):
        await notify(*args, **kwargs)
        clock[0] = 31.0

    monkeypatch.setattr(case.outbox, "notify", slow)
    result = await Maintenance(
        case.jobs, case.outbox, monotonic_clock=lambda: clock[0]).run_once()
    assert result.scanned == 1 and result.timed_out
    assert result.republished == 1


async def test_active_job_lease_prevents_expiry_cleanup(worker_case):
    case = worker_case
    receipt = await case.accept()
    async with case.jobs.leased_job(receipt.job_id) as job:
        await job.mark_running()
        case.clock.advance(hours=25)
        result = await Maintenance(case.jobs, case.outbox).run_once()
        assert result.skipped > 0
        assert await case.jobs.gateway.read(f"payload/{receipt.job_id}.json") is not None
        assert (await case.jobs.read(receipt.job_id)).state == "running"


async def test_invalid_poison_is_quarantined_without_body_leak(
        worker_case, poison_queue_harness, caplog):
    case = worker_case
    poison, client, _ = poison_queue_harness
    await client.send_message("private-token https://private.invalid/?secret=private-body")
    maintenance = Maintenance(case.jobs, case.outbox, history=case.history, poison=poison)
    result = await maintenance.run_once()
    assert result.quarantined == 1
    assert await client.peek_messages() == []
    page = await case.jobs.gateway.list_page("poison/")
    assert len(page.names) == 1
    document = await case.jobs.gateway.read(page.names[0])
    assert b"private-" not in document.body
    for record in caplog.records:
        if record.name.startswith("advisor_agent"):
            assert "private-" not in record.getMessage()
            assert record.exc_info is None
    assert (await maintenance.run_once()).quarantined == 0


async def test_terminal_poison_is_not_republished(worker_case, poison_queue_harness):
    case = worker_case
    poison, client, _ = poison_queue_harness
    await case.accept()
    await case.worker.process_session(case.partition(), "trace")
    await poison.publish(QueueWake(partition_key=case.partition(), trace_id="trace"))
    result = await Maintenance(case.jobs, case.outbox, history=case.history, poison=poison).run_once()
    assert result.republished == 0
    assert await client.peek_messages() == []


async def test_poison_recovery_budget_is_durable_and_bounded(
        worker_case, poison_queue_harness):
    case = worker_case
    poison, client, _ = poison_queue_harness
    receipt = await case.accept()
    trace = "first-poison"
    for _ in range(4):
        await poison.publish(QueueWake(partition_key=case.partition(), trace_id=trace))
        await Maintenance(case.jobs, case.outbox, history=case.history, poison=poison).run_once()
        trace = (await case.jobs.read(receipt.job_id)).recovery_trace_id
    record = await case.jobs.read(receipt.job_id)
    assert record.state == "failed" and record.recovery_attempts == 3
    assert await case.jobs.inbox.head(case.partition()) is None
    assert await client.peek_messages() == []


async def test_poison_does_not_consume_recovery_budget_on_job_contention(
        worker_case, poison_queue_harness):
    case = worker_case
    poison, _, _ = poison_queue_harness
    receipt = await case.accept()
    await poison.publish(QueueWake(partition_key=case.partition(), trace_id="trace"))
    async with case.jobs.leased_job(receipt.job_id):
        result = await Maintenance(case.jobs, case.outbox, history=case.history, poison=poison).run_once()
        assert result.skipped >= 1
        assert (await case.jobs.read(receipt.job_id)).recovery_attempts == 0


async def test_timeout_during_checkpoint_returns_only_confirmed_progress(worker_case, monkeypatch):
    import json
    case = worker_case
    await case.accept(notify=False)
    write = case.jobs.gateway.write
    entered = asyncio.Event()

    async def hang(name, *args, **kwargs):
        if name == "maintenance/state.json":
            entered.set()
            await asyncio.Event().wait()
        return await write(name, *args, **kwargs)

    monkeypatch.setattr(case.jobs.gateway, "write", hang)
    result = await Maintenance(case.jobs, case.outbox, max_seconds=2).run_once()
    assert entered.is_set() and result.timed_out
    persisted = await case.jobs.gateway.read("maintenance/state.json")
    assert result.cursors == json.loads(persisted.body)


async def test_quarantine_recovery_retires_head_after_interrupted_advance(
        worker_case, monkeypatch):
    from advisor_agent.durable.jobs import JobStateError
    case = worker_case
    receipt = await case.accept()
    name = f"jobs/{receipt.job_id}.json"
    document = await case.jobs.gateway.read(name)
    await case.jobs.gateway.delete(name, etag=document.etag)
    advance = case.jobs.inbox.advance
    calls = 0

    async def fail_once(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("interrupted terminal advance")
        await advance(*args)

    monkeypatch.setattr(case.jobs.inbox, "advance", fail_once)
    await Maintenance(case.jobs, case.outbox).run_once()
    await Maintenance(case.jobs, case.outbox).run_once()
    assert await case.jobs.inbox.head(case.partition()) is None
    with pytest.raises(JobStateError):
        await case.jobs.submit(case.payloads["a"])


async def test_body_free_quarantine_metadata_is_retained_only_seven_days(
        worker_case, poison_queue_harness):
    case = worker_case
    poison, client, _ = poison_queue_harness
    await client.send_message("invalid private-body")
    maintenance = Maintenance(case.jobs, case.outbox, history=case.history, poison=poison)
    await maintenance.run_once()
    assert (await case.jobs.gateway.list_page("poison/")).names
    case.clock.advance(days=7)
    await maintenance.run_once()
    assert (await case.jobs.gateway.list_page("poison/")).names == []


async def test_terminal_unadvanced_head_is_not_published_again(worker_case, monkeypatch):
    case = worker_case
    await case.accept(notify=False)

    async def interrupted(*args):
        raise ConnectionError("interrupted terminal advance")

    with monkeypatch.context() as patch:
        patch.setattr(case.jobs.inbox, "advance", interrupted)
        with pytest.raises(ConnectionError):
            await case.worker.process_session(case.partition(), "trace")
    result = await Maintenance(case.jobs, case.outbox).run_once()
    assert result.republished == 0 and result.failed == 0
    assert await case.jobs.inbox.head(case.partition()) is None


async def test_duplicate_poison_does_not_exhaust_a_healthy_jobs_recovery_budget(
        worker_case, poison_queue_harness):
    case = worker_case
    poison, _, _ = poison_queue_harness
    receipt = await case.accept()
    for _ in range(4):
        await poison.publish(QueueWake(partition_key=case.partition(), trace_id="same-poison"))
        await Maintenance(case.jobs, case.outbox, history=case.history, poison=poison).run_once()
    record = await case.jobs.read(receipt.job_id)
    assert record.state == "queued" and record.recovery_attempts == 1


async def test_pending_poison_publication_retries_the_same_recovery_reservation(
        worker_case, poison_queue_harness, monkeypatch):
    case = worker_case
    poison, _, _ = poison_queue_harness
    receipt = await case.accept()
    wake = QueueWake(partition_key=case.partition(), trace_id="same-poison")
    await poison.publish(wake)

    async def unavailable(*args, **kwargs):
        raise ConnectionError("queue unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(case.outbox, "notify", unavailable)
        await Maintenance(case.jobs, case.outbox, history=case.history, poison=poison).run_once()
    first = await case.jobs.read(receipt.job_id)
    assert first.recovery_pending and first.recovery_attempts == 1
    await poison.publish(wake)
    await Maintenance(case.jobs, case.outbox, history=case.history, poison=poison).run_once()
    restored = await case.jobs.read(receipt.job_id)
    assert not restored.recovery_pending and restored.recovery_attempts == 1
    assert restored.recovery_trace_id == first.recovery_trace_id


async def test_poison_context_contention_does_not_consume_recovery_budget(
        worker_case, poison_queue_harness):
    case = worker_case
    poison, _, _ = poison_queue_harness
    receipt = await case.accept()
    await poison.publish(QueueWake(partition_key=case.partition(), trace_id="trace"))
    async with case.history.leased(case.partition()):
        result = await Maintenance(case.jobs, case.outbox, history=case.history, poison=poison).run_once()
        assert result.skipped >= 1
        assert (await case.jobs.read(receipt.job_id)).recovery_attempts == 0


async def test_time_budget_also_bounds_initial_storage_setup(worker_case, monkeypatch):
    case = worker_case

    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(case.jobs.gateway, "read", stalled)
    result = await asyncio.wait_for(
        Maintenance(case.jobs, case.outbox, max_seconds=0.05).run_once(), 3)
    assert result.timed_out and result.scanned == 0


async def test_local_contract_transport_never_reads_ambient_netrc(blob_gateway, monkeypatch):
    import aiohttp

    def forbidden(*args):
        raise AssertionError("loopback contract consulted ambient credentials")

    monkeypatch.setattr(aiohttp.ClientSession, "_get_netrc_auth", forbidden)
    assert await blob_gateway.create("maintenance/transport-check.json", b"{}")
