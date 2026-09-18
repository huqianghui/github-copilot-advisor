"""Process-owned cloud resources and supervised ACA execution."""
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import hashlib
import inspect
import json
import logging
import math
import os
import re
import threading
from time import monotonic
from typing import TYPE_CHECKING

from azure.core.exceptions import HttpResponseError, ServiceRequestError, ServiceResponseError

from advisor_agent.durable.jobs import QuarantineRecord
from advisor_agent.durable.leases import LeaseBusy, LeaseLost
from advisor_agent.durable.queue import DeliveryLost, decode_wake
from advisor_shared.telemetry import step, trace_scope
from advisor_shared.delivery import DeliveryFailure
from teams_adapter.cloud.observability import (
    current_host_telemetry, host_invocation, observe_host_stage,
)

if TYPE_CHECKING:
    from advisor_agent.durable.maintenance import Maintenance
    from advisor_agent.durable.worker import SessionWorker
    from teams_adapter.cloud.ingress import CloudIngress

logger = logging.getLogger(__name__)


class _QuarantineUnconfirmed(RuntimeError):
    pass


def _access_denied(error):
    return isinstance(error, HttpResponseError) and error.status_code in (401, 403)


def _retryable_delivery_error(error):
    if isinstance(error, (
            DeliveryLost, LeaseLost, LeaseBusy, TimeoutError, ConnectionError,
            ServiceRequestError, ServiceResponseError)):
        return True
    status = getattr(error, "status_code", None)
    return (isinstance(error, HttpResponseError) and type(status) is int
            and (status in {408, 429} or 500 <= status <= 599))


def _can_defer_delivery(error):
    return (_retryable_delivery_error(error) or _access_denied(error)
            or isinstance(error, _QuarantineUnconfirmed))


def _queue_failure(event, error, phase, *, level=logging.WARNING):
    status = getattr(error, "status_code", None)
    logger.log(level, event, extra={"telemetry": {
        "phase": phase, "error_type": type(error).__name__,
        "http_status": status if type(status) is int and 100 <= status <= 599 else None,
        "reason": (
            "access_denied" if _access_denied(error) else
            "quarantine_unconfirmed" if isinstance(error, _QuarantineUnconfirmed) else
            "delivery_lost" if isinstance(error, DeliveryLost) else
            "lease_lost" if isinstance(error, LeaseLost) else
            "transient_failure" if _retryable_delivery_error(error) else "unexpected_failure"),
    }})


class _OwnedAsyncClose:
    def __init__(self, resource):
        self.resource = resource

    async def aclose(self):
        await self.resource.close()


class _LazyCallbackSession:
    """One real HTTP session, created on its first async callback, never per turn."""

    def __init__(self):
        self._session = None
        self._closed = False

    def post(self, *args, **kwargs):
        if self._closed:
            raise RuntimeError("benchmark callback session is closed")
        if self._session is None:
            from aiohttp import ClientSession
            self._session = ClientSession()
        return self._session.post(*args, **kwargs)

    async def aclose(self):
        if not self._closed:
            self._closed = True
            if self._session is not None:
                await self._session.close()


class _DeferredComponents:
    """A non-owning core factory backed by one lazily initialized component owner."""

    def __init__(self, factory):
        self._factory = factory
        self._components = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def ensure_ready(self):
        async with self._lock:
            if self._closed:
                raise RuntimeError("model components are closed")
            if self._components is None:
                with observe_host_stage("connection"):
                    self._components = await self._factory()

    def new_core(self, sessions, *, event_sink=None):
        if self._closed or self._components is None:
            raise RuntimeError("model components are not initialized")
        return self._components.new_core(sessions, event_sink=event_sink)

    async def aclose(self):
        async with self._lock:
            if not self._closed:
                self._closed = True
                if self._components is not None:
                    await self._components.aclose()


