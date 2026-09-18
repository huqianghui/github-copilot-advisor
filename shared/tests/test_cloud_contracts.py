"""Cross-host contracts reject unsafe payloads before persistence or queuing."""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from advisor_shared.cloud import HistoryMessage, PreparedTurn, QueueWake, TaskPayload


UTC_TIME = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


def task_data(**overrides):
    return {
        "job_id": "job-a", "partition_key": "partition-a",
        "activity": {"id": "activity-a", "text": "private-question",
                     "attachments": [{"content": {"items": [None, True, 1, 1.5]}}]},
        "accepted_at": UTC_TIME, "trace_id": "trace-a", "target": "teams",
        **overrides,
    }


def history_data(**overrides):
    return {
        "role": "user", "content": "private-question", "written_at": UTC_TIME,
        "turn_id": "turn-a", "sequence": 1, **overrides,
    }


@pytest.mark.parametrize("model,data", [
    (HistoryMessage, history_data()),
    (TaskPayload, task_data()),
    (QueueWake, {"partition_key": "partition-a", "trace_id": "trace-a"}),
    (PreparedTurn, {"reply": {"text": "private-answer"}, "history": [
        {"role": "user", "content": "private-question"},
        {"role": "assistant", "content": "private-answer"}], "outcome": "success"}),
])
def test_contracts_round_trip_json_without_discarding_fields(model, data):
    value = model.model_validate(data)
    assert model.model_validate_json(value.model_dump_json()) == value
    assert "private-question" not in repr(value)
    assert "private-answer" not in repr(value)


@pytest.mark.parametrize("model,data", [
    (HistoryMessage, history_data()),
    (TaskPayload, task_data()),
    (QueueWake, {"partition_key": "partition-a", "trace_id": "trace-a"}),
    (PreparedTurn, {"reply": None, "history": [], "outcome": "ignored"}),
])
def test_contracts_reject_extra_fields_and_hide_input_in_errors(model, data):
    with pytest.raises(ValidationError) as error:
        model.model_validate({**data, "authorization": "private-bearer"})
    assert "private-bearer" not in str(error.value)
    assert "private-question" not in str(error.value)


def test_queue_message_never_accepts_activity_or_bearer():
    with pytest.raises(ValueError):
        QueueWake.model_validate({
            "partition_key": "partition-a", "trace_id": "trace-a",
            "activity": {"text": "private"}, "authorization": "private",
        })
    assert QueueWake(partition_key="partition-a", trace_id="trace-a").model_dump() == {
        "partition_key": "partition-a", "trace_id": "trace-a", "version": 1,
    }


@pytest.mark.parametrize("version", [0, 2, "1", True])
def test_queue_rejects_unsupported_or_coerced_versions(version):
    with pytest.raises(ValueError):
        QueueWake(partition_key="partition-a", trace_id="trace-a", version=version)


@pytest.mark.parametrize("timestamp", [
    datetime(2026, 9, 17),
    datetime(2026, 9, 17, tzinfo=timezone(timedelta(hours=8))),
    "2026-09-17T12:00:00",
    "2026-09-17T12:00:00+08:00",
])
@pytest.mark.parametrize("model,data,field", [
    (HistoryMessage, history_data(), "written_at"),
    (TaskPayload, task_data(), "accepted_at"),
])
def test_persisted_timestamps_must_be_aware_utc(model, data, field, timestamp):
    with pytest.raises(ValueError):
        model.model_validate({**data, field: timestamp})


@pytest.mark.parametrize("field,value", [
    ("role", "system"), ("sequence", -1), ("sequence", True),
    ("sequence", "1"), ("turn_id", ""),
])
def test_history_rejects_invalid_role_sequence_or_turn_id(field, value):
    with pytest.raises(ValueError):
        HistoryMessage.model_validate(history_data(**{field: value}))


@pytest.mark.parametrize("activity", [
    {"Authorization": "private-bearer"},
    {"headers": {"authorization": "private-bearer"}},
    {"attachments": [{"content": {"AUTHORIZATION": "private-bearer"}}]},
    {"text": b"private-bytes"},
    {"value": object()},
    {"value": float("nan")},
])
def test_task_activity_is_json_without_authorization(activity):
    with pytest.raises(ValueError) as error:
        TaskPayload.model_validate(task_data(activity=activity))
    assert "private-bearer" not in str(error.value)
    assert "private-bytes" not in str(error.value)


@pytest.mark.parametrize("overrides", [
    {"target": "email"},
    {"target": "benchmark"},
    {"target": "benchmark", "run_id": "run-a"},
    {"target": "benchmark", "request_id": "request-a"},
    {"target": "benchmark", "run_id": "", "request_id": "request-a"},
])
def test_benchmark_tasks_require_both_correlation_ids(overrides):
    with pytest.raises(ValueError):
        TaskPayload.model_validate(task_data(**overrides))


def test_benchmark_correlation_ids_survive_storage_round_trip():
    task = TaskPayload.model_validate(task_data(
        target="benchmark", run_id="run-a", request_id="request-a"))
    restored = TaskPayload.model_validate_json(task.model_dump_json())
    assert (restored.run_id, restored.request_id) == ("run-a", "request-a")
    assert TaskPayload.model_validate(task_data()).request_id is None


@pytest.mark.parametrize("history", [
    [{"role": "user", "content": "private-question"}],
    [{"role": "assistant", "content": "private-answer"},
     {"role": "user", "content": "private-question"}],
    [{"role": "system", "content": "private-question"},
     {"role": "assistant", "content": "private-answer"}],
    [{"role": "user", "content": "private-question", "extra": "private-extra"},
     {"role": "assistant", "content": "private-answer"}],
    [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"},
     {"role": "user", "content": "next"}],
])
def test_prepared_history_is_empty_or_one_ordered_question_answer_pair(history):
    with pytest.raises(ValueError) as error:
        PreparedTurn(reply=None, history=history, outcome="degraded")
    assert "private-question" not in str(error.value)
    assert "private-extra" not in str(error.value)


@pytest.mark.parametrize("outcome", ["success", "degraded", "ignored"])
def test_prepared_turn_history_remains_plain_role_content_dicts(outcome):
    history = [{"role": "user", "content": "q"},
               {"role": "assistant", "content": "a"}]
    prepared = PreparedTurn(reply={"type": "message", "text": "a"},
                            history=history, outcome=outcome)
    assert prepared.history == history
    assert PreparedTurn(reply=None, history=[], outcome=outcome).history == []


def test_prepared_turn_rejects_unknown_outcome():
    with pytest.raises(ValueError):
        PreparedTurn(reply=None, history=[], outcome="pending")
