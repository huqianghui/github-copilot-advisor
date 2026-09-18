import pytest


def test_health_and_ack_do_not_prove_bot_success():
    from advisor_deploy.smoke import evaluate_smoke

    result = evaluate_smoke({
        "health": "passed", "ingress_ack": "passed",
        "embedding": "failed", "complete_answer": "not_run",
    })
    assert not result.passed
    assert result.failed_checks == ("embedding",)
    assert result.checks["complete_answer"] == "not_run"
    assert result.checks["history_recovery"] == "not_run"


def test_cloud_functionality_does_not_implicitly_pass_teams_client_acceptance():
    from advisor_deploy.smoke import evaluate_smoke

    result = evaluate_smoke({
        "health": "passed", "ingress_ack": "passed",
        "embedding": "passed", "model": "passed", "search": "passed",
        "blob_queue": "passed", "bot_auth": "passed", "benchmark_auth": "passed",
        "complete_answer": "passed", "history_recovery": "passed",
    })
    assert result.automated_passed
    assert not result.passed
    assert result.checks["teams_client"] == "not_run"


@pytest.mark.parametrize("status", ["not_run", "blocked", "unauthorized", "failed"])
def test_every_incomplete_or_unsuccessful_required_check_prevents_passing(status):
    from advisor_deploy.smoke import CHECKS, evaluate_smoke

    for missing in CHECKS:
        evidence = {name: "passed" for name in CHECKS}
        evidence[missing] = status
        result = evaluate_smoke(evidence)
        assert not result.passed
        assert result.checks[missing] == status
        assert result.failed_checks == ((missing,) if status == "failed" else ())


def test_only_complete_evidence_passes_and_result_is_not_mutated_by_caller():
    from advisor_deploy.smoke import CHECKS, evaluate_smoke

    evidence = {name: "passed" for name in CHECKS}
    result = evaluate_smoke(evidence)
    evidence["embedding"] = "failed"
    assert result.passed
    assert result.automated_passed
    assert result.failed_checks == ()
    with pytest.raises(TypeError):
        result.checks["embedding"] = "failed"


@pytest.mark.parametrize("evidence", [
    {"unknown_probe": "passed"}, {"embedding": True}, {"embedding": "success"},
    {"embedding": "secret-value"}, [],
])
def test_evaluator_rejects_ambiguous_or_body_bearing_evidence_without_echo(evidence):
    from advisor_deploy.smoke import evaluate_smoke

    with pytest.raises(ValueError) as error:
        evaluate_smoke(evidence)
    assert "secret-value" not in str(error.value)
