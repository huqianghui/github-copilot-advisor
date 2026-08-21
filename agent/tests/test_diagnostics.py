from pathlib import Path

import httpx
import respx

from advisor_agent.diagnostics import NetworkDiagnostics

DIAG_YAML = """
endpoints:
  - name: github-login
    url: https://github.com/login
  - name: github-api
    url: https://api.github.com/user
  - name: copilot-proxy
    url: https://copilot-proxy.githubusercontent.com
enterprise_url_template: "https://github.com/enterprises/{slug}"
status_api: https://www.githubstatus.com/api/v2/summary.json
allowlist_doc: https://docs.github.com/en/copilot/reference/copilot-allowlist-reference
self_test_commands:
  - 'curl -s --max-time 10 {url} -o /dev/null -w "HTTP %{{http_code}}, total %{{time_total}}s\\n"'
"""


def make_diag(tmp_path: Path) -> NetworkDiagnostics:
    p = tmp_path / "diagnostics.yaml"
    p.write_text(DIAG_YAML, encoding="utf-8")
    return NetworkDiagnostics(p, timeout_seconds=1.0)


def mock_status(indicator="none", incidents=()):
    respx.get("https://www.githubstatus.com/api/v2/summary.json").mock(
        return_value=httpx.Response(200, json={
            "status": {"indicator": indicator},
            "incidents": [{"name": n, "shortlink": f"https://stspg.io/{i}"}
                          for i, n in enumerate(incidents)],
        }))


@respx.mock
async def test_all_reachable_and_status_green_means_check_egress(tmp_path):
    respx.get("https://github.com/login").mock(
        return_value=httpx.Response(200))
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(401))   # 预期 401:链路通
    respx.get("https://copilot-proxy.githubusercontent.com").mock(
        return_value=httpx.Response(200))
    mock_status("none")
    out = await make_diag(tmp_path).run()
    assert out["verdict"] == "github_ok_check_egress"
    assert all(p["reachable"] for p in out["probes"])
    assert len(out["probes"]) == 3          # 无 slug 不加企业端点
    assert out["self_test_commands"]        # 自测命令已按端点展开
    assert "allowlist" in out["allowlist_doc"]


@respx.mock
async def test_incident_verdict_carries_shortlink(tmp_path):
    respx.get("https://github.com/login").mock(
        return_value=httpx.Response(200))
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(401))
    respx.get("https://copilot-proxy.githubusercontent.com").mock(
        return_value=httpx.Response(200))
    mock_status("major", incidents=["Copilot degraded"])
    out = await make_diag(tmp_path).run()
    assert out["verdict"] == "github_incident"
    assert out["github_status"]["incidents"][0]["name"] == "Copilot degraded"


@respx.mock
async def test_unreachable_endpoint_yields_partial(tmp_path):
    respx.get("https://github.com/login").mock(
        side_effect=httpx.ConnectTimeout("timeout"))
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(401))
    respx.get("https://copilot-proxy.githubusercontent.com").mock(
        return_value=httpx.Response(200))
    mock_status("none")
    out = await make_diag(tmp_path).run()
    assert out["verdict"] == "partial"
    failed = [p for p in out["probes"] if not p["reachable"]]
    assert failed[0]["name"] == "github-login"
    assert "timeout" in failed[0]["error"].lower()


@respx.mock
async def test_enterprise_slug_adds_probe(tmp_path):
    for url in ["https://github.com/login", "https://api.github.com/user",
                "https://copilot-proxy.githubusercontent.com",
                "https://github.com/enterprises/customer-a"]:
        respx.get(url).mock(return_value=httpx.Response(200))
    mock_status("none")
    out = await make_diag(tmp_path).run(enterprise_slug="customer-a")
    assert len(out["probes"]) == 4
    assert any("enterprises/customer-a" in p["url"] for p in out["probes"])


@respx.mock
async def test_status_api_failure_degrades_gracefully(tmp_path):
    for url in ["https://github.com/login", "https://api.github.com/user",
                "https://copilot-proxy.githubusercontent.com"]:
        respx.get(url).mock(return_value=httpx.Response(200))
    respx.get("https://www.githubstatus.com/api/v2/summary.json").mock(
        side_effect=httpx.ConnectError("down"))
    out = await make_diag(tmp_path).run()
    assert out["github_status"]["indicator"] == "unknown"
    assert out["verdict"] == "github_ok_check_egress"


@respx.mock
async def test_shipped_diagnostics_yaml_renders_self_test_commands():
    """守护测试:真实配置文件的模板必须能安全 format(回归 KeyError)。"""
    from pathlib import Path as _P
    shipped = _P(__file__).parent.parent / "diagnostics.yaml"
    for url in ["https://github.com/login", "https://api.github.com/user",
                "https://copilot-proxy.githubusercontent.com"]:
        respx.get(url).mock(return_value=httpx.Response(200))
    respx.get("https://www.githubstatus.com/api/v2/summary.json").mock(
        return_value=httpx.Response(200, json={"status": {"indicator": "none"},
                                               "incidents": []}))
    out = await NetworkDiagnostics(shipped, timeout_seconds=1.0).run()
    assert out["self_test_commands"]
    assert "%{http_code}" in out["self_test_commands"][0]
