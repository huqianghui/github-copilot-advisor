import asyncio

from azure.core.exceptions import HttpResponseError
from azure.storage.blob.aio import BlobLeaseClient
import pytest

from advisor_agent.durable.leases import LeaseLost
from advisor_agent.durable.worker import SessionWorker
from advisor_shared.delivery import DeliveryFailure


@pytest.mark.parametrize("point", ["before_history_commit", "after_history_commit"])
async def test_saved_result_is_reused_after_history_commit_crash(worker_case, point):
    case = worker_case
    await case.accept()
    case.fail_once_at(point)
    with pytest.raises(ConnectionError):
        await case.worker.process_session(case.partition(), "trace")
    assert await case.worker.process_session(case.partition(), "trace") == "drained"
    assert case.executor.prepare_calls == 1 and case.executor.send_calls == 1
    assert len(await case.history.get(case.partition())) == 2


async def test_validation_happens_before_any_history_read(worker_case):
    case = worker_case
    receipt = await case.accept()
    case.executor.validation_error = DeliveryFailure(
        definitely_not_sent=True, retryable=False, safe_code="invalid_target")
    assert await case.worker.process_session(case.partition(), "trace") == "drained"
    assert case.events == ["validate"]
    assert (await case.jobs.read(receipt.job_id)).state == "failed"
    assert await case.jobs.inbox.head(case.partition()) is None


async def test_acceptance_order_and_user_histories_are_preserved(worker_case):
    case = worker_case
    await case.accept("a1", "a")
    await case.accept("b1", "b")
    await case.accept("a2", "a")
    await case.worker.process_session(case.partition("b"), "later-wake-first")
    await case.worker.process_session(case.partition("a"), "earlier-wake-later")
    assert case.executor.sent == ["b1", "a1", "a2"]
    histories = dict(case.executor.histories)
    assert histories["a1"] == histories["b1"] == []
    assert [m["content"] for m in histories["a2"]] == ["question:a1", "answer:question:a1"]
    assert case.events.index("validate") < case.events.index("history")


async def test_interrupted_sending_is_never_replayed(worker_case, monkeypatch):
    case = worker_case
    receipt = await case.accept()

    async def interrupted(*args):
        raise asyncio.CancelledError()

    with monkeypatch.context() as patch:
        patch.setattr(case.executor, "send", interrupted)
        with pytest.raises(asyncio.CancelledError):
            await case.worker.process_session(case.partition(), "trace")
    assert (await case.jobs.read(receipt.job_id)).state == "sending"
    await case.worker.process_session(case.partition(), "trace")
    assert (await case.jobs.read(receipt.job_id)).state == "delivery_unknown"
    assert case.executor.prepare_calls == 1 and case.executor.send_calls == 0


async def test_ambiguous_send_is_terminal_without_retry(worker_case):
    case = worker_case
    receipt = await case.accept()
    case.executor.send_error = DeliveryFailure(
        definitely_not_sent=False, retryable=True, safe_code="send_timeout")
    await case.worker.process_session(case.partition(), "trace")
    await case.worker.process_session(case.partition(), "trace")
    assert case.executor.send_calls == 1
    assert (await case.jobs.read(receipt.job_id)).state == "delivery_unknown"


async def test_unsent_send_retry_reuses_result_and_respects_retry_after(worker_case):
    case = worker_case
    receipt = await case.accept()
    case.executor.send_error = DeliveryFailure(
        definitely_not_sent=True, retryable=True, safe_code="send_throttled",
        retry_after_seconds=120)
    assert await case.worker.process_session(case.partition(), "trace") == "rescheduled"
    written_at = (await case.jobs.read(receipt.job_id)).result_written_at
    assert await case.worker.process_session(case.partition(), "trace") == "rescheduled"
    assert case.executor.send_calls == 1
    case.clock.advance(seconds=120)
    case.executor.send_error = None
    assert await case.worker.process_session(case.partition(), "trace") == "drained"
    assert case.executor.prepare_calls == 1 and case.executor.send_calls == 2
    assert (await case.jobs.read(receipt.job_id)).result_written_at == written_at
    assert len(await case.history.get(case.partition())) == 2