class _TargetExecutor:
    """Dispatch exact stored targets; synthetic/recovered delivery never builds models."""

    def __init__(self, teams, benchmark, components):
        self.teams, self.benchmark, self.components = teams, benchmark, components

    def _select(self, payload):
        if payload.target == "teams":
            return self.teams
        if payload.target == "benchmark" and self.benchmark is not None:
            return self.benchmark
        raise DeliveryFailure(
            definitely_not_sent=True, retryable=False, safe_code="invalid_payload")

    async def validate(self, payload):
        result = self._select(payload).validate(payload)
        if inspect.isawaitable(result):
            await result

    async def prepare(self, payload, history):
        executor = self._select(payload)
        await self.validate(payload)
        try:
            needs_model = payload.target == "teams"
            if payload.target == "benchmark":
                _, reservation = await self.benchmark.runs.validate_payload(payload)
                needs_model = reservation.request.scenario == "real_agent"
            if needs_model:
                await self.components.ensure_ready()
        except Exception as error:
            logger.error("target_preparation_setup_failed error_type=%s", type(error).__name__)
            raise DeliveryFailure(
                definitely_not_sent=True, retryable=_retryable_delivery_error(error),
                safe_code="preparation_setup_failed") from None
        return await executor.prepare(payload, history)

    async def send(self, payload, prepared):
        return await self._select(payload).send(payload, prepared)


