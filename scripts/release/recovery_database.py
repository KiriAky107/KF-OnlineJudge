"""Root dispatch/receipt for an ordinary, finite and isolated DB identity recovery."""

import argparse
import fcntl
import json
import os
import re
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from . import (
    backup,
    backup_manifest,
    configuration,
    dependency_initialize,
    install,
    manifest,
    preflight,
    recovery_identity,
    restore,
    restore_policy,
)
from .policy import DeliveryError, canonical, digest, fields, integer, read_json, sha256
from .services import python_environment


def validate_complete(
    prepared: dict, data: dict, identity: dict, recovery_id: str, data_sha: str
) -> None:
    """A completed historical recovery survives reboot; an incomplete one never does."""
    fields(
        prepared,
        {
            "schema_version",
            "recovery_id",
            "backup_id",
            "private_backup_sha256",
            "source_sha",
            "configuration_id",
            "boot_id",
            "data_restored",
            "database_credentials_rotated",
            "services_started",
            "execution_verified",
        },
        "prepared recovery",
    )
    if (
        type(prepared["schema_version"]) is not int
        or prepared["schema_version"] != 1
        or prepared["recovery_id"] != restore.identifier(recovery_id)
        or any(
            prepared[key] is not False
            for key in (
                "data_restored",
                "database_credentials_rotated",
                "services_started",
                "execution_verified",
            )
        )
        or not isinstance(prepared["source_sha"], str)
        or re.fullmatch(r"[0-9a-f]{40}", prepared["source_sha"]) is None
        or not isinstance(prepared["configuration_id"], str)
        or re.fullmatch(r"cfg-[0-9a-f]{24}", prepared["configuration_id"]) is None
        or not isinstance(prepared["boot_id"], str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            prepared["boot_id"],
        )
        is None
    ):
        raise DeliveryError("incomplete or foreign prepared recovery; admission refused")
    validate_data_receipt(data, recovery_id, prepared, prepared["source_sha"], prepared["boot_id"])
    if any(data[key] != value for key, value in prepared.items() if key != "data_restored"):
        raise DeliveryError("recovery data receipt differs from prepared binding")
    fields(
        identity,
        {
            "schema_version",
            "recovery_id",
            "source_sha",
            "configuration_id",
            "boot_id",
            "data_receipt_sha256",
            "bootstrap_sha256",
            "cluster_identity",
            "temporary_server_stopped",
            "database_credentials_rotated",
            "ordinary_identity_readback",
            "services_started",
            "schema_migrated",
            "execution_state_changed",
            "execution_verified",
        },
        "completed recovery identity",
    )
    if (
        any(
            identity[key] != prepared[key]
            for key in (
                "schema_version",
                "recovery_id",
                "source_sha",
                "configuration_id",
                "boot_id",
            )
        )
        or type(identity["schema_version"]) is not int
        or identity["data_receipt_sha256"] != sha256(data_sha)
        or identity["temporary_server_stopped"] is not True
        or identity["database_credentials_rotated"] is not True
        or any(
            identity[key] is not False
            for key in (
                "services_started",
                "schema_migrated",
                "execution_state_changed",
                "execution_verified",
            )
        )
    ):
        raise DeliveryError("recovery identity is incomplete or unbound; admission refused")
    sha256(identity["bootstrap_sha256"])
    dependency_initialize.validate_cluster_identity(identity["cluster_identity"])
    read_result(canonical(identity["ordinary_identity_readback"]))


@contextmanager
def admission_guard():
    """Hold a read-only shared lock through startup, refusing all partial recoveries."""
    preflight.protected(restore.STATE, directory=True)
    if stat.S_IMODE(restore.STATE.stat().st_mode) != 0o700:
        raise DeliveryError("private recovery state required before admission")
    lock = restore.STATE / "restore.lock"
    preflight.protected(lock)
    configuration.private(lock)
    if lock.stat().st_size != 0:
        raise DeliveryError("unknown recovery lock contents")
    descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        for count, directory in enumerate(restore.STATE.iterdir(), start=1):
            if count > 9:
                raise DeliveryError("recovery history exceeds startup observation bound")
            if directory == lock:
                continue
            restore.identifier(directory.name)
            preflight.protected(directory, directory=True)
            if stat.S_IMODE(directory.stat().st_mode) != 0o700:
                raise DeliveryError("private recovery record directory required")
            records = {}
            for name in ("prepared.json", "data-restored.json", "identity-recovered.json"):
                path = directory / name
                preflight.protected(path)
                configuration.private(path)
                records[name] = read_json(path, 16384)
            validate_complete(
                records["prepared.json"],
                records["data-restored.json"],
                records["identity-recovered.json"],
                directory.name,
                digest(directory / "data-restored.json"),
            )
        yield
    finally:
        os.close(descriptor)


