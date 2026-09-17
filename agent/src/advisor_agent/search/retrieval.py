"""Outcomes of a bounded logical Web retrieval, distinct from provider attempts."""
from dataclasses import dataclass, field
from typing import Literal

from advisor_agent.search.models import SearchResult
from advisor_agent.search.source_policy import WebSearchScope
from advisor_shared.events import SearchAttempt

ScopeStatus = Literal["success", "empty", "timeout", "error", "not_configured"]
RetrievalStatus = Literal[
    "success", "partial", "empty", "timeout", "error", "not_configured"]


@dataclass
class WebScopeResult:
    scope: WebSearchScope
    status: ScopeStatus = "empty"
    results: list[SearchResult] = field(default_factory=list)
    attempts: list[SearchAttempt] = field(default_factory=list)
    failovers: int = 0


@dataclass
class WebRetrievalResult:
    status: RetrievalStatus
    results: list[SearchResult] = field(default_factory=list)
    scopes: list[WebScopeResult] = field(default_factory=list)

    @property
    def failovers(self) -> int:
        return sum(scope.failovers for scope in self.scopes)
