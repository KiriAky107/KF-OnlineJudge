"""Root factory unit composition, separate from ordinary service hardening."""

import argparse
import json
import os
import re
import secrets
import stat
import subprocess
from pathlib import Path

from . import configuration, install, manifest, preflight, service_install, services
from .policy import DeliveryError, canonical, digest, fields, read_json, sha256

UNIT = "kf-sandbox-factory.service"
UNITS = Path("/etc/kf-judge/units")
START_TIMEOUT_SECONDS = 1800


def validate_receipt(value: dict) -> dict:
    fields(
        value,
        {
            "schema_version",
            "source_sha",
            "configuration_id",
            "configuration_receipt_sha256",
            "unit_sha256",
            "installed",
            "services_started",
            "execution_verified",
        },
        "factory unit receipt",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["source_sha"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", value["source_sha"])
        or not isinstance(value["configuration_id"], str)
        or not re.fullmatch(r"cfg-[0-9a-f]{24}", value["configuration_id"])
        or any(
            value[name] is not False
            for name in (
                "installed",
                "services_started",
                "execution_verified",
            )
        )
    ):
        raise DeliveryError("factory unit binding differs")
    sha256(value["configuration_receipt_sha256"])
    sha256(value["unit_sha256"])
    return value


def batch_directory(receipt: dict) -> Path:
    value = validate_receipt(receipt)
    return UNITS / ("sd-" + value["source_sha"][:12] + "." + value["configuration_id"])


def check_batch(directory: Path, receipt: dict) -> None:
    preflight.protected(directory, directory=True)
    if stat.S_IMODE(directory.stat().st_mode) != 0o700:
        raise DeliveryError("private factory unit batch required")
    if {p.name for p in directory.iterdir()} != {UNIT, "receipt.json"}:
        raise DeliveryError("unknown factory batch member")
    path = directory / "receipt.json"
    preflight.protected(path)
    configuration.private(path)
    if canonical(validate_receipt(read_json(path, 8192))) != canonical(receipt):
        raise DeliveryError("factory batch receipt differs")
    path = directory / UNIT
    preflight.protected(path)
    if (
        path.stat().st_size > 32768
        or stat.S_IMODE(path.stat().st_mode) != 0o644
        or digest(path) != receipt["unit_sha256"]
    ):
        raise DeliveryError("factory batch bytes/mode differ")


def known_link(path: Path) -> Path | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_gid != 0:
        raise DeliveryError("unknown factory unit; refused overwrite")
    target = os.readlink(path)
    candidate = Path(target)
    if (
        candidate.as_posix() != target
        or candidate.name != UNIT
        or candidate.parent.parent != UNITS
        or not re.fullmatch(r"sd-[0-9a-f]{12}\.cfg-[0-9a-f]{24}", candidate.parent.name)
    ):
        raise DeliveryError("unknown factory unit pointer")
    receipt_path = candidate.parent / "receipt.json"
    preflight.protected(receipt_path)
    configuration.private(receipt_path)
    receipt = validate_receipt(read_json(receipt_path, 8192))
    if batch_directory(receipt) != candidate.parent:
        raise DeliveryError("factory pointer/receipt binding differs")
    check_batch(candidate.parent, receipt)
    return candidate.parent


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
        "IOAccounting=yes",
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
        f"TimeoutStartSec={START_TIMEOUT_SECONDS}",
        "RuntimeMaxSec=3700",
        "TimeoutStopSec=40",
        "KillMode=mixed",
        "Restart=no",
        "StandardOutput=journal",
        "StandardError=journal",
        "LogRateLimitIntervalSec=30s",
        "LogRateLimitBurst=100",
        "LogNamespace=kf-onlinejudge",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
    return ("\n".join(values) + "\n").encode()


def generate(output: Path) -> dict:
    if os.getuid() != 0 or os.geteuid() != 0:
        raise DeliveryError("Root factory composition requires trusted operator")
    selected, original = service_install.current_receipt()
    payload = install.RELEASES / selected["release_id"] / "payload"
    for name in (
        "factory_service.py",
        "manifest.py",
        "services.py",
        "service_profiles.py",
        "layout.py",
    ):
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


@manifest.verified_operation
def install_unit(source: Path, expected: str) -> dict:
    if os.getuid() != 0 or os.geteuid() != 0:
        raise DeliveryError("factory unit installation requires root operator")
    preflight.protected(source, directory=True)
    path = source / "receipt.json"
    preflight.protected(path)
    configuration.private(path)
    if digest(path) != sha256(expected):
        raise DeliveryError("factory source receipt differs")
    receipt = validate_receipt(read_json(path, 8192))
    selected, original = service_install.current_receipt()
    if (
        receipt["source_sha"] != original["source_sha"]
        or receipt["configuration_id"] != selected["configuration_id"]
        or receipt["configuration_receipt_sha256"] != selected["receipt_sha256"]
    ):
        raise DeliveryError("factory unit does not belong to selected configuration")
    payload = install.RELEASES / selected["release_id"] / "payload"
    for name in (
        "factory_service.py",
        "manifest.py",
        "services.py",
        "service_profiles.py",
        "layout.py",
    ):
        shipped = payload / "delivery/scripts/release" / name
        preflight.protected(shipped)
        if digest(shipped) != digest(Path(__file__).parent / name):
            raise DeliveryError("factory installer differs from selected delivery")
    if {p.name for p in source.iterdir()} != {UNIT, "receipt.json"}:
        raise DeliveryError("unknown factory source member")
    path = source / UNIT
    preflight.protected(path)
    configuration.private(path)
    contents = render(original)
    if (
        path.stat().st_size > 32768
        or digest(path) != receipt["unit_sha256"]
        or path.read_bytes() != contents
    ):
        raise DeliveryError("factory source is not the fixed privileged composition")
    install.require_stopped()
    system = Path("/etc/systemd/system")
    preflight.protected(system, directory=True)
    link = system / UNIT
    known_link(link)
    install.directory(UNITS, 0o700)
    directory = batch_directory(receipt)
    install.directory(directory, 0o700)
    for p in directory.iterdir():
        if p.name not in {UNIT, "receipt.json"}:
            raise DeliveryError("unknown partial factory batch member")
        preflight.protected(p)
        if p.stat().st_size > (8192 if p.name == "receipt.json" else 32768):
            raise DeliveryError("partial factory member exceeds bound")
    install.exclusive_file(directory / UNIT, contents, 0o644)
    install.exclusive_file(directory / "receipt.json", canonical(receipt), 0o600)
    check_batch(directory, receipt)
    if install.selection() != selected:
        raise DeliveryError("selected configuration changed during factory preparation")
    install.require_stopped()
    if known_link(link) != directory:
        temporary = system / (".kf-factory-" + secrets.token_hex(16))
        created = None
        try:
            temporary.symlink_to(directory / UNIT)
            info = temporary.lstat()
            created = (info.st_dev, info.st_ino)
            os.replace(temporary, link)
            install.sync_directory(system)
        finally:
            if created is not None and temporary.is_symlink():
                info = temporary.lstat()
                if (info.st_dev, info.st_ino) == created:
                    temporary.unlink()
    service_install.systemctl("daemon-reload")
    loaded = dict(
        line.split("=", 1)
        for line in service_install.systemctl(
            "show",
            UNIT,
            "--property=LoadState,ActiveState,MainPID,FragmentPath",
        ).splitlines()
        if "=" in line
    )
    if (
        loaded.get("LoadState") != "loaded"
        or loaded.get("ActiveState") != "inactive"
        or loaded.get("MainPID") != "0"
        or loaded.get("FragmentPath") not in {str(link), str(directory / UNIT)}
        or known_link(link) != directory
        or install.selection() != selected
    ):
        raise DeliveryError("factory unit load readback differs")
    return {
        "source_sha": receipt["source_sha"],
        "configuration_id": receipt["configuration_id"],
        "unit_installed": True,
        "unit_manager_readback": loaded,
        "services_started": False,
        "execution_verified": False,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Protected Root factory unit composition")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path)
    action.add_argument("--install", type=Path)
    parser.add_argument("--receipt-sha256")
    args = parser.parse_args(argv)
    try:
        if args.output is not None:
            if args.receipt_sha256 is not None:
                raise DeliveryError("unexpected generator receipt override")
            result = generate(args.output)
        else:
            if args.receipt_sha256 is None:
                raise DeliveryError("explicit factory receipt fingerprint required")
            result = install_unit(args.install, args.receipt_sha256)
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(
            1,
            "Factory composition refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
