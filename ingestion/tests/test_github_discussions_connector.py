import httpx
import pytest
import respx

from ingestion.config import SourceConfig
from ingestion.connectors import create
from ingestion.connectors.github_discussions import GitHubDiscussionsConnector

API = "https://api.github.com"


def make_config(**over) -> SourceConfig:
    base = dict(
        name="copilot-cli-discussions", type="github_discussions",
        repo="github/copilot-cli", product_area="cli",
        filters={"answered": True},
    )
    base.update(over)
    return SourceConfig(**base)


def discussion_node(number=7, answered=True) -> dict:
    return {
        "number": number,
        "title": "How to configure MCP servers?",
        "body": "Where does copilot CLI read MCP config from?",
        "url": f"https://github.com/github/copilot-cli/discussions/{number}",
        "createdAt": "2026-02-01T00:00:00Z",
        "answerChosenAt": "2026-02-02T00:00:00Z" if answered else None,
        "answer": {"body": "Put mcp.json under ~/.copilot/ and restart."}
                  if answered else None,
        "labels": {"nodes": [{"name": "question"}]},
    }


def gql_response(nodes, has_next=False):
    return {
        "data": {"repository": {"discussions": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": has_next, "endCursor": "c1"},
        }}}
    }


@respx.mock
async def test_fetch_answered_discussions_only():
    respx.post(f"{API}/graphql").mock(
        return_value=httpx.Response(200, json=gql_response([
            discussion_node(7, answered=True),
            discussion_node(8, answered=False),
        ]))
    )
    connector = create(make_config())
    assert isinstance(connector, GitHubDiscussionsConnector)
    items = [qa async for qa in connector.fetch(None)]
    assert len(items) == 1
    qa = items[0]
    assert qa.native_id == "7"
    assert qa.doc_type == "discussion"
    assert "mcp.json" in qa.answer
    assert qa.labels == ["question"]


@respx.mock
async def test_graphql_error_raises():
    respx.post(f"{API}/graphql").mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "bad"}]})
    )
    with pytest.raises(RuntimeError, match="bad"):
        _ = [qa async for qa in create(make_config()).fetch(None)]
