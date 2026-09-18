"""Opt-in benchmark admission; never authenticates using the Bot JWT boundary.

Run metadata contains a counter and at most 256 live reservations. Immutable
per-request receipts provide idempotency without an ever-growing run document.
A reservation survives failure before job admission and is resumed by the same
request; it is never refunded, so ambiguous storage failures cannot exceed the
budget. No benchmark lease is held while submitting a job or notifying outbox.

Concurrency limits unfinished generation/admission to max_concurrency, with
one additional bounded window of max_concurrency delivery handoffs. A job in
``sending`` has already checkpointed its answer, and its observer may submit
the next question before the final HTTP ACK/terminal write. That handoff does
not consume a generation slot, but total nonterminal reservations never exceed
2 * max_concurrency, even if all callbacks stall. It never refunds ``used``.

Approved datasets are explicit deployment inputs, not a packaged sample or a
fallback. The ingress imports neither the model factory nor the turn executor.

Hosts must supply BENCHMARK_TARGET_ID as the exact deployed resource ID expected
by the driver, and BENCHMARK_CASES_PATH as the installed, approved cases.json
file (no sample/default path). The approved artifact entry is
``runtime-config/benchmark-cases.json``: configure
``/app/runtime-config/benchmark-cases.json`` for ACA and the corresponding
absolute path under the Functions script root for Flex. The loader consumes
the configured path literally; it never searches the repository or examples.
Canonical Entra settings are
BENCHMARK_API_CLIENT_ID, BENCHMARK_ALLOWED_CLIENT_IDS, and
BENCHMARK_ALLOWED_PRINCIPAL_IDS. The earlier API_APP_ID/DRIVER_* spellings are
backward aliases only; conflicting values fail startup.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import json
import logging
from pathlib import Path
from time import perf_counter
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from advisor_agent.durable.jobs import JobConflict, JobExpired, make_job_id
from advisor_agent.durable.leases import LeaseBusy
from advisor_shared.benchmark import (
    BenchmarkAck, BenchmarkRequest, BenchmarkRunAccepted,
    BenchmarkRunRegistration, conversation_id, dataset_digest, load_dataset,
    SafeTelemetry, StageObservation,
)
from advisor_shared.cloud import TaskPayload
from advisor_shared.entra_api import (
    AuthRejected, EntraApiValidator, EntraPrincipal,
)
from teams_adapter.cloud.ingress import HttpResult
from teams_adapter.cloud.benchmark_observations import host_snapshot, merge_telemetry
from teams_adapter.cloud.observability import observe_ingress_job
from teams_adapter.extract import build_conversation_key


BENCHMARK_SERVICE_URL = "https://benchmark.invalid"
_MAX_BODY = 64 * 1024
logger = logging.getLogger(__name__)


def _log_failure(reason, error, *, status=503):
    known = {
        "AuthRejected", "AuthUnavailable", "HttpResponseError", "ServiceRequestError",
        "ServiceResponseError", "TimeoutError", "ConnectionError", "OSError",
        "ValueError", "ValidationError", "TypeError", "RuntimeError", "JobConflict",
        "JobExpired", "LeaseBusy", "LeaseLost", "BlobConflict",
    }
    kind = next((item.__name__ for item in type(error).__mro__
                 if item.__name__ in known), "OtherError")
    logger.warning("benchmark_rejected", extra={"telemetry": {
        "http_status": status, "reason": reason, "error_type": kind,
    }})


def _setting(env, canonical, alias):
    primary, previous = env.get(canonical), env.get(alias)
    if primary is not None and previous is not None and primary.strip() != previous.strip():
        raise ValueError(f"conflicting {canonical} and {alias}")
    return (primary if primary is not None else previous or "").strip()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _header(headers, name: str) -> str | None:
    values = [value for key, value in headers.items() if key.casefold() == name]
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    return values[0]


def _boolean(env, name, default=False):
    value = env.get(name, str(default)).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


@dataclass(frozen=True)
class BenchmarkSettings:
    enabled: bool = False
    target_id: str = ""
    cases_path: str = ""
    max_requests: int = 10000
    max_concurrency: int = 128
    max_run_seconds: int = 86400

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("benchmark enabled must be boolean")
        if not self.enabled:
            return
        if not self.target_id.strip() or len(self.target_id) > 1024 or not self.cases_path:
            raise ValueError("benchmark target and approved dataset must be configured")
        for value, bound in (
            (self.max_requests, 1_000_000), (self.max_concurrency, 128),
            (self.max_run_seconds, 86400),
        ):
            if type(value) is not int or not 1 <= value <= bound:
                raise ValueError("invalid benchmark deployment bound")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> BenchmarkSettings:
        if not _boolean(env, "BENCHMARK_ENABLED"):
            return cls()
        return cls(
            enabled=True, target_id=env.get("BENCHMARK_TARGET_ID", ""),
            cases_path=env.get("BENCHMARK_CASES_PATH", ""),
            max_requests=int(env.get("BENCHMARK_MAX_REQUESTS", "10000")),
            max_concurrency=int(env.get("BENCHMARK_MAX_CONCURRENCY", "128")),
            max_run_seconds=int(env.get("BENCHMARK_MAX_RUN_SECONDS", "86400")))


class BenchmarkAuth:
    """Explicit key mode has a separate header and never downgrades Entra."""

    def __init__(self, *, mode: Literal["entra", "key"] = "entra",
                 validator: EntraApiValidator | None = None, key: str | None = None):
        if mode not in {"entra", "key"}:
            raise ValueError("invalid benchmark auth mode")
        if mode == "entra" and validator is None:
            raise ValueError("benchmark Entra validator is required")
        if mode == "key" and (
            not isinstance(key, str) or len(key) < 32 or not key.isascii()
            or any(character.isspace() for character in key)
        ):
            raise ValueError("benchmark key must be at least 32 ASCII non-space characters")
        self.mode, self.validator, self._key = mode, validator, key

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, key_provider=None) -> BenchmarkAuth:
        mode = env.get("BENCHMARK_AUTH_MODE", "entra")
        if mode == "key":
            return cls(mode="key", key=env.get("BENCHMARK_API_KEY"))
        if mode != "entra":
            raise ValueError("invalid benchmark auth mode")
        # A v2 token's aud is the API application GUID, not its acquisition URI.
        audience = str(UUID(_setting(
            env, "BENCHMARK_API_CLIENT_ID", "BENCHMARK_API_APP_ID")))
        validator = EntraApiValidator(
            tenant_id=env.get("BENCHMARK_TENANT_ID", ""), audience=audience,
            allowed_client_ids=[item.strip() for item in _setting(
                env, "BENCHMARK_ALLOWED_CLIENT_IDS", "BENCHMARK_DRIVER_CLIENT_IDS").split(",")],
            allowed_principal_ids=[item.strip() for item in _setting(
                env, "BENCHMARK_ALLOWED_PRINCIPAL_IDS", "BENCHMARK_DRIVER_PRINCIPAL_IDS").split(",")],
            required_role="Benchmark.Invoke", key_provider=key_provider)
        return cls(validator=validator)

    async def verify(self, headers) -> EntraPrincipal:
        if self.mode == "key":
            supplied = _header(headers, "x-benchmark-key")
            if (supplied is None or not supplied.isascii() or len(supplied) > 16384
                    or not hmac.compare_digest(self._key, supplied)):
                raise AuthRejected("benchmark credential rejected")
            return EntraPrincipal(_digest(self._key), "benchmark-explicit-key", ())
        authorization = _header(headers, "authorization")
        if (authorization is None or len(authorization) > 16400
                or "\r" in authorization or "\n" in authorization):
            raise AuthRejected("benchmark credential required")
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].casefold() != "bearer" or not parts[1]:
            raise AuthRejected("benchmark credential rejected")
        return await self.validator.verify(parts[1])


@dataclass(frozen=True)
class ApprovedDataset:
    digest: str
    cases: dict[str, str]
    seeded_history: list[dict[str, str]]

    @classmethod
    def load(cls, path: str | Path) -> ApprovedDataset:
        data = load_dataset(path)
        return cls(
            dataset_digest(data),
            {case["case_id"]: case["question"] for case in data["cases"]},
            data["seeded_history"])


def _json(body, *, limit=_MAX_BODY):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate property")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("invalid JSON number")

    if isinstance(body, (str, bytes)):
        if len(body if isinstance(body, bytes) else body.encode("utf-8")) > limit:
            raise ValueError("request too large")
        body = json.loads(body, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(body, dict):
        raise ValueError("JSON object required")
    return body


class _Rejection(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__("benchmark request rejected")


class _Stored(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Reservation(_Stored):
    request: BenchmarkRequest
    accepted_at: AwareDatetime
    trace_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    job_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    telemetry: SafeTelemetry = Field(default_factory=SafeTelemetry)


class RegisteredRun(_Stored):
    registration: BenchmarkRunRegistration
    principal_id: str
    application_id: str
    used: int = Field(default=0, ge=0, le=1_000_000)
    active: list[Reservation] = Field(default_factory=list, max_length=256)


class BenchmarkRunStore:
    def __init__(self, jobs, settings: BenchmarkSettings, dataset: ApprovedDataset):
        self.jobs, self.gateway = jobs, jobs.gateway
        self.settings, self.dataset = settings, dataset

    @staticmethod
    def run_name(run_id):
        return f"benchmark/runs/{_digest(run_id)}.json"

    @staticmethod
    def request_name(request):
        return f"benchmark/requests/{_digest(request.run_id)}/{_digest(request.request_id)}.json"

    def now(self):
        return self.jobs.now()

    async def load(self, run_id) -> RegisteredRun:
        document = await self.gateway.read(self.run_name(run_id))
        if document is None:
            raise _Rejection(404)
        run = RegisteredRun.model_validate_json(document.body)
        if (run.registration.run_id != run_id
                or run.registration.dataset_sha256 != self.dataset.digest):
            raise _Rejection(409)
        return run

    def _authorize(self, run, principal):
        if (run.principal_id, run.application_id) != (
            principal.object_id, principal.application_id
        ):
            raise _Rejection(403)

    async def register(self, registration, principal) -> bool:
        existing = await self.gateway.read(self.run_name(registration.run_id))
        if existing:
            run = await self.load(registration.run_id)
            self._authorize(run, principal)
            if run.registration != registration:
                raise _Rejection(409)
            self._check_registration(registration)
            return False
        self._check_registration(registration)
        run = RegisteredRun(
            registration=registration, principal_id=principal.object_id,
            application_id=principal.application_id)
        if await self.gateway.create(
            self.run_name(registration.run_id), run.model_dump_json().encode("utf-8")
        ):
            return True
        return await self.register(registration, principal)

    def _check_registration(self, registration):
        remaining = (datetime.fromisoformat(
            registration.expires_at.replace("Z", "+00:00")) - self.now()).total_seconds()
        if (not 0 < remaining <= self.settings.max_run_seconds
                or registration.dataset_sha256 != self.dataset.digest
                or not set(registration.case_ids).issubset(self.dataset.cases)
                or registration.max_requests > self.settings.max_requests
                or registration.max_concurrency > self.settings.max_concurrency):
            raise _Rejection(400)

    def activity(self, request, run):
        registration = run.registration
        namespace = _digest("\0".join((
            self.settings.target_id, run.principal_id, run.application_id,
            registration.run_id)))
        return {
            "type": "message", "id": request.request_id,
            "channelId": "msteams", "serviceUrl": BENCHMARK_SERVICE_URL,
            "text": self.dataset.cases[request.case_id],
            "recipient": {"id": self.jobs.bot_id},
            "from": {"id": f"bench:{namespace}:user-{request.virtual_user}"},
            "conversation": {
                "id": conversation_id(request, registration.history_mode),
                "conversationType": "personal",
            },
            "channelData": {"tenant": {"id": f"bench:{namespace}"}},
        }

    def payload(self, reservation, run):
        activity = self.activity(reservation.request, run)
        return TaskPayload(
            job_id=reservation.job_id, partition_key=build_conversation_key(activity),
            activity=activity, accepted_at=reservation.accepted_at,
            trace_id=reservation.trace_id, target="benchmark",
            run_id=reservation.request.run_id, request_id=reservation.request.request_id)

    @asynccontextmanager
    async def _lease(self, name):
        async with AsyncExitStack() as stack:
            async with asyncio.timeout(5):
                while True:
                    try:
                        guard = await stack.enter_async_context(self.gateway.lease(name))
                        break
                    except LeaseBusy:
                        await asyncio.sleep(0.01)
            yield guard

    def _check_request(self, request, run):
        registration = run.registration
        if self.now() >= datetime.fromisoformat(registration.expires_at.replace("Z", "+00:00")):
            raise _Rejection(410)
        if (request.case_id not in registration.case_ids
                or request.scenario != registration.scenario
                or request.virtual_user >= registration.max_concurrency
                or registration.history_mode == "same_user" and request.virtual_user != 0):
            raise _Rejection(400)

    async def reserve(self, request, principal, *,
                      telemetry: SafeTelemetry | None = None) -> tuple[Reservation, RegisteredRun]:
        run = await self.load(request.run_id)
        self._authorize(run, principal)
        self._check_request(request, run)
        name = self.request_name(request)
        document = await self.gateway.read(name)
        if document:
            receipt = Reservation.model_validate_json(document.body)
            if receipt.request != request:
                raise _Rejection(409)
            return receipt, run
        run_name = self.run_name(request.run_id)
        async with self._lease(run_name) as guard:
            document = await self.gateway.read(run_name)
            run = RegisteredRun.model_validate_json(document.body)
            self._authorize(run, principal)
            self._check_request(request, run)
            receipt = next((item for item in run.active
                            if item.request.request_id == request.request_id), None)
            if receipt is not None:
                if receipt.request != request:
                    raise _Rejection(409)
            else:
                # A concurrent request may have completed between the first read
                # and our lease acquisition. Its immutable receipt still wins.
                completed = await self.gateway.read(name)
                if completed:
                    receipt = Reservation.model_validate_json(completed.body)
                    if receipt.request != request:
                        raise _Rejection(409)
                else:
                    if run.used >= run.registration.max_requests:
                        raise _Rejection(429)
                    live = []
                    preparing = 0
                    for item in run.active:
                        job = await self.jobs.read(item.job_id)
                        if job is None or not job.terminal:
                            live.append(item)
                            if job is None or job.state != "sending":
                                preparing += 1
                    if (preparing >= run.registration.max_concurrency
                            or len(live) >= 2 * run.registration.max_concurrency):
                        raise _Rejection(429)
                    activity = self.activity(request, run)
                    receipt = Reservation(
                        request=request, accepted_at=self.now(), trace_id=uuid4().hex,
                        telemetry=telemetry or SafeTelemetry(),
                        job_id=make_job_id(
                            self.jobs.bot_id, build_conversation_key(activity), request.request_id))
                    run.active, run.used = [*live, receipt], run.used + 1
                    guard.assert_active()
                    await self.gateway.write(
                        run_name, run.model_dump_json().encode("utf-8"),
                        etag=document.etag, lease_id=guard.id)
        if not await self.gateway.create(name, receipt.model_dump_json().encode("utf-8")):
            existing = await self.gateway.read(name)
            if existing is None or Reservation.model_validate_json(existing.body) != receipt:
                raise _Rejection(409)
        return receipt, run

    async def validate_payload(self, payload):
        if payload.target != "benchmark" or not payload.run_id or not payload.request_id:
            raise ValueError("invalid benchmark payload")
        run = await self.load(payload.run_id)
        document = await self.gateway.read(self.request_name(BenchmarkRequest(
            run_id=payload.run_id, request_id=payload.request_id,
            case_id=run.registration.case_ids[0], virtual_user=0,
            scenario=run.registration.scenario)))
        if document is None:
            raise ValueError("unregistered benchmark payload")
        reservation = Reservation.model_validate_json(document.body)
        if self.payload(reservation, run) != payload:
            raise ValueError("benchmark payload identity mismatch")
        return run, reservation


class BenchmarkIngress:
    """Shared host handlers returning HttpResult.

    ``telemetry_provider()`` reads a host-owned SafeTelemetry snapshot for this
    actual workload invocation. First-request evidence includes registration,
    authentication and ordinary Teams traffic, but excludes trusted host-designated
    health/readiness probes. It is never a benchmark-local counter or reset.
    Missing providers leave boot/instance metadata unknown.
    """

    def __init__(self, jobs, dispatcher, *, settings=None, auth=None,
                 telemetry_provider=None):
        self.settings = settings or BenchmarkSettings()
        self.jobs, self.dispatcher, self.auth = jobs, dispatcher, auth
        self.telemetry_provider = telemetry_provider
        self.runs = None
        if self.settings.enabled:
            if jobs is None or dispatcher is None or auth is None:
                raise ValueError("enabled benchmark dependencies are required")
            self.runs = BenchmarkRunStore(
                jobs, self.settings, ApprovedDataset.load(self.settings.cases_path))

    async def handle_runs(self, method, headers, body) -> HttpResult:
        return await self._handle("runs", method, headers, body)

    async def handle_messages(self, method, headers, body) -> HttpResult:
        return await self._handle("messages", method, headers, body)

    async def _handle(self, operation, method, headers, body):
        if not self.settings.enabled:
            return HttpResult(404)
        if method != "POST":
            return HttpResult(405, headers={"Allow": "POST"})
        try:
            snapshot = host_snapshot(self.telemetry_provider)
            started = perf_counter()
            principal = await self.auth.verify(headers)
            auth_observation = StageObservation(
                name="auth", duration_ms=(perf_counter() - started) * 1000, outcome="success")
        except AuthRejected as error:
            _log_failure("unauthorized", error, status=401)
            return HttpResult(401)
        except Exception as error:
            _log_failure("authentication_unavailable", error)
            return HttpResult(503)
        try:
            parsed = _json(body)
            if operation == "runs":
                registration = BenchmarkRunRegistration.model_validate(parsed)
                created = await self.runs.register(registration, principal)
                return HttpResult(
                    201 if created else 200, BenchmarkRunAccepted(
                        run_id=registration.run_id, target_id=self.settings.target_id,
                        dataset_sha256=self.runs.dataset.digest).model_dump(mode="json"))
            request = BenchmarkRequest.model_validate(parsed)
            reservation, run = await self.runs.reserve(
                request, principal, telemetry=merge_telemetry(
                    snapshot, SafeTelemetry(stages=[auth_observation])))
            payload = self.runs.payload(reservation, run)
            receipt = await self.jobs.submit(payload)
            observe_ingress_job(payload)
            await self.dispatcher.notify(payload.partition_key, payload.trace_id)
            return HttpResult(202, BenchmarkAck(
                run_id=request.run_id, request_id=request.request_id,
                job_id=receipt.job_id).model_dump(mode="json"))
        except _Rejection as error:
            _log_failure("admission_rejected", error, status=error.status)
            return HttpResult(
                error.status, headers={"Retry-After": "1"} if error.status == 429 else {})
        except (ValidationError, ValueError, TypeError, UnicodeError) as error:
            _log_failure("invalid_request", error, status=400)
            return HttpResult(400)
        except (JobConflict, JobExpired) as error:
            _log_failure("admission_conflict", error, status=409)
            return HttpResult(409)
        except Exception as error:
            _log_failure("admission_unavailable", error)
            return HttpResult(503)
