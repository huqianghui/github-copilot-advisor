"""The same gateway contracts run through the real SDK against memory or Azurite."""
from collections import deque
from contextlib import asynccontextmanager
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
        # Loopback tests must not consult developer proxy/netrc credentials.
        self.inner = AioHttpTransport(use_env_settings=False) if real else None
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
            marker = query.get("marker", [""])[0]
            if marker:
                if not marker.startswith("after:"):
                    return error_response(request, 400, "InvalidQueryParameterValue")
                names = [name for name in names if name > marker.removeprefix("after:")]
            limit = int(query.get("maxresults", ["200"])[0])
            selected = names[:limit]
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
                "after:" + selected[-1] if len(names) > len(selected) else "")
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


@asynccontextmanager
async def _blob_harness(request):
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
async def storage_harness(request):
    async with _blob_harness(request) as harness:
        yield harness


@pytest.fixture
async def history_storage_harness(request):
    async with _blob_harness(request) as harness:
        yield harness


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


@pytest.fixture
def job_clock():
    from datetime import timedelta

    class Clock:
        value = datetime(2026, 9, 18, tzinfo=timezone.utc)

        def __call__(self):
            return self.value

        def advance(self, **kwargs):
            self.value += timedelta(**kwargs)
    return Clock()


@pytest.fixture
def make_payload(job_clock):
    from teams_adapter.extract import build_conversation_key
    from advisor_shared.cloud import TaskPayload
    from advisor_agent.durable.jobs import make_job_id

    def make(activity_id="activity-a", user="user-a", bot_id="bot-a", text="private-question"):
        activity = {
            "type": "message", "id": activity_id, "text": text,
            "from": {"id": user}, "recipient": {"id": bot_id},
            "conversation": {"id": "conversation;thread=full", "conversationType": "personal"},
            "channelData": {"tenant": {"id": "tenant-a"}},
        }
        partition = build_conversation_key(activity)
        return TaskPayload(
            job_id=make_job_id(bot_id, partition, activity_id), partition_key=partition,
            activity=activity, accepted_at=job_clock(), trace_id="trace-a", target="teams")
    return make


@pytest.fixture
def job_store(blob_gateway, job_clock, request):
    from advisor_agent.durable.jobs import InboxStore, JobStore
    # The stress contract verifies serialization, not the loaded workstation's
    # latency. Production keeps its five-second admission deadline.
    wait = 15 if request.config.getoption("--cloud-store") == "azurite" else 5
    return JobStore(blob_gateway, bot_id="bot-a", utc_clock=job_clock,
                    inbox=InboxStore(blob_gateway, lease_wait_seconds=wait))


class QueueTransport(ContractTransport):
    def __init__(self, real=False):
        super().__init__(real)
        self.messages = {}

    async def send(self, request, **kwargs):
        query = parse_qs(urlsplit(request.url).query)
        operation = {"POST": "send", "GET": "receive", "PUT": "update",
                     "DELETE": "ack"}[request.method]
        if query.get("peekonly") == ["true"]:
            operation = "peek"
        self.calls.append((operation, request))
        if self.failures and self.failures[0][0] == operation:
            _, status, code = self.failures.popleft()
            return error_response(request, status, code)
        if self.inner:
            return await self.inner.send(request, **kwargs)
        now = datetime.now(timezone.utc)
        clock = monotonic()

        def xml_message(root, message, content=True):
            node = ET.SubElement(root, "QueueMessage")
            for key, value in {
                "MessageId": message["id"], "PopReceipt": message["receipt"],
                "InsertionTime": format_datetime(message["inserted"], usegmt=True),
                "ExpirationTime": format_datetime(message["expires"], usegmt=True),
                "TimeNextVisible": format_datetime(now, usegmt=True),
                "DequeueCount": str(message["count"]),
                **({"MessageText": message["body"]} if content else {}),
            }.items():
                ET.SubElement(node, key).text = value

        root = ET.Element("QueueMessagesList")
        if operation == "send":
            from datetime import timedelta
            message = {
                "id": str(uuid4()), "receipt": str(uuid4()), "inserted": now,
                "expires": now + timedelta(seconds=int(query.get("messagettl", ["604800"])[0])),
                "visible": clock + int(query.get("visibilitytimeout", ["0"])[0]), "count": 0,
                "body": ET.fromstring(request.body).findtext("MessageText"),
            }
            self.messages[message["id"]] = message
            xml_message(root, message, content=False)
            return Response(request, 201, ET.tostring(root), {"content-type": "application/xml"})
        if operation in ("receive", "peek"):
            limit = int(query.get("numofmessages", ["1"])[0])
            visible = [m for m in self.messages.values()
                       if m["visible"] <= clock and m["expires"] > now][:limit]
            for message in visible:
                if operation == "receive":
                    message["receipt"] = str(uuid4())
                    message["count"] += 1
                    message["visible"] = clock + int(query.get("visibilitytimeout", ["30"])[0])
                xml_message(root, message)
            return Response(request, 200, ET.tostring(root), {"content-type": "application/xml"})
        message_id = urlsplit(request.url).path.rsplit("/", 1)[-1]
        message = self.messages.get(message_id)
        if message is None:
            return error_response(request, 404, "MessageNotFound")
        if query.get("popreceipt", [""])[0] != message["receipt"]:
            return error_response(request, 400, "PopReceiptMismatch")
        if operation == "ack":
            del self.messages[message_id]
            return Response(request, 204)
        message["receipt"] = str(uuid4())
        message["visible"] = clock + int(query.get("visibilitytimeout", ["0"])[0])
        return Response(request, 204, headers={
            "x-ms-popreceipt": message["receipt"],
            "x-ms-time-next-visible": format_datetime(now, usegmt=True),
        })