def bootstrap_fingerprint() -> str:
    """Read the current cluster's proven credential marker, including restored nodes."""
    candidates = []
    cluster = dependency_initialize.cluster_identity()
    with admission_guard():
        initial = dependency_initialize.RECEIPT
        if initial.exists() or initial.is_symlink():
            preflight.protected(initial)
            configuration.private(initial)
            candidates.append(dependency_initialize.validate_receipt(read_json(initial, 8192)))
        for directory in restore.STATE.iterdir():
            if directory.name == "restore.lock":
                continue
            # admission_guard already validated all linked completion receipts.
            candidates.append(read_json(directory / "identity-recovered.json", 16384))
    if len(candidates) != 1 or candidates[0]["cluster_identity"] != cluster:
        raise DeliveryError("current DB bootstrap fingerprint is absent or ambiguous")
    return sha256(candidates[0]["bootstrap_sha256"])


def validate_data_receipt(
    value: dict, recovery_id: str, selected: dict, source_sha: str, boot: str
) -> dict:
    fields(
        value,
        {
            "schema_version",
            "recovery_id",
            "backup_id",
            "private_backup_sha256",
            "source_sha",
            "configuration_id",
            "boot_id",
            "data_restored",
            "database_credentials_rotated",
            "services_started",
            "execution_verified",
            "restored_payload_bytes",
            "restored_files",
            "restored_directories",
            "historical_configuration_active",
            "factory_replay_history_retained",
        },
        "cold data restoration receipt",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["recovery_id"] != restore.identifier(recovery_id)
        or value["source_sha"] != source_sha
        or value["configuration_id"] != selected["configuration_id"]
        or value["boot_id"] != boot
        or value["data_restored"] is not True
        or value["database_credentials_rotated"] is not False
        or value["services_started"] is not False
        or value["execution_verified"] is not False
        or value["historical_configuration_active"] is not False
        or value["factory_replay_history_retained"] is not True
    ):
        raise DeliveryError("cold data restoration is not this boot's selected complete data step")
    backup.identifier(value["backup_id"])
    sha256(value["private_backup_sha256"])
    integer(value["restored_payload_bytes"], 1, 7 * 1024**3, "restored bytes")
    for key in ("restored_files", "restored_directories"):
        integer(value[key], 1, 50000, "restored entries")
    if value["restored_files"] + value["restored_directories"] > 50000:
        raise DeliveryError("restored inventory limit differs")
    return value


def recover(recovery_id: str, data_sha: str, password_file: Path, password_sha: str) -> dict:
    # Share the data step's lock with admission. A partial password rotation
    # must not race service startup or another restoration operation.
    lock = restore.STATE / "restore.lock"
    configuration.private(lock)
    if lock.stat().st_size != 0:
        raise DeliveryError("unknown recovery lock contents")
    descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return recover_locked(recovery_id, data_sha, password_file, password_sha)
    finally:
        os.close(descriptor)


