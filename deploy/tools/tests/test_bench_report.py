from dataclasses import asdict
import asyncio
import json
import os
import time
from uuid import uuid4
from hashlib import sha256

import pytest

from advisor_deploy.bench.models import Sample


def test_reports_separate_outcomes_actual_window_and_missing_cold_evidence(tmp_path):
    from advisor_deploy.bench.report import render_report, write_result
    from advisor_deploy.bench.runner import RunResult

    result = RunResult(
        run_id="test-run", profile="local-smoke", concurrency=1,
        started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:00:10Z",
        window_seconds=10., submission_seconds=8., stop_reason="request_deadline",
        samples=[
            Sample("a", "kb-01", "success", 0., 2.),
            Sample("b", "kb-01", "degraded", 2., 3.),
            Sample("c", "kb-01", "failed", 3., 4.),
            Sample("d", "kb-01", "unknown", 4., 5.),
            Sample("e", "kb-01", "censored", 5., None),
        ], plan={"target_completions": 100, "min_seconds": 180.},
    )
    report = render_report(result)
    assert "| success | 1 |" in report
    assert "| degraded | 1 |" in report
    assert "| failed | 1 |" in report
    assert "| unknown | 1 |" in report
    assert "| censored | 1 |" in report
    assert "0.2" in report
    assert "10.000" in report
    assert "insufficient" in report
    assert "unverified" in report
    assert "not an SLA" in report
    path = tmp_path / "result.json"
    write_result(result, path)
    assert len(json.loads(path.read_text())["samples"]) == 5


def test_invalid_raw_results_cannot_be_reported_as_measurements():
    from advisor_deploy.bench.report import read_result

    for value in ({"question": "secret"}, {"samples": [], "window_seconds": float("nan")}):
        import pytest
        with pytest.raises(ValueError):
            read_result(value)


def test_historical_manifest_expiry_does_not_prevent_reporting_old_measurements():
    from advisor_deploy.bench.plan import RunPlan
    from advisor_deploy.bench.report import read_result, render_report
    from advisor_deploy.bench.runner import RunResult
    from test_bench_runner import cloud_plan_values

    plan = RunPlan.model_validate(cloud_plan_values()).model_dump()
    plan["deployment"]["valid_until"] = "2020-01-01T00:00:00Z"
    result = RunResult(
        run_id="old-run", profile="concurrency", concurrency=1,
        started_at="2019-01-01T00:00:00Z", ended_at="2019-01-01T00:00:01Z",
        window_seconds=1., submission_seconds=1., stop_reason="request_budget",
        samples=[Sample("a", "kb-01", "success", 0., .5)], plan=plan)
    assert "| success | 1 |" in render_report(read_result(asdict(result)))


def stage_report_result(scenario="real_agent"):
    from advisor_deploy.bench.runner import RunResult
    from test_bench_runner import cloud_plan_values

    plan = cloud_plan_values() | {"scenario": scenario}
    return RunResult(
        run_id="stage-run", profile="concurrency", concurrency=1,
        started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:00:10Z",
        window_seconds=10., submission_seconds=9., stop_reason="request_deadline",
        samples=[
            Sample("a", "kb-01", "success", 0., 2.),
            Sample("b", "kb-01", "success", 2., 4.),
            Sample("c", "kb-01", "success", 4., 5.),
            Sample("d", "kb-01", "degraded", 5., 6.),
            Sample("e", "kb-01", "failed", 6., 7.),
            Sample("f", "kb-01", "unknown", 7., 8.),
            Sample("g", "kb-01", "censored", 8., None),
        ], plan=plan,
        telemetry=[
            {"request_id": "b", "job_id": "job-b", "trace_id": "trace-b", "stages": [
                {"name": "agent", "duration_ms": 20., "outcome": "success"},
                {"name": "agent", "duration_ms": 40., "outcome": "success"},
                {"name": "model", "duration_ms": 80., "outcome": "failed"}]},
            {"request_id": "a", "job_id": "job-a", "trace_id": "trace-a", "stages": [
                {"name": "agent", "duration_ms": 0., "outcome": "success"},
                {"name": "agent", "duration_ms": 10., "outcome": "success"},
                {"name": "model", "duration_ms": 60., "outcome": "success"}]},
            {"request_id": "d", "job_id": "job-d", "trace_id": "trace-d", "stages": [
                {"name": "agent", "duration_ms": 1000., "outcome": "degraded"}]},
            {"request_id": "e", "job_id": "job-e", "trace_id": "trace-e", "stages": [
                {"name": "agent", "duration_ms": 5000., "outcome": "failed"}]},
            {"request_id": "f", "job_id": "job-f", "trace_id": "trace-f", "stages": []},
        ])


