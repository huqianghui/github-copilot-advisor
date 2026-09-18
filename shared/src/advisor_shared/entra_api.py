"""Single-tenant application-token verification for the isolated experiment API."""
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
import logging
import math
from uuid import UUID

import jwt

logger = logging.getLogger(__name__)
KeyProvider = Callable[[str], Awaitable[jwt.PyJWK]]


class AuthRejected(PermissionError):
    """The supplied credential does not authorize this API operation."""


class AuthUnavailable(RuntimeError):
    """Authentication infrastructure is unavailable; do not downgrade auth."""


@dataclass(frozen=True)
class EntraPrincipal:
    object_id: str
    application_id: str
    roles: tuple[str, ...]


def _guid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a GUID")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise ValueError(f"{field} must be a GUID") from None


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    return value


class EntraApiValidator:
    def __init__(
        self, *, tenant_id: str, audience: str,
        allowed_client_ids: Sequence[str], allowed_principal_ids: Sequence[str],
        required_role: str = "Benchmark.Invoke", key_provider: KeyProvider | None = None,
        leeway_seconds: float = 30,
    ):
        self.tenant_id = _guid(tenant_id, "tenant_id")
        self.audience = _text(audience, "audience")
        self.required_role = _text(required_role, "required_role")
        if not allowed_client_ids or not allowed_principal_ids:
            raise ValueError("explicit client and principal allowlists are required")
        if isinstance(allowed_client_ids, str) or isinstance(allowed_principal_ids, str):
            raise ValueError("identity allowlists must be sequences of GUIDs")
        self._clients = frozenset(_guid(item, "client_id") for item in allowed_client_ids)
        self._principals = frozenset(
            _guid(item, "principal_id") for item in allowed_principal_ids)
        if not math.isfinite(leeway_seconds) or leeway_seconds < 0:
            raise ValueError("leeway_seconds must be finite and nonnegative")
        self._leeway = leeway_seconds
        self.issuer = f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"
        self._keys = jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys",
            cache_keys=True, lifespan=300, timeout=5)
        self._key_lock = asyncio.Lock()
        self._key_provider = key_provider

    async def _key(self, token: str) -> jwt.PyJWK:
        if self._key_provider is not None:
            return await self._key_provider(token)
        async with self._key_lock:
            return await asyncio.to_thread(self._keys.get_signing_key_from_jwt, token)

    async def verify(self, token: str) -> EntraPrincipal:
        try:
            if (not isinstance(token, str) or len(token) > 16384
                    or token.count(".") != 2 or any(char.isspace() for char in token)):
                raise ValueError("malformed token")
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise ValueError("unsupported signing header")
            if not header["kid"].strip():
                raise ValueError("missing signing key ID")
            key = await self._key(token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], audience=self.audience,
                issuer=self.issuer, leeway=self._leeway,
                options={"require": [
                    "exp", "iat", "nbf", "iss", "aud", "tid", "oid", "azp", "roles",
                ]},
            )
            tenant = _guid(claims.get("tid"), "tid")
            principal = _guid(claims.get("oid"), "oid")
            client = _guid(claims.get("azp"), "azp")
            if (tenant != self.tenant_id or principal not in self._principals
                    or client not in self._clients):
                raise ValueError("identity is not allowed")
            if any(type(claims[name]) is not int for name in ("exp", "iat", "nbf")):
                raise ValueError("invalid token times")
            roles = claims.get("roles")
            if (not isinstance(roles, list) or not all(isinstance(role, str) for role in roles)
                    or self.required_role not in roles):
                raise ValueError("required application role is missing")
            return EntraPrincipal(principal, client, tuple(roles))
        except (jwt.PyJWKClientConnectionError, TimeoutError, OSError) as error:
            logger.warning("entra_api_auth_unavailable error_type=%s", type(error).__name__)
            raise AuthUnavailable("identity verification is unavailable") from None
        except (jwt.PyJWTError, ValueError) as error:
            logger.warning("entra_api_auth_rejected error_type=%s", type(error).__name__)
            raise AuthRejected("credential does not authorize this operation") from None
