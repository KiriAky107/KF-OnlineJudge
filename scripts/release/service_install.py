"""Root ordinary-unit installation; no service start, migration or readiness claim."""

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from pathlib import Path

from . import configuration, install, manifest, preflight, services
from .policy import DeliveryError, canonical, digest, fields, read_json, sha256

UNITS = Path("/etc/kf-onlinejudge/units")
SYSTEM_UNITS = Path("/etc/systemd/system")
NAMES = {"kf-" + name + ".service" for name in services.PROFILES}
RECEIPT_FIELDS = {
    "schema_version",
    "source_sha",
    "configuration_id",
    "configuration_receipt_sha256",
    "files",
    "installed",
    "services_started",
    "execution_verified",
}


def validate_receipt(value: dict) -> dict:
    value = fields(value, RECEIPT_FIELDS, "ordinary service receipt")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["source_sha"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", value["source_sha"])
        or not isinstance(value["configuration_id"], str)
        or not re.fullmatch(r"cfg-[0-9a-f]{24}", value["configuration_id"])
        or any(
            value[key] is not False
            for key in (
                "installed",
                "services_started",
                "execution_verified",
            )
        )
    ):
        raise DeliveryError("ordinary service receipt binding differs")
    sha256(value["configuration_receipt_sha256"])
    fields(value["files"], NAMES, "ordinary unit inventory")
    for checksum in value["files"].values():
        sha256(checksum)
    return value


def batch_directory(value: dict) -> Path:
    validate_receipt(value)
    return UNITS / ("sd-" + value["source_sha"][:12] + "." + value["configuration_id"])


def previous_batch(target: str, name: str) -> Path:
    """Only a known immutable ordinary-unit target can ever be replaced."""
    if name not in NAMES:
        raise DeliveryError("unexpected ordinary unit")
    path = Path(target)
    if (
        path.name != name
        or path.parent.parent != UNITS
        or not re.fullmatch(r"sd-[0-9a-f]{12}\.cfg-[0-9a-f]{24}", path.parent.name)
        or path.as_posix() != target
    ):
        raise DeliveryError("unknown system unit target; operator review required")
    return path.parent


def current_receipt() -> tuple[dict, dict]:
    selected = install.selection(check_configuration=False)
    path = configuration.installed_receipt_path(
        selected["release_id"], selected["configuration_id"]
    )
    installed = configuration.check_installed(path)
    if install.selection(check_configuration=False) != selected or any(
        installed[key] != selected[key]
        for key in ("release_id", "configuration_id", "manifest_sha256")
    ):
        raise DeliveryError("selected configuration changed during complete receipt validation")
    hashes = {
        name: installed["configuration_files"][
            str(
                configuration.installed_path(
                    selected["release_id"],
                    selected["configuration_id"],
                    name,
                )
            )
        ]
        for name in configuration.CONFIG_FILES
    }
    original = {**installed, "installed": False, "configuration_files": hashes}
    configuration.validate_receipt(original, installed=False)
    return selected, original


def check_group(directory: Path, receipt: dict) -> None:
    preflight.protected(directory, directory=True)
    if stat.S_IMODE(directory.stat().st_mode) != 0o700:
        raise DeliveryError("private immutable unit batch required")
    if {p.name for p in directory.iterdir()} != NAMES | {"receipt.json"}:
        raise DeliveryError("unexpected installed unit member")
    path = directory / "receipt.json"
    preflight.protected(path)
    configuration.private(path)
    if canonical(validate_receipt(read_json(path, 16384))) != canonical(receipt):
        raise DeliveryError("installed unit receipt differs")
    for name in sorted(NAMES):
        path = directory / name
        preflight.protected(path)
        if (
            path.stat().st_size > 32768
            or stat.S_IMODE(path.stat().st_mode) != 0o644
            or digest(path) != receipt["files"][name]
        ):
            raise DeliveryError("installed ordinary unit bytes/mode differ")


def known_link(path: Path) -> Path | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_gid != 0:
        raise DeliveryError("unknown system unit; refused overwrite")
    directory = previous_batch(os.readlink(path), path.name)
    receipt_path = directory / "receipt.json"
    preflight.protected(receipt_path)
    configuration.private(receipt_path)
    receipt = validate_receipt(read_json(receipt_path, 16384))
    if batch_directory(receipt) != directory:
        raise DeliveryError("old unit target and receipt binding differ")
    check_group(directory, receipt)
    return directory


