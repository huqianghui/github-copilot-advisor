import asyncio

import pytest

from advisor_shared.telemetry import current_trace, step, trace_scope


def test_linked_traces_have_independent_segments_and_clock_origins():
    with trace_scope(new=True, trace_id="a" * 32) as first:
        with step("first"):
            pass
    with trace_scope(new=True, trace_id=first.trace_id) as second:
        assert second.trace_id == first.trace_id
        assert second.segment_id != first.segment_id
        assert second.boot_id == first.boot_id
        assert second.started_at >= first.started_at
        assert second.timings == []
        with step("second"):
            pass
    timing = second.timings[0]
    assert timing.segment_id == second.segment_id
    assert timing.boot_id == second.boot_id
    assert timing.parent_span_id is None
    assert timing.start_offset_ms >= 0


@pytest.mark.parametrize("value", ["", "a\nsecret", "https://private.invalid", "a" * 129, 3])
def test_invalid_trace_identifier_is_rejected(value):
    with pytest.raises(ValueError):
        with trace_scope(new=True, trace_id=value):
            pass


async def test_concurrent_linked_traces_never_share_timings():
    async def turn(name):
        with trace_scope(new=True, trace_id=name) as trace:
            await asyncio.sleep(0)
            with step(name):
                assert current_trace() is trace
            return trace

    a, b = await asyncio.gather(turn("trace-a"), turn("trace-b"))
    assert [t.name for t in a.timings] == ["trace-a"]
    assert [t.name for t in b.timings] == ["trace-b"]
    assert a.segment_id != b.segment_id


def test_explicit_different_id_starts_new_clock_even_without_new_flag():
    with trace_scope(trace_id="outer") as outer:
        with trace_scope(trace_id="inner") as inner:
            assert inner.trace_id == "inner"
            assert inner.segment_id != outer.segment_id
        assert current_trace() is outer
