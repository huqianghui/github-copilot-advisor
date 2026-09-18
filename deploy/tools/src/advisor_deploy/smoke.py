"""Bounded anonymous HTTP probes plus strictly deployment-scoped acceptance evidence.

Evidence format: advisor-smoke-evidence-v1, resource_id, artifact_digest,
config_digest, auth_mode, checks (CheckStatus strings), and optional observed
embedding_dimensions. No response bodies, keys, URLs or arbitrary details are accepted.
Only current HTTP observations determine health and anonymous endpoint rejection.
Other checks remain not_run without explicit imported evidence.
"""
import argparse
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import json
import math
import os
from pathlib import Path
import re
import sys
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp

from advisor_deploy.manifest import arm_parts, load_manifest, read_json
from advisor_deploy.paths import has_link


CHECKS = (
    "health", "ingress_ack", "embedding", "model", "search", "blob_queue",
    "bot_auth", "benchmark_auth", "complete_answer", "history_recovery", "teams_client",
)


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    UNAUTHORIZED = "unauthorized"
    NOT_RUN = "not_run"


@dataclass(frozen=True)
class SmokeResult:
    checks: Mapping[str, CheckStatus]

    @property
    def passed(self) -> bool:
        return all(self.checks[name] == CheckStatus.PASSED for name in CHECKS)

    @property
    def automated_passed(self) -> bool:
        return all(self.checks[name] == CheckStatus.PASSED for name in CHECKS if name != "teams_client")

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(name for name in CHECKS if self.checks[name] == CheckStatus.FAILED)


def evaluate_smoke(evidence: Mapping[str, str]) -> SmokeResult:
    if not isinstance(evidence, Mapping) or not set(evidence).issubset(CHECKS):
        raise ValueError("invalid smoke evidence")
    try:
        checks = {name: CheckStatus(evidence.get(name, "not_run")) for name in CHECKS}
    except (ValueError, TypeError):
        raise ValueError("invalid smoke evidence") from None
    return SmokeResult(MappingProxyType(checks))


_HTTP_CHECKS = ("health", "bot_auth", "benchmark_auth")
_EVIDENCE_FIELDS = {
    "format", "resource_id", "artifact_digest", "config_digest", "auth_mode", "checks",
}


class _SmokeError(ValueError):
    pass


@dataclass(frozen=True)
class _Target:
    resource_id: str
    origin: str
    artifact_digest: str
    config_digest: str
    auth_mode: str


def _origin_parts(origin: str):
    try:
        parsed = urlsplit(origin)
        valid = (
            parsed.hostname and parsed.username is None and parsed.password is None
            and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
            and "\\" not in origin
            and not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in origin)
        )
        port = parsed.port
    except (ValueError, TypeError, AttributeError):
        raise _SmokeError("invalid_endpoint") from None
    if not valid:
        raise _SmokeError("invalid_endpoint")
    return parsed, port


def _target(manifest: dict, auth_mode: str) -> _Target:
    if auth_mode not in ("key", "entra"):
        raise _SmokeError("invalid_auth_mode")
    if manifest["status"] != "deployed" or manifest["discovery_complete"] is not True:
        raise _SmokeError("deployment_not_ready")
    target = manifest["target"]
    outputs = manifest["outputs"]
    if target not in ("aca", "functions") or not outputs:
        raise _SmokeError("deployment_target_unsupported")
    resource_id = outputs["app_id"]
    if (not isinstance(resource_id, str) or not resource_id
            or resource_id.lower() not in {value.lower() for value in manifest["owned_resource_ids"]}):
        raise _SmokeError("app_not_owned")
    parts = arm_parts(resource_id)
    expected = "microsoft.app/containerapps" if target == "aca" else "microsoft.web/sites"
    if len(parts) != 9 or (parts[6] + "/" + parts[7]).lower() != expected:
        raise _SmokeError("app_type_mismatch")
    name = outputs["app_name"]
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if (not isinstance(name, str) or not re.fullmatch(label, name)
            or name != parts[8].lower()):
        raise _SmokeError("app_name_mismatch")
    parsed, port = _origin_parts(outputs["endpoint"])
    if parsed.scheme != "https" or port not in (None, 443):
        raise _SmokeError("invalid_endpoint")
    host = parsed.hostname
    labels = host.split(".")
    if any(not re.fullmatch(label, item) for item in labels):
        raise _SmokeError("invalid_endpoint")
    if target == "aca":
        owned = (labels[0] == name and labels[-2:] == ["azurecontainerapps", "io"]
                 and (len(labels) == 5 or len(labels) == 6 and labels[1] == "internal"))
    else:
        owned = host == f"{name}.azurewebsites.net" or (
            len(labels) == 4 and labels[-2:] == ["azurewebsites", "net"]
            and re.fullmatch(re.escape(name) + r"-[a-z0-9]+", labels[0]) is not None
            and re.fullmatch(r"[a-z0-9]+-[0-9]+", labels[1]) is not None)
    if not owned:
        raise _SmokeError("endpoint_not_bound_to_app")
    auth_fields = ("openai_auth", "search_auth")
    if any(key not in manifest["summary"] for key in auth_fields):
        raise _SmokeError("deployment_auth_mode_unverified")
    if any(manifest["summary"][key] != auth_mode for key in auth_fields):
        raise _SmokeError("deployment_auth_mode_mismatch")
    return _Target(resource_id, f"https://{host}", manifest["artifact_digest"],
                   manifest["config_digest"], auth_mode)


