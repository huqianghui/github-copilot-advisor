"""单次问答的运行上下文:工具上报副作用,核心管线读取。
用 contextvars 而不是解析 LLM 自由文本(spec 7.1、10.2)。"""
from contextvars import ContextVar
from dataclasses import dataclass, field

from advisor_shared.messages import MentionDirective


@dataclass
class RunContext:
    stage: str = "generic_advice"
    mentions: list[MentionDirective] = field(default_factory=list)
    citations_seen: list[dict] = field(default_factory=list)
    tool_latencies_ms: dict[str, int] = field(default_factory=dict)
    failover_count: int = 0


current_run: ContextVar[RunContext] = ContextVar("current_run")


def new_run() -> RunContext:
    run = RunContext()
    current_run.set(run)
    return run
