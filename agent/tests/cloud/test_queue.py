import asyncio
from urllib.parse import parse_qs, urlsplit

from azure.core.exceptions import HttpResponseError
import pytest

from advisor_agent.durable import queue as queue_module
from advisor_agent.durable.queue import DeliveryLost, OutboxDispatcher, SessionQueue, decode_wake
from advisor_shared.cloud import QueueWake


async def receive_one(queue):
    values = [delivery async for delivery in queue.receive()]
    assert len(values) == 1
    return values[0]


@pytest.fixture
def visibility_tick(monkeypatch):
    entered, tick, intervals = asyncio.Event(), asyncio.Event(), []

    async def sleep(seconds):
        intervals.append(seconds)
        entered.set()
        await tick.wait()
        tick.clear()

    monkeypatch.setattr(queue_module, "sleep", sleep)
    return entered, tick, intervals


async def test_utf8_wake_is_plain_json_and_has_twenty_four_hour_ttl(queue_harness):
    queue, client, transport = queue_harness
    wake = QueueWake(partition_key="用户会话", trace_id="中文追踪")
    await queue.publish(wake)
    peeked = await client.peek_messages()
    assert peeked[0].content == wake.model_dump_json()
    assert "用户会话" in peeked[0].content
    send = next(req for op, req in transport.calls if op == "send")
    assert parse_qs(urlsplit(send.url).query)["messagettl"] == ["86400"]
    delivery = await receive_one(queue)
    assert delivery.body == wake.model_dump_json().encode("utf-8")
    assert decode_wake(delivery.body) == wake
    await delivery.ack()


async def test_oversized_utf8_wake_never_reaches_transport(queue_harness):
    queue, _, transport = queue_harness
    with pytest.raises(ValueError, match="64"):
        await queue.publish(QueueWake(partition_key="汉" * 23000, trace_id="trace"))
    assert not [req for op, req in transport.calls if op == "send"]


async def test_injected_base64_policies_are_rejected(queue_harness):
    from azure.storage.queue import TextBase64EncodePolicy
    _, client, _ = queue_harness
    original = client._message_encode_policy
    client._message_encode_policy = TextBase64EncodePolicy()
    try:
        with pytest.raises(ValueError, match="encoding"):
            SessionQueue(client)
    finally:
        client._message_encode_policy = original


async def test_visibility_renewal_rotates_receipt_used_for_ack(queue_harness):
    queue, client, transport = queue_harness
    await queue.publish(QueueWake(partition_key="p", trace_id="t"))
    delivery = await receive_one(queue)
    old = delivery.pop_receipt
    await delivery.renew_visibility()
    assert delivery.pop_receipt != old
    await delivery.ack()
    assert await client.peek_messages() == []
    ack = next(req for op, req in transport.calls if op == "ack")
    assert parse_qs(urlsplit(ack.url).query)["popreceipt"] == [delivery.pop_receipt]


async def test_previous_receipt_cannot_delete_or_renew_new_owners_message(queue_harness):
    queue, client, _ = queue_harness
    await queue.publish(QueueWake(partition_key="p", trace_id="t"))
    old = await receive_one(queue)
    await client.update_message(old.id, pop_receipt=old.pop_receipt, visibility_timeout=0)
    new = await receive_one(queue)
    with pytest.raises(DeliveryLost):
        await old.ack()
    with pytest.raises(DeliveryLost):
        await old.renew_visibility()
    await new.ack()
    assert await client.peek_messages() == []


async def test_release_requeues_but_does_not_ack(queue_harness):
    queue, _, transport = queue_harness
    await queue.publish(QueueWake(partition_key="p", trace_id="t"))
    delivery = await receive_one(queue)
    await delivery.release(0)
    with pytest.raises(DeliveryLost):
        await delivery.ack()
    assert not [req for op, req in transport.calls if op == "ack"]
    new = await receive_one(queue)
    await new.ack()


async def test_context_never_auto_acks_and_renewal_runs_every_twenty_seconds(
        queue_harness, visibility_tick):
    queue, _, transport = queue_harness
    entered, tick, intervals = visibility_tick
    await queue.publish(QueueWake(partition_key="p", trace_id="t"))
    delivery = await receive_one(queue)
    async with delivery:
        await asyncio.wait_for(entered.wait(), 2)
        entered.clear()
        old = delivery.pop_receipt
        tick.set()
        await asyncio.wait_for(entered.wait(), 2)
        assert delivery.pop_receipt != old
    assert intervals == [20, 20]
    assert not [req for op, req in transport.calls if op == "ack"]
    received = next(req for op, req in transport.calls if op == "receive")
    assert parse_qs(urlsplit(received.url).query)["visibilitytimeout"] == ["60"]
    await delivery.release(0)


async def test_visibility_failure_cancels_processing_without_ack(
        queue_harness, visibility_tick):
    queue, _, transport = queue_harness
    entered, tick, _ = visibility_tick
    await queue.publish(QueueWake(partition_key="p", trace_id="t"))
    delivery = await receive_one(queue)
    with pytest.raises(DeliveryLost):
        async with delivery:
            await asyncio.wait_for(entered.wait(), 2)
            transport.failures.append(("update", 503, "ServerBusy"))
            tick.set()
            await asyncio.wait_for(asyncio.Event().wait(), 2)
    assert delivery.lost.is_set()
    assert asyncio.current_task().cancelling() == 0
    assert not [req for op, req in transport.calls if op == "ack"]


