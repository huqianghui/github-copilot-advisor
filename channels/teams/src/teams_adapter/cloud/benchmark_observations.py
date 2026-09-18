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
come from completed ``llm.completion`` spans. HTTP model retry/throttle counts,
cache usage, global backlog and unobserved resource metrics remain unknown.
"""
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from advisor_shared.benchmark import DependencyObservation, SafeTelemetry, StageObservation
from advisor_shared.events import AdvisorEvent
from advisor_shared.telemetry import TraceContext, current_trace


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
    outcomes = {
        "success": "success", "empty": "empty", "degraded": "degraded",
        "not_configured": "unknown", "error": "failed",
        "timeout": "failed", "cancelled": "failed",
    }
    stages, completions = [], []
    timings = observation.trace.timings if observation.trace is not None else []
    for timing in timings:
        if timing.name in names:
            stages.append(StageObservation(
                name=names[timing.name], duration_ms=timing.duration_ms,
                outcome=outcomes[timing.status]))
        if timing.name == "llm.completion":
            completions.append(timing)
    counts = defaultdict(lambda: [0, 0])
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
