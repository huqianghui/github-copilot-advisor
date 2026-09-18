"""Explicit Bot JWT verification; never a benchmark/API authorization boundary."""
import logging
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar

from jwt import PyJWTError, PyJWKClientConnectionError, PyJWKError, PyJWKSetError
from microsoft_agents.hosting.core import AgentAuthConfiguration
from microsoft_agents.hosting.core.authorization import ClaimsIdentity
from microsoft_agents.hosting.core.authorization.jwt import JwtTokenValidator
from microsoft_agents.hosting.aiohttp import CloudAdapter
from microsoft_agents.hosting.core import RestChannelServiceClientFactory

logger = logging.getLogger(__name__)
_sdk_phase: ContextVar[str | None] = ContextVar("teams_cloud_sdk_phase", default=None)
_clients: ContextVar[AsyncExitStack | None] = ContextVar("teams_sdk_clients", default=None)


class AuthRejected(PermissionError):
    pass


class AuthUnavailable(RuntimeError):
    pass


class _SafeSdkLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        phase = _sdk_phase.get()
        if phase is not None:
            error_type = (record.exc_info[0].__name__
                          if record.exc_info and record.exc_info[0] else None)
            record.msg = "teams_sdk_diagnostic phase=%s error_type=%s"
            record.args = (phase, error_type)
            record.exc_info = record.exc_text = record.stack_info = None
        return True


@contextmanager
def _safe_sdk_logs(phase: str):
    # SDK validation can log private Pydantic input before re-raising.
    for name in ("microsoft_agents.activity.activity",
                 "microsoft_agents.hosting.core.app.agent_application",
                 "microsoft_agents.hosting.core._http_adapter_base",
                 "microsoft_agents.authentication.msal.msal_auth",
                 "microsoft_agents.hosting.core.authorization.connection_manager"):
        sdk_logger = logging.getLogger(name)
        if not any(isinstance(item, _SafeSdkLogFilter) for item in sdk_logger.filters):
            sdk_logger.addFilter(_SafeSdkLogFilter())
    token = _sdk_phase.set(phase)
    try:
        yield
    finally:
        _sdk_phase.reset(token)


async def validate_bot_request(
    headers, config: AgentAuthConfiguration,
) -> ClaimsIdentity:
    if (not config.CLIENT_ID or config.ANONYMOUS_ALLOWED
            or not config.VALIDATE_ISSUER):
        raise AuthUnavailable("unsafe_bot_auth_configuration")
    authorization = next(
        (value for key, value in headers.items() if key.lower() == "authorization"), "")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthRejected("bot_auth_required")
    try:
        with _safe_sdk_logs("authentication"):
            claims = await JwtTokenValidator(config).validate_token(parts[1])
        if type(claims.get_claim_value("exp")) not in (int, float):
            raise ValueError("expiry required")
        return claims
    except (PyJWKClientConnectionError, PyJWKError, PyJWKSetError):
        logger.warning("bot_auth_unavailable")
        raise AuthUnavailable("bot_auth_unavailable") from None
    except (PyJWTError, ValueError, TypeError, KeyError, OverflowError):
        logger.warning("bot_auth_rejected")
        raise AuthRejected("bot_auth_rejected") from None
    except Exception:
        logger.warning("bot_auth_unavailable")
        raise AuthUnavailable("bot_auth_unavailable") from None


class _OwnedClient:
    def __init__(self, client):
        self.client, self.closed = client, False

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def close(self):
        if not self.closed:
            self.closed = True
            await self.client.close()


class _ScopedClientFactory:
    def __init__(self, factory):
        self.factory = factory

    def _own(self, client):
        owned = _OwnedClient(client)
        stack = _clients.get()
        if stack is None:
            raise RuntimeError("SDK client created outside a turn scope")
        stack.push_async_callback(owned.close)
        return owned

    async def create_connector_client(self, *args, **kwargs):
        return self._own(await self.factory.create_connector_client(*args, **kwargs))

    async def create_user_token_client(self, *args, **kwargs):
        return self._own(await self.factory.create_user_token_client(*args, **kwargs))


@asynccontextmanager
async def _client_scope():
    stack = AsyncExitStack()
    token = _clients.set(stack)
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        try:
            await stack.aclose()
        except Exception as error:
            if not failed:
                raise
            logger.warning("teams_sdk_cleanup_failed error_type=%s", type(error).__name__)
        finally:
            _clients.reset(token)


class ManagedCloudAdapter(CloudAdapter):
    """SDK 1.5 closes clients only on success; retain its pipeline and add ownership."""

    def __init__(self, *, connection_manager=None,
                 channel_service_client_factory=None, **kwargs):
        factory = (channel_service_client_factory if channel_service_client_factory is not None
                   else RestChannelServiceClientFactory(connection_manager))
        super().__init__(connection_manager=connection_manager,
                         channel_service_client_factory=_ScopedClientFactory(factory),
                         **kwargs)

    async def process_proactive(self, *args, **kwargs):
        async with _client_scope():
            return await super().process_proactive(*args, **kwargs)

    async def process_activity(self, *args, **kwargs):
        async with _client_scope():
            return await super().process_activity(*args, **kwargs)
