"""Connector 统一接口:每种数据源类型一个实现(spec 6.1)。"""
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime

from pydantic import BaseModel

from ingestion.config import SourceConfig


class RawQA(BaseModel):
    """connector 输出、LLM 提炼前的中间形态。"""
    native_id: str
    title: str
    question: str
    answer: str
    url: str
    labels: list[str] = []
    doc_type: str
    created_at: datetime
    resolved_at: datetime | None = None


class Connector(ABC):
    def __init__(self, config: SourceConfig):
        self.config = config

    @abstractmethod
    def fetch(self, since: datetime | None) -> AsyncIterator[RawQA]:
        """按时间水位增量产出 RawQA。"""
