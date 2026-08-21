"""多 agent 扩展点:v1 no-op,未来可替换为 MAF 子 agent(spec 7.1)。"""
from typing import Protocol

from advisor_shared.messages import AdvisorRequest, AdvisorResponse


class QueryPlanner(Protocol):
    async def plan(self, request: AdvisorRequest) -> AdvisorRequest: ...


class AnswerEvaluator(Protocol):
    async def evaluate(self, request: AdvisorRequest,
                       response: AdvisorResponse) -> AdvisorResponse: ...


class NoopPlanner:
    async def plan(self, request: AdvisorRequest) -> AdvisorRequest:
        return request


class NoopEvaluator:
    async def evaluate(self, request: AdvisorRequest,
                       response: AdvisorResponse) -> AdvisorResponse:
        return response
