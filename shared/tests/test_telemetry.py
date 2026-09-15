import asyncio
import json
import logging

import pytest


def test_nested_steps_use_monotonic_time_and_keep_repeated_calls(monkeypatch):
    from advisor_shared import telemetry

    now = [10.0]
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: now[0])
    with telemetry.trace_scope() as trace:
        with telemetry.step("parent") as parent:
            now[0] = 10.25
            with telemetry.step("child"):
                now[0] = 10.5
            with telemetry.step("child"):
                now[0] = 10.75
            now[0] = 11.0
    first, second, total = trace.timings
    assert [s.duration_ms for s in trace.timings] == [250, 250, 1000]
    assert [s.start_offset_ms for s in trace.timings] == [250, 500, 0]
    assert first.parent_span_id == second.parent_span_id == parent.span_id
    assert first.span_id != second.span_id
    assert total.parent_span_id is None
    assert telemetry.current_trace() is None


@pytest.mark.parametrize(("error", "status"), [
    (ValueError("private-value"), "error"),
    (TimeoutError("private-value"), "timeout"),
    (asyncio.CancelledError("private-value"), "cancelled"),
])
def test_failed_steps_are_recorded_and_propagate_without_secrets(
        error, status, caplog):
    from advisor_shared.telemetry import step, trace_scope

    with caplog.at_level(logging.DEBUG), trace_scope() as trace:
        with pytest.raises(type(error)):
            with step("operation"):
                raise error
    assert trace.timings[0].status == status
    assert trace.timings[0].error_type == type(error).__name__
    assert trace.timings[0].duration_ms >= 0
    assert "private-value" not in caplog.text
    completed = [r for r in caplog.records if r.msg == "step_completed"]
    assert len(completed) == 1
    assert completed[0].telemetry["status"] == status
    assert "private-value" not in json.dumps(completed[0].telemetry)


def test_info_logs_completion_and_debug_adds_start(caplog):
    from advisor_shared.telemetry import step

    with caplog.at_level(logging.INFO, logger="advisor_shared.telemetry"):
        with step("info"):
            pass
    assert [r.msg for r in caplog.records] == ["step_completed"]
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="advisor_shared.telemetry"):
        with step("debug"):
            pass
    assert [r.msg for r in caplog.records] == ["step_started", "step_completed"]


async def test_concurrent_requests_and_parallel_siblings_are_isolated():
    from advisor_shared.telemetry import current_trace, step, trace_scope

    async def branch():
        with step("branch"):
            await asyncio.sleep(0)

    async def request():
        with trace_scope() as trace:
            with step("request") as root:
                await asyncio.gather(branch(), branch())
            return trace, root

    (first, root1), (second, root2) = await asyncio.gather(request(), request())
    assert first.trace_id != second.trace_id
    assert len(first.timings) == len(second.timings) == 3
    assert {s.parent_span_id for s in first.timings[:2]} == {root1.span_id}
    assert {s.parent_span_id for s in second.timings[:2]} == {root2.span_id}
    assert current_trace() is None


async def test_timed_decorator_preserves_return_signature_and_failure():
    import inspect
    from advisor_shared.telemetry import timed, trace_scope

    @timed("call")
    async def operation(value: int) -> int:
        if value < 0:
            raise ValueError("invalid")
        return value + 1

    assert list(inspect.signature(operation).parameters) == ["value"]
    with trace_scope() as trace:
        assert await operation(4) == 5
        with pytest.raises(ValueError):
            await operation(-1)
    assert [s.status for s in trace.timings] == ["success", "error"]


def test_json_and_text_logs_include_correlation_but_not_exception_body():
    from advisor_shared.logging import TelemetryFormatter
    from advisor_shared.telemetry import step, trace_scope

    error = ValueError("private-body")
    record = logging.LogRecord("advisor_agent.test", logging.ERROR, __file__, 1,
                               "operation failed", (), (ValueError, error, None))
    with trace_scope() as trace, step("operation") as span:
        payload = json.loads(TelemetryFormatter("json").format(record))
        text = TelemetryFormatter("text").format(record)
    assert payload["trace_id"] == trace.trace_id
    assert payload["span_id"] == span.span_id
    assert payload["error_type"] == "ValueError"
    assert payload["level"] == "ERROR"
    assert payload["timestamp"].endswith("+00:00")
    assert trace.trace_id in text
    assert "private-body" not in text
    assert "private-body" not in json.dumps(payload)


def test_logging_configuration_validates_options_before_changing_handlers(
        monkeypatch):
    from advisor_shared.logging import configure_logging

    handlers = list(logging.getLogger().handlers)
    monkeypatch.setenv("ADVISOR_LOG_LEVEL", "DEBIG")
    with pytest.raises(ValueError, match="ADVISOR_LOG_LEVEL"):
        configure_logging()
    assert logging.getLogger().handlers == handlers
    monkeypatch.setenv("ADVISOR_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("ADVISOR_LOG_FORMAT", "invalid")
    with pytest.raises(ValueError, match="ADVISOR_LOG_FORMAT"):
        configure_logging()
