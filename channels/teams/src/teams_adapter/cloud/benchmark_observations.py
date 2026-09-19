"""Allowlisted projections of actual application observations, never log parsing.

Host providers are snapshot accessors: first_request identifies the first
workload invocation on the observed process boot, not the first benchmark
request. Ordinary traffic, authentication, registration, queue and timer work
consume this history. Trusted host-designated health/readiness probes remain
observed but emit no boot observation and neither consume nor reset workload
history. Accessors do not mutate counters; missing providers leave these fields
unknown. This flag alone is not cold-start proof: external zero/new-boot
evidence is still required.

Search dependency counts are observed source-operation attempts (including
observed 429s), not inferred lower-level HTTP retries. Model rounds and usage
come from completed ``llm.completion`` spans. The model dependency instead counts
actual completed diagnostic HTTP-client attempts for chat AND embeddings,
including SDK retries/redirects, and observed HTTP 429 responses. It is not a
model-round count or proof that the server accepted each request. DEBUG-off,
absent, conflicting or capacity-exceeded attempt evidence stays omitted rather
than becoming zero/partial counts. No headers, URLs or bodies enter this path.
Cache usage, global backlog and unobserved resource metrics remain unknown.

Wire stages describe operations completed before serialization, including actual
history_read at preparation and history_commit at send when observed in that
attempt. Full ingress and final HTTP send complete too late for their original
snapshot: StageCompletion logs permit post-run backfill without changing original
callback arrival latency. Missing/uningested logs remain unknown, never zero.
``benchmark_trace_link`` also joins host ingress to job traces.

Coverage boundaries: ingress auth is verify(), connection/worker_init are actual
host/client construction (not DNS/TLS), history_setup is seeded bootstrap only,
agent is the whole core turn, model/search/render are their existing nested spans.
history.read covers archive loading from an already leased context, not earlier
context acquisition. Inbound network transit, individual connection handshakes,
and context-lease/projection acquisition are not separately observed. Final-send
and callback-auth completion logs cannot appear in that same HTTP callback.
These overlapping intervals are not an additive end-to-end latency decomposition.
"""
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
from typing import Literal

from pydantic import field_validator

from advisor_shared.benchmark import (
    DependencyObservation, Identifier, SafeTelemetry, StageObservation, WireModel,
)
from advisor_shared.events import AdvisorEvent
from advisor_shared.telemetry import TraceContext, current_trace

logger = logging.getLogger(__name__)

_OUTCOMES = {
    "success": "success", "empty": "empty", "degraded": "degraded",
    "not_configured": "unknown", "error": "failed",
    "timeout": "failed", "cancelled": "failed",
}


class StageCompletion(WireModel):
    """Log-only export; select these fields from the ordinary logging envelope.

    Join by job_id AND trace_id. Deduplicate by (boot_id, segment_id, span_id);
    different spans are real attempts. Do not double-count an operation already
    represented in callback stages. Process span IDs are not platform-instance
    evidence and cannot establish a cold start. No request/run/body fields exist.
    """

    event: Literal["benchmark_stage_completed"]
    version: Literal[1]
    trace_id: Identifier
    job_id: Identifier
    boot_id: Identifier
    segment_id: Identifier
    span_id: Identifier
    stage: StageObservation

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("completion version must be an integer")
        return value


def emit_stage_completion(**values) -> None:
    event = StageCompletion(event="benchmark_stage_completed", version=1, **values)
    logger.info("benchmark_stage_completed", extra={"telemetry": event.model_dump()})


def completed_history_telemetry(
    payload, name: Literal["history_read", "history_commit"],
) -> SafeTelemetry:
    trace = current_trace()
    if trace is None or trace.trace_id != payload.trace_id:
        return SafeTelemetry()
    source = {"history_read": "history.read", "history_commit": "history.commit"}[name]
    return SafeTelemetry(stages=[
        StageObservation(name=name, duration_ms=timing.duration_ms,
                         outcome=_OUTCOMES[timing.status])
        for timing in trace.timings
        if timing.name == source and timing.attributes.get("job_id") == payload.job_id
    ])


def link_ingress_trace(payload, telemetry: SafeTelemetry) -> None:
    """Call only after validating the stored reservation against the payload."""
    if telemetry.trace_id is not None:
        logger.info("benchmark_trace_link", extra={"telemetry": {
            "trace_id": payload.trace_id, "ingress_trace_id": telemetry.trace_id}})