async def test_external_cancellation_is_not_swallowed_or_acknowledged(
        queue_harness, visibility_tick):
    queue, _, transport = queue_harness
    entered, _, _ = visibility_tick
    await queue.publish(QueueWake(partition_key="p", trace_id="t"))
    delivery = await receive_one(queue)

    async def work():
        async with delivery:
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not [req for op, req in transport.calls if op == "ack"]


async def test_ack_waits_for_renewal_and_uses_new_receipt(queue_harness, monkeypatch):
    queue, client, transport = queue_harness
    await queue.publish(QueueWake(partition_key="p", trace_id="t"))
    delivery = await receive_one(queue)
    updating, proceed = asyncio.Event(), asyncio.Event()
    send = transport.send

    async def delayed(request, **kwargs):
        if request.method == "PUT" and "/messages/" in request.url:
            updating.set()
            await proceed.wait()
        return await send(request, **kwargs)

    monkeypatch.setattr(transport, "send", delayed)
    renewal = asyncio.create_task(delivery.renew_visibility())
    await asyncio.wait_for(updating.wait(), 2)
    ack = asyncio.create_task(delivery.ack())
    await asyncio.sleep(0)
    assert not ack.done()
    proceed.set()
    await asyncio.gather(renewal, ack)
    assert await client.peek_messages() == []


async def test_failed_publish_never_confirms_outbox(job_store, make_payload, queue_harness):
    queue, _, transport = queue_harness
    payload = make_payload()
    await job_store.submit(payload)
    dispatcher = OutboxDispatcher(job_store.inbox, queue)
    transport.failures.append(("send", 503, "ServerBusy"))
    with pytest.raises(HttpResponseError):
        await dispatcher.notify(payload.partition_key, payload.trace_id)
    assert (await job_store.inbox.snapshot_outbox(payload.partition_key)).needs_notification
    assert await dispatcher.repair_pending() == 1
    assert not (await job_store.inbox.snapshot_outbox(payload.partition_key)).needs_notification


