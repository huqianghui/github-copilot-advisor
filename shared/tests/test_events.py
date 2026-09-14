import json

from advisor_shared.events import AdvisorEvent


def test_event_minimal_and_log_line_is_json():
    e = AdvisorEvent(
        conversation_key="19:a;messageid=1", channel="teams",
        question_summary="登录失败", stage="kb_hit",
    )
    line = e.to_log_line()
    parsed = json.loads(line)
    assert parsed["stage"] == "kb_hit"
    assert parsed["failover_count"] == 0
    assert "\n" not in line


def test_event_rejects_unknown_stage():
    import pytest
    with pytest.raises(ValueError):
        AdvisorEvent(conversation_key="k", channel="teams",
                     question_summary="q", stage="nope")


def test_event_image_count_defaults_to_zero_and_serializes():
    event = AdvisorEvent(conversation_key="c", channel="teams",
                         question_summary="q", stage="kb_hit")
    assert event.image_count == 0
    assert '"image_count":0' in event.to_log_line()


def test_old_events_default_to_no_search_attempts():
    event = AdvisorEvent.model_validate({
        "conversation_key": "c", "channel": "teams",
        "question_summary": "q", "stage": "web",
    })
    assert event.model_dump().get("search_attempts") == []


def test_search_attempts_survive_event_json_roundtrip():
    event = AdvisorEvent.model_validate({
        "conversation_key": "c", "channel": "teams",
        "question_summary": "q", "stage": "web",
        "search_attempts": [{
            "source": "web", "provider": "brave", "status": "success",
            "result_count": 3, "duration_ms": 42, "timeout_seconds": 6,
        }],
    })
    recorded = json.loads(event.to_log_line()).get("search_attempts", [])
    assert len(recorded) == 1
    assert recorded[0]["provider"] == "brave"
    assert recorded[0]["result_count"] == 3
