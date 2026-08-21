"""MAF 注册的三个工具。docstring 即工具描述,LLM 依此决定调用时机(spec 7.2/7.3)。"""
import json
import time

from advisor_agent.escalation import EscalationConfig
from advisor_agent.run_context import current_run
from advisor_shared.messages import MentionDirective


class AdvisorTools:
    def __init__(self, combined, web, escalation: EscalationConfig):
        self._combined = combined
        self._web = web
        self._escalation = escalation

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
