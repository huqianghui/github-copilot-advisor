"""Plain-JSON wakes; ACA owns receipts, while Functions owns its own trigger ack."""
from __future__ import annotations

import asyncio
from asyncio import sleep
from collections.abc import AsyncIterator
from contextlib import suppress
import logging
from time import monotonic
from types import TracebackType
from uuid import uuid4

from azure.core.exceptions import HttpResponseError
from azure.storage.queue.aio import QueueClient
from azure.storage.queue._message_encoding import NoDecodePolicy, NoEncodePolicy
from typing_extensions import Self

from advisor_agent.durable.jobs import InboxStore
from advisor_agent.durable.leases import _error_code
from advisor_shared.cloud import QueueWake


_MAX_BYTES = 64 * 1024
_WAKE_TTL_SECONDS = 24 * 60 * 60
_VISIBILITY_SECONDS = 60
_RENEW_SECONDS = 20
_REQUEST_TIMEOUT_SECONDS = 20
logger = logging.getLogger(__name__)


class DeliveryLost(RuntimeError):
    """The consumer may no longer acknowledge this Queue delivery."""


def decode_wake(body: bytes) -> QueueWake:
    """Also usable by Functions: parsing grants no SDK delete/visibility capability."""
    if len(body) > _MAX_BYTES:
        raise ValueError("queue wake exceeds 64 KiB")
    return QueueWake.model_validate_json(body)


def _log_visibility_failure(error: BaseException) -> None:
    names = {"HttpResponseError", "ResourceNotFoundError", "ResourceModifiedError",
             "ClientAuthenticationError", "ServiceRequestError", "ServiceResponseError",
             "TimeoutError", "CancelledError", "DeliveryLost", "RuntimeError", "ValueError"}
    kind = next((cls.__name__ for cls in type(error).__mro__ if cls.__name__ in names), "OtherError")
    status = getattr(error, "status_code", None)
    code = _error_code(error) if isinstance(error, HttpResponseError) else None
    known = {"MessageNotFound", "PopReceiptMismatch", "ServerBusy", "AuthenticationFailed",
             "AuthorizationFailure", "AuthorizationPermissionMismatch", "QueueNotFound"}
    with suppress(Exception):
        logger.warning("queue.visibility_failed", extra={"telemetry": {
            "error_type": kind,
            "http_status": status if type(status) is int and 100 <= status <= 599 else None,
            "service_code": code if isinstance(code, str) and code in known else "Unknown",
        }})


class SessionQueue:
    def __init__(self, client: QueueClient):
        # Reject injected base64 policies: Functions consumes the same plain JSON.
        if (not isinstance(client._message_encode_policy, NoEncodePolicy)
                or not isinstance(client._message_decode_policy, NoDecodePolicy)):
            raise ValueError("queue encoding and decoding must be none")
        self._client = client
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("session queue is closed")

    async def publish(self, wake: QueueWake) -> None:
        self._ensure_open()
        body = wake.model_dump_json()
        if len(body.encode("utf-8")) > _MAX_BYTES:
            raise ValueError("queue wake exceeds 64 KiB")
        await self._client.send_message(body, time_to_live=_WAKE_TTL_SECONDS)

    async def receive(self) -> AsyncIterator[Delivery]:
        """One ACA delivery per poll. Use async with delivery for automatic renewal."""
        self._ensure_open()
        started = monotonic()
        messages = self._client.receive_messages(
            messages_per_page=1, max_messages=1, visibility_timeout=_VISIBILITY_SECONDS)
        async for message in messages:
            yield Delivery(self._client, message.id, message.pop_receipt,
                           message.content.encode("utf-8"), started)

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.close()


