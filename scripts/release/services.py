"""Ordinary service composition; rendering is not Linux service acceptance."""

import argparse
import hashlib
import json
import os
from pathlib import Path

from . import configuration, install, journal, manifest
from .policy import DeliveryError, canonical, digest
from .service_profiles import PROFILES as PROFILES


def python_environment(payload: Path) -> str:
    return (
        f"PYTHONHOME={payload}/python "
        f"PYTHONPATH={payload}/backend/src:{payload}/site:{payload}/delivery "
        f"LD_LIBRARY_PATH={payload}/lib:{payload}/python/lib PYTHONDONTWRITEBYTECODE=1"
    )


def render(receipt: dict) -> dict[str, bytes]:
    receipt = configuration.validate_receipt(receipt, installed=False)
    release, batch = receipt["release_id"], receipt["configuration_id"]
    payload = install.RELEASES / release / "payload"
    python = str(payload / "python/bin/python3.13")
    files = {}
    for name, (user, memory, pids, cpu, environment, credential) in PROFILES.items():
        unit_name = "kf-" + name + ".service"
        runtime = "kf-" + name
        values = [
            "[Unit]",
            "Description=KF bounded ordinary " + name,
            "After=network.target kf-postgres.service kf-rabbitmq.service",
            "StartLimitIntervalSec=600",
            "StartLimitBurst=3",
            "[Service]",
            "Type=exec",
            "Slice=kfonlinejudge.slice",
            "User=" + user,
            "Group=" + user,
            "WorkingDirectory=" + str(payload),
            "Environment=PATH=/usr/bin:/bin LC_ALL=C.UTF-8 " + python_environment(payload),
            "RuntimeDirectory=" + runtime,
            "RuntimeDirectoryMode=0700",
            "UMask=0077",
            "ExecStartPre=+/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C "
            + python_environment(payload)
            + f" {python} -s -P -m scripts.release.services check-binding"
            + f" --release-id {release} --configuration-id {batch}",
            f"MemoryMax={memory}M",
            "MemorySwapMax=0",
            f"TasksMax={pids}",
            f"CPUQuota={cpu}%",
            "LimitCORE=0",
            "LimitFSIZE=64M",
            "LimitNOFILE=" + ("64" if name == "problem-import" else "256"),
            "LimitSTACK=16M",
            "NoNewPrivileges=yes",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "ProtectKernelTunables=yes",
            "ProtectKernelModules=yes",
            "ProtectControlGroups=yes",
            "ProtectProc=invisible",
            "RestrictNamespaces=yes",
            "RestrictSUIDSGID=yes",
            "LockPersonality=yes",
            "PrivateTmp=yes",
            "PrivateDevices=yes",
            "KillMode=control-group",
            "TimeoutStartSec=120",
            "TimeoutStopSec=30",
            "Restart=on-failure",
            "RestartSec=30",
            "RuntimeMaxSec=24h",
            "StandardOutput=journal",
            "StandardError=journal",
            "LogRateLimitIntervalSec=30s",
            "LogRateLimitBurst=100",
            "LogNamespace=" + journal.NAMESPACE,
        ]
        if environment:
            path = configuration.installed_path(release, batch, environment)
            values += ["EnvironmentFile=" + str(path)]
            # The common storage identity is intentional. A separate PID
            # namespace prevents the import parser from reading API credentials
            # from a same-UID process; actual namespace support is verified in VM.
            values += ["PrivatePIDs=yes"]
        if credential:
            values += [
                "LoadCredential="
                + credential
                + ":"
                + str(configuration.installed_path(release, batch, credential)),
                "ExecStart="
                + python
                + " -s -P -m scripts.release.service_bootstrap "
                + name
                + f" --release-id {release} --configuration-id {batch}"
                + " --credential-directory=%d --sha256="
                + receipt["configuration_files"][credential],
                "UnsetEnvironment=KF_OJ_DATABASE_URL KF_OJ_IDENTITY_OPERATOR_DATABASE_URL"
                " KF_OJ_COORDINATOR_TOKEN KF_OJ_AMQP_URL",
            ]
        else:
            values += [
                "ExecStart="
                + python
                + " -s -P -m scripts.release.service_bootstrap "
                + name
                + f" --release-id {release} --configuration-id {batch}"
            ]
        if name in {"api", "problem-import"}:
            values += [
                "ReadWritePaths=/var/lib/kf-onlinejudge/artifacts /var/lib/kf-onlinejudge/imports"
            ]
        elif name == "worker":
            values += ["ReadWritePaths=/var/lib/kf-onlinejudge/worker"]
        if name == "problem-import":
            values += ["PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX"]
        else:
            values += ["RestrictAddressFamilies=AF_UNIX AF_INET"]
        values += ["[Install]", "WantedBy=multi-user.target"]
        files[unit_name] = ("\n".join(values) + "\n").encode()
    return files