def test_stage_report_uses_observed_spans_and_distinct_request_coverage():
    from advisor_deploy.bench.report import render_report

    report = render_report(stage_report_result())
    success = report.split("### Request outcome: `success`")[1].split("### Request outcome:")[0]
    assert "| agent | 4 | 2/3 | 1 | 66.67% |" in success
    assert "| agent | success | 4 | 17.500 | 15.000 | 37.000 | 39.400 | 0.000 | 40.000 |" in success
    assert "| model | success | 1 | 60.000 | 60.000 | 60.000 | 60.000 | 60.000 | 60.000 |" in success
    assert "| model | failed | 1 | 80.000 | 80.000 | 80.000 | 80.000 | 80.000 | 80.000 |" in success
    assert "1000.000" not in success
    assert "5000.000" not in success
    assert "| send | 0 | 0/3 | 3 | 0.00% |" in success
    assert "| send | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |" in success
    assert "| degraded |" in report
    assert "| agent | degraded | 1 | 1000.000 |" in report
    assert "| agent | failed | 1 | 5000.000 |" in report
    assert "Scenario: `real_agent`" in report
    assert "overlap and are not additive" in report
    assert "cross-process clocks are not directly subtracted" in report
    assert "observed zero" in report
    assert "small stage samples" in report
    # End-to-end remains the three observed success durations (2s, 2s, 1s),
    # not a sum of any pipeline, component or repeated-span observations.
    assert "| success | 3 | 1666.667 | 2000.000 | 2000.000 | 2000.000 | 1000.000 | 2000.000 |" in report
    assert "complete bounded HTTP reply body received" in report


def test_stage_report_lists_every_stage_even_when_unknown_or_censored():
    from advisor_deploy.bench.report import render_report

    report = render_report(stage_report_result())
    expected_stages = (
        "ingress", "queue_wait", "worker_init", "auth", "connection", "history_setup",
        "synthetic", "agent", "kb", "github", "web", "model", "render", "send",
    )
    for outcome in ("unknown", "censored"):
        section = report.split(f"### Request outcome: `{outcome}`")[1].split("### Request outcome:")[0]
        for name in expected_stages:
            assert f"| {name} | 0 | 0/1 | 1 | 0.00% |" in section
            assert f"| {name} | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |" in section


@pytest.mark.parametrize("scenario", ["synthetic", "real_agent"])
def test_stage_report_is_scoped_to_its_explicit_scenario(scenario):
    from advisor_deploy.bench.report import render_report

    result = stage_report_result(scenario)
    if scenario == "synthetic":
        for record in result.telemetry:
            for stage in record["stages"]:
                stage["name"] = "synthetic"
    report = render_report(result)
    assert f"Scenario: `{scenario}`" in report
    assert ("Scenario: `real_agent`" if scenario == "synthetic" else "Scenario: `synthetic`") not in report
    if scenario == "synthetic":
        assert "| agent | not observed | 0 | n/a |" in report
        assert "| synthetic | success | 5 |" in report


def test_stage_report_without_samples_or_scenario_is_explicitly_unknown():
    from advisor_deploy.bench.report import render_report

    result = stage_report_result()
    result.samples = []
    result.telemetry = []
    result.plan = {"target_completions": 50, "min_seconds": 180.}
    report = render_report(result)
    assert "Scenario: `unknown`" in report
    assert "| ingress | 0 | 0/0 | 0 | n/a |" in report
    assert "| ingress | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |" in report


@pytest.mark.parametrize("corruption", ["duplicate", "orphan", "missing_request_id"])
def test_stage_report_rejects_ambiguous_telemetry_instead_of_double_counting(corruption):
    from advisor_deploy.bench.report import read_result, render_report

    result = stage_report_result()
    if corruption == "duplicate":
        result.telemetry.append(result.telemetry[0].copy())
    elif corruption == "orphan":
        result.telemetry[0]["request_id"] = "not-a-sample"
    else:
        del result.telemetry[0]["request_id"]
    with pytest.raises(ValueError, match="invalid benchmark"):
        read_result(asdict(result))
    with pytest.raises(ValueError, match="invalid benchmark"):
        render_report(result)


def test_stage_report_preserves_observed_unknown_empty_and_zero_outcomes():
    from advisor_deploy.bench.report import render_report

    result = stage_report_result()
    result.telemetry[0]["stages"] = [
        {"name": "kb", "duration_ms": 0., "outcome": "empty"},
        {"name": "auth", "duration_ms": 2., "outcome": "unknown"},
    ]
    report = render_report(result)
    assert "| kb | empty | 1 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |" in report
    assert "| auth | unknown | 1 | 2.000 | 2.000 | 2.000 | 2.000 | 2.000 | 2.000 |" in report


def test_stage_report_covers_all_supported_stages_from_supplied_observations():
    from advisor_deploy.bench.report import render_report

    result = stage_report_result()
    result.samples = [result.samples[0]]
    names = [
        "ingress", "queue_wait", "worker_init", "auth", "connection", "history_setup",
        "synthetic", "agent", "kb", "github", "web", "model", "render", "send",
    ]
    result.telemetry = [{
        "request_id": "a", "job_id": "job-a",
        "stages": [{"name": name, "duration_ms": 12.5, "outcome": "success"} for name in names],
    }]
    report = render_report(result)
    for name in names:
        assert f"| {name} | 1 | 1/1 | 0 | 100.00% |" in report
        assert f"| {name} | success | 1 | 12.500 | 12.500 | 12.500 | 12.500 | 12.500 | 12.500 |" in report


