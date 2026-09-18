"""Async Blob operations with server-enforced conditional mutations."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import NoReturn

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError
from azure.storage.blob.aio import ContainerClient

from advisor_agent.durable.leases import LeaseGuard, LeaseLost, _error_code, _lease_rejected


class BlobConflict(RuntimeError):
    """The object's version no longer matches the caller's snapshot."""


@dataclass(frozen=True)
class BlobDocument:
    body: bytes = field(repr=False)
    etag: str
    last_modified: datetime


@dataclass(frozen=True)
class BlobPage:
    names: list[str]
    next_cursor: str | None


def _etag(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or value.strip() == "*":
        raise ValueError("etag must identify a specific blob version")
    return value


def _mutation_error(error: HttpResponseError) -> NoReturn:
    if _lease_rejected(error):
        raise LeaseLost("blob lease no longer authorizes this mutation") from None
    if (error.status_code == 412 and _error_code(error) in {
            "ConditionNotMet", "TargetConditionNotMet"} or
            error.status_code == 404 and _error_code(error) == "BlobNotFound"):
        raise BlobConflict("blob version no longer matches") from None
    raise error


class BlobGateway:
    """Owns an injected async ContainerClient, not its authentication credential."""

    def __init__(self, container: ContainerClient):
        self._container = container
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Blob gateway is closed")

    async def read(self, name: str) -> BlobDocument | None:
        self._ensure_open()
        try:
            download = await self._container.get_blob_client(name).download_blob()
            body = await download.readall()
            return BlobDocument(body, download.properties.etag, download.properties.last_modified)
        except HttpResponseError as error:
            if error.status_code == 404 and _error_code(error) == "BlobNotFound":
                return None
            raise

    async def list_page(self, prefix: str, *, cursor: str | None = None,
                        limit: int = 200) -> BlobPage:
        self._ensure_open()
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError("limit must be between 1 and 5000")
        pages = self._container.list_blobs(
            name_starts_with=prefix, results_per_page=limit).by_page(continuation_token=cursor)
        try:
            page = await anext(pages)
        except StopAsyncIteration:
            return BlobPage([], None)
        names = [blob.name async for blob in page]
        return BlobPage(names, pages.continuation_token or None)

    async def create(self, name: str, body: bytes) -> bool:
        self._ensure_open()
        try:
            await self._container.get_blob_client(name).upload_blob(body, overwrite=False)
            return True
        except HttpResponseError as error:
            if error.status_code == 409 and _error_code(error) == "BlobAlreadyExists":
                return False
            raise

    async def write(self, name: str, body: bytes, *, etag: str,
                    lease_id: str | None = None) -> str:
        self._ensure_open()
        etag = _etag(etag)
        try:
            result = await self._container.get_blob_client(name).upload_blob(
                body, overwrite=True, etag=etag,
                match_condition=MatchConditions.IfNotModified, lease=lease_id)
            return result["etag"]
        except HttpResponseError as error:
            _mutation_error(error)

    async def delete(self, name: str, *, etag: str, lease_id: str | None = None) -> None:
        self._ensure_open()
        etag = _etag(etag)
        try:
            await self._container.get_blob_client(name).delete_blob(
                etag=etag, match_condition=MatchConditions.IfNotModified, lease=lease_id)
        except HttpResponseError as error:
            _mutation_error(error)

    def lease(self, name: str) -> LeaseGuard:
        self._ensure_open()
        return LeaseGuard(self._container.get_blob_client(name))

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._container.close()
