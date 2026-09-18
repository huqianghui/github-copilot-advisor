import pytest
from pydantic import ValidationError
from copy import deepcopy
from pathlib import Path
import json


def test_requests_cannot_override_identity_question_or_callback():
    from advisor_shared.benchmark import BenchmarkRequest

    body = dict(run_id="run-1", request_id="request-1", case_id="kb-01",
                virtual_user=0, scenario="synthetic")
    assert BenchmarkRequest(**body).virtual_user == 0
    for extra in ("callback_url", "question", "user_id", "conversation_id"):
        with pytest.raises(ValidationError):
            BenchmarkRequest(**body, **{extra: "untrusted"})
    with pytest.raises(ValidationError):
        BenchmarkRequest(**(body | {"virtual_user": True}))


def test_final_reply_requires_outcome_and_rejects_untyped_body_fields():
    from advisor_shared.benchmark import BenchmarkReply

    body = dict(run_id="run-1", request_id="request-1", job_id="job-1")
    assert BenchmarkReply(**body, event="typing").outcome is None
    with pytest.raises(ValidationError):
        BenchmarkReply(**body, event="final")
    with pytest.raises(ValidationError):
        BenchmarkReply(**body, event="typing", outcome="success")
    with pytest.raises(ValidationError):
        BenchmarkReply(**body, event="final", outcome="success", text="secret")
    with pytest.raises(ValidationError):
        BenchmarkReply(**body, event="final", outcome="success",
                       telemetry={"duration_ms": float("nan")})


def test_only_final_answers_require_strict_transient_rendered_message():
    from advisor_shared.benchmark import BenchmarkReply

    body = dict(run_id="run-1", request_id="request-1", job_id="job-1", event="final")
    private = "private rendered content"
    message = dict(type="message", format="markdown", text=private)
    reply = BenchmarkReply(**body, outcome="success", reply=message)
    assert reply.reply.text == private
    assert reply.model_dump()["reply"]["text"] == private
    assert "message" not in reply.model_dump()
    assert private not in repr(reply)
    for outcome in ("success", "degraded"):
        for invalid in (None, message | {"text": " "}, message | {"url": private}):
            with pytest.raises(ValidationError) as error:
                BenchmarkReply(**body, outcome=outcome, reply=invalid)
            assert private not in str(error.value)
    for outcome in ("failed", "ignored", "unknown", "expired"):
        assert BenchmarkReply(**body, outcome=outcome).reply is None
    with pytest.raises(ValidationError):
        BenchmarkReply(**(body | {"event": "typing"}), reply=message)
    with pytest.raises(ValidationError):
        BenchmarkReply(**body, outcome="success", message=message)


def test_conversation_modes_do_not_accidentally_accumulate_empty_history():
    from advisor_shared.benchmark import BenchmarkRequest, conversation_id

    first = BenchmarkRequest(run_id="run-1", request_id="a", case_id="kb-01",
                             virtual_user=0, scenario="synthetic")
    second = first.model_copy(update={"request_id": "b"})
    assert conversation_id(first, "empty") != conversation_id(second, "empty")
    assert conversation_id(first, "seeded20") != conversation_id(second, "seeded20")
    assert conversation_id(first, "same_user") == conversation_id(second, "same_user")
    assert conversation_id(first, "same_user") != conversation_id(
        first.model_copy(update={"run_id": "run-2"}), "same_user")


def test_registration_is_bounded_and_has_no_request_chosen_destination():
    from advisor_shared.benchmark import BenchmarkRunRegistration

    values = dict(run_id="run-1", profile="warm", case_ids=["kb-01"], seed=7,
                  scenario="synthetic", history_mode="empty", max_requests=100,
                  max_concurrency=1, expires_at="2027-01-01T00:00:00Z",
                  dataset_sha256="a" * 64)
    assert BenchmarkRunRegistration(**values).max_requests == 100
    for changes in ({"case_ids": []}, {"max_requests": 0},
                    {"max_concurrency": 129}, {"case_ids": ["kb-01", "kb-01"]},
                    {"expires_at": "2027-01-01T00:00:00"},
                    {"callback_url": "https://example.invalid"}):
        with pytest.raises(ValidationError):
            BenchmarkRunRegistration(**(values | changes))


def test_dataset_hash_ignores_json_formatting_but_not_question_or_history_order():
    from advisor_shared.benchmark import dataset_digest

    first = {"version": 1, "cases": ["a", "b"], "seeded_history": ["x", "y"]}
    reordered_keys = {"seeded_history": ["x", "y"], "cases": ["a", "b"], "version": 1}
    assert dataset_digest(first) == dataset_digest(reordered_keys)
    assert dataset_digest(first) != dataset_digest(first | {"seeded_history": ["y", "x"]})