def test_stage_report_never_infers_durations_from_ack_or_other_safe_counters():
    from advisor_deploy.bench.report import render_report

    result = stage_report_result()
    result.samples = [Sample("a", "kb-01", "success", 0., 2., ack_ms=25.)]
    result.telemetry = [{
        "request_id": "a", "job_id": "job-a", "stages": [],
        "dependencies": [{"dependency": "model", "calls": 1, "throttled": 0}],
        "model_rounds": 1, "input_tokens": 100, "output_tokens": 20,
    }]
    result.observations = {"callback_processing_ms": [15.]}
    report = render_report(result)
    for stage in ("ingress", "auth", "model", "send"):
        assert f"| {stage} | 0 | 0/1 | 1 | 0.00% |" in report
        assert f"| {stage} | not observed | 0 | n/a |" in report


@pytest.mark.parametrize("outcome", ["expired", "ignored"])
def test_stage_report_keeps_other_terminal_outcomes_separate(outcome):
    from advisor_deploy.bench.report import render_report

    result = stage_report_result()
    result.samples = [Sample("a", "kb-01", outcome, 0., 1.)]
    result.telemetry = [{"request_id": "a", "job_id": "job-a", "stages": [
        {"name": "ingress", "duration_ms": 0., "outcome": "empty"}]}]
    report = render_report(result)
    assert f"### Request outcome: `{outcome}`" in report
    assert "| ingress | empty | 1 | 0.000 |" in report
    assert "### Request outcome: `success`" not in report


@pytest.mark.parametrize("duration", [-1., float("nan"), float("inf")])
def test_stage_report_rejects_invalid_durations_before_rendering(duration):
    from advisor_deploy.bench.report import render_report

    result = stage_report_result()
    result.telemetry[0]["stages"][0]["duration_ms"] = duration
    with pytest.raises(ValueError, match="invalid benchmark"):
        render_report(result)


def completion(span="span-1", name="send", duration=25., **changes):
    return {
        "event": "benchmark_stage_completed", "version": 1,
        "trace_id": "trace-a", "job_id": "job-a", "boot_id": "boot-1",
        "segment_id": "segment-1", "span_id": span,
        "stage": {"name": name, "duration_ms": duration, "outcome": "success"},
    } | changes


def write_stage_export(tmp_path, records):
    path = tmp_path / "stage-export.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def test_stage_logs_join_exact_pairs_dedupe_and_preserve_raw_result(tmp_path):
    from advisor_deploy.bench.report import load_stage_logs, render_report

    result = stage_report_result()
    for request_id, durations in (("a", [0., 10.]), ("b", [20., 40.])):
        record = next(item for item in result.telemetry if item["request_id"] == request_id)
        record["stages"].extend(
            {"name": "history_read", "duration_ms": value, "outcome": "success"}
            for value in durations)
    before = asdict(result)
    first = completion("span-a1", "history_read", 0.)
    path = write_stage_export(tmp_path, [
        first, completion("span-a2", "history_read", 10.), first.copy(),
        completion("span-send", "send", 25.),
        completion("span-ingress", "ingress", 4., job_id="job-d", trace_id="trace-d"),
    ])
    original = path.read_bytes()
    imported = load_stage_logs(path, window_start="2026-01-01T00:00:00Z",
                               window_end="2026-01-01T00:01:00Z")
    report = render_report(result, stage_logs=imported)
    assert asdict(result) == before
    assert path.read_bytes() == original
    assert sha256(original).hexdigest() in report
    assert path.resolve().as_uri() in report
    assert "Export records: 5; unique event IDs: 4; exact duplicate records: 1" in report
    assert "operator-declared" in report and "2026-01-01T00:01:00Z" in report
    assert "Superseded callback observations: 2" in report
    success = report.split("### Request outcome: `success`")[1].split("### Request outcome:")[0]
    assert "| history_read | 4 | 2/3 | 1 | 66.67% |" in success
    assert "| send | 1 | 1/3 | 2 | 33.33% |" in success
    assert "| history_read | callback | success | 2 | 30.000 |" in success
    assert "| history_read | completed log | success | 2 | 5.000 |" in success
    assert "| send | completed log | success | 1 | 25.000 |" in success
    assert "| history_read | 2 | 2 | 1 | 1 | 2 |" in success
    assert "| agent | callback | success | 4 | 17.500 |" in success
    assert "| success | 3 | 1666.667 | 2000.000 |" in report
    assert result.cold == before["cold"]
    assert result.samples[-1].status == "censored"


