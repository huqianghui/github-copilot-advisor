"""Offline CLI. Stdout contains only validated, non-secret summaries."""

import argparse
import json
from pathlib import Path
import sys

from advisor_deploy.config import IGNORED_NAMES, SECRET_NAMES, load_deploy_config
from advisor_deploy.package import build_image, build_package, validate_package


def _metadata_overrides(assignments: list[str]) -> dict[str, str]:
    result = {}
    for assignment in assignments:
        name, separator, value = assignment.partition("=")
        if not separator or not name or name in result:
            raise ValueError("invalid deployment configuration: malformed or duplicate metadata overlay")
        if name in SECRET_NAMES | IGNORED_NAMES or name.startswith("CONNECTIONS__"):
            raise ValueError("invalid deployment configuration: credentials are not CLI metadata")
        result[name] = value
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="advisor-deploy")
    commands = parser.add_subparsers(dest="command", required=True)
    package = commands.add_parser("package")
    package.add_argument("--target", choices=("aca", "functions"), required=True)
    package.add_argument("--output", type=Path, required=True)
    package.add_argument("--escalation-config", type=Path)
    package.add_argument("--diagnostics-config", type=Path)
    package.add_argument("--env-file", type=Path, help="Explicit opt-in; no default .env is read")
    package.add_argument("--build-image", metavar="LOCAL_TAG", help="Optional local Linux build/import check; never pushes")
    verify = commands.add_parser("verify-package")
    verify.add_argument("path", type=Path)
    config = commands.add_parser("config")
    config.add_argument("--target", choices=("aca", "functions"), required=True)
    config.add_argument("--env-file", type=Path, required=True)
    for command in (config, package):
        command.add_argument("--set", dest="overrides", action="append", default=[], metavar="NAME=VALUE",
                             help="Non-secret deployment metadata overlay; repeat as needed")
    args = parser.parse_args(argv)
    try:
        if args.command == "config":
            result = load_deploy_config(
                args.env_file, args.target, overrides=_metadata_overrides(args.overrides),
            ).public_summary()
        elif args.command == "verify-package":
            result = validate_package(args.path).to_dict()
        else:
            escalation, diagnostics = args.escalation_config, args.diagnostics_config
            if args.env_file is not None:
                if escalation is not None or diagnostics is not None:
                    raise ValueError("invalid deployment configuration: choose env-file OR explicit YAML paths")
                loaded = load_deploy_config(
                    args.env_file, args.target, overrides=_metadata_overrides(args.overrides),
                )
                escalation = loaded.runtime_files["escalation.yaml"]
                diagnostics = loaded.runtime_files["diagnostics.yaml"]
            elif args.overrides:
                raise ValueError("invalid deployment configuration: metadata overlays require --env-file")
            if args.build_image and args.target != "aca":
                raise ValueError("invalid package: image build is ACA-only")
            manifest = build_package(
                args.target, args.output, escalation_config=escalation, diagnostics_config=diagnostics,
            )
            if args.build_image:
                manifest = build_image(args.output, args.build_image)
            result = manifest.to_dict()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ValueError, OSError) as error:
        # Only our bounded errors are public. System exceptions can expose paths.
        message = str(error) if isinstance(error, ValueError) and str(error).startswith(("invalid package:", "invalid deployment configuration")) else "deployment operation failed"
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
