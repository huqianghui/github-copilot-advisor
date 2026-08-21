"""检索结果统一形态:三路(kb/github-live/web)共用。"""
from typing import Literal

from pydantic import BaseModel

MIN_RERANKER_SCORE = 1.5


class SearchResult(BaseModel):
    title: str
    content: str
    url: str
    origin: Literal["kb", "github-live", "web"]
    score: float
