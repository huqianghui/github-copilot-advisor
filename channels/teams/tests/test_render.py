# channels/teams/tests/test_render.py
from advisor_shared.messages import AdvisorResponse, Citation, MentionDirective
from teams_adapter.render import MAX_MARKDOWN_CHARS, render_reply


def test_plain_markdown_no_extras():
    activity = render_reply(AdvisorResponse(markdown="重启 VS Code 试试。"))
    assert activity["type"] == "message"
    assert activity["text"] == "重启 VS Code 试试。"
    assert activity["entities"] == []


def test_citations_appended_as_reference_list():
    resp = AdvisorResponse(
        markdown="见参考。",
        citations=[Citation(title="FAQ", url="https://g/faq"),
                   Citation(title="Issue 42", url="https://g/42")])
    text = render_reply(resp)["text"]
    assert "**参考:**" in text
    assert "- [FAQ](https://g/faq)" in text
    assert "- [Issue 42](https://g/42)" in text


def test_mentions_prepended_with_entities():
    resp = AdvisorResponse(
        markdown="请跟进。",
        mentions=[MentionDirective(name="李四", platform_user_id="29:1",
                                   role="CSAM")])
    activity = render_reply(resp)
    assert activity["text"].startswith("<at>李四</at>")
    assert activity["entities"] == [{
        "type": "mention", "text": "<at>李四</at>",
        "mentioned": {"id": "29:1", "name": "李四"},
    }]


def test_overlong_markdown_truncated():
    resp = AdvisorResponse(markdown="x" * 10000)
    text = render_reply(resp)["text"]
    assert len(text) <= MAX_MARKDOWN_CHARS + 50
    assert "截断" in text
