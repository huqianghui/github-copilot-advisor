import httpx
import pytest
import respx

from advisor_agent.usage import CopilotUsageClient

API = "https://api.github.com"


@respx.mock
async def test_billing_mode_returns_org_summary():
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


async def test_unknown_question_type_raises():
    with pytest.raises(ValueError, match="question_type"):
        await CopilotUsageClient().lookup("hack_things", "acme", "tok")
