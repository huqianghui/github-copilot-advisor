"""MAF 注册的五个工具。docstring 即工具描述,LLM 依此决定调用时机(spec 7.2/7.3)。"""
import json
import logging
import os
import time

from advisor_agent.diagnostics import NetworkDiagnostics
from advisor_agent.escalation import EscalationConfig
from advisor_agent.run_context import current_run
from advisor_agent.search.source_policy import WebSearchScope, source_confidence
from advisor_agent.usage import CopilotUsageClient
from advisor_shared.messages import MentionDirective

logger = logging.getLogger(__name__)


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

    async def web_search(self, query: str,
                         scope: WebSearchScope = "trusted") -> str:
        """仅当 search_solutions 返回 no_results=true 时搜索网络。
        默认 scope=trusted,优先检索 Copilot 官方文档/更新、VS Code 更新与
        issue、Copilot Community 和 copilotfaq。结果足以回答则直接按模板作答;
        这些结果为空或不足以回答时必须用 scope=general 扩展其它来源,
        不能把产品介绍/关键词命中当作解决方案直接给通用排查。
        source_confidence 仅表示来源置信度,不是答案充分性或已验证解决方案。"""
        run = current_run.get()
        if scope not in ("trusted", "general"):
            raise ValueError(f"invalid web search scope: {scope}")
        if scope == "general" and not run.trusted_web_searched:
            logger.warning("general web search blocked before trusted search")
            return json.dumps({
                "status": "trusted_search_required",
                "message": "请先使用 scope=trusted 搜索并判断是否足以回答。",
            }, ensure_ascii=False)
        start = time.monotonic()
        results, failovers = await self._web.search(query, scope=scope)
        run.tool_latencies_ms["web_search"] = (
            run.tool_latencies_ms.get("web_search", 0)
            + int((time.monotonic() - start) * 1000))
        if scope == "trusted":
            run.trusted_web_searched = True
        run.stage = "web"
        run.failover_count += failovers
        run.citations_seen.extend(
            {"title": r.title, "url": r.url} for r in results)
        return json.dumps(
            {"scope": scope, "no_results": not results,
             "guidance": (
                 "先判断结果是否包含针对当前问题的实质说明或可执行建议。"
                 "足够则停止搜索,用 2-3 句总结、建议列表、来源回答。"
                 "仅有产品介绍/关键词命中不算可用答案;结果不足或为空时,"
                 "下一步必须 web_search(scope='general'),不能直接给通用排查。"
                 if scope == "trusted" else
                 "仅引用能支撑当前问题的内容;无可靠答案时明确说明不确定性,"
                 "并按简洁模板给出必要建议。"),
             "results": [
                 {**r.model_dump(), "source_confidence": source_confidence(r)}
                 for r in results]},
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
        credits_usage(AI credits 用量与金额;用户用 premium requests 这类旧词
        提问时同样用它)、user_usage(个人明细,仅限私聊)。"""
        run = current_run.get()
        start = time.monotonic()
        try:
            entry = self._escalation.channel_entry(channel_id)
            token = os.environ.get(entry.org_token_env, "") \
                if entry and entry.org_token_env else ""
            if not (entry and entry.github_org and token):
                return json.dumps({
                    "status": "not_configured",
                    # 权限项跟着端点走:AI credits 用量端点要的是 organization
                    # "Administration" (read),不是 billing read。写错客户会建
                    # 一个在新端点上 403 的 token,而 403 在这里被吞成一句
                    # "权限不足"。
                    "guidance": ("未配置贵组织的查询授权。请组织的 org admin 创建"
                                 "只读 fine-grained PAT(Copilot read + "
                                 "Administration read 权限,后者用于查 AI "
                                 "credits 用量),交给支持团队配置后即可直接查询;"
                                 "也可自行访问 GitHub Settings → Copilot → "
                                 "Usage 查看。"),
                }, ensure_ascii=False)
            if is_group and question_type == "user_usage":
                return json.dumps({
                    "status": "privacy_blocked",
                    "message": "个人用量明细涉及隐私,请与我 1:1 私聊查询。",
                }, ensure_ascii=False)
            try:
                data = await self._usage.lookup(
                    question_type, entry.github_org, token, username)
                result = {"status": "ok", "data": data}
            except Exception as e:
                result = {"status": "error",
                          "message": f"查询失败:{type(e).__name__}。"
                                     "可能是 token 权限不足或已过期。"}
            return json.dumps(result, ensure_ascii=False)
        finally:
            # 记账必须无条件:not_configured / privacy_blocked 同样是"这个工具被
            # 调用过"的事实。tool_latencies_ms 是 AdvisorEvent 里唯一记录工具被
            # 调用的字段(spec 10.2),漏记会让隐私门禁在遥测里彻底隐形 —— 运营
            # 会以为没人问过个人用量,实际是问过、被挡了。放在 finally 而不是每个
            # return 前补一行:下一个加分支的人不可能再忘。
            run.tool_latencies_ms["copilot_usage_lookup"] = int(
                (time.monotonic() - start) * 1000)