@asynccontextmanager
async def _queue_harness(request):
    from azure.storage.queue.aio import QueueClient
    from advisor_agent.durable.queue import SessionQueue

    real = request.config.getoption("--cloud-store") == "azurite"
    transport = QueueTransport(real)
    endpoint = os.environ.get("ADVISOR_TEST_QUEUE_URL", "http://127.0.0.1:11001/devstoreaccount1")
    if real and urlsplit(endpoint).hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise pytest.UsageError("Azurite contracts require a loopback endpoint")
    key = ("Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsu"
           "Fq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==")
    client = QueueClient(
        endpoint if real else "https://memory.queue.core.windows.net",
        f"advisor-contract-{uuid4().hex}", credential=key,
        transport=transport, retry_total=0, api_version="2023-11-03",
        message_encode_policy=None, message_decode_policy=None)
    if real:
        await client.create_queue()
    queue = SessionQueue(client)
    try:
        yield queue, client, transport
    finally:
        try:
            if real:
                await client.delete_queue()
        finally:
            await queue.aclose()


@pytest.fixture
async def queue_harness(request):
    async with _queue_harness(request) as harness:
        yield harness


@pytest.fixture
async def poison_queue_harness(request):
    async with _queue_harness(request) as harness:
        yield harness


@pytest.fixture
def session_queue(queue_harness):
    return queue_harness[0]


@pytest.fixture
async def worker_case(job_store, job_clock, make_payload, history_storage_harness,
                      session_queue, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from advisor_agent.durable.history import BlobHistoryStore
    from advisor_agent.durable.leases import LeaseLost
    from advisor_agent.durable.queue import OutboxDispatcher
    from advisor_agent.durable.worker import SessionWorker
    from advisor_shared.cloud import PreparedTurn

    events = []

    class Executor:
        prepare_calls = 0
        send_calls = 0
        prepare_error = None
        send_error = None
        validation_error = None
        pause = None

        def __init__(self):
            self.sent = []
            self.histories = []
            self.entered = asyncio.Event()

        def validate(self, payload):
            events.append("validate")
            if self.validation_error:
                raise self.validation_error

        async def prepare(self, payload, history):
            self.prepare_calls += 1
            events.append("prepare")
            self.histories.append((payload.activity["id"], list(history)))
            self.entered.set()
            if self.pause is not None:
                await self.pause.wait()
            if self.prepare_error:
                raise self.prepare_error
            text = payload.activity["text"]
            return PreparedTurn(
                reply={"type": "message", "text": f"answer:{text}"},
                history=[{"role": "user", "content": text},
                         {"role": "assistant", "content": f"answer:{text}"}], outcome="success")

        async def send(self, payload, prepared):
            self.send_calls += 1
            events.append("send")
            if self.send_error:
                raise self.send_error
            self.sent.append(payload.activity["id"])
            return "reply-id"

    history = BlobHistoryStore(history_storage_harness[0], utc_clock=job_clock)
    executor = Executor()
    outbox = OutboxDispatcher(job_store.inbox, session_queue)
    worker = SessionWorker(job_store, history, outbox, executor)
    case = SimpleNamespace(jobs=job_store, history=history, executor=executor, outbox=outbox,
                           worker=worker, clock=job_clock, events=events,
                           history_gateway=history_storage_harness[0], queue=session_queue,
                           history_client=history_storage_harness[1], payloads={})
    history_read = history._model_history

    async def record_read(snapshot):
        events.append("history")
        return await history_read(snapshot)

    monkeypatch.setattr(history, "_model_history", record_read)

    async def accept(activity_id="a", user="a", *, notify=True):
        payload = make_payload(activity_id=activity_id, user=user, text=f"question:{activity_id}")
        case.payloads[activity_id] = payload
        # Set up an accepted turn as ingress would: replay the identical request
        # after transient admission loss. Worker failures are never retried here.
        for attempt in range(3):
            try:
                receipt = await job_store.submit(payload)
                if notify:
                    await outbox.notify(payload.partition_key, payload.trace_id)
                return receipt
            except (LeaseLost, TimeoutError):
                if attempt == 2:
                    raise

    def partition(user="a"):
        return make_payload(user=user).partition_key

    def fail_once_at(point):
        write = case.history_gateway.write
        fired = False

        async def injected(name, *args, **kwargs):
            nonlocal fired
            eligible = name.startswith("context/") and not fired
            if eligible and point == "before_history_commit":
                fired = True
                raise ConnectionError("injected history commit failure")
            result = await write(name, *args, **kwargs)
            if eligible and point == "after_history_commit":
                fired = True
                raise ConnectionError("injected history commit acknowledgement loss")
            return result
        monkeypatch.setattr(case.history_gateway, "write", injected)

    case.accept, case.partition, case.fail_once_at = accept, partition, fail_once_at
    return case
