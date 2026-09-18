"""Explicit Azure authentication; an owner shares and closes one async identity."""
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, TypedDict, cast
from urllib.parse import urlsplit

from azure.core.credentials import AzureKeyCredential, AzureNamedKeyCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import (
    AzureCliCredential, ManagedIdentityCredential, get_bearer_token_provider,
)
from pydantic import HttpUrl


AuthMode = Literal["key", "entra"]
EntraSource = Literal["azure_cli", "managed_identity"]
TokenProvider = Callable[[], Awaitable[str]]


class OpenAIAuth(TypedDict, total=False):
    api_key: str
    azure_ad_token_provider: TokenProvider


def _required(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"{name} must be configured")
    return value


def _mode(env: Mapping[str, str], name: str, default: AuthMode) -> AuthMode:
    value = env.get(name, default)
    if value not in ("key", "entra"):
        raise ValueError(f"{name} must be key or entra")
    return cast(AuthMode, value)


def _endpoint(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    try:
        HttpUrl(value)
        url = urlsplit(value)
        valid = (
            (url.scheme == "https" or (
                url.scheme == "http" and url.hostname in {"127.0.0.1", "::1", "localhost"}))
            and bool(url.hostname) and url.port != 0
            and url.username is None and url.password is None
            and not url.query and not url.fragment
            and not any(character.isspace() or ord(character) < 32 or ord(character) == 127
                        for character in value)
            and "\\" not in value
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            f"{name} must use HTTPS (HTTP only on loopback), without credentials, query or fragment")
    return value


@dataclass(frozen=True)
class AzureAuthSettings:
    """Missing services stay optional; configured key services fail at startup."""

    openai_auth_mode: AuthMode = "key"
    search_auth_mode: AuthMode = "key"
    storage_auth_mode: AuthMode = "entra"
    entra_credential: EntraSource = "azure_cli"
    client_id: str | None = None
    openai_endpoint: str | None = None
    search_endpoint: str | None = None
    blob_service_url: str | None = None
    queue_service_url: str | None = None
    openai_api_key: str | None = field(default=None, repr=False)
    search_api_key: str | None = field(default=None, repr=False)
    storage_account_name: str | None = None
    storage_account_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "AzureAuthSettings":
        openai_mode = _mode(env, "AZURE_OPENAI_AUTH_MODE", "key")
        search_mode = _mode(env, "AZURE_SEARCH_AUTH_MODE", "key")
        storage_mode = _mode(env, "ADVISOR_STORAGE_AUTH_MODE", "entra")
        source = env.get("ADVISOR_ENTRA_CREDENTIAL", "azure_cli")
        if source not in ("azure_cli", "managed_identity"):
            raise ValueError("ADVISOR_ENTRA_CREDENTIAL must be azure_cli or managed_identity")
        client_id = env.get("AZURE_CLIENT_ID")
        if source == "managed_identity":
            _required(client_id, "AZURE_CLIENT_ID")
        settings = cls(
            openai_auth_mode=openai_mode, search_auth_mode=search_mode,
            storage_auth_mode=storage_mode, entra_credential=cast(EntraSource, source),
            client_id=client_id,
            openai_endpoint=_endpoint(env, "AZURE_OPENAI_ENDPOINT"),
            search_endpoint=_endpoint(env, "AZURE_SEARCH_ENDPOINT"),
            blob_service_url=_endpoint(env, "ADVISOR_BLOB_SERVICE_URL"),
            queue_service_url=_endpoint(env, "ADVISOR_QUEUE_SERVICE_URL"),
            openai_api_key=env.get("AZURE_OPENAI_API_KEY") if openai_mode == "key" else None,
            search_api_key=env.get("AZURE_SEARCH_API_KEY") if search_mode == "key" else None,
            storage_account_name=env.get("ADVISOR_STORAGE_ACCOUNT_NAME"),
            storage_account_key=env.get("ADVISOR_STORAGE_ACCOUNT_KEY")
            if storage_mode == "key" else None,
        )
        if settings.openai_endpoint and openai_mode == "key":
            _required(settings.openai_api_key, "AZURE_OPENAI_API_KEY")
        if settings.search_endpoint and search_mode == "key":
            _required(settings.search_api_key, "AZURE_SEARCH_API_KEY")
        if storage_mode == "key":
            _required(settings.storage_account_name, "ADVISOR_STORAGE_ACCOUNT_NAME")
            _required(settings.storage_account_key, "ADVISOR_STORAGE_ACCOUNT_KEY")
        return settings


def make_entra_credential(source: EntraSource, client_id: str | None) -> AsyncTokenCredential:
    if source == "azure_cli":
        return AzureCliCredential()
    if source == "managed_identity":
        return ManagedIdentityCredential(client_id=_required(client_id, "AZURE_CLIENT_ID"))
    raise ValueError("invalid Entra credential configuration")


def make_openai_provider(credential: AsyncTokenCredential) -> TokenProvider:
    return get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default")


class AzureCredentials:
    """Reuse per host/process, then await aclose() after all service clients close."""

    def __init__(self, settings: AzureAuthSettings):
        self.settings = settings
        self._entra: AsyncTokenCredential | None = None
        self._openai_provider: TokenProvider | None = None
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Azure credentials are closed")

    def _identity(self) -> AsyncTokenCredential:
        self._ensure_open()
        if self._entra is None:
            self._entra = make_entra_credential(
                self.settings.entra_credential, self.settings.client_id)
        return self._entra

    async def _openai_token(self) -> str:
        self._ensure_open()
        assert self._openai_provider is not None
        return await self._openai_provider()

    def openai_auth(self) -> OpenAIAuth:
        self._ensure_open()
        if self.settings.openai_auth_mode == "key":
            return {"api_key": _required(self.settings.openai_api_key, "AZURE_OPENAI_API_KEY")}
        if self._openai_provider is None:
            self._openai_provider = make_openai_provider(self._identity())
        return {"azure_ad_token_provider": self._openai_token}

    def search_credential(self) -> AzureKeyCredential | AsyncTokenCredential:
        self._ensure_open()
        if self.settings.search_auth_mode == "key":
            return AzureKeyCredential(_required(self.settings.search_api_key, "AZURE_SEARCH_API_KEY"))
        return self._identity()

    def storage_credential(self) -> AzureNamedKeyCredential | AsyncTokenCredential:
        self._ensure_open()
        if self.settings.storage_auth_mode == "key":
            return AzureNamedKeyCredential(
                _required(self.settings.storage_account_name, "ADVISOR_STORAGE_ACCOUNT_NAME"),
                _required(self.settings.storage_account_key, "ADVISOR_STORAGE_ACCOUNT_KEY"))
        return self._identity()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._entra is not None:
            await self._entra.close()
