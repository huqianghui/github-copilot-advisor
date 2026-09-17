"""MAF 注册的五个工具。docstring 即工具描述,LLM 依此决定调用时机(spec 7.2/7.3)。"""
import json
import logging
import os

from advisor_agent.diagnostics import NetworkDiagnostics
from advisor_agent.escalation import EscalationConfig
from advisor_agent.run_context import current_run, timed_tool
from advisor_agent.search.source_policy import source_confidence
from advisor_agent.usage import CopilotUsageClient
from advisor_shared.messages import MentionDirective
from advisor_shared.telemetry import current_span, step

logger = logging.getLogger(__name__)


class AdvisorTools:
    def __init__(self, combined, web, escalation: EscalationConfig,
                 diagnostics: NetworkDiagnostics, usage: CopilotUsageClient):
        self._combined = combined
        self._web = web
        self._escalation = escalation
        self._diagnostics = diagnostics
        self._usage = usage

    @timed_tool
    async def search_solutions(self, query: str,
                               product_area: str | None = None) -> str:
        """搜索已解决的知识库问答和 GitHub 上正在讨论的相关 issue。
        回答任何 GitHub Copilot 问题前必须先调用此工具。
        product_area 可选值:vscode / intellij / cli / web / general。"""
        run = current_run.get()
        out = await self._combined.search_solutions(
            query, product_area=product_area)
        origins = {item["origin"] for item in out["results"]}
        if "kb" in origins:
            run.stage = "kb_hit"
        elif "github-live" in origins:
            run.stage = "live_hit"
        run.citations_seen.extend(
            {"title": item["title"], "url": item["url"]}
            for item in out["results"])
        return json.dumps(out, ensure_ascii=False)

    @timed_tool
    async def web_search(self, query: str) -> str:
        """仅当 search_solutions 返回 no_results=true 时使用,每个问答回合仅调用一次。
        工具内部在总预算内并行检索可信来源与全网,合并去重并优先呈现可信来源。
        根据 status 区分成功、部分成功、空结果、超时和服务不可用。
        返回后直接使用已有证据回答,不足时说明限制;不要换词或再次调用。
        source_confidence 仅表示来源置信度,不是答案充分性或已验证解决方案。"""
        run = current_run.get()
        async with run.web_search_lock:
            timing = current_span()
            if run.web_search_result is not None:
                if timing is not None:
                    timing.attributes["cached"] = True
                logger.info("reusing Web retrieval from this turn")
                return run.web_search_result
            if run.web_search_started:
                logger.warning("interrupted Web retrieval cannot restart in this turn")
                return json.dumps({
                    "status": "already_attempted", "no_results": None,
                    "retry_allowed": False, "results": [],
                    "guidance": "本回合检索已中断,不要再次搜索;请说明限制并按已有证据回答。",
                }, ensure_ascii=False)
            run.web_search_started = True
            run.stage = "web"
            outcome = await self._web.retrieve(query)
            run.failover_count += outcome.failovers
            run.citations_seen.extend(
                {"title": r.title, "url": r.url} for r in outcome.results)
            if timing is not None:
                timing.status = ("degraded" if outcome.status == "partial"
                                 else outcome.status)
                timing.attributes["cached"] = False
            guidance = (
                "本回合 Web 检索已完成,不要再次调用或换词重搜。"
                "先判断证据是否真正回答问题,优先使用可信来源,仅引用实际支撑答案的链接。"
                "没有充分证据时说明原因尚未确认,不要把产品介绍或关键词命中当作解决方案。")
            if outcome.status in {"timeout", "error", "not_configured", "partial"}:
                guidance += (
                    "部分或全部检索未能完成,不能据此断言网上没有资料;"
                    "可基于已获得证据回答,并明确说明检索受限。")
            run.web_search_result = json.dumps({
                "status": outcome.status,
                "no_results": (False if outcome.results else
                               True if outcome.status == "empty" else None),
                "retry_allowed": False,
                "guidance": guidance,
                "scopes": [
                    {"scope": scope.scope, "status": scope.status,
                     "result_count": len(scope.results),
                     "attempts": [a.model_dump() for a in scope.attempts]}
                    for scope in outcome.scopes],
                "results": [
                    {**r.model_dump(), "source_confidence": source_confidence(r)}
                    for r in outcome.results],
            }, ensure_ascii=False)
            return run.web_search_result

    @timed_tool
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

    @timed_tool
    async def network_diagnostics(self, channel_id: str) -> str:
        """主动探测 GitHub/Copilot 服务链路并查询 GitHub 官方状态页。
        当问题涉及超时、登录失败、断连、Authorization error 时,
        在 search_solutions 之后调用,把探测证据合并进回答。"""
        entry = self._escalation.channel_entry(channel_id)
        out = await self._diagnostics.run(
            enterprise_slug=entry.enterprise_slug if entry else None)
        return json.dumps(out, ensure_ascii=False)

    @timed_tool
    async def copilot_usage_lookup(self, channel_id: str, is_group: bool,
                                   question_type: str,
                                   username: str | None = None) -> str:
        """查询本组织 Copilot 计费与用量的真实数据。
        question_type:billing_mode(计费模式/seat 总量)、seats_summary、
        credits_usage(AI credits 用量与金额;用户用 premium requests 这类旧词
        提问时同样用它)、user_usage(个人明细,仅限私聊)。"""
        entry = self._escalation.channel_entry(channel_id)
        token = os.environ.get(entry.org_token_env, "") \
            if entry and entry.org_token_env else ""
        timing = current_span()
        if not (entry and entry.github_org and token):
            if timing is not None:
                timing.status = "not_configured"
            return json.dumps({
                "status": "not_configured",
                # AI credits 用量端点需要 organization "Administration" (read)。
                "guidance": ("未配置贵组织的查询授权。请组织的 org admin 创建"
                             "只读 fine-grained PAT(Copilot read + "
                             "Administration read 权限,后者用于查 AI "
                             "credits 用量),交给支持团队配置后即可直接查询;"
                             "也可自行访问 GitHub Settings → Copilot → "
                             "Usage 查看。"),
            }, ensure_ascii=False)
        if is_group and question_type == "user_usage":
            if timing is not None:
                timing.attributes["outcome"] = "privacy_blocked"
            return json.dumps({
                "status": "privacy_blocked",
                "message": "个人用量明细涉及隐私,请与我 1:1 私聊查询。",
            }, ensure_ascii=False)
        try:
            with step("usage.lookup"):
                data = await self._usage.lookup(
                    question_type, entry.github_org, token, username)
            result = {"status": "ok", "data": data}
        except Exception as e:
            if timing is not None:
                timing.status = "degraded"
            result = {"status": "error",
                      "message": f"查询失败:{type(e).__name__}。"
                                 "可能是 token 权限不足或已过期。"}
        return json.dumps(result, ensure_ascii=False)
