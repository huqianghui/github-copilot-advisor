import asyncio
from dataclasses import FrozenInstanceError

import pytest

from advisor_agent.run_context import (
    current_run,
    get_request_context,
    new_run,
    request_scope,
)


def test_new_run_resets_context():
    run = new_run()
    run.stage = "kb_hit"
    run.failover_count = 2
    fresh = new_run()
    assert fresh.stage == "generic_advice"
    assert fresh.failover_count == 0
    assert current_run.get() is fresh


def test_tools_report_via_contextvar():
    run = new_run()
    current_run.get().tool_latencies_ms["search_solutions"] = 812
    assert run.tool_latencies_ms == {"search_solutions": 812}


def test_request_context_requires_binding():
    with pytest.raises(RuntimeError, match="request context is not bound"):
        get_request_context()


def test_request_context_is_immutable():
    with request_scope("outer", False):
        with pytest.raises(FrozenInstanceError):
            setattr(get_request_context(), "is_group", True)


def test_nested_scopes_restore_request_and_run():
    with request_scope("outer", False) as outer:
        outer.stage = "kb_hit"
        with request_scope("inner", True) as inner:
            assert get_request_context().channel_id == "inner"
            assert get_request_context().is_group is True
            assert current_run.get() is inner
            assert inner is not outer
        assert get_request_context().channel_id == "outer"
        assert get_request_context().is_group is False
        assert current_run.get() is outer
        assert outer.stage == "kb_hit"
    with pytest.raises(RuntimeError):
        get_request_context()


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_scope_restores_both_contexts_after_failure(failure):
    with request_scope("outer", False) as outer:
        with pytest.raises(failure):
            with request_scope("inner", True):
                raise failure()
        assert get_request_context().channel_id == "outer"
        assert current_run.get() is outer
