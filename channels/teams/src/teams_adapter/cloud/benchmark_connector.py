"""Isolated benchmark SDK transport and checkpointed worker executor.

The observer URL is fixed process configuration; SDK service URLs never select
the HTTP destination. Per-turn clients do not close the runtime-owned session.
No OAuth, attachment, update, delete, or arbitrary-conversation API is provided.

``seeded20`` uses one immutable bootstrap per new test conversation, overlaid on
the first model input rather than inserted as ten job sequences. This avoids
colliding with the ordinary worker's first history commit. Its actual storage
setup time is reported separately as ``history_setup``; it is included in
end-to-end latency, never silently called model time.

``agent`` projects the observed core turn, not a model call. ``synthetic``
separately measures only the explicit 1,000 ms asynchronous wait, excluding
other core/SDK work. It is not a model latency sample. The rendered reply also
labels that scenario; no model/dependency observations are manufactured.

The saved PreparedTurn.reply is a private checkpoint envelope, including a null
rendered reply for ignored turns. Thus SessionWorker still invokes send for
no-visible-message outcomes, without requiring a special worker execution path.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import ipaddress
import json
from time import perf_counter
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from aiohttp import ClientConnectorError, ClientError, ClientSession
from azure.core.exceptions import HttpResponseError, ServiceRequestError, ServiceResponseError
from microsoft_agents.activity import Activity, ResourceResponse
from microsoft_agents.hosting.core import ChannelServiceClientFactoryBase
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from advisor_shared.benchmark import (
    BenchmarkReply, RenderedReply, SafeTelemetry, StageObservation,
)
from advisor_shared.cloud import PreparedTurn, TaskPayload
from advisor_shared.delivery import DeliveryFailure
from advisor_shared.telemetry import step
from teams_adapter.cloud.benchmark import (
    BENCHMARK_SERVICE_URL, BenchmarkAuth, _Rejection, _log_failure,
)
from teams_adapter.extract import build_conversation_key
from teams_adapter.cloud.benchmark_observations import (
    ObservedComponents, capture_preparation, host_snapshot, merge_telemetry,
    preparation_telemetry,
)


def _failure(code, *, definite=True, retryable=False, retry_after=None):
    return DeliveryFailure(
        definitely_not_sent=definite, retryable=retryable, safe_code=code,
        retry_after_seconds=retry_after)


def _callback_url(value, allow_loopback):
    try:
        parsed = urlsplit(value)
        if (not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != "/bench/replies"
                or any(character.isspace() for character in value)):
            raise ValueError("invalid callback")
        host = parsed.hostname.casefold()
        if any(host == domain or host.endswith("." + domain) for domain in (
            "botframework.com", "trafficmanager.net", "teams.microsoft.com",
            "teams.microsoft.us", "benchmark.invalid",
        )):
            raise ValueError("Teams is not a benchmark receiver")
        if parsed.scheme != "https":
            if not (allow_loopback and parsed.scheme == "http"
                    and ipaddress.ip_address(host).is_loopback):
                raise ValueError("HTTPS callback required")
        parsed.port
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid fixed benchmark callback URL") from None
    return value


def _retry_after(value):
    try:
        if value is None:
            return None
        if value.isascii() and value.isdecimal():
            return float(value)
        deadline = parsedate_to_datetime(value)
        if deadline.utcoffset() is None:
            return None
        return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


class BenchmarkReplySink:
    """Authenticated bounded HTTP sender. Session and credential stay caller-owned."""

    def __init__(
        self, session: ClientSession, callback_url: str, *,
        auth_mode: Literal["entra", "key"] = "entra",
        credential=None, api_app_id: str | None = None, key: str | None = None,
        allow_loopback: bool = False,
    ):
        self.session = session
        self.callback_url = _callback_url(callback_url, allow_loopback)
        self.auth_mode, self._credential, self._key = auth_mode, credential, key
        self.scope = None
        if auth_mode == "key":
            BenchmarkAuth(mode="key", key=key)
        elif auth_mode == "entra" and credential is not None and key is None:
            try:
                self.scope = f"api://{UUID(api_app_id)}/.default"
            except (ValueError, TypeError, AttributeError):
                raise ValueError("benchmark API application GUID is required") from None
        else:
            raise ValueError("explicit benchmark callback authentication is required")

    async def _headers(self):
        if self.auth_mode == "key":
            return {"X-Benchmark-Key": self._key}
        try:
            access = await self._credential.get_token(self.scope)
            token = access.token
            if (not isinstance(token, str) or not token or len(token) > 16384
                    or any(character.isspace() for character in token)):
                raise ValueError("invalid access token")
            return {"Authorization": f"Bearer {token}"}
        except Exception:
            raise _failure("benchmark_callback_auth_unavailable", retryable=True) from None

    async def _post(self, reply: BenchmarkReply) -> str:
        headers = await self._headers()
        try:
            async with asyncio.timeout(30):
                async with self.session.post(
                    self.callback_url, headers=headers,
                    json=reply.model_dump(mode="json"), allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        rejected = response.status in {400, 401, 403, 404, 405, 409, 413, 415, 422, 429}
                        raise _failure(
                            "benchmark_callback_rejected" if rejected else "benchmark_callback_unknown",
                            definite=rejected, retryable=response.status == 429,
                            retry_after=_retry_after(response.headers.get("Retry-After"))
                            if response.status == 429 else None)
                    # Never include an upstream response body in exceptions/logs.
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(4097):
                        body.extend(chunk)
                        if len(body) > 4096:
                            raise ValueError("invalid callback receipt")
                    acknowledgement = json.loads(body)
                    dispositions = ({"typing"} if reply.event == "typing"
                                    else {"completed", "duplicate", "late"})
                    if (not isinstance(acknowledgement, dict)
                            or acknowledgement.get("disposition") not in dispositions):
                        raise ValueError("invalid callback receipt")
            return reply.job_id
        except DeliveryFailure:
            raise
        except ClientConnectorError:
            raise _failure("benchmark_callback_unreachable", retryable=True) from None
        except (ClientError, TimeoutError, ValueError, UnicodeError, TypeError):
            raise _failure("benchmark_callback_unknown", definite=False) from None
        except Exception:
            raise _failure("benchmark_callback_unknown", definite=False) from None

    async def typing(self, payload: TaskPayload) -> str:
        return await self._post(BenchmarkReply(
            run_id=payload.run_id, request_id=payload.request_id, job_id=payload.job_id,
            event="typing", telemetry=SafeTelemetry(trace_id=payload.trace_id)))

    async def send(
        self, payload: TaskPayload, prepared: PreparedTurn, *,
        telemetry: SafeTelemetry | None = None,
    ) -> str:
        try:
            visible = prepared.reply
            message = (RenderedReply(type="message", format="markdown", text=visible["text"])
                       if visible is not None else None)
            outcome = prepared.outcome
            if message is None and outcome != "ignored":
                outcome = "unknown"
            reply = BenchmarkReply(
                run_id=payload.run_id, request_id=payload.request_id,
                job_id=payload.job_id, event="final", outcome=outcome,
                reply=message, telemetry=telemetry or SafeTelemetry(trace_id=payload.trace_id))
        except (ValueError, TypeError, KeyError):
            raise _failure("invalid_reply") from None
        return await self._post(reply)


@dataclass(frozen=True)
class _Turn:
    payload: TaskPayload
    phase: Literal["prepare", "send"]
    outcome: Literal["success", "degraded", "ignored"]
    telemetry: SafeTelemetry


_turn: ContextVar[_Turn | None] = ContextVar("benchmark_connector_turn", default=None)


class _NoOAuth:
    async def close(self):
        pass

    def __getattr__(self, name):
        raise _failure("benchmark_oauth_unsupported")


class _Connector:
    def __init__(self, sink, turn):
        self.sink, self.turn = sink, turn
        self.conversations = self

    async def close(self):
        pass

    async def reply_to_activity(self, conversation_id, activity_id, activity):
        if activity_id != self.turn.payload.activity["id"]:
            raise _failure("invalid_payload")
        return await self.send_to_conversation(conversation_id, activity)

    async def send_to_conversation(self, conversation_id, activity: Activity):
        payload = self.turn.payload
        if (conversation_id != payload.activity["conversation"]["id"]
                or activity.service_url != BENCHMARK_SERVICE_URL):
            raise _failure("invalid_payload")
        if activity.type == "typing" and self.turn.phase == "prepare":
            receipt = await self.sink.typing(payload)
        elif activity.type == "message" and self.turn.phase == "send":
            receipt = await self.sink.send(
                payload, PreparedTurn(
                    reply=activity.model_dump(mode="json", by_alias=True, exclude_none=True),
                    history=[], outcome=self.turn.outcome),
                telemetry=self.turn.telemetry)
        else:
            raise _failure("benchmark_send_blocked")
        return ResourceResponse(id=receipt)

    def __getattr__(self, name):
        raise _failure("benchmark_operation_unsupported")


class BenchmarkConnectorFactory(ChannelServiceClientFactoryBase):
    """Only a validated, request-local benchmark turn can obtain a connector."""

    def __init__(self, sink: BenchmarkReplySink, *, bot_app_id: str):
        self.sink, self.bot_app_id = sink, bot_app_id

    @contextmanager
    def bind(self, payload, *, phase, outcome="ignored", telemetry=None):
        token = _turn.set(_Turn(
            payload, phase, outcome, telemetry or SafeTelemetry(trace_id=payload.trace_id)))
        try:
            yield
        finally:
            _turn.reset(token)

    def _validated_turn(self, context, claims_identity):
        turn = _turn.get()
        try:
            if (turn is None or turn.payload.target != "benchmark" or context is None
                    or claims_identity.get_app_id() != self.bot_app_id
                    or build_conversation_key(context.activity.model_dump(
                        mode="json", by_alias=True, exclude_none=True))
                    != turn.payload.partition_key):
                raise ValueError("invalid benchmark connector scope")
        except Exception:
            raise _failure("invalid_payload") from None
        return turn

    async def create_connector_client(
        self, context, claims_identity, service_url, audience,
        scopes=None, use_anonymous=False,
    ):
        turn = self._validated_turn(context, claims_identity)
        if service_url != BENCHMARK_SERVICE_URL or use_anonymous:
            raise _failure("invalid_payload")
        return _Connector(self.sink, turn)

    async def create_user_token_client(self, context, claims_identity, use_anonymous=False):
        self._validated_turn(context, claims_identity)
        return _NoOAuth()


class _SyntheticBackend:
    async def run(self, user_text, history, images=None):
        if images:
            raise _failure("benchmark_attachments_unsupported")
        with step("benchmark.synthetic"):
            await asyncio.sleep(1.0)
        return "[synthetic benchmark: 1000 ms; no model invocation]"


class _SyntheticComponents:
    def new_core(self, sessions, *, event_sink):
        from advisor_agent.core import AdvisorCore
        return AdvisorCore(
            _SyntheticBackend(), sessions, event_sink=event_sink, channel_name="benchmark-synthetic")


class _Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    benchmark_checkpoint: Literal[1] = 1
    rendered_reply: dict[str, JsonValue] | None = Field(repr=False)
    telemetry: SafeTelemetry


class BenchmarkTurnExecutor:
    """validate/prepare/send protocol for the ordinary SessionWorker.

    TeamsTurnExecutor must offer the controlled ``expected_target`` constructor
    hook. Its default remains ``teams``. This adapter never mutates target markers
    or bypasses the SDK executor's normal identity checks.

    ``host_telemetry(payload)`` reads the host's actual worker-invocation snapshot,
    including observed boot/instance/first-workload data and measured worker setup
    time if available. Trusted probes do not consume workload history. The
    accessor must not increment counters when read. Preparation
    persists this evidence with the saved reply; missing observations stay unknown.
    """

    def __init__(
        self, runs, sink: BenchmarkReplySink, *,
        components=None, connection_manager, bot_app_id: str, host_telemetry=None,
    ):
        from teams_adapter.cloud.executor import TeamsTurnExecutor

        self.runs, self.sink = runs, sink
        self.host_telemetry = host_telemetry
        self.factory = BenchmarkConnectorFactory(sink, bot_app_id=bot_app_id)
        self._synthetic = TeamsTurnExecutor(
            ObservedComponents(_SyntheticComponents()), connection_manager, bot_app_id,
            channel_service_client_factory=self.factory, expected_target="benchmark")
        self._real = (TeamsTurnExecutor(
            ObservedComponents(components), connection_manager, bot_app_id,
            channel_service_client_factory=self.factory, expected_target="benchmark")
            if components is not None else None)

    async def validate(self, payload):
        try:
            await self.runs.validate_payload(payload)
            self._synthetic.validate(payload)
        except (ValueError, TypeError, AttributeError, KeyError, _Rejection) as error:
            _log_failure("invalid_payload", error, status=400)
            raise _failure("invalid_payload") from None
        except DeliveryFailure:
            raise
        except Exception as error:
            # The worker treats every DeliveryFailure from validate as permanent.
            # Preserve infrastructure exceptions so its host can release/retry the
            # wake instead of irreversibly failing an accepted job.
            _log_failure("validation_unavailable", error)
            raise

    async def _seed(self, payload):
        data = {
            "dataset_sha256": self.runs.dataset.digest,
            "history": self.runs.dataset.seeded_history,
        }
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        suffix = hashlib.sha256(payload.partition_key.encode("utf-8")).hexdigest()
        name = f"benchmark/seeds/{suffix}.json"
        document = await self.runs.gateway.read(name)
        if document is None:
            await self.runs.gateway.create(name, encoded)
            document = await self.runs.gateway.read(name)
        if document is None or document.body != encoded:
            raise _failure("benchmark_seed_conflict")
        return json.loads(document.body)["history"]

    async def prepare(self, payload, history):
        try:
            await self.validate(payload)
            run, reservation = await self.runs.validate_payload(payload)
            snapshot = host_snapshot(self.host_telemetry, payload)
            record = await self.runs.jobs.read(payload.job_id)
            scenario = reservation.request.scenario
            executor = self._synthetic if scenario == "synthetic" else self._real
            if executor is None:
                raise _failure("benchmark_real_agent_unconfigured")
            stages = []
            if run.registration.history_mode == "seeded20":
                if history:
                    raise _failure("benchmark_seed_conflict")
                started = perf_counter()
                history = await self._seed(payload)
                stages.append(StageObservation(
                    name="history_setup", duration_ms=(perf_counter() - started) * 1000,
                    outcome="success"))
            with capture_preparation() as observed, self.factory.bind(payload, phase="prepare"):
                prepared = await executor.prepare(payload, history)
            telemetry = merge_telemetry(
                reservation.telemetry, snapshot,
                preparation_telemetry(observed, synthetic=scenario == "synthetic"),
                SafeTelemetry(
                    trace_id=payload.trace_id, stages=stages,
                    business_attempts=record.generation_attempts
                    if record and record.generation_attempts else None))
            checkpoint = _Checkpoint(
                rendered_reply=prepared.reply,
                telemetry=telemetry)
            return PreparedTurn(
                reply=checkpoint.model_dump(mode="json"),
                history=prepared.history, outcome=prepared.outcome)
        except DeliveryFailure:
            raise
        except (HttpResponseError, ServiceRequestError, ServiceResponseError, TimeoutError, OSError) as error:
            _log_failure("preparation_unavailable", error)
            raise _failure("benchmark_preparation_unavailable", retryable=True) from None
        except Exception as error:
            _log_failure("preparation_failed", error)
            raise _failure("benchmark_preparation_failed") from None

    async def send(self, payload, prepared):
        try:
            await self.validate(payload)
            record = await self.runs.jobs.read(payload.job_id)
        except (HttpResponseError, ServiceRequestError, ServiceResponseError, TimeoutError, OSError) as error:
            _log_failure("delivery_validation_unavailable", error)
            raise _failure("benchmark_delivery_validation_unavailable", retryable=True) from None
        try:
            checkpoint = _Checkpoint.model_validate(prepared.reply)
            if record and record.delivery_attempts:
                checkpoint.telemetry.delivery_attempts = record.delivery_attempts
            rendered = PreparedTurn(
                reply=checkpoint.rendered_reply, history=[], outcome=prepared.outcome)
        except (ValueError, TypeError):
            raise _failure("invalid_reply") from None
        if rendered.reply is None:
            return await self.sink.send(payload, rendered, telemetry=checkpoint.telemetry)
        # Delivery uses only the saved rendered reply, never model/history/images.
        with self.factory.bind(
            payload, phase="send", outcome=prepared.outcome, telemetry=checkpoint.telemetry
        ):
            return await self._synthetic.send(payload, rendered)