class _ResponseTooLarge(Exception):
    pass


async def _read_bounded(response: aiohttp.ClientResponse, limit: int) -> bytes:
    body = bytearray()
    async for chunk in response.content.iter_chunked(min(1024, limit + 1)):
        if len(body) + len(chunk) > limit:
            response.close()
            raise _ResponseTooLarge()
        body.extend(chunk)
    return bytes(body)


async def _probe_origin(origin: str, *, timeout_seconds: float = 10,
                        max_response_bytes: int = 4096) -> dict:
    """Internal transport boundary. Plain HTTP is allowed only on an explicit loopback port."""
    parsed, port = _origin_parts(origin)
    if not (parsed.scheme == "https" or (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
            and port is not None and port > 0)):
        raise _SmokeError("invalid_probe_origin")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30
            or type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 65536):
        raise _SmokeError("invalid_probe_limits")
    origin = origin.rstrip("/")
    timeout = aiohttp.ClientTimeout(
        total=timeout_seconds, connect=min(5, timeout_seconds), sock_read=timeout_seconds)
    observations = {}
    async with aiohttp.ClientSession(
        timeout=timeout, trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
        auto_decompress=False, read_bufsize=4096, max_line_size=8190, max_field_size=8190,
        headers={"Accept-Encoding": "identity", "User-Agent": "advisor-smoke/1"},
    ) as client:
        for check, method, path in (
            ("health", "GET", "/healthz"),
            ("bot_auth", "POST", "/api/messages"),
            ("benchmark_auth", "POST", "/bench/runs"),
        ):
            status_code = None
            state, category = CheckStatus.FAILED, "http_error"
            try:
                async with client.request(
                    method, origin + path, json={} if method == "POST" else None,
                    allow_redirects=False,
                ) as response:
                    status_code = response.status
                    if check == "benchmark_auth" and response.status == 404:
                        body = b""
                        response.close()
                    else:
                        body = await _read_bounded(response, max_response_bytes)
                    if 300 <= response.status < 400:
                        category = "redirect_blocked"
                    elif check == "health":
                        if response.status == 200 and body.strip() == b"ok":
                            state, category = CheckStatus.PASSED, "ok"
                        else:
                            category = ("unexpected_health_body" if response.status == 200
                                        else "unexpected_status")
                    elif response.status in (401, 403):
                        state, category = CheckStatus.PASSED, "unauthenticated_rejected"
                    elif check == "benchmark_auth" and response.status == 404:
                        state, category = CheckStatus.NOT_RUN, "disabled_or_missing"
                    else:
                        category = "unexpected_status"
            except _ResponseTooLarge:
                category = "response_too_large"
            except TimeoutError:
                category = "timeout"
            except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError):
                category = "tls_error"
            except (aiohttp.ClientConnectionError, OSError):
                category = "connection_error"
            except aiohttp.ClientError:
                category = "http_error"
            observations[check] = {
                "status": state.value, "http_status": status_code, "category": category,
            }
    return observations


