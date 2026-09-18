"""Observed host evidence, independent of benchmark request counting.

Enter host_invocation at every application HTTP/queue/timer boundary, including
health and rejected requests. Snapshot accessors never advance counters.
first_request means the first WORKLOAD invocation on the observed process boot,
reported in its host scale group, never the first benchmark request. Trusted
health/readiness route handlers mark workload=False: probes remain observed and
logged but neither consume nor reset workload history. Ordinary requests,
authentication attempts, registration, queue and timer work all consume it.
Cold classification still requires external zero-instance/new-boot evidence;
this flag alone never proves a platform cold start.
Cloud instance IDs come only from platform host metadata. Without it, boot
observations remain absent; hostname/PID placeholders cannot prove an instance.
No backlog/resource/cache/token values are guessed.

Only completed host stages are exported. The target's existing preparation
observer handles real model/search/render spans and observed token usage.
queue_wait is persisted accepted_at UTC to the first validated job observation
in this worker attempt, including admission publication, scheduling, worker setup
and validation. It is not isolated queue-server transit and can include prior
retry delay. Clock-skew/invalid intervals are omitted, never clamped.
Full ingress completes after its reservation snapshot: host_invocation_completed
logs that local interval with its trace_id; benchmark_trace_link joins it to the
job trace. Stages overlap (for example queue_wait and setup); do not sum them.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import math
import os
import re
import threading
from time import perf_counter
from uuid import uuid4

from advisor_shared.benchmark import BootObservation, SafeTelemetry, StageObservation
from advisor_shared.telemetry import TraceContext, current_trace, trace_scope
from teams_adapter.cloud.benchmark_observations import emit_stage_completion

logger = logging.getLogger(__name__)
_current: ContextVar["HostInvocation | None"] = ContextVar("host_invocation", default=None)


@dataclass
class HostInvocation:
    owner: "HostObservations"
    kind: str
    host_kind: str
    first_request: bool
    ordinal: int
    trace_id: str
    workload: bool = True
    workload_ordinal: int | None = None
    stages: list[StageObservation] = field(default_factory=list)
    status: int | None = None
    first_job_id: str | None = None
    current_job_id: str | None = None
    jobs: set[str] = field(default_factory=set)
    job_stages: dict[str, list[StageObservation]] = field(default_factory=dict)
    ingress_job: tuple[str, str] | None = None

    def http_status(self, status: int) -> None:
        if type(status) is not int or not 100 <= status <= 599:
            raise ValueError("invalid observed HTTP status")
        self.status = status

    def snapshot(self, payload=None) -> SafeTelemetry:
        primary = True
        if payload is not None and self.kind == "worker":
            if payload.job_id not in self.jobs:
                return SafeTelemetry()
            primary = payload.job_id == self.first_job_id
        group = ("app" if self.host_kind == "aca" else
                 self.kind if self.kind in {"http", "worker"} else None)
        boots = []
        if self.workload and group is not None and self.owner.instance_id is not None:
            boots.append(BootObservation(
                group=group, boot_id=self.owner.boot_id, instance_id=self.owner.instance_id,
                first_request=self.first_request and primary))
        stages = [stage for stage in self.stages
                  if primary or stage.name not in {"connection", "worker_init"}]
        if payload is not None:
            stages.extend(self.job_stages.get(payload.job_id, ()))
        return SafeTelemetry(
            trace_id=payload.trace_id if payload is not None else self.trace_id,
            boots=boots, stages=stages).model_copy(deep=True)


class HostObservations:
    def __init__(self, *, environment=None, clock=perf_counter,
                 utc_clock=lambda: datetime.now(timezone.utc)):
        environment = os.environ if environment is None else environment
        self.boot_id = TraceContext().boot_id
        self.process_id = os.getpid()
        self.clock = clock
        self.utc_clock = utc_clock
        self.instance_id = None
        self.instance_source = None
        for key in ("CONTAINER_APP_REPLICA_NAME", "WEBSITE_INSTANCE_ID"):
            if key in environment:
                value = environment[key]
                if (isinstance(value, str)
                        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value)):
                    self.instance_id, self.instance_source = value, key
                else:
                    logger.warning("host_instance_metadata_invalid")
                break
        self._lock = threading.Lock()
        self._invocations = 0
        self._workload_invocations = 0

    def identity(self) -> dict:
        return {
            "boot_id": self.boot_id, "process_id": self.process_id,
            "instance_id": self.instance_id, "instance_source": self.instance_source,
        }

    @contextmanager
    def invocation(self, kind, *, host_kind, workload=True):
        if kind not in {"http", "worker", "maintenance"} or host_kind not in {"aca", "functions"}:
            raise ValueError("invalid host invocation boundary")
        if type(workload) is not bool or (not workload and kind != "http"):
            raise ValueError("only explicit HTTP probe boundaries may exclude workload")
        existing = _current.get()
        if existing is not None and existing.kind == kind:
            yield existing
            return
        with self._lock:
            self._invocations += 1
            ordinal = self._invocations
            if workload:
                self._workload_invocations += 1
                workload_ordinal = self._workload_invocations
            else:
                workload_ordinal = None
        started = self.clock()
        failed = False
        with trace_scope(new=True) as trace:
            ingress_span = uuid4().hex[:16] if kind == "http" else None
            invocation = HostInvocation(
                self, kind, host_kind, workload_ordinal == 1, ordinal, trace.trace_id,
                workload=workload, workload_ordinal=workload_ordinal)
            token = _current.set(invocation)
            try:
                yield invocation
            except BaseException:
                failed = True
                raise
            finally:
                try:
                    duration = (self.clock() - started) * 1000
                    outcome = ("failed" if failed or (invocation.status or 0) >= 400 else
                               "unknown" if kind == "http" and invocation.status is None else "success")
                    if kind == "http":
                        stage = StageObservation(name="ingress", duration_ms=duration, outcome=outcome)
                        invocation.stages.append(stage)
                        if invocation.ingress_job is not None:
                            job_id, job_trace_id = invocation.ingress_job
                            emit_stage_completion(
                                trace_id=job_trace_id, job_id=job_id, boot_id=trace.boot_id,
                                segment_id=trace.segment_id, span_id=ingress_span, stage=stage)
                    logger.info("host_invocation_completed", extra={"telemetry": {
                        **self.identity(), "kind": kind,
                        "trace_id": invocation.trace_id,
                        "name": "ingress" if kind == "http" else f"{kind}_invocation",
                        "outcome": outcome,
                        "probe": not workload, "first_request": workload_ordinal == 1,
                        "workload_index": workload_ordinal,
                        "invocation_index": ordinal, "duration_ms": duration,
                        "http_status": invocation.status,
                    }})
                finally:
                    _current.reset(token)

    def snapshot(self, payload=None) -> SafeTelemetry:
        current = _current.get()
        return current.snapshot(payload) if current is not None and current.owner is self else SafeTelemetry()


_observations = HostObservations()


def host_identity() -> dict:
    """Safe observed identity, including its source; never claims a first request."""
    return _observations.identity()


def host_invocation(kind, *, host_kind, workload=True):
    return _observations.invocation(kind, host_kind=host_kind, workload=workload)


def current_host_telemetry(payload=None) -> SafeTelemetry:
    """Read only. Unknown/out-of-boundary evidence stays absent."""
    current = _current.get()
    return current.snapshot(payload) if current is not None else SafeTelemetry()


def observe_ingress_job(payload) -> None:
    """Bind only a validated, durably admitted benchmark job, without its body."""
    current = _current.get()
    if (current is not None and current.kind == "http" and current.workload
            and payload.target == "benchmark"):
        current.ingress_job = payload.job_id, payload.trace_id


def observe_job(payload) -> None:
    """Bind real validated jobs; repeated validation does not create observations.

    Within a drained batch, only its first observed job receives initialization
    cost/first-request attribution. Reads of telemetry never claim that slot.
    """
    current = _current.get()
    if current is not None and current.kind == "worker":
        current.current_job_id = payload.job_id
        if payload.job_id in current.jobs:
            return
        if current.first_job_id is None:
            current.first_job_id = payload.job_id
        current.jobs.add(payload.job_id)
        logger.info("host_job_trace_link", extra={"telemetry": {
            "trace_id": payload.trace_id, "host_trace_id": current.trace_id}})
        try:
            accepted, observed = payload.accepted_at, current.owner.utc_clock()
            if (not isinstance(accepted, datetime) or not isinstance(observed, datetime)
                    or accepted.utcoffset() is None or observed.utcoffset() is None):
                raise ValueError("invalid timestamp")
            duration = (observed.astimezone(timezone.utc)
                        - accepted.astimezone(timezone.utc)).total_seconds() * 1000
            if not math.isfinite(duration):
                raise ValueError("invalid interval")
        except (ValueError, TypeError, OverflowError):
            reason = "invalid_timestamp"
        else:
            reason = "clock_skew" if duration < 0 else None
        if reason is not None:
            logger.warning("queue_wait_omitted", extra={"telemetry": {
                "trace_id": payload.trace_id, "reason": reason}})
            return
        current.job_stages[payload.job_id] = [
            StageObservation(name="queue_wait", duration_ms=duration, outcome="success")]


@contextmanager
def observe_host_stage(name):
    if name not in {"connection", "worker_init"}:
        raise ValueError("unsupported host setup stage")
    current = _current.get()
    if current is None:
        yield
        return
    started = current.owner.clock()
    outcome = "success"
    try:
        yield
    except BaseException:
        outcome = "failed"
        raise
    finally:
        stage = StageObservation(
            name=name, duration_ms=(current.owner.clock() - started) * 1000, outcome=outcome)
        if current.kind == "worker" and current.current_job_id is not None:
            current.job_stages.setdefault(current.current_job_id, []).append(stage)
        else:
            current.stages.append(stage)
        trace = current_trace()
        logger.info("host_stage_completed", extra={"telemetry": {
            "trace_id": trace.trace_id if trace is not None else current.trace_id,
            **stage.model_dump()}})
