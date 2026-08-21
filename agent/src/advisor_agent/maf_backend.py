"""编排后端:Azure OpenAI function-calling tool loop + 四个工具注册(spec 7.1/7.5)。

注:agent-framework-core(已安装,1.14.0)本身只提供 Agent/ChatAgent 骨架,真正
可用的 chat client 实现(OpenAIChatClient/AzureOpenAIChatClient)分别打包在
agent-framework-openai / agent-framework-azure-ai 这两个可选连接器包里 —— 二者
在当前环境都不可安装(corporate index 只有 agent-framework-azure-ai 的
pre-release,且已在 Task 1 从 agent/pyproject.toml 移除)。因此这里直接用已安装
的 openai SDK(AsyncAzureOpenAI,OpenAI API 兼容)实现一个等价的 function-calling
tool loop,对外仍暴露 MAFBackend 这个类名,并满足 AgentBackend 协议
(async run(user_text, history) -> str)。等 agent-framework-azure-ai 转正式版后,
可以把内部实现换成真正的 MAF ChatAgent,协议边界(AgentBackend)不需要变。
"""
import json
import os
from typing import Callable

from openai import AsyncAzureOpenAI

from advisor_agent.prompts import SYSTEM_PROMPT
from advisor_agent.tools import AdvisorTools

_MAX_TOOL_ROUNDS = 6

_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_solutions",
            "description": (
                "搜索已解决的知识库问答和 GitHub 上正在讨论的相关 issue。"
                "回答任何 GitHub Copilot 问题前必须先调用此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "product_area": {
                        "type": "string",
                        "description": "可选:vscode / intellij / cli / web / general",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "在 web 上搜索最新信息(版本发布、技术博客、官方文档)。"
                "仅当 search_solutions 返回 no_results=true 时才使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": (
                "升级到人工支持(CSAM/CSA)。仅当用户明确表示未解决/不满意,"
                "或问题涉及账务、合同、配额、组织级配置时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "一句话概括已尝试的路径",
                    },
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "network_diagnostics",
            "description": (
                "主动探测 GitHub/Copilot 链路 + GitHub 官方状态页。问题涉及"
                "超时/登录失败/断连/Authorization error 时,在 search_solutions 后调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


class MAFBackend:
    def __init__(self, tools: AdvisorTools,
                 channel_id_provider: Callable[[], str]):
        self._tools = tools
        self._channel_id = channel_id_provider
        self._client = AsyncAzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION",
                                       "2024-10-21"),
        )
        self._deployment = os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"]

    async def _dispatch(self, name: str, arguments: dict) -> str:
        if name == "search_solutions":
            return await self._tools.search_solutions(
                arguments["query"], arguments.get("product_area"))
        if name == "web_search":
            return await self._tools.web_search(arguments["query"])
        if name == "escalate_to_human":
            return await self._tools.escalate_to_human(
                self._channel_id(), arguments["reason"])
        if name == "network_diagnostics":
            return await self._tools.network_diagnostics(self._channel_id())
        raise ValueError(f"unknown tool: {name}")

    async def run(self, user_text: str, history: list[dict]) -> str:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        for _ in range(_MAX_TOOL_ROUNDS):
            response = await self._client.chat.completions.create(
                model=self._deployment,
                messages=messages,
                tools=_TOOL_SCHEMAS,
            )
            message = response.choices[0].message
            tool_calls = message.tool_calls
            if not tool_calls:
                return message.content or ""

            messages.append({
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in tool_calls
                ],
            })
            for call in tool_calls:
                arguments = json.loads(call.function.arguments or "{}")
                result = await self._dispatch(call.function.name, arguments)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })

        return "抱歉,处理这个问题花了太多轮工具调用,请换个方式描述或稍后重试。"
