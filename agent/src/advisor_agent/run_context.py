"""单次问答的运行上下文:工具上报副作用,核心管线读取。
用 contextvars 而不是解析 LLM 自由文本(spec 7.1、10.2)。"""
from contextvars import ContextVar
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from advisor_shared.events import SearchAttempt
from advisor_shared.messages import MentionDirective


@dataclass
class RunContext:
    stage: str = "generic_advice"
    mentions: list[MentionDirective] = field(default_factory=list)
    citations_seen: list[dict] = field(default_factory=list)
    tool_latencies_ms: dict[str, int] = field(default_factory=dict)
    failover_count: int = 0
    search_attempts: list[SearchAttempt] = field(default_factory=list)


current_run: ContextVar[RunContext] = ContextVar("current_run")


@dataclass(frozen=True)
class RequestContext:
    channel_id: str
    is_group: bool


_current_request: ContextVar[RequestContext] = ContextVar("current_request")


def get_request_context() -> RequestContext:
    try:
        return _current_request.get()
    except LookupError:
        raise RuntimeError("request context is not bound") from None


@contextmanager
def request_scope(channel_id: str, is_group: bool) -> Iterator[RunContext]:
    request_token = _current_request.set(RequestContext(channel_id, is_group))
    run = RunContext()
    run_token = current_run.set(run)
    try:
        yield run
    finally:
        current_run.reset(run_token)
        _current_request.reset(request_token)


def new_run() -> RunContext:
    """仅供独立工具测试使用；生产回合请通过 request_scope() 绑定。

    new_run() 不会恢复先前的 current_run token，也不应在 request_scope()
    管理的 scoped turn 内再次调用。
    """
    run = RunContext()
    current_run.set(run)
    return run
