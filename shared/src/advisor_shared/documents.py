# shared/src/advisor_shared/documents.py
"""QA 文档模型 — ingestion 写入与 agent 检索共同遵守的契约。"""
import hashlib
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

RAW_CONTENT_MAX_CHARS = 32_000


class QADocument(BaseModel):
    id: str
    title: str
    content: str
    keywords: list[str]
    raw_content: str
    url: str
    source: str
    doc_type: Literal["issue", "discussion", "manual_qa"]
    product_area: str
    created_at: datetime
    resolved_at: datetime | None
    content_vector: list[float] | None = None

    @staticmethod
    def make_id(source: str, native_id: str) -> str:
        return hashlib.sha256(f"{source}:{native_id}".encode()).hexdigest()[:32]

    def embedding_text(self) -> str:
        return f"{self.title}\n{self.content}"

    def to_search_document(self) -> dict:
        d = self.model_dump()
        d["created_at"] = self.created_at.isoformat()
        d["resolved_at"] = self.resolved_at.isoformat() if self.resolved_at else None
        if self.content_vector is None:
            d.pop("content_vector")
        return d