class CloudRuntime:
    def __init__(
        self, *, ingress, queue, worker_factory, maintenance, closeables=(),
        parallelism=4, shutdown_seconds=30, poll_interval=2.0, benchmark_ingress=None,
    ):
        if type(parallelism) is not int or parallelism < 1:
            raise ValueError("parallelism must be a positive integer")
        if any(not math.isfinite(value) or value <= 0
               for value in (shutdown_seconds, poll_interval)):
            raise ValueError("runtime delays must be finite and positive")
        if isinstance(poll_interval, bool) or poll_interval < 2:
            raise ValueError("poll_interval must be at least two seconds")
        self.ingress, self.queue, self.maintenance = ingress, queue, maintenance
        self.benchmark_ingress = benchmark_ingress
        self.dispatcher = getattr(ingress, "dispatcher", None)
        self._worker_factory = worker_factory
        self._worker = None
        self._worker_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._poll_lock = asyncio.Lock()
        self._next_poll_at = 0.0
        self._closeables = list(closeables)
        self.parallelism, self.shutdown_seconds = parallelism, shutdown_seconds
        self.poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._tasks = []
        self._closed = False
        self.healthy = True
        self.ready = False

    async def get_worker(self) -> "SessionWorker":
        async with self._worker_lock:
            if self._closed:
                raise RuntimeError("cloud runtime is closed")
            if self._worker is None:
                with observe_host_stage("worker_init"):
                    self._worker = await self._worker_factory()
            return self._worker

    def host_telemetry(self, payload=None):
        """Read the current host invocation; safe callback for target executors."""
        return current_host_telemetry(payload)

    async def start(self, *, worker_factory=None):
        if self._closed or not self.healthy:
            raise RuntimeError("cloud runtime cannot start")
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self.serve_queue(
                self._stop, parallelism=self.parallelism, worker_factory=worker_factory),
                name="advisor-queue"),
            asyncio.create_task(self.serve_maintenance(self._stop), name="advisor-maintenance"),
        ]
        for task in self._tasks:
            task.add_done_callback(self._supervise)
        self.ready = True

    def _supervise(self, task):
        error = None if task.cancelled() else task.exception()
        if not self._stop.is_set():
            self.healthy = self.ready = False
            logger.error("cloud_background_failed task=%s error_type=%s",
                         task.get_name(), type(error).__name__ if error else "UnexpectedExit")
            self._stop.set()
            for sibling in self._tasks:
                if sibling is not task and not sibling.done():
                    sibling.cancel()
        elif error is not None:
            logger.error("cloud_background_stopped_with_error error_type=%s",
                         type(error).__name__)

    async def serve_queue(self, stop: asyncio.Event, *, parallelism=4, worker_factory=None):
        if type(parallelism) is not int or parallelism < 1:
            raise ValueError("parallelism must be a positive integer")
        async with asyncio.TaskGroup() as group:
            for _ in range(parallelism):
                group.create_task(self._consume(stop, worker_factory or self.get_worker))

    async def _consume(self, stop, worker_factory):
        while not stop.is_set():
            delivery = await self._receive(stop)
            if delivery is not None:
                await self.process_delivery(delivery, worker_factory=worker_factory)

    async def _receive(self, stop):
        # One shared idle poll budget, independent of the number of busy workers.
        async with self._poll_lock:
            remaining = self._next_poll_at - monotonic()
            if remaining > 0:
                await self._wait(stop, remaining)
            if stop.is_set():
                return None
            delay = self.poll_interval
            try:
                deliveries = [delivery async for delivery in self.queue.receive()]
                if len(deliveries) > 1:
                    raise RuntimeError("queue receive must yield at most one delivery")
            except Exception as error:
                if not (_retryable_delivery_error(error) or _access_denied(error)):
                    _queue_failure("queue_poll_failed", error, "poll", level=logging.ERROR)
                    raise RuntimeError("queue_poll_failed") from None
                if _access_denied(error):
                    delay = max(30, delay)
                _queue_failure("queue_poll_retry", error, "poll",
                               level=logging.ERROR if _access_denied(error) else logging.WARNING)
                deliveries = []
            if deliveries:
                return deliveries[0]
            self._next_poll_at = monotonic() + delay
            return None

    async def quarantine_wake(self, body: bytes) -> None:
        """Persist the maintenance invalid_wake metadata contract, never the body."""
        digest = hashlib.sha256(body).hexdigest()
        jobs = self.ingress.jobs
        record = QuarantineRecord(
            code="invalid_wake", body_digest=digest, quarantined_at=jobs.now())
        name = f"poison/wake-{digest}.json"
        if await jobs.gateway.create(name, record.model_dump_json().encode("utf-8")):
            return
        document = await jobs.gateway.read(name)
        if document is None:
            raise _QuarantineUnconfirmed("quarantine_not_confirmed")
        try:
            existing = QuarantineRecord.model_validate_json(document.body)
        except (ValueError, TypeError):
            raise _QuarantineUnconfirmed("quarantine_not_confirmed") from None
        if existing.body_digest != digest:
            raise _QuarantineUnconfirmed("quarantine_not_confirmed")

    async def _recover_delivery(self, delivery, error, phase):
        needs_attention = _access_denied(error) or isinstance(error, _QuarantineUnconfirmed)
        _queue_failure("queue_delivery_retry", error, phase,
                       level=logging.ERROR if needs_attention else logging.WARNING)
        if isinstance(error, DeliveryLost) or delivery.lost.is_set():
            return "unsettled"
        try:
            await delivery.release(30 if needs_attention else 5)
            return "released"
        except Exception as release_error:
            _queue_failure(
                "queue_release_deferred", release_error, "release",
                level=logging.WARNING if _retryable_delivery_error(release_error)
                else logging.ERROR)
            return "unsettled"

    async def process_delivery(self, delivery, *, worker_factory=None) -> str:
        """ACA only; return acked/released/quarantined/unsettled after owned work.

        Known transient delivery failures retain/release the wake. Unexpected
        configuration or invariant failures still reach the pool supervisor.
        """
        with host_invocation("worker", host_kind="aca"):
            return await self._process_delivery(delivery, worker_factory=worker_factory)

    async def _process_delivery(self, delivery, *, worker_factory=None) -> str:
        worker_factory = worker_factory or self.get_worker
        phase = "acquire"
        try:
            async with delivery:
                try:
                    phase = "decode"
                    try:
                        wake = decode_wake(delivery.body)
                    except ValueError:
                        phase = "quarantine"
                        await self.quarantine_wake(delivery.body)
                        await delivery.ack()
                        logger.warning("queue_wake_quarantined", extra={"telemetry": {
                            "reason": "invalid_wake"}})
                        return "quarantined"
                    phase = "worker_init"
                    worker = await worker_factory()
                    phase = "process"
                    result = await worker.process_session(wake.partition_key, wake.trace_id)
                    phase = "settle"
                    if result in {"drained", "rescheduled"}:
                        await delivery.ack()
                        return "acked"
                    elif result == "busy":
                        await delivery.release(5)
                        return "released"
                    else:
                        raise RuntimeError("unknown worker completion state")
                except Exception as error:
                    if not _can_defer_delivery(error):
                        raise
                    return await self._recover_delivery(delivery, error, phase)
        except Exception as error:
            if _can_defer_delivery(error):
                _queue_failure(
                    "queue_delivery_retry", error, phase,
                    level=logging.ERROR if _access_denied(error)
                    or isinstance(error, _QuarantineUnconfirmed) else logging.WARNING)
                return "unsettled"
            _queue_failure("queue_consumer_failed", error, phase, level=logging.ERROR)
            raise RuntimeError("queue_consumer_failed") from None

    @staticmethod
    async def _wait(stop, seconds):
        try:
            async with asyncio.timeout(seconds):
                await stop.wait()
        except TimeoutError:
            pass

    async def serve_maintenance(self, stop: asyncio.Event, *, interval_seconds=300):
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("maintenance interval must be finite and positive")
        while not stop.is_set():
            await self._wait(stop, interval_seconds)
            if not stop.is_set():
                await self.run_maintenance_once()

    async def run_maintenance_once(self):
        with host_invocation("maintenance", host_kind="aca"), \
                trace_scope(new=True) as trace, step("cloud.maintenance") as timing:
            result = await self.maintenance.run_once()
            if result.failed:
                timing.status = "degraded"
            elif result.timed_out:
                timing.status = "timeout"
            counts = {key: getattr(result, key) for key in (
                "scanned", "republished", "expired", "failed", "quarantined",
                "skipped", "busy", "timed_out")}
            logger.info("maintenance_summary", extra={"telemetry": {
                "trace_id": trace.trace_id, "boot_id": trace.boot_id,
                "segment_id": trace.segment_id, **counts}})
            return result

    async def stop_consumer(self, task):
        done, pending = await asyncio.wait([task], timeout=self.shutdown_seconds)
        for item in pending:
            item.cancel()
        results = await asyncio.gather(*done, *pending, return_exceptions=True)
        for error in results:
            if isinstance(error, Exception):
                self.healthy = self.ready = False
                logger.error("cloud_background_failed error_type=%s", type(error).__name__)

    async def aclose(self):
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self.ready = False
            self._stop.set()
            if self._tasks:
                _, pending = await asyncio.wait(self._tasks, timeout=self.shutdown_seconds)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*self._tasks, return_exceptions=True)
            async with self._worker_lock:
                try:
                    async with AsyncExitStack() as stack:
                        for resource in self._closeables:
                            stack.push_async_callback(resource.aclose)
                except Exception as error:
                    self.healthy = False
                    logger.error("cloud_cleanup_failed error_type=%s", type(error).__name__)
                    raise RuntimeError("cloud_cleanup_failed") from None


