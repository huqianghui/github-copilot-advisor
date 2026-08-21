"""MAF 注册的五个工具。docstring 即工具描述,LLM 依此决定调用时机(spec 7.2/7.3)。"""
import json
import os
import time

from advisor_agent.diagnostics import NetworkDiagnostics
from advisor_agent.escalation import EscalationConfig
from advisor_agent.run_context import current_run
from advisor_agent.usage import CopilotUsageClient
from advisor_shared.messages import MentionDirective


class AdvisorTools:
    def __init__(self, combined, web, escalation: EscalationConfig,
                 diagnostics: NetworkDiagnostics, usage: CopilotUsageClient):
        self._combined = combined
        self._web = web
        self._escalation = escalation
        self._diagnostics = diagnostics
        self._usage = usage

    async def search_solutions(self, query: str,
                               product_area: str | None = None) -> str:
        """搜索已解决的知识库问答和 GitHub 上正在讨论的相关 issue。
        回答任何 GitHub Copilot 问题前必须先调用此工具。
        product_area 可选值:vscode / intellij / cli / web / general。"""
        run = current_run.get()
        start = time.monotonic()
        out = await self._combined.search_solutions(
            query, product_area=product_area)
        run.tool_latencies_ms["search_solutions"] = int(
            (time.monotonic() - start) * 1000)
        origins = {item["origin"] for item in out["results"]}
        if "kb" in origins:
            run.stage = "kb_hit"
        elif "github-live" in origins:
            run.stage = "live_hit"
        run.citations_seen.extend(
            {"title": item["title"], "url": item["url"]}
            for item in out["results"])
        return json.dumps(out, ensure_ascii=False)

    async def web_search(self, query: str) -> str:
        """在 web 上搜索最新信息(版本发布、技术博客、官方文档)。
        仅当 search_solutions 返回 no_results 时才使用此工具。"""
        run = current_run.get()
        start = time.monotonic()
        results, failovers = await self._web.search(query)
        run.tool_latencies_ms["web_search"] = int(
            (time.monotonic() - start) * 1000)
        run.stage = "web"
        run.failover_count += failovers
        run.citations_seen.extend(
            {"title": r.title, "url": r.url} for r in results)
        return json.dumps(
            {"results": [r.model_dump() for r in results]},
            ensure_ascii=False)

    async def escalate_to_human(self, channel_id: str, reason: str) -> str:
        """升级到人工支持(CSAM/CSA)。仅当:用户明确表示问题仍未解决或不满意;
        或问题涉及账务、合同、配额调整、组织级配置时使用。
        reason 用一句话说明已尝试的路径,便于接手人了解上下文。"""
        run = current_run.get()
        contacts, ticket_url = self._escalation.lookup(channel_id)
        run.stage = "escalated"
        for c in contacts:
            if c.in_channel and c.teams_user_id:
                run.mentions.append(MentionDirective(
                    name=c.name, platform_user_id=c.teams_user_id,
                    role=c.role))
        return json.dumps({
            "contacts": [c.model_dump() for c in contacts],
            "support_ticket_url": ticket_url,
            "reason_recorded": reason,
        }, ensure_ascii=False)

    async def network_diagnostics(self, channel_id: str) -> str:
        """主动探测 GitHub/Copilot 服务链路并查询 GitHub 官方状态页。
        当问题涉及超时、登录失败、断连、Authorization error 时,
        在 search_solutions 之后调用,把探测证据合并进回答。"""
        run = current_run.get()
        start = time.monotonic()
        entry = self._escalation.channel_entry(channel_id)
        out = await self._diagnostics.run(
            enterprise_slug=entry.enterprise_slug if entry else None)
        run.tool_latencies_ms["network_diagnostics"] = int(
            (time.monotonic() - start) * 1000)
        return json.dumps(out, ensure_ascii=False)

    async def copilot_usage_lookup(self, channel_id: str, is_group: bool,
                                   question_type: str,
                                   username: str | None = None) -> str:
        """查询本组织 Copilot 计费与用量的真实数据。
        question_type:billing_mode(计费模式/seat 总量)、seats_summary、
        premium_usage(premium requests 用量)、user_usage(个人明细,仅限私聊)。"""
        run = current_run.get()
        entry = self._escalation.channel_entry(channel_id)
        token = os.environ.get(entry.org_token_env, "") \
            if entry and entry.org_token_env else ""
        if not (entry and entry.github_org and token):
            return json.dumps({
                "status": "not_configured",
                "guidance": ("未配置贵组织的查询授权。请组织的 org admin 创建"
                             "只读 fine-grained PAT(Copilot read + billing "
                             "read 权限),交给支持团队配置后即可直接查询;"
                             "也可自行访问 GitHub Settings → Copilot → Usage 查看。"),
            }, ensure_ascii=False)
        if is_group and question_type == "user_usage":
            return json.dumps({
                "status": "privacy_blocked",
                "message": "个人用量明细涉及隐私,请与我 1:1 私聊查询。",
            }, ensure_ascii=False)
        start = time.monotonic()
        try:
            data = await self._usage.lookup(
                question_type, entry.github_org, token, username)
            result = {"status": "ok", "data": data}
        except Exception as e:
            result = {"status": "error",
                      "message": f"查询失败:{type(e).__name__}。"
                                 "可能是 token 权限不足或已过期。"}
        run.tool_latencies_ms["copilot_usage_lookup"] = int(
            (time.monotonic() - start) * 1000)
        return json.dumps(result, ensure_ascii=False)
