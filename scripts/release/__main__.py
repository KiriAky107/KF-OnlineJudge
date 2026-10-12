"""Operator entry points; validation does not imply installed service health."""

import argparse
import json
import subprocess
from pathlib import Path

from . import build, manifest, policy


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
        else:
            result = build.build(args.repo, args.inputs, args.output, args.work)
        print(json.dumps(result, sort_keys=True))
    except (policy.DeliveryError, OSError, subprocess.SubprocessError) as error:
        # OSError filenames can include private operator paths; do not echo them.
        parser.exit(
            1,
            "Delivery refused: "
            + (str(error) if isinstance(error, policy.DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
