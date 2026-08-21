"""Copilot 计费/用量只读查询(spec 7.2 工具5)。只实现 GET —— 只读铁律。"""
import httpx

_QUESTION_TYPES = {"seats_summary", "premium_usage", "user_usage",
                   "billing_mode"}


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
            if question_type == "premium_usage":
                resp = await client.get(
                    f"/orgs/{org}/settings/billing/usage")
                resp.raise_for_status()
                data = resp.json()
                items = [u for u in data.get("usageItems", [])
                         if "copilot" in (u.get("product") or "").lower()]
                return {"usageItems": items}
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