def test_distinct_process_span_identities_remain_distinct_observed_attempts(tmp_path):
    from advisor_deploy.bench.report import load_stage_logs, render_report

    path = write_stage_export(tmp_path, [
        completion(duration=5.),
        completion(duration=10., segment_id="segment-2"),
        completion(duration=15., boot_id="boot-2"),
    ])
    report = render_report(stage_report_result(), stage_logs=load_stage_logs(path))
    assert "unique event IDs: 3; exact duplicate records: 0" in report
    assert "| send | completed log | success | 3 | 10.000 | 10.000 | 14.500 | 14.900 | 5.000 | 15.000 |" in report
    assert "Log window: not supplied" in report


def test_completed_ingress_history_and_send_backfill_after_callback_without_new_cold_proof(tmp_path):
    from advisor_deploy.bench.report import load_stage_logs, render_report

    result = stage_report_result()
    original = next(item for item in result.telemetry if item["request_id"] == "a")
    original["stages"].append({"name": "history_read", "duration_ms": 7., "outcome": "success"})
    before = asdict(result)
    path = write_stage_export(tmp_path, [
        completion("ingress", "ingress", 12.), completion("read", "history_read", 7.),
        completion("commit", "history_commit", 20.), completion("send", "send", 30.),
    ])
    report = render_report(result, stage_logs=load_stage_logs(
        path, window_start="2026-01-01T00:00:00Z", window_end="2026-01-01T01:00:00Z"))
    for name, duration in (("ingress", 12.), ("history_read", 7.), ("history_commit", 20.), ("send", 30.)):
        assert f"| {name} | completed log | success | 1 | {duration:.3f} |" in report
    assert "Superseded callback observations: 1" in report
    assert "Cold classification: **unverified**" in report
    assert asdict(result) == before
    assert result.samples[0].finished_s == 2.
    assert "may finish after callback reception" in report


def test_completed_logs_have_explicit_source_precedence_without_fuzzy_identity_matching(tmp_path):
    from advisor_deploy.bench.report import load_stage_logs, render_report

    result = stage_report_result()
    for request_id, durations in (("a", [0., 10.]), ("b", [20., 40.])):
        record = next(item for item in result.telemetry if item["request_id"] == request_id)
        record["stages"].extend(
            {"name": "history_read", "duration_ms": value, "outcome": "success"}
            for value in durations)
    before = asdict(result)
    path = write_stage_export(tmp_path, [completion("completed-read", "history_read", 99.)])
    report = render_report(result, stage_logs=load_stage_logs(path))
    success = report.split("### Request outcome: `success`")[1].split("### Request outcome:")[0]
    assert "| history_read | 3 | 2/3 | 1 | 66.67% |" in success
    assert "| history_read | completed log | success | 1 | 99.000 |" in success
    assert "Superseded callback observations: 2" in report
    assert "not claimed identity deduplication" in report
    assert "partial export can supersede a more complete callback stage list" in report
    assert asdict(result) == before


@pytest.mark.parametrize("name", ["agent", "model", "render", "history_setup", "synthetic"])
def test_completed_log_import_cannot_replace_other_callback_stage_scopes(tmp_path, name):
    from advisor_deploy.bench.report import load_stage_logs

    path = write_stage_export(tmp_path, [completion(name=name)])
    with pytest.raises(ValueError, match="stage-log import"):
        load_stage_logs(path)


def test_timed_export_validates_collection_window_using_emission_metadata(tmp_path):
    from advisor_deploy.bench.report import load_stage_logs, render_report

    path = write_stage_export(tmp_path, [
        {"timestamp": "2026-01-01T00:00:01Z", "completion": completion("send-1")},
        {"timestamp": "2026-01-01T00:00:02+00:00", "completion": completion("send-1")},
        {"timestamp": "2026-01-01T00:00:03Z", "completion": completion("send-2", duration=30.)},
    ])
    imported = load_stage_logs(path, window_start="2026-01-01T00:00:00Z",
                               window_end="2026-01-01T00:00:04Z")
    report = render_report(stage_report_result(), stage_logs=imported)
    assert "verified against 3 supplied UTC log-emission timestamps" in report
    assert "Observed log-emission range: 2026-01-01T00:00:01+00:00 to 2026-01-01T00:00:03+00:00" in report
    assert "unique event IDs: 2; exact duplicate records: 1" in report
    assert "| send | completed log | success | 2 | 27.500 |" in report
    assert "does not attest Azure collection or complete attempt coverage" in report


@pytest.mark.parametrize("timestamp", [
    "2025-12-31T23:59:59Z", "2026-01-01T00:00:05Z",
    "2026-01-01T00:00:01", "2026-01-01T00:00:01+08:00",
])
def test_timed_export_rejects_non_utc_or_out_of_window_records(tmp_path, timestamp):
    from advisor_deploy.bench.report import load_stage_logs

    path = write_stage_export(tmp_path, [{"timestamp": timestamp, "completion": completion()}])
    with pytest.raises(ValueError, match="stage-log"):
        load_stage_logs(path, window_start="2026-01-01T00:00:00Z",
                        window_end="2026-01-01T00:00:04Z")