def recover_locked(recovery_id: str, data_sha: str, password_file: Path, password_sha: str) -> dict:
    if (os.getuid(), os.geteuid(), os.getgid()) != (0, 0, 0):
        raise DeliveryError("explicit root recovery identity dispatcher required")
    restore.identifier(recovery_id)
    restore.stopped()
    selected = install.selection()
    payload = install.RELEASES / selected["release_id"] / "payload"
    data = manifest.verify(payload.parent, selected["manifest_sha256"])
    for name in (
        "recovery_database.py",
        "recovery_identity.py",
        "restore.py",
        "restore_policy.py",
        "dependencies.py",
        "dependency_policy.py",
        "layout.py",
    ):
        if digest(payload / "delivery/scripts/release" / name) != digest(
            Path(__file__).parent / name
        ):
            raise DeliveryError("recovery identity program differs from selected delivery")
    directory = restore.STATE / recovery_id
    preflight.protected(directory, directory=True)
    preflight_path = directory / "data-restored.json"
    preflight.protected(preflight_path)
    configuration.private(preflight_path)
    if digest(preflight_path) != sha256(data_sha):
        raise DeliveryError("separate cold data restoration fingerprint differs")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    recorded = validate_data_receipt(
        read_json(preflight_path, 8192), recovery_id, selected, data["source_sha"], boot
    )
    preflight.protected(password_file)
    configuration.private(password_file)
    if re.fullmatch(r"/[A-Za-z0-9_./-]+", str(password_file)) is None or digest(
        password_file
    ) != sha256(password_sha):
        raise DeliveryError("private fresh DB bootstrap fingerprint/path differs")
    dependency_initialize.validate_password(password_file.read_bytes())
    # Bind the latest cluster fingerprint through the separately hashed private
    # snapshot. Restored nodes need not have an initial-cluster receipt.
    snapshot_path = backup.ROOT / recorded["backup_id"] / "manifest.json"
    preflight.protected(snapshot_path)
    configuration.private(snapshot_path)
    if digest(snapshot_path) != recorded["private_backup_sha256"]:
        raise DeliveryError("private recovery snapshot fingerprint changed")
    previous = backup_manifest.validate(backup_manifest.read_metadata(snapshot_path))
    if previous["backup_id"] != recorded["backup_id"]:
        raise DeliveryError("private recovery snapshot ID differs")
    if previous["database_bootstrap_sha256"] == password_sha:
        raise DeliveryError("new recovery DB bootstrap secret required")
    current = restore_policy.snapshot(
        {
            name: configuration.installed_path(
                selected["release_id"], selected["configuration_id"], name
            )
            for name in configuration.CONFIG_FILES
        }
    )
    fresh_bootstrap = password_file.read_text().strip()
    if any(
        fresh_bootstrap == urlsplit(endpoint).password
        for kind in ("database", "amqp")
        for endpoint in current["endpoints"][kind].values()
    ):
        raise DeliveryError("recovery bootstrap must be distinct from all service credentials")
    credential = directory / "identity-connections.json"
    install.exclusive_file(credential, canonical(current["endpoints"]), 0o600)
    if (directory / "identity-recovered.json").exists():
        raise DeliveryError("DB identities already recovered; no implicit repeat/reset")
    unit = recovery_identity.UNIT
    status = backup.manager("show", unit, "--property=LoadState,ActiveState,MainPID")
    if status != {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"}:
        raise DeliveryError("unknown or active DB identity recovery unit")
    if recovery_identity.RUNTIME.exists() or recovery_identity.RUNTIME.is_symlink():
        raise DeliveryError("unknown residual DB identity recovery runtime directory")
    identity = dependency_initialize.cluster_identity()
    description = "KF fixed DB recovery " + recovery_id + "." + selected["configuration_id"]
    command = [
        "/usr/bin/systemd-run",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        "--unit=" + unit,
        "--description=" + description,
        "--slice=kfonlinejudge.slice",
    ]
    for key, setting in {
        "Type": "exec",
        "User": "postgres",
        "Group": "postgres",
        "MemoryMax": "512M",
        "MemorySwapMax": "0",
        "CPUQuota": "100%",
        "TasksMax": "64",
        "RuntimeMaxSec": "240",
        "TimeoutStopSec": "45",
        "KillMode": "control-group",
        "KillSignal": "SIGINT",
        "Restart": "no",
        "LimitCORE": "0",
        "LimitFSIZE": "256M",
        "LimitNOFILE": "256",
        "LimitSTACK": "16M",
        "NoNewPrivileges": "yes",
        "CapabilityBoundingSet": "",
        "AmbientCapabilities": "",
        "UMask": "0077",
        "PrivateTmp": "yes",
        "PrivateDevices": "yes",
        "PrivateNetwork": "yes",
        "PrivatePIDs": "yes",
        "ProtectHome": "yes",
        "ProtectSystem": "strict",
        "ProtectControlGroups": "yes",
        "RestrictAddressFamilies": "AF_UNIX",
        "RuntimeDirectory": recovery_identity.RUNTIME.name,
        "RuntimeDirectoryMode": "0700",
        "ReadWritePaths": str(dependency_initialize.DATA.parent),
    }.items():
        command.append("--property=" + key + "=" + setting)
    command += [
        "--property=LoadCredential=connections:" + str(credential),
        "--property=LoadCredential=bootstrap:" + str(password_file),
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin:/bin",
        "LC_ALL=C.UTF-8",
        *python_environment(payload).split(),
        str(payload / "python/bin/python3.13"),
        "-s",
        "-P",
        "-m",
        "scripts.release.recovery_identity",
    ]
    result = None
    dispatched = False
    try:
        dispatched = True
        completed = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=290, check=False
        )
        if completed.returncode or len(completed.stdout) > 16384:
            raise DeliveryError("DB identity recovery failed; target remains stopped")
        result = read_result(completed.stdout)
    finally:
        if dispatched:
            active = backup.manager(
                "show", unit, "--property=LoadState,ActiveState,MainPID,Description,Transient"
            )
            if active.get("LoadState") != "not-found":
                if active.get("Description") != description or active.get("Transient") != "yes":
                    raise DeliveryError("foreign DB recovery unit; refused cleanup")
                from . import service_install

                service_install.systemctl("stop", unit)
            restore.stopped()
            if (
                recovery_identity.RUNTIME.exists()
                or dependency_initialize.DATA.joinpath("postmaster.pid").exists()
            ):
                raise DeliveryError("DB identity recovery shutdown/runtime cleanup unconfirmed")
    if install.selection() != selected or dependency_initialize.cluster_identity() != identity:
        raise DeliveryError("recovery configuration or cluster identity changed")
    if digest(preflight_path) != data_sha:
        raise DeliveryError("cold data restoration receipt changed")
    record = {
        "schema_version": 1,
        "recovery_id": recovery_id,
        "source_sha": data["source_sha"],
        "configuration_id": selected["configuration_id"],
        "boot_id": boot,
        "data_receipt_sha256": data_sha,
        "bootstrap_sha256": password_sha,
        "cluster_identity": identity,
        "temporary_server_stopped": True,
        "database_credentials_rotated": True,
        "ordinary_identity_readback": result,
        "services_started": False,
        "schema_migrated": False,
        "execution_state_changed": False,
        "execution_verified": False,
    }
    install.exclusive_file(directory / "identity-recovered.json", canonical(record), 0o600)
    return {
        "recovery_id": recovery_id,
        "database_credentials_rotated": True,
        "receipt_sha256": digest(directory / "identity-recovered.json"),
        "temporary_server_stopped": True,
        "schema_migrated": False,
        "services_started": False,
        "execution_verified": False,
    }


