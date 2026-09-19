from dataclasses import FrozenInstanceError

import pytest


def test_attempts_are_typed_deduplicated_and_local_to_the_captured_trace():
    from advisor_shared.telemetry import ModelHttpAttempt, trace_scope

    attempt = ModelHttpAttempt("a" * 32, 1, "chat.completions", 429)
    with trace_scope(new=True, trace_id="job-a") as original:
        with trace_scope(new=True, trace_id="job-b") as unrelated:
            original.observe_model_http_attempt(attempt)
            original.observe_model_http_attempt(attempt)
            assert unrelated.model_http_attempts == ()
    assert original.model_http_attempts == (attempt,)
    with pytest.raises(FrozenInstanceError):
        attempt.http_status = 200


def test_conflicting_duplicate_does_not_export_false_throttle_counts():
    from advisor_shared.telemetry import ModelHttpAttempt, TraceContext

    trace = TraceContext()
    trace.observe_model_http_attempt(ModelHttpAttempt("a" * 32, 1, "embeddings", 429))
    trace.observe_model_http_attempt(ModelHttpAttempt("a" * 32, 1, "embeddings", 200))
    assert trace.model_http_attempts is None


def test_attempt_storage_is_bounded_and_overflow_is_unknown_not_partial():
    from advisor_shared.telemetry import MAX_MODEL_HTTP_ATTEMPTS, ModelHttpAttempt, TraceContext

    trace = TraceContext()
    for number in range(1, MAX_MODEL_HTTP_ATTEMPTS + 1):
        trace.observe_model_http_attempt(ModelHttpAttempt("a" * 32, number, "embeddings", 200))
    assert len(trace.model_http_attempts) == MAX_MODEL_HTTP_ATTEMPTS
    trace.observe_model_http_attempt(ModelHttpAttempt(
        "a" * 32, MAX_MODEL_HTTP_ATTEMPTS + 1, "embeddings", 429))
    assert trace.model_http_attempts is None


@pytest.mark.parametrize("values", [
    ("private-body", 1, "embeddings", 200),
    ("a" * 32, True, "embeddings", 200),
    ("a" * 32, 0, "embeddings", 200),
    ("a" * 32, 1, "private-operation", 200),
    ("a" * 32, 1, "embeddings", True),
    ("a" * 32, 1, "embeddings", 999),
])
def test_invalid_observation_marks_data_unknown_without_logging_or_interrupting(values, caplog):
    from advisor_shared.telemetry import ModelHttpAttempt, TraceContext

    trace = TraceContext()
    trace.observe_model_http_attempt(ModelHttpAttempt(*values))
    assert trace.model_http_attempts is None
    assert not caplog.records