def test_timed_export_cannot_hide_out_of_window_duplicates_or_raw_envelopes(tmp_path):
    from advisor_deploy.bench.report import load_stage_logs

    valid = {"timestamp": "2026-01-01T00:00:01Z", "completion": completion()}
    invalid_sets = [
        [valid, valid | {"timestamp": "2026-01-01T00:01:00Z"}],
        [valid | {"message": "private-input"}],
        [valid, completion()],
        [{"timestamp": valid["timestamp"], **completion()}],
    ]
    for records in invalid_sets:
        path = write_stage_export(tmp_path, records)
        with pytest.raises(ValueError) as error:
            load_stage_logs(path, window_start="2026-01-01T00:00:00Z",
                            window_end="2026-01-01T00:00:04Z")
        assert "private-input" not in str(error.value)


def test_empty_stage_export_does_not_create_coverage(tmp_path):
    from advisor_deploy.bench.report import load_stage_logs, render_report

    path = write_stage_export(tmp_path, [])
    report = render_report(stage_report_result(), stage_logs=load_stage_logs(path))
    assert "Export records: 0; unique event IDs: 0; exact duplicate records: 0" in report
    assert "| send | none | not observed | 0 | n/a |" in report
    assert "Superseded callback observations: 0" in report


def test_stage_log_import_enforces_byte_and_record_bounds(tmp_path, monkeypatch):
    import advisor_deploy.bench.report as reports

    path = write_stage_export(tmp_path, [completion(), completion("second")])
    with monkeypatch.context() as limits:
        limits.setattr(reports, "MAX_STAGE_LOG_BYTES", 16)
        with pytest.raises(ValueError, match="stage-log import"):
            reports.load_stage_logs(path)
    with monkeypatch.context() as limits:
        limits.setattr(reports, "MAX_STAGE_LOG_RECORDS", 1)
        with pytest.raises(ValueError, match="stage-log import"):
            reports.load_stage_logs(path)


@pytest.mark.parametrize("change", [
    {"stage": {"name": "send", "duration_ms": 26., "outcome": "success"}},
    {"stage": {"name": "send", "duration_ms": 25., "outcome": "failed"}},
    {"job_id": "other-job"}, {"trace_id": "other-trace"},
])
def test_conflicting_log_identity_is_rejected_not_last_write_wins(tmp_path, change):
    from advisor_deploy.bench.report import load_stage_logs

    path = write_stage_export(tmp_path, [completion(), completion(**change)])
    with pytest.raises(ValueError, match="conflicting stage"):
        load_stage_logs(path)


@pytest.mark.parametrize("change", [
    {"message": "private-input"}, {"timestamp": "2026-01-01T00:00:00Z"},
    {"reply": {"text": "private-input"}}, {"event": "step_completed"}, {"version": True},
    {"version": 2}, {"trace_id": None}, {"job_id": None}, {"span_id": ""},
    {"stage": {"name": "send", "duration_ms": float("nan"), "outcome": "success"}},
    {"stage": {"name": "send", "duration_ms": -1., "outcome": "success"}},
])
def test_stage_log_import_accepts_only_strict_safe_completion_dto(tmp_path, change):
    from advisor_deploy.bench.report import load_stage_logs

    path = write_stage_export(tmp_path, [completion(**change)])
    with pytest.raises(ValueError) as error:
        load_stage_logs(path)
    assert "private-input" not in str(error.value)


def test_stage_log_import_rejects_raw_log_envelopes_and_duplicate_json_fields(tmp_path):
    from advisor_deploy.bench.report import load_stage_logs

    path = tmp_path / "stage-export.json"
    for raw in (
        '{"records":[]}', '{"tables":[]}',
        '[' + json.dumps(completion())[:-1] + ',"version":1}]',
        json.dumps([completion()]).encode("utf-16"),
    ):
        path.write_bytes(raw.encode() if isinstance(raw, str) else raw)
        with pytest.raises(ValueError):
            load_stage_logs(path)


@pytest.mark.parametrize("change", [
    {"job_id": "unmatched-job"}, {"trace_id": "unmatched-trace"},
])
def test_unmatched_log_job_or_trace_never_attaches_to_a_request(tmp_path, change):
    from advisor_deploy.bench.report import load_stage_logs, render_report

    path = write_stage_export(tmp_path, [completion(**change)])
    with pytest.raises(ValueError, match="stage-log correlation"):
        render_report(stage_report_result(), stage_logs=load_stage_logs(path))


@pytest.mark.parametrize("missing", ["job_id", "trace_id"])
def test_missing_callback_correlation_cannot_be_reconstructed_from_logs(tmp_path, missing):
    from advisor_deploy.bench.report import load_stage_logs, render_report

    result = stage_report_result()
    record = next(item for item in result.telemetry if item["request_id"] == "a")
    record[missing] = None
    path = write_stage_export(tmp_path, [completion()])
    with pytest.raises(ValueError, match="stage-log correlation"):
        render_report(result, stage_logs=load_stage_logs(path))