def test_history_setup_is_a_separate_bounded_stage_not_part_of_agent_duration():
    from advisor_shared.benchmark import BenchmarkReply

    body = dict(
        run_id="run-1", request_id="request-1", job_id="job-1",
        event="final", outcome="success",
        reply={"type": "message", "format": "markdown", "text": "A complete answer."})
    stages = [
        {"name": "history_setup", "duration_ms": 12.5, "outcome": "success"},
        {"name": "agent", "duration_ms": 80., "outcome": "success"},
    ]
    reply = BenchmarkReply(**body, telemetry={"stages": stages})
    assert [(stage.name, stage.duration_ms) for stage in reply.telemetry.stages] == [
        ("history_setup", 12.5), ("agent", 80.)]
    for invalid in (-1., float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            BenchmarkReply(**body, telemetry={"stages": [
                stages[0] | {"duration_ms": invalid}]})


def test_synthetic_wait_is_not_agent_or_model_dependency_telemetry():
    from advisor_shared.benchmark import SafeTelemetry

    telemetry = SafeTelemetry(stages=[
        {"name": "synthetic", "duration_ms": 1000., "outcome": "success"}])
    assert telemetry.stages[0].name == "synthetic"
    assert telemetry.dependencies == []
    assert telemetry.model_rounds is None
    assert telemetry.input_tokens is None
    assert telemetry.output_tokens is None


def test_shared_dataset_validation_preserves_canonical_content_and_rejects_bad_fixtures():
    from advisor_shared.benchmark import dataset_digest, validate_dataset

    data = {
        "version": 1,
        "cases": [{"case_id": "kb-01", "question": "  Approved fixture question.  "}],
        "seeded_history": [{"role": "user" if index % 2 == 0 else "assistant",
                            "content": f"Approved fixture message {index}"} for index in range(20)],
    }
    before = dataset_digest(data)
    validated = validate_dataset(data)
    assert validated["cases"][0]["question"] == "  Approved fixture question.  "
    assert dataset_digest(validated) == before
    invalid_values = [
        data | {"version": True},
        data | {"cases": []},
        data | {"cases": data["cases"] * 2},
        data | {"cases": [{"case_id": "invalid id", "question": "approved"}]},
        data | {"cases": [{"case_id": "kb-01", "question": " "}]},
        data | {"seeded_history": data["seeded_history"][:-1]},
        data | {"reply": "private-input"},
    ]
    wrong_role = deepcopy(data)
    wrong_role["seeded_history"][0]["role"] = "system"
    invalid_values.append(wrong_role)
    for invalid in invalid_values:
        with pytest.raises(ValueError) as error:
            validate_dataset(invalid)
        assert "private-input" not in str(error.value)


def test_shared_loader_validates_the_approved_repository_dataset():
    from advisor_shared.benchmark import dataset_digest, load_dataset

    path = Path(__file__).resolve().parents[2] / "deploy" / "bench" / "cases.json"
    data = load_dataset(path)
    assert len(data["seeded_history"]) == 20
    assert load_dataset(path, expected_digest=dataset_digest(data)) == data


def test_shared_bytes_parser_enforces_exact_size_limit_and_duplicate_field_rejection():
    from advisor_shared.benchmark import MAX_DATASET_BYTES, parse_dataset

    path = Path(__file__).resolve().parents[2] / "deploy" / "bench" / "cases.json"
    raw = path.read_bytes()
    expected = json.loads(raw)
    assert parse_dataset(raw) == expected
    padded = raw + b" " * (MAX_DATASET_BYTES - len(raw))
    assert parse_dataset(padded) == expected
    invalid_values = [
        padded + b" ",
        raw.decode("utf-8"),
        b"\xff",
        b'{"version":1,"version":1,"cases":[],"seeded_history":[]}',
        b"[" * 1500 + b"]" * 1500,
    ]
    for invalid in invalid_values:
        with pytest.raises(ValueError):
            parse_dataset(invalid)
    for question in (" ", "private-question-" * 2200):
        data = deepcopy(expected)
        data["cases"][0]["question"] = question
        with pytest.raises(ValueError) as error:
            parse_dataset(json.dumps(data).encode())
        assert "private-question-" not in str(error.value)


def test_shared_bytes_parser_reuses_case_and_seed_schema_limits():
    from advisor_shared.benchmark import parse_dataset

    path = Path(__file__).resolve().parents[2] / "deploy" / "bench" / "cases.json"
    data = json.loads(path.read_bytes())
    data["cases"][0]["question"] = "q" * 32000
    assert len(parse_dataset(json.dumps(data).encode())["cases"][0]["question"]) == 32000
    bulk = data | {"cases": [{"case_id": f"case-{index}", "question": "Approved question"}
                            for index in range(1000)]}
    assert len(parse_dataset(json.dumps(bulk).encode())["cases"]) == 1000
    bulk["cases"].append({"case_id": "case-1000", "question": "Approved question"})
    with pytest.raises(ValueError):
        parse_dataset(json.dumps(bulk).encode())
    for change in (
        {"version": True}, {"cases": data["cases"] * 2},
        {"seeded_history": data["seeded_history"][:-1]},
        {"extra": "private-input"},
    ):
        with pytest.raises(ValueError) as error:
            parse_dataset(json.dumps(data | change).encode())
        assert "private-input" not in str(error.value)
