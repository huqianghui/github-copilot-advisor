"""Copilot 计费/用量只读查询(spec 7.2 工具5)。只实现 GET —— 只读铁律。"""
import httpx

_QUESTION_TYPES = {"seats_summary", "credits_usage", "user_usage",
                   "billing_mode"}

# AI credits 用量端点要求的 API 版本。2026-06-01 起 usage-based billing
# (AI credits,按 token 计费)取代 request-based billing,premium request
# units 随之退役 —— 旧的 premium_usage 概念不再有对应端点,故未保留兼容分支。
_CREDITS_API_VERSION = "2026-03-10"


def _summarize_credits(payload: dict) -> dict:
    """把 usageItems 聚合成模型可以直接陈述的形状。

    取 net* 而非 gross*:net 是折扣后的实际计费量,与客户账单上的数字一致。
    grossQuantity 在有折扣时偏大,拿它回答"我们用了多少额度"会高报。

    不按 product 过滤:AI credits 在计费实体层**池化**,同一份额度也会被
    Copilot 以外的消耗(Spark、Spaces、第三方 coding agent 等)吃掉。
    只汇总 product=="Copilot" 会给出一个比账单小的数字 —— 对"额度还剩多少"
    这个 P0 问题来说是错的。

    金额直接取 API 的 netAmount,不由 credits × $0.01 反推:单价与折扣都在
    响应里,反推会算出账单上不存在的数字。
    """
    per_model: dict[str, dict[str, float]] = {}
    total_credits = 0.0
    total_amount = 0.0
    for item in payload.get("usageItems") or []:
        credits = float(item.get("netQuantity") or 0)
        amount = float(item.get("netAmount") or 0)
        total_credits += credits
        total_amount += amount
        # 同一个 model 可能跨多个 usageItem 出现(例如不同 product 都用了
        # GPT-5),必须累加而不是覆盖。
        model = item.get("model") or "unknown"
        bucket = per_model.setdefault(
            model, {"credits": 0.0, "amount_usd": 0.0})
        bucket["credits"] += credits
        bucket["amount_usd"] += amount
    by_model = sorted(
        ({"model": model,
          "credits": round(v["credits"], 2),
          "amount_usd": round(v["amount_usd"], 2)}
         for model, v in per_model.items()),
        key=lambda entry: entry["credits"], reverse=True)
    return {
        # 原样回传 API 声明的统计窗口:模型据此说"本月"还是"今年",
        # 不然它只能靠用户的提问措辞猜,可能把年度总量说成本月用量。
        "time_period": payload.get("timePeriod"),
        "organization": payload.get("organization"),
        "total_credits": round(total_credits, 2),
        "total_amount_usd": round(total_amount, 2),
        "by_model": by_model,
    }


class CopilotUsageClient:
    def __init__(self, base_url: str = "https://api.github.com"):
        self.base_url = base_url

    async def lookup(self, question_type: str, org: str, token: str,
                     username: str | None = None) -> dict:
        if question_type not in _QUESTION_TYPES:
            raise ValueError(f"unknown question_type: {question_type}")
        headers = {"Accept": "application/vnd.github+json",
                   "Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(base_url=self.base_url,
                                     headers=headers, timeout=15) as client:
            if question_type in ("billing_mode", "seats_summary"):
                resp = await client.get(f"/orgs/{org}/copilot/billing")
                resp.raise_for_status()
                return resp.json()
            if question_type == "credits_usage":
                # 前缀陷阱:billing usage API 挂在 /organizations/{org}/,
                # 而 Copilot API 挂在 /orgs/{org}/。这里写 /orgs/ 会 404。
                # 版本头只加在这一个请求上 —— 另外两个端点是已查证仍有效的
                # 现状,不拿新版本号去改它们的行为。
                resp = await client.get(
                    f"/organizations/{org}/settings/billing/ai_credit/usage",
                    headers={"X-GitHub-Api-Version": _CREDITS_API_VERSION})
                resp.raise_for_status()
                return _summarize_credits(resp.json())
            # user_usage
            resp = await client.get(
                f"/orgs/{org}/copilot/billing/seats",
                params={"per_page": 100})
            resp.raise_for_status()
            data = resp.json()
            seats = data.get("seats", [])
            if username:
                seats = [s for s in seats
                         if (s.get("assignee") or {}).get("login") == username]
            return {"total_seats": data.get("total_seats"), "seats": seats}
