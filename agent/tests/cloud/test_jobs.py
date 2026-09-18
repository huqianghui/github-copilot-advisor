import asyncio
from datetime import timedelta

from azure.core.exceptions import HttpResponseError
import pytest

from advisor_agent.durable.jobs import JobConflict, JobExpired, JobStateError, make_job_id
from advisor_shared.cloud import PreparedTurn


def prepared():
    return PreparedTurn(reply={"type": "message", "text": "private-answer"}, history=[
        {"role": "user", "content": "private-question"},
        {"role": "assistant", "content": "private-answer"}], outcome="success")


async def test_duplicate_activity_gets_one_sequence(job_store, make_payload):
    payload = make_payload()
    receipts = await asyncio.gather(*(job_store.submit(payload) for _ in range(8)))
    assert {r.job_id for r in receipts} == {payload.job_id}
    assert {r.sequence for r in receipts} == {1}
    assert sum(not r.duplicate for r in receipts) == 1
    assert len(await job_store.pending(payload.partition_key)) == 1


async def test_users_and_bot_identity_are_independent(job_store, make_payload):
    a, b = make_payload(user="a"), make_payload(user="b")
    assert (await job_store.submit(a)).job_id != (await job_store.submit(b)).job_id
    assert a.job_id != make_job_id("other-bot", a.partition_key, a.activity["id"])
    assert [r.job_id for r in await job_store.pending(a.partition_key)] == [a.job_id]


async def test_inbox_preserves_acceptance_order_and_rejects_non_head_advance(job_store, make_payload):
    payloads = [make_payload(activity_id=str(i)) for i in range(3)]
    for item in payloads:
        await job_store.submit(item)
    partition = payloads[0].partition_key
    assert [r.sequence for r in await job_store.pending(partition)] == [1, 2, 3]
    with pytest.raises(JobConflict):
        await job_store.inbox.advance(partition, payloads[1].job_id)
    await job_store.inbox.advance(partition, payloads[0].job_id)
    await job_store.inbox.advance(partition, payloads[0].job_id)
    assert (await job_store.inbox.head(partition)).job_id == payloads[1].job_id


@pytest.mark.parametrize("fail_at", [1, 2, 3, 4])
async def test_submit_recovers_each_persisted_write_boundary(
        job_store, make_payload, blob_gateway, monkeypatch, fail_at):
    payload = make_payload()
    count = 0
    create, write = blob_gateway.create, blob_gateway.write

    def crash():
        nonlocal count
        count += 1
        if count == fail_at:
            raise ConnectionError("simulated lost acknowledgement")

    async def after_create(*args, **kwargs):
        result = await create(*args, **kwargs)
        crash()
        return result

    async def after_write(*args, **kwargs):
        result = await write(*args, **kwargs)
        crash()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(blob_gateway, "create", after_create)
        patch.setattr(blob_gateway, "write", after_write)
        with pytest.raises(ConnectionError):
            await job_store.submit(payload)
    receipt = await job_store.submit(payload)
    assert receipt.sequence == 1
    assert len(await job_store.pending(payload.partition_key)) == 1
    assert (await job_store.inbox.snapshot_outbox(payload.partition_key)).needs_notification


async def test_submission_does_not_wait_for_a_running_job_lease(job_store, make_payload):
    first, second = make_payload(), make_payload(activity_id="next")
    await job_store.submit(first)
    async with job_store.leased_job(first.job_id) as job:
        await job.mark_running()
        duplicate, receipt = await asyncio.wait_for(asyncio.gather(
            job_store.submit(first), job_store.submit(second)), 2)
        assert duplicate.sequence == 1 and receipt.sequence == 2


async def test_result_is_durable_before_send_and_terminal_body_is_removed(
        job_store, make_payload, blob_gateway, job_clock):
    payload = make_payload()
    await job_store.submit(payload)
    async with job_store.leased_job(payload.job_id) as job:
        assert job.sequence == 1 and job.state == "queued"
        assert job.payload.activity["text"] == "private-question"
        await job.mark_running()
        await job.save_result(prepared(), written_at=job_clock())
    record = await job_store.read(payload.job_id)
    assert record.result == prepared()
    assert record.result_written_at == job_clock()
    assert "private-" not in repr(record)
    async with job_store.leased_job(payload.job_id) as job:
        await job.mark_sending()
        await job.complete("response-id")
    record = await job_store.read(payload.job_id)
    assert record.state == "completed" and record.result is None
    assert record.generation_attempts == 1 and record.delivery_attempts == 1
    assert await blob_gateway.read(f"payload/{payload.job_id}.json") is None
    tombstone = await blob_gateway.read(f"terminal-metadata/{payload.job_id}.json")
    assert b"private-" not in tombstone.body
    assert (await job_store.submit(payload)).duplicate
    assert await job_store.pending(payload.partition_key) == []