def generate(
    bundle: Path, expected: str, source: Path, expected_receipt: str, output: Path
) -> dict:
    data = manifest.verify(bundle, expected)
    for name in ("services.py", "service_profiles.py", "service_bootstrap.py", "layout.py"):
        shipped = bundle / "payload/delivery/scripts/release" / name
        if not shipped.is_file() or digest(shipped) != digest(Path(__file__).parent / name):
            raise DeliveryError("bundle lacks the matching ordinary service launch modules")
    receipt_file = source / "receipt.json"
    configuration.private(receipt_file)
    if digest(receipt_file) != expected_receipt:
        raise DeliveryError("service source configuration receipt differs")
    from .policy import read_json

    receipt = configuration.validate_receipt(read_json(receipt_file, 16384), installed=False)
    if hashlib.sha256(canonical(receipt)).hexdigest() != expected_receipt:
        raise DeliveryError("canonical generated configuration receipt required")
    if receipt["source_sha"] != data["source_sha"] or receipt["manifest_sha256"] != expected:
        raise DeliveryError("service configuration source differs from bundle")
    if {p.name for p in source.iterdir()} != configuration.CONFIG_FILES | {"receipt.json"}:
        raise DeliveryError("unexpected service configuration member")
    paths = {name: source / name for name in configuration.CONFIG_FILES}
    for name, path in paths.items():
        configuration.private(path)
        if digest(path) != receipt["configuration_files"][name]:
            raise DeliveryError("service configuration bytes differ")
    configuration.validate_contents(
        paths, bundle / "payload", data, attest=receipt["operator_attested_measurement"]
    )
    if output.exists() or output.is_symlink():
        raise DeliveryError("service output must be new")
    output.mkdir(mode=0o700)
    fingerprints = {}
    for name, contents in render(receipt).items():
        path = output / name
        path.write_bytes(contents)
        path.chmod(0o600)
        fingerprints[name] = digest(path)
    result = {
        "schema_version": 1,
        "source_sha": data["source_sha"],
        "configuration_id": receipt["configuration_id"],
        "configuration_receipt_sha256": expected_receipt,
        "files": fingerprints,
        "installed": False,
        "services_started": False,
        "execution_verified": False,
    }
    (output / "receipt.json").write_bytes(canonical(result))
    (output / "receipt.json").chmod(0o600)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded ordinary service composition")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-binding")
    check.add_argument("--release-id", required=True)
    check.add_argument("--configuration-id", required=True)
    check = sub.add_parser("generate")
    check.add_argument("bundle", type=Path)
    check.add_argument("--manifest-sha256", required=True)
    check.add_argument("--configuration", type=Path, required=True)
    check.add_argument("--configuration-receipt-sha256", required=True)
    check.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "check-binding":
            if os.geteuid() != 0:
                raise DeliveryError("protected service binding check requires operator")
            active = install.selection()
            if (active["release_id"], active["configuration_id"]) != (
                args.release_id,
                args.configuration_id,
            ):
                raise DeliveryError("unit belongs to an unselected release/configuration")
            journal.check_profile(allow_read_only=True)
            result = {"binding_valid": True, "execution_verified": False}
        else:
            result = generate(
                args.bundle,
                args.manifest_sha256,
                args.configuration,
                args.configuration_receipt_sha256,
                args.output,
            )
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError) as error:
        parser.exit(
            1,
            "Service refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