def test_two_requests_cannot_claim_the_same_job_and_trace_pair(tmp_path):
    from advisor_deploy.bench.report import load_stage_logs, render_report

    result = stage_report_result()
    result.telemetry[0].update(job_id="job-a", trace_id="trace-a")
    path = write_stage_export(tmp_path, [completion()])
    with pytest.raises(ValueError, match="stage-log correlation"):
        render_report(result, stage_logs=load_stage_logs(path))


@pytest.mark.parametrize("start,end", [
    ("2026-01-01T00:00:00Z", None),
    (None, "2026-01-01T00:01:00Z"),
    ("2026-01-01T00:00:00", "2026-01-01T00:01:00Z"),
    ("2026-01-01T00:02:00Z", "2026-01-01T00:01:00Z"),
    ("2026-01-01\n00:00:00Z", "2026-01-01T00:01:00Z"),
])
def test_import_window_is_explicit_bounded_metadata_not_inferred(tmp_path, start, end):
    from advisor_deploy.bench.report import load_stage_logs

    path = write_stage_export(tmp_path, [])
    with pytest.raises(ValueError, match="stage-log window"):
        load_stage_logs(path, window_start=start, window_end=end)


def test_report_cli_imports_logs_without_overwriting_either_source(tmp_path, capsys):
    from advisor_deploy.bench.__main__ import main
    from advisor_deploy.bench.report import write_result

    raw_path = tmp_path / "raw.json"
    write_result(stage_report_result(), raw_path)
    original = raw_path.read_bytes()
    logs = write_stage_export(tmp_path, [completion()])
    log_bytes = logs.read_bytes()
    output = tmp_path / "report.md"
    assert main(["report", "--input", str(raw_path), "--output", str(output),
                 "--stage-logs", str(logs), "--stage-log-window-start", "2026-01-01T00:00:00Z",
                 "--stage-log-window-end", "2026-01-01T00:01:00Z"]) == 0
    assert "| send | completed log | success | 1 | 25.000 |" in output.read_text()
    assert sha256(original).hexdigest() in output.read_text()
    assert raw_path.resolve().as_uri() in output.read_text()
    assert raw_path.read_bytes() == original and logs.read_bytes() == log_bytes
    for protected in (raw_path, logs):
        assert main(["report", "--input", str(raw_path), "--output", str(protected),
                     "--stage-logs", str(logs)]) == 2
    assert raw_path.read_bytes() == original and logs.read_bytes() == log_bytes
    assert "private-input" not in capsys.readouterr().out


def test_report_cli_rejects_unmatched_import_before_creating_output(tmp_path):
    from advisor_deploy.bench.__main__ import main
    from advisor_deploy.bench.report import write_result

    raw_path = tmp_path / "raw.json"
    write_result(stage_report_result(), raw_path)
    original = raw_path.read_bytes()
    logs = write_stage_export(tmp_path, [completion(job_id="unmatched")])
    output = tmp_path / "report.md"
    assert main(["report", "--input", str(raw_path), "--output", str(output),
                 "--stage-logs", str(logs)]) == 2
    assert not output.exists()
    assert raw_path.read_bytes() == original
    assert main(["report", "--input", str(raw_path), "--output", str(output),
                 "--stage-log-window-start", "2026-01-01T00:00:00Z"]) == 2
    assert not output.exists()


@pytest.mark.parametrize("protected", ["raw", "logs"])
def test_report_cli_cannot_overwrite_a_hard_link_to_input_evidence(tmp_path, protected):
    from advisor_deploy.bench.__main__ import main
    from advisor_deploy.bench.report import write_result

    raw_path = tmp_path / "raw.json"
    write_result(stage_report_result(), raw_path)
    logs = write_stage_export(tmp_path, [completion()])
    source = raw_path if protected == "raw" else logs
    before = source.read_bytes()
    output = tmp_path / "alias.md"
    os.link(source, output)
    assert main(["report", "--input", str(raw_path), "--output", str(output),
                 "--stage-logs", str(logs)]) == 2
    assert source.read_bytes() == before


@pytest.fixture
async def azurite_results():
    if os.environ.get("ADVISOR_BENCH_AZURITE") != "1":
        pytest.skip("set ADVISOR_BENCH_AZURITE=1 for real loopback Azurite evidence")
    from azure.core.exceptions import ResourceNotFoundError
    from azure.storage.blob.aio import ContainerClient

    # Published Azurite development-only credential; never used for Azure.
    key = ("Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsu"
           "Fq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==")
    async with ContainerClient(
        "http://127.0.0.1:11000/devstoreaccount1", "bench-contract-" + uuid4().hex,
        credential=key, retry_total=0, api_version="2023-11-03",
    ) as container:
        await container.create_container()
        try:
            yield container
        finally:
            try:
                await container.delete_container()
            except ResourceNotFoundError:
                pass


