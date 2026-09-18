"""Fixed observer controls for an ARM-authorized exec session; never a proof collector."""
import argparse
import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar
from azure.core.exceptions import AzureError
import jwt

from advisor_deploy.bench.plan import RunPlan
from advisor_deploy.bench.runner import ZeroProof
from advisor_deploy.paths import has_link


TOKEN_TIMEOUT_SECONDS = 10.
HTTP_TIMEOUT_SECONDS = 10.
MAX_RESPONSE_BYTES = 4096
MAX_PROOF_BYTES = 16 * 1024
MAX_PLAN_BYTES = 1024 * 1024
CONTROL_STATES = {"waiting", "starting", "running", "finished", "stopped"}
TokenProvider = Callable[[str, str], Awaitable[str]]


class ControlError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(content: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def nonfinite(value):
        raise ValueError()

    try:
        return json.loads(content.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=nonfinite)
    except (ValueError, TypeError, RecursionError):
        raise ControlError("invalid_json") from None


def _guid(value: str) -> str:
    return str(UUID(value))


def _operator_plan(plan: RunPlan, action: str) -> RunPlan:
    try:
        plan = RunPlan.model_validate(plan.model_dump(), context={"historical": action == "status"})
        if plan.local_test or plan.auth is None or plan.deployment is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise ControlError("invalid_control_plan") from None
    auth = plan.auth
    if _guid(auth.driver_client_id) == _guid(auth.audience):
        raise ControlError("driver_identity_must_be_distinct")
    if _guid(auth.driver_client_id) not in {_guid(item) for item in auth.operator_client_ids}:
        raise ControlError("driver_not_in_operator_allowlist")
    try:
        if _guid(os.environ["AZURE_CLIENT_ID"]) != _guid(auth.driver_client_id):
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ControlError("deployed_driver_identity_mismatch") from None
    return plan


async def managed_identity_token(client_id: str, scope: str) -> str:
    from azure.identity.aio import ManagedIdentityCredential

    async with ManagedIdentityCredential(client_id=client_id) as credential:
        return (await credential.get_token(scope)).token


def _check_operator_token(token: str, plan: RunPlan) -> None:
    # Sanity-check the selected MI/allowlists only. The receiver still verifies signature,
    # issuer, audience, times and role; unverified claims never choose an endpoint.
    try:
        if (not isinstance(token, str) or len(token) > 16384 or token.count(".") != 2
                or any(char.isspace() for char in token)):
            raise ValueError()
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or not header["kid"]:
            raise ValueError()
        claims = jwt.decode(token, options={"verify_signature": False})
        auth = plan.auth
        if (_guid(claims["azp"]) != _guid(auth.driver_client_id)
                or _guid(claims["oid"]) not in {_guid(item) for item in auth.operator_principal_ids}
                or _guid(claims["aud"]) != _guid(auth.audience)
                or _guid(claims["tid"]) != _guid(auth.tenant_id)
                or claims.get("iss") != f"https://login.microsoftonline.com/{_guid(auth.tenant_id)}/v2.0"
                or claims.get("ver") != "2.0" or claims.get("scp") is not None):
            raise ValueError()
        roles = claims.get("roles")
        if not isinstance(roles, list) or "Benchmark.Invoke" not in roles:
            raise ValueError()
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError):
        raise ControlError("operator_token_rejected") from None


async def request_control(
    plan: RunPlan, action: str, *, token_provider: TokenProvider = managed_identity_token,
    session: ClientSession | None = None,
) -> dict[str, str]:
    if action not in {"start", "status"}:
        raise ControlError("invalid_control_action")
    plan = _operator_plan(plan, action)
    auth = plan.auth
    try:
        token = await asyncio.wait_for(
            token_provider(auth.driver_client_id, auth.scope),
            TOKEN_TIMEOUT_SECONDS)
    except (AzureError, OSError, TimeoutError, ValueError, TypeError, RuntimeError):
        raise ControlError("operator_token_unavailable") from None
    _check_operator_token(token, plan)
    callback = urlsplit(plan.callback_url)
    url = urlunsplit((callback.scheme, callback.netloc, "/bench/control/" + action, "", ""))

    async def send(client: ClientSession):
        try:
            async with client.request(
                "POST" if action == "start" else "GET", url,
                json={} if action == "start" else None,
                headers={"Authorization": "Bearer " + token},
                allow_redirects=False, timeout=ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
            ) as response:
                if response.status != (202 if action == "start" else 200):
                    raise ControlError(f"control_http_{response.status}")
                if (response.content_type != "application/json"
                        or response.content_length is not None
                        and response.content_length > MAX_RESPONSE_BYTES):
                    raise ControlError("invalid_control_response")
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(1024):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ControlError("control_response_too_large")
                    chunks.append(chunk)
                value = _json(b"".join(chunks))
                if (not isinstance(value, dict) or set(value) != {"status"}
                        or not isinstance(value["status"], str)
                        or value["status"] not in CONTROL_STATES):
                    raise ControlError("invalid_control_response")
                return {"status": value["status"]}
        except (ClientError, OSError, TimeoutError):
            raise ControlError("control_unavailable") from None

    if session is not None:
        return await send(session)
    async with ClientSession(trust_env=False, cookie_jar=DummyCookieJar()) as client:
        return await send(client)


