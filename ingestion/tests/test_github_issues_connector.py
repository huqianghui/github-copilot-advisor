import httpx
import pytest
import respx

from ingestion.config import SourceConfig
from ingestion.connectors import create
from ingestion.connectors.github_issues import (
    GitHubIssuesConnector,
    extract_answer,
)

API = "https://api.github.com"


def make_config() -> SourceConfig:
    return SourceConfig(
        name="vscode-copilot-issues", type="github_issues",
        repo="microsoft/vscode", product_area="vscode",
        filters={"labels": ["github-copilot"], "state": "closed"},
    )


def issue_payload(number=1, title="Copilot hangs", body="It hangs.") -> dict:
    return {
        "number": number, "title": title, "body": body,
        "html_url": f"https://github.com/microsoft/vscode/issues/{number}",
        "labels": [{"name": "github-copilot"}],
        "created_at": "2026-01-01T00:00:00Z",
        "closed_at": "2026-01-05T00:00:00Z",
        "user": {"login": "someuser", "type": "User"},
    }


def comment(body, assoc="NONE", plus_one=0, user_type="User") -> dict:
    return {
        "body": body, "author_association": assoc,
        "user": {"login": "u", "type": user_type},
        "reactions": {"+1": plus_one},
    }


def test_extract_answer_prefers_last_member_comment():
    comments = [
        comment("try restarting, that fixed it for me " * 2, "NONE", plus_one=10),
        comment("Fixed in v1.97 — upgrade the extension please.", "MEMBER"),
    ]
    assert "v1.97" in extract_answer(issue_payload(), comments)


def test_extract_answer_falls_back_to_most_plus_one():
    comments = [
        comment("me too", "NONE", plus_one=1),
        comment("Downgrading the proxy setting resolved this for our whole team.",
                "NONE", plus_one=7),
    ]
    assert "proxy" in extract_answer(issue_payload(), comments)


def test_extract_answer_ignores_bots_and_short_comments():
    comments = [
        comment("This issue is stale.", "NONE", user_type="Bot", plus_one=99),
        comment("+1", "MEMBER"),
    ]
    assert extract_answer(issue_payload(), comments) is None


@respx.mock
async def test_fetch_yields_rawqa_and_skips_pull_requests():
    respx.get(f"{API}/repos/microsoft/vscode/issues").mock(
        return_value=httpx.Response(200, json=[
            issue_payload(number=1),
            {**issue_payload(number=2), "pull_request": {"url": "x"}},  # PR 要跳过
        ])
    )
    respx.get(f"{API}/repos/microsoft/vscode/issues/1/comments").mock(
        return_value=httpx.Response(200, json=[
            comment("Fixed in v1.97 — upgrade the extension please.", "MEMBER"),
        ])
    )
    connector = create(make_config())
    assert isinstance(connector, GitHubIssuesConnector)
    items = [qa async for qa in connector.fetch(None)]
    assert len(items) == 1
    qa = items[0]
    assert qa.native_id == "1"
    assert qa.doc_type == "issue"
    assert "v1.97" in qa.answer
    assert qa.labels == ["github-copilot"]


@respx.mock
async def test_fetch_skips_issue_without_answer():
    respx.get(f"{API}/repos/microsoft/vscode/issues").mock(
        return_value=httpx.Response(200, json=[issue_payload(number=3)])
    )
    respx.get(f"{API}/repos/microsoft/vscode/issues/3/comments").mock(
        return_value=httpx.Response(200, json=[])
    )
    items = [qa async for qa in create(make_config()).fetch(None)]
    assert items == []
