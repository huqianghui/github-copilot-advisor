from advisor_agent.prompts import SYSTEM_PROMPT


def test_prompt_contains_waterfall_rules():
    # spec 7.3 的五条核心规则都要落在 prompt 里
    assert "search_solutions" in SYSTEM_PROMPT      # 规则1:永远先组合检索
    assert "no_results" in SYSTEM_PROMPT            # 规则2:no_results 才 web_search
    assert "web_search" in SYSTEM_PROMPT
    assert "escalate_to_human" in SYSTEM_PROMPT     # 规则4:升级条件
    assert "支持工单" in SYSTEM_PROMPT or "工单" in SYSTEM_PROMPT  # 规则3
    assert "语言" in SYSTEM_PROMPT                   # 规则5:语言跟随
    assert "编造" in SYSTEM_PROMPT                   # 规则5:不编造


def test_prompt_mentions_source_priority():
    assert "kb" in SYSTEM_PROMPT and "github-live" in SYSTEM_PROMPT


def test_prompt_mentions_marketplace_rule():
    # 附加要求:版本/兼容性类问题走 web_search,查询词带 marketplace/plugin
    assert "marketplace" in SYSTEM_PROMPT


def test_prompt_mentions_network_diagnostics_rule():
    assert "network_diagnostics" in SYSTEM_PROMPT


def test_prompt_mentions_usage_lookup_rule():
    assert "copilot_usage_lookup" in SYSTEM_PROMPT