async def test_bounded_evidence_queue_rejects_payloads_and_overflow(tmp_path):
    from advisor_deploy.bench.sink import EvidenceEvent, EvidenceFailure, EvidenceSink, FileEvidenceStore
    from test_bench_runner import local_plan

    async with EvidenceSink(FileEvidenceStore(tmp_path), max_pending=1) as sink:
        await sink.start_run("run-1", local_plan())
        sink.emit(EvidenceEvent(run_id="run-1", kind="request_intent",
                                request_id="request-1", case_id="kb-01"))
        with pytest.raises(EvidenceFailure):
            sink.emit(EvidenceEvent(run_id="run-1", kind="request_intent",
                                    request_id="request-2", case_id="kb-01"))
        assert sink.failed
        private = "not-for-evidence"
        with pytest.raises(ValueError) as error:
            EvidenceEvent(run_id="run-1", kind="final", reply=private)
        assert private not in str(error.value)


async def test_actual_http_answers_are_durable_in_real_azurite_without_bodies(azurite_results):
    from advisor_deploy.bench.runner import run_level
    from advisor_deploy.bench.sink import BlobEvidenceStore, EvidenceSink
    from test_bench_runner import ack, harness

    emitted_stages = []

    async def handler(body, send, tasks):
        stages = []
        for name in ("history_setup", "synthetic"):
            started = time.perf_counter()
            await asyncio.sleep(0)
            stages.append({"name": name, "duration_ms": (time.perf_counter() - started) * 1000,
                           "outcome": "success"})
        emitted_stages.append(stages)
        await send(body, telemetry={"stages": stages})
        return ack(body)

    async with EvidenceSink(BlobEvidenceStore(azurite_results)) as sink:
        async with harness(handler) as values:
            plan, submit, receiver, _, _ = values
            result = await run_level(plan, submit, receiver, concurrency=1, evidence_sink=sink)
            assert result.stop_reason == "target_reached"
        final = json.loads(await (await azurite_results.download_blob(
            f"{result.run_id}/final.json")).readall())
        assert final["samples"][0]["status"] == "success"
        assert final["telemetry"][0]["stages"] == emitted_stages[0]
        checkpoint = json.loads(await (await azurite_results.download_blob(
            f"{result.run_id}/checkpoint.json")).readall())
        assert checkpoint["state"] == "finished"
        assert checkpoint["pending"] == {}
        names = [blob.name async for blob in azurite_results.list_blobs()]
        assert any("/events/" in name for name in names)
        for name in names:
            raw = (await (await azurite_results.download_blob(name)).readall()).decode()
            assert "Complete rendered local test reply." not in raw
            assert '"message"' not in raw
            assert '"reply"' not in raw
            assert '"question"' not in raw
            assert "Bearer " not in raw


async def test_interruption_preserves_censored_final_and_checkpoint_in_azurite(azurite_results):
    from advisor_deploy.bench.runner import run_level
    from advisor_deploy.bench.sink import BlobEvidenceStore, EvidenceSink
    from test_bench_runner import ack, harness

    admitted = asyncio.Event()

    async def handler(body, send, tasks):
        admitted.set()
        return ack(body)

    async with EvidenceSink(BlobEvidenceStore(azurite_results)) as sink:
        async with harness(handler) as values:
            plan, submit, receiver, _, _ = values
            task = asyncio.create_task(run_level(plan, submit, receiver, concurrency=1,
                                                 evidence_sink=sink))
            await admitted.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            run_id = next(iter(receiver.origins))
        final = json.loads(await (await azurite_results.download_blob(
            f"{run_id}/final.json")).readall())
        assert final["stop_reason"] == "driver_interrupted"
        assert final["samples"][0]["status"] == "censored"
        assert final["samples"][0]["finished_s"] is None
        checkpoint = json.loads(await (await azurite_results.download_blob(
            f"{run_id}/checkpoint.json")).readall())
        assert checkpoint["state"] == "interrupted"


async def test_storage_loss_stops_submissions_and_preserves_local_failure_result(
    azurite_results, tmp_path,
):
    from advisor_deploy.bench.report import write_result
    from advisor_deploy.bench.runner import run_level
    from advisor_deploy.bench.sink import BlobEvidenceStore, EvidenceSink
    from test_bench_runner import ack, harness

    async def handler(body, send, tasks):
        await azurite_results.delete_container()
        return ack(body)

    async with EvidenceSink(BlobEvidenceStore(azurite_results)) as sink:
        async with harness(handler) as values:
            plan, submit, receiver, requests, _ = values
            result = await run_level(
                plan, submit, receiver, concurrency=1, evidence_sink=sink,
                result_sink=lambda result: write_result(result, tmp_path / "fallback.json"))
            assert result.stop_reason == "observer_evidence_failure"
            assert len(requests) == 1
            assert result.samples[0].status == "censored"
            assert json.loads((tmp_path / "fallback.json").read_text())["stop_reason"] == (
                "observer_evidence_failure")