@dataclass
class PreparationObservations:
    trace: TraceContext | None = None
    events: list[AdvisorEvent] = field(default_factory=list)


_capture: ContextVar[PreparationObservations | None] = ContextVar(
    "benchmark_preparation_observations", default=None)


@contextmanager
def capture_preparation():
    observation = PreparationObservations()
    token = _capture.set(observation)
    try:
        yield observation
    finally:
        _capture.reset(token)


class ObservedComponents:
    """Leave the actual core, SDK and event sink intact; retain their trace scope."""

    def __init__(self, components):
        self.components = components

    def new_core(self, sessions, *, event_sink):
        captured = _capture.get()
        if captured is None:
            raise RuntimeError("benchmark preparation observation scope is missing")
        captured.trace = current_trace()

        def observe(event):
            captured.events.append(event)
            event_sink(event)

        return self.components.new_core(sessions, event_sink=observe)


def host_snapshot(provider, *args) -> SafeTelemetry:
    if provider is None:
        return SafeTelemetry()
    snapshot = SafeTelemetry.model_validate(provider(*args))
    return snapshot.model_copy(deep=True)


def merge_telemetry(*snapshots: SafeTelemetry) -> SafeTelemetry:
    values, boots = {}, {}
    stages, dependencies, paths = [], [], []
    for snapshot in snapshots:
        stages.extend(snapshot.stages)
        dependencies.extend(snapshot.dependencies)
        paths.extend(path for path in snapshot.paths if path not in paths)
        for boot in snapshot.boots:
            key = boot.group, boot.instance_id, boot.boot_id
            earlier = boots.get(key)
            boots[key] = boot.model_copy(update={
                "first_request": boot.first_request or bool(earlier and earlier.first_request)})
        for name, value in snapshot.model_dump(exclude={
            "stages", "dependencies", "paths", "boots",
        }).items():
            if value is not None:
                if name in {"host_crashed", "host_oom"}:
                    values[name] = bool(value or values.get(name))
                else:
                    values[name] = value
    return SafeTelemetry(
        **values, stages=stages, dependencies=dependencies,
        paths=paths, boots=list(boots.values()))


def preparation_telemetry(observation: PreparationObservations, *, synthetic=False) -> SafeTelemetry:
    names = {
        "agent.turn": "agent", "llm.completion": "model", "teams.render": "render",
        "search.kb": "kb", "search.github-live": "github", "search.web": "web",
        "benchmark.synthetic": "synthetic",
    }
    stages, completions = [], []
    timings = observation.trace.timings if observation.trace is not None else []
    for timing in timings:
        if timing.name in names:
            stages.append(StageObservation(
                name=names[timing.name], duration_ms=timing.duration_ms,
                outcome=_OUTCOMES[timing.status]))
        if timing.name == "llm.completion":
            completions.append(timing)
    counts = defaultdict(lambda: [0, 0])
    attempts = observation.trace.model_http_attempts if observation.trace is not None else None
    if attempts:
        counts["model"] = [len(attempts), sum(item.http_status == 429 for item in attempts)]
    paths = []
    source_paths = {"kb": "kb", "github-live": "github", "web": "web"}
    source_dependencies = {"kb": "search", "github-live": "github", "web": "web"}
    event_paths = {
        "kb_hit": "kb", "live_hit": "github", "web": "web",
        "generic_advice": "generic", "escalated": "escalation",
    }
    for event in observation.events:
        for attempt in event.search_attempts:
            if attempt.status == "not_configured":
                continue
            dependency = source_dependencies[attempt.source]
            counts[dependency][0] += 1
            counts[dependency][1] += int(attempt.http_status == 429)
            path = source_paths[attempt.source]
            if path not in paths:
                paths.append(path)
        if not synthetic:
            path = event_paths[event.stage]
            if path not in paths:
                paths.append(path)
    usage = {}
    for name in ("input_tokens", "output_tokens"):
        observed = [item.attributes.get(name) for item in completions]
        if observed and all(type(count) is int and count >= 0 for count in observed):
            usage[name] = sum(observed)
    return SafeTelemetry(
        stages=stages, paths=paths, model_rounds=len(completions) or None,
        dependencies=[DependencyObservation(dependency=name, calls=count, throttled=throttled)
                      for name, (count, throttled) in counts.items()],
        **usage)
