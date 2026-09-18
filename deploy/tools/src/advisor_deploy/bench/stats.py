from dataclasses import dataclass
import math
from statistics import fmean

from advisor_deploy.bench.models import OUTCOMES, Outcome, Sample, finite_nonnegative


def percentile(values: list[float], quantile: float) -> float | None:
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between zero and one")
    for value in values:
        finite_nonnegative(value, "duration")
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


@dataclass(frozen=True)
class Distribution:
    count: int
    mean_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None

    @classmethod
    def from_values(cls, values: list[float]) -> "Distribution":
        return cls(
            len(values), fmean(values) if values else None,
            percentile(values, .5), percentile(values, .95), percentile(values, .99))


@dataclass(frozen=True)
class Summary:
    attempted: int
    counts: dict[Outcome, int]
    distributions: dict[Outcome, Distribution]
    completed_after_window: int
    window_seconds: float
    answer_rps: float
    success_rps: float
    success_rate: float | None
    notes: tuple[str, ...]

    @property
    def mean_ms(self) -> float | None:
        return self.distributions["success"].mean_ms

    @property
    def p50_ms(self) -> float | None:
        return self.distributions["success"].p50_ms

    @property
    def p95_ms(self) -> float | None:
        return self.distributions["success"].p95_ms

    @property
    def p99_ms(self) -> float | None:
        return self.distributions["success"].p99_ms


def summarize(samples: list[Sample], *, window_seconds: float) -> Summary:
    finite_nonnegative(window_seconds, "window_seconds")
    if window_seconds == 0:
        raise ValueError("observation window must be positive")
    if len({sample.request_id for sample in samples}) != len(samples):
        raise ValueError("duplicate logical requests in sample set")
    counts: dict[Outcome, int] = {outcome: 0 for outcome in OUTCOMES}
    values: dict[Outcome, list[float]] = {outcome: [] for outcome in OUTCOMES}
    answers_in_window = successes_in_window = late = 0
    for sample in samples:
        counts[sample.status] += 1
        duration = sample.duration_ms
        if duration is not None:
            values[sample.status].append(duration)
        if sample.finished_s is None:
            continue
        if sample.finished_s > window_seconds:
            late += 1
        elif sample.status in {"success", "degraded"}:
            answers_in_window += 1
            successes_in_window += sample.status == "success"
    notes = []
    if not counts["success"]:
        notes.append("no_successful_completions")
    elif counts["success"] < 100:
        notes.append("small_tail_sample")
    if counts["censored"]:
        notes.append("unfinished_work_not_in_latency_distribution")
    return Summary(
        attempted=len(samples), counts=counts,
        distributions={status: Distribution.from_values(measured)
                       for status, measured in values.items()},
        completed_after_window=late, window_seconds=window_seconds,
        answer_rps=answers_in_window / window_seconds,
        success_rps=successes_in_window / window_seconds,
        success_rate=counts["success"] / len(samples) if samples else None,
        notes=tuple(notes))
