"""Copied as sitecustomize.py ONLY by native_host_smoke.py.

The real Functions host, JWT verifier, Azure Storage SDK, durable worker, Core
and Agents SDK run unchanged. Only JWKS, model and Teams connector I/O are fake.
"""
import asyncio
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import threading
import sys
import traceback

if os.environ.get("ADVISOR_HOST_SMOKE") != "1":
    raise RuntimeError("native smoke bootstrap requires an explicit opt-in")

_lock = threading.Lock()
_events = Path(os.environ["SMOKE_EVENTS"])
_jwk = json.loads(Path(os.environ["SMOKE_PUBLIC_JWK"]).read_text())


def record(kind, **values):
    with _lock, _events.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": kind, **values}) + "\n")


async def signing_key(uri, header):
    import jwt
    if header.get("kid") != _jwk["kid"]:
        raise jwt.PyJWKClientError("unknown offline key")
    return jwt.PyJWK.from_dict(_jwk)


class TokenClient:
    async def close(self):
        pass


class ConnectorClient(TokenClient):
    @property
    def conversations(self):
        return self

    async def reply_to_activity(self, conversation_id, activity_id, activity):
        from microsoft_agents.activity import ResourceResponse
        if activity.type == "message":
            record("reply", text=activity.text, reply_to=activity_id)
        return ResourceResponse(id="native-smoke-receipt")

    async def send_to_conversation(self, conversation_id, activity):
        return await self.reply_to_activity(conversation_id, None, activity)


class ConnectorFactory:
    async def create_user_token_client(self, *args, **kwargs):
        return TokenClient()

    async def create_connector_client(self, *args, **kwargs):
        return ConnectorClient()


class ModelBoundary:
    async def run(self, user_text, history, images=None):
        from teams_adapter.cloud.observability import current_host_telemetry, host_identity
        record("model_call", host_telemetry=current_host_telemetry().model_dump(mode="json"),
               host_identity=host_identity())
        await asyncio.sleep(0)
        return "native smoke answer"


def install_boundary(module):
    if module.__name__ == "advisor_agent.factory":
        def build_components(channel_name="generic", *, credentials=None):
            record("model_built")
            return module.AdvisorComponents(module.AzureAdvisor(
                ModelBoundary(), channel_name=channel_name,
                azure_clients=(), credential_owner=None))
        module.build_components = build_components
    elif module.__name__ == "teams_adapter.cloud.auth":
        module.RestChannelServiceClientFactory = lambda *args, **kwargs: ConnectorFactory()
    elif module.__name__ == "teams_adapter.cloud.runtime":
        original = module.build_runtime

        def observed_runtime(*args, **kwargs):
            try:
                if os.environ.get("BENCHMARK_ENABLED") == "true":
                    kwargs["benchmark_allow_loopback"] = True
                return original(*args, **kwargs)
            except Exception as error:
                record("runtime_setup_failure", error_type=type(error).__name__,
                       error=str(error), frames=[
                           {"file": Path(frame.filename).name, "line": frame.lineno}
                           for frame in traceback.extract_tb(error.__traceback__)])
                raise

        module.build_runtime = observed_runtime
    else:
        module.JwtTokenValidator._jwk_client_manager.get_signing_key = signing_key


class BoundaryLoader(importlib.abc.Loader):
    def __init__(self, original):
        self.original = original

    def create_module(self, spec):
        return self.original.create_module(spec)

    def __getattr__(self, name):
        return getattr(self.original, name)

    def exec_module(self, module):
        self.original.exec_module(module)
        install_boundary(module)


class BoundaryFinder(importlib.abc.MetaPathFinder):
    modules = {
        "advisor_agent.factory", "teams_adapter.cloud.auth",
        "teams_adapter.cloud.runtime",
        "microsoft_agents.hosting.core.authorization.jwt.jwt_token_validator",
    }

    def find_spec(self, fullname, path, target=None):
        if fullname not in self.modules:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and spec.loader is not None:
            spec.loader = BoundaryLoader(spec.loader)
        return spec


sys.meta_path.insert(0, BoundaryFinder())
Path(os.environ["SMOKE_BOOTSTRAP_READY"]).write_text(str(os.getpid()))