class Delivery:
    def __init__(self, client: QueueClient, message_id: str, pop_receipt: str,
                 body: bytes, received_at: float):
        self._client = client
        self.id = message_id
        self.pop_receipt = pop_receipt
        self.body = body
        self.lost = asyncio.Event()
        self._deadline = received_at + _VISIBILITY_SECONDS
        self._lock = asyncio.Lock()
        self._settled: str | None = None
        self._owner: asyncio.Task | None = None
        self._renewal: asyncio.Task | None = None
        self._monitoring = False
        self._entered = False
        self._cancel_requested = False

    def _assert_owned(self) -> None:
        if monotonic() >= self._deadline:
            self.lost.set()
        if self._settled or self.lost.is_set():
            raise DeliveryLost("queue delivery is no longer owned")

    def _lose(self, *, cancel_owner: bool = False) -> None:
        self.lost.set()
        if cancel_owner and self._owner is not None and not self._owner.done():
            self._cancel_requested = True
            self._owner.cancel()

    async def _change(self, action: str, delay: int = _VISIBILITY_SECONDS) -> None:
        async with self._lock:
            if action == "ack" and self._settled == "acked":
                return
            self._assert_owned()
            started = monotonic()
            try:
                async with asyncio.timeout(min(
                        _REQUEST_TIMEOUT_SECONDS, max(0.0, self._deadline - started))):
                    if action == "ack":
                        await self._client.delete_message(self.id, pop_receipt=self.pop_receipt)
                        self._settled = "acked"
                    else:
                        updated = await self._client.update_message(
                            self.id, pop_receipt=self.pop_receipt, visibility_timeout=delay)
                        self.pop_receipt = updated.pop_receipt
                        if action == "release":
                            self._settled = "released"
                        else:
                            self._deadline = started + _VISIBILITY_SECONDS
            except asyncio.CancelledError:
                self._lose()
                raise
            except Exception as error:
                self._lose()
                if (isinstance(error, HttpResponseError)
                        and error.status_code in (400, 404, 409, 412)
                        and _error_code(error) in {"MessageNotFound", "PopReceiptMismatch"}):
                    raise DeliveryLost("queue receipt is no longer valid") from None
                raise
            if self._settled:
                self._monitoring = False
                if self._renewal is not None:
                    self._renewal.cancel()

    async def ack(self) -> None:
        """Only the consumer calls this, after a safe drain or reliable reschedule."""
        await self._change("ack")

    async def renew_visibility(self) -> None:
        await self._change("renew")

    async def release(self, delay_seconds: int) -> None:
        if type(delay_seconds) is not int or not 0 <= delay_seconds <= 7 * 24 * 60 * 60:
            raise ValueError("visibility delay must be between zero and seven days")
        await self._change("release", delay_seconds)

    async def __aenter__(self) -> Self:
        if self._entered:
            raise RuntimeError("delivery contexts cannot be reused")
        self._assert_owned()
        self._entered = True
        self._monitoring = True
        self._owner = asyncio.current_task()
        self._renewal = asyncio.create_task(self._renew(), name="queue-visibility-renewal")
        return self

    async def _renew(self) -> None:
        try:
            while self._monitoring:
                await sleep(min(_RENEW_SECONDS, max(0.0, self._deadline - monotonic())))
                await self.renew_visibility()
        except asyncio.CancelledError as error:
            if self._monitoring:
                _log_visibility_failure(error)
                self._lose(cancel_owner=True)
            raise
        except Exception as error:
            _log_visibility_failure(error)
            self._lose(cancel_owner=True)

    async def _stop(self) -> None:
        self._monitoring = False
        if self._renewal is not None:
            self._renewal.cancel()
            await asyncio.gather(self._renewal, return_exceptions=True)

    async def __aexit__(self, exc_type: type[BaseException] | None,
                        exc: BaseException | None, tb: TracebackType | None) -> bool:
        self._monitoring = False
        if self._renewal is not None:
            self._renewal.cancel()
        cleanup = asyncio.create_task(self._stop(), name="queue-visibility-cleanup")
        cancelled = None
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError as error:
                cancelled = error
                if cleanup.cancelled():
                    break
        if self._cancel_requested and self._owner is not None:
            self._owner.uncancel()
        if cancelled is not None:
            raise cancelled
        if exc is not None:
            if (isinstance(exc, asyncio.CancelledError) and self._cancel_requested
                    and self._owner is not None and self._owner.cancelling() == 0):
                raise DeliveryLost("queue visibility renewal failed") from None
            return False
        if self.lost.is_set():
            raise DeliveryLost("queue visibility ownership was lost")
        return False


class OutboxDispatcher:
    def __init__(self, inbox: InboxStore, queue: SessionQueue):
        self.inbox, self.queue = inbox, queue

    async def notify(self, partition_key: str, trace_id: str) -> None:
        snapshot = await self.inbox.snapshot_outbox(partition_key)
        if not snapshot.pending_count:
            return
        await self.queue.publish(QueueWake(partition_key=partition_key, trace_id=trace_id))
        await self.inbox.confirm_notified(partition_key, snapshot.generation)

    async def repair_pending(self) -> int:
        count = 0
        async for snapshot in self.inbox.outboxes():
            if snapshot.needs_notification:
                await self.notify(snapshot.partition_key, uuid4().hex)
                count += 1
        return count
