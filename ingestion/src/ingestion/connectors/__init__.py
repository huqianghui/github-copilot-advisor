from ingestion.config import SourceConfig
from ingestion.connectors.base import Connector, RawQA
from ingestion.connectors.github_discussions import GitHubDiscussionsConnector
from ingestion.connectors.github_issues import GitHubIssuesConnector
from ingestion.connectors.manual_qa import ManualQAConnector

_REGISTRY: dict[str, type[Connector]] = {
    "manual_qa": ManualQAConnector,
    "github_issues": GitHubIssuesConnector,
    "github_discussions": GitHubDiscussionsConnector,
}


def create(config: SourceConfig) -> Connector:
    try:
        cls = _REGISTRY[config.type]
    except KeyError:
        raise ValueError(f"no connector for source type '{config.type}'") from None
    return cls(config)


__all__ = ["Connector", "RawQA", "ManualQAConnector", "GitHubIssuesConnector",
           "GitHubDiscussionsConnector", "create"]
