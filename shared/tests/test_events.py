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
