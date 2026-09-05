import httpx
import pytest
import respx

from advisor_agent.usage import CopilotUsageClient

API = "https://api.github.com"

CREDITS_URL = f"{API}/organizations/acme/settings/billing/ai_credit/usage"

# 样本刻意做成多维度,让"聚合"这件事真的可证伪。一条 usageItem 的样本会让
# 求和/取第一项/取最大/取最后一项/用 grossQuantity 全部得到同一个数字,
# 那样的测试通过与否与实现无关。这里的四条数据满足:
#
#   ∑netQuantity     = 25 + 60 + 50 + 20 = 155   ← 唯一正确答案
#   ∑grossQuantity   = 30 + 100 + 50 + 20 = 200  ← 用错字段
#   第一项            = 25                        ← 不聚合
#   最大项            = 60                        ← 取 max
#   最后一项          = 20                        ← 覆盖而非累加
#
# 五个值两两不等,所以断言 155 能同时否掉另外四种实现。
# 另外 GPT-5 跨两条 item 出现(Copilot 一条、Spark 一条),by_model 必须把
# 它们累加成 80 —— 若实现是"覆盖"则会得到 60 或 20,同样被区分开。
# 折扣是有意给的:没有折扣时 net == gross,grossQuantity 那个变异就检测不到。
CREDITS_PAYLOAD = {
    "timePeriod": {"year": 2026, "month": 9},
    "organization": "acme",
    "usageItems": [
        {"product": "Copilot", "sku": "Copilot AI Credits",
         "model": "GPT-4o-mini", "unitType": "credits", "pricePerUnit": 0.01,
         "grossQuantity": 30, "grossAmount": 0.30,
         "discountQuantity": 5, "discountAmount": 0.05,
         "netQuantity": 25, "netAmount": 0.25},
        {"product": "Copilot", "sku": "Copilot AI Credits",
         "model": "GPT-5", "unitType": "credits", "pricePerUnit": 0.01,
         "grossQuantity": 100, "grossAmount": 1.00,
         "discountQuantity": 40, "discountAmount": 0.40,
         "netQuantity": 60, "netAmount": 0.60},
        {"product": "Copilot", "sku": "Copilot AI Credits",
         "model": "Claude-Sonnet-4.5", "unitType": "credits",
         "pricePerUnit": 0.01,
         "grossQuantity": 50, "grossAmount": 0.50,
         "discountQuantity": 0, "discountAmount": 0,
         "netQuantity": 50, "netAmount": 0.50},
        # 同一份 credits 池也会被 Copilot 以外的产品吃掉。
        {"product": "GitHub Spark", "sku": "Spark AI Credits",
         "model": "GPT-5", "unitType": "credits", "pricePerUnit": 0.01,
         "grossQuantity": 20, "grossAmount": 0.20,
         "discountQuantity": 0, "discountAmount": 0,
         "netQuantity": 20, "netAmount": 0.20},
    ],
}


def mock_credits(payload=None):
    return respx.get(CREDITS_URL).mock(
        return_value=httpx.Response(200, json=payload or CREDITS_PAYLOAD))


@respx.mock
async def test_credits_usage_uses_organizations_prefix_not_orgs():
    """前缀陷阱:billing usage API 是 /organizations/,Copilot API 才是 /orgs/。

    写成 /orgs/{org}/settings/billing/... 在真实 API 上是 404;respx 对未注册
    的请求会直接报错,所以这条路由就是对路径的断言。
    """
    route = mock_credits()
    await CopilotUsageClient().lookup("credits_usage", "acme", "tok")
    assert route.called
    assert route.calls.last.request.url.path == \
        "/organizations/acme/settings/billing/ai_credit/usage"


@respx.mock
async def test_credits_usage_sends_required_api_version_header():
    route = mock_credits()
    await CopilotUsageClient().lookup("credits_usage", "acme", "tok")
    assert route.calls.last.request.headers["X-GitHub-Api-Version"] == \
        "2026-03-10"


@respx.mock
async def test_credits_usage_sums_net_quantity_across_all_items():
    mock_credits()
    out = await CopilotUsageClient().lookup("credits_usage", "acme", "tok")
    # 手算,不是从被测代码推出来的:25 + 60 + 50 + 20
    assert out["total_credits"] == 155
    # 0.25 + 0.60 + 0.50 + 0.20
    assert out["total_amount_usd"] == 1.55


@respx.mock
async def test_credits_usage_totals_use_net_not_gross():
    """净额与总额在有折扣时不同,报总额会高估客户的额度消耗。"""
    mock_credits()
    out = await CopilotUsageClient().lookup("credits_usage", "acme", "tok")
    assert out["total_credits"] != 200        # ∑grossQuantity
    assert out["total_amount_usd"] != 2.00    # ∑grossAmount


