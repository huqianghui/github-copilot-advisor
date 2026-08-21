from advisor_shared.messages import (
    AdvisorRequest,
    AdvisorResponse,
    Citation,
    MentionDirective,
)


def test_request_roundtrip():
    req = AdvisorRequest(
        text="Copilot 登录不上", conversation_key="19:abc;messageid=5",
        channel_id="19:abc@thread.tacv2", user_id="29:u1",
        user_name="张三", is_group=True,
    )
    assert req.is_group is True
    assert AdvisorRequest(**req.model_dump()) == req


def test_response_defaults_empty_lists():
    resp = AdvisorResponse(markdown="试试重启 VS Code。")
    assert resp.citations == [] and resp.mentions == []


def test_response_with_citation_and_mention():
    resp = AdvisorResponse(
        markdown="见链接",
        citations=[Citation(title="FAQ", url="https://github.com/x")],
        mentions=[MentionDirective(name="李四", platform_user_id="29:csam",
                                   role="CSAM")],
    )
    assert resp.citations[0].url == "https://github.com/x"
    assert resp.mentions[0].role == "CSAM"