@pytest.mark.parametrize("stage", ["prepare", "send"])
async def test_stage_attempts_are_bounded_and_do_not_block_later_jobs(worker_case, stage):
    case = worker_case
    first = await case.accept("first")
    failure = DeliveryFailure(definitely_not_sent=True, retryable=True, safe_code="unavailable")
    setattr(case.executor, f"{stage}_error", failure)
    for _ in range(3):
        await case.worker.process_session(case.partition(), "trace")
    assert (await case.jobs.read(first.job_id)).state == "failed"
    assert getattr(case.executor, f"{stage}_calls") == 3
    setattr(case.executor, f"{stage}_error", None)
    await case.accept("next")
    assert await case.worker.process_session(case.partition(), "trace") == "drained"
    assert case.executor.sent[-1] == "next"


async def test_recovered_running_retries_boundedly_without_teams_import(worker_case):
    case = worker_case
    receipt = await case.accept()
    async with case.jobs.leased_job(receipt.job_id) as job:
        await job.mark_running()
    await case.worker.process_session(case.partition(), "trace")
    record = await case.jobs.read(receipt.job_id)
    assert record.generation_attempts == 2 and record.state == "completed"


async def test_context_contention_does_not_consume_attempts_or_notifications(worker_case):
    case = worker_case
    receipt = await case.accept()
    async with case.history.leased(case.partition()):
        assert await case.worker.process_session(case.partition(), "trace") == "busy"
    assert (await case.jobs.read(receipt.job_id)).generation_attempts == 0
    assert await case.worker.process_session(case.partition(), "trace") == "drained"


async def test_real_context_lease_loss_prevents_send(worker_case, monkeypatch):
    case = worker_case
    await case.accept()
    prepare = case.executor.prepare

    async def lose_after_prepare(*args):
        result = await prepare(*args)
        name = (await case.history_gateway.list_page("context/")).names[0]
        await BlobLeaseClient(case.history_client.get_blob_client(name)).break_lease(
            lease_break_period=0)
        return result

    monkeypatch.setattr(case.executor, "prepare", lose_after_prepare)
    with pytest.raises(LeaseLost):
        await case.worker.process_session(case.partition(), "trace")
    assert case.executor.send_calls == 0


async def test_batch_cap_reliably_notifies_remaining_work(worker_case, monkeypatch):
    case = worker_case
    for number in range(5):
        await case.accept(str(number))
    notified = []
    notify = case.outbox.notify

    async def recorded(*args, **kwargs):
        notified.append(args)
        await notify(*args, **kwargs)

    monkeypatch.setattr(case.outbox, "notify", recorded)
    assert await case.worker.process_session(case.partition(), "trace") == "rescheduled"
    assert case.executor.send_calls == 4 and len(notified) == 1
    assert await case.worker.process_session(case.partition(), "trace") == "drained"
    assert case.executor.send_calls == 5


async def test_reschedule_failure_does_not_claim_drained(worker_case, monkeypatch):
    case = worker_case
    for number in range(5):
        await case.accept(str(number))

    async def fail(*args, **kwargs):
        raise ConnectionError("notification unavailable")

    monkeypatch.setattr(case.outbox, "notify", fail)
    with pytest.raises(ConnectionError):
        await case.worker.process_session(case.partition(), "trace")
    assert len(await case.jobs.pending(case.partition())) == 1


async def test_expired_job_is_not_recreated_when_payload_is_gone(worker_case):
    case = worker_case
    receipt = await case.accept()
    case.clock.advance(hours=24)
    name = f"payload/{receipt.job_id}.json"
    document = await case.jobs.gateway.read(name)
    await case.jobs.gateway.delete(name, etag=document.etag)
    assert await case.worker.process_session(case.partition(), "trace") == "drained"
    assert (await case.jobs.read(receipt.job_id)).state == "expired"
    assert case.executor.prepare_calls == case.executor.send_calls == 0