def read_result(raw: bytes) -> dict:
    from .operator_units import decode

    value = decode(raw, 16384)
    fields(
        value,
        {
            "dependency",
            "actual_cgroup_limits",
            "data_filesystem_bytes",
            "execution_verified",
            "rotated_roles",
            "role_authority_sha256",
            "authenticated",
            "schema_migrated",
            "execution_state_changed",
            "effective_settings",
            "temporary_server_stopped",
            "services_started",
        },
        "ordinary DB recovery result",
    )
    if (
        value["dependency"] != "postgres"
        or value["rotated_roles"] != sorted(["postgres", *configuration.PROFILES.values()])
        or value["actual_cgroup_limits"]
        != {
            "memory.max": "536870912",
            "memory.swap.max": "0",
            "pids.max": "64",
            "cpu.max": "100000 100000",
        }
        or value["authenticated"] is not True
        or value["temporary_server_stopped"] is not True
        or value["schema_migrated"] is not False
        or value["execution_state_changed"] is not False
        or value["services_started"] is not False
        or value["execution_verified"] is not False
    ):
        raise DeliveryError("ordinary DB recovery result is incomplete or unproven")
    integer(
        value["data_filesystem_bytes"], 2 * 1024**3 * 9 // 10, 2 * 1024**3, "DB recovery volume"
    )
    sha256(value["role_authority_sha256"])
    fields(value["effective_settings"], set(recovery_identity.recovery_settings()), "PG settings")
    for item in value["effective_settings"].values():
        fields(item, {"setting", "unit"}, "PG effective setting")
        if (
            not isinstance(item["setting"], str)
            or item["unit"] is not None
            and not isinstance(item["unit"], str)
        ):
            raise DeliveryError("invalid effective PostgreSQL setting")
    recovery_identity.check_settings(
        [{"name": key, **item} for key, item in value["effective_settings"].items()]
    )
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Explicit ordinary DB identity recovery after cold restoration"
    )
    parser.add_argument("--recovery-id", required=True)
    parser.add_argument("--data-receipt-sha256", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--password-sha256", required=True)
    args = parser.parse_args()
    try:
        value = recover(
            args.recovery_id, args.data_receipt_sha256, args.password_file, args.password_sha256
        )
        print(json.dumps(value, sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(
            1,
            "DB recovery refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
