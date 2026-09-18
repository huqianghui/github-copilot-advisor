import asyncio

from azure.core.exceptions import HttpResponseError
import pytest

from advisor_agent.durable import leases
from advisor_agent.durable.blob import BlobConflict
from advisor_agent.durable.leases import LeaseBusy, LeaseLost


@pytest.fixture
def renewal_tick(monkeypatch):
    entered, tick = asyncio.Event(), asyncio.Event()
    intervals = []

    async def wait(seconds):
        intervals.append(seconds)
        entered.set()
        await tick.wait()
        tick.clear()
    monkeypatch.setattr(leases, "sleep", wait)
    return entered, tick, intervals


async def test_lease_fences_the_actual_mutable_blob(blob_gateway, storage_harness):
    await blob_gateway.create("context/a", b"one")
    first = await blob_gateway.read("context/a")
    async with blob_gateway.lease("context/a") as guard:
        guard.assert_active()
        assert not guard.lost.is_set()
        with pytest.raises(LeaseLost):
            await blob_gateway.write("context/a", b"unfenced", etag=first.etag)
        etag = await blob_gateway.write("context/a", b"two", etag=first.etag, lease_id=guard.id)
        await blob_gateway.delete("context/a", etag=etag, lease_id=guard.id)
    assert await blob_gateway.read("context/a") is None
    acquire = next(req for op, req in storage_harness[2].calls if op == "acquire")
    assert acquire.url.split("?")[0].endswith("/context/a")
    assert acquire.headers["x-ms-lease-duration"] == "60"
    with pytest.raises(LeaseLost):
        guard.assert_active()


async def test_cannot_acquire_while_another_owner_holds_lease(blob_gateway):
    await blob_gateway.create("context/a", b"one")
    async with blob_gateway.lease("context/a"):
        with pytest.raises(LeaseBusy):
            async with blob_gateway.lease("context/a"):
                pytest.fail("competing owner entered")
    async with blob_gateway.lease("context/a") as later:
        later.assert_active()


async def test_marker_lease_cannot_authorize_other_objects(blob_gateway):
    await blob_gateway.create("lock/a", b"marker")
    await blob_gateway.create("context/a", b"one")
    first = await blob_gateway.read("context/a")
    async with blob_gateway.lease("lock/a") as marker:
        with pytest.raises(LeaseLost):
            await blob_gateway.write("context/a", b"stale", etag=first.etag, lease_id=marker.id)
    assert (await blob_gateway.read("context/a")).body == b"one"


async def test_old_owner_cannot_write_after_lease_loss(blob_gateway, expire_lease):
    await blob_gateway.create("context/a", b"one")
    async with blob_gateway.lease("context/a") as old:
        first = await blob_gateway.read("context/a")
        await expire_lease("context/a")
        async with blob_gateway.lease("context/a") as replacement:
            assert replacement.id != old.id
            with pytest.raises(LeaseLost):
                await blob_gateway.write("context/a", b"stale",
                                         etag=first.etag, lease_id=old.id)
            with pytest.raises(LeaseLost):
                await blob_gateway.delete("context/a", etag=first.etag, lease_id=old.id)
            await blob_gateway.write("context/a", b"new", etag=first.etag, lease_id=replacement.id)
    assert (await blob_gateway.read("context/a")).body == b"new"


async def test_renewal_uses_twenty_second_interval_and_keeps_same_lease(
        blob_gateway, storage_harness, renewal_tick):
    entered, tick, intervals = renewal_tick
    await blob_gateway.create("context/a", b"one")
    async with blob_gateway.lease("context/a") as guard:
        await asyncio.wait_for(entered.wait(), 2)
        entered.clear()
        tick.set()
        await asyncio.wait_for(entered.wait(), 2)
        guard.assert_active()
        first = await blob_gateway.read("context/a")
        await blob_gateway.write("context/a", b"two", etag=first.etag, lease_id=guard.id)
    assert intervals == [20, 20]
    renew = [req for op, req in storage_harness[2].calls if op == "renew"]
    assert len(renew) == 1
    assert renew[0].headers["x-ms-lease-id"] == guard.id


@pytest.mark.parametrize("status,code", [
    (403, "AuthorizationPermissionMismatch"), (503, "ServerBusy"),
    (409, "LeaseIdMismatchWithLeaseOperation"),
])
async def test_renewal_failure_cancels_work_and_marks_lost(
        blob_gateway, fail_next_operation, renewal_tick, status, code):
    entered, tick, _ = renewal_tick
    await blob_gateway.create("context/a", b"one")
    interrupted = False
    with pytest.raises(LeaseLost):
        async with blob_gateway.lease("context/a") as guard:
            await asyncio.wait_for(entered.wait(), 2)
            fail_next_operation("renew", status, code)
            tick.set()
            try:
                await asyncio.wait_for(asyncio.Event().wait(), 2)
            finally:
                interrupted = True
    assert interrupted and guard.lost.is_set()
    assert asyncio.current_task().cancelling() == 0
    with pytest.raises(LeaseLost):
        guard.assert_active()
    async with blob_gateway.lease("context/a") as next_owner:
        next_owner.assert_active()


