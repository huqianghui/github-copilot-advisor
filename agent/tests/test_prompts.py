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


def test_prompt_uses_current_billing_concept():
    """规则 8 讲的必须是 AI credits,不是已退役的 premium requests。"""
    assert "AI credits" in SYSTEM_PROMPT
    assert "credits_usage" in SYSTEM_PROMPT


def test_prompt_maps_the_retired_term_instead_of_dropping_it():
    """旧词必须留在 prompt 里 —— 但只能以"映射到 AI credits"的身份出现。

    这是字符串绊线,不是语义守卫:它挡得住"把旧词整段删掉"(那样模型遇到
    premium requests 提问会当成陌生概念),也挡得住"仍把旧词当现行概念描述"
    (靠 已退役/取代 这两个词)。挡不住措辞正确但含义写反的改写 ——
    真正验证这条规则的是 eval_cases.yaml 的 usage-credits-legacy-term。
    """
    assert "premium request" in SYSTEM_PROMPT
    assert "退役" in SYSTEM_PROMPT
    assert "取代" in SYSTEM_PROMPT


def test_prompt_has_image_handling_rule():
    assert "复述" in SYSTEM_PROMPT
    assert "search_solutions 的 query 主体" in SYSTEM_PROMPT


def test_prompt_forbids_echoing_secrets_from_images():
    for word in ("token", "API key", "cookie"):
        assert word in SYSTEM_PROMPT
    assert "已略过" in SYSTEM_PROMPT
