import asyncio
from datetime import timedelta

from azure.core.exceptions import HttpResponseError
import pytest

from advisor_agent.durable.blob import BlobConflict


async def test_read_only_missing_blob_is_none(blob_gateway):
    assert await blob_gateway.read("missing") is None


@pytest.mark.parametrize("body", [b"", b"private-payload", b"\x00\xff\xfe"])
async def test_create_and_read_preserve_bytes_etag_and_utc_time(blob_gateway, body):
    assert await blob_gateway.create("history/a", body)
    document = await blob_gateway.read("history/a")
    assert document.body == body
    assert document.etag
    assert document.last_modified.utcoffset() == timedelta(0)
    assert "private-payload" not in repr(document)


async def test_create_is_atomic_and_does_not_overwrite(blob_gateway):
    results = await asyncio.gather(*(
        blob_gateway.create("archive/a", str(i).encode()) for i in range(5)))
    assert sum(results) == 1
    expected = str(results.index(True)).encode()
    assert (await blob_gateway.read("archive/a")).body == expected
    assert await blob_gateway.create("archive/a", b"replacement") is False
    assert (await blob_gateway.read("archive/a")).body == expected


async def test_stale_etag_is_rejected(blob_gateway):
    await blob_gateway.create("context/a", b"one")
    first = await blob_gateway.read("context/a")
    etag = await blob_gateway.write("context/a", b"two", etag=first.etag)
    assert etag != first.etag
    with pytest.raises(BlobConflict):
        await blob_gateway.write("context/a", b"stale", etag=first.etag)
    assert (await blob_gateway.read("context/a")).body == b"two"


async def test_delete_requires_current_etag(blob_gateway):
    await blob_gateway.create("context/a", b"one")
    first = await blob_gateway.read("context/a")
    etag = await blob_gateway.write("context/a", b"two", etag=first.etag)
    with pytest.raises(BlobConflict):
        await blob_gateway.delete("context/a", etag=first.etag)
    await blob_gateway.delete("context/a", etag=etag)
    assert await blob_gateway.read("context/a") is None


@pytest.mark.parametrize("operation", ["write", "delete"])
@pytest.mark.parametrize("etag", ["", "*", None])
async def test_mutations_never_allow_unconditional_etags(blob_gateway, operation, etag):
    await blob_gateway.create("context/a", b"one")
    args = ("context/a", b"two") if operation == "write" else ("context/a",)
    with pytest.raises(ValueError, match="etag"):
        await getattr(blob_gateway, operation)(*args, etag=etag)
    assert (await blob_gateway.read("context/a")).body == b"one"


async def test_list_page_is_bounded_prefix_filtered_and_resumable(blob_gateway):
    for name in ("history/a", "history/b", "history/c", "other/a"):
        await blob_gateway.create(name, b"one")
    first = await blob_gateway.list_page("history/", limit=2)
    assert first.names == ["history/a", "history/b"]
    assert first.next_cursor
    second = await blob_gateway.list_page("history/", cursor=first.next_cursor, limit=2)
    assert second.names == ["history/c"]
    assert second.next_cursor is None
    empty = await blob_gateway.list_page("absent/")
    assert empty.names == [] and empty.next_cursor is None


@pytest.mark.parametrize("limit", [0, -1, True, 5001])
async def test_list_page_rejects_unbounded_or_invalid_limits(blob_gateway, limit):
    with pytest.raises(ValueError, match="limit"):
        await blob_gateway.list_page("", limit=limit)


@pytest.mark.parametrize("operation", ["read", "list", "create", "write", "delete"])
@pytest.mark.parametrize("status,code", [
    (401, "NoAuthenticationInformation"), (403, "AuthorizationPermissionMismatch"),
    (503, "ServerBusy"), (404, "ContainerNotFound"),
    (409, "ContainerBeingDeleted"),
])
async def test_service_failures_are_not_missing_or_conflicts(
        blob_gateway, fail_next_operation, operation, status, code):
    await blob_gateway.create("context/a", b"one")
    etag = (await blob_gateway.read("context/a")).etag
    fail_next_operation(operation, status, code)
    with pytest.raises(HttpResponseError) as error:
        if operation == "list":
            await blob_gateway.list_page("context/")
        elif operation == "create":
            await blob_gateway.create("context/b", b"two")
        elif operation == "write":
            await blob_gateway.write("context/a", b"two", etag=etag)
        elif operation == "delete":
            await blob_gateway.delete("context/a", etag=etag)
        else:
            await blob_gateway.read("context/a")
    assert error.value.status_code == status
    assert (await blob_gateway.read("context/a")).body == b"one"


async def test_sdk_sends_etag_conditions_for_mutations(storage_harness):
    gateway, _, transport = storage_harness
    await gateway.create("context/a", b"one")
    first = await gateway.read("context/a")
    etag = await gateway.write("context/a", b"two", etag=first.etag)
    await gateway.delete("context/a", etag=etag)
    operations = {op: req for op, req in transport.calls}
    assert operations["create"].headers["If-None-Match"] == "*"
    assert operations["write"].headers["If-Match"] == first.etag
    assert operations["delete"].headers["If-Match"] == etag