@pytest.mark.parametrize("method,args", [
    ("save_result", (prepared(),)), ("mark_sending", ()), ("complete", ("response-id",)),
    ("mark_delivery_unknown", ("delivery_unknown",)),
])
async def test_invalid_transitions_do_not_change_persisted_state(
        job_store, make_payload, method, args):
    payload = make_payload()
    await job_store.submit(payload)
    async with job_store.leased_job(payload.job_id) as job:
        with pytest.raises(JobStateError):
            await getattr(job, method)(*args)
        assert job.state == "queued"


async def test_ambiguous_send_never_becomes_sendable_again(job_store, make_payload):
    payload = make_payload()
    await job_store.submit(payload)
    async with job_store.leased_job(payload.job_id) as job:
        await job.mark_running()
        await job.save_result(prepared())
        await job.mark_sending()
        with pytest.raises(JobStateError):
            await job.retry("delivery_failed")
        await job.mark_delivery_unknown("delivery_unknown")
        with pytest.raises(JobStateError):
            await job.mark_sending()
    assert (await job_store.read(payload.job_id)).state == "delivery_unknown"


async def test_generation_retry_preserves_attempts_and_respects_due_time(
        job_store, make_payload, job_clock):
    payload = make_payload()
    await job_store.submit(payload)
    async with job_store.leased_job(payload.job_id) as job:
        await job.mark_running()
        await job.retry("generation_failed", retry_at=job_clock() + timedelta(seconds=10))
        with pytest.raises(JobStateError):
            await job.mark_running()
    job_clock.advance(seconds=10)
    async with job_store.leased_job(payload.job_id) as job:
        await job.mark_running()
        assert job.record.generation_attempts == 2


async def test_confirming_old_generation_does_not_clear_new_arrivals(job_store, make_payload):
    first, second = make_payload(), make_payload(activity_id="next")
    await job_store.submit(first)
    snapshot = await job_store.inbox.snapshot_outbox(first.partition_key)
    await job_store.submit(second)
    await job_store.inbox.confirm_notified(first.partition_key, snapshot.generation)
    latest = await job_store.inbox.snapshot_outbox(first.partition_key)
    assert latest.needs_notification
    await job_store.inbox.confirm_notified(first.partition_key, latest.generation)
    await job_store.inbox.confirm_notified(first.partition_key, snapshot.generation)
    assert not (await job_store.inbox.snapshot_outbox(first.partition_key)).needs_notification


async def test_actual_job_lease_fences_writes_after_server_break(
        job_store, make_payload, expire_lease):
    from advisor_agent.durable.leases import LeaseLost
    payload = make_payload()
    await job_store.submit(payload)
    async with job_store.leased_job(payload.job_id) as job:
        await expire_lease(f"jobs/{payload.job_id}.json")
        with pytest.raises(LeaseLost):
            await job.mark_running()
    assert (await job_store.read(payload.job_id)).state == "queued"


async def test_expiry_removes_bodies_and_terminal_metadata_expires_after_seven_days(
        job_store, make_payload, job_clock, blob_gateway):
    payload = make_payload()
    await job_store.submit(payload)
    job_clock.advance(hours=24)
    with pytest.raises(JobExpired):
        await job_store.load_payload(payload.job_id)
    await job_store.prune()
    assert (await job_store.read(payload.job_id)).state == "expired"
    assert await blob_gateway.read(f"payload/{payload.job_id}.json") is None
    job_clock.advance(days=7)
    await job_store.prune()
    assert await job_store.read(payload.job_id) is None
    assert await job_store.inbox.head(payload.partition_key) is None
    assert (await job_store.submit(make_payload(activity_id="new"))).sequence == 2


async def test_terminal_cleanup_recovers_after_metadata_persist_failure(
        job_store, make_payload, blob_gateway, monkeypatch):
    payload = make_payload()
    await job_store.submit(payload)
    create = blob_gateway.create

    async def failed(name, body):
        if name.startswith("terminal-metadata/"):
            raise ConnectionError("injected")
        return await create(name, body)

    async with job_store.leased_job(payload.job_id) as job:
        await job.mark_running()
        await job.save_result(prepared())
        await job.mark_sending()
        with monkeypatch.context() as patch:
            patch.setattr(blob_gateway, "create", failed)
            with pytest.raises(ConnectionError):
                await job.complete("response-id")
    async with job_store.leased_job(payload.job_id) as job:
        assert job.state == "completed"
        await job.complete("response-id")
    assert await blob_gateway.read(f"payload/{payload.job_id}.json") is None


async def test_duplicate_different_body_is_not_silently_accepted(job_store, make_payload):
    await job_store.submit(make_payload())
    with pytest.raises(JobConflict):
        await job_store.submit(make_payload(text="different-private-body"))


async def test_service_failure_is_not_a_missing_job(job_store, make_payload, fail_next_operation):
    payload = make_payload()
    await job_store.submit(payload)
    fail_next_operation("read", 403, "AuthorizationPermissionMismatch")
    with pytest.raises(HttpResponseError):
        await job_store.read(payload.job_id)


