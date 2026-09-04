from advisor_shared.messages import (
    AdvisorRequest,
    AdvisorResponse,
    Citation,
    ImageInput,
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


def _req(**kw):
    base = dict(text="hi", conversation_key="c", channel_id="ch",
                user_id="u", user_name="n", is_group=False)
    base.update(kw)
    return AdvisorRequest(**base)


def test_request_defaults_to_no_images():
    assert _req().images == []


def test_request_carries_images():
    req = _req(text="", images=[ImageInput(data=b"\x89PNG", mime_type="image/png")])
    assert req.images[0].data == b"\x89PNG"
    assert req.images[0].mime_type == "image/png"
    assert req.images[0].name == ""       # Teams inline image 无文件名
