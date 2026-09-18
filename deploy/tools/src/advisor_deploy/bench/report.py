"""Persist safe observations and report only the measured application-link window."""
from dataclasses import asdict, fields
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from advisor_deploy.bench.models import OUTCOMES, Sample, finite_nonnegative
from advisor_deploy.bench.plan import RunPlan
from advisor_deploy.bench.runner import RunResult, ZeroProof
from advisor_deploy.bench.sink import EvidenceSample
from advisor_deploy.bench.stats import summarize
from advisor_deploy.bench.cold import ColdCategory
from advisor_shared.benchmark import BootObservation, SafeTelemetry, ScaleGroup, WireModel


Nonnegative = Annotated[float, Field(ge=0)]


class DriverObservation(WireModel):
    cpu_seconds: Nonnegative | None = None
    peak_memory_mb: Nonnegative | None = None
    peak_loop_lag_seconds: Nonnegative | None = None
    max_cpu_capacity_ratio: Nonnegative | None = None
    observer_clock: Literal["perf_counter"] | None = None
    observer_clock_resolution_seconds: Nonnegative | None = None
    memory_measurement: Literal["process_high_watermark_or_unavailable"] | None = None
    evidence_operations: int = Field(default=0, ge=0)
    evidence_io_seconds: Nonnegative | None = None
    max_evidence_io_seconds: Nonnegative | None = None


class ObserverCounters(WireModel):
    http_attempts: int = Field(default=0, ge=0)
    http_429: int = Field(default=0, ge=0)
    duplicate_replies: int = Field(default=0, ge=0)
    late_replies: int = Field(default=0, ge=0)
    typing_events: int = Field(default=0, ge=0)
    unstarted_intents: int = Field(default=0, ge=0)
    callback_processing_ms: list[Nonnegative] = Field(default_factory=list)
    measurement_endpoint: Literal[
        "complete_callback_body_received_before_authentication",
    ] | None = None
    stop_reasons: list[Literal[
        "target_reached", "submission_limit", "request_budget", "request_deadline",
        "host_failure", "driver_saturated", "protocol_error", "observer_failure",
        "observer_evidence_failure", "authentication_unavailable", "retry_after_exceeds_deadline",
        "target_rejection", "persistent_throttle", "persistent_backlog", "driver_interrupted",
        "zero_not_observed", "deployment_expired", "preparation_failure", "finished",
    ]] = Field(default_factory=list)


class ColdGroup(WireModel):
    category: ColdCategory
    proof: ZeroProof
    boot: BootObservation | None = None


class ColdObservation(WireModel):
    category: ColdCategory = "unverified"
    groups: dict[ScaleGroup, ColdGroup] = Field(default_factory=dict)


def read_result(value: dict) -> RunResult:
    try:
        if not isinstance(value, dict) or not set(value) <= {item.name for item in fields(RunResult)}:
            raise ValueError("unknown result fields")
        data = dict(value)
        for sample in data["samples"]:
            EvidenceSample.model_validate(sample)
        data["samples"] = [Sample(**sample) for sample in data["samples"]]
        result = RunResult(**data)
        DriverObservation.model_validate(result.driver)
        ObserverCounters.model_validate(result.observations)
        ColdObservation.model_validate(result.cold)
        finite_nonnegative(result.window_seconds, "window_seconds")
        finite_nonnegative(result.submission_seconds, "submission_seconds")
        if result.window_seconds <= 0 or result.submission_seconds > result.window_seconds:
            raise ValueError("invalid observation window")
        if len({sample.request_id for sample in result.samples}) != len(result.samples):
            raise ValueError("duplicate request samples")
        if any(sample.started_s > result.window_seconds for sample in result.samples):
            raise ValueError("sample starts after observation window")
        if "target_url" in result.plan:
            RunPlan.model_validate(result.plan, context={"historical": True})
        elif not set(result.plan) <= {"target_completions", "min_seconds"}:
            raise ValueError("unrecognized plan summary")
        for item in result.telemetry:
            SafeTelemetry.model_validate({
                key: val for key, val in item.items() if key not in {"request_id", "job_id"}})
        return result
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid benchmark result") from error