@respx.mock
async def test_credits_usage_aggregates_by_model_sorted_by_credits():
    """GPT-5 跨两条 item,必须累加成 80 而不是取其中一条(60 或 20)。"""
    mock_credits()
    out = await CopilotUsageClient().lookup("credits_usage", "acme", "tok")
    assert out["by_model"] == [
        {"model": "GPT-5", "credits": 80, "amount_usd": 0.80},
        {"model": "Claude-Sonnet-4.5", "credits": 50, "amount_usd": 0.50},
        {"model": "GPT-4o-mini", "credits": 25, "amount_usd": 0.25},
    ]
    # 输入顺序是 GPT-4o-mini / GPT-5 / Claude,输出顺序不同 —— 排序被真正断言,
    # 而不是碰巧与输入同序。
    assert [e["model"] for e in out["by_model"]] != \
        ["GPT-4o-mini", "GPT-5", "Claude-Sonnet-4.5"]


@respx.mock
async def test_credits_usage_counts_products_sharing_the_credit_pool():
    """credits 在计费实体层池化:只汇总 product==Copilot 会少报 Spark 的 20。"""
    mock_credits()
    out = await CopilotUsageClient().lookup("credits_usage", "acme", "tok")
    assert out["total_credits"] == 155        # 若按 Copilot 过滤则是 135
    gpt5 = next(e for e in out["by_model"] if e["model"] == "GPT-5")
    assert gpt5["credits"] == 80              # 若按 Copilot 过滤则是 60


@respx.mock
async def test_credits_usage_passes_through_time_period():
    """模型据此说"本月"还是"今年";丢掉它就只能照用户措辞猜。"""
    mock_credits()
    out = await CopilotUsageClient().lookup("credits_usage", "acme", "tok")
    assert out["time_period"] == {"year": 2026, "month": 9}
    assert out["organization"] == "acme"


@respx.mock
async def test_credits_usage_handles_empty_usage():
    mock_credits({"timePeriod": {"year": 2026, "month": 9},
                  "organization": "acme", "usageItems": []})
    out = await CopilotUsageClient().lookup("credits_usage", "acme", "tok")
    assert out["total_credits"] == 0
    assert out["total_amount_usd"] == 0
    assert out["by_model"] == []


async def test_retired_premium_usage_question_type_is_rejected():
    """premium requests 已于 2026-06-01 退役,不保留兼容分支。"""
    with pytest.raises(ValueError, match="question_type"):
        await CopilotUsageClient().lookup("premium_usage", "acme", "tok")


@respx.mock
async def test_billing_mode_returns_org_summary():
    """已查证仍有效的端点,保持 /orgs/ 前缀不变。"""
    respx.get(f"{API}/orgs/acme/copilot/billing").mock(
        return_value=httpx.Response(200, json={
            "seat_breakdown": {"total": 50, "active_this_cycle": 42},
            "plan_type": "business",
            "seat_management_setting": "assign_selected",
        }))
    out = await CopilotUsageClient().lookup("billing_mode", "acme", "tok")
    assert out["plan_type"] == "business"
    assert out["seat_breakdown"]["total"] == 50


@respx.mock
async def test_user_usage_filters_by_username():
    """已查证仍有效的端点,保持 /orgs/ 前缀不变。"""
    respx.get(f"{API}/orgs/acme/copilot/billing/seats").mock(
        return_value=httpx.Response(200, json={
            "total_seats": 2,
            "seats": [
                {"assignee": {"login": "alice"}, "last_activity_at": "2026-08-20T00:00:00Z",
                 "last_activity_editor": "vscode/1.97"},
                {"assignee": {"login": "bob"}, "last_activity_at": None,
                 "last_activity_editor": None},
            ]}))
    out = await CopilotUsageClient().lookup("user_usage", "acme", "tok",
                                            username="bob")
    assert len(out["seats"]) == 1
    assert out["seats"][0]["assignee"]["login"] == "bob"


@respx.mock
async def test_permission_error_propagates_as_http_error():
    respx.get(f"{API}/orgs/acme/copilot/billing").mock(
        return_value=httpx.Response(403, json={"message": "forbidden"}))
    with pytest.raises(httpx.HTTPStatusError):
        await CopilotUsageClient().lookup("billing_mode", "acme", "tok")


@respx.mock
async def test_credits_permission_error_propagates_as_http_error():
    """新端点要 Administration read;权限不足时必须抛,而不是静默返回 0。"""
    respx.get(CREDITS_URL).mock(
        return_value=httpx.Response(403, json={"message": "forbidden"}))
    with pytest.raises(httpx.HTTPStatusError):
        await CopilotUsageClient().lookup("credits_usage", "acme", "tok")


async def test_unknown_question_type_raises():
    with pytest.raises(ValueError, match="question_type"):
        await CopilotUsageClient().lookup("hack_things", "acme", "tok")
