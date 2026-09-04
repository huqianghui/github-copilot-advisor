import pytest
from pydantic import ValidationError

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


def test_rejects_non_image_mime_type():
    for bad in ("text/html", "../evil"):
        with pytest.raises(ValidationError):
            ImageInput(data=b"x", mime_type=bad)


def test_accepts_common_image_mime_types():
    for good in ("image/png", "image/jpeg", "image/svg+xml"):
        assert ImageInput(data=b"x", mime_type=good).mime_type == good


def test_image_bytes_hidden_from_repr_but_otherwise_unchanged():
    img = ImageInput(data=b"\x89PNG-secret-token", mime_type="image/png")
    assert "secret-token" not in repr(img)
    assert "secret-token" not in repr(_req(images=[img]))
    # repr 之外行为照旧:取值、相等、model_dump 都仍是原始字节
    assert img.data == b"\x89PNG-secret-token"
    assert img == ImageInput(data=b"\x89PNG-secret-token", mime_type="image/png")
    assert img.model_dump()["data"] == b"\x89PNG-secret-token"
