"""Bounded closed-loop load; HTTP admission is never an answer."""
import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import random
import time
from uuid import uuid4

from aiohttp import ClientError, ClientSession, ClientTimeout
from pydantic import Field

from advisor_deploy.bench.cold import ColdEvidence, classify_cold
from advisor_deploy.bench.models import Sample
from advisor_deploy.bench.plan import RunPlan
from advisor_deploy.bench.receiver import ReplyReceiver
from advisor_deploy.bench.sink import EvidenceEvent, EvidenceFailure, EvidenceSink
from advisor_shared.benchmark import (
    BenchmarkAck, BenchmarkRequest, BenchmarkRunAccepted, BenchmarkRunRegistration,
    Identifier, ScaleGroup, WireModel,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunResult:
    run_id: str
    profile: str
    concurrency: int
    started_at: str
    ended_at: str
    window_seconds: float
    submission_seconds: float
    stop_reason: str
    samples: list[Sample]
    plan: dict
    telemetry: list[dict] = field(default_factory=list)
    driver: dict = field(default_factory=dict)
    cold: dict = field(default_factory=dict)
    observations: dict = field(default_factory=dict)


class TrendGuard:
    def __init__(self):
        self.previous_throttles: set[str] = set()
        self.backlog: list[int | None] = []
        self.windows = 0

    def close_window(self, dependencies: dict[str, tuple[int, int]],
                     backlog: int | None) -> str | None:
        self.windows += 1
        throttled = {name for name, (calls, limited) in dependencies.items()
                     if calls > 0 and limited > 0 and limited / calls >= .05}
        persistent = bool(throttled & self.previous_throttles)
        self.previous_throttles = throttled
        if self.windows > 1:
            self.backlog = (self.backlog + [backlog])[-3:]
        if persistent:
            return "persistent_throttle"
        if (len(self.backlog) == 3 and all(value is not None for value in self.backlog)
                and self.backlog[0] < self.backlog[1] < self.backlog[2]):
            return "persistent_backlog"
        return None


def retry_after_seconds(value: str | None) -> float:
    if value is None:
        return 1.
    try:
        if value.isdigit():
            return float(value)
        parsed = parsedate_to_datetime(value)
        return max(0., (parsed - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return 1.


class HttpSubmitter:
    def __init__(self, session: ClientSession, plan: RunPlan,
                 token_provider: Callable[[], Awaitable[str]] | None = None):
        if not plan.local_test and token_provider is None:
            raise ValueError("cloud submissions require an explicit token provider")
        self.session, self.plan, self.token_provider = session, plan, token_provider
        self.observations: list[tuple[int, bool]] = []
        self.pause_until = 0.

    async def headers(self) -> dict[str, str]:
        if self.token_provider is None:
            return {}
        return {"Authorization": "Bearer " + await self.token_provider()}

    async def register(self, registration: BenchmarkRunRegistration) -> None:
        async with self.session.post(
            self.plan.target_url.rstrip("/") + "/bench/runs",
            json=registration.model_dump(), headers=await self.headers(),
            timeout=ClientTimeout(total=self.plan.http_timeout_seconds),
            allow_redirects=False,
        ) as response:
            if response.status not in {200, 201}:
                raise ValueError("target run registration rejected")
            raw = await response.content.read(65537)
            if len(raw) > 65536:
                raise ValueError("oversized registration response")
            accepted = BenchmarkRunAccepted.model_validate_json(raw)
            if (accepted.run_id != registration.run_id
                    or accepted.target_id != self.plan.expected_target_id
                    or accepted.dataset_sha256 != self.plan.dataset_sha256):
                raise ValueError("target registration identity or dataset mismatch")

    async def submit(self, request: BenchmarkRequest, receiver: ReplyReceiver,
                     stop: Callable[[str], None],
                     should_stop: Callable[[], bool] = lambda: False) -> None:
        entry = receiver.entries[request.run_id, request.request_id]
        ambiguous = False
        for attempt in range(self.plan.max_http_attempts):
            if entry.future.done():
                return
            if should_stop():
                if not ambiguous:
                    receiver.finish(request.run_id, request.request_id, "failed")
                return
            remaining = (self.plan.request_timeout_seconds
                         - receiver.elapsed(request.run_id) + entry.started_s)
            if remaining <= 0:
                return
            delay = max(0., self.pause_until - time.monotonic())
            if delay >= remaining:
                if not ambiguous:
                    receiver.finish(request.run_id, request.request_id, "failed")
                stop("retry_after_exceeds_deadline")
                return
            if delay:
                await asyncio.sleep(delay)
                remaining -= delay
                if should_stop():
                    if not ambiguous:
                        receiver.finish(request.run_id, request.request_id, "failed")
                    return
            try:
                headers = await self.headers()
            except Exception:
                if not ambiguous:
                    receiver.finish(request.run_id, request.request_id, "failed")
                stop("authentication_unavailable")
                return
            if should_stop():
                if not ambiguous:
                    receiver.finish(request.run_id, request.request_id, "failed")
                return
            remaining = (self.plan.request_timeout_seconds
                         - receiver.elapsed(request.run_id) + entry.started_s)
            if remaining <= 0:
                return
            try:
                async with self.session.post(
                    self.plan.target_url.rstrip("/") + "/bench/messages",
                    json=request.model_dump(), headers=headers,
                    timeout=ClientTimeout(total=min(self.plan.http_timeout_seconds, remaining)),
                    allow_redirects=False,
                ) as response:
                    self.observations.append((response.status, response.status == 429))
                    if response.status == 202:
                        raw = await response.content.read(65537)
                        if len(raw) > 65536:
                            raise ValueError("oversized ACK")
                        accepted = BenchmarkAck.model_validate_json(raw)
                        if (accepted.run_id != request.run_id
                                or accepted.request_id != request.request_id):
                            raise ValueError("ACK correlation conflict")
                        receiver.acknowledge(request.run_id, request.request_id, accepted.job_id)
                        return
                    if response.status == 429:
                        delay = retry_after_seconds(response.headers.get("Retry-After"))
                        self.pause_until = max(self.pause_until, time.monotonic() + delay)
                        remaining = (self.plan.request_timeout_seconds
                                     - receiver.elapsed(request.run_id) + entry.started_s)
                        if delay >= remaining:
                            if not ambiguous:
                                receiver.finish(request.run_id, request.request_id, "failed")
                            stop("retry_after_exceeds_deadline")
                            return
                        if attempt + 1 == self.plan.max_http_attempts and not ambiguous:
                            receiver.finish(request.run_id, request.request_id, "failed")
                        continue
                    if 400 <= response.status < 500:
                        if not ambiguous:
                            receiver.finish(request.run_id, request.request_id, "failed")
                        if response.status in {401, 403, 404, 409}:
                            stop("target_rejection")
                        return
                    ambiguous = True
            except (ClientError, TimeoutError):
                ambiguous = True
                self.observations.append((0, False))
            except ValueError:
                # Admission might already have happened: don't turn an invalid ACK into a rejection.
                stop("protocol_error")
                return
            if attempt + 1 < self.plan.max_http_attempts:
                await asyncio.sleep(.01 if self.plan.local_test else .25 * 2**attempt)


class ZeroProof(WireModel):
    target_id: str
    group: ScaleGroup
    observed_at: str
    replicas: int = Field(ge=0)
    source: str = Field(pattern=r"^(azure_monitor|arm)$")
    evidence_id: Identifier


def supplied_zero_evidence(path: Path, *, target_id: str, since: str) -> list[ZeroProof]:
    """Read external control-plane observations. Never probe application status/health."""
    if not path.exists():
        return []
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list) or len(values) > 10000:
        raise ValueError("invalid zero evidence file")
    threshold = datetime.fromisoformat(since.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    result = {}
    for value in values:
        proof = ZeroProof.model_validate(value)
        observed = datetime.fromisoformat(proof.observed_at.replace("Z", "+00:00"))
        if observed.utcoffset() is None:
            raise ValueError("control-plane observation requires timezone")
        if (proof.target_id == target_id and proof.replicas == 0
                and threshold <= observed <= now):
            if proof.group not in result or proof.observed_at > result[proof.group].observed_at:
                result[proof.group] = proof
    return list(result.values())


def peak_memory_mb() -> float | None:
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except ImportError:
        # Windows doesn't expose getrusage; unknown is not zero utilization.
        return None


async def run_level(
    plan: RunPlan, submit: HttpSubmitter, receiver: ReplyReceiver, *, concurrency: int,
    before_load: Callable[[str], Awaitable[list[ZeroProof]]] | None = None,
    result_sink: Callable[[RunResult], None] | None = None,
    warmup: bool = False,
    evidence_sink: EvidenceSink | None = None,
) -> RunResult:
    plan = RunPlan.model_validate(plan.model_dump())
    if concurrency not in (1, 2, 4, 8, 16, 32, 64, 128):
        raise ValueError("unsupported concurrency")
    if not plan.local_test and concurrency > plan.deployment.max_concurrency:
        raise ValueError("concurrency exceeds deployment bound")
    run_id = uuid4().hex
    expiry = datetime.now(timezone.utc) + timedelta(
        seconds=plan.zero_wait_seconds + plan.max_submit_seconds + plan.drain_seconds + 60)
    if plan.deployment:
        expiry = min(expiry, datetime.fromisoformat(plan.deployment.valid_until.replace("Z", "+00:00")))
    profile = "warmup" if warmup else plan.profile
    registration = BenchmarkRunRegistration(
        run_id=run_id, profile=profile, case_ids=plan.case_ids, seed=plan.seed,
        scenario=plan.scenario, history_mode=plan.history_mode, max_requests=plan.max_requests,
        max_concurrency=concurrency, expires_at=expiry.isoformat(),
        dataset_sha256=plan.dataset_sha256)
    receiver.evidence_sink = evidence_sink
    if evidence_sink:
        try:
            await evidence_sink.start_run(run_id, plan)
        except EvidenceFailure:
            result = RunResult(
                run_id, profile, concurrency, utc_now(), utc_now(), 1e-9, 0.,
                "observer_evidence_failure", [], plan.model_dump())
            if result_sink:
                result_sink(result)
            return result
    preparing_at, preparing_clock = utc_now(), time.perf_counter()
    try:
        await submit.register(registration)
        registered_at = utc_now()
        zero_proofs = await before_load(registered_at) if before_load else []
    except BaseException as error:
        result = RunResult(
            run_id, profile, concurrency, preparing_at, utc_now(),
            max(time.perf_counter() - preparing_clock, 1e-9), 0.,
            "driver_interrupted" if isinstance(error, asyncio.CancelledError)
            else "preparation_failure", [], plan.model_dump())
        try:
            if evidence_sink:
                await evidence_sink.finalize(result)
        finally:
            if result_sink:
                result_sink(result)
        raise
    receiver.begin_run(run_id)
    started_at = utc_now()
    cpu_started = time.process_time()
    peak_lag = 0.
    stop_event = asyncio.Event()
    stop_reason = ""
    stop_reasons = []
    stopped_at = None
    issued = 0
    completed = 0
    tasks = []
    guard = TrendGuard()
    dependency_bucket = defaultdict(lambda: [0, 0])
    backlog = None
    telemetry_seen = set()
    transport_start = len(submit.observations)
    observation_cursor = transport_start
    next_window = plan.trend_seconds
    max_cpu_ratio = 0. if plan.deployment else None
    last_cpu = cpu_started
    last_cpu_wall = 0.
    saturated_windows = 0
    target = (plan.warmup_requests if warmup else
              plan.warm_target if plan.profile == "warm" else
              1 if plan.profile == "cold" else plan.target_completions)

    def stop(reason: str) -> None:
        nonlocal stop_reason, stopped_at
        if reason not in stop_reasons:
            stop_reasons.append(reason)
        if not stop_event.is_set():
            stop_reason, stopped_at = reason, receiver.elapsed(run_id)
            stop_event.set()
        elif reason == "driver_interrupted" or (
            stop_reason in {"target_reached", "submission_limit", "request_budget"}
            and reason in {"request_deadline", "host_failure", "driver_saturated",
                           "protocol_error", "observer_failure", "observer_evidence_failure"}
        ):
            stop_reason = reason

    def stop_submitting() -> bool:
        if evidence_sink and evidence_sink.failed:
            stop("observer_evidence_failure")
        if receiver.elapsed(run_id) >= plan.max_submit_seconds:
            stop("submission_limit")
        if datetime.now(timezone.utc) >= expiry:
            stop("deployment_expired")
        return stop_event.is_set() and stop_reason not in {"target_reached", "request_budget"}

    def consume_telemetry() -> None:
        nonlocal backlog
        for (run, request_id), entry in receiver.entries.items():
            if run != run_id or entry.telemetry is None or request_id in telemetry_seen:
                continue
            telemetry_seen.add(request_id)
            telemetry = entry.telemetry
            if telemetry.host_crashed or telemetry.host_oom:
                stop("host_failure")
            if telemetry.pending_jobs is not None:
                backlog = telemetry.pending_jobs
            for dependency in telemetry.dependencies:
                bucket = dependency_bucket[dependency.dependency]
                bucket[0] += dependency.calls
                bucket[1] += dependency.throttled

    async def worker(slot: int) -> None:
        nonlocal issued, completed
        randomizer = random.Random(plan.seed + slot)
        while not stop_event.is_set():
            elapsed = receiver.elapsed(run_id)
            minimum = 0 if warmup or plan.profile == "cold" else plan.min_seconds
            if datetime.now(timezone.utc) >= expiry:
                stop("deployment_expired")
                break
            if completed >= target and elapsed >= minimum:
                stop("target_reached")
                break
            if elapsed >= plan.max_submit_seconds:
                stop("submission_limit")
                break
            if issued >= plan.max_requests:
                stop("request_budget")
                break
            issued += 1
            request = BenchmarkRequest(
                run_id=run_id, request_id=uuid4().hex,
                case_id=randomizer.choice(plan.case_ids),
                virtual_user=0 if plan.history_mode == "same_user" else slot,
                scenario=plan.scenario)
            try:
                if evidence_sink:
                    await evidence_sink.record(EvidenceEvent(
                        run_id=run_id, request_id=request.request_id, case_id=request.case_id,
                        kind="request_intent"))
                    if stop_submitting():
                        await evidence_sink.record(EvidenceEvent(
                            run_id=run_id, request_id=request.request_id, kind="not_submitted"))
                        break
                entry = receiver.register(run_id, request.request_id, request.case_id,
                                          deadline_seconds=plan.request_timeout_seconds)
            except EvidenceFailure:
                stop("observer_evidence_failure")
                break
            submission = asyncio.create_task(submit.submit(
                request, receiver, stop, stop_submitting))
            try:
                await receiver.wait_for_terminal(run_id, request.request_id)
            except TimeoutError:
                receiver.censor(run_id, request.request_id)
                stop("request_deadline")
            finally:
                # The final callback may beat ACK. Give an already arriving ACK one
                # event-loop turn, but never wait on an admission call after completion.
                await asyncio.sleep(0)
                if not submission.done():
                    submission.cancel()
                await asyncio.gather(submission, return_exceptions=True)
            consume_telemetry()
            if evidence_sink and evidence_sink.failed:
                stop("observer_evidence_failure")
            if entry.sample.status != "censored":
                completed += 1
            else:
                stop("request_deadline")

    async def monitor() -> None:
        nonlocal peak_lag, observation_cursor, next_window, backlog
        nonlocal last_cpu, last_cpu_wall, max_cpu_ratio, saturated_windows
        interval = .005 if plan.local_test else .1
        while True:
            due = time.monotonic() + interval
            await asyncio.sleep(interval)
            lag = max(0., time.monotonic() - due)
            peak_lag = max(peak_lag, lag)
            elapsed = receiver.elapsed(run_id)
            if evidence_sink and evidence_sink.failed:
                stop("observer_evidence_failure")
                for (run, request_id), entry in receiver.entries.items():
                    if run == run_id and entry.sample is None:
                        receiver.censor(run_id, request_id)
            if lag > plan.driver_max_loop_lag_seconds:
                stop("driver_saturated")
            if elapsed >= plan.max_submit_seconds:
                stop("submission_limit")
            if datetime.now(timezone.utc) >= expiry:
                stop("deployment_expired")
            for (run, request_id), entry in receiver.entries.items():
                if run != run_id or entry.sample is not None:
                    continue
                if receiver.deadline_due(run_id, request_id):
                    receiver.censor(run_id, request_id)
                    stop("request_deadline")
                elif stopped_at is not None and elapsed - stopped_at >= plan.drain_seconds:
                    receiver.censor(run_id, request_id)
            consume_telemetry()
            if elapsed >= next_window:
                observations = submit.observations[observation_cursor:]
                observation_cursor = len(submit.observations)
                dependency_bucket["ingress"][0] += sum(code > 0 for code, _ in observations)
                dependency_bucket["ingress"][1] += sum(limited for _, limited in observations)
                reason = guard.close_window(dict(dependency_bucket), backlog)
                if reason:
                    stop(reason)
                dependency_bucket.clear()
                backlog = None
                now_cpu = time.process_time()
                saturated = False
                if plan.deployment:
                    ratio = ((now_cpu - last_cpu) / (elapsed - last_cpu_wall)
                             / plan.deployment.driver_cpu_cores)
                    max_cpu_ratio = max(max_cpu_ratio, ratio)
                    memory = peak_memory_mb()
                    saturated = ratio >= .9 or (
                        memory is not None and memory >= plan.deployment.driver_memory_mb * .9)
                saturated_windows = saturated_windows + 1 if saturated else 0
                if saturated_windows >= 2:
                    stop("driver_saturated")
                last_cpu, last_cpu_wall = now_cpu, elapsed
                next_window = elapsed + plan.trend_seconds

    if before_load and not zero_proofs:
        stop("zero_not_observed")
    monitor_task = asyncio.create_task(monitor())

    def monitor_stopped(task):
        if not task.cancelled() and task.exception() is not None:
            stop("observer_failure")

    monitor_task.add_done_callback(monitor_stopped)
    try:
        tasks = [asyncio.create_task(worker(slot)) for slot in range(concurrency)]
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        stop("driver_interrupted")
        raise
    finally:
        monitor_task.cancel()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(monitor_task, *tasks, return_exceptions=True)
        for (run, request_id), entry in receiver.entries.items():
            if run == run_id and entry.sample is None:
                receiver.censor(run_id, request_id)
        elapsed = max(receiver.elapsed(run_id), 1e-9)
        samples = receiver.samples(run_id)
        entries = [entry for (run, _), entry in receiver.entries.items() if run == run_id]
        cold_groups = {}
        for proof in zero_proofs:
            boot = next((boot for entry in entries if entry.telemetry
                         for boot in entry.telemetry.boots
                         if boot.group == proof.group and boot.first_request), None)
            evidence = ColdEvidence(
                idle_seconds=max(0., (datetime.fromisoformat(started_at)
                                      - datetime.fromisoformat(registered_at)).total_seconds()),
                zero_confirmed=True, new_boot_confirmed=boot is not None, reason="idle")
            cold_groups[proof.group] = dict(
                category=classify_cold(evidence), proof=proof.model_dump(),
                boot=boot.model_dump() if boot else None)
        expected_groups = ({"http", "worker"} if plan.deployment
                           and plan.deployment.host == "functions" else {"app"})
        cold_category = ("natural_zero" if expected_groups <= cold_groups.keys()
                         and all(cold_groups[group]["category"] == "natural_zero"
                                 for group in expected_groups) else "unverified")
        result = RunResult(
            run_id, profile, concurrency, started_at, utc_now(), elapsed,
            stopped_at if stopped_at is not None else elapsed,
            stop_reason or "finished", samples, plan.model_dump(),
            telemetry=[dict(request_id=entry.request_id, job_id=entry.job_id,
                            **entry.telemetry.model_dump())
                       for entry in entries if entry.telemetry],
            driver=dict(cpu_seconds=time.process_time() - cpu_started,
                        peak_memory_mb=peak_memory_mb(), peak_loop_lag_seconds=peak_lag,
                        max_cpu_capacity_ratio=max_cpu_ratio,
                        observer_clock="perf_counter",
                        observer_clock_resolution_seconds=time.get_clock_info("perf_counter").resolution,
                        memory_measurement="process_high_watermark_or_unavailable"),
            cold=dict(category=cold_category, groups=cold_groups),
            observations=dict(
                http_attempts=len(submit.observations) - transport_start,
                http_429=sum(limited for _, limited in submit.observations[transport_start:]),
                duplicate_replies=sum(entry.duplicates for entry in entries),
                late_replies=sum(entry.late for entry in entries),
                typing_events=sum(entry.typing for entry in entries),
                callback_processing_ms=[entry.observer_processing_ms for entry in entries
                                        if entry.observer_processing_ms is not None],
                measurement_endpoint="complete_callback_body_received_before_authentication",
                stop_reasons=stop_reasons),
        )
        try:
            if evidence_sink:
                await evidence_sink.finalize(result)
        finally:
            if result_sink:
                result_sink(result)
    return result


async def run_plan(plan: RunPlan, submit: HttpSubmitter, receiver: ReplyReceiver, *,
                   result_sink: Callable[[RunResult], None] | None = None,
                   evidence_sink: EvidenceSink | None = None) -> list[RunResult]:
    results = []
    if plan.profile != "cold":
        if plan.profile == "warm":
            result = await run_level(plan, submit, receiver, concurrency=1,
                                     result_sink=result_sink, warmup=True, evidence_sink=evidence_sink)
            results.append(result)
            if result.stop_reason != "target_reached":
                return results
        levels = [1] if plan.profile == "warm" else plan.concurrency_levels
        for concurrency in levels:
            result = await run_level(plan, submit, receiver, concurrency=concurrency,
                                     result_sink=result_sink, evidence_sink=evidence_sink)
            results.append(result)
            if result.stop_reason != "target_reached":
                break
        return results
    groups = {"http", "worker"} if plan.deployment and plan.deployment.host == "functions" else {"app"}

    async def wait_zero(since: str) -> list[ZeroProof]:
        deadline = time.monotonic() + plan.zero_wait_seconds
        while time.monotonic() < deadline:
            proofs = supplied_zero_evidence(
                Path(plan.zero_evidence_path), target_id=plan.expected_target_id, since=since)
            if groups <= {proof.group for proof in proofs}:
                return proofs
            await asyncio.sleep(min(5., max(0., deadline - time.monotonic())))
        return []

    for _ in range(plan.cold_attempts):
        result = await run_level(plan, submit, receiver, concurrency=1,
                                 before_load=wait_zero, result_sink=result_sink, evidence_sink=evidence_sink)
        results.append(result)
        if sum(item.cold.get("category") == "natural_zero" for item in results) >= plan.cold_target:
            break
        if result.stop_reason not in {"target_reached", "zero_not_observed"}:
            break
    return results