async def test_actual_server_break_is_detected_by_renewal(
        blob_gateway, expire_lease, renewal_tick):
    entered, tick, _ = renewal_tick
    await blob_gateway.create("context/a", b"one")
    with pytest.raises(LeaseLost):
        async with blob_gateway.lease("context/a") as guard:
            await asyncio.wait_for(entered.wait(), 2)
            await expire_lease("context/a")
            tick.set()
            await asyncio.wait_for(asyncio.Event().wait(), 2)
    assert guard.lost.is_set()


async def test_external_cancellation_is_preserved_and_lease_released(blob_gateway):
    await blob_gateway.create("context/a", b"one")
    entered = asyncio.Event()

    async def work():
        async with blob_gateway.lease("context/a"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with blob_gateway.lease("context/a") as guard:
        guard.assert_active()


async def test_body_failure_is_not_masked_by_release_failure(blob_gateway, fail_next_operation):
    await blob_gateway.create("context/a", b"one")
    with pytest.raises(ValueError, match="business failure"):
        async with blob_gateway.lease("context/a"):
            fail_next_operation("release", 503, "ServerBusy")
            raise ValueError("business failure")


async def test_release_failure_on_normal_exit_is_visible(blob_gateway, fail_next_operation):
    await blob_gateway.create("context/a", b"one")
    with pytest.raises(HttpResponseError):
        async with blob_gateway.lease("context/a"):
            fail_next_operation("release", 403, "AuthorizationPermissionMismatch")


@pytest.mark.parametrize("status,code", [
    (403, "AuthorizationPermissionMismatch"), (503, "ServerBusy"),
    (409, "ContainerBeingDeleted"), (404, "BlobNotFound"),
])
async def test_acquire_errors_other_than_contention_are_visible(
        blob_gateway, fail_next_operation, status, code):
    await blob_gateway.create("context/a", b"one")
    fail_next_operation("acquire", status, code)
    with pytest.raises(HttpResponseError) as error:
        async with blob_gateway.lease("context/a"):
            pytest.fail("owner entered after acquisition failure")
    assert error.value.status_code == status


async def test_cancellation_during_release_waits_for_cleanup(storage_harness, monkeypatch):
    gateway, _, transport = storage_harness
    await gateway.create("context/a", b"one")
    releasing, proceed = asyncio.Event(), asyncio.Event()
    send = transport.send

    async def delayed(request, **kwargs):
        if request.headers.get("x-ms-lease-action") == "release":
            releasing.set()
            await proceed.wait()
        return await send(request, **kwargs)

    monkeypatch.setattr(transport, "send", delayed)

    async def work():
        async with gateway.lease("context/a"):
            pass

    task = asyncio.create_task(work())
    await asyncio.wait_for(releasing.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with gateway.lease("context/a") as guard:
        guard.assert_active()


async def test_external_cancellation_wins_over_simultaneous_renewal_loss(
        storage_harness, fail_next_operation, renewal_tick, monkeypatch):
    gateway, _, transport = storage_harness
    entered, tick, _ = renewal_tick
    await gateway.create("context/a", b"one")
    send = transport.send
    owner = None

    async def failing(request, **kwargs):
        response = await send(request, **kwargs)
        if request.headers.get("x-ms-lease-action") == "renew":
            owner.cancel()
        return response

    monkeypatch.setattr(transport, "send", failing)

    async def work():
        async with gateway.lease("context/a"):
            await asyncio.Event().wait()

    owner = asyncio.create_task(work())
    await asyncio.wait_for(entered.wait(), 2)
    fail_next_operation("renew", 403, "AuthorizationPermissionMismatch")
    tick.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(owner, 2)


async def test_stuck_renewal_cannot_outlive_the_lease_safety_window(
        storage_harness, renewal_tick, monkeypatch):
    gateway, _, transport = storage_harness
    entered, tick, _ = renewal_tick
    await gateway.create("context/a", b"one")
    monkeypatch.setattr(leases, "_RENEW_TIMEOUT_SECONDS", 0.01)
    send = transport.send

    async def stalled(request, **kwargs):
        if request.headers.get("x-ms-lease-action") == "renew":
            await asyncio.Event().wait()
        return await send(request, **kwargs)

    monkeypatch.setattr(transport, "send", stalled)
    with pytest.raises(LeaseLost):
        async with gateway.lease("context/a") as guard:
            await asyncio.wait_for(entered.wait(), 2)
            tick.set()
            await asyncio.wait_for(asyncio.Event().wait(), 2)
    assert guard.lost.is_set()


async def test_assert_active_rejects_an_expired_local_deadline(blob_gateway, monkeypatch):
    await blob_gateway.create("context/a", b"one")
    with pytest.raises(LeaseLost):
        async with blob_gateway.lease("context/a") as guard:
            monkeypatch.setattr(leases, "monotonic", lambda: float("inf"))
            guard.assert_active()


async def test_cancelled_acquire_releases_a_lease_already_granted_by_server(
        storage_harness, monkeypatch):
    gateway, _, transport = storage_harness
    await gateway.create("context/a", b"one")
    granted = asyncio.Event()
    send = transport.send

    async def delayed(request, **kwargs):
        response = await send(request, **kwargs)
        if request.headers.get("x-ms-lease-action") == "acquire" and not granted.is_set():
            granted.set()
            await asyncio.Event().wait()
        return response

    monkeypatch.setattr(transport, "send", delayed)

    async def work():
        async with gateway.lease("context/a"):
            pytest.fail("cancelled acquisition entered")

    task = asyncio.create_task(work())
    await asyncio.wait_for(granted.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with gateway.lease("context/a") as guard:
        guard.assert_active()


async def test_normal_exit_cancels_a_ready_heartbeat_without_false_lease_loss(
        blob_gateway, renewal_tick):
    entered, tick, _ = renewal_tick
    await blob_gateway.create("context/a", b"one")
    async with blob_gateway.lease("context/a") as guard:
        await asyncio.wait_for(entered.wait(), 2)
        tick.set()
    assert not guard.lost.is_set()


async def test_cancelled_release_does_not_spin_on_a_finished_cleanup_task(
        storage_harness, monkeypatch):
    gateway, _, transport = storage_harness
    await gateway.create("context/a", b"one")
    send, shield = transport.send, asyncio.shield
    retries = 0

    async def cancelled_release(request, **kwargs):
        if request.headers.get("x-ms-lease-action") == "release":
            raise asyncio.CancelledError()
        return await send(request, **kwargs)

    def bounded_shield(task):
        nonlocal retries
        if task.get_name() == "blob-lease-cleanup" and task.cancelled():
            retries += 1
            if retries > 1:
                pytest.fail("cleanup kept retrying a finished cancelled task")
        return shield(task)

    monkeypatch.setattr(transport, "send", cancelled_release)
    monkeypatch.setattr(asyncio, "shield", bounded_shield)
    with pytest.raises(asyncio.CancelledError):
        async with gateway.lease("context/a"):
            pass


async def test_acquisition_cannot_enter_after_its_conservative_deadline(
        storage_harness, monkeypatch):
    gateway, _, transport = storage_harness
    await gateway.create("context/a", b"one")
    now = [0.0]
    send = transport.send
    monkeypatch.setattr(leases, "monotonic", lambda: now[0])

    async def delayed_acquisition(request, **kwargs):
        response = await send(request, **kwargs)
        if request.headers.get("x-ms-lease-action") == "acquire":
            now[0] = 61.0
        return response

    monkeypatch.setattr(transport, "send", delayed_acquisition)
    with pytest.raises(LeaseLost):
        async with gateway.lease("context/a"):
            pytest.fail("lease already exceeded the conservative deadline")
    monkeypatch.setattr(transport, "send", send)
    async with gateway.lease("context/a") as guard:
        guard.assert_active()


async def test_lease_and_etag_preconditions_are_both_required(blob_gateway):
    await blob_gateway.create("context/a", b"one")
    async with blob_gateway.lease("context/a") as guard:
        first = await blob_gateway.read("context/a")
        await blob_gateway.write("context/a", b"two", etag=first.etag, lease_id=guard.id)
        with pytest.raises(BlobConflict):
            await blob_gateway.write("context/a", b"stale", etag=first.etag, lease_id=guard.id)
        with pytest.raises(BlobConflict):
            await blob_gateway.delete("context/a", etag=first.etag, lease_id=guard.id)
        guard.assert_active()
    assert (await blob_gateway.read("context/a")).body == b"two"


async def test_heartbeat_wait_never_exceeds_remaining_lease_time(
        storage_harness, renewal_tick, monkeypatch):
    gateway, _, transport = storage_harness
    entered, tick, intervals = renewal_tick
    await gateway.create("context/a", b"one")
    now = [0.0]
    send = transport.send
    monkeypatch.setattr(leases, "monotonic", lambda: now[0])

    async def delayed(request, **kwargs):
        response = await send(request, **kwargs)
        if request.headers.get("x-ms-lease-action") == "acquire":
            now[0] = 55.0
        return response

    monkeypatch.setattr(transport, "send", delayed)
    with pytest.raises(LeaseLost):
        async with gateway.lease("context/a") as guard:
            await asyncio.wait_for(entered.wait(), 2)
            assert intervals == [5]
            now[0] = 60.0
            tick.set()
            await asyncio.wait_for(asyncio.Event().wait(), 2)
    assert guard.lost.is_set()


async def test_renewal_request_timeout_cannot_cross_lease_deadline(
        blob_gateway, renewal_tick, monkeypatch):
    entered, tick, _ = renewal_tick
    now, timeouts = [0.0], []
    timeout = asyncio.timeout
    monkeypatch.setattr(leases, "monotonic", lambda: now[0])

    def record_timeout(seconds):
        timeouts.append(seconds)
        return timeout(seconds)

    monkeypatch.setattr(asyncio, "timeout", record_timeout)
    await blob_gateway.create("context/a", b"one")
    async with blob_gateway.lease("context/a"):
        await asyncio.wait_for(entered.wait(), 2)
        entered.clear()
        now[0] = 55.0
        tick.set()
        await asyncio.wait_for(entered.wait(), 2)
    assert timeouts[0] == 5


async def test_unexpected_renewal_cancellation_also_stops_protected_work(
        storage_harness, renewal_tick, monkeypatch):
    gateway, _, transport = storage_harness
    entered, tick, _ = renewal_tick
    await gateway.create("context/a", b"one")
    send = transport.send

    async def cancelled_renew(request, **kwargs):
        if request.headers.get("x-ms-lease-action") == "renew":
            raise asyncio.CancelledError()
        return await send(request, **kwargs)

    monkeypatch.setattr(transport, "send", cancelled_renew)
    with pytest.raises(LeaseLost):
        async with gateway.lease("context/a") as guard:
            await asyncio.wait_for(entered.wait(), 2)
            tick.set()
            await asyncio.wait_for(asyncio.Event().wait(), 2)
    assert guard.lost.is_set()


@pytest.mark.parametrize("operation", ["renew", "release"])
@pytest.mark.parametrize("code,expected", [
    ("AuthorizationPermissionMismatch", "AuthorizationPermissionMismatch"),
    ("https://private.invalid/?token=private-token", "Unknown"),
])
async def test_failures_log_only_safe_metadata_and_preserve_primary_error(
        storage_harness, renewal_tick, monkeypatch, caplog, operation, code, expected):
    from advisor_shared.logging import TelemetryFormatter

    gateway, _, transport = storage_harness
    entered, tick, _ = renewal_tick
    await gateway.create("context/a", b"private-body")
    send = transport.send
    failure = HttpResponseError("private-exception https://private.invalid/?token=private-token")
    failure.status_code = 403
    failure.error_code = code

    async def broken(request, **kwargs):
        if request.headers.get("x-ms-lease-action") == operation:
            raise failure
        return await send(request, **kwargs)

    monkeypatch.setattr(transport, "send", broken)
    with pytest.raises(LeaseLost if operation == "renew" else ValueError):
        async with gateway.lease("context/a"):
            if operation == "renew":
                await asyncio.wait_for(entered.wait(), 2)
                tick.set()
                await asyncio.wait_for(asyncio.Event().wait(), 2)
            else:
                raise ValueError("primary failure")
    records = [r for r in caplog.records if r.name == leases.__name__]
    assert len(records) == 1
    record = records[0]
    assert record.telemetry == {
        "error_type": "HttpResponseError", "http_status": 403, "service_code": expected}
    assert record.exc_info is None
    rendered = TelemetryFormatter().format(record)
    assert "private-" not in rendered
    assert "primary failure" not in rendered


async def test_cancelled_owner_still_logs_suppressed_cleanup_error(
        blob_gateway, fail_next_operation, caplog):
    await blob_gateway.create("context/a", b"one")
    entered = asyncio.Event()

    async def work():
        async with blob_gateway.lease("context/a"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await asyncio.wait_for(entered.wait(), 2)
    fail_next_operation("release", 503, "ServerBusy")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    records = [r for r in caplog.records if r.name == leases.__name__]
    assert len(records) == 1
    assert records[0].telemetry["http_status"] == 503
    assert records[0].telemetry["service_code"] == "ServerBusy"
