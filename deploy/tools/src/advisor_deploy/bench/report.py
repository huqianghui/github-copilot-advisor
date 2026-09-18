"""Report observed application-link and stage timings without inferring missing work."""
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Annotated, Literal, get_args

from pydantic import Field, field_validator

from advisor_deploy.bench.models import OUTCOMES, Sample, finite_nonnegative
from advisor_deploy.bench.plan import RunPlan
from advisor_deploy.bench.runner import RunResult, ZeroProof
from advisor_deploy.bench.sink import EvidenceSample
from advisor_deploy.bench.stats import Distribution, summarize
from advisor_deploy.bench.cold import ColdCategory
from advisor_shared.benchmark import (
    BootObservation, Identifier, SafeTelemetry, ScaleGroup, StageObservation, WireModel,
)


Nonnegative = Annotated[float, Field(ge=0)]
STAGE_NAMES = get_args(StageObservation.model_fields["name"].annotation)
STAGE_OUTCOMES = get_args(StageObservation.model_fields["outcome"].annotation)
MAX_STAGE_LOG_BYTES = 32 * 1024 * 1024
MAX_STAGE_LOG_RECORDS = 100000
COMPLETED_LOG_STAGES = frozenset({"ingress", "history_read", "history_commit", "send"})


def _log_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValueError("invalid log timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("invalid log timestamp")
    return parsed


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


class RequestTelemetry(SafeTelemetry):
    request_id: Identifier
    job_id: Identifier | None = None


class ImportedStageCompletion(WireModel):
    """The fixed StageCompletion export contract, without a Teams runtime dependency."""
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
            raise ValueError("stage completion version must be integer 1")
        return value

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.boot_id, self.segment_id, self.span_id


class TimedStageCompletion(WireModel):
    """Collection metadata around the unchanged runtime StageCompletion DTO."""
    timestamp: str
    completion: ImportedStageCompletion

    @field_validator("timestamp")
    @classmethod
    def utc_emission_time(cls, value):
        if _log_timestamp(value).utcoffset() != timedelta(0):
            raise ValueError("log emission timestamp must be UTC")
        return value


@dataclass(frozen=True)
class StageLogImport:
    source_path: Path
    source_sha256: str
    records: tuple[ImportedStageCompletion, ...]
    raw_record_count: int
    duplicate_count: int
    window_start: str | None
    window_end: str | None
    emission_timestamps: tuple[str, ...] = ()


def load_stage_logs(path: Path, *, window_start: str | None = None,
                    window_end: str | None = None) -> StageLogImport:
    start = end = None
    try:
        if (window_start is None) != (window_end is None):
            raise ValueError()
        if window_start is not None and window_end is not None:
            start, end = _log_timestamp(window_start), _log_timestamp(window_end)
            if start > end:
                raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid stage-log window") from None

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError()
            value[key] = item
        return value

    def nonfinite(value):
        raise ValueError()

    try:
        source = Path(path).resolve()
        with source.open("rb") as stream:
            raw = stream.read(MAX_STAGE_LOG_BYTES + 1)
        if len(raw) > MAX_STAGE_LOG_BYTES:
            raise ValueError()
        values = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                            parse_constant=nonfinite)
        if not isinstance(values, list) or len(values) > MAX_STAGE_LOG_RECORDS:
            raise ValueError()
        timestamps: tuple[str, ...] = ()
        if values and isinstance(values[0], dict) and set(values[0]) == {"timestamp", "completion"}:
            collected = [TimedStageCompletion.model_validate(value) for value in values]
            records = [value.completion for value in collected]
            timestamps = tuple(value.timestamp for value in collected)
            if start is not None and end is not None and any(
                not start <= _log_timestamp(value) <= end for value in timestamps
            ):
                raise ValueError()
        else:
            records = [ImportedStageCompletion.model_validate(value) for value in values]
        if any(record.stage.name not in COMPLETED_LOG_STAGES for record in records):
            raise ValueError()
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError("invalid completed stage-log import") from None
    unique: dict[tuple[str, str, str], ImportedStageCompletion] = {}
    for record in records:
        earlier = unique.get(record.identity)
        if earlier is not None and earlier != record:
            raise ValueError("conflicting stage completion identity")
        unique[record.identity] = record
    return StageLogImport(
        source, sha256(raw).hexdigest(), tuple(unique.values()), len(records),
        len(records) - len(unique), window_start, window_end, timestamps)