def _proof_destination(plan: RunPlan, plan_path: Path) -> Path:
    path = Path(plan_path).absolute()
    if ".." in path.parts or has_link(path) or not path.is_file():
        raise ControlError("unsafe_zero_path")
    root = path.parent / "results"
    if not plan.zero_evidence_path:
        raise ControlError("zero_path_not_configured")
    configured = Path(plan.zero_evidence_path)
    if ".." in configured.parts:
        raise ControlError("unsafe_zero_path")
    destination = configured if configured.is_absolute() else path.parent / configured
    if (destination.parent != root or has_link(root) or has_link(destination)
            or not root.is_dir()):
        raise ControlError("unsafe_zero_path")
    if destination.exists():
        metadata = destination.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ControlError("unsafe_zero_path")
    return destination


def write_zero(plan: RunPlan, content: bytes, *, plan_path: Path) -> int:
    plan = _operator_plan(plan, "write-zero")
    if plan.profile != "cold":
        raise ControlError("cold_profile_required")
    if not isinstance(content, bytes) or len(content) > MAX_PROOF_BYTES:
        raise ControlError("invalid_zero_evidence")
    values = _json(content)
    groups = {"http", "worker"} if plan.deployment.host == "functions" else {"app"}
    if not isinstance(values, list) or len(values) > len(groups):
        raise ControlError("invalid_zero_evidence")
    now = datetime.now(timezone.utc)
    seen = set()
    try:
        for value in values:
            proof = ZeroProof.model_validate(value)
            observed = datetime.fromisoformat(proof.observed_at.replace("Z", "+00:00"))
            if (proof.target_id != plan.expected_target_id or proof.group not in groups
                    or proof.group in seen or proof.replicas != 0
                    or observed.utcoffset() is None or observed > now
                    or (now - observed).total_seconds() > plan.zero_wait_seconds):
                raise ValueError()
            seen.add(proof.group)
    except (ValueError, TypeError, OverflowError):
        raise ControlError("invalid_zero_evidence") from None

    temporary = None
    try:
        destination = _proof_destination(plan, plan_path)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent, prefix=".zero-evidence-", suffix=".tmp")
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        _proof_destination(plan, plan_path)
        if has_link(temporary):
            raise ControlError("unsafe_zero_path")
        os.replace(temporary, destination)
        temporary = None
    except OSError:
        raise ControlError("zero_evidence_write_failed") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(values)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ControlError("invalid_arguments")


def main(argv=None) -> int:
    parser = _Parser(description="Fixed observer controls; execute inside the authorized observer.")
    parser.add_argument("--plan", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    commands.add_parser("start")
    commands.add_parser("status")
    write = commands.add_parser("write-zero")
    write.add_argument("--json", dest="proof_json", required=True)
    try:
        args = parser.parse_args(argv)
        if has_link(args.plan.absolute()):
            raise ControlError("invalid_control_plan")
        with args.plan.open("rb") as source:
            raw = source.read(MAX_PLAN_BYTES + 1)
        if len(raw) > MAX_PLAN_BYTES:
            raise ControlError("invalid_control_plan")
        try:
            plan = RunPlan.model_validate(
                _json(raw), context={"historical": args.command == "status"})
        except (ValueError, TypeError):
            raise ControlError("invalid_control_plan") from None
        if args.command == "write-zero":
            count = write_zero(plan, args.proof_json.encode("utf-8"), plan_path=args.plan)
            print(json.dumps({"written": count}))
        else:
            print(json.dumps(asyncio.run(request_control(plan, args.command))))
        return 0
    except ControlError as error:
        print(json.dumps({"error": error.code}))
        return 2
    except (OSError, ValueError, TypeError):
        print(json.dumps({"error": "control_input_unavailable"}))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"error": "control_interrupted"}))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
