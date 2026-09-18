import pytest

from advisor_deploy.bench.models import Sample
from advisor_deploy.bench.stats import percentile, summarize


def test_observed_percentiles_are_linear_interpolations():
    assert percentile([40, 10, 30, 20], .5) == 25
    assert percentile([40, 10, 30, 20], .95) == 38.5
    assert percentile([40, 10, 30, 20], .99) == pytest.approx(39.7)
    assert percentile([], .5) is None


@pytest.mark.parametrize("values,q", [
    ([float("nan")], .5), ([float("inf")], .5), ([-1], .5),
    ([1], -1), ([1], 2), ([1], float("nan")),
])
def test_invalid_measurements_are_not_silently_dropped(values, q):
    with pytest.raises(ValueError):
        percentile(values, q)


def test_summary_distinguishes_answers_failures_and_censored_work():
    samples = [
        Sample("a", "case", "success", 0, .1, ack_ms=1),
        Sample("b", "case", "success", 0, .3, ack_ms=1),
        Sample("c", "case", "degraded", 0, .2, ack_ms=1),
        Sample("d", "case", "failed", 0, .01, ack_ms=1),
        Sample("e", "case", "censored", 0, None, ack_ms=1),
    ]
    result = summarize(samples, window_seconds=1)
    assert result.attempted == 5
    assert result.counts == {
        "success": 2, "degraded": 1, "failed": 1, "unknown": 0,
        "expired": 0, "ignored": 0, "censored": 1,
    }
    assert result.mean_ms == pytest.approx(200)
    assert result.p50_ms == pytest.approx(200)
    assert result.answer_rps == 3
    assert result.success_rps == 2
    assert result.success_rate == .4
    assert result.distributions["failed"].mean_ms == 10
    assert "small_tail_sample" in result.notes


def test_throughput_uses_completion_window_not_sum_of_overlapping_durations():
    samples = [
        Sample("a", "case", "success", 0, 2),
        Sample("b", "case", "success", 1, 3),
        Sample("late", "case", "success", 2, 6),
    ]
    result = summarize(samples, window_seconds=4)
    assert result.answer_rps == .5
    assert result.completed_after_window == 1
    assert result.counts["success"] == 3
    assert result.mean_ms == pytest.approx(8000 / 3)


def test_empty_sample_set_has_no_success_shaped_latency():
    result = summarize([], window_seconds=10)
    assert result.mean_ms is None
    assert result.p99_ms is None
    assert result.success_rate is None
    assert result.answer_rps == 0
    assert "no_successful_completions" in result.notes


def test_duplicate_request_ids_are_not_counted_twice():
    with pytest.raises(ValueError, match="duplicate"):
        summarize([Sample("a", "case", "success", 0, 1)] * 2, window_seconds=2)


@pytest.mark.parametrize("status,start,finish", [
    ("success", 0, None), ("success", 2, 1), ("success", -1, 1),
    ("success", 0, float("nan")), ("censored", 0, 1),
    ("unexpected", 0, 1),
])
def test_sample_shapes_require_honest_terminal_state(status, start, finish):
    with pytest.raises(ValueError):
        Sample("a", "case", status, start, finish)
