"""Root factory unit composition, separate from ordinary service hardening."""

import argparse
import json
import os
from pathlib import Path

from . import configuration, install, preflight, service_install, services
from .policy import DeliveryError, canonical, digest


def render(receipt: dict) -> bytes:
    receipt = configuration.validate_receipt(receipt, installed=False)
    release, batch = receipt["release_id"], receipt["configuration_id"]
    payload = install.RELEASES / release / "payload"
    binary = payload / "bin/kf-sandbox"
    runtime = configuration.installed_path(release, batch, "runtime.json")
    clean = "/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C "
    values = [
        "[Unit]",
        "Description=KF bounded Root helper factory",
        "After=local-fs.target",
        "[Service]",
        "Type=exec",
        "User=root",
        "Group=root",
        "Slice=kfonlinejudge.slice",
        "WorkingDirectory=" + str(payload),
        "UMask=0077",
        "ExecStartPre="
        + clean
        + services.python_environment(payload)
        + f" {payload}/python/bin/python3.13 -s -P -m scripts.release.services check-binding"
        + f" --release-id {release} --configuration-id {batch}",
        "ExecStartPre=" + clean + str(binary) + " prepare-factory --config " + str(runtime),
        "ExecStart="
        + clean
        + str(binary)
        + " factory --config "
        + str(runtime)
        + " --lifetime-seconds 3600 --max-requests 4096",
        "MemoryMax=256M",
        "MemorySwapMax=0",
        "TasksMax=32",
        "CPUQuota=50%",
        "LimitCORE=0",
        "LimitFSIZE=64M",
        "LimitNOFILE=256",
        "LimitSTACK=16M",
        "TimeoutStartSec=1800",
        "RuntimeMaxSec=3700",
        "TimeoutStopSec=30",
        "KillMode=mixed",
        "Restart=no",
        "StandardOutput=journal",
        "StandardError=journal",
        "LogRateLimitIntervalSec=30s",
        "LogRateLimitBurst=100",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
    return ("\n".join(values) + "\n").encode()


def generate(output: Path) -> dict:
    if os.getuid() != 0 or os.geteuid() != 0:
        raise DeliveryError("Root factory composition requires trusted operator")
    selected, original = service_install.current_receipt()
    payload = install.RELEASES / selected["release_id"] / "payload"
    for name in ("factory_service.py", "services.py"):
        installed = payload / "delivery/scripts/release" / name
        preflight.protected(installed)
        if digest(installed) != digest(Path(__file__).parent / name):
            raise DeliveryError("factory composition differs from selected delivery")
    preflight.protected(payload / "bin/kf-sandbox")
    if output.exists() or output.is_symlink():
        raise DeliveryError("factory unit output must be new")
    preflight.protected(output.parent, directory=True)
    output.mkdir(mode=0o700)
    unit = output / "kf-sandbox-factory.service"
    unit.write_bytes(render(original))
    unit.chmod(0o600)
    result = {
        "schema_version": 1,
        "source_sha": original["source_sha"],
        "configuration_id": selected["configuration_id"],
        "configuration_receipt_sha256": selected["receipt_sha256"],
        "unit_sha256": digest(unit),
        "installed": False,
        "services_started": False,
        "execution_verified": False,
    }
    receipt = output / "receipt.json"
    receipt.write_bytes(canonical(result))
    receipt.chmod(0o600)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Protected Root factory unit composition")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(generate(args.output), sort_keys=True))
    except (OSError, ValueError) as error:
        parser.exit(
            1,
            "Factory composition refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
