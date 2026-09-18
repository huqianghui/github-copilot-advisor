"""The same gateway contracts run through the real SDK against memory or Azurite."""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import format_datetime
import os
from time import monotonic
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4
import xml.etree.ElementTree as ET

from azure.core.pipeline.transport import (
    AioHttpTransport, AsyncHttpResponse, AsyncHttpTransport,
)
from azure.core.utils import CaseInsensitiveDict
import pytest


def pytest_addoption(parser):
    parser.addoption("--cloud-store", choices=("memory", "azurite"), default="memory")


class Response(AsyncHttpResponse):
    def __init__(self, request, status, body=b"", headers=None):
        super().__init__(request, None)
        self.status_code = status
        self.reason = "contract response"
        self.headers = CaseInsensitiveDict({
            "content-length": str(len(body)), "content-type": "application/octet-stream",
            **(headers or {}),
        })
        self.content_type = self.headers["content-type"]
        self._body = body

    def body(self):
        return self._body

    async def load_body(self):
        pass

    async def read(self):
        return self._body

    def stream_download(self, pipeline, **kwargs):
        return Download(self)


class Download:
    """The SDK attaches properties/location metadata to this mutable iterator."""

    def __init__(self, response):
        self.response = response
        self.body = response.body()
        self.content_length = len(self.body)
        self.sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.sent or not self.body:
            raise StopAsyncIteration
        self.sent = True
        return self.body


def error_response(request, status, code):
    root = ET.Element("Error")
    ET.SubElement(root, "Code").text = code
    ET.SubElement(root, "Message").text = "injected contract failure"
    return Response(request, status, ET.tostring(root), {
        "content-type": "application/xml", "x-ms-error-code": code})


@dataclass
class MemoryBlob:
    body: bytes
    etag: str = field(default_factory=lambda: f'"{uuid4()}"')
    last_modified: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    lease_id: str | None = None
    lease_expires: float = 0

    def headers(self):
        return {"etag": self.etag, "last-modified": format_datetime(self.last_modified, usegmt=True),
                "x-ms-blob-type": "BlockBlob"}


