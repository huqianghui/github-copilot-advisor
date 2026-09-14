"""每次问答一条结构化事件 — 可观测性契约(spec 10.2)。"""
from typing import Literal

from pydantic import BaseModel, Field

Stage = Literal["kb_hit", "live_hit", "web", "generic_advice", "escalated"]
SearchSource = Literal["kb", "github-live", "web"]


class SearchAttempt(BaseModel):
    source: SearchSource
    provider: str | None
    status: Literal[
        "success", "empty", "timeout", "error", "not_configured", "cancelled"
    ] = "empty"
    result_count: int | None = Field(default=None, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    timeout_seconds: float | None = None
    error_type: str | None = None
    http_status: int | None = None
    error_code: str | None = None


class AdvisorEvent(BaseModel):
    conversation_key: str
    channel: str
    question_summary: str
    stage: Stage
    tool_latencies_ms: dict[str, int] = {}
    failover_count: int = 0
    mentioned_human: bool = False
    image_count: int = 0
    error: str | None = None
    search_attempts: list[SearchAttempt] = Field(default_factory=list)

    def to_log_line(self) -> str:
        return self.model_dump_json()