@observe_host_stage("connection")
def build_runtime(env=None, *, benchmark_allow_loopback=False) -> CloudRuntime:
    """Compose once from explicit SDK Bot auth and ADVISOR_* storage settings.

    Uses ADVISOR_BLOB_SERVICE_URL/QUEUE_SERVICE_URL, HISTORY_CONTAINER,
    JOB_CONTAINER, QUEUE_NAME, POISON_QUEUE_NAME and WORKER_PARALLELISM.
    ADVISOR_QUEUE_POLL_SECONDS is a shared idle budget (minimum/default 2 seconds).
    AzureCredentials owns the selected identity; clients borrow it. Teams-only
    hosts initialize models once on first get_worker. Opt-in benchmark hosts
    defer models until Teams/real_agent preparation; synthetic and saved-reply
    delivery need none. benchmark_allow_loopback is a programmatic local-test
    transport allowance, never an environment-controlled production fallback.
    """
    from azure.storage.blob.aio import ContainerClient
    from azure.storage.queue.aio import QueueClient
    from microsoft_agents.activity import load_configuration_from_env
    from microsoft_agents.authentication.msal import MsalConnectionManager
    from microsoft_agents.hosting.core import AgentAuthConfiguration, OutboundHostValidator
    from advisor_agent.durable.blob import BlobGateway
    from advisor_agent.durable.history import BlobHistoryStore
    from advisor_agent.durable.jobs import JobStore, InboxStore
    from advisor_agent.durable.maintenance import Maintenance
    from advisor_agent.durable.queue import OutboxDispatcher, SessionQueue
    from advisor_shared.azure_auth import AzureAuthSettings, AzureCredentials, make_entra_credential
    from teams_adapter.cloud.auth import ManagedCloudAdapter, _safe_sdk_logs
    from teams_adapter.cloud.ingress import CloudIngress
    from teams_adapter.cloud.benchmark import BenchmarkAuth, BenchmarkIngress, BenchmarkSettings

    env = dict(os.environ if env is None else env)
    benchmark_settings = BenchmarkSettings.from_env(env)
    settings = AzureAuthSettings.from_env(env)
    if not settings.blob_service_url or not settings.queue_service_url:
        raise ValueError("ADVISOR_BLOB_SERVICE_URL and ADVISOR_QUEUE_SERVICE_URL are required")
    parallelism = int(env.get("ADVISOR_WORKER_PARALLELISM", "4"))
    shutdown = float(env.get("ADVISOR_SHUTDOWN_SECONDS", "30"))
    poll_interval = float(env.get("ADVISOR_QUEUE_POLL_SECONDS", "2"))
    if (parallelism < 1 or not math.isfinite(shutdown) or shutdown <= 0
            or not math.isfinite(poll_interval) or poll_interval < 2):
        raise ValueError("invalid cloud lifecycle configuration")

    def name(key, default):
        value = env.get(key, default)
        if (not 3 <= len(value) <= 63 or "--" in value
                or re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", value) is None):
            raise ValueError(f"{key} must be a valid Azure storage resource name")
        return value

    jobs_name = name("ADVISOR_JOB_CONTAINER", "advisor-jobs")
    history_name = name("ADVISOR_HISTORY_CONTAINER", "advisor-history")
    queue_name = name("ADVISOR_QUEUE_NAME", "advisor-incoming")
    poison_name = name("ADVISOR_POISON_QUEUE_NAME", "advisor-incoming-poison")
    # The .NET host's LOGGING__* values are not Python SDK logging settings.
    sdk_env = {key: value for key, value in env.items()
               if key.split("__", 1)[0] in {"CONNECTIONS", "CONNECTIONSMAP"}}
    with _safe_sdk_logs("startup"):
        connections = MsalConnectionManager(**load_configuration_from_env(sdk_env))
    configured = connections.get_default_connection_configuration()
    if (configured.ANONYMOUS_ALLOWED or not configured.VALIDATE_ISSUER
            or not configured.CLIENT_ID or not configured.TENANT_ID):
        raise ValueError("cloud Bot authentication requires identity, tenant and issuer validation")
    # A standalone validation config accepts this bot audience, not other SDK connections.
    auth = AgentAuthConfiguration(
        client_id=configured.CLIENT_ID, tenant_id=configured.TENANT_ID,
        authority=configured.AUTHORITY, issuers=configured.ISSUERS,
        anonymous_allowed=False, validate_issuer=True)
    credentials = AzureCredentials(settings)
    credential = credentials.storage_credential()
    jobs_gateway = BlobGateway(ContainerClient(
        settings.blob_service_url, jobs_name, credential=credential))
    history_gateway = BlobGateway(ContainerClient(
        settings.blob_service_url, history_name, credential=credential))
    queue = SessionQueue(QueueClient(
        settings.queue_service_url, queue_name, credential=credential,
        message_encode_policy=None, message_decode_policy=None))
    poison = SessionQueue(QueueClient(
        settings.queue_service_url, poison_name, credential=credential,
        message_encode_policy=None, message_decode_policy=None))
    inbox = InboxStore(jobs_gateway)
    jobs = JobStore(jobs_gateway, bot_id=configured.CLIENT_ID, inbox=inbox)
    dispatcher = OutboxDispatcher(inbox, queue)
    history = BlobHistoryStore(history_gateway)
    maintenance = Maintenance(jobs, dispatcher, history=history, poison=poison)
    ingress = CloudIngress(
        jobs, dispatcher, bot_app_id=configured.CLIENT_ID, auth_config=auth,
        protocol_adapter=ManagedCloudAdapter(
            connection_manager=connections, host_validator=OutboundHostValidator(enabled=True)))
    closeables = [credentials, jobs_gateway, history_gateway, queue, poison]
    benchmark_ingress = BenchmarkIngress(
        jobs, dispatcher, settings=benchmark_settings,
        auth=BenchmarkAuth.from_env(env) if benchmark_settings.enabled else None,
        telemetry_provider=current_host_telemetry)
    benchmark_sink = None
    if benchmark_settings.enabled:
        from teams_adapter.cloud.benchmark_connector import BenchmarkReplySink
        callback_session = _LazyCallbackSession()
        callback_credential = None
        if benchmark_ingress.auth.mode == "entra":
            if settings.storage_auth_mode == "entra":
                callback_credential = credential
            else:
                callback_credential = make_entra_credential(
                    settings.entra_credential, settings.client_id)
                closeables.insert(1, _OwnedAsyncClose(callback_credential))
        benchmark_sink = BenchmarkReplySink(
            callback_session, env.get("BENCHMARK_RECEIVER_URL", ""),
            auth_mode=benchmark_ingress.auth.mode,
            credential=callback_credential,
            api_app_id=env.get("BENCHMARK_RECEIVER_AUDIENCE"),
            key=env.get("BENCHMARK_API_KEY") if benchmark_ingress.auth.mode == "key" else None,
            allow_loopback=benchmark_allow_loopback)
        closeables.append(callback_session)

    async def construct_owned_components():
        def construct_components():
            from advisor_agent.factory import build_components
            logging.getLogger("openai").setLevel(logging.INFO)
            return build_components(channel_name="teams", credentials=credentials)

        with trace_scope(new=True), step("cloud.components_init"):
            construction = asyncio.create_task(
                asyncio.to_thread(construct_components), name="advisor-components-init")
            try:
                components = await asyncio.shield(construction)
            except asyncio.CancelledError:
                # Thread work cannot be cancelled: join it and close its result.
                try:
                    components = await construction
                    await components.aclose()
                except Exception as error:
                    logger.error("cloud_worker_init_cleanup_failed error_type=%s",
                                 type(error).__name__)
                raise
            return components

    deferred = _DeferredComponents(construct_owned_components)
    if benchmark_settings.enabled:
        closeables.append(deferred)

    async def build_worker():
        from advisor_agent.durable.worker import SessionWorker
        from teams_adapter.cloud.executor import TeamsTurnExecutor
        if benchmark_settings.enabled:
            from teams_adapter.cloud.benchmark_connector import BenchmarkTurnExecutor
            benchmark = BenchmarkTurnExecutor(
                benchmark_ingress.runs, benchmark_sink, components=deferred,
                connection_manager=connections, bot_app_id=configured.CLIENT_ID,
                host_telemetry=runtime.host_telemetry)
            executor = _TargetExecutor(
                TeamsTurnExecutor(deferred, connections, configured.CLIENT_ID),
                benchmark, deferred)
            return SessionWorker(jobs, history, dispatcher, executor)
        components = await construct_owned_components()
        try:
            worker = SessionWorker(
                jobs, history, dispatcher,
                TeamsTurnExecutor(components, connections, configured.CLIENT_ID))
        except BaseException:
            await components.aclose()
            raise
        runtime._closeables.append(components)
        return worker

    runtime = CloudRuntime(
        ingress=ingress, queue=queue, maintenance=maintenance, worker_factory=build_worker,
        closeables=closeables, benchmark_ingress=benchmark_ingress,
        parallelism=parallelism, shutdown_seconds=shutdown, poll_interval=poll_interval)
    return runtime


_runtime: CloudRuntime | None = None
_runtime_lock = threading.Lock()


def get_runtime() -> CloudRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = build_runtime()
        return _runtime


def get_ingress() -> "CloudIngress":
    return get_runtime().ingress


def get_benchmark_ingress():
    """Disabled routes stay 404 without touching storage, model or authentication."""
    from teams_adapter.cloud.benchmark import BenchmarkIngress, BenchmarkSettings
    settings = BenchmarkSettings.from_env(os.environ)
    if not settings.enabled:
        return BenchmarkIngress(None, None, settings=settings)
    return get_runtime().benchmark_ingress


async def get_worker() -> "SessionWorker":
    return await get_runtime().get_worker()


def get_maintenance() -> "Maintenance":
    return get_runtime().maintenance
