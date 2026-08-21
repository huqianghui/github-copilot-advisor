"""GitHub discussions connector:GraphQL(answered 过滤仅 GraphQL 支持)。"""
from collections.abc import AsyncIterator
from datetime import datetime

from ingestion.connectors.base import Connector, RawQA
from ingestion.github_client import GitHubClient

_DISCUSSIONS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    discussions(first: 50, after: $cursor,
                orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes {
        number title body url createdAt answerChosenAt
        answer { body }
        labels(first: 10) { nodes { name } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

_ORG_REPOS_QUERY = """
query($org: String!, $cursor: String) {
  organization(login: $org) {
    repositories(first: 50, after: $cursor) {
      nodes { nameWithOwner hasDiscussionsEnabled }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


class GitHubDiscussionsConnector(Connector):
    def __init__(self, config, client: GitHubClient | None = None):
        super().__init__(config)
        self.client = client or GitHubClient()

    async def fetch(self, since: datetime | None) -> AsyncIterator[RawQA]:
        for repo in await self._target_repos():
            async for qa in self._fetch_repo(repo, since):
                yield qa

    async def _target_repos(self) -> list[str]:
        if self.config.repo:
            return [self.config.repo]
        repos, cursor = [], None
        while True:
            data = await self.client.graphql(
                _ORG_REPOS_QUERY, {"org": self.config.repo_org, "cursor": cursor})
            page = data["organization"]["repositories"]
            repos += [r["nameWithOwner"] for r in page["nodes"]
                      if r["hasDiscussionsEnabled"]]
            if not page["pageInfo"]["hasNextPage"]:
                return repos
            cursor = page["pageInfo"]["endCursor"]

    async def _fetch_repo(self, repo: str,
                          since: datetime | None) -> AsyncIterator[RawQA]:
        owner, name = repo.split("/")
        cursor = None
        while True:
            data = await self.client.graphql(
                _DISCUSSIONS_QUERY,
                {"owner": owner, "name": name, "cursor": cursor})
            page = data["repository"]["discussions"]
            for node in page["nodes"]:
                created = _dt(node["createdAt"])
                if since and created <= since:
                    return  # DESC 排序,更早的都可跳过
                if self.config.filters.answered and not node.get("answer"):
                    continue
                yield RawQA(
                    native_id=str(node["number"]),
                    title=node["title"],
                    question=node.get("body") or "",
                    answer=(node.get("answer") or {}).get("body", ""),
                    url=node["url"],
                    labels=[l["name"] for l in node["labels"]["nodes"]],
                    doc_type="discussion",
                    created_at=created,
                    resolved_at=_dt(node.get("answerChosenAt")),
                )
            if not page["pageInfo"]["hasNextPage"]:
                return
            cursor = page["pageInfo"]["endCursor"]
