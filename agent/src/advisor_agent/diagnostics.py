"""网络诊断:Azure 侧排除性证据 + GitHub 状态页 + 客户自测指引(spec 7.2 工具4)。
注意:agent 探测的是 Azure 出口视角,测不到客户网络 —— verdict 只做排除推理。"""
import asyncio
import time
from pathlib import Path

import httpx
import yaml
from pydantic import BaseModel


class ProbeResult(BaseModel):
    name: str
    url: str
    reachable: bool
    status_code: int | None = None
    latency_ms: int | None = None
    error: str | None = None


class NetworkDiagnostics:
    def __init__(self, config_path: Path, timeout_seconds: float = 5.0):
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        self.endpoints: list[dict] = raw["endpoints"]
        self.enterprise_template: str = raw.get("enterprise_url_template", "")
        self.status_api: str = raw["status_api"]
        self.allowlist_doc: str = raw.get("allowlist_doc", "")
        self.self_test_templates: list[str] = raw.get("self_test_commands", [])
        self.timeout = timeout_seconds

    async def run(self, enterprise_slug: str | None = None) -> dict:
        endpoints = list(self.endpoints)
        if enterprise_slug and self.enterprise_template:
            endpoints.append({
                "name": f"enterprise-{enterprise_slug}",
                "url": self.enterprise_template.format(slug=enterprise_slug),
            })
        async with httpx.AsyncClient(timeout=self.timeout,
                                     follow_redirects=True) as client:
            probes = await asyncio.gather(
                *(self._probe(client, e) for e in endpoints))
            status = await self._github_status(client)

        all_reachable = all(p.reachable for p in probes)
        if status["indicator"] not in ("none", "unknown"):
            verdict = "github_incident"
        elif all_reachable:
            verdict = "github_ok_check_egress"
        else:
            verdict = "partial"

        commands = [t.format(url=e["url"])
                    for e in endpoints for t in self.self_test_templates]
        return {
            "probes": [p.model_dump() for p in probes],
            "github_status": status,
            "verdict": verdict,
            "self_test_commands": commands,
            "allowlist_doc": self.allowlist_doc,
        }

    async def _probe(self, client: httpx.AsyncClient,
                     endpoint: dict) -> ProbeResult:
        start = time.monotonic()
        try:
            resp = await client.get(endpoint["url"])
            # 4xx(如 /user 的 401)说明 TCP/TLS/HTTP 链路是通的
            return ProbeResult(
                name=endpoint["name"], url=endpoint["url"], reachable=True,
                status_code=resp.status_code,
                latency_ms=int((time.monotonic() - start) * 1000))
        except Exception as e:
            return ProbeResult(
                name=endpoint["name"], url=endpoint["url"], reachable=False,
                error=f"{type(e).__name__}: {e}")

    async def _github_status(self, client: httpx.AsyncClient) -> dict:
        try:
            resp = await client.get(self.status_api)
            resp.raise_for_status()
            data = resp.json()
            return {
                "indicator": data["status"]["indicator"],
                "incidents": [{"name": i["name"],
                               "shortlink": i.get("shortlink", "")}
                              for i in data.get("incidents", [])],
            }
        except Exception:
            return {"indicator": "unknown", "incidents": []}