class ContractTransport(AsyncHttpTransport):
    """Implements wire-level preconditions, never bypassing SDK serialization."""

    def __init__(self, real=False):
        self.inner = AioHttpTransport() if real else None
        self.blobs = {}
        self.failures = deque()
        self.calls = []
        self.closed = False

    async def open(self):
        if self.inner:
            await self.inner.open()

    async def close(self):
        self.closed = True
        if self.inner:
            await self.inner.close()

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def send(self, request, **kwargs):
        query = parse_qs(urlsplit(request.url).query)
        comp = query.get("comp", [None])[0]
        if comp == "lease":
            operation = request.headers["x-ms-lease-action"]
        elif comp == "list":
            operation = "list"
        elif request.method == "PUT":
            operation = "create" if request.headers.get("If-None-Match") == "*" else "write"
        else:
            operation = {"GET": "read", "DELETE": "delete"}.get(request.method, request.method)
        self.calls.append((operation, request))
        if self.failures and self.failures[0][0] == operation:
            _, status, code = self.failures.popleft()
            return error_response(request, status, code)
        if self.inner:
            return await self.inner.send(request, **kwargs)
        # Memory URL is https://memory.invalid/contract/<blob>.
        name = unquote(urlsplit(request.url).path).split("/", 2)[-1]
        if operation == "list":
            names = sorted(name for name in self.blobs
                           if name.startswith(query.get("prefix", [""])[0]))
            start = int(query.get("marker", ["0"])[0])
            limit = int(query.get("maxresults", ["200"])[0])
            selected = names[start:start + limit]
            root = ET.Element("EnumerationResults", ServiceEndpoint="https://memory.invalid/",
                              ContainerName="contract")
            blobs = ET.SubElement(root, "Blobs")
            for item in selected:
                node = ET.SubElement(blobs, "Blob")
                ET.SubElement(node, "Name").text = item
                props = ET.SubElement(node, "Properties")
                ET.SubElement(props, "Etag").text = self.blobs[item].etag
                ET.SubElement(props, "Last-Modified").text = self.blobs[item].headers()["last-modified"]
                ET.SubElement(props, "Content-Length").text = str(len(self.blobs[item].body))
                ET.SubElement(props, "BlobType").text = "BlockBlob"
            ET.SubElement(root, "NextMarker").text = (
                str(start + len(selected)) if start + len(selected) < len(names) else "")
            return Response(request, 200, ET.tostring(root), {"content-type": "application/xml"})
        blob = self.blobs.get(name)
        if operation == "create":
            if blob is not None:
                return error_response(request, 409, "BlobAlreadyExists")
            blob = MemoryBlob(bytes(request.body))
            self.blobs[name] = blob
            return Response(request, 201, headers=blob.headers())
        if blob is None:
            return error_response(request, 404, "BlobNotFound")
        if blob.lease_id and monotonic() >= blob.lease_expires:
            blob.lease_id = None
        if operation in ("acquire", "renew", "release", "break"):
            if operation == "acquire":
                if blob.lease_id:
                    return error_response(request, 409, "LeaseAlreadyPresent")
                duration = int(request.headers["x-ms-lease-duration"])
                assert 15 <= duration <= 60
                blob.lease_id = request.headers["x-ms-proposed-lease-id"]
                blob.lease_expires = monotonic() + duration
                return Response(request, 201, headers={
                    **blob.headers(), "x-ms-lease-id": blob.lease_id})
            if operation == "break":
                blob.lease_id = None
                return Response(request, 202, headers={"x-ms-lease-time": "0"})
            if not blob.lease_id:
                return error_response(request, 409, "LeaseNotPresentWithLeaseOperation")
            if request.headers.get("x-ms-lease-id") != blob.lease_id:
                return error_response(request, 409, "LeaseIdMismatchWithLeaseOperation")
            if operation == "renew":
                blob.lease_expires = monotonic() + 60
                return Response(request, 200, headers={"x-ms-lease-id": blob.lease_id})
            blob.lease_id = None
            return Response(request, 200)
        if operation in ("write", "delete"):
            lease_id = request.headers.get("x-ms-lease-id")
            if blob.lease_id and lease_id is None:
                return error_response(request, 412, "LeaseIdMissing")
            if lease_id and not blob.lease_id:
                return error_response(request, 412, "LeaseNotPresentWithBlobOperation")
            if lease_id and lease_id != blob.lease_id:
                return error_response(request, 412, "LeaseIdMismatchWithBlobOperation")
        if request.headers.get("If-Match") not in (None, blob.etag):
            return error_response(request, 412, "ConditionNotMet")
        if operation == "read":
            if request.headers.get("x-ms-range"):
                if not blob.body:
                    return error_response(request, 416, "InvalidRange")
                return Response(request, 206, blob.body, {
                    **blob.headers(),
                    "content-range": f"bytes 0-{len(blob.body) - 1}/{len(blob.body)}",
                })
            return Response(request, 200, blob.body, blob.headers())
        if operation == "write":
            blob.body = bytes(request.body)
            blob.etag = f'"{uuid4()}"'
            blob.last_modified = datetime.now(timezone.utc)
            return Response(request, 201, headers=blob.headers())
        if operation == "delete":
            del self.blobs[name]
            return Response(request, 202)
        raise AssertionError(f"unsupported contract operation: {operation}")


@pytest.fixture
async def storage_harness(request):
    from azure.storage.blob.aio import ContainerClient
    from advisor_agent.durable.blob import BlobGateway

    real = request.config.getoption("--cloud-store") == "azurite"
    transport = ContractTransport(real)
    if real:
        endpoint = os.environ.get("ADVISOR_TEST_BLOB_URL", "http://127.0.0.1:11000/devstoreaccount1")
        if urlsplit(endpoint).hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise pytest.UsageError("Azurite contracts require a loopback endpoint")
        # Azurite's published development-only key, never a cloud credential.
        key = ("Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsu"
               "Fq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==")
        container = ContainerClient(endpoint, f"advisor-contract-{uuid4().hex}", credential=key,
                                    transport=transport, retry_total=0, api_version="2023-11-03")
        await container.create_container()
    else:
        container = ContainerClient("https://memory.invalid", "contract", credential=None,
                                    transport=transport, retry_total=0, api_version="2023-11-03")
    gateway = BlobGateway(container)
    try:
        yield gateway, container, transport
    finally:
        try:
            if real:
                await container.delete_container()
        finally:
            await gateway.aclose()


@pytest.fixture
def blob_gateway(storage_harness):
    return storage_harness[0]


@pytest.fixture
def expire_lease(storage_harness):
    from azure.storage.blob.aio import BlobLeaseClient

    async def expire(name):
        # A server-side break is deterministic; production has no fake expiry hook.
        await BlobLeaseClient(storage_harness[1].get_blob_client(name)).break_lease(
            lease_break_period=0)
    return expire


@pytest.fixture
def fail_next_operation(storage_harness):
    def fail(operation, status=503, code="ServerBusy"):
        storage_harness[2].failures.append((operation, status, code))
    return fail