@dataclass(frozen=True)
class ReportStage:
    observation: StageObservation
    source: Literal["callback", "completed log"]


def _join_stage_logs(records: dict[str, RequestTelemetry], imported: StageLogImport | None):
    joined = {request_id: [ReportStage(stage, "callback") for stage in record.stages]
              for request_id, record in records.items()}
    superseded: dict[tuple[str, str], int] = {}
    if imported is None:
        return joined, superseded
    correlations = {}
    for request_id, record in records.items():
        if record.job_id is None or record.trace_id is None:
            continue
        pair = record.job_id, record.trace_id
        if pair in correlations:
            raise ValueError("ambiguous stage-log correlation")
        correlations[pair] = request_id
    groups: dict[tuple[str, str], list[StageObservation]] = {}
    for record in imported.records:
        request_id = correlations.get((record.job_id, record.trace_id))
        if request_id is None:
            raise ValueError("unmatched stage-log correlation")
        groups.setdefault((request_id, record.stage.name), []).append(record.stage)
    for (request_id, name), stages in groups.items():
        original = joined[request_id]
        superseded[request_id, name] = sum(item.observation.name == name for item in original)
        # Callback stages lack span IDs. Source precedence is explicit, not fuzzy deduplication.
        joined[request_id] = [item for item in original if item.observation.name != name] + [
            ReportStage(stage, "completed log") for stage in stages]
    return joined, superseded


def _telemetry_by_request(result: RunResult) -> dict[str, RequestTelemetry]:
    requests = {sample.request_id for sample in result.samples}
    records: dict[str, RequestTelemetry] = {}
    try:
        for item in result.telemetry:
            record = RequestTelemetry.model_validate(item)
            if record.request_id not in requests or record.request_id in records:
                raise ValueError()
            records[record.request_id] = record
    except (ValueError, TypeError):
        raise ValueError("invalid benchmark telemetry correlation") from None
    return records