def systemctl(*arguments: str) -> str:
    result = subprocess.run(
        ["/usr/bin/systemctl", *arguments],
        timeout=15,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    if result.returncode or len(result.stdout) > 8192 or len(result.stderr) > 8192:
        raise DeliveryError("bounded unit manager operation refused")
    return result.stdout


@manifest.verified_operation
def install_units(source: Path, expected: str) -> dict:
    if os.getuid() != 0 or os.geteuid() != 0:
        raise DeliveryError("ordinary unit installation requires root operator")
    preflight.protected(source, directory=True)
    path = source / "receipt.json"
    preflight.protected(path)
    configuration.private(path)
    if digest(path) != sha256(expected):
        raise DeliveryError("ordinary unit source receipt fingerprint differs")
    receipt = validate_receipt(read_json(path, 16384))
    selected, original = current_receipt()
    if (
        receipt["source_sha"] != original["source_sha"]
        or receipt["configuration_id"] != selected["configuration_id"]
        # Generated units bind the canonical *source* configuration receipt;
        # installing that configuration changes its installed flag/path keys.
        or receipt["configuration_receipt_sha256"]
        != hashlib.sha256(canonical(original)).hexdigest()
    ):
        raise DeliveryError("ordinary unit source is not the selected configuration")
    # A separately trusted operator cannot install a new launcher against an
    # older delivery that lacks that launcher or has different bytes.
    payload = install.RELEASES / selected["release_id"] / "payload"
    for name in (
        "services.py",
        "service_profiles.py",
        "service_bootstrap.py",
        "service_install.py",
        "layout.py",
        "manifest.py",
    ):
        shipped = payload / "delivery/scripts/release" / name
        preflight.protected(shipped)
        if digest(shipped) != digest(Path(__file__).parent / name):
            raise DeliveryError("ordinary unit installer differs from selected delivery")
    if {p.name for p in source.iterdir()} != NAMES | {"receipt.json"}:
        raise DeliveryError("unexpected ordinary unit source member")
    contents = services.render(original)
    for name in sorted(NAMES):
        path = source / name
        preflight.protected(path)
        configuration.private(path)
        if path.stat().st_size > 32768 or digest(path) != receipt["files"][name]:
            raise DeliveryError("ordinary unit source fingerprint differs")
        if path.read_bytes() != contents[name]:
            raise DeliveryError("ordinary unit is not the fixed trusted composition")
    install.require_stopped()
    preflight.protected(SYSTEM_UNITS, directory=True)
    # Check every existing pointer before changing any. Regular files, foreign
    # links and incompletely recorded old batches require explicit review.
    for name in sorted(NAMES):
        known_link(SYSTEM_UNITS / name)
    install.directory(UNITS, 0o700)
    directory = batch_directory(receipt)
    install.directory(directory, 0o700)
    if not {p.name for p in directory.iterdir()} <= NAMES | {"receipt.json"}:
        raise DeliveryError("unexpected partial unit batch member")
    for path in directory.iterdir():
        preflight.protected(path)
        maximum = 16384 if path.name == "receipt.json" else 32768
        if path.stat().st_size > maximum:
            raise DeliveryError("partial unit batch member exceeds bound")
    for name in sorted(NAMES):
        install.exclusive_file(directory / name, contents[name], 0o644)
    install.exclusive_file(directory / "receipt.json", canonical(receipt), 0o600)
    check_group(directory, receipt)
    if install.selection() != selected:
        raise DeliveryError("selected configuration changed during unit preparation")
    install.require_stopped()
    for name in sorted(NAMES):
        path = SYSTEM_UNITS / name
        old = known_link(path)
        if old == directory:
            continue
        temporary = SYSTEM_UNITS / (".kf-unit-" + secrets.token_hex(16))
        created = None
        try:
            temporary.symlink_to(directory / name)
            info = temporary.lstat()
            created = (info.st_dev, info.st_ino)
            os.replace(temporary, path)
            install.sync_directory(SYSTEM_UNITS)
        finally:
            if created is not None and temporary.is_symlink():
                info = temporary.lstat()
                if (info.st_dev, info.st_ino) == created:
                    temporary.unlink()
    systemctl("daemon-reload")
    loaded = {}
    for name in sorted(NAMES):
        values = dict(
            line.split("=", 1)
            for line in systemctl(
                "show",
                name,
                "--property=LoadState,ActiveState,MainPID,FragmentPath",
            ).splitlines()
            if "=" in line
        )
        if (
            values.get("LoadState") != "loaded"
            or values.get("ActiveState") != "inactive"
            or values.get("MainPID") != "0"
            or values.get("FragmentPath") not in {str(SYSTEM_UNITS / name), str(directory / name)}
            or known_link(SYSTEM_UNITS / name) != directory
        ):
            raise DeliveryError("ordinary unit manager load readback differs")
        loaded[name] = values
    if install.selection() != selected:
        raise DeliveryError("selected configuration changed during unit loading")
    return {
        "source_sha": receipt["source_sha"],
        "configuration_id": receipt["configuration_id"],
        "units_installed": True,
        "unit_manager_readback": loaded,
        "services_started": False,
        "execution_verified": False,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Root ordinary-unit installation")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--receipt-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(install_units(args.directory, args.receipt_sha256), sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(
            1,
            "Unit installation refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
