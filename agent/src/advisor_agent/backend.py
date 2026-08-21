"""LLM 编排后端协议:MAF 是默认实现(Task 10),预留 Copilot SDK 等(spec 7.5)。"""
from typing import Protocol


class AgentBackend(Protocol):
    async def run(self, user_text: str, history: list[dict]) -> str:
        """跑一轮完整的 tool loop,返回最终回答文本。
        history: [{"role": "user"|"assistant", "content": str}, ...]"""
        ...
