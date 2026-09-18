"""Renew the actual mutable blob's lease and cancel work when ownership is lost."""
import asyncio
from asyncio import sleep
from contextlib import suppress
from time import monotonic
from types import TracebackType
from uuid import uuid4

from azure.core.exceptions import HttpResponseError
from azure.storage.blob.aio import BlobClient, BlobLeaseClient
from typing_extensions import Self


_LEASE_SECONDS = 60
_RENEW_SECONDS = 20
_RENEW_TIMEOUT_SECONDS = 20
_RELEASE_TIMEOUT_SECONDS = 20


class LeaseBusy(RuntimeError):
    """Another owner holds the requested blob lease."""


class LeaseLost(RuntimeError):
    """Work must stop because its blob lease is no longer trustworthy."""


def _error_code(error: HttpResponseError) -> str | None:
    code = getattr(error, "error_code", None)
    return getattr(code, "value", code)


def _lease_rejected(error: HttpResponseError) -> bool:
    return error.status_code in (409, 412) and _error_code(error) in {
        "LeaseLost", "LeaseIdMissing", "LeaseIdMismatchWithBlobOperation",
        "LeaseNotPresentWithBlobOperation", "LeaseIdMismatchWithLeaseOperation",
        "LeaseNotPresentWithLeaseOperation", "LeaseIsBrokenAndCannotBeRenewed",
        "LeaseIsBreakingAndCannotBeChanged",
    }


class LeaseGuard:
    def __init__(self, blob: BlobClient):
        # Knowing the proposed ID allows cleanup even if acquisition is cancelled
        # after the server grants the lease but before its response reaches us.
        self._id = str(uuid4())
        self._lease = BlobLeaseClient(blob, lease_id=self._id)
        self.lost = asyncio.Event()
        self._owner: asyncio.Task | None = None
        self._renewal: asyncio.Task | None = None
        self._entered = False
        self._active = False
        self._cancel_requested = False
        self._deadline = 0.0

    @property
    def id(self) -> str:
        return self._id

    def assert_active(self) -> None:
        if self._active and monotonic() >= self._deadline:
            self._active = False
            self.lost.set()
        if not self._active or self.lost.is_set():
            raise LeaseLost("blob lease is not active")

    async def __aenter__(self) -> Self:
        if self._entered:
            raise RuntimeError("lease guards cannot be reused")
        self._entered = True
        self._owner = asyncio.current_task()
        started = monotonic()
        try:
            await self._lease.acquire(lease_duration=_LEASE_SECONDS)
        except asyncio.CancelledError:
            await self._finish()
            raise
        except HttpResponseError as error:
            if error.status_code == 409 and _error_code(error) in {
                    "LeaseAlreadyPresent", "LeaseIsBreakingAndCannotBeAcquired"}:
                raise LeaseBusy("blob is leased by another owner") from None
            raise
        # A delayed response must not extend our estimate of server ownership.
        self._deadline = started + _LEASE_SECONDS
        self._active = True
        try:
            self.assert_active()
        except LeaseLost:
            _, cancelled = await self._finish()
            if cancelled is not None:
                raise cancelled
            raise
        self._renewal = asyncio.create_task(self._renew(), name="blob-lease-renewal")
        return self

    async def _renew(self) -> None:
        try:
            while True:
                await sleep(min(_RENEW_SECONDS, max(0.0, self._deadline - monotonic())))
                self.assert_active()
                started = monotonic()
                remaining = max(0.0, self._deadline - started)
                async with asyncio.timeout(min(_RENEW_TIMEOUT_SECONDS, remaining)):
                    await self._lease.renew()
                self._deadline = started + _LEASE_SECONDS
        except asyncio.CancelledError:
            if self._active:
                self._lose()
            raise
        except Exception:
            self._lose()

    def _lose(self) -> None:
        self._active = False
        self.lost.set()
        if self._owner is not None and not self._owner.done():
            self._cancel_requested = True
            self._owner.cancel()

    async def _cleanup(self) -> None:
        if self._renewal is not None:
            self._renewal.cancel()
            with suppress(asyncio.CancelledError):
                await self._renewal
        try:
            async with asyncio.timeout(_RELEASE_TIMEOUT_SECONDS):
                await self._lease.release()
        except HttpResponseError as error:
            if not (_lease_rejected(error) or (
                    error.status_code == 404 and _error_code(error) == "BlobNotFound")):
                raise

    async def _finish(self) -> tuple[Exception | None, asyncio.CancelledError | None]:
        cleanup = asyncio.create_task(self._cleanup(), name="blob-lease-cleanup")
        cancelled = None
        while True:
            try:
                await asyncio.shield(cleanup)
                return None, cancelled
            except asyncio.CancelledError as error:
                cancelled = error
                if cleanup.cancelled():
                    return None, cancelled
            except Exception as error:
                return error, cancelled

    async def __aexit__(self, _exc_type: type[BaseException] | None,
                        exc: BaseException | None, _tb: TracebackType | None) -> bool:
        self._active = False
        if self._renewal is not None:
            self._renewal.cancel()
        cleanup_error, cleanup_cancel = await self._finish()
        if self._cancel_requested and self._owner is not None:
            # Remove only our own cancellation, never a concurrent caller cancel.
            self._owner.uncancel()
        if cleanup_cancel is not None:
            raise cleanup_cancel
        if exc is not None:
            if (isinstance(exc, asyncio.CancelledError) and self._cancel_requested
                    and self._owner is not None and self._owner.cancelling() == 0):
                raise LeaseLost("blob lease renewal failed") from None
            return False
        if self.lost.is_set():
            raise LeaseLost("blob lease is no longer active")
        if cleanup_error is not None:
            raise cleanup_error
        return False