async def test_unexpired_missing_payload_is_explicit_failure_not_empty_history(worker_case, caplog):
    case = worker_case
    receipt = await case.accept()
    name = f"payload/{receipt.job_id}.json"
    document = await case.jobs.gateway.read(name)
    await case.jobs.gateway.delete(name, etag=document.etag)
    await case.worker.process_session(case.partition(), "trace")
    assert (await case.jobs.read(receipt.job_id)).failure_code == "invalid_payload"
    assert case.events == []
    assert any(record.message == "worker.payload_inconsistent" for record in caplog.records)


async def test_persistence_permission_failure_is_not_business_failure(worker_case, fail_next_operation):
    case = worker_case
    receipt = await case.accept()
    fail_next_operation("read", 403, "AuthorizationPermissionMismatch")
    with pytest.raises(HttpResponseError):
        await case.worker.process_session(case.partition(), "trace")
    assert (await case.jobs.read(receipt.job_id)).generation_attempts == 0
    assert case.executor.prepare_calls == 0


async def test_ignored_preparation_commits_no_history_and_sends_nothing(worker_case, monkeypatch):
    from advisor_shared.cloud import PreparedTurn
    case = worker_case
    receipt = await case.accept()

    async def ignore(*args):
        return PreparedTurn(reply=None, history=[], outcome="ignored")

    monkeypatch.setattr(case.executor, "prepare", ignore)
    assert await case.worker.process_session(case.partition(), "trace") == "drained"
    assert (await case.jobs.read(receipt.job_id)).state == "completed"
    assert case.executor.send_calls == 0
    assert await case.history.get(case.partition()) == []


async def test_persistently_failed_history_commit_has_a_bounded_stage_budget(worker_case, monkeypatch):
    case = worker_case
    receipt = await case.accept()
    write = case.history_gateway.write

    async def failed(name, *args, **kwargs):
        if name.startswith("context/"):
            raise ConnectionError("injected history storage failure")
        return await write(name, *args, **kwargs)

    monkeypatch.setattr(case.history_gateway, "write", failed)
    for _ in range(3):
        with pytest.raises(ConnectionError):
            await case.worker.process_session(case.partition(), "trace")
    assert await case.worker.process_session(case.partition(), "trace") == "drained"
    assert (await case.jobs.read(receipt.job_id)).failure_code == "history_failed"
    assert case.executor.prepare_calls == 1 and case.executor.send_calls == 0


async def test_duplicate_wake_can_retry_after_holder_cancellation(worker_case):
    case = worker_case
    await case.accept()
    await case.outbox.notify(case.partition(), "duplicate")
    delivery = [item async for item in case.queue.receive()][0]
    case.executor.pause = asyncio.Event()

    async def process():
        async with delivery:
            await case.worker.process_session(case.partition(), "trace")

    holder = asyncio.create_task(process())
    await asyncio.wait_for(case.executor.entered.wait(), 10)
    duplicate = [item async for item in case.queue.receive()][0]
    async with duplicate:
        assert await case.worker.process_session(case.partition(), "duplicate") == "busy"
        await duplicate.release(0)
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder
    case.executor.pause = None
    fresh = [item async for item in case.queue.receive()][0]
    async with fresh:
        assert await case.worker.process_session(case.partition(), "trace") == "drained"
        await fresh.ack()
    assert case.executor.send_calls == 1


async def test_accept_fixture_replays_transient_admission_without_a_new_sequence(
        worker_case, monkeypatch):
    case = worker_case
    submit = case.jobs.submit
    first = True

    async def acknowledgement_lost(payload):
        nonlocal first
        receipt = await submit(payload)
        if first:
            first = False
            raise LeaseLost("injected admission acknowledgement loss")
        return receipt

    monkeypatch.setattr(case.jobs, "submit", acknowledgement_lost)
    receipt = await case.accept()
    assert receipt.duplicate and receipt.sequence == 1
    assert len(await case.jobs.pending(case.partition())) == 1
