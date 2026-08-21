"""GitHub issues connector:REST 拉取 + 答案提取(spec 6.1)。"""
from collections.abc import AsyncIterator
from datetime import datetime

from ingestion.connectors.base import Connector, RawQA
from ingestion.github_client import GitHubClient

_MAINTAINER = {"OWNER", "MEMBER", "COLLABORATOR"}
_MIN_ANSWER_CHARS = 40


def _substantive(c: dict) -> bool:
    return (c.get("user") or {}).get("type") != "Bot" and \
        len(c.get("body") or "") > _MIN_ANSWER_CHARS


def extract_answer(issue: dict, comments: list[dict]) -> str | None:
    candidates = [c for c in comments if _substantive(c)]
    maintainer = [c for c in candidates
                  if c.get("author_association") in _MAINTAINER]
    if maintainer:
        return maintainer[-1]["body"]
    by_plus_one = [c for c in candidates
                   if (c.get("reactions") or {}).get("+1", 0) > 0]
    if by_plus_one:
        return max(by_plus_one,
                   key=lambda c: c["reactions"]["+1"])["body"]
    return None


class GitHubIssuesConnector(Connector):
    def __init__(self, config, client: GitHubClient | None = None):
        super().__init__(config)
        self.client = client or GitHubClient()

    async def fetch(self, since: datetime | None) -> AsyncIterator[RawQA]:
        f = self.config.filters
        params: dict = {"state": f.state or "closed"}
        if f.labels:
            params["labels"] = ",".join(f.labels)
        if since:
            params["since"] = since.isoformat()
        async for issue in self.client.paged_get(
                f"/repos/{self.config.repo}/issues", params):
            if "pull_request" in issue:
                continue
            comments = [c async for c in self.client.paged_get(
                f"/repos/{self.config.repo}/issues/{issue['number']}/comments", {})]
            answer = extract_answer(issue, comments)
            if not answer:
                continue
            yield RawQA(
                native_id=str(issue["number"]),
                title=issue["title"],
                question=issue.get("body") or "",
                answer=answer,
                url=issue["html_url"],
                labels=[l["name"] for l in issue.get("labels", [])],
                doc_type="issue",
                created_at=datetime.fromisoformat(
                    issue["created_at"].replace("Z", "+00:00")),
                resolved_at=datetime.fromisoformat(
                    issue["closed_at"].replace("Z", "+00:00"))
                if issue.get("closed_at") else None,
            )
