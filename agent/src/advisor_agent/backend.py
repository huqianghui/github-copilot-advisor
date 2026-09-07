"""LLM 编排后端协议:MAF 是默认实现(Task 10),预留 Copilot SDK 等(spec 7.5)。"""
from typing import Protocol

from advisor_shared.messages import ImageInput


class AgentBackend(Protocol):
    async def run(self, user_text: str, history: list[dict],
                  images: list[ImageInput] | None = None) -> str:
        """跑一轮完整的 tool loop,返回最终回答文本。
        history: [{"role": "user"|"assistant", "content": str}, ...]
        images: 本轮附带的图片;历史里永远只有文本(见图片输入 spec §8)。"""
        ...
