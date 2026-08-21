"""每次问答一条结构化事件 — 可观测性契约(spec 10.2)。"""
from typing import Literal

from pydantic import BaseModel

Stage = Literal["kb_hit", "live_hit", "web", "generic_advice", "escalated"]


class AdvisorEvent(BaseModel):
    conversation_key: str
    channel: str
    question_summary: str
    stage: Stage
    tool_latencies_ms: dict[str, int] = {}
    failover_count: int = 0
    mentioned_human: bool = False
    error: str | None = None

    def to_log_line(self) -> str:
        return self.model_dump_json()
