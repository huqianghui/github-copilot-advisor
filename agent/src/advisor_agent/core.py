"""AdvisorCore:planner → tool loop → evaluator 管线,事件与兜底(spec 7.1/10.1/10.2)。"""
import logging
from typing import Callable

from advisor_agent.backend import AgentBackend
from advisor_agent.extensions import (
    AnswerEvaluator,
    NoopEvaluator,
    NoopPlanner,
    QueryPlanner,
)
from advisor_agent.run_context import new_run
from advisor_agent.sessions import SessionStore
from advisor_shared.events import AdvisorEvent
from advisor_shared.messages import AdvisorRequest, AdvisorResponse, Citation

logger = logging.getLogger(__name__)

FALLBACK_MESSAGE = ("抱歉,我这边暂时出了点问题,请稍后重试。"
                    "如果持续失败,请联系群里的支持人员。")

# 1 = 不重试。重试全部下沉到 openai SDK(区分可重试状态码、遵守 Retry-After、
# 指数退避);这一层的 `except Exception` 是盲重试,而且重跑的是整个 tool loop
# —— search_solutions / web_search 会被重复执行,既贵又有重复副作用(重复的
# GitHub API 调用、重复的 web search 计费)。两层叠乘还会把最坏等待推到
# 6 次连接 × 15s,对 Teams 用户不可接受。
_MAX_ATTEMPTS = 1
_MAX_CITATIONS = 5
_SUMMARY_CHARS = 80


def _log_event(event: AdvisorEvent) -> None:
    logger.info("advisor_event %s", event.to_log_line())


class AdvisorCore:
    def __init__(self, backend: AgentBackend, sessions: SessionStore,
                 planner: QueryPlanner | None = None,
                 evaluator: AnswerEvaluator | None = None,
                 event_sink: Callable[[AdvisorEvent], None] = _log_event,
                 channel_name: str = "unknown"):
        self.backend = backend
        self.sessions = sessions
        self.planner = planner or NoopPlanner()
        self.evaluator = evaluator or NoopEvaluator()
        self.event_sink = event_sink
        self.channel_name = channel_name

    async def handle(self, request: AdvisorRequest) -> AdvisorResponse:
        request = await self.planner.plan(request)
        run = new_run()
        history = await self.sessions.get(request.conversation_key)
        # 必须在 planner 之后求值:planner 会换掉 request 对象。
        # 历史与事件共用 —— 纯图片消息的 question_summary 不能是空串。
        marked_text = (f"[图片×{len(request.images)}] {request.text}".strip()
                       if request.images else request.text)

        answer, error = None, None
        for _ in range(_MAX_ATTEMPTS):
            try:
                answer = await self.backend.run(
                    request.text, history, request.images or None)
                break
            except Exception as e:
                logger.exception("backend attempt failed")
                error = str(e)

        if answer is None:
            response = AdvisorResponse(markdown=FALLBACK_MESSAGE)
        else:
            seen, citations = set(), []
            for c in run.citations_seen:
                if c["url"] in seen:
                    continue
                seen.add(c["url"])
                citations.append(Citation(title=c["title"], url=c["url"]))
            response = AdvisorResponse(
                markdown=answer,
                citations=citations[:_MAX_CITATIONS],
                mentions=list(run.mentions),
            )
            response = await self.evaluator.evaluate(request, response)
            await self.sessions.append(
                request.conversation_key, "user", marked_text)
            await self.sessions.append(
                request.conversation_key, "assistant", response.markdown)

        self.event_sink(AdvisorEvent(
            conversation_key=request.conversation_key,
            channel=self.channel_name,
            question_summary=marked_text[:_SUMMARY_CHARS],
            stage=run.stage,
            tool_latencies_ms=run.tool_latencies_ms,
            failover_count=run.failover_count,
            mentioned_human=bool(run.mentions),
            image_count=len(request.images),
            error=error if answer is None else None,
            search_attempts=run.search_attempts,
        ))
        return response