def write_result(result: RunResult, path: Path) -> None:
    data = asdict(result)
    read_result(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    partial.replace(path)


def render_report(result: RunResult) -> str:
    summary = summarize(result.samples, window_seconds=result.window_seconds)
    complete = sum(count for status, count in summary.counts.items() if status != "censored")
    target = (result.plan.get("warmup_requests", 3) if result.profile == "warmup" else
              result.plan.get("warm_target", 100) if result.profile == "warm" else
              1 if result.profile == "cold" else result.plan.get("target_completions", 50))
    minimum = 0 if result.profile in {"cold", "warmup"} else result.plan.get("min_seconds", 180)
    sufficient = complete >= target and result.submission_seconds >= minimum
    mode = "LOCAL SYNTHETIC HTTP BOUNDARY" if result.plan.get("local_test") else "APPLICATION LINK"
    rows = [
        f"# Benchmark {result.run_id}",
        "",
        f"**{mode}** — not Teams-client latency; not an SLA or an absolute platform maximum.",
        f"Profile: `{result.profile}`; requested outstanding-question limit: {result.concurrency}.",
        ("**Observer evidence failure: this run cannot establish target capacity.**"
         if result.stop_reason == "observer_evidence_failure" else
         "Completion endpoint: complete bounded HTTP reply body received by the observer; "
         "accepted only after authentication and complete-message validation."),
        f"Observation: {result.started_at} to {result.ended_at}.",
        f"Actual window: **{result.window_seconds:.3f} seconds**, including drain; "
        f"submission window: {result.submission_seconds:.3f} seconds.",
        f"Stop: `{result.stop_reason}`. Sampling: **{'sufficient' if sufficient else 'insufficient'}** "
        f"({complete}/{target} terminal observations; minimum {minimum} submission seconds).",
        f"Attempted: {summary.attempted}; censored: {summary.counts['censored']}; "
        f"after-window completions: {summary.completed_after_window}.",
        f"Full answers (success + degraded)/actual window: **{summary.answer_rps:.6g} answers/s**.",
        f"Successes/actual window: **{summary.success_rps:.6g} successes/s**; "
        f"successes/attempted: {summary.success_rate if summary.success_rate is not None else 'n/a'}.",
        "",
        "| Outcome | Count | Mean ms | P50 ms | P95 ms | P99 ms | Min ms | Max ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]

    def number(value):
        return "n/a" if value is None else f"{value:.3f}"

    for outcome in OUTCOMES:
        distribution = summary.distributions[outcome]
        durations = [sample.duration_ms for sample in result.samples
                     if sample.status == outcome and sample.duration_ms is not None]
        values = [distribution.mean_ms, distribution.p50_ms, distribution.p95_ms,
                  distribution.p99_ms, min(durations) if durations else None,
                  max(durations) if durations else None]
        rows.append(f"| {outcome} | {summary.counts[outcome]} | "
                    + " | ".join(number(value) for value in values) + " |")
    rows += [
        "",
        "ACKs and typing events are not completions. Censored requests have no fabricated duration.",
        "Failure, degradation, unknown delivery and unfinished work remain separate.",
        f"Cold classification: **{result.cold.get('category', 'unverified')}**.",
        "Natural-zero requires supplied control-plane zero proof and first-workload/new-boot "
        "evidence for every required scale group. Absence of evidence is unverified.",
        "The first_request marker means first workload on the observed boot, not a platform probe; "
        "probe exclusion alone is not cold-start evidence.",
        "Small cold samples and tail quantiles are exploratory; no excluded adverse samples.",
        f"Notes: {', '.join(summary.notes) or 'none'}.",
        "",
        "## Configuration and limitations",
        "",
        f"Dataset SHA-256: `{result.plan.get('dataset_sha256', 'not provided')}`; "
        f"seed: {result.plan.get('seed', 'not provided')}; "
        f"history mode: `{result.plan.get('history_mode', 'not provided')}`.",
        "Per-request safe telemetry and supplied cold evidence are in the raw result. "
        "Missing host counts, resource utilization, stage timings or dependency calls are unknown, "
        "not zero. Target-generated cross-process timings are not subtracted from observer clocks.",
        "Driver utilization is measured separately; a driver stop is not a target capacity limit.",
        "Observer authentication/validation and evidence I/O are recorded separately from "
        "body-reception latency. Durable write-ahead intents precede the per-request clock; "
        "journal backpressure can limit throughput and must not be attributed to target capacity.",
        "Compare only matching workload/history hashes, model versions, budgets and host settings. "
        "Model/Entra/configuration/capacity and cloud functional checks remain deployment gates.",
        "No cloud cost or capacity conclusion is produced by local synthetic tests.",
    ]
    return "\n".join(rows) + "\n"
