"""Operator entry points; validation does not imply installed service health."""

import argparse
import json
import subprocess
from pathlib import Path

from . import build, configuration, database, install, manifest, policy, preflight


def main() -> None:
    parser = argparse.ArgumentParser(description="KF bounded offline delivery")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("contract")
    check.add_argument("path", type=Path)
    check = sub.add_parser("verify")
    check.add_argument("bundle", type=Path)
    check.add_argument("--manifest-sha256", required=True)
    check = sub.add_parser("build")
    check.add_argument("--repo", type=Path, required=True)
    check.add_argument("--inputs", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    check.add_argument("--work", type=Path, required=True)
    for name in ("preflight", "install-files"):
        check = sub.add_parser(name)
        check.add_argument("bundle", type=Path)
        check.add_argument("--manifest-sha256", required=True)
    check = sub.add_parser("generate-configuration")
    check.add_argument("bundle", type=Path)
    check.add_argument("--manifest-sha256", required=True)
    check.add_argument("--profile", type=Path, required=True)
    check.add_argument("--connections", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    check.add_argument("--attest-measurement", action="store_true")
    check = sub.add_parser("install-configuration")
    check.add_argument("directory", type=Path)
    check.add_argument("--receipt-sha256", required=True)
    check = sub.add_parser("check-configuration")
    check.add_argument("receipt", type=Path)
    check = sub.add_parser("provision-database-identities")
    check.add_argument("--operator", type=Path, required=True)
    check.add_argument("--connections", type=Path, required=True)
    check = sub.add_parser("activate")
    check.add_argument("release_id")
    check.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    try:
        if args.command == "contract":
            policy.contract(args.path)
            result = {"contract_valid": True, "installed": False, "execution_verified": False}
        elif args.command == "verify":
            data = manifest.verify(args.bundle, args.manifest_sha256)
            result = {
                "bundle_valid": True,
                "source_sha": data["source_sha"],
                "payload_bytes": data["payload_bytes"],
                "files": len(data["files"]),
                "installed": False,
                "execution_verified": False,
            }
        elif args.command == "build":
            result = build.build(args.repo, args.inputs, args.output, args.work)
        elif args.command == "preflight":
            data = manifest.verify(args.bundle, args.manifest_sha256)
            result = preflight.observe(args.bundle / "payload", bundle_data=data)
            print(json.dumps(result, sort_keys=True))
            if not result["eligible"]:
                parser.exit(1)
            return
        elif args.command == "install-files":
            result = install.files(args.bundle, args.manifest_sha256)
        elif args.command == "generate-configuration":
            result = configuration.generate(
                args.bundle,
                args.manifest_sha256,
                args.profile,
                args.connections,
                args.output,
                attest=args.attest_measurement,
            )
        elif args.command == "install-configuration":
            result = configuration.install_configuration(args.directory, args.receipt_sha256)
        elif args.command == "check-configuration":
            receipt = configuration.check_installed(args.receipt)
            result = {
                "configuration_valid": True,
                "source_sha": receipt["source_sha"],
                "execution_verified": False,
            }
        elif args.command == "provision-database-identities":
            result = database.provision(args.operator, args.connections)
        else:
            result = install.activate(args.release_id, args.manifest_sha256)
        print(json.dumps(result, sort_keys=True))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        # OSError filenames can include private operator paths; do not echo them.
        parser.exit(
            1,
            "Delivery refused: "
            + (str(error) if isinstance(error, policy.DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