async def test_crash_after_publish_only_adds_duplicate_safe_wakes(
        job_store, make_payload, queue_harness, monkeypatch):
    queue, client, _ = queue_harness
    payload = make_payload()
    await job_store.submit(payload)
    dispatcher = OutboxDispatcher(job_store.inbox, queue)

    async def crash(*args):
        raise ConnectionError("confirmation interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(job_store.inbox, "confirm_notified", crash)
        with pytest.raises(ConnectionError):
            await dispatcher.notify(payload.partition_key, payload.trace_id)
    assert await dispatcher.repair_pending() == 1
    wakes = await client.peek_messages(max_messages=8)
    assert len(wakes) == 2
    for message in wakes:
        decoded = decode_wake(message.content.encode("utf-8"))
        assert decoded.partition_key == payload.partition_key
        assert "private-question" not in message.content
        assert "activity" not in message.content
    assert len(await job_store.pending(payload.partition_key)) == 1


async def test_arrival_during_publish_is_not_cleared_by_old_confirmation(
        job_store, make_payload, queue_harness, monkeypatch):
    queue, _, _ = queue_harness
    first, second = make_payload(), make_payload(activity_id="new")
    await job_store.submit(first)
    publish = queue.publish

    async def arrival(wake):
        await publish(wake)
        await job_store.submit(second)

    with monkeypatch.context() as patch:
        patch.setattr(queue, "publish", arrival)
        await OutboxDispatcher(job_store.inbox, queue).notify(first.partition_key, first.trace_id)
    assert (await job_store.inbox.snapshot_outbox(first.partition_key)).needs_notification
    assert await OutboxDispatcher(job_store.inbox, queue).repair_pending() == 1


async def test_explicit_reschedule_can_publish_even_already_confirmed_pending(
        job_store, make_payload, queue_harness):
    queue, client, _ = queue_harness
    payload = make_payload()
    await job_store.submit(payload)
    dispatcher = OutboxDispatcher(job_store.inbox, queue)
    await dispatcher.notify(payload.partition_key, payload.trace_id)
    assert not (await job_store.inbox.snapshot_outbox(payload.partition_key)).needs_notification
    await dispatcher.notify(payload.partition_key, payload.trace_id)
    assert len(await client.peek_messages(max_messages=8)) == 2


@pytest.mark.parametrize("operation", ["send", "receive", "update", "ack"])
async def test_permission_failures_are_never_silently_acknowledged(queue_harness, operation):
    queue, _, transport = queue_harness
    wake = QueueWake(partition_key="p", trace_id="t")
    await queue.publish(wake)
    delivery = await receive_one(queue) if operation in ("update", "ack") else None
    transport.failures.append((operation, 403, "AuthorizationPermissionMismatch"))
    with pytest.raises(HttpResponseError) as error:
        if operation == "send":
            await queue.publish(wake)
        elif operation == "receive":
            await receive_one(queue)
        elif operation == "update":
            await delivery.renew_visibility()
        else:
            await delivery.ack()
    assert error.value.status_code == 403


async def test_exact_64_kib_utf8_limit_is_enforced_before_network(queue_harness):
    queue, _, transport = queue_harness
    empty = QueueWake(partition_key="p", trace_id="t")
    overhead = len(empty.model_dump_json().encode("utf-8")) - 1
    boundary = QueueWake(partition_key="x" * (65536 - overhead), trace_id="t")
    assert len(boundary.model_dump_json().encode("utf-8")) == 65536
    await queue.publish(boundary)
    sent = len([req for op, req in transport.calls if op == "send"])
    with pytest.raises(ValueError):
        await queue.publish(boundary.model_copy(update={"partition_key": boundary.partition_key + "x"}))
    assert len([req for op, req in transport.calls if op == "send"]) == sent


async def test_ready_visibility_heartbeat_does_not_race_normal_exit(
        session_queue, visibility_tick):
    entered, tick, _ = visibility_tick
    await session_queue.publish(QueueWake(partition_key="p", trace_id="t"))
    delivery = await receive_one(session_queue)
    async with delivery:
        await asyncio.wait_for(entered.wait(), 2)
        tick.set()
    assert not delivery.lost.is_set()
    await delivery.ack()


async def test_cancelled_heartbeat_request_stops_work(queue_harness, visibility_tick, monkeypatch):
    queue, _, transport = queue_harness
    entered, tick, _ = visibility_tick
    await queue.publish(QueueWake(partition_key="p", trace_id="t"))
    delivery = await receive_one(queue)
    send = transport.send

    async def cancelled(request, **kwargs):
        if request.method == "PUT" and "/messages/" in request.url:
            raise asyncio.CancelledError()
        return await send(request, **kwargs)

    monkeypatch.setattr(transport, "send", cancelled)
    with pytest.raises(DeliveryLost):
        async with delivery:
            await asyncio.wait_for(entered.wait(), 2)
            tick.set()
            await asyncio.wait_for(asyncio.Event().wait(), 2)
    assert delivery.lost.is_set()


async def test_expired_local_visibility_never_attempts_ack(queue_harness, monkeypatch):
    queue, _, transport = queue_harness
    await queue.publish(QueueWake(partition_key="p", trace_id="t"))
    delivery = await receive_one(queue)
    monkeypatch.setattr(queue_module, "monotonic", lambda: float("inf"))
    with pytest.raises(DeliveryLost):
        await delivery.ack()
    assert not [req for op, req in transport.calls if op == "ack"]


async def test_external_cancel_wins_over_simultaneous_visibility_failure(
        queue_harness, visibility_tick, monkeypatch):
    queue, _, transport = queue_harness
    entered, tick, _ = visibility_tick
    await queue.publish(QueueWake(partition_key="p", trace_id="t"))
    delivery = await receive_one(queue)
    send = transport.send
    owner = None

    async def failed(request, **kwargs):
        response = await send(request, **kwargs)
        if request.method == "PUT" and "/messages/" in request.url:
            owner.cancel()
        return response

    monkeypatch.setattr(transport, "send", failed)

    async def work():
        async with delivery:
            await asyncio.Event().wait()

    owner = asyncio.create_task(work())
    await asyncio.wait_for(entered.wait(), 2)
    transport.failures.append(("update", 503, "ServerBusy"))
    tick.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(owner, 2)


async def test_broken_diagnostics_do_not_keep_a_lost_delivery_running(
        queue_harness, visibility_tick, monkeypatch):
    queue, _, transport = queue_harness
    entered, tick, _ = visibility_tick
    await queue.publish(QueueWake(partition_key="p", trace_id="t"))
    delivery = await receive_one(queue)

    def broken_log(*args, **kwargs):
        raise RuntimeError("diagnostic sink failed")

    monkeypatch.setattr(queue_module.logger, "warning", broken_log)
    with pytest.raises(DeliveryLost):
        async with delivery:
            await asyncio.wait_for(entered.wait(), 2)
            transport.failures.append(("update", 503, "ServerBusy"))
            tick.set()
            await asyncio.wait_for(asyncio.Event().wait(), 2)


async def test_outbox_repair_is_paged_and_resumable(job_store, make_payload, session_queue):
    for user in ("a", "b", "c"):
        await job_store.submit(make_payload(user=user))
    dispatcher = OutboxDispatcher(job_store.inbox, session_queue)
    first = await dispatcher.repair_page(limit=1)
    assert first.scanned == 1 and first.republished == 1
    assert first.next_cursor
    second = await dispatcher.repair_page(cursor=first.next_cursor, limit=1)
    assert second.scanned == 1 and second.republished == 1
    assert await dispatcher.repair_pending() == 1


async def test_retry_wake_is_not_visible_before_its_requested_delay(session_queue):
    await session_queue.publish(QueueWake(partition_key="p", trace_id="t"), delay_seconds=30)
    assert [delivery async for delivery in session_queue.receive()] == []