def _import_evidence(
        path: Path, target: _Target,
) -> tuple[dict[str, CheckStatus], list[str], int | None]:
    try:
        if path.stat().st_size > 64 * 1024:
            raise _SmokeError("invalid_evidence")
        data = read_json(path)
    except (ValueError, OSError):
        raise _SmokeError("invalid_evidence") from None
    if (not _EVIDENCE_FIELDS <= set(data)
            or set(data) - _EVIDENCE_FIELDS - {"embedding_dimensions"}
            or data["format"] != "advisor-smoke-evidence-v1"
            or any(type(data[field]) is not str for field in (
                "resource_id", "artifact_digest", "config_digest", "auth_mode"))):
        raise _SmokeError("invalid_evidence")
    if (data["resource_id"].lower() != target.resource_id.lower()
            or data["artifact_digest"] != target.artifact_digest
            or data["config_digest"] != target.config_digest
            or data["auth_mode"] != target.auth_mode):
        raise _SmokeError("evidence_scope_mismatch")
    try:
        checks = dict(evaluate_smoke(data["checks"]).checks)
    except ValueError:
        raise _SmokeError("invalid_evidence") from None
    dimension = data.get("embedding_dimensions")
    if dimension is not None and (type(dimension) is not int or not 0 <= dimension <= 65536):
        raise _SmokeError("invalid_evidence")
    issues = []
    if checks["embedding"] == CheckStatus.PASSED:
        if dimension is None:
            checks["embedding"] = CheckStatus.NOT_RUN
            issues.append("embedding_dimension_missing")
        elif dimension != 3072:
            checks["embedding"] = CheckStatus.FAILED
            issues.append("embedding_dimension_mismatch")
    return checks, issues, dimension


def _report(target: _Target | None, auth_mode: str, checks: Mapping[str, str],
            observations: dict, evidence_import: str, issues: list[str],
            embedding_dimensions: int | None = None) -> dict:
    result = evaluate_smoke(checks)
    return {
        "format": "advisor-smoke-result-v1",
        "resource_id": target.resource_id if target else None,
        "artifact_digest": target.artifact_digest if target else None,
        "config_digest": target.config_digest if target else None,
        "auth_mode": auth_mode if auth_mode in ("key", "entra") else None,
        "checks": {key: value.value for key, value in result.checks.items()},
        "passed": result.passed and not issues,
        "automated_passed": result.automated_passed and not issues,
        "failed_checks": list(result.failed_checks),
        "http": observations, "evidence_import": evidence_import, "issues": issues,
        "embedding_dimensions": embedding_dimensions,
    }


async def run_smoke(deployment_path: Path, *, auth_mode: str,
                    evidence_path: Path | None = None) -> dict:
    """Probe only the manifest's owned deployed app; no credentials or model calls."""
    try:
        target = _target(load_manifest(Path(deployment_path)), auth_mode)
    except _SmokeError as error:
        category = (error.args[0] if error.args and error.args[0] in (
            "deployment_auth_mode_unverified", "deployment_auth_mode_mismatch")
            else "deployment_rejected")
        return _report(None, auth_mode, {}, {}, "not_run", [category])
    except (ValueError, OSError):
        return _report(None, auth_mode, {}, {}, "not_run", ["deployment_rejected"])
    checks, issues = {}, []
    imported = "not_run"
    dimension = None
    if evidence_path is not None:
        try:
            checks, issues, dimension = _import_evidence(Path(evidence_path), target)
            imported = "passed"
        except _SmokeError as error:
            imported = "failed"
            issues = [error.args[0]]
    observations = await _probe_origin(target.origin)
    # Imported acceptance cannot override current live endpoint observations.
    checks.update({name: observations[name]["status"] for name in _HTTP_CHECKS})
    return _report(target, auth_mode, checks, observations, imported, issues, dimension)


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise _SmokeError("invalid_arguments")


def _write_report(path: Path, report: dict, inputs: tuple[Path, ...]) -> None:
    path = path.absolute()
    if (path.suffix.lower() != ".json" or has_link(path)
            or any(path.resolve() == item.resolve() for item in inputs)):
        raise _SmokeError("output_rejected")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".next")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, sort_keys=True, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = _SafeParser(description="Run bounded, anonymous smoke probes for an owned deployment.")
    parser.add_argument("--deployment", required=True, type=Path, metavar="PATH")
    parser.add_argument("--auth-mode", required=True, metavar="{key,entra}")
    parser.add_argument("--output", required=True, type=Path, metavar="PATH")
    parser.add_argument("--evidence", type=Path, metavar="PATH")
    try:
        args = parser.parse_args(argv)
    except (ValueError, argparse.ArgumentError):
        print("smoke: invalid_arguments", file=sys.stderr)
        return 2
    inputs = (args.deployment,) + ((args.evidence,) if args.evidence else ())
    try:
        if (args.output.suffix.lower() != ".json" or has_link(args.output.absolute())
                or any(args.output.resolve() == item.resolve() for item in inputs)):
            raise _SmokeError("output_rejected")
        report = asyncio.run(run_smoke(
            args.deployment, auth_mode=args.auth_mode, evidence_path=args.evidence))
        _write_report(args.output, report, inputs)
    except (OSError, ValueError):
        print("smoke: output_or_input_rejected", file=sys.stderr)
        return 2
    print("smoke: passed" if report["passed"] else "smoke: incomplete")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