def _number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _stage_report_rows(result: RunResult, imported: StageLogImport | None = None) -> list[str]:
    records = _telemetry_by_request(result)
    joined, superseded = _join_stage_logs(records, imported)
    scenario = result.plan.get("scenario", "unknown")
    if not isinstance(scenario, str) or scenario not in {"synthetic", "real_agent", "unknown"}:
        raise ValueError("invalid benchmark scenario")
    rows = [
        "",
        "## Observed per-stage latency",
        "",
        f"Scenario: `{scenario}`; profile: `{result.profile}`; run: `{result.run_id}`.",
        "This section uses only supplied StageObservation values for this run's requests. "
        "Synthetic and real-Agent scenarios are not pooled or inferred from stage names.",
        "Coverage is distinct requests with any observation of a stage / all attempted requests "
        "in that request-outcome cohort. Missing stage coverage is unknown, not zero or proof "
        "that the stage did not run. An observed zero duration is retained.",
        "Latency count is stage observations, not requests: repeated spans are separate observations, "
        "not per-request sums. Stage outcomes are split within each request-outcome cohort.",
        "Stage durations overlap and are not additive; cross-process clocks are not directly subtracted. "
        "End-to-end remains the observer's complete callback-body reception time, not a sum of stages.",
        "Percentiles use the existing linear interpolation; small stage samples and tail quantiles "
        "are exploratory, not an SLA.",
    ]
    if imported is not None:
        timing_note = (f"verified against {len(imported.emission_timestamps)} supplied UTC log-emission timestamps"
                       if imported.emission_timestamps else
                       "event DTO contains no timestamp to verify membership")
        window = (f"{imported.window_start} to {imported.window_end} "
                  f"(operator-declared; {timing_note})"
                  if imported.window_start is not None else
                  "not supplied")
        rows += [
            "",
            "### Completed-stage log provenance",
            "",
            f"Source: [separate stage-log export]({imported.source_path.as_uri()}).",
            f"Source SHA-256 (original bytes): `{imported.source_sha256}`.",
            f"Export records: {imported.raw_record_count}; unique event IDs: {len(imported.records)}; "
            f"exact duplicate records: {imported.duplicate_count}.",
            f"Log window: {window}.",
            f"Superseded callback observations: {sum(superseded.values())}.",
            "Log identity is exactly (boot_id, segment_id, span_id); differing fields for one "
            "identity reject the import. Different identities are distinct observed attempts.",
            "Join is exact job_id AND trace_id from the original callback correspondence. "
            "Missing, unmatched or ambiguous correlation rejects the whole import.",
            "Completed logs take precedence only for the matched request/stage. Callback stages "
            "have no event IDs: supersession is a source-selection policy, not claimed identity "
            "deduplication. Other callback stages are retained. Sources are not pooled in distributions.",
            "Log precedence is restricted to ingress, history_read, history_commit and send; "
            "all other stage scopes remain callback-sourced.",
            "A partial export can supersede a more complete callback stage list; export the "
            "intended complete log window and inspect counts. Request coverage is not span completeness.",
            "Raw results and the separate import file are unchanged. Source SHA/window are "
            "provenance, not Azure attestation. Imported spans may finish after callback reception "
            "or the original run window; they never change end-to-end timing, outcomes or cold evidence.",
            "Timestamp window validation does not attest Azure collection or complete attempt coverage.",
        ]
        if imported.emission_timestamps:
            timestamps = [_log_timestamp(value) for value in imported.emission_timestamps]
            rows.append(
                f"Observed log-emission range: {min(timestamps).isoformat()} to {max(timestamps).isoformat()}. "
                "Emission timestamps are collection metadata, not stage duration endpoints.")
    cohorts: list[tuple[str, list[Sample]]] = [
        (outcome, [sample for sample in result.samples if sample.status == outcome])
        for outcome in OUTCOMES
    ]
    cohorts = [(outcome, samples) for outcome, samples in cohorts if samples]
    if not cohorts:
        cohorts = [("none", [])]
    for outcome, samples in cohorts:
        sources = ("callback", "completed log") if imported is not None else ("callback",)
        durations = {name: {source: {status: [] for status in STAGE_OUTCOMES} for source in sources}
                     for name in STAGE_NAMES}
        coverage = {name: set() for name in STAGE_NAMES}
        source_coverage = {name: {source: set() for source in sources} for name in STAGE_NAMES}
        for sample in samples:
            for observed in joined.get(sample.request_id, []):
                stage = observed.observation
                durations[stage.name][observed.source][stage.outcome].append(stage.duration_ms)
                coverage[stage.name].add(sample.request_id)
                source_coverage[stage.name][observed.source].add(sample.request_id)
        rows += [
            "",
            f"### Request outcome: `{outcome}`",
            "",
            f"Attempted requests in cohort: {len(samples)}.",
            "",
            "| Stage | Observations | Requests with stage / cohort | Missing/unknown requests | Coverage |",
            "|---|---:|---:|---:|---:|",
        ]
        for name in STAGE_NAMES:
            observed = sum(len(values) for source in durations[name].values() for values in source.values())
            covered = len(coverage[name])
            percent = f"{covered / len(samples):.2%}" if samples else "n/a"
            rows.append(f"| {name} | {observed} | {covered}/{len(samples)} | "
                        f"{len(samples) - covered} | {percent} |")
        if imported is not None:
            rows += [
                "",
                "| Stage | Callback observations used | Log observations used | Callback requests | "
                "Log requests | Callback observations superseded |",
                "|---|---:|---:|---:|---:|---:|",
            ]
            for name in STAGE_NAMES:
                counts = [sum(len(values) for values in durations[name][source].values())
                          for source in sources]
                replaced = sum(superseded.get((sample.request_id, name), 0) for sample in samples)
                rows.append(f"| {name} | {counts[0]} | {counts[1]} | "
                            f"{len(source_coverage[name]['callback'])} | "
                            f"{len(source_coverage[name]['completed log'])} | {replaced} |")
        rows += [
            "",
            ("| Stage | Source | Stage outcome | Count | Mean ms | P50 ms | P95 ms | P99 ms | Min ms | Max ms |"
             if imported is not None else
             "| Stage | Stage outcome | Count | Mean ms | P50 ms | P95 ms | P99 ms | Min ms | Max ms |"),
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|" if imported is not None else
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for name in STAGE_NAMES:
            observed = False
            for source in sources:
                for status in STAGE_OUTCOMES:
                    values = durations[name][source][status]
                    if not values:
                        continue
                    observed = True
                    distribution = Distribution.from_values(values)
                    metrics = [
                        distribution.mean_ms, distribution.p50_ms, distribution.p95_ms,
                        distribution.p99_ms, min(values), max(values),
                    ]
                    prefix = f"| {name} | {source} |" if imported is not None else f"| {name} |"
                    rows.append(f"{prefix} {status} | {distribution.count} | "
                                + " | ".join(_number(value) for value in metrics) + " |")
            if not observed:
                prefix = f"| {name} | none |" if imported is not None else f"| {name} |"
                rows.append(f"{prefix} not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |")
    return rows


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
        _telemetry_by_request(result)
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


def render_report(result: RunResult, *, stage_logs: StageLogImport | None = None) -> str:
    summary = summarize(result.samples, window_seconds=result.window_seconds)
    functional_smoke = result.profile == "cloud-smoke"
    complete = sum(count for status, count in summary.counts.items() if status != "censored")
    target = (result.plan.get("warmup_requests", 3) if result.profile == "warmup" else
              result.plan.get("warm_target", 100) if result.profile == "warm" else
              1 if result.profile == "cold" else result.plan.get("target_completions", 50))
    minimum = 0 if result.profile in {"cold", "warmup"} else result.plan.get("min_seconds", 180)
    sufficient = complete >= target and result.submission_seconds >= minimum
    mode = ("FUNCTIONAL SMOKE" if functional_smoke else
            "LOCAL SYNTHETIC HTTP BOUNDARY" if result.plan.get("local_test") else "APPLICATION LINK")
    observation_label = "Smoke observations" if functional_smoke else "Sampling"
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
        f"Stop: `{result.stop_reason}`. {observation_label}: **{'sufficient' if sufficient else 'insufficient'}** "
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
    if functional_smoke:
        verification = (result.plan.get("deployment") or {}).get("cloud_smoke_verified")
        recorded = "true" if verification is True else "false" if verification is False else "not supplied"
        rows[3:3] = [
            "**NOT A CAPACITY OR PERFORMANCE RECOMMENDATION.**",
            "Original request/stage timings are functional diagnostic observations only. "
            "A terminal observation count or process exit does not certify successful smoke verification.",
            "Operator review of completed-question and evidence is required before an explicitly "
            "verified later warm/concurrency/cold plan; this report never promotes the gate.",
            f"Recorded cloud_smoke_verified: {recorded} (unchanged).",
            "",
        ]

    for outcome in OUTCOMES:
        distribution = summary.distributions[outcome]
        durations = [sample.duration_ms for sample in result.samples
                     if sample.status == outcome and sample.duration_ms is not None]
        values = [distribution.mean_ms, distribution.p50_ms, distribution.p95_ms,
                  distribution.p99_ms, min(durations) if durations else None,
                  max(durations) if durations else None]
        rows.append(f"| {outcome} | {summary.counts[outcome]} | "
                    + " | ".join(_number(value) for value in values) + " |")
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
    ]
    rows += _stage_report_rows(result, stage_logs)
    rows += [
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