async def test_unaccepted_partial_job_can_expire_without_becoming_executable(
        job_store, make_payload, blob_gateway, monkeypatch, job_clock):
    payload = make_payload()
    create = blob_gateway.create

    async def fail_inbox(name, body):
        if name.startswith("inbox/"):
            raise ConnectionError("injected")
        return await create(name, body)

    with monkeypatch.context() as patch:
        patch.setattr(blob_gateway, "create", fail_inbox)
        with pytest.raises(ConnectionError):
            await job_store.submit(payload)
    with pytest.raises(JobStateError):
        async with job_store.leased_job(payload.job_id):
            pytest.fail("unaccepted job entered worker")
    job_clock.advance(hours=24)
    await job_store.prune()
    assert (await job_store.read(payload.job_id)).state == "expired"
    with pytest.raises(JobExpired):
        await job_store.submit(payload)


async def test_bounded_retention_scan_can_resume_past_live_first_page(
        job_store, make_payload, job_clock):
    payloads = sorted([make_payload(activity_id=str(i)) for i in range(3)], key=lambda p: p.job_id)
    for payload in payloads:
        await job_store.submit(payload)
    job_clock.advance(hours=24)
    result = await job_store.prune(limit=1)
    assert result.cursors["jobs/"]
    result = await job_store.prune(limit=1, cursors=result.cursors)
    assert (await job_store.read(payloads[1].job_id)).state == "expired"


async def test_retention_does_not_start_or_wait_for_live_jobs(job_store, make_payload):
    payload = make_payload()
    await job_store.submit(payload)
    await job_store.prune()
    assert (await job_store.read(payload.job_id)).state == "pending_notify"
    async with job_store.leased_job(payload.job_id) as job:
        await job.mark_running()
        await asyncio.wait_for(job_store.prune(), 2)
        assert job.state == "running"


async def test_known_unsent_delivery_retry_preserves_checkpoint_and_provider_delay(
        job_store, make_payload, job_clock):
    from advisor_shared.delivery import DeliveryFailure
    payload = make_payload()
    await job_store.submit(payload)
    failure = DeliveryFailure(definitely_not_sent=True, retryable=True,
                              safe_code="send_throttled", retry_after_seconds=120)
    async with job_store.leased_job(payload.job_id) as job:
        await job.mark_running()
        await job.save_result(prepared())
        timestamp = job.record.result_written_at
        await job.mark_sending()
        await job.retry_delivery(failure, retry_at=job_clock() + timedelta(seconds=1))
        assert job.result == prepared()
        assert job.record.result_written_at == timestamp
        assert job.record.retry_at == job_clock() + timedelta(seconds=120)
        with pytest.raises(JobStateError):
            await job.mark_sending()
    job_clock.advance(seconds=120)
    async with job_store.leased_job(payload.job_id) as job:
        await job.mark_sending()
        assert job.record.delivery_attempts == 2
        assert job.record.generation_attempts == 1


@pytest.mark.parametrize("method", ["retry_delivery", "fail_delivery"])
async def test_uncertain_send_never_accepts_a_retry_or_failure_proof(
        job_store, make_payload, method):
    from advisor_shared.delivery import DeliveryFailure
    payload = make_payload()
    await job_store.submit(payload)
    async with job_store.leased_job(payload.job_id) as job:
        await job.mark_running()
        await job.save_result(prepared())
        await job.mark_sending()
        with pytest.raises(JobStateError):
            await getattr(job, method)(DeliveryFailure(
                definitely_not_sent=False, retryable=True, safe_code="send_timeout"))
        assert job.state == "sending"


async def test_definitive_delivery_rejection_can_fail_terminally(job_store, make_payload):
    from advisor_shared.delivery import DeliveryFailure
    payload = make_payload()
    await job_store.submit(payload)
    async with job_store.leased_job(payload.job_id) as job:
        await job.mark_running()
        await job.save_result(prepared())
        await job.mark_sending()
        await job.fail_delivery(DeliveryFailure(
            definitely_not_sent=True, retryable=False, safe_code="send_rejected"))
    assert (await job_store.read(payload.job_id)).state == "failed"


async def test_prune_uses_one_total_budget_across_prefixes(job_store, make_payload, blob_gateway):
    payload = make_payload()
    await job_store.submit(payload)
    result = await job_store.prune(limit=1)
    assert result.scanned == 1
    assert result.cursor is not None
    next_result = await job_store.prune(limit=1, cursor=result.cursor)
    assert next_result.scanned <= 1
    assert await blob_gateway.read(f"payload/{payload.job_id}.json") is not None


async def test_expired_payload_is_not_deleted_while_its_job_lease_is_active(
        job_store, make_payload, blob_gateway, job_clock):
    payload = make_payload()
    await job_store.submit(payload)
    async with job_store.leased_job(payload.job_id) as job:
        await job.mark_running()
        job_clock.advance(hours=25)
        result = await job_store.prune()
        assert result.skipped >= 1
        assert await blob_gateway.read(f"payload/{payload.job_id}.json") is not None
        assert (await job_store.read(payload.job_id)).state == "running"