async def test_missing_azurite_container_is_not_automatically_recreated(azurite_results):
    from azure.core.exceptions import ResourceNotFoundError
    from advisor_deploy.bench.sink import BlobEvidenceStore, EvidenceFailure, EvidenceSink

    await azurite_results.delete_container()
    with pytest.raises(EvidenceFailure):
        async with EvidenceSink(BlobEvidenceStore(azurite_results)):
            raise AssertionError("missing pre-provisioned container must fail closed")
    with pytest.raises(ResourceNotFoundError):
        await azurite_results.get_container_properties()


async def test_cancel_during_zero_wait_preserves_an_interrupted_preparation_checkpoint(tmp_path):
    from advisor_deploy.bench.runner import run_level
    from advisor_deploy.bench.sink import EvidenceSink, FileEvidenceStore
    from test_bench_runner import ack, harness

    waiting = asyncio.Event()

    async def handler(body, send, tasks):
        raise AssertionError("cold wait must not submit a query")

    async def before_load(since):
        waiting.set()
        await asyncio.Event().wait()

    async with EvidenceSink(FileEvidenceStore(tmp_path)) as sink:
        async with harness(handler, profile="cold") as values:
            plan, submit, receiver, requests, registrations = values
            task = asyncio.create_task(run_level(plan, submit, receiver, concurrency=1,
                                                 before_load=before_load, evidence_sink=sink))
            await waiting.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            run_id = registrations[0]["run_id"]
            assert requests == []
        checkpoint = json.loads((tmp_path / run_id / "checkpoint.json").read_text())
        assert checkpoint["state"] == "interrupted"
        final = json.loads((tmp_path / run_id / "final.json").read_text())
        assert final["samples"] == []


@pytest.mark.parametrize("field", ["driver", "observations", "cold"])
def test_result_metadata_never_accepts_untyped_payload_fields(field, tmp_path):
    from advisor_deploy.bench.report import write_result
    from advisor_deploy.bench.runner import RunResult

    result = RunResult(
        run_id="test-run", profile="local-smoke", concurrency=1,
        started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:00:01Z",
        window_seconds=1., submission_seconds=1., stop_reason="target_reached",
        samples=[Sample("a", "kb-01", "success", 0., .5)],
        plan={"target_completions": 1, "min_seconds": 0.})
    setattr(result, field, {"reply": "private-rendered-body"})
    path = tmp_path / "result.json"
    with pytest.raises(ValueError) as error:
        write_result(result, path)
    assert "private-rendered-body" not in str(error.value)
    assert not path.exists()


async def test_finalization_checkpoint_failure_cannot_publish_a_clean_final(tmp_path):
    from advisor_deploy.bench.runner import run_level
    from advisor_deploy.bench.sink import EvidenceSink, FileEvidenceStore
    from test_bench_runner import ack, harness

    class FailingFinalCheckpoint(FileEvidenceStore):
        async def write(self, name, data, *, overwrite):
            if name.endswith("/checkpoint.json") and json.loads(data)["state"] == "finished":
                raise OSError("injected finalization checkpoint failure")
            await super().write(name, data, overwrite=overwrite)

    async def handler(body, send, tasks):
        await send(body)
        return ack(body)

    async with EvidenceSink(FailingFinalCheckpoint(tmp_path)) as sink:
        async with harness(handler) as values:
            plan, submit, receiver, _, _ = values
            result = await run_level(plan, submit, receiver, concurrency=1, evidence_sink=sink)
        assert result.stop_reason == "observer_evidence_failure"
        final = json.loads((tmp_path / result.run_id / "final.json").read_text())
        assert final["stop_reason"] == "observer_evidence_failure"
        assert final["samples"][0]["status"] == "success"
        assert list((tmp_path / result.run_id / "events").glob("*.json"))


async def test_final_result_is_the_last_successful_durable_commit(tmp_path):
    from advisor_deploy.bench.runner import run_level
    from advisor_deploy.bench.sink import EvidenceSink, FileEvidenceStore
    from test_bench_runner import ack, harness

    committed = []

    class RecordingStore(FileEvidenceStore):
        async def write(self, name, data, *, overwrite):
            await super().write(name, data, overwrite=overwrite)
            committed.append(name)

    async def handler(body, send, tasks):
        await send(body)
        return ack(body)

    async with EvidenceSink(RecordingStore(tmp_path)) as sink:
        async with harness(handler) as values:
            plan, submit, receiver, _, _ = values
            result = await run_level(plan, submit, receiver, concurrency=1, evidence_sink=sink)
    assert committed[-2:] == [f"{result.run_id}/checkpoint.json", f"{result.run_id}/final.json"]
    assert json.loads((tmp_path / result.run_id / "final.json").read_text())["stop_reason"] == (
        "target_reached")
