from ingestion.config import SourceConfig
from ingestion.connectors.base import Connector, RawQA
from ingestion.connectors.manual_qa import ManualQAConnector

_REGISTRY: dict[str, type[Connector]] = {
    "manual_qa": ManualQAConnector,
}


def create(config: SourceConfig) -> Connector:
    try:
        cls = _REGISTRY[config.type]
    except KeyError:
        raise ValueError(f"no connector for source type '{config.type}'") from None
    return cls(config)


__all__ = ["Connector", "RawQA", "ManualQAConnector", "create"]
