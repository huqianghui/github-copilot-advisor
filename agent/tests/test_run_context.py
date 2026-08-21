from advisor_agent.run_context import current_run, new_run


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
